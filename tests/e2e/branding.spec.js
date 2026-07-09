const { test, expect } = require('@playwright/test');

test('AgentSquid branding is used for install metadata and header logo', async ({ page, request }) => {
  await page.goto('/');

  await expect(page).toHaveTitle('AgentSquid');
  await expect(page.locator('meta[name="apple-mobile-web-app-title"]')).toHaveAttribute('content', 'AgentSquid');

  const manifest = await (await request.get('/manifest.webmanifest')).json();
  expect(manifest.name).toBe('AgentSquid');
  expect(manifest.short_name).toBe('AgentSquid');
  expect(manifest.description).toBe('AgentSquid chat console');

  const logo = page.locator('header#topbar .text-logo');
  await expect(logo).toContainText('AgentSquid');
  await expect(logo).toHaveCSS('font-family', /JetBrains Mono/);
  await expect(logo).toHaveCSS('letter-spacing', /0\.2[0-9]+px/);
});
