const { test, expect } = require('@playwright/test');

async function mockBackend(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    '#other@codex push the changes',
    '#squid@haiku! push the changes',
    '#squid@codex push the changes',
    '#squid@codex inspect the changes',
  ] } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
}

test('typed prompt prefixes show unique routed history with the current route first', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'push');

  const items = page.locator('#autocomplete .ac-item');
  await expect(items).toHaveCount(3);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Recent Prompts');
  const currentRouteItem = page.locator('#autocomplete .ac-item', { hasText: 'push the changes #squid@codex' });
  const olderRouteItem = page.locator('#autocomplete .ac-item', { hasText: 'push the changes #squid@haiku!' });
  await expect(currentRouteItem).toBeVisible();
  await expect(page.locator('#autocomplete .ac-item', { hasText: 'push the changes #other@codex' })).toBeVisible();
  await expect(olderRouteItem).toBeVisible();
  await expect.poll(async () => {
    const currentBox = await currentRouteItem.boundingBox();
    const olderBox = await olderRouteItem.boundingBox();
    return currentBox && olderBox ? currentBox.y > olderBox.y : false;
  }).toBe(true);
  await expect(page.locator('#autocomplete .ac-item.selected .ac-route-btn')).toContainText('#squid@codex');
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('push the changes');
  await page.locator('#input').press('ArrowUp');
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('push the changes #other@codex');
  await page.locator('#input').press('ArrowDown');
  await expect(page.locator('#autocomplete .ac-item.selected .ac-route-btn')).toContainText('#squid@codex');
  await expect(page.getByRole('button', { name: 'Close suggestions' })).toBeVisible();
  await expect.poll(async () => {
    const titleBox = await page.locator('#autocomplete .ac-title').boundingBox();
    const closeBox = await page.getByRole('button', { name: 'Close suggestions' }).boundingBox();
    return titleBox && closeBox ? closeBox.x > titleBox.x : false;
  }).toBe(true);

  await olderRouteItem.locator('.ac-route-btn').click();
  await expect(page.locator('#input')).toHaveValue('push the changes');
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@haiku!');
});

test('every routed item shows its route chip, including the current route', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'push');

  await expect(page.locator('#autocomplete .ac-item')).toHaveCount(3);
  await expect(page.locator('#autocomplete .ac-item:not(:has(.ac-route-btn))')).toHaveCount(0);
  for (const route of ['#squid@codex', '#other@codex', '#squid@haiku!']) {
    await expect(page.locator('#autocomplete .ac-item', { hasText: `push the changes ${route}` }).locator('.ac-route-btn')).toContainText(route);
  }
});

test('arrowing through recent prompts preserves the current route', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/prompts/recent**');
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    '#squid@codex! what is your model?',
    'review before publish',
    '#squid@claude! what is your model?',
    '#squid@codex address the issues',
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  const chip = page.locator('#topic-chip');
  const selected = page.locator('#autocomplete .ac-item.selected');
  const prompts = ['what is your model?', 'review before publish', 'what is your model?', 'address the issues'];
  for (const prompt of prompts) {
    await composer.press('ArrowUp');
    await expect(selected).toContainText(prompt);
    await expect(composer).toHaveValue(prompt);
    await expect(chip).toHaveText('#squid@codex');
  }
  await composer.press('ArrowDown');
  await expect(selected.locator('.ac-route-btn')).toContainText('#squid@claude!');
  await expect(chip).toHaveText('#squid@codex');
});

test('resubmitting a removed prompt restores it to autocomplete', async ({ page }) => {
  await mockBackend(page);
  const entry = '#squid@haiku! push the changes';
  await page.addInitScript(({ entry }) => {
    localStorage.setItem('squid_hidden_prompts', JSON.stringify([`${entry}\u0000unused`]));
  }, { entry });
  await page.goto('/');

  const key = await page.evaluate(entry => promptHistoryDedupKey(entry), entry);
  await page.evaluate(({ entry, key }) => {
    hiddenPromptKeys = new Set([key]);
    localStorage.setItem('squid_hidden_prompts', JSON.stringify([key]));
    recordPrompt(entry);
  }, { entry, key });
  await page.fill('#input', 'push');

  await expect(page.locator('#autocomplete .ac-item', { hasText: 'push the changes #squid@haiku!' })).toBeVisible();
  await expect.poll(() => page.evaluate(() => JSON.parse(localStorage.getItem('squid_hidden_prompts') || '[]'))).toEqual([]);
});

test('route agent tags use provider color from recent prompt metadata', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/prompts/recent**');
  await page.route('**/prompts/recent**', r => r.fulfill({ json: {
    items: ['#squid@haiku! push the changes'],
    agents: {
      haiku: {
        name: 'haiku',
        harness: 'claudecode',
        provider: 'deepseek',
        backend: 'claudecode:deepseek',
        color: '#4D9DE0',
        provider_color: '#4D9DE0',
      },
    },
  } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'push');

  await expect(page.locator('#autocomplete .ac-route-btn .ac-agent')).toHaveCSS('color', 'rgb(77, 157, 224)');
});

test('flow-backed prompt history displays the submitted origin route', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/prompts/recent**');
  await page.route('**/prompts/recent**', r => r.fulfill({ json: {
    items: ['#squid@opencode!>@review summarize'],
  } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'sum');

  const item = page.locator('#autocomplete .ac-item', { hasText: 'summarize #squid@opencode!' });
  await expect(item).toBeVisible();
  await expect(item).not.toContainText('>@review');
  await expect(item.locator('.ac-route-switch-icon')).toHaveCount(1);
});

test('route and prompt history use the same typography and row height', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await expect.poll(() => page.evaluate(() => promptHistoryLoaded)).toBe(true);
  for (const width of [390, 1280]) {
    await page.setViewportSize({ width, height: 800 });
    const { metrics: [prompt, route, routeWithoutPrompt], firstElements } = await page.evaluate(() => {
      const entry = '#other@codex push the changes';
      const [promptItem] = promptHistoryAutocompleteItems([entry]);
      const routes = routeHistoryAutocompleteItems('#unused@codex');
      const routeItem = routes.find(item => item.routeTarget.topic === 'other');
      _acRender([promptItem, routeItem, routes[0]]);
      const rows = Array.from(acEl.querySelectorAll('.ac-row')).reverse();
      return {
        metrics: rows.map(row => ({
          height: row.parentElement.getBoundingClientRect().height,
          labelSize: getComputedStyle(row.querySelector('.ac-label')).fontSize,
          chipSize: getComputedStyle(row.querySelector('.ac-route-btn')).fontSize,
        })),
        firstElements: rows.map(row => row.firstElementChild.tagName),
      };
    });
    expect(firstElements).toEqual(['SPAN', 'BUTTON', 'BUTTON']);
    expect(route).toEqual(prompt);
    expect(routeWithoutPrompt).toEqual(prompt);
  }
});

test('left and right browse recent routes from an empty composer', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.focus();
  await composer.press('ArrowLeft');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('#other@codex');
  await expect(page.locator('#autocomplete .ac-route-switch-icon')).toHaveCount(0);
  await expect(page.locator('#topic-chip')).toContainText('#other@codex');
  await expect(composer).toHaveValue('');

  await composer.press('ArrowUp');
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('#squid@haiku!');
  await expect(page.locator('#topic-chip')).toContainText('#squid@haiku!');

  await composer.press('ArrowDown');
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('#other@codex');
  await expect(page.locator('#topic-chip')).toContainText('#other@codex');

  await composer.press('ArrowLeft');
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('#squid@haiku!');
  await expect(page.locator('#topic-chip')).toContainText('#squid@haiku!');

  await composer.press('ArrowRight');
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('#other@codex');
  await expect(page.locator('#topic-chip')).toContainText('#other@codex');

  await composer.press('Escape');
  await expect(page.locator('#autocomplete')).not.toHaveClass(/open/);
  await expect(page.locator('#topic-chip')).toContainText('#other@codex');
  await expect(composer).toHaveValue('');
  await expect.poll(() => page.evaluate(() => JSON.parse(localStorage.getItem('squid_sticky_chip')).topic)).toBe('other');
});

test('route history includes older database routes alongside saved and recent routes', async ({ page }) => {
  await mockBackend(page);
  const time = Date.parse('2026-11-23T00:00:00Z');
  const entries = [
    { text: '#squid@claude check the history', created_at: new Date(time + 1000).toISOString() },
  ];
  const savedRoutes = ['#squid@codex', ...Array.from({ length: 9 }, (_, i) => `#older${i}@codex`)];
  await page.route('**/prompts/recent**', r => r.fulfill({ json: {
    items: entries.map(entry => entry.text),
    entries,
    routes: [
      { route: '#squid@haiku!', prompt: 'older ad hoc prompt', created_at: new Date(time - 1000).toISOString() },
      { route: '#hidden@haiku', prompt: 'hidden route prompt', created_at: new Date(time - 1000).toISOString() },
    ],
    history: [
      ...savedRoutes.map(value => ({ kind: 'route', key: value, value, hidden: false, updated_at: time })),
      { kind: 'route', key: '#hidden@haiku', value: '', hidden: true, updated_at: time },
    ],
  } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');
  await expect.poll(() => page.evaluate(() => promptHistoryLoaded)).toBe(true);
  await page.locator('#input').press('ArrowLeft');
  const rows = page.locator('#autocomplete .ac-item');
  await expect(rows).toHaveCount(12);
  await expect(rows.filter({ hasText: '#squid@claude' })).toHaveClass(/selected/);
  await expect(rows.filter({ hasText: '#squid@claude' })).toContainText('check the history');
  await expect(rows.filter({ hasText: '#squid@codex' })).toHaveCount(1);
  const olderRoute = rows.filter({ hasText: '#squid@haiku!' });
  await expect(olderRoute).toContainText('older ad hoc prompt');
  await expect(rows.filter({ hasText: '#hidden@haiku' })).toHaveCount(0);

  await page.locator('#input').press('Escape');
  await page.locator('#topic-chip').click();
  await expect(rows).toHaveCount(12);
  await page.locator('#input').press('Tab');
  await page.locator('#input').press('Backspace');
  await expect(rows).toHaveCount(12);
});

test('deleting a recent route persists without removing its prompts', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  const rows = page.locator('#autocomplete .ac-item');
  const removed = rows.filter({ hasText: '#squid@haiku!' });
  const other = rows.filter({ hasText: '#other@codex' });
  await composer.press('ArrowLeft');
  await expect(other).toHaveClass(/selected/);
  await removed.getByRole('button', { name: 'Remove from history' }).click();
  await expect(removed).toHaveCount(0);
  await expect(other).toHaveClass(/selected/);

  await page.reload();
  await composer.press('ArrowLeft');
  await expect(other).toBeVisible();
  await expect(removed).toHaveCount(0);
  await composer.press('Escape');
  await composer.fill('push');
  await expect(removed).toContainText('push the changes');
});

test('server history refresh updates the open route picker and preserves the draft', async ({ page }) => {
  await mockBackend(page);
  const items = ['#older@codex older prompt'];
  const history = [];
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items, history } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');
  await page.waitForFunction(() => typeof promptHistoryLoaded !== 'undefined' && promptHistoryLoaded);

  const composer = page.locator('#input');
  const chip = page.locator('#topic-chip');
  const picker = page.locator('#autocomplete');
  const selected = picker.locator('.selected .ac-route-btn');
  await composer.fill('keep this draft');
  await chip.click();
  await composer.press('ArrowUp');
  await expect(selected).toHaveText('#older@codex');

  items.unshift('#newer@codex newer prompt');
  await page.evaluate(() => loadPromptHistory());
  await expect(picker.locator('.ac-route-btn', { hasText: '#newer@codex' })).toBeVisible();
  await expect(selected).toHaveText('#older@codex');

  history.push({ kind: 'route', key: '#older@codex', value: '', hidden: true, updated_at: Date.now() });
  await page.evaluate(() => loadPromptHistory());
  await expect(picker.locator('.ac-route-btn', { hasText: '#older@codex' })).toHaveCount(0);
  await expect(selected).toHaveText('#squid@codex');
  await expect(composer).toHaveValue('#squid@codex keep this draft');
  await composer.press('Enter');
  await expect(chip).toContainText('#squid@codex');
  await expect(composer).toHaveValue('keep this draft');

  await composer.fill('');
  await composer.press('Escape');
  await composer.press('ArrowLeft');
  await expect(chip).toContainText('#newer@codex');
  history.push({ kind: 'route', key: '#newer@codex', value: '', hidden: true, updated_at: Date.now() });
  await page.evaluate(() => loadPromptHistory());
  await expect(picker.locator('.ac-route-btn', { hasText: '#newer@codex' })).toHaveCount(0);
  await expect(chip).toContainText('#squid@codex');
});

test('left opens route autocomplete when the composer contains only a route', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('#squid@codex');
  await composer.press('ArrowLeft');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('#other@codex');
  await expect(page.locator('#topic-chip')).toContainText('#other@codex');
  await expect(composer).toHaveValue('');
});

test('route autocomplete includes multi-head flow routes from prompt history', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/prompts/recent**');
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@a+@b>@revuopen compare heads',
    '#other@codex older prompt',
  ] } }));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.focus();
  await composer.press('ArrowLeft');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  const multiHeadItem = page.locator('#autocomplete .ac-item', { hasText: '#squid@a+@b>@revuopen' });
  await expect(multiHeadItem).toBeVisible();
  await expect(multiHeadItem.locator('.ac-route-btn')).toBeVisible();
});

test('route autocomplete opens for a single duplicate-head broadcast route', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/prompts/recent**');
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@qwen!,@qwen! compare twice',
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('#squid@qwen!,@qwen!');
  await composer.press('ArrowLeft');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  await expect(page.locator('#autocomplete .ac-item.selected', { hasText: '#squid@qwen!,@qwen!' })).toBeVisible();
});

test('complex repeated-agent flow routes keep agent colors in chip and autocomplete', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/config/agents');
  await page.route('**/config/agents', route => route.fulfill({ json: [
    { name: 'qwen', backend: 'claudecode:qwen', provider: 'qwen', provider_color: '#4D9DE0' },
  ] }));
  await page.unroute('**/prompts/recent**');
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#debug@qwen,@qwen!>#squid investigate colors',
  ] } }));
  await page.goto('/');
  await page.evaluate(() => _acAgents());
  await page.evaluate(() => clearTopicChip());

  const composer = page.locator('#input');
  await composer.fill('#debug@qwen,@qwen!>#squid');
  await composer.press(' ');

  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip .chip-agent').first()).toHaveCSS('color', 'rgb(77, 157, 224)');
  await expect(page.locator('#topic-chip')).toContainText('#debug');
  await expect(page.locator('#topic-chip')).toContainText('#squid');

  await composer.focus();
  await composer.press('ArrowLeft');

  const routeItem = page.locator('#autocomplete .ac-item', { hasText: '#debug@qwen,@qwen!>#squid' });
  await expect(routeItem).toBeVisible();
  await expect(routeItem.locator('.ac-route-btn .ac-agent').first()).toHaveCSS('color', 'rgb(77, 157, 224)');
  await expect(routeItem.locator('.ac-route-btn')).toContainText('#debug');
  await expect(routeItem.locator('.ac-route-btn')).toContainText('#squid');
});

test('route autocomplete opens for a non-fresh duplicate-head broadcast route', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/prompts/recent**');
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@qwen,@qwen say hi',
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.focus();
  await composer.press('ArrowLeft');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  await expect(page.locator('#autocomplete .ac-item.selected', { hasText: '#squid@qwen,@qwen' })).toBeVisible();

  await composer.fill('#squid@qwen,@qwen');
  await composer.press('ArrowLeft');
  await expect(page.locator('#autocomplete .ac-item.selected', { hasText: '#squid@qwen,@qwen' })).toBeVisible();
});

test('broadcast route autocomplete opens immediately after comma', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/config/agents', route => route.fulfill({ json: [
    { name: 'qwen', backend: 'codex' },
    { name: 'claude', backend: 'claudecode:anthropic' },
  ] }));
  await page.route('**/topics/squid/agents/history', route => route.fulfill({ json: [
    { agent: 'claude', last_prompt: 'compare prompt', last_adhoc_prompt: 'fresh compare prompt' },
  ] }));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('#squid@qwen,');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  await expect(page.locator('#autocomplete .ac-item', { hasText: '#squid@qwen,@claude!' })).toBeVisible();

  await page.locator('#autocomplete .ac-item', { hasText: '#squid@qwen,@claude!' }).click();

  await expect(composer).toHaveValue('#squid@qwen,@claude! ');
});

test('route autocomplete includes saved flows matching an agent route prefix', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/prompts/recent**');
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@qwen,@qwen! compare twice',
  ] } }));
  await page.route('**/config/agents', route => route.fulfill({ json: [
    { name: 'qwen', backend: 'codex' },
  ] }));
  await page.route('**/topics/squid/agents/history', route => route.fulfill({ json: [] }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'qwen', adhoc: false, lookback: 0,
  })));
  await page.goto('/');
  await expect.poll(() => page.evaluate(() => promptHistoryLoaded)).toBe(true);

  const composer = page.locator('#input');
  await page.locator('#topic-chip').click();
  await composer.press('Backspace');
  await composer.pressSequentially('qwen');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('#squid@qwen');
  await expect(page.locator('#autocomplete .ac-item.selected')).not.toContainText(',');
  await expect(page.locator('#autocomplete .ac-item', { hasText: '#squid@qwen,@qwen!' })).toBeVisible();
});

test('broadcast route autocomplete keeps saved repeated-agent flows visible after comma', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/prompts/recent**');
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#conflict@claude,@claude! compare twice',
    '#conflict@claude,@codex compare other',
  ] } }));
  await page.unroute('**/config/agents');
  await page.route('**/config/agents', route => route.fulfill({ json: [
    'alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel'
  ].map(name => ({ name, backend: 'codex' })) }));
  await page.route('**/topics/conflict/agents/history', route => route.fulfill({ json: [
    { agent: 'claude', last_prompt: 'Changed FOOT to random number string', last_adhoc_prompt: 'Changed FOOT to random number string' },
  ] }));
  await page.goto('/');
  await page.evaluate(() => clearTopicChip());

  const composer = page.locator('#input');
  await composer.fill('#conflict@claude,');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  const savedFlow = page.locator('#autocomplete .ac-item', { hasText: '#conflict@claude,@claude!' });
  await expect(savedFlow).toBeVisible();
  await expect(page.locator('#autocomplete .ac-item', { hasText: /#conflict@claude,@claude(?![!])/ })).toHaveCount(0);
  await expect(page.locator('#autocomplete .ac-item', { hasText: '#conflict@alpha!' })).toHaveCount(0);
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('#conflict@claude,@claude!');

  await composer.press('ArrowUp');
  await expect(page.locator('#autocomplete .ac-item.selected')).not.toContainText('#conflict@claude,@claude!');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);

  await composer.press('ArrowDown');
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('#conflict@claude,@claude!');
  await expect(composer).toHaveValue('#conflict@claude,@claude!');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);

  await composer.press('ArrowUp');
  await expect(page.locator('#autocomplete .ac-item.selected')).not.toContainText('#conflict@claude,@claude!');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
});

test('route autocomplete keeps saved flows visible after a matching chain operator', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/prompts/recent**');
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@qwen>@revucla! review this',
  ] } }));
  await page.route('**/config/agents', route => route.fulfill({ json: [] }));
  await page.route('**/topics/squid/agents/history', route => route.fulfill({ json: [] }));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('#squid@qwen>');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  await expect(page.locator('#autocomplete .ac-item', { hasText: '#squid@qwen>@revucla!' })).toBeVisible();
});

test('help menu documents route history arrow shortcuts', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.locator('#help-btn').click();

  await expect(page.locator('#help-panel')).toHaveClass(/open/);
  await expect(page.locator('#help-content')).toContainText('with empty input or only a route: browse recent routes');
  await expect(page.locator('#help-content')).toContainText('navigate autocomplete, route history, or prompt history');
  await expect(page.locator('#help-content')).toContainText('/bookmarks, /bm');
  await expect(page.locator('#help-content')).toContainText('toggle to show only bookmarked responses');
  await expect(page.locator('#help-content')).toContainText('/prompts');
  await expect(page.locator('#help-content')).toContainText('toggle to show only your prompts in history');
});

test('default topic prompt history distinguishes agent routes', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#default@agentA compare models',
    '#default@agentB compare models',
    'compare bare default',
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'default', agent: 'agentB', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'compare');

  const sameRouteItem = page.locator('#autocomplete .ac-item', { hasText: 'compare models #default@agentB' });
  await expect(sameRouteItem.locator('.ac-route-btn')).toContainText('#default@agentB');

  const otherRouteItem = page.locator('#autocomplete .ac-item', { hasText: 'compare models #default@agentA' });
  await expect(otherRouteItem.locator('.ac-route-btn')).toBeVisible();
  await expect(otherRouteItem.locator('.ac-route-btn')).toContainText('#default@agentA');
});

test('adhoc prompt history route chips ignore lookback counts', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@codex!2 reuse context',
    '#squid@haiku!3 reuse context',
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: true, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'reuse');

  const sameRouteItem = page.locator('#autocomplete .ac-item', { hasText: 'reuse context #squid@codex!' });
  await expect(sameRouteItem.locator('.ac-route-btn')).toContainText('#squid@codex!');
  await expect(sameRouteItem.locator('.ac-route-btn')).not.toContainText('!2');

  const otherRouteItem = page.locator('#autocomplete .ac-item', { hasText: '#squid@haiku!' });
  await expect(otherRouteItem.locator('.ac-route-btn')).toBeVisible();
  await expect(otherRouteItem.locator('.ac-route-btn')).toContainText('#squid@haiku!');
  await expect(otherRouteItem.locator('.ac-route-btn')).not.toContainText('!3');
});

test('clicking a different-route item body inserts prompt only without changing the route', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'push');
  const otherRouteItem = page.locator('#autocomplete .ac-item', { hasText: 'push the changes #other@codex' });
  await otherRouteItem.locator('.ac-history-prompt').click();

  await expect(page.locator('#input')).toHaveValue('push the changes');
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex');
});

test('Tab completion inserts the prompt and preserves the current route', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'draft', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('push');
  await expect(page.locator('#autocomplete .ac-item.selected .ac-route-btn')).toContainText('#other@codex');
  await composer.press('Tab');

  await expect(composer).toHaveValue('push the changes');
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
  await expect(page.locator('#topic-chip')).toContainText('#draft@codex');
});

test('clicking prompt history normalizes adhoc lookback out of the converted chip', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@haiku!3 review the changes',
  ] } }));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('review');
  const label = 'Use prompt and switch to #squid@haiku!';
  const routeButton = page.locator('#autocomplete').getByRole('button', { name: label, exact: true });
  await expect(routeButton).toHaveAttribute('title', label);
  await routeButton.locator('.ac-route-switch-icon').click();

  await expect(composer).toHaveValue('review the changes');
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@haiku!');
});

test('ArrowUp previews prompt text and ArrowDown restores the draft', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@haiku!3 review the changes',
    '#other@codex inspect the changes',
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'draft', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('unfinished draft');
  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('ArrowUp');

  await expect(composer).toHaveValue('review the changes');
  await expect(page.locator('#topic-chip')).toContainText('#draft@codex');

  await composer.press('ArrowUp');
  await expect(composer).toHaveValue('inspect the changes');
  await expect(page.locator('#topic-chip')).toContainText('#draft@codex');

  await composer.press('ArrowDown');
  await expect(composer).toHaveValue('review the changes');
  await expect(page.locator('#topic-chip')).toContainText('#draft@codex');

  await composer.press('ArrowDown');
  await expect(composer).toHaveValue('unfinished draft');
  await expect(page.locator('#topic-chip')).toContainText('#draft@codex');
});

for (const dismissal of ['Escape', 'outside click', 'close button']) {
  test(`recalled prompt and the current route survive ${dismissal} and reload`, async ({ page }) => {
    await mockBackend(page);
    await page.addInitScript(() => sessionStorage.setItem('squid_skip_sw_reload', '1'));
    await page.goto('/');
    await page.evaluate(() => {
      setTopicChip('squid', 'codex');
      applyHistoryFilter({ topic: 'squid', agent: 'codex', adhoc: false });
    });
    await expect.poll(() => page.evaluate(() => promptHistoryLoaded)).toBe(true);

    const composer = page.locator('#input');
    const chip = page.locator('#topic-chip');
    await composer.press('ArrowUp');
    await expect(composer).toHaveValue('push the changes');
    await expect(chip).toContainText('#squid@codex');
    await expect(page.locator('#search-bar-scope')).toHaveValue('#squid@codex');

    if (dismissal === 'Escape') await composer.press('Escape');
    if (dismissal === 'outside click') await page.locator('#messages').click();
    if (dismissal === 'close button') await page.getByRole('button', { name: 'Close suggestions' }).click();
    await expect(page.locator('#autocomplete')).toBeHidden();
    await expect(composer).toHaveValue('push the changes');
    await expect(chip).toContainText('#squid@codex');

    const draft = dismissal === 'Escape' ? '  edited prompt\nsecond line  ' : 'push the changes';
    if (dismissal === 'Escape') await composer.fill(draft);
    await page.reload();
    await expect(composer).toHaveValue(draft);
    await expect(chip).toContainText('#squid@codex');
  });
}

test('refresh keeps an explicitly selected sessioned default route with its draft', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', route => route.fulfill({ json: {
    items: ['#default@claude review the changes'],
  } }));
  await page.goto('/');
  await page.evaluate(() => setTopicChip('other', 'codex'));
  await expect.poll(() => page.evaluate(() => promptHistoryLoaded)).toBe(true);

  const composer = page.locator('#input');
  await composer.fill('review');
  await page.locator('#autocomplete .ac-route-btn', { hasText: '#default@claude' }).click();
  await expect(page.locator('#topic-chip')).toContainText('#default@claude');
  await page.reload();
  await expect(composer).toHaveValue('review the changes');
  await expect(page.locator('#topic-chip')).toContainText('#default@claude');
  await expect.poll(() => page.evaluate(() => stickyChip?.adhoc)).toBe(false);
});

test('an untouched tab leaves a newer draft and route intact on reload', async ({ page, context }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => {
    setTopicChip('squid', 'codex');
    localStorage.setItem('squid_draft', 'older draft');
  });
  const staleTab = await context.newPage();
  await mockBackend(staleTab);
  await staleTab.goto('/');
  await expect(staleTab.locator('#input')).toHaveValue('older draft');

  await page.evaluate(() => setTopicChip('other', 'codex'));
  await page.locator('#input').fill('newer draft');
  await expect.poll(() => page.evaluate(() => readStoredComposerDraft().draft?.text)).toBe('newer draft');

  await staleTab.reload();
  await expect(staleTab.locator('#input')).toHaveValue('newer draft');
  await expect(staleTab.locator('#topic-chip')).toContainText('#other@codex');
});

for (const sameText of [false, true]) {
  test(`clearing an older draft preserves the newer tab's ${sameText ? 'resaved original text' : 'edited text'}`, async ({ browser }) => {
    const context = await browser.newContext({ serviceWorkers: 'block' });
    try {
      const olderTab = await context.newPage();
      const newerTab = await context.newPage();
      await mockBackend(olderTab);
      await mockBackend(newerTab);
      await olderTab.goto('/');
      const olderComposer = olderTab.locator('#input');
      const newerComposer = newerTab.locator('#input');
      const savedDraft = () => newerTab.evaluate(() => readStoredComposerDraft().draft?.text ?? null);

      await olderComposer.fill('older draft');
      await expect.poll(() => olderTab.evaluate(() => readStoredComposerDraft().draft?.text)).toBe('older draft');
      await newerTab.goto('/');
      await newerComposer.fill('newer draft');
      await expect.poll(savedDraft).toBe('newer draft');
      const expectedDraft = sameText ? 'older draft' : 'newer draft';
      if (sameText) {
        await newerComposer.fill(expectedDraft);
        await expect.poll(savedDraft).toBe(expectedDraft);
      }

      await olderComposer.fill('');
      await expect.poll(() => olderTab.evaluate(() => lastSavedComposerDraft)).toBeNull();
      await newerTab.reload();
      await expect(newerComposer).toHaveValue(expectedDraft);

      await newerComposer.fill('');
      await expect.poll(savedDraft).toBeNull();
    } finally {
      await context.close();
    }
  });
}

test('ArrowUp moves within a multiline draft before recalling prompt history', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('fix this word\nkeep editing here');
  const endPosition = await composer.evaluate(input => {
    input.setSelectionRange(input.value.length, input.value.length);
    return input.selectionStart;
  });

  await composer.press('ArrowUp');

  await expect(composer).toHaveValue('fix this word\nkeep editing here');
  await expect.poll(() => composer.evaluate(input => input.selectionStart)).toBeLessThan(endPosition);
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);

  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('ArrowUp');
  await expect(composer).toHaveValue('push the changes');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);

  await composer.press('ArrowDown');
  await expect(composer).toHaveValue('fix this word\nkeep editing here');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
});

for (const [layout, width] of [['mobile', 390], ['desktop', 1280]]) {
  test(`autocomplete hides the advisory and history controls on ${layout}`, async ({ page }) => {
    await mockBackend(page);
    await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
      topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
    })));
    await page.route('**/topics/squid/session?agent=codex', r => r.fulfill({ json: {
      session_id: 'test-session', session_turn_count: 13,
    } }));
    await page.setViewportSize({ width, height: 844 });
    await page.goto('/');

    const composer = page.locator('#input');
    await composer.fill('/f #squid@codex');
    await composer.press('Enter');
    await expect(page.locator('#search-bar-scope')).toHaveValue('#squid@codex');
    await page.evaluate(() => {
      _sessionIds['squid@codex'] = 'test-session';
      _advisoryTurnCount = 13;
      evaluateAdvisory();
    });

    const advisory = page.locator('#session-advisory');
    const filter = page.locator('#search-bar');
    const filterButton = page.locator('#chip-filter-btn');
    const searchButton = page.locator('#chip-search-btn');
    const keywords = page.locator('#search-bar-keywords');
    const autocomplete = page.locator('#autocomplete');
    const sendButton = page.locator('#send-btn');
    await expect(advisory).toBeVisible();
    await expect(filter).toBeVisible();
    const sendPosition = await sendButton.boundingBox();

    await composer.fill('push');
    await expect(page.locator(`.ac-close-${layout}`)).toBeVisible();
    await expect(advisory).toBeHidden();
    await expect(filter).toBeHidden();
    await expect(searchButton).toHaveAttribute('aria-expanded', 'false');
    await expect(filterButton).toBeVisible();
    await expect(filterButton).toHaveAttribute('aria-pressed', 'true');
    const openSendPosition = await sendButton.boundingBox();
    expect(openSendPosition.x).toBeCloseTo(sendPosition.x);
    expect(openSendPosition.y + openSendPosition.height).toBeCloseTo(sendPosition.y + sendPosition.height);
    await page.getByRole('button', { name: 'Close suggestions' }).click();

    await expect(autocomplete).toBeHidden();
    await expect(advisory).toBeVisible();
    await expect(filter).toBeVisible();
    await expect(searchButton).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('#search-bar-scope')).toHaveValue('#squid@codex');
    await expect(page.locator('#messages')).toBeFocused();
    await expect(composer).toHaveValue('push');

    await keywords.fill('unfinished');
    await composer.fill('pus');
    await expect(autocomplete).toBeVisible();
    await composer.press('Escape');
    await expect(autocomplete).toBeHidden();
    await expect(filter).toBeVisible();
    await expect(keywords).toHaveValue('unfinished');

    await composer.fill('push');
    await expect(autocomplete).toBeVisible();
    if (layout === 'mobile') await searchButton.click();
    else await composer.press('Control+f');
    await expect(autocomplete).toBeHidden();
    await expect(keywords).toBeFocused();
    await expect(keywords).toHaveValue('unfinished');
    await expect(composer).toHaveValue('push');

    await composer.fill('pus');
    await expect(autocomplete).toBeVisible();
    await composer.press('Tab');
    await expect(composer).toHaveValue('push the changes');
  });
}

test('plain Enter sends the message without selecting from autocomplete', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/chat**', r => r.fulfill({ json: {} }));
  await page.goto('/');

  await page.fill('#input', 'push');
  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await page.keyboard.press('Enter');

  await expect(page.locator('#input')).toHaveValue('');
  await expect(page.locator('#autocomplete')).not.toHaveClass(/open/);
});

test('search composer action opens all-history search and preserves the draft', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/topics', r => r.fulfill({ json: [
    { name: 'default', agent: 'codex', sticky_adhoc: false, last_model: null, last_backend: 'codex', queue_depth: 0, active: false },
  ] }));
  await page.goto('/');

  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
  await page.fill('#input', 'needle');
  await page.locator('#chip-search-btn').click();

  await expect(page.locator('#search-bar')).toBeVisible();
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
  await expect(page.locator('#search-bar-keywords')).toBeFocused();
  await expect(page.locator('#input')).toHaveValue('needle');
});

test('composer input icons render at 16px', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await expect(page.locator('#chip-actions .composer-tool-btn').first()).toHaveCSS('width', '24px');
  await expect(page.locator('#chip-actions .composer-tool-btn').first()).toHaveCSS('height', '24px');
  await expect(page.locator('#chip-actions .composer-tool-btn svg').first()).toHaveCSS('width', '16px');
  await expect(page.locator('#chip-actions .composer-tool-btn svg').first()).toHaveCSS('height', '16px');
  await expect(page.locator('#pin-btn svg')).toHaveCSS('width', '16px');
  await expect(page.locator('#pin-btn svg')).toHaveCSS('height', '16px');

  const inputPaddingBottom = await page.locator('#input').evaluate(el => parseFloat(getComputedStyle(el).paddingBottom));
  expect(inputPaddingBottom).toBeLessThanOrEqual(6);
});

test('mobile composer actions stay compact', async ({ page }) => {
  await mockBackend(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');

  await expect(page.locator('#chip-bookmark-btn')).toBeVisible();
  await expect(page.locator('#chip-prompts-btn')).toHaveCount(0);
  await expect(page.locator('#chip-filter-btn')).toBeVisible();
  await expect(page.locator('#chip-filter-btn')).toHaveCSS('width', '28px');
  await expect(page.locator('#chip-filter-btn')).toHaveCSS('height', '28px');
  const inputPaddingBottom = await page.locator('#input').evaluate(el => parseFloat(getComputedStyle(el).paddingBottom));
  expect(inputPaddingBottom).toBeLessThanOrEqual(5);
});

test('clear composer action inserts /clear and ArrowUp restores the draft', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('draft before clear');
  await page.locator('#chip-clear-btn').click();

  await expect(composer).toHaveValue('/clear');
  await composer.press('ArrowUp');
  await expect(composer).toHaveValue('draft before clear');
});

for (const explicitRoute of [false, true]) {
  test(`clear stashes the ${explicitRoute ? 'explicit' : 'chip'} route with the draft`, async ({ page }) => {
    await mockBackend(page);
    await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
      topic: 'squid', agent: 'codex', adhoc: true, lookback: 0,
    })));
    await page.goto('/');

    const composer = page.locator('#input');
    const draft = explicitRoute ? '#squid@claude draft before clear' : 'draft before clear';
    const route = explicitRoute ? '#squid@claude' : '#squid@codex!';
    if (explicitRoute) await page.evaluate(() => clearTopicChip());
    await composer.fill(draft);
    await page.locator('#chip-clear-btn').click();

    await expect(composer).toHaveValue('/clear');
    expect(await page.evaluate(() => JSON.parse(localStorage.getItem('squid_stashed_prompts'))))
      .toEqual([`${route} draft before clear`]);
    await composer.press('ArrowUp');
    await expect(composer).toHaveValue('draft before clear');
    await expect(page.locator('#topic-chip')).toContainText(route);

    // Persisted recall must restore the route after the original chip is gone.
    await page.reload();
    await page.evaluate(() => clearTopicChip());
    await composer.fill('draft before');
    await page.locator('#autocomplete .ac-item', { hasText: 'draft before clear' }).locator('.ac-route-btn').click();
    await expect(composer).toHaveValue('draft before clear');
    await expect(page.locator('#topic-chip')).toContainText(route);
  });
}

test('stash composer action adds the prompt to recall and ArrowUp restores it', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('remember this draft');
  await page.locator('#chip-stash-btn').click();

  await expect(composer).toHaveValue('');
  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-item', { hasText: 'remember this draft' })).toBeVisible();

  await composer.press('ArrowUp');
  await expect(composer).toHaveValue('remember this draft');
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex');
});

test('stashing an existing route prompt moves one copy to the latest position', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    '#squid@codex older prompt',
    '#squid@codex Push the change and update the v0.1 to latest commit.',
  ] } }));
  await page.addInitScript(() => {
    localStorage.setItem('squid_stashed_prompts', JSON.stringify([
      '#squid@codex Push the change and update the v0.1 to latest commit.',
      '#squid@codex another stashed prompt',
    ]));
    localStorage.setItem('squid_sticky_chip', JSON.stringify({
      topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
    }));
  });
  await page.goto('/');

  await page.fill('#input', 'Push the change and update the v0.1 to latest commit.');
  await page.locator('#chip-stash-btn').click();

  const stashed = await page.evaluate(() => JSON.parse(localStorage.getItem('squid_stashed_prompts')));
  expect(stashed).toEqual([
    '#squid@codex Push the change and update the v0.1 to latest commit.',
    '#squid@codex another stashed prompt',
  ]);
  await expect(page.locator('#autocomplete .ac-item', { hasText: 'Push the change and update the v0.1 to latest commit.' })).toHaveCount(1);
});

test('composer action titles keep the restored chip route over delayed default fallback', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/topics', async route => {
    await new Promise(resolve => setTimeout(resolve, 150));
    await route.fulfill({ json: [
      { name: 'default', agent: 'claude', sticky_adhoc: true, last_model: null, last_backend: 'claude', queue_depth: 0, active: false },
    ] });
  });
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: true, lookback: 1,
  })));
  await page.goto('/');

  await expect(page.locator('#topic-chip')).toContainText('#squid@codex!1');
  await expect(page.locator('#chip-stash-btn')).toHaveAttribute('title', 'Stash prompt for autocomplete (#squid@codex!)');
  await page.waitForTimeout(250);
  await expect(page.locator('#chip-stash-btn')).toHaveAttribute('title', 'Stash prompt for autocomplete (#squid@codex!)');
});

test('prompt autocomplete dedupes normalized stashed and recent routes', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    '#squid@codex! reuse context',
  ] } }));
  await page.addInitScript(() => {
    localStorage.setItem('squid_stashed_prompts', JSON.stringify([
      '#squid@codex!1 reuse context',
    ]));
    localStorage.setItem('squid_sticky_chip', JSON.stringify({
      topic: 'squid', agent: 'codex', adhoc: true, lookback: 1,
    }));
  });
  await page.goto('/');

  await page.fill('#input', 'reuse');
  await expect(page.locator('#autocomplete .ac-item', { hasText: 'reuse context' })).toHaveCount(1);
});

test('prompt autocomplete dedupes exact duplicate recent prompts', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    '#squid@codex Push the change and update the v0.1 to latest commit.',
    '#squid@codex Push the change and update the v0.1 to latest commit.',
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'Push');
  await expect(page.locator('#autocomplete .ac-item', { hasText: 'Push the change and update the v0.1 to latest commit.' })).toHaveCount(1);
});

test('long prompt autocomplete rows keep the remove button visible', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockBackend(page);
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    '#squid@codex maybe i am on v0.1.0',
    '#squid@codex This is a very long saved prompt that should truncate before it can push the remove button outside the autocomplete row.',
    '#squid@codex This is another very long saved prompt matching the input so the selected row still keeps its remove button visible.',
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'This');

  const autocomplete = page.locator('#autocomplete');
  const removeButtons = page.locator('#autocomplete .ac-del-prompt-btn');
  await expect(removeButtons).toHaveCount(2);
  const boxes = await page.evaluate(() => {
    const ac = document.getElementById('autocomplete').getBoundingClientRect();
    const buttons = [...document.querySelectorAll('#autocomplete .ac-del-prompt-btn')]
      .map(btn => btn.getBoundingClientRect());
    return {
      acLeft: ac.left,
      acRight: ac.right,
      buttons: buttons.map(btn => ({ left: btn.left, right: btn.right })),
    };
  });
  for (const btn of boxes.buttons) {
    expect(btn.left).toBeGreaterThanOrEqual(boxes.acLeft);
    expect(btn.right).toBeLessThanOrEqual(boxes.acRight);
  }
  await expect(autocomplete).toContainText('This is a very long saved prompt');
});

test('bare default prompts are recorded without a default route chip', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/chat**', r => r.fulfill({ json: {} }));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('plain prompt');
  await page.keyboard.press('Enter');
  await expect(composer).toHaveValue('');

  await composer.fill('plain');
  const item = page.locator('#autocomplete .ac-item', { hasText: 'plain prompt' });
  await expect(item).toBeVisible();
  await expect(item.locator('.ac-route-btn')).toHaveCount(0);
  await expect(item).not.toContainText('#default');
});

test('submitted prompts are stashed before the chat response completes', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/chat**', () => {});
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('interruptible prompt');
  await page.keyboard.press('Enter');

  await expect.poll(() => page.evaluate(() => (
    JSON.parse(localStorage.getItem('squid_stashed_prompts') || '[]')
  ))).toEqual(['#squid@codex interruptible prompt']);
  await composer.fill('interruptible');
  await expect(page.locator('#autocomplete .ac-item', { hasText: 'interruptible prompt' })).toBeVisible();
});

for (const { name, chip, route } of [
  {
    name: 'topic and agent',
    chip: { topic: 'squid', agent: 'codex', adhoc: false, lookback: 0 },
    route: '#squid@codex',
  },
  {
    name: 'adhoc lookback',
    chip: { topic: 'squid', agent: 'codex', adhoc: true, lookback: 3 },
    route: '#squid@codex!3',
  },
]) {
  test(`Backspace at the start of a populated prompt expands the ${name} chip`, async ({ page }) => {
    await mockBackend(page);
    await page.addInitScript(value => {
      localStorage.setItem('squid_sticky_chip', JSON.stringify(value));
    }, chip);
    await page.goto('/');

    const composer = page.locator('#input');
    await composer.fill('fix the bug');
    await composer.evaluate(input => input.setSelectionRange(0, 0));
    await composer.press('Backspace');

    await expect(composer).toHaveValue(`${route} fix the bug`);
    await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
    await expect.poll(() => composer.evaluate(input => input.selectionStart)).toBe(route.length);
    await expect.poll(() => composer.evaluate(input => input.selectionEnd)).toBe(route.length);
  });
}

test('Backspace and chip click show the same scrollable route history and preserve the draft', async ({ page }) => {
  await mockBackend(page);
  await page.setViewportSize({ width: 390, height: 760 });
  const longRoute = '#architecture@codex+@reviewer>#handoff@verylongreviewagent!';
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    `${longRoute} review the design`,
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');
  await expect.poll(() => page.evaluate(() => promptHistoryLoaded)).toBe(true);

  const composer = page.locator('#input');
  const chip = page.locator('#topic-chip');
  const picker = page.locator('#autocomplete');
  const routes = picker.locator('.ac-route-btn');
  await composer.fill('keep this draft');
  await chip.click();
  await expect(routes).toHaveText([longRoute, '#squid@codex']);
  await composer.press('Escape');
  await composer.press('ArrowRight');
  await expect(composer).toHaveValue('keep this draft');

  await composer.press('Home');
  await composer.press('Backspace');
  await expect(routes).toHaveText([longRoute, '#squid@codex']);
  await expect.poll(() => routes.first().evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);
  await composer.press('ArrowUp');
  await expect(composer).toHaveValue('#squid@codex keep this draft');
  await composer.press('Enter');
  await expect(chip).toContainText(longRoute);
  await expect(composer).toHaveValue('keep this draft');
  await expect(picker).toBeHidden();
});

test('editing a route clears history selection while agent suggestions load', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/config/agents', r => r.fulfill({ json: [
    { name: 'claude', backend: 'claude' },
  ] }));
  let releaseAgentHistory;
  const agentHistory = new Promise(resolve => { releaseAgentHistory = resolve; });
  await page.route('**/topics/squid/agents/history', async r => {
    await agentHistory;
    await r.fulfill({ json: [] });
  });
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');
  await page.waitForFunction(() => typeof promptHistoryLoaded !== 'undefined' && promptHistoryLoaded);

  const composer = page.locator('#input');
  const chip = page.locator('#topic-chip');
  const picker = page.locator('#autocomplete');
  try {
    await composer.fill('keep this draft');
    await chip.click();
    await composer.press('ArrowUp');
    await expect(picker.locator('.selected .ac-route-btn')).toBeVisible();
    await expect(picker.locator('.selected .ac-route-btn')).not.toHaveText('#squid@codex');

    await composer.press('Backspace');
    await expect(composer).toHaveValue('#squid@ keep this draft');
    await expect(picker).toBeHidden();
    await composer.press('Enter');
  } finally {
    releaseAgentHistory();
  }

  await expect(picker.locator('.ac-route-label', { hasText: '#squid@claude' })).toBeVisible();
  await expect(composer).toHaveValue('#squid@ keep this draft');
  await expect(chip).toBeHidden();
});

test('Backspace on an empty prompt keeps expanding the topic chip as before', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: true, lookback: 3,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.focus();
  await composer.press('Backspace');

  await expect(composer).toHaveValue('#squid@codex!3');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
  await expect(page.locator('#autocomplete .ac-item.selected .ac-route-btn')).toHaveText('#squid@codex!');

  await composer.press('Tab');
  await expect(composer).toHaveValue('');
  await expect.poll(() => page.evaluate(() => stickyChip?.lookback)).toBe(3);
});

test('Editing an expanded route switches between agent suggestions, topic suggestions, and history', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/topics', r => r.fulfill({ json: [
    { name: 'squid', agent: 'codex' },
  ] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [
    { name: 'codex', backend: 'codex' },
    { name: 'claude', backend: 'claude' },
  ] }));
  await page.route('**/topics/squid/agents/history', r => r.fulfill({ json: [] }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: true, lookback: 12,
  })));
  await page.goto('/');
  await expect.poll(() => page.evaluate(() => promptHistoryLoaded)).toBe(true);

  const composer = page.locator('#input');
  await composer.fill('fix the bug');
  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('Backspace');

  await composer.press('Backspace');
  await expect(composer).toHaveValue('#squid@codex!1 fix the bug');
  await composer.press('Backspace');
  await expect(composer).toHaveValue('#squid@codex! fix the bug');
  await composer.press('Backspace');
  await expect(composer).toHaveValue('#squid@codex fix the bug');
  await composer.press('Backspace');
  await expect(composer).toHaveValue('#squid@ fix the bug');
  await expect(page.locator('#autocomplete .ac-route-label', { hasText: '#squid@claude' })).toBeVisible();
  await composer.press('Backspace');
  await expect(composer).toHaveValue('#squid fix the bug');
  await composer.press('Shift+ArrowLeft');
  await composer.press('Backspace');
  await expect(composer).toHaveValue('#squi fix the bug');
  await expect.poll(() => composer.evaluate(input => input.selectionStart)).toBe(5);
  await expect(page.locator('#autocomplete .ac-route-label', { hasText: '#squid@codex' })).toBeVisible();

  await composer.press('Backspace');
  await expect(composer).toHaveValue('# fix the bug');
  await expect(page.locator('#autocomplete .ac-route-btn', { hasText: '#other@codex' })).toBeVisible();
});

test('Backspace inside a populated prompt keeps the topic chip and edits normally', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('fix the bug');
  await composer.evaluate(input => input.setSelectionRange(4, 4));
  await composer.press('Backspace');

  await expect(composer).toHaveValue('fixthe bug');
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
});

test('moving from an expanded slug into the prompt restores the chip', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: true, lookback: 3,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('fix the bug');
  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('Backspace');
  await composer.press('ArrowRight');

  await expect(composer).toHaveValue('fix the bug');
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex!3');
  await expect.poll(() => composer.evaluate(input => input.selectionStart)).toBe(0);
});

test('restored chip uses the edited slug', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('fix the bug');
  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('Backspace');
  await composer.evaluate(input => {
    input.value = '#other@haiku!2 fix the bug';
    input.setSelectionRange(input.value.length, input.value.length);
    input.dispatchEvent(new Event('input'));
  });

  await expect(composer).toHaveValue('fix the bug');
  await expect(page.locator('#topic-chip')).toContainText('#other@haiku!2');
});

test('slug autocomplete stays separate from prompt history and preserves the prompt', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/topics', route => route.fulfill({ json: [
    { name: 'other', agent: 'haiku', last_prompt: 'previous topic prompt', active: false },
  ] }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('fix the bug');
  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('Backspace');
  await composer.evaluate(input => {
    input.value = '#ot fix the bug';
    input.setSelectionRange(3, 3);
    input.dispatchEvent(new Event('input'));
  });

  const items = page.locator('#autocomplete .ac-item');
  await expect(items).toHaveCount(1);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  await expect(items.first()).toContainText('#other@haiku');
  await expect(items.first()).toContainText('last previous topic prompt');
  await expect(items.first()).not.toContainText('push the changes');
  await items.first().click();

  await expect(composer).toHaveValue('#other fix the bug');
  await expect.poll(() => composer.evaluate(input => input.selectionStart)).toBe('#other'.length);
});

test('agent autocomplete supports an empty agent slot before adhoc lookback', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/config/agents', route => route.fulfill({ json: [
    { name: 'codex', backend: 'codex' },
    { name: 'claude', backend: 'claude' },
  ] }));
  await page.route('**/topics/squid/agents/history', route => route.fulfill({ json: [
    { agent: 'claude', last_prompt: 'session prompt', last_adhoc_prompt: 'adhoc prompt' },
  ] }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: true, lookback: 12,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await page.locator('#topic-chip').click();
  await composer.evaluate(input => input.setSelectionRange('#squid@codex'.length, '#squid@codex'.length));
  await composer.press('Backspace');
  await expect(composer).toHaveValue('#squid@!12');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  const claudeItem = page.locator('#autocomplete .ac-item', { hasText: '#squid@claude!12' });
  await expect(claudeItem).toBeVisible();
  await expect(claudeItem).toContainText('last adhoc prompt');

  await claudeItem.click();

  await expect(composer).toHaveValue('');
  await expect(page.locator('#topic-chip')).toContainText('#squid@claude!12');
});

test('route chain target autocomplete opens while typing squid flow route', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/config/agents', route => route.fulfill({ json: [
    { name: 'codex', backend: 'codex' },
    { name: 'revuopen', backend: 'opencode' },
    { name: 'revucla', backend: 'claude' },
  ] }));
  await page.route('**/topics/squid/agents/history', route => route.fulfill({ json: [
    { agent: 'revuopen', last_prompt: 'review lane prompt', last_adhoc_prompt: 'fresh review prompt' },
  ] }));
  await page.unroute('**/prompts/recent**');
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@codex>@revuopen full flow prompt',
  ] } }));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('#squid@codex>@revu');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  await expect.poll(async () => page.locator('#autocomplete .ac-item').evaluateAll(items => {
    const text = items.map(item => item.innerText.replace(/\s+/g, ''));
    return {
      persistent: text.some(row => row.startsWith('#squid@codex>@revuopenlastfullflowprompt')),
      targetLane: text.some(row => row.includes('reviewlaneprompt') || row.includes('freshreviewprompt')),
    };
  })).toEqual({ persistent: true, targetLane: false });

  await page.locator('#autocomplete .ac-item', { hasText: '#squid@codex>@revuopen!' }).click();

  await expect(composer).toHaveValue('');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex>@revuopen!');
});

test('route chain target autocomplete opens immediately after chain separator', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/config/agents', route => route.fulfill({ json: [
    { name: 'codex', backend: 'codex' },
    { name: 'revuopen', backend: 'opencode' },
  ] }));
  await page.route('**/topics/squid/agents/history', route => route.fulfill({ json: [
    { agent: 'revuopen', last_prompt: 'review lane prompt', last_adhoc_prompt: 'fresh review prompt' },
  ] }));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('#squid@codex>');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  await expect(page.locator('#autocomplete .ac-item', { hasText: '#squid@codex>@revuopen' })).toHaveCount(2);

  await page.locator('#autocomplete .ac-item', { hasText: '#squid@codex>@revuopen!' }).click();

  await expect(composer).toHaveValue('');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex>@revuopen!');
});

test('multi-head route chain target autocomplete opens after join separator', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/config/agents', route => route.fulfill({ json: [
    { name: 'a', backend: 'codex' },
    { name: 'b', backend: 'codex' },
    { name: 'revuopen', backend: 'opencode' },
  ] }));
  await page.route('**/topics/squid/agents/history', route => route.fulfill({ json: [
    { agent: 'revuopen', last_prompt: 'review lane prompt', last_adhoc_prompt: 'fresh review prompt' },
  ] }));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('#squid@a+@b>@rev');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  await expect(page.locator('#autocomplete .ac-item', { hasText: '#squid@a+@b>@revuopen' })).toHaveCount(2);

  await page.locator('#autocomplete .ac-item', { hasText: '#squid@a+@b>@revuopen!' }).click();

  await expect(composer).toHaveValue('');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@a+@b>@revuopen!');
});

test('route chain target autocomplete opens right after a bare "<", before ">" is typed', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/config/agents', route => route.fulfill({ json: [
    { name: 'codex', backend: 'codex' },
    { name: 'revuopen', backend: 'opencode' },
  ] }));
  await page.route('**/topics/squid/agents/history', route => route.fulfill({ json: [
    { agent: 'revuopen', last_prompt: 'review lane prompt', last_adhoc_prompt: 'fresh review prompt' },
  ] }));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('#squid@codex<');

  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  // "<" auto-closes to "<>" in suggested completions since numbered rounds aren't accepted yet.
  await expect(page.locator('#autocomplete .ac-item', { hasText: '#squid@codex<>@revuopen' })).toHaveCount(2);

  await page.locator('#autocomplete .ac-item', { hasText: '#squid@codex<>@revuopen!' }).click();

  await expect(composer).toHaveValue('');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex<>@revuopen!');
});

test('long autocomplete routes scroll horizontally on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 760 });
  await mockBackend(page);
  await page.unroute('**/prompts/recent**');
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@codex+#architecture@reviewer>#handoff@verylongreviewagent! long flow prompt',
  ] } }));
  await page.goto('/');

  await page.fill('#input', 'long');

  const routeBtn = page.locator('#autocomplete .ac-route-btn').first();
  await expect(routeBtn).toBeVisible();
  await expect(routeBtn).toContainText('#architecture');
  await expect(routeBtn).toContainText('@verylongreviewagent!');
  await expect.poll(() => routeBtn.evaluate(el => ({
    scrollable: el.scrollWidth > el.clientWidth,
    overflowX: getComputedStyle(el).overflowX,
  }))).toEqual({ scrollable: true, overflowX: 'auto' });
});

test('an invalid edited slug remains expanded on blur', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('fix the bug');
  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('Backspace');
  await composer.evaluate(input => {
    input.value = '#squid@ fix the bug';
    input.dispatchEvent(new Event('input'));
  });
  await page.locator('#messages').click();

  await expect(composer).toHaveValue('#squid@ fix the bug');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
});
