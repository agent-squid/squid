/**
 * Filter command contract tests.
 * Verifies that /filter honours topic, agent, and adhoc (!) flag.
 */
const { test, expect } = require('@playwright/test');

async function mockBackend(page, { topic = 'squid', agent = 'claude' } = {}) {
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/quota',         r => r.fulfill({ json: {} }));
  await page.route('**/topics',        r => r.fulfill({ json: [
    { name: topic, agent, last_model: null, last_backend: 'claude', queue_depth: 0, active: false, last_prompt: 'hi' }
  ]}));
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
