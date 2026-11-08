const { test, expect } = require('@playwright/test');
const activeScope = page => page.locator('#search-bar-scope');
async function editFilterScope(page, scope) {
  if (!await page.locator('#search-bar').isVisible()) await page.locator('#chip-search-btn').click();
  await page.fill('#search-bar-scope', scope);
  await page.locator('#search-bar-scope').press('Enter');
}


async function clearSearchKeywords(page) {
  if (!await page.locator('#search-bar').isVisible()) await page.locator('#chip-search-btn').click();
  await page.fill('#search-bar-keywords', '');
  await page.locator('#search-bar-keywords').press('Enter');
}

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

  await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);
  await expect(activeScope(page)).toHaveValue('#all');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('claude login');

  await page.locator('#search-bar-keywords').click();
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('claude login');
});

test('search inputs have breathing room and the composer action highlights applied search', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  await page.fill('#input', '/s #all claude login');
  await page.keyboard.press('Enter');

  await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);
  await expect(page.locator('#search-bar')).toHaveClass(/active/);
  await expect(page.locator('#chip-search-btn')).toHaveClass(/active/);
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
  }

  await page.locator('#search-bar-clear').click();
  await expect(page.locator('#search-bar')).not.toHaveClass(/active/);
  await expect(page.locator('#chip-search-btn')).toHaveClass(/active/);
  await expect(page.locator('#chip-search-btn')).toHaveAttribute('aria-expanded', 'false');
  await expect(page.locator('#input')).not.toHaveValue(/\/s #all claude login/);
});

test('search composer action inherits active filter scope over composer route', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');

  await page.fill('#input', '/f #squid@claude');
  await page.keyboard.press('Enter');
  await editFilterScope(page, '#squid');
  await expect(activeScope(page)).toHaveValue('#squid');
  await expect(activeScope(page)).not.toHaveValue(/@/);

  await page.fill('#input', 'needle');
  await page.locator('#chip-search-btn').click();

  await expect(page.locator('#search-bar-scope')).toHaveValue('#squid');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('');
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

  await expect(page.locator('#chip-filter-btn')).toHaveClass(/active/);
  await page.locator('#chip-search-btn').click();
  await expect(activeScope(page)).toHaveValue('#squid@codex>@revu');
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

  await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);
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
  await expect(activeScope(page)).not.toHaveValue(/#/);
  await expect(activeScope(page)).toHaveValue('@claude!');
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
  await expect(activeScope(page)).toHaveValue('@claude-opus*');

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

  await clearSearchKeywords(page);
  await expect(page.locator('#search-bar')).toHaveClass(/active/);
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
  await editFilterScope(page, '#squid');
  await expect(activeScope(page)).toHaveValue('#squid');
  await expect(activeScope(page)).not.toHaveValue(/@/);

  await page.fill('#input', '/s needle');
  await page.keyboard.press('Enter');
  await expect(page.locator('#search-bar')).toHaveClass(/active/);

  await clearSearchKeywords(page);
  await expect(page.locator('#search-bar')).toHaveClass(/active/);
  await expect(activeScope(page)).toHaveValue('#squid');
  await expect(activeScope(page)).not.toHaveValue(/@/);
});

test('search icon preserves composer draft and keyword clearing keeps the topic-only route', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/search**', route => route.fulfill({ json: { items: [] } }));

  await page.goto('/');
  await page.fill('#input', '/f #squid@claude');
  await page.keyboard.press('Enter');
  await editFilterScope(page, '#squid');
  await expect(activeScope(page)).toHaveValue('#squid');

  await page.fill('#input', 'needle');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar-scope')).toHaveValue('#squid');
  await expect(page.locator('#input')).toHaveValue('needle');
  await page.fill('#search-bar-keywords', 'needle');
  await page.keyboard.press('Enter');
  await expect(page.locator('#search-bar')).toHaveClass(/active/);

  await clearSearchKeywords(page);
  await expect(page.locator('#search-bar')).toHaveClass(/active/);
  await expect(activeScope(page)).toHaveValue('#squid');
  await expect(activeScope(page)).not.toHaveValue(/@/);
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
  await editFilterScope(page, '#squid');
  await page.locator('#chip-bookmark-btn').click();
  await expect(activeScope(page)).toHaveValue('#squid');

  await page.fill('#input', 'needle');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar-scope')).toHaveValue('#squid');
  await expect(page.locator('#input')).toHaveValue('needle');
  await page.fill('#search-bar-keywords', 'needle');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant.search-result-item')).toHaveCount(1);

  await clearSearchKeywords(page);
  await expect(activeScope(page)).toHaveValue('#squid');
  await expect(activeScope(page)).not.toHaveValue(/@/);
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
  await editFilterScope(page, '@codex');
  await expect(activeScope(page)).toHaveValue('@codex');

  await page.fill('#input', 'needle');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar-scope')).toHaveValue('@codex');
  await expect(page.locator('#input')).toHaveValue('needle');
  await page.fill('#search-bar-keywords', 'needle');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant.search-result-item')).toHaveCount(1);

  await page.locator('#chip-filter-btn').click();
  await expect(activeScope(page)).toHaveValue('#all');
  await expect(page.locator('#chip-bookmark-btn')).toHaveAttribute('aria-pressed', 'true');

  await clearSearchKeywords(page);
  await expect(activeScope(page)).toHaveValue('#all');
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

  await clearSearchKeywords(page);
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
  await expect(activeScope(page)).toHaveValue('#squid*');
});

test('mobile search wraps within the viewport and preserves the draft through search and Escape', async ({ page }) => {
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
    return { visible: scope.width > 0 && keywords.width > 0, fits: document.documentElement.scrollWidth <= innerWidth };
  });
  expect(layout).toEqual({ visible: true, fits: true });
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

test('opening search keeps routed composer drafts separate from history fields', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.evaluate(() => clearTopicChip());
  await page.fill('#input', '#squid@claude draft keywords');
  // The composer moves the typed route into its sticky chip before search opens.
  await expect(page.locator('#input')).toHaveValue('draft keywords');
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar-keywords')).toHaveValue('');
  await expect(page.locator('#input')).toHaveValue('draft keywords');
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
});

test('blank scope removes the active filter and a filter change closes an unsubmitted panel', async ({ page }) => {
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
  await expect.poll(() => urls.length).toBe(1);
  for (const key of ['topic', 'agent', 'adhoc']) expect(urls.at(-1).searchParams.has(key)).toBe(false);
  await expect(activeScope(page)).toHaveValue('#all');
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
  test(`unsubmitted panel responds to ${selector}`, async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await page.fill('#input', '/f #squid@claude');
    await page.locator('#input').press('Enter');
    await page.locator('#chip-search-btn').click();
    await page.locator(selector).click();
    if (selector === '#chip-filter-btn') {
      await expect(page.locator('#search-bar')).toBeVisible();
      await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
      await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);
    } else {
      await expect(page.locator('#search-bar')).not.toBeVisible();
      await expect(page.locator('#chip-search-btn')).toHaveAttribute('aria-expanded', 'false');
    }
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
  await expect(page.locator('#search-scope-options [role="option"]')).toHaveText(['Search all', 'This topic · #squid', 'This topic + subtopics · #squid*']);
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
    expect(urls.at(-1).searchParams.has('topic')).toBe(false);
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
  expect(values).toEqual(['#all', '#squid.ux@claude', '#squid.ux@claude!', '#squid.ux@claude*', '#squid.ux', '#squid.ux*', '#squid*', '@claude', '@claude!', '@claude*']);
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
  await expect(page.locator('#search-scope-options [role="option"]')).toHaveText(['Search all', 'Route · #squid@codex>@revu', 'This topic · #squid', 'This topic + subtopics · #squid*']);
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
      return { visible: scope.width > 0 && button.width > 0, fits: document.documentElement.scrollWidth <= innerWidth };
    });
    expect(layout).toEqual({ visible: true, fits: true });
    await page.fill('#search-bar-scope', 'invalid');
    await button.click();
    await expect(page.locator('#search-bar-scope')).toHaveAttribute('aria-invalid', 'true');
    expect(urls).toHaveLength(1);
  });
}

test('opening search stays neutral and submitting highlights the composer action', async ({ page }) => {
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
  await expect(page.locator('#chip-search-btn')).toHaveClass(/active/);
  await page.locator('#search-bar-clear').click();
  await expect(page.locator('#chip-search-btn')).toHaveClass(/active/);
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar')).toHaveClass(/search-filtered/);
  await page.fill('#input', '/f #other');
  await page.locator('#input').press('Enter');
  await expect(page.locator('#chip-search-btn')).not.toHaveClass(/active/);
  await expect(page.locator('#search-bar')).not.toHaveClass(/search-filtered/);
});

for (const width of [1280, 375]) {
  test(`composer search highlights only once filtered at width ${width}`, async ({ page }) => {
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
    await expect(composer).toHaveClass(/active/);
    await expect.poll(appearance).toEqual(neutral);
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
      'Search all', 'Route · #squid.ux@claude', 'Route ad hoc · #squid.ux@claude!', 'Route both lanes · #squid.ux@claude*',
      'This topic · #squid.ux', 'This topic + subtopics · #squid.ux*', 'Parent + subtopics · #squid*',
      'Agent · session · @claude', 'Agent · ad hoc · @claude!', 'Agent · both · @claude*',
    ]);
    await page.getByRole('option', { name: 'Parent + subtopics · #squid*', exact: true }).click();
    await expect(page.locator('#search-bar-scope')).toHaveValue('#squid*');
  });
}

test('scoped search highlights filter and clearing it preserves keywords', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  const filter = page.locator('#chip-filter-btn');
  await page.fill('#input', '/s #squid@claude needle');
  await page.locator('#input').press('Enter');
  await expect(filter).toHaveClass(/active/);
  await expect(filter).toHaveAttribute('aria-pressed', 'true');
  const request = page.waitForRequest(r => new URL(r.url()).pathname === '/search');
  await filter.click();
  const params = new URL((await request).url()).searchParams;
  expect(params.get('q')).toBe('needle');
  for (const key of ['topic', 'agent', 'adhoc', 'flow_route']) expect(params.has(key)).toBe(false);
  await expect(filter).not.toHaveClass(/active/);
  await expect(filter).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('#search-bar')).toBeVisible();
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('needle');
});

test('selecting all replaces the shared route without restoring an old filter', async ({ page }) => {
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
  await expect.poll(() => urls.at(-1)?.searchParams.has('topic')).toBe(false);
  for (const key of ['agent', 'adhoc', 'flow_route']) expect(urls.at(-1).searchParams.has(key)).toBe(false);
  expect(urls.at(-1).searchParams.get('q')).toBe('needle');
  await expect(filter).toHaveAttribute('aria-pressed', 'false');
  await page.locator('#search-bar-clear').click();
  await expect(page.locator('#search-bar')).not.toBeVisible();
  await expect(filter).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('#chip-search-btn')).toHaveClass(/active/);
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('needle');
});

for (const width of [1280, 375]) {
  test(`search submit stays neutral after searching and keywords avoid a native clear control at width ${width}`, async ({ page }) => {
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
    await expect(page.locator('#chip-search-btn')).toHaveClass(/active/);
    await expect.poll(color).toBe(neutral);
    await submit.hover();
    await expect.poll(color).toBe(neutral);
    await submit.click();
    await expect.poll(color).toBe(neutral);
    await expect(submit).toHaveCSS('outline-style', 'none');
    await page.mouse.move(0, 0);
    await keywords.focus();
    await keywords.press('Tab');
    await expect(submit).toBeFocused();
    await expect(submit).toHaveCSS('outline-style', 'solid');
    await expect.poll(color).toBe(neutral);
    await keywords.focus();
    await expect.poll(color).toBe(neutral);
    await expect(page.locator('#chip-search-btn')).not.toHaveAttribute('aria-pressed', 'true');
    await page.locator('#search-bar-clear').click();
    await page.locator('#chip-search-btn').click();
    await page.mouse.move(0, 0);
    await expect.poll(color).toBe(neutral);
  });
}

test('operator edits apply only after explicit search submission', async ({ page }) => {
  await mockBackend(page);
  const urls = [];
  await page.route('**/search**', route => { urls.push(new URL(route.request().url())); return route.fulfill({ json: { items: [] } }); });
  await page.goto('/');
  await page.fill('#input', '/s #all alpha beta');
  await page.keyboard.press('Enter');
  await expect(page.locator('#search-bar-operator')).toHaveAttribute('value', 'and');
  await expect.poll(() => urls.length).toBe(1);
  const request = page.waitForRequest(r => new URL(r.url()).pathname === '/search' && new URL(r.url()).searchParams.get('operator') === 'or');
  await page.click('#search-bar-operator'); await page.click('#search-operator-options [data-operator="or"]');
  await page.locator('#search-bar-keywords').focus();
  expect(urls).toHaveLength(1);
  await page.locator('#search-bar-submit').click();
  const params = new URL((await request).url()).searchParams;
  expect(params.get('q')).toBe('alpha beta');
  expect(params.has('topic')).toBe(false);
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('alpha beta');
  await expect(page.locator('#search-bar-operator')).toHaveAttribute('value', 'or');
});

for (const action of ['apply operator', 'clear keywords', 'replace search']) {
  test(`delayed search response is ignored after ${action}`, async ({ page }) => {
    await mockBackend(page);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let delayedRoute;
    await page.route('**/search**', route => {
      if (!delayedRoute) {
        delayedRoute = route;
        return;
      }
      return route.fulfill({ json: { items: [{
        id: 902, topic: 'squid', agent: 'codex', content: 'Current alpha beta result',
      }] } });
    });
    await page.goto('/');
    await page.fill('#input', '/s #all alpha beta');
    await page.keyboard.press('Enter');
    await expect.poll(() => !!delayedRoute).toBe(true);

    if (action === 'apply operator') {
      await page.click('#search-bar-operator'); await page.click('#search-operator-options [data-operator="or"]');
      await page.locator('#search-bar-submit').click();
    } else {
      await clearSearchKeywords(page);
      if (action === 'replace search') {
        await page.fill('#input', '/s #all alpha beta');
        await page.keyboard.press('Enter');
      }
    }
    if (action !== 'clear keywords') {
      await expect(page.locator('.search-result-item')).toHaveCount(1);
      await expect(page.locator('.search-result-item')).toContainText('Current alpha beta result');
    }

    const responsePromise = page.waitForResponse(r => r.request() === delayedRoute.request());
    await delayedRoute.fulfill({ json: { items: [{
      id: 901, topic: 'squid', agent: 'codex', content: 'Stale alpha beta result',
    }] } });
    await (await responsePromise).finished();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));

    await expect(page.locator('.search-result-item')).toHaveCount(action === 'clear keywords' ? 0 : 1);
    await expect(page.locator('#messages')).not.toContainText('Stale alpha beta result');
    expect(errors).toEqual([]);
  });
}

for (const kind of ['filter', 'search']) {
  for (const topic of ['squid', 'squid.ux']) {
    test(`${kind} offers topic and subtree choices for ${topic}`, async ({ page }) => {
      await mockBackend(page);
      await page.goto('/');
      await page.fill('#input', kind === 'filter' ? `/f #${topic}` : `/s #${topic} needle`);
      await page.locator('#input').press('Enter');
      if (kind === 'filter') await page.locator('#chip-search-btn').click();
      await page.locator('#search-scope-toggle').click();
      const options = page.locator('#search-scope-options');
      await expect(options.locator(`[data-scope="#${topic}"]`)).toHaveText(`This topic · #${topic}`);
      await expect(options.locator(`[data-scope="#${topic}*"]`)).toHaveText(`This topic + subtopics · #${topic}*`);
      if (topic.includes('.')) {
        await expect(options.locator('[data-scope="#squid*"]')).toHaveText('Parent + subtopics · #squid*');
      }
      const request = page.waitForRequest(r => {
        const url = new URL(r.url());
        return url.pathname === (kind === 'filter' ? '/history' : '/search') &&
          url.searchParams.get('topic') === topic && url.searchParams.get('topic_subtree') === 'true';
      });
      await options.locator(`[data-scope="#${topic}*"]`).click();
      await page.locator('#search-bar-submit').click();
      await request;
      await expect(page.locator('#search-bar-scope')).toHaveValue(`#${topic}*`);
    });
  }
}

test('closing the panel preserves a pending search and reopening shows its results', async ({ page }) => {
  await mockBackend(page);
  let pending;
  await page.route('**/search**', route => { pending = route; });
  await page.goto('/');
  await page.fill('#input', '/s #all needle');
  await page.locator('#input').press('Enter');
  await expect.poll(() => !!pending).toBe(true);
  await page.locator('#search-bar-clear').click();
  await expect(page.locator('#search-bar')).not.toBeVisible();
  await expect(page.locator('#chip-search-btn')).toHaveClass(/active/);
  const response = page.waitForResponse(r => r.request() === pending.request());
  await pending.fulfill({ json: { items: [{ id: 903, topic: 'squid', agent: 'codex', content: 'Preserved needle result' }] } });
  await (await response).finished();
  await expect(page.locator('.search-result-item')).toHaveCount(1);
  await expect(page.locator('.search-result-item')).toContainText('Preserved needle result');
  await expect(page.locator('#search-bar')).not.toBeVisible();
  await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar-keywords')).toHaveValue('needle');
  await expect(page.locator('.search-result-item')).toHaveCount(1);
});
