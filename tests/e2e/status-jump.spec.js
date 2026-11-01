const { test, expect } = require('@playwright/test');

test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/health') return route.fulfill({ json: { status: 'ok' } });
    if (path === '/history') return route.fulfill({ json: { items: [], has_more: false } });
    if (['/topics', '/config/agents', '/queue', '/processes'].includes(path)) return route.fulfill({ json: [] });
    return route.continue();
  });
  await page.goto('/');
  await page.waitForFunction(() => typeof jumpToStatusMessage === 'function');
});

test('recovered WIP survives clearing and restore stays outside the scroller', async ({ page }) => {
  await page.evaluate(async () => {
    reloadHistory({ topic: 'other' });
    const bubble = makeWipBubble({ id: 987, topic: 'squid', agent: 'claude', prompt: 'test', status: 'pending' });
    messages.appendChild(bubble);
    setLiveGroupHidden(true);
    await jumpToStatusMessage(987);
    bubble.style.minHeight = '2000px';
    messages.scrollTop = messages.scrollHeight;
  });
  await expect(page.locator('.msg-thinking[data-msg-id="987"]')).toBeVisible();
  await expect(page.locator('#view-chat > #status-jump-notice')).toBeVisible();
  await expect(page.locator('#status-jump-notice + #form')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Restore filter', exact: true })).toBeInViewport();
  await page.getByRole('button', { name: 'Restore filter', exact: true }).click();
  await expect(page.locator('#status-jump-notice')).toHaveCount(0);
  expect(await page.evaluate(() => historyFilter.topic)).toBe('other');
  await expect(page.locator('.msg-thinking[data-msg-id="987"]')).toHaveClass(/live-hidden/);
});

test('search exit fetches the same scope used for subsequent pagination', async ({ page }) => {
  let requested;
  await page.route('**/history/around?**', route => {
    requested = new URL(route.request().url());
    return route.fulfill({ json: {
      found: true, target_id: 988, items: [{ id: 988, topic: 'squid', agent: 'claude', content: 'finished', status: 'done', timestamp: new Date().toISOString() }],
      has_older: false, has_newer: false,
    } });
  });
  await page.evaluate(async () => {
    historyFilter = { topic: 'squid' };
    searchState = { topic: 'squid', keywords: 'test' };
    searchActive = true;
    await jumpToStatusMessage(988);
  });
  expect(requested.searchParams.has('topic')).toBe(false);
  expect(await page.evaluate(() => historyUrlParams().has('topic'))).toBe(false);
  await expect(page.locator('#status-jump-notice')).toBeVisible();
});

test('matching recovered adhoc WIP retains its route filter', async ({ page }) => {
  await page.evaluate(async () => {
    const route = '#squid@claude!';
    reloadHistory({ flow_route: route, adhoc: true });
    const bubble = makeWipBubble({ id: 989, topic: 'squid', agent: 'claude', adhoc: true, flow_route: route, prompt: 'test', status: 'pending' });
    messages.appendChild(bubble);
    setLiveGroupHidden(true);
    await jumpToStatusMessage(989);
  });
  await expect(page.locator('.msg-thinking[data-msg-id="989"]')).toBeVisible();
  await expect(page.locator('#status-jump-notice')).toHaveCount(0);
  expect(await page.evaluate(() => historyFilter.flow_route)).toBe('#squid@claude!');
});

for (const mode of ['bookmarks', 'bad']) {
  test(`${mode}-only hides recovered WIP again after restoring filter`, async ({ page }) => {
    await page.evaluate(mode => {
      messages.appendChild(makeWipBubble({
        id: 990, topic: 'squid', agent: 'claude', prompt: 'pending turn', status: 'pending',
      }));
      if (mode === 'bookmarks') toggleBookmarkOnlyHistory();
      else toggleBadOnlyHistory();
    }, mode);
    const wip = page.locator('.msg-thinking[data-msg-id="990"]');
    await expect(wip).toHaveCount(1);
    await expect(wip).toBeHidden();
    await page.evaluate(() => jumpToStatusMessage(990));
    await expect(wip).toBeVisible();
    await page.getByRole('button', { name: 'Restore filter', exact: true }).click();
    await expect(wip).toBeHidden();
    expect(await page.evaluate(mode => mode === 'bookmarks' ? bookmarkOnlyHistory : badOnlyHistory, mode)).toBe(true);
    await page.evaluate(mode => {
      if (mode === 'bookmarks') toggleBookmarkOnlyHistory();
      else toggleBadOnlyHistory();
    }, mode);
    await expect(wip).toBeVisible();
  });
}


test('jumping to a stored response preserves another recovered live group', async ({ page }) => {
  await page.route('**/history/around?**', route => route.fulfill({ json: {
    found: true, target_id: 992,
    items: [{ id: 992, topic: 'squid', agent: 'claude', content: 'finished', status: 'done', timestamp: new Date().toISOString() }],
    has_older: false, has_newer: false,
  } }));
  await page.evaluate(async () => {
    const prompt = document.createElement('div');
    prompt.className = 'msg user history-item';
    prompt.dataset.msgId = '990';
    prompt.textContent = 'Running prompt';
    messages.appendChild(prompt);
    messages.appendChild(makeWipBubble({ id: 991, topic: 'squid', agent: 'claude', prompt: 'running', status: 'pending' }));
    window.statusJumpLiveBubble = messages.querySelector('[data-msg-id="991"]');
    await jumpToStatusMessage(992);
  });
  await expect(page.locator('.msg-thinking[data-msg-id="991"]')).toBeVisible();
  await expect(page.locator('.msg.user[data-msg-id="990"]')).toBeVisible();
  expect(await page.evaluate(() => messages.querySelector('[data-msg-id="991"]') === window.statusJumpLiveBubble)).toBe(true);
  await expect(page.locator('.msg[data-msg-id="992"]')).toBeVisible();
});
