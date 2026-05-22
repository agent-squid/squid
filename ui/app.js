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

// ── settings ─────────────────────────────────────────────────────────────────

let lookback = localStorage.getItem('lookback') === 'all' ? 'all'
             : localStorage.getItem('lookback') != null ? (parseInt(localStorage.getItem('lookback'), 10) || 0)
             : 5;

function initSettings() {
  document.querySelectorAll('.lb').forEach(btn => {
    const val = btn.dataset.val === 'all' ? 'all' : parseInt(btn.dataset.val);
    if (val === lookback) btn.classList.add('active');
    else btn.classList.remove('active');

    btn.addEventListener('click', () => {
      lookback = val;
      localStorage.setItem('lookback', val);
      document.querySelectorAll('.lb').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      refreshFileHistory();
    });
  });
}

settingsBtn.addEventListener('click', () => {
  settingsBar.classList.toggle('open');
  settingsBtn.classList.toggle('active');
});

// ── conversation history ──────────────────────────────────────────────────────

const sessionHistory = []; // exchanges from current page session
let fileHistory = [];      // exchanges loaded from /history files

function buildContextHistory() {
  if (lookback === 0) return [];
  const combined = [...fileHistory, ...sessionHistory];
  if (lookback === 'all') return combined;
  return combined.slice(-(lookback * 2));
}

async function refreshFileHistory() {
  if (lookback === 0) { fileHistory = []; return; }

  // Skip the most recent N exchanges already captured in sessionHistory
  const skipExchanges = Math.floor(sessionHistory.length / 2);
  const wantExchanges = lookback === 'all' ? Infinity : lookback;

  const items = [];
  let offset = skipExchanges;
  const batch = 20;

  while (items.length < wantExchanges) {
    const limit = Math.min(batch, wantExchanges === Infinity ? batch : wantExchanges - items.length);
    let data;
    try {
      const res = await fetch(`/history?offset=${offset}&limit=${limit}`);
      data = await res.json();
    } catch { break; }
    items.push(...data.items);
    if (!data.has_more) break;
    offset += data.items.length;
  }

  // API returns newest-first; reverse to chronological for context
  fileHistory = [...items].reverse().flatMap(item => [
    { role: 'user',      content: item.prompt   || '' },
    { role: 'assistant', content: item.response || '' },
  ]).filter(m => m.content);
}

marked.setOptions({ breaks: true });

// ── history pagination ────────────────────────────────────────────────────────

let historyOffset = 0;
let historyExhausted = false;
let historyLoading = false;
let topSentinel = null;

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

  for (const item of [...items].reverse()) {
    const group = document.createElement('div');
    group.className = 'history-group';

    const header = document.createElement('div');
    header.className = 'history-header';
    const label = item.title || truncate(item.prompt, 60);
    const ts = item.timestamp ? fmtDate(item.timestamp) : '';
    header.textContent = ts ? `${ts}  ·  ${label}` : label;
    group.appendChild(header);

    const userBubble = document.createElement('div');
    userBubble.className = 'msg user';
    userBubble.textContent = item.prompt;
    group.appendChild(userBubble);

    const asstBubble = document.createElement('div');
    asstBubble.className = 'msg assistant';
    asstBubble.innerHTML = marked.parse(item.response || '');
    group.appendChild(asstBubble);

    if (item.stats) addStats(asstBubble, item.stats);

    fragment.appendChild(group);
  }

  // Insert after sentinel (keeps sentinel pinned at top)
  const anchor = topSentinel ? topSentinel.nextSibling : messages.firstChild;
  messages.insertBefore(fragment, anchor);

  // Restore scroll so existing content doesn't jump
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

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  addMessage('user', text);
  input.value = '';
  sendMessage(text);
});

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

async function sendMessage(text) {
  const bubble = addMessage('assistant', '');
  const loader = addLoader(bubble);
  const quotaBefore = quotaRaw;  // snapshot before the exchange
  let lastSessionId = null;
  let statsEl = null;
  let statusEl = null;
  let statusBuf = '';

  const ctxHistory = buildContextHistory();
  const ctxLabel = lookback === 0 ? 'off' : lookback === 'all' ? 'all' : `${ctxHistory.length / 2}`;

  const res = await fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text, backend: 'auto', history: ctxHistory }),
  });

  const reader = res.body.getReader();
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

        if (eventName === 'stats') {
          try {
            const stats = JSON.parse(data);
            lastSessionId = stats.session_id ?? null;
            loader.remove();
            statsEl = addStats(bubble, stats, ctxLabel);
          } catch {}
          eventName = null;
        } else if (eventName === 'status') {
          statusBuf += data;
          if (!statusEl) {
            statusEl = document.createElement('div');
            statusEl.className = 'status-msg';
            bubble.after(statusEl);
          }
          const trimmed = statusBuf.replace(/\s+/g, ' ').trim();
          statusEl.textContent = trimmed.length > 120 ? '…' + trimmed.slice(-117) : trimmed;
          scrollToBottom();
        } else if (eventName === 'done' || eventName === 'error') {
          if (statusEl) { statusEl.remove(); statusEl = null; }
          loader.remove();
          eventName = null;
        } else {
          if (statusEl) { statusEl.remove(); statusEl = null; }
          if (loader.parentNode === bubble) bubble.after(loader);
          if (dataLineCount > 1) raw += '\n';
          raw += data;
          bubble.innerHTML = marked.parse(raw);
          scrollToBottom();
        }
      } else if (line === '') {
        eventName = null;
        dataLineCount = 0;
      }
    }
  }

  // Record this exchange in session history
  if (raw) {
    sessionHistory.push({ role: 'user',      content: text });
    sessionHistory.push({ role: 'assistant', content: raw  });
  }

  // Refresh quota, then persist and display the before/after delta
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

function isAtBottom() {
  return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
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

function addStats(bubble, stats, ctxLabel) {
  const el = document.createElement('div');
  el.className = 'stats';

  const cacheRead  = stats.cache_read_tokens  || 0;
  const cacheWrite = stats.cache_write_tokens || 0;
  const input      = stats.input_tokens       || 0;
  const out        = stats.output_tokens      || 0;
  const inp        = input + cacheRead + cacheWrite;
  const cost       = stats.cost_usd != null ? `$${stats.cost_usd.toFixed(4)}` : '';
  const cache      = cacheRead ? ` · ${fmtNum(cacheRead)} cached` : '';
  const dur        = stats.duration_ms ? ` · ${(stats.duration_ms / 1000).toFixed(1)}s` : '';
  const ctx = ctxLabel != null ? `  · ctx:${ctxLabel}` : '';
  el.appendChild(document.createTextNode(
    `↑ ${fmtNum(inp)}${cache}  ↓ ${fmtNum(out)} tokens${cost ? '  ·  ' + cost : ''}${dur}${ctx}`
  ));
  const qdSpan = document.createElement('span');
  qdSpan.className = 'stats-quota-delta';
  el.appendChild(qdSpan);

  // Tooltip breakdown
  const RATES = { 'Cache read': [cacheRead, 0.30], 'Cache write': [cacheWrite, 3.75], 'Input': [input, 3.00], 'Output': [out, 15.00] };
  const rows = Object.entries(RATES)
    .filter(([, [n]]) => n > 0)
    .map(([label, [n, rate]]) => {
      const lineCost = (n / 1e6) * rate;
      return `<tr><td>${label}</td><td>${fmtNum(n)}</td><td>$${rate.toFixed(2)}/M</td><td>$${lineCost.toFixed(4)}</td></tr>`;
    }).join('');

  const ctxRow = ctxLabel != null
    ? `<tr class="ctx-row"><td colspan="4">Context lookback: <b>${ctxLabel === 'off' ? 'off (no history)' : ctxLabel === 'all' ? 'all exchanges' : `${ctxLabel} exchange${ctxLabel !== '1' ? 's' : ''}`}</b></td></tr>`
    : '';

  const tooltip = document.createElement('div');
  tooltip.className = 'stats-tooltip';
  tooltip.innerHTML = `<table><thead><tr><th>Type</th><th>Tokens</th><th>Rate</th><th>Cost</th></tr></thead><tbody>${rows}${ctxRow}</tbody>${cost ? `<tfoot><tr><td colspan="3">Total</td><td>${cost}</td></tr></tfoot>` : ''}</table>`;
  el.appendChild(tooltip);

  bubble.after(el);
  return el;
}

function fmtNum(n) {
  return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
}

function fmtDate(iso) {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
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
let quotaTimer = null;
let quotaPct = null;
let quotaRaw = null;   // raw float from API for DB storage
let quotaDelta = null;

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
    quotaRaw = raw;
    quotaPct = pct;
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
  const orgInput  = document.getElementById('creds-org');
  const keyInput  = document.getElementById('creds-key');
  const saveBtn   = document.getElementById('creds-save');
  const status    = document.getElementById('creds-status');

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

async function loadStats() {
  statsContent.innerHTML = '<div class="empty">Loading…</div>';
  let rows;
  try {
    const res = await fetch(`/stats?period=${statsPeriod}`);
    rows = await res.json();
  } catch {
    statsContent.innerHTML = '<div class="empty">Failed to load.</div>';
    return;
  }

  if (!rows.length) {
    statsContent.innerHTML = '<div class="empty">No data yet.</div>';
    return;
  }

  let totalSessions = 0, totalIn = 0, totalOut = 0, totalCost = 0;

  let totalQuotaDelta = 0;
  const bodyRows = rows.map(r => {
    const inp = (r.input_tokens || 0) + (r.cache_read_tokens || 0) + (r.cache_write_tokens || 0);
    const out = r.output_tokens || 0;
    const cost = r.cost_usd || 0;
    const qd = r.quota_delta;
    totalSessions += r.sessions || 0;
    totalIn += inp;
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
      <th>Sessions</th>
      <th>Tokens In</th>
      <th>Tokens Out</th>
      <th>Cost</th>
      <th>Quota Δ</th>
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td>Total</td>
      <td>${totalSessions}</td>
      <td>${fmtNum(totalIn)}</td>
      <td>${fmtNum(totalOut)}</td>
      <td>$${totalCost.toFixed(4)}</td>
      <td>${totalQuotaDelta > 0 ? '+' + totalQuotaDelta.toFixed(1) + '%' : '—'}</td>
    </tr></tfoot>
  </table>`;
}

function initStats() {
  statsBtn.addEventListener('click', () => {
    const open = statsPanel.classList.toggle('open');
    statsBtn.classList.toggle('active', open);
    if (open) {
      const topbar = document.getElementById('topbar');
      statsPanel.style.top = (topbar.offsetHeight + 4) + 'px';
      loadStats();
    }
  });

  document.getElementById('stats-close').addEventListener('click', () => {
    statsPanel.classList.remove('open');
    statsBtn.classList.remove('active');
  });

  document.querySelectorAll('.st').forEach(btn => {
    btn.addEventListener('click', () => {
      statsPeriod = btn.dataset.period;
      document.querySelectorAll('.st').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      loadStats();
    });
  });
}

// ── init ─────────────────────────────────────────────────────────────────────

initSettings();
initHistoryScroll();
initStats();
initQuota();
initCreds();
refreshFileHistory();
