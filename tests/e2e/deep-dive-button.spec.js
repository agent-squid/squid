const { test, expect } = require('@playwright/test');

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: {} }));
  await page.route('**/quota', r => r.fulfill({ json: {} }));
  await page.route('**/stats/filters', r => r.fulfill({ json: { agents: ['claude'], topics: ['squid'] } }));
  await page.route('**/stats/filter-presets', r => r.fulfill({ json: [] }));
  await page.route('**/bookmarks', r => r.fulfill({ json: [] }));
}

test('deep dive button appears on history responses with stats', async ({ page }) => {
  await mockApp(page);

  await page.route('**/history**', r => r.fulfill({
    json: {
      items: [{
        id: 1,
        topic: 'squid',
        agent: 'claude',
        adhoc: false,
        prompt: 'test prompt',
        content: 'test response',
        timestamp: '2026-07-10T10:00:00Z',
        session_id: 'sid-1',
        stats: {
          input_tokens: 100,
          output_tokens: 50,
          duration_ms: 3000,
          cache_read: 200,
          cache_write: 50,
        },
      }],
      has_more: false,
    },
  }));

  // also mock the stats endpoint so the stats view works if navigated to
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('period') === 'turn') {
      return route.fulfill({
        json: [{
          msg_id: 1, period: '2026-07-10T10:00:00Z', topic: 'squid', agent: 'claude', adhoc: 0,
          sessions: 1, total_turns: 1, input_tokens: 100, output_tokens: 50, duration_ms: 3000,
        }],
      });
    }
    return route.fulfill({ json: [{ period: '2026-07-10 10:00', sessions: 1, total_turns: 1, input_tokens: 10, output_tokens: 5 }] });
  });

  await page.goto('/');

  // The deep dive button should appear on the stats row of the history item
  const deepDiveBtn = page.locator('.stats-deep-dive-btn');
  await expect(deepDiveBtn).toBeVisible();

  // Verify the button has the correct title
  await expect(deepDiveBtn).toHaveAttribute('title', 'Deep Dive by Turns');
});

test('deep dive button click navigates to stats by turn with filters', async ({ page }) => {
  await mockApp(page);

  const statsRequests = [];
  await page.route('**/history**', r => r.fulfill({
    json: {
      items: [{
        id: 1,
        topic: 'squid',
        agent: 'claude',
        adhoc: true,
        prompt: 'test prompt',
        content: 'test response',
        timestamp: '2026-07-10T10:00:00Z',
        session_id: 'sid-1',
        stats: {
          input_tokens: 100,
          output_tokens: 50,
          duration_ms: 3000,
        },
      }],
      has_more: false,
    },
  }));

  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    return route.fulfill({
      json: [{
        msg_id: 1, period: '2026-07-10T10:00:00Z', topic: 'squid', agent: 'claude', adhoc: 1,
        sessions: 1, total_turns: 1, input_tokens: 100, output_tokens: 50, duration_ms: 3000,
      }],
    });
  });

  await page.goto('/');

  // Click the deep dive button
  await page.locator('.stats-deep-dive-btn').click();

  // Should have navigated to the stats view
  await expect(page.locator('#view-stats')).toHaveClass(/active/);

  // Should be in By Turn mode
  await expect(page.locator('#sf-period')).toHaveValue('turn');

  // Should have the topic filter set to 'squid'
  await expect(page.locator('#sf-topic-toggle')).toContainText('squid');

  // Should have the agent filter set to 'claude'
  await expect(page.locator('#sf-agent-toggle')).toContainText('claude');

  // Should have the session type set to 'adhoc' (since the response was adhoc)
  await expect(page.locator('#sf-adhoc')).toHaveValue('adhoc');

  // Breakbown should be disabled in turn view
  await expect(page.locator('#sf-breakdown')).toBeDisabled();

  // Verify the stats query was made with correct params
  const turnReq = statsRequests.find(r => r.period === 'turn');
  expect(turnReq).toBeDefined();
  expect(turnReq.topic).toBe('squid');
  expect(turnReq.agent).toBe('claude');
  expect(turnReq.adhoc).toBe('adhoc');
  // 3h default range for Deep Dive
  expect(turnReq.days).toBe('0');
  expect(turnReq.hours).toBe('3');
  // Left axis: Input Tokens (sum), Right axis: Cache Hit % (avg)
  expect(turnReq.chart_metrics).toBe('tokens_in,cache_hit_rate');
  expect(turnReq.chart_aggs).toBe('sum,avg');
});

test('deep dive button click on session response sets session filter', async ({ page }) => {
  await mockApp(page);

  const statsRequests = [];
  await page.route('**/history**', r => r.fulfill({
    json: {
      items: [{
        id: 1,
        topic: 'default',
        agent: 'codex',
        adhoc: false,
        prompt: 'hello',
        content: 'hi there',
        timestamp: '2026-07-10T10:00:00Z',
        session_id: 'sid-1',
        stats: { input_tokens: 50, output_tokens: 25, duration_ms: 1500 },
      }],
      has_more: false,
    },
  }));

  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    return route.fulfill({
      json: [{
        msg_id: 1, period: '2026-07-10T10:00:00Z', topic: 'default', agent: 'codex', adhoc: 0,
        sessions: 1, total_turns: 1, input_tokens: 50, output_tokens: 25, duration_ms: 1500,
      }],
    });
  });

  await page.goto('/');

  await page.locator('.stats-deep-dive-btn').click();

  // Default topic should not set a topic filter
  const turnReq = statsRequests.find(r => r.period === 'turn');
  expect(turnReq.topic).toBeUndefined();

  // Agent filter should be 'codex'
  expect(turnReq.agent).toBe('codex');

  // Should be session type, not adhoc
  await expect(page.locator('#sf-adhoc')).toHaveValue('session');
});

test('stats view defaults to Deep Dive by Turns out of the box', async ({ page }) => {
  await mockApp(page);

  await page.route('**/stats?**', route => {
    return route.fulfill({
      json: [{ period: '2026-07-10 10:00', sessions: 1, total_turns: 1, input_tokens: 100, output_tokens: 50 }],
    });
  });

  // No presets exist (fresh install)
  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  // Period should default to 'turn' (Deep Dive by Turns)
  await expect(page.locator('#sf-period')).toHaveValue('turn');

  // Default range is 3h for Deep Dive
  await expect(page.locator('#sf-days')).toHaveValue('-3');
  // Turn view has 1h-7d options
  await expect(page.locator('#sf-days option')).toHaveText(['1h', '3h', '6h', '12h', '1d', '3d', '7d']);

  // Default chart: tokens_in (left Y1), cache_hit_rate (right Y2)
  await expect(page.locator('#sc-y1')).toHaveValue('tokens_in');
  await expect(page.locator('#sc-y1-agg')).toHaveValue('sum');
  // cache_hit_rate should be in extra series with y2 axis
  await expect(page.locator('#sc-extra')).toBeVisible();
});
