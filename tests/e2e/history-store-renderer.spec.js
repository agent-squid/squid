/**
 * ADR-0041 Stage 4 — real production wiring of createHistoryRegistry into
 * #messages via ?renderer=store (see the historyReconciler declaration and
 * appendHistoryItems in ui/app.js). Unlike history-registry.spec.js (which
 * exercises the registry against an isolated container), these tests drive
 * the actual app the same way history-pagination.spec.js does for the
 * direct-DOM path — same mock setup, same scenarios, same assertions —
 * to prove store-driven rendering is behaviorally equivalent for the two
 * riskiest cases: multi-page chronological ordering, and a jump (which
 * exercises historyReconciler.reset() + the raw DOM sweep in jumpToMessage,
 * immediately followed by pagination that extends the reconciler-owned set).
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

test('renderer=store: history renders completed turns in the same order as the direct-DOM path', async ({ page }) => {
  await mockApp(page);

  await page.route('**/history**', route => route.fulfill({
    json: {
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

  await page.goto('/?renderer=store');

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

test('renderer=store: jump renders a bounded window, then older/newer pagination extends it in correct order', async ({ page }) => {
  await mockApp(page);

  await page.route('**/history**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/history/around') {
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

  await page.goto('/?renderer=store');
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
});

test('renderer=store: a topic filter change (reloadHistory) does not resurrect turns the previous scope rendered', async ({ page }) => {
  await mockApp(page);

  // Keyed off the request's own topic param, not call order — app.js's boot
  // sequence fires /history?offset=0 twice before any explicit reload (a
  // pre-existing quirk unrelated to this change), so a call counter can't
  // reliably tell "initial load" from "post-filter reload" apart.
  await page.route('**/history**', route => {
    const topic = new URL(route.request().url()).searchParams.get('topic');
    if (topic === 'other') {
      return route.fulfill({ json: { items: [
        { id: 2, role: 'assistant', topic: 'other', agent: 'claude', content: 'other turn', status: 'done', prompt: 'p2', completed_at: '2026-07-15T10:01:00Z' },
      ], has_more: false } });
    }
    return route.fulfill({ json: { items: [
      { id: 1, role: 'assistant', topic: 'squid', agent: 'claude', content: 'squid turn', status: 'done', prompt: 'p1', completed_at: '2026-07-15T10:00:00Z' },
    ], has_more: false } });
  });

  await page.goto('/?renderer=store');
  await expect(page.locator('.msg[data-msg-id="1"]')).toBeVisible();

  await page.evaluate(() => reloadHistory({ topic: 'other' }));
  await expect(page.locator('.msg[data-msg-id="2"]')).toBeVisible();

  // The bug reset() exists to prevent: without it, the reconciler's stale
  // bookkeeping for id 1 would reattach it on the next reorder() pass even
  // though the new scope never re-installed it.
  await expect(page.locator('.msg[data-msg-id="1"]')).toHaveCount(0);
  const ids = await page.locator('#messages > .msg.assistant.history-item').evaluateAll(
    rows => rows.map(row => row.dataset.msgId)
  );
  expect(ids).toEqual(['2']);
});

// A live (pending) turn holds its position by *start* time among completed
// turns sorted by end time: turns that ended before it started render above
// it, turns that ended after render below. The reconciler must interleave
// per turn (historyStoreAnchor compares completedAt against the live bubble's
// data-order-at) — a single block-wide anchor keyed on msg_id strands the
// live bubble on the wrong side whenever id order and start-vs-end-time order
// disagree. Both directions are covered below: live id below the completed
// ids (long-running turn submitted before them) and above (submitted after).
test('renderer=store: live bubble keeps its start-time slot — live id older than completed ids', async ({ page }) => {
  await mockApp(page);

  await page.route('**/history**', route => route.fulfill({
    json: {
      items: [
        { id: 102, role: 'assistant', topic: 'squid', agent: 'claude', content: 'ended after live started', status: 'done', prompt: 'p102', timestamp: '2026-07-15T12:09:00Z', completed_at: '2026-07-15T12:15:00Z' },
        { id: 101, role: 'assistant', topic: 'squid', agent: 'claude', content: '', status: 'pending', prompt: 'live prompt', timestamp: '2026-07-15T12:10:00Z' },
        { id: 100, role: 'assistant', topic: 'squid', agent: 'claude', content: 'ended before live started', status: 'done', prompt: 'p100', timestamp: '2026-07-15T12:00:00Z', completed_at: '2026-07-15T12:05:00Z' },
      ],
      has_more: false,
    },
  }));

  await page.goto('/?renderer=store');
  await expect(page.locator('.msg[data-msg-id="101"]')).toBeVisible();

  const ids = await page.locator('#messages > .msg.assistant.history-item').evaluateAll(
    rows => rows.map(row => row.dataset.msgId)
  );
  expect(ids).toEqual(['100', '101', '102']);
});

test('renderer=store: live bubble keeps its start-time slot — live id newer than completed ids', async ({ page }) => {
  await mockApp(page);

  await page.route('**/history**', route => route.fulfill({
    json: {
      items: [
        { id: 202, role: 'assistant', topic: 'squid', agent: 'claude', content: 'ended after live started', status: 'done', prompt: 'p202', timestamp: '2026-07-15T12:09:00Z', completed_at: '2026-07-15T12:15:00Z' },
        { id: 203, role: 'assistant', topic: 'squid', agent: 'claude', content: '', status: 'pending', prompt: 'live prompt', timestamp: '2026-07-15T12:10:00Z' },
        { id: 200, role: 'assistant', topic: 'squid', agent: 'claude', content: 'ended before live started', status: 'done', prompt: 'p200', timestamp: '2026-07-15T12:00:00Z', completed_at: '2026-07-15T12:05:00Z' },
      ],
      has_more: false,
    },
  }));

  await page.goto('/?renderer=store');
  await expect(page.locator('.msg[data-msg-id="203"]')).toBeVisible();

  const ids = await page.locator('#messages > .msg.assistant.history-item').evaluateAll(
    rows => rows.map(row => row.dataset.msgId)
  );
  expect(ids).toEqual(['200', '203', '202']);
});
