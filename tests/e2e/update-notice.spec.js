const { test, expect } = require('@playwright/test');

const PYPI_PROJECT_URL = 'https://pypi.org/pypi/agentsquid/json';

async function mockApp(page, { version = '0.1.0', installOnRestart = 'ask', canInstallOnRestart = true } = {}) {
  await page.route('**/health', r => r.fulfill({
    json: {
      status: 'ok',
      version,
      updates: { install_on_restart: installOnRestart, can_install_on_restart: canInstallOnRestart },
      harnesses: [],
      providers: {},
    },
  }));
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
  return page.route(PYPI_PROJECT_URL, r => r.fulfill({
    json: { info: { version } },
  }));
}

async function openSettings(page) {
  await page.locator('#hamburger-btn').click();
  await page.locator('.hmenu-item[data-view="settings"]').click();
}

test('hamburger, Settings, and Restart get a dot, and Settings explains restart upgrade', async ({ page }) => {
  await mockApp(page, { version: '0.1.0' });
  await mockLatestVersion(page, '0.2.0');

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).toHaveClass(/has-update/);

  await page.locator('#hamburger-btn').click();
  await expect(page.locator('.hmenu-item[data-view="settings"]')).toHaveClass(/has-update/);
  await expect(page.locator('#hmenu-restart')).toHaveClass(/has-update/);
  await page.locator('.hmenu-item[data-view="settings"]').click();

  const notice = page.locator('#settings-update-notice');
  await expect(page.locator('#settings-version-info')).toHaveText('AgentSquid v0.1.0');
  const headerBox = await page.locator('#config-editor-header').boundingBox();
  const versionBox = await page.locator('#settings-version-info').boundingBox();
  const actionsBox = await page.locator('#config-editor-actions').boundingBox();
  const noticeBox = await notice.boundingBox();
  expect(Math.abs(versionBox.x - headerBox.x)).toBeLessThan(2);
  expect(Math.abs((actionsBox.x + actionsBox.width) - (headerBox.x + headerBox.width))).toBeLessThan(2);
  expect(noticeBox.y).toBeGreaterThan(headerBox.y + headerBox.height - 1);
  await expect(notice).toBeVisible();
  await expect(notice).toContainText('AgentSquid v0.1.0 → v0.2.0');
  await expect(notice).toContainText('Restart Server can upgrade before restarting');
  await expect(page.locator('#settings-update-cmd')).toContainText('pipx upgrade agentsquid');
});

test('no dot when already on the latest version', async ({ page }) => {
  await mockApp(page, { version: '0.1.0' });
  await mockLatestVersion(page, '0.1.0');

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).not.toHaveClass(/has-update/);
});

test('no-update cache is reused for 24 hours', async ({ page }) => {
  let pypiHits = 0;
  await mockApp(page, { version: '0.1.1' });
  await page.route(PYPI_PROJECT_URL, r => {
    pypiHits += 1;
    return r.fulfill({ json: { info: { version: '0.1.1' } } });
  });

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).not.toHaveClass(/has-update/);
  expect(pypiHits).toBe(1);

  await page.reload();
  await expect(page.locator('#hamburger-btn')).not.toHaveClass(/has-update/);
  expect(pypiHits).toBe(1);
});

test('manual update check bypasses no-update cache', async ({ page }) => {
  await mockApp(page, { version: '0.1.1' });
  await mockLatestVersion(page, '0.1.1');

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).not.toHaveClass(/has-update/);

  await page.unroute(PYPI_PROJECT_URL);
  await mockLatestVersion(page, '0.1.2');
  await openSettings(page);
  await page.locator('#settings-update-check').click();

  await expect(page.locator('#hamburger-btn')).toHaveClass(/has-update/);
  await expect(page.locator('#settings-update-notice')).toContainText('AgentSquid v0.1.1 → v0.1.2');
});

test('version notice still appears when restart-time install is unavailable', async ({ page }) => {
  await mockApp(page, { version: '0.1.0', canInstallOnRestart: false });
  await mockLatestVersion(page, '0.2.0');

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).toHaveClass(/has-update/);
  await page.locator('#hamburger-btn').click();
  await expect(page.locator('.hmenu-item[data-view="settings"]')).toHaveClass(/has-update/);
  await expect(page.locator('#hmenu-restart')).not.toHaveClass(/has-update/);
  await page.locator('.hmenu-item[data-view="settings"]').click();
  await expect(page.locator('#settings-update-notice')).toBeVisible();
  await expect(page.locator('#settings-update-notice')).toContainText('AgentSquid v0.1.0 → v0.2.0');
  await expect(page.locator('#settings-update-notice')).not.toContainText('Restart Server can upgrade before restarting');
});

test('restart does not prompt for upgrade when restart-time install is unavailable', async ({ page }) => {
  const cmdBodies = [];
  await mockApp(page, { version: '0.1.0', canInstallOnRestart: false });
  await mockLatestVersion(page, '0.2.0');
  await page.route('**/cmd', async route => {
    cmdBodies.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true } });
  });

  await page.goto('/');
  await openSettings(page);
  await expect(page.locator('#settings-update-notice')).toBeVisible();
  await page.locator('#hamburger-btn').click();
  await page.locator('#hmenu-restart').click();

  await expect.poll(() => cmdBodies.length).toBe(1);
  expect(cmdBodies[0]).toMatchObject({ command: 'restart' });
  expect(cmdBodies[0]).not.toHaveProperty('upgrade', true);
});

test('dismissing clears the dot and stays cleared on reload for that version', async ({ page }) => {
  await mockApp(page, { version: '0.1.0' });
  await mockLatestVersion(page, '0.2.0');

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).toHaveClass(/has-update/);
  await openSettings(page);
  await page.locator('#settings-update-dismiss').click();

  await expect(page.locator('#hamburger-btn')).not.toHaveClass(/has-update/);
  await page.locator('#hamburger-btn').click();
  await expect(page.locator('#hmenu-restart')).not.toHaveClass(/has-update/);
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

  await page.unroute(PYPI_PROJECT_URL);
  await mockLatestVersion(page, '0.3.0');
  // simulate the 24h cache TTL elapsing so the next boot re-fetches instead of reusing 0.2.0
  await page.evaluate(() => localStorage.removeItem('squid_update_check_cache'));
  await page.reload();

  await expect(page.locator('#hamburger-btn')).toHaveClass(/has-update/);
  await openSettings(page);
  await expect(page.locator('#settings-update-notice')).toContainText('AgentSquid v0.1.0 → v0.3.0');
});

test('release candidates do not outrank the matching final release', async ({ page }) => {
  await mockApp(page, { version: '0.1.2' });
  await mockLatestVersion(page, '0.1.2rc1');

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).not.toHaveClass(/has-update/);
});

test('the matching final release outranks an installed release candidate', async ({ page }) => {
  await mockApp(page, { version: '0.1.2rc1' });
  await mockLatestVersion(page, '0.1.2');

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).toHaveClass(/has-update/);
});

test('restart asks whether to upgrade when an update is available', async ({ page }) => {
  const cmdBodies = [];
  await mockApp(page, { version: '0.1.0', installOnRestart: 'ask' });
  await mockLatestVersion(page, '0.2.0');
  await page.route('**/cmd', async route => {
    cmdBodies.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true } });
  });

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).toHaveClass(/has-update/);
  await page.locator('#hamburger-btn').click();
  await page.locator('#hmenu-restart').click();
  await expect(page.locator('#restart-modal-title')).toHaveText('AgentSquid v0.2.0 is available');
  await page.locator('#restart-modal-confirm').click();

  expect(cmdBodies[0]).toMatchObject({ command: 'restart', upgrade: true });
});

test('restart can skip an available update from the prompt', async ({ page }) => {
  const cmdBodies = [];
  await mockApp(page, { version: '0.1.0', installOnRestart: 'ask' });
  await mockLatestVersion(page, '0.2.0');
  await page.route('**/cmd', async route => {
    cmdBodies.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true } });
  });

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).toHaveClass(/has-update/);
  await page.locator('#hamburger-btn').click();
  await page.locator('#hmenu-restart').click();
  await page.locator('#restart-modal-secondary').click();

  expect(cmdBodies[0]).toMatchObject({ command: 'restart', upgrade: false });
});

test('always mode upgrades on restart without asking', async ({ page }) => {
  const cmdBodies = [];
  await mockApp(page, { version: '0.1.0', installOnRestart: 'always' });
  await mockLatestVersion(page, '0.2.0');
  await page.route('**/cmd', async route => {
    cmdBodies.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true } });
  });

  await page.goto('/');
  await expect(page.locator('#hamburger-btn')).toHaveClass(/has-update/);
  await page.locator('#hamburger-btn').click();
  await page.locator('#hmenu-restart').click();

  await expect.poll(() => cmdBodies.length).toBe(1);
  await expect(page.locator('#restart-modal')).not.toHaveClass(/open/);
  expect(cmdBodies[0]).toMatchObject({ command: 'restart', upgrade: true });
});
