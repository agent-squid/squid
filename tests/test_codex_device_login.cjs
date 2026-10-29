const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const source = readFileSync(`${__dirname}/../ui/app.js`, 'utf8');
const parser = source.slice(source.indexOf('function codexDevicePrompt('), source.indexOf('// Recover completion'));
function buffer(rows) {
  return { length: rows.length, getLine: i => ({
    isWrapped: rows[i][1] || false, translateToString: () => rows[i][0],
  }) };
}
test('recognizes complete success lines, including terminal wrapping', () => {
  const ctx = vm.createContext({}); vm.runInContext(parser, ctx);
  for (const rows of [
    [['Successfully logged in']],
    [['Successfully logged '], ['in', true]],
    [['Waiting'], ['  Successfully logged in.  ']],
  ]) assert.equal(ctx.codexDevicePrompt(buffer(rows)).success, true);
  for (const text of ['Successfully logged', 'Not Successfully logged in', 'If Successfully logged in', 'Successfully logged in failed']) {
    assert.equal(ctx.codexDevicePrompt(buffer([[text]])).success, false);
  }
});
test('parsed success completes immediately without cancelling the CLI; stale terminals are ignored', () => {
  let callback; const queued = []; const completions = [];
  const term = { buffer: { active: buffer([['Successfully logged in']]) }, onWriteParsed: fn => { callback = fn; } };
  const ctx = vm.createContext({ term, deviceLogin:true, _authSession:{term},
    queueMicrotask: fn => queued.push(fn), finishExit:(code, options)=>completions.push([code,options.cancelSession]) });
  vm.runInContext(parser, ctx);
  vm.runInContext(source.slice(source.indexOf('  if (deviceLogin) term.onWriteParsed('), source.indexOf('  const fitAddon = new FitAddon.FitAddon();')), ctx);
  callback(); assert.equal(completions.length, 0);
  queued.shift()(); assert.deepEqual(completions, [[0,false]]);
  ctx._authSession = { term:{} }; callback(); assert.equal(queued.length, 0);
});

test('actual success handler closes the panel and retries once without a toast dependency', () => {
  for (const cancelSession of [true, false]) {
    let visible = true; let disposed = false; let retried = 0; let cancelled = 0; let stopped = false;
    const panel = { classList:{ remove: name => { assert.equal(name, 'open'); visible = false; } } };
    const term = { dispose: () => { disposed = true; } };
    const session = { id:'codex-test', term, panel, mode:'login', running:true, transport:'ws',
      stopCompletionMonitor:()=>{stopped=true}, onSuccessRetry:()=>{retried++} };
    const ctx = vm.createContext({ _authSession:session, term, deviceLogin:true, isCatalogOperation:false,
      window:{removeEventListener:()=>{}}, realtimeV1:{clearAuth:()=>{}, authSend:()=>{cancelled++}},
      form:{classList:{remove:()=>{}}}, authPanel:panel });
    const finish = source.slice(source.indexOf('  const finishExit = '), source.indexOf('  // cols/rows travel with the spawn request'));
    const close = source.slice(source.indexOf('async function closeAuthPanel('), source.indexOf('authPanelCancelBtn.addEventListener('));
    vm.runInContext(close + finish + `\nfinishExit(0, {cancelSession:${cancelSession}}); finishExit(0);`, ctx);
    assert.equal(visible, false);
    assert.equal(ctx._authSession, null);
    assert.equal(disposed, true);
    assert.equal(stopped, true);
    assert.equal(retried, 1);
    assert.equal(cancelled, cancelSession ? 1 : 0);
  }
});
