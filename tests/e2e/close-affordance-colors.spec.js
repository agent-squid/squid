const { test, expect } = require('@playwright/test');

const CLOSE = 'rgb(208, 120, 64)';
const REMOVE = 'rgb(200, 121, 65)';

async function mockBackend(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    '#squid@codex review orange close markers',
  ] } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic: 'default', exists: false, content: '', path: '',
    squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
  }}));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/queue', r => r.fulfill({ json: [
    { topic: 'squid', position: 1, agent: 'codex', prompt_preview: 'queued work' },
  ] }));
  await page.route('**/cmd', r => r.fulfill({ json: { ok: true } }));
}

test('dismiss and remove controls use visible orange affordance colors on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('pinnedItems', JSON.stringify([
    { id: 42, topic: 'squid', agent: 'codex', content: 'Pinned response context' },
  ])));

  await page.goto('/');

  await page.fill('#input', 'review');
  await expect(page.locator('#autocomplete.open')).toBeVisible();
  await expect(page.locator('#autocomplete .ac-close')).toHaveCSS('color', CLOSE);
  await expect(page.locator('#autocomplete .ac-del-btn')).toHaveCSS('color', REMOVE);

  await page.locator('#help-btn').click();
  await expect(page.locator('#help-panel.open')).toBeVisible();
  await expect(page.locator('#help-close')).toHaveCSS('color', CLOSE);

  await page.locator('#pin-btn').click();
  await expect(page.locator('#pin-panel.open')).toBeVisible();
  await expect(page.locator('#pin-panel-close')).toHaveCSS('color', CLOSE);
  await expect(page.locator('.pin-item-remove')).toHaveCSS('color', REMOVE);

  await page.locator('#proc-status').click();
  await expect(page.locator('#proc-status-popup.open')).toBeVisible();
  await expect(page.locator('#proc-popup-close')).toHaveCSS('color', CLOSE);
  await expect(page.locator('.proc-deq-btn')).toHaveCSS('color', REMOVE);
});

test('thinking bubble kill control uses the warning orange while running', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.evaluate(() => {
    const orig = window.fetch;
    window.fetch = async (url, opts) => {
      if (!url.includes('/chat')) return orig(url, opts);
      const { readable, writable } = new TransformStream();
      const writer = writable.getWriter();
      const enc = new TextEncoder();
      writer.write(enc.encode(
        'event: meta\ndata: {"agent":"codex","backend":"codex","msg_id":42,"adhoc":false}\n\n'
      ));
      window._testSseWriter = writer;
      return new Response(readable, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      });
    };
  });

  await page.fill('#input', '#squid@codex keep running');
  await page.keyboard.press('Enter');

  const killBtn = page.locator('.thinking-kill-btn');
  await expect(killBtn).toBeVisible({ timeout: 5000 });
  await expect(killBtn).toHaveCSS('color', REMOVE);

  await page.evaluate(() => window._testSseWriter?.close().catch(() => {}));
});
