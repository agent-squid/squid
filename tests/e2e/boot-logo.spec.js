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
  const { totalSessions = 0, streak = 0, insights } = opts;
  await page.addInitScript(value => {
    window.__squidBootLogoVariant = value;
  }, variant);
  if (streak > 0) {
    await page.addInitScript(dates => {
      localStorage.setItem('squid_active_days', JSON.stringify(dates));
    }, streakDates(streak));
  }
  await page.route('**/health', route => route.fulfill({
    json: { status: 'ok', boot_time: '2026-07-14T12:00:00Z', harnesses: [], total_sessions: totalSessions, first_seen: '2026-01-01T00:00:00Z' },
  }));
  await page.route('**/insights.json', route => route.fulfill({
    json: insights || {
      version: 1,
      boot: {
        default: 'More Done, Less Tokens.',
        templates: [
          { text: '7 days on a roll.', when: { streak: 7 } },
          { text: 'Session #{hit}! Next big thing.', when: { sessions: { milestone: [50, 100] } } },
        ],
      },
    },
  }));
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
  await mockBoot(page, 2, { streak: 7, totalSessions: 10 });
  await page.goto('/');
  await expect(page.locator('.boot-logo-talking-squid .boot-logo-bubble')).toHaveText('7 days on a roll.');
});

test('talking squid shows session milestone message', async ({ page }) => {
  await mockBoot(page, 2, { streak: 1, totalSessions: 100 });
  await page.goto('/');
  await expect(page.locator('.boot-logo-talking-squid .boot-logo-bubble')).toHaveText('Session #100! Next big thing.');
});

test('talking squid falls back to default when no match', async ({ page }) => {
  await mockBoot(page, 2, { streak: 1, totalSessions: 2 });
  await page.goto('/');
  await expect(page.locator('.boot-logo-talking-squid .boot-logo-bubble')).toHaveText('More Done, Less Tokens.');
});
