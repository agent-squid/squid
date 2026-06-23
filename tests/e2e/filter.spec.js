/**
 * Filter command and history display contract tests.
 * Verifies that /filter honours topic, agent, and adhoc (!) flag,
 * and that history shows only assistant responses (no user bubbles).
 */
const { test, expect } = require('@playwright/test');

async function mockBackend(page, { topic = 'squid', agent = 'claude' } = {}) {
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok' } }));
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
}

// ── tests ─────────────────────────────────────────────────────────────────────

test('/filter with #topic@agent sends topic+agent+adhoc=false to /history', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude /filter');
  await page.keyboard.press('Enter');

  await page.waitForTimeout(300);
  expect(capturedUrl).toMatch(/topic=squid/);
  expect(capturedUrl).toMatch(/agent=claude/);
  expect(capturedUrl).toMatch(/adhoc=false/);
});

test('/filter with #topic@agent! sends adhoc=true to /history', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude! /filter');
  await page.keyboard.press('Enter');

  await page.waitForTimeout(300);
  expect(capturedUrl).toMatch(/topic=squid/);
  expect(capturedUrl).toMatch(/agent=claude/);
  expect(capturedUrl).toMatch(/adhoc=true/);
});

test('/filter with #topic only omits adhoc param from /history', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '#squid /filter');
  await page.keyboard.press('Enter');

  await page.waitForTimeout(300);
  expect(capturedUrl).toMatch(/topic=squid/);
  expect(capturedUrl).not.toMatch(/adhoc/);
});

test('/f accepts an explicit topic after the command during normal typing', async ({ page }) => {
  await mockBackend(page);

  const urls = [];
  await page.route('**/history**', async route => {
    urls.push(route.request().url());
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude!');
  await page.keyboard.press('Enter');
  await page.locator('#input').pressSequentially('/f #squid');
  await page.keyboard.press('Enter');

  const last = urls[urls.length - 1];
  expect(last).toMatch(/topic=squid/);
  expect(last).not.toMatch(/agent=/);
  expect(last).not.toMatch(/adhoc=/);
});

test('/f supports an agent-only filter across topics', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f @claude!');
  await page.keyboard.press('Enter');

  expect(capturedUrl).not.toMatch(/topic=/);
  expect(capturedUrl).toMatch(/agent=claude/);
  expect(capturedUrl).toMatch(/adhoc=true/);
});

test('/f @agent* filters both modes for one exact agent', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f @claude*');
  await page.keyboard.press('Enter');

  expect(capturedUrl).not.toMatch(/topic=/);
  expect(capturedUrl).toMatch(/agent=claude/);
  expect(capturedUrl).not.toMatch(/adhoc=/);
  await expect(page.locator('.filter-scope-agent')).toContainText('@claude*');

  await page.locator('.filter-scope-agent').click();
  await expect(page.locator('#input')).toHaveValue('/f @claude*');

  await page.fill('#input', '/f #squid@claude*');
  await page.keyboard.press('Enter');
  expect(capturedUrl).toMatch(/topic=squid/);
  expect(capturedUrl).toMatch(/agent=claude/);
  expect(capturedUrl).not.toMatch(/adhoc=/);
  await expect(page.locator('#filter-badge-label')).toContainText('#squid@claude*');
});

test('/f #all is rejected; reset is the explicit way to clear filters', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');

  await page.fill('#input', '/f #squid');
  await page.keyboard.press('Enter');
  await page.fill('#input', '/f #all');
  await page.keyboard.press('Enter');

  await expect(page.locator('.filter-scope-topic')).toContainText('#squid');
  await expect(page.locator('.cmd-feedback').last()).toContainText('Usage: /f');
});

test('filter badge segments can be removed independently and clicked to edit', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');

  await page.fill('#input', '/f #squid@claude!');
  await page.keyboard.press('Enter');
  await expect(page.locator('.filter-scope-topic')).toContainText('#squid');
  await expect(page.locator('.filter-scope-agent')).toContainText('@claude!');

  await page.locator('.filter-scope-topic .filter-scope-remove').click();
  await expect(page.locator('.filter-scope-topic')).toHaveCount(0);
  await expect(page.locator('.filter-scope-agent')).toContainText('@claude!');

  await page.fill('#input', 'unfinished prompt');
  await page.locator('.filter-scope-agent').click();
  await expect(page.locator('#input')).toHaveValue('/f @claude!');
  await page.locator('#input').press('ArrowUp');
  await expect(page.locator('#input')).toHaveValue('unfinished prompt');

  await page.fill('#input', '/f #squid@claude!');
  await page.keyboard.press('Enter');
  await page.locator('.filter-scope-agent .filter-scope-remove').click();
  await expect(page.locator('.filter-scope-topic')).toContainText('#squid');
  await expect(page.locator('.filter-scope-agent')).toHaveCount(0);
});

test('/filter reset clears filter and reloads history without params', async ({ page }) => {
  await mockBackend(page);

  const urls = [];
  await page.route('**/history**', async route => {
    urls.push(route.request().url());
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  // Set a filter first
  await page.fill('#input', '#squid@claude! /filter');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);

  // Now reset
  await page.fill('#input', '/filter reset');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);

  const last = urls[urls.length - 1];
  expect(last).not.toMatch(/topic=/);
  expect(last).not.toMatch(/agent=/);
  expect(last).not.toMatch(/adhoc=/);
});

test('history renders only assistant bubbles — no user bubbles', async ({ page }) => {
  await mockBackend(page);

  // Server returns only assistant rows — prompt snippet comes from the reply_to join
  await page.route('**/history**', r => r.fulfill({ json: {
    items: [
      { id: 2, role: 'assistant', topic: 'squid', agent: 'claude', content: 'Here is the answer.',
        status: 'done', adhoc: false, session_id: null, prompt: 'What is 2+2?',
        context: null, timestamp: new Date().toISOString(), reply_to: 1, stats: null },
    ],
    has_more: false,
  }}));

  await page.goto('/');
  await page.waitForTimeout(400);

  // Only the assistant bubble should be in the DOM
  await expect(page.locator('.msg.assistant.history-item')).toHaveCount(1);
  await expect(page.locator('.msg.user.history-item')).toHaveCount(0);

  // Prompt snippet is visible in the response header
  await expect(page.locator('.response-header-text')).toContainText('What is 2+2?');
});
