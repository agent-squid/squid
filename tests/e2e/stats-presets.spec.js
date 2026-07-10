const { test, expect } = require('@playwright/test');

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: {} }));
}

test('stats filter presets save in their own top row and apply saved state', async ({ page }) => {
  await mockApp(page);
  let presets = [];
  let nextId = 1;
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex', 'clive'], topics: ['squid', 'ops'] },
  }));
  await page.route('**/stats/filter-presets', async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      const preset = { id: nextId++, name: body.name, state: body.state, is_default: false };
      presets.push(preset);
      return route.fulfill({ json: preset });
    }
    return route.fulfill({ json: presets });
  });
  await page.route('**/stats/filter-presets/**', async route => {
    const id = Number(new URL(route.request().url()).pathname.split('/').pop());
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON();
      presets = presets.map(p => p.id === id ? { ...p, ...body, is_default: body.is_default ? true : p.is_default } : p);
      return route.fulfill({ json: presets.find(p => p.id === id) });
    }
    if (route.request().method() === 'DELETE') {
      presets = presets.filter(p => p.id !== id);
      return route.fulfill({ json: { ok: true } });
    }
  });
  await page.route('**/stats?**', route => route.fulfill({
    json: [{ period: '2026-06-26 14:00', sessions: 2, total_turns: 3, input_tokens: 1500, output_tokens: 700 }],
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-presets')).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const presetsTop = document.getElementById('stats-presets').getBoundingClientRect().top;
    const filtersTop = document.getElementById('stats-filters').getBoundingClientRect().top;
    return presetsTop < filtersTop;
  })).toBe(true);

  await page.locator('#sf-topic-toggle').click();
  await page.locator('#sf-topic-menu input[value="squid"]').check();
  await page.locator('#sf-agent-toggle').click();
  await page.locator('#sf-agent-menu input[value="codex"]').check();
  page.once('dialog', async dialog => {
    expect(dialog.message()).toBe('Preset name');
    await dialog.accept('Squid Codex');
  });
  await page.locator('#stats-preset-save').click();

  await expect(page.locator('#stats-preset-select')).toHaveValue('1');
  await expect(page.locator('#stats-preset-select')).toContainText('Squid Codex');
  await expect(page.locator('#stats-preset-status')).toHaveText('saved');
  expect(presets[0].state.dimensions.topic.values).toEqual(['squid']);
  expect(presets[0].state.dimensions.agent.values).toEqual(['codex']);

  await page.locator('#sf-topic-toggle').click();
  await page.locator('#sf-topic-menu input[value="ops"]').check();
  await expect(page.locator('#stats-preset-select')).toHaveValue('__custom');
  await page.locator('#stats-preset-select').selectOption('1');
  await expect(page.locator('#sf-topic-toggle')).toHaveText('#squid');
  await expect(page.locator('#sf-agent-toggle')).toHaveText('@codex');
  await expect(page.locator('#stats-preset-select')).toHaveValue('1');
});

test('stats filter preset dropdown shows the default saved view on load', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex', 'clive'], topics: ['squid', 'ops'] },
  }));
  await page.route('**/stats/filter-presets', route => route.fulfill({
    json: [{
      id: 7,
      name: 'Squid Codex',
      is_default: true,
      state: {
        version: 1,
        time: { period: 'hourly', days: 14 },
        dimensions: {
          topic: { mode: 'selected', values: ['squid'] },
          agent: { mode: 'selected', values: ['codex'] },
          session_type: { mode: 'all', values: [] },
        },
        breakdown: { key: '' },
        measure: { primary: 'turns', secondary: null, visible: ['sessions', 'turns'] },
      },
    }],
  }));
  await page.route('**/stats?**', route => route.fulfill({
    json: [{ period: '2026-06-26 14:00', sessions: 2, total_turns: 3, input_tokens: 1500, output_tokens: 700 }],
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  await expect(page.locator('#stats-preset-select')).toHaveValue('7');
  await expect(page.locator('#stats-preset-select')).toContainText('Squid Codex (default)');
  await expect(page.locator('#sf-topic-toggle')).toHaveText('#squid');
  await expect(page.locator('#sf-agent-toggle')).toHaveText('@codex');
});
