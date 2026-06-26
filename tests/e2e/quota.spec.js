const { test, expect } = require('@playwright/test');

test('percentage quota gauges retain their reset countdown', async ({ page }) => {
  await page.route('**/health', route => route.fulfill({
    json: { status: 'ok', backends: {} },
  }));
  await page.route('**/config/agents', route => route.fulfill({ json: [] }));
  await page.route('**/topics', route => route.fulfill({ json: [] }));
  await page.route('**/history**', route => route.fulfill({
    json: { items: [], has_more: false },
  }));
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
