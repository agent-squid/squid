const { test, expect } = require('@playwright/test');

// The app registers a service worker (ui/sw.js). Playwright's page.route()
// can't intercept requests a service worker proxies, so without this the
// insights.json mock below is silently bypassed and tests hit the real
// https://agentsquid.ai over the network.
test.use({ serviceWorkers: 'block' });

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
  // The boot-message "insights" widget independently fires its own
  // /stats?period=weekly requests (with a week-over-week comparison anchor)
  // on every page load, regardless of the Stats tab. An empty measures list
  // makes resolveInsightMeasures() short-circuit before it ever calls /stats,
  // so those unrelated requests don't pollute statsRequests assertions here.
  // The trailing "*" is required: fetchInsights() cache-busts with a
  // "?_=<timestamp>" query string, which a bare "**/insights.json" pattern
  // (no wildcard after it) fails to match, silently falling through to a
  // real network request.
  await page.route('**/insights.json*', r => r.fulfill({ json: {} }));
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
  // Default is now By Turn — switch to hourly for aggregate tests.
  await page.locator('#sf-period').selectOption('hourly');

  const dayOptions = await page.locator('#sf-days option').evaluateAll(
    opts => opts.map(o => ({ value: o.value, text: o.textContent }))
  );
  expect(dayOptions).toEqual([
    { value: '1', text: '1d' },
    { value: '3', text: '3d' },
    { value: '7', text: '7d' },
    { value: '14', text: '14d' },
    { value: '28', text: '28d' },
    { value: '90', text: '90d' },
    { value: '0', text: 'All Time' },
  ]);

  await page.locator('#sf-days').selectOption('1');
  await expect.poll(() => statsRequests.some(req => req.days === '1')).toBe(true);

  await page.locator('#sf-days').selectOption('3');
  await expect.poll(() => statsRequests.some(req => req.days === '3')).toBe(true);
});

test('weekly period floors the days filter at 14d', async ({ page }) => {
  await mockApp(page);
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    route.fulfill({ json: [] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-period').selectOption('hourly');
  await page.locator('#sf-days').selectOption('3');

  await page.locator('#sf-period').selectOption('weekly');

  const dayOptions = await page.locator('#sf-days option').evaluateAll(
    opts => opts.map(o => ({ value: o.value, text: o.textContent }))
  );
  expect(dayOptions).toEqual([
    { value: '14', text: '14d' },
    { value: '28', text: '28d' },
    { value: '90', text: '90d' },
    { value: '0', text: 'All Time' },
  ]);
  // Switching to weekly while a sub-14d range (3d) was selected snaps to 14d.
  await expect(page.locator('#sf-days')).toHaveValue('14');
  await expect.poll(() => statsRequests.some(req => req.period === 'weekly' && req.days === '14')).toBe(true);
});

test('Overview turns chart falls back from null chart field to total_turns', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats?**', route => route.fulfill({
    json: [{
      period: '2026-07-10 10:00',
      sessions: 3,
      total_turns: 7,
      chart_turns_sum: null,
      input_tokens: 100,
      output_tokens: 20,
    }],
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#stats-preset-select').selectOption('__overall');
  await expect(page.locator('#stats-content table')).toBeVisible();

  await expect.poll(() => page.evaluate(() => {
    const chart = window.Chart?.getChart(document.getElementById('stats-chart'));
    return chart?.data?.datasets?.[0]?.data || null;
  })).toEqual([7]);
});

test('anchor control sets a fixed end-of-window timestamp and can reset to Now', async ({ page }) => {
  await mockApp(page);
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    route.fulfill({ json: [] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  await expect(page.locator('#sf-anchor-toggle')).toHaveText('Now');
  await page.locator('#sf-anchor-toggle').click();
  await page.locator('#sf-anchor-input').fill('2026-07-10 09:30:12');
  await page.locator('#sf-anchor-apply').click();

  // Menu closes and the toggle reflects the picked timestamp.
  await expect(page.locator('#sf-anchor-menu')).toBeHidden();
  await expect(page.locator('#sf-anchor-toggle')).toHaveText('09:30:12');
  await expect(page.locator('#sf-anchor-toggle')).not.toContainText('AM');
  await expect(page.locator('#sf-anchor-toggle')).not.toContainText('PM');
  await expect(page.locator('#sf-anchor-toggle')).not.toContainText('Jul');
  await expect.poll(() => statsRequests.some(req => req.anchor)).toBe(true);
  const anchoredReq = statsRequests.find(req => req.anchor);
  expect(anchoredReq.anchor).toContain('2026-07-10');

  // Reopening shows the full date/time in the picker.
  await page.locator('#sf-anchor-toggle').click();
  await expect(page.locator('#sf-anchor-input')).toHaveValue('2026-07-10 09:30:12');

  // Reset to Now clears the anchor param entirely.
  await page.locator('#sf-anchor-clear').click();
  await expect(page.locator('#sf-anchor-toggle')).toHaveText('Now');
  await expect.poll(() => statsRequests[statsRequests.length - 1].anchor).toBeUndefined();
});

test('anchor control has mobile spacing and themed datetime input', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await mockApp(page);
  await page.route('**/stats?**', route => route.fulfill({ json: [] }));

  await page.goto('/');
  await page.evaluate(() => navigateView('stats'));
  await expect(page.locator('#view-stats')).toHaveClass(/active/);

  await page.locator('#sf-anchor-toggle').click();
  await page.locator('#sf-anchor-input').fill('2026-07-10 09:30:12');
  await page.locator('#sf-anchor-apply').click();

  const anchorBox = await page.locator('#sf-anchor-toggle').boundingBox();
  const daysBox = await page.locator('#sf-days').boundingBox();
  const breakdownBox = await page.locator('#sf-breakdown').boundingBox();
  expect(anchorBox).not.toBeNull();
  expect(daysBox).not.toBeNull();
  expect(breakdownBox).not.toBeNull();
  expect(anchorBox.height).toBeLessThanOrEqual(daysBox.height + 2);
  expect(breakdownBox.x - (anchorBox.x + anchorBox.width)).toBeGreaterThanOrEqual(6);

  await page.locator('#sf-anchor-toggle').click();
  const anchorMenuStyles = await page.locator('#sf-anchor-menu').evaluate(el => {
    const style = getComputedStyle(el);
    return {
      background: style.backgroundColor,
      border: style.borderColor,
      radius: style.borderRadius,
      fontSize: getComputedStyle(el.querySelector('.sf-anchor-datetime-label')).fontSize,
      padding: style.padding,
    };
  });
  expect(anchorMenuStyles).toEqual({
    background: 'rgb(21, 21, 29)',
    border: 'rgb(52, 52, 74)',
    radius: '6px',
    fontSize: '12.48px',
    padding: '7.2px',
  });
  const inputStyles = await page.locator('#sf-anchor-input').evaluate(el => {
    const style = getComputedStyle(el);
    return {
      background: style.backgroundColor,
      border: style.borderColor,
      color: style.color,
    };
  });
  expect(inputStyles.background).not.toBe('rgb(255, 255, 255)');
  expect(inputStyles.border).not.toBe('rgb(255, 255, 255)');
});

test('session type filter highlights when scoped to session or adhoc', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats?**', route => route.fulfill({ json: [] }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  await expect(page.locator('#sf-adhoc')).not.toHaveClass(/active/);
  await page.locator('#sf-adhoc').selectOption('session');
  await expect(page.locator('#sf-adhoc')).toHaveClass(/active/);
  await page.locator('#sf-adhoc').selectOption('adhoc');
  await expect(page.locator('#sf-adhoc')).toHaveClass(/active/);
  await page.locator('#sf-adhoc').selectOption('all');
  await expect(page.locator('#sf-adhoc')).not.toHaveClass(/active/);
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
  await page.locator('#sf-period').selectOption('hourly');
  await expect(page.locator('#stats-content table')).toBeVisible();

  // Uncheck all but the basic four so column indices are predictable.
  await page.locator('#sf-measures-toggle').click();
  for (const key of ['avg_tokens_turn', 'cache_hit_rate', 'cache_read', 'cache_write', 'duration', 'new_input', 'tokens_total']) {
    await page.locator(`#sf-measures-menu input[value="${key}"]`).uncheck();
  }
  // Close the measures menu so the next toggle-click opens it again.
  await page.locator('#sf-measures-toggle').click();

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
  await expect(totals.nth(2)).toHaveText('—');  // cache_hit_rate uses avg agg, no meaningful total
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
	  'Bad Responses',
	  'Cache Hit %',
	  'Cache Read',
	  'Cache Write',
    'Cancelled',
    'Cost',
    'Duration',
    'Errors',
    'New Input',
    'Quota Delta',
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
  await expect.poll(() => page.locator('#stats-turn-popup').evaluate(el => el.scrollHeight > el.clientHeight)).toBe(true);
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
  await page.locator('.sc-series-pill').first().click();
  await page.locator('.sc-extra-row .sc-extra-metric').selectOption('cache_hit_rate');

  await expect.poll(() => statsRequests.at(-1)?.chart_metrics).toBe('cache_hit_rate');
  expect(statsRequests.at(-1).chart_aggs).toBe('avg');
});

test('bad responses measure requests marked bad count', async ({ page }) => {
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
        marked_bad: 1,
        chart_marked_bad_sum: 1,
      }],
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-measures-toggle').click();
  await expect(page.locator('#sf-measures-menu input[value="marked_bad"]')).toBeVisible();
  await page.locator('#sf-measures-menu input[value="marked_bad"]').check();
  await page.locator('.sc-series-pill').first().click();
  await page.locator('.sc-extra-row .sc-extra-metric').selectOption('marked_bad');

  await expect.poll(() => statsRequests.at(-1)?.chart_metrics).toBe('marked_bad,cache_hit_rate');
  expect(statsRequests.at(-1).chart_aggs).toBe('sum,avg');
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
  await page.locator('#sf-period').selectOption('hourly');

  await expect(page.locator('.sc-series-pill').first()).toContainText('SUM Tokens In · L');

  await page.locator('.sc-series-pill').first().click();
  await page.locator('.sc-extra-row .sc-extra-metric').selectOption('tokens_in');
  await page.locator('.sc-extra-row .sc-extra-agg').selectOption('p50');
  await page.locator('.sc-series-remove').nth(1).click();
  await expect(page.locator('#stats-content thead th')).toContainText([
    'Hour',
    'Turns',
    'Sessions',
    'P50 Tokens In',
    'Tokens Out',
  ]);
  const p50TokensCol = await page.locator('#stats-content thead th').evaluateAll(ths =>
    ths.findIndex(th => th.textContent.trim() === 'P50 Tokens In')
  );
  await expect(page.locator('#stats-content tbody tr').first().locator('td').nth(p50TokensCol)).toHaveText('20');
  await expect(page.locator('#stats-content tfoot tr').first().locator('td').nth(p50TokensCol)).toHaveText('—');
  await expect.poll(() => statsRequests.some(req => {
    const metrics = (req.chart_metrics || '').split(',');
    const aggs = (req.chart_aggs || '').split(',');
    return metrics.some((metric, i) => metric === 'tokens_in' && aggs[i] === 'p50');
  })).toBe(true);
  await page.locator('.sc-extra-row .sc-extra-agg').selectOption('avg');
  const avgTokensCol = await page.locator('#stats-content thead th').evaluateAll(ths =>
    ths.findIndex(th => th.textContent.trim() === 'AVG Tokens In')
  );
  expect(avgTokensCol).toBeGreaterThan(-1);
  await expect(page.locator('#stats-content tbody tr').first().locator('td').nth(avgTokensCol)).toHaveText('15');
  await page.locator('.sc-extra-row .sc-extra-agg').selectOption('p50');

  await page.locator('#sc-add-series').click();
  await expect(page.locator('.sc-series-pill')).toHaveCount(2);
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
  await expect(page.locator('.sc-series-pill')).toHaveCount(1);
  await expect(page.locator('.sc-series-pill').first()).toContainText('P50 Tokens In · L');
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
  await page.locator('#sf-period').selectOption('hourly');

  await page.locator('#sf-measures-toggle').click();
  await page.locator('#sf-measures-menu input[value="new_input"]').check();

  await page.locator('.sc-series-pill').first().click();
  await page.locator('.sc-extra-row .sc-extra-metric').selectOption('tokens_in');
  await page.locator('.sc-series-remove').nth(1).click();
  await page.locator('#sc-add-series').click();
  const newInputRow = page.locator('.sc-extra-row').first();
  await newInputRow.locator('.sc-extra-metric').selectOption('new_input');
  await newInputRow.locator('.sc-extra-agg').selectOption('p95');
  await page.keyboard.press('Escape');
  await expect(page.locator('.sc-extra-row')).toHaveCount(0);
  await expect(page.locator('.sc-series-pill').nth(1)).toContainText('P95 New Input · L');
  await expect(page.locator('#stats-content thead th')).toContainText([
    'Hour',
    'Turns',
    'P95 New Input',
    'Sessions',
    'Tokens In',
    'Tokens Out',
  ]);
  const p95NewInputCol = await page.locator('#stats-content thead th').evaluateAll(ths =>
    ths.findIndex(th => th.textContent.trim() === 'P95 New Input')
  );
  await expect(page.locator('#stats-content tbody tr').first().locator('td').nth(p95NewInputCol)).toHaveText('125');
  await expect(page.locator('#stats-content tfoot tr').first().locator('td').nth(p95NewInputCol)).toHaveText('—');

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
  await page.locator('#sf-period').selectOption('hourly');

  await page.locator('.sc-series-pill').first().click();
  await page.locator('.sc-extra-row .sc-extra-metric').selectOption('tokens_in');
  await page.locator('.sc-extra-row .sc-extra-agg').selectOption('p50');
  await page.locator('.sc-series-remove').nth(1).click();

  // Add a second series charting the *same* measure at a different
  // percentile — this is the whole point of independent chips.
  await page.locator('#sc-add-series').click();
  const row = page.locator('.sc-extra-row').first();
  await expect(row).toBeVisible();
  await row.locator('.sc-extra-metric').selectOption('tokens_in');
  await row.locator('.sc-extra-agg').selectOption('p95');
  await page.locator('.sc-series-pill').nth(1).click();
  await expect(page.locator('.sc-extra-row')).toHaveCount(0);
  await expect(page.locator('.sc-series-pill').nth(1)).toContainText('P95 Tokens In · L');
  await page.locator('.sc-series-pill').nth(1).click();
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
  await expect(page.locator('.sc-series-pill').nth(1)).toContainText('P95 Tokens In · R');

  // Removing it drops the chip without a refetch.
  await page.locator('.sc-series-remove').nth(1).click();
  await expect(page.locator('.sc-extra-row')).toHaveCount(0);
  await expect(page.locator('.sc-series-pill')).toHaveCount(1);
  expect(statsRequests.length).toBe(requestsBeforeToggle);
});

test('duration measure defaults to average in aggregate stats', async ({ page }) => {
  await mockApp(page);
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    const params = Object.fromEntries(url.searchParams.entries());
    statsRequests.push(params);
    route.fulfill({
      json: [{
        period: '2026-07-10 10:00',
        sessions: 2,
        total_turns: 2,
        input_tokens: 60,
        output_tokens: 30,
        duration_ms: 6500,
        chart_duration_avg: 6.5,
      }],
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-period').selectOption('hourly');
  await page.locator('#sf-measures-toggle').click();
  await page.locator('#sf-measures-menu input[value="duration"]').check();
  await expect(page.locator('#stats-content thead th')).toContainText(['AVG Duration']);
  await expect(page.locator('#stats-content tbody')).toContainText('6.5s');

  await page.locator('.sc-series-pill').first().click();
  await page.locator('.sc-extra-row .sc-extra-metric').selectOption('duration');
  await expect(page.locator('.sc-extra-row .sc-extra-agg')).toHaveValue('avg');
  await expect.poll(() => statsRequests.some(req => (
    (req.chart_metrics || '').split(',').includes('duration') &&
    (req.chart_aggs || '').split(',').includes('avg')
  ))).toBe(true);
  await expect(page.locator('#stats-content thead th')).toContainText(['AVG Duration']);
  await expect(page.locator('#stats-content tbody')).toContainText('6.5s');
});

test('chart metric options are independent from table Measures', async ({ page }) => {
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
  await page.locator('#sf-period').selectOption('hourly');

  await page.locator('.sc-series-pill').first().click();
  const metricOptions = () => page.locator('.sc-extra-row .sc-extra-metric option').evaluateAll(opts => opts.map(o => o.value));

  await expect.poll(metricOptions).toContain('cost');
  await expect.poll(metricOptions).toContain('duration');
  await expect.poll(metricOptions).toContain('tokens_total');

  await page.locator('#sf-measures-toggle').click();
  await page.locator('#sf-measures-menu input[value="cost"]').check();
  await page.locator('#sf-measures-menu input[value="tokens_total"]').check();
  const requestsBeforeUncheck = statsRequests.length;

  await expect(page.locator('.sc-series-pill').first()).toContainText('SUM Tokens In · L');
  expect(statsRequests.length).toBe(requestsBeforeUncheck);

  // Table measure changes no longer mutate chart series.
  await page.locator('#sf-measures-menu input[value="turns"]').uncheck();
  await expect(page.locator('.sc-series-pill').first()).toContainText('SUM Tokens In · L');
  expect(statsRequests.length).toBe(requestsBeforeUncheck);
});

test('stats chart aggregate controls keep legacy preset chart metrics even when table-hidden', async ({ page }) => {
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
        // wasn't in the visible measures ("sessions"/"turns"). Chart series
        // are now independent from table measures, so keep those metrics.
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

  await expect(page.locator('.sc-series-pill').first()).toContainText('SUM Tokens In · L');
  await expect(page.locator('.sc-series-pill').nth(1)).toContainText('SUM Cost ($) · R');
  await expect(page.locator('.sc-extra-row')).toHaveCount(0);
});

test('response status filter is a multiselect that scopes the stats request', async ({ page }) => {
  await mockApp(page);
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    route.fulfill({ json: [] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  await expect(page.locator('#sf-status-toggle')).toHaveText('All Status');
  await expect(page.locator('#sf-status-toggle')).not.toHaveClass(/active/);
  await expect(page.locator('#sf-status-menu')).toBeHidden();

  await page.locator('#sf-status-toggle').click();
  await expect(page.locator('#sf-status-menu')).toBeVisible();
  await expect(page.locator('#sf-status-menu label')).toHaveText([
    'Complete', 'Error', 'Cancelled', 'Shell',
  ]);
  await page.locator('#sf-status-menu input[value="done"]').check();
  await expect(page.locator('#sf-status-toggle')).toHaveText('Complete');
  await expect(page.locator('#sf-status-toggle')).toHaveClass(/active/);
  await expect.poll(() => statsRequests.at(-1)?.status).toBe('done');

  await page.locator('#sf-status-menu input[value="cancelled"]').check();
  await expect(page.locator('#sf-status-toggle')).toHaveText('2 Statuses');
  // Value order follows menu (DOM) order — done, error, cancelled — not click order.
  await expect.poll(() => statsRequests.at(-1)?.status).toBe('done,cancelled');

  // Selecting three still reports an explicit selection.
  // it as an explicit selection rather than silently reverting to "All".
  await page.locator('#sf-status-menu input[value="error"]').check();
  await expect(page.locator('#sf-status-toggle')).toHaveText('3 Statuses');
  await expect.poll(() => statsRequests.at(-1)?.status).toBe('done,error,cancelled');

  await page.locator('#sf-status-menu input[value="shell"]').check();
  await expect(page.locator('#sf-status-toggle')).toHaveText('4 Statuses');
  await expect.poll(() => statsRequests.at(-1)?.status).toBe('done,error,cancelled,shell');

  await page.locator('#sf-status-menu input[value="done"]').uncheck();
  await page.locator('#sf-status-menu input[value="cancelled"]').uncheck();
  await page.locator('#sf-status-menu input[value="error"]').uncheck();
  await page.locator('#sf-status-menu input[value="shell"]').uncheck();
  await expect(page.locator('#sf-status-toggle')).toHaveText('All Status');
  await expect(page.locator('#sf-status-toggle')).not.toHaveClass(/active/);
  await expect.poll(() => statsRequests.at(-1)?.status).toBeUndefined();

  // Menu is still open from checking/unchecking above — clicking outside
  // closes it, matching the topic/agent multiselects.
  await expect(page.locator('#sf-status-menu')).toBeVisible();
  await page.locator('#stats-content').click();
  await expect(page.locator('#sf-status-menu')).toBeHidden();
});

test('flow filter scopes the stats request', async ({ page }) => {
  await mockApp(page);
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(Object.fromEntries(url.searchParams.entries()));
    route.fulfill({ json: [] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();

  await expect(page.locator('#sf-flow')).toHaveValue('all');
  await expect(page.locator('#sf-flow')).not.toHaveClass(/active/);
  await expect.poll(() => statsRequests.at(-1)?.flow).toBeUndefined();

  await page.locator('#sf-flow').selectOption('single');
  await expect(page.locator('#sf-flow')).toHaveClass(/active/);
  await expect.poll(() => statsRequests.at(-1)?.flow).toBe('single');

  await page.locator('#sf-flow').selectOption('multi');
  await expect.poll(() => statsRequests.at(-1)?.flow).toBe('multi');

  await page.locator('#sf-flow').selectOption('all');
  await expect(page.locator('#sf-flow')).not.toHaveClass(/active/);
  await expect.poll(() => statsRequests.at(-1)?.flow).toBeUndefined();
});
