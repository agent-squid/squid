const { test, expect } = require('@playwright/test');

// Backend-mocked tests must not reload under a newly installed service worker.
test.use({ serviceWorkers: 'block' });
const item = id => ({ id, topic: 'squid', agent: 'claude', status: 'done', content: `reply ${id}`, prompt: `prompt ${id}`, timestamp: '2026-11-15T12:00:00Z' });
async function setup(page) {
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (/\.(js|css|png|jpg|woff2?)$/.test(url.pathname) || url.pathname === '/') return route.continue();
    return route.fulfill({ json: [] });
  });
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
}
test('failed history sync preserves messages and recovery displays latest without duplicates', async ({ page }) => {
  await setup(page);
  let latest = 1, fail = false;
  await page.route('**/history?**', r => fail ? r.fulfill({ status: 503, json: {} }) : r.fulfill({ json: { items: [item(latest)], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  fail = true;
  await page.evaluate(() => { void syncConnectionHistory(); });
  await expect(page.locator('#connection-status')).toContainText('Connection lost');
  await page.locator('#connection-help').click();
  await page.locator('#connection-method').selectOption('ssh');
  await expect(page.locator('#connection-advice')).toContainText('SSH port-forwarding tunnel');
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(1);
  latest = 2; fail = false;
  await page.evaluate(() => { void syncConnectionHistory(); });
  await expect(page.locator('#connection-status')).toContainText('Up to date');
  await expect(page.locator('.assistant[data-msg-id="2"]')).toHaveCount(1);
  await page.evaluate(() => { void syncConnectionHistory(); });
  await expect(page.locator('.assistant[data-msg-id="2"]')).toHaveCount(1);
});
test('older history stays until reader chooses latest', async ({ page }) => {
  await setup(page);
  let latest = 1;
  await page.route('**/history?**', r => r.fulfill({ json: { items: [item(latest)], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  latest = 2;
  await page.evaluate(() => { historyWindowMode = true; });
  await page.evaluate(() => { void syncConnectionHistory(); });
  await expect(page.locator('#connection-banner')).toBeHidden();
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(1);
  await expect(page.locator('.assistant[data-msg-id="2"]')).toHaveCount(0);
  await page.evaluate(() => syncConnectionHistory(true));
  await expect(page.locator('.assistant[data-msg-id="2"]')).toHaveCount(1);
  await expect(page.locator('#connection-status')).toContainText('Up to date');
});

test('unreachable host retains draft and reconnects on browser online event', async ({ page }) => {
  await setup(page);
  await page.route('**/history?**', r => r.fulfill({ json: { items: [item(1)], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.locator('#input').fill('keep my draft');
  await page.route('**/health', r => r.abort('connectionrefused'));
  await page.evaluate(() => { void syncConnectionHistory(); });
  await expect(page.locator('#connection-status')).toContainText('Connection lost');
  await expect(page.locator('#input')).toHaveValue('keep my draft');
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(1);
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok' } }));
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await expect(page.locator('#input')).toHaveValue('keep my draft');
});

test('refresh preserves the identity and content of recovered running bubbles', async ({ page }) => {
  await setup(page);
  let latest = 1;
  await page.route('**/history?**', r => r.fulfill({ json: { items: [item(latest)], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.evaluate(() => {
    const pending = makeWipBubble({ id: 99, topic: 'squid', agent: 'claude', prompt: 'still running', status: 'pending' });
    pending.querySelector('.thinking-live').textContent = 'streamed text so far';
    messages.appendChild(pending);
    window.savedPending = pending;
    messages.scrollTop = messages.scrollHeight;
  });
  latest = 2;
  await page.evaluate(() => { void syncConnectionHistory(); });
  await expect(page.locator('.assistant[data-msg-id="2"]')).toHaveCount(1);
  expect(await page.evaluate(() => window.savedPending.isConnected && messages.querySelector('[data-msg-id="99"]') === window.savedPending)).toBe(true);
  await expect(page.locator('[data-msg-id="99"] .thinking-live')).toHaveText('streamed text so far');
});

test('failed latest refresh leaves pagination observers and generation intact', async ({ page }) => {
  await setup(page);
  let fail = false;
  await page.route('**/history?**', r => fail ? r.fulfill({ status: 503, json: {} }) : r.fulfill({ json: { items: [item(1)], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  fail = true;
  const result = await page.evaluate(async () => {
    let disconnected = false;
    const observer = new IntersectionObserver(() => {});
    const originalDisconnect = observer.disconnect.bind(observer);
    observer.disconnect = () => { disconnected = true; originalDisconnect(); };
    historyObserver = observer;
    observer.observe(messages);
    const before = historyGeneration;
    const success = await resetHistoryToLatest();
    return { success, disconnected, sameObserver: historyObserver === observer, sameGeneration: historyGeneration === before };
  });
  expect(result).toEqual({ success: false, disconnected: false, sameObserver: true, sameGeneration: true });
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(1);
});

test('scrolling up during a slow refresh defers replacement until explicit jump', async ({ page }) => {
  await setup(page);
  let delay = false;
  let release;
  let requested;
  const requestStarted = new Promise(resolve => { requested = resolve; });
  await page.route('**/history?**', async r => {
    if (delay) {
      requested();
      await new Promise(resolve => { release = resolve; });
      return r.fulfill({ json: { items: [item(2)], has_more: false } });
    }
    return r.fulfill({ json: { items: [{ ...item(1), content: 'old history\n\n'.repeat(300) }], has_more: false } });
  });
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.evaluate(() => { messages.scrollTop = messages.scrollHeight; });
  delay = true;
  await page.evaluate(() => { void syncConnectionHistory(); });
  await requestStarted;
  await page.evaluate(() => { messages.scrollTop = 0; });
  release();
  await expect(page.locator('#connection-banner')).toBeHidden();
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(1);
  await expect(page.locator('.assistant[data-msg-id="2"]')).toHaveCount(0);
  expect(await page.locator('#messages').evaluate(el => el.scrollTop)).toBe(0);
  delay = false;
});

test('active search survives refresh including explicit latest requests', async ({ page }) => {
  await setup(page);
  await page.route('**/history?**', r => r.fulfill({ json: { items: [item(1)], has_more: false } }));
  await page.route('**/search?**', r => r.fulfill({ json: { items: [{ ...item(77), content: 'matching result' }] } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.evaluate(() => startSearch('#all matching'));
  await expect(page.locator('.assistant.search-result-item')).toHaveCount(1);
  await page.evaluate(async () => { messages.scrollTop = messages.scrollHeight; await syncConnectionHistory(); await syncConnectionHistory(true); });
  await expect(page.locator('.assistant.search-result-item')).toHaveCount(1);
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(0);
  await expect(page.locator('#connection-status')).toContainText('clear search');
  expect(await page.evaluate(() => searchActive)).toBe(true);
});

test('search started during an in-flight refresh stays visible', async ({ page }) => {
  await setup(page);
  let delay = false, release, requested;
  const started = new Promise(resolve => { requested = resolve; });
  await page.route('**/history?**', async r => {
    if (delay) { requested(); await new Promise(resolve => { release = resolve; }); }
    await r.fulfill({ json: { items: [item(1)], has_more: false } });
  });
  await page.route('**/search?**', r => r.fulfill({ json: { items: [{ ...item(77), content: 'matching result' }] } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  delay = true;
  await page.evaluate(() => { void syncConnectionHistory(); });
  await started;
  await page.evaluate(() => startSearch('#all matching'));
  await expect(page.locator('.assistant.search-result-item')).toHaveCount(1);
  release();
  await expect(page.locator('#connection-status')).toContainText('clear search');
  await expect(page.locator('.assistant.search-result-item')).toHaveCount(1);
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(0);
});

test('same-ID content changes are reconciled and unchanged snapshots retain their DOM', async ({ page }) => {
  await setup(page);
  let content = 'original response';
  await page.route('**/history?**', r => r.fulfill({ json: { items: [{ ...item(1), content }], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  content = 'updated response';
  await page.evaluate(() => { void syncConnectionHistory(); });
  await expect(page.locator('.assistant[data-msg-id="1"]')).toContainText('updated response');
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(1);
  await expect(page.locator('#connection-status')).toContainText('Up to date');
  await page.evaluate(() => { window.savedCompleted = messages.querySelector('.assistant[data-msg-id="1"]'); });
  await page.evaluate(() => syncConnectionHistory());
  expect(await page.evaluate(() => messages.querySelector('.assistant[data-msg-id="1"]') === window.savedCompleted)).toBe(true);
});

test('empty server history removes deleted messages before reporting up to date', async ({ page }) => {
  await setup(page);
  let rows = [item(1)];
  await page.route('**/history?**', r => r.fulfill({ json: { items: rows, has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(1);
  rows = [];
  await page.evaluate(() => syncConnectionHistory());
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(0);
  await expect(page.locator('#connection-status')).toContainText('Up to date');
});

test('removing a row from the current page reconciles the remaining history', async ({ page }) => {
  await setup(page);
  let rows = [item(2), item(1)];
  await page.route('**/history?**', r => r.fulfill({ json: { items: rows, has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  rows = [item(1)];
  await page.evaluate(() => syncConnectionHistory());
  await expect(page.locator('.assistant[data-msg-id="2"]')).toHaveCount(0);
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(1);
});

test('page membership changes reconcile deletions even when replacement rows are already rendered', async ({ page }) => {
  await setup(page);
  let rows = [item(6), item(5), item(4), item(3), item(2)];
  await page.route('**/history?**', r => r.fulfill({ json: { items: rows, has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.evaluate(() => {
    // An older page previously loaded by scrolling is already in the DOM.
    insertCompletedHistoryItem({ id: 1, topic: 'squid', agent: 'claude', status: 'done', content: 'reply 1', prompt: 'prompt 1', timestamp: '2026-11-15T12:00:00Z' }, { reconcile: true });
    messages.scrollTop = messages.scrollHeight;
  });
  rows = [item(5), item(4), item(3), item(2), item(1)];
  await page.evaluate(() => syncConnectionHistory());
  await expect(page.locator('.assistant[data-msg-id="6"]')).toHaveCount(0);
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(1);
  await expect(page.locator('.assistant.history-item')).toHaveCount(5);
});

test('refresh reconciles deleted rows from loaded older pages as well as the latest five', async ({ page }) => {
  await setup(page);
  let rows = Array.from({ length: 10 }, (_, index) => ({ ...item(10 - index), content: `reply ${10 - index}\n\n`.repeat(40) }));
  let refreshLimit;
  await page.route('**/history?**', r => {
    const params = new URL(r.request().url()).searchParams;
    const offset = Number(params.get('offset'));
    const limit = Number(params.get('limit'));
    if (offset === 0) refreshLimit = limit;
    return r.fulfill({ json: { items: rows.slice(offset, offset + limit), has_more: offset + limit < rows.length } });
  });
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.evaluate(async () => { await loadHistory(); messages.scrollTop = messages.scrollHeight; });
  await expect(page.locator('.assistant[data-msg-id="2"]')).toHaveCount(1);
  rows = rows.filter(row => row.id !== 2);
  await page.evaluate(() => syncConnectionHistory());
  expect(refreshLimit).toBe(10);
  await expect(page.locator('.assistant[data-msg-id="2"]')).toHaveCount(0);
  await expect(page.locator('.assistant.history-item')).toHaveCount(9);
  await expect(page.locator('#connection-status')).toContainText('Up to date');
});

for (const width of [320, 390, 768, 1280]) {
  test(`connection UI is quiet when healthy and recovery actions fit below the header at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await setup(page);
    await page.route('**/history?**', r => r.fulfill({ json: { items: [item(1)], has_more: false } }));
    await page.goto('/');
    await expect(page.locator('#connection-status')).toContainText('Squid reachable');
    await expect(page.locator('#connection-banner')).toBeHidden();
    await expect(page.locator('#connection-snackbar')).toBeHidden();
    await page.route('**/health', r => r.abort('connectionrefused'));
    await page.evaluate(() => syncConnectionHistory());
    await expect(page.locator('#connection-banner')).toBeVisible();
    await expect(page.locator('#connection-retry')).toBeVisible();
    const geometry = await page.evaluate(() => {
      const rect = id => document.getElementById(id).getBoundingClientRect();
      const banner = rect('connection-banner'), status = rect('connection-status'), retry = rect('connection-retry'), help = rect('connection-help'), header = rect('topbar');
      return { belowHeader: banner.top >= header.bottom - 1, separateActions: retry.top >= status.bottom, fits: help.right <= innerWidth && retry.left >= 0 };
    });
    expect(geometry).toEqual({ belowHeader: true, separateActions: true, fits: true });
    await page.locator('#connection-help').click();
    await expect(page.locator('#connection-guide')).toBeVisible();
    await page.route('**/health', r => r.fulfill({ json: { status: 'ok' } }));
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await expect(page.locator('#connection-banner')).toBeHidden();
    await expect(page.locator('#connection-guide')).toBeHidden();
    await expect(page.locator('#connection-snackbar')).toHaveText('Back online');
    await expect(page.locator('#connection-snackbar')).toBeVisible();
    await expect(page.locator('#connection-snackbar')).toBeHidden({ timeout: 6000 });
  });
}

test('reading unchanged older history does not show a routine connection banner', async ({ page }) => {
  await setup(page);
  await page.route('**/history?**', r => r.fulfill({ json: { items: [item(1)], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.evaluate(async () => { historyWindowMode = true; await syncConnectionHistory(); });
  await expect(page.locator('#connection-banner')).toBeHidden();
  await expect(page.locator('#connection-snackbar')).toBeHidden();
});

for (const width of [320, 390, 1280]) {
  test(`connection method changes preserve chat geometry and scroll at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await setup(page);
    await page.route('**/history?**', r => r.fulfill({ json: { items: [{ ...item(1), content: 'Long history paragraph\n\n'.repeat(100) }], has_more: false } }));
    await page.goto('/');
    await expect(page.locator('#connection-status')).toContainText('Squid reachable');
    await page.route('**/health', r => r.abort('connectionrefused'));
    await page.evaluate(() => syncConnectionHistory());
    await page.evaluate(() => { messages.scrollTop = 300; });
    const geometry = () => page.locator('#messages').evaluate(el => ({ top: el.getBoundingClientRect().top, height: el.clientHeight, scroll: el.scrollTop }));
    const before = await geometry();
    await page.locator('#connection-help').click();
    await expect(page.getByRole('dialog', { name: 'Connection help' })).toBeVisible();
    await expect(page.locator('#connection-method')).toBeFocused();
    expect(await geometry()).toEqual(before);
    for (const method of ['tailscale', 'ssh', 'local', 'unknown']) {
      await page.locator('#connection-method').selectOption(method);
      expect(await geometry()).toEqual(before);
    }
    await page.keyboard.press('Tab');
    await expect(page.locator('#connection-guide-close')).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(page.locator('#connection-method')).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(page.locator('#connection-guide')).toBeHidden();
    await expect(page.locator('#connection-help')).toBeFocused();
    expect(await geometry()).toEqual(before);
    await page.locator('#connection-help').click();
    await page.locator('#connection-guide-close').click();
    await expect(page.locator('#connection-guide')).toBeHidden();
    expect(await geometry()).toEqual(before);
  });
}

test('startup stays silent until a slow initial history load, then clears without recovery toast', async ({ page }) => {
  await setup(page);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/history?**', async route => {
    await gate;
    return route.fulfill({ json: { items: [item(1)], has_more: false } });
  });
  await page.goto('/');
  await expect(page.locator('#connection-banner')).toBeHidden();
  await expect(page.locator('#connection-banner')).toBeVisible({ timeout: 5000 });
  await expect(page.locator('#connection-status')).toHaveText('Connecting to Squid…');
  release();
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await expect(page.locator('#connection-banner')).toBeHidden();
  await expect(page.locator('#connection-snackbar')).toBeHidden();
});


test('unchanged history remains quiet with prompt nodes first and reader above bottom', async ({ page }) => {
  await setup(page);
  await page.route('**/history?**', route => route.fulfill({ json: { items: [{ ...item(1), content: 'history paragraph\n\n'.repeat(200) }], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.evaluate(() => { messages.scrollTop = 0; connectionHistoryPage = null; });
  await page.evaluate(() => syncConnectionHistory());
  await expect(page.locator('#connection-banner')).toBeHidden();
  await expect(page.locator('#connection-status')).toContainText('Up to date');
  expect(await page.locator('#messages').evaluate(el => el.scrollTop)).toBe(0);
  await page.evaluate(() => syncConnectionHistory());
  await expect(page.locator('#connection-banner')).toBeHidden();
});


test('failed boot health does not leave an unreachable message after unchanged-history recovery', async ({ page }) => {
  await setup(page);
  await page.route('**/history?**', route => route.fulfill({ json: { items: [item(1)], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.route('**/health', route => route.abort('connectionrefused'));
  await page.evaluate(async () => { await showBootBanner(); await syncConnectionHistory(); });
  await expect(page.locator('#connection-banner')).toBeVisible();
  await expect(page.locator('#messages .boot-offline')).toHaveCount(0);
  await page.route('**/health', route => route.fulfill({ json: { status: 'ok' } }));
  await page.evaluate(() => syncConnectionHistory());
  await expect(page.locator('#connection-snackbar')).toHaveText('Back online · history updated');
  await expect(page.locator('#connection-banner')).toBeHidden();
  await expect(page.locator('#messages')).not.toContainText('Squid unreachable');
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(1);
});


test('bottom pull refreshes history in place and retains draft and styling', async ({ page }) => {
  await setup(page);
  let latest = 1;
  await page.route('**/history?**', route => route.fulfill({ json: { items: [item(latest)], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.locator('#input').fill('keep my draft');
  latest = 2;
  await page.evaluate(() => {
    window.pullRefreshMarker = true;
    messages.scrollTop = messages.scrollHeight;
    messages.dispatchEvent(new TouchEvent('touchstart', { touches: [new Touch({ identifier: 1, target: messages, clientY: 600 })] }));
    messages.dispatchEvent(new TouchEvent('touchend', { changedTouches: [new Touch({ identifier: 1, target: messages, clientY: 50 })] }));
  });
  await expect(page.locator('.assistant[data-msg-id="2"]')).toHaveCount(1);
  expect(await page.evaluate(() => window.pullRefreshMarker)).toBe(true);
  await expect(page.locator('#input')).toHaveValue('keep my draft');
});


test('background checks preserve the offline banner until confirmed recovery', async ({ page }) => {
  await setup(page);
  await page.route('**/history?**', route => route.fulfill({ json: { items: [item(1)], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.route('**/health', route => route.abort('connectionrefused'));
  await page.evaluate(() => syncConnectionHistory());
  const before = await page.locator('#connection-banner').evaluate(el => el.outerHTML);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/health', async route => { await gate; return route.abort('connectionrefused'); });
  await page.evaluate(() => { void syncConnectionHistory(); });
  expect(await page.locator('#connection-banner').evaluate(el => el.outerHTML)).toBe(before);
  release();
  await expect.poll(() => page.evaluate(() => connectionSyncBusy)).toBe(false);
  expect(await page.locator('#connection-banner').evaluate(el => el.outerHTML)).toBe(before);
});

test('typing while above bottom keeps healthy history updates silent', async ({ page }) => {
  await setup(page);
  let latest = 1;
  await page.route('**/history?**', route => route.fulfill({ json: { items: [{ ...item(latest), content: 'long history\n\n'.repeat(100) }], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.locator('#input').fill('draft while connected');
  await page.evaluate(() => { messages.scrollTop = 0; });
  latest = 2;
  await page.evaluate(() => syncConnectionHistory());
  await expect(page.locator('#connection-banner')).toBeHidden();
  await expect(page.locator('#connection-snackbar')).toBeHidden();
  await expect(page.locator('#input')).toHaveValue('draft while connected');
});


test('recovery during search retains the warning until history is synced', async ({ page }) => {
  await setup(page);
  await page.route('**/history?**', r => r.fulfill({ json: { items: [item(1)], has_more: false } }));
  await page.route('**/search?**', r => r.fulfill({ json: { items: [{ ...item(77), content: 'matching result' }] } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await page.evaluate(() => startSearch('#all matching'));
  await expect(page.locator('.assistant.search-result-item')).toHaveCount(1);
  await page.route('**/health', r => r.abort('connectionrefused'));
  await page.evaluate(() => syncConnectionHistory());
  await expect(page.locator('#connection-banner')).toBeVisible();
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok' } }));
  await page.evaluate(() => syncConnectionHistory());
  await expect(page.locator('#connection-banner')).toBeVisible();
  await expect(page.locator('#connection-status')).toContainText('clear search to sync history');
  await expect(page.locator('#connection-snackbar')).toBeHidden();
  await expect(page.locator('.assistant.search-result-item')).toHaveCount(1);
  await page.evaluate(async () => { clearSearch(); await loadHistory(); messages.scrollTop = messages.scrollHeight; });
  await expect.poll(() => page.evaluate(() => historyLoading)).toBe(false);
  await page.evaluate(() => syncConnectionHistory(true));
  await expect(page.locator('#connection-banner')).toBeHidden();
  await expect(page.locator('#connection-status')).toContainText('Up to date');
});

test('startup timer cannot overwrite a view load error or remove Retry', async ({ page }) => {
  await page.clock.install();
  await setup(page);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/history?**', async r => { await gate; return r.fulfill({ json: { items: [], has_more: false } }); });
  await page.route('**/config/agents', r => r.abort('connectionrefused'));
  await page.goto('/');
  await page.evaluate(() => switchView('agents'));
  await expect(page.locator('#connection-status')).toContainText('Unable to load agents.');
  await page.evaluate(() => syncConnectionHistory());
  await page.clock.runFor(4000);
  await expect(page.locator('#connection-status')).toContainText('Unable to load agents.');
  await expect(page.locator('#connection-retry')).toBeVisible();
  await expect(page.locator('#connection-banner')).toBeVisible();
  release();
});


test('failed autocomplete startup retries after health recovery without replacing a draft', async ({ page }) => {
  await setup(page);
  let fail = true;
  let requests = 0;
  await page.route('**/prompts/recent?**', route => {
    requests++;
    return fail ? route.fulfill({ status: 503, json: {} }) : route.fulfill({ json: { items: ['server autocomplete history'], agents: {} } });
  });
  await page.route('**/history?**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toContainText('Squid reachable');
  await expect.poll(() => page.evaluate(() => promptHistoryLoading)).toBe(false);
  expect(await page.evaluate(() => promptHistoryLoaded)).toBe(false);
  await page.locator('#input').fill('server auto');
  await page.evaluate(() => { recordPrompt('local prompt while disconnected'); localStorage.setItem('squid_draft', 'outdated saved draft'); });
  fail = false;
  await page.evaluate(() => syncConnectionHistory());
  await expect.poll(() => page.evaluate(() => promptHistoryLoaded)).toBe(true);
  expect(await page.evaluate(() => promptHistory)).toEqual(expect.arrayContaining(['local prompt while disconnected', 'server autocomplete history']));
  await expect(page.locator('#input')).toHaveValue('server auto');
  await expect(page.locator('#autocomplete')).toContainText('server autocomplete history');
  const loadedRequests = requests;
  await page.evaluate(() => syncConnectionHistory());
  expect(requests).toBe(loadedRequests);
});

// Exercise actual correlated WS replies, including SSE turn-streaming mode.
test('SSE lifecycle recovery updates and deletes exact rows without rebuilding unchanged history', async ({ page }) => {
  await setup(page);
  await page.route('**/config/realtime', r => r.fulfill({ json: { transport: 'sse' } }));
  let rows = [3, 2, 1].map(id => ({ ...item(id), content: `Response ${id}\n\n`.repeat(100) }));
  await page.route('**/history?**', r => r.fulfill({ json: { items: rows, has_more: false } }));
  let socket;
  let checks = 0;
  let oversized = false;
  let holdCheck = false;
  let releaseCheck;
  await page.routeWebSocket('**/ws/v1', ws => {
    socket = ws;
    ws.onMessage(raw => {
      const frame = JSON.parse(raw);
      if (frame.type === 'subscribe') {
        ws.send(JSON.stringify({ v: 1, type: 'subscribed', payload: {} }));
        ws.send(JSON.stringify({ v: 1, type: 'snapshot', event_id: 1, payload: { cursor_reset: true, conversations: [] } }));
      } else if (frame.type === 'history.check') {
        checks++;
        const reply = JSON.stringify({ v: 1, type: 'command.result', request_id: frame.request_id,
          payload: oversized ? { ok: false, error: 'history_result_too_large' }
            : { ok: true, items: rows.filter(row => frame.payload.ids.includes(row.id)) } });
        if (holdCheck) releaseCheck = () => ws.send(reply);
        else ws.send(reply);
      }
    });
    ws.send(JSON.stringify({ v: 1, type: 'hello', payload: {} }));
  });
  await page.goto('/');
  await expect.poll(() => checks).toBeGreaterThan(0);
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  await expect.poll(() => page.evaluate(() => renderedHistoryCheckBusy)).toBe(false);
  const before = await page.evaluate(() => {
    window.savedReadingRow = messages.querySelector('.assistant[data-msg-id="2"]');
    messages.scrollTop += savedReadingRow.getBoundingClientRect().top - messages.getBoundingClientRect().top + 60;
    return savedReadingRow.getBoundingClientRect().top;
  });
  let historyRequests = 0;
  await page.route('**/history?**', r => { historyRequests++; return r.abort(); });
  rows = rows.filter(row => row.id !== 1).map(row => row.id === 3 ? { ...row, content: 'Changed existing response' } : row);
  socket.send(JSON.stringify({ v: 1, type: 'message.changed', event_id: 2, msg_id: 3,
    scope: { topic: 'squid', agent: 'claude' }, payload: { role: 'assistant', status: 'done' } }));
  await expect(page.locator('.assistant[data-msg-id="3"]')).toContainText('Changed existing response');
  await expect(page.locator('.assistant[data-msg-id="1"]')).toHaveCount(0);
  const after = await page.evaluate(() => ({ same: savedReadingRow === messages.querySelector('.assistant[data-msg-id="2"]'), top: savedReadingRow.getBoundingClientRect().top }));
  expect(after.same).toBe(true);
  expect(Math.abs(after.top - before)).toBeLessThanOrEqual(1);
  expect(historyRequests).toBe(0);
  const discovered = { ...item(4), content: 'Turn completed on another client' };
  rows.push(discovered);
  await page.route('**/chat/4/status', r => r.fulfill({ json: discovered }));
  socket.send(JSON.stringify({ v: 1, type: 'message.changed', event_id: 3, msg_id: 4,
    scope: { topic: 'squid', agent: 'claude' }, payload: { role: 'assistant', status: 'done' } }));
  await expect(page.locator('.assistant[data-msg-id="4"]')).toContainText(discovered.content);
  expect(historyRequests).toBe(0);
  await expect.poll(() => page.evaluate(() => renderedHistoryCheckBusy)).toBe(false);
  holdCheck = true;
  rows = rows.map(row => row.id === 3 ? { ...row, content: 'Delayed changed response\n\n'.repeat(100) } : row);
  await page.evaluate(() => { scrollToBottom(true); void reconcileRenderedHistory(); });
  await expect.poll(() => typeof releaseCheck).toBe('function');
  const readingTop = await page.evaluate(() => { messages.scrollTop = 250; return messages.scrollTop; });
  holdCheck = false;
  releaseCheck();
  await expect(page.locator('.assistant[data-msg-id="3"]')).toContainText('Delayed changed response');
  await expect.poll(() => page.evaluate(() => renderedHistoryCheckBusy)).toBe(false);
  expect(await page.evaluate(() => messages.scrollTop)).toBe(readingTop);
  oversized = true;
  await page.evaluate(() => reconcileRenderedHistory());
  await expect(page.locator('#connection-latest')).toBeVisible();
  await expect(page.locator('#connection-status')).toContainText('History recovery unavailable');
  oversized = false;
  await page.evaluate(() => reconcileRenderedHistory());
  await expect(page.locator('#connection-banner')).toBeHidden();

});

for (const mode of ['bookmarked', 'marked_bad']) {
  test(`recovery retains changed ${mode} rows and removes rows that leave the filter`, async ({ page }) => {
    await setup(page);
    let response = { ...item(77), content: 'Original filtered response', marked_bad: true };
    let member = true;
    const requests = [];
    await page.route('**/history?**', r => r.fulfill({ json: { items: [response], has_more: false } }));
    await page.routeWebSocket('**/ws/v1', ws => {
      ws.onMessage(raw => {
        const frame = JSON.parse(raw);
        if (frame.type === 'subscribe') {
          ws.send(JSON.stringify({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'history.check') {
          requests.push(frame.payload);
          ws.send(JSON.stringify({ v: 1, type: 'command.result', request_id: frame.request_id,
            payload: { ok: true, items: member ? [response] : [] } }));
        }
      });
      ws.send(JSON.stringify({ v: 1, type: 'hello', payload: {} }));
    });
    await page.goto('/');
    await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
    await expect.poll(() => page.evaluate(() => realtimeV1.isActive() && !renderedHistoryCheckBusy)).toBe(true);
    await page.evaluate(mode => {
      bookmarkOnlyHistory = mode === 'bookmarked';
      badOnlyHistory = mode === 'marked_bad';
    }, mode);
    response = { ...response, content: 'Updated filtered response' };
    await page.evaluate(() => reconcileRenderedHistory());
    await expect(page.locator('.assistant[data-msg-id="77"]')).toContainText('Updated filtered response');
    expect(requests.at(-1)).toMatchObject({ bookmarked: mode === 'bookmarked', marked_bad: mode === 'marked_bad' });
    await page.evaluate(() => { window.savedFilteredRow = messages.querySelector('.assistant[data-msg-id="77"]'); });
    await page.evaluate(() => reconcileRenderedHistory());
    expect(await page.evaluate(() => savedFilteredRow === messages.querySelector('.assistant[data-msg-id="77"]'))).toBe(true);
    member = false;
    await page.evaluate(() => reconcileRenderedHistory());
    await expect(page.locator('.assistant[data-msg-id="77"]')).toHaveCount(0);
  });
}

test('prompt-only recovery updates and deletes rows while preserving presentation and reading position', async ({ page }) => {
  await setup(page);
  let rows = [3, 2, 1].map(id => ({ ...item(id), prompt: `Prompt ${id}\n\n`.repeat(100) }));
  let checks = 0;
  let socket;
  await page.route('**/history?**', r => r.fulfill({ json: { items: rows, has_more: false } }));
  await page.routeWebSocket('**/ws/v1', ws => {
    socket = ws;
    ws.onMessage(raw => {
      const frame = JSON.parse(raw);
      if (frame.type === 'subscribe') ws.send(JSON.stringify({ v: 1, type: 'subscribed', payload: {} }));
      if (frame.type === 'history.check') {
        checks++;
        ws.send(JSON.stringify({ v: 1, type: 'command.result', request_id: frame.request_id,
          payload: { ok: true, items: rows.filter(row => frame.payload.ids.includes(row.id)) } }));
      }
    });
    ws.send(JSON.stringify({ v: 1, type: 'hello', payload: {} }));
  });
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  await expect.poll(() => page.evaluate(() => realtimeV1.isActive() && !renderedHistoryCheckBusy)).toBe(true);
  await page.evaluate(() => togglePromptOnlyHistory());
  await expect(page.locator('.prompt-only-history-item')).toHaveCount(3);
  await expect.poll(() => page.evaluate(() => historyLoading)).toBe(false);
  const before = await page.evaluate(() => {
    window.savedPromptRow = messages.querySelector('.prompt-only-history-item[data-msg-id="2"]');
    messages.scrollTop += savedPromptRow.getBoundingClientRect().top - messages.getBoundingClientRect().top + 60;
    return savedPromptRow.getBoundingClientRect().top;
  });
  const previousChecks = checks;
  rows = rows.filter(row => row.id !== 3).map(row => row.id === 1
    ? { ...row, status: 'pending', prompt: 'Updated pending prompt\n\n'.repeat(150) } : row);
  await page.evaluate(() => reconcileRenderedHistory());
  expect(checks).toBeGreaterThan(previousChecks);
  await expect(page.locator('.prompt-only-history-item[data-msg-id="1"]')).toContainText('Updated pending prompt');
  await expect(page.locator('.prompt-only-history-item[data-msg-id="3"]')).toHaveCount(0);
  const position = await page.evaluate(() => ({
    same: savedPromptRow === messages.querySelector('.prompt-only-history-item[data-msg-id="2"]'),
    top: savedPromptRow.getBoundingClientRect().top,
  }));
  expect(position.same).toBe(true);
  expect(Math.abs(position.top - before)).toBeLessThanOrEqual(1);
  await expect(page.locator('#messages > .assistant')).toHaveCount(0);
  await expect(page.locator('.prompt-only-history-item[data-msg-id="1"] .prompt-only-open-btn')).toHaveCount(1);
  const discovered = { ...item(4), status: 'pending', prompt: 'Discovered pending prompt', content: 'Hidden response body' };
  rows.push(discovered);
  await page.route('**/chat/4/status', r => r.fulfill({ json: discovered }));
  socket.send(JSON.stringify({ v: 1, type: 'message.changed', event_id: 1, msg_id: 4,
    payload: { role: 'assistant', status: 'pending' }, scope: { topic: 'squid', agent: 'claude' } }));
  await expect(page.locator('.prompt-only-history-item[data-msg-id="4"]')).toContainText(discovered.prompt);
  await expect(page.locator('#messages > .assistant')).toHaveCount(0);
  expect(await page.locator('.prompt-only-history-item').evaluateAll(els => els.map(el => el.dataset.msgId))).toEqual(['1', '2', '4']);
});
