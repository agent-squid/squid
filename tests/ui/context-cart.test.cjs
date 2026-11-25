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
    _pendingSessionInjectedIds: {},
    _pendingSessionAttachedFiles: {},
  });
  for (const [start, end] of [
    ['function getPinnedItems()', 'function refreshPinnedContextUi()'],
    ['function getStagedAttachments()', 'function _removeAttachedFilePath('],
    ['function getInjectedInto()', 'function _currentContextTarget()'],
    ['function _memoryRootTopic(', 'function _memoryCacheKeys('],
    ['function _injectablePinnedIds(', 'function updatePinCount('],
    ['function _pinStatus(', 'function _memoryStatus('],
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

test('sent status remains visible after deselecting a pin', () => {
  const cart = setup({ pinnedItems: [{ id: 1, selectedTrees: ['work'] }], injectedInto: { session: [1] } });
  const target = { topic: 'work', agent: 'echo', adhoc: false };
  cart._sessionIds['work@echo'] = 'session';
  assert.equal(cart._injectablePinnedIds('work', 'echo', false).length, 0);
  assert.equal(cart._injectablePinnedIds('work', 'echo', true).join(','), '1');
  cart.setPinSelected(1, 'work', false);
  assert.equal(cart._pinStatus(cart.getPinnedItems()[0], target, false).text, 'Sent');
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

test('unchecked attachments stay staged and keep their delivery status', () => {
  const cart = setup();
  const target = { topic: 'work', agent: 'echo', adhoc: true };
  const path = '/draft.txt';
  cart.setAttachedFiles([{ path }], target.topic);
  assert.equal(cart._attachedFilesState(target).selected[0].path, path);
  cart.setAttachedFileSelected(path, target.topic, false);
  assert.equal(cart.getAttachedFiles(target.topic)[0].path, path);
  assert.equal(cart._attachedFilesState(target).selected.length, 0);

  cart._sessionIds['work@echo'] = 'session';
  cart.setAttachedFilesInSession({ session: [path] });
  target.adhoc = false;
  assert.equal(cart._attachedFilesState(target).saved[0].path, path);
  cart.setAttachedFileSelected(path, target.topic, true);
  assert.equal(cart._attachedFilesState(target).selected.length, 0);
  target.adhoc = true;
  assert.equal(cart._attachedFilesState(target).selected[0].path, path);
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
