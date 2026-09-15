const { test, expect } = require('@playwright/test');

// See boot-logo.spec.js / stats-aggregates.spec.js: the app's own SW
// controllerchange handler force-reloads the page mid-test if left enabled.
test.use({ serviceWorkers: 'block' });

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: {} }));
  // Stub the clipboard so "Copy link" doesn't need a real permissions prompt.
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: async () => {} }, configurable: true,
    });
  });
}

const SHORE_OFFER = {
  ceremony_id: '018f4d3e-0000-7000-8000-000000000004',
  code: 'ABCDE1234FGHJK5678MNPQR9012',
  expires_at: Date.now() / 1000 + 300,
  pair_url: 'https://dev.agentsquid.ai/@haebin/pair#stub',
};

async function typeCommand(page, command) {
  const input = page.locator('#input');
  await input.click();
  await input.fill(command);
  await input.press('Enter');
}

test.describe('Connect modal (Shore/AgentSquid.ai + Tailscale)', () => {
  test('/pair with only Shore configured shows its QR directly, no tab bar', async ({ page }) => {
    await mockApp(page);
    await page.route('**/remote', r => r.fulfill({ json: { reason: 'not_installed' } }));
    await page.route('**/shore/devices', r => r.fulfill({ json: { devices: [] } }));
    await page.route('**/shore/pairing/begin', r => r.fulfill({ json: SHORE_OFFER }));
    await page.route('**/shore/pairing/status**', r => r.fulfill({ json: { status: 'pending' } }));
    await page.route('**/shore/pairing/requests', r => r.fulfill({ json: { requests: [] } }));
    await page.goto('/');

    await typeCommand(page, '/pair');
    const modal = page.locator('#connect-modal');
    await expect(modal).toBeVisible();
    await expect(page.locator('#connect-tabs')).toHaveCount(0);
    // qrcode.js renders both a hidden <canvas> and a visible <img> fallback
    // simultaneously -- assert on the one actually shown, not the raw count.
    await expect(page.locator('#connect-agentsquid-panel .connect-qr img')).toBeVisible();
    await expect(page.locator('#shore-pair-code')).toHaveText(SHORE_OFFER.code);
    await expect(page.locator('.connect-link-url')).toHaveText(SHORE_OFFER.pair_url);
  });

  test('both configured: tabs appear, each command opens with the matching tab active, switching works', async ({ page }) => {
    await mockApp(page);
    await page.route('**/remote', r => r.fulfill({ json: { url: 'https://example.ts.net/' } }));
    await page.route('**/shore/devices', r => r.fulfill({ json: { devices: [] } }));
    await page.route('**/shore/pairing/begin', r => r.fulfill({ json: SHORE_OFFER }));
    await page.route('**/shore/pairing/status**', r => r.fulfill({ json: { status: 'pending' } }));
    await page.route('**/shore/pairing/requests', r => r.fulfill({ json: { requests: [] } }));
    await page.goto('/');

    await typeCommand(page, '/pair');
    await expect(page.locator('.connect-tab.active')).toHaveText('AgentSquid.ai');
    await expect(page.locator('#connect-agentsquid-panel')).toBeVisible();
    await expect(page.locator('#connect-tailscale-panel')).toBeHidden();

    await page.locator('.connect-tab', { hasText: 'Tailscale' }).click();
    await expect(page.locator('#connect-tailscale-panel')).toBeVisible();
    await expect(page.locator('#connect-agentsquid-panel')).toBeHidden();
    await expect(page.locator('#connect-tailscale-panel .connect-qr img')).toBeVisible();
    await expect(page.locator('.connect-link-url').first()).toHaveText('https://example.ts.net/');

    const copyBtn = page.locator('#connect-tailscale-panel .connect-copy-btn');
    await copyBtn.click();
    await expect(copyBtn).toHaveText('Copied');

    await page.keyboard.press('Escape');
    await expect(page.locator('#connect-modal')).toHaveCount(0);

    await typeCommand(page, '/remote');
    await expect(page.locator('.connect-tab.active')).toHaveText('Tailscale');
  });

  test('neither configured shows the install/login prompt, no QR', async ({ page }) => {
    await mockApp(page);
    await page.route('**/remote', r => r.fulfill({ json: { reason: 'not_installed' } }));
    await page.route('**/shore/devices', r => r.fulfill({ status: 400, json: { error: 'shore_not_configured' } }));
    await page.goto('/');

    await typeCommand(page, '/pair');
    await expect(page.locator('#connect-neither')).toBeVisible();
    await expect(page.locator('#connect-tabs')).toHaveCount(0);
    await expect(page.locator('.connect-qr')).toHaveCount(0);
    await expect(page.locator('#connect-neither')).toContainText('Tailscale is not installed');
    await expect(page.locator('#connect-neither')).toContainText('agentsquid login');
  });
});
