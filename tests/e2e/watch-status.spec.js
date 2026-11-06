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


test('proposals and watches across topics show targets, jobs and pause controls', async ({ page }) => {
  await mockIdleHost(page);
  let paused = false;
  await page.route('**/watch-status', route => route.fulfill({ json: [
    { ...watch, status: paused ? 'paused' : 'active', version: paused ? 2 : 1,
      pause_reason: paused ? 'manual' : null, latest_msg_id: 42, latest_run_state: 'succeeded' },
    { id: 'proposal', topic: 'other', target: '<new PR>', status: 'proposed', version: 0,
      instructions: '<review feedback>', monitoring: true },
  ] }));
  await page.route('**/watch-status/external-watch/pause', route => {
    expect(route.request().postDataJSON()).toEqual({ version: 1 });
    paused = true;
    return route.fulfill({ json: { paused: true } });
  });
  await page.goto('/');
  await page.locator('#proc-status').click();
  const popup = page.locator('#proc-status-popup');
  await expect(popup).toContainText('Watches (2)');
  await expect(popup).toContainText('#other');
  await expect(popup).toContainText('<new PR>');
  await expect(popup).toContainText('<review feedback>');
  await expect(popup).toContainText('Awaiting activation');
  await expect(popup.getByRole('button', { name: 'succeeded' })).toHaveAttribute('data-msgid', '42');
  await popup.getByRole('button', { name: 'Pause watch', exact: true }).click();
  await expect(popup).toContainText('Paused');
  await expect(popup.getByRole('button', { name: 'Pause watch', exact: true })).toHaveCount(0);
  await expect(page.locator('#proc-status')).not.toHaveClass(/has-watches/);
  await expect(popup.getByRole('button', { name: 'Cancel watch', exact: true })).toHaveCount(2);
});


test('monitor bubble restores its snapshot, updates in place and links a separate job', async ({ page }) => {
  await mockIdleHost(page);
  let status = 'active';
  const anchor = { id: 101, topic: 'squid', source: 'monitor', status: 'done',
    content: JSON.stringify({ watch_id: 'monitor-1', target: 'Bug inbox', status: 'proposed' }) };
  await page.route('**/history?*', r => r.fulfill({ json: { items: [anchor], has_more: false, total: 1 } }));
  await page.route('**/watch-status*', r => r.fulfill({ json: [{ ...watch, id: 'monitor-1',
    target: 'Bug inbox', status, monitor_msg_id: 101, last_checked_at: 100,
    latest_msg_id: 102, latest_run_state: 'running', monitoring: true,
  }] }));
  await page.goto('/');
  const monitor = page.locator('.monitor-bubble[data-msg-id="101"]');
  await expect(monitor).toHaveCount(1);
  await expect(monitor).toContainText('Monitoring · waiting');
  await expect(monitor).toContainText('Last check:');
  await page.evaluate(() => {
    const job = makeWipBubble({ id: 102, topic: 'squid', agent: 'coder', prompt: 'Fix the bug' });
    document.querySelector('#messages').appendChild(job);
    window.jumpToStatusMessage = async id => { window.monitorJumpTarget = id; };
  });
  await expect(page.locator('.msg-thinking[data-msg-id="102"]')).toHaveCount(1);
  await monitor.getByRole('button', { name: 'Latest job · running' }).click();
  await expect.poll(() => page.evaluate(() => window.monitorJumpTarget)).toBe(102);
  await page.locator('#proc-status').click();
  await page.locator('#proc-status-popup tr[data-jump-msgid="101"]').click();
  await expect.poll(() => page.evaluate(() => window.monitorJumpTarget)).toBe(101);
  status = 'paused';
  await page.evaluate(() => refreshWatchStatus());
  await expect(monitor).toContainText('Paused');
  await expect(monitor).toHaveCount(1);
  await page.reload();
  await expect(monitor).toContainText('Paused');
  status = 'cancelled';
  await page.evaluate(() => refreshWatchStatus());
  await expect(monitor).toContainText('Cancelled');
  await expect(monitor.getByRole('button', { name: 'Cancel monitor' })).toHaveCount(0);
});

test('mobile status keeps monitor actions reachable by scrolling', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockIdleHost(page);
  await page.route('**/watch-status', r => r.fulfill({ json: [{ ...watch,
    target: 'https://github.com/agent-squid/squid/issues/123', latest_msg_id: 42, latest_run_state: 'succeeded',
  }] }));
  await page.goto('/');
  await page.locator('#proc-status').click();
  const popup = page.locator('#proc-status-popup');
  const cancel = popup.getByRole('button', { name: 'Cancel watch', exact: true });
  await cancel.scrollIntoViewIfNeeded();
  const rect = await cancel.boundingBox();
  expect(rect.x).toBeGreaterThanOrEqual(0);
  expect(rect.x + rect.width).toBeLessThanOrEqual(390);
  await expect(popup).toHaveCSS('overflow-x', 'auto');
});
