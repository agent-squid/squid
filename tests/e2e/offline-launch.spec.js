const { test, expect } = require('@playwright/test');
const { readFile } = require('node:fs/promises');
const { createServer } = require('node:http');
const { extname, resolve } = require('node:path');

const uiDir = resolve(__dirname, '../../ui');
const contentTypes = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.jpg': 'image/jpeg',
  '.png': 'image/png',
  '.webmanifest': 'application/manifest+json',
};

test('offline cold launch runs Squid and reconnects without losing the draft', async ({ page }) => {
  let hostUnavailable = false;
  let mismatchedShell = false;
  let recoveryChecks = 0;

  // Fail requests at the origin so the page and the service worker see the
  // same outage.
  const server = createServer(async (request, response) => {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    // Keep worker script revalidation separate from the page and API outage.
    if (hostUnavailable && pathname !== '/sw.js') {
      response.writeHead(503, { 'Cache-Control': 'no-store' }).end();
      return;
    }
    if (pathname === '/' && mismatchedShell) {
      response.writeHead(200, { 'Content-Type': 'text/html' });
      response.end('<html><body>mismatched shell</body></html>');
      return;
    }

    const filename = pathname === '/' ? '/index.html' : pathname;
    const contentType = contentTypes[extname(filename)];
    if (contentType) {
      try {
        const data = await readFile(resolve(uiDir, '.' + filename));
        if (contentType === 'text/html') {
          // Optional CDN assets should not affect this local outage test.
          response.setHeader('Content-Security-Policy', "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'");
        }
        response.writeHead(200, { 'Content-Type': contentType });
        response.end(data);
      } catch {
        response.writeHead(404).end();
      }
      return;
    }

    const payload = pathname === '/health' ? { status: 'ok', backends: {} }
      : pathname === '/history' ? { items: [], has_more: false }
      : [];
    response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    response.end(JSON.stringify(payload));
  });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const origin = 'http://127.0.0.1:' + server.address().port;
  try {
    await page.routeWebSocket(origin.replace('http:', 'ws:') + '/ws/v1', socket => {
      socket.onMessage(raw => {
        const frame = JSON.parse(raw);
        if (frame.type === 'subscribe') {
          socket.send(JSON.stringify({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'health.check') {
          if (!hostUnavailable) recoveryChecks++;
          socket.send(JSON.stringify({
            v: 1, type: 'command.result', request_id: frame.request_id,
            payload: hostUnavailable ? { ok: false, error: 'unreachable' } : { ok: true, status: 'ok' },
          }));
        }
      });
      socket.send(JSON.stringify({ v: 1, type: 'hello', payload: { heartbeat_seconds: 30 } }));
    });

    const status = page.locator('#connection-status');
    const banner = page.locator('#connection-banner');
    // Wait for the initial worker claim, then control the test's reloads.
    await page.addInitScript(() => {
      if (!navigator.serviceWorker.controller) sessionStorage.setItem('squid_skip_sw_reload', '1');
    });
    await page.goto(origin, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => !!navigator.serviceWorker.controller);
    await expect(status).toContainText('Squid reachable');

    mismatchedShell = true;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.locator('#app')).toBeVisible();
    await expect(status).toContainText('Squid reachable');
    mismatchedShell = false;

    hostUnavailable = true;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => !!navigator.serviceWorker.controller);
    await expect(banner).toHaveAttribute('data-state', 'lost');
    await expect(banner).toBeVisible();
    await expect(page.locator('#connection-retry')).toBeVisible();
    const input = page.locator('#input');
    await input.fill('offline draft');
    expect(await page.evaluate(async () => !!await caches.match('/health'))).toBe(false);

    const checksBeforeReconnect = recoveryChecks;
    hostUnavailable = false;
    await page.locator('#connection-retry').click();
    await expect.poll(() => recoveryChecks).toBeGreaterThan(checksBeforeReconnect);
    await expect(status).toContainText('Squid reachable');
    await expect(banner).toBeHidden();
    await expect(input).toHaveValue('offline draft');
  } finally {
    const closed = new Promise(done => server.close(done));
    server.closeAllConnections();
    await closed;
  }
});
