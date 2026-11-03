const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('ui/app.js', 'utf8');
const renderer = source.slice(source.indexOf('function _replaceMarkdownDiagram('), source.indexOf('function _renderMarkdownFilePreview('));
const setter = source.slice(source.indexOf('function setAssistantMarkdown('), source.indexOf('const HARNESS_MODEL_HINTS'));

function setup(render) {
  const context = vm.createContext({
    _loadMermaid: async () => ({ render }),
    _mermaidDiagramId: 0,
    messages: { contains: () => false },
    document: { createElement: () => ({}) },
  });
  vm.runInContext(renderer, context);
  return context;
}

function preview(texts) {
  const blocks = texts.map(textContent => ({ textContent, parentElement: {
    replaceWith(diagram) { this.diagram = diagram; },
  } }));
  return { blocks, isConnected: true, querySelectorAll: () => blocks,
    contains: code => blocks.includes(code) };
}

test('renders valid diagrams and preserves invalid source without blocking later diagrams', async () => {
  const context = setup(async (_, text) => {
    if (text === 'invalid') throw new Error('Syntax error');
    return { svg: '<svg></svg>' };
  });
  const container = preview(['invalid', 'flowchart LR\n A-->B']);
  await context._renderMarkdownDiagrams(container);
  assert.equal(container.blocks[0].parentElement.title, 'Mermaid: Syntax error');
  assert.equal(container.blocks[0].parentElement.diagram, undefined);
  assert.equal(container.blocks[1].parentElement.diagram.innerHTML, '<svg></svg>');
});

test('ignores stale blocks when a response is replaced during asynchronous rendering', async () => {
  let finish, started;
  const rendering = new Promise(resolve => { started = resolve; });
  const context = setup(() => new Promise(resolve => { finish = resolve; started(); }));
  const container = preview(['flowchart LR\n A-->B']);
  const pending = context._renderMarkdownDiagrams(container);
  await rendering;
  container.contains = () => false;
  finish({ svg: '<svg></svg>' });
  await pending;
  assert.equal(container.blocks[0].parentElement.diagram, undefined);
});

test('waits for bubble insertion before rendering and skips removed bubbles', () => {
  let frame;
  const rendered = [];
  const context = vm.createContext({
    renderAssistantMarkdown: text => `<p>${text}</p>`,
    requestAnimationFrame: callback => { frame = callback; },
    _renderMarkdownDiagrams: container => rendered.push(container),
  });
  vm.runInContext(setter, context);
  const container = { isConnected: false };
  context.setAssistantMarkdown(container, 'hello');
  assert.equal(container.innerHTML, '<p>hello</p>');
  assert.equal(rendered.length, 0);
  container.isConnected = true;
  frame();
  assert.equal(rendered.length, 1);
  context.setAssistantMarkdown(container, 'updated');
  container.isConnected = false;
  frame();
  assert.equal(rendered.length, 1);
});

for (const { label, top, bottom, delta, expected } of [
  { label: 'keeps the bottom pinned after expansion', top: 600, bottom: 350, delta: 250, expected: 850 },
  { label: 'compensates for expansion above the viewport', top: 300, bottom: -10, delta: 250, expected: 550 },
  { label: 'compensates for contraction above the viewport', top: 300, bottom: -10, delta: -100, expected: 200 },
  { label: 'keeps a visible diagram stationary', top: 300, bottom: 200, delta: 250, expected: 300 },
  { label: 'does not scroll for a diagram below the viewport', top: 300, bottom: 900, delta: 250, expected: 300 },
]) {
  test(label, () => {
    const context = setup(async () => ({ svg: '<svg></svg>' }));
    const scroller = {
      contains: () => true, scrollTop: top, scrollHeight: 1000, clientHeight: 400,
      getBoundingClientRect: () => ({ top: 0 }),
    };
    context.messages = scroller;
    const code = { parentElement: {
      getBoundingClientRect: () => ({ bottom }),
      replaceWith() { scroller.scrollHeight += delta; },
    } };
    context._replaceMarkdownDiagram(code, {});
    assert.equal(scroller.scrollTop, expected);
  });
}

test('uses the current scroll position when asynchronous rendering completes', async () => {
  let finish, started;
  const rendering = new Promise(resolve => { started = resolve; });
  const context = setup(() => new Promise(resolve => { finish = resolve; started(); }));
  const scroller = {
    contains: () => true, scrollTop: 600, scrollHeight: 1000, clientHeight: 400,
    getBoundingClientRect: () => ({ top: 0 }),
  };
  context.messages = scroller;
  const container = preview(['flowchart LR\n A-->B']);
  Object.assign(container.blocks[0].parentElement, {
    getBoundingClientRect: () => ({ bottom: 200 }),
    replaceWith() { scroller.scrollHeight += 250; },
  });
  const pending = context._renderMarkdownDiagrams(container);
  await rendering;
  scroller.scrollTop = 300;
  finish({ svg: '<svg></svg>' });
  await pending;
  assert.equal(scroller.scrollTop, 300);
});


test('polling leaves diagrams as source until the response completes', async () => {
  let tick;
  let status = 'running';
  let diagrams = 0;
  let loaders = 0;
  let stopped = false;
  const content = '```mermaid\nflowchart LR\n A-->B\n```';
  const context = vm.createContext({
    setInterval(callback) { tick = callback; return 1; },
    clearInterval() { stopped = true; },
    fetch: async () => ({ ok: true, json: async () => ({ status, content }) }),
    renderAssistantMarkdown: text => `<pre>${text}</pre>`,
    setAssistantMarkdown(container, text) { diagrams++; container.innerHTML = text; },
    addLoader() { loaders++; },
  });
  vm.runInContext(source.slice(source.indexOf('async function pollMessageStatus('), source.indexOf('function makeUserBubble(')), context);
  const container = { style: {} };
  await context.pollMessageStatus(1, container, {});
  await tick();
  await tick();
  assert.equal(diagrams, 0);
  assert.equal(loaders, 2);
  assert.equal(stopped, false);
  assert.equal(container.innerHTML, `<pre>${content}</pre>`);
  status = 'done';
  await tick();
  assert.equal(diagrams, 1);
  assert.equal(stopped, true);
  assert.equal(loaders, 2);
});
