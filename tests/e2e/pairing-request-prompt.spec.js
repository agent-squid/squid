const { test, expect } = require('@playwright/test');

// See boot-logo.spec.js / stats-aggregates.spec.js: the app's own SW
// controllerchange handler force-reloads the page mid-test if left enabled.
test.use({ serviceWorkers: 'block' });

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: {} }));
}

const REAL_CODE = 'A1B2C3D4E5F60718';
const DECOYS = ['9F8E7D6C5B4A3921', '0123456789ABCDEF'];

function pairingRequest(overrides = {}) {
  return {
    request_id: '018f4d3e-0000-7000-8000-00000000000a',
    device_id: '018f4d3e-1111-7000-8000-00000000000b',
    choices: [DECOYS[0], REAL_CODE, DECOYS[1]],
    received_at: Date.now() / 1000,
    expires_at: Date.now() / 1000 + 120,
    ...overrides,
  };
}

async function routeRequests(page, state) {
  await page.route('**/shore/pairing/requests', r => {
    state.polls = (state.polls || 0) + 1;
    if (state.status) return r.fulfill({ status: state.status, json: { error: state.error } });
    return r.fulfill({ json: { requests: state.requests } });
  });
}

test.describe('Browser pairing request prompt', () => {
  test('pops up app-wide without /pair and approves with the picked code', async ({ page }) => {
    await mockApp(page);
    const state = { requests: [pairingRequest()] };
    await routeRequests(page, state);
    let approveBody = null;
    await page.route('**/shore/pairing/requests/approve', r => {
      approveBody = r.request().postDataJSON();
      state.requests = [];
      r.fulfill({ json: { ok: true } });
    });
    await page.goto('/');

    const modal = page.locator('#pairing-request-modal');
    await expect(modal).toBeVisible();
    await expect(page.locator('#connect-modal')).toHaveCount(0);
    await expect(page.locator('#pairing-request-device')).toHaveText('Device 018f4d3e…');
    await expect(page.locator('#pairing-request-expires')).toHaveText(/Expires in [12]:\d{2}/);
    const choices = page.locator('.pairing-request-choice');
    await expect(choices).toHaveText(['9F8E-7D6C-5B4A-3921', 'A1B2-C3D4-E5F6-0718', '0123-4567-89AB-CDEF']);
    // No code is pre-focused (Enter must not pick one); Escape still works.
    await expect(page.locator('#pairing-request-box')).toBeFocused();
    await page.screenshot({ path: test.info().outputPath('pairing-request-prompt.png') });

    await choices.nth(1).click();
    await expect(modal).toBeHidden();
    expect(approveBody).toEqual({ request_id: pairingRequest().request_id, verification_code: REAL_CODE });
    await expect(page.locator('.cmd-feedback').last()).toContainText('confirm the host fingerprints in your browser');
  });

  test('a wrong pick is reported and the prompt closes', async ({ page }) => {
    await mockApp(page);
    const state = { requests: [pairingRequest()] };
    await routeRequests(page, state);
    await page.route('**/shore/pairing/requests/approve', r => {
      state.requests = [];
      r.fulfill({ status: 409, json: { error: 'pairing_code_mismatch' } });
    });
    await page.goto('/');

    await page.locator('.pairing-request-choice').first().click();
    await expect(page.locator('#pairing-request-modal')).toBeHidden();
    await expect(page.locator('.cmd-feedback').last()).toContainText('did not match');
  });

  test('host offline keeps the prompt open with an inline error for retry', async ({ page }) => {
    await mockApp(page);
    const state = { requests: [pairingRequest()] };
    await routeRequests(page, state);
    await page.route('**/shore/pairing/requests/approve', r =>
      r.fulfill({ status: 409, json: { error: 'shore_not_connected' } }));
    await page.goto('/');

    await page.locator('.pairing-request-choice').nth(1).click();
    await expect(page.locator('#pairing-request-modal')).toBeVisible();
    await expect(page.locator('#pairing-request-error')).toContainText('not connected to AgentSquid.AI');
    await expect(page.locator('.pairing-request-choice').nth(1)).toBeEnabled();
  });

  test('Reject cancels the request on the host', async ({ page }) => {
    await mockApp(page);
    const state = { requests: [pairingRequest()] };
    await routeRequests(page, state);
    let rejectBody = null;
    await page.route('**/shore/pairing/requests/reject', r => {
      rejectBody = r.request().postDataJSON();
      state.requests = [];
      r.fulfill({ json: { ok: true } });
    });
    await page.goto('/');

    await page.locator('#pairing-request-reject').click();
    await expect(page.locator('#pairing-request-modal')).toBeHidden();
    expect(rejectBody).toEqual({ request_id: pairingRequest().request_id });
    await expect(page.locator('.cmd-feedback').last()).toHaveText('Pairing request rejected.');
  });

  test('Later (Escape) hides it across polls; /pair can reopen it', async ({ page }) => {
    await mockApp(page);
    const state = { requests: [pairingRequest()] };
    await routeRequests(page, state);
    await page.route('**/remote', r => r.fulfill({ json: { reason: 'not_installed' } }));
    await page.route('**/shore/devices', r => r.fulfill({ json: { devices: [] } }));
    await page.route('**/shore/pairing/begin', r => r.fulfill({ json: {
      ceremony_id: '018f4d3e-0000-7000-8000-000000000004', code: 'ABCDE', expires_at: Date.now() / 1000 + 300,
      pair_url: 'https://dev.agentsquid.ai/@haebin/pair#abc' } }));
    await page.route('**/shore/pairing/status**', r => r.fulfill({ json: { status: 'pending' } }));
    await page.goto('/');

    await expect(page.locator('#pairing-request-modal')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.locator('#pairing-request-modal')).toBeHidden();
    const polls = state.polls;
    await expect.poll(() => state.polls).toBeGreaterThan(polls);
    await expect(page.locator('#pairing-request-modal')).toBeHidden();

    const input = page.locator('#input');
    await input.click();
    await input.fill('/pair');
    await input.press('Enter');
    const review = page.locator('#shore-pair-requests button');
    await expect(review).toHaveText('Review browser 018f4d3e… pairing request');
    await review.click();
    await expect(page.locator('#connect-modal')).toHaveCount(0);
    await expect(page.locator('#pairing-request-modal')).toBeVisible();
  });

  test('a request that disappears on the host closes the prompt', async ({ page }) => {
    await mockApp(page);
    const state = { requests: [pairingRequest()] };
    await routeRequests(page, state);
    await page.goto('/');

    await expect(page.locator('#pairing-request-modal')).toBeVisible();
    state.requests = [];
    await expect(page.locator('#pairing-request-modal')).toBeHidden();
    await expect(page.locator('.cmd-feedback').last()).toContainText('expired or was handled elsewhere');
  });

  test('a non-host browser (403) stops watching and never shows the prompt', async ({ page }) => {
    await mockApp(page);
    const state = { status: 403, error: 'loopback_required' };
    await routeRequests(page, state);
    await page.goto('/');

    await expect.poll(() => state.polls).toBe(1);
    await page.waitForTimeout(4000);
    expect(state.polls).toBe(1);
    await expect(page.locator('#pairing-request-modal')).toBeHidden();
  });
});
