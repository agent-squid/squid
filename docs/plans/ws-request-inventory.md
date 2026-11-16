# Browser transport request inventory

Static inventory of direct `fetch` and `EventSource` call sites in `ui/app.js`.
Includes non-Chat application calls because ADR-0040 has an application-wide target.
Dynamic URL construction, form/download URLs, and helper ownership still need manual audit;
this inventory is not evidence that the request-audit gate passes.

History WS preparation: `history.page` and `history.around` now have local handlers.
Browser rollout, large-result transfer, and Shore capability support remain pending.

Source snapshot: `ui/app.js`, SHA-256 `da93f1c3dd8be7708c7c4949aa3a2cf5581cbcca1cefcb803c744aaa180d26c6`.
Line numbers describe this snapshot and must be regenerated after source changes.

| Source line | Request expression |
| --- | --- |
| 2 | `` const featureFlags = fetch('/config/features', { cache: 'no-store' }) `` |
| 35 | `` const realtimeTransportMode = fetch('/config/realtime', { cache: 'no-store' }) `` |
| 249 | `` const res = await fetch(UPDATE_CHECK_URL); `` |
| 301 | `` const res = await fetch('/health', { cache: 'no-store' }); `` |
| 749 | `` const res = await fetch('/shore/devices'); `` |
| 781 | `` const res = await fetch('/shore/pairing/requests', { cache: 'no-store' }); `` |
| 859 | `` const res = await fetch(path, { method: 'POST', headers: { 'content-type': 'application/json' }, `` |
| 907 | `` fetch('/remote').then(res => res.json()).catch(() => ({})), `` |
| 1037 | `` const res = await fetch('/shore/pairing/begin', { method: 'POST' }); `` |
| 1082 | `` const res = await fetch(`/shore/pairing/status?ceremony_id=${encodeURIComponent(body.ceremony_id)}`); `` |
| 1154 | `` const res = await fetch('/shore/pairing/requests'); `` |
| 1179 | `` const res = await fetch('/shore/devices'); `` |
| 1219 | `` fetch('/shore/devices/revoke', { `` |
| 2648 | `` const res = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(8000) }); `` |
| 2724 | `` const res = await fetch(`/history/around?${params.toString()}`); `` |
| 2781 | `` const res = await fetch(`/history?${params.toString()}`, { cache: 'no-store', signal: AbortSignal.timeout(8000) }); `` |
| 3013 | `` let res = await fetch(`/history/around?${params.toString()}`); `` |
| 3018 | `` res = await fetch(`/history/around?${unfiltered}`); `` |
| 3707 | `` const res = await fetch('/prompts/recent?limit=50', { cache: 'no-store', signal: AbortSignal.timeout(8000) }); `` |
| 3737 | `` const res = await fetch(url); `` |
| 3948 | `` const res = await fetch('/cmd', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); `` |
| 3976 | `` const res = await fetch('/cmd', { `` |
| 3999 | `` const res = await fetch('/processes'); `` |
| 4019 | `` const res = await fetch('/processes'); `` |
| 4059 | `` const beforeBootTime = await fetch('/health') `` |
| 4063 | `` const res = await fetch('/cmd', { `` |
| 4077 | `` const r = await fetch('/health'); `` |
| 4968 | `` const response = await fetch(`/auth/session/${session.id}`, { cache: 'no-store' }); `` |
| 5047 | `` const response = await fetch(`/auth/session/${session.id}/input`, { `` |
| 5213 | `` fetch(`/auth/session/${_authSession.id}/resize`, { `` |
| 5378 | `` res = await fetch('/auth/session', { `` |
| 5391 | `` fetch(`/auth/session/${data.id}/cancel`, { method: 'POST' }).catch(() => {}); `` |
| 5406 | `` const es = new EventSource(`/auth/session/${data.id}/events`); `` |
| 5426 | `` fetch(`/auth/session/${_authSession.id}/input`, { `` |
| 5446 | `` else fetch(`/auth/session/${session.id}/cancel`, { method: 'POST' }).catch(() => {}); `` |
| 5470 | `` fetch(`/auth/session/${_authSession.id}/input`, { `` |
| 5532 | `` const recoveryBaseline = fetch('/processes').then(async res => { `` |
| 5655 | `` const response = await fetch('/cmd', { `` |
| 5676 | `` const statusRes = await fetch(`/chat/${msgId}/status`); `` |
| 5917 | `` fetch(`/chat/${msgId}/quota-delta`, { `` |
| 5924 | `` fetch('/stats/quota-delta', { `` |
| 5992 | `` const res = await fetch('/processes'); `` |
| 6275 | `` const statusRes = await fetch(`/chat/${id}/status`); `` |
| 6533 | `` const res = await fetch('/chat', { `` |
| 7541 | `` const res = await fetch('/localfile?' + new URLSearchParams({ path: conflictFile, _t: Date.now() })); `` |
| 7566 | `` const postRetry = force => fetch(`/chat/${msgId}/worktree/retry`, { `` |
| 7688 | `` const res = await fetch(`/chat/${msgId}/worktree/auto-resolve`, { `` |
| 7806 | `` const statusRes = await fetch(`/chat/${msgId}/worktree/status?` + `` |
| 7845 | `` const res = await fetch(`/chat/${msgId}/worktree/discard`, { `` |
| 7924 | `` const res = await fetch(`/chat/${msgId}/diff-file?repo=${encodeURIComponent(sourceRepo)}&path=${encodeURIComponent(file.path)}`); `` |
| 8022 | `` const res = await fetch(`/chat/${msgId}/revert`, { `` |
| 8089 | `` const res = await fetch(`/chat/${check.msgId}/diff-revert-status?repo=${encodeURIComponent(check.repo)}`, `` |
| 8336 | `` const response = await fetch(`/watch-status/${encodeURIComponent(w.id)}/${action}`, { `` |
| 8661 | `` const res = await fetch(`/chat/${item.id}/status`); `` |
| 9154 | `` const response = await fetch('/cmd', { `` |
| 9268 | `` const res = await fetch(`/chat/${msgId}/status`); `` |
| 9293 | `` const res = await fetch(`/chat/${msgId}/status`); `` |
| 9337 | `` const res = await fetch(`/chat/flow/${encodeURIComponent(flowRunId)}/steps?after_id=${cursor}`); `` |
| 9455 | `` const res = await fetch(`/chat/${item.id}/status`); `` |
| 9532 | `` const es = new EventSource(`/chat/${item.id}/events`); `` |
| 9563 | `` const res = await fetch(`/chat/${item.id}/status`); `` |
| 9660 | `` const res = await fetch(`/chat/${item.id}/status`); `` |
| 9720 | `` const res = await fetch(`/chat/${msgId}/status`); `` |
| 10499 | `` const res = await fetch('/health'); `` |
| 10850 | `` const res = await fetch(url); `` |
| 11063 | `` const res = await fetch(`/config/${popup.dataset.gauge}/max-budget`, { `` |
| 11078 | `` const res = await fetch(`/config/${popup.dataset.gauge}/max-budget`, { method: 'DELETE' }); `` |
| 11099 | `` const res = await fetch('/config/creds/codex', { `` |
| 11127 | `` fetch('/config/creds/capabilities').then(r => r.ok ? r.json() : null).then(data => { `` |
| 11140 | `` const res = await fetch('/config/creds/auto', { method: 'POST' }); `` |
| 11164 | `` const res = await fetch('/config/creds', { `` |
| 11482 | `` fetch(`/chat/previews?ids=${encodeURIComponent(ids.join(','))}`) `` |
| 12564 | `` _statsPresets = await fetch('/stats/filter-presets').then(r => r.json()); `` |
| 12620 | `` const res = await fetch('/stats/filter-presets', { `` |
| 12636 | `` const res = await fetch(`/stats/filter-presets/${conflict.id}`, { `` |
| 12661 | `` await fetch(`/stats/filter-presets/${existing.id}`, { `` |
| 12666 | `` await fetch(`/stats/filter-presets/${existing.id}`, { `` |
| 12671 | `` const res = await fetch('/stats/filter-presets', { `` |
| 12677 | `` await fetch(`/stats/filter-presets/${p.id}`, { `` |
| 12691 | `` const res = await fetch(`/stats/filter-presets/${_activeStatsPresetId}`, { `` |
| 12712 | `` const res = await fetch(`/stats/filter-presets/${_activeStatsPresetId}`, { `` |
| 13025 | `` const res = await fetch(`/watch-status/${encodeURIComponent(btn.dataset.watchId)}/${btn.dataset.action}`, { `` |
| 13063 | `` const response = await fetch('/cmd', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }); `` |
| 13090 | `` const response = await fetch('/cmd', { `` |
| 13174 | `` const res = await fetch(`/watch-status${suffix}`, { signal: controller.signal }); `` |
| 13217 | `` const [procRes, queueRes] = await Promise.all([fetch('/processes'), fetch('/queue')]); `` |
| 13262 | `` await fetch('/stats/filters').then(r => r.json()).then(data => { `` |
| 13272 | `` rows = await fetch(`/stats?${params}`).then(r => r.json()); `` |
| 13808 | `` await fetch(`/stats/filter-presets/${_activeStatsPresetId}`, { method: 'DELETE' }); `` |
| 13849 | `` _topicsManageCachePromise = fetch('/topics/manage?include_hidden=true') `` |
| 14078 | `` const res = await fetch(`/topics/${encodeURIComponent(topic)}/hidden`, { `` |
| 14176 | `` const res = await fetch(url, { method: 'DELETE' }); `` |
| 14184 | `` const res = await fetch(`/topics/${encodeURIComponent(topic)}`, { method: 'DELETE' }); `` |
| 14682 | `` const res = await fetch('/config/yaml'); `` |
| 14738 | `` const res = await fetch('/config/yaml', { `` |
| 14766 | `` fetch('/config/agents').then(r => r.json()), `` |
| 14767 | `` fetch('/health').then(r => r.json()).catch(() => null), `` |
| 14903 | `` const sessions = await fetch(`/config/agents/${encodeURIComponent(name)}/sessions`).then(r => r.ok ? r.json() : null).catch(() => null); `` |
| 14910 | `` const res = await fetch(`/config/agents/${encodeURIComponent(name)}/home-mode`, { `` |
| 14953 | `` await fetch(`/config/agents/${btn.dataset.name}`, { method: 'DELETE' }); `` |
| 14999 | `` const sessions = await fetch(`/config/agents/${encodeURIComponent(body.name)}/sessions`).then(r => r.ok ? r.json() : null).catch(() => null); `` |
| 15009 | `` const res = await fetch('/config/agents', { `` |
| 15050 | `` fetch(`/topics/${encodeURIComponent(topic)}/memory/squid/seed`, { method: 'POST' }).catch(() => {}); `` |
| 15164 | `` const res = await fetch('/config/agents', { `` |
| 15191 | `` try { _topicsCache = await (await fetch('/topics')).json(); } catch { _topicsCache = []; } `` |
| 15198 | `` _agentsCachePromise = fetch('/config/agents') `` |
| 15321 | `` await fetch(`/topics/${encodeURIComponent(name)}/hidden`, { `` |
| 15586 | `` fetch(`/topics/${encodeURIComponent(topic)}/agents/history`).then(r => r.ok ? r.json() : []).catch(() => []), `` |
| 15662 | `` topic ? fetch(`/topics/${encodeURIComponent(topic)}/agents/history`).then(r => r.ok ? r.json() : []).catch(() => []) : Promise.resolve([]), `` |
| 15734 | `` fetch(`/topics/${encodeURIComponent(topic)}/agents/history`).then(r => r.ok ? r.json() : []).catch(() => []), `` |
| 15779 | `` fetch(`/topics/${encodeURIComponent(topic)}/agents/history`).then(r => r.ok ? r.json() : []).catch(() => []), `` |
| 15922 | `` const res = await fetch(`${INSIGHTS_URL}?_=${Date.now()}`); `` |
| 16045 | `` const res = await fetch(`/stats?${params}`); `` |
| 16236 | `` const res = await fetch('/health'); `` |
| 16446 | `` const { path, entries, source, turns } = await fetch(`/session-log?${q}`).then(r => r.json()); `` |
| 16448 | `` const text = await fetch('/localfile?' + new URLSearchParams({ path, _t: Date.now() })).then(r => r.text()); `` |
| 16487 | `` fetch(`/chat/${id}/status`) `` |
| 16539 | `` const response = await fetch(`/chat/${encodeURIComponent(msgId)}/context`); `` |
| 16636 | `` const msg = await fetch(`/chat/${msgId}/status`).then(r => r.json()); `` |
| 17115 | `` const eventsText = await fetch(`/chat/${msgId}/events?after_seq=-1`).then(r => r.text()); `` |
| 17122 | `` const msg = await fetch(`/chat/${msgId}/status`).then(r => r.json()); `` |
| 17235 | `` const res = await fetch('/localfile/check-paths', { `` |
| 17378 | `` fetch(`/topics/${encodeURIComponent(topic)}/memory`) `` |
| 17392 | `` const data = await fetch(`/topics/${encodeURIComponent(topic)}/memory`).then(r => r.json()); `` |
| 17412 | `` const res = await fetch(`/topics/${encodeURIComponent(topic)}/memory/squid/code-roots`, { `` |
| 17439 | `` fetch(`/topics/${encodeURIComponent(topic)}/session?agent=${encodeURIComponent(agent)}`) `` |
| 17480 | `` const res = await fetch(`/topics/${encodeURIComponent(topic)}/session?agent=${encodeURIComponent(agent)}`); `` |
| 17508 | `` const res = await fetch(`/topics/${encodeURIComponent(target.topic)}/session?agent=${encodeURIComponent(target.agent)}`); `` |
| 17549 | `` const meta = await fetch(`/topics/${encodeURIComponent(topic)}/memory`) `` |
| 17559 | `` session = await fetch(`/topics/${encodeURIComponent(topic)}/session?agent=${encodeURIComponent(agent)}`) `` |
| 17848 | `` const data = await fetch(`/topics/${encodeURIComponent(topic)}/memory/squid/seed`, { method: 'POST' }).then(r => r.json()); `` |
| 17874 | `` const res = await fetch(`/topics/${encodeURIComponent(topic)}/memory`, { `` |
| 17923 | `` const res = await fetch('/bookmarks'); `` |
| 17932 | `` await fetch('/bookmarks', { method: 'POST', headers: {'Content-Type':'application/json'}, `` |
| 17938 | `` const res2 = await fetch('/bookmarks'); `` |
| 17948 | `` fetch(`/bookmarks/${msgId}`, { method: 'DELETE' }).catch(() => {}); `` |
| 17953 | `` fetch('/bookmarks', { method: 'POST', headers: {'Content-Type':'application/json'}, `` |
| 18045 | `` const res = await fetch('/annotations?kind=bad_response'); `` |
| 18061 | `` fetch(`/annotations/bad_response/${msgId}`, { method: 'DELETE' }).catch(() => {}); `` |
| 18065 | `` fetch('/annotations', { `` |
| 19047 | `` fetch('/localfile?' + new URLSearchParams({ path: '/tmp' })) `` |
| 19341 | `` const res = await fetch(endpoint, { `` |
| 19368 | `` const res = await fetch('/localfile/rename', { `` |
| 19391 | `` const res = await fetch('/localfile/delete', { `` |
| 19419 | `` const res = await fetch('/localfile/upload?' + new URLSearchParams({ parent: parentPath, name: file.name }), { `` |
| 19725 | `` const res = await fetch('/localfile?' + new URLSearchParams({ path, _t: Date.now() })); `` |
| 19743 | `` const res = await fetch('/localfile', { `` |
| 19797 | `` const res = await fetch('/localfile/history?' + new URLSearchParams({ path })); `` |
| 19841 | `` const r = await fetch('/localfile/revert-edit', { `` |
| 19937 | `` const rootsRes = await fetch('/config/localfile-roots'); `` |
| 19940 | `` const healthRes = await fetch('/health').catch(() => null); `` |
| 19961 | `` const res = await fetch('/localfile?' + new URLSearchParams({ path, _t: Date.now() })); `` |
| 20526 | `` fetch('/config/creds', { `` |
| 20545 | `` fetch('/config/creds/codex', { `` |
| 20664 | `` const response = await fetch(`/history?${params}`, { cache: 'no-store', signal: AbortSignal.timeout(8000) }); `` |
