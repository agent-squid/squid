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

test.describe('stats view — mobile', () => {
  test.use({ viewport: { width: 390, height: 844 } }); // iPhone 14 size

  test('By Turn table stays compact and unscrolled with a small measure count', async ({ page }) => {
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

    // The Deep Dive default already selects most measures, so explicitly
    // pare down to a small set (turns, sessions, tokens_in, tokens_out) to
    // exercise the compact/unscrolled path.
    await page.locator('#sf-measures-toggle').click();
    for (const v of ['avg_tokens_turn', 'cache_hit_rate', 'cache_read', 'cache_write', 'duration', 'new_input', 'tokens_total']) {
      await page.locator(`#sf-measures-menu input[value="${v}"]`).uncheck();
    }
    await page.locator('#sf-measures-toggle').click();

    await expect(page.locator('.stats-turn-table')).not.toHaveClass(/stats-turn-table-wide/);
    await expect.poll(() => page.locator('.stats-table-scroll').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  });

  test('By Turn table grows and scrolls instead of crushing columns with the default (many-measure) Deep Dive view', async ({ page }) => {
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

    // No manual measure toggling: the Deep Dive default view itself already
    // selects most measures, and this exact case is what regressed before.
    await expect(page.locator('.stats-turn-table')).toHaveClass(/stats-turn-table-wide/);
    const scroll = page.locator('.stats-table-scroll');
    await expect.poll(() => scroll.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);

    // Columns must actually be readable-sized in the scrollable state, not
    // just technically wider than 0 — this is the crushed-column regression.
    const headerWidths = await page.locator('.stats-turn-table thead th').evaluateAll(
      ths => ths.map(th => th.getBoundingClientRect().width)
    );
    for (const w of headerWidths) expect(w).toBeGreaterThan(30);
  });
});
