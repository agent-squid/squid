const { test, expect } = require('@playwright/test');
const { installConnectionSocket } = require('./ws-health-mock');

test.use({ serviceWorkers: 'block' });

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
  await expect(page.locator('#chip-prompts-btn')).toHaveCount(0);
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

test('mobile composer grows upward beside the right-hand button stack', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 720 });
  await mockBackend(page);
  await page.goto('/');

  const input = page.locator('#input');
  const send = page.locator('#send-btn');
  const context = page.locator('#pin-btn');
  const initial = await input.boundingBox();
  const initialSend = await send.boundingBox();
  const initialContext = await context.boundingBox();
  const sendBottom = initialSend.y + initialSend.height;
  expect(initialContext.x).toBe(initialSend.x);
  expect(initialContext.y + initialContext.height).toBeLessThan(initialSend.y);
  expect(initial.x + initial.width).toBeLessThan(initialSend.x);

  await input.fill('A wrapped draft '.repeat(10));
  const wrapped = await input.boundingBox();
  expect(wrapped.height).toBeGreaterThan(initial.height);
  expect(wrapped.y).toBeLessThan(initial.y);
  expect(wrapped.width).toBe(initial.width);
  expect(await send.boundingBox()).toEqual(initialSend);
  expect(await context.boundingBox()).toEqual(initialContext);

  await input.fill('A line of the draft\n'.repeat(60));
  await page.setViewportSize({ width: 390, height: 430 });
  await expect.poll(async () => (await input.boundingBox()).height).toBeLessThanOrEqual(430 * 0.34);
  const capped = await input.boundingBox();
  const sendBox = await send.boundingBox();
  const contextBox = await context.boundingBox();
  expect(await input.evaluate(el => el.scrollHeight > el.clientHeight)).toBe(true);
  expect(sendBox.y + sendBox.height).toBeCloseTo(sendBottom - 290, 0);
  expect(contextBox.x).toBe(sendBox.x);
  expect(contextBox.y + contextBox.height).toBeLessThan(sendBox.y);
  expect(capped.x + capped.width).toBeLessThan(sendBox.x);

  await page.setViewportSize({ width: 390, height: 320 });
  await expect.poll(async () => (await send.boundingBox()).y).toBeCloseTo(sendBottom - 400 - initialSend.height, 0);
  const shortInput = await input.boundingBox();
  const shortSend = await send.boundingBox();
  expect(shortInput.x + shortInput.width).toBeLessThan(shortSend.x);
  expect(shortInput.height).toBeGreaterThanOrEqual(22);
  const shortContext = await context.boundingBox();
  expect(shortContext.x).toBe(shortSend.x);
  expect(shortContext.y + shortContext.height).toBeLessThan(shortSend.y);
  const pinPanel = page.locator('#pin-panel');
  await context.click();
  await expect(pinPanel).toBeVisible();
  const openPanel = await pinPanel.boundingBox();
  const sendWithPanel = await send.boundingBox();
  expect(openPanel.y + openPanel.height).toBeLessThan(shortContext.y);
  expect(sendWithPanel.y).toBe(shortSend.y);
  await send.click({ trial: true });
  await context.click();
  await expect(pinPanel).toBeHidden();

  await page.setViewportSize({ width: 390, height: 720 });
  await input.fill('');
  await expect.poll(async () => (await input.boundingBox()).height).toBe(initial.height);
});

for (const viewport of [
  { width: 390, height: 320 },
  { width: 390, height: 390 },
  { width: 844, height: 220 },
]) {
  test(`suggestions leave the draft and actions reachable at ${viewport.width} × ${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    await installConnectionSocket(page);
    await page.goto('/');
    await expect(page.locator('#connection-banner')).toBeHidden();

    const input = page.locator('#input');
    const send = page.locator('#send-btn');
    const context = page.locator('#pin-btn');
    const initialSend = await send.boundingBox();
    const initialDraft = await input.boundingBox();
    await input.fill('/');
    const suggestions = page.locator('#autocomplete');
    await expect(suggestions).toBeVisible();
    const draftBox = await input.boundingBox();
    const suggestionsBox = await suggestions.boundingBox();
    expect(suggestionsBox.y + suggestionsBox.height).toBeLessThan(draftBox.y);
    expect(draftBox.height).toBeGreaterThanOrEqual(22);
    await expect(suggestions.locator('.ac-item').last()).toBeInViewport();
    await input.click({ trial: true });
    const sendBox = await send.boundingBox();
    expect(sendBox.y).toBeCloseTo(initialSend.y, 0);
    expect(sendBox.y + sendBox.height).toBeLessThanOrEqual(viewport.height);
    await send.click({ trial: true });
    await context.click({ trial: true });
    await input.press('c');
    await suggestions.getByRole('button', { name: 'Close suggestions' }).click();
    await expect(suggestions).toBeHidden();
    await expect(page.locator('#messages')).toBeFocused();
    await expect(input).toHaveValue('/c');
    expect((await input.boundingBox()).height).toBeCloseTo(initialDraft.height, 0);
  });
}

test('connection loss leaves the composer reachable in a short landscape viewport', async ({ page }) => {
  const viewport = { width: 844, height: 220 };
  await page.setViewportSize(viewport);
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
  await page.routeWebSocket('**/ws/v1', socket => socket.close());
  await page.goto('/');

  const banner = page.locator('#connection-banner');
  await expect(banner).toBeVisible();
  await expect(banner).toHaveAttribute('data-state', 'lost');

  const input = page.locator('#input');
  const send = page.locator('#send-btn');
  const context = page.locator('#pin-btn');
  await input.fill('/');
  const suggestions = page.locator('#autocomplete');
  await expect(suggestions.locator('.ac-item').last()).toBeInViewport();
  const draft = await input.boundingBox();
  const sendBox = await send.boundingBox();
  expect(draft.height).toBeGreaterThanOrEqual(22);
  expect(draft.x + draft.width).toBeLessThan(sendBox.x);
  expect(sendBox.y + sendBox.height).toBeLessThanOrEqual(viewport.height);
  await input.click({ trial: true });
  await send.click({ trial: true });
  await context.click({ trial: true });
  await suggestions.getByRole('button', { name: 'Close suggestions' }).click();
  await banner.getByRole('button', { name: 'Connection help' }).click();
  await expect(page.locator('#connection-guide')).toBeVisible();
});

test('returning to chat remeasures the draft after resizing another view', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 760 });
  await mockBackend(page);
  await page.goto('/');

  const input = page.locator('#input');
  const draft = 'First line\nSecond line\nThird line\nFourth line';
  await input.fill(draft);
  const expanded = await input.boundingBox();

  await page.locator('.nav-tab[data-view="topics"]').click();
  await expect(input).toBeHidden();
  await page.setViewportSize({ width: 844, height: 700 });
  await expect(page.locator('body')).toHaveCSS('height', '700px');
  await page.locator('.nav-tab[data-view="chat"]').click();

  await expect(input).toHaveValue(draft);
  expect((await input.boundingBox()).height).toBe(expanded.height);
});

test.describe('touch composer', () => {
  test.use({ viewport: { width: 390, height: 720 }, isMobile: true, hasTouch: true });

  test('Return adds lines and Send submits the draft', async ({ page }) => {
    await mockBackend(page);
    await page.route('**/chat', route => route.fulfill({ json: {} }));
    await page.goto('/');

    const input = page.locator('#input');
    const initial = await input.boundingBox();
    await input.fill('First line');
    await input.press('Enter');
    await input.press('Enter');
    await page.keyboard.insertText('Third line');

    await expect(input).toHaveValue('First line\n\nThird line');
    expect((await input.boundingBox()).height).toBeGreaterThan(initial.height);

    const [request] = await Promise.all([
      page.waitForRequest(request => new URL(request.url()).pathname === '/chat' && request.method() === 'POST'),
      page.locator('#send-btn').tap(),
    ]);
    expect(request.postDataJSON().message).toBe('First line\n\nThird line');
    await expect(input).toHaveValue('');
  });
});

test('closing the keyboard restores draft height while suggestions remain open', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 720 });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
  await installConnectionSocket(page);
  await page.goto('/');

  const input = page.locator('#input');
  const suggestions = page.locator('#autocomplete');
  const initial = await input.boundingBox();

  // A soft keyboard can resize the visual viewport without resizing the window.
  const setVisualHeight = height => page.evaluate(value => {
    Object.defineProperty(window.visualViewport, 'height', { configurable: true, value });
    window.visualViewport.dispatchEvent(new Event('resize'));
  }, height);

  await setVisualHeight(320);
  await input.fill('/');
  await expect(suggestions).toBeVisible();
  await input.press('c');
  expect((await input.boundingBox()).height).toBeLessThan(initial.height);

  await setVisualHeight(720);
  await expect(suggestions).toBeVisible();
  await expect(input).toHaveValue('/c');
  expect((await input.boundingBox()).height).toBeCloseTo(initial.height, 0);
});

test('compact composer panels keep the transcript jump control out of the way', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 320 });
  await mockBackend(page);
  await page.route('**/history**', route => route.fulfill({
    json: {
      items: [{
        id: 1, role: 'assistant', topic: 'default', agent: 'claude',
        content: 'Transcript line\n\n'.repeat(50), prompt: 'Show the transcript',
        status: 'done', timestamp: '2026-11-21T12:00:00Z',
      }],
      has_more: false,
    },
  }));
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
  await installConnectionSocket(page);
  await page.goto('/');

  const messages = page.locator('#messages');
  const jump = page.locator('#scroll-btn');
  await expect(messages).toContainText('Transcript line');
  await expect.poll(() => messages.evaluate(el => el.scrollTop)).toBeGreaterThan(0);
  await messages.evaluate(el => { el.scrollTop = 0; });
  await expect(jump).toBeVisible();

  const context = page.locator('#pin-btn');
  await context.click();
  await expect(page.locator('#pin-panel')).toBeVisible();
  await expect(jump).toBeHidden();
  await context.click();
  await expect(jump).toBeVisible();

  await page.locator('#input').fill('/');
  const suggestions = page.locator('#autocomplete');
  await expect(suggestions).toBeVisible();
  await expect(jump).toBeHidden();
  await suggestions.getByRole('button', { name: 'Close suggestions' }).click();
  await expect(jump).toBeVisible();
});
