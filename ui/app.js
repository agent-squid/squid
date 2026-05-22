const messages    = document.getElementById('messages');
const form        = document.getElementById('form');
const input       = document.getElementById('input');
const scrollBtn   = document.getElementById('scroll-btn');
const settingsBtn = document.getElementById('settings-btn');
const settingsBar = document.getElementById('settings-bar');
const statsBtn    = document.getElementById('stats-btn');
const statsPanel  = document.getElementById('stats-panel');
const statsContent= document.getElementById('stats-content');
const aliasesBtn  = document.getElementById('aliases-btn');
const aliasesPanel= document.getElementById('aliases-panel');

messages.addEventListener('scroll', () => {
  scrollBtn.classList.toggle('visible', !isAtBottom());
});
scrollBtn.addEventListener('click', () => {
  messages.scrollTop = messages.scrollHeight;
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
    });
  });
}

settingsBtn.addEventListener('click', () => {
  settingsBar.classList.toggle('open');
  settingsBtn.classList.toggle('active');
});

// ── prompt parsing ────────────────────────────────────────────────────────────

function parseInput(text) {
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
    if (!item.prompt && !item.response) continue;

    const group = document.createElement('div');
    group.className = 'history-group';

    const header = document.createElement('div');
    header.className = 'history-header';
    const label = truncate(item.prompt, 60);
    const ts = item.timestamp ? fmtDate(item.timestamp) : '';
    header.textContent = ts ? `${ts}  ·  ${label}` : label;
    group.appendChild(header);

    const userBubble = makeUserBubble(item.prompt || '', item.topic, item.alias);
    group.appendChild(userBubble);

    const asstBubble = document.createElement('div');
    asstBubble.className = 'msg assistant';
    asstBubble.innerHTML = marked.parse(item.response || '');
    group.appendChild(asstBubble);

    if (item.stats) addStats(asstBubble, item.stats);

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

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
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
  const { topic, alias, message } = parseInput(text);

  // User bubble with optional topic badge
  messages.appendChild(makeUserBubble(message, topic, alias));
  requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });

  const bubble   = addMessage('assistant', '');
  const loader   = addLoader(bubble);
  const quotaBefore = quotaRaw;
  let lastSessionId = null;
  let statsEl   = null;
  let statusEl  = null;
  let statusBuf = '';
  let queueEl   = null;

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

        if (eventName === 'queued') {
          try {
            const info = JSON.parse(data);
            loader.remove();
            if (!queueEl) {
              queueEl = document.createElement('div');
              queueEl.className = 'queue-msg';
              bubble.after(queueEl);
            }
            queueEl.textContent = `#${info.topic} · queued — position ${info.position}`;
          } catch {}
          eventName = null;

        } else if (eventName === 'stats') {
          try {
            const stats = JSON.parse(data);
            lastSessionId = stats.session_id ?? null;
            if (queueEl) { queueEl.remove(); queueEl = null; }
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
          if (queueEl)  { queueEl.remove();  queueEl  = null; }
          loader.remove();
          eventName = null;

        } else {
          if (statusEl) { statusEl.remove(); statusEl = null; }
          if (queueEl)  { queueEl.remove();  queueEl  = null; }
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

function makeUserBubble(text, topic, alias) {
  const div = document.createElement('div');
  div.className = 'msg user';
  if (topic && topic !== 'default') {
    const badge = document.createElement('div');
    badge.className = 'topic-badge';
    badge.textContent = alias ? `#${topic}@${alias}` : `#${topic}`;
    div.appendChild(badge);
  }
  const content = document.createElement('div');
  content.textContent = text;
  div.appendChild(content);
  return div;
}

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
  const ctx        = ctxLabel != null ? `  · ctx:${ctxLabel}` : '';

  el.appendChild(document.createTextNode(
    `↑ ${fmtNum(inp)}${cache}  ↓ ${fmtNum(out)} tokens${cost ? '  ·  ' + cost : ''}${dur}${ctx}`
  ));

  const qdSpan = document.createElement('span');
  qdSpan.className = 'stats-quota-delta';
  el.appendChild(qdSpan);

  const RATES = {
    'Cache read':  [cacheRead,  0.30],
    'Cache write': [cacheWrite, 3.75],
    'Input':       [input,      3.00],
    'Output':      [out,       15.00],
  };
  const rows = Object.entries(RATES)
    .filter(([, [n]]) => n > 0)
    .map(([label, [n, rate]]) => {
      const lineCost = (n / 1e6) * rate;
      return `<tr><td>${label}</td><td>${fmtNum(n)}</td><td>$${rate.toFixed(2)}/M</td><td>$${lineCost.toFixed(4)}</td></tr>`;
    }).join('');

  const ctxRow = ctxLabel != null
    ? `<tr class="ctx-row"><td colspan="4">Context lookback: <b>${
        ctxLabel === 'off' ? 'off (no history)'
        : ctxLabel === 'all' ? 'all exchanges'
        : `${ctxLabel} exchange${ctxLabel !== '1' ? 's' : ''}`
      }</b></td></tr>`
    : '';

  const tooltip = document.createElement('div');
  tooltip.className = 'stats-tooltip';
  tooltip.innerHTML = `<table>
    <thead><tr><th>Type</th><th>Tokens</th><th>Rate</th><th>Cost</th></tr></thead>
    <tbody>${rows}${ctxRow}</tbody>
    ${cost ? `<tfoot><tr><td colspan="3">Total</td><td>${cost}</td></tr></tfoot>` : ''}
  </table>`;
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

async function loadStats() {
  statsContent.innerHTML = '<div class="empty">Loading…</div>';
  let rows;
  try {
    const url = statsGroup === 'topic'
      ? '/stats?group=topic'
      : `/stats?period=${statsPeriod}`;
    const res = await fetch(url);
    rows = await res.json();
  } catch {
    statsContent.innerHTML = '<div class="empty">Failed to load.</div>';
    return;
  }

  if (!rows.length) {
    statsContent.innerHTML = '<div class="empty">No data yet.</div>';
    return;
  }

  if (statsGroup === 'topic') {
    renderTopicStats(rows);
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

function initStats() {
  statsBtn.addEventListener('click', () => {
    const open = statsPanel.classList.toggle('open');
    statsBtn.classList.toggle('active', open);
    if (aliasesPanel.classList.contains('open')) {
      aliasesPanel.classList.remove('open');
      aliasesBtn.classList.remove('active');
    }
    if (open) {
      statsPanel.style.top = (document.getElementById('topbar').offsetHeight + 4) + 'px';
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
      statsGroup  = btn.dataset.group;
      document.querySelectorAll('.st').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      loadStats();
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
      <td><button class="del-btn" data-name="${a.name}">✕</button></td>
    </tr>`).join('');
  listEl.innerHTML = `<table>
    <thead><tr><th>Name</th><th>Backend</th><th>Model</th><th>CWD</th><th></th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;

  listEl.querySelectorAll('.del-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      await fetch(`/config/aliases/${btn.dataset.name}`, { method: 'DELETE' });
      loadAliases();
    });
  });
}

function initAliases() {
  aliasesBtn.addEventListener('click', () => {
    const open = aliasesPanel.classList.toggle('open');
    aliasesBtn.classList.toggle('active', open);
    if (statsPanel.classList.contains('open')) {
      statsPanel.classList.remove('open');
      statsBtn.classList.remove('active');
    }
    if (open) {
      aliasesPanel.style.top = (document.getElementById('topbar').offsetHeight + 4) + 'px';
      loadAliases();
    }
  });

  document.getElementById('aliases-close').addEventListener('click', () => {
    aliasesPanel.classList.remove('open');
    aliasesBtn.classList.remove('active');
  });

  const statusEl = document.getElementById('alias-form-status');
  document.getElementById('alias-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      name:    document.getElementById('af-name').value.trim(),
      backend: document.getElementById('af-backend').value,
      model:   document.getElementById('af-model').value.trim() || null,
      cwd:     document.getElementById('af-cwd').value.trim()   || null,
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
        document.getElementById('af-name').value  = '';
        document.getElementById('af-model').value = '';
        document.getElementById('af-cwd').value   = '';
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
