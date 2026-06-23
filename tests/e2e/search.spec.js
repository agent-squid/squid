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
  await expect(result.locator('.user-ctx')).toContainText('#42 · ctx:');
  await expect(result.locator('mark.search-kw-highlight')).toHaveText(['Needle', 'search', 'needle', 'search', 'needle', 'search']);
  await expect(result.locator('.history-prompt mark.search-kw-highlight')).toHaveCount(0);
  await expect(result.locator('code:not(pre code) mark.search-kw-highlight')).toHaveText(['needle', 'search']);
  await expect(result.locator('pre mark.search-kw-highlight')).toHaveText(['needle', 'search']);
});
