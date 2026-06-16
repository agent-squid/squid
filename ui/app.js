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

const BACKEND_MODEL_HINTS = Object.freeze({
  claude: 'e.g. claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-7',
  codex:  'e.g. o4-mini, o3',
  cursor: 'model (optional)',
});

const AGENT_THEME_COLORS = Object.freeze({
  claude: '#AE5332',
  codex: '#e8e4dc',
  cursor: '#9aa0a6',
  antigravity: '#4ea1ff',
  copilot: '#ff5db1',
  default: '#888888',
});

function agentThemeColor(backend) {
  return AGENT_THEME_COLORS[(backend || '').toLowerCase()] || AGENT_THEME_COLORS.default;
}

function agentSlugColor(agent, backendFallback = null) {
  const config = (_agentsCache || []).find(a => a.name === agent);
  return agentThemeColor(config?.backend || backendFallback || agent);
}

function setAgentSlugColor(el, agent, backendFallback = null) {
  el.dataset.agentName = agent || '';
  if (backendFallback) el.dataset.backendFallback = backendFallback;
  el.style.setProperty('--agent-color', agentSlugColor(agent, backendFallback));
  if (agent && !backendFallback && !_agentsCache) _acAgents().catch(() => {});
}

function refreshAgentSlugColors() {
  document.querySelectorAll('[data-agent-name]').forEach(el => {
    setAgentSlugColor(el, el.dataset.agentName, el.dataset.backendFallback || null);
  });
}

function quotaGaugeColor(backend) {
  return agentThemeColor(backend);
}

function backendDisplayName(backend) {
  const names = {
    claude: 'Claude',
    codex: 'Codex',
    cursor: 'Cursor',
    antigravity: 'Antigravity',
    copilot: 'Copilot',
  };
  return names[(backend || '').toLowerCase()] || (backend || 'Agent');
}

// Rewrite file:// links and images to /localfile?path= so local paths are served.
(function () {
  function extractLine(path) {
    const m = path.match(/:(\d+)(?:-(\d+))?(?::\d+)?$/);
    if (!m) return { line: null, endLine: null };
    return { line: parseInt(m[1], 10), endLine: m[2] ? parseInt(m[2], 10) : null };
  }

  function stripLineSuffix(path) {
    return path.replace(/:\d+(?:-\d+)?(?::\d+)?$/, '');
  }

  function isLocalFilePath(path) {
    return /^(\/|~\/)/.test(path) && /\.\w{1,16}$/.test(path);
  }

  function localFileUrl(path, line, endLine) {
    const params = new URLSearchParams({ path });
    const token = localStorage.getItem('squid_token');
    if (token) params.set('token', token);
    const base = '/localfile?' + params.toString();
    if (!line) return base;
    return base + '#L' + line + (endLine && endLine !== line ? '-L' + endLine : '');
  }

  function fileToLocal(url) {
    if (!url) return url;
    let rawPath = url;
    if (url.startsWith('file://')) rawPath = decodeURIComponent(url.replace(/^file:\/\//, ''));
    const { line, endLine } = extractLine(rawPath);
    const path = stripLineSuffix(rawPath);
    if (isLocalFilePath(path)) return localFileUrl(path, line, endLine);
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
  if (name === 'topics') loadTopicsView();
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
const _memoryInjectedInto = {}; // `${topic}@${agent|_}` → topic memory already sent to the current session
let _agentsCache = null;
let _agentsCachePromise = null;

function clearCachedSessionId(topic, agent) {
  const taKey = `${topic}@${agent || '_'}`;
  const sid = _sessionIds[taKey];
  if (sid) {
    const inj = getInjectedInto();
    delete inj[sid];
    setInjectedInto(inj);
  }
  delete _sessionIds[taKey];
  delete _memoryInjectedInto[taKey];
  delete _sessionLookupCache[taKey];
  if (agent) delete _sessionLookupCache[`${topic}@${agent}`];
}

// ── topic chip ────────────────────────────────────────────────────────────────

const topicChipEl = document.getElementById('topic-chip');
let stickyChip = null; // { topic, agent, adhoc } | null

function setTopicChip(topic, agent, adhoc = false, lookback = 0) {
  stickyChip = { topic, agent, adhoc, lookback };
  localStorage.setItem('squid_sticky_chip', JSON.stringify({ topic, agent, adhoc }));

  topicChipEl.innerHTML = '';
  const tSpan = document.createElement('span');
  tSpan.className = 'chip-topic';
  tSpan.textContent = '#' + topic;
  topicChipEl.appendChild(tSpan);
  if (agent) {
    const aSpan = document.createElement('span');
    aSpan.className = 'chip-agent';
    aSpan.textContent = '@' + agent;
    setAgentSlugColor(aSpan, agent);
    topicChipEl.appendChild(aSpan);
  }
  if (adhoc) {
    const adSpan = document.createElement('span');
    adSpan.className = 'chip-adhoc';
    adSpan.textContent = lookback > 0 ? `!${lookback}` : '!';
    if (agent) setAgentSlugColor(adSpan, agent);
    topicChipEl.appendChild(adSpan);
  }
  topicChipEl.classList.add('visible');
  topicChipEl.classList.remove('needs-agent');
  input.placeholder = 'message…';
  updateActiveQuotaGauge();
  updatePinCount();
  updateInContextMarkers();
}

function clearTopicChip() {
  stickyChip = null;
  localStorage.removeItem('squid_sticky_chip');
  topicChipEl.classList.remove('visible', 'needs-agent');
  input.placeholder = '#topic or #topic@agent message…';
  updateActiveQuotaGauge();
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
    return { topic: ma[1].toLowerCase(), agent: ma[2] || null, adhoc: true, lookback: ma[3] ? Math.min(parseInt(ma[3]), 20) : 0, message: ma[4].trim() };
  }
  // session: #topic or #topic@agent
  const ms = text.match(/^#(\w+)(?:@(\w+))?\s+([\s\S]*)$/);
  if (ms && ms[3].trim()) {
    return { topic: ms[1].toLowerCase(), agent: ms[2] || null, adhoc: false, lookback: 0, message: ms[3].trim() };
  }
  // bare topic switch: #topic or #topic@agent with no message — switches chip only
  const mb = text.match(/^#(\w+)(?:@(\w+))?(!)?$/);
  if (mb) {
    return { topic: mb[1].toLowerCase(), agent: mb[2] || null, adhoc: !!mb[3], lookback: 0, message: '' };
  }
  return { topic: 'default', agent: null, adhoc: false, lookback: 0, message: text };
}

// ── topic tag helper (colored, clickable) ──────────────────────────────────────

function makeTopicTag(topic, agent, { clickable = false, adhoc = false, lookback = 0, backend = null } = {}) {
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
    setAgentSlugColor(aSpan, agent, backend);
    wrap.appendChild(aSpan);
  }

  if (adhoc) {
    const adSpan = document.createElement('span');
    adSpan.className = 'tag-adhoc';
    adSpan.textContent = '!' + (lookback > 0 ? lookback : '');
    if (agent) setAgentSlugColor(adSpan, agent, backend);
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
  if (searchActive && searchState) {
    searchState = { ...searchState, topic: null, agent: null, adhoc: null };
    searchLoading = false;
    document.querySelectorAll('.search-result-item').forEach(el => el.remove());
    document.querySelectorAll('#messages > .cmd-feedback.search-no-results').forEach(el => el.remove());
    _updateFilterBadge();
    loadSearchResults();
    return;
  }
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
  const { topic, agent, adhoc } = (searchActive && searchState) ? searchState : historyFilter;

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
    setAgentSlugColor(a, agent);
    labelEl.appendChild(a);
  }
  if (adhoc) { // only show '!' for adhoc — 'sess' was removed because it concatenated visually with the agent name
    const ad = document.createElement('span');
    ad.className = 'tag-adhoc';
    ad.textContent = '!';
    if (agent) setAgentSlugColor(ad, agent);
    labelEl.appendChild(ad);
  }
  badge.classList.add('active');
}

// ── history pagination (display) ─────────────────────────────────────────────

let historyOffset = 0;
let historyExhausted = false;
let historyLoading = false;
let topSentinel = null;

// ── search state ──────────────────────────────────────────────────────────────
let searchActive = false;
let searchState = null;  // { topic, agent, adhoc, keywords }
let searchLoading = false;

let promptHistory = [];   // newest first, in-memory, seeded from DB
let promptHistoryPos = -1; // -1 = editing draft; 0..N = navigating history
let promptDraft = '';      // stashed current input while navigating
let _draftSaveTimer = null;

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
  updateInContextMarkers();
  refreshAllRevertButtons();

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

// ── keyword search ────────────────────────────────────────────────────────────

function parseSearchInput(text) {
  let rest = text.trim();
  let topic = null;
  let agent = null;
  let agentWildcard = false;
  let adhocAll = false;

  // #topic[@agent[*]][!] keywords
  const topicMatch = rest.match(/^#(\w+)(?:@(\w+)(\*)?)?(!)?[ \t]*([\s\S]*)/);
  if (topicMatch) {
    topic = topicMatch[1].toLowerCase();
    agent = topicMatch[2] || null;
    agentWildcard = !!topicMatch[3];
    adhocAll = !!topicMatch[4];
    rest = topicMatch[5].trim();
  } else {
    // @agent[*][!] keywords — agent scope across all topics
    const agentMatch = rest.match(/^@(\w+)(\*)?(!)?[ \t]+([\s\S]*)/);
    if (agentMatch) {
      topic = 'all';
      agent = agentMatch[1];
      agentWildcard = !!agentMatch[2];
      adhocAll = !!agentMatch[3];
      rest = agentMatch[4].trim();
    }
  }

  return { topic, agent, agentWildcard, adhocAll, keywords: rest };
}

function _updateSearchBar() {
  const bar = document.getElementById('search-bar');
  const kwEl = document.getElementById('search-bar-keywords');

  if (!searchActive || !searchState) {
    bar.classList.remove('active');
    return;
  }

  kwEl.textContent = searchState.keywords;
  bar.classList.add('active');
  _updateFilterBadge();
}

function highlightTextNodes(root, keywords) {
  if (!keywords.length) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const p = node.parentElement;
      if (!p) return NodeFilter.FILTER_REJECT;
      if (p.closest('code, pre, script, style')) return NodeFilter.FILTER_REJECT;
      if (p.closest('.response-header, .history-prompt-full, .user-ctx')) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const text = node.textContent;
    let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    let replaced = false;
    for (const kw of keywords) {
      const re = new RegExp(`(${kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
      const next = html.replace(re, '<mark class="search-kw-highlight">$1</mark>');
      if (next !== html) { html = next; replaced = true; }
    }
    if (replaced) {
      const span = document.createElement('span');
      span.innerHTML = html;
      node.parentNode.replaceChild(span, node);
    }
  }
}

function startSearch(rawArgs) {
  const parsed = parseSearchInput(rawArgs);

  if (!parsed.keywords) {
    showCmdFeedback('Usage: /s [#topic[@agent[*]][!]] keywords…');
    return;
  }

  let topic, agent, adhoc;
  if (parsed.topic !== null) {
    // explicit scope typed in command overrides the active filter
    topic = parsed.topic === 'all' ? null : parsed.topic;
    agent = parsed.agent ? (parsed.agent + (parsed.agentWildcard ? '*' : '')) : null;
    adhoc = parsed.adhocAll ? true : null;  // no ! → no adhoc filter; ! → adhoc-only
  } else if (historyFilter.topic || historyFilter.agent) {
    // active history filter (set by /filter or tag click)
    topic = historyFilter.topic || null;
    agent = historyFilter.agent || null;
    adhoc = historyFilter.adhoc ?? null;
  } else {
    // fall back to sticky chip (current chat context)
    topic = stickyChip?.topic || null;
    agent = stickyChip?.agent || null;
    adhoc = stickyChip?.adhoc ? true : false;
  }

  searchState = { topic, agent, adhoc, keywords: parsed.keywords };
  searchActive = true;
  searchLoading = false;

  // Stop history scroll
  if (topSentinel) { topSentinel.remove(); topSentinel = null; }
  historyLoading = false;

  // Clear pane
  document.querySelectorAll('.history-item, .boot-banner, .search-result-item').forEach(el => el.remove());
  document.querySelectorAll('#messages > .msg:not(.msg-thinking), #messages > .msg-thinking-done, #messages > .msg-time, #messages > .stats, #messages > .cmd-feedback').forEach(el => el.remove());

  _updateSearchBar();
  loadSearchResults();
}

function clearSearch() {
  searchActive = false;
  searchState = null;
  searchLoading = false;
  document.getElementById('search-bar').classList.remove('active');

  document.querySelectorAll('.search-result-item').forEach(el => el.remove());
  document.querySelectorAll('#messages > .cmd-feedback.search-no-results').forEach(el => el.remove());
  if (topSentinel) { topSentinel.remove(); topSentinel = null; }
  historyOffset = 0;
  historyExhausted = false;
  historyLoading = false;
  _updateFilterBadge();
  initHistoryScroll();
}

function recordPrompt(text) {
  const t = text.trim();
  if (!t) return;
  promptHistory = [t, ...promptHistory.filter(x => x !== t)].slice(0, 200);
  promptHistoryPos = -1;
  promptDraft = '';
}

async function initPromptHistory() {
  const draft = localStorage.getItem('squid_draft');
  if (draft) { input.value = draft; resizeComposer(); }
  try {
    const res = await fetch('/prompts/recent?limit=50');
    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data.items)) promptHistory = data.items;
  } catch { /* ignore */ }
}

async function loadSearchResults() {
  if (searchLoading || !searchState) return;
  searchLoading = true;

  let url = `/search?limit=100&q=${encodeURIComponent(searchState.keywords)}`;
  if (searchState.topic) url += `&topic=${encodeURIComponent(searchState.topic)}`;
  if (searchState.agent) url += `&agent=${encodeURIComponent(searchState.agent)}`;
  if (searchState.adhoc !== null && searchState.adhoc !== undefined) url += `&adhoc=${searchState.adhoc}`;

  let data;
  try {
    const res = await fetch(url);
    if (!res.ok) { searchLoading = false; return; }
    data = await res.json();
  } catch {
    searchLoading = false;
    return;
  }

  const { items } = data;
  if (!Array.isArray(items)) { searchLoading = false; return; }

  const kws = searchState.keywords.trim().split(/\s+/).filter(Boolean);
  const fragment = document.createDocumentFragment();
  for (const item of [...items].reverse()) {
    if (!item.content) continue;
    const el = appendHistoryItem(item, fragment);
    if (el) highlightTextNodes(el, kws);
  }
  [...fragment.children].forEach(el => el.classList.add('search-result-item'));
  messages.appendChild(fragment);
  searchLoading = false;

  if (items.length === 0) {
    const noResult = document.createElement('div');
    noResult.className = 'cmd-feedback search-no-results';
    noResult.textContent = `No results for "${searchState.keywords}"`;
    messages.appendChild(noResult);
  } else if (items.length >= 100) {
    const cap = document.createElement('div');
    cap.className = 'cmd-feedback search-no-results';
    cap.textContent = 'Showing top 100 results — add keywords to narrow.';
    messages.appendChild(cap);
  }
  messages.scrollTop = messages.scrollHeight;
}

// ── live chat ────────────────────────────────────────────────────────────────

// All Squid-owned commands. Shown in the / autocomplete popup.
// args:true = takes optional args (insert into input); args:false = execute directly on select.
const SQUID_COMMANDS = [
  { name: 'clear',        desc: 'clear session — next message starts fresh',         args: false },
  { name: 'compact',      desc: 'compact session (resets context for Codex)',         args: false },
  { name: 'stop',         desc: 'kill running process for current topic',             args: false },
  { name: 'stopall',      desc: 'kill + drain queue for current topic',               args: false },
  { name: 'deq',          desc: 'drain queue (deq N removes Nth item)',               args: true  },
  { name: 'restart',      desc: 'restart the server',                                 args: false },
  { name: 'filter',       desc: 'filter history by current topic or agent',           args: false },
  { name: 'filter reset', desc: 'clear the active filter',                            args: false },
  { name: 's',            desc: 'search: /s [#topic[@agent[*]][!]] keywords…',        args: true  },
  { name: 'search',       desc: 'search history (alias for /s)',                      args: true  },
  { name: 'status',       desc: 'show active processes panel',                        args: false },
  { name: 'help',         desc: 'show help panel',                                    args: false },
  { name: 'remote',       desc: 'show QR code for mobile / tablet access',            args: false },
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
  const ms = t.match(/^s(?:earch)?(?:\s+([\s\S]*))?$/i);
  if (ms) return { command: 'search', args: (ms[1] || '').trim() };
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

  if (cmd.command === 'search') {
    if (!cmd.args) {
      showCmdFeedback('Usage: /s [#topic[@agent[*]][!]] keywords…');
      return;
    }
    startSearch(cmd.args);
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
      clearCachedSessionId(topic, data.agent || agent || null);
      if (pinPanel.classList.contains('open')) renderPinPanel();
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
  if (!message) {
    input.value = '';
    resizeComposer();
    hideAutocomplete();
    setTopicChip(topic, agent, adhoc, lookback);
    return;
  }
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
  if (searchActive) clearSearch();
  invalidateTopicsCache();
  recordPrompt(text);
  localStorage.removeItem('squid_draft');
  sendMessage(text);
});

function fmtCtxLabel(adhoc, pinCount = 0, mem = false) {
  const parts = [adhoc ? 'adhoc' : 'sess'];
  if (mem) parts.push('mem');
  if (pinCount > 0) parts.push(`${pinCount} pin${pinCount !== 1 ? 's' : ''}`);
  return parts.join(' · ');
}

const _lookbackUnselected = new Set(); // cleared on N change or after send; never persisted
let _lastLookbackN = 0;

function _allLookbackItems(adhoc, lookback) {
  if (!adhoc || lookback <= 0) return [];
  return [...document.querySelectorAll('#messages .history-item.assistant')]
    .filter(el => el.dataset.msgId)
    .map(el => {
      const id = parseInt(el.dataset.msgId);
      const contentEl = el.querySelector(':scope > div:nth-child(2)');
      return { id, el, topic: el.dataset.topic || 'default', agent: el.dataset.agent || null, session_id: el.dataset.sessionId || null, content: contentEl?.innerText || '' };
    })
    .sort((a, b) => a.id - b.id)
    .slice(-lookback);
}

function _activeLookbackItems(adhoc, lookback) {
  return _allLookbackItems(adhoc, lookback).filter(item => !_lookbackUnselected.has(item.id));
}

function updateInContextMarkers() {
  const { topic, agent, adhoc, lookback } = _currentContextTarget();

  if (lookback !== _lastLookbackN) {
    _lookbackUnselected.clear();
    _lastLookbackN = lookback;
    // Strip all stale lookback-sel classes before re-applying the new selection
    document.querySelectorAll('.msg-pin-btn.lookback-sel')
      .forEach(btn => btn.classList.remove('lookback-sel'));
  }

  const taKey   = `${topic}@${agent || '_'}`;
  const sid     = (!adhoc && agent) ? (_sessionIds[taKey] || null) : null;
  const injected = sid ? (getInjectedInto()[sid] || []) : [];

  const activeItems = adhoc && lookback > 0 ? _activeLookbackItems(adhoc, lookback) : [];
  const activeIdSet = new Set(activeItems.map(i => i.id));

  document.querySelectorAll('#messages .history-item.assistant').forEach(el => {
    const ctxSpan = el.querySelector('.user-ctx');
    const msgId = el.dataset.msgId ? parseInt(el.dataset.msgId) : null;
    const wasInjected = msgId && injected.includes(msgId);

    // Orange dot = already in context (session continuity or prior injection), not "will inject"
    let inCtx = false;
    if (!adhoc) {
      inCtx = !!(sid && el.dataset.sessionId === sid);
    }
    inCtx = inCtx || !!wasInjected;

    if (ctxSpan) {
      ctxSpan.classList.toggle('ctx-live', inCtx);
      ctxSpan.classList.remove('ctx-injected');
    }

    // Reflect lookback selection state on the pin button
    const pinBtn = el.querySelector('.msg-pin-btn');
    if (pinBtn) pinBtn.classList.toggle('lookback-sel', !!(adhoc && activeIdSet.has(msgId)));
  });

  // Clear lookback-sel on any bubbles no longer in active set (e.g. after N shrinks)
  document.querySelectorAll('.msg-pin-btn.lookback-sel').forEach(btn => {
    const id = parseInt(btn.dataset.msgId);
    if (!activeIdSet.has(id)) btn.classList.remove('lookback-sel');
  });
}

async function _maybePromoteSlug(val) {
  const m = val.match(/^#(\w+)(?:@(\w+))?(!\d*)? $/);
  if (!m) return;
  const topic = m[1].toLowerCase();
  const explicitAgent = m[2] || null;
  const adhocStr = m[3] || null;
  const adhoc = !!adhocStr;
  const lookback = adhocStr ? parseInt(adhocStr.slice(1)) || 0 : 0;

  let agent = explicitAgent;
  let resolvedAdhoc = adhoc;
  if (!agent) {
    const topics = await _acTopics();
    if (input.value !== val) return;
    const topicData = topics.find(t => t.name === topic);
    agent = topicData?.agent || null;
    if (!adhocStr) resolvedAdhoc = !!(topicData?.sticky_adhoc);
  }

  setTopicChip(topic, agent, resolvedAdhoc, lookback);
  input.value = '';
  hideAutocomplete();
  resizeComposer();
  updatePinCount();
}

input.addEventListener('input', () => {
  resizeComposer();
  updateAutocomplete();
  updateInContextMarkers();
  updatePinCount();
  updateActiveQuotaGauge();
  _maybePromoteSlug(input.value);
  clearTimeout(_draftSaveTimer);
  _draftSaveTimer = setTimeout(() => localStorage.setItem('squid_draft', input.value), 300);
  if (promptHistoryPos !== -1 && input.value !== promptHistory[promptHistoryPos]) {
    promptHistoryPos = -1;
  }
});

input.addEventListener('keydown', (e) => {
  if (acOpen) {
    if (e.key === 'ArrowDown') { e.preventDefault(); acSel = Math.min(acSel + 1, acItems.length - 1); _acHighlight(); return; }
    if (e.key === 'ArrowUp')   { e.preventDefault(); acSel = Math.max(acSel - 1, -1); _acHighlight(); return; }
    if (e.key === 'Tab' || (e.key === 'Enter' && acSel >= 0)) { e.preventDefault(); _acSelect(acSel >= 0 ? acSel : 0); return; }
    if (e.key === 'Escape') { hideAutocomplete(); return; }
  }
  if (!acOpen && e.key === 'ArrowUp' && promptHistory.length && !input.value.includes('\n')) {
    e.preventDefault();
    if (promptHistoryPos === -1) promptDraft = input.value;
    promptHistoryPos = Math.min(promptHistoryPos + 1, promptHistory.length - 1);
    input.value = promptHistory[promptHistoryPos];
    resizeComposer();
    return;
  }
  if (!acOpen && e.key === 'ArrowDown' && promptHistoryPos >= 0) {
    e.preventDefault();
    promptHistoryPos--;
    input.value = promptHistoryPos >= 0 ? promptHistory[promptHistoryPos] : promptDraft;
    resizeComposer();
    return;
  }
  if (e.key === 'Escape' && searchActive) { clearSearch(); return; }
  if (e.key === 'Escape' && pinPanel.classList.contains('open')) { closePinPanel(); return; }
  if (e.key === 'Escape' && helpPanel.classList.contains('open')) { closeHelp(); return; }
  if (e.key === 'Escape' && document.getElementById('msg-modal')?.classList.contains('open')) { document.getElementById('msg-modal').classList.remove('open'); return; }
  if (e.key === 'Escape' && document.getElementById('memory-modal')?.classList.contains('open')) { closeMemoryEditor(); return; }
  if (e.key === 'Escape' && document.getElementById('topic-delete-modal')?.classList.contains('open')) { closeTopicDeleteModal(); return; }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
  if (e.key === 'Backspace' && input.value === '' && stickyChip) {
    e.preventDefault();
    let tag = `#${stickyChip.topic}`;
    if (stickyChip.agent) tag += `@${stickyChip.agent}`;
    if (stickyChip.adhoc) tag += `!${stickyChip.lookback || ''}`;
    clearTopicChip();
    input.value = tag;
    input.dispatchEvent(new Event('input'));
  }
});

async function sendMessage(text) {
  const { topic, agent, adhoc, lookback, message } = parseInput(text);
  setTopicChip(topic, agent, adhoc, lookback);
  const sendTime = new Date().toISOString();

  const userBubble = makeUserBubble(message, topic, agent, null, adhoc, lookback);
  const userTopicTag = userBubble.querySelector('.topic-tag');
  messages.appendChild(userBubble);
  addTimestamp(userBubble, sendTime, true);
  requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });

  // Non-blocking nudge — fires async after the message is already in flight
  maybeShowCodeRootsNudge(topic, userBubble);

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
  let userAborted = false;

  // Kill button — shown once msg_id is known, hidden when done
  const killBtn = document.createElement('button');
  killBtn.type = 'button';
  killBtn.className = 'thinking-kill-btn';
  killBtn.title = 'Stop this process';
  killBtn.textContent = '×';
  killBtn.style.display = 'none';
  killBtn.addEventListener('click', async () => {
    killBtn.disabled = true;
    userAborted = true;
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
  bubble.dataset.topic = topic;
  if (agent) bubble.dataset.agent = agent;
  const responseHeader = document.createElement('div');
  responseHeader.className = 'response-header';
  const responseHeaderTag = makeTopicTag(topic, agent, { adhoc, lookback });
  const headerText = document.createElement('span');
  headerText.className = 'response-header-text';
  headerText.appendChild(responseHeaderTag);
  headerText.appendChild(document.createTextNode('  ' + truncate(message, 55)));
  responseHeader.appendChild(headerText);
  const liveCtxSpan = document.createElement('span');
  liveCtxSpan.className = 'user-ctx';
  liveCtxSpan.textContent = 'ctx:' + fmtCtxLabel(adhoc);
  liveCtxSpan.dataset.topic = topic;
  liveCtxSpan.addEventListener('click', e => { e.stopPropagation(); showCtxPopup(liveCtxSpan); });
  responseHeader.appendChild(liveCtxSpan);
  bubble.appendChild(responseHeader);
  const contentDiv = document.createElement('div');
  bubble.appendChild(contentDiv);

  let firstDataReceived = false;
  let quotaBackend = await resolveQuotaBackend(topic, agent);
  let quotaBeforeSnapshot = await fetchQuotaForBackend(quotaBackend);
  quotaTrackStart(quotaBackend);
  let lastSessionId = null;
  let statsEl = null;
  let doneTime = null;
  let msgId = null;
  let statusTimer = null;
  let completedFromStatus = false;
  let detachedPolling = false;
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
          freezeThinking();
          showStoredResponse(data.content || '');
          if (!statsEl) addTimestamp(bubble, doneTime, false);
          controller.abort();
        } else if (data.status === 'error') {
          completedFromStatus = true;
          stopStatusFallback();
          freezeThinking();
          showError(data.content || 'Response interrupted.');
          controller.abort();
        }
      } catch {}
    }, 2000);
  }

  // Compute pinned IDs to inject — works for both session and adhoc turns
  let _effectiveAgent = agent || stickyChip?.agent || null;
  if (!_effectiveAgent) {
    try {
      const topics = await _acTopics();
      _effectiveAgent = topics.find(t => t.name === topic)?.agent || null;
    } catch {}
  }
  const _taKey = `${topic}@${_effectiveAgent || '_'}`;
  const _injected = getInjectedInto();
  const _currentSid = _sessionIds[_taKey] || null;
  const _includeTopicMemory = (await _topicMemoryStateForSend(topic, _effectiveAgent, adhoc)).selected;
  const _lookbackItems = _activeLookbackItems(adhoc, lookback);
  const _lookbackIds = _lookbackItems.map(item => item.id);
  const _pinnedIds = getPinnedItems()
    .filter(item => {
      // Skip pins from the current session — --resume already has that context
      const sameSession = item.session_id && _currentSid && item.session_id === _currentSid;
      if (sameSession && !adhoc) return false;
      // Fresh adhoc turn (no lookback) — no accumulated context, always inject
      if (adhoc && lookback === 0) return true;
      // Skip already-injected items (keyed by session_id for cross-device correctness)
      if (_currentSid && (_injected[_currentSid] || []).includes(item.id)) return false;
      return true;
    })
    .map(item => item.id);
  const _contextIds = [...new Set([..._lookbackIds, ..._pinnedIds])];

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message, topic, agent, lookback, adhoc,
        ...(adhoc && lookback > 0 ? { lookback_via_pins: true } : {}),
        ...(_includeTopicMemory ? { include_topic_memory: true } : {}),
        ...(_contextIds.length ? { pinned_ids: _contextIds } : {}),
      }),
      // For UI sends, !N is resolved into explicit pinned_ids from the current list.
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
    _lookbackUnselected.clear();
    _lastLookbackN = 0;
    if (_includeTopicMemory && !adhoc) {
      const memoryKey = _memoryInjectedKey(topic, _effectiveAgent);
      _memoryInjectedInto[memoryKey] = true;
      _memorySelectionOverrides[_memoryOverrideKey(topic, _effectiveAgent, false)] = false;
      updatePinCount();
      if (pinPanel.classList.contains('open')) renderPinPanel();
    }

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
              if (meta.backend === 'claude' || meta.backend === 'codex') {
                quotaBackend = meta.backend;
                if (quotaBeforeSnapshot?.backend && quotaBeforeSnapshot.backend !== quotaBackend) {
                  quotaBeforeSnapshot = null;
                }
              }
              const resolvedAdhoc = adhoc; // server echoes back what we sent; use closure as reliable source
              const newTag = makeTopicTag(topic, resolvedAgent, { adhoc: resolvedAdhoc, clickable: true, lookback, backend: meta.backend || null });
              responseHeaderTag.replaceWith(newTag);
              const newUserTag = makeTopicTag(topic, resolvedAgent, { adhoc: resolvedAdhoc, clickable: true, lookback, backend: meta.backend || null });
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
              if (meta.msg_id) {
                msgId = meta.msg_id;
                bubble.dataset.msgId = String(msgId);
                bubble.dataset.topic = topic;
                if (resolvedAgent) bubble.dataset.agent = resolvedAgent;
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
              liveCtxSpan.textContent = 'ctx:' + fmtCtxLabel(!!stats.adhoc);
              if (stats.session_id) liveCtxSpan.dataset.sessionId = stats.session_id;
              if (stats.cwd) liveCtxSpan.dataset.cwd = stats.cwd;
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
              const diffTools = changeTools(liveToolEvents);
              for (const tool of diffTools) {
                const block = makeToolBlock(tool, msgId);
                block.classList.add('tool-block-history');
                messages.appendChild(block);
              }
              refreshAllRevertButtons();
              scrollToBottom();
            }
            // Update ctx label with pin count and store IDs for popup
            liveCtxSpan.textContent = 'ctx:' + fmtCtxLabel(adhoc, _contextIds.length, _includeTopicMemory);
            liveCtxSpan.dataset.pinnedIds = JSON.stringify(_contextIds);
            liveCtxSpan.dataset.mem = _includeTopicMemory ? 'true' : 'false';
            // Record injected pinned IDs keyed by session_id for cross-device correctness
            // Skip recording for lookback=0 adhoc turns — each is a fresh context, pins always re-inject
            if (_contextIds.length) {
              if (!(adhoc && lookback === 0) && lastSessionId) {
                const _inj = getInjectedInto();
                _inj[lastSessionId] = [...new Set([...(_inj[lastSessionId] || []), ..._contextIds])];
                setInjectedInto(_inj);
              }
              if (pinPanel.classList.contains('open')) renderPinPanel();
            }
            updateInContextMarkers();
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
            else if (raw.length && data.length && /[.!?]$/.test(raw) && /^[A-Z]/.test(data)) raw += ' ';
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
        detachedPolling = true;
        statusBuf += (statusBuf ? '\n' : '') + 'Still running — waiting for saved response…';
        raw = '';
        updateThinkingPreview();
        startStatusFallback(msgId);
      } else {
        showError('Unable to start response stream.');
      }
    }
  } finally {
    if (!detachedPolling) stopStatusFallback();
    if (!thinkingFrozen) {
      if (!detachedPolling) {
        if (!userAborted && msgId && !firstDataReceived && !completedFromStatus) {
          const content = document.createElement('span');
          content.className = 'msg-error';
          content.textContent = 'Response is still running. Reopen the page or history to pick it up.';
          if (!bubble.parentNode) messages.appendChild(bubble);
          contentDiv.appendChild(content);
        } else {
          freezeThinking();
        }
      }
    }
    if (!detachedPolling && !userAborted && !firstDataReceived && !completedFromStatus) {
      if (!bubble.parentNode) messages.appendChild(bubble);
      contentDiv.innerHTML = '<span class="msg-error">No response — backend may be rate-limited or unavailable.</span>';
    }
    if (!statsEl && doneTime && firstDataReceived) addTimestamp(bubble, doneTime, false);
  }

  // Quota snapshot — wait briefly for provider APIs to reflect the just-completed turn.
  await new Promise(r => setTimeout(r, 3000));
  const hasQuotaBefore = quotaBeforeSnapshot?.backend === quotaBackend && quotaBeforeSnapshot.raw !== null;
  const quotaAfterSnapshot = await fetchQuotaForBackend(quotaBackend, { trackDelta: hasQuotaBefore });
  const quotaBefore = quotaBeforeSnapshot?.raw ?? null;
  const quotaAfter = quotaAfterSnapshot?.raw ?? null;
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
  if (lastSessionId && quotaBefore !== null && quotaAfter !== null) {
    fetch('/stats/quota-delta', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: lastSessionId, before: quotaBefore, after: quotaAfter }),
    }).catch(() => {});
  }
  quotaTrackEnd(quotaBackend);
}

// ── tooltip ───────────────────────────────────────────────────────────────────
const _ttEl = document.getElementById('app-tooltip');
let _ttAnchor = null;

function _ttPosition(anchor) {
  const r = anchor.getBoundingClientRect();
  const tw = _ttEl.offsetWidth, th = _ttEl.offsetHeight;
  const GAP = 8, MARGIN = 10;
  const top = r.top - th - GAP >= MARGIN ? r.top - th - GAP : r.bottom + GAP;
  const left = Math.max(MARGIN, Math.min(r.left + r.width / 2 - tw / 2, window.innerWidth - tw - MARGIN));
  _ttEl.style.top = top + 'px';
  _ttEl.style.left = left + 'px';
}

function showTooltip(anchor, text) {
  _ttAnchor = anchor;
  _ttEl.textContent = text;
  _ttEl.classList.add('show');
  _ttPosition(anchor);
}

function hideTooltip() {
  _ttEl.classList.remove('show');
  _ttAnchor = null;
}

document.addEventListener('mouseover', e => {
  const el = e.target.closest('[data-tooltip]');
  if (el) showTooltip(el, el.dataset.tooltip);
});
document.addEventListener('mouseout', e => {
  const el = e.target.closest('[data-tooltip]');
  if (el && !el.contains(e.relatedTarget)) hideTooltip();
});
document.addEventListener('touchstart', e => {
  const el = e.target.closest('[data-tooltip]');
  if (el) {
    if (_ttAnchor === el) { hideTooltip(); return; }
    e.preventDefault();
    showTooltip(el, el.dataset.tooltip);
  } else if (_ttAnchor) {
    hideTooltip();
  }
}, { passive: false });

// ── helpers ──────────────────────────────────────────────────────────────────

function toolLabel(tool) {
  const name = tool.name || '';
  if (name === 'GitDiff') {
    const n = tool.file_count ?? (tool.files || []).length;
    return `Changed files: ${n} file${n !== 1 ? 's' : ''}`;
  }
  if (name === 'Read' || name === 'Edit' || name === 'Write' || name === 'MultiEdit' || name === 'Diff')
    return `${name}: ${tool.file || ''}`;
  if (name === 'Bash') return `Bash: ${truncate(tool.command || '', 70)}`;
  if (name === 'Agent') return `Agent: ${truncate(tool.description || '', 70)}`;
  if (name === 'WebFetch' || name === 'WebSearch') return `${name}: ${truncate(tool.query || '', 70)}`;
  if (name === 'TodoWrite') { const n = (tool.todos || []).length; return `TodoWrite: ${n} item${n !== 1 ? 's' : ''}`; }
  return name + (tool.key ? ': ' + truncate(tool.value || '', 50) : '');
}

function changeTools(tools) {
  const gitTools = tools.filter(t => t.name === 'GitDiff');
  if (gitTools.length) return gitTools;
  return tools.filter(t => t.name === 'Edit' || t.name === 'Write' || t.name === 'MultiEdit' || t.name === 'Diff');
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

function splitUnifiedDiff(diff) {
  const map = new Map();
  if (!diff) return map;
  const lines = diff.split('\n');
  let path = null, chunk = [];
  for (const line of lines) {
    if (line.startsWith('diff --git ')) {
      if (path !== null) map.set(path, chunk.join('\n'));
      const m = line.match(/^diff --git a\/.+ b\/(.+)$/);
      path = m ? m[1] : line;
      chunk = [line];
    } else if (path !== null) {
      chunk.push(line);
    }
  }
  if (path !== null) map.set(path, chunk.join('\n'));
  return map;
}

function _countDiffStats(chunk) {
  let add = 0, del = 0;
  for (const line of chunk.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) add++;
    else if (line.startsWith('-') && !line.startsWith('---')) del++;
  }
  return { add, del };
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

function makeToolBlock(tool, msgId) {
  const name = tool.name || '';
  const block = document.createElement('div');
  block.className = 'tool-block';

  const hasDiff = name === 'Edit' || name === 'MultiEdit' || name === 'Write' || name === 'Diff' || name === 'GitDiff';
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
  if (name !== 'GitDiff') body.appendChild(scroll);

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
  } else if (name === 'GitDiff') {
    const count = tool.file_count ?? (tool.files || []).length;
    const additions = tool.additions ?? 0;
    const deletions = tool.deletions ?? 0;
    toggle.textContent = `Changed files: ${count} file${count !== 1 ? 's' : ''}, +${additions} -${deletions}`;

    if (msgId && tool.repo) {
      block.dataset.msgId = String(msgId);
      block.dataset.repo = tool.repo;
      const revertBar = document.createElement('div');
      revertBar.className = 'gitdiff-revert-bar';
      body.appendChild(revertBar);
    }

    const fileDiffs = splitUnifiedDiff(tool.diff || '');
    for (const file of (tool.files || [])) {
      const status = file.status || '?';
      const displayPath = file.old_path ? `${file.old_path} → ${file.path}` : file.path;
      const chunk = fileDiffs.get(file.path) || fileDiffs.get(file.old_path) || '';

      const row = document.createElement('div');
      row.className = 'gitdiff-file-row';
      if (msgId && tool.repo) row.dataset.file = file.path;

      const fileToggle = document.createElement('button');
      fileToggle.className = 'gitdiff-file-toggle';

      if (chunk) {
        const { add, del } = _countDiffStats(chunk);
        fileToggle.textContent = `${status} ${displayPath}  +${add} -${del}`;
        const fileBody = document.createElement('div');
        fileBody.className = 'gitdiff-file-body';
        const fileScroll = document.createElement('div');
        fileScroll.className = 'diff-scroll';
        renderUnifiedDiffLines(fileScroll, chunk);
        fileBody.appendChild(fileScroll);
        row.appendChild(fileToggle);
        row.appendChild(fileBody);
        fileToggle.addEventListener('click', () => row.classList.toggle('gitdiff-file-expanded'));
      } else {
        fileToggle.textContent = `${status} ${displayPath}`;
        fileToggle.classList.add('gitdiff-file-toggle--no-diff');
        row.appendChild(fileToggle);
      }

      const _absPath = file.path
        ? (file.path.startsWith('/') ? file.path : tool.repo ? tool.repo + '/' + file.path : null)
        : null;
      if (status !== 'D' && _absPath && _isTextPath(_absPath)) {
        const openBtn = document.createElement('button');
        openBtn.className = 'gitdiff-file-open';
        openBtn.title = 'Open file';
        openBtn.textContent = '↗';
        openBtn.addEventListener('click', e => {
          e.stopPropagation();
          openFileViewer(_absPath);
        });
        row.appendChild(openBtn);
      }

      body.appendChild(row);
    }

    if (tool.truncated) {
      const el = document.createElement('span');
      el.className = 'diff-line diff-hunk';
      el.style.display = 'block';
      el.textContent = '[diff truncated]';
      body.appendChild(el);
    }

    block.classList.add('tool-expanded');
  }

  toggle.addEventListener('click', () => block.classList.toggle('tool-expanded'));
  block.appendChild(toggle);
  block.appendChild(body);
  return block;
}

async function _doRevert(msgId, repo, filePath) {
  const body = filePath ? { repo, file_path: filePath } : { repo };
  const res = await fetch(`/chat/${msgId}/revert`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return res.json();
}

async function fetchRevertEligibility(block) {
  const msgId = block.dataset.msgId;
  const repo = block.dataset.repo;
  if (!msgId || !repo) return;

  let eligibility;
  try {
    const res = await fetch(`/chat/${msgId}/diff-revert-status?repo=${encodeURIComponent(repo)}`);
    if (!res.ok) return;
    eligibility = await res.json();
  } catch { return; }

  // Update per-file rows
  for (const row of block.querySelectorAll('.gitdiff-file-row[data-file]')) {
    const fpath = row.dataset.file;
    const status = eligibility[fpath];
    row.querySelector('.gitdiff-revert-btn')?.remove();
    row.classList.toggle('gitdiff-file-row--reverted', status === 'reverted');

    if (status === 'revertable') {
      const btn = document.createElement('button');
      btn.className = 'gitdiff-revert-btn';
      btn.textContent = 'revert';
      btn.addEventListener('click', async e => {
        e.stopPropagation();
        btn.disabled = true; btn.textContent = '…';
        try {
          const data = await _doRevert(msgId, repo, fpath);
          if (data.ok && data.reverted?.length) {
            refreshAllRevertButtons();
          } else {
            btn.disabled = false; btn.textContent = 'revert';
            btn.title = data.failed?.[0]?.error || data.error || 'failed';
          }
        } catch { btn.disabled = false; btn.textContent = 'revert'; }
      });
      row.appendChild(btn);
    }
  }

  // Update collection-level revert bar
  const bar = block.querySelector('.gitdiff-revert-bar');
  if (!bar) return;
  bar.innerHTML = '';
  const revertableFiles = Object.entries(eligibility).filter(([, s]) => s === 'revertable');
  if (revertableFiles.length > 1) {
    const btn = document.createElement('button');
    btn.className = 'gitdiff-revert-all-btn';
    btn.textContent = `Revert all ${revertableFiles.length} files`;
    btn.addEventListener('click', async e => {
      e.stopPropagation();
      btn.disabled = true; btn.textContent = '…';
      try {
        const data = await _doRevert(msgId, repo, null);
        if (data.ok) { refreshAllRevertButtons(); }
        else { btn.disabled = false; btn.textContent = `Revert all`; btn.title = data.error || 'failed'; }
      } catch { btn.disabled = false; }
    });
    bar.appendChild(btn);
  }
}

function refreshAllRevertButtons() {
  document.querySelectorAll('.tool-block[data-msg-id][data-repo]').forEach(fetchRevertEligibility);
}


function appendHistoryItem(item, container) {
  const lb = item.stats?.lookback ?? 0;
  const asstBubble = document.createElement('div');
  asstBubble.className = 'msg assistant history-item';
  if (item.id) asstBubble.dataset.msgId = String(item.id);
  asstBubble.dataset.topic = item.topic || 'default';
  if (item.agent) asstBubble.dataset.agent = item.agent;
  if (item.session_id) asstBubble.dataset.sessionId = item.session_id;

  const asstHeader = document.createElement('div');
  asstHeader.className = 'response-header';
  const asstLabel = item.agent || item.backend;
  const asstTag = makeTopicTag(item.topic || 'default', asstLabel, { clickable: true, adhoc: !!item.adhoc, lookback: lb, backend: item.backend || null });
  const asstHeaderText = document.createElement('span');
  asstHeaderText.className = 'response-header-text';
  asstHeaderText.appendChild(asstTag);
  asstHeaderText.appendChild(document.createTextNode('  '));
  const promptSpan = document.createElement('span');
  promptSpan.className = 'history-prompt';
  promptSpan.textContent = truncate(item.prompt || '', 55);
  promptSpan.dataset.full = item.prompt || '';
  const promptFullDiv = document.createElement('div');
  promptFullDiv.className = 'history-prompt-full';
  promptFullDiv.textContent = item.prompt || '';
  const togglePrompt = () => {
    const expanded = promptSpan.classList.toggle('expanded');
    promptFullDiv.classList.toggle('visible', expanded);
  };
  promptSpan.addEventListener('click', togglePrompt);
  promptFullDiv.addEventListener('click', togglePrompt);
  asstHeaderText.appendChild(promptSpan);
  asstHeader.appendChild(asstHeaderText);

  const _pc = (() => {
    try {
      const v = JSON.parse(item.prompt_context || 'null');
      if (!v) return { pins: [], mem: false };
      if (Array.isArray(v)) return { pins: v, mem: false };
      return { pins: v.pins || [], mem: !!v.mem };
    } catch { return { pins: [], mem: false }; }
  })();
  const ctxSpan = document.createElement('span');
  ctxSpan.className = 'user-ctx';
  ctxSpan.textContent = 'ctx:' + fmtCtxLabel(!!item.adhoc, _pc.pins.length, _pc.mem);
  ctxSpan.dataset.sessionId = item.session_id || '';
  ctxSpan.dataset.cwd = item.stats?.cwd || '';
  ctxSpan.dataset.topic = item.topic || '';
  ctxSpan.dataset.pinnedIds = JSON.stringify(_pc.pins);
  ctxSpan.dataset.mem = _pc.mem ? 'true' : 'false';
  ctxSpan.addEventListener('click', e => { e.stopPropagation(); showCtxPopup(ctxSpan); });
  asstHeader.appendChild(ctxSpan);

  asstBubble.appendChild(asstHeader);
  asstBubble.appendChild(promptFullDiv);

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
      const diffTools = changeTools(tools);
      let lastEl = asstBubble;
      for (const tool of diffTools) {
        const block = makeToolBlock(tool, item.id);
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
const QUOTA_BACKENDS = ['claude', 'codex', 'cursor'];

// Per-backend config — add an entry here to support a new quota backend.
const QUOTA_CONFIG = {
  claude: {
    endpoint:     '/quota/claude',
    displayId:    'quota-display',
    pieArcId:     'quota-pie-arc',
    labelId:      'quota-label',
    pieC:         2 * Math.PI * 6,
    credsPopupId: 'quota-creds-popup',
    errorTitle:   'Claude usage unavailable · click for credentials',
    parse:        parseClaudeQuota,
  },
  codex: {
    endpoint:     '/quota/codex',
    displayId:    'codex-quota-display',
    pieArcId:     'codex-pie-arc',
    labelId:      'codex-quota-label',
    pieC:         2 * Math.PI * 6,
    credsPopupId: 'codex-creds-popup',
    errorTitle:   'Codex usage unavailable · click for credentials',
    parse:        parseCodexQuota,
  },
  cursor: {
    endpoint:     '/quota/cursor',
    displayId:    'cursor-quota-display',
    pieArcId:     'cursor-pie-arc',
    labelId:      'cursor-quota-label',
    pieC:         2 * Math.PI * 6,
    credsPopupId: null,
    errorTitle:   'Cursor usage unavailable — run cursor-agent to log in',
    parse:        parseCursorQuota,
  },
};

const quotaSnapshots = {
  claude: { backend: 'claude', status: 'unknown' },
  codex:  { backend: 'codex',  status: 'unknown' },
  cursor: { backend: 'cursor', status: 'unknown' },
};
// Per-backend runtime state. timer is the label-refresh interval handle.
// activeCount tracks in-flight messages; drives the 30s quota poll interval.
const quotaState = {
  claude: { raw: null, pct: null, resetAt: null, delta: null, inFlight: false, timer: null, activeCount: 0 },
  codex:  { raw: null, pct: null, resetAt: null, delta: null, inFlight: false, timer: null, activeCount: 0 },
  cursor: { raw: null, pct: null, resetAt: null, delta: null, inFlight: false, timer: null, activeCount: 0 },
};

let activeQuotaBackend = null;
let quotaResolveSeq = 0;
let quotaPollInterval = null;

function _startQuotaPoll() {
  if (quotaPollInterval) return;
  quotaPollInterval = setInterval(() => {
    for (const [backend, state] of Object.entries(quotaState)) {
      if (state.activeCount > 0) fetchQuotaForBackend(backend);
    }
  }, 30000);
}

function _stopQuotaPoll() {
  if (Object.values(quotaState).some(s => s.activeCount > 0)) return;
  clearInterval(quotaPollInterval);
  quotaPollInterval = null;
}

function quotaTrackStart(backend) {
  if (!quotaState[backend]) return;
  quotaState[backend].activeCount++;
  _startQuotaPoll();
}

function quotaTrackEnd(backend) {
  if (!quotaState[backend]) return;
  quotaState[backend].activeCount = Math.max(0, quotaState[backend].activeCount - 1);
  _stopQuotaPoll();
}

function setQuotaSnapshot(backend, snapshot) {
  quotaSnapshots[backend] = { backend, ...snapshot };
  if (procStatusPopup?.classList.contains('open')) {
    renderProcPopup(cachedProcRows, cachedQueueRows);
  }
}

function quotaTimeText(resetAt) {
  if (!resetAt) return '';
  const diff = resetAt - Date.now();
  if (diff <= 0) return 'resetting';
  const totalMin = Math.floor(diff / 60000);
  const h = Math.floor(totalMin / 60);
  const m = String(totalMin % 60).padStart(2, '0');
  return h > 0 ? `${h}:${m}` : `${m}m`;
}

function setVisibleQuotaBackend(backend) {
  activeQuotaBackend = (backend in QUOTA_CONFIG) ? backend : null;
  for (const [b, cfg] of Object.entries(QUOTA_CONFIG)) {
    document.getElementById(cfg.displayId)?.classList.toggle('quota-hidden', activeQuotaBackend !== b);
  }
}

async function resolveActiveQuotaBackend() {
  const parsed = parseInput(input.value.trim());
  const topicName = parsed.topic || stickyChip?.topic || null;
  let agentName = parsed.agent || stickyChip?.agent || null;

  return resolveQuotaBackend(topicName, agentName);
}

async function resolveQuotaBackend(topicName, agentName) {
  if (!agentName && topicName) {
    const topics = await _acTopics();
    agentName = topics.find(t => t.name === topicName)?.agent || null;
  }
  if (!agentName) return 'claude';

  const agents = await _acAgents();
  return agents.find(a => a.name === agentName)?.backend || null;
}

async function updateActiveQuotaGauge() {
  const seq = ++quotaResolveSeq;
  const backend = await resolveActiveQuotaBackend();
  if (seq !== quotaResolveSeq) return;
  setVisibleQuotaBackend(backend);
}

function parseClaudeQuota(data) {
  const session = data?.five_hour;
  if (!session) return null;
  const raw = session.utilization ?? 0;
  return {
    raw,
    pct: Math.round(raw),
    resetAt: new Date(session.resets_at).getTime(),
    title: 'Claude session usage',
  };
}

function parseCodexQuota(data) {
  const primary = data?.rate_limit?.primary_window;
  if (!primary) return null;
  const raw = primary.used_percent ?? 0;
  const resetAt = primary.reset_after_seconds != null
    ? Date.now() + primary.reset_after_seconds * 1000
    : (primary.reset_at != null ? primary.reset_at * 1000 : null);
  return {
    raw,
    pct: Math.max(0, Math.min(100, Math.round(raw))),
    resetAt,
    title: buildCodexQuotaTitle(data),
  };
}

function updateGaugeLabel(backend) {
  const cfg = QUOTA_CONFIG[backend];
  const state = quotaState[backend];
  if (!cfg || state.pct == null) return;
  const label = document.getElementById(cfg.labelId);
  if (!label) return;
  const delta = state.delta != null ? ` +${state.delta}%` : '';
  const timeStr = quotaTimeText(state.resetAt);
  label.textContent = `${state.pct}%${delta}` + (timeStr ? ` in ${timeStr}` : '');
  const arc = document.getElementById(cfg.pieArcId);
  if (arc) {
    const filled = (state.pct / 100) * cfg.pieC;
    arc.setAttribute('stroke-dasharray', `${filled} ${cfg.pieC}`);
    arc.setAttribute('stroke', quotaGaugeColor(backend, state.pct));
  }
}

function renderQuotaLoaded(backend, snapshot) {
  const cfg = QUOTA_CONFIG[backend];
  const state = quotaState[backend];
  state.raw = snapshot.raw;
  state.pct = snapshot.pct;
  state.resetAt = snapshot.resetAt;

  const displayEl = document.getElementById(cfg.displayId);
  displayEl.classList.remove('error');
  displayEl.classList.add('loaded');
  displayEl.title = snapshot.title ?? '';
  updateGaugeLabel(backend);
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(() => updateGaugeLabel(backend), 10000);

  setQuotaSnapshot(backend, {
    status: 'loaded',
    pct: snapshot.pct,
    resetAt: snapshot.resetAt,
    title: snapshot.title,
  });
}

function showQuotaError(backend, text) {
  const cfg = QUOTA_CONFIG[backend];
  const state = quotaState[backend];
  state.resetAt = null;
  if (state.timer) { clearInterval(state.timer); state.timer = null; }

  const displayEl = document.getElementById(cfg.displayId);
  displayEl.classList.remove('loaded');
  displayEl.classList.add('error');
  displayEl.title = cfg.errorTitle;
  setQuotaSnapshot(backend, { status: 'error', text });

  const label = document.getElementById(cfg.labelId);
  if (label) label.textContent = text;
  const arc = document.getElementById(cfg.pieArcId);
  if (arc) arc.setAttribute('stroke-dasharray', `0 ${cfg.pieC}`);
}

async function fetchQuotaForBackend(backend, { trackDelta = false } = {}) {
  const cfg = QUOTA_CONFIG[backend];
  if (!cfg) return null;
  const state = quotaState[backend];
  if (state.inFlight) return state.raw == null ? null : { backend, ...state };
  state.inFlight = true;
  const label = backend[0].toUpperCase() + backend.slice(1);
  try {
    const res = await fetch(cfg.endpoint);
    if (!res.ok) {
      showQuotaError(backend, res.status === 400 ? `${label} auth` : `${label} error`);
      return null;
    }
    const data = await res.json();
    const snapshot = cfg.parse(data);
    if (!snapshot) {
      showQuotaError(backend, `${label} n/a`);
      return null;
    }
    if (trackDelta && state.raw !== null) {
      const d = snapshot.raw - state.raw;
      state.delta = d > 0.05 ? Math.round(d * 10) / 10 : null;
    } else {
      state.delta = null;
    }
    renderQuotaLoaded(backend, snapshot);
    return { backend, ...state };
  } catch {
    showQuotaError(backend, `${label} error`);
    return null;
  } finally {
    state.inFlight = false;
  }
}

async function fetchQuota(trackDelta = false) {
  return fetchQuotaForBackend('claude', { trackDelta });
}

function initQuota() {
  const cfg = QUOTA_CONFIG.claude;
  quotaDisplay.style.setProperty('--quota-accent', agentThemeColor('claude'));
  setVisibleQuotaBackend('claude');
  quotaDisplay.innerHTML = `
    <svg id="quota-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
      <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
      <circle id="${cfg.pieArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('claude')}"
              stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
              transform="rotate(-90 9 9)"/>
    </svg>
    <span id="${cfg.labelId}"></span>`;

  const credsPopup = document.getElementById(cfg.credsPopupId);
  quotaDisplay.addEventListener('click', () => credsPopup.classList.toggle('open'));
  document.addEventListener('click', (e) => {
    if (!quotaDisplay.contains(e.target) && !credsPopup.contains(e.target))
      credsPopup.classList.remove('open');
  });
  fetchQuota();
}

// ── Codex (ChatGPT) quota ──────────────────────────────────────────────────────

const codexQuotaDisplay = document.getElementById('codex-quota-display');

async function fetchCodexQuota() {
  return fetchQuotaForBackend('codex');
}

function parseCursorQuota(data) {
  if (data.isUnlimited) return { raw: 0, pct: 0, resetAt: null, title: 'Cursor (unlimited)' };
  const plan = data?.individualUsage?.plan;
  if (!plan) return null;
  const raw = plan.totalPercentUsed ?? 0;
  const pct = Math.max(0, Math.min(100, Math.round(raw)));
  const resetAt = data.billingCycleEnd ? new Date(data.billingCycleEnd).getTime() : null;
  const title = data.autoModelSelectedDisplayMessage || 'Cursor usage';
  return { raw, pct, resetAt, title };
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
  const cfg = QUOTA_CONFIG.codex;
  codexQuotaDisplay.style.setProperty('--quota-accent', agentThemeColor('codex'));
  codexQuotaDisplay.innerHTML = `
    <svg id="codex-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
      <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
      <circle id="${cfg.pieArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('codex')}"
              stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
              transform="rotate(-90 9 9)"/>
    </svg>
    <span id="${cfg.labelId}"></span>`;
  showQuotaError('codex', 'Codex auth');

  const credsPopup = document.getElementById(cfg.credsPopupId);
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

// ── Cursor quota ──────────────────────────────────────────────────────────────

const cursorQuotaDisplay = document.getElementById('cursor-quota-display');

async function fetchCursorQuota() {
  return fetchQuotaForBackend('cursor');
}

function initCursorQuota() {
  const cfg = QUOTA_CONFIG.cursor;
  cursorQuotaDisplay.style.setProperty('--quota-accent', agentThemeColor('cursor'));
  cursorQuotaDisplay.innerHTML = `
    <svg id="cursor-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
      <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
      <circle id="${cfg.pieArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('cursor')}"
              stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
              transform="rotate(-90 9 9)"/>
    </svg>
    <span id="${cfg.labelId}"></span>`;
  fetchCursorQuota();
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

function renderQuotaStatus() {
  const rows = QUOTA_BACKENDS.map(backend => {
    const q = quotaSnapshots[backend] || { backend, status: 'unsupported' };
    const accent = agentThemeColor(backend);
    let value = 'n/a';
    let detail = 'no quota integration';
    if (q.status === 'loaded') {
      value = `${q.pct}%`;
      const reset = quotaTimeText(q.resetAt);
      detail = reset ? `resets in ${reset}` : 'reset time unavailable';
    } else if (q.status === 'error') {
      value = 'error';
      detail = q.text || 'unavailable';
    } else if (q.status === 'unknown') {
      value = '...';
      detail = 'loading';
    }
    return `<div class="quota-status-row">
      <span class="quota-status-name"><span class="quota-status-dot" style="background:${accent}"></span>${backendDisplayName(backend)}</span>
      <span class="quota-status-value">${value}</span>
      <span class="quota-status-detail">${detail}</span>
    </div>`;
  }).join('');

  return `<div class="proc-section-label">Quotas</div>
    <div class="quota-status-list">${rows}</div>`;
}

function renderProcPopup(running, queued) {
  const header = `<div class="proc-popup-header">
    <span class="settings-label">Status</span>
    <button id="proc-popup-close" type="button">✕</button>
  </div>`;

  let body = renderQuotaStatus();
  if (!running.length && !queued.length) {
    body += '<div class="proc-status-empty">No active processes or queued prompts.</div>';
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

// ── topic manager ─────────────────────────────────────────────────────────────

let _topicsManageCache = null;
let _topicsManageCachePromise = null;
const _topicsExpanded = new Set();
let _topicDeleteTarget = null;

function invalidateTopicsManageCache() {
  _topicsManageCache = null;
}

async function _managedTopics() {
  if (_topicsManageCache) return _topicsManageCache;
  if (!_topicsManageCachePromise) {
    _topicsManageCachePromise = fetch('/topics/manage?include_hidden=true')
      .then(r => r.json())
      .then(data => { _topicsManageCache = data; return data; })
      .finally(() => { _topicsManageCachePromise = null; });
  }
  return _topicsManageCachePromise;
}

function _topicStatusBadges(topic) {
  const badges = [];
  if (topic.last_at) badges.push(`<span class="topic-badge time">${escapeHtml(fmtTime(topic.last_at))}</span>`);
  if (topic.active) badges.push('<span class="topic-badge live">live</span>');
  if (topic.queue_depth > 0) badges.push(`<span class="topic-badge">queue ${topic.queue_depth}</span>`);
  if (topic.hidden) badges.push('<span class="topic-badge hidden">hidden</span>');
  return badges.join('');
}

function _topicAgentDisplay(agentName, backendFallback = null) {
  if (!agentName) return '<span class="col-default">no agent</span>';
  return `<span class="ac-agent"${_agentStyleAttr(agentName, backendFallback)}>@${escapeHtml(agentName)}</span>`;
}

function _renderTopicAgents(topic) {
  const agents = topic.agents || [];
  if (!agents.length) {
    return `<div class="topic-agent-row"><div class="topic-agent-main"><span class="col-default">No agent lanes yet</span></div></div>`;
  }
  let html = '';
  for (const lane of agents) {
    const backend = lane.last_backend || topic.last_backend || null;
    const sessionPrompt = lane.last_prompt ? escapeHtml(truncate(lane.last_prompt, 120)) : '<span class="col-default">No session prompt</span>';
    const laneTime = lane.last_at ? `<span class="topic-badge time">${escapeHtml(fmtTime(lane.last_at))}</span>` : '';
    html += `
      <div class="topic-agent-row" data-topic="${escapeHtml(topic.name)}" data-agent="${escapeHtml(lane.agent)}" data-adhoc="0">
        <div class="topic-agent-main">
          <span class="topic-agent-label"><span class="topic-name">#${escapeHtml(topic.name)}</span>${_topicAgentDisplay(lane.agent, backend)}</span>
        </div>
        <div class="topic-prompt">${sessionPrompt}</div>
        <div class="topic-meta">
          ${laneTime}
          <button class="topic-btn" data-topic-open="${escapeHtml(topic.name)}" data-agent-open="${escapeHtml(lane.agent)}" data-adhoc-open="0" type="button">Open</button>
        </div>
      </div>`;
    if (lane.last_adhoc_prompt) {
      html += `
        <div class="topic-agent-row adhoc" data-topic="${escapeHtml(topic.name)}" data-agent="${escapeHtml(lane.agent)}" data-adhoc="1">
          <div class="topic-agent-main">
            <span class="topic-agent-label"><span class="topic-name">#${escapeHtml(topic.name)}</span>${_topicAgentDisplay(lane.agent, backend)}!</span>
          </div>
          <div class="topic-prompt">${escapeHtml(truncate(lane.last_adhoc_prompt, 120))}</div>
          <div class="topic-meta">
            <span class="topic-badge">adhoc</span>
            ${laneTime}
            <button class="topic-btn" data-topic-open="${escapeHtml(topic.name)}" data-agent-open="${escapeHtml(lane.agent)}" data-adhoc-open="1" type="button">Open</button>
          </div>
        </div>`;
    }
  }
  return html;
}

function _renderTopicRows(topic) {
  const expanded = _topicsExpanded.has(topic.name);
  const agentLabel = _topicAgentDisplay(topic.agent, topic.last_backend || null);
  const prompt = topic.last_prompt ? escapeHtml(truncate(topic.last_prompt, 120)) : '<span class="col-default">No prompt yet</span>';
  const memoryLabel = topic.memory?.exists ? 'Memory' : 'Add memory';
  const hideLabel = topic.hidden ? 'Show' : 'Hide';
  return `
    <div class="topic-row${topic.hidden ? ' hidden' : ''}${expanded ? ' expanded' : ''}" data-topic="${escapeHtml(topic.name)}">
      <div class="topic-main">
        <span class="topic-caret">${expanded ? '▾' : '▸'}</span>
        <span class="topic-identity"><span class="topic-name">#${escapeHtml(topic.name)}</span>${agentLabel}</span>
      </div>
      <div class="topic-prompt">${prompt}</div>
      <div class="topic-meta">
        ${_topicStatusBadges(topic)}
        <button class="topic-btn" data-topic-open="${escapeHtml(topic.name)}" type="button">Open</button>
        <button class="topic-btn" data-topic-memory="${escapeHtml(topic.name)}" type="button">${memoryLabel}</button>
        <button class="topic-btn" data-topic-hide="${escapeHtml(topic.name)}" data-hidden="${topic.hidden ? '1' : '0'}" type="button">${hideLabel}</button>
        <button class="topic-btn danger" data-topic-delete="${escapeHtml(topic.name)}" type="button">Delete</button>
      </div>
    </div>
    <div class="topic-agents"${expanded ? '' : ' hidden'}>${_renderTopicAgents(topic)}</div>`;
}

async function loadTopicsView() {
  const listEl = document.getElementById('topics-list');
  const countEl = document.getElementById('topics-count');
  const searchEl = document.getElementById('topics-search');
  if (!listEl) return;
  if (!_topicsManageCache) listEl.innerHTML = '<div class="topics-empty">Loading…</div>';
  let topics;
  try {
    topics = await _managedTopics();
  } catch {
    listEl.innerHTML = '<div class="topics-empty">Failed to load.</div>';
    if (countEl) countEl.textContent = '';
    return;
  }

  const q = (searchEl?.value || '').trim().toLowerCase();
  const filtered = q ? topics.filter(t => t.name.toLowerCase().includes(q)) : topics;
  if (countEl) countEl.textContent = `${filtered.length} / ${topics.length}`;
  if (!filtered.length) {
    listEl.innerHTML = '<div class="topics-empty">No topics found.</div>';
    return;
  }
  listEl.innerHTML = filtered.map(_renderTopicRows).join('');
  bindTopicsView();
}

function bindTopicsView() {
  const listEl = document.getElementById('topics-list');
  listEl.querySelectorAll('.topic-row').forEach(row => {
    row.addEventListener('click', e => {
      if (e.target.closest('button')) return;
      const topic = row.dataset.topic;
      const expanded = row.classList.toggle('expanded');
      const caret = row.querySelector('.topic-caret');
      if (caret) caret.textContent = expanded ? '▾' : '▸';
      if (expanded) _topicsExpanded.add(topic);
      else _topicsExpanded.delete(topic);
      const agentsEl = row.nextElementSibling;
      if (agentsEl?.classList.contains('topic-agents')) agentsEl.hidden = !expanded;
    });
  });
  listEl.querySelectorAll('[data-topic-open]').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const topic = btn.dataset.topicOpen;
      const agent = btn.dataset.agentOpen || null;
      const adhoc = btn.dataset.adhocOpen === '1';
      if (agent) filterByAgent(topic, agent, adhoc);
      else filterByTopic(topic);
      switchView('chat');
    });
  });
  listEl.querySelectorAll('[data-topic-memory]').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      openMemoryEditor(btn.dataset.topicMemory);
    });
  });
  listEl.querySelectorAll('[data-topic-hide]').forEach(btn => {
    btn.addEventListener('click', async e => {
      e.stopPropagation();
      const topic = btn.dataset.topicHide;
      const nextHidden = btn.dataset.hidden !== '1';
      btn.disabled = true;
      try {
        await fetch(`/topics/${encodeURIComponent(topic)}/hidden`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ hidden: nextHidden }),
        });
        invalidateTopicsCache();
        invalidateTopicsManageCache();
        loadTopicsView();
      } finally {
        btn.disabled = false;
      }
    });
  });
  listEl.querySelectorAll('[data-topic-delete]').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      openTopicDeleteModal(btn.dataset.topicDelete);
    });
  });
}

function initTopicsView() {
  const searchEl = document.getElementById('topics-search');
  if (!searchEl) return;
  searchEl.addEventListener('input', () => loadTopicsView());
}

function openTopicDeleteModal(topic) {
  _topicDeleteTarget = topic;
  document.getElementById('topic-delete-modal-title').textContent = `#${topic}`;
  document.getElementById('topic-delete-confirm').disabled = false;
  document.getElementById('topic-delete-modal').classList.add('open');
}

function closeTopicDeleteModal() {
  _topicDeleteTarget = null;
  document.getElementById('topic-delete-modal').classList.remove('open');
}

async function confirmTopicDelete() {
  if (!_topicDeleteTarget) return;
  const topic = _topicDeleteTarget;
  const btn = document.getElementById('topic-delete-confirm');
  btn.disabled = true;
  try {
    const res = await fetch(`/topics/${encodeURIComponent(topic)}`, { method: 'DELETE' });
    if (!res.ok) return;
    _topicsExpanded.delete(topic);
    closeTopicDeleteModal();
    invalidateTopicsCache();
    invalidateTopicsManageCache();
    loadTopicsView();
  } finally {
    btn.disabled = false;
  }
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
  const afBackend = document.getElementById('af-backend');
  const afModel   = document.getElementById('af-model');
  afBackend.addEventListener('change', () => {
    afModel.placeholder = BACKEND_MODEL_HINTS[afBackend.value] || 'model (optional)';
  });

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

async function maybeShowCodeRootsNudge(topic, anchor) {
  let meta;
  try { meta = await fetchMemoryMeta(topic); } catch { return; }
  if (hasCodeRootsDecision(meta)) return;
  if (document.getElementById('code-roots-prompt')) return;
  showCodeRootsNudge(topic, anchor);
}

function showCodeRootsNudge(topic, anchor) {
  const existing = document.getElementById('code-roots-prompt');
  if (existing) existing.remove();

  const panel = document.createElement('div');
  panel.id = 'code-roots-prompt';
  panel.className = 'agent-create-prompt';
  panel.innerHTML = `
    <div class="acp-title">Set up diff tracking for <strong>#${topic}</strong>? Squid shows changed files after each agent run. <span style="color:#666">(optional — dismiss to skip)</span></div>
    <div class="acp-row">
      <input id="crp-paths" placeholder="/path/to/repo  (comma-separate for multiple)" />
    </div>
    <div class="acp-actions">
      <button id="crp-save">Save</button>
      <button id="crp-skip">Not a code project</button>
      <button id="crp-cancel">Dismiss</button>
    </div>`;

  if (anchor.nextSibling) {
    messages.insertBefore(panel, anchor.nextSibling);
  } else {
    messages.appendChild(panel);
  }
  messages.scrollTop = messages.scrollHeight;

  panel.querySelector('#crp-cancel').addEventListener('click', () => panel.remove());

  panel.querySelector('#crp-skip').addEventListener('click', async () => {
    try {
      await saveCodeRootsDecision(topic, { code_roots_skipped: true });
      panel.remove();
    } catch (err) {
      panel.querySelector('.acp-title').textContent = err?.message || 'Failed to save.';
    }
  });

  panel.querySelector('#crp-save').addEventListener('click', async () => {
    const roots = parseCodeRootsInput(panel.querySelector('#crp-paths').value);
    if (!roots.length) {
      panel.querySelector('.acp-title').textContent = 'Enter at least one path, or click "Not a code project".';
      return;
    }
    try {
      await saveCodeRootsDecision(topic, { code_roots: roots });
      panel.remove();
    } catch (err) {
      panel.querySelector('.acp-title').textContent = err?.message || 'Failed to save.';
    }
  });
}

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
        <option value="codex">codex</option>
        <option value="cursor">cursor</option>
      </select>
      <input id="acp-model" placeholder="${BACKEND_MODEL_HINTS.claude}" />
      <input id="acp-cwd" placeholder="cwd (default: /tmp/squid)" />
    </div>
    <div class="acp-actions">
      <button id="acp-save">Create &amp; send</button>
      <button id="acp-cancel">Cancel</button>
    </div>`;

  messages.appendChild(prompt);
  messages.scrollTop = messages.scrollHeight;

  const modelInput = prompt.querySelector('#acp-model');
  prompt.querySelector('#acp-backend').addEventListener('change', e => {
    modelInput.placeholder = BACKEND_MODEL_HINTS[e.target.value] || 'model (optional)';
  });

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
  if (!_agentsCachePromise) {
    _agentsCachePromise = fetch('/config/agents')
      .then(res => res.json())
      .catch(() => [])
      .then(agents => {
        _agentsCache = agents;
        refreshAgentSlugColors();
        return _agentsCache;
      })
      .finally(() => { _agentsCachePromise = null; });
  }
  await _agentsCachePromise;
  refreshAgentSlugColors();
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
      await fetch(`/topics/${encodeURIComponent(name)}/hidden`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hidden: true }),
      });
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
  input.value = item.trail === false ? item.insert : item.insert + ' ';
  resizeComposer();
  input.focus();
  input.dispatchEvent(new Event('input'));
}

function _agentStyleAttr(agentName, backendFallback = null) {
  return ` style="--agent-color:${agentSlugColor(agentName, backendFallback)}" data-agent-name="${escapeHtml(agentName || '')}"${backendFallback ? ` data-backend-fallback="${escapeHtml(backendFallback)}"` : ''}`;
}

function _acTopicLabel(topicName, modelLabel, backendFallback = null) {
  return `<span class="ac-topic">#${escapeHtml(topicName)}</span>` +
         (modelLabel ? `<span class="ac-agent"${_agentStyleAttr(modelLabel, backendFallback)}>@${escapeHtml(modelLabel)}</span>` : '');
}

function _acAgentLabel(topicName, agentName, backendFallback = null) {
  return `<span class="ac-topic">#${escapeHtml(topicName)}</span><span class="ac-agent"${_agentStyleAttr(agentName.replace(/!$/, ''), backendFallback)}>@${escapeHtml(agentName)}</span>`;
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
          label:       _acTopicLabel(t.name, t.last_model || t.last_backend || '', t.last_backend || null),
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
    const backendByAgent = new Map(agents.map(a => [a.name, a.backend]));
    const items = [];

    // Used agents — with last prompt
    for (const h of history) {
      if (!h.agent.toLowerCase().startsWith(prefix)) continue;
      items.push({
        label:  _acAgentLabel(topic, h.agent, backendByAgent.get(h.agent) || null),
        insert: `#${topic}@${h.agent}`,
        sub:    h.last_prompt ? truncate(h.last_prompt, 55) : '',
      });
      // Also offer adhoc variant
      items.push({
        label:  _acAgentLabel(topic, h.agent + '!', backendByAgent.get(h.agent) || null),
        insert: `#${topic}@${h.agent}!`,
        sub:    h.last_adhoc_prompt ? truncate(h.last_adhoc_prompt, 55) : '',
        meta:   'adhoc',
      });
    }

    // Other available agents — no prompt
    for (const a of agents) {
      if (usedNames.has(a.name)) continue;
      if (!a.name.toLowerCase().startsWith(prefix)) continue;
      items.push({
        label:  _acAgentLabel(topic, a.name, a.backend),
        insert: `#${topic}@${a.name}`,
        meta:   a.model || a.backend,
      });
    }

    _acRender(items.slice(0, 10));
  } else if (!val && promptHistory.length) {
    _acRender(promptHistory.slice(0, 8).map(ph => ({
      label: escapeHtml(truncate(ph, 70)),
      insert: ph,
      trail: false,
    })));
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

const BOOT_LOGO_ART = ` 🦑 AGENT
 ██████╗ ██████╗ ██╗   ██╗██╗██████╗
██╔════╝██╔═══██╗██║   ██║██║██╔══██╗
╚█████╗ ██║   ██║██║   ██║██║██║  ██║
 ╚═══██╗██║▄▄ ██║██║   ██║██║██║  ██║
██████╔╝╚██████╔╝╚██████╔╝██║██████╔╝
╚═════╝  ╚══▀▀═╝  ╚═════╝ ╚═╝╚═════╝`;

const BOOT_LOGO_MOBILE = '🦑 AGENT-SQUID';

function bootLogoHtml() {
  return `<pre class="boot-art">${BOOT_LOGO_ART}</pre>` +
    `<div class="boot-art-mobile">${BOOT_LOGO_MOBILE}</div>`;
}

async function showBootBanner() {
  try {
    const res = await fetch('/health');
    if (!res.ok) return;
    const data = await res.json();
    const bootTime = data.boot_time ? fmtTime(data.boot_time) : '';
    const el = document.createElement('div');
    el.className = 'boot-banner';
    el.innerHTML = bootLogoHtml() +
      `<div class="boot-meta">agent squid${bootTime ? `  ·  started ${bootTime}` : ''}</div>` +
      (!navigator.onLine ? `<div class="boot-offline">no internet — LLM calls will fail</div>` : '');
    messages.appendChild(el);

    const backends = data.backends || {};
    const anyAvailable = Object.values(backends).some(b => b.available);
    if (!anyAvailable) {
      const setup = document.createElement('div');
      setup.className = 'no-agent-setup';
      const agents = [
        { name: 'Claude Code', cmd: 'npm install -g @anthropic-ai/claude-code' },
        { name: 'Codex',       cmd: 'npm install -g @openai/codex' },
      ];
      setup.innerHTML = `
        <div class="no-agent-title">No coding agents found</div>
        <div class="no-agent-sub">Install at least one to get started, then restart Squid.</div>
        <div class="no-agent-list">
          ${agents.map(a => `
            <div class="no-agent-row">
              <span class="no-agent-name">${a.name}</span>
              <code class="no-agent-cmd">${a.cmd}</code>
              <button class="no-agent-copy" data-cmd="${a.cmd}">copy</button>
            </div>`).join('')}
        </div>
        <div class="no-agent-restart">Then restart: <code>bin/start.sh --restart</code></div>`;
      setup.querySelectorAll('.no-agent-copy').forEach(btn => {
        btn.addEventListener('click', () => {
          navigator.clipboard.writeText(btn.dataset.cmd).then(() => {
            btn.textContent = 'copied';
            setTimeout(() => { btn.textContent = 'copy'; }, 1500);
          });
        });
      });
      messages.appendChild(setup);
    }

    messages.scrollTop = messages.scrollHeight;
  } catch {
    const isLocal = ['127.0.0.1', 'localhost', '::1'].includes(location.hostname);
    const msg = !navigator.onLine
      ? 'no network connection'
      : isLocal
        ? 'squid server is not running'
        : 'server unreachable — check Tailscale';
    const el = document.createElement('div');
    el.className = 'boot-banner';
    el.innerHTML = bootLogoHtml() +
      `<div class="boot-offline">${msg}</div>`;
    messages.appendChild(el);
    messages.scrollTop = messages.scrollHeight;
  }
}

// ── ctx popup ─────────────────────────────────────────────────────────────────

function showCtxPopup(spanEl) {
  let popup = document.getElementById('ctx-popup');
  if (!popup) {
    popup = document.createElement('div');
    popup.id = 'ctx-popup';
    document.getElementById('app').appendChild(popup);
  }
  if (popup._forSpanEl === spanEl && popup.classList.contains('open')) {
    popup.classList.remove('open');
    popup._forSpanEl = null;
    return;
  }
  popup._forSpanEl = spanEl;

  const sid    = spanEl.dataset.sessionId || '';
  const cwd    = spanEl.dataset.cwd || '';
  const mem    = spanEl.dataset.mem === 'true';
  const topic  = spanEl.dataset.topic || '';
  const pinIds = JSON.parse(spanEl.dataset.pinnedIds || '[]');

  let html = '';
  if (sid || cwd) {
    html += `<div class="ctx-popup-row"><span class="ctx-popup-key">session</span><span class="ctx-popup-val">${sid}</span></div>`;
    if (cwd) html += `<div class="ctx-popup-row"><span class="ctx-popup-key">cwd</span><span class="ctx-popup-val">${cwd}</span></div>`;
  }
  if (mem && topic) {
    if (html) html += `<div class="ctx-popup-divider"></div>`;
    html += `<div class="ctx-popup-pin ctx-popup-mem-row" data-topic="${topic}">
      <span class="ctx-popup-tag">mem</span>
      <span class="ctx-popup-preview">#${topic} memory</span>
    </div>`;
  }
  if (pinIds.length) {
    if (html) html += `<div class="ctx-popup-divider"></div>`;
    html += `<div class="ctx-popup-row"><span class="ctx-popup-key">pins</span></div>`;
    pinIds.forEach(id => {
      html += `<div class="ctx-popup-pin" data-pin-id="${id}">
        <span class="ctx-popup-tag">#${id}</span>
        <span class="ctx-popup-preview" id="ctx-pin-preview-${id}">loading…</span>
      </div>`;
    });
  }
  if (!html) html = `<div class="ctx-popup-row"><span class="ctx-popup-key">${spanEl.textContent.trim()}</span></div>`;

  popup.innerHTML = html;
  popup.classList.add('open');

  const memRow = popup.querySelector('.ctx-popup-mem-row');
  if (memRow) {
    memRow.addEventListener('click', () => {
      openMemoryEditor(memRow.dataset.topic);
    });
  }

  popup.querySelectorAll('.ctx-popup-pin[data-pin-id]').forEach(row => {
    row.addEventListener('click', () => openMsgModal(parseInt(row.dataset.pinId)));
  });

  pinIds.forEach(id => {
    fetch(`/chat/${id}/status`)
      .then(r => r.json())
      .then(msg => {
        const el = document.getElementById(`ctx-pin-preview-${id}`);
        if (el) el.textContent = (msg.content || msg.prompt || '(empty)').slice(0, 80);
      })
      .catch(() => {
        const el = document.getElementById(`ctx-pin-preview-${id}`);
        if (el) el.textContent = 'failed to load';
      });
  });

  const rect = spanEl.getBoundingClientRect();
  const appRect = document.getElementById('app').getBoundingClientRect();
  popup.style.bottom = (appRect.bottom - rect.top + 6) + 'px';
  popup.style.right  = (appRect.right  - rect.right + 0) + 'px';
}

async function openMsgModal(msgId) {
  const modal = document.getElementById('msg-modal');
  const title = document.getElementById('msg-modal-title');
  const body  = document.getElementById('msg-modal-body');
  title.textContent = `Message #${msgId}`;
  body.innerHTML = '<div class="ctx-popup-row" style="padding:1rem"><span class="ctx-popup-key">Loading…</span></div>';
  modal.classList.add('open');
  try {
    const msg = await fetch(`/chat/${msgId}/status`).then(r => r.json());
    title.textContent = `Message #${msgId} · #${msg.topic || ''}${msg.agent ? ' @' + msg.agent : ''}`;
    body.innerHTML = '';
    appendHistoryItem(msg, body);
  } catch {
    body.innerHTML = '<div class="ctx-popup-row" style="padding:1rem"><span class="ctx-popup-key">Failed to load</span></div>';
  }
}

// ── pin basket ────────────────────────────────────────────────────────────────

const memoryModal = document.getElementById('memory-modal');
const memoryEditor = document.getElementById('memory-editor');
const memoryTitle = document.getElementById('memory-modal-title');
const memoryPath = document.getElementById('memory-path');
const memoryTokenCount = document.getElementById('memory-token-count');
const memoryTokenHelp = document.getElementById('memory-token-help');
const memorySaveBtn = document.getElementById('memory-save');
const memoryCloseBtn = document.getElementById('memory-modal-close');
const _memoryCache = {};
const _sessionLookupCache = {};
const _memorySelectionOverrides = {};
let _editingMemoryTopic = null;

function updateMemoryTokenCount() {
  const n = Math.ceil((memoryEditor.value || '').length / 4);
  memoryTokenCount.textContent = n > 0 ? ` · ~${fmtNum(n)} tokens` : '';
}
memoryEditor.addEventListener('input', updateMemoryTokenCount);

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function getPinnedItems() {
  try { return JSON.parse(localStorage.getItem('pinnedItems') || '[]'); } catch { return []; }
}
function setPinnedItems(items) { localStorage.setItem('pinnedItems', JSON.stringify(items)); }
function clearPinnedItems() {
  setPinnedItems([]);
  document.querySelectorAll('.msg-pin-btn.pinned').forEach(b => b.classList.remove('pinned'));
  const { adhoc, lookback } = _currentContextTarget();
  _allLookbackItems(adhoc, lookback).forEach(item => _lookbackUnselected.add(item.id));
  updateInContextMarkers();
  updatePinCount();
  if (pinPanel.classList.contains('open')) renderPinPanel();
}
function getInjectedInto() {
  try { return JSON.parse(localStorage.getItem('injectedInto') || '{}'); } catch { return {}; }
}
function setInjectedInto(map) { localStorage.setItem('injectedInto', JSON.stringify(map)); }

function _currentContextTarget() {
  const parsed = parseInput(input.value);
  const topic = parsed.topic || stickyChip?.topic || 'default';
  const adhoc = parsed.adhoc || (stickyChip?.adhoc ?? false);
  // parsed.lookback is 0 both for "explicit !0" and "regex didn't match" (default path).
  // Use chip's lookback when the adhoc regex didn't match (parsed.adhoc is false but chip is adhoc).
  const fromChip = !!(stickyChip && !input.value.startsWith('#'));
  const lookback = adhoc
    ? (fromChip || !parsed.adhoc ? (stickyChip?.lookback || 0) : parsed.lookback)
    : 0;
  let agent = parsed.agent || stickyChip?.agent || null;
  if (!agent && _topicsCache) {
    agent = _topicsCache.find(t => t.name === topic)?.agent || null;
  } else if (!agent) {
    _acTopics().then(() => {
      updatePinCount();
      if (pinPanel.classList.contains('open')) renderPinPanel();
      updateInContextMarkers();
    });
  }
  return { topic, agent, adhoc, lookback };
}

function _memoryOverrideKey(topic, agent, adhoc) {
  return `${topic}@${agent || '_'}:${adhoc ? 'adhoc' : 'session'}`;
}

function _memoryInjectedKey(topic, agent) {
  return `${topic}@${agent || '_'}`;
}

function _getMemoryMeta(topic) {
  if (_memoryCache[topic]) return _memoryCache[topic];
  _memoryCache[topic] = { topic, exists: false, content: '', path: `context/topics/${topic}/memory.md`, loading: true };
  fetch(`/topics/${encodeURIComponent(topic)}/memory`)
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (data) _memoryCache[topic] = { ...data, loading: false };
      updatePinCount();
      if (pinPanel.classList.contains('open')) renderPinPanel();
    })
    .catch(() => { _memoryCache[topic].loading = false; });
  return _memoryCache[topic];
}

async function fetchMemoryMeta(topic) {
  const data = await fetch(`/topics/${encodeURIComponent(topic)}/memory`).then(r => r.json());
  if (!data || typeof data !== 'object' || Array.isArray(data)) return data;
  _memoryCache[topic] = { ...data, loading: false };
  return _memoryCache[topic];
}

function hasCodeRootsDecision(meta) {
  if (!meta || typeof meta !== 'object' || Array.isArray(meta)) return true;
  const squid = meta?.squid || {};
  return !!(squid.code_roots && squid.code_roots.length) || !!squid.code_roots_skipped;
}

function parseCodeRootsInput(value) {
  return (value || '')
    .split(/[\n,]/)
    .map(s => s.trim())
    .filter(Boolean);
}

async function saveCodeRootsDecision(topic, payload) {
  const res = await fetch(`/topics/${encodeURIComponent(topic)}/memory/squid/code-roots`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Save failed (${res.status})`);
  _memoryCache[topic] = { ...data, loading: false };
  updatePinCount();
  if (pinPanel.classList.contains('open')) renderPinPanel();
  return data;
}


function _getSessionMeta(topic, agent) {
  if (!agent) return { session_id: null, cwd: null, loading: false };
  const key = `${topic}@${agent}`;
  if (_sessionIds[`${topic}@${agent}`]) {
    return { session_id: _sessionIds[`${topic}@${agent}`], cwd: null, loading: false };
  }
  if (_sessionLookupCache[key]) return _sessionLookupCache[key];
  _sessionLookupCache[key] = { session_id: null, cwd: null, loading: true };
  fetch(`/topics/${encodeURIComponent(topic)}/session?agent=${encodeURIComponent(agent)}`)
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      _sessionLookupCache[key] = { ...(data || { session_id: null, cwd: null }), loading: false };
      if (data?.session_id) {
        _sessionIds[`${topic}@${agent}`] = data.session_id;
        if (data.injected_ids?.length) {
          const inj = getInjectedInto();
          inj[data.session_id] = [...new Set([...(inj[data.session_id] || []), ...data.injected_ids])];
          setInjectedInto(inj);
        }
      }
      updatePinCount();
      if (pinPanel.classList.contains('open')) renderPinPanel();
      updateInContextMarkers();
    })
    .catch(() => { _sessionLookupCache[key].loading = false; });
  return _sessionLookupCache[key];
}

function _topicMemoryState() {
  const { topic, agent, adhoc } = _currentContextTarget();
  const meta = _getMemoryMeta(topic);
  const session = _getSessionMeta(topic, agent);
  const exists = !!(meta.exists && (meta.content || '').trim());
  const key = _memoryOverrideKey(topic, agent, adhoc);
  const injected = !adhoc && !!_memoryInjectedInto[_memoryInjectedKey(topic, agent)];
  const defaultSelected = exists && (adhoc || (!injected && !session.loading && !session.session_id));
  const selected = exists && (_memorySelectionOverrides[key] ?? defaultSelected);
  return { topic, agent, adhoc, meta, session, exists, selected, key, injected };
}

async function _topicMemoryStateForSend(topic, agent, adhoc) {
  const meta = await fetch(`/topics/${encodeURIComponent(topic)}/memory`)
    .then(r => r.ok ? r.json() : null)
    .catch(() => null);
  if (meta) _memoryCache[topic] = { ...meta, loading: false };
  const exists = !!(meta?.exists && (meta.content || '').trim());
  if (!exists) return { selected: false };

  let session = { session_id: null };
  if (agent && !adhoc) {
    session = await fetch(`/topics/${encodeURIComponent(topic)}/session?agent=${encodeURIComponent(agent)}`)
      .then(r => r.ok ? r.json() : { session_id: null })
      .catch(() => ({ session_id: null }));
    if (session.session_id) _sessionIds[`${topic}@${agent}`] = session.session_id;
  }
  const key = _memoryOverrideKey(topic, agent, adhoc);
  const injected = !adhoc && !!_memoryInjectedInto[_memoryInjectedKey(topic, agent)];
  const defaultSelected = adhoc || (!injected && !session.session_id);
  return { selected: _memorySelectionOverrides[key] ?? defaultSelected };
}

function updatePinCount() {
  const { topic, agent, adhoc, lookback } = _currentContextTarget();
  const n = getPinnedItems().length + _activeLookbackItems(adhoc, lookback).length;
  const memorySelected = _topicMemoryState().selected;
  const total = n + (memorySelected ? 1 : 0);
  pinCountEl.textContent = total || '';
  pinCountEl.classList.toggle('visible', total > 0);
  pinBtn.classList.toggle('has-pins', total > 0);
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

  // Skip only if the pin is from the exact current session — --resume already covers it
  if (sameSession && !isAdhoc) {
    const qual = chipAgent ? ` · #${chipTopic}@${chipAgent}` : '';
    return { text: `in session${qual} · skip`, cls: 'pin-status-session' };
  }

  // Already injected into this topic@agent via a previous adhoc turn
  // Only meaningful for !N lookback where model retains prior context; !0 is always fresh
  const chipLookback = parsed.adhoc ? parsed.lookback : (stickyChip?.lookback ?? 0);
  if (currentSid && (injected[currentSid] || []).includes(item.id) && !(isAdhoc && chipLookback === 0))
    return { text: 'injected · skip', cls: 'pin-status-done' };

  return { text: 'will inject', cls: 'pin-status-inject' };
}

function _memoryStatus(state) {
  if (state.meta.loading || state.session.loading) return { text: 'checking', cls: 'pin-status-session' };
  if (!state.exists) return { text: 'no memory', cls: 'pin-status-empty' };
  if (state.selected) return { text: 'will inject', cls: 'pin-status-inject' };
  if (state.injected) return { text: 'in session · skip', cls: 'pin-status-session' };
  if (!state.adhoc && state.session.session_id) return { text: 'in session · skip', cls: 'pin-status-session' };
  return { text: 'skipped', cls: 'pin-status-done' };
}

function renderPinPanel() {
  const items = getPinnedItems();
  const listEl = document.getElementById('pin-panel-list');
  const clearBtn = document.getElementById('pin-panel-clear');
  const { topic: ctxTopic, agent: ctxAgent, adhoc, lookback } = _currentContextTarget();
  const activeLbItems = _activeLookbackItems(adhoc, lookback);
  const activeLbIds = new Set(activeLbItems.map(i => i.id));
  const manualPinIds = new Set(items.map(i => i.id));
  // Merge: lookback items first, then manual pins not already covered by lookback
  const mergedItems = [
    ...activeLbItems.map(i => ({ ...i, isLookback: true, isManual: manualPinIds.has(i.id) })),
    ...items.filter(i => !activeLbIds.has(i.id)).map(i => ({ ...i, isLookback: false, isManual: true })),
  ];
  if (clearBtn) clearBtn.disabled = mergedItems.length === 0;
  let html = '';
  const memoryState = _topicMemoryState();
  const memoryStatus = _memoryStatus(memoryState);
  const preview = memoryState.exists
    ? (memoryState.meta.content || '').trim().replace(/\s+/g, ' ').slice(0, 90)
    : 'No memory yet';
  html += `<div class="memory-item">
    <span class="pin-item-tag">#${escapeHtml(memoryState.topic)}</span>
    <span class="memory-item-preview" data-memory-edit="1">Topic memory · ${escapeHtml(preview)}</span>
    <span class="memory-item-status ${memoryStatus.cls}">${memoryStatus.text}</span>
    <button class="pin-item-toggle${memoryState.selected ? ' active' : ''}" data-memory-toggle="1" type="button">${memoryState.selected ? 'On' : (memoryState.exists ? 'Off' : 'Add')}</button>
  </div>`;

  if (mergedItems.length) {
    html += `<div class="pin-section-label">Pinned</div>`;
    mergedItems.forEach(item => {
      const st = _pinStatus(item);
      const tag = _pinTagStr(item);
      const preview = (item.content || '').replace(/\s+/g, ' ').slice(0, 90);
      const lbAttr = item.isManual ? '' : ' data-lb="1"';
      html += `<div class="pin-item">
        <span class="pin-item-tag">${escapeHtml(tag)}</span>
        <span class="pin-item-preview">${escapeHtml(preview)}</span>
        <span class="pin-item-status ${st.cls}">${st.text}</span>
        <button class="pin-item-remove" data-id="${item.id}"${lbAttr} type="button">✕</button>
      </div>`;
    });
  } else {
    html += '<div style="padding:0.5rem 0.8rem;color:#484858;font-size:0.78em">No pins yet.<br>Click <svg width="9" height="11" viewBox="0 0 12 14" fill="currentColor" aria-hidden="true" style="vertical-align:-0.1em"><path d="M2 0h8a1 1 0 0 1 1 1v12.8l-5-2.9-5 2.9V1a1 1 0 0 1 1-1z"/></svg> on any response to add it.</div>';
  }

  listEl.innerHTML = html;
  listEl.querySelectorAll('[data-memory-edit]').forEach(el => {
    el.addEventListener('mousedown', e => {
      e.preventDefault();
      openMemoryEditor(memoryState.topic);
    });
  });
  listEl.querySelectorAll('[data-memory-toggle]').forEach(btn => {
    btn.addEventListener('mousedown', e => {
      e.preventDefault();
      if (!memoryState.exists) {
        openMemoryEditor(memoryState.topic);
        return;
      }
      _memorySelectionOverrides[memoryState.key] = !memoryState.selected;
      updatePinCount();
      renderPinPanel();
    });
  });
  listEl.querySelectorAll('.pin-item-remove').forEach(btn => {
    btn.addEventListener('mousedown', e => {
      e.preventDefault();
      const id = parseInt(btn.dataset.id);
      if (btn.dataset.lb) {
        _lookbackUnselected.add(id);
        updateInContextMarkers();
      } else {
        setPinnedItems(getPinnedItems().filter(i => i.id !== id));
        document.querySelectorAll(`.msg-pin-btn[data-msg-id="${id}"]`)
          .forEach(b => b.classList.remove('pinned'));
      }
      updatePinCount();
      renderPinPanel();
    });
  });
}

async function openMemoryEditor(topic) {
  _editingMemoryTopic = topic;
  closePinPanel();
  memoryTitle.textContent = `Topic memory · #${topic}`;
  memoryEditor.value = 'Loading...';
  memoryPath.textContent = `context/topics/${topic}/memory.md`;
  memoryTokenCount.textContent = '';
  memoryModal.classList.add('open');
  try {
    const data = await fetch(`/topics/${encodeURIComponent(topic)}/memory`).then(r => r.json());
    _memoryCache[topic] = { ...data, loading: false };
    memoryEditor.value = data.content || '';
    memoryPath.textContent = data.path || `context/topics/${topic}/memory.md`;
    updateMemoryTokenCount();
    memoryEditor.focus();
  } catch {
    memoryEditor.value = '';
    updateMemoryTokenCount();
  }
}

function closeMemoryEditor() {
  memoryModal.classList.remove('open');
  _editingMemoryTopic = null;
}

async function saveMemoryEditor() {
  if (!_editingMemoryTopic) return;
  const topic = _editingMemoryTopic;
  const idleLabel = 'Save';
  memorySaveBtn.disabled = true;
  memorySaveBtn.textContent = 'Saving...';
  memoryPath.textContent = `context/topics/${topic}/memory.md`;
  try {
    const res = await fetch(`/topics/${encodeURIComponent(topic)}/memory`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: memoryEditor.value }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Save failed (${res.status})`);
    _memoryCache[topic] = { ...data, loading: false };
    memoryPath.textContent = `${data.path || `context/topics/${topic}/memory.md`} · saved`;
    memorySaveBtn.textContent = 'Saved';
    updatePinCount();
    if (pinPanel.classList.contains('open')) renderPinPanel();
  } catch (err) {
    memoryPath.textContent = err?.message || 'Save failed';
    memorySaveBtn.textContent = idleLabel;
  } finally {
    memorySaveBtn.disabled = false;
    if (memorySaveBtn.textContent === 'Saved') {
      setTimeout(() => {
        if (!memorySaveBtn.disabled) memorySaveBtn.textContent = idleLabel;
      }, 1200);
    }
  }
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
    } else if (btn.classList.contains('lookback-sel')) {
      _lookbackUnselected.add(msgId);
      updateInContextMarkers();
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
  const header = bubbleEl.querySelector('.response-header');
  (header || bubbleEl).appendChild(btn);
}

function initPin() {
  pinBtn.addEventListener('click', () => {
    if (pinPanel.classList.contains('open')) closePinPanel();
    else openPinPanel();
  });
  document.getElementById('pin-panel-close').addEventListener('click', closePinPanel);
  document.getElementById('pin-panel-clear').addEventListener('click', clearPinnedItems);
  memoryCloseBtn.addEventListener('click', closeMemoryEditor);
  memorySaveBtn.addEventListener('click', saveMemoryEditor);
  memoryModal.addEventListener('mousedown', e => {
    if (e.target === memoryModal) closeMemoryEditor();
  });
  document.getElementById('msg-modal-close').addEventListener('click', () => {
    document.getElementById('msg-modal').classList.remove('open');
  });
  document.getElementById('msg-modal').addEventListener('mousedown', e => {
    if (e.target === document.getElementById('msg-modal')) document.getElementById('msg-modal').classList.remove('open');
  });
  document.getElementById('topic-delete-modal-close').addEventListener('click', closeTopicDeleteModal);
  document.getElementById('topic-delete-cancel').addEventListener('click', closeTopicDeleteModal);
  document.getElementById('topic-delete-confirm').addEventListener('click', confirmTopicDelete);
  document.getElementById('topic-delete-modal').addEventListener('mousedown', e => {
    if (e.target === document.getElementById('topic-delete-modal')) closeTopicDeleteModal();
  });
  updatePinCount();
}

// ── init ─────────────────────────────────────────────────────────────────────

initSettings();
initPin();
document.getElementById('filter-badge-clear').addEventListener('click', clearFilter);
document.getElementById('search-bar-clear').addEventListener('click', clearSearch);
document.getElementById('search-bar-keywords').addEventListener('click', () => {
  if (!searchActive || !searchState) return;
  let cmd = '/s ';
  if (searchState.topic) {
    cmd += '#' + searchState.topic;
    if (searchState.agent) cmd += '@' + searchState.agent;
    if (searchState.adhoc) cmd += '!';
    cmd += ' ';
  } else if (searchState.agent) {
    cmd += '@' + searchState.agent;
    if (searchState.adhoc) cmd += '!';
    cmd += ' ';
  }
  cmd += searchState.keywords;
  input.value = cmd.trim();
  input.focus();
  resizeComposer();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('topic-delete-modal')?.classList.contains('open')) {
    closeTopicDeleteModal();
  }
});
document.addEventListener('click', e => {
  if (!acEl.contains(e.target) && e.target !== input) hideAutocomplete();
  if (!pinPanel.contains(e.target) && !pinBtn.contains(e.target)) closePinPanel();
  const ctxPopup = document.getElementById('ctx-popup');
  const inSecondary = e.target.closest('#msg-modal, #memory-modal, #topic-delete-modal');
  const secondaryOpen = document.getElementById('msg-modal')?.classList.contains('open')
    || document.getElementById('memory-modal')?.classList.contains('open')
    || document.getElementById('topic-delete-modal')?.classList.contains('open');
  if (ctxPopup && !ctxPopup.contains(e.target) && !e.target.closest('.user-ctx') && !inSecondary && !secondaryOpen) {
    ctxPopup.classList.remove('open');
  }
  if (!procStatusPopup.contains(e.target) && e.target !== procStatusBtn && !procStatusBtn.contains(e.target)) {
    procStatusPopup.classList.remove('open');
  }
});
// ── file viewer ───────────────────────────────────────────────────────────────

const _TEXT_EXTS = new Set(['txt','md','py','js','ts','jsx','tsx','json','yaml','yml',
  'toml','ini','cfg','conf','sh','bash','zsh','fish','rb','go','rs','java','c','cpp',
  'h','hpp','cs','php','swift','kt','kts','lua','r','sql','html','css','xml','svg',
  'log','env','gitignore','dockerfile','makefile','lock','csv','tsv']);

function _isTextPath(path) {
  const ext = (path.split('.').pop() || '').toLowerCase();
  return _TEXT_EXTS.has(ext) || !path.includes('.');
}

function openFileViewer(path, line, endLine) {
  document.getElementById('file-modal')?.remove();

  const modal = document.createElement('div');
  modal.id = 'file-modal';

  const box = document.createElement('div');
  box.id = 'file-modal-box';

  const header = document.createElement('div');
  header.id = 'file-modal-header';

  const pathEl = document.createElement('span');
  pathEl.id = 'file-modal-path';
  pathEl.textContent = path + (line ? ':' + line + (endLine && endLine !== line ? '-' + endLine : '') : '');

  const closeBtn = document.createElement('button');
  closeBtn.id = 'file-modal-close';
  closeBtn.textContent = '×';
  const closeModal = () => modal.remove();
  closeBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
  const escHandler = e => { if (e.key === 'Escape') { closeModal(); document.removeEventListener('keydown', escHandler); } };
  document.addEventListener('keydown', escHandler);

  header.appendChild(pathEl);
  header.appendChild(closeBtn);

  const body = document.createElement('div');
  body.id = 'file-modal-body';
  body.textContent = 'Loading…';

  box.appendChild(header);
  box.appendChild(body);
  modal.appendChild(box);
  document.body.appendChild(modal);

  const token = localStorage.getItem('squid_token');
  const params = new URLSearchParams({ path });
  if (token) params.set('token', token);
  fetch('/localfile?' + params)
    .then(async res => {
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        body.textContent = d.error || `Error ${res.status}`;
        return;
      }
      const text = await res.text();
      _renderFileViewer(body, text, line, endLine, path);
    })
    .catch(() => { body.textContent = 'Failed to load file.'; });
}

const _EXT_LANG = {
  py:'python', js:'javascript', ts:'typescript', jsx:'javascript', tsx:'typescript',
  json:'json', yaml:'yaml', yml:'yaml', toml:'toml', sh:'bash', bash:'bash',
  zsh:'bash', fish:'bash', rb:'ruby', go:'go', rs:'rust', java:'java',
  c:'c', cpp:'cpp', h:'c', hpp:'cpp', cs:'csharp', php:'php', swift:'swift',
  kt:'kotlin', lua:'lua', r:'r', sql:'sql', html:'html', css:'css',
  xml:'xml', svg:'xml', md:'markdown',
};

function _splitHighlightedLines(html) {
  const lines = [];
  let line = '';
  let open = [];
  let i = 0;
  while (i < html.length) {
    if (html[i] === '\n') {
      lines.push(line + '</span>'.repeat(open.length));
      line = open.join('');
      i++;
    } else if (html[i] === '<') {
      if (html.startsWith('</span>', i)) {
        open.pop();
        line += '</span>';
        i += 7;
      } else if (html.startsWith('<span', i)) {
        const end = html.indexOf('>', i);
        const tag = html.slice(i, end + 1);
        open.push(tag);
        line += tag;
        i = end + 1;
      } else {
        line += html[i++];
      }
    } else {
      line += html[i++];
    }
  }
  if (line || open.length) lines.push(line + '</span>'.repeat(open.length));
  return lines;
}

function _renderFileViewer(container, text, targetLine, endLine, path) {
  const rawLines = text.split('\n');
  if (rawLines.length && rawLines[rawLines.length - 1] === '') rawLines.pop();
  const numWidth = String(rawLines.length).length;

  let hlLines = null;
  if (typeof hljs !== 'undefined') {
    try {
      const ext = (path || '').split('.').pop().toLowerCase();
      const lang = _EXT_LANG[ext];
      const result = lang && hljs.getLanguage(lang)
        ? hljs.highlight(text, { language: lang })
        : hljs.highlightAuto(text);
      hlLines = _splitHighlightedLines(result.value);
    } catch {}
  }

  const wrap = document.createElement('div');
  wrap.className = 'fv-lines';

  rawLines.forEach((content, i) => {
    const n = i + 1;
    const inRange = targetLine && n >= targetLine && n <= (endLine || targetLine);
    const row = document.createElement('div');
    row.className = 'fv-line' + (inRange ? ' fv-target' : '');
    if (n === targetLine) row.id = 'fv-target';

    const num = document.createElement('span');
    num.className = 'fv-num';
    num.textContent = String(n).padStart(numWidth, ' ');

    const code = document.createElement('span');
    code.className = 'fv-code';
    if (hlLines?.[i] != null) {
      code.innerHTML = hlLines[i] || '​';
    } else {
      code.textContent = content;
    }

    row.appendChild(num);
    row.appendChild(code);
    wrap.appendChild(row);
  });

  container.innerHTML = '';
  container.appendChild(wrap);

  if (targetLine) requestAnimationFrame(() => {
    document.getElementById('fv-target')?.scrollIntoView({ block: 'center' });
  });
}

document.addEventListener('click', e => {
  const a = e.target.closest('a');
  if (!a) return;
  const href = a.getAttribute('href') || '';
  if (!href.startsWith('/localfile')) return;
  const url = new URL(a.href);
  const path = url.searchParams.get('path') || '';
  if (!_isTextPath(path)) return;
  e.preventDefault();
  const hm = url.hash.match(/^#L(\d+)(?:-L(\d+))?$/);
  const line = hm ? parseInt(hm[1], 10) : null;
  const endLine = hm?.[2] ? parseInt(hm[2], 10) : null;
  openFileViewer(path, line, endLine);
});

initHistoryScroll();
initPromptHistory();
initStats();
initTopicsView();
initAliases();
initQuota();
initCreds();
initCodexQuota();
initCodexCreds();
initCursorQuota();
updateActiveQuotaGauge();
initPullToRefresh();
startProcPoll();
showBootBanner();
try {
  const saved = JSON.parse(localStorage.getItem('squid_sticky_chip') || 'null');
  if (saved?.topic) setTopicChip(saved.topic, saved.agent || null, saved.adhoc || false);
} catch { /* ignore */ }

// Patch bookmarklet hrefs with the actual origin (avoids hardcoding the port).
document.querySelectorAll('.creds-bookmarklet').forEach(a => {
  a.href = a.href.replace('SQUID_ORIGIN', location.origin);
});

// ── bookmarklet credential import ─────────────────────────────────────────────
(function () {
  const hash = location.hash;
  if (!hash.startsWith('#squid-import/')) return;
  history.replaceState(null, '', location.pathname);
  const parts = hash.slice('#squid-import/'.length).split('/');
  const type = parts[0];

  function _showImportResult(popupId, statusId, ok) {
    const popup = document.getElementById(popupId);
    const status = document.getElementById(statusId);
    if (popup) popup.classList.add('open');
    if (status) {
      status.textContent = ok ? 'Imported!' : 'Import failed';
      status.style.color = ok ? '#69a875' : '#a06655';
    }
  }

  if (type === 'claude' && parts.length >= 3) {
    const org_id = decodeURIComponent(parts[1]);
    const session_key = decodeURIComponent(parts[2]);
    fetch('/config/creds', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ org_id, session_key }),
    })
      .then(r => r.json())
      .then(d => _showImportResult('quota-creds-popup', 'creds-status', !!d.ok))
      .catch(() => _showImportResult('quota-creds-popup', 'creds-status', false));
  } else if (type === 'claude-org' && parts.length >= 2) {
    const org_id = decodeURIComponent(parts[1]);
    const orgInput = document.getElementById('creds-org');
    const popup = document.getElementById('quota-creds-popup');
    const status = document.getElementById('creds-status');
    if (orgInput) orgInput.value = org_id;
    if (popup) popup.classList.add('open');
    if (status) { status.textContent = 'Org ID filled — paste session key below'; status.style.color = '#888'; }
    document.getElementById('creds-key')?.focus();
  } else if (type === 'codex' && parts.length >= 2) {
    const token = decodeURIComponent(parts[1]);
    fetch('/config/creds/codex', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    })
      .then(r => r.json())
      .then(d => _showImportResult('codex-creds-popup', 'codex-creds-status', !!d.ok))
      .catch(() => _showImportResult('codex-creds-popup', 'codex-creds-status', false));
  }
})();
