"""Real disposable Git transports plus a fake GitHub; no external publication."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import time

import httpx
import pytest
from fastapi import FastAPI

from agent import task_publication as p, task_workspaces as tasks, continuations as c, stats_db as db
from agent import continuation_owners as owners
from agent.task_publication_api import router
from tests.test_task_workspaces import setup, git
from tests.test_watch_dispatch import setup as watch_setup

PLAN = dict(request_key='publish', steps=[
    dict(mode='task_review', prompt='Review without editing; report success only if ready'),
    dict(mode='task_publication', prompt='Publish the reviewed task'),
])


class Crash(BaseException):
    pass


class FakeGitHub:
    def __init__(self):
        self.prs = []
        self.creates = 0
        self.fail = None

    def find(self, scope, task):
        for pr in self.prs:
            pr['head']['sha'] = p.remote_head(task['worktree_path'], scope, task['branch'])
        return self.prs

    def create(self, scope, task, title):
        self.creates += 1
        if self.fail == 'before':
            raise RuntimeError('HTTP failed before creation')
        self.prs.append(dict(number=1, html_url='https://github.com/test/project/pull/1',
            state='open', merged_at=None, body=p.marker(task), base=dict(ref=scope['base_branch']),
            head=dict(ref=task['branch'], sha=p.remote_head(task['worktree_path'], scope, task['branch']))))
        if self.fail == 'after':
            raise RuntimeError('Lost successful PR response')
        if self.fail == 'crash_after':
            raise Crash()
        return self.prs[0]


@pytest.fixture
def publication(setup, tmp_path, monkeypatch):
    repo, task = setup
    remote = tmp_path / 'remote.git'
    git(repo, 'init', '--bare', str(remote))
    git(repo, 'remote', 'add', 'origin', str(remote))
    request_id = task['origin_request_id']
    parent = db.insert_assistant_message('test', 'codex', request_id, adhoc=True)
    tasks.bind(parent, task['id'])
    monkeypatch.setattr(c, '_enabled', True)
    monkeypatch.setattr(owners, '_service', None)
    body = dict(repository=str(repo), remote='origin', base_branch='main', push=True, pull_request=True)
    p.record_grant(task['id'], request_id=request_id, publication=body,
                   expires_at=time.time()+3600, audit='authenticated human')
    token = c.mint(parent)
    (Path(task['worktree_path']) / 'file.txt').write_text('Reviewed fix\n')
    c.create(token, PLAN)
    db.update_assistant_message(parent, 'Fix synced', None, 'done')
    db.insert_run_event(parent, 4, 'done', None)
    c.complete(parent, True, 'completed_and_synced')
    return repo, remote, task, parent, token, FakeGitHub()


def review(publication):
    _, _, task, parent, token, gh = publication
    step = c.claim(set())
    assert step['mode'] == 'task_review'
    user = db.insert_user_message('test','codex','Review',source='continuation')
    child = db.insert_assistant_message('test','codex',user,adhoc=True,source='continuation')
    tasks.bind(child, task['id'])
    assert c.link(step['id'], child)
    p.begin_review(step, child)
    return step, child


def finish_review(publication):
    step, child = review(publication)
    c.result(c.mint(child), {'success':True})
    db.update_assistant_message(child,'Review passed',None,'done')
    db.insert_run_event(child,4,'done',None)
    c.complete(child,True,'completed_and_synced')
    assert not tasks.get(publication[2]['id'])['review_required']
    return c.claim(set())


def pub_records():
    with db._connection() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM task_publications')]


def test_reviewed_task_publishes_exact_diff_and_preserves_dirty_human_checkout(publication):
    repo, remote, task, parent, token, gh = publication
    original = git(repo,'rev-parse','HEAD')
    (repo/'file.txt').write_text('Human dirty file\n')
    (repo/'private.txt').write_text('Human untracked file\n')
    step = finish_review(publication)
    p.execute(step, github=gh)
    result = p.get(step['id'])
    assert result['state'] == 'completed' and gh.creates == 1
    assert git(remote,'show',result['intended_sha']+':file.txt') == 'Reviewed fix'
    assert git(remote,'diff','--name-only',original,result['intended_sha']) == 'file.txt'
    assert git(repo,'rev-parse','HEAD') == original
    assert git(repo,'symbolic-ref','HEAD') == 'refs/heads/main'
    assert (repo/'file.txt').read_text() == 'Human dirty file\n'
    assert (repo/'private.txt').read_text() == 'Human untracked file\n'
    assert git(remote,'for-each-ref','--format=%(refname)') == 'refs/heads/'+task['branch']
    # Reconcile repeatedly and after DB initialization: no new push/PR/commit.
    db.init_db()
    for _ in range(2):
        assert p.advance(step['id'], github=gh)['state'] == 'completed'
    assert gh.creates == 1
    assert git(Path(task['worktree_path']),'rev-parse','HEAD') == result['intended_sha']


@pytest.mark.parametrize('failure', ['before','after','crash_after'])
def test_pr_failure_and_crash_never_duplicate_creation(publication, failure):
    gh = publication[-1]
    gh.fail = failure
    step = finish_review(publication)
    if failure == 'crash_after':
        with pytest.raises(Crash):
            p.execute(step, github=gh)
    else:
        p.execute(step, github=gh)
    record = p.get(step['id'])
    assert record['state'] == ('pr_unknown' if failure == 'before' else 'pr_intent' if failure == 'crash_after' else 'completed')
    gh.fail = None
    after = p.advance(step['id'], allow_effects=True, github=gh)
    assert after['state'] == 'completed'
    assert gh.creates == (2 if failure == 'before' else 1)
    assert git(publication[1],'rev-parse','refs/heads/'+publication[2]['branch']) == record['intended_sha']


@pytest.mark.parametrize('after_effect', [False, True])
def test_push_failure_records_commit_and_reconciles_only_confirmed_push(publication, monkeypatch, after_effect):
    original = p.git
    pushes = []
    def git_with_error(path, *args, **kwargs):
        if args[0] == 'push':
            pushes.append(args)
            if after_effect:
                original(path, *args, **kwargs)
            raise RuntimeError('Lost push response')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(p,'git',git_with_error)
    step = finish_review(publication)
    p.execute(step, github=publication[-1])
    record = p.get(step['id'])
    assert record['state'] == ('completed' if after_effect else 'push_unknown')
    assert git(Path(publication[2]['worktree_path']),'rev-parse','HEAD') == record['intended_sha']
    observed = p.advance(step['id'], github=publication[-1])
    assert observed['state'] == ('completed' if after_effect else 'committed')
    assert len(pushes) == 1
    monkeypatch.setattr(p, 'git', original)
    assert p.advance(step['id'], allow_effects=True, github=publication[-1])['state'] == 'completed'
    assert publication[-1].creates == 1
    assert all('--force' not in arg for arg in pushes[0])


def test_review_is_bound_to_exact_tree_and_cannot_approve_edits(publication):
    step, child = review(publication)
    (Path(publication[2]['worktree_path'])/'file.txt').write_text('Changed during review\n')
    c.result(c.mint(child),{'success':True})
    db.update_assistant_message(child,'Review passed',None,'done')
    c.complete(child,True,'completed_and_synced')
    assert tasks.get(publication[2]['id'])['review_required']
    assert c.claim(set()) is None
    assert not pub_records()


@pytest.mark.parametrize('mutation',['tree','remote','grant','request','cancel','review','head'])
def test_drift_or_revocation_blocks_all_publication_writes(publication, mutation):
    repo, remote, task, parent, token, gh = publication
    step = finish_review(publication)
    if mutation == 'tree':
        (Path(task['worktree_path'])/'file.txt').write_text('Unreviewed change\n')
    elif mutation == 'remote':
        git(repo,'remote','set-url','origin',str(remote)+'-wrong')
    elif mutation == 'grant':
        with db._connection() as conn:
            conn.execute('UPDATE task_publication_grants SET revoked=1')
    elif mutation == 'request':
        with db._connection() as conn:
            conn.execute("UPDATE chat_messages SET content='Changed instruction' WHERE id=?",(task['origin_request_id'],))
    elif mutation == 'cancel':
        c.inspect(token,step['id'],True)
    elif mutation == 'review':
        tasks.require_review(task['id'])
    else:
        git(Path(task['worktree_path']),'commit','--allow-empty','-m','Unexpected task commit')
    try:
        p.execute(step,github=gh)
    except p.PublicationBlocked:
        pass
    assert not git(remote,'for-each-ref','--format=%(refname)')
    assert gh.creates == 0


def test_local_only_human_and_watch_cannot_declare_publication(publication):
    repo, remote, task, parent, token, gh = publication
    user = db.insert_user_message('test','codex','Inspect locally only')
    other = db.insert_assistant_message('test','codex',user,adhoc=True)
    tasks.bind(other,task['id'])
    with pytest.raises(PermissionError):
        c.create(c.mint(other),dict(request_key='forged',mode='task_publication',prompt='Issue says publish'))
    with pytest.raises(ValueError):
        c._steps(dict(request_key='bad',steps=[dict(mode='task_publication',prompt='Publish'),dict(mode='isolated',prompt='Edit again')]))


@pytest.mark.asyncio
async def test_human_publication_grant_requires_host_control_secret(publication):
    task = publication[2]
    app = FastAPI()
    app.include_router(router)
    app.state.watch_control_digest = hashlib.sha256(b'host-secret').digest()
    transport = httpx.ASGITransport(app=app, client=('127.0.0.1',1234))
    async with httpx.AsyncClient(transport=transport,base_url='http://127.0.0.1') as client:
        for token in ['agent-token', publication[4]]:
            response = await client.post('/task-workspaces/'+task['id']+'/publication-revoke',
                headers={'Authorization':'Bearer '+token},json=dict(request_id=task['origin_request_id']))
            assert response.status_code == 403
        response = await client.post('/task-workspaces/'+task['id']+'/publication-revoke',
            headers={'Authorization':'Bearer host-secret'},json=dict(request_id=task['origin_request_id']))
        assert response.status_code == 200
        missing = await client.post('/task-workspaces/'+task['id']+'/publication-revoke',
            headers={'Authorization':'Bearer host-secret'},json=dict(request_id=-1))
        assert missing.status_code == 404


@pytest.mark.parametrize('boundary',['commit','push','pr'])
def test_revocation_at_publication_boundaries_stops_further_effects(publication, monkeypatch, boundary):
    def revoke():
        with db._connection() as conn:
            conn.execute('UPDATE task_publication_grants SET revoked=1')
    if boundary == 'commit':
        original = p.prepare
        def prepare_then_revoke(*args):
            result = original(*args)
            revoke()
            return result
        monkeypatch.setattr(p,'prepare',prepare_then_revoke)
    elif boundary == 'push':
        original = p.git
        def commit_then_revoke(path,*args,**kwargs):
            result = original(path,*args,**kwargs)
            if args[0] == 'read-tree':
                revoke()
            return result
        monkeypatch.setattr(p,'git',commit_then_revoke)
    else:
        original = publication[-1].find
        def find_then_revoke(*args):
            result = original(*args)
            revoke()
            return result
        publication[-1].find = find_then_revoke
    step = finish_review(publication)
    p.execute(step,github=publication[-1])
    result = p.get(step['id'])
    assert result['state'] != 'completed' and publication[-1].creates == 0
    if boundary != 'pr':
        assert not git(publication[1],'for-each-ref','--format=%(refname)')
    message = json.loads(db.get_message(result['child_id'])['content'])
    if boundary == 'commit':
        assert message['commit'] is None
    else:
        assert message['commit'] == result['intended_sha']


def test_unexpected_remote_head_blocks_pr_creation(publication, monkeypatch):
    original = p.remote_head
    reads = []
    def drift(path,scope,branch):
        result = original(path,scope,branch)
        reads.append(result)
        if len(reads) > 1:
            return 'f'*40
        return result
    monkeypatch.setattr(p,'remote_head',drift)
    step = finish_review(publication)
    p.execute(step,github=publication[-1])
    assert p.get(step['id'])['error'] == 'unexpected_remote_head'
    assert publication[-1].creates == 0
    assert not git(publication[1],'for-each-ref','--format=%(refname)')


def test_recover_only_confirms_existing_effects_without_replaying_writes(publication, monkeypatch):
    gh = publication[-1]
    gh.fail = 'crash_after'
    step = finish_review(publication)
    with pytest.raises(Crash):
        p.execute(step,github=gh)
    gh.fail = None
    monkeypatch.setattr(p,'GitHub',lambda:gh)
    db.init_db()
    p.recover()
    c.recover()
    assert p.get(step['id'])['state'] == 'completed'
    with db._connection() as conn:
        assert conn.execute('SELECT state FROM continuations WHERE id=?',(step['id'],)).fetchone()[0] == 'successful'
    assert gh.creates == 1


def test_recover_does_not_push_when_crash_precedes_push(publication, monkeypatch):
    original = p.git
    def crash(path,*args,**kwargs):
        if args[0] == 'push':
            raise Crash()
        return original(path,*args,**kwargs)
    step = finish_review(publication)
    monkeypatch.setattr(p,'git',crash)
    with pytest.raises(Crash):
        p.execute(step,github=publication[-1])
    monkeypatch.setattr(p,'git',original)
    monkeypatch.setattr(p,'GitHub',lambda:publication[-1])
    p.recover()
    assert p.get(step['id'])['state'] == 'committed'
    assert not git(publication[1],'for-each-ref','--format=%(refname)')
    assert publication[-1].creates == 0


@pytest.mark.parametrize('url,repo',[
    ('git@github.com:owner/project.git','owner/project'),
    ('https://github.com/owner/project.git','owner/project'),
])
def test_github_destination_is_explicit(url,repo):
    assert p.GitHub().repository(dict(push_url=url)) == repo


@pytest.mark.parametrize('edit_review',[False, True])
@pytest.mark.asyncio
async def test_scheduler_review_and_host_publication_use_saved_task(publication, monkeypatch, edit_review):
    from agent.topic_queue import TopicDispatcher
    from agent import watch_launch
    monkeypatch.setattr(watch_launch,'_api_url','http://127.0.0.1:1')
    monkeypatch.setattr(watch_launch,'_service',None)
    monkeypatch.setattr(p,'GitHub',lambda:publication[-1])
    seen = []
    async def runner(prompt,**kwargs):
        seen.append(kwargs['msg_id'])
        assert not Path(kwargs['cwd'],'.git').exists()
        if edit_review:
            Path(kwargs['cwd'],'file.txt').write_text('Unauthorized review edit\n')
        c.result(kwargs['backend_env']['SQUID_CONTINUE_TOKEN'],{'success':True})
        yield 'Review complete'
    monkeypatch.setattr('agent.runners.run_codex',runner)
    dispatcher = TopicDispatcher()
    scheduler = asyncio.create_task(c.maintain(dispatcher))
    try:
        async def settled():
            while True:
                with db._connection() as conn:
                    outcome = c.sequence_outcome(conn,publication[3])
                if outcome[0] != 'pending':
                    return outcome
                await asyncio.sleep(.05)
        result = await asyncio.wait_for(settled(),15)
        assert len(seen) == 1  # Publication is host-run, never an agent shell.
        assert result[0] == ('failed' if edit_review else 'succeeded')
        if edit_review:
            assert not pub_records()
            assert Path(publication[2]['worktree_path'],'file.txt').read_text() == 'Reviewed fix\n'
            assert any(r['status'] == 'promotion_failed' for r in db.get_worktrees('test',str(seen[0])))
        else:
            assert pub_records()[0]['state'] == 'completed'
            assert publication[-1].creates == 1
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler,return_exceptions=True)
        for worker in dispatcher._workers.values():
            worker.q.put_nowait(None)
        await asyncio.sleep(0)



@pytest.mark.parametrize('enabled',[False, True])
def test_watch_publication_requires_explicit_activation_and_settles_once(watch_setup, tmp_path, monkeypatch, enabled):
    from agent import watch_runs, server, worktree
    service = watch_setup[0]
    repo, remote = tmp_path/'watch-repo', tmp_path/'watch-remote.git'
    repo.mkdir()
    git(repo,'init','-b','main')
    git(repo,'config','user.name','Test')
    git(repo,'config','user.email','test@example.invalid')
    (repo/'file.txt').write_text('Base\n')
    git(repo,'add','.')
    git(repo,'commit','-m','Base')
    git(repo,'init','--bare',str(remote))
    git(repo,'remote','add','origin',str(remote))
    git(repo,'update-ref','refs/remotes/origin/main',git(repo,'rev-parse','HEAD'))
    db.upsert_agent('watch-agent','codex','openai',None,cwd=str(repo))
    memory = lambda topic:dict(code_roots=[str(repo)])
    monkeypatch.setattr('agent.memory.topic_memory_squid_config',memory)
    monkeypatch.setattr(server,'topic_memory_squid_config',memory)
    monkeypatch.setattr(server,'WORKTREE_ISOLATION_ENABLED',True)
    monkeypatch.setattr(tasks,'TASKS_HOME',tmp_path/'tasks')
    monkeypatch.setattr(worktree,'_WORKTREES_HOME',tmp_path/'turns')
    monkeypatch.setattr(c,'_enabled',True)
    monkeypatch.setattr(owners,'_service',service)
    with db._connection() as conn:
        conn.execute('DELETE FROM watch_execution_grants')
        conn.execute('UPDATE watch_events SET payload=?',(json.dumps(dict(issue=dict(number=1))),))
    watch_runs.execution_grant(service,topic='squid',watch_id=watch_setup[3]['id'],agent='watch-agent',
        instructions='Fix, review and publish a PR',max_runs=3,max_attempts=2,request_key='publish',user_action='authenticated human',
        publication=dict(repository=str(repo),remote='origin',base_branch='main',push=True,pull_request=True), enable_publication=enabled)
    run_id = watch_runs.claim_batch(service,lambda topic:False)
    prepared = asyncio.run(server._prepare_chat_turn(message='Fix issue',topic='squid',agent='watch-agent',source='watch',watch_run_id=run_id))
    parent = prepared['asst_msg_id']
    task = tasks.for_turn(parent)
    (Path(task['worktree_path'])/'file.txt').write_text('Reviewed watch fix\n')
    watch_runs.set_state(run_id,'queued')
    assert watch_runs.begin(service,run_id)
    token = c.mint(parent)
    if not enabled:
        with pytest.raises(PermissionError):
            c.create(token,PLAN)
        assert not pub_records()
        return
    c.create(token,PLAN)
    db.update_assistant_message(parent,'Fix complete',None,'done')
    db.insert_run_event(parent,4,'done',None)
    c.complete(parent,True,'completed_and_synced')
    from agent import admission_reads
    original_run = subprocess.run
    observed = []
    def check_claim_reads(command, *args, **kwargs):
        context = admission_reads._reads.get()
        if command[0] == 'git' and context is not None:
            assert not context[1], 'Watch Git validation ran under the claim write lock'
            observed.append(tuple(command))
        return original_run(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', check_claim_reads)
    review_step = c.claim(set())
    user = db.insert_user_message('squid','watch-agent','Review',source='continuation')
    child = db.insert_assistant_message('squid','watch-agent',user,adhoc=True,source='continuation')
    tasks.bind(child,task['id'])
    assert c.link(review_step['id'],child)
    p.begin_review(review_step,child)
    c.result(c.mint(child),{'success':True})
    db.update_assistant_message(child,'Review passed',None,'done')
    db.insert_run_event(child,4,'done',None)
    c.complete(child,True,'completed_and_synced')
    step = c.claim(set())
    assert observed
    p.execute(step,github=FakeGitHub())
    assert p.get(step['id'])['state'] == 'completed'
    watch_runs.settle(service,run_id)
    assert watch_runs.details(run_id)[0]['state'] == 'succeeded'
    with db._connection() as conn:
        assert conn.execute('SELECT disposition FROM watch_events').fetchone()[0] == 'handled'
    assert (repo/'file.txt').read_text() == 'Base\n'


def test_concurrent_reconciliation_serializes_the_same_task(publication):
    from concurrent.futures import ThreadPoolExecutor
    step = finish_review(publication)
    task = tasks.for_turn(step['parent_id'])
    user = db.insert_user_message('test','codex','Publish',source='continuation')
    child = db.insert_assistant_message('test','codex',user,adhoc=True,source='continuation')
    tasks.bind(child,task['id'])
    c.link(step['id'],child)
    p.prepare(step,child,task)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _:p.advance(step['id'],allow_effects=True,github=publication[-1]),range(2)))
    assert all(r['state'] == 'completed' for r in results)
    assert publication[-1].creates == 1


@pytest.mark.parametrize('pr_state', ['open', 'missing', 'closed', 'merged'])
def test_later_publication_updates_only_the_saved_pr(publication, pr_state):
    repo, remote, task, parent, token, gh = publication
    first = finish_review(publication)
    p.execute(first,github=gh)
    assert gh.creates == 1
    first_sha = p.get(first['id'])['intended_sha']
    if pr_state == 'missing':
        gh.prs.clear()
    else:
        gh.prs[0]['body'] += '\nHuman description retained'
        if pr_state in {'closed', 'merged'}:
            gh.prs[0]['state'] = 'closed'
        if pr_state == 'merged':
            gh.prs[0]['merged_at'] = '2026-11-07T00:00:00Z'
    request = db.insert_user_message('test','codex','Revise and update the same task PR')
    second_parent = db.insert_assistant_message('test','codex',request,adhoc=True)
    tasks.bind(second_parent,task['id'])
    p.record_grant(task['id'],request_id=request,
        publication=dict(repository=str(repo),remote='origin',base_branch='main',push=True,pull_request=True),
        expires_at=time.time()+3600,audit='authenticated second request')
    (Path(task['worktree_path'])/'file.txt').write_text('Reviewed second revision\n')
    tasks.require_review(task['id'])
    second_token = c.mint(second_parent)
    c.create(second_token,PLAN)
    db.update_assistant_message(second_parent,'Revision synced',None,'done')
    db.insert_run_event(second_parent,4,'done',None)
    c.complete(second_parent,True,'completed_and_synced')
    second_fixture = (repo,remote,task,second_parent,second_token,gh)
    second = finish_review(second_fixture)
    p.execute(second,github=gh)
    result = p.get(second['id'])
    assert result['state'] == ('completed' if pr_state == 'open' else 'committed')
    assert gh.creates == 1
    if pr_state == 'open':
        assert gh.prs[0]['body'].endswith('Human description retained')
    assert tasks.get(task['id'])['pr_number'] == 1
    if pr_state == 'open':
        assert git(remote,'show',result['intended_sha']+':file.txt') == 'Reviewed second revision'
    else:
        assert git(remote,'rev-parse','refs/heads/'+task['branch']) == first_sha
        assert tasks.get(task['id'])['published_sha'] == first_sha


def test_local_commit_partial_outcome_reports_actual_ref_after_index_failure(publication, monkeypatch):
    original = p.git
    def fail_index(path,*args,**kwargs):
        if args[0] == 'read-tree':
            raise RuntimeError('Index update failed')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(p,'git',fail_index)
    step = finish_review(publication)
    p.execute(step,github=publication[-1])
    record = p.get(step['id'])
    message = json.loads(db.get_message(record['child_id'])['content'])
    assert message['committed'] is True and message['commit'] == record['intended_sha']
    assert message['pushed'] is False
    monkeypatch.setattr(p,'git',original)
    result = p.advance(step['id'],allow_effects=True,github=publication[-1])
    assert result['state'] == 'completed' and result['intended_sha'] == record['intended_sha']


@pytest.mark.parametrize('authorized',[False,True])
@pytest.mark.asyncio
async def test_chat_records_publication_scope_before_starting_the_agent(publication, monkeypatch, authorized):
    from agent import server
    from starlette.requests import Request
    monkeypatch.setattr(server.app.state,'watch_control_digest',hashlib.sha256(b'host-secret').digest(),raising=False)
    async def noop():
        pass
    monkeypatch.setattr(server,'maybe_sync',noop)
    request = Request(dict(type='http',method='POST',scheme='http',path='/chat',query_string=b'',
        headers=[(b'authorization',b'Bearer host-secret' if authorized else b'Bearer agent-token')],
        client=('127.0.0.1',1234),server=('127.0.0.1',80),app=server.app))
    body = server.ChatRequest(message='Fix, separately review and publish this task',topic='test',agent='codex',task_id=publication[2]['id'],
        publication=dict(repository=str(publication[0]),remote='origin',base_branch='main',push=True,pull_request=True),
        publication_expires_at=time.time()+600)
    response = await server.chat(body,request)
    assert response.status_code == (200 if authorized else 403)
    if authorized:
        assert response.headers['X-Squid-Task-Id'] == publication[2]['id']
        parent = int(response.headers['X-Squid-Msg-Id'])
        with db._connection() as conn:
            assert conn.execute('SELECT 1 FROM task_publication_grants WHERE request_id=?',(db.get_message(parent)['reply_to'],)).fetchone()
        await response.body_iterator.aclose()  # Review preparation only; do not launch a model in this test.


@pytest.mark.asyncio
async def test_expired_owner_can_only_reconcile_confirmed_effects_with_fresh_human_authority(publication, monkeypatch):
    repo, remote, task, parent, token, gh = publication
    gh.fail = 'crash_after'
    step = finish_review(publication)
    with pytest.raises(Crash):
        p.execute(step,github=gh)
    with db._connection() as conn:
        conn.execute('UPDATE task_publication_grants SET expires=1')
    request = db.insert_user_message('test','codex','Reconcile the already published task')
    new_parent = db.insert_assistant_message('test','codex',request,adhoc=True)
    tasks.bind(new_parent,task['id'])
    p.record_grant(task['id'],request_id=request,
        publication=dict(repository=str(repo),remote='origin',base_branch='main',push=True,pull_request=True),
        expires_at=time.time()+600,audit='fresh authenticated authority')
    monkeypatch.setattr(p,'GitHub',lambda:gh)
    original = p.git
    def readonly(path,*args,**kwargs):
        assert args[0] not in {'push','update-ref','read-tree','commit-tree'}
        return original(path,*args,**kwargs)
    monkeypatch.setattr(p,'git',readonly)
    app = FastAPI()
    app.include_router(router)
    app.state.watch_control_digest = hashlib.sha256(b'host-secret').digest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',1)),base_url='http://127.0.0.1') as client:
        headers = {'Authorization':'Bearer host-secret'}
        denied = await client.post('/task-publications/'+step['id']+'/reconcile',headers=headers,json={})
        assert denied.status_code == 409
        confirmed = await client.post('/task-publications/'+step['id']+'/reconcile',headers=headers,json=dict(authority_parent_id=new_parent))
        assert confirmed.status_code == 200 and confirmed.json()['state'] == 'completed'
    assert gh.creates == 1


def test_publication_preserves_unreviewed_staged_artifacts(publication):
    task = publication[2]
    path = Path(task['worktree_path'])
    (path/'file.txt').write_text('Unreviewed staged variant\n')
    git(path,'add','file.txt')
    (path/'file.txt').write_text('Reviewed fix\n')
    staged = git(path,'write-tree')
    step = finish_review(publication)
    p.execute(step,github=publication[-1])
    assert not pub_records()
    assert git(path,'write-tree') == staged
    assert not git(publication[1],'for-each-ref','--format=%(refname)')


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_count', [1, 2])
async def test_cancelled_host_step_joins_inflight_transport_and_blocks_next_effect(publication, monkeypatch, cancel_count):
    import threading
    entered, release = threading.Event(), threading.Event()
    original = p.git
    def slow_push(path,*args,**kwargs):
        if args[0] == 'push':
            entered.set()
            assert release.wait(5)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(p,'git',slow_push)
    monkeypatch.setattr(p,'GitHub',lambda:publication[-1])
    step = finish_review(publication)
    operation = asyncio.create_task(c.dispatch(step,None))
    try:
        assert await asyncio.to_thread(entered.wait,5)
        operation.cancel()
        async def cancelled():
            while True:
                with db._connection() as conn:
                    if conn.execute('SELECT cancel_requested FROM continuations WHERE id=?',(step['id'],)).fetchone()[0]:
                        return
                await asyncio.sleep(.01)
        await asyncio.wait_for(cancelled(),2)
        for _ in range(cancel_count - 1):
            operation.cancel()
            await asyncio.sleep(.01)
        assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert publication[-1].creates == 0
        with db._connection() as conn:
            assert conn.execute('SELECT state FROM continuations WHERE id=?',(step['id'],)).fetchone()[0] == 'ambiguous'
    finally:
        release.set()
        await asyncio.gather(operation,return_exceptions=True)


@pytest.mark.parametrize('state', ['pr_unknown', 'pr_intent'])
def test_readonly_reconciliation_makes_missing_pr_retryable(publication, state):
    gh = publication[-1]
    gh.fail = 'before'
    step = finish_review(publication)
    p.execute(step, github=gh)
    p.save(step['id'], state)
    assert p.advance(step['id'], github=gh)['state'] == 'pushed'
    assert gh.creates == 1
    gh.fail = None
    assert p.advance(step['id'], allow_effects=True, github=gh)['state'] == 'completed'
    assert p.advance(step['id'], allow_effects=True, github=gh)['state'] == 'completed'
    assert gh.creates == 2


def test_recovery_logs_failed_publication(publication, monkeypatch, caplog):
    gh = publication[-1]
    gh.fail = 'crash_after'
    step = finish_review(publication)
    with pytest.raises(Crash):
        p.execute(step, github=gh)
    def unavailable(*args, **kwargs):
        raise RuntimeError('network unavailable')
    monkeypatch.setattr(p, 'advance', unavailable)
    p.recover()
    assert step['id'] in caplog.text
    assert 'network unavailable' in caplog.text


@pytest.mark.asyncio
async def test_authority_reconciliation_runs_off_event_loop(publication, monkeypatch):
    import threading
    loop_thread = threading.get_ident()
    def get(identifier):
        assert threading.get_ident() != loop_thread
        raise p.PublicationBlocked('test blocked')
    monkeypatch.setattr(p, 'get', get)
    app = FastAPI()
    app.include_router(router)
    app.state.watch_control_digest = hashlib.sha256(b'host-secret').digest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response = await client.post('/task-publications/test/reconcile',
            headers={'Authorization':'Bearer host-secret'},json=dict(authority_parent_id=publication[3]))
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_failed_reconcile_does_not_replace_authority(publication, monkeypatch):
    from agent.task_publication_api import _reconcile
    gh = publication[-1]
    gh.fail = 'before'
    step = finish_review(publication)
    p.execute(step, github=gh)
    def blocked(*args, **kwargs):
        raise p.PublicationBlocked('network unavailable')
    monkeypatch.setattr(p, '_advance_locked', blocked)
    with pytest.raises(p.PublicationBlocked):
        await asyncio.to_thread(_reconcile, step['id'], publication[3], False)
    with db._connection() as conn:
        assert not conn.execute('SELECT 1 FROM task_publication_authorities').fetchone()


@pytest.mark.asyncio
async def test_startup_serves_health_while_publication_recovery_waits(setup, monkeypatch):
    import threading
    from unittest.mock import AsyncMock
    from agent import server, context, watch_auth, watch_dispatch, flow
    started, release = threading.Event(), threading.Event()
    def recover():
        started.set()
        assert release.wait(10), 'test did not release recovery'
    monkeypatch.setattr(p, 'recover', recover)
    monkeypatch.setattr(context, 'runtime_templates', lambda: None)
    monkeypatch.setattr(watch_auth, 'load_digest', lambda *a, **kw: None)
    monkeypatch.setattr(server, 'prune_realtime_data', lambda: None)
    monkeypatch.setattr(server, '_sync_pi_agents_models_store', lambda: None)
    monkeypatch.setattr(server, '_recover_orphaned_pending_on_startup', AsyncMock())
    monkeypatch.setattr(server, '_resume_stalled_flows_on_startup', AsyncMock())
    monkeypatch.setattr(watch_dispatch.WatchDispatch, 'recover', AsyncMock())
    monkeypatch.setattr(c, 'recover', lambda: None)
    monkeypatch.setattr(c, 'maintain', AsyncMock())
    monkeypatch.setattr(flow, 'maintain_durable_flows', AsyncMock())
    monkeypatch.setattr(server, 'shore_enabled', lambda: False)
    monkeypatch.setattr(server, '_publish_process_changed', lambda *a: None)
    monkeypatch.setattr(server, '_publish_queue_changed', lambda *a: None)
    monkeypatch.setattr(server, '_cfg', {'server': {'host': '127.0.0.1', 'port': 8000}})
    try:
        async with server._server_lifespan(server.app):
            assert await asyncio.to_thread(started.wait, 2)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as client:
                response = await asyncio.wait_for(client.get('/health'), 2)
            assert response.status_code == 200
            assert not release.is_set()
            release.set()
    finally:
        release.set()
        c.configure(False)


def test_concurrent_reconcile_does_not_duplicate_pr(publication):
    from concurrent.futures import ThreadPoolExecutor
    from agent.task_publication_api import _reconcile
    from unittest.mock import patch
    gh = publication[-1]
    gh.fail = 'before'
    step = finish_review(publication)
    p.execute(step, github=gh)
    gh.fail = None
    with patch.object(p, 'GitHub', return_value=gh), ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_reconcile, step['id'], None, True) for _ in range(2)]
        assert all(f.result(timeout=20)['state'] == 'completed' for f in futures)
    assert gh.creates == 2  # One failed attempt, then one successful creation.



def test_readonly_recovery_preserves_pending_index_repair(publication, monkeypatch):
    original = p.git
    def fail_index(path, *args, **kwargs):
        if args[0] == 'read-tree':
            raise RuntimeError('Index update failed')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(p, 'git', fail_index)
    step = finish_review(publication)
    p.execute(step, github=publication[-1])
    record = p.get(step['id'])
    assert record['state'] == 'prepared'
    monkeypatch.setattr(p, 'git', original)
    observed = p.advance(step['id'], github=publication[-1])
    assert observed['state'] == 'prepared'
    assert git(Path(publication[2]['worktree_path']), 'write-tree') != record['tree']
    assert not git(publication[1], 'for-each-ref', '--format=%(refname)')
    result = p.advance(step['id'], allow_effects=True, github=publication[-1])
    assert result['state'] == 'completed'
    assert git(Path(publication[2]['worktree_path']), 'write-tree') == record['tree']



def test_unsupported_pr_host_blocks_before_push(publication):
    step = finish_review(publication)
    # The real adapter rejects a local/non-GitHub URL before a transport write.
    p.execute(step)
    record = p.get(step['id'])
    assert record['state'] == 'committed'
    assert record['error'] == 'unsupported_github_remote'
    assert not git(publication[1], 'for-each-ref', '--format=%(refname)')



@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'git_failure', 'missing_executable'])
async def test_reconcile_transport_failure_preserves_intent_and_can_be_retried(publication, monkeypatch, failure):
    gh = publication[-1]
    gh.fail = 'before'
    step = finish_review(publication)
    p.execute(step, github=gh)
    before = p.get(step['id'])
    assert before['state'] == 'pr_unknown'
    original = p.remote_head
    def unavailable(*args, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(['git', 'ls-remote'], 45)
        if failure == 'git_failure':
            raise subprocess.CalledProcessError(128, ['git', 'ls-remote'], stderr='connection failed')
        raise FileNotFoundError('git unavailable')
    monkeypatch.setattr(p, 'remote_head', unavailable)
    monkeypatch.setattr(p, 'GitHub', lambda: gh)
    app = FastAPI()
    app.include_router(router)
    app.state.watch_control_digest = hashlib.sha256(b'host-secret').digest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        headers = {'Authorization': 'Bearer host-secret'}
        failed = await client.post('/task-publications/'+step['id']+'/reconcile', headers=headers, json={'resume': True})
        assert failed.status_code == 503
        assert failed.json() == {'error': 'publication_transport_unavailable'}
        assert p.get(step['id']) == before
        monkeypatch.setattr(p, 'remote_head', original)
        gh.fail = None
        resumed = await client.post('/task-publications/'+step['id']+'/reconcile', headers=headers, json={'resume': True})
        assert resumed.status_code == 200
        assert resumed.json()['state'] == 'completed'
    assert gh.creates == 2  # Failed original creation, then one authorized retry.



def test_finalization_preserves_reconciled_success_and_terminal_event(publication):
    gh = publication[-1]
    gh.fail = 'before'
    step = finish_review(publication)
    p.execute(step, github=gh)
    partial = p.get(step['id'])
    assert partial['state'] == 'pr_unknown'
    child = partial['child_id']
    gh.fail = None
    completed = p.advance(step['id'], allow_effects=True, github=gh)
    assert completed['state'] == 'completed'
    p.finish(completed)
    with db._connection() as conn:
        events = [tuple(r) for r in conn.execute(
            'SELECT seq,event_type,payload FROM run_events WHERE msg_id=? ORDER BY seq', (child,))]
    assert [r[1] for r in events] == ['error', 'done']
    # Simulate the original executor arriving after a concurrent reconcile.
    p.finish(partial)
    p.finish(completed)
    assert db.get_message(child)['status'] == 'done'
    assert json.loads(db.get_message(child)['content'])['publication'] == 'completed'
    with db._connection() as conn:
        assert conn.execute('SELECT state FROM continuations WHERE id=?', (step['id'],)).fetchone()[0] == 'successful'
        assert [tuple(r) for r in conn.execute(
            'SELECT seq,event_type,payload FROM run_events WHERE msg_id=? ORDER BY seq', (child,))] == events



def test_execution_error_cannot_overwrite_concurrent_reconciliation(publication, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    entered, release, reconciled = threading.Event(), threading.Event(), threading.Event()
    local = threading.local()
    original_advance, original_get = p._advance_locked, p.get
    failed = False
    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            local.failed = True
            raise RuntimeError('transport failed before first effect')
        return original_advance(*args, **kwargs)
    def pause_failure_record(identifier):
        record = original_get(identifier)
        if getattr(local, 'failed', False):
            local.failed = False
            entered.set()
            assert release.wait(15), 'failure handler was not released'
        return record
    monkeypatch.setattr(p, '_advance_locked', fail_once)
    monkeypatch.setattr(p, 'get', pause_failure_record)
    gh = publication[-1]
    step = finish_review(publication)
    def reconcile():
        record = p.advance(step['id'], allow_effects=True, github=gh)
        p.finish(record)
        reconciled.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        execution = pool.submit(p.execute, step, github=gh)
        try:
            assert entered.wait(10)
            recovery = pool.submit(reconcile)
            # Give reconciliation time to expose a stale failure write if the
            # executor released its lock before recording the failed outcome.
            reconciled.wait(5)
        finally:
            release.set()
        execution.result(timeout=20)
        recovery.result(timeout=20)
    record = p.get(step['id'])
    assert record['state'] == 'completed' and record['error'] is None
    assert db.get_message(record['child_id'])['status'] == 'done'
    assert json.loads(db.get_message(record['child_id'])['content'])['publication'] == 'completed'
    assert gh.creates == 1


@pytest.mark.parametrize('after_effect', [False, True])
def test_fresh_process_crash_at_push_recovers_without_replaying_writes(publication, after_effect):
    import sys

    repo, remote, task, _, _, _ = publication
    step = finish_review(publication)
    source = Path(__file__).resolve().parents[1]
    common = '''
import json
import os
from pathlib import Path
import sys
from agent import continuations as c, stats_db as db, task_publication as p
from agent import memory

db._DB_PATH = Path(sys.argv[1])
repo, identifier = sys.argv[2:4]
memory.topic_memory_squid_config = lambda topic: {'code_roots': [repo]}
c.configure(True)
pr_creates = []
class ReadOnlyGitHub:
    def find(self, scope, task):
        return []
    def create(self, *args):
        pr_creates.append(True)
        raise AssertionError('Recovery must never create a PR')
'''
    crashed = subprocess.run([sys.executable, '-c', common + '''
original = p.git
def crash_at_push(path, *args, **kwargs):
    if args[0] == 'push':
        if sys.argv[4] == 'after':
            original(path, *args, **kwargs)
        os._exit(73)
    return original(path, *args, **kwargs)
p.git = crash_at_push
with db._connection() as conn:
    step = dict(conn.execute('SELECT * FROM continuations WHERE id=?', (identifier,)).fetchone())
p.execute(step, github=ReadOnlyGitHub())
raise AssertionError('Publisher did not reach the crash boundary')
''', str(db._DB_PATH), str(repo), step['id'], 'after' if after_effect else 'before'],
        cwd=source, capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 73, crashed.stderr
    record = p.get(step['id'])
    assert record['state'] == 'push_intent'
    assert git(Path(task['worktree_path']), 'rev-parse', 'HEAD') == record['intended_sha']
    refs = git(remote, 'for-each-ref', '--format=%(refname) %(objectname)')
    assert ('refs/heads/' + task['branch'] in refs) == after_effect
    snapshots = []
    for _ in range(2):
        recovered = subprocess.run([sys.executable, '-c', common + '''
original = p.git
writes = []
def no_writes(path, *args, **kwargs):
    if args[0] in {'push', 'update-ref', 'read-tree', 'commit-tree'}:
        writes.append(args[0])
        raise AssertionError('Recovery attempted a Git write: ' + args[0])
    return original(path, *args, **kwargs)
p.git = no_writes
c.recover()
p.GitHub = ReadOnlyGitHub
p.recover()
p.recover()
with db._connection() as conn:
    continuation = dict(conn.execute('SELECT * FROM continuations WHERE id=?', (identifier,)).fetchone())
    children = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE source='continuation'").fetchone()[0]
print(json.dumps(dict(publication=p.get(identifier), continuation=continuation,
    children=children, writes=writes, pr_creates=pr_creates)))
''', str(db._DB_PATH), str(repo), step['id']], cwd=source,
            capture_output=True, text=True, timeout=30)
        assert recovered.returncode == 0, recovered.stderr
        snapshots.append(json.loads(recovered.stdout))
    assert snapshots[0] == snapshots[1]
    assert snapshots[0]['publication']['state'] == ('pushed' if after_effect else 'committed')
    assert snapshots[0]['continuation']['state'] == 'ambiguous'
    assert snapshots[0]['writes'] == snapshots[0]['pr_creates'] == []
    # Reviewed child user/assistant plus publication child user/assistant.
    assert snapshots[0]['children'] == 4
    assert git(remote, 'for-each-ref', '--format=%(refname) %(objectname)') == refs
    child = db.get_message(record['child_id'])
    outcome = json.loads(child['content'])
    assert outcome['committed'] is True
    assert outcome['pushed'] is after_effect


def test_publication_uses_create_response_before_pr_is_listed(publication, monkeypatch):
    gh = publication[-1]
    monkeypatch.setattr(gh, 'find', lambda *args: [])
    monkeypatch.setattr(p.time, 'sleep', lambda _: None)
    step = finish_review(publication)
    p.execute(step, github=gh)
    assert p.get(step['id'])['state'] == 'completed'
    assert gh.creates == 1


@pytest.mark.parametrize('lag', ['missing', 'stale', 'wrong_branch', 'wrong_sha', 'persistent_stale'])
def test_published_pr_lookup_retries_only_expected_propagation_lag(monkeypatch, lag):
    import copy
    task = dict(id='task', base_sha='base', branch='task-branch', pr_number=1)
    scope = dict(base_branch='main')
    record = dict(remote_before='old', intended_sha='new')
    pr = dict(number=1, html_url='https://github.com/test/project/pull/1', state='open',
              merged_at=None, body=p.marker(task), base=dict(ref='main'),
              head=dict(ref='task-branch', sha='new'))
    calls = []
    def find(*args):
        calls.append(True)
        value = copy.deepcopy(pr)
        if lag == 'missing' and len(calls) < 3:
            return []
        if lag == 'persistent_stale' or (lag == 'stale' and len(calls) < 3):
            value['head']['sha'] = 'old'
        if lag == 'wrong_branch':
            value['head']['ref'] = 'unrelated'
        if lag == 'wrong_sha':
            value['head']['sha'] = 'unrelated'
        return [value]
    gh = FakeGitHub()
    monkeypatch.setattr(gh, 'find', find)
    monkeypatch.setattr(p.time, 'sleep', lambda _: None)
    if lag in {'wrong_branch', 'wrong_sha'}:
        with pytest.raises(p.PublicationBlocked, match='unexpected_pr_identity'):
            p.find_published_pr(gh, scope, task, record)
        assert len(calls) == 1
    else:
        result = p.find_published_pr(gh, scope, task, record)
        assert len(calls) == (4 if lag == 'persistent_stale' else 3)
        if lag == 'persistent_stale':
            with pytest.raises(p.PublicationBlocked, match='unexpected_pr_identity'):
                p.validate_pr(result[0], scope, task, 'new')
        else:
            assert result == [pr]


def test_task_bound_backing_git_is_rejected_before_acceptance(publication):
    request = db.insert_user_message('test', 'codex', 'Commit the task changes')
    parent = db.insert_assistant_message('test', 'codex', request)
    tasks.bind(parent, publication[2]['id'])
    token = c.mint(parent)
    with pytest.raises(PermissionError):
        c.create(token, dict(request_key='commit', mode='backing_git', prompt='Commit the changes'))
    with db._connection() as conn:
        assert not conn.execute('SELECT 1 FROM continuations WHERE parent_id=?', (parent,)).fetchone()


@pytest.mark.asyncio
async def test_partial_reconcile_refreshes_saved_message_without_repeating_effects(publication, monkeypatch):
    gh = publication[-1]
    gh.fail = 'before'
    step = finish_review(publication)
    p.execute(step, github=gh)
    record = p.get(step['id'])
    assert record['state'] == 'pr_unknown'
    monkeypatch.setattr(p, 'GitHub', lambda: gh)
    app = FastAPI()
    app.include_router(router)
    app.state.watch_control_digest = hashlib.sha256(b'host-secret').digest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        for _ in range(2):
            response = await client.post('/task-publications/'+step['id']+'/reconcile',
                headers={'Authorization': 'Bearer host-secret'}, json={})
            assert response.status_code == 200
            assert response.json()['state'] == 'pushed'
            content = json.loads(db.get_message(record['child_id'])['content'])
            assert content['publication'] == 'pushed' and content['pushed'] is True and content['error'] is None
    assert gh.creates == 1


@pytest.mark.parametrize('change', ['unrelated_write', 'revoke', 'cancel', 'expire'])
def test_publication_claim_git_does_not_hold_write_lock_and_rechecks_authority(publication, monkeypatch, change):
    import sqlite3
    from agent import admission_reads
    step = finish_review(publication)
    # finish_review claims publication; put this unstarted step back at admission.
    with db._connection() as conn:
        conn.execute("UPDATE continuations SET state='eligible' WHERE id=?", (step['id'],))
    original = subprocess.run
    reads = []
    def checked_run(command, *args, **kwargs):
        if command[0] == 'git':
            context = admission_reads._reads.get()
            assert context is not None and not context[1], 'Git ran under the claim write lock'
            reads.append(tuple(command))
            if len(reads) == 1:
                # A separate connection must acquire a write lock while the
                # first identity read is in progress. No timeout/retry masking.
                with sqlite3.connect(str(db._DB_PATH), timeout=.05) as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    if change == 'revoke':
                        writer.execute('UPDATE task_publication_grants SET revoked=1')
                    elif change == 'expire':
                        writer.execute('UPDATE task_publication_grants SET expires=0')
                    elif change == 'cancel':
                        writer.execute('UPDATE continuations SET cancel_requested=1 WHERE id=?', (step['id'],))
                    else:
                        writer.execute("UPDATE agents SET model=model WHERE name='codex'")
        return original(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', checked_run)
    claimed = c.claim(set())
    assert reads
    with db._connection() as conn:
        row = dict(conn.execute('SELECT * FROM continuations WHERE id=?', (step['id'],)).fetchone())
    assert bool(claimed) == (change == 'unrelated_write')
    assert row['state'] == ('claimed' if claimed else 'blocked')
    assert not pub_records()


def test_changed_publication_identity_read_retries_outside_lock(publication, monkeypatch):
    from contextlib import contextmanager
    from agent import admission_reads
    step = finish_review(publication)
    with db._connection() as conn:
        conn.execute("UPDATE continuations SET state='eligible' WHERE id=?", (step['id'],))
    capture = admission_reads.capture
    @contextmanager
    def change_after_capture():
        with capture() as observations:
            yield observations
        # Change a host-owned scope after preflight, before the atomic claim.
        # The new remote requires identity reads absent from the observations.
        with db._connection() as conn:
            scope = json.loads(conn.execute('SELECT scope_json FROM task_publication_grants').fetchone()[0])
            scope['remote'] = 'missing-new-remote'
            conn.execute('UPDATE task_publication_grants SET scope_json=?', (json.dumps(scope),))
    monkeypatch.setattr(admission_reads, 'capture', change_after_capture)
    assert c.claim(set()) is None
    with db._connection() as conn:
        assert conn.execute('SELECT state FROM continuations WHERE id=?', (step['id'],)).fetchone()[0] == 'eligible'
    # Fresh preflight observes the missing remote and blocks normally. A failed
    # Git read is replayed as failure, rather than being retried under the lock.
    monkeypatch.setattr(admission_reads, 'capture', capture)
    assert c.claim(set()) is None
    with db._connection() as conn:
        assert conn.execute('SELECT state FROM continuations WHERE id=?', (step['id'],)).fetchone()[0] == 'blocked'


@pytest.mark.parametrize('after_effect', [False, True])
def test_fresh_process_crash_at_pr_creation_recovers_without_duplicate_effects(publication, tmp_path, after_effect):
    import sys
    repo, remote, task, _, _, _ = publication
    step = finish_review(publication)
    source = Path(__file__).resolve().parents[1]
    pr_file = tmp_path / 'external-pr.json'
    common = '''
import json,os,sys
from pathlib import Path
from agent import continuations as c,stats_db as db,task_publication as p
from agent import memory
db._DB_PATH=Path(sys.argv[1])
db.init_db()
repo,identifier,pr_file=sys.argv[2],sys.argv[3],Path(sys.argv[4])
memory.topic_memory_squid_config=lambda _: {'code_roots':[repo]}
c.configure(True)
creates=[]
class GitHub:
    def find(self,scope,task):
        return json.loads(pr_file.read_text()) if pr_file.exists() else []
    def create(self,scope,task,title):
        creates.append(True)
        if len(sys.argv)>5:
            if sys.argv[5]=='after':
                pr=dict(number=1,html_url='https://github.com/test/project/pull/1',state='open',merged_at=None,
                    body=p.marker(task),base=dict(ref=scope['base_branch']),
                    head=dict(ref=task['branch'],sha=p.remote_head(task['worktree_path'],scope,task['branch'])))
                pr_file.write_text(json.dumps([pr]))
            os._exit(73)
        raise AssertionError('Recovery attempted PR creation')
'''
    args = [str(db._DB_PATH), str(repo), step['id'], str(pr_file)]
    crashed = subprocess.run([sys.executable, '-c', common + '''
with db._connection() as conn:
    step=dict(conn.execute('SELECT * FROM continuations WHERE id=?',(identifier,)).fetchone())
p.execute(step,github=GitHub())
raise AssertionError('Publisher did not reach PR creation')
''', *args, 'after' if after_effect else 'before'], cwd=source, capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 73, crashed.stderr
    record = p.get(step['id'])
    assert record['state'] == 'pr_intent'
    assert pr_file.exists() == after_effect
    refs = git(remote, 'for-each-ref', '--format=%(refname) %(objectname)')
    assert record['intended_sha'] in refs
    snapshots = []
    for _ in range(2):
        recovered = subprocess.run([sys.executable, '-c', common + '''
original=p.git
writes=[]
def read_only(path,*args,**kwargs):
    if args[0] in {'push','update-ref','read-tree','commit-tree'}:
        writes.append(args[0])
        raise AssertionError('Recovery attempted a Git write')
    return original(path,*args,**kwargs)
p.git=read_only
p.GitHub=GitHub
c.recover()
p.recover()
p.recover()
with db._connection() as conn:
    continuation=dict(conn.execute('SELECT * FROM continuations WHERE id=?',(identifier,)).fetchone())
    messages=[dict(row) for row in conn.execute('SELECT id,role,reply_to FROM chat_messages ORDER BY id')]
print(json.dumps(dict(publication=p.get(identifier),continuation=continuation,messages=messages,writes=writes,creates=creates)))
''', *args], cwd=source, capture_output=True, text=True, timeout=30)
        assert recovered.returncode == 0, recovered.stderr
        snapshots.append(json.loads(recovered.stdout))
    assert snapshots[0] == snapshots[1]
    assert snapshots[0]['publication']['state'] == ('completed' if after_effect else 'pushed')
    assert snapshots[0]['continuation']['state'] == ('successful' if after_effect else 'ambiguous')
    assert json.loads(snapshots[0]['continuation']['remaining_json']) == []
    assert snapshots[0]['writes'] == snapshots[0]['creates'] == []
    assert git(remote, 'for-each-ref', '--format=%(refname) %(objectname)') == refs
    assert pr_file.exists() == after_effect
