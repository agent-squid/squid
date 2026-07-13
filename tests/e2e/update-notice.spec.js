const { test, expect } = require('@playwright/test');

const PYPROJECT_URL = 'https://raw.githubusercontent.com/agent-squid/squid/main/pyproject.toml';

async function mockApp(page, { version = '0.1.0' } = {}) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', version, harnesses: [], providers: {} } }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: {} }));
  await page.route('**/config/yaml', r => r.fulfill({
    json: { content: 'agents: []\n', revision: 'rev-1', path: '/tmp/squid/config.yaml' },
  }));
}

function mockLatestVersion(page, version) {
  return page.route(PYPROJECT_URL, r => r.fulfill({
    contentType: 'text/plain',
    body: `[project]\nname = "squid"\nversion = "${version}"\n`,
  }));
}

async function openSettings(page) {
  await page.locator('#hamburger-btn').click();
  await page.locator('.hmenu-item[data-view="settings"]').click();
}

test('hamburger and Settings row get a dot, and Settings shows the update command', async ({ page }) => {
  await mockApp(page, { version: '0.1.0' });
  await mockLatestVersion(page, '0.2.0');

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).toHaveClass(/has-update/);

  await page.locator('#hamburger-btn').click();
  await expect(page.locator('.hmenu-item[data-view="settings"]')).toHaveClass(/has-update/);
  await page.locator('.hmenu-item[data-view="settings"]').click();

  const notice = page.locator('#settings-update-notice');
  await expect(notice).toBeVisible();
  await expect(notice).toContainText('v0.1.0 → v0.2.0');
  await expect(page.locator('#settings-update-cmd')).toContainText('v0.2.0.tar.gz');
});

test('no dot when already on the latest version', async ({ page }) => {
  await mockApp(page, { version: '0.1.0' });
  await mockLatestVersion(page, '0.1.0');

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).not.toHaveClass(/has-update/);
});

test('dismissing clears the dot and stays cleared on reload for that version', async ({ page }) => {
  await mockApp(page, { version: '0.1.0' });
  await mockLatestVersion(page, '0.2.0');

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).toHaveClass(/has-update/);
  await openSettings(page);
  await page.locator('#settings-update-dismiss').click();

  await expect(page.locator('#hamburger-btn')).not.toHaveClass(/has-update/);
  await expect(page.locator('#settings-update-notice')).toBeHidden();

  await page.reload();
  await expect(page.locator('#hamburger-btn')).not.toHaveClass(/has-update/);
});

test('a further version bump re-shows the dot even after a prior dismissal', async ({ page }) => {
  await mockApp(page, { version: '0.1.0' });
  await mockLatestVersion(page, '0.2.0');

  await page.goto('/');
  await openSettings(page);
  await page.locator('#settings-update-dismiss').click();
  await expect(page.locator('#hamburger-btn')).not.toHaveClass(/has-update/);

  await page.unroute(PYPROJECT_URL);
  await mockLatestVersion(page, '0.3.0');
  // simulate the 24h cache TTL elapsing so the next boot re-fetches instead of reusing 0.2.0
  await page.evaluate(() => localStorage.removeItem('squid_update_check_cache'));
  await page.reload();

  await expect(page.locator('#hamburger-btn')).toHaveClass(/has-update/);
  await openSettings(page);
  await expect(page.locator('#settings-update-notice')).toContainText('v0.1.0 → v0.3.0');
});
