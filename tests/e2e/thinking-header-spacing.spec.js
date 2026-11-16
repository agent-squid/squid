const { test, expect } = require('@playwright/test');
const { installConnectionSocket } = require('./ws-health-mock');

// These tests mock the application backend and must not trigger SW reloads.
test.use({ serviceWorkers: 'block' });

// The route (topic tag) and the user prompt share a flex header line inside
// assistant/thinking bubbles. They're separated by a literal text node, and a
// flex container drops collapsible whitespace-only anonymous items — so two
// plain spaces collapse to *zero* gap and the prompt jams against the route.
// The separator must be non-collapsible (nbsp) so there's a real gap.

const SSE_HEADERS = {
  'Content-Type': 'text/event-stream',
  'Cache-Control': 'no-cache',
  'X-Accel-Buffering': 'no',
};

async function mockBackend(page) {
  await installConnectionSocket(page);
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic: 'default', exists: false, content: '', path: '',
    squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
  }}));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
}

function sse(...events) {
  return events.map(({ event, data }) => {
    const str = typeof data === 'string' ? data : JSON.stringify(data);
    return event ? `event: ${event}\ndata: ${str}\n\n` : `data: ${str}\n\n`;
  }).join('');
}

const META  = { event: 'meta',  data: { agent: 'claude', harness: 'claudecode', provider: 'anthropic', msg_id: 1, adhoc: false } };

test('thinking header keeps a gap between route and prompt', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  // Stream held open mid-flight (status event, no done) so the thinking bubble
  // stays in the DOM with its route + prompt header intact.
  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse(META, { event: 'status', data: 'Working...' }),
  }));

  await page.fill('#input', 'route spacing check');
  await page.keyboard.press('Enter');

  const thinking = page.locator('.msg.assistant.msg-thinking');
  await expect(thinking).toBeVisible();
  await expect(thinking.locator('.topic-tag')).toBeVisible();
  await expect(thinking.locator('.history-prompt')).toBeVisible();

  const gap = await page.evaluate(() => {
    const header = document.querySelector('.msg.assistant.msg-thinking .response-header-text');
    const tag = header && header.querySelector('.topic-tag');
    const prompt = header && header.querySelector('.history-prompt');
    if (!tag || !prompt) return null;
    return prompt.getBoundingClientRect().left - tag.getBoundingClientRect().right;
  });

  expect(gap).not.toBeNull();
  expect(gap).toBeGreaterThan(0.5);
});

for (const atBottom of [false, true]) {
  test(`submission and delayed WIP data ${atBottom ? 'follow from bottom' : 'jump from scrollback'}`, async ({ page }) => {
    await page.setViewportSize({ width: atBottom ? 1280 : 390, height: 844 });
    await mockBackend(page);
    await page.goto('/');
    await page.waitForFunction(() => typeof sendMessage === 'function');
    await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
    let release;
    const responseReady = new Promise(resolve => { release = resolve; });
    let requested;
    const requestStarted = new Promise(resolve => { requested = resolve; });
    await page.route('**/chat', async r => {
      requested();
      await responseReady;
      await r.fulfill({ status: 200, headers: SSE_HEADERS,
        body: sse(META, { event: 'status', data: 'Working...\n'.repeat(30) }, { data: 'First response chunk' }),
      });
    });
    const before = await page.evaluate(atBottom => {
      const pane = document.getElementById('messages');
      const filler = document.createElement('div');
      filler.style.height = '2500px';
      filler.style.flexShrink = '0';
      pane.appendChild(filler);
      pane.scrollTop = atBottom ? pane.scrollHeight : 200;
      const top = pane.scrollTop;
      void sendMessage('scroll position check');
      return top;
    }, atBottom);
    const position = () => page.evaluate(() => {
      const pane = document.getElementById('messages');
      return { top: pane.scrollTop, gap: pane.scrollHeight - pane.clientHeight - pane.scrollTop };
    });
    try {
      await requestStarted;
      const pending = await position();
      expect(pending.gap).toBeLessThanOrEqual(1);
      let readingTop;
      if (!atBottom) {
        expect(pending.top).toBeGreaterThan(before);
        // After submitting, the reader scrolls up while the response is delayed.
        // Offline/reconnect status and background sync must leave them there.
        const reading = await page.evaluate(async () => {
          messages.scrollTop = 200;
          const row = [...messages.children].find(el => el.style.height === '2500px');
          const before = row.getBoundingClientRect().top;
          connectionMessage('lost', 'Connection lost');
          await syncConnectionHistory();
          return { before, after: row.getBoundingClientRect().top, top: messages.scrollTop };
        });
        expect(reading.after).toBe(reading.before);
        readingTop = reading.top;
      }
      release();
      await expect(page.locator('.thinking-live')).toContainText('First response chunk');
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const streaming = await position();
      if (atBottom) expect(streaming.gap).toBeLessThanOrEqual(1);
      else expect(streaming.top).toBe(readingTop);
    } finally { release(); }
  });
}

for (const recovered of [false, true]) {
  test(`${recovered ? 'recovered' : 'live'} completion preserves scrollback through WIP removal`, async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
    const item = { id: 99, topic: 'default', status: 'done', content: 'Final response\n\n'.repeat(80), completed_at: new Date().toISOString() };
    await page.route('**/chat/99/status', r => r.fulfill({ json: item }));
    await page.evaluate(({ recovered, item }) => {
      const pane = document.getElementById('messages');
      const filler = document.createElement('div');
      filler.style.cssText = 'height:2500px;flex-shrink:0';
      pane.appendChild(filler);
      if (recovered) {
        pane.appendChild(makeWipBubble({ ...item, status: 'pending' }));
      } else {
        const originalFetch = window.fetch;
        window.fetch = (url, ...args) => {
          if (url !== '/chat') return originalFetch(url, ...args);
          return Promise.resolve(new Response(new ReadableStream({ start(controller) {
            const encoder = new TextEncoder();
            controller.enqueue(encoder.encode('event: meta\ndata: {"msg_id":99}\n\nevent: status\ndata: Working...\n\n'));
            window.finishScrollTest = () => {
              controller.enqueue(encoder.encode(`data: ${JSON.stringify(item.content)}\n\nevent: done\ndata: {}\n\n`));
              controller.close();
            };
          }}), { headers: { 'Content-Type': 'text/event-stream' } }));
        };
        void sendMessage('completion scroll test');
      }
    }, { recovered, item });
    await expect(page.locator('.msg-thinking')).toBeVisible();
    if (!recovered) await page.waitForFunction(() => !!window.finishScrollTest);
    const before = await page.evaluate(() => {
      const pane = document.getElementById('messages');
      pane.querySelector('.msg-thinking').style.cssText = 'height:200px;flex-shrink:0';
      pane.scrollTop = pane.scrollHeight - pane.clientHeight - 30;
      return pane.scrollTop;
    });
    await page.evaluate(async ({ recovered, item }) => {
      if (recovered) await replacePendingWithStoredItem(item, document.querySelector('.msg-thinking'));
      else window.finishScrollTest();
    }, { recovered, item });
    await expect(page.locator('.msg-thinking')).toHaveCount(0);
    await expect(page.locator('.msg.assistant')).toContainText('Final response');
    expect(await page.locator('#messages').evaluate(el => el.scrollTop)).toBe(before);
  });
}

test('history reload retains the client boot banner', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  const banner = page.locator('.boot-banner').first();
  await expect(banner).toBeVisible();
  await banner.evaluate(el => { el.dataset.retainedBanner = 'yes'; });
  await page.evaluate(() => reloadHistory());
  await expect(page.locator('.boot-banner[data-retained-banner="yes"]')).toHaveCount(1);
});

test('explicit refresh replaces old branding with one fresh timestamped logo', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  await page.evaluate(() => {
    window.__squidBootLogoVariant = 2;
    window.refreshMessageCount = 0;
    currentBootMessage = async () => `Fresh fun message ${++window.refreshMessageCount}`;
  });
  await page.evaluate(async () => {
    const filler = document.createElement('div');
    filler.style.cssText = 'height:2500px;flex-shrink:0';
    messages.appendChild(filler);
    connectionMessage('waiting', 'Squid reachable · jump to latest to sync history');
    messages.scrollTop = messages.scrollHeight;
    // Reproduce layout growth between the history reset and banner insertion.
    const originalRecover = recoverPendingBubbles;
    recoverPendingBubbles = () => {
      originalRecover();
      filler.style.height = '2700px';
    };
    try { await syncConnectionHistory(true, true); }
    finally { recoverPendingBubbles = originalRecover; }
  });
  const marker = page.locator('.history-refresh-marker');
  await expect(marker).toHaveCount(1);
  await expect(marker).toContainText('History refreshed · ');
  await expect(marker.locator('time')).toHaveAttribute('datetime', /T/);
  await expect(marker.locator('.boot-logo-icon')).toHaveCount(1);
  await expect(marker.locator('.boot-logo-bubble')).toHaveText('Fresh fun message 1');
  await expect(page.locator('.boot-banner')).toHaveCount(1);
  const position = await page.locator('#messages').evaluate(el => ({
    gap: el.scrollHeight - el.scrollTop - el.clientHeight,
    logoBottom: el.querySelector('.boot-banner:last-child').getBoundingClientRect().bottom,
    paneBottom: el.getBoundingClientRect().bottom,
  }));
  expect(position.gap).toBeLessThanOrEqual(1);
  expect(position.logoBottom).toBeLessThanOrEqual(position.paneBottom);
  await marker.evaluate(el => { el.dataset.previousRefresh = 'yes'; });
  await page.evaluate(() => syncConnectionHistory(true, true));
  await expect(marker).toHaveCount(1);
  await expect(page.locator('[data-previous-refresh]')).toHaveCount(0);
  await expect(page.locator('.boot-banner')).toHaveCount(1);
  await expect(marker.locator('.boot-logo-bubble')).toHaveText('Fresh fun message 2');
  const timestamp = await marker.locator('time').getAttribute('datetime');
  await page.evaluate(() => reloadHistory());
  await expect(marker).toHaveCount(1);
  await expect(marker.locator('time')).toHaveAttribute('datetime', timestamp);
});

test('latest refresh alone retains the randomly selected full banner', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  for (const variant of [0, 1, 2]) {
    await page.locator('.boot-banner').evaluateAll(banners => {
      banners.forEach(el => { el.dataset.oldBanner = 'yes'; });
    });
    await page.evaluate(async variant => {
      delete window.__squidBootLogoVariant;
      const random = Math.random;
      Math.random = () => (variant + 0.5) / 3;
      currentBootMessage = async () => `Updated fun message ${variant}`;
      try { await syncConnectionHistory(true, true); }
      finally { Math.random = random; }
    }, variant);
    const banner = page.locator('.boot-banner');
    await expect(banner).toHaveCount(1);
    await expect(page.locator('[data-old-banner]')).toHaveCount(0);
    await expect(page.locator('#messages > .boot-banner:last-child')).toHaveCount(1);
    await expect(banner.locator('time')).toHaveAttribute('datetime', /T/);
    await expect(banner.locator('.boot-art')).toHaveCount(variant === 0 ? 1 : 0);
    await expect(banner.locator('.boot-logo-squid-only')).toHaveCount(variant === 1 ? 1 : 0);
    await expect(banner.locator('.boot-logo-talking-squid')).toHaveCount(variant === 2 ? 1 : 0);
    if (variant === 2) {
      await expect(banner.locator('.boot-logo-bubble')).toHaveText('Updated fun message 2');
    }
  }
});

test('pull-up near the edge requests refresh without enabling incoming-message following', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  const result = await page.evaluate(() => {
    const filler = document.createElement('div');
    filler.style.cssText = 'height:2500px;flex-shrink:0';
    messages.appendChild(filler);
    messages.scrollTop = messages.scrollHeight - messages.clientHeight - 30;
    const followsIncoming = isAtBottom();
    const originalSync = syncConnectionHistory;
    const requests = [];
    syncConnectionHistory = (...args) => { requests.push(args); };
    try {
      const start = new Event('touchstart');
      start.touches = [{ clientY: 600 }];
      messages.dispatchEvent(start);
      const end = new Event('touchend');
      end.changedTouches = [{ clientY: 50 }];
      messages.dispatchEvent(end);
    } finally { syncConnectionHistory = originalSync; }
    return { followsIncoming, requests };
  });
  expect(result).toEqual({ followsIncoming: false, requests: [[true, true]] });
});

test('history rebuild keeps retained banners between the same turns', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  const order = await page.evaluate(async () => {
    messages.querySelectorAll('.boot-banner').forEach(el => el.remove());
    const first = { id: 301, status: 'done', topic: 'default', content: 'First response', completed_at: '2026-11-15T10:00:00Z' };
    const second = { ...first, id: 302, content: 'Second response', completed_at: '2026-11-15T11:00:00Z' };
    await resetHistoryToLatest({ data: { items: [first], has_more: false } });
    for (const label of ['logo A', 'logo B']) {
      const banner = document.createElement('div');
      banner.className = 'boot-banner';
      banner.textContent = label;
      await appendBootBannerAtBottom(banner);
    }
    await resetHistoryToLatest({ data: { items: [second, first], has_more: false }, followLatest: false });
    const labels = () => [...messages.children]
      .filter(el => el.classList.contains('boot-banner') || el.matches('.msg.assistant'))
      .map(el => el.dataset.msgId || el.textContent);
    const firstReset = labels();
    await resetHistoryToLatest({ data: { items: [second, first], has_more: false }, followLatest: false });
    return [firstReset, labels()];
  });
  expect(order).toEqual([
    ['301', 'logo A', 'logo B', '302'],
    ['301', 'logo A', 'logo B', '302'],
  ]);
});

test('startup logo precedes a pending prompt after reload', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/history**', r => r.fulfill({ json: {
    items: [{ id: 501, status: 'pending', topic: 'default', prompt: 'Reloaded pending prompt', content: '', timestamp: '2026-11-15T12:00:00Z' }],
    has_more: false,
  } }));
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  const order = await page.evaluate(() => [...messages.children]
    .filter(el => el.classList.contains('boot-banner') || el.classList.contains('msg-thinking'))
    .map(el => el.classList.contains('boot-banner') ? 'logo' : 'pending'));
  expect(order).toEqual(['logo', 'pending']);
});

test('banner restoration keeps the live prompt and WIP together', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  const order = await page.evaluate(() => {
    const user = makeUserBubble('Live prompt', 'default', null);
    const pending = makeWipBubble({ id: 502, topic: 'default', prompt: 'Live prompt' });
    const banner = document.createElement('div');
    banner.className = 'boot-banner';
    banner._historyNeighbors = { before: [], after: ['502'] };
    messages.append(user);
    addTimestamp(user, new Date().toISOString(), true);
    messages.append(pending, banner);
    restoreBootBannerPositions();
    return [...messages.children].filter(el => [user, pending, banner].includes(el))
      .map(el => el === user ? 'prompt' : el === pending ? 'pending' : 'logo');
  });
  expect(order).toEqual(['logo', 'prompt', 'pending']);
});

test('delayed startup logo stays before a prompt whose response already finished', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  const order = await page.evaluate(async () => {
    const user = makeUserBubble('Fast completed turn', 'default', null);
    messages.appendChild(user);
    addTimestamp(user, new Date().toISOString(), true);
    const response = appendHistoryItem({ id: 601, topic: 'default', content: 'Finished before startup logo', status: 'done' }, messages);
    const logo = document.createElement('div');
    logo.className = 'boot-banner';
    logo.textContent = 'Delayed startup logo';
    await appendBootBannerAtBottom(logo, { beforePending: true });
    return [...messages.children].filter(el => [user, response, logo].includes(el))
      .map(el => el === user ? 'prompt' : el === response ? 'response' : 'logo');
  });
  expect(order).toEqual(['logo', 'prompt', 'response']);
});

test('history rebuild finds the same message after an earlier message grows', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  const position = await page.evaluate(async () => {
    const first = { id: 701, topic: 'default', status: 'done', content: 'Earlier message', completed_at: '2026-11-15T10:00:00Z' };
    const second = { ...first, id: 702, content: 'Reading this message', completed_at: '2026-11-15T11:00:00Z' };
    const originalAppend = appendHistoryItems;
    let firstHeight = 800;
    appendHistoryItems = (items, fragment, insertItem) => {
      originalAppend(items, fragment, insertItem);
      messages.querySelectorAll('.msg.assistant').forEach(el => {
        el.style.height = `${el.dataset.msgId === '701' ? firstHeight : 800}px`;
        el.style.flexShrink = '0';
      });
    };
    try {
      await resetHistoryToLatest({ data: { items: [second, first], has_more: false } });
      const oldRow = messages.querySelector(':scope > .msg.assistant[data-msg-id="702"]');
      messages.scrollTop = messages.scrollHeight - messages.clientHeight;
      const before = oldRow.getBoundingClientRect().top;
      const oldTop = messages.scrollTop;
      firstHeight = 1000;
      await resetHistoryToLatest({ data: { items: [second, first], has_more: false }, followLatest: false });
      const newRow = messages.querySelector(':scope > .msg.assistant[data-msg-id="702"]');
      return { before, after: newRow.getBoundingClientRect().top, replaced: oldRow !== newRow, delta: messages.scrollTop - oldTop };
    } finally { appendHistoryItems = originalAppend; }
  });
  expect(position.replaced).toBe(true);
  expect(position.after).toBe(position.before);
  expect(position.delta).toBe(200);
});

test('history rebuild that drops the reading anchor preserves the nearest offset', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  const gap = await page.evaluate(async () => {
    const item = (id, hour) => ({ id, topic: 'default', status: 'done', content: `Message ${id}`, completed_at: `2026-11-15T${hour}:00:00Z` });
    const originalAppend = appendHistoryItems;
    appendHistoryItems = (items, fragment, insertItem) => {
      originalAppend(items, fragment, insertItem);
      messages.querySelectorAll('.msg.assistant').forEach(el => { el.style.height = '800px'; el.style.flexShrink = '0'; });
    };
    try {
      await resetHistoryToLatest({ data: { items: [item(712, 12), item(711, 11), item(710, 10)], has_more: false } });
      messages.querySelector(':scope > .msg.assistant[data-msg-id="711"]').scrollIntoView();
      const before = messages.scrollTop;
      await resetHistoryToLatest({ data: { items: [item(714, 14), item(713, 13)], has_more: false }, followLatest: false });
      return messages.scrollTop - Math.min(before, messages.scrollHeight - messages.clientHeight);
    } finally { appendHistoryItems = originalAppend; }
  });
  expect(Math.abs(gap)).toBeLessThanOrEqual(1);
});

test('bottom-pinned transcript stays pinned when its viewport shrinks', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  const gap = await page.evaluate(async () => {
    const filler = document.createElement('div');
    filler.style.height = '2500px';
    filler.style.flexShrink = '0';
    messages.appendChild(filler);
    messages.scrollTop = messages.scrollHeight;
    await new Promise(r => setTimeout(r, 50));
    const viewportHeight = messages.clientHeight;
    messages.style.height = `${viewportHeight - 200}px`;
    messages.style.flex = 'none';
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    return messages.scrollHeight - messages.scrollTop - messages.clientHeight;
  });
  expect(gap).toBeLessThanOrEqual(1);
});

test('restored scrollback stays in place when the viewport shrinks', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  const position = await page.evaluate(async () => {
    const filler = document.createElement('div');
    filler.style.cssText = 'height:2500px;flex-shrink:0';
    messages.appendChild(filler);
    scrollToBottom(true);
    // Restore reading position and resize before the asynchronous scroll event.
    messages.scrollTop = 200;
    restoreTranscriptPosition(captureTranscriptPosition());
    const viewportHeight = messages.clientHeight;
    messages.style.height = `${viewportHeight - 200}px`;
    messages.style.flex = 'none';
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return messages.scrollTop;
  });
  expect(position).toBe(200);
});

for (const [mode, pendingId] of ['refresh', 'reload', 'jump'].flatMap(mode => [100, 102].map(id => [mode, id]))) {
  test(`new submission retains ordering across ${mode} and completion ${pendingId}`, async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
    await page.route('**/chat', r => r.fulfill({ status: 200, headers: SSE_HEADERS,
      body: sse({ event: 'meta', data: { msg_id: pendingId } }, { event: 'status', data: 'Working...' }),
    }));
    const old = { id: 90, topic: 'default', status: 'done', content: 'Older conversation', completed_at: '2026-11-15T10:00:00Z' };
    const newer = { ...old, id: 101, content: 'Concurrent response', completed_at: '2026-11-15T11:00:00Z' };
    await page.evaluate(old => resetHistoryToLatest({ data: { items: [old], has_more: false } }), old);
    await page.fill('#input', 'Newest human prompt');
    await page.keyboard.press('Enter');
    await expect(page.locator(`.msg-thinking[data-msg-id="${pendingId}"]`)).toBeVisible();
    await page.route('**/history**', r => r.fulfill({ json: {
      items: [newer, old], has_more: false, found: true, target_id: 90, has_older: false, has_newer: false,
    } }));
    if (mode === 'refresh') {
      await page.evaluate(items => resetHistoryToLatest({ data: { items, has_more: false }, followLatest: false }), [newer, old]);
    } else if (mode === 'reload') {
      await page.evaluate(async () => { reloadHistory(); await loadHistory(); });
    } else {
      await page.evaluate(() => jumpToMessage(90));
    }
    await expect(page.locator('.msg.assistant[data-msg-id="90"]')).toHaveCount(1);
    const order = () => page.locator('#messages > .msg').evaluateAll(els => els.map(el => el.classList.contains('user') ? 'prompt' : el.dataset.msgId));
    const rows = await order();
    expect(rows.indexOf('90')).toBeLessThan(rows.indexOf('prompt'));
    expect(rows.indexOf('prompt') + 1).toBe(rows.indexOf(String(pendingId)));
    expect(rows.indexOf('101') < rows.indexOf('prompt')).toBe(pendingId > 101);
    const completed = { ...old, id: pendingId, prompt: 'Newest human prompt', content: 'New response', completed_at: '2026-11-15T12:00:00Z' };
    await page.route(`**/chat/${pendingId}/status`, r => r.fulfill({ json: completed }));
    await page.evaluate(async completed => {
      await replacePendingWithStoredItem(completed, messages.querySelector(`.msg-thinking[data-msg-id="${completed.id}"]`));
    }, completed);
    await expect(page.locator('.msg-thinking')).toHaveCount(0);
    const final = await order();
    expect(final.indexOf('90')).toBeLessThan(final.indexOf('prompt'));
    expect(final.indexOf(String(pendingId))).toBeGreaterThan(final.indexOf('prompt'));
  });
}

test('footer growth and timestamped rebuilds preserve transcript position', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  const result = await page.evaluate(async () => {
    const items = [3, 2, 1].map(id => ({ id, topic: 'default', status: 'done',
      content: ('Response ' + id + '\n\n').repeat(100),
      timestamp: '2026-11-15T10:00:00Z', completed_at: '2026-11-15T11:00:00Z' }));
    await resetHistoryToLatest({ data: { items, has_more: false } });
    const position = captureTranscriptPosition();
    const footer = document.createElement('div');
    footer.style.cssText = 'height:48px;flex-shrink:0';
    messages.appendChild(footer);
    restoreTranscriptPosition(position);
    const gap = messages.scrollHeight - messages.scrollTop - messages.clientHeight;
    const row = messages.querySelector('.assistant[data-msg-id="2"]');
    messages.scrollTop += row.getBoundingClientRect().top - messages.getBoundingClientRect().top + 60;
    const before = row.getBoundingClientRect().top;
    const offsets = [];
    for (let i = 0; i < 3; i++) {
      await resetHistoryToLatest({ data: { items, has_more: false }, followLatest: false });
      offsets.push(messages.querySelector('.assistant[data-msg-id="2"]').getBoundingClientRect().top - before);
    }
    return { gap, offsets };
  });
  expect(result.gap).toBeLessThanOrEqual(1);
  for (const offset of result.offsets) expect(Math.abs(offset)).toBeLessThanOrEqual(1);
});

test('automatic connectivity recovery never fetches history', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  let historyRequests = 0;
  await page.route('**/history**', r => { historyRequests++; return r.fulfill({ json: { items: [], has_more: false } }); });
  await page.evaluate(async () => {
    await checkConnectionHealth();
    window.dispatchEvent(new Event('online'));
    recoverForegroundState();
  });
  await page.waitForTimeout(16000);
  expect(historyRequests).toBe(0);
});

test('live placement preserves completion order when IDs disagree', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
  await page.route('**/chat', r => r.fulfill({ status: 200, headers: SSE_HEADERS,
    body: sse({ event: 'meta', data: { msg_id: 100 } }, { event: 'status', data: 'Working...' }),
  }));
  await page.fill('#input', 'Pending prompt');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg-thinking[data-msg-id="100"]')).toBeVisible();
  await page.evaluate(() => resetHistoryToLatest({ data: { items: [
    { id: 90, status: 'done', topic: 'default', content: 'Later completion', completed_at: '2026-11-15T12:00:00Z' },
    { id: 110, status: 'done', topic: 'default', content: 'Earlier completion', completed_at: '2026-11-15T10:00:00Z' },
  ], has_more: false }, followLatest: false }));
  const ids = await page.locator('#messages > .assistant:not(.msg-thinking)').evaluateAll(els => els.map(el => el.dataset.msgId));
  expect(ids).toEqual(['110', '90']);
});

for (const readAway of [false, true]) {
  test(`no-edit completion late footer growth ${readAway ? 'respects scrollback' : 'keeps timestamp visible'}`, async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await page.evaluate(() => Promise.all([initialHistoryReady, historyBootstrapSettled, bootBannerSettled]));
    await page.evaluate(() => {
      const filler = document.createElement('div');
      filler.style.cssText = 'height:2500px;flex-shrink:0';
      messages.appendChild(filler);
      scrollToBottom(true);
    });
    await page.route('**/chat', r => r.fulfill({ status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Completed without file changes.' }, { event: 'done', data: {} }),
    }));
    await page.fill('#input', 'Check without editing files');
    await page.keyboard.press('Enter');
    const footer = page.locator('#messages > .msg-time:not(.right)').last();
    await expect(footer).toBeVisible();
    await expect.poll(() => page.locator('#messages').evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(1);
    if (readAway) {
      await page.locator('#messages').hover();
      await page.mouse.wheel(0, -200);
      await expect.poll(() => page.locator('#messages').evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeGreaterThan(50);
    }
    const before = await page.locator('#messages').evaluate(el => el.scrollTop);
    // Delayed layout in an existing footer, without explicit restoration.
    await footer.evaluate(el => { el.style.minHeight = '96px'; });
    if (readAway) {
      await page.waitForTimeout(150);
      expect(await page.locator('#messages').evaluate(el => el.scrollTop)).toBe(before);
    } else {
      await expect.poll(() => page.locator('#messages').evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(1);
      expect(await footer.evaluate(el => el.getBoundingClientRect().bottom <= document.getElementById('messages').getBoundingClientRect().bottom)).toBe(true);
      await expect(page.locator('#scroll-btn')).not.toHaveClass(/visible/);
    }
  });
}
