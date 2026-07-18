const { test, expect } = require('@playwright/test');

async function mockBackend(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/topics', r => r.fulfill({ json: [
    { name: 'default', agent: null, last_model: null, last_backend: 'claude', queue_depth: 0, active: false, last_prompt: '' }
  ] }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic: 'default', exists: true, content: '',
    squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
  }}));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/bookmarks**', r => r.fulfill({ json: { items: [] } }));
}

test('composer tool buttons have correct initial tooltips with slash-command hints', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.waitForSelector('#chip-bookmark-btn');

  await expect(page.locator('#chip-bookmark-btn')).toHaveAttribute('title', 'bookmarks only: /bm');
  await expect(page.locator('#chip-prompts-btn')).toHaveAttribute('title', 'prompts only: /prompts');
  await expect(page.locator('#chip-filter-btn')).toHaveAttribute('title', 'filter topic: /f #default');
  await expect(page.locator('#chip-search-btn')).toHaveAttribute('title', 'search: /s #default kw1 kw2…');
  await expect(page.locator('#chip-clear-btn')).toHaveAttribute('title', 'clear context: /clear');
});

test('bookmark button tooltip toggles between on/off states', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.waitForSelector('#chip-bookmark-btn');

  await expect(page.locator('#chip-bookmark-btn')).toHaveAttribute('title', 'bookmarks only: /bm');

  await page.locator('#chip-bookmark-btn').click();
  await expect(page.locator('#chip-bookmark-btn')).toHaveAttribute('title', 'show full thread: /bm');

  await page.locator('#chip-bookmark-btn').click();
  await expect(page.locator('#chip-bookmark-btn')).toHaveAttribute('title', 'bookmarks only: /bm');
});

test('prompts button tooltip toggles between on/off states', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.waitForSelector('#chip-prompts-btn');

  await expect(page.locator('#chip-prompts-btn')).toHaveAttribute('title', 'prompts only: /prompts');

  await page.locator('#chip-prompts-btn').click();
  await expect(page.locator('#chip-prompts-btn')).toHaveAttribute('title', 'show full thread: /prompts');

  await page.locator('#chip-prompts-btn').click();
  await expect(page.locator('#chip-prompts-btn')).toHaveAttribute('title', 'prompts only: /prompts');
});

test('dynamic filter tooltip updates with scope after topic chip set', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.waitForSelector('#chip-filter-btn');

  // Type a topic into the input and switch chip
  await page.fill('#input', '#squid');
  await page.keyboard.press('Enter');

  // Wait for the chip to become visible
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);

  await expect(page.locator('#chip-filter-btn')).toHaveAttribute('title', 'filter topic: /f #squid');
  await expect(page.locator('#chip-search-btn')).toHaveAttribute('title', 'search: /s #squid kw1 kw2…');
  await expect(page.locator('#chip-clear-btn')).toHaveAttribute('title', 'clear context: /clear');
});