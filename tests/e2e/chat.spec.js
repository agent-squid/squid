const { test, expect } = require('@playwright/test');

// ── SSE helpers ───────────────────────────────────────────────────────────────

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

const META  = { event: 'meta',  data: { agent: 'claude', backend: 'claude', msg_id: 1, adhoc: false } };
const STATS = { event: 'stats', data: { session_id: 'test-sid', input_tokens: 10, output_tokens: 5, adhoc: false, lookback: 0 } };
const DONE  = { event: 'done',  data: '' };

// ── mock setup ────────────────────────────────────────────────────────────────

async function mockBackend(page) {
  // Compatibility-path tests must not also attach the real global WebSocket
  // lifecycle. Dedicated WebSocket tests override this route explicitly.
  await page.route('**/config/realtime', r => r.fulfill({ json: { transport: 'sse' } }));
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok', boot_time: new Date().toISOString() } }));
  await page.route('**/history**',     r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**',       r => r.fulfill({ json: {} }));
  await page.route('**/topics',        r => r.fulfill({ json: [] }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic: 'squid', exists: true, content: '---\nsquid:\n  code_roots_skipped: true\n---\n', path: '~/.squid/context/topics/squid/memory.md',
    squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
  }}));
  await page.route('**/topics/**',     r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));
}

// Returns { intercepted, fulfill } — intercepted resolves when the /chat
// request is received; calling fulfill(body) sends the SSE response.
function holdChat(page) {
  let _fulfill;
  const intercepted = new Promise(resolve => {
    page.route('**/chat', route => {
      _fulfill = body => route.fulfill({ status: 200, headers: SSE_HEADERS, body });
      resolve();
    });
  });
  return { intercepted, fulfill: body => _fulfill(body) };
}

async function sendMsg(page, text = 'hello') {
  await page.fill('#input', text);
  await page.keyboard.press('Enter');
}

async function setPageHidden(page, hidden) {
  await page.evaluate(hiddenValue => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: hiddenValue });
    document.dispatchEvent(new Event('visibilitychange'));
  }, hidden);
}

// Pause so you can see what's on screen before moving on
const look = (page, ms = 2500) => page.waitForTimeout(ms);

// ── selectors ─────────────────────────────────────────────────────────────────

const THINKING  = '.msg.assistant.msg-thinking';
const RESPONSE  = '.msg.assistant:not(.msg-thinking)';
const MSG_ERROR = '.msg-error';

// ── tests ─────────────────────────────────────────────────────────────────────

test('websocket transport starts and completes a new chat without POST /chat', async ({ page }) => {
  await page.addInitScript(() => {
    window.__chatStartFrame = null;
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'chat.start') {
          window.__chatStartFrame = frame;
          setTimeout(() => {
            this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
              payload: { ok: true, msg_id: 83, flow_run_id: null } });
            setTimeout(() => {
              this.receive({ v: 1, type: 'chat.text', event_id: 1, msg_id: 83, run_seq: 0,
                payload: { text: 'WebSocket response' } });
              this.receive({ v: 1, type: 'chat.done', event_id: 2, msg_id: 83, run_seq: 1, payload: {} });
            });
          });
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
  let httpChatRequests = 0;
  await page.route('**/chat', route => {
    httpChatRequests++;
    return route.abort();
  });
  await page.route('**/chat/83/status', route => route.fulfill({ json: {
    id: 83,
    role: 'assistant',
    reply_to: 82,
    topic: 'default',
    agent: 'claude',
    backend: 'claude',
    adhoc: false,
    status: 'done',
    prompt: 'hello over ws',
    content: 'WebSocket response',
    completed_at: new Date().toISOString(),
  }}));

  await page.goto('/');
  await sendMsg(page, 'hello over ws');

  await expect(page.locator(RESPONSE).filter({ hasText: 'WebSocket response' })).toBeVisible();
  expect(httpChatRequests).toBe(0);
  expect(await page.evaluate(() => ({
    type: window.__chatStartFrame?.type,
    hasRequestId: !!window.__chatStartFrame?.request_id,
    message: window.__chatStartFrame?.payload?.message,
  }))).toEqual({ type: 'chat.start', hasRequestId: true, message: 'hello over ws' });
});

test('websocket auto-resolve and worktree events converge the rendered diff', async ({ page }) => {
  await page.addInitScript(() => {
    window.__autoResolveFrame = null;
    window.__activeRealtimeSocket = null;
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        window.__activeRealtimeSocket = this;
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'worktree.auto_resolve') {
          window.__autoResolveFrame = frame;
          // Force global discovery to win the race with command.result.
          this.receive({ v: 1, type: 'message.changed', event_id: 1, msg_id: 91,
            scope: { topic: 'default', agent: 'claude' },
            payload: { role: 'assistant', status: 'pending' } });
        }
      }
      finishAutoResolveCommand() {
        const frame = window.__autoResolveFrame;
        this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
          payload: { ok: true, msg_id: 91, worktree_msg_id: 1,
            agent: 'claude', provider: 'anthropic' } });
      }
      completeAutoResolve() {
        this.receive({ v: 1, type: 'chat.text', event_id: 2, msg_id: 91, run_seq: 0,
          payload: { text: 'Resolved over WebSocket' } });
        this.receive({ v: 1, type: 'chat.done', event_id: 3, msg_id: 91, run_seq: 1, payload: {} });
        this.receive({ v: 1, type: 'worktree.changed', event_id: 4, msg_id: 1,
          payload: { repo: '/tmp/repo/', status: 'resolved' } });
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
  await page.route('**/history**', route => route.fulfill({ json: {
    items: [{
      id: 1, topic: 'default', agent: 'claude', backend: 'claude', status: 'done',
      prompt: 'change app', content: 'Original response', timestamp: new Date().toISOString(), adhoc: false,
      context: JSON.stringify([{
        name: 'GitDiff', repo: '/tmp/repo', worktree_repo: '/tmp/worktree', worktree_status: 'conflict',
        worktree_conflicts: ['ui/app.js'], integration_worktree_path: '/tmp/integration',
        file_count: 1, additions: 1, deletions: 0, files: [{ status: 'M', path: 'ui/app.js' }],
        diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+changed',
      }]),
    }],
    has_more: false,
  }}));
  let revertStatus = 'revertable';
  await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
    json: { 'ui/app.js': revertStatus },
  }));
  let resolveStatus = 'pending';
  await page.route('**/chat/91/status', route => route.fulfill({ json: {
    id: 91, role: 'assistant', topic: 'default', agent: 'claude', backend: 'claude', adhoc: true,
    source: 'diff_viewer', status: resolveStatus, prompt: 'Auto-resolve merge conflict',
    content: resolveStatus === 'done' ? 'Resolved over WebSocket' : '',
    completed_at: resolveStatus === 'done' ? new Date().toISOString() : null,
  }}));
  let autoResolveHttpRequests = 0;
  await page.route('**/chat/1/worktree/auto-resolve', route => {
    autoResolveHttpRequests++;
    route.abort();
  });

  await page.goto('/');
  const block = page.locator('.tool-block-history').first();
  await block.getByRole('button', { name: 'Auto-Resolve' }).click({ noWaitAfter: true });
  // message.changed discovers the pending assistant before command.result.
  await expect(page.locator(`${THINKING}[data-msg-id="91"]`)).toHaveAttribute('data-agent', 'claude');
  await page.evaluate(() => window.__activeRealtimeSocket.finishAutoResolveCommand());
  const resolveUser = page.locator('.msg.user').filter({ hasText: 'Auto-resolve merge conflict' });
  await expect(resolveUser).toHaveCount(1);
  expect(await resolveUser.evaluate(el => {
    const messageSiblings = [...el.parentElement.children].filter(child => child.classList.contains('msg'));
    return messageSiblings[messageSiblings.indexOf(el) + 1]?.dataset.msgId;
  })).toBe('91');
  await expect(page.locator(`[data-msg-id="91"]`)).toHaveCount(1);
  resolveStatus = 'done';
  await page.evaluate(() => window.__activeRealtimeSocket.completeAutoResolve());
  await expect(page.locator(RESPONSE).filter({ hasText: 'Resolved over WebSocket' })).toBeVisible();
  await expect(block.locator('.tool-toggle')).toContainText('Conflict Resolved');
  await expect(block.getByRole('button', { name: 'revert' })).toBeVisible();
  expect(autoResolveHttpRequests).toBe(0);
  expect(await page.evaluate(() => ({
    type: window.__autoResolveFrame?.type,
    payload: window.__autoResolveFrame?.payload,
    hasRequestId: !!window.__autoResolveFrame?.request_id,
  }))).toEqual({
    type: 'worktree.auto_resolve',
    payload: { msg_id: 1, topic: 'default', repo: '/tmp/repo' },
    hasRequestId: true,
  });

  revertStatus = 'reverted';
  await page.evaluate(() => window.__activeRealtimeSocket.receive({
    v: 1, type: 'diff.reverted', event_id: 5, msg_id: 1,
    payload: { repo: '/tmp/repo', files: ['ui/app.js'] },
  }));
  await expect(block.locator('.gitdiff-file-row')).toHaveClass(/gitdiff-file-row--reverted/);
  await expect(block.getByRole('button', { name: 'revert' })).toHaveCount(0);
});

test('websocket tall response scrolls to reveal its top even if the reveal rAF fires late', async ({ page }) => {
  // sendMessage() schedules a one-shot requestAnimationFrame right when the
  // thinking bubble is created, to follow *it* into view. It's guarded by
  // thinkingFrozen so a late-firing callback (completion already rendered
  // and positioned the response bubble by the time the browser gets around
  // to it) no-ops instead of stomping that positioning back to the literal
  // bottom. Hold every rAF scheduled from send-time onward so this test
  // controls exactly when that callback fires, instead of hoping real
  // scheduling exposes the race — a passing test here means the guard
  // actually works, not that the mock happened to be fast enough.
  await page.addInitScript(() => {
    window.__heldRafs = [];
    window.__holdRafs = false;
    const realRaf = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = cb => {
      if (window.__holdRafs) { window.__heldRafs.push(cb); return window.__heldRafs.length; }
      return realRaf(cb);
    };
    window.__flushRafs = () => { window.__heldRafs.splice(0).forEach(cb => cb()); };

    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'chat.start') {
          setTimeout(() => {
            this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
              payload: { ok: true, msg_id: 85, flow_run_id: null } });
            setTimeout(() => {
              this.receive({ v: 1, type: 'chat.done', event_id: 1, msg_id: 85, run_seq: 1, payload: {} });
            });
          });
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
  const longContent = Array.from({ length: 200 }, (_, i) =>
    `Paragraph sentence number ${i + 1} with several words to force wrapping across many lines in the bubble.`
  ).join(' ');
  await page.route('**/chat/85/status', route => route.fulfill({ json: {
    id: 85, role: 'assistant', reply_to: 84, topic: 'default', agent: 'claude', backend: 'claude',
    adhoc: false, status: 'done', prompt: 'hello over ws', content: longContent,
    completed_at: new Date().toISOString(),
  }}));

  await page.goto('/');
  await page.evaluate(() => { window.__holdRafs = true; });
  await sendMsg(page, 'hello over ws');

  const response = page.locator(RESPONSE).filter({ hasText: 'Paragraph sentence number 1 ' });
  await expect(response).toBeVisible();

  const readPositions = () => page.evaluate(() => {
    const el = document.querySelector('.msg.assistant:not(.msg-thinking)');
    const container = document.getElementById('messages');
    return {
      bubbleTop: el.getBoundingClientRect().top,
      messagesTop: container.getBoundingClientRect().top,
      atBottom: container.scrollHeight - container.scrollTop - container.clientHeight < 150,
    };
  });

  const before = await readPositions();
  expect(Math.abs(before.bubbleTop - before.messagesTop)).toBeLessThan(2);
  expect(before.atBottom).toBe(false);

  // Flush the held rAFs, including the stale "reveal thinking bubble" one
  // queued at send time — confirm it doesn't override the positioning above.
  await page.evaluate(() => window.__flushRafs());

  const after = await readPositions();
  expect(Math.abs(after.bubbleTop - after.messagesTop)).toBeLessThan(2);
});

test('websocket processing event starts the native shell timeout clock', async ({ page }) => {
  await page.addInitScript(() => {
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'chat.start') {
          setTimeout(() => {
            this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
              payload: { ok: true, msg_id: 84, flow_run_id: null } });
            setTimeout(() => this.receive({
              v: 1, type: 'chat.processing', event_id: 1, msg_id: 84, run_seq: 0,
              payload: { topic: 'default' },
            }));
          });
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));

  await page.goto('/');
  await sendMsg(page, '! top');

  const status = page.locator('.shell-running-status');
  await expect(status).toContainText('Running · timeout in 2:00');
  await expect(status).toContainText('Running · timeout in 1:59', { timeout: 2500 });
});

test('websocket transport cancels a running chat without POST /cmd', async ({ page }) => {
  await page.addInitScript(() => {
    window.__chatCancelFrame = null;
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'chat.start') {
          setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
            payload: { ok: true, msg_id: 85, flow_run_id: null } }));
        } else if (frame.type === 'chat.cancel') {
          window.__chatCancelFrame = frame;
          setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
            payload: { ok: true, cancelled: true, killed: true, msg_id: 85 } }));
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
  let cmdRequests = 0;
  await page.route('**/cmd', route => { cmdRequests++; return route.abort(); });

  await page.goto('/');
  await sendMsg(page, 'cancel over ws');
  await expect(page.locator(THINKING).locator('.thinking-kill-btn')).toBeVisible();
  await page.locator(THINKING).locator('.thinking-kill-btn').click();

  await expect(page.locator(THINKING)).toContainText('Cancelled.');
  expect(cmdRequests).toBe(0);
  expect(await page.evaluate(() => ({
    type: window.__chatCancelFrame?.type,
    hasRequestId: !!window.__chatCancelFrame?.request_id,
    msgId: window.__chatCancelFrame?.payload?.msg_id,
  }))).toEqual({ type: 'chat.cancel', hasRequestId: true, msgId: 85 });
});

test('auto transport falls back to POST /cmd when websocket cancel is unavailable', async ({ page }) => {
  await page.addInitScript(() => {
    class FailedWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = FailedWebSocket.CONNECTING;
        setTimeout(() => { this.readyState = 3; this.onclose?.(); });
      }
      close() {}
    }
    window.WebSocket = FailedWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'auto' } }));
  let cmdBody = null;
  await page.route('**/cmd', async route => {
    cmdBody = route.request().postDataJSON();
    await route.fulfill({ json: { ok: true, killed: 1 } });
  });

  await page.goto('/');
  expect(await page.evaluate(() => cancelRealtimeMessage(86, 'squid', 'codex'))).toBe(true);
  expect(cmdBody).toEqual({ command: 'stop_msg', topic: 'squid', msg_id: 86 });
});

test('auto transport falls back to POST /cmd when websocket cancel times out after send', async ({ page }) => {
  await page.addInitScript(() => {
    class UnacknowledgedCancelWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = UnacknowledgedCancelWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = UnacknowledgedCancelWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        }
        // The server receives chat.cancel but its command.result is lost.
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = UnacknowledgedCancelWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'auto' } }));
  let cmdBody = null;
  await page.route('**/cmd', async route => {
    cmdBody = route.request().postDataJSON();
    await route.fulfill({ json: { ok: true, killed: 1 } });
  });

  await page.goto('/');
  expect(await page.evaluate(() => cancelRealtimeMessage(88, 'squid', 'codex'))).toBe(true);
  expect(cmdBody).toEqual({ command: 'stop_msg', topic: 'squid', msg_id: 88 });
});

test('failed websocket cancel keeps the running chat retryable and shows the error', async ({ page }) => {
  await page.addInitScript(() => {
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'chat.start') {
          setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
            payload: { ok: true, msg_id: 87, flow_run_id: null } }));
        } else if (frame.type === 'chat.cancel') {
          setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
            payload: { ok: false, error: 'cancel rejected by server' } }));
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));

  await page.goto('/');
  await sendMsg(page, 'cancel should fail visibly');
  const thinking = page.locator(THINKING);
  const killBtn = thinking.locator('.thinking-kill-btn');
  await expect(killBtn).toBeVisible();
  await killBtn.click();

  await expect(thinking.locator('.cancel-error')).toHaveText('cancel rejected by server');
  await expect(killBtn).toBeEnabled();
  await expect(thinking).toBeVisible();
});

test('websocket submission sends an attachment once without rewriting the displayed prompt', async ({ page }) => {
  await page.addInitScript(() => {
    window.__chatStartFrames = [];
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'chat.start') {
          window.__chatStartFrames.push(frame);
          const msgId = 90 + window.__chatStartFrames.length;
          setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
            payload: { ok: true, msg_id: msgId, flow_run_id: null } }));
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
  await page.route('**/localfile/check-paths', async route => {
    const { paths } = route.request().postDataJSON();
    await route.fulfill({ json: { paths: paths.map(path => ({ path, resolved_path: path, exists: true, is_file: true })) } });
  });

  await page.goto('/');
  await page.evaluate(() => {
    localStorage.setItem('attachedFiles', JSON.stringify([{ path: '/tmp/screenshot.png', name: 'screenshot.png' }]));
    updatePinCount();
  });
  await sendMsg(page, '#squid@claude first prompt');
  await page.waitForFunction(() => window.__chatStartFrames.length === 1);
  await expect(page.locator('#pin-btn')).toHaveClass(/has-context-pending/);
  await sendMsg(page, '#squid@claude second prompt');
  await page.waitForFunction(() => window.__chatStartFrames.length === 2);

  const payloads = await page.evaluate(() => window.__chatStartFrames.map(frame => frame.payload));
  expect(payloads[0].message).toBe('first prompt');
  expect(payloads[0].attached_paths).toEqual(['/tmp/screenshot.png']);
  expect(payloads[1].message).toBe('#squid@claude second prompt');
  expect(payloads[1].attached_paths).toBeUndefined();
});

test('auto transport falls back to POST /chat when websocket fails before subscribe', async ({ page }) => {
  await page.addInitScript(() => {
    class FailedWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = FailedWebSocket.CONNECTING;
        setTimeout(() => { this.readyState = 3; this.onclose?.(); });
      }
      close() {}
    }
    window.WebSocket = FailedWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'auto' } }));
  let httpChatRequests = 0;
  await page.route('**/chat', route => {
    httpChatRequests++;
    return route.fulfill({ status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'SSE fallback response' }, DONE) });
  });

  await page.goto('/');
  await sendMsg(page, 'fallback safely');

  await expect(page.locator(RESPONSE)).toContainText('SSE fallback response');
  expect(httpChatRequests).toBe(1);
});

test('auto transport falls back when websocket opens but never subscribes', async ({ page }) => {
  test.setTimeout(15_000);
  await page.addInitScript(() => {
    class StalledWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = StalledWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = StalledWebSocket.OPEN;
          this.onopen?.();
        });
      }
      send() {}
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = StalledWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'auto' } }));
  let httpChatRequests = 0;
  await page.route('**/chat', route => {
    httpChatRequests++;
    return route.fulfill({ status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'SSE timeout fallback' }, DONE) });
  });

  await page.goto('/');
  await sendMsg(page, 'fallback after timeout');

  await expect(page.locator(RESPONSE)).toContainText('SSE timeout fallback', { timeout: 10_000 });
  expect(httpChatRequests).toBe(1);
});

test('auto transport does not resubmit a command that times out after send', async ({ page }) => {
  test.setTimeout(15_000);
  await page.addInitScript(() => {
    class StalledResultWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = StalledResultWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = StalledResultWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = StalledResultWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'auto' } }));
  let httpChatRequests = 0;
  await page.route('**/chat', route => {
    httpChatRequests++;
    return route.abort();
  });

  await page.goto('/');
  await sendMsg(page, 'do not duplicate after timeout');

  await expect(page.locator(MSG_ERROR)).toContainText('timed out after submission', { timeout: 10_000 });
  expect(httpChatRequests).toBe(0);
});

test('auto transport does not resubmit a rejected websocket command over HTTP', async ({ page }) => {
  await page.addInitScript(() => {
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'chat.start') {
          setTimeout(() => this.receive({
            v: 1, type: 'command.result', request_id: frame.request_id,
            payload: {
              ok: false, status: 409, msg_id: 84,
              error: 'worktree sync requires attention',
              worktrees: [{ repo_root: '/repo', worktree_path: '/worktree', status: 'pending' }],
            },
          }));
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'auto' } }));
  let httpChatRequests = 0;
  await page.route('**/chat', route => {
    httpChatRequests++;
    return route.abort();
  });

  await page.goto('/');
  await sendMsg(page, 'blocked once');

  await expect(page.locator('.msg-error')).toContainText('worktree sync requires attention');
  await expect(page.locator('.tool-block')).toHaveCount(1);
  expect(httpChatRequests).toBe(0);
});

test.describe('response bubble', () => {
  test.beforeEach(async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
  });

  test('does not appear in DOM before done', async ({ page }) => {
    const { intercepted, fulfill } = holdChat(page);

    await sendMsg(page);
    await intercepted;

    // ── LOOK: thinking bubble visible, response bubble absent ────────────────
    await expect(page.locator(THINKING)).toBeVisible();
    await expect(page.locator(RESPONSE)).not.toBeAttached();
    await look(page);  // pause — observe: only thinking bubble, no response bubble yet

    await fulfill(sse(META, { data: 'Hello!' }, DONE));

    await expect(page.locator(RESPONSE)).toBeVisible();
    await look(page);  // pause — observe: response bubble now at bottom
  });

  test('thinking bubble height can be doubled', async ({ page }) => {
    const { intercepted } = holdChat(page);

    await sendMsg(page);
    await intercepted;

    const thinking = page.locator(THINKING);
    const live = thinking.locator('.thinking-live');
    const normalMax = await live.evaluate(el => parseFloat(getComputedStyle(el).maxHeight));
    const heightBtn = thinking.getByRole('button', { name: 'Double thinking height' });
    await expect(heightBtn).not.toBeVisible();
    await thinking.evaluate(el => {
      const liveEl = el.querySelector('.thinking-live');
      liveEl.textContent = Array.from({ length: 16 }, (_, i) => `thinking line ${i + 1}`).join('\n');
      window.updateThinkingHeightButton(el);
    });
    await expect(heightBtn).toBeVisible();
    expect(await thinking.evaluate(el => {
      const bubble = el.getBoundingClientRect();
      const btn = el.querySelector('.thinking-height-btn').getBoundingClientRect();
      return btn.bottom <= bubble.bottom && btn.bottom >= bubble.bottom - 12 && btn.right <= bubble.right;
    })).toBe(true);
    await heightBtn.click();

    await expect(thinking).toHaveClass(/thinking-tall/);
    await expect(thinking.getByRole('button', { name: 'Normal thinking height' })).toBeVisible();
    const tallMax = await live.evaluate(el => parseFloat(getComputedStyle(el).maxHeight));
    expect(tallMax).toBeGreaterThan(normalMax * 1.8);
  });

  test('appears at bottom of #messages on done', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Response text' }, STATS, DONE),
    }));

    await sendMsg(page);
    await expect(page.locator(RESPONSE)).toBeVisible();

    const last = page.locator('#messages > *').last();
    await expect(last).toHaveClass(/stats/);
    await look(page);  // pause — observe: stats line is last child, bubble above it
  });

  test('stats footer shows unknown tokens as em-dash instead of zero', async ({ page }) => {
    // A stats event with no token fields: the backend didn't record usage, so
    // the footer must not invent "0 tokens in/out" — it shows "—" instead.
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Response text' }, { event: 'stats', data: { session_id: 'test-sid', duration_ms: 500 } }, DONE),
    }));

    await sendMsg(page);
    const stats = page.locator('#messages > .stats');
    await expect(stats).toBeVisible();
    await expect(stats).toContainText('↑ —');
    await expect(stats).toContainText('↓ —');
    await expect(stats).not.toContainText('↑ 0');
    await expect(stats).not.toContainText('↓ 0');
  });

  test('response taller than the viewport scrolls to reveal its top, not its tail', async ({ page }) => {
    const longText = Array.from({ length: 200 }, (_, i) =>
      `Paragraph sentence number ${i + 1} with several words to force wrapping across many lines in the bubble.`
    ).join(' ');
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: longText }, DONE),
    }));

    await sendMsg(page);
    const response = page.locator(RESPONSE);
    await expect(response).toBeVisible();
    await expect(response).toContainText('Paragraph sentence number 1 ');

    // Landing at the literal bottom would skip past the whole response,
    // requiring a scroll back up to read it. Land on its top instead, so
    // scrolling down keeps reading in the natural direction.
    const { bubbleTop, messagesTop, atBottom } = await page.evaluate(() => {
      const el = document.querySelector('.msg.assistant:not(.msg-thinking)');
      const container = document.getElementById('messages');
      return {
        bubbleTop: el.getBoundingClientRect().top,
        messagesTop: container.getBoundingClientRect().top,
        atBottom: container.scrollHeight - container.scrollTop - container.clientHeight < 150,
      };
    });
    expect(Math.abs(bubbleTop - messagesTop)).toBeLessThan(2);
    expect(atBottom).toBe(false);
    await look(page);  // pause — observe: top of the long response visible, not its tail
  });

  test('renders response tildes literally instead of strikethrough', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(META, { data: 'Use ~/Work/squid and ~~do not strike~~ here.' }, DONE),
    }));

    await sendMsg(page);
    const response = page.locator(RESPONSE);
    await expect(response).toContainText('Use ~/Work/squid and ~~do not strike~~ here.');
    await expect(response.locator('del')).toHaveCount(0);
  });

  test('context indicator exposes the Squid message ID', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Response text' }, STATS, DONE),
    }));

    await sendMsg(page);
    const ctx = page.locator(RESPONSE).locator('.user-ctx');
    await expect(ctx).toHaveText(/^ctx:/);
    await expect(ctx).not.toContainText('#1');

    await ctx.click();
    await expect(page.locator('#ctx-popup')).toContainText('message#1');
  });

  test('clicking a header route sets composer route without filtering history', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Hello' }, DONE),
    }));

    await sendMsg(page, '#squid@claude remember this route');
    await expect(page.locator(`${RESPONSE} .response-header .tag-topic`)).not.toHaveClass(/clickable/);
    await expect(page.locator(`${RESPONSE} .response-header .tag-agent`)).not.toHaveClass(/clickable/);
    let historyReloads = 0;
    await page.route('**/history**', r => {
      historyReloads++;
      return r.fulfill({ json: { items: [], has_more: false } });
    });
    await page.locator(`${RESPONSE} .response-header .tag-topic`).click();
    await page.waitForTimeout(300);

    await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
    await expect(page.locator('#topic-chip')).toContainText('#squid');
    await expect(page.locator('#topic-chip')).toContainText('@claude');
    await expect(page.locator('#filter-badge')).not.toHaveClass(/active/);
    await expect(page.locator(RESPONSE)).toContainText('Hello');
    expect(historyReloads).toBe(0);
  });

  test('filter round-trip keeps an older live prompt above newer completed history', async ({ page }) => {
    await page.addInitScript(() => {
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
          });
        }
        send(data) {
          const frame = JSON.parse(data);
          if (frame.type === 'subscribe') {
            setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
          } else if (frame.type === 'chat.start') {
            // The live prompt gets msg_id 5, older than the completed history
            // below. Never send chat.done — the turn stays in flight.
            setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
              payload: { ok: true, msg_id: 5, flow_run_id: null } }));
          }
        }
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() { this.readyState = 3; this.onclose?.(); }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    // Completed messages newer than the in-flight prompt (msg 5).
    await page.route('**/history**', route => route.fulfill({ json: {
      items: [
        { id: 7, role: 'assistant', topic: 'squid', agent: 'claude', status: 'done', content: 'Done 7', prompt: 'p7', timestamp: '2026-08-15T12:00:00Z' },
        { id: 6, role: 'assistant', topic: 'squid', agent: 'claude', status: 'done', content: 'Done 6', prompt: 'p6', timestamp: '2026-08-15T11:00:00Z' },
      ],
      has_more: false,
    }}));

    await page.goto('/');
    await sendMsg(page, 'slow live prompt');
    await expect(page.locator('.msg.assistant.msg-thinking[data-msg-id="5"]')).toBeAttached();

    // Toggle a filter on and back off. reloadHistory() preserves the live group
    // but clears completed history, which loadHistory() must re-anchor below the
    // older live prompt — not above it.
    await page.evaluate(() => applyHistoryFilter({ topic: 'squid', agent: null, adhoc: null, flow_route: null }));
    await page.evaluate(() => clearFilter());
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="6"]')).toBeAttached({ timeout: 5_000 });
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="7"]')).toBeAttached({ timeout: 5_000 });

    const order = await page.locator('#messages > .msg.assistant[data-msg-id]').evaluateAll(
      nodes => nodes.map(node => Number(node.dataset.msgId)),
    );
    expect(order).toEqual([5, 6, 7]);
  });

  test('filter round-trip does not flip a completed response above its own user prompt', async ({ page }) => {
    await page.addInitScript(() => {
      let startCount = 0;
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__mockWs = this;
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
          });
        }
        send(data) {
          const frame = JSON.parse(data);
          if (frame.type === 'subscribe') {
            setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
          } else if (frame.type === 'chat.start') {
            startCount++;
            const msgId = startCount === 1 ? 6 : 7;
            setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
              payload: { ok: true, msg_id: msgId, flow_run_id: null } }));
          }
        }
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() { this.readyState = 3; this.onclose?.(); }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    await page.route('**/chat/6/status', route => route.fulfill({ json: {
      id: 6, role: 'assistant', reply_to: 5, topic: 'default', agent: 'claude', backend: 'claude',
      adhoc: false, status: 'done', prompt: "what's your model?", content: 'Done 6',
      completed_at: '2026-08-15T12:00:00Z',
    }}));
    let exposeHistory = false;
    await page.route('**/history**', route => route.fulfill({ json: {
      items: exposeHistory ? [{
        id: 6, role: 'assistant', topic: 'default', agent: 'claude', status: 'done',
        content: 'Done 6', prompt: "what's your model?", timestamp: '2026-08-15T12:00:00Z',
      }] : [],
      has_more: false,
    }}));

    await page.goto('/');
    await sendMsg(page, "what's your model?");
    await expect(page.locator('.msg.assistant.msg-thinking[data-msg-id="6"]')).toBeAttached();
    await sendMsg(page, 'hi');
    await expect(page.locator('.msg.assistant.msg-thinking[data-msg-id="7"]')).toBeAttached();

    // Complete msg 6 *after* msg 7 has started, so its completed response is
    // inserted below the still-live msg 7 group and the two user bubbles end up
    // adjacent with no assistant element between them.
    await page.evaluate(() => {
      window.__mockWs.receive({ v: 1, type: 'chat.text', event_id: 1, msg_id: 6, run_seq: 0, payload: { text: 'Done 6' } });
      window.__mockWs.receive({ v: 1, type: 'chat.done', event_id: 2, msg_id: 6, run_seq: 1, payload: {} });
    });
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="6"]')).toBeAttached({ timeout: 5_000 });

    const preOrder = await page.locator('#messages > .msg.assistant[data-msg-id]').evaluateAll(
      nodes => nodes.map(node => Number(node.dataset.msgId)),
    );
    expect(preOrder).toEqual([7, 6]);

    exposeHistory = true;
    await page.evaluate(() => applyHistoryFilter({ topic: 'squid', agent: null, adhoc: null, flow_route: null }));
    await page.evaluate(() => clearFilter());
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="6"]')).toBeAttached({ timeout: 5_000 });

    const order = await page.locator('#messages > .msg.assistant[data-msg-id]').evaluateAll(
      nodes => nodes.map(node => Number(node.dataset.msgId)),
    );
    expect(order).toEqual([6, 7]);
    // The completed turn's standalone user bubble must be gone — its prompt is
    // embedded in the re-fetched history item. A leftover "what's your model?"
    // bubble here is the flipped "response first, then user prompt" symptom.
    const userTexts = await page.locator('#messages > .msg.user').evaluateAll(
      nodes => nodes.map(node => node.textContent.trim()),
    );
    expect(userTexts).toHaveLength(1);
    expect(userTexts[0]).toContain('hi');
    expect(userTexts[0]).not.toContain("what's your model?");
  });

  test('filter round-trip keeps the newest live prompt at the bottom when history includes it as pending', async ({ page }) => {
    // The real /history endpoint returns the still-running turn as a pending row
    // (its completed_at falls back to created_at, the newest timestamp), so it
    // sorts to the top of the page. Anchoring to that pending row must NOT treat
    // the live group as "not newer than the page" and push completed history
    // below it — the running prompt has the latest timestamp and stays at the
    // bottom.
    await mockBackend(page);
    let exposeHistory = false;
    await page.route('**/history**', route => route.fulfill({ json: {
      items: exposeHistory ? [
        { id: 8, role: 'assistant', topic: 'squid', agent: 'deepseek', status: 'pending', content: '', prompt: 'running', timestamp: '2026-08-15T12:00:00Z', completed_at: '2026-08-15T12:00:00Z' },
        { id: 7, role: 'assistant', topic: 'squid', agent: 'opencode', status: 'done', content: 'Pushed', prompt: 'push', timestamp: '2026-08-15T11:30:00Z', completed_at: '2026-08-15T11:30:00Z' },
        { id: 6, role: 'assistant', topic: 'squid', agent: 'deepseek', status: 'done', content: 'Hi', prompt: 'say hi', timestamp: '2026-08-15T11:00:00Z', completed_at: '2026-08-15T11:00:00Z' },
        { id: 5, role: 'assistant', topic: 'squid', agent: 'deepseek', status: 'done', content: 'Hi', prompt: 'say hi', timestamp: '2026-08-15T11:00:00Z', completed_at: '2026-08-15T11:00:00Z' },
      ] : [],
      has_more: false,
    }}));

    await page.goto('/');

    let chatReq = 0;
    const prompts = ['say hi', 'say hi', 'push', 'running'];
    const agents = ['deepseek', 'deepseek', 'opencode', 'deepseek'];
    await page.route('**/chat', route => {
      chatReq++;
      const id = chatReq; // 1-based turn -> msg_id 5..8 below
      const meta = { event: 'meta', data: { agent: agents[id - 1] ?? 'deepseek', backend: 'claude', msg_id: id + 4, adhoc: true } };
      // Turns 5, 6, 7 complete; turn 8 stays in flight (no done).
      if (chatReq <= 3) {
        return route.fulfill({ status: 200, headers: SSE_HEADERS, body: sse(meta, { data: 'done-' + (id + 4) }, DONE) });
      }
      return route.fulfill({ status: 200, headers: SSE_HEADERS, body: sse(meta, { data: 'streaming...' }) });
    });

    await sendMsg(page, 'say hi');
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="5"]')).toBeAttached();
    await sendMsg(page, 'say hi');
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="6"]')).toBeAttached();
    await sendMsg(page, 'push');
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="7"]')).toBeAttached();
    await sendMsg(page, 'running');
    await expect(page.locator('.msg.assistant.msg-thinking[data-msg-id="8"]')).toBeAttached();

    exposeHistory = true;
    await page.evaluate(() => applyHistoryFilter({ topic: 'squid', agent: null, adhoc: null, flow_route: null }));
    await page.evaluate(() => clearFilter());
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="5"]')).toBeAttached({ timeout: 5_000 });
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="6"]')).toBeAttached({ timeout: 5_000 });
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="7"]')).toBeAttached({ timeout: 5_000 });

    const order = await page.locator('#messages > .msg.assistant[data-msg-id]').evaluateAll(
      nodes => nodes.map(node => Number(node.dataset.msgId)),
    );
    expect(order).toEqual([5, 6, 7, 8]);

    // The live group keeps its standalone user bubble; completed turns re-fetch
    // with the prompt embedded, so no leftover "say hi"/"push" user bubbles.
    const userTexts = await page.locator('#messages > .msg.user').evaluateAll(
      nodes => nodes.map(node => node.textContent.trim()),
    );
    expect(userTexts).toHaveLength(1);
    expect(userTexts[0]).toContain('running');
  });

  test('context indicator shows compact session turn, memory, and pin counts', async ({ page }) => {
    await page.route('**/topics/*/memory', r => r.fulfill({ json: {
      topic: 'squid',
      exists: true,
      content: 'Project preference',
      path: '~/.squid/context/topics/squid/memory.md',
    }}));
    await page.route('**/topics/squid/session?agent=claude', r => r.fulfill({ json: { session_id: null, cwd: null } }));
    await page.evaluate(() => {
      localStorage.setItem('pinnedItems', JSON.stringify([
        { id: 7, topic: 'squid', agent: 'claude', session_id: 'other', content: 'Pinned one' },
        { id: 8, topic: 'squid', agent: 'claude', session_id: 'other', content: 'Pinned two' },
      ]));
    });
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(
        META,
        { data: 'Response text' },
        { event: 'stats', data: { session_id: 'test-sid', input_tokens: 10, output_tokens: 5, adhoc: false, lookback: 0, session_turn_count: 18 } },
        DONE,
      ),
    }));

    await sendMsg(page, '#squid@claude hello');
    const ctx = page.locator(RESPONSE).locator('.user-ctx');
    await expect(ctx).toHaveText('ctx: sess 18t · mem · 2p');

    await ctx.click();
    await expect(page.locator('#ctx-popup')).toContainText('session context18 turns');
    const regularLinkColor = await page.locator('#ctx-popup .ctx-popup-link').first()
      .evaluate(el => getComputedStyle(el).color);
    await expect(page.locator('#ctx-popup .ctx-popup-tag').first()).toHaveCSS('color', regularLinkColor);

    await page.locator('#ctx-popup .ctx-popup-pin[data-pin-id="7"]').click();
    await expect(page.locator('#msg-modal')).toHaveClass(/open/);
    await expect(page.locator('#msg-modal-title')).toContainText('Message #7');
    await expect(page.locator('#ctx-popup')).not.toHaveClass(/open/);
  });

  test('context popup shows flow run id when present', async ({ page }) => {
    await page.unroute('**/history**');
    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 42,
        role: 'assistant',
        topic: 'squid',
        agent: 'codex',
        adhoc: false,
        status: 'done',
        content: 'Flow response',
        prompt: 'Flow prompt',
        prompt_source: 'human',
        flow_run_id: 'flow-test-123',
        flow_route: '#squid@codex>@review',
        timestamp: '2026-07-16T12:00:00Z',
      }],
      has_more: false,
    }}));
    await page.reload();

    const ctx = page.locator('.msg.assistant.history-item .user-ctx');
    await expect(ctx).toBeVisible();
    await ctx.click();

    await expect(page.locator('#ctx-popup')).toContainText('flow run');
    await expect(page.locator('#ctx-popup')).toContainText('flow-test-123');
  });

  test('ctx popup near the top stays below the topbar on desktop and mobile', async ({ page }) => {
    for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 700 }]) {
      await page.setViewportSize(viewport);
      await page.goto('/');
      await page.evaluate(() => {
        document.getElementById('top-test-ctx')?.remove();
        const app = document.getElementById('app');
        const appRect = app.getBoundingClientRect();
        const topbarRect = document.getElementById('topbar').getBoundingClientRect();
        const anchor = document.createElement('span');
        anchor.id = 'top-test-ctx';
        anchor.className = 'user-ctx';
        anchor.textContent = 'ctx: top';
        anchor.dataset.msgId = '99';
        anchor.style.position = 'absolute';
        anchor.style.right = '1rem';
        anchor.style.top = `${topbarRect.bottom - appRect.top + 2}px`;
        anchor.addEventListener('click', e => {
          e.stopPropagation();
          showCtxPopup(anchor);
        });
        app.appendChild(anchor);
      });

      await page.locator('#top-test-ctx').click();
      await expect(page.locator('#ctx-popup')).toHaveClass(/open/);
      await expect.poll(() => page.evaluate(() => {
        const popup = document.getElementById('ctx-popup').getBoundingClientRect();
        const topbar = document.getElementById('topbar').getBoundingClientRect();
        return {
          belowTopbar: popup.top >= topbar.bottom,
          insideBottom: popup.bottom <= window.innerHeight,
        };
      })).toEqual({ belowTopbar: true, insideBottom: true });
      await page.locator('#ctx-popup').evaluate(popup => popup.classList.remove('open'));
    }
  });

  test('ctx popup exposes a thought trace link when tool calls or status text were recorded', async ({ page }) => {
    await page.route('**/chat/1/events**', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(
        { event: 'status', data: 'Checking the repo first.' },
        { event: 'tool', data: { name: 'Bash', command: 'ls -la' } },
        { event: 'status', data: 'Now reading the target file.' },
        { data: 'Final answer text' },
        { event: 'done', data: '' },
      ),
    }));
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'status', data: 'Checking the repo first.' },
        { event: 'tool', data: { name: 'Bash', command: 'ls -la' } },
        { event: 'status', data: 'Now reading the target file.' },
        { data: 'Final answer text' },
        STATS,
        DONE,
      ),
    }));

    await sendMsg(page);
    const ctx = page.locator(RESPONSE).locator('.user-ctx');
    await ctx.click();

    const traceRow = page.locator('#ctx-popup .ctx-popup-trace-row');
    await expect(traceRow).toBeVisible();
    await expect(traceRow).toContainText('trace');
    await expect(traceRow).toContainText('thoughts');

    await traceRow.click();
    await expect(page.locator('#msg-modal')).toHaveClass(/open/);
    await expect(page.locator('#msg-modal-title')).toContainText('thought trace');
    await expect(page.locator('#ctx-popup')).not.toHaveClass(/open/);

    // Narration and tool calls render interleaved in true chronological order,
    // not grouped into two separate blocks — and the final answer text (a
    // plain 'text' event) is excluded since it's already shown in the bubble.
    const children = page.locator('#msg-modal-body > *');
    await expect(children).toHaveCount(3);
    await expect(children.nth(0)).toHaveClass(/trace-status/);
    await expect(children.nth(0)).toContainText('Checking the repo first.');
    await expect(children.nth(1)).toHaveClass(/tool-block/);
    await expect(children.nth(1)).toContainText('Bash: ls -la');
    await expect(children.nth(2)).toHaveClass(/trace-status/);
    await expect(children.nth(2)).toContainText('Now reading the target file.');
    await expect(page.locator('#msg-modal-body')).not.toContainText('Final answer text');

    await children.nth(1).locator('.tool-toggle').click();
    await expect(children.nth(1).locator('.trace-tool-pre')).toHaveText('ls -la');
    await expect(children.nth(1).getByRole('button', { name: 'Copy command' })).toBeVisible();
  });

  test('ctx popup has no thought trace link when nothing was recorded', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Just an answer, no tools used' }, STATS, DONE),
    }));

    await sendMsg(page);
    const ctx = page.locator(RESPONSE).locator('.user-ctx');
    await ctx.click();

    await expect(page.locator('#ctx-popup')).toBeVisible();
    await expect(page.locator('#ctx-popup .ctx-popup-trace-row')).not.toBeAttached();
  });

  test('content is markdown-rendered in final bubble', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: '**bold** and `code`' }, DONE),
    }));

    await sendMsg(page);
    const bubble = page.locator(RESPONSE);
    await expect(bubble.locator('strong')).toHaveText('bold');
    await expect(bubble.locator('code')).toHaveText('code');
    await look(page);  // pause — observe: bold and inline code rendered in bubble
  });

  test('local file links with line suffix route through /localfile', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('squid_token', 'test-token'));
    await page.goto('/');
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: '[app.js](/Users/haebin/Work/squid/ui/app.js:470)' }, DONE),
    }));

    await sendMsg(page);
    const href = await page.locator(`${RESPONSE} a`).getAttribute('href');
    expect(href).toContain('/localfile?path=');
    expect(decodeURIComponent(href)).toContain('/Users/haebin/Work/squid/ui/app.js');
    expect(decodeURIComponent(href)).not.toContain('app.js:470');
    expect(href).toContain('token=test-token');
  });

  test('Squid worktree links are not rewritten to local file links', async ({ page }) => {
    await page.goto('/');
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: '[app.py](/Users/haebin/.squid/worktrees/ef27c425/sqd-squid-2066-921e61/app.py:12)' }, DONE),
    }));

    await sendMsg(page);
    const href = await page.locator(`${RESPONSE} a`).getAttribute('href');
    expect(href).toBe('#');
    expect(href).not.toContain('/localfile?path=');
  });

  test('returning from another tab preserves scrolled-up chat position', async ({ page }) => {
    await page.evaluate(() => {
      const messages = document.getElementById('messages');
      messages.innerHTML = '';
      for (let i = 0; i < 20; i++) {
        const bubble = document.createElement('div');
        bubble.className = 'msg assistant';
        bubble.style.minHeight = '120px';
        bubble.textContent = `older response ${i} `.repeat(20);
        messages.appendChild(bubble);
      }
      messages.scrollTop = 120;
    });

    const before = await page.locator('#messages').evaluate(el => el.scrollTop);
    await setPageHidden(page, true);
    await setPageHidden(page, false);

    await expect.poll(() => page.locator('#messages').evaluate(el => el.scrollTop)).toBe(before);
  });

  test('mobile scroll-to-bottom button stays above composer controls', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await page.evaluate(() => {
      const messages = document.getElementById('messages');
      messages.innerHTML = '';
      for (let i = 0; i < 20; i++) {
        const bubble = document.createElement('div');
        bubble.className = 'msg assistant';
        bubble.style.minHeight = '120px';
        bubble.textContent = `older response ${i} `.repeat(20);
        messages.appendChild(bubble);
      }
      messages.scrollTop = 0;
      document.getElementById('topic-chip').classList.add('visible');
      document.getElementById('topic-chip').textContent = '#squid @claude';
      messages.dispatchEvent(new Event('scroll'));
    });

    await expect(page.locator('#scroll-btn')).toBeVisible();
    const boxes = await page.evaluate(() => {
      const rect = id => {
        const { top, bottom, left, right } = document.getElementById(id).getBoundingClientRect();
        return { top, bottom, left, right };
      };
      return {
        scroll: rect('scroll-btn'),
        inputArea: rect('input-area'),
        chipActions: rect('chip-actions'),
      };
    });

    expect(boxes.scroll.bottom).toBeLessThanOrEqual(boxes.inputArea.top - 8);
    expect(boxes.scroll.bottom).toBeLessThanOrEqual(boxes.chipActions.top - 8);
  });

  test('returning from another tab keeps following chat when already at bottom', async ({ page }) => {
    await page.evaluate(() => {
      const messages = document.getElementById('messages');
      messages.innerHTML = '';
      for (let i = 0; i < 20; i++) {
        const bubble = document.createElement('div');
        bubble.className = 'msg assistant';
        bubble.style.minHeight = '120px';
        bubble.textContent = `new response ${i} `.repeat(20);
        messages.appendChild(bubble);
      }
      messages.scrollTop = messages.scrollHeight;
    });

    await setPageHidden(page, true);
    await page.evaluate(() => {
      const messages = document.getElementById('messages');
      const bubble = document.createElement('div');
      bubble.className = 'msg assistant';
      bubble.style.minHeight = '120px';
      bubble.textContent = 'late update '.repeat(80);
      messages.appendChild(bubble);
    });
    await setPageHidden(page, false);

    await expect.poll(() => page.locator('#messages').evaluate(el => (
      el.scrollHeight - el.scrollTop - el.clientHeight
    ))).toBeLessThan(150);
  });

  test('completed response scrolls to reveal its top when its insertion exceeds the threshold', async ({ page }) => {
    // Landing at the literal bottom would skip past a tall completed item
    // entirely, forcing a scroll back up to read it from the start. This
    // applies to any completed-item insertion the user was following at the
    // bottom for — not just their own just-sent message — including a turn
    // discovered from another tab/session via the realtime layer.
    await expect(page.locator('#messages')).toBeVisible();
    await page.evaluate(() => {
      const messages = document.getElementById('messages');
      messages.innerHTML = '';
      for (let i = 0; i < 12; i++) {
        const bubble = document.createElement('div');
        bubble.className = 'msg assistant';
        bubble.style.minHeight = '100px';
        bubble.textContent = `older response ${i}`;
        messages.appendChild(bubble);
      }
      messages.scrollTop = messages.scrollHeight;
      insertCompletedHistoryItem({
        id: 999,
        topic: 'squid',
        agent: 'codex',
        content: 'large completed response '.repeat(150),
        completed_at: '2026-08-14T12:00:00Z',
      });
    });

    const { bubbleTop, messagesTop, atBottom } = await page.evaluate(() => {
      const el = document.querySelector('[data-msg-id="999"]');
      const container = document.getElementById('messages');
      return {
        bubbleTop: el.getBoundingClientRect().top,
        messagesTop: container.getBoundingClientRect().top,
        atBottom: container.scrollHeight - container.scrollTop - container.clientHeight < 2,
      };
    });
    expect(Math.abs(bubbleTop - messagesTop)).toBeLessThan(2);
    expect(atBottom).toBe(false);
  });

  test('renders Codex unified diff tool blocks', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: { name: 'Diff', file: 'ui/app.js', diff: '@@ -1 +1 @@\n-old\n+new' } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const block = page.locator('.tool-block-history').first();
    await expect(block.locator('.tool-toggle')).toContainText('Diff: ui/app.js');
    await block.locator('.tool-toggle').click();
    await expect(block.locator('.diff-hunk')).toContainText('@@ -1 +1 @@');
    await expect(block.locator('.diff-remove')).toContainText('-old');
    await expect(block.locator('.diff-add')).toContainText('+new');
  });

  test('renders GitDiff changed files before legacy edit tools', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: { name: 'Edit', file: 'ui/app.js', old: 'old', new: 'new' } },
        { event: 'tool', data: {
          name: 'GitDiff',
          file_count: 1,
          additions: 1,
          deletions: 1,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n-old\n+new',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const blocks = page.locator('.tool-block-history');
    await expect(blocks).toHaveCount(1);
    const block = blocks.first();
    await expect(block.locator('.tool-toggle')).toContainText('Changed files: 1 file, +1 -1');
    const fileToggle = block.locator('.gitdiff-file-toggle');
    await expect(fileToggle).toContainText('M ui/app.js');
    await expect(fileToggle).toBeVisible();
    const topToggleBox = await block.locator('.tool-toggle').boundingBox();
    const fileToggleBox = await fileToggle.boundingBox();
    expect(fileToggleBox.x - topToggleBox.x).toBeGreaterThanOrEqual(6);
    await fileToggle.click();
    const fileMetaBox = await block.locator('.gitdiff-file-body .diff-header-summary').boundingBox();
    expect(fileMetaBox.x - fileToggleBox.x).toBeGreaterThanOrEqual(6);
    await expect(block.locator('.diff-hunk')).toContainText('@@ -1 +1 @@');
  });

  test('suppresses legacy edit list when GitDiff reports no net changes', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: { name: 'Edit', file: 'ui/app.js', old: 'old', new: 'new' } },
        { event: 'tool', data: {
          name: 'GitDiff',
          file_count: 0,
          additions: 0,
          deletions: 0,
          files: [],
          diff: '',
          no_changes: true,
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    await expect(page.locator(RESPONSE)).toContainText('Done');
    await expect(page.locator('.tool-block-history')).toHaveCount(0);
  });

  test('dedupes repeated legacy edit tool records', async ({ page }) => {
    const writeTool = {
      name: 'Write',
      tool_use_id: 'toolu_duplicate',
      file: '/tmp/repo/line.txt',
      content: 'new line\n',
    };
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: writeTool },
        { event: 'tool', data: writeTool },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const blocks = page.locator('.tool-block-history');
    await expect(blocks).toHaveCount(1);
    await expect(blocks.first().locator('.tool-toggle')).toContainText('Write: /tmp/repo/line.txt');
  });

  test('dedupes repeated legacy edit tool records from history', async ({ page }) => {
    const writeTool = {
      name: 'Write',
      tool_use_id: 'toolu_duplicate_history',
      file: '/tmp/repo/line.txt',
      content: 'new line\n',
    };
    await page.unroute('**/history**');
    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 8484,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'done',
        prompt: 'fix conflict',
        content: 'Done',
        context: JSON.stringify([writeTool, writeTool]),
        timestamp: new Date().toISOString(),
        adhoc: false,
      }],
      has_more: false,
    }}));

    await page.reload();
    await expect(page.locator(RESPONSE)).toContainText('Done');
    const blocks = page.locator('.tool-block-history');
    await expect(blocks).toHaveCount(1);
    await expect(blocks.first().locator('.tool-toggle')).toContainText('Write: /tmp/repo/line.txt');
  });

  test('GitDiff renders text diffs for files with generic trailing suffixes', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'config/squid.yaml.example' }],
          diff: 'diff --git a/config/squid.yaml.example b/config/squid.yaml.example\n@@ -1 +1,2 @@\n old\n+new',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const block = page.locator('.tool-block-history').first();
    await block.locator('.tool-toggle').click();
    await expect(block.locator('.gitdiff-file-toggle')).not.toHaveClass(/gitdiff-file-toggle--no-diff/);
    await expect(block.locator('.gitdiff-binary-badge')).toHaveCount(0);
    await expect(block.locator('.diff-add')).toContainText('+new');
  });

  test('GitDiff mobile labels use shortest unique file names', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          file_count: 3,
          additions: 1,
          deletions: 1,
          files: [
            { status: 'M', path: 'ui/app.js' },
            { status: 'M', path: 'src/components/Button/index.ts' },
            { status: 'M', path: 'src/pages/Button/index.ts' },
          ],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n-old\n+new',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const rows = page.locator('.gitdiff-file-toggle');
    await expect(rows).toContainText([
      'M app.js',
      'M components/Button/index.ts',
      'M pages/Button/index.ts',
    ]);
    await expect(rows.nth(1)).toHaveAttribute('title', 'src/components/Button/index.ts');
  });

  test('GitDiff file-open control is visible and opens the file viewer', async ({ page }) => {
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable' },
    }));
    await page.route('**/localfile**', route => route.fulfill({
      status: 200, contentType: 'text/plain', body: 'const opened = true;',
    }));
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const openButton = page.getByRole('button', { name: 'Open ui/app.js in file viewer' });
    await expect(openButton).toBeVisible();
    await expect(openButton).toHaveText('view');
    const revertButton = page.getByRole('button', { name: 'revert' });
    await expect(revertButton).toBeVisible();
    await expect(page.locator('.gitdiff-file-row').locator('button')).toHaveText([
      'M ui/app.js  +1 -0',
      'revert',
      'view',
    ]);
    const [viewSize, revertSize] = await Promise.all([
      openButton.boundingBox(),
      revertButton.boundingBox(),
    ]);
    expect(viewSize.width).toBeCloseTo(revertSize.width, 0);
    expect(viewSize.height).toBeCloseTo(revertSize.height, 0);
    await openButton.click();

    await expect(page.locator('#file-modal-breadcrumb')).toContainText('tmp/repo/ui/app.js');
    await expect(page.locator('#file-modal-body')).toContainText('const opened = true;');
    await expect(page.locator('#file-modal-body .fv-changed')).toContainText('const opened = true;');
  });

  test('GitDiff file-open control is visible for mjs text diffs', async ({ page }) => {
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'scripts/build.mjs' }],
          diff: 'diff --git a/scripts/build.mjs b/scripts/build.mjs\n@@ -1 +1 @@\n+export const opened = true;',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const row = page.locator('.gitdiff-file-row');
    await expect(row.locator('.gitdiff-binary-badge')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Open scripts/build.mjs in file viewer' })).toBeVisible();
  });

  test('GitDiff uses streamed worktree sync status before showing actions', async ({ page }) => {
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable' },
    }));
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          worktree_repo: '/tmp/.squid/worktrees/topic/repo',
          worktree_status: 'pending',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
        } },
        { event: 'tool', data: {
          name: 'WorktreeSync',
          status: 'synced',
          repo: '/tmp/repo',
          worktree_repo: '/tmp/.squid/worktrees/topic/repo',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    await expect(page.locator('.tool-toggle')).toContainText('Changed files: 1 file, +1 -0');
    await expect(page.locator('.tool-toggle')).not.toContainText('pending');
    await expect(page.getByRole('button', { name: 'Open ui/app.js in file viewer' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'revert' })).toBeVisible();
  });

  test('GitDiff surfaces streamed worktree sync conflicts', async ({ page }) => {
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable' },
    }));
    let discardBody = null;
    await page.route('**/chat/1/worktree/retry', route => route.fulfill({
      status: 404,
      json: { error: 'worktree not found' },
    }));
    await page.route('**/chat/1/worktree/discard', route => {
      discardBody = route.request().postDataJSON();
      route.fulfill({ json: { ok: true } });
    });
    await page.route('**/localfile**', route => route.fulfill({
      status: 200,
      contentType: 'text/plain',
      body: 'line 1\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> turn\nline 7\n<<<<<<< HEAD\nours 2\n=======\ntheirs 2\n>>>>>>> turn\n',
    }));
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          worktree_repo: '/tmp/.squid/worktrees/topic/repo',
          worktree_status: 'pending',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
        } },
        { event: 'tool', data: {
          name: 'WorktreeSync',
          status: 'conflict',
          repo: '/tmp/repo',
          worktree_repo: '/tmp/.squid/worktrees/topic/repo',
          integration_worktree_path: '/tmp/.squid/worktrees/topic/repo-integration',
          conflicts: ['ui/app.js'],
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const block = page.locator('.tool-block-history').first();
    await expect(block.locator('.tool-toggle')).toContainText('Changed files: 1 file, +1 -0 · conflict');
    await expect(block.locator('.gitdiff-sync-notice')).toContainText('Worktree sync conflict: ui/app.js');
    await expect(block.locator('.gitdiff-sync-path')).toContainText('/tmp/.squid/worktrees/topic/repo-integration');
    await expect(page.getByRole('button', { name: 'Open ui/app.js in file viewer' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'revert' })).toHaveCount(0);
    await block.getByRole('button', { name: 'Conflicts' }).click();
    await expect(page.locator('#file-modal-breadcrumb')).toContainText('/tmp/.squid/worktrees/topic/repo-integration/ui/app.js');
    await expect(page.locator('.fv-edit-find')).toHaveValue('<<<<<<<');
    await expect(page.locator('#file-modal-body .fv-target')).toContainText('<<<<<<< HEAD');
    await expect(page.locator('.fv-edit-line')).toHaveValue('2');
    await page.locator('.fv-edit-tool-btn[aria-label="Next match"]').click();
    await expect(page.locator('#file-modal-body .fv-target')).toContainText('<<<<<<< HEAD');
    await expect(page.locator('.fv-edit-line')).toHaveValue('8');
    await page.locator('#file-modal-close').click();
    await expect(block.getByRole('button', { name: 'Auto-Resolve' })).toHaveAttribute('title', /Ask the model to merge both sides directly in the integration worktree/);
    await block.getByRole('button', { name: 'Resolve', exact: true }).click();
    await expect(block.locator('.gitdiff-sync-notice')).toContainText('Resolve failed: worktree not found');
    await expect(block.getByRole('button', { name: 'Discard Turn' })).toHaveAttribute('title', /already-applied main checkout changes are not reverted/);
    await block.getByRole('button', { name: 'Discard Turn' }).click();
    await expect(block.locator('.gitdiff-sync-notice')).toContainText('Worktree discarded');
    expect(discardBody).toEqual({ topic: 'default', repo: '/tmp/repo' });
  });

  test('auto-resolve reconciles its globally discovered bubble by message id', async ({ page }) => {
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({ json: {} }));
    await page.route('**/chat/1/worktree/auto-resolve', async route => {
      await page.evaluate(() => {
        const recovered = makeWipBubble({ id: 91, topic: 'default', agent: 'claude', adhoc: true,
          prompt: 'Auto-resolve merge conflict', content: '' });
        messages.appendChild(recovered);
      });
      await route.fulfill({
        status: 200,
        headers: { ...SSE_HEADERS, 'X-Squid-Msg-Id': '91' },
        body: sse(
          { event: 'meta', data: { agent: 'claude', msg_id: 91, adhoc: true } },
          { event: 'processing', data: { topic: 'default' } },
          { data: 'Resolved exactly once' },
          DONE,
          { event: 'resolve_result', data: { ok: true, files: ['ui/app.js'] } },
        ),
      });
    });
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff', repo: '/tmp/repo', worktree_repo: '/tmp/worktree', worktree_status: 'pending',
          file_count: 1, additions: 1, deletions: 0, files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+changed',
        } },
        { event: 'tool', data: {
          name: 'WorktreeSync', status: 'conflict', repo: '/tmp/repo', worktree_repo: '/tmp/worktree',
          integration_worktree_path: '/tmp/integration', conflicts: ['ui/app.js'],
        } },
        { data: 'Original response' }, DONE,
      ),
    }));

    await sendMsg(page);
    await page.getByRole('button', { name: 'Auto-Resolve' }).click();
    const resolved = page.locator('.msg.assistant[data-msg-id="91"]');
    await expect(resolved).toHaveCount(1);
    await expect(resolved).toContainText('Resolved exactly once');
    await expect(resolved).not.toContainText('{"topic":"default"}');
    await expect(page.locator(`${THINKING}[data-msg-id="91"]`)).not.toBeAttached();
  });

  test('realtime worktree and revert changes converge an existing GitDiff block', async ({ page }) => {
    let revertStatus = { 'ui/app.js': 'revertable' };
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({ json: revertStatus }));
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff', repo: '/tmp/repo', worktree_repo: '/tmp/worktree', worktree_status: 'pending',
          file_count: 1, additions: 1, deletions: 0, files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+changed',
        } },
        { event: 'tool', data: {
          name: 'WorktreeSync', status: 'conflict', repo: '/tmp/repo', worktree_repo: '/tmp/worktree',
          integration_worktree_path: '/tmp/integration', conflicts: ['ui/app.js'],
        } },
        { data: 'Original response' }, DONE,
      ),
    }));

    await sendMsg(page);
    const block = page.locator('.tool-block-history').first();
    await expect(block.getByRole('button', { name: 'Auto-Resolve' })).toBeVisible();

    // A trailing slash in an event's canonical repo must still match the
    // rendered tool's source repo and update the existing DOM in place.
    await page.evaluate(() => applyRealtimeWorktreeChange({
      msg_id: 1, payload: { repo: '/tmp/repo/', status: 'resolved' },
    }));
    await expect(block.locator('.tool-toggle')).toContainText('Conflict Resolved');
    await expect(block.getByRole('button', { name: 'Auto-Resolve' })).toHaveCount(0);
    await expect(block.getByRole('button', { name: 'revert' })).toBeVisible();

    revertStatus = { 'ui/app.js': 'reverted' };
    await page.evaluate(() => refreshAllRevertButtons({ force: true }));
    await expect(block.locator('.gitdiff-file-row')).toHaveClass(/gitdiff-file-row--reverted/);
    await expect(block.getByRole('button', { name: 'revert' })).toHaveCount(0);
  });

  test('blocked worktree response renders controls for original turn', async ({ page }) => {
    let retryUrl = null;
    let retryBody = null;
    await page.route('**/chat/*/worktree/retry', route => {
      retryUrl = route.request().url();
      retryBody = route.request().postDataJSON();
      route.fulfill({ json: { ok: true } });
    });
    await page.route('**/localfile**', route => route.fulfill({
      status: 200,
      contentType: 'text/plain',
      body: 'line 1\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> turn\n',
    }));
    await page.route('**/chat', route => route.fulfill({
      status: 409,
      json: {
        error: 'worktree sync requires attention before starting another turn',
        worktrees: [{
          repo_root: '/tmp/repo',
          worktree_path: '/tmp/.squid/worktrees/topic/repo',
          integration_worktree_path: '/tmp/.squid/worktrees/topic/repo-integration',
          status: 'conflict',
          msg_id: '7282',
          conflicts: ['ui/app.js'],
        }],
      },
    }));

    await sendMsg(page);
    await expect(page.locator(MSG_ERROR)).toContainText('worktree sync requires attention');
    const block = page.locator('.tool-block-history').first();
    await expect(block.locator('.gitdiff-sync-notice')).toContainText('Worktree sync conflict: ui/app.js');
    await expect(block.getByRole('button', { name: 'Conflicts' })).toBeVisible();
    await expect(block.getByRole('button', { name: 'Auto-Resolve' })).toBeVisible();
    await expect(block.getByRole('button', { name: 'Discard Turn' })).toHaveAttribute(
      'title',
      /later blocked message points at turn #7282.*Discard only this isolated turn's pending worktree changes/,
    );
    await block.evaluate(el => el.after(el.cloneNode(true)));
    await expect(page.locator('.tool-block-history .gitdiff-sync-notice')).toHaveCount(2);
    await block.getByRole('button', { name: 'Resolve', exact: true }).click();
    await expect(page.locator('.tool-block-history .gitdiff-sync-notice')).toHaveText([
      /Worktree resolved and synced/,
      /Worktree resolved and synced/,
    ]);
    await expect(page.locator('.tool-block-history').nth(0).locator('.gitdiff-resolved-label')).toHaveText('Resolved');
    await expect(page.locator('.tool-block-history').nth(1).locator('.gitdiff-resolved-label')).toHaveText('Resolved');
    expect(retryUrl).toContain('/chat/7282/worktree/retry');
    expect(retryBody).toEqual({ topic: 'default', repo: '/tmp/repo', force: false });
  });

  test('active worktree blocker offers retry sync and discard controls', async ({ page }) => {
    let retryBody = null;
    await page.route('**/chat/*/worktree/retry', route => {
      retryBody = route.request().postDataJSON();
      route.fulfill({ json: { ok: true } });
    });
    await page.route('**/chat', route => route.fulfill({
      status: 409,
      json: {
        error: 'worktree sync requires attention before starting another turn',
        worktrees: [{
          repo_root: '/tmp/repo',
          worktree_path: '/tmp/.squid/worktrees/topic/repo',
          status: 'active',
          msg_id: '7283',
        }],
      },
    }));

    await sendMsg(page);
    const block = page.locator('.tool-block-history').first();
    await expect(block.locator('.gitdiff-sync-notice')).toContainText('Worktree sync active');
    await expect(block.getByRole('button', { name: 'Retry Sync' })).toBeVisible();
    await expect(block.getByRole('button', { name: 'Discard Turn' })).toBeVisible();
    await expect(block.getByRole('button', { name: 'Resolve', exact: true })).toHaveCount(0);
    await expect(block.getByRole('button', { name: 'Conflicts' })).toHaveCount(0);
    await expect(block.getByRole('button', { name: 'Open worktree changes in file viewer' })).toHaveCount(0);
    await expect(block.getByRole('button', { name: 'revert' })).toHaveCount(0);

    await block.getByRole('button', { name: 'Retry Sync' }).click();
    await expect(block.locator('.gitdiff-sync-notice')).toContainText('Worktree synced');
    expect(retryBody).toEqual({ topic: 'default', repo: '/tmp/repo', force: false });
  });

  test('mobile browser back closes GitDiff file viewer back to diff list', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable' },
    }));
    await page.route('**/localfile**', route => route.fulfill({
      status: 200, contentType: 'text/plain', body: 'const opened = true;',
    }));
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    await page.getByRole('button', { name: 'Open ui/app.js in file viewer' }).click();
    await expect(page.locator('#file-modal-box')).toBeVisible();
    await expect(page.locator('#file-modal-body')).toContainText('const opened = true;');

    await page.evaluate(() => history.back());
    await expect(page.locator('#file-modal-box')).toHaveCount(0);
    await expect(page.locator('.gitdiff-file-row')).toContainText('M app.js');
    await expect(page.locator('#view-chat')).toHaveClass(/active/);
  });

  test('GitDiff file-open uses source repo instead of worktree repo', async ({ page }) => {
    const openedPaths = [];
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable' },
    }));
    await page.route('**/localfile**', route => {
      const url = new URL(route.request().url());
      openedPaths.push(url.searchParams.get('path'));
      route.fulfill({ status: 200, contentType: 'text/plain', body: 'const opened = true;' });
    });
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/Users/haebin/.squid/worktrees/ef27c425/sqd-squid-2066-921e61',
          source: '/Users/haebin/Work/squid',
          worktree_repo: '/Users/haebin/.squid/worktrees/ef27c425/sqd-squid-2066-921e61',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    await page.getByRole('button', { name: 'Open ui/app.js in file viewer' }).click();

    await expect(page.locator('#file-modal-breadcrumb')).toContainText('Work/squid/ui/app.js');
    expect(openedPaths[0]).toBe('/Users/haebin/Work/squid/ui/app.js');
    expect(openedPaths[0]).not.toContain('/.squid/worktrees/');
  });

  test('revert eligibility is refreshed for new GitDiff blocks and after a revert', async ({ page }) => {
    const statusRequests = [];
    await page.route('**/chat/*/diff-revert-status**', route => {
      const msgId = route.request().url().match(/\/chat\/(\d+)\//)[1];
      statusRequests.push(msgId);
      route.fulfill({ json: { 'ui/app.js': 'revertable' } });
    });
    await page.route('**/chat/1/revert', route => route.fulfill({ json: { ok: true, reverted: ['ui/app.js'] } }));

    const gitDiffTool = {
      name: 'GitDiff', repo: '/tmp/repo', file_count: 1, additions: 1, deletions: 0,
      files: [{ status: 'M', path: 'ui/app.js' }],
      diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
    };

    // First message completes with a GitDiff tool block (msg_id 1).
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { event: 'tool', data: gitDiffTool }, { data: 'Done' }, DONE),
    }), { times: 1 });
    await sendMsg(page, 'first');
    await expect(page.locator('.tool-block-history')).toHaveCount(1);
    await expect.poll(() => statusRequests).toEqual(['1']);

    // Second message completes with its own GitDiff tool block (msg_id 2).
    // Rendering it can retroactively change older blocks' eligibility, so the
    // existing block is rechecked along with the new one.
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        { event: 'meta', data: { agent: 'claude', backend: 'claude', msg_id: 2, adhoc: false } },
        { event: 'tool', data: gitDiffTool },
        { data: 'Done' },
        DONE,
      ),
    }));
    await sendMsg(page, 'second');
    await expect(page.locator('.tool-block-history')).toHaveCount(2);
    await expect.poll(() => statusRequests).toEqual(['1', '1', '2']);

    // Reverting changes the working tree, so eligibility for every block -
    // including the already-checked one - needs a fresh check.
    await page.locator('.tool-block-history').first().getByRole('button', { name: 'revert' }).click();
    await expect(page.locator('#restart-modal')).toHaveClass(/open/);
    await expect(page.locator('#restart-modal-title')).toHaveText('Revert ui/app.js?');
    await page.locator('#restart-modal-confirm').click();
    await expect.poll(() => statusRequests).toEqual(['1', '1', '2', '1', '2']);
  });

  test('single-file revert can be cancelled before request', async ({ page }) => {
    let revertRequests = 0;
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable' },
    }));
    await page.route('**/chat/1/revert', route => {
      revertRequests++;
      route.fulfill({ json: { ok: true, reverted: ['ui/app.js'] } });
    });
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          file_count: 1,
          additions: 1,
          deletions: 0,
          files: [{ status: 'M', path: 'ui/app.js' }],
          diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n+const opened = true;',
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    await page.getByRole('button', { name: 'revert' }).click();
    await expect(page.locator('#restart-modal-title')).toHaveText('Revert ui/app.js?');
    await page.locator('#restart-modal-cancel').click();
    await expect(page.locator('#restart-modal')).not.toHaveClass(/open/);
    expect(revertRequests).toBe(0);
  });

  test('revert all does not show success when no files reverted', async ({ page }) => {
    await page.route('**/chat/1/diff-revert-status**', route => route.fulfill({
      json: { 'ui/app.js': 'revertable', 'ui/style.css': 'revertable' },
    }));
    await page.route('**/chat/1/revert', route => route.fulfill({
      json: {
        ok: true,
        reverted: [],
        failed: [{ file: 'ui/app.js', error: 'patch does not apply' }],
      },
    }));
    await page.route('**/chat', route => route.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'tool', data: {
          name: 'GitDiff',
          repo: '/tmp/repo',
          file_count: 2,
          additions: 2,
          deletions: 0,
          files: [
            { status: 'M', path: 'ui/app.js' },
            { status: 'M', path: 'ui/style.css' },
          ],
          diff: [
            'diff --git a/ui/app.js b/ui/app.js',
            '@@ -1 +1 @@',
            '+const opened = true;',
            'diff --git a/ui/style.css b/ui/style.css',
            '@@ -1 +1 @@',
            '+body { color: red; }',
          ].join('\n'),
        } },
        { data: 'Done' },
        DONE,
      ),
    }));

    await sendMsg(page);
    const revertAll = page.getByRole('button', { name: 'Revert all 2 files' });
    await revertAll.click();
    await expect(page.locator('#restart-modal-title')).toHaveText('Revert 2 files?');
    await page.locator('#restart-modal-confirm').click();

    await expect(revertAll).toBeEnabled();
    await expect(revertAll).toHaveText('Revert all 2 files');
    await expect(revertAll).toHaveAttribute('title', 'patch does not apply');
  });

  test('recovered completion restores GitDiff and renders one end timestamp', async ({ page }) => {
    const gitDiff = {
      name: 'GitDiff',
      file_count: 1,
      additions: 1,
      deletions: 1,
      files: [{ status: 'M', path: 'ui/app.js' }],
      diff: 'diff --git a/ui/app.js b/ui/app.js\n@@ -1 +1 @@\n-old\n+new',
    };
    await page.route('**/chat/*/status', r => r.fulfill({ json: {
      status: 'done',
      content: 'Recovered response',
      context: JSON.stringify([gitDiff]),
    } }));
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(META, { data: 'Partial response' }),
    }));

    await sendMsg(page);
    await expect(page.locator('.tool-block-history')).toHaveCount(1, { timeout: 5_000 });
    await expect(page.locator(RESPONSE)).toContainText('Recovered response');
    await expect(page.locator('.msg-time')).toHaveCount(2); // user start + assistant completion
    await page.waitForTimeout(2_200);
    await expect(page.locator('.msg-time')).toHaveCount(2);
  });

  test('live recovery ignores empty interrupted error until final completion', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: { ...SSE_HEADERS, 'X-Squid-Msg-Id': '90' },
      body: '',
    }));

    let statusCalls = 0;
    await page.route('**/chat/90/status', r => {
      statusCalls++;
      if (statusCalls === 1) {
        return r.fulfill({ json: { id: 90, status: 'pending', content: 'Partial response' } });
      }
      if (statusCalls === 2) {
        return r.fulfill({ json: { id: 90, status: 'error', content: '' } });
      }
      return r.fulfill({ json: {
        id: 90,
        topic: 'default',
        agent: 'claude',
        backend: 'claude',
        status: 'done',
        content: 'Recovered final response',
        adhoc: false,
        timestamp: new Date().toISOString(),
      }});
    });

    await sendMsg(page);

    await expect(page.locator(RESPONSE).filter({ hasText: 'Recovered final response' })).toBeVisible({ timeout: 8_000 });
    await expect(page.locator(MSG_ERROR).filter({ hasText: 'Response interrupted.' })).not.toBeAttached();
    expect(statusCalls).toBeGreaterThanOrEqual(3);
  });

  test('stream error after partial content keeps partial in thinking bubble only', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(META, { data: 'Partial answer' }, { event: 'error', data: 'Connection lost' }),
    }));

    await sendMsg(page);

    const statusBubble = page.locator('.msg-thinking-done');
    await expect(statusBubble).toContainText('Partial answer');
    await expect(statusBubble).toContainText('Connection lost');
    await expect(page.locator(RESPONSE)).not.toBeAttached();
    await expect(page.locator(MSG_ERROR)).not.toBeAttached();
  });

  test('stream error with message id keeps polling until final completion', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: { ...SSE_HEADERS, 'X-Squid-Msg-Id': '92' },
      body: sse(
        { event: 'meta', data: { agent: 'claude', backend: 'claude', msg_id: 92, adhoc: false } },
        { event: 'status', data: 'Wrapping up...' },
        { event: 'error', data: 'Connection lost' },
      ),
    }));

    let statusCalls = 0;
    await page.route('**/chat/92/status', r => {
      statusCalls++;
      if (statusCalls === 1) {
        return r.fulfill({ json: { id: 92, status: 'pending', content: '' } });
      }
      if (statusCalls === 2) {
        return r.fulfill({ json: { id: 92, status: 'error', content: '' } });
      }
      return r.fulfill({ json: {
        id: 92,
        topic: 'default',
        agent: 'claude',
        backend: 'claude',
        status: 'done',
        content: 'Recovered final response',
        adhoc: false,
        timestamp: new Date().toISOString(),
      }});
    });

    await sendMsg(page);

    await expect(page.locator(RESPONSE).filter({ hasText: 'Recovered final response' })).toBeVisible({ timeout: 8_000 });
    await expect(page.locator(MSG_ERROR).filter({ hasText: 'Connection lost' })).not.toBeAttached();
    expect(statusCalls).toBeGreaterThanOrEqual(3);
  });

  test('polling error after partial content keeps partial in thinking bubble only', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: { ...SSE_HEADERS, 'X-Squid-Msg-Id': '91' },
      body: '',
    }));

    let statusCalls = 0;
    await page.route('**/chat/91/status', r => {
      statusCalls++;
      if (statusCalls === 1) {
        return r.fulfill({ json: { id: 91, status: 'pending', content: 'Partial response' } });
      }
      return r.fulfill({ json: { id: 91, status: 'error', content: 'Partial response' } });
    });

    await sendMsg(page);

    const statusBubble = page.locator('.msg-thinking-done');
    await expect(statusBubble).toContainText('Partial response', { timeout: 5_000 });
    await expect(statusBubble).toContainText('Connection interrupted.');
    await expect(page.locator(RESPONSE)).not.toBeAttached();
    await expect(page.locator(MSG_ERROR)).not.toBeAttached();
  });

  test('status events are hidden after final response completes', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { event: 'status', data: 'Thinking...' }, { data: 'Result' }, DONE),
    }));

    await sendMsg(page);
    await expect(page.locator(RESPONSE)).toBeVisible();
    await expect(page.locator(RESPONSE)).toContainText('Result');
    await expect(page.locator(RESPONSE)).not.toContainText('Thinking...');
    await expect(page.locator(THINKING)).not.toBeAttached();
    await expect(page.locator('.msg-thinking-done')).not.toBeAttached();
    await look(page);  // pause — observe: only final response bubble
  });

  test('status streaming preserves newlines and adjacent delta chunks', async ({ page }) => {
    const body = sse(META)
      + 'event: status\ndata: first line\ndata: sec\n\n'
      + 'event: status\ndata: ond line\n\n'
      + sse({ data: 'Final response' }, DONE);
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS, body,
    }));

    await sendMsg(page);

    await expect(page.locator(THINKING)).not.toBeAttached();
    await expect(page.locator(RESPONSE)).toContainText('Final response');
    await expect(page.locator(RESPONSE)).not.toContainText('first line');
  });

  test('partial status remains in status bubble when the response errors', async ({ page }) => {
    const statusEvents = Array.from({ length: 40 }, (_, i) => ({
      event: 'status',
      data: `Checking the code ${i + 1}...`,
    }));
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        ...statusEvents,
        { event: 'error', data: 'Backend unavailable' },
      ),
    }));

    await sendMsg(page);

    const statusBubble = page.locator('.msg-thinking-done');
    await expect(statusBubble.locator('.thinking-body')).toContainText('Checking the code 1...');
    await expect(statusBubble.getByRole('button', { name: 'Double thinking height' })).not.toBeVisible();
    await statusBubble.locator('.thinking-toggle').click();
    await expect(statusBubble.getByRole('button', { name: 'Double thinking height' })).toBeVisible();
    const normalMax = await statusBubble.locator('.thinking-body').evaluate(el => parseFloat(getComputedStyle(el).maxHeight));
    await statusBubble.getByRole('button', { name: 'Double thinking height' }).click();
    await expect(statusBubble).toHaveClass(/thinking-tall/);
    const tallMax = await statusBubble.locator('.thinking-body').evaluate(el => parseFloat(getComputedStyle(el).maxHeight));
    expect(tallMax).toBeGreaterThan(normalMax * 1.8);
    const response = page.locator(RESPONSE);
    await expect(response.locator(MSG_ERROR)).toHaveText('Backend unavailable');
    await expect(response).not.toContainText('Checking the code...');
  });

  test('agent-prefixed status appears in the thought bubble', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'status', data: '[Agent: scan panels] Searching file viewer code...' },
        { event: 'error', data: 'Backend unavailable' },
      ),
    }));

    await sendMsg(page);

    const statusBubble = page.locator('.msg-thinking-done');
    await expect(statusBubble.locator('.thinking-body')).toContainText('[Agent: scan panels] Searching file viewer code...');
    const response = page.locator(RESPONSE);
    await expect(response.locator(MSG_ERROR)).toHaveText('Backend unavailable');
    await expect(response).not.toContainText('Searching file viewer code...');
  });

  test('terminated response error removes the status bubble', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(
        META,
        { event: 'status', data: 'Let me find the file browser header.' },
        { event: 'error', data: 'CLI exited -15: ' },
      ),
    }));

    await sendMsg(page);

    await expect(page.locator(THINKING)).not.toBeAttached();
    await expect(page.locator(RESPONSE)).toHaveCount(1);
    await expect(page.locator(RESPONSE).locator(MSG_ERROR)).toHaveText('Response interrupted.');
  });

  test('websocket recovery replaces its connection error with waiting state', async ({ page }) => {
    await page.addInitScript(() => {
      window.__webSocket = null;
      window.__webSockets = [];
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__webSocket = this;
          window.__webSockets.push(this);
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: {} });
          });
        }
        send() {}
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() { this.readyState = 3; this.onclose?.(); }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    await page.route('**/history**', route => route.fulfill({ json: {
      items: [{ id: 86, topic: 'squid', agent: 'claude', backend: 'claude', status: 'pending',
        prompt: 'recover me', content: '', adhoc: false }],
      has_more: false,
    }}));

    await page.goto('/');
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);
    const live = page.locator(`${THINKING}[data-msg-id="86"] .thinking-live`);
    await page.evaluate(() => window.__webSocket.onclose?.());
    await expect(live).toContainText('WebSocket connection failed; retrying…');
    await page.waitForFunction(() => window.__webSockets.length >= 2 && window.__webSocket.readyState === 1);
    await page.evaluate(() => window.__webSocket.receive({ v: 1, type: 'subscribed', payload: {} }));
    await expect(live).toContainText('Waiting for new updates…');
    await expect(live).not.toContainText('WebSocket connection failed');
  });

  test('interrupted stream before meta keeps a recovering status bubble', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: { ...SSE_HEADERS, 'X-Squid-Msg-Id': '88' },
      body: '',
    }));
    await page.route('**/chat/88/status', r => r.fulfill({ json: {
      id: 88,
      status: 'pending',
      content: '',
    }}));

    await sendMsg(page);

    const statusBubble = page.locator(THINKING);
    await expect(statusBubble).toBeVisible();
    await expect(statusBubble).toContainText('Connection interrupted');
    await expect(page.locator(RESPONSE)).not.toBeAttached();
  });

  test('interrupted stream without headers recovers message id from process tracker', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: '',
    }));
    await page.route('**/processes', r => r.fulfill({ json: [
      { topic: 'default', agent: 'claude', adhoc: true, msg_id: 89, state: 'running' },
    ] }));
    await page.route('**/chat/89/status', r => r.fulfill({ json: {
      id: 89,
      status: 'pending',
      content: '',
    }}));

    await sendMsg(page);

    const statusBubble = page.locator(THINKING);
    await expect(statusBubble).toBeVisible();
    await expect(statusBubble).toContainText('Connection interrupted');
    await expect(statusBubble).toHaveAttribute('data-msg-id', '89');
  });

  test('thinking bubble removed when no status events', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { data: 'Hello' }, DONE),
    }));

    await sendMsg(page);
    await expect(page.locator(RESPONSE)).toBeVisible();
    await expect(page.locator(THINKING)).not.toBeAttached();
    await look(page);  // pause — observe: only response bubble, thinking bubble gone
  });

  test('error appears at bottom in bubble', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { event: 'error', data: 'Backend unavailable' }),
    }));

    await sendMsg(page);
    const errorBubble = page.locator(RESPONSE);
    await expect(errorBubble).toBeVisible();
    await expect(errorBubble.locator(MSG_ERROR)).toContainText('Backend unavailable');
    const last = page.locator('#messages > *').last();
    await expect(last).toHaveClass(/assistant/);
    await look(page);  // pause — observe: error message in bubble at bottom
  });

  test('auth-required error shows login button before the error text', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200, headers: SSE_HEADERS,
      body: sse(META, { event: 'error', data: '[[cli-auth-required:claudecode]] Not logged in — run /login' }),
    }));

    await sendMsg(page);
    const errorBubble = page.locator(RESPONSE);
    await expect(errorBubble).toBeVisible();
    const loginBtn = errorBubble.locator('.auth-login-btn');
    await expect(loginBtn).toHaveText('Log in to Claude Code');
    await expect(errorBubble.locator(MSG_ERROR)).toContainText('Not logged in — run /login');

    // Button must render first so it leads the message, not trail it.
    const order = await errorBubble.locator('.auth-login-btn, .msg-error').evaluateAll(
      els => els.map(el => el.className),
    );
    expect(order[0]).toContain('auth-login-btn');
    await look(page);  // pause — observe: login button leads the error text
  });

  test('locked Cursor keychain error offers unlock instead of login', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(META, {
        event: 'error',
        data: '[[cli-auth-required:cursor]] Error: Your macOS login keychain is locked.',
      }),
    }));

    await sendMsg(page);
    const authBtn = page.locator(RESPONSE).locator('.auth-login-btn');
    await expect(authBtn).toHaveText('Unlock keychain');
    await expect(authBtn).toHaveAttribute('data-auth-mode', 'unlock');
    await expect(authBtn).not.toContainText('Log in');
  });
});

test.describe('parallel responses', () => {
  test.beforeEach(async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
  });

  test('two concurrent responses both land at bottom without early bubble insertion', async ({ page }) => {
    const routes = [];
    const bothIntercepted = new Promise(resolve => {
      page.route('**/chat', route => {
        routes.push(route);
        if (routes.length === 2) resolve();
      });
    });

    await sendMsg(page, '#a hello');
    await sendMsg(page, '#b world');
    await bothIntercepted;

    // ── LOOK: both requests in flight, no response bubbles yet ───────────────
    await expect(page.locator(RESPONSE)).not.toBeAttached();
    await look(page);  // pause — observe: two thinking bubbles, zero response bubbles

    await routes[1].fulfill({ status: 200, headers: SSE_HEADERS, body: sse(META, { data: 'Second done first' }, DONE) });
    await expect(page.locator(RESPONSE)).toHaveCount(1);
    await look(page);  // pause — observe: one bubble at bottom, other still thinking

    await routes[0].fulfill({ status: 200, headers: SSE_HEADERS, body: sse(META, { data: 'First done second' }, DONE) });
    await expect(page.locator(RESPONSE)).toHaveCount(2);
    await look(page);  // pause — observe: both bubbles at bottom in completion order
  });
});

test.describe('recovered pending responses', () => {
  test('healthy realtime Flow watcher expires without HTTP polling', async ({ page }) => {
    await page.addInitScript(() => {
      const nativeSetInterval = window.setInterval.bind(window);
      window.setInterval = (callback, delay, ...args) =>
        nativeSetInterval(callback, delay === 1500 ? 1 : delay, ...args);
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__webSocket = this;
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
          });
        }
        send(data) {
          if (JSON.parse(data).type === 'subscribe') {
            this.serverSubscribed = true;
            this.receive({ v: 1, type: 'subscribed', payload: {} });
          }
        }
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() { this.readyState = 3; this.onclose?.(); }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'auto' } }));
    let polls = 0;
    await page.route('**/chat/flow/run-expire/steps**', route => {
      polls++;
      route.fulfill({ json: { messages: [], complete: false } });
    });

    await page.goto('/');
    await page.waitForFunction(() => window.__webSocket?.serverSubscribed);
    await page.evaluate(() => watchFlowRun('run-expire', 10));
    await expect.poll(() => page.evaluate(
      () => _flowRunWatchers.has('run-expire'),
    ), { timeout: 4000 }).toBe(false);
    expect(polls).toBe(0);
  });

  test('auto mode resumes Flow polling when realtime disconnects', async ({ page }) => {
    await page.addInitScript(() => {
      window.__webSockets = [];
      window.__allowSockets = true;
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__webSockets.push(this);
          window.__activeWebSocket = this;
          if (window.__allowSockets) setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
          });
        }
        send(data) {
          if (JSON.parse(data).type === 'subscribe') {
            this.serverSubscribed = true;
            this.receive({ v: 1, type: 'subscribed', payload: {} });
          }
        }
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() {
          this.readyState = 3;
          const handler = this.onclose;
          setTimeout(() => handler?.());
        }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'auto' } }));
    let polls = 0;
    await page.route('**/chat/flow/run-auto/steps**', route => {
      polls++;
      route.fulfill({ json: { messages: [], complete: false } });
    });

    await page.goto('/');
    await page.waitForFunction(() => window.__activeWebSocket?.serverSubscribed);
    expect(await page.evaluate(() => realtimeTransportMode)).toBe('auto');
    expect(await page.evaluate(() => realtimeV1.isActive())).toBe(true);
    await page.evaluate(() => watchFlowRun('run-auto', 10));
    await page.waitForTimeout(1700);
    const activePolls = polls;
    await page.waitForTimeout(1700);
    expect(polls).toBe(activePolls);
    await page.evaluate(() => {
      window.__allowSockets = false;
      window.__activeWebSocket.close();
    });
    await expect.poll(() => polls, { timeout: 4000 }).toBeGreaterThan(activePolls);
  });

  test('Flow snapshot state stays out of transcript and live steps use completion order', async ({ page }) => {
    await page.addInitScript(() => {
      window.__webSocket = null;
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__webSocket = this;
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
          });
        }
        send(data) {
          if (JSON.parse(data).type === 'subscribe') {
            setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
          }
        }
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() { this.readyState = 3; this.onclose?.(); }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    let statusRequests = 0;
    const completions = { 400: '00:00:00.500', 401: '00:00:01', 402: '00:00:02', 403: '00:00:03' };
    for (const [id, completed] of Object.entries(completions)) {
      await page.route(`**/chat/${id}/status`, route => {
        statusRequests++;
        route.fulfill({ json: {
          id: Number(id), role: 'assistant', topic: 'squid', agent: 'claude', status: 'done',
          prompt: `Flow handoff ${id}`, content: `Flow result ${id}`, source: 'workflow',
          flow_run_id: 'run-401', flow_step_id: `step-${id}`,
          timestamp: '2027-01-01T00:00:00Z', completed_at: `2027-01-01T${completed}Z`,
        }});
      });
    }

    await page.goto('/');
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'snapshot', event_id: 10, payload: {
        conversations: [{ messages: [{
          id: 400, role: 'assistant', topic: 'squid', agent: 'claude', status: 'done',
        }] }],
        flow_steps: [401, 403, 402].map(assistant_msg_id => ({ assistant_msg_id })),
      },
    }));
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="400"]'))
      .toContainText('Flow result 400');
    expect(statusRequests).toBe(1);
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="401"]')).not.toBeAttached();
    await page.evaluate(() => {
      for (const [event_id, assistant_msg_id] of [[11, 401], [12, 403], [13, 402]]) {
        window.__webSocket.receive({
          v: 1, type: 'flow.step.created', event_id, msg_id: assistant_msg_id,
          scope: { topic: 'squid', agent: 'claude' },
          payload: { flow_run_id: 'run-401', step_id: `step-${assistant_msg_id}`, assistant_msg_id },
        });
      }
    });

    await expect(page.locator('.msg.assistant.history-item[data-msg-id]')).toHaveCount(4);
    expect(await page.locator('.msg.assistant.history-item[data-msg-id]').evaluateAll(
      nodes => nodes.map(node => Number(node.dataset.msgId)),
    )).toEqual([400, 401, 402, 403]);
  });

  test('reconnecting websocket replays without duplicating an already-attached flow step', async ({ page }) => {
    await page.addInitScript(() => {
      window.__webSockets = [];
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__webSockets.push(this);
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: {} });
          });
        }
        send(data) {
          const frame = JSON.parse(data);
          if (frame.type === 'subscribe') {
            this.subscribe = frame;
            setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
          }
        }
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() {
          this.readyState = 3;
          const handler = this.onclose;
          setTimeout(() => handler?.());
        }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    await page.route('**/chat/601/status', route => route.fulfill({ json: {
      id: 601, role: 'assistant', topic: 'squid', agent: 'claude', status: 'done',
      prompt: 'Flow handoff 601', content: 'Flow result 601', source: 'workflow',
      flow_run_id: 'run-601', flow_step_id: 'step-601',
      timestamp: '2027-01-01T00:00:00Z', completed_at: '2027-01-01T00:00:01Z',
    }}));
    await page.route('**/chat/602/status', route => route.fulfill({ json: {
      id: 602, role: 'assistant', topic: 'squid', agent: 'claude', status: 'done',
      prompt: 'Flow handoff 602', content: 'Flow result 602', source: 'workflow',
      flow_run_id: 'run-601', flow_step_id: 'step-602',
      timestamp: '2027-01-01T00:00:00Z', completed_at: '2027-01-01T00:00:02Z',
    }}));

    await page.goto('/');
    await page.waitForFunction(() => window.__webSockets[0]?.subscribe);
    await page.evaluate(() => window.__webSockets[0].receive({
      v: 1, type: 'flow.step.created', event_id: 30, msg_id: 601,
      scope: { topic: 'squid', agent: 'claude' },
      payload: { flow_run_id: 'run-601', step_id: 'step-601', assistant_msg_id: 601 },
    }));
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="601"]'))
      .toContainText('Flow result 601');

    // The connection drops and a fresh socket reconnects. The server replays
    // from the last acknowledged cursor, which includes the event already
    // applied plus the next one — the applied cursor must suppress the replay.
    await page.evaluate(() => window.__webSockets[0].close());
    await page.waitForFunction(() => window.__webSockets.length >= 2 && window.__webSockets.at(-1)?.subscribe);
    await page.evaluate(() => {
      const resumed = window.__webSockets.at(-1);
      resumed.receive({
        v: 1, type: 'flow.step.created', event_id: 30, msg_id: 601,
        scope: { topic: 'squid', agent: 'claude' },
        payload: { flow_run_id: 'run-601', step_id: 'step-601', assistant_msg_id: 601 },
      });
      resumed.receive({
        v: 1, type: 'flow.step.created', event_id: 31, msg_id: 602,
        scope: { topic: 'squid', agent: 'claude' },
        payload: { flow_run_id: 'run-601', step_id: 'step-602', assistant_msg_id: 602 },
      });
    });

    await expect(page.locator('.msg.assistant.history-item[data-msg-id="602"]'))
      .toContainText('Flow result 602');
    await expect(page.locator('.msg.assistant.history-item[data-msg-id]')).toHaveCount(2);
    expect(await page.locator('.msg.assistant.history-item[data-msg-id]').evaluateAll(
      nodes => nodes.map(node => Number(node.dataset.msgId)),
    )).toEqual([601, 602]);
  });

  test('flow snapshot rollover preserves an already-attached step and resumes live delivery', async ({ page }) => {
    await page.addInitScript(() => {
      window.__webSocket = null;
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__webSocket = this;
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
          });
        }
        send(data) {
          if (JSON.parse(data).type === 'subscribe') {
            setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
          }
        }
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() { this.readyState = 3; this.onclose?.(); }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    await page.route('**/chat/701/status', route => route.fulfill({ json: {
      id: 701, role: 'assistant', topic: 'squid', agent: 'claude', status: 'done',
      prompt: 'Flow handoff 701', content: 'Flow result 701', source: 'workflow',
      flow_run_id: 'run-701', flow_step_id: 'step-701',
      timestamp: '2027-01-01T00:00:00Z', completed_at: '2027-01-01T00:00:01Z',
    }}));
    await page.route('**/chat/702/status', route => route.fulfill({ json: {
      id: 702, role: 'assistant', topic: 'squid', agent: 'claude', status: 'done',
      prompt: 'Flow handoff 702', content: 'Flow result 702', source: 'workflow',
      flow_run_id: 'run-701', flow_step_id: 'step-702',
      timestamp: '2027-01-01T00:00:00Z', completed_at: '2027-01-01T00:00:02Z',
    }}));

    await page.goto('/');
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'flow.step.created', event_id: 40, msg_id: 701,
      scope: { topic: 'squid', agent: 'claude' },
      payload: { flow_run_id: 'run-701', step_id: 'step-701', assistant_msg_id: 701 },
    }));
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="701"]'))
      .toContainText('Flow result 701');

    // The server decides the retained range is unavailable and rolls over to
    // a fresh snapshot instead of replaying. Flow state in a snapshot stays
    // out of the transcript (verified above), so the already-applied step
    // must survive the rollover untouched rather than being duplicated or
    // dropped.
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'snapshot', event_id: 45, payload: {
        cursor_reset: true,
        conversations: [{ messages: [] }],
        flow_steps: [{ assistant_msg_id: 701 }],
      },
    }));
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="701"]'))
      .toContainText('Flow result 701');
    await expect(page.locator('.msg.assistant.history-item[data-msg-id]')).toHaveCount(1);

    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'flow.step.created', event_id: 46, msg_id: 702,
      scope: { topic: 'squid', agent: 'claude' },
      payload: { flow_run_id: 'run-701', step_id: 'step-702', assistant_msg_id: 702 },
    }));

    await expect(page.locator('.msg.assistant.history-item[data-msg-id="702"]'))
      .toContainText('Flow result 702');
    await expect(page.locator('.msg.assistant.history-item[data-msg-id]')).toHaveCount(2);
    expect(await page.locator('.msg.assistant.history-item[data-msg-id]').evaluateAll(
      nodes => nodes.map(node => Number(node.dataset.msgId)),
    )).toEqual([701, 702]);
  });

  test('websocket delivers a completed flow step through the live event path', async ({ page }) => {
    await page.addInitScript(() => {
      window.__webSocket = null;
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__webSocket = this;
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
          });
        }
        send(data) {
          if (JSON.parse(data).type === 'subscribe') {
            setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
          }
        }
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() { this.readyState = 3; this.onclose?.(); }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    await page.route('**/chat/801/status', route => route.fulfill({ json: {
      id: 801, role: 'assistant', topic: 'squid', agent: 'claude', status: 'done',
      prompt: 'Flow handoff 801', content: 'Flow result 801', source: 'workflow',
      flow_run_id: 'run-801', flow_step_id: 'step-801',
      timestamp: '2027-01-01T00:00:00Z', completed_at: '2027-01-01T00:00:01Z',
    }}));

    await page.goto('/');
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'flow.step.created', event_id: 50, msg_id: 801,
      scope: { topic: 'squid', agent: 'claude' },
      payload: { flow_run_id: 'run-801', step_id: 'step-801', assistant_msg_id: 801 },
    }));

    // Same final assertion as the sse-mode parity test below: both
    // transports must converge on one normalized turn for the same step.
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="801"]'))
      .toContainText('Flow result 801');
    await expect(page.locator('.msg.assistant.history-item[data-msg-id]')).toHaveCount(1);
  });

  test('sse-mode flow polling renders the same completed flow step as websocket delivery', async ({ page }) => {
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'sse' } }));
    await page.route('**/chat/801/status', route => route.fulfill({ json: {
      id: 801, role: 'assistant', topic: 'squid', agent: 'claude', status: 'done',
      prompt: 'Flow handoff 801', content: 'Flow result 801', source: 'workflow',
      flow_run_id: 'run-801', flow_step_id: 'step-801',
      timestamp: '2027-01-01T00:00:00Z', completed_at: '2027-01-01T00:00:01Z',
    }}));
    await page.route('**/chat/flow/run-801/steps**', route => route.fulfill({ json: {
      messages: [{ id: 801, role: 'assistant' }], complete: true,
    }}));

    await page.goto('/');
    await page.evaluate(() => watchFlowRun('run-801', 800));

    // Same final assertion as the websocket-mode parity test above: both
    // transports must converge on one normalized turn for the same step.
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="801"]'))
      .toContainText('Flow result 801');
    await expect(page.locator('.msg.assistant.history-item[data-msg-id]')).toHaveCount(1);
  });

  test('initial history installs before global snapshot discovery', async ({ page }) => {
    await page.setViewportSize({ width: 600, height: 500 });
    await page.addInitScript(() => {
      window.__webSocket = null;
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__webSocket = this;
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
          });
        }
        send(data) {
          const frame = JSON.parse(data);
          if (frame.type === 'subscribe') {
            this.subscribe = frame;
            setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
          }
        }
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() { this.readyState = 3; this.onclose?.(); }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    let releaseHistory;
    let historyRequestedResolve;
    const historyRequested = new Promise(resolve => { historyRequestedResolve = resolve; });
    await page.route('**/history**', route => {
      releaseHistory = () => route.fulfill({ json: { items: [
        { id: 202, role: 'assistant', topic: 'squid', agent: 'codex', status: 'done',
          prompt: 'newest', content: 'response 202\n'.repeat(80), timestamp: '2027-01-01T00:00:00Z',
          completed_at: '2027-01-01T00:00:03Z' },
        { id: 200, role: 'assistant', topic: 'squid', agent: 'codex', status: 'done',
          prompt: 'oldest', content: 'response 200', timestamp: '2027-01-01T00:00:00Z',
          completed_at: '2027-01-01T00:00:01Z' },
      ], has_more: false } });
      historyRequestedResolve();
    });
    await page.route('**/chat/198/status', route => route.fulfill({ json: {
      id: 198, role: 'assistant', topic: 'squid', agent: 'codex', status: 'done',
      prompt: 'middle', content: 'response 198', timestamp: '2027-01-01T00:00:00Z',
      completed_at: '2027-01-01T00:00:02Z',
    }}));

    await page.goto('/');
    await historyRequested;
    expect(await page.evaluate(() => window.__webSocket)).toBeNull();
    releaseHistory();
    await page.waitForFunction(() => window.__webSocket?.subscribe);
    await page.locator('#messages').evaluate(el => { el.scrollTop = el.scrollHeight; });
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'snapshot', event_id: 10, payload: { conversations: [{ messages: [
        { id: 198, role: 'assistant', topic: 'squid', agent: 'codex', status: 'done' },
      ] }] },
    }));

    await expect(page.locator('.msg.assistant.history-item[data-msg-id]')).toHaveCount(3);
    expect(await page.locator('.msg.assistant.history-item[data-msg-id]').evaluateAll(
      nodes => nodes.map(node => Number(node.dataset.msgId)),
    )).toEqual([200, 198, 202]);
    await expect.poll(() => page.locator('#messages').evaluate(
      el => el.scrollHeight - el.scrollTop - el.clientHeight,
    )).toBeLessThan(2);
  });

  test('empty reconnect snapshot shows a waiting state until activity resumes', async ({ page }) => {
    await page.addInitScript(() => {
      window.__webSocket = null;
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__webSocket = this;
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: {} });
          });
        }
        send() {}
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() { this.readyState = 3; this.onclose?.(); }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    await page.route('**/history**', route => route.fulfill({ json: {
      items: [{ id: 85, topic: 'squid', agent: 'claude', backend: 'claude', status: 'pending',
        prompt: 'idle mobile', content: '', adhoc: false }],
      has_more: false,
    }}));

    await page.goto('/');
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'snapshot', event_id: 10, payload: { conversations: [{
        messages: [{ id: 85, status: 'pending', content: '' }],
      }] },
    }));
    const live = page.locator(`${THINKING}[data-msg-id="85"] .thinking-live`);
    await expect(live.locator('.loader')).toBeVisible();
    await expect(live).toContainText('Waiting for new updates…');

    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'chat.status', event_id: 11, msg_id: 85, payload: { text: 'Working again' },
    }));
    await expect(live).toHaveText('Working again');
    await expect(live.locator('.thinking-waiting')).not.toBeAttached();
  });

  test('global lifecycle discovers a desktop turn and reconnect completion stays deduplicated', async ({ page }) => {
    await page.addInitScript(() => {
      window.__webSockets = [];
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__webSockets.push(this);
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: {} });
          });
        }
        send(data) {
          const frame = JSON.parse(data);
          if (frame.type === 'subscribe') {
            this.subscribe = frame;
            setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
          }
        }
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() {
          this.readyState = 3;
          const handler = this.onclose;
          setTimeout(() => handler?.());
        }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    let completed = false;
    let routeTurnCount = 0;
    await page.route('**/topics/desktop/session?agent=codex', route => route.fulfill({ json: {
      session_id: 'desktop-session', session_turn_count: routeTurnCount, cwd: null,
    }}));
    await page.route('**/chat/91/status', route => route.fulfill({ json: {
      id: 91, role: 'assistant', reply_to: 90, topic: 'desktop', agent: 'codex', adhoc: false,
      prompt: 'started elsewhere', status: completed ? 'done' : 'pending',
      content: completed ? 'Finished while asleep' : 'Started on desktop',
      completed_at: completed ? new Date().toISOString() : null,
    }}));
    const discoveredCompletions = {
      100: { delay: 80, completed_at: '2027-01-01T00:00:01Z' },
      102: { delay: 5, completed_at: '2027-01-01T00:00:02Z' },
      104: { delay: 30, completed_at: '2027-01-01T00:00:03Z' },
    };
    for (const [id, completion] of Object.entries(discoveredCompletions)) {
      await page.route(`**/chat/${id}/status`, async route => {
        await new Promise(resolve => setTimeout(resolve, completion.delay));
        await route.fulfill({ json: {
          id: Number(id), role: 'assistant', topic: 'desktop', agent: 'codex', adhoc: false,
          prompt: `turn ${id}`, status: 'done', content: `response ${id}`,
          timestamp: '2027-01-01T00:00:00Z', completed_at: completion.completed_at,
        }});
      });
    }
    const discoveredPending = {
      106: { delay: 60, timestamp: '2027-01-02T00:00:01Z' },
      108: { delay: 5, timestamp: '2027-01-02T00:00:02Z' },
    };
    for (const [id, pending] of Object.entries(discoveredPending)) {
      await page.route(`**/chat/${id}/status`, async route => {
        await new Promise(resolve => setTimeout(resolve, pending.delay));
        await route.fulfill({ json: {
          id: Number(id), role: 'assistant', topic: 'desktop', agent: 'codex', adhoc: false,
          prompt: `turn ${id}`, status: 'pending', content: `working ${id}`, timestamp: pending.timestamp,
        }});
      });
    }

    await page.goto('/');
    await page.waitForFunction(() => window.__webSockets[0]?.subscribe);
    await page.evaluate(() => setTopicChip('desktop', 'codex', false));
    await expect(page.locator('#topic-chip .chip-turn-count')).toHaveText('·0t');
    expect(await page.evaluate(() => window.__webSockets[0].subscribe.payload.scopes))
      .toContainEqual({ lifecycle: 'global' });
    await page.evaluate(() => window.__webSockets[0].receive({
      v: 1, type: 'chat.tool', event_id: 1, msg_id: 91, run_seq: 0,
      scope: { topic: 'desktop', agent: 'codex' },
      payload: { name: 'Bash', command: 'echo discovered without message.changed' },
    }));
    await expect(page.locator(`${THINKING}[data-msg-id="91"]`)).toContainText('Started on desktop');

    await setPageHidden(page, true);
    await setPageHidden(page, false);
    await page.waitForFunction(() => window.__webSockets.length >= 2 && window.__webSockets.at(-1)?.subscribe);
    completed = true;
    routeTurnCount = 1;
    await page.evaluate(() => {
      const resumed = window.__webSockets.at(-1);
      resumed.receive({ v: 1, type: 'snapshot', event_id: 4, payload: { conversations: [{
        messages: [{ id: 91, role: 'assistant', topic: 'desktop', agent: 'codex',
          status: 'pending', content: 'Finished while asleep', run_seq: 3 }],
      }] } });
      const done = { v: 1, type: 'message.changed', event_id: 5, msg_id: 91,
        scope: { topic: 'desktop', agent: 'codex' },
        payload: { id: 91, role: 'assistant', status: 'done', content: 'Finished while asleep' } };
      resumed.receive(done);
      resumed.receive(done);
    });

    await expect(page.locator(`${RESPONSE}[data-msg-id="91"]`)).toHaveCount(1);
    await expect(page.locator(`${RESPONSE}[data-msg-id="91"]`)).toContainText('Finished while asleep');
    await expect(page.locator(`${THINKING}[data-msg-id="91"]`)).not.toBeAttached();
    await expect(page.locator('#topic-chip .chip-turn-count')).toHaveText('·1t');

    await page.evaluate(() => window.__webSockets.at(-1).receive({
      v: 1, type: 'snapshot', event_id: 6, payload: { conversations: [{ messages: [
        { id: 100, role: 'assistant', status: 'done' },
        { id: 102, role: 'assistant', status: 'done' },
        { id: 104, role: 'assistant', status: 'done' },
      ] }] },
    }));
    await expect(page.locator(`${RESPONSE}[data-msg-id="100"], ${RESPONSE}[data-msg-id="102"], ${RESPONSE}[data-msg-id="104"]`))
      .toHaveCount(3);
    expect(await page.locator(`${RESPONSE}[data-msg-id]`).evaluateAll(nodes =>
      nodes.map(node => Number(node.dataset.msgId)).filter(id => [100, 102, 104].includes(id))))
      .toEqual([100, 102, 104]);

    await page.evaluate(() => window.__webSockets.at(-1).receive({
      v: 1, type: 'snapshot', event_id: 7, payload: { conversations: [{ messages: [
        { id: 106, role: 'assistant', status: 'pending' },
        { id: 108, role: 'assistant', status: 'pending' },
      ] }] },
    }));
    await expect(page.locator(`${THINKING}[data-msg-id="106"], ${THINKING}[data-msg-id="108"]`)).toHaveCount(2);
    expect(await page.locator(`${THINKING}[data-msg-id]`).evaluateAll(nodes =>
      nodes.map(node => Number(node.dataset.msgId)).filter(id => [106, 108].includes(id))))
      .toEqual([106, 108]);

    expect(await page.evaluate(() => {
      _sessionIds['desktop@codex'] = 'current-session';
      _setKnownSessionTurnCount('desktop', 'codex', 3, 'current-session');
      _setKnownSessionTurnCount('desktop', 'codex', 12, 'historical-session');
      return {
        route: _knownSessionTurnCount('desktop', 'codex'),
        historical: _sessionTurnCounts['historical-session'],
      };
    })).toEqual({ route: 3, historical: 12 });
  });

  test('foreground resume replaces a stale websocket and restores missed content from snapshot', async ({ page }) => {
    await page.addInitScript(() => {
      window.__webSockets = [];
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__webSockets.push(this);
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: {} });
          });
        }
        send(data) {
          const frame = JSON.parse(data);
          if (frame.type === 'subscribe') {
            this.subscribe = frame;
            setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
          }
        }
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() {
          this.readyState = 3;
          const closeHandler = this.onclose;
          setTimeout(() => closeHandler?.());
        }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    await page.route('**/history**', route => route.fulfill({ json: {
      items: [{ id: 84, topic: 'squid', agent: 'claude', backend: 'claude', status: 'pending',
        prompt: 'mobile suspension', content: 'Before lock', adhoc: false }],
      has_more: false,
    }}));

    await page.goto('/');
    await page.waitForFunction(() => window.__webSockets[0]?.subscribe);
    await page.evaluate(() => {
      const first = window.__webSockets[0];
      first.receive({ v: 1, type: 'snapshot', event_id: 20, payload: { conversations: [{
        messages: [{ id: 84, status: 'pending', content: 'Before lock', run_seq: 2 }],
      }] } });
    });
    await setPageHidden(page, true);
    await setPageHidden(page, false);
    await page.waitForFunction(() => window.__webSockets.length >= 2 && window.__webSockets.at(-1)?.subscribe);
    await page.evaluate(() => {
      const resumed = window.__webSockets.at(-1);
      resumed.receive({ v: 1, type: 'snapshot', event_id: 24, payload: { conversations: [{
        messages: [{ id: 84, status: 'pending', content: 'Before lock and while asleep', run_seq: 6 }],
      }] } });
      resumed.receive({ v: 1, type: 'chat.text', event_id: 25, msg_id: 84, run_seq: 7,
        payload: { text: ' plus live' } });
    });

    await expect(page.locator(`${THINKING}[data-msg-id="84"] .thinking-live`))
      .toHaveText('Before lock and while asleep plus live');
    expect(await page.evaluate(() => localStorage.getItem('squid-realtime-v1-cursor'))).toBe('25');
  });

  test('websocket snapshots advance the applied cursor and text replay deduplicates by run sequence', async ({ page }) => {
    await page.addInitScript(() => {
      window.__webSocketSent = [];
      window.__webSocket = null;
      class MockWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        constructor() {
          this.readyState = MockWebSocket.CONNECTING;
          window.__webSocket = this;
          setTimeout(() => {
            this.readyState = MockWebSocket.OPEN;
            this.onopen?.();
            this.receive({ v: 1, type: 'hello', payload: {} });
          });
        }
        send(data) { window.__webSocketSent.push(JSON.parse(data)); }
        receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
        close() { this.readyState = 3; this.onclose?.(); }
      }
      window.WebSocket = MockWebSocket;
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
    await page.route('**/history**', route => route.fulfill({ json: {
      items: [{
        id: 82,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'deduplicate replay',
        content: '',
        adhoc: false,
      }],
      has_more: false,
    }}));

    await page.goto('/');
    await page.waitForFunction(() => window.__webSocketSent.some(frame => frame.type === 'subscribe'));
    await page.evaluate(() => {
      window.__webSocket.receive({
        v: 1,
        type: 'snapshot',
        event_id: 10,
        payload: { conversations: [{ messages: [{ id: 82, status: 'pending', content: 'Base', run_seq: 4 }] }] },
      });
      window.__webSocket.receive({
        v: 1, type: 'chat.text', event_id: 11, msg_id: 82, run_seq: 4, payload: { text: ' BAD_REPLAY' },
      });
      window.__webSocket.receive({
        v: 1, type: 'chat.text', event_id: 12, msg_id: 82, run_seq: 5, payload: { text: ' plus' },
      });
    });

    await expect(page.locator(THINKING)).toContainText('Base plus');
    await expect(page.locator(THINKING)).not.toContainText('BAD_REPLAY');
    expect(await page.evaluate(() => ({
      cursor: localStorage.getItem('squid-realtime-v1-cursor'),
      ackIds: window.__webSocketSent.filter(frame => frame.type === 'ack').map(frame => frame.payload.event_id),
    }))).toEqual({ cursor: '12', ackIds: [10, 11, 12] });
  });

  test('sse transport mode does not create a WebSocket', async ({ page }) => {
    await page.addInitScript(() => {
      window.__webSocketCount = 0;
      window.__eventSources = [];
      window.WebSocket = class {
        constructor() { window.__webSocketCount++; }
      };
      window.EventSource = class {
        constructor(url) {
          this.url = url;
          this.listeners = {};
          window.__eventSources.push(this);
        }
        addEventListener(type, callback) { this.listeners[type] = callback; }
        close() {}
      };
    });
    await mockBackend(page);
    await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'sse' } }));
    await page.route('**/history**', route => route.fulfill({ json: {
      items: [{
        id: 81,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'transport toggle',
        content: 'History partial',
        adhoc: false,
      }],
      has_more: false,
    }}));

    await page.goto('/');
    await page.waitForFunction(() => window.__eventSources.length === 1);
    expect(await page.evaluate(() => ({
      webSockets: window.__webSocketCount,
      eventSourceUrl: window.__eventSources[0].url,
    }))).toEqual({ webSockets: 0, eventSourceUrl: '/chat/81/events' });
  });

  test('poll fallback keeps retrying after transient status failure', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(window, 'EventSource', { configurable: true, value: undefined });
      const realSetInterval = window.setInterval.bind(window);
      window.setInterval = (callback, delay, ...args) => realSetInterval(callback, delay === 2000 ? 20 : delay, ...args);
    });
    await mockBackend(page);

    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 79,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'long-running task',
        content: 'History partial',
        adhoc: false,
      }],
      has_more: false,
    }}));
    let statusCalls = 0;
    await page.route('**/chat/79/status', r => {
      statusCalls++;
      if (statusCalls === 1) return r.fulfill({ status: 500, body: '' });
      if (statusCalls === 2) return r.fulfill({ json: { id: 79, status: 'pending', content: 'Still working' } });
      return r.fulfill({ json: {
        id: 79,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'done',
        prompt: 'long-running task',
        content: 'Recovered after transient failure',
        adhoc: false,
        timestamp: new Date().toISOString(),
      }});
    });

    await page.goto('/');

    await expect(page.locator(RESPONSE).filter({ hasText: 'Recovered after transient failure' })).toBeVisible({ timeout: 5_000 });
    expect(statusCalls).toBeGreaterThanOrEqual(3);
    await expect(page.locator(THINKING)).not.toBeAttached();
  });

  test('pageshow reconnects stale pending event watcher', async ({ page }) => {
    await page.addInitScript(() => {
      window.__eventSources = [];
      window.EventSource = class {
        constructor(url) {
          this.url = url;
          this.closed = false;
          this.listeners = {};
          window.__eventSources.push(this);
        }
        addEventListener(type, callback) {
          this.listeners[type] = callback;
        }
        close() {
          this.closed = true;
        }
      };
    });
    await mockBackend(page);

    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 80,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'long-running task',
        content: 'History partial',
        adhoc: false,
      }],
      has_more: false,
    }}));

    await page.goto('/');
    await expect(page.locator(`${THINKING}[data-msg-id="80"]`)).toBeVisible();
    await page.waitForFunction(() => window.__eventSources.length === 1);

    await page.evaluate(() => window.dispatchEvent(new Event('pageshow')));

    await page.waitForFunction(() => window.__eventSources.length === 2);
    expect(await page.evaluate(() => ({
      firstClosed: window.__eventSources[0].closed,
      secondUrl: window.__eventSources[1].url,
    }))).toEqual({
      firstClosed: true,
      secondUrl: '/chat/80/events',
    });
  });

  test('refresh recovery streams pending status from event replay', async ({ page }) => {
    await mockBackend(page);

    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 77,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'long-running task',
        content: 'History partial',
        adhoc: false,
      }],
      has_more: false,
    }}));
    let eventsRequested = false;
    await page.route('**/chat/77/events', r => {
      eventsRequested = true;
      return r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(
        { event: 'status', data: 'Still connected after refresh' },
        { data: 'Recovered stream text' },
        DONE,
      ),
    });
    });
    await page.route('**/chat/77/status', r => r.fulfill({ json: {
      id: 77,
      topic: 'squid',
      agent: 'claude',
      backend: 'claude',
      status: 'done',
      prompt: 'long-running task',
      content: 'Recovered final response',
      adhoc: false,
      timestamp: new Date().toISOString(),
    }}));

    await page.goto('/');

    await expect(page.locator(RESPONSE).filter({ hasText: 'Recovered final response' })).toBeVisible();
    expect(eventsRequested).toBe(true);
    await expect(page.locator(THINKING)).not.toBeAttached();
  });

  test('refresh polling does not finalize an empty interrupted error', async ({ page }) => {
    await mockBackend(page);

    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 78,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'long-running task',
        content: 'History partial',
        adhoc: false,
      }],
      has_more: false,
    }}));
    await page.route('**/chat/78/events', r => r.fulfill({ status: 500, body: '' }));
    let statusCalls = 0;
    await page.route('**/chat/78/status', r => {
      statusCalls++;
      if (statusCalls === 1) {
        return r.fulfill({ json: { id: 78, status: 'pending', content: 'History partial' } });
      }
      if (statusCalls === 2) {
        return r.fulfill({ json: { id: 78, status: 'error', content: '' } });
      }
      return r.fulfill({ json: {
        id: 78,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'done',
        prompt: 'long-running task',
        content: 'Recovered final response',
        adhoc: false,
        timestamp: new Date().toISOString(),
      }});
    });

    await page.goto('/');

    await expect(page.locator(RESPONSE).filter({ hasText: 'Recovered final response' })).toBeVisible({ timeout: 8_000 });
    await expect(page.locator(MSG_ERROR).filter({ hasText: 'Response interrupted.' })).not.toBeAttached();
    await expect(page.locator(THINKING)).not.toBeAttached();
  });

  test('search back keeps one status bubble when live meta arrives after history', async ({ page }) => {
    await mockBackend(page);

    let exposePending = false;
    await page.route('**/history**', r => r.fulfill({ json: {
      items: exposePending ? [{
        id: 1,
        topic: 'default',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'long-running task',
        content: 'Working from history...',
        adhoc: false,
      }] : [],
      has_more: false,
    }}));
    await page.route('**/search**', r => r.fulfill({ json: { items: [] } }));

    const { intercepted, fulfill } = holdChat(page);
    await page.goto('/');
    await sendMsg(page, 'long-running task');
    await intercepted;

    // The pending row becomes visible to history before the held SSE sends meta.
    exposePending = true;
    await page.evaluate(() => startSearch('needle'));
    await page.evaluate(() => clearSearch());
    await expect(page.locator(`${THINKING}[data-msg-id="1"]`)).toHaveCount(1);
    await expect(page.locator(THINKING)).toHaveCount(2); // live (unidentified) + recovered WIP
    const recovered = page.locator(`${THINKING}[data-msg-id="1"]`);
    await expect(recovered.locator('.response-header')).toBeVisible();
    await expect(recovered.locator('.response-header-text')).toContainText('long-running task');
    await expect(recovered.locator('.history-prompt')).toBeVisible();
    expect(await recovered.evaluate(el => getComputedStyle(el).color)).toBe('rgb(136, 136, 136)');
    expect(await recovered.locator('.history-prompt').evaluate(el => getComputedStyle(el).color)).toBe('rgb(102, 102, 102)');
    await recovered.locator('.history-prompt').click();
    await expect(recovered.locator('.history-prompt-full.visible')).toHaveText('long-running task');
    expect(await recovered.locator('.history-prompt.expanded').evaluate(el => getComputedStyle(el).color)).toBe('rgb(136, 136, 136)');
    expect(await recovered.locator('.history-prompt-full.visible').evaluate(el => getComputedStyle(el).color)).toBe('rgb(136, 136, 136)');

    await fulfill(sse(META, { event: 'status', data: 'Still working...' }));

    // meta.msg_id reconciles the recovered WIP into the live SSE bubble.
    await expect(page.locator(THINKING)).toHaveCount(1);
    await expect(page.locator(`${THINKING}[data-msg-id="1"]`)).toHaveCount(1);
    await expect(page.locator(THINKING)).toContainText('Still working...');
  });

  test('completed response moves to bottom instead of replacing its status bubble', async ({ page }) => {
    await mockBackend(page);

    let recovered = false;
    await page.route('**/history**', r => r.fulfill({ json: {
      items: [{
        id: 41,
        topic: 'squid',
        agent: 'claude',
        backend: 'claude',
        status: 'pending',
        prompt: 'long-running task',
        content: 'Working...',
        adhoc: false,
      }],
      has_more: false,
    }}));
    await page.route('**/chat/41/status', r => r.fulfill({ json: recovered ? {
      id: 41,
      topic: 'squid',
      agent: 'claude',
      backend: 'claude',
      status: 'done',
      prompt: 'long-running task',
      content: 'Recovered final response',
      adhoc: false,
      timestamp: new Date().toISOString(),
    } : {
      id: 41,
      status: 'pending',
      content: 'Working...',
    }}));
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: SSE_HEADERS,
      body: sse(META, { data: 'Newer response' }, DONE),
    }));

    await page.goto('/');
    await expect(page.locator(THINKING)).toContainText('Working...');

    await sendMsg(page, 'new request');
    await expect(page.locator(RESPONSE).filter({ hasText: 'Newer response' })).toBeVisible();

    recovered = true;
    const recoveredBubble = page.locator(`${RESPONSE}[data-msg-id="41"]`);
    await expect(recoveredBubble).toContainText('Recovered final response', { timeout: 5_000 });
    await expect(page.locator(THINKING)).not.toBeAttached();
    await expect(page.locator('#messages > .msg.assistant').last()).toContainText('Recovered final response');
  });
});

// ── CLI auth panel transport (ADR-0040 step 1) ───────────────────────────────

test('websocket transport drives the auth panel over WS without POST /auth/session', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => {
    window.__authFrames = [];
    window.__ws = null;
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        window.__ws = this;
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'auth.start') {
          window.__authFrames.push(frame);
          setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
            payload: { ok: true, session_id: 'sess-1', harness: 'claudecode', command: 'claude auth login --claudeai' } }));
        } else if (frame.type === 'auth.input' || frame.type === 'auth.resize' || frame.type === 'auth.cancel') {
          window.__authFrames.push(frame);
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
  let httpAuthRequests = 0;
  await page.route('**/auth/session', route => {
    httpAuthRequests++;
    return route.abort();
  });

  await page.goto('/');
  await page.evaluate(() => openAuthPanel('claudecode', null));

  await page.waitForFunction(() => window.__authFrames.some(f => f.type === 'auth.start'));
  expect(httpAuthRequests).toBe(0);
  const start = await page.evaluate(() => window.__authFrames.find(f => f.type === 'auth.start'));
  expect(start.payload).toMatchObject({ harness: 'claudecode', mode: 'login' });
  expect(typeof start.request_id).toBe('string');
  expect(start.request_id.length).toBeGreaterThan(0);

  const keypad = page.locator('#auth-panel-keypad');
  await expect(keypad).toBeVisible();
  await keypad.getByRole('button', { name: 'Escape' }).click();
  await keypad.getByRole('button', { name: 'Up arrow' }).click();
  await keypad.getByRole('button', { name: 'Down arrow' }).click();
  await keypad.getByRole('button', { name: 'Enter' }).click();
  await expect.poll(() => page.evaluate(() => window.__authFrames
    .filter(f => f.type === 'auth.input').map(f => f.payload.data)))
    .toEqual(['\x1b', '\x1b[A', '\x1b[B', '\r']);

  // Streamed output + done arrive over the socket; a clean exit (0) on the
  // login flow closes the panel, which in turn cancels the WS session.
  await page.evaluate(() => {
    window.__ws.receive({ v: 1, type: 'auth.output', payload: { session_id: 'sess-1', data: btoa('login output') } });
    window.__ws.receive({ v: 1, type: 'auth.done', payload: { session_id: 'sess-1', returncode: 0 } });
  });
  await expect(page.locator('#auth-panel')).not.toHaveClass(/open/);
  await page.waitForFunction(() => window.__authFrames.some(f => f.type === 'auth.cancel'));
});

test('foreground resume() resends the live auth.start so the server can re-attach', async ({ page }) => {
  await page.addInitScript(() => {
    window.__authFrames = [];
    window.__ws = null;
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        window.__ws = this;
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'auth.start') {
          window.__authFrames.push(frame);
          setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
            payload: { ok: true, session_id: 'sess-1', harness: 'claudecode', command: 'claude auth login --claudeai' } }));
        } else if (frame.type === 'auth.input' || frame.type === 'auth.resize' || frame.type === 'auth.cancel') {
          window.__authFrames.push(frame);
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));

  await page.goto('/');
  await page.evaluate(() => openAuthPanel('claudecode', null));
  await page.waitForFunction(() => window.__authFrames.some(f => f.type === 'auth.start'));
  const firstStart = await page.evaluate(() => window.__authFrames.find(f => f.type === 'auth.start'));
  const socketBeforeResume = await page.evaluateHandle(() => window.__ws);

  // Simulate the visibilitychange handler backgrounding then foregrounding
  // the tab (e.g. to complete an OAuth redirect) — resume() force-closes the
  // socket and opens a new one, per ui/app.js's visibilitychange listener.
  // (The page's own pageshow/visibilitychange listeners may also fire their
  // own resume() calls independently in this headless environment — that's
  // fine, every resend still carries the same idempotent request_id.)
  await page.evaluate(() => realtimeV1.resume());

  // Wait for a *new* socket (identity, not count, since background
  // recovery listeners may cycle through more than one) to come up and
  // carry a resent auth.start with the original request_id.
  await page.waitForFunction(
    ([staleSocket, requestId]) => window.__ws && window.__ws !== staleSocket
      && window.__ws.readyState === 1
      && window.__authFrames.filter(f => f.type === 'auth.start' && f.request_id === requestId).length >= 2,
    [socketBeforeResume, firstStart.request_id],
  );

  const frames = await page.evaluate(() => window.__authFrames.filter(f => f.type === 'auth.start'));
  expect(frames.length).toBeGreaterThanOrEqual(2);
  // Every resend must be the *same* idempotent request, not a new spawn.
  for (const frame of frames) expect(frame.request_id).toBe(firstStart.request_id);

  // The panel is still usable on the new socket after the reattach.
  await page.evaluate(() => {
    window.__ws.receive({ v: 1, type: 'auth.output', payload: { session_id: 'sess-1', data: btoa('still here') } });
  });
  await expect(page.locator('#auth-panel')).toHaveClass(/open/);
});

test('websocket auth.done with null returncode reports failure, not success', async ({ page }) => {
  await page.addInitScript(() => {
    window.__ws = null;
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        window.__ws = this;
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'auth.start') {
          setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
            payload: { ok: true, session_id: 'sess-1', harness: 'claudecode', command: 'claude auth login --claudeai' } }));
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));

  await page.goto('/');
  await page.evaluate(() => openAuthPanel('claudecode', null));

  // A reaped session (idle timeout / server-side cancel) arrives with returncode
  // null; Number(null) === 0 would read as success and close the panel + fire the
  // retry. The client must report Exited (-1) and keep the panel open instead.
  await page.evaluate(() => window.__ws.receive({ v: 1, type: 'auth.done', payload: { session_id: 'sess-1', returncode: null } }));
  await expect(page.locator('#auth-panel-title')).toHaveText('Exited (-1)');
  await expect(page.locator('#auth-panel')).toHaveClass(/open/);
  await expect(page.locator('#auth-panel-retry-btn')).toBeVisible();
});

test('sse transport keeps the auth panel on the HTTP path', async ({ page }) => {
  await page.addInitScript(() => {
    // No WebSocket should be created in sse mode; record any auth.start just
    // in case so the assertion below is meaningful rather than vacuously empty.
    window.__authFrames = [];
  });
  await mockBackend(page); // defaults to transport: sse
  let httpAuthRequests = 0;
  await page.route('**/auth/session', route => {
    httpAuthRequests++;
    return route.fulfill({ status: 200, headers: { 'Content-Type': 'application/json' },
      json: { id: 'http-sess-1', harness: 'claudecode', command: 'claude auth login --claudeai' } });
  });

  await page.goto('/');
  await page.evaluate(() => openAuthPanel('claudecode', null));

  await expect.poll(() => httpAuthRequests).toBe(1);
  expect(await page.evaluate(() => window.__authFrames.length)).toBe(0);
  await expect(page.locator('#auth-panel')).toHaveClass(/open/);
});

// ── Keychain-unlock remediation (docs/plans/cursor-keychain-unlock-remediation.md) ──

const KEYCHAIN_LOCKED_OUTPUT =
  'Error: Your macOS login keychain is locked.\nRun security unlock-keychain and try again.';

test('locked-keychain cursor login output shows the unlock affordance', async ({ page }) => {
  await page.addInitScript(() => {
    window.__authFrames = [];
    window.__ws = null;
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        window.__ws = this;
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'auth.start') {
          window.__authFrames.push(frame);
          setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
            payload: { ok: true, session_id: `sess-${window.__authFrames.length}`, harness: frame.payload.harness, command: 'cursor login' } }));
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));

  await page.goto('/');
  await page.evaluate(() => openAuthPanel('cursor', null));
  await page.waitForFunction(() => window.__authFrames.some(f => f.type === 'auth.start'));

  await expect(page.locator('#auth-panel-unlock-btn')).toBeHidden();
  await page.evaluate((output) => {
    window.__ws.receive({ v: 1, type: 'auth.output', payload: { session_id: 'sess-1', data: btoa(output) } });
  }, KEYCHAIN_LOCKED_OUTPUT);
  await expect(page.locator('#auth-panel-unlock-btn')).toBeVisible();
});

test('completing the unlock (exit 0) auto-retries the original cursor login', async ({ page }) => {
  await page.addInitScript(() => {
    window.__authFrames = [];
    window.__ws = null;
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        window.__ws = this;
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'auth.start') {
          window.__authFrames.push(frame);
          const command = frame.payload.mode === 'unlock' ? 'security unlock-keychain' : 'cursor login';
          setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
            payload: { ok: true, session_id: `sess-${window.__authFrames.length}`, harness: frame.payload.harness, command } }));
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));

  await page.goto('/');
  await page.evaluate(() => openAuthPanel('cursor', null));
  await page.waitForFunction(() => window.__authFrames.length === 1);

  await page.evaluate((output) => {
    window.__ws.receive({ v: 1, type: 'auth.output', payload: { session_id: 'sess-1', data: btoa(output) } });
  }, KEYCHAIN_LOCKED_OUTPUT);
  await page.click('#auth-panel-unlock-btn');

  await page.waitForFunction(() => window.__authFrames.length === 2);
  const unlockStart = await page.evaluate(() => window.__authFrames[1]);
  expect(unlockStart.payload.mode).toBe('unlock');
  await expect(page.locator('#auth-panel-title')).toHaveText('Unlock macOS keychain');

  // The unlock exits 0 (password accepted) — the original cursor login must
  // be automatically re-issued, not just close the panel.
  await page.evaluate(() => {
    window.__ws.receive({ v: 1, type: 'auth.done', payload: { session_id: 'sess-2', returncode: 0 } });
  });
  await page.waitForFunction(() => window.__authFrames.length === 3);
  const retryStart = await page.evaluate(() => window.__authFrames[2]);
  expect(retryStart.payload).toMatchObject({ harness: 'cursor', mode: 'login' });
  await expect(page.locator('#auth-panel')).toHaveClass(/open/);
});

test('server unlock_requires_local refusal is surfaced without a password prompt', async ({ page }) => {
  await page.addInitScript(() => {
    window.__authFrames = [];
    window.__ws = null;
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        window.__ws = this;
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'auth.start') {
          window.__authFrames.push(frame);
          const payload = frame.payload.mode === 'unlock'
            ? { ok: false, error: 'unlock_requires_local',
                detail: 'Keychain unlock is only available from a loopback client unless auth.allow_remote_keychain_unlock is enabled.' }
            : { ok: true, session_id: `sess-${window.__authFrames.length}`, harness: frame.payload.harness, command: 'cursor login' };
          setTimeout(() => this.receive({ v: 1, type: 'command.result', request_id: frame.request_id, payload }));
        } else if (frame.type === 'auth.input') {
          window.__authFrames.push(frame);
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));

  await page.goto('/');
  await page.evaluate(() => openAuthPanel('cursor', null));
  await page.waitForFunction(() => window.__authFrames.length === 1);

  await page.evaluate((output) => {
    window.__ws.receive({ v: 1, type: 'auth.output', payload: { session_id: 'sess-1', data: btoa(output) } });
  }, KEYCHAIN_LOCKED_OUTPUT);
  await page.click('#auth-panel-unlock-btn');

  await page.waitForFunction(() => window.__authFrames.length === 2);
  // The refusal detail goes to the terminal body; the panel title stays a
  // short, stable label instead of echoing the full message.
  await expect(page.locator('#auth-panel-title')).toHaveText('Failed to start');
  await expect(page.locator('#auth-panel-retry-btn')).toBeVisible();

  // No onData listener is ever wired for a session that failed to start, so
  // typing at the terminal cannot reach the server as a password.
  await page.locator('#auth-panel-term').click();
  await page.keyboard.type('hunter2');
  expect(await page.evaluate(() => window.__authFrames.some(f => f.type === 'auth.input'))).toBe(false);
});

test('cancelling during the auth.start round-trip cancels the spawned session, not resurrects the panel', async ({ page }) => {
  await page.addInitScript(() => {
    window.__authFrames = [];
    window.__ws = null;
    window.__pendingAuthStart = null;
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        window.__ws = this;
        this.readyState = MockWebSocket.CONNECTING;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'auth.start') {
          window.__authFrames.push(frame);
          // Hold the command.result open so the test can cancel while the
          // auth.start round-trip is still in flight.
          window.__pendingAuthStart = frame;
        } else if (frame.type === 'auth.cancel') {
          window.__authFrames.push(frame);
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));

  await page.goto('/');
  // Don't await the panel open — it blocks on the held command.result.
  await page.evaluate(() => { window.__openAuthPanel = openAuthPanel('claudecode', null); });
  await page.waitForFunction(() => window.__pendingAuthStart);

  // Cancel while auth.start is in flight; the server has already spawned the
  // PTY, but the client doesn't have its session_id yet.
  await page.evaluate(() => closeAuthPanel());

  // The command.result finally lands for the session that was just spawned.
  await page.evaluate(() => {
    const frame = window.__pendingAuthStart;
    window.__ws.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
      payload: { ok: true, session_id: 'sess-1', harness: 'claudecode', command: 'claude auth login' } });
  });

  // The orphaned session must be cancelled, and the panel must stay closed
  // (no resurrection onto the disposed terminal).
  await page.waitForFunction(() => window.__authFrames.some(f => f.type === 'auth.cancel'));
  const cancelFrame = await page.evaluate(() => window.__authFrames.find(f => f.type === 'auth.cancel'));
  expect(cancelFrame.payload).toEqual({ session_id: 'sess-1' });
  await expect(page.locator('#auth-panel')).not.toHaveClass(/open/);
});
