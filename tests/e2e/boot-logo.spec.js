const { test, expect } = require('@playwright/test');

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
  await page.route('**/insights.json', route => route.fulfill({
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
