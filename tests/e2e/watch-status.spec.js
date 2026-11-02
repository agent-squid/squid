const { test, expect } = require('@playwright/test');

test('watch job stays visible between polls and can be cancelled', async ({ page }) => {
  await page.route('**/health', route => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/topics', route => route.fulfill({ json: [] }));
  await page.route('**/config/agents', route => route.fulfill({ json: [] }));
  let cancelled = false;
  let polling = false;
  let status = 'active';
  await page.route('**/watch-status', route => route.fulfill({ json: cancelled ? [] : [{
    id: 'watch-test', topic: 'squid', target: '<TASK-1>', status, version: 3,
    next_poll: Date.now() / 1000 + 30, agent: 'coder', flow_route: '#squid@coder>@reviewer',
    instructions: '<Watch feedback>', polling, error: false, monitoring: true,
  }] }));
  await page.route('**/watch-status/watch-test/cancel', route => {
    expect(route.request().postDataJSON()).toEqual({ version: 3 });
    cancelled = true;
    return route.fulfill({ json: { cancelled: true } });
  });
  await page.route('**/processes', route => route.fulfill({ json: [] }));
  await page.route('**/queue', route => route.fulfill({ json: [] }));
  await page.goto('/');
  await page.locator('#proc-status').click();
  await expect(page.locator('#proc-status-popup')).toContainText('Watches');
  await expect(page.locator('#proc-status-popup')).toContainText('#squid@coder>@reviewer');
  await expect(page.locator('#proc-status-popup')).toContainText('<Watch feedback>');
  await expect(page.locator('#proc-status-popup')).toContainText(/Next \d+s/);
  await expect(page.locator('#proc-status')).toHaveClass(/has-watches/);
  polling = true;
  await page.evaluate(() => pollProcs());
  await expect(page.locator('#proc-status-popup td').filter({ hasText: /^Polling$/ })).toHaveCount(1);
  status = 'paused';
  await page.evaluate(() => pollProcs());
  await expect(page.locator('#proc-status-popup td').filter({ hasText: /^Paused$/ })).toHaveCount(1);
  await page.getByRole('button', { name: 'Cancel watch', exact: true }).click();
  await expect(page.locator('.proc-watch-stop')).toHaveCount(0);
  await expect(page.locator('#proc-status')).not.toHaveClass(/has-watches/);
});

async function mockIdleHost(page) {
  await page.route('**/health', route => route.fulfill({ json: { status: 'ok' } }));
  for (const path of ['topics', 'config/agents', 'processes', 'queue']) {
    await page.route(`**/${path}`, route => route.fulfill({ json: [] }));
  }
}

const watch = {
  id: 'external-watch', topic: 'squid', target: 'TASK-2', status: 'active', version: 1,
  next_poll: Date.now() / 1000 + 300, agent: 'coder', monitoring: true,
};

test('discovers externally activated watch after process polling goes idle', async ({ page }) => {
  await mockIdleHost(page);
  let active = false;
  await page.route('**/watch-status', route => route.fulfill({ json: active ? [watch] : [] }));
  await page.goto('/');
  await page.waitForFunction(() => watchStatusFresh && !watchStatusLoading && procPollInterval === null);
  // Isolate discovery from unrelated health/realtime callbacks restarting the
  // process loop. Only the independent watch timer can discover this job.
  await page.evaluate(() => { stopProcPoll(); startProcPoll = () => {}; });
  active = true;
  // Do not open the popup or manually refresh: background discovery owns this.
  await expect(page.locator('#proc-status')).toHaveClass(/has-watches/, { timeout: 20000 });
  await expect(page.locator('#proc-status-popup')).not.toHaveClass(/open/);
  await page.locator('#proc-status').click();
  await expect(page.locator('#proc-status-popup')).toContainText('TASK-2');
});

test('failed watch reads mark cached jobs offline and recovery restores them', async ({ page }) => {
  await mockIdleHost(page);
  let failure = null;
  await page.route('**/watch-status', route => {
    if (failure === 'network') return route.abort();
    if (failure === 'http') return route.fulfill({ status: 503, json: {} });
    if (failure === 'invalid') return route.fulfill({ json: {} });
    return route.fulfill({ json: [watch] });
  });
  await page.goto('/');
  await page.locator('#proc-status').click();
  await expect(page.locator('#proc-status')).toHaveClass(/has-watches/);
  for (const kind of ['network', 'http', 'invalid']) {
    failure = kind;
    await page.evaluate(() => refreshWatchStatus());
    await expect(page.locator('#proc-status')).not.toHaveClass(/has-watches/);
    await expect(page.locator('#proc-status-popup')).toContainText('TASK-2');
    await expect(page.locator('#proc-status-popup td').filter({ hasText: /^Offline$/ })).toHaveCount(1);
    failure = null;
    await page.evaluate(() => refreshWatchStatus());
    await expect(page.locator('#proc-status')).toHaveClass(/has-watches/);
    await expect(page.locator('#proc-status-popup td').filter({ hasText: /^Offline$/ })).toHaveCount(0);
  }
});
