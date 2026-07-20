const { test, expect } = require('@playwright/test');

test('composer sends only the origin turn for a one-way route chain; server-dispatched target renders when discovered', async ({ page }) => {
  // Route chain continuation (target/return handoffs) now runs server-side
  // (agent/flow.py) so it survives a refresh — the browser never POSTs those
  // steps itself. It only polls /chat/flow/{id}/steps to discover and render
  // them, same as any other message it didn't send. See ADR-0032.
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
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '10', 'X-Squid-Flow-Run-Id': '1' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":10,"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent} response\n\nevent: done\ndata: \n\n`,
    });
  });
  await page.route('**/chat/flow/1/steps**', r => r.fulfill({ json: {
    messages: [{ id: 11, role: 'assistant', topic: 'squid', agent: 'revucla', status: 'done' }],
    complete: true,
  } }));
  await page.route('**/chat/11/status', r => r.fulfill({ json: {
    id: 11, role: 'assistant', topic: 'squid', agent: 'revucla', adhoc: true, status: 'done',
    content: 'revucla response',
    prompt: 'Squid route chain handoff.\nRoute: #squid@codex!>@revucla!\nPrevious step: @codex\nCurrent step: @revucla!\nOriginal prompt: review this\n\n<previous_step_output>',
    prompt_source: 'system', session_id: null,
  } }));

  await page.goto('/');
  await page.fill('#input', '#squid@codex!>@revucla! review this');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex!>@revucla!');
  await expect(page.locator('#input')).toHaveValue('review this');
  await page.locator('#input').evaluate(el => el.setSelectionRange(0, 0));
  await page.locator('#input').press('Backspace');
  await expect(page.locator('#input')).toHaveValue('#squid@codex!>@revucla! review this');
  await page.fill('#input', '#squid@codex!>@revucla! review this');
  await expect(page.locator('#input')).toHaveValue('review this');
  await page.locator('#input').press('Enter');

  await expect(page.locator('.route-chain-marker')).toHaveText('#squid@codex!>@revucla!');
  await expect(page.locator('.route-chain-marker')).toHaveCount(1);
  await expect(page.locator('.route-chain-marker + .msg.user')).toBeVisible();
  await expect(page.locator('.route-chain-marker .tag-topic')).toHaveText('#squid');
  await expect(page.locator('.route-chain-marker .tag-agent')).toHaveText(['@codex', '@revucla']);
  await expect(page.locator('.route-chain-marker .tag-adhoc')).toHaveText(['!', '!']);
  await expect(page.locator('.route-chain-marker')).toHaveCSS('justify-content', 'flex-start');

  // Only the origin is ever POSTed — the target handoff is server-dispatched.
  await expect.poll(() => chatBodies.length).toBe(1);
  expect(chatBodies[0]).toMatchObject({
    topic: 'squid',
    agent: 'codex',
    message: 'review this',
    adhoc: true,
    source: 'human',
  });
  expect(chatBodies[0].route).toBeUndefined();
  expect(chatBodies[0].flow_route).toBe('#squid@codex!>@revucla!');
  expect(chatBodies[0].flow_run_id).toBeUndefined();

  // The server-dispatched target step is discovered via polling and rendered.
  await expect(page.locator('.msg[data-msg-id="11"]')).toBeVisible();
  await expect(page.locator('.msg[data-msg-id="11"] .tag-agent')).toHaveText('@revucla');
  await expect(page.locator('.msg[data-msg-id="11"]')).toContainText('revucla response');
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex!>@revucla!');
});

test('route-chain handoff live bubble appears after origin completes through status fallback', async ({ page }) => {
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

  await page.route('**/chat', r => r.fulfill({
    status: 200,
    headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '10', 'X-Squid-Flow-Run-Id': 'fallback-flow' },
    body: 'event: meta\ndata: {"agent":"codex","msg_id":10,"adhoc":true}\n\ndata:partial origin\n\n',
  }));
  await page.route('**/chat/10/status', r => r.fulfill({ json: {
    id: 10, role: 'assistant', topic: 'squid', agent: 'codex', adhoc: true, status: 'done',
    content: 'origin recovered',
    prompt: 'review this',
    prompt_source: 'human', session_id: null,
  } }));
  await page.route('**/chat/flow/fallback-flow/steps**', r => r.fulfill({ json: {
    messages: [{ id: 11, role: 'assistant', topic: 'squid', agent: 'revucla', status: 'pending' }],
    complete: false,
  } }));
  await page.route('**/chat/11/status', r => r.fulfill({ json: {
    id: 11, role: 'assistant', topic: 'squid', agent: 'revucla', adhoc: true, status: 'pending',
    content: '',
    prompt: 'Squid route chain handoff.\nRoute: #squid@codex!>@revucla!\nPrevious step: @codex\nCurrent step: @revucla!\nOriginal prompt: review this',
    prompt_source: 'system', session_id: null,
  } }));
  await page.route('**/chat/11/events', r => r.fulfill({
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
    body: '',
  }));

  await page.goto('/');
  await page.fill('#input', '#squid@codex!>@revucla! review this');
  await page.locator('#input').press('Enter');

  const handoffBubble = page.locator('.msg-thinking[data-msg-id="11"]');
  await expect(handoffBubble).toBeVisible();
  await expect(handoffBubble.locator('.tag-agent')).toHaveText('@revucla');
});

test('composer sends request-response route chain back to origin', async ({ page }) => {
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
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '20', 'X-Squid-Flow-Run-Id': 'rr1' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":20,"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent} response\n\nevent: done\ndata: \n\n`,
    });
  });
  // Target handoff (round 1) and return-to-origin are both dispatched
  // server-side; the mock reports both as already complete by the time the
  // client's first poll lands.
  await page.route('**/chat/flow/rr1/steps**', r => r.fulfill({ json: {
    messages: [
      { id: 21, role: 'assistant', topic: 'squid', agent: 'revucla', status: 'done' },
      { id: 22, role: 'assistant', topic: 'squid', agent: 'codex', status: 'done' },
    ],
    complete: true,
  } }));
  await page.route('**/chat/21/status', r => r.fulfill({ json: {
    id: 21, role: 'assistant', topic: 'squid', agent: 'revucla', adhoc: true, status: 'done',
    content: 'revucla response',
    prompt: 'Squid route chain handoff.\nRoute: #squid@codex<>@revucla!\nPrevious step: @codex\nCurrent step: @revucla!\nOriginal prompt: review this',
    prompt_source: 'system', session_id: null,
  } }));
  await page.route('**/chat/22/status', r => r.fulfill({ json: {
    id: 22, role: 'assistant', topic: 'squid', agent: 'codex', adhoc: false, status: 'done',
    content: 'codex final response',
    prompt: 'Squid route chain handoff.\nRoute: #squid@codex<>@revucla!\nPrevious step: @revucla\nCurrent step: @codex\nOriginal prompt: review this',
    prompt_source: 'system', session_id: null,
  } }));

  await page.goto('/');
  await page.fill('#input', '#squid@codex<>@revucla! review this');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex<>@revucla!');
  await expect(page.locator('#input')).toHaveValue('review this');
  await page.locator('#input').press('Enter');

  await expect(page.locator('.route-chain-marker')).toHaveText('#squid@codex<>@revucla!');
  await expect(page.locator('.route-chain-marker .route-chain-arrow')).toHaveText('<>');

  // Only the origin is ever POSTed — both the target and return-to-origin
  // handoffs are dispatched server-side (agent/flow.py).
  await expect.poll(() => chatBodies.length).toBe(1);
  expect(chatBodies[0]).toMatchObject({
    topic: 'squid',
    agent: 'codex',
    message: 'review this',
    adhoc: false,
    source: 'human',
    flow_route: '#squid@codex<>@revucla!',
  });
  expect(chatBodies[0].flow_run_id).toBeUndefined();

  // Both server-dispatched steps are discovered via polling and rendered.
  await expect(page.locator('.msg[data-msg-id="21"]')).toContainText('revucla response');
  await expect(page.locator('.msg[data-msg-id="22"]')).toContainText('codex final response');
});

test('composer sends simple one-way route chain with persistent target session', async ({ page }) => {
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
    const stats = body.agent === 'codex'
      ? 'event: stats\ndata: {"session_id":"origin-sid","adhoc":false,"session_turn_count":"8"}\n\n'
      : '';
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '10', 'X-Squid-Flow-Run-Id': '2' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":10,"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent} response\n\n${stats}event: done\ndata: \n\n`,
    });
  });
  await page.route('**/chat/flow/2/steps**', r => r.fulfill({ json: {
    messages: [{ id: 11, role: 'assistant', topic: 'squid', agent: 'revucla', status: 'done' }],
    complete: true,
  } }));
  await page.route('**/chat/11/status', r => r.fulfill({ json: {
    id: 11, role: 'assistant', topic: 'squid', agent: 'revucla', adhoc: false, status: 'done',
    content: 'revucla response',
    prompt: 'Squid route chain handoff.\nRoute: #squid@codex>@revucla\nPrevious step: @codex\nCurrent step: @revucla\nOriginal prompt: review this',
    prompt_source: 'system', session_id: 'target-sid',
  } }));

  await page.goto('/');
  await page.evaluate(() => {
    _sessionIds['squid@codex'] = 'origin-sid';
    _sessionTurnCounts['origin-sid'] = 7;
    _sessionIds['squid@revucla'] = 'target-sid';
    _sessionTurnCounts['target-sid'] = 18;
  });
  await page.fill('#input', '#squid@codex>@revucla review this');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex>@revucla');
  await expect(page.locator('#topic-chip .chip-turn-count')).toHaveCount(0);
  await expect(page.locator('#input')).toHaveValue('review this');
  await page.locator('#input').press('Enter');

  await expect(page.locator('.route-chain-marker')).toContainText('#squid@codex');
  await expect(page.locator('.route-chain-marker')).toContainText('@revucla');
  await expect(page.locator('.route-chain-marker .route-chain-turn-count')).toHaveText(['·7t', '·18t']);
  await expect(page.locator('.route-chain-marker')).toHaveCount(1);

  // Only the origin is ever POSTed — the target handoff (persistent lane) is
  // dispatched server-side.
  await expect.poll(() => chatBodies.length).toBe(1);
  expect(chatBodies[0]).toMatchObject({
    topic: 'squid',
    agent: 'codex',
    message: 'review this',
    adhoc: false,
    source: 'human',
  });
  expect(chatBodies[0].route).toBeUndefined();

  await expect(page.locator('.msg[data-msg-id="11"]')).toContainText('revucla response');
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex>@revucla');
  await expect(page.locator('#topic-chip .chip-turn-count')).toHaveCount(0);
});

test('route chain composer suppresses single-session clear advisory', async ({ page }) => {
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

  await page.route('**/chat', r => {
    const body = JSON.parse(r.request().postData() || '{}');
    const stats = body.agent === 'codex'
      ? 'event: stats\ndata: {"session_id":"origin-sid","adhoc":false,"session_turn_count":"18"}\n\n'
      : '';
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': body.agent === 'codex' ? '10' : '11' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":${body.agent === 'codex' ? 10 : 11},"adhoc":false}\n\ndata:${body.agent} response\n\n${stats}event: done\ndata: \n\n`,
    });
  });

  await page.goto('/');
  await page.evaluate(() => {
    _sessionIds['squid@codex'] = 'origin-sid';
    _sessionTurnCounts['origin-sid'] = 17;
  });
  await page.fill('#input', '#squid@codex>@revucla review this');
  await page.locator('#input').press('Enter');

  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip .chip-turn-count')).toHaveCount(0);
  await expect(page.locator('.route-chain-marker .route-chain-turn-count')).toContainText(['·17t']);
  await expect(page.locator('#session-advisory')).toBeHidden();
});

test('route chain marker shows known zero turn counts', async ({ page }) => {
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
  await page.route('**/chat', r => {
    const body = JSON.parse(r.request().postData() || '{}');
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '10', 'X-Squid-Flow-Run-Id': '2' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":10,"adhoc":false}\n\ndata:${body.agent} response\n\nevent: done\ndata: \n\n`,
    });
  });
  await page.route('**/chat/flow/2/steps**', r => r.fulfill({ json: { messages: [], complete: true } }));

  await page.goto('/');
  await page.evaluate(() => {
    _sessionIds['squid@codex'] = 'origin-sid';
    _sessionTurnCounts['origin-sid'] = 0;
    _sessionIds['squid@revucla'] = 'target-sid';
    _sessionTurnCounts['target-sid'] = 0;
  });
  await page.fill('#input', '#squid@codex>@revucla review this');
  await page.locator('#input').press('Enter');

  await expect(page.locator('.route-chain-marker .route-chain-turn-count')).toHaveText(['·0t', '·0t']);
});

test('/clear on a route chain clears each persistent route node', async ({ page }) => {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  const cmdBodies = [];
  await page.route('**/cmd', r => {
    const body = JSON.parse(r.request().postData() || '{}');
    cmdBodies.push(body);
    return r.fulfill({ json: { ok: true, agent: body.agent } });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@codex>#hive@review');
  await page.locator('#input').press('Enter');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await page.fill('#input', '/clear');
  await page.locator('#input').press('Enter');

  await expect.poll(() => cmdBodies).toEqual([
    { command: 'clear', topic: 'squid', agent: 'codex' },
    { command: 'clear', topic: 'hive', agent: 'review' },
  ]);
  await expect.poll(() => page.evaluate(() =>
    _routeChainTurnCounts('squid', 'codex', false, 'review', false, 'hive')
  )).toEqual({ origin: 0, target: 0 });
});

test('route chain marker follows live group filter visibility', async ({ page }) => {
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

  let releaseChat;
  const chatReady = new Promise(resolve => { releaseChat = resolve; });
  await page.route('**/chat', async r => {
    const body = JSON.parse(r.request().postData() || '{}');
    if (body.agent === 'codex') await chatReady;
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': body.agent === 'codex' ? '10' : '11' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":${body.agent === 'codex' ? 10 : 11},"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent} response\n\nevent: done\ndata: \n\n`,
    });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@codex!>@revucla! review this');
  await page.locator('#input').press('Enter');
  await expect(page.locator('.route-chain-marker')).toBeVisible();
  await expect(page.locator('.msg-thinking:not(.msg-thinking-done)')).toBeVisible();

  // Switching to a different route while a chip is active now requires an
  // explicit clear/edit (click-to-edit, backspace) rather than just typing a
  // new "#topic@agent" — the implicit override-by-typing that used to do
  // this was removed (it made ordinary messages starting with "#" silently
  // swap the active route). Clearing here stands in for that explicit action.
  await page.evaluate(() => clearTopicChip());
  await page.fill('#input', '#other@codex /filter');
  await page.locator('#input').press('Enter');
  await expect(page.locator('.route-chain-marker')).toHaveClass(/live-hidden/);

  await page.locator('#chip-filter-btn').click();
  await expect(page.locator('.route-chain-marker')).toBeVisible();
  await expect(page.locator('.route-chain-marker + .msg.user')).toBeVisible();

  releaseChat();
});

test('history shows route chain start marker', async ({ page }) => {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/history**', r => r.fulfill({
    json: {
      items: [
        {
          id: 11,
          role: 'assistant',
          topic: 'squid',
          agent: 'revucla',
          adhoc: 1,
          status: 'done',
          content: 'target response',
          prompt_source: 'system',
          prompt: [
            'Squid route chain handoff.',
            'Route: #squid@codex!>@revucla!',
            'Previous step: @codex',
            'Current step: @revucla!',
            'Original prompt: review this',
          ].join('\n'),
          timestamp: '2026-07-16T12:01:00Z',
        },
        {
          id: 10,
          role: 'assistant',
          topic: 'squid',
          agent: 'codex',
          adhoc: 1,
          status: 'done',
          content: 'origin response',
          prompt_source: 'human',
          prompt: 'review this',
          timestamp: '2026-07-16T12:00:00Z',
        },
      ],
      has_more: false,
    },
  }));

  await page.goto('/');

  const marker = page.locator('.route-chain-marker.history-item');
  await expect(marker).toHaveCount(1);
  await expect(marker).toHaveText('#squid@codex!>@revucla!');
  await expect(marker).not.toContainText('Squid Flow');
  await expect(marker.locator('.tag-topic')).toHaveText('#squid');
  await expect(marker.locator('.tag-agent')).toHaveText(['@codex', '@revucla']);
  await expect(marker.locator('.tag-adhoc')).toHaveText(['!', '!']);
  const order = await page.locator('#messages > *').evaluateAll(nodes => nodes.map(node => ({
    marker: node.classList.contains('route-chain-marker'),
    msgId: node.getAttribute('data-msg-id'),
  })));
  expect(order.findIndex(node => node.marker)).toBeLessThan(order.findIndex(node => node.msgId === '10'));
});

test('history does not show route chain start marker before target-only page', async ({ page }) => {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/history**', r => r.fulfill({
    json: {
      items: [{
        id: 11,
        role: 'assistant',
        topic: 'squid',
        agent: 'revucla',
        adhoc: 1,
        status: 'done',
        content: 'target response',
        prompt_source: 'system',
        prompt: [
          'Squid route chain handoff.',
          'Route: #squid@codex!>@revucla!',
          'Previous step: @codex',
          'Current step: @revucla!',
          'Original prompt: review this',
        ].join('\n'),
        timestamp: '2026-07-16T12:01:00Z',
      }],
      has_more: false,
    },
  }));

  await page.goto('/');

  await expect(page.locator('.route-chain-marker.history-item')).toHaveCount(0);
  await expect(page.locator('.msg[data-msg-id="11"]')).toBeVisible();
});

test('bare route chain with trailing whitespace parses from root, not the adhoc catch-all', async ({ page }) => {
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

  await page.goto('/');

  // Root route ends without "!" (session, not adhoc) even though the chain
  // text as a whole ends with "!" on the target — trailing whitespace with
  // no message yet used to make this fall through every parseInput branch
  // to the `{ topic: 'default', adhoc: true }` catch-all.
  const parsed = await page.evaluate(() => parseInput('#squid@deepseek>@revuqwen! '));
  expect(parsed).toMatchObject({
    topic: 'squid',
    agent: 'deepseek',
    adhoc: false,
    route: '#squid@deepseek>@revuqwen!',
    chainTarget: 'revuqwen',
    chainTargetFresh: true,
  });

  // Same route promoted through the composer must produce an identical chip,
  // with the input already cleared by the time it settles.
  await page.locator('#input').pressSequentially('#squid@deepseek>@revuqwen!', { delay: 0 });
  await page.locator('#input').pressSequentially(' ', { delay: 0 });
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).not.toHaveClass(/needs-agent/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@deepseek>@revuqwen!');
  await expect(page.locator('#topic-chip .chip-chain-origin-fresh')).toHaveCount(0);
  await expect(page.locator('#input')).toHaveValue('');
});

test('alias route chain highlights topic chip segments', async ({ page }) => {
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

  await page.goto('/');

  await page.fill('#input', '#squid@deepseek=>@revuqwen! review this');

  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@deepseek=>@revuqwen!');
  await expect(page.locator('#topic-chip .chip-topic')).toHaveText('#squid');
  await expect(page.locator('#topic-chip .chip-agent')).toHaveText(['@deepseek', '@revuqwen']);
  await expect(page.locator('#topic-chip .chip-route-arrow')).toHaveText('=>');
  await expect(page.locator('#topic-chip .chip-chain-fresh')).toHaveText('!');
  await expect(page.locator('#input')).toHaveValue('review this');
});

test('composer sends one-way route chain that hands off to a different topic', async ({ page }) => {
  // ADR-0032 "also targeted for v0.1": a chain target may be a full
  // #topic@agent, not just a bare @agent on the origin's own topic.
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
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '10', 'X-Squid-Flow-Run-Id': '3' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":10,"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent} response\n\nevent: done\ndata: \n\n`,
    });
  });
  await page.route('**/chat/flow/3/steps**', r => r.fulfill({ json: {
    messages: [{ id: 11, role: 'assistant', topic: 'hive', agent: 'revucla', status: 'done' }],
    complete: true,
  } }));
  await page.route('**/chat/11/status', r => r.fulfill({ json: {
    id: 11, role: 'assistant', topic: 'hive', agent: 'revucla', adhoc: true, status: 'done',
    content: 'revucla response',
    prompt: 'Squid route chain handoff.\nRoute: #squid@codex!>#hive@revucla!\nPrevious step: @codex\nCurrent step: @revucla!\nOriginal prompt: review this\n\n<previous_step_output>',
    prompt_source: 'system', session_id: null,
  } }));

  await page.goto('/');
  await page.fill('#input', '#squid@codex!>#hive@revucla! review this');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex!>#hive@revucla!');
  await expect(page.locator('#input')).toHaveValue('review this');
  await page.locator('#input').press('Enter');

  await expect(page.locator('.route-chain-marker')).toHaveText('#squid@codex!>#hive@revucla!');
  await expect(page.locator('.route-chain-marker .tag-topic')).toHaveText(['#squid', '#hive']);
  await expect(page.locator('.route-chain-marker .tag-agent')).toHaveText(['@codex', '@revucla']);

  // Only the origin is ever POSTed, on its own topic — the cross-topic target
  // handoff is dispatched server-side (agent/flow.py).
  await expect.poll(() => chatBodies.length).toBe(1);
  expect(chatBodies[0]).toMatchObject({
    topic: 'squid',
    agent: 'codex',
    message: 'review this',
    adhoc: true,
    source: 'human',
    flow_route: '#squid@codex!>#hive@revucla!',
  });

  // The server-dispatched target step is discovered via polling and rendered.
  await expect(page.locator('.msg[data-msg-id="11"]')).toBeVisible();
  await expect(page.locator('.msg[data-msg-id="11"] .tag-agent')).toHaveText('@revucla');
  await expect(page.locator('.msg[data-msg-id="11"]')).toContainText('revucla response');
});

test('composer accepts bare-topic route chain target and saves reduced route', async ({ page }) => {
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
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '30', 'X-Squid-Flow-Run-Id': 'bare-topic' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":30,"adhoc":false}\n\ndata:${body.agent} response\n\nevent: done\ndata: \n\n`,
    });
  });
  await page.route('**/chat/flow/bare-topic/steps**', r => r.fulfill({ json: {
    messages: [{ id: 31, role: 'assistant', topic: 'hive', agent: 'codex', status: 'done' }],
    complete: true,
  } }));
  await page.route('**/chat/31/status', r => r.fulfill({ json: {
    id: 31, role: 'assistant', topic: 'hive', agent: 'codex', adhoc: false, status: 'done',
    content: 'hive codex response',
    prompt: 'Squid route chain handoff.\nRoute: #squid@codex>#hive\nPrevious step: @codex\nCurrent step: @codex\nOriginal prompt: continue there',
    prompt_source: 'system', session_id: null,
  } }));

  await page.goto('/');
  await page.fill('#input', '#squid@codex>#hive continue there');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex>#hive');
  await page.locator('#input').press('Enter');

  await expect.poll(() => chatBodies.length).toBe(1);
  expect(chatBodies[0]).toMatchObject({
    topic: 'squid',
    agent: 'codex',
    flow_route: '#squid@codex>#hive',
    message: 'continue there',
  });
});

test('composer starts join origins under one reduced flow route', async ({ page }) => {
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
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': String(40 + chatBodies.length), 'X-Squid-Flow-Run-Id': 'join1' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":${40 + chatBodies.length},"adhoc":false}\n\ndata:${body.agent} response\n\nevent: done\ndata: \n\n`,
    });
  });
  await page.route('**/chat/flow/join1/steps**', r => r.fulfill({ json: {
    messages: [],
    complete: true,
  } }));

  await page.goto('/');
  await page.fill('#input', '#squid@a+@b>@c compare');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toHaveText('#squid@a+@b>@c');
  await page.locator('#input').press('Enter');

  await expect.poll(() => chatBodies.length).toBe(2);
  await expect(page.locator('.msg.user .topic-tag')).toHaveText('#squid@a+@b>@c');
  await expect(page.locator('.route-chain-marker')).toHaveText(['#squid@a>@c', '#squid@b>@c']);
  expect(chatBodies.map(b => b.agent)).toEqual(['a', 'b']);
  expect(chatBodies.every(b => b.topic === 'squid')).toBe(true);
  expect(chatBodies.every(b => b.message === 'compare')).toBe(true);
  expect(chatBodies.every(b => b.flow_route === '#squid@a+@b>@c')).toBe(true);
  expect(chatBodies[0].flow_run_id).toBeUndefined();
  expect(chatBodies[1].flow_run_id).toBe('join1');
});

test('complex comma-head route chain markers keep agent colors', async ({ page }) => {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic: 'debug',
    content: '',
    revision: 'r1',
    exists: false,
  } }));

  const chatBodies = [];
  await page.route('**/chat', r => {
    const body = JSON.parse(r.request().postData() || '{}');
    chatBodies.push(body);
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': String(50 + chatBodies.length), 'X-Squid-Flow-Run-Id': 'comma-heads' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":${50 + chatBodies.length},"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent} response\n\nevent: done\ndata: \n\n`,
    });
  });
  await page.route('**/chat/flow/comma-heads/steps**', r => r.fulfill({ json: {
    messages: [],
    complete: true,
  } }));

  await page.goto('/');
  await page.evaluate(() => {
    _agentsCache = [{ name: 'qwen', backend: 'claudecode:qwen', color: '#4D9DE0' }];
  });
  await page.fill('#input', '#debug@qwen,@qwen!>#squid compare');
  await page.locator('#input').press('Enter');

  await expect.poll(() => chatBodies.length).toBe(2);
  const marker = page.locator('.route-chain-marker').first();
  await expect(marker).toContainText('#debug@qwen,@qwen!>#squid');
  await expect(marker.locator('.tag-agent').first()).toHaveCSS('color', 'rgb(77, 157, 224)');
  await expect(marker).toContainText('#debug');
  await expect(marker).toContainText('#squid');
});

test('composer accepts repeated and delayed single-operator flow routes', async ({ page }) => {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));

  await page.goto('/');

  const repeated = await page.evaluate(() => parseInput('#squid@codex<2>@review repeat me'));
  expect(repeated).toMatchObject({
    topic: 'squid',
    agent: 'codex',
    route: '#squid@codex<2>@review',
    chainTarget: 'review',
    chainRounds: 2,
    message: 'repeat me',
  });

  const delayed = await page.evaluate(() => parseInput('#squid@codex=2:1s>@review delay me'));
  expect(delayed).toMatchObject({
    topic: 'squid',
    agent: 'codex',
    route: '#squid@codex=2:1s>@review',
    chainTarget: 'review',
    message: 'delay me',
  });
});

test('history shows route chain start marker on origin row with inferred flow route', async ({ page }) => {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/history**', r => r.fulfill({
    json: {
      items: [{
        id: 10,
        role: 'assistant',
        topic: 'squid',
        agent: 'codex',
        adhoc: 1,
        status: 'done',
        content: 'origin response',
        prompt_source: 'human',
        prompt: 'review this',
        flow_route: '#squid@codex!>@revucla!',
        timestamp: '2026-07-16T12:00:00Z',
      }],
      has_more: false,
    },
  }));

  await page.goto('/');

  const marker = page.locator('.route-chain-marker.history-item');
  await expect(marker).toHaveCount(1);
  await expect(marker).toHaveText('#squid@codex!>@revucla!');
  const order = await page.locator('#messages > *').evaluateAll(nodes => nodes.map(node => ({
    marker: node.classList.contains('route-chain-marker'),
    msgId: node.getAttribute('data-msg-id'),
  })));
  expect(order.findIndex(node => node.marker)).toBeLessThan(order.findIndex(node => node.msgId === '10'));
});
