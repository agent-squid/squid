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

test('By Turn shows one row per response with its own completion time, no bucketing', async ({ page }) => {
  await mockApp(page);
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    const params = Object.fromEntries(url.searchParams.entries());
    statsRequests.push(params);
    if (params.period !== 'turn') {
      return route.fulfill({ json: [{ period: '2026-07-10 10:00', sessions: 1, total_turns: 1, input_tokens: 10, output_tokens: 5 }] });
    }
    const agentFilter = url.searchParams.get('agent');
    const rowsData = [
      {
        msg_id: 2, period: '2026-07-10T10:05:00Z', topic: 'squid', agent: 'codex', adhoc: 0,
        sessions: 1, total_turns: 1, input_tokens: 200, output_tokens: 20,
        cost_usd: 0.2, duration_ms: 8000, quota_delta: 0.5,
      },
      {
        msg_id: 1, period: '2026-07-10T10:00:00Z', topic: 'squid', agent: 'codex', adhoc: 1,
        sessions: 1, total_turns: 1, input_tokens: 100, output_tokens: 10,
        cost_usd: 0.1, duration_ms: 5000, quota_delta: 1.0,
      },
    ];
    route.fulfill({ json: agentFilter ? rowsData.filter(r => r.agent === agentFilter) : rowsData });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  // By Turn is the smallest grain, so it sorts before Hourly and Daily.
  const periodOptions = await page.locator('#sf-period option').allTextContents();
  expect(periodOptions).toEqual(['By Turn', 'Hourly', 'Daily', 'Weekly']);

  await expect(page.locator('#sf-measures-menu input[value="sessions"]')).toBeChecked();
  await expect(page.locator('#sf-measures-menu input[value="turns"]')).toBeChecked();

  await page.locator('#sf-period').selectOption('turn');

  await expect.poll(() => statsRequests.some(req => req.period === 'turn')).toBe(true);
  const lastReq = statsRequests[statsRequests.length - 1];
  expect(lastReq.breakdown).toBeUndefined();

  // Breakdown doesn't apply to a raw per-turn view — disable it rather than
  // let users pick a mode that would silently be ignored server-side.
  await expect(page.locator('#sf-breakdown')).toBeDisabled();

  // Deep Dive starts with its full default measure set in By Turn.
  await expect(page.locator('#sf-measures-menu input[value="sessions"]')).toBeChecked();
  await expect(page.locator('#sf-measures-menu input[value="turns"]')).toBeChecked();

  // Turns stays available as a response link column, but it is not the
  // default chart series in By Turn because every row would plot as 1.
  await expect(page.locator('.sc-series-pill').first()).toContainText('RAW Tokens In · L');

  // Each row is already one turn, not a time bucket — sum/avg/min/max of a
  // single value are identical, so the agg picker stays in place with one RAW
  // option and the chart legend says "RAW", not silently plotting a
  // stale/absent aggregate as zero.
  await page.locator('.sc-series-pill').first().click();
  await expect(page.locator('.sc-extra-row .sc-extra-agg option')).toHaveText(['RAW']);
  await expect.poll(() => page.evaluate(() => {
    const chart = window.Chart?.getChart(document.getElementById('stats-chart'));
    return chart?.data?.datasets?.[0]?.label;
  })).toBe('RAW Tokens In');

  const rows = page.locator('#stats-content tbody tr');
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0).locator('.stats-turn-link')).toHaveText('1');
  await expect(rows.nth(0).locator('.stats-turn-link')).toHaveAttribute('data-turn-ids', '2');
  // Newest turn first, each with its own duration — not a shared/aggregated value.
  await expect(rows.nth(0)).toContainText('8.0s');
  await expect(rows.nth(1)).toContainText('5.0s');

  // Topic + agent + session-type collapse into one route dim, matching the
  // #topic@agent[!] scope notation used elsewhere in the app.
  await expect(page.locator('#stats-content thead th').nth(1)).toHaveText('Route');
  await expect(rows.nth(0)).toContainText('#squid@codex');
  await expect(rows.nth(0)).not.toContainText('#squid@codex!');
  await expect(rows.nth(1)).toContainText('#squid@codex!');

  // Time renders in the same compact MM-DD HH:MM style as the Hourly bucket
  // label (not the verbose "Jul 10, 10:05 AM" chat-style format).
  const timeCell = rows.nth(0).locator('td').nth(0);
  const routeCell = rows.nth(0).locator('td').nth(1);
  await expect(timeCell).toHaveText(/^\d{2}-\d{2} \d{2}:\d{2}$/);
  // Both the leading time and route columns are shrink-to-fit ("compact"),
  // and the route column overrides the table's default right-alignment.
  await expect(timeCell).toHaveClass(/stats-col-compact/);
  await expect(routeCell).toHaveClass(/stats-col-compact/);
  await expect(routeCell).toHaveCSS('text-align', 'left');

  // The route dim is just a display — the existing Agent filter still narrows the rows.
  await page.locator('#sf-agent-toggle').click();
  await page.locator('#sf-agent-menu input[value="codex"]').check();
  await expect.poll(() => statsRequests.some(req => req.period === 'turn' && req.agent === 'codex')).toBe(true);
  await expect(rows).toHaveCount(2);

  // Switching back to Hourly restores the usual defaults for that view.
  await page.locator('#sf-period').selectOption('hourly');
  await expect(page.locator('#sf-measures-menu input[value="sessions"]')).toBeChecked();
  await expect(page.locator('#sf-measures-menu input[value="turns"]')).toBeChecked();
});

test('By Turn offers sub-day ranges capped at 7d, other grains keep the full range', async ({ page }) => {
  await mockApp(page);
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    if (url.searchParams.get('period') !== 'turn') {
      return route.fulfill({ json: [{ period: '2026-07-10 10:00', sessions: 1, total_turns: 1, input_tokens: 10, output_tokens: 5 }] });
    }
    route.fulfill({ json: [] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-period').selectOption('hourly');

  await expect(page.locator('#sf-days option')).toHaveText(['1d', '3d', '7d', '14d', '28d', '90d', 'All Time']);

  // 90d is only meaningful for the hourly/daily/weekly grains - pick it here
  // so we can confirm By Turn doesn't inherit an out-of-range value from it.
  await page.locator('#sf-days').selectOption('90');

  await page.locator('#sf-period').selectOption('turn');
  await expect(page.locator('#sf-days option')).toHaveText(['1h', '3h', '6h', '12h', '1d', '3d', '7d']);
  // 90d isn't offered for By Turn, so the selection clamps down to the 7d cap
  // rather than leaving an option selected that no longer exists in the list.
  await expect(page.locator('#sf-days')).toHaveValue('7');

  await page.locator('#sf-days').selectOption({ label: '1h' });
  await expect.poll(() => statsRequests.some(req => req.period === 'turn' && req.hours === '1')).toBe(true);
  const hourReq = statsRequests.find(req => req.period === 'turn' && req.hours === '1');
  expect(hourReq.days).toBe('0');

  // Switching back out of By Turn drops the sub-day options and the hour selection.
  await page.locator('#sf-period').selectOption('hourly');
  await expect(page.locator('#sf-days option')).toHaveText(['1d', '3d', '7d', '14d', '28d', '90d', 'All Time']);
  await expect(page.locator('#sf-days')).toHaveValue('1');
});

test('By Turn default table fits mobile width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApp(page);
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('period') !== 'turn') {
      return route.fulfill({ json: [{ period: '2026-07-10 10:00', sessions: 1, total_turns: 1, input_tokens: 10, output_tokens: 5 }] });
    }
    return route.fulfill({
      json: [{
        msg_id: 2, period: '2026-07-10T10:05:00Z', topic: 'squid', agent: 'codex-with-long-name', adhoc: 0,
        sessions: 1, total_turns: 1, input_tokens: 200, output_tokens: 20, duration_ms: 8000,
      }],
    });
  });

  await page.goto('/');
  await page.evaluate(() => switchView('stats'));
  await page.locator('#sf-period').selectOption('turn');
  await page.waitForSelector('.stats-turn-link');

  // The Deep Dive default now selects most measures (see stats-mobile.spec.js),
  // so pare down to a small set to exercise the compact/unscrolled path.
  await page.locator('#sf-measures-toggle').click();
  for (const v of ['avg_tokens_turn', 'cache_hit_rate', 'cache_read', 'cache_write', 'duration', 'new_input', 'tokens_total']) {
    await page.locator(`#sf-measures-menu input[value="${v}"]`).uncheck();
  }
  await page.locator('#sf-measures-toggle').click();

  await expect.poll(() => page.locator('.stats-table-scroll').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
});

test('By Turn table scrolls horizontally on desktop when columns exceed viewport', async ({ page }) => {
  // Desktop viewport — the turn table must use width:max-content so it can
  // grow past the container and trigger horizontal scroll.  A plain width:100%
  // (the default for other stats tables) would crush all columns to fit, which
  // is the regression this test guards against.
  await page.setViewportSize({ width: 1280, height: 720 });
  await mockApp(page);
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('period') !== 'turn') {
      return route.fulfill({ json: [{ period: '2026-07-10 10:00', sessions: 1, total_turns: 1, input_tokens: 10, output_tokens: 5 }] });
    }
    return route.fulfill({
      json: [{
        msg_id: 2, period: '2026-07-10T10:05:00Z', topic: 'squid', agent: 'codex', adhoc: 0,
        sessions: 1, total_turns: 1, input_tokens: 200, output_tokens: 20, duration_ms: 8000,
        cache_read_tokens: 900000, cache_write_tokens: 20000, cost_usd: 12.34,
      }],
    });
  });

  await page.goto('/');
  await page.evaluate(() => switchView('stats'));
  await page.locator('#sf-period').selectOption('turn');
  await page.waitForSelector('.stats-turn-link');

  // Select enough extra measures that the row content must exceed the viewport.
  await page.locator('#sf-measures-toggle').click();
  for (const v of ['cost', 'cache_read', 'cache_write', 'cache_hit_rate']) {
    await page.locator(`#sf-measures-menu input[value="${v}"]`).check();
  }
  await page.locator('#sf-measures-toggle').click();

  const scroll = page.locator('.stats-table-scroll');
  await expect.poll(() => scroll.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);

  // Columns must stay readable — the crushed-column variant of this bug
  // would show scrollWidth == clientWidth with every column squeezed to ~0.
  const headerWidths = await page.locator('.stats-turn-table thead th').evaluateAll(
    ths => ths.map(th => th.getBoundingClientRect().width)
  );
  for (const w of headerWidths) expect(w).toBeGreaterThan(30);
});

test('By Turn footer has no colspan, so the frozen rail matches the body columns', async ({ page }) => {
  // Regression test: the tfoot used to open with <td colspan="2">Total</td>,
  // which shifted every later footer cell one position left of its matching
  // body column. The sticky-column CSS freezes by nth-child position, so
  // Avg Tokens/Turn's total ended up pinned in the rail meant for the Turns
  // total. Emitting two separate Time/Route cells keeps footer and body
  // column counts identical.
  await page.setViewportSize({ width: 1280, height: 720 });
  await mockApp(page);
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('period') !== 'turn') {
      return route.fulfill({ json: [{ period: '2026-07-10 10:00', sessions: 1, total_turns: 1, input_tokens: 10, output_tokens: 5 }] });
    }
    return route.fulfill({
      json: [{
        msg_id: 2, period: '2026-07-10T10:05:00Z', topic: 'squid', agent: 'codex', adhoc: 0,
        sessions: 1, total_turns: 3, input_tokens: 200, output_tokens: 20, duration_ms: 8000,
      }],
    });
  });

  await page.goto('/');
  await page.evaluate(() => switchView('stats'));
  await page.locator('#sf-period').selectOption('turn');
  await page.waitForSelector('.stats-turn-link');

  const bodyCellCount = await page.locator('.stats-turn-table tbody tr').first().locator('td').count();
  const footerCellCount = await page.locator('.stats-turn-table tfoot tr').locator('td').count();
  expect(footerCellCount).toBe(bodyCellCount);

  await expect(page.locator('.stats-turn-table thead th').nth(2)).toHaveText('Turns');
  await expect(page.locator('.stats-turn-table tfoot td').nth(2)).toHaveText('3');
});
