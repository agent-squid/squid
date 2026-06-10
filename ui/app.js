// ── bearer token auth ────────────────────────────────────────────────────────
// First visit: open http://<host>:<port>/?token=<value> — stored to localStorage,
// then stripped from the URL. All subsequent relative fetch() calls send it
// automatically via the interceptor below.
(function () {
  const param = new URLSearchParams(window.location.search).get('token');
  if (param) {
    localStorage.setItem('squid_token', param);
    window.history.replaceState({}, '', window.location.pathname);
  }
  const token = localStorage.getItem('squid_token');

  function showAuthBanner() {
    if (document.getElementById('auth-banner')) return;
    const el = document.createElement('div');
    el.id = 'auth-banner';
    el.innerHTML = `
      <div id="auth-banner-box">
        <div id="auth-banner-title">Authentication required</div>
        <div id="auth-banner-body">
          Open this page with your token to sign in:<br>
          <code>${location.origin}/?token=<em>your-token</em></code><br><br>
          Your token is the <code>server.token</code> value in <code>squid.yaml</code>.
        </div>
      </div>`;
    document.body.appendChild(el);
  }

  const _orig = window.fetch.bind(window);
  window.fetch = (url, opts = {}) => {
    if (typeof url === 'string' && !url.startsWith('http')) {
      if (token) opts = { ...opts, headers: { 'Authorization': `Bearer ${token}`, ...(opts.headers || {}) } };
    }
    return _orig(url, opts).then(res => {
      if (res.status === 401) showAuthBanner();
      return res;
    });
  };
})();

const messages     = document.getElementById('messages');
const form         = document.getElementById('form');
const input        = document.getElementById('input');
const scrollBtn    = document.getElementById('scroll-btn');
const statsContent = document.getElementById('stats-content');
const helpBtn      = document.getElementById('help-btn');
const helpPanel    = document.getElementById('help-panel');
const acEl         = document.getElementById('autocomplete');
const pinBtn       = document.getElementById('pin-btn');
const pinPanel     = document.getElementById('pin-panel');
const pinCountEl   = document.getElementById('pin-count');

window.scrollTo(0, 0);

// Android PWA: dvh can be wrong after location.reload(); override with actual visual height
(function() {
  function setVh() {
    const h = (window.visualViewport || window).height;
    document.body.style.height = h + 'px';
  }
  setVh();
  (window.visualViewport || window).addEventListener('resize', setVh);
}());

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}

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

// Rewrite file:// links and images to /localfile?path= so local paths are served.
(function () {
  function stripLineSuffix(path) {
    return path.replace(/:\d+(?::\d+)?$/, '');
  }

  function isLocalFilePath(path) {
    return /^(\/|~\/)/.test(path) && /\.\w{1,16}$/.test(path);
  }

  function localFileUrl(path) {
    const params = new URLSearchParams({ path });
    const token = localStorage.getItem('squid_token');
    if (token) params.set('token', token);
    return '/localfile?' + params.toString();
  }

  function fileToLocal(url) {
    if (!url) return url;
    if (url.startsWith('file://')) {
      const p = stripLineSuffix(decodeURIComponent(url.replace(/^file:\/\//, '')));
      return localFileUrl(p);
    }
    // bare absolute paths like /Users/... or ~/..., optionally with :line suffix
    const p = stripLineSuffix(url);
    if (isLocalFilePath(p)) {
      return localFileUrl(p);
    }
    return url;
  }
  // marked v5+ passes a token object; override href while forwarding everything else.
  marked.use({
    renderer: {
      link({ href, title, tokens }) {
        return marked.Renderer.prototype.link.call(this, { href: fileToLocal(href), title, tokens });
      },
      image({ href, title, text }) {
        return marked.Renderer.prototype.image.call(this, { href: fileToLocal(href), title, text });
      },
    },
  });
})();

// ── navigation ────────────────────────────────────────────────────────────────

let currentView = 'chat';

function switchView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  document.querySelectorAll('.nav-tab, .hmenu-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === name);
  });
  currentView = name;
  if (name === 'analytics') loadStats();
  if (name === 'agents') loadAgents();
}

function initSettings() {
  document.querySelectorAll('.nav-tab').forEach(btn =>
    btn.addEventListener('click', () => {
      hamburgerMenu.classList.remove('open');
      hamburgerBtn.classList.remove('active');
      switchView(btn.dataset.view);
    })
  );
  const hamburgerBtn  = document.getElementById('hamburger-btn');
  const hamburgerMenu = document.getElementById('hamburger-menu');
  hamburgerBtn.addEventListener('click', e => {
    e.stopPropagation();
    const open = hamburgerMenu.classList.toggle('open');
    hamburgerBtn.classList.toggle('active', open);
  });
  document.querySelectorAll('.hmenu-item').forEach(btn =>
    btn.addEventListener('click', () => {
      hamburgerMenu.classList.remove('open');
      hamburgerBtn.classList.remove('active');
      switchView(btn.dataset.view);
    })
  );
  document.addEventListener('click', e => {
    if (!hamburgerMenu.contains(e.target) && e.target !== hamburgerBtn) {
      hamburgerMenu.classList.remove('open');
      hamburgerBtn.classList.remove('active');
    }
  });
}

async function openRemoteQR() {
  if (document.getElementById('remote-modal')) return;

  const token = localStorage.getItem('squid_token') || '';
  let remoteUrl = null, remoteReason = 'error';
  try {
    const res = await fetch('/remote');
    const data = await res.json();
    remoteUrl = data.url || null;
    remoteReason = data.reason || null;
  } catch {}

  const authUrl = remoteUrl
    ? (token ? `${remoteUrl}?token=${token}` : remoteUrl)
    : null;

  const modal = document.createElement('div');
  modal.id = 'remote-modal';

  const box = document.createElement('div');
  box.id = 'remote-modal-box';

  const title = document.createElement('div');
  title.id = 'remote-modal-title';
  title.textContent = 'Remote Access';

  const qrDiv = document.createElement('div');
  qrDiv.id = 'remote-qr';

  const urlEl = document.createElement('div');
  urlEl.id = 'remote-url';

  const closeBtn = document.createElement('button');
  closeBtn.id = 'remote-modal-close';
  closeBtn.textContent = '✕';
  closeBtn.addEventListener('click', () => modal.remove());
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { modal.remove(); document.removeEventListener('keydown', esc); }
  });

  if (authUrl) {
    urlEl.textContent = authUrl;
    box.appendChild(closeBtn);
    box.appendChild(title);
    box.appendChild(qrDiv);
    box.appendChild(urlEl);
    modal.appendChild(box);
    document.body.appendChild(modal);
    // Render QR after insertion so the div has dimensions
    new QRCode(qrDiv, { text: authUrl, width: 220, height: 220,
                         colorDark: '#0f0f13', colorLight: '#f5f0e8' });
  } else {
    const reason = remoteReason;
    const msgs = {
      not_installed: 'Tailscale is not installed.\nInstall from tailscale.com, then restart squid — bin/start.sh will configure remote access automatically.',
      not_running:   'Tailscale is installed but not running.\nStart the Tailscale app, then run bin/start.sh again.',
      no_dns:        'Tailscale is running but has no DNS name.\nEnable MagicDNS in your Tailscale admin console (tailscale.com/kb/1081).',
      error:         'Could not reach Tailscale. Check that the Tailscale app is running.',
    };
    urlEl.style.whiteSpace = 'pre-line';
    urlEl.textContent = msgs[reason] || 'Tailscale unavailable — remote access requires Tailscale (tailscale.com).';
    box.appendChild(closeBtn);
    box.appendChild(title);
    box.appendChild(urlEl);
    modal.appendChild(box);
    document.body.appendChild(modal);
  }
}

function openHelp() {
  helpPanel.classList.add('open');
  helpBtn.classList.add('active');
  // position below topbar (same pattern as stats panel)
  helpPanel.style.top = (document.getElementById('topbar').offsetHeight + 4) + 'px';
}

function closeHelp() {
  helpPanel.classList.remove('open');
  helpBtn.classList.remove('active');
}

helpBtn.addEventListener('click', () => {
  if (helpPanel.classList.contains('open')) { closeHelp(); } else { openHelp(); }
});
document.getElementById('help-close').addEventListener('click', closeHelp);

// ── prompt parsing ────────────────────────────────────────────────────────────

// ── per-topic session tracking ────────────────────────────────────────────────
const _sessionIds = {}; // `${topic}@${agent|_}` → most recent session_id

// ── topic chip ────────────────────────────────────────────────────────────────

const topicChipEl = document.getElementById('topic-chip');
let stickyChip = null; // { topic, agent, adhoc } | null

function setTopicChip(topic, agent, adhoc = false, lookback = 0) {
  stickyChip = { topic, agent, adhoc, lookback };

  topicChipEl.innerHTML = '';
  const tSpan = document.createElement('span');
  tSpan.className = 'chip-topic';
  tSpan.textContent = '#' + topic;
  topicChipEl.appendChild(tSpan);
  if (agent) {
    const aSpan = document.createElement('span');
    aSpan.className = 'chip-agent';
    aSpan.textContent = '@' + agent;
    topicChipEl.appendChild(aSpan);
  }
  if (adhoc) {
    const adSpan = document.createElement('span');
    adSpan.className = 'chip-adhoc';
    adSpan.textContent = lookback > 0 ? `!${lookback}` : '!';
    topicChipEl.appendChild(adSpan);
  }
  topicChipEl.classList.add('visible');
  topicChipEl.classList.remove('needs-agent');
  input.placeholder = 'message…';
}

function clearTopicChip() {
  stickyChip = null;
  topicChipEl.classList.remove('visible', 'needs-agent');
  input.placeholder = '#topic or #topic@agent message…';
  document.querySelectorAll('.history-item.ctx-highlight').forEach(el => el.classList.remove('ctx-highlight'));
}

topicChipEl.addEventListener('click', () => {
  const hadFilter = historyFilter.topic || historyFilter.agent;
  clearTopicChip();
  if (hadFilter) reloadHistory({});
  input.focus();
});

function parseInput(text) {
  if (stickyChip && !text.startsWith('#')) {
    const adhoc = !!stickyChip.adhoc;
    return { topic: stickyChip.topic, agent: stickyChip.agent, adhoc, lookback: stickyChip.lookback || 0, message: text.trim() || text };
  }
  // adhoc: #topic!N or #topic@agent!N (N optional, defaults to 0 = no lookback)
  const ma = text.match(/^#(\w+)(?:@(\w+))?!(\d*)\s+([\s\S]*)$/);
  if (ma && ma[4].trim()) {
    return { topic: ma[1], agent: ma[2] || null, adhoc: true, lookback: ma[3] ? parseInt(ma[3]) : 0, message: ma[4].trim() };
  }
  // session: #topic or #topic@agent
  const ms = text.match(/^#(\w+)(?:@(\w+))?\s+([\s\S]*)$/);
  if (ms && ms[3].trim()) {
    return { topic: ms[1], agent: ms[2] || null, adhoc: false, lookback: 0, message: ms[3].trim() };
  }
  return { topic: 'default', agent: null, adhoc: false, lookback: 0, message: text };
}

// ── topic tag helper (colored, clickable) ──────────────────────────────────────

function makeTopicTag(topic, agent, { clickable = false, adhoc = false, lookback = 0 } = {}) {
  const wrap = document.createElement('span');
  wrap.className = 'topic-tag';

  const tSpan = document.createElement('span');
  tSpan.className = 'tag-topic' + (clickable ? ' clickable' : '');
  tSpan.textContent = '#' + topic;
  wrap.appendChild(tSpan);

  if (agent) {
    const aSpan = document.createElement('span');
    aSpan.className = 'tag-agent' + (clickable ? ' clickable' : '');
    aSpan.textContent = '@' + agent;
    wrap.appendChild(aSpan);
  }

  if (adhoc) {
    const adSpan = document.createElement('span');
    adSpan.className = 'tag-adhoc';
    adSpan.textContent = '!' + (lookback > 0 ? lookback : '');
    wrap.appendChild(adSpan);
  }

  if (clickable) {
    wrap.addEventListener('click', (e) => {
      e.stopPropagation();
      if (agent && e.target.classList.contains('tag-agent')) filterByAgent(topic, agent, adhoc, lookback);
      else filterByTopic(topic);
    });
  }

  return wrap;
}

// ── history filter ─────────────────────────────────────────────────────────────

let historyFilter = { topic: null, agent: null, adhoc: null };

function filterByTopic(topic) {
  setTopicChip(topic, null);
  reloadHistory({ topic, agent: null, adhoc: null });
}

function filterByAgent(topic, agent, adhoc = false, lookback = 0) {
  setTopicChip(topic, agent, adhoc, lookback);
  reloadHistory({ topic, agent, adhoc });
}

function clearFilter() {
  reloadHistory({});
}

function reloadHistory(filter = {}) {
  historyFilter = filter;
  historyOffset = 0;
  historyExhausted = false;
  historyLoading = false;
  if (topSentinel) { topSentinel.remove(); topSentinel = null; }
  document.querySelectorAll('.history-item, .boot-banner').forEach(el => el.remove());
  // Remove live (non-history) messages too — completed ones are in the DB and will reload
  document.querySelectorAll('#messages > .msg:not(.msg-thinking), #messages > .msg-thinking-done, #messages > .msg-time, #messages > .stats').forEach(el => el.remove());
  _updateFilterBadge();
  initHistoryScroll();
}

function _updateFilterBadge() {
  const badge = document.getElementById('filter-badge');
  const labelEl = document.getElementById('filter-badge-label');
  const { topic, agent, adhoc } = historyFilter;

  if (!topic && !agent) {
    badge.classList.remove('active');
    return;
  }

  labelEl.innerHTML = '';
  if (topic) {
    const t = document.createElement('span');
    t.className = 'tag-topic';
    t.textContent = '#' + topic;
    labelEl.appendChild(t);
  }
  if (agent) {
    const a = document.createElement('span');
    a.className = 'tag-agent';
    a.textContent = '@' + agent;
    labelEl.appendChild(a);
  }
  if (adhoc) { // only show '!' for adhoc — 'sess' was removed because it concatenated visually with the agent name
    const ad = document.createElement('span');
    ad.className = 'tag-adhoc';
    ad.textContent = '!';
    labelEl.appendChild(ad);
  }
  badge.classList.add('active');
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
    let url = `/history?offset=${historyOffset}&limit=5`;
    if (historyFilter.topic) url += `&topic=${encodeURIComponent(historyFilter.topic)}`;
    if (historyFilter.agent) url += `&agent=${encodeURIComponent(historyFilter.agent)}`;
    if (historyFilter.adhoc != null) url += `&adhoc=${historyFilter.adhoc}`;
    const res = await fetch(url);
    data = await res.json();
  } catch {
    historyLoading = false;
    return;
  }

  const { items, has_more } = data;
  const prevHeight = messages.scrollHeight;
  const fragment = document.createDocumentFragment();

  for (const item of [...items].reverse()) {
    if (!item.content && item.status !== 'pending') continue;

    if (item.status === 'pending') {
      const wipBubble = makeWipBubble(item);
      fragment.appendChild(wipBubble);
      pollPendingItem(item, wipBubble);
      continue;
    }

    appendHistoryItem(item, fragment);
  }

  const anchor = topSentinel ? topSentinel.nextSibling : messages.firstChild;
  messages.insertBefore(fragment, anchor);
  messages.scrollTop += messages.scrollHeight - prevHeight;

  historyOffset += items.length;
  historyExhausted = !has_more;
  historyLoading = false;
  updateCtxHighlight();

  if (historyExhausted && topSentinel) {
    topSentinel.remove();
    topSentinel = null;
  } else if (!historyExhausted && topSentinel) {
    // If sentinel is still visible after inserting items, load next page immediately
    // (IntersectionObserver only fires on state changes, not continuously)
    const sr = topSentinel.getBoundingClientRect();
    const mr = messages.getBoundingClientRect();
    if (sr.bottom >= mr.top && sr.top <= mr.bottom) {
      loadHistory();
    }
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

// All Squid-owned commands. Shown in the / autocomplete popup.
// args:true = takes optional args (insert into input); args:false = execute directly on select.
const SQUID_COMMANDS = [
  { name: 'clear',        desc: 'clear session — next message starts fresh',   args: false },
  { name: 'compact',      desc: 'compact session (resets context for Codex)',   args: false },
  { name: 'stop',         desc: 'kill running process for current topic',       args: false },
  { name: 'stopall',      desc: 'kill + drain queue for current topic',         args: false },
  { name: 'deq',          desc: 'drain queue (deq N removes Nth item)',         args: true  },
  { name: 'restart',      desc: 'restart the server',                           args: false },
  { name: 'filter',       desc: 'filter history by current topic or agent',     args: false },
  { name: 'filter reset', desc: 'clear the active filter',                      args: false },
  { name: 'status',       desc: 'show active processes panel',                  args: false },
  { name: 'help',         desc: 'show help panel',                              args: false },
  { name: 'remote',       desc: 'show QR code for mobile / tablet access',      args: false },
];

function parseCommand(message) {
  const t = message.trim().replace(/^\//, ''); // strip optional leading /
  if (/^restart$/i.test(t))      return { command: 'restart' };
  if (/^stop$/i.test(t))         return { command: 'stop' };
  if (/^stopall$/i.test(t))      return { command: 'stopall' };
  if (/^clear$/i.test(t))        return { command: 'clear' };
  if (/^compact$/i.test(t))      return { command: 'compact' };
  if (/^status$/i.test(t))       return { command: 'status' };
  if (/^help$/i.test(t))         return { command: 'help' };
  if (/^remote$/i.test(t))       return { command: 'remote' };
  if (/^filter reset$/i.test(t)) return { command: 'filter_reset' };
  if (/^filter$/i.test(t))       return { command: 'filter' };
  const m = t.match(/^deq(?:\s+(-?\d+))?$/i);
  if (m) return { command: 'deq', pos: m[1] != null ? parseInt(m[1]) : null };
  return null;
}

async function handleCommand(cmd, topic, agent, adhoc = false, lookback = 0) {
  if (cmd.command === 'status') {
    toggleProcPopup();
    return;
  }
  if (cmd.command === 'help') {
    openHelp();
    return;
  }
  if (cmd.command === 'remote') {
    openRemoteQR();
    return;
  }
  if (cmd.command === 'filter') {
    if (agent) filterByAgent(topic, agent, adhoc, lookback);
    else filterByTopic(topic);
    return;
  }
  if (cmd.command === 'filter_reset') {
    clearFilter();
    return;
  }

  if (cmd.command === 'clear' || cmd.command === 'compact') {
    const feedbackEl = showCmdFeedback(`${cmd.command}…`);
    try {
      const body = { command: cmd.command, topic };
      if (agent) body.agent = agent;
      const res = await fetch('/cmd', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const data = await res.json();
      if (!data.ok) { feedbackEl.textContent = `${cmd.command} failed: ${data.error || ''}`; return; }
      const tag = agent ? `#${topic}@${agent}` : `#${topic}`;
      feedbackEl.textContent = `${tag} — session cleared`;
    } catch {
      feedbackEl.textContent = `${cmd.command} — request failed`;
    }
    return;
  }

  const label = cmd.command === 'deq'
    ? `deq${cmd.pos != null ? ' ' + cmd.pos : ''}`
    : cmd.command;
  const feedbackEl = showCmdFeedback(`${label}…`);

  try {
    const body = { command: cmd.command, topic };
    if (agent && (cmd.command === 'stop' || cmd.command === 'stopall')) body.agent = agent;
    if (cmd.command === 'stop' || cmd.command === 'stopall') body.adhoc = adhoc || null;
    if (cmd.pos != null) body.pos = cmd.pos;
    const res = await fetch('/cmd', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) { feedbackEl.textContent = `${label} failed`; return; }

    if (cmd.command === 'restart') {
      feedbackEl.textContent = 'restarting…';
      // Poll /health until server is back up, then reload
      const poll = async () => {
        try {
          const r = await fetch('/health');
          if (r.ok) { location.reload(); return; }
        } catch {}
        setTimeout(poll, 500);
      };
      setTimeout(poll, 800);
      return;
    }

    const detail = cmd.command === 'stop'    ? `#${topic} — killed ${data.killed}`
                 : cmd.command === 'stopall' ? `#${topic} — killed ${data.killed}, drained ${data.drained}`
                 : `#${topic} — drained ${data.drained}`;
    feedbackEl.textContent = `${label} ${detail}`;
  } catch {
    feedbackEl.textContent = `${label} — request failed`;
  }
}

function showCmdFeedback(text) {
  const el = document.createElement('div');
  el.className = 'cmd-feedback';
  el.textContent = text;
  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
  return el;
}

function resizeComposer() {
  input.style.height = 'auto';
  input.style.height = `${input.scrollHeight}px`;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  const { topic, agent, adhoc, lookback, message } = parseInput(text);
  const cmd = parseCommand(message);
  if (cmd) {
    input.value = '';
    resizeComposer();
    hideAutocomplete();
    await handleCommand(cmd, topic, agent, adhoc, lookback);
    // Re-set chip after topic-scoped commands so next message stays in context
    if (['clear', 'compact', 'stop', 'stopall', 'deq'].includes(cmd.command) && (topic !== 'default' || agent)) {
      setTopicChip(topic, agent, adhoc, lookback);
    }
    return;
  }
  input.value = '';
  resizeComposer();
  hideAutocomplete();
  invalidateTopicsCache();
  ctxHighlightEnabled = false;
  sendMessage(text);
  document.querySelectorAll('.history-item.ctx-highlight').forEach(el => el.classList.remove('ctx-highlight'));
});

function fmtCtxLabel(adhoc, lookback, pinCount = 0) {
  let base;
  if (!adhoc) base = 'sess';
  else base = lookback > 0 ? `${lookback} back${lookback !== 1 ? 's' : ''}` : 'none';
  return pinCount > 0 ? `${base} · ${pinCount} bookmarked` : base;
}

let ctxHighlightEnabled = false;

function updateCtxHighlight() {
  document.querySelectorAll('.history-item.ctx-highlight').forEach(el => el.classList.remove('ctx-highlight'));
  document.querySelectorAll('.msg-pin-btn.dynamic-sel').forEach(b => b.classList.remove('dynamic-sel'));
  if (!ctxHighlightEnabled) return;
  const { adhoc, lookback } = parseInput(input.value);
  if (!adhoc || lookback <= 0) return;
  const msgItems = [...document.querySelectorAll('.history-item.msg')];
  msgItems.slice(-lookback * 2).forEach(el => el.classList.add('ctx-highlight'));
  // Pre-highlight bookmark icons on the last N assistant messages
  const asstItems = [...document.querySelectorAll('#messages .history-item.msg.assistant')];
  asstItems.slice(-lookback).forEach(el => {
    const btn = el.querySelector('.msg-pin-btn');
    if (btn && !btn.classList.contains('pinned')) btn.classList.add('dynamic-sel');
  });
  if (pinPanel.classList.contains('open')) renderPinPanel();
}

input.addEventListener('input', () => { ctxHighlightEnabled = true; resizeComposer(); updateAutocomplete(); updateCtxHighlight(); });

input.addEventListener('keydown', (e) => {
  if (acOpen) {
    if (e.key === 'ArrowDown') { e.preventDefault(); acSel = Math.min(acSel + 1, acItems.length - 1); _acHighlight(); return; }
    if (e.key === 'ArrowUp')   { e.preventDefault(); acSel = Math.max(acSel - 1, -1); _acHighlight(); return; }
    if (e.key === 'Tab' || (e.key === 'Enter' && acSel >= 0)) { e.preventDefault(); _acSelect(acSel >= 0 ? acSel : 0); return; }
    if (e.key === 'Escape') { hideAutocomplete(); return; }
  }
  if (e.key === 'Escape' && pinPanel.classList.contains('open')) { closePinPanel(); return; }
  if (e.key === 'Escape' && helpPanel.classList.contains('open')) { closeHelp(); return; }
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
  const { topic, agent, adhoc, lookback, message } = parseInput(text);
  setTopicChip(topic, agent, adhoc, lookback);
  const sendTime = new Date().toISOString();

  const ctxLabel = fmtCtxLabel(adhoc, lookback);
  const userBubble = makeUserBubble(message, topic, agent, null, adhoc, lookback);
  const userTopicTag = userBubble.querySelector('.topic-tag');
  messages.appendChild(userBubble);
  const userTsEl = addTimestamp(userBubble, sendTime, true);
  let userCtxSpan = null;
  if (userTsEl) {
    userCtxSpan = document.createElement('span');
    userCtxSpan.className = 'user-ctx';
    userCtxSpan.textContent = '  · ctx:' + ctxLabel;
    userCtxSpan.addEventListener('click', e => { e.stopPropagation(); showCtxPopup(userCtxSpan); });
    userTsEl.appendChild(userCtxSpan);
  }
  requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });

  // ── Thinking bubble (visible immediately, shows status/queue/loader) ──────────
  const thinkingBubble = document.createElement('div');
  thinkingBubble.className = 'msg assistant msg-thinking';
  const thinkingContent = document.createElement('div');
  thinkingContent.className = 'thinking-live';
  thinkingBubble.appendChild(thinkingContent);
  messages.appendChild(thinkingBubble);
  const thinkingLoader = addLoader(thinkingContent);
  let thinkingFrozen = false;
  let statusBuf = '';

  // Kill button — shown once msg_id is known, hidden when done
  const killBtn = document.createElement('button');
  killBtn.type = 'button';
  killBtn.className = 'thinking-kill-btn';
  killBtn.title = 'Stop this process';
  killBtn.textContent = '×';
  killBtn.style.display = 'none';
  killBtn.addEventListener('click', async () => {
    killBtn.disabled = true;
    controller.abort();
    if (msgId) {
      await fetch('/cmd', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'stop_msg', topic, msg_id: msgId }),
      }).catch(() => {});
    }
  });
  thinkingBubble.appendChild(killBtn);

  function updateThinkingPreview() {
    if (thinkingFrozen) return;
    if (thinkingLoader.parentNode) thinkingLoader.remove();
    const text = (statusBuf ? statusBuf.trimEnd() + (raw ? '\n\n' : '') : '') + raw;
    thinkingContent.textContent = text.trim();
    thinkingContent.scrollTop = thinkingContent.scrollHeight;
    thinkingBubble.style.display = '';
    scrollToBottom();
  }
  function freezeThinking() {
    if (thinkingFrozen) return;
    thinkingFrozen = true;
    killBtn.style.display = 'none';
    if (statusBuf.trim()) {
      if (thinkingLoader.parentNode) thinkingLoader.remove();
      const lines = statusBuf.split('\n').map(l => l.trim()).filter(Boolean);
      const summary = lines[lines.length - 1] || '';
      const summaryTrunc = summary.length > 80 ? '…' + summary.slice(-77) : summary;
      thinkingContent.innerHTML = '';
      thinkingContent.className = '';  // remove scrollable class before freezing
      const toggle = document.createElement('button');
      toggle.className = 'thinking-toggle';
      toggle.textContent = summaryTrunc;
      const body = document.createElement('div');
      body.className = 'thinking-body';
      body.textContent = lines.join('\n');
      thinkingContent.appendChild(toggle);
      thinkingContent.appendChild(body);
      toggle.addEventListener('click', () => thinkingBubble.classList.toggle('thinking-expanded'));
      thinkingBubble.style.display = '';
      thinkingBubble.classList.add('msg-thinking-done');
    } else {
      thinkingBubble.remove();
    }
  }

  requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });

  // ── Response bubble (not yet in DOM — appended on first content chunk) ────────
  const bubble = document.createElement('div');
  bubble.className = 'msg assistant';
  const responseHeader = document.createElement('div');
  responseHeader.className = 'response-header';
  const responseHeaderTag = makeTopicTag(topic, agent, { adhoc, lookback });
  const headerText = document.createElement('span');
  headerText.className = 'response-header-text';
  headerText.appendChild(responseHeaderTag);
  headerText.appendChild(document.createTextNode('  ' + truncate(message, 55)));
  responseHeader.appendChild(headerText);
  bubble.appendChild(responseHeader);
  const contentDiv = document.createElement('div');
  bubble.appendChild(contentDiv);

  let firstDataReceived = false;
  const quotaBefore = quotaRaw;
  let lastSessionId = null;
  let statsEl = null;
  let doneTime = null;
  let msgId = null;
  let statusTimer = null;
  let completedFromStatus = false;
  let raw = '';
  let resolvedAgent = agent;  // updated by meta event
  const liveToolEvents = [];
  const controller = new AbortController();



  function revealResponseBubble() {
    if (firstDataReceived) return;
    firstDataReceived = true;
    // Bubble stays out of DOM until done — content streams into thinkingBubble as preview
    if (thinkingLoader.parentNode) thinkingLoader.remove();
    requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
  }

  function stopStatusFallback() {
    if (!statusTimer) return;
    clearInterval(statusTimer);
    statusTimer = null;
  }

  function showError(text) {
    revealResponseBubble();  // sets firstDataReceived, suppresses finally fallback
    if (!bubble.parentNode) messages.appendChild(bubble);
    const errDisplay = (text || 'Response interrupted.')
      .split('\n')[0]
      .replace(/^CLI exited \d+:\s*/, '')
      .trim();
    // Don't wipe streamed content with a generic fallback message
    if (!errDisplay && raw) return;
    contentDiv.innerHTML = `<span class="msg-error">${errDisplay || 'Response interrupted.'}</span>`;
    scrollToBottom();
  }

  function showStoredResponse(content) {
    if (!bubble.parentNode) messages.appendChild(bubble);
    raw = content || '';
    contentDiv.innerHTML = marked.parse(raw);
    scrollToBottom();
  }

  function startStatusFallback(id) {
    if (statusTimer || !id) return;
    statusTimer = setInterval(async () => {
      try {
        const statusRes = await fetch(`/chat/${id}/status`);
        if (!statusRes.ok) return;
        const data = await statusRes.json();
        if (data.status === 'done') {
          completedFromStatus = true;
          stopStatusFallback();
          doneTime = new Date().toISOString();
          showStoredResponse(data.content || '');
          controller.abort();
        } else if (data.status === 'error') {
          completedFromStatus = true;
          stopStatusFallback();
          showError(data.content || 'Response interrupted.');
          controller.abort();
        }
      } catch {}
    }, 2000);
  }

  // Compute pinned IDs to inject — works for both session and adhoc turns
  const _effectiveAgent = agent || stickyChip?.agent || null;
  const _taKey = `${topic}@${_effectiveAgent || '_'}`;
  const _injected = getInjectedInto();
  const _currentSid = _sessionIds[_taKey] || null;
  const _pinnedIds = getPinnedItems()
    .filter(item => {
      // Skip bookmarks from the current session — --resume already has that context
      const sameSession = item.session_id && _currentSid && item.session_id === _currentSid;
      if (sameSession && !adhoc) return false;
      // Skip already-injected items
      if ((_injected[_taKey] || []).includes(item.id)) return false;
      return true;
    })
    .map(item => item.id);

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, topic, agent, lookback, adhoc, ...(_pinnedIds.length ? { pinned_ids: _pinnedIds } : {}) }),
      // lookback: 0 for session mode (CLI owns context), N for adhoc #topic!N
      signal: controller.signal,
    });
    if (res.status === 400) {
      const err = await res.json().catch(() => ({}));
      if (err.error && err.error.includes('not found')) {
        freezeThinking();
        showAgentCreatePrompt(agent, () => sendMessage(text));
        return;
      }
      throw new Error(err.error || `HTTP 400`);
    }
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
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
              resolvedAgent = meta.agent || (meta.backend !== 'auto' ? meta.backend : null);
              const resolvedAdhoc = adhoc; // server echoes back what we sent; use closure as reliable source
              const newTag = makeTopicTag(topic, resolvedAgent, { adhoc: resolvedAdhoc, clickable: true, lookback });
              responseHeaderTag.replaceWith(newTag);
              const newUserTag = makeTopicTag(topic, resolvedAgent, { adhoc: resolvedAdhoc, clickable: true, lookback });
              if (userTopicTag) {
                userTopicTag.replaceWith(newUserTag);
              } else if (resolvedAgent || topic !== 'default') {
                const content = userBubble.firstElementChild;
                if (content) {
                  content.insertBefore(document.createTextNode(' '), content.firstChild);
                  content.insertBefore(newUserTag, content.firstChild);
                }
              }
              setTopicChip(topic, resolvedAgent, resolvedAdhoc, lookback);
              // If no ctx span yet but resolved agent found, add ctx to the timestamp footer
              if (!userCtxSpan && resolvedAgent && !resolvedAdhoc && userTsEl) {
                userCtxSpan = document.createElement('span');
                userCtxSpan.className = 'user-ctx';
                userCtxSpan.textContent = '  · ctx:session';
                userTsEl.appendChild(userCtxSpan);
              }
              if (meta.msg_id) {
                msgId = meta.msg_id;
                startStatusFallback(msgId);
                addPinButton(bubble, msgId, topic, resolvedAgent);
                killBtn.style.display = '';
              }
            } catch {}
            eventName = null;

          } else if (eventName === 'queued') {
            try {
              const info = JSON.parse(data);
              setThinkingText(`#${info.topic} · queued — position ${info.position}`);
            } catch {}
            pollProcs();
            eventName = null;

          } else if (eventName === 'stats') {
            try {
              const stats = JSON.parse(data);
              lastSessionId = stats.session_id ?? null;
              if (stats.session_id && !adhoc) {
                _sessionIds[`${topic}@${resolvedAgent || '_'}`] = stats.session_id;
                bubble.dataset.sessionId = stats.session_id;
              }
              statsEl = addStats(bubble, stats, new Date().toISOString());
              // Update user timestamp ctx with real compound label from stats
              const finalCtxLabel = fmtCtxLabel(!!stats.adhoc, stats.lookback ?? 0);
              if (userTsEl) {
                if (!userCtxSpan && finalCtxLabel) {
                  userCtxSpan = document.createElement('span');
                  userCtxSpan.className = 'user-ctx';
                  userTsEl.appendChild(userCtxSpan);
                }
                if (userCtxSpan) {
                  if (finalCtxLabel) {
                    userCtxSpan.textContent = '  · ctx:' + finalCtxLabel;
                    if (stats.session_id) userCtxSpan.dataset.sessionId = stats.session_id;
                    if (stats.cwd) userCtxSpan.dataset.cwd = stats.cwd;
                  } else {
                    userCtxSpan.remove();
                    userCtxSpan = null;
                  }
                }
              }
            } catch {}
            eventName = null;

          } else if (eventName === 'tool') {
            try {
              const tool = JSON.parse(data);
              liveToolEvents.push(tool);
              const label = toolLabel(tool);
              statusBuf += label + '\n';
              updateThinkingPreview();
            } catch {}
            eventName = null;

          } else if (eventName === 'status') {
            if (statusBuf && !statusBuf.endsWith('\n')) statusBuf += ' ';
            statusBuf += data.trimStart();
            updateThinkingPreview();
            // no eventName reset — allow multi-line accumulation

          } else if (eventName === 'done') {
            stopStatusFallback();
            freezeThinking();
            invalidateTopicsCache();
            doneTime = new Date().toISOString();
            if (firstDataReceived) {
              contentDiv.innerHTML = marked.parse(raw);
              messages.appendChild(bubble);
              if (statsEl) messages.appendChild(statsEl); // stats goes between bubble and diffs, not after
              const diffTools = liveToolEvents.filter(t => t.name === 'Edit' || t.name === 'Write' || t.name === 'MultiEdit' || t.name === 'Diff');
              for (const tool of diffTools) {
                const block = makeToolBlock(tool);
                block.classList.add('tool-block-history');
                messages.appendChild(block);
              }
              scrollToBottom();
            }
            // Update ctx label with pin count and store IDs for popup
            if (_pinnedIds.length && userCtxSpan) {
              const finalCtx = fmtCtxLabel(adhoc, lookback, _pinnedIds.length);
              userCtxSpan.textContent = '  · ctx:' + finalCtx;
              userCtxSpan.dataset.pinnedIds = JSON.stringify(_pinnedIds);
            }
            // Record injected pinned IDs so they're not re-injected into this session
            if (_pinnedIds.length) {
              const _finalAgent = resolvedAgent || agent || null;
              const _taKey = `${topic}@${_finalAgent || '_'}`;
              const _inj = getInjectedInto();
              _inj[_taKey] = [...new Set([...(_inj[_taKey] || []), ..._pinnedIds])];
              setInjectedInto(_inj);
              if (pinPanel.classList.contains('open')) renderPinPanel();
            }
            eventName = null;

          } else if (eventName === 'error') {
            stopStatusFallback();
            const errLine = data.trim();
            showError(errLine);
            eventName = null;

          } else {
            // Actual response content — accumulate and show in thinking preview
            if (!firstDataReceived) revealResponseBubble();
            if (dataLineCount > 1) raw += '\n';
            else if (raw.length && data.length && !/\s$/.test(raw) && !/^\s/.test(data)) raw += '\n';
            raw += data;
            updateThinkingPreview();
          }

        } else if (line === '') {
          eventName = null;
          dataLineCount = 0;
        }
      }
    }
  } catch (err) {
    if (!completedFromStatus && err.name !== 'AbortError') {
      if (msgId) {
        try {
          const statusRes = await fetch(`/chat/${msgId}/status`);
          if (statusRes.ok) {
            const data = await statusRes.json();
            if (data.status === 'done') showStoredResponse(data.content || '');
            else if (data.status === 'error') showError(data.content || 'Response interrupted.');
            else showError('Connection interrupted before the response finished.');
          } else {
            showError('Connection interrupted before the response finished.');
          }
        } catch {
          showError('Connection interrupted before the response finished.');
        }
      } else {
        showError('Unable to start response stream.');
      }
    }
  } finally {
    stopStatusFallback();
    if (!thinkingFrozen) {
      if (msgId && !firstDataReceived && !completedFromStatus) {
        const content = document.createElement('span');
        content.className = 'msg-error';
        content.textContent = 'Response is still running. Reopen the page or history to pick it up.';
        if (!bubble.parentNode) messages.appendChild(bubble);
        contentDiv.appendChild(content);
      } else {
        freezeThinking();
      }
    }
    if (!firstDataReceived && !completedFromStatus) {
      if (!bubble.parentNode) messages.appendChild(bubble);
      contentDiv.innerHTML = '<span class="msg-error">No response — backend may be rate-limited or unavailable.</span>';
    }
    if (!statsEl && doneTime && firstDataReceived) addTimestamp(bubble, doneTime, false);
  }

  // Quota snapshot — wait briefly for claude.ai API to reflect the just-completed turn
  await new Promise(r => setTimeout(r, 3000));
  await fetchQuota(true);
  const quotaAfter = quotaRaw;
  if (quotaBefore !== null && quotaAfter !== null && quotaAfter !== quotaBefore) {
    const d = Math.round((quotaAfter - quotaBefore) * 10) / 10;
    if (statsEl && d > 0) {
      statsEl.querySelector('.stats-quota-delta').textContent = `  ·  +${d}%`;
    }
    if (msgId) {
      fetch(`/chat/${msgId}/quota-delta`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ before: quotaBefore, after: quotaAfter }),
      }).catch(() => {});
    }
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

function toolLabel(tool) {
  const name = tool.name || '';
  if (name === 'Read' || name === 'Edit' || name === 'Write' || name === 'MultiEdit' || name === 'Diff')
    return `${name}: ${tool.file || ''}`;
  if (name === 'Bash') return `Bash: ${truncate(tool.command || '', 70)}`;
  if (name === 'Agent') return `Agent: ${truncate(tool.description || '', 70)}`;
  if (name === 'WebFetch' || name === 'WebSearch') return `${name}: ${truncate(tool.query || '', 70)}`;
  if (name === 'TodoWrite') { const n = (tool.todos || []).length; return `TodoWrite: ${n} item${n !== 1 ? 's' : ''}`; }
  return name + (tool.key ? ': ' + truncate(tool.value || '', 50) : '');
}


function renderDiffLines(container, oldStr, newStr) {
  for (const line of (oldStr || '').split('\n')) {
    const el = document.createElement('span');
    el.className = 'diff-line diff-remove';
    el.textContent = '- ' + line;
    container.appendChild(el);
  }
  for (const line of (newStr || '').split('\n')) {
    const el = document.createElement('span');
    el.className = 'diff-line diff-add';
    el.textContent = '+ ' + line;
    container.appendChild(el);
  }
}

function renderUnifiedDiffLines(container, diff) {
  for (const line of (diff || '').split('\n')) {
    const el = document.createElement('span');
    if (line.startsWith('+') && !line.startsWith('+++')) {
      el.className = 'diff-line diff-add';
    } else if (line.startsWith('-') && !line.startsWith('---')) {
      el.className = 'diff-line diff-remove';
    } else if (line.startsWith('@@')) {
      el.className = 'diff-line diff-hunk';
    } else {
      el.className = 'diff-line';
    }
    el.textContent = line;
    container.appendChild(el);
  }
}

function makeToolBlock(tool) {
  const name = tool.name || '';
  const block = document.createElement('div');
  block.className = 'tool-block';

  const hasDiff = name === 'Edit' || name === 'MultiEdit' || name === 'Write' || name === 'Diff';
  if (!hasDiff) {
    const label = document.createElement('span');
    label.className = 'tool-label';
    if (name === 'Read')   label.textContent = 'Read: ' + (tool.file || '');
    else if (name === 'Bash')  label.textContent = 'Bash: ' + truncate(tool.command || '', 100);
    else if (name === 'Agent') label.textContent = 'Agent: ' + truncate(tool.description || '', 80);
    else if (name === 'WebFetch' || name === 'WebSearch')
      label.textContent = name + ': ' + truncate(tool.query || '', 80);
    else if (name === 'TodoWrite') {
      const n = (tool.todos || []).length;
      label.textContent = `TodoWrite: ${n} item${n !== 1 ? 's' : ''}`;
    } else label.textContent = name + (tool.key ? ': ' + truncate(tool.value || '', 60) : '');
    block.appendChild(label);
    return block;
  }

  const toggle = document.createElement('button');
  toggle.className = 'tool-toggle';
  const body = document.createElement('div');
  body.className = 'tool-body';
  const scroll = document.createElement('div');
  scroll.className = 'diff-scroll';
  body.appendChild(scroll);

  if (name === 'Edit') {
    toggle.textContent = 'Edit: ' + (tool.file || '');
    renderDiffLines(scroll, tool.old || '', tool.new || '');
  } else if (name === 'MultiEdit') {
    toggle.textContent = 'MultiEdit: ' + (tool.file || '');
    (tool.edits || []).forEach((edit, i) => {
      if (i > 0) { const sep = document.createElement('div'); sep.className = 'diff-sep'; scroll.appendChild(sep); }
      renderDiffLines(scroll, edit.old_string || '', edit.new_string || '');
    });
  } else if (name === 'Write') {
    toggle.textContent = 'Write: ' + (tool.file || '');
    for (const line of (tool.content || '').split('\n')) {
      const el = document.createElement('span');
      el.className = 'diff-line diff-add';
      el.textContent = '+ ' + line;
      scroll.appendChild(el);
    }
  } else if (name === 'Diff') {
    toggle.textContent = 'Diff: ' + (tool.file || 'working tree');
    renderUnifiedDiffLines(scroll, tool.diff || '');
  }

  toggle.addEventListener('click', () => block.classList.toggle('tool-expanded'));
  block.appendChild(toggle);
  block.appendChild(body);
  return block;
}


function appendHistoryItem(item, container) {
  const lb = item.stats?.lookback ?? 0;
  const asstBubble = document.createElement('div');
  asstBubble.className = 'msg assistant history-item';

  const asstHeader = document.createElement('div');
  asstHeader.className = 'response-header';
  const asstLabel = item.agent || item.backend;
  const asstTag = makeTopicTag(item.topic || 'default', asstLabel, { clickable: true, adhoc: !!item.adhoc, lookback: lb });
  const asstHeaderText = document.createElement('span');
  asstHeaderText.className = 'response-header-text';
  asstHeaderText.appendChild(asstTag);
  asstHeaderText.appendChild(document.createTextNode('  '));
  const promptSpan = document.createElement('span');
  promptSpan.className = 'history-prompt';
  promptSpan.textContent = truncate(item.prompt || '', 55);
  promptSpan.dataset.full = item.prompt || '';
  promptSpan.addEventListener('click', () => {
    const expanded = promptSpan.classList.toggle('expanded');
    promptSpan.textContent = expanded ? promptSpan.dataset.full : truncate(promptSpan.dataset.full, 55);
    asstHeaderText.classList.toggle('expanded', expanded);
  });
  asstHeaderText.appendChild(promptSpan);
  asstHeader.appendChild(asstHeaderText);
  asstBubble.appendChild(asstHeader);

  const asstContent = document.createElement('div');
  if (item.status === 'error') {
    const raw = (item.content || '').split('\n')[0].replace(/^CLI exited \d+:\s*/, '').trim();
    asstContent.innerHTML = `<span class="msg-error">${raw || 'Response interrupted.'}</span>`;
  } else {
    asstContent.innerHTML = marked.parse(item.content || '');
  }
  asstBubble.appendChild(asstContent);
  if (item.id) addPinButton(asstBubble, item.id, item.topic || 'default', item.agent || null, item.session_id || null);

  if (container) container.appendChild(asstBubble);

  if (item.stats) {
    const statsEl = addStats(asstBubble, item.stats, item.timestamp);
    statsEl.classList.add('history-item');
  } else if (item.timestamp) {
    const tsEl = addTimestamp(asstBubble, item.timestamp);
    if (tsEl) tsEl.classList.add('history-item');
  }

  if (item.context) {
    try {
      const tools = typeof item.context === 'string' ? JSON.parse(item.context) : item.context;
      const diffTools = tools.filter(t => t.name === 'Edit' || t.name === 'Write' || t.name === 'MultiEdit' || t.name === 'Diff');
      let lastEl = asstBubble;
      for (const tool of diffTools) {
        const block = makeToolBlock(tool);
        block.classList.add('history-item', 'tool-block-history');
        if (container) container.appendChild(block);
        else { lastEl.after(block); lastEl = block; }
      }
    } catch {}
  }

  return asstBubble;
}

function makeWipBubble(item) {
  const bubble = document.createElement('div');
  bubble.className = 'msg assistant msg-thinking history-item';
  const content = document.createElement('div');
  content.className = 'thinking-live';
  const asstLabel = item.agent || item.backend || '';
  const statusLine = document.createElement('span');
  statusLine.textContent = `#${item.topic || 'default'}${asstLabel ? ' @' + asstLabel : ''} · ${truncate(item.prompt || '', 55)}`;
  content.appendChild(statusLine);
  addLoader(content);
  bubble.appendChild(content);
  const killBtn = document.createElement('button');
  killBtn.type = 'button';
  killBtn.className = 'thinking-kill-btn';
  killBtn.title = 'Stop this process';
  killBtn.textContent = '×';
  killBtn.addEventListener('click', async () => {
    killBtn.disabled = true;
    await fetch('/cmd', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: 'stop_msg', msg_id: item.id }) }).catch(() => {});
  });
  bubble.appendChild(killBtn);
  return bubble;
}

async function pollPendingItem(item, wipBubble) {
  const MAX_POLLS = 960;
  let count = 0;
  const timer = setInterval(async () => {
    count++;
    try {
      const res = await fetch(`/chat/${item.id}/status`);
      if (!res.ok) { clearInterval(timer); return; }
      const data = await res.json();
      if (data.status === 'done' || data.status === 'error') {
        clearInterval(timer);
        if (!wipBubble.parentNode) return;
        const finalBubble = appendHistoryItem(data, null);
        wipBubble.replaceWith(finalBubble);
      } else if (count >= MAX_POLLS) {
        clearInterval(timer);
        const content = wipBubble.querySelector('.thinking-live');
        if (content) content.innerHTML += '<br><span class="msg-error">Timed out.</span>';
      }
    } catch { clearInterval(timer); }
  }, 2000);
}

async function pollMessageStatus(msgId, contentEl, bubbleEl, stopBtn = null) {
  const MAX_POLLS = 960;  // 32 min at 2s intervals — covers 30 min default timeout
  let count = 0;
  const timer = setInterval(async () => {
    count++;
    try {
      const res = await fetch(`/chat/${msgId}/status`);
      if (!res.ok) { clearInterval(timer); return; }
      const data = await res.json();
      if (data.status === 'done') {
        clearInterval(timer);
        if (stopBtn) stopBtn.remove();
        contentEl.style.paddingRight = '';
        contentEl.innerHTML = marked.parse(data.content || '');
      } else if (data.status === 'error') {
        clearInterval(timer);
        if (stopBtn) stopBtn.remove();
        contentEl.style.paddingRight = '';
        const raw = (data.content || '').split('\n')[0].replace(/^CLI exited \d+:\s*/, '').trim();
        contentEl.innerHTML = `<span class="msg-error">${raw || 'Response interrupted.'}</span>`;
      } else if (count >= MAX_POLLS) {
        clearInterval(timer);
        if (stopBtn) stopBtn.remove();
        contentEl.innerHTML = '<span class="msg-error">Response timed out.</span>';
      } else if (data.content) {
        contentEl.innerHTML = marked.parse(data.content);
        addLoader(contentEl);
      }
    } catch {
      clearInterval(timer);
    }
  }, 2000);
}

function makeUserBubble(text, topic, agent, backendFallback = null, adhoc = false, lookback = 0) {
  const div = document.createElement('div');
  div.className = 'msg user';
  const content = document.createElement('div');
  const showTag = topic && (topic !== 'default' || agent || adhoc);
  if (showTag) {
    const label = agent || backendFallback;
    const tag = makeTopicTag(topic, label, { clickable: true, adhoc, lookback });
    content.appendChild(tag);
    content.appendChild(document.createTextNode(' '));
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

function addStats(bubble, stats, timestamp) {
  const el = document.createElement('div');
  el.className = 'stats';

  const cacheRead  = stats.cache_read_tokens  || 0;
  const cacheWrite = stats.cache_write_tokens || 0;
  const input      = stats.input_tokens       || 0;
  const out        = stats.output_tokens      || 0;
  // ── Token semantics differ by backend (authoritative source: runners.py) ────────────
  // Claude: input_tokens is a ~2–4 token uncacheable residual. The user's actual message
  //   lands in cache_write (cache_creation_input_tokens). True total = input + cacheWrite
  //   + cacheRead. Seeing "3 new tokens" is correct, not a bug.
  // Codex: input_tokens is the FULL total; cache_read is a subset already inside it.
  //   Adding cache_read would double-count. output_tokens already includes reasoning.
  // We have gone back and forth on this — do not "fix" by treating input alone as total.
  // Heuristic to tell them apart: Claude has input < (cacheRead + cacheWrite).
  // ─────────────────────────────────────────────────────────────────────────────────────
  const isSplit    = (cacheRead + cacheWrite) > 0 && input < (cacheRead + cacheWrite);
  const inp        = isSplit ? input + cacheRead + cacheWrite : input;
  const newThis    = isSplit ? input + cacheWrite : (cacheRead > 0 ? input - cacheRead : 0);
  const detailLabel = isSplit ? ` (${fmtNum(newThis)} new)`
                    : cacheRead > 0 ? ` (${fmtNum(newThis)} uncached)`
                    : '';
  const hasCost    = stats.cost_usd != null;
  const cost       = hasCost ? `$${stats.cost_usd.toFixed(4)}` : '';
  const cacheStr   = cacheRead ? ` · ${fmtNum(cacheRead)} cached` : '';
  const dur        = stats.duration_ms ? ` · ${(stats.duration_ms / 1000).toFixed(1)}s` : '';
  const timePrefix = timestamp ? fmtTime(timestamp) + '  ·  ' : '';

  el.appendChild(document.createTextNode(`${timePrefix}↑ ${fmtNum(inp)}`));
  if (detailLabel) {
    const detailSpan = document.createElement('span');
    detailSpan.className = 'stats-token-detail';
    detailSpan.textContent = detailLabel;
    el.appendChild(detailSpan);
  }
  if (cacheStr) {
    const cacheSpan = document.createElement('span');
    cacheSpan.className = 'stats-cache-str';
    cacheSpan.textContent = cacheStr;
    el.appendChild(cacheSpan);
  }
  el.appendChild(document.createTextNode(`  ↓ ${fmtNum(out)} tokens${dur}`));

  const qdSpan = document.createElement('span');
  qdSpan.className = 'stats-quota-delta';
  const msgQd = (stats.msg_quota_before != null && stats.msg_quota_after != null)
    ? Math.round((stats.msg_quota_after - stats.msg_quota_before) * 10) / 10
    : stats.quota_delta;
  if (msgQd != null && msgQd > 0) qdSpan.textContent = `  ·  +${msgQd}%`;
  el.appendChild(qdSpan);

  let rows, thead, tfoot;
  if (hasCost) {
    const TOKEN_ROWS = [
      ['New input',   input],
      ['Cache write', cacheWrite],
      ['Cache read',  cacheRead],
      ['Output',      out],
    ];
    rows = TOKEN_ROWS
      .filter(([, n]) => n > 0)
      .map(([label, n]) => `<tr><td>${label}</td><td>${fmtNum(n)}</td></tr>`)
      .join('');
    thead = '<tr><th>Type</th><th>Tokens</th></tr>';
    tfoot = `<tfoot><tr><td>Total cost</td><td>${cost}</td></tr></tfoot>`;
  } else {
    const TOKEN_ROWS = [
      ['Cache read', cacheRead],
      ['Input',      input],
      ['Output',     out],
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
    <tbody>${rows}</tbody>
    ${tfoot}
  </table>`;
  el.appendChild(tooltip);

  el.addEventListener('click', (e) => {
    e.stopPropagation();
    el.classList.toggle('stats-locked');
  });
  document.addEventListener('click', () => el.classList.remove('stats-locked'), { capture: true });

  bubble.after(el);
  return el;
}

function fmtNum(n) {
  const value = Number(n) || 0;
  const abs = Math.abs(value);
  if (abs >= 1_000_000) {
    return (value / 1_000_000).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }) + 'M';
  }
  if (abs >= 1000) {
    return (value / 1000).toLocaleString(undefined, {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    }) + 'K';
  }
  return Math.round(value).toLocaleString();
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

    if (trackDelta && quotaRaw !== null) {
      const d = raw - quotaRaw;
      quotaDelta = d > 0.05 ? Math.round(d * 10) / 10 : null;
    }
    quotaRaw    = raw;
    quotaPct    = pct;
    quotaResetAt = new Date(session.resets_at).getTime();

    quotaDisplay.classList.add('loaded');
    updateQuotaLabel(pct);

    if (quotaTimer) clearInterval(quotaTimer);
    quotaTimer = setInterval(() => updateQuotaLabel(pct), 10000);
  } catch {}
}

const QUOTA_PIE_C = 2 * Math.PI * 6; // circumference for r=6

function updateQuotaLabel(pct) {
  const label = document.getElementById('quota-label');
  if (!label) return;
  const delta = quotaDelta != null ? ` +${quotaDelta}%` : '';
  if (!quotaResetAt) { label.textContent = `${pct}%${delta}`; return; }
  const diff = quotaResetAt - Date.now();
  if (diff <= 0) { label.textContent = `${pct}%${delta} · resetting`; return; }
  const totalMin = Math.floor(diff / 60000);
  const h = Math.floor(totalMin / 60);
  const m = String(totalMin % 60).padStart(2, '0');
  const timeStr = h > 0 ? `${h}:${m}` : `${m}m`;
  label.textContent = `${pct}%${delta} in ${timeStr}`;

  const arc = document.getElementById('quota-pie-arc');
  if (arc) {
    const filled = (pct / 100) * QUOTA_PIE_C;
    arc.setAttribute('stroke-dasharray', `${filled} ${QUOTA_PIE_C}`);
    arc.setAttribute('stroke', pct >= 80 ? '#e05030' : '#f07040');
  }
}

function initQuota() {
  quotaDisplay.innerHTML = `
    <svg id="quota-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
      <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
      <circle id="quota-pie-arc" cx="9" cy="9" r="6" fill="none" stroke="#f07040"
              stroke-width="4" stroke-dasharray="0 ${QUOTA_PIE_C}" stroke-linecap="round"
              transform="rotate(-90 9 9)"/>
    </svg>
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

// ── Codex (ChatGPT) quota ──────────────────────────────────────────────────────

const codexQuotaDisplay = document.getElementById('codex-quota-display');
let codexResetAt  = null;
let codexTimer    = null;
const CODEX_PIE_C = 2 * Math.PI * 6;

async function fetchCodexQuota() {
  try {
    const res = await fetch('/quota/codex');
    if (!res.ok) {
      showCodexQuotaError(res.status === 400 ? 'Codex auth' : 'Codex error');
      return;
    }
    const data = await res.json();
    const primary = data?.rate_limit?.primary_window;
    if (!primary) {
      showCodexQuotaError('Codex n/a');
      return;
    }
    const pct = Math.max(0, Math.min(100, Math.round(primary.used_percent ?? 0)));
    codexResetAt = primary.reset_after_seconds != null
      ? Date.now() + primary.reset_after_seconds * 1000
      : (primary.reset_at != null ? primary.reset_at * 1000 : null);

    codexQuotaDisplay.classList.remove('error');
    codexQuotaDisplay.classList.add('loaded');
    codexQuotaDisplay.title = buildCodexQuotaTitle(data);
    updateCodexLabel(pct);

    if (codexTimer) clearInterval(codexTimer);
    codexTimer = setInterval(() => updateCodexLabel(pct), 10000);
  } catch {
    showCodexQuotaError('Codex error');
  }
}

function updateCodexLabel(pct) {
  const label = document.getElementById('codex-quota-label');
  if (!label) return;
  const diff = codexResetAt ? codexResetAt - Date.now() : null;
  let timeStr = '';
  if (diff != null && diff > 0) {
    const totalMin = Math.floor(diff / 60000);
    const h = Math.floor(totalMin / 60);
    const m = String(totalMin % 60).padStart(2, '0');
    timeStr = ' in ' + (h > 0 ? `${h}:${m}` : `${m}m`);
  }
  label.textContent = `${pct}%${timeStr}`;

  const arc = document.getElementById('codex-pie-arc');
  if (arc) {
    const filled = (pct / 100) * CODEX_PIE_C;
    arc.setAttribute('stroke-dasharray', `${filled} ${CODEX_PIE_C}`);
    arc.setAttribute('stroke', pct >= 80 ? '#e05030' : '#10a37f');
  }
}

function showCodexQuotaError(text) {
  codexResetAt = null;
  codexQuotaDisplay.classList.remove('loaded');
  codexQuotaDisplay.classList.add('error');
  codexQuotaDisplay.title = 'Codex usage unavailable · click for credentials';

  const label = document.getElementById('codex-quota-label');
  if (label) label.textContent = text;

  const arc = document.getElementById('codex-pie-arc');
  if (arc) arc.setAttribute('stroke-dasharray', `0 ${CODEX_PIE_C}`);

  if (codexTimer) {
    clearInterval(codexTimer);
    codexTimer = null;
  }
}

function buildCodexQuotaTitle(data) {
  const primary = data?.rate_limit?.primary_window;
  const secondary = data?.rate_limit?.secondary_window;
  const parts = ['Codex usage'];
  if (primary?.used_percent != null) {
    parts.push(`5h ${Math.round(primary.used_percent)}%`);
  }
  if (secondary?.used_percent != null) {
    parts.push(`weekly ${Math.round(secondary.used_percent)}%`);
  }
  if (data?.rate_limit?.limit_reached) {
    parts.push('limit reached');
  }
  return parts.join(' · ') + ' · click for credentials';
}

function initCodexQuota() {
  codexQuotaDisplay.innerHTML = `
    <svg id="codex-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
      <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
      <circle id="codex-pie-arc" cx="9" cy="9" r="6" fill="none" stroke="#10a37f"
              stroke-width="4" stroke-dasharray="0 ${CODEX_PIE_C}" stroke-linecap="round"
              transform="rotate(-90 9 9)"/>
    </svg>
    <span id="codex-quota-label"></span>`;
  showCodexQuotaError('Codex auth');

  const credsPopup = document.getElementById('codex-creds-popup');
  codexQuotaDisplay.addEventListener('click', () => credsPopup.classList.toggle('open'));
  document.addEventListener('click', (e) => {
    if (!codexQuotaDisplay.contains(e.target) && !credsPopup.contains(e.target))
      credsPopup.classList.remove('open');
  });
  fetchCodexQuota();
}

function initCodexCreds() {
  const tokenInput = document.getElementById('codex-creds-token');
  const saveBtn    = document.getElementById('codex-creds-save');
  const status     = document.getElementById('codex-creds-status');

  saveBtn.addEventListener('click', async () => {
    const token = tokenInput.value.trim();
    if (!token) { status.textContent = 'token required'; return; }
    try {
      const res = await fetch('/config/creds/codex', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      });
      if (res.ok) {
        status.textContent = 'saved ✓';
        tokenInput.value = '';
        fetchCodexQuota();
      } else {
        status.textContent = 'failed';
      }
    } catch { status.textContent = 'error'; }
    setTimeout(() => { status.textContent = ''; }, 3000);
  });
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

// ── process status dot + popup ────────────────────────────────────────────────

const procStatusBtn   = document.getElementById('proc-status');
const procStatusPopup = document.getElementById('proc-status-popup');
let procPollInterval  = null;

function updateProcStatusDot(running, queued) {
  procStatusBtn.classList.toggle('has-procs', running.length > 0 || queued.length > 0);
}

function renderProcPopup(running, queued) {
  const header = `<div class="proc-popup-header">
    <span class="settings-label">Processes</span>
    <button id="proc-popup-close" type="button">✕</button>
  </div>`;

  let body = '';
  if (!running.length && !queued.length) {
    body = '<div class="proc-status-empty">No active processes or queued prompts.</div>';
  } else {
    if (running.length) {
      const rows = running.map(r => `
        <tr>
          <td><span class="proc-dot"></span>#${r.topic || '—'}</td>
          <td>@${r.agent || '—'}</td>
          <td class="proc-queue-preview">${r.prompt_preview || '—'}</td>
          <td>${r.duration_s}s</td>
          <td><button class="proc-stop-btn" data-msgid="${r.msg_id || ''}" data-topic="${r.topic || ''}" data-agent="${r.agent || ''}">Stop</button></td>
        </tr>`).join('');
      body += `<div class="proc-section-label">Running</div>
        <table><thead><tr><th>Topic</th><th>Agent</th><th>Prompt</th><th>Time</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`;
    }
    if (queued.length) {
      const rows = queued.map(r => `
        <tr>
          <td>#${r.topic || '—'}</td>
          <td>@${r.agent || '—'}</td>
          <td class="proc-queue-preview">${r.prompt_preview || '—'}</td>
          <td><button class="proc-deq-btn" data-topic="${r.topic || ''}" data-pos="${r.position}">✕</button></td>
        </tr>`).join('');
      body += `<div class="proc-section-label">Queued</div>
        <table><thead><tr><th>Topic</th><th>Agent</th><th>Prompt</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`;
    }
  }

  procStatusPopup.innerHTML = header + body;

  procStatusPopup.querySelectorAll('.proc-stop-btn').forEach(btn => {
    btn.addEventListener('click', async e => {
      e.stopPropagation();
      btn.disabled = true; btn.textContent = '…';
      const b = btn.dataset.msgid
        ? { command: 'stop_msg', msg_id: parseInt(btn.dataset.msgid) }
        : { command: 'stop', topic: btn.dataset.topic, agent: btn.dataset.agent };
      await fetch('/cmd', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
      await pollProcs();
    });
  });

  procStatusPopup.querySelectorAll('.proc-deq-btn').forEach(btn => {
    btn.addEventListener('click', async e => {
      e.stopPropagation();
      btn.disabled = true; btn.textContent = '…';
      await fetch('/cmd', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'deq', topic: btn.dataset.topic, pos: parseInt(btn.dataset.pos) }),
      });
      await pollProcs();
    });
  });

  document.getElementById('proc-popup-close').addEventListener('click', e => {
    e.stopPropagation();
    procStatusPopup.classList.remove('open');
  });
}

function toggleProcPopup() {
  const open = procStatusPopup.classList.toggle('open');
  if (open) { renderProcPopup(cachedProcRows, cachedQueueRows); pollProcs(); }
}

let cachedProcRows  = [];
let cachedQueueRows = [];

async function pollProcs() {
  try {
    const [procRes, queueRes] = await Promise.all([fetch('/processes'), fetch('/queue')]);
    cachedProcRows  = await procRes.json();
    cachedQueueRows = await queueRes.json();
    updateProcStatusDot(cachedProcRows, cachedQueueRows);
    if (procStatusPopup.classList.contains('open')) renderProcPopup(cachedProcRows, cachedQueueRows);
  } catch { /* ignore */ }
}

function startProcPoll() {
  if (procPollInterval) return;
  pollProcs();
  procPollInterval = setInterval(pollProcs, 3000);
}

procStatusBtn.addEventListener('click', e => {
  e.stopPropagation();
  toggleProcPopup();
});

// keep legacy aliases so switchView / initStats still work
async function loadStats() {
  statsContent.innerHTML = '<div class="empty">Loading…</div>';
  let rows;
  try {
    const url = statsGroup === 'topic' ? '/stats?group=topic'
              : statsGroup === 'model' ? '/stats?group=agent'
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
  } else if (statsGroup === 'model') {
    renderAgentStats(rows);
  } else {
    renderTimeStats(rows);
  }
}

function renderTimeStats(rows) {
  let totalSessions = 0, totalIn = 0, totalOut = 0, totalCost = 0, totalQuotaDelta = 0;
  const bodyRows = rows.map(r => {
    // Claude: raw=new-only, cr=cached separately → effective = raw+cr.
    // Codex:  raw=total-including-cache, cr=breakdown → use raw only.
    const raw  = r.input_tokens || 0;
    const cr   = r.cache_read_tokens || 0;
    const inp  = (cr > 0 && raw < cr) ? raw + cr : raw;
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
    // Claude: raw=new-only, cr=cached separately → effective = raw+cr.
    // Codex:  raw=total-including-cache, cr=breakdown → use raw only.
    const raw  = r.input_tokens  || 0;
    const cr   = r.cache_read_tokens || 0;
    const inp  = (cr > 0 && raw < cr) ? raw + cr : raw;
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

function renderAgentStats(rows) {
  let totalSessions = 0, totalIn = 0, totalOut = 0, totalCost = 0;
  const bodyRows = rows.map(r => {
    // Claude: raw=new-only, cr=cached separately → effective = raw+cr.
    // Codex:  raw=total-including-cache, cr=breakdown → use raw only.
    const raw  = r.input_tokens || 0;
    const cr   = r.cache_read_tokens || 0;
    const inp  = (cr > 0 && raw < cr) ? raw + cr : raw;
    const out  = r.output_tokens || 0;
    const cost = r.cost_usd || 0;
    totalSessions += r.sessions || 0;
    totalIn  += inp;
    totalOut += out;
    totalCost += cost;
    return `<tr>
      <td>${r.agent}</td>
      <td>${r.sessions}</td>
      <td>${fmtNum(inp)}</td>
      <td>${fmtNum(out)}</td>
      <td>$${cost.toFixed(4)}</td>
    </tr>`;
  }).join('');

  statsContent.innerHTML = `<table>
    <thead><tr>
      <th>Agent</th><th>Sessions</th><th>Tokens In</th><th>Tokens Out</th><th>Cost</th>
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

// ── agent manager ─────────────────────────────────────────────────────────────

async function loadAgents() {
  const listEl = document.getElementById('agents-list');
  listEl.innerHTML = '<div class="empty">Loading…</div>';
  let agents;
  try {
    const res = await fetch('/config/agents');
    agents = await res.json();
  } catch {
    listEl.innerHTML = '<div class="empty">Failed to load.</div>';
    return;
  }
  if (!agents.length) {
    listEl.innerHTML = '<div class="empty">No agents yet. Add one below.</div>';
    return;
  }
  const rows = agents.map(a => `
    <tr>
      <td><span class="agent-name">${a.name}</span></td>
      <td>${a.backend}</td>
      <td class="col-model">${a.model || '<span class="col-default">—</span>'}</td>
      <td>${a.cwd || '<span class="col-default">/tmp/squid</span>'}</td>
      <td class="col-timeout">${a.timeout ? a.timeout + 's' : '<span class="col-default">30m</span>'}</td>
      <td>
        <button class="del-btn" data-name="${a.name}" title="Delete agent (does not affect existing messages)">✕</button>
      </td>
    </tr>`).join('');
  listEl.innerHTML = `<table>
    <thead><tr><th>Name</th><th>Backend</th><th class="col-model">Model</th><th>CWD</th><th class="col-timeout">Timeout</th><th></th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;

  listEl.querySelectorAll('.del-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      await fetch(`/config/agents/${btn.dataset.name}`, { method: 'DELETE' });
      _agentsCache = null;
      loadAgents();
    });
  });
}

function initAliases() {
  const statusEl = document.getElementById('agent-form-status');
  document.getElementById('agent-form').addEventListener('submit', async (e) => {
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

    // Warn if key attributes changed on an existing agent with active sessions
    const existing = (_agentsCache || []).find(a => a.name === body.name);
    if (existing) {
      const keyChanged = existing.backend !== body.backend ||
                         (existing.model || null) !== body.model ||
                         (existing.cwd || null) !== body.cwd;
      if (keyChanged) {
        const sessions = await fetch(`/config/agents/${encodeURIComponent(body.name)}/sessions`).then(r => r.ok ? r.json() : null).catch(() => null);
        const activeTopics = sessions?.topics?.map(s => s.topic) ?? [];
        if (activeTopics.length > 0) {
          const topicList = activeTopics.join(', ');
          const ok = confirm(`Changing backend, model, or cwd for "${body.name}" will clear active sessions in: ${topicList}.\n\nContinue?`);
          if (!ok) return;
        }
      }
    }

    try {
      const res = await fetch('/config/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        const data = await res.json();
        const cleared = data.sessions_cleared || [];
        statusEl.textContent = cleared.length ? `saved ✓ (cleared sessions: ${cleared.join(', ')})` : 'saved ✓';
        document.getElementById('af-name').value    = '';
        document.getElementById('af-model').value   = '';
        document.getElementById('af-cwd').value     = '';
        document.getElementById('af-timeout').value = '';
        _agentsCache = null;
        loadAgents();
      } else {
        statusEl.textContent = 'failed';
      }
    } catch { statusEl.textContent = 'error'; }
    setTimeout(() => { statusEl.textContent = ''; }, 5000);
  });
}

// ── inline agent creation prompt ─────────────────────────────────────────────

function showAgentCreatePrompt(agentName, onSaved) {
  const existing = document.getElementById('agent-create-prompt');
  if (existing) existing.remove();

  const prompt = document.createElement('div');
  prompt.id = 'agent-create-prompt';
  prompt.className = 'agent-create-prompt';
  prompt.innerHTML = `
    <div class="acp-title">Agent <strong>${agentName}</strong> not found — create it?</div>
    <div class="acp-row">
      <select id="acp-backend">
        <option value="claude">claude</option>
        <option value="cursor">cursor</option>
        <option value="antigravity">antigravity</option>
        <option value="codex">codex</option>
        <option value="copilot">copilot</option>
      </select>
      <input id="acp-model" placeholder="model (optional)" />
      <input id="acp-cwd" placeholder="cwd (default: /tmp/squid)" />
    </div>
    <div class="acp-actions">
      <button id="acp-save">Create &amp; send</button>
      <button id="acp-cancel">Cancel</button>
    </div>`;

  messages.appendChild(prompt);
  messages.scrollTop = messages.scrollHeight;

  prompt.querySelector('#acp-cancel').addEventListener('click', () => prompt.remove());
  prompt.querySelector('#acp-save').addEventListener('click', async () => {
    const backend = prompt.querySelector('#acp-backend').value;
    const model   = prompt.querySelector('#acp-model').value.trim() || null;
    const cwd     = prompt.querySelector('#acp-cwd').value.trim()   || null;
    const res = await fetch('/config/agents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: agentName, backend, model, cwd }),
    });
    if (res.ok) {
      _agentsCache = null;  // invalidate autocomplete cache
      prompt.remove();
      onSaved();
    } else {
      prompt.querySelector('.acp-title').textContent = 'Failed to create agent.';
    }
  });
}

// ── topic / agent autocomplete ───────────────────────────────────────────────

let _topicsCache  = null;
let _agentsCache = null;
let acOpen  = false;
let acItems = [];
let acSel   = -1;

function invalidateTopicsCache() { _topicsCache = null; }

async function _acTopics() {
  if (_topicsCache) return _topicsCache;
  try { _topicsCache = await (await fetch('/topics')).json(); } catch { _topicsCache = []; }
  return _topicsCache;
}

async function _acAgents() {
  if (_agentsCache) return _agentsCache;
  try { _agentsCache = await (await fetch('/config/agents')).json(); } catch { _agentsCache = []; }
  return _agentsCache;
}

function hideAutocomplete() {
  acEl.classList.remove('open');
  acOpen = false; acItems = []; acSel = -1;
}

function _acHighlight() {
  acEl.querySelectorAll('.ac-item').forEach((el, i) => el.classList.toggle('selected', i === acSel));
  if (acSel >= 0) acEl.children[acSel]?.scrollIntoView({ block: 'nearest' });
}

function _acRender(items) {
  if (!items.length) { hideAutocomplete(); return; }
  acItems = items; acSel = -1;
  acEl.innerHTML = items.map((item, i) =>
    `<div class="ac-item" data-i="${i}"${item.execute != null ? ' data-cmd' : ''}>` +
    `<div class="ac-row"><span class="ac-label">${item.label}</span>` +
    (item.sub ? `<span class="ac-sub">${item.sub}</span>` : '') +
    (item.meta ? `<span class="ac-meta">${item.meta}</span>` : '') +
    (item.deleteTopic ? `<button class="ac-del-btn" data-topic="${item.deleteTopic}" type="button" title="Delete #${item.deleteTopic} sessions">✕</button>` : '') +
    `</div>` +
    `</div>`
  ).join('');
  acEl.querySelectorAll('.ac-item').forEach((el, i) =>
    el.addEventListener('mousedown', e => {
      if (e.target.classList.contains('ac-del-btn')) return;
      e.preventDefault(); _acSelect(i);
    })
  );
  acEl.querySelectorAll('.ac-del-btn').forEach(btn =>
    btn.addEventListener('mousedown', async e => {
      e.preventDefault(); e.stopPropagation();
      const name = btn.dataset.topic;
      await fetch(`/topics/${encodeURIComponent(name)}/hide`, { method: 'POST' });
      invalidateTopicsCache();
      acItems = acItems.filter(it => it.deleteTopic !== name);
      btn.closest('.ac-item').remove();
      if (!acEl.querySelector('.ac-item')) hideAutocomplete();
    })
  );
  acEl.classList.add('open');
  acOpen = true;
}

function _acSelect(idx) {
  if (idx < 0 || idx >= acItems.length) return;
  const item = acItems[idx];
  hideAutocomplete();
  if (item.execute) {
    input.value = item.insert;
    resizeComposer();
    form.requestSubmit();
    return;
  }
  input.value = item.insert + ' ';
  resizeComposer();
  input.focus();
}

function _acTopicLabel(topicName, modelLabel) {
  return `<span class="ac-topic">#${topicName}</span>` +
         (modelLabel ? `<span class="ac-agent">@${modelLabel}</span>` : '');
}

function _acAgentLabel(topicName, agentName) {
  return `<span class="ac-topic">#${topicName}</span><span class="ac-agent">@${agentName}</span>`;
}

async function updateAutocomplete() {
  const val = input.value;

  // Command popup: message portion starts with /
  const { message: msgPart } = parseInput(val);
  if (msgPart.startsWith('/')) {
    const slashIdx = val.lastIndexOf('/');
    const before   = val.slice(0, slashIdx);           // prefix to preserve (#topic@alias )
    const partial  = msgPart.slice(1).toLowerCase();   // typed after /
    const matched  = SQUID_COMMANDS.filter(c => c.name.toLowerCase().startsWith(partial));
    if (matched.length) {
      _acRender(matched.map(c => ({
        label:   `<span class="ac-cmd">/${c.name}</span>`,
        sub:     c.desc,
        meta:    'squid',
        insert:  before + '/' + c.name,
        execute: !c.args,
      })));
    } else {
      hideAutocomplete();
    }
    return;
  }

  const mTopic = val.match(/^#(\w*)[!]?$/);
  const mAlias = val.match(/^#(\w+)@(\w*)[!]?$/);
  if (mTopic) {
    const prefix = mTopic[1].toLowerCase();
    const topics = await _acTopics();
    if (input.value !== val) return;
    _acRender(
      topics.filter(t => t.name.toLowerCase().startsWith(prefix)).slice(0, 8)
        .map(t => ({
          label:       _acTopicLabel(t.name, t.last_model || t.last_backend || ''),
          insert:      '#' + t.name,
          deleteTopic: t.name,
          meta:        t.active ? '● live' : t.queue_depth > 0 ? `queue ${t.queue_depth}` : '',
          sub:         t.last_prompt ? truncate(t.last_prompt, 55) : '',
        }))
    );
  } else if (mAlias) {
    const topic  = mAlias[1];
    const prefix = mAlias[2].toLowerCase();
    const [agents, history] = await Promise.all([
      _acAgents(),
      fetch(`/topics/${encodeURIComponent(topic)}/agents/history`).then(r => r.ok ? r.json() : []).catch(() => []),
    ]);
    if (input.value !== val) return;

    const usedNames = new Set(history.map(h => h.agent));
    const items = [];

    // Used agents — with last prompt
    for (const h of history) {
      if (!h.agent.toLowerCase().startsWith(prefix)) continue;
      items.push({
        label:  _acAgentLabel(topic, h.agent),
        insert: `#${topic}@${h.agent}`,
        sub:    h.last_prompt ? truncate(h.last_prompt, 55) : '',
      });
      // Also offer adhoc variant
      items.push({
        label:  _acAgentLabel(topic, h.agent + '!'),
        insert: `#${topic}@${h.agent}!`,
        sub:    h.last_prompt ? truncate(h.last_prompt, 55) : '',
        meta:   'adhoc',
      });
    }

    // Other available agents — no prompt
    for (const a of agents) {
      if (usedNames.has(a.name)) continue;
      if (!a.name.toLowerCase().startsWith(prefix)) continue;
      items.push({
        label:  _acAgentLabel(topic, a.name),
        insert: `#${topic}@${a.name}`,
        meta:   a.model || a.backend,
      });
    }

    _acRender(items.slice(0, 10));
  } else {
    hideAutocomplete();
  }
}

// ── pull-to-refresh (bottom overscroll) ──────────────────────────────────────

function initPullToRefresh() {
  let startY = 0;
  let startedAtBottom = false;
  const GESTURE_ZONE = 64; // px from bottom edge reserved for Android nav gesture

  messages.addEventListener('touchstart', (e) => {
    const y = e.touches[0].clientY;
    // Ignore touches starting in the Android system gesture zone
    if (y > window.innerHeight - GESTURE_ZONE) return;
    startY = y;
    startedAtBottom = isAtBottom();
  }, { passive: true });

  messages.addEventListener('touchend', (e) => {
    if (!startedAtBottom || !startY) return;
    const dy = startY - e.changedTouches[0].clientY; // positive = finger moved up
    if (dy > 240) setTimeout(() => location.reload(), 150);
    startY = 0;
  }, { passive: true });
}

// ── boot banner ──────────────────────────────────────────────────────────────

async function showBootBanner() {
  try {
    const res = await fetch('/health');
    if (!res.ok) return;
    const data = await res.json();
    const bootTime = data.boot_time ? fmtTime(data.boot_time) : '';
    const art = `\
    🦑 AGENT
    ██████╗ ██████╗ ██╗   ██╗██╗██████╗
   ██╔════╝██╔═══██╗██║   ██║██║██╔══██╗
   ╚█████╗ ██║   ██║██║   ██║██║██║  ██║
    ╚═══██╗██║▄▄ ██║██║   ██║██║██║  ██║
   ██████╔╝╚██████╔╝╚██████╔╝██║██████╔╝
   ╚═════╝  ╚══▀▀═╝  ╚═════╝ ╚═╝╚═════╝`;
    const el = document.createElement('div');
    el.className = 'boot-banner';
    el.innerHTML = `<pre class="boot-art">${art}</pre>` +
      `<div class="boot-meta">agent squid${bootTime ? `  ·  started ${bootTime}` : ''}</div>`;
    messages.appendChild(el);
    messages.scrollTop = messages.scrollHeight;
  } catch {}
}

// ── ctx popup ─────────────────────────────────────────────────────────────────

function showCtxPopup(spanEl) {
  let popup = document.getElementById('ctx-popup');
  if (!popup) {
    popup = document.createElement('div');
    popup.id = 'ctx-popup';
    document.getElementById('app').appendChild(popup);
  }
  // Toggle off if already showing for this span
  if (popup.dataset.forSpan === String(spanEl) && popup.classList.contains('open')) {
    popup.classList.remove('open');
    return;
  }
  popup.dataset.forSpan = String(spanEl);

  const sid    = spanEl.dataset.sessionId || '';
  const cwd    = spanEl.dataset.cwd || '';
  const pinIds = JSON.parse(spanEl.dataset.pinnedIds || '[]');
  const pins   = getPinnedItems().filter(i => pinIds.includes(i.id));

  let html = '';
  if (sid || cwd) {
    html += `<div class="ctx-popup-row"><span class="ctx-popup-key">session</span><span class="ctx-popup-val">${sid}</span></div>`;
    if (cwd) html += `<div class="ctx-popup-row"><span class="ctx-popup-key">cwd</span><span class="ctx-popup-val">${cwd}</span></div>`;
  }
  if (pins.length) {
    if (html) html += `<div class="ctx-popup-divider"></div>`;
    html += `<div class="ctx-popup-row"><span class="ctx-popup-key">bookmarked</span></div>`;
    pins.forEach(item => {
      html += `<div class="ctx-popup-pin">
        <span class="ctx-popup-tag">${_pinTagStr(item)}</span>
        <span class="ctx-popup-preview">${(item.content || '').replace(/</g,'&lt;').slice(0,70)}</span>
      </div>`;
    });
  }
  if (!html) html = `<div class="ctx-popup-row"><span class="ctx-popup-key">${spanEl.textContent.trim()}</span></div>`;

  popup.innerHTML = html;
  popup.classList.add('open');

  // Position above the span
  const rect = spanEl.getBoundingClientRect();
  const appRect = document.getElementById('app').getBoundingClientRect();
  popup.style.bottom = (appRect.bottom - rect.top + 6) + 'px';
  popup.style.right  = (appRect.right  - rect.right + 0) + 'px';
}

// ── pin basket ────────────────────────────────────────────────────────────────

function getPinnedItems() {
  try { return JSON.parse(localStorage.getItem('pinnedItems') || '[]'); } catch { return []; }
}
function setPinnedItems(items) { localStorage.setItem('pinnedItems', JSON.stringify(items)); }
function getInjectedInto() {
  try { return JSON.parse(localStorage.getItem('injectedInto') || '{}'); } catch { return {}; }
}
function setInjectedInto(map) { localStorage.setItem('injectedInto', JSON.stringify(map)); }

function updatePinCount() {
  const n = getPinnedItems().length;
  pinCountEl.textContent = n || '';
  pinCountEl.classList.toggle('visible', n > 0);
  pinBtn.classList.toggle('has-pins', n > 0);
}

function _pinTagStr(item) {
  return item.agent ? `#${item.topic}@${item.agent}` : `#${item.topic}`;
}

function _pinStatus(item) {
  const injected  = getInjectedInto();
  const parsed    = parseInput(input.value);
  const chipTopic = parsed.topic || stickyChip?.topic || 'default';
  const isAdhoc   = parsed.adhoc || (stickyChip?.adhoc ?? false);

  // Resolve effective agent: explicit in input > stickyChip > topics cache (sticky agent)
  let chipAgent = parsed.agent || stickyChip?.agent || null;
  if (!chipAgent) {
    if (_topicsCache) {
      chipAgent = _topicsCache.find(t => t.name === chipTopic)?.agent || null;
    } else {
      // Cache cold — load async and re-render panel when ready
      _acTopics().then(() => { if (pinPanel.classList.contains('open')) renderPinPanel(); });
    }
  }

  const chipTaKey = `${chipTopic}@${chipAgent || '_'}`;
  const currentSid = _sessionIds[chipTaKey] || null;
  const sameSession = item.session_id && currentSid && item.session_id === currentSid;

  // Skip only if the bookmark is from the exact current session — --resume already covers it
  if (sameSession && !isAdhoc) {
    const qual = chipAgent ? ` · #${chipTopic}@${chipAgent}` : '';
    return { text: `in session${qual} · skip`, cls: 'pin-status-session' };
  }

  // Already injected into this topic@agent via a previous adhoc turn
  const taKey = `${chipTopic}@${chipAgent || '_'}`;
  if ((injected[taKey] || []).includes(item.id))
    return { text: 'already added · skip', cls: 'pin-status-done' };

  return { text: 'will inject', cls: 'pin-status-inject' };
}

function renderPinPanel() {
  const items = getPinnedItems();
  const listEl = document.getElementById('pin-panel-list');
  let html = '';

  // Lookback section — shown when !N is active in input
  const { adhoc, lookback } = parseInput(input.value);
  if (ctxHighlightEnabled && adhoc && lookback > 0) {
    const asstItems = [...document.querySelectorAll('#messages .history-item.msg.assistant')];
    const lbItems = asstItems.slice(-lookback);
    if (lbItems.length) {
      html += `<div class="pin-section-label">Lookback · last ${lookback}</div>`;
      lbItems.forEach(el => {
        const btn = el.querySelector('.msg-pin-btn');
        const preview = (el.querySelector(':scope > div:nth-child(2)')?.innerText || '').slice(0, 80);
        html += `<div class="pin-item pin-item-lookback">
          <span class="pin-item-preview">${preview.replace(/</g,'&lt;')}</span>
          <span class="pin-item-status pin-status-inject">will inject</span>
        </div>`;
      });
    }
  }

  if (items.length) {
    if (html) html += `<div class="pin-section-label">Bookmarked</div>`;
    html += items.map(item => {
      const st = _pinStatus(item);
      const tag = _pinTagStr(item);
      const preview = (item.content || '').replace(/</g, '&lt;').slice(0, 90);
      return `<div class="pin-item">
        <span class="pin-item-tag">${tag}</span>
        <span class="pin-item-preview">${preview}</span>
        <span class="pin-item-status ${st.cls}">${st.text}</span>
        <button class="pin-item-remove" data-id="${item.id}" type="button">✕</button>
      </div>`;
    }).join('');
  }

  if (!html) {
    html = '<div style="padding:0.5rem 0.8rem;color:#484858;font-size:0.78em">No bookmarks yet.<br>Click 🔖 on any response to add it.</div>';
  }

  listEl.innerHTML = html;
  listEl.querySelectorAll('.pin-item-remove').forEach(btn => {
    btn.addEventListener('mousedown', e => {
      e.preventDefault();
      const id = parseInt(btn.dataset.id);
      setPinnedItems(getPinnedItems().filter(i => i.id !== id));
      document.querySelectorAll(`.msg-pin-btn[data-msg-id="${id}"]`)
        .forEach(b => b.classList.remove('pinned'));
      updatePinCount();
      renderPinPanel();
    });
  });
}

function openPinPanel() {
  renderPinPanel();
  pinPanel.classList.add('open');
}
function closePinPanel() {
  pinPanel.classList.remove('open');
}

function addPinButton(bubbleEl, msgId, topic, agent, sessionId = null) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'msg-pin-btn';
  btn.dataset.msgId = String(msgId);
  btn.title = 'Pin as context';
  btn.innerHTML = `<svg width="10" height="12" viewBox="0 0 12 14" fill="currentColor" aria-hidden="true">
    <path d="M2 0h8a1 1 0 0 1 1 1v12.8l-5-2.9-5 2.9V1a1 1 0 0 1 1-1z"/>
  </svg>`;
  if (sessionId) bubbleEl.dataset.sessionId = sessionId;
  if (getPinnedItems().find(i => i.id === msgId)) btn.classList.add('pinned');
  btn.addEventListener('click', e => {
    e.stopPropagation();
    const pinned = getPinnedItems();
    if (pinned.find(i => i.id === msgId)) {
      setPinnedItems(pinned.filter(i => i.id !== msgId));
      btn.classList.remove('pinned');
    } else {
      const contentEl = bubbleEl.querySelector(':scope > div:nth-child(2)');
      const text = (contentEl?.innerText || '').slice(0, 300);
      const sid = bubbleEl.dataset.sessionId || null;
      setPinnedItems([...pinned, { id: msgId, topic, agent: agent || null, session_id: sid, content: text }]);
      btn.classList.add('pinned');
    }
    updatePinCount();
    if (pinPanel.classList.contains('open')) renderPinPanel();
  });
  bubbleEl.appendChild(btn);
}

function initPin() {
  pinBtn.addEventListener('click', () => {
    if (pinPanel.classList.contains('open')) closePinPanel();
    else openPinPanel();
  });
  document.getElementById('pin-panel-close').addEventListener('click', closePinPanel);
  updatePinCount();
}

// ── init ─────────────────────────────────────────────────────────────────────

initSettings();
initPin();
document.getElementById('filter-badge-clear').addEventListener('click', clearFilter);
document.addEventListener('click', e => {
  if (!acEl.contains(e.target) && e.target !== input) hideAutocomplete();
  if (!pinPanel.contains(e.target) && !pinBtn.contains(e.target)) closePinPanel();
  const ctxPopup = document.getElementById('ctx-popup');
  if (ctxPopup && !ctxPopup.contains(e.target) && !e.target.closest('.user-ctx')) {
    ctxPopup.classList.remove('open');
  }
  if (!procStatusPopup.contains(e.target) && e.target !== procStatusBtn && !procStatusBtn.contains(e.target)) {
    procStatusPopup.classList.remove('open');
  }
});
initHistoryScroll();
initStats();
initAliases();
initQuota();
initCreds();
initCodexQuota();
initCodexCreds();
initPullToRefresh();
startProcPoll();
showBootBanner();
