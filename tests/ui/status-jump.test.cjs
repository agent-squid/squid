const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('ui/app.js', 'utf8');
const helper = source.slice(source.indexOf('async function jumpToStatusMessage('), source.indexOf('async function jumpToMessage('));

function setup({ matches = true, search = false, bubblePresent = true } = {}) {
  const calls = [];
  let restore;
  const bubble = {
    dataset: { topic: 'squid', agent: 'claude', adhoc: '0' },
    scrollIntoView() { calls.push('scroll'); },
    classList: { add() {}, remove() {} },
    insertAdjacentElement() {},
  };
  const context = vm.createContext({
    historyFilter: { topic: 'shore' }, searchActive: search,
    searchState: search ? { keywords: ['hello'] } : null,
    promptOnlyHistory: false, bookmarkOnlyHistory: false, badOnlyHistory: false,
    searchLoading: false, currentView: 'stats',
    form: { before() { calls.push('notice'); } },
    messages: { querySelector(selector) { return selector.includes('.msg-thinking') && !bubblePresent ? null : bubble; } },
    document: {
      getElementById() { return { remove() {}, classList: { remove() {} } }; },
      querySelectorAll() { return []; },
      createElement() { return { appendChild() {}, setAttribute() {}, remove() {}, addEventListener(_, fn) { if (this.textContent === 'Restore filter') restore = fn; } }; },
    },
    itemMatchesFilter() { return matches; }, hasResponseOnlyFilter() { return false; },
    reloadHistory(filter) { context.historyFilter = filter; calls.push('reload'); },
    applyHistoryFilter(filter) { context.historyFilter = filter; calls.push('restore'); },
    navigateView() { calls.push('chat'); },
    showCmdFeedback() { calls.push('notice'); return { appendChild() {}, remove() {} }; },
    updateSearchButton() {}, updatePromptOnlyButton() {}, updateBookmarkButton() {},
    invalidateHistoryLoad() {}, setLiveGroupHidden() {}, _updateSearchBar() {}, loadSearchResults() {},
    jumpToMessage: async () => { calls.push('stored'); }, setTimeout() {},
  });
  vm.runInContext(helper, context);
  return { context, calls, restore: () => restore() };
}

test('matching WIP preserves filter and navigates to chat', async () => {
  const { context, calls } = setup();
  await context.jumpToStatusMessage(7);
  assert.deepEqual(calls, ['chat', 'scroll']);
  assert.equal(context.historyFilter.topic, 'shore');
});

test('excluded WIP clears filter before scrolling and offers restoration', async () => {
  const { context, calls, restore } = setup({ matches: false });
  await context.jumpToStatusMessage(7);
  assert.deepEqual(calls, ['reload', 'chat', 'notice', 'scroll']);
  assert.equal(context.historyFilter.topic, undefined);
  restore();
  assert.equal(context.historyFilter.topic, 'shore');
});

test('search-hidden WIP clears search and restores its keywords', async () => {
  const { context, restore } = setup({ search: true });
  await context.jumpToStatusMessage(7);
  assert.equal(context.searchActive, false);
  restore();
  assert.equal(context.searchActive, true);
  assert.deepEqual(context.searchState.keywords, ['hello']);
});

test('completed WIP uses stored-message navigation', async () => {
  const { context, calls } = setup({ bubblePresent: false });
  await context.jumpToStatusMessage(7);
  assert.equal(calls[0], 'stored');
});
