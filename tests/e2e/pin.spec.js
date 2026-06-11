/**
 * Pin basket contract tests.
 * Verifies the core feature invariants — not implementation details.
 */
const { test, expect } = require('@playwright/test');

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
const STATS = { event: 'stats', data: { session_id: 'test-sess-abc', input_tokens: 10, output_tokens: 5 } };
const DONE  = { event: 'done',  data: '' };
const MEMORY_WITH_SKIP = '---\nsquid:\n  code_roots_skipped: true\n---\nPrefer transparent context.';

async function mockBackend(page, { agent = 'claude', topic = 'squid' } = {}) {
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/history**',     r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**',       r => r.fulfill({ json: {} }));
  await page.route('**/topics',        r => r.fulfill({ json: [
    { name: topic, agent, last_model: null, last_backend: 'claude', queue_depth: 0, active: false, last_prompt: 'hi' }
  ]}));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic, exists: true, content: '---\nsquid:\n  code_roots_skipped: true\n---\n', path: `context/topics/${topic}/memory.md`,
    squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
  }}));
  await page.route('**/topics/**',     r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));
}

async function seedPin(page, item) {
  await page.evaluate(item => {
    const existing = JSON.parse(localStorage.getItem('pinnedItems') || '[]');
    existing.push(item);
    localStorage.setItem('pinnedItems', JSON.stringify(existing));
  }, item);
}

// ── tests ─────────────────────────────────────────────────────────────────────

test('bookmark button on bubble adds item to pin panel', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse(META, { data: 'Hello from agent' }, DONE),
  }));

  await page.goto('/');
  await page.fill('#input', '#squid@claude hello');
  await page.keyboard.press('Enter');

  // Wait for response bubble
  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();

  // Click the bookmark button on the bubble
  const bubble = page.locator('.msg.assistant:not(.msg-thinking)');
  await bubble.hover();
  await bubble.locator('.msg-pin-btn').click();

  // Open pin panel — should show the bookmarked item
  await page.click('#pin-btn');
  await expect(page.locator('#pin-panel.open')).toBeVisible();
  await expect(page.locator('.pin-item')).toHaveCount(1);
  await expect(page.locator('.pin-item-preview')).toContainText('Hello from agent');
});

test('pinned item from current session shows in-session skip', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse(META, { data: 'Hello from agent' }, STATS, DONE),
  }));

  await page.goto('/');
  await page.fill('#input', '#squid@claude hello');
  await page.keyboard.press('Enter');

  // Bookmark the response — session_id from STATS gets stored in the bookmark
  const bubble = page.locator('.msg.assistant:not(.msg-thinking)');
  await expect(bubble).toBeVisible();
  await bubble.hover();
  await bubble.locator('.msg-pin-btn').click();

  await page.click('#pin-btn');
  await expect(page.locator('#pin-panel.open')).toBeVisible();
  await expect(page.locator('.pin-item-status')).toContainText('in session');
  await expect(page.locator('.pin-item-status')).toContainText('skip');
  await expect(page.locator('.pin-item-status')).toContainText('claude');
});

test('/clear invalidates cached session id so same-session bookmark can inject', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse(META, { data: 'Hello from agent' }, STATS, DONE),
  }));
  await page.route('**/cmd', r => r.fulfill({ json: { ok: true, agent: 'claude' } }));

  await page.goto('/');
  await page.fill('#input', '#squid@claude hello');
  await page.keyboard.press('Enter');

  const bubble = page.locator('.msg.assistant:not(.msg-thinking)');
  await expect(bubble).toBeVisible();
  await bubble.hover();
  await bubble.locator('.msg-pin-btn').click();

  await page.click('#pin-btn');
  await expect(page.locator('.pin-item-status')).toContainText('in session');

  await page.fill('#input', '#squid@claude /clear');
  await page.keyboard.press('Enter');

  await expect(page.locator('.pin-item-status')).toContainText('will inject');
});

test('pinned item from same topic@agent shows will-inject for adhoc turn', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });

  await page.goto('/');
  await seedPin(page, { id: 42, topic: 'squid', agent: 'claude', content: 'cached response' });

  // Adhoc input — same topic@agent but ! flag
  await page.fill('#input', '#squid@claude! hello');

  await page.click('#pin-btn');
  await expect(page.locator('#pin-panel.open')).toBeVisible();
  await expect(page.locator('.pin-item-status')).toContainText('will inject');
});

test('session send includes pinned_ids for cross-topic bookmarks', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });

  let capturedBody = null;
  await page.route('**/chat', async route => {
    capturedBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'session response' }, DONE),
    });
  });

  await page.goto('/');
  // Bookmark from a different topic — should be injected into session turn
  await seedPin(page, { id: 77, topic: 'other', agent: 'codex', content: 'cross-topic context' });

  await page.fill('#input', '#squid@claude hello');
  await page.keyboard.press('Enter');

  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();
  expect(capturedBody?.pinned_ids).toContain(77);
  expect(capturedBody?.adhoc).toBeFalsy();
});

test('adhoc send includes pinned_ids in POST /chat body', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });

  let capturedBody = null;
  await page.route('**/chat', async route => {
    capturedBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        { event: 'meta', data: { agent: 'claude', backend: 'claude', msg_id: 2, adhoc: true } },
        { data: 'response' },
        DONE,
      ),
    });
  });

  await page.goto('/');
  // Seed a pin from a different topic so it passes the "will inject" filter
  await seedPin(page, { id: 99, topic: 'other', agent: 'claude', content: 'other context' });

  await page.fill('#input', '#squid@claude! tell me something');
  await page.keyboard.press('Enter');

  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();
  expect(capturedBody?.pinned_ids).toContain(99);
});

test('fresh session send includes topic memory when memory exists', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: true, content: MEMORY_WITH_SKIP, path: 'context/topics/squid/memory.md',
      squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
    },
  }));
  await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({
    json: { session_id: null, cwd: null },
  }));

  let capturedBody = null;
  await page.route('**/chat', async route => {
    capturedBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'session response' }, DONE),
    });
  });

  await page.goto('/');
  await page.fill('#input', '#Squid@claude hello');
  await page.keyboard.press('Enter');

  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();
  expect(capturedBody?.topic).toBe('squid');
  expect(capturedBody?.include_topic_memory).toBe(true);
});

test('/clear memory injects once and is unselected immediately after send', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: true, content: MEMORY_WITH_SKIP, path: 'context/topics/squid/memory.md',
      squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
    },
  }));
  await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({
    json: { session_id: null, cwd: null },
  }));
  await page.route('**/cmd', r => r.fulfill({ json: { ok: true, agent: 'claude' } }));

  let capturedBody = null;
  await page.route('**/chat', async route => {
    capturedBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'session response' }, DONE),
    });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude /clear');
  await page.keyboard.press('Enter');

  await page.click('#pin-btn');
  await expect(page.locator('.memory-item-status')).toContainText('will inject');
  await page.click('#pin-btn');

  await page.fill('#input', '#squid@claude hello');
  await page.keyboard.press('Enter');

  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();
  expect(capturedBody?.include_topic_memory).toBe(true);

  await page.click('#pin-btn');
  await expect(page.locator('.memory-item-status')).toContainText('in session');
  await expect(page.locator('[data-memory-toggle]')).toHaveText('Off');

  await page.click('#pin-btn');
  await page.fill('#input', '#squid@claude next');
  await page.click('#pin-btn');
  await expect(page.locator('.memory-item-status')).toContainText('in session');
  await expect(page.locator('[data-memory-toggle]')).toHaveText('Off');
});

test('topic memory editor saves and refreshes preview', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({
    json: { session_id: null, cwd: null },
  }));

  let savedContent = '';
  await page.route('**/topics/squid/memory', async route => {
    if (route.request().method() === 'PUT') {
      savedContent = route.request().postDataJSON().content;
      await route.fulfill({
        json: { topic: 'squid', exists: true, content: savedContent, path: 'context/topics/squid/memory.md' },
      });
      return;
    }
    await route.fulfill({
      json: { topic: 'squid', exists: !!savedContent, content: savedContent, path: 'context/topics/squid/memory.md' },
    });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude hello');
  await page.click('#pin-btn');
  await page.locator('[data-memory-toggle]').click();

  await expect(page.locator('#memory-modal.open')).toBeVisible();
  await page.fill('#memory-editor', 'Prefer transparent context.');
  await page.click('#memory-save');

  await expect(page.locator('#memory-save')).toHaveText('Saved');
  await expect(page.locator('#memory-path')).toContainText('saved');
  expect(savedContent).toBe('Prefer transparent context.');

  await page.click('#memory-modal-close');
  await page.click('#pin-btn');
  await expect(page.locator('.memory-item-preview')).toContainText('Prefer transparent context.');
});

test('active session skips topic memory by default', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: true, content: MEMORY_WITH_SKIP, path: 'context/topics/squid/memory.md',
      squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
    },
  }));
  await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({
    json: { session_id: 'active-session', cwd: '/tmp/squid' },
  }));

  let capturedBody = null;
  await page.route('**/chat', async route => {
    capturedBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'session response' }, DONE),
    });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude hello');
  await page.keyboard.press('Enter');

  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();
  expect(capturedBody?.include_topic_memory).toBeUndefined();
});
