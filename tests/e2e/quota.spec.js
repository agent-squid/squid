const { test, expect } = require('@playwright/test');

async function mockShell(page) {
  await page.route('**/health', route => route.fulfill({
    json: {
      status: 'ok',
      harnesses: [{ id: 'claudecode', installed: true, default_provider: 'anthropic', compatible_providers: ['anthropic'] }],
      providers: { anthropic: { label: 'Claude', gauge: { type: 'claude' }, gauge_authed: true } },
    },
  }));
  await page.route('**/config/agents', route => route.fulfill({ json: [] }));
  await page.route('**/topics', route => route.fulfill({ json: [] }));
  await page.route('**/history**', route => route.fulfill({
    json: { items: [], has_more: false },
  }));
  await page.route('**/queue', route => route.fulfill({ json: [] }));
  await page.route('**/processes', route => route.fulfill({ json: [] }));
  await page.route('**/topics/**', route => route.fulfill({ json: {} }));
}

test('topbar quota gauge keeps its neutral original treatment', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/provider/*', route => route.fulfill({
    json: { status: 'ok', raw: 42, used_percent: 42 },
  }));

  await page.goto('/');

  await expect(page.getByRole('button', { name: 'Status' })).toBeVisible();
  await expect(page.locator('#quota-display')).toBeVisible();
  await expect(page.locator('#quota-display')).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  await expect(page.locator('#quota-display svg')).toHaveCount(2);
  await page.locator('#quota-display').click();
  await expect(page.locator('#quota-creds-popup')).toHaveClass(/open/);
});

test('credential popups describe their auth source', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/provider/*', route => route.fulfill({
    json: { status: 'ok', raw: 42, used_percent: 42 },
  }));

  await page.goto('/');
  await page.locator('#quota-display').click();
  await expect(page.locator('#quota-creds-popup')).toContainText('Runs on this Squid server machine only');
  await expect(page.locator('#quota-creds-popup')).toContainText('remote phone/tablet browsers cannot share their cookies');

  await page.evaluate(() => document.getElementById('codex-creds-popup').classList.add('open'));
  await expect(page.locator('#codex-creds-popup')).toContainText('Run codex in the terminal to authenticate on the Squid host');
  await expect(page.locator('#codex-creds-popup')).toContainText('~/.codex/auth.json');
  await expect(page.locator('#codex-creds-popup')).not.toContainText('browser detect');
  await expect(page.locator('#codex-creds-auto')).toHaveCount(0);
});

test('auto-detected Claude credentials reveal the status gauge without a refresh', async ({ page }) => {
  await mockShell(page);
  await page.route('**/health', route => route.fulfill({
    json: {
      status: 'ok',
      harnesses: [{ id: 'claudecode', installed: true, default_provider: 'anthropic', compatible_providers: ['anthropic'] }],
      providers: { anthropic: { label: 'Claude', gauge: { type: 'claude' }, gauge_authed: false } },
    },
  }));
  await page.route('**/config/creds/auto', route => route.fulfill({
    json: { ok: true, claude_org_id: 'org-12345678' },
  }));
  await page.route('**/quota/provider/*', route => route.fulfill({
    json: { status: 'ok', raw: 42, used_percent: 42 },
  }));

  await page.goto('/');
  await page.locator('#quota-display').click();
  await page.locator('#creds-auto').click();
  await page.getByRole('button', { name: 'Status' }).click();

  const claudeRow = page.locator('#proc-status-popup .quota-status-row').filter({ hasText: 'Claude' });
  await expect(claudeRow).toHaveCount(1);
  await expect(claudeRow).toContainText('42%');
});

test('topbar quota gauge is chat-only while status keeps quota available', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/provider/*', route => route.fulfill({
    json: { status: 'ok', raw: 42, used_percent: 42 },
  }));
  await page.route('**/config/localfile-roots**', route => route.fulfill({ json: { roots: [] } }));

  await page.goto('/');
  await expect(page.locator('#quota-display')).toBeVisible();
  await page.locator('#quota-display').click();
  await expect(page.locator('#quota-creds-popup')).toHaveClass(/open/);

  await page.getByRole('button', { name: 'Files' }).click();
  await expect(page.locator('#quota-display')).toBeHidden();
  await expect(page.locator('#quota-creds-popup')).not.toHaveClass(/open/);

  await page.getByRole('button', { name: 'Status' }).click();
  const rows = page.locator('#proc-status-popup .quota-status-row');
  await expect(rows).toHaveCount(1);
  await expect(rows.filter({ hasText: 'Claude' })).toContainText('42%');

  await page.getByRole('button', { name: 'Chat' }).click();
  await expect(page.locator('#quota-display')).toBeVisible();
});

test('status quota rows come from provider catalog', async ({ page }) => {
  const providerQuotaPaths = [];
  await page.route('**/quota/backend/*', route => route.abort());
  await page.route('**/health', route => route.fulfill({
    json: {
      status: 'ok',
      providers: {
        anthropic: {
          label: 'Anthropic',
          gauge: { type: 'claude' },
          gauge_authed: true,
        },
      },
    },
  }));
  await page.route('**/quota/provider/anthropic', route => {
    providerQuotaPaths.push(new URL(route.request().url()).pathname);
    return route.fulfill({
      json: { status: 'ok', raw: 37, used_percent: 37, title: 'Anthropic usage' },
    });
  });
  await page.route('**/config/agents', route => route.fulfill({ json: [] }));
  await page.route('**/topics', route => route.fulfill({ json: [] }));
  await page.route('**/history**', route => route.fulfill({
    json: { items: [], has_more: false },
  }));
  await page.route('**/queue', route => route.fulfill({ json: [] }));
  await page.route('**/processes', route => route.fulfill({ json: [] }));
  await page.route('**/topics/**', route => route.fulfill({ json: {} }));

  await page.goto('/');
  await page.getByRole('button', { name: 'Status' }).click();

  const rows = page.locator('#proc-status-popup .quota-status-row');
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText('Anthropic');
  await expect(rows.first()).toContainText('37%');
  expect(providerQuotaPaths).toContain('/quota/provider/anthropic');
  expect(providerQuotaPaths.every(path => path === '/quota/provider/anthropic')).toBe(true);
});

test('quota transient errors retry before showing failure', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/provider/*', route => route.fulfill({
    json: { status: 'ok', raw: 42, used_percent: 42 },
  }));

  await page.goto('/');
  await expect(page.locator('#quota-5h-pct')).toHaveText('42');

  const result = await page.evaluate(async () => {
    const realFetch = window.fetch.bind(window);
    const realSetTimeout = window.setTimeout.bind(window);
    const realClearTimeout = window.clearTimeout.bind(window);
    const delays = [];
    const callbacks = [];
    let quotaCalls = 0;

    window.fetch = async (url, options) => {
      if (String(url).includes('/quota/provider/anthropic')) {
        quotaCalls++;
        return new Response('', { status: 502 });
      }
      return realFetch(url, options);
    };
    window.setTimeout = (callback, delay) => {
      delays.push(delay);
      callbacks.push(callback);
      return delay;
    };
    window.clearTimeout = () => {};

    try {
      await fetchQuotaForBackend('anthropic');
      const afterFirst = {
        text: document.getElementById('quota-label').textContent,
        isError: document.getElementById('quota-display').classList.contains('error'),
      };

      await callbacks.shift()();
      await callbacks.shift()();
      await callbacks.shift()();

      return {
        delays,
        quotaCalls,
        afterFirst,
        finalText: document.getElementById('quota-label').textContent,
        finalIsError: document.getElementById('quota-display').classList.contains('error'),
      };
    } finally {
      window.fetch = realFetch;
      window.setTimeout = realSetTimeout;
      window.clearTimeout = realClearTimeout;
    }
  });

  expect(result.delays).toEqual([3000, 10000, 30000, 60000]);
  expect(result.quotaCalls).toBe(4);
  expect(result.afterFirst).toEqual({ text: '', isError: false });
  expect(result.finalText).toBe('Claude error');
  expect(result.finalIsError).toBe(true);
});

test('codex quota transient error recovers without page refresh', async ({ page }) => {
  await page.route('**/health', route => route.fulfill({
    json: {
      status: 'ok',
      harnesses: [{ id: 'codex', installed: true, default_provider: 'openai', compatible_providers: ['openai'] }],
      providers: { openai: { label: 'GPT', gauge: { type: 'codex' }, gauge_authed: true } },
    },
  }));
  await page.route('**/config/agents', route => route.fulfill({ json: [
    { name: 'gpt', harness: 'codex', provider: 'openai' },
  ] }));
  await page.route('**/topics', route => route.fulfill({ json: [
    { name: 'default', agent: 'gpt', sticky_adhoc: true },
  ] }));
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', route => route.fulfill({ json: [] }));
  await page.route('**/processes', route => route.fulfill({ json: [] }));
  await page.route('**/topics/**', route => route.fulfill({ json: {} }));

  let quotaCalls = 0;
  await page.route('**/quota/provider/openai', route => {
    quotaCalls++;
    if (quotaCalls <= 4) return route.fulfill({ status: 502 });
    return route.fulfill({ json: { status: 'ok', raw: 21, used_percent: 21 } });
  });

  await page.addInitScript(() => {
    const realSetTimeout = window.setTimeout.bind(window);
    const realClearTimeout = window.clearTimeout.bind(window);
    window.__quotaTimerCallbacks = [];
    window.setTimeout = (callback, delay, ...args) => {
      if ([3000, 10000, 30000, 60000].includes(delay)) {
        const handle = { callback, args, delay, cleared: false };
        window.__quotaTimerCallbacks.push(handle);
        return handle;
      }
      return realSetTimeout(callback, delay, ...args);
    };
    window.clearTimeout = handle => {
      if (handle && typeof handle === 'object' && 'cleared' in handle) {
        handle.cleared = true;
        return;
      }
      return realClearTimeout(handle);
    };
  });
  await page.goto('/');
  await page.waitForFunction(() => typeof setVisibleQuotaBackend === 'function');
  await page.evaluate(async () => {
    await bootBannerSettled;
    const state = quotaStateFor('openai');
    if (state.inFlight) await state.requestDone;
    clearQuotaRetry(state);
    window.__quotaTimerCallbacks.length = 0;
  });
  quotaCalls = 0;
  const result = await page.evaluate(async () => {
    setVisibleQuotaBackend('openai');
    await fetchQuotaForBackend('openai');
    const callbacks = window.__quotaTimerCallbacks;
    for (let i = 0; i < 10 && !callbacks.some(item => item.delay === 60000); i++) {
      const next = callbacks.find(item => !item.cleared);
      if (!next) break;
      next.cleared = true;
      await next.callback(...next.args);
    }
    const bgIndex = callbacks.findIndex(item => !item.cleared && item.delay === 60000);
    if (bgIndex < 0) return {
      errored: document.getElementById('codex-quota-display').classList.contains('error'),
      recovered: false,
      text: callbacks.map(item => item.delay).join(','),
    };
    const errored = document.getElementById('codex-quota-display').classList.contains('error');
    callbacks[bgIndex].cleared = true;
    await callbacks[bgIndex].callback(...callbacks[bgIndex].args);
    return {
      errored,
      recovered: document.getElementById('codex-quota-display').classList.contains('loaded'),
      text: document.getElementById('codex-5h-pct').textContent,
    };
  });

  expect(result).toEqual({ errored: true, recovered: true, text: '21' });
  expect(quotaCalls).toBeGreaterThanOrEqual(5);
});

test('codex quota keeps background retrying after repeated visible errors', async ({ page }) => {
  await page.route('**/health', route => route.fulfill({
    json: {
      status: 'ok',
      harnesses: [{ id: 'codex', installed: true, default_provider: 'openai', compatible_providers: ['openai'] }],
      providers: { openai: { label: 'GPT', gauge: { type: 'codex' }, gauge_authed: true } },
    },
  }));
  await page.route('**/config/agents', route => route.fulfill({ json: [
    { name: 'gpt', harness: 'codex', provider: 'openai' },
  ] }));
  await page.route('**/topics', route => route.fulfill({ json: [
    { name: 'default', agent: 'gpt', sticky_adhoc: true },
  ] }));
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', route => route.fulfill({ json: [] }));
  await page.route('**/processes', route => route.fulfill({ json: [] }));
  await page.route('**/topics/**', route => route.fulfill({ json: {} }));

  let quotaCalls = 0;
  await page.route('**/quota/provider/openai', route => {
    quotaCalls++;
    if (quotaCalls <= 5) return route.fulfill({ status: 502 });
    return route.fulfill({ json: { status: 'ok', raw: 34, used_percent: 34 } });
  });

  await page.addInitScript(() => {
    const realSetTimeout = window.setTimeout.bind(window);
    const realClearTimeout = window.clearTimeout.bind(window);
    window.__quotaTimerCallbacks = [];
    window.setTimeout = (callback, delay, ...args) => {
      if ([3000, 10000, 30000, 60000].includes(delay)) {
        const handle = { callback, args, delay, cleared: false };
        window.__quotaTimerCallbacks.push(handle);
        return handle;
      }
      return realSetTimeout(callback, delay, ...args);
    };
    window.clearTimeout = handle => {
      if (handle && typeof handle === 'object' && 'cleared' in handle) {
        handle.cleared = true;
        return;
      }
      return realClearTimeout(handle);
    };
  });
  await page.goto('/');
  await page.waitForFunction(() => typeof setVisibleQuotaBackend === 'function');
  quotaCalls = 0;

  const result = await page.evaluate(async () => {
    setVisibleQuotaBackend('openai');
    await fetchQuotaForBackend('openai');
    const callbacks = window.__quotaTimerCallbacks;
    for (let i = 0; i < 5; i++) {
      const next = callbacks.find(item => !item.cleared);
      if (!next) break;
      next.cleared = true;
      await next.callback(...next.args);
    }
    return {
      delays: callbacks.map(item => item.delay),
      isError: document.getElementById('codex-quota-display').classList.contains('error'),
      isLoaded: document.getElementById('codex-quota-display').classList.contains('loaded'),
      text: document.getElementById('codex-5h-pct').textContent,
    };
  });

  expect(result).toEqual({
    delays: [3000, 10000, 30000, 60000, 60000],
    isError: false,
    isLoaded: true,
    text: '34',
  });
  expect(quotaCalls).toBeGreaterThanOrEqual(6);
});

test('codex quota auth error keeps retrying without page refresh', async ({ page }) => {
  await page.route('**/health', route => route.fulfill({
    json: {
      status: 'ok',
      harnesses: [{ id: 'codex', installed: true, default_provider: 'openai', compatible_providers: ['openai'] }],
      providers: { openai: { label: 'GPT', gauge: { type: 'codex' }, gauge_authed: true } },
    },
  }));
  await page.route('**/config/agents', route => route.fulfill({ json: [
    { name: 'gpt', harness: 'codex', provider: 'openai' },
  ] }));
  await page.route('**/topics', route => route.fulfill({ json: [
    { name: 'default', agent: 'gpt', sticky_adhoc: true },
  ] }));
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', route => route.fulfill({ json: [] }));
  await page.route('**/processes', route => route.fulfill({ json: [] }));
  await page.route('**/topics/**', route => route.fulfill({ json: {} }));

  let quotaCalls = 0;
  await page.route('**/quota/provider/openai', route => {
    quotaCalls++;
    if (quotaCalls === 1) return route.fulfill({ status: 401, json: { error: 'token expired' } });
    return route.fulfill({ json: { status: 'ok', raw: 18, used_percent: 18 } });
  });

  await page.addInitScript(() => {
    const realSetTimeout = window.setTimeout.bind(window);
    const realClearTimeout = window.clearTimeout.bind(window);
    window.__quotaTimerCallbacks = [];
    window.setTimeout = (callback, delay, ...args) => {
      if (delay === 60000) {
        const handle = { callback, args, delay, cleared: false };
        window.__quotaTimerCallbacks.push(handle);
        return handle;
      }
      return realSetTimeout(callback, delay, ...args);
    };
    window.clearTimeout = handle => {
      if (handle && typeof handle === 'object' && 'cleared' in handle) {
        handle.cleared = true;
        return;
      }
      return realClearTimeout(handle);
    };
  });
  await page.goto('/');
  quotaCalls = 0;

  const result = await page.evaluate(async () => {
    setVisibleQuotaBackend('openai');
    clearQuotaRetry(quotaStateFor('openai'));
    window.__quotaTimerCallbacks.length = 0;
    await fetchQuotaForBackend('openai');
    const timer = window.__quotaTimerCallbacks.find(item => !item.cleared && item.delay === 60000);
    const afterError = {
      errored: document.getElementById('codex-quota-display').classList.contains('error'),
      text: document.getElementById('codex-quota-label').textContent,
      hasRetry: !!timer,
    };
    timer.cleared = true;
    await timer.callback(...timer.args);
    return {
      afterError,
      recovered: document.getElementById('codex-quota-display').classList.contains('loaded'),
      text: document.getElementById('codex-5h-pct').textContent,
    };
  });

  expect(result).toEqual({
    afterError: { errored: true, text: 'GPT auth', hasRetry: true },
    recovered: true,
    text: '18',
  });
  expect(quotaCalls).toBe(2);
});

test('codex quota initialization uses configured provider id', async ({ page }) => {
  await page.route('**/health', route => route.fulfill({
    json: {
      status: 'ok',
      harnesses: [{ id: 'codex', installed: true, default_provider: 'gpt', compatible_providers: ['gpt'] }],
      providers: { gpt: { label: 'GPT', gauge: { type: 'codex' }, gauge_authed: true } },
    },
  }));
  await page.route('**/config/agents', route => route.fulfill({ json: [
    { name: 'gpt', harness: 'codex', provider: 'gpt' },
  ] }));
  await page.route('**/topics', route => route.fulfill({ json: [
    { name: 'default', agent: 'gpt', sticky_adhoc: true },
  ] }));
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', route => route.fulfill({ json: [] }));
  await page.route('**/processes', route => route.fulfill({ json: [] }));
  await page.route('**/topics/**', route => route.fulfill({ json: {} }));

  const quotaPaths = [];
  await page.route('**/quota/provider/*', route => {
    quotaPaths.push(new URL(route.request().url()).pathname);
    return route.fulfill({ json: { status: 'ok', raw: 27, used_percent: 27 } });
  });

  await page.goto('/');
  await page.evaluate(async () => {
    await setVisibleQuotaBackend('gpt');
    await fetchCodexQuota();
  });

  await expect(page.locator('#codex-5h-pct')).toHaveText('27');
  expect(quotaPaths).toContain('/quota/provider/gpt');
  expect(quotaPaths).not.toContain('/quota/provider/openai');
});

test('detached status fallback timeout finalizes active quota tracking', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/provider/*', route => route.fulfill({
    json: { status: 'ok', raw: 42, used_percent: 42 },
  }));
  await page.route('**/chat', route => route.fulfill({
    status: 200,
    headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '10' },
    body: 'event: meta\ndata: {"agent":"codex","msg_id":10,"adhoc":true}\n\ndata:partial\n\n',
  }));
  await page.route('**/chat/10/status', route => route.fulfill({ json: {
    id: 10,
    role: 'assistant',
    topic: 'default',
    agent: 'codex',
    adhoc: true,
    status: 'pending',
    content: '',
  } }));

  await page.goto('/');
  await page.evaluate(() => {
    window.__squidStatusIntervals = [];
    const realSetInterval = window.setInterval.bind(window);
    const realClearInterval = window.clearInterval.bind(window);
    window.__restoreSquidIntervals = () => {
      window.setInterval = realSetInterval;
      window.clearInterval = realClearInterval;
    };
    window.setInterval = (callback, delay, ...args) => {
      if (delay === 2000) {
        const handle = { callback, args, delay, cleared: false };
        window.__squidStatusIntervals.push(handle);
        return handle;
      }
      return realSetInterval(callback, delay, ...args);
    };
    window.clearInterval = handle => {
      if (handle && typeof handle === 'object' && 'cleared' in handle) {
        handle.cleared = true;
        return;
      }
      return realClearInterval(handle);
    };
  });

  try {
    await page.fill('#input', 'hello');
    await page.locator('#input').press('Enter');
    await expect.poll(() => page.evaluate(() => quotaState.anthropic?.activeCount || 0)).toBe(1);

    await page.waitForFunction(() => window.__squidStatusIntervals.length > 0);
    await page.evaluate(async () => {
      const handle = window.__squidStatusIntervals[0];
      for (let i = 0; i < 960 && !handle.cleared; i++) {
        await handle.callback(...handle.args);
      }
    });

    await expect.poll(() => page.evaluate(() => quotaState.anthropic?.activeCount || 0)).toBe(0);
    await expect(page.locator('.msg-thinking-done')).toContainText('Recovery timed out.');
  } finally {
    await page.evaluate(() => window.__restoreSquidIntervals?.());
  }
});

test('websocket turn records and shows its quota delta', async ({ page }) => {
  await mockShell(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
  let quotaPct = 40;
  await page.route('**/quota/provider/*', route => route.fulfill({
    json: { status: 'ok', raw: quotaPct, used_percent: quotaPct },
  }));
  const msgQuotaCalls = [];
  const sessionQuotaCalls = [];
  await page.route('**/chat/*/quota-delta', route => {
    msgQuotaCalls.push({ url: route.request().url(), body: route.request().postDataJSON() });
    return route.fulfill({ json: { ok: true } });
  });
  await page.route('**/stats/quota-delta', route => {
    sessionQuotaCalls.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true } });
  });
  await page.route('**/chat/10/status', route => route.fulfill({ json: {
    id: 10,
    role: 'assistant',
    topic: 'default',
    status: 'done',
    content: 'hi there',
    session_id: 'ws-session',
    completed_at: '2026-09-25T12:00:00Z',
    stats: { session_id: 'ws-session', input_tokens: 1, output_tokens: 2 },
  } }));
  await page.routeWebSocket('**/ws/v1', ws => {
    ws.send(JSON.stringify({ v: 1, type: 'hello', payload: {} }));
    ws.onMessage(message => {
      const frame = JSON.parse(message);
      if (frame.type === 'subscribe') {
        ws.send(JSON.stringify({ v: 1, type: 'subscribed', payload: {} }));
      } else if (frame.type === 'chat.start') {
        ws.send(JSON.stringify({ v: 1, type: 'command.result', request_id: frame.request_id, payload: { ok: true, msg_id: 10 } }));
        quotaPct = 42.5;
        setTimeout(() => ws.send(JSON.stringify({
          v: 1, type: 'chat.done', event_id: 1, msg_id: 10, scope: { topic: 'default' }, payload: {},
        })), 50);
      }
    });
  });

  await page.goto('/');
  await page.fill('#input', 'hello');
  await page.locator('#input').press('Enter');

  await expect.poll(() => msgQuotaCalls.length).toBe(1);
  expect(msgQuotaCalls[0].url).toContain('/chat/10/quota-delta');
  expect(msgQuotaCalls[0].body).toEqual({ before: 40, after: 42.5 });
  await expect.poll(() => sessionQuotaCalls.length).toBe(1);
  expect(sessionQuotaCalls[0]).toEqual({ session_id: 'ws-session', before: 40, after: 42.5 });
  await expect(page.locator('.msg.assistant.history-item[data-msg-id="10"] + .stats .stats-quota-delta'))
    .toHaveText(/\+2\.5 pp/);
  await expect.poll(() => page.evaluate(() => quotaState.anthropic?.activeCount || 0)).toBe(0);
});

test('websocket turn whose stored status cannot be read still finalizes quota tracking', async ({ page }) => {
  await mockShell(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
  let quotaPct = 40;
  await page.route('**/quota/provider/*', route => route.fulfill({
    json: { status: 'ok', raw: quotaPct, used_percent: quotaPct },
  }));
  const msgQuotaCalls = [];
  await page.route('**/chat/*/quota-delta', route => {
    msgQuotaCalls.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true } });
  });
  await page.route('**/chat/10/status', route => route.fulfill({ status: 500, body: 'boom' }));
  await page.routeWebSocket('**/ws/v1', ws => {
    ws.send(JSON.stringify({ v: 1, type: 'hello', payload: {} }));
    ws.onMessage(message => {
      const frame = JSON.parse(message);
      if (frame.type === 'subscribe') {
        ws.send(JSON.stringify({ v: 1, type: 'subscribed', payload: {} }));
      } else if (frame.type === 'chat.start') {
        ws.send(JSON.stringify({ v: 1, type: 'command.result', request_id: frame.request_id, payload: { ok: true, msg_id: 10 } }));
        quotaPct = 41;
        setTimeout(() => ws.send(JSON.stringify({
          v: 1, type: 'chat.done', event_id: 1, msg_id: 10, scope: { topic: 'default' }, payload: {},
        })), 50);
      }
    });
  });

  await page.goto('/');
  await page.fill('#input', 'hello');
  await page.locator('#input').press('Enter');

  await expect.poll(() => msgQuotaCalls.length).toBe(1);
  expect(msgQuotaCalls[0]).toEqual({ before: 40, after: 41 });
  await expect.poll(() => page.evaluate(() => quotaState.anthropic?.activeCount || 0)).toBe(0);
});

test('terminal turn on a backend with no quota gauge records no fabricated quota delta', async ({ page }) => {
  await mockShell(page);
  // Anthropic has no `gauge` field at all here — "no quota concept for this
  // backend", distinct from a transient fetch failure.
  await page.route('**/health', route => route.fulfill({
    json: {
      status: 'ok',
      harnesses: [{ id: 'claudecode', installed: true, default_provider: 'anthropic', compatible_providers: ['anthropic'] }],
      providers: { anthropic: { label: 'Claude' } },
    },
  }));
  const quotaProviderCalls = [];
  await page.route('**/quota/provider/*', route => {
    quotaProviderCalls.push(route.request().url());
    return route.abort();
  });
  const quotaDeltaCalls = [];
  await page.route('**/chat/*/quota-delta', route => {
    quotaDeltaCalls.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true } });
  });
  await page.route('**/stats/quota-delta', route => {
    quotaDeltaCalls.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true } });
  });
  await page.route('**/chat', route => route.fulfill({
    status: 200,
    headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '10' },
    body: 'event: meta\ndata: {"agent":"codex","msg_id":10,"adhoc":true}\n\ndata:partial\n\n',
  }));
  await page.route('**/chat/10/status', route => route.fulfill({ json: {
    id: 10,
    role: 'assistant',
    topic: 'default',
    agent: 'codex',
    adhoc: true,
    status: 'error',
    content: 'boom',
  } }));

  await page.goto('/');
  await page.evaluate(() => {
    window.__squidStatusIntervals = [];
    const realSetInterval = window.setInterval.bind(window);
    window.setInterval = (callback, delay, ...args) => {
      if (delay === 2000) {
        const handle = { callback, args, delay };
        window.__squidStatusIntervals.push(handle);
        return handle;
      }
      return realSetInterval(callback, delay, ...args);
    };
    window.__restoreSquidIntervals = () => { window.setInterval = realSetInterval; };
  });

  try {
    await page.fill('#input', 'hello');
    await page.locator('#input').press('Enter');
    await page.waitForFunction(() => window.__squidStatusIntervals.length > 0);
    // Single poll returning status:'error' drives finalizeQuotaTracking()
    // through its 1s internal delay before the (non-)POST would fire.
    await page.evaluate(async () => {
      await window.__squidStatusIntervals[0].callback();
    });
    await expect(page.locator('.msg-thinking-done')).toContainText('boom');
    await page.waitForTimeout(1500);

    expect(quotaProviderCalls).toHaveLength(0);
    expect(quotaDeltaCalls).toHaveLength(0);
  } finally {
    await page.evaluate(() => window.__restoreSquidIntervals?.());
  }
});

test('percentage quota gauges retain their reset countdown', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/provider/*', route => {
    const isCursor = route.request().url().endsWith('/cursor');
    return route.fulfill({ json: {
      status: 'ok',
      text: '100%',
      raw: 100,
      used_percent: 100,
      reset_at: Date.now() / 1000 + (isCursor ? 4.2 * 24 * 60 * 60 : (4 * 60 + 35) * 60),
      title: 'Usage',
    } });
  });

  await page.goto('/');

  // Selecting a topic must load its backend gauge without requiring a prompt.
  await page.evaluate(async () => {
    _providerMetadata.cursor = { label: 'Cursor', gauge: { type: 'cursor' } };
    _topicsCache = [{ name: 'work', agent: 'cursor-agent' }];
    _agentsCache = [{ name: 'cursor-agent', harness: 'cursor', provider: 'cursor' }];
    activeQuotaBackend = 'anthropic';
    input.value = '#work';
    await updateActiveQuotaGauge();
  });
  await expect(page.locator('#cursor-5h-pct')).toHaveText('100');
  await expect(page.locator('#cursor-quota-label')).toHaveText('4.2D');

  // A late response for another backend must not overwrite a shared gauge.
  await page.evaluate(() => {
    _providerMetadata.anthropic = { label: 'Claude', gauge: { type: 'claude' } };
    _providerMetadata.qwen = { label: 'Qwen', gauge: { type: 'static' } };
    setVisibleQuotaBackend('qwen');
    renderQuotaLoaded('qwen', {
      raw: null, pct: null, resetAt: null, displayText: 'Local', title: 'Local quota',
    });
    renderQuotaLoaded('anthropic', {
      raw: 55, pct: 55, resetAt: Date.now() + 60 * 60 * 1000, title: 'Claude usage',
    });
  });
  await expect(page.locator('#static-quota-label')).toHaveText('Local');

  for (const [provider, gauge] of [['anthropic', 'claude'], ['openai', 'codex'], ['cursor', 'cursor']]) {
    await page.evaluate(async ({ provider, gauge }) => {
      _providerMetadata[provider] = { gauge: { type: gauge } };
      setVisibleQuotaBackend(provider);
      await fetchQuotaForBackend(provider);
    }, { provider, gauge });

    const labelId = gauge === 'claude' ? 'quota-label' : `${gauge}-quota-label`;
    const pctId = gauge === 'claude' ? 'quota-5h-pct' : `${gauge}-5h-pct`;
    const expected = gauge === 'cursor'
      ? '4.2D'
      : /^4:3[345]$/;
    await expect(page.locator(`#${pctId}`)).toHaveText('100');
    await expect(page.locator(`#${labelId}`)).toHaveText(expected);

    await page.evaluate(providerId => {
      renderQuotaLoaded(providerId, {
        raw: 97,
        pct: 97,
        resetAt: Date.now() + 4.2 * 24 * 60 * 60 * 1000,
      });
    }, provider);
    await expect(page.locator(`#${pctId}`)).toHaveText('97');
    await expect(page.locator(`#${labelId}`)).toHaveText('4.2D');

    await page.evaluate(providerId => {
      quotaStateFor(providerId).delta = 52;
      updateGaugeLabel(providerId);
    }, provider);
    await expect(page.locator(`#${labelId}`)).toHaveText('4.2D');
  }
});

test('dual quota gauges keep 5h and 7d data separate', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/provider/*', route => route.fulfill({
    json: {
      status: 'ok',
      raw: 42,
      used_percent: 42,
      reset_at: Date.now() / 1000 + 34 * 60,
      seven_day: {
        used_percent: 73,
        reset_at: Date.now() / 1000 + 3.5 * 24 * 60 * 60,
      },
    },
  }));

  await page.goto('/');
  await expect(page.locator('#quota-5h-pct')).toHaveText('42');
  await expect(page.locator('#quota-7d-label')).toHaveText('73');
  await expect(page.locator('#quota-7d-suffix')).toHaveText('3.5D');

  await page.evaluate(() => {
    renderQuotaLoaded('anthropic', {
      raw: 12,
      pct: 12,
      resetAt: Date.now() + 60 * 60 * 1000,
      sevenDay: null,
    });
  });
  await expect(page.locator('#quota-5h-pct')).toHaveText('12');
  await expect(page.locator('#quota-7d-label')).toHaveText('—');
  await expect(page.locator('#quota-7d-suffix')).toHaveText('7D');
});

test('kimi balance gauge shows balance, budget pct, and saves max budget', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/provider/kimi', route => route.fulfill({
    json: {
      status: 'ok', text: '$24.93', raw: 24.93, used_percent: null, reset_at: null,
      title: 'Kimi · $5.07 spent of $30.00', max_budget: 30, max_budget_pct: 17, spent: 5.07,
    },
  }));
  const budgetCalls = [];
  await page.route('**/config/kimi/max-budget', route => {
    budgetCalls.push({ method: route.request().method(), body: route.request().postData() });
    return route.fulfill({ json: { status: 'ok' } });
  });

  await page.goto('/');

  // Selecting a kimi-backed topic activates the kimi balance gauge.
  await page.evaluate(async () => {
    _providerMetadata.kimi = { label: 'Kimi', gauge: { type: 'kimi' } };
    _topicsCache = [{ name: 'work', agent: 'kimi-agent' }];
    _agentsCache = [{ name: 'kimi-agent', harness: 'claudecode', provider: 'kimi' }];
    input.value = '#work';
    await updateActiveQuotaGauge();
  });

  const display = page.locator('#kimi-quota-display');
  await expect(display).toBeVisible();
  await expect(page.locator('#quota-display')).toBeHidden();
  await expect(page.locator('#kimi-quota-label')).toHaveText('$24.93');
  await expect(page.locator('#kimi-pct')).toHaveText('17');

  // The shared balance popup saves against the kimi-scoped endpoint.
  await display.click();
  await expect(page.locator('#balance-max-popup')).toHaveClass(/open/);
  await page.locator('#balance-max-input').fill('30');
  await page.locator('#balance-max-save').click();
  await expect(page.locator('#balance-max-status')).toHaveText('saved ✓');
  expect(budgetCalls).toEqual([{ method: 'POST', body: '{"amount":30}' }]);

  await page.locator('#balance-max-clear').click();
  await expect.poll(() => budgetCalls.length).toBe(2);
  expect(budgetCalls[1]).toEqual({ method: 'DELETE', body: null });
});


test('remote setup remains available without credentials and imports a Codex workspace', async ({ page }) => {
  await mockShell(page);
  await page.route('**/health', route => route.fulfill({ json: {
    status: 'ok', harnesses: [], providers: {
      anthropic: { label: 'Claude', gauge: { type: 'claude' }, gauge_authed: false },
      openai: { label: 'Codex', gauge: { type: 'codex' }, gauge_authed: false },
    },
  } }));
  await page.route('**/config/creds/capabilities', route => route.fulfill({ json: { host_browser_detection: false } }));
  await page.route('**/quota/provider/*', route => route.fulfill({ json: { status: 'ok', used_percent: 12 } }));
  let imported;
  await page.route('**/config/creds/codex', route => {
    imported = route.request().postDataJSON();
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Status', exact: true }).click();
  const rows = page.locator('#proc-status-popup .quota-status-row');
  await expect(rows).toHaveCount(2);
  await rows.filter({ hasText: 'Claude' }).getByRole('button', { name: 'Set up usage' }).click();
  await expect(page.locator('#creds-auto')).toBeDisabled();
  await expect(page.locator('#claude-remote-guide')).toHaveAttribute('open', '');
  await expect(page.locator('#quota-creds-popup')).toContainText('lastActiveOrg');
  await page.getByRole('button', { name: 'Status', exact: true }).click();
  await rows.filter({ hasText: 'Codex' }).getByRole('button', { name: 'Set up usage' }).click();
  await page.locator('#codex-remote-guide summary').click();
  await expect(page.locator('#codex-remote-guide')).toContainText('ChatGPT-Account-ID');
  await page.locator('#codex-creds-token').fill('Bearer example-token');
  await page.locator('#codex-creds-account').fill('workspace-123');
  await page.locator('#codex-creds-save').click();
  await expect(page.locator('#codex-creds-status')).toContainText('saved');
  expect(imported).toEqual({ token: 'Bearer example-token', account_id: 'workspace-123' });
  await expect(page.locator('#codex-creds-token')).toHaveValue('');
  await page.getByRole('button', { name: 'Status', exact: true }).click();
  await expect(rows.filter({ hasText: 'Codex' })).toContainText('12%');
  await expect(rows.filter({ hasText: 'Codex' }).getByRole('button', { name: 'Usage settings' })).toBeVisible();
});


test('browser import verifies fresh usage for a custom provider after an older read finishes', async ({ page }) => {
  await mockShell(page);
  await page.route('**/health', route => route.fulfill({ json: {
    status: 'ok', harnesses: [], providers: {
      office: { label: 'Office Codex', gauge: { type: 'codex' }, gauge_authed: true },
    },
  } }));
  let holdNext = false;
  let oldRead;
  let imported = false;
  let freshReads = 0;
  await page.route('**/quota/provider/office', route => {
    if (holdNext) {
      holdNext = false;
      oldRead = route;
      return;
    }
    if (imported) {
      freshReads++;
      return route.fulfill({ status: 401, json: { error: 'expired' } });
    }
    return route.fulfill({ json: { status: 'ok', raw: 88, used_percent: 88 } });
  });
  await page.route('**/config/creds/codex', route => {
    imported = true;
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Status', exact: true }).click();
  const row = page.locator('.quota-status-row').filter({ hasText: 'Office Codex' });
  await expect(row).toContainText('88%');
  await row.getByRole('button', { name: 'Usage settings' }).click();
  holdNext = true;
  await page.evaluate(() => { void fetchQuotaForBackend('office'); });
  await expect.poll(() => Boolean(oldRead)).toBe(true);
  await page.locator('#codex-creds-token').fill('Bearer replacement-token');
  await page.locator('#codex-creds-save').click();
  await expect(page.locator('#codex-creds-status')).toContainText('saved');
  await expect(page.locator('#codex-creds-status')).not.toContainText('usage connected');
  await oldRead.fulfill({ json: { status: 'ok', raw: 88, used_percent: 88 } });
  await expect(page.locator('#codex-creds-status')).toContainText('usage unavailable');
  expect(freshReads).toBeGreaterThan(0);
});


test('Antigravity refresh uses the selected provider and waits for fresh quota', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/provider/*', route => route.fulfill({
    json: { status: 'ok', raw: 10, used_percent: 10 },
  }));
  await page.route('**/health', route => route.fulfill({ json: { status: 'ok', providers: { anthropic: { label: 'Claude', gauge: { type: 'claude' } }, 'custom-agy': { label: 'Custom AGY', gauge: { type: 'antigravity' } } } } }));
  await page.goto('/');
  await expect(page.locator('#quota-display')).toBeVisible();
  await page.evaluate(() => {
    _providerMetadata['custom-agy'] = { label: 'Custom AGY', gauge: { type: 'antigravity' } };
    resolveActiveQuotaBackend = async () => 'custom-agy';
    setVisibleQuotaBackend('custom-agy');
  });
  let requests = 0;
  await page.route('**/quota/provider/custom-agy', async route => {
    const used = ++requests * 10;
    await new Promise(resolve => setTimeout(resolve, 150));
    await route.fulfill({ json: { status: 'ok', raw: used, used_percent: used, title: 'Live AGY' } });
  });
  await page.locator('#antigravity-quota-display').click();
  await expect(page.locator('#antigravity-pct')).toHaveText(/[1-9]0/);
  await expect(page.locator('#antigravity-quota-display')).toHaveAttribute('title', 'Live AGY');
  const results = await page.evaluate(async () => {
    const first = fetchQuotaForBackend('custom-agy', true);
    const second = fetchQuotaForBackend('custom-agy', true);
    return (await Promise.all([first, second])).map(snapshot => snapshot.raw);
  });
  expect(results[0]).toBeGreaterThanOrEqual(20);
  expect(results[1]).toBe(results[0] + 10);
  expect(requests).toBeGreaterThanOrEqual(3);
  await expect(page.locator('#antigravity-pct')).toHaveText(String(results[1]));
  const beforePoll = requests;
  await page.evaluate(async () => {
    const poll = fetchQuotaForBackend('custom-agy');
    // Multiple polling callers must reuse the in-flight request, even if
    // that read takes longer than a polling interval.
    await Promise.all([poll, fetchQuotaForBackend('custom-agy'), fetchQuotaForBackend('custom-agy')]);
  });
  expect(requests).toBe(beforePoll + 1);
});

for (const baseline of [null, 40]) {
  test(`Antigravity reads quota only after completion and uses the last check (${baseline})`, async ({ page }) => {
    await mockShell(page);
    await page.route('**/config/realtime', r => r.fulfill({ json: { transport: 'sse' } }));
    await page.route('**/quota/provider/anthropic', r => r.fulfill({ json: { status: 'ok', raw: 10, used_percent: 10 } }));
    let quotaReads = 0;
    await page.route('**/quota/provider/custom-agy', r => {
      quotaReads++;
      return r.fulfill({ json: { status: 'ok', raw: 42, used_percent: 42 } });
    });
    const deltas = [];
    await page.route('**/chat/*/quota-delta', r => {
      deltas.push(r.request().postDataJSON());
      return r.fulfill({ json: { ok: true } });
    });
    await page.route('**/stats/quota-delta', r => r.fulfill({ json: { ok: true } }));
    let finish;
    let started = false;
    await page.route('**/chat', async r => {
      started = true;
      await new Promise(resolve => { finish = resolve; });
      await r.fulfill({ contentType: 'text/event-stream', body:
        'event: meta\ndata: {"agent":"agy","provider":"custom-agy","harness":"antigravity","msg_id":10}\n\n' +
        'data: Done\n\nevent: stats\ndata: {"session_id":"agy-session","input_tokens":20,"output_tokens":5}\n\n' +
        'event: done\ndata: \n\n' });
    });
    await page.goto('/');
    await page.evaluate(async () => { await bootBannerSettled; });
    await page.evaluate(value => {
      resolveActiveQuotaBackend = async () => 'anthropic';
      _providerMetadata['custom-agy'] = { label: 'AGY', gauge: { type: 'antigravity' } };
      resolveQuotaProvider = async () => 'custom-agy';
      quotaStateFor('custom-agy').raw = value;
      quotaStateFor('custom-agy').readingKey = JSON.stringify([null]);
    }, baseline);
    await page.fill('#input', 'hello');
    await page.locator('#input').press('Enter');
    await expect.poll(() => started).toBe(true);
    expect(quotaReads).toBe(0);
    if (baseline !== null) await page.evaluate(() => { quotaStateFor('custom-agy').raw = 41; });
    finish();
    await expect.poll(() => quotaReads).toBe(1);
    await expect.poll(() => page.evaluate(() => quotaStateFor('custom-agy').activeCount)).toBe(0);
    if (baseline === null) {
      expect(deltas).toEqual([]);
    } else {
      expect(deltas).toEqual([{ before: 41, after: 42 }]);
      await expect(page.locator('.stats-quota-delta').last()).toHaveAttribute('title', /Change since last check/);
    }
  });
}

test('Antigravity baselines survive errors and rebase bucket switches and resets', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/provider/*', r => r.fulfill({ json: { status: 'ok', raw: 10, used_percent: 10 } }));
  await page.goto('/');
  await page.evaluate(async () => { await bootBannerSettled; });
  await page.evaluate(() => {
    _providerMetadata['custom-agy'] = { label: 'AGY', gauge: { type: 'antigravity' } };
  });
  let payload;
  let fail = false;
  await page.route('**/quota/provider/custom-agy', async r => {
    await new Promise(resolve => setTimeout(resolve, 30));
    return fail ? r.fulfill({ status: 502 }) : r.fulfill({ json: payload });
  });
  const reading = (used, group = 'Gemini', reset = '2026-11-15T00:00:00Z') => ({
    status: 'ok', raw: used, used_percent: used, reset_at: reset,
    buckets: [{ group, window: 'weekly', used_percent: used, reset_at: reset }],
  });
  const fetchReading = () => page.evaluate(async () => {
    const result = await fetchQuotaForBackend('custom-agy', true);
    return result && { raw: result.raw, previousRaw: result.previousRaw };
  });
  payload = reading(40);
  expect(await fetchReading()).toEqual({ raw: 40, previousRaw: null });
  fail = true;
  expect(await fetchReading()).toBeNull();
  fail = false;
  for (const invalid of [{ status: 'ok' }, { status: 'error', raw: 0 },
                         { status: 'ok', raw: '42' }, { status: 'ok', raw: 101 }]) {
    payload = invalid;
    expect(await fetchReading()).toBeNull();
  }
  payload = reading(42);
  expect(await fetchReading()).toEqual({ raw: 42, previousRaw: 40 });
  payload = reading(50, 'Claude');
  expect(await fetchReading()).toEqual({ raw: 50, previousRaw: null });
  payload = reading(2, 'Claude', '2026-11-22T00:00:00Z');
  expect(await fetchReading()).toEqual({ raw: 2, previousRaw: null });
  payload = reading(1, 'Claude', '2026-11-22T00:00:00Z');
  expect(await fetchReading()).toEqual({ raw: 1, previousRaw: null });
  payload = reading(3, 'Claude', '2026-11-22T00:00:00Z');
  const results = await page.evaluate(async () => {
    return (await Promise.all([
      fetchQuotaForBackend('custom-agy', true), fetchQuotaForBackend('custom-agy', true),
    ])).map(r => ({ raw: r.raw, previousRaw: r.previousRaw }));
  });
  expect(results).toEqual([{ raw: 3, previousRaw: 1 }, { raw: 3, previousRaw: 3 }]);
});

test('discovered Antigravity completion refreshes once and local callbacks keep ownership', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/provider/*', r => r.fulfill({ json: { status: 'ok', raw: 10, used_percent: 10 } }));
  await page.route('**/chat/123/status', r => r.fulfill({ json: {
    id: 123, role: 'assistant', topic: 'default', agent: 'agy', status: 'done', content: 'Done',
  } }));
  await page.goto('/');
  await page.evaluate(async () => { await bootBannerSettled; });
  let reads = 0;
  await page.route('**/quota/provider/custom-agy', r => {
    reads++;
    return r.fulfill({ json: { status: 'ok', raw: 12, used_percent: 12 } });
  });
  await page.evaluate(async () => {
    _providerMetadata['custom-agy'] = { label: 'AGY', gauge: { type: 'antigravity' } };
    window.__providerLookups = 0;
    resolveQuotaProvider = async () => ++window.__providerLookups === 1 ? 'custom-agy' : 'anthropic';
    const item = { id: 123, topic: 'default', agent: 'agy', status: 'pending' };
    await trackQuotaForPendingTurn(item.id, item.topic, item.agent);
    const bubble = document.createElement('div');
    document.getElementById('messages').appendChild(bubble);
    await replacePendingWithStoredItem(item, bubble);
    await replacePendingWithStoredItem(item, document.createElement('div'));
  });
  expect(reads).toBe(1);
  expect(await page.evaluate(() => window.__providerLookups)).toBe(1);
  await page.evaluate(async () => {
    resolveQuotaProvider = async () => 'custom-agy';
    const item = { id: 123, topic: 'default', agent: 'agy', status: 'pending' };
    await trackQuotaForPendingTurn(item.id, item.topic, item.agent);
    await replacePendingWithStoredItem(item, document.createElement('div'), () => {}, () => {});
  });
  expect(reads).toBe(1);
  expect(await page.evaluate(() => quotaStateFor('custom-agy').activeCount)).toBe(0);
});
