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
  expect(periodOptions).toEqual(['By Turn', 'Hourly', 'Daily']);

  await expect(page.locator('#sf-measures-menu input[value="sessions"]')).toBeChecked();
  await expect(page.locator('#sf-measures-menu input[value="turns"]')).toBeChecked();

  await page.locator('#sf-period').selectOption('turn');

  await expect.poll(() => statsRequests.some(req => req.period === 'turn')).toBe(true);
  const lastReq = statsRequests[statsRequests.length - 1];
  expect(lastReq.breakdown).toBeUndefined();

  // Breakdown doesn't apply to a raw per-turn view — disable it rather than
  // let users pick a mode that would silently be ignored server-side.
  await expect(page.locator('#sf-breakdown')).toBeDisabled();

  // Sessions are redundant in this view, but Turns stays on because it links
  // to the underlying response.
  await expect(page.locator('#sf-measures-menu input[value="sessions"]')).not.toBeChecked();
  await expect(page.locator('#sf-measures-menu input[value="turns"]')).toBeChecked();

  // Turns stays available as a response link column, but it is not the
  // default chart series in By Turn because every row would plot as 1.
  await expect(page.locator('#sc-y1')).toHaveValue('tokens_in');

  // Each row is already one turn, not a time bucket — sum/avg/min/max of a
  // single value are identical, so the agg picker stays in place with one RAW
  // option and the chart legend says "RAW", not silently plotting a
  // stale/absent aggregate as zero.
  await expect(page.locator('#sc-y1-agg')).toBeVisible();
  await expect(page.locator('#sc-y1-agg option')).toHaveText(['RAW']);
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

  await expect.poll(() => page.locator('.stats-table-scroll').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
});
