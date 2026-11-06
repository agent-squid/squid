# Test a watch with GitHub CLI over stdio MCP

`tools/gh_watch_mcp.py` is a development/test utility, not an installed console
entry point. It exposes read tools `read_issue_comments` and `read_issues`, and uses
`gh api` with GitHub CLI's existing authentication. It does not post comments,
close issues, resolve review threads, or contain watch/change-detection logic.
PR conversation comments work; inline PR review comments are not included.

## Configure the host

Install the MCP extra in the Python environment used to launch the utility.
Ensure `gh` is on the Squid server's PATH and authenticated for the same OS user
(`gh auth status --hostname github.com`). Stdio transport uses a restricted
inherited environment; do not assume a shell-only `GH_TOKEN` reaches it. The
normal `gh` credential store must be usable without an interactive prompt.

Merge this into the host's `squid.yaml`, using persistent absolute paths in the
backing checkout, never a per-turn worktree path:

```yaml
watches:
  enabled: true
  connections:
    github-test:
      connection:
        transport: stdio
        argv:
          - /absolute/path/to/squid/.venv/bin/python
          - /absolute/path/to/squid/tools/gh_watch_mcp.py
      read_tools: [read_issue_comments, read_issues]
```

Preserve existing watch policy/connections. If `policy.adapters` is explicitly
restricted, it must include `plan`. Restart the host after configuration changes.
The MCP server starts on demand; do not launch it manually in the background.
No production configuration is modified by adding this utility.

## Propose a watch

Replace `OWNER`, `REPO` and issue number in this example. Use a throwaway issue
with at most 50 short comments. Save as `watch.json`:

```json
{
  "target": "https://github.com/OWNER/REPO/issues/1",
  "request_key": "github-comments-test-1",
  "plan": {
    "version": 1,
    "initial": "quiet",
    "steps": [{
      "server": "github-test",
      "tool": "read_issue_comments",
      "arguments": {"owner": "OWNER", "repo": "REPO", "issue_number": 1},
      "items": "/comments",
      "identity": ["/id", "/updated_at"],
      "complete": {"path": "/complete", "equals": true}
    }]
  }
}
```

Use `agentsquid tools --discover` to confirm availability and schemas. For
`watch validate`, supply only `target` and `plan`; for `watch create`, include
`request_key` as above. Set `SQUID_API_URL` to the issuing instance. Use the
separate human controls (`--human-topic TOPIC`) to record the exact-watch
observation grant and activate the returned watch/version. See
[authorization](watch-authorization.md) for grant and activation JSON/commands.
Creation and validation alone do not start monitoring.

## Exercise the loop

1. Activate, then wait for the first successful poll to establish the quiet
   baseline. Comments added before that poll belong to the baseline.
2. Add a comment manually; wait at least the configured polling interval.
3. Confirm one new durable event. Further unchanged polls must not duplicate it.
4. Edit that comment; its changed `updated_at` should produce a new revision.
5. Optionally enable local execution policy and record a separate, finite
   execution grant. Confirm local dispatch through `agentsquid watch runs ID`.
6. Cancel the watch when finished.

The reader requests 51 comments to detect the 50-record limit. More than 50
comments, invalid output, failed reads, timeouts, raw API responses exceeding
4 MiB, or normalized results exceeding 64 KiB
fail explicitly. It does not truncate, paginate, or claim complete change
history: polling can miss intermediate revisions and deletions. GitHub's
`updated_at` timestamp also cannot distinguish multiple edits within the same
source timestamp resolution. No issue-state stop condition is included.
Watch-triggered publication remains disabled.

## Automated verification

```sh
uv run --extra mcp --with pytest --with pytest-asyncio python -m pytest tests/test_gh_watch_mcp.py -q
```

Tests use a fake `gh`, including real MCP stdio calls and a durable watch test
covering quiet baseline, new comments, edits, and event deduplication. They
require no GitHub account and make no external writes. A live watch still needs
host configuration, a target issue, grants and activation.

## Watch newly created repository issues

Use the same connection and authorization lifecycle, with this proposal instead:

```json
{
  "target": "https://github.com/OWNER/REPO/issues",
  "request_key": "github-new-issues-test-1",
  "plan": {
    "version": 1,
    "initial": "quiet",
    "steps": [{
      "server": "github-test",
      "tool": "read_issues",
      "arguments": {"owner": "OWNER", "repo": "REPO"},
      "items": "/issues",
      "identity": ["/id"],
      "complete": {"path": "/complete", "equals": true}
    }]
  }
}
```

After observation grant and activation, wait for a successful baseline poll,
then manually create a throwaway issue. The next poll should record one event;
unchanged polls and issue edits should not create further events. Cancel when
done. A local agent turn still requires its own execution grant.

The reader includes open and closed issues so closing an issue does not remove
it from the snapshot. Pull requests are excluded. The GitHub issues endpoint
returns PRs too: more than 50 combined issue/PR records fails explicitly, even
when fewer than 50 are issues. This is a bounded test reader, without pagination;
it does not support larger repositories or guarantee detection of deleted issues.
