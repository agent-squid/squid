const { test, expect } = require('@playwright/test');

async function mockBackend(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/search**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
}

test('explicit #all search shows its scope and restores the full command', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.fill('#input', '/s #all claude login');
  await page.keyboard.press('Enter');

  await expect(page.locator('#filter-badge')).toHaveClass(/active/);
  await expect(page.locator('#filter-badge-label')).toHaveText('#all');
  await expect(page.locator('#search-bar-keywords')).toHaveText('claude login');

  await page.locator('#search-bar-keywords').click();
  await expect(page.locator('#input')).toHaveValue('/s #all claude login');
});

test('search and filter tags align and search icon highlights while searching', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.fill('#input', '/s #all claude login');
  await page.keyboard.press('Enter');

  await expect(page.locator('#filter-badge')).toHaveClass(/active/);
  await expect(page.locator('#search-bar')).toHaveClass(/active/);
  await expect(page.locator('#chip-search-btn')).toHaveClass(/active/);
  await expect(page.locator('#chip-search-btn')).toHaveAttribute('aria-pressed', 'true');

  const heights = await page.evaluate(() => ({
    filter: document.querySelector('#filter-badge').getBoundingClientRect().height,
    search: document.querySelector('#search-bar').getBoundingClientRect().height,
  }));
  expect(Math.abs(heights.filter - heights.search)).toBeLessThanOrEqual(1);

  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar')).not.toHaveClass(/active/);
  await expect(page.locator('#chip-search-btn')).not.toHaveClass(/active/);
  await expect(page.locator('#chip-search-btn')).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('#input')).not.toHaveValue(/\/s #all claude login/);
});

test('search composer action inherits active filter scope over composer route', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');

  await page.fill('#input', '/f #squid@claude');
  await page.keyboard.press('Enter');
  await page.locator('.filter-scope-agent .filter-scope-remove').click();
  await expect(page.locator('.filter-scope-topic')).toContainText('#squid');
  await expect(page.locator('.filter-scope-agent')).toHaveCount(0);

  await page.fill('#input', 'needle');
  await page.locator('#chip-search-btn').click();

  await expect(page.locator('#input')).toHaveValue('/s #squid needle');
});

test('unscoped global search does not display or restore #all', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.fill('#input', '/s claude login');
  await page.keyboard.press('Enter');

  await expect(page.locator('#filter-badge')).not.toHaveClass(/active/);
  await expect(page.locator('#search-bar-keywords')).toHaveText('claude login');

  await page.locator('#search-bar-keywords').click();
  await expect(page.locator('#input')).toHaveValue('/s claude login');
});

test('agent-only adhoc search works across topics and preserves its syntax', async ({ page }) => {
  await mockBackend(page);
  let capturedUrl = null;
  await page.route('**/search**', route => {
    capturedUrl = route.request().url();
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('/');

  await page.fill('#input', '/s @claude! needle');
  await page.keyboard.press('Enter');

  expect(capturedUrl).not.toMatch(/topic=/);
  expect(capturedUrl).toMatch(/agent=claude/);
  expect(capturedUrl).toMatch(/adhoc=true/);
  await expect(page.locator('.filter-scope-topic')).toHaveCount(0);
  await expect(page.locator('.filter-scope-agent')).toContainText('@claude!');
  await page.locator('#search-bar-keywords').click();
  await expect(page.locator('#input')).toHaveValue('/s @claude! needle');
});

test('agent search suffix selects session, adhoc, or both for the exact agent', async ({ page }) => {
  await mockBackend(page);
  const urls = [];
  await page.route('**/search**', route => {
    urls.push(route.request().url());
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('/');

  await page.fill('#input', '/s @claude-opus needle');
  await page.keyboard.press('Enter');
  expect(urls.at(-1)).toMatch(/agent=claude-opus/);
  expect(urls.at(-1)).toMatch(/adhoc=false/);

  await page.fill('#input', '/s @claude-opus* needle');
  await page.keyboard.press('Enter');
  expect(urls.at(-1)).toMatch(/agent=claude-opus/);
  expect(urls.at(-1)).not.toMatch(/adhoc=/);
  await expect(page.locator('.filter-scope-agent')).toContainText('@claude-opus*');

  await page.locator('#search-bar-keywords').click();
  await expect(page.locator('#input')).toHaveValue('/s @claude-opus* needle');
});

test('clearing the #all scope removes its explicit marker', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.fill('#input', '/s #all claude login');
  await page.keyboard.press('Enter');
  await expect(page.locator('#filter-badge-label')).toHaveText('#all');

  await page.locator('.filter-scope-topic .filter-scope-remove').click();
  await expect(page.locator('#filter-badge')).not.toHaveClass(/active/);

  await page.locator('#search-bar-keywords').click();
  await expect(page.locator('#input')).toHaveValue('/s claude login');
});

test('search keywords are highlighted in result content only', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/search**', route => route.fulfill({
    json: {
      items: [{
        id: 42,
        topic: 'squid',
        agent: 'codex',
        prompt: 'Where did the needle search go?',
        content: 'A Needle appears in search prose. `needle search` is highlighted inline.\n\n```text\nneedle search is highlighted in a block\n```',
      }],
    },
  }));
  await page.goto('/');

  await page.fill('#input', '/s needle search');
  await page.keyboard.press('Enter');

  const result = page.locator('.search-result-item');
  await expect(result).toHaveCount(1);
  await expect(result.locator('.user-ctx')).toHaveText(/^ctx:/);
  await expect(result.locator('.user-ctx')).not.toContainText('#42');
  await expect(result.locator('mark.search-kw-highlight')).toHaveText(['Needle', 'search', 'needle', 'search', 'needle', 'search']);
  await expect(result.locator('.history-prompt mark.search-kw-highlight')).toHaveCount(0);
  await expect(result.locator('code:not(pre code) mark.search-kw-highlight')).toHaveText(['needle', 'search']);
  await expect(result.locator('pre mark.search-kw-highlight')).toHaveText(['needle', 'search']);
});

test('search highlighting follows FTS tokenization for punctuation-prefixed terms', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/search**', route => route.fulfill({
    json: {
      items: [{
        id: 144,
        topic: 'squid',
        agent: 'codex',
        prompt: 'What is a backend?',
        content: 'The backend knows how to collect cost/usage from the runtime.',
      }],
    },
  }));
  await page.goto('/');

  await page.fill('#input', '/s /cost');
  await page.keyboard.press('Enter');

  const result = page.locator('.search-result-item');
  await expect(result).toHaveCount(1);
  await expect(result.locator('mark.search-kw-highlight')).toHaveText(['cost']);
});

test('user prompt search includes the active filter scope', async ({ page }) => {
  await mockBackend(page);
  const urls = [];
  await page.route('**/search**', route => {
    urls.push(route.request().url());
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('/');

  await page.fill('#input', '/f #squid@claude!');
  await page.keyboard.press('Enter');
  await page.locator('#chip-prompts-btn').click();
  await page.fill('#input', '/s needle');
  await page.keyboard.press('Enter');

  const last = urls.at(-1);
  expect(last).toMatch(/role=user/);
  expect(last).toMatch(/topic=squid/);
  expect(last).toMatch(/agent=claude/);
  expect(last).toMatch(/adhoc=true/);
});

test('bookmarked search includes the active filter scope and keeps bookmark-only results', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/search**');
  const urls = [];
  await page.route('**/search**', route => {
    urls.push(route.request().url());
    return route.fulfill({ json: { items: [
      { id: 42, topic: 'squid', agent: 'claude', prompt: 'Find it', content: 'Needle bookmark', status: 'done', adhoc: false, timestamp: new Date().toISOString() },
      { id: 99, topic: 'squid', agent: 'claude', prompt: 'Find another', content: 'Needle unbookmarked', status: 'done', adhoc: false, timestamp: new Date().toISOString() },
    ] } });
  });
  await page.goto('/');
  await page.evaluate(() => {
    _bookmarkItems = [{ id: 42, topic: 'squid', agent: 'claude', content: 'Needle bookmark' }];
    _bookmarkIds = new Set(_bookmarkItems.map(i => i.id));
  });

  await page.fill('#input', '/f #squid@claude');
  await page.keyboard.press('Enter');
  await page.locator('#chip-bookmark-btn').click();
  await page.fill('#input', '/s needle');
  await page.keyboard.press('Enter');

  const last = urls.at(-1);
  expect(last).toMatch(/role=assistant/);
  expect(last).toMatch(/bookmarked=true/);
  expect(last).toMatch(/topic=squid/);
  expect(last).toMatch(/agent=claude/);
  expect(last).toMatch(/adhoc=false/);
  const result = page.locator('.msg.assistant.search-result-item');
  await expect(result).toHaveCount(1);
  await expect(result).toContainText('Needle bookmark');
  await expect(result).not.toContainText('Needle unbookmarked');
});

test('clearing search restores the filtered bookmark-only history list', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/history**');
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/history/by-ids**', route => route.fulfill({ json: { items: [
    { id: 42, role: 'assistant', topic: 'squid', agent: 'claude', prompt: 'Find it', content: 'Needle bookmark', status: 'done', adhoc: false, timestamp: new Date().toISOString() },
    { id: 99, role: 'assistant', topic: 'other', agent: 'claude', prompt: 'Other', content: 'Other bookmark', status: 'done', adhoc: false, timestamp: new Date().toISOString() },
  ] }}));
  await page.route('**/search**', route => route.fulfill({ json: { items: [
    { id: 42, topic: 'squid', agent: 'claude', prompt: 'Find it', content: 'Needle bookmark', status: 'done', adhoc: false, timestamp: new Date().toISOString() },
  ] } }));

  await page.goto('/');
  await page.evaluate(() => {
    _bookmarkItems = [
      { id: 42, topic: 'squid', agent: 'claude', content: 'Needle bookmark' },
      { id: 99, topic: 'other', agent: 'claude', content: 'Other bookmark' },
    ];
    _bookmarkIds = new Set(_bookmarkItems.map(i => i.id));
  });

  await page.fill('#input', '/f #squid');
  await page.keyboard.press('Enter');
  await page.locator('#chip-bookmark-btn').click();
  await expect(page.locator('.msg.assistant.history-item')).toHaveCount(1);
  await expect(page.locator('.msg.assistant.history-item')).toContainText('Needle bookmark');

  await page.fill('#input', '/s needle');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant.search-result-item')).toHaveCount(1);

  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar')).not.toHaveClass(/active/);
  await expect(page.locator('.msg.assistant.history-item')).toHaveCount(1);
  await expect(page.locator('.msg.assistant.history-item')).toContainText('Needle bookmark');
  await expect(page.locator('.msg.assistant.history-item')).not.toContainText('Other bookmark');
});

test('clearing search preserves a topic-only filter after removing agent scope', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/search**', route => route.fulfill({ json: { items: [] } }));

  await page.goto('/');
  await page.fill('#input', '/f #squid@claude');
  await page.keyboard.press('Enter');
  await page.locator('.filter-scope-agent .filter-scope-remove').click();
  await expect(page.locator('#filter-badge-label')).toHaveText('#squid');
  await expect(page.locator('.filter-scope-agent')).toHaveCount(0);

  await page.fill('#input', '/s needle');
  await page.keyboard.press('Enter');
  await expect(page.locator('#search-bar')).toHaveClass(/active/);

  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar')).not.toHaveClass(/active/);
  await expect(page.locator('#filter-badge-label')).toHaveText('#squid');
  await expect(page.locator('.filter-scope-agent')).toHaveCount(0);
});

test('search icon prefill then clear preserves topic-only filter after removing agent scope', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/search**', route => route.fulfill({ json: { items: [] } }));

  await page.goto('/');
  await page.fill('#input', '/f #squid@claude');
  await page.keyboard.press('Enter');
  await page.locator('.filter-scope-agent .filter-scope-remove').click();
  await expect(page.locator('#filter-badge-label')).toHaveText('#squid');

  await page.fill('#input', 'needle');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#input')).toHaveValue('/s #squid needle');
  await page.keyboard.press('Enter');
  await expect(page.locator('#search-bar')).toHaveClass(/active/);

  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar')).not.toHaveClass(/active/);
  await expect(page.locator('#filter-badge-label')).toHaveText('#squid');
  await expect(page.locator('.filter-scope-agent')).toHaveCount(0);
});

test('bookmark search clear preserves topic-only filter after removing agent scope', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/history**');
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/history/by-ids**', route => route.fulfill({ json: { items: [
    { id: 42, role: 'assistant', topic: 'squid', agent: 'claude', prompt: 'Find it', content: 'Needle bookmark', status: 'done', adhoc: false, timestamp: new Date().toISOString() },
  ] }}));
  await page.route('**/search**', route => route.fulfill({ json: { items: [
    { id: 42, topic: 'squid', agent: 'claude', prompt: 'Find it', content: 'Needle bookmark', status: 'done', adhoc: false, timestamp: new Date().toISOString() },
  ] } }));

  await page.goto('/');
  await page.evaluate(() => {
    _bookmarkItems = [{ id: 42, topic: 'squid', agent: 'claude', content: 'Needle bookmark' }];
    _bookmarkIds = new Set(_bookmarkItems.map(i => i.id));
  });
  await page.fill('#input', '/f #squid@claude');
  await page.keyboard.press('Enter');
  await page.locator('.filter-scope-agent .filter-scope-remove').click();
  await page.locator('#chip-bookmark-btn').click();
  await expect(page.locator('#filter-badge-label')).toHaveText('#squid');

  await page.fill('#input', 'needle');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#input')).toHaveValue('/s #squid needle');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant.search-result-item')).toHaveCount(1);

  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#filter-badge-label')).toHaveText('#squid');
  await expect(page.locator('.filter-scope-agent')).toHaveCount(0);
  await expect(page.locator('.msg.assistant.history-item')).toHaveCount(1);
});

test('bookmark search clear does not restore filter removed during search', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/history**');
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/history/by-ids**', route => route.fulfill({ json: { items: [
    { id: 42, role: 'assistant', topic: 'squid', agent: 'codex', prompt: 'Find it', content: 'Needle bookmark', status: 'done', adhoc: false, timestamp: new Date().toISOString() },
  ] }}));
  await page.route('**/search**', route => route.fulfill({ json: { items: [
    { id: 42, topic: 'squid', agent: 'codex', prompt: 'Find it', content: 'Needle bookmark', status: 'done', adhoc: false, timestamp: new Date().toISOString() },
  ] } }));

  await page.goto('/');
  await page.evaluate(() => {
    _bookmarkItems = [{ id: 42, topic: 'squid', agent: 'codex', content: 'Needle bookmark' }];
    _bookmarkIds = new Set(_bookmarkItems.map(i => i.id));
  });
  await page.locator('#chip-bookmark-btn').click();
  await page.fill('#input', '/f #squid@codex');
  await page.keyboard.press('Enter');
  await page.locator('.filter-scope-topic .filter-scope-remove').click();
  await expect(page.locator('#filter-badge-label')).toHaveText('@codex');

  await page.fill('#input', 'needle');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#input')).toHaveValue('/s @codex needle');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant.search-result-item')).toHaveCount(1);

  await page.locator('#chip-filter-btn').click();
  await expect(page.locator('#filter-badge')).not.toHaveClass(/active/);

  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar')).not.toHaveClass(/active/);
  await expect(page.locator('#filter-badge')).not.toHaveClass(/active/);
});

test('a live in-flight message stays hidden while searching, then reappears on clear', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));

  let fulfillChat;
  const chatIntercepted = new Promise(resolve => {
    page.route('**/chat', route => {
      fulfillChat = body => route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no' },
        body,
      });
      resolve();
    });
  });

  await page.goto('/');
  await page.fill('#input', 'hello');
  await page.keyboard.press('Enter');
  await chatIntercepted;
  await fulfillChat(`event: meta\ndata: ${JSON.stringify({ agent: 'claude', backend: 'claude', msg_id: 1, adhoc: false })}\n\n`);

  const thinking = page.locator('.msg.assistant.msg-thinking');
  const userBubble = page.locator('.msg.user').last();
  await expect(thinking).toBeVisible();
  await expect(userBubble).toBeVisible();

  await page.fill('#input', '/s claude login');
  await page.keyboard.press('Enter');

  // Still in the DOM (so streaming keeps updating it) but not shown while searching —
  // search results only ever reflect what's already persisted in the DB.
  await expect(thinking).toBeAttached();
  await expect(thinking).not.toBeVisible();
  await expect(userBubble).not.toBeVisible();

  await page.locator('#chip-search-btn').click();
  await expect(thinking).toBeVisible();
  await expect(userBubble).toBeVisible();
});
