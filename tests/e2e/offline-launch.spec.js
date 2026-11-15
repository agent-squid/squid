const { test, expect } = require('@playwright/test');

// Real service worker and shell requests; only backend APIs are mocked.
test('offline cold launch runs Squid and reconnects without losing the draft', async ({ page, context }) => {
  let offline = false;
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (/\.(js|css|png|jpg|woff2?)$/.test(url.pathname) || url.pathname === '/') return route.continue();
    if (offline) return route.abort('internetdisconnected');
    if (url.pathname === '/health') return route.fulfill({ json: { status: 'ok', backends: {} } });
    if (url.pathname === '/history') return route.fulfill({ json: { items: [], has_more: false } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Up to date');
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) {
      await new Promise(resolve => navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true }));
    }
  });
  await expect.poll(() => page.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
  await expect(page.locator('#connection-status')).toContainText('Up to date');
  // A newer server document must not be mixed with the installed shell assets.
  await page.route('**/', route => route.fulfill({ contentType: 'text/html', body: '<html><body>mismatched shell</body></html>' }));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('#connection-status')).toContainText('Up to date');
  expect(await page.locator('#app').evaluate(el => getComputedStyle(el).display)).toBe('flex');
  await page.unroute('**/');
  offline = true;
  await context.setOffline(true);
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
    page.evaluate(() => { void doRefresh(); }),
  ]);
  expect(await page.evaluate(() => caches.keys())).not.toHaveLength(0);
  expect(await page.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
  await expect(page.locator('#connection-banner')).toBeVisible();
  await expect(page.locator('#connection-status')).toContainText('connect to load history');
  await expect(page.locator('#connection-retry')).toBeVisible();
  await page.locator('#connection-help').click();
  await expect(page.getByRole('dialog', { name: 'Connection help' })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.locator('#input').fill('offline draft');
  expect(await page.evaluate(() => fetch('/health').then(() => false, () => true))).toBe(true);
  offline = false;
  await context.setOffline(false);
  // Browser online event triggers recovery automatically.
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await expect(page.locator('#connection-banner')).toBeHidden();
  await expect(page.locator('#connection-snackbar')).toHaveText('Back online · history updated');
  await expect(page.locator('#input')).toHaveValue('offline draft');
});
