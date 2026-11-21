const { test, expect } = require('@playwright/test');

test.use({ serviceWorkers: 'block' });

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: {
    status: 'ok',
    backends: {},
    harnesses: [
      { id: 'claude', label: 'Claude Code', installed: true, protocol: 'stdio', default_provider: 'anthropic', compatible_providers: ['anthropic'], effort: { levels: { low: 'Quick', high: 'Thorough' }, models: { 'no-thinking': {} } } },
      { id: 'opencode', label: 'OpenCode', installed: true, protocol: 'stdio', default_provider: 'openai', compatible_providers: ['openai'] },
    ],
    providers: {
      anthropic: { label: 'Anthropic', auth_type: 'api_key', missing_secrets: [], models: ['claude-sonnet-4-6'], color: '#d97757' },
      openai: { label: 'OpenAI', auth_type: 'api_key', missing_secrets: ['OPENAI_API_KEY'], models: ['gpt-5'], color: '#74aa9c' },
    },
  } }));
  await page.route('**/config/agents', r => r.fulfill({ json: [
    { name: 'dev', harness: 'claude', provider: 'anthropic', model: 'claude-sonnet-4-6' },
    { name: 'review', harness: 'opencode', provider: 'openai', model: 'gpt-5' },
  ] }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: {} }));
  await page.route('**/quota/**', r => r.fulfill({ json: {} }));
  await page.route('**/creds/**', r => r.fulfill({ json: {} }));
}

async function navigateToAgentsMobile(page) {
  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-menu').getByRole('button', { name: 'Agents' }).click();
  await expect(page.locator('#view-agents')).toHaveClass(/active/);
}

test.describe('agents view — mobile', () => {
  test.use({ viewport: { width: 390, height: 844 } }); // iPhone 14 size

  test('agent form fields layout in 2 rows', async ({ page }) => {
    await mockApp(page);
    await page.goto('/');
    await navigateToAgentsMobile(page);
    await page.waitForSelector('#agents-list table');

    const layout = await page.evaluate(() => {
      const row = document.querySelector('.agent-form-row');
      if (!row) return null;
      const style = getComputedStyle(row);
      const controls = [
        '#af-name',
        '#af-harness',
        '#af-provider',
        '#af-model',
        '#af-cwd',
        '.agent-form-row button[type=submit]',
      ].map(selector => {
        const el = document.querySelector(selector);
        const rect = el.getBoundingClientRect();
        return { selector, top: Math.round(rect.top), width: Math.round(rect.width) };
      });
      return { gridAreas: style.gridTemplateAreas, controls };
    });

    expect(layout.gridAreas).toContain('name');
    expect(layout.gridAreas).toContain('harness');
    expect(layout.gridAreas).toContain('provider');
    expect(layout.gridAreas).toContain('save');
    expect(layout.gridAreas).toContain('model');
    expect(layout.gridAreas).toContain('picker');
    expect(layout.gridAreas).toContain('cwd');
    expect(layout.gridAreas).toContain('status');
    expect(new Set(layout.controls.map(c => c.top)).size).toBe(2);

    const bySelector = Object.fromEntries(layout.controls.map(c => [c.selector, c.top]));
    expect(bySelector['#af-name']).toBe(bySelector['#af-harness']);
    expect(bySelector['#af-harness']).toBe(bySelector['#af-provider']);
    expect(bySelector['#af-model']).toBe(bySelector['#af-cwd']);
    expect(bySelector['#af-cwd']).toBe(bySelector['.agent-form-row button[type=submit]']);
    expect(bySelector['#af-name']).toBeLessThan(bySelector['#af-model']);

    const byWidth = Object.fromEntries(layout.controls.map(c => [c.selector, c.width]));
    expect(byWidth['#af-name']).toBeGreaterThanOrEqual(88);
    expect(byWidth['#af-harness']).toBeGreaterThanOrEqual(105);
    expect(byWidth['#af-provider']).toBeLessThanOrEqual(112);

    // Verify status is on its own row (all cells in that row are "status")
    const rows = layout.gridAreas.split('"').filter(s => s.trim());
    const statusRow = rows.find(r => r.includes('status'));
    expect(statusRow).toBeTruthy();
    expect(statusRow.trim().split(/\s+/).every(w => w === 'status')).toBe(true);
  });

  test('runtime column shows the harness and provider on mobile', async ({ page }) => {
    await mockApp(page);
    await page.goto('/');
    await navigateToAgentsMobile(page);

    const agents = page.locator('#agents-list table');
    await expect(agents.getByRole('columnheader', { name: /Runtime/ })).toBeVisible();
    await expect(agents.getByRole('cell', { name: 'claude / anthropic' })).toBeVisible();
    await expect(agents.getByRole('cell', { name: 'opencode / openai' })).toBeVisible();
  });
});


test('configured effort resets on model changes and fits mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApp(page);
  await page.goto('/');
  await navigateToAgentsMobile(page);
  await page.locator('#af-harness').selectOption('claude');
  const effort = page.locator('#af-effort');
  await expect(effort).toBeEnabled();
  await expect(effort.locator('option')).toHaveText(['Default', 'Quick', 'Thorough']);
  await effort.selectOption('high');
  await page.locator('#af-model').fill('no-thinking');
  await expect(effort).toBeDisabled();
  await expect(effort).toHaveValue('');
  await page.locator('#af-model').fill('claude-sonnet-4-6');
  await expect(effort).toBeEnabled();
  await expect(effort).toHaveValue('');
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    const model = await page.locator('#af-model').boundingBox();
    const box = await effort.boundingBox();
    expect(Math.abs(model.y - box.y)).toBeLessThan(2);
    expect(model.width).toBeGreaterThan(box.width * 1.5);
    expect(box.x + box.width).toBeLessThanOrEqual(width);
  }
  let saved;
  await page.route('**/config/agents', async r => {
    if (r.request().method() === 'POST') {
      saved = r.request().postDataJSON();
      await r.fulfill({ json: { ok: true, sessions_cleared: [] } });
    } else await r.fulfill({ json: [] });
  });
  await page.locator('#af-name').fill('new-agent');
  await effort.selectOption('high');
  await page.locator('.agent-save-btn').click();
  await expect.poll(() => saved?.effort).toBe('high');
});


test('agent list shows effort with runtime default model', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApp(page);
  await page.route('**/config/agents', r => r.fulfill({ json: [
    { name: 'default-model', harness: 'claude', provider: 'anthropic', model: null, effort: 'high' },
  ] }));
  await page.goto('/');
  await navigateToAgentsMobile(page);
  await expect(page.locator('#agents-list .agent-model')).toHaveText('Default / Thorough');
});

for (const [harness, name, id, directory] of [
  ['codex', 'Codex', 'cope', '.codex'],
  ['claudecode', 'Claude', 'clape', '.claude'],
]) {
  test(`configured ${name} login shows its status and authenticates from chat`, async ({ page }) => {
    const home = `/home/operator/.squid/provider-homes/${id}/${directory}`;
    await mockApp(page);
    await page.route('**/health', route => route.fulfill({ json: {
      status: 'ok',
      harnesses: [
        { id: harness, label: name, installed: true, default_provider: id, compatible_providers: [id] },
      ],
      providers: {
        host: { label: `${name} Host`, auth_type: 'subscription', subscription_harness: harness },
        [id]: {
          label: `${name} Personal`, auth_type: 'subscription',
          subscription_harness: harness, dedicated_login: true, codex_home: home,
          codex_signed_in: false, subscription_needs_verification: harness === "claudecode",
        },
      },
    } }));
    const selectedAgent = harness === 'claudecode' ? 'claude' : 'codex';
    await page.route('**/config/agents', route => route.fulfill({ json: [
      { name: selectedAgent, harness, provider: id, dedicated_login: true },
    ] }));
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'sse' } }));
    await page.route('**/auth/session', route => route.fulfill({
      status: 400,
      json: { error: 'Fixture stopped before contacting a login service' },
    }));
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await navigateToAgentsMobile(page);
    const catalog = page.locator('#providers-catalog');
    const host = catalog.locator('.bcat-row').filter({ hasText: `${name} Host` });
    await expect(host.getByText('Uses host login')).toBeVisible();
    const provider = catalog.locator('.bcat-row').filter({ hasText: `${name} Personal` });
    await expect(provider.getByText(`Separate ${name} login`)).toBeVisible();
    await expect(catalog.getByRole('checkbox')).toHaveCount(0);
    await expect(provider.getByText(home)).toBeVisible();
    await expect(provider).toContainText(harness === 'claudecode' ? 'Account needs verification' : 'Signed out');
    await expect(catalog.getByRole('button', { name: /Sign in/ })).toHaveCount(0);
    await page.locator('#hamburger-btn').click();
    await page.locator('#hamburger-menu').getByRole('button', { name: 'Chat', exact: true }).click();
    const slashRequest = page.waitForRequest(req => new URL(req.url()).pathname === '/auth/session');
    await page.locator('#input').fill(`#test@${selectedAgent} /login`);
    await page.locator('#input').press('Enter');
    expect((await slashRequest).postDataJSON()).toMatchObject({ harness, provider: id, mode: 'login' });
  });
}
