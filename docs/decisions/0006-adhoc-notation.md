---
status: accepted
date: 2026-05-25
---
# ADR-0006: `!` Suffix for Adhoc (Oneshot) Mode

## Context and Problem Statement

Adhoc turns run in parallel, bypass the topic queue, and use the oneshot prompt injection
approach rather than the resumable session. A concise input notation is needed to flag them.

## Considered Options

| Notation | Example | Rejected reason |
|---|---|---|
| `?a` | `#topic@agent?a msg` | Awkward, non-standard |
| `&` | `#topic@agent& msg` | Reserved URL character (`&` separates query params) |
| `?` | `#topic@agent? msg` | Reserved URL character (`?` starts query string) |
| `-` | `#topic@agent- msg` | Visually ambiguous with hyphenated agent names |
| `!` | `#topic@agent! msg` | Selected |

## Decision Outcome

**`!` suffix.** `#topic@agent!` or `#topic!` flags the turn as adhoc.

`!` is URL-safe, reads as "fire now / immediate", and is unambiguous as a suffix on `\w+`
agent names (agentes cannot contain `!`).

**Bare `!` (without `#topic` prefix) is explicitly not supported.** In Claude Code and many
terminals, a leading `!` is interpreted as a shell/bash command. Reserving bare `!` avoids
collision if squid is run without Claude Code, and keeps bash execution available.

## Consequences

- Good: URL-safe; visually reads as emphasis/immediacy
- Good: unambiguous in the regex since agent names are `\w+`
- Bad: `!` may feel like negation to some users
- Constraint: adhoc always requires a `#topic` prefix
