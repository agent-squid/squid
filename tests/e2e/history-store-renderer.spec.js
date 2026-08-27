/**
 * Production wiring of createHistoryRegistry into #messages. Unlike
 * history-registry.spec.js (which exercises an isolated registry), these
 * tests drive the reconciler through the app's real history, pagination,
 * filtering, and live-turn entry points.
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

test('reconciler renders completed turns in completion order', async ({ page }) => {
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

  await page.goto('/');

  const ids = await page.locator('#messages > .msg.assistant.history-item').evaluateAll(
    rows => rows.map(row => row.dataset.msgId)
  );
  expect(ids).toEqual(['4396', '4394']);

  const expectedTimes = await page.evaluate(() => [
    fmtTime('2026-07-15T12:10:28Z'),
    fmtTime('2026-07-15T12:15:25Z'),
  ]);
  await expect(page.locator('#messages > .msg-time.history-item:not(.user-prompt-time)')).toHaveText(expectedTimes);
});

test('a sparse snapshot cannot erase a completed turn stats footer on the next reconcile', async ({ page }) => {
  await mockApp(page);
  await page.route('**/history**', route => route.fulfill({ json: {
    items: [{
      id: 4400, role: 'assistant', topic: 'squid', agent: 'codex',
      content: 'complete', status: 'done', prompt: 'prompt',
      completed_at: '2026-07-15T12:20:00Z',
      stats: { input_tokens: 12, output_tokens: 3, duration_ms: 1000 },
    }],
    has_more: false,
  }}));

  await page.goto('/');
  await expect(page.locator('#messages > .stats.history-item')).toContainText('↑ 12');

  await page.evaluate(() => {
    shadowInstallHistoryPage([{
      id: 4400, role: 'assistant', topic: 'squid', agent: 'codex',
      content: 'complete', status: 'done', prompt: 'prompt',
      completed_at: '2026-07-15T12:20:00Z',
      // Realtime snapshot row: deliberately no stats field.
    }]);
    historyReconciler.reconcile();
  });

  await expect(page.locator('#messages > .stats.history-item')).toContainText('↑ 12');
  await expect(page.locator('#messages > .msg-time.history-item:not(.user-prompt-time)')).toHaveCount(0);
});

test('jump renders a bounded window, then reconciler pagination extends it in correct order', async ({ page }) => {
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
});

// ADR-0041 Gap 1: a history row with no explicit `status` field (some
// fixtures/older server rows omit it, e.g. deep-dive-button.spec.js) must
// still render as a completed turn. Before the fix, historyItemToStoreRows only set
// completed_at for an explicit terminal status and isTerminal only
// recognized an explicit terminal status, so the row rendered nowhere.
test('a history row with no status field still renders as completed', async ({ page }) => {
  await mockApp(page);

  await page.route('**/history**', route => route.fulfill({
    json: {
      items: [{
        id: 1, role: 'assistant', topic: 'squid', agent: 'claude',
        content: 'response with no status field', prompt: 'p1',
        timestamp: '2026-07-15T10:00:00Z',
      }],
      has_more: false,
    },
  }));

  await page.goto('/');
  await expect(page.locator('.msg[data-msg-id="1"]')).toBeVisible();
  await expect(page.locator('.msg[data-msg-id="1"]')).toContainText('response with no status field');
});

test('an empty error row renders the terminal fallback', async ({ page }) => {
  await mockApp(page);

  await page.route('**/history**', route => route.fulfill({
    json: {
      items: [{
        id: 2, role: 'assistant', topic: 'squid', agent: 'claude',
        content: '', context: null, status: 'error', prompt: 'failed prompt',
        timestamp: '2026-07-15T10:00:00Z', completed_at: '2026-07-15T10:00:01Z',
      }],
      has_more: false,
    },
  }));

  await page.goto('/');
  const response = page.locator('.msg.assistant.history-item[data-msg-id="2"]');
  await expect(response).toBeVisible();
  await expect(response.locator('.msg-error')).toHaveText('Response interrupted.');
});

test('a topic filter change does not resurrect turns from the previous reconciler scope', async ({ page }) => {
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

  await page.goto('/');
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
test('reconciler keeps a live bubble in its start-time slot when its id is older', async ({ page }) => {
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

  await page.goto('/');
  await expect(page.locator('.msg[data-msg-id="101"]')).toBeVisible();

  const ids = await page.locator('#messages > .msg.assistant.history-item').evaluateAll(
    rows => rows.map(row => row.dataset.msgId)
  );
  expect(ids).toEqual(['100', '101', '102']);
});

test('reconciler keeps a live bubble in its start-time slot when its id is newer', async ({ page }) => {
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

  await page.goto('/');
  await expect(page.locator('.msg[data-msg-id="203"]')).toBeVisible();

  const ids = await page.locator('#messages > .msg.assistant.history-item').evaluateAll(
    rows => rows.map(row => row.dataset.msgId)
  );
  expect(ids).toEqual(['200', '203', '202']);
});

// Regression (#13990): in an observer tab watching a Squid Flow it did not
// send, the origin turn is discovered as *pending* → reconciler-owned, while
// the chain-step target is discovered already *completed* → inserted direct-DOM
// (insertCompletedHistoryItem, which forget()s it so reorder() never touches
// it). historyStoreAnchor used to see only reconciler `next` and live thinking
// bubbles, so it fell through to bottomSentinel and dropped the reconciler-owned
// origin *below* its own already-on-screen, chronologically-later target —
// scrambling live flow order ("only the last step visible"). historyStoreAnchor
// must interleave with direct-DOM completed bubbles too.
test('a reconciler-owned flow origin lands before an already-on-screen completion fallback', async ({ page }) => {
  await mockApp(page);
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');
  await page.waitForFunction(() => typeof historyReconciler !== 'undefined' && historyReconciler
    && typeof insertCompletedHistoryItem === 'function' && typeof shadowInstallHistoryPage === 'function');

  const order = await page.evaluate(() => {
    const route = '#squid@echo>@echo1';
    // Target (echo1) discovered already-completed → direct-DOM insert (forgotten
    // by the reconciler), the same path discoverRealtimeTurn/attachFlowStep use.
    insertCompletedHistoryItem({
      id: 102, role: 'assistant', topic: 'squid', agent: 'echo1', content: 'target reply',
      status: 'done', prompt: 'chain step', reply_to: 101, flow_route: route,
      timestamp: '2026-07-15T12:00:07Z', completed_at: '2026-07-15T12:00:12Z',
    });
    // Origin (echo) becomes reconciler-owned and is placed by reorder(), which
    // asks historyStoreAnchor where it goes relative to what's already on screen.
    shadowInstallHistoryPage([{
      id: 100, role: 'assistant', topic: 'squid', agent: 'echo', content: 'origin reply',
      status: 'done', prompt: 'flow start', reply_to: 99, flow_route: route,
      timestamp: '2026-07-15T12:00:00Z', completed_at: '2026-07-15T12:00:05Z',
    }]);
    historyReconciler.reconcile();
    return [...document.querySelectorAll('#messages > .msg.assistant.history-item[data-msg-id]')]
      .map(e => e.dataset.msgId);
  });

  // Origin (100, earlier completed_at) must precede target (102), not sink below it.
  expect(order).toEqual(['100', '102']);
});

// Regression: the live header renders its user prompt from raw.prompt (the
// denormalized field HTTP history and the /status discovery fetch carry). A
// turn discovered only through sparse producers (WS snapshot/lifecycle) has a
// raw without it, while the store still knows the linked user message's text
// (turn.promptContent). The registry used to read raw.prompt alone, so such a
// turn rendered a prompt-less header live and only showed the prompt after a
// full history refresh reattached raw.prompt ("prompt disappears in live
// view"). render() must fall back to turn.promptContent.
test('a pending turn whose raw lacks the denormalized prompt still renders the linked user prompt', async ({ page }) => {
  await mockApp(page);
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');
  await page.waitForFunction(() => typeof historyReconciler !== 'undefined' && historyReconciler
    && !!window.__transcriptStore);

  await page.evaluate(() => {
    const store = window.__transcriptStore;
    // User message discovered via a lifecycle patch → gives the store its
    // promptContent, without any denormalized row.
    store.applyMessagePatch(600, { role: 'user', content: 'observed question' }, 10);
    // Assistant turn linked to it, still pending.
    store.applyMessagePatch(601, { role: 'assistant', status: 'pending', content: '', reply_to: 600 }, 20);
    // Sparse discovery raw (WS snapshot / status-less): identity + display
    // fields but NO prompt.
    store.attachRaw(601, { id: 601, topic: 'squid', agent: 'claude', adhoc: false, status: 'pending', timestamp: '2026-07-15T12:00:00Z' });
    historyReconciler.reconcileDirtyIds([601]);
  });

  await expect(
    page.locator('#messages > .msg.assistant.msg-thinking[data-msg-id="601"] .history-prompt-truncated')
  ).toContainText('observed question');
});

test('a completed turn whose raw lacks the denormalized prompt still renders the linked user prompt', async ({ page }) => {
  await mockApp(page);
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');
  await page.waitForFunction(() => typeof historyReconciler !== 'undefined' && historyReconciler
    && !!window.__transcriptStore);

  await page.evaluate(() => {
    const store = window.__transcriptStore;
    store.applyMessagePatch(700, { role: 'user', content: 'observed done question' }, 10);
    store.applyMessagePatch(701, {
      role: 'assistant', status: 'done', content: 'the answer', reply_to: 700,
      completed_at: '2026-07-15T12:00:05Z',
    }, 20);
    store.attachRaw(701, { id: 701, topic: 'squid', agent: 'claude', adhoc: false, status: 'done' });
    historyReconciler.reconcileDirtyIds([701]);
  });

  await expect(
    page.locator('#messages > .msg.assistant.history-item[data-msg-id="701"]:not(.msg-thinking) .history-prompt-truncated')
  ).toContainText('observed done question');
});

test('store renderer keeps the complete live scaffold owned after terminal reconciliation', async ({ page }) => {
  await mockApp(page);
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');

  const state = await page.evaluate(() => {
    const pending = {
      id: 801, role: 'assistant', reply_to: 800, topic: 'squid', agent: 'claude',
      status: 'pending', content: '', prompt: 'keep this live prompt',
      timestamp: '2026-08-27T12:00:00Z',
    };
    shadowInstallHistoryPage([pending], undefined, { source: 'live' });
    historyReconciler.reconcileDirtyIds([801]);

    const pendingGroup = historyReconciler.getGroup(801);
    const prompt = document.querySelector('#messages > .msg.user[data-turn-owner-id="801"]');
    shadowInstallHistoryPage([{
      ...pending,
      status: 'done',
      content: 'final response',
      completed_at: '2026-08-27T12:01:00Z',
    }], undefined, { source: 'live' });
    historyReconciler.reconcileDirtyIds([801]);

    // Exercise a later store render as well. A prompt that merely happens to
    // remain in the DOM, but was dropped from the registry's owned range, is
    // vulnerable to the next reset/reorder race.
    window.__transcriptStore.applyMessagePatch(801, {
      stats: { input_tokens: 10, output_tokens: 2, duration_ms: 1000 },
    }, 20);
    historyReconciler.reconcileDirtyIds([801]);

    const completedGroup = historyReconciler.getGroup(801);
    return {
      pendingOwnedPrompt: pendingGroup.nodes.includes(prompt),
      promptConnected: !!prompt?.isConnected,
      completedOwnedPrompt: completedGroup.nodes.includes(prompt),
      responseConnected: completedGroup.nodes.some(node =>
        node.matches?.('.msg.assistant.history-item[data-msg-id="801"]:not(.msg-thinking)')),
    };
  });

  expect(state).toEqual({
    pendingOwnedPrompt: true,
    promptConnected: true,
    completedOwnedPrompt: true,
    responseConnected: true,
  });
});

test('store renderer keeps a cancelled live prompt owned and visible', async ({ page }) => {
  await mockApp(page);
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');

  const state = await page.evaluate(() => {
    const pending = {
      id: 802, role: 'assistant', reply_to: 799, topic: 'squid', agent: 'claude',
      status: 'pending', content: '', prompt: 'do not remove cancelled prompt',
      timestamp: '2026-08-27T12:02:00Z',
    };
    shadowInstallHistoryPage([pending], undefined, { source: 'live' });
    historyReconciler.reconcileDirtyIds([802]);
    const prompt = document.querySelector('#messages > .msg.user[data-turn-owner-id="802"]');

    shadowInstallHistoryPage([{
      ...pending,
      status: 'cancelled',
      content: '',
      completed_at: '2026-08-27T12:03:00Z',
    }], undefined, { source: 'live' });
    historyReconciler.reconcileDirtyIds([802]);

    // Re-dirty the cancelled turn to catch a prompt that survived only as an
    // unowned DOM orphan during the first terminal render.
    window.__transcriptStore.attachRaw(802, {
      ...pending,
      status: 'cancelled',
      content: '',
      completed_at: '2026-08-27T12:03:00Z',
    });
    historyReconciler.reconcileDirtyIds([802]);

    const group = historyReconciler.getGroup(802);
    return {
      promptConnected: !!prompt?.isConnected,
      promptText: prompt?.textContent || '',
      promptOwned: group.nodes.includes(prompt),
      cancelledResponseConnected: group.nodes.some(node =>
        node.matches?.('.msg.assistant.history-item[data-msg-id="802"]:not(.msg-thinking)')),
    };
  });

  expect(state).toEqual({
    promptConnected: true,
    promptText: expect.stringContaining('do not remove cancelled prompt'),
    promptOwned: true,
    cancelledResponseConnected: true,
  });
});

test('store reconciliation leaves the boot boundary between history and new responses', async ({ page }) => {
  await mockApp(page);
  await page.route('**/history**', r => r.fulfill({ json: {
    items: [{
      id: 810, role: 'assistant', reply_to: 809, topic: 'squid', agent: 'claude',
      status: 'done', prompt: 'before boot', content: 'historical response',
      timestamp: '2026-08-27T10:00:00Z', completed_at: '2026-08-27T10:01:00Z',
    }],
    has_more: false,
  }}));
  await page.goto('/');
  await expect(page.locator('#messages > .boot-banner')).toBeVisible();

  const order = await page.evaluate(() => {
    const pending = {
      id: 812, role: 'assistant', reply_to: 811, topic: 'squid', agent: 'claude',
      status: 'pending', prompt: 'after boot', content: '',
      timestamp: '2026-08-27T12:00:00Z',
    };
    shadowInstallHistoryPage([pending], undefined, { source: 'live' });
    historyReconciler.reconcileDirtyIds([812]);
    shadowInstallHistoryPage([{
      ...pending, status: 'done', content: 'new response',
      completed_at: '2026-08-27T12:01:00Z',
    }], undefined, { source: 'live' });
    historyReconciler.reconcileDirtyIds([812]);

    return [...document.querySelectorAll('#messages > *')]
      .filter(node => node.matches('.boot-banner, .msg.assistant[data-msg-id]'))
      .map(node => node.classList.contains('boot-banner') ? 'boot' : node.dataset.msgId);
  });

  expect(order).toEqual(['810', 'boot', '812']);
});

test('store pagination and live completion share one stable response order', async ({ page }) => {
  await mockApp(page);
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#messages > .boot-banner')).toBeVisible();

  const order = await page.evaluate(() => {
    const latest = {
      id: 822, role: 'assistant', reply_to: 821, topic: 'squid', agent: 'claude',
      status: 'done', prompt: 'latest history', content: 'latest response',
      timestamp: '2026-08-27T11:00:00Z', completed_at: '2026-08-27T11:01:00Z',
    };
    const pending = {
      id: 824, role: 'assistant', reply_to: 823, topic: 'squid', agent: 'claude',
      status: 'pending', prompt: 'live prompt', content: '',
      timestamp: '2026-08-27T12:00:00Z',
    };
    shadowInstallHistoryPage([latest], { kind: 'offset', offset: 0, hasMore: true }, { source: 'history' });
    shadowInstallHistoryPage([pending], undefined, { source: 'live' });
    historyReconciler.reconcile();

    // Install the next (older) page after the live turn already exists.
    shadowInstallHistoryPage([{
      id: 820, role: 'assistant', reply_to: 819, topic: 'squid', agent: 'claude',
      status: 'done', prompt: 'older history', content: 'older response',
      timestamp: '2026-08-27T10:00:00Z', completed_at: '2026-08-27T10:01:00Z',
    }], { kind: 'offset', offset: 1, hasMore: false }, { source: 'history' });
    historyReconciler.reconcile();

    shadowInstallHistoryPage([{
      ...pending, status: 'done', content: 'live final response',
      completed_at: '2026-08-27T12:01:00Z',
    }], undefined, { source: 'live' });
    historyReconciler.reconcile();

    return [...document.querySelectorAll('#messages > *')]
      .filter(node => node.matches('.boot-banner, .msg.assistant[data-msg-id]'))
      .map(node => node.classList.contains('boot-banner') ? 'boot' : node.dataset.msgId);
  });

  expect(order).toEqual(['820', '822', 'boot', '824']);
});
