# Squid — End-to-End Tests

Playwright tests for the Squid UI (`ui/`). All backend endpoints are mocked via
Playwright route interception — no running Squid server required.

## Setup

```bash
cd tests/e2e
npm install
npx playwright install chromium
```

## Running

```bash
# Headless (CI)
npm test

# Headed with slow motion — watch tests execute in a real browser
SLOWMO=700 npx playwright test --headed

# Interactive UI — step through tests, inspect DOM snapshots, view timelines
npm run test:ui

# Single test by name
npx playwright test --headed -g "does not appear in DOM before done"
```

## Test coverage

| Test | What it verifies |
|---|---|
| `does not appear in DOM before done` | Core invariant: response bubble is withheld from the DOM until the `done` SSE event. Holds the stream open mid-flight and asserts the bubble is absent, then releases. |
| `appears at bottom of #messages on done` | The stats line is the last child of `#messages`, confirming the bubble landed at the bottom. |
| `content is markdown-rendered in final bubble` | `marked.parse()` runs at `done`, not during streaming — bold and inline code render correctly. |
| `thinking bubble collapses to toggle when status events present` | Status events produce a collapsible `▸` toggle; the thinking bubble is not removed. |
| `thinking bubble removed when no status events` | Pure content stream — thinking bubble is cleaned up on `done`. |
| `error appears at bottom in bubble` | Error event puts a bubble with error text at the bottom, not mid-list. |
| `two concurrent responses both land at bottom without early bubble insertion` | Two parallel streams held open; neither bubble appears until its own `done` fires, and they appear in completion order. |

## Architecture

- `mockBackend(page)` — stubs all non-chat endpoints (`/health`, `/history`, `/quota`, etc.)
- `holdChat(page)` — captures the `/chat` route without fulfilling it, returning a `fulfill(body)` function the test controls. Used to assert mid-stream state.
- `sse(...events)` — builds a valid SSE body string from an array of `{ event, data }` objects.
- `look(page, ms)` — `waitForTimeout` pause so you can observe the current state when running headed.

## SLOWMO

`SLOWMO` (ms per action) scales the per-test timeout automatically
(`SLOWMO * 20`). Use `700` for comfortable viewing, `2000+` to read each step.
