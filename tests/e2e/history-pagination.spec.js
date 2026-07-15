/**
 * Regression test for the "switch tabs mid-pagination" bug: #view-chat is
 * display:none while another tab is active, which collapses the sentinel's
 * getBoundingClientRect() to zero — making the self-chaining pagination check
 * in loadHistory() think the sentinel is still visible no matter what, and
 * chain-load the entire history in the background.
 */
const { test, expect } = require('@playwright/test');

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [
    { name: 'squid', agent: 'claude', last_model: null, last_backend: 'claude', queue_depth: 0, active: false, last_prompt: 'hi' }
  ]}));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/stats/filters', r => r.fulfill({ json: { agents: ['claude'], topics: ['squid'] } }));
  await page.route('**/stats/filter-presets', r => r.fulfill({ json: [] }));
  await page.route('**/stats?**', r => r.fulfill({ json: [] }));
}

test('loadHistory does not chain-load pages while the Chat tab is backgrounded', async ({ page }) => {
  await mockApp(page);

  let requestCount = 0;
  await page.route('**/history**', async route => {
    requestCount++;
    const offset = Number(new URL(route.request().url()).searchParams.get('offset') || '0');
    const items = Array.from({ length: 5 }, (_, i) => ({
      id: 1000 - offset - i, role: 'assistant', topic: 'squid', agent: 'claude',
      content: `reply ${offset + i}`, status: 'done', adhoc: false,
      prompt: `prompt ${offset + i}`, context: null, timestamp: new Date().toISOString(),
    }));
    // has_more never flips false — a runaway chain would keep requesting forever.
    await route.fulfill({ json: { items, has_more: true } });
  });

  await page.goto('/');
  await page.waitForLoadState('load');
  await page.waitForTimeout(500); // let initial boot pagination settle

  // Switch away from Chat — #view-chat is now display:none.
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#view-chat')).not.toHaveClass(/active/);

  const before = requestCount;
  // Simulate a pagination page landing while backgrounded — the exact situation a
  // page already in flight ends up in when the user switches tabs before it resolves.
  // Without the currentView guard, the self-chaining check at the tail of loadHistory()
  // reads the hidden container's zero-size rect as "sentinel still visible" and calls
  // loadHistory() again immediately, forever (since has_more never goes false).
  await page.evaluate(() => { historyExhausted = false; loadHistory(); });
  await page.waitForTimeout(1000);

  expect(requestCount - before).toBeLessThanOrEqual(1);
});

test('history renders completed turns in the same order as live completion', async ({ page }) => {
  await mockApp(page);

  await page.route('**/history**', route => route.fulfill({
    json: {
      // Backend returns newest completed first; the UI reverses the page before
      // appending, so the visible transcript remains oldest-to-newest.
      items: [
        {
          id: 4394, role: 'assistant', topic: 'squid', agent: 'codex',
          content: 'slow response', status: 'done', adhoc: false,
          prompt: 'slow prompt',
          timestamp: '2026-07-15T12:06:46Z',
          completed_at: '2026-07-15T12:15:25Z',
        },
        {
          id: 4396, role: 'assistant', topic: 'squid', agent: 'deepseek',
          content: 'fast response', status: 'done', adhoc: false,
          prompt: 'fast prompt',
          timestamp: '2026-07-15T12:09:14Z',
          completed_at: '2026-07-15T12:10:28Z',
        },
      ],
      has_more: false,
    },
  }));

  await page.goto('/');

  const ids = await page.locator('#messages > .msg.assistant.history-item').evaluateAll(
    rows => rows.map(row => row.dataset.msgId)
  );
  expect(ids).toEqual(['4396', '4394']);
});
