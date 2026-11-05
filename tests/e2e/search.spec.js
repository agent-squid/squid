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

test('explicit #all search shows editable scope and keywords', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.fill('#input', '/s #all claude login');
  await page.keyboard.press('Enter');

  await expect(page.locator('#filter-badge')).toHaveClass(/active/);
  await expect(page.locator('#filter-badge-label')).toHaveText('#all');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('claude login');

  await page.locator('#search-bar-keywords').click();
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('claude login');
});

test('search inputs have breathing room and the composer action stays neutral', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.fill('#input', '/s #all claude login');
  await page.keyboard.press('Enter');

  await expect(page.locator('#filter-badge')).toHaveClass(/active/);
  await expect(page.locator('#search-bar')).toHaveClass(/active/);
  await expect(page.locator('#chip-search-btn')).not.toHaveClass(/active/);
  await expect(page.locator('#search-bar')).toHaveClass(/search-filtered/);
  await expect(page.locator('#chip-search-btn')).toHaveAttribute('aria-expanded', 'true');

  const spacing = await page.evaluate(() => {
    const bar = document.querySelector('#search-bar').getBoundingClientRect();
    return [...document.querySelectorAll('#search-bar input')].map(input => {
      const rect = input.getBoundingClientRect();
      return { top: rect.top - bar.top, bottom: bar.bottom - rect.bottom };
    });
  });
  for (const { top, bottom } of spacing) {
    expect(top).toBeGreaterThanOrEqual(4);
    expect(bottom).toBeGreaterThanOrEqual(4);
    expect(Math.abs(top - bottom)).toBeLessThanOrEqual(1);
  }

  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar')).not.toHaveClass(/active/);
  await expect(page.locator('#chip-search-btn')).not.toHaveClass(/active/);
  await expect(page.locator('#chip-search-btn')).toHaveAttribute('aria-expanded', 'false');
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

  await expect(page.locator('#search-bar-scope')).toHaveValue('#squid');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('needle');
  await expect(page.locator('#input')).toHaveValue('needle');
});

test('flow route filter and search use flow_route scope', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/history**');
  await page.unroute('**/search**');
  const historyUrls = [];
  const searchUrls = [];
  await page.route('**/history**', route => {
    historyUrls.push(route.request().url());
    return route.fulfill({ json: { items: [], has_more: false } });
  });
  await page.route('**/search**', route => {
    searchUrls.push(route.request().url());
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('/');

  await page.fill('#input', '/f #squid@codex>@revu');
  await page.keyboard.press('Enter');

  await expect(page.locator('#filter-badge')).toHaveClass(/active/);
  await expect(page.locator('#filter-badge-label')).toHaveText('#squid@codex>@revu');
  await expect(page.locator('#topic-chip')).toHaveClass(/route-chain/);
  await expect.poll(() => historyUrls.some(url => url.includes('flow_route=%23squid%40codex%3E%40revu'))).toBe(true);
  const flowHistoryUrl = historyUrls.find(url => url.includes('flow_route=%23squid%40codex%3E%40revu'));
  expect(flowHistoryUrl).not.toMatch(/[?&]topic=/);
  expect(flowHistoryUrl).not.toMatch(/[?&]agent=/);

  await page.fill('#input', '/s needle');
  await page.keyboard.press('Enter');

  await expect(page.locator('#search-bar')).toHaveClass(/active/);
  await expect.poll(() => searchUrls.some(url => url.includes('flow_route=%23squid%40codex%3E%40revu'))).toBe(true);
  const flowSearchUrl = searchUrls.find(url => url.includes('flow_route=%23squid%40codex%3E%40revu'));
  expect(flowSearchUrl).not.toMatch(/[?&]topic=/);
  expect(flowSearchUrl).not.toMatch(/[?&]agent=/);
});

test('unscoped global search shows all-history scope in its editor', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.fill('#input', '/s claude login');
  await page.keyboard.press('Enter');

  await expect(page.locator('#filter-badge')).not.toHaveClass(/active/);
  await expect(page.locator('#search-bar-keywords')).toHaveValue('claude login');

  await page.locator('#search-bar-keywords').click();
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('claude login');
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
  await expect(page.locator('#search-bar-scope')).toHaveValue('@claude!');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('needle');
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
  await expect(page.locator('#search-bar-scope')).toHaveValue('@claude-opus*');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('needle');
});

test('the visible scope editor replaces an all-history search scope', async ({ page }) => {
  await mockBackend(page);
  const urls = [];
  await page.route('**/search**', route => {
    urls.push(new URL(route.request().url()));
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('/');
  await page.fill('#input', '/s #all claude login');
  await page.keyboard.press('Enter');
  await expect(page.locator('#filter-badge')).not.toBeVisible();
  await page.fill('#search-bar-scope', '#squid');
  await page.locator('#search-bar-keywords').press('Enter');
  await expect.poll(() => urls.at(-1)?.searchParams.get('topic')).toBe('squid');
  await expect(page.locator('#search-bar-scope')).toHaveValue('#squid');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('claude login');
  await expect(page.locator('#filter-badge')).not.toBeVisible();
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
  await page.fill('#input', '/prompts');
  await page.keyboard.press('Enter');
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
  await page.route('**/history**', route => {
    const url = new URL(route.request().url());
    const bookmarked = url.searchParams.get('bookmarked') === 'true';
    return route.fulfill({ json: { items: bookmarked ? [
      { id: 42, role: 'assistant', topic: 'squid', agent: 'claude', prompt: 'Find it', content: 'Needle bookmark', status: 'done', adhoc: false, timestamp: new Date().toISOString() },
    ] : [], has_more: false } });
  });
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
  await expect(page.locator('#search-bar-scope')).toHaveValue('#squid');
  await expect(page.locator('#input')).toHaveValue('needle');
  await page.fill('#search-bar-keywords', 'needle');
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
  await page.route('**/history**', route => {
    const url = new URL(route.request().url());
    const bookmarked = url.searchParams.get('bookmarked') === 'true';
    return route.fulfill({ json: { items: bookmarked ? [
      { id: 42, role: 'assistant', topic: 'squid', agent: 'claude', prompt: 'Find it', content: 'Needle bookmark', status: 'done', adhoc: false, timestamp: new Date().toISOString() },
    ] : [], has_more: false } });
  });
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
  await expect(page.locator('#filter-badge-label')).toHaveText('#squidbookmarked');

  await page.fill('#input', 'needle');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar-scope')).toHaveValue('#squid');
  await expect(page.locator('#input')).toHaveValue('needle');
  await page.fill('#search-bar-keywords', 'needle');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant.search-result-item')).toHaveCount(1);

  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#filter-badge-label')).toHaveText('#squidbookmarked');
  await expect(page.locator('.filter-scope-agent')).toHaveCount(0);
  await expect(page.locator('.msg.assistant.history-item')).toHaveCount(1);
});

test('bookmark search clear does not restore filter removed during search', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/history**');
  await page.route('**/history**', route => {
    const url = new URL(route.request().url());
    const bookmarked = url.searchParams.get('bookmarked') === 'true';
    return route.fulfill({ json: { items: bookmarked ? [
      { id: 42, role: 'assistant', topic: 'squid', agent: 'codex', prompt: 'Find it', content: 'Needle bookmark', status: 'done', adhoc: false, timestamp: new Date().toISOString() },
    ] : [], has_more: false } });
  });
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
  await expect(page.locator('#filter-badge-label')).toHaveText('@codexbookmarked');

  await page.fill('#input', 'needle');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar-scope')).toHaveValue('@codex');
  await expect(page.locator('#input')).toHaveValue('needle');
  await page.fill('#search-bar-keywords', 'needle');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant.search-result-item')).toHaveCount(1);

  await page.locator('#chip-filter-btn').click();
  await expect(page.locator('#filter-badge-label')).toHaveText('bookmarked');
  await expect(page.locator('#chip-bookmark-btn')).toHaveAttribute('aria-pressed', 'true');

  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar')).not.toHaveClass(/active/);
  await expect(page.locator('#filter-badge-label')).toHaveText('bookmarked');
  await expect(page.locator('#chip-bookmark-btn')).toHaveAttribute('aria-pressed', 'true');
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

test('a completed live response is not added to the visible list while searching', async ({ page }) => {
  await mockBackend(page);

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

  await page.fill('#input', '/s needle');
  await page.keyboard.press('Enter');
  await fulfillChat(
    `event: meta\ndata: ${JSON.stringify({ agent: 'claude', backend: 'claude', msg_id: 1, adhoc: false })}\n\n` +
    `data:New response\n\n` +
    `event: done\ndata: \n\n`
  );

  await expect(page.locator('.msg.assistant:not(.msg-thinking)').filter({ hasText: 'New response' })).toHaveCount(0);
});

test('explicit #topic* search scope sends topic_subtree=true', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/search**');
  const searchUrls = [];
  await page.route('**/search**', route => {
    searchUrls.push(route.request().url());
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('/');

  await page.fill('#input', '/s #squid* needle');
  await page.keyboard.press('Enter');

  const last = searchUrls[searchUrls.length - 1];
  expect(last).toContain('topic=squid');
  expect(last).toContain('topic_subtree=true');
  await expect(page.locator('#filter-badge-label')).toContainText('#squid*');
});

test('mobile search stays on one row and preserves the draft through search and Escape', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await mockBackend(page);
  const urls = [];
  await page.route('**/search**', route => {
    urls.push(new URL(route.request().url()));
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('/');
  await page.fill('#input', '  unfinished draft  ');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar-keywords')).toBeFocused();
  await expect(page.locator('#input')).toHaveValue('  unfinished draft  ');
  const layout = await page.evaluate(() => {
    const scope = document.getElementById('search-bar-scope').getBoundingClientRect();
    const keywords = document.getElementById('search-bar-keywords').getBoundingClientRect();
    return { sameRow: scope.top === keywords.top, narrower: scope.width < keywords.width, fits: document.documentElement.scrollWidth <= innerWidth };
  });
  expect(layout).toEqual({ sameRow: true, narrower: true, fits: true });
  await page.fill('#search-bar-scope', '@claude!');
  await page.fill('#search-bar-keywords', 'needle');
  await page.locator('#search-bar-keywords').press('Enter');
  await expect.poll(() => urls.at(-1)?.searchParams.get('adhoc')).toBe('true');
  expect(urls.at(-1).searchParams.get('agent')).toBe('claude');
  await expect(page.locator('#input')).toHaveValue('  unfinished draft  ');
  await page.locator('#search-bar-keywords').press('Escape');
  await expect(page.locator('#search-bar')).not.toBeVisible();
  await expect(page.locator('#input')).toHaveValue('  unfinished draft  ');
});

test('search scope validates separately from keywords and supports routes and all history', async ({ page }) => {
  await mockBackend(page);
  const urls = [];
  await page.route('**/search**', route => {
    urls.push(new URL(route.request().url()));
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('/');
  await page.locator('#chip-search-btn').click();
  await page.fill('#search-bar-scope', 'invalid');
  await page.fill('#search-bar-keywords', 'needle');
  await page.locator('#search-bar-keywords').press('Enter');
  await expect(page.locator('#search-bar-scope')).toHaveAttribute('aria-invalid', 'true');
  expect(urls).toHaveLength(0);
  await page.fill('#search-bar-scope', '#squid@codex>@revu');
  await page.locator('#search-bar-scope').press('Enter');
  await expect.poll(() => urls.at(-1)?.searchParams.get('flow_route')).toBe('#squid@codex>@revu');
  await page.fill('#search-bar-scope', '#all');
  await page.locator('#search-bar-scope').press('Enter');
  await expect.poll(() => urls.length).toBe(2);
  expect(urls.at(-1).searchParams.has('flow_route')).toBe(false);
  expect(urls.at(-1).searchParams.has('topic')).toBe(false);
  await page.locator('#search-bar-clear').click();
  await page.locator('#chip-search-btn').click();
  await page.locator('#search-bar-keywords').press('Escape');
  await expect(page.locator('#search-bar')).not.toBeVisible();
  await expect(page.locator('#chip-search-btn')).toHaveAttribute('aria-expanded', 'false');
});

for (const clearAll of [false, true]) {
  test(`scope editor stays synchronized after ${clearAll ? 'clearing the filter' : 'editing out the agent scope'}`, async ({ page }) => {
    await mockBackend(page);
    const urls = [];
    await page.route('**/search**', route => {
      urls.push(new URL(route.request().url()));
      return route.fulfill({ json: { items: [] } });
    });
    await page.goto('/');
    if (clearAll) {
      await page.fill('#input', '/f #squid@claude');
      await page.locator('#input').press('Enter');
    }
    await page.fill('#input', '/s #squid@claude needle');
    await page.locator('#input').press('Enter');
    await page.fill('#search-bar-keywords', 'updated keywords');
    if (clearAll) await page.locator('#chip-filter-btn').click();
    else await page.fill('#search-bar-scope', '#squid');
    await expect(page.locator('#search-bar-scope')).toHaveValue(clearAll ? '#all' : '#squid');
    await expect(page.locator('#search-bar-keywords')).toHaveValue('updated keywords');
    await page.locator('#search-bar-keywords').press('Enter');
    await expect.poll(() => urls.at(-1)?.searchParams.get('q')).toBe('updated keywords');
    expect(urls.at(-1).searchParams.has('agent')).toBe(false);
    expect(urls.at(-1).searchParams.get('topic')).toBe(clearAll ? null : 'squid');
  });
}

test('spaced route scope is normalized without becoming search keywords', async ({ page }) => {
  await mockBackend(page);
  const urls = [];
  await page.route('**/search**', route => {
    urls.push(new URL(route.request().url()));
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('/');
  await page.locator('#chip-search-btn').click();
  await page.fill('#search-bar-scope', '#squid@codex > @revu');
  await page.fill('#search-bar-keywords', 'needle');
  await page.locator('#search-bar-scope').press('Enter');
  await expect.poll(() => urls.at(-1)?.searchParams.get('flow_route')).toBe('#squid@codex>@revu');
  expect(urls.at(-1).searchParams.get('q')).toBe('needle');
  expect(urls.at(-1).searchParams.has('topic')).toBe(false);
  expect(urls.at(-1).searchParams.has('agent')).toBe(false);
  await expect(page.locator('#search-bar-scope')).toHaveValue('#squid@codex>@revu');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('needle');
});

test('opening search prefills keywords from a routed draft without modifying it', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => clearTopicChip());
  await page.fill('#input', '#squid@claude draft keywords');
  // The composer moves the typed route into its sticky chip before search opens.
  await expect(page.locator('#input')).toHaveValue('draft keywords');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar-keywords')).toHaveValue('draft keywords');
  await expect(page.locator('#input')).toHaveValue('draft keywords');
  await expect(page.locator('#search-bar-scope')).toHaveValue('#squid@claude');
});

test('blank scope inherits the active filter and a filter change closes an unsubmitted panel', async ({ page }) => {
  await mockBackend(page);
  const urls = [];
  await page.route('**/search**', route => {
    urls.push(new URL(route.request().url()));
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('/');
  await page.fill('#input', '/f #squid@claude!');
  await page.locator('#input').press('Enter');
  await page.locator('#chip-search-btn').click();
  await page.fill('#search-bar-scope', '');
  await page.fill('#search-bar-keywords', 'needle');
  await page.locator('#search-bar-keywords').press('Enter');
  await expect.poll(() => urls.at(-1)?.searchParams.get('topic')).toBe('squid');
  expect(urls.at(-1).searchParams.get('agent')).toBe('claude');
  expect(urls.at(-1).searchParams.get('adhoc')).toBe('true');
  await expect(page.locator('.filter-scope-topic')).toHaveText('#squid');
  await page.locator('#search-bar-clear').click();
  await page.locator('#chip-search-btn').click();
  await page.fill('#input', '/f #other');
  await page.locator('#input').press('Enter');
  await expect(page.locator('#search-bar')).not.toBeVisible();
  await expect(page.locator('#chip-search-btn')).toHaveAttribute('aria-expanded', 'false');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar-scope')).toHaveValue('#other');
});

for (const selector of ['#chip-filter-btn', '#search-bar-clear']) {
  test(`unsubmitted search closes through ${selector}`, async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await page.fill('#input', '/f #squid@claude');
    await page.locator('#input').press('Enter');
    await page.locator('#chip-search-btn').click();
    await page.locator(selector).click();
    await expect(page.locator('#search-bar')).not.toBeVisible();
    await expect(page.locator('#chip-search-btn')).toHaveAttribute('aria-expanded', 'false');
  });
}

test('scope dropdown always offers Search all and only expands matches when typing', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/topics', r => r.fulfill({ json: [{ name: 'squid' }, { name: 'other' }] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [{ name: 'claude', backend: 'claude' }] }));
  const urls = [];
  await page.route('**/search**', route => {
    urls.push(new URL(route.request().url()));
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('/');
  await page.fill('#input', '/f #squid');
  await page.locator('#input').press('Enter');
  await page.locator('#chip-search-btn').click();
  await page.locator('#search-scope-toggle').click();
  await expect(page.locator('#search-scope-options')).toBeVisible();
  await expect(page.locator('#search-scope-options [role="option"]')).toHaveText(['Search all', 'This topic · #squid']);
  await page.fill('#search-bar-scope', '#oth');
  await expect(page.locator('#search-scope-options [role="option"]')).toHaveText(['Search all', '#other']);
  await page.locator('#search-scope-options').getByRole('option', { name: 'Search all', exact: true }).click();
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
  await expect(page.locator('#search-scope-options')).not.toBeVisible();
  await page.fill('#search-bar-keywords', 'needle');
  await page.locator('#search-bar-keywords').press('Enter');
  await expect.poll(() => urls.length).toBe(1);
  expect(urls[0].searchParams.has('topic')).toBe(false);
  expect(urls[0].searchParams.get('q')).toBe('needle');
});

test('mobile scope dropdown supports keyboard selection and dismissal', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await mockBackend(page);
  await page.goto('/');
  await page.locator('#chip-search-btn').click();
  await page.locator('#search-bar-scope').click();
  await expect(page.getByRole('option', { name: 'Search all', exact: true })).toBeVisible();
  await page.locator('#search-bar-scope').press('ArrowDown');
  await expect(page.getByRole('option', { name: 'Search all', exact: true })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
  await expect(page.locator('#search-bar-keywords')).toBeFocused();
  await page.locator('#search-scope-toggle').click();
  await page.locator('#search-scope-toggle').press('Escape');
  await expect(page.locator('#search-scope-options')).not.toBeVisible();
  await expect(page.locator('#search-bar')).toBeVisible();
});

for (const keywords of ['#other needle', '@claude needle']) {
  test(`blank scope preserves literal keywords: ${keywords}`, async ({ page }) => {
    await mockBackend(page);
    const urls = [];
    await page.route('**/search**', route => { urls.push(new URL(route.request().url())); return route.fulfill({ json: { items: [] } }); });
    await page.goto('/');
    await page.fill('#input', '/f #squid');
    await page.locator('#input').press('Enter');
    await page.locator('#chip-search-btn').click();
    await page.fill('#search-bar-scope', '');
    await page.fill('#search-bar-keywords', keywords);
    await page.locator('#search-bar-keywords').press('Enter');
    await expect.poll(() => urls.at(-1)?.searchParams.get('q')).toBe(keywords);
    expect(urls.at(-1).searchParams.get('topic')).toBe('squid');
    expect(urls.at(-1).searchParams.has('agent')).toBe(false);
  });
}

test('related scopes cover parent topics and agent lanes without duplicates', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.fill('#input', '/f #squid.ux@claude!');
  await page.locator('#input').press('Enter');
  await page.locator('#chip-search-btn').click();
  await page.locator('#search-scope-toggle').click();
  const options = page.locator('#search-scope-options [role="option"]');
  const values = await options.evaluateAll(nodes => nodes.map(n => n.dataset.scope));
  expect(values).toEqual(['#all', '#squid.ux@claude', '#squid.ux@claude!', '#squid.ux', '#squid*', '@claude', '@claude!', '@claude*']);
  expect(new Set(values).size).toBe(values.length);
  await page.getByRole('option', { name: 'Route ad hoc · #squid.ux@claude!', exact: true }).click();
  await expect(page.locator('#search-bar-scope')).toHaveValue('#squid.ux@claude!');
});

test('flow route suggestions retain the flow without agent permutations', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.fill('#input', '/f #squid@codex>@revu');
  await page.locator('#input').press('Enter');
  await page.locator('#chip-search-btn').click();
  await page.locator('#search-scope-toggle').click();
  await expect(page.locator('#search-scope-options [role="option"]')).toHaveText(['Search all', 'Route · #squid@codex>@revu', 'This topic · #squid']);
});

test('delayed suggestions keep the highlighted option focused', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.locator('#chip-search-btn').click();
  await page.evaluate(() => {
    _acTopics = () => new Promise(resolve => { window.finishSearchTopics = resolve; });
    _acAgents = async () => [];
  });
  await page.fill('#search-bar-scope', '#oth');
  await page.locator('#search-bar-scope').press('ArrowDown');
  const all = page.getByRole('option', { name: 'Search all', exact: true });
  await expect(all).toBeFocused();
  await page.evaluate(() => window.finishSearchTopics([{ name: 'other' }]));
  await expect(page.getByRole('option', { name: '#other', exact: true })).toBeVisible();
  await expect(all).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
  await expect(page.locator('#search-bar-keywords')).toBeFocused();
});

test('a later command search uses its own route for related options', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.fill('#input', '/f #first@claude');
  await page.locator('#input').press('Enter');
  await page.locator('#chip-search-btn').click();
  await page.locator('#search-bar-clear').click();
  await page.fill('#input', '/s #second@codex! needle');
  await page.locator('#input').press('Enter');
  await page.locator('#search-scope-toggle').click();
  await expect(page.getByRole('option', { name: 'Route ad hoc · #second@codex!', exact: true })).toBeVisible();
  await expect(page.locator('#search-scope-options')).not.toContainText('#first');
});

test('Search all keeps the original route choices available for narrowing again', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.fill('#input', '/f #squid@claude');
  await page.locator('#input').press('Enter');
  await page.locator('#chip-search-btn').click();
  await page.locator('#search-scope-toggle').click();
  await page.getByRole('option', { name: 'Search all', exact: true }).click();
  await page.fill('#search-bar-keywords', 'needle');
  await page.locator('#search-bar-keywords').press('Enter');
  await page.locator('#search-scope-toggle').click();
  await expect(page.getByRole('option', { name: 'Route · #squid@claude', exact: true })).toBeVisible();
});

for (const width of [1280, 375]) {
  test(`search button matches the composer icon and submits at width ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 812 });
    await mockBackend(page);
    const urls = [];
    await page.route('**/search**', route => { urls.push(new URL(route.request().url())); return route.fulfill({ json: { items: [] } }); });
    await page.goto('/');
    await page.fill('#input', 'unfinished draft');
    await page.locator('#chip-search-btn').click();
    const button = page.getByRole('button', { name: 'Run search', exact: true });
    await expect(button).toBeVisible();
    expect(await button.locator('svg').getAttribute('viewBox')).toBe(await page.locator('#chip-search-btn svg').getAttribute('viewBox'));
    expect(await button.locator('path').getAttribute('d')).toBe(await page.locator('#chip-search-btn path').getAttribute('d'));
    await page.fill('#search-bar-scope', '#all');
    await page.fill('#search-bar-keywords', 'needle');
    await button.click();
    await expect.poll(() => urls.at(-1)?.searchParams.get('q')).toBe('needle');
    await expect(page.locator('#input')).toHaveValue('unfinished draft');
    const layout = await page.evaluate(() => {
      const scope = document.getElementById('search-bar-scope').getBoundingClientRect();
      const button = document.getElementById('search-bar-submit').getBoundingClientRect();
      return { aligned: Math.abs((scope.top + scope.bottom) / 2 - (button.top + button.bottom) / 2) < 1, fits: document.documentElement.scrollWidth <= innerWidth };
    });
    expect(layout).toEqual({ aligned: true, fits: true });
    await page.fill('#search-bar-scope', 'invalid');
    await button.click();
    await expect(page.locator('#search-bar-scope')).toHaveAttribute('aria-invalid', 'true');
    expect(urls).toHaveLength(1);
  });
}

test('opening and submitting search keeps the composer action and bar neutral', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.fill('#input', '/f #squid');
  await page.locator('#input').press('Enter');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#chip-search-btn')).not.toHaveClass(/active/);
  await expect(page.locator('#search-bar')).toBeVisible();
  await expect(page.locator('#search-bar')).not.toHaveClass(/search-filtered/);
  const border = () => page.locator('#search-bar').evaluate(el => getComputedStyle(el).borderColor);
  const neutralBorder = await border();
  await page.fill('#search-bar-keywords', 'needle');
  await page.locator('#search-bar-submit').click();
  await expect(page.locator('#search-bar')).toHaveClass(/search-filtered/);
  await expect.poll(border).toBe(neutralBorder);
  await expect(page.locator('#chip-search-btn')).not.toHaveClass(/active/);
  await page.locator('#search-bar-clear').click();
  await expect(page.locator('#search-bar')).not.toHaveClass(/search-filtered/);
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar')).not.toHaveClass(/search-filtered/);
  await page.fill('#input', '/f #other');
  await page.locator('#input').press('Enter');
  await expect(page.locator('#chip-search-btn')).not.toHaveClass(/active/);
  await expect(page.locator('#search-bar')).not.toHaveClass(/search-filtered/);
});

for (const width of [1280, 375]) {
  test(`search submit highlights only once filtered at width ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 812 });
    await mockBackend(page);
    await page.goto('/');
    const composer = page.locator('#chip-search-btn');
    await composer.click();
    const button = page.locator('#search-bar-submit');
    const appearance = () => button.evaluate(el => {
      const style = getComputedStyle(el);
      return { color: style.color, border: style.borderColor };
    });
    await page.mouse.move(0, 0);
    await page.locator('#search-bar-keywords').focus();
    const neutral = await appearance();
    await expect(composer).toHaveAttribute('aria-expanded', 'true');
    await expect(composer).not.toHaveClass(/active/);
    await expect(page.locator('#filter-badge')).not.toBeVisible();
    await page.fill('#search-bar-keywords', 'needle');
    await button.click();
    await page.mouse.move(0, 0);
    await page.locator('#search-bar-keywords').focus();
    await expect(page.locator('#search-bar')).toHaveClass(/search-filtered/);
    await expect(composer).not.toHaveClass(/active/);
    await expect.poll(appearance).not.toEqual(neutral);
    await page.locator('#search-bar-clear').click();
    await expect(composer).toHaveAttribute('aria-expanded', 'false');
    await composer.click();
    await page.mouse.move(0, 0);
    await page.locator('#search-bar-keywords').focus();
    await expect.poll(appearance).toEqual(neutral);
  });
}

for (const suffix of ['', '!', '*']) {
  test(`route choices always offer session and ad hoc for original suffix '${suffix}'`, async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await page.fill('#input', `/f #squid.ux@claude${suffix}`);
    await page.locator('#input').press('Enter');
    await page.locator('#chip-search-btn').click();
    await page.locator('#search-scope-toggle').click();
    const options = page.locator('#search-scope-options [role="option"]');
    await expect(options).toHaveText([
      'Search all', 'Route · #squid.ux@claude', 'Route ad hoc · #squid.ux@claude!',
      'This topic · #squid.ux', 'Parent + subtopics · #squid*',
      'Agent · session · @claude', 'Agent · ad hoc · @claude!', 'Agent · both · @claude*',
    ]);
    await page.getByRole('option', { name: 'Parent + subtopics · #squid*', exact: true }).click();
    await expect(page.locator('#search-bar-scope')).toHaveValue('#squid*');
  });
}

test('search scope does not activate the history filter button', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  const filter = page.locator('#chip-filter-btn');
  await page.fill('#input', '/s #squid@claude needle');
  await page.locator('#input').press('Enter');
  await expect(filter).not.toHaveClass(/active/);
  await expect(filter).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('#search-bar')).toBeVisible();
  // A neutral filter action applies the composer filter rather than clearing search scope.
  await filter.click();
  await expect(filter).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#search-bar')).not.toBeVisible();
});

test('searching inside a history filter preserves its independent indicator and scope', async ({ page }) => {
  await mockBackend(page);
  const urls = [];
  await page.route('**/search**', route => { urls.push(new URL(route.request().url())); return route.fulfill({ json: { items: [] } }); });
  await page.goto('/');
  const filter = page.locator('#chip-filter-btn');
  await page.fill('#input', '/f #squid@claude!');
  await page.locator('#input').press('Enter');
  await page.locator('#chip-search-btn').click();
  await page.fill('#search-bar-keywords', 'needle');
  await page.locator('#search-bar-submit').click();
  await expect.poll(() => urls.at(-1)?.searchParams.get('topic')).toBe('squid');
  expect(urls.at(-1).searchParams.get('agent')).toBe('claude');
  expect(urls.at(-1).searchParams.get('adhoc')).toBe('true');
  await expect(filter).toHaveClass(/active/);
  await page.fill('#search-bar-scope', '#all');
  await page.locator('#search-bar-submit').click();
  await expect(filter).toHaveAttribute('aria-pressed', 'true');
  await page.locator('#search-bar-clear').click();
  await expect(filter).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#filter-badge-label')).toHaveText('#squid@claude!');
});

for (const width of [1280, 375]) {
  test(`search submit stays highlighted after searching and keywords avoid a native clear control at width ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 812 });
    await mockBackend(page);
    await page.goto('/');
    await page.locator('#chip-search-btn').click();
    const keywords = page.locator('#search-bar-keywords');
    await expect(keywords).toHaveAttribute('type', 'text');
    await expect(keywords).toHaveAttribute('enterkeyhint', 'search');
    await page.mouse.move(0, 0);
    const submit = page.locator('#search-bar-submit');
    const color = () => submit.evaluate(el => getComputedStyle(el).color);
    const neutral = await color();
    const accent = await page.evaluate(() => {
      const el = document.createElement('span');
      el.style.color = 'var(--accent)';
      document.getElementById('search-bar').append(el);
      const value = getComputedStyle(el).color;
      el.remove();
      return value;
    });
    expect(neutral).not.toBe(accent);
    await keywords.fill('needle');
    await keywords.press('Enter');
    await page.mouse.move(0, 0);
    await expect(page.locator('#search-bar')).toHaveClass(/search-filtered/);
    await expect(page.locator('#chip-search-btn')).not.toHaveClass(/active/);
    await expect.poll(color).toBe(accent);
    await submit.hover();
    await expect.poll(color).toBe(accent);
    await page.mouse.move(0, 0);
    await keywords.focus();
    await keywords.press('Tab');
    await expect(submit).toBeFocused();
    await expect.poll(color).toBe(accent);
    await keywords.focus();
    await expect.poll(color).toBe(accent);
    await expect(page.locator('#chip-search-btn')).not.toHaveAttribute('aria-pressed', 'true');
    await page.locator('#search-bar-clear').click();
    await page.locator('#chip-search-btn').click();
    await page.mouse.move(0, 0);
    await expect.poll(color).toBe(neutral);
  });
}
