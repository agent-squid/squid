
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

const DRIVER_MODEL_HINTS = Object.freeze({
  claude:   'e.g. claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-7',
  codex:    'e.g. o4-mini, o3',
  cursor:   'model (optional)',
  opencode: 'e.g. opencode/deepseek-v4-flash-free, anthropic/claude-sonnet-4-6',
});

const AGENT_THEME_COLORS = Object.freeze({
  claude: '#AE5332',
  codex: '#7070a0',
  cursor: '#FFFFFF',
  opencode: '#CFCECD',
  deepseek: '#4d9de0',
  antigravity: '#4ea1ff',
  copilot: '#ff5db1',
  default: '#888888',
});

let _backendMetadata = {};

function agentThemeColor(backend) {
  const configured = _backendMetadata[backend]?.color;
  if (configured) return configured;
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
  return _backendMetadata[backend]?.label || backend || 'Agent';
}

function backendModelHint(backend) {
  const driver = _backendMetadata[backend]?.driver || backend;
  return DRIVER_MODEL_HINTS[driver] || 'model (optional)';
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

  let remoteUrl = null, remoteReason = 'error';
  try {
    const res = await fetch('/remote');
    const data = await res.json();
    remoteUrl = data.url || null;
    remoteReason = data.reason || null;
  } catch {}

  const authUrl = remoteUrl;

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
const _memoryInjectedInto = {}; // `${topic}@${agent|_}` → topic memory revision sent to the current session
let _agentsCache = null;
let _agentsCachePromise = null;
let _squidHome = '/tmp/squid'; // updated from /health on first loadAgents()
let _activePollImmediate = null; // fn to trigger an immediate status poll for the active stream

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
  delete _memorySelectionOverrides[_memoryOverrideKey(topic, agent, false)];
}

// ── topic chip ────────────────────────────────────────────────────────────────

const topicChipEl = document.getElementById('topic-chip');
const chipRow = document.getElementById('chip-row');
const chipFilterBtn = document.getElementById('chip-filter-btn');
let stickyChip = null; // { topic, agent, adhoc } | null
let editingExpandedSlug = false;
let expandedSlugEditToken = 0;

function setTopicChip(topic, agent, adhoc = false, lookback = 0) {
  editingExpandedSlug = false;
  expandedSlugEditToken++;
  stickyChip = { topic, agent, adhoc, lookback };
  // Don't persist a sessioned default chip — #default is adhoc-first; session there is ephemeral
  if (topic !== 'default' || adhoc) {
    localStorage.setItem('squid_sticky_chip', JSON.stringify({ topic, agent, adhoc, lookback }));
  }

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
  chipRow.hidden = false;
  chipFilterBtn.hidden = false;
  input.placeholder = 'message…';
  updateActiveQuotaGauge();
  updatePinCount();
  updateInContextMarkers();
  _lastContextIndicatorKey = _contextIndicatorKeyFrom(topic, agent, adhoc, lookback);
}

function clearTopicChip() {
  stickyChip = null;
  localStorage.removeItem('squid_sticky_chip');
  topicChipEl.classList.remove('visible', 'needs-agent');
  chipRow.hidden = true;
  chipFilterBtn.hidden = true;
  input.placeholder = '#topic or #topic@agent message…';
  updateActiveQuotaGauge();
  _lastContextIndicatorKey = '';
}

topicChipEl.addEventListener('click', () => {
  if (!stickyChip) return;
  const prompt = input.value;
  let tag = `#${stickyChip.topic}`;
  if (stickyChip.agent) tag += `@${stickyChip.agent}`;
  if (stickyChip.adhoc) tag += `!${stickyChip.lookback || ''}`;
  clearTopicChip();
  editingExpandedSlug = true;
  expandedSlugEditToken++;
  input.value = prompt ? `${tag} ${prompt}` : tag;
  input.setSelectionRange(tag.length, tag.length);
  input.dispatchEvent(new Event('input'));
  input.focus();
});

chipFilterBtn.addEventListener('click', () => {
  if (!stickyChip) return;
  if (stickyChip.agent) filterByAgent(stickyChip.topic, stickyChip.agent, stickyChip.adhoc, stickyChip.lookback || 0);
  else filterByTopic(stickyChip.topic);
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
  // bare topic switch: #topic, #topic@agent, #topic!N, or #topic@agent!N with no message
  // switches chip only.
  const mb = text.match(/^#(\w+)(?:@(\w+))?(?:!(\d*))?$/);
  if (mb) {
    return {
      topic: mb[1].toLowerCase(),
      agent: mb[2] || null,
      adhoc: mb[3] !== undefined,
      lookback: mb[3] ? Math.min(parseInt(mb[3]), 20) : 0,
      message: '',
    };
  }
  return { topic: 'default', agent: null, adhoc: true, lookback: 0, message: text };
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
  applyHistoryFilter({ topic, agent: null, adhoc: null });
}

function filterByAgent(topic, agent, adhoc = false, lookback = 0) {
  setTopicChip(topic, agent, adhoc, lookback);
  applyHistoryFilter({ topic, agent, adhoc });
}

function applyHistoryFilter(filter) {
  if (searchActive) {
    searchActive = false;
    searchState = null;
    searchLoading = false;
    document.getElementById('search-bar').classList.remove('active');
    document.querySelectorAll('.search-result-item, #messages > .cmd-feedback.search-no-results').forEach(el => el.remove());
  }
  reloadHistory(filter);
}

function clearFilter() {
  if (searchActive && searchState) {
    searchState = { ...searchState, topic: null, agent: null, adhoc: null, explicitAll: false };
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
  invalidateHistoryLoad();
  if (topSentinel) { topSentinel.remove(); topSentinel = null; }
  document.querySelectorAll('.history-item, .boot-banner, .tool-block-history').forEach(el => el.remove());
  // Remove live (non-history) messages too — completed ones are in the DB and will reload
  document.querySelectorAll('#messages > .msg:not(.msg-thinking), #messages > .msg-thinking-done, #messages > .msg-time, #messages > .stats').forEach(el => el.remove());
  _updateFilterBadge();
  initHistoryScroll();
}

function _updateFilterBadge() {
  const badge = document.getElementById('filter-badge');
  const labelEl = document.getElementById('filter-badge-label');
  const activeState = (searchActive && searchState) ? searchState : historyFilter;
  const { topic, agent, adhoc } = activeState;
  const explicitAll = !!activeState.explicitAll;

  if (!topic && !agent && !explicitAll) {
    badge.classList.remove('active');
    return;
  }

  labelEl.innerHTML = '';
  const addSegment = (kind, content, remove) => {
    const segment = document.createElement('span');
    segment.className = `filter-scope-segment filter-scope-${kind}`;
    segment.appendChild(content);
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'filter-scope-remove';
    x.setAttribute('aria-label', `Remove ${kind} filter`);
    x.addEventListener('click', e => { e.stopPropagation(); remove(); });
    segment.appendChild(x);
    segment.addEventListener('click', editActiveFilter);
    labelEl.appendChild(segment);
  };

  if (topic || explicitAll) {
    const t = document.createElement('span');
    t.className = 'tag-topic';
    t.textContent = '#' + (explicitAll ? 'all' : topic);
    addSegment('topic', t, () => removeFilterSegment('topic'));
  }
  if (agent) {
    const lane = document.createElement('span');
    const a = document.createElement('span');
    a.className = 'tag-agent';
    a.textContent = '@' + agent;
    setAgentSlugColor(a, agent);
    lane.appendChild(a);
    if (adhoc === true) {
      const ad = document.createElement('span');
      ad.className = 'tag-adhoc';
      ad.textContent = '!';
      setAgentSlugColor(ad, agent);
      lane.appendChild(ad);
    } else if (adhoc === null) {
      const both = document.createElement('span');
      both.className = 'tag-adhoc';
      both.textContent = '*';
      setAgentSlugColor(both, agent);
      lane.appendChild(both);
    }
    addSegment('agent', lane, () => removeFilterSegment('agent'));
  }
  badge.classList.add('active');
}

function removeFilterSegment(kind) {
  const active = (searchActive && searchState) ? searchState : historyFilter;
  const next = { ...active };
  if (kind === 'topic') {
    next.topic = null;
    next.explicitAll = false;
  } else {
    next.agent = null;
    next.adhoc = null;
  }

  if (searchActive && searchState) {
    searchState = next;
    searchLoading = false;
    document.querySelectorAll('.search-result-item, #messages > .cmd-feedback.search-no-results').forEach(el => el.remove());
    _updateFilterBadge();
    loadSearchResults();
  } else {
    reloadHistory({ topic: next.topic || null, agent: next.agent || null, adhoc: next.adhoc ?? null });
  }
}

// ── history pagination (display) ─────────────────────────────────────────────

let historyOffset = 0;
let historyExhausted = false;
let historyLoading = false;
let topSentinel = null;
let historyGeneration = 0;
let historyObserver = null;
const pendingPollTimers = new WeakMap();

function invalidateHistoryLoad() {
  historyGeneration++;
  historyLoading = false;
  if (historyObserver) {
    historyObserver.disconnect();
    historyObserver = null;
  }
}

function cancelPendingPoll(bubble) {
  const timer = pendingPollTimers.get(bubble);
  if (timer) clearInterval(timer);
  pendingPollTimers.delete(bubble);
}

function reconcilePendingBubble(msgId, preferredBubble) {
  if (msgId == null) return;
  messages.querySelectorAll(`.msg-thinking[data-msg-id="${msgId}"]`).forEach(bubble => {
    if (bubble === preferredBubble) return;
    cancelPendingPoll(bubble);
    bubble.remove();
  });
}

// ── search state ──────────────────────────────────────────────────────────────
let searchActive = false;
let searchState = null;  // { topic, agent, adhoc, explicitAll, keywords }
let searchLoading = false;

let promptHistory = [];   // newest first, in-memory, seeded from DB
let promptHistoryPos = -1; // -1 = editing draft; 0..N = navigating history
let promptDraft = '';      // stashed current input while navigating
let promptDraftChip = null; // stashed chip state while navigating history
let commandEditRestore = null; // prompt replaced by clicking an editable search/filter badge
let _draftSaveTimer = null;

function createTopSentinel() {
  const el = document.createElement('div');
  el.id = 'history-sentinel';
  return el;
}

async function loadHistory() {
  if (historyExhausted || historyLoading) return;
  historyLoading = true;
  const generation = historyGeneration;

  let data;
  try {
    let url = `/history?offset=${historyOffset}&limit=5`;
    if (historyFilter.topic) url += `&topic=${encodeURIComponent(historyFilter.topic)}`;
    if (historyFilter.agent) url += `&agent=${encodeURIComponent(historyFilter.agent)}`;
    if (historyFilter.adhoc != null) url += `&adhoc=${historyFilter.adhoc}`;
    const res = await fetch(url);
    data = await res.json();
  } catch {
    if (generation === historyGeneration) historyLoading = false;
    return;
  }

  // Search/filter navigation may have superseded this request while it was in flight.
  if (generation !== historyGeneration) return;

  const { items, has_more } = data;
  const prevHeight = messages.scrollHeight;
  const fragment = document.createDocumentFragment();

  for (const item of [...items].reverse()) {
    if (!item.content && item.status !== 'pending') continue;

    // Skip if a bubble for this message is already in the DOM — e.g. an
    // in-progress live (SSE) bubble that survived a search → back round-trip.
    // Without this, loadHistory would render a second, polling-driven bubble
    // for the same message alongside the live one.
    if (item.id != null && messages.querySelector(`[data-msg-id="${item.id}"]`)) continue;

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
  if (historyObserver) historyObserver.disconnect();
  topSentinel = createTopSentinel();
  messages.insertBefore(topSentinel, messages.firstChild);

  historyObserver = new IntersectionObserver(
    (entries) => { if (entries[0].isIntersecting) loadHistory(); },
    { root: messages, threshold: 0 },
  );
  historyObserver.observe(topSentinel);
}

// ── keyword search ────────────────────────────────────────────────────────────

function parseScopeInput(text, { allowAll = false } = {}) {
  const scope = (text || '').trim();
  if (!scope) return undefined;
  if (/^#all$/i.test(scope)) {
    return allowAll
      ? { topic: null, agent: null, adhoc: null, explicitAll: true }
      : null;
  }

  const topicMatch = scope.match(/^#([\w-]+)(?:@([\w-]+)([!*])?)?$/);
  if (topicMatch) {
    const agent = topicMatch[2] || null;
    const mode = topicMatch[3] || '';
    return {
      topic: topicMatch[1].toLowerCase(),
      agent,
      adhoc: agent ? (mode === '*' ? null : mode === '!') : null,
      explicitAll: false,
    };
  }

  const agentMatch = scope.match(/^@([\w-]+)([!*])?$/);
  if (agentMatch) {
    const mode = agentMatch[2] || '';
    return {
      topic: null,
      agent: agentMatch[1],
      adhoc: mode === '*' ? null : mode === '!',
      explicitAll: false,
    };
  }
  return null;
}

function parseSearchInput(text) {
  const rest = text.trim();
  const match = rest.match(/^(\S+)[ \t]+([\s\S]*)$/);
  if (match && (match[1].startsWith('#') || match[1].startsWith('@'))) {
    const scope = parseScopeInput(match[1], { allowAll: true });
    if (scope) return { ...scope, explicitScope: true, keywords: match[2].trim() };
  }
  return { topic: null, agent: null, adhoc: null, explicitAll: false, explicitScope: false, keywords: rest };
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
  const escapedKeywords = [...new Set(keywords.filter(Boolean))]
    .sort((a, b) => b.length - a.length)
    .map(kw => kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (!escapedKeywords.length) return;
  const pattern = new RegExp(escapedKeywords.join('|'), 'gi');
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const p = node.parentElement;
      if (!p) return NodeFilter.FILTER_REJECT;
      if (p.closest('script, style')) return NodeFilter.FILTER_REJECT;
      if (p.closest('.response-header, .history-prompt-full, .user-ctx')) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const text = node.textContent;
    pattern.lastIndex = 0;
    let match = pattern.exec(text);
    if (!match) continue;

    const fragment = document.createDocumentFragment();
    let offset = 0;
    do {
      if (match.index > offset) fragment.appendChild(document.createTextNode(text.slice(offset, match.index)));
      const mark = document.createElement('mark');
      mark.className = 'search-kw-highlight';
      mark.textContent = match[0];
      fragment.appendChild(mark);
      offset = match.index + match[0].length;
      match = pattern.exec(text);
    } while (match);
    if (offset < text.length) fragment.appendChild(document.createTextNode(text.slice(offset)));
    node.parentNode.replaceChild(fragment, node);
  }
}

function startSearch(rawArgs) {
  const parsed = parseSearchInput(rawArgs);

  if (!parsed.keywords) {
    showCmdFeedback('Usage: /s [#topic[@agent[!|*]] | @agent[!|*] | #all] keywords…');
    return;
  }

  let topic, agent, adhoc;
  const explicitAll = parsed.explicitAll;
  if (parsed.explicitScope) {
    // explicit scope typed in command overrides the active filter
    topic = parsed.topic;
    agent = parsed.agent || null;
    adhoc = parsed.adhoc;
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

  searchState = { topic, agent, adhoc, explicitAll, keywords: parsed.keywords };
  searchActive = true;
  searchLoading = false;

  // Stop history scroll
  if (topSentinel) { topSentinel.remove(); topSentinel = null; }
  invalidateHistoryLoad();

  // Clear pane
  document.querySelectorAll('.history-item, .boot-banner, .search-result-item, .tool-block-history').forEach(el => el.remove());
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
  invalidateHistoryLoad();
  _updateFilterBadge();
  initHistoryScroll();
}

function recordPrompt(text) {
  const t = text.trim();
  if (!t) return;
  promptHistory = [t, ...promptHistory.filter(x => x !== t)].slice(0, 200);
  promptHistoryPos = -1;
  promptDraft = '';
  promptDraftChip = null;
}

function formatPromptHistoryEntry(topic, agent, adhoc, _lookback, message) {
  const prompt = message.trim();
  const route = promptHistoryRoute(topic, agent, adhoc);
  if (!route) return prompt;
  return `${route} ${prompt}`;
}

function splitPromptHistoryEntry(entry) {
  const text = String(entry || '').trim();
  const match = text.match(/^(#\w+(?:@\w+)?(?:!\d*)?)\s+([\s\S]+)$/);
  if (!match) return { route: '', prompt: text };
  return { route: match[1], prompt: match[2].trim() };
}

function promptHistoryRoute(topic, agent, adhoc) {
  if (!topic || (topic === 'default' && !agent)) return '';
  let route = `#${topic}`;
  if (agent) route += `@${agent}`;
  if (adhoc) route += '!';
  return route;
}

function normalizePromptHistoryRoute(route) {
  const match = String(route || '').match(/^#(\w+)(?:@(\w+))?(!)?\d*$/);
  if (!match) return '';
  return promptHistoryRoute(match[1].toLowerCase(), match[2] || null, !!match[3]);
}

function applyPromptHistoryEntry(entry) {
  const { route, prompt } = splitPromptHistoryEntry(entry);
  if (route) {
    const match = route.match(/^#(\w+)(?:@(\w+))?(!(?:(\d+))?)?$/);
    if (match) {
      setTopicChip(
        match[1].toLowerCase(),
        match[2] || null,
        !!match[3],
        0,
      );
    }
  }
  input.value = prompt;
  input.setSelectionRange(prompt.length, prompt.length);
  resizeComposer();
}

function currentPromptHistoryRoute() {
  if (!stickyChip) return '';
  return promptHistoryRoute(stickyChip.topic, stickyChip.agent, stickyChip.adhoc);
}

function matchingPromptHistory(value, limit = 8) {
  const prefix = value.trimStart().toLowerCase();
  if (!prefix) return promptHistory.slice(0, limit);

  const currentRoute = currentPromptHistoryRoute().toLowerCase();
  return promptHistory
    .map((entry, recency) => ({ entry, recency, ...splitPromptHistoryEntry(entry) }))
    .filter(item => item.prompt.toLowerCase().startsWith(prefix))
    .sort((a, b) => {
      const aCurrent = normalizePromptHistoryRoute(a.route).toLowerCase() === currentRoute;
      const bCurrent = normalizePromptHistoryRoute(b.route).toLowerCase() === currentRoute;
      return Number(bCurrent) - Number(aCurrent) || a.recency - b.recency;
    })
    .slice(0, limit)
    .map(item => item.entry);
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
  updateInContextMarkers();
  updatePinCount();
  if (pinPanel.classList.contains('open')) renderPinPanel();
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
  { name: 'f', alias: 'filter', desc: 'filter — e.g. /f #topic  ·  /f @agent!  ·  /f reset', args: true },
  { name: 's', alias: 'search', desc: 'search — e.g. /s #topic kw  ·  /s @agent! kw  ·  /s #all kw', args: true },
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
  const mf = t.match(/^(?:f|filter)(?:\s+([\s\S]*))?$/i);
  if (mf) {
    const args = (mf[1] || '').trim();
    if (/^reset$/i.test(args)) return { command: 'filter_reset' };
    return { command: 'filter', args };
  }
  const m = t.match(/^deq(?:\s+(-?\d+))?$/i);
  if (m) return { command: 'deq', pos: m[1] != null ? parseInt(m[1]) : null };
  if (message.trim().startsWith('/')) {
    const ms = t.match(/^s(?:earch)?(?:\s+([\s\S]*))?$/i);
    if (ms) return { command: 'search', args: (ms[1] || '').trim() };
  }
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
    const scope = parseScopeInput(cmd.args);
    if (scope === null) {
      showCmdFeedback('Usage: /f [#topic[@agent[!|*]] | @agent[!|*] | reset]');
      return;
    }
    if (scope) applyHistoryFilter(scope);
    else if (agent) applyHistoryFilter({ topic, agent, adhoc });
    else applyHistoryFilter({ topic, agent: null, adhoc: null });
    return;
  }
  if (cmd.command === 'filter_reset') {
    clearFilter();
    return;
  }

  if (cmd.command === 'search') {
    if (!cmd.args) {
      showCmdFeedback('Usage: /s [#topic[@agent[!|*]] | @agent[!|*] | #all] keywords…');
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
  editingExpandedSlug = false;
  expandedSlugEditToken++;
  const text = input.value.trim();
  if (!text) return;
  commandEditRestore = null;
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
    if (cmd.command === 'restart') {
      clearTimeout(_draftSaveTimer);
      localStorage.removeItem('squid_draft');
    }
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
  recordPrompt(formatPromptHistoryEntry(topic, agent, adhoc, lookback, message));
  localStorage.removeItem('squid_draft');
  sendMessage(text);
});

function fmtCtxLabel(adhoc, pinCount = 0, mem = false, sessionTurnCount = 0) {
  const sessionLabel = !adhoc && sessionTurnCount > 0 ? `sess ${sessionTurnCount}t` : 'sess';
  const parts = [adhoc ? 'adhoc' : sessionLabel];
  if (mem) parts.push('mem');
  if (pinCount > 0) parts.push(`${pinCount}p`);
  return parts.join(' · ');
}

function setCtxLabel(spanEl, adhoc, pinCount = 0, mem = false, sessionTurnCount = 0) {
  spanEl.textContent = `ctx: ${fmtCtxLabel(adhoc, pinCount, mem, sessionTurnCount)}`;
}

const _lookbackUnselected = new Set(); // cleared when the active !N candidate set changes; never persisted
let _lastLookbackSelectionKey = '';
let _lastContextIndicatorKey = '';

function _allLookbackItems(adhoc, lookback) {
  if (!adhoc || lookback <= 0) return [];
  return [...document.querySelectorAll('#messages .history-item.assistant:not(.msg-thinking)')]
    .filter(el => el.dataset.msgId)
    .map(el => {
      const id = parseInt(el.dataset.msgId);
      return { id, el, topic: el.dataset.topic || 'default', agent: el.dataset.agent || null, session_id: el.dataset.sessionId || null, content: _messageBodyText(el) };
    })
    .sort((a, b) => a.id - b.id)
    .slice(-lookback);
}

function _messageBodyText(bubbleEl) {
  const bodyEls = [...bubbleEl.children].filter(el =>
    el.tagName === 'DIV'
    && !el.classList.contains('response-header')
    && !el.classList.contains('history-prompt-full')
  );
  return bodyEls[bodyEls.length - 1]?.innerText || '';
}

function _lookbackSelectionKey(adhoc, lookback, items) {
  return `${adhoc ? 1 : 0}:${lookback}:${items.map(item => item.id).join(',')}`;
}

function _activeLookbackItems(adhoc, lookback) {
  const allItems = _allLookbackItems(adhoc, lookback);
  const selectionKey = _lookbackSelectionKey(adhoc, lookback, allItems);
  if (selectionKey !== _lastLookbackSelectionKey) {
    _lookbackUnselected.clear();
    _lastLookbackSelectionKey = selectionKey;
  }
  return allItems.filter(item => !_lookbackUnselected.has(item.id));
}

function updateInContextMarkers() {
  const { topic, agent, adhoc, lookback } = _currentContextTarget();

  const taKey   = `${topic}@${agent || '_'}`;
  const sid     = (!adhoc && agent) ? (_sessionIds[taKey] || null) : null;
  const injected = sid ? (getInjectedInto()[sid] || []) : [];

  const activeItems = adhoc && lookback > 0 ? _activeLookbackItems(adhoc, lookback) : [];
  const activeIdSet = new Set(activeItems.map(i => i.id));

  document.querySelectorAll('#messages .history-item.assistant:not(.msg-thinking)').forEach(el => {
    const ctxSpan = el.querySelector('.user-ctx');
    const msgId = el.dataset.msgId ? parseInt(el.dataset.msgId) : null;
    const wasInjected = msgId && injected.includes(msgId);

    // Orange dot = already in context (session continuity or prior injection), not "will inject"
    let inCtx = false;
    if (!adhoc) {
      inCtx = !!(sid && el.dataset.sessionId === sid);
    }
    inCtx = inCtx || !!wasInjected;
    const selectedForLookback = !!(adhoc && activeIdSet.has(msgId));

    if (ctxSpan) {
      ctxSpan.classList.toggle('ctx-live', inCtx);
      ctxSpan.classList.remove('ctx-injected');
    }

    // Reflect lookback selection state on the pin button
    const pinBtn = el.querySelector('.msg-pin-btn');
    el.classList.toggle('lookback-sel', selectedForLookback);
    if (pinBtn) pinBtn.classList.toggle('lookback-sel', selectedForLookback);
  });

  // Clear lookback-sel on any bubbles no longer in active set (e.g. after N shrinks)
  document.querySelectorAll('.msg-pin-btn.lookback-sel').forEach(btn => {
    const id = parseInt(btn.dataset.msgId);
    if (!activeIdSet.has(id)) btn.classList.remove('lookback-sel');
  });
}

function _contextIndicatorKeyFrom(topic, agent, adhoc, lookback) {
  return `${topic}@${agent || '_'}:${adhoc ? 'adhoc' : 'session'}:${lookback || 0}`;
}

function _contextIndicatorKey() {
  const { topic, agent, adhoc, lookback } = _currentContextTarget();
  return _contextIndicatorKeyFrom(topic, agent, adhoc, lookback);
}

function refreshContextIndicators({ force = false } = {}) {
  const key = _contextIndicatorKey();
  if (!force && key === _lastContextIndicatorKey) return;
  _lastContextIndicatorKey = key;
  updateInContextMarkers();
  updatePinCount();
  if (pinPanel.classList.contains('open')) renderPinPanel();
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

  input.value = '';
  setTopicChip(topic, agent, resolvedAdhoc, lookback);
  hideAutocomplete();
  resizeComposer();
}

async function _maybeCollapseExpandedSlug(force = false, allowCompletedPrompt = false) {
  if (!editingExpandedSlug && !allowCompletedPrompt) return;

  const val = input.value;
  const m = val.match(/^#(\w+)(?:@(\w+))?(!(\d*))? ([\s\S]+)$/);
  if (!m) return;

  const prompt = m[5];
  const promptStart = val.length - prompt.length;
  if (!force && (input.selectionStart < promptStart || input.selectionEnd < promptStart)) return;

  const token = expandedSlugEditToken;
  const topic = m[1].toLowerCase();
  const explicitAgent = m[2] || null;
  const adhocStr = m[3] || null;
  const lookback = adhocStr ? parseInt(m[4]) || 0 : 0;
  let agent = explicitAgent;
  let adhoc = !!adhocStr;

  if (!agent) {
    const topics = await _acTopics();
    if ((!allowCompletedPrompt && (!editingExpandedSlug || expandedSlugEditToken !== token)) || input.value !== val) return;
    const topicData = topics.find(t => t.name === topic);
    agent = topicData?.agent || null;
    if (!adhocStr) adhoc = !!topicData?.sticky_adhoc;
  }

  if (!force && (input.selectionStart < promptStart || input.selectionEnd < promptStart)) return;
  const selectionStart = Math.max(0, input.selectionStart - promptStart);
  const selectionEnd = Math.max(0, input.selectionEnd - promptStart);
  setTopicChip(topic, agent, adhoc, lookback);
  input.value = prompt;
  input.setSelectionRange(selectionStart, selectionEnd);
  input.dispatchEvent(new Event('input'));
}

input.addEventListener('input', () => {
  resizeComposer();
  updateAutocomplete();
  // Expanded #topic@agent!N text is still being edited. The chip creation path
  // is the commit point that selects lookback responses.
  if (!input.value.trimStart().startsWith('#')) refreshContextIndicators();
  updateActiveQuotaGauge();
  _maybePromoteSlug(input.value);
  clearTimeout(_draftSaveTimer);
  _draftSaveTimer = setTimeout(() => localStorage.setItem('squid_draft', input.value), 300);
  if (promptHistoryPos !== -1 && input.value !== promptHistory[promptHistoryPos]) {
    promptHistoryPos = -1;
  }
});

document.addEventListener('selectionchange', () => {
  if (document.activeElement === input) _maybeCollapseExpandedSlug();
});

input.addEventListener('blur', () => {
  _maybeCollapseExpandedSlug(true);
});

input.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowUp' && commandEditRestore !== null) {
    e.preventDefault();
    input.value = commandEditRestore;
    commandEditRestore = null;
    input.setSelectionRange(input.value.length, input.value.length);
    resizeComposer();
    return;
  }
  if (acOpen) {
    if (e.key === 'ArrowDown') { e.preventDefault(); acSel = Math.max(acSel - 1, 0); _acHighlight(); return; }
    if (e.key === 'ArrowUp')   { e.preventDefault(); acSel = Math.min(acSel + 1, acItems.length - 1); _acHighlight(); return; }
    if (e.key === 'Tab') { e.preventDefault(); _acSelect(acSel >= 0 ? acSel : 0); return; }
    if (e.key === 'Escape') { hideAutocomplete(); return; }
  }
  if (!acOpen && e.key === 'ArrowUp' && promptHistory.length) {
    if (promptHistoryPos >= 0) {
      e.preventDefault();
      promptHistoryPos = Math.min(promptHistoryPos + 1, promptHistory.length - 1);
      applyPromptHistoryEntry(promptHistory[promptHistoryPos]);
      return;
    }
    const _posBefore = input.selectionStart;
    requestAnimationFrame(() => {
      if (input.selectionStart === _posBefore) {
        promptDraft = input.value;
        promptDraftChip = stickyChip ? { ...stickyChip } : null;
        promptHistoryPos = 0;
        applyPromptHistoryEntry(promptHistory[0]);
      }
    });
    return;
  }
  if (!acOpen && e.key === 'ArrowDown' && promptHistoryPos >= 0) {
    e.preventDefault();
    promptHistoryPos--;
    if (promptHistoryPos < 0) {
      input.value = promptDraft;
      if (promptDraftChip) setTopicChip(promptDraftChip.topic, promptDraftChip.agent, promptDraftChip.adhoc, promptDraftChip.lookback || 0);
      else clearTopicChip();
      promptDraftChip = null;
    } else {
      applyPromptHistoryEntry(promptHistory[promptHistoryPos]);
    }
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
  if (
    e.key === 'Backspace'
    && stickyChip
    && input.selectionStart === 0
    && input.selectionEnd === 0
  ) {
    e.preventDefault();
    const prompt = input.value;
    let tag = `#${stickyChip.topic}`;
    if (stickyChip.agent) tag += `@${stickyChip.agent}`;
    if (stickyChip.adhoc) tag += `!${stickyChip.lookback || ''}`;
    clearTopicChip();
    editingExpandedSlug = true;
    expandedSlugEditToken++;
    input.value = prompt ? `${tag} ${prompt}` : tag;
    input.setSelectionRange(tag.length, tag.length);
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
  setCtxLabel(liveCtxSpan, adhoc);
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
  let completionRendered = false;
  let completionTimestampEl = null;
  let detachedPolling = false;
  let raw = '';
  let resolvedAgent = agent;  // updated by meta event
  let liveSessionTurnCount = 0;
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
    _activePollImmediate = null;
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

  function addCompletionTimestamp() {
    if (!completionTimestampEl && !statsEl && doneTime && firstDataReceived) {
      completionTimestampEl = addTimestamp(bubble, doneTime, false);
    }
  }

  function renderCompletionTools(tools) {
    const diffTools = changeTools(tools || []);
    for (const tool of diffTools) {
      const block = makeToolBlock(tool, msgId);
      block.classList.add('tool-block-history');
      messages.appendChild(block);
    }
    refreshAllRevertButtons();
  }

  function startStatusFallback(id) {
    if (statusTimer || !id) return;
    const doPoll = async () => {
      try {
        const statusRes = await fetch(`/chat/${id}/status`);
        if (!statusRes.ok) return;
        const data = await statusRes.json();
        if (data.status === 'done') {
          if (completionRendered || completedFromStatus) return;
          completedFromStatus = true;
          completionRendered = true;
          stopStatusFallback();
          doneTime = new Date().toISOString();
          freezeThinking();
          showStoredResponse(data.content || '');
          bubble.classList.add('history-item');
          if (!statsEl && data.stats) statsEl = addStats(bubble, data.stats, doneTime);
          if (statsEl) messages.appendChild(statsEl);
          liveSessionTurnCount = parseInt(data.session_turn_count || '0', 10) || liveSessionTurnCount;
          liveCtxSpan.dataset.sessionTurnCount = String(liveSessionTurnCount);
          setCtxLabel(liveCtxSpan, !!data.adhoc, _contextIds.length, _includeTopicMemory, liveSessionTurnCount);
          let storedTools = [];
          if (data.context) {
            try {
              storedTools = typeof data.context === 'string' ? JSON.parse(data.context) : data.context;
              if (!Array.isArray(storedTools)) storedTools = [];
            } catch {}
          }
          renderCompletionTools(storedTools.length ? storedTools : liveToolEvents);
          addCompletionTimestamp();
          scrollToBottom();
          controller.abort();
        } else if (data.status === 'error') {
          completedFromStatus = true;
          stopStatusFallback();
          freezeThinking();
          showError(data.content || 'Response interrupted.');
          controller.abort();
        } else if (data.status === 'pending' && data.content && !thinkingFrozen) {
          raw = data.content;
          // If DB has tool events the SSE stream didn't deliver, surface them
          if (data.context) {
            try {
              const dbTools = typeof data.context === 'string' ? JSON.parse(data.context) : data.context;
              if (Array.isArray(dbTools) && dbTools.length > liveToolEvents.length) {
                statusBuf = dbTools.map(toolLabel).join('\n') + '\nConnection interrupted — recovering…';
              }
            } catch {}
          }
          updateThinkingPreview();
        }
      } catch {}
    };
    _activePollImmediate = doPoll;
    statusTimer = setInterval(doPoll, 2000);
  }

  // Compute pinned IDs to inject — works for both session and adhoc turns
  let _effectiveAgent = agent || stickyChip?.agent || null;
  if (!_effectiveAgent) {
    try {
      const topics = await _acTopics();
      _effectiveAgent = topics.find(t => t.name === topic)?.agent || null;
    } catch {}
  }
  const _topicMemoryForSend = await _topicMemoryStateForSend(topic, _effectiveAgent, adhoc);
  const _includeTopicMemory = _topicMemoryForSend.selected;
  const _lookbackItems = _activeLookbackItems(adhoc, lookback);
  const _lookbackIds = _lookbackItems.map(item => item.id);
  const _pinnedIds = _injectablePinnedIds(topic, _effectiveAgent, adhoc, lookback);
  const _contextIds = [...new Set([..._lookbackIds, ..._pinnedIds])];

  try {
    startProcPoll({ hold: true });
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
    _lastLookbackSelectionKey = '';
    if (_includeTopicMemory && !adhoc) {
      const memoryKey = _memoryInjectedKey(topic, _effectiveAgent);
      _memoryInjectedInto[memoryKey] = _topicMemoryForSend.revision;
      delete _memorySelectionOverrides[_memoryOverrideKey(topic, _effectiveAgent, false)];
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
          // Match EventSource parsing: one optional space after "data:" is a
          // field separator, not payload content.
          const field = line.slice(5);
          const data = field.startsWith(' ') ? field.slice(1) : field;
          dataLineCount++;

          if (eventName === 'meta') {
            try {
              const meta = JSON.parse(data);
              resolvedAgent = meta.agent || (meta.backend !== 'auto' ? meta.backend : null);
              if (meta.backend) {
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
                liveCtxSpan.dataset.msgId = String(msgId);
                setCtxLabel(liveCtxSpan, adhoc);
                thinkingBubble.dataset.msgId = String(msgId);
                reconcilePendingBubble(msgId, thinkingBubble);
                bubble.dataset.topic = topic;
                if (resolvedAgent) bubble.dataset.agent = resolvedAgent;
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
              if (completionTimestampEl) {
                completionTimestampEl.remove();
                completionTimestampEl = null;
              }
              liveSessionTurnCount = parseInt(stats.session_turn_count || '0', 10) || 0;
              setCtxLabel(liveCtxSpan, !!stats.adhoc, _contextIds.length, _includeTopicMemory, liveSessionTurnCount);
              liveCtxSpan.dataset.sessionTurnCount = String(liveSessionTurnCount);
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
            // Preserve streaming chunk boundaries exactly. Repeated data fields
            // within one SSE event represent newlines in the source text.
            if (dataLineCount > 1) statusBuf += '\n';
            statusBuf += data;
            updateThinkingPreview();
            // no eventName reset — allow multi-line accumulation

          } else if (eventName === 'done') {
            if (completionRendered) {
              eventName = null;
              continue;
            }
            completionRendered = true;
            stopStatusFallback();
            freezeThinking();
            invalidateTopicsCache();
            doneTime = new Date().toISOString();
            if (firstDataReceived) {
              contentDiv.innerHTML = marked.parse(raw);
              bubble.classList.add('history-item');
              messages.appendChild(bubble);
              if (searchActive && searchState) {
                const kws = searchState.keywords.trim().split(/\s+/).filter(Boolean);
                if (kws.length) highlightTextNodes(bubble, kws);
              }
              if (statsEl) messages.appendChild(statsEl); // stats goes between bubble and diffs, not after
              renderCompletionTools(liveToolEvents);
              scrollToBottom();
            }
            // Update ctx label with pin count and store IDs for popup
            setCtxLabel(liveCtxSpan, adhoc, _contextIds.length, _includeTopicMemory, liveSessionTurnCount);
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
            completedFromStatus = true;
            completionRendered = true;
            freezeThinking();
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
        statusBuf += (statusBuf ? '\n' : '') + 'Connection interrupted — recovering…';
        updateThinkingPreview();
        startStatusFallback(msgId);
      } else {
        showError('Unable to start response stream.');
      }
    }
  } finally {
    releaseProcPoll();
    if (!detachedPolling) stopStatusFallback();
    if (!thinkingFrozen) {
      if (!detachedPolling) {
        // Stream ended without a 'done' event — switch to polling if we have a msgId
        if (!completedFromStatus && msgId && !userAborted) {
          detachedPolling = true;
          statusBuf += (statusBuf ? '\n' : '') + 'Connection interrupted — recovering…';
          updateThinkingPreview();
          startStatusFallback(msgId);
        } else if (!completedFromStatus) {
          freezeThinking();
        }
      }
    }
    if (!detachedPolling && !userAborted && !firstDataReceived && !completedFromStatus) {
      if (!bubble.parentNode) messages.appendChild(bubble);
      contentDiv.innerHTML = '<span class="msg-error">No response — backend may be rate-limited or unavailable.</span>';
    }
    addCompletionTimestamp();
  }

  // Quota is a backend-wide meter, so this before/after difference is only an
  // observational signal. Parallel prompts have overlapping windows and can
  // double-count each other's usage; provider reporting lag can shift usage to
  // a later turn. Do not treat or aggregate it as exact per-prompt attribution.
  // See ADR-0023.
  await new Promise(r => setTimeout(r, 1000));
  const quotaAfterSnapshot = await fetchQuotaForBackend(quotaBackend);
  const quotaBefore = quotaBeforeSnapshot?.raw ?? null;
  const quotaAfter = quotaAfterSnapshot?.raw ?? null;
  if (quotaBefore !== null && quotaAfter !== null && quotaAfter !== quotaBefore) {
    const d = Math.round((quotaAfter - quotaBefore) * 10) / 10;
    if (statsEl && d > 0) {
      const deltaEl = statsEl.querySelector('.stats-quota-delta');
      deltaEl.textContent = `  ·  +${d} pp`;
      deltaEl.title = 'Observed account quota-meter change; not exact message usage';
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

function _firstChangedNewRange(chunk) {
  let newLine = null;
  let fallback = null;
  let start = null;
  let end = null;
  for (const line of (chunk || '').split('\n')) {
    const hunk = line.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hunk) {
      if (start) return { line: start, endLine: end };
      if (fallback) return fallback;
      newLine = parseInt(hunk[1], 10);
      fallback = { line: newLine, endLine: newLine };
      continue;
    }
    if (newLine == null) continue;
    if (line.startsWith('+') && !line.startsWith('+++')) {
      if (!start) start = newLine;
      end = newLine;
      newLine++;
    } else if (line.startsWith('-') && !line.startsWith('---')) {
      continue;
    } else {
      if (start) return { line: start, endLine: end };
      newLine++;
    }
  }
  if (start) return { line: start, endLine: end };
  return fallback;
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

function _gitDiffFullDisplayPath(file) {
  return file?.old_path ? `${file.old_path} → ${file.path}` : (file?.path || '');
}

function _pathSegments(path) {
  return String(path || '').split(/[\\/]+/).filter(Boolean);
}

function _shortestUniquePathLabels(paths) {
  const segments = paths.map(_pathSegments);
  const depths = segments.map(parts => parts.length ? 1 : 0);
  let changed = true;
  while (changed) {
    changed = false;
    const labels = segments.map((parts, i) => parts.slice(-depths[i]).join('/'));
    const counts = new Map();
    labels.forEach(label => counts.set(label, (counts.get(label) || 0) + 1));
    labels.forEach((label, i) => {
      if (label && counts.get(label) > 1 && depths[i] < segments[i].length) {
        depths[i]++;
        changed = true;
      }
    });
  }
  return segments.map((parts, i) => parts.slice(-depths[i]).join('/'));
}

function _gitDiffDisplayPaths(files) {
  const fullLabels = files.map(_gitDiffFullDisplayPath);
  if (!window.matchMedia?.('(max-width: 768px)').matches) return fullLabels;
  const newLabels = _shortestUniquePathLabels(files.map(file => file.path || ''));
  const oldLabels = _shortestUniquePathLabels(files.map(file => file.old_path || ''));
  return files.map((file, i) => file.old_path ? `${oldLabels[i]} → ${newLabels[i]}` : newLabels[i]);
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
    const files = tool.files || [];
    const displayPaths = _gitDiffDisplayPaths(files);
    const fullDisplayPaths = files.map(_gitDiffFullDisplayPath);
    for (const [i, file] of files.entries()) {
      const status = file.status || '?';
      const displayPath = displayPaths[i];
      const fullDisplayPath = fullDisplayPaths[i];
      const chunk = fileDiffs.get(file.path) || fileDiffs.get(file.old_path) || '';
      const firstChangedRange = _firstChangedNewRange(chunk);

      const row = document.createElement('div');
      row.className = 'gitdiff-file-row';
      if (msgId && tool.repo) row.dataset.file = file.path;

      const fileToggle = document.createElement('button');
      fileToggle.className = 'gitdiff-file-toggle';
      fileToggle.title = fullDisplayPath;

      const isBinary = chunk.includes('Binary files') || !_isTextPath(file.path || '');
      if (isBinary) {
        fileToggle.textContent = `${status} ${displayPath}`;
        fileToggle.classList.add('gitdiff-file-toggle--no-diff');
        const badge = document.createElement('span');
        badge.className = 'gitdiff-binary-badge';
        badge.textContent = 'binary';
        fileToggle.appendChild(badge);
        row.appendChild(fileToggle);
      } else if (chunk) {
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
        openBtn.type = 'button';
        openBtn.className = 'gitdiff-file-open';
        openBtn.title = 'Open in file viewer';
        openBtn.setAttribute('aria-label', `Open ${file.path} in file viewer`);
        openBtn.textContent = 'view';
        openBtn.addEventListener('click', e => {
          e.stopPropagation();
          openFileViewer(_absPath, firstChangedRange?.line, firstChangedRange?.endLine);
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

function makeHistoryPromptToggle(prompt) {
  const promptToggle = document.createElement('span');
  promptToggle.className = 'history-prompt';
  const promptToggleText = document.createElement('span');
  promptToggleText.className = 'history-prompt-truncated';
  promptToggleText.textContent = truncate(prompt || '', 55);
  const promptCaret = document.createElement('span');
  promptCaret.className = 'history-prompt-caret';
  promptCaret.textContent = '▼';
  promptToggle.appendChild(promptToggleText);
  promptToggle.appendChild(promptCaret);
  const promptFullDiv = document.createElement('div');
  promptFullDiv.className = 'history-prompt-full';
  promptFullDiv.textContent = prompt || '';
  const togglePrompt = () => {
    const expanded = promptToggle.classList.toggle('expanded');
    promptCaret.textContent = expanded ? '▲' : '▼';
    promptFullDiv.classList.toggle('visible', expanded);
  };
  promptToggle.addEventListener('click', togglePrompt);
  return { promptToggle, promptFullDiv };
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
  const { promptToggle, promptFullDiv } = makeHistoryPromptToggle(item.prompt);
  asstHeaderText.appendChild(promptToggle);
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
  if (item.id != null) ctxSpan.dataset.msgId = String(item.id);
  const sessionTurnCount = parseInt(item.session_turn_count || '0', 10) || 0;
  setCtxLabel(ctxSpan, !!item.adhoc, _pc.pins.length, _pc.mem, sessionTurnCount);
  ctxSpan.dataset.sessionId = item.session_id || '';
  ctxSpan.dataset.cwd = item.stats?.cwd || '';
  ctxSpan.dataset.topic = item.topic || '';
  ctxSpan.dataset.sessionTurnCount = String(sessionTurnCount);
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
  bubble.dataset.msgId = String(item.id);
  bubble.dataset.topic = item.topic || 'default';
  if (item.agent) bubble.dataset.agent = item.agent;

  // Recovered pending rows have no preceding user bubble after a refresh, so
  // give them the same prompt-bearing header as completed history responses.
  const header = document.createElement('div');
  header.className = 'response-header';
  const headerText = document.createElement('span');
  headerText.className = 'response-header-text';
  const content = document.createElement('div');
  content.className = 'thinking-live';
  const asstLabel = item.agent || item.backend || '';
  headerText.appendChild(makeTopicTag(item.topic || 'default', asstLabel, {
    clickable: true,
    adhoc: !!item.adhoc,
    lookback: item.stats?.lookback ?? 0,
    backend: item.backend || null,
  }));
  headerText.appendChild(document.createTextNode('  '));
  const { promptToggle, promptFullDiv } = makeHistoryPromptToggle(item.prompt);
  headerText.appendChild(promptToggle);
  header.appendChild(headerText);
  bubble.appendChild(header);
  bubble.appendChild(promptFullDiv);
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
    if (!wipBubble.isConnected) {
      cancelPendingPoll(wipBubble);
      return;
    }
    count++;
    try {
      const res = await fetch(`/chat/${item.id}/status`);
      if (!res.ok) { clearInterval(timer); return; }
      const data = await res.json();
      if (data.status === 'done' || data.status === 'error') {
        clearInterval(timer);
        if (!wipBubble.parentNode) return;
        wipBubble.remove();
        const wipEl = appendHistoryItem(data, messages);
        if (wipEl && searchActive && searchState) {
          const kws = searchState.keywords.trim().split(/\s+/).filter(Boolean);
          if (kws.length) highlightTextNodes(wipEl, kws);
        }
        updateInContextMarkers();
        updatePinCount();
        if (pinPanel.classList.contains('open')) renderPinPanel();
        refreshAllRevertButtons();
        scrollToBottom();
      } else if (count >= MAX_POLLS) {
        clearInterval(timer);
        const content = wipBubble.querySelector('.thinking-live');
        if (content) content.innerHTML += '<br><span class="msg-error">Timed out.</span>';
      } else if (data.content) {
        // Show partial content while still generating, as a growing/scrolling
        // block (mirrors the live SSE preview) rather than a single truncated
        // line. The header span stays on top; partial output streams below and
        // the .thinking-live container scrolls once it exceeds its max-height.
        const live = wipBubble.querySelector('.thinking-live');
        if (live) {
          const loader = live.querySelector('.loader');
          if (loader) loader.remove();
          let stream = live.querySelector('.thinking-stream');
          if (!stream) {
            stream = document.createElement('div');
            stream.className = 'thinking-stream';
            live.appendChild(stream);
          }
          stream.textContent = data.content;
          live.scrollTop = live.scrollHeight;
        }
      }
    } catch { clearInterval(timer); }
  }, 2000);
  pendingPollTimers.set(wipBubble, timer);
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
  if (msgQd != null && msgQd > 0) {
    qdSpan.textContent = `  ·  +${msgQd} pp`;
    qdSpan.title = 'Observed account quota-meter change; not exact message usage';
  }
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

function fmtAxisNum(n) {
  const value = Number(n) || 0;
  const abs = Math.abs(value);
  const compact = (divisor, suffix) => {
    const scaled = value / divisor;
    return scaled.toLocaleString(undefined, {
      minimumFractionDigits: 0,
      maximumFractionDigits: 1,
    }) + suffix;
  };
  if (abs >= 1_000_000_000) return compact(1_000_000_000, 'B');
  if (abs >= 1_000_000) return compact(1_000_000, 'M');
  if (abs >= 1000) return compact(1000, 'K');
  if (abs >= 10 || abs === 0) return Math.round(value).toLocaleString();
  return Number(value.toFixed(2)).toLocaleString();
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
// Presentation details for coded gauge adapters. Backend-to-gauge routing comes
// from /health, never from backend or model naming conventions.
const QUOTA_CONFIG = {
  claude: {
    endpoint:     '/quota/claude',
    displayId:    'quota-display',
    pieArcId:     'quota-pie-arc',
    labelId:      'quota-label',
    pieC:         2 * Math.PI * 6,
    credsPopupId: 'quota-creds-popup',
    errorTitle:   'Claude usage unavailable · click for credentials',
  },
  codex: {
    endpoint:     '/quota/codex',
    displayId:    'codex-quota-display',
    pieArcId:     'codex-pie-arc',
    labelId:      'codex-quota-label',
    pieC:         2 * Math.PI * 6,
    credsPopupId: 'codex-creds-popup',
    errorTitle:   'Codex usage unavailable · click for credentials',
  },
  cursor: {
    endpoint:     '/quota/cursor',
    displayId:    'cursor-quota-display',
    pieArcId:     'cursor-pie-arc',
    labelId:      'cursor-quota-label',
    pieC:         2 * Math.PI * 6,
    credsPopupId: 'cursor-creds-popup',
    errorTitle:   'Cursor usage unavailable · click for info',
  },
  deepseek: {
    endpoint:     '/quota/deepseek',  // returns remaining pre-paid balance
    displayId:    'deepseek-quota-display',
    pieArcId:     'deepseek-pie-arc',
    labelId:      'deepseek-quota-label',
    pieC:         2 * Math.PI * 6,
    credsPopupId: 'deepseek-max-popup',
    errorTitle:   'DeepSeek balance unavailable',
    formatLabel:  (state) => state.displayText || '—',
  },
  static: {
    displayId:    'quota-display',
    pieArcId:     'quota-pie-arc',
    labelId:      'quota-label',
    pieC:         2 * Math.PI * 6,
  },
};

const quotaSnapshots = {};
// Per-backend runtime state. timer is the label-refresh interval handle.
// activeCount tracks in-flight messages; drives the 30s quota poll interval.
const quotaState = {};

function quotaStateFor(backend) {
  return quotaState[backend] ||= {
    raw: null, pct: null, resetAt: null, displayText: null,
    inFlight: false, timer: null, activeCount: 0,
  };
}

function gaugeTypeFor(backend) {
  return _backendMetadata[backend]?.gauge?.type || 'none';
}

function quotaConfigFor(backend) {
  return QUOTA_CONFIG[gaugeTypeFor(backend)] || null;
}

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
  if (!quotaConfigFor(backend)) return;
  quotaStateFor(backend).activeCount++;
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
  if (h >= 24) return `${(totalMin / 60 / 24).toFixed(1)}D`;
  return h > 0 ? `${h}:${m}` : `${m}m`;
}

function setVisibleQuotaBackend(backend) {
  const gaugeType = gaugeTypeFor(backend);
  activeQuotaBackend = QUOTA_CONFIG[gaugeType] ? backend : null;
  const displayIds = new Set(Object.values(QUOTA_CONFIG).map(cfg => cfg.displayId));
  for (const id of displayIds) document.getElementById(id)?.classList.add('quota-hidden');
  if (activeQuotaBackend) {
    const cfg = QUOTA_CONFIG[gaugeType];
    const displayEl = document.getElementById(cfg.displayId);
    displayEl?.classList.remove('quota-hidden', 'loaded', 'error');
    const snapshot = quotaSnapshots[activeQuotaBackend];
    if (snapshot?.status === 'loaded') {
      displayEl?.classList.add('loaded');
      if (displayEl) displayEl.title = snapshot.title ?? '';
      updateGaugeLabel(activeQuotaBackend);
    } else if (snapshot?.status === 'error') {
      displayEl?.classList.add('error');
      if (displayEl) displayEl.title = cfg.errorTitle ?? '';
      const label = document.getElementById(cfg.labelId);
      if (label) label.textContent = snapshot.text;
      const arc = document.getElementById(cfg.pieArcId);
      if (arc) arc.setAttribute('stroke-dasharray', `0 ${cfg.pieC}`);
    } else {
      updateGaugeLabel(activeQuotaBackend);
    }
  }
}

async function resolveActiveQuotaBackend() {
  const parsed = parseInput(input.value.trim());
  const topicName = parsed.topic || stickyChip?.topic || null;
  let agentName = parsed.agent || stickyChip?.agent || null;

  return resolveQuotaBackend(topicName, agentName);
}

async function resolveQuotaBackend(topicName, agentName) {
  if (!Object.keys(_backendMetadata).length) {
    try {
      const res = await fetch('/health');
      if (res.ok) _backendMetadata = (await res.json()).backends || {};
    } catch { /* gauge remains hidden until health is reachable */ }
  }
  if (!agentName && topicName) {
    const topics = await _acTopics();
    agentName = topics.find(t => t.name === topicName)?.agent || null;
  }
  if (!agentName) return 'claude';

  const agents = await _acAgents();
  const agent = agents.find(a => a.name === agentName);
  return agent?.backend || null;
}

async function updateActiveQuotaGauge() {
  const seq = ++quotaResolveSeq;
  const backend = await resolveActiveQuotaBackend();
  if (seq !== quotaResolveSeq) return;
  const previousBackend = activeQuotaBackend;
  setVisibleQuotaBackend(backend);
  if (activeQuotaBackend && activeQuotaBackend !== previousBackend) {
    fetchQuotaForBackend(activeQuotaBackend);
  }
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

function scheduleGaugeTick(backend) {
  const state = quotaStateFor(backend);
  if (!state.resetAt || state.resetAt <= Date.now()) return;
  state.timer = setTimeout(() => {
    updateGaugeLabel(backend);
    scheduleGaugeTick(backend);
  }, 10000);
}

function updateGaugeLabel(backend) {
  if (backend !== activeQuotaBackend) return;
  const cfg = quotaConfigFor(backend);
  const state = quotaStateFor(backend);
  if (!cfg) return;
  const label = document.getElementById(cfg.labelId);
  if (!label) return;
  if (state.displayText != null) {
    label.textContent = state.displayText;
  } else if (cfg.formatLabel) {
    label.textContent = cfg.formatLabel(state);
  } else if (state.pct == null) {
    label.textContent = '—';
  } else {
    const timeStr = quotaTimeText(state.resetAt);
    label.textContent = `${state.pct}%` + (timeStr ? ` in ${timeStr}` : '');
  }
  const arc = document.getElementById(cfg.pieArcId);
  if (arc && state.pct != null) {
    const filled = (state.pct / 100) * cfg.pieC;
    arc.setAttribute('stroke-dasharray', `${filled} ${cfg.pieC}`);
    arc.setAttribute('stroke', quotaGaugeColor(backend, state.pct));
  } else if (arc) {
    arc.setAttribute('stroke-dasharray', `0 ${cfg.pieC}`);
  }
}

function renderQuotaLoaded(backend, snapshot) {
  const cfg = quotaConfigFor(backend);
  const state = quotaStateFor(backend);
  state.raw = snapshot.raw;
  state.pct = snapshot.pct;
  state.resetAt = snapshot.resetAt;
  state.displayText = snapshot.displayText ?? null;

  if (backend === activeQuotaBackend) {
    const displayEl = document.getElementById(cfg.displayId);
    displayEl.classList.remove('error');
    displayEl.classList.add('loaded');
    displayEl.title = snapshot.title ?? '';
  }
  updateGaugeLabel(backend);
  if (state.timer) clearTimeout(state.timer);
  state.timer = null;
  scheduleGaugeTick(backend);

  setQuotaSnapshot(backend, {
    status: 'loaded',
    pct: snapshot.pct,
    resetAt: snapshot.resetAt,
    title: snapshot.title,
    displayText: snapshot.displayText ?? null,
  });
}

function showQuotaError(backend, text) {
  const cfg = quotaConfigFor(backend);
  if (!cfg) return;
  const state = quotaStateFor(backend);
  state.resetAt = null;
  if (state.timer) { clearTimeout(state.timer); state.timer = null; }

  setQuotaSnapshot(backend, { status: 'error', text });

  if (backend === activeQuotaBackend) {
    const displayEl = document.getElementById(cfg.displayId);
    displayEl.classList.remove('loaded');
    displayEl.classList.add('error');
    displayEl.title = cfg.errorTitle;
    const label = document.getElementById(cfg.labelId);
    if (label) label.textContent = text;
    const arc = document.getElementById(cfg.pieArcId);
    if (arc) arc.setAttribute('stroke-dasharray', `0 ${cfg.pieC}`);
  }
}

async function fetchQuotaForBackend(backend) {
  const cfg = quotaConfigFor(backend);
  if (!cfg) return null;
  const state = quotaStateFor(backend);
  if (state.inFlight) return state.raw == null ? null : { backend, ...state };
  state.inFlight = true;
  const label = backendDisplayName(backend);
  try {
    const res = await fetch(`/quota/backend/${encodeURIComponent(backend)}`);
    if (!res.ok) {
      showQuotaError(backend, res.status === 400 ? `${label} auth` : `${label} error`);
      return null;
    }
    const data = await res.json();
    if (data.status === 'none') return null;
    if (!data.status) {
      showQuotaError(backend, `${label} n/a`);
      return null;
    }
    const resetAt = typeof data.reset_at === 'number'
      ? data.reset_at * 1000
      : (data.reset_at ? new Date(data.reset_at).getTime() : null);
    const snapshot = {
      raw: data.raw ?? null,
      pct: data.used_percent == null ? null : Math.max(0, Math.min(100, Math.round(data.used_percent))),
      resetAt,
      title: data.title || '',
      // Percentage gauges build their label from pct + resetAt (for example,
      // "42% in 4:34"). `text` is reserved for non-percentage gauges such as
      // static labels, unlimited plans, and account balances.
      displayText: data.used_percent == null ? (data.text ?? null) : null,
    };
    if (gaugeTypeFor(backend) === 'deepseek' && snapshot.raw != null) {
      const max = parseFloat(localStorage.getItem(`deepseek-max-balance:${backend}`)
        || localStorage.getItem('deepseek-max-balance') || '');
      if (max > 0) {
        const spent = Math.max(0, max - snapshot.raw);
        snapshot.pct = Math.max(0, Math.min(100, Math.round((spent / max) * 100)));
        snapshot.title = `DeepSeek · ${spent.toFixed(2)} spent of ${max.toFixed(2)}`;
      }
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

async function fetchQuota() {
  return fetchQuotaForBackend('claude');
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
  quotaDisplay.addEventListener('click', () => {
    if (gaugeTypeFor(activeQuotaBackend) === 'claude') credsPopup.classList.toggle('open');
  });
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

function parseDeepSeekQuota(data) {
  const usd = data.balance_infos?.find(b => b.currency === 'USD');
  const cny = data.balance_infos?.find(b => b.currency === 'CNY');
  const info = usd || cny;
  if (!info) return null;
  const symbol = info.currency === 'USD' ? '$' : '¥';
  const balance = parseFloat(info.total_balance);
  const displayText = `${symbol}${balance.toFixed(2)}`;
  const maxStr = localStorage.getItem('deepseek-max-balance');
  const max = maxStr ? parseFloat(maxStr) : null;
  const spent = max ? Math.max(0, max - balance) : 0;
  const pct = (max && max > 0) ? Math.max(0, Math.min(100, Math.round((spent / max) * 100))) : 0;
  return {
    raw: balance,
    pct,
    resetAt: null,
    title: `DeepSeek · ${symbol}${spent.toFixed(2)} spent${max ? ` of ${symbol}${parseFloat(max).toFixed(2)}` : ' · click to set max'}`,
    displayText,
  };
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

function initDeepSeekQuota() {
  const cfg = QUOTA_CONFIG.deepseek;
  const displayEl = document.getElementById(cfg.displayId);
  if (!displayEl) return;
  displayEl.style.setProperty('--quota-accent', agentThemeColor('deepseek'));
  displayEl.innerHTML = `
    <svg id="deepseek-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
      <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
      <circle id="${cfg.pieArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('deepseek')}"
              stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
              transform="rotate(-90 9 9)"/>
    </svg>
    <span id="${cfg.labelId}">—</span>`;

  const popup = document.getElementById(cfg.credsPopupId);
  displayEl.addEventListener('click', () => popup.classList.toggle('open'));
  document.addEventListener('click', (e) => {
    if (!displayEl.contains(e.target) && !popup.contains(e.target))
      popup.classList.remove('open');
  });
}

function initDeepSeekMaxPopup() {
  const maxInput = document.getElementById('deepseek-max-input');
  const saveBtn  = document.getElementById('deepseek-max-save');
  const clearBtn = document.getElementById('deepseek-max-clear');
  const status   = document.getElementById('deepseek-max-status');
  if (!maxInput || !saveBtn) return;

  const storageKey = () => `deepseek-max-balance:${activeQuotaBackend || 'deepseek'}`;

  saveBtn.addEventListener('click', () => {
    const val = parseFloat(maxInput.value);
    if (!val || val <= 0) { status.textContent = 'enter a positive amount'; return; }
    localStorage.setItem(storageKey(), String(val));
    status.textContent = 'saved ✓';
    if (activeQuotaBackend) fetchQuotaForBackend(activeQuotaBackend);
    setTimeout(() => { status.textContent = ''; }, 2000);
  });

  clearBtn?.addEventListener('click', () => {
    localStorage.removeItem(storageKey());
    maxInput.value = '';
    status.textContent = 'cleared';
    if (activeQuotaBackend) fetchQuotaForBackend(activeQuotaBackend);
    setTimeout(() => { status.textContent = ''; }, 2000);
  });
}

function initCodexCreds() {
  const tokenInput = document.getElementById('codex-creds-token');
  const saveBtn    = document.getElementById('codex-creds-save');
  const status     = document.getElementById('codex-creds-status');
  const autoBtn    = document.getElementById('codex-creds-auto');
  const autoStatus = document.getElementById('codex-creds-auto-status');

  if (location.hostname !== '127.0.0.1' && location.hostname !== 'localhost') {
    autoBtn.style.display = 'none';
    autoStatus.style.display = 'none';
  }

  autoBtn.addEventListener('click', async () => {
    autoBtn.disabled = true;
    autoStatus.textContent = 'detecting…';
    try {
      const res = await fetch('/config/creds/codex/auto', { method: 'POST' });
      const data = await res.json();
      if (res.ok) {
        autoStatus.textContent = 'saved ✓';
        fetchCodexQuota();
      } else {
        autoStatus.textContent = data.error || 'failed';
      }
    } catch { autoStatus.textContent = 'error'; }
    autoBtn.disabled = false;
    setTimeout(() => { autoStatus.textContent = ''; }, 5000);
  });

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
  const orgInput   = document.getElementById('creds-org');
  const keyInput   = document.getElementById('creds-key');
  const saveBtn    = document.getElementById('creds-save');
  const status     = document.getElementById('creds-status');
  const autoBtn    = document.getElementById('creds-auto');
  const autoStatus = document.getElementById('creds-auto-status');

  if (location.hostname !== '127.0.0.1' && location.hostname !== 'localhost') {
    autoBtn.style.display = 'none';
    autoStatus.style.display = 'none';
  }

  autoBtn.addEventListener('click', async () => {
    autoBtn.disabled = true;
    autoStatus.textContent = 'detecting…';
    try {
      const res = await fetch('/config/creds/auto', { method: 'POST' });
      const data = await res.json();
      if (res.ok) {
        autoStatus.textContent = `saved ✓ (org: ${data.org_id.slice(0, 8)}…)`;
        fetchQuota();
      } else {
        autoStatus.textContent = data.error || 'failed';
      }
    } catch { autoStatus.textContent = 'error'; }
    autoBtn.disabled = false;
    setTimeout(() => { autoStatus.textContent = ''; }, 5000);
  });

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

  const credsPopup = document.getElementById(cfg.credsPopupId);
  cursorQuotaDisplay.addEventListener('click', () => credsPopup.classList.toggle('open'));
  document.addEventListener('click', (e) => {
    if (!cursorQuotaDisplay.contains(e.target) && !credsPopup.contains(e.target))
      credsPopup.classList.remove('open');
  });
  fetchCursorQuota();
}

// ── usage stats panel ─────────────────────────────────────────────────────────

let statsPeriod = 'daily';
let statsGroup  = 'time';
let statsFilters = { days: 30, agent: '', topic: '', adhoc: 'all' };
let statsChartY1 = 'turns';
let statsChartY2 = '';
let statsChartInstance = null;
let _lastStatsRows = null;
let _statsFiltersLoaded = false;
let _statsPage = 0;
const _STATS_PAGE_SIZE = 10;
const _statsMeasures = new Set(['sessions', 'turns', 'tokens_in', 'tokens_out']);

function _rerenderStats() {
  if (!_lastStatsRows) return;
  if (statsGroup === 'topic') renderTopicStats(_lastStatsRows);
  else if (statsGroup === 'model') renderAgentStats(_lastStatsRows);
  else { renderTimeStats(_lastStatsRows); _renderChart(_lastStatsRows); }
}

function _statsPageSlice(rows) {
  const start = _statsPage * _STATS_PAGE_SIZE;
  return rows.slice(start, start + _STATS_PAGE_SIZE);
}

function _statsAppendPager(totalRows) {
  const totalPages = Math.ceil(totalRows / _STATS_PAGE_SIZE);
  if (totalPages <= 1) return;
  const div = document.createElement('div');
  div.className = 'stats-pager';
  div.innerHTML = `<button class="stats-pager-btn" id="sp-prev" type="button" ${_statsPage === 0 ? 'disabled' : ''}>‹ Prev</button>
    <span class="stats-pager-info">${_statsPage + 1} / ${totalPages}</span>
    <button class="stats-pager-btn" id="sp-next" type="button" ${_statsPage >= totalPages - 1 ? 'disabled' : ''}>Next ›</button>`;
  statsContent.appendChild(div);
  div.querySelector('#sp-prev')?.addEventListener('click', () => { _statsPage--; _rerenderStats(); });
  div.querySelector('#sp-next')?.addEventListener('click', () => { _statsPage++; _rerenderStats(); });
}

const CHART_METRICS = {
  turns:      { label: 'Turns',      fn: r => (r.total_turns || 0),                                                     color: 'rgba(100,160,255,1)',  fill: 'rgba(100,160,255,0.08)' },
  cost:       { label: 'Cost ($)',   fn: r => (r.cost_usd || 0),                                                        color: 'rgba(255,160,80,1)',   fill: 'rgba(255,160,80,0.08)'  },
  tokens_in:  { label: 'Tokens In', fn: r => { const raw = r.input_tokens||0, cr = r.cache_read_tokens||0; return (cr>0&&raw<cr)?raw+cr:raw; }, color: 'rgba(80,200,120,1)',   fill: 'rgba(80,200,120,0.08)'  },
  tokens_out: { label: 'Tokens Out',fn: r => (r.output_tokens || 0),                                                   color: 'rgba(200,100,200,1)',  fill: 'rgba(200,100,200,0.08)' },
  sessions:   { label: 'Sessions',  fn: r => (r.sessions || 0),                                                        color: 'rgba(200,200,60,1)',   fill: 'rgba(200,200,60,0.08)'  },
};

function _statsMeasureSelected(measure) {
  return _statsMeasures.has(measure);
}

function _formatCost(value) {
  return `$${(value || 0).toFixed(4)}`;
}

function _formatQuotaDelta(value) {
  return value != null ? `${value >= 0 ? '+' : ''}${value.toFixed(1)} pp` : '—';
}

function _statsInputTokens(row) {
  const raw = row.input_tokens || 0, cr = row.cache_read_tokens || 0;
  return (cr > 0 && raw < cr) ? raw + cr : raw;
}

const STATS_TABLE_MEASURES = [
  { key: 'sessions', label: 'Sessions', row: r => r.sessions || 0, total: t => t.sessions || 0 },
  { key: 'turns', label: 'Turns', row: r => r.total_turns || '—', total: t => t.turns || '—' },
  { key: 'tokens_in', label: 'Tokens In', row: r => fmtNum(_statsInputTokens(r)), total: t => fmtNum(t.tokens_in || 0) },
  { key: 'tokens_out', label: 'Tokens Out', row: r => fmtNum(r.output_tokens || 0), total: t => fmtNum(t.tokens_out || 0) },
  { key: 'cost', label: 'Cost', row: r => _formatCost(r.cost_usd), total: t => _formatCost(t.cost || 0) },
  { key: 'quota', label: 'Quota meter Δ', title: 'Observed account meter change; not exact attributed usage', row: r => _formatQuotaDelta(r.quota_delta), total: t => _formatQuotaDelta(t.quota) },
];

function _statsMeasureHeaders() {
  return STATS_TABLE_MEASURES
    .filter(m => _statsMeasureSelected(m.key))
    .map(m => `<th${m.title ? ` title="${m.title}"` : ''}>${m.label}</th>`)
    .join('');
}

function _statsMeasureCells(row) {
  return STATS_TABLE_MEASURES
    .filter(m => _statsMeasureSelected(m.key))
    .map(m => `<td>${m.row(row)}</td>`)
    .join('');
}

function _statsMeasureTotals(totals) {
  return STATS_TABLE_MEASURES
    .filter(m => _statsMeasureSelected(m.key))
    .map(m => `<td>${m.total(totals)}</td>`)
    .join('');
}

function _statsTotals(rows) {
  const totals = { sessions: 0, turns: 0, tokens_in: 0, tokens_out: 0, cost: 0, quota: null };
  for (const r of rows) {
    totals.sessions += r.sessions || 0;
    totals.turns += r.total_turns || 0;
    totals.tokens_in += _statsInputTokens(r);
    totals.tokens_out += r.output_tokens || 0;
    totals.cost += r.cost_usd || 0;
    if (r.quota_delta != null) totals.quota = (totals.quota || 0) + r.quota_delta;
  }
  return totals;
}

function _statsPeriodLabel(period) {
  if (!period) return '—';
  if (statsPeriod === 'hourly') {
    const match = String(period).match(/^\d{4}-(\d{2}-\d{2}\s+\d{2}:\d{2})/);
    if (match) return match[1];
  }
  return period;
}

function _updateStatsMeasureLabel() {
  const toggle = document.getElementById('sf-measures-toggle');
  if (!toggle) return;
  const labels = STATS_TABLE_MEASURES
    .filter(m => _statsMeasureSelected(m.key))
    .map(m => m.label.replace(' meter Δ', ''));
  toggle.textContent = labels.length ? `Measures (${labels.length})` : 'Measures';
  toggle.classList.toggle('active', labels.length > 0);
}

function _destroyChart() {
  if (statsChartInstance) { statsChartInstance.destroy(); statsChartInstance = null; }
}

function _renderChart(rows) {
  if (!rows || !rows.length || typeof Chart === 'undefined') { _destroyChart(); return; }
  const chronological = [...rows].reverse();
  const labels = chronological.map(r => r.period);
  const m1 = CHART_METRICS[statsChartY1] || CHART_METRICS.turns;
  const datasets = [{
    label: m1.label, data: chronological.map(m1.fn),
    borderColor: m1.color, backgroundColor: m1.fill,
    yAxisID: 'y1', tension: 0.3, fill: true,
    pointRadius: labels.length > 60 ? 1 : labels.length > 20 ? 2 : 4,
    pointHoverRadius: 5,
  }];
  const scales = {
    x: { ticks: { color: '#555', font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, grid: { color: '#1a1a24' } },
    y1: { type: 'linear', position: 'left', ticks: { color: '#555', font: { size: 10 }, callback: fmtAxisNum }, grid: { color: '#1a1a24' } },
  };
  if (statsChartY2) {
    const m2 = CHART_METRICS[statsChartY2];
    if (m2) {
      datasets.push({
        label: m2.label, data: chronological.map(m2.fn),
        borderColor: m2.color, backgroundColor: 'transparent',
        yAxisID: 'y2', tension: 0.3, fill: false,
        pointRadius: labels.length > 60 ? 1 : labels.length > 20 ? 2 : 4,
        pointHoverRadius: 5,
      });
      scales.y2 = { type: 'linear', position: 'right', ticks: { color: '#555', font: { size: 10 }, callback: fmtAxisNum }, grid: { display: false } };
    }
  }
  _destroyChart();
  statsChartInstance = new Chart(document.getElementById('stats-chart'), {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { labels: { color: '#888', font: { size: 11 }, boxWidth: 12, padding: 12 } },
        tooltip: { backgroundColor: '#1a1a24', borderColor: '#2e2e3e', borderWidth: 1, titleColor: '#aaa', bodyColor: '#888' },
      },
      scales,
    },
  });
}

// ── process status dot + popup ────────────────────────────────────────────────

const procStatusBtn   = document.getElementById('proc-status');
const procStatusPopup = document.getElementById('proc-status-popup');
let procPollInterval  = null;
let procPollHolds     = 0;
let procPollSeq       = 0;

function updateProcStatusDot(running, queued) {
  procStatusBtn.classList.toggle('has-procs', running.length > 0 || queued.length > 0);
}

function renderQuotaStatus() {
  // The status popup is a backend overview, so its rows must come from the
  // configured backend catalog. quotaSnapshots is populated lazily and only
  // contains gauges that have already been fetched (usually the active one).
  const backends = [...new Set([
    ...Object.keys(_backendMetadata),
    ...Object.keys(quotaSnapshots),
  ])];
  const rows = backends
    .map(backend => {
    const q = quotaSnapshots[backend] || {
      backend,
      status: quotaConfigFor(backend) ? 'unknown' : 'unsupported',
    };
    const accent = agentThemeColor(backend);
    let value = 'n/a';
    let detail = 'no quota integration';
    if (q.status === 'loaded') {
      value = q.displayText || (q.pct == null ? '—' : `${q.pct}%`);
      const reset = quotaTimeText(q.resetAt);
      detail = reset ? `resets in ${reset}` : (q.title || 'no reset');
    } else if (q.status === 'error') {
      value = 'error';
      detail = q.text || 'unavailable';
    } else if (q.status === 'unknown') {
      value = '...';
      detail = 'loading';
    }
    return `<div class="quota-status-row">
      <span class="quota-status-name"><span class="quota-status-dot" style="background:${accent}"></span>${escapeHtml(backendDisplayName(backend))}</span>
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
  if (open) {
    renderProcPopup(cachedProcRows, cachedQueueRows);
    startProcPoll();
    for (const backend of Object.keys(_backendMetadata)) {
      if (quotaConfigFor(backend)) fetchQuotaForBackend(backend);
    }
  }
}

let cachedProcRows  = [];
let cachedQueueRows = [];

async function pollProcs() {
  const seq = ++procPollSeq;
  try {
    const [procRes, queueRes] = await Promise.all([fetch('/processes'), fetch('/queue')]);
    const [running, queued] = await Promise.all([procRes.json(), queueRes.json()]);
    if (seq !== procPollSeq) return;
    cachedProcRows  = running;
    cachedQueueRows = queued;
    updateProcStatusDot(cachedProcRows, cachedQueueRows);
    if (procStatusPopup.classList.contains('open')) renderProcPopup(cachedProcRows, cachedQueueRows);
    if (!cachedProcRows.length && !cachedQueueRows.length && procPollHolds === 0) stopProcPoll();
  } catch { /* ignore */ }
}

function startProcPoll({ hold = false } = {}) {
  if (hold) procPollHolds++;
  if (procPollInterval) return pollProcs();
  procPollInterval = setInterval(pollProcs, 3000);
  pollProcs();
}

function releaseProcPoll() {
  procPollHolds = Math.max(0, procPollHolds - 1);
  pollProcs();
}

function stopProcPoll() {
  if (!procPollInterval) return;
  clearInterval(procPollInterval);
  procPollInterval = null;
}

procStatusBtn.addEventListener('click', e => {
  e.stopPropagation();
  toggleProcPopup();
});

async function loadStats() {
  statsContent.innerHTML = '<div class="empty">Loading…</div>';

  if (!_statsFiltersLoaded) {
    _statsFiltersLoaded = true;
    fetch('/stats/filters').then(r => r.json()).then(data => {
      const agentSel = document.getElementById('sf-agent');
      const topicSel = document.getElementById('sf-topic');
      const curAgent = agentSel.value, curTopic = topicSel.value;
      agentSel.innerHTML = '<option value="">All</option>' +
        data.agents.map(a => `<option value="${escapeHtml(a)}"${a === curAgent ? ' selected' : ''}>${escapeHtml(a)}</option>`).join('');
      topicSel.innerHTML = '<option value="">All</option>' +
        data.topics.map(t => `<option value="${escapeHtml(t)}"${t === curTopic ? ' selected' : ''}>#${escapeHtml(t)}</option>`).join('');
    }).catch(() => {});
  }

  const params = new URLSearchParams();
  if (statsGroup !== 'time') {
    params.set('group', statsGroup === 'model' ? 'agent' : statsGroup);
  } else {
    params.set('period', statsPeriod);
  }
  params.set('days', statsFilters.days);
  params.set('tz_offset_minutes', new Date().getTimezoneOffset());
  if (statsFilters.agent) params.set('agent', statsFilters.agent);
  if (statsFilters.topic) params.set('topic', statsFilters.topic);
  if (statsFilters.adhoc !== 'all') params.set('adhoc', statsFilters.adhoc);

  let rows;
  try {
    rows = await fetch(`/stats?${params}`).then(r => r.json());
  } catch {
    statsContent.innerHTML = '<div class="empty">Failed to load.</div>';
    return;
  }

  const chartWrap = document.getElementById('stats-chart-wrap');
  if (!rows.length) {
    statsContent.innerHTML = '<div class="empty">No data yet.</div>';
    _destroyChart();
    if (chartWrap) chartWrap.hidden = true;
    return;
  }

  const isTime = statsGroup !== 'topic' && statsGroup !== 'model';
  if (chartWrap) chartWrap.hidden = !isTime;

  _lastStatsRows = rows;
  _statsPage = 0;
  if (statsGroup === 'topic') {
    renderTopicStats(rows);
  } else if (statsGroup === 'model') {
    renderAgentStats(rows);
  } else {
    renderTimeStats(rows);
    _renderChart(rows);
  }
}

function renderTimeStats(rows) {
  const totals = _statsTotals(rows);
  const bodyRows = _statsPageSlice(rows).map(r => {
    return `<tr>
      <td>${_statsPeriodLabel(r.period)}</td>
      ${_statsMeasureCells(r)}
    </tr>`;
  }).join('');

  statsContent.innerHTML = `<table>
    <thead><tr>
      <th>${statsPeriod === 'hourly' ? 'Hour' : 'Date'}</th>
      ${_statsMeasureHeaders()}
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td>Total</td>${_statsMeasureTotals(totals)}
    </tr></tfoot>
  </table>`;
  _statsAppendPager(rows.length);
}

function renderTopicStats(rows) {
  const totals = _statsTotals(rows);
  const bodyRows = _statsPageSlice(rows).map(r => {
    return `<tr>
      <td>#${escapeHtml(r.topic)}</td>
      ${_statsMeasureCells(r)}
    </tr>`;
  }).join('');

  statsContent.innerHTML = `<table>
    <thead><tr>
      <th>Topic</th>
      ${_statsMeasureHeaders()}
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td>Total</td>${_statsMeasureTotals(totals)}
    </tr></tfoot>
  </table>`;
  _statsAppendPager(rows.length);
}

function renderAgentStats(rows) {
  const totals = _statsTotals(rows);
  const bodyRows = _statsPageSlice(rows).map(r => {
    return `<tr>
      <td>${escapeHtml(r.agent)}</td>
      ${_statsMeasureCells(r)}
    </tr>`;
  }).join('');

  statsContent.innerHTML = `<table>
    <thead><tr>
      <th>Agent</th>
      ${_statsMeasureHeaders()}
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td>Total</td>${_statsMeasureTotals(totals)}
    </tr></tfoot>
  </table>`;
  _statsAppendPager(rows.length);
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

  document.getElementById('sf-days').addEventListener('change', e => {
    statsFilters.days = parseInt(e.target.value);
    loadStats();
  });

  document.getElementById('sf-adhoc').addEventListener('change', e => {
    statsFilters.adhoc = e.target.value;
    loadStats();
  });

  document.getElementById('sf-agent').addEventListener('change', e => {
    statsFilters.agent = e.target.value;
    loadStats();
  });

  document.getElementById('sf-topic').addEventListener('change', e => {
    statsFilters.topic = e.target.value;
    loadStats();
  });

  const measures = document.getElementById('sf-measures');
  const measuresToggle = document.getElementById('sf-measures-toggle');
  const measuresMenu = document.getElementById('sf-measures-menu');
  measuresToggle.addEventListener('click', e => {
    e.stopPropagation();
    const open = measuresMenu.hidden;
    measuresMenu.hidden = !open;
    measuresToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  measuresMenu.querySelectorAll('input[type="checkbox"]').forEach(input => {
    input.addEventListener('change', () => {
      if (input.checked) _statsMeasures.add(input.value);
      else _statsMeasures.delete(input.value);
      _updateStatsMeasureLabel();
      _rerenderStats();
    });
  });
  document.addEventListener('click', e => {
    if (!measures.contains(e.target)) {
      measuresMenu.hidden = true;
      measuresToggle.setAttribute('aria-expanded', 'false');
    }
  });
  _updateStatsMeasureLabel();

  document.getElementById('sc-y1').addEventListener('change', e => {
    statsChartY1 = e.target.value;
    if (_lastStatsRows) _renderChart(_lastStatsRows);
  });

  const y2Sel = document.getElementById('sc-y2');
  y2Sel.addEventListener('change', e => {
    statsChartY2 = e.target.value;
    if (!statsChartY2) {
      y2Sel.hidden = true;
      document.getElementById('sc-compare-btn').textContent = '+ Y2';
    }
    if (_lastStatsRows) _renderChart(_lastStatsRows);
  });

  document.getElementById('sc-compare-btn').addEventListener('click', () => {
    const hidden = y2Sel.hidden;
    y2Sel.hidden = !hidden;
    document.getElementById('sc-compare-btn').textContent = hidden ? '− Y2' : '+ Y2';
    if (!hidden) { statsChartY2 = ''; if (_lastStatsRows) _renderChart(_lastStatsRows); }
  });
}

// ── topic manager ─────────────────────────────────────────────────────────────

let _topicsManageCache = null;
let _topicsManageCachePromise = null;
const _topicsExpanded = new Set();
let _topicDeleteTarget = null;
let _topicsSort = { col: 'last_at', dir: 'desc' };

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
    const isDefaultTopic = topic.name === 'default';
    const sessionPrompt = lane.last_prompt ? escapeHtml(truncate(lane.last_prompt, 120)) : '<span class="col-default">No session prompt</span>';
    const laneTime = lane.last_at ? `<span class="topic-badge time">${escapeHtml(fmtTime(lane.last_at))}</span>` : '';
    // Default topic: skip session lane if no session history — adhoc is the normal mode there
    if (isDefaultTopic && !lane.last_prompt) {
      if (lane.last_adhoc_prompt) {
        html += `
        <div class="topic-agent-row adhoc" data-topic="${escapeHtml(topic.name)}" data-agent="${escapeHtml(lane.agent)}" data-adhoc="1">
          <div class="topic-agent-main">
            <span class="topic-agent-label"><span class="topic-name">#${escapeHtml(topic.name)}</span>${_topicAgentDisplay(lane.agent, backend)}!${lane.adhoc_turns > 0 ? ` <span class="topic-turn-count">${lane.adhoc_turns}</span>` : ''}</span>
          </div>
          <div class="topic-prompt">${escapeHtml(truncate(lane.last_adhoc_prompt, 120))}</div>
          <div class="topic-meta">
            <span class="topic-badge">adhoc</span>
            ${laneTime}
            <button class="topic-btn" data-topic-open="${escapeHtml(topic.name)}" data-agent-open="${escapeHtml(lane.agent)}" data-adhoc-open="1" type="button">Open</button>
          </div>
        </div>`;
      }
      continue;
    }
    // Session lane: always show, but Delete only if there's actual session history
    const sessionTurns = lane.session_turns || 0;
    const liveTurns = lane.live_turns || 0;
    const liveBadge = liveTurns > 0 ? ` <span class="topic-turn-count live" title="turns in current session">${liveTurns} now</span>` : '';
    const turnCount = sessionTurns > 0 ? ` <span class="topic-turn-count">${sessionTurns}</span>${liveBadge}` : liveBadge;
    html += `
      <div class="topic-agent-row" data-topic="${escapeHtml(topic.name)}" data-agent="${escapeHtml(lane.agent)}" data-adhoc="0">
        <div class="topic-agent-main">
          <span class="topic-agent-label"><span class="topic-name">#${escapeHtml(topic.name)}</span>${_topicAgentDisplay(lane.agent, backend)}${turnCount}</span>
        </div>
        <div class="topic-prompt">${sessionPrompt}</div>
        <div class="topic-meta">
          ${laneTime}
          <button class="topic-btn" data-topic-open="${escapeHtml(topic.name)}" data-agent-open="${escapeHtml(lane.agent)}" data-adhoc-open="0" type="button">Open</button>
          ${lane.last_prompt ? `<button class="topic-btn danger" data-agent-del-topic="${escapeHtml(topic.name)}" data-agent-del-agent="${escapeHtml(lane.agent)}" data-agent-del-adhoc="0" type="button">Delete</button>` : ''}
        </div>
      </div>`;
    if (lane.last_adhoc_prompt) {
      // Adhoc lane on default topic: no Delete — use topic-level Clear instead
      html += `
        <div class="topic-agent-row adhoc" data-topic="${escapeHtml(topic.name)}" data-agent="${escapeHtml(lane.agent)}" data-adhoc="1">
          <div class="topic-agent-main">
            <span class="topic-agent-label"><span class="topic-name">#${escapeHtml(topic.name)}</span>${_topicAgentDisplay(lane.agent, backend)}!${lane.adhoc_turns > 0 ? ` <span class="topic-turn-count">${lane.adhoc_turns}</span>` : ''}</span>
          </div>
          <div class="topic-prompt">${escapeHtml(truncate(lane.last_adhoc_prompt, 120))}</div>
          <div class="topic-meta">
            <span class="topic-badge">adhoc</span>
            ${laneTime}
            <button class="topic-btn" data-topic-open="${escapeHtml(topic.name)}" data-agent-open="${escapeHtml(lane.agent)}" data-adhoc-open="1" type="button">Open</button>
            ${!isDefaultTopic ? `<button class="topic-btn danger" data-agent-del-topic="${escapeHtml(topic.name)}" data-agent-del-agent="${escapeHtml(lane.agent)}" data-agent-del-adhoc="1" type="button">Delete</button>` : ''}
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
  const hideLabel = topic.name !== 'default' ? (topic.hidden ? 'Show' : 'Hide') : '';
  return `
    <div class="topic-row${topic.hidden ? ' hidden' : ''}${expanded ? ' expanded' : ''}" data-topic="${escapeHtml(topic.name)}">
      <div class="topic-main">
        <span class="topic-caret">${expanded ? '▾' : '▸'}</span>
        <span class="topic-identity"><span class="topic-name">#${escapeHtml(topic.name)}</span>${agentLabel}${topic.total_turns > 0 ? `<span class="topic-turn-count">${topic.total_turns}</span>` : ''}</span>
      </div>
      <div class="topic-prompt">${prompt}</div>
      <div class="topic-meta">
        ${_topicStatusBadges(topic)}
        <button class="topic-btn" data-topic-open="${escapeHtml(topic.name)}" type="button">Open</button>
        <button class="topic-btn" data-topic-memory="${escapeHtml(topic.name)}" type="button">${memoryLabel}</button>
        ${topic.name !== 'default' ? `<button class="topic-btn" data-topic-hide="${escapeHtml(topic.name)}" data-hidden="${topic.hidden ? '1' : '0'}" type="button">${hideLabel}</button>` : ''}
        <button class="topic-btn danger" data-topic-delete="${escapeHtml(topic.name)}" type="button">${topic.name === 'default' ? 'Clear' : 'Delete'}</button>
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

  const sorted = [...filtered].sort((a, b) => {
    let cmp;
    if (_topicsSort.col === 'name') {
      cmp = a.name.localeCompare(b.name);
    } else if (_topicsSort.col === 'turns') {
      cmp = (a.total_turns || 0) - (b.total_turns || 0);
    } else {
      const ta = a.last_at || '';
      const tb = b.last_at || '';
      cmp = ta < tb ? -1 : ta > tb ? 1 : 0;
    }
    return _topicsSort.dir === 'asc' ? cmp : -cmp;
  });

  listEl.innerHTML = sorted.map(_renderTopicRows).join('');
  _updateTopicsSortBar();
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
        const res = await fetch(`/topics/${encodeURIComponent(topic)}/hidden`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ hidden: nextHidden }),
        });
        if (!res.ok) return;
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
  listEl.querySelectorAll('[data-agent-del-topic]').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      openAgentDeleteModal(btn.dataset.agentDelTopic, btn.dataset.agentDelAgent, btn.dataset.agentDelAdhoc === '1');
    });
  });
}

function _updateTopicsSortBar() {
  document.querySelectorAll('[data-topics-sort]').forEach(btn => {
    const col = btn.dataset.topicsSort;
    const arrow = btn.querySelector('.sort-arrow');
    const active = _topicsSort.col === col;
    btn.classList.toggle('active', active);
    if (arrow) arrow.textContent = active ? (_topicsSort.dir === 'asc' ? '↑' : '↓') : '↕';
  });
}

function initTopicsView() {
  const searchEl = document.getElementById('topics-search');
  if (searchEl) searchEl.addEventListener('input', () => loadTopicsView());

  document.querySelectorAll('[data-topics-sort]').forEach(btn => {
    btn.addEventListener('click', () => {
      const col = btn.dataset.topicsSort;
      if (_topicsSort.col === col) {
        _topicsSort.dir = _topicsSort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        _topicsSort = { col, dir: col === 'name' ? 'asc' : 'desc' };  // name→asc, last_at/turns→desc
      }
      loadTopicsView();
    });
  });
}

let _agentDeleteTarget = null; // { topic, agent, adhoc }

function openTopicDeleteModal(topic) {
  _topicDeleteTarget = topic;
  _agentDeleteTarget = null;
  const isDefault = topic === 'default';
  document.getElementById('topic-delete-modal-heading').textContent = isDefault ? 'Clear default' : 'Delete topic';
  document.getElementById('topic-delete-modal-title').textContent = `#${topic}`;
  document.getElementById('topic-delete-modal-copy').textContent = isDefault
    ? 'Clears all messages in the default topic. The topic itself is not removed. Cannot be undone.'
    : 'Removes all messages, sessions, and stats for this topic. Cannot be undone.';
  document.getElementById('topic-delete-confirm').textContent = isDefault ? 'Clear' : 'Delete';
  document.getElementById('topic-delete-confirm').disabled = false;
  document.getElementById('topic-delete-modal').classList.add('open');
}

function openAgentDeleteModal(topic, agent, adhoc) {
  _topicDeleteTarget = null;
  _agentDeleteTarget = { topic, agent, adhoc };
  const scope = adhoc ? `#${topic}@${agent}!` : `#${topic}@${agent}`;
  document.getElementById('topic-delete-confirm').textContent = 'Delete';
  document.getElementById('topic-delete-modal-heading').textContent = 'Delete agent lane';
  document.getElementById('topic-delete-modal-title').textContent = scope;
  document.getElementById('topic-delete-modal-copy').textContent = adhoc
    ? 'Removes all adhoc messages for this agent in this topic. Cannot be undone.'
    : 'Removes all session messages and session state for this agent in this topic. Cannot be undone.';
  document.getElementById('topic-delete-confirm').disabled = false;
  document.getElementById('topic-delete-modal').classList.add('open');
}

function closeTopicDeleteModal() {
  _topicDeleteTarget = null;
  _agentDeleteTarget = null;
  document.getElementById('topic-delete-modal').classList.remove('open');
}

async function confirmTopicDelete() {
  const btn = document.getElementById('topic-delete-confirm');
  btn.disabled = true;
  try {
    if (_agentDeleteTarget) {
      const { topic, agent, adhoc } = _agentDeleteTarget;
      const url = `/topics/${encodeURIComponent(topic)}/agent?agent=${encodeURIComponent(agent)}&adhoc=${adhoc}`;
      const res = await fetch(url, { method: 'DELETE' });
      if (!res.ok) return;
      closeTopicDeleteModal();
      invalidateTopicsCache();
      invalidateTopicsManageCache();
      loadTopicsView();
    } else if (_topicDeleteTarget) {
      const topic = _topicDeleteTarget;
      const res = await fetch(`/topics/${encodeURIComponent(topic)}`, { method: 'DELETE' });
      if (!res.ok) return;
      _topicsExpanded.delete(topic);
      closeTopicDeleteModal();
      invalidateTopicsCache();
      invalidateTopicsManageCache();
      loadTopicsView();
    }
  } finally {
    btn.disabled = false;
  }
}

// ── backends catalog ──────────────────────────────────────────────────────────

const DRIVER_CATALOG = Object.freeze({
  claude: {
    label: 'Claude Code',
    installCmd: 'curl -fsSL https://claude.ai/install.sh | bash',
    authHint: 'run claude to authenticate',
    gaugeHint: 'click gauge in header → paste org ID + session key',
  },
  codex: {
    label: 'Codex',
    installCmd: 'curl -fsSL https://chatgpt.com/codex/install.sh | sh',
    authHint: 'run codex to authenticate',
    gaugeHint: 'click gauge in header → use bookmarklet or paste token',
  },
  cursor: {
    label: 'Cursor Agent',
    installCmd: 'curl -fsS https://cursor.com/install | bash',
    authHint: 'run cursor-agent to authenticate',
    gaugeHint: 'automatic via cursor-agent',
  },
  opencode: {
    label: 'OpenCode',
    installCmd: 'curl -fsSL https://opencode.ai/install | bash',
    authHint: 'free tier requires no auth — run opencode to configure providers',
    gaugeHint: 'free tier available (opencode/deepseek-v4-flash-free)',
  },
});

const GAUGE_CATALOG = Object.freeze({
  claude: 'click gauge in header → paste org ID + session key',
  codex: 'click gauge in header → use bookmarklet or paste token',
  cursor: 'automatic via cursor-agent',
  deepseek: 'uses this backend API key',
  none: 'no gauge configured',
});

function renderBackendsCatalog(backends) {
  const el = document.getElementById('backends-catalog');
  if (!el) return;

  _backendMetadata = backends || {};
  const backendSelect = document.getElementById('af-backend');
  if (backendSelect) {
    const previous = backendSelect.value;
    backendSelect.innerHTML = Object.keys(_backendMetadata)
      .map(id => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join('');
    if (_backendMetadata[previous]) backendSelect.value = previous;
  }
  refreshAgentSlugColors();

  el.innerHTML = Object.entries(_backendMetadata).map(([id, info]) => {
    const driverInfo = DRIVER_CATALOG[info.driver] || {};
    const available   = info.available;
    const gaugeAuthed = info.gauge_authed;
    const color       = agentThemeColor(id);
    const label       = info.label;
    const authHint    = driverInfo.authHint || `uses ${info.driver} driver`;
    const installCmd  = driverInfo.installCmd || '';
    const gauge       = info.gauge || { type: 'none' };
    const gaugeHint   = gauge.type === 'static'
      ? (gauge.text || 'static')
      : (GAUGE_CATALOG[gauge.type] || '—');

    let codingHtml;
    if (available) {
      codingHtml = `<span class="bcat-status-ok">✓ detected</span>
        <span class="bcat-hint">${escapeHtml(authHint)}</span>`;
    } else {
      const missing = info.missing_secrets?.length
        ? `missing: ${info.missing_secrets.join(', ')}`
        : 'driver not found';
      codingHtml = `<span class="bcat-status-miss">✗ ${escapeHtml(missing)}</span>` +
        (installCmd ? `<div class="bcat-install">
          <code class="bcat-cmd">${escapeHtml(installCmd)}</code>
          <button class="bcat-copy" data-cmd="${escapeHtml(installCmd)}">copy</button>
        </div>` : '');
    }

    let gaugeHtml;
    if (!available) {
      gaugeHtml = `<span class="bcat-gauge-na">—</span>`;
    } else if (gauge.type === 'none') {
      gaugeHtml = `<span class="bcat-gauge-na">—</span>`;
    } else if (gauge.type === 'static') {
      gaugeHtml = `<span class="bcat-hint">${escapeHtml(gauge.text || '—')}</span>`;
    } else if (gaugeAuthed) {
      gaugeHtml = `<span class="bcat-gauge-ok">gauge ✓</span>`;
    } else {
      gaugeHtml = `<span class="bcat-hint">${escapeHtml(gaugeHint)}</span>`;
    }

    return `<div class="bcat-row">
      <div class="bcat-name"><span class="bcat-dot" style="background:${color}"></span>${escapeHtml(label)}</div>
      <div class="bcat-coding">${codingHtml}</div>
      <div class="bcat-gauge">${gaugeHtml}</div>
    </div>`;
  }).join('');

  el.querySelectorAll('.bcat-copy').forEach(btn => {
    btn.addEventListener('click', () => {
      navigator.clipboard.writeText(btn.dataset.cmd).then(() => {
        btn.textContent = 'copied';
        setTimeout(() => { btn.textContent = 'copy'; }, 1500);
      });
    });
  });
}

// ── agent manager ─────────────────────────────────────────────────────────────

let _configRevision = null;

async function loadConfigYaml() {
  const editor = document.getElementById('config-editor');
  const status = document.getElementById('config-editor-status');
  if (!editor) return;
  status.textContent = 'loading…';
  try {
    const res = await fetch('/config/yaml');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed to load configuration');
    editor.value = data.content;
    _configRevision = data.revision;
    document.getElementById('config-editor-path').textContent = data.path;
    status.textContent = '';
  } catch (err) {
    status.textContent = err.message || 'failed to load';
  }
}

async function saveConfigYaml() {
  const editor = document.getElementById('config-editor');
  const status = document.getElementById('config-editor-status');
  const save = document.getElementById('config-editor-save');
  save.disabled = true;
  status.textContent = 'validating…';
  try {
    const res = await fetch('/config/yaml', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: editor.value, revision: _configRevision }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed to save configuration');
    _configRevision = data.revision;
    status.textContent = data.restart_required ? 'saved ✓ · restart required' : 'saved ✓';
  } catch (err) {
    status.textContent = err.message || 'save failed';
  } finally {
    save.disabled = false;
  }
}

async function loadAgents() {
  const listEl = document.getElementById('agents-list');
  listEl.innerHTML = '<div class="empty">Loading…</div>';
  let agents, health;
  try {
    [agents, health] = await Promise.all([
      fetch('/config/agents').then(r => r.json()),
      fetch('/health').then(r => r.json()).catch(() => null),
    ]);
  } catch {
    listEl.innerHTML = '<div class="empty">Failed to load.</div>';
    return;
  }
  if (health?.squid_home) {
    _squidHome = health.squid_home;
    const cwdInput = document.getElementById('af-cwd');
    if (cwdInput) cwdInput.placeholder = `${_squidHome}/…`;
  }
  renderBackendsCatalog(health?.backends);
  loadConfigYaml();
  if (!agents.length) {
    listEl.innerHTML = '<div class="empty">No agents yet. Add one below.</div>';
    return;
  }
  const rows = agents.map(a => `
    <tr>
      <td><span class="agent-name">${a.name}</span></td>
      <td>${a.backend}</td>
      <td class="col-model">${a.model || '<span class="col-default">—</span>'}</td>
      <td>${a.cwd || `<span class="col-default">${_squidHome}</span>`}</td>
      <td>
        <button class="del-btn" data-name="${a.name}" title="Delete agent (does not affect existing messages)">✕</button>
      </td>
    </tr>`).join('');
  listEl.innerHTML = `<table>
    <thead><tr><th>Name</th><th>Backend</th><th class="col-model">Model</th><th>CWD</th><th></th></tr></thead>
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
    afModel.placeholder = backendModelHint(afBackend.value);
  });
  document.getElementById('config-editor-reload').addEventListener('click', loadConfigYaml);
  document.getElementById('config-editor-save').addEventListener('click', saveConfigYaml);
  document.getElementById('config-editor').addEventListener('keydown', e => {
    if (e.key !== 'Tab') return;
    e.preventDefault();
    const editor = e.currentTarget;
    const start = editor.selectionStart;
    editor.setRangeText('  ', start, editor.selectionEnd, 'end');
  });

  document.getElementById('agent-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      name:    document.getElementById('af-name').value.trim(),
      backend: document.getElementById('af-backend').value,
      model:   document.getElementById('af-model').value.trim() || null,
      cwd:     document.getElementById('af-cwd').value.trim()   || null,
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
        document.getElementById('af-name').value  = '';
        document.getElementById('af-model').value = '';
        document.getElementById('af-cwd').value   = '';
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
  const backendOptions = Object.keys(_backendMetadata).length
    ? Object.keys(_backendMetadata)
    : ['claude', 'codex', 'cursor', 'opencode'];
  prompt.innerHTML = `
    <div class="acp-title">Agent <strong>${agentName}</strong> not found — create it?</div>
    <div class="acp-row">
      <select id="acp-backend">
        ${backendOptions.map(id => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join('')}
      </select>
      <input id="acp-model" placeholder="${backendModelHint(backendOptions[0])}" />
      <input id="acp-cwd" placeholder="cwd (default: ${_squidHome})" />
    </div>
    <div class="acp-actions">
      <button id="acp-save">Create &amp; send</button>
      <button id="acp-cancel">Cancel</button>
    </div>`;

  messages.appendChild(prompt);
  messages.scrollTop = messages.scrollHeight;

  const modelInput = prompt.querySelector('#acp-model');
  prompt.querySelector('#acp-backend').addEventListener('change', e => {
    modelInput.placeholder = backendModelHint(e.target.value);
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
  acEl.querySelectorAll('.ac-item').forEach(el => {
    el.classList.toggle('selected', Number(el.dataset.i) === acSel);
  });
  if (acSel >= 0) acEl.querySelector(`.ac-item[data-i="${acSel}"]`)?.scrollIntoView({ block: 'nearest' });
}

function _acRender(items, title = 'Suggestions') {
  if (!items.length) { hideAutocomplete(); return; }
  acItems = items; acSel = 0;
  const rows = items.map((item, i) =>
    `<div class="ac-item" data-i="${i}"${item.execute != null ? ' data-cmd' : ''}>` +
    `<div class="ac-row">` +
    (item.routeHtml ? `<button class="ac-route-btn" type="button" data-i="${i}" title="Switch to this route">${item.routeHtml}</button> ` : '') +
    `<span class="ac-label">${item.label}</span>` +
    (item.sub ? `<span class="ac-sub">${item.sub}</span>` : '') +
    (item.meta ? `<span class="ac-meta">${item.meta}</span>` : '') +
    (item.deleteTopic ? `<button class="ac-del-btn" data-topic="${item.deleteTopic}" type="button" title="Delete #${item.deleteTopic} sessions">✕</button>` : '') +
    `</div>` +
    `</div>`
  ).reverse().join('');
  acEl.innerHTML =
    `<div class="ac-list">${rows}</div>` +
    `<div class="ac-header">` +
    `<div class="ac-title">${escapeHtml(title)}</div>` +
    `<button class="ac-close" type="button" aria-label="Close suggestions">` +
    `<span class="ac-close-desktop">Esc</span><span class="ac-close-mobile">×</span>` +
    `</button>` +
    `</div>`;
  acEl.querySelectorAll('.ac-item').forEach(el =>
    el.addEventListener('mousedown', e => {
      if (e.target.classList.contains('ac-del-btn')) return;
      const routeBtn = e.target.closest('.ac-route-btn');
      if (routeBtn) {
        e.preventDefault();
        const idx = Number(routeBtn.dataset.i);
        const fullEntry = idx >= 0 && idx < acItems.length ? acItems[idx].fullEntry : null;
        if (fullEntry) {
          hideAutocomplete();
          applyPromptHistoryEntry(fullEntry);
          input.focus();
        }
        return;
      }
      e.preventDefault(); _acSelect(Number(el.dataset.i));
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
  acEl.querySelector('.ac-close').addEventListener('mousedown', e => {
    e.preventDefault();
    e.stopPropagation();
    hideAutocomplete();
    input.focus();
  });
  acEl.classList.add('open');
  acOpen = true;
  _acHighlight();
  requestAnimationFrame(() => {
    const list = acEl.querySelector('.ac-list');
    if (list) list.scrollTop = list.scrollHeight;
  });
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
  if (item.replaceSlug && editingExpandedSlug) {
    const promptSeparator = input.value.indexOf(' ');
    if (promptSeparator >= 0) {
      input.value = item.insert + input.value.slice(promptSeparator);
      input.setSelectionRange(item.insert.length, item.insert.length);
      resizeComposer();
      input.focus();
      input.dispatchEvent(new Event('input'));
      return;
    }
  }
  if (item.clearChip && stickyChip) clearTopicChip();
  input.value = item.trail === false ? item.insert : item.insert + ' ';
  resizeComposer();
  input.focus();
  input.dispatchEvent(new Event('input'));
  if (item.collapseSlug) _maybeCollapseExpandedSlug(true, true);
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

function _acLastPrompt(prompt) {
  if (!prompt) return '';
  return `<span class="ac-last-badge">last</span> ${escapeHtml(truncate(prompt, 55))}`;
}

async function updateAutocomplete() {
  const val = input.value;
  const promptSeparator = editingExpandedSlug ? val.indexOf(' ') : -1;
  const slugVal = promptSeparator >= 0 ? val.slice(0, promptSeparator) : val;
  const replacingSlug = editingExpandedSlug && promptSeparator >= 0;

  // Command popup: message portion starts with /
  const { message: msgPart } = parseInput(val);
  if (!editingExpandedSlug && msgPart.startsWith('/')) {
    const slashIdx = val.lastIndexOf('/');
    const before   = val.slice(0, slashIdx);           // prefix to preserve (#topic@alias )
    const partial  = msgPart.slice(1).toLowerCase();   // typed after /
    const matched  = SQUID_COMMANDS.filter(c =>
      c.name.toLowerCase().startsWith(partial) ||
      (c.alias && c.alias.toLowerCase().startsWith(partial))
    );
    if (matched.length) {
      _acRender(matched.map(c => ({
        label:   `<span class="ac-cmd">/${c.name}${c.alias ? ', /' + c.alias : ''}</span>`,
        sub:     c.desc,
        meta:    'squid',
        insert:  before + '/' + c.name,
        execute: !c.args,
      })), 'Commands');
    } else {
      hideAutocomplete();
    }
    return;
  }

  const mTopic = slugVal.match(/^#(\w*)[!]?$/);
  const mAlias = slugVal.match(/^#(\w+)@(\w*)[!]?$/);
  if (mTopic) {
    const prefix = mTopic[1].toLowerCase();
    const topics = await _acTopics();
    if (input.value !== val) return;
    _acRender(
      topics.filter(t => t.name.toLowerCase().startsWith(prefix)).slice(0, 8)
        .map(t => ({
          label:       _acTopicLabel(t.name, t.agent || '', t.last_backend || null),
          insert:      '#' + t.name,
          replaceSlug: replacingSlug,
          deleteTopic: t.name,
          meta:        t.active ? '● live' : t.queue_depth > 0 ? `queue ${t.queue_depth}` : '',
          sub:         _acLastPrompt(t.last_prompt),
        })),
      'Routes'
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

    const isDefault = topic.toLowerCase() === 'default';
    // Used agents — with last prompt
    for (const h of history) {
      if (!h.agent.toLowerCase().startsWith(prefix)) continue;
      // Default topic: suppress session variant — adhoc only
      if (!isDefault) {
        items.push({
          label:  _acAgentLabel(topic, h.agent, backendByAgent.get(h.agent) || null),
          insert: `#${topic}@${h.agent}`,
          replaceSlug: replacingSlug,
          sub:    _acLastPrompt(h.last_prompt),
        });
      }
      items.push({
        label:  _acAgentLabel(topic, h.agent + '!', backendByAgent.get(h.agent) || null),
        insert: `#${topic}@${h.agent}!`,
        replaceSlug: replacingSlug,
        sub:    _acLastPrompt(h.last_adhoc_prompt),
        meta:   'adhoc',
      });
    }

    // Other available agents — no prompt
    for (const a of agents) {
      if (usedNames.has(a.name)) continue;
      if (!a.name.toLowerCase().startsWith(prefix)) continue;
      // Default topic: only offer adhoc variant
      items.push({
        label:  _acAgentLabel(topic, isDefault ? a.name + '!' : a.name, a.backend),
        insert: `#${topic}@${a.name}${isDefault ? '!' : ''}`,
        replaceSlug: replacingSlug,
        meta:   a.backend,
      });
    }

    _acRender(items.slice(0, 10), 'Routes');
  } else if (editingExpandedSlug) {
    hideAutocomplete();
  } else if (promptHistory.length) {
    const currentRoute = currentPromptHistoryRoute().toLowerCase();
    _acRender(matchingPromptHistory(val).map(ph => {
      const { route, prompt } = splitPromptHistoryEntry(ph);
      const promptText = prompt || ph;
      const routeKey = normalizePromptHistoryRoute(route);
      const isDifferentRoute = !!(routeKey && routeKey.toLowerCase() !== currentRoute);
      let routeHtml = '';
      if (routeKey) {
        const rm = routeKey.match(/^#(\w+)(?:@(\w+))?(!)?$/);
        if (rm) {
          const topic = rm[1], agent = rm[2], adhoc = rm[3] || '';
          routeHtml = `<span class="ac-topic">#${escapeHtml(topic)}</span>`;
          if (agent) {
            const display = `@${escapeHtml(agent)}${escapeHtml(adhoc)}`;
            routeHtml += `<span class="ac-agent"${_agentStyleAttr(agent)}>${escapeHtml(display)}</span>`;
          }
        }
      }
      const label = `<span class="ac-history-prompt">${escapeHtml(truncate(promptText, 55))}</span>`;
      return {
        label,
        insert: promptText,
        trail: false,
        ...(isDifferentRoute && routeHtml ? { routeHtml, fullEntry: `${routeKey} ${promptText}` } : {}),
      };
    }), 'Recent Prompts');
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
  if (Math.random() < 0.5) {
    return '<img class="boot-logo-icon" src="/favicon.png" alt="" />';
  }
  return `<pre class="boot-art">${BOOT_LOGO_ART}</pre>` +
    `<div class="boot-art-mobile">${BOOT_LOGO_MOBILE}</div>`;
}

async function showBootBanner() {
  try {
    const res = await fetch('/health');
    if (!res.ok) return;
    const data = await res.json();
    _backendMetadata = data.backends || {};
    await updateActiveQuotaGauge();
    if (activeQuotaBackend) fetchQuotaForBackend(activeQuotaBackend);
    const bootTime = data.boot_time ? fmtTime(data.boot_time) : '';
    const el = document.createElement('div');
    el.className = 'boot-banner';
    el.innerHTML = bootLogoHtml() +
      `<div class="boot-meta">AgentSquid${bootTime ? `  ·  started ${bootTime}` : ''}</div>` +
      (!navigator.onLine ? `<div class="boot-offline">no internet — LLM calls will fail</div>` : '');
    messages.appendChild(el);

    const backends = data.backends || {};
    const anyAvailable = Object.values(backends).some(b => b.available);
    if (!anyAvailable) {
      const setup = document.createElement('div');
      setup.className = 'no-agent-setup';
      const agents = [
        { name: 'Claude Code',  cmd: 'curl -fsSL https://claude.ai/install.sh | bash' },
        { name: 'Codex',        cmd: 'curl -fsSL https://chatgpt.com/codex/install.sh | sh' },
        { name: 'Cursor Agent', cmd: 'curl -fsS https://cursor.com/install | bash' },
        { name: 'OpenCode',     cmd: 'curl -fsSL https://opencode.ai/install | bash' },
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
  const msgId  = spanEl.dataset.msgId || '';
  const cwd    = spanEl.dataset.cwd || '';
  const mem    = spanEl.dataset.mem === 'true';
  const topic  = spanEl.dataset.topic || '';
  const sessionTurnCount = parseInt(spanEl.dataset.sessionTurnCount || '0', 10) || 0;
  const pinIds = JSON.parse(spanEl.dataset.pinnedIds || '[]');

  let html = '';
  if (msgId) {
    html += `<div class="ctx-popup-row"><span class="ctx-popup-key">message</span><span class="ctx-popup-val">#${msgId}</span></div>`;
  }
  if (sid || cwd) {
    html += `<div class="ctx-popup-row"><span class="ctx-popup-key">session</span><span class="ctx-popup-val">${sid}</span></div>`;
    if (sessionTurnCount > 0) {
      html += `<div class="ctx-popup-row"><span class="ctx-popup-key">session context</span><span class="ctx-popup-val">${sessionTurnCount} turn${sessionTurnCount !== 1 ? 's' : ''}</span></div>`;
    }
    if (cwd) html += `<div class="ctx-popup-row"><span class="ctx-popup-key">cwd</span><span class="ctx-popup-val">${cwd}</span></div>`;
  } else if (sessionTurnCount > 0) {
    html += `<div class="ctx-popup-row"><span class="ctx-popup-key">session context</span><span class="ctx-popup-val">${sessionTurnCount} turn${sessionTurnCount !== 1 ? 's' : ''}</span></div>`;
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
  document.querySelectorAll('.msg.assistant.pinned-sel').forEach(b => b.classList.remove('pinned-sel'));
  const { adhoc, lookback } = _currentContextTarget();
  const lookbackItems = _allLookbackItems(adhoc, lookback);
  _lastLookbackSelectionKey = _lookbackSelectionKey(adhoc, lookback, lookbackItems);
  lookbackItems.forEach(item => _lookbackUnselected.add(item.id));
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

function _memoryRevision(meta) {
  return meta?.revision || `content:${meta?.content || ''}`;
}

function _clearMemorySelectionOverridesForTopic(topic) {
  for (const key of Object.keys(_memorySelectionOverrides)) {
    if (key.startsWith(`${topic}@`)) delete _memorySelectionOverrides[key];
  }
}

function _clearSessionLookupCacheForTopic(topic) {
  for (const key of Object.keys(_sessionLookupCache)) {
    if (key === `${topic}@_` || key.startsWith(`${topic}@`)) delete _sessionLookupCache[key];
  }
}

function _rememberSessionMemoryRevision(topic, agent, session) {
  if (!session?.memory_injected && !session?.memory_revision) return;
  _memoryInjectedInto[_memoryInjectedKey(topic, agent)] = session.memory_revision || 'legacy:unknown';
}

function _getMemoryMeta(topic) {
  if (_memoryCache[topic]) return _memoryCache[topic];
  _memoryCache[topic] = { topic, exists: false, content: '', path: `~/.squid/context/topics/${topic}/memory.md`, loading: true };
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
  const oldRevision = _memoryRevision(_memoryCache[topic]);
  const res = await fetch(`/topics/${encodeURIComponent(topic)}/memory/squid/code-roots`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Save failed (${res.status})`);
  if (_memoryRevision(data) !== oldRevision) {
    _clearMemorySelectionOverridesForTopic(topic);
    _clearSessionLookupCacheForTopic(topic);
  }
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
        _rememberSessionMemoryRevision(topic, agent, data);
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
  const revision = _memoryRevision(meta);
  const injectedRevision = _memoryInjectedInto[_memoryInjectedKey(topic, agent)];
  const injected = !adhoc && !!injectedRevision && injectedRevision === revision;
  const stale = !adhoc && !!injectedRevision && injectedRevision !== revision;
  const defaultSelected = exists && (adhoc || stale || (!injectedRevision && !session.loading && !session.session_id));
  const selected = exists && (_memorySelectionOverrides[key] ?? defaultSelected);
  return { topic, agent, adhoc, meta, session, exists, selected, key, injected, stale, revision };
}

async function _topicMemoryStateForSend(topic, agent, adhoc) {
  const meta = await fetch(`/topics/${encodeURIComponent(topic)}/memory`)
    .then(r => r.ok ? r.json() : null)
    .catch(() => null);
  if (meta) _memoryCache[topic] = { ...meta, loading: false };
  const exists = !!(meta?.exists && (meta.content || '').trim());
  if (!exists) return { selected: false };
  const revision = _memoryRevision(meta);

  let session = { session_id: null };
  if (agent && !adhoc) {
    session = await fetch(`/topics/${encodeURIComponent(topic)}/session?agent=${encodeURIComponent(agent)}`)
      .then(r => r.ok ? r.json() : { session_id: null })
      .catch(() => ({ session_id: null }));
    if (session.session_id) _sessionIds[`${topic}@${agent}`] = session.session_id;
    _rememberSessionMemoryRevision(topic, agent, session);
  }
  const key = _memoryOverrideKey(topic, agent, adhoc);
  const injectedRevision = _memoryInjectedInto[_memoryInjectedKey(topic, agent)];
  const stale = !adhoc && !!injectedRevision && injectedRevision !== revision;
  const defaultSelected = adhoc || stale || (!injectedRevision && !session.session_id);
  return { selected: _memorySelectionOverrides[key] ?? defaultSelected, revision };
}

function _injectablePinnedIds(topic, agent, adhoc, lookback, items = getPinnedItems()) {
  const taKey = `${topic}@${agent || '_'}`;
  const currentSid = _sessionIds[taKey] || null;
  const injected = getInjectedInto();
  return items
    .filter(item => {
      const sameSession = item.session_id && currentSid && item.session_id === currentSid;
      if (sameSession && !adhoc) return false;
      if (adhoc && lookback === 0) return true;
      if (currentSid && (injected[currentSid] || []).includes(item.id)) return false;
      return true;
    })
    .map(item => item.id);
}

function updatePinCount() {
  const { topic, agent, adhoc, lookback } = _currentContextTarget();
  const ids = [
    ..._activeLookbackItems(adhoc, lookback).map(item => item.id),
    ..._injectablePinnedIds(topic, agent, adhoc, lookback),
  ];
  const n = new Set(ids).size;
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
      const itemCls = item.isLookback ? 'pin-item pin-item-lookback' : 'pin-item';
      const control = item.isLookback
        ? `<button class="pin-item-toggle active" data-lookback-id="${item.id}" type="button">On</button>`
        : `<button class="pin-item-remove" data-id="${item.id}" type="button">✕</button>`;
      html += `<div class="${itemCls}">
        <span class="pin-item-tag">${escapeHtml(tag)}</span>
        <span class="pin-item-preview">${escapeHtml(preview)}</span>
        <span class="pin-item-status ${st.cls}">${st.text}</span>
        ${control}
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
  listEl.querySelectorAll('[data-lookback-id]').forEach(btn => {
    btn.addEventListener('mousedown', e => {
      e.preventDefault();
      const id = parseInt(btn.dataset.lookbackId);
      _lookbackUnselected.add(id);
      updateInContextMarkers();
      updatePinCount();
      renderPinPanel();
    });
  });
  listEl.querySelectorAll('.pin-item-remove').forEach(btn => {
    btn.addEventListener('mousedown', e => {
      e.preventDefault();
      const id = parseInt(btn.dataset.id);
      setPinnedItems(getPinnedItems().filter(i => i.id !== id));
      document.querySelectorAll(`.msg-pin-btn[data-msg-id="${id}"]`)
        .forEach(b => b.classList.remove('pinned'));
      document.querySelectorAll(`.msg.assistant[data-msg-id="${id}"]`)
        .forEach(b => b.classList.remove('pinned-sel'));
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
  memoryPath.textContent = `~/.squid/context/topics/${topic}/memory.md`;
  memoryTokenCount.textContent = '';
  memoryModal.classList.add('open');
  try {
    const data = await fetch(`/topics/${encodeURIComponent(topic)}/memory`).then(r => r.json());
    _memoryCache[topic] = { ...data, loading: false };
    memoryEditor.value = data.content || '';
    memoryPath.textContent = data.path || `~/.squid/context/topics/${topic}/memory.md`;
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
  const oldRevision = _memoryRevision(_memoryCache[topic]);
  const idleLabel = 'Save';
  memorySaveBtn.disabled = true;
  memorySaveBtn.textContent = 'Saving...';
  memoryPath.textContent = `~/.squid/context/topics/${topic}/memory.md`;
  try {
    const res = await fetch(`/topics/${encodeURIComponent(topic)}/memory`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: memoryEditor.value }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Save failed (${res.status})`);
    if (_memoryRevision(data) !== oldRevision) {
      _clearMemorySelectionOverridesForTopic(topic);
      _clearSessionLookupCacheForTopic(topic);
    }
    _memoryCache[topic] = { ...data, loading: false };
    memoryPath.textContent = `${data.path || `~/.squid/context/topics/${topic}/memory.md`} · saved`;
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
  updateInContextMarkers();
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
  if (getPinnedItems().find(i => i.id === msgId)) {
    btn.classList.add('pinned');
    bubbleEl.classList.add('pinned-sel');
  }
  btn.addEventListener('click', e => {
    e.stopPropagation();
    const pinned = getPinnedItems();
    if (pinned.find(i => i.id === msgId)) {
      setPinnedItems(pinned.filter(i => i.id !== msgId));
      btn.classList.remove('pinned');
      bubbleEl.classList.remove('pinned-sel');
    } else if (btn.classList.contains('lookback-sel')) {
      _lookbackUnselected.add(msgId);
      updateInContextMarkers();
    } else {
      const text = _messageBodyText(bubbleEl).slice(0, 300);
      const sid = bubbleEl.dataset.sessionId || null;
      setPinnedItems([...pinned, { id: msgId, topic, agent: agent || null, session_id: sid, content: text }]);
      btn.classList.add('pinned');
      bubbleEl.classList.add('pinned-sel');
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
document.getElementById('search-bar-clear').addEventListener('click', clearSearch);

function formatFilterCommand(state) {
  let scope = '';
  if (state.topic) scope = '#' + state.topic;
  if (state.agent) {
    scope += '@' + state.agent;
    if (state.adhoc === true) scope += '!';
    else if (state.adhoc === null) scope += '*';
  }
  return scope ? `/f ${scope}` : '/f reset';
}

function stashComposerAndEdit(command) {
  const prev = input.value.trim();
  commandEditRestore = prev && prev !== command ? prev : null;
  if (prev && prev !== command) {
    recordPrompt(prev);
    const hint = document.createElement('span');
    hint.className = 'restore-hint';
    hint.textContent = '↑ restore';
    document.getElementById('tag-bar').appendChild(hint);
    setTimeout(() => {
      hint.classList.add('fade');
      hint.addEventListener('transitionend', () => hint.remove(), { once: true });
    }, 1800);
  }
  input.value = command;
  input.focus();
  input.setSelectionRange(command.length, command.length);
  resizeComposer();
}

function editActiveFilter() {
  const active = (searchActive && searchState) ? searchState : historyFilter;
  stashComposerAndEdit(formatFilterCommand(active));
}

function formatSearchCommand(state) {
  let cmd = '/s ';
  if (state.explicitAll || state.topic) {
    cmd += state.explicitAll ? '#all' : '#' + state.topic;
    if (state.agent) cmd += '@' + state.agent;
    if (state.agent && state.adhoc === true) cmd += '!';
    else if (state.agent && state.adhoc === null) cmd += '*';
    cmd += ' ';
  } else if (state.agent) {
    cmd += '@' + state.agent;
    if (state.adhoc === true) cmd += '!';
    else if (state.adhoc === null) cmd += '*';
    cmd += ' ';
  }
  return (cmd + state.keywords).trim();
}

document.getElementById('search-bar-keywords').addEventListener('click', () => {
  if (!searchActive || !searchState) return;
  const cmd = formatSearchCommand(searchState);
  stashComposerAndEdit(cmd);
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

let _fvNavigate = null;

function _fmtSize(bytes) {
  if (bytes == null) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

function _fmtMtime(ts) {
  if (ts == null) return '';
  const diff = (Date.now() / 1000) - ts;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
  return new Date(ts * 1000).toLocaleDateString();
}

function openFileViewer(initialPath, initialLine, initialEndLine) {
  document.getElementById('file-modal')?.remove();
  _fvNavigate = null;

  const navHistory = [{ path: initialPath, line: initialLine, endLine: initialEndLine }];
  let historyIdx = 0;
  let path = initialPath;
  let line = initialLine;
  let endLine = initialEndLine;

  // ── DOM ──────────────────────────────────────────────────────────────────────
  const modal = document.createElement('div');
  modal.id = 'file-modal';

  const box = document.createElement('div');
  box.id = 'file-modal-box';

  const header = document.createElement('div');
  header.id = 'file-modal-header';

  const navBtns = document.createElement('div');
  navBtns.className = 'fv-nav-btns';
  const backBtn = document.createElement('button');
  backBtn.className = 'fv-nav-btn';
  backBtn.setAttribute('aria-label', 'Back');
  backBtn.textContent = '‹';
  const fwdBtn = document.createElement('button');
  fwdBtn.className = 'fv-nav-btn';
  fwdBtn.setAttribute('aria-label', 'Forward');
  fwdBtn.textContent = '›';
  navBtns.append(backBtn, fwdBtn);

  const breadcrumb = document.createElement('div');
  breadcrumb.id = 'file-modal-breadcrumb';

  const actions = document.createElement('div');
  actions.className = 'fv-header-actions';
  const copyBtn = document.createElement('button');
  copyBtn.className = 'fv-action-btn';
  copyBtn.title = 'Copy path';
  copyBtn.textContent = '⎘';
  const closeBtn = document.createElement('button');
  closeBtn.id = 'file-modal-close';
  closeBtn.textContent = '×';
  actions.append(copyBtn, closeBtn);

  header.append(navBtns, breadcrumb, actions);

  const body = document.createElement('div');
  body.id = 'file-modal-body';
  body.textContent = 'Loading…';

  box.append(header, body);
  modal.appendChild(box);
  document.body.appendChild(modal);

  // ── Navigation ───────────────────────────────────────────────────────────────
  function navigate(newPath, newLine = null, newEndLine = null) {
    navHistory.splice(historyIdx + 1);
    navHistory.push({ path: newPath, line: newLine, endLine: newEndLine });
    historyIdx = navHistory.length - 1;
    path = newPath; line = newLine; endLine = newEndLine;
    updateNav();
    loadFile();
  }

  function updateNav() {
    backBtn.disabled = historyIdx === 0;
    fwdBtn.disabled = historyIdx === navHistory.length - 1;
    breadcrumb.innerHTML = '';
    const parts = path.split('/').filter(Boolean);
    const addSep = () => {
      const s = document.createElement('span');
      s.className = 'fv-crumb-sep';
      s.textContent = '/';
      breadcrumb.appendChild(s);
    };
    const addCrumb = (label, crumbPath) => {
      const isLast = crumbPath === null;
      const el = document.createElement(isLast ? 'span' : 'a');
      el.className = 'fv-crumb' + (isLast ? ' fv-crumb-current' : '');
      el.textContent = label;
      if (!isLast) {
        el.href = '#';
        el.addEventListener('click', e => { e.preventDefault(); navigate(crumbPath); });
      }
      breadcrumb.appendChild(el);
    };
    addCrumb('/', parts.length ? '/' : null);
    parts.forEach((part, i) => {
      addSep();
      const isLast = i === parts.length - 1;
      addCrumb(part, isLast ? null : '/' + parts.slice(0, i + 1).join('/'));
    });
    if (window.matchMedia?.('(max-width: 768px)').matches) {
      requestAnimationFrame(() => { breadcrumb.scrollLeft = breadcrumb.scrollWidth; });
    }
  }

  // ── Events ───────────────────────────────────────────────────────────────────
  backBtn.addEventListener('click', () => {
    if (historyIdx > 0) {
      historyIdx--;
      ({ path, line, endLine } = navHistory[historyIdx]);
      updateNav(); loadFile();
    }
  });
  fwdBtn.addEventListener('click', () => {
    if (historyIdx < navHistory.length - 1) {
      historyIdx++;
      ({ path, line, endLine } = navHistory[historyIdx]);
      updateNav(); loadFile();
    }
  });
  copyBtn.addEventListener('click', () => {
    navigator.clipboard?.writeText(path).then(() => {
      copyBtn.textContent = '✓';
      setTimeout(() => { copyBtn.textContent = '⎘'; }, 1500);
    });
  });
  const closeModal = () => { modal.remove(); _fvNavigate = null; };
  closeBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
  const escHandler = e => { if (e.key === 'Escape') { closeModal(); document.removeEventListener('keydown', escHandler); } };
  document.addEventListener('keydown', escHandler);

  _fvNavigate = navigate;

  // ── Content ──────────────────────────────────────────────────────────────────
  function showAllowRoot() {
    const hint = path.split('/').slice(0, -1).join('/') || '/';
    body.innerHTML = '';
    const panel = document.createElement('div');
    panel.className = 'fv-config-hint';
    panel.innerHTML = '<strong>Path not in allowed roots</strong>' +
      '<p>Choose an existing parent directory to add to <code>server.localfile_roots</code>.</p>' +
      '<p>All files beneath the selected directory will become readable in the Squid web UI.</p>';
    const row = document.createElement('div');
    row.className = 'fv-root-row';
    const rootInput = document.createElement('input');
    rootInput.value = hint;
    rootInput.spellcheck = false;
    rootInput.setAttribute('aria-label', 'Directory to allow');
    const allowBtn = document.createElement('button');
    allowBtn.type = 'button';
    allowBtn.textContent = 'Allow directory';
    const status = document.createElement('div');
    status.className = 'fv-root-status';
    row.append(rootInput, allowBtn);
    panel.append(row, status);
    body.appendChild(panel);
    allowBtn.addEventListener('click', async () => {
      allowBtn.disabled = true;
      status.textContent = 'Updating configuration…';
      try {
        const res = await fetch('/config/localfile-roots', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path, root: rootInput.value.trim() }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to allow directory');
        status.textContent = 'Allowed. Loading file…';
        await loadFile();
      } catch (err) {
        status.textContent = err.message || 'Failed to update configuration.';
        allowBtn.disabled = false;
      }
    });
  }

  async function loadFile() {
    body.textContent = 'Loading…';
    try {
      const res = await fetch('/localfile?' + new URLSearchParams({ path }));
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        const err = errData.error || '';
        if (res.status === 403 && (err.includes('localfile_roots') || err.includes('outside allowed roots'))) {
          showAllowRoot();
        } else {
          body.textContent = err || `Error ${res.status}`;
        }
        return;
      }
      const ct = res.headers.get('content-type') || '';
      if (!ct.includes('text/') && !ct.includes('application/json') && !_isTextPath(path)) {
        modal.remove(); _fvNavigate = null;
        window.open('/localfile?' + new URLSearchParams({ path }), '_blank');
        return;
      }
      const text = await res.text();
      if (ct.includes('application/json')) {
        try {
          const data = JSON.parse(text);
          if (data.type === 'directory') {
            if (data.path !== path) { path = data.path; updateNav(); }
            _renderDirListing(body, data);
            return;
          }
        } catch {}
      }
      _renderFileViewer(body, text, line, endLine, path);
    } catch {
      body.textContent = 'Failed to load file.';
    }
  }

  updateNav();
  loadFile();
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

function _renderDirListing(container, data) {
  container.innerHTML = '';

  const filterWrap = document.createElement('div');
  filterWrap.className = 'fv-filter-wrap';
  const filterInput = document.createElement('input');
  filterInput.className = 'fv-filter';
  filterInput.type = 'text';
  filterInput.placeholder = 'Filter…';
  filterInput.setAttribute('aria-label', 'Filter files');
  filterWrap.appendChild(filterInput);
  container.appendChild(filterWrap);

  const list = document.createElement('div');
  list.className = 'fv-dir-listing';
  container.appendChild(list);

  const hasMeta = data.entries.some(e => e.size != null || e.mtime != null);

  function renderEntries(entries) {
    list.innerHTML = '';
    if (!entries.length) {
      const empty = document.createElement('div');
      empty.className = 'fv-dir-empty';
      empty.textContent = filterInput.value ? 'No matches' : '(empty)';
      list.appendChild(empty);
      return;
    }
    entries.forEach(entry => {
      const a = document.createElement('a');
      a.className = 'fv-dir-entry' + (entry.is_dir ? ' fv-dir-entry--dir' : '');
      a.href = '/localfile?' + new URLSearchParams({ path: entry.path });

      const nameSpan = document.createElement('span');
      nameSpan.className = 'fv-dir-name';
      nameSpan.textContent = entry.is_dir ? entry.name + '/' : entry.name;
      a.appendChild(nameSpan);

      if (hasMeta) {
        const sizeSpan = document.createElement('span');
        sizeSpan.className = 'fv-dir-meta';
        sizeSpan.textContent = entry.is_dir ? '' : _fmtSize(entry.size);
        a.appendChild(sizeSpan);

        const mtimeSpan = document.createElement('span');
        mtimeSpan.className = 'fv-dir-meta';
        mtimeSpan.textContent = _fmtMtime(entry.mtime);
        a.appendChild(mtimeSpan);
      }

      list.appendChild(a);
    });
  }

  filterInput.addEventListener('input', () => {
    const q = filterInput.value.trim().toLowerCase();
    renderEntries(q ? data.entries.filter(e => e.name.toLowerCase().includes(q)) : data.entries);
  });

  renderEntries(data.entries);
  requestAnimationFrame(() => filterInput.focus());
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
  const pathVal = url.searchParams.get('path') || '';
  e.preventDefault();
  const hm = url.hash.match(/^#L(\d+)(?:-L(\d+))?$/);
  const lineVal = hm ? parseInt(hm[1], 10) : null;
  const endLineVal = hm?.[2] ? parseInt(hm[2], 10) : null;
  if (_fvNavigate && document.getElementById('file-modal')) {
    _fvNavigate(pathVal, lineVal, endLineVal);
  } else {
    openFileViewer(pathVal, lineVal, endLineVal);
  }
});

initHistoryScroll();
initPromptHistory();
initStats();
initTopicsView();
initAliases();
initQuota();
initDeepSeekQuota();
initDeepSeekMaxPopup();
initCreds();
initCodexQuota();
initCodexCreds();
initCursorQuota();
updateActiveQuotaGauge();
initPullToRefresh();
// Discover processes that survived a refresh; polling stops again when idle.
startProcPoll();
showBootBanner();
try {
  const saved = JSON.parse(localStorage.getItem('squid_sticky_chip') || 'null');
  if (saved?.topic) {
    setTopicChip(saved.topic, saved.agent || null, saved.adhoc || false, saved.lookback || 0);
  } else {
    _acAgents().then(agents => {
      if (stickyChip) return;
      const first = agents[0];
      if (first) setTopicChip('default', first.name, true, 0);
    }).catch(() => {});
  }
} catch { /* ignore */ }

// Patch bookmarklet hrefs with the actual origin (avoids hardcoding the port).
document.querySelectorAll('.creds-bookmarklet').forEach(a => {
  a.href = a.href.replace('SQUID_ORIGIN', location.origin);
});

// ── tab visibility recovery ───────────────────────────────────────────────────
// When the user switches away and back, scroll to show the current streaming
// state and immediately trigger the status poll if we're in recovery mode.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    messages.scrollTop = messages.scrollHeight;
    if (_activePollImmediate) _activePollImmediate();
    startProcPoll();
  }
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
