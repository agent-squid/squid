const { test, expect } = require('@playwright/test');

// ── SSE helpers ───────────────────────────────────────────────────────────────

const SSE_HEADERS = {
  'Content-Type': 'text/event-stream',
  'Cache-Control': 'no-cache',
  'X-Accel-Buffering': 'no',
};

function sse(...events) {
  return events.map(({ event, data }) => {
    const str = typeof data === 'string' ? data : JSON.stringify(data);
    return event ? `event: ${event}\ndata: ${str}\n\n` : `data: ${str}\n\n`;
  }).join('');
}

const META  = { event: 'meta',  data: { agent: 'claude', backend: 'claude', msg_id: 1, adhoc: false } };
const STATS = { event: 'stats', data: { session_id: 'test-sid', input_tokens: 10, output_tokens: 5, adhoc: false, lookback: 0 } };
const DONE  = { event: 'done',  data: '' };

// ── mock setup ────────────────────────────────────────────────────────────────

async function mockBackend(page) {
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok', boot_time: new Date().toISOString() } }));
  await page.route('**/history**',     r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**',       r => r.fulfill({ json: {} }));
  await page.route('**/topics',        r => r.fulfill({ json: [] }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic: 'squid', exists: true, content: '---\nsquid:\n  code_roots_skipped: true\n---\n', path: '~/.squid/context/topics/squid/memory.md',
    squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
  }}));
  await page.route('**/topics/**',     r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));
}

// Returns { intercepted, fulfill } — intercepted resolves when the /chat
// request is received; calling fulfill(body) sends the SSE response.
function holdChat(page) {
  let _fulfill;
  const intercepted = new Promise(resolve => {
    page.route('**/chat', route => {
      _fulfill = body => route.fulfill({ status: 200, headers: SSE_HEADERS, body });
      resolve();
    });
  });
  return { intercepted, fulfill: body => _fulfill(body) };
}

async function sendMsg(page, text = 'hello') {
  await page.fill('#input', text);
  await page.keyboard.press('Enter');
}

async function setPageHidden(page, hidden) {
  await page.evaluate(hiddenValue => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: hiddenValue });
    document.dispatchEvent(new Event('visibilitychange'));
  }, hidden);
}

// Pause so you can see what's on screen before moving on
const look = (page, ms = 2500) => page.waitForTimeout(ms);

// ── selectors ─────────────────────────────────────────────────────────────────

const THINKING  = '.msg.assistant.msg-thinking';
const RESPONSE  = '.msg.assistant:not(.msg-thinking)';
const MSG_ERROR = '.msg-error';

// ── tests ─────────────────────────────────────────────────────────────────────

test.describe('response bubble', () => {
  test.beforeEach(async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
  });

  test('does not appear in DOM before done', async ({ page }) => {
    const { intercepted, fulfill } = holdChat(page);

    await sendMsg(page);
    await intercepted;

    // ── LOOK: thinking bubble visible, response bubble absent ────────────────
    await expect(page.locator(THINKING)).toBeVisible();
    await expect(page.locator(RESPONSE)).not.toBeAttached();
    await look(page);  // pause — observe: only thinking bubble, no response bubble yet

    await fulfill(sse(META, { data: 'Hello!' }, DONE));

    await expect(page.locator(RESPONSE)).toBeVisible();
    await look(page);  // pause — observe: response bubble now at bottom
  });

  test('thinking bubble height can be doubled', async ({ page }) => {
    const { intercepted } = holdChat(page);

    await sendMsg(page);
    await intercepted;

    const thinking = page.locator(THINKING);
    const live = thinking.locator('.thinking-live');
    const normalMax = await live.evaluate(el => parseFloat(getComputedStyle(el).maxHeight));
    const heightBtn = thinking.getByRole('button', { name: 'Double thinking height' });
    await expect(heightBtn).not.toBeVisible();
    await thinking.evaluate(el => {
      const liveEl = el.querySelector('.thinking-live');
      liveEl.textContent = Array.from({ length: 16 }, (_, i) => `thinking line ${i + 1}`).join('\n');
      window.updateThinkingHeightButton(el);
    });
    await expect(heightBtn).toBeVisible();
    expect(await thinking.evaluate(el => {
      const bubble = el.getBoundingClientRect();
      const btn = el.querySelector('.thinking-height-btn').getBoundingClientRect();
      return btn.bottom <= bubble.bottom && btn.bottom >= bubble.bottom - 12 && btn.right <= bubble.right;
    })).toBe(true);
    await heightBtn.click();

    await expect(thinking).toHaveClass(/thinking-tall/);
    await expect(thinking.getByRole('button', { name: 'Normal thinking height' })).toBeVisible();
    const tallMax = await live.evaluate(el => parseFloat(getComputedStyle(el).maxHeight));
    expect(tallMax).toBeGreaterThan(normalMax * 1.8);
  });

  test('appears at bottom of #messages on done', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Response text' }, STATS, DONE),
    }));

    await sendMsg(page);
    await expect(page.locator(RESPONSE)).toBeVisible();

    const last = page.locator('#messages > *').last();
    await expect(last).toHaveClass(/stats/);
    await look(page);  // pause — observe: stats line is last child, bubble above it
  });

  test('renders response tildes literally instead of strikethrough', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(META, { data: 'Use ~/Work/squid and ~~do not strike~~ here.' }, DONE),
    }));

    await sendMsg(page);
    const response = page.locator(RESPONSE);
    await expect(response).toContainText('Use ~/Work/squid and ~~do not strike~~ here.');
    await expect(response.locator('del')).toHaveCount(0);
  });

  test('context indicator exposes the Squid message ID', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Response text' }, STATS, DONE),
    }));

    await sendMsg(page);
    const ctx = page.locator(RESPONSE).locator('.user-ctx');
    await expect(ctx).toHaveText(/^ctx:/);
    await expect(ctx).not.toContainText('#1');

    await ctx.click();
    await expect(page.locator('#ctx-popup')).toContainText('message#1');
  });

  test('clicking a header route sets composer route without filtering history', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Hello' }, DONE),
    }));

    await sendMsg(page, '#squid@claude remember this route');
    await expect(page.locator(`${RESPONSE} .response-header .tag-topic`)).not.toHaveClass(/clickable/);
    await expect(page.locator(`${RESPONSE} .response-header .tag-agent`)).not.toHaveClass(/clickable/);
    let historyReloads = 0;
    await page.route('**/history**', r => {
      historyReloads++;
      return r.fulfill({ json: { items: [], has_more: false } });
    });
    await page.locator(`${RESPONSE} .response-header .tag-topic`).click();
    await page.waitForTimeout(300);

    await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
    await expect(page.locator('#topic-chip')).toContainText('#squid');
    await expect(page.locator('#topic-chip')).toContainText('@claude');
    await expect(page.locator('#filter-badge')).not.toHaveClass(/active/);
    await expect(page.locator(RESPONSE)).toContainText('Hello');
    expect(historyReloads).toBe(0);
  });

  test('context indicator shows compact session turn, memory, and pin counts', async ({ page }) => {
    await page.route('**/topics/*/memory', r => r.fulfill({ json: {
      topic: 'squid',
      exists: true,
      content: 'Project preference',
      path: '~/.squid/context/topics/squid/memory.md',
    }}));
    await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({ json: { session_id: null, cwd: null } }));
    await page.evaluate(() => {
      localStorage.setItem('pinnedItems', JSON.stringify([
        { id: 7, topic: 'squid', agent: 'claude', session_id: 'other', content: 'Pinned one' },
        { id: 8, topic: 'squid', agent: 'claude', session_id: 'other', content: 'Pinned two' },
      ]));
    });
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(
        META,
        { data: 'Response text' },
        { event: 'stats', data: { session_id: 'test-sid', input_tokens: 10, output_tokens: 5, adhoc: false, lookback: 0, session_turn_count: 18 } },
        DONE,
      ),
    }));

    await sendMsg(page, '#squid@claude hello');
    const ctx = page.locator(RESPONSE).locator('.user-ctx');
    await expect(ctx).toHaveText('ctx: sess 18t · mem · 2p');

    await ctx.click();
    await expect(page.locator('#ctx-popup')).toContainText('session context18 turns');
    const regularLinkColor = await page.locator('#ctx-popup .ctx-popup-link').first()
      .evaluate(el => getComputedStyle(el).color);
    await expect(page.locator('#ctx-popup .ctx-popup-tag').first()).toHaveCSS('color', regularLinkColor);

    await page.locator('#ctx-popup .ctx-popup-pin[data-pin-id="7"]').click();
    await expect(page.locator('#msg-modal')).toHaveClass(/open/);
    await expect(page.locator('#msg-modal-title')).toContainText('Message #7');
    await expect(page.locator('#ctx-popup')).not.toHaveClass(/open/);
  });

  test('context popup shows flow run id when present', async ({ page }) => {
    await page.unroute('**/history**');
    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 42,
        role: 'assistant',
        topic: 'squid',
        agent: 'codex',
        adhoc: false,
        status: 'done',
        content: 'Flow response',
        prompt: 'Flow prompt',
        prompt_source: 'human',
        flow_run_id: 'flow-test-123',
        flow_route: '#squid@codex>@review',
        timestamp: '2026-07-16T12:00:00Z',
      }],
      has_more: false,
    }}));
    await page.reload();

    const ctx = page.locator('.msg.assistant.history-item .user-ctx');
    await expect(ctx).toBeVisible();
    await ctx.click();

    await expect(page.locator('#ctx-popup')).toContainText('flow run');
    await expect(page.locator('#ctx-popup')).toContainText('flow-test-123');
  });

  test('ctx popup near the top stays below the topbar on desktop and mobile', async ({ page }) => {
    for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 700 }]) {
      await page.setViewportSize(viewport);
      await page.goto('/');
      await page.evaluate(() => {
        document.getElementById('top-test-ctx')?.remove();
        const app = document.getElementById('app');
        const appRect = app.getBoundingClientRect();
        const topbarRect = document.getElementById('topbar').getBoundingClientRect();
        const anchor = document.createElement('span');
        anchor.id = 'top-test-ctx';
        anchor.className = 'user-ctx';
        anchor.textContent = 'ctx: top';
        anchor.dataset.msgId = '99';
        anchor.style.position = 'absolute';
        anchor.style.right = '1rem';
        anchor.style.top = `${topbarRect.bottom - appRect.top + 2}px`;
        anchor.addEventListener('click', e => {
          e.stopPropagation();
          showCtxPopup(anchor);
        });
        app.appendChild(anchor);
      });

      await page.locator('#top-test-ctx').click();
      await expect(page.locator('#ctx-popup')).toHaveClass(/open/);
      await expect.poll(() => page.evaluate(() => {
        const popup = document.getElementById('ctx-popup').getBoundingClientRect();
        const topbar = document.getElementById('topbar').getBoundingClientRect();
        return {
          belowTopbar: popup.top >= topbar.bottom,
          insideBottom: popup.bottom <= window.innerHeight,
        };
      })).toEqual({ belowTopbar: true, insideBottom: true });
      await page.locator('#ctx-popup').evaluate(popup => popup.classList.remove('open'));
    }
  });

  test('ctx popup exposes a thought trace link when tool calls or status text were recorded', async ({ page }) => {
    await page.route('**/chat/1/events**', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(
        { event: 'status', data: 'Checking the repo first.' },
        { event: 'tool', data: { name: 'Bash', command: 'ls -la' } },
        { event: 'status', data: 'Now reading the target file.' },
        { data: 'Final answer text' },
        { event: 'done', data: '' },
      ),
    }));
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'status', data: 'Checking the repo first.' },
        { event: 'tool', data: { name: 'Bash', command: 'ls -la' } },
        { event: 'status', data: 'Now reading the target file.' },
        { data: 'Final answer text' },
        STATS,
        DONE,
      ),
    }));

    await sendMsg(page);
    const ctx = page.locator(RESPONSE).locator('.user-ctx');
    await ctx.click();

    const traceRow = page.locator('#ctx-popup .ctx-popup-trace-row');
    await expect(traceRow).toBeVisible();
    await expect(traceRow).toContainText('trace');
    await expect(traceRow).toContainText('thoughts');

    await traceRow.click();
    await expect(page.locator('#msg-modal')).toHaveClass(/open/);
    await expect(page.locator('#msg-modal-title')).toContainText('thought trace');
    await expect(page.locator('#ctx-popup')).not.toHaveClass(/open/);

    // Narration and tool calls render interleaved in true chronological order,
    // not grouped into two separate blocks — and the final answer text (a
    // plain 'text' event) is excluded since it's already shown in the bubble.
    const children = page.locator('#msg-modal-body > *');
    await expect(children).toHaveCount(3);
    await expect(children.nth(0)).toHaveClass(/trace-status/);
    await expect(children.nth(0)).toContainText('Checking the repo first.');
    await expect(children.nth(1)).toHaveClass(/tool-block/);
    await expect(children.nth(1)).toContainText('Bash: ls -la');
    await expect(children.nth(2)).toHaveClass(/trace-status/);
    await expect(children.nth(2)).toContainText('Now reading the target file.');
    await expect(page.locator('#msg-modal-body')).not.toContainText('Final answer text');

    await children.nth(1).locator('.tool-toggle').click();
    await expect(children.nth(1).locator('.trace-tool-pre')).toContainText('"command": "ls -la"');
  });

  test('ctx popup has no thought trace link when nothing was recorded', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Just an answer, no tools used' }, STATS, DONE),
    }));

    await sendMsg(page);
    const ctx = page.locator(RESPONSE).locator('.user-ctx');
    await ctx.click();

    await expect(page.locator('#ctx-popup')).toBeVisible();
    await expect(page.locator('#ctx-popup .ctx-popup-trace-row')).not.toBeAttached();
  });

  test('content is markdown-rendered in final bubble', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: '**bold** and `code`' }, DONE),
    }));

    await sendMsg(page);
    const bubble = page.locator(RESPONSE);
    await expect(bubble.locator('strong')).toHaveText('bold');
    await expect(bubble.locator('code')).toHaveText('code');
    await look(page);  // pause — observe: bold and inline code rendered in bubble
  });

  test('local file links with line suffix route through /localfile', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('squid_token', 'test-token'));
    await page.goto('/');
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: '[app.js](/Users/haebin/Work/squid/ui/app.js:470)' }, DONE),
    }));

    await sendMsg(page);
    const href = await page.locator(`${RESPONSE} a`).getAttribute('href');
    expect(href).toContain('/localfile?path=');
    expect(decodeURIComponent(href)).toContain('/Users/haebin/Work/squid/ui/app.js');
    expect(decodeURIComponent(href)).not.toContain('app.js:470');
    expect(href).toContain('token=test-token');
  });

  test('Squid worktree links are not rewritten to local file links', async ({ page }) => {
    await page.goto('/');
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: '[app.py](/Users/haebin/.squid/worktrees/ef27c425/sqd-squid-2066-921e61/app.py:12)' }, DONE),
    }));

    await sendMsg(page);
    const href = await page.locator(`${RESPONSE} a`).getAttribute('href');
    expect(href).toBe('#');
    expect(href).not.toContain('/localfile?path=');
  });

  test('returning from another tab preserves scrolled-up chat position', async ({ page }) => {
    await page.evaluate(() => {
      const messages = document.getElementById('messages');
      messages.innerHTML = '';
      for (let i = 0; i < 20; i++) {
        const bubble = document.createElement('div');
        bubble.className = 'msg assistant';
        bubble.style.minHeight = '120px';
        bubble.textContent = `older response ${i} `.repeat(20);
        messages.appendChild(bubble);
      }
      messages.scrollTop = 120;
    });

    const before = await page.locator('#messages').evaluate(el => el.scrollTop);
    await setPageHidden(page, true);
    await setPageHidden(page, false);

    await expect.poll(() => page.locator('#messages').evaluate(el => el.scrollTop)).toBe(before);
  });

  test('mobile scroll-to-bottom button stays above composer controls', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await page.evaluate(() => {
      const messages = document.getElementById('messages');
      messages.innerHTML = '';
      for (let i = 0; i < 20; i++) {
        const bubble = document.createElement('div');
        bubble.className = 'msg assistant';
        bubble.style.minHeight = '120px';
        bubble.textContent = `older response ${i} `.repeat(20);
        messages.appendChild(bubble);
      }
      messages.scrollTop = 0;
      document.getElementById('topic-chip').classList.add('visible');
      document.getElementById('topic-chip').textContent = '#squid @claude';
      messages.dispatchEvent(new Event('scroll'));
    });

    await expect(page.locator('#scroll-btn')).toBeVisible();
    const boxes = await page.evaluate(() => {
      const rect = id => {
        const { top, bottom, left, right } = document.getElementById(id).getBoundingClientRect();
        return { top, bottom, left, right };
      };
      return {
        scroll: rect('scroll-btn'),
        inputArea: rect('input-area'),
        chipActions: rect('chip-actions'),
      };
    });

    expect(boxes.scroll.bottom).toBeLessThanOrEqual(boxes.inputArea.top - 8);
    expect(boxes.scroll.bottom).toBeLessThanOrEqual(boxes.chipActions.top - 8);
  });

  test('returning from another tab keeps following chat when already at bottom', async ({ page }) => {
    await page.evaluate(() => {
      const messages = document.getElementById('messages');
      messages.innerHTML = '';
      for (let i = 0; i < 20; i++) {
        const bubble = document.createElement('div');
        bubble.className = 'msg assistant';
        bubble.style.minHeight = '120px';
        bubble.textContent = `new response ${i} `.repeat(20);
        messages.appendChild(bubble);
      }
      messages.scrollTop = messages.scrollHeight;
    });

    await setPageHidden(page, true);
    await page.evaluate(() => {
      const messages = document.getElementById('messages');
      const bubble = document.createElement('div');
      bubble.className = 'msg assistant';
      bubble.style.minHeight = '120px';
      bubble.textContent = 'late update '.repeat(80);
      messages.appendChild(bubble);
    });
    await setPageHidden(page, false);

    await expect.poll(() => page.locator('#messages').evaluate(el => (
      el.scrollHeight - el.scrollTop - el.clientHeight
    ))).toBeLessThan(150);
  });

  test('renders Codex unified diff tool blocks', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: { name: 'Diff', file: 'ui/app.js', diff: '@@ -1 +1 @@\n-old\n+new' } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const block = page.locator('.tool-block-history').first();
    await expect(block.locator('.tool-toggle')).toContainText('Diff: ui/app.js');
    await block.locator('.tool-toggle').click();
    await expect(block.locator('.diff-hunk')).toContainText('@@ -1 +1 @@');
    await expect(block.locator('.diff-remove')).toContainText('-old');
    await expect(block.locator('.diff-add')).toContainText('+new');
  });

  test('renders GitDiff changed files before legacy edit tools', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: { name: 'Edit', file: 'ui/app.js', old: 'old', new: 'new' } },
        { event: 'tool', data: {
          name: 'GitDiff',
          file_count: 1,
          additions: 1,
          deletions: 1,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n-old\n+new',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const blocks = page.locator('.tool-block-history');
    await expect(blocks).toHaveCount(1);
    const block = blocks.first();
    await expect(block.locator('.tool-toggle')).toContainText('Changed files: 1 file, +1 -1');
    const fileToggle = block.locator('.gitdiff-file-toggle');
    await expect(fileToggle).toContainText('M ui/app.js');
    await expect(fileToggle).toBeVisible();
    const topToggleBox = await block.locator('.tool-toggle').boundingBox();
    const fileToggleBox = await fileToggle.boundingBox();
    expect(fileToggleBox.x - topToggleBox.x).toBeGreaterThanOrEqual(6);
    await fileToggle.click();
    const fileMetaBox = await block.locator('.gitdiff-file-body .diff-header-summary').boundingBox();
    expect(fileMetaBox.x - fileToggleBox.x).toBeGreaterThanOrEqual(6);
    await expect(block.locator('.diff-hunk')).toContainText('@@ -1 +1 @@');
  });

  test('suppresses legacy edit list when GitDiff reports no net changes', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: { name: 'Edit', file: 'ui/app.js', old: 'old', new: 'new' } },
        { event: 'tool', data: {
          name: 'GitDiff',
          file_count: 0,
          additions: 0,
          deletions: 0,
          files: [],
          diff: '',
          no_changes: true,
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    await expect(page.locator(RESPONSE)).toContainText('Done');
    await expect(page.locator('.tool-block-history')).toHaveCount(0);
  });

  test('dedupes repeated legacy edit tool records', async ({ page }) => {
    const writeTool = {
      name: 'Write',
      tool_use_id: 'toolu_duplicate',
      file: '/tmp/repo/line.txt',
      content: 'new line\n',
    };
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: writeTool },
        { event: 'tool', data: writeTool },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const blocks = page.locator('.tool-block-history');
    await expect(blocks).toHaveCount(1);
    await expect(blocks.first().locator('.tool-toggle')).toContainText('Write: /tmp/repo/line.txt');
  });

  test('dedupes repeated legacy edit tool records from history', async ({ page }) => {
    const writeTool = {
      name: 'Write',
      tool_use_id: 'toolu_duplicate_history',
      file: '/tmp/repo/line.txt',
      content: 'new line\n',
    };
    await page.unroute('**/history**');
    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 8484,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'done',
        prompt: 'fix conflict',
        content: 'Done',
        context: JSON.stringify([writeTool, writeTool]),
        timestamp: new Date().toISOString(),
        adhoc: false,
      }],
      has_more: false,
    }}));

    await page.reload();
    await expect(page.locator(RESPONSE)).toContainText('Done');
    const blocks = page.locator('.tool-block-history');
    await expect(blocks).toHaveCount(1);
    await expect(blocks.first().locator('.tool-toggle')).toContainText('Write: /tmp/repo/line.txt');
  });

  test('GitDiff renders text diffs for files with generic trailing suffixes', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'config/squid.yaml.example' }],
          diff: 'diff --git a/config/squid.yaml.example b/config/squid.yaml.example\n@@ -1 +1,2 @@\n old\n+new',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const block = page.locator('.tool-block-history').first();
    await block.locator('.tool-toggle').click();
    await expect(block.locator('.gitdiff-file-toggle')).not.toHaveClass(/gitdiff-file-toggle--no-diff/);
    await expect(block.locator('.gitdiff-binary-badge')).toHaveCount(0);
    await expect(block.locator('.diff-add')).toContainText('+new');
  });

  test('GitDiff mobile labels use shortest unique file names', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          file_count: 3,
          additions: 1,
          deletions: 1,
          files: [
            { status: 'M', path: 'ui/app.js' },
            { status: 'M', path: 'src/components/Button/index.ts' },
            { status: 'M', path: 'src/pages/Button/index.ts' },
          ],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n-old\n+new',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const rows = page.locator('.gitdiff-file-toggle');
    await expect(rows).toContainText([
      'M app.js',
      'M components/Button/index.ts',
      'M pages/Button/index.ts',
    ]);
    await expect(rows.nth(1)).toHaveAttribute('title', 'src/components/Button/index.ts');
  });

  test('GitDiff file-open control is visible and opens the file viewer', async ({ page }) => {
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable' },
    }));
    await page.route('**/localfile**', route => route.fulfill({
      status: 200, contentType: 'text/plain', body: 'const opened = true;',
    }));
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const openButton = page.getByRole('button', { name: 'Open ui/app.js in file viewer' });
    await expect(openButton).toBeVisible();
    await expect(openButton).toHaveText('view');
    const revertButton = page.getByRole('button', { name: 'revert' });
    await expect(revertButton).toBeVisible();
    await expect(page.locator('.gitdiff-file-row').locator('button')).toHaveText([
      'M ui/app.js  +1 -0',
      'revert',
      'view',
    ]);
    const [viewSize, revertSize] = await Promise.all([
      openButton.boundingBox(),
      revertButton.boundingBox(),
    ]);
    expect(viewSize.width).toBeCloseTo(revertSize.width, 0);
    expect(viewSize.height).toBeCloseTo(revertSize.height, 0);
    await openButton.click();

    await expect(page.locator('#file-modal-breadcrumb')).toContainText('tmp/repo/ui/app.js');
    await expect(page.locator('#file-modal-body')).toContainText('const opened = true;');
    await expect(page.locator('#file-modal-body .fv-changed')).toContainText('const opened = true;');
  });

  test('GitDiff file-open control is visible for mjs text diffs', async ({ page }) => {
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'scripts/build.mjs' }],
          diff: 'diff --git a/scripts/build.mjs b/scripts/build.mjs\n@@ -1 +1 @@\n+export const opened = true;',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const row = page.locator('.gitdiff-file-row');
    await expect(row.locator('.gitdiff-binary-badge')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Open scripts/build.mjs in file viewer' })).toBeVisible();
  });

  test('GitDiff uses streamed worktree sync status before showing actions', async ({ page }) => {
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable' },
    }));
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          worktree_repo: '/tmp/.squid/worktrees/topic/repo',
          worktree_status: 'pending',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
        } },
        { event: 'tool', data: {
          name: 'WorktreeSync',
          status: 'synced',
          repo: '/tmp/repo',
          worktree_repo: '/tmp/.squid/worktrees/topic/repo',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    await expect(page.locator('.tool-toggle')).toContainText('Changed files: 1 file, +1 -0');
    await expect(page.locator('.tool-toggle')).not.toContainText('pending');
    await expect(page.getByRole('button', { name: 'Open ui/app.js in file viewer' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'revert' })).toBeVisible();
  });

  test('GitDiff surfaces streamed worktree sync conflicts', async ({ page }) => {
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable' },
    }));
    let discardBody = null;
    await page.route('**/chat/1/worktree/retry', route => route.fulfill({
      status: 404,
      json: { error: 'worktree not found' },
    }));
    await page.route('**/chat/1/worktree/discard', route => {
      discardBody = route.request().postDataJSON();
      route.fulfill({ json: { ok: true } });
    });
    await page.route('**/localfile**', route => route.fulfill({
      status: 200,
      contentType: 'text/plain',
      body: 'line 1\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> turn\nline 7\n<<<<<<< HEAD\nours 2\n=======\ntheirs 2\n>>>>>>> turn\n',
    }));
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          worktree_repo: '/tmp/.squid/worktrees/topic/repo',
          worktree_status: 'pending',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
        } },
        { event: 'tool', data: {
          name: 'WorktreeSync',
          status: 'conflict',
          repo: '/tmp/repo',
          worktree_repo: '/tmp/.squid/worktrees/topic/repo',
          integration_worktree_path: '/tmp/.squid/worktrees/topic/repo-integration',
          conflicts: ['ui/app.js'],
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const block = page.locator('.tool-block-history').first();
    await expect(block.locator('.tool-toggle')).toContainText('Changed files: 1 file, +1 -0 · conflict');
    await expect(block.locator('.gitdiff-sync-notice')).toContainText('Worktree sync conflict: ui/app.js');
    await expect(block.locator('.gitdiff-sync-path')).toContainText('/tmp/.squid/worktrees/topic/repo-integration');
    await expect(page.getByRole('button', { name: 'Open ui/app.js in file viewer' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'revert' })).toHaveCount(0);
    await block.getByRole('button', { name: 'Conflicts' }).click();
    await expect(page.locator('#file-modal-breadcrumb')).toContainText('/tmp/.squid/worktrees/topic/repo-integration/ui/app.js');
    await expect(page.locator('.fv-edit-find')).toHaveValue('<<<<<<<');
    await expect(page.locator('#file-modal-body .fv-target')).toContainText('<<<<<<< HEAD');
    await expect(page.locator('.fv-edit-line')).toHaveValue('2');
    await page.locator('.fv-edit-tool-btn[aria-label="Next match"]').click();
    await expect(page.locator('#file-modal-body .fv-target')).toContainText('<<<<<<< HEAD');
    await expect(page.locator('.fv-edit-line')).toHaveValue('8');
    await page.locator('#file-modal-close').click();
    await expect(block.getByRole('button', { name: 'Auto-Resolve' })).toHaveAttribute('title', /Ask the model to merge both sides directly in the integration worktree/);
    await block.getByRole('button', { name: 'Resolve', exact: true }).click();
    await expect(block.locator('.gitdiff-sync-notice')).toContainText('Resolve failed: worktree not found');
    await expect(block.getByRole('button', { name: 'Discard Turn' })).toHaveAttribute('title', /already-applied main checkout changes are not reverted/);
    await block.getByRole('button', { name: 'Discard Turn' }).click();
    await expect(block.locator('.gitdiff-sync-notice')).toContainText('Worktree discarded');
    expect(discardBody).toEqual({ topic: 'default', repo: '/tmp/repo' });
  });

  test('blocked worktree response renders controls for original turn', async ({ page }) => {
    let retryUrl = null;
    let retryBody = null;
    await page.route('**/chat/*/worktree/retry', route => {
      retryUrl = route.request().url();
      retryBody = route.request().postDataJSON();
      route.fulfill({ json: { ok: true } });
    });
    await page.route('**/localfile**', route => route.fulfill({
      status: 200,
      contentType: 'text/plain',
      body: 'line 1\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> turn\n',
    }));
    await page.route('**/chat', route => route.fulfill({
      status: 409,
      json: {
        error: 'worktree sync requires attention before starting another turn',
        worktrees: [{
          repo_root: '/tmp/repo',
          worktree_path: '/tmp/.squid/worktrees/topic/repo',
          integration_worktree_path: '/tmp/.squid/worktrees/topic/repo-integration',
          status: 'conflict',
          msg_id: '7282',
          conflicts: ['ui/app.js'],
        }],
      },
    }));

    await sendMsg(page);
    await expect(page.locator(MSG_ERROR)).toContainText('worktree sync requires attention');
    const block = page.locator('.tool-block-history').first();
    await expect(block.locator('.gitdiff-sync-notice')).toContainText('Worktree sync conflict: ui/app.js');
    await expect(block.getByRole('button', { name: 'Conflicts' })).toBeVisible();
    await expect(block.getByRole('button', { name: 'Auto-Resolve' })).toBeVisible();
    await expect(block.getByRole('button', { name: 'Discard Turn' })).toHaveAttribute(
      'title',
      /later blocked message points at turn #7282.*Discard only this isolated turn's pending worktree changes/,
    );
    await block.evaluate(el => el.after(el.cloneNode(true)));
    await expect(page.locator('.tool-block-history .gitdiff-sync-notice')).toHaveCount(2);
    await block.getByRole('button', { name: 'Resolve', exact: true }).click();
    await expect(page.locator('.tool-block-history .gitdiff-sync-notice')).toHaveText([
      /Worktree resolved and synced/,
      /Worktree resolved and synced/,
    ]);
    await expect(page.locator('.tool-block-history').nth(0).locator('.gitdiff-resolved-label')).toHaveText('Resolved');
    await expect(page.locator('.tool-block-history').nth(1).locator('.gitdiff-resolved-label')).toHaveText('Resolved');
    expect(retryUrl).toContain('/chat/7282/worktree/retry');
    expect(retryBody).toEqual({ topic: 'default', repo: '/tmp/repo', force: false });
  });

  test('active worktree blocker offers retry sync and discard controls', async ({ page }) => {
    let retryBody = null;
    await page.route('**/chat/*/worktree/retry', route => {
      retryBody = route.request().postDataJSON();
      route.fulfill({ json: { ok: true } });
    });
    await page.route('**/chat', route => route.fulfill({
      status: 409,
      json: {
        error: 'worktree sync requires attention before starting another turn',
        worktrees: [{
          repo_root: '/tmp/repo',
          worktree_path: '/tmp/.squid/worktrees/topic/repo',
          status: 'active',
          msg_id: '7283',
        }],
      },
    }));

    await sendMsg(page);
    const block = page.locator('.tool-block-history').first();
    await expect(block.locator('.gitdiff-sync-notice')).toContainText('Worktree sync active');
    await expect(block.getByRole('button', { name: 'Retry Sync' })).toBeVisible();
    await expect(block.getByRole('button', { name: 'Discard Turn' })).toBeVisible();
    await expect(block.getByRole('button', { name: 'Resolve', exact: true })).toHaveCount(0);
    await expect(block.getByRole('button', { name: 'Conflicts' })).toHaveCount(0);
    await expect(block.getByRole('button', { name: 'Open worktree changes in file viewer' })).toHaveCount(0);
    await expect(block.getByRole('button', { name: 'revert' })).toHaveCount(0);

    await block.getByRole('button', { name: 'Retry Sync' }).click();
    await expect(block.locator('.gitdiff-sync-notice')).toContainText('Worktree synced');
    expect(retryBody).toEqual({ topic: 'default', repo: '/tmp/repo', force: false });
  });

  test('mobile browser back closes GitDiff file viewer back to diff list', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable' },
    }));
    await page.route('**/localfile**', route => route.fulfill({
      status: 200, contentType: 'text/plain', body: 'const opened = true;',
    }));
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    await page.getByRole('button', { name: 'Open ui/app.js in file viewer' }).click();
    await expect(page.locator('#file-modal-box')).toBeVisible();
    await expect(page.locator('#file-modal-body')).toContainText('const opened = true;');

    await page.evaluate(() => history.back());
    await expect(page.locator('#file-modal-box')).toHaveCount(0);
    await expect(page.locator('.gitdiff-file-row')).toContainText('M app.js');
    await expect(page.locator('#view-chat')).toHaveClass(/active/);
  });

  test('GitDiff file-open uses source repo instead of worktree repo', async ({ page }) => {
    const openedPaths = [];
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable' },
    }));
    await page.route('**/localfile**', route => {
      const url = new URL(route.request().url());
      openedPaths.push(url.searchParams.get('path'));
      route.fulfill({ status: 200, contentType: 'text/plain', body: 'const opened = true;' });
    });
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/Users/haebin/.squid/worktrees/ef27c425/sqd-squid-2066-921e61',
          source: '/Users/haebin/Work/squid',
          worktree_repo: '/Users/haebin/.squid/worktrees/ef27c425/sqd-squid-2066-921e61',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    await page.getByRole('button', { name: 'Open ui/app.js in file viewer' }).click();

    await expect(page.locator('#file-modal-breadcrumb')).toContainText('Work/squid/ui/app.js');
    expect(openedPaths[0]).toBe('/Users/haebin/Work/squid/ui/app.js');
    expect(openedPaths[0]).not.toContain('/.squid/worktrees/');
  });

  test('revert eligibility is refreshed for new GitDiff blocks and after a revert', async ({ page }) => {
    const statusRequests = [];
    await page.route('**/chat/*/diff-revert-status**', route => {
      const msgId = route.request().url().match(/\/chat\/(\d+)\//)[1];
      statusRequests.push(msgId);
      route.fulfill({ json: { 'ui/app.js': 'revertable' } });
    });
    await page.route('**/chat/1/revert', route => route.fulfill({ json: { ok: true, reverted: ['ui/app.js'] } }));

    const gitDiffTool = {
      name: 'GitDiff', repo: '/tmp/repo', file_count: 1, additions: 1, deletions: 0,
      files: [{ status: 'M', path: 'ui/app.js' }],
      diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
    };

    // First message completes with a GitDiff tool block (msg_id 1).
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { event: 'tool', data: gitDiffTool }, { data: 'Done' }, DONE),
    }), { times: 1 });
    await sendMsg(page, 'first');
    await expect(page.locator('.tool-block-history')).toHaveCount(1);
    await expect.poll(() => statusRequests).toEqual(['1']);

    // Second message completes with its own GitDiff tool block (msg_id 2).
    // Rendering it can retroactively change older blocks' eligibility, so the
    // existing block is rechecked along with the new one.
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        { event: 'meta', data: { agent: 'claude', backend: 'claude', msg_id: 2, adhoc: false } },
        { event: 'tool', data: gitDiffTool },
        { data: 'Done' },
        DONE,
      ),
    }));
    await sendMsg(page, 'second');
    await expect(page.locator('.tool-block-history')).toHaveCount(2);
    await expect.poll(() => statusRequests).toEqual(['1', '1', '2']);

    // Reverting changes the working tree, so eligibility for every block -
    // including the already-checked one - needs a fresh check.
    await page.locator('.tool-block-history').first().getByRole('button', { name: 'revert' }).click();
    await expect(page.locator('#restart-modal')).toHaveClass(/open/);
    await expect(page.locator('#restart-modal-title')).toHaveText('Revert ui/app.js?');
    await page.locator('#restart-modal-confirm').click();
    await expect.poll(() => statusRequests).toEqual(['1', '1', '2', '1', '2']);
  });

  test('single-file revert can be cancelled before request', async ({ page }) => {
    let revertRequests = 0;
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable' },
    }));
    await page.route('**/chat/1/revert', route => {
      revertRequests++;
      route.fulfill({ json: { ok: true, reverted: ['ui/app.js'] } });
    });
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    await page.getByRole('button', { name: 'revert' }).click();
    await expect(page.locator('#restart-modal-title')).toHaveText('Revert ui/app.js?');
    await page.locator('#restart-modal-cancel').click();
    await expect(page.locator('#restart-modal')).not.toHaveClass(/open/);
    expect(revertRequests).toBe(0);
  });

  test('revert all does not show success when no files reverted', async ({ page }) => {
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable', 'ui/style.css': 'revertable' },
    }));
    await page.route('**/chat/1/revert', route => route.fulfill({
      json: {
        ok: true,
        reverted: [],
        failed: [{ file: 'ui/app.js', error: 'patch does not apply' }],
      },
    }));
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          file_count: 2,
          additions: 2,
          deletions: 0,
          files: [
            { status: 'M', path: 'ui/app.js' },
            { status: 'M', path: 'ui/style.css' },
          ],
          diff: [
            'diff --git a/ui/app.js b/ui/app.js',
            '@@ -1 +1 @@',
            '+const opened = true;',
            'diff --git a/ui/style.css b/ui/style.css',
            '@@ -1 +1 @@',
            '+body { color: red; }',
          ].join('\n'),
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const revertAll = page.getByRole('button', { name: 'Revert all 2 files' });
    await revertAll.click();
    await expect(page.locator('#restart-modal-title')).toHaveText('Revert 2 files?');
    await page.locator('#restart-modal-confirm').click();

    await expect(revertAll).toBeEnabled();
    await expect(revertAll).toHaveText('Revert all 2 files');
    await expect(revertAll).toHaveAttribute('title', 'patch does not apply');
  });

  test('recovered completion restores GitDiff and renders one end timestamp', async ({ page }) => {
    const gitDiff = {
      name: 'GitDiff',
      file_count: 1,
      additions: 1,
      deletions: 1,
      files: [{ status: 'M', path: 'ui/app.js' }],
      diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n-old\n+new',
    };
    await page.route('**/chat/*/status', r => r.fulfill({ json: {
      status: 'done',
      content: 'Recovered response',
      context: JSON.stringify([gitDiff]),
    } }));
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(META, { data: 'Partial response' }),
    }));

    await sendMsg(page);
    await expect(page.locator('.tool-block-history')).toHaveCount(1, { timeout: 5_000 });
    await expect(page.locator(RESPONSE)).toContainText('Recovered response');
    await expect(page.locator('.msg-time')).toHaveCount(2); // user start + assistant completion
    await page.waitForTimeout(2_200);
    await expect(page.locator('.msg-time')).toHaveCount(2);
  });

  test('live recovery ignores empty interrupted error until final completion', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: { ...SSE_HEADERS, 'X-Squid-Msg-Id': '90' },
      body: '',
    }));

    let statusCalls = 0;
    await page.route('**/chat/90/status', r => {
      statusCalls++;
      if (statusCalls === 1) {
        return r.fulfill({ json: { id: 90, status: 'pending', content: 'Partial response' } });
      }
      if (statusCalls === 2) {
        return r.fulfill({ json: { id: 90, status: 'error', content: '' } });
      }
      return r.fulfill({ json: {
        id: 90,
        topic: 'default',
        agent: 'claude',
        backend: 'claude',
        status: 'done',
        content: 'Recovered final response',
        adhoc: false,
        timestamp: new Date().toISOString(),
      }});
    });

    await sendMsg(page);

    await expect(page.locator(RESPONSE).filter({ hasText: 'Recovered final response' })).toBeVisible({ timeout: 8_000 });
    await expect(page.locator(MSG_ERROR).filter({ hasText: 'Response interrupted.' })).not.toBeAttached();
    expect(statusCalls).toBeGreaterThanOrEqual(3);
  });

  test('stream error after partial content keeps partial in thinking bubble only', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(META, { data: 'Partial answer' }, { event: 'error', data: 'Connection lost' }),
    }));

    await sendMsg(page);

    const statusBubble = page.locator('.msg-thinking-done');
    await expect(statusBubble).toContainText('Partial answer');
    await expect(statusBubble).toContainText('Connection lost');
    await expect(page.locator(RESPONSE)).not.toBeAttached();
    await expect(page.locator(MSG_ERROR)).not.toBeAttached();
  });

  test('stream error with message id keeps polling until final completion', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: { ...SSE_HEADERS, 'X-Squid-Msg-Id': '92' },
      body: sse(
        { event: 'meta', data: { agent: 'claude', backend: 'claude', msg_id: 92, adhoc: false } },
        { event: 'status', data: 'Wrapping up...' },
        { event: 'error', data: 'Connection lost' },
      ),
    }));

    let statusCalls = 0;
    await page.route('**/chat/92/status', r => {
      statusCalls++;
      if (statusCalls === 1) {
        return r.fulfill({ json: { id: 92, status: 'pending', content: '' } });
      }
      if (statusCalls === 2) {
        return r.fulfill({ json: { id: 92, status: 'error', content: '' } });
      }
      return r.fulfill({ json: {
        id: 92,
        topic: 'default',
        agent: 'claude',
        backend: 'claude',
        status: 'done',
        content: 'Recovered final response',
        adhoc: false,
        timestamp: new Date().toISOString(),
      }});
    });

    await sendMsg(page);

    await expect(page.locator(RESPONSE).filter({ hasText: 'Recovered final response' })).toBeVisible({ timeout: 8_000 });
    await expect(page.locator(MSG_ERROR).filter({ hasText: 'Connection lost' })).not.toBeAttached();
    expect(statusCalls).toBeGreaterThanOrEqual(3);
  });

  test('polling error after partial content keeps partial in thinking bubble only', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: { ...SSE_HEADERS, 'X-Squid-Msg-Id': '91' },
      body: '',
    }));

    let statusCalls = 0;
    await page.route('**/chat/91/status', r => {
      statusCalls++;
      if (statusCalls === 1) {
        return r.fulfill({ json: { id: 91, status: 'pending', content: 'Partial response' } });
      }
      return r.fulfill({ json: { id: 91, status: 'error', content: 'Partial response' } });
    });

    await sendMsg(page);

    const statusBubble = page.locator('.msg-thinking-done');
    await expect(statusBubble).toContainText('Partial response', { timeout: 5_000 });
    await expect(statusBubble).toContainText('Connection interrupted.');
    await expect(page.locator(RESPONSE)).not.toBeAttached();
    await expect(page.locator(MSG_ERROR)).not.toBeAttached();
  });

  test('status events are hidden after final response completes', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { event: 'status', data: 'Thinking...' }, { data: 'Result' }, DONE),
    }));

    await sendMsg(page);
    await expect(page.locator(RESPONSE)).toBeVisible();
    await expect(page.locator(RESPONSE)).toContainText('Result');
    await expect(page.locator(RESPONSE)).not.toContainText('Thinking...');
    await expect(page.locator(THINKING)).not.toBeAttached();
    await expect(page.locator('.msg-thinking-done')).not.toBeAttached();
    await look(page);  // pause — observe: only final response bubble
  });

  test('status streaming preserves newlines and adjacent delta chunks', async ({ page }) => {
    const body = sse(META)
      + 'event: status\ndata: first line\ndata: sec\n\n'
      + 'event: status\ndata: ond line\n\n'
      + sse({ data: 'Final response' }, DONE);
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS, body,
    }));

    await sendMsg(page);

    await expect(page.locator(THINKING)).not.toBeAttached();
    await expect(page.locator(RESPONSE)).toContainText('Final response');
    await expect(page.locator(RESPONSE)).not.toContainText('first line');
  });

  test('partial status remains in status bubble when the response errors', async ({ page }) => {
    const statusEvents = Array.from({ length: 40 }, (_, i) => ({
      event: 'status',
      data: `Checking the code ${i + 1}...`,
    }));
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        ...statusEvents,
        { event: 'error', data: 'Backend unavailable' },
      ),
    }));

    await sendMsg(page);

    const statusBubble = page.locator('.msg-thinking-done');
    await expect(statusBubble.locator('.thinking-body')).toContainText('Checking the code 1...');
    await expect(statusBubble.getByRole('button', { name: 'Double thinking height' })).not.toBeVisible();
    await statusBubble.locator('.thinking-toggle').click();
    await expect(statusBubble.getByRole('button', { name: 'Double thinking height' })).toBeVisible();
    const normalMax = await statusBubble.locator('.thinking-body').evaluate(el => parseFloat(getComputedStyle(el).maxHeight));
    await statusBubble.getByRole('button', { name: 'Double thinking height' }).click();
    await expect(statusBubble).toHaveClass(/thinking-tall/);
    const tallMax = await statusBubble.locator('.thinking-body').evaluate(el => parseFloat(getComputedStyle(el).maxHeight));
    expect(tallMax).toBeGreaterThan(normalMax * 1.8);
    const response = page.locator(RESPONSE);
    await expect(response.locator(MSG_ERROR)).toHaveText('Backend unavailable');
    await expect(response).not.toContainText('Checking the code...');
  });

  test('agent-prefixed status appears in the thought bubble', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'status', data: '[Agent: scan panels] Searching file viewer code...' },
        { event: 'error', data: 'Backend unavailable' },
      ),
    }));

    await sendMsg(page);

    const statusBubble = page.locator('.msg-thinking-done');
    await expect(statusBubble.locator('.thinking-body')).toContainText('[Agent: scan panels] Searching file viewer code...');
    const response = page.locator(RESPONSE);
    await expect(response.locator(MSG_ERROR)).toHaveText('Backend unavailable');
    await expect(response).not.toContainText('Searching file viewer code...');
  });

  test('terminated response error removes the status bubble', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'status', data: 'Let me find the file browser header.' },
        { event: 'error', data: 'CLI exited 143: ' },
      ),
    }));

    await sendMsg(page);

    await expect(page.locator(THINKING)).not.toBeAttached();
    await expect(page.locator(RESPONSE)).toHaveCount(1);
    await expect(page.locator(RESPONSE).locator(MSG_ERROR)).toHaveText('Response interrupted.');
  });

  test('interrupted stream before meta keeps a recovering status bubble', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: { ...SSE_HEADERS, 'X-Squid-Msg-Id': '88' },
      body: '',
    }));
    await page.route('**/chat/88/status', r => r.fulfill({ json: {
      id: 88,
      status: 'pending',
      content: '',
    }}));

    await sendMsg(page);

    const statusBubble = page.locator(THINKING);
    await expect(statusBubble).toBeVisible();
    await expect(statusBubble).toContainText('Connection interrupted');
    await expect(page.locator(RESPONSE)).not.toBeAttached();
  });

  test('interrupted stream without headers recovers message id from process tracker', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: '',
    }));
    await page.route('**/processes', r => r.fulfill({ json: [
      { topic: 'default', agent: 'claude', adhoc: true, msg_id: 89, state: 'running' },
    ] }));
    await page.route('**/chat/89/status', r => r.fulfill({ json: {
      id: 89,
      status: 'pending',
      content: '',
    }}));

    await sendMsg(page);

    const statusBubble = page.locator(THINKING);
    await expect(statusBubble).toBeVisible();
    await expect(statusBubble).toContainText('Connection interrupted');
    await expect(statusBubble).toHaveAttribute('data-msg-id', '89');
  });

  test('thinking bubble removed when no status events', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Hello' }, DONE),
    }));

    await sendMsg(page);
    await expect(page.locator(RESPONSE)).toBeVisible();
    await expect(page.locator(THINKING)).not.toBeAttached();
    await look(page);  // pause — observe: only response bubble, thinking bubble gone
  });

  test('error appears at bottom in bubble', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { event: 'error', data: 'Backend unavailable' }),
    }));

    await sendMsg(page);
    const errorBubble = page.locator(RESPONSE);
    await expect(errorBubble).toBeVisible();
    await expect(errorBubble.locator(MSG_ERROR)).toContainText('Backend unavailable');
    const last = page.locator('#messages > *').last();
    await expect(last).toHaveClass(/assistant/);
    await look(page);  // pause — observe: error message in bubble at bottom
  });
});

test.describe('parallel responses', () => {
  test.beforeEach(async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
  });

  test('two concurrent responses both land at bottom without early bubble insertion', async ({ page }) => {
    const routes = [];
    const bothIntercepted = new Promise(resolve => {
      page.route('**/chat', route => {
        routes.push(route);
        if (routes.length === 2) resolve();
      });
    });

    await sendMsg(page, '#a hello');
    await sendMsg(page, '#b world');
    await bothIntercepted;

    // ── LOOK: both requests in flight, no response bubbles yet ───────────────
    await expect(page.locator(RESPONSE)).not.toBeAttached();
    await look(page);  // pause — observe: two thinking bubbles, zero response bubbles

    await routes[1].fulfill({ status: 200, headers: SSE_HEADERS, body: sse(META, { data: 'Second done first' }, DONE) });
    await expect(page.locator(RESPONSE)).toHaveCount(1);
    await look(page);  // pause — observe: one bubble at bottom, other still thinking

    await routes[0].fulfill({ status: 200, headers: SSE_HEADERS, body: sse(META, { data: 'First done second' }, DONE) });
    await expect(page.locator(RESPONSE)).toHaveCount(2);
    await look(page);  // pause — observe: both bubbles at bottom in completion order
  });
});

test.describe('recovered pending responses', () => {
  test('poll fallback keeps retrying after transient status failure', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(window, 'EventSource', { configurable: true, value: undefined });
      const realSetInterval = window.setInterval.bind(window);
      window.setInterval = (callback, delay, ...args) => realSetInterval(callback, delay === 2000 ? 20 : delay, ...args);
    });
    await mockBackend(page);

    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 79,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'long-running task',
        content: 'History partial',
        adhoc: false,
      }],
      has_more: false,
    }}));
    let statusCalls = 0;
    await page.route('**/chat/79/status', r => {
      statusCalls++;
      if (statusCalls === 1) return r.fulfill({ status: 500, body: '' });
      if (statusCalls === 2) return r.fulfill({ json: { id: 79, status: 'pending', content: 'Still working' } });
      return r.fulfill({ json: {
        id: 79,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'done',
        prompt: 'long-running task',
        content: 'Recovered after transient failure',
        adhoc: false,
        timestamp: new Date().toISOString(),
      }});
    });

    await page.goto('/');

    await expect(page.locator(RESPONSE).filter({ hasText: 'Recovered after transient failure' })).toBeVisible({ timeout: 5_000 });
    expect(statusCalls).toBeGreaterThanOrEqual(3);
    await expect(page.locator(THINKING)).not.toBeAttached();
  });

  test('pageshow reconnects stale pending event watcher', async ({ page }) => {
    await page.addInitScript(() => {
      window.__eventSources = [];
      window.EventSource = class {
        constructor(url) {
          this.url = url;
          this.closed = false;
          this.listeners = {};
          window.__eventSources.push(this);
        }
        addEventListener(type, callback) {
          this.listeners[type] = callback;
        }
        close() {
          this.closed = true;
        }
      };
    });
    await mockBackend(page);

    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 80,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'long-running task',
        content: 'History partial',
        adhoc: false,
      }],
      has_more: false,
    }}));

    await page.goto('/');
    await expect(page.locator(`${THINKING}[data-msg-id="80"]`)).toBeVisible();
    await page.waitForFunction(() => window.__eventSources.length === 1);

    await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));

    await page.waitForFunction(() => window.__eventSources.length === 2);
    expect(await page.evaluate(() => ({
      firstClosed: window.__eventSources[0].closed,
      secondUrl: window.__eventSources[1].url,
    }))).toEqual({
      firstClosed: true,
      secondUrl: '/chat/80/events',
    });
  });

  test('refresh recovery streams pending status from event replay', async ({ page }) => {
    await mockBackend(page);

    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 77,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'long-running task',
        content: 'History partial',
        adhoc: false,
      }],
      has_more: false,
    }}));
    let eventsRequested = false;
    await page.route('**/chat/77/events', r => {
      eventsRequested = true;
      return r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(
        { event: 'status', data: 'Still connected after refresh' },
        { data: 'Recovered stream text' },
        DONE,
      ),
    });
    });
    await page.route('**/chat/77/status', r => r.fulfill({ json: {
      id: 77,
      topic: 'squid',
      agent: 'claude',
      backend: 'claude',
      status: 'done',
      prompt: 'long-running task',
      content: 'Recovered final response',
      adhoc: false,
      timestamp: new Date().toISOString(),
    }}));

    await page.goto('/');

    await expect(page.locator(RESPONSE).filter({ hasText: 'Recovered final response' })).toBeVisible();
    expect(eventsRequested).toBe(true);
    await expect(page.locator(THINKING)).not.toBeAttached();
  });

  test('refresh polling does not finalize an empty interrupted error', async ({ page }) => {
    await mockBackend(page);

    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 78,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'long-running task',
        content: 'History partial',
        adhoc: false,
      }],
      has_more: false,
    }}));
    await page.route('**/chat/78/events', r => r.fulfill({ status: 500, body: '' }));
    let statusCalls = 0;
    await page.route('**/chat/78/status', r => {
      statusCalls++;
      if (statusCalls === 1) {
        return r.fulfill({ json: { id: 78, status: 'pending', content: 'History partial' } });
      }
      if (statusCalls === 2) {
        return r.fulfill({ json: { id: 78, status: 'error', content: '' } });
      }
      return r.fulfill({ json: {
        id: 78,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'done',
        prompt: 'long-running task',
        content: 'Recovered final response',
        adhoc: false,
        timestamp: new Date().toISOString(),
      }});
    });

    await page.goto('/');

    await expect(page.locator(RESPONSE).filter({ hasText: 'Recovered final response' })).toBeVisible({ timeout: 8_000 });
    await expect(page.locator(MSG_ERROR).filter({ hasText: 'Response interrupted.' })).not.toBeAttached();
    await expect(page.locator(THINKING)).not.toBeAttached();
  });

  test('search back keeps one status bubble when live meta arrives after history', async ({ page }) => {
    await mockBackend(page);

    let exposePending = false;
    await page.route('**/history**', r => r.fulfill({ json: {
      items: exposePending ? [{
        id: 1,
        topic: 'default',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'long-running task',
        content: 'Working from history...',
        adhoc: false,
      }] : [],
      has_more: false,
    }}));
    await page.route('**/search**', r => r.fulfill({ json: { items: [] } }));

    const { intercepted, fulfill } = holdChat(page);
    await page.goto('/');
    await sendMsg(page, 'long-running task');
    await intercepted;

    // The pending row becomes visible to history before the held SSE sends meta.
    exposePending = true;
    await page.evaluate(() => startSearch('needle'));
    await page.evaluate(() => clearSearch());
    await expect(page.locator(`${THINKING}[data-msg-id="1"]`)).toHaveCount(1);
    await expect(page.locator(THINKING)).toHaveCount(2); // live (unidentified) + recovered WIP
    const recovered = page.locator(`${THINKING}[data-msg-id="1"]`);
    await expect(recovered.locator('.response-header')).toBeVisible();
    await expect(recovered.locator('.response-header-text')).toContainText('long-running task');
    await expect(recovered.locator('.history-prompt')).toBeVisible();
    await recovered.locator('.history-prompt').click();
    await expect(recovered.locator('.history-prompt-full.visible')).toHaveText('long-running task');

    await fulfill(sse(META, { event: 'status', data: 'Still working...' }));

    // meta.msg_id reconciles the recovered WIP into the live SSE bubble.
    await expect(page.locator(THINKING)).toHaveCount(1);
    await expect(page.locator(`${THINKING}[data-msg-id="1"]`)).toHaveCount(1);
    await expect(page.locator(THINKING)).toContainText('Still working...');
  });

  test('completed response moves to bottom instead of replacing its status bubble', async ({ page }) => {
    await mockBackend(page);

    let recovered = false;
    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 41,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'long-running task',
        content: 'Working...',
        adhoc: false,
      }],
      has_more: false,
    }}));
    await page.route('**/chat/41/status', r => r.fulfill({ json: recovered ? {
      id: 41,
      topic: 'squid',
      agent: 'claude',
      backend: 'claude',
      status: 'done',
      prompt: 'long-running task',
      content: 'Recovered final response',
      adhoc: false,
      timestamp: new Date().toISOString(),
    } : {
      id: 41,
      status: 'pending',
      content: 'Working...',
    }}));
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(META, { data: 'Newer response' }, DONE),
    }));

    await page.goto('/');
    await expect(page.locator(THINKING)).toContainText('Working...');

    await sendMsg(page, 'new request');
    await expect(page.locator(RESPONSE).filter({ hasText: 'Newer response' })).toBeVisible();

    recovered = true;
    const recoveredBubble = page.locator(`${RESPONSE}[data-msg-id="41"]`);
    await expect(recoveredBubble).toContainText('Recovered final response', { timeout: 5_000 });
    await expect(page.locator(THINKING)).not.toBeAttached();
    await expect(page.locator('#messages > .msg.assistant').last()).toContainText('Recovered final response');
  });
});
