const { test, expect } = require('@playwright/test');

test('a provider approval retries only after the user selects Allow once', async ({ page }) => {
  const requests = [];
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'sse' } }));
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', route => route.fulfill({ json: [] }));
  await page.route('**/topics/*/memory', route => route.fulfill({
    json: { exists: false, content: '', squid: { code_roots: [], code_roots_skipped: true } },
  }));
  await page.route('**/config/agents', route => route.fulfill({ json: [] }));
  await page.route('**/chat', route => {
    const request = route.request().postDataJSON();
    requests.push(request);
    if (!request.provider_approval) {
      return route.fulfill({
        status: 409,
        json: {
          error: 'provider_approval_required',
          detail: 'Agent example on topic #test requires one-time approval to use provider example.',
          approval_token: 'one-time-token',
        },
      });
    }
    return route.fulfill({
      contentType: 'text/event-stream',
      body: 'event: meta\ndata: {"agent":"example","msg_id":10}\n\nevent: done\ndata: \n\n',
    });
  });

  await page.goto('/');
  await page.fill('#input', '#test@example hello');
  await page.keyboard.press('Enter');

  const modal = page.locator('#restart-modal');
  await expect(modal).toBeVisible();
  await expect(modal).toContainText('provider example');
  expect(requests).toHaveLength(1);

  await modal.getByRole('button', { name: 'Allow once' }).click();
  await expect(modal).toBeHidden();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[1]).toEqual({ ...requests[0], provider_approval: 'one-time-token' });
});
