const { test, expect } = require('@playwright/test');

async function mockBackend(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    '#other@codex push the changes',
    '#squid@haiku! push the changes',
    '#squid@codex push the changes',
    '#squid@codex inspect the changes',
  ] } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
}

test('typed prompt prefixes show unique routed history with the current route first', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'push');

  const items = page.locator('#autocomplete .ac-item');
  await expect(items).toHaveCount(3);
  await expect(items.nth(0)).toContainText('#squid@codex push the changes');
  await expect(items.nth(1)).toContainText('#other@codex push the changes');
  await expect(items.nth(2)).toContainText('#squid@haiku! push the changes');
  await expect(page.getByRole('button', { name: 'Close suggestions' })).toBeVisible();

  await items.nth(2).click();
  await expect(page.locator('#input')).toHaveValue('#squid@haiku! push the changes');
});

test('autocomplete can be dismissed with its touch-accessible close button', async ({ page }) => {
  await mockBackend(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');

  await page.fill('#input', 'push');
  await page.getByRole('button', { name: 'Close suggestions' }).click();

  await expect(page.locator('#autocomplete')).not.toHaveClass(/open/);
  await expect(page.locator('#input')).toBeFocused();
  await expect(page.locator('#input')).toHaveValue('push');
});

test('plain Enter sends typed text when no autocomplete result is selected', async ({ page }) => {
  await mockBackend(page);
  let sent;
  await page.route('**/chat', route => {
    sent = route.request().postDataJSON();
    return route.fulfill({
      contentType: 'text/event-stream',
      body: 'event: done\ndata: {}\n\n',
    });
  });
  await page.goto('/');

  await page.fill('#input', 'push');
  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await page.keyboard.press('Enter');

  await expect.poll(() => sent?.message).toBe('push');
});
