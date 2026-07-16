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
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '10' },
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
  await expect(page.locator('.route-chain-marker')).toHaveText('Squid Flow: #squid@codex!>@revucla!');
  await expect(page.locator('.route-chain-marker')).toHaveCount(1);
  await expect(page.locator('.msg-time + .route-chain-marker')).toBeVisible();
  expect(chatBodies[0]).toMatchObject({
    topic: 'squid',
    agent: 'codex',
    message: 'review this',
    adhoc: true,
    source: 'human',
  });
  expect(chatBodies[0].route).toBeUndefined();
  expect(chatBodies[1]).toMatchObject({
    topic: 'squid',
    agent: 'revucla',
    adhoc: true,
    source: 'system',
    pinned_ids: [10],
  });
  expect(chatBodies[1].message).toContain('Squid route chain handoff.');
  expect(chatBodies[1].message).toContain('Route: #squid@codex!>@revucla!');
  expect(chatBodies[1].message).toContain('<previous_step_output>');
  expect(chatBodies[1].message).not.toContain('codex response');
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex!>@revucla!');
  await expect(page.locator('.msg.user.user-system-generated .user-source-label')).toHaveText('SYSTEM');
  const handoffMetrics = await page.locator('.msg.user.user-system-generated').evaluate(el => ({
    maxHeight: parseFloat(getComputedStyle(el).maxHeight),
    clientHeight: el.clientHeight,
  }));
  expect(handoffMetrics.maxHeight).toBeGreaterThan(38);
  expect(handoffMetrics.clientHeight).toBeGreaterThan(20);
  await page.click('#pin-btn');
  await expect(page.locator('.pin-item-tag', { hasText: '<previous_step_output>' })).toBeVisible();
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
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '10' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":10,"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:${body.agent} response\n\nevent: done\ndata: \n\n`,
    });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@codex>@revucla review this');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex>@revucla');
  await expect(page.locator('#input')).toHaveValue('review this');
  await page.locator('#input').press('Enter');

  await expect.poll(() => chatBodies.length).toBe(2);
  await expect(page.locator('.route-chain-marker')).toHaveText('Squid Flow: #squid@codex>@revucla');
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
  await expect(page.locator('.msg-time + .route-chain-marker')).toBeVisible();

  releaseChat();
});
