const { test, expect } = require('@playwright/test');

async function mockBackend(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    '#other@codex push the changes',
    '#squid@haiku! push the changes',
    '#squid@codex push the changes',
    '#squid@codex inspect the changes',
  ] } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
}

test('typed prompt prefixes show unique routed history with the current route first', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'push');

  const items = page.locator('#autocomplete .ac-item');
  await expect(items).toHaveCount(3);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Recent Prompts');
  // same-route item has no route chip — identified by absence of .ac-route-btn
  const currentRouteItem = page.locator('#autocomplete .ac-item:not(:has(.ac-route-btn))');
  const olderRouteItem = page.locator('#autocomplete .ac-item', { hasText: '#squid@haiku! push the changes' });
  await expect(currentRouteItem).toBeVisible();
  await expect(page.locator('#autocomplete .ac-item', { hasText: '#other@codex push the changes' })).toBeVisible();
  await expect(olderRouteItem).toBeVisible();
  await expect.poll(async () => {
    const currentBox = await currentRouteItem.boundingBox();
    const olderBox = await olderRouteItem.boundingBox();
    return currentBox && olderBox ? currentBox.y > olderBox.y : false;
  }).toBe(true);
  await expect(page.locator('#autocomplete .ac-item.selected .ac-route-btn')).toHaveCount(0);
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('push the changes');
  await page.locator('#input').press('ArrowUp');
  await expect(page.locator('#autocomplete .ac-item.selected')).toContainText('#other@codex push the changes');
  await page.locator('#input').press('ArrowDown');
  await expect(page.locator('#autocomplete .ac-item.selected .ac-route-btn')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Close suggestions' })).toBeVisible();

  await olderRouteItem.locator('.ac-route-btn').click();
  await expect(page.locator('#input')).toHaveValue('push the changes');
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@haiku!');
});

test('different-route items show a route chip button; same-route items do not', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'push');

  // same-route item has no route button
  const sameRouteItem = page.locator('#autocomplete .ac-item:not(:has(.ac-route-btn))');
  await expect(sameRouteItem).toBeVisible();
  await expect(sameRouteItem.locator('.ac-route-btn')).toHaveCount(0);

  // different-route items each have a route button showing their slug
  await expect(page.locator('#autocomplete .ac-item', { hasText: '#other@codex' }).locator('.ac-route-btn')).toBeVisible();
  await expect(page.locator('#autocomplete .ac-item', { hasText: '#other@codex' }).locator('.ac-route-btn')).toContainText('#other@codex');
  await expect(page.locator('#autocomplete .ac-item', { hasText: '#squid@haiku!' }).locator('.ac-route-btn')).toBeVisible();
  await expect(page.locator('#autocomplete .ac-item', { hasText: '#squid@haiku!' }).locator('.ac-route-btn')).toContainText('#squid@haiku!');
});

test('default topic prompt history distinguishes agent routes', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#default@agentA compare models',
    '#default@agentB compare models',
    'compare bare default',
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'default', agent: 'agentB', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'compare');

  const sameRouteItem = page.locator('#autocomplete .ac-item:not(:has(.ac-route-btn))', { hasText: 'compare models' });
  await expect(sameRouteItem).toBeVisible();
  await expect(sameRouteItem).not.toContainText('#default@agentB');

  const otherRouteItem = page.locator('#autocomplete .ac-item', { hasText: '#default@agentA compare models' });
  await expect(otherRouteItem.locator('.ac-route-btn')).toBeVisible();
  await expect(otherRouteItem.locator('.ac-route-btn')).toContainText('#default@agentA');
});

test('adhoc prompt history route chips ignore lookback counts', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@codex!2 reuse context',
    '#squid@haiku!3 reuse context',
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: true, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'reuse');

  const sameRouteItem = page.locator('#autocomplete .ac-item:not(:has(.ac-route-btn))', { hasText: 'reuse context' });
  await expect(sameRouteItem).toBeVisible();
  await expect(sameRouteItem).not.toContainText('#squid@codex');

  const otherRouteItem = page.locator('#autocomplete .ac-item', { hasText: '#squid@haiku!' });
  await expect(otherRouteItem.locator('.ac-route-btn')).toBeVisible();
  await expect(otherRouteItem.locator('.ac-route-btn')).toContainText('#squid@haiku!');
  await expect(otherRouteItem.locator('.ac-route-btn')).not.toContainText('!3');
});

test('clicking a different-route item body inserts prompt only without changing the route', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'push');
  const otherRouteItem = page.locator('#autocomplete .ac-item', { hasText: '#other@codex push the changes' });
  await otherRouteItem.locator('.ac-history-prompt').click();

  await expect(page.locator('#input')).toHaveValue('push the changes');
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex');
});

test('Tab completion converts a routed prompt history slug into the topic chip', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('push');
  await composer.press('Tab');

  await expect(composer).toHaveValue('push the changes');
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex');
});

test('clicking prompt history normalizes adhoc lookback out of the converted chip', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@haiku!3 review the changes',
  ] } }));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('review');
  await expect(page.locator('#autocomplete .ac-item .ac-route-btn')).toContainText('#squid@haiku!');
  await page.locator('#autocomplete .ac-item .ac-route-btn').click();

  await expect(composer).toHaveValue('review the changes');
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@haiku!');
});

test('ArrowUp converts recalled prompt routes to chips and ArrowDown restores the draft', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', route => route.fulfill({ json: { items: [
    '#squid@haiku!3 review the changes',
    '#other@codex inspect the changes',
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'draft', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('unfinished draft');
  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('ArrowUp');

  await expect(composer).toHaveValue('review the changes');
  await expect(page.locator('#topic-chip')).toContainText('#squid@haiku!');

  await composer.press('ArrowUp');
  await expect(composer).toHaveValue('inspect the changes');
  await expect(page.locator('#topic-chip')).toContainText('#other@codex');

  await composer.press('ArrowDown');
  await expect(composer).toHaveValue('review the changes');
  await expect(page.locator('#topic-chip')).toContainText('#squid@haiku!');

  await composer.press('ArrowDown');
  await expect(composer).toHaveValue('unfinished draft');
  await expect(page.locator('#topic-chip')).toContainText('#draft@codex');
});

test('ArrowUp moves within a multiline draft before recalling prompt history', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('fix this word\nkeep editing here');
  const endPosition = await composer.evaluate(input => {
    input.setSelectionRange(input.value.length, input.value.length);
    return input.selectionStart;
  });

  await composer.press('ArrowUp');

  await expect(composer).toHaveValue('fix this word\nkeep editing here');
  await expect.poll(() => composer.evaluate(input => input.selectionStart)).toBeLessThan(endPosition);
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);

  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('ArrowUp');
  await expect(composer).toHaveValue('push the changes');
  await expect(page.locator('#topic-chip')).toContainText('#other@codex');

  await composer.press('ArrowDown');
  await expect(composer).toHaveValue('fix this word\nkeep editing here');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
});

test('autocomplete can be dismissed with its touch-accessible close button', async ({ page }) => {
  await mockBackend(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');

  await page.fill('#input', 'push');
  await expect(page.locator('.ac-close-mobile')).toHaveText('×');
  await expect(page.locator('.ac-close-mobile')).toBeVisible();
  await page.getByRole('button', { name: 'Close suggestions' }).click();

  await expect(page.locator('#autocomplete')).not.toHaveClass(/open/);
  await expect(page.locator('#input')).toBeFocused();
  await expect(page.locator('#input')).toHaveValue('push');
});

test('plain Enter sends the message without selecting from autocomplete', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/chat**', r => r.fulfill({ json: {} }));
  await page.goto('/');

  await page.fill('#input', 'push');
  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await page.keyboard.press('Enter');

  await expect(page.locator('#input')).toHaveValue('');
  await expect(page.locator('#autocomplete')).not.toHaveClass(/open/);
});

test('search composer action prefills the effective default agent route', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/topics', r => r.fulfill({ json: [
    { name: 'default', agent: 'codex', sticky_adhoc: false, last_model: null, last_backend: 'codex', queue_depth: 0, active: false },
  ] }));
  await page.goto('/');

  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
  await page.fill('#input', 'needle');
  await page.locator('#chip-search-btn').click();

  await expect(page.locator('#input')).toHaveValue('/s #default@codex needle');
});

test('clear composer action inserts /clear and ArrowUp restores the draft', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('draft before clear');
  await page.locator('#chip-clear-btn').click();

  await expect(composer).toHaveValue('/clear');
  await composer.press('ArrowUp');
  await expect(composer).toHaveValue('draft before clear');
});

test('stash composer action adds the prompt to recall and ArrowUp restores it', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('remember this draft');
  await page.locator('#chip-stash-btn').click();

  await expect(composer).toHaveValue('');
  await expect(page.locator('#autocomplete')).toHaveClass(/open/);
  await expect(page.locator('#autocomplete .ac-item', { hasText: 'remember this draft' })).toBeVisible();

  await composer.press('ArrowUp');
  await expect(composer).toHaveValue('remember this draft');
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex');
});

test('stashing an existing route prompt moves one copy to the latest position', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    '#squid@codex older prompt',
    '#squid@codex Push the change and update the v0.1 to latest commit.',
  ] } }));
  await page.addInitScript(() => {
    localStorage.setItem('squid_stashed_prompts', JSON.stringify([
      '#squid@codex Push the change and update the v0.1 to latest commit.',
      '#squid@codex another stashed prompt',
    ]));
    localStorage.setItem('squid_sticky_chip', JSON.stringify({
      topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
    }));
  });
  await page.goto('/');

  await page.fill('#input', 'Push the change and update the v0.1 to latest commit.');
  await page.locator('#chip-stash-btn').click();

  const stashed = await page.evaluate(() => JSON.parse(localStorage.getItem('squid_stashed_prompts')));
  expect(stashed).toEqual([
    '#squid@codex Push the change and update the v0.1 to latest commit.',
    '#squid@codex another stashed prompt',
  ]);
  await expect(page.locator('#autocomplete .ac-item', { hasText: 'Push the change and update the v0.1 to latest commit.' })).toHaveCount(1);
});

test('composer action titles keep the restored chip route over delayed default fallback', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/topics', async route => {
    await new Promise(resolve => setTimeout(resolve, 150));
    await route.fulfill({ json: [
      { name: 'default', agent: 'claude', sticky_adhoc: true, last_model: null, last_backend: 'claude', queue_depth: 0, active: false },
    ] });
  });
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: true, lookback: 1,
  })));
  await page.goto('/');

  await expect(page.locator('#topic-chip')).toContainText('#squid@codex!1');
  await expect(page.locator('#chip-stash-btn')).toHaveAttribute('title', 'Stash prompt for autocomplete (#squid@codex!)');
  await page.waitForTimeout(250);
  await expect(page.locator('#chip-stash-btn')).toHaveAttribute('title', 'Stash prompt for autocomplete (#squid@codex!)');
});

test('prompt autocomplete dedupes normalized stashed and recent routes', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    '#squid@codex! reuse context',
  ] } }));
  await page.addInitScript(() => {
    localStorage.setItem('squid_stashed_prompts', JSON.stringify([
      '#squid@codex!1 reuse context',
    ]));
    localStorage.setItem('squid_sticky_chip', JSON.stringify({
      topic: 'squid', agent: 'codex', adhoc: true, lookback: 1,
    }));
  });
  await page.goto('/');

  await page.fill('#input', 'reuse');
  await expect(page.locator('#autocomplete .ac-item', { hasText: 'reuse context' })).toHaveCount(1);
});

test('prompt autocomplete dedupes exact duplicate recent prompts', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [
    '#squid@codex Push the change and update the v0.1 to latest commit.',
    '#squid@codex Push the change and update the v0.1 to latest commit.',
  ] } }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  await page.fill('#input', 'Push');
  await expect(page.locator('#autocomplete .ac-item', { hasText: 'Push the change and update the v0.1 to latest commit.' })).toHaveCount(1);
});

test('bare default prompts are recorded without a default route chip', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
  await page.route('**/chat**', r => r.fulfill({ json: {} }));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('plain prompt');
  await page.keyboard.press('Enter');
  await expect(composer).toHaveValue('');

  await composer.fill('plain');
  const item = page.locator('#autocomplete .ac-item', { hasText: 'plain prompt' });
  await expect(item).toBeVisible();
  await expect(item.locator('.ac-route-btn')).toHaveCount(0);
  await expect(item).not.toContainText('#default');
});

for (const { name, chip, route } of [
  {
    name: 'topic and agent',
    chip: { topic: 'squid', agent: 'codex', adhoc: false, lookback: 0 },
    route: '#squid@codex',
  },
  {
    name: 'adhoc lookback',
    chip: { topic: 'squid', agent: 'codex', adhoc: true, lookback: 3 },
    route: '#squid@codex!3',
  },
]) {
  test(`Backspace at the start of a populated prompt expands the ${name} chip`, async ({ page }) => {
    await mockBackend(page);
    await page.addInitScript(value => {
      localStorage.setItem('squid_sticky_chip', JSON.stringify(value));
    }, chip);
    await page.goto('/');

    const composer = page.locator('#input');
    await composer.fill('fix the bug');
    await composer.evaluate(input => input.setSelectionRange(0, 0));
    await composer.press('Backspace');

    await expect(composer).toHaveValue(`${route} fix the bug`);
    await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
    await expect.poll(() => composer.evaluate(input => input.selectionStart)).toBe(route.length);
    await expect.poll(() => composer.evaluate(input => input.selectionEnd)).toBe(route.length);
  });
}

test('Backspace on an empty prompt keeps expanding the topic chip as before', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: true, lookback: 3,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.focus();
  await composer.press('Backspace');

  await expect(composer).toHaveValue('#squid@codex!3');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
});

test('Backspace inside a populated prompt keeps the topic chip and edits normally', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('fix the bug');
  await composer.evaluate(input => input.setSelectionRange(4, 4));
  await composer.press('Backspace');

  await expect(composer).toHaveValue('fixthe bug');
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
});

test('moving from an expanded slug into the prompt restores the chip', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: true, lookback: 3,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('fix the bug');
  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('Backspace');
  await composer.press('ArrowRight');

  await expect(composer).toHaveValue('fix the bug');
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex!3');
  await expect.poll(() => composer.evaluate(input => input.selectionStart)).toBe(0);
});

test('restored chip uses the edited slug', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('fix the bug');
  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('Backspace');
  await composer.evaluate(input => {
    input.value = '#other@haiku!2 fix the bug';
    input.setSelectionRange(input.value.length, input.value.length);
    input.dispatchEvent(new Event('input'));
  });

  await expect(composer).toHaveValue('fix the bug');
  await expect(page.locator('#topic-chip')).toContainText('#other@haiku!2');
});

test('slug autocomplete stays separate from prompt history and preserves the prompt', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/topics', route => route.fulfill({ json: [
    { name: 'other', agent: 'haiku', last_prompt: 'previous topic prompt', active: false },
  ] }));
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('fix the bug');
  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('Backspace');
  await composer.evaluate(input => {
    input.value = '#ot fix the bug';
    input.setSelectionRange(3, 3);
    input.dispatchEvent(new Event('input'));
  });

  const items = page.locator('#autocomplete .ac-item');
  await expect(items).toHaveCount(1);
  await expect(page.locator('#autocomplete .ac-title')).toHaveText('Routes');
  await expect(items.first()).toContainText('#other@haiku');
  await expect(items.first()).toContainText('last previous topic prompt');
  await expect(items.first()).not.toContainText('push the changes');
  await items.first().click();

  await expect(composer).toHaveValue('#other fix the bug');
  await expect.poll(() => composer.evaluate(input => input.selectionStart)).toBe('#other'.length);
});

test('an invalid edited slug remains expanded on blur', async ({ page }) => {
  await mockBackend(page);
  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');

  const composer = page.locator('#input');
  await composer.fill('fix the bug');
  await composer.evaluate(input => input.setSelectionRange(0, 0));
  await composer.press('Backspace');
  await composer.evaluate(input => {
    input.value = '#squid@ fix the bug';
    input.dispatchEvent(new Event('input'));
  });
  await page.locator('#messages').click();

  await expect(composer).toHaveValue('#squid@ fix the bug');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
});
