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
