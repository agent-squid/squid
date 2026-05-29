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

// ── navigation ────────────────────────────────────────────────────────────────

let currentView = 'chat';

function switchView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  document.querySelectorAll('.nav-tab, .hmenu-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === name);
  });
  currentView = name;
  if (name === 'analytics') {
    loadStats();
    if (statsGroup === 'proc') startLivePoll(); else stopLivePoll();
  } else {
    stopLivePoll();
  }
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

let historyFilter = { topic: null, agent: null };

function filterByTopic(topic) {
  setTopicChip(topic, null);
  reloadHistory({ topic, agent: null });
}

function filterByAgent(topic, agent, adhoc = false, lookback = 0) {
  setTopicChip(topic, agent, adhoc, lookback);
  reloadHistory({ topic, agent });
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
  const { topic, agent } = historyFilter;

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
    const res = await fetch(url);
    data = await res.json();
  } catch {
    historyLoading = false;
    return;
  }

  const { items, has_more } = data;
  const prevHeight = messages.scrollHeight;
  const fragment = document.createDocumentFragment();

  // Build lookup: user msg id → assistant stats (for ctx label on user rows)
  const statsByUserMsgId = {};
  for (const item of items) {
    if (item.role === 'assistant' && item.reply_to && item.stats) {
      statsByUserMsgId[item.reply_to] = item.stats;
    }
  }

  for (const item of [...items].reverse()) {
    if (item.role === 'user' && !item.content) continue;
    if (item.role === 'assistant' && !item.content && item.status !== 'pending') continue;

    if (item.role === 'user') {
      const userStats = statsByUserMsgId[item.id];
      const lb = userStats?.lookback ?? 0;
      const histCtxLabel = (item.agent || item.adhoc) ? fmtCtxLabel(!!item.adhoc, lb) : null;
      const userBubble = makeUserBubble(item.content, item.topic, item.agent, item.backend, !!item.adhoc, lb);
      userBubble.classList.add('history-item');
      fragment.appendChild(userBubble);
      if (item.timestamp) {
        const ts = document.createElement('div');
        ts.className = 'msg-time right history-item';
        ts.textContent = fmtTime(item.timestamp);
        if (histCtxLabel) {
          const ctxSpan = document.createElement('span');
          ctxSpan.className = 'user-ctx';
          ctxSpan.textContent = '  · ctx:' + histCtxLabel;
          if (userStats?.session_id) ctxSpan.dataset.sessionId = userStats.session_id;
          if (userStats?.cwd) ctxSpan.dataset.cwd = userStats.cwd;
          ctxSpan.addEventListener('click', e => { e.stopPropagation(); showCtxPopup(ctxSpan); });
          ts.appendChild(ctxSpan);
        }
        fragment.appendChild(ts);
      }
    } else if (item.role === 'assistant') {
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
      asstHeaderText.appendChild(document.createTextNode('  ' + truncate(item.prompt || '', 55)));
      asstHeader.appendChild(asstHeaderText);
      asstBubble.appendChild(asstHeader);

      const asstContent = document.createElement('div');
      if (item.status === 'pending') {
        addLoader(asstContent);
        pollMessageStatus(item.id, asstContent, asstBubble);
      } else if (item.status === 'error') {
        const raw = (item.content || '').split('\n')[0].replace(/^CLI exited \d+:\s*/, '').trim();
        asstContent.innerHTML = `<span class="msg-error">${raw || 'Response interrupted.'}</span>`;
      } else {
        asstContent.innerHTML = marked.parse(item.content || '');
      }
      asstBubble.appendChild(asstContent);
      if (item.id) addPinButton(asstBubble, item.id, item.topic || 'default', item.agent || null);
      fragment.appendChild(asstBubble);

      if (item.stats) {
        const statsEl = addStats(asstBubble, item.stats, item.timestamp);
        statsEl.classList.add('history-item');
      }

      if (item.context) {
        try {
          const tools = typeof item.context === 'string' ? JSON.parse(item.context) : item.context;
          const diffTools = tools.filter(t => t.name === 'Edit' || t.name === 'Write' || t.name === 'MultiEdit');
          for (const tool of diffTools) {
            const block = makeToolBlock(tool);
            block.classList.add('history-item', 'tool-block-history');
            fragment.appendChild(block);
          }
        } catch {}
      }
    }
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
  { name: 'help',         desc: 'show help panel',                              args: false },
];

function parseCommand(message) {
  const t = message.trim().replace(/^\//, ''); // strip optional leading /
  if (/^restart$/i.test(t))      return { command: 'restart' };
  if (/^stop$/i.test(t))         return { command: 'stop' };
  if (/^stopall$/i.test(t))      return { command: 'stopall' };
  if (/^clear$/i.test(t))        return { command: 'clear' };
  if (/^compact$/i.test(t))      return { command: 'compact' };
  if (/^help$/i.test(t))         return { command: 'help' };
  if (/^filter reset$/i.test(t)) return { command: 'filter_reset' };
  if (/^filter$/i.test(t))       return { command: 'filter' };
  const m = t.match(/^deq(?:\s+(-?\d+))?$/i);
  if (m) return { command: 'deq', pos: m[1] != null ? parseInt(m[1]) : null };
  return null;
}

async function handleCommand(cmd, topic, agent) {
  if (cmd.command === 'help') {
    openHelp();
    return;
  }
  if (cmd.command === 'filter') {
    if (agent) filterByAgent(topic, agent);
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
  const { topic, agent, message } = parseInput(text);
  const cmd = parseCommand(message);
  if (cmd) {
    input.value = '';
    resizeComposer();
    hideAutocomplete();
    await handleCommand(cmd, topic, agent);
    // Re-set chip after topic-scoped commands so next message stays in context
    if (['clear', 'compact', 'stop', 'stopall', 'deq'].includes(cmd.command) && (topic !== 'default' || agent)) {
      setTopicChip(topic, agent);
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
  const _pinnedIds = getPinnedItems()
    .filter(item => {
      // Skip bookmarks from the same session — --resume already has that context
      const sameSession = item.topic === topic && (item.agent || null) === _effectiveAgent;
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
              }
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
            statusBuf += data;
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
              if (statsEl) messages.appendChild(statsEl);
              const diffTools = liveToolEvents.filter(t => t.name === 'Edit' || t.name === 'Write' || t.name === 'MultiEdit');
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

function toolLabel(tool) {
  const name = tool.name || '';
  if (name === 'Read' || name === 'Edit' || name === 'Write' || name === 'MultiEdit')
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

function makeToolBlock(tool) {
  const name = tool.name || '';
  const block = document.createElement('div');
  block.className = 'tool-block';

  const hasDiff = name === 'Edit' || name === 'MultiEdit' || name === 'Write';
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
  }

  toggle.addEventListener('click', () => block.classList.toggle('tool-expanded'));
  block.appendChild(toggle);
  block.appendChild(body);
  return block;
}


async function pollMessageStatus(msgId, contentEl, bubbleEl) {
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
        contentEl.innerHTML = marked.parse(data.content || '');
      } else if (data.status === 'error') {
        clearInterval(timer);
        const raw = (data.content || '').split('\n')[0].replace(/^CLI exited \d+:\s*/, '').trim();
        contentEl.innerHTML = `<span class="msg-error">${raw || 'Response interrupted.'}</span>`;
      } else if (count >= MAX_POLLS) {
        clearInterval(timer);
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
  const reasoning  = stats.reasoning_tokens   || 0;
  const inp        = input + cacheRead + cacheWrite;
  const hasCost    = stats.cost_usd != null;
  const cost       = hasCost ? `$${stats.cost_usd.toFixed(4)}` : '';
  const cache      = cacheRead ? ` · ${fmtNum(cacheRead)} cached` : '';
  const reason     = reasoning ? ` · ${fmtNum(reasoning)} reasoning` : '';
  const dur        = stats.duration_ms ? ` · ${(stats.duration_ms / 1000).toFixed(1)}s` : '';
  const timePrefix = timestamp ? fmtTime(timestamp) + '  ·  ' : '';

  el.appendChild(document.createTextNode(
    `${timePrefix}↑ ${fmtNum(inp)}${cache}  ↓ ${fmtNum(out)}${reason} tokens${dur}`
  ));

  const qdSpan = document.createElement('span');
  qdSpan.className = 'stats-quota-delta';
  el.appendChild(qdSpan);

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
    if (statsGroup === 'proc' && currentView === 'analytics') loadStats();
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
              : statsGroup === 'model' ? '/stats?group=agent'
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
    renderAgentStats(rows);
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

function renderAgentStats(rows) {
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
      <td>${r.agent || '—'}</td>
      <td>${r.duration_s}s</td>
      <td>${r.started_iso ? fmtTime(r.started_iso) : '—'}</td>
    </tr>`).join('');
  statsContent.innerHTML = `<table>
    <thead><tr><th>PID</th><th>Backend</th><th>Topic</th><th>Agent</th><th>Duration</th><th>Started</th></tr></thead>
    <tbody>${bodyRows}</tbody>
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
      if (statsGroup === 'proc') startLivePoll();
      else stopLivePoll();
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
    if (dy > 80) setTimeout(() => location.reload(), 150);
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

  const sameTopic  = item.topic === chipTopic;
  const agentMatch = !chipAgent || (item.agent || null) === chipAgent;

  // "in session" only skips for session turns — --resume already covers it
  if (sameTopic && agentMatch && !isAdhoc) {
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

function addPinButton(bubbleEl, msgId, topic, agent) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'msg-pin-btn';
  btn.dataset.msgId = String(msgId);
  btn.title = 'Pin as context';
  btn.innerHTML = `<svg width="10" height="12" viewBox="0 0 12 14" fill="currentColor" aria-hidden="true">
    <path d="M2 0h8a1 1 0 0 1 1 1v12.8l-5-2.9-5 2.9V1a1 1 0 0 1 1-1z"/>
  </svg>`;
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
      setPinnedItems([...pinned, { id: msgId, topic, agent: agent || null, content: text }]);
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
});
initHistoryScroll();
initStats();
initAliases();
initQuota();
initCreds();
initPullToRefresh();
showBootBanner();
