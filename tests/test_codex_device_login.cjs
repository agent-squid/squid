const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const source = readFileSync(`${__dirname}/../ui/app.js`, 'utf8');
const parser = source.slice(source.indexOf('function authPromptText('), source.indexOf('// Recover completion'));
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
  const ctx = vm.createContext({ term, deviceLogin:true, claudeLogin:false, _authSession:{term,running:true},
    queueMicrotask: fn => queued.push(fn), finishExit:(code, options)=>completions.push([code,options.cancelSession]) });
  vm.runInContext(parser, ctx);
  vm.runInContext(source.slice(source.indexOf('  if (deviceLogin) term.onWriteParsed('), source.indexOf('  const fitAddon = new FitAddon.FitAddon();')), ctx);
  callback(); assert.equal(completions.length, 0);
  queued.shift()(); assert.deepEqual(completions, [[0,false]]);
  ctx._authSession = { term:{} }; callback(); assert.equal(queued.length, 0);
});

test('Claude and Codex show success once, then close and retry without cancelling the CLI', () => {
  for (const claudeLogin of [true, false]) {
    for (const manualClose of [true, false]) {
      let visible = true; let disposed = false; let retried = 0; let cancelled = 0;
      const feedback = []; const timers = [];
      const panel = { classList:{ remove: () => { visible = false; } } };
      const term = { dispose: () => { disposed = true; } };
      const session = { id:'auth-test', term, panel, mode:'login', running:true, transport:'ws',
        stopCompletionMonitor:()=>{}, onSuccessRetry:()=>{retried++} };
      const ctx = vm.createContext({ _authSession:session, term, deviceLogin:true, claudeLogin, isCatalogOperation:false,
        panelTitle:{}, deviceStatus:{}, deviceLink:{}, deviceCodeRow:{}, claudeCodeRow:{}, panelRetryBtn:{},
        showCmdFeedback:text=>{feedback.push(text);return {setAttribute:()=>{}}},
        setTimeout:(fn,delay)=>{assert.equal(delay,800);timers.push(fn);return 1}, clearTimeout:()=>{},
        window:{removeEventListener:()=>{}}, realtimeV1:{clearAuth:()=>{}, authSend:()=>{cancelled++}},
        form:{classList:{remove:()=>{}}}, authPanel:panel });
      const finish = source.slice(source.indexOf('  const finishExit = '), source.indexOf('  // cols/rows travel with the spawn request'));
      const close = source.slice(source.indexOf('async function closeAuthPanel('), source.indexOf('authPanelCancelBtn.addEventListener('));
      vm.runInContext(close + finish + '\nfinishExit(0); finishExit(0);', ctx);
      const label = claudeLogin ? 'Claude' : 'Codex';
      assert.equal(ctx.panelTitle.textContent, `Signed in to ${label} ✓`);
      assert.deepEqual(feedback, [`${label} login successful. Retrying your request…`]);
      assert.equal(visible,true); assert.equal(retried,0);
      if (manualClose) vm.runInContext('closeAuthPanel()',ctx);
      timers.shift()();
      assert.equal(visible,false); assert.equal(ctx._authSession,null);
      assert.equal(disposed,true); assert.equal(retried,1); assert.equal(cancelled,0);
    }
  }
});

test('Claude login URL survives terminal wrapping and rejects unrelated origins', () => {
  const ctx = vm.createContext({ URL }); vm.runInContext(parser, ctx);
  const url = 'https://claude.ai/oauth/authorize?client_id=test&code_challenge=challenge&state=state';
  for (const endpoint of ['https://claude.ai/oauth/authorize', 'https://claude.com/cai/oauth/authorize', 'https://platform.claude.com/oauth/authorize']) {
    const candidate = url.replace('https://claude.ai/oauth/authorize', endpoint);
    assert.equal(ctx.claudeWebPrompt(buffer([[candidate.slice(0,60)], [candidate.slice(60),true]])).url, candidate);
  }
  for (const invalid of [url.replace('claude.ai','claude.ai.evil.test'), url.replace('/oauth/authorize','/other'), 'https://claude.ai/oauth/authorize']) {
    assert.equal(ctx.claudeWebPrompt(buffer([[invalid]])).url, null);
  }
});

test('Claude code submission preserves the full code, clears it after sending, and rejects terminal controls', async () => {
  const submitSource = source.slice(source.indexOf('  claudeCodeSubmit.onclick = '), source.indexOf('  claudeCodeInput.onkeydown = '));
  const term = {}; const session = {id:'claude-session',term,running:true};
  const requests = [];
  const input = {value:' code#state ',disabled:false}; const button = {disabled:false};
  const ctx = vm.createContext({term,_authSession:session,claudeCodeInput:input,claudeCodeSubmit:button,
    deviceStatus:{textContent:''},fetch:async (url, options)=>{requests.push([url,JSON.parse(options.body)]);return {ok:true};}});
  vm.runInContext(submitSource,ctx);
  await button.onclick();
  assert.deepEqual(requests,[['/auth/session/claude-session/input',{data:'code#state\r'}]]);
  assert.equal(input.value,''); assert.equal(button.disabled,false);
  input.value='code\nextra'; await button.onclick(); assert.equal(requests.length,1);
  input.value='code'; ctx._authSession={...session,term:{}}; await button.onclick(); assert.equal(requests.length,1);
});
