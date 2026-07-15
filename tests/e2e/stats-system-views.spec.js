const { test, expect } = require('@playwright/test');

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: {} }));
  await page.route('**/stats/filters', r => r.fulfill({
    json: { agents: ['codex'], topics: ['squid'] },
  }));
  await page.route('**/stats/filter-presets', r => r.fulfill({ json: [] }));
  await page.route('**/stats?**', r => r.fulfill({
    json: [{ period: '2026-07-14 10:00', sessions: 1, total_turns: 2,
             input_tokens: 100, output_tokens: 50 }],
  }));
}

// ── dropdown: hardcoded system views always present ────────────────────────

test('system views Overview and Deep Dive by Turns are always in the dropdown', async ({ page }) => {
  await mockApp(page);

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();

  const options = await page.locator('#stats-preset-select option').evaluateAll(
    opts => opts.map(o => ({ value: o.value, text: o.textContent.trim() }))
  );
  expect(options).toEqual([
    { value: '__overall', text: 'Overview' },
    { value: '__deepdive', text: 'Deep Dive by Turns' },
  ]);
});

// ── no trashbin on system views ────────────────────────────────────────────

test('delete button is disabled when a system view is selected', async ({ page }) => {
  await mockApp(page);

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();

  // Deep Dive selected by default — delete disabled
  await expect(page.locator('#stats-preset-select')).toHaveValue('__deepdive');
  await expect(page.locator('#stats-preset-delete')).toBeDisabled();

  // Deep Dive — delete disabled
  await page.locator('#stats-preset-select').selectOption('__deepdive');
  await expect(page.locator('#stats-preset-delete')).toBeDisabled();

  // Save a user preset so we can verify delete is enabled for it.
  // Override the mock so POST /stats/filter-presets returns a proper preset object.
  let nextPresetId = 1;
  await page.route('**/stats/filter-presets', async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      return route.fulfill({ json: { id: nextPresetId++, name: body.name, is_default: false } });
    }
    if (route.request().method() === 'GET') {
      return route.fulfill({ json: nextPresetId > 1 ? [{ id: 1, name: 'My View', is_default: false }] : [] });
    }
    return route.fulfill({ json: {} });
  });
  await page.locator('#stats-preset-save').click();
  await page.locator('#preset-name-input').fill('My View');
  await page.locator('#preset-name-confirm').click();
  await expect(page.locator('#stats-preset-delete')).toBeEnabled();

  // Back to Overview — delete disabled again
  await page.locator('#stats-preset-select').selectOption('__overall');
  await expect(page.locator('#stats-preset-delete')).toBeDisabled();
});

// ── reserved names blocked ─────────────────────────────────────────────────

test('reserved names Overview and Deep Dive by Turns cannot be saved', async ({ page }) => {
  await mockApp(page);

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();

  // Try "Overview"
  await page.locator('#stats-preset-save').click();
  await page.locator('#preset-name-input').fill('Overview');
  await page.locator('#preset-name-confirm').click();
  await expect(page.locator('#preset-name-modal-error')).toContainText('reserved system view');
  await expect(page.locator('#preset-name-modal')).toHaveClass(/open/);

  // Try "Deep Dive by Turns"
  await page.locator('#preset-name-input').fill('Deep Dive by Turns');
  await page.locator('#preset-name-confirm').click();
  await expect(page.locator('#preset-name-modal-error')).toContainText('reserved system view');
  await expect(page.locator('#preset-name-modal')).toHaveClass(/open/);

  // Try case-insensitive variants
  await page.locator('#preset-name-input').fill('OVERVIEW');
  await page.locator('#preset-name-confirm').click();
  await expect(page.locator('#preset-name-modal-error')).toContainText('reserved system view');

  // Close modal
  await page.locator('#preset-name-cancel').click();
});

// ── star: only one active at a time ────────────────────────────────────────

test('star is only active on one view at a time', async ({ page }) => {
  await mockApp(page);

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();

  // No user presets: star active on Deep Dive (the built-in fallback default)
  await expect(page.locator('#stats-preset-select')).toHaveValue('__deepdive');
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);
  await expect(page.locator('#stats-preset-default')).toBeEnabled();

  // Switch to Overview: star NOT active (Deep Dive is the fallback, not Overview)
  await page.locator('#stats-preset-select').selectOption('__overall');
  await expect(page.locator('#stats-preset-default')).not.toHaveClass(/active/);
  await expect(page.locator('#stats-preset-default')).toBeEnabled();
});

// ── star click on system view clears user default ──────────────────────────

test('clicking star on Deep Dive clears an existing user default', async ({ page }) => {
  let presets = [{
    id: 1, name: 'My View', is_default: true,
    state: { version: 1, time: { period: 'hourly', days: 7 },
             dimensions: { topic: { mode: 'all', values: [] }, agent: { mode: 'all', values: [] }, session_type: { mode: 'all', values: [] } },
             breakdown: { key: '' }, measure: { primary: { metric: 'turns', agg: 'sum' }, series: [], visible: ['turns', 'sessions', 'tokens_in', 'tokens_out'] } },
  }];
  let nextId = 2;
  await mockApp(page);
  await page.route('**/stats/filter-presets', async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      const preset = { id: nextId++, name: body.name, is_default: false, state: body.state };
      presets.push(preset);
      return route.fulfill({ json: preset });
    }
    return route.fulfill({ json: presets });
  });
  await page.route('**/stats/filter-presets/**', async route => {
    const id = Number(new URL(route.request().url()).pathname.split('/').pop());
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON();
      if (body.is_default === true) presets.forEach(p => p.is_default = false);
      presets = presets.map(p => p.id === id ? { ...p, ...body } : p);
      return route.fulfill({ json: presets.find(p => p.id === id) });
    }
    return route.fulfill({ json: {} });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();

  // My View is default — star active on it
  await expect(page.locator('#stats-preset-select')).toHaveValue('1');
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);

  // Switch to Deep Dive — star inactive (My View is default, not Deep Dive)
  await page.locator('#stats-preset-select').selectOption('__deepdive');
  await expect(page.locator('#stats-preset-default')).not.toHaveClass(/active/);

  // Click star on Deep Dive — saves a "Deep Dive by Turns" preset as new default
  await page.locator('#stats-preset-default').click();
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);

  // Switch to Overview — star inactive (Deep Dive preset is default, not Overview)
  await page.locator('#stats-preset-select').selectOption('__overall');
  await expect(page.locator('#stats-preset-default')).not.toHaveClass(/active/);

  // Switch back to Deep Dive — star active again
  await page.locator('#stats-preset-select').selectOption('__deepdive');
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);
});

// ── star click on Overview when already active is a no-op ───────────────────

test('clicking star on Deep Dive when already the fallback default is a no-op', async ({ page }) => {
  await mockApp(page);

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();

  // Deep Dive active by default — star lit
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);

  // Click star — should stay lit (no-op, already the fallback)
  await page.locator('#stats-preset-default').click();
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);
  await expect(page.locator('#stats-preset-select')).toHaveValue('__deepdive');
});

// ── dropdown auto-selects Deep Dive when statsPeriod is turn ────────────────

test('dropdown shows Deep Dive by Turns after navigating via deep dive button', async ({ page }) => {
  await mockApp(page);
  await page.route('**/history**', r => r.fulfill({
    json: {
      items: [{
        id: 1, timestamp: '2026-07-14T10:00:00Z', topic: 'squid', agent: 'codex',
        role: 'assistant', status: 'done', content: 'response',
        stats: { input_tokens: 100, output_tokens: 50, session_id: 's1' },
      }],
      has_more: false,
    },
  }));
  await page.route('**/chat/*/status', r => r.fulfill({
    json: { id: 1, msg_id: 1, topic: 'squid', agent: 'codex', status: 'done', content: 'response' },
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();

  // Default: Deep Dive by Turns
  await expect(page.locator('#stats-preset-select')).toHaveValue('__deepdive');
  await expect(page.locator('#sf-period')).toHaveValue('turn');

  // Navigate to history, click deep dive button
  await page.getByRole('button', { name: 'Chat' }).click();
  await expect(page.locator('.stats-deep-dive-btn')).toBeVisible();
  await page.locator('.stats-deep-dive-btn').click();

  // Now on stats with deep dive state
  await expect(page.locator('#stats-preset-select')).toHaveValue('__deepdive');
  await expect(page.locator('#sf-period')).toHaveValue('turn');
});

test('deep dive button records mobile history so browser/swipe back returns to Chat', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApp(page);
  await page.route('**/history**', r => r.fulfill({
    json: {
      items: [{
        id: 1, timestamp: '2026-07-14T10:00:00Z', topic: 'squid', agent: 'codex',
        role: 'assistant', status: 'done', content: 'response',
        stats: { input_tokens: 100, output_tokens: 50, session_id: 's1' },
      }],
      has_more: false,
    },
  }));
  await page.route('**/chat/*/status', r => r.fulfill({
    json: { id: 1, msg_id: 1, topic: 'squid', agent: 'codex', status: 'done', content: 'response' },
  }));

  await page.goto('/');
  await expect(page.locator('#view-chat')).toHaveClass(/active/);
  await expect(page.locator('.stats-deep-dive-btn')).toBeVisible();
  await page.locator('.stats-deep-dive-btn').click();
  await expect(page.locator('#view-stats')).toHaveClass(/active/);

  // The click was an intra-page nav (no real page load), so a mobile back
  // gesture / browser back button should pop the pushed history state and
  // return to Chat, not leave the swipe with nowhere to go.
  await page.goBack();
  await expect(page.locator('#view-chat')).toHaveClass(/active/);
});

// ── reserved names also blocked server-side ─────────────────────────────────

test('clicking star on Deep Dive saves it as a persistent default preset', async ({ page }) => {
  let presets = [{
    id: 99, name: 'Other View', is_default: true,
    state: { version: 1, time: { period: 'hourly', days: 1 },
             dimensions: { topic: { mode: 'all', values: [] }, agent: { mode: 'all', values: [] }, session_type: { mode: 'all', values: [] } },
             breakdown: { key: '' }, measure: { primary: { metric: 'turns', agg: 'sum' }, series: [], visible: ['turns'] } },
  }];
  let nextId = 100;
  await mockApp(page);
  await page.route('**/stats/filter-presets', async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      const preset = { id: nextId++, name: body.name, is_default: false, state: body.state };
      presets.push(preset);
      return route.fulfill({ json: preset });
    }
    return route.fulfill({ json: presets });
  });
  await page.route('**/stats/filter-presets/**', async route => {
    const id = Number(new URL(route.request().url()).pathname.split('/').pop());
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON();
      if (body.is_default === true) presets.forEach(p => p.is_default = false);
      presets = presets.map(p => p.id === id ? { ...p, ...body } : p);
      return route.fulfill({ json: presets.find(p => p.id === id) });
    }
    return route.fulfill({ json: {} });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();

  // Other View is default — star active on it, not on Deep Dive
  await expect(page.locator('#stats-preset-select')).toHaveValue('99');
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);

  // Switch to Deep Dive — star inactive (Other View is default)
  await page.locator('#stats-preset-select').selectOption('__deepdive');
  await expect(page.locator('#stats-preset-default')).not.toHaveClass(/active/);

  // Click star — saves a "Deep Dive by Turns" preset as the new default
  await page.locator('#stats-preset-default').click();
  // Star should now be active (Deep Dive preset became default)
  // The mock auto-clears Other View's is_default when setting a new default
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);

  // Switch to Overview — star inactive (Deep Dive preset is default, not Overview)
  await page.locator('#stats-preset-select').selectOption('__overall');
  await expect(page.locator('#stats-preset-default')).not.toHaveClass(/active/);

  // Switch back to Deep Dive — star active
  await page.locator('#stats-preset-select').selectOption('__deepdive');
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);
});

// ── star click on Overview persists it as the default (symmetric to Deep Dive) ─

test('clicking star on Overview saves it as a persistent default preset', async ({ page }) => {
  let presets = [{
    id: 99, name: 'Other View', is_default: true,
    state: { version: 1, time: { period: 'hourly', days: 1 },
             dimensions: { topic: { mode: 'all', values: [] }, agent: { mode: 'all', values: [] }, session_type: { mode: 'all', values: [] } },
             breakdown: { key: '' }, measure: { primary: { metric: 'turns', agg: 'sum' }, series: [], visible: ['turns'] } },
  }];
  let nextId = 100;
  await mockApp(page);
  await page.route('**/stats/filter-presets', async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      const preset = { id: nextId++, name: body.name, is_default: false, state: body.state };
      presets.push(preset);
      return route.fulfill({ json: preset });
    }
    return route.fulfill({ json: presets });
  });
  await page.route('**/stats/filter-presets/**', async route => {
    const id = Number(new URL(route.request().url()).pathname.split('/').pop());
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON();
      if (body.is_default === true) presets.forEach(p => p.is_default = false);
      presets = presets.map(p => p.id === id ? { ...p, ...body } : p);
      return route.fulfill({ json: presets.find(p => p.id === id) });
    }
    return route.fulfill({ json: {} });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();

  // Other View is default — star active on it, not on Overview
  await expect(page.locator('#stats-preset-select')).toHaveValue('99');
  await page.locator('#stats-preset-select').selectOption('__overall');
  await expect(page.locator('#stats-preset-default')).not.toHaveClass(/active/);

  // Click star on Overview — saves an "Overview" preset as the new default
  await page.locator('#stats-preset-default').click();
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);

  // Switch to Deep Dive — star inactive (Overview preset is default, not Deep Dive)
  await page.locator('#stats-preset-select').selectOption('__deepdive');
  await expect(page.locator('#stats-preset-default')).not.toHaveClass(/active/);

  // Reload the page entirely — the saved Overview default must survive reload,
  // not just the in-memory session (this is what regressed previously).
  await page.reload();
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();
  await expect(page.locator('#stats-preset-select')).toHaveValue('__overall');
  await expect(page.locator('#stats-preset-default')).toHaveClass(/active/);
});

// ── reset icon goes to whatever is actually marked default ─────────────────

test('reset icon goes to the saved default (Overview), not hardcoded Deep Dive', async ({ page }) => {
  let presets = [{
    id: 5, name: 'Overview', is_default: true,
    state: { version: 1, time: { period: 'hourly', days: 1 },
             dimensions: { topic: { mode: 'all', values: [] }, agent: { mode: 'all', values: [] }, session_type: { mode: 'all', values: [] } },
             breakdown: { key: '' }, measure: { primary: { metric: 'turns', agg: 'sum' }, series: [], visible: ['turns'] } },
  }];
  await mockApp(page);
  await page.route('**/stats/filter-presets', route => route.fulfill({ json: presets }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();

  // Overview preset is default — that's what loads on fresh page open.
  await expect(page.locator('#stats-preset-select')).toHaveValue('__overall');

  // Navigate away to Deep Dive, then hit reset — it should go back to the
  // saved default (Overview), not hardcoded Deep Dive.
  await page.locator('#stats-preset-select').selectOption('__deepdive');
  await expect(page.locator('#sf-period')).toHaveValue('turn');
  await page.locator('#stats-preset-reset').click();
  await expect(page.locator('#stats-preset-select')).toHaveValue('__overall');
  await expect(page.locator('#sf-period')).toHaveValue('hourly');
});

test('reset icon falls back to Deep Dive when nothing is marked default', async ({ page }) => {
  await mockApp(page);

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();

  await page.locator('#stats-preset-select').selectOption('__overall');
  await expect(page.locator('#sf-period')).toHaveValue('hourly');
  await page.locator('#stats-preset-reset').click();
  await expect(page.locator('#stats-preset-select')).toHaveValue('__deepdive');
  await expect(page.locator('#sf-period')).toHaveValue('turn');
});

test('frontend blocks reserved names in the save modal', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats/filter-presets', async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      return route.fulfill({ json: { id: 1, name: body.name, is_default: false } });
    }
    return route.fulfill({ json: [] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();

  await page.locator('#stats-preset-save').click();
  await page.locator('#preset-name-input').fill('Overview');
  await page.locator('#preset-name-confirm').click();
  await expect(page.locator('#preset-name-modal-error')).toContainText('reserved system view');
  await expect(page.locator('#preset-name-modal')).toHaveClass(/open/);

  await page.locator('#preset-name-cancel').click();
});
