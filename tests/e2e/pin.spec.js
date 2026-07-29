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
    topic, exists: true, content: '---\nsquid:\n  code_roots_skipped: true\n---\n', path: `~/.squid/context/topics/${topic}/memory.md`,
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

async function seedAttachedFile(page, path) {
  await page.evaluate(path => {
    localStorage.setItem('attachedFiles', JSON.stringify([{
      path,
      name: path.split('/').filter(Boolean).pop() || path,
    }]));
    updatePinCount();
  }, path);
}

// ── tests ─────────────────────────────────────────────────────────────────────

test('pin button on bubble adds item to pin panel', async ({ page }) => {
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

  // Click the pin button on the bubble
  const bubble = page.locator('.msg.assistant:not(.msg-thinking)');
  await expect(bubble.locator('.msg-pin-btn svg')).toHaveCSS('width', '16px');
  await expect(bubble.locator('.msg-pin-btn svg')).toHaveCSS('height', '16px');
  await bubble.hover();
  await bubble.locator('.msg-pin-btn').click();
  await expect(bubble).toHaveClass(/pinned-sel/);

  // Open pin panel — should show the pinned item
  await page.click('#pin-btn');
  await expect(page.locator('#pin-panel.open')).toBeVisible();
  await expect(page.locator('.pin-item')).toHaveCount(1);
  await expect(page.locator('.pin-item-preview')).toContainText('Hello from agent');

  await page.locator('.pin-item-remove').click();
  await expect(page.locator('.pin-item')).toHaveCount(0);
  await expect(bubble).not.toHaveClass(/pinned-sel/);
});

test('escape closes pin panel and returns focus to composer', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.goto('/');
  await seedPin(page, { id: 12, topic: 'squid', agent: 'claude', session_id: 'other-session', content: 'Pinned context' });

  await page.click('#pin-btn');
  await expect(page.locator('#pin-panel.open')).toBeVisible();

  await page.keyboard.press('Escape');

  await expect(page.locator('#pin-panel.open')).not.toBeVisible();
  await expect(page.locator('#input')).toBeFocused();
});

test('clear pins unselects pinned responses from pin panel', async ({ page }) => {
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
  await seedPin(page, { id: 42, topic: 'other', agent: 'claude', content: 'first selected response' });
  await seedPin(page, { id: 43, topic: 'notes', agent: 'codex', content: 'second selected response' });

  await page.click('#pin-btn');
  await expect(page.locator('#pin-panel-clear')).toBeEnabled();
  await expect(page.locator('.pin-item')).toHaveCount(2);

  await page.click('#pin-panel-clear');
  await expect(page.locator('.pin-item')).toHaveCount(0);
  await expect(page.locator('#pin-panel-clear')).toBeDisabled();
  await expect(page.locator('.memory-item')).toBeVisible();
  const storedPins = await page.evaluate(() => JSON.parse(localStorage.getItem('pinnedItems') || '[]'));
  expect(storedPins).toEqual([]);

  await page.fill('#input', '#squid@claude hello');
  await page.keyboard.press('Enter');

  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();
  expect(capturedBody?.pinned_ids).toBeUndefined();
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

  // Pin the response — session_id from STATS gets stored in the pin
  const bubble = page.locator('.msg.assistant:not(.msg-thinking)');
  await expect(bubble).toBeVisible();
  await bubble.hover();
  await bubble.locator('.msg-pin-btn').click();

  await page.click('#pin-btn');
  await expect(page.locator('#pin-panel.open')).toBeVisible();
  await expect(page.locator('.pin-item-status')).toContainText('in session');
  await expect(page.locator('.pin-item-status')).toContainText('skip');
  await expect(page.locator('.pin-item-status')).not.toContainText('claude');
  await expect(page.locator('#pin-count')).toHaveText('1');
  await expect(page.locator('#pin-btn')).toHaveClass(/has-saved-pins/);
});

test('/clear invalidates cached session id so same-session pin can inject', async ({ page }) => {
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

test('session send includes pinned_ids for cross-topic pins', async ({ page }) => {
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
  // Pin from a different topic — should be injected into session turn
  await seedPin(page, { id: 77, topic: 'other', agent: 'codex', content: 'cross-topic context' });

  await page.fill('#input', '#squid@claude hello');
  await page.keyboard.press('Enter');

  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();
  expect(capturedBody?.pinned_ids).toContain(77);
  expect(capturedBody?.adhoc).toBeFalsy();
});

test('session send clears the will-inject pin badge once context is delivered', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse(META, { data: 'session response' }, STATS, DONE),
  }));

  await page.goto('/');
  // Pin from a different topic — should be injected into session turn
  await seedPin(page, { id: 77, topic: 'other', agent: 'codex', content: 'cross-topic context' });

  await page.fill('#input', '#squid@claude hello');
  await page.click('#pin-btn');
  await expect(page.locator('.pin-item-status')).toContainText('will inject');
  await page.click('#pin-btn'); // close panel

  await page.focus('#input');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();

  // Already sent in this session turn — badge should clear, not keep showing "will inject".
  await expect(page.locator('#pin-count')).toHaveText('1');
  await expect(page.locator('#pin-btn')).toHaveClass(/has-saved-pins/);
  await page.click('#pin-btn');
  await expect(page.locator('.pin-item-status')).toContainText('in session');
});

test('session send turns attached file badge gray once delivered', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: false, content: '', path: '~/.squid/context/topics/squid/memory.md',
      squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
    },
  }));
  await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({
    json: { session_id: null, cwd: null },
  }));

  const sentMessages = [];
  await page.route('**/chat', async route => {
    sentMessages.push(route.request().postDataJSON().message);
    await route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'session response' }, STATS, DONE),
    });
  });

  await page.goto('/');
  await seedAttachedFile(page, '/Users/haebin/Work/squid/pyproject.toml');

  await page.fill('#input', '#squid@claude hello');
  await page.click('#pin-btn');
  await expect(page.locator('#pin-count')).toHaveText('1');
  await expect(page.locator('#pin-btn')).toHaveClass(/(^|\s)has-context(\s|$)/);
  await expect(page.locator('.pin-item-status')).toContainText('will inject');
  await page.click('#pin-btn');

  await page.focus('#input');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();
  expect(sentMessages[0]).toContain('Files:\n- /Users/haebin/Work/squid/pyproject.toml');

  await expect(page.locator('#pin-count')).toHaveText('1');
  await expect(page.locator('#pin-btn')).toHaveClass(/has-saved-pins/);
  await expect(page.locator('#pin-btn')).not.toHaveClass(/(^|\s)has-context(\s|$)/);
  await page.click('#pin-btn');
  await expect(page.locator('.pin-item-status')).toContainText('in session');
  await page.click('#pin-btn');

  await page.fill('#input', '#squid@claude again');
  await page.keyboard.press('Enter');
  await expect(sentMessages[1]).not.toContain('Files:');
});

test('session send marks memory and pinned context as sending while request is pending', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: true, content: MEMORY_WITH_SKIP, revision: 'rev-1', path: '~/.squid/context/topics/squid/memory.md',
      squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
    },
  }));
  await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({
    json: { session_id: null, cwd: null },
  }));
  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse(META, STATS),
  }));

  await page.goto('/');
  await seedPin(page, { id: 77, topic: 'other', agent: 'codex', content: 'cross-topic context' });

  await page.fill('#input', '#squid@claude hello');
  await page.click('#pin-btn');
  await expect(page.locator('#pin-count')).toHaveText('2');
  await expect(page.locator('#pin-btn')).toHaveClass(/(^|\s)has-context(\s|$)/);
  await page.click('#pin-btn');

  await page.focus('#input');
  await page.keyboard.press('Enter');

  await expect(page.locator('#pin-count')).toHaveText('2');
  await expect(page.locator('#pin-btn')).toHaveClass(/has-context-pending/);
  await expect(page.locator('#pin-count')).toHaveCSS('animation-name', 'pin-badge-pending-pulse');
  await expect(page.locator('#pin-count')).toHaveCSS('animation-duration', '1.8s');
  await expect(page.locator('#pin-count')).toHaveCSS('transform', 'none');
  await page.click('#pin-btn');
  await expect(page.locator('.memory-item-status')).toContainText('sending');
  await expect(page.locator('.pin-item-status')).toContainText('sending');
});

test('status polling completion clears sending context badge', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: true, content: MEMORY_WITH_SKIP, revision: 'rev-1', path: '~/.squid/context/topics/squid/memory.md',
      squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
    },
  }));
  await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({
    json: { session_id: null, cwd: null },
  }));
  await page.route('**/chat/42/status', r => r.fulfill({
    json: {
      status: 'done',
      content: 'recovered response',
      agent: 'claude',
      session_id: 'test-sess-abc',
      session_turn_count: 1,
      stats: { session_id: 'test-sess-abc', input_tokens: 10, output_tokens: 5 },
    },
  }));
  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse({ event: 'meta', data: { agent: 'claude', backend: 'claude', msg_id: 42, adhoc: false } }),
  }));

  await page.goto('/');
  await seedPin(page, { id: 77, topic: 'other', agent: 'codex', content: 'cross-topic context' });

  await page.fill('#input', '#squid@claude hello');
  await page.focus('#input');
  await page.keyboard.press('Enter');

  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toContainText('recovered response');
  await expect(page.locator('#pin-count')).toHaveText('1');
  await expect(page.locator('#pin-btn')).toHaveClass(/has-saved-pins/);
  await expect(page.locator('#pin-btn')).not.toHaveClass(/has-context-pending/);
  await page.click('#pin-btn');
  await expect(page.locator('.memory-item-status')).toContainText('in session');
  await expect(page.locator('.pin-item-status')).toContainText('in session');
});

test('cancelled session send restores pending memory and pins to will-inject', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: true, content: MEMORY_WITH_SKIP, revision: 'rev-1', path: '~/.squid/context/topics/squid/memory.md',
      squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
    },
  }));
  await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({
    json: { session_id: null, cwd: null },
  }));
  await page.route('**/cmd', r => r.fulfill({ json: { ok: true, killed: 1 } }));

  await page.goto('/');
  await seedPin(page, { id: 77, topic: 'other', agent: 'codex', content: 'cross-topic context' });
  await page.evaluate(() => {
    const orig = window.fetch;
    window.fetch = async (url, opts = {}) => {
      const path = typeof url === 'string' ? url : url?.url || '';
      if (!path.endsWith('/chat')) return orig(url, opts);
      const { readable, writable } = new TransformStream();
      const writer = writable.getWriter();
      const enc = new TextEncoder();
      writer.write(enc.encode(
        'event: meta\ndata: {"agent":"claude","backend":"claude","msg_id":42,"adhoc":false}\n\n'
      ));
      opts.signal?.addEventListener('abort', () => writer.close().catch(() => {}), { once: true });
      return new Response(readable, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      });
    };
  });

  await page.fill('#input', '#squid@claude hello');
  await page.click('#pin-btn');
  await expect(page.locator('#pin-count')).toHaveText('2');
  await expect(page.locator('#pin-btn')).toHaveClass(/(^|\s)has-context(\s|$)/);
  await page.click('#pin-btn');
  await page.focus('#input');
  await page.keyboard.press('Enter');

  await expect(page.locator('.thinking-kill-btn')).toBeVisible();
  await expect(page.locator('#pin-btn')).toHaveClass(/has-context-pending/);
  await page.locator('.thinking-kill-btn').click();
  await expect(page.locator('#pin-count')).toHaveText('2');
  await expect(page.locator('#pin-btn')).not.toHaveClass(/has-context-pending/);
  await expect(page.locator('#pin-btn')).toHaveClass(/(^|\s)has-context(\s|$)/);

  await page.click('#pin-btn');
  await expect(page.locator('.memory-item-status')).toContainText('will inject');
  await expect(page.locator('.pin-item-status')).toContainText('will inject');
});

test('adhoc turn keeps the will-inject pin badge after send', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse(
      { event: 'meta', data: { agent: 'claude', backend: 'claude', msg_id: 30, adhoc: true } },
      { data: 'response' },
      STATS,
      DONE,
    ),
  }));

  await page.goto('/');
  // Pin from the same topic@agent — adhoc turns always re-inject regardless of prior sends.
  await seedPin(page, { id: 88, topic: 'squid', agent: 'claude', content: 'cached response' });

  await page.fill('#input', '#squid@claude! hello');
  await page.click('#pin-btn');
  await expect(page.locator('.pin-item-status')).toContainText('will inject');
  await page.click('#pin-btn'); // close panel

  await page.focus('#input');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();

  // Adhoc turns re-inject every time — badge should still show it'll inject again, not clear.
  await expect(page.locator('#pin-count')).toHaveText('1');
  await expect(page.locator('#pin-btn')).toHaveClass(/(^|\s)has-context(\s|$)/);
  await page.click('#pin-btn');
  await expect(page.locator('.pin-item-status')).toContainText('will inject');
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

test('adhoc lookback selects recent visible responses as toggleable context pins', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/history**', r => r.fulfill({ json: {
    has_more: false,
    items: [
      { id: 10, topic: 'squid', agent: 'claude', role: 'assistant', status: 'done', prompt: 'p1', content: 'first response', adhoc: false },
      { id: 11, topic: 'squid', agent: 'claude', role: 'assistant', status: 'done', prompt: 'p2', content: 'second response', adhoc: false },
      { id: 12, topic: 'squid', agent: 'claude', role: 'assistant', status: 'done', prompt: 'p3', content: 'third response', adhoc: false },
    ],
  }}));

  let capturedBody = null;
  await page.route('**/chat', async route => {
    capturedBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        { event: 'meta', data: { agent: 'claude', backend: 'claude', msg_id: 20, adhoc: true } },
        { data: 'response' },
        DONE,
      ),
    });
  });

  await page.goto('/');
  await expect(page.locator('.history-item.msg.assistant')).toHaveCount(3);
  await page.fill('#input', '#squid@claude!2 ');
  await expect(page.locator('#topic-chip')).toContainText('!2');
  await expect(page.locator('.msg-pin-btn.lookback-sel')).toHaveCount(2);
  await expect(page.locator('.msg-pin-btn[data-msg-id="11"]')).toHaveClass(/lookback-sel/);
  await expect(page.locator('.msg-pin-btn[data-msg-id="12"]')).toHaveClass(/lookback-sel/);

  await page.click('#pin-btn');
  await expect(page.locator('.pin-item-lookback')).toHaveCount(2);
  await expect(page.locator('.pin-item-lookback')).toContainText(['second response', 'third response']);

  await page.locator('[data-lookback-id="11"]').click();
  await expect(page.locator('.msg-pin-btn[data-msg-id="11"]')).not.toHaveClass(/lookback-sel/);
  await expect(page.locator('.msg-pin-btn[data-msg-id="12"]')).toHaveClass(/lookback-sel/);
  await expect(page.locator('.pin-item-lookback')).toHaveCount(1);
  await page.click('#pin-btn');

  await page.focus('#input');
  await page.fill('#input', 'compare');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant:not(.msg-thinking)').last()).toBeVisible();

  expect(capturedBody?.adhoc).toBe(true);
  expect(capturedBody?.lookback).toBe(2);
  expect(capturedBody?.lookback_via_pins).toBe(true);
  expect(capturedBody?.pinned_ids).toEqual([12]);
});

test('adhoc lookback selects async search result responses without another keystroke', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/search**', r => r.fulfill({ json: {
    items: [
      { id: 21, topic: 'squid', agent: 'claude', role: 'assistant', status: 'done', prompt: 'p1', content: 'first search response', adhoc: false },
      { id: 22, topic: 'squid', agent: 'claude', role: 'assistant', status: 'done', prompt: 'p2', content: 'second search response', adhoc: false },
      { id: 23, topic: 'squid', agent: 'claude', role: 'assistant', status: 'done', prompt: 'p3', content: 'third search response', adhoc: false },
    ],
  }}));

  await page.goto('/');
  await page.fill('#input', '#squid@claude!2 ');
  await expect(page.locator('#topic-chip')).toContainText('!2');

  await page.fill('#input', '/s #squid response');
  await page.keyboard.press('Enter');

  await expect(page.locator('.search-result-item.msg.assistant')).toHaveCount(3);
  await expect(page.locator('.search-result-item.msg.assistant.lookback-sel')).toHaveCount(2);
  await expect(page.locator('.msg-pin-btn[data-msg-id="22"]')).toHaveClass(/lookback-sel/);
  await expect(page.locator('.msg-pin-btn[data-msg-id="23"]')).toHaveClass(/lookback-sel/);
});

test('fresh session send includes topic memory when memory exists', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: true, content: MEMORY_WITH_SKIP, path: '~/.squid/context/topics/squid/memory.md',
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

test('topic memory reinjects when revision changes after prior injection', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });

  let memoryContent = MEMORY_WITH_SKIP;
  let memoryRevision = 'rev-1';
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: true, content: memoryContent, revision: memoryRevision,
      path: '~/.squid/context/topics/squid/memory.md',
      squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
    },
  }));
  await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({
    json: { session_id: null, cwd: null },
  }));

  const capturedBodies = [];
  await page.route('**/chat', async route => {
    capturedBodies.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: `response ${capturedBodies.length}` }, DONE),
    });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude first');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toHaveCount(1);

  await page.fill('#input', '#squid@claude second');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toHaveCount(2);

  memoryContent = `${MEMORY_WITH_SKIP}\nFresh update.`;
  memoryRevision = 'rev-2';

  await page.fill('#input', '#squid@claude third');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toHaveCount(3);

  expect(capturedBodies.map(body => body.include_topic_memory)).toEqual([true, undefined, true]);
});

test('context cart highlights stale topic memory from session revision', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: true, content: `${MEMORY_WITH_SKIP}\nFresh update.`, revision: 'rev-2',
      path: '~/.squid/context/topics/squid/memory.md',
      squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
    },
  }));
  await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({
    json: { session_id: 'active-session', cwd: '/tmp/squid', injected_ids: [], memory_revision: 'rev-1' },
  }));

  await page.goto('/');
  await page.fill('#input', '#squid@claude hello');
  await page.click('#pin-btn');

  await expect(page.locator('.memory-item-status')).toContainText('will inject');
  await expect(page.locator('[data-memory-toggle]')).toHaveText('On');
});

test('context cart highlights legacy injected topic memory without revision', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: true, content: `${MEMORY_WITH_SKIP}\nFresh update.`, revision: 'rev-2',
      path: '~/.squid/context/topics/squid/memory.md',
      squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
    },
  }));
  await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({
    json: { session_id: 'active-session', cwd: '/tmp/squid', injected_ids: [], memory_injected: true, memory_revision: null },
  }));

  await page.goto('/');
  await page.fill('#input', '#squid@claude hello');
  await page.click('#pin-btn');

  await expect(page.locator('.memory-item-status')).toContainText('will inject');
  await expect(page.locator('[data-memory-toggle]')).toHaveText('On');
});

test('/clear memory injects once and is unselected immediately after send', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: true, content: MEMORY_WITH_SKIP, path: '~/.squid/context/topics/squid/memory.md',
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
        json: { topic: 'squid', exists: true, content: savedContent, path: '~/.squid/context/topics/squid/memory.md' },
      });
      return;
    }
    await route.fulfill({
      json: { topic: 'squid', exists: !!savedContent, content: savedContent, path: '~/.squid/context/topics/squid/memory.md' },
    });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude hello');
  await page.click('#pin-btn');
  await page.locator('[data-memory-toggle]').click();

  await expect(page.locator('#memory-modal.open')).toBeVisible();
  await expect(page.locator('#memory-path')).toHaveText('~/.squid/context/topics/squid/memory.md');
  await page.fill('#memory-editor', 'Prefer transparent context.');
  await page.click('#memory-save');

  await expect(page.locator('#memory-save')).toHaveText('Saved');
  await expect(page.locator('#memory-path')).toContainText('saved');
  expect(savedContent).toBe('Prefer transparent context.');

  await page.click('#memory-modal-close');
  await page.click('#pin-btn');
  await expect(page.locator('.memory-item-preview')).toContainText('Prefer transparent context.');
});

test('a slow first memory fetch does not clobber a newer save (out-of-order response)', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({
    json: { session_id: null, cwd: null },
  }));

  let savedContent = '';
  let getCallCount = 0;
  let releaseFirstGet;
  const firstGetHeld = new Promise(resolve => { releaseFirstGet = resolve; });

  await page.route('**/topics/squid/memory', async route => {
    if (route.request().method() === 'PUT') {
      savedContent = route.request().postDataJSON().content;
      await route.fulfill({
        json: { topic: 'squid', exists: true, content: savedContent, path: '~/.squid/context/topics/squid/memory.md' },
      });
      return;
    }
    getCallCount++;
    if (getCallCount === 1) {
      // Simulates the GET fired the moment the chip is created (before memory exists),
      // which stays in flight and only resolves — with stale, pre-save data — after a
      // save that happened in the meantime.
      await firstGetHeld;
      await route.fulfill({
        json: { topic: 'squid', exists: false, content: '', path: '~/.squid/context/topics/squid/memory.md' },
      });
      return;
    }
    await route.fulfill({
      json: { topic: 'squid', exists: !!savedContent, content: savedContent, path: '~/.squid/context/topics/squid/memory.md' },
    });
  });

  await page.goto('/');
  const firstGetIssued = page.waitForRequest(req =>
    req.url().includes('/topics/squid/memory') && req.method() === 'GET');
  // Typing "#squid@claude " promotes the chip, which fires the (held) memory GET above.
  await page.fill('#input', '#squid@claude ');
  await firstGetIssued;

  // Add memory content and save while the first GET is still pending.
  await page.click('#pin-btn');
  await page.locator('[data-memory-toggle]').click();
  await expect(page.locator('#memory-modal.open')).toBeVisible();
  await page.fill('#memory-editor', 'Fresh memory content.');
  await page.click('#memory-save');
  await expect(page.locator('#memory-save')).toHaveText('Saved');
  await page.click('#memory-modal-close');

  // Now let the stale first GET resolve with its old (pre-save) snapshot.
  releaseFirstGet();
  await page.waitForTimeout(200);

  await page.click('#pin-btn');
  await expect(page.locator('.memory-item-preview')).toContainText('Fresh memory content.');
  await expect(page.locator('[data-memory-toggle]')).toHaveText('On');
});

test('active session skips topic memory by default', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/topics/squid/memory', r => r.fulfill({
    json: {
      topic: 'squid', exists: true, content: MEMORY_WITH_SKIP, path: '~/.squid/context/topics/squid/memory.md',
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
  await expect(page.locator('#pin-count')).toHaveText('');
});
