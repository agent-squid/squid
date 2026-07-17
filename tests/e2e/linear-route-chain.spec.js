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
