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
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('breakdown') === 'agent') {
      return route.fulfill({
        json: [{ period: '2026-06-26 14:00', agent_key: 'codex', agent: 'codex', sessions: 2, total_turns: 3 }],
      });
    }
    return route.fulfill({
      json: [{ period: '2026-06-26 14:00', sessions: 2, total_turns: 3, input_tokens: 1500, output_tokens: 700 }],
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-period').selectOption('hourly');
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
  await page.locator('#sf-breakdown').selectOption('agent');
  await page.locator('#stats-content tfoot td').first().getByRole('button', { name: 'Sort breakdown columns by total' }).nth(1).click();
  await page.locator('#stats-preset-save').click();
  await expect(page.locator('#preset-name-modal')).toHaveClass(/open/);
  await page.locator('#preset-name-input').fill('Squid Codex');
  await page.locator('#preset-name-confirm').click();

  await expect(page.locator('#stats-preset-select')).toHaveValue('1');
  await expect(page.locator('#stats-preset-select')).toContainText('Squid Codex');
  expect(presets[0].state.dimensions.topic.values).toEqual(['squid']);
  expect(presets[0].state.dimensions.agent.values).toEqual(['codex']);
  expect(presets[0].state.breakdown.sort).toEqual({ mode: 'total', dir: 'desc' });

  await page.locator('#sf-topic-toggle').click();
  await page.locator('#sf-topic-menu input[value="ops"]').check();
  await expect(page.locator('#stats-preset-select')).toHaveValue('1');
  await page.locator('#stats-preset-select').selectOption('1');
  await expect(page.locator('#sf-topic-toggle')).toHaveText('#squid');
  await expect(page.locator('#sf-agent-toggle')).toHaveText('@codex');
  await expect(page.locator('#stats-preset-select')).toHaveValue('1');

  await page.locator('#stats-preset-delete').click();
  await expect(page.locator('#stats-preset-select')).toHaveValue('__deepdive');
  await expect(page.locator('#sf-topic-toggle')).toHaveText('All Topics');
  await expect(page.locator('#sf-agent-toggle')).toHaveText('All Agents');
  await expect(page.locator('#sf-breakdown')).toHaveValue('');
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
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);
  await expect(page.locator('#sf-topic-toggle')).toHaveText('#squid');
  await expect(page.locator('#sf-agent-toggle')).toHaveText('@codex');
});

test('stats presets save chart micro settings and reset restores overall view', async ({ page }) => {
  await mockApp(page);
  let presets = [];
  let nextId = 1;
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex'], topics: ['squid'] },
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
  await page.route('**/stats?**', route => route.fulfill({
    json: [{
      period: '2026-06-26 14:00',
      sessions: 2,
      total_turns: 3,
      input_tokens: 1500,
      output_tokens: 700,
      cost_usd: 1.25,
      chart_cost_avg: 1.25,
      chart_tokens_in_p95: 1500,
    }],
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-period').selectOption('hourly');

  await page.locator('#sf-measures-toggle').click();
  await page.locator('#sf-measures-menu input[value="cost"]').check();
  await page.locator('#sc-y1').selectOption('cost');
  await page.locator('#sc-y1-agg').selectOption('avg');
  // Remove the default extra series (cache_hit_rate) so the new one is alone.
  const removeBtns = page.locator('.sc-series-remove');
  const count = await removeBtns.count();
  for (let i = 0; i < count; i++) await removeBtns.nth(0).click();
  await page.locator('#sc-add-series').click();
  await page.locator('.sc-extra-metric').selectOption('tokens_in');
  await page.locator('.sc-extra-agg').selectOption('p95');
  await page.locator('.sc-extra-axis').click();
  await expect(page.locator('.sc-series-pill-text')).toHaveText('P95 Tokens In · R');

  await page.locator('#stats-preset-save').click();
  await page.locator('#preset-name-input').fill('Chart Shape');
  await page.locator('#preset-name-confirm').click();

  expect(presets[0].state.measure.primary).toEqual({ metric: 'cost', agg: 'avg' });
  expect(presets[0].state.measure.series).toEqual([{ metric: 'tokens_in', agg: 'p95', axis: 'y2' }]);
  expect(presets[0].state.measure.visible).toContain('cost');

  await page.locator('#sf-period').selectOption('turn');
  await page.locator('#sf-topic-toggle').click();
  await page.locator('#sf-topic-menu input[value="squid"]').check();
  await page.locator('#sf-agent-toggle').click();
  await page.locator('#sf-agent-menu input[value="codex"]').check();
  await page.locator('#sf-adhoc').selectOption('adhoc');

  await page.locator('#stats-preset-reset').click();
  await expect(page.locator('#stats-preset-select')).toHaveValue('__deepdive');
  // Reset goes to Deep Dive: turn, 3h, tokens_in + cache_hit_rate
  await expect(page.locator('#sf-period')).toHaveValue('turn');
  await expect(page.locator('#sf-days')).toHaveValue('-3');
  await expect(page.locator('#sf-breakdown')).toHaveValue('');
  await expect(page.locator('#sf-breakdown')).toBeDisabled();
  await expect(page.locator('#sf-topic-toggle')).toHaveText('All Topics');
  await expect(page.locator('#sf-agent-toggle')).toHaveText('All Agents');
  await expect(page.locator('#sf-adhoc')).toHaveValue('all');
  await expect(page.locator('#sc-y1')).toHaveValue('tokens_in');
  await expect(page.locator('#sc-y1-agg')).toHaveValue('sum');
  await expect(page.locator('#sf-measures-menu input[value="cost"]')).not.toBeChecked();
  await expect(page.locator('#sf-measures-menu input[value="quota"]')).not.toBeChecked();

  await page.locator('#stats-preset-select').selectOption('1');
  await expect(page.locator('#sc-y1')).toHaveValue('cost');
  await expect(page.locator('#sc-y1-agg')).toHaveValue('avg');
  await expect(page.locator('.sc-series-pill-text')).toHaveText('P95 Tokens In · R');
  await expect(page.locator('#sf-measures-menu input[value="cost"]')).toBeChecked();
});

test('saving a duplicate view name shows the conflict in the modal and can overwrite', async ({ page }) => {
  await mockApp(page);
  let presets = [{
    id: 1,
    name: 'Squid Codex',
    is_default: false,
    state: {
      version: 1,
      time: { period: 'hourly', days: 14 },
      dimensions: {
        topic: { mode: 'all', values: [] },
        agent: { mode: 'all', values: [] },
        session_type: { mode: 'all', values: [] },
      },
      breakdown: { key: '' },
      measure: { primary: 'turns', secondary: null, visible: ['sessions', 'turns'] },
    },
  }];
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex'], topics: ['squid'] },
  }));
  await page.route('**/stats/filter-presets', async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      if (presets.some(p => p.name.toLowerCase() === body.name.toLowerCase())) {
        return route.fulfill({ status: 400, json: { error: `A view named "${body.name}" already exists.` } });
      }
      const preset = { id: 2, name: body.name, state: body.state, is_default: false };
      presets.push(preset);
      return route.fulfill({ json: preset });
    }
    return route.fulfill({ json: presets });
  });
  await page.route('**/stats/filter-presets/**', async route => {
    const id = Number(new URL(route.request().url()).pathname.split('/').pop());
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON();
      presets = presets.map(p => p.id === id ? { ...p, ...body } : p);
      return route.fulfill({ json: presets.find(p => p.id === id) });
    }
  });
  await page.route('**/stats?**', route => route.fulfill({
    json: [{ period: '2026-06-26 14:00', sessions: 2, total_turns: 3 }],
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  await page.locator('#stats-preset-save').click();
  await page.locator('#preset-name-input').fill('Squid Codex');
  await page.locator('#preset-name-confirm').click();

  // Error appears inside the modal, not in the status span behind the icons.
  await expect(page.locator('#preset-name-modal')).toHaveClass(/open/);
  await expect(page.locator('#preset-name-modal-error')).toHaveText('A view named "Squid Codex" already exists.');
  await expect(page.locator('#preset-name-overwrite')).toBeVisible();

  await page.locator('#preset-name-overwrite').click();

  await expect(page.locator('#preset-name-modal')).not.toHaveClass(/open/);
  await expect(page.locator('#stats-preset-select')).toHaveValue('1');
});

test('star toggles default on a named preset', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex'], topics: ['squid'] },
  }));
  let presets = [{
    id: 3,
    name: 'My Preset',
    is_default: true,
    state: {
      version: 1,
      time: { period: 'hourly', days: 7 },
      dimensions: {
        topic: { mode: 'selected', values: ['squid'] },
        agent: { mode: 'all', values: [] },
        session_type: { mode: 'all', values: [] },
      },
      breakdown: { key: '' },
      measure: { primary: 'turns', secondary: null, visible: ['sessions', 'turns'] },
    },
  }];
  let nextId = 4;
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
      // Mirrors the backend's unique-default index: setting is_default=true
      // on one row clears it on every other row.
      if (body.is_default === true) presets = presets.map(p => ({ ...p, is_default: p.id === id }));
      presets = presets.map(p => p.id === id ? { ...p, ...body } : p);
      return route.fulfill({ json: presets.find(p => p.id === id) });
    }
  });
  await page.route('**/stats?**', route => route.fulfill({
    json: [{ period: '2026-06-26 14:00', sessions: 1, total_turns: 2 }],
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  // Starts on the default preset — star button is lit
  await expect(page.locator('#stats-preset-select')).toHaveValue('3');
  await expect(page.locator('#stats-preset-default')).toBeEnabled();
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);

  // Click star to unset default
  await page.locator('#stats-preset-default').click();
  await expect(page.locator('#stats-preset-default')).not.toHaveClass(/active/);

  // Click star again to set as default
  await page.locator('#stats-preset-default').click();
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);

  // Switch to Overall View — star is enabled but inactive (another preset is default).
  await page.locator('#stats-preset-select').selectOption('__overall');
  await expect(page.locator('#stats-preset-default')).toBeEnabled();
  await expect(page.locator('#stats-preset-default')).not.toHaveClass(/active/);
  await expect(page.locator('#stats-preset-delete')).toBeDisabled();

  // Click star on Overview — saves Overview itself as the persistent default
  // (clearing the other preset's default), so its own star lights up.
  await page.locator('#stats-preset-default').click();
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);

  // Switch to Deep Dive — star is inactive; Overview is the saved default now.
  await page.locator('#stats-preset-select').selectOption('__deepdive');
  await expect(page.locator('#stats-preset-default')).not.toHaveClass(/active/);
});
