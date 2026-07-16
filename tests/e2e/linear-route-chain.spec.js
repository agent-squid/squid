const { test, expect } = require('@playwright/test');

test('composer sends simple one-way route chain to chat', async ({ page }) => {
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
    const stats = body.agent === 'revucla'
      ? 'event: stats\ndata: {"session_id":"target-sid","adhoc":false,"session_turn_count":"18"}\n\n'
      : '';
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '10', 'X-Squid-Flow-Run-Id': body.flow_run_id || '1' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":10,"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent} response\n\n${stats}event: done\ndata: \n\n`,
    });
  });

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

  await expect.poll(() => chatBodies.length).toBe(2);
  await expect(page.locator('.route-chain-marker')).toHaveText('#squid@codex!>@revucla!');
  await expect(page.locator('.route-chain-marker')).toHaveCount(1);
  await expect(page.locator('.route-chain-marker + .msg.user')).toBeVisible();
  await expect(page.locator('.route-chain-marker .tag-topic')).toHaveText('#squid');
  await expect(page.locator('.route-chain-marker .tag-agent')).toHaveText(['@codex', '@revucla']);
  await expect(page.locator('.route-chain-marker .tag-adhoc')).toHaveText(['!', '!']);
  await expect(page.locator('.route-chain-marker')).toHaveCSS('justify-content', 'flex-start');
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
  expect(chatBodies[1]).toMatchObject({
    topic: 'squid',
    agent: 'revucla',
    adhoc: true,
    source: 'system',
    pinned_ids: [10],
    flow_route: '#squid@codex!>@revucla!',
    flow_run_id: '1',
  });
  expect(chatBodies[1].message).toContain('Squid route chain handoff.');
  expect(chatBodies[1].message).toContain('Route: #squid@codex!>@revucla!');
  expect(chatBodies[1].message).toContain('<previous_step_output>');
  expect(chatBodies[1].message).not.toContain('codex response');
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex!>@revucla!');
  await expect(page.locator('.msg.user.user-system-generated .user-source-label')).toHaveText('SYSTEM');
  await expect(page.locator('.msg.user.user-system-generated')).toHaveCSS('border-top-color', 'rgb(42, 42, 53)');
  const handoffMetrics = await page.locator('.msg.user.user-system-generated').evaluate(el => ({
    maxHeight: parseFloat(getComputedStyle(el).maxHeight),
    clientHeight: el.clientHeight,
  }));
  expect(handoffMetrics.maxHeight).toBeGreaterThan(70);
  expect(handoffMetrics.clientHeight).toBeGreaterThan(20);
  await page.click('#pin-btn');
  await expect(page.locator('.pin-item-tag', { hasText: '<previous_step_output>' })).toHaveCount(0);
  await expect(page.locator('.pin-item')).toHaveCount(0);
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
  let msgId = 20;
  await page.route('**/chat', r => {
    const body = JSON.parse(r.request().postData() || '{}');
    chatBodies.push(body);
    msgId += 1;
    return r.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream',
        'X-Squid-Msg-Id': String(msgId),
        'X-Squid-Flow-Run-Id': body.flow_run_id || 'rr1',
      },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":${msgId},"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent} response\n\nevent: done\ndata: \n\n`,
    });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@codex<>@revucla! review this');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex<>@revucla!');
  await expect(page.locator('#input')).toHaveValue('review this');
  await page.locator('#input').press('Enter');

  await expect.poll(() => chatBodies.length).toBe(3);
  await expect(page.locator('.route-chain-marker')).toHaveText('#squid@codex<>@revucla!');
  await expect(page.locator('.route-chain-marker .route-chain-arrow')).toHaveText('<>');
  await expect(page.locator('.msg.user.user-system-generated')).toHaveCount(2);

  expect(chatBodies[0]).toMatchObject({
    topic: 'squid',
    agent: 'codex',
    message: 'review this',
    adhoc: false,
    source: 'human',
    flow_route: '#squid@codex<>@revucla!',
  });
  expect(chatBodies[0].flow_run_id).toBeUndefined();
  expect(chatBodies[1]).toMatchObject({
    topic: 'squid',
    agent: 'revucla',
    adhoc: true,
    source: 'system',
    pinned_ids: [21],
    flow_route: '#squid@codex<>@revucla!',
    flow_run_id: 'rr1',
  });
  expect(chatBodies[1].message).toContain('Current step: @revucla!');
  expect(chatBodies[1].message).toContain('Original prompt: review this');
  expect(chatBodies[2]).toMatchObject({
    topic: 'squid',
    agent: 'codex',
    adhoc: false,
    source: 'system',
    pinned_ids: [22],
    flow_route: '#squid@codex<>@revucla!',
    flow_run_id: 'rr1',
  });
  expect(chatBodies[2].message).toContain('Previous step: @revucla');
  expect(chatBodies[2].message).toContain('Current step: @codex');
  expect(chatBodies[2].message).toContain('Original prompt: review this');
  expect(chatBodies[2].message).not.toContain('revucla response');
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
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '10', 'X-Squid-Flow-Run-Id': body.flow_run_id || '2' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":10,"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent} response\n\n${stats}event: done\ndata: \n\n`,
    });
  });

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

  await expect.poll(() => chatBodies.length).toBe(2);
  await expect(page.locator('.route-chain-marker')).toContainText('#squid@codex');
  await expect(page.locator('.route-chain-marker')).toContainText('@revucla');
  await expect(page.locator('.route-chain-marker .route-chain-turn-count')).toHaveText(['·7t', '·18t']);
  await expect(page.locator('.route-chain-marker')).toHaveCount(1);
  expect(chatBodies[0]).toMatchObject({
    topic: 'squid',
    agent: 'codex',
    message: 'review this',
    adhoc: false,
    source: 'human',
  });
  expect(chatBodies[0].route).toBeUndefined();
  expect(chatBodies[1]).toMatchObject({
    topic: 'squid',
    agent: 'revucla',
    adhoc: false,
    source: 'system',
    pinned_ids: [10],
  });
  expect(chatBodies[1].message).toContain('Squid route chain handoff.');
  expect(chatBodies[1].message).toContain('Route: #squid@codex>@revucla');
  expect(chatBodies[1].message).toContain('<previous_step_output>');
  expect(chatBodies[1].message).not.toContain('codex response');
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
