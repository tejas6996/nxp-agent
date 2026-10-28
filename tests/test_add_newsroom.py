"""Adding newsrooms from the admin: URL guard, stored sources, the check/add/remove API."""

import asyncio
import json
import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.infra import url_guard as ug
from app.services import site_settings as ss
from app.sites import SITES


# --------------------------------------------------------------------------- URL guard

@pytest.mark.parametrize("url", [
    "http://localhost/", "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://10.1.2.3/",
    "http://192.168.0.10/", "http://172.16.5.4/", "http://100.64.0.1/", "http://[::1]/", "http://[fd00::1]/",
    "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://2130706433/", "http://intranet/", "http://db.internal/",
    "http://printer.local/", "https://user:pass@example.com/", "https://example.com:8443/", "ftp://example.com/",
    "file:///etc/passwd", "gopher://example.com/",
])
def test_unsafe_urls_refused(url):
    with pytest.raises(ug.UnsafeURLError):
        asyncio.run(ug.resolve_public(url))


def _fake_dns(monkeypatch, answers):
    async def fake(self, host, port, **kw):  # noqa: ANN001
        if host not in answers:
            raise socket.gaierror("not found")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers[host]]
    monkeypatch.setattr(asyncio.AbstractEventLoop, "getaddrinfo", fake, raising=False)
    import asyncio.base_events as be
    monkeypatch.setattr(be.BaseEventLoop, "getaddrinfo", fake)


def test_dns_answers_checked(monkeypatch):
    _fake_dns(monkeypatch, {
        "news.example.com": ["93.184.216.34"],
        "evil.example.com": ["127.0.0.1"],
        "mixed.example.com": ["93.184.216.34", "10.0.0.7"],
        "v6.example.com": ["2606:2800:220:1:248:1893:25c8:1946"],
    })
    assert asyncio.run(ug.resolve_public("https://news.example.com/press")) == ("news.example.com", 443, "93.184.216.34")
    assert asyncio.run(ug.resolve_public("http://v6.example.com/"))[2].startswith("2606:")
    for bad in ("https://evil.example.com/", "https://mixed.example.com/", "https://nowhere.example.com/"):
        with pytest.raises(ug.UnsafeURLError):
            asyncio.run(ug.resolve_public(bad))


def test_normalise_user_url():
    assert ug.normalise_user_url(" www.renesas.com/en/about/newsroom ") == "https://www.renesas.com/en/about/newsroom"
    assert ug.normalise_user_url("HTTPS://News.ti.com#top") == "https://News.ti.com/"
    assert ug.normalise_user_url("example.com:443/news") == "https://example.com:443/news"
    for bad in ("", "javascript:alert(1)", "mailto:a@b.com", "data:text/html,x", "x" * 3000):
        with pytest.raises(ug.UnsafeURLError):
            ug.normalise_user_url(bad)


def test_curl_pin_entry():
    assert ug.curl_resolve_entry("a.com", 443, "1.2.3.4") == "a.com:443:1.2.3.4"
    assert ug.curl_resolve_entry("a.com", 443, "2606::1") == "a.com:443:[2606::1]"


# --------------------------------------------------------------------------- stored sources

class _S:
    def __init__(self, folder: Path) -> None:
        self.state_file = str(folder / "scraper_state.json")


def test_add_list_remove_custom_site(tmp_path: Path):
    s = _S(tmp_path)
    entry = ss.add_custom_site(s, {"name": "  Acme   Semi ", "url": "https://acme.example/news", "feed": None}, by="admin 9")
    assert entry["name"] == "Acme Semi" and entry["custom"] and entry["added_by"] == "admin 9"
    names = [x["name"] for x in ss.all_sites(s)]
    assert names[: len(SITES)] == [x["name"] for x in SITES] and names[-1] == "Acme Semi"
    with pytest.raises(ValueError):  # same newsroom twice
        ss.add_custom_site(s, {"name": "Acme 2", "url": "https://www.acme.example/news/"})
    with pytest.raises(ValueError):  # name already used (built-in)
        ss.add_custom_site(s, {"name": SITES[0]["name"].upper(), "url": "https://other.example/news"})
    with pytest.raises(ValueError):  # already a built-in newsroom
        ss.add_custom_site(s, {"name": "Copy", "url": SITES[0]["url"]})
    fc = ss.add_custom_site(s, {"name": "JS Only Co", "url": "https://js.example/press", "firecrawl": True})
    assert fc["firecrawl_cooldown_hours"] == 24
    assert not ss.remove_custom_site(s, SITES[0]["url"])  # built-in: cannot be removed
    ss.set_ignored(s, "https://acme.example/news", True)
    assert ss.remove_custom_site(s, "https://acme.example/news")
    assert "https://acme.example/news" not in ss.ignored_urls(s)
    assert [x["name"] for x in ss.load_custom_sites(s)] == ["JS Only Co"]


@pytest.mark.parametrize("name", ["<script>x</script>", "a", "x" * 81, "A<b>", "Name‮", "x\x00y"])
def test_bad_names_refused(name):
    with pytest.raises(ValueError):
        ss.clean_name(name)


def test_names_tidied():
    assert ss.clean_name("  Bad\nName  ") == "Bad Name"
    assert ss.clean_name("Rohde & Schwarz (India)") == "Rohde & Schwarz (India)"


def test_tampered_file_entries_ignored(tmp_path: Path):
    s = _S(tmp_path)
    ss.custom_path(s).write_text(json.dumps({"sites": [
        {"name": "Good Co", "url": "https://good.example/news"},
        {"name": "Internal", "url": "http://localhost/admin"},
        {"name": "Ftp", "url": "ftp://x.example/"},
        {"name": "<b>bad</b>", "url": "https://x.example/"},
        {"name": "Port", "url": "https://x.example:8080/"},
        "not a dict",
    ]}), encoding="utf-8")
    assert [x["name"] for x in ss.load_custom_sites(s)] == ["Good Co"]
    ss.custom_path(s).write_text("{broken", encoding="utf-8")
    assert ss.load_custom_sites(s) == [] and len(ss.all_sites(s)) == len(SITES)


# --------------------------------------------------------------------------- API

@pytest.fixture()
def client(monkeypatch, tmp_path):
    from app import main
    from app.settings import get_settings

    monkeypatch.setattr(main.settings, "internal_api_token", "s3cret")
    monkeypatch.setattr(get_settings(), "state_file", str(tmp_path / "scraper_state.json"))
    monkeypatch.setattr(get_settings(), "output_dir", str(tmp_path / "out"))

    async def public_ok(url):
        ug.basic_check(url)
        host = (url.split("//", 1)[1].split("/", 1)[0]).split(":")[0]
        if host in ("localhost", "127.0.0.1") or host.startswith("10."):
            raise ug.UnsafeURLError("That address is not on the public internet.")
        return host, 443, "93.184.216.34"

    from app.api.v1 import sources_admin as sa
    monkeypatch.setattr(sa, "resolve_public", public_ok)
    with TestClient(main.app) as c:
        yield c


H = {"X-Internal-Token": "s3cret"}


def _wait(client, job_id):
    for _ in range(100):
        j = client.get(f"/api/v1/sources/check/{job_id}", headers=H).json()
        if j["status"] != "running":
            return j
        time.sleep(0.02)
    return j


def _fake_check(monkeypatch, result):
    from app.api.v1 import sources_admin as sa

    async def fake(http, llm, sem, url, today):
        return dict(result, input=url, notes=list(result.get("notes", [])))
    monkeypatch.setattr(sa, "check_newsroom", fake)


def test_check_then_add_then_remove(client, monkeypatch):
    _fake_check(monkeypatch, {"verdict": "feed", "method": "feed", "listing": "https://acme.example/news",
                              "feed": "https://acme.example/news/feed/", "items": 10, "newest": "2026-10-20",
                              "titles": ["Acme launches X"], "samples": [{"title": "Acme launches X", "url": "https://acme.example/news/x", "date": "2026-10-20"}],
                              "name_guess": "Acme"})
    start = client.post("/api/v1/sources/check", json={"url": "acme.example/news"}, headers=H)
    assert start.status_code == 202 and start.json()["url"] == "https://acme.example/news"
    job = _wait(client, start.json()["job_id"])
    assert job["status"] == "done" and job["result"]["addable"] and job["result"]["feed"].endswith("/feed/")
    added = client.post("/api/v1/sources/custom", json={"job_id": job["job_id"], "name": "Acme Corp", "by": "admin 4"}, headers=H)
    assert added.status_code == 201 and added.json()["feed"] == "https://acme.example/news/feed/"
    again = client.post("/api/v1/sources/custom", json={"job_id": job["job_id"], "name": "Acme Again"}, headers=H)
    assert again.status_code == 400  # one add per check
    rows = {r["url"]: r for r in client.get("/api/v1/sources", headers=H).json()}
    assert rows["https://acme.example/news"]["custom"] is True and rows["https://acme.example/news"]["added_at"]
    assert rows[SITES[0]["url"]]["custom"] is False
    dup = _wait(client, client.post("/api/v1/sources/check", json={"url": "https://acme.example/news/"}, headers=H).json()["job_id"])
    assert dup["result"]["verdict"] == "duplicate" and dup["result"]["duplicate_of"] == "Acme Corp"
    gone = client.post("/api/v1/sources/custom/remove", json={"url": "https://acme.example/news", "by": "admin 4"}, headers=H)
    assert gone.status_code == 200
    assert client.post("/api/v1/sources/custom/remove", json={"url": SITES[0]["url"]}, headers=H).status_code == 404


def test_check_refuses_private_and_bad_links(client):
    for bad in ("http://localhost:80/", "http://10.0.0.8/news", "javascript:alert(1)", "ftp://x.example/"):
        r = client.post("/api/v1/sources/check", json={"url": bad}, headers=H)
        assert r.status_code == 400, bad


def test_add_needs_a_passed_check(client, monkeypatch):
    assert client.post("/api/v1/sources/custom", json={"job_id": "nope", "name": "X Co"}, headers=H).status_code == 400
    _fake_check(monkeypatch, {"verdict": "dead", "why": "HTTP 404"})
    job = _wait(client, client.post("/api/v1/sources/check", json={"url": "https://dead.example/x"}, headers=H).json()["job_id"])
    assert job["result"]["addable"] is False
    assert client.post("/api/v1/sources/custom", json={"job_id": job["job_id"], "name": "Dead Co"}, headers=H).status_code == 400


def test_bad_name_rejected_on_add(client, monkeypatch):
    _fake_check(monkeypatch, {"verdict": "direct", "method": "direct", "listing": "https://n.example/press", "items": 5,
                              "titles": ["t"], "samples": []})
    job = _wait(client, client.post("/api/v1/sources/check", json={"url": "https://n.example/press"}, headers=H).json()["job_id"])
    r = client.post("/api/v1/sources/custom", json={"job_id": job["job_id"], "name": "<img src=x>"}, headers=H)
    assert r.status_code == 400


def test_endpoints_need_token(client):
    assert client.post("/api/v1/sources/check", json={"url": "https://a.example/"}).status_code == 401
    assert client.post("/api/v1/sources/custom/remove", json={"url": "https://a.example/"}).status_code == 401
