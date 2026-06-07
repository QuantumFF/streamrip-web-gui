"""Unit tests for DownloadStore: the in-memory Active/History state owner.

These exercise the store's lifecycle transitions directly, with no threads,
HTTP, or queue — just the small interface (add_queued / mark_downloading /
finalize / snapshot / find_history).
"""
import app as app_module


def make_store(max_history=app_module.MAX_HISTORY):
    return app_module.DownloadStore(max_history=max_history)


def test_next_id_is_unique():
    store = make_store()
    ids = {store.next_id() for _ in range(100)}
    assert len(ids) == 100


def test_add_queued_appears_in_snapshot_as_queued():
    store = make_store()
    store.add_queued({"id": "a", "url": "u", "quality": 3, "status": "queued"})

    snap = store.snapshot()
    active = {r["id"]: r for r in snap["active"]}
    assert active["a"]["status"] == "queued"
    assert snap["history"] == []


def test_mark_downloading_transitions_record_in_place():
    store = make_store()
    store.add_queued({"id": "a", "url": "u", "quality": 3, "status": "queued"})
    store.mark_downloading("a", started=123.0)

    active = {r["id"]: r for r in store.snapshot()["active"]}
    assert active["a"]["status"] == "downloading"
    assert active["a"]["started"] == 123.0


def test_mark_downloading_unknown_id_is_a_noop():
    store = make_store()
    store.mark_downloading("missing", started=1.0)
    assert store.snapshot()["active"] == []


def test_add_mark_finalize_moves_active_to_history():
    store = make_store()
    store.add_queued({"id": "a", "url": "u", "quality": 4, "status": "queued"})
    store.mark_downloading("a", started=1.0)

    entry = store.finalize(
        {"id": "a", "status": "completed", "output": "ok", "completed_at": 2.0}
    )

    # Left Active, landed in History.
    assert store.snapshot()["active"] == []
    history = store.snapshot()["history"]
    assert [h["id"] for h in history] == ["a"]
    # url/quality folded in from the popped Active record (Redownload needs them).
    assert entry["url"] == "u"
    assert entry["quality"] == 4
    assert entry["status"] == "completed"


def test_finalize_keeps_caller_supplied_metadata_over_record():
    store = make_store()
    store.add_queued(
        {"id": "a", "url": "u", "quality": 3, "metadata": {"x": 1}, "status": "queued"}
    )
    entry = store.finalize(
        {"id": "a", "status": "completed", "metadata": {"x": 2}, "completed_at": 1.0}
    )
    assert entry["metadata"] == {"x": 2}


def test_finalize_falls_back_to_record_metadata():
    store = make_store()
    store.add_queued(
        {"id": "a", "url": "u", "quality": 3, "metadata": {"x": 1}, "status": "queued"}
    )
    entry = store.finalize({"id": "a", "status": "completed", "completed_at": 1.0})
    assert entry["metadata"] == {"x": 1}


def test_finalize_without_active_record_yields_null_url_quality():
    # A Download that was never in Active (defensive) still produces a valid entry.
    store = make_store()
    entry = store.finalize({"id": "gone", "status": "failed", "completed_at": 1.0})
    assert entry["url"] is None
    assert entry["quality"] is None
    assert entry["metadata"] == {}


def test_finalize_prepends_newest_first():
    store = make_store()
    for tid in ("a", "b", "c"):
        store.add_queued({"id": tid, "url": "u", "quality": 3, "status": "queued"})
        store.finalize({"id": tid, "status": "completed", "completed_at": 1.0})
    assert [h["id"] for h in store.snapshot()["history"]] == ["c", "b", "a"]


def test_history_is_trimmed_to_max_history():
    store = make_store(max_history=3)
    for i in range(10):
        tid = f"t{i}"
        store.add_queued({"id": tid, "url": "u", "quality": 3, "status": "queued"})
        store.finalize({"id": tid, "status": "completed", "completed_at": float(i)})

    history = store.snapshot()["history"]
    assert len(history) == 3
    # Newest three survive, newest first.
    assert [h["id"] for h in history] == ["t9", "t8", "t7"]


def test_find_history_returns_entry_or_none():
    store = make_store()
    store.add_queued({"id": "a", "url": "u", "quality": 3, "status": "queued"})
    store.finalize({"id": "a", "status": "completed", "completed_at": 1.0})

    found = store.find_history("a")
    assert found is not None and found["id"] == "a"
    assert store.find_history("nope") is None


def test_snapshot_reports_queue_size_shape():
    store = make_store()
    snap = store.snapshot()
    assert set(["active", "history", "queue_size"]) == set(snap.keys())
    assert isinstance(snap["queue_size"], int)
