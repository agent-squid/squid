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
    await block.locator('.tool-toggle').click();
    await expect(block.locator('.gitdiff-file-toggle')).toContainText('M ui/app.js');
    await expect(block.locator('.diff-hunk')).toContainText('@@ -1 +1 @@');
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
    const [viewSize, revertSize] = await Promise.all([
      openButton.boundingBox(),
      revertButton.boundingBox(),
    ]);
    expect(viewSize.width).toBeCloseTo(revertSize.width, 0);
    expect(viewSize.height).toBeCloseTo(revertSize.height, 0);
    await openButton.click();

    await expect(page.locator('#file-modal-breadcrumb')).toContainText('tmp/repo/ui/app.js');
    await expect(page.locator('#file-modal-body')).toContainText('const opened = true;');
    await expect(page.locator('#file-modal-body .fv-target')).toContainText('const opened = true;');
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
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'status', data: 'Checking the code...' },
        { event: 'error', data: 'Backend unavailable' },
      ),
    }));

    await sendMsg(page);

    const statusBubble = page.locator('.msg-thinking-done');
    await expect(statusBubble.locator('.thinking-body')).toContainText('Checking the code...');
    const response = page.locator(RESPONSE);
    await expect(response.locator(MSG_ERROR)).toHaveText('Backend unavailable');
    await expect(response).not.toContainText('Checking the code...');
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
