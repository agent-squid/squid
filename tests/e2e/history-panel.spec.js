const { test, expect } = require('@playwright/test');
for (const mobile of [false, true]) test(`shared panel ${mobile}`, async ({page}) => {
 if(mobile) await page.setViewportSize({width:390,height:844});
 for(const endpoint of ['history','search','bookmarks']) await page.route(`**/${endpoint}**`,r=>r.fulfill({json:{items:[],has_more:false}}));
 await page.goto('/');
 await page.fill('#input','keep draft');
 await page.click('#chip-search-btn');
 await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
 await page.fill('#search-bar-scope','#demo*');
 await page.click('#search-bar-bookmarks');
 await page.fill('#search-bar-keywords','hello');
 await page.click('#search-bar-submit');
 await expect(page.locator('#chip-filter-btn')).toHaveClass(/active/);
 await page.click('#search-bar-clear');
 await expect(page.locator('#search-bar')).not.toBeVisible();
 await expect(page.locator('#chip-search-btn')).toHaveClass(/active/);
 await page.click('#chip-search-btn');
 await expect(page.locator('#search-bar-keywords')).toHaveValue('hello');
 await page.fill('#search-bar-keywords','');
 await page.press('#search-bar-keywords','Enter');
 await expect(page.locator('#chip-search-btn')).not.toHaveClass(/active/);
 await expect(page.locator('#chip-filter-btn')).toHaveClass(/active/);
 await page.click('#search-bar-reset');
 await expect(page.locator('#search-bar-bookmarks')).toHaveAttribute('aria-pressed', 'false');
 await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);
 await expect(page.locator('#input')).toHaveValue('keep draft');
 expect(await page.locator('#search-bar').evaluate(el=>el.getBoundingClientRect().right<=innerWidth)).toBe(true);
});

for (const mobile of [false, true]) {
 test(`blank route removes an applied restriction ${mobile}`, async ({ page }) => {
  if (mobile) await page.setViewportSize({ width: 390, height: 844 });
  for (const endpoint of ['history', 'search', 'bookmarks'])
   await page.route(`**/${endpoint}**`, r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');
  await page.click('#chip-search-btn');
  await page.fill('#search-bar-scope', '#demo*');
  await page.fill('#search-bar-keywords', 'hello');
  await page.click('#search-bar-submit');
  await expect(page.locator('#chip-filter-btn')).toHaveClass(/active/);
  await page.fill('#search-bar-scope', '');
  const request = page.waitForRequest(r => r.url().includes('/search?'));
  await page.click('#search-bar-submit');
  const params = new URL((await request).url()).searchParams;
  for (const key of ['topic', 'topic_subtree', 'agent', 'adhoc', 'flow_route']) expect(params.has(key)).toBe(false);
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
  await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);
 });

 test(`bad filter stays visible and removable during search ${mobile}`, async ({ page }) => {
  if (mobile) await page.setViewportSize({ width: 390, height: 844 });
  for (const endpoint of ['history', 'search', 'bookmarks'])
   await page.route(`**/${endpoint}**`, r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');
  await page.fill('#input', '/bad');
  await page.press('#input', 'Enter');
  await page.click('#chip-search-btn');
  const remove = page.getByRole('button', { name: 'Bad responses only', exact: true });
  await expect(remove).toHaveAttribute('aria-pressed', 'true');
  await page.fill('#search-bar-keywords', 'hello');
  const filtered = page.waitForRequest(r => r.url().includes('/search?'));
  await page.click('#search-bar-submit');
  expect(new URL((await filtered).url()).searchParams.get('marked_bad')).toBe('true');
  await expect(remove).toBeVisible();
  const unfiltered = page.waitForRequest(r => r.url().includes('/search?'));
  await remove.click();
  expect(new URL((await unfiltered).url()).searchParams.has('marked_bad')).toBe(false);
  await expect(remove).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('#filter-badge')).not.toBeVisible();
  await expect(page.locator('#chip-search-btn')).toHaveClass(/active/);
 });
}

for (const mobile of [false, true]) test(`icon toggles combine and sync shortcuts ${mobile}`, async ({ page }) => {
 if (mobile) await page.setViewportSize({ width: 390, height: 844 });
 for (const endpoint of ['history', 'search', 'bookmarks'])
  await page.route(`**/${endpoint}**`, r => r.fulfill({ json: { items: [], has_more: false } }));
 await page.goto('/');
 await page.click('#chip-search-btn');
 await page.fill('#search-bar-scope', '#demo*');
 await page.fill('#search-bar-keywords', 'hello');
 await page.selectOption('#search-bar-operator', 'or');
 await page.click('#search-bar-submit');
 await page.click('#search-bar-bookmarks');
 const combined = page.waitForRequest(r => r.url().includes('/search?') && r.url().includes('marked_bad=true'));
 await page.click('#search-bar-bad');
 const params = new URL((await combined).url()).searchParams;
 for (const [key, value] of Object.entries({topic:'demo', topic_subtree:'true', q:'hello', operator:'or', bookmarked:'true', marked_bad:'true'})) expect(params.get(key)).toBe(value);
 await expect(page.locator('#search-bar-bookmarks')).toHaveAttribute('aria-pressed', 'true');
 await expect(page.locator('#search-bar-bad')).toHaveAttribute('aria-pressed', 'true');
 await expect(page.locator('#chip-bookmark-btn')).toHaveAttribute('aria-pressed', 'true');
 await page.click('#chip-bookmark-btn');
 await expect(page.locator('#search-bar-bookmarks')).toHaveAttribute('aria-pressed', 'false');
 await expect(page.locator('#search-bar-bad')).toHaveAttribute('aria-pressed', 'true');
 await page.click('#search-bar-reset');
 await expect(page.locator('#search-bar-bad')).toHaveAttribute('aria-pressed', 'false');
 // Without keywords, toggles keep the panel and its unfinished fields open.
 await page.fill('#search-bar-keywords', 'unfinished');
 await page.click('#search-bar-bookmarks');
 await page.click('#search-bar-bad');
 await expect(page.locator('#search-bar-keywords')).toHaveValue('unfinished');
 await expect(page.locator('#search-bar')).toBeVisible();
});

for (const mobile of [false, true]) {
 test(`clearing toolbar route updates open panel ${mobile}`, async ({ page }) => {
  if (mobile) await page.setViewportSize({ width: 390, height: 844 });
  for (const endpoint of ['history', 'search', 'bookmarks'])
   await page.route(`**/${endpoint}**`, r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');
  await page.click('#chip-search-btn');
  await page.fill('#search-bar-scope', '#demo*');
  await page.click('#search-bar-submit');
  await page.fill('#search-bar-keywords', 'unfinished');
  await page.click('#chip-filter-btn');
  await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
  await expect(page.locator('#search-bar-keywords')).toHaveValue('unfinished');
  const request = page.waitForRequest(r => r.url().includes('/search?'));
  await page.click('#search-bar-submit');
  const params = new URL((await request).url()).searchParams;
  expect(params.get('q')).toBe('unfinished');
  for (const key of ['topic', 'topic_subtree', 'agent', 'adhoc', 'flow_route']) expect(params.has(key)).toBe(false);
 });

 for (const toggle of ['bookmarks', 'bad']) test(`clear stale search notices on ${toggle} toggle ${mobile}`, async ({ page }) => {
  if (mobile) await page.setViewportSize({ width: 390, height: 844 });
  for (const endpoint of ['history', 'bookmarks'])
   await page.route(`**/${endpoint}**`, r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/search**', r => r.fulfill({ json: { items: [] } }));
  await page.goto('/');
  await page.click('#chip-search-btn');
  await page.fill('#search-bar-keywords', 'hello');
  await page.click('#search-bar-submit');
  await expect(page.locator('.search-no-results')).toHaveCount(1);
  // Hold the next response to check cleanup before replacement notices arrive.
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/search**', async r => { await gate; await r.fulfill({ json: { items: [] } }); });
  await page.evaluate(() => {
   const cap = document.createElement('div');
   cap.className = 'cmd-feedback search-no-results';
   cap.textContent = 'Showing top 100 results — add keywords to narrow.';
   document.getElementById('messages').appendChild(cap);
  });
  await page.click(`#search-bar-${toggle}`);
  await expect(page.locator('.search-no-results')).toHaveCount(0);
  release();
  await expect(page.locator('.search-no-results')).toHaveCount(1);
  await expect(page.locator('.search-no-results')).toHaveText('No results for "hello"');
 });
}

for (const width of [280, 320, 375, 390, 600, 768, 1280]) test(`all history controls fit one row at ${width}`, async ({ page }) => {
 await page.setViewportSize({ width, height: 844 });
 for (const endpoint of ['history', 'search', 'bookmarks'])
  await page.route(`**/${endpoint}**`, r => r.fulfill({ json: { items: [], has_more: false } }));
 await page.goto('/');
 await page.click('#chip-search-btn');
 await expect(page.getByRole('button', { name: 'Clear all history filters', exact: true })).toBeVisible();
 await expect(page.locator('#search-bar-reset .material-symbols-outlined')).toHaveText('restart_alt');
 await expect(page.locator('#search-bar-operator option')).toHaveText(width <= 600 ? ['&&', '||'] : ['AND', 'OR']);
 const layout = await page.locator('#search-bar').evaluate(bar => {
  const bounds = bar.getBoundingClientRect();
  if (getComputedStyle(bar).display !== 'grid') throw new Error('History controls must use a single grid row');
  return [...bar.children].map(el => {
   const rect = el.getBoundingClientRect();
   return { centered: Math.abs((rect.top + rect.bottom) / 2 - (bounds.top + bounds.bottom) / 2) < 1,
    fits: rect.left >= bounds.left && rect.right <= bounds.right && rect.right <= innerWidth,
    width: rect.width };
  });
 });
 for (const control of layout) {
  expect(control.centered).toBe(true);
  expect(control.fits).toBe(true);
  expect(control.width).toBeGreaterThan(20);
 }
 await page.selectOption('#search-bar-operator', 'or');
 await page.setViewportSize({ width: width <= 600 ? 1280 : 375, height: 844 });
 await expect(page.locator('#search-bar-operator')).toHaveValue('or');
 await expect(page.locator('#search-bar-operator option')).toHaveText(width <= 600 ? ['AND', 'OR'] : ['&&', '||']);
});

for (const mobile of [false, true]) test(`composer route supplies suggestions without applying a filter ${mobile}`, async ({ page }) => {
 if (mobile) await page.setViewportSize({ width: 375, height: 844 });
 for (const endpoint of ['history', 'search', 'bookmarks'])
  await page.route(`**/${endpoint}**`, r => r.fulfill({ json: { items: [], has_more: false } }));
 await page.goto('/');
 await page.fill('#input', '#squid.ux@claude! draft');
 await expect(page.locator('#input')).toHaveValue('draft');
 await page.click('#chip-search-btn');
 await expect(page.locator('#search-bar-scope')).toHaveValue('#all');
 await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);
 await page.click('#search-scope-toggle');
 const values = ['#all', '#squid.ux@claude', '#squid.ux@claude!', '#squid.ux@claude*', '#squid.ux', '#squid.ux*', '#squid*', '@claude', '@claude!', '@claude*'];
 await expect.poll(() => page.locator('#search-scope-options [role="option"]').evaluateAll(nodes => nodes.map(n => n.dataset.scope))).toEqual(values);
 await expect(page.locator('#input')).toHaveValue('draft');
 await page.locator('#search-scope-options [data-scope="#squid.ux@claude*"]').click();
 const request = page.waitForRequest(r => r.url().includes('/history?') && r.url().includes('agent=claude'));
 await page.click('#search-bar-submit');
 const params = new URL((await request).url()).searchParams;
 expect(params.get('topic')).toBe('squid.ux');
 expect(params.has('adhoc')).toBe(false);
 await expect(page.locator('#chip-filter-btn')).toHaveClass(/active/);
 const order = await page.locator('#search-bar').evaluate(bar => [...bar.children].slice(0, 3).map(el => el.id));
 expect(order).toEqual(['search-bar-bookmarks', 'search-bar-bad', 'search-scope-picker']);
});
