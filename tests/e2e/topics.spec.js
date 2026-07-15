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
    total_turns: 7,
    memory: { exists: true, path: '~/.squid/context/topics/squid/memory.md' },
    agents: [
      {
        agent: 'codex',
        last_prompt: 'session prompt',
        last_adhoc_prompt: 'adhoc prompt',
        last_at: '2026-06-13T12:00:00Z',
        last_session_at: '2026-06-12T12:00:00Z',
        last_adhoc_at: '2026-06-13T12:00:00Z',
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
    total_turns: 2,
    memory: { exists: false, path: '~/.squid/context/topics/archive/memory.md' },
    agents: [],
  },
];

async function mockBackend(page, topics = TOPICS) {
  const currentTopics = () => typeof topics === 'function' ? topics() : topics;
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics/manage**', r => r.fulfill({ json: currentTopics() }));
  await page.route('**/topics', r => r.fulfill({ json: currentTopics().filter(t => !t.hidden) }));
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
  await expect(page.locator('.topic-row').first().locator('.topic-identity')).not.toContainText('@codex');
  await expect(page.locator('.topic-row').first().locator('.topic-identity')).toContainText('7');
  await expect(page.locator('.topic-row.hidden')).toContainText('#archive');

  await page.locator('.topic-row', { hasText: '#squid' }).click();
  await expect(page.locator('.topic-row', { hasText: '#squid' }).locator('.topic-identity')).not.toContainText('@codex');
  const sessionLane = page.locator('.topic-agent-row[data-adhoc="0"]', { hasText: '@codex' });
  await expect(sessionLane).toBeVisible();
  await expect(sessionLane).not.toContainText('#squid');
  await expect(sessionLane.locator('.topic-badge.time')).toContainText('Jun 12');
  await expect(page.locator('.topic-agent-row.adhoc')).toContainText('adhoc prompt');
  await expect(page.locator('.topic-agent-row.adhoc .topic-badge.time')).toContainText('Jun 13');

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

test('topic open records topics page so browser back returns there', async ({ page }) => {
  await mockBackend(page);

  await page.goto('/');
  await page.getByRole('button', { name: 'Topics' }).click();
  await expect(page.locator('#view-topics')).toHaveClass(/active/);

  await page.locator('.topic-row', { hasText: '#squid' }).getByRole('button', { name: 'Open' }).click();
  await expect(page.locator('#view-chat')).toHaveClass(/active/);

  await page.goBack();
  await expect(page.locator('#view-topics')).toHaveClass(/active/);
});

test('topics page refetches route times after sending an adhoc prompt', async ({ page }) => {
  let topics = JSON.parse(JSON.stringify(TOPICS));
  topics[0].agents[0].last_adhoc_prompt = 'old adhoc prompt';
  topics[0].agents[0].last_adhoc_at = '2026-06-12T12:00:00Z';

  await mockBackend(page, () => topics);
  await page.route('**/chat', route => {
    topics = JSON.parse(JSON.stringify(TOPICS));
    topics[0].agents[0].last_adhoc_prompt = 'new adhoc prompt';
    topics[0].agents[0].last_adhoc_at = '2026-06-13T12:00:00Z';
    return route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: 'event: done\\ndata: \\n\\n',
    });
  });

  await page.goto('/');
  await page.getByRole('button', { name: 'Topics' }).click();
  await page.locator('.topic-row', { hasText: '#squid' }).click();
  await expect(page.locator('.topic-agent-row.adhoc .topic-badge.time')).toContainText('Jun 12');

  await page.getByRole('button', { name: 'Chat' }).click();
  await page.fill('#input', '#squid@codex! new adhoc prompt');
  await page.keyboard.press('Enter');
  await page.getByRole('button', { name: 'Topics' }).click();

  await expect(page.locator('.topic-agent-row.adhoc')).toContainText('new adhoc prompt');
  await expect(page.locator('.topic-agent-row.adhoc .topic-badge.time')).toContainText('Jun 13');
});
