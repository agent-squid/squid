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

const META = { event: 'meta', data: { agent: 'claude', backend: 'claude', msg_id: 1, adhoc: false } };
const DONE = { event: 'done', data: '' };

function statsEvent(turnCount) {
  return { event: 'stats', data: { session_id: 'test-sid', input_tokens: 10, output_tokens: 5, adhoc: false, lookback: 0, session_turn_count: String(turnCount) } };
}

async function mockBackend(page) {
  await page.route('**/config/realtime', r => r.fulfill({ json: { transport: 'sse' } }));
  await page.route('**/health',          r => r.fulfill({ json: { status: 'ok', boot_time: new Date().toISOString() } }));
  await page.route('**/history**',       r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**',         r => r.fulfill({ json: {} }));
  await page.route('**/topics',          r => r.fulfill({ json: [] }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: { topic: 'test', exists: false, content: '', squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false } } }));
  await page.route('**/topics/**',       r => r.fulfill({ json: [] }));
  await page.route('**/config/agents',   r => r.fulfill({ json: [] }));
  await page.route('**/chat/*/status',   r => r.fulfill({ json: { status: 'pending', content: '' } }));
}

async function sendTurns(page, count, startAt = 1) {
  for (let i = 0; i < count; i++) {
    let fulfill;
    await page.route('**/chat', route => { fulfill = body => route.fulfill({ status: 200, headers: SSE_HEADERS, body }); });
    await page.fill('#input', `msg ${i}`);
    await page.keyboard.press('Enter');
    fulfill(sse(META, statsEvent(startAt + i), { event: 'text', data: 'ok' }, DONE));
    await page.waitForFunction(() => !document.querySelector('.msg-thinking'));
    await page.unroute('**/chat');
  }
  return startAt + count; // next turn number
}

test.describe('session advisory', () => {
  test.beforeEach(async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await page.fill('#input', '#test@claude ');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(200);
  });

  test('hidden before 10 turns', async ({ page }) => {
    await sendTurns(page, 5);
    await expect(page.locator('#session-advisory')).toBeHidden();
  });

  test('stale session lookup cannot overwrite a newer turn count', async ({ page }) => {
    await page.evaluate(() => {
      _sessionIds['test@claude'] = 'test-sid';
      _updateChipTurnCount('test', 'claude', 'test-sid', 12);
      _setKnownSessionTurnCount('test', 'claude', 11, 'test-sid');
    });
    await expect(page.locator('#topic-chip .chip-turn-count')).toHaveText('·12t');
    expect(await page.evaluate(() => _knownSessionTurnCount('test', 'claude'))).toBe(12);
  });

  test('route selection refreshes a cached zero after another device creates the session', async ({ page }) => {
    let turnCount = 0;
    let requests = 0;
    await page.route('**/topics/test/session?agent=claude', route => {
      requests++;
      return route.fulfill({ json: turnCount
        ? { session_id: 'shared-session', session_turn_count: turnCount, cwd: null }
        : { session_id: null, session_turn_count: 0, cwd: null } });
    });

    await page.evaluate(() => setTopicChip('test', 'claude', false));
    await expect(page.locator('#topic-chip .chip-turn-count')).toHaveText('·0t');
    turnCount = 1;
    await page.evaluate(() => setTopicChip('test', 'claude', false));
    await expect(page.locator('#topic-chip .chip-turn-count')).toHaveText('·1t');
    expect(requests).toBeGreaterThanOrEqual(2);
  });

  test('shows at 10 turns', async ({ page }) => {
    await sendTurns(page, 10);
    await expect(page.locator('#session-advisory')).toBeVisible();
    await expect(page.locator('#session-advisory-msg')).toContainText('10+');
  });

  test('dismiss hides until next bucket', async ({ page }) => {
    let next = await sendTurns(page, 10);
    await expect(page.locator('#session-advisory')).toBeVisible();
    await page.click('#session-advisory-dismiss');
    await expect(page.locator('#session-advisory')).toBeHidden();

    // re-shows at 20
    await sendTurns(page, 10, next);
    await expect(page.locator('#session-advisory')).toBeVisible();
    await expect(page.locator('#session-advisory-msg')).toContainText('20+');
  });

  test('hidden for adhoc route', async ({ page }) => {
    // Route replacement is explicit while a sticky chip is active.
    await page.evaluate(() => clearTopicChip());
    await page.fill('#input', '#test@claude! hello');
    let fulfill;
    await page.route('**/chat', route => { fulfill = body => route.fulfill({ status: 200, headers: SSE_HEADERS, body }); });
    await page.keyboard.press('Enter');
    fulfill(sse({ event: 'meta', data: { agent: 'claude', backend: 'claude', msg_id: 2, adhoc: true } }, { event: 'stats', data: { session_id: 'adhoc-sid', adhoc: true, session_turn_count: '10' } }, DONE));
    await page.waitForFunction(() => !document.querySelector('.msg-thinking'));
    await expect(page.locator('#session-advisory')).toBeHidden();
  });

  test('idle advisory shows when last_turn_at is over 1h ago', async ({ page }) => {
    await sendTurns(page, 1);
    // backdate last_turn_at by 2 hours, then re-evaluate without a new turn
    await page.evaluate(() => {
      const key = [...Object.keys(localStorage)].find(k => k.startsWith('squid_adv_lta_'));
      if (key) localStorage.setItem(key, String(Date.now() - 2 * 3600 * 1000));
      window._squidEvalAdvisory();
    });
    await expect(page.locator('#session-advisory')).toBeVisible();
    await expect(page.locator('#session-advisory-msg')).toContainText('context from');
  });

  test('chip click inserts /clear and stashes draft', async ({ page }) => {
    await sendTurns(page, 10);
    await page.fill('#input', 'my draft');
    await page.click('#session-advisory-clear');
    await expect(page.locator('#input')).toHaveValue('/clear');
    await expect(page.locator('.restore-hint')).toBeVisible();
  });

  test('Tab inserts /clear when advisory visible', async ({ page }) => {
    await sendTurns(page, 10);
    await page.click('#input');
    await page.keyboard.press('Tab');
    await expect(page.locator('#input')).toHaveValue('/clear');
  });
});
