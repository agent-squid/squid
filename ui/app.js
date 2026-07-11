
const messages     = document.getElementById('messages');
const form         = document.getElementById('form');
const input        = document.getElementById('input');
const scrollBtn    = document.getElementById('scroll-btn');
const statsContent = document.getElementById('stats-content');
const helpBtn      = document.getElementById('help-btn');
const helpPanel    = document.getElementById('help-panel');
const acEl         = document.getElementById('autocomplete');
const pinBtn          = document.getElementById('pin-btn');
const pinPanel        = document.getElementById('pin-panel');
const pinCountEl      = document.getElementById('pin-count');
const bookmarkBtn     = document.getElementById('chip-bookmark-btn');

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
    navigator.serviceWorker.register('/sw.js').then(reg => {
      // Mobile PWAs (esp. iOS standalone) rarely re-check for SW updates on relaunch,
      // so force a check on load and whenever the app regains foreground.
      reg.update();
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') reg.update();
      });
    }).catch(() => {});
  });
  let swRefreshing = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (swRefreshing) return;
    swRefreshing = true;
    window.location.reload();
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
    if (_isSquidWorktreePath(path)) return '#';
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
const MOBILE_VIEW_ORDER = ['chat', 'files', 'topics', 'agents', 'stats', 'community', 'settings'];
const VIEW_LABELS = {
  chat: 'Chat',
  files: 'Files',
  topics: 'Topics',
  agents: 'Agents',
  stats: 'Stats',
  community: 'Community',
  settings: 'Settings',
};
let _mobileViewHistoryDepth = 0;

function isMobileViewport() {
  return window.matchMedia?.('(max-width: 768px)').matches || window.innerWidth <= 768;
}

function switchView(name) {
  if (!MOBILE_VIEW_ORDER.includes(name)) return;
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  document.querySelectorAll('.nav-tab, .hmenu-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === name);
  });
  const mobileTitle = document.getElementById('mobile-view-title');
  if (mobileTitle) mobileTitle.textContent = VIEW_LABELS[name] || name;
  currentView = name;
  document.getElementById('right-rail')?.classList.toggle('quota-chat-hidden', name !== 'chat');
  if (name !== 'chat') {
    document.querySelectorAll('#quota-creds-popup, #codex-creds-popup, #cursor-creds-popup, #deepseek-max-popup')
      .forEach(popup => popup.classList.remove('open'));
  }
  if (name === 'files') openFilesTabView();
  if (name === 'topics') loadTopicsView();
  if (name === 'stats') loadStats();
  if (name === 'agents') loadAgents();
  if (name === 'settings') loadConfigYaml();
}

function navigateView(name, { recordHistory = true } = {}) {
  if (name === currentView) return;
  switchView(name);
  if (recordHistory && isMobileViewport() && history.pushState) {
    history.pushState({ squidView: name }, '', location.href);
    _mobileViewHistoryDepth += 1;
  }
}

function initMobileViewNavigation() {
  if (history.replaceState) history.replaceState({ squidView: currentView }, '', location.href);

  window.addEventListener('popstate', e => {
    const name = e.state?.squidView || 'chat';
    _mobileViewHistoryDepth = Math.max(0, _mobileViewHistoryDepth - 1);
    switchView(name);
  });

  const app = document.getElementById('app');
  if (!app) return;

  let swipeStart = null;
  const EDGE_EXCLUSION = 24; // px from screen edge reserved for OS back-gesture; don't start our swipe there
  const ignoredSwipeTarget = target => !!target.closest(
    'input, textarea, select, button, a, [contenteditable="true"], .cm-editor, #input-area, #stats-content, #file-modal-body, iframe'
  );

  app.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse') return;
    if (!isMobileViewport() || ignoredSwipeTarget(e.target)) return;
    if (e.clientX < EDGE_EXCLUSION || e.clientX > window.innerWidth - EDGE_EXCLUSION) return;
    swipeStart = { x: e.clientX, y: e.clientY, id: e.pointerId };
  });

  app.addEventListener('pointerup', e => {
    if (!swipeStart || (e.pointerId != null && e.pointerId !== swipeStart.id)) return;
    const dx = e.clientX - swipeStart.x;
    const dy = e.clientY - swipeStart.y;
    swipeStart = null;
    if (Math.abs(dx) < 70 || Math.abs(dx) < Math.abs(dy) * 1.5) return;

    if (dx > 0) {
      if (_mobileViewHistoryDepth > 0) history.back();
      else {
        const prev = MOBILE_VIEW_ORDER.indexOf(currentView) - 1;
        if (prev >= 0) navigateView(MOBILE_VIEW_ORDER[prev]);
      }
    } else {
      const next = MOBILE_VIEW_ORDER.indexOf(currentView) + 1;
      if (next < MOBILE_VIEW_ORDER.length) navigateView(MOBILE_VIEW_ORDER[next]);
    }
  });

  app.addEventListener('pointercancel', () => { swipeStart = null; });
}

async function doRefresh() {
  /* Clear all Cache API caches, unregister service workers, then reload. */
  try {
    if ('caches' in window) {
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
    }
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map(r => r.unregister()));
    }
  } catch (_) {/* best-effort */}
  window.location.reload();
}

function initSettings() {
  document.querySelectorAll('.nav-tab').forEach(btn =>
    btn.addEventListener('click', () => {
      hamburgerMenu.classList.remove('open');
      hamburgerBtn.classList.remove('active');
      navigateView(btn.dataset.view);
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
      if (btn.id === 'hmenu-refresh') return;
      hamburgerMenu.classList.remove('open');
      hamburgerBtn.classList.remove('active');
      navigateView(btn.dataset.view);
    })
  );
  document.getElementById('hmenu-refresh')?.addEventListener('click', e => {
    e.stopPropagation();
    hamburgerMenu.classList.remove('open');
    hamburgerBtn.classList.remove('active');
    doRefresh()
  });
  document.addEventListener('click', e => {
    if (!hamburgerMenu.contains(e.target) && !hamburgerBtn.contains(e.target)) {
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
  closeBtn.innerHTML = '<span class="close-desktop">Esc</span><span class="close-mobile">✕</span>';
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
const _sessionTurnCounts = {}; // session_id → turn count
const sessionAdvisoryEl    = document.getElementById('session-advisory');
const sessionAdvisoryMsgEl = document.getElementById('session-advisory-msg');
let _advisoryTurnCount = 0;
let _advisoryDismissKey = null;
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
    delete _sessionTurnCounts[sid];
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
const chipSearchBtn = document.getElementById('chip-search-btn');
const chipClearBtn = document.getElementById('chip-clear-btn');
const chipStashBtn = document.getElementById('chip-stash-btn');
let stickyChip = null; // { topic, agent, adhoc } | null
let editingExpandedSlug = false;
let expandedSlugEditToken = 0;
let composerActionTitleSeq = 0;

function setTopicChip(topic, agent, adhoc = false, lookback = 0) {
  editingExpandedSlug = false;
  expandedSlugEditToken++;
  stickyChip = { topic, agent, adhoc, lookback };
  _advisoryTurnCount = 0;
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
  } else {
    clearTimeout(_chipTurnCountTimer);
    _chipTurnCountTimer = null;
    const sid = _sessionIds[`${topic}@${agent || '_'}`];
    const count = sid ? (_sessionTurnCounts[sid] || 0) : 0;
    if (count > 0) {
      _renderChipTurnCount(count);
    } else {
      _scheduleChipTurnCountUpdate(topic, agent);
    }
  }
  topicChipEl.classList.add('visible');
  topicChipEl.classList.remove('needs-agent');
  chipRow.hidden = false;
  input.placeholder = 'message…';
  updateComposerActionTitles();
  updateActiveQuotaGauge();
  updatePinCount();
  updateInContextMarkers();
  _lastContextIndicatorKey = _contextIndicatorKeyFrom(topic, agent, adhoc, lookback);
  evaluateAdvisory();
}

function clearTopicChip() {
  stickyChip = null;
  localStorage.removeItem('squid_sticky_chip');
  topicChipEl.classList.remove('visible', 'needs-agent');
  input.placeholder = '#topic or #topic@agent message…';
  updateComposerActionTitles();
  updateActiveQuotaGauge();
  _lastContextIndicatorKey = '';
  hideAdvisory();
}

function _renderChipTurnCount(count) {
  let tcSpan = topicChipEl.querySelector('.chip-turn-count');
  if (count <= 0) { tcSpan?.remove(); return; }
  if (!tcSpan) {
    tcSpan = document.createElement('span');
    tcSpan.className = 'chip-turn-count';
    topicChipEl.appendChild(tcSpan);
  }
  tcSpan.textContent = `·${count}t`;
  tcSpan.classList.toggle('mid', count > 10 && count <= 20);
  tcSpan.classList.toggle('high', count > 20);
}

function _updateChipTurnCount(topic, agent, sessionId, count) {
  if (sessionId && count > 0) _sessionTurnCounts[sessionId] = count;
  if (!stickyChip || stickyChip.adhoc || stickyChip.topic !== topic || (stickyChip.agent || null) !== (agent || null)) return;
  _renderChipTurnCount(count);
}

let _chipTurnCountTimer = null;

function _scheduleChipTurnCountUpdate(topic, agent) {
  clearTimeout(_chipTurnCountTimer);
  _chipTurnCountTimer = setTimeout(() => {
    _chipTurnCountTimer = null;
    if (!stickyChip || stickyChip.adhoc || stickyChip.topic !== topic || (stickyChip.agent || null) !== (agent || null)) return;
    const sid = _sessionIds[`${topic}@${agent || '_'}`];
    if (!sid) return;
    const count = _sessionTurnCounts[sid] || 0;
    if (count > 0) _renderChipTurnCount(count);
  }, 700);
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

function routeScopeText(route) {
  const topic = route?.topic || 'default';
  let scope = `#${topic}`;
  if (route?.agent) {
    scope += `@${route.agent}`;
    if (route.adhoc) scope += '!';
  }
  return scope;
}

function searchScopeText(state) {
  if (!state) return '';
  let scope = '';
  if (state.explicitAll) scope = '#all';
  else if (state.topic) scope = `#${state.topic}`;
  if (state.agent) {
    scope += `@${state.agent}`;
    if (state.adhoc === true) scope += '!';
    else if (state.adhoc === null) scope += '+';
  }
  return scope;
}

function activeHistoryFilterScope() {
  return (historyFilter.topic || historyFilter.agent) ? searchScopeText(historyFilter) : '';
}

async function resolveEffectiveComposerRoute() {
  if (input.value.trimStart().startsWith('#')) {
    const parsed = parseInput(input.value);
    return { topic: parsed.topic || 'default', agent: parsed.agent || null, adhoc: !!parsed.adhoc, lookback: parsed.lookback || 0 };
  }
  if (stickyChip) return { ...stickyChip };

  let topic = 'default';
  let agent = null;
  let adhoc = true;
  try {
    const topics = await _acTopics();
    const defaultTopic = topics.find(t => t.name === 'default');
    if (defaultTopic?.agent) {
      agent = defaultTopic.agent;
      adhoc = !!defaultTopic.sticky_adhoc;
    }
  } catch { /* ignore */ }
  if (!agent) {
    try {
      const agents = await _acAgents();
      if (agents[0]?.name) agent = agents[0].name;
    } catch { /* ignore */ }
  }
  return { topic, agent, adhoc, lookback: 0 };
}

async function updateComposerActionTitles() {
  const seq = ++composerActionTitleSeq;
  const route = await resolveEffectiveComposerRoute();
  if (seq !== composerActionTitleSeq) return;
  const scope = routeScopeText(route);
  chipFilterBtn.title = `Filter history by ${scope}`;
  chipSearchBtn.title = `Search ${scope}`;
  chipClearBtn.title = 'Insert /clear';
  chipStashBtn.title = `Stash prompt for autocomplete (${scope})`;
}

chipFilterBtn.addEventListener('click', async e => {
  e.stopPropagation();
  const active = (searchActive && searchState) ? searchState : historyFilter;
  if (active?.topic || active?.agent || active?.explicitAll) {
    clearFilter();
    return;
  }
  const route = await resolveEffectiveComposerRoute();
  if (route.agent) filterByAgent(route.topic, route.agent, route.adhoc, route.lookback || 0);
  else filterByTopic(route.topic);
});

chipSearchBtn.addEventListener('click', async e => {
  e.stopPropagation();
  if (searchActive) {
    clearSearch();
    return;
  }
  if (input.value.trimStart().startsWith('/s')) return;
  const parsed = parseInput(input.value);
  const keywords = input.value.trimStart().startsWith('#') ? parsed.message.trim() : input.value.trim();
  let scope = activeHistoryFilterScope();
  if (!scope) scope = routeScopeText(await resolveEffectiveComposerRoute());
  const command = keywords ? `/s ${scope} ${keywords}` : `/s ${scope} `;
  stashComposerAndEdit(command);
});

chipClearBtn.addEventListener('click', e => {
  e.stopPropagation();
  stashComposerAndEdit('/clear');
});

chipStashBtn.addEventListener('click', async e => {
  e.stopPropagation();
  const text = input.value;
  const parsed = parseInput(text);
  const message = parsed.message.trim();
  if (!message) {
    _acRender(promptHistoryAutocompleteItems(matchingPromptHistory('', Infinity)), 'Recent Prompts');
    acSel = -1;
    _acHighlight();
    input.focus();
    return;
  }
  const route = await resolveEffectiveComposerRoute();
  const entry = formatPromptHistoryEntry(route.topic, route.agent, route.adhoc, route.lookback || 0, message);
  recordPrompt(entry);
  saveStashedPrompt(entry);
  input.value = '';
  localStorage.removeItem('squid_draft');
  resizeComposer();
  _acRender(promptHistoryAutocompleteItems(matchingPromptHistory('', Infinity)), 'Recent Prompts');
  acSel = -1;
  _acHighlight();
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
  wrap.className = 'topic-tag' + (clickable ? ' clickable' : '');

  const tSpan = document.createElement('span');
  tSpan.className = 'tag-topic';
  tSpan.textContent = '#' + topic;
  wrap.appendChild(tSpan);

  if (agent) {
    const aSpan = document.createElement('span');
    aSpan.className = 'tag-agent';
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
      if (
        !stickyChip ||
        stickyChip.topic !== topic ||
        stickyChip.agent !== (agent || null) ||
        !!stickyChip.adhoc !== !!adhoc ||
        (stickyChip.lookback || 0) !== (lookback || 0)
      ) {
        setTopicChip(topic, agent || null, adhoc, lookback);
      }
    });
  }

  return wrap;
}

// ── history filter ─────────────────────────────────────────────────────────────

let historyFilter = { topic: null, agent: null, adhoc: null };
let promptOnlyHistory = false;

function updatePromptOnlyButton() {
  const btn = document.getElementById('chip-prompts-btn');
  if (!btn) return;
  btn.classList.toggle('active', promptOnlyHistory);
  btn.setAttribute('aria-pressed', promptOnlyHistory ? 'true' : 'false');
  btn.title = promptOnlyHistory ? 'Show full thread' : 'User prompts only';
}

function updateFilterButton() {
  const active = (searchActive && searchState) ? searchState : historyFilter;
  const isFiltered = !!(active?.topic || active?.agent || active?.explicitAll);
  chipFilterBtn?.classList.toggle('active', isFiltered);
  chipFilterBtn?.setAttribute('aria-pressed', isFiltered ? 'true' : 'false');
}

function updateSearchButton() {
  chipSearchBtn?.classList.toggle('active', !!searchActive);
  chipSearchBtn?.setAttribute('aria-pressed', searchActive ? 'true' : 'false');
}

function hasHistoryFilterScope() {
  return !!(historyFilter.topic || historyFilter.agent || historyFilter.explicitAll);
}

function persistSearchFilterScope(state) {
  if (!hasHistoryFilterScope()) return;
  historyFilter = {
    topic: state.topic || null,
    agent: state.agent || null,
    adhoc: state.adhoc ?? null,
    explicitAll: !!state.explicitAll,
  };
}

function togglePromptOnlyHistory() {
  promptOnlyHistory = !promptOnlyHistory;
  if (promptOnlyHistory && bookmarkOnlyHistory) {
    bookmarkOnlyHistory = false;
    updateBookmarkButton();
  }
  updatePromptOnlyButton();
  if (searchActive) {
    clearSearch();
  } else {
    reloadHistory(historyFilter);
  }
}

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
    persistSearchFilterScope(searchState);
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
  // Preserve user bubbles and their timestamps that precede an active thinking bubble
  const preserveForLive = new Set();
  document.querySelectorAll('#messages > .msg-thinking').forEach(thinking => {
    let el = thinking.previousElementSibling;
    while (el && (el.classList.contains('msg-time') || (el.classList.contains('msg') && el.classList.contains('user')))) {
      preserveForLive.add(el);
      el = el.previousElementSibling;
    }
  });
  document.querySelectorAll('#messages > .msg:not(.msg-thinking), #messages > .msg-thinking-done, #messages > .msg-time, #messages > .stats').forEach(el => {
    if (!preserveForLive.has(el)) el.remove();
  });
  _updateFilterBadge();
  if (bookmarkOnlyHistory) {
    loadBookmarkHistory();
  } else {
    initHistoryScroll();
  }
}

async function loadBookmarkHistory() {
  const bookmarked = getBookmarkedItems();
  if (!bookmarked.length) {
    refreshDateDividers();
    return;
  }
  const ids = bookmarked.map(i => i.id).join(',');
  let data;
  try {
    const res = await fetch(`/history/by-ids?ids=${ids}`);
    data = await res.json();
  } catch {
    return;
  }
  const { items } = data;
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    if (!itemMatchesFilter(item, historyFilter)) continue;
    if (!item.content) continue;
    appendHistoryItem(item, fragment);
  }
  messages.appendChild(fragment);
  messages.scrollTop = messages.scrollHeight;
  updateInContextMarkers();
  refreshAllRevertButtons();
  evaluateAdvisory();
  refreshDateDividers();
}

function _updateFilterBadge() {
  const badge = document.getElementById('filter-badge');
  const labelEl = document.getElementById('filter-badge-label');
  const activeState = (searchActive && searchState) ? searchState : historyFilter;
  const { topic, agent, adhoc } = activeState;
  const explicitAll = !!activeState.explicitAll;

  if (!topic && !agent && !explicitAll) {
    badge.classList.remove('active');
    updateFilterButton();
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
  updateFilterButton();
}

function itemMatchesFilter(item, filter) {
  if (!filter) return true;
  if (filter.topic && (item.topic || 'default') !== filter.topic) return false;
  if (filter.agent && (item.agent || null) !== filter.agent) return false;
  if (filter.adhoc !== null && filter.adhoc !== undefined && !!item.adhoc !== filter.adhoc) return false;
  return true;
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
    persistSearchFilterScope(searchState);
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
  const cleanup = pendingPollTimers.get(bubble);
  if (typeof cleanup === 'function') cleanup();
  else if (cleanup) clearInterval(cleanup);
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
const STASHED_PROMPTS_KEY = 'squid_stashed_prompts';
const HIDDEN_PROMPTS_KEY  = 'squid_hidden_prompts';
let hiddenPromptKeys = new Set(JSON.parse(localStorage.getItem(HIDDEN_PROMPTS_KEY) || '[]'));

function createTopSentinel() {
  const el = document.createElement('div');
  el.id = 'history-sentinel';
  return el;
}

async function loadHistory() {
  if (bookmarkOnlyHistory) return;
  if (historyExhausted || historyLoading) return;
  historyLoading = true;
  const generation = historyGeneration;

  let data;
  try {
    let url = `/history?offset=${historyOffset}&limit=5`;
    const applyRouteFilter = !bookmarkOnlyHistory;
    if (applyRouteFilter && historyFilter.topic) url += `&topic=${encodeURIComponent(historyFilter.topic)}`;
    if (applyRouteFilter && historyFilter.agent) url += `&agent=${encodeURIComponent(historyFilter.agent)}`;
    if (applyRouteFilter && historyFilter.adhoc != null) url += `&adhoc=${historyFilter.adhoc}`;
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
    // Skip if a bubble for this message is already in the DOM — e.g. an
    // in-progress live (SSE) bubble that survived a search → back round-trip.
    // Without this, loadHistory would render a second, polling-driven bubble
    // for the same message alongside the live one.
    if (item.id != null && messages.querySelector(`[data-msg-id="${item.id}"]`)) continue;

    if (promptOnlyHistory) {
      appendPromptOnlyHistoryItem(item, fragment);
      continue;
    }

    if (!item.content && item.status !== 'pending') continue;

    if (item.status === 'pending') {
      const wipBubble = makeWipBubble(item);
      fragment.appendChild(wipBubble);
      reconnectPendingItem(item, wipBubble);
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
  evaluateAdvisory();
  refreshDateDividers();

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

  const topicMatch = scope.match(/^#([\w-]+)(?:@([\w-]+)([!+])?)?$/);
  if (topicMatch) {
    const agent = topicMatch[2] || null;
    const mode = topicMatch[3] || '';
    return {
      topic: topicMatch[1].toLowerCase(),
      agent,
      adhoc: agent ? (mode === '+' ? null : mode === '!') : null,
      explicitAll: false,
    };
  }

  const agentMatch = scope.match(/^@([\w-]+)([!+])?$/);
  if (agentMatch) {
    const mode = agentMatch[2] || '';
    return {
      topic: null,
      agent: agentMatch[1],
      adhoc: mode === '+' ? null : mode === '!',
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
    updateSearchButton();
    return;
  }

  kwEl.textContent = searchState.keywords;
  bar.classList.add('active');
  updateSearchButton();
  _updateFilterBadge();
}

function searchHighlightTerms(keywords) {
  const terms = [];
  for (const keyword of keywords) {
    const text = String(keyword || '');
    const tokens = text.match(/[\p{L}\p{N}_]+/gu);
    terms.push(...(tokens?.length ? tokens : [text]));
  }
  return terms;
}

function highlightTextNodes(root, keywords) {
  const escapedKeywords = [...new Set(searchHighlightTerms(keywords).filter(Boolean))]
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
  updateSearchButton();

  document.querySelectorAll('.search-result-item, .date-divider').forEach(el => el.remove());
  document.querySelectorAll('#messages > .cmd-feedback.search-no-results').forEach(el => el.remove());
  if (topSentinel) { topSentinel.remove(); topSentinel = null; }
  historyOffset = 0;
  historyExhausted = false;
  invalidateHistoryLoad();
  _updateFilterBadge();
  if (bookmarkOnlyHistory) {
    loadBookmarkHistory();
  } else {
    initHistoryScroll();
  }
}

function recordPrompt(text) {
  const t = text.trim();
  if (!t) return;
  const key = promptHistoryDedupKey(t);
  promptHistory = [t, ...promptHistory.filter(x => promptHistoryDedupKey(x) !== key)].slice(0, 200);
  saveStashedPrompt(t);
  promptHistoryPos = -1;
  promptDraft = '';
  promptDraftChip = null;
}

function getStashedPrompts() {
  try {
    const items = JSON.parse(localStorage.getItem(STASHED_PROMPTS_KEY) || '[]');
    return Array.isArray(items) ? items.filter(x => typeof x === 'string' && x.trim()) : [];
  } catch {
    return [];
  }
}

function saveStashedPrompt(text) {
  const t = text.trim();
  if (!t) return;
  const key = promptHistoryDedupKey(t);
  const items = [t, ...getStashedPrompts().filter(x => promptHistoryDedupKey(x) !== key)].slice(0, 200);
  localStorage.setItem(STASHED_PROMPTS_KEY, JSON.stringify(items));
}

function hidePrompt(text) {
  const key = promptHistoryDedupKey(text.trim());
  hiddenPromptKeys.add(key);
  localStorage.setItem(HIDDEN_PROMPTS_KEY, JSON.stringify([...hiddenPromptKeys]));
  promptHistory = promptHistory.filter(e => promptHistoryDedupKey(e) !== key);
  const stashed = getStashedPrompts().filter(e => promptHistoryDedupKey(e) !== key);
  localStorage.setItem(STASHED_PROMPTS_KEY, JSON.stringify(stashed));
}

function mergePromptHistory(...groups) {
  const seen = new Set();
  const merged = [];
  groups.flat().forEach(item => {
    const text = String(item || '').trim();
    const key = promptHistoryDedupKey(text);
    if (!text || seen.has(key)) return;
    seen.add(key);
    merged.push(text);
  });
  return merged.slice(0, 200);
}

function dedupePromptHistoryEntries(entries) {
  const seen = new Set();
  return entries.filter(entry => {
    const key = promptHistoryDedupKey(entry);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
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

function promptHistoryDedupKey(entry) {
  const { route, prompt } = splitPromptHistoryEntry(entry);
  return `${normalizePromptHistoryRoute(route).toLowerCase()}\0${prompt}`;
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
  if (!prefix) return dedupePromptHistoryEntries(promptHistory.filter(e => !hiddenPromptKeys.has(promptHistoryDedupKey(e)))).slice(0, limit);

  const currentRoute = currentPromptHistoryRoute().toLowerCase();
  const seen = new Set();
  return promptHistory
    .map((entry, recency) => ({ entry, recency, ...splitPromptHistoryEntry(entry) }))
    .filter(item => !hiddenPromptKeys.has(promptHistoryDedupKey(item.entry)) && item.prompt.toLowerCase().startsWith(prefix))
    .sort((a, b) => {
      const aCurrent = normalizePromptHistoryRoute(a.route).toLowerCase() === currentRoute;
      const bCurrent = normalizePromptHistoryRoute(b.route).toLowerCase() === currentRoute;
      return Number(bCurrent) - Number(aCurrent) || a.recency - b.recency;
    })
    .filter(item => {
      const key = promptHistoryDedupKey(item.entry);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, limit)
    .map(item => item.entry);
}

function promptHistoryAutocompleteItems(entries) {
  const currentRoute = normalizePromptHistoryRoute(currentPromptHistoryRoute()).toLowerCase();
  return entries.map(ph => {
    const { route, prompt } = splitPromptHistoryEntry(ph);
    const promptText = prompt || ph;
    const routeKey = normalizePromptHistoryRoute(route);
    const isDifferentRoute = !!(routeKey && routeKey.toLowerCase() !== currentRoute);
    const routeHtml = isDifferentRoute ? _acRouteHtml(routeKey) : '';
    return {
      label: `<span class="ac-history-prompt">${escapeHtml(truncate(promptText, 55))}</span>`,
      insert: promptText,
      trail: false,
      deletePromptEntry: ph,
      ...(routeHtml ? { routeHtml, fullEntry: `${routeKey} ${promptText}` } : {}),
    };
  });
}

async function initPromptHistory() {
  const draft = localStorage.getItem('squid_draft');
  if (draft) { input.value = draft; resizeComposer(); }
  try {
    const res = await fetch('/prompts/recent?limit=50');
    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data.items)) promptHistory = mergePromptHistory(getStashedPrompts(), data.items);
  } catch { /* ignore */ }
}

async function loadSearchResults() {
  if (searchLoading || !searchState) return;
  searchLoading = true;

  const searchRole = promptOnlyHistory ? 'user' : 'assistant';
  let url = `/search?limit=100&q=${encodeURIComponent(searchState.keywords)}&role=${searchRole}`;
  if (bookmarkOnlyHistory) url += '&bookmarked=true';
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
  const bookmarkedIds = bookmarkOnlyHistory ? _bookmarkIds : null;
  const fragment = document.createDocumentFragment();
  for (const item of [...items].reverse()) {
    if (!item.content && !item.prompt) continue;
    if (item.status === 'pending') continue;
    if (bookmarkedIds && !bookmarkedIds.has(item.id)) continue;
    let el;
    if (promptOnlyHistory) {
      el = appendPromptOnlyHistoryItem(item, fragment);
    } else {
      el = appendHistoryItem(item, fragment);
    }
    if (el) highlightTextNodes(el, kws);
  }
  [...fragment.children].forEach(el => el.classList.add('search-result-item'));
  messages.appendChild(fragment);
  searchLoading = false;
  refreshDateDividers();

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

  if (cmd.command === 'clear') {
    const stoppingRows = await commandWouldStopRunningPrompt(cmd.command, topic, agent);
    if (stoppingRows.length) {
      const route = agent ? `#${topic}@${agent}` : `#${topic}`;
      const ok = await confirmRestartWithRunningPrompts(stoppingRows, {
        header: 'Clear Session',
        title: 'Running prompt will be stopped',
        copy: `Clearing ${route} will stop the prompt currently running before clearing the session.`,
        confirmLabel: 'Clear',
      });
      if (!ok) {
        showCmdFeedback(`${cmd.command} cancelled`);
        return;
      }
    }
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

  if (cmd.command === 'restart') {
    const runningPrompts = await runningPromptsForRestart();
    const ok = !runningPrompts.length || await confirmRestartWithRunningPrompts(runningPrompts);
    if (!ok) {
      showCmdFeedback('restart cancelled');
      return;
    }
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
      // Poll /health until server is back up, then hard-refresh
      const poll = async () => {
        try {
          const r = await fetch('/health');
          if (r.ok) { doRefresh(); return; }
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

async function commandWouldStopRunningPrompt(command, topic, agent) {
  if (command !== 'clear') return [];
  try {
    const res = await fetch('/processes');
    if (!res.ok) return [];
    const rows = await res.json();
    if (!Array.isArray(rows)) return [];
    return rows.filter(row => {
      if (!row || isIdleProc(row)) return false;
      if (row.topic !== topic) return false;
      if (Boolean(row.adhoc)) return false;
      if (agent && row.agent !== agent) return false;
      return true;
    });
  } catch {
    return [];
  }
}

async function runningPromptsForRestart() {
  try {
    const res = await fetch('/processes');
    if (!res.ok) return [];
    const rows = await res.json();
    if (!Array.isArray(rows)) return [];
    return rows.filter(row => row && !isIdleProc(row));
  } catch {
    return [];
  }
}

function confirmRestartWithRunningPrompts(rows, {
  header = 'Restart Squid',
  title = 'Running prompts will be stopped',
  copy = 'Restarting now will stop these active prompts before the server restarts.',
  confirmLabel = 'Restart',
} = {}) {
  return new Promise(resolve => {
    const modal = document.getElementById('restart-modal');
    const list = document.getElementById('restart-modal-processes');
    const confirmBtn = document.getElementById('restart-modal-confirm');
    document.querySelector('#restart-modal .settings-label').textContent = header;
    document.getElementById('restart-modal-title').textContent = title;
    document.getElementById('restart-modal-copy').textContent = copy;
    confirmBtn.textContent = confirmLabel;
    const close = (ok) => {
      modal.classList.remove('open');
      resolve(ok);
    };
    list.innerHTML = rows.map(row => {
      const route = `#${escapeHtml(row.topic || 'default')}${row.agent ? `@${escapeHtml(row.agent)}` : ''}${row.adhoc ? '!' : ''}`;
      const preview = escapeHtml(row.prompt_preview || `message #${row.msg_id || ''}`.trim());
      const duration = row.duration_s != null ? `${escapeHtml(String(row.duration_s))}s` : '';
      return `<div class="restart-process-row">
        <span class="proc-dot"></span>
        <span class="restart-process-route">${route}</span>
        <span class="restart-process-preview">${preview}${duration ? ` · ${duration}` : ''}</span>
      </div>`;
    }).join('');
    confirmBtn.disabled = false;
    modal.classList.add('open');
    confirmBtn.focus();
    modal._resolveRestart = close;
  });
}

function closeRestartModal(ok = false) {
  const modal = document.getElementById('restart-modal');
  if (!modal) return;
  const resolve = modal._resolveRestart;
  modal._resolveRestart = null;
  modal.classList.remove('open');
  if (resolve) resolve(ok);
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
    if (['clear', 'stop', 'stopall', 'deq'].includes(cmd.command) && (topic !== 'default' || agent)) {
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

function semanticRouteBackspace() {
  if (input.selectionStart !== input.selectionEnd) return false;
  const val = input.value;
  if (!val.startsWith('#')) return false;

  const caret = input.selectionStart;
  const promptSeparator = val.indexOf(' ');
  const routeEnd = promptSeparator >= 0 ? promptSeparator : val.length;
  if (caret <= 1 || caret > routeEnd) return false;

  const route = val.slice(0, routeEnd);
  const prompt = val.slice(routeEnd);
  let nextRoute = null;
  let nextCaret = caret;

  if (caret === routeEnd && /^#\w+@\w+!\d+$/.test(route)) return false;

  if (caret === routeEnd && route.endsWith('!')) {
    nextRoute = route.slice(0, -1);
    nextCaret = nextRoute.length;
  } else {
    const before = route.slice(0, caret);
    const after = route.slice(caret);
    const agentMatch = before.match(/^(#\w+@)\w+$/);

    if (agentMatch && (caret === routeEnd || after.startsWith('!'))) {
      nextRoute = agentMatch[1] + after;
      nextCaret = agentMatch[1].length;
    } else if (before.endsWith('@') && /^#\w+@$/.test(before) && (caret === routeEnd || after.startsWith('!'))) {
      nextRoute = before.slice(0, -1) + after;
      nextCaret = before.length - 1;
    }
  }

  if (nextRoute == null) return false;
  input.value = nextRoute + prompt;
  input.setSelectionRange(nextCaret, nextCaret);
  input.dispatchEvent(new Event('input'));
  return true;
}

function closeEscSurfaces() {
  let closed = false;
  if (searchActive) { clearSearch(); closed = true; }
  if (procStatusPopup?.classList.contains('open')) { procStatusPopup.classList.remove('open'); closed = true; }
  if (pinPanel.classList.contains('open')) { closePinPanel(); closed = true; }
  if (helpPanel.classList.contains('open')) { closeHelp(); closed = true; }
  const msgModal = document.getElementById('msg-modal');
  if (msgModal?.classList.contains('open')) { msgModal.classList.remove('open'); closed = true; }
  const restartModal = document.getElementById('restart-modal');
  if (restartModal?.classList.contains('open')) { closeRestartModal(false); closed = true; }
  if (document.getElementById('memory-modal')?.classList.contains('open')) { closeMemoryEditor(); closed = true; }
  if (document.getElementById('topic-delete-modal')?.classList.contains('open')) { closeTopicDeleteModal(); closed = true; }
  if (document.getElementById('preset-name-modal')?.classList.contains('open')) { _closePresetNameModal(null); closed = true; }
  if (!sessionAdvisoryEl.hidden) { if (_advisoryDismissKey) localStorage.setItem(_advisoryDismissKey, '1'); sessionAdvisoryEl.hidden = true; closed = true; }
  return closed;
}

input.addEventListener('keydown', (e) => {
  if (e.key === 'Backspace' && semanticRouteBackspace()) {
    e.preventDefault();
    return;
  }
  if (e.key === 'ArrowUp' && commandEditRestore !== null) {
    e.preventDefault();
    input.value = commandEditRestore;
    commandEditRestore = null;
    input.setSelectionRange(input.value.length, input.value.length);
    resizeComposer();
    return;
  }
  if (e.key === 'Tab' && !sessionAdvisoryEl.hidden) { e.preventDefault(); stashComposerAndEdit('/clear'); return; }
  if (acOpen) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (acSel <= 0) {
        _acRestoreDraft();
      } else {
        acSel--;
        _acHighlight();
        _acPreview();
      }
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      acSel = Math.min(acSel + 1, acItems.length - 1);
      _acHighlight();
      _acPreview();
      return;
    }
    if (e.key === 'Tab') { e.preventDefault(); _acSelect(acSel >= 0 ? acSel : 0); return; }
    if (e.key === 'Escape') { e.preventDefault(); _acRestoreDraft(); return; }
  }
  if (!acOpen && e.key === 'ArrowUp' && promptHistory.length) {
    const _posBefore = input.selectionStart;
    requestAnimationFrame(() => {
      if (input.selectionStart !== _posBefore) return;
      const items = matchingPromptHistory('');
      if (!items.length) return;
      _acRender(promptHistoryAutocompleteItems(items), 'Recent prompts');
      acSel = 0;
      _acHighlight();
      _acPreview();
    });
    return;
  }
  if (e.key === 'Escape' && closeEscSurfaces()) { e.preventDefault(); return; }
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
  let statusRecoveredFromDb = false;
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
    if (queuePosition !== null) {
      await fetch('/cmd', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'deq', topic, pos: queuePosition }),
      }).catch(() => {});
      pollProcs();
    } else if (msgId) {
      await fetch('/cmd', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'stop_msg', topic, msg_id: msgId }),
      }).catch(() => {});
    }
  });
  thinkingBubble.appendChild(killBtn);

  function setThinkingText(text) {
    if (thinkingFrozen) return;
    statusBuf = text;
    updateThinkingPreview();
  }

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
    if (thinkingLoader.parentNode) thinkingLoader.remove();
    if (statusBuf.trim()) {
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

  function removeThinking() {
    thinkingFrozen = true;
    killBtn.style.display = 'none';
    thinkingBubble.remove();
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
  let queuePosition = null;
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

  function attachMsgId(id) {
    const parsed = parseInt(id, 10);
    if (!Number.isFinite(parsed) || parsed <= 0) return false;
    msgId = parsed;
    bubble.dataset.msgId = String(msgId);
    liveCtxSpan.dataset.msgId = String(msgId);
    thinkingBubble.dataset.msgId = String(msgId);
    reconcilePendingBubble(msgId, thinkingBubble);
    killBtn.style.display = '';
    return true;
  }

  async function recoverMsgIdFromProcesses() {
    if (msgId) return true;
    try {
      const res = await fetch('/processes');
      if (!res.ok) return false;
      const rows = await res.json();
      if (!Array.isArray(rows)) return false;
      const matches = rows.filter(row => {
        if (!row || row.msg_id == null || row.state === 'idle') return false;
        if (row.topic !== topic) return false;
        if (Boolean(row.adhoc) !== Boolean(adhoc)) return false;
        const expectedAgent = resolvedAgent || agent;
        if (expectedAgent && row.agent !== expectedAgent) return false;
        return true;
      });
      if (matches.length !== 1) return false;
      resolvedAgent = matches[0].agent || resolvedAgent;
      return attachMsgId(matches[0].msg_id);
    } catch {
      return false;
    }
  }

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
    const errDisplay = normalizedErrorDisplay(text);
    // Don't wipe streamed content with a generic fallback message
    if (!errDisplay && raw) return;
    contentDiv.innerHTML = `<span class="msg-error">${errDisplay || 'Response interrupted.'}</span>`;
    scrollToBottom();
  }

  function normalizedErrorDisplay(text) {
    return (text || 'Response interrupted.')
      .split('\n')[0]
      .replace(/^CLI exited \d+:\s*/, '')
      .trim();
  }

  function discardInterruptedStatusBubble(errText) {
    if (normalizedErrorDisplay(errText)) return false;
    thinkingFrozen = true;
    killBtn.style.display = 'none';
    thinkingBubble.remove();
    return true;
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

  function parkInterruptedPartial(content, reason = 'Connection interrupted.') {
    if (content) raw = content;
    if (reason && !statusBuf.includes(reason)) {
      statusBuf += (statusBuf ? '\n' : '') + reason;
    }
    updateThinkingPreview();
    thinkingFrozen = true;
    killBtn.style.display = 'none';
    if (thinkingLoader.parentNode) thinkingLoader.remove();
    thinkingBubble.style.display = '';
    thinkingBubble.classList.add('msg-thinking-done');
    scrollToBottom();
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
          const reconnectMsg = 'Connection interrupted — recovering…';
          const ssePart = statusBuf.replace(reconnectMsg, '').trim();
          if (data.status_raw && data.status_raw.trim().length > ssePart.length) {
            statusBuf = data.status_raw;
          }
          removeThinking();
          showStoredResponse(data.content || '');
          bubble.classList.add('history-item');
          resolvedAgent = data.agent || resolvedAgent;
          addPinButton(bubble, msgId, topic, resolvedAgent, data.session_id || null);
          addBookmarkButton(bubble, msgId, topic, resolvedAgent);
          if (!statsEl && data.stats) statsEl = addStats(bubble, data.stats, doneTime);
          if (statsEl) messages.appendChild(statsEl);
          liveSessionTurnCount = parseInt(data.session_turn_count || '0', 10) || liveSessionTurnCount;
          _advisoryTurnCount = liveSessionTurnCount;
          if (data.session_id && !adhoc) _sessionIds[`${topic}@${resolvedAgent || '_'}`] = data.session_id;
          liveCtxSpan.dataset.sessionTurnCount = String(liveSessionTurnCount);
          setCtxLabel(liveCtxSpan, !!data.adhoc, _contextIds.length, _includeTopicMemory, liveSessionTurnCount);
          if (!adhoc) _updateChipTurnCount(topic, resolvedAgent || null, data.session_id || null, liveSessionTurnCount);
          evaluateAdvisory();
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
          if (!String(data.content || '').trim()) return;
          completedFromStatus = true;
          stopStatusFallback();
          if (raw || firstDataReceived) {
            parkInterruptedPartial(data.content || raw);
          } else {
            discardInterruptedStatusBubble(data.content) || freezeThinking();
            showError(data.content);
          }
          controller.abort();
        } else if (data.status === 'pending') {
          if (data.content && !thinkingFrozen) raw = data.content;
          // Recover status from DB once. Use length comparison so the richer source
          // wins: DB status_raw beats a bare "Connection interrupted" message, but
          // SSE-delivered text that is longer than the DB snapshot is kept as-is.
          if (!thinkingFrozen && !statusRecoveredFromDb) {
            const reconnectMsg = 'Connection interrupted — recovering…';
            const ssePart = statusBuf.replace(reconnectMsg, '').trim();
            if (data.status_raw && data.status_raw.trim().length > ssePart.length) {
              statusBuf = data.status_raw + '\n' + reconnectMsg;
              statusRecoveredFromDb = true;
            } else if (data.context && !ssePart) {
              try {
                const dbTools = typeof data.context === 'string' ? JSON.parse(data.context) : data.context;
                if (Array.isArray(dbTools) && dbTools.length > liveToolEvents.length) {
                  statusBuf = dbTools.map(toolLabel).join('\n') + '\n' + reconnectMsg;
                  statusRecoveredFromDb = true;
                }
              } catch {}
            }
          }
          if (!thinkingFrozen) updateThinkingPreview();
        }
      } catch {}
    };
    _activePollImmediate = doPoll;
    doPoll();
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
    if (!msgId) attachMsgId(res.headers.get('X-Squid-Msg-Id'));
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
                queuePosition = null;
                attachMsgId(meta.msg_id);
                setCtxLabel(liveCtxSpan, adhoc);
                bubble.dataset.topic = topic;
                if (resolvedAgent) bubble.dataset.agent = resolvedAgent;
                addPinButton(bubble, msgId, topic, resolvedAgent);
                addBookmarkButton(bubble, msgId, topic, resolvedAgent);
              }
            } catch {}
            eventName = null;

          } else if (eventName === 'queued') {
            try {
              const info = JSON.parse(data);
              queuePosition = info.position;
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
              _advisoryTurnCount = liveSessionTurnCount;
              setCtxLabel(liveCtxSpan, !!stats.adhoc, _contextIds.length, _includeTopicMemory, liveSessionTurnCount);
              liveCtxSpan.dataset.sessionTurnCount = String(liveSessionTurnCount);
              if (stats.session_id) liveCtxSpan.dataset.sessionId = stats.session_id;
              if (!adhoc) _updateChipTurnCount(topic, resolvedAgent || null, stats.session_id || null, liveSessionTurnCount);
              if (stats.cwd) liveCtxSpan.dataset.cwd = stats.cwd;
              if (stats.session_id && !adhoc && stickyChip && !stickyChip.adhoc) {
                localStorage.setItem(`squid_adv_lta_${stickyChip.topic}_${stickyChip.agent||'_'}_${stats.session_id}`, String(Date.now()));
              }
              evaluateAdvisory();
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
            removeThinking();
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
            const errLine = data.trim();
            if (raw || firstDataReceived) {
              parkInterruptedPartial(null, errLine || 'Connection interrupted.');
            } else {
              discardInterruptedStatusBubble(errLine) || freezeThinking();
              showError(errLine);
            }
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
      if (msgId || await recoverMsgIdFromProcesses()) {
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
        if (!completedFromStatus && !msgId && !userAborted) {
          await recoverMsgIdFromProcesses();
        }
        if (!completedFromStatus && msgId && !userAborted) {
          detachedPolling = true;
          statusBuf += (statusBuf ? '\n' : '') + 'Connection interrupted — recovering…';
          updateThinkingPreview();
          startStatusFallback(msgId);
        } else if (!completedFromStatus) {
          if (!userAborted && raw) {
            parkInterruptedPartial(raw, 'Stream ended — response may be incomplete.');
          } else {
            freezeThinking();
          }
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
  if (gitTools.length) return gitTools.filter(t => (t.file_count ?? (t.files || []).length) > 0);
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

function _allChangedNewLines(chunk) {
  const lines = new Set();
  let newLine = null;
  for (const line of (chunk || '').split('\n')) {
    const hunk = line.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hunk) { newLine = parseInt(hunk[1], 10); continue; }
    if (newLine == null) continue;
    if (line.startsWith('+') && !line.startsWith('+++')) { lines.add(newLine++); }
    else if (line.startsWith('-') && !line.startsWith('---')) { /* skip */ }
    else { newLine++; }
  }
  return lines;
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
  let headerDetails = null;
  for (const line of (diff || '').split('\n')) {
    if (line.startsWith('diff --git ') || line.startsWith('index ') ||
        line.startsWith('--- ') || line.startsWith('+++ ')) {
      if (line.startsWith('diff --git ')) {
        headerDetails = document.createElement('details');
        headerDetails.className = 'diff-header';
        const summary = document.createElement('summary');
        summary.className = 'diff-line diff-file diff-header-summary';
        summary.textContent = line;
        headerDetails.appendChild(summary);
        container.appendChild(headerDetails);
      } else if (headerDetails) {
        const el = document.createElement('span');
        el.className = 'diff-line diff-file';
        el.textContent = line;
        headerDetails.appendChild(el);
      }
      continue;
    }
    headerDetails = null;
    const el = document.createElement('span');
    if (line.startsWith('+')) {
      el.className = 'diff-line diff-add';
    } else if (line.startsWith('-')) {
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

function _isSquidWorktreePath(path) {
  return /(^|\/)\.squid\/worktrees\//.test(path || '');
}

function _gitDiffSourceRepo(tool) {
  if (!tool) return '';
  if (tool.source) return tool.source;
  if (tool.repo && !_isSquidWorktreePath(tool.repo)) return tool.repo;
  if (tool.cwd && !_isSquidWorktreePath(tool.cwd)) return tool.cwd;
  return '';
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

    const sourceRepo = _gitDiffSourceRepo(tool);
    if (msgId && sourceRepo) {
      block.dataset.msgId = String(msgId);
      block.dataset.repo = sourceRepo;
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
      const allChangedLines = _allChangedNewLines(chunk);

      const row = document.createElement('div');
      row.className = 'gitdiff-file-row';
      if (msgId && sourceRepo) row.dataset.file = file.path;

      const fileToggle = document.createElement('button');
      fileToggle.className = 'gitdiff-file-toggle';
      fileToggle.title = fullDisplayPath;

      // git's own content-based binary detection wins when we have diff text to
      // check; only fall back to the extension guess when there's no chunk to look at
      const isBinary = chunk ? chunk.includes('Binary files') : !_isTextPath(file.path || '');
      let fileBody = null;
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
        fileBody = document.createElement('div');
        fileBody.className = 'gitdiff-file-body';
        const fileScroll = document.createElement('div');
        fileScroll.className = 'diff-scroll';
        renderUnifiedDiffLines(fileScroll, chunk);
        fileBody.appendChild(fileScroll);
        row.appendChild(fileToggle);
        fileToggle.addEventListener('click', () => row.classList.toggle('gitdiff-file-expanded'));
      } else {
        fileToggle.textContent = `${status} ${displayPath}`;
        fileToggle.classList.add('gitdiff-file-toggle--no-diff');
        row.appendChild(fileToggle);
      }

      const _absPath = file.path
        ? (file.path.startsWith('/') ? file.path : sourceRepo ? sourceRepo + '/' + file.path : null)
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
          openFileViewer(_absPath, firstChangedRange?.line, firstChangedRange?.endLine, null, allChangedLines);
        });
        row.appendChild(openBtn);
      }

      if (fileBody) row.appendChild(fileBody);

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
      const _fb = row.querySelector('.gitdiff-file-body');
      if (_fb) row.insertBefore(btn, _fb); else row.appendChild(btn);
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
  if (sessionTurnCount > 0 && item.session_id && !item.adhoc) {
    const prev = _sessionTurnCounts[item.session_id] || 0;
    if (sessionTurnCount > prev) {
      _sessionTurnCounts[item.session_id] = sessionTurnCount;
      const taKey = `${item.topic || 'default'}@${item.agent || '_'}`;
      if (stickyChip && !stickyChip.adhoc && stickyChip.topic === (item.topic || 'default') &&
          (stickyChip.agent || null) === (item.agent || null) &&
          _sessionIds[taKey] === item.session_id) {
        _renderChipTurnCount(sessionTurnCount);
      }
    }
  }
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
  if (item.timestamp) asstBubble.dataset.ts = item.timestamp;
  if (item.id) addPinButton(asstBubble, item.id, item.topic || 'default', item.agent || null, item.session_id || null);
  if (item.id) addBookmarkButton(asstBubble, item.id, item.topic || 'default', item.agent || null);

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

async function replacePendingWithStoredItem(item, wipBubble) {
  try {
    const res = await fetch(`/chat/${item.id}/status`);
    if (!res.ok || !wipBubble.parentNode) return;
    const data = await res.json();
    if (data.status !== 'done' && data.status !== 'error') return;
    if (data.status === 'error' && !String(data.content || '').trim()) return;
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
  } catch {}
}

function reconnectPendingItem(item, wipBubble) {
  if (!window.EventSource) {
    pollPendingItem(item, wipBubble);
    return;
  }

  const live = wipBubble.querySelector('.thinking-live');
  const loader = live?.querySelector('.loader');
  let raw = '';
  let statusBuf = '';
  let closed = false;

  const updatePreview = () => {
    if (!live) return;
    if (loader?.parentNode) loader.remove();
    const text = (statusBuf ? statusBuf.trimEnd() + (raw ? '\n\n' : '') : '') + raw;
    live.textContent = text.trim();
    live.scrollTop = live.scrollHeight;
  };

  if (item.content && live) {
    if (loader?.parentNode) loader.remove();
    live.textContent = item.content;
  }

  const es = new EventSource(`/chat/${item.id}/events`);
  pendingPollTimers.set(wipBubble, () => {
    closed = true;
    es.close();
  });

  es.onmessage = event => {
    raw += event.data;
    updatePreview();
  };
  es.addEventListener('status', event => {
    statusBuf += (statusBuf ? '\n' : '') + event.data;
    updatePreview();
  });
  es.addEventListener('tool', event => {
    try {
      statusBuf += (statusBuf ? '\n' : '') + toolLabel(JSON.parse(event.data));
      updatePreview();
    } catch {}
  });
  es.addEventListener('done', async () => {
    closed = true;
    es.close();
    pendingPollTimers.delete(wipBubble);
    await replacePendingWithStoredItem(item, wipBubble);
  });
  es.addEventListener('error', async event => {
    if (closed) return;
    closed = true;
    es.close();
    pendingPollTimers.delete(wipBubble);
    if (event.data) {
      if (live) live.innerHTML = `<span class="msg-error">${event.data}</span>`;
      await replacePendingWithStoredItem(item, wipBubble);
      return;
    }
    pollPendingItem(item, wipBubble);
  });
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
      if (data.status === 'done' || (data.status === 'error' && String(data.content || '').trim())) {
        clearInterval(timer);
        await replacePendingWithStoredItem(item, wipBubble);
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

function appendPromptOnlyHistoryItem(item, container) {
  if (!String(item.prompt || '').trim()) return null;
  const bubble = makeUserBubble(
    item.prompt,
    item.topic || 'default',
    item.agent || null,
    item.backend || null,
    !!item.adhoc,
    item.stats?.lookback ?? 0,
  );
  bubble.classList.add('history-item', 'prompt-only-history-item');
  if (item.id) bubble.dataset.msgId = String(item.id);
  bubble.dataset.topic = item.topic || 'default';
  if (item.agent) bubble.dataset.agent = item.agent;
  if (item.session_id) bubble.dataset.sessionId = item.session_id;
  if (item.timestamp) bubble.dataset.ts = item.timestamp;
  if (item.id) {
    const openBtn = document.createElement('button');
    openBtn.type = 'button';
    openBtn.className = 'prompt-only-open-btn';
    openBtn.title = 'Open full response';
    openBtn.setAttribute('aria-label', 'Open full response');
    openBtn.textContent = '↗';
    openBtn.addEventListener('click', e => {
      e.stopPropagation();
      openMsgModal(item.id);
    });
    bubble.appendChild(openBtn);
  }
  if (container) container.appendChild(bubble);
  if (item.timestamp) {
    const tsEl = addTimestamp(bubble, item.timestamp, true);
    if (tsEl) tsEl.classList.add('history-item');
  }
  return bubble;
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

function fmtDate(d) {
  const now = new Date();
  const toDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = toDay(now) - toDay(d);
  if (diff === 0) return 'Today';
  if (diff === 86400000) return 'Yesterday';
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}

function refreshDateDividers() {
  document.querySelectorAll('#messages .date-divider').forEach(el => el.remove());
  const bubbles = [...document.querySelectorAll('#messages .history-item[data-ts]')];
  let lastKey = null;
  for (const el of bubbles) {
    const d = new Date(el.dataset.ts);
    const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    if (key !== lastKey) {
      const div = document.createElement('div');
      div.className = 'date-divider history-item';
      div.textContent = fmtDate(d);
      el.before(div);
      lastKey = key;
    }
  }
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
    fiveHourPctId:   'quota-5h-pct',
    sevenDayArcId:   'quota-7d-arc',
    sevenDayLabelId: 'quota-7d-label',
    sevenDaySuffixId: 'quota-7d-suffix',
    pieC:         2 * Math.PI * 6,
    credsPopupId: 'quota-creds-popup',
    errorTitle:   'Claude usage unavailable · click for credentials',
  },
  codex: {
    endpoint:     '/quota/codex',
    displayId:    'codex-quota-display',
    pieArcId:     'codex-pie-arc',
    labelId:      'codex-quota-label',
    fiveHourPctId:   'codex-5h-pct',
    sevenDayArcId:   'codex-7d-arc',
    sevenDayLabelId: 'codex-7d-label',
    sevenDaySuffixId: 'codex-7d-suffix',
    pieC:         2 * Math.PI * 6,
    credsPopupId: 'codex-creds-popup',
    errorTitle:   'Codex usage unavailable · click for credentials',
  },
  cursor: {
    endpoint:     '/quota/cursor',
    displayId:    'cursor-quota-display',
    pieArcId:     'cursor-pie-arc',
    labelId:      'cursor-quota-label',
    fiveHourPctId:   'cursor-5h-pct',
    pieC:         2 * Math.PI * 6,
    credsPopupId: 'cursor-creds-popup',
    errorTitle:   'Cursor usage unavailable · click for info',
  },
  deepseek: {
    endpoint:     '/quota/deepseek',  // returns remaining pre-paid balance
    displayId:    'deepseek-quota-display',
    pieArcId:     'deepseek-pie-arc',
    fiveHourPctId:   'deepseek-pct',
    labelId:      'deepseek-quota-label',
    pieC:         2 * Math.PI * 6,
    credsPopupId: 'deepseek-max-popup',
    errorTitle:   'DeepSeek balance unavailable',
    formatLabel:  (state) => state.displayText || '—',
  },
  static: {
    displayId:    'static-quota-display',
    pieArcId:     'static-pie-arc',
    labelId:      'static-quota-label',
    pieC:         2 * Math.PI * 6,
  },
};

const quotaSnapshots = {};
const QUOTA_ERROR_RETRY_DELAYS = [3000, 10000, 30000];
// Per-backend runtime state. timer is the label-refresh interval handle.
// activeCount tracks in-flight messages; drives the 30s quota poll interval.
const quotaState = {};

function quotaStateFor(backend) {
  return quotaState[backend] ||= {
    raw: null, pct: null, resetAt: null, displayText: null,
    inFlight: false, timer: null, retryTimer: null, retryAttempt: 0, activeCount: 0,
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

function scheduleGaugeTick(backend) {
  const state = quotaStateFor(backend);
  if (!state.resetAt || state.resetAt <= Date.now()) return;
  state.timer = setTimeout(() => {
    updateGaugeLabel(backend);
    updateSevenDayGauge(backend);
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

  // Update center percentage for gauges that show pct in donut center
  // (codex/claude always; deepseek only when max budget is set — pct is null otherwise)
  if (cfg.fiveHourPctId) {
    const pctEl = document.getElementById(cfg.fiveHourPctId);
    if (pctEl) pctEl.textContent = state.pct != null ? `${state.pct}` : '';
  }

  if (state.displayText != null) {
    label.textContent = state.displayText;
  } else if (cfg.formatLabel) {
    label.textContent = cfg.formatLabel(state);
  } else if (state.pct == null) {
    label.textContent = '—';
  } else {
    // If the backend has 5h/7d dual gauges, percentage goes in donut center,
    // external label shows only countdown. Otherwise show "X% in Xh".
    if (cfg.fiveHourPctId) {
      const timeStr = quotaTimeText(state.resetAt);
      label.textContent = timeStr || '';
    } else {
      const timeStr = quotaTimeText(state.resetAt);
      label.textContent = `${state.pct}%` + (timeStr ? ` in ${timeStr}` : '');
    }
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

function updateSevenDayGauge(backend) {
  const state = quotaStateFor(backend);
  const cfg = quotaConfigFor(backend);
  if (!state.sevenDay || !cfg.sevenDayArcId) return;
  const arc = document.getElementById(cfg.sevenDayArcId);
  const label = document.getElementById(cfg.sevenDayLabelId);
  if (arc && state.sevenDay.pct != null) {
    const filled = (state.sevenDay.pct / 100) * cfg.pieC;
    arc.setAttribute('stroke-dasharray', `${filled} ${cfg.pieC}`);
    arc.setAttribute('stroke', quotaGaugeColor(backend, state.sevenDay.pct));
  } else if (arc) {
    arc.setAttribute('stroke-dasharray', `0 ${cfg.pieC}`);
  }
  if (label) {
    label.textContent = state.sevenDay.pct != null ? `${state.sevenDay.pct}` : '—';
  }
  // Update 7D suffix with days remaining
  const suffix = document.getElementById(cfg.sevenDaySuffixId);
  if (suffix) {
    if (state.sevenDay.resetAt) {
      const diff = state.sevenDay.resetAt - Date.now();
      const days = Math.max(0, diff / (24 * 60 * 60 * 1000));
      suffix.textContent = `${days.toFixed(1)}D`;
    } else {
      suffix.textContent = '7D';
    }
  }
}

function clearQuotaRetry(state) {
  if (state.retryTimer) { clearTimeout(state.retryTimer); state.retryTimer = null; }
  state.retryAttempt = 0;
}

function renderQuotaLoaded(backend, snapshot) {
  const cfg = quotaConfigFor(backend);
  const state = quotaStateFor(backend);
  clearQuotaRetry(state);
  state.raw = snapshot.raw;
  state.pct = snapshot.pct;
  state.resetAt = snapshot.resetAt;
  state.displayText = snapshot.displayText ?? null;
  state.sevenDay = snapshot.sevenDay ?? null;

  if (backend === activeQuotaBackend) {
    const displayEl = document.getElementById(cfg.displayId);
    displayEl.classList.remove('error');
    displayEl.classList.add('loaded');
    displayEl.title = snapshot.title ?? '';
  }
  updateGaugeLabel(backend);
  if (cfg.sevenDayArcId) updateSevenDayGauge(backend);
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
  clearQuotaRetry(state);
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
  if (cfg.sevenDayArcId) {
    const sevenDayArc = document.getElementById(cfg.sevenDayArcId);
    if (sevenDayArc) sevenDayArc.setAttribute('stroke-dasharray', `0 ${cfg.pieC}`);
    const sevenDayLabel = document.getElementById(cfg.sevenDayLabelId);
    if (sevenDayLabel) sevenDayLabel.textContent = '—';
  }
  if (cfg.fiveHourPctId) {
    const fiveHrPct = document.getElementById(cfg.fiveHourPctId);
    if (fiveHrPct) fiveHrPct.textContent = '—';
  }
}

function showTransientQuotaError(backend, text) {
  const state = quotaStateFor(backend);
  if (state.retryTimer) return;
  const delay = QUOTA_ERROR_RETRY_DELAYS[state.retryAttempt++];
  if (delay == null) {
    showQuotaError(backend, text);
    return;
  }
  state.retryTimer = setTimeout(async () => {
    state.retryTimer = null;
    await fetchQuotaForBackend(backend);
  }, delay);
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
      if (res.status === 400) showQuotaError(backend, `${label} auth`);
      else showTransientQuotaError(backend, `${label} error`);
      return null;
    }
    const data = await res.json();
    if (data.status === 'none') {
      clearQuotaRetry(state);
      return null;
    }
    if (!data.status) {
      showQuotaError(backend, `${label} n/a`);
      return null;
    }
    const resetAt = typeof data.reset_at === 'number'
      ? data.reset_at * 1000
      : (data.reset_at ? new Date(data.reset_at).getTime() : null);
    const snapshot = {
      raw: data.raw ?? null,
      pct: data.used_percent != null
        ? Math.max(0, Math.min(100, Math.round(data.used_percent)))
        : (gaugeTypeFor(backend) === 'deepseek' && data.max_budget_pct != null
          ? Math.max(0, Math.min(100, Math.round(data.max_budget_pct)))
          : null),
      resetAt,
      title: data.title || '',
      displayText: gaugeTypeFor(backend) === 'deepseek'
        ? (data.text ?? null)
        : (data.used_percent == null ? (data.text ?? null) : null),
      sevenDay: data.seven_day ? {
        pct: data.seven_day.used_percent == null ? null : Math.round(data.seven_day.used_percent),
        resetAt: data.seven_day.reset_at
          ? (typeof data.seven_day.reset_at === 'number'
            ? data.seven_day.reset_at * 1000
            : new Date(data.seven_day.reset_at).getTime())
          : null,
      } : null,
    };
    renderQuotaLoaded(backend, snapshot);
    return { backend, ...state };
  } catch {
    showTransientQuotaError(backend, `${label} error`);
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
    <span class="quota-7d-group">
      <svg id="quota-7d-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
        <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
        <circle id="${cfg.sevenDayArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('claude')}"
                stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
                transform="rotate(-90 9 9)"/>
        <text id="${cfg.sevenDayLabelId}" x="9" y="9" text-anchor="middle" dominant-baseline="central" font-size="9" fill="#fff">—</text>
      </svg>
      <span id="${cfg.sevenDaySuffixId}" class="quota-7d-suffix">7D</span>
    </span>
    <span style="display:inline-flex;align-items:center">
      <svg id="quota-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
        <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
        <circle id="${cfg.pieArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('claude')}"
                stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
                transform="rotate(-90 9 9)"/>
        <text id="${cfg.fiveHourPctId}" x="9" y="9" text-anchor="middle" dominant-baseline="central" font-size="9" fill="#fff">—</text>
      </svg>
      <span id="${cfg.labelId}"></span>
    </span>`;

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


function initCodexQuota() {
  const cfg = QUOTA_CONFIG.codex;
  codexQuotaDisplay.style.setProperty('--quota-accent', agentThemeColor('codex'));
  codexQuotaDisplay.innerHTML = `
    <span class="quota-7d-group">
      <svg id="codex-7d-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
        <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
        <circle id="${cfg.sevenDayArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('codex')}"
                stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
                transform="rotate(-90 9 9)"/>
        <text id="${cfg.sevenDayLabelId}" x="9" y="9" text-anchor="middle" dominant-baseline="central" font-size="9" fill="#fff">—</text>
      </svg>
      <span id="${cfg.sevenDaySuffixId}" class="quota-7d-suffix">7D</span>
    </span>
    <span style="display:inline-flex;align-items:center">
      <svg id="codex-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
        <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
        <circle id="${cfg.pieArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('codex')}"
                stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
                transform="rotate(-90 9 9)"/>
        <text id="${cfg.fiveHourPctId}" x="9" y="9" text-anchor="middle" dominant-baseline="central" font-size="9" fill="#fff">—</text>
      </svg>
      <span id="${cfg.labelId}"></span>
    </span>`;
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
    <span style="display:inline-flex;align-items:center">
      <svg id="deepseek-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
        <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
        <circle id="${cfg.pieArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('deepseek')}"
                stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
                transform="rotate(-90 9 9)"/>
        <text id="${cfg.fiveHourPctId}" x="9" y="9" text-anchor="middle" dominant-baseline="central" font-size="9" fill="#fff"></text>
      </svg>
      <span id="${cfg.labelId}">—</span>
    </span>`;

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

  saveBtn.addEventListener('click', async () => {
    const val = parseFloat(maxInput.value);
    if (!val || val <= 0) { status.textContent = 'enter a positive amount'; return; }
    const res = await fetch(`/config/deepseek/max-budget`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount: val }),
    });
    if (res.ok) {
      status.textContent = 'saved ✓';
      if (activeQuotaBackend) fetchQuotaForBackend(activeQuotaBackend);
    } else {
      status.textContent = 'save failed';
    }
    setTimeout(() => { status.textContent = ''; }, 2000);
  });

  clearBtn?.addEventListener('click', async () => {
    const res = await fetch(`/config/deepseek/max-budget`, { method: 'DELETE' });
    maxInput.value = '';
    if (res.ok) {
      status.textContent = 'cleared';
      if (activeQuotaBackend) fetchQuotaForBackend(activeQuotaBackend);
    } else {
      status.textContent = 'clear failed';
    }
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
        autoStatus.textContent = `saved ✓ (org: ${(data.claude_org_id || '').slice(0, 8)}…)`;
        fetchQuota();
      } else {
        autoStatus.textContent = data.error || 'failed';
      }
    } catch { autoStatus.textContent = 'error'; }
    autoBtn.disabled = false;
    setTimeout(() => { autoStatus.textContent = ''; }, 5000);
  });

  saveBtn.addEventListener('click', async () => {
    const claude_org_id      = orgInput.value.trim();
    const claude_session_key = keyInput.value.trim();
    if (!claude_org_id || !claude_session_key) { status.textContent = 'both fields required'; return; }
    try {
      const res = await fetch('/config/creds', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ claude_org_id, claude_session_key }),
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
    <span style="display:inline-flex;align-items:center">
      <svg id="cursor-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
        <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
        <circle id="${cfg.pieArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('cursor')}"
                stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
                transform="rotate(-90 9 9)"/>
        <text id="${cfg.fiveHourPctId}" x="9" y="9" text-anchor="middle" dominant-baseline="central" font-size="9" fill="#fff">—</text>
      </svg>
      <span id="${cfg.labelId}"></span>
    </span>`;

  const credsPopup = document.getElementById(cfg.credsPopupId);
  cursorQuotaDisplay.addEventListener('click', () => credsPopup.classList.toggle('open'));
  document.addEventListener('click', (e) => {
    if (!cursorQuotaDisplay.contains(e.target) && !credsPopup.contains(e.target))
      credsPopup.classList.remove('open');
  });
  fetchCursorQuota();
}

// ── Static (text-only) gauge ───────────────────────────────────────────────────


// ── usage stats panel ─────────────────────────────────────────────────────────

let statsPeriod = 'hourly';
let statsBreakdown = '';
let statsFilters = { days: 7, agents: [], topics: [], adhoc: 'all' };
let statsChartY1 = 'turns';
let statsChartY2 = '';
let statsChartAggY1 = 'sum';
let statsChartAggY2 = 'sum';
let statsChartInstance = null;
let _lastStatsRows = null;
let _statsFiltersLoaded = false;
let _statsPage = 0;
const _STATS_PAGE_SIZE = 10;
const _statsMeasures = new Set(['sessions', 'turns', 'tokens_in', 'tokens_out']);
const _STATS_BREAKDOWN_SERIES_BUDGET = 4;
const _STATS_BREAKDOWN_VISIBLE_SERIES_LIMIT = 8;
let _statsPresets = [];
let _activeStatsPresetId = null;
let _statsPresetsLoaded = false;
let _statsBreakdownColumnSort = { mode: 'name', dir: 'asc' };
const STATS_SERIES_COLORS = [
  'rgba(100,160,255,1)',
  'rgba(80,200,120,1)',
  'rgba(255,160,80,1)',
  'rgba(200,100,200,1)',
  'rgba(200,200,60,1)',
  'rgba(120,200,220,1)',
];

function _rerenderStats() {
  if (!_lastStatsRows) return;
  if (statsBreakdown) { renderAgentBreakdownStats(_lastStatsRows); _renderBreakdownChart(_lastStatsRows); }
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

function _setStatsTable(html) {
  statsContent.innerHTML = `<div class="stats-table-scroll">${html}</div>`;
}

const CHART_METRICS = {
  turns:      { label: 'Turns',      fn: r => (r.total_turns || 0),                                                     color: 'rgba(100,160,255,1)',  fill: 'rgba(100,160,255,0.08)' },
  cost:       { label: 'Cost ($)',   fn: r => (r.cost_usd || 0),                                                        color: 'rgba(255,160,80,1)',   fill: 'rgba(255,160,80,0.08)'  },
  tokens_in:  { label: 'Tokens In', fn: r => { const raw = r.input_tokens||0, cr = r.cache_read_tokens||0; return (cr>0&&raw<cr)?raw+cr:raw; }, color: 'rgba(80,200,120,1)',   fill: 'rgba(80,200,120,0.08)'  },
  tokens_out: { label: 'Tokens Out',fn: r => (r.output_tokens || 0),                                                   color: 'rgba(200,100,200,1)',  fill: 'rgba(200,100,200,0.08)' },
  sessions:   { label: 'Sessions',  fn: r => (r.sessions || 0),                                                        color: 'rgba(200,200,60,1)',   fill: 'rgba(200,200,60,0.08)'  },
  quota:      { label: 'Quota',     fn: r => (r.quota_delta || 0),                                                     color: 'rgba(120,200,220,1)',  fill: 'rgba(120,200,220,0.08)' },
};

const STATS_AGG_LABELS = { sum: 'SUM', avg: 'AVG', min: 'MIN', max: 'MAX', p50: 'P50', p75: 'P75', p95: 'P95' };
const STATS_METRIC_AGGS = {
  turns: ['sum'],
  sessions: ['sum'],
  cost: ['sum', 'avg', 'min', 'max', 'p50', 'p75', 'p95'],
  tokens_in: ['sum', 'avg', 'min', 'max', 'p50', 'p75', 'p95'],
  tokens_out: ['sum', 'avg', 'min', 'max', 'p50', 'p75', 'p95'],
  quota: ['sum', 'avg', 'min', 'max', 'p50', 'p75', 'p95'],
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

function _statsMetricValue(row, metric) {
  if (metric === 'turns') return row.total_turns || 0;
  if (metric === 'cost') return row.cost_usd || 0;
  if (metric === 'tokens_in') return _statsInputTokens(row);
  if (metric === 'tokens_out') return row.output_tokens || 0;
  if (metric === 'sessions') return row.sessions || 0;
  if (metric === 'quota') return row.quota_delta || 0;
  return row.total_turns || 0;
}

function _formatStatsMetricValue(value, metric) {
  if (metric === 'cost') return _formatCost(value);
  if (metric === 'quota') return _formatQuotaDelta(value);
  return fmtNum(value || 0);
}

function _defaultStatsAgg(metric) {
  return (STATS_METRIC_AGGS[metric] || ['sum'])[0];
}

function _normalizeStatsAgg(metric, agg) {
  const allowed = STATS_METRIC_AGGS[metric] || ['sum'];
  return allowed.includes(agg) ? agg : _defaultStatsAgg(metric);
}

function _statsMetricHasVariableAgg(metric) {
  return (STATS_METRIC_AGGS[metric] || ['sum']).length > 1;
}

function _syncStatsAggSelect(select, metric, agg) {
  if (!select) return;
  const allowed = STATS_METRIC_AGGS[metric] || ['sum'];
  select.innerHTML = allowed.map(key => `<option value="${key}">${STATS_AGG_LABELS[key]}</option>`).join('');
  select.value = _normalizeStatsAgg(metric, agg);
}

function _statsChartAggField(metric, agg) {
  return `chart_${metric}_${agg}`;
}

function _statsChartSeriesValue(row, metric, agg) {
  if (agg === 'sum') return _statsMetricValue(row, metric);
  const value = row[_statsChartAggField(metric, agg)];
  return value == null ? 0 : value;
}

function _statsChartSeriesLabel(metric, agg) {
  const m = CHART_METRICS[metric] || CHART_METRICS.turns;
  return `${STATS_AGG_LABELS[agg] || agg.toUpperCase()} ${m.label}`;
}

function _statsCompareColor(primaryColor) {
  return STATS_SERIES_COLORS.find(color => color !== primaryColor) || 'rgba(255,160,80,1)';
}

function _statsMeasureState(metric, agg) {
  return { metric, agg: _normalizeStatsAgg(metric, agg) };
}

function _parseStatsMeasureState(value, fallbackMetric = 'turns') {
  if (typeof value === 'string') return { metric: value || fallbackMetric, agg: _defaultStatsAgg(value || fallbackMetric) };
  const metric = value?.metric || fallbackMetric;
  return { metric, agg: _normalizeStatsAgg(metric, value?.agg || _defaultStatsAgg(metric)) };
}

function _syncStatsChartAggControls() {
  statsChartAggY1 = _normalizeStatsAgg(statsChartY1, statsChartAggY1);
  statsChartAggY2 = _normalizeStatsAgg(statsChartY2, statsChartAggY2);
  const y1AggSel = document.getElementById('sc-y1-agg');
  _syncStatsAggSelect(y1AggSel, statsChartY1, statsChartAggY1);
  if (y1AggSel) y1AggSel.hidden = !_statsMetricHasVariableAgg(statsChartY1);
  const y2AggSel = document.getElementById('sc-y2-agg');
  _syncStatsAggSelect(y2AggSel, statsChartY2 || 'turns', statsChartAggY2);
  if (y2AggSel) y2AggSel.hidden = !statsChartY2 || document.getElementById('sc-y2')?.hidden || !_statsMetricHasVariableAgg(statsChartY2);
}

const STATS_ICON_LEFT = '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M10 3.5 5.5 8l4.5 4.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const STATS_ICON_RIGHT = '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m6 3.5 4.5 4.5L6 12.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function _statsBreakdownAxisLabel(label, sortMode) {
  const ariaLabel = sortMode === 'total' ? 'Sort breakdown columns by total' : 'Sort breakdown columns by name';
  const leftActive = _statsBreakdownColumnSort.mode === sortMode && _statsBreakdownColumnSort.dir === 'asc';
  const rightActive = _statsBreakdownColumnSort.mode === sortMode && _statsBreakdownColumnSort.dir === 'desc';
  return `<span class="stats-breakdown-axis-label">
    <span>${escapeHtml(label)}</span>
    <span class="stats-breakdown-sort">
      <button class="stats-breakdown-sort-btn${leftActive ? ' active' : ''}" type="button" data-stats-column-sort="${sortMode}" data-stats-column-dir="asc" title="${escapeHtml(ariaLabel)}" aria-label="${escapeHtml(ariaLabel)}">${STATS_ICON_LEFT}</button>
      <button class="stats-breakdown-sort-btn${rightActive ? ' active' : ''}" type="button" data-stats-column-sort="${sortMode}" data-stats-column-dir="desc" title="${escapeHtml(ariaLabel)}" aria-label="${escapeHtml(ariaLabel)}">${STATS_ICON_RIGHT}</button>
    </span>
  </span>`;
}

function _bindStatsBreakdownSort() {
  statsContent.querySelectorAll('[data-stats-column-sort]').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      _statsBreakdownColumnSort = {
        mode: btn.dataset.statsColumnSort || 'name',
        dir: btn.dataset.statsColumnDir || 'asc',
      };
      _rerenderStats();
    });
  });
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

function _agentLabel(row) {
  return row.agent || row.agent_key || 'unknown';
}

function _agentKey(row) {
  return row.agent_key || row.agent || 'unknown';
}

function _agentBaseKey(key) {
  return String(key || 'unknown').replace(/!$/, '');
}

function _sessionTypesForFilter() {
  if (statsFilters.adhoc === 'session') return ['session'];
  if (statsFilters.adhoc === 'adhoc') return ['adhoc'];
  return ['session', 'adhoc'];
}

function _statsSeriesKey(row) {
  const agent = _agentBaseKey(_agentKey(row));
  const sessionType = row.session_type || (String(_agentKey(row)).endsWith('!') ? 'adhoc' : 'session');
  const agentLabel = sessionType === 'adhoc' && statsBreakdown.endsWith('_session') ? `${agent}!` : agent;
  if (statsBreakdown === 'topic_agent' || statsBreakdown === 'topic_agent_session') {
    return `${row.topic || 'unknown'}\u0000${agentLabel}`;
  }
  return agentLabel;
}

function _statsAdhocSuffix() {
  if (statsFilters.adhoc === 'adhoc') return '!';
  if (statsFilters.adhoc === 'all') return '+';
  return '';
}

function _statsSeriesLabel(key) {
  const raw = String(key);
  const [topic, agent] = raw.split('\u0000');
  if (agent != null) {
    if (statsBreakdown === 'topic_agent') return `#${topic}@${_agentBaseKey(agent)}${_statsAdhocSuffix()}`;
    return `#${topic}@${agent}`;
  }
  if (statsBreakdown === 'agent') return `@${raw}${_statsAdhocSuffix()}`;
  if (statsBreakdown === 'agent_session') return `@${raw}`;
  if (statsBreakdown === 'topic') return `#${raw}`;
  return raw;
}

function _compareStatsSeriesByName(a, b, labels) {
  const labelA = labels.get(a) || a;
  const labelB = labels.get(b) || b;
  const baseA = _agentBaseKey(labelA);
  const baseB = _agentBaseKey(labelB);
  const baseCompare = baseA.localeCompare(baseB) || _agentBaseKey(a).localeCompare(_agentBaseKey(b));
  if (baseCompare) return baseCompare;
  return (a.endsWith('!') ? 1 : 0) - (b.endsWith('!') ? 1 : 0) || labelA.localeCompare(labelB) || a.localeCompare(b);
}

function _topDimensionValues(rows, dimension, limit, allowed = null) {
  const totals = new Map();
  const labels = new Map();
  for (const row of rows) {
    const key = dimension === 'topic' ? (row.topic || 'unknown') : _agentBaseKey(_agentKey(row));
    if (allowed && !allowed.includes(key)) continue;
    totals.set(key, (totals.get(key) || 0) + (row.total_turns || 0));
    if (!labels.has(key)) labels.set(key, key);
  }
  return [...totals.entries()]
    .sort((a, b) => b[1] - a[1] || (labels.get(a[0]) || '').localeCompare(labels.get(b[0]) || ''))
    .slice(0, limit)
    .map(([key]) => key);
}

function _breakdownSelection(rows) {
  const labels = new Map();
  const availableSeries = new Set();
  for (const row of rows) {
    const key = _statsSeriesKey(row);
    availableSeries.add(key);
    if (!labels.has(key)) labels.set(key, _statsSeriesLabel(key));
  }
  const sessionTypes = _sessionTypesForFilter();
  const sessionCount = statsBreakdown.endsWith('_session') ? sessionTypes.length : 1;
  const topicLimit = statsBreakdown.startsWith('topic_') ? Math.max(1, Math.floor(_STATS_BREAKDOWN_SERIES_BUDGET / (2 * sessionCount))) : 0;
  const agentLimit = statsBreakdown === 'agent' ? _STATS_BREAKDOWN_SERIES_BUDGET : Math.max(1, Math.floor(_STATS_BREAKDOWN_SERIES_BUDGET / (Math.max(1, topicLimit) * sessionCount)));
  const selectedTopics = statsFilters.topics.length ? statsFilters.topics : _topDimensionValues(rows, 'topic', topicLimit || _STATS_BREAKDOWN_SERIES_BUDGET);
  const selectedAgents = statsFilters.agents.length ? statsFilters.agents : _topDimensionValues(rows, 'agent', agentLimit, null);

  for (const topic of selectedTopics) {
    for (const agent of selectedAgents) {
      if (statsBreakdown === 'topic_agent_session') {
        for (const sessionType of sessionTypes) {
          const key = `${topic}\u0000${sessionType === 'adhoc' ? `${agent}!` : agent}`;
          labels.set(key, _statsSeriesLabel(key));
        }
      } else if (statsBreakdown === 'topic_agent') {
        const key = `${topic}\u0000${agent}`;
        labels.set(key, _statsSeriesLabel(key));
      }
    }
  }
  for (const agent of selectedAgents) {
    if (statsBreakdown === 'agent_session') {
      for (const sessionType of sessionTypes) {
        const key = sessionType === 'adhoc' ? `${agent}!` : agent;
        labels.set(key, _statsSeriesLabel(key));
      }
    } else if (statsBreakdown === 'agent') {
      labels.set(agent, _statsSeriesLabel(agent));
    }
  }

  let selected;
  if (statsBreakdown === 'topic_agent_session') {
    selected = [];
    for (const topic of selectedTopics) {
      for (const agent of selectedAgents) {
        for (const sessionType of sessionTypes) selected.push(`${topic}\u0000${sessionType === 'adhoc' ? `${agent}!` : agent}`);
      }
    }
  } else if (statsBreakdown === 'topic_agent') {
    selected = [];
    for (const topic of selectedTopics) {
      for (const agent of selectedAgents) selected.push(`${topic}\u0000${agent}`);
    }
  } else if (statsBreakdown === 'agent_session') {
    selected = [];
    for (const agent of selectedAgents) {
      for (const sessionType of sessionTypes) selected.push(sessionType === 'adhoc' ? `${agent}!` : agent);
    }
  } else if (statsFilters.adhoc === 'all') {
    selected = [];
    for (const agent of selectedAgents) {
      for (const key of [agent, `${agent}!`]) {
        if (availableSeries.has(key)) selected.push(key);
      }
    }
  } else if (statsFilters.adhoc === 'adhoc') {
    selected = [];
    for (const agent of selectedAgents) {
      const adhocKey = `${agent}!`;
      if (availableSeries.has(adhocKey)) selected.push(adhocKey);
    }
    if (!selected.length) selected = selectedAgents;
  } else {
    selected = selectedAgents;
  }
  selected = selected.slice().sort((a, b) => _compareStatsSeriesByName(a, b, labels));
  return { selected, selectedAgents, selectedTopics, labels };
}

function _breakdownPivot(rows) {
  const { selected, selectedAgents, selectedTopics, labels } = _breakdownSelection(rows);
  const allSelected = selected.slice();
  const canSumTotals = statsChartAggY1 === 'sum';
  const periods = [...new Set(rows.map(r => r.period))].sort().reverse();
  const periodRows = periods.map(period => {
    const values = Object.fromEntries(allSelected.map(key => [key, 0]));
    let misc = canSumTotals ? 0 : null;
    let total = canSumTotals ? 0 : null;
    for (const row of rows) {
      if (row.period !== period) continue;
      const value = _statsChartSeriesValue(row, statsChartY1, statsChartAggY1);
      if (canSumTotals) total += value;
      const key = _statsSeriesKey(row);
      const selectedAgent = selectedAgents.includes(_agentBaseKey(_agentKey(row)));
      const selectedTopic = !statsBreakdown.startsWith('topic_') || selectedTopics.includes(row.topic || 'unknown');
      if (selectedAgent && selectedTopic && allSelected.includes(key)) values[key] += value;
      else if (canSumTotals) misc += value;
    }
    return { period, values, misc, total };
  });
  const seriesTotals = new Map(allSelected.map(key => [
    key,
    periodRows.reduce((sum, row) => sum + (row.values[key] || 0), 0),
  ]));
  const visibleSelected = allSelected
    .slice()
    .sort((a, b) => (seriesTotals.get(b) || 0) - (seriesTotals.get(a) || 0) || _compareStatsSeriesByName(a, b, labels))
    .slice(0, _STATS_BREAKDOWN_VISIBLE_SERIES_LIMIT);
  const visibleSet = new Set(visibleSelected);
  const overflowSelected = allSelected.filter(key => !visibleSet.has(key));
  if (overflowSelected.length) {
    for (const row of periodRows) {
      if (!canSumTotals) continue;
      for (const key of overflowSelected) row.misc += row.values[key] || 0;
    }
  }
  if (_statsBreakdownColumnSort.mode === 'total') {
    visibleSelected.sort((a, b) => {
      const totalA = seriesTotals.get(a) || 0;
      const totalB = seriesTotals.get(b) || 0;
      const totalCompare = _statsBreakdownColumnSort.dir === 'asc' ? totalA - totalB : totalB - totalA;
      return totalCompare || _compareStatsSeriesByName(a, b, labels);
    });
  } else if (_statsBreakdownColumnSort.dir === 'asc') {
    visibleSelected.sort((a, b) => _compareStatsSeriesByName(a, b, labels));
  } else if (_statsBreakdownColumnSort.dir === 'desc') {
    visibleSelected.sort((a, b) => _compareStatsSeriesByName(b, a, labels));
  }
  return { selected: visibleSelected, labels, periodRows, overflowCount: overflowSelected.length, canSumTotals };
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

function _statsMultiLabel(values, allLabel, singular, prefix) {
  if (!values.length) return allLabel;
  if (values.length === 1) return `${prefix}${values[0]}`;
  return `${values.length} ${singular}s`;
}

function _updateStatsFilterLabels() {
  const topicToggle = document.getElementById('sf-topic-toggle');
  const agentToggle = document.getElementById('sf-agent-toggle');
  if (topicToggle) {
    topicToggle.textContent = _statsMultiLabel(statsFilters.topics, 'All Topics', 'Topic', '#');
    topicToggle.classList.toggle('active', statsFilters.topics.length > 0);
  }
  if (agentToggle) {
    agentToggle.textContent = _statsMultiLabel(statsFilters.agents, 'All Agents', 'Agent', '@');
    agentToggle.classList.toggle('active', statsFilters.agents.length > 0);
  }
}

function _syncStatsAgentMenuSelection() {
  document.querySelectorAll('#sf-agent-menu input[type="checkbox"]').forEach(input => {
    input.checked = statsFilters.agents.includes(input.value);
  });
  _updateStatsFilterLabels();
}

function _syncStatsTopicMenuSelection() {
  document.querySelectorAll('#sf-topic-menu input[type="checkbox"]').forEach(input => {
    input.checked = statsFilters.topics.includes(input.value);
  });
  _updateStatsFilterLabels();
}

function _resetStatsDimensionFilters() {
  statsFilters.topics = [];
  statsFilters.agents = [];
  statsFilters.adhoc = 'all';
  document.getElementById('sf-adhoc').value = 'all';
  _syncStatsTopicMenuSelection();
  _syncStatsAgentMenuSelection();
}

function _updateStatsBreakdownUi() {
  const active = !!statsBreakdown;
  document.getElementById('stats-chart-controls')?.classList.toggle('breakdown-active', active);
  const measures = document.getElementById('sf-measures');
  if (measures) measures.classList.toggle('disabled', active);
  const measuresToggle = document.getElementById('sf-measures-toggle');
  if (measuresToggle) {
    measuresToggle.disabled = active;
    measuresToggle.setAttribute('aria-disabled', active ? 'true' : 'false');
    if (active) measuresToggle.setAttribute('aria-expanded', 'false');
  }
  const measuresMenu = document.getElementById('sf-measures-menu');
  if (active && measuresMenu) measuresMenu.hidden = true;
}

function _renderStatsMultiMenu(menu, values, selected, prefix) {
  menu.innerHTML = values.map(value => {
    const safe = escapeHtml(value);
    return `<label><input type="checkbox" value="${safe}"${selected.includes(value) ? ' checked' : ''}> ${prefix}${safe}</label>`;
  }).join('') || '<div class="empty">No options.</div>';
}

function _normalizeStatsBreakdownSort(sort = {}) {
  const mode = sort.mode === 'total' ? 'total' : 'name';
  const dir = sort.dir === 'desc' ? 'desc' : 'asc';
  return { mode, dir };
}

function _statsQueryParams({ includeTz = false } = {}) {
  const params = new URLSearchParams();
  params.set('period', statsPeriod);
  if (statsBreakdown) {
    params.set('breakdown', statsBreakdown);
    params.set('breakdown_sort', _statsBreakdownColumnSort.mode);
    params.set('breakdown_sort_dir', _statsBreakdownColumnSort.dir);
  }
  params.set('days', statsFilters.days);
  if (includeTz) params.set('tz_offset_minutes', new Date().getTimezoneOffset());
  if (statsFilters.agents.length) params.set('agent', statsFilters.agents.join(','));
  if (statsFilters.topics.length) params.set('topic', statsFilters.topics.join(','));
  if (statsFilters.adhoc !== 'all') params.set('adhoc', statsFilters.adhoc);
  params.set('chart_metric', statsChartY1);
  params.set('chart_agg', statsChartAggY1);
  if (statsChartY2) {
    params.set('chart2_metric', statsChartY2);
    params.set('chart2_agg', statsChartAggY2);
  }
  return params;
}

function _statsState() {
  return {
    version: 1,
    time: { period: statsPeriod, days: statsFilters.days },
    dimensions: {
      topic: { mode: statsFilters.topics.length ? 'selected' : 'auto_top', values: [...statsFilters.topics] },
      agent: { mode: statsFilters.agents.length ? 'selected' : 'auto_top', values: [...statsFilters.agents] },
      session_type: { mode: statsFilters.adhoc === 'all' ? 'all' : 'selected', values: statsFilters.adhoc === 'all' ? [] : [statsFilters.adhoc] },
    },
    breakdown: { key: statsBreakdown, sort: { ..._statsBreakdownColumnSort } },
    measure: {
      primary: _statsMeasureState(statsChartY1, statsChartAggY1),
      secondary: statsChartY2 ? _statsMeasureState(statsChartY2, statsChartAggY2) : null,
      visible: [..._statsMeasures],
    },
  };
}

function _overallStatsState() {
  return {
    version: 1,
    time: { period: 'hourly', days: 7 },
    dimensions: {
      topic: { mode: 'auto_top', values: [] },
      agent: { mode: 'auto_top', values: [] },
      session_type: { mode: 'all', values: [] },
    },
    breakdown: { key: '', sort: { mode: 'name', dir: 'asc' } },
    measure: { primary: { metric: 'turns', agg: 'sum' }, secondary: null, visible: ['sessions', 'turns', 'tokens_in', 'tokens_out'] },
  };
}

function _markStatsPresetDirty() {
  _renderStatsPresetControls();
}

function _applyStatsState(state) {
  statsPeriod = state?.time?.period || 'hourly';
  statsFilters.days = Number(state?.time?.days ?? 7);
  statsBreakdown = state?.breakdown?.key || '';
  _statsBreakdownColumnSort = _normalizeStatsBreakdownSort(state?.breakdown?.sort);
  const dims = state?.dimensions || {};
  statsFilters.topics = dims.topic?.mode === 'selected' ? [...(dims.topic.values || [])] : [];
  statsFilters.agents = dims.agent?.mode === 'selected' ? [...(dims.agent.values || [])] : [];
  const sessionValues = dims.session_type?.values || [];
  statsFilters.adhoc = dims.session_type?.mode === 'all' ? 'all' : (sessionValues[0] || 'all');
  const primaryMeasure = _parseStatsMeasureState(state?.measure?.primary, 'turns');
  const secondaryMeasure = state?.measure?.secondary ? _parseStatsMeasureState(state.measure.secondary, '') : { metric: '', agg: 'sum' };
  statsChartY1 = primaryMeasure.metric || 'turns';
  statsChartAggY1 = primaryMeasure.agg;
  statsChartY2 = secondaryMeasure.metric || '';
  statsChartAggY2 = secondaryMeasure.agg || 'sum';
  _statsMeasures.clear();
  for (const key of state?.measure?.visible || ['sessions', 'turns', 'tokens_in', 'tokens_out']) _statsMeasures.add(key);
  document.getElementById('sf-period').value = statsPeriod;
  document.getElementById('sf-days').value = String(statsFilters.days);
  document.getElementById('sf-breakdown').value = statsBreakdown;
  document.getElementById('sf-adhoc').value = statsFilters.adhoc;
  document.getElementById('sc-y1').value = statsChartY1;
  const y2Sel = document.getElementById('sc-y2');
  y2Sel.value = statsChartY2;
  y2Sel.hidden = !statsChartY2;
  document.getElementById('sc-y2-agg').hidden = !statsChartY2;
  document.getElementById('sc-compare-btn').textContent = statsChartY2 ? '− Y2' : '+ Y2';
  _syncStatsChartAggControls();
  document.querySelectorAll('#sf-measures-menu input[type="checkbox"]').forEach(input => {
    input.checked = _statsMeasures.has(input.value);
  });
  _syncStatsTopicMenuSelection();
  _syncStatsAgentMenuSelection();
  _updateStatsMeasureLabel();
  _updateStatsBreakdownUi();
}

function _renderStatsPresetControls() {
  const select = document.getElementById('stats-preset-select');
  if (!select) return;
  const options = ['<option value="__overall">Overall View</option>'];
  options.push(..._statsPresets.map(preset => {
    const label = `${preset.name}${preset.is_default ? ' (default)' : ''}`;
    return `<option value="${preset.id}">${escapeHtml(label)}</option>`;
  }));
  select.innerHTML = options.join('');
  select.value = _activeStatsPresetId ? String(_activeStatsPresetId) : '__overall';
  const hasActivePreset = !!_activeStatsPresetId;
  const hasDefaultPreset = _statsPresets.some(p => p.is_default);
  document.getElementById('stats-preset-update').disabled = !hasActivePreset;
  document.getElementById('stats-preset-default').disabled = !hasActivePreset && !hasDefaultPreset;
  document.getElementById('stats-preset-delete').disabled = !hasActivePreset;
}

function _setStatsPresetStatus(text) {
  const status = document.getElementById('stats-preset-status');
  if (status) status.textContent = text || '';
}

async function _loadStatsPresets({ applyDefault = false } = {}) {
  try {
    _statsPresets = await fetch('/stats/filter-presets').then(r => r.json());
    const def = _statsPresets.find(preset => preset.is_default);
    if (applyDefault && def && !_activeStatsPresetId) {
      _activeStatsPresetId = def.id;
      _applyStatsState(def.state);
    }
    _renderStatsPresetControls();
  } catch {
    _statsPresets = [];
  }
}

let _presetNameResolve = null;

function _openPresetNameModal(defaultName) {
  return new Promise(resolve => {
    _presetNameResolve = resolve;
    const input = document.getElementById('preset-name-input');
    input.value = defaultName || '';
    document.getElementById('preset-name-confirm').disabled = !input.value.trim();
    document.getElementById('preset-name-modal').classList.add('open');
    input.focus();
    input.select();
  });
}

function _closePresetNameModal(name = null) {
  document.getElementById('preset-name-modal').classList.remove('open');
  const resolve = _presetNameResolve;
  _presetNameResolve = null;
  if (resolve) resolve(name);
}

async function _saveStatsPreset({ update = false, makeDefault = false } = {}) {
  if (makeDefault && !_activeStatsPresetId) {
    const currentDefault = _statsPresets.find(p => p.is_default);
    if (!currentDefault) return;
    const res = await fetch(`/stats/filter-presets/${currentDefault.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_default: false }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      _setStatsPresetStatus(err.error || 'save failed');
      return;
    }
    await _loadStatsPresets();
    _setStatsPresetStatus('default cleared');
    return;
  }
  const active = _statsPresets.find(preset => preset.id === _activeStatsPresetId);
  let name;
  if (update || makeDefault) {
    name = active?.name;
  } else {
    name = await _openPresetNameModal(active?.name || '');
    if (!name) return;
  }
  const url = update || makeDefault ? `/stats/filter-presets/${_activeStatsPresetId}` : '/stats/filter-presets';
  const method = update || makeDefault ? 'PUT' : 'POST';
  const body = makeDefault ? { is_default: true } : { name: name.trim(), state: _statsState() };
  const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    _setStatsPresetStatus(err.error || 'save failed');
    return;
  }
  const preset = await res.json();
  await _loadStatsPresets();
  _activeStatsPresetId = preset.id;
  _renderStatsPresetControls();
  _setStatsPresetStatus(makeDefault ? 'default set' : update ? 'updated' : 'saved');
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
    label: _statsChartSeriesLabel(statsChartY1, statsChartAggY1),
    data: chronological.map(r => _statsChartSeriesValue(r, statsChartY1, statsChartAggY1)),
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
      const sharedAxis = statsChartY2 === statsChartY1;
      const y2Color = sharedAxis ? _statsCompareColor(m1.color) : m2.color;
      datasets.push({
        label: _statsChartSeriesLabel(statsChartY2, statsChartAggY2),
        data: chronological.map(r => _statsChartSeriesValue(r, statsChartY2, statsChartAggY2)),
        borderColor: y2Color, backgroundColor: 'transparent',
        yAxisID: sharedAxis ? 'y1' : 'y2', tension: 0.3, fill: false,
        pointRadius: labels.length > 60 ? 1 : labels.length > 20 ? 2 : 4,
        pointHoverRadius: 5,
      });
      if (!sharedAxis) {
        scales.y2 = { type: 'linear', position: 'right', ticks: { color: '#555', font: { size: 10 }, callback: fmtAxisNum }, grid: { display: false } };
      }
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

function _renderBreakdownChart(rows) {
  if (!rows || !rows.length || typeof Chart === 'undefined') { _destroyChart(); return; }
  const pivot = _breakdownPivot(rows);
  const chronological = [...pivot.periodRows].reverse();
  const labels = chronological.map(r => r.period);
  const datasets = pivot.selected.map((key, i) => {
    const color = STATS_SERIES_COLORS[i % STATS_SERIES_COLORS.length];
    return {
      label: pivot.labels.get(key) || key,
      data: chronological.map(r => r.values[key] || 0),
      borderColor: color,
      backgroundColor: color.replace(',1)', ',0.08)'),
      yAxisID: 'y1',
      tension: 0.3,
      fill: false,
      pointRadius: labels.length > 60 ? 1 : labels.length > 20 ? 2 : 4,
      pointHoverRadius: 5,
    };
  });
  if (chronological.some(r => r.misc > 0)) {
    datasets.push({
      label: 'Misc',
      data: chronological.map(r => r.misc || 0),
      borderColor: 'rgba(140,140,150,1)',
      backgroundColor: 'rgba(140,140,150,0.08)',
      yAxisID: 'y1',
      tension: 0.3,
      fill: false,
      pointRadius: labels.length > 60 ? 1 : labels.length > 20 ? 2 : 4,
      pointHoverRadius: 5,
    });
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
      scales: {
        x: { ticks: { color: '#555', font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, grid: { color: '#1a1a24' } },
        y1: { type: 'linear', position: 'left', ticks: { color: '#555', font: { size: 10 }, callback: fmtAxisNum }, grid: { color: '#1a1a24' } },
      },
    },
  });
}

// ── process status dot + popup ────────────────────────────────────────────────

const procStatusBtn   = document.getElementById('proc-status');
const procStatusPopup = document.getElementById('proc-status-popup');
let procPollInterval  = null;
let procPollHolds     = 0;
let procPollSeq       = 0;

function isIdleProc(row) {
  return row?.state === 'idle';
}

function formatIdleDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return '—';
  if (value < 60) return `${Math.max(0, Math.floor(value))}s`;
  if (value < 3600) return `${(value / 60).toFixed(1)}m`;
  if (value < 86400) return `${(value / 3600).toFixed(1)}h`;
  return `${(value / 86400).toFixed(1)}d`;
}

function updateProcStatusDot(processes, queued) {
  const active = processes.some(r => !isIdleProc(r));
  const hasIdle = !active && processes.some(isIdleProc);
  procStatusBtn.classList.toggle('has-procs', active || queued.length > 0);
  procStatusBtn.classList.toggle('has-idle', hasIdle);
}

function shouldShowQuotaStatusBackend(backend) {
  const info = _backendMetadata[backend];
  const gaugeType = info?.gauge?.type || 'none';
  if (gaugeType === 'none') return false;
  return !!info?.available;
}

function renderQuotaStatus() {
  // The status popup is a backend overview, so its rows must come from the
  // configured backend catalog. quotaSnapshots is populated lazily and only
  // contains gauges that have already been fetched (usually the active one).
  const backends = [...new Set([
    ...Object.keys(_backendMetadata),
    ...Object.keys(quotaSnapshots),
  ])].filter(shouldShowQuotaStatusBackend);
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

  if (!rows) return '';
  return `<div class="proc-section-label">Quotas</div>
    <div class="quota-status-list">${rows}</div>`;
}

function procStopButton(row) {
  return `<button class="proc-stop-btn" data-msgid="${row.msg_id || ''}" data-topic="${row.topic || ''}" data-agent="${row.agent || ''}">Stop</button>`;
}

function renderProcPopup(processes, queued) {
  const header = `<div class="proc-popup-header">
    <span class="settings-label">Status</span>
    <button id="proc-popup-close" type="button"><span class="close-desktop">Esc</span><span class="close-mobile">✕</span></button>
  </div>`;

  let body = renderQuotaStatus();
  const running = processes.filter(r => !isIdleProc(r));
  const idle = processes.filter(isIdleProc);
  if (!processes.length && !queued.length) {
    body += '<div class="proc-status-empty">No active processes or queued prompts.</div>';
  } else {
    if (running.length) {
      const rows = running.map(r => `
        <tr>
          <td><span class="proc-dot"></span>#${r.topic || '—'}</td>
          <td>@${r.agent || '—'}</td>
          <td class="proc-queue-preview">${r.prompt_preview || '—'}</td>
          <td>${r.duration_s}s</td>
          <td>${procStopButton(r)}</td>
        </tr>`).join('');
      body += `<div class="proc-section-label">Running</div>
        <table><thead><tr><th>Topic</th><th>Agent</th><th>Prompt</th><th>Time</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`;
    }
    if (idle.length) {
      const rows = idle.map(r => `
        <tr>
          <td><span class="proc-dot proc-dot-idle"></span>#${r.topic || '—'}</td>
          <td>@${r.agent || '—'}</td>
          <td class="proc-queue-preview">${r.prompt_preview || 'warm session'}</td>
          <td>${formatIdleDuration(r.state_duration_s ?? r.duration_s)}</td>
          <td>${procStopButton(r)}</td>
        </tr>`).join('');
      body += `<div class="proc-section-label">Idle Live Sessions</div>
        <table><thead><tr><th>Topic</th><th>Agent</th><th>Last prompt</th><th>Idle</th><th></th></tr></thead>
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

let procPopupOpenedAt = 0;

function toggleProcPopup() {
  const open = procStatusPopup.classList.toggle('open');
  if (open) {
    procPopupOpenedAt = Date.now();
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
    const hasActive = cachedProcRows.some(r => !isIdleProc(r));
    if (!hasActive && !cachedQueueRows.length && procPollHolds === 0 && !procStatusPopup.classList.contains('open')) stopProcPoll();
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
  _updateStatsBreakdownUi();

  if (!_statsPresetsLoaded) {
    _statsPresetsLoaded = true;
    await _loadStatsPresets({ applyDefault: true });
  }

  if (!_statsFiltersLoaded) {
    _statsFiltersLoaded = true;
    fetch('/stats/filters').then(r => r.json()).then(data => {
      _renderStatsMultiMenu(document.getElementById('sf-agent-menu'), data.agents, statsFilters.agents, '@');
      _renderStatsMultiMenu(document.getElementById('sf-topic-menu'), data.topics, statsFilters.topics, '#');
      _updateStatsFilterLabels();
    }).catch(() => {});
  }

  const params = _statsQueryParams({ includeTz: true });

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

  if (chartWrap) chartWrap.hidden = false;

  if (statsBreakdown && !statsFilters.topics.length && statsBreakdown.startsWith('topic_')) {
    const sessionCount = statsBreakdown.endsWith('_session') ? _sessionTypesForFilter().length : 1;
    const topicLimit = Math.max(1, Math.floor(_STATS_BREAKDOWN_SERIES_BUDGET / (2 * sessionCount)));
    const defaultTopics = _topDimensionValues(rows, 'topic', topicLimit);
    if (defaultTopics.length) {
      statsFilters.topics = defaultTopics;
      _syncStatsTopicMenuSelection();
    }
  }

  if (statsBreakdown && !statsFilters.agents.length) {
    const topicCount = statsBreakdown.startsWith('topic_') ? Math.max(1, statsFilters.topics.length || 1) : 1;
    const sessionCount = statsBreakdown.endsWith('_session') ? _sessionTypesForFilter().length : 1;
    const agentLimit = statsBreakdown === 'agent' ? _STATS_BREAKDOWN_SERIES_BUDGET : Math.max(1, Math.floor(_STATS_BREAKDOWN_SERIES_BUDGET / (topicCount * sessionCount)));
    const defaultAgents = _topDimensionValues(rows, 'agent', agentLimit);
    if (defaultAgents.length) {
      statsFilters.agents = defaultAgents;
      _syncStatsAgentMenuSelection();
    }
  }

  _lastStatsRows = rows;
  _statsPage = 0;
  _updateStatsBreakdownUi();

  if (statsBreakdown) {
    renderAgentBreakdownStats(rows);
    _renderBreakdownChart(rows);
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

  _setStatsTable(`<table>
    <thead><tr>
      <th>${statsPeriod === 'hourly' ? 'Hour' : 'Date'}</th>
      ${_statsMeasureHeaders()}
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td>Total</td>${_statsMeasureTotals(totals)}
    </tr></tfoot>
  </table>`);
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

  _setStatsTable(`<table>
    <thead><tr>
      <th>Topic</th>
      ${_statsMeasureHeaders()}
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td>Total</td>${_statsMeasureTotals(totals)}
    </tr></tfoot>
  </table>`);
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

  _setStatsTable(`<table>
    <thead><tr>
      <th>Agent</th>
      ${_statsMeasureHeaders()}
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td>Total</td>${_statsMeasureTotals(totals)}
    </tr></tfoot>
  </table>`);
  _statsAppendPager(rows.length);
}

function renderAgentBreakdownStats(rows) {
  const pivot = _breakdownPivot(rows);
  const metric = statsChartY1;
  const totalExplicit = {};
  for (const key of pivot.selected) totalExplicit[key] = 0;
  let totalMisc = pivot.canSumTotals ? 0 : null;
  let grandTotal = pivot.canSumTotals ? 0 : null;
  if (pivot.canSumTotals) {
    for (const row of pivot.periodRows) {
      for (const key of pivot.selected) totalExplicit[key] += row.values[key] || 0;
      totalMisc += row.misc || 0;
      grandTotal += row.total || 0;
    }
  }
  const hasMisc = pivot.canSumTotals;
  const totalText = pivot.canSumTotals ? null : '—';
  const headers = pivot.selected.map(key => {
    const label = pivot.labels.get(key) || key;
    return `<th class="stats-series-col" aria-label="${escapeHtml(label)}">${escapeHtml(label)}</th>`;
  }).join('');
  const miscTitle = pivot.overflowCount ? `Includes ${pivot.overflowCount} hidden selected series plus unselected matching data` : 'Unselected matching data';
  const miscHeader = hasMisc ? `<th class="stats-sticky-right stats-misc-col" title="${escapeHtml(miscTitle)}">Misc</th>` : '';
  const bodyRows = _statsPageSlice(pivot.periodRows).map(row => {
    const cells = pivot.selected
      .map(key => `<td class="stats-series-col">${_formatStatsMetricValue(row.values[key] || 0, metric)}</td>`)
      .join('');
    return `<tr>
      <td class="stats-sticky-left">${_statsPeriodLabel(row.period)}</td>
      ${cells}
      ${hasMisc ? `<td class="stats-sticky-right stats-misc-col">${_formatStatsMetricValue(row.misc || 0, metric)}</td>` : ''}
      <td class="stats-sticky-right stats-total-col">${pivot.canSumTotals ? _formatStatsMetricValue(row.total || 0, metric) : totalText}</td>
    </tr>`;
  }).join('');
  const totalCells = pivot.selected
    .map(key => `<td class="stats-series-col">${pivot.canSumTotals ? _formatStatsMetricValue(totalExplicit[key] || 0, metric) : totalText}</td>`)
    .join('');

  _setStatsTable(`<table class="stats-breakdown-table${hasMisc ? ' has-misc' : ''}">
    <thead><tr>
      <th class="stats-sticky-left">${_statsBreakdownAxisLabel(statsPeriod === 'hourly' ? 'Hour' : 'Date', 'name')}</th>
      ${headers}
      ${miscHeader}
      <th class="stats-sticky-right stats-total-col">Total</th>
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td class="stats-sticky-left">${_statsBreakdownAxisLabel('Total', 'total')}</td>
      ${totalCells}
      ${hasMisc ? `<td class="stats-sticky-right stats-misc-col">${_formatStatsMetricValue(totalMisc, metric)}</td>` : ''}
      <td class="stats-sticky-right stats-total-col">${pivot.canSumTotals ? _formatStatsMetricValue(grandTotal, metric) : totalText}</td>
    </tr></tfoot>
  </table>`);
  _bindStatsBreakdownSort();
  _statsAppendPager(pivot.periodRows.length);
}


function initStats() {
  document.getElementById('sf-period').addEventListener('change', e => {
    statsPeriod = e.target.value;
    _markStatsPresetDirty();
    loadStats();
  });

  document.getElementById('sf-days').addEventListener('change', e => {
    statsFilters.days = parseInt(e.target.value);
    _markStatsPresetDirty();
    loadStats();
  });

  document.getElementById('sf-adhoc').addEventListener('change', e => {
    statsFilters.adhoc = e.target.value;
    _markStatsPresetDirty();
    loadStats();
  });

  const filterMenus = [
    { wrap: document.getElementById('sf-topic-filter'), toggle: document.getElementById('sf-topic-toggle'), menu: document.getElementById('sf-topic-menu'), key: 'topics' },
    { wrap: document.getElementById('sf-agent-filter'), toggle: document.getElementById('sf-agent-toggle'), menu: document.getElementById('sf-agent-menu'), key: 'agents' },
  ];
  filterMenus.forEach(({ toggle, menu, key }) => {
    toggle.addEventListener('click', e => {
      e.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    menu.addEventListener('change', e => {
      if (!e.target.matches('input[type="checkbox"]')) return;
      statsFilters[key] = [...menu.querySelectorAll('input[type="checkbox"]:checked')].map(input => input.value);
      _updateStatsFilterLabels();
      _markStatsPresetDirty();
      loadStats();
    });
  });

  document.getElementById('sf-breakdown').addEventListener('change', e => {
    statsBreakdown = e.target.value;
    _statsBreakdownColumnSort = { mode: 'name', dir: 'asc' };
    e.target.value = statsBreakdown;
    _markStatsPresetDirty();
    if (statsBreakdown) {
      statsChartY2 = '';
      statsChartAggY2 = 'sum';
      const y2Sel = document.getElementById('sc-y2');
      y2Sel.hidden = true;
      y2Sel.value = '';
      document.getElementById('sc-y2-agg').hidden = true;
      document.getElementById('sc-compare-btn').textContent = '+ Y2';
    } else {
      _resetStatsDimensionFilters();
    }
    loadStats();
  });

  const measures = document.getElementById('sf-measures');
  const measuresToggle = document.getElementById('sf-measures-toggle');
  const measuresMenu = document.getElementById('sf-measures-menu');
  measuresToggle.addEventListener('click', e => {
    e.stopPropagation();
    if (measuresToggle.disabled) return;
    const open = measuresMenu.hidden;
    measuresMenu.hidden = !open;
    measuresToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  measuresMenu.querySelectorAll('input[type="checkbox"]').forEach(input => {
    input.addEventListener('change', () => {
      if (input.checked) _statsMeasures.add(input.value);
      else _statsMeasures.delete(input.value);
      _markStatsPresetDirty();
      _updateStatsMeasureLabel();
      _rerenderStats();
    });
  });
  document.addEventListener('click', e => {
    if (!measures.contains(e.target)) {
      measuresMenu.hidden = true;
      measuresToggle.setAttribute('aria-expanded', 'false');
    }
    filterMenus.forEach(({ wrap, toggle, menu }) => {
      if (!wrap.contains(e.target)) {
        menu.hidden = true;
        toggle.setAttribute('aria-expanded', 'false');
      }
    });
  });
  _updateStatsFilterLabels();
  _updateStatsMeasureLabel();
  _syncStatsChartAggControls();

  document.getElementById('sc-y1').addEventListener('change', e => {
    statsChartY1 = e.target.value;
    statsChartAggY1 = _normalizeStatsAgg(statsChartY1, statsChartAggY1);
    _syncStatsChartAggControls();
    _markStatsPresetDirty();
    loadStats();
  });

  document.getElementById('sc-y1-agg').addEventListener('change', e => {
    statsChartAggY1 = _normalizeStatsAgg(statsChartY1, e.target.value);
    _syncStatsChartAggControls();
    _markStatsPresetDirty();
    loadStats();
  });

  const y2Sel = document.getElementById('sc-y2');
  y2Sel.addEventListener('change', e => {
    statsChartY2 = e.target.value;
    statsChartAggY2 = _normalizeStatsAgg(statsChartY2, statsChartAggY2);
    _syncStatsChartAggControls();
    _markStatsPresetDirty();
    if (!statsChartY2) {
      y2Sel.hidden = true;
      document.getElementById('sc-y2-agg').hidden = true;
      document.getElementById('sc-compare-btn').textContent = '+ Y2';
    }
    loadStats();
  });

  document.getElementById('sc-y2-agg').addEventListener('change', e => {
    statsChartAggY2 = _normalizeStatsAgg(statsChartY2, e.target.value);
    _syncStatsChartAggControls();
    _markStatsPresetDirty();
    loadStats();
  });

  document.getElementById('sc-compare-btn').addEventListener('click', () => {
    const hidden = y2Sel.hidden;
    y2Sel.hidden = !hidden;
    document.getElementById('sc-y2-agg').hidden = !hidden || !statsChartY2;
    document.getElementById('sc-compare-btn').textContent = hidden ? '− Y2' : '+ Y2';
    _syncStatsChartAggControls();
    _markStatsPresetDirty();
    if (!hidden) {
      statsChartY2 = '';
      statsChartAggY2 = 'sum';
      document.getElementById('sc-y2-agg').hidden = true;
      loadStats();
    }
  });

  document.getElementById('stats-preset-select')?.addEventListener('change', e => {
    if (e.target.value === '__overall') {
      _activeStatsPresetId = null;
      _applyStatsState(_overallStatsState());
      _renderStatsPresetControls();
      _setStatsPresetStatus('');
      loadStats();
      return;
    }
    const preset = _statsPresets.find(item => String(item.id) === e.target.value);
    if (!preset) {
      _renderStatsPresetControls();
      return;
    }
    _activeStatsPresetId = preset.id;
    _applyStatsState(preset.state);
    _renderStatsPresetControls();
    _setStatsPresetStatus('');
    loadStats();
  });
  document.getElementById('stats-preset-save')?.addEventListener('click', () => _saveStatsPreset());
  document.getElementById('stats-preset-update')?.addEventListener('click', () => _saveStatsPreset({ update: true }));
  document.getElementById('stats-preset-default')?.addEventListener('click', () => _saveStatsPreset({ makeDefault: true }));
  document.getElementById('stats-preset-delete')?.addEventListener('click', async () => {
    if (!_activeStatsPresetId) return;
    await fetch(`/stats/filter-presets/${_activeStatsPresetId}`, { method: 'DELETE' });
    _activeStatsPresetId = null;
    _applyStatsState(_overallStatsState());
    await _loadStatsPresets();
    _renderStatsPresetControls();
    _setStatsPresetStatus('deleted');
    loadStats();
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
    const isProvider  = info.kind === 'provider';
    const color       = agentThemeColor(id);
    const label       = info.label;
    const installCmd  = driverInfo.installCmd || '';
    const gauge       = info.gauge || { type: 'none' };
    const usesBackendApiKey = info.provider === 'deepseek' || gauge.type === 'deepseek';
    const readyViaConfig = isProvider || usesBackendApiKey;
    const authHint    = usesBackendApiKey
      ? 'DeepSeek API key configured'
      : (isProvider ? 'configured in YAML' : (driverInfo.authHint || `uses ${info.driver} driver`));
    const gaugeHint   = gauge.type === 'static'
      ? (gauge.text || 'static')
      : (GAUGE_CATALOG[gauge.type] || '—');

    let codingHtml;
    if (available) {
      codingHtml = `<span class="bcat-status-ok">✓ ${readyViaConfig ? 'ready' : 'detected'}</span>
        <span class="bcat-hint">${escapeHtml(authHint)}</span>`;
    } else {
      const missingItems = info.missing_requirements || [
        ...(info.missing_settings || []),
        ...(info.missing_secrets || []),
      ];
      const missing = usesBackendApiKey && missingItems.includes('api_key')
        ? 'configure DeepSeek API key in backend YAML'
        : (missingItems.length ? `missing: ${missingItems.join(', ')}` : 'driver not found');
      codingHtml = `<span class="bcat-status-miss">✗ ${escapeHtml(missing)}</span>` +
        (installCmd && !missingItems.length ? `<div class="bcat-install">
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
      <div class="bcat-coding">${codingHtml}<span class="bcat-hint">protocol: ${escapeHtml(info.protocol || 'oneshot-cli')}</span></div>
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
let _configCmView = null;

function setConfigStatus(text, state = 'neutral') {
  const status = document.getElementById('config-editor-status');
  if (!status) return;
  status.textContent = text || '';
  status.classList.remove('ok', 'error');
  if (state === 'ok' || state === 'error') status.classList.add(state);
}

async function loadConfigYaml() {
  const container = document.getElementById('config-editor');
  if (!container) return;
  setConfigStatus('loading…');
  try {
    const res = await fetch('/config/yaml');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed to load configuration');
    _configRevision = data.revision;
    document.getElementById('config-editor-path').textContent = data.path;

    // Use CodeMirror if available
    if (window._cm) {
      const { EditorView, EditorState, basicSetup, oneDark, atomOneDarkHighlight, LANGS } = window._cm;
      if (_configCmView) _configCmView.destroy();
      const extensions = [basicSetup, oneDark];
      if (atomOneDarkHighlight) extensions.push(atomOneDarkHighlight);
      const lang = LANGS.yaml?.();
      if (lang) extensions.push(lang);
      _configCmView = new EditorView({
        state: EditorState.create({ doc: data.content, extensions }),
        parent: container,
      });
      window._configCmView = _configCmView;
    } else {
      // Fallback: wait for CM to load
      const ok = await (window._cmPromise || Promise.resolve(false));
      if (ok) return loadConfigYaml();
      container.textContent = data.content;
      container.style.whiteSpace = 'pre-wrap';
      container.style.font = '12px/1.55 JetBrains Mono, ui-monospace, monospace';
      container.style.padding = '0.75rem';
      container.style.color = 'var(--text-muted)';
      container.style.overflow = 'auto';
    }
    setConfigStatus('');
  } catch (err) {
    setConfigStatus(`Error: ${err.message || 'failed to load'}`, 'error');
  }
}

function getConfigContent() {
  if (_configCmView) return _configCmView.state.doc.toString();
  return null;
}

async function saveConfigYaml() {
  const save = document.getElementById('config-editor-save');
  save.disabled = true;
  setConfigStatus('validating…');
  const content = getConfigContent();
  if (content === null) {
    setConfigStatus('Error: editor not ready — try again', 'error');
    save.disabled = false;
    return;
  }
  try {
    const res = await fetch('/config/yaml', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, revision: _configRevision }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed to save configuration');
    _configRevision = data.revision;
    setConfigStatus(data.restart_required ? 'saved ✓ · restart required' : 'saved ✓', 'ok');
  } catch (err) {
    setConfigStatus(`Error: ${err.message || 'save failed'}`, 'error');
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
  if (!agents.length) {
    listEl.innerHTML = '<div class="empty">No agents yet. Add one below.</div>';
    return;
  }
  const rows = agents.map(a => {
    const model = a.model || '';
    const modelHtml = model
      ? `<span class="agent-model" title="${escapeHtml(model)}">${escapeHtml(model)}</span>`
      : '<span class="col-default">—</span>';
    return `
    <tr>
      <td><span class="agent-name">${a.name}</span></td>
      <td>${a.backend}</td>
      <td class="col-model">${modelHtml}</td>
      <td class="col-cwd">${a.cwd || `<span class="col-default">${_squidHome}</span>`}</td>
      <td>
        <button class="edit-btn" data-name="${escapeHtml(a.name)}" data-backend="${escapeHtml(a.backend)}" data-model="${escapeHtml(a.model || '')}" data-cwd="${escapeHtml(a.cwd || '')}" title="Edit agent">✎</button>
        <button class="del-btn" data-name="${a.name}" title="Delete agent (does not affect existing messages)">✕</button>
      </td>
    </tr>`;
  }).join('');
  listEl.innerHTML = `<table>
    <thead><tr><th>Name</th><th>Backend</th><th class="col-model">Model</th><th class="col-cwd">CWD</th><th></th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;

  listEl.querySelectorAll('.edit-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.getElementById('af-name').value    = btn.dataset.name;
      document.getElementById('af-backend').value = btn.dataset.backend;
      document.getElementById('af-model').value   = btn.dataset.model;
      document.getElementById('af-cwd').value     = btn.dataset.cwd;
      document.getElementById('agent-form').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      document.getElementById('af-name').focus();
    });
  });

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
let _acStashedForNav = false;

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
  acOpen = false; acItems = []; acSel = -1; _acStashedForNav = false;
}

function _acPreview() {
  if (acSel < 0 || acSel >= acItems.length) return;
  const item = acItems[acSel];
  if (!_acStashedForNav) {
    promptDraft = input.value;
    promptDraftChip = stickyChip ? { ...stickyChip } : null;
    _acStashedForNav = true;
  }
  if (item.fullEntry) {
    applyPromptHistoryEntry(item.fullEntry);
  } else {
    input.value = item.insert;
    input.setSelectionRange(item.insert.length, item.insert.length);
    resizeComposer();
  }
}

function _acRestoreDraft() {
  const had = _acStashedForNav;
  hideAutocomplete();
  if (had) {
    input.value = promptDraft;
    if (promptDraftChip) setTopicChip(promptDraftChip.topic, promptDraftChip.agent, promptDraftChip.adhoc, promptDraftChip.lookback || 0);
    else clearTopicChip();
    promptDraft = '';
    promptDraftChip = null;
    resizeComposer();
  }
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
    (item.routeHtml ? `<button class="ac-route-btn" type="button" data-i="${i}" title="Switch to this route">${item.routeHtml}<span class="ac-route-switch-icon" aria-hidden="true"></span></button> ` : '') +
    `<span class="ac-label">${item.label}</span>` +
    (item.sub ? `<span class="ac-sub">${item.sub}</span>` : '') +
    (item.meta ? `<span class="ac-meta">${item.meta}</span>` : '') +
    (item.deleteTopic ? `<button class="ac-del-btn" data-topic="${item.deleteTopic}" type="button" title="Delete #${item.deleteTopic} sessions">✕</button>` : '') +
    (item.deletePromptEntry != null ? `<button class="ac-del-btn ac-del-prompt-btn" data-i="${i}" type="button" title="Remove from history">✕</button>` : '') +
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
      if (e.target.classList.contains('ac-del-btn') || e.target.classList.contains('ac-del-prompt-btn')) return;
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
  acEl.querySelectorAll('.ac-del-btn:not(.ac-del-prompt-btn)').forEach(btn =>
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
  acEl.querySelectorAll('.ac-del-prompt-btn').forEach(btn =>
    btn.addEventListener('mousedown', e => {
      e.preventDefault(); e.stopPropagation();
      const idx = Number(btn.dataset.i);
      if (idx < 0 || idx >= acItems.length) return;
      const entry = acItems[idx].deletePromptEntry;
      if (entry) hidePrompt(entry);
      acItems = acItems.filter((_, i) => i !== idx);
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

function _acRouteLabel(topic, agent = '', backendFallback = null) {
  const cleanAgent = agent.replace(/[!]\d*$/, '');
  return `<span class="ac-topic">#${escapeHtml(topic)}</span>` +
    (agent ? `<span class="ac-agent"${_agentStyleAttr(cleanAgent, backendFallback)}>@${escapeHtml(agent)}</span>` : '');
}

function _acRouteHtml(route) {
  const rm = String(route || '').match(/^#(\w+)(?:@(\w+))?(!\d*)?$/);
  if (!rm) return '';
  return _acRouteLabel(rm[1], (rm[2] || '') + (rm[3] || ''));
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
  const mAlias = slugVal.match(/^#(\w+)@(\w*)(!\d*)?$/);
  if (mTopic) {
    const prefix = mTopic[1].toLowerCase();
    const topics = await _acTopics();
    if (input.value !== val) return;
    _acRender(
      topics.filter(t => t.name.toLowerCase().startsWith(prefix)).slice(0, 8)
        .map(t => ({
          label:       _acRouteLabel(t.name, t.agent || '', t.last_backend || null),
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
    const adhocSuffix = mAlias[3] || '';
    const preserveAdhocSuffix = mAlias[3] !== undefined;
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
      if (preserveAdhocSuffix) {
        items.push({
          label:  _acRouteLabel(topic, h.agent + adhocSuffix, backendByAgent.get(h.agent) || null),
          insert: `#${topic}@${h.agent}${adhocSuffix}`,
          replaceSlug: replacingSlug,
          sub:    _acLastPrompt(h.last_adhoc_prompt),
          meta:   'adhoc',
        });
        continue;
      }
      // Default topic: suppress session variant — adhoc only
      if (!isDefault) {
        items.push({
          label:  _acRouteLabel(topic, h.agent, backendByAgent.get(h.agent) || null),
          insert: `#${topic}@${h.agent}`,
          replaceSlug: replacingSlug,
          sub:    _acLastPrompt(h.last_prompt),
        });
      }
      items.push({
        label:  _acRouteLabel(topic, h.agent + '!', backendByAgent.get(h.agent) || null),
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
      if (preserveAdhocSuffix) {
        items.push({
          label:  _acRouteLabel(topic, a.name + adhocSuffix, a.backend),
          insert: `#${topic}@${a.name}${adhocSuffix}`,
          replaceSlug: replacingSlug,
          meta:   a.backend,
        });
        continue;
      }
      // Default topic: only offer adhoc variant
      items.push({
        label:  _acRouteLabel(topic, isDefault ? a.name + '!' : a.name, a.backend),
        insert: `#${topic}@${a.name}${isDefault ? '!' : ''}`,
        replaceSlug: replacingSlug,
        meta:   a.backend,
      });
    }

    _acRender(items.slice(0, 10), 'Routes');
  } else if (editingExpandedSlug) {
    hideAutocomplete();
  } else if (promptHistory.length) {
    _acRender(promptHistoryAutocompleteItems(matchingPromptHistory(val)), 'Recent Prompts');
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
  if (topic) html += `<div id="ctx-roots-section"></div>`;
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

  if (topic) {
    fetchMemoryMeta(topic).then(meta => {
      const roots = meta?.squid?.code_roots || [];
      const placeholder = document.getElementById('ctx-roots-section');
      if (!placeholder) return;
      if (!roots.length) { placeholder.remove(); return; }
      const frag = document.createDocumentFragment();
      const divider = document.createElement('div');
      divider.className = 'ctx-popup-divider';
      frag.appendChild(divider);
      roots.forEach((root, i) => {
        const row = document.createElement('div');
        row.className = 'ctx-popup-row';
        row.innerHTML = `<span class="ctx-popup-key">${i === 0 ? 'roots' : ''}</span><span class="ctx-popup-val">${escapeHtml(root)}</span>`;
        frag.appendChild(row);
      });
      placeholder.replaceWith(frag);
    }).catch(() => {
      const placeholder = document.getElementById('ctx-roots-section');
      if (placeholder) placeholder.remove();
    });
  }

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
  try { return (JSON.parse(localStorage.getItem('pinnedItems') || '[]')).sort((a, b) => a.id - b.id); } catch { return []; }
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
      if (!_acStashedForNav && data?.session_id && stickyChip && !stickyChip.adhoc &&
          stickyChip.topic === topic && stickyChip.agent === agent) {
        const count = _sessionTurnCounts[data.session_id] || 0;
        if (count > 0) _renderChipTurnCount(count);
      }
      updatePinCount();
      if (pinPanel.classList.contains('open')) renderPinPanel();
      updateInContextMarkers();
      evaluateAdvisory();
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
    html += '<div style="padding:0.5rem 0.8rem;color:#484858;font-size:0.78em">No pins yet.<br>Click <svg width="10" height="11" viewBox="0 0 32 32" fill="currentColor" aria-hidden="true" style="vertical-align:-0.1em"><path d="M25 21v1H8v-1l2-2L11 4L9 2V1h15v1l-2 2l1 15l2 2zM16 31h1l1-8h-3l1 8z"/></svg> on any response to add it.</div>';
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

// ── Bookmarks ─────────────────────────────────────────────────────────────

let _bookmarkItems = [];
let _bookmarkIds = new Set();

function getBookmarkedItems() { return _bookmarkItems; }

async function _loadBookmarks() {
  try {
    const res = await fetch('/bookmarks');
    if (!res.ok) return;
    const data = await res.json();
    _bookmarkItems = data.items || [];
    _bookmarkIds = new Set(_bookmarkItems.map(i => i.id));
    // one-time migration: push any localStorage bookmarks to the server
    const legacy = (() => { try { return JSON.parse(localStorage.getItem('bookmarkedItems') || '[]'); } catch { return []; } })();
    for (const item of legacy) {
      if (!_bookmarkIds.has(item.id)) {
        await fetch('/bookmarks', { method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ msg_id: item.id, topic: item.topic, agent: item.agent || null, content: item.content || null }) });
      }
    }
    if (legacy.length) {
      localStorage.removeItem('bookmarkedItems');
      const res2 = await fetch('/bookmarks');
      if (res2.ok) { const d = await res2.json(); _bookmarkItems = d.items || []; _bookmarkIds = new Set(_bookmarkItems.map(i => i.id)); }
    }
  } catch { /* ignore — falls back to empty */ }
}

async function _apiToggleBookmark(msgId, topic, agent, text) {
  if (_bookmarkIds.has(msgId)) {
    _bookmarkIds.delete(msgId);
    _bookmarkItems = _bookmarkItems.filter(i => i.id !== msgId);
    fetch(`/bookmarks/${msgId}`, { method: 'DELETE' }).catch(() => {});
    return false;
  } else {
    const content = text ? text.slice(0, 300) : null;
    _bookmarkIds.add(msgId);
    _bookmarkItems = [{ id: msgId, topic, agent: agent || null, content, saved_at: new Date().toISOString() }, ..._bookmarkItems];
    fetch('/bookmarks', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ msg_id: msgId, topic, agent: agent || null, content }) }).catch(() => {});
    return true;
  }
}

let bookmarkOnlyHistory = false;

function updateBookmarkButton() {
  bookmarkBtn.setAttribute('aria-pressed', bookmarkOnlyHistory ? 'true' : 'false');
  bookmarkBtn.title = bookmarkOnlyHistory ? 'Show full thread' : 'Bookmarked only';
}

function toggleBookmarkOnlyHistory() {
  bookmarkOnlyHistory = !bookmarkOnlyHistory;
  if (bookmarkOnlyHistory && promptOnlyHistory) {
    promptOnlyHistory = false;
    updatePromptOnlyButton();
  }
  updateBookmarkButton();
  if (searchActive) {
    document.querySelectorAll('.search-result-item, .date-divider').forEach(el => el.remove());
    loadSearchResults();
  } else {
    reloadHistory(historyFilter);
  }
}

function addBookmarkButton(bubbleEl, msgId, topic, agent) {
  const existing = bubbleEl.querySelector(`.msg-bookmark-btn[data-msg-id="${msgId}"]`);
  if (existing) return existing;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'msg-bookmark-btn';
  btn.dataset.msgId = String(msgId);
  btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M4 1h8a1 1 0 0 1 1 1v12l-5-2.8-5 2.8V2a1 1 0 0 1 1-1z"/></svg>`;
  if (_bookmarkIds.has(msgId)) {
    btn.classList.add('bookmarked');
    btn.title = 'Remove bookmark';
  } else {
    btn.title = 'Bookmark';
  }
  btn.addEventListener('click', async e => {
    e.stopPropagation();
    const text = _messageBodyText(bubbleEl);
    const nowBookmarked = await _apiToggleBookmark(msgId, topic, agent, text);
    btn.classList.toggle('bookmarked', nowBookmarked);
    btn.title = nowBookmarked ? 'Remove bookmark' : 'Bookmark';
  });
  const header = bubbleEl.querySelector('.response-header');
  const target = header || bubbleEl;
  target.insertBefore(btn, target.firstChild);
}

function initBookmark() {
  bookmarkBtn.addEventListener('click', toggleBookmarkOnlyHistory);
  updateBookmarkButton();
  _loadBookmarks();
}

function addPinButton(bubbleEl, msgId, topic, agent, sessionId = null) {
  const existing = bubbleEl.querySelector(`.msg-pin-btn[data-msg-id="${msgId}"]`);
  if (existing) {
    if (sessionId) bubbleEl.dataset.sessionId = sessionId;
    return existing;
  }
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'msg-pin-btn';
  btn.dataset.msgId = String(msgId);
  btn.title = 'Pin as context';
  btn.innerHTML = `<svg width="14" height="15" viewBox="0 0 32 32" fill="currentColor" aria-hidden="true">
    <path d="M25 21v1H8v-1l2-2L11 4L9 2V1h15v1l-2 2l1 15l2 2zM16 31h1l1-8h-3l1 8z"/>
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
  document.getElementById('restart-modal-close').addEventListener('click', () => closeRestartModal(false));
  document.getElementById('restart-modal-cancel').addEventListener('click', () => closeRestartModal(false));
  document.getElementById('restart-modal-confirm').addEventListener('click', () => closeRestartModal(true));
  document.getElementById('restart-modal').addEventListener('mousedown', e => {
    if (e.target === document.getElementById('restart-modal')) closeRestartModal(false);
  });
  document.getElementById('topic-delete-modal-close').addEventListener('click', closeTopicDeleteModal);
  document.getElementById('topic-delete-cancel').addEventListener('click', closeTopicDeleteModal);
  document.getElementById('topic-delete-confirm').addEventListener('click', confirmTopicDelete);
  document.getElementById('topic-delete-modal').addEventListener('mousedown', e => {
    if (e.target === document.getElementById('topic-delete-modal')) closeTopicDeleteModal();
  });
  document.getElementById('preset-name-modal-close').addEventListener('click', () => _closePresetNameModal(null));
  document.getElementById('preset-name-cancel').addEventListener('click', () => _closePresetNameModal(null));
  document.getElementById('preset-name-confirm').addEventListener('click', () => {
    const name = document.getElementById('preset-name-input').value.trim();
    if (name) _closePresetNameModal(name);
  });
  document.getElementById('preset-name-input').addEventListener('input', e => {
    document.getElementById('preset-name-confirm').disabled = !e.target.value.trim();
  });
  document.getElementById('preset-name-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const name = document.getElementById('preset-name-input').value.trim();
      if (name) _closePresetNameModal(name);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      _closePresetNameModal(null);
    }
  });
  document.getElementById('preset-name-modal').addEventListener('mousedown', e => {
    if (e.target === document.getElementById('preset-name-modal')) _closePresetNameModal(null);
  });
  updatePinCount();
}

// ── init ─────────────────────────────────────────────────────────────────────

initSettings();
initPin();
initBookmark();
document.getElementById('search-bar-clear').addEventListener('click', clearSearch);
document.getElementById('chip-prompts-btn')?.addEventListener('click', togglePromptOnlyHistory);
updatePromptOnlyButton();
updateSearchButton();

function formatFilterCommand(state) {
  let scope = '';
  if (state.topic) scope = '#' + state.topic;
  if (state.agent) {
    scope += '@' + state.agent;
    if (state.adhoc === true) scope += '!';
    else if (state.adhoc === null) scope += '+';
  }
  return scope ? `/f ${scope}` : '/f reset';
}

function hideAdvisory() {
  sessionAdvisoryEl.hidden = true;
  _advisoryDismissKey = null;
}

function evaluateAdvisory() {
  if (!stickyChip || stickyChip.adhoc) { hideAdvisory(); return; }
  const { topic, agent } = stickyChip;
  const sessionId = _sessionIds[`${topic}@${agent || '_'}`];
  if (!sessionId) { hideAdvisory(); return; }

  const domEls = messages.querySelectorAll(`[data-session-id="${CSS.escape(sessionId)}"][data-session-turn-count]`);
  const turnCount = Math.max(
    domEls.length ? parseInt(domEls[domEls.length - 1].dataset.sessionTurnCount || '0', 10) || 0 : 0,
    _advisoryTurnCount
  );

  const ltaStr = localStorage.getItem(`squid_adv_lta_${topic}_${agent||'_'}_${sessionId}`);
  const idleH = ltaStr ? (Date.now() - parseInt(ltaStr, 10)) / 3600000 : 0;
  const turnBucket = Math.floor(turnCount / 10);

  if (idleH >= 1) {
    const key = `squid_adv_dis_${topic}_${agent||'_'}_${sessionId}_idle`;
    if (!localStorage.getItem(key)) {
      const h = Math.floor(idleH);
      const ago = h < 2 ? 'an hour ago' : h < 24 ? `${h}h ago` : 'yesterday';
      sessionAdvisoryMsgEl.textContent = `Still need context from ${ago}?`;
      _advisoryDismissKey = key;
      sessionAdvisoryEl.hidden = false;
      return;
    }
  }
  if (turnBucket >= 1) {
    const key = `squid_adv_dis_${topic}_${agent||'_'}_${sessionId}_t${turnBucket}`;
    if (!localStorage.getItem(key)) {
      sessionAdvisoryMsgEl.textContent = `Need all ${turnBucket * 10}+ turns in context?`;
      _advisoryDismissKey = key;
      sessionAdvisoryEl.hidden = false;
      return;
    }
  }
  hideAdvisory();
}

window._squidEvalAdvisory = evaluateAdvisory;
document.getElementById('session-advisory-clear').addEventListener('click', () => stashComposerAndEdit('/clear'));
document.getElementById('session-advisory-dismiss').addEventListener('click', () => {
  if (_advisoryDismissKey) localStorage.setItem(_advisoryDismissKey, '1');
  hideAdvisory();
});

function stashComposerAndEdit(command) {
  const prev = input.value.trim();
  commandEditRestore = prev && prev !== command ? prev : null;
  if (prev && prev !== command && !prev.startsWith('/')) {
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
    else if (state.agent && state.adhoc === null) cmd += '+';
    cmd += ' ';
  } else if (state.agent) {
    cmd += '@' + state.agent;
    if (state.adhoc === true) cmd += '!';
    else if (state.adhoc === null) cmd += '+';
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
  if (e.key === 'Escape' && closeEscSurfaces()) {
    e.preventDefault();
  }
});
document.addEventListener('click', e => {
  if (!acEl.contains(e.target) && e.target !== input) hideAutocomplete();
  if (!pinPanel.contains(e.target) && !pinBtn.contains(e.target)) closePinPanel();
  const ctxPopup = document.getElementById('ctx-popup');
  const inSecondary = e.target.closest('#msg-modal, #memory-modal, #topic-delete-modal, #preset-name-modal');
  const secondaryOpen = document.getElementById('msg-modal')?.classList.contains('open')
    || document.getElementById('memory-modal')?.classList.contains('open')
    || document.getElementById('topic-delete-modal')?.classList.contains('open')
    || document.getElementById('preset-name-modal')?.classList.contains('open');
  if (ctxPopup && !ctxPopup.contains(e.target) && !e.target.closest('.user-ctx') && !inSecondary && !secondaryOpen) {
    ctxPopup.classList.remove('open');
  }
  if (
    !procStatusPopup.contains(e.target) && e.target !== procStatusBtn && !procStatusBtn.contains(e.target)
    && Date.now() - procPopupOpenedAt > 300
  ) {
    procStatusPopup.classList.remove('open');
  }
});
// ── file viewer ───────────────────────────────────────────────────────────────

const _TEXT_EXTS = new Set(['txt','md','py','js','ts','jsx','tsx','json','yaml','yml',
  'toml','ini','cfg','conf','sh','bash','zsh','fish','rb','go','rs','java','c','cpp',
  'h','hpp','cs','php','swift','kt','kts','lua','r','sql','html','css','xml','svg',
  'log','env','gitignore','dockerfile','makefile','lock','csv','tsv']);

const _WEB_PREVIEW_EXTS = new Set(['html','htm','svg','pdf','png','jpg','jpeg','gif','webp','avif','md','markdown']);

const _GENERIC_SUFFIXES = new Set(['example', 'sample', 'dist', 'template', 'orig', 'bak']);

function _isTextPath(path) {
  const ext = (path.split('.').pop() || '').toLowerCase();
  if (_TEXT_EXTS.has(ext) || !path.includes('.')) return true;
  // strip generic trailing suffixes (config.yaml.example, settings.json.sample, …)
  // and re-check the real extension underneath instead of misreading it as binary
  if (_GENERIC_SUFFIXES.has(ext)) return _isTextPath(path.slice(0, -(ext.length + 1)));
  return false;
}

function _isWebPreviewPath(path) {
  const ext = (path.split('.').pop() || '').toLowerCase();
  return _WEB_PREVIEW_EXTS.has(ext);
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

function openFileRootBrowser() {
  openFileViewer(null);
}

function openFilesTabView() {
  openFileViewer(null, null, null, document.getElementById('view-files'));
}

function openFileViewer(initialPath, initialLine, initialEndLine, inlineContainer = null, initialChangedLines = null) {
  document.getElementById('file-modal')?.remove();
  _fvNavigate = null;

  const isInline = !!inlineContainer;

  const navHistory = [{ path: initialPath, line: initialLine, endLine: initialEndLine }];
  let historyIdx = 0;
  let path = initialPath;
  let line = initialLine;
  let endLine = initialEndLine;
  let changedLines = initialChangedLines;
  let pathKind = initialPath ? null : 'roots';
  let pathIsText = false;

  // ── DOM ──────────────────────────────────────────────────────────────────────
  let modal, box;
  if (isInline) {
    inlineContainer.innerHTML = '';
    box = document.createElement('div');
    box.id = 'file-modal-box';
    inlineContainer.appendChild(box);
    modal = inlineContainer;
  } else {
    modal = document.createElement('div');
    modal.id = 'file-modal';
    box = document.createElement('div');
    box.id = 'file-modal-box';
  }

  const header = document.createElement('div');
  header.id = 'file-modal-header';

  // SVG icon set for the file viewer header (16x16, stroke-based, matches #send-btn's line style)
  const FV_ICON_CHEVRON_LEFT = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M10 3.5L5.5 8l4.5 4.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const FV_ICON_CHEVRON_RIGHT = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M6 3.5L10.5 8 6 12.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const FV_ICON_EXTERNAL_LINK = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M7 3H4.5A1.5 1.5 0 0 0 3 4.5v7A1.5 1.5 0 0 0 4.5 13h7a1.5 1.5 0 0 0 1.5-1.5V9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M9.5 3H13v3.5M13 3 7.5 8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const FV_ICON_PENCIL = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M11.4 2.4a1.4 1.4 0 0 1 2 2L5.6 12.2l-2.7.7.7-2.7 7.8-7.8Z" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const FV_ICON_HISTORY = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="8" cy="8.5" r="5" stroke="currentColor" stroke-width="1.4"/><path d="M8 5.8v2.9l2 1.2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/><path d="M6 2.2h4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
  const FV_ICON_COPY = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="6" y="6" width="7.5" height="7.5" rx="1.3" stroke="currentColor" stroke-width="1.4"/><path d="M3.8 10.2h-.3A1.5 1.5 0 0 1 2 8.7v-5A1.5 1.5 0 0 1 3.5 2.2h5A1.5 1.5 0 0 1 10 3.7v.3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const FV_ICON_CHECK = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3.5 8.3l3 3 6-6.6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const FV_ICON_CLOSE = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';

  const navBtns = document.createElement('div');
  navBtns.className = 'fv-nav-btns';
  const backBtn = document.createElement('button');
  backBtn.className = 'fv-nav-btn';
  backBtn.setAttribute('aria-label', 'Back');
  backBtn.innerHTML = FV_ICON_CHEVRON_LEFT;
  const fwdBtn = document.createElement('button');
  fwdBtn.className = 'fv-nav-btn';
  fwdBtn.setAttribute('aria-label', 'Forward');
  fwdBtn.innerHTML = FV_ICON_CHEVRON_RIGHT;
  navBtns.append(backBtn, fwdBtn);

  const breadcrumb = document.createElement('div');
  breadcrumb.id = 'file-modal-breadcrumb';

  const actions = document.createElement('div');
  actions.className = 'fv-header-actions';
  const previewBtn = document.createElement('button');
  previewBtn.className = 'fv-action-btn';
  previewBtn.title = 'Preview in browser';
  previewBtn.setAttribute('aria-label', 'Preview in browser');
  previewBtn.innerHTML = FV_ICON_EXTERNAL_LINK;
  const editBtn = document.createElement('button');
  editBtn.className = 'fv-action-btn';
  editBtn.title = 'Edit file';
  editBtn.setAttribute('aria-label', 'Edit file');
  editBtn.innerHTML = FV_ICON_PENCIL;
  editBtn.hidden = true;
  const historyBtn = document.createElement('button');
  historyBtn.className = 'fv-action-btn';
  historyBtn.title = 'Edit history';
  historyBtn.setAttribute('aria-label', 'Edit history');
  historyBtn.innerHTML = FV_ICON_HISTORY;
  historyBtn.hidden = true;
  const copyBtn = document.createElement('button');
  copyBtn.className = 'fv-action-btn';
  copyBtn.title = 'Copy path';
  copyBtn.innerHTML = FV_ICON_COPY;
  const closeBtn = document.createElement('button');
  closeBtn.id = 'file-modal-close';
  closeBtn.innerHTML = FV_ICON_CLOSE;
  actions.append(previewBtn, historyBtn, editBtn, copyBtn, closeBtn);

  // edit footer (shown only in edit mode, appended to box)
  const editFooter = document.createElement('div');
  editFooter.className = 'fv-edit-footer';
  editFooter.hidden = true;
  const editStatus = document.createElement('span');
  editStatus.className = 'fv-edit-status';
  const editTools = document.createElement('div');
  editTools.className = 'fv-edit-tools';
  const findInput = document.createElement('input');
  findInput.className = 'fv-edit-find';
  findInput.type = 'search';
  findInput.placeholder = 'Find';
  findInput.setAttribute('aria-label', 'Find in editor');
  const findPrevBtn = document.createElement('button');
  findPrevBtn.className = 'fv-edit-tool-btn';
  findPrevBtn.type = 'button';
  findPrevBtn.title = 'Previous match';
  findPrevBtn.setAttribute('aria-label', 'Previous match');
  findPrevBtn.textContent = '↑';
  const findNextBtn = document.createElement('button');
  findNextBtn.className = 'fv-edit-tool-btn';
  findNextBtn.type = 'button';
  findNextBtn.title = 'Next match';
  findNextBtn.setAttribute('aria-label', 'Next match');
  findNextBtn.textContent = '↓';
  const lineInput = document.createElement('input');
  lineInput.className = 'fv-edit-line';
  lineInput.type = 'number';
  lineInput.min = '1';
  lineInput.placeholder = 'Line';
  lineInput.setAttribute('aria-label', 'Line number');
  const lineGoBtn = document.createElement('button');
  lineGoBtn.className = 'fv-edit-tool-btn';
  lineGoBtn.type = 'button';
  lineGoBtn.title = 'Go to line';
  lineGoBtn.setAttribute('aria-label', 'Go to line');
  lineGoBtn.textContent = 'Go';
  editTools.append(findInput, findPrevBtn, findNextBtn, lineInput, lineGoBtn);
  const findPopover = document.createElement('div');
  findPopover.className = 'fv-edit-find-popover';
  findPopover.hidden = true;
  const saveBtn = document.createElement('button');
  saveBtn.className = 'fv-save-btn';
  saveBtn.textContent = 'Save';
  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'fv-cancel-btn';
  cancelBtn.textContent = 'Cancel';
  editFooter.append(editStatus, editTools, cancelBtn, saveBtn);

  header.append(navBtns, breadcrumb, actions);

  const body = document.createElement('div');
  body.id = 'file-modal-body';
  body.textContent = 'Loading…';

  box.append(header, body, editFooter);
  if (!isInline) {
    modal.appendChild(box);
    document.body.appendChild(modal);
  }

  // ── Navigation ───────────────────────────────────────────────────────────────
  function navigate(newPath, newLine = null, newEndLine = null) {
    exitEditMode();
    _historyOpen = false;
    historyBtn.style.opacity = '';
    editFooter.hidden = true;
    navHistory.splice(historyIdx + 1);
    navHistory.push({ path: newPath, line: newLine, endLine: newEndLine });
    historyIdx = navHistory.length - 1;
    path = newPath; line = newLine; endLine = newEndLine;
    pathKind = path ? null : 'roots';
    pathIsText = false;
    updateNav();
    loadFile();
  }

  function updateNav() {
    backBtn.disabled = historyIdx === 0;
    fwdBtn.disabled = historyIdx === navHistory.length - 1;
    if (!path) {
      pathKind = 'roots';
      pathIsText = false;
      previewBtn.hidden = true;
      editBtn.hidden = true;
      historyBtn.hidden = true;
      copyBtn.hidden = true;
      breadcrumb.textContent = 'Files';
      return;
    }
    copyBtn.hidden = false;
    const isFile = pathKind === 'file';
    previewBtn.hidden = !isFile || !_isWebPreviewPath(path);
    const isText = isFile && (pathIsText || _isTextPath(path));
    editBtn.hidden = !isText;
    historyBtn.hidden = !isText;
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
      exitEditMode();
      historyIdx--;
      ({ path, line, endLine } = navHistory[historyIdx]);
      pathKind = path ? null : 'roots';
      pathIsText = false;
      updateNav(); loadFile();
    }
  });
  fwdBtn.addEventListener('click', () => {
    if (historyIdx < navHistory.length - 1) {
      exitEditMode();
      historyIdx++;
      ({ path, line, endLine } = navHistory[historyIdx]);
      pathKind = path ? null : 'roots';
      pathIsText = false;
      updateNav(); loadFile();
    }
  });
  previewBtn.addEventListener('click', () => {
    const ext = (path.split('.').pop() || '').toLowerCase();
    const params = { path };
    if (ext === 'md' || ext === 'markdown') params.render = '1';
    window.open('/localfile?' + new URLSearchParams(params), '_blank', 'noopener');
  });
  copyBtn.addEventListener('click', () => {
    navigator.clipboard?.writeText(path).then(() => {
      copyBtn.innerHTML = FV_ICON_CHECK;
      setTimeout(() => { copyBtn.innerHTML = FV_ICON_COPY; }, 1500);
    });
  });
  const closeModal = () => { if (!isInline) modal.remove(); _fvNavigate = null; };
  closeBtn.hidden = isInline;
  closeBtn.addEventListener('click', closeModal);
  if (!isInline) {
    modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
    const escHandler = e => { if (e.key === 'Escape') { closeModal(); document.removeEventListener('keydown', escHandler); } };
    document.addEventListener('keydown', escHandler);
  }

  // ── Edit mode ────────────────────────────────────────────────────────────────
  let _editOriginal = null;
  let _editFindPos = -1;
  let _editFindCleanup = null;
  let _editFindAnchor = null;

  function positionFindPopover(clientX = null, clientY = null) {
    if (!body._cmView || findPopover.hidden) return;
    const bodyRect = body.getBoundingClientRect();
    const popRect = findPopover.getBoundingClientRect();
    const gap = 10;
    const fallbackX = bodyRect.width - popRect.width - gap;
    const fallbackY = gap;
    let x = clientX == null ? fallbackX : clientX - bodyRect.left + gap;
    let y = clientY == null ? fallbackY : clientY - bodyRect.top + gap;
    x = Math.max(gap, Math.min(x, bodyRect.width - popRect.width - gap));
    y = Math.max(gap, Math.min(y, bodyRect.height - popRect.height - gap));
    findPopover.style.transform = `translate(${x}px, ${y}px)`;
  }

  function showFindPopover(clientX = null, clientY = null) {
    _editFindAnchor = clientX == null || clientY == null ? null : { clientX, clientY };
    if (!findPopover.contains(editTools)) findPopover.appendChild(editTools);
    if (!findPopover.isConnected) body.appendChild(findPopover);
    findPopover.hidden = false;
    requestAnimationFrame(() => positionFindPopover(clientX, clientY));
  }

  function showFindPopoverAtPos(pos) {
    const view = body._cmView;
    const coords = typeof view?.coordsAtPos === 'function' ? view.coordsAtPos(pos) : null;
    if (coords) showFindPopover(coords.right || coords.left, coords.bottom || coords.top);
    else showFindPopover();
  }

  function dockFindTools() {
    if (!editFooter.contains(editTools)) editFooter.insertBefore(editTools, cancelBtn);
    findPopover.hidden = true;
  }

  function teardownFindPopover() {
    if (_editFindCleanup) _editFindCleanup();
    _editFindCleanup = null;
    _editFindAnchor = null;
    dockFindTools();
  }

  function moveEditorToLine(lineNo, floatTools = false) {
    const view = body._cmView;
    if (!view) return;
    const doc = view.state.doc;
    const total = doc.lines || String(doc).split('\n').length;
    const target = Math.min(Math.max(parseInt(lineNo, 10) || 1, 1), total);
    const info = doc.line(target);
    view.dispatch({ selection: { anchor: info.from }, scrollIntoView: true });
    lineInput.value = String(target);
    editStatus.textContent = `Line ${target}`;
    if (floatTools) showFindPopoverAtPos(info.from);
    view.focus();
  }

  function findInEditor(dir = 1) {
    const view = body._cmView;
    const query = findInput.value;
    if (!view || !query) return;
    const text = view.state.doc.toString();
    const haystack = text.toLowerCase();
    const needle = query.toLowerCase();
    const current = view.state.selection?.main?.to ?? _editFindPos;
    let pos = dir < 0
      ? haystack.lastIndexOf(needle, Math.max(0, current - needle.length - 1))
      : haystack.indexOf(needle, Math.max(0, current));
    if (pos < 0) pos = dir < 0 ? haystack.lastIndexOf(needle) : haystack.indexOf(needle);
    if (pos < 0) {
      editStatus.textContent = 'No matches';
      return;
    }
    _editFindPos = pos;
    view.dispatch({ selection: { anchor: pos, head: pos + query.length }, scrollIntoView: true });
    const foundLine = view.state.doc.lineAt(pos).number;
    lineInput.value = String(foundLine);
    editStatus.textContent = `Match on line ${foundLine}`;
    showFindPopoverAtPos(pos);
    view.focus();
  }

  async function enterEditMode(text) {
    if (!window._cm) {
      editBtn.textContent = '…';
      const ok = await (window._cmPromise || Promise.resolve(false));
      editBtn.innerHTML = FV_ICON_PENCIL;
      if (!ok) {
        body.innerHTML = '';
        body.style.padding = '1rem';
        body.textContent = 'Editor unavailable — esm.sh could not be reached. Check your internet connection.';
        return;
      }
    }
    const { EditorView, EditorState, basicSetup, oneDark, atomOneDarkHighlight, LANGS } = window._cm;

    _editOriginal = text;
    body.innerHTML = '';
    body.style.display = 'flex';
    body.style.flexDirection = 'column';
    body.style.padding = '0';
    body.style.overflow = 'hidden';

    const ext = (path || '').split('.').pop().toLowerCase();
    const lang = LANGS[ext]?.();
    const extensions = [basicSetup, oneDark];
    if (atomOneDarkHighlight) extensions.push(atomOneDarkHighlight);
    if (lang) extensions.push(lang);

    const state = EditorState.create({ doc: text, extensions });
    const view = new EditorView({ state, parent: body });
    body._cmView = view;

    box.classList.add('fv-editing');
    editFooter.hidden = false;
    editBtn.hidden = true;
    historyBtn.hidden = true;
    editStatus.textContent = 'Editing';
    findInput.value = '';
    _editFindPos = -1;
    lineInput.value = line ? String(line) : '';
    saveBtn.disabled = false;
    const reposition = () => {
      if (_editFindAnchor) positionFindPopover(_editFindAnchor.clientX, _editFindAnchor.clientY);
      else positionFindPopover();
    };
    window.addEventListener('resize', reposition);
    _editFindCleanup = () => {
      window.removeEventListener('resize', reposition);
    };
    view.focus();
    if (line) moveEditorToLine(line);
  }

  function exitEditMode() {
    teardownFindPopover();
    if (body._cmView) { body._cmView.destroy(); body._cmView = null; }
    _editOriginal = null;
    box.classList.remove('fv-editing');
    _editFindPos = -1;
    body.style.display = '';
    body.style.flexDirection = '';
    body.style.padding = '';
    body.style.overflow = '';
    editFooter.hidden = true;
    const isText = pathKind === 'file' && path ? (pathIsText || _isTextPath(path)) : false;
    editBtn.hidden = !isText;
    historyBtn.hidden = !isText;
  }

  editBtn.addEventListener('click', async () => {
    const res = await fetch('/localfile?' + new URLSearchParams({ path, _t: Date.now() }));
    if (!res.ok) return;
    const text = await res.text();
    enterEditMode(text);
  });

  cancelBtn.addEventListener('click', () => {
    exitEditMode();
    loadFile();
  });

  findPrevBtn.addEventListener('click', () => findInEditor(-1));
  findNextBtn.addEventListener('click', () => findInEditor(1));
  findInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      findInEditor(e.shiftKey ? -1 : 1);
    }
  });
  findInput.addEventListener('input', () => { _editFindPos = -1; });
  lineGoBtn.addEventListener('click', () => moveEditorToLine(lineInput.value, true));
  lineInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      moveEditorToLine(lineInput.value, true);
    }
  });

  saveBtn.addEventListener('click', async () => {
    const view = body._cmView;
    if (!view) return;
    saveBtn.disabled = true;
    editStatus.textContent = 'Saving…';
    try {
      const res = await fetch('/localfile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, content: view.state.doc.toString() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Save failed');
      exitEditMode();
      loadFile();
    } catch (err) {
      editStatus.textContent = err.message || 'Save failed';
      saveBtn.disabled = false;
    }
  });

  // ── History panel ────────────────────────────────────────────────────────────
  let _historyOpen = false;

  async function openHistoryPanel() {
    _historyOpen = true;
    historyBtn.style.opacity = '1';
    body.innerHTML = 'Loading…';
    const res = await fetch('/localfile/history?' + new URLSearchParams({ path }));
    const data = await res.json();
    body.innerHTML = '';
    const panel = document.createElement('div');
    panel.className = 'fv-history-panel';
    const list = document.createElement('div');
    list.className = 'fv-history-list';
    if (!data.history?.length) {
      const empty = document.createElement('div');
      empty.className = 'fv-history-empty';
      empty.textContent = 'No edit history for this file.';
      list.appendChild(empty);
    } else {
      data.history.forEach(item => {
        const row = document.createElement('div');
        row.className = 'fv-history-item';
        const time = document.createElement('span');
        time.className = 'fv-history-time';
        time.textContent = item.edited_at.replace('T', ' ').replace('Z', ' UTC');
        const btn = document.createElement('button');
        btn.className = 'fv-history-revert-btn';
        btn.textContent = 'Revert to this';
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          btn.textContent = 'Reverting…';
          try {
            const r = await fetch('/localfile/revert-edit', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ edit_id: item.id }),
            });
            const d = await r.json();
            if (!r.ok) throw new Error(d.error || 'Revert failed');
            closeHistoryPanel();
          } catch (err) {
            btn.textContent = err.message || 'Failed';
          }
        });
        row.append(time, btn);
        list.appendChild(row);
      });
    }
    panel.appendChild(list);
    body.appendChild(panel);
  }

  function closeHistoryPanel() {
    _historyOpen = false;
    historyBtn.style.opacity = '';
    loadFile();
  }

  historyBtn.addEventListener('click', () => {
    if (_historyOpen) { closeHistoryPanel(); } else { openHistoryPanel(); }
  });

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

  function renderFileRoots(data) {
    body.innerHTML = '';
    const hint = document.createElement('p');
    hint.className = 'fv-roots-hint';
    hint.appendChild(document.createTextNode('Only directories listed in the YAML config are shown. '));
    const configLink = document.createElement('a');
    configLink.href = '#';
    configLink.textContent = 'Edit YAML config →';
    configLink.addEventListener('click', e => {
      e.preventDefault();
      if (!isInline) modal.remove();
      _fvNavigate = null;
      switchView('settings');
    });
    hint.appendChild(configLink);
    body.appendChild(hint);
    const roots = data.roots || [];
    if (!roots.length) {
      const empty = document.createElement('div');
      empty.className = 'fv-dir-empty';
      empty.textContent = 'No local file roots configured';
      body.appendChild(empty);
      return;
    }
    const list = document.createElement('div');
    list.className = 'fv-dir-listing fv-root-listing';
    roots.forEach(root => {
      const a = document.createElement('a');
      a.className = 'fv-dir-entry fv-dir-entry--dir';
      a.href = '/localfile?' + new URLSearchParams({ path: root });
      a.addEventListener('click', e => {
        e.preventDefault();
        e.stopPropagation();
        navigate(root);
      });
      const nameSpan = document.createElement('span');
      nameSpan.className = 'fv-dir-name';
      nameSpan.textContent = root;
      a.appendChild(nameSpan);
      list.appendChild(a);
    });
    body.appendChild(list);
  }

  async function loadFileRoots() {
    const rootsRes = await fetch('/config/localfile-roots');
    if (rootsRes.ok) return rootsRes.json();
    if (rootsRes.status !== 404) throw new Error('roots');
    const healthRes = await fetch('/health').catch(() => null);
    if (healthRes?.ok) {
      const health = await healthRes.json();
      if (health.squid_home) _squidHome = health.squid_home;
      return { roots: _squidHome ? [_squidHome] : [] };
    }
    throw new Error('Squid server API not available. Open the Squid server URL, not the static UI preview.');
  }

  async function loadFile() {
    body.textContent = 'Loading…';
    try {
      if (!path) {
        pathKind = 'roots';
        pathIsText = false;
        updateNav();
        renderFileRoots(await loadFileRoots());
        return;
      }
      const res = await fetch('/localfile?' + new URLSearchParams({ path, _t: Date.now() }));
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
        if (!isInline) { modal.remove(); _fvNavigate = null; }
        window.open('/localfile?' + new URLSearchParams({ path, _t: Date.now() }), '_blank');
        if (isInline) { body.textContent = 'Opened in new tab'; }
        return;
      }
      const text = await res.text();
      if (ct.includes('application/json')) {
        try {
          const data = JSON.parse(text);
          if (data.type === 'directory') {
            pathKind = 'directory';
            pathIsText = false;
            if (data.path !== path) path = data.path;
            updateNav();
            _renderDirListing(body, data);
            return;
          }
        } catch {}
      }
      pathKind = 'file';
      pathIsText = ct.includes('text/') || ct.includes('application/json');
      updateNav();
      _renderFileViewer(body, text, line, endLine, path, changedLines);
    } catch (err) {
      body.textContent = err?.message || 'Failed to load file.';
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
      a.addEventListener('click', e => {
        e.preventDefault();
        e.stopPropagation();
        if (_fvNavigate) _fvNavigate(entry.path);
        else openFileViewer(entry.path);
      });

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

function _renderFileViewer(container, text, targetLine, endLine, path, changedLines = null) {
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
    const isChanged = changedLines?.has(n);
    const inRange = !changedLines && targetLine && n >= targetLine && n <= (endLine || targetLine);
    const row = document.createElement('div');
    row.className = 'fv-line' + (isChanged ? ' fv-changed' : inRange ? ' fv-target' : '');
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
updateComposerActionTitles();
updateActiveQuotaGauge();
initMobileViewNavigation();
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
// When the user switches away and back, keep following the current streaming
// state only if the user was already reading at the bottom.
let _messagesAtBottomBeforeHide = true;
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    _messagesAtBottomBeforeHide = isAtBottom();
  } else {
    if (_messagesAtBottomBeforeHide) {
      messages.scrollTop = messages.scrollHeight;
    }
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
    const claude_org_id = decodeURIComponent(parts[1]);
    const claude_session_key = decodeURIComponent(parts[2]);
    fetch('/config/creds', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ claude_org_id, claude_session_key }),
    })
      .then(r => r.json())
      .then(d => _showImportResult('quota-creds-popup', 'creds-status', !!d.ok))
      .catch(() => _showImportResult('quota-creds-popup', 'creds-status', false));
  } else if (type === 'claude-org' && parts.length >= 2) {
    const claude_org_id = decodeURIComponent(parts[1]);
    const orgInput = document.getElementById('creds-org');
    const popup = document.getElementById('quota-creds-popup');
    const status = document.getElementById('creds-status');
    if (orgInput) orgInput.value = claude_org_id;
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
