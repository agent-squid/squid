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

test('credential auto-detect notes that remote browser cookies are unavailable', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/provider/*', route => route.fulfill({
    json: { status: 'ok', raw: 42, used_percent: 42 },
  }));

  await page.goto('/');
  await page.locator('#quota-display').click();
  await expect(page.locator('#quota-creds-popup')).toContainText('Runs on this Squid server machine only');
  await expect(page.locator('#quota-creds-popup')).toContainText('remote phone/tablet browsers cannot share their cookies');

  await page.evaluate(() => document.getElementById('codex-creds-popup').classList.add('open'));
  await expect(page.locator('#codex-creds-popup')).toContainText('Runs on this Squid server machine only');
  await expect(page.locator('#codex-creds-popup')).toContainText('remote phone/tablet browsers cannot share their cookies');
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

  expect(result.delays).toEqual([3000, 10000, 30000]);
  expect(result.quotaCalls).toBe(4);
  expect(result.afterFirst).toEqual({ text: '', isError: false });
  expect(result.finalText).toBe('Claude error');
  expect(result.finalIsError).toBe(true);
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
