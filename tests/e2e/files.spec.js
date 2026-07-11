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

test('Files menu reports when opened against static UI without Squid API', async ({ page }) => {
  await mockApp(page);
  await page.route('**/health', r => r.fulfill({ status: 404, body: 'Not Found' }));
  await page.route('**/config/localfile-roots**', r => r.fulfill({
    status: 404,
    body: 'Not Found',
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Files' }).click();

  await expect(page.locator('#file-modal')).toBeVisible();
  await expect(page.locator('#file-modal-body')).toContainText('Squid server API not available');
  await expect(page.locator('#file-modal-body')).not.toContainText('/tmp/squid');
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

test('md file shows preview button and opens rendered preview with render=1', async ({ page }) => {
  await mockApp(page);
  await page.route('**/config/localfile-roots**', r => r.fulfill({
    json: { roots: ['/tmp/work'] },
  }));
  await page.route(url => url.pathname === '/localfile', route => {
    const url = new URL(route.request().url());
    const path = url.searchParams.get('path');
    if (path === '/tmp/work') {
      return route.fulfill({
        contentType: 'application/json',
        json: {
          type: 'directory',
          path,
          entries: [
            { name: 'README.md', path: '/tmp/work/README.md', is_dir: false, size: 20, mtime: 1 },
          ],
        },
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'text/markdown',
      body: '# Hello\n\nWorld',
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Files' }).click();
  await page.getByRole('link', { name: '/tmp/work' }).click();
  await page.getByRole('link', { name: 'README.md' }).click();

  // File viewer shows raw md source, not HTML
  await expect(page.locator('#file-modal-body')).toContainText('# Hello');

  // Preview button is visible for md files
  await expect(page.getByRole('button', { name: 'Preview in browser' })).toBeVisible();

  // Preview opens /localfile with render=1
  await page.evaluate(() => {
    window._openedUrl = null;
    window.open = (url) => { window._openedUrl = url; };
  });
  await page.getByRole('button', { name: 'Preview in browser' }).click();
  const openedUrl = await page.evaluate(() => window._openedUrl);
  expect(openedUrl).toContain('render=1');
  expect(openedUrl).toContain('/localfile');
});

test('plain text file has no preview button', async ({ page }) => {
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
            { name: 'notes.txt', path: '/tmp/work/notes.txt', is_dir: false, size: 10, mtime: 1 },
          ],
        },
      });
    }
    return route.fulfill({ status: 200, contentType: 'text/plain', body: 'hello world' });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Files' }).click();
  await page.getByRole('link', { name: '/tmp/work' }).click();
  await page.getByRole('link', { name: 'notes.txt' }).click();

  await expect(page.locator('#file-modal-body')).toContainText('hello world');
  await expect(page.getByRole('button', { name: 'Preview in browser' })).toBeHidden();
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

test('mobile swipes move between views and browser back returns to the previous view', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 390, height: 720 });
  await page.goto('/');

  const swipe = async (fromX, toX) => {
    await page.locator('#app').evaluate((app, { fromX, toX }) => {
      app.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true,
        clientX: fromX,
        clientY: 240,
        pointerId: 1,
        pointerType: 'touch',
      }));
      app.dispatchEvent(new PointerEvent('pointerup', {
        bubbles: true,
        clientX: toX,
        clientY: 245,
        pointerId: 1,
        pointerType: 'touch',
      }));
    }, { fromX, toX });
  };

  await expect(page.locator('#view-chat')).toHaveClass(/active/);
  await expect(page.locator('#mobile-view-title')).toHaveText('Chat');

  await swipe(350, 60);
  await expect(page.locator('#view-files')).toHaveClass(/active/);
  await expect(page.locator('#mobile-view-title')).toHaveText('Files');

  await swipe(350, 60);
  await expect(page.locator('#view-topics')).toHaveClass(/active/);
  await expect(page.locator('#mobile-view-title')).toHaveText('Topics');

  await page.evaluate(() => history.back());
  await expect(page.locator('#view-files')).toHaveClass(/active/);
  await expect(page.locator('#mobile-view-title')).toHaveText('Files');

  await swipe(60, 350);
  await expect(page.locator('#view-chat')).toHaveClass(/active/);
  await expect(page.locator('#mobile-view-title')).toHaveText('Chat');

  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-menu').getByRole('button', { name: 'Agents' }).click();
  await expect(page.locator('#view-agents')).toHaveClass(/active/);
  await expect(page.locator('#mobile-view-title')).toHaveText('Agents');

  await page.evaluate(() => history.back());
  await expect(page.locator('#view-chat')).toHaveClass(/active/);
  await expect(page.locator('#mobile-view-title')).toHaveText('Chat');
});

test('swipes starting in the edge-exclusion strip are ignored so the OS back gesture can claim them', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 390, height: 720 });
  await page.goto('/');

  const swipe = async (fromX, toX) => {
    await page.locator('#app').evaluate((app, { fromX, toX }) => {
      app.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true,
        clientX: fromX,
        clientY: 240,
        pointerId: 1,
        pointerType: 'touch',
      }));
      app.dispatchEvent(new PointerEvent('pointerup', {
        bubbles: true,
        clientX: toX,
        clientY: 245,
        pointerId: 1,
        pointerType: 'touch',
      }));
    }, { fromX, toX });
  };

  await expect(page.locator('#view-chat')).toHaveClass(/active/);

  // Starts inside the 24px edge-exclusion strip (left edge) — should not register as an in-app swipe.
  await swipe(10, 300);
  await expect(page.locator('#view-chat')).toHaveClass(/active/);

  // Starts inside the 24px edge-exclusion strip (right edge) — should not register as an in-app swipe.
  await swipe(385, 100);
  await expect(page.locator('#view-chat')).toHaveClass(/active/);

  // Just outside the strip — swipe still works normally.
  await swipe(350, 60);
  await expect(page.locator('#view-files')).toHaveClass(/active/);
});

test('mouse click-and-drag at a narrow viewport does not trigger view navigation', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 390, height: 720 });
  await page.goto('/');

  await expect(page.locator('#view-chat')).toHaveClass(/active/);

  await page.locator('#app').evaluate(app => {
    app.dispatchEvent(new PointerEvent('pointerdown', {
      bubbles: true, clientX: 350, clientY: 240, pointerId: 1, pointerType: 'mouse',
    }));
    app.dispatchEvent(new PointerEvent('pointerup', {
      bubbles: true, clientX: 60, clientY: 245, pointerId: 1, pointerType: 'mouse',
    }));
  });

  await expect(page.locator('#view-chat')).toHaveClass(/active/);
});
