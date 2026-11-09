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

  const expectedTimes = await page.evaluate(() => [
    fmtTime('2026-07-15T12:10:28Z'),
    fmtTime('2026-07-15T12:15:25Z'),
  ]);
  await expect(page.locator('#messages > .msg-time.history-item')).toHaveText(expectedTimes);
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

test('scroll-up pagination anchors older pages at the top even with a live group present', async ({ page }) => {
  await mockApp(page);

  const tall = 'x\n'.repeat(120); // tall enough to fill the viewport and stop the auto-chain
  await page.route('**/history**', route => {
    const url = new URL(route.request().url());
    const offset = Number(url.searchParams.get('offset') || '0');
    if (offset === 0) {
      return route.fulfill({ json: {
        items: [
          { id: 202, role: 'assistant', topic: 'squid', agent: 'claude', content: tall, status: 'done', prompt: 'p', completed_at: '2026-07-15T10:02:00Z' },
          { id: 201, role: 'assistant', topic: 'squid', agent: 'claude', content: tall, status: 'done', prompt: 'p', completed_at: '2026-07-15T10:01:00Z' },
        ],
        has_more: true,
      } });
    }
    return route.fulfill({ json: {
      items: [
        { id: 200, role: 'assistant', topic: 'squid', agent: 'claude', content: 'older completed', status: 'done', prompt: 'p', completed_at: '2026-07-15T09:00:00Z' },
      ],
      has_more: false,
    } });
  });

  await page.goto('/');
  await page.waitForFunction(() => document.querySelector('.msg.assistant.history-item[data-msg-id="202"]'));

  // A still-running turn newer than everything fetched so far. The buggy
  // anchor locked onto this group and inserted older pages before it (i.e. at
  // the bottom) instead of at the top of the transcript.
  await page.evaluate(() => {
    const thinking = document.createElement('div');
    thinking.className = 'msg-thinking';
    thinking.dataset.msgId = '300';
    document.getElementById('messages').appendChild(thinking);
  });

  await page.evaluate(() => { historyExhausted = false; loadHistory(); });
  await page.waitForFunction(() => document.querySelector('.msg.assistant.history-item[data-msg-id="200"]'));

  const ids = await page.locator('#messages > .msg.assistant.history-item').evaluateAll(
    rows => rows.map(row => row.dataset.msgId)
  );
  expect(ids).toEqual(['200', '201', '202']);
});

for (const clipboardFails of [false, true]) {
  test(`ctx message ID copy ${clipboardFails ? 'reports failure' : 'copies without jumping'}`, async ({ page }) => {
    await mockApp(page);
    await page.route('**/history**', route => route.fulfill({ json: {
      items: [{ id: 200, role: 'assistant', topic: 'squid', agent: 'claude',
        content: 'reply', prompt: 'prompt', status: 'done' }], has_more: false,
    } }));
    await page.route('**/chat/200/context', route => route.fulfill({ json: {
      attempts: [{ sources: [], instructions: [] }],
    } }));
    await page.addInitScript(fails => {
      window.copiedMessageIds = [];
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
        writeText: async text => {
          window.copiedMessageIds.push(text);
          if (fails) throw new Error('Clipboard denied');
        },
      } });
    }, clipboardFails);
    await page.goto('/');
    await page.locator('.msg[data-msg-id="200"] .user-ctx').click();
    const copy = page.getByRole('button', { name: 'Copy message ID', exact: true });
    await copy.click();
    await expect(copy).toHaveAttribute('title', clipboardFails ? 'Copy failed' : 'Copied');
    expect(await page.evaluate(() => window.copiedMessageIds)).toEqual(['200']);
    await expect(page.locator('#ctx-popup')).toHaveClass(/open/);
    await expect(page.locator('.msg[data-msg-id="200"]')).not.toHaveClass(/msg-jump-highlight/);
    await expect(copy).toHaveAttribute('title', 'Copy message ID');
  });
}

for (const injected of [false, true]) {
  test(`ctx ${injected ? 'shows recorded instruction text' : 'omits unsent instructions'}`, async ({ page }) => {
    await mockApp(page);
    await page.route('**/history**', route => route.fulfill({ json: {
      items: [{ id: 200, role: 'assistant', topic: 'squid', agent: 'claude',
        content: 'reply', prompt: 'prompt', status: 'done' }], has_more: false,
    } }));
    await page.route('**/chat/200/context', route => route.fulfill({ json: {
      attempts: [{ sources: [], instructions: injected ? [{
        source: 'agent/runtime/launch-history.md', revision: 'recorded-revision',
        delivery: 'injected', content: 'Exact hint sent for this launch.',
      }] : [], tools: { operations: [], watch_operations: [] } }],
    } }));
    await page.goto('/');
    await page.locator('.msg[data-msg-id="200"] .user-ctx').click();
    // Wait for the recorded launch snapshot, rather than checking before fetch resolves.
    await expect(page.locator('#ctx-sources-section')).toContainText('tools');
    const links = page.locator('#ctx-sources-section a.ctx-popup-instruction');
    await expect(links).toHaveCount(injected ? 1 : 0);
    if (injected) {
      await links.click();
      await expect(page.locator('#msg-modal')).toHaveClass(/open/);
      await expect(page.locator('#msg-modal-body pre')).toHaveText(
        'Revision: recorded-revision\n\nExact hint sent for this launch.');
    }
  });
}


for (const liveId of [50, 101, 300]) {
  test(`jump pagination preserves history order with live turn ${liveId}`, async ({ page }) => {
    await mockApp(page);
    const item = id => ({
      id, role: 'assistant', topic: 'squid', agent: 'claude',
      content: 'old response', status: 'done', prompt: 'old prompt',
      timestamp: '2026-11-06T10:00:00Z', completed_at: '2026-11-06T10:01:00Z',
    });
    await page.route('**/history**', route => {
      const url = new URL(route.request().url());
      if (!url.pathname.endsWith('/around')) {
        return route.fulfill({ json: { items: [], has_more: false } });
      }
      const paging = url.searchParams.has('direction');
      return route.fulfill({ json: {
        found: true, target_id: 100, items: [item(paging ? 102 : 100)],
        has_older: false, has_newer: true, has_more: true,
        newer_cursor: { id: paging ? 102 : 100, completed_at: '2026-11-06T10:01:00Z' },
      } });
    });
    await page.goto('/');
    await page.waitForFunction(() => typeof messages !== 'undefined' && typeof jumpToMessage === 'function');
    await page.evaluate(async liveId => {
      const prompt = document.createElement('div');
      prompt.className = 'msg user';
      prompt.textContent = 'new live prompt';
      const thinking = document.createElement('div');
      thinking.className = 'msg-thinking';
      thinking.dataset.msgId = String(liveId);
      messages.append(prompt, thinking);
      await jumpToMessage(100);
      historyWindowEdgesArmed = false;
    }, liveId);
    const order = () => page.locator('#messages > .msg, #messages > .msg-thinking, #history-bottom-sentinel').evaluateAll(
      rows => rows.map(row => row.dataset.msgId || row.id || row.textContent)
    );
    let rows = await order();
    expect(rows).toContain('100');
    expect(rows).toContain('new live prompt');
    expect(rows.indexOf('100') < rows.indexOf('new live prompt')).toBe(liveId > 100);
    expect(rows).toContain('history-bottom-sentinel');
    expect(rows.indexOf('100')).toBeLessThan(rows.indexOf('history-bottom-sentinel'));
    expect(rows.indexOf('history-bottom-sentinel') < rows.indexOf('new live prompt')).toBe(liveId > 100);
    await page.evaluate(async () => { await loadHistoryWindow('newer'); });
    rows = await order();
    expect(rows).toContain('102');
    expect(rows.indexOf('100')).toBeLessThan(rows.indexOf('102'));
    expect(rows.indexOf('102') < rows.indexOf('new live prompt')).toBe(liveId > 102);
    expect(rows.indexOf('102')).toBeLessThan(rows.indexOf('history-bottom-sentinel'));
    expect(rows.indexOf('history-bottom-sentinel') < rows.indexOf('new live prompt')).toBe(liveId > 102);
  });

}
