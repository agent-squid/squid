const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const app = fs.readFileSync(path.join(__dirname, '../../ui/app.js'), 'utf8');
const queue = app.slice(app.indexOf('const _revertChecks ='), app.indexOf('function historyPromptTruncateLimit'));

test('200 diff blocks coalesce reconnect refreshes within four requests', async ({ page }) => {
  await page.setContent('<main id="messages"></main>');
  await page.addScriptTag({ content: queue });
  await page.evaluate(() => {
    window.started = []; window.pending = []; window.active = 0; window.peak = 0;
    window.fetch = async url => {
      started.push(url); active++; peak = Math.max(peak, active);
      await new Promise(resolve => pending.push(resolve));
      active--;
      return { ok: true, json: async () => ({}) };
    };
    for (let i = 0; i < 200; i++) {
      const block = document.createElement('div');
      block.className = 'tool-block'; block.dataset.msgId = i; block.dataset.repo = '/tmp/repo';
      document.querySelector('#messages').appendChild(block);
    }
    // A duplicate rendering of the same diff shares its request.
    document.querySelector('#messages').appendChild(document.querySelector('.tool-block').cloneNode(true));
    refreshAllRevertButtons();
    for (let i = 0; i < 10; i++) refreshAllRevertButtons({ force: true });
  });
  expect(await page.evaluate(() => started.length)).toBe(4);
  // Four active reads each need one replacement; queued reads stay deduplicated.
  for (let i = 0; i < 52; i++) {
    await page.evaluate(async () => {
      pending.splice(0).forEach(resolve => resolve());
      await new Promise(resolve => setTimeout(resolve, 0));
    });
  }
  expect(await page.evaluate(() => started.length)).toBe(204);
  expect(await page.evaluate(() => peak)).toBe(4);
  expect(await page.evaluate(() => active)).toBe(0);
});

test('view changes drop queued checks and failures release request slots', async ({ page }) => {
  await page.setContent('<main id="messages"></main>');
  await page.addScriptTag({ content: queue });
  await page.evaluate(() => {
    window.started = 0; window.pending = [];
    window.fetch = async () => {
      started++;
      await new Promise(resolve => pending.push(resolve));
      throw new Error('offline');
    };
    for (let i = 0; i < 200; i++) {
      const block = document.createElement('div');
      block.className = 'tool-block'; block.dataset.msgId = i; block.dataset.repo = '/tmp/repo';
      document.querySelector('#messages').appendChild(block);
    }
    refreshAllRevertButtons();
    document.querySelector('#messages').replaceChildren();
    pending.splice(0).forEach(resolve => resolve());
  });
  await expect.poll(() => page.evaluate(() => _revertChecksActive)).toBe(0);
  expect(await page.evaluate(() => started)).toBe(4);
  expect(await page.evaluate(() => _revertChecks.size)).toBe(0);
});
