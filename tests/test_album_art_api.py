"""Seam: the /api/album-art endpoint for Qobuz, driven through the Flask test
client against a fake http_get (issue #15).

Proves the route dispatches Qobuz art to the Qobuz Source adapter and that the
externally observable response shape is unchanged, with no real network call.
"""
import app as app_module


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _stub_http_get(monkeypatch, payload, status_code=200):
    calls = []

    def _get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse(payload, status_code)

    monkeypatch.setattr(app_module, "http_get", _get)
    return calls


def _stub_config(monkeypatch, tmp_path, content):
    cfg = tmp_path / "config.toml"
    cfg.write_text(content)
    monkeypatch.setattr(app_module, "STREAMRIP_CONFIG", str(cfg))


def test_album_art_qobuz_uses_adapter(client, monkeypatch, tmp_path):
    app_module.album_art_cache.clear()
    _stub_config(monkeypatch, tmp_path, 'app_id = "id1"\npassword_or_token = "tok"\n')
    calls = _stub_http_get(
        monkeypatch,
        {
            "image": {"large": "http://art/large.jpg"},
            "tracks_count": 9,
            "release_type": "album",
            "release_date_original": "1997-05-21",
        },
    )

    resp = client.get(
        "/api/album-art", query_string={"source": "qobuz", "type": "album", "id": "111"}
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["album_art"] == "http://art/large.jpg"
    assert data["tracks_count"] == 9
    assert data["release_type"] == "album"
    assert data["year"] == "1997"
    # Hit the adapter's seam, not the real network.
    assert calls and calls[0]["url"] == "https://www.qobuz.com/api.json/0.2/album/get"


def test_album_art_qobuz_caches_result(client, monkeypatch, tmp_path):
    app_module.album_art_cache.clear()
    _stub_config(monkeypatch, tmp_path, 'app_id = "id1"\npassword_or_token = "tok"\n')
    calls = _stub_http_get(monkeypatch, {"image": {"large": "http://art/x.jpg"}})

    q = {"source": "qobuz", "type": "album", "id": "222"}
    client.get("/api/album-art", query_string=q)
    client.get("/api/album-art", query_string=q)
    # Second call served from cache -> seam hit exactly once.
    assert len(calls) == 1


def test_album_art_deezer_album_uses_template_no_network(client, monkeypatch):
    app_module.album_art_cache.clear()
    calls = _stub_http_get(monkeypatch, {})
    resp = client.get(
        "/api/album-art",
        query_string={"source": "deezer", "type": "album", "id": "111"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["album_art"] == "https://api.deezer.com/album/111/image"
    # Album art is a pure template -> no network call.
    assert calls == []


def test_album_art_deezer_artist_uses_api(client, monkeypatch):
    app_module.album_art_cache.clear()
    calls = _stub_http_get(monkeypatch, {"picture_medium": "http://art/medium.jpg"})
    resp = client.get(
        "/api/album-art",
        query_string={"source": "deezer", "type": "artist", "id": "333"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["album_art"] == "http://art/medium.jpg"
    # Artist art hit the adapter's seam, not the real network.
    assert calls and calls[0]["url"] == "https://api.deezer.com/artist/333"
