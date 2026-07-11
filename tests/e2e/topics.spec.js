const { test, expect } = require('@playwright/test');

const TOPICS = [
  {
    name: 'squid',
    agent: 'codex',
    sticky_adhoc: false,
    last_model: 'gpt-5',
    last_backend: 'codex',
    last_prompt: 'implement the topic manager',
    last_at: '2026-06-12T12:00:00Z',
    hidden: false,
    queue_depth: 0,
    active: false,
    memory: { exists: true, path: '~/.squid/context/topics/squid/memory.md' },
    agents: [
      {
        agent: 'codex',
        last_prompt: 'session prompt',
        last_adhoc_prompt: 'adhoc prompt',
        last_at: '2026-06-12T12:00:00Z',
        last_model: 'gpt-5',
        last_backend: 'codex',
      },
    ],
  },
  {
    name: 'archive',
    agent: 'claude',
    sticky_adhoc: false,
    last_model: null,
    last_backend: 'claude',
    last_prompt: 'old prompt',
    last_at: '2026-06-11T12:00:00Z',
    hidden: true,
    queue_depth: 1,
    active: true,
    memory: { exists: false, path: '~/.squid/context/topics/archive/memory.md' },
    agents: [],
  },
];

async function mockBackend(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics/manage**', r => r.fulfill({ json: TOPICS }));
  await page.route('**/topics', r => r.fulfill({ json: TOPICS.filter(t => !t.hidden) }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic: 'squid',
    exists: true,
    content: 'Remember topic state.',
    path: '~/.squid/context/topics/squid/memory.md',
    squid: { code_roots: [], code_roots_skipped: false, code_roots_missing: false },
  }}));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
}

test('topics tab renders searchable expandable topic lanes and actions', async ({ page }) => {
  await mockBackend(page);

  const hiddenRequests = [];
  const deletedRequests = [];
  await page.route('**/topics/*/hidden', async route => {
    hiddenRequests.push(JSON.parse(route.request().postData() || '{}'));
    await route.fulfill({ json: { ok: true } });
  });
  await page.route('**/topics/*', async route => {
    if (route.request().method() === 'DELETE') {
      deletedRequests.push(route.request().url());
      await route.fulfill({ json: { ok: true } });
      return;
    }
    await route.fallback();
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Topics' }).click();

  await expect(page.locator('.topic-row')).toHaveCount(2);
  await expect(page.locator('.topic-row').first()).toContainText('#squid');
  await expect(page.locator('.topic-row.hidden')).toContainText('#archive');

  await page.locator('.topic-row', { hasText: '#squid' }).click();
  const sessionLane = page.locator('.topic-agent-row[data-adhoc="0"]', { hasText: '@codex' });
  await expect(sessionLane).toBeVisible();
  await expect(sessionLane).not.toContainText('#squid');
  await expect(page.locator('.topic-agent-row.adhoc')).toContainText('adhoc prompt');

  await page.fill('#topics-search', 'squ');
  await expect(page.locator('.topic-row')).toHaveCount(1);
  await expect(page.locator('.topic-row')).toContainText('#squid');

  await page.locator('[data-topic-hide="squid"]').click();
  expect(hiddenRequests).toEqual([{ hidden: true }]);

  await page.locator('[data-topic-memory="squid"]').click();
  await expect(page.locator('#memory-modal.open')).toBeVisible();
  await expect(page.locator('#memory-editor')).toHaveValue('Remember topic state.');
  await page.locator('#memory-modal-close').click();

  await page.locator('[data-topic-delete="squid"]').click();
  await expect(page.locator('#topic-delete-modal.open')).toBeVisible();
  await expect(page.locator('#topic-delete-modal-title')).toHaveText('#squid');
  await page.locator('#topic-delete-confirm').click();
  expect(deletedRequests[0]).toContain('/topics/squid');
});
