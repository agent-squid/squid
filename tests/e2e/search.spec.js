const { test, expect } = require('@playwright/test');

async function mockBackend(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/search**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
}

test('explicit #all search shows its scope and restores the full command', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.fill('#input', '/s #all claude login');
  await page.keyboard.press('Enter');

  await expect(page.locator('#filter-badge')).toHaveClass(/active/);
  await expect(page.locator('#filter-badge-label')).toHaveText('#all');
  await expect(page.locator('#search-bar-keywords')).toHaveText('claude login');

  await page.locator('#search-bar-keywords').click();
  await expect(page.locator('#input')).toHaveValue('/s #all claude login');
});

test('unscoped global search does not display or restore #all', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.fill('#input', '/s claude login');
  await page.keyboard.press('Enter');

  await expect(page.locator('#filter-badge')).not.toHaveClass(/active/);
  await expect(page.locator('#search-bar-keywords')).toHaveText('claude login');

  await page.locator('#search-bar-keywords').click();
  await expect(page.locator('#input')).toHaveValue('/s claude login');
});

test('clearing the #all scope removes its explicit marker', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.fill('#input', '/s #all claude login');
  await page.keyboard.press('Enter');
  await expect(page.locator('#filter-badge-label')).toHaveText('#all');

  await page.locator('#filter-badge-clear').click();
  await expect(page.locator('#filter-badge')).not.toHaveClass(/active/);

  await page.locator('#search-bar-keywords').click();
  await expect(page.locator('#input')).toHaveValue('/s claude login');
});
