// Normalized client transcript store and reducer — ADR-0041.
//
// The store and its action vocabulary live here, standalone and
// dependency-free; it owns no DOM. app.js feeds it via shadowInstallHistoryPage
// (HTTP history) today, with the remaining producers (WS snapshot, WS events,
// SSE) wired in per the ADR's "Migration sequence" section.
//
// Wire-level field names (msg_id, reply_to, completed_at, ...) are ADR-0040's
// concern; this module accepts them as-is on read but stores camelCase
// internally so producers stay free to normalize at the adapter boundary.

(function (global) {
  'use strict';

  // chat_messages.status vocabulary (agent/stats_db.py): 'pending', 'scheduled',
  // 'claimed', 'running', 'done', 'error', 'cancelled' — 'done' is the terminal
  // success state on the wire, not 'completed'. Kept as an explicit allowlist,
  // not a denylist of known non-terminal values: a run-event-sourced message
  // (applyRunEvent's 'text'/'tool'/'stats' kinds) legitimately has no status at
  // all while still streaming, and mergeSparse's terminal-status monotonicity
  // guard must not misread that "unknown" as "terminal" — doing so silently
  // drops the real 'running' status patch that arrives right after (a missing
  // status meant "this producer hasn't reported one yet," not "this is done").
  // A history row with a missing status (ADR-0041 Gap 1) is normalized to a
  // real terminal value at that producer's own edge instead (see
  // historyItemToStoreRows in ui/app.js), not by loosening this allowlist.
  const TERMINAL_STATUSES = new Set(['done', 'error', 'cancelled']);

  function isTerminal(status) {
    return TERMINAL_STATUSES.has(status);
  }

  // snake_case -> camelCase for the identity/ordering fields every producer
  // shares; unknown fields pass through unchanged so callers can carry
  // arbitrary content/tools/stats/route payloads.
  function normalizeRow(row) {
    const out = { ...row };
    if ('msg_id' in out) { out.msgId = out.msg_id; delete out.msg_id; }
    if ('reply_to' in out) { out.replyTo = out.reply_to; delete out.reply_to; }
    if ('completed_at' in out) { out.completedAt = out.completed_at; delete out.completed_at; }
    if ('created_at' in out) { out.createdAt = out.created_at; delete out.created_at; }
    if ('queued_at' in out) { out.queuedAt = out.queued_at; delete out.queued_at; }
    return out;
  }

  function identityConflict(existing, incoming) {
    if (!existing) return null;
    if (incoming.role !== undefined && existing.role !== undefined && incoming.role !== existing.role) {
      return `role mismatch for msg ${existing.msgId}: ${existing.role} -> ${incoming.role}`;
    }
    // existing.replyTo == null (not just undefined) is still "not yet known,"
    // not a confirmed fact: a flow-chain step's own message.changed 'created'
    // event reports reply_to: null before the server finishes linking it to
    // its synthetic handoff prompt, and a stricter !== undefined check here
    // would let that provisional null permanently block the real value from
    // ever landing — silently dropping the *whole* patch (status/content
    // included, since message.changed is WS's only channel for those) on
    // every later update for that message, leaving the turn stuck at
    // 'pending' in the store while its DOM bubble had already completed.
    if (incoming.replyTo !== undefined && existing.replyTo != null && incoming.replyTo !== existing.replyTo) {
      return `reply_to mismatch for msg ${existing.msgId}: ${existing.replyTo} -> ${incoming.replyTo}`;
    }
    return null;
  }

  // Authoritative merge (history pages, snapshots): every field the caller
  // declares replaces the stored value outright, except a terminal status
  // can never be downgraded back to a non-terminal one — and, like
  // mergeSparse below, that guard rejects the whole row once triggered, not
  // just its status key, so a stale non-terminal row can't wipe real content
  // out from under an already-terminal message while merely failing to move
  // its status backward.
  function mergeAuthoritative(existing, fields) {
    if (existing && fields.status !== undefined && isTerminal(existing.status) && !isTerminal(fields.status)) {
      return { ...existing };
    }
    const merged = existing ? { ...existing } : { tools: [], stats: {} };
    for (const key of Object.keys(fields)) {
      if (fields[key] === undefined) continue;
      merged[key] = fields[key];
    }
    return merged;
  }

  // Sparse merge (lifecycle patches, run-event deltas): omitted fields are
  // left untouched, never cleared. Same terminal-status monotonicity rule —
  // but applied to the whole patch, not just the status key: a WS lifecycle
  // producer can emit a stray out-of-order message.changed reporting a
  // non-terminal status for an already-terminal message (observed for a Flow
  // origin step: 'done' immediately followed by a spurious 'pending' — likely
  // a side effect of dispatching the next chain step touching the same row).
  // That patch's other sparse fields are equally stale; applying just its
  // content (typically blank, since a genuinely 'pending' row has none yet)
  // while only guarding status would silently wipe the real completed
  // content and leave the turn status:'done'/content:'' — which the history
  // registry then renders as zero nodes, an invisible-but-"complete" bubble.
  // Reject the whole patch instead of merging around it.
  function mergeSparse(existing, patch) {
    if (existing && patch.status !== undefined && isTerminal(existing.status) && !isTerminal(patch.status)) {
      return { ...existing };
    }
    const merged = existing ? { ...existing } : { tools: [], stats: {} };
    for (const key of Object.keys(patch)) {
      if (patch[key] === undefined) continue;
      merged[key] = patch[key];
    }
    return merged;
  }

  function createTranscriptStore() {
    const messagesById = new Map();
    const turnsByAssistantId = new Map();
    const pendingReconcile = new Set();
    const lastRunSeqByAssistantId = new Map();
    // promptMsgId -> assistantMsgId, kept in step with messagesById so a
    // user-role message can find its turn without scanning every message.
    const assistantByReplyTo = new Map();
    let lastAppliedEventId = 0;

    const view = {
      loadedMessageIds: new Set(),
      pageBoundaries: [],
      activeWindowIds: new Set(),
      visibleScope: null,
    };

    function markDirty(assistantMsgId) {
      if (assistantMsgId != null) pendingReconcile.add(assistantMsgId);
    }

    // A message record's own identity determines which turn it belongs to:
    // an assistant message is its own turn key; a user message's turn key
    // is whichever assistant message replies to it (if known yet).
    function turnKeyFor(msg) {
      if (msg.role === 'assistant') return msg.msgId;
      if (msg.role === 'user') return assistantByReplyTo.get(msg.msgId) ?? null;
      return null;
    }

    function upsertTurn(assistantMsgId) {
      const assistantMsg = messagesById.get(assistantMsgId);
      if (!assistantMsg) return;
      const promptMsg = assistantMsg.replyTo != null ? messagesById.get(assistantMsg.replyTo) : null;
      turnsByAssistantId.set(assistantMsgId, {
        assistantMsgId,
        promptMsgId: assistantMsg.replyTo ?? null,
        status: assistantMsg.status,
        createdAt: assistantMsg.queuedAt ?? assistantMsg.createdAt ?? null,
        completedAt: assistantMsg.completedAt ?? null,
        tools: assistantMsg.tools ?? [],
        stats: assistantMsg.stats ?? {},
        narrative: assistantMsg.narrative ?? '',
        // Live-accumulated reply text (applyRunEvent's 'text' kind patches
        // this onto the message directly). Distinct from turn.raw?.content:
        // raw is a static snapshot attached once at the last full-row
        // install, so for a still-streaming turn it lags behind whatever
        // 'text' deltas have arrived since — a pending-turn renderer needs
        // this field, not raw's, to show live text.
        content: assistantMsg.content ?? '',
        route: assistantMsg.route,
        promptContent: promptMsg ? promptMsg.content : undefined,
        // Full-fidelity render payload (ADR-0041 Stage 4): a producer that
        // wants the store to be able to *render* a turn, not just track its
        // identity/ordering, attaches its original denormalized row here.
        // Enumerating every display field individually (topic, agent, adhoc,
        // session_id, prompt_context, tool context, ...) as its own tracked
        // property would drift out of sync with the renderer it feeds; this
        // stays a single opaque passthrough, same policy as normalizeRow's
        // "unknown fields pass through unchanged" for messages.
        raw: assistantMsg.raw,
      });
      markDirty(assistantMsgId);
    }

    function putMessage(msg) {
      messagesById.set(msg.msgId, msg);
      if (msg.role === 'assistant') {
        if (msg.replyTo != null) assistantByReplyTo.set(msg.replyTo, msg.msgId);
        upsertTurn(msg.msgId);
      } else {
        const key = turnKeyFor(msg);
        if (key != null) upsertTurn(key);
      }
    }

    function dirtySnapshot() {
      return [...pendingReconcile];
    }

    // Validates identity across the whole batch, not just against
    // pre-transaction store state: two rows for the same msg_id within one
    // page/snapshot must not be allowed to silently overwrite each other's
    // identity (e.g. one flipping the other's role).
    function findBatchConflict(rows) {
      const seen = new Map();
      for (const row of rows) {
        if (row.msgId == null) return 'row missing msg_id';
        const baseline = seen.has(row.msgId) ? seen.get(row.msgId) : messagesById.get(row.msgId);
        const conflict = identityConflict(baseline, row);
        if (conflict) return conflict;
        seen.set(row.msgId, baseline ? { ...baseline, ...row } : row);
      }
      return null;
    }

    function installHistoryPage(page, boundary) {
      if (!Array.isArray(page)) return { ok: false, error: 'installHistoryPage: page must be an array', dirty: dirtySnapshot() };
      const rows = page.map(normalizeRow);
      const batchConflict = findBatchConflict(rows);
      if (batchConflict) return { ok: false, error: `installHistoryPage: ${batchConflict}`, dirty: dirtySnapshot() };
      for (const row of rows) {
        const merged = mergeAuthoritative(messagesById.get(row.msgId), row);
        putMessage(merged);
        view.loadedMessageIds.add(row.msgId);
      }
      if (boundary !== undefined) view.pageBoundaries.push(boundary);
      return { ok: true, dirty: dirtySnapshot() };
    }

    function installSnapshot(snapshot, eventId) {
      if (!snapshot || !Array.isArray(snapshot.messages)) {
        return { ok: false, error: 'installSnapshot: snapshot.messages must be an array', dirty: dirtySnapshot() };
      }
      const numericEventId = Number(eventId);
      if (!Number.isFinite(numericEventId)) {
        return { ok: false, error: 'installSnapshot: event_id must be numeric', dirty: dirtySnapshot() };
      }
      if (numericEventId <= lastAppliedEventId) {
        // At-or-below watermark: no-op, but surface any still-outstanding
        // reconcile work so a prior render failure can be retried.
        return { ok: true, dirty: dirtySnapshot(), noop: true };
      }
      const rows = snapshot.messages.map(normalizeRow);
      const batchConflict = findBatchConflict(rows);
      if (batchConflict) return { ok: false, error: `installSnapshot: ${batchConflict}`, dirty: dirtySnapshot() };
      for (const row of rows) {
        const merged = mergeAuthoritative(messagesById.get(row.msgId), row);
        putMessage(merged);
      }
      view.activeWindowIds = new Set(rows.map(r => r.msgId));
      lastAppliedEventId = numericEventId;
      return { ok: true, dirty: dirtySnapshot() };
    }

    function applyMessagePatch(msgId, patch, eventId) {
      if (msgId == null) return { ok: false, error: 'applyMessagePatch: msg_id required', dirty: dirtySnapshot() };
      const numericEventId = Number(eventId);
      if (!Number.isFinite(numericEventId)) {
        return { ok: false, error: 'applyMessagePatch: event_id must be numeric', dirty: dirtySnapshot() };
      }
      if (numericEventId <= lastAppliedEventId) {
        return { ok: true, dirty: dirtySnapshot(), noop: true };
      }
      const normalized = normalizeRow(patch || {});
      const existing = messagesById.get(msgId);
      const conflict = identityConflict(existing, normalized);
      if (conflict) return { ok: false, error: `applyMessagePatch: ${conflict}`, dirty: dirtySnapshot() };
      // mergeSparse's terminal-status guard rejects a stale non-terminal patch
      // wholesale (see its comment — the flow-origin spurious 'pending' case).
      // Honor that rejection here too: applying the merged copy of `existing`
      // changes no field, but putMessage's unconditional upsertTurn would still
      // re-dirty the id — and the next reconcile() pass would then re-render an
      // already-terminal turn for no reason (at best scroll churn, at worst a
      // duplicate bubble while the registry's completed→completed re-render
      // builds fresh nodes). The watermark still advances: the event was seen.
      if (existing && normalized.status !== undefined && isTerminal(existing.status) && !isTerminal(normalized.status)) {
        lastAppliedEventId = numericEventId;
        return { ok: true, dirty: dirtySnapshot(), noop: true };
      }
      const merged = mergeSparse(existing, { ...normalized, msgId });
      putMessage(merged);
      lastAppliedEventId = numericEventId;
      return { ok: true, dirty: dirtySnapshot() };
    }

    // eventId is optional: WS (producers 2/3) always pass a real numeric
    // global cursor and participate in the lastAppliedEventId watermark
    // below. SSE (producer 4) has no global cursor on the wire at all — it
    // predates ADR-0040's realtime protocol and is compatibility-only — so
    // it calls this with eventId left `undefined`, which skips the global
    // watermark entirely and relies only on this message's own runSeq
    // (sourced from the real run_events.seq now carried on the SSE wire,
    // see agent/server.py's sse_chunk/sse_event `id:` field). This is safe
    // because one SSE connection only ever carries one message's deltas, so
    // there is no cross-message ordering for it to lose by not
    // participating in the global cursor. A fabricated/local global id was
    // considered and rejected: because the watermark is shared store-wide,
    // an invented value could sit below or above the real WS cursor and
    // silently drop legitimate producer-2/3 events for the rest of the
    // session.
    function applyRunEvent(msgId, runSeq, kind, payload, eventId) {
      if (msgId == null) return { ok: false, error: 'applyRunEvent: msg_id required', dirty: dirtySnapshot() };
      const hasEventId = eventId !== undefined;
      let numericEventId;
      if (hasEventId) {
        numericEventId = Number(eventId);
        if (!Number.isFinite(numericEventId)) {
          return { ok: false, error: 'applyRunEvent: event_id must be numeric', dirty: dirtySnapshot() };
        }
        if (numericEventId <= lastAppliedEventId) {
          return { ok: true, dirty: dirtySnapshot(), noop: true };
        }
      }
      const numericRunSeq = Number(runSeq);
      // run_seq is mandatory, not best-effort: a producer with no valid
      // sequence for this delta (e.g. a dropped/missing SSE `id:` line) must
      // not silently apply unprotected — that would accept a replay as a
      // fresh delta and duplicate content. Reject instead of falling through
      // with no dedup guard.
      if (!Number.isFinite(numericRunSeq)) {
        return { ok: false, error: 'applyRunEvent: run_seq must be numeric', dirty: dirtySnapshot() };
      }
      const lastRunSeq = lastRunSeqByAssistantId.get(msgId) ?? -1;
      if (numericRunSeq <= lastRunSeq) {
        // event_id already advanced past a prior duplicate/out-of-order
        // run_seq for this message; run-level payload is still a no-op.
        if (hasEventId) lastAppliedEventId = numericEventId;
        return { ok: true, dirty: dirtySnapshot(), noop: true };
      }

      const existing = messagesById.get(msgId);
      const conflict = identityConflict(existing, { role: 'assistant' });
      if (conflict) return { ok: false, error: `applyRunEvent: ${conflict}`, dirty: dirtySnapshot() };

      let patch = { msgId, role: 'assistant' };
      switch (kind) {
        case 'text': {
          const priorContent = existing?.content ?? '';
          const delta = payload?.delta ?? '';
          patch.content = payload?.mode === 'replace' ? (payload.text ?? '') : priorContent + delta;
          break;
        }
        case 'tool': {
          const priorTools = existing?.tools ?? [];
          // Real tool payloads (agent/topic_queue.py's `_emit_tool`) key on
          // `tool_use_id`, not `id` — a WS-sourced tool start/result pair
          // would otherwise never match its own prior entry and just pile up
          // as duplicate tools[] rows instead of updating one in place.
          const toolId = payload?.id ?? payload?.tool_use_id;
          const idx = toolId == null ? -1 : priorTools.findIndex(t => (t.id ?? t.tool_use_id) === toolId);
          const nextTools = idx === -1 ? [...priorTools, payload] : priorTools.map((t, i) => (i === idx ? { ...t, ...payload } : t));
          patch.tools = nextTools;
          break;
        }
        case 'status': {
          if (payload?.status !== undefined) patch.status = payload.status;
          if (payload?.completed_at !== undefined) patch.completedAt = payload.completed_at;
          if (payload?.completedAt !== undefined) patch.completedAt = payload.completedAt;
          break;
        }
        case 'stats': {
          patch.stats = { ...(existing?.stats ?? {}), ...(payload ?? {}) };
          break;
        }
        // The live status/thinking narrative (CLI status_raw scrollback and
        // queue position). Tool frames remain structured in tools[] rather
        // than being flattened into this field. Distinct from the 'status' kind
        // above, which is a lifecycle transition (payload.status), not
        // narrative text. Mirrors 'text''s own append/replace split: most
        // frames (chat.status and queued position) append onto
        // the running narrative, but a few (chat.loading, a fresh
        // processing announcement) supersede it outright — payload.mode
        // distinguishes the two the same way it already does for 'text'.
        case 'narrative': {
          const priorNarrative = existing?.narrative ?? '';
          const delta = payload?.delta ?? '';
          patch.narrative = payload?.mode === 'replace' ? (payload.text ?? '') : priorNarrative + delta;
          break;
        }
        default:
          return { ok: false, error: `applyRunEvent: unknown kind '${kind}'`, dirty: dirtySnapshot() };
      }

      const merged = mergeSparse(existing, patch);
      putMessage(merged);
      lastRunSeqByAssistantId.set(msgId, Math.max(lastRunSeq, numericRunSeq));
      if (hasEventId) lastAppliedEventId = numericEventId;
      return { ok: true, dirty: dirtySnapshot() };
    }

    // ADR-0041 Stage 4 prerequisite: fill a turn's render payload (raw) when
    // a producer has a denormalized row but only ever fed identity/ordering
    // fields. The WS lifecycle producer's message.changed branch
    // (applyMessagePatch) carries id/role/status/content but never the full
    // display row (topic/agent/adhoc/prompt/...), so a turn discovered purely
    // via that transport has no raw and the pending renderer cannot build or
    // adopt from it. attachRaw fills only `raw` — never content/status/
    // run_seq/watermark — so a still-streaming turn's live-accumulated fields
    // are not clobbered or double-counted (installHistoryPage would be: it
    // replaces content authoritatively while leaving lastRunSeqByAssistantId
    // untouched, so the next applyRunEvent delta would re-append text the row
    // already contained). Existing raw fields remain first-writer-wins, but a
    // later discovery row may fill fields the snapshot shape omitted (notably
    // the denormalized `prompt` needed to build a pending turn). An unknown
    // msg_id is a silent no-op, not an error — a flow step whose
    // message.changed was missed (or discovered via
    // the SSE polling fallback, which never fed the store) simply isn't
    // tracked here yet; a later snapshot or history load installs it.
    function attachRaw(msgId, raw) {
      if (msgId == null) return { ok: false, error: 'attachRaw: msg_id required', dirty: dirtySnapshot() };
      if (raw == null) return { ok: false, error: 'attachRaw: raw required', dirty: dirtySnapshot() };
      const existing = messagesById.get(msgId);
      if (!existing) return { ok: true, dirty: dirtySnapshot(), noop: true };
      const existingRaw = existing.raw;
      const mergedRaw = existingRaw == null ? raw : { ...raw, ...existingRaw };
      const addsField = existingRaw == null || Object.keys(raw).some(key =>
        !Object.prototype.hasOwnProperty.call(existingRaw, key));
      if (!addsField) return { ok: true, dirty: dirtySnapshot(), noop: true };
      messagesById.set(msgId, { ...existing, raw: mergedRaw });
      const turn = turnsByAssistantId.get(msgId);
      if (turn) turnsByAssistantId.set(msgId, { ...turn, raw: mergedRaw });
      return { ok: true, dirty: dirtySnapshot() };
    }

    function setVisibleScope(scope) {
      view.visibleScope = scope;
      return { ok: true, dirty: dirtySnapshot() };
    }

    function clearReconciled(assistantMsgIds) {
      for (const id of assistantMsgIds) pendingReconcile.delete(id);
    }

    function getOrderedTurnIds() {
      const completed = [];
      const pending = [];
      for (const turn of turnsByAssistantId.values()) {
        if (isTerminal(turn.status)) completed.push(turn);
        else pending.push(turn);
      }
      completed.sort((a, b) => {
        const at = a.completedAt ?? '';
        const bt = b.completedAt ?? '';
        if (at !== bt) return at < bt ? -1 : 1;
        return a.assistantMsgId - b.assistantMsgId;
      });
      pending.sort((a, b) => {
        const at = a.createdAt ?? '';
        const bt = b.createdAt ?? '';
        if (at !== bt) return at < bt ? -1 : 1;
        return a.assistantMsgId - b.assistantMsgId;
      });
      return {
        completed: completed.map(t => t.assistantMsgId),
        pending: pending.map(t => t.assistantMsgId),
      };
    }

    return {
      installHistoryPage,
      installSnapshot,
      applyMessagePatch,
      applyRunEvent,
      attachRaw,
      setVisibleScope,
      getMessage: msgId => messagesById.get(msgId),
      getTurn: assistantMsgId => turnsByAssistantId.get(assistantMsgId),
      getPendingReconcile: dirtySnapshot,
      clearReconciled,
      getOrderedTurnIds,
      isTerminal,
      getLastAppliedEventId: () => lastAppliedEventId,
      getView: () => ({
        loadedMessageIds: [...view.loadedMessageIds],
        pageBoundaries: [...view.pageBoundaries],
        activeWindowIds: [...view.activeWindowIds],
        visibleScope: view.visibleScope,
      }),
    };
  }

  const SquidTranscriptStore = { createTranscriptStore };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = SquidTranscriptStore;
  } else {
    global.SquidTranscriptStore = SquidTranscriptStore;
  }
})(typeof window !== 'undefined' ? window : globalThis);
