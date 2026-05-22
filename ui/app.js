const messages    = document.getElementById('messages');
const form        = document.getElementById('form');
const input       = document.getElementById('input');
const scrollBtn   = document.getElementById('scroll-btn');
const settingsBtn = document.getElementById('settings-btn');
const settingsBar = document.getElementById('settings-bar');
const statsBtn    = document.getElementById('stats-btn');
const statsPanel  = document.getElementById('stats-panel');
const statsContent= document.getElementById('stats-content');

messages.addEventListener('scroll', () => {
  scrollBtn.classList.toggle('visible', !isAtBottom());
});
scrollBtn.addEventListener('click', () => {
  messages.scrollTop = messages.scrollHeight;
});
scrollBtn.addEventListener('mouseenter', () => {
  scrollBtn.style.background = '#3a3a45';
  scrollBtn.style.borderColor = '#666';
  scrollBtn.style.opacity = '1';
});
scrollBtn.addEventListener('mouseleave', () => {
  scrollBtn.style.background = '';
  scrollBtn.style.borderColor = '';
  scrollBtn.style.opacity = '';
});

marked.setOptions({ breaks: true });

// ── settings / lookback ───────────────────────────────────────────────────────

let lookback = localStorage.getItem('lookback') === 'all' ? 'all'
             : localStorage.getItem('lookback') != null ? (parseInt(localStorage.getItem('lookback'), 10) || 0)
             : 5;

function initSettings() {
  document.querySelectorAll('.lb').forEach(btn => {
    const val = btn.dataset.val === 'all' ? 'all' : parseInt(btn.dataset.val);
    btn.classList.toggle('active', val === lookback);
    btn.addEventListener('click', () => {
      lookback = val;
      localStorage.setItem('lookback', val);
      document.querySelectorAll('.lb').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      updatePinBtnStates();
    });
  });

  document.getElementById('reset-pins-btn').addEventListener('click', async () => {
    await fetch('/chat/reset-pins', { method: 'POST' });
    document.querySelectorAll('.pin-btn.pinned, .pin-btn.excluded').forEach(b => {
      b.classList.remove('pinned', 'excluded');
      const pos = b.dataset.topicPos ? parseInt(b.dataset.topicPos) : null;
      b.classList.toggle('active', _inLookbackWindow(pos));
    });
  });
}

settingsBtn.addEventListener('click', () => {
  const open = settingsBar.classList.toggle('open');
  settingsBtn.classList.toggle('active', open);
  if (open) loadAliases();
});

// ── prompt parsing ────────────────────────────────────────────────────────────

// ── topic chip ────────────────────────────────────────────────────────────────

const topicChipEl = document.getElementById('topic-chip');
let stickyChip = null; // { topic, alias } | null

function setTopicChip(topic, alias) {
  if (topic === 'default' && !alias) { clearTopicChip(); return; }
  stickyChip = { topic, alias };
  topicChipEl.textContent = alias ? `#${topic}@${alias}` : `#${topic}`;
  topicChipEl.classList.add('visible');
  input.placeholder = 'message…';
}

function clearTopicChip() {
  stickyChip = null;
  topicChipEl.classList.remove('visible');
  input.placeholder = '#topic or #topic@alias message…';
}

topicChipEl.addEventListener('click', () => { clearTopicChip(); input.focus(); });

function parseInput(text) {
  // If chip is active and textarea has no #-prefix, use chip context
  if (stickyChip && !text.startsWith('#')) {
    return { topic: stickyChip.topic, alias: stickyChip.alias, message: text.trim() || text };
  }
  const m = text.match(/^#(\w+)(?:@(\w+))?\s+([\s\S]*)$/);
  if (m && m[3].trim()) {
    return { topic: m[1], alias: m[2] || null, message: m[3].trim() };
  }
  return { topic: 'default', alias: null, message: text };
}

// ── history pagination (display) ─────────────────────────────────────────────

let historyOffset = 0;
let historyExhausted = false;
let historyLoading = false;
let topSentinel = null;
let historyGlobalCount = 0;  // newest-first global position across all paginated loads

function createTopSentinel() {
  const el = document.createElement('div');
  el.id = 'history-sentinel';
  return el;
}

async function loadHistory() {
  if (historyExhausted || historyLoading) return;
  historyLoading = true;

  let data;
  try {
    const res = await fetch(`/history?offset=${historyOffset}&limit=5`);
    data = await res.json();
  } catch {
    historyLoading = false;
    return;
  }

  const { items, has_more } = data;
  const prevHeight = messages.scrollHeight;
  const fragment = document.createDocumentFragment();

  // Assign newest-first global position (items arrive newest-first from server)
  const itemsWithPos = items.map(item => {
    return { ...item, _topicPos: ++historyGlobalCount };
  });

  for (const item of [...itemsWithPos].reverse()) {
    if (!item.prompt && !item.response) continue;

    const group = document.createElement('div');
    group.className = 'history-group';

    const userBubble = makeUserBubble(item.prompt || '', item.topic, item.alias, item.backend);
    group.appendChild(userBubble);
    const userTs = item.user_timestamp || item.timestamp;
    if (userTs) {
      const uts = document.createElement('div');
      uts.className = 'msg-time right';
      uts.textContent = fmtTime(userTs);
      group.appendChild(uts);
    }

    const asstBubble = document.createElement('div');
    asstBubble.className = 'msg assistant';
    const asstHeader = document.createElement('div');
    asstHeader.className = 'response-header';
    const asstTag = document.createElement('span');
    asstTag.className = 'topic-tag';
    const asstLabel = item.alias || item.backend;
    asstTag.textContent = asstLabel ? `#${item.topic}@${asstLabel}` : `#${item.topic}`;
    const asstHeaderText = document.createElement('span');
    asstHeaderText.className = 'response-header-text';
    asstHeaderText.appendChild(asstTag);
    asstHeaderText.appendChild(document.createTextNode('  ' + truncate(item.prompt || '', 55)));
    asstHeader.appendChild(asstHeaderText);
    const inLookback = _inLookbackWindow(item._topicPos);
    asstHeader.appendChild(makePinBtn(item.id, item.pinned || 0, inLookback, item._topicPos));
    asstBubble.appendChild(asstHeader);
    const asstContent = document.createElement('div');
    if (item.status === 'pending') {
      addLoader(asstContent);
      pollMessageStatus(item.id, asstContent, asstBubble);
    } else if (item.status === 'error') {
      const raw = (item.response || '').split('\n')[0].replace(/^CLI exited \d+:\s*/, '').trim();
      asstContent.innerHTML = `<span class="msg-error">${raw || 'Response interrupted.'}</span>`;
    } else {
      asstContent.innerHTML = marked.parse(item.response || '');
    }
    asstBubble.appendChild(asstContent);
    group.appendChild(asstBubble);

    if (item.stats) addStats(asstBubble, item.stats, null, item.timestamp);

    fragment.appendChild(group);
  }

  const anchor = topSentinel ? topSentinel.nextSibling : messages.firstChild;
  messages.insertBefore(fragment, anchor);
  messages.scrollTop += messages.scrollHeight - prevHeight;

  historyOffset += items.length;
  historyExhausted = !has_more;
  historyLoading = false;

  if (historyExhausted && topSentinel) {
    topSentinel.remove();
    topSentinel = null;
  }
}

function initHistoryScroll() {
  topSentinel = createTopSentinel();
  messages.insertBefore(topSentinel, messages.firstChild);

  const observer = new IntersectionObserver(
    (entries) => { if (entries[0].isIntersecting) loadHistory(); },
    { root: messages, threshold: 0 },
  );
  observer.observe(topSentinel);
}

// ── live chat ────────────────────────────────────────────────────────────────

function parseCommand(message) {
  const t = message.trim();
  if (/^stop$/i.test(t))    return { command: 'stop' };
  if (/^stopall$/i.test(t)) return { command: 'stopall' };
  if (/^list$/i.test(t))    return { command: 'list' };
  const m = t.match(/^deq(?:\s+(-?\d+))?$/i);
  if (m) return { command: 'deq', pos: m[1] != null ? parseInt(m[1]) : null };
  return null;
}

async function handleCommand(cmd, topic) {
  const label = cmd.command === 'deq'
    ? `deq${cmd.pos != null ? ' ' + cmd.pos : ''}`
    : cmd.command;
  const feedbackEl = showCmdFeedback(`${label}…`);

  try {
    const body = { command: cmd.command, topic };
    if (cmd.pos != null) body.pos = cmd.pos;
    const res = await fetch('/cmd', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) { feedbackEl.textContent = `${label} failed`; return; }

    if (cmd.command === 'list') {
      feedbackEl.remove();
      renderTopicList(data.topics);
    } else {
      const detail = cmd.command === 'stop'    ? `#${topic} — killed ${data.killed}`
                   : cmd.command === 'stopall' ? `#${topic} — killed ${data.killed}, drained ${data.drained}`
                   : `#${topic} — drained ${data.drained}`;
      feedbackEl.textContent = `${label} ${detail}`;
    }
  } catch {
    feedbackEl.textContent = `${label} — request failed`;
  }
}

function renderTopicList(topics) {
  const el = document.createElement('div');
  el.className = 'cmd-topic-list';
  if (!topics.length) {
    el.textContent = 'no topics yet';
    messages.appendChild(el);
    scrollToBottom();
    return;
  }
  topics.forEach(t => {
    const row = document.createElement('div');
    row.className = 'ctl-row';
    const tag    = document.createElement('span');
    tag.className = 'ctl-tag';
    tag.textContent = t.alias ? `#${t.name}@${t.alias}` : `#${t.name}`;
    const prompt = document.createElement('span');
    prompt.className = 'ctl-prompt';
    prompt.textContent = t.last_prompt ? truncate(t.last_prompt, 60) : '—';
    const when = document.createElement('span');
    when.className = 'ctl-time';
    when.textContent = t.last_at ? fmtTime(t.last_at) : '—';
    row.append(tag, prompt, when);
    row.addEventListener('click', () => {
      setTopicChip(t.name, t.alias || null);
      input.focus();
    });
    el.appendChild(row);
  });
  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
}

function showCmdFeedback(text) {
  const el = document.createElement('div');
  el.className = 'cmd-feedback';
  el.textContent = text;
  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
  return el;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  const { topic, alias, message } = parseInput(text);
  const cmd = parseCommand(message);
  if (cmd) {
    input.value = '';
    await handleCommand(cmd, topic);
    return;
  }
  input.value = '';
  sendMessage(text);
});

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
  if (e.key === 'Backspace' && input.value === '' && stickyChip) {
    e.preventDefault();
    clearTopicChip();
  }
});

async function sendMessage(text) {
  const { topic, alias, message } = parseInput(text);
  setTopicChip(topic, alias);
  const sendTime = new Date().toISOString();

  // User bubble
  const userBubble = makeUserBubble(message, topic, alias);
  const userTopicTag = userBubble.querySelector('.topic-tag');
  messages.appendChild(userBubble);
  addTimestamp(userBubble, sendTime, true);
  requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });

  // ── Thinking bubble (visible immediately, shows status/queue/loader) ──────────
  const thinkingBubble = document.createElement('div');
  thinkingBubble.className = 'msg assistant msg-thinking';
  const thinkingContent = document.createElement('div');
  thinkingBubble.appendChild(thinkingContent);
  messages.appendChild(thinkingBubble);
  const thinkingLoader = addLoader(thinkingContent);
  let thinkingFrozen = false;
  let statusBuf = '';

  function setThinkingText(text) {
    if (thinkingFrozen) return;
    if (thinkingLoader.parentNode) thinkingLoader.remove();
    thinkingContent.textContent = text;
    scrollToBottom();
  }
  function freezeThinking() {
    if (thinkingFrozen) return;
    thinkingFrozen = true;
    thinkingBubble.remove();
  }

  requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });

  // ── Response bubble (not yet in DOM — appended on first content chunk) ────────
  const bubble = document.createElement('div');
  bubble.className = 'msg assistant';
  const responseHeader = document.createElement('div');
  responseHeader.className = 'response-header';
  const responseHeaderTag = document.createElement('span');
  responseHeaderTag.className = 'topic-tag';
  responseHeaderTag.textContent = alias ? `#${topic}@${alias}` : `#${topic}`;
  const headerText = document.createElement('span');
  headerText.className = 'response-header-text';
  headerText.appendChild(responseHeaderTag);
  headerText.appendChild(document.createTextNode('  ' + truncate(message, 55)));
  responseHeader.appendChild(headerText);
  const pinBtn = makePinBtn(null, 0, lookback > 0 || lookback === 'all');
  responseHeader.appendChild(pinBtn);
  bubble.appendChild(responseHeader);
  const contentDiv = document.createElement('div');
  bubble.appendChild(contentDiv);

  let firstDataReceived = false;
  const quotaBefore = quotaRaw;
  let lastSessionId = null;
  let statsEl = null;
  let doneTime = null;

  const ctxLabel = lookback === 0 ? 'off' : lookback === 'all' ? 'all' : String(lookback);

  const res = await fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, topic, alias, lookback }),
  });

  const reader  = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let raw = '';
  let eventName = null;
  let dataLineCount = 0;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buf += decoder.decode(value, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop();

    for (const line of lines) {
      if (line.startsWith('event:')) {
        eventName = line.slice(6).trim();
        dataLineCount = 0;
      } else if (line.startsWith('data:')) {
        const data = line.slice(5);
        dataLineCount++;

        if (eventName === 'meta') {
          try {
            const meta = JSON.parse(data);
            const label = meta.alias || meta.backend;
            const fullTag = `#${topic}@${label}`;
            responseHeaderTag.textContent = fullTag;
            if (userTopicTag) userTopicTag.textContent = fullTag + ' ';
            if (meta.msg_id) pinBtn.dataset.msgId = meta.msg_id;
          } catch {}
          eventName = null;

        } else if (eventName === 'queued') {
          try {
            const info = JSON.parse(data);
            setThinkingText(`#${info.topic} · queued — position ${info.position}`);
          } catch {}
          eventName = null;

        } else if (eventName === 'stats') {
          try {
            const stats = JSON.parse(data);
            lastSessionId = stats.session_id ?? null;
            statsEl = addStats(bubble, stats, ctxLabel, new Date().toISOString());
          } catch {}
          eventName = null;

        } else if (eventName === 'status') {
          statusBuf += data;
          const trimmed = statusBuf.replace(/\s+/g, ' ').trim();
          setThinkingText(trimmed.length > 120 ? '…' + trimmed.slice(-117) : trimmed);
          // no eventName reset — allow multi-line accumulation

        } else if (eventName === 'done') {
          freezeThinking();
          doneTime = new Date().toISOString();
          eventName = null;

        } else if (eventName === 'error') {
          freezeThinking();
          if (!firstDataReceived) {
            firstDataReceived = true;
            messages.appendChild(bubble);
            messages.scrollTop = messages.scrollHeight;
          }
          const errLine = data.trim();
          const errDisplay = errLine.split('\n')[0].replace(/^CLI exited \d+:\s*/, '');
          contentDiv.innerHTML = `<span class="msg-error">${errDisplay}</span>`;
          scrollToBottom();
          eventName = null;

        } else {
          // Actual response content — reveal the response bubble on first chunk
          if (!firstDataReceived) {
            firstDataReceived = true;
            freezeThinking();
            messages.appendChild(bubble);
            requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
          }
          if (dataLineCount > 1) raw += '\n';
          raw += data;
          contentDiv.innerHTML = marked.parse(raw);
          scrollToBottom();
        }

      } else if (line === '') {
        eventName = null;
        dataLineCount = 0;
      }
    }
  }

  if (!thinkingFrozen) freezeThinking();
  if (!statsEl && doneTime && firstDataReceived) addTimestamp(bubble, doneTime, false);

  // Quota delta
  await fetchQuota(true);
  if (statsEl && quotaDelta != null) {
    statsEl.querySelector('.stats-quota-delta').textContent = `  ·  quota +${quotaDelta}%`;
  }
  if (lastSessionId && quotaBefore !== null && quotaRaw !== null) {
    fetch('/stats/quota-delta', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: lastSessionId, before: quotaBefore, after: quotaRaw }),
    }).catch(() => {});
  }
}

// ── helpers ──────────────────────────────────────────────────────────────────

function makePinBtn(msgId, pinnedState, inLookback = false, topicPos = null) {
  // pinnedState: 1 = pinned (always include), 0 = default, -1 = excluded (always skip)
  const btn = document.createElement('button');
  btn.className = 'pin-btn';
  if (pinnedState === 1) btn.classList.add('pinned');
  else if (pinnedState === -1) btn.classList.add('excluded');
  else if (inLookback) btn.classList.add('active');
  btn.type = 'button';
  btn.title = 'ctx';
  btn.textContent = 'ctx';
  if (msgId) btn.dataset.msgId = msgId;
  if (topicPos !== null) btn.dataset.topicPos = String(topicPos);
  btn.addEventListener('click', async () => {
    if (!btn.dataset.msgId) return;
    const pos = btn.dataset.topicPos ? parseInt(btn.dataset.topicPos) : null;
    let newPinned;
    if (btn.classList.contains('pinned')) {
      // pinned → default
      newPinned = 0;
      btn.classList.remove('pinned');
      btn.classList.toggle('active', _inLookbackWindow(pos));
    } else if (btn.classList.contains('active')) {
      // in-window → excluded
      newPinned = -1;
      btn.classList.remove('active');
      btn.classList.add('excluded');
    } else if (btn.classList.contains('excluded')) {
      // excluded → default (restore window state)
      newPinned = 0;
      btn.classList.remove('excluded');
      btn.classList.toggle('active', _inLookbackWindow(pos));
    } else {
      // dim (outside window) → pinned
      newPinned = 1;
      btn.classList.add('pinned');
    }
    await fetch(`/chat/${btn.dataset.msgId}/pin`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pinned: newPinned }),
    });
  });
  return btn;
}

function _inLookbackWindow(topicPos) {
  if (lookback === 'all') return true;
  if (lookback === 0) return false;
  if (topicPos === null) return lookback > 0;
  return topicPos <= lookback;
}

function updatePinBtnStates() {
  document.querySelectorAll('.pin-btn').forEach(pb => {
    if (pb.classList.contains('pinned') || pb.classList.contains('excluded')) return;
    if (!pb.dataset.topicPos) return;
    pb.classList.toggle('active', _inLookbackWindow(parseInt(pb.dataset.topicPos)));
  });
}

async function pollMessageStatus(msgId, contentEl, bubbleEl) {
  const MAX_POLLS = 480;  // 16 min at 2s intervals — covers 15 min alias timeout
  let count = 0;
  const timer = setInterval(async () => {
    count++;
    try {
      const res = await fetch(`/chat/${msgId}/status`);
      if (!res.ok) { clearInterval(timer); return; }
      const data = await res.json();
      if (data.status === 'done') {
        clearInterval(timer);
        contentEl.innerHTML = marked.parse(data.content || '');
      } else if (data.status === 'error') {
        clearInterval(timer);
        const raw = (data.content || '').split('\n')[0].replace(/^CLI exited \d+:\s*/, '').trim();
        contentEl.innerHTML = `<span class="msg-error">${raw || 'Response interrupted.'}</span>`;
      } else if (count >= MAX_POLLS) {
        clearInterval(timer);
        contentEl.innerHTML = '<span class="msg-error">Response timed out.</span>';
      }
    } catch {
      clearInterval(timer);
    }
  }, 2000);
}

function makeUserBubble(text, topic, alias, backendFallback = null) {
  const div = document.createElement('div');
  div.className = 'msg user';
  const content = document.createElement('div');
  if (topic && topic !== 'default') {
    const label = alias || backendFallback;
    const tag = document.createElement('span');
    tag.className = 'topic-tag';
    tag.textContent = (label ? `#${topic}@${label}` : `#${topic}`) + ' ';
    content.appendChild(tag);
  }
  content.appendChild(document.createTextNode(text));
  div.appendChild(content);
  return div;
}


function isAtBottom() {
  return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 150;
}

function scrollToBottom() {
  if (isAtBottom()) messages.scrollTop = messages.scrollHeight;
}

function addLoader(bubble) {
  const el = document.createElement('span');
  el.className = 'loader';
  el.innerHTML = '<span></span><span></span><span></span>';
  bubble.appendChild(el);
  return el;
}

function addStats(bubble, stats, ctxLabel, timestamp) {
  const el = document.createElement('div');
  el.className = 'stats';

  const cacheRead  = stats.cache_read_tokens  || 0;
  const cacheWrite = stats.cache_write_tokens || 0;
  const input      = stats.input_tokens       || 0;
  const out        = stats.output_tokens      || 0;
  const reasoning  = stats.reasoning_tokens   || 0;
  const inp        = input + cacheRead + cacheWrite;
  const hasCost    = stats.cost_usd != null;
  const cost       = hasCost ? `$${stats.cost_usd.toFixed(4)}` : '';
  const cache      = cacheRead ? ` · ${fmtNum(cacheRead)} cached` : '';
  const reason     = reasoning ? ` · ${fmtNum(reasoning)} reasoning` : '';
  const dur        = stats.duration_ms ? ` · ${(stats.duration_ms / 1000).toFixed(1)}s` : '';
  const ctx        = ctxLabel != null ? `  · ctx:${ctxLabel}` : '';
  const timePrefix = timestamp ? fmtTime(timestamp) + '  ·  ' : '';

  el.appendChild(document.createTextNode(
    `${timePrefix}↑ ${fmtNum(inp)}${cache}  ↓ ${fmtNum(out)}${reason} tokens${cost ? '  ·  ' + cost : ''}${dur}${ctx}`
  ));

  const qdSpan = document.createElement('span');
  qdSpan.className = 'stats-quota-delta';
  el.appendChild(qdSpan);

  const ctxColspan = hasCost ? '4' : '2';
  const ctxRow = ctxLabel != null
    ? `<tr class="ctx-row"><td colspan="${ctxColspan}">Context lookback: <b>${
        ctxLabel === 'off' ? 'off (no history)'
        : ctxLabel === 'all' ? 'all exchanges'
        : `${ctxLabel} exchange${ctxLabel !== '1' ? 's' : ''}`
      }</b></td></tr>`
    : '';

  let rows, thead, tfoot;
  if (hasCost) {
    const RATES = {
      'Cache read':  [cacheRead,  0.30],
      'Cache write': [cacheWrite, 3.75],
      'Input':       [input,      3.00],
      'Output':      [out,       15.00],
    };
    rows = Object.entries(RATES)
      .filter(([, [n]]) => n > 0)
      .map(([label, [n, rate]]) => {
        const lineCost = (n / 1e6) * rate;
        return `<tr><td>${label}</td><td>${fmtNum(n)}</td><td>$${rate.toFixed(2)}/M</td><td>$${lineCost.toFixed(4)}</td></tr>`;
      }).join('');
    thead = '<tr><th>Type</th><th>Tokens</th><th>Rate</th><th>Cost</th></tr>';
    tfoot = `<tfoot><tr><td colspan="3">Total</td><td>${cost}</td></tr></tfoot>`;
  } else {
    const TOKEN_ROWS = [
      ['Cache read', cacheRead],
      ['Input',      input],
      ['Output',     out],
      ['Reasoning',  reasoning],
    ];
    rows = TOKEN_ROWS
      .filter(([, n]) => n > 0)
      .map(([label, n]) => `<tr><td>${label}</td><td>${fmtNum(n)}</td></tr>`)
      .join('');
    thead = '<tr><th>Type</th><th>Tokens</th></tr>';
    tfoot = '';
  }

  const tooltip = document.createElement('div');
  tooltip.className = 'stats-tooltip';
  tooltip.innerHTML = `<table>
    <thead>${thead}</thead>
    <tbody>${rows}${ctxRow}</tbody>
    ${tfoot}
  </table>`;
  el.appendChild(tooltip);

  bubble.after(el);
  return el;
}

function fmtNum(n) {
  return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
}

function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function addTimestamp(afterEl, isoStr, alignRight = false) {
  if (!isoStr) return null;
  const el = document.createElement('div');
  el.className = 'msg-time' + (alignRight ? ' right' : '');
  el.textContent = fmtTime(isoStr);
  afterEl.after(el);
  return el;
}

function truncate(str, max) {
  if (!str) return '';
  return str.length <= max ? str : str.slice(0, max - 1) + '…';
}

function addMessage(role, content) {
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  if (content) {
    if (role === 'assistant') div.innerHTML = marked.parse(content);
    else div.textContent = content;
  }
  messages.appendChild(div);
  requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
  return div;
}

// ── credentials + quota ───────────────────────────────────────────────────────

const quotaDisplay = document.getElementById('quota-display');
let quotaResetAt = null;
let quotaTimer   = null;
let quotaPct     = null;
let quotaRaw     = null;
let quotaDelta   = null;

async function fetchQuota(trackDelta = false) {
  try {
    const res = await fetch('/quota');
    if (!res.ok) return;
    const data = await res.json();
    const session = data.five_hour;
    if (!session) return;

    const raw = session.utilization ?? 0;
    const pct = Math.round(raw);

    if (trackDelta && quotaPct !== null) {
      const d = pct - quotaPct;
      quotaDelta = d > 0 ? d : null;
    }
    quotaRaw    = raw;
    quotaPct    = pct;
    quotaResetAt = new Date(session.resets_at).getTime();

    const bar = document.getElementById('quota-bar');
    if (bar) {
      bar.style.width = `${pct}%`;
      bar.classList.toggle('warn', pct >= 80);
    }
    quotaDisplay.classList.add('loaded');
    updateQuotaLabel(pct);

    if (quotaTimer) clearInterval(quotaTimer);
    quotaTimer = setInterval(() => updateQuotaLabel(pct), 10000);
  } catch {}
}

function updateQuotaLabel(pct) {
  const label = document.getElementById('quota-label');
  if (!label) return;
  const delta = quotaDelta != null ? ` +${quotaDelta}%` : '';
  if (!quotaResetAt) { label.textContent = `${pct}%${delta}`; return; }
  const diff = quotaResetAt - Date.now();
  if (diff <= 0) { label.textContent = `${pct}%${delta} · resetting`; return; }
  const m = Math.floor(diff / 60000);
  const h = Math.floor(m / 60);
  const timeStr = h > 0 ? `${h}h ${m % 60}m` : `${m}m`;
  label.textContent = `${pct}%${delta} · resets in ${timeStr}`;
}

function initQuota() {
  quotaDisplay.innerHTML = `
    <div id="quota-bar-wrap"><div id="quota-bar"></div></div>
    <span id="quota-label"></span>`;

  const credsPopup = document.getElementById('quota-creds-popup');
  quotaDisplay.addEventListener('click', () => {
    credsPopup.classList.toggle('open');
  });
  document.addEventListener('click', (e) => {
    if (!quotaDisplay.contains(e.target) && !credsPopup.contains(e.target)) {
      credsPopup.classList.remove('open');
    }
  });
  fetchQuota();
}

function initCreds() {
  const orgInput = document.getElementById('creds-org');
  const keyInput = document.getElementById('creds-key');
  const saveBtn  = document.getElementById('creds-save');
  const status   = document.getElementById('creds-status');

  saveBtn.addEventListener('click', async () => {
    const org_id      = orgInput.value.trim();
    const session_key = keyInput.value.trim();
    if (!org_id || !session_key) { status.textContent = 'both fields required'; return; }
    try {
      const res = await fetch('/config/creds', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ org_id, session_key }),
      });
      if (res.ok) {
        status.textContent = 'saved ✓';
        keyInput.value = '';
        fetchQuota();
      } else {
        status.textContent = 'failed';
      }
    } catch { status.textContent = 'error'; }
    setTimeout(() => { status.textContent = ''; }, 3000);
  });
}

// ── usage stats panel ─────────────────────────────────────────────────────────

let statsPeriod = 'daily';
let statsGroup  = 'time';
let liveInterval = null;

function startLivePoll() {
  if (liveInterval) return;
  liveInterval = setInterval(() => {
    if (statsGroup === 'proc' && statsPanel.classList.contains('open')) loadStats();
  }, 2000);
}

function stopLivePoll() {
  if (liveInterval) { clearInterval(liveInterval); liveInterval = null; }
}

async function loadStats() {
  if (statsGroup !== 'proc') {
    statsContent.innerHTML = '<div class="empty">Loading…</div>';
  }
  let rows;
  try {
    const url = statsGroup === 'topic' ? '/stats?group=topic'
              : statsGroup === 'model' ? '/stats?group=model'
              : statsGroup === 'proc'  ? '/processes'
              : `/stats?period=${statsPeriod}`;
    const res = await fetch(url);
    rows = await res.json();
  } catch {
    statsContent.innerHTML = '<div class="empty">Failed to load.</div>';
    return;
  }

  if (statsGroup === 'proc') {
    renderProcStats(rows);
    return;
  }
  if (!rows.length) {
    statsContent.innerHTML = '<div class="empty">No data yet.</div>';
    return;
  }
  if (statsGroup === 'topic') {
    renderTopicStats(rows);
  } else if (statsGroup === 'model') {
    renderModelStats(rows);
  } else {
    renderTimeStats(rows);
  }
}

function renderTimeStats(rows) {
  let totalSessions = 0, totalIn = 0, totalOut = 0, totalCost = 0, totalQuotaDelta = 0;
  const bodyRows = rows.map(r => {
    const inp  = (r.input_tokens || 0) + (r.cache_read_tokens || 0) + (r.cache_write_tokens || 0);
    const out  = r.output_tokens || 0;
    const cost = r.cost_usd || 0;
    const qd   = r.quota_delta;
    totalSessions += r.sessions || 0;
    totalIn  += inp;
    totalOut += out;
    totalCost += cost;
    if (qd != null) totalQuotaDelta += qd;
    return `<tr>
      <td>${r.period || '—'}</td>
      <td>${r.sessions}</td>
      <td>${fmtNum(inp)}</td>
      <td>${fmtNum(out)}</td>
      <td>$${cost.toFixed(4)}</td>
      <td>${qd != null ? '+' + qd.toFixed(1) + '%' : '—'}</td>
    </tr>`;
  }).join('');

  statsContent.innerHTML = `<table>
    <thead><tr>
      <th>${statsPeriod === 'hourly' ? 'Hour' : 'Date'}</th>
      <th>Sessions</th><th>Tokens In</th><th>Tokens Out</th><th>Cost</th><th>Quota Δ</th>
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td>Total</td><td>${totalSessions}</td>
      <td>${fmtNum(totalIn)}</td><td>${fmtNum(totalOut)}</td>
      <td>$${totalCost.toFixed(4)}</td>
      <td>${totalQuotaDelta > 0 ? '+' + totalQuotaDelta.toFixed(1) + '%' : '—'}</td>
    </tr></tfoot>
  </table>`;
}

function renderTopicStats(rows) {
  let totalSessions = 0, totalIn = 0, totalOut = 0, totalCost = 0;
  const bodyRows = rows.map(r => {
    const inp  = r.input_tokens  || 0;
    const out  = r.output_tokens || 0;
    const cost = r.cost_usd      || 0;
    totalSessions += r.sessions || 0;
    totalIn  += inp;
    totalOut += out;
    totalCost += cost;
    return `<tr>
      <td>#${r.topic}</td>
      <td>${r.sessions}</td>
      <td>${fmtNum(inp)}</td>
      <td>${fmtNum(out)}</td>
      <td>$${cost.toFixed(4)}</td>
    </tr>`;
  }).join('');

  statsContent.innerHTML = `<table>
    <thead><tr>
      <th>Topic</th><th>Sessions</th><th>Tokens In</th><th>Tokens Out</th><th>Cost</th>
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td>Total</td><td>${totalSessions}</td>
      <td>${fmtNum(totalIn)}</td><td>${fmtNum(totalOut)}</td>
      <td>$${totalCost.toFixed(4)}</td>
    </tr></tfoot>
  </table>`;
}

function renderModelStats(rows) {
  let totalSessions = 0, totalIn = 0, totalOut = 0, totalCost = 0;
  const bodyRows = rows.map(r => {
    const inp  = (r.input_tokens || 0) + (r.cache_read_tokens || 0) + (r.cache_write_tokens || 0);
    const out  = r.output_tokens || 0;
    const cost = r.cost_usd || 0;
    totalSessions += r.sessions || 0;
    totalIn  += inp;
    totalOut += out;
    totalCost += cost;
    return `<tr>
      <td>${r.model}</td>
      <td>${r.sessions}</td>
      <td>${fmtNum(inp)}</td>
      <td>${fmtNum(out)}</td>
      <td>$${cost.toFixed(4)}</td>
    </tr>`;
  }).join('');

  statsContent.innerHTML = `<table>
    <thead><tr>
      <th>Model</th><th>Sessions</th><th>Tokens In</th><th>Tokens Out</th><th>Cost</th>
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td>Total</td><td>${totalSessions}</td>
      <td>${fmtNum(totalIn)}</td><td>${fmtNum(totalOut)}</td>
      <td>$${totalCost.toFixed(4)}</td>
    </tr></tfoot>
  </table>`;
}

function renderProcStats(rows) {
  if (!rows.length) {
    statsContent.innerHTML = '<div class="empty">No active processes.</div>';
    return;
  }
  const bodyRows = rows.map(r => `
    <tr>
      <td><span class="proc-dot"></span>${r.pid}</td>
      <td>${r.backend || '—'}</td>
      <td>${r.topic ? '#' + r.topic : '—'}</td>
      <td>${r.alias || '—'}</td>
      <td>${r.duration_s}s</td>
      <td>${r.started_iso ? fmtTime(r.started_iso) : '—'}</td>
    </tr>`).join('');
  statsContent.innerHTML = `<table>
    <thead><tr><th>PID</th><th>Backend</th><th>Topic</th><th>Alias</th><th>Duration</th><th>Started</th></tr></thead>
    <tbody>${bodyRows}</tbody>
  </table>`;
}

function initStats() {
  statsBtn.addEventListener('click', () => {
    const open = statsPanel.classList.toggle('open');
    statsBtn.classList.toggle('active', open);
    if (open) {
      statsPanel.style.top = (document.getElementById('topbar').offsetHeight + 4) + 'px';
      loadStats();
      if (statsGroup === 'proc') startLivePoll();
    } else {
      stopLivePoll();
    }
  });

  document.getElementById('stats-close').addEventListener('click', () => {
    statsPanel.classList.remove('open');
    statsBtn.classList.remove('active');
    stopLivePoll();
  });

  document.querySelectorAll('.st').forEach(btn => {
    btn.addEventListener('click', () => {
      statsPeriod = btn.dataset.period;
      statsGroup  = btn.dataset.group;
      document.querySelectorAll('.st').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      loadStats();
      if (statsGroup === 'proc') startLivePoll();
      else stopLivePoll();
    });
  });
}

// ── alias manager ─────────────────────────────────────────────────────────────

async function loadAliases() {
  const listEl = document.getElementById('aliases-list');
  listEl.innerHTML = '<div class="empty">Loading…</div>';
  let aliases;
  try {
    const res = await fetch('/config/aliases');
    aliases = await res.json();
  } catch {
    listEl.innerHTML = '<div class="empty">Failed to load.</div>';
    return;
  }
  if (!aliases.length) {
    listEl.innerHTML = '<div class="empty">No aliases yet. Add one below.</div>';
    return;
  }
  const rows = aliases.map(a => `
    <tr>
      <td>${a.name}</td>
      <td>${a.backend}</td>
      <td>${a.model || '—'}</td>
      <td>${a.cwd || '—'}</td>
      <td>${a.timeout ? a.timeout + 's' : '—'}</td>
      <td>
        <button class="edit-btn" data-name="${a.name}" data-backend="${a.backend}" data-model="${a.model || ''}" data-cwd="${a.cwd || ''}" data-timeout="${a.timeout || ''}">✎</button>
        <button class="del-btn" data-name="${a.name}">✕</button>
      </td>
    </tr>`).join('');
  listEl.innerHTML = `<table>
    <thead><tr><th>Name</th><th>Backend</th><th>Model</th><th>CWD</th><th>Timeout</th><th></th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;

  listEl.querySelectorAll('.edit-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.getElementById('af-name').value    = btn.dataset.name;
      document.getElementById('af-backend').value = btn.dataset.backend;
      document.getElementById('af-model').value   = btn.dataset.model;
      document.getElementById('af-cwd').value     = btn.dataset.cwd;
      document.getElementById('af-timeout').value = btn.dataset.timeout;
      document.getElementById('af-name').focus();
    });
  });

  listEl.querySelectorAll('.del-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      await fetch(`/config/aliases/${btn.dataset.name}`, { method: 'DELETE' });
      loadAliases();
    });
  });
}

function initAliases() {
  const statusEl = document.getElementById('alias-form-status');
  document.getElementById('alias-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const rawTimeout = parseInt(document.getElementById('af-timeout').value, 10);
    const body = {
      name:    document.getElementById('af-name').value.trim(),
      backend: document.getElementById('af-backend').value,
      model:   document.getElementById('af-model').value.trim() || null,
      cwd:     document.getElementById('af-cwd').value.trim()   || null,
      timeout: Number.isFinite(rawTimeout) && rawTimeout > 0 ? rawTimeout : null,
    };
    if (!body.name) return;
    try {
      const res = await fetch('/config/aliases', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        statusEl.textContent = 'saved ✓';
        document.getElementById('af-name').value    = '';
        document.getElementById('af-model').value   = '';
        document.getElementById('af-cwd').value     = '';
        document.getElementById('af-timeout').value = '';
        loadAliases();
      } else {
        statusEl.textContent = 'failed';
      }
    } catch { statusEl.textContent = 'error'; }
    setTimeout(() => { statusEl.textContent = ''; }, 3000);
  });
}

// ── init ─────────────────────────────────────────────────────────────────────

initSettings();
initHistoryScroll();
initStats();
initAliases();
initQuota();
initCreds();
