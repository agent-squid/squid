---
status: accepted
date: 2026-06-16
---
# ADR-0021: Keyword Search Model and Conventions

## Context and Problem Statement

Squid accumulates assistant responses across topics, agents, and session types
(regular and adhoc). Users need a way to retrieve past responses by keyword
without manually scrolling history. The search surface must integrate cleanly
with the existing filter and topic-slug system and work identically on desktop
and mobile.

## Decisions

### 1. FTS5 standalone table with incremental boot population

Search is backed by a SQLite FTS5 virtual table (`messages_fts`) using the
`unicode61` tokenizer. The table is **standalone** (no `content=` option) and
stores a copy of assistant content in its own shadow tables.

An `AFTER UPDATE OF content` trigger is the **primary indexing path** — it
inserts each assistant response into `messages_fts` when the content column
transitions from NULL to a value (i.e. when a streaming response completes).

On startup, an **incremental population** INSERT runs as error correction:

```sql
INSERT INTO messages_fts(rowid, content)
SELECT id, content FROM chat_messages
WHERE role = 'assistant' AND content IS NOT NULL
  AND id NOT IN (SELECT rowid FROM messages_fts)
```

This fills any gaps left by prior crashes, deletes that ran before FTS cleanup
was wired up, or other edge cases. Because the standalone table's rowid scan
is real (unlike external-content tables), the `NOT IN` guard is safe and
efficient. On a fully-indexed database it scans the FTS rowids and inserts
zero rows — cost is O(index size), not O(table size).

The original DROP+RECREATE approach (full rebuild on every startup) was
replaced for two reasons:
1. It added startup latency proportional to total assistant message count.
2. It was never logically necessary — the trigger handles real-time indexing,
   so the full rebuild was redundant on every boot after the first.

**TBD:** The remaining `DROP TRIGGER IF EXISTS` guard in the DDL list could
be moved to a proper schema migration step. It exists only to handle the
one-time re-creation of the trigger when upgrading from external-content to
standalone; once all installs have gone through that upgrade it's dead code.

Only assistant responses are indexed. User prompts are not indexed and are
never highlighted in search results.

### 1a. FTS cleanup on bulk delete

When a topic or topic+agent lane is deleted, the FTS index is cleaned up in
the same transaction before the `chat_messages` rows are removed:

```sql
DELETE FROM messages_fts
WHERE rowid IN (
    SELECT id FROM chat_messages
    WHERE topic=? AND role='assistant'
    -- plus any agent/adhoc filter for per-agent deletes
)
```

This is one statement per delete operation (not one per row). Without explicit
cleanup, FTS rows become orphaned — the rowid exists in `messages_fts` but the
corresponding `chat_messages` row is gone. Orphaned rows are harmless in
practice (search queries JOIN back to `chat_messages`, so deleted messages
never appear in results), but they waste storage and accumulate indefinitely.

The incremental boot population does not re-add orphaned FTS rows because it
guards on `id NOT IN (SELECT rowid FROM messages_fts)` — the guard checks
whether the FTS row exists, not whether the base row still exists. Orphans are
therefore invisible to boot correction and would persist forever without
explicit cleanup on delete.

### 2. AND-expression matching

Multi-word queries are compiled to FTS5 AND expressions using phrase-quoted
tokens:

```
"term1" AND "term2"
```

Each token is stripped of double-quote characters before wrapping to prevent
malformed expressions. An empty token list returns zero results immediately
without a DB round-trip.

### 3. Scope syntax

The `/s` command accepts an optional scope prefix before keywords. Scope
controls which topic, agent, and session type is searched.

| Prefix | Topic | Agent | Adhoc filter |
|--------|-------|-------|--------------|
| *(none)* | from active filter or sticky chip | same | from active filter or sticky chip |
| `#topic` | specified | all | none (session + adhoc) |
| `#topic!` | specified | all | adhoc-only |
| `#topic@agent` | specified | exact | none |
| `#topic@agent*` | specified | LIKE prefix | none |
| `#topic@agent!` | specified | exact | adhoc-only |
| `#topic@agent*!` | specified | LIKE prefix | adhoc-only |
| `#all` | none | all | none |
| `@agent` | none | exact | none |
| `@agent*` | none | LIKE prefix | none |
| `@agent!` | none | exact | adhoc-only |

**Wildcard** (`*`) applies only to agent names, not topics. A topic is a
complete identifier; agent names vary by model variant (e.g. `claude`,
`claude-opus`, `haiku`), so prefix matching is useful there.

**`!` means adhoc-only**, not "include adhoc." Absence of `!` with an explicit
scope means no adhoc filter (both session and adhoc messages returned). This
matches the semantics of the `/filter` system where `#topic@agent!` filters
history to adhoc messages only.

When no explicit scope is given, scope resolution follows this priority:

1. Active `historyFilter` (set by `/filter` or tag click)
2. Sticky chip (current chat context)
3. No scope (search everything)

### 4. Scope and the filter badge are unified

The filter badge (`#filter-badge`) is the single visual scope indicator during
search. No separate scope chip exists in the search bar. The badge reads from
`searchState` when a search is active and from `historyFilter` otherwise.

Clicking `×` on the filter badge during search clears the search scope (sets
topic/agent/adhoc to null and re-runs the search) without touching
`historyFilter`. The filter state is restored when search is cleared.

The search bar itself shows only the keyword query and a close button.

### 5. Search results use the standard history card

Search results are rendered with `appendHistoryItem` — the same function used
for history pagination. Each result card includes the topic tag, prompt header,
full markdown content, pin button, stats footer, and tool blocks. There is no
separate snippet renderer.

Keyword highlights are applied via a DOM `TreeWalker` that walks text nodes in
the response body only, skipping `.response-header`, `.history-prompt-full`,
`.user-ctx`, `<code>`, and `<pre>` elements. Matches are wrapped in
`<mark class="search-kw-highlight">`.

Results load in reverse-chronological order, appended to the messages pane in
a single batch. The pane scrolls to the bottom after insertion so the most
recent results are immediately visible.

### 6. Single-fetch, no pagination

Search results are returned in one request, capped at 100 items. There is no
`offset` parameter and no scroll-triggered loading. The endpoint is:

```
GET /search?q=&limit=&topic=&agent=&adhoc=
```

The response shape is `{ items }` — no `total` or `has_more`. The server hard-
caps `limit` at 100 regardless of the requested value. If exactly 100 results
are returned the UI shows a hint: *"Showing top 100 results — add keywords to
narrow."*

The rationale for no pagination: keyword search result sets are inherently
small (typically 5–50 matches for a specific query). Re-querying the FTS index
on every scroll-up would repeat the same index scan with a different offset,
adding round-trips without benefit. When a query returns too many results the
correct user action is to add more keywords, not scroll through 100 cards.
History uses scroll-triggered pagination because it has no pre-filter and can
span thousands of messages; search does not have this property.

The service worker excludes `/search` from caching.

## Consequences

- Good: FTS5 inverted index makes multi-keyword search fast even over thousands
  of messages.
- Good: incremental boot population adds near-zero startup latency on a
  fully-indexed database; only truly missing rows are inserted.
- Good: unified scope with `/filter` means users learn one mental model for
  both history filtering and search scoping.
- Good: agent wildcard (`@agent*`) handles model-variant naming without
  requiring separate entries per model.
- Good: no snippet/expand UI — full cards make results immediately actionable
  (pin, copy, read in context).
- Good: single-fetch with a 100-item cap eliminates repeated FTS index scans
  and removes scroll-triggered pagination state from the client entirely.
- Good: explicit FTS cleanup on delete keeps the index tight — no orphaned rows
  accumulating over time.
- Bad: standalone FTS table duplicates assistant content. For large deployments
  this doubles storage for the indexed columns. External-content tables avoid
  this but require careful population logic (see root bug below).
- Bad: `unicode61` tokenizer treats `/`, `.`, `-` as separators, so searching
  `ui/app.js` tokenizes to `[ui, app, js]` and matches documents containing
  those tokens anywhere, not just in the slash-delimited form.

## Root bug reference

The initial implementation used an FTS5 **external-content** table
(`content='chat_messages'`). On this table type, `SELECT rowid FROM
messages_fts` (without MATCH) proxies to the base table and returns all
`chat_messages.id` values. The `NOT IN (SELECT rowid FROM messages_fts)` guard
used during population therefore always evaluated FALSE, inserting zero rows.
The FTS inverted index remained empty and all MATCH queries returned zero
results. Switching to a standalone table with DROP+RECREATE resolved this.
