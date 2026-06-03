"""Seam 1 + Seam 4: HTTP API lifecycle, driven by a stubbed subprocess runner.

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


def test_submit_creates_queued_card_immediately(client):
    # Hold every worker so nothing can start; the submission must still appear.
    blocker = BlockingRunner()
    app_module.run_rip = blocker

    # Saturate all workers first.
    for _ in range(app_module.MAX_CONCURRENT_DOWNLOADS):
        client.post('/api/download', json={'url': QOBUZ_URL, 'quality': 3})
    assert blocker.wait_started(app_module.MAX_CONCURRENT_DOWNLOADS)

    # Now submit one more while all workers are busy.
    resp = client.post('/api/download', json={'url': QOBUZ_URL, 'quality': 3})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'queued'
    task_id = body['task_id']

    # It is visible immediately as queued, even though no worker is free.
    active = _active_ids(client)
    assert task_id in active
    assert active[task_id]['status'] == 'queued'

    blocker.release.set()


def test_no_submitted_download_is_invisible(client):
    blocker = BlockingRunner()
    app_module.run_rip = blocker

    # Saturate workers, then submit many more.
    submitted = []
    for _ in range(app_module.MAX_CONCURRENT_DOWNLOADS):
        r = client.post('/api/download', json={'url': QOBUZ_URL, 'quality': 3})
        submitted.append(r.get_json()['task_id'])
    assert blocker.wait_started(app_module.MAX_CONCURRENT_DOWNLOADS)

    extra = 8
    for _ in range(extra):
        r = client.post('/api/download', json={'url': QOBUZ_URL, 'quality': 3})
        submitted.append(r.get_json()['task_id'])

    active = _active_ids(client)
    # Every submission is visible: the saturating ones downloading, the rest queued.
    for task_id in submitted:
        assert task_id in active
    downloading = [a for a in active.values() if a['status'] == 'downloading']
    queued = [a for a in active.values() if a['status'] == 'queued']
    assert len(downloading) == app_module.MAX_CONCURRENT_DOWNLOADS
    assert len(queued) == extra

    blocker.release.set()


def test_stubbed_run_reaches_completed(client):
    app_module.run_rip = fake_runner(["─ Downloading track 1", "Done"], returncode=0)

    task_id = client.post('/api/download', json={'url': QOBUZ_URL}).get_json()['task_id']

    assert wait_for(lambda: any(h['id'] == task_id for h in _history(client)))
    entry = next(h for h in _history(client) if h['id'] == task_id)
    assert entry['status'] == 'completed'
    # Once terminal it has left the Active list.
    assert task_id not in _active_ids(client)


def test_stubbed_run_reaches_failed(client):
    app_module.run_rip = fake_runner(["boom"], returncode=1)

    task_id = client.post('/api/download', json={'url': QOBUZ_URL}).get_json()['task_id']

    assert wait_for(lambda: any(h['id'] == task_id for h in _history(client)))
    entry = next(h for h in _history(client) if h['id'] == task_id)
    assert entry['status'] == 'failed'


def test_stubbed_run_reaches_skipped(client):
    app_module.run_rip = fake_runner([SKIP_LINE, "Fetching cover art"], returncode=0)

    task_id = client.post('/api/download', json={'url': QOBUZ_URL}).get_json()['task_id']

    assert wait_for(lambda: any(h['id'] == task_id for h in _history(client)))
    entry = next(h for h in _history(client) if h['id'] == task_id)
    assert entry['status'] == 'skipped'


def test_history_is_populated_server_side_for_rehydration(client):
    app_module.run_rip = fake_runner(["─ Downloading track 1"], returncode=0)
    task_id = client.post('/api/download', json={'url': QOBUZ_URL}).get_json()['task_id']
    assert wait_for(lambda: any(h['id'] == task_id for h in _history(client)))

    # Rehydration payload carries everything the frontend needs.
    entry = next(h for h in _history(client) if h['id'] == task_id)
    assert set(['id', 'status', 'metadata', 'completed_at']).issubset(entry.keys())


def test_unsupported_url_is_rejected_and_not_queued(client):
    resp = client.post('/api/download', json={'url': 'https://example.com/x'})
    assert resp.status_code == 400
    assert _active_ids(client) == {}


def test_missing_url_is_rejected(client):
    resp = client.post('/api/download', json={})
    assert resp.status_code == 400
