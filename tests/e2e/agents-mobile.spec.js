const { test, expect } = require('@playwright/test');

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: {
    status: 'ok',
    backends: {},
    harnesses: [
      { id: 'claude', label: 'Claude Code', installed: true, protocol: 'stdio', default_provider: 'anthropic', compatible_providers: ['anthropic'] },
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

  test('runtime column abbreviated on mobile', async ({ page }) => {
    await mockApp(page);
    await page.goto('/');
    await navigateToAgentsMobile(page);
    await page.waitForSelector('#agents-list table');

    // Check that runtime-short spans are visible and runtime-full are hidden
    const visibility = await page.evaluate(() => {
      const fullSpans = document.querySelectorAll('#agents-list tbody .runtime-full');
      const shortSpans = document.querySelectorAll('#agents-list tbody .runtime-short');
      const results = [];
      for (let i = 0; i < fullSpans.length; i++) {
        const fullStyle = getComputedStyle(fullSpans[i]);
        const shortStyle = getComputedStyle(shortSpans[i]);
        results.push({
          fullDisplay: fullStyle.display,
          shortDisplay: shortStyle.display,
          fullText: fullSpans[i].textContent,
          shortText: shortSpans[i]?.textContent || '',
        });
      }
      return results;
    });

    // On mobile (390px), runtime-full should be hidden and runtime-short visible
    expect(visibility.length).toBeGreaterThanOrEqual(2);
    for (const v of visibility) {
      expect(v.fullDisplay).toBe('none');
      expect(v.shortDisplay).toBe('inline');
    }
    // First agent: claude / anthropic → cla:ant
    expect(visibility[0].fullText).toBe('claude / anthropic');
    expect(visibility[0].shortText).toBe('cla:ant');
    // Second agent: opencode / openai → ope:ope
    expect(visibility[1].fullText).toBe('opencode / openai');
    expect(visibility[1].shortText).toBe('ope:ope');
  });

  test('runtime header abbreviated on mobile', async ({ page }) => {
    await mockApp(page);
    await page.goto('/');
    await navigateToAgentsMobile(page);
    await page.waitForSelector('#agents-list table');

    const headerTexts = await page.evaluate(() => {
      const headerFull = document.querySelector('#agents-list thead .runtime-full');
      const headerShort = document.querySelector('#agents-list thead .runtime-short');
      return {
        fullDisplay: headerFull ? getComputedStyle(headerFull).display : 'missing',
        shortDisplay: headerShort ? getComputedStyle(headerShort).display : 'missing',
        fullText: headerFull?.textContent || '',
        shortText: headerShort?.textContent || '',
      };
    });

    expect(headerTexts.fullDisplay).toBe('none');
    expect(headerTexts.shortDisplay).toBe('inline');
    expect(headerTexts.fullText).toBe('Runtime');
    expect(headerTexts.shortText).toBe('Run');
  });
});
