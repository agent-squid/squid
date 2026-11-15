const { test, expect } = require('@playwright/test');

for (const view of ['topics', 'agents', 'settings', 'files', 'stats']) {
  test(`${view} uses the shared load error and retries`, async ({ page }) => {
    let fail = false;
    let failedRequests = 0;
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (/\.(js|css|png|jpg|woff2?)$/.test(url.pathname) || url.pathname === '/') return route.continue();
      if (url.pathname === '/health') return route.fulfill({ json: { status: 'ok', backends: {} } });
      if (url.pathname === '/history') return route.fulfill({ json: { items: [], has_more: false } });
      if (fail) { failedRequests++; return route.abort('connectionrefused'); }
      return route.fulfill({ json: [] });
    });
    await page.goto('/');
    await expect(page.locator('#connection-status')).toContainText('Up to date');
    fail = true;
    await page.evaluate(view => switchView(view), view);
    const panel = page.locator('#connection-banner');
    await expect(panel).toBeVisible();
    await expect(panel.locator('#connection-status')).toContainText(`Unable to load ${view}.`);
    await expect(panel.locator('#connection-status')).toContainText('Check your connection and try again.');
    await expect(page.locator('#load-errors')).toBeHidden();
    const rect = await panel.boundingBox();
    const header = await page.locator('#topbar').boundingBox();
    expect(rect.y).toBeGreaterThanOrEqual(header.y + header.height - 1);
    expect(rect.y - header.y - header.height).toBeLessThan(20);
    expect(rect.x).toBeGreaterThanOrEqual(0);
    expect(rect.x + rect.width).toBeLessThanOrEqual(391);
    const before = failedRequests;
    await panel.getByRole('button', { name: 'Retry' }).click();
    await expect.poll(() => failedRequests).toBeGreaterThan(before);
    await expect(panel).toBeVisible();
    // Simultaneous host and view failures still produce only one visible box.
    await page.route('**/health', route => route.abort('connectionrefused'));
    await page.evaluate(() => syncConnectionHistory());
    await expect(panel.locator('#connection-status')).toContainText('Squid unreachable');
    await expect(page.locator('#load-errors')).toBeHidden();
    await expect(page.locator('.load-error:visible')).toHaveCount(0);
  });
}

test('cached topics keep content alongside the global offline warning and recover', async ({ page }) => {
  let offline = false;
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (/\.(js|css|png|jpg|woff2?)$/.test(url.pathname) || url.pathname === '/') return route.continue();
    if (url.pathname === '/health') return offline ? route.abort('connectionrefused') : route.fulfill({ json: { status: 'ok', backends: {} } });
    if (url.pathname === '/history') return route.fulfill({ json: { items: [], has_more: false } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Up to date');
  await page.evaluate(() => switchView('topics'));
  await expect(page.locator('#topics-list')).toHaveText('No topics found.');
  offline = true;
  await page.evaluate(() => { switchView('chat'); switchView('topics'); });
  await expect(page.locator('#connection-banner')).toBeVisible();
  await expect(page.locator('#connection-status')).toContainText('Squid unreachable');
  await expect(page.locator('#topics-list')).toHaveText('No topics found.');
  await expect(page.locator('#load-errors')).toBeHidden();
  expect(await page.locator('#connection-banner').evaluate(el => el.parentElement.id)).toBe('app');
  await page.locator('#connection-help').click();
  await expect(page.getByRole('dialog', { name: 'Connection help' })).toBeVisible();
  await page.keyboard.press('Escape');
  offline = false;
  await page.locator('#connection-retry').click();
  await expect(page.locator('#connection-banner')).toBeHidden();
  await expect(page.locator('#connection-snackbar')).toHaveText('Back online');
  await expect(page.locator('#topics-list')).toHaveText('No topics found.');
});
