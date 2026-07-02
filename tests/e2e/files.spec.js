const { test, expect } = require('@playwright/test');

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: {} }));
}

test('Files menu opens configured roots and browses into files', async ({ page }) => {
  await mockApp(page);
  await page.route('**/config/localfile-roots**', r => r.fulfill({
    json: { roots: ['/tmp/work/project', '/tmp/notes'] },
  }));
  await page.route(url => url.pathname === '/localfile', route => {
    const url = new URL(route.request().url());
    const path = url.searchParams.get('path');
    if (path === '/tmp/work/project') {
      return route.fulfill({
        contentType: 'application/json',
        json: {
          type: 'directory',
          path,
          entries: [
            { name: 'src', path: '/tmp/work/project/src', is_dir: true, size: null, mtime: 1 },
            { name: 'README.md', path: '/tmp/work/project/README.md', is_dir: false, size: 12, mtime: 1 },
          ],
        },
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'text/markdown',
      body: '# Project\n\nEditable file',
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Files' }).click();

  await expect(page.locator('#file-modal')).toBeVisible();
  await expect(page.locator('#file-modal-breadcrumb')).toHaveText('Files');
  await expect(page.getByRole('button', { name: 'Back' })).toBeDisabled();
  await expect(page.locator('#file-modal-body')).toContainText('/tmp/work/project');

  await page.getByRole('link', { name: '/tmp/work/project' }).click();
  await expect(page.locator('#file-modal-breadcrumb')).toContainText('tmp/work/project');
  await expect(page.getByRole('link', { name: 'README.md' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Back' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Edit file' })).toBeHidden();
  await expect(page.getByRole('button', { name: 'Edit history' })).toBeHidden();

  await page.getByRole('button', { name: 'Back' }).click();
  await expect(page.locator('#file-modal-breadcrumb')).toHaveText('Files');
  await expect(page.locator('#file-modal-body')).toContainText('/tmp/notes');

  await page.getByRole('button', { name: 'Forward' }).click();
  await expect(page.locator('#file-modal-breadcrumb')).toContainText('tmp/work/project');
  await expect(page.getByRole('link', { name: 'README.md' })).toBeVisible();

  await page.getByRole('link', { name: 'README.md' }).click();
  await expect(page.locator('#file-modal-breadcrumb')).toContainText('README.md');
  await expect(page.locator('#file-modal-body')).toContainText('Editable file');
  await expect(page.getByRole('button', { name: 'Edit file' })).toBeVisible();
});

test('Files menu falls back to squid_home when roots endpoint is missing', async ({ page }) => {
  await mockApp(page);
  await page.route('**/health', r => r.fulfill({
    json: { status: 'ok', squid_home: '/tmp/fresh/squid', backends: {} },
  }));
  await page.route('**/config/localfile-roots**', r => r.fulfill({
    status: 404,
    body: 'Not Found',
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Files' }).click();

  await expect(page.locator('#file-modal')).toBeVisible();
  await expect(page.locator('#file-modal-breadcrumb')).toHaveText('Files');
  await expect(page.getByRole('link', { name: '/tmp/fresh/squid' })).toBeVisible();
});

test('yaml example files open inline from the browser instead of downloading', async ({ page }) => {
  await mockApp(page);
  await page.route('**/config/localfile-roots**', r => r.fulfill({
    json: { roots: ['/tmp/work'] },
  }));
  await page.route(url => url.pathname === '/localfile', route => {
    const path = new URL(route.request().url()).searchParams.get('path');
    if (path === '/tmp/work') {
      return route.fulfill({
        contentType: 'application/json',
        json: {
          type: 'directory',
          path,
          entries: [
            { name: 'squid', path: '/tmp/work/squid', is_dir: true, size: null, mtime: 1 },
          ],
        },
      });
    }
    if (path === '/tmp/work/squid') {
      return route.fulfill({
        contentType: 'application/json',
        json: {
          type: 'directory',
          path,
          entries: [
            { name: 'config', path: '/tmp/work/squid/config', is_dir: true, size: null, mtime: 1 },
          ],
        },
      });
    }
    if (path === '/tmp/work/squid/config') {
      return route.fulfill({
        contentType: 'application/json',
        json: {
          type: 'directory',
          path,
          entries: [
            { name: 'squid.yaml.example', path: '/tmp/work/squid/config/squid.yaml.example', is_dir: false, size: 21, mtime: 1 },
          ],
        },
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'text/plain',
      body: 'server:\n  port: 8000\n',
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Files' }).click();
  await page.getByRole('link', { name: '/tmp/work' }).click();
  await page.getByRole('link', { name: 'squid/' }).click();
  await page.getByRole('link', { name: 'config/' }).click();

  const downloadPromise = page.waitForEvent('download', { timeout: 1000 }).then(() => true).catch(() => false);
  await page.getByRole('link', { name: 'squid.yaml.example' }).click();

  await expect(page.locator('#file-modal-breadcrumb')).toContainText('squid.yaml.example');
  await expect(page.locator('#file-modal-body')).toContainText('server:');
  expect(await downloadPromise).toBe(false);
});

test('desktop burger only shows settings while mobile burger shows primary nav', async ({ page }) => {
  await mockApp(page);

  await page.setViewportSize({ width: 1024, height: 720 });
  await page.goto('/');
  await page.locator('#hamburger-btn').click();
  await expect(page.locator('#hamburger-menu')).toBeVisible();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Settings' })).toBeVisible();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Chat' })).toBeHidden();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Topics' })).toBeHidden();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Files' })).toBeHidden();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Agents' })).toBeHidden();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Analytics' })).toBeHidden();

  await page.setViewportSize({ width: 390, height: 720 });
  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-btn').click();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Chat' })).toBeVisible();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Topics' })).toBeVisible();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Files' })).toBeVisible();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Agents' })).toBeVisible();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Analytics' })).toBeVisible();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Settings' })).toBeVisible();
});
