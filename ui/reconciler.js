// Idempotent transcript reconciler — ADR-0041, migration step 3.
//
// The reconciler is the only code path allowed to apply store-owned
// mutations to a turn group's DOM. It never talks to #messages directly;
// callers inject a `registry` that owns rendering and placement so this
// module stays dependency-free and testable the same way transcript-store.js
// is (see tests/e2e/reconciler.spec.js).
//
// registry contract:
//   render(turn, ctx) -> group
//     Build (or rebuild) the complete node set for one turn group. `ctx`
//     carries `store`, `previousGroup` (the group this call replaces, or
//     null), `previousBucket`/`nextBucket` ('pending' | 'completed') so a
//     live-to-terminal transition can be detected and swapped atomically
//     instead of two visible steps. Throwing (or returning a falsy value)
//     marks the turn's reconcile as failed for this pass; the id stays dirty
//     in the store and is retried on the next reconcile() call.
//   reorder(order, groups) -> void
//     `order` is store.getOrderedTurnIds()'s { completed, pending } shape.
//     `groups` is a Map of assistantMsgId -> group for every group that is
//     up-to-date this pass. Called once per pass that made progress so the
//     registry can place/move each group at its deterministic position; ids
//     absent from `groups` — never rendered, or whose render failed this pass
//     — are the registry's to skip (a failed id's previous group is stale
//     against its new status and must not be repositioned).
//
// The reconciler touches only dirty turn groups for rendering; reorder is a
// placement pass over already-rendered groups, not a re-render.

(function (global) {
  'use strict';

  function createReconciler({ store, registry }) {
    if (!store) throw new Error('createReconciler: store is required');
    if (!registry || typeof registry.render !== 'function' || typeof registry.reorder !== 'function') {
      throw new Error('createReconciler: registry must implement render(turn, ctx) and reorder(order, groups)');
    }
    const isTerminal = typeof store.isTerminal === 'function'
      ? store.isTerminal
      : status => status === 'done' || status === 'error' || status === 'cancelled';

    // One stable registry identity per assistant msg_id, kept for the life
    // of the reconciler — never recreated on update, only ever replaced in
    // place by a fresh render() result.
    const groups = new Map();
    const bucketOf = new Map();

    function bucketFor(turn) {
      return isTerminal(turn.status) ? 'completed' : 'pending';
    }

    function reconcileIds(ids) {
      const succeeded = [];
      const failed = [];
      for (const assistantMsgId of ids) {
        const turn = store.getTurn(assistantMsgId);
        if (!turn) { failed.push(assistantMsgId); continue; }
        const previousGroup = groups.get(assistantMsgId) ?? null;
        const previousBucket = bucketOf.get(assistantMsgId) ?? null;
        const nextBucket = bucketFor(turn);
        let group;
        try {
          group = registry.render(turn, { store, previousGroup, previousBucket, nextBucket });
        } catch {
          failed.push(assistantMsgId);
          continue;
        }
        if (!group) { failed.push(assistantMsgId); continue; }
        groups.set(assistantMsgId, group);
        bucketOf.set(assistantMsgId, nextBucket);
        succeeded.push(assistantMsgId);
      }
      return { succeeded, failed };
    }

    // Processes exactly the dirty ids present at call time; ids that become
    // dirty from a concurrent action during this pass are left for the next
    // reconcile() call rather than folded in, so a cursor ack gated on this
    // call's result reflects only what it actually reconciled.
    function reconcile() {
      const dirty = store.getPendingReconcile();
      const { succeeded, failed } = reconcileIds(dirty);
      if (succeeded.length) {
        // A failed render leaves that id's previous group stale while the
        // store's ordering has already advanced — exclude it so the registry
        // can't reposition a stale group against its new bucket/status.
        const failedIds = new Set(failed);
        const upToDate = new Map([...groups].filter(([id]) => !failedIds.has(id)));
        registry.reorder(store.getOrderedTurnIds(), upToDate);
        store.clearReconciled(succeeded);
      }
      return { ok: failed.length === 0, reconciledIds: succeeded, failedIds: failed };
    }

    // Cursor persistence belongs to the producer adapter, not the
    // reconciler — this only gates the ack callback on this pass's result so
    // a producer can call reconcileAndAck(() => saveCursor(eventId)) and
    // trust it never fires over a partially-failed pass.
    function reconcileAndAck(ack) {
      const result = reconcile();
      if (result.ok && typeof ack === 'function') ack();
      return result;
    }

    // reconcile() only calls registry.reorder() when something is newly
    // dirty — correct for the store's own producers, but a placement
    // decision registry.reorder() already made (e.g. "no live groups yet,
    // anchor at the end") can be invalidated by DOM mutations *outside* the
    // store entirely: a still-unmigrated producer (e.g. realtime pending-turn
    // discovery) adding or removing a live group changes where completed
    // turns belong, without ever dirtying any assistant msg_id. Nothing else
    // would re-trigger reorder() in that case, so an already-placed group can
    // be stuck referencing a stale anchor indefinitely. A caller whose own
    // mutation can invalidate reconciler placement should call this
    // afterward — cheap (no re-render, just re-placement of already-rendered
    // groups) and idempotent when nothing actually needs to move.
    function reposition() {
      registry.reorder(store.getOrderedTurnIds(), new Map(groups));
    }

    // For a caller that discards its own rendered nodes outside the
    // reconciler — a filter/scope change or a jump that wipes and rebuilds
    // its DOM region directly, rather than an incremental store update. The
    // reconciler has no way to observe that removal (the store has no
    // delete/eviction action either), so without this its `groups` bookkeeping
    // would keep pointing at now-detached nodes and reattach them on the next
    // reorder() — resurrecting turns the reset was meant to clear. The caller
    // is responsible for calling this *before* it removes those nodes, and
    // for re-dirtying (or re-installing) whatever should reappear afterward.
    //
    // Also clears the store's own pendingReconcile set — not just registry
    // identity. A producer that feeds the store without always reconciling
    // (e.g. a rendering mode that installs but doesn't render) can leave ids
    // dirty indefinitely; left alone, those ids resurface on the next
    // reconcile() after this reset — the exact class of bug reset() exists to
    // prevent, just via the store's dirty tracking instead of `groups`. Safe
    // because every real caller re-installs whatever's actually in the new
    // scope immediately after calling this, which re-dirties it regardless —
    // nothing genuinely pending is lost by clearing early. This does assume
    // one thing: today the store has exactly one real producer (HTTP
    // history), so every dirty id is this reconciler's concern. A second
    // store producer sharing dirty-id space would need this reconsidered —
    // an indiscriminate clear could mask its unrelated pending work too.
    function reset() {
      groups.clear();
      bucketOf.clear();
      store.clearReconciled(store.getPendingReconcile());
    }

    return {
      reconcile,
      reconcileAndAck,
      reposition,
      reset,
      getGroup: assistantMsgId => groups.get(assistantMsgId),
      getGroupCount: () => groups.size,
    };
  }

  const SquidReconciler = { createReconciler };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = SquidReconciler;
  } else {
    global.SquidReconciler = SquidReconciler;
  }
})(typeof window !== 'undefined' ? window : globalThis);
