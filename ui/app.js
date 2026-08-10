
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
    // doRefresh() already reloads once it deregisters the old worker; the fresh
    // worker claiming control here would otherwise trigger a second reload.
    if (sessionStorage.getItem('squid_skip_sw_reload')) {
      sessionStorage.removeItem('squid_skip_sw_reload');
      return;
    }
    window.location.reload();
  });
}

messages.addEventListener('scroll', () => {
  updateScrollButtonVisibility();
});
scrollBtn.addEventListener('click', () => {
  if (historyWindowMode) {
    resetHistoryToLatest();
    return;
  }
  messages.scrollTop = messages.scrollHeight;
  updateScrollButtonVisibility();
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

function escapeMarkdownTildes(text) {
  const src = String(text || '');
  let inFence = false;
  let fenceChar = '';
  let fenceLen = 0;
  let out = '';

  for (const line of src.match(/[^\n]*(?:\n|$)/g) || []) {
    if (!line) continue;
    const fence = line.match(/^[ \t]{0,3}(`{3,}|~{3,})/);
    const isFenceLine = !!fence && (!inFence || (fence[1][0] === fenceChar && fence[1].length >= fenceLen));
    const protectLine = inFence || isFenceLine;

    if (isFenceLine) {
      if (inFence) {
        inFence = false;
      } else {
        inFence = true;
        fenceChar = fence[1][0];
        fenceLen = fence[1].length;
      }
    }

    if (protectLine) {
      out += line;
      continue;
    }

    let i = 0;
    let inlineTicks = 0;
    while (i < line.length) {
      const ch = line[i];
      if (ch === '`') {
        let j = i + 1;
        while (line[j] === '`') j++;
        const runLen = j - i;
        if (!inlineTicks) inlineTicks = runLen;
        else if (runLen === inlineTicks) inlineTicks = 0;
        out += line.slice(i, j);
        i = j;
      } else {
        out += ch === '~' && !inlineTicks ? '\\~' : ch;
        i++;
      }
    }
  }

  return out;
}

function renderAssistantMarkdown(content) {
  return marked.parse(escapeMarkdownTildes(content));
}

const HARNESS_MODEL_HINTS = Object.freeze({
  claudecode: 'e.g. claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-7',
  codex:      'e.g. o4-mini, o3',
  cursor:     'model (optional)',
  opencode:   'e.g. opencode/deepseek-v4-flash-free, anthropic/claude-sonnet-4-6',
});

const AGENT_THEME_COLORS = Object.freeze({
  claude: '#AE5332',
  codex: '#7070a0',
  cursor: '#FFFFFF',
  opencode: '#CFCECD',
  deepseek: '#4d9de0',
  kimi: '#4d6bfe',
  antigravity: '#4ea1ff',
  copilot: '#ff5db1',
  default: '#888888',
});

let _backendMetadata = {};
let _harnessMetadata = {};
let _providerMetadata = {};

// ── update notice (ADR-0030) ─────────────────────────────────────────────────
const UPDATE_CHECK_URL = 'https://pypi.org/pypi/agentsquid/json';
const UPDATE_CACHE_KEY = 'squid_update_check_cache';
const UPDATE_CACHE_TTL_MS = 24 * 60 * 60 * 1000;
const UPDATE_DISMISS_KEY = 'squid_update_dismissed_version';
let _updateInfo = null; // { current, latest } once a newer version is confirmed available
let _updatesInstallOnRestart = 'ask';
let _canInstallOnRestart = false;
let _squidVersion = null;

function _parseVersion(v) {
  const match = String(v).trim().match(/^v?(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?/i);
  if (!match) return null;
  return {
    release: match[1].split('.').map(n => parseInt(n, 10) || 0),
    preType: match[2]?.toLowerCase() || null,
    preNum: match[3] ? parseInt(match[3], 10) || 0 : null,
    postNum: match[4] ? parseInt(match[4], 10) || 0 : null,
  };
}

function _isNewerVersion(latest, current) {
  const a = _parseVersion(latest), b = _parseVersion(current);
  if (!a || !b) return false;
  if (a.preType && !b.preType) return false;
  for (let i = 0; i < Math.max(a.release.length, b.release.length); i++) {
    const x = a.release[i] || 0, y = b.release[i] || 0;
    if (x !== y) return x > y;
  }
  const preRank = { a: 0, b: 1, rc: 2 };
  if (a.preType || b.preType) {
    if (!a.preType) return true;
    if (!b.preType) return false;
    if (preRank[a.preType] !== preRank[b.preType]) return preRank[a.preType] > preRank[b.preType];
    if (a.preNum !== b.preNum) return a.preNum > b.preNum;
  }
  if (a.postNum !== b.postNum) return (a.postNum ?? -1) > (b.postNum ?? -1);
  return false;
}

async function _fetchLatestSquidVersion({ force = false } = {}) {
  try {
    const cached = JSON.parse(localStorage.getItem(UPDATE_CACHE_KEY) || 'null');
    if (!force && cached && Date.now() - cached.checkedAt < UPDATE_CACHE_TTL_MS) return cached.version;
  } catch {}
  try {
    const res = await fetch(UPDATE_CHECK_URL);
    if (!res.ok) return null;
    const data = await res.json();
    const version = data?.info?.version;
    if (!version) return null;
    localStorage.setItem(UPDATE_CACHE_KEY, JSON.stringify({ version, checkedAt: Date.now() }));
    return version;
  } catch {
    return null;
  }
}

function setUpdateAvailable(info) {
  _updateInfo = info;
  const hasUpdate = !!info;
  document.getElementById('hamburger-btn')?.classList.toggle('has-update', hasUpdate);
  document.querySelector('.hmenu-item[data-view="settings"]')?.classList.toggle('has-update', hasUpdate);
  document.getElementById('hmenu-restart')?.classList.toggle('has-update', hasUpdate && _canInstallOnRestart);
  renderSettingsUpdateNotice();
}

function renderSettingsUpdateNotice() {
  const el = document.getElementById('settings-update-notice');
  if (!el) return;
  if (!_updateInfo) { el.hidden = true; return; }
  el.hidden = false;
  document.getElementById('settings-update-text').textContent =
    _canInstallOnRestart
      ? `AgentSquid v${_updateInfo.current} → v${_updateInfo.latest} available — Restart Server can upgrade before restarting`
      : `AgentSquid v${_updateInfo.current} → v${_updateInfo.latest} available`;
  document.getElementById('settings-update-cmd').textContent = 'pipx upgrade agentsquid';
}

function renderSettingsVersion(version) {
  const el = document.getElementById('settings-version-info');
  if (!el) return;
  el.textContent = version ? `v${version}` : '';
}

function updateSettingsFromHealth(health) {
  _squidVersion = health?.version || null;
  renderSettingsVersion(_squidVersion);
  const updates = health?.updates || {};
  const mode = updates.install_on_restart;
  _updatesInstallOnRestart = ['ask', 'always', 'never'].includes(mode) ? mode : 'ask';
  _canInstallOnRestart = updates.can_install_on_restart === true;
  document.getElementById('hmenu-restart')?.classList.toggle('has-update', !!_updateInfo && _canInstallOnRestart);
  renderSettingsUpdateNotice();
}

async function checkForSquidUpdate(currentVersion, { force = false } = {}) {
  if (!currentVersion) return { checked: false };
  const latest = await _fetchLatestSquidVersion({ force });
  if (!latest || !_isNewerVersion(latest, currentVersion)) {
    setUpdateAvailable(null);
    return { checked: true, latest, hasUpdate: false };
  }
  if (!force && localStorage.getItem(UPDATE_DISMISS_KEY) === latest) {
    setUpdateAvailable(null);
    return { checked: true, latest, hasUpdate: false, dismissed: true };
  }
  setUpdateAvailable({ current: currentVersion, latest });
  return { checked: true, latest, hasUpdate: true };
}

function setUpdateCheckButtonText(btn, text) {
  const label = btn.querySelector('.settings-update-check-label');
  if (label) label.textContent = text;
  else btn.textContent = text;
  btn.title = text;
  btn.setAttribute('aria-label', text);
}

function setUpdateCheckButtonIcon(btn, icon) {
  const el = btn.querySelector('.material-symbols-outlined');
  if (el) el.textContent = icon;
}

async function forceCheckForSquidUpdate() {
  const btn = document.getElementById('settings-update-check');
  if (!btn) return;
  const original = btn.querySelector('.settings-update-check-label')?.textContent || btn.textContent || 'Check Updates';
  const originalIcon = btn.querySelector('.material-symbols-outlined')?.textContent || 'deployed_code_update';
  btn.disabled = true;
  btn.classList.remove('is-success');
  setUpdateCheckButtonText(btn, 'Checking...');
  setUpdateCheckButtonIcon(btn, originalIcon);
  try {
    const result = await checkForSquidUpdate(_squidVersion, { force: true });
    setUpdateCheckButtonText(btn, result.hasUpdate ? 'Update Found' : 'Up to Date');
    btn.classList.add('is-success');
    setUpdateCheckButtonIcon(btn, 'check_circle');
  } catch {
    setUpdateCheckButtonText(btn, 'Check Failed');
  } finally {
    setTimeout(() => {
      setUpdateCheckButtonText(btn, original || 'Check Updates');
      setUpdateCheckButtonIcon(btn, originalIcon);
      btn.classList.remove('is-success');
      btn.disabled = false;
    }, 1500);
  }
}

function splitAgentRef(ref, provider = null) {
  if (provider) return { harness: ref || '', provider };
  const raw = String(ref || '');
  const idx = raw.indexOf(':');
  if (idx >= 0) return { harness: raw.slice(0, idx), provider: raw.slice(idx + 1) || null };
  const aliases = { claude: 'claudecode' };
  return { harness: aliases[raw] || raw, provider: null };
}

function agentBackendRef(agent) {
  if (!agent) return null;
  if (agent.backend) return agent.backend;
  const harness = agent.harness || '';
  return agent.provider ? `${harness}:${agent.provider}` : harness;
}

function runtimeRef(harness, provider = null) {
  return provider ? `${harness}:${provider}` : harness;
}

function providerMetadataForBackend(backend) {
  if (_providerMetadata[backend]) return _providerMetadata[backend]; // backend is already a bare provider id
  const provider = _backendMetadata[backend]?.provider || splitAgentRef(backend).provider;
  return provider ? _providerMetadata[provider] : null;
}

function agentThemeColor(backend) {
  const configured = _backendMetadata[backend]?.color || providerMetadataForBackend(backend)?.color;
  if (configured) return configured;
  return AGENT_THEME_COLORS[(backend || '').toLowerCase()] || AGENT_THEME_COLORS.default;
}

function agentSlugColor(agent, backendFallback = null) {
  const config = (_agentsCache || []).find(a => a.name === agent);
  if (config?.color) return config.color;
  if (config?.provider_color) return config.provider_color;
  if (config?.provider && _providerMetadata[config.provider]?.color) return _providerMetadata[config.provider].color;
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

function mergeAgentCache(agents) {
  if (!Array.isArray(agents)) return _agentsCache || [];
  const merged = new Map((_agentsCache || []).map(agent => [agent.name, agent]));
  for (const agent of agents) {
    if (!agent?.name) continue;
    const normalized = { ...agent, backend: agentBackendRef(agent) };
    merged.set(agent.name, { ...(merged.get(agent.name) || {}), ...normalized });
  }
  _agentsCache = Array.from(merged.values());
  refreshAgentSlugColors();
  return _agentsCache;
}

function quotaGaugeColor(backend) {
  return agentThemeColor(backend);
}

function backendDisplayName(backend) {
  return _backendMetadata[backend]?.label || providerMetadataForBackend(backend)?.label || backend || 'Agent';
}

function backendModelHint(backend) {
  const harness = _backendMetadata[backend]?.harness || backend;
  return HARNESS_MODEL_HINTS[harness] || 'model (optional)';
}

// Suggestions only — provider.models is a UI convenience list, never
// enforced server-side, so any model string a user types is accepted.
function backendModelSuggestions(backend) {
  return providerMetadataForBackend(backend)?.models || [];
}


// Populates the compact "pick a known model" <select> next to a freeform
// model <input>. Choosing an option fills the input; the picker itself
// always resets to its placeholder so it never becomes the source of truth.
function populateModelPicker(pickerId, inputId, backend) {
  const picker = document.getElementById(pickerId);
  const input = document.getElementById(inputId);
  if (!picker) return;
  const models = backendModelSuggestions(backend);
  if (!models.length) {
    picker.hidden = true;
    picker.innerHTML = '';
    return;
  }
  picker.hidden = false;
  picker.innerHTML = '<option value="" selected disabled hidden></option>' +
    models.map(m => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join('');
  picker.onchange = () => {
    if (picker.value && input) {
      input.value = picker.value;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
    picker.value = '';
  };
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
const MOBILE_VIEW_ORDER = ['chat', 'files', 'topics', 'agents', 'stats', 'community', 'settings', 'flow'];
const VIEW_LABELS = {
  chat: 'Chat',
  files: 'Files',
  topics: 'Topics',
  agents: 'Agents',
  stats: 'Stats',
  community: 'Community',
  settings: 'Settings',
  flow: 'Squid Flow',
};
let _mobileViewHistoryDepth = 0;
let _mobileViewHistorySkip = 0;

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
    document.querySelectorAll('#quota-creds-popup, #codex-creds-popup, #cursor-creds-popup, #balance-max-popup')
      .forEach(popup => popup.classList.remove('open'));
  }
  if (name === 'files') openFilesTabView();
  if (name === 'topics') loadTopicsView();
  if (name === 'stats') loadStats();
  if (name === 'agents') loadAgents();
  if (name === 'settings') loadConfigYaml();
  if (name === 'flow') initFlowView();
}

function navigateView(name, { recordHistory = true } = {}) {
  if (name === currentView) return;
  if (recordHistory && name === 'chat' && isMobileViewport() && _mobileViewHistoryDepth > 0 && history.go) {
    const depth = _mobileViewHistoryDepth;
    _mobileViewHistorySkip = depth;
    _mobileViewHistoryDepth = 0;
    switchView(name);
    history.go(-depth);
    return;
  }
  switchView(name);
  if (recordHistory && isMobileViewport() && history.pushState) {
    history.pushState({ squidView: name }, '', location.href);
    _mobileViewHistoryDepth += 1;
  }
}

function navigateViewFromHistoryAnchor(anchorName, name) {
  if (name === currentView) return;
  if (name === 'chat' && isMobileViewport() && _mobileViewHistoryDepth > 0 && history.go) {
    const depth = _mobileViewHistoryDepth;
    _mobileViewHistorySkip = depth;
    _mobileViewHistoryDepth = 0;
    switchView(name);
    history.go(-depth);
    return;
  }
  if (history.replaceState && history.pushState) {
    const state = (history.state && typeof history.state === 'object') ? history.state : {};
    if (state.squidView !== anchorName) {
      history.replaceState({ ...state, squidView: anchorName }, '', location.href);
    }
    switchView(name);
    history.pushState({ squidView: name }, '', location.href);
    if (isMobileViewport()) _mobileViewHistoryDepth += 1;
    return;
  }
  switchView(name);
}

function initMobileViewNavigation() {
  if (history.replaceState) history.replaceState({ squidView: currentView }, '', location.href);

  window.addEventListener('popstate', e => {
    if (_fvHandlePopState?.(e)) return;
    const name = e.state?.squidView || 'chat';
    if (_mobileViewHistorySkip > 0) {
      _mobileViewHistorySkip = 0;
    } else {
      _mobileViewHistoryDepth = Math.max(0, _mobileViewHistoryDepth - 1);
    }
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
    }
  });

  app.addEventListener('pointercancel', () => { swipeStart = null; });
}

async function doRefresh() {
  /* Clear all Cache API caches, unregister service workers, then reload. */
  try {
    sessionStorage.setItem('squid_skip_sw_reload', '1');
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
    if (helpPanel.classList.contains('open')) closeHelp();
    const open = hamburgerMenu.classList.toggle('open');
    hamburgerBtn.classList.toggle('active', open);
  });
  document.querySelectorAll('.hmenu-item').forEach(btn =>
    btn.addEventListener('click', () => {
      if (btn.id === 'hmenu-refresh' || btn.id === 'hmenu-restart') return;
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
  document.getElementById('hmenu-restart')?.addEventListener('click', e => {
    e.stopPropagation();
    hamburgerMenu.classList.remove('open');
    hamburgerBtn.classList.remove('active');
    restartServer();
  });
  document.getElementById('settings-update-copy')?.addEventListener('click', e => {
    const btn = e.currentTarget;
    const cmd = document.getElementById('settings-update-cmd').textContent;
    navigator.clipboard.writeText(cmd).then(() => {
      btn.textContent = 'copied';
      setTimeout(() => { btn.textContent = 'copy'; }, 1500);
    });
  });
  document.getElementById('settings-update-check')?.addEventListener('click', forceCheckForSquidUpdate);
  document.getElementById('settings-update-dismiss')?.addEventListener('click', () => {
    if (_updateInfo) localStorage.setItem(UPDATE_DISMISS_KEY, _updateInfo.latest);
    setUpdateAvailable(null);
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
      not_installed: 'Tailscale is not installed.\nInstall from tailscale.com, then restart squid to configure remote access automatically.',
      not_running:   'Tailscale is installed but not running.\nStart the Tailscale app, then restart squid.',
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
const _sessionTurnCountsByRoute = {}; // `${topic}@${agent}` → known turn count, including cleared 0
const _routeTurnCountRefreshAt = {}; // `${topic}@${agent}` → last refresh timestamp
const sessionAdvisoryEl    = document.getElementById('session-advisory');
const sessionAdvisoryMsgEl = document.getElementById('session-advisory-msg');
let _advisoryTurnCount = 0;
let _advisoryDismissKey = null;
const _memoryInjectedInto = {}; // `${topic}@${agent|_}` → topic memory revision sent to the current session
let _agentsCache = null;
let _agentsCachePromise = null;
let _squidHome = '/tmp/squid'; // updated from /health on first loadAgents()
let agentTableSort = { key: 'runtime', dir: 'asc' };
let agentFilterQuery = '';
let _activePollImmediate = null; // fn to trigger an immediate status poll for the active stream

function clearCachedSessionId(topic, agent) {
  const taKey = `${topic}@${agent || '_'}`;
  const sid = _sessionIds[taKey];
  if (sid) {
    const inj = getInjectedInto();
    delete inj[sid];
    setInjectedInto(inj);
    const attached = getAttachedFilesInSession();
    delete attached[sid];
    setAttachedFilesInSession(attached);
    delete _sessionTurnCounts[sid];
  }
  delete _sessionIds[taKey];
  delete _memoryInjectedInto[taKey];
  delete _pendingSessionMemoryRevisions[taKey];
  delete _pendingSessionInjectedIds[taKey];
  delete _pendingSessionAttachedFiles[taKey];
  delete _sessionLookupCache[taKey];
  if (agent) delete _sessionLookupCache[`${topic}@${agent}`];
  if (agent) delete _sessionTurnCountsByRoute[`${topic}@${agent}`];
  delete _memorySelectionOverrides[_memoryOverrideKey(topic, agent, false)];
}

function _setKnownSessionTurnCount(topic, agent, count, sessionId = null) {
  if (!agent || count == null) return;
  const n = parseInt(count, 10) || 0;
  _sessionTurnCountsByRoute[`${topic}@${agent}`] = n;
  if (sessionId) _sessionTurnCounts[sessionId] = n;
}

// ── topic chip ────────────────────────────────────────────────────────────────

const topicChipEl = document.getElementById('topic-chip');
const chipRow = document.getElementById('chip-row');
const chipFilterBtn = document.getElementById('chip-filter-btn');
const chipSearchBtn = document.getElementById('chip-search-btn');
const chipClearBtn = document.getElementById('chip-clear-btn');
const chipStashBtn = document.getElementById('chip-stash-btn');
let stickyChip = null; // { topic, agent, adhoc, chainTarget?, chainTargetFresh?, route? } | null
let editingExpandedSlug = false;
let expandedSlugEditToken = 0;
let composerActionTitleSeq = 0;
const TOPIC_SLUG_SRC = '[A-Za-z0-9_]+(?:\\.[A-Za-z0-9_]+)*';
const TOPIC_SLUG_PARTIAL_SRC = '[A-Za-z0-9_]*(?:\\.[A-Za-z0-9_]*)*';
const AGENT_SLUG_SRC = '\\w+';
const AGENT_SLUG_PARTIAL_SRC = '\\w*';

function _chainRouteText(topic, originAgent, targetAgent, targetFresh = false, originFresh = false, operator = '>', targetTopic = null) {
  const targetPrefix = targetTopic && targetTopic !== topic ? `#${targetTopic}` : '';
  return `#${topic}@${originAgent}${originFresh ? '!' : ''}${operator}${targetPrefix}@${targetAgent}${targetFresh ? '!' : ''}`;
}

function _parseSquidFlow(route) {
  const text = String(route || '').trim().replace(/\s+/g, '');
  if (!text || !window.SquidFlow || typeof window.SquidFlow.parse !== 'function') return null;
  const result = window.SquidFlow.parse(text);
  return result && result.ok ? result : null;
}

function _isLiveFlowChain(result) {
  if (!result || !Array.isArray(result.branches) || !result.branches.length) return false;
  return result.branches.every(branch => {
    const steps = branch.steps || [];
    if (steps.length !== 2) return false;
    const origin = steps[0];
    const next = steps[1];
    if (origin.kind !== 'atom' && origin.kind !== 'join') return false;
    if (next.kind === 'roundtrip') return next.rounds >= 1;
    // 'oneway' isn't a separate op.type — '>'/'=>' are the count=1/wait=null
    // case of 'scheduled' (see ADR-0032, "Edge types").
    return next.kind === 'atom' && next.via && next.via.type === 'scheduled' && !next.via.unbounded && next.via.count >= 1;
  });
}

function parseRouteChain(route) {
  const result = _parseSquidFlow(route);
  if (!result || !_isLiveFlowChain(result)) return null;
  const firstBranch = result.branches[0];
  const originStep = firstBranch.steps[0];
  const nextStep = firstBranch.steps[1];
  const origins = originStep.kind === 'join'
    ? originStep.atoms
    : result.branches.map(b => b.steps[0].atom);
  // A round-trip step's target is always an array (its own joined group for
  // a target-side join, or a single-atom array otherwise) — flatten same as
  // origin-side join already does via originStep.atoms above.
  const targets = result.branches.flatMap(b => {
    const step = b.steps[1];
    return step.kind === 'roundtrip' ? step.target : [step.atom];
  });
  const origin = origins[0];
  const target = targets[0];
  let operator = '>';
  let rounds = 0;
  if (nextStep.kind === 'roundtrip') {
    rounds = nextStep.rounds || 1;
    operator = rounds === 1 && !nextStep.wait ? '<>' : `<${rounds}${nextStep.wait ? ':' + nextStep.wait : ''}>`;
  } else if (nextStep.via?.type === 'scheduled') {
    // Shortest spelling for the (count, wait) pair — '>' when both are
    // default, mirroring roundtrip's '<>' preference over '<1>' above.
    const { count, wait } = nextStep.via;
    operator = count === 1 && !wait ? '>' : `=${count === 1 ? '' : count}${wait ? ':' + wait : ''}>`;
  }
  const routeText = result.key || String(route || '').trim().replace(/\s+/g, '');
  // Deliberately raw counts, not deduped by resolved value: a rolling-anchor
  // bare atom (e.g. `#t@a!,@a!`) can resolve to the exact same
  // (topic, agent, fresh) as its sibling while still being two independent
  // dispatches (two separate turns/sessions) — the send path fans out on
  // origins.length/targets.length (see the flowOrigins.length > 1 check),
  // so the chip must call it "multi" on the same basis or the two disagree.
  return {
    topic: origin.topic,
    origin: origin.agent,
    originFresh: !!origin.fresh,
    operator,
    rounds,
    targetTopic: target.topic,
    target: target.agent,
    targetFresh: !!target.fresh,
    origins,
    targets,
    join: originStep.kind === 'join',
    targetJoin: nextStep.kind === 'roundtrip' && !!nextStep.join,
    fanout: targets.length > 1,
    multiOrigin: origins.length > 1,
    complex: originStep.kind === 'join' || targets.length > 1 || origins.length > 1 || /^=\d/.test(operator) || rounds > 1 || operator.includes(':'),
    route: routeText,
  };
}

// Origin Broadcast (ADR-0032): a bare, comma-separated origin list with no
// operator. Each listed origin receives the literal prompt independently —
// no chain envelope, no downstream step. `#topic@a,@b` == sending `#topic@a`
// and `#topic@b` separately, grouped under one flow_run_id for display only.
//
// Atoms may each be a full `#topic@agent`, a bare `@agent`, or a bare
// `#topic` — whichever half an atom omits is inherited from its nearest
// fully-explicit ancestor, not a single anchor fixed for the whole list: the
// first fully-explicit atom seeds the root, but every later atom that is
// itself fully explicit supersedes it for everything after (see ADR-0032,
// "Within an origin list...").
//
// Stored/displayed in *reduced* form, not fully-explicit: greedy max-coverage
// dominating-set grouping, ported from ui/flow-lang.js's minimalGroupedText
// (see ADR-0032, "Canonical Key (Storage/Dedup Identity)"). Repeatedly pick
// whichever remaining agent would "cover" the most other remaining ones (two
// atoms cover each other if they share a topic or an agent); that atom
// anchors a run, written in full, and every atom it covers joins the run,
// each dropping whichever one field it shares with the anchor. This beats a
// single fixed sort axis (e.g. topic-only) whenever agents repeat more than
// topics do, or vice versa, and can mix which field drops within one run —
// something a single-axis sort can never do. Ties break on ascending
// (topic, agent) so the result is a pure function of the resolved agent set,
// same set in, same text out — this doesn't reorder the *dispatch*, only the
// stored/displayed route text (each independent /chat send still goes out in
// `agents`' original order).
function _broadcastRouteText(agents) {
  let remaining = agents.slice();
  const runs = [];
  while (remaining.length) {
    let anchor = null;
    let anchorCover = null;
    for (const cand of remaining) {
      const cover = remaining.filter(a => a.topic === cand.topic || a.agent === cand.agent);
      const better = !anchor || cover.length > anchorCover.length ||
        (cover.length === anchorCover.length &&
          (cand.topic < anchor.topic ||
            (cand.topic === anchor.topic && cand.agent < anchor.agent)));
      if (better) { anchor = cand; anchorCover = cover; }
    }
    runs.push({ anchor, members: anchorCover });
    const covered = new Set(anchorCover);
    remaining = remaining.filter(a => !covered.has(a));
  }
  runs.sort((r1, r2) =>
    r1.anchor.topic < r2.anchor.topic ? -1 : r1.anchor.topic > r2.anchor.topic ? 1
      : r1.anchor.agent < r2.anchor.agent ? -1 : r1.anchor.agent > r2.anchor.agent ? 1 : 0);
  const parts = [];
  for (const run of runs) {
    const a0 = run.anchor;
    parts.push(`#${a0.topic}@${a0.agent}${a0.fresh ? '!' : ''}`);
    for (const a of run.members) {
      if (a === a0) continue;
      const fresh = a.fresh ? '!' : '';
      parts.push(a.topic === a0.topic ? `@${a.agent}${fresh}` : `#${a.topic}${fresh}`);
    }
  }
  return parts.join(',');
}

const _BROADCAST_ATOM_RE = new RegExp(`^(?:#(${TOPIC_SLUG_SRC}))?(?:@(${AGENT_SLUG_SRC}))?(!?)$`);
// Loose detector for "does this text look like a broadcast route" — the
// first atom must be a full #topic@agent, later atoms may each drop either
// half (or both stay, or a bare topic switch, etc.); parseOriginBroadcast
// does the real validation. Shared so every call site that needs to peel a
// broadcast route off the front of raw composer text stays in sync.
const _BROADCAST_ROUTE_DETECT_SRC = `#${TOPIC_SLUG_SRC}@${AGENT_SLUG_SRC}!?(?:,(?:#${TOPIC_SLUG_SRC})?(?:@${AGENT_SLUG_SRC})?!?)+`;

// Core rolling-anchor resolver, shared by parseOriginBroadcast (full-route
// validation) and the composer autocomplete (which wants the same walk over
// however many atoms are typed so far, even just one, to know what an
// about-to-be-typed trailing atom would currently inherit).
function _resolveBroadcastAtoms(rawAtoms) {
  const parsedAtoms = [];
  for (const raw of rawAtoms) {
    const m = raw.match(_BROADCAST_ATOM_RE);
    if (!m || (!m[1] && !m[2])) return null; // each atom needs at least a topic or an agent
    parsedAtoms.push({ topic: m[1] ? m[1].toLowerCase() : null, agent: m[2] || null, fresh: !!m[3] });
  }
  const firstExplicit = parsedAtoms.find(a => a.topic && a.agent);
  if (!firstExplicit) return null; // at least one atom must be a full #topic@agent

  let root = { topic: firstExplicit.topic, agent: firstExplicit.agent };
  return parsedAtoms.map(a => {
    if (a.topic && a.agent) root = { topic: a.topic, agent: a.agent }; // supersedes root from here on
    return { topic: a.topic || root.topic, agent: a.agent || root.agent, fresh: a.fresh };
  });
}

function parseOriginBroadcast(route) {
  const text = String(route || '').trim();
  if (!text.startsWith('#')) return null;
  if (_parseSquidFlow(text)?.branches?.some(b => (b.steps || []).length > 1)) return null;
  const rawAtoms = text.split(',');
  if (rawAtoms.length < 2) return null;
  const agents = _resolveBroadcastAtoms(rawAtoms);
  if (!agents) return null;
  return { topic: agents[0].topic, agents, route: _broadcastRouteText(agents) };
}

function setTopicChip(topic, agent, adhoc = false, lookback = 0, opts = {}) {
  editingExpandedSlug = false;
  expandedSlugEditToken++;
  const chainTarget = opts.chainTarget || null;
  const chainTargetFresh = !!opts.chainTargetFresh;
  const chainOperator = opts.chainOperator || '>';
  const chainRounds = opts.chainRounds || (chainOperator === '<>' ? 1 : 0);
  const chainTargetTopic = opts.chainTargetTopic || null;
  const broadcastAgents = opts.broadcastAgents || null;
  const flowOrigins = opts.flowOrigins || null;
  const suppressTurnCount = !!opts.suppressTurnCount;
  const route = broadcastAgents
    ? (opts.route || _broadcastRouteText(broadcastAgents))
    : (chainTarget && agent
      ? (opts.route || _chainRouteText(topic, agent, chainTarget, chainTargetFresh, adhoc, chainOperator, chainTargetTopic))
      : null);
  const parsedRoute = route ? parseRouteChain(route) : null;
  stickyChip = {
    topic, agent, adhoc, lookback, suppressTurnCount,
    ...(route
      ? (broadcastAgents ? { route, broadcastAgents } : { route, chainTarget, chainTargetFresh, chainOperator, chainRounds, chainTargetTopic, flowOrigins })
      : {}),
  };
  _advisoryTurnCount = 0;
  // Don't persist a sessioned default chip — #default is adhoc-first; session there is ephemeral
  if (topic !== 'default' || adhoc) {
    const { suppressTurnCount: _omit, ...storedChip } = stickyChip;
    localStorage.setItem('squid_sticky_chip', JSON.stringify(storedChip));
  }

  topicChipEl.innerHTML = '';
  topicChipEl.classList.toggle('route-chain', !!route && !broadcastAgents);
  topicChipEl.classList.toggle('origin-broadcast', !!broadcastAgents);
  if (!broadcastAgents && !(route && parsedRoute?.complex)) {
    const tSpan = document.createElement('span');
    tSpan.className = 'chip-topic';
    tSpan.textContent = '#' + topic;
    topicChipEl.appendChild(tSpan);
  }
  if (broadcastAgents) {
    // Origin Broadcast (ADR-0032): N independent origins, no chain envelope —
    // just a comma-joined origin list, no arrow, no single "agent" identity,
    // and (unlike a single-topic broadcast) no single "topic" identity
    // either — each origin shows its own #topic when it differs from the
    // one before it, and its own @agent when it differs from the one before
    // it (rolling-anchor resolution already filled in every origin's
    // topic/agent, this just avoids repeating an unchanged field — matching
    // the literal typed shorthand, e.g. `#t1@echo,#t2` stays that way
    // instead of spelling out as `#t1@echo,#t2@echo`).
    let lastTopic = null;
    let lastAgent = null;
    broadcastAgents.forEach((a, i) => {
      if (i > 0) {
        const sepSpan = document.createElement('span');
        sepSpan.className = 'chip-broadcast-sep';
        sepSpan.textContent = ',';
        topicChipEl.appendChild(sepSpan);
      }
      const topicChanged = a.topic !== lastTopic;
      if (topicChanged) {
        const tSpan2 = document.createElement('span');
        tSpan2.className = 'chip-topic';
        tSpan2.textContent = '#' + a.topic;
        topicChipEl.appendChild(tSpan2);
        lastTopic = a.topic;
      }
      // Drop @agent only when the #topic just rendered already identifies
      // this atom — if neither field changed (e.g. a repeated `!` for
      // freshness), the agent must still render or the atom is left with no
      // identifying token at all (see _broadcastRouteText/minimalGroupedText,
      // which never drop both fields for the same reason).
      if (a.agent !== lastAgent || !topicChanged) {
        const aSpan = document.createElement('span');
        aSpan.className = 'chip-agent chip-broadcast-agent';
        aSpan.textContent = '@' + a.agent;
        setAgentSlugColor(aSpan, a.agent);
        topicChipEl.appendChild(aSpan);
        lastAgent = a.agent;
      }
      if (a.fresh) {
        const freshSpan = document.createElement('span');
        freshSpan.className = 'chip-adhoc';
        freshSpan.textContent = '!';
        setAgentSlugColor(freshSpan, a.agent);
        topicChipEl.appendChild(freshSpan);
      }
    });
  } else if (route && parsedRoute?.complex) {
    appendColoredRouteTokens(topicChipEl, route);
  } else {
    if (agent) {
      const aSpan = document.createElement('span');
      aSpan.className = 'chip-agent';
      aSpan.textContent = '@' + agent;
      setAgentSlugColor(aSpan, agent);
      topicChipEl.appendChild(aSpan);
    }
    if (route) {
      if (adhoc) {
        const originFreshSpan = document.createElement('span');
        originFreshSpan.className = 'chip-adhoc chip-chain-origin-fresh';
        originFreshSpan.textContent = '!';
        if (agent) setAgentSlugColor(originFreshSpan, agent);
        topicChipEl.appendChild(originFreshSpan);
      }
      const arrowSpan = document.createElement('span');
      arrowSpan.className = 'chip-route-arrow';
      arrowSpan.textContent = chainOperator;
      topicChipEl.appendChild(arrowSpan);
      if (chainTargetTopic && chainTargetTopic !== topic) {
        const targetTopicSpan = document.createElement('span');
        targetTopicSpan.className = 'chip-topic chip-chain-target-topic';
        targetTopicSpan.textContent = '#' + chainTargetTopic;
        topicChipEl.appendChild(targetTopicSpan);
      }
      const targetSpan = document.createElement('span');
      targetSpan.className = 'chip-agent chip-chain-target';
      targetSpan.textContent = '@' + chainTarget;
      setAgentSlugColor(targetSpan, chainTarget);
      topicChipEl.appendChild(targetSpan);
      if (chainTargetFresh) {
        const freshSpan = document.createElement('span');
        freshSpan.className = 'chip-adhoc chip-chain-fresh';
        freshSpan.textContent = '!';
        setAgentSlugColor(freshSpan, chainTarget);
        topicChipEl.appendChild(freshSpan);
      }
    } else if (adhoc) {
      const adSpan = document.createElement('span');
      adSpan.className = 'chip-adhoc';
      adSpan.textContent = lookback > 0 ? `!${lookback}` : '!';
      if (agent) setAgentSlugColor(adSpan, agent);
      topicChipEl.appendChild(adSpan);
    } else if (!suppressTurnCount) {
      clearTimeout(_chipTurnCountTimer);
      _chipTurnCountTimer = null;
      const count = _knownSessionTurnCount(topic, agent);
      if (count != null) {
        _renderChipTurnCount(count, { allowZero: true });
      } else {
        _scheduleChipTurnCountUpdate(topic, agent);
      }
    } else {
      clearTimeout(_chipTurnCountTimer);
      _chipTurnCountTimer = null;
      _renderChipTurnCount(0);
    }
  }
  topicChipEl.classList.add('visible');
  topicChipEl.classList.remove('needs-agent');
  chipRow.hidden = false;
  if (route && !broadcastAgents) {
    refreshRouteTurnCounts(route, { force: true });
  }
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
  topicChipEl.classList.remove('visible', 'needs-agent', 'route-chain', 'origin-broadcast');
  input.placeholder = '#topic or #topic@agent message…';
  updateComposerActionTitles();
  updateActiveQuotaGauge();
  _lastContextIndicatorKey = '';
  hideAdvisory();
}

function _renderChipTurnCount(count, opts = {}) {
  let tcSpan = topicChipEl.querySelector('.chip-turn-count');
  if (stickyChip?.suppressTurnCount || stickyChip?.route) count = 0;
  if (count < 0 || (count === 0 && !opts.allowZero)) { tcSpan?.remove(); return; }
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
  _setKnownSessionTurnCount(topic, agent, count, sessionId);
  if (!stickyChip || stickyChip.adhoc || stickyChip.suppressTurnCount || stickyChip.route || stickyChip.topic !== topic || (stickyChip.agent || null) !== (agent || null)) return;
  _renderChipTurnCount(count, { allowZero: true });
}

let _chipTurnCountTimer = null;

function _scheduleChipTurnCountUpdate(topic, agent) {
  clearTimeout(_chipTurnCountTimer);
  _chipTurnCountTimer = setTimeout(() => {
    _chipTurnCountTimer = null;
    if (!stickyChip || stickyChip.adhoc || stickyChip.suppressTurnCount || stickyChip.route || stickyChip.topic !== topic || (stickyChip.agent || null) !== (agent || null)) return;
    const count = _knownSessionTurnCount(topic, agent);
    if (count != null) _renderChipTurnCount(count, { allowZero: true });
  }, 700);
}

function _knownSessionTurnCount(topic, agent) {
  if (!agent) return null;
  const routeKey = `${topic}@${agent}`;
  if (Object.prototype.hasOwnProperty.call(_sessionTurnCountsByRoute, routeKey)) return _sessionTurnCountsByRoute[routeKey] || 0;
  const sid = _sessionIds[routeKey];
  if (!sid || !Object.prototype.hasOwnProperty.call(_sessionTurnCounts, sid)) return null;
  return _sessionTurnCounts[sid] || 0;
}

function _sessionTurnCountForRouteStep(topic, agent, fresh = false) {
  if (!agent || fresh) return null;
  return _knownSessionTurnCount(topic, agent);
}

function _routeChainTurnCounts(topic, originAgent, originFresh, targetAgent, targetFresh, targetTopic) {
  return {
    origin: _sessionTurnCountForRouteStep(topic, originAgent, originFresh),
    target: _sessionTurnCountForRouteStep(targetTopic || topic, targetAgent, targetFresh),
  };
}

function _routePersistentSessionTargets(route) {
  const chain = parseRouteChain(route);
  // Broadcast routes (`@a,@b`) have no chain operator, so parseRouteChain
  // always returns null for them — fall back to the broadcast parser instead
  // of reporting zero persistent targets for every broadcast /clear.
  const steps = chain
    ? [...(chain.origins || []), ...(chain.targets || [])]
    : (parseOriginBroadcast(route)?.agents || []);
  const seen = new Set();
  const targets = [];
  steps.forEach(step => {
    if (!step?.topic || !step?.agent || step.fresh) return;
    const key = `${step.topic}@${step.agent}`;
    if (seen.has(key)) return;
    seen.add(key);
    targets.push({ topic: step.topic, agent: step.agent });
  });
  return targets;
}

topicChipEl.addEventListener('click', () => {
  if (!stickyChip) return;
  const prompt = input.value;
  let tag = stickyChip.route || `#${stickyChip.topic}`;
  if (!stickyChip.route) {
    if (stickyChip.agent) tag += `@${stickyChip.agent}`;
    if (stickyChip.adhoc) tag += `!${stickyChip.lookback || ''}`;
  }
  clearTopicChip();
  editingExpandedSlug = true;
  expandedSlugEditToken++;
  input.value = prompt ? `${tag} ${prompt}` : tag;
  input.setSelectionRange(tag.length, tag.length);
  input.dispatchEvent(new Event('input'));
  input.focus();
});

function routeScopeText(route) {
  if (route?.route) return route.route;
  const topic = route?.topic || 'default';
  let scope = `#${topic}`;
  if (route?.agent) {
    scope += `@${route.agent}`;
    if (route.adhoc) scope += '!';
  }
  return scope;
}

function canonicalFlowRoute(route) {
  const text = String(route || '').trim().replace(/\s+/g, '');
  if (!text) return '';
  const parsed = _parseSquidFlow(text);
  if (parsed?.key) return parsed.key;
  // Fallback for legacy/non-flow strings.
  return text.split(',').filter(Boolean).join(',');
}

function searchScopeText(state) {
  if (!state) return '';
  if (state.flow_route) return state.flow_route;
  let scope = '';
  if (state.explicitAll) scope = '#all';
  else if (state.topic) scope = `#${state.topic}`;
  if (state.agent) {
    scope += `@${state.agent}`;
    if (state.adhoc === true) scope += '!';
    else if (state.adhoc === null) scope += '*';
  }
  return scope;
}

function activeHistoryFilterScope() {
  return (historyFilter.flow_route || historyFilter.topic || historyFilter.agent) ? searchScopeText(historyFilter) : '';
}

async function resolveEffectiveComposerRoute() {
  if (input.value.trimStart().startsWith('#')) {
    const parsed = parseInput(input.value);
    return {
      topic: parsed.topic || 'default',
      agent: parsed.agent || null,
      adhoc: !!parsed.adhoc,
      lookback: parsed.lookback || 0,
      ...(parsed.route ? {
        route: parsed.route,
        chainTarget: parsed.chainTarget,
        chainTargetFresh: parsed.chainTargetFresh,
        chainOperator: parsed.chainOperator || '>',
        chainRounds: parsed.chainRounds || 0,
        chainTargetTopic: parsed.chainTargetTopic,
      } : {}),
    };
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
  chipFilterBtn.title = `filter topic: /f ${scope}`;
  chipSearchBtn.title = `search: /s ${scope} kw1 kw2…`;
  chipClearBtn.title = 'clear context: /clear';
  chipStashBtn.title = `Stash prompt for autocomplete (${scope})`;
}

chipFilterBtn.addEventListener('click', async e => {
  e.stopPropagation();
  const active = (searchActive && searchState) ? searchState : historyFilter;
  if (active?.flow_route || active?.topic || active?.agent || active?.explicitAll) {
    clearFilter();
    return;
  }
  const route = await resolveEffectiveComposerRoute();
  if (route.route) filterByFlowRoute(route.route, route);
  else if (route.agent) filterByAgent(route.topic, route.agent, route.adhoc, route.lookback || 0);
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
  const entry = route.route ? `${route.route} ${message}` : formatPromptHistoryEntry(route.topic, route.agent, route.adhoc, route.lookback || 0, message);
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
  if (stickyChip) {
    const explicit = String(text || '').trimStart();
    if (explicit.startsWith('#')) {
      const activeChip = stickyChip;
      stickyChip = null;
      try {
        const parsed = parseInput(text);
        if (parseCommand(parsed.message)) return parsed;
      } finally {
        stickyChip = activeChip;
      }
    }
    const adhoc = !!stickyChip.adhoc;
    return {
      topic: stickyChip.topic,
      agent: stickyChip.agent,
      adhoc,
      lookback: stickyChip.lookback || 0,
      ...(stickyChip.route ? {
        route: stickyChip.route,
        chainTarget: stickyChip.chainTarget,
        chainTargetFresh: stickyChip.chainTargetFresh,
        chainOperator: stickyChip.chainOperator || '>',
        chainRounds: stickyChip.chainRounds || 0,
        chainTargetTopic: stickyChip.chainTargetTopic || null,
        broadcastAgents: stickyChip.broadcastAgents || null,
        flowOrigins: stickyChip.flowOrigins || null,
      } : {}),
      message: text.trim() || text,
    };
  }
  const broadcastWithMessage = text.match(new RegExp(`^(${_BROADCAST_ROUTE_DETECT_SRC})(?:\\s+([\\s\\S]*))?$`));
  const parsedBroadcastWithMessage = broadcastWithMessage ? parseOriginBroadcast(broadcastWithMessage[1]) : null;
  if (parsedBroadcastWithMessage && broadcastWithMessage[2] && broadcastWithMessage[2].trim()) {
    return {
      topic: parsedBroadcastWithMessage.topic,
      agent: null,
      adhoc: false,
      lookback: 0,
      route: broadcastWithMessage[1],
      broadcastAgents: parsedBroadcastWithMessage.agents,
      message: broadcastWithMessage[2].trim(),
    };
  }
  const routeTokenWithMessage = text.match(/^(\S+)\s+([\s\S]*)$/);
  const parsedChainWithMessage = routeTokenWithMessage ? parseRouteChain(routeTokenWithMessage[1]) : null;
  if (parsedChainWithMessage && routeTokenWithMessage[2].trim()) {
    return {
      topic: parsedChainWithMessage.topic,
      agent: parsedChainWithMessage.origin,
      adhoc: parsedChainWithMessage.originFresh,
      lookback: 0,
      // Preserve the literal typed token here, not parsedChainWithMessage.route
      // (the canonicalized/reduced spelling) — canonicalization is for
      // backend storage/dedup (flow_route), not for what the composer/chip
      // and prompt-history recall should echo back to the user.
      route: routeTokenWithMessage[1],
      chainTarget: parsedChainWithMessage.target,
      chainTargetFresh: parsedChainWithMessage.targetFresh,
      chainOperator: parsedChainWithMessage.operator,
      chainRounds: parsedChainWithMessage.rounds,
      chainTargetTopic: parsedChainWithMessage.targetTopic,
      flowOrigins: parsedChainWithMessage.origins,
      message: routeTokenWithMessage[2].trim(),
    };
  }
  // adhoc: #topic!N or #topic@agent!N (N optional, defaults to 0 = no lookback)
  const ma = text.match(new RegExp(`^#(${TOPIC_SLUG_SRC})(?:@(${AGENT_SLUG_SRC}))?!(\\d*)\\s+([\\s\\S]*)$`));
  if (ma && ma[4].trim()) {
    return { topic: ma[1].toLowerCase(), agent: ma[2] || null, adhoc: true, lookback: ma[3] ? Math.min(parseInt(ma[3]), 20) : 0, message: ma[4].trim() };
  }
  // session: #topic or #topic@agent
  const ms = text.match(new RegExp(`^#(${TOPIC_SLUG_SRC})(?:@(${AGENT_SLUG_SRC}))?\\s+([\\s\\S]*)$`));
  if (ms && ms[3].trim()) {
    return { topic: ms[1].toLowerCase(), agent: ms[2] || null, adhoc: false, lookback: 0, message: ms[3].trim() };
  }
  // bare topic switch: #topic, #topic@agent, #topic!N, or #topic@agent!N with no message
  // switches chip only.
  const parsedChainBare = parseRouteChain(text.trim());
  if (parsedChainBare) {
    return {
      topic: parsedChainBare.topic,
      agent: parsedChainBare.origin,
      adhoc: parsedChainBare.originFresh,
      lookback: 0,
      route: text.trim(),
      chainTarget: parsedChainBare.target,
      chainTargetFresh: parsedChainBare.targetFresh,
      chainOperator: parsedChainBare.operator,
      chainRounds: parsedChainBare.rounds,
      chainTargetTopic: parsedChainBare.targetTopic,
      flowOrigins: parsedChainBare.origins,
      message: '',
    };
  }
  const parsedBroadcastBare = parseOriginBroadcast(text.trim());
  if (parsedBroadcastBare) {
    return {
      topic: parsedBroadcastBare.topic,
      agent: null,
      adhoc: false,
      lookback: 0,
      route: text.trim(),
      broadcastAgents: parsedBroadcastBare.agents,
      message: '',
    };
  }
  const mb = text.match(new RegExp(`^#(${TOPIC_SLUG_SRC})(?:@(${AGENT_SLUG_SRC}))?(?:!(\\d*))?$`));
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

// Origin Broadcast (ADR-0032), live display only: the shared user bubble has
// no single agent identity (it's the one literal prompt sent to N targets),
// so instead of makeTopicTag's single #topic@agent it shows the full comma
// route — #topic@a,@b — matching what the composer chip already renders for
// the same broadcast. This is never persisted; after a refresh each target's
// own history row shows only its own #topic@agent, same as any other turn.
function makeBroadcastRouteTag(broadcastAgents) {
  const wrap = document.createElement('span');
  wrap.className = 'topic-tag';
  let lastTopic = null;
  broadcastAgents.forEach((a, i) => {
    if (i > 0) {
      const sepSpan = document.createElement('span');
      sepSpan.className = 'tag-broadcast-sep';
      sepSpan.textContent = ',';
      wrap.appendChild(sepSpan);
    }
    if (a.topic !== lastTopic) {
      const tSpan = document.createElement('span');
      tSpan.className = 'tag-topic';
      tSpan.textContent = '#' + a.topic;
      wrap.appendChild(tSpan);
      lastTopic = a.topic;
    }
    const aSpan = document.createElement('span');
    aSpan.className = 'tag-agent';
    aSpan.textContent = '@' + a.agent;
    setAgentSlugColor(aSpan, a.agent);
    wrap.appendChild(aSpan);
    if (a.fresh) {
      const freshSpan = document.createElement('span');
      freshSpan.className = 'tag-adhoc';
      freshSpan.textContent = '!';
      setAgentSlugColor(freshSpan, a.agent);
      wrap.appendChild(freshSpan);
    }
  });
  return wrap;
}

function makeFlowRouteTag(route) {
  const wrap = document.createElement('span');
  wrap.className = 'topic-tag';
  appendColoredRouteTokens(wrap, route, {
    topicClass: 'tag-topic',
    agentClass: 'tag-agent',
    freshClass: 'tag-adhoc',
  });
  return wrap;
}

// ── history filter ─────────────────────────────────────────────────────────────

let historyFilter = { topic: null, agent: null, adhoc: null, flow_route: null };
let promptOnlyHistory = false;

function updatePromptOnlyButton() {
  const btn = document.getElementById('chip-prompts-btn');
  if (!btn) return;
  btn.classList.toggle('active', promptOnlyHistory);
  btn.setAttribute('aria-pressed', promptOnlyHistory ? 'true' : 'false');
  btn.title = promptOnlyHistory ? 'show full thread: /prompts' : 'prompts only: /prompts';
}

function updateFilterButton() {
  const active = (searchActive && searchState) ? searchState : historyFilter;
  const isFiltered = !!(active?.flow_route || active?.topic || active?.agent || active?.explicitAll);
  chipFilterBtn?.classList.toggle('active', isFiltered);
  chipFilterBtn?.setAttribute('aria-pressed', isFiltered ? 'true' : 'false');
}

function updateSearchButton() {
  chipSearchBtn?.classList.toggle('active', !!searchActive);
  chipSearchBtn?.setAttribute('aria-pressed', searchActive ? 'true' : 'false');
}

function hasHistoryFilterScope() {
  return !!(historyFilter.flow_route || historyFilter.topic || historyFilter.agent || historyFilter.explicitAll);
}

function hasResponseOnlyFilter() {
  return bookmarkOnlyHistory || badOnlyHistory;
}

function persistSearchFilterScope(state) {
  if (!hasHistoryFilterScope()) return;
  historyFilter = {
    flow_route: state.flow_route || null,
    topic: state.flow_route ? null : (state.topic || null),
    agent: state.flow_route ? null : (state.agent || null),
    adhoc: state.flow_route ? null : (state.adhoc ?? null),
    explicitAll: !!state.explicitAll,
  };
}

function togglePromptOnlyHistory() {
  promptOnlyHistory = !promptOnlyHistory;
  if (promptOnlyHistory && bookmarkOnlyHistory) {
    bookmarkOnlyHistory = false;
    updateBookmarkButton();
  }
  if (promptOnlyHistory && badOnlyHistory) {
    badOnlyHistory = false;
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
  applyHistoryFilter({ topic, agent: null, adhoc: null, flow_route: null });
}

function filterByAgent(topic, agent, adhoc = false, lookback = 0) {
  setTopicChip(topic, agent, adhoc, lookback);
  applyHistoryFilter({ topic, agent, adhoc, flow_route: null });
}

function filterByFlowRoute(route, routeParts = null) {
  const flowRoute = canonicalFlowRoute(route);
  const parsed = routeParts?.route ? routeParts : parseInput(flowRoute);
  setTopicChip(parsed.topic, parsed.agent, parsed.adhoc, parsed.lookback || 0, {
    route: parsed.route,
    chainTarget: parsed.chainTarget,
    chainTargetFresh: parsed.chainTargetFresh,
    chainOperator: parsed.chainOperator || '>',
    chainRounds: parsed.chainRounds || 0,
    chainTargetTopic: parsed.chainTargetTopic,
  });
  applyHistoryFilter({ flow_route: flowRoute, topic: null, agent: null, adhoc: null });
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
    searchState = { ...searchState, flow_route: null, topic: null, agent: null, adhoc: null, explicitAll: false };
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

// A still-in-flight (queued or streaming) thinking bubble, plus the user bubble/timestamp
// preceding it, form a "live group" — kept in the DOM across filter/search. While a search
// scope is active they stay hidden (can't be evaluated against keywords until it lands in
// the DB), but while a filter scope is active each group is shown or hidden based on whether
// its own topic/agent/adhoc (tracked on the thinking bubble's dataset) matches the filter.
function collectLiveGroupElements() {
  const group = new Set();
  document.querySelectorAll('#messages > .msg-thinking:not(.msg-thinking-done)').forEach(thinking => {
    group.add(thinking);
    let el = thinking.previousElementSibling;
    while (el && isLiveGroupPreviousElement(el)) {
      group.add(el);
      el = el.previousElementSibling;
    }
  });
  return group;
}

function isLiveGroupPreviousElement(el) {
  return el.classList.contains('msg-time')
    || el.classList.contains('route-chain-marker')
    || el.id === 'code-roots-prompt'
    || (el.classList.contains('msg') && el.classList.contains('user'));
}

function setLiveGroupHidden(hidden) {
  document.querySelectorAll('#messages > .msg-thinking:not(.msg-thinking-done)').forEach(thinking => {
    const group = [thinking];
    let el = thinking.previousElementSibling;
    while (el && isLiveGroupPreviousElement(el)) {
      group.push(el);
      el = el.previousElementSibling;
    }
    // Search scope can't be matched client-side (keywords aren't tracked on the live
    // bubble), so a search in progress forces the whole group hidden. A filter scope can
    // be matched — only hide groups that don't belong to it.
    const stayHidden = hidden && (searchActive || !itemMatchesFilter({
      topic: thinking.dataset.topic || 'default',
      agent: thinking.dataset.agent || null,
      adhoc: thinking.dataset.adhoc === '1',
      flow_route: thinking.dataset.flowRoute || null,
    }, historyFilter));
    group.forEach(node => node.classList.toggle('live-hidden', stayHidden));
  });
}

function reloadHistory(filter = {}) {
  historyFilter = filter;
  historyOffset = 0;
  historyExhausted = false;
  historyWindowMode = false;
  historyWindowEdgesArmed = false;
  historyTopPaginationArmed = true;
  historyOlderCursor = null;
  historyNewerCursor = null;
  historyHasOlder = false;
  historyHasNewer = false;
  invalidateHistoryLoad();
  if (topSentinel) { topSentinel.remove(); topSentinel = null; }
  if (bottomSentinel) { bottomSentinel.remove(); bottomSentinel = null; }
  document.querySelectorAll('.history-item, .boot-banner, .tool-block-history, #messages > .cmd-feedback').forEach(el => el.remove());
  // Remove live (non-history) messages too — completed ones are in the DB and will reload
  const preserveForLive = collectLiveGroupElements();
  document.querySelectorAll('#messages > .msg:not(.msg-thinking), #messages > .msg-thinking-done, #messages > .msg-time, #messages > .stats, #messages > .route-chain-marker').forEach(el => {
    if (!preserveForLive.has(el)) el.remove();
  });
  setLiveGroupHidden(hasHistoryFilterScope() || hasResponseOnlyFilter());
  _updateFilterBadge();
  initHistoryScroll();
}

function _updateFilterBadge() {
  const badge = document.getElementById('filter-badge');
  const labelEl = document.getElementById('filter-badge-label');
  const activeState = (searchActive && searchState) ? searchState : historyFilter;
  const { topic, agent, adhoc, flow_route } = activeState;
  const explicitAll = !!activeState.explicitAll;

  if (!flow_route && !topic && !agent && !explicitAll && !bookmarkOnlyHistory && !badOnlyHistory) {
    badge.classList.remove('active');
    updateFilterButton();
    return;
  }

  labelEl.innerHTML = '';
  const addSegment = (kind, content, remove, editable = true) => {
    const segment = document.createElement('span');
    segment.className = `filter-scope-segment filter-scope-${kind}`;
    segment.appendChild(content);
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'filter-scope-remove';
    x.setAttribute('aria-label', `Remove ${kind} filter`);
    x.addEventListener('click', e => { e.stopPropagation(); remove(); });
    segment.appendChild(x);
    if (editable) segment.addEventListener('click', editActiveFilter);
    labelEl.appendChild(segment);
  };

  if (flow_route) {
    const route = document.createElement('span');
    route.className = 'tag-topic';
    route.textContent = flow_route;
    addSegment('flow', route, () => removeFilterSegment('flow'));
  } else if (topic || explicitAll) {
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
  if (bookmarkOnlyHistory) {
    addSegment('bookmarks', document.createTextNode('bookmarked'), () => removeFilterSegment('bookmarks'), false);
  }
  if (badOnlyHistory) {
    const icon = document.createElement('span');
    icon.className = 'filter-scope-icon material-symbols-outlined';
    icon.textContent = 'thumb_down';
    icon.title = 'Marked bad responses';
    icon.setAttribute('aria-label', 'Marked bad responses');
    addSegment('bad', icon, () => removeFilterSegment('bad'), false);
  }
  badge.classList.add('active');
  updateFilterButton();
}

function itemMatchesFilter(item, filter) {
  if (!filter) return true;
  if (filter.flow_route && (item.flow_route || item.flowRoute || null) !== filter.flow_route) return false;
  if (filter.topic && (item.topic || 'default') !== filter.topic) return false;
  if (filter.agent && (item.agent || null) !== filter.agent) return false;
  if (filter.adhoc !== null && filter.adhoc !== undefined && !!item.adhoc !== filter.adhoc) return false;
  return true;
}

function shouldShowNewResponse(item) {
  if (searchActive) return false;
  if (hasResponseOnlyFilter()) return false;
  if (hasHistoryFilterScope()) return itemMatchesFilter(item, historyFilter);
  return true;
}

function removeFilterSegment(kind) {
  const active = (searchActive && searchState) ? searchState : historyFilter;
  const next = { ...active };
  if (kind === 'topic') {
    next.topic = null;
    next.explicitAll = false;
  } else if (kind === 'flow') {
    next.flow_route = null;
  } else if (kind === 'bookmarks') {
    bookmarkOnlyHistory = false;
    updateBookmarkButton();
  } else if (kind === 'bad') {
    badOnlyHistory = false;
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
    reloadHistory({ flow_route: next.flow_route || null, topic: next.topic || null, agent: next.agent || null, adhoc: next.adhoc ?? null });
  }
}

// ── history pagination (display) ─────────────────────────────────────────────

let historyOffset = 0;
let historyExhausted = false;
let historyLoading = false;
let topSentinel = null;
let bottomSentinel = null;
let historyGeneration = 0;
let historyObserver = null;
let historyBottomObserver = null;
let historyWindowMode = false;
let historyOlderCursor = null;
let historyNewerCursor = null;
let historyHasOlder = false;
let historyHasNewer = false;
let historyWindowEdgesArmed = false;
let historyTopPaginationArmed = true;
const pendingPollTimers = new WeakMap();

function invalidateHistoryLoad() {
  historyGeneration++;
  historyLoading = false;
  if (historyObserver) {
    historyObserver.disconnect();
    historyObserver = null;
  }
  if (historyBottomObserver) {
    historyBottomObserver.disconnect();
    historyBottomObserver = null;
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

function createBottomSentinel() {
  const el = document.createElement('div');
  el.id = 'history-bottom-sentinel';
  return el;
}

function updateScrollButtonVisibility() {
  scrollBtn.classList.toggle('visible', historyWindowMode || !isAtBottom());
}

function historyUrlParams() {
  const params = new URLSearchParams();
  if (historyFilter.flow_route) params.set('flow_route', historyFilter.flow_route);
  if (historyFilter.topic) params.set('topic', historyFilter.topic);
  if (historyFilter.agent) params.set('agent', historyFilter.agent);
  if (historyFilter.adhoc != null) params.set('adhoc', historyFilter.adhoc);
  if (bookmarkOnlyHistory) params.set('bookmarked', 'true');
  if (badOnlyHistory) params.set('marked_bad', 'true');
  return params;
}

function appendHistoryItems(items, fragment) {
  const chronologicalItems = [...items].reverse();
  for (let i = 0; i < chronologicalItems.length; i += 1) {
    const item = chronologicalItems[i];
    // Skip if a bubble for this message is already in the DOM — e.g. an
    // in-progress live (SSE) bubble that survived a search → back round-trip.
    // Without this, loadHistory would render a second, polling-driven bubble
    // for the same message alongside the live one.
    if (item.id != null && messages.querySelector(`[data-msg-id="${item.id}"]`)) continue;

    if (promptOnlyHistory) {
      appendPromptOnlyHistoryItem(item, fragment);
      continue;
    }

    if (!item.content && !item.context && item.status !== 'pending') continue;

    if (item.status === 'pending') {
      // Queued/in-flight items already carry topic/agent/adhoc from the DB row, so a
      // filter scope can be checked directly — only skip ones that don't belong to it.
      if (!itemMatchesFilter(item, historyFilter)) continue;
      const wipBubble = makeWipBubble(item);
      fragment.appendChild(wipBubble);
      reconnectPendingItem(item, wipBubble);
      continue;
    }

    const routeMarker = historyRouteChainMarkerForItem(item, chronologicalItems[i + 1], chronologicalItems[i - 1]);
    appendHistoryRouteChainMarker(routeMarker, item, fragment);
    appendHistoryItem(item, fragment);
  }
}

async function loadHistory() {
  // #view-chat is display:none while another tab is active, which collapses every
  // descendant's getBoundingClientRect() to zero — including the sentinel visibility
  // check below, which would then read as "still in view" no matter what and chain-load
  // the entire history in the background. Bail here so a page in flight when the user
  // switches tabs doesn't keep pulling more; the IntersectionObserver re-evaluates and
  // resumes normally once #view-chat becomes visible again.
  if (currentView !== 'chat') return;
  if (historyWindowMode) return;
  if (historyExhausted || historyLoading) return;
  historyLoading = true;
  const generation = historyGeneration;

  let data;
  try {
    const params = historyUrlParams();
    params.set('offset', historyOffset);
    params.set('limit', 5);
    const url = `/history?${params.toString()}`;
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
  appendHistoryItems(items, fragment);

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
  updateScrollButtonVisibility();
}

async function loadHistoryWindow(direction) {
  if (currentView !== 'chat' || !historyWindowMode) return;
  if (historyLoading) return;
  const cursor = direction === 'newer' ? historyNewerCursor : historyOlderCursor;
  const hasMore = direction === 'newer' ? historyHasNewer : historyHasOlder;
  if (!cursor || !hasMore) return;
  historyLoading = true;
  const generation = historyGeneration;

  let data;
  try {
    const params = historyUrlParams();
    params.set('direction', direction);
    params.set('cursor_completed_at', cursor.completed_at);
    params.set('cursor_id', cursor.id);
    params.set('limit', 20);
    const res = await fetch(`/history/around?${params.toString()}`);
    data = await res.json();
  } catch {
    if (generation === historyGeneration) historyLoading = false;
    return;
  }
  if (generation !== historyGeneration || !historyWindowMode) return;

  const prevHeight = messages.scrollHeight;
  const fragment = document.createDocumentFragment();
  appendHistoryItems(data.items || [], fragment);

  if (direction === 'newer') {
    const anchor = bottomSentinel || null;
    messages.insertBefore(fragment, anchor);
    historyNewerCursor = data.newer_cursor || historyNewerCursor;
    historyHasNewer = !!data.has_more;
  } else {
    const anchor = topSentinel ? topSentinel.nextSibling : messages.firstChild;
    messages.insertBefore(fragment, anchor);
    messages.scrollTop += messages.scrollHeight - prevHeight;
    historyOlderCursor = data.older_cursor || historyOlderCursor;
    historyHasOlder = !!data.has_more;
  }

  historyLoading = false;
  updateInContextMarkers();
  refreshAllRevertButtons();
  evaluateAdvisory();
  refreshDateDividers();
  updateHistoryWindowSentinels();
  updateScrollButtonVisibility();
}

async function resetHistoryToLatest() {
  const generation = historyGeneration + 1;
  invalidateHistoryLoad();
  historyWindowMode = false;
  historyWindowEdgesArmed = false;
  historyTopPaginationArmed = false;
  historyOlderCursor = null;
  historyNewerCursor = null;
  historyHasOlder = false;
  historyHasNewer = false;
  historyOffset = 0;
  historyExhausted = false;
  if (topSentinel) { topSentinel.remove(); topSentinel = null; }
  if (bottomSentinel) { bottomSentinel.remove(); bottomSentinel = null; }

  let data;
  try {
    const params = historyUrlParams();
    params.set('offset', 0);
    params.set('limit', 5);
    const res = await fetch(`/history?${params.toString()}`);
    data = await res.json();
  } catch {
    if (generation === historyGeneration) {
      historyLoading = false;
      updateScrollButtonVisibility();
    }
    return;
  }
  if (generation !== historyGeneration) return;

  document.querySelectorAll('.history-item, .boot-banner, .search-result-item, .tool-block-history, #messages > .cmd-feedback').forEach(el => el.remove());
  const preserveForLive = collectLiveGroupElements();
  document.querySelectorAll('#messages > .msg:not(.msg-thinking), #messages > .msg-thinking-done, #messages > .msg-time, #messages > .stats, #messages > .route-chain-marker').forEach(el => {
    if (!preserveForLive.has(el)) el.remove();
  });
  setLiveGroupHidden(hasHistoryFilterScope() || hasResponseOnlyFilter());

  const fragment = document.createDocumentFragment();
  appendHistoryItems(data.items || [], fragment);
  messages.appendChild(fragment);
  historyOffset = (data.items || []).length;
  historyExhausted = !data.has_more;
  historyLoading = false;

  if (!historyExhausted) {
    topSentinel = createTopSentinel();
    messages.insertBefore(topSentinel, messages.firstChild);
    historyObserver = new IntersectionObserver(
      (entries) => { if (historyTopPaginationArmed && entries[0].isIntersecting) loadHistory(); },
      { root: messages, threshold: 0 },
    );
    historyObserver.observe(topSentinel);
    setTimeout(() => { historyTopPaginationArmed = true; }, 250);
  }

  messages.scrollTop = messages.scrollHeight;
  _updateFilterBadge();
  updateInContextMarkers();
  refreshAllRevertButtons();
  evaluateAdvisory();
  refreshDateDividers();
  updateScrollButtonVisibility();
}

function initHistoryScroll() {
  if (historyObserver) historyObserver.disconnect();
  if (historyBottomObserver) {
    historyBottomObserver.disconnect();
    historyBottomObserver = null;
  }
  if (bottomSentinel) {
    bottomSentinel.remove();
    bottomSentinel = null;
  }
  historyWindowMode = false;
  historyWindowEdgesArmed = false;
  historyTopPaginationArmed = true;
  topSentinel = createTopSentinel();
  messages.insertBefore(topSentinel, messages.firstChild);

  historyObserver = new IntersectionObserver(
    (entries) => { if (historyTopPaginationArmed && entries[0].isIntersecting) loadHistory(); },
    { root: messages, threshold: 0 },
  );
  historyObserver.observe(topSentinel);
}

function updateHistoryWindowSentinels() {
  if (topSentinel && !historyHasOlder) {
    topSentinel.remove();
    topSentinel = null;
  }
  if (bottomSentinel && !historyHasNewer) {
    bottomSentinel.remove();
    bottomSentinel = null;
  }
}

function initHistoryWindowScroll() {
  if (historyObserver) historyObserver.disconnect();
  if (historyBottomObserver) historyBottomObserver.disconnect();
  historyWindowEdgesArmed = false;
  setTimeout(() => { historyWindowEdgesArmed = true; }, 250);

  if (historyHasOlder) {
    topSentinel = createTopSentinel();
    messages.insertBefore(topSentinel, messages.firstChild);
    historyObserver = new IntersectionObserver(
      (entries) => { if (historyWindowEdgesArmed && entries[0].isIntersecting) loadHistoryWindow('older'); },
      { root: messages, threshold: 0 },
    );
    historyObserver.observe(topSentinel);
  }

  if (historyHasNewer) {
    bottomSentinel = createBottomSentinel();
    messages.appendChild(bottomSentinel);
    historyBottomObserver = new IntersectionObserver(
      (entries) => { if (historyWindowEdgesArmed && entries[0].isIntersecting) loadHistoryWindow('newer'); },
      { root: messages, threshold: 0 },
    );
    historyBottomObserver.observe(bottomSentinel);
  }
}

async function jumpToMessage(msgId, flowRunId = null) {
  const targetLabel = flowRunId ? `flow:${flowRunId}` : String(msgId);
  const feedbackEl = showCmdFeedback(`jump ${targetLabel}...`);
  const generation = historyGeneration;

  try {
    const params = historyUrlParams();
    if (flowRunId) params.set('flow_run_id', flowRunId);
    else params.set('msg_id', msgId);
    params.set('before', 20);
    params.set('after', 20);
    const res = await fetch(`/history/around?${params.toString()}`);
    const data = await res.json();
    if (!res.ok || !data.found) {
      feedbackEl.textContent = `jump ${targetLabel} — not found`;
      return;
    }
    if (generation !== historyGeneration) return;

    invalidateHistoryLoad();
    historyWindowMode = true;
    historyLoading = true;
    document.getElementById('msg-modal')?.classList.remove('open');
    document.getElementById('ctx-popup')?.classList.remove('open');
    document.getElementById('stats-turn-popup')?.classList.remove('open');
    if (currentView !== 'chat') navigateView('chat');

    if (searchActive) {
      searchActive = false;
      searchState = null;
      searchLoading = false;
      document.getElementById('search-bar').classList.remove('active');
      updateSearchButton();
    }
    if (promptOnlyHistory) {
      promptOnlyHistory = false;
      updatePromptOnlyButton();
    }
    if (topSentinel) { topSentinel.remove(); topSentinel = null; }
    if (bottomSentinel) { bottomSentinel.remove(); bottomSentinel = null; }
    document.querySelectorAll('.history-item, .boot-banner, .search-result-item, .tool-block-history, #messages > .cmd-feedback').forEach(el => el.remove());
    const preserveForLive = collectLiveGroupElements();
    document.querySelectorAll('#messages > .msg:not(.msg-thinking), #messages > .msg-thinking-done, #messages > .msg-time, #messages > .stats, #messages > .route-chain-marker').forEach(el => {
      if (!preserveForLive.has(el)) el.remove();
    });
    setLiveGroupHidden(hasHistoryFilterScope() || hasResponseOnlyFilter());

    const fragment = document.createDocumentFragment();
    appendHistoryItems(data.items || [], fragment);
    messages.appendChild(fragment);
    historyOlderCursor = data.older_cursor || null;
    historyNewerCursor = data.newer_cursor || null;
    historyHasOlder = !!data.has_older;
    historyHasNewer = !!data.has_newer;
    historyLoading = false;
    initHistoryWindowScroll();
    _updateFilterBadge();
    updateInContextMarkers();
    refreshAllRevertButtons();
    evaluateAdvisory();
    refreshDateDividers();

    const resolvedMsgId = data.target_id || msgId;
    const target = messages.querySelector(`.msg[data-msg-id="${resolvedMsgId}"]`);
    if (target) {
      target.scrollIntoView({ block: 'center' });
      target.classList.add('msg-jump-highlight');
      setTimeout(() => target.classList.remove('msg-jump-highlight'), 1800);
    }
    updateScrollButtonVisibility();
  } catch {
    feedbackEl.textContent = `jump ${targetLabel} — request failed`;
    historyLoading = false;
  }
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

  if (/^#.+>|,/.test(scope)) {
    const flowRoute = canonicalFlowRoute(scope);
    return flowRoute
      ? { flow_route: flowRoute, topic: null, agent: null, adhoc: null, explicitAll: false }
      : null;
  }

  const topicMatch = scope.match(new RegExp(`^#(${TOPIC_SLUG_SRC})(?:@([\\w-]+)([!+*])?)?$`));
  if (topicMatch) {
    const agent = topicMatch[2] || null;
    const mode = topicMatch[3] || '';
    return {
      topic: topicMatch[1].toLowerCase(),
      agent,
      adhoc: agent ? (mode === '+' || mode === '*' ? null : mode === '!') : null,
      explicitAll: false,
    };
  }

  const agentMatch = scope.match(/^@([\w-]+)([!+*])?$/);
  if (agentMatch) {
    const mode = agentMatch[2] || '';
    return {
      topic: null,
      agent: agentMatch[1],
      adhoc: mode === '+' || mode === '*' ? null : mode === '!',
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
  return { flow_route: null, topic: null, agent: null, adhoc: null, explicitAll: false, explicitScope: false, keywords: rest };
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
    showCmdFeedback('Usage: /s [#topic[@agent[!|*]] | #topic@agent>@agent | @agent[!|*] | #all] keywords…');
    return;
  }

  let flow_route, topic, agent, adhoc;
  const explicitAll = parsed.explicitAll;
  if (parsed.explicitScope) {
    // explicit scope typed in command overrides the active filter
    flow_route = parsed.flow_route || null;
    topic = parsed.topic;
    agent = parsed.agent || null;
    adhoc = parsed.adhoc;
  } else if (historyFilter.flow_route || historyFilter.topic || historyFilter.agent) {
    // active history filter (set by /filter or tag click)
    flow_route = historyFilter.flow_route || null;
    topic = historyFilter.topic || null;
    agent = historyFilter.agent || null;
    adhoc = historyFilter.adhoc ?? null;
  } else {
    // fall back to sticky chip (current chat context)
    flow_route = stickyChip?.route ? canonicalFlowRoute(stickyChip.route) : null;
    topic = flow_route ? null : (stickyChip?.topic || null);
    agent = flow_route ? null : (stickyChip?.agent || null);
    adhoc = flow_route ? null : (stickyChip?.adhoc ? true : false);
  }

  searchState = { flow_route, topic, agent, adhoc, explicitAll, keywords: parsed.keywords };
  searchActive = true;
  searchLoading = false;

  // Stop history scroll
  if (topSentinel) { topSentinel.remove(); topSentinel = null; }
  if (bottomSentinel) { bottomSentinel.remove(); bottomSentinel = null; }
  historyWindowMode = false;
  historyWindowEdgesArmed = false;
  invalidateHistoryLoad();

  // Clear pane
  document.querySelectorAll('.history-item, .boot-banner, .search-result-item, .tool-block-history').forEach(el => el.remove());
  const preserveForLive = collectLiveGroupElements();
  document.querySelectorAll('#messages > .msg:not(.msg-thinking), #messages > .msg-thinking-done, #messages > .msg-time, #messages > .stats, #messages > .cmd-feedback').forEach(el => {
    if (!preserveForLive.has(el)) el.remove();
  });
  setLiveGroupHidden(true);

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
  if (bottomSentinel) { bottomSentinel.remove(); bottomSentinel = null; }
  historyOffset = 0;
  historyExhausted = false;
  historyWindowMode = false;
  historyWindowEdgesArmed = false;
  historyOlderCursor = null;
  historyNewerCursor = null;
  historyHasOlder = false;
  historyHasNewer = false;
  invalidateHistoryLoad();
  setLiveGroupHidden(hasHistoryFilterScope() || hasResponseOnlyFilter());
  _updateFilterBadge();
  initHistoryScroll();
}

function recordPrompt(text) {
  const t = text.trim();
  if (!t) return;
  const key = promptHistoryDedupKey(t);
  if (hiddenPromptKeys.delete(key)) {
    localStorage.setItem(HIDDEN_PROMPTS_KEY, JSON.stringify([...hiddenPromptKeys]));
  }
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
  const tokenMatch = text.match(/^(\S+)\s+([\s\S]+)$/);
  if (tokenMatch && normalizePromptHistoryRoute(tokenMatch[1])) {
    return { route: tokenMatch[1], prompt: tokenMatch[2].trim() };
  }
  return { route: '', prompt: text };
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
  const chain = parseRouteChain(route);
  if (chain) return chain.route;
  const broadcast = parseOriginBroadcast(route);
  if (broadcast) return broadcast.route;
  const match = String(route || '').match(new RegExp(`^#(${TOPIC_SLUG_SRC})(?:@(${AGENT_SLUG_SRC}))?(!)?\\d*$`));
  if (!match) return '';
  return promptHistoryRoute(match[1].toLowerCase(), match[2] || null, !!match[3]);
}

function promptHistoryDisplayRoute(route) {
  const chain = parseRouteChain(route);
  if (chain && !chain.complex) return promptHistoryRoute(chain.topic, chain.origin, chain.originFresh);
  if (chain) return chain.route;
  return normalizePromptHistoryRoute(route);
}

function lastPromptForFlowRoute(route) {
  const targetRoute = normalizePromptHistoryRoute(route).toLowerCase();
  if (!targetRoute) return '';
  for (const entry of promptHistory) {
    if (hiddenPromptKeys.has(promptHistoryDedupKey(entry))) continue;
    const { route: entryRoute, prompt } = splitPromptHistoryEntry(entry);
    if (normalizePromptHistoryRoute(entryRoute).toLowerCase() === targetRoute) return prompt;
  }
  return '';
}

function applyPromptHistoryEntry(entry) {
  const { route, prompt } = splitPromptHistoryEntry(entry);
  if (route) {
    const chain = parseRouteChain(route);
    if (chain) {
      setTopicChip(chain.topic, chain.origin, chain.originFresh, 0, {
        route: chain.route,
        chainTarget: chain.target,
        chainTargetFresh: chain.targetFresh,
        chainOperator: chain.operator,
        chainRounds: chain.rounds,
        chainTargetTopic: chain.targetTopic,
      });
    }
    const broadcast = !chain ? parseOriginBroadcast(route) : null;
    if (broadcast) {
      setTopicChip(broadcast.topic, null, false, 0, {
        route: broadcast.route,
        broadcastAgents: broadcast.agents,
      });
    }
    const match = route.match(new RegExp(`^#(${TOPIC_SLUG_SRC})(?:@(${AGENT_SLUG_SRC}))?(!(?:(\\d+))?)?$`));
    if (!chain && !broadcast && match) {
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

function routeFromParsedInput(parsed) {
  if (parsed.route) return parsed.route;
  return promptHistoryRoute(parsed.topic, parsed.agent, parsed.adhoc);
}

function currentPromptHistoryRoute() {
  if (!stickyChip) return '';
  if (stickyChip.route) return stickyChip.route;
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
    const displayRouteKey = promptHistoryDisplayRoute(routeKey);
    const isDifferentRoute = !!(routeKey && routeKey.toLowerCase() !== currentRoute);
    const routeHtml = isDifferentRoute ? _acRouteHtml(displayRouteKey) : '';
    return {
      label: `<span class="ac-history-prompt">${escapeHtml(truncate(promptText, 55))}</span>`,
      labelClass: 'ac-prompt-label',
      rowClass: 'ac-prompt-row',
      insert: promptText,
      trail: false,
      deletePromptEntry: ph,
      ...(routeHtml ? {
        routeHtml,
        fullEntry: `${displayRouteKey} ${promptText}`,
      } : {}),
    };
  });
}

function parseHistoryRouteTarget(route) {
  const chain = parseRouteChain(route);
  if (chain) {
    return {
      topic: chain.topic,
      agent: chain.origin,
      adhoc: chain.originFresh,
      lookback: 0,
      route: chain.route,
      chainTarget: chain.target,
      chainTargetFresh: chain.targetFresh,
      chainOperator: chain.operator,
      chainRounds: chain.rounds,
      chainTargetTopic: chain.targetTopic,
      flowOrigins: chain.origins,
    };
  }
  const broadcast = parseOriginBroadcast(route);
  if (broadcast) {
    return {
      topic: broadcast.topic,
      agent: null,
      adhoc: false,
      lookback: 0,
      route: broadcast.route,
      broadcastAgents: broadcast.agents,
    };
  }
  const match = String(route || '').match(new RegExp(`^#(${TOPIC_SLUG_SRC})(?:@(${AGENT_SLUG_SRC}))?(!)?$`));
  if (!match) return null;
  return {
    topic: match[1].toLowerCase(),
    agent: match[2] || null,
    adhoc: !!match[3],
    lookback: 0,
  };
}

function applyRouteTarget(routeTarget) {
  if (!routeTarget) return;
  setTopicChip(routeTarget.topic, routeTarget.agent, routeTarget.adhoc, routeTarget.lookback || 0, {
    route: routeTarget.route,
    chainTarget: routeTarget.chainTarget,
    chainTargetFresh: routeTarget.chainTargetFresh,
    chainOperator: routeTarget.chainOperator,
    chainRounds: routeTarget.chainRounds,
    chainTargetTopic: routeTarget.chainTargetTopic,
    broadcastAgents: routeTarget.broadcastAgents,
    flowOrigins: routeTarget.flowOrigins,
  });
  input.value = '';
  input.setSelectionRange(0, 0);
  resizeComposer();
}

function routeHistoryAutocompleteItems(currentRoute = '') {
  const normalizedCurrent = normalizePromptHistoryRoute(currentRoute);
  const items = [];
  const seen = new Set();

  const addRoute = (route, prompt = '', current = false) => {
    const routeKey = normalizePromptHistoryRoute(route);
    const routeTarget = parseHistoryRouteTarget(routeKey);
    const dedupeKey = routeKey.toLowerCase();
    if (!routeKey || !routeTarget || seen.has(dedupeKey)) return;
    seen.add(dedupeKey);
    items.push({
      routeHtml: _acRouteHtml(routeKey),
      routeSwitchIcon: false,
      label: '',
      insert: '',
      trail: false,
      routeTarget,
      // Dedicated route-history browsing (ArrowLeft/ArrowRight over a
      // composer that's only a route) — every item here is a full route to
      // switch to, so previewing-by-highlight applying it live is the point.
      // appendMatchingRouteHistoryItems below shares this same item shape
      // but gets mixed into an otherwise plain-text "Routes" suggestion
      // list, where auto-applying on mere highlight would leave the real
      // chip stuck on a route the user only scrolled past — so it must NOT
      // set this flag.
      previewApply: true,
      currentRoute: current,
      sub: prompt ? _acLastPrompt(prompt) : '',
    });
  };

  addRoute(normalizedCurrent, '', true);
  for (const entry of promptHistory) {
    const { route, prompt } = splitPromptHistoryEntry(entry);
    if (hiddenPromptKeys.has(promptHistoryDedupKey(entry))) continue;
    addRoute(route, prompt, false);
    if (items.length >= 9) break;
  }
  return items;
}

function appendMatchingRouteHistoryItems(items, prefix, seenRoutes = null) {
  const lowerPrefix = String(prefix || '').toLowerCase();
  if (!lowerPrefix.startsWith('#')) return items;
  const seen = seenRoutes || new Set(items.map(item => {
    const route = item.routeTarget?.route || item.insert || '';
    return normalizePromptHistoryRoute(route).toLowerCase();
  }).filter(Boolean));

  for (const entry of promptHistory) {
    if (hiddenPromptKeys.has(promptHistoryDedupKey(entry))) continue;
    const { route, prompt } = splitPromptHistoryEntry(entry);
    const normalizedRouteKey = normalizePromptHistoryRoute(route);
    const rawRouteKey = String(route || '').trim().replace(/\s+/g, '');
    const routeKey = [normalizedRouteKey, rawRouteKey]
      .find(candidate => candidate && candidate.toLowerCase().startsWith(lowerPrefix));
    if (!routeKey) continue;
    const dedupeKey = routeKey.toLowerCase();
    if (seen.has(dedupeKey)) continue;
    const routeTarget = parseHistoryRouteTarget(routeKey);
    if (!routeTarget) continue;
    seen.add(dedupeKey);
    items.push({
      routeHtml: _acRouteHtml(routeKey),
      routeSwitchIcon: false,
      label: '',
      // Plain route text, not '' — these items sit alongside live-typed
      // insert-only suggestions in the same arrow-navigable list, so
      // previewing one must behave the same way theirs does (fill the
      // composer as text) rather than instantly switching the real chip;
      // see previewApply on routeHistoryAutocompleteItems above for why.
      insert: routeKey,
      trail: false,
      routeTarget,
      sub: prompt ? _acLastPrompt(prompt) : '',
    });
  }
  return items;
}

function composerHasOnlyRoute() {
  const value = input.value.trim();
  if (!value) return true;
  return new RegExp(`^#${TOPIC_SLUG_SRC}(?:@${AGENT_SLUG_SRC})?(?:!\\d*)?$`).test(value) || !!parseRouteChain(value) || !!parseOriginBroadcast(value);
}

function composerRouteForRouteHistory() {
  const value = input.value.trim();
  if (value) {
    const chain = parseRouteChain(value);
    if (chain) return chain.route;
    const broadcast = parseOriginBroadcast(value);
    if (broadcast) return broadcast.route;
    return routeFromParsedInput(parseInput(value));
  }
  return currentPromptHistoryRoute();
}

function openRouteHistoryAutocomplete(direction) {
  const items = routeHistoryAutocompleteItems(composerRouteForRouteHistory());
  if (!items.length) return false;
  const currentIndex = Math.max(0, items.findIndex(item => item.currentRoute));
  _acRender(items, 'Routes');
  if (direction === 'previous') {
    acSel = Math.min(currentIndex + 1, acItems.length - 1);
  } else if (direction === 'next') {
    acSel = Math.max(currentIndex - 1, 0);
  } else {
    acSel = currentIndex;
  }
  _acHighlight();
  if (direction) _acPreview();
  return true;
}

async function initPromptHistory() {
  const draft = localStorage.getItem('squid_draft');
  if (draft) { input.value = draft; resizeComposer(); }
  try {
    const res = await fetch('/prompts/recent?limit=50');
    if (!res.ok) return;
    const data = await res.json();
    if (data.agents && typeof data.agents === 'object') {
      mergeAgentCache(Object.values(data.agents));
    }
    if (Array.isArray(data.items)) promptHistory = mergePromptHistory(getStashedPrompts(), data.items);
    if (input.value.trim()) updateAutocomplete();
  } catch { /* ignore */ }
}

async function loadSearchResults() {
  if (searchLoading || !searchState) return;
  searchLoading = true;

  const searchRole = promptOnlyHistory ? 'user' : 'assistant';
  let url = `/search?limit=100&q=${encodeURIComponent(searchState.keywords)}&role=${searchRole}`;
  if (bookmarkOnlyHistory) url += '&bookmarked=true';
  if (badOnlyHistory) url += '&marked_bad=true';
  if (searchState.topic) url += `&topic=${encodeURIComponent(searchState.topic)}`;
  if (searchState.agent) url += `&agent=${encodeURIComponent(searchState.agent)}`;
  if (searchState.adhoc !== null && searchState.adhoc !== undefined) url += `&adhoc=${searchState.adhoc}`;
  if (searchState.flow_route) url += `&flow_route=${encodeURIComponent(searchState.flow_route)}`;

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
    if (!item.content && !item.prompt) continue;
    if (item.status === 'pending') continue;
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
  { name: 'restart',      desc: 'restart the squid server — kills any in-flight prompts (confirms first)', args: false },
  { name: 'refresh',      desc: 'hard refresh this browser tab — clears cache, server untouched', args: false },
  { name: 'f', alias: 'filter', desc: 'filter — e.g. /f #topic  ·  /f @agent!  ·  /f reset', args: true },
  { name: 's', alias: 'search', desc: 'search — e.g. /s #topic kw  ·  /s @agent! kw  ·  /s #all kw', args: true },
  { name: 'jump', alias: 'j', desc: 'jump to message or flow id — e.g. /jump 12345 · /jump flow:71', args: true },
  { name: 'bookmarks', alias: 'bm', desc: 'toggle bookmarked responses only',         args: false },
  { name: 'bad',        desc: 'toggle marked bad responses only',                     args: false },
  { name: 'prompts',     desc: 'toggle user prompts only',                            args: false },
  { name: 'status',       desc: 'show active processes panel',                        args: false },
  { name: 'help',         desc: 'show help panel',                                    args: false },
  { name: 'remote',       desc: 'show QR code for mobile / tablet access',            args: false },
];

function parseCommand(message) {
  const trimmed = message.trim();
  if (!trimmed.startsWith('/')) return null; // commands must be slash-prefixed
  const t = trimmed.slice(1);
  if (/^restart$/i.test(t))      return { command: 'restart' };
  if (/^refresh$/i.test(t))      return { command: 'refresh' };
  if (/^stop$/i.test(t))         return { command: 'stop' };
  if (/^stopall$/i.test(t))      return { command: 'stopall' };
  if (/^clear$/i.test(t))        return { command: 'clear' };
  if (/^(?:bookmarks|bm)$/i.test(t)) return { command: 'bookmarks' };
  if (/^bad$/i.test(t))          return { command: 'bad' };
  if (/^prompts$/i.test(t))      return { command: 'prompts' };
  if (/^status$/i.test(t))       return { command: 'status' };
  if (/^help$/i.test(t))         return { command: 'help' };
  if (/^remote$/i.test(t))       return { command: 'remote' };
  const mf = t.match(/^(?:f|filter)(?:\s+([\s\S]*))?$/i);
  if (mf) {
    const args = (mf[1] || '').trim();
    if (/^reset$/i.test(args)) return { command: 'filter_reset' };
    if (args && parseScopeInput(args) === null) return null;
    return { command: 'filter', args };
  }
  const m = t.match(/^deq(?:\s+(-?\d+))?$/i);
  if (m) return { command: 'deq', pos: m[1] != null ? parseInt(m[1]) : null };
  const mfj = t.match(/^(?:j|jump)\s+flow:\s*(\S+)$/i);
  if (mfj) return { command: 'jump', flowRunId: mfj[1] };
  const mj = t.match(/^(?:j|jump)\s+(?:msg:\s*)?(\d+)$/i);
  if (mj) return { command: 'jump', msgId: parseInt(mj[1], 10) };
  const ms = t.match(/^s(?:earch)?(?:\s+([\s\S]*))?$/i);
  if (ms) return { command: 'search', args: (ms[1] || '').trim() };
  const ml = t.match(/^login(?:\s+(\S+))?$/i);
  if (ml) return { command: 'login', harness: (ml[1] || '').toLowerCase() };
  return null;
}

async function handleCommand(cmd, topic, agent, adhoc = false, lookback = 0, opts = {}) {
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
  if (cmd.command === 'login') {
    const harness = cmd.harness || _agentToHarness(stickyChip?.agent);
    if (!harness || !HARNESS_LABELS[harness]) {
      showToast('Usage: /login [claude|codex|cursor|opencode]');
      return;
    }
    openAuthPanel(harness, null);
    return;
  }
  if (cmd.command === 'refresh') {
    doRefresh();
    return;
  }
  if (cmd.command === 'prompts') {
    togglePromptOnlyHistory();
    return;
  }
  if (cmd.command === 'bookmarks') {
    toggleBookmarkOnlyHistory();
    return;
  }
  if (cmd.command === 'bad') {
    toggleBadOnlyHistory();
    return;
  }
  if (cmd.command === 'restart') {
    await restartServer();
    return;
  }
  if (cmd.command === 'filter') {
    const scope = parseScopeInput(cmd.args);
    if (scope === null) {
      showCmdFeedback('Usage: /f [#topic[@agent[!|*]] | #topic@agent>@agent | @agent[!|*] | reset]');
      return;
    }
    if (scope?.flow_route) filterByFlowRoute(scope.flow_route);
    else if (scope) applyHistoryFilter(scope);
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
      showCmdFeedback('Usage: /s [#topic[@agent[!|*]] | #topic@agent>@agent | @agent[!|*] | #all] keywords…');
      return;
    }
    startSearch(cmd.args);
    return;
  }

  if (cmd.command === 'jump') {
    await jumpToMessage(cmd.msgId || null, cmd.flowRunId || null);
    return;
  }

  if (cmd.command === 'clear') {
    const routeTargets = opts.route ? _routePersistentSessionTargets(opts.route) : [];
    const clearTargets = opts.route ? routeTargets : [{ topic, agent }];
    if (!clearTargets.length) {
      showCmdFeedback(`${opts.route} — no persistent sessions to clear`);
      return;
    }
    const stoppingRows = await commandWouldStopRunningPrompt(cmd.command, topic, agent, opts.route ? clearTargets : null);
    if (stoppingRows.length) {
      const route = opts.route || (agent ? `#${topic}@${agent}` : `#${topic}`);
      const ok = await confirmRestartWithRunningPrompts(stoppingRows, {
        header: 'Clear Session',
        title: stoppingRows.length === 1 ? 'Running prompt will be stopped' : 'Running prompts will be stopped',
        copy: `Clearing ${route} will stop running session prompts in that scope before clearing.`,
        confirmLabel: 'Clear',
      });
      if (!ok) {
        showCmdFeedback(`${cmd.command} cancelled`);
        return;
      }
    }
    const feedbackEl = showCmdFeedback(`${cmd.command}…`);
    try {
      const cleared = [];
      let lastError = '';
      for (const target of clearTargets) {
        const body = { command: cmd.command, topic: target.topic };
        if (target.agent) body.agent = target.agent;
        const res = await fetch('/cmd', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const data = await res.json();
        if (!data.ok) { lastError = data.error || ''; continue; }
        const clearedAgent = data.agent || target.agent || null;
        clearCachedSessionId(target.topic, clearedAgent);
        _setKnownSessionTurnCount(target.topic, clearedAgent, 0);
        cleared.push(clearedAgent ? `#${target.topic}@${clearedAgent}` : `#${target.topic}`);
      }
      if (!cleared.length) { feedbackEl.textContent = `${cmd.command} failed: ${lastError}`; return; }
      if (opts.route) refreshRouteTurnCounts(opts.route, { force: true });
      if (pinPanel.classList.contains('open')) renderPinPanel();
      feedbackEl.textContent = `${cleared.join(', ')} — session${cleared.length === 1 ? '' : 's'} cleared`;
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

    const detail = cmd.command === 'stop'    ? `#${topic} — killed ${data.killed}`
                 : cmd.command === 'stopall' ? `#${topic} — killed ${data.killed}, drained ${data.drained}`
                 : `#${topic} — drained ${data.drained}`;
    feedbackEl.textContent = `${label} ${detail}`;
  } catch {
    feedbackEl.textContent = `${label} — request failed`;
  }
}

async function commandWouldStopRunningPrompt(command, topic, agent, targets = null) {
  if (command !== 'clear') return [];
  const targetSet = Array.isArray(targets) && targets.length
    ? new Set(targets.map(t => `${t.topic}@${t.agent || '_'}`))
    : null;
  try {
    const res = await fetch('/processes');
    if (!res.ok) return [];
    const rows = await res.json();
    if (!Array.isArray(rows)) return [];
    return rows.filter(row => {
      if (!row || isIdleProc(row)) return false;
      if (targetSet) {
        if (!targetSet.has(`${row.topic}@${row.agent || '_'}`)) return false;
      } else if (row.topic !== topic) return false;
      if (Boolean(row.adhoc)) return false;
      if (!targetSet && agent && row.agent !== agent) return false;
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

// Shared by /restart and the "Restart Server" menu item: confirms if prompts
// are actively running (they'd be killed), asks the backend to restart, then
// polls /health and hard-refreshes this tab once it's back.
async function restartServer() {
  const runningPrompts = await runningPromptsForRestart();
  const canUpgrade = _canInstallOnRestart && _updateInfo;
  let upgrade = canUpgrade && _updatesInstallOnRestart === 'always';
  let ok = true;
  if (runningPrompts.length) {
    ok = await confirmRestartWithRunningPrompts(runningPrompts);
  }
  if (ok && canUpgrade && _updatesInstallOnRestart === 'ask') {
    const choice = await confirmRestartWithRunningPrompts([], {
      header: 'Update AgentSquid',
      title: `AgentSquid v${_updateInfo.latest} is available`,
      copy: `Upgrade from v${_updateInfo.current} before restarting?`,
      confirmLabel: 'Upgrade and Restart',
      confirmResult: 'upgrade',
      secondaryLabel: 'Restart Without Upgrading',
      secondaryResult: 'restart',
    });
    ok = !!choice;
    upgrade = choice === 'upgrade';
  }
  if (!ok) {
    showCmdFeedback('restart cancelled');
    return;
  }
  const feedbackEl = showCmdFeedback(upgrade ? 'upgrading…' : 'restart…');
  try {
    const res = await fetch('/cmd', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: 'restart', topic: 'default', upgrade }),
    });
    const data = await res.json();
    if (!data.ok) { feedbackEl.textContent = upgrade ? `upgrade failed — ${data.error || 'restart cancelled'}` : 'restart failed'; return; }
    feedbackEl.textContent = 'restarting…';
    // Poll /health until server is back up, then hard-refresh this tab.
    const poll = async () => {
      try {
        const r = await fetch('/health');
        if (r.ok) { doRefresh(); return; }
      } catch {}
      setTimeout(poll, 500);
    };
    setTimeout(poll, 800);
  } catch {
    feedbackEl.textContent = 'restart — request failed';
  }
}

function confirmRestartWithRunningPrompts(rows, {
  header = 'Restart Squid',
  title = 'Running prompts will be stopped',
  copy = 'Restarting now will stop these active prompts before the server restarts.',
  confirmLabel = 'Restart',
  confirmResult = true,
  secondaryLabel = null,
  secondaryResult = false,
} = {}) {
  return new Promise(resolve => {
    const modal = document.getElementById('restart-modal');
    const list = document.getElementById('restart-modal-processes');
    const confirmBtn = document.getElementById('restart-modal-confirm');
    const secondaryBtn = document.getElementById('restart-modal-secondary');
    document.querySelector('#restart-modal .settings-label').textContent = header;
    document.getElementById('restart-modal-title').textContent = title;
    document.getElementById('restart-modal-copy').textContent = copy;
    confirmBtn.textContent = confirmLabel;
    if (secondaryLabel) {
      secondaryBtn.textContent = secondaryLabel;
      secondaryBtn.hidden = false;
      secondaryBtn.disabled = false;
    } else {
      secondaryBtn.hidden = true;
      secondaryBtn.textContent = '';
    }
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
    modal._confirmRestartResult = confirmResult;
    modal._secondaryRestartResult = secondaryResult;
  });
}

function closeRestartModal(ok = false) {
  const modal = document.getElementById('restart-modal');
  if (!modal) return;
  const resolve = modal._resolveRestart;
  modal._resolveRestart = null;
  modal._confirmRestartResult = true;
  modal._secondaryRestartResult = false;
  modal.classList.remove('open');
  if (resolve) resolve(ok);
}

function confirmAgentSessionClear(agentName, topics) {
  return new Promise(resolve => {
    const modal = document.getElementById('agent-session-modal');
    const confirmBtn = document.getElementById('agent-session-confirm');
    const title = document.getElementById('agent-session-modal-title');
    const copy = document.getElementById('agent-session-modal-copy');
    const topicList = document.getElementById('agent-session-modal-topics');
    title.textContent = `Save changes to "${agentName}"?`;
    copy.textContent = 'Changing runtime, model, cwd, or sandboxed HOME will clear active sessions for this agent.';
    topicList.textContent = (topics || []).map(topic => `#${topic}`).join(', ');
    const close = (ok) => {
      modal.classList.remove('open');
      resolve(ok);
    };
    modal._resolveAgentSession = close;
    modal.classList.add('open');
    confirmBtn.focus();
  });
}

function closeAgentSessionModal(ok = false) {
  const modal = document.getElementById('agent-session-modal');
  if (!modal) return;
  const resolve = modal._resolveAgentSession;
  modal._resolveAgentSession = null;
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
  const { topic, agent, adhoc, lookback, route, chainTarget, chainTargetFresh, chainOperator, chainRounds, chainTargetTopic, broadcastAgents, flowOrigins, message } = parseInput(text);
  if (!message) {
    input.value = '';
    resizeComposer();
    hideAutocomplete();
    setTopicChip(topic, agent, adhoc, lookback, { route, chainTarget, chainTargetFresh, chainOperator, chainRounds, chainTargetTopic, broadcastAgents, flowOrigins });
    return;
  }
  const cmd = parseCommand(message);
  if (cmd) {
    input.value = '';
    resizeComposer();
    hideAutocomplete();
    if (cmd.command === 'restart' || cmd.command === 'refresh') {
      // Both reload the page — clear the autosaved draft first, or the
      // 300ms debounce (below) may have already captured "/restart"/"/refresh"
      // itself, which initPromptHistory() would then restore into the input.
      clearTimeout(_draftSaveTimer);
      localStorage.removeItem('squid_draft');
    }
    await handleCommand(cmd, topic, agent, adhoc, lookback, { route });
    // Re-set chip after topic-scoped commands so next message stays in context
    if (['clear', 'stop', 'stopall', 'deq'].includes(cmd.command) && (topic !== 'default' || agent)) {
      setTopicChip(topic, agent, adhoc, lookback, { route, chainTarget, chainTargetFresh, chainOperator, chainRounds, chainTargetTopic, broadcastAgents, flowOrigins });
    }
    return;
  }
  input.value = '';
  resizeComposer();
  hideAutocomplete();
  if (searchActive) clearSearch();
  invalidateTopicsCache();
  invalidateTopicsManageCache();
  // Record what was literally typed, not the canonical/reduced route —
  // canonicalization is for backend storage/dedup (flow_route) and chain
  // matching, not for what autocomplete/arrow-up recall should show back.
  // A sticky chip has no route text in `text` at all (the composer holds
  // only the free-text message then), so that case still has to
  // reconstruct from the chip's own (already-canonical) route.
  recordPrompt(!stickyChip ? text : (route ? `${route} ${message}` : formatPromptHistoryEntry(topic, agent, adhoc, lookback, message)));
  localStorage.removeItem('squid_draft');
  if (broadcastAgents || (flowOrigins && flowOrigins.length > 1)) {
    sendOriginBroadcast(text);
  } else {
    sendMessage(text);
  }
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
  const broadcastAgents = stickyChip?.broadcastAgents || null;
  const singleTarget = broadcastAgents ? null : _currentContextTarget();
  const targets = broadcastAgents
    ? broadcastAgents.map(a => ({ topic: a.topic, agent: a.agent, adhoc: !!a.fresh }))
    : [singleTarget];

  // Each target's own session id + injected-into set — a bubble lights up if
  // it's in context for *any* currently active head, not just one derived
  // target (same "any head" rule as the pin-count badge — see updatePinCount).
  const perTargetCtx = targets.map(({ topic, agent, adhoc }) => {
    const taKey = `${topic}@${agent || '_'}`;
    const sid = (!adhoc && agent) ? (_sessionIds[taKey] || null) : null;
    const injected = sid ? (getInjectedInto()[sid] || []) : [];
    return { adhoc, sid, injected };
  });

  // Lookback selection is a single-target-only concept — broadcast heads
  // don't carry a numeric lookback.
  const activeItems = singleTarget?.adhoc && singleTarget.lookback > 0
    ? _activeLookbackItems(singleTarget.adhoc, singleTarget.lookback) : [];
  const activeIdSet = new Set(activeItems.map(i => i.id));

  document.querySelectorAll('#messages .history-item.assistant:not(.msg-thinking)').forEach(el => {
    const ctxSpan = el.querySelector('.user-ctx');
    const msgId = el.dataset.msgId ? parseInt(el.dataset.msgId) : null;

    // Orange dot = already in context (session continuity or prior
    // injection) for at least one active head, not "will inject."
    const inCtx = perTargetCtx.some(({ adhoc, sid, injected }) => {
      const wasInjected = !!(msgId && injected.includes(msgId));
      const sessionMatch = !adhoc && !!(sid && el.dataset.sessionId === sid);
      return sessionMatch || wasInjected;
    });
    const selectedForLookback = !!(singleTarget?.adhoc && activeIdSet.has(msgId));

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
  // An active chip already owns routing — same guard as parseInput's own
  // stickyChip branch. Without this, typing an ordinary message that
  // happens to start with "#word " (e.g. "#other hello") would silently
  // swap the active chip out from under the user on every keystroke, since
  // this listener fires on the raw input event, not just at submit time.
  // Intentional editing (clicking the chip, or backspacing into it) already
  // clears stickyChip first via clearTopicChip(), so this doesn't block that.
  if (stickyChip) return;
  const chainText = val.endsWith(' ') ? val.trim() : '';
  const chain = parseRouteChain(chainText);
  if (chain) {
    input.value = '';
    // route: the literal typed text, not chain.route (the canonicalized/
    // reduced spelling) — otherwise a redundant-but-explicit field (e.g.
    // `@echo` matching what a join would already infer) silently vanishes
    // from the chip the instant the route is promoted, before the user
    // even finishes the message.
    setTopicChip(chain.topic, chain.origin, chain.originFresh, 0, {
      route: chainText,
      chainTarget: chain.target,
      chainTargetFresh: chain.targetFresh,
      chainOperator: chain.operator,
      chainRounds: chain.rounds,
      chainTargetTopic: chain.targetTopic,
      flowOrigins: chain.origins,
    });
    hideAutocomplete();
    resizeComposer();
    return;
  }
  const broadcast = parseOriginBroadcast(chainText);
  if (broadcast) {
    input.value = '';
    setTopicChip(broadcast.topic, null, false, 0, {
      route: chainText,
      broadcastAgents: broadcast.agents,
    });
    hideAutocomplete();
    resizeComposer();
    return;
  }
  const m = val.match(new RegExp(`^#(${TOPIC_SLUG_SRC})(?:@(${AGENT_SLUG_SRC}))?(!\\d*)? $`));
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
  // editingExpandedSlug means the user explicitly opened the route for
  // editing (click-to-edit, or backspacing into it) — clearTopicChip()
  // already cleared stickyChip for that flow, so this proceeds regardless.
  // Outside of that, only auto-promote a completed "#route message" when
  // there's no chip already active: an active chip already owns routing
  // (same rule as _maybePromoteSlug and parseInput's own stickyChip
  // branch), so ordinary message text that happens to start with "#" must
  // not silently swap it out from under the user.
  if (!editingExpandedSlug && (!allowCompletedPrompt || stickyChip)) return;

  const val = input.value;
  const chainMatch = val.match(/^(\S+)\s+([\s\S]+)$/);
  const chain = chainMatch ? parseRouteChain(chainMatch[1]) : null;
  if (chain) {
    const prompt = chainMatch[2];
    const promptStart = val.length - prompt.length;
    if (!force && (input.selectionStart < promptStart || input.selectionEnd < promptStart)) return;
    const selectionStart = Math.max(0, input.selectionStart - promptStart);
    const selectionEnd = Math.max(0, input.selectionEnd - promptStart);
    setTopicChip(chain.topic, chain.origin, chain.originFresh, 0, {
      // Literal typed token, not chain.route (canonical) — see
      // _maybePromoteSlug for why.
      route: chainMatch[1],
      chainTarget: chain.target,
      chainTargetFresh: chain.targetFresh,
      chainOperator: chain.operator,
      chainRounds: chain.rounds,
      chainTargetTopic: chain.targetTopic,
      flowOrigins: chain.origins,
    });
    input.value = prompt;
    input.setSelectionRange(selectionStart, selectionEnd);
    input.dispatchEvent(new Event('input'));
    return;
  }
  const broadcastMatch = val.match(new RegExp(`^(${_BROADCAST_ROUTE_DETECT_SRC})\\s+([\\s\\S]+)$`));
  const broadcast = broadcastMatch ? parseOriginBroadcast(broadcastMatch[1]) : null;
  if (broadcast) {
    const prompt = broadcastMatch[2];
    const promptStart = val.length - prompt.length;
    if (!force && (input.selectionStart < promptStart || input.selectionEnd < promptStart)) return;
    const selectionStart = Math.max(0, input.selectionStart - promptStart);
    const selectionEnd = Math.max(0, input.selectionEnd - promptStart);
    setTopicChip(broadcast.topic, null, false, 0, {
      route: broadcastMatch[1],
      broadcastAgents: broadcast.agents,
    });
    input.value = prompt;
    input.setSelectionRange(selectionStart, selectionEnd);
    input.dispatchEvent(new Event('input'));
    return;
  }
  const m = val.match(new RegExp(`^#(${TOPIC_SLUG_SRC})(?:@(${AGENT_SLUG_SRC}))?(!(\\d*))? ([\\s\\S]+)$`));
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
  _maybeCollapseExpandedSlug(false, true);
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
  // An active chip already owns routing (see updateAutocomplete/
  // _maybeCollapseExpandedSlug), so "#topic@agent" typed as ordinary message
  // text no longer functions as a route unless the chip is explicitly
  // opened for editing first. Deleting a whole "@agent" in one backspace
  // would be misleading in that state — treat it as regular text instead.
  if (stickyChip && !editingExpandedSlug) return false;
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

  if (caret === routeEnd && new RegExp(`^#${TOPIC_SLUG_SRC}@${AGENT_SLUG_SRC}!\\d+$`).test(route)) return false;

  if (caret === routeEnd && route.endsWith('!')) {
    nextRoute = route.slice(0, -1);
    nextCaret = nextRoute.length;
  } else {
    const before = route.slice(0, caret);
    const after = route.slice(caret);
    const agentMatch = before.match(new RegExp(`^(#${TOPIC_SLUG_SRC}@)${AGENT_SLUG_SRC}$`));
    const topicMatch = before.match(new RegExp(`^(#)${TOPIC_SLUG_SRC}$`));
    const chainTargetMatch = before.match(new RegExp(`^(#${TOPIC_SLUG_SRC}@${AGENT_SLUG_SRC}!?(?:(?:<>)|=>|>)@)${AGENT_SLUG_SRC}$`));
    const chainOriginMatch = before.match(new RegExp(`^(#${TOPIC_SLUG_SRC}@)${AGENT_SLUG_SRC}$`));
    // Origin Broadcast (ADR-0032): drop the trailing `,#topic`, `,@agent`, or
    // `,#topic@agent` segment as one unit, same as a chain target —
    // otherwise a comma-list route can only be trimmed one character at a
    // time. (Trailing `!` on that segment is stripped by the top-of-function
    // check on an earlier backspace press, same as everywhere else here.)
    const _atom = `(?:#${TOPIC_SLUG_SRC}@${AGENT_SLUG_SRC}|#${TOPIC_SLUG_SRC}|@${AGENT_SLUG_SRC})`;
    const broadcastTailMatch = before.match(new RegExp(`^(#${TOPIC_SLUG_SRC}@${AGENT_SLUG_SRC}!?(?:,${_atom}!?)*),(${_atom})$`));

    if (broadcastTailMatch && (caret === routeEnd || after.startsWith('!'))) {
      nextRoute = broadcastTailMatch[1] + after;
      nextCaret = broadcastTailMatch[1].length;
    } else if (chainTargetMatch && (caret === routeEnd || after.startsWith('!'))) {
      nextRoute = chainTargetMatch[1] + after;
      nextCaret = chainTargetMatch[1].length;
    } else if (before.endsWith('@') && new RegExp(`^#${TOPIC_SLUG_SRC}@${AGENT_SLUG_SRC}!?(?:(?:<>)|=>|>)@$`).test(before) && (caret === routeEnd || after.startsWith('!'))) {
      nextRoute = before.slice(0, -2) + after;
      nextCaret = before.length - 2;
    } else if (chainOriginMatch && (after.startsWith('>@') || after.startsWith('<>@') || after.startsWith('!>@') || after.startsWith('!<>@'))) {
      nextRoute = chainOriginMatch[1] + after;
      nextCaret = chainOriginMatch[1].length;
    } else if (agentMatch && (caret === routeEnd || after.startsWith('!'))) {
      nextRoute = agentMatch[1] + after;
      nextCaret = agentMatch[1].length;
    } else if (topicMatch && (caret === routeEnd || after.startsWith('@') || after.startsWith('!'))) {
      // Dotted topic names are hierarchical; trim only the last segment so
      // "#parent.child" backs up to "#parent." instead of clearing the topic.
      const lastDot = before.lastIndexOf('.');
      nextCaret = lastDot > 0 ? lastDot + 1 : topicMatch[1].length;
      nextRoute = before.slice(0, nextCaret) + after;
    } else if (before.endsWith('@') && new RegExp(`^#${TOPIC_SLUG_SRC}@$`).test(before) && (caret === routeEnd || after.startsWith('!'))) {
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
  // Dismiss one surface at a time, highest visual z-order first.
  // Modals (cover everything)
  const restartModal = document.getElementById('restart-modal');
  if (restartModal?.classList.contains('open')) { closeRestartModal(false); return true; }
  if (document.getElementById('agent-session-modal')?.classList.contains('open')) { closeAgentSessionModal(false); return true; }
  const msgModal = document.getElementById('msg-modal');
  if (msgModal?.classList.contains('open')) { msgModal.classList.remove('open'); return true; }
  if (document.getElementById('memory-modal')?.classList.contains('open')) { closeMemoryEditor(); return true; }
  if (document.getElementById('topic-delete-modal')?.classList.contains('open')) { closeTopicDeleteModal(); return true; }
  if (document.getElementById('preset-name-modal')?.classList.contains('open')) { _closePresetNameModal(null); return true; }
  // Floating popup
  if (procStatusPopup?.classList.contains('open')) { procStatusPopup.classList.remove('open'); return true; }
  // Search overlay
  if (searchActive) { clearSearch(); return true; }
  // Inline panels (below search, above composer)
  if (pinPanel.classList.contains('open')) { closePinPanel({ restoreFocus: true }); return true; }
  if (authPanel.classList.contains('open')) { closeAuthPanel(); return true; }
  if (helpPanel.classList.contains('open')) { closeHelp(); return true; }
  // Advisory bar (lowest)
  if (!sessionAdvisoryEl.hidden) { if (_advisoryDismissKey) localStorage.setItem(_advisoryDismissKey, '1'); sessionAdvisoryEl.hidden = true; return true; }
  return false;
}

input.addEventListener('keydown', (e) => {
  if (e.key === 'Backspace' && semanticRouteBackspace()) {
    e.preventDefault();
    return;
  }
  if (e.key === 'ArrowUp' && commandEditRestore !== null) {
    e.preventDefault();
    restoreStashedInput();
    return;
  }
  if (e.key === 'Tab' && !sessionAdvisoryEl.hidden) { e.preventDefault(); stashComposerAndEdit('/clear'); return; }
  if (acOpen) {
    if (acItems[0]?.routeTarget && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
      e.preventDefault();
      if (e.key === 'ArrowLeft') {
        acSel = Math.min(acSel + 1, acItems.length - 1);
      } else {
        acSel = Math.max(acSel - 1, 0);
      }
      _acHighlight();
      _acPreview();
      return;
    }
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
  if (!acOpen && composerHasOnlyRoute() && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    const direction = e.key === 'ArrowLeft' ? 'previous' : 'next';
    if (openRouteHistoryAutocomplete(direction)) {
      e.preventDefault();
      return;
    }
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
    let tag = stickyChip.route || `#${stickyChip.topic}`;
    if (!stickyChip.route) {
      if (stickyChip.agent) tag += `@${stickyChip.agent}`;
      if (stickyChip.adhoc) tag += `!${stickyChip.lookback || ''}`;
    }
    clearTopicChip();
    editingExpandedSlug = true;
    expandedSlugEditToken++;
    input.value = prompt ? `${tag} ${prompt}` : tag;
    input.setSelectionRange(tag.length, tag.length);
    input.dispatchEvent(new Event('input'));
  }
});

// ── Auth sessions (ADR-0035) ────────────────────────────────────────────────
// A harness whose account isn't logged in fails a turn with an error whose
// text is tagged "[[cli-auth-required:<harness>]] ..." by the server
// (agent/topic_queue.py). This offers an inline "Log in" button on that
// error that swaps the composer (#form) for an embedded xterm.js panel
// running the harness's own login command through the scoped-PTY endpoints
// (agent/auth_sessions.py) — never a general shell, always one fixed
// allowlisted command chosen server-side from the harness id alone.
const authPanel = document.getElementById('auth-panel');
const authPanelTitle = document.getElementById('auth-panel-title');
const authPanelTerm = document.getElementById('auth-panel-term');
const authPanelCancelBtn = document.getElementById('auth-panel-cancel-btn');
const authPanelRetryBtn = document.getElementById('auth-panel-retry-btn');
const agentsAuthPanel = document.getElementById('agents-auth-panel');
const agentsAuthPanelTitle = document.getElementById('agents-auth-panel-title');
const agentsAuthPanelTerm = document.getElementById('agents-auth-panel-term');
const agentsAuthPanelCancelBtn = document.getElementById('agents-auth-panel-cancel-btn');
const agentsAuthPanelRetryBtn = document.getElementById('agents-auth-panel-retry-btn');
const agentsAuthPanelHome = document.getElementById('agents-auth-panel-home');
let _authSession = null; // { id, harness, es, term, onSuccessRetry }

const HARNESS_LABELS = { claudecode: 'Claude Code', codex: 'Codex', cursor: 'Cursor', opencode: 'OpenCode', pi: 'Pi' };

// Map agent slugs (claude, codex, …) to harness ids (claudecode, …).
// claude/deepseek/cc-deepseek/deepcla/claude-live all run on the claudecode harness.
function _agentToHarness(agent) {
  if (!agent) return null;
  const a = agent.toLowerCase();
  if (a === 'claude' || a === 'claude-live' || a === 'deepseek' || a === 'cc-deepseek' || a === 'deepcla') return 'claudecode';
  if (HARNESS_LABELS[a]) return a;
  return null;
}

function parseAuthRequiredError(text) {
  const m = /^\[\[cli-auth-required:([a-z]+)\]\]\s*/.exec(text || '');
  if (!m) return null;
  return { harness: m[1], message: text.slice(m[0].length) };
}

function authLoginButtonHtml(harness) {
  const label = HARNESS_LABELS[harness] || harness;
  return `<button type="button" class="auth-login-btn" data-harness="${harness}">Log in to ${label}</button> `;
}

function wireAuthLoginButtons(root, onSuccessRetry) {
  root.querySelectorAll('.auth-login-btn').forEach(btn => {
    btn.addEventListener('click', () => openAuthPanel(btn.dataset.harness, onSuccessRetry));
  });
}

function base64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function _authPanelTitle(harness, mode, model) {
  const label = HARNESS_LABELS[harness] || (harness === 'ollama' ? 'Ollama' : harness);
  if (mode === 'install') return `Install — ${label}`;
  if (mode === 'pull') return `Pulling ${model} — Ollama`;
  if (mode === 'remove') return `Removing ${model} — Ollama`;
  return `Log in — ${label}`;
}

function _authPanelDoneTitle(harness, mode, model) {
  const label = HARNESS_LABELS[harness] || (harness === 'ollama' ? 'Ollama' : harness);
  if (mode === 'install') return `Installed — ${label}`;
  if (mode === 'pull') return `Pulled ${model} — Ollama`;
  if (mode === 'remove') return `Removed ${model} — Ollama`;
  return `Done — ${label}`;
}

function _setCatalogOperationBusy(busy) {
  document.querySelectorAll('.bcat-install-btn, .bcat-pull-btn, .bcat-rm-btn').forEach(btn => {
    if (busy) {
      btn.dataset.operationWasDisabled = btn.disabled ? '1' : '0';
      btn.disabled = true;
    } else if (btn.dataset.operationWasDisabled !== undefined) {
      btn.disabled = btn.dataset.operationWasDisabled === '1';
      delete btn.dataset.operationWasDisabled;
    }
  });
}

async function openAuthPanel(harness, onSuccessRetry, opts = {}) {
  const mode = opts.mode || 'login';
  const model = opts.model || null;
  const isCatalogOperation = mode !== 'login';
  const panel = isCatalogOperation ? agentsAuthPanel : authPanel;
  const panelTitle = isCatalogOperation ? agentsAuthPanelTitle : authPanelTitle;
  const panelTerm = isCatalogOperation ? agentsAuthPanelTerm : authPanelTerm;
  const panelRetryBtn = isCatalogOperation ? agentsAuthPanelRetryBtn : authPanelRetryBtn;
  const anchor = opts.anchor || (isCatalogOperation ? _authSession?.anchor : null);
  if (_authSession) await closeAuthPanel({ refreshCatalog: false });
  if (isCatalogOperation) {
    if (anchor?.isConnected) anchor.after(panel);
    else agentsAuthPanelHome.before(panel);
  }
  if (isCatalogOperation) _setCatalogOperationBusy(true);
  if (!isCatalogOperation) form.classList.add('dimmed');
  panel.classList.add('open');
  panelRetryBtn.hidden = true;
  panelTitle.textContent = _authPanelTitle(harness, mode, model);
  panelTerm.innerHTML = '';

  const term = new Terminal({
    convertEol: true,
    // No fixed cols/rows: a hardcoded 100 cols was both too narrow (Claude's
    // login URL wrapped out of view since the chat composer is well under
    // 100 cols * ~8px on desktop, let alone mobile) and, at 10 rows, too
    // short — opencode's provider-picker (7+ entries) needs more than that,
    // and once its frame exceeds the PTY's row count the terminal has to
    // scroll, desyncing opencode's relative-cursor incremental redraw
    // (reproduced outside Squid with a plain VT100 emulator, pyte). Fit to
    // the actual #auth-panel-term box below instead.
    fontSize: 13,
    fontFamily: 'ui-monospace, SFMono, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
    theme: { background: '#13131c' },
  });
  term.open(panelTerm);
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  // Compat shim: addon-fit reads `_core.viewport.scrollBarWidth`, but the
  // vendored xterm.min.js build (version unpinned) neither exposes a public
  // `viewport` alias for its private `_viewport` nor tracks a scrollBarWidth
  // on it at all (its scrollbar appears to be a CSS overlay now, not
  // JS-reserved layout space). Without this, proposeDimensions() computes
  // NaN, fit() silently no-ops, and the terminal stays at xterm's 80x24
  // default instead of the panel's real size — and earlier, before this
  // whole shim existed, it threw outright reading `.scrollBarWidth` of
  // undefined, which aborted openAuthPanel before it ever spawned the login
  // process (why /login appeared to do nothing).
  if (term._core && term._core.viewport == null) {
    term._core.viewport = { scrollBarWidth: term._core._viewport?.scrollBarWidth ?? 0 };
  }
  const fitTerminal = () => {
    fitAddon.fit();
    const minCols = isCatalogOperation ? 30 : 20;
    const cols = Math.max(minCols, Math.min(500, term.cols));
    const rows = isCatalogOperation ? 10 : Math.max(5, Math.min(200, term.rows));
    if (cols !== term.cols || rows !== term.rows) term.resize(cols, rows);
  };
  // #auth-panel just flipped from display:none to flex this same tick, so
  // its box has no layout yet — give it one frame before fit() measures it.
  await new Promise(resolve => requestAnimationFrame(resolve));
  fitTerminal();
  // Catalog commands render animated progress against the PTY width they
  // start with. Keep at least a small usable grid on very narrow phones;
  // the Agents panel scrolls horizontally when the viewport cannot fit it.
  term.focus();

  // Make URLs clickable — avoids macOS "open with" system modal.
  term.registerLinkProvider({
    provideLinks(y, callback) {
      const line = term.buffer.active.getLine(y);
      if (!line) { callback([]); return; }
      const text = line.translateToString();
      if (!text) { callback([]); return; }
      const links = [];
      const re = /https?:\/\/[^\x00-\x1f\x7f\s<>"{}|\\^`]+/g;
      let m;
      while ((m = re.exec(text)) !== null) {
        const url = m[0];
        links.push({
          text: url,
          range: { start: { x: m.index, y }, end: { x: m.index + url.length, y } },
          activate: (_ev, text) => { window.open(text, '_blank'); },
        });
      }
      callback(links);
    },
  });

  // Esc closes the auth panel (xterm.js swallows Esc for escape sequences,
  // so it never reaches the document-level closeEscSurfaces handler).
  term.attachCustomKeyEventHandler((e) => {
    if (e.type === 'keydown' && e.key === 'Escape') { closeAuthPanel(); return false; }
    return true;
  });

  // Re-fit on viewport/panel-width changes (e.g. rotating a phone, resizing
  // the browser) and push the new size to the live PTY so the harness's TUI
  // reflows instead of staying pinned to whatever size it started at.
  let resizeTimer = null;
  const handleResize = () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!_authSession || _authSession.term !== term) return;
      // ollama's progress renderer handles SIGWINCH poorly mid-frame. Keep
      // both xterm and its PTY at their initial grid until the command exits.
      if (isCatalogOperation && _authSession.running) return;
      const prevCols = term.cols, prevRows = term.rows;
      fitTerminal();
      if (!isCatalogOperation && _authSession.running && _authSession.id &&
          (term.cols !== prevCols || term.rows !== prevRows)) {
        fetch(`/auth/session/${_authSession.id}/resize`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ cols: term.cols, rows: term.rows }),
        }).catch(() => {});
      }
    }, 150);
  };
  window.addEventListener('resize', handleResize);

  _authSession = { id: null, harness, mode, model, panel, panelTitle, panelTerm,
    panelRetryBtn, anchor, term, fitAddon, handleResize, onSuccessRetry, running: true };

  let res, data;
  try {
    // cols/rows travel with the spawn request itself (not a follow-up
    // resize call) so the PTY has its real size before the child process
    // execs — otherwise the harness's TUI can render its first frame at
    // the PTY's default size and get a SIGWINCH mid-render once the resize
    // arrives, which is what corrupted the opencode login list on up-arrow.
    res = await fetch('/auth/session', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ harness, cols: term.cols, rows: term.rows, mode, model }),
    });
    data = await res.json();
  } catch (err) {
    res = null;
    data = { error: String(err) };
  }
  if (!res || !res.ok) {
    term.write((data.error || 'Failed to start').replace(/\n/g, '\r\n'));
    panelTitle.textContent = data.error || 'Failed to start';
    panelRetryBtn.hidden = false;
    _authSession.running = false;
    if (isCatalogOperation) _setCatalogOperationBusy(false);
    return;
  }

  // The PTY execs its allowlisted argv directly, without a shell prompt to
  // echo it. Show the exact server-derived command before replay/live output.
  term.write(`$ ${data.command}\r\n\r\n`);
  const es = new EventSource(`/auth/session/${data.id}/events`);
  _authSession = { id: data.id, harness, mode, model, panel, panelTitle, panelTerm,
    panelRetryBtn, anchor, es, term, fitAddon, handleResize, onSuccessRetry, running: true };

  es.addEventListener('data', event => {
    try { term.write(base64ToBytes(event.data)); } catch {}
  });
  es.addEventListener('exit', event => {
    es.close();
    if (!_authSession || _authSession.id !== data.id) return;
    _authSession.running = false;
    if (isCatalogOperation) _setCatalogOperationBusy(false);
    const code = parseInt(event.data, 10);
    if (code === 0) {
      const retry = _authSession?.onSuccessRetry;
      const finishedMode = _authSession?.mode;
      if (finishedMode && finishedMode !== 'login') {
        panelTitle.textContent = _authPanelDoneTitle(harness, mode, model);
        // The process is gone, so it is now safe to fit the retained result
        // to the current viewport without sending SIGWINCH to ollama.
        fitTerminal();
      } else {
        closeAuthPanel();
        if (retry) retry();
      }
    } else {
      panelTitle.textContent = `Exited (${code})`;
      panelRetryBtn.hidden = false;
      if (isCatalogOperation) {
        fitTerminal();
      }
    }
  });

  term.onData(input => {
    if (!_authSession?.id) return;
    fetch(`/auth/session/${_authSession.id}/input`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: input }),
    }).catch(() => {});
  });
}

async function closeAuthPanel({ refreshCatalog = true } = {}) {
  const session = _authSession;
  _authSession = null;
  if (session) {
    if (session.handleResize) window.removeEventListener('resize', session.handleResize);
    if (session.es) session.es.close();
    // The endpoint only signals a running child; for a completed session it
    // simply releases the retained server-side replay buffer immediately.
    if (session.id) fetch(`/auth/session/${session.id}/cancel`, { method: 'POST' }).catch(() => {});
    if (session.term) session.term.dispose();
  }
  (session?.panel || authPanel).classList.remove('open');
  const wasCatalogOperation = session?.mode && session.mode !== 'login';
  if (wasCatalogOperation) {
    _setCatalogOperationBusy(false);
    agentsAuthPanelHome.before(agentsAuthPanel);
    if (refreshCatalog && typeof loadAgents === 'function') loadAgents();
  }
  form.classList.remove('dimmed');
}

authPanelCancelBtn.addEventListener('click', () => closeAuthPanel());
agentsAuthPanelCancelBtn.addEventListener('click', () => closeAuthPanel());
function retryAuthSession() {
  const harness = _authSession?.harness;
  const retry = _authSession?.onSuccessRetry;
  const mode = _authSession?.mode;
  const model = _authSession?.model;
  const anchor = _authSession?.anchor;
  if (harness) openAuthPanel(harness, retry, { mode, model, anchor });
}
authPanelRetryBtn.addEventListener('click', retryAuthSession);
agentsAuthPanelRetryBtn.addEventListener('click', retryAuthSession);

async function sendMessage(text, opts = {}) {
  const source = opts.source === 'workflow' || opts.source === 'diff_viewer' ? opts.source : 'human';
  const updateComposerRoute = source === 'human' && opts.updateComposerRoute !== false;
  const suppressChipTurnCount = !!opts.suppressChipTurnCount;
  const parsed = parseInput(text);
  const { lookback, route, chainTarget, chainTargetFresh, chainOperator, chainRounds, chainTargetTopic, broadcastAgents, flowOrigins, message } = parsed;
  // Origin Broadcast (ADR-0032): N independent origins, one per sendMessage
  // call — the orchestrator (sendOriginBroadcast) tells each call which of
  // the parsed broadcastAgents it's responsible for via opts.broadcastTarget.
  // Each target carries its own topic too (rolling-anchor resolution — see
  // parseOriginBroadcast — can send different targets to different topics),
  // not just parsed.topic, which is only the group's first/anchor topic.
  const topic = opts.broadcastTarget ? opts.broadcastTarget.topic : parsed.topic;
  const agent = opts.broadcastTarget ? opts.broadcastTarget.agent : parsed.agent;
  const adhoc = opts.broadcastTarget ? !!opts.broadcastTarget.fresh : parsed.adhoc;
  const flowRoute = route ? canonicalFlowRoute(route) : canonicalFlowRoute(opts.flowRoute);
  const chipDisplayFlowRoute = source === 'human' && topicChipEl?.classList.contains('route-chain')
    ? topicChipEl.textContent.trim()
    : null;
  const displayFlowRoute = opts.displayFlowRoute || flowRoute || route || opts.flowRoute || stickyChip?.route || chipDisplayFlowRoute || null;
  let flowRunId = flowRoute ? (opts.flowRunId || null) : null;
  let flowRunIdEmitted = false;
  if (updateComposerRoute) {
    setTopicChip(topic, agent, adhoc, lookback, { route, chainTarget, chainTargetFresh, chainOperator, chainRounds, chainTargetTopic, broadcastAgents, flowOrigins, suppressTurnCount: suppressChipTurnCount });
  }
  const sendTime = new Date().toISOString();
  // A search scope can't be evaluated against a message that isn't in the DB yet, so keep
  // the live group hidden while searching. A filter scope, on the other hand, can be checked
  // client-side against this message's own topic/agent/adhoc — show it if it matches.
  const liveHiddenByScope = searchActive ||
    (hasHistoryFilterScope() && !itemMatchesFilter({ topic, agent, adhoc, flow_route: flowRoute }, historyFilter));

  let chainMarker = null;
  const renderSuppressedHeadMarker = opts.suppressUserBubble && flowOrigins && flowOrigins.length > 1;
  if (source === 'human' && route && chainTarget && (!opts.suppressUserBubble || renderSuppressedHeadMarker)) {
    const markerRoute = routeChainMarkerRouteForHead(route, topic, agent, adhoc);
    chainMarker = makeRouteChainMarker(markerRoute, {
      turnCounts: _routeChainTurnCounts(topic, agent, adhoc, chainTarget, chainTargetFresh, chainTargetTopic),
    });
    chainMarker.dataset.topic = topic;
    chainMarker.dataset.flowRoute = flowRoute;
    if (agent) chainMarker.dataset.agent = agent;
    if (adhoc) chainMarker.dataset.adhoc = '1';
    if (liveHiddenByScope) chainMarker.classList.add('live-hidden');
  }
  // Origin Broadcast: the same literal prompt goes to every target, so only
  // the first target's call renders the shared user bubble — the rest reuse
  // it via opts.suppressUserBubble instead of duplicating it per agent.
  const userBubble = opts.suppressUserBubble ? null : makeUserBubble(message, topic, agent, null, adhoc, lookback, source, broadcastAgents, displayFlowRoute);
  const userTopicTag = userBubble ? userBubble.querySelector('.topic-tag') : null;
  // The live user bubble now renders the route flow itself (displayFlowRoute),
  // so it already reads as the one prompt driving every masked head — the
  // start marker is a graph annotation on top of it, not a header above it.
  // It goes after the bubble it belongs to (and, for a broadcast, above the
  // bubble-less per-head markers appended by the sibling sendMessage calls
  // that follow this one) regardless of single- vs multi-head.
  if (userBubble) {
    messages.appendChild(userBubble);
    const userTimeEl = addTimestamp(userBubble, sendTime, true);
    if (liveHiddenByScope) {
      userBubble.classList.add('live-hidden');
      userTimeEl?.classList.add('live-hidden');
    }
    requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });

    // Non-blocking nudge — fires async after the message is already in flight
    maybeShowCodeRootsNudge(topic, userBubble);
  }
  if (chainMarker) messages.appendChild(chainMarker);

  // ── Thinking bubble (visible immediately, shows status/queue/loader) ──────────
  // Same route+prompt header shape as the history/WIP bubble (makeWipBubble,
  // appendHistoryItem) — so a live-in-flight turn already reads the way it
  // will after a refresh, instead of a bare loader with no context until the
  // first content chunk reveals a separate response bubble below.
  const thinkingBubble = document.createElement('div');
  thinkingBubble.className = 'msg assistant msg-thinking';
  thinkingBubble.dataset.topic = topic;
  if (flowRoute) thinkingBubble.dataset.flowRoute = flowRoute;
  if (agent) thinkingBubble.dataset.agent = agent;
  if (adhoc) thinkingBubble.dataset.adhoc = '1';
  if (liveHiddenByScope) thinkingBubble.classList.add('live-hidden');
  const thinkingHeader = document.createElement('div');
  thinkingHeader.className = 'response-header';
  const thinkingHeaderText = document.createElement('span');
  thinkingHeaderText.className = 'response-header-text';
  let thinkingHeaderTag = makeTopicTag(topic, agent, { adhoc, lookback });
  thinkingHeaderText.appendChild(thinkingHeaderTag);
  thinkingHeaderText.appendChild(document.createTextNode('  '));
  const { promptToggle: thinkingPromptToggle, promptFullDiv: thinkingPromptFullDiv } = makeHistoryPromptToggle(message);
  thinkingHeaderText.appendChild(thinkingPromptToggle);
  thinkingHeader.appendChild(thinkingHeaderText);
  thinkingBubble.appendChild(thinkingHeader);
  thinkingBubble.appendChild(thinkingPromptFullDiv);
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
      renderCancelledThinking('Dequeued.');
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
  addThinkingHeightButton(thinkingBubble);

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
    updateThinkingHeightButton(thinkingBubble);
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
      toggle.addEventListener('click', () => requestAnimationFrame(() => updateThinkingHeightButton(thinkingBubble)));
      thinkingBubble.style.display = '';
      thinkingBubble.classList.add('msg-thinking-done');
      updateThinkingHeightButton(thinkingBubble);
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
  if (flowRoute) bubble.dataset.flowRoute = flowRoute;
  if (agent) bubble.dataset.agent = agent;
  const responseHeader = document.createElement('div');
  responseHeader.className = 'response-header';
  let responseHeaderTag = makeTopicTag(topic, agent, { adhoc, lookback });
  const headerText = document.createElement('span');
  headerText.className = 'response-header-text';
  headerText.appendChild(responseHeaderTag);
  headerText.appendChild(document.createTextNode('  '));
  const { promptToggle: responsePromptToggle, promptFullDiv: responsePromptFullDiv } = makeHistoryPromptToggle(message);
  headerText.appendChild(responsePromptToggle);
  responseHeader.appendChild(headerText);
  const liveCtxSpan = document.createElement('span');
  liveCtxSpan.className = 'user-ctx';
  setCtxLabel(liveCtxSpan, adhoc);
  liveCtxSpan.dataset.topic = topic;
  if (flowRunId) liveCtxSpan.dataset.flowRunId = flowRunId;
  liveCtxSpan.addEventListener('click', e => { e.stopPropagation(); showCtxPopup(liveCtxSpan); });
  responseHeader.appendChild(liveCtxSpan);
  bubble.appendChild(responseHeader);
  bubble.appendChild(responsePromptFullDiv);
  const contentDiv = document.createElement('div');
  bubble.appendChild(contentDiv);

  // The response bubble lands wherever `messages` happens to end at the
  // moment its first content arrives — not necessarily right after
  // thinkingBubble's old slot, since concurrent heads (Origin Broadcast) or
  // other topics can append in between while this one is still streaming.
  // chainMarker was placed right before thinkingBubble, which gets removed
  // once this bubble is ready (removeThinking/freezeThinking) — so without
  // relocating it here too, the marker is orphaned above whatever now sits
  // in that old slot instead of above its own response. insertBefore moves
  // an already-attached node rather than duplicating it, so this is safe to
  // call every time the bubble is (re)placed.
  function placeResponseBubble() {
    if (!bubble.parentNode) messages.appendChild(bubble);
    if (chainMarker) messages.insertBefore(chainMarker, bubble);
  }

  let firstDataReceived = false;

  let quotaBackend = await resolveQuotaProvider(topic, agent);
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
  let quotaFinalized = false;

  async function finalizeQuotaTracking() {
    if (quotaFinalized) return;
    quotaFinalized = true;
    // Quota is a backend-wide meter, so this before/after difference is only an
    // observational signal. Parallel prompts have overlapping windows and can
    // double-count each other's usage; provider reporting lag can shift usage to
    // a later turn. Do not treat or aggregate it as exact per-prompt attribution.
    // See ADR-0023.
    await new Promise(r => setTimeout(r, 1000));
    const quotaAfterSnapshot = await fetchQuotaForBackend(quotaBackend);
    // Balance-based gauges (DeepSeek/Kimi) with a max budget: use the computed
    // percentage so the delta represents budget-% change, not raw dollars.
    const usePct = isBalanceGauge(quotaBackend)
      && quotaBeforeSnapshot?.pct != null && quotaAfterSnapshot?.pct != null;
    const quotaBefore = usePct ? quotaBeforeSnapshot.pct : (quotaBeforeSnapshot?.raw ?? null);
    const quotaAfter = usePct ? quotaAfterSnapshot.pct : (quotaAfterSnapshot?.raw ?? null);
    if (quotaBefore !== null && quotaAfter !== null && quotaAfter !== quotaBefore) {
      // Balance-based gauges report a decreasing balance; flip sign so usage
      // always shows as positive (consistent with utilization % gauges).
      const isBalanceMeter = isBalanceGauge(quotaBackend) && !usePct;
      const rawDiff = quotaAfter - quotaBefore;
      const d = Math.round((isBalanceMeter ? -rawDiff : rawDiff) * 10) / 10;
      if (statsEl && d > 0) {
        const deltaEl = statsEl.querySelector('.stats-quota-delta');
        deltaEl.textContent = `  ·  +${d} pp`;
        deltaEl.title = 'Observed account quota-meter change; not exact message usage';
      }
      // Persist the sign exactly as intended. The DB layer has no backend/gauge
      // context, so balance snapshots are normalized here instead.
      const recordQuotaBefore = isBalanceMeter && quotaBefore > quotaAfter ? quotaAfter : quotaBefore;
      const recordQuotaAfter = isBalanceMeter && quotaBefore > quotaAfter ? quotaBefore : quotaAfter;
      if (msgId) {
        fetch(`/chat/${msgId}/quota-delta`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ before: recordQuotaBefore, after: recordQuotaAfter }),
        }).catch(() => {});
      }
      if (lastSessionId) {
        fetch('/stats/quota-delta', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: lastSessionId, before: recordQuotaBefore, after: recordQuotaAfter }),
        }).catch(() => {});
      }
    }
    quotaTrackEnd(quotaBackend);
  }

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

  function showError(errText) {
    revealResponseBubble();  // sets firstDataReceived, suppresses finally fallback
    const authReq = parseAuthRequiredError(errText);
    const errDisplay = normalizedErrorDisplay(authReq ? authReq.message : errText);
    // Don't wipe streamed content with a generic fallback message
    if (!errDisplay && raw) return;
    if (!shouldShowNewResponse({ topic, agent: resolvedAgent || agent, adhoc, flow_route: flowRoute })) return;
    placeResponseBubble();
    contentDiv.innerHTML = (authReq ? authLoginButtonHtml(authReq.harness) : '')
      + `<span class="msg-error">${escapeHtml(errDisplay) || 'Response interrupted.'}</span>`;
    if (authReq) wireAuthLoginButtons(contentDiv, () => sendMessage(text, opts));
    scrollToBottom();
  }

  function normalizedErrorDisplay(text) {
    return (text || 'Response interrupted.')
      .split('\n')[0]
      .replace(/^CLI exited \d+:\s*/, '')
      .trim();
  }

  function discardInterruptedStatusBubble(errText) {
    // showError always creates a response bubble (falls back to
    // 'Response interrupted.'), so the thinking bubble should always
    // be removed when an error arrives with no streamed content.
    thinkingFrozen = true;
    killBtn.style.display = 'none';
    thinkingBubble.remove();
    return true;
  }

  function showStoredResponse(content) {
    if (!shouldShowNewResponse({ topic, agent: resolvedAgent || agent, adhoc, flow_route: flowRoute })) return false;
    placeResponseBubble();
    raw = content || '';
    contentDiv.innerHTML = renderAssistantMarkdown(raw);
    scrollToBottom();
    return true;
  }

  function shouldShowLiveResponse() {
    return shouldShowNewResponse({ topic, agent: resolvedAgent || agent, adhoc, flow_route: flowRoute });
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

  function renderCancelledThinking(label) {
    thinkingFrozen = true;
    killBtn.style.display = 'none';
    if (thinkingLoader.parentNode) thinkingLoader.remove();
    thinkingContent.className = '';
    thinkingContent.innerHTML = `<span class="msg-error">${label}</span>`;
    thinkingBubble.style.display = '';
    thinkingBubble.classList.add('msg-thinking-done');
    updateThinkingHeightButton(thinkingBubble);
    scrollToBottom();
  }

  function renderCompletionTools(tools) {
    if (!shouldShowLiveResponse()) return;
    const diffTools = changeTools(tools || []);
    for (const tool of diffTools) {
      const block = makeToolBlock(tool, msgId, null, topic);
      block.classList.add('tool-block-history');
      messages.appendChild(block);
    }
    // force: this message's own GitDiff block is new (would get checked
    // regardless), but it can also retroactively flip earlier same-session
    // blocks from revertable to conflicting (see get_diff_revert_eligibility)
    // — those are already dataset.revertChecked='1' from their own render
    // and would otherwise sit stale until a full history reload re-creates
    // their DOM from scratch.
    refreshAllRevertButtons({ force: true });
  }

  function renderWorktreeBlockers(worktrees) {
    for (const wt of worktrees || []) {
      const ownerMsgId = Number.parseInt(wt.msg_id, 10);
      const conflicts = Array.isArray(wt.conflicts) ? wt.conflicts : [];
      const files = conflicts.length
        ? conflicts.map(path => ({ status: 'U', path }))
        : [{ status: 'M', path: 'worktree changes' }];
      const block = makeToolBlock({
        name: 'GitDiff',
        repo: wt.repo_root,
        source: wt.repo_root,
        worktree_repo: wt.worktree_path,
        worktree_status: wt.status || 'pending',
        worktree_conflicts: conflicts,
        integration_worktree_path: wt.integration_worktree_path || '',
        worktree_blocker: true,
        files,
        file_count: files.length,
        additions: 0,
        deletions: 0,
        diff: '',
      }, Number.isFinite(ownerMsgId) ? ownerMsgId : null, null, topic);
      block.classList.add('tool-block-history');
      messages.appendChild(block);
    }
    refreshAllRevertButtons();
    scrollToBottom();
  }

  function markSessionContextDelivered(sessionId) {
    const pendingAgentKeys = [
      `${topic}@${_effectiveAgent || '_'}`,
      `${topic}@${resolvedAgent || '_'}`,
    ];
    const memoryKeys = [
      _memoryInjectedKey(topic, _effectiveAgent),
      _memoryInjectedKey(topic, resolvedAgent),
    ];
    const changed = _contextIds.length || _includeTopicMemory || _attachedFiles.length;

    if (_contextIds.length) {
      if (!(adhoc && lookback === 0) && sessionId) {
        const injected = getInjectedInto();
        injected[sessionId] = [...new Set([...(injected[sessionId] || []), ..._contextIds])];
        setInjectedInto(injected);
      }
      if (!adhoc) pendingAgentKeys.forEach(key => { delete _pendingSessionInjectedIds[key]; });
    }
    if (_includeTopicMemory && !adhoc) {
      memoryKeys.forEach(key => {
        if (key) {
          _memoryInjectedInto[key] = _topicMemoryForSend.revision;
          delete _pendingSessionMemoryRevisions[key];
        }
      });
    }
    if (_attachedFiles.length && !adhoc) {
      if (sessionId) {
        const inSession = getAttachedFilesInSession();
        inSession[sessionId] = [...new Set([
          ...(inSession[sessionId] || []),
          ..._attachedFiles.map(f => f.path),
        ])];
        setAttachedFilesInSession(inSession);
      }
      pendingAgentKeys.forEach(key => { delete _pendingSessionAttachedFiles[key]; });
    }
    if (changed) {
      updatePinCount();
      if (pinPanel.classList.contains('open')) renderPinPanel();
    }
  }

  function startStatusFallback(id) {
    if (statusTimer || !id) return;
    const MAX_POLLS = 960;  // 32 min at 2s intervals — covers 30 min default timeout
    let count = 0;
    const timeoutFallback = () => {
      completedFromStatus = true;
      stopStatusFallback();
      if (raw || firstDataReceived) {
        parkInterruptedPartial(raw, 'Recovery timed out.');
      } else {
        freezeThinking();
        showError('Response timed out.');
      }
      finalizeQuotaTracking();
    };
    const doPoll = async () => {
      count++;
      try {
        const statusRes = await fetch(`/chat/${id}/status`);
        if (!statusRes.ok) {
          if (count >= MAX_POLLS) timeoutFallback();
          return;
        }
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
          if (!showStoredResponse(data.content || '')) {
            controller.abort();
            finalizeQuotaTracking();
            return;
          }
          bubble.classList.add('history-item');
          resolvedAgent = data.agent || resolvedAgent;
          addPinButton(bubble, msgId, topic, resolvedAgent, data.session_id || null);
          addBookmarkButton(bubble, msgId, topic, resolvedAgent);
          addReplyButton(bubble, topic, resolvedAgent, !!adhoc);
          addBadResponseButton(bubble, msgId, topic, resolvedAgent, !!data.marked_bad);
          const completedAt = data.completed_at || data.stats?.completed_at || doneTime;
          if (!statsEl && data.stats) statsEl = addStats(bubble, data.stats, completedAt);
          if (statsEl) {
            messages.appendChild(statsEl);
            addDeepDiveButton(bubble, topic, resolvedAgent, !!adhoc, statsEl, msgId, completedAt);
          }
          liveSessionTurnCount = parseInt(data.session_turn_count || '0', 10) || liveSessionTurnCount;
          _advisoryTurnCount = liveSessionTurnCount;
          const completedSessionId = data.session_id || data.stats?.session_id || lastSessionId;
          if (completedSessionId && !adhoc) _sessionIds[`${topic}@${resolvedAgent || '_'}`] = completedSessionId;
          liveCtxSpan.dataset.sessionTurnCount = String(liveSessionTurnCount);
          if (completedSessionId) liveCtxSpan.dataset.sessionId = completedSessionId;
          setCtxLabel(liveCtxSpan, !!data.adhoc, _contextIds.length, _includeTopicMemory, liveSessionTurnCount);
          if (!adhoc && !suppressChipTurnCount) _updateChipTurnCount(topic, resolvedAgent || null, completedSessionId || null, liveSessionTurnCount);
          markSessionContextDelivered(completedSessionId);
          evaluateAdvisory();
          let storedTools = [];
          if (data.context) {
            try {
              storedTools = typeof data.context === 'string' ? JSON.parse(data.context) : data.context;
              if (!Array.isArray(storedTools)) storedTools = [];
            } catch {}
          }
          liveCtxSpan.dataset.hasTrace = _hasTraceContent(data.status_raw, storedTools.length ? storedTools : liveToolEvents, data.content || '') ? 'true' : 'false';
          renderCompletionTools(storedTools.length ? storedTools : liveToolEvents);
          addCompletionTimestamp();
          scrollToBottom();
          if (flowRunId && msgId) watchFlowRun(flowRunId, msgId, flowRoute || route);
          if (flowRoute || route) refreshRouteTurnCounts(flowRoute || route, { force: true });
          controller.abort();
          finalizeQuotaTracking();
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
          finalizeQuotaTracking();
        } else if (data.status === 'cancelled') {
          completedFromStatus = true;
          stopStatusFallback();
          renderCancelledThinking(cancelledTurnLabel(data.content));
          controller.abort();
          finalizeQuotaTracking();
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
        if (count >= MAX_POLLS && !completedFromStatus && !completionRendered) timeoutFallback();
      } catch {
        if (count >= MAX_POLLS) timeoutFallback();
      }
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
  const _extraPinnedIds = Array.isArray(opts.extraPinnedIds) ? opts.extraPinnedIds : [];
  const _contextIds = [...new Set([..._lookbackIds, ..._pinnedIds, ..._extraPinnedIds])];
  await pruneMissingAttachedFiles();
  const _attachedFiles = _attachedFilesState({ topic, agent: _effectiveAgent, adhoc }).selected;
  const _messageForServer = _attachedFiles.length
    ? `${message}\n\nFiles:\n${_attachedFiles.map(f => `- ${f.path}`).join('\n')}`
    : message;

  try {
    startProcPoll({ hold: true });
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: _messageForServer, topic, agent, lookback, adhoc, source,
        ...(flowRoute ? { flow_route: flowRoute, ...(flowRunId ? { flow_run_id: flowRunId } : {}) } : {}),
        ...(adhoc && lookback > 0 ? { lookback_via_pins: true } : {}),
        ...(_includeTopicMemory ? { include_topic_memory: true } : {}),
        ...(_contextIds.length ? { pinned_ids: _contextIds } : {}),
      }),
      // For UI sends, !N is resolved into explicit pinned_ids from the current list.
      signal: controller.signal,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      if (res.status === 400 && err.error && err.error.includes('not found')) {
        freezeThinking();
        showAgentCreatePrompt(agent, () => sendMessage(text));
        return;
      }
      if (res.status === 409 && Array.isArray(err.worktrees) && err.worktrees.length) {
        freezeThinking();
        showError(err.error || 'Worktree sync requires attention before starting another turn.');
        renderWorktreeBlockers(err.worktrees);
        completionRendered = true;
        return;
      }
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    if (!res.body) throw new Error(`HTTP ${res.status}`);
    const responseFlowRunId = res.headers.get('X-Squid-Flow-Run-Id');
    if (flowRoute && responseFlowRunId) {
      flowRunId = responseFlowRunId;
      liveCtxSpan.dataset.flowRunId = flowRunId;
      if (chainMarker) chainMarker.dataset.flowRunId = flowRunId;
      thinkingBubble.dataset.flowRunId = flowRunId;
      bubble.dataset.flowRunId = flowRunId;
    }
    // Origin Broadcast: hand the flow_run_id back to the orchestrator as soon
    // as headers arrive — not after this whole turn finishes streaming — so
    // sibling targets can fire without waiting on this one to complete.
    if (opts.onFlowRunId) { opts.onFlowRunId(flowRunId); flowRunIdEmitted = true; }
    if (!msgId) attachMsgId(res.headers.get('X-Squid-Msg-Id'));
    _lookbackUnselected.clear();
    _lastLookbackSelectionKey = '';
    if (_contextIds.length && !adhoc) {
      const pendingKey = `${topic}@${_effectiveAgent || '_'}`;
      _pendingSessionInjectedIds[pendingKey] = [...new Set([
        ...(_pendingSessionInjectedIds[pendingKey] || []),
        ..._contextIds,
      ])];
      updatePinCount();
      if (pinPanel.classList.contains('open')) renderPinPanel();
    }
    if (_includeTopicMemory && !adhoc) {
      const memoryKey = _memoryInjectedKey(topic, _effectiveAgent);
      _pendingSessionMemoryRevisions[memoryKey] = _topicMemoryForSend.revision;
      delete _memorySelectionOverrides[_memoryOverrideKey(topic, _effectiveAgent, false)];
      updatePinCount();
      if (pinPanel.classList.contains('open')) renderPinPanel();
    }
    if (_attachedFiles.length && !adhoc) {
      const pendingKey = `${topic}@${_effectiveAgent || '_'}`;
      _pendingSessionAttachedFiles[pendingKey] = [...new Set([
        ...(_pendingSessionAttachedFiles[pendingKey] || []),
        ..._attachedFiles.map(f => f.path),
      ])];
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
              resolvedAgent = meta.agent || null;
              const metaRuntime = runtimeRef(meta.harness || '', meta.provider || null);
              if (meta.provider) {
                quotaBackend = meta.provider;
                if (quotaBeforeSnapshot?.backend && quotaBeforeSnapshot.backend !== quotaBackend) {
                  quotaBeforeSnapshot = null;
                }
              }
              const resolvedAdhoc = adhoc; // server echoes back what we sent; use closure as reliable source
              const newTag = makeTopicTag(topic, resolvedAgent, { adhoc: resolvedAdhoc, clickable: true, lookback, backend: metaRuntime || null });
              responseHeaderTag.replaceWith(newTag);
              responseHeaderTag = newTag;
              const newThinkingTag = makeTopicTag(topic, resolvedAgent, { adhoc: resolvedAdhoc, clickable: true, lookback, backend: metaRuntime || null });
              thinkingHeaderTag.replaceWith(newThinkingTag);
              thinkingHeaderTag = newThinkingTag;
              // Flow/broadcast user bubbles show the full live route, not
              // whichever single agent reported meta first.
              if (userBubble && !broadcastAgents && !displayFlowRoute) {
                const newUserTag = makeTopicTag(topic, resolvedAgent, { adhoc: resolvedAdhoc, clickable: true, lookback, backend: metaRuntime || null });
                if (userTopicTag) {
                  userTopicTag.replaceWith(newUserTag);
                } else if (resolvedAgent || topic !== 'default') {
                  const content = userBubble.firstElementChild;
                  if (content) {
                    content.insertBefore(document.createTextNode(' '), content.firstChild);
                    content.insertBefore(newUserTag, content.firstChild);
                  }
                }
              }
              if (updateComposerRoute) {
                setTopicChip(topic, resolvedAgent, resolvedAdhoc, lookback, {
                  route,
                  chainTarget,
                  chainTargetFresh,
                  chainOperator,
                  chainRounds,
                  chainTargetTopic,
                  suppressTurnCount: suppressChipTurnCount,
                });
              }
              if (meta.msg_id) {
                queuePosition = null;
                attachMsgId(meta.msg_id);
                setCtxLabel(liveCtxSpan, adhoc);
                bubble.dataset.topic = topic;
                if (resolvedAgent) {
                  bubble.dataset.agent = resolvedAgent;
                  thinkingBubble.dataset.agent = resolvedAgent;
                  // Agent wasn't known at send time (e.g. default agent) — now that it's
                  // resolved, re-check whether this live group matches the active filter.
                  if (!searchActive && (hasHistoryFilterScope() || hasResponseOnlyFilter())) setLiveGroupHidden(true);
                }
                addPinButton(bubble, msgId, topic, resolvedAgent);
                addBookmarkButton(bubble, msgId, topic, resolvedAgent);
                addReplyButton(bubble, topic, resolvedAgent, !!adhoc);
                addBadResponseButton(bubble, msgId, topic, resolvedAgent);
              }
            } catch {}
            eventName = null;

          } else if (eventName === 'queued') {
            try {
              const info = JSON.parse(data);
              queuePosition = info.position;
              killBtn.style.display = '';
              setThinkingText(`#${info.topic} · queued — position ${info.position}`);
            } catch {}
            pollProcs();
            eventName = null;

          } else if (eventName === 'loading') {
            // ADR-0037: local-model (e.g. Ollama) active load/unload visibility.
            try {
              const info = JSON.parse(data);
              setThinkingText(info.from
                ? `#${topic} · switching ${info.from} → ${info.to}…`
                : `#${topic} · loading ${info.to}…`);
            } catch {}
            eventName = null;

          } else if (eventName === 'stats') {
            try {
              const stats = JSON.parse(data);
              lastSessionId = stats.session_id ?? null;
              if (stats.session_id && !adhoc) {
                _sessionIds[`${topic}@${resolvedAgent || '_'}`] = stats.session_id;
                bubble.dataset.sessionId = stats.session_id;
              }
              const statsTimestamp = stats.completed_at || new Date().toISOString();
              statsEl = addStats(bubble, stats, statsTimestamp);
              addDeepDiveButton(bubble, topic, resolvedAgent, !!adhoc, statsEl, msgId, statsTimestamp);
              if (completionTimestampEl) {
                completionTimestampEl.remove();
                completionTimestampEl = null;
              }
              liveSessionTurnCount = parseInt(stats.session_turn_count || '0', 10) || 0;
              _advisoryTurnCount = liveSessionTurnCount;
              setCtxLabel(liveCtxSpan, !!stats.adhoc, _contextIds.length, _includeTopicMemory, liveSessionTurnCount);
              liveCtxSpan.dataset.sessionTurnCount = String(liveSessionTurnCount);
              if (stats.session_id) liveCtxSpan.dataset.sessionId = stats.session_id;
              if (!adhoc && !suppressChipTurnCount) _updateChipTurnCount(topic, resolvedAgent || null, stats.session_id || null, liveSessionTurnCount);
              if (stats.cwd) liveCtxSpan.dataset.cwd = stats.cwd;
              if (resolvedAgent) liveCtxSpan.dataset.agent = resolvedAgent;
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
            liveCtxSpan.dataset.hasTrace = (statusBuf.trim() || liveToolEvents.length) ? 'true' : 'false';
            removeThinking();
            invalidateTopicsCache();
            invalidateTopicsManageCache();
            doneTime = new Date().toISOString();
            if (firstDataReceived) {
              contentDiv.innerHTML = renderAssistantMarkdown(raw);
              bubble.classList.add('history-item');
              if (shouldShowNewResponse({ topic, agent: resolvedAgent || agent, adhoc, flow_route: flowRoute })) {
                placeResponseBubble();
                if (statsEl) messages.appendChild(statsEl); // stats goes between bubble and diffs, not after
                renderCompletionTools(liveToolEvents);
                scrollToBottom();
              }
            }
            // Later chain steps (target handoff, "<>" return) are dispatched
            // server-side (agent/flow.py) — not sent from here anymore, so a
            // refresh mid-chain can't strand it. Just watch for them to keep
            // rendering live while this tab stays open.
            if (flowRunId && msgId) watchFlowRun(flowRunId, msgId, flowRoute || route);
            if (flowRoute || route) refreshRouteTurnCounts(flowRoute || route, { force: true });
            // Update ctx label with pin count and store IDs for popup
            setCtxLabel(liveCtxSpan, adhoc, _contextIds.length, _includeTopicMemory, liveSessionTurnCount);
            liveCtxSpan.dataset.pinnedIds = JSON.stringify(_contextIds);
            liveCtxSpan.dataset.mem = _includeTopicMemory ? 'true' : 'false';
            markSessionContextDelivered(lastSessionId);
            updateInContextMarkers();
            eventName = null;

          } else if (eventName === 'error') {
            stopStatusFallback();
            const errLine = data.trim();
            if (raw || firstDataReceived) {
              parkInterruptedPartial(null, errLine || 'Connection interrupted.');
            } else {
              discardInterruptedStatusBubble(errLine) || freezeThinking();
              showError(errLine);
            }
            if (msgId && !userAborted) {
              detachedPolling = true;
              startStatusFallback(msgId);
            } else {
              completedFromStatus = true;
              completionRendered = true;
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
    // Safety net: if this target's request failed before headers ever
    // arrived, don't leave sibling broadcast targets awaiting an id forever.
    if (opts.onFlowRunId && !flowRunIdEmitted) { opts.onFlowRunId(null); flowRunIdEmitted = true; }
    if (!completedFromStatus && err.name !== 'AbortError') {
      if (msgId || await recoverMsgIdFromProcesses()) {
        detachedPolling = true;
        statusBuf += (statusBuf ? '\n' : '') + 'Connection interrupted — recovering…';
        updateThinkingPreview();
        startStatusFallback(msgId);
      } else {
        showError(err?.message || 'Unable to start response stream.');
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
    if (!adhoc && !completionRendered && !detachedPolling) {
      delete _pendingSessionInjectedIds[`${topic}@${_effectiveAgent || '_'}`];
      delete _pendingSessionInjectedIds[`${topic}@${resolvedAgent || '_'}`];
      delete _pendingSessionMemoryRevisions[_memoryInjectedKey(topic, _effectiveAgent)];
      delete _pendingSessionMemoryRevisions[_memoryInjectedKey(topic, resolvedAgent)];
      delete _pendingSessionAttachedFiles[`${topic}@${_effectiveAgent || '_'}`];
      delete _pendingSessionAttachedFiles[`${topic}@${resolvedAgent || '_'}`];
      updatePinCount();
      if (pinPanel.classList.contains('open')) renderPinPanel();
    }
    if (!detachedPolling && !userAborted && !firstDataReceived && !completedFromStatus) {
      if (shouldShowNewResponse({ topic, agent: resolvedAgent || agent, adhoc, flow_route: flowRoute })) {
        placeResponseBubble();
        contentDiv.innerHTML = '<span class="msg-error">No response — backend may be rate-limited or unavailable.</span>';
      }
    }
    addCompletionTimestamp();
  }

  if (!detachedPolling) await finalizeQuotaTracking();
  return { flowRunId, msgId };
}

// Origin Broadcast (ADR-0032): sends the same prompt to each listed agent as
// an independent origin turn — no chain envelope, no dispatch coupling
// between them. This is sugar over N normal sendMessage calls, fired in
// parallel: the first call's request goes out immediately and, as soon as
// its response headers arrive (not once its whole turn finishes), hands its
// server-allocated flow_run_id to the rest via onFlowRunId — so siblings
// start within one header round-trip of each other instead of waiting for
// each prior turn to fully complete. agent/flow.py never sees or dispatches
// any of this, since a bare comma-separated route has no operator to
// recognize. Only the first target renders the (shared) user prompt bubble —
// see suppressUserBubble in sendMessage — since every target got the same
// literal prompt and repeating it per agent would just be visual noise.
async function sendOriginBroadcast(text, opts = {}) {
  const parsed = parseInput(text);
  const originTargets = parsed.broadcastAgents || parsed.flowOrigins || null;
  if (!originTargets) return;
  const displayFlowRoute = parsed.route || (topicChipEl?.classList.contains('route-chain') ? topicChipEl.textContent.trim() : null);
  if ((opts.source || 'human') === 'human') {
    if (parsed.broadcastAgents) {
      setTopicChip(parsed.topic, null, false, 0, { route: parsed.route, broadcastAgents: parsed.broadcastAgents });
    } else {
      setTopicChip(parsed.topic, parsed.agent, parsed.adhoc, 0, {
        route: parsed.route,
        chainTarget: parsed.chainTarget,
        chainTargetFresh: parsed.chainTargetFresh,
        chainOperator: parsed.chainOperator,
        chainRounds: parsed.chainRounds,
        chainTargetTopic: parsed.chainTargetTopic,
        flowOrigins: parsed.flowOrigins,
      });
    }
  }
  let resolveSharedFlowRunId;
  const sharedFlowRunId = new Promise(resolve => { resolveSharedFlowRunId = resolve; });
  const sends = originTargets.map((target, i) => {
    if (i === 0) {
      return sendMessage(text, {
        ...opts,
        broadcastTarget: target,
        flowRoute: parsed.route,
        displayFlowRoute,
        updateComposerRoute: false,
        onFlowRunId: resolveSharedFlowRunId,
      });
    }
    return sharedFlowRunId.then(flowRunId => sendMessage(text, {
      ...opts,
      broadcastTarget: target,
      flowRoute: parsed.route,
      displayFlowRoute,
      flowRunId,
      updateComposerRoute: false,
      suppressUserBubble: true,
    }));
  });
  await Promise.all(sends);
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
  if (name === 'WorktreeSync') return `Worktree sync: ${tool.status || 'unknown'}`;
  if (name === 'Read' || name === 'Edit' || name === 'Write' || name === 'MultiEdit' || name === 'Diff')
    return `${name}: ${tool.file || ''}`;
  if (name === 'Bash') return `Bash: ${truncate(tool.command || '', 70)}`;
  if (name === 'Agent') return `Agent: ${truncate(tool.description || '', 70)}`;
  if (name === 'WebFetch' || name === 'WebSearch') return `${name}: ${truncate(tool.query || '', 70)}`;
  if (name === 'TodoWrite') { const n = (tool.todos || []).length; return `TodoWrite: ${n} item${n !== 1 ? 's' : ''}`; }
  return name + (tool.key ? ': ' + truncate(tool.value || '', 50) : '');
}

function changeTools(tools) {
  tools = dedupeToolRecords(tools);
  const syncByWorktree = new Map();
  for (const tool of tools || []) {
    if (tool.name === 'WorktreeSync' && tool.worktree_repo && tool.status) {
      syncByWorktree.set(tool.worktree_repo, tool);
    }
  }
  const gitTools = tools.filter(t => t.name === 'GitDiff');
  if (gitTools.length) {
    return gitTools
      .filter(t => (t.file_count ?? (t.files || []).length) > 0)
      .map(t => {
        const syncTool = t.worktree_repo ? syncByWorktree.get(t.worktree_repo) : null;
        return syncTool
          ? {
              ...t,
              worktree_status: syncTool.status,
              worktree_conflicts: syncTool.conflicts || t.worktree_conflicts || [],
              integration_worktree_path: syncTool.integration_worktree_path || t.integration_worktree_path || '',
            }
          : t;
      });
  }
  return tools.filter(t => t.name === 'Edit' || t.name === 'Write' || t.name === 'MultiEdit' || t.name === 'Diff');
}

// Whether a message has anything worth showing in the "thought trace" popup —
// the free-text status/reasoning stream and/or any recorded tool calls.
function _traceStatusText(statusRaw, finalContent) {
  const trace = String(statusRaw || '').trim();
  const finalText = String(finalContent || '').trim();
  if (!trace || !finalText) return trace;
  if (trace === finalText) return '';
  let stripped = trace;
  while (stripped.endsWith(finalText)) {
    stripped = stripped.slice(0, -finalText.length).trimEnd();
    if (!stripped) return '';
  }
  if (stripped !== trace) return stripped;
  return trace;
}

function _hasTraceContent(statusRaw, contextVal, finalContent = '') {
  if (_traceStatusText(statusRaw, finalContent)) return true;
  try {
    const arr = typeof contextVal === 'string' ? JSON.parse(contextVal) : contextVal;
    return Array.isArray(arr) && arr.length > 0;
  } catch { return false; }
}

function dedupeToolRecords(tools) {
  const seen = new Set();
  const deduped = [];
  for (const tool of tools || []) {
    if (!tool || typeof tool !== 'object') continue;
    const id = tool.tool_use_id;
    if (id) {
      const key = `${tool.name || ''}\0${id}`;
      if (seen.has(key)) continue;
      seen.add(key);
    }
    deduped.push(tool);
  }
  return deduped;
}


const DIFF_LCS_MAX_CELLS = 4_000_000;

function diffLineOps(oldLines, newLines) {
  const n = oldLines.length, m = newLines.length;
  if (n * m > DIFF_LCS_MAX_CELLS) return null;
  const dp = new Array(n + 1);
  for (let i = 0; i <= n; i++) dp[i] = new Uint32Array(m + 1);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = oldLines[i] === newLines[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (oldLines[i] === newLines[j]) {
      ops.push({ type: 'same', line: oldLines[i] });
      i++; j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      ops.push({ type: 'remove', line: oldLines[i] });
      i++;
    } else {
      ops.push({ type: 'add', line: newLines[j] });
      j++;
    }
  }
  for (; i < n; i++) ops.push({ type: 'remove', line: oldLines[i] });
  for (; j < m; j++) ops.push({ type: 'add', line: newLines[j] });
  return ops;
}

function renderDiffLines(container, oldStr, newStr) {
  const oldLines = (oldStr || '').split('\n');
  const newLines = (newStr || '').split('\n');
  const ops = diffLineOps(oldLines, newLines) || [
    ...oldLines.map(line => ({ type: 'remove', line })),
    ...newLines.map(line => ({ type: 'add', line })),
  ];
  const prefix = { same: '  ', remove: '- ', add: '+ ' };
  const cls = { same: 'diff-line diff-same', remove: 'diff-line diff-remove', add: 'diff-line diff-add' };
  for (const op of ops) {
    const el = document.createElement('span');
    el.className = cls[op.type];
    el.textContent = prefix[op.type] + op.line;
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

function markVisibleWorktreeResolved(msgId, sourceRepo, message) {
  if (!msgId) return;
  const terminalLabel = /discarded/i.test(message)
    ? 'Discarded'
    : /synced/i.test(message) && !/resolved/i.test(message)
      ? 'Synced'
      : 'Resolved';
  const toggleLabel = terminalLabel === 'Resolved' ? 'Conflict Resolved' : terminalLabel;
  const selector = `.tool-block[data-worktree-msg-id="${CSS.escape(String(msgId))}"]`;
  document.querySelectorAll(selector).forEach(block => {
    if (sourceRepo && block.dataset.worktreeRepo !== sourceRepo) return;
    const notice = block.querySelector('.gitdiff-sync-notice');
    if (notice) notice.textContent = message;
    const actions = block.querySelector('.gitdiff-sync-actions');
    if (actions) {
      const label = document.createElement('span');
      label.className = 'gitdiff-resolved-label';
      label.textContent = terminalLabel;
      actions.replaceWith(label);
    }
    const toggle = block.querySelector('.tool-toggle');
    if (toggle) {
      toggle.textContent = toggle.textContent.replace(/ · (conflict|promotion_failed|pending|active)\b/, ` · ${toggleLabel}`);
    }
  });
}

// Revert is "undo what I just saw," not time travel — past this window other
// work has almost certainly built on top of the file, so the eligibility
// check (which scans every later message in the topic) is skipped entirely.
const REVERT_WINDOW_MS = 24 * 60 * 60 * 1000;

function _withinRevertWindow(timestamp) {
  if (!timestamp) return true;  // no timestamp (e.g. a just-finished live response) is always "now"
  const t = new Date(timestamp).getTime();
  return Number.isFinite(t) && Date.now() - t < REVERT_WINDOW_MS;
}

function makeToolBlock(tool, msgId, timestamp, messageTopic = null) {
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
    const worktreeStatus = tool.worktree_repo ? tool.worktree_status : null;
    const worktreeBlocked = !!worktreeStatus && worktreeStatus !== 'synced';
    const statusLabel = worktreeStatus === 'resolved' ? 'Conflict Resolved' : worktreeStatus;
    const statusSuffix = worktreeBlocked ? ` · ${statusLabel}` : '';
    toggle.textContent = `Changed files: ${count} file${count !== 1 ? 's' : ''}, +${additions} -${deletions}${statusSuffix}`;

    const sourceRepo = _gitDiffSourceRepo(tool);
    // Only conflict/promotion_failed/discarded turns have nothing (or an
    // ambiguous result) to revert — a resolved turn's changes did land, same
    // as a plain synced one, so revert stays available for it.
    const revertBlocked = !!worktreeStatus && worktreeStatus !== 'synced' && worktreeStatus !== 'resolved';
    const revertEligible = msgId && sourceRepo && _withinRevertWindow(timestamp) && !revertBlocked;
    if (revertEligible) {
      block.dataset.msgId = String(msgId);
      block.dataset.repo = sourceRepo;
      const revertBar = document.createElement('div');
      revertBar.className = 'gitdiff-revert-bar';
      body.appendChild(revertBar);
    }

    if (worktreeBlocked) {
      if (msgId) block.dataset.worktreeMsgId = String(msgId);
      if (sourceRepo) block.dataset.worktreeRepo = sourceRepo;
      const notice = document.createElement('div');
      notice.className = 'gitdiff-sync-notice';
      const conflicts = Array.isArray(tool.worktree_conflicts) ? tool.worktree_conflicts : [];
      const conflictText = conflicts.length ? conflicts.join(', ') : 'changed files';
      notice.textContent = worktreeStatus === 'conflict'
        ? `Worktree sync conflict: ${conflictText}. New turns for this topic are blocked until the integration worktree is resolved or discarded.`
        : worktreeStatus === 'promotion_failed'
          ? `Worktree sync failed. New turns for this topic are blocked until this worktree is resolved or discarded.`
          : worktreeStatus === 'discarded'
            ? `Discarded — this turn's changes were never applied to the main checkout.`
            : worktreeStatus === 'resolved'
              ? `Conflict resolved — final state applied to the main checkout.`
              : `Worktree sync ${worktreeStatus}. Squid is still promoting this turn; retry or refresh if it does not clear.`;
      if (worktreeStatus === 'discarded') notice.classList.add('gitdiff-sync-notice-discarded');
      if (worktreeStatus === 'resolved') notice.classList.add('gitdiff-sync-notice-resolved');
      body.appendChild(notice);

      const worktreeActionable = worktreeStatus !== 'discarded' && worktreeStatus !== 'resolved';
      if (msgId && sourceRepo && messageTopic && worktreeActionable) {
        const actions = document.createElement('div');
        actions.className = 'gitdiff-sync-actions';
        const firstConflictPath = conflicts[0] || (tool.files || [])[0]?.path || '';
        const targetTurn = msgId ? `turn #${msgId}` : 'this turn';
        const blockerPrefix = tool.worktree_blocker
          ? `This later blocked message points at ${targetTurn}. `
          : `Targets ${targetTurn}. `;
        if (worktreeStatus === 'conflict' && tool.integration_worktree_path && firstConflictPath) {
          const openConflictBtn = document.createElement('button');
          openConflictBtn.type = 'button';
          openConflictBtn.className = 'gitdiff-resolve-worktree-btn';
          openConflictBtn.textContent = 'Conflicts';
          openConflictBtn.title = `${blockerPrefix}Open the conflicted file in the integration worktree.`;
          openConflictBtn.addEventListener('click', async e => {
            e.stopPropagation();
            const conflictFile = `${tool.integration_worktree_path}/${firstConflictPath}`;
            let markerLine = null;
            try {
              const res = await fetch('/localfile?' + new URLSearchParams({ path: conflictFile, _t: Date.now() }));
              if (res.ok) {
                const text = await res.text();
                const idx = text.split('\n').findIndex(line => line.startsWith('<<<<<<<'));
                if (idx >= 0) markerLine = idx + 1;
              }
            } catch {}
            openFileViewer(conflictFile, markerLine, null, null, null, null, { search: '<<<<<<<' });
          });
          actions.appendChild(openConflictBtn);
        }

        const retryBtn = document.createElement('button');
        retryBtn.type = 'button';
        retryBtn.className = 'gitdiff-resolve-worktree-btn';
        const retryLabel = worktreeStatus === 'conflict' || worktreeStatus === 'promotion_failed' ? 'Resolve' : 'Retry Sync';
        retryBtn.textContent = retryLabel;
        retryBtn.title = worktreeStatus === 'conflict' || worktreeStatus === 'promotion_failed'
          ? `${blockerPrefix}Apply the saved integration worktree resolution back to the main checkout.`
          : `${blockerPrefix}Retry promoting this isolated turn's changes to the main checkout.`;
        retryBtn.addEventListener('click', async e => {
          e.stopPropagation();
          if (retryBtn.disabled) return;
          retryBtn.disabled = true;
          retryBtn.textContent = 'Resolving...';
          const postRetry = force => fetch(`/chat/${msgId}/worktree/retry`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ topic: messageTopic, repo: sourceRepo, force }),
          }).then(async res => ({ res, data: await res.json().catch(() => ({})) }));
          try {
            let { res, data } = await postRetry(false);
            if (!res.ok && data.conflict_markers_remain) {
              const files = (data.files || []).join(', ') || 'the conflicted file(s)';
              const proceed = await confirmRestartWithRunningPrompts([], {
                header: 'Resolve Conflict',
                title: 'Conflict markers still present',
                copy: `${files} still look${data.files?.length === 1 ? 's' : ''} like they contain conflict-marker ` +
                  `text (<<<<<<< / ======= / >>>>>>>). Promote anyway?`,
                confirmLabel: 'Promote Anyway',
              });
              if (!proceed) {
                retryBtn.disabled = false;
                retryBtn.textContent = retryLabel;
                return;
              }
              ({ res, data } = await postRetry(true));
            }
            if (!res.ok || !data.ok) throw new Error(data.error || 'Resolve failed');
            const resolvedMessage = data.already_resolved
              ? 'Worktree was already resolved. Future turns for this topic can start normally.'
              : worktreeStatus === 'conflict' || worktreeStatus === 'promotion_failed'
                ? 'Worktree resolved and synced. Future turns for this topic can start normally.'
                : 'Worktree synced. Future turns for this topic can start normally.';
            markVisibleWorktreeResolved(msgId, sourceRepo, resolvedMessage);
          } catch (err) {
            const msg = err?.message || 'Resolve failed';
            retryBtn.disabled = false;
            retryBtn.textContent = retryLabel;
            retryBtn.title = msg;
            notice.textContent = `${retryLabel} failed: ${msg}`;
          }
        });
        actions.appendChild(retryBtn);

        if (worktreeStatus === 'conflict') {
          const sep = document.createElement('span');
          sep.className = 'gitdiff-sync-actions-sep';
          actions.appendChild(sep);

          const autoResolveBtn = document.createElement('button');
          autoResolveBtn.type = 'button';
          autoResolveBtn.className = 'gitdiff-resolve-worktree-btn';
          autoResolveBtn.textContent = 'Auto-Resolve';
          autoResolveBtn.title = `${blockerPrefix}Ask the model to merge both sides directly in the integration worktree, using the original turn as context.`;
          autoResolveBtn.addEventListener('click', async e => {
            e.stopPropagation();
            if (autoResolveBtn.disabled) return;
            autoResolveBtn.disabled = true;
            autoResolveBtn.textContent = 'Resolving...';
            let thinkingBubble = null, thinkingContent = null, loader = null;
            const fail = msg => {
              autoResolveBtn.disabled = false;
              autoResolveBtn.textContent = 'Auto-Resolve';
              autoResolveBtn.title = msg;
              notice.textContent = `Auto-resolve failed: ${msg}`;
              if (thinkingBubble) {
                thinkingBubble.classList.remove('msg-thinking');
                if (loader?.parentNode) loader.remove();
                const errEl = document.createElement('div');
                errEl.className = 'msg-error';
                errEl.textContent = msg;
                thinkingBubble.appendChild(errEl);
              }
            };
            try {
              const res = await fetch(`/chat/${msgId}/worktree/auto-resolve`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ topic: messageTopic, repo: sourceRepo }),
              });
              const isStream = res.ok && (res.headers.get('content-type') || '').includes('text/event-stream');
              if (!isStream) {
                const data = await res.json().catch(() => ({}));
                if (!res.ok || !data.ok) throw new Error(data.error || 'Auto-resolve failed');
                const resolvedMessage = data.already_resolved
                  ? 'Worktree was already resolved. Future turns for this topic can start normally.'
                  : 'Auto-resolved by the model. Future turns for this topic can start normally.';
                markVisibleWorktreeResolved(msgId, sourceRepo, resolvedMessage);
                return;
              }

              // Real model work is happening now — render it as a normal chat
              // turn (prompt + live thinking + streamed response) instead of
              // hiding it behind a bare button spinner.
              const userBubble = makeUserBubble('Auto-resolve merge conflict', messageTopic, null, null, true, 0, 'diff_viewer');
              messages.appendChild(userBubble);
              addTimestamp(userBubble, new Date().toISOString(), true);

              thinkingBubble = document.createElement('div');
              thinkingBubble.className = 'msg assistant msg-thinking';
              thinkingBubble.dataset.topic = messageTopic;
              const thinkingHeader = document.createElement('div');
              thinkingHeader.className = 'response-header';
              const thinkingHeaderText = document.createElement('span');
              thinkingHeaderText.className = 'response-header-text';
              thinkingHeaderText.appendChild(makeTopicTag(messageTopic, null, { adhoc: true }));
              thinkingHeader.appendChild(thinkingHeaderText);
              thinkingBubble.appendChild(thinkingHeader);
              thinkingContent = document.createElement('div');
              thinkingContent.className = 'thinking-live';
              thinkingBubble.appendChild(thinkingContent);
              loader = addLoader(thinkingContent);
              messages.appendChild(thinkingBubble);
              scrollToBottom();

              let raw = '';
              let statusBuf = '';
              const updatePreview = () => {
                if (loader?.parentNode) loader.remove();
                thinkingContent.textContent = (statusBuf ? statusBuf.trim() + (raw ? '\n\n' : '') : '') + raw;
                scrollToBottom();
              };

              const reader = res.body.getReader();
              const decoder = new TextDecoder();
              let buf = '';
              let eventName = null;
              let resolveResult = null;

              while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buf += decoder.decode(value, { stream: true });
                const lines = buf.split('\n');
                buf = lines.pop();
                for (const line of lines) {
                  if (line.startsWith('event:')) {
                    eventName = line.slice(6).trim();
                  } else if (line.startsWith('data:')) {
                    const field = line.slice(5);
                    const data = field.startsWith(' ') ? field.slice(1) : field;
                    if (eventName === 'resolve_result') {
                      try { resolveResult = JSON.parse(data); } catch {}
                    } else if (eventName === 'status' || eventName === 'tool') {
                      statusBuf += (statusBuf ? '\n' : '') + data;
                      updatePreview();
                    } else if (eventName === 'error') {
                      throw new Error(data.trim() || 'Auto-resolve failed');
                    } else if (eventName === 'done' || eventName === 'meta' || eventName === 'stats' || eventName === 'loading') {
                      // internal turn — no per-event UI beyond the live text below
                    } else {
                      raw += data;
                      updatePreview();
                    }
                  } else if (line === '') {
                    eventName = null;
                  }
                }
              }

              if (!resolveResult || !resolveResult.ok) {
                // The stream closed without a final resolve_result event
                // (dropped connection, backgrounded tab, proxy buffering) —
                // the server may have already finished the resolve before
                // that happened, so check real state before declaring failure.
                let liveStatus = null;
                try {
                  const statusRes = await fetch(`/chat/${msgId}/worktree/status?` +
                    new URLSearchParams({ topic: messageTopic, repo: sourceRepo }));
                  if (statusRes.ok) liveStatus = (await statusRes.json())?.status;
                } catch {}
                if (liveStatus === 'resolved' || liveStatus === 'synced') {
                  thinkingBubble.classList.remove('msg-thinking');
                  if (loader?.parentNode) loader.remove();
                  thinkingContent.innerHTML = raw ? renderAssistantMarkdown(raw) : '';
                  thinkingBubble.classList.add('history-item');
                  markVisibleWorktreeResolved(msgId, sourceRepo,
                    'Auto-resolved by the model. Future turns for this topic can start normally.');
                  return;
                }
                throw new Error(resolveResult?.error || 'Auto-resolve failed');
              }

              thinkingBubble.classList.remove('msg-thinking');
              if (loader?.parentNode) loader.remove();
              thinkingContent.innerHTML = raw ? renderAssistantMarkdown(raw) : '';
              thinkingBubble.classList.add('history-item');
              markVisibleWorktreeResolved(msgId, sourceRepo, 'Auto-resolved by the model. Future turns for this topic can start normally.');
            } catch (err) {
              fail(err?.message || 'Auto-resolve failed');
            }
          });
          actions.appendChild(autoResolveBtn);
        }

        const discardBtn = document.createElement('button');
        discardBtn.type = 'button';
        discardBtn.className = 'gitdiff-discard-worktree-btn';
        discardBtn.textContent = 'Discard Turn';
        discardBtn.title = `${blockerPrefix}Discard only this isolated turn's pending worktree changes; already-applied main checkout changes are not reverted.`;
        discardBtn.addEventListener('click', async e => {
          e.stopPropagation();
          if (discardBtn.disabled) return;
          discardBtn.disabled = true;
          discardBtn.textContent = 'Discarding...';
          try {
            const res = await fetch(`/chat/${msgId}/worktree/discard`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ topic: messageTopic, repo: sourceRepo }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data.ok) throw new Error(data.error || 'Discard failed');
            const resolvedMessage = data.already_resolved
              ? 'Worktree was already resolved. Future turns for this topic can start normally.'
              : 'Worktree discarded. Future turns for this topic can start normally.';
            markVisibleWorktreeResolved(msgId, sourceRepo, resolvedMessage);
          } catch (err) {
            const msg = err?.message || 'Discard failed';
            discardBtn.disabled = false;
            discardBtn.textContent = 'Discard Turn';
            discardBtn.title = msg;
            notice.textContent = `Discard failed: ${msg}`;
          }
        });
        actions.appendChild(discardBtn);
        body.appendChild(actions);
      }

      if (tool.integration_worktree_path) {
        const pathLine = document.createElement('div');
        pathLine.className = 'gitdiff-sync-path';
        pathLine.textContent = tool.integration_worktree_path;
        body.appendChild(pathLine);
      }
    }

    const fileDiffs = splitUnifiedDiff(tool.diff || '');
    const omittedPaths = new Set(tool.omitted_paths || []);
    const files = tool.files || [];
    const displayPaths = _gitDiffDisplayPaths(files);
    const fullDisplayPaths = files.map(_gitDiffFullDisplayPath);
    for (const [i, file] of files.entries()) {
      const status = file.status || '?';
      const displayPath = displayPaths[i];
      const fullDisplayPath = fullDisplayPaths[i];
      const chunk = fileDiffs.get(file.path) || fileDiffs.get(file.old_path) || '';
      const isOmitted = omittedPaths.has(file.path) || (file.old_path && omittedPaths.has(file.old_path));
      const firstChangedRange = _firstChangedNewRange(chunk);
      const allChangedLines = _allChangedNewLines(chunk);

      const row = document.createElement('div');
      row.className = 'gitdiff-file-row';
      if (revertEligible) row.dataset.file = file.path;

      const fileToggle = document.createElement('button');
      fileToggle.className = 'gitdiff-file-toggle';
      fileToggle.title = fullDisplayPath;

      // git's own content-based binary detection wins when we have diff text to
      // check; only fall back to the extension guess when there's no chunk to look at
      const isBinary = chunk ? chunk.includes('Binary files') : !_isTextPath(file.path || '');
      let fileBody = null;
      if (isOmitted) {
        fileToggle.textContent = `${status} ${displayPath}`;
        fileToggle.classList.add('gitdiff-file-toggle--no-diff');
        const badge = document.createElement('span');
        badge.className = 'gitdiff-binary-badge';
        badge.title = 'This turn’s diff was too large — earlier files were shown in full and this one was left out entirely, not cut mid-file.';
        badge.textContent = 'too large to show';
        fileToggle.appendChild(badge);
        row.appendChild(fileToggle);

        if (msgId && sourceRepo) {
          const loadBtn = document.createElement('button');
          loadBtn.type = 'button';
          loadBtn.className = 'gitdiff-file-open';
          loadBtn.textContent = 'load diff';
          loadBtn.addEventListener('click', async e => {
            e.stopPropagation();
            loadBtn.disabled = true; loadBtn.textContent = '…';
            try {
              const res = await fetch(`/chat/${msgId}/diff-file?repo=${encodeURIComponent(sourceRepo)}&path=${encodeURIComponent(file.path)}`);
              const data = await res.json();
              if (!res.ok || !data.diff) {
                loadBtn.textContent = 'load diff';
                loadBtn.disabled = false;
                loadBtn.title = data.error || 'failed to load';
                return;
              }
              const loadedBody = document.createElement('div');
              loadedBody.className = 'gitdiff-file-body';
              const loadedScroll = document.createElement('div');
              loadedScroll.className = 'diff-scroll';
              renderUnifiedDiffLines(loadedScroll, data.diff);
              loadedBody.appendChild(loadedScroll);
              row.appendChild(loadedBody);
              loadBtn.remove();
              fileToggle.classList.remove('gitdiff-file-toggle--no-diff');
              fileToggle.addEventListener('click', () => row.classList.toggle('gitdiff-file-expanded'));
              row.classList.add('gitdiff-file-expanded');
            } catch {
              loadBtn.textContent = 'load diff';
              loadBtn.disabled = false;
              loadBtn.title = 'failed to load';
            }
          });
          row.appendChild(loadBtn);
        }
      } else if (isBinary) {
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
      if (status !== 'D' && _absPath && !isBinary && !worktreeBlocked) {
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

function _revertFailureText(data, fallback = 'failed') {
  return data?.failed?.[0]?.error || data?.error || fallback;
}

function confirmDiffRevert({ filePath = null, count = 1 } = {}) {
  const title = filePath ? `Revert ${filePath}?` : `Revert ${count} files?`;
  const copy = filePath
    ? 'This applies a reverse patch directly to the main checkout.'
    : `This applies reverse patches for ${count} files directly to the main checkout.`;
  return confirmRestartWithRunningPrompts([], {
    header: 'Revert Changes',
    title,
    copy,
    confirmLabel: 'Revert',
  });
}

async function fetchRevertEligibility(block) {
  const msgId = block.dataset.msgId;
  const repo = block.dataset.repo;
  if (!msgId || !repo) return;
  // Marked synchronously (before the fetch even starts) so an overlapping
  // refreshAllRevertButtons() call in the same tick sees it as claimed and
  // skips it, instead of firing a duplicate request for the same block.
  block.dataset.revertChecked = '1';

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
        const confirmed = await confirmDiffRevert({ filePath: fpath });
        if (!confirmed) return;
        btn.disabled = true; btn.textContent = '…';
        try {
          const data = await _doRevert(msgId, repo, fpath);
          if (data.ok && data.reverted?.length) {
            refreshAllRevertButtons({ force: true });
          } else {
            btn.disabled = false; btn.textContent = 'revert';
            btn.title = _revertFailureText(data);
          }
        } catch { btn.disabled = false; btn.textContent = 'revert'; }
      });
      const openBtn = row.querySelector('.gitdiff-file-open');
      if (openBtn) row.insertBefore(btn, openBtn);
      else {
        const _fb = row.querySelector('.gitdiff-file-body');
        if (_fb) row.insertBefore(btn, _fb); else row.appendChild(btn);
      }
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
      const confirmed = await confirmDiffRevert({ count: revertableFiles.length });
      if (!confirmed) return;
      btn.disabled = true; btn.textContent = '…';
      try {
        const data = await _doRevert(msgId, repo, null);
        if (data.ok && data.reverted?.length) { refreshAllRevertButtons({ force: true }); }
        else {
          btn.disabled = false;
          btn.textContent = `Revert all ${revertableFiles.length} files`;
          btn.title = _revertFailureText(data);
        }
      } catch { btn.disabled = false; btn.textContent = `Revert all ${revertableFiles.length} files`; }
    });
    bar.appendChild(btn);
  }
}

// Revert-eligibility is per-block static info (does a git diff exist / is it
// still revertable) except right after an actual revert, where the working
// tree changed and every block's eligibility may have shifted - hence `force`.
function refreshAllRevertButtons({ force = false } = {}) {
  for (const block of document.querySelectorAll('.tool-block[data-msg-id][data-repo]')) {
    if (!force && block.dataset.revertChecked === '1') continue;
    fetchRevertEligibility(block);
  }
}

function historyPromptTruncateLimit() {
  return window.matchMedia?.('(max-width: 600px)').matches ? 24 : 55;
}

function makeHistoryPromptToggle(prompt) {
  const promptToggle = document.createElement('span');
  promptToggle.className = 'history-prompt';
  const promptToggleText = document.createElement('span');
  promptToggleText.className = 'history-prompt-truncated';
  promptToggleText.textContent = truncate(prompt || '', historyPromptTruncateLimit());
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

function historyRouteChainFromPrompt(prompt) {
  const match = String(prompt || '').match(new RegExp(`(?:^|\\n)Route:\\s*(#${TOPIC_SLUG_SRC}@${AGENT_SLUG_SRC}!?(?:(?:<>)|=>|>)(?:#${TOPIC_SLUG_SRC})?@${AGENT_SLUG_SRC}!?)(?:\\s|$)`));
  return match ? normalizePromptHistoryRoute(match[1]) : '';
}

function routeChainParts(route) {
  return parseRouteChain(route);
}

function historyItemMatchesRouteOrigin(item, parts) {
  return !!parts
    && (item?.topic || 'default') === parts.topic
    && (item?.agent || null) === parts.origin
    && !!item?.adhoc === parts.originFresh;
}

function historyRouteChainMarkerForItem(item, nextItem, prevItem) {
  const ownRoute = normalizePromptHistoryRoute(item?.flow_route || item?.flowRoute || '');
  if (historyItemMatchesRouteOrigin(item, routeChainParts(ownRoute))) {
    return ownRoute;
  }
  const nextRoute = nextItem?.prompt_source === 'workflow'
    ? historyRouteChainFromPrompt(nextItem.prompt)
    : '';
  const nextParts = routeChainParts(nextRoute);
  if (historyItemMatchesRouteOrigin(item, nextParts)) {
    return nextRoute;
  }
  return '';
}

function appendHistoryRouteChainMarker(route, item, container) {
  if (!route || !container) return null;
  const marker = makeRouteChainMarker(route);
  marker.classList.add('history-item');
  marker.dataset.topic = item.topic || 'default';
  if (item.agent) marker.dataset.agent = item.agent;
  if (item.adhoc) marker.dataset.adhoc = '1';
  container.appendChild(marker);
  return marker;
}

function addThinkingHeightButton(bubble) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'thinking-height-btn hidden';
  btn.title = 'Double thinking height';
  btn.setAttribute('aria-label', 'Double thinking height');
  btn.textContent = '▼';
  btn.addEventListener('click', e => {
    e.stopPropagation();
    const expanded = bubble.classList.toggle('thinking-tall');
    btn.title = expanded ? 'Normal thinking height' : 'Double thinking height';
    btn.setAttribute('aria-label', btn.title);
    btn.textContent = expanded ? '▲' : '▼';
    updateThinkingHeightButton(bubble);
  });
  bubble.appendChild(btn);
  requestAnimationFrame(() => updateThinkingHeightButton(bubble));
  return btn;
}

function updateThinkingHeightButton(bubble) {
  const btn = bubble.querySelector('.thinking-height-btn');
  if (!btn) return;
  const target = bubble.querySelector('.thinking-live, .thinking-expanded .thinking-body');
  const canGrow = !!target && (bubble.classList.contains('thinking-tall') || target.scrollHeight > target.clientHeight + 1);
  btn.classList.toggle('hidden', !canGrow);
}

function cancelledTurnLabel(content) {
  return /before start/i.test(String(content || '')) ? 'Dequeued.' : 'Cancelled.';
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
  if (item.session_turn_count != null && item.agent && !item.adhoc) {
    _setKnownSessionTurnCount(item.topic || 'default', item.agent, sessionTurnCount, item.session_id || null);
  }
  if (sessionTurnCount > 0 && item.session_id && !item.adhoc) {
    const prev = _sessionTurnCounts[item.session_id] || 0;
    if (sessionTurnCount > prev) {
      _sessionTurnCounts[item.session_id] = sessionTurnCount;
      const taKey = `${item.topic || 'default'}@${item.agent || '_'}`;
      if (stickyChip && !stickyChip.adhoc && stickyChip.topic === (item.topic || 'default') &&
          !stickyChip.route &&
          (stickyChip.agent || null) === (item.agent || null) &&
          _sessionIds[taKey] === item.session_id) {
        _renderChipTurnCount(sessionTurnCount);
      }
    }
  }
  setCtxLabel(ctxSpan, !!item.adhoc, _pc.pins.length, _pc.mem, sessionTurnCount);
  ctxSpan.dataset.sessionId = item.session_id || '';
  ctxSpan.dataset.flowRunId = item.flow_run_id || '';
  ctxSpan.dataset.cwd = item.stats?.cwd || '';
  ctxSpan.dataset.agent = item.agent || '';
  ctxSpan.dataset.topic = item.topic || '';
  ctxSpan.dataset.sessionTurnCount = String(sessionTurnCount);
  ctxSpan.dataset.pinnedIds = JSON.stringify(_pc.pins);
  ctxSpan.dataset.mem = _pc.mem ? 'true' : 'false';
  ctxSpan.dataset.hasTrace = _hasTraceContent(item.status_raw, item.context, item.content || '') ? 'true' : 'false';
  ctxSpan.addEventListener('click', e => { e.stopPropagation(); showCtxPopup(ctxSpan); });
  asstHeader.appendChild(ctxSpan);

  asstBubble.appendChild(asstHeader);
  asstBubble.appendChild(promptFullDiv);

  const asstContent = document.createElement('div');
  if (item.status === 'error') {
    const authReq = parseAuthRequiredError(item.content || '');
    const raw = ((authReq ? authReq.message : item.content) || '').split('\n')[0].replace(/^CLI exited \d+:\s*/, '').trim();
    asstContent.innerHTML = (authReq ? authLoginButtonHtml(authReq.harness) : '')
      + `<span class="msg-error">${escapeHtml(raw) || 'Response interrupted.'}</span>`;
    // No auto-retry here (unlike the live-turn path in showError) — a
    // history row only has item.prompt as plain text, not the original
    // composer opts (route/broadcast/lookback) sendMessage needs to
    // reconstruct the exact same send; the button still logs in, the user
    // just resends manually afterward.
    if (authReq) wireAuthLoginButtons(asstContent, null);
  } else if (item.status === 'cancelled') {
    asstContent.innerHTML = `<span class="msg-error">${cancelledTurnLabel(item.content)}</span>`;
  } else {
    asstContent.innerHTML = renderAssistantMarkdown(item.content || '');
  }
  asstBubble.appendChild(asstContent);
  if (item.timestamp) asstBubble.dataset.ts = item.timestamp;
  if (item.id) addPinButton(asstBubble, item.id, item.topic || 'default', item.agent || null, item.session_id || null);
  if (item.id) addBookmarkButton(asstBubble, item.id, item.topic || 'default', item.agent || null);
  if (item.id) addReplyButton(asstBubble, item.topic || 'default', item.agent || null, !!item.adhoc);
  if (item.id) addBadResponseButton(asstBubble, item.id, item.topic || 'default', item.agent || null, !!item.marked_bad);

  if (container) container.appendChild(asstBubble);

  if (item.stats) {
    const completedAt = item.completed_at || item.stats?.completed_at || item.timestamp;
    const statsEl = addStats(asstBubble, item.stats, completedAt);
    statsEl.classList.add('history-item');
    addDeepDiveButton(asstBubble, item.topic || 'default', item.agent || null, !!item.adhoc, statsEl, item.id, completedAt);
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
        // A "worktree blocker" GitDiff tool describes another (earlier) turn's
        // still-unresolved worktree, not this message's own — resolve/discard
        // actions and status lookups must target that original turn's id.
        const toolMsgId = tool.worktree_blocker && tool.worktree_msg_id != null
          ? tool.worktree_msg_id
          : item.id;
        const block = makeToolBlock(tool, toolMsgId, item.timestamp, item.topic || 'default');
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
  if (item.adhoc != null) bubble.dataset.adhoc = item.adhoc ? 'true' : 'false';

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
  addThinkingHeightButton(bubble);
  return bubble;
}

async function replacePendingWithStoredItem(item, wipBubble) {
  try {
    const res = await fetch(`/chat/${item.id}/status`);
    if (!res.ok || !wipBubble.parentNode) return;
    const data = await res.json();
    if (data.status !== 'done' && data.status !== 'error' && data.status !== 'cancelled') return;
    if (data.status === 'error' && !String(data.content || '').trim()) return;
    wipBubble.remove();
    if (!shouldShowNewResponse(data)) return;
    appendHistoryItem(data, messages);
    updateInContextMarkers();
    updatePinCount();
    if (pinPanel.classList.contains('open')) renderPinPanel();
    refreshAllRevertButtons();
    scrollToBottom();
  } catch {}
}

const _flowRunWatchers = new Set();

async function attachFlowStep(msgId) {
  if (messages.querySelector(`[data-msg-id="${msgId}"]`)) return;
  try {
    const res = await fetch(`/chat/${msgId}/status`);
    if (!res.ok) return;
    const data = await res.json();
    if (messages.querySelector(`[data-msg-id="${msgId}"]`)) return;
    if (!shouldShowNewResponse(data)) return;
    if (data.status === 'pending') {
      const wipBubble = makeWipBubble(data);
      messages.appendChild(wipBubble);
      reconnectPendingItem(data, wipBubble);
    } else {
      appendHistoryItem(data, messages);
    }
    scrollToBottom();
  } catch {}
}

// Squid Flow route chain steps after the origin (target handoff, "<>" return)
// are dispatched server-side (agent/flow.py) with no client request involved —
// poll for them while this tab is open so they render live, same as a step
// this tab sent itself. Purely cosmetic: the chain completes server-side
// with or without a watcher.
function watchFlowRun(flowRunId, afterId, route = null) {
  if (!flowRunId || _flowRunWatchers.has(flowRunId)) return;
  _flowRunWatchers.add(flowRunId);
  let cursor = afterId;
  let polls = 0;
  const MAX_POLLS = 400; // ~10 min at 1.5s — well past any single-step timeout
  const timer = setInterval(tick, 1500);

  async function tick() {
    polls++;
    try {
      const res = await fetch(`/chat/flow/${encodeURIComponent(flowRunId)}/steps?after_id=${cursor}`);
      if (res.ok) {
        const data = await res.json();
        for (const row of data.messages || []) {
          cursor = Math.max(cursor, row.id);
          if (row.role === 'assistant') await attachFlowStep(row.id);
        }
        if (data.complete) {
          clearInterval(timer);
          _flowRunWatchers.delete(flowRunId);
          if (route) refreshRouteTurnCounts(route, { force: true });
          return;
        }
      }
    } catch {}
    if (polls >= MAX_POLLS) {
      clearInterval(timer);
      _flowRunWatchers.delete(flowRunId);
    }
  }
  tick();
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
    updateThinkingHeightButton(wipBubble);
  };

  if (item.content && live) {
    if (loader?.parentNode) loader.remove();
    live.textContent = item.content;
    updateThinkingHeightButton(wipBubble);
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
  es.addEventListener('loading', event => {
    // ADR-0037: local-model (e.g. Ollama) active load/unload visibility.
    try {
      const info = JSON.parse(event.data);
      statusBuf += (statusBuf ? '\n' : '') + (info.from
        ? `switching ${info.from} → ${info.to}…`
        : `loading ${info.to}…`);
      updatePreview();
    } catch {}
  });
  es.addEventListener('queued', event => {
    // ADR-0037: provider-scoped FIFO lane — replayed so a flow-dispatched
    // sibling turn (no live client) still shows it's waiting, not loading.
    try {
      const info = JSON.parse(event.data);
      statusBuf += (statusBuf ? '\n' : '') + `#${info.topic} · queued — position ${info.position}`;
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
      updateThinkingHeightButton(wipBubble);
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
      if (!res.ok) return;
      const data = await res.json();
      if (data.status === 'done' || data.status === 'cancelled' || (data.status === 'error' && String(data.content || '').trim())) {
        cancelPendingPoll(wipBubble);
        await replacePendingWithStoredItem(item, wipBubble);
      } else if (count >= MAX_POLLS) {
        cancelPendingPoll(wipBubble);
        const content = wipBubble.querySelector('.thinking-live');
        if (content) content.innerHTML += '<br><span class="msg-error">Timed out.</span>';
        updateThinkingHeightButton(wipBubble);
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
          updateThinkingHeightButton(wipBubble);
        }
      }
    } catch {}
  }, 2000);
  pendingPollTimers.set(wipBubble, timer);
}

function recoverPendingBubbles() {
  document.querySelectorAll('#messages > .msg-thinking.history-item:not(.msg-thinking-done)[data-msg-id]').forEach(bubble => {
    const id = parseInt(bubble.dataset.msgId || '', 10);
    if (!Number.isFinite(id) || id <= 0) return;
    cancelPendingPoll(bubble);
    reconnectPendingItem({
      id,
      topic: bubble.dataset.topic || 'default',
      agent: bubble.dataset.agent || null,
      adhoc: bubble.dataset.adhoc === 'true',
    }, bubble);
  });
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
        contentEl.innerHTML = renderAssistantMarkdown(data.content || '');
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
        contentEl.innerHTML = renderAssistantMarkdown(data.content);
        addLoader(contentEl);
      }
    } catch {
      clearInterval(timer);
    }
  }, 2000);
}

function makeUserBubble(text, topic, agent, backendFallback = null, adhoc = false, lookback = 0, source = 'human', broadcastAgents = null, flowRoute = null) {
  const div = document.createElement('div');
  div.className = 'msg user';
  if (source === 'workflow' || source === 'diff_viewer') {
    div.classList.add('user-system-generated');
    div.dataset.source = source;
    const label = document.createElement('div');
    label.className = 'user-source-label';
    label.textContent = source === 'diff_viewer' ? 'DIFF VIEWER' : 'WORKFLOW';
    div.appendChild(label);
  }
  const content = document.createElement('div');
  const showTag = flowRoute || broadcastAgents || (topic && (topic !== 'default' || agent || adhoc));
  if (showTag) {
    const tag = flowRoute
      ? makeFlowRouteTag(flowRoute)
      : broadcastAgents
      ? makeBroadcastRouteTag(broadcastAgents)
      : makeTopicTag(topic, agent || backendFallback, { clickable: true, adhoc, lookback });
    content.appendChild(tag);
    content.appendChild(document.createTextNode(' '));
  }
  content.appendChild(document.createTextNode(text));
  div.appendChild(content);
  return div;
}

function appendRouteChainTurnCount(parent, count) {
  if (count == null || count < 0) return;
  const countSpan = document.createElement('span');
  countSpan.className = 'chip-turn-count route-chain-turn-count';
  countSpan.textContent = `·${count}t`;
  countSpan.classList.toggle('mid', count > 10 && count <= 20);
  countSpan.classList.toggle('high', count > 20);
  parent.appendChild(countSpan);
}

function makeRouteChainMarker(route, opts = {}) {
  const div = document.createElement('div');
  div.className = 'route-chain-marker';
  const chain = parseRouteChain(route);
  if (!chain) {
    div.textContent = route || '';
    return div;
  }
  if (chain.complex) {
    appendColoredRouteTokens(div, chain.route || route || '', {
      topicClass: 'tag-topic',
      agentClass: 'tag-agent',
      freshClass: 'tag-adhoc',
    });
    return div;
  }
  const { topic, origin: originAgent, originFresh, operator, target: targetAgent, targetFresh, targetTopic } = chain;
  const topicSpan = document.createElement('span');
  topicSpan.className = 'tag-topic';
  topicSpan.textContent = `#${topic}`;
  div.appendChild(topicSpan);

  const originSpan = document.createElement('span');
  originSpan.className = 'tag-agent';
  originSpan.textContent = `@${originAgent}`;
  setAgentSlugColor(originSpan, originAgent);
  div.appendChild(originSpan);

  if (originFresh) {
    const freshSpan = document.createElement('span');
    freshSpan.className = 'tag-adhoc';
    freshSpan.textContent = '!';
    setAgentSlugColor(freshSpan, originAgent);
    div.appendChild(freshSpan);
  }
  appendRouteChainTurnCount(div, opts.turnCounts?.origin);

  const arrowSpan = document.createElement('span');
  arrowSpan.className = 'route-chain-arrow';
  arrowSpan.textContent = operator;
  div.appendChild(arrowSpan);

  if (targetTopic && targetTopic !== topic) {
    const targetTopicSpan = document.createElement('span');
    targetTopicSpan.className = 'tag-topic';
    targetTopicSpan.textContent = `#${targetTopic}`;
    div.appendChild(targetTopicSpan);
  }

  const targetSpan = document.createElement('span');
  targetSpan.className = 'tag-agent';
  targetSpan.textContent = `@${targetAgent}`;
  setAgentSlugColor(targetSpan, targetAgent);
  div.appendChild(targetSpan);

  if (targetFresh) {
    const freshSpan = document.createElement('span');
    freshSpan.className = 'tag-adhoc';
    freshSpan.textContent = '!';
    setAgentSlugColor(freshSpan, targetAgent);
    div.appendChild(freshSpan);
  }
  appendRouteChainTurnCount(div, opts.turnCounts?.target);
  return div;
}

function routeChainMarkerRouteForHead(route, topic, agent, adhoc) {
  const chain = parseRouteChain(route);
  // A '+' join gates the target on *every* joined origin completing (see
  // agent/flow.py next_chain_steps: a branch is skipped until none of its
  // origins resolve to None) — collapsing to "thisOrigin > target" would
  // draw an edge that doesn't exist yet (or isn't from this origin alone).
  // Comma-separated multi-origin fanout has no such gate — each origin
  // independently and immediately dispatches to the target — so collapsing
  // stays correct there.
  if (!chain?.multiOrigin || chain.join || chain.targets.length !== 1) return route;
  const origin = chain.origins.find(o =>
    o.topic === topic && o.agent === agent && !!o.fresh === !!adhoc
  );
  if (!origin) return route;
  const target = chain.targets[0];
  return _chainRouteText(origin.topic, origin.agent, target.agent, !!target.fresh, !!origin.fresh, chain.operator, target.topic);
}

// ---- Squid Flow playground view (ui/flow-lang.js) ------------------------
// Standalone grammar/compiler for Squid Flow route expressions — see
// docs/decisions/0032-route-chains-with-cwd-profile-agents.md. Not wired
// into the composer or backend yet; this view is for exploring the syntax.

const FLOW_EXAMPLES = [
  '#squid@codex<2>@review!',
  '#squid@codex>@review!',
  '#topic1@agent1,#topic2@agent1>#topic3',
  '#topic1@agent1,#topic2@agent2<>#topic3,#topic4@agentx',
  '#topic1@agent1+#topic2@agent1>#topic3',
  '#topic1@agent1<>#topic2+#topic3',
  '#squid@codex>#hive@review!',
  '#squid@codex>#hive',
  '#topic1@agent1=5:1d>@agent2',
  '#topic1@agent1=:5m>@agent2',
  '#topic1@agent1,@agent2',
  '#topic1@agent2,#topic3@agent1,#topic4@agent2',
  '#topic1@agent1!,@agent2!',
  '#topic1@agent1<3:1h>@agent2!',
];

let _flowViewInitialized = false;

function flowRouteLineEl(text, note, continuation) {
  const div = document.createElement('div');
  div.className = continuation ? 'flow-route-line flow-route-line-continuation' : 'flow-route-line';
  const tokenRe = /(#[A-Za-z0-9_.-]+)|(@[A-Za-z0-9_.-]+!?)|(<[^>]*>)|(=[^>]*>)|(>)|(\+)|(,)|(;)|(…)/g;
  let m;
  while ((m = tokenRe.exec(text))) {
    const span = document.createElement('span');
    if (m[1]) {
      span.className = 'tag-topic';
      span.textContent = m[1];
    } else if (m[2]) {
      const fresh = m[2].endsWith('!');
      const agentName = m[2].replace(/^@/, '').replace(/!$/, '');
      span.className = 'tag-agent';
      span.textContent = fresh ? `@${agentName}` : m[2];
      setAgentSlugColor(span, agentName);
      div.appendChild(span);
      if (fresh) {
        const freshSpan = document.createElement('span');
        freshSpan.className = 'tag-adhoc';
        freshSpan.textContent = '!';
        setAgentSlugColor(freshSpan, agentName);
        div.appendChild(freshSpan);
      }
      continue;
    } else if (m[3] || m[4]) {
      span.className = 'route-chain-arrow';
      span.textContent = m[0];
    } else if (m[5]) {
      span.className = 'route-chain-arrow';
      span.textContent = '>';
    } else if (m[6]) {
      span.className = 'route-chain-arrow';
      span.textContent = '+';
    } else if (m[7]) {
      span.className = 'route-chain-arrow';
      span.textContent = ',';
    } else if (m[8]) {
      span.className = 'route-chain-arrow';
      span.textContent = ';';
    } else if (m[9]) {
      span.className = 'route-chain-arrow';
      span.textContent = '…';
    }
    div.appendChild(span);
  }
  if (note) {
    const noteSpan = document.createElement('span');
    noteSpan.className = 'flow-route-note';
    noteSpan.textContent = ` · ${note}`;
    div.appendChild(noteSpan);
  }
  return div;
}

function renderFlowView() {
  const input = document.getElementById('flow-input');
  const statusEl = document.getElementById('flow-status');
  const keyLine = document.getElementById('flow-key-line');
  const canonicalList = document.getElementById('flow-canonical-list');
  const expandedList = document.getElementById('flow-expanded-list');
  const canonicalCount = document.getElementById('flow-canonical-count');
  const expandedCount = document.getElementById('flow-expanded-count');
  if (!input) return;

  const text = input.value.trim();
  keyLine.innerHTML = '';
  canonicalList.innerHTML = '';
  expandedList.innerHTML = '';
  canonicalCount.textContent = '';
  expandedCount.textContent = '';

  if (!text) {
    statusEl.className = 'flow-status idle';
    statusEl.textContent = 'Type an expression above.';
    return;
  }

  const result = SquidFlow.parse(text);
  if (!result.ok) {
    statusEl.className = 'flow-status err';
    statusEl.textContent = result.error;
    return;
  }

  statusEl.className = 'flow-status ok';
  statusEl.textContent = `Valid — ${result.canonical.length} independent branch${result.canonical.length === 1 ? '' : 'es'}.`;
  const keyEl = flowRouteLineEl(result.key);
  keyEl.classList.add('flow-key-line');
  keyLine.appendChild(keyEl);
  canonicalCount.textContent = `(${result.canonical.length})`;
  expandedCount.textContent = `(${result.expanded.length})`;
  for (const c of result.canonical) canonicalList.appendChild(flowRouteLineEl(c));
  for (const e of result.expanded) expandedList.appendChild(flowRouteLineEl(e.text, e.note, e.continuation));
}

// Pre-fills the Squid Flow view with a route and switches to it — for a
// future composer icon that sends the current route in for validation.
function openFlowView(routeText) {
  navigateView('flow');
  const input = document.getElementById('flow-input');
  if (input && routeText) {
    input.value = routeText;
    renderFlowView();
  }
}

function initFlowView() {
  if (_flowViewInitialized) return;
  _flowViewInitialized = true;
  const input = document.getElementById('flow-input');
  const examplesEl = document.getElementById('flow-examples');
  for (const ex of FLOW_EXAMPLES) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'flow-example-btn';
    btn.textContent = ex;
    btn.addEventListener('click', () => {
      input.value = ex;
      renderFlowView();
    });
    examplesEl.appendChild(btn);
  }
  input.addEventListener('input', renderFlowView);
  input.value = FLOW_EXAMPLES[0];
  renderFlowView();
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
    item.prompt_source || 'human',
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

  const input      = stats.input_tokens       || 0;
  const out        = stats.output_tokens      || 0;
  // ── Token semantics differ by backend (authoritative source: runners.py) ────────────
  // Claude: input_tokens is a ~2–4 token uncacheable residual. The user's actual message
  //   lands in cache_write (cache_creation_input_tokens). True total = input + cacheWrite
  //   + cacheRead. Seeing "3 new tokens" is correct, not a bug.
  // Codex: input_tokens is the FULL total; cache_read is a subset already inside it.
  //   Adding cache_read would double-count. output_tokens already includes reasoning.
  // We have gone back and forth on this — do not "fix" by treating input alone as total.
  // Heuristic to tell them apart (see _splitInputTokens): Claude has
  // input < (cacheRead + cacheWrite).
  // ─────────────────────────────────────────────────────────────────────────────────────
  const { isSplit, newInput: newThis, cacheRead, cacheWrite, total: inp } = _splitInputTokens(stats);
  const detailLabel = isSplit ? ` (${fmtNum(newThis)} new)`
                    : cacheRead > 0 ? ` (${fmtNum(newThis)} uncached)`
                    : '';
  const hasCost    = stats.cost_usd != null;
  const cost       = hasCost ? `$${stats.cost_usd.toFixed(4)}` : '';
  const cacheStr   = cacheRead ? ` · ${fmtNum(cacheRead)} cached` : '';
  const dur        = stats.duration_ms ? ` · ${(stats.duration_ms / 1000).toFixed(1)}s` : '';
  const timePrefix = timestamp ? fmtTime(timestamp) + '  ·  ' : '';

  const inputTokenClass = inp > 1_000_000 ? 'stats-token-danger'
                         : inp >= 500_000 ? 'stats-token-warn'
                         : '';

  el.appendChild(document.createTextNode(timePrefix));
  const inpSpan = document.createElement('span');
  inpSpan.className = inputTokenClass;
  inpSpan.textContent = `↑ ${fmtNum(inp)}`;
  el.appendChild(inpSpan);
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
    if (role === 'assistant') div.innerHTML = renderAssistantMarkdown(content);
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
    credsPopupId: 'balance-max-popup',
    errorTitle:   'DeepSeek balance unavailable',
    formatLabel:  (state) => state.displayText || '—',
  },
  kimi: {
    displayId:    'kimi-quota-display',
    pieArcId:     'kimi-pie-arc',
    fiveHourPctId:   'kimi-pct',
    labelId:      'kimi-quota-label',
    pieC:         2 * Math.PI * 6,
    credsPopupId: 'balance-max-popup',
    errorTitle:   'Kimi balance unavailable',
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
const QUOTA_ERROR_BACKGROUND_RETRY_DELAY = 60000;
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
  return _backendMetadata[backend]?.gauge?.type || providerMetadataForBackend(backend)?.gauge?.type || 'none';
}

// Prepaid-balance gauges (DeepSeek/Kimi) share one rendering path: balance
// text instead of a percentage, and the max-budget popup on click.
const BALANCE_GAUGES = new Set(['deepseek', 'kimi']);

function isBalanceGauge(ref) {
  return BALANCE_GAUGES.has(gaugeTypeFor(ref));
}

function quotaConfigFor(backend) {
  return QUOTA_CONFIG[gaugeTypeFor(backend)] || null;
}

async function ensureQuotaMetadata() {
  if (Object.keys(_providerMetadata).length) return;
  try {
    const res = await fetch('/health');
    if (!res.ok) return;
    const health = await res.json();
    _providerMetadata = health.providers || {};
    _harnessMetadata = {};
    for (const h of (health.harnesses || [])) _harnessMetadata[h.id] = h;
  } catch { /* quota remains hidden until health is reachable */ }
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
      updateSevenDayGauge(activeQuotaBackend);
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

  return resolveQuotaProvider(topicName, agentName);
}

async function resolveQuotaProvider(topicName, agentName) {
  if (!Object.keys(_providerMetadata).length) {
    await ensureQuotaMetadata();
  }
  if (!agentName && topicName) {
    const topics = await _acTopics();
    agentName = topics.find(t => t.name === topicName)?.agent || null;
  }
  if (!agentName) return 'anthropic';

  const agents = await _acAgents();
  const agent = agents.find(a => a.name === agentName);
  return agent?.provider || splitAgentRef(agent?.backend).provider || null;
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
  if (!cfg.sevenDayArcId) return;
  const arc = document.getElementById(cfg.sevenDayArcId);
  const label = document.getElementById(cfg.sevenDayLabelId);
  if (arc && state.sevenDay?.pct != null) {
    const filled = (state.sevenDay.pct / 100) * cfg.pieC;
    arc.setAttribute('stroke-dasharray', `${filled} ${cfg.pieC}`);
    arc.setAttribute('stroke', quotaGaugeColor(backend, state.sevenDay.pct));
  } else if (arc) {
    arc.setAttribute('stroke-dasharray', `0 ${cfg.pieC}`);
  }
  if (label) {
    label.textContent = state.sevenDay?.pct != null ? `${state.sevenDay.pct}` : '—';
  }
  // Update 7D suffix with days remaining
  const suffix = document.getElementById(cfg.sevenDaySuffixId);
  if (suffix) {
    if (state.sevenDay?.resetAt) {
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
    sevenDay: snapshot.sevenDay ?? null,
  });
}

function showQuotaError(backend, text, options = {}) {
  const cfg = quotaConfigFor(backend);
  if (!cfg) return;
  const state = quotaStateFor(backend);
  if (options.clearRetry !== false) clearQuotaRetry(state);
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
    showQuotaError(backend, text, { clearRetry: false });
    state.retryTimer = setTimeout(async () => {
      state.retryTimer = null;
      await fetchQuotaForBackend(backend);
    }, QUOTA_ERROR_BACKGROUND_RETRY_DELAY);
    return;
  }
  state.retryTimer = setTimeout(async () => {
    state.retryTimer = null;
    await fetchQuotaForBackend(backend);
  }, delay);
}

function showRecoverableQuotaError(backend, text) {
  const state = quotaStateFor(backend);
  showQuotaError(backend, text, { clearRetry: false });
  if (state.retryTimer) return;
  state.retryTimer = setTimeout(async () => {
    state.retryTimer = null;
    await fetchQuotaForBackend(backend);
  }, QUOTA_ERROR_BACKGROUND_RETRY_DELAY);
}

async function fetchQuotaForBackend(backend) {
  const provider = quotaStatusProviderKey(backend);
  let cfg = quotaConfigFor(provider);
  if (!cfg && !Object.keys(_providerMetadata).length) {
    await ensureQuotaMetadata();
    cfg = quotaConfigFor(provider);
  }
  if (!cfg) return null;
  const state = quotaStateFor(provider);
  if (state.inFlight) return state.raw == null ? null : { backend: provider, ...state };
  state.inFlight = true;
  const label = backendDisplayName(provider);
  try {
    const url = `/quota/provider/${encodeURIComponent(provider)}`;
    const res = await fetch(url);
    if (!res.ok) {
      const isAuthFailure = [400, 401, 403].includes(res.status);
      if (isAuthFailure && gaugeTypeFor(provider) === 'codex') {
        showRecoverableQuotaError(provider, `${label} auth`);
      } else if (isAuthFailure) showQuotaError(provider, `${label} auth`);
      else showTransientQuotaError(provider, `${label} error`);
      return null;
    }
    const data = await res.json();
    if (data.status === 'none') {
      clearQuotaRetry(state);
      return null;
    }
    if (!data.status) {
      showQuotaError(provider, `${label} n/a`);
      return null;
    }
    const resetAt = typeof data.reset_at === 'number'
      ? data.reset_at * 1000
      : (data.reset_at ? new Date(data.reset_at).getTime() : null);
    const snapshot = {
      raw: data.raw ?? null,
      pct: data.used_percent != null
        ? Math.max(0, Math.min(100, Math.round(data.used_percent)))
        : (isBalanceGauge(provider) && data.max_budget_pct != null
          ? Math.max(0, Math.min(100, Math.round(data.max_budget_pct)))
          : null),
      resetAt,
      title: data.title || '',
      displayText: isBalanceGauge(provider)
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
    renderQuotaLoaded(provider, snapshot);
    return { backend: provider, ...state };
  } catch {
    showTransientQuotaError(provider, `${label} error`);
    return null;
  } finally {
    state.inFlight = false;
  }
}

async function fetchQuota() {
  return fetchQuotaForBackend('anthropic');
}

function initQuota() {
  const cfg = QUOTA_CONFIG.claude;
  quotaDisplay.style.setProperty('--quota-accent', agentThemeColor('anthropic'));
  setVisibleQuotaBackend('anthropic');
  quotaDisplay.innerHTML = `
    <span class="quota-7d-group">
      <svg id="quota-7d-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
        <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
        <circle id="${cfg.sevenDayArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('anthropic')}"
                stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
                transform="rotate(-90 9 9)"/>
        <text id="${cfg.sevenDayLabelId}" x="9" y="9" text-anchor="middle" dominant-baseline="central" font-size="9" fill="#fff">—</text>
      </svg>
      <span id="${cfg.sevenDaySuffixId}" class="quota-7d-suffix">7D</span>
    </span>
    <span style="display:inline-flex;align-items:center;gap:0.1rem">
      <svg id="quota-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
        <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
        <circle id="${cfg.pieArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('anthropic')}"
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
  if (!Object.keys(_providerMetadata).length) await ensureQuotaMetadata();
  const provider = Object.keys(_providerMetadata)
    .find(id => _providerMetadata[id]?.gauge?.type === 'codex') || 'openai';
  return fetchQuotaForBackend(provider);
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
    <span style="display:inline-flex;align-items:center;gap:0.1rem">
      <svg id="codex-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
        <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
        <circle id="${cfg.pieArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor('codex')}"
                stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
                transform="rotate(-90 9 9)"/>
        <text id="${cfg.fiveHourPctId}" x="9" y="9" text-anchor="middle" dominant-baseline="central" font-size="9" fill="#fff">—</text>
      </svg>
      <span id="${cfg.labelId}"></span>
    </span>`;
  const credsPopup = document.getElementById(cfg.credsPopupId);
  codexQuotaDisplay.addEventListener('click', () => credsPopup.classList.toggle('open'));
  document.addEventListener('click', (e) => {
    if (!codexQuotaDisplay.contains(e.target) && !credsPopup.contains(e.target))
      credsPopup.classList.remove('open');
  });
  fetchCodexQuota();
}

function initBalanceQuota(gaugeType) {
  const cfg = QUOTA_CONFIG[gaugeType];
  const displayEl = document.getElementById(cfg.displayId);
  if (!displayEl) return;
  displayEl.style.setProperty('--quota-accent', agentThemeColor(gaugeType));
  displayEl.innerHTML = `
    <span style="display:inline-flex;align-items:center">
      <svg id="${gaugeType}-pie" width="18" height="18" viewBox="0 0 18 18" style="flex-shrink:0">
        <circle cx="9" cy="9" r="6" fill="none" stroke="#2a2a3c" stroke-width="4"/>
        <circle id="${cfg.pieArcId}" cx="9" cy="9" r="6" fill="none" stroke="${agentThemeColor(gaugeType)}"
                stroke-width="4" stroke-dasharray="0 ${cfg.pieC}" stroke-linecap="round"
                transform="rotate(-90 9 9)"/>
        <text id="${cfg.fiveHourPctId}" x="9" y="9" text-anchor="middle" dominant-baseline="central" font-size="9" fill="#fff"></text>
      </svg>
      <span id="${cfg.labelId}">—</span>
    </span>`;

  const popup = document.getElementById(cfg.credsPopupId);
  displayEl.addEventListener('click', () => {
    popup.dataset.gauge = gaugeType;
    popup.classList.toggle('open');
  });
}

function initBalanceMaxPopup() {
  const popup    = document.getElementById('balance-max-popup');
  const maxInput = document.getElementById('balance-max-input');
  const saveBtn  = document.getElementById('balance-max-save');
  const clearBtn = document.getElementById('balance-max-clear');
  const status   = document.getElementById('balance-max-status');
  if (!popup || !maxInput || !saveBtn) return;

  // One outside-click closer for the shared popup — any balance display counts
  // as inside, otherwise gauge A's closer would kill the popup opened by B.
  document.addEventListener('click', (e) => {
    const inTrigger = [...BALANCE_GAUGES].some(g =>
      document.getElementById(QUOTA_CONFIG[g].displayId)?.contains(e.target));
    if (!inTrigger && !popup.contains(e.target)) popup.classList.remove('open');
  });

  saveBtn.addEventListener('click', async () => {
    const val = parseFloat(maxInput.value);
    if (!val || val <= 0) { status.textContent = 'enter a positive amount'; return; }
    const res = await fetch(`/config/${popup.dataset.gauge}/max-budget`, {
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
    const res = await fetch(`/config/${popup.dataset.gauge}/max-budget`, { method: 'DELETE' });
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

let statsPeriod = 'turn';
let statsBreakdown = '';
// anchor: null means "now"; otherwise an ISO timestamp the days/hours window ends at.
let statsFilters = { days: -3, agents: [], topics: [], adhoc: 'all', status: [], flow: 'all', anchor: null };
const STATS_STATUS_LABELS = { done: 'Complete', error: 'Error', cancelled: 'Cancelled' };
// statsFilters.days doubles as an hours flag: negative values mean
// "-days" hours (e.g. -3 = 3h). Only the 'turn' grain offers sub-day ranges,
// since coarser grains bucket by hour/day/week and hours would be meaningless there.
const STATS_DAY_OPTIONS_DEFAULT = [
  { value: 1, label: '1d' },
  { value: 3, label: '3d' },
  { value: 7, label: '7d' },
  { value: 14, label: '14d' },
  { value: 28, label: '28d' },
  { value: 90, label: '90d' },
  { value: 0, label: 'All Time' },
];
// Capped at 7d: per-turn points are dense enough that longer ranges are mostly noise.
const STATS_DAY_OPTIONS_TURN = [
  { value: -1, label: '1h' },
  { value: -3, label: '3h' },
  { value: -6, label: '6h' },
  { value: -12, label: '12h' },
  { value: 1, label: '1d' },
  { value: 3, label: '3d' },
  { value: 7, label: '7d' },
];
// Floored at 14d: fewer than two weekly buckets makes week-over-week comparison meaningless.
const STATS_DAY_OPTIONS_WEEKLY = STATS_DAY_OPTIONS_DEFAULT.filter(o => o.value === 0 || o.value >= 14);

function _statsDayOptionsFor(period) {
  if (period === 'turn') return STATS_DAY_OPTIONS_TURN;
  if (period === 'weekly') return STATS_DAY_OPTIONS_WEEKLY;
  return STATS_DAY_OPTIONS_DEFAULT;
}

function _syncStatsDayOptions(period) {
  const options = _statsDayOptionsFor(period);
  const select = document.getElementById('sf-days');
  const valid = new Set(options.map(o => o.value));
  if (!valid.has(statsFilters.days)) {
    statsFilters.days = period === 'turn' ? 7 : (period === 'weekly' ? 14 : 1);
  }
  select.innerHTML = options.map(o => `<option value="${o.value}">${o.label}</option>`).join('');
  select.value = String(statsFilters.days);
}
let statsChartY1 = 'tokens_in';
let statsChartAggY1 = 'sum';
// Additional chart series beyond the primary (Y1) one — each entry is
// { metric, agg, axis } where axis is 'y1' (shares Y1's scale) or 'y2' (its
// own right-hand scale). Unlike Y1, the same metric can appear more than
// once here (e.g. P50 and P95 of the same measure).
let statsChartExtra = [{ metric: 'cache_hit_rate', agg: 'avg', axis: 'y2' }];
// Null when no popover is open; otherwise the zero-based chart series index.
// Index 0 is persisted as measure.primary for backward compatibility, but the
// UI treats it like any other series chip.
let statsChartEditIndex = null;
let statsChartInstance = null;
let _lastStatsRows = null;
let _statsPage = 0;
const _STATS_PAGE_SIZE = 10;
// One-shot: the msg_id to highlight+scroll to on the next render, set when
// navigating in from a message's footer stats link. Cleared once applied —
// not "the top row" in general, since concurrent topics can put a later-
// finishing turn after it within the same window.
let _statsHighlightMsgId = null;

function _applyStatsHighlight() {
  if (_statsHighlightMsgId == null) return;
  const target = _statsHighlightMsgId;
  _statsHighlightMsgId = null;
  const row = [...statsContent.querySelectorAll('tbody tr[data-msg-ids]')].find(tr =>
    String(tr.dataset.msgIds || '').split(',').map(id => parseInt(id, 10)).includes(target)
  );
  if (!row) return;
  row.classList.add('stats-row-highlight');
  row.scrollIntoView({ block: 'center' });
}
const DEFAULT_STATS_MEASURES = ['turns', 'avg_tokens_turn', 'cache_hit_rate', 'cache_read', 'cache_write', 'duration', 'new_input', 'sessions', 'tokens_in', 'tokens_out', 'tokens_total'];
// How many measure columns the By Turn table's mobile CSS can squeeze to
// 100% width and keep readable. Independent of DEFAULT_STATS_MEASURES,
// which is how many measures are *pre-selected*, not how many fit compact.
const _STATS_TURN_TABLE_COMPACT_LIMIT = 4;
// Duration is available in aggregate views too, but stays out of the base
// default to keep the initial table compact. It is auto-added in By Turn.
const _statsMeasures = new Set(DEFAULT_STATS_MEASURES);
let _statsDurationAutoAddedForTurn = false;
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
  if (statsPeriod === 'turn') { renderTurnStats(_lastStatsRows); _renderChart(_lastStatsRows); }
  else if (statsBreakdown) { renderAgentBreakdownStats(_lastStatsRows); _renderBreakdownChart(_lastStatsRows); }
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

const CTX_POPUP_LAYER_CLASSES = ['stats-turn-popup', 'page-context-popup', 'modal-context-popup'];
const CTX_POPUP_MODAL_SCOPE = '#msg-modal, #memory-modal, #topic-delete-modal, #agent-session-modal, #preset-name-modal, #file-modal';

function _setCtxPopupLayer(popup, layerClass) {
  popup.classList.remove(...CTX_POPUP_LAYER_CLASSES);
  popup.classList.add(layerClass);
}

function _closeCtxPopup(popup = document.getElementById('ctx-popup')) {
  if (!popup) return;
  popup.classList.remove('open');
  popup._forSpanEl = null;
}

function _positionCtxPopupNearAnchor(popup, anchorEl) {
  const margin = 6;
  const app = document.getElementById('app');
  const appRect = app.getBoundingClientRect();
  const anchorRect = anchorEl.getBoundingClientRect();
  const topbarRect = document.getElementById('topbar')?.getBoundingClientRect();
  const inModal = !!anchorEl.closest(CTX_POPUP_MODAL_SCOPE);
  const minTop = inModal ? Math.max(appRect.top + margin, margin) : Math.max(appRect.top + margin, (topbarRect?.bottom || 0) + margin);
  const maxBottom = Math.min(window.innerHeight - margin, appRect.bottom - margin);
  const minLeft = appRect.left + margin;
  const maxRight = appRect.right - margin;

  popup.style.bottom = '';
  popup.style.right = '';
  popup.style.maxHeight = `${Math.max(120, maxBottom - minTop)}px`;

  const popupRect = popup.getBoundingClientRect();
  let top = anchorRect.top - popupRect.height - margin;
  if (top < minTop) top = anchorRect.bottom + margin;
  top = Math.max(minTop, Math.min(top, maxBottom - popupRect.height));

  let left = anchorRect.right - popupRect.width;
  left = Math.max(minLeft, Math.min(left, maxRight - popupRect.width));

  popup.style.top = `${top - appRect.top}px`;
  popup.style.left = `${left - appRect.left}px`;
}

function _positionStatsTurnsPopup(popup, anchorEl) {
  const margin = 6;
  const statsRect = document.getElementById('view-stats').getBoundingClientRect();
  const appRect = document.getElementById('app').getBoundingClientRect();
  const anchorRect = anchorEl.getBoundingClientRect();
  const availableHeight = Math.max(120, statsRect.height - margin * 2);
  const readableMaxHeight = Math.round(window.innerHeight * 0.55);
  popup.style.maxHeight = `${Math.max(120, Math.min(availableHeight, readableMaxHeight))}px`;
  popup.style.bottom = '';
  popup.style.right = '';

  const popupRect = popup.getBoundingClientRect();
  const minTop = statsRect.top + margin;
  const maxTop = statsRect.bottom - popupRect.height - margin;
  let top = anchorRect.top - popupRect.height - margin;
  if (top < minTop) top = Math.min(anchorRect.bottom + margin, maxTop);
  top = Math.max(minTop, Math.min(top, maxTop));

  const minLeft = statsRect.left + margin;
  const maxLeft = statsRect.right - popupRect.width - margin;
  const left = Math.max(minLeft, Math.min(anchorRect.right - popupRect.width, maxLeft));

  popup.style.top = `${top - appRect.top}px`;
  popup.style.left = `${left - appRect.left}px`;
}

function showStatsTurnsPopup(anchorEl, ids) {
  if (!ids.length) return;
  if (ids.length === 1) {
    openMsgModal(ids[0]);
    return;
  }

  let popup = document.getElementById('stats-turn-popup');
  if (!popup) {
    popup = document.createElement('div');
    popup.id = 'stats-turn-popup';
    document.getElementById('app').appendChild(popup);
  }
  if (popup._forStatsTurnsEl === anchorEl && popup.classList.contains('open')) {
    popup.classList.remove('open');
    popup._forStatsTurnsEl = null;
    return;
  }
  popup._forSpanEl = null;
  popup._forStatsTurnsEl = anchorEl;
  _setCtxPopupLayer(popup, 'stats-turn-popup');
  popup.innerHTML = `<div class="ctx-popup-row"><span class="ctx-popup-key">turns</span></div>` +
    ids.map(id => `<div class="ctx-popup-pin" data-turn-id="${id}">
      <span class="ctx-popup-tag">#${id}</span>
      <span class="ctx-popup-preview" id="stats-turn-preview-${id}">loading…</span>
      <button type="button" class="ctx-popup-jump-btn" data-jump-msg-id="${id}" title="/jump ${id}">/jump</button>
    </div>`).join('');
  popup.classList.add('open');

  popup.querySelectorAll('.ctx-popup-jump-btn[data-jump-msg-id]').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      jumpToMessage(parseInt(btn.dataset.jumpMsgId, 10));
    });
  });

  popup.querySelectorAll('.ctx-popup-pin[data-turn-id]').forEach(row => {
    row.addEventListener('click', () => {
      openMsgModal(parseInt(row.dataset.turnId, 10));
    });
  });
  fetch(`/chat/previews?ids=${encodeURIComponent(ids.join(','))}`)
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(payload => {
      const previews = new Map((payload.items || []).map(item => [Number(item.id), item.preview || '(empty)']));
      ids.forEach(id => {
        const el = document.getElementById(`stats-turn-preview-${id}`);
        if (el) el.textContent = (previews.get(id) || '(missing)').slice(0, 80);
      });
    })
    .catch(() => {
      ids.forEach(id => {
        const el = document.getElementById(`stats-turn-preview-${id}`);
        if (el) el.textContent = 'failed to load';
      });
    });

  _positionStatsTurnsPopup(popup, anchorEl);
}

const CHART_METRICS = {
  turns:          { label: 'Turns',          fn: r => (r.total_turns || 0),      color: 'rgba(100,160,255,1)',  fill: 'rgba(100,160,255,0.08)' },
  cost:           { label: 'Cost ($)',       fn: r => (r.cost_usd || 0),         color: 'rgba(255,160,80,1)',   fill: 'rgba(255,160,80,0.08)'  },
  tokens_in:      { label: 'Tokens In',      fn: r => _statsInputTokens(r),      color: 'rgba(80,200,120,1)',   fill: 'rgba(80,200,120,0.08)'  },
  tokens_out:     { label: 'Tokens Out',     fn: r => (r.output_tokens || 0),    color: 'rgba(200,100,200,1)',  fill: 'rgba(200,100,200,0.08)' },
  tokens_total:   { label: 'Total Tokens',   fn: r => _statsInputTokens(r) + (r.output_tokens || 0), color: 'rgba(120,210,180,1)', fill: 'rgba(120,210,180,0.08)' },
  sessions:       { label: 'Sessions',       fn: r => (r.sessions || 0),         color: 'rgba(200,200,60,1)',   fill: 'rgba(200,200,60,0.08)'  },
  quota:          { label: 'Quota Delta',    fn: r => (r.quota_delta || 0),      color: 'rgba(120,200,220,1)',  fill: 'rgba(120,200,220,0.08)' },
  duration:       { label: 'Duration (s)',   fn: r => (r.duration_ms || 0) / 1000, color: 'rgba(255,120,120,1)', fill: 'rgba(255,120,120,0.08)' },
  cache_read:     { label: 'Cache Read',     fn: r => _splitInputTokens(r).cacheRead,  color: 'rgba(90,180,255,1)',   fill: 'rgba(90,180,255,0.08)'  },
  cache_write:    { label: 'Cache Write',    fn: r => _splitInputTokens(r).cacheWrite, color: 'rgba(180,140,255,1)',  fill: 'rgba(180,140,255,0.08)' },
  cancelled_turns:{ label: 'Cancelled',      fn: r => (r.cancelled_turns || 0), color: 'rgba(190,150,90,1)', fill: 'rgba(190,150,90,0.08)' },
  new_input:      { label: 'New Input',      fn: r => _splitInputTokens(r).newInput,   color: 'rgba(80,200,120,1)',   fill: 'rgba(80,200,120,0.08)'  },
  error_turns:    { label: 'Errors',         fn: r => (r.error_turns || 0),     color: 'rgba(255,100,100,1)',  fill: 'rgba(255,100,100,0.08)' },
  marked_bad:     { label: 'Bad Responses',  fn: r => (r.marked_bad || 0),      color: 'rgba(220,80,110,1)',   fill: 'rgba(220,80,110,0.08)' },
  cache_hit_rate: { label: 'Cache Hit %',    fn: r => (_cacheHitRate(r) || 0),   color: 'rgba(230,200,80,1)',   fill: 'rgba(230,200,80,0.08)'  },
  avg_tokens_turn:{ label: 'Avg Tokens/Turn',fn: r => (_avgTokensPerTurn(r) || 0), color: 'rgba(150,150,255,1)', fill: 'rgba(150,150,255,0.08)' },
};

const STATS_AGG_LABELS = { sum: 'SUM', avg: 'AVG', min: 'MIN', max: 'MAX', p50: 'P50', p75: 'P75', p95: 'P95' };
const STATS_METRIC_AGGS = {
  turns: ['sum'],
  sessions: ['sum'],
  cost: ['sum', 'avg', 'min', 'max', 'p50', 'p75', 'p95'],
  tokens_in: ['sum', 'avg', 'min', 'max', 'p50', 'p75', 'p95'],
  tokens_out: ['sum', 'avg', 'min', 'max', 'p50', 'p75', 'p95'],
  tokens_total: ['sum', 'avg', 'min', 'max', 'p50', 'p75', 'p95'],
  quota: ['sum', 'avg', 'min', 'max', 'p50', 'p75', 'p95'],
  duration: ['avg', 'sum', 'min', 'max', 'p50', 'p75', 'p95'],
  cache_read: ['sum', 'avg', 'min', 'max', 'p50', 'p75', 'p95'],
  cache_write: ['sum', 'avg', 'min', 'max', 'p50', 'p75', 'p95'],
  cancelled_turns: ['sum'],
  new_input: ['sum', 'avg', 'min', 'max', 'p50', 'p75', 'p95'],
  error_turns: ['sum'],
  marked_bad: ['sum'],
  // Ratios/derived-per-bucket values — there's nothing to vary the aggregation over.
  cache_hit_rate: ['avg'],
  avg_tokens_turn: ['sum'],
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

// Same categorization the per-message tooltip uses (see addStats()): Claude-style
// backends report a tiny uncacheable "input_tokens" residual plus cache_write/
// cache_read, so input < cache_read+cache_write signals a split; Codex-style
// backends report input_tokens as the full total with cache_read as a subset
// already inside it. Single source of truth so "Tokens In" and the separate
// Cache Read/Cache Write/New Input measures always add up to the same total.
function _splitInputTokens(row) {
  const raw = row.input_tokens || 0;
  const cacheRead = row.cache_read_tokens || 0;
  const cacheWrite = row.cache_write_tokens || 0;
  const isSplit = (cacheRead + cacheWrite) > 0 && raw < (cacheRead + cacheWrite);
  const newInput = isSplit ? raw + cacheWrite : Math.max(0, raw - cacheRead);
  return { isSplit, newInput, cacheRead, cacheWrite, total: newInput + cacheRead };
}

function _statsInputTokens(row) {
  return _splitInputTokens(row).total;
}

function _cacheHitRate(row) {
  const { cacheRead, newInput } = _splitInputTokens(row);
  const total = cacheRead + newInput;
  return total > 0 ? (cacheRead / total) * 100 : null;
}

function _avgTokensPerTurn(row) {
  const turns = row.total_turns || 0;
  if (!turns) return null;
  return (_statsInputTokens(row) + (row.output_tokens || 0)) / turns;
}

function _statsMetricValue(row, metric) {
  if (metric === 'turns') return row.total_turns || 0;
  if (metric === 'cost') return row.cost_usd || 0;
  if (metric === 'tokens_in') return _statsInputTokens(row);
  if (metric === 'tokens_out') return row.output_tokens || 0;
  if (metric === 'tokens_total') return _statsInputTokens(row) + (row.output_tokens || 0);
  if (metric === 'sessions') return row.sessions || 0;
  if (metric === 'quota') return row.quota_delta || 0;
  if (metric === 'duration') return (row.duration_ms || 0) / 1000;
  if (metric === 'cache_read') return _splitInputTokens(row).cacheRead;
  if (metric === 'cache_write') return _splitInputTokens(row).cacheWrite;
  if (metric === 'cancelled_turns') return row.cancelled_turns || 0;
  if (metric === 'new_input') return _splitInputTokens(row).newInput;
  if (metric === 'error_turns') return row.error_turns || 0;
  if (metric === 'marked_bad') return row.marked_bad || 0;
  if (metric === 'cache_hit_rate') return _cacheHitRate(row);
  if (metric === 'avg_tokens_turn') return _avgTokensPerTurn(row);
  return row.total_turns || 0;
}

function _formatStatsMetricValue(value, metric) {
  if (metric === 'cost') return _formatCost(value);
  if (metric === 'quota') return _formatQuotaDelta(value);
  if (metric === 'duration') return `${(value || 0).toFixed(1)}s`;
  if (metric === 'cache_hit_rate') return value == null ? '—' : `${value.toFixed(1)}%`;
  return fmtNum(value || 0);
}

function _statsTurnMessageIds(row) {
  const raw = row?.message_ids ?? row?.msg_ids ?? row?.msg_id ?? '';
  if (Array.isArray(raw)) return raw.map(id => parseInt(id, 10)).filter(Number.isFinite);
  if (typeof raw === 'number') return Number.isFinite(raw) ? [raw] : [];
  return String(raw || '')
    .split(',')
    .map(id => parseInt(id, 10))
    .filter(Number.isFinite);
}

function _statsTurnsCell(row) {
  const count = row.total_turns || 0;
  if (!count) return '—';
  const ids = _statsTurnMessageIds(row);
  if (!ids.length) return fmtNum(count);
  return `<button type="button" class="stats-turn-link" data-turn-ids="${escapeHtml(ids.join(','))}">${fmtNum(count)}</button>`;
}

function _defaultStatsAgg(metric) {
  return (STATS_METRIC_AGGS[metric] || ['sum'])[0];
}

function _normalizeStatsAgg(metric, agg) {
  const allowed = STATS_METRIC_AGGS[metric] || ['sum'];
  return allowed.includes(agg) ? agg : _defaultStatsAgg(metric);
}

function _syncStatsAggSelect(select, metric, agg) {
  if (!select) return;
  const allowed = STATS_METRIC_AGGS[metric] || ['sum'];
  const value = _normalizeStatsAgg(metric, agg);
  if (statsPeriod === 'turn') {
    select.innerHTML = `<option value="${value}">RAW</option>`;
    select.value = value;
    return;
  }
  select.innerHTML = allowed.map(key => `<option value="${key}">${STATS_AGG_LABELS[key]}</option>`).join('');
  select.value = value;
}

function _statsChartSeries() {
  return [{ metric: statsChartY1, agg: statsChartAggY1, axis: 'y1' }, ...statsChartExtra];
}

function _setStatsChartSeries(series) {
  const seen = new Set();
  const normalized = (series || [])
    .map((entry, i) => {
      const metric = entry?.metric && CHART_METRICS[entry.metric] ? entry.metric : 'turns';
      return {
        metric,
        agg: _normalizeStatsAgg(metric, entry?.agg || _defaultStatsAgg(metric)),
        axis: i === 0 ? 'y1' : (entry?.axis === 'y2' ? 'y2' : 'y1'),
      };
    })
    .filter(entry => {
      const key = `${entry.metric}:${entry.agg}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  if (!normalized.length) normalized.push({ metric: 'turns', agg: 'sum', axis: 'y1' });
  statsChartY1 = normalized[0].metric;
  statsChartAggY1 = normalized[0].agg;
  statsChartExtra = normalized.slice(1);
}

function _statsChartEntry(index) {
  if (index === 0) return { metric: statsChartY1, agg: statsChartAggY1, axis: 'y1' };
  const entry = statsChartExtra[index - 1];
  return entry ? { ...entry } : null;
}

function _updateStatsChartEntry(index, patch) {
  const entry = { ..._statsChartEntry(index), ...patch };
  if (!entry.metric) return;
  entry.agg = _normalizeStatsAgg(entry.metric, entry.agg || _defaultStatsAgg(entry.metric));
  entry.axis = index === 0 ? 'y1' : (entry.axis === 'y2' ? 'y2' : 'y1');
  if (index === 0) {
    statsChartY1 = entry.metric;
    statsChartAggY1 = entry.agg;
  } else if (statsChartExtra[index - 1]) {
    statsChartExtra[index - 1] = entry;
  }
  _setStatsChartSeries(_statsChartSeries());
}

function _removeStatsChartEntry(index) {
  const series = _statsChartSeries();
  if (series.length <= 1) return;
  series.splice(index, 1);
  _setStatsChartSeries(series);
  if (statsChartEditIndex === index) statsChartEditIndex = null;
  else if (statsChartEditIndex != null && statsChartEditIndex > index) statsChartEditIndex--;
}

function _statsChartAggField(metric, agg) {
  return `chart_${metric}_${agg}`;
}

function _statsChartSeriesValue(row, metric, agg) {
  // Raw per-turn rows have no chart_<metric>_<agg> field (nothing was grouped
  // to aggregate) — always use the row's own value rather than a stale/absent
  // aggregate, regardless of whatever agg was last selected in another view.
  if (statsPeriod === 'turn') return _statsMetricValue(row, metric);
  const field = _statsChartAggField(metric, agg);
  if (Object.prototype.hasOwnProperty.call(row, field) && row[field] != null) return row[field];
  if (metric === 'duration' && agg === 'avg' && row.duration_ms != null) return row.duration_ms / 1000;
  if (agg === 'sum') return _statsMetricValue(row, metric);
  const value = row[_statsChartAggField(metric, agg)];
  return value == null ? 0 : value;
}

function _statsChartSeriesLabel(metric, agg) {
  const m = CHART_METRICS[metric] || CHART_METRICS.turns;
  const aggLabel = statsPeriod === 'turn' ? 'RAW' : (STATS_AGG_LABELS[agg] || agg.toUpperCase());
  return `${aggLabel} ${m.label}`;
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
  _setStatsChartSeries(_statsChartSeries());
  _renderStatsChartExtraRows();
}

function _statsChartAxisLabel(axis) {
  return axis === 'y2' ? 'R' : 'L';
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
  { key: 'turns', label: 'Turns', row: r => _statsTurnsCell(r), total: t => t.turns || '—' },
  {
    key: 'avg_tokens_turn', label: 'Avg Tokens/Turn',
    row: r => { const v = _avgTokensPerTurn(r); return v != null ? fmtNum(v) : '—'; },
    total: t => t.turns ? fmtNum((t.tokens_in + t.tokens_out) / t.turns) : '—',
  },
  {
    key: 'cache_hit_rate', label: 'Cache Hit %', title: 'Cache reads as a share of total input tokens',
    row: r => _formatStatsMetricValue(_cacheHitRate(r), 'cache_hit_rate'),
    total: t => {
      const denom = (t.cache_read || 0) + (t.new_input || 0);
      return denom > 0 ? `${((t.cache_read / denom) * 100).toFixed(1)}%` : '—';
    },
  },
  { key: 'cache_read', label: 'Cache Read', row: r => fmtNum(_splitInputTokens(r).cacheRead), total: t => fmtNum(t.cache_read || 0) },
  { key: 'cache_write', label: 'Cache Write', row: r => fmtNum(_splitInputTokens(r).cacheWrite), total: t => fmtNum(t.cache_write || 0) },
  { key: 'cancelled_turns', label: 'Cancelled', row: r => fmtNum(r.cancelled_turns || 0), total: t => fmtNum(t.cancelled_turns || 0) },
  { key: 'cost', label: 'Cost', row: r => _formatCost(r.cost_usd), total: t => _formatCost(t.cost || 0) },
  { key: 'duration', label: 'Duration', row: r => r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)}s` : '—', total: t => t.duration != null ? `${(t.duration / 1000).toFixed(1)}s` : '—' },
  { key: 'error_turns', label: 'Errors', row: r => fmtNum(r.error_turns || 0), total: t => fmtNum(t.error_turns || 0) },
  { key: 'marked_bad', label: 'Bad Responses', row: r => fmtNum(r.marked_bad || 0), total: t => fmtNum(t.marked_bad || 0) },
  { key: 'new_input', label: 'New Input', row: r => fmtNum(_splitInputTokens(r).newInput), total: t => fmtNum(t.new_input || 0) },
  { key: 'quota', label: 'Quota Delta', title: 'Observed account meter change; not exact attributed usage', row: r => _formatQuotaDelta(r.quota_delta), total: t => _formatQuotaDelta(t.quota) },
  { key: 'sessions', label: 'Sessions', row: r => r.sessions || 0, total: t => t.sessions || 0 },
  { key: 'tokens_in', label: 'Tokens In', row: r => fmtNum(_statsInputTokens(r)), total: t => fmtNum(t.tokens_in || 0) },
  { key: 'tokens_out', label: 'Tokens Out', row: r => fmtNum(r.output_tokens || 0), total: t => fmtNum(t.tokens_out || 0) },
  { key: 'tokens_total', label: 'Total Tokens', row: r => fmtNum(_statsInputTokens(r) + (r.output_tokens || 0)), total: t => fmtNum((t.tokens_in || 0) + (t.tokens_out || 0)) },
];

function _firstSelectedNonTurnMeasure() {
  const preferred = DEFAULT_STATS_MEASURES.find(key => key !== 'turns' && key !== 'sessions' && _statsMeasureSelected(key));
  return preferred || (STATS_TABLE_MEASURES.find(m => m.key !== 'turns' && _statsMeasureSelected(m.key)) || STATS_TABLE_MEASURES[0]).key;
}

function _statsChartMetricKeys() {
  return STATS_TABLE_MEASURES.map(m => m.key);
}

// Returns whether the requested (metric, agg) pairs changed, since that
// requires a refetch (a newly-added one's backend chart_<metric>_<agg>
// aggregate wouldn't exist yet).
function _syncStatsChartMetricSelects() {
  const keys = _statsChartMetricKeys();
  const before = _statsChartSeries().map(s => `${s.metric}:${s.agg}:${s.axis}`).join('|');
  let changed = false;

  if (!keys.includes(statsChartY1)) {
    statsChartY1 = 'turns';
    statsChartAggY1 = 'sum';
  }
  statsChartExtra = statsChartExtra.filter(entry => keys.includes(entry.metric));

  _syncStatsChartAggControls();
  const after = _statsChartSeries().map(s => `${s.metric}:${s.agg}:${s.axis}`).join('|');
  changed = before !== after;
  return changed;
}

// Renders the repeatable "extra series" chip row list into #sc-extra. Each
// chip is an independent (metric, agg, axis) triple — the same metric can
// appear in more than one chip (e.g. P50 and P95 of Tokens In side by side).
function _renderStatsChartExtraRows() {
  const wrap = document.getElementById('sc-extra');
  if (!wrap) return;
  const keys = _statsChartMetricKeys();
  const series = statsBreakdown ? _statsChartSeries().slice(0, 1) : _statsChartSeries();
  if (statsChartEditIndex != null && !series[statsChartEditIndex]) statsChartEditIndex = null;
  const pills = series.map((entry, i) => {
    entry.agg = _normalizeStatsAgg(entry.metric, entry.agg);
    const label = _statsChartSeriesLabel(entry.metric, entry.agg);
    const axisLabel = _statsChartAxisLabel(entry.axis);
    const remove = series.length > 1
      ? `<button type="button" class="sc-series-remove" aria-label="Remove series ${i + 1}">×</button>`
      : '';
    return `<span class="sc-series-pill-wrap" data-index="${i}">
      <button type="button" class="sc-series-pill${statsChartEditIndex === i ? ' active' : ''}" aria-expanded="${statsChartEditIndex === i ? 'true' : 'false'}" aria-label="Edit series ${i + 1}">
        <span class="sc-series-pill-text">${escapeHtml(label)} · ${axisLabel}</span>
      </button>
      ${remove}
    </span>`;
  }).join('');

  const entry = statsChartEditIndex == null ? null : series[statsChartEditIndex];
  if (!entry) {
    wrap.innerHTML = pills;
    return;
  }
  entry.agg = _normalizeStatsAgg(entry.metric, entry.agg);
  const metricOptions = keys.map(key => {
    const label = (CHART_METRICS[key] || {}).label || key;
    return `<option value="${key}"${key === entry.metric ? ' selected' : ''}>${label}</option>`;
  }).join('');
  const allowedAggs = STATS_METRIC_AGGS[entry.metric] || ['sum'];
  const aggOptions = statsPeriod === 'turn'
    ? `<option value="${entry.agg}" selected>RAW</option>`
    : allowedAggs.map(key => `<option value="${key}"${key === entry.agg ? ' selected' : ''}>${STATS_AGG_LABELS[key]}</option>`).join('');
  const axisLabel = _statsChartAxisLabel(entry.axis);
  wrap.innerHTML = `${pills}<div class="sc-series-popover sc-extra-row" data-index="${statsChartEditIndex}">
    <label>
      <span>Metric</span>
      <select class="sc-extra-metric" aria-label="Series ${statsChartEditIndex + 1} metric">${metricOptions}</select>
    </label>
    <label>
      <span>Agg</span>
      <select class="sc-extra-agg" aria-label="Series ${statsChartEditIndex + 1} aggregation">${aggOptions}</select>
    </label>
    <button type="button" class="sc-extra-axis" title="Plotted on the ${entry.axis === 'y2' ? 'right' : 'left'} axis — click to switch" ${statsChartEditIndex === 0 ? 'disabled' : ''}>${axisLabel}</button>
  </div>`;
}

function _statsTableAggForMeasure(metric) {
  if (statsPeriod === 'turn') return 'sum';
  if (statsChartY1 === metric) return statsChartAggY1;
  const entry = statsChartExtra.find(s => s.metric === metric);
  return entry ? entry.agg : _defaultStatsAgg(metric);
}

function _statsTableMeasureLabel(measure) {
  const agg = _statsTableAggForMeasure(measure.key);
  if (agg === 'sum') return measure.label;
  return `${STATS_AGG_LABELS[agg] || agg.toUpperCase()} ${measure.label}`;
}

function _statsTableMeasureValue(row, measure) {
  const agg = _statsTableAggForMeasure(measure.key);
  if (!Object.prototype.hasOwnProperty.call(row, _statsChartAggField(measure.key, agg))) return measure.row(row);
  return _formatStatsMetricValue(_statsChartSeriesValue(row, measure.key, agg), measure.key);
}

function _statsSelectedMeasures() {
  return STATS_TABLE_MEASURES.filter(m => _statsMeasureSelected(m.key));
}

function _statsMeasureHeaders(extra) {
  return _statsSelectedMeasures()
    .map((m, i) => {
      var cls = '';
      if (extra && extra.colClass) cls = ` class="${extra.colClass(m, i)}"`;
      return `<th${cls}${m.title ? ` title="${m.title}"` : ''}>${_statsTableMeasureLabel(m)}</th>`;
    })
    .join('');
}

function _statsMeasureCells(row, extra) {
  return _statsSelectedMeasures()
    .map((m, i) => {
      var cls = '';
      if (extra && extra.colClass) cls = ` class="${extra.colClass(m, i)}"`;
      return `<td${cls}>${_statsTableMeasureValue(row, m)}</td>`;
    })
    .join('');
}

function _statsMeasureTotals(totals, extra) {
  return STATS_TABLE_MEASURES
    .filter(m => _statsMeasureSelected(m.key))
    .map((m, i) => {
      var cls = '';
      if (extra && extra.colClass) cls = ` class="${extra.colClass(m, i)}"`;
      return `<td${cls}>${_statsTableAggForMeasure(m.key) === 'sum' ? m.total(totals) : '—'}</td>`;
    })
    .join('');
}

function _statsTotals(rows) {
  const totals = {
    sessions: 0, turns: 0, done_turns: 0, error_turns: 0, cancelled_turns: 0,
    marked_bad: 0,
    tokens_in: 0, tokens_out: 0, cost: 0, quota: null, duration: null,
    cache_read: 0, cache_write: 0, new_input: 0,
  };
  for (const r of rows) {
    const split = _splitInputTokens(r);
    totals.sessions += r.sessions || 0;
    totals.turns += r.total_turns || 0;
    totals.done_turns += r.done_turns || 0;
    totals.error_turns += r.error_turns || 0;
    totals.cancelled_turns += r.cancelled_turns || 0;
    totals.marked_bad += r.marked_bad || 0;
    totals.tokens_in += split.total;
    totals.tokens_out += r.output_tokens || 0;
    totals.cost += r.cost_usd || 0;
    if (r.quota_delta != null) totals.quota = (totals.quota || 0) + r.quota_delta;
    if (r.duration_ms != null) totals.duration = (totals.duration || 0) + r.duration_ms;
    totals.cache_read += split.cacheRead;
    totals.cache_write += split.cacheWrite;
    totals.new_input += split.newInput;
  }
  return totals;
}

function _turnTimeLabel(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
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

function _turnRouteLabel(row) {
  const topic = row.topic || 'unknown';
  const agent = _agentLabel(row);
  return `#${topic}@${agent}${row.adhoc ? '!' : ''}`;
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
  if (statsBreakdown) {
    toggle.textContent = 'Measures';
    toggle.classList.remove('active');
    return;
  }
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

function _statsStatusLabel(values) {
  if (!values.length) return 'All Status';
  if (values.length === 1) return STATS_STATUS_LABELS[values[0]] || values[0];
  return `${values.length} Statuses`;
}

function _updateStatsFilterLabels() {
  const topicToggle = document.getElementById('sf-topic-toggle');
  const agentToggle = document.getElementById('sf-agent-toggle');
  const statusToggle = document.getElementById('sf-status-toggle');
  const adhocSelect = document.getElementById('sf-adhoc');
  const flowSelect = document.getElementById('sf-flow');
  if (topicToggle) {
    topicToggle.textContent = _statsMultiLabel(statsFilters.topics, 'All Topics', 'Topic', '#');
    topicToggle.classList.toggle('active', statsFilters.topics.length > 0);
  }
  if (agentToggle) {
    agentToggle.textContent = _statsMultiLabel(statsFilters.agents, 'All Agents', 'Agent', '@');
    agentToggle.classList.toggle('active', statsFilters.agents.length > 0);
  }
  if (statusToggle) {
    statusToggle.textContent = _statsStatusLabel(statsFilters.status);
    statusToggle.classList.toggle('active', statsFilters.status.length > 0);
  }
  if (adhocSelect) adhocSelect.classList.toggle('active', statsFilters.adhoc !== 'all');
  if (flowSelect) flowSelect.classList.toggle('active', statsFilters.flow !== 'all');
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

function _syncStatsStatusMenuSelection() {
  document.querySelectorAll('#sf-status-menu input[type="checkbox"]').forEach(input => {
    input.checked = statsFilters.status.includes(input.value);
  });
  _updateStatsFilterLabels();
}

function _resetStatsDimensionFilters() {
  statsFilters.topics = [];
  statsFilters.agents = [];
  statsFilters.adhoc = 'all';
  statsFilters.status = [];
  statsFilters.flow = 'all';
  document.getElementById('sf-adhoc').value = 'all';
  document.getElementById('sf-flow').value = 'all';
  _syncStatsTopicMenuSelection();
  _syncStatsAgentMenuSelection();
  _syncStatsStatusMenuSelection();
}

function _updateStatsBreakdownUi() {
  const active = !!statsBreakdown;
  const breakdownSel = document.getElementById('sf-breakdown');
  if (breakdownSel) breakdownSel.disabled = statsPeriod === 'turn';
  _updateStatsMeasureLabel();
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

// Anchor inputs display local wall time explicitly; submitted filters still use ISO.
function _isoToLocalInputValue(iso) {
  const d = new Date(iso);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function _localAnchorInputToIso(value) {
  const match = String(value || '').trim().match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$/);
  if (!match) return null;
  const [, y, mo, d, h, mi, s = '00'] = match;
  const parsed = new Date(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s));
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString();
}

function _anchorChipTime(iso) {
  const d = new Date(iso);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function _anchorFullDateTime(iso) {
  const d = new Date(iso);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function _updateStatsAnchorLabel() {
  const toggle = document.getElementById('sf-anchor-toggle');
  if (!toggle) return;
  if (statsFilters.anchor) {
    toggle.textContent = _anchorChipTime(statsFilters.anchor);
    toggle.title = _anchorFullDateTime(statsFilters.anchor);
    toggle.classList.add('anchored');
  } else {
    toggle.textContent = 'Now';
    toggle.title = '';
    toggle.classList.remove('anchored');
  }
}

function _statsQueryParams({ includeTz = false } = {}) {
  const params = new URLSearchParams();
  params.set('period', statsPeriod);
  if (statsBreakdown) {
    params.set('breakdown', statsBreakdown);
    params.set('breakdown_sort', _statsBreakdownColumnSort.mode);
    params.set('breakdown_sort_dir', _statsBreakdownColumnSort.dir);
  }
  if (statsFilters.days < 0) {
    params.set('days', 0);
    params.set('hours', -statsFilters.days);
  } else {
    params.set('days', statsFilters.days);
  }
  if (includeTz) params.set('tz_offset_minutes', new Date().getTimezoneOffset());
  if (statsFilters.anchor) params.set('anchor', statsFilters.anchor);
  if (statsFilters.agents.length) params.set('agent', statsFilters.agents.join(','));
  if (statsFilters.topics.length) params.set('topic', statsFilters.topics.join(','));
  if (statsFilters.adhoc !== 'all') params.set('adhoc', statsFilters.adhoc);
  if (statsFilters.status.length) params.set('status', statsFilters.status.join(','));
  if (statsFilters.flow !== 'all') params.set('flow', statsFilters.flow);
  const series = _statsChartSeries();
  params.set('chart_metrics', series.map(s => s.metric).join(','));
  params.set('chart_aggs', series.map(s => s.agg).join(','));
  return params;
}

function _statsState() {
  return {
    version: 1,
    time: { period: statsPeriod, days: statsFilters.days, anchor: statsFilters.anchor || null },
    dimensions: {
      topic: { mode: statsFilters.topics.length ? 'selected' : 'auto_top', values: [...statsFilters.topics] },
      agent: { mode: statsFilters.agents.length ? 'selected' : 'auto_top', values: [...statsFilters.agents] },
      session_type: { mode: statsFilters.adhoc === 'all' ? 'all' : 'selected', values: statsFilters.adhoc === 'all' ? [] : [statsFilters.adhoc] },
      status: { mode: statsFilters.status.length ? 'selected' : 'all', values: [...statsFilters.status] },
      flow: { mode: statsFilters.flow === 'all' ? 'all' : 'selected', values: statsFilters.flow === 'all' ? [] : [statsFilters.flow] },
    },
    breakdown: { key: statsBreakdown, sort: { ..._statsBreakdownColumnSort } },
    measure: {
      primary: _statsMeasureState(statsChartY1, statsChartAggY1),
      series: statsChartExtra.map(s => ({ metric: s.metric, agg: s.agg, axis: s.axis })),
      visible: [..._statsMeasures],
    },
  };
}

function _overallStatsState() {
  return {
    version: 1,
    time: { period: 'hourly', days: 1 },
    dimensions: {
      topic: { mode: 'auto_top', values: [] },
      agent: { mode: 'auto_top', values: [] },
      session_type: { mode: 'all', values: [] },
      status: { mode: 'all', values: [] },
      flow: { mode: 'all', values: [] },
    },
    breakdown: { key: '', sort: { mode: 'name', dir: 'asc' } },
    measure: { primary: { metric: 'turns', agg: 'sum' }, series: [], visible: [...DEFAULT_STATS_MEASURES] },
  };
}

function _deepDiveStatsState() {
  return {
    version: 1,
    time: { period: 'turn', days: -3 },
    dimensions: {
      topic: { mode: 'auto_top', values: [] },
      agent: { mode: 'auto_top', values: [] },
      session_type: { mode: 'all', values: [] },
      // Error/cancelled turns don't have a stats link and break the
      // per-turn chart, so default this view to completed turns only.
      status: { mode: 'selected', values: ['done'] },
      flow: { mode: 'all', values: [] },
    },
    breakdown: { key: '', sort: { mode: 'name', dir: 'asc' } },
    measure: {
      primary: { metric: 'tokens_in', agg: 'sum' },
      series: [{ metric: 'cache_hit_rate', agg: 'avg', axis: 'y2' }],
      visible: [...DEFAULT_STATS_MEASURES],
    },
  };
}

function _markStatsPresetDirty() {
  _renderStatsPresetControls();
}

function _applyStatsState(state) {
  statsPeriod = state?.time?.period || 'hourly';
  statsFilters.days = Number(state?.time?.days ?? 1);
  statsFilters.anchor = state?.time?.anchor || null;
  statsBreakdown = state?.breakdown?.key || '';
  _statsDurationAutoAddedForTurn = false;
  _statsBreakdownColumnSort = _normalizeStatsBreakdownSort(state?.breakdown?.sort);
  const dims = state?.dimensions || {};
  statsFilters.topics = dims.topic?.mode === 'selected' ? [...(dims.topic.values || [])] : [];
  statsFilters.agents = dims.agent?.mode === 'selected' ? [...(dims.agent.values || [])] : [];
  const sessionValues = dims.session_type?.values || [];
  statsFilters.adhoc = dims.session_type?.mode === 'all' ? 'all' : (sessionValues[0] || 'all');
  statsFilters.status = dims.status?.mode === 'selected' ? [...(dims.status.values || [])] : [];
  const flowValues = dims.flow?.values || [];
  statsFilters.flow = dims.flow?.mode === 'selected' ? (flowValues[0] || 'all') : 'all';
  const primaryMeasure = _parseStatsMeasureState(state?.measure?.primary, 'turns');
  statsChartY1 = primaryMeasure.metric || 'turns';
  statsChartAggY1 = primaryMeasure.agg;
  // Older presets saved a single "secondary" measure (always its own right
  // axis unless it matched the primary metric) rather than a series list.
  const rawSeries = state?.measure?.series
    || (state?.measure?.secondary ? [state.measure.secondary] : []);
  statsChartExtra = rawSeries
    .map(raw => {
      const parsed = _parseStatsMeasureState(raw, '');
      if (!parsed.metric) return null;
      // New-format entries carry an explicit axis; legacy "secondary" entries
      // don't, so fall back to the old rule (shared axis only if same metric).
      const axis = (raw && typeof raw === 'object' && (raw.axis === 'y1' || raw.axis === 'y2'))
        ? raw.axis
        : (parsed.metric === statsChartY1 ? 'y1' : 'y2');
      return { metric: parsed.metric, agg: parsed.agg, axis };
    })
    .filter(Boolean);
  _statsMeasures.clear();
  for (const key of state?.measure?.visible || DEFAULT_STATS_MEASURES) _statsMeasures.add(key);
  document.getElementById('sf-period').value = statsPeriod;
  _syncStatsDayOptions(statsPeriod);
  document.getElementById('sf-breakdown').value = statsBreakdown;
  document.getElementById('sf-adhoc').value = statsFilters.adhoc;
  document.getElementById('sf-flow').value = statsFilters.flow;
  _updateStatsAnchorLabel();
  // A saved preset's chart metric might not be in its saved visible measures
  // (older presets could chart something the table didn't show) — reconcile
  // to whatever's actually checked, same as any other measures change.
  _syncStatsChartMetricSelects();
  document.querySelectorAll('#sf-measures-menu input[type="checkbox"]').forEach(input => {
    input.checked = _statsMeasures.has(input.value);
  });
  _syncStatsTopicMenuSelection();
  _syncStatsAgentMenuSelection();
  _syncStatsStatusMenuSelection();
  _updateStatsMeasureLabel();
  _updateStatsBreakdownUi();
}

function _renderStatsPresetControls() {
  const select = document.getElementById('stats-preset-select');
  if (!select) return;
  const options = [
    '<option value="__overall">Overview</option>',
    '<option value="__deepdive">Deep Dive by Turns</option>',
  ];
  options.push(..._statsPresets
    .filter(p => p.name !== 'Overview' && p.name !== 'Deep Dive by Turns')
    .map(preset => {
      return `<option value="${preset.id}">${escapeHtml(preset.name)}</option>`;
    }));
  select.innerHTML = options.join('');
  // If the active preset has a reserved system name, treat it as the
  // matching hardcoded entry so the select doesn't show an empty value.
  if (_activeStatsPresetId) {
    const active = _statsPresets.find(p => p.id === _activeStatsPresetId);
    if (active && (active.name === 'Overview' || active.name === 'Deep Dive by Turns')) _activeStatsPresetId = null;
  }
  const systemView = !_activeStatsPresetId
    ? (statsPeriod === 'turn' ? '__deepdive' : '__overall') : null;
  select.value = _activeStatsPresetId ? String(_activeStatsPresetId) : systemView;
  const hasActivePreset = !!_activeStatsPresetId;
  const hasAnyDefault = _statsPresets.some(p => p.is_default);
  const deepDivePreset = _statsPresets.find(p => p.name === 'Deep Dive by Turns');
  const overviewPreset = _statsPresets.find(p => p.name === 'Overview');
  // Deep Dive is the built-in fallback default when no user preset is marked
  // default at all; Overview only shows as default when explicitly saved as one.
  const activeIsDefault = hasActivePreset
    ? _statsPresets.find(p => p.id === _activeStatsPresetId)?.is_default
    : systemView === '__deepdive' ? (!hasAnyDefault || !!deepDivePreset?.is_default)
    : systemView === '__overall' ? !!overviewPreset?.is_default
    : false;
  document.getElementById('stats-preset-default').classList.toggle('active', activeIsDefault);
  document.getElementById('stats-preset-default').disabled = false;
  document.getElementById('stats-preset-delete').disabled = !hasActivePreset;
}


function _defaultStatsPreset() {
  return _statsPresets.find(preset => preset.is_default);
}

async function _loadStatsPresets({ applyDefault = false } = {}) {
  try {
    _statsPresets = await fetch('/stats/filter-presets').then(r => r.json());
    const def = _defaultStatsPreset();
    if (applyDefault && def && !_activeStatsPresetId) {
      _activeStatsPresetId = def.id;
      _applyStatsState(def.state);
    }
    _renderStatsPresetControls();
  } catch {
    _statsPresets = [];
    _renderStatsPresetControls();
  }
}

let _presetNameResolve = null;
let _presetNameConflict = null;

function _setPresetNameModalError(text, conflict = null) {
  document.getElementById('preset-name-modal-error').textContent = text || '';
  document.getElementById('preset-name-overwrite').hidden = !conflict;
  _presetNameConflict = conflict;
}

function _openPresetNameModal(defaultName) {
  return new Promise(resolve => {
    _presetNameResolve = resolve;
    const input = document.getElementById('preset-name-input');
    input.value = defaultName || '';
    _setPresetNameModalError('');
    document.getElementById('preset-name-confirm').disabled = !input.value.trim();
    document.getElementById('preset-name-modal').classList.add('open');
    input.focus();
    input.select();
  });
}

function _closePresetNameModal(preset = null) {
  document.getElementById('preset-name-modal').classList.remove('open');
  _setPresetNameModalError('');
  const resolve = _presetNameResolve;
  _presetNameResolve = null;
  if (resolve) resolve(preset);
}

async function _submitPresetName() {
  const name = document.getElementById('preset-name-input').value.trim();
  if (!name) return;
  const reserved = name.toLowerCase();
  if (reserved === 'overview' || reserved === 'deep dive by turns') {
    _setPresetNameModalError(`"${name}" is a reserved system view.`);
    return;
  }
  const conflict = _statsPresets.find(p => p.name.toLowerCase() === name.toLowerCase());
  if (conflict) {
    _setPresetNameModalError(`A view named "${conflict.name}" already exists.`, conflict);
    return;
  }
  const res = await fetch('/stats/filter-presets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, state: _statsState() }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    _setPresetNameModalError(err.error || 'Save failed.');
    return;
  }
  _closePresetNameModal(await res.json());
}

async function _overwritePresetFromModal() {
  const conflict = _presetNameConflict;
  if (!conflict) return;
  const res = await fetch(`/stats/filter-presets/${conflict.id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state: _statsState() }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    _setPresetNameModalError(err.error || 'Save failed.', conflict);
    return;
  }
  _closePresetNameModal(await res.json());
}

async function _saveStatsPreset({ update = false, makeDefault = false } = {}) {
  if (makeDefault && !_activeStatsPresetId) {
    // Neither system view (Overview / Deep Dive) is a real preset until the
    // user marks it default — upsert a reserved-name preset row so the
    // choice survives reload. The backend's unique-default index clears any
    // other row's is_default automatically, so no manual cleanup is needed.
    const isDeepDive = statsPeriod === 'turn';
    const name = isDeepDive ? 'Deep Dive by Turns' : 'Overview';
    const buildState = isDeepDive ? _deepDiveStatsState : _overallStatsState;
    const existing = _statsPresets.find(p => p.name === name);
    if (existing?.is_default) {
      // Toggle off: clicking an already-default system view unsets it.
      await fetch(`/stats/filter-presets/${existing.id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_default: false }),
      });
    } else if (existing) {
      await fetch(`/stats/filter-presets/${existing.id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_default: true, state: buildState() }),
      });
    } else {
      const res = await fetch('/stats/filter-presets', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, state: buildState() }),
      });
      if (res.ok) {
        const p = await res.json();
        await fetch(`/stats/filter-presets/${p.id}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ is_default: true }),
        });
      }
    }
    await _loadStatsPresets();
    _renderStatsPresetControls();
    return;
  }
  if (makeDefault && _activeStatsPresetId) {
    const preset = _statsPresets.find(p => p.id === _activeStatsPresetId);
    if (preset?.is_default) {
      // Toggle off: clicking an already-default preset unsets it.
      const res = await fetch(`/stats/filter-presets/${_activeStatsPresetId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_default: false }),
      });
      if (!res.ok) return;
      await _loadStatsPresets();
      _renderStatsPresetControls();
      return;
    }
  }
  if (!update && !makeDefault) {
    const active = _statsPresets.find(preset => preset.id === _activeStatsPresetId);
    const preset = await _openPresetNameModal(active?.name || '');
    if (!preset) return;
    await _loadStatsPresets();
    _activeStatsPresetId = preset.id;
    _renderStatsPresetControls();
    return;
  }
  const body = makeDefault ? { is_default: true } : { state: _statsState() };
  const res = await fetch(`/stats/filter-presets/${_activeStatsPresetId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    return;
  }
  const preset = await res.json();
  await _loadStatsPresets();
  _activeStatsPresetId = preset.id;
  _renderStatsPresetControls();
}

function _destroyChart() {
  if (statsChartInstance) { statsChartInstance.destroy(); statsChartInstance = null; }
}

function _renderChart(rows) {
  if (!rows || !rows.length || typeof Chart === 'undefined') { _destroyChart(); return; }
  const chronological = [...rows].reverse();
  const labels = chronological.map(r => statsPeriod === 'turn' ? fmtTime(r.period) : r.period);
  const pointRadius = labels.length > 60 ? 1 : labels.length > 20 ? 2 : 4;
  const series = _statsChartSeries();
  const usedColors = new Set();
  const datasets = series.map((s, i) => {
    const m = CHART_METRICS[s.metric] || CHART_METRICS.turns;
    // Some metrics share a base color (e.g. Tokens In / New Input are both
    // green) — fall back to the series palette on collision so two charted
    // measures are never visually indistinguishable.
    let color = m.color;
    if (usedColors.has(color)) color = STATS_SERIES_COLORS.find(c => !usedColors.has(c)) || color;
    usedColors.add(color);
    return {
      label: _statsChartSeriesLabel(s.metric, s.agg),
      data: chronological.map(r => _statsChartSeriesValue(r, s.metric, s.agg)),
      borderColor: color, backgroundColor: i === 0 ? m.fill : 'transparent',
      yAxisID: s.axis, tension: 0.3, fill: i === 0,
      pointRadius, pointHoverRadius: 5,
    };
  });
  const scales = {
    x: { ticks: { color: '#555', font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, grid: { color: '#1a1a24' } },
    y1: { type: 'linear', position: 'left', ticks: { color: '#555', font: { size: 10 }, callback: fmtAxisNum }, grid: { color: '#1a1a24' } },
  };
  if (series.some(s => s.axis === 'y2')) {
    scales.y2 = { type: 'linear', position: 'right', ticks: { color: '#555', font: { size: 10 }, callback: fmtAxisNum }, grid: { display: false } };
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

function quotaStatusProviderKey(ref) {
  return splitAgentRef(ref).provider || ref;
}

function quotaSnapshotForProvider(provider) {
  return quotaSnapshots[provider] ||
    Object.values(quotaSnapshots).find(snapshot => quotaStatusProviderKey(snapshot.backend) === provider) ||
    null;
}

function shouldShowQuotaStatusProvider(provider) {
  const info = _providerMetadata[provider] || _backendMetadata[provider];
  const snapshot = quotaSnapshotForProvider(provider);
  const snapshotGaugeType = snapshot ? gaugeTypeFor(snapshot.backend) : 'none';
  const gaugeType = info?.gauge?.type || 'none';
  if (gaugeType === 'none' && snapshotGaugeType === 'none') return false;
  return info?.gauge_authed !== false;
}

function renderQuotaStatus() {
  // Rows come from the configured provider catalog (providers: in squid.yaml) —
  // quota is a provider attribute, not a harness one (ADR-0028), so this needs
  // no hardcoded backend list. quotaSnapshots is unioned in defensively for
  // gauges fetched before /health populated _providerMetadata.
  const backends = [...new Set([
    ...Object.keys(_providerMetadata),
    ...Object.keys(quotaSnapshots).map(quotaStatusProviderKey),
  ])].filter(shouldShowQuotaStatusProvider);
  const rows = backends
    .map(provider => {
    const snapshot = quotaSnapshotForProvider(provider);
    const q = snapshot || {
      backend: provider,
      status: quotaConfigFor(provider) ? 'unknown' : 'unsupported',
    };
    const accent = agentThemeColor(provider);
    let value = 'n/a';
    let detail = 'no quota integration';
    if (q.status === 'loaded') {
      value = q.displayText || (q.pct == null ? '—' : `${q.pct}%`);
      const reset = quotaTimeText(q.resetAt);
      detail = reset ? `resets in ${reset}` : (isBalanceGauge(q.backend) ? 'no reset' : (q.title || 'no reset'));
    } else if (q.status === 'error') {
      value = 'error';
      detail = q.text || 'unavailable';
    } else if (q.status === 'unknown') {
      value = '...';
      detail = 'loading';
    }
    return `<div class="quota-status-row">
      <span class="quota-status-name"><span class="quota-status-dot" style="background:${accent}"></span>${escapeHtml(backendDisplayName(provider))}</span>
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
          <td><span class="proc-dot"></span>#${r.topic || '—'}@${r.agent || '—'}</td>
          <td class="proc-queue-preview">${r.prompt_preview || '—'}</td>
          <td>${r.duration_s}s</td>
          <td>${procStopButton(r)}</td>
        </tr>`).join('');
      body += `<div class="proc-section-label">Running</div>
        <table><thead><tr><th>Route</th><th>Prompt</th><th>Time</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`;
    }
    if (queued.length) {
      const rows = queued.map(r => `
        <tr>
          <td>#${r.topic || '—'}@${r.agent || '—'}</td>
          <td class="proc-queue-preview">${r.prompt_preview || '—'}</td>
          <td><button class="proc-deq-btn" data-topic="${r.topic || ''}" data-pos="${r.position}">✕</button></td>
        </tr>`).join('');
      body += `<div class="proc-section-label">Queued</div>
        <table><thead><tr><th>Route</th><th>Prompt</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`;
    }
    if (idle.length) {
      const rows = idle.map(r => `
        <tr>
          <td><span class="proc-dot proc-dot-idle"></span>#${r.topic || '—'}@${r.agent || '—'}</td>
          <td class="proc-queue-preview">${r.prompt_preview || 'warm session'}</td>
          <td>${formatIdleDuration(r.state_duration_s ?? r.duration_s)}</td>
          <td>${procStopButton(r)}</td>
        </tr>`).join('');
      body += `<div class="proc-section-label">Idle Live Sessions <span class="help-icon" data-tooltip="Idle sessions stay warm between prompts. Currently, only Claude Code supports the interactive-cli protocol that keeps them alive."><svg width="12" height="12" viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="7.5" cy="7.5" r="6.5"/><path d="M5.8 5.8a1.7 1.7 0 0 1 3.4 0c0 1.1-1.7 1.7-1.7 2.9"/><circle cx="7.5" cy="11" r="0.6" fill="currentColor" stroke="none"/></svg></span></div>
        <table><thead><tr><th>Route</th><th>Last prompt</th><th>Idle</th><th></th></tr></thead>
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
    for (const backend of Object.keys(_providerMetadata)) {
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

  await fetch('/stats/filters').then(r => r.json()).then(data => {
    _renderStatsMultiMenu(document.getElementById('sf-agent-menu'), data.agents, statsFilters.agents, '@');
    _renderStatsMultiMenu(document.getElementById('sf-topic-menu'), data.topics, statsFilters.topics, '#');
    _updateStatsFilterLabels();
  }).catch(() => {});

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
    statsContent.innerHTML = '<div class="empty">No results for the current filters. Try widening the time range or clearing agent/topic/route filters if you have usage history.</div>';
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

  if (statsPeriod === 'turn') {
    renderTurnStats(rows);
    _renderChart(rows);
  } else if (statsBreakdown) {
    renderAgentBreakdownStats(rows);
    _renderBreakdownChart(rows);
  } else {
    renderTimeStats(rows);
    _renderChart(rows);
  }
  _applyStatsHighlight();
}

function renderTurnStats(rows) {
  const totals = _statsTotals(rows);
  const bodyRows = _statsPageSlice(rows).map(r => {
    return `<tr data-msg-ids="${escapeHtml(_statsTurnMessageIds(r).join(','))}">
      <td class="stats-col-compact stats-time-col">${_turnTimeLabel(r.period)}</td>
      <td class="stats-col-compact stats-route-col">${escapeHtml(_turnRouteLabel(r))}</td>
      ${_statsMeasureCells(r)}
    </tr>`;
  }).join('');

  // The mobile layout squeezes measure columns to fit the viewport width,
  // which works fine for a small handful but crushes columns unreadably
  // once several more measures are selected — past that count, let the table
  // grow past the viewport and scroll instead of shrinking every column.
  // This must NOT be compared against DEFAULT_STATS_MEASURES.length: that
  // list is the rich Deep Dive default (11 measures) and is already past
  // the point columns stay readable when squeezed to 100% width.
  const visibleMeasureCount = STATS_TABLE_MEASURES.filter(m => _statsMeasureSelected(m.key)).length;
  const wideClass = visibleMeasureCount > _STATS_TURN_TABLE_COMPACT_LIMIT ? ' stats-turn-table-wide' : '';

  _setStatsTable(`<table class="stats-turn-table${wideClass}">
    <thead><tr>
      <th class="stats-col-compact stats-time-col">Time</th>
      <th class="stats-col-compact stats-route-col">Route</th>
      ${_statsMeasureHeaders()}
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td class="stats-col-compact stats-time-col">Total</td>
      <td class="stats-col-compact stats-route-col"></td>
      ${_statsMeasureTotals(totals)}
    </tr></tfoot>
  </table>`);
  _statsAppendPager(rows.length);
}

function renderTimeStats(rows) {
  // Sticky column classes: time column is always frozen; turns column is
  // frozen when it is the first selected measure.
  const firstMeasure = _statsSelectedMeasures()[0];
  const freezeTurns = firstMeasure && firstMeasure.key === 'turns';
  const extra = {
    colClass: function (m, i) {
      if (i === 0 && freezeTurns) return 'stats-sticky-left';
      return '';
    },
  };

  const totals = _statsTotals(rows);
  const bodyRows = _statsPageSlice(rows).map(r => {
    return `<tr data-msg-ids="${escapeHtml(_statsTurnMessageIds(r).join(','))}">
      <td class="stats-time-col">${_statsPeriodLabel(r.period)}</td>
      ${_statsMeasureCells(r, extra)}
    </tr>`;
  }).join('');

  _setStatsTable(`<table class="stats-time-table">
    <thead><tr>
      <th class="stats-time-col">${statsPeriod === 'hourly' ? 'Hour' : statsPeriod === 'weekly' ? 'Week' : 'Date'}</th>
      ${_statsMeasureHeaders(extra)}
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td class="stats-time-col">Total</td>${_statsMeasureTotals(totals, extra)}
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
  const miscHeader = hasMisc ? `<th class="stats-misc-col" title="${escapeHtml(miscTitle)}">Misc</th>` : '';
  const bodyRows = _statsPageSlice(pivot.periodRows).map(row => {
    const cells = pivot.selected
      .map(key => `<td class="stats-series-col">${_formatStatsMetricValue(row.values[key] || 0, metric)}</td>`)
      .join('');
    return `<tr>
      <td class="stats-sticky-left">${_statsPeriodLabel(row.period)}</td>
      ${cells}
      ${hasMisc ? `<td class="stats-misc-col">${_formatStatsMetricValue(row.misc || 0, metric)}</td>` : ''}
      <td class="stats-sticky-right stats-total-col">${pivot.canSumTotals ? _formatStatsMetricValue(row.total || 0, metric) : totalText}</td>
    </tr>`;
  }).join('');
  const totalCells = pivot.selected
    .map(key => `<td class="stats-series-col">${pivot.canSumTotals ? _formatStatsMetricValue(totalExplicit[key] || 0, metric) : totalText}</td>`)
    .join('');

  _setStatsTable(`<table class="stats-breakdown-table${hasMisc ? ' has-misc' : ''}">
    <thead><tr>
      <th class="stats-sticky-left">${_statsBreakdownAxisLabel(statsPeriod === 'hourly' ? 'Hour' : statsPeriod === 'weekly' ? 'Week' : 'Date', 'name')}</th>
      ${headers}
      ${miscHeader}
      <th class="stats-sticky-right stats-total-col">Total</th>
    </tr></thead>
    <tbody>${bodyRows}</tbody>
    <tfoot><tr>
      <td class="stats-sticky-left">${_statsBreakdownAxisLabel('Total', 'total')}</td>
      ${totalCells}
      ${hasMisc ? `<td class="stats-misc-col">${_formatStatsMetricValue(totalMisc, metric)}</td>` : ''}
      <td class="stats-sticky-right stats-total-col">${pivot.canSumTotals ? _formatStatsMetricValue(grandTotal, metric) : totalText}</td>
    </tr></tfoot>
  </table>`);
  _bindStatsBreakdownSort();
  _statsAppendPager(pivot.periodRows.length);
}


function initStats() {
  statsContent.addEventListener('click', e => {
    const btn = e.target.closest('.stats-turn-link[data-turn-ids]');
    if (!btn || !statsContent.contains(btn)) return;
    e.stopPropagation();
    const ids = String(btn.dataset.turnIds || '')
      .split(',')
      .map(id => parseInt(id, 10))
      .filter(Number.isFinite);
    showStatsTurnsPopup(btn, ids);
  });

  document.getElementById('sf-period').addEventListener('change', e => {
    const prevPeriod = statsPeriod;
    statsPeriod = e.target.value;
    _syncStatsDayOptions(statsPeriod);
    let measuresChanged = false;
    if (statsPeriod === 'turn' && prevPeriod !== 'turn') {
      if (statsBreakdown) {
        statsBreakdown = '';
        document.getElementById('sf-breakdown').value = '';
      }
      // Every row is exactly one session in this view, so start sessions off.
      // Keep turns on because that column links to the underlying response.
      const removedSessions = _statsMeasures.delete('sessions');
      const addedDuration = !_statsMeasures.has('duration');
      if (addedDuration) {
        _statsMeasures.add('duration');
        _statsDurationAutoAddedForTurn = true;
      }
      measuresChanged = removedSessions || addedDuration;
      if (statsChartY1 === 'turns') {
        statsChartY1 = _firstSelectedNonTurnMeasure();
        statsChartAggY1 = _normalizeStatsAgg(statsChartY1, statsChartAggY1);
      }
    } else if (statsPeriod !== 'turn' && prevPeriod === 'turn') {
      _statsMeasures.add('sessions');
      _statsMeasures.add('turns');
      if (_statsDurationAutoAddedForTurn) _statsMeasures.delete('duration');
      _statsDurationAutoAddedForTurn = false;
      measuresChanged = true;
    }
    if (measuresChanged) {
      document.querySelectorAll('#sf-measures-menu input[type="checkbox"]').forEach(input => {
        input.checked = _statsMeasures.has(input.value);
      });
      _updateStatsMeasureLabel();
    }
    _syncStatsChartMetricSelects();
    _updateStatsBreakdownUi();
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
    _updateStatsFilterLabels();
    _markStatsPresetDirty();
    loadStats();
  });

  document.getElementById('sf-flow').addEventListener('change', e => {
    statsFilters.flow = e.target.value;
    _updateStatsFilterLabels();
    _markStatsPresetDirty();
    loadStats();
  });

  const anchorWrap = document.getElementById('sf-anchor-filter');
  const anchorToggle = document.getElementById('sf-anchor-toggle');
  const anchorMenu = document.getElementById('sf-anchor-menu');
  const anchorInput = document.getElementById('sf-anchor-input');
  anchorToggle.addEventListener('click', e => {
    e.stopPropagation();
    const open = anchorMenu.hidden;
    if (open) anchorInput.value = _isoToLocalInputValue(statsFilters.anchor || new Date().toISOString());
    anchorMenu.hidden = !open;
    anchorToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  document.getElementById('sf-anchor-apply').addEventListener('click', e => {
    e.stopPropagation();
    if (!anchorInput.value) return;
    const parsed = _localAnchorInputToIso(anchorInput.value);
    if (!parsed) return;
    statsFilters.anchor = parsed;
    anchorMenu.hidden = true;
    anchorToggle.setAttribute('aria-expanded', 'false');
    _updateStatsAnchorLabel();
    _markStatsPresetDirty();
    loadStats();
  });
  document.getElementById('sf-anchor-clear').addEventListener('click', e => {
    e.stopPropagation();
    statsFilters.anchor = null;
    anchorMenu.hidden = true;
    anchorToggle.setAttribute('aria-expanded', 'false');
    _updateStatsAnchorLabel();
    _markStatsPresetDirty();
    loadStats();
  });

  const filterMenus = [
    { wrap: document.getElementById('sf-topic-filter'), toggle: document.getElementById('sf-topic-toggle'), menu: document.getElementById('sf-topic-menu'), key: 'topics' },
    { wrap: document.getElementById('sf-agent-filter'), toggle: document.getElementById('sf-agent-toggle'), menu: document.getElementById('sf-agent-menu'), key: 'agents' },
    { wrap: document.getElementById('sf-status-filter'), toggle: document.getElementById('sf-status-toggle'), menu: document.getElementById('sf-status-menu'), key: 'status' },
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
      // Breakdown series are dimension values (topic/agent), not metrics —
      // only the primary (Y1) metric applies while it's active.
      statsChartExtra = [];
      _renderStatsChartExtraRows();
    } else {
      _resetStatsDimensionFilters();
    }
    _syncStatsChartMetricSelects();
    _updateStatsMeasureLabel();
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
      if (input.value === 'duration') _statsDurationAutoAddedForTurn = false;
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
    if (!anchorWrap.contains(e.target)) {
      anchorMenu.hidden = true;
      anchorToggle.setAttribute('aria-expanded', 'false');
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
  _updateStatsAnchorLabel();
  _syncStatsChartAggControls();

  const extraWrap = document.getElementById('sc-extra');
  extraWrap.addEventListener('change', e => {
    const row = e.target.closest('.sc-series-popover');
    if (!row) return;
    const i = Number(row.dataset.index);
    const entry = _statsChartEntry(i);
    if (!entry) return;
    if (e.target.classList.contains('sc-extra-metric')) {
      const prevMetric = entry.metric;
      const prevAgg = entry.agg;
      entry.metric = e.target.value;
      entry.agg = prevAgg === _defaultStatsAgg(prevMetric)
        ? _defaultStatsAgg(entry.metric)
        : _normalizeStatsAgg(entry.metric, prevAgg);
      // Same-metric chips (e.g. P50/P95 of one measure) almost always belong
      // on the same axis — align to an existing chip with that metric first.
      const match = _statsChartSeries().find((s, j) => j !== i && s.metric === entry.metric);
      if (match) entry.axis = match.axis;
    } else if (e.target.classList.contains('sc-extra-agg')) {
      entry.agg = e.target.value;
    } else {
      return;
    }
    _updateStatsChartEntry(i, entry);
    _renderStatsChartExtraRows();
    _markStatsPresetDirty();
    loadStats();
  });
  extraWrap.addEventListener('click', e => {
    e.stopPropagation();
    const remove = e.target.closest('.sc-series-remove');
    if (remove) {
      const pill = remove.closest('.sc-series-pill-wrap');
      const i = Number(pill?.dataset.index);
      _removeStatsChartEntry(i);
      _renderStatsChartExtraRows();
      _markStatsPresetDirty();
      _rerenderStats();
      return;
    }
    const pill = e.target.closest('.sc-series-pill');
    if (pill) {
      const i = Number(pill.closest('.sc-series-pill-wrap')?.dataset.index);
      statsChartEditIndex = statsChartEditIndex === i ? null : i;
      _renderStatsChartExtraRows();
      return;
    }
    const popover = e.target.closest('.sc-series-popover');
    if (!popover) return;
    const i = Number(popover.dataset.index);
    const entry = _statsChartEntry(i);
    if (!entry) return;
    if (e.target.classList.contains('sc-extra-axis')) {
      if (i === 0) return;
      entry.axis = entry.axis === 'y2' ? 'y1' : 'y2';
      _updateStatsChartEntry(i, entry);
      _renderStatsChartExtraRows();
      _markStatsPresetDirty();
      _rerenderStats();
    }
  });

  document.getElementById('sc-add-series').addEventListener('click', () => {
    const keys = _statsChartMetricKeys();
    const used = new Set(_statsChartSeries().map(s => s.metric));
    const metric = keys.find(key => !used.has(key)) || keys[0] || 'turns';
    const match = _statsChartSeries().find(s => s.metric === metric);
    statsChartExtra.push({ metric, agg: _defaultStatsAgg(metric), axis: match ? match.axis : 'y1' });
    statsChartEditIndex = _statsChartSeries().length - 1;
    _renderStatsChartExtraRows();
    _markStatsPresetDirty();
    loadStats();
  });
  document.addEventListener('click', e => {
    if (statsChartEditIndex == null) return;
    if (extraWrap.contains(e.target) || document.getElementById('sc-add-series')?.contains(e.target)) return;
    statsChartEditIndex = null;
    _renderStatsChartExtraRows();
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape' || statsChartEditIndex == null) return;
    statsChartEditIndex = null;
    _renderStatsChartExtraRows();
  });

  document.getElementById('stats-preset-select')?.addEventListener('change', e => {
    if (e.target.value === '__overall') {
      _activeStatsPresetId = null;
      _applyStatsState(_overallStatsState());
      _renderStatsPresetControls();
      loadStats();
      return;
    }
    if (e.target.value === '__deepdive') {
      _activeStatsPresetId = null;
      _applyStatsState(_deepDiveStatsState());
      _renderStatsPresetControls();
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
    loadStats();
  });
  document.getElementById('stats-preset-save')?.addEventListener('click', () => _saveStatsPreset());

  document.getElementById('stats-preset-default')?.addEventListener('click', () => _saveStatsPreset({ makeDefault: true }));
  document.getElementById('stats-preset-reset')?.addEventListener('click', () => {
    // "Reset to default view" means whatever is actually marked default
    // (custom preset, Overview, or Deep Dive) — not hardcoded to Deep Dive.
    // Falls back to Deep Dive only when nothing is marked default at all.
    const def = _defaultStatsPreset();
    _activeStatsPresetId = def ? def.id : null;
    _applyStatsState(def ? def.state : _deepDiveStatsState());
    _renderStatsPresetControls();
    loadStats();
  });
  document.getElementById('stats-preset-delete')?.addEventListener('click', async () => {
    if (!_activeStatsPresetId) return;
    await fetch(`/stats/filter-presets/${_activeStatsPresetId}`, { method: 'DELETE' });
    _activeStatsPresetId = null;
    await _loadStatsPresets();
    const def = _defaultStatsPreset();
    _activeStatsPresetId = def ? def.id : null;
    _applyStatsState(def ? def.state : _deepDiveStatsState());
    _renderStatsPresetControls();
    loadStats();
  });

  // Static HTML checkbox markup is just an authoring convenience — the JS
  // defaults in _statsMeasures are the source of truth, and _applyStatsState
  // only runs here if a saved default preset exists. Sync unconditionally so
  // a fresh install (no presets) can't drift from what's actually checked.
  document.querySelectorAll('#sf-measures-menu input[type="checkbox"]').forEach(input => {
    input.checked = _statsMeasures.has(input.value);
  });
  const periodSel = document.getElementById('sf-period');
  if (periodSel) periodSel.value = statsPeriod;
  _syncStatsDayOptions(statsPeriod);
  _updateStatsMeasureLabel();
  _syncStatsChartMetricSelects();
  // Populate the preset dropdown immediately so it's never empty.
  _renderStatsPresetControls();
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
    const sessionLaneTime = (lane.last_session_at || lane.last_at) ? `<span class="topic-badge time">${escapeHtml(fmtTime(lane.last_session_at || lane.last_at))}</span>` : '';
    const adhocLaneTime = (lane.last_adhoc_at || lane.last_at) ? `<span class="topic-badge time">${escapeHtml(fmtTime(lane.last_adhoc_at || lane.last_at))}</span>` : '';
    // Default topic: skip session lane if no session history — adhoc is the normal mode there
    if (isDefaultTopic && !lane.last_prompt) {
      if (lane.last_adhoc_prompt) {
        html += `
        <div class="topic-agent-row adhoc" data-topic="${escapeHtml(topic.name)}" data-agent="${escapeHtml(lane.agent)}" data-adhoc="1">
          <div class="topic-agent-main">
            <span class="topic-agent-label">${_topicAgentDisplay(lane.agent, backend)}!${lane.adhoc_turns > 0 ? ` <span class="topic-turn-count">${lane.adhoc_turns}</span>` : ''}</span>
          </div>
          <div class="topic-prompt">${escapeHtml(truncate(lane.last_adhoc_prompt, 120))}</div>
          <div class="topic-meta">
            <span class="topic-badge">adhoc</span>
            ${adhocLaneTime}
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
          <span class="topic-agent-label">${_topicAgentDisplay(lane.agent, backend)}${turnCount}</span>
        </div>
        <div class="topic-prompt">${sessionPrompt}</div>
        <div class="topic-meta">
          ${sessionLaneTime}
          <button class="topic-btn" data-topic-open="${escapeHtml(topic.name)}" data-agent-open="${escapeHtml(lane.agent)}" data-adhoc-open="0" type="button">Open</button>
          ${lane.last_prompt ? `<button class="topic-btn danger" data-agent-del-topic="${escapeHtml(topic.name)}" data-agent-del-agent="${escapeHtml(lane.agent)}" data-agent-del-adhoc="0" type="button">Delete</button>` : ''}
        </div>
      </div>`;
    if (lane.last_adhoc_prompt) {
      // Adhoc lane on default topic: no Delete — use topic-level Clear instead
      html += `
        <div class="topic-agent-row adhoc" data-topic="${escapeHtml(topic.name)}" data-agent="${escapeHtml(lane.agent)}" data-adhoc="1">
          <div class="topic-agent-main">
            <span class="topic-agent-label">${_topicAgentDisplay(lane.agent, backend)}!${lane.adhoc_turns > 0 ? ` <span class="topic-turn-count">${lane.adhoc_turns}</span>` : ''}</span>
          </div>
          <div class="topic-prompt">${escapeHtml(truncate(lane.last_adhoc_prompt, 120))}</div>
          <div class="topic-meta">
            <span class="topic-badge">adhoc</span>
            ${adhocLaneTime}
            <button class="topic-btn" data-topic-open="${escapeHtml(topic.name)}" data-agent-open="${escapeHtml(lane.agent)}" data-adhoc-open="1" type="button">Open</button>
            ${!isDefaultTopic ? `<button class="topic-btn danger" data-agent-del-topic="${escapeHtml(topic.name)}" data-agent-del-agent="${escapeHtml(lane.agent)}" data-agent-del-adhoc="1" type="button">Delete</button>` : ''}
          </div>
        </div>`;
    }
  }
  return html;
}

function _topicRootName(name) {
  return String(name || 'default').split('.', 1)[0] || 'default';
}

function _topicCompare(a, b) {
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
}

function _nestedTopicRows(sortedTopics, allTopics) {
  const allByName = new Map((allTopics || []).map(topic => [topic.name, topic]));
  const listed = new Set(sortedTopics.map(topic => topic.name));
  const roots = [];
  const childrenByRoot = new Map();
  for (const topic of sortedTopics) {
    const root = _topicRootName(topic.name);
    if (root !== topic.name && allByName.has(root)) {
      if (!childrenByRoot.has(root)) childrenByRoot.set(root, []);
      childrenByRoot.get(root).push(topic);
    } else {
      roots.push(topic);
    }
  }
  for (const [root, children] of childrenByRoot) {
    if (!listed.has(root)) roots.push(allByName.get(root));
  }
  roots.sort(_topicCompare);
  const rows = [];
  for (const root of roots) {
    rows.push({ topic: root, subtopic: false });
    const children = (childrenByRoot.get(root.name) || []).sort(_topicCompare);
    children.forEach(topic => rows.push({ topic, subtopic: true }));
  }
  return rows;
}

function _renderTopicRows(topic, opts = {}) {
  const subtopic = !!opts.subtopic;
  const expanded = _topicsExpanded.has(topic.name);
  const prompt = topic.last_prompt ? escapeHtml(truncate(topic.last_prompt, 120)) : '<span class="col-default">No prompt yet</span>';
  const memoryLabel = topic.memory?.exists ? 'Memory' : 'Add memory';
  const hideLabel = topic.name !== 'default' ? (topic.hidden ? 'Show' : 'Hide') : '';
  return `
    <div class="topic-row${topic.hidden ? ' hidden' : ''}${expanded ? ' expanded' : ''}${subtopic ? ' subtopic' : ''}" data-topic="${escapeHtml(topic.name)}">
      <div class="topic-main">
        <span class="topic-caret">${expanded ? '▾' : '▸'}</span>
        <span class="topic-identity"><span class="topic-name">#${escapeHtml(topic.name)}</span>${topic.total_turns > 0 ? `<span class="topic-turn-count">${topic.total_turns}</span>` : ''}</span>
      </div>
      <div class="topic-prompt">${prompt}</div>
      <div class="topic-meta">
        ${_topicStatusBadges(topic)}
        <button class="topic-btn" data-topic-open="${escapeHtml(topic.name)}" type="button">Open</button>
        ${subtopic ? '' : `<button class="topic-btn" data-topic-memory="${escapeHtml(topic.name)}" type="button">${memoryLabel}</button>`}
        ${topic.name !== 'default' ? `<button class="topic-btn" data-topic-hide="${escapeHtml(topic.name)}" data-hidden="${topic.hidden ? '1' : '0'}" type="button">${hideLabel}</button>` : ''}
        <button class="topic-btn danger" data-topic-delete="${escapeHtml(topic.name)}" type="button">${topic.name === 'default' ? 'Clear' : 'Delete'}</button>
      </div>
    </div>
    <div class="topic-agents${subtopic ? ' subtopic' : ''}"${expanded ? '' : ' hidden'}>${_renderTopicAgents(topic)}</div>`;
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

  const sorted = [...filtered].sort(_topicCompare);
  const rows = _nestedTopicRows(sorted, topics);

  listEl.innerHTML = rows.map(row => _renderTopicRows(row.topic, { subtopic: row.subtopic })).join('');
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
      navigateViewFromHistoryAnchor('topics', 'chat');
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

// ── runtime catalogs ──────────────────────────────────────────────────────────

const GAUGE_CATALOG = Object.freeze({
  claude: 'click gauge in header -> Detect',
  codex: 'uses codex CLI auth',
  cursor: 'automatic via cursor-agent',
  deepseek: 'uses this provider API key',
  kimi: 'uses this provider API key',
  none: 'no gauge configured',
});

function ensureRuntimeMetadata(harness, provider = null) {
  if (!harness) return;
  const ref = runtimeRef(harness, provider);
  if (_backendMetadata[ref]) return;
  const hInfo = _harnessMetadata[harness] || {};
  const pInfo = provider ? _providerMetadata[provider] : null;
  _backendMetadata[ref] = {
    label: provider ? `${hInfo.label || harness} / ${pInfo?.label || provider}` : (hInfo.label || harness),
    color: pInfo?.color,
    harness,
    provider,
    protocol: hInfo.protocol || 'oneshot-cli',
    gauge: pInfo?.gauge || { type: 'none' },
  };
}

function providerOptionsForHarness(harness, selectedProvider = null) {
  const hInfo = _harnessMetadata[harness] || {};
  const ids = [...new Set([
    ...(hInfo.compatible_providers || []),
    hInfo.default_provider,
    selectedProvider,
  ].filter(Boolean))];
  return ids.sort((a, b) => {
    const aLabel = _providerMetadata[a]?.label || a;
    const bLabel = _providerMetadata[b]?.label || b;
    return aLabel.localeCompare(bLabel);
  });
}

function setProviderOptions(harness, selectedProvider = null, selectId = 'af-provider') {
  const providerSelect = document.getElementById(selectId);
  if (!providerSelect) return '';
  const hInfo = _harnessMetadata[harness] || {};
  const providerIds = providerOptionsForHarness(harness, selectedProvider);
  providerSelect.innerHTML = providerIds
    .map(id => `<option value="${escapeHtml(id)}">${escapeHtml(_providerMetadata[id]?.label || id)}</option>`)
    .join('');
  const value = selectedProvider || hInfo.default_provider || providerIds[0] || '';
  if (value && providerIds.includes(value)) providerSelect.value = value;
  return providerSelect.value || '';
}

function syncAgentModelControls(harness, provider, selectPrefix = 'af') {
  const modelInput = document.getElementById(`${selectPrefix}-model`);
  const backend = runtimeRef(harness, provider || null);
  if (modelInput) modelInput.placeholder = backendModelHint(backend);
  populateModelPicker(`${selectPrefix}-model-picker`, `${selectPrefix}-model`, backend);
}

function selectedRuntime(selectPrefix = 'af') {
  const harness = document.getElementById(`${selectPrefix}-harness`)?.value || '';
  const provider = document.getElementById(`${selectPrefix}-provider`)?.value || null;
  ensureRuntimeMetadata(harness, provider);
  return { harness, provider };
}

function refreshRuntimeMetadata(health) {
  _backendMetadata = {};
  _providerMetadata = health?.providers || {};
  _harnessMetadata = {};
  for (const h of (health?.harnesses || [])) {
    _harnessMetadata[h.id] = h;
  }

  const harnessSelect = document.getElementById('af-harness');
  if (harnessSelect) {
    const previous = harnessSelect.value;
    const harnessIds = Object.keys(_harnessMetadata).sort((a, b) =>
      (_harnessMetadata[a].label || a).localeCompare(_harnessMetadata[b].label || b)
    );
    harnessSelect.innerHTML = harnessIds
      .map(id => `<option value="${escapeHtml(id)}">${escapeHtml(_harnessMetadata[id].label || id)}</option>`).join('');
    if (_harnessMetadata[previous]) harnessSelect.value = previous;
    const provider = setProviderOptions(harnessSelect.value);
    syncAgentModelControls(harnessSelect.value, provider);
  }
  refreshAgentSlugColors();
}

function renderHarnessesCatalog(health) {
  const el = document.getElementById('harnesses-catalog');
  if (!el) return;
  // Keep an inline operation terminal and its retained output alive across
  // tab changes/health refreshes. Closing it triggers the deferred render.
  if (el.contains(agentsAuthPanel)) return;

  const harnessIds = Object.keys(_harnessMetadata).sort((a, b) =>
    (_harnessMetadata[a].label || a).localeCompare(_harnessMetadata[b].label || b)
  );
  if (!harnessIds.length) {
    el.innerHTML = '<div class="empty">No harnesses reported.</div>';
    return;
  }

  el.innerHTML = harnessIds.map(id => {
    const info = _harnessMetadata[id] || {};
    const available = !!info.installed;
    const label = info.label || id;
    const installCmd = info.install_cmd || '';
    const providers = (info.compatible_providers || [])
      .map(providerId => _providerMetadata[providerId]?.label || providerId)
      .sort((a, b) => a.localeCompare(b));
    const providerHint = providers.length ? `providers: ${providers.join(', ')}` : 'providers: none reported';

    let codingHtml;
    if (available) {
      codingHtml = `<span class="bcat-status-ok">✓ installed</span>
        <span class="bcat-hint">${escapeHtml(providerHint)}</span>`;
    } else {
      codingHtml = `<span class="bcat-status-miss">✗ CLI not found</span>` +
        (installCmd ? `<div class="bcat-install">
          <code class="bcat-cmd">${escapeHtml(installCmd)}</code>
          <button class="bcat-copy" data-cmd="${escapeHtml(installCmd)}">copy</button>
          <button class="bcat-install-btn" data-target="${escapeHtml(id)}">install</button>
        </div>` : '');
    }

    return `<div class="bcat-row">
      <div class="bcat-name">${escapeHtml(label)}</div>
      <div class="bcat-coding">${codingHtml}<span class="bcat-hint">protocol: ${escapeHtml(info.protocol || 'oneshot-cli')}</span></div>
      <div class="bcat-gauge"><span class="bcat-hint">${escapeHtml(info.default_provider || '')}</span></div>
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
  el.querySelectorAll('.bcat-install-btn').forEach(btn => {
    btn.addEventListener('click', () => openAuthPanel(btn.dataset.target, null, {
      mode: 'install', anchor: btn.closest('.bcat-row'),
    }));
  });
}

function renderProvidersCatalog() {
  const el = document.getElementById('providers-catalog');
  if (!el) return;
  if (el.contains(agentsAuthPanel)) return;

  const providerIds = Object.keys(_providerMetadata).sort((a, b) => {
    if (a === 'ollama') return -1;
    if (b === 'ollama') return 1;
    return (_providerMetadata[a].label || a).localeCompare(_providerMetadata[b].label || b);
  });
  if (!providerIds.length) {
    el.innerHTML = '<div class="empty">No providers configured.</div>';
    return;
  }

  el.innerHTML = providerIds.map(id => {
    const info = _providerMetadata[id] || {};
    const gauge = info.gauge || { type: 'none' };
    const missingSecrets = info.missing_secrets || [];
    const hasMissingSecrets = missingSecrets.length > 0;
    const label = info.label || id;
    const color = info.color || '#888888';
    const authText = info.auth_type === 'api_key'
      ? (hasMissingSecrets ? `missing: ${missingSecrets.join(', ')}` : 'API key configured')
      : info.auth_type === 'none'
      ? 'no auth'
      : 'subscription auth';
    const statusClass = hasMissingSecrets ? 'bcat-status-miss' : 'bcat-status-ok';
    const statusMark = hasMissingSecrets ? '✗' : '✓';
    const gaugeText = gauge.type === 'static'
      ? (gauge.text || 'static')
      : (GAUGE_CATALOG[gauge.type] || 'no gauge configured');
    const modelCount = (info.models || []).length;
    const models = `frequently used models: <a class="bcat-link bcat-yaml-link" href="#">${modelCount}</a> · freeform selection`;
    const modelLibrary = id === 'ollama'
      ? ' · <a class="bcat-link" href="https://ollama.com/library" target="_blank" rel="noopener noreferrer">model library</a>'
      : '';

    // Binary-backed providers (ollama today, see agent/providers.py's
    // _PROVIDER_INSTALL) carry install_cmd/installed on top of the usual
    // auth fields — install-or-manage-models row, not just an auth status.
    const isBinaryBacked = info.install_cmd !== undefined;
    let localHtml = '';
    if (isBinaryBacked && !info.installed) {
      localHtml = `<div class="bcat-install">
        <code class="bcat-cmd">${escapeHtml(info.install_cmd)}</code>
        <button class="bcat-copy" data-cmd="${escapeHtml(info.install_cmd)}">copy</button>
        <button class="bcat-install-btn" data-target="${escapeHtml(id)}">install</button>
      </div>`;
    } else if (isBinaryBacked && info.installed) {
      // pulled_models is absent when the backend couldn't ask ollama (CLI
      // race, timeout) — fall back to both buttons enabled rather than
      // guessing at pulled state.
      const pulled = Array.isArray(info.pulled_models) ? new Set(info.pulled_models) : null;
      const configuredModels = (info.models || []).map(m => {
        const isPulled = pulled ? pulled.has(m) : null;
        const pullDisabled = isPulled === true ? 'disabled' : '';
        const rmDisabled = isPulled === false ? 'disabled' : '';
        return `
        <div class="bcat-model-row">
          <code class="bcat-cmd">${escapeHtml(m)}</code>
          <button class="bcat-pull-btn" data-provider="${escapeHtml(id)}" data-model="${escapeHtml(m)}" ${pullDisabled}>pull</button>
          <button class="bcat-rm-btn" data-provider="${escapeHtml(id)}" data-model="${escapeHtml(m)}" ${rmDisabled}>remove</button>
        </div>`;
      }).join('');
      localHtml = `<div class="bcat-models">${configuredModels}
        <div class="bcat-model-row bcat-custom-model">
          <input class="bcat-model-input" type="text" maxlength="200" placeholder="e.g. llama3.2:3b" aria-label="Ollama model name">
          <button class="bcat-pull-btn" data-provider="${escapeHtml(id)}" disabled>pull</button>
        </div>
      </div>`;
    }

    return `<div class="bcat-row${id === 'ollama' ? ' bcat-row-ollama' : ''}">
      <div class="bcat-name"><span class="bcat-dot" style="background:${escapeHtml(color)}"></span>${escapeHtml(label)}</div>
      <div class="bcat-coding">
        <span class="${statusClass}">${statusMark} ${escapeHtml(authText)}</span>
        <span class="bcat-hint">${models}${modelLibrary}</span>
        ${localHtml}
      </div>
      <div class="bcat-gauge"><span class="bcat-hint">${escapeHtml(gaugeText)}</span></div>
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
  el.querySelectorAll('.bcat-yaml-link').forEach(link => {
    link.addEventListener('click', event => {
      event.preventDefault();
      navigateView('settings');
    });
  });
  el.querySelectorAll('.bcat-install-btn').forEach(btn => {
    btn.addEventListener('click', () => openAuthPanel(btn.dataset.target, null, {
      mode: 'install', anchor: btn.closest('.bcat-row'),
    }));
  });
  el.querySelectorAll('.bcat-pull-btn').forEach(btn => {
    const input = btn.closest('.bcat-custom-model')?.querySelector('.bcat-model-input');
    const pull = () => {
      const model = input ? input.value.trim() : btn.dataset.model;
      if (!model) return;
      openAuthPanel(btn.dataset.provider, null, {
        mode: 'pull', model, anchor: btn.closest('.bcat-row'),
      });
    };
    btn.addEventListener('click', pull);
    if (input) {
      input.addEventListener('input', () => { btn.disabled = !input.value.trim(); });
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter' && input.value.trim()) {
          event.preventDefault();
          pull();
        }
      });
    }
  });
  el.querySelectorAll('.bcat-rm-btn').forEach(btn => {
    btn.addEventListener('click', () => openAuthPanel(btn.dataset.provider, null, {
      mode: 'remove', model: btn.dataset.model, anchor: btn.closest('.bcat-row'),
    }));
  });
}

function renderRuntimeCatalogs(health) {
  updateSettingsFromHealth(health);
  refreshRuntimeMetadata(health);
  renderHarnessesCatalog(health);
  renderProvidersCatalog();
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

function _updateHomeModeIcon(select) {
  const icon = select.parentElement?.querySelector('.home-mode-icon');
  if (icon) icon.textContent = select.value === 'blank_home' ? 'location_away' : 'location_home';
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
  renderRuntimeCatalogs(health);
  renderAgentTable(agents);
}

function renderAgentTable(agents) {
  const listEl = document.getElementById('agents-list');
  if (!agents.length) {
    listEl.innerHTML = '<div class="empty">No agents yet. Add one below.</div>';
    return;
  }
  const sortAgentRows = rows => rows.sort((left, right) => {
    const nameCmp = left.name.localeCompare(right.name, undefined, { sensitivity: 'base', numeric: true });
    const runtimeCmp = left.runtimeSort.localeCompare(right.runtimeSort, undefined, { sensitivity: 'base', numeric: true });
    const cmp = agentTableSort.key === 'name'
      ? nameCmp || runtimeCmp
      : runtimeCmp || nameCmp;
    return agentTableSort.dir === 'desc' ? -cmp : cmp;
  });
  const agentRows = sortAgentRows(agents.map(a => {
    const ref = agentBackendRef(a);
    const runtime = splitAgentRef(a.harness || ref, a.provider || null);
    if (!a.backend) a.backend = ref;
    ensureRuntimeMetadata(runtime.harness, runtime.provider);
    const runtimeText = runtime.provider ? `${runtime.harness} / ${runtime.provider}` : runtime.harness;
    return { a, ref, runtime, runtimeText, runtimeSort: runtimeText || '', name: a.name || '' };
  }));
  const filterQuery = agentFilterQuery.trim().toLowerCase();
  const visibleRows = filterQuery
    ? agentRows.filter(({ a, runtimeText }) =>
        [a.name, runtimeText, a.model, a.cwd].filter(Boolean).join(' ').toLowerCase().includes(filterQuery))
    : agentRows;
  const rows = visibleRows.map(({ a, ref, runtime, runtimeText }) => {
    const model = a.model || '';
    const modelHtml = model
      ? `<span class="agent-model" title="${escapeHtml(model)}">${escapeHtml(model)}</span>`
      : '<span class="col-default">—</span>';
    const cwdHtml = a.cwd
      ? `<span class="agent-cwd agent-cwd-link" data-cwd="${escapeHtml(a.cwd)}" title="Open ${escapeHtml(a.cwd)} in file viewer">${escapeHtml(a.cwd)}</span>`
      : `<span class="agent-cwd agent-cwd-link col-default" data-cwd="${escapeHtml(_squidHome)}" title="Open ${escapeHtml(_squidHome)} in file viewer">${escapeHtml(_squidHome)}</span>`;
    // A custom cwd hides the default squid home from the row entirely — give
    // it a small, grey secondary link back to squid home so it's still one
    // click away instead of only reachable from rows that have no custom cwd.
    const homeLinkHtml = (a.cwd && a.cwd !== _squidHome)
      ? `<span class="agent-cwd-home agent-cwd-link col-default" data-cwd="${escapeHtml(_squidHome)}" title="Open default (${escapeHtml(_squidHome)}) in file viewer">⌂</span>`
      : '';
    // Defaults to User Home for anything that isn't explicitly 'blank_home' --
    // covers agents with a null cwd (default squid tmp home) the same as any
    // other agent; home_mode is independent of cwd.
    const homeMode = a.home_mode === 'blank_home' ? 'blank_home' : 'user_home';
    const homeIcon = homeMode === 'blank_home' ? 'location_away' : 'location_home';
    const homeHtml = `<span class="home-mode-field" title="User Home: today's behavior, full environment inheritance. Blank Home: isolated plugins/skills/settings/history; credential is still symlinked from your real HOME, not isolated.">
        <span class="material-symbols-outlined home-mode-icon" aria-hidden="true">${homeIcon}</span>
        <select class="home-mode-select agent-home-select" data-name="${escapeHtml(a.name)}" data-current="${homeMode}" aria-label="Sandboxed HOME for ${escapeHtml(a.name)}">
          <option value="user_home"${homeMode === 'user_home' ? ' selected' : ''}>User Home</option>
          <option value="blank_home"${homeMode === 'blank_home' ? ' selected' : ''}>Blank Home</option>
        </select>
      </span>`;
    return `
    <tr>
      <td><span class="agent-name">${escapeHtml(a.name)}</span></td>
      <td class="col-runtime">${escapeHtml(runtimeText)}</td>
      <td class="col-model">${modelHtml}</td>
      <td class="col-home">${homeHtml}</td>
      <td class="col-cwd"><span class="agent-cwd-cell">${cwdHtml}${homeLinkHtml}</span></td>
      <td>
        <button class="edit-btn" data-name="${escapeHtml(a.name)}" data-harness="${escapeHtml(runtime.harness)}" data-provider="${escapeHtml(runtime.provider || '')}" data-backend="${escapeHtml(ref || '')}" data-model="${escapeHtml(a.model || '')}" data-cwd="${escapeHtml(a.cwd || '')}" data-home-mode="${escapeHtml(homeMode)}" title="Edit agent">✎</button>
        <button class="del-btn" data-name="${escapeHtml(a.name)}" title="Delete agent (does not affect existing messages)">✕</button>
      </td>
    </tr>`;
  }).join('');
  const sortIndicator = key => agentTableSort.key === key ? (agentTableSort.dir === 'desc' ? 'v' : '^') : '';
  const ariaSort = key => agentTableSort.key === key ? (agentTableSort.dir === 'desc' ? 'descending' : 'ascending') : 'none';
  const bodyHtml = visibleRows.length
    ? rows
    : `<tr><td colspan="6" class="empty">No agents match "${escapeHtml(agentFilterQuery)}".</td></tr>`;
  listEl.innerHTML = `
    <div class="agent-filter-wrap">
      <input id="agent-filter" class="agent-filter" type="text" placeholder="Filter agents…" aria-label="Filter agents" value="${escapeHtml(agentFilterQuery)}" />
    </div>
    <table>
    <thead><tr>
      <th aria-sort="${ariaSort('name')}"><button class="agent-sort-btn" data-sort="name" type="button">Name <span class="sort-indicator">${sortIndicator('name')}</span></button></th>
      <th class="col-runtime" aria-sort="${ariaSort('runtime')}"><button class="agent-sort-btn" data-sort="runtime" type="button">Runtime <span class="sort-indicator">${sortIndicator('runtime')}</span></button></th>
      <th class="col-model">Model</th><th class="col-home">Home</th><th class="col-cwd">CWD</th><th></th>
    </tr></thead>
    <tbody>${bodyHtml}</tbody>
  </table>`;

  const filterInput = document.getElementById('agent-filter');
  filterInput.addEventListener('input', () => {
    agentFilterQuery = filterInput.value;
    const cursorPos = filterInput.selectionStart;
    renderAgentTable(agents);
    const newInput = document.getElementById('agent-filter');
    newInput.focus();
    const pos = Math.min(cursorPos, newInput.value.length);
    newInput.setSelectionRange(pos, pos);
  });

  listEl.querySelectorAll('.agent-sort-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.sort;
      if (agentTableSort.key === key) {
        agentTableSort = { key, dir: agentTableSort.dir === 'asc' ? 'desc' : 'asc' };
      } else {
        agentTableSort = { key, dir: 'asc' };
      }
      renderAgentTable(agents);
    });
  });

  listEl.querySelectorAll('.agent-cwd-link').forEach(el => {
    el.addEventListener('click', () => openFileViewer(el.dataset.cwd));
  });

  listEl.querySelectorAll('.agent-home-select').forEach(sel => {
    sel.addEventListener('change', async () => {
      const name = sel.dataset.name;
      const prevMode = sel.dataset.current;
      const newMode = sel.value;
      if (newMode === prevMode) return;
      const statusEl = document.getElementById('agent-form-status');
      const sessions = await fetch(`/config/agents/${encodeURIComponent(name)}/sessions`).then(r => r.ok ? r.json() : null).catch(() => null);
      const activeTopics = sessions?.topics?.map(s => s.topic) ?? [];
      if (activeTopics.length > 0) {
        const ok = await confirmAgentSessionClear(name, activeTopics);
        if (!ok) { sel.value = prevMode; return; }
      }
      try {
        const res = await fetch(`/config/agents/${encodeURIComponent(name)}/home-mode`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ home_mode: newMode }),
        });
        if (res.ok) {
          const data = await res.json();
          sel.dataset.current = newMode;
          _updateHomeModeIcon(sel);
          _agentsCache = null;
          if (statusEl) {
            const cleared = data.sessions_cleared || [];
            statusEl.textContent = cleared.length ? `home mode saved ✓ (cleared sessions: ${cleared.join(', ')})` : 'home mode saved ✓';
            setTimeout(() => { statusEl.textContent = ''; }, 5000);
          }
        } else {
          sel.value = prevMode;
        }
      } catch {
        sel.value = prevMode;
      }
    });
  });

  listEl.querySelectorAll('.edit-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.getElementById('af-name').value    = btn.dataset.name;
      document.getElementById('af-harness').value = btn.dataset.harness;
      const provider = setProviderOptions(btn.dataset.harness, btn.dataset.provider || null);
      syncAgentModelControls(btn.dataset.harness, provider);
      document.getElementById('af-model').value   = btn.dataset.model;
      document.getElementById('af-cwd').value     = btn.dataset.cwd;
      const afHomeMode = document.getElementById('af-home-mode');
      afHomeMode.value = btn.dataset.homeMode === 'blank_home' ? 'blank_home' : 'user_home';
      _updateHomeModeIcon(afHomeMode);
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
  const afHarness = document.getElementById('af-harness');
  const afProvider = document.getElementById('af-provider');
  afHarness.addEventListener('change', () => {
    const provider = setProviderOptions(afHarness.value);
    syncAgentModelControls(afHarness.value, provider);
  });
  afProvider.addEventListener('change', () => {
    syncAgentModelControls(afHarness.value, afProvider.value || null);
  });
  const afHomeMode = document.getElementById('af-home-mode');
  afHomeMode.addEventListener('change', () => _updateHomeModeIcon(afHomeMode));
  document.getElementById('config-editor-reload').addEventListener('click', loadConfigYaml);
  document.getElementById('config-editor-save').addEventListener('click', saveConfigYaml);

  document.getElementById('agent-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      name:      document.getElementById('af-name').value.trim(),
      ...selectedRuntime(),
      model:     document.getElementById('af-model').value.trim() || null,
      cwd:       document.getElementById('af-cwd').value.trim()   || null,
      home_mode: document.getElementById('af-home-mode').value,
    };
    if (!body.name) return;

    // Warn if key attributes changed on an existing agent with active sessions
    const existing = (_agentsCache || []).find(a => a.name === body.name);
    if (existing) {
      const keyChanged = (existing.harness || '') !== body.harness ||
                         (existing.provider || null) !== body.provider ||
                         (existing.model || null) !== body.model ||
                         (existing.cwd || null) !== body.cwd ||
                         (existing.home_mode || 'user_home') !== body.home_mode;
      if (keyChanged) {
        const sessions = await fetch(`/config/agents/${encodeURIComponent(body.name)}/sessions`).then(r => r.ok ? r.json() : null).catch(() => null);
        const activeTopics = sessions?.topics?.map(s => s.topic) ?? [];
        if (activeTopics.length > 0) {
          const ok = await confirmAgentSessionClear(body.name, activeTopics);
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
        const afHomeModeReset = document.getElementById('af-home-mode');
        afHomeModeReset.value = 'user_home';
        _updateHomeModeIcon(afHomeModeReset);
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
  if (!meta.exists) seedTopicMemoryPlaceholder(topic);
  showCodeRootsNudge(topic, anchor);
}

// Seeds memory.md with a commented squid-config example on first contact with a
// topic that has no memory file yet, so dismissing/skipping the nudge still
// leaves users a file that shows what's available to configure later.
function seedTopicMemoryPlaceholder(topic) {
  fetch(`/topics/${encodeURIComponent(topic)}/memory/squid/seed`, { method: 'POST' }).catch(() => {});
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
  const harnessOptions = Object.keys(_harnessMetadata).length
    ? Object.keys(_harnessMetadata).sort((a, b) => (_harnessMetadata[a].label || a).localeCompare(_harnessMetadata[b].label || b))
    : ['claudecode', 'codex', 'cursor', 'opencode'];
  const firstHarness = harnessOptions[0];
  const firstProvider = _harnessMetadata[firstHarness]?.default_provider || providerOptionsForHarness(firstHarness)[0] || '';
  prompt.innerHTML = `
    <div class="acp-title">Agent <strong>${agentName}</strong> not found — create it?</div>
    <div class="acp-row">
      <select id="acp-harness">
        ${harnessOptions.map(id => `<option value="${escapeHtml(id)}">${escapeHtml(_harnessMetadata[id]?.label || id)}</option>`).join('')}
      </select>
      <select id="acp-provider"></select>
      <input id="acp-model" placeholder="${backendModelHint(runtimeRef(firstHarness, firstProvider))}" />
      <select id="acp-model-picker" class="model-picker" title="Pick a known model" aria-label="Pick a known model" hidden></select>
      <input id="acp-cwd" placeholder="cwd (default: ${_squidHome})" />
    </div>
    <div class="acp-actions">
      <button id="acp-save">Create &amp; send</button>
      <button id="acp-cancel">Cancel</button>
    </div>`;

  messages.appendChild(prompt);
  messages.scrollTop = messages.scrollHeight;

  const modelInput = prompt.querySelector('#acp-model');
  const harnessSelect = prompt.querySelector('#acp-harness');
  const providerSelect = prompt.querySelector('#acp-provider');
  setProviderOptions(firstHarness, firstProvider, 'acp-provider');
  populateModelPicker('acp-model-picker', 'acp-model', runtimeRef(firstHarness, providerSelect.value || firstProvider));
  harnessSelect.addEventListener('change', e => {
    setProviderOptions(e.target.value, null, 'acp-provider');
    modelInput.placeholder = backendModelHint(runtimeRef(e.target.value, providerSelect.value || null));
    populateModelPicker('acp-model-picker', 'acp-model', runtimeRef(e.target.value, providerSelect.value || null));
  });
  providerSelect.addEventListener('change', e => {
    modelInput.placeholder = backendModelHint(runtimeRef(harnessSelect.value, e.target.value || null));
    populateModelPicker('acp-model-picker', 'acp-model', runtimeRef(harnessSelect.value, e.target.value || null));
  });

  prompt.querySelector('#acp-cancel').addEventListener('click', () => prompt.remove());
  prompt.querySelector('#acp-save').addEventListener('click', async () => {
    const harness = harnessSelect.value;
    const provider = providerSelect.value || null;
    const model   = prompt.querySelector('#acp-model').value.trim() || null;
    const cwd     = prompt.querySelector('#acp-cwd').value.trim()   || null;
    const res = await fetch('/config/agents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: agentName, harness, provider, model, cwd }),
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
        return mergeAgentCache(agents);
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
  } else if (item.routeTarget && item.previewApply) {
    applyRouteTarget(item.routeTarget);
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
    if (promptDraftChip) {
      setTopicChip(promptDraftChip.topic, promptDraftChip.agent, promptDraftChip.adhoc, promptDraftChip.lookback || 0, {
        route: promptDraftChip.route,
        chainTarget: promptDraftChip.chainTarget,
        chainTargetFresh: promptDraftChip.chainTargetFresh,
        chainOperator: promptDraftChip.chainOperator,
        chainRounds: promptDraftChip.chainRounds,
        chainTargetTopic: promptDraftChip.chainTargetTopic,
        broadcastAgents: promptDraftChip.broadcastAgents,
        flowOrigins: promptDraftChip.flowOrigins,
      });
    }
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
    `<div class="ac-row${item.rowClass ? ` ${item.rowClass}` : ''}">` +
    (item.routeHtml ? `<button class="ac-route-btn" type="button" data-i="${i}" title="Switch to this route">${item.routeHtml}${item.routeSwitchIcon === false ? '' : '<span class="ac-route-switch-icon" aria-hidden="true"></span>'}</button> ` : '') +
    `<span class="ac-label${item.labelClass ? ` ${item.labelClass}` : ''}">${item.label}</span>` +
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
        const item = idx >= 0 && idx < acItems.length ? acItems[idx] : null;
        const fullEntry = item?.fullEntry || null;
        if (fullEntry) {
          hideAutocomplete();
          applyPromptHistoryEntry(fullEntry);
          input.focus();
        } else if (item) {
          _acSelect(idx);
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
    messages.focus();
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
  if (item.routeTarget) {
    applyRouteTarget(item.routeTarget);
    input.focus();
    return;
  }
  if (item.promoteRoute) {
    const chain = parseRouteChain(item.insert);
    if (chain) {
      setTopicChip(chain.topic, chain.origin, chain.originFresh, 0, {
        route: chain.route,
        chainTarget: chain.target,
        chainTargetFresh: chain.targetFresh,
        chainOperator: chain.operator,
        chainRounds: chain.rounds,
        chainTargetTopic: chain.targetTopic,
        flowOrigins: chain.origins,
      });
      input.value = '';
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

function _coloredRouteHtml(route) {
  const src = String(route || '');
  let out = '';
  let last = 0;
  const tokenRe = new RegExp(`#${TOPIC_SLUG_SRC}|@${AGENT_SLUG_SRC}!?`, 'g');
  for (const match of src.matchAll(tokenRe)) {
    const token = match[0];
    out += escapeHtml(src.slice(last, match.index));
    if (token.startsWith('#')) {
      out += `<span class="ac-topic">${escapeHtml(token)}</span>`;
    } else {
      const agent = token.slice(1).replace(/!$/, '');
      out += `<span class="ac-agent"${_agentStyleAttr(agent)}>${escapeHtml(token)}</span>`;
    }
    last = match.index + token.length;
  }
  out += escapeHtml(src.slice(last));
  return out;
}

function appendColoredRouteTokens(parent, route, opts = {}) {
  const topicClass = opts.topicClass || 'chip-topic';
  const agentClass = opts.agentClass || 'chip-agent';
  const freshClass = opts.freshClass || 'chip-adhoc';
  const src = String(route || '');
  let last = 0;
  const tokenRe = new RegExp(`#${TOPIC_SLUG_SRC}|@${AGENT_SLUG_SRC}!?`, 'g');
  for (const match of src.matchAll(tokenRe)) {
    const token = match[0];
    if (match.index > last) parent.appendChild(document.createTextNode(src.slice(last, match.index)));
    if (token.startsWith('#')) {
      const span = document.createElement('span');
      span.className = topicClass;
      span.textContent = token;
      parent.appendChild(span);
    } else {
      const fresh = token.endsWith('!');
      const agent = token.slice(1).replace(/!$/, '');
      const agentSpan = document.createElement('span');
      agentSpan.className = agentClass;
      agentSpan.textContent = '@' + agent;
      setAgentSlugColor(agentSpan, agent);
      parent.appendChild(agentSpan);
      if (fresh) {
        const freshSpan = document.createElement('span');
        freshSpan.className = freshClass;
        freshSpan.textContent = '!';
        setAgentSlugColor(freshSpan, agent);
        parent.appendChild(freshSpan);
      }
    }
    last = match.index + token.length;
  }
  if (last < src.length) parent.appendChild(document.createTextNode(src.slice(last)));
}

function _acRouteLabel(topic, agent = '', backendFallback = null) {
  const cleanAgent = agent.replace(/[!]\d*$/, '');
  return `<span class="ac-topic">#${escapeHtml(topic)}</span>` +
    (agent ? `<span class="ac-agent"${_agentStyleAttr(cleanAgent, backendFallback)}>@${escapeHtml(agent)}</span>` : '');
}

function _acRouteHtml(route) {
  const cm = parseRouteChain(route);
  if (cm) {
    if (cm.complex) return _coloredRouteHtml(cm.route || route);
    return _acRouteLabel(cm.topic, cm.origin + (cm.originFresh ? '!' : '')) +
      `<span class="ac-route-chain-arrow">${escapeHtml(cm.operator)}</span>` +
      `<span class="ac-agent"${_agentStyleAttr(cm.target)}>@${escapeHtml(cm.target)}${cm.targetFresh ? '!' : ''}</span>`;
  }
  const broadcast = parseOriginBroadcast(route);
  if (broadcast) {
    let lastTopic = null;
    return broadcast.agents.map((a, i) => {
      const sep = i > 0 ? '<span class="ac-broadcast-sep">,</span>' : '';
      let topicHtml = '';
      if (a.topic !== lastTopic) {
        topicHtml = `<span class="ac-topic">#${escapeHtml(a.topic)}</span>`;
        lastTopic = a.topic;
      }
      const agentHtml = `<span class="ac-agent"${_agentStyleAttr(a.agent)}>@${escapeHtml(a.agent)}${a.fresh ? '!' : ''}</span>`;
      return sep + topicHtml + agentHtml;
    }).join('');
  }
  const rm = String(route || '').match(new RegExp(`^#(${TOPIC_SLUG_SRC})(?:@(${AGENT_SLUG_SRC}))?(!\\d*)?$`));
  if (rm) return _acRouteLabel(rm[1], (rm[2] || '') + (rm[3] || ''));
  // Neither parser resolved this route — e.g. a join whose origins span
  // different topics leaves a bare `@target` topic-ambiguous, so
  // parseRouteChain returns null while the composer is still mid-type.
  // Render the raw tokens instead of going blank; it becomes a normal
  // colored/parsed label again once the missing piece (usually the target's
  // #topic) is typed.
  return _coloredRouteHtml(route);
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

  // An active chip already owns routing (parseInput reuses it unconditionally
  // now — see _maybeCollapseExpandedSlug/_maybePromoteSlug), so "#topic@agent"
  // typed as ordinary message text no longer functions as a route unless the
  // chip is explicitly opened for editing first. Route-shaped autocomplete
  // would be misleading in that state — picking a suggestion would just
  // insert literal text, not set a route — so skip it and fall through to
  // the same handling any other message text gets (prompt history, below).
  const routeSyntaxActive = !stickyChip || editingExpandedSlug;
  const chainSyntaxActive = routeSyntaxActive || new RegExp(`^#${TOPIC_SLUG_SRC}@${AGENT_SLUG_SRC}!?(?:[+,](?:#${TOPIC_SLUG_SRC})?(?:@${AGENT_SLUG_SRC})?!?)*(?:<>?|>|=>)`).test(slugVal);
  const broadcastSyntaxActive = routeSyntaxActive || new RegExp(`^#${TOPIC_SLUG_SRC}@${AGENT_SLUG_SRC}!?(?:,(?:#${TOPIC_SLUG_SRC}@?${AGENT_SLUG_PARTIAL_SRC}|@${AGENT_SLUG_PARTIAL_SRC}|#${TOPIC_SLUG_SRC}|${AGENT_SLUG_PARTIAL_SRC})!?)*,$`).test(slugVal);
  const aliasSyntaxActive = routeSyntaxActive || new RegExp(`^#${TOPIC_SLUG_SRC}@${AGENT_SLUG_PARTIAL_SRC}$`).test(slugVal);
  const mTopic = routeSyntaxActive && slugVal.match(new RegExp(`^#(${TOPIC_SLUG_PARTIAL_SRC})[!]?$`));
  const mMultiOriginChainAlias = chainSyntaxActive && slugVal.match(new RegExp(`^(#${TOPIC_SLUG_SRC}@${AGENT_SLUG_SRC}!?(?:[+,](?:#${TOPIC_SLUG_SRC})?(?:@${AGENT_SLUG_SRC})?!?)+)(<>?|>|=>)(?:@?)(${AGENT_SLUG_PARTIAL_SRC})(!)?$`));
  const mChainAlias = chainSyntaxActive && slugVal.match(new RegExp(`^#(${TOPIC_SLUG_SRC})@(${AGENT_SLUG_SRC})(!)?(<>?|=>|>)(?:@?)(${AGENT_SLUG_PARTIAL_SRC})(!)?$`));
  const mChainTopicTarget = chainSyntaxActive && slugVal.match(new RegExp(`^#(${TOPIC_SLUG_SRC})@(${AGENT_SLUG_SRC})(!)?(<>?|=>|>)#(${TOPIC_SLUG_PARTIAL_SRC})$`));
  const mAlias = aliasSyntaxActive && slugVal.match(new RegExp(`^#(${TOPIC_SLUG_SRC})@(${AGENT_SLUG_PARTIAL_SRC})(!\\d*)?$`));
  // Origin Broadcast (ADR-0032): autocomplete for whichever trailing atom is
  // currently being typed after the last comma — everything before it (the
  // already-typed atoms) is preserved verbatim, never rewritten. `,` and
  // `,@agent` suggest agents; `,#topic` suggests topics; `,#topic@agent`
  // suggests agents scoped to the explicit topic if one was typed, else to
  // the rolling-anchor topic that atom would currently inherit.
  const _BROADCAST_PREFIX_SRC = `#${TOPIC_SLUG_SRC}@${AGENT_SLUG_SRC}!?(?:,(?:#${TOPIC_SLUG_SRC}@${AGENT_SLUG_SRC}|#${TOPIC_SLUG_SRC}|@${AGENT_SLUG_SRC})!?)*`;
  const mBroadcastFull = broadcastSyntaxActive && slugVal.match(new RegExp(`^(${_BROADCAST_PREFIX_SRC}),#(${TOPIC_SLUG_SRC})@(${AGENT_SLUG_PARTIAL_SRC})$`));
  const mBroadcastAgent = broadcastSyntaxActive && slugVal.match(new RegExp(`^(${_BROADCAST_PREFIX_SRC}),@?(${AGENT_SLUG_PARTIAL_SRC})$`));
  const mBroadcastTopic = broadcastSyntaxActive && slugVal.match(new RegExp(`^(${_BROADCAST_PREFIX_SRC}),#(${TOPIC_SLUG_PARTIAL_SRC})$`));
  if (mBroadcastFull || mBroadcastAgent) {
    const prefix = mBroadcastFull ? mBroadcastFull[1] : mBroadcastAgent[1];
    const explicitTopic = mBroadcastFull ? mBroadcastFull[2].toLowerCase() : null;
    const agentPrefix = (mBroadcastFull ? mBroadcastFull[3] : mBroadcastAgent[2]).toLowerCase();
    let topic = explicitTopic;
    if (!topic) {
      const resolved = _resolveBroadcastAtoms(prefix.split(','));
      topic = resolved ? resolved[resolved.length - 1].topic : null;
    }
    if (!topic) { hideAutocomplete(); return; }
    const prefixAgentKeys = new Set((_resolveBroadcastAtoms(prefix.split(',')) || [])
      .map(a => `${a.topic}\0${String(a.agent || '').toLowerCase()}`));
    const [agents, history] = await Promise.all([
      _acAgents(),
      fetch(`/topics/${encodeURIComponent(topic)}/agents/history`).then(r => r.ok ? r.json() : []).catch(() => []),
    ]);
    if (input.value !== val) return;
    const usedNames = new Set(history.map(h => h.agent));
    const backendByAgent = new Map(agents.map(a => [a.name, a.backend]));
    let items = [];
    const topicPrefix = explicitTopic ? `#${topic}` : ''; // omit if the topic was inherited, not typed
    const addItem = (name, fresh, sub, meta) => {
      if (prefixAgentKeys.has(`${topic}\0${String(name || '').toLowerCase()}`)) return;
      const route = `${prefix},${topicPrefix}@${name}${fresh ? '!' : ''}`;
      items.push({
        label: _acRouteHtml(route),
        insert: route,
        replaceSlug: replacingSlug,
        sub,
        meta: fresh ? 'fresh' : meta,
      });
    };
    for (const h of history) {
      if (!h.agent.toLowerCase().startsWith(agentPrefix)) continue;
      // ADR-0032: autocomplete should prefer `!` on broadcast origins —
      // comparing agents usually wants independent fresh takes.
      addItem(h.agent, true, _acLastPrompt(h.last_adhoc_prompt));
      addItem(h.agent, false, _acLastPrompt(h.last_prompt));
    }
    appendMatchingRouteHistoryItems(items, slugVal);
    if (agentPrefix) {
      for (const a of agents) {
        if (usedNames.has(a.name)) continue;
        if (!a.name.toLowerCase().startsWith(agentPrefix)) continue;
        addItem(a.name, true, '', a.backend);
        addItem(a.name, false, '', a.backend);
      }
    }
    _acRender(items.slice(0, 10), 'Routes');
  } else if (mBroadcastTopic) {
    const prefix = mBroadcastTopic[1];
    const topicPrefix = mBroadcastTopic[2].toLowerCase();
    const topics = await _acTopics();
    if (input.value !== val) return;
    _acRender(
      topics.filter(t => t.name.toLowerCase().startsWith(topicPrefix)).slice(0, 8)
        .map(t => ({
          label: _acRouteLabel(t.name, t.agent || '', t.last_backend || null),
          insert: `${prefix},#${t.name}`,
          replaceSlug: replacingSlug,
          meta: t.active ? '● live' : t.queue_depth > 0 ? `queue ${t.queue_depth}` : '',
          sub: _acLastPrompt(t.last_prompt),
        })),
      'Routes'
    );
  } else if (mTopic) {
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
  } else if (mMultiOriginChainAlias) {
    const originGroup = mMultiOriginChainAlias[1];
    const operator = mMultiOriginChainAlias[2].startsWith('<') ? '<>' : mMultiOriginChainAlias[2];
    const prefix = mMultiOriginChainAlias[3].toLowerCase();
    const targetFreshTyped = mMultiOriginChainAlias[4] !== undefined;
    const resolvedOrigins = _resolveBroadcastAtoms(originGroup.split(/[+,]/));
    const topic = resolvedOrigins && new Set(resolvedOrigins.map(a => a.topic)).size === 1 ? resolvedOrigins[0].topic : null;
    const [agents, history] = await Promise.all([
      _acAgents(),
      topic ? fetch(`/topics/${encodeURIComponent(topic)}/agents/history`).then(r => r.ok ? r.json() : []).catch(() => []) : Promise.resolve([]),
    ]);
    if (input.value !== val) return;

    const usedNames = new Set(history.map(h => h.agent));
    const backendByAgent = new Map(agents.map(a => [a.name, a.backend]));
    let items = [];
    const routePrefix = `${originGroup}${operator}@`;
    const addChainItem = (agentName, fresh, meta) => {
      const route = `${routePrefix}${agentName}${fresh ? '!' : ''}`;
      items.push({
        label: _acRouteHtml(route),
        insert: route,
        replaceSlug: replacingSlug,
        promoteRoute: true,
        sub: _acLastPrompt(lastPromptForFlowRoute(route)),
        meta,
      });
    };

    for (const h of history) {
      if (!h.agent.toLowerCase().startsWith(prefix)) continue;
      if (targetFreshTyped) {
        addChainItem(h.agent, true, 'fresh');
        continue;
      }
      addChainItem(h.agent, false, backendByAgent.get(h.agent) || null);
      addChainItem(h.agent, true, 'fresh');
    }

    for (const a of agents) {
      if (usedNames.has(a.name)) continue;
      if (!a.name.toLowerCase().startsWith(prefix)) continue;
      addChainItem(a.name, targetFreshTyped, targetFreshTyped ? 'fresh' : a.backend);
    }

    appendMatchingRouteHistoryItems(items, slugVal);
    _acRender(items.slice(0, 10), 'Routes');
  } else if (mChainTopicTarget) {
    const originTopic = mChainTopicTarget[1];
    const originAgent = mChainTopicTarget[2];
    const originFresh = mChainTopicTarget[3] || '';
    const operator = mChainTopicTarget[4].startsWith('<') ? '<>' : mChainTopicTarget[4];
    const topicPrefix = mChainTopicTarget[5].toLowerCase();
    const topics = await _acTopics();
    if (input.value !== val) return;
    const items = topics.filter(t => t.name.toLowerCase().startsWith(topicPrefix)).slice(0, 8)
      .map(t => {
        const route = `#${originTopic}@${originAgent}${originFresh}${operator}#${t.name}`;
        return {
        label: escapeHtml(route),
        insert: route,
        replaceSlug: replacingSlug,
        promoteRoute: true,
        meta: t.active ? '● live' : t.queue_depth > 0 ? `queue ${t.queue_depth}` : '',
        sub: _acLastPrompt(lastPromptForFlowRoute(route)),
      };
      });
    appendMatchingRouteHistoryItems(items, slugVal);
    _acRender(items.slice(0, 10), 'Routes');
  } else if (mChainAlias) {
    const topic = mChainAlias[1];
    const originAgent = mChainAlias[2];
    const originFresh = mChainAlias[3] || '';
    // Autocomplete offers agents as soon as the operator starts (e.g. a bare
    // "<"), not just once it's fully typed as "<>" — auto-close the bracket
    // since numbered rounds ("<N>") aren't accepted yet.
    const operator = mChainAlias[4].startsWith('<') ? '<>' : mChainAlias[4];
    const prefix = mChainAlias[5].toLowerCase();
    const targetFreshTyped = mChainAlias[6] !== undefined;
    const [agents, history] = await Promise.all([
      _acAgents(),
      fetch(`/topics/${encodeURIComponent(topic)}/agents/history`).then(r => r.ok ? r.json() : []).catch(() => []),
    ]);
    if (input.value !== val) return;

    const usedNames = new Set(history.map(h => h.agent));
    const backendByAgent = new Map(agents.map(a => [a.name, a.backend]));
    const items = [];
    const routePrefix = `#${topic}@${originAgent}${originFresh}${operator}@`;
    const addChainItem = (agentName, fresh, meta) => {
      const route = `${routePrefix}${agentName}${fresh ? '!' : ''}`;
      items.push({
        label: _acRouteHtml(route),
        insert: route,
        replaceSlug: replacingSlug,
        promoteRoute: true,
        sub: _acLastPrompt(lastPromptForFlowRoute(route)),
        meta,
      });
    };

    for (const h of history) {
      if (!h.agent.toLowerCase().startsWith(prefix)) continue;
      if (targetFreshTyped) {
        addChainItem(h.agent, true, 'fresh');
        continue;
      }
      addChainItem(h.agent, false, backendByAgent.get(h.agent) || null);
      addChainItem(h.agent, true, 'fresh');
    }

    for (const a of agents) {
      if (usedNames.has(a.name)) continue;
      if (!a.name.toLowerCase().startsWith(prefix)) continue;
      addChainItem(a.name, targetFreshTyped, targetFreshTyped ? 'fresh' : a.backend);
    }

    appendMatchingRouteHistoryItems(items, slugVal);
    _acRender(items.slice(0, 10), 'Routes');
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

    appendMatchingRouteHistoryItems(items, slugVal);
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
  const REFRESH_DRAG_THRESHOLD = 480;

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
    if (dy > REFRESH_DRAG_THRESHOLD) setTimeout(() => location.reload(), 150);
    startY = 0;
  }, { passive: true });
}

// ── boot banner ──────────────────────────────────────────────────────────────

const BOOT_FALLBACK_TEXT = 'More Done, Less Tokens';
const INSIGHTS_URL = 'https://agentsquid.ai/insights.json';

const BOOT_LOGO_ART = ` 🦑 AGENT
 ██████╗ ██████╗ ██╗   ██╗██╗██████╗
██╔════╝██╔═══██╗██║   ██║██║██╔══██╗
╚█████╗ ██║   ██║██║   ██║██║██║  ██║
 ╚═══██╗██║▄▄ ██║██║   ██║██║██║  ██║
██████╔╝╚██████╔╝╚██████╔╝██║██████╔╝
╚═════╝  ╚══▀▀═╝  ╚═════╝ ╚═╝╚═════╝`;

const BOOT_LOGO_MOBILE = '🦑 AGENT-SQUID';

// ── streak ──────────────────────────────────────────────────────────────────

function _todayKey() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function getStreak() {
  try {
    const dates = JSON.parse(localStorage.getItem('squid_active_days') || '[]');
    const set = new Set(dates);
    set.add(_todayKey());
    localStorage.setItem('squid_active_days', JSON.stringify([...set].sort().slice(-400)));
    let streak = 0;
    const d = new Date();
    while (true) {
      const k = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      if (set.has(k)) { streak++; d.setDate(d.getDate() - 1); }
      else break;
    }
    return streak;
  } catch { return 1; }
}

// ── insights fetch ─────────────────────────────────────────────────────────

async function fetchInsights() {
  try {
    // Cache-bust to avoid stale CDN edge cache
    const res = await fetch(`${INSIGHTS_URL}?_=${Date.now()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    localStorage.setItem('squid_insights', JSON.stringify(data));
    return data;
  } catch {
    try {
      return JSON.parse(localStorage.getItem('squid_insights'));
    } catch {}
    return null;
  }
}

// ── message picker ─────────────────────────────────────────────────────────

const MEASURE_DEFAULT_AGG = {
  turns: 'sum', sessions: 'sum', cost: 'sum',
  tokens_in: 'sum', tokens_out: 'sum', tokens_total: 'sum',
  cache_read: 'sum', cache_write: 'sum', new_input: 'sum',
  quota: 'sum', duration: 'sum',
  marked_bad: 'sum',
  cache_hit_rate: 'avg', avg_tokens_turn: 'sum',
};

function _measureRowValue(row, measure) {
  // Map measure name to the column in a /stats row.
  // Rows from get_aggregated_stats have fields like total_turns, cost_usd, etc.
  // Chart series values are in chart_{measure}_{agg} columns.
  switch (measure) {
    case 'turns': return row.total_turns || 0;
    case 'sessions': return row.sessions || 0;
    case 'cost': return row.cost_usd || 0;
    case 'tokens_in': return _statsInputTokens(row);
    case 'tokens_out': return row.output_tokens || 0;
    case 'tokens_total': return _statsInputTokens(row) + (row.output_tokens || 0);
    case 'cache_read': return _splitInputTokens(row).cacheRead;
    case 'cache_write': return _splitInputTokens(row).cacheWrite;
    case 'new_input': return _splitInputTokens(row).newInput;
    case 'cache_hit_rate': return _cacheHitRate(row) || 0;
    case 'avg_tokens_turn': return _avgTokensPerTurn(row) || 0;
    case 'quota': return row.quota_delta || 0;
    case 'marked_bad': return row.marked_bad || 0;
    case 'duration': return (row.duration_ms || 0) / 1000;
    default: return 0;
  }
}

function _aggregateRows(rows, measure, agg) {
  if (!rows.length) return 0;
  const vals = rows.map(r => _measureRowValue(r, measure)).filter(v => v != null);
  if (!vals.length) return 0;
  switch (agg) {
    case 'sum': return vals.reduce((a, b) => a + b, 0);
    case 'avg': return vals.reduce((a, b) => a + b, 0) / vals.length;
    case 'min': return Math.min(...vals);
    case 'max': return Math.max(...vals);
    default: return vals.reduce((a, b) => a + b, 0);
  }
}

function _fmtValue(raw, measure, fmt) {
  if (fmt === 'delta') {
    const sign = raw >= 0 ? '+' : '';
    return `${sign}${Math.round(raw)}`;
  }
  if (fmt === 'pp') {
    const sign = raw >= 0 ? '+' : '';
    return `${sign}${raw.toFixed(1)} pp`;
  }
  if (fmt === 'pct') {
    const sign = raw >= 0 ? '+' : '';
    return `${sign}${raw.toFixed(1)}%`;
  }
  // Default formatting by measure type
  if (measure === 'cache_hit_rate') return `${raw.toFixed(1)}%`;
  if (measure === 'cost') return `$${raw.toFixed(2)}`;
  if (measure === 'duration') {
    const m = Math.floor(raw / 60);
    const s = Math.round(raw % 60);
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
  }
  return String(Math.round(raw));
}

async function resolveInsightMeasures(insights) {
  const measures = insights?.measures?.values;
  if (!measures || !measures.length) return null;

  const defaultPeriod = insights.measures.period || '7d';
  const dbMeasures = measures.filter(m => m.measure);

  if (!dbMeasures.length) return null;

  // Group by period to minimize API calls
  const groups = new Map();
  for (const m of dbMeasures) {
    const period = m.period || defaultPeriod;
    const hasCompare = !!m.compare;
    const key = `${period}|${hasCompare}`;
    if (!groups.has(key)) groups.set(key, { period, hasCompare, measures: [] });
    groups.get(key).measures.push(m);
  }

  const values = {};
  // Seed local/clock sources
  for (const m of measures) {
    if (m.source === 'local' && m.key === 'streak') values[m.key] = getStreak();
    if (m.source === 'clock' && m.key === 'hour') values[m.key] = new Date().getHours();
    if (m.source === 'clock' && m.key === 'dow') values[m.key] = new Date().getDay();
  }

  for (const [, group] of groups) {
    const days = Math.max(parseInt(group.period) || 7, 7);

    const fetchRows = async (anchor) => {
      const params = new URLSearchParams();
      params.set('period', days <= 14 ? 'weekly' : 'daily');
      params.set('days', String(days));
      if (anchor) params.set('anchor', anchor);
      if (group.measures[0].filter?.agent) params.set('agent', group.measures[0].filter.agent);
      if (group.measures[0].filter?.topic) params.set('topic', group.measures[0].filter.topic);
      if (group.measures[0].filter?.adhoc) params.set('adhoc', group.measures[0].filter.adhoc);
      try {
        const res = await fetch(`/stats?${params}`);
        return res.ok ? await res.json() : [];
      } catch { return []; }
    };

    const currentRows = await fetchRows();
    // Fetch the previous window as its own exact N-day query (anchored to
    // N days ago) instead of doubling the range and splitting rows in half —
    // weekly buckets are calendar-aligned (Sunday-start), not aligned to the
    // request's day count, so a 2N-day fetch can split into an uneven number
    // of buckets and a naive halfway slice mismatches window sizes badly.
    const prevRows = group.hasCompare
      ? await fetchRows(new Date(Date.now() - days * 86400000).toISOString())
      : null;

    for (const m of group.measures) {
      const agg = m.agg || MEASURE_DEFAULT_AGG[m.measure] || 'sum';

      if (m.compare === 'prev_period') {
        const cur = _aggregateRows(currentRows, m.measure, agg);
        const prev = _aggregateRows(prevRows, m.measure, agg);
        values[m.key] = cur - prev;  // raw delta for conditions
        values[`_${m.key}_cur`] = cur;   // current period raw value
        values[`_${m.key}_prev`] = prev;  // previous period raw value
      } else {
        values[m.key] = _aggregateRows(currentRows, m.measure, agg);
      }
    }
  }

  // Attach measure metadata for formatting during substitution
  values._measures = measures;
  return values;
}

function _evalCondition(value, cond) {
  if (cond === null || cond === undefined) return true;
  if (typeof cond === 'object' && cond !== null && !Array.isArray(cond)) {
    if ('eq' in cond) return value === cond.eq;
    if ('gte' in cond) return value >= cond.gte;
    if ('lte' in cond) return value <= cond.lte;
    if ('gt' in cond) return value > cond.gt;
    if ('lt' in cond) return value < cond.lt;
    if ('in' in cond) return cond.in.includes(value);
    if ('between' in cond) return value >= cond.between[0] && value < cond.between[1];
  }
  // Bare value: exact match
  return value === cond;
}

function pickBootMessage(insights, values) {
  const section = insights?.boot;
  if (!section || !section.templates) return section?.default || BOOT_FALLBACK_TEXT;

  const measures = values?._measures || [];
  const measureByKey = {};
  for (const m of measures) measureByKey[m.key] = m;

  // Fisher-Yates shuffle so every matching template gets equal probability.
  // `random` weights on individual templates still act as a per-template gate.
  const templates = [...section.templates];
  for (let i = templates.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [templates[i], templates[j]] = [templates[j], templates[i]];
  }

  for (const t of templates) {
    const w = t.when || {};

    // Per-template random gate (optional — omit for equal distribution)
    if (w.random != null && Math.random() > w.random) continue;

    let match = true;
    for (const [key, cond] of Object.entries(w)) {
      if (key === 'random') continue;
      const raw = values[key];
      if (!_evalCondition(raw, cond)) { match = false; break; }
    }
    if (!match) continue;

    return t.text.replace(/\{(\w+)\}/g, (_, k) => {
      const raw = values[k];
      if (raw === undefined || raw === null) return `{${k}}`;
      const m = measureByKey[k];
      return _fmtValue(raw, m?.measure, m?.fmt);
    }) + (t.encore || '');
  }
  return section.default || BOOT_FALLBACK_TEXT;
}

// ── render ──────────────────────────────────────────────────────────────────

function bootLogoHtml(bubbleText) {
  const forced = Number(window.__squidBootLogoVariant);
  const variant = Number.isInteger(forced) && forced >= 0 && forced <= 2
    ? forced
    : Math.floor(Math.random() * 3);
  const logo = '<img class="boot-logo-icon" src="/favicon.png" alt="" />';
  const bubble = bubbleText ? `<div class="boot-logo-bubble">${bubbleText}</div>` : '';
  if (variant === 0) {
    return `<pre class="boot-art">${BOOT_LOGO_ART}</pre>` +
      `<div class="boot-art-mobile">${BOOT_LOGO_MOBILE}</div>`;
  }
  if (variant === 1) {
    return `<div class="boot-logo-lockup boot-logo-squid-only">${logo}</div>`;
  }
  return `<div class="boot-logo-lockup boot-logo-talking-squid">${logo}${bubble}</div>`;
}

async function showBootBanner() {
  try {
    const res = await fetch('/health');
    if (!res.ok) return;
    const data = await res.json();
    _providerMetadata = data.providers || {};
    _harnessMetadata = {};
    for (const h of (data.harnesses || [])) _harnessMetadata[h.id] = h;
    updateSettingsFromHealth(data);
    checkForSquidUpdate(data.version);
    await updateActiveQuotaGauge();
    if (activeQuotaBackend) fetchQuotaForBackend(activeQuotaBackend);
    const bootTime = data.boot_time ? fmtTime(data.boot_time) : '';

    // Dynamic boot message
    const insightsPromise = fetchInsights();
    const streak = getStreak();
    const now = new Date();
    const insights = await insightsPromise;
    const values = (await resolveInsightMeasures(insights)) || {};
    // Always seed clock sources even if measures block is absent
    if (!values.streak) values.streak = streak;
    if (values.hour === undefined) values.hour = now.getHours();
    if (values.dow === undefined) values.dow = now.getDay();
    const bubbleText = pickBootMessage(insights, values);

    const el = document.createElement('div');
    el.className = 'boot-banner';
    el.innerHTML = bootLogoHtml(bubbleText) +
      `<div class="boot-meta">AgentSquid${bootTime ? `  ·  started ${bootTime}` : ''}</div>` +
      (!navigator.onLine ? `<div class="boot-offline">no internet — LLM calls will fail</div>` : '');
    messages.appendChild(el);

    const anyAvailable = (data.harnesses || []).some(h => h.installed);
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
        <div class="no-agent-restart">Then restart squid.</div>`;
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
    el.innerHTML = bootLogoHtml(BOOT_FALLBACK_TEXT) +
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
  popup._forStatsTurnsEl = null;
  _setCtxPopupLayer(popup, spanEl.closest(CTX_POPUP_MODAL_SCOPE) ? 'modal-context-popup' : 'page-context-popup');

  const sid    = spanEl.dataset.sessionId || '';
  const flowRunId = spanEl.dataset.flowRunId || '';
  const msgId  = spanEl.dataset.msgId || '';
  const cwd    = spanEl.dataset.cwd || '';
  const agent  = spanEl.dataset.agent || '';
  const mem    = spanEl.dataset.mem === 'true';
  const topic  = spanEl.dataset.topic || '';
  const sessionTurnCount = parseInt(spanEl.dataset.sessionTurnCount || '0', 10) || 0;
  const pinIds = JSON.parse(spanEl.dataset.pinnedIds || '[]');
  const hasTrace = spanEl.dataset.hasTrace === 'true';

  let html = '';
  if (msgId) {
    html += `<div class="ctx-popup-row ctx-popup-jump-row" data-jump-msg-id="${escapeHtml(msgId)}"><span class="ctx-popup-key">message</span><span class="ctx-popup-val ctx-popup-link" title="/jump ${escapeHtml(msgId)}">#${escapeHtml(msgId)}</span></div>`;
  }
  if (flowRunId) {
    html += `<div class="ctx-popup-row ctx-popup-jump-row" data-jump-flow-run-id="${escapeHtml(flowRunId)}"><span class="ctx-popup-key">flow run</span><span class="ctx-popup-val ctx-popup-link" title="/jump flow:${escapeHtml(flowRunId)}">${escapeHtml(flowRunId)}</span></div>`;
  }
  if (sid || cwd) {
    if (sid && agent) {
      html += `<div class="ctx-popup-row ctx-popup-session-row" data-session-id="${escapeHtml(sid)}" data-agent="${escapeHtml(agent)}" data-cwd="${escapeHtml(cwd)}" data-msg-id="${escapeHtml(msgId)}"><span class="ctx-popup-key">session</span><span class="ctx-popup-val ctx-popup-link" title="Open raw session log">${sid}</span></div>`;
    } else {
      html += `<div class="ctx-popup-row"><span class="ctx-popup-key">session</span><span class="ctx-popup-val">${sid}</span></div>`;
    }
    if (sessionTurnCount > 0) {
      html += `<div class="ctx-popup-row"><span class="ctx-popup-key">session context</span><span class="ctx-popup-val">${sessionTurnCount} turn${sessionTurnCount !== 1 ? 's' : ''}</span></div>`;
    }
    if (cwd) html += `<div class="ctx-popup-row ctx-popup-cwd-row" data-cwd="${escapeHtml(cwd)}"><span class="ctx-popup-key">cwd</span><span class="ctx-popup-val ctx-popup-link" title="Open in file viewer">${cwd}</span></div>`;
  } else if (sessionTurnCount > 0) {
    html += `<div class="ctx-popup-row"><span class="ctx-popup-key">session context</span><span class="ctx-popup-val">${sessionTurnCount} turn${sessionTurnCount !== 1 ? 's' : ''}</span></div>`;
  }
  if (mem && topic) {
    if (html) html += `<div class="ctx-popup-divider"></div>`;
    html += `<div class="ctx-popup-row ctx-popup-mem-row" data-topic="${topic}">
      <span class="ctx-popup-key">memory</span>
      <span class="ctx-popup-val ctx-popup-link">#${topic}</span>
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
  if (msgId && hasTrace) {
    if (html) html += `<div class="ctx-popup-divider"></div>`;
    html += `<div class="ctx-popup-row ctx-popup-trace-row" data-msg-id="${msgId}">
      <span class="ctx-popup-key">trace</span>
      <span class="ctx-popup-val ctx-popup-link">thoughts</span>
    </div>`;
  }
  if (topic) html += `<div id="ctx-roots-section"></div>`;
  if (!html) html = `<div class="ctx-popup-row"><span class="ctx-popup-key">${spanEl.textContent.trim()}</span></div>`;

  popup.innerHTML = html;
  popup.classList.add('open');

  popup.querySelectorAll('.ctx-popup-jump-row .ctx-popup-link').forEach(link => {
    link.addEventListener('click', e => {
      e.stopPropagation();
      const row = link.closest('.ctx-popup-jump-row');
      const jumpMsgId = row.dataset.jumpMsgId ? parseInt(row.dataset.jumpMsgId, 10) : null;
      const jumpFlowRunId = row.dataset.jumpFlowRunId || null;
      jumpToMessage(jumpMsgId, jumpFlowRunId);
    });
  });

  const memRow = popup.querySelector('.ctx-popup-mem-row');
  if (memRow) {
    memRow.addEventListener('click', () => {
      _closeCtxPopup(popup);
      openMemoryEditor(memRow.dataset.topic);
    });
  }

  const cwdRow = popup.querySelector('.ctx-popup-cwd-row');
  if (cwdRow) {
    cwdRow.addEventListener('click', () => {
      _closeCtxPopup(popup);
      openFileViewer(cwdRow.dataset.cwd);
    });
  }

  const sessionRow = popup.querySelector('.ctx-popup-session-row');
  if (sessionRow) {
    sessionRow.addEventListener('click', async () => {
      const valEl = sessionRow.querySelector('.ctx-popup-val');
      const original = valEl.textContent;
      const { sessionId, agent: rowAgent, cwd: rowCwd, msgId: rowMsgId } = sessionRow.dataset;
      try {
        const q = new URLSearchParams({ agent: rowAgent, session_id: sessionId, cwd: rowCwd || '' });
        const { path, entries, source, turns } = await fetch(`/session-log?${q}`).then(r => r.json());
        if (path) {
          const text = await fetch('/localfile?' + new URLSearchParams({ path, _t: Date.now() })).then(r => r.text());
          _closeCtxPopup(popup);
          openLogViewer(sessionId, _parseJsonlEntries(text), undefined, turns, rowMsgId, path);
        } else if (entries) {
          _closeCtxPopup(popup);
          openLogViewer(
            `opencode ${sessionId}`, entries,
            source === 'opencode-sqlite'
              ? "Reconstructed from opencode's SQLite session log (opencode.db) — opencode has no per-session transcript file the way other harnesses do."
              : undefined,
            turns, rowMsgId, path,
          );
        } else {
          valEl.textContent = 'no local transcript found';
          setTimeout(() => { valEl.textContent = original; }, 1500);
        }
      } catch {
        valEl.textContent = 'lookup failed';
        setTimeout(() => { valEl.textContent = original; }, 1500);
      }
    });
  }

  popup.querySelectorAll('.ctx-popup-pin[data-pin-id]').forEach(row => {
    row.addEventListener('click', () => {
      _closeCtxPopup(popup);
      openMsgModal(parseInt(row.dataset.pinId));
    });
  });

  const traceRow = popup.querySelector('.ctx-popup-trace-row');
  if (traceRow) {
    traceRow.addEventListener('click', () => {
      _closeCtxPopup(popup);
      openTraceModal(parseInt(traceRow.dataset.msgId, 10));
    });
  }

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
        row.className = 'ctx-popup-row ctx-popup-root-row';
        row.dataset.root = root;
        row.innerHTML = `<span class="ctx-popup-key">${i === 0 ? 'roots' : ''}</span><span class="ctx-popup-val ctx-popup-link" title="Open in file viewer">${escapeHtml(root)}</span>`;
        row.addEventListener('click', () => {
          _closeCtxPopup(popup);
          openFileViewer(root);
        });
        frag.appendChild(row);
      });
      placeholder.replaceWith(frag);
    }).catch(() => {
      const placeholder = document.getElementById('ctx-roots-section');
      if (placeholder) placeholder.remove();
    });
  }

  _positionCtxPopupNearAnchor(popup, spanEl);
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

function makeTraceToolBlock(tool) {
  const block = document.createElement('div');
  block.className = 'tool-block trace-tool-block';
  const toggle = document.createElement('button');
  toggle.className = 'tool-toggle';
  toggle.textContent = toolLabel(tool);
  const body = document.createElement('div');
  body.className = 'tool-body';
  const scroll = document.createElement('div');
  scroll.className = 'diff-scroll';
  const pre = document.createElement('pre');
  pre.className = 'trace-tool-pre';
  pre.textContent = JSON.stringify(tool, null, 2);
  scroll.appendChild(pre);
  body.appendChild(scroll);
  toggle.addEventListener('click', () => block.classList.toggle('tool-expanded'));
  block.appendChild(toggle);
  block.appendChild(body);
  return block;
}

// One-shot parse of an SSE response body into an ordered event list. Mirrors
// the live chat stream parser's line handling (event:/data: fields, multi-line
// data joined by \n on blank-line boundaries) but returns everything at once
// instead of driving UI state incrementally.
function _parseSseEvents(text) {
  const events = [];
  let eventName = null;
  let dataLines = [];
  const flush = () => {
    if (eventName !== null || dataLines.length) events.push({ type: eventName, data: dataLines.join('\n') });
    eventName = null;
    dataLines = [];
  };
  for (const raw of text.split('\n')) {
    const line = raw.replace(/\r$/, '');
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      const field = line.slice(5);
      dataLines.push(field.startsWith(' ') ? field.slice(1) : field);
    } else if (line === '') {
      flush();
    }
  }
  flush();
  return events;
}

// Reconstruct the true chronological interleaving of narration and tool calls
// from the run_events log (shared seq counter across event types — see
// agent/topic_queue.py) rather than the collapsed status_raw/context fields.
function renderTraceTimeline(events, container) {
  const timeline = [];
  let narration = '';
  const finalText = events
    .filter(ev => ev.type === null || ev.type === 'text')
    .map(ev => ev.data || '')
    .join('');
  const flushNarration = () => {
    const text = _traceStatusText(narration, finalText);
    if (text) timeline.push({ kind: 'status', text });
    narration = '';
  };
  for (const ev of events) {
    if (ev.type === 'status') {
      narration += ev.data;
    } else if (ev.type === 'tool') {
      flushNarration();
      try { timeline.push({ kind: 'tool', tool: JSON.parse(ev.data) }); } catch {}
    }
    // 'text' (default/unnamed event — the final answer), 'stats', 'error', 'done' aren't part of the trace.
  }
  flushNarration();
  if (!timeline.length) return false;
  for (const item of timeline) {
    if (item.kind === 'status') {
      const section = document.createElement('div');
      section.className = 'trace-status';
      section.textContent = item.text.trim();
      container.appendChild(section);
    } else {
      container.appendChild(makeTraceToolBlock(item.tool));
    }
  }
  return true;
}

function renderTraceBody(msg, container) {
  const statusRaw = _traceStatusText(msg.status_raw, msg.content || '');
  if (statusRaw) {
    const section = document.createElement('div');
    section.className = 'trace-status';
    section.textContent = statusRaw;
    container.appendChild(section);
  }
  let tools = [];
  try {
    const parsed = typeof msg.context === 'string' ? JSON.parse(msg.context) : msg.context;
    if (Array.isArray(parsed)) tools = parsed;
  } catch {}
  if (tools.length) {
    const toolsWrap = document.createElement('div');
    toolsWrap.className = 'tool-calls';
    tools.forEach(tool => toolsWrap.appendChild(makeTraceToolBlock(tool)));
    container.appendChild(toolsWrap);
  }
  if (!statusRaw && !tools.length) {
    container.innerHTML = '<div class="ctx-popup-row" style="padding:1rem"><span class="ctx-popup-key">No trace recorded</span></div>';
  }
}

// Turns a harness's raw .jsonl transcript into the same {kind, type,
// time_created, data} shape _opencode_session_transcript_rows() returns for
// opencode's SQLite rows, so both feed the same openLogViewer(). Field names
// vary per harness (claudecode/codex/pi use timestamp+type, cursor uses
// role+message with no timestamp) -- this reads what's there and falls back
// to line position for entries with no parseable time.
function _parseJsonlEntries(text) {
  const entries = [];
  let seq = 0;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    seq += 1;
    let data;
    try { data = JSON.parse(line); } catch { data = { _unparsed: raw }; }
    const type = (data && typeof data === 'object') ? (data.type || data.role || null) : null;
    let time_created = null;
    if (data && typeof data === 'object' && data.timestamp) {
      const t = Date.parse(data.timestamp);
      if (!Number.isNaN(t)) time_created = t;
    }
    entries.push({ kind: 'line', type, seq, time_created, data });
  }
  return entries;
}

function _logEntryLabel(entry) {
  const parts = [];
  if (entry.kind && entry.kind !== 'line') parts.push(entry.kind);
  if (entry.type) parts.push(entry.type);
  parts.push(entry.time_created ? new Date(entry.time_created).toLocaleString() : `line ${entry.seq}`);
  return parts.join(' · ');
}

// Tags each entry with which squid turn it falls in, using /session-log's
// `turns` (squid's own per-turn timestamps for this session_id -- the raw
// log itself, jsonl or SQLite, has no notion of squid's turn grouping).
// `turns` must be sorted ascending by time_ms, `entries` in chronological
// order (both already are, from the server for opencode and from file order
// for jsonl).
//
// Some jsonl line types carry no timestamp at all -- e.g. claudecode's
// ai-title/last-prompt/mode metadata lines -- and they're not rare: one real
// 42-turn session had 224 of out 1414 lines with no timestamp, scattered
// throughout, not just at the start. Treating "no timestamp" as "before the
// first turn" made that divider reappear constantly instead of once. Since
// entries are already chronological, an untimed entry belongs with whatever
// turn came immediately before it in the file. Only entries before the
// *first* timestamped entry (no prior turn to inherit) stay untagged --
// that's the one legitimate "before first turn" case.
function _assignTurnIndices(entries, turns) {
  if (!turns || !turns.length) return;
  let lastTurn = null;
  for (const e of entries) {
    if (e.time_created == null) { e._turn = lastTurn; continue; }
    let turn = null;
    for (const t of turns) {
      if (t.time_ms <= e.time_created) turn = t.turn_index;
      else break;
    }
    e._turn = turn;
    lastTurn = turn;
  }
}

// A "turn" here is one squid prompt sent into this session through to
// squid's recorded reply -- everything between this divider and the next
// happened while the harness was processing that single prompt (which, for
// an agentic turn, can be dozens of internal tool calls -- see the entry
// count jump across dividers on a turn that did a lot of file/tool work).
const _TURN_DIVIDER_HINT = 'One squid turn: everything below, up to the next divider, happened while the harness processed this one prompt (may include many internal tool calls).';

function _makeTurnDivider(turnIndex, turnsByIndex) {
  const div = document.createElement('div');
  div.className = 'log-viewer-turn-divider';
  div.title = _TURN_DIVIDER_HINT;
  div.dataset.turn = turnIndex == null ? '' : String(turnIndex);
  if (turnIndex == null) {
    div.textContent = 'before first squid turn';
  } else {
    const t = turnsByIndex.get(turnIndex);
    const when = t ? new Date(t.time_ms).toLocaleString() : '';
    div.textContent = `squid turn ${turnIndex}${when ? ' · ' + when : ''}`;
  }
  return div;
}

// Case-insensitive JSON haystack for search, computed once and cached on the
// entry -- a big session (one real one had 1414 log lines) re-stringifying
// every entry's full data on every search keystroke is real, avoidable jank.
function _entrySearchText(entry) {
  if (entry._searchText == null) entry._searchText = JSON.stringify(entry.data).toLowerCase();
  return entry._searchText;
}

function _entryMatches(entry, q) {
  return _entrySearchText(entry).includes(q)
    || (entry.type || '').toLowerCase().includes(q)
    || (entry.kind || '').toLowerCase().includes(q);
}

// entry._expanded persists across re-renders (list.innerHTML is rebuilt on
// every keystroke/reverse-toggle) so a block a user opened -- or one the
// search jumped to -- doesn't silently re-collapse out from under them.
function _makeLogEntryBlock(entry, isMatch) {
  const block = document.createElement('div');
  block.className = 'tool-block trace-tool-block';
  if (isMatch) block.classList.add('log-viewer-entry-match');
  const toggle = document.createElement('button');
  toggle.className = 'tool-toggle';
  toggle.textContent = _logEntryLabel(entry);
  const body = document.createElement('div');
  body.className = 'tool-body';
  const scroll = document.createElement('div');
  scroll.className = 'diff-scroll';
  const pre = document.createElement('pre');
  pre.className = 'trace-tool-pre';
  scroll.appendChild(pre);
  body.appendChild(scroll);
  // Pretty-printing (indent=2) is deferred to first expand, not done for
  // every entry on every render -- most entries in a large session are
  // never opened, so paying for that upfront (and again on every re-render
  // from search/reverse) is wasted work.
  const expand = () => {
    if (!pre.textContent) pre.textContent = JSON.stringify(entry.data, null, 2);
    entry._expanded = true;
    block.classList.add('tool-expanded');
  };
  if (entry._expanded) expand();
  toggle.addEventListener('click', () => {
    if (block.classList.contains('tool-expanded')) {
      entry._expanded = false;
      block.classList.remove('tool-expanded');
    } else {
      expand();
    }
  });
  block.appendChild(toggle);
  block.appendChild(body);
  return block;
}

// Shared viewer for both opencode's SQLite-reconstructed session rows and a
// harness's raw .jsonl transcript (once normalized by _parseJsonlEntries) --
// same entry shape, same search/reverse-order toolbar either way.
function openLogViewer(title, entries, bannerText, turns, currentMsgId, sourcePath) {
  const modal = document.getElementById('msg-modal');
  const titleEl = document.getElementById('msg-modal-title');
  const body = document.getElementById('msg-modal-body');
  if (sourcePath) {
    titleEl.innerHTML = '';
    const link = document.createElement('a');
    link.textContent = title;
    link.className = 'msg-modal-title-link';
    link.title = `Open raw transcript: ${sourcePath}`;
    link.addEventListener('click', () => openFileViewer(sourcePath));
    titleEl.appendChild(link);
  } else {
    titleEl.textContent = title;
  }
  body.innerHTML = '';
  _assignTurnIndices(entries, turns);
  const hasTurns = !!(turns && turns.length);
  const turnsByIndex = new Map((turns || []).map(t => [t.turn_index, t]));

  if (bannerText) {
    const banner = document.createElement('div');
    banner.className = 'trace-status';
    banner.textContent = bannerText;
    body.appendChild(banner);
  }

  const toolbar = document.createElement('div');
  toolbar.className = 'log-viewer-toolbar';
  const searchInput = document.createElement('input');
  searchInput.type = 'text';
  searchInput.placeholder = 'Search…';
  searchInput.className = 'log-viewer-search';
  const prevMatchBtn = document.createElement('button');
  prevMatchBtn.type = 'button';
  prevMatchBtn.className = 'log-viewer-nav-btn';
  prevMatchBtn.innerHTML = '<span class="material-symbols-outlined" aria-hidden="true">keyboard_arrow_up</span>';
  prevMatchBtn.title = 'Previous match (Shift+Enter)';
  prevMatchBtn.setAttribute('aria-label', 'Previous match');
  prevMatchBtn.hidden = true;
  const nextMatchBtn = document.createElement('button');
  nextMatchBtn.type = 'button';
  nextMatchBtn.className = 'log-viewer-nav-btn';
  nextMatchBtn.innerHTML = '<span class="material-symbols-outlined" aria-hidden="true">keyboard_arrow_down</span>';
  nextMatchBtn.title = 'Next match (Enter)';
  nextMatchBtn.setAttribute('aria-label', 'Next match');
  nextMatchBtn.hidden = true;
  const reverseBtn = document.createElement('button');
  reverseBtn.type = 'button';
  reverseBtn.className = 'log-viewer-reverse-btn';
  const countEl = document.createElement('span');
  countEl.className = 'log-viewer-count';
  toolbar.appendChild(searchInput);
  toolbar.appendChild(prevMatchBtn);
  toolbar.appendChild(nextMatchBtn);
  toolbar.appendChild(reverseBtn);
  toolbar.appendChild(countEl);
  body.appendChild(toolbar);

  const list = document.createElement('div');
  list.className = 'log-viewer-list';
  body.appendChild(list);
  const scrollBody = list.parentElement;
  const scrollToEl = el => {
    const elRect = el.getBoundingClientRect();
    const toolbarRect = toolbar.getBoundingClientRect();
    scrollBody.scrollTop += elRect.top - toolbarRect.bottom - 6;
  };

  // Newest-first by default: the biggest turn number (the most recent one)
  // is what you see first, matching the button label below at all times --
  // the label always names the order currently on screen, not an action.
  let reversed = true;
  const updateReverseLabel = () => { reverseBtn.textContent = reversed ? 'Newest first' : 'Oldest first'; };

  // Search is find-in-page, not a filter: matching entries are highlighted
  // and stepped through with the surrounding log still visible for context,
  // rather than hiding everything else. A common keyword (e.g. "error")
  // shouldn't force every hit open at once, so only `currentEntry` -- the
  // one currently jumped to -- gets auto-expanded; other matches just get an
  // accent-colored label (.log-viewer-entry-match) until you step to them.
  let currentEntry = null;
  const render = () => {
    const q = searchInput.value.trim().toLowerCase();
    const ordered = reversed ? entries.slice().reverse() : entries;
    const matches = q ? ordered.filter(e => _entryMatches(e, q)) : [];
    if (q) {
      if (!currentEntry || !matches.includes(currentEntry)) currentEntry = matches[0] || null;
      if (currentEntry) currentEntry._expanded = true;
    } else {
      currentEntry = null;
    }
    const matchSet = q ? new Set(matches) : null;
    list.innerHTML = '';
    if (!ordered.length) {
      const empty = document.createElement('div');
      empty.className = 'ctx-popup-row';
      empty.style.padding = '1rem';
      empty.textContent = 'No entries.';
      list.appendChild(empty);
    } else {
      let lastTurn;
      for (const entry of ordered) {
        if (hasTurns && entry._turn !== lastTurn) {
          lastTurn = entry._turn;
          list.appendChild(_makeTurnDivider(lastTurn, turnsByIndex));
        }
        const el = _makeLogEntryBlock(entry, matchSet?.has(entry));
        if (entry === currentEntry) el.classList.add('log-viewer-current-match');
        list.appendChild(el);
      }
    }
    if (q) highlightTextNodes(list, [q]);
    prevMatchBtn.hidden = nextMatchBtn.hidden = !q;
    if (q) {
      const idx = matches.indexOf(currentEntry);
      countEl.textContent = matches.length ? `match ${idx + 1} of ${matches.length}` : 'No matches';
      const currentEl = list.querySelector('.log-viewer-current-match');
      if (currentEl) scrollToEl(currentEl);
    } else {
      countEl.textContent = `${entries.length} entries`;
    }
  };

  const stepMatch = delta => {
    const q = searchInput.value.trim().toLowerCase();
    if (!q) return;
    const ordered = reversed ? entries.slice().reverse() : entries;
    const matches = ordered.filter(e => _entryMatches(e, q));
    if (!matches.length) return;
    const cur = currentEntry ? matches.indexOf(currentEntry) : -1;
    const idx = cur === -1 ? (delta > 0 ? 0 : matches.length - 1) : (cur + delta + matches.length) % matches.length;
    currentEntry = matches[idx];
    render();
  };

  // Debounced -- rebuilding the whole list (list.innerHTML = '' + rebuild)
  // on every keystroke is the other big cost on a large session; 150ms
  // feels instant while typing but collapses a fast typist's keystrokes
  // into one rebuild instead of one per character.
  let searchDebounce = null;
  searchInput.addEventListener('input', () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(render, 150);
  });
  searchInput.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    stepMatch(e.shiftKey ? -1 : 1);
  });
  prevMatchBtn.addEventListener('click', () => stepMatch(-1));
  nextMatchBtn.addEventListener('click', () => stepMatch(1));
  reverseBtn.addEventListener('click', () => {
    reversed = !reversed;
    updateReverseLabel();
    render();
  });

  updateReverseLabel();
  render();
  modal.classList.add('open');

  // Jump straight to the turn the popup was opened from, instead of always
  // landing on the newest turn regardless of which message you came from.
  if (currentMsgId != null && turns) {
    const openedTurn = turns.find(t => String(t.msg_id) === String(currentMsgId));
    if (openedTurn) {
      const divider = list.querySelector(`[data-turn="${openedTurn.turn_index}"]`);
      if (divider) {
        scrollToEl(divider);
        divider.classList.add('log-viewer-turn-divider-target');
        setTimeout(() => divider.classList.remove('log-viewer-turn-divider-target'), 2000);
      }
    }
  }
}

async function openTraceModal(msgId) {
  const modal = document.getElementById('msg-modal');
  const title = document.getElementById('msg-modal-title');
  const body  = document.getElementById('msg-modal-body');
  title.textContent = `Message #${msgId} · thought trace`;
  body.innerHTML = '<div class="ctx-popup-row" style="padding:1rem"><span class="ctx-popup-key">Loading…</span></div>';
  modal.classList.add('open');
  try {
    const eventsText = await fetch(`/chat/${msgId}/events?after_seq=-1`).then(r => r.text());
    const events = _parseSseEvents(eventsText);
    body.innerHTML = '';
    const rendered = renderTraceTimeline(events, body);
    if (!rendered) {
      // Older messages predate the run_events log — fall back to the collapsed
      // status_raw/context fields, which have no cross-type ordering.
      const msg = await fetch(`/chat/${msgId}/status`).then(r => r.json());
      renderTraceBody(msg, body);
    }
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
const _pendingSessionInjectedIds = {}; // `${topic}@${agent|_}` -> pinned IDs submitted to an in-flight session turn
const _pendingSessionMemoryRevisions = {}; // `${topic}@${agent|_}` -> topic memory revision submitted to an in-flight session turn
const _pendingSessionAttachedFiles = {}; // `${topic}@${agent|_}` -> attached file paths submitted to an in-flight session turn
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

function getAttachedFiles() {
  try { return JSON.parse(localStorage.getItem('attachedFiles') || '[]'); } catch { return []; }
}
function setAttachedFiles(items) { localStorage.setItem('attachedFiles', JSON.stringify(items)); }
function getAttachedFilesInSession() {
  try { return JSON.parse(localStorage.getItem('attachedFilesInSession') || '{}'); } catch { return {}; }
}
function setAttachedFilesInSession(map) { localStorage.setItem('attachedFilesInSession', JSON.stringify(map)); }
function _removeAttachedFilePath(path) {
  if (!path) return false;
  let changed = false;
  const items = getAttachedFiles();
  const nextItems = items.filter(f => f.path !== path);
  if (nextItems.length !== items.length) {
    setAttachedFiles(nextItems);
    changed = true;
  }
  const inSession = getAttachedFilesInSession();
  Object.keys(inSession).forEach(key => {
    const next = (inSession[key] || []).filter(p => p !== path);
    if (next.length !== (inSession[key] || []).length) {
      inSession[key] = next;
      changed = true;
    }
  });
  if (changed) setAttachedFilesInSession(inSession);
  Object.keys(_pendingSessionAttachedFiles).forEach(key => {
    const next = (_pendingSessionAttachedFiles[key] || []).filter(p => p !== path);
    if (next.length !== (_pendingSessionAttachedFiles[key] || []).length) {
      _pendingSessionAttachedFiles[key] = next;
      changed = true;
    }
  });
  return changed;
}
function removeAttachedFilePath(path) {
  const changed = _removeAttachedFilePath(path);
  if (changed) {
    updatePinCount();
    if (pinPanel.classList.contains('open')) renderPinPanel();
  }
  return changed;
}
async function pruneMissingAttachedFiles() {
  const files = getAttachedFiles();
  const paths = [...new Set(files.map(f => f.path).filter(Boolean))];
  if (!paths.length) return [];
  try {
    const res = await fetch('/localfile/check-paths', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths }),
    });
    if (!res.ok) return [];
    const data = await res.json();
    const missing = (data.paths || [])
      .filter(item => !item.exists || !item.is_file)
      .map(item => item.path);
    if (!missing.length) return [];
    missing.forEach(path => _removeAttachedFilePath(path));
    updatePinCount();
    if (pinPanel.classList.contains('open')) renderPinPanel();
    return missing;
  } catch {
    return [];
  }
}
function addAttachedFile(path) {
  const items = getAttachedFiles();
  if (items.some(f => f.path === path)) return;
  setAttachedFiles([...items, { path, name: path.split('/').filter(Boolean).pop() || path }]);
  updatePinCount();
  if (pinPanel.classList.contains('open')) renderPinPanel();
}
function openFileUploadPicker() {
  const savedPath = localStorage.getItem('squid_fv_last_path_picker') || null;
  openFileViewer(savedPath, null, null, null, null, { onPick: addAttachedFile }, { persistKey: 'picker' });
}
function clearAttachedFiles() {
  setAttachedFiles([]);
  updatePinCount();
  if (pinPanel.classList.contains('open')) renderPinPanel();
}
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

function _memoryRootTopic(topic) {
  return String(topic || 'default').toLowerCase().split('.', 1)[0] || 'default';
}

function _memoryCacheKeys(topic, data = null) {
  const keys = [];
  const add = value => {
    const key = String(value || '').toLowerCase();
    if (key && !keys.includes(key)) keys.push(key);
  };
  add(topic);
  add(data?.topic);
  add(_memoryRootTopic(topic));
  return keys;
}

function _knownMemoryMeta(topic) {
  return _memoryCacheKeys(topic).map(key => _memoryCache[key]).find(Boolean) || null;
}

function _cacheMemoryMeta(topic, data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return data;
  const meta = { ...data, loading: false };
  _memoryCacheKeys(topic, data).forEach(key => { _memoryCache[key] = meta; });
  return meta;
}

function _clearMemoryRouteState(topic, data = null) {
  _memoryCacheKeys(topic, data).forEach(key => {
    _clearMemorySelectionOverridesForTopic(key);
    _clearSessionLookupCacheForTopic(key);
  });
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
  const cached = _knownMemoryMeta(topic);
  if (cached) return cached;
  const placeholder = { topic, exists: false, content: '', path: `~/.squid/context/topics/${topic}/memory.md`, loading: true };
  _memoryCache[topic] = placeholder;
  fetch(`/topics/${encodeURIComponent(topic)}/memory`)
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      // A newer write (save, code-roots decision, or a send's own fresh fetch) may have
      // already replaced the cache entry while this request was in flight — don't clobber it.
      if (data && _memoryCache[topic] === placeholder) _cacheMemoryMeta(topic, data);
      updatePinCount();
      if (pinPanel.classList.contains('open')) renderPinPanel();
    })
    .catch(() => { if (_memoryCache[topic] === placeholder) placeholder.loading = false; });
  return _memoryCache[topic];
}

async function fetchMemoryMeta(topic) {
  const data = await fetch(`/topics/${encodeURIComponent(topic)}/memory`).then(r => r.json());
  if (!data || typeof data !== 'object' || Array.isArray(data)) return data;
  return _cacheMemoryMeta(topic, data);
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
  const oldRevision = _memoryRevision(_knownMemoryMeta(topic));
  const res = await fetch(`/topics/${encodeURIComponent(topic)}/memory/squid/code-roots`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Save failed (${res.status})`);
  if (_memoryRevision(data) !== oldRevision) {
    _clearMemoryRouteState(topic, data);
  }
  const meta = _cacheMemoryMeta(topic, data);
  updatePinCount();
  if (pinPanel.classList.contains('open')) renderPinPanel();
  return meta;
}


function _getSessionMeta(topic, agent) {
  if (!agent) return { session_id: null, cwd: null, loading: false };
  const key = `${topic}@${agent}`;
  const cachedSid = _sessionIds[`${topic}@${agent}`];
  if (cachedSid && Object.prototype.hasOwnProperty.call(_sessionTurnCounts, cachedSid)) {
    return { session_id: cachedSid, cwd: null, session_turn_count: _sessionTurnCounts[cachedSid], loading: false };
  }
  if (_sessionLookupCache[key]) return _sessionLookupCache[key];
  _sessionLookupCache[key] = { session_id: null, cwd: null, loading: true };
  fetch(`/topics/${encodeURIComponent(topic)}/session?agent=${encodeURIComponent(agent)}`)
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data || typeof data !== 'object' || Array.isArray(data)) return;
      _sessionLookupCache[key] = { ...(data || { session_id: null, cwd: null }), loading: false };
      if (data.session_turn_count != null) {
        _setKnownSessionTurnCount(topic, agent, data.session_turn_count, data.session_id || null);
      }
      if (data?.session_id) {
        _sessionIds[`${topic}@${agent}`] = data.session_id;
        _rememberSessionMemoryRevision(topic, agent, data);
        if (data.injected_ids?.length) {
          const inj = getInjectedInto();
          inj[data.session_id] = [...new Set([...(inj[data.session_id] || []), ...data.injected_ids])];
          setInjectedInto(inj);
        }
      }
      if (!_acStashedForNav && stickyChip && !stickyChip.adhoc && !stickyChip.route &&
          stickyChip.topic === topic && stickyChip.agent === agent) {
        const count = _knownSessionTurnCount(topic, agent);
        if (count != null) _renderChipTurnCount(count, { allowZero: true });
      }
      updatePinCount();
      if (pinPanel.classList.contains('open')) renderPinPanel();
      updateInContextMarkers();
      evaluateAdvisory();
    })
    .catch(() => { _sessionLookupCache[key].loading = false; });
  return _sessionLookupCache[key];
}

async function refreshRouteTurnCounts(route, opts = {}) {
  const targets = _routePersistentSessionTargets(route);
  if (!targets.length) return;
  const force = !!opts.force;
  const minAgeMs = opts.minAgeMs ?? 5000;
  await Promise.all(targets.map(async target => {
    const key = `${target.topic}@${target.agent}`;
    if (!force && Object.prototype.hasOwnProperty.call(_sessionTurnCountsByRoute, key)) return;
    const now = Date.now();
    if (force && now - (_routeTurnCountRefreshAt[key] || 0) < minAgeMs) return;
    _routeTurnCountRefreshAt[key] = now;
    try {
      const res = await fetch(`/topics/${encodeURIComponent(target.topic)}/session?agent=${encodeURIComponent(target.agent)}`);
      if (!res.ok) return;
      const data = await res.json();
      if (!data || typeof data !== 'object' || Array.isArray(data)) return;
      _sessionLookupCache[key] = { ...(data || { session_id: null, cwd: null }), loading: false };
      if (data?.session_id) {
        _sessionIds[key] = data.session_id;
        _rememberSessionMemoryRevision(target.topic, target.agent, data);
      } else {
        delete _sessionIds[key];
      }
      if (data && data.session_turn_count != null) {
        _setKnownSessionTurnCount(target.topic, target.agent, data.session_turn_count, data.session_id || null);
      }
    } catch {}
  }));
}

function _topicMemoryState(target = null) {
  const { topic, agent, adhoc } = target || _currentContextTarget();
  const meta = _getMemoryMeta(topic);
  const session = _getSessionMeta(topic, agent);
  const exists = !!(meta.exists && (meta.content || '').trim());
  const key = _memoryOverrideKey(topic, agent, adhoc);
  const injectedKey = _memoryInjectedKey(topic, agent);
  const revision = _memoryRevision(meta);
  const pendingRevision = !adhoc ? _pendingSessionMemoryRevisions[injectedKey] : null;
  const pending = !!pendingRevision && pendingRevision === revision;
  const injectedRevision = _memoryInjectedInto[injectedKey];
  const injected = !adhoc && !!injectedRevision && injectedRevision === revision;
  const stale = !adhoc && !!injectedRevision && injectedRevision !== revision;
  const defaultSelected = exists && !pending && (adhoc || stale || (!injectedRevision && !session.loading && !session.session_id));
  const selected = exists && (_memorySelectionOverrides[key] ?? defaultSelected);
  return { topic, agent, adhoc, meta, session, exists, selected, key, injected, stale, pending, revision };
}

async function _topicMemoryStateForSend(topic, agent, adhoc) {
  const meta = await fetch(`/topics/${encodeURIComponent(topic)}/memory`)
    .then(r => r.ok ? r.json() : null)
    .catch(() => null);
  if (meta) _cacheMemoryMeta(topic, meta);
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
  const pending = _pendingSessionInjectedIds[taKey] || [];
  return items
    .filter(item => {
      const sameSession = item.session_id && currentSid && item.session_id === currentSid;
      if (sameSession && !adhoc) return false;
      if (adhoc && lookback === 0) return true;
      if (!adhoc && pending.includes(item.id)) return false;
      if (currentSid && (injected[currentSid] || []).includes(item.id)) return false;
      return true;
    })
    .map(item => item.id);
}

function _attachedFilesState(target = null) {
  const { topic, agent, adhoc } = target || _currentContextTarget();
  const files = getAttachedFiles();
  if (adhoc) return { selected: files, pending: [], saved: [] };
  const taKey = `${topic}@${agent || '_'}`;
  const currentSid = _sessionIds[taKey] || null;
  const inSession = getAttachedFilesInSession();
  const sent = new Set(currentSid ? (inSession[currentSid] || []) : []);
  const pending = new Set(_pendingSessionAttachedFiles[taKey] || []);
  return {
    selected: files.filter(f => !sent.has(f.path) && !pending.has(f.path)),
    pending: files.filter(f => pending.has(f.path)),
    saved: files.filter(f => sent.has(f.path)),
  };
}

function updatePinCount() {
  const broadcastAgents = stickyChip?.broadcastAgents || null;
  let selectedCount, pendingCount;
  if (broadcastAgents) {
    // Origin Broadcast (multi-head): heads are independent turns, each with
    // its own topic/agent, so a true union-count across heads would mix
    // unrelated pins into one number that looks precise but isn't (see
    // discussion — a summed count can't say *which* head needs what without
    // a per-head breakdown UI we don't have). Simpler and honest: this is a
    // presence flag, not a count — at most +1 for "some head has a pin
    // selected," at most +1 for "some head has memory selected," same as
    // the single-target formula below but clamped to boolean per category.
    const asTarget = a => ({ topic: a.topic, agent: a.agent, adhoc: !!a.fresh });
    const anyPinSelected = broadcastAgents.some(a => _injectablePinnedIds(a.topic, a.agent, !!a.fresh, 0).length > 0);
    const anyMemSelected = broadcastAgents.some(a => _topicMemoryState(asTarget(a)).selected);
    selectedCount = (anyPinSelected ? 1 : 0) + (anyMemSelected ? 1 : 0);
    const anyPinPending = broadcastAgents.some(a => !a.fresh && (_pendingSessionInjectedIds[`${a.topic}@${a.agent || '_'}`] || []).length > 0);
    const anyMemPending = broadcastAgents.some(a => _topicMemoryState(asTarget(a)).pending);
    pendingCount = (anyPinPending ? 1 : 0) + (anyMemPending ? 1 : 0);
  } else {
    const { topic, agent, adhoc, lookback } = _currentContextTarget();
    const taKey = `${topic}@${agent || '_'}`;
    const selectedIds = [
      ..._activeLookbackItems(adhoc, lookback).map(item => item.id),
      ..._injectablePinnedIds(topic, agent, adhoc, lookback),
    ];
    const pendingIds = !adhoc ? (_pendingSessionInjectedIds[taKey] || []) : [];
    const memoryState = _topicMemoryState();
    selectedCount = new Set(selectedIds).size + (memoryState.selected ? 1 : 0);
    pendingCount = new Set(pendingIds).size + (memoryState.pending ? 1 : 0);
  }
  const attachedState = _attachedFilesState();
  const pinnedCount = getPinnedItems().length;
  selectedCount += attachedState.selected.length;
  pendingCount += attachedState.pending.length;
  const savedPins = pinnedCount + attachedState.saved.length;
  const badgeCount = selectedCount || pendingCount || savedPins;
  pinCountEl.textContent = badgeCount || '';
  pinCountEl.classList.toggle('visible', badgeCount > 0);
  pinBtn.classList.toggle('has-context', selectedCount > 0);
  pinBtn.classList.toggle('has-context-pending', selectedCount === 0 && pendingCount > 0);
  pinBtn.classList.toggle('has-saved-pins', selectedCount === 0 && pendingCount === 0 && savedPins > 0);
}

function _pinTagStr(item) {
  if (item.context_tag === 'previous_step_output') return '<previous_step_output>';
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
  const pending = _pendingSessionInjectedIds[chipTaKey] || [];

  // Skip only if the pin is from the exact current session — --resume already covers it
  if (sameSession && !isAdhoc) {
    return { text: 'in session · skip', cls: 'pin-status-session' };
  }

  // Already available in this topic@agent session via a previous injected turn
  // Only meaningful for !N lookback where model retains prior context; !0 is always fresh
  const chipLookback = parsed.adhoc ? parsed.lookback : (stickyChip?.lookback ?? 0);
  if (currentSid && (injected[currentSid] || []).includes(item.id) && !(isAdhoc && chipLookback === 0))
    return { text: 'in session · skip', cls: 'pin-status-session' };

  if (!isAdhoc && pending.includes(item.id))
    return { text: 'sending', cls: 'pin-status-session' };

  return { text: 'will inject', cls: 'pin-status-inject' };
}

function _memoryStatus(state) {
  if (state.meta.loading || state.session.loading) return { text: 'checking', cls: 'pin-status-session' };
  if (!state.exists) return { text: 'no memory', cls: 'pin-status-empty' };
  if (state.pending) return { text: 'sending', cls: 'pin-status-session' };
  if (state.selected) return { text: 'will inject', cls: 'pin-status-inject' };
  if (state.injected) return { text: 'in session · skip', cls: 'pin-status-session' };
  if (!state.adhoc && state.session.session_id) return { text: 'in session · skip', cls: 'pin-status-session' };
  return { text: 'skipped', cls: 'pin-status-done' };
}

function _attachedFileStatus(file) {
  const state = _attachedFilesState();
  if (state.pending.some(f => f.path === file.path)) return { text: 'sending', cls: 'pin-status-session' };
  if (state.saved.some(f => f.path === file.path)) return { text: 'in session · skip', cls: 'pin-status-session' };
  return { text: 'will inject', cls: 'pin-status-inject' };
}

function renderPinPanel() {
  const items = getPinnedItems();
  const listEl = document.getElementById('pin-panel-list');
  const clearBtn = document.getElementById('pin-panel-clear');
  const clearFilesBtn = document.getElementById('pin-panel-clear-files');
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
  const memoryToggleActive = memoryState.selected || memoryState.pending;
  const memoryToggleText = memoryToggleActive ? 'On' : (memoryState.exists ? 'Off' : 'Add');
  html += `<div class="memory-item">
    <span class="pin-item-tag">#${escapeHtml(memoryState.topic)}</span>
    <span class="memory-item-preview" data-memory-edit="1">Topic memory · ${escapeHtml(preview)}</span>
    <span class="memory-item-status ${memoryStatus.cls}">${memoryStatus.text}</span>
    <button class="pin-item-toggle${memoryToggleActive ? ' active' : ''}" data-memory-toggle="1" type="button"${memoryState.pending ? ' disabled' : ''}>${memoryToggleText}</button>
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
      html += `<div class="${itemCls}" data-open-id="${item.id}">
        <span class="pin-item-tag">${escapeHtml(tag)}</span>
        <span class="pin-item-preview">${escapeHtml(preview)}</span>
        <span class="pin-item-status ${st.cls}">${st.text}</span>
        ${control}
      </div>`;
    });
  } else {
    html += '<div style="padding:0.5rem 0.8rem;color:#484858;font-size:0.78em">No pins yet.<br>Click <svg width="10" height="11" viewBox="0 0 32 32" fill="currentColor" aria-hidden="true" style="vertical-align:-0.1em"><path d="M25 21v1H8v-1l2-2L11 4L9 2V1h15v1l-2 2l1 15l2 2zM16 31h1l1-8h-3l1 8z"/></svg> on any response to add it.</div>';
  }

  const attachedFiles = getAttachedFiles();
  if (clearFilesBtn) clearFilesBtn.disabled = attachedFiles.length === 0;
  if (attachedFiles.length) {
    html += `<div class="pin-section-label">Files</div>`;
    attachedFiles.forEach(file => {
      const st = _attachedFileStatus(file);
      const lastSlash = file.path.lastIndexOf('/');
      const dirPart = lastSlash >= 0 ? file.path.slice(0, lastSlash + 1) : '';
      const namePart = lastSlash >= 0 ? file.path.slice(lastSlash + 1) : file.path;
      html += `<div class="pin-item" data-file-path="${escapeHtml(file.path)}">
        <span class="pin-item-tag"><span class="material-symbols-outlined" aria-hidden="true">description</span></span>
        <span class="pin-item-preview pin-item-preview-path" title="${escapeHtml(file.path)}"><span class="pin-item-path-dir">${escapeHtml(dirPart)}</span><span class="pin-item-path-name">${escapeHtml(namePart)}</span></span>
        <span class="pin-item-status ${st.cls}">${st.text}</span>
        <button class="pin-item-remove" data-file-remove="${escapeHtml(file.path)}" type="button">✕</button>
      </div>`;
    });
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
  listEl.querySelectorAll('.pin-item[data-open-id]').forEach(row => {
    row.querySelectorAll('.pin-item-tag, .pin-item-preview').forEach(el => {
      el.addEventListener('click', () => openMsgModal(parseInt(row.dataset.openId, 10)));
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
  listEl.querySelectorAll('.pin-item[data-file-path]').forEach(row => {
    row.querySelectorAll('.pin-item-preview').forEach(el => {
      el.addEventListener('click', () => openFileViewer(row.dataset.filePath));
    });
  });
  listEl.querySelectorAll('[data-file-remove]').forEach(btn => {
    btn.addEventListener('mousedown', e => {
      e.preventDefault();
      removeAttachedFilePath(btn.dataset.fileRemove);
    });
  });
  listEl.querySelectorAll('.pin-item-remove[data-id]').forEach(btn => {
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
    const data = await fetch(`/topics/${encodeURIComponent(topic)}/memory/squid/seed`, { method: 'POST' }).then(r => r.json());
    _cacheMemoryMeta(topic, data);
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
  const oldRevision = _memoryRevision(_knownMemoryMeta(topic));
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
      _clearMemoryRouteState(topic, data);
    }
    _cacheMemoryMeta(topic, data);
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
  pruneMissingAttachedFiles();
}
function closePinPanel({ restoreFocus = false } = {}) {
  pinPanel.classList.remove('open');
  if (restoreFocus) input.focus({ preventScroll: true });
}

// ── Bookmarks ─────────────────────────────────────────────────────────────

let _bookmarkItems = [];
let _bookmarkIds = new Set();
let _badResponseIds = new Set();

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
          body: JSON.stringify({ msg_id: item.id }) });
      }
    }
    if (legacy.length) {
      localStorage.removeItem('bookmarkedItems');
      const res2 = await fetch('/bookmarks');
      if (res2.ok) { const d = await res2.json(); _bookmarkItems = d.items || []; _bookmarkIds = new Set(_bookmarkItems.map(i => i.id)); }
    }
  } catch { /* ignore — falls back to empty */ }
}

async function _apiToggleBookmark(msgId, topic, agent) {
  if (_bookmarkIds.has(msgId)) {
    _bookmarkIds.delete(msgId);
    _bookmarkItems = _bookmarkItems.filter(i => i.id !== msgId);
    fetch(`/bookmarks/${msgId}`, { method: 'DELETE' }).catch(() => {});
    return false;
  } else {
    _bookmarkIds.add(msgId);
    _bookmarkItems = [{ id: msgId, topic, agent: agent || null, saved_at: new Date().toISOString() }, ..._bookmarkItems];
    fetch('/bookmarks', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ msg_id: msgId }) }).catch(() => {});
    return true;
  }
}

let bookmarkOnlyHistory = false;
let badOnlyHistory = false;

function updateBookmarkButton() {
  bookmarkBtn.setAttribute('aria-pressed', bookmarkOnlyHistory ? 'true' : 'false');
  bookmarkBtn.title = bookmarkOnlyHistory ? 'show full thread: /bm' : 'bookmarks only: /bm';
}

function toggleBookmarkOnlyHistory() {
  bookmarkOnlyHistory = !bookmarkOnlyHistory;
  if (bookmarkOnlyHistory && promptOnlyHistory) {
    promptOnlyHistory = false;
    updatePromptOnlyButton();
  }
  updateBookmarkButton();
  _updateFilterBadge();
  if (searchActive) {
    document.querySelectorAll('.search-result-item, .date-divider').forEach(el => el.remove());
    loadSearchResults();
  } else {
    reloadHistory(historyFilter);
  }
}

function toggleBadOnlyHistory() {
  badOnlyHistory = !badOnlyHistory;
  if (badOnlyHistory && promptOnlyHistory) {
    promptOnlyHistory = false;
    updatePromptOnlyButton();
  }
  _updateFilterBadge();
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
    const nowBookmarked = await _apiToggleBookmark(msgId, topic, agent);
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

async function _loadBadResponses() {
  try {
    const res = await fetch('/annotations?kind=bad_response');
    if (!res.ok) return;
    const data = await res.json();
    _badResponseIds = new Set((data.items || []).map(i => i.msg_id));
    document.querySelectorAll('.msg-bad-response-btn[data-msg-id]').forEach(btn => {
      const marked = _badResponseIds.has(parseInt(btn.dataset.msgId, 10));
      btn.classList.toggle('marked-bad', marked);
      btn.title = marked ? 'Unmark bad response' : 'Mark bad response';
      btn.setAttribute('aria-pressed', marked ? 'true' : 'false');
    });
  } catch { /* ignore — falls back to unmarked */ }
}

async function _apiToggleBadResponse(msgId, topic, agent) {
  if (_badResponseIds.has(msgId)) {
    _badResponseIds.delete(msgId);
    fetch(`/annotations/bad_response/${msgId}`, { method: 'DELETE' }).catch(() => {});
    return false;
  }
  _badResponseIds.add(msgId);
  fetch('/annotations', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      msg_id: msgId,
      kind: 'bad_response',
      payload: {},
    }),
  }).catch(() => {});
  return true;
}

function addBadResponseButton(bubbleEl, msgId, topic, agent, marked = false) {
  const existing = bubbleEl.querySelector(`.msg-bad-response-btn[data-msg-id="${msgId}"]`);
  if (existing) return existing;
  if (marked) _badResponseIds.add(msgId);
  const isMarked = marked || _badResponseIds.has(msgId);
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'msg-bad-response-btn';
  btn.dataset.msgId = String(msgId);
  btn.title = isMarked ? 'Unmark bad response' : 'Mark bad response';
  btn.setAttribute('aria-label', btn.title);
  btn.setAttribute('aria-pressed', isMarked ? 'true' : 'false');
  btn.innerHTML = `<span class="material-symbols-outlined">thumb_down</span>`;
  btn.classList.toggle('marked-bad', isMarked);
  btn.addEventListener('click', async e => {
    e.stopPropagation();
    const nowMarked = await _apiToggleBadResponse(msgId, topic, agent);
    btn.classList.toggle('marked-bad', nowMarked);
    btn.title = nowMarked ? 'Unmark bad response' : 'Mark bad response';
    btn.setAttribute('aria-label', btn.title);
    btn.setAttribute('aria-pressed', nowMarked ? 'true' : 'false');
  });
  bubbleEl.appendChild(btn);
  return btn;
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

function addDeepDiveButton(bubbleEl, topic, agent, adhoc, statsEl, msgId, timestamp) {
  if (!statsEl) statsEl = bubbleEl.nextElementSibling;
  if (!statsEl || !statsEl.classList.contains('stats')) return;
  const existing = statsEl.querySelector('.stats-deep-dive-btn');
  if (!existing) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'stats-deep-dive-btn';
    btn.title = 'Deep Dive by Turns';
    btn.innerHTML = `<span class="material-symbols-outlined">ssid_chart</span>`;
    btn.addEventListener('click', e => {
      e.stopPropagation();
      if (msgId) _navigateToAnchoredStats(topic, agent, adhoc, msgId, timestamp);
      else _navigateToDeepDive(topic, agent, adhoc);
    });
    statsEl.appendChild(btn);
  }
  return statsEl.querySelector('.stats-deep-dive-btn');
}

function addReplyButton(bubbleEl, topic, agent, adhoc) {
  if (bubbleEl.querySelector('.msg-reply-btn')) return;
  bubbleEl.classList.add('has-reply-btn');
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'msg-reply-btn';
  btn.title = 'Reply';
  btn.innerHTML = `<span class="material-symbols-outlined">reply</span>`;
  btn.addEventListener('click', e => {
    e.stopPropagation();
    replyToMessage(bubbleEl, topic, agent, !!adhoc);
  });
  // Anchored via position:absolute (bottom-right corner of the bubble, see
  // .msg-reply-btn) — not part of the .stats row, so it doesn't inherit that
  // row's shrink-wrapped width and stays pinned to the bubble's actual edge
  // regardless of how long the stats text or topic tag are.
  bubbleEl.appendChild(btn);
}

function blinkTopicChip() {
  if (!topicChipEl) return;
  topicChipEl.classList.remove('reply-blink');
  void topicChipEl.offsetWidth; // restart the animation if it's already running
  topicChipEl.classList.add('reply-blink');
}
topicChipEl?.addEventListener('animationend', e => {
  if (e.animationName === 'topic-chip-reply-blink') topicChipEl.classList.remove('reply-blink');
});

// Reply is one click: land on the message's route if we're elsewhere, or —
// if already on that route but the message belongs to an earlier session —
// pull it into context the same way the pin button does. Same route, same
// session: nothing to do, you're already there.
async function replyToMessage(bubbleEl, topic, agent, adhoc) {
  const msgTopic = topic || 'default';
  const msgAgent = agent || null;
  const msgAdhoc = !!adhoc;

  const active = await resolveEffectiveComposerRoute();
  const sameRoute = !active.route
    && (active.topic || 'default') === msgTopic
    && (active.agent || null) === msgAgent
    && !!active.adhoc === msgAdhoc;

  if (!sameRoute) {
    // Point the composer at this message's route only — reply shouldn't
    // also filter the visible history, unlike clicking the topic chip does.
    setTopicChip(msgTopic, msgAgent, msgAdhoc);
  }
  // Blink either way: on a route switch it confirms the new target, and on
  // an already-matching route it's the only feedback the click did anything
  // — otherwise a same-route reply looks like a no-op.
  blinkTopicChip();
  if (!sameRoute) return;

  const msgId = bubbleEl.dataset.msgId ? parseInt(bubbleEl.dataset.msgId, 10) : null;
  const sid = bubbleEl.dataset.sessionId || null;
  const currentSid = (!msgAdhoc && msgAgent) ? (_sessionIds[`${msgTopic}@${msgAgent}`] || null) : null;
  // No id or no session on the message — can't tell it apart from the active
  // one, so do nothing rather than guess. But a missing currentSid (e.g. the
  // route's session was just cleared) isn't "unknown" — it means there is no
  // active session, so a message that does have a sid is necessarily stale
  // and should be pinned, not skipped.
  if (msgId == null || !sid || sid === currentSid) return;

  const pinned = getPinnedItems();
  if (pinned.find(i => i.id === msgId)) return;
  const text = _messageBodyText(bubbleEl).slice(0, 300);
  setPinnedItems([...pinned, { id: msgId, topic: msgTopic, agent: msgAgent, session_id: sid, content: text }]);
  bubbleEl.querySelector(`.msg-pin-btn[data-msg-id="${msgId}"]`)?.classList.add('pinned');
  bubbleEl.classList.add('pinned-sel');
  updatePinCount();
  if (pinPanel.classList.contains('open')) renderPinPanel();
}

async function _navigateToDeepDive(topic, agent, adhoc) {
  const state = _deepDiveStatsState();
  if (topic) state.dimensions.topic = { mode: 'selected', values: [topic] };
  if (agent) state.dimensions.agent = { mode: 'selected', values: [agent] };
  state.dimensions.session_type = { mode: 'selected', values: [adhoc ? 'adhoc' : 'session'] };

  _activeStatsPresetId = null;
  _applyStatsState(state);

  if (!_statsPresetsLoaded) {
    _statsPresetsLoaded = true;
    await _loadStatsPresets({ applyDefault: false });
  }
  _renderStatsPresetControls();

  if (currentView !== 'stats') {
    navigateView('stats');
  } else {
    loadStats();
  }
}

async function _navigateToAnchoredStats(topic, agent, adhoc, msgId, timestamp) {
  const state = _deepDiveStatsState();
  if (topic) state.dimensions.topic = { mode: 'selected', values: [topic] };
  if (agent) state.dimensions.agent = { mode: 'selected', values: [agent] };
  state.dimensions.session_type = { mode: 'selected', values: [adhoc ? 'adhoc' : 'session'] };
  // Keep Deep Dive's normal range and anchor that window at this turn's end
  // time, then flag its row once the table renders.
  state.time = { ...state.time, anchor: timestamp || new Date().toISOString() };

  _activeStatsPresetId = null;
  _applyStatsState(state);
  _statsHighlightMsgId = msgId;

  if (!_statsPresetsLoaded) {
    _statsPresetsLoaded = true;
    await _loadStatsPresets({ applyDefault: false });
  }
  _renderStatsPresetControls();

  if (currentView !== 'stats') {
    navigateView('stats');
  } else {
    loadStats();
  }
}

function initPin() {
  pinBtn.addEventListener('click', () => {
    if (pinPanel.classList.contains('open')) closePinPanel();
    else openPinPanel();
  });
  document.getElementById('pin-panel-close').addEventListener('click', closePinPanel);
  document.getElementById('pin-panel-clear').addEventListener('click', clearPinnedItems);
  document.getElementById('pin-panel-clear-files').addEventListener('click', clearAttachedFiles);
  document.getElementById('pin-panel-upload').addEventListener('click', openFileUploadPicker);
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
  document.getElementById('restart-modal-secondary').addEventListener('click', () => {
    closeRestartModal(document.getElementById('restart-modal')._secondaryRestartResult);
  });
  document.getElementById('restart-modal-confirm').addEventListener('click', () => {
    closeRestartModal(document.getElementById('restart-modal')._confirmRestartResult);
  });
  document.getElementById('restart-modal').addEventListener('mousedown', e => {
    if (e.target === document.getElementById('restart-modal')) closeRestartModal(false);
  });
  document.getElementById('agent-session-modal-close').addEventListener('click', () => closeAgentSessionModal(false));
  document.getElementById('agent-session-cancel').addEventListener('click', () => closeAgentSessionModal(false));
  document.getElementById('agent-session-confirm').addEventListener('click', () => closeAgentSessionModal(true));
  document.getElementById('agent-session-modal').addEventListener('mousedown', e => {
    if (e.target === document.getElementById('agent-session-modal')) closeAgentSessionModal(false);
  });
  document.getElementById('topic-delete-modal-close').addEventListener('click', closeTopicDeleteModal);
  document.getElementById('topic-delete-cancel').addEventListener('click', closeTopicDeleteModal);
  document.getElementById('topic-delete-confirm').addEventListener('click', confirmTopicDelete);
  document.getElementById('topic-delete-modal').addEventListener('mousedown', e => {
    if (e.target === document.getElementById('topic-delete-modal')) closeTopicDeleteModal();
  });
  document.getElementById('preset-name-modal-close').addEventListener('click', () => _closePresetNameModal(null));
  document.getElementById('preset-name-cancel').addEventListener('click', () => _closePresetNameModal(null));
  document.getElementById('preset-name-confirm').addEventListener('click', () => { _submitPresetName(); });
  document.getElementById('preset-name-overwrite').addEventListener('click', () => { _overwritePresetFromModal(); });
  document.getElementById('preset-name-input').addEventListener('input', e => {
    document.getElementById('preset-name-confirm').disabled = !e.target.value.trim();
    _setPresetNameModalError('');
  });
  document.getElementById('preset-name-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      _submitPresetName();
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
_loadBadResponses();
document.getElementById('search-bar-clear').addEventListener('click', clearSearch);
document.getElementById('chip-prompts-btn')?.addEventListener('click', togglePromptOnlyHistory);
updatePromptOnlyButton();
updateSearchButton();

function formatFilterCommand(state) {
  if (state.flow_route) return `/f ${state.flow_route}`;
  let scope = '';
  if (state.topic) scope = '#' + state.topic;
  if (state.agent) {
    scope += '@' + state.agent;
    if (state.adhoc === true) scope += '!';
    else if (state.adhoc === null) scope += '*';
  }
  return scope ? `/f ${scope}` : '/f reset';
}

function hideAdvisory() {
  sessionAdvisoryEl.hidden = true;
  _advisoryDismissKey = null;
}

function evaluateAdvisory() {
  if (!stickyChip || stickyChip.adhoc || stickyChip.route) { hideAdvisory(); return; }
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

function restoreStashedInput() {
  if (commandEditRestore === null) return;
  input.value = commandEditRestore;
  commandEditRestore = null;
  input.setSelectionRange(input.value.length, input.value.length);
  resizeComposer();
}

function stashComposerAndEdit(command) {
  const prev = input.value.trim();
  commandEditRestore = prev && prev !== command ? prev : null;
  if (prev && prev !== command && !prev.startsWith('/')) {
    recordPrompt(prev);
    const hint = document.createElement('span');
    hint.className = 'restore-hint';
    if (isMobileViewport()) {
      hint.classList.add('restore-hint-tappable');
      const icon = document.createElement('span');
      icon.className = 'restore-hint-icon material-symbols-outlined';
      icon.textContent = 'archive';
      hint.append(icon, ' tap to restore');
      hint.addEventListener('click', () => { restoreStashedInput(); input.focus(); });
    } else {
      hint.textContent = '↑ to restore';
    }
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
  if (state.flow_route) {
    cmd += state.flow_route + ' ';
  } else if (state.explicitAll || state.topic) {
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
  if (e.key === 'Escape' && closeEscSurfaces()) {
    e.preventDefault();
  }
});
document.addEventListener('click', e => {
  if (!acEl.contains(e.target) && e.target !== input) hideAutocomplete();
  if (!pinPanel.contains(e.target) && !pinBtn.contains(e.target)) closePinPanel();
  const ctxPopup = document.getElementById('ctx-popup');
  const statsTurnPopup = document.getElementById('stats-turn-popup');
  const inSecondary = e.target.closest('#msg-modal, #memory-modal, #topic-delete-modal, #agent-session-modal, #preset-name-modal');
  const inCtxPopup = ctxPopup?.contains(e.target);
  const secondaryOpen = document.getElementById('msg-modal')?.classList.contains('open')
    || document.getElementById('memory-modal')?.classList.contains('open')
    || document.getElementById('topic-delete-modal')?.classList.contains('open')
    || document.getElementById('agent-session-modal')?.classList.contains('open')
    || document.getElementById('preset-name-modal')?.classList.contains('open');
  if (ctxPopup && !inCtxPopup && !e.target.closest('.user-ctx') && !inSecondary && !secondaryOpen) {
    ctxPopup.classList.remove('open');
  }
  if (statsTurnPopup && !statsTurnPopup.contains(e.target) && !e.target.closest('.stats-turn-link') && !inCtxPopup && !inSecondary && !secondaryOpen) {
    statsTurnPopup.classList.remove('open');
    statsTurnPopup._forStatsTurnsEl = null;
  }
  if (
    !procStatusPopup.contains(e.target) && e.target !== procStatusBtn && !procStatusBtn.contains(e.target)
    && Date.now() - procPopupOpenedAt > 300
  ) {
    procStatusPopup.classList.remove('open');
  }
});
// ── file viewer ───────────────────────────────────────────────────────────────

const _TEXT_EXTS = new Set(['txt','md','py','js','mjs','cjs','ts','jsx','tsx','json','yaml','yml',
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

function _isMarkdownPath(path) {
  const ext = (path.split('.').pop() || '').toLowerCase();
  return ext === 'md' || ext === 'markdown';
}

function _languageExtForPath(path) {
  const parts = (path || '').split('.').filter(Boolean);
  let ext = (parts.pop() || '').toLowerCase();
  while (_GENERIC_SUFFIXES.has(ext) && parts.length) ext = (parts.pop() || '').toLowerCase();
  return ext;
}

let _fvNavigate = null;
let _fvHandlePopState = null;

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
  const savedPath = localStorage.getItem('squid_fv_last_path_browser') || null;
  openFileViewer(savedPath, null, null, document.getElementById('view-files'), null, null, { persistKey: 'browser' });
}

const FV_MAX_PREVIEW_BYTES = 2 * 1024 * 1024;

function openFileViewer(initialPath, initialLine, initialEndLine, inlineContainer = null, initialChangedLines = null, pickOpts = null, viewerOpts = null) {
  document.getElementById('file-modal')?.remove();
  _fvNavigate = null;
  _fvHandlePopState = null;

  const isInline = !!inlineContainer;

  const navHistory = [{ path: initialPath, line: initialLine, endLine: initialEndLine }];
  let historyIdx = 0;
  let path = initialPath;
  let line = initialLine;
  let endLine = initialEndLine;
  let changedLines = initialChangedLines;
  let pathKind = initialPath ? null : 'roots';
  let pathIsText = false;
  let fileText = '';
  let markdownPreview = false;
  let webPreview = false;
  let pushedFileViewerHistory = false;
  let setDirUploadStatus = null;
  const initialSearch = viewerOpts?.search || '';

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

  const fvIcon = name => `<span class="material-symbols-outlined" aria-hidden="true">${name}</span>`;
  const FV_ICON_CHEVRON_LEFT = fvIcon('chevron_left');
  const FV_ICON_CHEVRON_RIGHT = fvIcon('chevron_right');
  const FV_ICON_HOME = fvIcon('home');
  const FV_ICON_EXTERNAL_LINK = fvIcon('open_in_new');
  const FV_ICON_SOURCE = fvIcon('code');
  const FV_ICON_PENCIL = fvIcon('edit');
  const FV_ICON_HISTORY = fvIcon('history');
  const FV_ICON_COPY = fvIcon('content_copy');
  const FV_ICON_CHECK = fvIcon('check');
  const FV_ICON_SAVE = fvIcon('save');
  const FV_ICON_DISCARD = fvIcon('undo');
  const FV_ICON_CLOSE = fvIcon('close');
  const FV_ICON_RENAME = fvIcon('drive_file_rename');
  const FV_ICON_UPLOAD = fvIcon('upload_file');
  const FV_ICON_FILE_PLUS = fvIcon('note_add');
  const FV_ICON_FOLDER_PLUS = fvIcon('create_new_folder');
  const FV_ICON_DELETE = fvIcon('delete');

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
  const homeBtn = document.createElement('button');
  homeBtn.className = 'fv-nav-btn';
  homeBtn.setAttribute('aria-label', 'File roots');
  homeBtn.title = 'File roots';
  homeBtn.innerHTML = FV_ICON_HOME;
  navBtns.append(backBtn, fwdBtn, homeBtn);

  const breadcrumb = document.createElement('div');
  breadcrumb.id = 'file-modal-breadcrumb';

  const actions = document.createElement('div');
  actions.className = 'fv-header-actions';
  const previewBtn = document.createElement('button');
  previewBtn.className = 'fv-action-btn';
  previewBtn.title = 'Preview';
  previewBtn.setAttribute('aria-label', 'Preview');
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
  const renameBtn = document.createElement('button');
  renameBtn.className = 'fv-action-btn';
  renameBtn.title = 'Rename';
  renameBtn.setAttribute('aria-label', 'Rename');
  renameBtn.innerHTML = FV_ICON_RENAME;
  renameBtn.hidden = true;
  const deleteBtn = document.createElement('button');
  deleteBtn.className = 'fv-action-btn fv-danger-btn';
  deleteBtn.title = 'Delete file';
  deleteBtn.setAttribute('aria-label', 'Delete file');
  deleteBtn.innerHTML = FV_ICON_DELETE;
  deleteBtn.hidden = true;
  const newFileBtn = document.createElement('button');
  newFileBtn.className = 'fv-action-btn';
  newFileBtn.title = 'Create file';
  newFileBtn.setAttribute('aria-label', 'Create file');
  newFileBtn.innerHTML = FV_ICON_FILE_PLUS;
  newFileBtn.hidden = true;
  const newFolderBtn = document.createElement('button');
  newFolderBtn.className = 'fv-action-btn';
  newFolderBtn.title = 'Add folder';
  newFolderBtn.setAttribute('aria-label', 'Add folder');
  newFolderBtn.innerHTML = FV_ICON_FOLDER_PLUS;
  newFolderBtn.hidden = true;
  const uploadBtn = document.createElement('button');
  uploadBtn.className = 'fv-action-btn';
  uploadBtn.title = 'Upload file';
  uploadBtn.setAttribute('aria-label', 'Upload file');
  uploadBtn.innerHTML = FV_ICON_UPLOAD;
  uploadBtn.hidden = true;
  if (pickOpts) uploadBtn.classList.add('fv-action-btn-attention');
  const uploadInput = document.createElement('input');
  uploadInput.type = 'file';
  uploadInput.multiple = true;
  uploadInput.hidden = true;
  const saveBtn = document.createElement('button');
  saveBtn.className = 'fv-action-btn fv-edit-action-btn fv-save-btn';
  saveBtn.type = 'button';
  saveBtn.title = 'Save';
  saveBtn.setAttribute('aria-label', 'Save');
  saveBtn.innerHTML = FV_ICON_SAVE;
  saveBtn.hidden = true;
  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'fv-action-btn fv-edit-action-btn fv-cancel-btn';
  cancelBtn.type = 'button';
  cancelBtn.title = 'Discard changes';
  cancelBtn.setAttribute('aria-label', 'Discard changes');
  cancelBtn.innerHTML = FV_ICON_DISCARD;
  cancelBtn.hidden = true;
  const copyBtn = document.createElement('button');
  copyBtn.className = 'fv-action-btn';
  copyBtn.title = 'Copy path';
  copyBtn.setAttribute('aria-label', 'Copy path');
  copyBtn.innerHTML = FV_ICON_COPY;
  const closeBtn = document.createElement('button');
  closeBtn.id = 'file-modal-close';
  closeBtn.title = 'Close';
  closeBtn.setAttribute('aria-label', 'Close');
  closeBtn.innerHTML = FV_ICON_CLOSE;
  actions.append(newFolderBtn, newFileBtn, uploadBtn, previewBtn, historyBtn, editBtn, renameBtn, deleteBtn, cancelBtn, saveBtn, copyBtn, closeBtn);

  // edit toolbar (shown only in edit mode, directly below the file viewer header)
  const editToolbar = document.createElement('div');
  editToolbar.className = 'fv-edit-toolbar';
  editToolbar.hidden = true;
  const editStatus = document.createElement('span');
  editStatus.className = 'fv-edit-status';
  const editTools = document.createElement('div');
  editTools.className = 'fv-edit-tools';
  const findInput = document.createElement('input');
  findInput.className = 'fv-edit-find';
  findInput.type = 'search';
  findInput.placeholder = 'Find';
  findInput.setAttribute('aria-label', 'Find in file');
  findInput.value = initialSearch;
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
  editToolbar.append(editTools, editStatus);

  header.append(navBtns, breadcrumb, actions);

  const pickHint = document.createElement('p');
  pickHint.className = pickOpts ? 'fv-roots-hint fv-roots-hint-pick' : 'fv-roots-hint';
  pickHint.appendChild(document.createTextNode('Select a file to attach it as context, or upload a new one. '));
  const pickHintTmpLink = document.createElement('a');
  pickHintTmpLink.href = '#';
  pickHintTmpLink.textContent = 'Go to /tmp for temporary uploads →';
  pickHintTmpLink.hidden = true;
  pickHintTmpLink.addEventListener('click', e => {
    e.preventDefault();
    navigate('/tmp');
  });
  pickHint.appendChild(pickHintTmpLink);
  pickHint.hidden = !pickOpts;
  if (pickOpts) {
    fetch('/localfile?' + new URLSearchParams({ path: '/tmp' }))
      .then(res => { if (res.ok) pickHintTmpLink.hidden = false; })
      .catch(() => {});
  }

  const body = document.createElement('div');
  body.id = 'file-modal-body';
  body.textContent = 'Loading…';

  const pathModal = document.createElement('div');
  pathModal.className = 'fv-path-modal';
  pathModal.hidden = true;
  const pathModalBox = document.createElement('form');
  pathModalBox.className = 'fv-path-modal-box';
  const pathModalHeader = document.createElement('div');
  pathModalHeader.className = 'fv-path-modal-header';
  const pathModalTitle = document.createElement('span');
  pathModalTitle.className = 'settings-label';
  const pathModalClose = document.createElement('button');
  pathModalClose.type = 'button';
  pathModalClose.className = 'fv-path-modal-close';
  pathModalClose.textContent = '✕';
  pathModalHeader.append(pathModalTitle, pathModalClose);
  const pathModalBody = document.createElement('div');
  pathModalBody.className = 'fv-path-modal-body';
  const pathModalInput = document.createElement('input');
  pathModalInput.type = 'text';
  pathModalInput.spellcheck = false;
  pathModalInput.autocomplete = 'off';
  pathModalInput.setAttribute('aria-label', 'Destination path');
  const pathModalHint = document.createElement('div');
  pathModalHint.className = 'fv-path-modal-hint';
  pathModalHint.textContent = 'Edit the filename or destination path.';
  const pathModalError = document.createElement('div');
  pathModalError.className = 'fv-path-modal-error';
  pathModalBody.append(pathModalInput, pathModalHint, pathModalError);
  const pathModalFooter = document.createElement('div');
  pathModalFooter.className = 'fv-path-modal-footer';
  const pathModalCancel = document.createElement('button');
  pathModalCancel.type = 'button';
  pathModalCancel.textContent = 'Cancel';
  const pathModalConfirm = document.createElement('button');
  pathModalConfirm.type = 'submit';
  pathModalConfirm.textContent = 'Rename';
  pathModalFooter.append(pathModalCancel, pathModalConfirm);
  pathModalBox.append(pathModalHeader, pathModalBody, pathModalFooter);
  pathModal.appendChild(pathModalBox);

  box.append(header, pickHint, editToolbar, body, uploadInput, pathModal);
  if (!isInline) {
    modal.appendChild(box);
    document.body.appendChild(modal);
  }

  // ── Navigation ───────────────────────────────────────────────────────────────
  function navigate(newPath, newLine = null, newEndLine = null) {
    exitEditMode();
    _historyOpen = false;
    historyBtn.style.opacity = '';
    editToolbar.hidden = true;
    navHistory.splice(historyIdx + 1);
    navHistory.push({ path: newPath, line: newLine, endLine: newEndLine });
    historyIdx = navHistory.length - 1;
    path = newPath; line = newLine; endLine = newEndLine;
    pathKind = path ? null : 'roots';
    pathIsText = false;
    fileText = '';
    markdownPreview = false;
    webPreview = false;
    if (viewerOpts?.persistKey) {
      const key = 'squid_fv_last_path_' + viewerOpts.persistKey;
      if (path) localStorage.setItem(key, path); else localStorage.removeItem(key);
    }
    updateNav();
    loadFile();
  }

  function updatePreviewButtonLabel() {
    if (_isMarkdownPath(path || '')) {
      previewBtn.title = markdownPreview ? 'Show Markdown source' : 'Preview Markdown';
      previewBtn.setAttribute('aria-label', previewBtn.title);
      previewBtn.innerHTML = markdownPreview ? FV_ICON_SOURCE : FV_ICON_EXTERNAL_LINK;
      return;
    }
    previewBtn.title = webPreview ? 'Show source' : 'Preview';
    previewBtn.setAttribute('aria-label', previewBtn.title);
    previewBtn.innerHTML = webPreview ? FV_ICON_SOURCE : FV_ICON_EXTERNAL_LINK;
  }

  function updateNav() {
    backBtn.disabled = historyIdx === 0;
    fwdBtn.disabled = historyIdx === navHistory.length - 1;
    homeBtn.disabled = !path;
    if (!path) {
      pathKind = 'roots';
      pathIsText = false;
      fileText = '';
      markdownPreview = false;
      webPreview = false;
      previewBtn.hidden = true;
      editBtn.hidden = true;
      historyBtn.hidden = true;
      renameBtn.hidden = true;
      newFileBtn.hidden = true;
      newFolderBtn.hidden = true;
      uploadBtn.hidden = true;
      copyBtn.hidden = true;
      deleteBtn.hidden = true;
      breadcrumb.textContent = 'Files';
      return;
    }
    copyBtn.hidden = false;
    const isFile = pathKind === 'file';
    const isDirectory = pathKind === 'directory';
    newFileBtn.hidden = !isDirectory;
    newFolderBtn.hidden = !isDirectory;
    uploadBtn.hidden = !isDirectory;
    renameBtn.hidden = false;
    deleteBtn.hidden = !isFile;
    previewBtn.hidden = !isFile || !_isWebPreviewPath(path);
    updatePreviewButtonLabel();
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
  function applyHistoryEntry(entry) {
    ({ path, line, endLine } = entry);
    webPreview = entry.preview === 'web';
    markdownPreview = false;
    pathKind = path ? null : 'roots';
    pathIsText = false;
    fileText = '';
    updateNav();
    loadFile();
  }

  function goFileHistory(delta) {
    const nextIdx = historyIdx + delta;
    if (nextIdx < 0 || nextIdx >= navHistory.length) return false;
    exitEditMode();
    historyIdx = nextIdx;
    applyHistoryEntry(navHistory[historyIdx]);
    return true;
  }

  backBtn.addEventListener('click', () => {
    if (historyIdx > 0) {
      if (webPreview && isMobileViewport() && history.state?.squidFilePreview) {
        history.back();
        return;
      }
      goFileHistory(-1);
    }
  });
  fwdBtn.addEventListener('click', () => {
    if (historyIdx < navHistory.length - 1) {
      goFileHistory(1);
    }
  });
  homeBtn.addEventListener('click', () => {
    if (path) navigate(null);
  });
  previewBtn.addEventListener('click', () => {
    if (_isMarkdownPath(path || '')) {
      markdownPreview = !markdownPreview;
      if (markdownPreview) {
        editToolbar.hidden = true;
        _renderMarkdownFilePreview(body, fileText);
      } else {
        _renderFileViewer(body, fileText, line, endLine, path, changedLines);
        showFileViewToolbar();
      }
      updatePreviewButtonLabel();
      return;
    }
    if (webPreview) {
      if (isMobileViewport() && history.state?.squidFilePreview) history.back();
      else goFileHistory(-1);
      return;
    }
    navHistory.splice(historyIdx + 1);
    navHistory.push({ path, line, endLine, preview: 'web' });
    historyIdx = navHistory.length - 1;
    webPreview = true;
    updateNav();
    editToolbar.hidden = true;
    _renderWebFilePreview(body, path);
    if (isMobileViewport() && history.pushState) {
      const state = (history.state && typeof history.state === 'object') ? history.state : {};
      history.pushState({ ...state, squidView: currentView, squidFilePreview: true }, '', location.href);
    }
  });
  copyBtn.addEventListener('click', () => {
    navigator.clipboard?.writeText(path).then(() => {
      copyBtn.innerHTML = FV_ICON_CHECK;
      setTimeout(() => { copyBtn.innerHTML = FV_ICON_COPY; }, 1500);
    });
  });

  let _pathModalResolve = null;
  function closePathModal(value = null) {
    pathModal.hidden = true;
    pathModalError.textContent = '';
    const resolve = _pathModalResolve;
    _pathModalResolve = null;
    if (resolve) resolve(value);
  }

  function openPathModal({ title, value, confirmLabel, hint, mode = 'input', danger = false }) {
    const isInput = mode === 'input';
    pathModalTitle.textContent = title;
    pathModalInput.hidden = !isInput;
    pathModalInput.value = isInput ? (value || '') : '';
    pathModalCancel.hidden = mode === 'alert';
    pathModalConfirm.textContent = confirmLabel || (mode === 'alert' ? 'OK' : mode === 'confirm' ? 'Confirm' : 'Save');
    pathModalConfirm.classList.toggle('fv-path-modal-confirm-danger', !!danger);
    pathModalHint.textContent = hint || (isInput ? 'Edit the filename or destination path.' : '');
    pathModalError.textContent = '';
    pathModal.hidden = false;
    requestAnimationFrame(() => {
      if (isInput) { pathModalInput.focus(); pathModalInput.select(); }
      else pathModalConfirm.focus();
    });
    return new Promise(resolve => { _pathModalResolve = resolve; });
  }

  pathModal.addEventListener('mousedown', e => {
    if (e.target === pathModal) closePathModal(null);
  });
  pathModalBox.addEventListener('mousedown', e => e.stopPropagation());
  pathModalClose.addEventListener('click', () => closePathModal(null));
  pathModalCancel.addEventListener('click', () => closePathModal(null));
  pathModalBox.addEventListener('submit', e => {
    e.preventDefault();
    if (pathModalInput.hidden) { closePathModal(true); return; }
    const value = pathModalInput.value.trim();
    if (!value) {
      pathModalError.textContent = 'Destination is required.';
      return;
    }
    closePathModal(value);
  });
  pathModalInput.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      e.preventDefault();
      closePathModal(null);
    }
  });

  async function createLocalChild(kind) {
    if (pathKind !== 'directory') return;
    const name = await openPathModal({
      title: kind === 'folder' ? 'New folder' : 'New file',
      value: '',
      confirmLabel: 'Create',
      hint: `Enter a name for the new ${kind === 'folder' ? 'folder' : 'file'}.`,
    });
    if (name == null) return;
    const trimmed = name.trim();
    if (!trimmed) return;
    const endpoint = kind === 'folder' ? '/localfile/create-folder' : '/localfile/create-file';
    const btn = kind === 'folder' ? newFolderBtn : newFileBtn;
    btn.disabled = true;
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ parent: path, name: trimmed }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Create failed');
      navigate(data.path);
    } catch (err) {
      await openPathModal({ title: 'Create failed', mode: 'alert', hint: err.message || 'Create failed' });
    } finally {
      btn.disabled = false;
    }
  }
  async function renameLocalPath(targetPath, currentName, afterRename = null) {
    const dest = await openPathModal({
      title: 'Rename or move',
      value: targetPath,
      confirmLabel: 'Rename',
    });
    if (dest == null) return;
    const trimmed = dest.trim();
    if (!trimmed || trimmed === targetPath || trimmed === currentName) return;
    const body = trimmed.includes('/') || trimmed.startsWith('~')
      ? { path: targetPath, to_path: trimmed }
      : { path: targetPath, name: trimmed };
    try {
      const res = await fetch('/localfile/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Rename failed');
      if (afterRename) afterRename(data.path);
      else navigate(data.path);
    } catch (err) {
      await openPathModal({ title: 'Rename failed', mode: 'alert', hint: err.message || 'Rename failed' });
    }
  }
  async function deleteLocalFile(targetPath, name, afterDelete = null) {
    const confirmed = await openPathModal({
      title: 'Delete',
      mode: 'confirm',
      confirmLabel: 'Delete',
      hint: `Delete ${name}? This can't be undone.`,
      danger: true,
    });
    if (!confirmed) return;
    try {
      const res = await fetch('/localfile/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: targetPath }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Delete failed');
      removeAttachedFilePath(data.path || targetPath);
      if (afterDelete) afterDelete();
      else navigate(targetPath.split('/').slice(0, -1).join('/') || null);
    } catch (err) {
      await openPathModal({ title: 'Delete failed', mode: 'alert', hint: err.message || 'Delete failed' });
    }
  }
  async function uploadLocalFiles(parentPath, files, setStatus) {
    const uploadFiles = Array.from(files || []).filter(file => file?.name);
    if (!uploadFiles.length) return [];
    const uploadedPaths = [];
    for (let i = 0; i < uploadFiles.length; i++) {
      const file = uploadFiles[i];
      setStatus?.(`Uploading ${i + 1}/${uploadFiles.length}: ${file.name}`);
      const res = await fetch('/localfile/upload?' + new URLSearchParams({ parent: parentPath, name: file.name }), {
        method: 'POST',
        body: file,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `Upload failed: ${file.name}`);
      uploadedPaths.push(data.path);
    }
    setStatus?.('Upload complete');
    loadFile();
    return uploadedPaths;
  }
  async function uploadAndMaybePick(parentPath, files, setStatus) {
    const uploadedPaths = await uploadLocalFiles(parentPath, files, setStatus);
    if (pickOpts && uploadedPaths.length) {
      uploadedPaths.forEach(p => pickOpts.onPick(p));
      // uploadLocalFiles() reloads the listing above, which replaces the status
      // node — read setDirUploadStatus fresh instead of the (now stale) setStatus.
      setDirUploadStatus?.(`Added ${uploadedPaths.length} file${uploadedPaths.length === 1 ? '' : 's'} to context`);
      setTimeout(closeModal, 900);
    }
  }
  newFolderBtn.addEventListener('click', () => createLocalChild('folder'));
  newFileBtn.addEventListener('click', () => createLocalChild('file'));
  uploadBtn.addEventListener('click', () => uploadInput.click());
  uploadInput.addEventListener('change', async () => {
    if (pathKind !== 'directory' || !uploadInput.files?.length) return;
    uploadBtn.disabled = true;
    try {
      await uploadAndMaybePick(path, uploadInput.files, setDirUploadStatus);
    } catch (err) {
      await openPathModal({ title: 'Upload failed', mode: 'alert', hint: err.message || 'Upload failed' });
    } finally {
      uploadInput.value = '';
      uploadBtn.disabled = false;
    }
  });
  renameBtn.addEventListener('click', () => {
    if (!path) return;
    renameLocalPath(path, path.split('/').filter(Boolean).pop() || path);
  });
  deleteBtn.addEventListener('click', () => {
    if (!path || pathKind !== 'file') return;
    deleteLocalFile(path, path.split('/').filter(Boolean).pop() || path);
  });
  const closeModal = ({ fromHistory = false } = {}) => {
    if (!fromHistory && pushedFileViewerHistory && isMobileViewport() && history.state?.squidFileViewer) {
      history.back();
      return;
    }
    if (!isInline) modal.remove();
    _fvNavigate = null;
    _fvHandlePopState = null;
  };
  closeBtn.hidden = isInline;
  closeBtn.addEventListener('click', closeModal);
  if (!isInline) {
    modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
    const escHandler = e => {
      if (e.key === 'Escape' && !pathModal.hidden) {
        closePathModal(null);
        return;
      }
      if (e.key === 'Escape') { closeModal(); document.removeEventListener('keydown', escHandler); }
    };
    document.addEventListener('keydown', escHandler);
  }

  // ── Edit mode ────────────────────────────────────────────────────────────────
  let _editOriginal = null;
  let _editFindPos = -1;
  let _editFindCleanup = null;
  let _editFindAnchor = null;

  function updateEditDirtyState() {
    const view = body._cmView;
    const dirty = !!view && view.state.doc.toString() !== _editOriginal;
    saveBtn.disabled = !dirty;
    editStatus.textContent = dirty ? 'Unsaved changes' : 'No changes';
  }

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

  function shouldFloatEditTools() {
    return false;
  }

  function showFindPopoverAtPos(pos) {
    if (!shouldFloatEditTools()) {
      dockFindTools();
      return;
    }
    const view = body._cmView;
    const coords = typeof view?.coordsAtPos === 'function' ? view.coordsAtPos(pos) : null;
    if (coords) showFindPopover(coords.right || coords.left, coords.bottom || coords.top);
    else showFindPopover();
  }

  function dockFindTools() {
    if (!editToolbar.contains(editTools)) editToolbar.insertBefore(editTools, editStatus);
    findPopover.hidden = true;
  }

  function teardownFindPopover() {
    if (_editFindCleanup) _editFindCleanup();
    _editFindCleanup = null;
    _editFindAnchor = null;
    dockFindTools();
  }

  function _fvHighlightViewLine(lineNo) {
    const rows = body.querySelectorAll('.fv-line');
    rows.forEach(r => r.classList.remove('fv-target'));
    const row = rows[lineNo - 1];
    if (row) {
      row.classList.add('fv-target');
      row.scrollIntoView({ block: 'center' });
    }
    return row;
  }

  function moveEditorToLine(lineNo, floatTools = false) {
    const view = body._cmView;
    if (!view) {
      const rows = body.querySelectorAll('.fv-line');
      if (!rows.length) return;
      const target = Math.min(Math.max(parseInt(lineNo, 10) || 1, 1), rows.length);
      _fvHighlightViewLine(target);
      lineInput.value = String(target);
      editStatus.textContent = `Line ${target}`;
      return;
    }
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
    if (!query) return;
    if (!view) {
      const rows = Array.from(body.querySelectorAll('.fv-line'));
      const total = rows.length;
      if (!total) return;
      const needle = query.toLowerCase();
      const start = _editFindPos >= 0 && _editFindPos < total ? _editFindPos : (dir < 0 ? 0 : total - 1);
      for (let step = 1; step <= total; step++) {
        const idx = ((start + dir * step) % total + total) % total;
        const text = rows[idx].querySelector('.fv-code')?.textContent || '';
        if (text.toLowerCase().includes(needle)) {
          _editFindPos = idx;
          _fvHighlightViewLine(idx + 1);
          lineInput.value = String(idx + 1);
          editStatus.textContent = `Match on line ${idx + 1}`;
          return;
        }
      }
      editStatus.textContent = 'No matches';
      return;
    }
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

  function showFileViewToolbar() {
    findInput.value = initialSearch;
    _editFindPos = initialSearch && line ? Math.max(0, (parseInt(line, 10) || 1) - 1) : -1;
    lineInput.value = line ? String(line) : '';
    editStatus.textContent = initialSearch ? `Find: ${initialSearch}` : '';
    editToolbar.hidden = false;
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

    const ext = _languageExtForPath(path);
    const lang = LANGS[ext]?.();
    const extensions = [basicSetup, oneDark, EditorView.updateListener.of(update => {
      if (update.docChanged) updateEditDirtyState();
    })];
    if (atomOneDarkHighlight) extensions.push(atomOneDarkHighlight);
    if (lang) extensions.push(lang);

    const state = EditorState.create({ doc: text, extensions });
    const view = new EditorView({ state, parent: body });
    body._cmView = view;

    box.classList.add('fv-editing');
    editToolbar.hidden = false;
    editBtn.hidden = true;
    historyBtn.hidden = true;
    renameBtn.hidden = true;
    cancelBtn.hidden = false;
    saveBtn.hidden = false;
    editStatus.textContent = 'No changes';
    findInput.value = initialSearch;
    _editFindPos = -1;
    lineInput.value = line ? String(line) : '';
    saveBtn.disabled = true;
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
    editToolbar.hidden = true;
    cancelBtn.hidden = true;
    saveBtn.hidden = true;
    const isText = pathKind === 'file' && path ? (pathIsText || _isTextPath(path)) : false;
    editBtn.hidden = !isText;
    historyBtn.hidden = !isText;
    renameBtn.hidden = !path;
    deleteBtn.hidden = pathKind !== 'file';
    newFileBtn.hidden = pathKind !== 'directory';
    newFolderBtn.hidden = pathKind !== 'directory';
    uploadBtn.hidden = pathKind !== 'directory';
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

  function renderTextDiff(container, before, after) {
    const beforeLines = String(before ?? '').split('\n');
    const afterLines = String(after ?? '').split('\n');
    const m = beforeLines.length;
    const n = afterLines.length;
    const dp = Array.from({ length: m + 1 }, () => Array(n + 1).fill(0));
    for (let i = m - 1; i >= 0; i--) {
      for (let j = n - 1; j >= 0; j--) {
        dp[i][j] = beforeLines[i] === afterLines[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < m || j < n) {
      const el = document.createElement('div');
      if (i < m && j < n && beforeLines[i] === afterLines[j]) {
        el.className = 'diff-line';
        el.textContent = '  ' + beforeLines[i++];
        j++;
      } else if (j < n && (i === m || dp[i][j + 1] >= dp[i + 1]?.[j])) {
        el.className = 'diff-line diff-add';
        el.textContent = '+ ' + afterLines[j++];
      } else {
        el.className = 'diff-line diff-remove';
        el.textContent = '- ' + beforeLines[i++];
      }
      container.appendChild(el);
    }
  }

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
        const meta = document.createElement('div');
        meta.className = 'fv-history-meta';
        const time = document.createElement('span');
        time.className = 'fv-history-time';
        time.textContent = item.edited_at.replace('T', ' ').replace('Z', ' UTC');
        const rowActions = document.createElement('div');
        rowActions.className = 'fv-history-actions';
        const diffBtn = document.createElement('button');
        diffBtn.className = 'fv-history-diff-btn';
        diffBtn.textContent = 'Show diff';
        const btn = document.createElement('button');
        btn.className = 'fv-history-revert-btn';
        btn.textContent = 'Revert to this';
        const diffWrap = document.createElement('div');
        diffWrap.className = 'fv-history-diff';
        diffWrap.hidden = true;
        const diffScroll = document.createElement('div');
        diffScroll.className = 'diff-scroll';
        renderTextDiff(diffScroll, item.before, item.after);
        diffWrap.appendChild(diffScroll);
        diffBtn.addEventListener('click', () => {
          diffWrap.hidden = !diffWrap.hidden;
          diffBtn.textContent = diffWrap.hidden ? 'Show diff' : 'Hide diff';
        });
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
        meta.appendChild(time);
        rowActions.append(diffBtn, btn);
        meta.appendChild(rowActions);
        row.append(meta, diffWrap);
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
  _fvHandlePopState = () => {
    if (webPreview) return goFileHistory(-1);
    if (!isInline && document.getElementById('file-modal')) {
      closeModal({ fromHistory: true });
      return true;
    }
    return false;
  };
  if (!isInline && isMobileViewport() && history.pushState) {
    const state = (history.state && typeof history.state === 'object') ? history.state : {};
    history.pushState({ ...state, squidView: currentView, squidFileViewer: true }, '', location.href);
    pushedFileViewerHistory = true;
  }

  // ── Content ──────────────────────────────────────────────────────────────────
  function renderFileRoots(data) {
    body.classList.remove('fv-web-preview-body');
    body.innerHTML = '';
    const hint = document.createElement('p');
    hint.className = 'fv-roots-hint';
    hint.appendChild(document.createTextNode('Frequently visited directories from the YAML config. The file browser can reach any path you have OS access to, same as the agent. '));
    const configLink = document.createElement('a');
    configLink.href = '#';
    configLink.textContent = 'Edit YAML config →';
    configLink.addEventListener('click', e => {
      e.preventDefault();
      if (!isInline) modal.remove();
      _fvNavigate = null;
      _fvHandlePopState = null;
      navigateView('settings');
    });
    hint.appendChild(configLink);
    body.appendChild(hint);
    const roots = data.roots || [];
    if (!roots.length) {
      const empty = document.createElement('div');
      empty.className = 'fv-dir-empty';
      empty.textContent = 'No frequently visited directories configured';
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
        webPreview = false;
        updateNav();
        renderFileRoots(await loadFileRoots());
        return;
      }
      const res = await fetch('/localfile?' + new URLSearchParams({ path, _t: Date.now() }));
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        body.textContent = errData.error || `Error ${res.status}`;
        return;
      }
      const ct = res.headers.get('content-type') || '';
      if (ct.startsWith('image/')) {
        pathKind = 'file';
        pathIsText = false;
        fileText = '';
        markdownPreview = false;
        updateNav();
        editToolbar.hidden = true;
        _renderImageFilePreview(body, path);
        return;
      }
      if (!ct.includes('text/') && !ct.includes('application/json') && !_isTextPath(path)) {
        if (!isInline) { modal.remove(); _fvNavigate = null; _fvHandlePopState = null; }
        window.open('/localfile?' + new URLSearchParams({ path, _t: Date.now() }), '_blank');
        if (isInline) { body.textContent = 'Opened in new tab'; }
        return;
      }
      const contentLength = Number(res.headers.get('content-length'));
      if (contentLength && contentLength > FV_MAX_PREVIEW_BYTES) {
        pathKind = 'file';
        pathIsText = ct.includes('text/') || ct.includes('application/json');
        fileText = '';
        markdownPreview = false;
        updateNav();
        editToolbar.hidden = true;
        _renderTooLargeFilePreview(body, path, contentLength);
        return;
      }
      const text = await res.text();
      if (ct.includes('application/json')) {
        try {
          const data = JSON.parse(text);
          if (data.type === 'directory') {
            pathKind = 'directory';
            pathIsText = false;
            webPreview = false;
            if (data.path !== path) path = data.path;
            updateNav();
            setDirUploadStatus = _renderDirListing(body, data, {
              renameIcon: FV_ICON_RENAME,
              deleteIcon: FV_ICON_DELETE,
              onRename: entry => renameLocalPath(entry.path, entry.name, () => loadFile()),
              onDelete: entry => deleteLocalFile(entry.path, entry.name, () => loadFile()),
              onUploadFiles: (files, setStatus) => uploadAndMaybePick(data.path, files, setStatus),
              onPick: pickOpts ? entry => { pickOpts.onPick(entry.path); closeModal(); } : undefined,
            });
            return;
          }
        } catch {}
      }
      pathKind = 'file';
      pathIsText = ct.includes('text/') || ct.includes('application/json');
      fileText = text;
      markdownPreview = false;
      updateNav();
      if (webPreview) {
        editToolbar.hidden = true;
        _renderWebFilePreview(body, path);
      } else {
        _renderFileViewer(body, text, line, endLine, path, changedLines);
        showFileViewToolbar();
      }
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

function _renderDirListing(container, data, opts = {}) {
  container.classList.remove('fv-web-preview-body');
  container.innerHTML = '';

  const filterWrap = document.createElement('div');
  filterWrap.className = 'fv-filter-wrap';
  const filterInput = document.createElement('input');
  filterInput.className = 'fv-filter';
  filterInput.type = 'text';
  filterInput.placeholder = 'Filter…';
  filterInput.setAttribute('aria-label', 'Filter files');
  filterWrap.appendChild(filterInput);
  const uploadStatus = document.createElement('div');
  uploadStatus.className = 'fv-upload-status';
  uploadStatus.hidden = true;
  filterWrap.appendChild(uploadStatus);
  container.appendChild(filterWrap);

  const list = document.createElement('div');
  list.className = 'fv-dir-listing';
  container.appendChild(list);

  const hasMeta = data.entries.some(e => e.size != null || e.mtime != null);
  const setUploadStatus = text => {
    uploadStatus.textContent = text || '';
    uploadStatus.hidden = !text;
  };

  if (opts.onUploadFiles) {
    container.ondragover = e => {
      if (!Array.from(e.dataTransfer?.types || []).includes('Files')) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      container.classList.add('fv-drop-active');
    };
    container.ondragleave = e => {
      if (!container.contains(e.relatedTarget)) container.classList.remove('fv-drop-active');
    };
    container.ondrop = async e => {
      if (!e.dataTransfer?.files?.length) return;
      e.preventDefault();
      container.classList.remove('fv-drop-active');
      try {
        await opts.onUploadFiles(e.dataTransfer.files, setUploadStatus);
        setTimeout(() => setUploadStatus(''), 1200);
      } catch (err) {
        setUploadStatus(err.message || 'Upload failed');
      }
    };
  } else {
    container.ondragover = null;
    container.ondragleave = null;
    container.ondrop = null;
  }

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
      const row = document.createElement('div');
      row.className = 'fv-dir-entry' + (entry.is_dir ? ' fv-dir-entry--dir' : '');
      const a = document.createElement('a');
      a.className = 'fv-dir-entry-link';
      a.href = '/localfile?' + new URLSearchParams({ path: entry.path });
      a.addEventListener('click', e => {
        e.preventDefault();
        e.stopPropagation();
        if (!entry.is_dir && opts.onPick) { opts.onPick(entry); return; }
        if (_fvNavigate) _fvNavigate(entry.path);
        else openFileViewer(entry.path);
      });

      const nameSpan = document.createElement('span');
      nameSpan.className = 'fv-dir-name';
      nameSpan.textContent = entry.is_dir ? entry.name + '/' : entry.name;
      a.appendChild(nameSpan);
      row.appendChild(a);

      if (hasMeta) {
        const sizeSpan = document.createElement('span');
        sizeSpan.className = 'fv-dir-meta';
        sizeSpan.textContent = entry.is_dir ? '' : _fmtSize(entry.size);
        row.appendChild(sizeSpan);

        const mtimeSpan = document.createElement('span');
        mtimeSpan.className = 'fv-dir-meta';
        mtimeSpan.textContent = _fmtMtime(entry.mtime);
        row.appendChild(mtimeSpan);
      }

      if (opts.onRename) {
        const renameBtn = document.createElement('button');
        renameBtn.className = 'fv-dir-action';
        renameBtn.type = 'button';
        renameBtn.title = 'Rename';
        renameBtn.setAttribute('aria-label', `Rename ${entry.name}`);
        renameBtn.innerHTML = opts.renameIcon || '';
        renameBtn.addEventListener('click', e => {
          e.preventDefault();
          e.stopPropagation();
          opts.onRename(entry);
        });
        row.appendChild(renameBtn);
      }

      if (opts.onDelete && !entry.is_dir) {
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'fv-dir-action fv-dir-danger-action';
        deleteBtn.type = 'button';
        deleteBtn.title = 'Delete';
        deleteBtn.setAttribute('aria-label', `Delete ${entry.name}`);
        deleteBtn.innerHTML = opts.deleteIcon || '';
        deleteBtn.addEventListener('click', e => {
          e.preventDefault();
          e.stopPropagation();
          opts.onDelete(entry);
        });
        row.appendChild(deleteBtn);
      }

      list.appendChild(row);
    });
  }

  filterInput.addEventListener('input', () => {
    const q = filterInput.value.trim().toLowerCase();
    renderEntries(q ? data.entries.filter(e => e.name.toLowerCase().includes(q)) : data.entries);
  });

  renderEntries(data.entries);
  requestAnimationFrame(() => filterInput.focus());
  return setUploadStatus;
}

function _renderFileViewer(container, text, targetLine, endLine, path, changedLines = null) {
  container.classList.remove('fv-web-preview-body');
  const rawLines = text.split('\n');
  if (rawLines.length && rawLines[rawLines.length - 1] === '') rawLines.pop();
  const numWidth = String(rawLines.length).length;

  let hlLines = null;
  if (typeof hljs !== 'undefined') {
    try {
      const ext = _languageExtForPath(path);
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

function _renderMarkdownFilePreview(container, text) {
  container.classList.remove('fv-web-preview-body');
  container.innerHTML = '';
  const preview = document.createElement('div');
  preview.className = 'fv-md-preview';
  preview.innerHTML = renderAssistantMarkdown(text || '');
  container.appendChild(preview);
}

function _renderTooLargeFilePreview(container, path, size) {
  container.classList.remove('fv-web-preview-body');
  container.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'fv-too-large';
  const msg = document.createElement('div');
  msg.textContent = `File is ${_fmtSize(size)} — too large to preview inline (limit ${_fmtSize(FV_MAX_PREVIEW_BYTES)}).`;
  const link = document.createElement('a');
  link.href = '/localfile?' + new URLSearchParams({ path, _t: Date.now() });
  link.target = '_blank';
  link.rel = 'noopener';
  link.textContent = 'Open in new tab';
  wrap.append(msg, link);
  container.appendChild(wrap);
}

function _renderImageFilePreview(container, path) {
  container.classList.remove('fv-web-preview-body');
  container.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'fv-image-preview';
  const img = document.createElement('img');
  img.alt = path.split('/').pop() || path;
  img.src = '/localfile?' + new URLSearchParams({ path, _t: Date.now() });
  wrap.appendChild(img);
  container.appendChild(wrap);
}

function _renderWebFilePreview(container, path) {
  container.classList.add('fv-web-preview-body');
  container.innerHTML = '';
  const frame = document.createElement('iframe');
  frame.className = 'fv-web-preview';
  frame.title = 'File preview';
  frame.src = '/localfile?' + new URLSearchParams({ path });
  container.appendChild(frame);
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
initBalanceQuota('deepseek');
initBalanceQuota('kimi');
initBalanceMaxPopup();
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
    setTopicChip(saved.topic, saved.agent || null, saved.adhoc || false, saved.lookback || 0, {
      route: saved.route,
      chainTarget: saved.chainTarget,
      chainTargetFresh: saved.chainTargetFresh,
      chainOperator: saved.chainOperator,
      chainRounds: saved.chainRounds,
      chainTargetTopic: saved.chainTargetTopic,
      broadcastAgents: saved.broadcastAgents || null,
    });
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
function recoverForegroundState() {
  updateActiveQuotaGauge().then(() => {
    if (activeQuotaBackend) fetchQuotaForBackend(activeQuotaBackend);
  }).catch(() => {});
  if (_activePollImmediate) _activePollImmediate();
  recoverPendingBubbles();
  startProcPoll();
  if (stickyChip?.route && !stickyChip.broadcastAgents) {
    refreshRouteTurnCounts(stickyChip.route, { force: true });
  }
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    _messagesAtBottomBeforeHide = isAtBottom();
  } else {
    if (_messagesAtBottomBeforeHide) {
      messages.scrollTop = messages.scrollHeight;
    }
    recoverForegroundState();
  }
});
window.addEventListener('pageshow', recoverForegroundState);

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
