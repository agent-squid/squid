const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('ui/app.js', 'utf8');

function makeStorage(initial = {}) {
  const storage = new Map(Object.entries(initial).map(([key, value]) => [key, JSON.stringify(value)]));
  return {
    getItem: key => storage.get(key),
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
}

function setup(initial = {}, localStorage = makeStorage(initial)) {
  const context = vm.createContext({
    localStorage,
    sessionStorage: makeStorage(),
    document: { querySelectorAll: () => ['other', 'work.first', 'work.second'].map((topic, index) => ({
      dataset: { topic, msgId: String(index + 1) }, children: [],
    })) },
    _sessionIds: {},
    pendingCartContextByRoute: new Map(),
    pendingAttachmentCounts: new Map(),
    pendingAttachmentsByRoute: new Map(),
    pinPanel: { classList: { contains: () => false } },
    updatePinCount() {},
  });
  for (const [start, end] of [
    ['function historyPromptContext(', 'function makeHistoryContextControl('],
    ['function getPinnedItems()', 'function refreshPinnedContextUi()'],
    ['function getStagedAttachments()', 'function addAttachedFile('],
    ['function getInjectedInto()', 'function _currentContextTarget()'],
    ['function _memoryRootTopic(', 'function _memoryCacheKeys('],
    ['function _injectablePinnedIds(', 'function updatePinCount('],
    ['function _pinStatus(', 'function _memoryStatus('],
    ['function _attachedFileStatus(', 'function renderPinPanel('],
    ['const _lookbackSelections', 'function updateInContextMarkers('],
  ]) vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end)), context);
  context.setPinnedItems = items => context.localStorage.setItem('pinnedItems', JSON.stringify(items));
  return context;
}

test('legacy pins remain available and checkbox changes persist per tree', () => {
  const cart = setup({ pinnedItems: [{ id: 1, topic: 'work.first' }] });
  assert.equal(cart.getPinnedItems().length, 1);
  assert.equal(cart.getSelectedPins('work').length, 0);
  cart.setPinSelected(1, 'work.first', true);
  assert.equal(cart.getSelectedPins('work.second')[0].id, 1);
  assert.equal(cart.getSelectedPins('other').length, 0);
  cart.setPinSelected(1, 'other', true);
  cart.setPinSelected(1, 'work', false);
  assert.equal(cart.getSelectedPins('work.first').length, 0);
  assert.equal(cart.getSelectedPins('other')[0].id, 1);
  cart.setPinnedItems([]);
  assert.equal(cart.getSelectedPins('other').length, 0);
});

test('a pin already in the session is locked and checked even after deselection', () => {
  const cart = setup({ pinnedItems: [{ id: 1, selectedTrees: ['work'] }], injectedInto: { session: [1] } });
  const target = { topic: 'work', agent: 'echo', adhoc: false };
  cart._sessionIds['work@echo'] = 'session';
  assert.equal(cart._injectablePinnedIds('work', 'echo', false).length, 0);
  assert.equal(cart._injectablePinnedIds('work', 'echo', true).join(','), '1');
  cart.setPinSelected(1, 'work', false);
  const state = cart._pinStatus(cart.getPinnedItems()[0], target, false);
  assert.equal(state.text, 'In session');
  assert.equal(state.checked, true);
  assert.equal(state.disabled, true);
  assert.equal(cart._injectablePinnedIds('work', 'echo', true).length, 0);
});

test('staged files clear across trees and stay cleared on return', () => {
  const cart = setup({ attachedFiles: [{ path: '/legacy.txt' }] });
  assert.equal(cart.getAttachedFiles('work').length, 0);
  cart.setAttachedFiles([{ path: '/draft.txt' }], 'work');
  cart.clearAttachmentsForTreeSwitch('work.sibling');
  assert.equal(cart.getAttachedFiles('work.sibling')[0].path, '/draft.txt');
  assert.equal(cart.getAttachedFiles('other').length, 0);
  cart.clearAttachmentsForTreeSwitch('other');
  assert.equal(cart.getAttachedFiles('work').length, 0);
});

test('automatic lookback only offers responses from the destination topic', () => {
  const cart = setup();
  assert.equal(cart._activeLookbackItems('work.first', true, 8).map(item => item.id).join(','), '2');
  assert.equal(cart._activeLookbackItems('other', true, 8).map(item => item.id).join(','), '1');
});

test('switching trees in another tab preserves staged files', () => {
  const sharedStorage = makeStorage();
  const firstTab = setup({}, sharedStorage);
  const secondTab = setup({}, sharedStorage);
  firstTab.setAttachedFiles([{ path: '/draft.txt' }], 'work');
  secondTab.clearAttachmentsForTreeSwitch('other');
  secondTab.setAttachedFiles([{ path: '/other.txt' }], 'other');
  assert.equal(firstTab.getAttachedFiles('work')[0].path, '/draft.txt');
  assert.equal(secondTab.getAttachedFiles('other')[0].path, '/other.txt');
});

test('fresh sends send only lookback exclusions, leaving the window to the server', () => {
  const cart = setup();
  const start = source.indexOf('  const lookbackKey =');
  const end = source.indexOf('  if (updateComposerRoute)', start);
  const payloadStart = source.indexOf('  const chatPayload =', end);
  const payloadEnd = source.indexOf('  async function approveProvider', payloadStart);
  assert.ok(start >= 0 && end > start && payloadStart > end && payloadEnd > payloadStart);
  vm.runInContext(`
    function makePayload(opts) {
      const topic = 'work.first', agent = 'echo', adhoc = true, lookback = 3;
      const nativeShell = false, message = 'hello', source = 'human', flowRoute = null;
      const _includeTopicMemory = false, _contextIds = [], _attachedFiles = [];
      ${source.slice(start, end)}
      ${source.slice(payloadStart, payloadEnd)}
      return chatPayload;
    }
  `, cart);
  cart._setLookbackSelected('work.first', 3, 'echo', 2, false);
  const payload = cart.makePayload({});
  assert.equal(payload.lookback, 3);
  assert.equal(payload.lookback_excluded_ids.join(','), '2');
  assert.equal(payload.lookback_via_pins, undefined);
  assert.equal(cart.makePayload({ retryContext: {} }).lookback_via_pins, true);

  cart._currentContextTarget = () => ({ topic: 'work.first', adhoc: true, lookback: 3, routeAgent: 'echo' });
  cart.pinPanel = { classList: { contains: () => false } };
  cart.updateInContextMarkers = () => {};
  cart.updatePinCount = () => {};
  cart.document.querySelectorAll = () => [];
  const clearStart = source.indexOf('function clearPinnedItems()');
  const clearEnd = source.indexOf('function getInjectedInto()', clearStart);
  vm.runInContext(source.slice(clearStart, clearEnd), cart);
  cart.clearPinnedItems();
  assert.equal(cart.makePayload({}).lookback_via_pins, true);
  assert.equal(cart.makePayload({}).lookback_excluded_ids, undefined);
});

test('a rendered lookback deselection survives candidate changes', () => {
  const cart = setup();
  const row = id => ({ dataset: { topic: 'work.first', agent: 'echo', msgId: String(id) }, children: [] });
  cart.document.querySelectorAll = () => [row(2)];
  cart._setLookbackSelected('work.first', 3, 'echo', 2, false);
  assert.equal(cart._activeLookbackItems('work.first', true, 3, 'echo').length, 0);
  cart.document.querySelectorAll = () => [row(1), row(2)];
  assert.equal(cart._activeLookbackItems('work.first', true, 3, 'echo').map(item => item.id).join(','), '1');
});

test('attachments are used once, retryable on failure, and selectable again', () => {
  const cart = setup();
  const target = { topic: 'work', agent: 'echo', adhoc: true };
  cart.setAttachedFiles([{ path: '/draft.txt' }], target.topic);
  const [file] = cart._attachedFilesState(target).selected;
  const failedSend = cart.trackAttachmentSend(target, [file]);
  assert.equal(cart._attachedFileStatus(file).text, 'Sending');
  assert.equal(cart._attachedFilesState(target).selected.length, 0);
  failedSend(false);
  assert.equal(cart._attachedFilesState(target).selected.length, 1);

  const successfulSend = cart.trackAttachmentSend(target, [file]);
  successfulSend(true);
  const sibling = { ...target, topic: 'work.sibling', adhoc: false };
  const [used] = cart._attachedFilesState(sibling).available;
  assert.equal(cart._attachedFileStatus(used).text, 'Used');
  assert.equal(cart._attachedFilesState(sibling).selected.length, 0);
  cart.setAttachedFileSelected(file.path, sibling.topic, true);
  assert.equal(cart._attachedFilesState(sibling).selected[0].path, file.path);
});

test('successful sign-in retry consumes the original staged attachment', async () => {
  const cart = setup();
  const target = { topic: 'work', agent: 'echo', adhoc: false };
  const path = '/draft.txt';
  cart.setAttachedFiles([{ path }], target.topic);
  const { stagedFiles } = cart.contextCartSnapshot(target.topic);
  cart.trackAttachmentSend(target, stagedFiles)(false);

  const retryStart = source.indexOf('retryContext: {') + 'retryContext: '.length;
  const retryEnd = source.indexOf('\n    }));', retryStart);
  const retryContext = vm.runInContext(
    '((_attachedFiles, _contextIds, lookbackViaPins, lookbackExcludedIds, _includeTopicMemory) => ('
      + source.slice(retryStart, retryEnd).trim().replace(/,$/, '') + '))', cart,
  )(stagedFiles, [], false, [], false);

  Object.assign(cart, {
    topic: target.topic, _effectiveAgent: target.agent, adhoc: false,
    opts: { retryContext }, _topicMemoryStateForSend: async () => ({ selected: false }),
  });
  const start = source.indexOf('  const availability = opts.retryContext');
  const end = source.indexOf('  if (!nativeShell) {', start);
  const files = await vm.runInContext('(async () => {' + source.slice(start, end) + 'return _attachedFiles; })()', cart);
  const settle = cart.trackAttachmentSend(target, files);
  assert.equal(cart._attachedFileStatus(stagedFiles[0]).text, 'Sending');
  settle(true);
  assert.equal(cart._attachedFileStatus(cart.getAttachedFiles(target.topic)[0]).text, 'Used');
  assert.equal(cart._attachedFilesState(target).selected.length, 0);
});

test('send preparation skips pins that start sending during file validation', async () => {
  const cart = setup();
  const target = { topic: 'work', agent: 'echo', adhoc: false };
  let finishValidation;
  Object.assign(cart, {
    topic: target.topic, _effectiveAgent: target.agent, adhoc: false, nativeShell: false,
    opts: {}, stagedFiles: [], selectedPins: [{ id: 1 }], lookbackSelection: null, shellLookbackIds: [],
    _topicMemoryStateForSend: async () => ({ selected: false }),
  });
  const start = source.indexOf('  const availability = opts.retryContext');
  const end = source.indexOf('  if (!nativeShell) {', start);
  cart.checkAttachedFiles = () => new Promise(resolve => { finishValidation = resolve; });
  const prepared = vm.runInContext('(async () => {' + source.slice(start, end) + 'return _contextIds; })()', cart);
  cart.trackPendingCartContext(target, [1], null);
  finishValidation(new Map());
  assert.equal((await prepared).length, 0);
});

test('a second send skips an attachment used while it was preparing', () => {
  const cart = setup();
  const target = { topic: 'work', agent: 'echo', adhoc: false };
  cart.setAttachedFiles([{ path: '/draft.txt' }], target.topic);
  const { stagedFiles } = cart.contextCartSnapshot(target.topic);
  const settle = cart.trackAttachmentSend(target, stagedFiles);
  assert.equal(cart.contextCartSnapshot(target.topic).stagedFiles.length, 0);

  settle(true);
  const availability = new Map([['/draft.txt', false]]);
  assert.equal(cart.getAttachmentsForSend(target.topic, stagedFiles, availability, false).length, 0);
  assert.equal(cart.getAttachmentsForSend(target.topic, stagedFiles, availability, true).length, 1);
});

test('missing files stay in the cart and recover their previous selection', async () => {
  const cart = setup();
  const target = { topic: 'work' };
  const path = '/draft.txt';
  let exists = false;
  cart.fetch = async () => ({
    ok: true, json: async () => ({ paths: [{ path, exists, is_file: exists }] }),
  });
  cart.setAttachedFiles([{ path }], target.topic);
  await cart.checkAttachedFiles(target.topic);
  const status = cart._attachedFileStatus(cart.getAttachedFiles(target.topic)[0]);
  assert.equal(status.text, 'No file');
  assert.equal(status.checked, false);
  assert.equal(status.disabled, true);
  assert.equal(cart._attachedFilesState(target).available.length, 0);
  exists = true;
  await cart.checkAttachedFiles(target.topic);
  assert.equal(cart._attachedFilesState(target).selected[0].path, path);
});

test('an old send cannot consume a file restaged after switching trees', () => {
  const cart = setup();
  const topic = 'work';
  const path = '/draft.txt';
  cart.setAttachedFiles([{ path }], topic);
  const settle = cart.trackAttachmentSend({ topic }, cart.getAttachedFiles(topic));
  cart.clearAttachmentsForTreeSwitch('other');
  cart.setAttachedFiles([{ path }], topic);
  settle(true);
  assert.equal(cart._attachedFilesState({ topic }).selected[0].path, path);
});

test('completing one turn keeps the other turn’s pins and memory pending', () => {
  const cart = setup();
  const target = { topic: 'work', agent: 'echo', adhoc: false };
  const clearFirst = cart.trackPendingCartContext(target, [1, 2], 'revision');
  const clearSecond = cart.trackPendingCartContext(target, [2, 3], 'revision');
  Object.assign(cart, {
    topic: target.topic, _effectiveAgent: target.agent, resolvedAgent: target.agent, adhoc: false,
    _contextIds: [1, 2], reportedContextIds: undefined, _includeTopicMemory: true, _attachedFiles: [],
    settleAttachments: null, clearPendingContext: clearFirst,
    _topicMemoryForSend: { revision: 'revision' },
    _memorySelectionOverrides: {}, _memoryInjectedInto: {},
    _memoryOverrideKey: () => 'work@echo', _memoryInjectedKey: () => 'work@echo',
  });
  const start = source.indexOf('  function markSessionContextDelivered(');
  const end = source.indexOf('  function saveCartTurn(', start);
  vm.runInContext(source.slice(start, end)
    + 'markSessionContextDelivered("session", { prompt_context: "[1,4]" });', cart);
  assert.equal(cart.getInjectedInto().session.join(','), '1,4');
  clearFirst();

  assert.equal(cart.hasPendingCartPin(target, 1), false);
  assert.equal(cart.hasPendingCartPin(target, 2), true);
  assert.equal(cart.hasPendingCartPin(target, 3), true);
  assert.equal(cart.hasPendingCartMemory(target, 'revision'), true);
  assert.equal(cart._pinStatus({ id: 3 }, target, true).text, 'Sending');
  assert.equal(cart._injectablePinnedIds(target.topic, target.agent, false, [{ id: 3 }]).length, 0);

  clearSecond();
  assert.equal(cart.hasPendingCartPin(target, 2), false);
  assert.equal(cart.hasPendingCartPin(target, 3), false);
  assert.equal(cart.hasPendingCartMemory(target, 'revision'), false);
});

test('badge prioritizes ready items and excludes locked items', () => {
  const cart = setup({
    pinnedItems: [
      { id: 1, selectedTrees: ['work'] },
      { id: 2, selectedTrees: ['other'] },
      { id: 3, session_id: 'session', selectedTrees: ['work'] },
      { id: 4, selectedTrees: ['work'] },
    ],
  });
  const target = { topic: 'work', agent: 'echo', adhoc: false };
  cart._currentContextTarget = () => target;
  cart._topicMemoryState = () => ({ exists: false, selected: false });
  cart.stickyChip = null;
  cart._sessionIds['work@echo'] = 'session';
  cart.pinCountEl = { classList: { toggle() {} } };
  const classes = new Map();
  cart.pinBtn = { classList: { toggle: (name, active) => classes.set(name, active) }, setAttribute() {} };
  cart.setAttachedFiles([{ path: '/used.txt', selected: false, used: true }, { path: '/missing.txt', unavailable: true }], target.topic);
  vm.runInContext(source.slice(source.indexOf('function updatePinCount('), source.indexOf('function _pinTagStr(')), cart);
  const clearPin = cart.trackPendingCartContext(target, [4], null);
  assert.equal(cart.pinCountEl.textContent, 1);
  assert.equal(classes.get('has-context'), true);
  assert.equal(classes.get('has-context-pending'), false);
  assert.match(cart.pinBtn.title, /1 ready to send/);
  cart.setPinnedItems(cart.getPinnedItems().filter(pin => pin.id !== 1));
  cart.updatePinCount();
  assert.equal(cart.pinCountEl.textContent, 1);
  assert.equal(classes.get('has-context'), false);
  assert.equal(classes.get('has-context-pending'), true);
  assert.match(cart.pinBtn.title, /1 sending/);
  cart.setPinnedItems(cart.getPinnedItems().filter(pin => pin.id !== 2));
  cart.setAttachedFiles([{ path: '/draft.txt' }, { path: '/missing.txt', unavailable: true }], target.topic);
  const [file] = cart.getAttachedFiles(target.topic);
  const settle = cart.trackAttachmentSend(target, [file]);
  assert.equal(cart.pinCountEl.textContent, 2);
  for (const destination of [{ topic: 'work.sibling' }, { agent: 'other' }, { adhoc: true }]) {
    cart._currentContextTarget = () => ({ ...target, ...destination });
    cart.updatePinCount();
    assert.equal(classes.get('has-context-pending'), false);
    assert.equal(cart._attachedFileStatus(file).text, 'Sending');
  }
  cart._currentContextTarget = () => target;
  cart.updatePinCount();
  assert.equal(classes.get('has-context-pending'), true);
  clearPin();
  settle(false);
  assert.equal(classes.get('has-context-pending'), false);
  assert.equal(classes.get('has-context'), true);
  cart.setPinnedItems(cart.getPinnedItems().filter(pin => pin.id !== 4));
  cart.setAttachedFileSelected(file.path, target.topic, false);
  cart.updatePinCount();
  assert.match(cart.pinBtn.title, /1 available to select/);
  cart.setAttachedFiles([{ path: '/missing.txt', unavailable: true }], target.topic);
  cart.updatePinCount();
  assert.equal(cart.pinCountEl.textContent, '');
});

test('cart clicks stay inside the panel after the clicked row is replaced', () => {
  const start = source.indexOf("document.addEventListener('click', e => {\n  if (!acEl.contains(e.target)");
  const end = source.indexOf("  const ctxPopup =", start);
  assert.ok(start >= 0 && end > start);
  const pinPanel = { contains: () => false };
  const pinBtn = { contains: () => false };
  let closeCount = 0;
  let click;
  vm.runInNewContext(source.slice(start, end) + '});', {
    document: { addEventListener: (_, listener) => { click = listener; } },
    acEl: { contains: () => false },
    topicChipEl: { contains: () => false },
    input: {},
    hideAutocomplete() {},
    pinPanel,
    pinBtn,
    closePinPanel() { closeCount++; },
  });

  const detachedButton = {};
  click({ target: detachedButton, composedPath: () => [detachedButton, pinPanel] });
  click({ target: pinBtn, composedPath: () => [pinBtn] });
  assert.equal(closeCount, 0);
  click({ target: {}, composedPath: () => [] });
  assert.equal(closeCount, 1);
});

test('late success consumes the override and shows reported context even after completion', () => {
  const helperStart = source.indexOf('  let reportedContextIds;');
  const helperEnd = source.indexOf('  function markSessionContextDelivered(', helperStart);
  const callbackStart = source.indexOf('        const startChat = () => realtimeV1.start(');
  const callbackEnd = source.indexOf('        let result = await startChat();', callbackStart);
  assert.ok(helperStart >= 0 && helperEnd > helperStart && callbackStart >= 0 && callbackEnd > callbackStart);
  const liveCtxSpan = { dataset: {} };
  let consumed = 0;
  let displayedCount;
  const context = vm.createContext({
    adhoc: true, nativeShell: false, liveSessionTurnCount: 0, liveCtxSpan,
    _contextIds: [5], _includeTopicMemory: false, chatPayload: {},
    msgId: null, completionRendered: true, completedFromStatus: false,
    consumeLookbackSelection() { consumed++; },
    setCtxLabel(_element, _adhoc, count) { displayedCount = count; },
    realtimeV1: { start(_payload, onLate) { onLate({ ok: true, msg_id: 10, context_ids: [3, 5] }); } },
  });
  vm.runInContext(source.slice(helperStart, helperEnd) + source.slice(callbackStart, callbackEnd) + 'startChat();', context);
  assert.equal(consumed, 1);
  assert.equal(displayedCount, 2);
  assert.equal(liveCtxSpan.dataset.pinnedIds, '[3,5]');
});

test('a late acknowledgment restores attachment tracking after local timeout cleanup', async () => {
  const cart = setup();
  const topic = 'work';
  const file = { id: 'draft', path: '/draft.txt' };
  cart.setAttachedFiles([file], topic);
  Object.assign(cart, {
    topic, _effectiveAgent: 'echo', adhoc: false, _attachedFiles: [file],
    quotaFinalized: true, msgId: null, completionRendered: false, completedFromStatus: false,
    chatPayload: {}, acceptChatStart() {}, savePendingCartTurn() {},
    attachMsgId(id) { cart.msgId = id; return true; },
    markSessionContextPending() {},
    startStatusFallback(id) { assert.equal(id, 42); },
    detachedPolling: false,
  });
  let acknowledge;
  cart.realtimeV1 = { start(_payload, onLate) { acknowledge = onLate; } };
  const trackingStart = source.indexOf('  let settleAttachments = null;');
  const trackingEnd = source.indexOf('  function attachMsgId(', trackingStart);
  const callbackStart = source.indexOf('        const startChat = () => realtimeV1.start(');
  const callbackEnd = source.indexOf('        let result = await startChat();', callbackStart);
  vm.runInContext(source.slice(trackingStart, trackingEnd) + source.slice(callbackStart, callbackEnd)
    + 'trackCartAttachments(); startChat();', cart);
  await vm.runInContext('finalizeQuotaTracking()', cart);
  assert.equal(cart.pendingAttachmentCounts.has(file.id), false);

  acknowledge({ ok: true, msg_id: 42 });
  assert.equal(cart.pendingAttachmentCounts.get(file.id), 1);
  vm.runInContext('settleAttachments(true)', cart);
  assert.equal(cart.getAttachedFiles(topic)[0].used, true);
  assert.equal(cart.getAttachedFiles(topic)[0].selected, false);
});

test('refresh restores the sending badge on its route until the stored turn settles', async () => {
  for (const status of ['done', 'cancelled']) {
    const sender = setup();
    const target = { topic: 'work', agent: 'echo', adhoc: false };
    sender.setAttachedFiles([{ path: '/draft.txt' }], target.topic);
    const [file] = sender.getAttachedFiles(target.topic);
    sender.savePendingCartTurn(42, { ...target, fileIds: [file.id], pinIds: [] });

    const cart = setup({}, sender.localStorage);
    cart.sessionStorage = sender.sessionStorage;
    let destination = target;
    cart._currentContextTarget = () => destination;
    cart._topicMemoryState = () => ({ exists: false });
    cart.stickyChip = null;
    cart.pinCountEl = { classList: { toggle() {} } };
    const classes = new Map();
    cart.pinBtn = { classList: { toggle: (name, active) => classes.set(name, active) }, setAttribute() {} };
    vm.runInContext(source.slice(source.indexOf('function updatePinCount('), source.indexOf('function _pinTagStr(')), cart);
    let poll;
    let turnStatus = 'pending';
    cart.setTimeout = callback => { poll = callback; };
    cart.fetch = async url => {
      assert.equal(url, '/chat/42/status');
      return { ok: true, json: async () => ({ status: turnStatus, session_id: 'session', prompt_context: '[7]', content: 'Partial response' }) };
    };
    cart.restorePendingCartTurns();
    await new Promise(setImmediate);

    assert.match(cart.pinBtn.title, /1 sending/);
    assert.equal(classes.get('has-context-pending'), true);
    destination = { ...target, agent: 'other' };
    cart.updatePinCount();
    assert.equal(classes.get('has-context-pending'), false);
    destination = target;
    cart.updatePinCount();
    assert.equal(classes.get('has-context-pending'), true);

    turnStatus = status;
    await poll();
    assert.equal(classes.get('has-context-pending'), false);
    assert.equal(classes.get('has-context'), status === 'cancelled');
    assert.equal(cart.getAttachedFiles(target.topic)[0].used, status === 'done' ? true : undefined);
    assert.equal(Object.keys(cart.readPendingCartTurns()).length, 0);
    assert.equal(cart.getInjectedInto().session?.join(','), status === 'done' ? '7' : undefined);
  }
});

test('realtime completion retries an unavailable or pending status before releasing the cart', () => {
  const fallback = [];
  const delivered = [];
  let finalized = 0;
  const context = vm.createContext({
    msgId: 42, lastSessionId: null, turnStatus: 'pending', resolvedAgent: null,
    startStatusFallback: id => fallback.push(id),
    markSessionContextDelivered: id => delivered.push(id),
    finalizeQuotaTracking: () => { finalized++; },
  });
  const start = source.indexOf('          onSettled: data => {');
  const end = source.indexOf('          onProcessing:', start);
  const { onSettled } = vm.runInContext('({' + source.slice(start, end) + '})', context);

  onSettled(null);
  onSettled({ status: 'pending' });
  assert.deepEqual(fallback, [42, 42]);
  assert.equal(finalized, 0);
  onSettled({ status: 'done', agent: 'echo', session_id: 'session' });
  assert.deepEqual(delivered, ['session']);
  assert.equal(finalized, 1);
});

test('status recovery confirms context when another path rendered the response', async () => {
  const confirmed = [];
  const context = vm.createContext({
    statusTimer: null,
    topic: 'work', adhoc: false, _sessionIds: {},
    liveBookmarkState: false,
    completionRendered: false,
    completedFromStatus: false,
    resolvedAgent: null,
    lastSessionId: null,
    readTurnStatus: async () => ({ status: 'done', agent: 'echo', stats: { session_id: 'session' } }),
    _activePollImmediate: null,
    setInterval: () => 1,
    adoptCompletedBubbleRenderedElsewhere: () => true,
    turnStatus: 'pending',
    finalizeQuotaTracking() {},
    markSessionContextDelivered: sessionId => confirmed.push(sessionId),
    settleAttachments() {},
  });
  const start = source.indexOf('  function startStatusFallback(');
  const end = source.indexOf('  // Compute pinned IDs', start);
  vm.runInContext(source.slice(start, end) + 'startStatusFallback(42);', context);
  await new Promise(setImmediate);
  assert.deepEqual(confirmed, ['session']);
  assert.equal(context._sessionIds['work@echo'], 'session');
});
