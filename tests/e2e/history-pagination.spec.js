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

test('jump renders a bounded message window and pages older and newer from its edges', async ({ page }) => {
  await mockApp(page);

  const aroundRequests = [];
  await page.route('**/history**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/history/around') {
      aroundRequests.push(url.search);
      const direction = url.searchParams.get('direction');
      if (direction === 'older') {
        return route.fulfill({ json: {
          items: [
            { id: 97, role: 'assistant', topic: 'squid', agent: 'claude', content: 'older edge', status: 'done', prompt: 'older edge prompt', completed_at: '2026-07-15T09:57:00Z' },
          ],
          direction: 'older',
          has_more: false,
          older_cursor: { id: 97, completed_at: '2026-07-15T09:57:00Z' },
          newer_cursor: { id: 97, completed_at: '2026-07-15T09:57:00Z' },
        } });
      }
      if (direction === 'newer') {
        return route.fulfill({ json: {
          items: [
            { id: 103, role: 'assistant', topic: 'squid', agent: 'claude', content: 'newer edge', status: 'done', prompt: 'newer edge prompt', completed_at: '2026-07-15T10:03:00Z' },
          ],
          direction: 'newer',
          has_more: false,
          older_cursor: { id: 103, completed_at: '2026-07-15T10:03:00Z' },
          newer_cursor: { id: 103, completed_at: '2026-07-15T10:03:00Z' },
        } });
      }
      return route.fulfill({ json: {
        items: [
          { id: 102, role: 'assistant', topic: 'squid', agent: 'claude', content: 'newer response', status: 'done', prompt: 'newer prompt', completed_at: '2026-07-15T10:02:00Z' },
          { id: 100, role: 'assistant', topic: 'squid', agent: 'claude', content: 'target response', status: 'done', prompt: 'target prompt', completed_at: '2026-07-15T10:00:00Z' },
          { id: 98, role: 'assistant', topic: 'squid', agent: 'claude', content: 'older response', status: 'done', prompt: 'older prompt', completed_at: '2026-07-15T09:58:00Z' },
        ],
        target_id: 100,
        found: true,
        has_older: true,
        has_newer: true,
        older_cursor: { id: 98, completed_at: '2026-07-15T09:58:00Z' },
        newer_cursor: { id: 102, completed_at: '2026-07-15T10:02:00Z' },
      } });
    }
    return route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/jump 100');
  await page.keyboard.press('Enter');

  await expect(page.locator('.msg[data-msg-id="100"]')).toHaveClass(/msg-jump-highlight/);
  await expect(page.locator('#messages > .msg.assistant.history-item')).toHaveCount(3);

  await page.evaluate(() => loadHistoryWindow('older'));
  await expect(page.locator('.msg[data-msg-id="97"]')).toBeVisible();

  await page.evaluate(() => loadHistoryWindow('newer'));
  await expect(page.locator('.msg[data-msg-id="103"]')).toBeVisible();

  const ids = await page.locator('#messages > .msg.assistant.history-item').evaluateAll(
    rows => rows.map(row => row.dataset.msgId)
  );
  expect(ids).toEqual(['97', '98', '100', '102', '103']);
  expect(aroundRequests.some(q => q.includes('msg_id=100'))).toBe(true);
  expect(aroundRequests.some(q => q.includes('direction=older'))).toBe(true);
  expect(aroundRequests.some(q => q.includes('direction=newer'))).toBe(true);
});

test('jump flow resolves a flow run id without treating it as a message id', async ({ page }) => {
  await mockApp(page);

  const aroundRequests = [];
  await page.route('**/history**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/history/around') {
      aroundRequests.push(url.search);
      return route.fulfill({ json: {
        items: [
          { id: 200, role: 'assistant', topic: 'squid', agent: 'claude', flow_run_id: '71', content: 'flow target', status: 'done', prompt: 'flow prompt', completed_at: '2026-07-15T10:00:00Z' },
        ],
        target_id: 200,
        flow_run_id: '71',
        found: true,
        has_older: false,
        has_newer: false,
      } });
    }
    return route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/jump flow: 71');
  await page.keyboard.press('Enter');

  await expect(page.locator('.msg[data-msg-id="200"]')).toHaveClass(/msg-jump-highlight/);
  expect(aroundRequests.some(q => q.includes('flow_run_id=71'))).toBe(true);
  expect(aroundRequests.some(q => q.includes('msg_id=71'))).toBe(false);
});

test('context popup flow id links jump to the surrounding transcript', async ({ page }) => {
  await mockApp(page);

  const aroundRequests = [];
  await page.route('**/history**', route => {
    const url = new URL(route.request().url());
    const item = {
      id: 200, role: 'assistant', topic: 'squid', agent: 'claude',
      flow_run_id: '71', content: 'flow target', status: 'done',
      prompt: 'flow prompt', completed_at: '2026-07-15T10:00:00Z',
    };
    if (url.pathname === '/history/around') {
      aroundRequests.push(url.search);
      return route.fulfill({ json: {
        items: [item],
        target_id: 200,
        flow_run_id: '71',
        found: true,
        has_older: false,
        has_newer: false,
      } });
    }
    return route.fulfill({ json: { items: [item], has_more: false } });
  });

  await page.goto('/');
  await page.locator('.msg[data-msg-id="200"] .user-ctx').click();
  await page.locator('.ctx-popup-jump-row[data-jump-flow-run-id="71"] .ctx-popup-link').click();

  await expect(page.locator('.msg[data-msg-id="200"]')).toHaveClass(/msg-jump-highlight/);
  expect(aroundRequests.some(q => q.includes('flow_run_id=71'))).toBe(true);
});

test('scroll button abandons jump window and reloads latest history page', async ({ page }) => {
  await mockApp(page);

  const historyRequests = [];
  const aroundRequests = [];
  await page.route('**/history**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/history/around') {
      aroundRequests.push(url.search);
      return route.fulfill({ json: {
        items: [
          { id: 102, role: 'assistant', topic: 'squid', agent: 'claude', content: 'newer response', status: 'done', prompt: 'newer prompt', completed_at: '2026-07-15T10:02:00Z' },
          { id: 100, role: 'assistant', topic: 'squid', agent: 'claude', content: 'target response', status: 'done', prompt: 'target prompt', completed_at: '2026-07-15T10:00:00Z' },
          { id: 98, role: 'assistant', topic: 'squid', agent: 'claude', content: 'older response', status: 'done', prompt: 'older prompt', completed_at: '2026-07-15T09:58:00Z' },
        ],
        target_id: 100,
        found: true,
        has_older: true,
        has_newer: true,
        older_cursor: { id: 98, completed_at: '2026-07-15T09:58:00Z' },
        newer_cursor: { id: 102, completed_at: '2026-07-15T10:02:00Z' },
      } });
    }
    historyRequests.push(url.search);
    return route.fulfill({ json: {
      items: [
        { id: 300, role: 'assistant', topic: 'squid', agent: 'claude', content: 'latest response', status: 'done', prompt: 'latest prompt', completed_at: '2026-07-15T11:00:00Z' },
      ],
      has_more: true,
    } });
  });

  await page.goto('/');
  await page.fill('#input', '/jump 100');
  await page.keyboard.press('Enter');
  await expect(page.locator('#scroll-btn')).toBeVisible();

  await page.locator('#scroll-btn').click();

  await expect(page.locator('.msg[data-msg-id="300"]')).toBeVisible();
  await expect(page.locator('.msg[data-msg-id="100"]')).toHaveCount(0);
  expect(historyRequests.some(q => q.includes('offset=0') && q.includes('limit=5'))).toBe(true);
  expect(aroundRequests.some(q => q.includes('direction=newer'))).toBe(false);
});
