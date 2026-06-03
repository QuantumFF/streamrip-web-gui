"""Seam 1 + Seam 4: Redownload re-enqueues a History entry bypassing streamrip's
database (--no-db), via the HTTP API and the stubbed subprocess runner.

No test here launches a real `rip` process.
"""
import app as app_module
from conftest import fake_runner, wait_for, BlockingRunner

QOBUZ_URL = 'https://qobuz.com/album/123'
SKIP_LINE = "Skipping track 1: 'x'. Marked as downloaded in the database."


def _active_ids(client):
    data = client.get('/api/status').get_json()
    return {item['id']: item for item in data['active']}


def _history(client):
    return client.get('/api/status').get_json()['history']


def _drive_to_history(client, url=QOBUZ_URL, quality=3, lines=("─ Downloading track 1",), returncode=0):
    """Submit a normal Download and wait for it to land in History; return its id."""
    app_module.run_rip = fake_runner(list(lines), returncode=returncode)
    task_id = client.post(
        '/api/download', json={'url': url, 'quality': quality}
    ).get_json()['task_id']
    assert wait_for(lambda: any(h['id'] == task_id for h in _history(client)))
    return task_id


def test_redownload_creates_new_active_entry_distinct_from_original(client):
    # Hold workers so the redownloaded item is observable as a queued Active
    # entry that is distinct from the original History entry.
    original_id = _drive_to_history(client, lines=[SKIP_LINE], returncode=0)
    entry = next(h for h in _history(client) if h['id'] == original_id)
    assert entry['status'] == 'skipped'

    blocker = BlockingRunner()
    app_module.run_rip = blocker

    resp = client.post('/api/redownload', json={'id': original_id})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'queued'
    new_id = body['task_id']

    # A brand-new Download with a fresh id distinct from the History entry.
    assert new_id != original_id
    active = _active_ids(client)
    assert new_id in active
    # The original History entry is untouched.
    assert any(h['id'] == original_id for h in _history(client))

    blocker.release.set()


def test_redownload_reuses_original_url_quality_and_metadata(client):
    # Submit with a distinctive quality and let it finish, then redownload.
    app_module.run_rip = fake_runner(["─ Downloading track 1"], returncode=0)
    task_id = client.post(
        '/api/download', json={'url': QOBUZ_URL, 'quality': 4}
    ).get_json()['task_id']
    assert wait_for(lambda: any(h['id'] == task_id for h in _history(client)))

    blocker = BlockingRunner()
    app_module.run_rip = blocker

    new_id = client.post('/api/redownload', json={'id': task_id}).get_json()['task_id']

    active = _active_ids(client)
    assert active[new_id]['url'] == QOBUZ_URL
    assert active[new_id]['quality'] == 4

    blocker.release.set()


def test_redownload_command_includes_no_db_flag(client):
    # The original (normal) Download's command must NOT carry --no-db, while the
    # redownloaded one MUST. Capture both commands through the runner seam.
    captured = []

    def capturing_runner(cmd):
        captured.append(list(cmd))
        def gen():
            yield "─ Downloading track 1"
            return 0
        return gen()

    app_module.run_rip = capturing_runner

    task_id = client.post(
        '/api/download', json={'url': QOBUZ_URL, 'quality': 3}
    ).get_json()['task_id']
    assert wait_for(lambda: any(h['id'] == task_id for h in _history(client)))

    new_id = client.post('/api/redownload', json={'id': task_id}).get_json()['task_id']
    assert wait_for(lambda: any(h['id'] == new_id for h in _history(client)))

    assert len(captured) == 2
    normal_cmd, redownload_cmd = captured[0], captured[1]
    assert '--no-db' not in normal_cmd
    assert '--no-db' in redownload_cmd


def test_redownloaded_item_follows_normal_lifecycle_to_history(client):
    original_id = _drive_to_history(client, lines=[SKIP_LINE], returncode=0)

    app_module.run_rip = fake_runner(["─ Downloading track 1"], returncode=0)
    new_id = client.post('/api/redownload', json={'id': original_id}).get_json()['task_id']

    # It reaches a terminal state on its own and lands in History as a distinct
    # entry from the original.
    assert wait_for(lambda: any(h['id'] == new_id for h in _history(client)))
    new_entry = next(h for h in _history(client) if h['id'] == new_id)
    assert new_entry['status'] == 'completed'
    assert new_id not in _active_ids(client)


def test_redownload_unknown_id_is_rejected(client):
    resp = client.post('/api/redownload', json={'id': 'does_not_exist'})
    assert resp.status_code == 404
    assert _active_ids(client) == {}


def test_redownload_missing_id_is_rejected(client):
    resp = client.post('/api/redownload', json={})
    assert resp.status_code == 400
    assert _active_ids(client) == {}
