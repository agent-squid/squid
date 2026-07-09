const { test, expect } = require('@playwright/test');

test('community view keeps the shared mobile top spacing below the nav', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('https://agentsquid.ai/community.html?embed=1', route =>
    route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Community</title>' })
  );
  await page.goto('/');

  await page.getByRole('button', { name: 'Menu' }).click();
  await page.getByRole('button', { name: 'Community' }).click();

  const community = page.locator('#view-community');
  await expect(community).toHaveClass(/active/);
  await expect(community).toHaveCSS('padding-top', '12px');
});
