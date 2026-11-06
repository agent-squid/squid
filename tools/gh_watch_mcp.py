"""Read-only GitHub issue and comment MCP server for watch integration testing.

Run with a Python environment containing agentsquid[mcp]. Authentication uses
GitHub CLI's own credential store. No watch state or change detection lives here.
"""
import json
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlencode


LIMIT = 65_536
# GitHub includes user profiles, reactions and other fields discarded below.
RAW_LIMIT = 4 * 1024 * 1024


def read_issue_comments(owner: str, repo: str, issue_number: int) -> dict[str, Any]:
    """Read a complete, bounded issue-comment snapshot; also works for PR issue comments.

    Excludes PR inline review threads. More than 50 comments or oversized output
    fails explicitly rather than returning an incomplete snapshot.
    """
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}', owner)
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', repo)
            or repo in {'.', '..'} or type(issue_number) is not int or issue_number < 1):
        raise ValueError('Invalid GitHub issue coordinates')
    comments = _read(f'repos/{owner}/{repo}/issues/{issue_number}/comments?per_page=51')
    if not isinstance(comments, list):
        raise ValueError('GitHub returned an unexpected comment response')
    if len(comments) > 50:
        raise ValueError('Incomplete snapshot: more than 50 comments; pagination is unsupported')
    records = []
    for comment in comments:
        if (not isinstance(comment, dict) or type(comment.get('id')) is not int
                or not isinstance(comment.get('updated_at'), str) or not comment['updated_at']
                or not isinstance(comment.get('body'), str)
                or not isinstance(comment.get('html_url'), str)):
            raise ValueError('GitHub comment lacks required identity or content fields')
        records.append({key: comment[key] for key in ('id', 'updated_at', 'body', 'html_url')})
    snapshot = {'comments': records, 'complete': True}
    # Match the ASCII-escaped JSON accounting in Squid's MCP transport.
    if len(json.dumps(snapshot, ensure_ascii=True, allow_nan=False).encode()) > LIMIT:
        raise ValueError('GitHub snapshot exceeds the watch output limit')
    return snapshot



def _read(endpoint: str) -> Any:
    gh = shutil.which('gh')
    if not gh:
        raise ValueError('GitHub CLI is unavailable')
    # Request one extra record to detect truncation without pagination. Force GET;
    # literal arguments and validated path segments never pass through a shell.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                [gh, 'api', '--hostname', 'github.com', '--method', 'GET', endpoint],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                timeout=12, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('GitHub read failed or timed out') from None
        if result.returncode:
            raise ValueError('GitHub read failed; check access and gh authentication')
        output.seek(0)
        raw = output.read(RAW_LIMIT + 1)
    if len(raw) > RAW_LIMIT:
        raise ValueError('GitHub response exceeds the raw input limit')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('GitHub returned invalid JSON') from None


def read_issues(owner: str, repo: str) -> dict[str, Any]:
    """Read up to 50 repository issues, open or closed, excluding pull requests.

    Fails if the repository has more than 50 issue/PR records. Use /id alone
    as watch identity to detect newly created issues without triggering on edits.
    """
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}', owner)
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', repo) or repo in {'.', '..'}):
        raise ValueError('Invalid GitHub repository coordinates')
    issues = _read(f'repos/{owner}/{repo}/issues?state=all&sort=created&direction=asc&per_page=51')
    if not isinstance(issues, list):
        raise ValueError('GitHub returned an unexpected issue response')
    if len(issues) > 50:
        raise ValueError('Incomplete snapshot: more than 50 issue/PR records; pagination is unsupported')
    records = []
    for issue in issues:
        if not isinstance(issue, dict):
            raise ValueError('GitHub returned an invalid issue record')
        if 'pull_request' in issue:
            continue
        if (type(issue.get('id')) is not int or type(issue.get('number')) is not int
                or issue['id'] < 1 or issue['number'] < 1
                or any(not isinstance(issue.get(key), str) or not issue[key]
                       for key in ('title', 'html_url', 'created_at', 'updated_at'))
                or issue.get('state') not in ('open', 'closed')
                or issue.get('body') is not None and not isinstance(issue['body'], str)):
            raise ValueError('GitHub issue lacks required identity or content fields')
        records.append({key: issue.get(key) for key in
                        ('id', 'number', 'title', 'body', 'html_url', 'created_at', 'updated_at', 'state')})
    snapshot = {'issues': records, 'complete': True}
    if len(json.dumps(snapshot, ensure_ascii=True, allow_nan=False).encode()) > LIMIT:
        raise ValueError('GitHub snapshot exceeds the watch output limit')
    return snapshot


def read_bugs(owner: str, repo: str, author: str, author_id: int, label: str = 'bug') -> dict[str, Any]:
    """Read issues filed by one verified GitHub account with one required label.

    Includes open and closed issues. Reads at most five 100-record pages and
    fails rather than returning a truncated snapshot. Identity /id detects
    issues newly entering this filtered set, including later label additions.
    This filters task data; it does not authorize execution or publication.
    """
    login_pattern = r'[A-Za-z0-9][A-Za-z0-9-]{0,99}'
    if (not isinstance(owner, str) or not re.fullmatch(login_pattern, owner)
            or not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', repo)
            or repo in {'.', '..'} or not isinstance(author, str)
            or not re.fullmatch(login_pattern, author) or type(author_id) is not int or author_id < 1
            or not isinstance(label, str) or not 1 <= len(label) <= 50
            or label != label.strip() or ',' in label
            or any(ord(c) < 32 or ord(c) == 127 for c in label)):
        raise ValueError('Invalid GitHub bug filter')
    account = _read(f'users/{author}')
    if (not isinstance(account, dict) or type(account.get('id')) is not int
            or account['id'] != author_id or not isinstance(account.get('login'), str)
            or account['login'].casefold() != author.casefold()):
        raise ValueError('GitHub author identity could not be verified')
    records, seen = [], set()
    for page in range(1, 6):
        query = urlencode(dict(state='all', creator=account['login'], labels=label,
                               sort='created', direction='asc', per_page=100, page=page))
        issues = _read(f'repos/{owner}/{repo}/issues?{query}')
        if not isinstance(issues, list) or len(issues) > 100:
            raise ValueError('GitHub returned an unexpected issue page')
        for issue in issues:
            if (not isinstance(issue, dict) or type(issue.get('id')) is not int
                    or issue['id'] < 1 or issue['id'] in seen):
                raise ValueError('GitHub issue identity is invalid or pagination changed')
            seen.add(issue['id'])
            if 'pull_request' in issue:
                continue
            user, labels = issue.get('user'), issue.get('labels')
            if (not isinstance(user, dict) or type(user.get('id')) is not int
                    or not isinstance(user.get('login'), str) or not isinstance(labels, list)
                    or any(not isinstance(item, dict) or not isinstance(item.get('name'), str)
                           for item in labels)):
                raise ValueError('GitHub issue lacks author or label evidence')
            # Recheck provider filters locally, including immutable account ID.
            if (user['id'] != account['id'] or user['login'].casefold() != author.casefold()
                    or not any(item['name'].casefold() == label.casefold() for item in labels)):
                continue
            if (type(issue.get('number')) is not int or issue['number'] < 1
                    or any(not isinstance(issue.get(key), str) or not issue[key]
                           for key in ('title', 'html_url', 'created_at', 'updated_at'))
                    or issue.get('state') not in ('open', 'closed')
                    or issue.get('body') is not None and not isinstance(issue['body'], str)):
                raise ValueError('GitHub issue lacks required identity or content fields')
            record = {key: issue.get(key) for key in
                      ('id', 'number', 'title', 'body', 'html_url', 'created_at', 'updated_at', 'state')}
            record.update(author={'id': user['id'], 'login': user['login']},
                          labels=[item['name'] for item in labels])
            records.append(record)
            if len(json.dumps({'issues': records, 'complete': True}, allow_nan=False).encode()) > LIMIT:
                raise ValueError('GitHub snapshot exceeds the watch output limit')
        if len(issues) < 100:
            return {'issues': records, 'complete': True}
    raise ValueError('Incomplete snapshot: five-page limit reached')


def make_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP('GitHub watch test reader')
    server.tool(annotations={'readOnlyHint': True, 'destructiveHint': False})(read_issue_comments)
    server.tool(annotations={'readOnlyHint': True, 'destructiveHint': False})(read_issues)
    server.tool(annotations={'readOnlyHint': True, 'destructiveHint': False})(read_bugs)
    return server


if __name__ == '__main__':
    make_server().run(transport='stdio')
