const { test, expect } = require('@playwright/test');

test.beforeEach(async ({ page }) => {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));
});

test('Squid Flow view shows a single sorted canonical key alongside the per-branch breakdown', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Menu' }).click();
  await page.getByRole('button', { name: 'Squid Flow' }).click();
  await expect(page.locator('#view-flow')).toHaveClass(/active/);

  // Two independent branches, entered out of sorted order.
  await page.fill('#flow-input', '#t2@a1,#t1@a2>#t3');

  const keyLine = page.locator('#flow-key-line .flow-route-line');
  await expect(keyLine).toHaveCount(1);
  // The key is built from the parsed clause (sorted origins, shared hop),
  // not by flattening + sorting the two expanded branches — so it stays as
  // condensed as the input instead of repeating the shared ">#t3" hop twice.
  await expect(keyLine).toHaveText('#t1@a2,#t2@a1>#t3');

  // The interim breakdown keeps each branch's reduced form in input order.
  const branchLines = page.locator('#flow-canonical-list .flow-route-line');
  await expect(branchLines).toHaveCount(2);
  await expect(branchLines.nth(0)).toHaveText('#t2@a1>#t3');
  await expect(branchLines.nth(1)).toHaveText('#t1@a2>#t3');
});

test('Squid Flow canonical key is order-independent for the same workflow', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Menu' }).click();
  await page.getByRole('button', { name: 'Squid Flow' }).click();

  const keyLine = page.locator('#flow-key-line .flow-route-line');

  // Both origins share agent a1, so the key also drops the redundant
  // repeated agent (one origin becomes the anchor, the other goes bare) —
  // regardless of which order they were typed in.
  await page.fill('#flow-input', '#t1@a1,#t2@a1');
  await expect(keyLine).toHaveText('#t1@a1,#t2');

  await page.fill('#flow-input', '#t2@a1,#t1@a1');
  await expect(keyLine).toHaveText('#t1@a1,#t2');
});

test('Squid Flow canonical key drops a target field only against the parent state, idempotently', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Menu' }).click();
  await page.getByRole('button', { name: 'Squid Flow' }).click();

  const keyLine = page.locator('#flow-key-line .flow-route-line');

  // Redundantly repeating the origin's topic on the target reduces away —
  // the target's only legal source of a dropped field is the fixed parent
  // state (the edge's source), never a sibling.
  await page.fill('#flow-input', '#squid@codex<2>#squid@review!');
  await expect(keyLine).toHaveText('#squid@codex<2>@review!');

  // Already-bare input is a fixed point: re-deriving the key from the
  // resolved value must not re-add the redundant topic.
  await page.fill('#flow-input', '#squid@codex<2>@review!');
  await expect(keyLine).toHaveText('#squid@codex<2>@review!');
});

test('Squid Flow canonical key stays condensed for a comma-origins x comma-targets clause', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Menu' }).click();
  await page.getByRole('button', { name: 'Squid Flow' }).click();

  const keyLine = page.locator('#flow-key-line .flow-route-line');
  const statusEl = page.locator('#flow-status');

  // 2 origins x 2 targets under one shared round trip expands to 4
  // independent branches — but the key should stay as compact as the
  // input, not spell out all 4 branches separately.
  await page.fill('#flow-input', '#topic1@agent1,#topic2@agent2<>#topic3,#topic4@agentx');
  await expect(statusEl).toContainText('4 independent branches');
  await expect(keyLine).toHaveText('#topic1@agent1,#topic2@agent2<>#topic3,#topic4@agentx');

  // Reordering the origins still normalizes to the identical sorted origin
  // list. The target group does NOT: the two origins disagree on both topic
  // and agent, so the target's incoming state is fully ambiguous and the
  // target group can't be resolved at all — it falls back to keeping the
  // target atoms exactly as written (order-preserving), not sorted, since
  // there's nothing to safely group without a resolved value to group by.
  await page.fill('#flow-input', '#topic2@agent2,#topic1@agent1<>#topic4@agentx,#topic3');
  await expect(keyLine).toHaveText('#topic1@agent1,#topic2@agent2<>#topic4@agentx,#topic3');
});

test('Squid Flow origin list lets a partial atom borrow from any fully-explicit sibling', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Menu' }).click();
  await page.getByRole('button', { name: 'Squid Flow' }).click();

  const statusEl = page.locator('#flow-status');
  const keyLine = page.locator('#flow-key-line .flow-route-line');

  // The anchor doesn't have to be first positionally — #t1 (bare agent)
  // borrows from #t3@a1, which comes after it.
  await page.fill('#flow-input', '#t1,#t3@a1');
  await expect(statusEl).toHaveClass(/ok/);
  await expect(keyLine).toHaveText('#t1@a1,#t3');

  // A bare-topic atom borrows the anchor's topic instead of its agent.
  await page.fill('#flow-input', '#t3@a1,@a2');
  await expect(statusEl).toHaveClass(/ok/);
  await expect(keyLine).toHaveText('#t3@a1,@a2');

  // One anchor can donate to two different siblings at once — one borrows
  // its agent, the other borrows its topic.
  await page.fill('#flow-input', '#t3,@a2,#t1@a1');
  await expect(statusEl).toHaveClass(/ok/);
  await expect(keyLine).toHaveText('#t1@a1,#t3,@a2');

  // A list with no fully-explicit atom at all still has nothing to anchor to.
  await page.fill('#flow-input', '#t3,@a2');
  await expect(statusEl).toHaveClass(/err/);
  await expect(statusEl).toContainText('full #topic@agent');
});

test('Squid Flow canonical key never collides two different graphs that share the same atoms', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Menu' }).click();
  await page.getByRole('button', { name: 'Squid Flow' }).click();

  const keyLine = page.locator('#flow-key-line .flow-route-line');

  // #t3 (bare topic, missing agent) borrows agent from whichever atom is the
  // *nearest preceding* fully-explicit root — which one that is depends on
  // input order, so these two inputs resolve to genuinely different graphs
  // (t3 ends up on a2 vs a1) and must not collapse to the same key.
  await page.fill('#flow-input', '#t1@a1,#t2@a2,#t3');
  await expect(keyLine).toHaveText('#t1@a1,#t2@a2,#t3');

  await page.fill('#flow-input', '#t2@a2,#t1@a1,#t3');
  await expect(keyLine).toHaveText('#t1@a1,#t3,#t2@a2');
});

test('Squid Flow scheduled operator puts the loop count first, like <N>', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Menu' }).click();
  await page.getByRole('button', { name: 'Squid Flow' }).click();

  const statusEl = page.locator('#flow-status');
  const keyLine = page.locator('#flow-key-line .flow-route-line');

  // Bare count, no delay — repeats immediately, same shape as `<N>`. Target
  // atoms never borrow from each other (no rolling anchor for targets — each
  // resolves independently against the fixed parent state), so the only
  // thing a target atom can drop is a field matching that parent state
  // directly — here `@a` already omits topic 't', matching the origin's
  // topic, so it stays bare (see ADR-0032, "Canonical Key").
  await page.fill('#flow-input', '#t@c=2>@a');
  await expect(statusEl).toHaveClass(/ok/);
  await expect(keyLine).toHaveText('#t@c=2>@a');

  // Count + delay.
  await page.fill('#flow-input', '#t@c=1:1d>@a');
  await expect(statusEl).toHaveClass(/ok/);
  await expect(keyLine).toHaveText('#t@c=1:1d>@a');

  // Unbounded loop requires an explicit delay.
  await page.fill('#flow-input', '#t@c=*:1d>@a');
  await expect(statusEl).toHaveClass(/ok/);
  await expect(keyLine).toHaveText('#t@c=*:1d>@a');

  // Unbounded with no delay is a runaway cycle, not a schedule — rejected.
  await page.fill('#flow-input', '#t@c=*>@a');
  await expect(statusEl).toHaveClass(/err/);
  await expect(statusEl).toContainText("requires an explicit ':wait'");

  // The old bare-delay-no-count form is no longer valid syntax.
  await page.fill('#flow-input', '#t@c=1d>@a');
  await expect(statusEl).toHaveClass(/err/);
});

test('Squid Flow fully-expanded form gives each scheduled repeat its own row', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Menu' }).click();
  await page.getByRole('button', { name: 'Squid Flow' }).click();

  const expandedLines = page.locator('#flow-expanded-list .flow-route-line');

  await page.fill('#flow-input', '#squid@codex=2>@review!');
  await expect(expandedLines).toHaveCount(3);
  // The origin only ever runs once — it gets its own row — and only the
  // repeated target hop forks into one row per run.
  await expect(expandedLines.nth(0)).toHaveText('#squid@codex · runs immediately once');
  await expect(expandedLines.nth(1)).toHaveText('>#squid@review! · run 1 of 2');
  await expect(expandedLines.nth(2)).toHaveText('>#squid@review! · run 2 of 2');

  // Unbounded repeats are capped in the preview, with a trailing ellipsis row.
  await page.fill('#flow-input', '#squid@codex=*:1d>@review!');
  await expect(expandedLines).toHaveCount(7);
  await expect(expandedLines.last()).toHaveText('…');
});

test('Squid Flow fully-expanded rows show wait time between repeats', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Menu' }).click();
  await page.getByRole('button', { name: 'Squid Flow' }).click();

  const expandedLines = page.locator('#flow-expanded-list .flow-route-line');

  await page.fill('#flow-input', '#topic1@agent1=5:1d>@agent2');
  await expect(expandedLines).toHaveCount(6);
  // agent1 (the origin) only runs once, immediately — it's its own row.
  await expect(expandedLines.nth(0)).toHaveText('#topic1@agent1 · runs immediately once');
  // Every run, including the first, is a fixed wait after the previous one
  // (or after the trigger, for the first) — so run N is N waits out. Each
  // row's own arrow is `=1:T>`, since in isolation one delayed repeat IS
  // exactly a single one-way scheduled edge from whatever came before it.
  await expect(expandedLines.nth(1)).toHaveText('=1:1d>#topic1@agent2 · run 1 of 5 · +1d');
  await expect(expandedLines.nth(2)).toHaveText('=1:1d>#topic1@agent2 · run 2 of 5 · +2d');
  await expect(expandedLines.nth(3)).toHaveText('=1:1d>#topic1@agent2 · run 3 of 5 · +3d');
  await expect(expandedLines.nth(4)).toHaveText('=1:1d>#topic1@agent2 · run 4 of 5 · +4d');
  await expect(expandedLines.nth(5)).toHaveText('=1:1d>#topic1@agent2 · run 5 of 5 · +5d');
});

test('Squid Flow round-trip wait applies symmetrically to every hop, out and back', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Menu' }).click();
  await page.getByRole('button', { name: 'Squid Flow' }).click();

  const expandedLines = page.locator('#flow-expanded-list .flow-route-line');

  // No wait: hops stay a single continuous chain, unchanged from before.
  await page.fill('#flow-input', '#squid@codex<2>@review!');
  await expect(expandedLines).toHaveCount(1);
  await expect(expandedLines.nth(0)).toHaveText('#squid@codex>#squid@review!>#squid@codex>#squid@review!>#squid@codex');

  // With a wait, each hop — out AND back — gets its own row, its own
  // offset, and its own `=1:T>` arrow: in isolation, one delayed hop IS
  // exactly a single one-way scheduled edge from whatever preceded it.
  await page.fill('#flow-input', '#squid@codex<2:1d>@review!');
  await expect(expandedLines).toHaveCount(5);
  await expect(expandedLines.nth(0)).toHaveText('#squid@codex · runs immediately once');
  await expect(expandedLines.nth(1)).toHaveText('=1:1d>#squid@review! · round 1 of 2 (out) · +1d');
  await expect(expandedLines.nth(2)).toHaveText('=1:1d>#squid@codex · round 1 of 2 (return) · +2d');
  await expect(expandedLines.nth(3)).toHaveText('=1:1d>#squid@review! · round 2 of 2 (out) · +3d');
  await expect(expandedLines.nth(4)).toHaveText('=1:1d>#squid@codex · round 2 of 2 (return) · +4d');
});

test('Squid Flow view lists the syntax legend inline instead of only linking the ADR', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Menu' }).click();
  await page.getByRole('button', { name: 'Squid Flow' }).click();

  const legend = page.locator('#flow-legend');
  await expect(legend).toBeVisible();
  await expect(legend).toContainText('one-way handoff');
  await expect(legend).toContainText('join');
});
