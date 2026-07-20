// Squid Flow language: parse / validate / canonicalize / expand.
// Standalone, dependency-free. Not wired into the live app yet — see
// flow-playground.html for an interactive surface, and ADR-0032 for the
// language spec this implements.
//
// Scope: single linear clause only. `;` (multi-clause DAG / cycles) is
// explicitly out of scope and rejected with a clear message.

(function (global) {
  'use strict';

  class FlowError extends Error {
    constructor(message, pos) {
      super(message);
      this.pos = pos;
    }
  }

  const NAME = '[A-Za-z0-9_.-]+';
  const ATOM_RE = new RegExp(`^(?:#(${NAME}))?(?:@(${NAME}))?(!)?`);
  const ROUNDTRIP_RE = /^<(\d+)?(?::([0-9]+[A-Za-z]+))?>/;
  const ONEWAY_ALIAS_RE = /^=>/;
  // Count comes first, same as roundtrip's bare `<N>`. `:wait` is an
  // optional add-on that spaces the bounded repeats out.
  const SCHEDULED_RE = /^=(\d+)(?::([0-9]+[A-Za-z]+))?>/;
  const ONEWAY_RE = /^>/;

  function stripLeadingWs(s) {
    const m = s.match(/^\s+/);
    return m ? s.slice(m[0].length) : s;
  }

  // ---- Tokenizing atoms and groups ----------------------------------------

  function parseAtom(s, offset) {
    const m = ATOM_RE.exec(s);
    if (!m || m[0].length === 0 || (!m[1] && !m[2])) {
      throw new FlowError(`Expected #topic and/or @agent at position ${offset}`, offset);
    }
    if (m[3] && !m[2]) {
      throw new FlowError(`'!' requires an @agent (position ${offset})`, offset);
    }
    return {
      atom: { topic: m[1] || null, agent: m[2] || null, fresh: !!m[3] },
      consumed: m[0].length,
    };
  }

  // allowPlus: whether '+' is a legal separator in this position (origin only).
  function parseGroup(s, offset, allowPlus) {
    const atoms = [];
    let kind = 'single';
    let rest = s;
    let pos = offset;
    for (;;) {
      const { atom, consumed } = parseAtom(rest, pos);
      atoms.push(atom);
      rest = rest.slice(consumed);
      pos += consumed;
      const sep = rest[0];
      if (sep === ',' || sep === '+') {
        if (sep === '+' && !allowPlus) {
          throw new FlowError(`'+' (join) is not valid here (position ${pos}) — join is only valid on the origin side of an edge`, pos);
        }
        const thisKind = sep === ',' ? 'comma' : 'plus';
        if (kind !== 'single' && kind !== thisKind) {
          throw new FlowError(`Cannot mix ',' and '+' in the same group (position ${pos})`, pos);
        }
        kind = thisKind;
        rest = rest.slice(1);
        pos += 1;
        continue;
      }
      break;
    }
    return { group: { kind, atoms }, consumed: pos - offset, rest };
  }

  // ---- Operators -----------------------------------------------------------

  function parseOperator(s, offset) {
    let m = ROUNDTRIP_RE.exec(s);
    if (m) {
      return {
        op: {
          type: 'roundtrip',
          rounds: m[1] ? parseInt(m[1], 10) : 1,
          wait: m[2] || null,
        },
        consumed: m[0].length,
      };
    }
    m = ONEWAY_ALIAS_RE.exec(s);
    if (m) return { op: { type: 'oneway', alias: true }, consumed: m[0].length };

    m = SCHEDULED_RE.exec(s);
    if (m) {
      return {
        op: {
          type: 'scheduled',
          count: parseInt(m[1], 10),
          wait: m[2] || null,
        },
        consumed: m[0].length,
      };
    }
    m = ONEWAY_RE.exec(s);
    if (m) return { op: { type: 'oneway', alias: false }, consumed: m[0].length };

    return null;
  }

  // ---- Clause parser: origin group, then zero or more (operator, group) ----

  function parseClause(text) {
    if (text.includes(';')) {
      const pos = text.indexOf(';');
      throw new FlowError(`';' is reserved for a future multi-clause Squid Flow feature and is not supported (position ${pos})`, pos);
    }
    let s = stripLeadingWs(text);
    let offset = text.length - s.length;
    const { group: origin, consumed, rest } = parseGroup(s, offset, true);
    s = rest;
    offset += consumed;

    const hops = [];
    for (;;) {
      s = stripLeadingWs(s);
      offset = text.length - s.length;
      if (!s) break;
      const parsedOp = parseOperator(s, offset);
      if (!parsedOp) {
        throw new FlowError(`Unrecognized syntax at position ${offset}: "${s}"`, offset);
      }
      s = s.slice(parsedOp.consumed);
      offset += parsedOp.consumed;

      if (parsedOp.op.type === 'roundtrip') {
        const { group: target, consumed: c2, rest: rest2 } = parseGroup(s, offset, false);
        s = rest2;
        offset += c2;
        hops.push({ op: parsedOp.op, target });
        s = stripLeadingWs(s);
        if (s) {
          throw new FlowError(`Chaining after a round-trip ('<>'/'<N>') is not supported yet (position ${text.length - s.length})`, text.length - s.length);
        }
        break;
      }

      const { group: target, consumed: c2, rest: rest2 } = parseGroup(s, offset, false);
      s = rest2;
      offset += c2;
      hops.push({ op: parsedOp.op, target });
      s = stripLeadingWs(s);
      if (s) {
        throw new FlowError(`Multi-hop Squid Flow clauses are not supported in v0.1 (position ${text.length - s.length})`, text.length - s.length);
      }
    }

    return { origin, hops };
  }

  // ---- Resolution: root inheritance, agreement rules, branch expansion ----

  function resolveGroupAgainstState(group, state, isOrigin) {
    // Rolling anchor (ADR-0032) applies *within* an origin list only — an
    // origin atom has no parent to inherit from, so a bare one borrows from
    // a sibling instead, and any sibling that's itself fully explicit
    // supersedes the root for whichever siblings come after it (see the
    // pre-seed below for a bare atom positioned before that sibling).
    //
    // A hop-target group is different: every atom in it *does* have a
    // parent — the edge's source, passed in as `state` — so each one
    // resolves independently against that single incoming state. Siblings
    // never inherit from each other here, no matter how they're written;
    // that's what "a comma-list source decomposes into independent
    // branches" (ADR-0032) means for the target side of an edge too. A
    // target atom that happens to be fully explicit is just explicit for
    // itself — it does not become a new root for the atoms next to it.
    if (!isOrigin) {
      const resolved = group.atoms.map(atom => {
        const topic = atom.topic != null ? atom.topic : state.topic;
        const agent = atom.agent != null ? atom.agent : state.agent;
        if (topic == null || agent == null) {
          throw new FlowError(`Cannot resolve "${renderAtom(atom)}" — missing ${topic == null ? 'topic' : 'agent'} and no root value available`, 0);
        }
        return { topic, agent, fresh: atom.fresh, written: atom };
      });
      return finishGroupResolution(group, resolved);
    }

    const firstAnchor = group.atoms.find(a => a.topic != null && a.agent != null);
    if (!firstAnchor) {
      throw new FlowError('At least one origin in this list must be a full #topic@agent — nothing to inherit yet', 0);
    }
    let root = { topic: firstAnchor.topic, agent: firstAnchor.agent };
    const resolved = group.atoms.map(atom => {
      if (atom.topic != null && atom.agent != null) root = { topic: atom.topic, agent: atom.agent };
      const topic = atom.topic != null ? atom.topic : root.topic;
      const agent = atom.agent != null ? atom.agent : root.agent;
      if (topic == null || agent == null) {
        throw new FlowError(`Cannot resolve "${renderAtom(atom)}" — missing ${topic == null ? 'topic' : 'agent'} and no root value available`, 0);
      }
      return { topic, agent, fresh: atom.fresh, written: atom };
    });
    return finishGroupResolution(group, resolved);
  }

  function finishGroupResolution(group, resolved) {
    if (group.kind === 'plus') {
      // Topic and agent agreement are independent: a later step may only need
      // the one the joined origins actually agree on (e.g. an explicit target
      // topic only needs the joined agent to be unambiguous).
      const topics = new Set(resolved.map(r => r.topic));
      const agents = new Set(resolved.map(r => r.agent));
      return {
        resolved,
        forwardState: {
          topic: topics.size === 1 ? resolved[0].topic : null,
          agent: agents.size === 1 ? resolved[0].agent : null,
        },
      };
    }
    return { resolved }; // comma/single: no single forward state — each branch carries its own
  }

  function renderAtom(atom, { fromState } = {}) {
    let s = '';
    if (atom.topic != null && !(fromState && fromState.topic === atom.topic && !atom._forceTopic)) s += `#${atom.topic}`;
    if (atom.agent != null && !(fromState && fromState.agent === atom.agent && !atom._forceAgent)) s += `${s ? '' : ''}@${atom.agent}`;
    if (!s) s = atom.topic != null ? `#${atom.topic}` : `@${atom.agent}`;
    if (atom.fresh) s += '!';
    return s;
  }

  function fullAtomText(resolved) {
    return `#${resolved.topic}@${resolved.agent}${resolved.fresh ? '!' : ''}`;
  }

  // Expands a parsed clause into independent branches. Each branch is a
  // linear sequence of resolved steps (plus join metadata where relevant).
  function expand(clause) {
    const originResolution = resolveGroupAgainstState(clause.origin, { topic: null, agent: null }, true);

    let branches;
    if (clause.origin.kind === 'plus') {
      branches = [{
        steps: [{ kind: 'join', atoms: originResolution.resolved, forwardState: originResolution.forwardState }],
      }];
    } else {
      branches = originResolution.resolved.map(r => ({
        steps: [{ kind: 'atom', atom: r, forwardState: { topic: r.topic, agent: r.agent } }],
      }));
    }

    for (const hop of clause.hops) {
      const nextBranches = [];
      for (const branch of branches) {
        const last = branch.steps[branch.steps.length - 1];
        const state = last.forwardState;

        if (hop.op.type === 'roundtrip') {
          if (last.kind !== 'atom') {
            throw new FlowError('A join cannot have a return step (no single root to return to) — write the join as one-way only', 0);
          }
          const { resolved } = resolveGroupAgainstState(hop.target, state, false);
          for (const t of resolved) {
            // Segments appended *after* the origin, which is already its own
            // step — do not re-include it at the front here.
            const appended = [];
            for (let i = 0; i < hop.op.rounds; i += 1) {
              appended.push(t);
              appended.push(last.atom);
            }
            nextBranches.push({
              steps: branch.steps.concat([{
                kind: 'roundtrip',
                origin: last.atom,
                target: t,
                rounds: hop.op.rounds,
                wait: hop.op.wait,
                appendedSeq: appended,
              }]),
            });
          }
          continue;
        }

        // oneway / scheduled — resolveGroupAgainstState throws if the target
        // actually needs a field that's ambiguous (null) in state.
        const { resolved } = resolveGroupAgainstState(hop.target, state, false);
        for (const t of resolved) {
          nextBranches.push({
            steps: branch.steps.concat([{
              kind: 'atom',
              atom: t,
              forwardState: { topic: t.topic, agent: t.agent },
              via: hop.op,
            }]),
          });
        }
      }
      branches = nextBranches;
    }

    return branches;
  }

  // ---- Rendering: canonical (reduced) and fully-expanded forms ------------

  function opToText(op) {
    if (op.type === 'oneway') return op.alias ? '=>' : '>';
    if (op.type === 'scheduled') {
      let s = `=${op.count}`;
      if (op.wait) s += `:${op.wait}`;
      return `${s}>`;
    }
    return '';
  }

  function renderStepAtomCanonical(resolved, priorState) {
    const dropTopic = priorState && priorState.topic === resolved.topic && resolved.written && resolved.written.topic == null;
    const dropAgent = priorState && priorState.agent === resolved.agent && resolved.written && resolved.written.agent == null;
    let s = '';
    if (!dropTopic) s += `#${resolved.topic}`;
    if (!dropAgent) s += `@${resolved.agent}`;
    if (!s) s = `@${resolved.agent}`; // both matched root somehow; keep agent visible
    if (resolved.fresh) s += '!';
    return s;
  }

  function canonicalForBranch(branch) {
    let text = '';
    let priorState = null;
    for (const step of branch.steps) {
      if (step.kind === 'join') {
        const parts = step.atoms.map(a => `#${a.topic}@${a.agent}${a.fresh ? '!' : ''}`);
        text += parts.join('+');
        priorState = step.forwardState;
      } else if (step.kind === 'roundtrip') {
        const opText = step.rounds === 1 && !step.wait ? '<>' : `<${step.rounds}${step.wait ? ':' + step.wait : ''}>`;
        text += opText + renderStepAtomCanonical(step.target, priorState);
        priorState = { topic: step.target.topic, agent: step.target.agent };
      } else {
        if (text) text += opToText(step.via || { type: 'oneway', alias: false });
        text += renderStepAtomCanonical(step.atom, priorState);
        priorState = { topic: step.atom.topic, agent: step.atom.agent };
      }
    }
    return text;
  }

  // ---- Canonical KEY: condensed, order-independent, write-only --------------
  // `canonical` (above) is meant to be read (and, in principle, re-entered) —
  // it flattens every independent branch into its own human-readable string.
  // `key` serves a different purpose entirely: it's a DB/dedup identity token
  // for "same workflow," never re-parsed as flow syntax by anything (nothing
  // in this codebase currently reads it back — see the top-level `parse()`
  // doc below). Because it's write-only, it's safe to do what `canonical`
  // can't: resolve every atom first (respecting the order-sensitive rolling
  // anchor — see resolveGroupAgainstState — so this step alone must still
  // happen in original order), and only THEN group the resulting
  // *already-resolved* (topic, agent) pairs. Once an atom is fully resolved
  // it's just a plain fact with no remaining positional ambiguity, so
  // reordering facts is fine — it's reordering the ambiguous, not-yet-resolved
  // written atoms that silently changes meaning (that was the actual bug in
  // an earlier version of this function, which sorted before resolving).
  // The result: two differently-ordered or differently-phrased inputs that
  // resolve to the same set of turns get the same key, which is exactly what
  // an identity key for "same workflow" should do.
  //
  // Grouping strategy: greedy max-coverage dominating set, not a single fixed
  // sort axis. A fixed axis (e.g. "always sort/group by topic") leaves real
  // savings on the table whenever agents repeat more than topics do, and the
  // reverse (group by agent) does the same when topics repeat more — neither
  // axis dominates the other in general, and an atom can drop at most one
  // field either way. So: repeatedly pick whichever remaining atom would
  // "cover" the most other remaining atoms (an atom covers another if they
  // share a topic OR an agent — the atom could drop that shared field once
  // written against this one as root); that atom becomes a run's anchor,
  // written in full, and every atom it covers joins the run, dropping
  // whichever field it shares with the anchor. Remove the run, repeat on
  // what's left. This also lets a single anchor's run mix which atoms drop
  // topic and which drop agent (some members may match on topic, others on
  // agent), which a single-axis sort can never do. Ties (equal coverage) are
  // broken by ascending (topic, agent) so the result stays deterministic —
  // same atom set always yields the same key, which is the only actual
  // requirement (ADR-0032: "canonicalization must produce exactly one shape
  // per meaning"); nothing about that requirement demands a single sort axis,
  // only that the function be pure. This is a greedy approximation of
  // minimum dominating set (NP-hard in general) — not guaranteed globally
  // optimal, but exact for the small atom counts a broadcast/join list
  // actually has, and it always matches or beats either single-axis sort.
  function minimalGroupedText(resolvedAtoms, sep) {
    let remaining = resolvedAtoms.slice();
    const runs = [];
    while (remaining.length) {
      let anchor = null;
      let anchorCover = null;
      for (const cand of remaining) {
        const cover = remaining.filter(a => a.topic === cand.topic || a.agent === cand.agent);
        const better = !anchor || cover.length > anchorCover.length ||
          (cover.length === anchorCover.length &&
            (cand.topic < anchor.topic ||
              (cand.topic === anchor.topic && cand.agent < anchor.agent)));
        if (better) { anchor = cand; anchorCover = cover; }
      }
      runs.push({ anchor, members: anchorCover });
      const covered = new Set(anchorCover);
      remaining = remaining.filter(a => !covered.has(a));
    }
    runs.sort((r1, r2) =>
      r1.anchor.topic < r2.anchor.topic ? -1 : r1.anchor.topic > r2.anchor.topic ? 1
        : r1.anchor.agent < r2.anchor.agent ? -1 : r1.anchor.agent > r2.anchor.agent ? 1 : 0);
    const parts = [];
    for (const run of runs) {
      const a0 = run.anchor;
      parts.push(`#${a0.topic}@${a0.agent}${a0.fresh ? '!' : ''}`);
      for (const a of run.members) {
        if (a === a0) continue;
        const fresh = a.fresh ? '!' : '';
        parts.push(a.topic === a0.topic ? `@${a.agent}${fresh}` : `#${a.topic}${fresh}`);
      }
    }
    return parts.join(sep);
  }

  // Same "resolve each atom independently against the parent state" rule as
  // resolveGroupAgainstState's hop-target path (no sibling-to-sibling
  // inheritance — only an origin list rolls, since only an origin has no
  // parent to inherit from instead), but returns null rather than throwing
  // when a field can't be resolved — used only here, where the key needs to
  // degrade gracefully (order-preserving, no sort/group) rather than fail
  // outright when upstream branches disagree on a field this group needs.
  function resolveGroupSoft(atoms, state) {
    const out = [];
    for (const atom of atoms) {
      const topic = atom.topic != null ? atom.topic : (state ? state.topic : null);
      const agent = atom.agent != null ? atom.agent : (state ? state.agent : null);
      if (topic == null || agent == null) return null;
      out.push({ topic, agent, fresh: atom.fresh });
    }
    return out;
  }

  // Order-preserving fallback for when a hop-target group can't be fully
  // resolved (branches upstream disagree on a field it needs) — keeps atoms
  // exactly as written rather than guessing at a grouping for an unknown value.
  function verbatimGroupText(atoms, sep) {
    return atoms.map(renderAtom).join(sep);
  }

  // Target atoms never borrow from each other (resolveGroupAgainstState's
  // isOrigin=false path is a stateless per-atom map against one fixed parent
  // state — there is no rolling root for targets), so minimalGroupedText's
  // sibling-coverage grouping would be structurally wrong here: it could
  // drop a field because it happens to match a *sibling's* value, but
  // re-parsing a bare target atom only ever looks up the parent state, never
  // a sibling — silently corrupting the value on round-trip whenever a
  // sibling's field coincides with something other than the parent's. Each
  // target atom can only safely drop a field that matches the parent state
  // directly.
  function dropAgainstParentText(resolved, uniformState) {
    return resolved.map(a => {
      const dropTopic = !!uniformState && uniformState.topic != null && uniformState.topic === a.topic;
      const dropAgent = !!uniformState && uniformState.agent != null && uniformState.agent === a.agent;
      let s = '';
      if (!dropTopic) s += `#${a.topic}`;
      if (!dropAgent) s += `@${a.agent}`;
      if (!s) s = `@${a.agent}`; // both matched parent; keep agent visible
      if (a.fresh) s += '!';
      return s;
    });
  }

  function keyGroupText(group, uniformState) {
    const sep = group.kind === 'plus' ? '+' : ',';
    const resolved = resolveGroupSoft(group.atoms, uniformState);
    return resolved ? dropAgainstParentText(resolved, uniformState).join(sep) : verbatimGroupText(group.atoms, sep);
  }

  function keyOriginGroupText(resolvedAtoms, kind) {
    // Origins are already fully resolved by the caller (resolveGroupAgainstState),
    // in original order — grouping is safe here for the reasons above.
    return minimalGroupedText(resolvedAtoms, kind === 'plus' ? '+' : ',');
  }

  function canonicalKeyForClause(clause) {
    const { resolved: originResolved, forwardState: joinForwardState } =
      resolveGroupAgainstState(clause.origin, { topic: null, agent: null }, true);
    let text = keyOriginGroupText(originResolved, clause.origin.kind);

    // Track topic/agent agreement across all live branches independently —
    // a hop target can still safely drop a redundant field even when
    // origins disagree on the OTHER field (mirrors '+' forwardState below).
    let uniformState;
    if (clause.origin.kind === 'plus') {
      uniformState = joinForwardState;
    } else {
      const topics = new Set(originResolved.map(r => r.topic));
      const agents = new Set(originResolved.map(r => r.agent));
      uniformState = {
        topic: topics.size === 1 ? originResolved[0].topic : null,
        agent: agents.size === 1 ? originResolved[0].agent : null,
      };
    }

    for (const hop of clause.hops) {
      const opText = hop.op.type === 'roundtrip'
        ? (hop.op.rounds === 1 && !hop.op.wait ? '<>' : `<${hop.op.rounds}${hop.op.wait ? ':' + hop.op.wait : ''}>`)
        : opToText(hop.op);
      text += opText + keyGroupText(hop.target, uniformState);
      if (hop.op.type === 'roundtrip') break; // never chained further (see parseClause)
      uniformState = (uniformState && hop.target.atoms.length === 1)
        ? {
            topic: hop.target.atoms[0].topic != null ? hop.target.atoms[0].topic : uniformState.topic,
            agent: hop.target.atoms[0].agent != null ? hop.target.atoms[0].agent : uniformState.agent,
          }
        : null;
    }
    return text;
  }

  // A scheduled edge's repeats are independent completions of the same
  // edge, not a continuing chain — so they fork the trace into separate
  // instances, each rendered as its own row, rather than more '>' hops.
  // Everything else (join/roundtrip/plain oneway) stays a single instance.
  const SCHEDULED_PREVIEW_CAP = 5;

  // wait is always `<digits><unit>` (e.g. "1d", "30m") — multiply the
  // number to get each repeat's cumulative offset from the trigger.
  function scheduledOffsetLabel(wait, multiplier) {
    const m = /^([0-9]+)([A-Za-z]+)$/.exec(wait);
    if (!m) return null;
    return `${parseInt(m[1], 10) * multiplier}${m[2]}`;
  }

  function scheduledRunNote(via, i, totalLabel) {
    const note = `run ${i + 1} of ${totalLabel}`;
    if (!via.wait) return note;
    const offset = scheduledOffsetLabel(via.wait, i + 1);
    return offset ? `${note} · +${offset}` : note;
  }

  // A round-trip's wait applies symmetrically to every hop (out and back
  // alike, see ADR-0032) — hop i (0-indexed) is the (i+1)th hop overall, so
  // it fires at (i+1)*wait after the trigger, same multiplier convention as
  // scheduledRunNote.
  function roundtripHopNote(step, i) {
    const roundNum = Math.floor(i / 2) + 1;
    const leg = i % 2 === 0 ? 'out' : 'return';
    const note = `round ${roundNum} of ${step.rounds} (${leg})`;
    if (!step.wait) return note;
    const offset = scheduledOffsetLabel(step.wait, i + 1);
    return offset ? `${note} · +${offset}` : note;
  }

  // A branch normally renders as one row. A scheduled edge only delays
  // *reaching the target* repeatedly — the origin side of that edge still
  // only ran once — so everything up to (and including) the origin of the
  // first scheduled edge renders as a single "trunk" row, and only the
  // repeated target hops fork into their own rows below it.
  function expandedForBranch(branch) {
    const trunk = [];
    let forkedAt = -1;
    for (let idx = 0; idx < branch.steps.length; idx += 1) {
      const step = branch.steps[idx];
      if (step.via && step.via.type === 'scheduled') { forkedAt = idx; break; }
      if (step.kind === 'roundtrip' && step.wait) { forkedAt = idx; break; }
      if (step.kind === 'join') trunk.push(step.atoms.map(fullAtomText).join('+'));
      else if (step.kind === 'roundtrip') for (const atom of step.appendedSeq) trunk.push(fullAtomText(atom));
      else trunk.push(fullAtomText(step.atom));
    }
    if (forkedAt === -1) return [{ text: trunk.join('>'), note: null, continuation: false }];

    const trunkRow = { text: trunk.join('>'), note: 'runs immediately once', continuation: false };
    // Each seg carries its OWN connecting arrow, not a shared row-level one —
    // a single row can accumulate segs from more than one hop (e.g. a plain
    // '>' step chained after a scheduled/roundtrip fork), and only the segs
    // that actually carry a wait should render as `=1:T>` instead of a bare
    // '>'. In isolation, one delayed repeat/hop IS exactly a `=1:T>` one-way
    // edge from whatever came before it — so that's what its arrow shows.
    let instances = [{ segs: [], note: null }];
    let truncated = false;
    for (let idx = forkedAt; idx < branch.steps.length; idx += 1) {
      const step = branch.steps[idx];
      if (step.kind === 'join') {
        const text = step.atoms.map(fullAtomText).join('+');
        for (const inst of instances) inst.segs.push({ arrow: '>', text });
      } else if (step.kind === 'roundtrip' && step.wait) {
        const next = [];
        const arrow = `=1:${step.wait}>`;
        for (const inst of instances) {
          for (let i = 0; i < step.appendedSeq.length; i += 1) {
            const text = fullAtomText(step.appendedSeq[i]);
            const note = roundtripHopNote(step, i);
            next.push({ segs: inst.segs.concat([{ arrow, text }]), note: inst.note ? `${inst.note}; ${note}` : note });
          }
        }
        instances = next;
      } else if (step.kind === 'roundtrip') {
        for (const atom of step.appendedSeq) {
          const text = fullAtomText(atom);
          for (const inst of instances) inst.segs.push({ arrow: '>', text });
        }
      } else if (step.via && step.via.type === 'scheduled') {
        const text = fullAtomText(step.atom);
        const repeats = step.via.count;
        if (repeats > SCHEDULED_PREVIEW_CAP) truncated = true;
        const shown = Math.min(repeats, SCHEDULED_PREVIEW_CAP);
        const totalLabel = String(step.via.count);
        const arrow = step.via.wait ? `=1:${step.via.wait}>` : '>';
        const next = [];
        for (const inst of instances) {
          for (let i = 0; i < shown; i += 1) {
            const note = scheduledRunNote(step.via, i, totalLabel);
            next.push({ segs: inst.segs.concat([{ arrow, text }]), note: inst.note ? `${inst.note}; ${note}` : note });
          }
        }
        instances = next;
      } else {
        const text = fullAtomText(step.atom);
        for (const inst of instances) inst.segs.push({ arrow: '>', text });
      }
    }
    const deltaRows = instances.map(inst => ({
      text: inst.segs.map(s => `${s.arrow}${s.text}`).join(''),
      note: inst.note,
      continuation: true,
    }));
    if (truncated) deltaRows.push({ text: '…', note: null, continuation: true });
    return [trunkRow, ...deltaRows];
  }

  // ---- Public entry point ---------------------------------------------------

  function parse(text) {
    try {
      const clause = parseClause(text);
      const branches = expand(clause);
      const canonical = branches.map(canonicalForBranch);
      return {
        ok: true,
        canonical,
        // Flattened so each concrete instance (scheduled repeats fork a
        // branch into several) renders as its own row, not crammed onto one.
        expanded: branches.flatMap(expandedForBranch),
        // The fully-reduced, order-independent, condensed form — built from
        // the parsed clause itself (not the expanded branches), so a
        // Cartesian fan-out doesn't balloon into one string per branch. This
        // is what gets saved to the DB and used as the identity key for
        // "same workflow" — the per-branch `canonical` breakdown above is
        // only an interim display aid. Write-only: nothing re-parses `key`
        // as flow syntax, which is what makes order-independence safe here
        // even under rolling anchor (ADR-0032) — see sortedGroupedKeyText.
        key: canonicalKeyForClause(clause),
        branches,
      };
    } catch (err) {
      if (err instanceof FlowError) {
        return { ok: false, error: err.message, pos: err.pos };
      }
      return { ok: false, error: String(err && err.message || err) };
    }
  }

  const SquidFlow = { parse, FlowError };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = SquidFlow;
  } else {
    global.SquidFlow = SquidFlow;
  }
})(typeof window !== 'undefined' ? window : globalThis);
