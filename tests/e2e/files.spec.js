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

  await expect(page.locator('#file-modal-box')).toBeVisible();
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

  await page.getByRole('button', { name: 'File roots' }).click();
  await expect(page.locator('#file-modal-breadcrumb')).toHaveText('Files');
  await expect(page.getByRole('button', { name: 'File roots' })).toBeDisabled();
  await expect(page.getByRole('link', { name: '/tmp/work/project' })).toBeVisible();
  await expect(page.getByRole('link', { name: '/tmp/notes' })).toBeVisible();
});

test('Files menu creates folders, creates files, and renames entries', async ({ page }) => {
  await mockApp(page);
  const calls = [];
  const entries = [
    { name: 'src', path: '/tmp/work/project/src', is_dir: true, size: null, mtime: 1 },
    { name: 'README.md', path: '/tmp/work/project/README.md', is_dir: false, size: 12, mtime: 1 },
  ];
  await page.route('**/config/localfile-roots**', r => r.fulfill({
    json: { roots: ['/tmp/work/project'] },
  }));
  await page.route(url => url.pathname.startsWith('/localfile'), async route => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.pathname === '/localfile/create-folder') {
      const body = req.postDataJSON();
      calls.push({ endpoint: 'create-folder', body });
      const path = `${body.parent}/${body.name}`;
      entries.push({ name: body.name, path, is_dir: true, size: null, mtime: 2 });
      return route.fulfill({ json: { ok: true, path } });
    }
    if (url.pathname === '/localfile/upload') {
      const parent = url.searchParams.get('parent');
      const name = url.searchParams.get('name');
      calls.push({ endpoint: 'upload', parent, name, body: req.postData() });
      const path = `${parent}/${name}`;
      entries.push({ name, path, is_dir: false, size: req.postData()?.length || 0, mtime: 2 });
      return route.fulfill({ json: { ok: true, path } });
    }
    if (url.pathname === '/localfile/create-file') {
      const body = req.postDataJSON();
      calls.push({ endpoint: 'create-file', body });
      const path = `${body.parent}/${body.name}`;
      entries.push({ name: body.name, path, is_dir: false, size: 0, mtime: 3 });
      return route.fulfill({ json: { ok: true, path } });
    }
    if (url.pathname === '/localfile/rename') {
      const body = req.postDataJSON();
      calls.push({ endpoint: 'rename', body });
      const entry = entries.find(e => e.path === body.path);
      const path = body.to_path || `${body.path.split('/').slice(0, -1).join('/')}/${body.name}`;
      const name = path.split('/').pop();
      if (entry) {
        entry.name = name;
        entry.path = path;
      }
      return route.fulfill({ json: { ok: true, path } });
    }
    const path = url.searchParams.get('path');
    if (path === '/tmp/work/project') {
      return route.fulfill({
        contentType: 'application/json',
        json: { type: 'directory', path, entries: entries.filter(e => e.path.split('/').slice(0, -1).join('/') === path) },
      });
    }
    if (path === '/tmp/work/project/src' || path === '/tmp/work/project/lib') {
      return route.fulfill({
        contentType: 'application/json',
        json: { type: 'directory', path, entries: entries.filter(e => e.path.split('/').slice(0, -1).join('/') === path) },
      });
    }
    return route.fulfill({ status: 200, contentType: 'text/plain', body: '' });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Files' }).click();
  await page.getByRole('link', { name: '/tmp/work/project' }).click();

  await page.locator('#file-modal-body').dispatchEvent('drop', {
    dataTransfer: await page.evaluateHandle(() => {
      const dt = new DataTransfer();
      dt.items.add(new File(['uploaded body'], 'upload.txt', { type: 'text/plain' }));
      return dt;
    }),
  });
  await expect(page.getByRole('link', { name: 'upload.txt' })).toBeVisible();

  const chooserPromise = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: 'Upload file' }).click();
  const chooser = await chooserPromise;
  await chooser.setFiles({
    name: 'picked.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('picked body'),
  });
  await expect(page.getByRole('link', { name: 'picked.txt' })).toBeVisible();

  await page.getByRole('button', { name: 'Add folder' }).click();
  await expect(page.locator('.fv-path-modal')).toBeVisible();
  await expect(page.locator('.fv-path-modal')).toContainText('New folder');
  await page.getByLabel('Destination path').fill('lib');
  await page.locator('.fv-path-modal').getByRole('button', { name: 'Create' }).click();
  await expect(page.locator('#file-modal-breadcrumb')).toContainText('lib');

  await page.getByRole('button', { name: 'Back' }).click();
  await expect(page.getByRole('link', { name: 'lib/' })).toBeVisible();

  await page.getByRole('button', { name: 'Create file' }).click();
  await expect(page.locator('.fv-path-modal')).toBeVisible();
  await expect(page.locator('.fv-path-modal')).toContainText('New file');
  await page.getByLabel('Destination path').fill('notes.txt');
  await page.locator('.fv-path-modal').getByRole('button', { name: 'Create' }).click();
  await expect(page.locator('#file-modal-breadcrumb')).toContainText('notes.txt');

  await page.getByRole('button', { name: 'Back' }).click();
  await page.getByRole('button', { name: 'Rename README.md' }).click();
  await expect(page.locator('.fv-path-modal')).toBeVisible();
  await expect(page.locator('.fv-path-modal')).toContainText('Rename or move');
  await expect(page.getByLabel('Destination path')).toHaveValue('/tmp/work/project/README.md');
  await page.getByLabel('Destination path').fill('/tmp/work/project/GUIDE.md');
  await page.locator('.fv-path-modal').getByRole('button', { name: 'Rename' }).click();
  await expect(page.getByRole('link', { name: 'GUIDE.md' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'README.md' })).toHaveCount(0);

  await page.getByRole('button', { name: 'Rename GUIDE.md' }).click();
  await page.getByLabel('Destination path').fill('/tmp/work/project/src/GUIDE.md');
  await page.locator('.fv-path-modal').getByRole('button', { name: 'Rename' }).click();
  await expect(page.getByRole('link', { name: 'GUIDE.md' })).toHaveCount(0);
  await page.getByRole('link', { name: 'src/' }).click();
  await expect(page.getByRole('link', { name: 'GUIDE.md' })).toBeVisible();

  expect(calls).toEqual([
    { endpoint: 'upload', parent: '/tmp/work/project', name: 'upload.txt', body: 'uploaded body' },
    { endpoint: 'upload', parent: '/tmp/work/project', name: 'picked.txt', body: 'picked body' },
    { endpoint: 'create-folder', body: { parent: '/tmp/work/project', name: 'lib' } },
    { endpoint: 'create-file', body: { parent: '/tmp/work/project', name: 'notes.txt' } },
    { endpoint: 'rename', body: { path: '/tmp/work/project/README.md', to_path: '/tmp/work/project/GUIDE.md' } },
    { endpoint: 'rename', body: { path: '/tmp/work/project/GUIDE.md', to_path: '/tmp/work/project/src/GUIDE.md' } },
  ]);
});

test('Files menu deletes files from rows and the open file view', async ({ page }) => {
  await mockApp(page);
  const calls = [];
  const entries = [
    { name: 'README.md', path: '/tmp/work/project/README.md', is_dir: false, size: 12, mtime: 1 },
    { name: 'notes.txt', path: '/tmp/work/project/notes.txt', is_dir: false, size: 5, mtime: 1 },
    { name: 'src', path: '/tmp/work/project/src', is_dir: true, size: null, mtime: 1 },
  ];
  await page.route('**/config/localfile-roots**', r => r.fulfill({
    json: { roots: ['/tmp/work/project'] },
  }));
  await page.route(url => url.pathname.startsWith('/localfile'), async route => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.pathname === '/localfile/delete') {
      const body = req.postDataJSON();
      calls.push({ endpoint: 'delete', body });
      const idx = entries.findIndex(e => e.path === body.path);
      if (idx >= 0) entries.splice(idx, 1);
      return route.fulfill({ json: { ok: true, path: body.path } });
    }
    const path = url.searchParams.get('path');
    if (path === '/tmp/work/project') {
      return route.fulfill({
        contentType: 'application/json',
        json: { type: 'directory', path, entries },
      });
    }
    if (path === '/tmp/work/project/src') {
      return route.fulfill({
        contentType: 'application/json',
        json: { type: 'directory', path, entries: [] },
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'text/plain',
      body: path?.endsWith('README.md') ? 'readme body' : 'notes',
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Files' }).click();
  await page.getByRole('link', { name: '/tmp/work/project' }).click();

  await expect(page.getByRole('button', { name: 'Delete src' })).toHaveCount(0);
  page.once('dialog', async dialog => {
    expect(dialog.message()).toBe('Delete notes.txt?');
    await dialog.accept();
  });
  await page.getByRole('button', { name: 'Delete notes.txt' }).click();
  await expect(page.getByRole('link', { name: 'notes.txt' })).toHaveCount(0);

  await page.getByRole('link', { name: 'README.md' }).click();
  await expect(page.locator('#file-modal-body')).toContainText('readme body');
  await expect(page.getByRole('button', { name: 'Delete file' })).toBeVisible();

  page.once('dialog', async dialog => {
    expect(dialog.message()).toBe('Delete README.md?');
    await dialog.accept();
  });
  await page.getByRole('button', { name: 'Delete file' }).click();
  await expect(page.locator('#file-modal-breadcrumb')).toContainText('tmp/work/project');
  await expect(page.getByRole('link', { name: 'README.md' })).toHaveCount(0);

  expect(calls).toEqual([
    { endpoint: 'delete', body: { path: '/tmp/work/project/notes.txt' } },
    { endpoint: 'delete', body: { path: '/tmp/work/project/README.md' } },
  ]);
});

test('mobile file browser header icons match compact editor action sizing', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 390, height: 720 });
  await page.route('**/config/localfile-roots**', r => r.fulfill({
    json: { roots: ['/tmp/work/project'] },
  }));
  await page.route(url => url.pathname === '/localfile', route => {
    const path = new URL(route.request().url()).searchParams.get('path');
    if (path === '/tmp/work/project') {
      return route.fulfill({
        contentType: 'application/json',
        json: {
          type: 'directory',
          path,
          entries: [
            { name: 'README.md', path: '/tmp/work/project/README.md', is_dir: false, size: 12, mtime: 1 },
          ],
        },
      });
    }
    return route.fulfill({ status: 200, contentType: 'text/plain', body: 'hello' });
  });

  await page.goto('/');
  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-menu').getByRole('button', { name: 'Files' }).click();
  await page.getByRole('link', { name: '/tmp/work/project' }).click();

  const sizes = await page.evaluate(() => {
    const sizeFor = label => {
      const btn = document.querySelector(`#file-modal-header [aria-label="${label}"]`);
      const icon = btn?.querySelector('.material-symbols-outlined');
      const btnStyle = getComputedStyle(btn);
      const iconStyle = getComputedStyle(icon);
      return { width: btnStyle.width, height: btnStyle.height, fontSize: iconStyle.fontSize };
    };
    return {
      create: sizeFor('Create file'),
      upload: sizeFor('Upload file'),
      save: sizeFor('Save'),
    };
  });
  expect(sizes.create).toEqual({ width: '30px', height: '30px', fontSize: '20px' });
  expect(sizes.upload).toEqual(sizes.create);
  expect(sizes.save).toEqual(sizes.create);
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

  await expect(page.locator('#file-modal-box')).toBeVisible();
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

  await expect(page.locator('#file-modal-box')).toBeVisible();
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

test('md file shows preview button and renders preview inline', async ({ page }) => {
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
  await expect(page.getByRole('button', { name: 'Preview Markdown' })).toBeVisible();

  // Preview renders inline instead of opening /localfile?render=1 in a new tab.
  await page.evaluate(() => {
    window._openedUrl = 'not-opened';
    window.open = (url) => { window._openedUrl = url; };
  });
  await page.getByRole('button', { name: 'Preview Markdown' }).click();
  const openedUrl = await page.evaluate(() => window._openedUrl);
  expect(openedUrl).toBe('not-opened');
  await expect(page.locator('#file-modal-body .fv-md-preview h1')).toHaveText('Hello');
  await expect(page.locator('#file-modal-body')).toContainText('World');
  const previewFontSize = await page.locator('#file-modal-body .fv-md-preview').evaluate(el => parseFloat(getComputedStyle(el).fontSize));
  expect(previewFontSize).toBeGreaterThanOrEqual(15.5);
  await expect(page.getByRole('button', { name: 'Show Markdown source' })).toBeVisible();

  await page.getByRole('button', { name: 'Show Markdown source' }).click();
  await expect(page.locator('#file-modal-body')).toContainText('# Hello');
});

test('html preview stays in file viewer and back returns to source', async ({ page }) => {
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
            { name: 'community.html', path: '/tmp/work/community.html', is_dir: false, size: 44, mtime: 1 },
          ],
        },
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'text/html',
      body: '<!doctype html><title>Community</title><h1>Community</h1>',
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Files' }).click();
  await page.getByRole('link', { name: '/tmp/work' }).click();
  await page.getByRole('link', { name: 'community.html' }).click();

  await expect(page.locator('#file-modal-body')).toContainText('<h1>Community</h1>');
  await page.setViewportSize({ width: 390, height: 800 });

  await page.evaluate(() => {
    window._openedUrl = 'not-opened';
    window.open = url => { window._openedUrl = url; };
  });
  await page.getByRole('button', { name: 'Preview' }).click();
  expect(await page.evaluate(() => window._openedUrl)).toBe('not-opened');
  await expect(page.locator('#file-modal-body iframe.fv-web-preview')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Show source' })).toBeVisible();

  await page.getByRole('button', { name: 'Show source' }).click();
  await expect(page.locator('#file-modal-body iframe.fv-web-preview')).toHaveCount(0);
  await expect(page.locator('#file-modal-body')).toContainText('<h1>Community</h1>');

  await page.getByRole('button', { name: 'Preview' }).click();
  await expect(page.locator('#file-modal-body iframe.fv-web-preview')).toBeVisible();
  await page.getByRole('button', { name: 'Back' }).click();
  await expect(page.locator('#file-modal-body iframe.fv-web-preview')).toHaveCount(0);
  await expect(page.locator('#file-modal-body')).toContainText('<h1>Community</h1>');

  await page.getByRole('button', { name: 'Preview' }).click();
  await expect(page.locator('#file-modal-body iframe.fv-web-preview')).toBeVisible();
  await page.evaluate(() => history.back());
  await expect(page.locator('#file-modal-body iframe.fv-web-preview')).toHaveCount(0);
  await expect(page.locator('#file-modal-body')).toContainText('<h1>Community</h1>');
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

test('file editor keeps controls at top, highlights generic suffix files, discards edits, and shows history diffs', async ({ page }) => {
  await mockApp(page);
  await page.route('https://esm.sh/**', route => route.abort());
  let savedContent = 'server:\n  port: 8000\n';
  await page.route('**/config/localfile-roots**', r => r.fulfill({
    json: { roots: ['/tmp/work'] },
  }));
  await page.route(url => url.pathname === '/localfile/history', route => route.fulfill({
    json: {
      history: [{
        id: 7,
        file_path: '/tmp/work/config.yaml.example',
        edited_at: '2026-07-12T10:00:00Z',
        before: 'server:\n  port: 8000\n',
        after: 'server:\n  port: 9000\n',
      }],
    },
  }));
  await page.route(url => url.pathname === '/localfile/revert-edit', route => route.fulfill({
    json: { ok: true },
  }));
  await page.route(url => url.pathname === '/localfile', route => {
    const req = route.request();
    const path = new URL(req.url()).searchParams.get('path');
    if (req.method() === 'POST') {
      savedContent = req.postDataJSON().content;
      return route.fulfill({ json: { ok: true, edit_id: 8 } });
    }
    if (path === '/tmp/work') {
      return route.fulfill({
        contentType: 'application/json',
        json: {
          type: 'directory',
          path,
          entries: [
            { name: 'config.yaml.example', path: '/tmp/work/config.yaml.example', is_dir: false, size: 21, mtime: 1 },
          ],
        },
      });
    }
    return route.fulfill({ status: 200, contentType: 'text/plain', body: savedContent });
  });

  await page.goto('/');
  await page.evaluate(() => {
    let yamlRequested = false;
    const makeDoc = text => {
      const lines = text.split('\n');
      return {
        length: text.length,
        lines: lines.length,
        toString: () => text,
        line: number => {
          const n = Math.min(Math.max(number, 1), lines.length);
          const from = lines.slice(0, n - 1).join('\n').length + (n > 1 ? 1 : 0);
          return { from, to: from + lines[n - 1].length, number: n };
        },
        lineAt: pos => {
          let offset = 0;
          for (let i = 0; i < lines.length; i++) {
            const end = offset + lines[i].length;
            if (pos <= end) return { from: offset, to: end, number: i + 1 };
            offset = end + 1;
          }
          return { from: Math.max(0, text.length - (lines.at(-1)?.length || 0)), to: text.length, number: lines.length };
        },
      };
    };
    class FakeEditorView {
      static updateListener = { of: listener => ({ listener }) };
      constructor({ state, parent }) {
        this.state = state;
        this.listeners = state.extensions.filter(ext => ext?.listener).map(ext => ext.listener);
        const root = document.createElement('div');
        root.className = 'cm-editor';
        const input = document.createElement('textarea');
        input.value = state.doc.toString();
        input.addEventListener('input', () => {
          this.state.doc = makeDoc(input.value);
          this.listeners.forEach(listener => listener({ docChanged: true }));
        });
        root.appendChild(input);
        parent.appendChild(root);
        this.input = input;
      }
      focus() {}
      destroy() { this.input.closest('.cm-editor')?.remove(); }
      dispatch(update) {
        if (update.changes) {
          this.input.value = update.changes.insert;
          this.input.dispatchEvent(new Event('input', { bubbles: true }));
        }
        if (update.selection) this.state.selection = { main: { to: update.selection.head ?? update.selection.anchor } };
      }
    }
    window._cm = {
      EditorView: FakeEditorView,
      EditorState: {
        create: ({ doc, extensions }) => ({
          doc: makeDoc(doc),
          extensions,
        }),
      },
      basicSetup: {},
      oneDark: {},
      atomOneDarkHighlight: {},
      LANGS: {
        yaml: () => { yamlRequested = true; return { name: 'yaml-lang' }; },
      },
    };
    window._cmPromise = Promise.resolve(true);
    window.__yamlRequested = () => yamlRequested;
  });

  await page.getByRole('button', { name: 'Files' }).click();
  await page.getByRole('link', { name: '/tmp/work' }).click();
  await page.getByRole('link', { name: 'config.yaml.example' }).click();
  expect(await page.evaluate(() =>
    Array.from(document.querySelector('.fv-header-actions').children)
      .map(el => el.getAttribute('aria-label') || el.title || el.id)
      .slice(-2)
  )).toEqual(['Copy path', 'Close']);
  await page.getByRole('button', { name: 'Edit file' }).click();

  await expect(page.locator('.fv-edit-toolbar')).toBeVisible();
  expect(await page.evaluate(() => {
    const box = document.querySelector('#file-modal-box');
    return Array.from(box.children).map(el => el.id || el.className).slice(0, 3);
  })).toEqual(['file-modal-header', 'fv-edit-toolbar', 'file-modal-body']);
  expect(await page.evaluate(() => {
    const actions = document.querySelector('.fv-header-actions');
    return ['Save', 'Discard changes'].every(label =>
      actions?.contains(document.querySelector(`[aria-label="${label}"]`))
    );
  })).toBe(true);
  expect(await page.evaluate(() =>
    Array.from(document.querySelector('.fv-header-actions').children)
      .map(el => el.getAttribute('aria-label') || el.title || el.id)
      .slice(-4)
  )).toEqual(['Discard changes', 'Save', 'Copy path', 'Close']);
  await expect(page.getByLabel('Find in editor')).toBeVisible();
  await expect(page.getByLabel('Line number')).toBeVisible();
  await page.setViewportSize({ width: 390, height: 720 });
  expect(await page.evaluate(() => {
    const actions = document.querySelector('.fv-header-actions');
    const labels = ['Discard changes', 'Save', 'Copy path', 'Close'];
    return labels.map(label => {
      const rect = actions.querySelector(`[aria-label="${label}"]`).getBoundingClientRect();
      return { label, left: rect.left, visible: rect.width > 0 && rect.height > 0 };
    }).filter(item => item.visible).sort((a, b) => a.left - b.left).map(item => item.label);
  })).toEqual(['Discard changes', 'Save', 'Copy path']);
  expect(await page.evaluate(() => window.__yamlRequested())).toBe(true);

  await expect(page.getByRole('button', { name: 'Save' })).toBeDisabled();
  await page.locator('.cm-editor textarea').fill('server:\n  port: 9000\n');
  await expect(page.getByRole('button', { name: 'Save' })).toBeEnabled();
  await page.getByLabel('Find in editor').fill('port');
  await page.getByRole('button', { name: 'Next match' }).click();
  await expect(page.locator('.fv-edit-toolbar > .fv-edit-tools')).toBeVisible();
  await expect(page.locator('.fv-edit-find-popover')).toBeHidden();
  await page.getByLabel('Line number').fill('2');
  await page.getByRole('button', { name: 'Go to line' }).click();
  await expect(page.locator('.fv-edit-toolbar > .fv-edit-tools')).toBeVisible();
  await expect(page.locator('.fv-edit-find-popover')).toBeHidden();
  await page.getByRole('button', { name: 'Discard changes' }).click();
  await expect(page.locator('#file-modal-body')).toContainText('port: 8000');

  await page.getByRole('button', { name: 'Edit history' }).click();
  await page.getByRole('button', { name: 'Show diff' }).click();
  await expect(page.locator('.fv-history-diff')).toContainText('-   port: 8000');
  await expect(page.locator('.fv-history-diff')).toContainText('+   port: 9000');
  await expect(page.getByRole('button', { name: 'Revert to this' })).toBeVisible();
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
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Stats' })).toBeHidden();

  await page.setViewportSize({ width: 390, height: 720 });
  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-btn').click();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Chat' })).toBeVisible();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Topics' })).toBeVisible();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Files' })).toBeVisible();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Agents' })).toBeVisible();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Stats' })).toBeVisible();
  await expect(page.locator('#hamburger-menu').getByRole('button', { name: 'Settings' })).toBeVisible();
});

test('mobile burger closes an open help panel', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 390, height: 720 });
  await page.goto('/');

  await page.locator('#help-btn').click();
  await expect(page.locator('#help-panel')).toHaveClass(/open/);

  await page.locator('#hamburger-btn').click();
  await expect(page.locator('#help-panel')).not.toHaveClass(/open/);
  await expect(page.locator('#hamburger-menu')).toBeVisible();
});

test('mobile right-swipe goes back to the previous view via history', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 390, height: 720 });
  await page.goto('/');

  const swipeRight = async () => {
    await page.locator('#app').evaluate(app => {
      app.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true, clientX: 60, clientY: 240, pointerId: 1, pointerType: 'touch',
      }));
      app.dispatchEvent(new PointerEvent('pointerup', {
        bubbles: true, clientX: 350, clientY: 245, pointerId: 1, pointerType: 'touch',
      }));
    });
  };

  await expect(page.locator('#view-chat')).toHaveClass(/active/);

  // Navigate to Files via hamburger → pushes history entry
  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-menu').getByRole('button', { name: 'Files' }).click();
  await expect(page.locator('#view-files')).toHaveClass(/active/);

  // Right-swipe → back to Chat
  await swipeRight();
  await expect(page.locator('#view-chat')).toHaveClass(/active/);

  // Navigate to Agents via hamburger → pushes another history entry
  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-menu').getByRole('button', { name: 'Agents' }).click();
  await expect(page.locator('#view-agents')).toHaveClass(/active/);

  // history.back() also works
  await page.evaluate(() => history.back());
  await expect(page.locator('#view-chat')).toHaveClass(/active/);

  // Left-swipe is a no-op (no forward navigation)
  await page.locator('#app').evaluate(app => {
    app.dispatchEvent(new PointerEvent('pointerdown', {
      bubbles: true, clientX: 350, clientY: 240, pointerId: 2, pointerType: 'touch',
    }));
    app.dispatchEvent(new PointerEvent('pointerup', {
      bubbles: true, clientX: 60, clientY: 245, pointerId: 2, pointerType: 'touch',
    }));
  });
  await expect(page.locator('#view-chat')).toHaveClass(/active/);
});

test('mobile swipes do not traverse views without history', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 390, height: 720 });
  await page.goto('/');

  const swipe = async (fromX, toX) => {
    await page.locator('#app').evaluate((app, { fromX, toX }) => {
      app.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true, clientX: fromX, clientY: 240, pointerId: 1, pointerType: 'touch',
      }));
      app.dispatchEvent(new PointerEvent('pointerup', {
        bubbles: true, clientX: toX, clientY: 245, pointerId: 1, pointerType: 'touch',
      }));
    }, { fromX, toX });
  };

  await page.evaluate(() => switchView('files'));
  await expect(page.locator('#view-files')).toHaveClass(/active/);

  await swipe(350, 60);
  await expect(page.locator('#view-files')).toHaveClass(/active/);

  await swipe(60, 350);
  await expect(page.locator('#view-files')).toHaveClass(/active/);
});

test('mobile edit YAML config link returns to Files on browser back', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 390, height: 720 });
  await page.route('**/config/localfile-roots**', r => r.fulfill({
    json: { roots: ['/tmp/work'] },
  }));
  await page.route('**/config/yaml', r => r.fulfill({
    json: { content: 'agents: []\n', revision: 'rev-1', path: '/tmp/squid/config.yaml' },
  }));

  await page.goto('/');
  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-menu').getByRole('button', { name: 'Files' }).click();
  await expect(page.locator('#view-files')).toHaveClass(/active/);

  await page.getByRole('link', { name: /Edit YAML config/ }).click();
  await expect(page.locator('#view-settings')).toHaveClass(/active/);

  await page.evaluate(() => history.back());
  await expect(page.locator('#view-files')).toHaveClass(/active/);
});

test('swipes starting in the edge-exclusion strip are ignored so the OS back gesture can claim them', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 390, height: 720 });
  await page.goto('/');

  // Navigate to Files first so there's a history entry to go back to
  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-menu').getByRole('button', { name: 'Files' }).click();
  await expect(page.locator('#view-files')).toHaveClass(/active/);

  const swipe = async (fromX, toX) => {
    await page.locator('#app').evaluate((app, { fromX, toX }) => {
      app.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true, clientX: fromX, clientY: 240, pointerId: 1, pointerType: 'touch',
      }));
      app.dispatchEvent(new PointerEvent('pointerup', {
        bubbles: true, clientX: toX, clientY: 245, pointerId: 1, pointerType: 'touch',
      }));
    }, { fromX, toX });
  };

  // Starts inside the 24px edge-exclusion strip (left edge) — should not navigate back.
  await swipe(10, 300);
  await expect(page.locator('#view-files')).toHaveClass(/active/);

  // Starts outside the edge-exclusion strip — right-swipe goes back.
  await swipe(60, 350);
  await expect(page.locator('#view-chat')).toHaveClass(/active/);
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
