"""Ignoring / resuming sources from the EE Herald admin, and the per-site Firecrawl
interval used for Firecrawl-only sites."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import site_settings as ss
from app.sites import SITES


class _S:
    """Minimal settings stand-in: only state_file is used by site_settings."""

    def __init__(self, folder: Path) -> None:
        self.state_file = str(folder / "scraper_state.json")


def test_ignore_and_resume_round_trip(tmp_path: Path):
    s = _S(tmp_path)
    url = SITES[0]["url"]
    assert ss.ignored_urls(s) == set()
    entry = ss.set_ignored(s, url, True, by="admin 7")
    assert entry["by"] == "admin 7" and entry["at"]
    assert ss.ignored_urls(s) == {url}
    assert [x["url"] for x in ss.active_sites(SITES[:3], s)] == [SITES[1]["url"], SITES[2]["url"]]
    ss.set_ignored(s, url, False)
    assert ss.ignored_urls(s) == set()
    assert len(ss.active_sites(SITES[:3], s)) == 3


def test_damaged_settings_file_ignores_nothing(tmp_path: Path):
    s = _S(tmp_path)
    ss.settings_path(s).write_text("{not json", encoding="utf-8")
    assert ss.ignored_urls(s) == set()
    ss.settings_path(s).write_text(json.dumps({"ignored": ["x"]}), encoding="utf-8")
    assert ss.ignored_urls(s) == set()


def test_by_is_trimmed(tmp_path: Path):
    s = _S(tmp_path)
    entry = ss.set_ignored(s, SITES[0]["url"], True, by="x" * 500)
    assert len(entry["by"]) == 60


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from app import main
    from app.settings import get_settings

    monkeypatch.setattr(main.settings, "internal_api_token", "s3cret")
    monkeypatch.setattr(get_settings(), "state_file", str(tmp_path / "scraper_state.json"))
    monkeypatch.setattr(get_settings(), "output_dir", str(tmp_path / "out"))
    with TestClient(main.app) as c:
        yield c


def test_sources_endpoint_ignore_resume(client):
    h = {"X-Internal-Token": "s3cret"}
    url = SITES[1]["url"]
    r = client.post("/api/v1/sources/state", json={"url": url, "ignored": True, "by": "admin 3"}, headers=h)
    assert r.status_code == 200 and r.json()["ignored"] is True
    rows = {x["url"]: x for x in client.get("/api/v1/sources", headers=h).json()}
    assert rows[url]["ignored"] is True and rows[url]["ignored_at"]
    assert rows[SITES[0]["url"]]["ignored"] is False
    r = client.post("/api/v1/sources/state", json={"url": url, "ignored": False}, headers=h)
    assert r.json()["ignored"] is False
    assert client.get("/api/v1/sources", headers=h).json()[1]["ignored"] is False


def test_unknown_source_refused(client):
    h = {"X-Internal-Token": "s3cret"}
    r = client.post("/api/v1/sources/state", json={"url": "https://evil.example/", "ignored": True}, headers=h)
    assert r.status_code == 404
    assert client.post("/api/v1/sources/state", json={"url": SITES[0]["url"], "ignored": True}).status_code == 401


async def test_per_site_firecrawl_interval():
    from app.services.crawling import Crawler

    class _Budget:
        def try_spend(self):
            raise AssertionError("must not spend while in cooldown")

    now = datetime(2026, 10, 20, 12, 0, 0)
    settings = SimpleSettings(firecrawl_listing_cooldown_hours=8)
    crawler = Crawler(settings, None, object(), _Budget(), None, None, {}, now.date(), now)
    state = {"method": "firecrawl", "last_firecrawl_at": (now - timedelta(hours=10)).isoformat()}
    site = {"name": "Daily site", "url": "https://x.example/news", "firecrawl_cooldown_hours": 24}
    scan = await crawler._scan_with_firecrawl(site, state, [])
    assert scan.status == "skipped" and "cooldown 24h" in scan.detail


class SimpleSettings:
    def __init__(self, **kw):
        self.__dict__.update(kw)
