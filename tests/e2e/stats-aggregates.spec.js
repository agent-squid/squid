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

test('stats chart aggregate chips apply to Y1 and Y2', async ({ page }) => {
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
        chart_tokens_in_p50: 20,
        chart_tokens_in_p75: 30,
      }],
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  await expect(page.locator('#sc-y1')).toHaveValue('turns');
  await expect(page.locator('#sc-y1-agg')).toBeHidden();
  await expect(page.locator('#sc-y1-agg')).toHaveValue('sum');

  await page.locator('#sc-y1').selectOption('tokens_in');
  await expect(page.locator('#sc-y1-agg')).toBeVisible();
  await page.locator('#sc-y1-agg').selectOption('p50');
  await expect.poll(() => statsRequests.some(req => (
    req.chart_metric === 'tokens_in' && req.chart_agg === 'p50'
  ))).toBe(true);

  await page.locator('#sc-compare-btn').click();
  await page.locator('#sc-y2').selectOption('tokens_in');
  await expect(page.locator('#sc-y2-agg')).toBeVisible();
  await page.locator('#sc-y2-agg').selectOption('p75');

  await expect.poll(() => statsRequests.some(req => (
    req.chart_metric === 'tokens_in' &&
    req.chart_agg === 'p50' &&
    req.chart2_metric === 'tokens_in' &&
    req.chart2_agg === 'p75'
  ))).toBe(true);
  await expect.poll(() => page.evaluate(() => {
    const chart = window.Chart?.getChart(document.getElementById('stats-chart'));
    const datasets = chart?.data?.datasets || [];
    return datasets.length === 2 && datasets[0].borderColor !== datasets[1].borderColor;
  })).toBe(true);

  await page.locator('#sf-breakdown').selectOption('agent');
  await expect(page.locator('#stats-chart-controls')).toHaveClass(/breakdown-active/);
  await expect(page.locator('#sc-y1-agg')).toBeVisible();
  await expect(page.locator('#sc-y2-agg')).toBeHidden();
  await expect.poll(() => statsRequests.some(req => (
    req.breakdown === 'agent' &&
    req.chart_metric === 'tokens_in' &&
    req.chart_agg === 'p50'
  ))).toBe(true);
  await expect(page.locator('#stats-content tbody td.stats-series-col').first()).toHaveText('20');
});

test('stats chart aggregate controls load legacy preset measures', async ({ page }) => {
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

  await expect(page.locator('#sc-y1')).toHaveValue('tokens_in');
  await expect(page.locator('#sc-y1-agg')).toHaveValue('sum');
  await expect(page.locator('#sc-y2')).toHaveValue('cost');
  await expect(page.locator('#sc-y2-agg')).toHaveValue('sum');
  await expect(page.locator('#sc-y2-agg')).toBeVisible();
});
