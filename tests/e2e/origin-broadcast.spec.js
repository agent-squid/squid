const { test, expect } = require('@playwright/test');

test('composer sends origin broadcast as independent per-agent turns sharing one flow_run_id', async ({ page }) => {
  // Origin Broadcast (ADR-0032): `#topic@a,@b` is sugar over sending
  // `#topic@a` and `#topic@b` separately with the same literal prompt — no
  // chain envelope, no server-side dispatch coupling (agent/flow.py never
  // recognizes a bare comma-list, since there's no operator). The two turns
  // are grouped for display only via a shared flow_run_id.
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic: 'squid',
    content: '',
    revision: 'r1',
    exists: false,
  } }));

  const chatBodies = [];
  await page.route('**/chat', r => {
    const body = JSON.parse(r.request().postData() || '{}');
    chatBodies.push(body);
    const msgId = body.agent === 'opencode' ? 10 : 11;
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': String(msgId), 'X-Squid-Flow-Run-Id': 'b1' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":${msgId},"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent} response\n\nevent: done\ndata: \n\n`,
    });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@opencode!,@qwen! review this');
  await expect(page.locator('#topic-chip')).toHaveClass(/origin-broadcast/);
  await expect(page.locator('#topic-chip')).not.toHaveClass(/route-chain/);
  // The composer chip display skips repeating a topic that's unchanged from
  // the item right before it (purely cosmetic); the underlying route/flow_route
  // is always fully-explicit — see the flow_route assertions below.
  await expect(page.locator('#topic-chip')).toContainText('#squid@opencode!,@qwen!');
  await expect(page.locator('#input')).toHaveValue('review this');
  await page.locator('#input').press('Enter');

  // Two independent origin turns are posted — one per broadcast agent, both
  // carrying the same literal prompt and the same flow_route for grouping,
  // and no chain-only fields (route/flow_run_id echo back but there's no
  // downstream target — agent/flow.py has nothing to recognize here).
  await expect.poll(() => chatBodies.length).toBe(2);
  // flow_route is stored/echoed in *reduced* form (ADR-0032, "Canonical Key"):
  // both agents share topic squid, so the second drops its topic.
  expect(chatBodies[0]).toMatchObject({
    topic: 'squid',
    agent: 'opencode',
    message: 'review this',
    adhoc: true,
    source: 'human',
    flow_route: '#squid@opencode!,@qwen!',
  });
  expect(chatBodies[1]).toMatchObject({
    topic: 'squid',
    agent: 'qwen',
    message: 'review this',
    adhoc: true,
    source: 'human',
    flow_route: '#squid@opencode!,@qwen!',
  });
  // The second call reuses the flow_run_id the server allocated for the first.
  expect(chatBodies[1].flow_run_id).toBe('b1');

  // Both agents get their own independent response, but since every target
  // got the same literal prompt, only one user bubble renders — the second
  // turn suppresses its own to avoid showing the prompt twice. That shared
  // bubble shows the full route (both targets), live-only — never persisted,
  // since each target's own history row shows just its own #topic@agent.
  await expect(page.locator('.msg.user')).toHaveCount(1);
  await expect(page.locator('.msg.user .topic-tag')).toContainText('#squid@opencode!,@qwen!');
  await expect(page.locator('.route-chain-marker')).toHaveCount(0);
  await expect(page.locator('.msg.assistant[data-agent="opencode"]')).toHaveCount(1);
  await expect(page.locator('.msg.assistant[data-agent="qwen"]')).toHaveCount(1);
});

test('origin broadcast resolves omitted halves by rolling anchor, across topics', async ({ page }) => {
  // ADR-0032: within an origin list, whichever half an atom omits is
  // inherited from its nearest fully-explicit ancestor, not one anchor fixed
  // for the whole list. `#t1@a1,#t2,@a2` — `#t2` (bare topic) borrows `@a1`
  // from the root; `@a2` (bare agent) borrows `#t1` from the *same* still-
  // unsuperseded root, since neither `#t2` nor `@a2` was itself fully
  // explicit. Resolves to three independent origins: (t1,a1), (t2,a1), (t1,a2).
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic: 't1', content: '', revision: 'r1', exists: false,
  } }));

  const chatBodies = [];
  await page.route('**/chat', r => {
    const body = JSON.parse(r.request().postData() || '{}');
    chatBodies.push(body);
    const msgId = 10 + chatBodies.length;
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': String(msgId), 'X-Squid-Flow-Run-Id': 'b1' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":${msgId},"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent}@${body.topic} response\n\nevent: done\ndata: \n\n`,
    });
  });

  await page.goto('/');
  await page.fill('#input', '#t1@a1,#t2,@a2 review this');
  await expect(page.locator('#topic-chip')).toContainText('#t1@a1,#t2@a1,#t1@a2');
  await expect(page.locator('#input')).toHaveValue('review this');
  await page.locator('#input').press('Enter');

  await expect.poll(() => chatBodies.length).toBe(3);
  expect(chatBodies[0]).toMatchObject({ topic: 't1', agent: 'a1', message: 'review this' });
  expect(chatBodies[1]).toMatchObject({ topic: 't2', agent: 'a1', message: 'review this' });
  expect(chatBodies[2]).toMatchObject({ topic: 't1', agent: 'a2', message: 'review this' });
  // Reduced form (ADR-0032, "Canonical Key"): (t1,a1) covers both siblings
  // (shares agent a1 with (t2,a1), shares topic t1 with (t1,a2)) and anchors
  // the only run — this happens to equal the original typed text exactly,
  // since it was already minimal.
  expect(chatBodies[0].flow_route).toBe('#t1@a1,#t2,@a2');
  expect(chatBodies[1].flow_route).toBe('#t1@a1,#t2,@a2');
  expect(chatBodies[2].flow_route).toBe('#t1@a1,#t2,@a2');
});

test('origin broadcast route is stored/recalled in minimal (dominating-set) form', async ({ page }) => {
  // ADR-0032, "Canonical Key (Storage/Dedup Identity)": the stored/displayed
  // flow_route is reduced via greedy max-coverage grouping, not left
  // fully-explicit and not just topic-sorted. #t1@a2,#t3@a1,#t4@a2 has no
  // adjacent repeated topics (so a topic-only sort finds nothing to drop),
  // but #t1@a2 and #t4@a2 share an agent — the minimal form exploits that.
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic: 't1', content: '', revision: 'r1', exists: false,
  } }));

  const chatBodies = [];
  await page.route('**/chat', r => {
    const body = JSON.parse(r.request().postData() || '{}');
    chatBodies.push(body);
    const msgId = 10 + chatBodies.length;
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': String(msgId), 'X-Squid-Flow-Run-Id': 'b1' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":${msgId},"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent}@${body.topic} response\n\nevent: done\ndata: \n\n`,
    });
  });

  await page.goto('/');
  await page.fill('#input', '#t1@a2,#t3@a1,#t4@a2 review this');
  await page.locator('#input').press('Enter');
  await expect.poll(() => chatBodies.length).toBe(3);
  expect(chatBodies[0].flow_route).toBe('#t1@a2,#t4,#t3@a1');
  expect(chatBodies[1].flow_route).toBe('#t1@a2,#t4,#t3@a1');
  expect(chatBodies[2].flow_route).toBe('#t1@a2,#t4,#t3@a1');

  // Switch context away from the broadcast (click-to-edit the chip, since an
  // active chip already owns routing — typing a new "#..." route as plain
  // text with a chip active is just literal message text, not a route
  // switch) so route-history recall below has something to recall *back to*.
  await page.locator('#topic-chip').click();
  await page.fill('#input', '#other@bob hi there');
  await page.locator('#input').press('Enter');
  await expect.poll(() => chatBodies.length).toBe(4);
  await expect(page.locator('#topic-chip')).not.toHaveClass(/origin-broadcast/);

  // Route-history recall (←, composerHasOnlyRoute + openRouteHistoryAutocomplete)
  // must reconstruct all three original heads from the reduced route text,
  // not just the ones still fully explicit in it — this is the actual point
  // of wiring parseOriginBroadcast into parseHistoryRouteTarget/applyRouteTarget:
  // previously they only understood a bare route or a single-hop chain, so a
  // broadcast entry was silently invisible to route-history recall.
  await page.fill('#input', '');
  await page.locator('#input').press('ArrowLeft');
  await expect(page.locator('#topic-chip')).toHaveClass(/origin-broadcast/);
  await expect(page.locator('#topic-chip')).toContainText('#t1@a2,#t4@a2,#t3@a1');

  // Re-sending from the recalled chip must dispatch to the same three
  // original targets, proving the recall didn't lose or corrupt a head.
  await page.fill('#input', 'go again');
  await page.locator('#input').press('Enter');
  await expect.poll(() => chatBodies.length).toBe(7);
  const targets = chatBodies.slice(4).map(b => `${b.topic}@${b.agent}`).sort();
  expect(targets).toEqual(['t1@a2', 't3@a1', 't4@a2']);
  expect(chatBodies.slice(4).every(b => b.flow_route === '#t1@a2,#t4,#t3@a1')).toBe(true);
});
