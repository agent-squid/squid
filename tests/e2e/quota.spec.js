const { test, expect } = require('@playwright/test');

async function mockShell(page) {
  await page.route('**/health', route => route.fulfill({
    json: { status: 'ok', backends: { claude: { gauge: { type: 'claude' } } } },
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
  await page.route('**/quota/backend/*', route => route.fulfill({
    json: { status: 'ok', raw: 42, used_percent: 42 },
  }));

  await page.goto('/');

  await expect(page.getByRole('button', { name: 'Status' })).toBeVisible();
  await expect(page.locator('#quota-display')).toBeVisible();
  await expect(page.locator('#quota-display')).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  await expect(page.locator('#quota-display svg')).toHaveCount(1);
  await page.locator('#quota-display').click();
  await expect(page.locator('#quota-creds-popup')).toHaveClass(/open/);
});

test('quota transient errors retry before showing failure', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/backend/*', route => route.fulfill({
    json: { status: 'ok', raw: 42, used_percent: 42 },
  }));

  await page.goto('/');
  await expect(page.locator('#quota-label')).toHaveText('42%');

  const result = await page.evaluate(async () => {
    const realFetch = window.fetch.bind(window);
    const realSetTimeout = window.setTimeout.bind(window);
    const realClearTimeout = window.clearTimeout.bind(window);
    const delays = [];
    const callbacks = [];
    let quotaCalls = 0;

    window.fetch = async (url, options) => {
      if (String(url).includes('/quota/backend/claude')) {
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
      await fetchQuotaForBackend('claude');
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
  expect(result.afterFirst).toEqual({ text: '42%', isError: false });
  expect(result.finalText).toBe('claude error');
  expect(result.finalIsError).toBe(true);
});

test('percentage quota gauges retain their reset countdown', async ({ page }) => {
  await mockShell(page);
  await page.route('**/quota/backend/*', route => {
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
    _backendMetadata = { cursor: { gauge: { type: 'cursor' } } };
    _topicsCache = [{ name: 'work', agent: 'cursor-agent' }];
    _agentsCache = [{ name: 'cursor-agent', backend: 'cursor' }];
    activeQuotaBackend = 'claude';
    input.value = '#work';
    await updateActiveQuotaGauge();
  });
  await expect(page.locator('#cursor-quota-label')).toHaveText('100% in 4.2D');

  // A late response for another backend must not overwrite a shared gauge.
  await page.evaluate(() => {
    _backendMetadata = {
      claude: { gauge: { type: 'claude' } },
      qwen: { gauge: { type: 'static' } },
    };
    setVisibleQuotaBackend('qwen');
    renderQuotaLoaded('qwen', {
      raw: null, pct: null, resetAt: null, displayText: 'Local', title: 'Local quota',
    });
    renderQuotaLoaded('claude', {
      raw: 55, pct: 55, resetAt: Date.now() + 60 * 60 * 1000, title: 'Claude usage',
    });
  });
  await expect(page.locator('#quota-label')).toHaveText('Local');

  for (const backend of ['claude', 'codex', 'cursor']) {
    await page.evaluate(async backendId => {
      _backendMetadata = { [backendId]: { gauge: { type: backendId } } };
      setVisibleQuotaBackend(backendId);
      await fetchQuotaForBackend(backendId);
    }, backend);

    const labelId = backend === 'claude' ? 'quota-label' : `${backend}-quota-label`;
    const expected = backend === 'cursor'
      ? '100% in 4.2D'
      : /^100% in 4:3[345]$/;
    await expect(page.locator(`#${labelId}`)).toHaveText(expected);

    await page.evaluate(backendId => {
      renderQuotaLoaded(backendId, {
        raw: 97,
        pct: 97,
        resetAt: Date.now() + 4.2 * 24 * 60 * 60 * 1000,
      });
    }, backend);
    await expect(page.locator(`#${labelId}`)).toHaveText('97% in 4.2D');

    await page.evaluate(backendId => {
      quotaStateFor(backendId).delta = 52;
      updateGaugeLabel(backendId);
    }, backend);
    await expect(page.locator(`#${labelId}`)).toHaveText('97% in 4.2D');
  }
});
