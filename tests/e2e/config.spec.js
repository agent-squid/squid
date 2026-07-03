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

async function installCodeMirrorStub(page) {
  await page.evaluate(() => {
    function makeDoc(text) {
      const lines = text.split('\n');
      return {
        lines: lines.length,
        toString: () => text,
        line(number) {
          const n = Math.min(Math.max(number, 1), lines.length);
          let from = 0;
          for (let i = 1; i < n; i++) from += lines[i - 1].length + 1;
          return { number: n, from, to: from + lines[n - 1].length };
        },
        lineAt(pos) {
          let from = 0;
          for (let i = 0; i < lines.length; i++) {
            const to = from + lines[i].length;
            if (pos <= to || i === lines.length - 1) return { number: i + 1, from, to };
            from = to + 1;
          }
        },
      };
    }
    class EditorState {
      static create({ doc, extensions }) {
        const d = makeDoc(doc);
        return { doc: d, extensions, selection: { main: { from: 0, to: 0 } }, _text: doc,
          update(changeSpec) { return changeSpec; },
        };
      }
    }
    class EditorView {
      constructor({ state, parent }) {
        this.state = state;
        this.dom = document.createElement('div');
        this.dom.className = 'cm-editor';
        this.dom.textContent = state.doc.toString();
        this.dom.dataset.extensions = state.extensions.flat().join(',');
        parent.appendChild(this.dom);
      }
      dispatch(update) {
        if (update.changes) {
          const { from, to, insert } = update.changes;
          const old = this.state.doc.toString();
          const text = old.slice(0, from) + insert + old.slice(to);
          this.state = EditorState.create({ doc: text, extensions: this.state.extensions });
          this.dom.textContent = text;
        }
        if (update.selection) {
          const from = update.selection.anchor;
          const to = update.selection.head ?? from;
          this.state.selection = { main: { from, to } };
          this.dom.dataset.selection = this.state.doc.toString().slice(Math.min(from, to), Math.max(from, to));
          this.dom.dataset.line = String(this.state.doc.lineAt(from).number);
        }
      }
      focus() {}
      destroy() { this.dom.remove(); }
    }
    const stub = { EditorView, EditorState, basicSetup: [], oneDark: [], atomOneDarkHighlight: 'atom-one-dark-highlight', LANGS: {} };
    Object.defineProperty(window, '_cm', {
      configurable: true,
      get: () => stub,
      set: () => {},
    });
    window._cmPromise = Promise.resolve(true);
  });
}

test('configuration editor loads and saves the complete YAML', async ({ page }) => {
  await mockApp(page);
  await installCodeMirrorStub(page);
  const original = `server:\n  host: "127.0.0.1"\n  port: 8000\nagent:\n  first_byte_timeout: 300\n  response_timeout: 1800\nbackends:\n  qwen:\n    driver: codex\n`;
  let saved;
  await page.route('**/config/yaml', async route => {
    if (route.request().method() === 'PUT') {
      saved = route.request().postDataJSON();
      return route.fulfill({ json: { ok: true, revision: 'rev-2', restart_required: true } });
    }
    return route.fulfill({ json: { content: original, revision: 'rev-1', path: '/home/user/.squid/squid.yaml' } });
  });

  await page.goto('/');
  await page.locator('#hamburger-btn').click();
  await page.getByRole('button', { name: 'Settings' }).click();
  await expect(page.locator('#config-editor .cm-editor')).toBeVisible();
  await expect(page.locator('#config-editor .cm-editor')).toContainText('backends:');
  await expect(page.locator('#config-editor .cm-editor')).toContainText('qwen:');
  // Edit via CM dispatch
  const updated = original.replace('port: 8000', 'port: 8123');
  await page.evaluate(content => {
    const view = window._configCmView;
    if (!view) return;
    view.dispatch(view.state.update({
      changes: { from: 0, to: view.state.doc.length, insert: content },
    }));
  }, updated);
  await page.locator('#config-editor-save').click();

  await expect(page.locator('#config-editor-status')).toHaveText('saved ✓ · restart required');
  expect(saved.revision).toBe('rev-1');
  expect(saved.content).toContain('port: 8123');
});

test('agents backend catalog marks unkeyed DeepSeek as unavailable', async ({ page }) => {
  await mockApp(page);
  await page.route('**/health', r => r.fulfill({ json: {
    status: 'ok',
    backends: {
      deepseek: {
        driver: 'claude',
        kind: 'provider',
        label: 'DeepSeek',
        available: false,
        missing_requirements: ['api_key'],
        gauge: { type: 'deepseek' },
        gauge_authed: false,
      },
    },
  }}));

  await page.goto('/');
  await page.locator('.nav-tab[data-view="agents"]').click();

  const row = page.locator('#backends-catalog .bcat-row').filter({ hasText: 'DeepSeek' });
  await expect(row).toBeVisible();
  await expect(row).toContainText('missing: api_key');
  await expect(row).not.toContainText('detected');
});

test('agents backend catalog marks configured provider backends as ready', async ({ page }) => {
  await mockApp(page);
  await page.route('**/health', r => r.fulfill({ json: {
    status: 'ok',
    backends: {
      qwen: {
        driver: 'codex',
        kind: 'provider',
        label: 'Qwen',
        available: true,
        missing_requirements: [],
        gauge: { type: 'static', text: 'Local' },
        gauge_authed: true,
      },
    },
  }}));

  await page.goto('/');
  await page.locator('.nav-tab[data-view="agents"]').click();

  const row = page.locator('#backends-catalog .bcat-row').filter({ hasText: 'Qwen' });
  await expect(row).toBeVisible();
  await expect(row).toContainText('ready');
  await expect(row).toContainText('configured in YAML');
  await expect(row).not.toContainText('detected');
});

test('agents backend catalog treats keyed DeepSeek as ready without Claude auth hint', async ({ page }) => {
  await mockApp(page);
  await page.route('**/health', r => r.fulfill({ json: {
    status: 'ok',
    backends: {
      deepseek: {
        driver: 'claude',
        label: 'DeepSeek',
        available: true,
        missing_requirements: [],
        provider: 'deepseek',
        gauge: { type: 'deepseek' },
        gauge_authed: true,
      },
    },
  }}));

  await page.goto('/');
  await page.locator('.nav-tab[data-view="agents"]').click();

  const row = page.locator('#backends-catalog .bcat-row').filter({ hasText: 'DeepSeek' });
  await expect(row).toBeVisible();
  await expect(row).toContainText('ready');
  await expect(row).toContainText('uses this backend API key');
  await expect(row).toContainText('gauge ✓');
  await expect(row).not.toContainText('run claude to authenticate');
  await expect(row).not.toContainText('detected');
});

test('blocked file viewer lets the user choose a broader parent and retries', async ({ page }) => {
  await mockApp(page);
  let allowed = false;
  let submitted;
  await page.route('**/localfile**', route => {
    if (!allowed) return route.fulfill({ status: 403, json: { error: 'path outside allowed roots' } });
    return route.fulfill({ status: 200, contentType: 'text/plain', body: 'file contents' });
  });
  await page.route('**/config/localfile-roots', route => {
    submitted = route.request().postDataJSON();
    allowed = true;
    return route.fulfill({ json: { ok: true, root: submitted.root, added: true } });
  });

  await page.goto('/');
  await page.evaluate(() => openFileViewer('/tmp/work/project/file.md'));
  const root = page.locator('.fv-root-row input');
  await expect(root).toHaveValue('/tmp/work/project');
  await root.fill('/tmp/work');
  await page.getByRole('button', { name: 'Allow directory' }).click();

  await expect(page.locator('#file-modal-body')).toContainText('file contents');
  expect(submitted).toEqual({ path: '/tmp/work/project/file.md', root: '/tmp/work' });
});

test('file viewer renders markdown when served as generic binary', async ({ page }) => {
  await mockApp(page);
  await page.route('**/localfile**', route => route.fulfill({
    status: 200,
    contentType: 'application/octet-stream',
    body: '# Markdown\n\nVisible in the viewer',
  }));

  await page.goto('/');
  await page.evaluate(() => openFileViewer('/tmp/work/project/file.md'));

  await expect(page.locator('#file-modal-body')).toContainText('Visible in the viewer');
});

test('file viewer edits text confirmed by content type despite unknown extension', async ({ page }) => {
  await mockApp(page);
  await page.route('**/localfile**', route => route.fulfill({
    status: 200,
    contentType: 'text/plain',
    body: 'unknown extension text',
  }));

  await page.goto('/');
  await page.evaluate(() => openFileViewer('/tmp/work/project/example.customthing'));

  await expect(page.locator('#file-modal-body')).toContainText('unknown extension text');
  await expect(page.getByRole('button', { name: 'Edit file' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Edit history' })).toBeVisible();
});

test('file viewer edit mode uses a distinct editor surface', async ({ page }) => {
  await mockApp(page);
  await page.route('**/localfile**', route => route.fulfill({
    status: 200,
    contentType: 'text/plain',
    body: 'file contents',
  }));

  await page.goto('/');
  await installCodeMirrorStub(page);
  await page.evaluate(() => openFileViewer('/tmp/work/project/file.md'));
  await expect(page.locator('#file-modal-body')).toContainText('file contents');
  const viewerBg = await page.locator('#file-modal-body').evaluate(el => getComputedStyle(el).backgroundColor);
  const viewerFontSize = await page.locator('#file-modal-body').evaluate(el => getComputedStyle(el).fontSize);

  await page.getByRole('button', { name: 'Edit file' }).click();

  await expect(page.locator('#file-modal-box')).toHaveClass(/fv-editing/);
  await expect(page.locator('.fv-edit-status')).toHaveText('Editing');
  await expect(page.locator('.cm-editor')).toBeVisible();
  await expect(page.locator('.cm-editor')).toHaveAttribute('data-extensions', /atom-one-dark-highlight/);
  const editorBg = await page.locator('#file-modal-body').evaluate(el => getComputedStyle(el).backgroundColor);
  expect(editorBg).not.toBe(viewerBg);
  await expect(page.locator('.cm-editor')).toHaveCSS('font-size', viewerFontSize);
  const activeLineBg = await page.locator('.cm-editor').evaluate(el => {
    const activeLine = document.createElement('div');
    activeLine.className = 'cm-activeLine';
    el.appendChild(activeLine);
    return getComputedStyle(activeLine).backgroundColor;
  });
  expect(activeLineBg).toBe('rgba(240, 112, 64, 0.12)');
});

test('file viewer editor can find text and jump to lines', async ({ page }) => {
  await mockApp(page);
  await page.route('**/localfile**', route => route.fulfill({
    status: 200,
    contentType: 'text/plain',
    body: 'alpha\nbeta\ncharlie\ntarget here\n',
  }));

  await page.goto('/');
  await installCodeMirrorStub(page);
  await page.evaluate(() => openFileViewer('/tmp/work/project/file.md', 3));
  await page.getByRole('button', { name: 'Edit file' }).click();

  const findPopover = page.locator('.fv-edit-find-popover');
  await expect(findPopover).toBeHidden();
  await expect(page.locator('.fv-edit-footer .fv-edit-tools')).toBeVisible();
  const editorBox = await page.locator('.cm-editor').boundingBox();
  await page.mouse.click(editorBox.x + 60, editorBox.y + 120);
  await expect(findPopover).toBeHidden();

  await expect(page.getByLabel('Line number')).toHaveValue('3');
  await expect(page.locator('.cm-editor')).toHaveAttribute('data-line', '3');

  await page.getByLabel('Line number').fill('2');
  await page.getByRole('button', { name: 'Go to line' }).click();
  await expect(findPopover).toBeVisible();
  await expect(page.locator('#file-modal-body > .fv-edit-find-popover .fv-edit-tools')).toBeVisible();
  let popoverBox = await findPopover.boundingBox();
  let footerBox = await page.locator('.fv-edit-footer').boundingBox();
  expect(popoverBox.y).toBeLessThan(footerBox.y);
  await expect(page.locator('.fv-edit-status')).toHaveText('Line 2');
  await expect(page.locator('.cm-editor')).toHaveAttribute('data-line', '2');

  await page.getByLabel('Find in editor').fill('target');
  await page.getByLabel('Find in editor').press('Enter');
  await expect(findPopover).toBeVisible();
  popoverBox = await findPopover.boundingBox();
  footerBox = await page.locator('.fv-edit-footer').boundingBox();
  expect(popoverBox.y).toBeLessThan(footerBox.y);
  await expect(page.locator('.fv-edit-status')).toHaveText('Match on line 4');
  await expect(page.locator('.cm-editor')).toHaveAttribute('data-selection', 'target');
  await expect(page.getByLabel('Line number')).toHaveValue('4');

  await page.getByLabel('Line number').fill('2');
  await page.getByRole('button', { name: 'Go to line' }).click();
  await expect(page.locator('.fv-edit-status')).toHaveText('Line 2');
  await expect(page.locator('.cm-editor')).toHaveAttribute('data-line', '2');
});

test('mobile file viewer breadcrumb starts at the path end', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 390, height: 800 });
  await page.route('**/localfile**', route => route.fulfill({
    status: 200,
    contentType: 'text/plain',
    body: 'file contents',
  }));

  await page.goto('/');
  await page.evaluate(() => openFileViewer('/tmp/work/project/src/components/deeply/nested/file.md'));

  await expect(page.locator('#file-modal-breadcrumb')).toContainText('file.md');
  await expect.poll(() => page.locator('#file-modal-breadcrumb').evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
});

test('analytics measures dropdown controls cost and quota columns independently', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex'], topics: ['squid'] },
  }));
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(url);
    return route.fulfill({
      json: [{
        period: url.searchParams.get('period') === 'hourly' ? '2026-06-26 14:00' : '2026-06-26',
        sessions: 2,
        total_turns: 3,
        input_tokens: 1500,
        output_tokens: 700,
        cost_usd: 1.25,
        quota_delta: 2.5,
      }],
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Analytics' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();
  expect(statsRequests.at(-1).searchParams.get('tz_offset_minutes')).not.toBeNull();
  await expect(page.locator('#sf-measures-toggle')).toHaveText('Measures (4)');
  await expect(page.locator('#stats-content th', { hasText: 'Sessions' })).toBeVisible();
  await expect(page.locator('#stats-content th', { hasText: 'Turns' })).toBeVisible();
  await expect(page.locator('#stats-content th', { hasText: 'Tokens In' })).toBeVisible();
  await expect(page.locator('#stats-content th', { hasText: 'Tokens Out' })).toBeVisible();
  await expect(page.locator('#stats-content th', { hasText: 'Cost' })).toHaveCount(0);
  await expect(page.locator('#stats-content th', { hasText: 'Quota meter' })).toHaveCount(0);

  await page.locator('#sf-measures-toggle').click();
  await page.locator('#sf-measures-menu input[value="cost"]').check();
  await expect(page.locator('#sf-measures-toggle')).toHaveText('Measures (5)');
  await expect(page.locator('#stats-content th', { hasText: 'Cost' })).toBeVisible();
  await expect(page.locator('#stats-content')).toContainText('$1.2500');
  await expect(page.locator('#stats-content th', { hasText: 'Quota meter' })).toHaveCount(0);

  await page.locator('#sf-measures-menu input[value="quota"]').check();
  await expect(page.locator('#sf-measures-toggle')).toHaveText('Measures (6)');
  await expect(page.locator('#stats-content th', { hasText: 'Quota meter' })).toBeVisible();
  await expect(page.locator('#stats-content')).toContainText('+2.5 pp');

  await page.locator('#sf-measures-menu input[value="tokens_out"]').uncheck();
  await expect(page.locator('#sf-measures-toggle')).toHaveText('Measures (5)');
  await expect(page.locator('#stats-content th', { hasText: 'Tokens Out' })).toHaveCount(0);

  await page.getByRole('button', { name: 'Hourly' }).click();
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('period')).toBe('hourly');
  expect(statsRequests.at(-1).searchParams.get('tz_offset_minutes')).not.toBeNull();
  await expect(page.locator('#stats-content tbody td').first()).toHaveText('06-26 14:00');
  await expect(page.locator('#stats-content tbody td').first()).not.toContainText('2026');
});

test('/restart clears its persisted draft before the page reloads', async ({ page }) => {
  await mockApp(page);
  await page.route('**/cmd', route => route.fulfill({ json: { ok: true } }));
  await page.goto('/');
  await page.fill('#input', '/restart');
  await page.waitForTimeout(400);
  expect(await page.evaluate(() => localStorage.getItem('squid_draft'))).toBe('/restart');

  await page.keyboard.press('Enter');
  await expect.poll(() => page.evaluate(() => localStorage.getItem('squid_draft'))).toBeNull();
  await expect(page.locator('#input')).toHaveValue('');
});
