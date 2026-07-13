const { test, expect } = require('@playwright/test');

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: {} }));
  await page.route('**/stats/filters', r => r.fulfill({ json: { agents: ['codex'], topics: ['squid'] } }));
  await page.route('**/stats/filter-presets', r => r.fulfill({ json: [] }));
}

test('stats days filter includes 1d and 3d windows and requests them', async ({ page }) => {
  await mockApp(page);
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    route.fulfill({ json: [] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  const dayOptions = await page.locator('#sf-days option').evaluateAll(
    opts => opts.map(o => ({ value: o.value, text: o.textContent }))
  );
  expect(dayOptions).toEqual([
    { value: '1', text: '1d' },
    { value: '3', text: '3d' },
    { value: '7', text: '7d' },
    { value: '30', text: '30d' },
    { value: '90', text: '90d' },
    { value: '0', text: 'All Time' },
  ]);

  await page.locator('#sf-days').selectOption('1');
  await expect.poll(() => statsRequests.some(req => req.days === '1')).toBe(true);

  await page.locator('#sf-days').selectOption('3');
  await expect.poll(() => statsRequests.some(req => req.days === '3')).toBe(true);
});

test('Tokens In/Out/Total, Cache Read/Write, New Input, Cache Hit % and Avg Tokens/Turn compute consistently', async ({ page }) => {
  await mockApp(page);
  // Claude-style split: input_tokens is just the small uncacheable residual;
  // the real content lands in cache_write (new) and cache_read (reused).
  await page.route('**/stats?**', route => route.fulfill({
    json: [{
      period: '2026-07-10 10:00',
      sessions: 1, total_turns: 2,
      input_tokens: 50, output_tokens: 50,
      cache_read_tokens: 300, cache_write_tokens: 100,
      cost_usd: 0.01,
    }],
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  // Tokens In must be New Input + Cache Read, i.e. it has to include
  // cache_write (100) — not just input_tokens + cache_read (which would
  // wrongly give 350, silently dropping the cache_write tokens).
  // Columns: period, turns, sessions, tokens_in, tokens_out.
  await expect(page.locator('#stats-content tbody td').nth(3)).toHaveText('450');

  await page.locator('#sf-measures-toggle').click();
  for (const key of ['sessions', 'turns', 'tokens_in', 'tokens_out']) {
    await page.locator(`#sf-measures-menu input[value="${key}"]`).uncheck();
  }
  for (const key of ['cache_read', 'cache_write', 'new_input', 'cache_hit_rate', 'avg_tokens_turn', 'tokens_total']) {
    await page.locator(`#sf-measures-menu input[value="${key}"]`).check();
  }

  const cells = page.locator('#stats-content tbody td');
  // cells.nth(0) is the period column.
  await expect(cells.nth(1)).toHaveText('250');    // Avg Tokens/Turn = (450+50) / 2
  await expect(cells.nth(2)).toHaveText('66.7%');  // Cache Hit % = 300 / (300+150)
  await expect(cells.nth(3)).toHaveText('300');    // Cache Read
  await expect(cells.nth(4)).toHaveText('100');    // Cache Write
  await expect(cells.nth(5)).toHaveText('150');    // New Input (50 residual + 100 cache_write)
  await expect(cells.nth(6)).toHaveText('500');    // Total Tokens = Tokens In + Tokens Out

  const totals = page.locator('#stats-content tfoot td');
  await expect(totals.nth(1)).toHaveText('250');
  await expect(totals.nth(2)).toHaveText('66.7%');
  await expect(totals.nth(3)).toHaveText('300');
  await expect(totals.nth(4)).toHaveText('100');
  await expect(totals.nth(5)).toHaveText('150');
  await expect(totals.nth(6)).toHaveText('500');
});

test('stats measures are alphabetical and Turns links open responses', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats?**', route => route.fulfill({
    json: [
      { period: '2026-07-10 11:00', sessions: 1, total_turns: 1, input_tokens: 10, output_tokens: 5, message_ids: '10' },
      { period: '2026-07-10 10:00', sessions: 1, total_turns: 2, input_tokens: 20, output_tokens: 10, message_ids: '20,21' },
    ],
  }));
  await page.route('**/chat/*/status', route => {
    const id = Number(new URL(route.request().url()).pathname.split('/')[2]);
    return route.fulfill({
      json: {
        id,
        msg_id: id,
        topic: 'squid',
        agent: 'codex',
        role: 'assistant',
        status: 'done',
        prompt_context: id === 10 ? JSON.stringify({ pins: [20, 21] }) : null,
        content: `Response ${id}`,
      },
    });
  });

  await page.goto('/');
  await page.evaluate(() => switchView('stats'));
  await page.waitForFunction(() => document.querySelectorAll('.stats-turn-link').length === 2);
  await page.locator('.stats-turn-link[data-turn-ids="10"]').click();
  await expect(page.locator('#view-stats')).toHaveClass(/active/);
  await expect(page.locator('#view-chat')).not.toHaveClass(/active/);
  await expect(page.locator('#msg-modal')).toHaveClass(/open/);
  await expect(page.locator('#msg-modal-title')).toContainText('Message #10');
  await expect(page.locator('#msg-modal-body')).toContainText('Response 10');
  await page.locator('#msg-modal .user-ctx').click();
  await expect(page.locator('#ctx-popup')).toHaveClass(/open/);
  await expect(page.locator('#ctx-popup')).toHaveClass(/modal-context-popup/);
  await expect(page.locator('#ctx-popup .ctx-popup-pin')).toHaveCount(2);
  await expect.poll(() => page.evaluate(() => {
    const popupZ = Number(getComputedStyle(document.getElementById('ctx-popup')).zIndex);
    const modalZ = Number(getComputedStyle(document.getElementById('msg-modal')).zIndex);
    return popupZ > modalZ;
  })).toBe(true);
  await page.locator('#ctx-popup .ctx-popup-pin[data-pin-id="20"]').click();
  await expect(page.locator('#msg-modal-title')).toContainText('Message #20');
  await expect(page.locator('#ctx-popup')).not.toHaveClass(/open/);
  await page.locator('#msg-modal').evaluate(modal => modal.classList.remove('open'));

  const state = await page.evaluate(async () => {
    const measureLabels = [...document.querySelectorAll('#sf-measures-menu label')]
      .map(label => label.textContent.trim().replace(/\s+/g, ' '));
    const links = [...document.querySelectorAll('.stats-turn-link')]
      .map(btn => ({ text: btn.textContent.trim(), ids: btn.dataset.turnIds }));

    showStatsTurnsPopup(document.querySelector('.stats-turn-link[data-turn-ids="20,21"]'), [20, 21]);
    await new Promise(resolve => setTimeout(resolve, 100));
    const popupIds = [...document.querySelectorAll('#stats-turn-popup .ctx-popup-pin')]
      .map(row => row.dataset.turnId);

    return { measureLabels, links, popupIds };
  });

  expect(state.measureLabels).toEqual([
    'Turns',
    'Avg Tokens/Turn',
    'Cache Hit %',
    'Cache Read',
    'Cache Write',
    'Cost',
    'Duration',
    'New Input',
    'Quota',
    'Sessions',
    'Tokens In',
    'Tokens Out',
    'Total Tokens',
  ]);
  expect(state.links).toEqual([
    { text: '1', ids: '10' },
    { text: '2', ids: '20,21' },
  ]);
  expect(state.popupIds).toEqual(['20', '21']);
});

test('stats turn list popup stays inside the stats view', async ({ page }) => {
  await mockApp(page);
  const ids = Array.from({ length: 40 }, (_, i) => i + 1);
  await page.route('**/stats?**', route => route.fulfill({
    json: [{
      period: '2026-07-10 11:00',
      sessions: 1,
      total_turns: ids.length,
      input_tokens: 10,
      output_tokens: 5,
      message_ids: ids.join(','),
    }],
  }));
  await page.route('**/chat/*/status', route => {
    const id = Number(new URL(route.request().url()).pathname.split('/')[2]);
    return route.fulfill({ json: { id, content: `Response ${id}` } });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('.stats-turn-link').click();
  await expect(page.locator('#stats-turn-popup')).toHaveClass(/open/);
  await expect(page.locator('#stats-turn-popup')).toHaveClass(/stats-turn-popup/);

  await expect.poll(() => page.evaluate(() => {
    const popup = document.getElementById('stats-turn-popup').getBoundingClientRect();
    const stats = document.getElementById('view-stats').getBoundingClientRect();
    return {
      top: popup.top >= stats.top,
      bottom: popup.bottom <= stats.bottom,
      left: popup.left >= stats.left,
      right: popup.right <= stats.right,
    };
  })).toEqual({ top: true, bottom: true, left: true, right: true });
  await expect.poll(() => page.evaluate(() => {
    const popupZ = Number(getComputedStyle(document.getElementById('stats-turn-popup')).zIndex);
    const modalZ = Number(getComputedStyle(document.getElementById('msg-modal')).zIndex);
    return popupZ < modalZ;
  })).toBe(true);

  await page.locator('#stats-turn-popup .ctx-popup-pin[data-turn-id="1"]').click();
  await expect(page.locator('#msg-modal')).toHaveClass(/open/);
  await expect(page.locator('#msg-modal-title')).toContainText('Message #1');
  await expect(page.locator('#stats-turn-popup')).toHaveClass(/open/);
  await expect(page.locator('#stats-turn-popup')).toHaveClass(/stats-turn-popup/);
  await expect.poll(() => page.evaluate(() => {
    const popupZ = Number(getComputedStyle(document.getElementById('stats-turn-popup')).zIndex);
    const modalZ = Number(getComputedStyle(document.getElementById('msg-modal')).zIndex);
    return popupZ < modalZ;
  })).toBe(true);
  await page.locator('#msg-modal .user-ctx').click();
  await expect(page.locator('#ctx-popup')).toHaveClass(/open/);
  await expect(page.locator('#stats-turn-popup')).toHaveClass(/open/);
  await page.locator('#ctx-popup .ctx-popup-row').first().click();
  await expect(page.locator('#stats-turn-popup')).toHaveClass(/open/);
});

test('cache hit chart requests a non-additive aggregate', async ({ page }) => {
  await mockApp(page);
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    route.fulfill({
      json: [{
        period: '2026-07-10 10:00',
        sessions: 2,
        total_turns: 2,
        input_tokens: 100,
        output_tokens: 0,
        cache_read_tokens: 150,
        cache_write_tokens: 0,
        chart_cache_hit_rate_avg: 75,
      }],
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-measures-toggle').click();
  await page.locator('#sf-measures-menu input[value="cache_hit_rate"]').check();
  await page.locator('#sc-y1').selectOption('cache_hit_rate');

  await expect.poll(() => statsRequests.at(-1)?.chart_metrics).toBe('cache_hit_rate');
  expect(statsRequests.at(-1).chart_aggs).toBe('avg');
});

test('stats chart aggregate chips apply to Y1 and an added series', async ({ page }) => {
  await mockApp(page);
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    if (url.searchParams.get('breakdown') === 'agent') {
      return route.fulfill({
        json: [{
          period: '2026-07-10 10:00',
          agent_key: 'codex',
          agent: 'codex',
          sessions: 3,
          total_turns: 3,
          input_tokens: 60,
          output_tokens: 30,
          chart_tokens_in_avg: 15,
          chart_tokens_in_p50: 20,
          chart_tokens_in_p75: 30,
        }],
      });
    }
    route.fulfill({
      json: [{
        period: '2026-07-10 10:00',
        sessions: 3,
        total_turns: 3,
        input_tokens: 60,
        output_tokens: 30,
        cost_usd: 0.02,
        chart_tokens_in_avg: 15,
        chart_tokens_in_p50: 20,
        chart_tokens_in_p75: 30,
      }],
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  await expect(page.locator('#sc-y1')).toHaveValue('turns');
  await expect(page.locator('#sc-y1-agg')).toBeVisible();
  await expect(page.locator('#sc-y1-agg option')).toHaveText(['SUM']);
  await expect(page.locator('#sc-y1-agg')).toHaveValue('sum');

  await page.locator('#sc-y1').selectOption('tokens_in');
  await expect(page.locator('#sc-y1-agg')).toBeVisible();
  await page.locator('#sc-y1-agg').selectOption('p50');
  await expect(page.locator('#stats-content thead th')).toContainText([
    'Hour',
    'Turns',
    'Sessions',
    'P50 Tokens In',
    'Tokens Out',
  ]);
  await expect(page.locator('#stats-content tbody td').nth(3)).toHaveText('20');
  await expect(page.locator('#stats-content tfoot td').nth(3)).toHaveText('—');
  await expect.poll(() => statsRequests.some(req => (
    req.chart_metrics === 'tokens_in' && req.chart_aggs === 'p50'
  ))).toBe(true);
  await page.locator('#sc-y1-agg').selectOption('avg');
  await expect(page.locator('#stats-content thead th').nth(3)).toHaveText('AVG Tokens In');
  await expect(page.locator('#stats-content tbody td').nth(3)).toHaveText('15');
  await page.locator('#sc-y1-agg').selectOption('p50');

  await page.locator('#sc-add-series').click();
  await expect(page.locator('.sc-series-pill')).toHaveCount(1);
  await expect(page.locator('.sc-extra-row')).toBeVisible();
  const extraRow = page.locator('.sc-extra-row').first();
  await extraRow.locator('.sc-extra-metric').selectOption('tokens_in');
  await expect(extraRow.locator('.sc-extra-agg')).toBeVisible();
  await extraRow.locator('.sc-extra-agg').selectOption('p75');

  await expect.poll(() => statsRequests.some(req => (
    req.chart_metrics === 'tokens_in,tokens_in' &&
    req.chart_aggs === 'p50,p75'
  ))).toBe(true);
  await expect.poll(() => page.evaluate(() => {
    const chart = window.Chart?.getChart(document.getElementById('stats-chart'));
    const datasets = chart?.data?.datasets || [];
    return datasets.length === 2 && datasets[0].borderColor !== datasets[1].borderColor;
  })).toBe(true);

  await page.locator('#sf-breakdown').selectOption('agent');
  await expect(page.locator('#stats-chart-controls')).toHaveClass(/breakdown-active/);
  await expect(page.locator('#sc-y1-agg')).toBeVisible();
  await expect(page.locator('#sc-extra')).toBeEmpty();
  await expect.poll(() => statsRequests.some(req => (
    req.breakdown === 'agent' &&
    req.chart_metrics === 'tokens_in' &&
    req.chart_aggs === 'p50'
  ))).toBe(true);
  await expect(page.locator('#stats-content tbody td.stats-series-col').first()).toHaveText('20');
});

test('stats chart uses distinct colors for Tokens In and New Input even though they share a base color', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats?**', route => route.fulfill({
    json: [{
      period: '2026-07-10 10:00',
      sessions: 1, total_turns: 1,
      input_tokens: 50, output_tokens: 50,
      cache_read_tokens: 300, cache_write_tokens: 100,
      cost_usd: 0.01,
      chart_new_input_p95: 125,
    }],
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  await page.locator('#sf-measures-toggle').click();
  await page.locator('#sf-measures-menu input[value="new_input"]').check();

  await page.locator('#sc-y1').selectOption('tokens_in');
  await page.locator('#sc-add-series').click();
  const newInputRow = page.locator('.sc-extra-row').first();
  await newInputRow.locator('.sc-extra-metric').selectOption('new_input');
  await newInputRow.locator('.sc-extra-agg').selectOption('p95');
  await page.keyboard.press('Escape');
  await expect(page.locator('.sc-extra-row')).toHaveCount(0);
  await expect(page.locator('.sc-series-pill')).toContainText('P95 New Input · L');
  await expect(page.locator('#stats-content thead th')).toContainText([
    'Hour',
    'Turns',
    'P95 New Input',
    'Sessions',
    'Tokens In',
    'Tokens Out',
  ]);
  await expect(page.locator('#stats-content tbody td').nth(2)).toHaveText('125');
  await expect(page.locator('#stats-content tfoot td').nth(2)).toHaveText('—');

  await expect.poll(() => page.evaluate(() => {
    const chart = window.Chart?.getChart(document.getElementById('stats-chart'));
    const datasets = chart?.data?.datasets || [];
    return datasets.length === 2 && datasets[0].borderColor !== datasets[1].borderColor;
  })).toBe(true);
});

test('same measure can be added multiple times with different aggregations, and axis is togglable', async ({ page }) => {
  await mockApp(page);
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    route.fulfill({
      json: [{
        period: '2026-07-10 10:00',
        sessions: 3, total_turns: 3,
        input_tokens: 60, output_tokens: 30, cost_usd: 0.02,
        cache_read_tokens: 40, cache_write_tokens: 10,
        chart_tokens_in_p50: 20,
        chart_tokens_in_p95: 55,
      }],
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  await page.locator('#sc-y1').selectOption('tokens_in');
  await page.locator('#sc-y1-agg').selectOption('p50');

  // Add a second series charting the *same* measure at a different
  // percentile — this is the whole point of independent chips.
  await page.locator('#sc-add-series').click();
  const row = page.locator('.sc-extra-row').first();
  await expect(row).toBeVisible();
  await row.locator('.sc-extra-metric').selectOption('tokens_in');
  await row.locator('.sc-extra-agg').selectOption('p95');
  await page.locator('.sc-series-pill').click();
  await expect(page.locator('.sc-extra-row')).toHaveCount(0);
  await expect(page.locator('.sc-series-pill')).toContainText('P95 Tokens In · L');
  await page.locator('.sc-series-pill').click();
  await expect(page.locator('.sc-extra-row')).toBeVisible();

  await expect.poll(() => statsRequests.some(req => (
    req.chart_metrics === 'tokens_in,tokens_in' && req.chart_aggs === 'p50,p95'
  ))).toBe(true);

  // New chips default onto Y1's axis; toggling flips to the right axis
  // without a refetch (data for both series is already loaded).
  await expect(row.locator('.sc-extra-axis')).toHaveText('L');
  const requestsBeforeToggle = statsRequests.length;
  await row.locator('.sc-extra-axis').click();
  await expect(row.locator('.sc-extra-axis')).toHaveText('R');
  expect(statsRequests.length).toBe(requestsBeforeToggle);

  await expect.poll(() => page.evaluate(() => {
    const chart = window.Chart?.getChart(document.getElementById('stats-chart'));
    return chart?.data?.datasets?.[1]?.yAxisID;
  })).toBe('y2');
  await expect(page.locator('.sc-series-pill')).toContainText('P95 Tokens In · R');

  // Removing it drops the chip without a refetch.
  await page.locator('.sc-series-remove').click();
  await expect(page.locator('.sc-extra-row')).toHaveCount(0);
  await expect(page.locator('.sc-series-pill')).toHaveCount(0);
  expect(statsRequests.length).toBe(requestsBeforeToggle);
});

test('chart metric options are limited to whatever is checked in Measures', async ({ page }) => {
  await mockApp(page);
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    route.fulfill({
      json: [{
        period: '2026-07-10 10:00', sessions: 3, total_turns: 3,
        input_tokens: 60, output_tokens: 30, cost_usd: 0.02,
      }],
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  const y1Options = () => page.locator('#sc-y1 option').evaluateAll(opts => opts.map(o => o.value));

  // Defaults: Sessions/Turns/Tokens In/Tokens Out checked, Cost/Duration not
  // (Duration only ever has data in By Turn, so it isn't on by default here).
  await expect.poll(y1Options).toEqual(['turns', 'sessions', 'tokens_in', 'tokens_out']);
  await expect(page.locator('#sc-y1 option[value="cost"]')).toHaveCount(0);
  await expect(page.locator('#sc-y1 option[value="duration"]')).toHaveCount(0);
  await expect(page.locator('#sc-y1 option[value="tokens_total"]')).toHaveCount(0);

  // Checking Cost in Measures makes it selectable on the chart...
  await page.locator('#sf-measures-toggle').click();
  await page.locator('#sf-measures-menu input[value="cost"]').check();
  await page.locator('#sf-measures-menu input[value="tokens_total"]').check();
  await expect.poll(y1Options).toContain('cost');
  await expect.poll(y1Options).toContain('tokens_total');
  const requestsBeforeUncheck = statsRequests.length;

  // ...and unrelated toggles (Cost) don't touch the currently-charted metric
  // (Turns), so no refetch is needed — just a table re-render.
  await expect(page.locator('#sc-y1')).toHaveValue('turns');
  expect(statsRequests.length).toBe(requestsBeforeUncheck);

  // Unchecking the metric actually being charted (Turns) moves the chart to
  // the next selected measure and *does* require a refetch, since the
  // backend hasn't computed that metric's aggregate yet.
  await page.locator('#sf-measures-menu input[value="turns"]').uncheck();
  await expect(page.locator('#sc-y1')).toHaveValue('cost');
  await expect.poll(() => statsRequests.some(req => req.chart_metrics === 'cost')).toBe(true);
  await expect(page.locator('#sc-y1 option[value="turns"]')).toHaveCount(0);
});

test('stats chart aggregate controls reconcile a legacy preset whose chart metric is not a visible measure', async ({ page }) => {
  await mockApp(page);
  await page.unroute('**/stats/filter-presets');
  await page.route('**/stats/filter-presets', route => route.fulfill({
    json: [{
      id: 4,
      name: 'Legacy',
      is_default: true,
      state: {
        version: 1,
        time: { period: 'hourly', days: 7 },
        dimensions: {
          topic: { mode: 'auto_top', values: [] },
          agent: { mode: 'auto_top', values: [] },
          session_type: { mode: 'all', values: [] },
        },
        breakdown: { key: '' },
        // Older presets could save a chart metric ("tokens_in"/"cost") that
        // wasn't in the visible measures ("sessions"/"turns") — the chart is
        // now constrained to whatever's checked in Measures, so loading this
        // should fall back rather than chart something the table doesn't show.
        measure: { primary: 'tokens_in', secondary: 'cost', visible: ['sessions', 'turns'] },
      },
    }],
  }));
  await page.route('**/stats?**', route => route.fulfill({
    json: [{
      period: '2026-07-10 10:00',
      sessions: 1,
      total_turns: 1,
      input_tokens: 10,
      output_tokens: 5,
      cost_usd: 0.01,
    }],
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  // Y1 falls back to the first checked measure ("Turns"); its only agg is
  // SUM, so the agg picker stays visible with one option.
  await expect(page.locator('#sc-y1')).toHaveValue('turns');
  await expect(page.locator('#sc-y1-agg')).toBeVisible();
  await expect(page.locator('#sc-y1-agg option')).toHaveText(['SUM']);
  // The saved secondary series ("Cost") isn't checked either — dropped
  // rather than forced onto an unrelated selected measure.
  await expect(page.locator('.sc-extra-row')).toHaveCount(0);
});
