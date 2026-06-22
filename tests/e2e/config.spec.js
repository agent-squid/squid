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

test('configuration editor loads and saves the complete YAML', async ({ page }) => {
  await mockApp(page);
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
  await page.getByRole('button', { name: 'Agents', exact: true }).click();
  const editor = page.locator('#config-editor');
  await expect(editor).toHaveValue(/backends:/);
  await expect(editor).toHaveValue(/qwen:/);
  await editor.fill(original.replace('port: 8000', 'port: 8123'));
  await page.locator('#config-editor-save').click();

  await expect(page.locator('#config-editor-status')).toHaveText('saved ✓ · restart required');
  expect(saved.revision).toBe('rev-1');
  expect(saved.content).toContain('port: 8123');
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
