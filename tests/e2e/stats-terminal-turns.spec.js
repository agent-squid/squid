const { test, expect } = require('@playwright/test');

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: {} }));
  await page.route('**/quota', r => r.fulfill({ json: {} }));
  await page.route('**/bookmarks', r => r.fulfill({ json: [] }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/stats/filters', r => r.fulfill({ json: { agents: ['codex'], topics: ['squid'] } }));
  await page.route('**/stats/filter-presets', r => r.fulfill({ json: [] }));
}

test('stats measures render cancelled and error turns', async ({ page }) => {
  await mockApp(page);

  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    return route.fulfill({ json: [
      {
        msg_id: 2, period: '2026-07-15T10:05:00Z', topic: 'squid', agent: 'codex', adhoc: 0,
        status: 'error', sessions: 1, total_turns: 1, done_turns: 0, error_turns: 1, cancelled_turns: 0,
        input_tokens: 0, output_tokens: 0,
      },
      {
        msg_id: 1, period: '2026-07-15T10:00:00Z', topic: 'squid', agent: 'codex', adhoc: 0,
        status: 'cancelled', sessions: 1, total_turns: 1, done_turns: 0, error_turns: 0, cancelled_turns: 1,
        input_tokens: 0, output_tokens: 0,
      },
    ] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-measures-toggle').click();
  await page.locator('#sf-measures-menu input[value="cancelled_turns"]').check();
  await page.locator('#sf-measures-menu input[value="error_turns"]').check();

  await expect(page.locator('#stats-content thead th')).toContainText(['Cancelled', 'Errors']);
  const rows = page.locator('#stats-content tbody tr');
  await expect(rows.nth(0)).toContainText('1');
  await expect(rows.nth(0)).toContainText('0');
  await expect(rows.nth(1)).toContainText('0');
  await expect(rows.nth(1)).toContainText('1');

  await page.locator('.sc-series-pill').first().click();
  await page.locator('.sc-extra-row .sc-extra-metric').selectOption('error_turns');
  await expect.poll(() => statsRequests.at(-1)?.chart_metrics?.split(',')[0]).toBe('error_turns');
});
