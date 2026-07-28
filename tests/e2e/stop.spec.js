/**
 * Stop command and click-to-kill contract tests.
 * Verifies scoped stop (topic / topic@agent / topic@agent!) and
 * per-process kill via the thinking bubble × button.
 */
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

const META = { event: 'meta', data: { agent: 'claude', backend: 'claude', msg_id: 42, adhoc: false } };
const DONE = { event: 'done', data: '' };

async function mockBackend(page, { topic = 'squid', agent = 'claude' } = {}) {
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/history**',     r => r.fulfill({ json: { items: [], has_more: false } }));
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
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));
  await page.route('**/processes',     r => r.fulfill({ json: [] }));
}

// ── tests ─────────────────────────────────────────────────────────────────────

test('#topic /stop sends topic only — kills all agents', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/cmd', r => r.fulfill({ json: { ok: true, killed: 1 } }));

  let cmdBody = null;
  await page.route('**/cmd', async route => {
    cmdBody = route.request().postDataJSON();
    await route.fulfill({ json: { ok: true, killed: 1 } });
  });

  await page.goto('/');
  await page.fill('#input', '#squid /stop');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(300);

  expect(cmdBody.command).toBe('stop');
  expect(cmdBody.topic).toBe('squid');
  expect(cmdBody.agent).toBeFalsy();
  expect(cmdBody.adhoc == null || cmdBody.adhoc === false).toBeTruthy();
});

test('#topic@agent /stop sends agent — scoped to session processes', async ({ page }) => {
  await mockBackend(page);

  let cmdBody = null;
  await page.route('**/cmd', async route => {
    cmdBody = route.request().postDataJSON();
    await route.fulfill({ json: { ok: true, killed: 1 } });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude /stop');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(300);

  expect(cmdBody.command).toBe('stop');
  expect(cmdBody.topic).toBe('squid');
  expect(cmdBody.agent).toBe('claude');
  expect(cmdBody.adhoc).toBeFalsy();
});

test('#topic@agent! /stop sends agent + adhoc=true — LIFO kill of most recent adhoc', async ({ page }) => {
  await mockBackend(page);

  let cmdBody = null;
  await page.route('**/cmd', async route => {
    cmdBody = route.request().postDataJSON();
    await route.fulfill({ json: { ok: true, killed: 1 } });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude! /stop');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(300);

  expect(cmdBody.command).toBe('stop');
  expect(cmdBody.topic).toBe('squid');
  expect(cmdBody.agent).toBe('claude');
  expect(cmdBody.adhoc).toBe(true);
});

test('consecutive #topic@agent! /stop walks LIFO — chip preserves adhoc flag', async ({ page }) => {
  await mockBackend(page);

  const cmdBodies = [];
  await page.route('**/cmd', async route => {
    cmdBodies.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true, killed: 1 } });
  });

  await page.goto('/');

  // First stop
  await page.fill('#input', '#squid@claude! /stop');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);

  // Second stop — chip should still have adhoc=true so this also targets adhoc
  await page.fill('#input', '/stop');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);

  expect(cmdBodies).toHaveLength(2);
  expect(cmdBodies[0].adhoc).toBe(true);
  expect(cmdBodies[1].adhoc).toBe(true);
});

test('#topic@agent! /clear preserves adhoc chip without sending adhoc to clear', async ({ page }) => {
  await mockBackend(page);

  const cmdBodies = [];
  await page.route('**/cmd', async route => {
    cmdBodies.push(route.request().postDataJSON());
    const body = cmdBodies[cmdBodies.length - 1];
    await route.fulfill({ json: body.command === 'clear' ? { ok: true, agent: 'claude' } : { ok: true, killed: 1 } });
  });

  await page.goto('/');

  await page.fill('#input', '#squid@claude! /clear');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);

  await page.fill('#input', '/stop');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);

  expect(cmdBodies).toHaveLength(2);
  expect(cmdBodies[0].command).toBe('clear');
  expect(cmdBodies[0].topic).toBe('squid');
  expect(cmdBodies[0].agent).toBe('claude');
  expect(cmdBodies[0].adhoc).toBeFalsy();
  expect(cmdBodies[1].command).toBe('stop');
  expect(cmdBodies[1].topic).toBe('squid');
  expect(cmdBodies[1].agent).toBe('claude');
  expect(cmdBodies[1].adhoc).toBe(true);
});

test('#topic@agent /clear shows known zero turn count on the chip', async ({ page }) => {
  await mockBackend(page, { topic: 'debug', agent: 'squid' });

  await page.route('**/cmd', async route => {
    const body = route.request().postDataJSON();
    await route.fulfill({ json: { ok: true, agent: body.agent } });
  });

  await page.goto('/');
  await page.fill('#input', '#debug@squid /clear');
  await page.keyboard.press('Enter');

  await expect(page.locator('#topic-chip')).toContainText('#debug@squid');
  await expect(page.locator('#topic-chip .chip-turn-count')).toHaveText('·0t');
});

test('#topic@missing-agent /clear leaves turn count unknown', async ({ page }) => {
  await mockBackend(page, { topic: 'debug', agent: 'codex' });

  await page.route('**/cmd', async route => {
    await route.fulfill({ status: 400, json: { ok: false, error: 'agent not found: squid' } });
  });
  await page.route('**/topics/debug/session?agent=squid', async route => {
    await route.fulfill({ status: 404, json: { error: 'agent not found: squid' } });
  });

  await page.goto('/');
  await page.fill('#input', '#debug@squid /clear');
  await page.keyboard.press('Enter');

  await expect(page.locator('#topic-chip')).toContainText('#debug@squid');
  await expect(page.locator('#topic-chip .chip-turn-count')).toHaveCount(0);
  await expect(page.locator('.cmd-feedback')).toContainText('clear failed: agent not found: squid');
});

test('/clear warns before stopping a running prompt and cancels cleanly', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/processes', r => r.fulfill({ json: [
    { msg_id: 854, topic: 'squid', agent: 'claude', adhoc: false, state: 'running' },
  ] }));

  const cmdBodies = [];
  await page.route('**/cmd', async route => {
    cmdBodies.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true, agent: 'claude' } });
  });
  page.on('dialog', async dialog => {
    expect(dialog.message()).toContain('clear will stop the prompt currently running on #squid@claude');
    await dialog.dismiss();
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude /clear');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);

  expect(cmdBodies).toHaveLength(0);
  await expect(page.locator('.cmd-feedback')).toContainText('clear cancelled');
});

test('/clear warning can be accepted before sending clear command', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/processes', r => r.fulfill({ json: [
    { msg_id: 854, topic: 'squid', agent: 'claude', adhoc: false, state: 'running' },
  ] }));

  const cmdBodies = [];
  await page.route('**/cmd', async route => {
    cmdBodies.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true, agent: 'claude' } });
  });
  page.on('dialog', async dialog => {
    expect(dialog.message()).toContain('clear will stop the prompt currently running on #squid@claude');
    await dialog.accept();
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude /clear');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);

  expect(cmdBodies).toHaveLength(1);
  expect(cmdBodies[0]).toMatchObject({ command: 'clear', topic: 'squid', agent: 'claude' });
});

test('/restart warns when any prompt is running and cancels cleanly', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/processes', r => r.fulfill({ json: [
    { msg_id: 854, topic: 'other', agent: 'codex', adhoc: false, state: 'running', prompt_preview: 'working now' },
  ] }));

  const cmdBodies = [];
  await page.route('**/cmd', async route => {
    cmdBodies.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true } });
  });

  await page.goto('/');
  await page.fill('#input', '/restart');
  await page.keyboard.press('Enter');
  await expect(page.locator('#restart-modal.open')).toBeVisible();
  await expect(page.locator('#restart-modal-processes')).toContainText('#other@codex');
  await expect(page.locator('#restart-modal-processes')).toContainText('working now');
  await page.locator('#restart-modal-cancel').click();

  expect(cmdBodies).toHaveLength(0);
  await expect(page.locator('.cmd-feedback')).toContainText('restart cancelled');
});

test('/restart warning can be accepted before sending restart command', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/processes', r => r.fulfill({ json: [
    { msg_id: 854, topic: 'other', agent: 'codex', adhoc: false, state: 'running', prompt_preview: 'working now' },
  ] }));

  const cmdBodies = [];
  await page.route('**/cmd', async route => {
    cmdBodies.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true } });
  });

  await page.goto('/');
  await page.fill('#input', '/restart');
  await page.keyboard.press('Enter');
  await expect(page.locator('#restart-modal.open')).toBeVisible();
  await page.locator('#restart-modal-confirm').click();
  await page.waitForTimeout(200);

  expect(cmdBodies).toHaveLength(1);
  expect(cmdBodies[0]).toMatchObject({ command: 'restart' });
});

test('kill button appears on thinking bubble and sends stop_msg with msg_id', async ({ page }) => {
  await mockBackend(page);

  let cmdBody = null;
  await page.route('**/cmd', async route => {
    cmdBody = route.request().postDataJSON();
    await route.fulfill({ json: { ok: true, killed: 1 } });
  });

  await page.goto('/');

  // Mock fetch at the JS level so we control a real ReadableStream — Playwright's
  // route.fulfill sends the full body at once, which closes the stream immediately
  // and lets freezeThinking hide the button before we can assert.
  await page.evaluate(() => {
    const orig = window.fetch;
    window.fetch = async (url, opts) => {
      if (!url.includes('/chat')) return orig(url, opts);
      const { readable, writable } = new TransformStream();
      const writer = writable.getWriter();
      const enc = new TextEncoder();
      writer.write(enc.encode(
        'event: meta\ndata: {"agent":"claude","backend":"claude","msg_id":42,"adhoc":false}\n\n'
      ));
      window._testSseWriter = writer; // kept open — freezeThinking never runs
      return new Response(readable, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      });
    };
  });

  await page.fill('#input', '#squid@claude hello');
  await page.keyboard.press('Enter');

  // Kill button is shown after the meta event while stream stays open
  const killBtn = page.locator('.thinking-kill-btn');
  await expect(killBtn).toBeVisible({ timeout: 5000 });

  await killBtn.click();
  await page.waitForTimeout(300);

  expect(cmdBody?.command).toBe('stop_msg');
  expect(cmdBody?.msg_id).toBe(42);

  // Close the held stream so the page can clean up
  await page.evaluate(() => window._testSseWriter?.close().catch(() => {}));
});

test('queued prompt kill leaves dequeued label', async ({ page }) => {
  await mockBackend(page);

  let cmdBody = null;
  await page.route('**/cmd', async route => {
    cmdBody = route.request().postDataJSON();
    await route.fulfill({ json: { ok: true, drained: 1 } });
  });

  await page.goto('/');
  await page.evaluate(() => {
    const orig = window.fetch;
    window.fetch = async (url, opts) => {
      if (!url.includes('/chat')) return orig(url, opts);
      const { readable, writable } = new TransformStream();
      const writer = writable.getWriter();
      const enc = new TextEncoder();
      writer.write(enc.encode(
        'event: queued\ndata: {"topic":"squid","position":1}\n\n'
      ));
      window._testSseWriter = writer;
      return new Response(readable, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      });
    };
  });

  await page.fill('#input', '#squid@claude wait behind current work');
  await page.keyboard.press('Enter');

  const killBtn = page.locator('.thinking-kill-btn');
  await expect(killBtn).toBeVisible({ timeout: 5000 });
  await killBtn.click();

  expect(cmdBody).toMatchObject({ command: 'deq', topic: 'squid', pos: 1 });
  await expect(page.locator('.msg-thinking')).toContainText('Dequeued.');

  await page.evaluate(() => window._testSseWriter?.close().catch(() => {}));
});
