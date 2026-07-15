const { test, expect } = require('@playwright/test');

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/stats/filters', r => r.fulfill({ json: { agents: [], topics: [] } }));
  await page.route('**/stats/filter-presets', r => r.fulfill({ json: [] }));
  await page.route('**/stats?**', r => r.fulfill({ json: [] }));
}

test.describe('mobile view navigation history', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('tapping Chat collapses internal mobile view history', async ({ page }) => {
    await mockApp(page);
    await page.goto('/');

    await page.evaluate(() => navigateView('stats'));
    await expect(page.locator('#view-stats')).toHaveClass(/active/);

    await page.evaluate(() => navigateView('agents'));
    await expect(page.locator('#view-agents')).toHaveClass(/active/);

    await page.evaluate(() => navigateView('chat'));
    await expect(page.locator('#view-chat')).toHaveClass(/active/);
    await expect.poll(() => page.evaluate(() => history.state?.squidView)).toBe('chat');
    await expect.poll(() => page.evaluate(() => _mobileViewHistoryDepth)).toBe(0);

    await page.evaluate(() => history.back());
    await expect.poll(() => page.url()).toBe('about:blank');
  });
});
