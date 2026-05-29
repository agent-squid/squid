const { test, expect } = require('@playwright/test');

// ── SSE helpers ───────────────────────────────────────────────────────────────

const SSE_HEADERS = {
  'Content-Type': 'text/event-stream',
  'Cache-Control': 'no-cache',
  'X-Accel-Buffering': 'no',
};

function sse(...events) {
  return events.map(({ event, data }) => {
    const str = typeof data === 'string' ? data : JSON.stringify(data);
    return event ? `event: ${event}\ndata: ${str}\n\n` : `data: ${str}\n\n`;
  }).join('');
}

const META  = { event: 'meta',  data: { agent: 'claude', backend: 'claude', msg_id: 1, adhoc: false } };
const STATS = { event: 'stats', data: { session_id: 'test-sid', input_tokens: 10, output_tokens: 5, adhoc: false, lookback: 0 } };
const DONE  = { event: 'done',  data: '' };

// ── mock setup ────────────────────────────────────────────────────────────────

async function mockBackend(page) {
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok', boot_time: new Date().toISOString() } }));
  await page.route('**/history**',     r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota',         r => r.fulfill({ json: {} }));
  await page.route('**/topics',        r => r.fulfill({ json: [] }));
  await page.route('**/topics/**',     r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));
}

// Returns { intercepted, fulfill } — intercepted resolves when the /chat
// request is received; calling fulfill(body) sends the SSE response.
function holdChat(page) {
  let _fulfill;
  const intercepted = new Promise(resolve => {
    page.route('**/chat', route => {
      _fulfill = body => route.fulfill({ status: 200, headers: SSE_HEADERS, body });
      resolve();
    });
  });
  return { intercepted, fulfill: body => _fulfill(body) };
}

async function sendMsg(page, text = 'hello') {
  await page.fill('#input', text);
  await page.keyboard.press('Enter');
}

// Pause so you can see what's on screen before moving on
const look = (page, ms = 2500) => page.waitForTimeout(ms);

// ── selectors ─────────────────────────────────────────────────────────────────

const THINKING  = '.msg.assistant.msg-thinking';
const RESPONSE  = '.msg.assistant:not(.msg-thinking)';
const MSG_ERROR = '.msg-error';

// ── tests ─────────────────────────────────────────────────────────────────────

test.describe('response bubble', () => {
  test.beforeEach(async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
  });

  test('does not appear in DOM before done', async ({ page }) => {
    const { intercepted, fulfill } = holdChat(page);

    await sendMsg(page);
    await intercepted;

    // ── LOOK: thinking bubble visible, response bubble absent ────────────────
    await expect(page.locator(THINKING)).toBeVisible();
    await expect(page.locator(RESPONSE)).not.toBeAttached();
    await look(page);  // pause — observe: only thinking bubble, no response bubble yet

    await fulfill(sse(META, { data: 'Hello!' }, DONE));

    await expect(page.locator(RESPONSE)).toBeVisible();
    await look(page);  // pause — observe: response bubble now at bottom
  });

  test('appears at bottom of #messages on done', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Response text' }, STATS, DONE),
    }));

    await sendMsg(page);
    await expect(page.locator(RESPONSE)).toBeVisible();

    const last = page.locator('#messages > *').last();
    await expect(last).toHaveClass(/stats/);
    await look(page);  // pause — observe: stats line is last child, bubble above it
  });

  test('content is markdown-rendered in final bubble', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: '**bold** and `code`' }, DONE),
    }));

    await sendMsg(page);
    const bubble = page.locator(RESPONSE);
    await expect(bubble.locator('strong')).toHaveText('bold');
    await expect(bubble.locator('code')).toHaveText('code');
    await look(page);  // pause — observe: bold and inline code rendered in bubble
  });

  test('thinking bubble collapses to toggle when status events present', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { event: 'status', data: 'Thinking...' }, { data: 'Result' }, DONE),
    }));

    await sendMsg(page);
    await expect(page.locator(RESPONSE)).toBeVisible();
    await expect(page.locator('.msg-thinking-done')).toBeVisible();
    await expect(page.locator('.thinking-toggle')).toBeVisible();
    await look(page);  // pause — observe: collapsed ▸ toggle above the response bubble
  });

  test('thinking bubble removed when no status events', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Hello' }, DONE),
    }));

    await sendMsg(page);
    await expect(page.locator(RESPONSE)).toBeVisible();
    await expect(page.locator(THINKING)).not.toBeAttached();
    await look(page);  // pause — observe: only response bubble, thinking bubble gone
  });

  test('error appears at bottom in bubble', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { event: 'error', data: 'Backend unavailable' }),
    }));

    await sendMsg(page);
    const errorBubble = page.locator(RESPONSE);
    await expect(errorBubble).toBeVisible();
    await expect(errorBubble.locator(MSG_ERROR)).toContainText('Backend unavailable');
    const last = page.locator('#messages > *').last();
    await expect(last).toHaveClass(/assistant/);
    await look(page);  // pause — observe: error message in bubble at bottom
  });
});

test.describe('parallel responses', () => {
  test.beforeEach(async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
  });

  test('two concurrent responses both land at bottom without early bubble insertion', async ({ page }) => {
    const routes = [];
    const bothIntercepted = new Promise(resolve => {
      page.route('**/chat', route => {
        routes.push(route);
        if (routes.length === 2) resolve();
      });
    });

    await sendMsg(page, '#a hello');
    await sendMsg(page, '#b world');
    await bothIntercepted;

    // ── LOOK: both requests in flight, no response bubbles yet ───────────────
    await expect(page.locator(RESPONSE)).not.toBeAttached();
    await look(page);  // pause — observe: two thinking bubbles, zero response bubbles

    await routes[1].fulfill({ status: 200, headers: SSE_HEADERS, body: sse(META, { data: 'Second done first' }, DONE) });
    await expect(page.locator(RESPONSE)).toHaveCount(1);
    await look(page);  // pause — observe: one bubble at bottom, other still thinking

    await routes[0].fulfill({ status: 200, headers: SSE_HEADERS, body: sse(META, { data: 'First done second' }, DONE) });
    await expect(page.locator(RESPONSE)).toHaveCount(2);
    await look(page);  // pause — observe: both bubbles at bottom in completion order
  });
});
