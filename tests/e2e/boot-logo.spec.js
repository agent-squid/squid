const { test, expect } = require('@playwright/test');

// The app registers a service worker (ui/sw.js) on window 'load'. The new
// tests below deliberately hold the page open for over a second while
// waiting on delayed mocked routes — long enough in practice for the SW to
// register, update-check, and (if its cached version differs from a prior
// test run's) trigger app.js's own controllerchange -> location.reload(),
// silently restarting the whole scenario mid-test. Block it, matching the
// existing precedent in stats-aggregates.spec.js.
test.use({ serviceWorkers: 'block' });

function todayKey() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function streakDates(n) {
  const dates = [];
  const d = new Date();
  for (let i = 0; i < n; i++) {
    dates.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`);
    d.setDate(d.getDate() - 1);
  }
  return dates;
}

async function mockBoot(page, variant, opts = {}) {
  const { streak = 0, insights, statsRows = [] } = opts;
  await page.addInitScript(value => {
    window.__squidBootLogoVariant = value;
  }, variant);
  if (streak > 0) {
    await page.addInitScript(dates => {
      localStorage.setItem('squid_active_days', JSON.stringify(dates));
    }, streakDates(streak));
  }
  await page.route('**/health', route => route.fulfill({
    json: { status: 'ok', boot_time: '2026-07-14T12:00:00Z', harnesses: [], total_prompts: 0, first_seen: '2026-01-01T00:00:00Z' },
  }));
  await page.route('**/insights.json*', route => route.fulfill({
    json: insights || {
      measures: {
        period: '7d',
        values: [
          { key: 'streak', source: 'local' },
          { key: 'hour',   source: 'clock' },
          { key: 'dow',    source: 'clock' },
          { key: 'turns',     measure: 'turns' },
          { key: 'turns_wow', measure: 'turns', compare: 'prev_period', fmt: 'delta' },
          { key: 'cache',     measure: 'cache_hit_rate' },
          { key: 'cache_wow', measure: 'cache_hit_rate', compare: 'prev_period', fmt: 'pp' },
        ],
      },
      boot: {
        default: 'More Done, Less Tokens.',
        templates: [
          { text: '7 days on a roll.', when: { streak: 7 } },
          { text: '{turns} turns last 7d, {turns_wow} from before 🦑', when: { turns: { gte: 50 } } },
          { text: 'Cache hit {cache} — {cache_wow} vs last 7d', when: { cache: { gte: 80 }, cache_wow: { gte: 1 } } },
        ],
      },
    },
  }));
  // Mock /stats — used by resolveInsightMeasures. resolveInsightMeasures now
  // issues a separate request per window (current: no anchor, previous:
  // anchor=N-days-ago) instead of one combined range split in half, so route
  // by presence of `anchor` to hand back the right slice of statsRows
  // (index 0 = current period, index 1 = previous period).
  await page.route('**/stats**', route => {
    const url = new URL(route.request().url());
    const rows = url.searchParams.has('anchor') ? statsRows.slice(1) : statsRows.slice(0, 1);
    route.fulfill({ json: rows });
  });
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**', route => route.fulfill({ json: {} }));
  await page.route('**/topics', route => route.fulfill({ json: [] }));
  await page.route('**/config/agents', route => route.fulfill({ json: [] }));
}

test('boot logo variants: art, squid-only, talking-squid', async ({ page }) => {
  // Variant 0: ASCII art
  await mockBoot(page, 0);
  await page.goto('/');
  await expect(page.locator('.boot-art')).toContainText('AGENT');
  await expect(page.locator('.boot-art-mobile')).toHaveText('🦑 AGENT-SQUID');
  await expect(page.locator('.boot-logo-icon')).toHaveCount(0);
  await expect(page.locator('.boot-logo-bubble')).toHaveCount(0);

  // Variant 1: squid logo only
  await mockBoot(page, 1);
  await page.reload();
  await expect(page.locator('.boot-logo-squid-only .boot-logo-icon')).toBeVisible();
  await expect(page.locator('.boot-logo-bubble')).toHaveCount(0);

  // Variant 2: talking squid with default fallback
  await mockBoot(page, 2);
  await page.reload();
  await expect(page.locator('.boot-logo-talking-squid .boot-logo-icon')).toBeVisible();
  await expect(page.locator('.boot-logo-talking-squid .boot-logo-bubble')).toHaveText('More Done, Less Tokens.');
});

test('narrow load settles at the bottom of the boot banner', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 });
  await mockBoot(page, 2);
  await page.goto('/');
  await expect(page.locator('.boot-logo-icon')).toBeVisible();
  await expect.poll(() => page.locator('#messages').evaluate(el => (
    el.scrollHeight - el.scrollTop - el.clientHeight
  ))).toBeLessThan(2);
  // Set as early as possible (inline in <head>), not from app.js — verify
  // it's already in effect on the very first load, no reload needed.
  expect(await page.evaluate(() => history.scrollRestoration)).toBe('manual');
});

// Enough items, each long enough to wrap several lines, to guarantee
// #messages actually overflows a 700px-tall mobile viewport — otherwise
// scrollTop assignments below are indistinguishable no-ops (scrollHeight <=
// clientHeight means every position reads as "at the bottom").
function tallHistoryItems(n) {
  return Array.from({ length: n }, (_, i) => ({
    id: 100 + i,
    role: 'assistant',
    reply_to: 50 + i,
    topic: 'default',
    agent: 'claude',
    backend: 'claude',
    adhoc: false,
    status: 'done',
    prompt: `prompt ${i}`,
    content: `Response ${i}: `.repeat(40),
    completed_at: new Date(Date.now() - (n - i) * 60000).toISOString(),
  }));
}

// The old version of this test reloaded after setting #messages.scrollTop =
// 0, expecting that to simulate a browser restoring a stale scroll
// position. It didn't: the JS realm and DOM are torn down and rebuilt fresh
// on reload, so the assignment never survives to be "restored" — the test
// passed regardless of whether any fix was in place. These two replace it
// by exercising the actual mechanism Squid now uses to recover: history
// loads (and its own pagination-compensation scrolling) finishes quickly,
// then the boot banner's logo image is deliberately slowed, holding the
// final "bootstrap settled" realignment open. Something (standing in for
// native restoration, CSS scroll anchoring, or a realtime-snapshot race —
// Squid can't tell which one hits in the field) knocks #messages off bottom
// during that window; the final realignment must correct it once everything
// Squid controls has actually settled — but must not override a real user
// scroll made during that same window.
async function slowBootLogoImage(page, delayMs = 1500) {
  await page.route('**/favicon.png', async route => {
    await new Promise(resolve => setTimeout(resolve, delayMs));
    await route.fulfill({ status: 404, body: '' });
  });
}

test('final realignment corrects the transcript to bottom once history and boot banner settle', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 });
  await mockBoot(page, 2);
  await page.route('**/history**', route => route.fulfill({ json: { items: tallHistoryItems(20), has_more: false } }));
  await slowBootLogoImage(page);
  // page.goto() defaults to waiting for the 'load' event, which itself
  // waits on the deliberately-slowed boot-logo image — so by the time goto()
  // would normally resolve, bootBannerSettled has already fired. Resolve
  // earlier so the test can interfere with scroll position *during* the
  // delay window instead of after it.
  await page.goto('/', { waitUntil: 'domcontentloaded' });

  // History is tall and unthrottled — give its own pagination-compensation
  // scrolling time to finish, then knock the view off bottom while the
  // slowed boot-logo image still holds the final realignment open.
  await page.waitForTimeout(150);
  await page.locator('#messages').evaluate(el => { el.scrollTop = 0; });

  await expect.poll(() => page.locator('#messages').evaluate(el => (
    el.scrollHeight - el.scrollTop - el.clientHeight
  )), { timeout: 5000 }).toBeLessThan(2);
});

test('a manual scroll during startup is not overridden by the final realignment', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 });
  await mockBoot(page, 2);
  await page.route('**/history**', route => route.fulfill({ json: { items: tallHistoryItems(20), has_more: false } }));
  await slowBootLogoImage(page);
  // page.goto() defaults to waiting for the 'load' event, which itself
  // waits on the deliberately-slowed boot-logo image — so by the time goto()
  // would normally resolve, bootBannerSettled has already fired. Resolve
  // earlier so the test can interfere with scroll position *during* the
  // delay window instead of after it.
  await page.goto('/', { waitUntil: 'domcontentloaded' });

  await page.waitForTimeout(150);
  await page.locator('#messages').evaluate(el => { el.scrollTop = 0; });
  const box = await page.locator('#messages').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, 10);

  // Long enough for the (1500ms-delayed) settle signal to have resolved
  // and, if the guard failed, for the realignment to have already fired.
  await page.waitForTimeout(2000);
  const gap = await page.locator('#messages').evaluate(el => (
    el.scrollHeight - el.scrollTop - el.clientHeight
  ));
  expect(gap).toBeGreaterThan(2);
});

test('talking squid shows streak message on day 7', async ({ page }) => {
  await mockBoot(page, 2, { streak: 7 });
  await page.goto('/');
  await expect(page.locator('.boot-logo-talking-squid .boot-logo-bubble')).toHaveText('7 days on a roll.');
});

test('talking squid shows turns milestone with WoW', async ({ page }) => {
  // Mock /stats with 2 weekly rows (this week + last week)
  // total_turns: 228 this week, 180 last week → delta +48
  await mockBoot(page, 2, {
    streak: 1,
    statsRows: [
      { period: '2026-07-13', total_turns: 228, sessions: 5, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, cost_usd: 0, quota_delta: 0, duration_ms: 0 },
      { period: '2026-07-06', total_turns: 180, sessions: 4, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, cost_usd: 0, quota_delta: 0, duration_ms: 0 },
    ],
  });
  await page.goto('/');
  await expect(page.locator('.boot-logo-talking-squid .boot-logo-bubble')).toHaveText('228 turns last 7d, +48 from before 🦑');
});

test('talking squid falls back to default when no match', async ({ page }) => {
  await mockBoot(page, 2, { streak: 1, statsRows: [
    { period: '2026-07-13', total_turns: 5, sessions: 1, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, cost_usd: 0, quota_delta: 0, duration_ms: 0 },
  ]});
  await page.goto('/');
  await expect(page.locator('.boot-logo-talking-squid .boot-logo-bubble')).toHaveText('More Done, Less Tokens.');
});
