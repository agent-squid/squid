const { test, expect } = require('@playwright/test');

const SSE_HEADERS = {
  'Content-Type': 'text/event-stream',
  'Cache-Control': 'no-cache',
  'X-Accel-Buffering': 'no',
};

async function mockBackend(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic: 'default', exists: false, content: '', path: '',
    squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
  }}));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
}

function runningProcess() {
  return [{ msg_id: 42, topic: 'squid', agent: 'claude', prompt_preview: 'working', duration_s: 1 }];
}

test('tracker discovers a process after refresh and clears when it finishes', async ({ page }) => {
  await mockBackend(page);
  let running = true;
  await page.route('**/processes', r => r.fulfill({ json: running ? runningProcess() : [] }));

  await page.goto('/');
  const tracker = page.locator('#proc-status');
  await expect(tracker).toHaveClass(/has-procs/);

  running = false;
  await expect(tracker).not.toHaveClass(/has-procs/, { timeout: 5_000 });
});

test('opening the tracker restarts polling until an external process finishes', async ({ page }) => {
  await mockBackend(page);
  let running = false;
  await page.route('**/processes', r => r.fulfill({ json: running ? runningProcess() : [] }));

  await page.goto('/');
  const tracker = page.locator('#proc-status');
  await expect(tracker).not.toHaveClass(/has-procs/);

  running = true;
  await tracker.click();
  await expect(tracker).toHaveClass(/has-procs/);

  running = false;
  await expect(tracker).not.toHaveClass(/has-procs/, { timeout: 5_000 });
});

test('status popup lists quota gauges only for available backends', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/health', r => r.fulfill({ json: {
    status: 'ok',
    backends: {
      claude: { label: 'Claude', available: true, gauge: { type: 'claude' } },
      codex: { label: 'Codex', available: true, gauge: { type: 'codex' } },
      deepseek: { label: 'DeepSeek', available: false, gauge: { type: 'deepseek' } },
      qwen: { label: 'Qwen', available: false, gauge: { type: 'static' } },
      local: { label: 'Local', available: true, gauge: { type: 'static' } },
      bare: { label: 'Bare', available: true, gauge: { type: 'none' } },
    },
  }}));
  await page.route('**/config/agents', r => r.fulfill({ json: [
    { name: 'qwen-agent', backend: 'qwen' },
  ] }));
  await page.route('**/quota/backend/*', r => {
    const backend = r.request().url().split('/').pop();
    if (backend === 'local' || backend === 'qwen') {
      return r.fulfill({ json: { status: 'ok', text: 'Local', raw: null, used_percent: null } });
    }
    return r.fulfill({ json: { status: 'ok', raw: 42, used_percent: 42 } });
  });
  await page.route('**/processes', r => r.fulfill({ json: [] }));

  await page.goto('/');
  await page.locator('#proc-status').click();

  const rows = page.locator('#proc-status-popup .quota-status-row');
  await expect(rows).toHaveCount(3);
  await expect(rows).toContainText(['Claude', 'Codex', 'Local']);
  await expect(rows.filter({ hasText: 'Claude' })).toContainText('42%');
  await expect(rows.filter({ hasText: 'Codex' })).toContainText('42%');
  await expect(rows.filter({ hasText: 'DeepSeek' })).toHaveCount(0);
  await expect(rows.filter({ hasText: 'Qwen' })).toHaveCount(0);
  const localRow = rows.filter({
    has: page.locator('.quota-status-name').filter({ hasText: /^Local$/ }),
  });
  await expect(localRow).toContainText('Local');
  await expect(rows.filter({ hasText: 'Bare' })).toHaveCount(0);
});

test('fresh install status popup hides unavailable default gauges', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/health', r => r.fulfill({ json: {
    status: 'ok',
    backends: {
      claude: { label: 'Claude', available: false, gauge: { type: 'claude' } },
      codex: { label: 'Codex', available: false, gauge: { type: 'codex' } },
      cursor: { label: 'Cursor', available: false, gauge: { type: 'cursor' } },
      opencode: { label: 'OpenCode', available: true, gauge: { type: 'static', text: 'Free tier' } },
      local: { label: 'Local', available: true, gauge: { type: 'none' } },
    },
  }}));
  await page.route('**/processes', r => r.fulfill({ json: [] }));

  await page.goto('/');
  await page.locator('#proc-status').click();

  const rows = page.locator('#proc-status-popup .quota-status-row');
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText(['OpenCode']);
  await expect(page.locator('#proc-status-popup')).not.toContainText('Claude');
  await expect(page.locator('#proc-status-popup')).not.toContainText('Codex');
  await expect(page.locator('#proc-status-popup')).not.toContainText('Cursor');
  await expect(page.locator('#proc-status-popup')).toContainText('No active processes or queued prompts.');
});

test('status popup closes on Escape', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  const popup = page.locator('#proc-status-popup');
  await page.locator('#proc-status').click();
  await expect(popup).toHaveClass(/open/);

  await page.keyboard.press('Escape');
  await expect(popup).not.toHaveClass(/open/);
});

test('/status command opens the popup and survives a same-tick outside click (mobile ghost-click)', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  // Mobile browsers can fire a synthetic/ghost click on an unrelated element
  // in the same tick right after a virtual-keyboard-dismissing submit opens
  // the popup. Drive both actions inside one evaluate() so there's no
  // Playwright IPC delay between them, matching that real-world race.
  const result = await page.evaluate(() => {
    toggleProcPopup();
    const openedByCommand = procStatusPopup.classList.contains('open');
    document.querySelector('#topbar .text-logo').dispatchEvent(new MouseEvent('click', { bubbles: true }));
    return { openedByCommand, openAfterGhostClick: procStatusPopup.classList.contains('open') };
  });
  expect(result.openedByCommand).toBe(true);
  expect(result.openAfterGhostClick).toBe(true);

  // A real outside click well after opening still closes it normally.
  await page.waitForTimeout(350);
  await page.locator('#topbar .text-logo').click();
  await expect(page.locator('#proc-status-popup')).not.toHaveClass(/open/);
});

test('status popup formats idle live-session durations above a minute', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/processes', r => r.fulfill({ json: [
    { topic: 'squid', agent: 'claude', state: 'idle', prompt_preview: 'seconds', state_duration_s: 42 },
    { topic: 'squid', agent: 'codex', state: 'idle', prompt_preview: 'minutes', state_duration_s: 186 },
    { topic: 'squid', agent: 'qwen', state: 'idle', prompt_preview: 'hours', state_duration_s: 11160 },
    { topic: 'squid', agent: 'deepseek', state: 'idle', prompt_preview: 'days', state_duration_s: 267840 },
  ] }));

  await page.goto('/');
  await page.locator('#proc-status').click();

  const popup = page.locator('#proc-status-popup');
  await expect(popup).toContainText('42s');
  await expect(popup).toContainText('3.1m');
  await expect(popup).toContainText('3.1h');
  await expect(popup).toContainText('3.1d');
});

test('an empty pre-chat poll does not stop tracking the newly started process', async ({ page }) => {
  await mockBackend(page);
  let chatStarted = false;
  let processPollsAfterChat = 0;
  await page.route('**/processes', r => {
    if (chatStarted) processPollsAfterChat++;
    const visible = chatStarted && processPollsAfterChat > 0;
    return r.fulfill({ json: visible ? runningProcess() : [] });
  });

  let finishChat;
  await page.route('**/chat', async r => {
    chatStarted = true;
    await new Promise(resolve => { finishChat = resolve; });
    await r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: 'event: meta\ndata: {"agent":"claude","backend":"claude","msg_id":42}\n\ndata: done\n\nevent: done\ndata:\n\n',
    });
  });

  await page.goto('/');
  await page.fill('#input', 'start work');
  await page.keyboard.press('Enter');
  await expect(page.locator('#proc-status')).toHaveClass(/has-procs/, { timeout: 5_000 });
  finishChat();
});

test('an older running poll cannot overwrite a newer completed state', async ({ page }) => {
  await mockBackend(page);
  let pollCount = 0;
  let releaseFirst;
  let markFirstRequested;
  const firstRequested = new Promise(resolve => { markFirstRequested = resolve; });
  let markSecondResolved;
  const secondResolved = new Promise(resolve => { markSecondResolved = resolve; });
  await page.route('**/processes', async r => {
    pollCount++;
    if (pollCount === 1) {
      markFirstRequested();
      await new Promise(resolve => { releaseFirst = resolve; });
      return r.fulfill({ json: runningProcess() });
    }
    markSecondResolved();
    return r.fulfill({ json: [] });
  });

  await page.goto('/');
  await firstRequested;
  await page.locator('#proc-status').click();
  await secondResolved;

  releaseFirst();
  await page.waitForTimeout(500);
  await expect(page.locator('#proc-status')).not.toHaveClass(/has-procs/);
});

// ── thinking bubble status behavior ──────────────────────────────────────────

function sse(...events) {
  return events.map(({ event, data }) => {
    const str = typeof data === 'string' ? data : JSON.stringify(data);
    return event ? `event: ${event}\ndata: ${str}\n\n` : `data: ${str}\n\n`;
  }).join('');
}

const META  = { event: 'meta',  data: { agent: 'claude', backend: 'claude', msg_id: 1, adhoc: false } };
const STATS = { event: 'stats', data: { session_id: 'test-sid', input_tokens: 10, output_tokens: 5, adhoc: false, lookback: 0 } };
const DONE  = { event: 'done',  data: '' };

const THINKING = '.msg.assistant.msg-thinking';

test('thinking bubble shows status text during streaming and collapses on done', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse(
      META,
      { event: 'status', data: 'Working...' },
      { event: 'status', data: 'Analyzing files' },
      { event: 'tool',   data: { name: 'Read', input_json: '{"file_path":"a.txt"}' } },
      { event: 'status', data: 'Complete' },
      { data: 'Response text' },
      STATS,
      DONE,
    ),
  }));

  await page.fill('#input', 'test status');
  await page.keyboard.press('Enter');

  // During streaming, thinking bubble shows status text
  const thinking = page.locator(THINKING);
  await expect(thinking).toBeVisible();
  await expect(thinking).toContainText('Working...');

  // After done, thinking freezes with a collapsible toggle
  await expect(thinking).toHaveClass(/msg-thinking-done/);
  await expect(thinking.locator('.thinking-toggle')).toBeVisible();
  // Toggle text is the last non-empty status line ("Complete")
  await expect(thinking.locator('.thinking-toggle')).toHaveText('Complete');

  // Click toggle to expand and see full status
  await thinking.locator('.thinking-toggle').click();
  await expect(thinking).toHaveClass(/thinking-expanded/);
  await expect(thinking.locator('.thinking-body')).toContainText('Analyzing files');
});

test('thinking bubble is removed on done when no status text was streamed', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse(
      META,
      { data: 'Plain response without status' },
      STATS,
      DONE,
    ),
  }));

  await page.fill('#input', 'plain');
  await page.keyboard.press('Enter');

  // Response bubble appears, thinking bubble should be gone
  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();
  await expect(page.locator(THINKING)).not.toBeAttached();
});

test('history item shows thinking toggle when status_raw is present', async ({ page }) => {
  await mockBackend(page);
  // Return a completed item with status_raw in the history
  await page.route('**/history**', r => r.fulfill({
    json: {
      items: [{
        id: 50,
        role: 'assistant',
        topic: 'squid',
        agent: 'claude',
        content: 'The response content.',
        status: 'done',
        status_raw: 'Working...\nReading files\nDone',
        session_id: 'sess-1',
        timestamp: new Date().toISOString(),
        prompt: 'test prompt',
        adhoc: false,
      }],
      has_more: false,
    },
  }));

  await page.goto('/');

  // The history item should show the thinking toggle
  const toggle = page.locator('.msg-thinking-done.history-item .thinking-toggle');
  await expect(toggle).toBeVisible();
  await expect(toggle).toHaveText('Done');

  // Click to expand and verify full status text
  await toggle.click();
  await expect(page.locator('.msg-thinking-done.history-item .thinking-body')).toContainText('Reading files');
});

test('status recovery uses persisted status_raw in polling fallback', async ({ page }) => {
  await mockBackend(page);
  // SSE drops immediately after meta — no status events delivered via SSE.
  // The client must recover status exclusively from the DB poll response.
  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse(META),
  }));

  // /chat/{id}/status returns pending with status_raw persisted by the server
  await page.route('**/chat/*/status', r => r.fulfill({
    json: {
      id: 1,
      role: 'assistant',
      topic: 'squid',
      agent: 'claude',
      content: 'Partial response',
      status: 'pending',
      status_raw: 'Working...\nReading files\nIn progress',
      session_id: 'sess-1',
    },
  }));

  await page.goto('/');
  await page.fill('#input', 'recovery test');
  await page.keyboard.press('Enter');

  // DB status_raw is recovered and shown; 'In progress' was never in the SSE stream
  const thinking = page.locator(THINKING);
  await expect(thinking).toContainText('In progress', { timeout: 5_000 });
  await expect(thinking).toContainText('Connection interrupted', { timeout: 5_000 });
});

test('done polling fallback freezes persisted status_raw', async ({ page }) => {
  await mockBackend(page);
  // SSE drops before any status events. The first poll already sees completion.
  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse(META),
  }));

  await page.route('**/chat/*/status', r => r.fulfill({
    json: {
      id: 1,
      role: 'assistant',
      topic: 'squid',
      agent: 'claude',
      content: 'Recovered final response',
      status: 'done',
      status_raw: 'Working...\nReading files\nDone',
      session_id: 'sess-1',
    },
  }));

  await page.goto('/');
  await page.fill('#input', 'done recovery test');
  await page.keyboard.press('Enter');

  const thinking = page.locator(THINKING);
  await expect(thinking).toHaveClass(/msg-thinking-done/, { timeout: 5_000 });
  await expect(thinking.locator('.thinking-toggle')).toHaveText('Done');
  await thinking.locator('.thinking-toggle').click();
  await expect(thinking.locator('.thinking-body')).toContainText('Reading files');
  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toContainText('Recovered final response');
});
