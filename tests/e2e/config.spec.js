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

test('configuration editor shows backend validation errors prominently', async ({ page }) => {
  await mockApp(page);
  await installCodeMirrorStub(page);
  const invalid = `server:\n  host: "127.0.0.1"\n  port: 8000\nagent:\n  first_byte_timeout: 300\n  response_timeout: 1800\nbackends:\n  codex-live:\n    driver: codex\n    protocol: interactive-cli\n`;
  await page.route('**/config/yaml', async route => {
    if (route.request().method() === 'PUT') {
      return route.fulfill({
        status: 400,
        json: { error: "Backend 'codex-live' protocol 'interactive-cli' is not supported by driver 'codex'" },
      });
    }
    return route.fulfill({ json: { content: invalid, revision: 'rev-1', path: '/home/user/.squid/squid.yaml' } });
  });

  await page.goto('/');
  await page.locator('#hamburger-btn').click();
  await page.getByRole('button', { name: 'Settings' }).click();
  await page.locator('#config-editor-save').click();

  const status = page.locator('#config-editor-status');
  await expect(status).toHaveText("Error: Backend 'codex-live' protocol 'interactive-cli' is not supported by driver 'codex'");
  await expect(status).toHaveClass(/error/);
  await expect(status).toHaveCSS('color', 'rgb(255, 107, 107)');
  await expect(status).toHaveCSS('border-top-color', 'rgb(255, 77, 77)');
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
  await expect(row).toContainText('configure DeepSeek API key in backend YAML');
  await expect(row).not.toContainText('curl -fsSL');
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
        protocol: 'oneshot-cli',
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
  await expect(row).toContainText('protocol: oneshot-cli');
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
  await expect(row).toContainText('DeepSeek API key configured');
  await expect(row).toContainText('gauge ✓');
  await expect(row).not.toContainText('run claude to authenticate');
  await expect(row).not.toContainText('detected');
});

test('edit button prefills agent form with existing agent values', async ({ page }) => {
  await mockApp(page);
  await page.route('**/health', r => r.fulfill({ json: {
    status: 'ok',
    backends: {
      claude: { driver: 'claude', label: 'Claude', available: true, protocol: 'interactive-cli', missing_requirements: [], gauge: { type: 'none' } },
      codex:  { driver: 'codex',  label: 'Codex',  available: true, protocol: 'oneshot-cli',    missing_requirements: [], gauge: { type: 'none' } },
    },
  }}));
  await page.route('**/config/agents', r => r.fulfill({ json: [
    { name: 'codex', backend: 'codex', model: null, cwd: null },
    { name: 'haiku', backend: 'claude', model: 'claude-haiku-4-5', cwd: '/tmp/work' },
  ]}));

  await page.goto('/');
  await page.locator('.nav-tab[data-view="agents"]').click();

  const row = page.locator('#agents-list tbody tr').filter({ hasText: 'haiku' });
  await row.locator('.edit-btn').click();

  await expect(page.locator('#af-name')).toHaveValue('haiku');
  await expect(page.locator('#af-backend')).toHaveValue('claude');
  await expect(page.locator('#af-model')).toHaveValue('claude-haiku-4-5');
  await expect(page.locator('#af-cwd')).toHaveValue('/tmp/work');
});

test('mobile agents list hides cwd while edit still exposes it', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 390, height: 720 });
  const longModel = 'claude-sonnet-4-5-20260711-extra-long-mobile-overflow-check';
  await page.route('**/health', r => r.fulfill({ json: {
    status: 'ok',
    backends: {
      claude: { driver: 'claude', label: 'Claude', available: true, protocol: 'interactive-cli', missing_requirements: [], gauge: { type: 'none' } },
    },
  }}));
  await page.route('**/config/agents', r => r.fulfill({ json: [
    { name: 'haiku', backend: 'claude', model: longModel, cwd: '/tmp/work' },
  ]}));

  await page.goto('/');
  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-menu').getByRole('button', { name: 'Agents' }).click();

  const row = page.locator('#agents-list tbody tr').filter({ hasText: 'haiku' });
  await expect(row.locator('.col-cwd')).toBeHidden();
  await expect(row.locator('.agent-model')).toHaveAttribute('title', longModel);
  await expect(row.locator('.agent-model')).toHaveText(longModel);
  await expect(row.locator('.agent-model')).toBeVisible();
  expect(await row.locator('.agent-model').evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);
  expect(await page.locator('#agents-list').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  const saveButton = page.locator('#agent-form button[type="submit"]');
  await expect(saveButton.locator('.agent-save-icon')).toBeVisible();
  await expect(saveButton.locator('.agent-save-label')).toBeHidden();
  const formBoxes = await page.locator('#af-name, #af-backend, #af-model, #af-cwd, #agent-form button[type="submit"]').evaluateAll(elements =>
    elements.map(el => el.getBoundingClientRect())
  );
  expect(Math.max(...formBoxes.map(box => box.top)) - Math.min(...formBoxes.map(box => box.top))).toBeLessThan(2);
  expect(await page.locator('#agent-form').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);

  await row.locator('.edit-btn').click();
  await expect(page.locator('#af-cwd')).toHaveValue('/tmp/work');
  await expect(page.locator('#af-model')).toHaveValue(longModel);
});

test('agents tab separates agents and backends sections with a divider', async ({ page }) => {
  await mockApp(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Agents' }).click();

  const divider = page.locator('#settings-agents > .agents-section-divider');
  await expect(divider).toBeVisible();
  await expect(divider).toHaveCSS('border-top-style', 'solid');
  await expect.poll(() => page.locator('#settings-agents').evaluate(el => {
    const children = Array.from(el.children);
    const divider = el.querySelector('.agents-section-divider');
    const backendsLabel = Array.from(el.querySelectorAll('.settings-label')).find(label => label.textContent.trim() === 'Backends');
    return {
      formBeforeDivider: children.indexOf(document.getElementById('agent-form')) < children.indexOf(divider),
      dividerBeforeBackends: children.indexOf(divider) < children.indexOf(backendsLabel),
    };
  })).toEqual({ formBeforeDivider: true, dividerBeforeBackends: true });
});

test('agents and settings omit redundant section titles', async ({ page }) => {
  await mockApp(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Agents' }).click();

  await expect(page.locator('#settings-agents > .settings-label')).toHaveText(['Backends']);
  await expect(page.locator('#settings-agents > .settings-label', { hasText: 'Agents' })).toHaveCount(0);

  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-menu').getByRole('button', { name: 'Settings' }).click();
  await expect(page.locator('#config-editor-header > .settings-label')).toHaveCount(0);
  await expect(page.locator('#config-editor-actions')).toBeVisible();
  await expect.poll(() => page.locator('#config-editor-header').evaluate(header => {
    const actions = document.getElementById('config-editor-actions').getBoundingClientRect();
    const headerBox = header.getBoundingClientRect();
    return Math.abs(headerBox.right - actions.right);
  })).toBeLessThan(1);
});

test('files and settings share mobile top spacing below the nav', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 390, height: 720 });
  await page.route('**/config/localfile-roots**', route => route.fulfill({
    json: { roots: ['/tmp/work'] },
  }));
  await page.route('**/config/yaml', route => route.fulfill({
    json: { content: 'agents: []\n', revision: 'rev-1', path: '/tmp/squid/config.yaml' },
  }));

  await page.goto('/');
  await expect(page.locator('#view-chat')).toHaveCSS('padding-top', '0px');

  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-menu').getByRole('button', { name: 'Files' }).click();
  await expect(page.locator('#file-modal-box')).toBeVisible();
  const filesTop = await page.locator('#file-modal-box').evaluate(el => el.getBoundingClientRect().top);

  await page.locator('#hamburger-btn').click();
  await page.locator('#hamburger-menu').getByRole('button', { name: 'Settings' }).click();
  await expect(page.locator('#config-editor-actions')).toBeVisible();
  const settingsTop = await page.locator('#config-editor-actions').evaluate(el => el.getBoundingClientRect().top);
  const topbarBottom = await page.locator('#topbar').evaluate(el => el.getBoundingClientRect().bottom);

  expect(Math.abs(settingsTop - filesTop)).toBeLessThan(2);
  expect(settingsTop - topbarBottom).toBeGreaterThan(12);
});

test('edit button prefills form with empty model and cwd when not set', async ({ page }) => {
  await mockApp(page);
  await page.route('**/health', r => r.fulfill({ json: {
    status: 'ok',
    backends: {
      codex: { driver: 'codex', label: 'Codex', available: true, protocol: 'oneshot-cli', missing_requirements: [], gauge: { type: 'none' } },
    },
  }}));
  await page.route('**/config/agents', r => r.fulfill({ json: [
    { name: 'codex', backend: 'codex', model: null, cwd: null },
  ]}));

  await page.goto('/');
  await page.locator('.nav-tab[data-view="agents"]').click();

  await page.locator('#agents-list tbody tr .edit-btn').click();

  await expect(page.locator('#af-name')).toHaveValue('codex');
  await expect(page.locator('#af-backend')).toHaveValue('codex');
  await expect(page.locator('#af-model')).toHaveValue('');
  await expect(page.locator('#af-cwd')).toHaveValue('');
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
    json: { agents: ['codex', 'clive'], topics: ['squid', 'ops'] },
  }));
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    const period = url.searchParams.get('period') === 'hourly' ? '2026-06-26 14:00' : '2026-06-26';
    statsRequests.push(url);
    if (url.searchParams.get('breakdown') === 'agent') {
      const selectedAgents = (url.searchParams.get('agent') || '').split(',').filter(Boolean);
      const rows = [
        { period, agent_key: 'codex', agent: 'codex', sessions: 7, total_turns: 7, input_tokens: 1400, output_tokens: 300, cost_usd: 1.4 },
        { period, agent_key: 'clive', agent: 'clive', sessions: 2, total_turns: 2, input_tokens: 300, output_tokens: 80, cost_usd: 0.3 },
        { period, agent_key: 'cursor', agent: 'cursor', sessions: 1, total_turns: 1, input_tokens: 200, output_tokens: 60, cost_usd: 0.2 },
      ].filter(row => !selectedAgents.length || selectedAgents.includes(row.agent));
      return route.fulfill({
        json: rows,
      });
    }
    return route.fulfill({
      json: [{
        period,
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
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#stats-content table')).toBeVisible();
  await expect(page.locator('#stats-tabs')).toHaveCount(0);
  await expect(page.locator('#sf-period')).toHaveValue('hourly');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('period')).toBe('hourly');
  expect(statsRequests.at(-1).searchParams.get('breakdown')).toBeNull();
  expect(statsRequests.at(-1).searchParams.get('days')).toBe('7');
  expect(statsRequests.at(-1).searchParams.get('tz_offset_minutes')).not.toBeNull();
  await expect(page.locator('#sf-breakdown option')).toHaveCount(5);
  await expect(page.locator('#sc-compare-btn')).toBeVisible();
  await expect(page.locator('#sf-measures-toggle')).toHaveText('Measures (4)');
  await expect.poll(() => page.evaluate(() => {
    const tops = ['sf-period', 'sf-measures-toggle', 'sf-topic-toggle', 'sf-agent-toggle', 'sf-adhoc']
      .map(id => document.getElementById(id)?.getBoundingClientRect().top ?? 0);
    return Math.max(...tops) - Math.min(...tops);
  })).toBeLessThan(3);
  await expect(page.locator('#stats-content th', { hasText: 'Sessions' })).toBeVisible();
  await expect(page.locator('#stats-content th', { hasText: 'Turns' })).toBeVisible();
  await expect(page.locator('#stats-content th', { hasText: 'Tokens In' })).toBeVisible();
  await expect(page.locator('#stats-content th', { hasText: 'Tokens Out' })).toBeVisible();
  await expect(page.locator('#stats-content th', { hasText: 'Cost' })).toHaveCount(0);
  await expect(page.locator('#stats-content th', { hasText: 'Quota meter' })).toHaveCount(0);

  await page.locator('#sf-topic-toggle').click();
  await page.locator('#sf-topic-menu input[value="squid"]').check();
  await expect(page.locator('#sf-topic-toggle')).toHaveText('#squid');
  await page.locator('#sf-topic-menu input[value="ops"]').check();
  await expect(page.locator('#sf-topic-toggle')).toHaveText('2 Topics');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('topic')).toBe('squid,ops');

  await page.locator('#sf-agent-toggle').click();
  await page.locator('#sf-agent-menu input[value="codex"]').check();
  await expect(page.locator('#sf-agent-toggle')).toHaveText('@codex');
  await page.locator('#sf-agent-menu input[value="clive"]').check();
  await expect(page.locator('#sf-agent-toggle')).toHaveText('2 Agents');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('agent')).toBe('codex,clive');

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

  await page.locator('#sf-period').selectOption('daily');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('period')).toBe('daily');
  expect(statsRequests.at(-1).searchParams.get('tz_offset_minutes')).not.toBeNull();
  await expect(page.locator('#stats-content tbody td').first()).toHaveText('2026-06-26');

  await page.locator('#sf-period').selectOption('hourly');
  await page.locator('#sf-breakdown').selectOption('agent');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('breakdown')).toBe('agent');
  expect(statsRequests.at(-1).searchParams.get('agent')).toBe('codex,clive');
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await expect.poll(() => page.locator('.stats-table-scroll').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await expect.poll(() => page.locator('#stats-content thead th').nth(1).evaluate(el => el.getBoundingClientRect().width)).toBeLessThan(125);
  await expect(page.locator('#sf-agent-toggle')).toHaveText('2 Agents');
  await expect(page.locator('#sc-compare-btn')).toBeHidden();
  await expect(page.locator('#sf-measures')).toBeVisible();
  await expect(page.locator('#sf-measures-toggle')).toBeDisabled();
  const disabledMeasuresStyle = await page.locator('#sf-measures-toggle').evaluate(el => {
    const style = getComputedStyle(el);
    return {
      backgroundColor: style.backgroundColor,
      borderColor: style.borderColor,
      color: style.color,
      opacity: style.opacity,
    };
  });
  await page.locator('#sf-measures-toggle').hover({ force: true });
  await expect.poll(() => page.locator('#sf-measures-toggle').evaluate(el => {
    const style = getComputedStyle(el);
    return {
      backgroundColor: style.backgroundColor,
      borderColor: style.borderColor,
      color: style.color,
      opacity: style.opacity,
    };
  })).toEqual(disabledMeasuresStyle);
  await expect(page.locator('#stats-filters .sf-sep')).toBeVisible();
  await expect(page.getByRole('columnheader', { name: '@codex+', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: '@clive+', exact: true })).toBeVisible();
  await expect(page.locator('#stats-content thead th')).toHaveText(['Hour', '@clive+', '@codex+', 'Misc', 'Total']);
  await expect(page.locator('#stats-content th', { hasText: 'Misc' })).toHaveCount(1);
  await expect(page.locator('#stats-content tfoot')).toContainText('9');
  await page.locator('#sf-adhoc').selectOption('session');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('adhoc')).toBe('session');
  await page.locator('#sc-y1').selectOption('cost');
  await expect(page.locator('#stats-content tfoot')).toContainText('$1.7000');
  await expect(page.locator('#stats-content tfoot')).not.toContainText('9');

  await page.locator('#sf-breakdown').selectOption('');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('breakdown')).toBeNull();
  expect(statsRequests.at(-1).searchParams.get('topic')).toBeNull();
  expect(statsRequests.at(-1).searchParams.get('agent')).toBeNull();
  expect(statsRequests.at(-1).searchParams.get('adhoc')).toBeNull();
  await expect(page.locator('#sf-topic-toggle')).toHaveText('All Topics');
  await expect(page.locator('#sf-agent-toggle')).toHaveText('All Agents');
  await expect(page.locator('#sf-adhoc')).toHaveValue('all');
  await page.locator('#sf-topic-toggle').click();
  await expect(page.locator('#sf-topic-menu input[value="squid"]')).not.toBeChecked();
  await expect(page.locator('#sf-topic-menu input[value="ops"]')).not.toBeChecked();
  await page.locator('#sf-agent-toggle').click();
  await expect(page.locator('#sf-agent-menu input[value="codex"]')).not.toBeChecked();
  await expect(page.locator('#sf-agent-menu input[value="clive"]')).not.toBeChecked();
  await expect(page.locator('#sc-compare-btn')).toBeVisible();
  await expect(page.locator('#sf-measures')).toBeVisible();
  await expect(page.locator('#sf-measures-toggle')).toBeEnabled();
});

test('agent breakdown defaults to top four agents when none are selected', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex', 'clive', 'cursor', 'haiku'], topics: [] },
  }));
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(url);
    if (url.searchParams.get('breakdown') === 'agent') {
      return route.fulfill({
        json: [
          { period: '2026-06-26 14:00', agent_key: 'codex', agent: 'codex', sessions: 1, total_turns: 9 },
          { period: '2026-06-26 14:00', agent_key: 'clive', agent: 'clive', sessions: 1, total_turns: 7 },
          { period: '2026-06-26 14:00', agent_key: 'cursor', agent: 'cursor', sessions: 1, total_turns: 5 },
          { period: '2026-06-26 14:00', agent_key: 'haiku', agent: 'haiku', sessions: 1, total_turns: 2 },
        ],
      });
    }
    return route.fulfill({ json: [{ period: '2026-06-26 14:00', sessions: 4, total_turns: 23 }] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-breakdown').selectOption('agent');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('breakdown')).toBe('agent');
  expect(statsRequests.at(-1).searchParams.get('agent')).toBeNull();
  await expect(page.locator('#sf-agent-toggle')).toHaveText('4 Agents');
  await page.locator('#sf-agent-toggle').click();
  await expect(page.locator('#sf-agent-menu input[value="codex"]')).toBeChecked();
  await expect(page.locator('#sf-agent-menu input[value="clive"]')).toBeChecked();
  await expect(page.locator('#sf-agent-menu input[value="cursor"]')).toBeChecked();
  await expect(page.locator('#sf-agent-menu input[value="haiku"]')).toBeChecked();
  await expect(page.getByRole('columnheader', { name: '@codex+', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: '@clive+', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: '@cursor+', exact: true })).toBeVisible();
  await expect(page.locator('#stats-content thead th')).toHaveText(['Hour', '@clive+', '@codex+', '@cursor+', '@haiku+', 'Misc', 'Total']);
  await expect(page.locator('#stats-content th', { hasText: 'Misc' })).toHaveCount(1);
});

test('stats breakdown caps visible series and keeps edge columns sticky while scrolling', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 900, height: 720 });
  const agents = Array.from({ length: 10 }, (_, i) => `agent${String(i + 1).padStart(2, '0')}`);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents, topics: [] },
  }));
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('breakdown') === 'agent') {
      return route.fulfill({
        json: agents.map((agent, index) => ({
          period: '2026-06-26 14:00',
          agent_key: agent,
          agent,
          sessions: 1,
          total_turns: index + 1,
        })),
      });
    }
    return route.fulfill({ json: [{ period: '2026-06-26 14:00', sessions: 10, total_turns: 55 }] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-agent-toggle').click();
  for (const agent of agents) {
    await page.locator(`#sf-agent-menu input[value="${agent}"]`).check();
  }
  await page.locator('#sf-breakdown').selectOption('agent');

  await expect(page.locator('#stats-content thead th')).toHaveText([
    'Hour',
    '@agent03+',
    '@agent04+',
    '@agent05+',
    '@agent06+',
    '@agent07+',
    '@agent08+',
    '@agent09+',
    '@agent10+',
    'Misc',
    'Total',
  ]);
  await expect(page.locator('#stats-content tbody tr').first().locator('td')).toHaveText([
    '06-26 14:00',
    '3',
    '4',
    '5',
    '6',
    '7',
    '8',
    '9',
    '10',
    '3',
    '55',
  ]);
  await expect(page.getByRole('columnheader', { name: '@agent01', exact: true })).toHaveCount(0);
  await expect(page.getByRole('columnheader', { name: '@agent02', exact: true })).toHaveCount(0);
  await page.locator('#stats-content tfoot td').first().getByRole('button', { name: 'Sort breakdown columns by total' }).nth(1).click();
  await expect(page.locator('#stats-content thead th')).toHaveText([
    'Hour',
    '@agent10+',
    '@agent09+',
    '@agent08+',
    '@agent07+',
    '@agent06+',
    '@agent05+',
    '@agent04+',
    '@agent03+',
    'Misc',
    'Total',
  ]);
  await expect(page.locator('#stats-content tbody tr').first().locator('td.stats-misc-col')).toHaveText('3');
  await expect(page.locator('#stats-content thead th', { hasText: 'Misc' })).toHaveCount(1);
  await expect.poll(() => page.locator('.stats-table-scroll').evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);

  const before = await page.locator('.stats-table-scroll').evaluate(el => {
    const first = el.querySelector('tbody td:first-child').getBoundingClientRect();
    const misc = el.querySelector('tbody td.stats-misc-col').getBoundingClientRect();
    const total = el.querySelector('tbody td.stats-total-col').getBoundingClientRect();
    return { firstLeft: first.left, miscRight: misc.right, totalRight: total.right };
  });
  await page.locator('.stats-table-scroll').evaluate(el => { el.scrollLeft = el.scrollWidth; });
  const after = await page.locator('.stats-table-scroll').evaluate(el => {
    const first = el.querySelector('tbody td:first-child').getBoundingClientRect();
    const misc = el.querySelector('tbody td.stats-misc-col').getBoundingClientRect();
    const total = el.querySelector('tbody td.stats-total-col').getBoundingClientRect();
    return { firstLeft: first.left, miscRight: misc.right, totalRight: total.right };
  });
  expect(Math.abs(after.firstLeft - before.firstLeft)).toBeLessThan(2);
  expect(Math.abs(after.miscRight - before.miscRight)).toBeLessThan(2);
  expect(Math.abs(after.totalRight - before.totalRight)).toBeLessThan(2);
});

test('stats breakdown horizontal scroll leaves pager controls fixed', async ({ page }) => {
  await mockApp(page);
  await page.setViewportSize({ width: 900, height: 720 });
  const agents = Array.from({ length: 10 }, (_, i) => `agent${String(i + 1).padStart(2, '0')}`);
  const periods = Array.from({ length: 11 }, (_, i) => `2026-06-${String(i + 1).padStart(2, '0')} 14:00`);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents, topics: [] },
  }));
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('breakdown') === 'agent') {
      return route.fulfill({
        json: periods.flatMap((period, periodIndex) => agents.map((agent, agentIndex) => ({
          period,
          agent_key: agent,
          agent,
          sessions: 1,
          total_turns: periodIndex + agentIndex + 1,
        }))),
      });
    }
    return route.fulfill({ json: [{ period: periods[0], sessions: 10, total_turns: 55 }] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-agent-toggle').click();
  for (const agent of agents) {
    await page.locator(`#sf-agent-menu input[value="${agent}"]`).check();
  }
  await page.locator('#sf-breakdown').selectOption('agent');

  const scroll = page.locator('.stats-table-scroll');
  const pager = page.locator('.stats-pager');
  await expect(pager).toBeVisible();
  await expect.poll(() => scroll.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);

  const before = await pager.boundingBox();
  await scroll.evaluate(el => { el.scrollLeft = el.scrollWidth; });
  const after = await pager.boundingBox();
  expect(Math.abs(after.x - before.x)).toBeLessThan(1);
  expect(Math.abs(after.width - before.width)).toBeLessThan(1);
  await expect(pager).toContainText('1 / 2');
});

test('stats breakdown columns can sort by footer totals', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex', 'clive', 'cursor', 'haiku'], topics: [] },
  }));
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(url);
    if (url.searchParams.get('breakdown') === 'agent') {
      return route.fulfill({
        json: [
          { period: '2026-06-26 14:00', agent_key: 'codex', agent: 'codex', sessions: 1, total_turns: 9 },
          { period: '2026-06-26 14:00', agent_key: 'clive', agent: 'clive', sessions: 1, total_turns: 7 },
          { period: '2026-06-26 14:00', agent_key: 'cursor', agent: 'cursor', sessions: 1, total_turns: 5 },
          { period: '2026-06-26 14:00', agent_key: 'haiku', agent: 'haiku', sessions: 1, total_turns: 2 },
        ],
      });
    }
    return route.fulfill({ json: [{ period: '2026-06-26 14:00', sessions: 4, total_turns: 23 }] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.getByRole('button', { name: 'Sort breakdown columns by total' })).toHaveCount(0);

  await page.locator('#sf-breakdown').selectOption('agent');
  await expect(page.locator('#stats-content thead th')).toHaveText(['Hour', '@clive+', '@codex+', '@cursor+', '@haiku+', 'Misc', 'Total']);
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('breakdown_sort')).toBe('name');
  expect(statsRequests.at(-1).searchParams.get('breakdown_sort_dir')).toBe('asc');
  expect(new URL(page.url()).search).toBe('');
  await expect(page.getByRole('button', { name: 'Sort breakdown columns by total' })).toHaveCount(2);
  await expect(page.getByRole('button', { name: 'Sort breakdown columns by name' })).toHaveCount(2);
  await expect(page.locator('#stats-content thead th').first().getByRole('button', { name: 'Sort breakdown columns by name' })).toHaveCount(2);
  await expect(page.locator('#stats-content tfoot td').first().getByRole('button', { name: 'Sort breakdown columns by total' })).toHaveCount(2);
  await expect(page.locator('#stats-content thead th').nth(1).getByRole('button')).toHaveCount(0);
  await expect(page.locator('#stats-content thead th').first().locator('button.active')).toHaveCount(1);
  await expect(page.locator('#stats-content tfoot td').first().locator('button.active')).toHaveCount(0);

  const nameSortButtons = page.locator('#stats-content thead th').first().getByRole('button', { name: 'Sort breakdown columns by name' });
  const totalSortButtons = page.locator('#stats-content tfoot td').first().getByRole('button', { name: 'Sort breakdown columns by total' });

  await totalSortButtons.nth(1).click();
  await expect(page.locator('#stats-content thead th')).toHaveText(['Hour', '@codex+', '@clive+', '@cursor+', '@haiku+', 'Misc', 'Total']);
  expect(new URL(page.url()).search).toBe('');
  await expect(page.locator('#stats-content thead th').first().locator('button.active')).toHaveCount(0);
  await expect(page.locator('#stats-content tfoot td').first().locator('button.active')).toHaveCount(1);

  await totalSortButtons.nth(0).click();
  await expect(page.locator('#stats-content thead th')).toHaveText(['Hour', '@haiku+', '@cursor+', '@clive+', '@codex+', 'Misc', 'Total']);
  expect(new URL(page.url()).search).toBe('');
  await expect(page.locator('#stats-content thead th').first().locator('button.active')).toHaveCount(0);
  await expect(page.locator('#stats-content tfoot td').first().locator('button.active')).toHaveCount(1);

  await nameSortButtons.nth(1).click();
  await expect(page.locator('#stats-content thead th')).toHaveText(['Hour', '@haiku+', '@cursor+', '@codex+', '@clive+', 'Misc', 'Total']);
  expect(new URL(page.url()).search).toBe('');
  await expect(page.locator('#stats-content thead th').first().locator('button.active')).toHaveCount(1);
  await expect(page.locator('#stats-content tfoot td').first().locator('button.active')).toHaveCount(0);

  await nameSortButtons.nth(0).click();
  await expect(page.locator('#stats-content thead th')).toHaveText(['Hour', '@clive+', '@codex+', '@cursor+', '@haiku+', 'Misc', 'Total']);
  await expect(page.locator('#stats-content thead th').first().locator('button.active')).toHaveCount(1);
  await expect(page.locator('#stats-content tfoot td').first().locator('button.active')).toHaveCount(0);
});

test('legacy stats URL state does not open stats or restore filters', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex', 'clive', 'cursor', 'haiku'], topics: [] },
  }));
  await page.route('**/stats?**', route => route.fulfill({
    json: [
      { period: '2026-06-26 14:00', agent_key: 'codex', agent: 'codex', sessions: 1, total_turns: 9 },
      { period: '2026-06-26 14:00', agent_key: 'clive', agent: 'clive', sessions: 1, total_turns: 7 },
      { period: '2026-06-26 14:00', agent_key: 'cursor', agent: 'cursor', sessions: 1, total_turns: 5 },
      { period: '2026-06-26 14:00', agent_key: 'haiku', agent: 'haiku', sessions: 1, total_turns: 2 },
    ],
  }));

  await page.goto('/?view=stats&period=hourly&days=7&breakdown=agent&breakdown_sort=total&breakdown_sort_dir=asc');

  await expect(page.locator('#view-chat')).toHaveClass(/active/);
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#sf-period')).toHaveValue('hourly');
  await expect(page.locator('#sf-breakdown')).toHaveValue('');
  await expect(page.locator('#stats-content thead th').first()).toHaveText('Hour');
  await expect(page.locator('#stats-content tfoot td').first().locator('button.active')).toHaveCount(0);
});

test('stats interactions do not persist URL state and refresh returns to chat', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex'], topics: [] },
  }));
  await page.route('**/stats?**', route => route.fulfill({
    json: [{ period: '2026-06-26 14:00', agent_key: 'codex', agent: 'codex', sessions: 1, total_turns: 9 }],
  }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await expect(page.locator('#view-stats')).toHaveClass(/active/);
  await page.locator('#sf-breakdown').selectOption('agent');
  await expect(page.locator('#stats-content thead th')).toHaveText(['Hour', '@codex+', 'Misc', 'Total']);
  expect(new URL(page.url()).search).toBe('');

  await page.reload();
  await expect(page.locator('#view-chat')).toHaveClass(/active/);
  await expect(page.locator('#view-stats')).not.toHaveClass(/active/);
});

test('agent session type breakdown expands selected base agents into session variants', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex', 'clive'], topics: [] },
  }));
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(url);
    if (url.searchParams.get('breakdown') === 'agent_session') {
      const selectedAgents = (url.searchParams.get('agent') || '').split(',').filter(Boolean);
      const rows = [
        { period: '2026-06-26 14:00', agent_key: 'codex', agent: 'codex', sessions: 4, total_turns: 4 },
        { period: '2026-06-26 14:00', agent_key: 'codex!', agent: 'codex!', sessions: 3, total_turns: 3 },
        { period: '2026-06-26 14:00', agent_key: 'clive', agent: 'clive', sessions: 2, total_turns: 2 },
      ].filter(row => !selectedAgents.length || selectedAgents.includes(row.agent.replace(/!$/, '')));
      return route.fulfill({
        json: rows,
      });
    }
    return route.fulfill({ json: [{ period: '2026-06-26 14:00', sessions: 9, total_turns: 9 }] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-agent-toggle').click();
  await page.locator('#sf-agent-menu input[value="codex"]').check();
  await page.locator('#sf-breakdown').selectOption('agent_session');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('breakdown')).toBe('agent_session');
  expect(statsRequests.at(-1).searchParams.get('agent')).toBe('codex');
  await expect(page.locator('#sf-agent-toggle')).toHaveText('@codex');
  await expect(page.getByRole('columnheader', { name: '@codex', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: '@codex!', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: '@clive', exact: true })).toHaveCount(0);
  await expect(page.locator('#stats-content th', { hasText: 'Misc' })).toHaveCount(1);
  await expect(page.locator('#stats-content tfoot')).toContainText('7');
});

test('agent session type breakdown defaults two base agents into four session lanes', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex', 'clive', 'opencode', 'haiku'], topics: [] },
  }));
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(url);
    if (url.searchParams.get('breakdown') === 'agent_session') {
      return route.fulfill({
        json: [
          { period: '2026-06-26 14:00', agent_key: 'codex', agent: 'codex', sessions: 4, total_turns: 4 },
          { period: '2026-06-26 14:00', agent_key: 'codex!', agent: 'codex!', sessions: 0, total_turns: 6 },
          { period: '2026-06-26 14:00', agent_key: 'clive', agent: 'clive', sessions: 8, total_turns: 8 },
          { period: '2026-06-26 14:00', agent_key: 'opencode!', agent: 'opencode!', sessions: 0, total_turns: 7 },
          { period: '2026-06-26 14:00', agent_key: 'haiku', agent: 'haiku', sessions: 1, total_turns: 1 },
        ],
      });
    }
    return route.fulfill({ json: [{ period: '2026-06-26 14:00', sessions: 13, total_turns: 26 }] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-breakdown').selectOption('agent_session');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('breakdown')).toBe('agent_session');
  expect(statsRequests.at(-1).searchParams.get('agent')).toBeNull();
  await expect(page.locator('#sf-agent-toggle')).toHaveText('2 Agents');
  await expect(page.locator('#stats-content thead th')).toHaveText([
    'Hour', '@clive', '@clive!', '@codex', '@codex!', 'Misc', 'Total',
  ]);
  await page.locator('#sf-agent-toggle').click();
  await expect(page.locator('#sf-agent-menu input[value="codex"]')).toBeChecked();
  await expect(page.locator('#sf-agent-menu input[value="clive"]')).toBeChecked();
  await expect(page.locator('#sf-agent-menu input[value="opencode"]')).not.toBeChecked();
  await expect(page.locator('#sf-agent-menu input[value="haiku"]')).not.toBeChecked();
});

test('agent breakdown shows agent! columns based on session type filter', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex', 'clive'], topics: [] },
  }));
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(url);
    const adhoc = url.searchParams.get('adhoc');
    if (adhoc === 'session') {
      return route.fulfill({
        json: [
          { period: '2026-06-26 14:00', agent_key: 'codex', agent: 'codex', sessions: 4, total_turns: 4 },
        ],
      });
    }
    if (adhoc === 'adhoc') {
      return route.fulfill({
        json: [
          { period: '2026-06-26 14:00', agent_key: 'codex!', agent: 'codex!', sessions: 3, total_turns: 3 },
        ],
      });
    }
    return route.fulfill({
      json: [
        { period: '2026-06-26 14:00', agent_key: 'codex', agent: 'codex', sessions: 4, total_turns: 4 },
        { period: '2026-06-26 14:00', agent_key: 'codex!', agent: 'codex!', sessions: 3, total_turns: 3 },
      ],
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-breakdown').selectOption('agent');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('breakdown')).toBe('agent');
  await expect(page.locator('#sf-adhoc option[value="all"]')).toHaveText('Sess + Adhoc');
  await expect(page.locator('#sf-adhoc option[value="session"]')).toHaveText('Session');
  await expect(page.locator('#sf-adhoc option[value="adhoc"]')).toHaveText('Adhoc');
  await expect(page.locator('#sf-adhoc')).toHaveValue('all');

  // default (session + adhoc): non-session breakdown aggregates both modes under the base agent
  await expect(page.getByRole('columnheader', { name: '@codex+', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: '@codex!', exact: true })).toHaveCount(0);

  // session only: bare @codex label (no suffix)
  await page.locator('#sf-adhoc').selectOption('session');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('adhoc')).toBe('session');
  await expect(page.getByRole('columnheader', { name: '@codex', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: '@codex!', exact: true })).toHaveCount(0);

  // adhoc only: @codex! label
  await page.locator('#sf-adhoc').selectOption('adhoc');
  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('adhoc')).toBe('adhoc');
  await expect(page.getByRole('columnheader', { name: '@codex!', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: '@codex+', exact: true })).toHaveCount(0);
});

test('topic agent session breakdown expands coarse topic and agent filters into session lanes', async ({ page }) => {
  await mockApp(page);
  await page.route('**/stats/filters', route => route.fulfill({
    json: { agents: ['codex', 'clive'], topics: ['squid', 'ops'] },
  }));
  const statsRequests = [];
  await page.route('**/stats?**', route => {
    const url = new URL(route.request().url());
    statsRequests.push(url);
    if (url.searchParams.get('breakdown') === 'topic_agent') {
      return route.fulfill({
        json: [
          { period: '2026-06-26 14:00', topic: 'squid', agent_key: 'codex', agent: 'codex', sessions: 4, total_turns: 7 },
          { period: '2026-06-26 14:00', topic: 'squid', agent_key: 'clive', agent: 'clive', sessions: 2, total_turns: 3 },
          { period: '2026-06-26 14:00', topic: 'ops', agent_key: 'codex', agent: 'codex', sessions: 1, total_turns: 1 },
        ],
      });
    }
    if (url.searchParams.get('breakdown') === 'topic_agent_session') {
      return route.fulfill({
        json: [
          { period: '2026-06-26 14:00', topic: 'squid', agent_key: 'codex', agent: 'codex', session_type: 'session', sessions: 4, total_turns: 4 },
          { period: '2026-06-26 14:00', topic: 'squid', agent_key: 'codex', agent: 'codex', session_type: 'adhoc', sessions: 0, total_turns: 3 },
          { period: '2026-06-26 14:00', topic: 'squid', agent_key: 'clive', agent: 'clive', session_type: 'session', sessions: 2, total_turns: 2 },
          { period: '2026-06-26 14:00', topic: 'squid', agent_key: 'clive', agent: 'clive', session_type: 'adhoc', sessions: 0, total_turns: 1 },
          { period: '2026-06-26 14:00', topic: 'ops', agent_key: 'codex', agent: 'codex', session_type: 'session', sessions: 1, total_turns: 1 },
        ],
      });
    }
    return route.fulfill({ json: [{ period: '2026-06-26 14:00', sessions: 8, total_turns: 11 }] });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Stats' }).click();
  await page.locator('#sf-topic-toggle').click();
  await page.locator('#sf-topic-menu input[value="squid"]').check();
  await page.locator('#sf-agent-toggle').click();
  await page.locator('#sf-agent-menu input[value="codex"]').check();
  await page.locator('#sf-agent-menu input[value="clive"]').check();
  await page.locator('#sf-breakdown').selectOption('topic_agent');

  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('breakdown')).toBe('topic_agent');
  expect(statsRequests.at(-1).searchParams.get('topic')).toBe('squid');
  expect(statsRequests.at(-1).searchParams.get('agent')).toBe('codex,clive');
  await expect(page.locator('#stats-content thead th')).toHaveText([
    'Hour', '#squid@clive+', '#squid@codex+', 'Misc', 'Total',
  ]);

  await page.locator('#sf-breakdown').selectOption('topic_agent_session');

  await expect.poll(() => statsRequests.at(-1)?.searchParams.get('breakdown')).toBe('topic_agent_session');
  expect(statsRequests.at(-1).searchParams.get('topic')).toBe('squid');
  expect(statsRequests.at(-1).searchParams.get('agent')).toBe('codex,clive');
  await expect(page.locator('#stats-content thead th')).toHaveText([
    'Hour', '#squid@clive', '#squid@clive!', '#squid@codex', '#squid@codex!', 'Misc', 'Total',
  ]);
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
