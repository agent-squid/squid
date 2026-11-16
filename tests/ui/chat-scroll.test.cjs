const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('ui/app.js', 'utf8');
const helpers = source.slice(source.indexOf('function isAtBottom()'), source.indexOf('function addLoader('));

function setup(gap) {
  const messages = { scrollHeight: 1000, scrollTop: 500 - gap, clientHeight: 500 };
  const context = vm.createContext({ messages });
  vm.runInContext(helpers, context);
  return { messages, context };
}

test('only follows at the bottom, with tolerance for fractional pixels', () => {
  for (const gap of [0, 0.5, 1]) assert.equal(setup(gap).context.isAtBottom(), true);
  for (const gap of [2, 30, 149, 300]) {
    const { context, messages } = setup(gap);
    const originalTop = messages.scrollTop;
    assert.equal(context.isAtBottom(), false);
    context.scrollToBottom();
    assert.equal(messages.scrollTop, originalTop);
  }
});

for (const recovered of [false, true]) {
  test(`${recovered ? 'recovered' : 'live'} WIP growth preserves scrollback and follows only from bottom`, () => {
    const start = source.indexOf(recovered ? '  const updatePreview = () => {' : '  function updateThinkingPreview() {',
      recovered ? source.indexOf('async function reconnectPendingItem(') : 0);
    const end = source.indexOf(recovered ? '\n  };' : '\n  function freezeThinking()', start);
    const preview = source.slice(start, recovered ? end + 5 : end);
    for (const gap of [0, 30, 149, 300]) {
      const { messages, context } = setup(gap);
      const originalTop = messages.scrollTop;
      const content = { set textContent(value) { messages.scrollHeight += 220; }, scrollHeight: 220 };
      Object.assign(context, {
        thinkingFrozen: false, thinkingLoader: {}, statusBuf: 'Working', raw: 'Output',
        orderedPreview: (status, raw) => status + raw,
        thinkingContent: content, live: content, thinkingBubble: { style: {} }, wipBubble: {},
        waitingForUpdates: false, loader: null, updateThinkingHeightButton() {},
      });
      vm.runInContext(preview + (recovered ? '\nupdatePreview();' : '\nupdateThinkingPreview();'), context);
      assert.equal(messages.scrollTop, gap === 0 ? messages.scrollHeight : originalTop);
    }
  });
}

test('Flow completion preserves the insertion helper scroll position', async () => {
  const { context, messages } = setup(0);
  Object.assign(context, {
    messages: Object.assign(messages, { querySelector() { return null; } }),
    fetch: async () => ({ ok: true, json: async () => ({ status: 'done' }) }),
    shouldShowNewResponse: () => true,
    insertCompletedHistoryItem() { messages.scrollTop = 490; },
  });
  const start = source.indexOf('async function attachFlowStep(');
  const end = source.indexOf('// Squid Flow route chain', start);
  vm.runInContext(source.slice(start, end), context);
  await context.attachFlowStep(1);
  assert.equal(messages.scrollTop, 490);
});

test('WIP preview appends reasoning after earlier response text', () => {
  const context = vm.createContext({});
  vm.runInContext(source.slice(source.indexOf('function createWipPreview()'), source.indexOf('function isAtBottom()')), context);
  const preview = context.createWipPreview();
  assert.equal(preview('Thinking', ''), 'Thinking');
  assert.equal(preview('Thinking', 'Opening.'), 'Thinking\n\nOpening.');
  assert.equal(preview('ThinkingMore thinking', 'Opening.'), 'Thinking\n\nOpening.\n\nMore thinking');
  assert.equal(preview('ThinkingMore thinking', 'Opening.Answer'), 'Thinking\n\nOpening.\n\nMore thinking\n\nAnswer');
});

test('WIP status replacement preserves response position and subsequent arrival order', () => {
  const context = vm.createContext({});
  vm.runInContext(source.slice(source.indexOf('function createWipPreview()'), source.indexOf('function isAtBottom()')), context);
  const preview = context.createWipPreview();
  preview('Thinking', 'Opening.');
  preview('ThinkingMore reasoning', 'Opening.');
  assert.equal(preview('loading model…', 'Opening.'), 'Opening.\n\nloading model…');
  assert.equal(preview('loading model…', 'Opening.Answer'), 'Opening.\n\nloading model…\n\nAnswer');
  assert.equal(preview('', 'Opening.Answer'), 'Opening.Answer');
  assert.equal(preview('', 'Recovered answer'), 'Recovered answer');
});
