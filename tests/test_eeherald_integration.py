"""Tests for the pieces the EE Herald admin integration relies on: SEO keywords that must
come from the source, digest retention, the reboot-safe run lock, the internal token and
the background press-release jobs."""

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services.press_release import sourced_keywords
from app.services.state import _lock_owner_gone, prune_old_outputs, run_lock

SOURCE = (
    "Renesas Electronics Corporation today introduced 100V GaN FETs for AI data center "
    "power and humanoid robots. The RGA1A0100 family ships in a 5 x 6 mm package."
)


def test_keywords_kept_only_when_in_source():
    out = sourced_keywords(
        ["Renesas", "100V GaN FET", "AI data center power", "NVIDIA", "best GaN ever", "RGA1A0100"],
        SOURCE,
    )
    assert out == ["Renesas", "100V GaN FET", "AI data center power", "RGA1A0100"]


def test_keywords_ignore_case_spacing_duplicates_and_limit():
    out = sourced_keywords(["gan fets", "GaN FETs", "ai-data center", "Renesas"] * 3, SOURCE, limit=3)
    assert out == ["gan fets", "ai-data center", "Renesas"]


def test_prune_old_outputs_removes_only_old_digests(tmp_path: Path):
    (tmp_path / "reports").mkdir()
    old_pdf = tmp_path / "News_Digest_01_01_2026_0800.pdf"
    old_report = tmp_path / "reports" / "Run_Report_01_01_2026_080000.txt"
    new_pdf = tmp_path / "News_Digest_07_10_2026_0800.pdf"
    keep_other = tmp_path / "press_releases.json"
    for path in (old_pdf, old_report, new_pdf, keep_other):
        path.write_text("x")
    long_ago = time.time() - 200 * 86400
    for path in (old_pdf, old_report, keep_other):
        os.utime(path, (long_ago, long_ago))
    assert prune_old_outputs(tmp_path, 90) == 2
    assert not old_pdf.exists() and not old_report.exists()
    assert new_pdf.exists() and keep_other.exists()
    assert prune_old_outputs(tmp_path, 0) == 0  # 0 = keep everything


@pytest.mark.skipif(os.name != "posix", reason="process check is POSIX only")
def test_lock_left_by_dead_process_is_taken_over(tmp_path: Path):
    state = tmp_path / "scraper_state.json"
    lock = tmp_path / "scraper_state.json.lock"
    lock.write_text("pid=999999 started=2026-10-07 08:00:00\n")
    assert _lock_owner_gone(lock)
    with run_lock(state):  # does not raise: the owner is gone
        assert lock.exists()
    assert not lock.exists()


@pytest.mark.skipif(os.name != "posix", reason="process check is POSIX only")
def test_lock_of_running_process_is_respected(tmp_path: Path):
    from app.exceptions import PipelineBusyError

    state = tmp_path / "scraper_state.json"
    lock = tmp_path / "scraper_state.json.lock"
    lock.write_text(f"pid={os.getppid()} started=2026-10-07 08:00:00\n")
    assert not _lock_owner_gone(lock)
    with pytest.raises(PipelineBusyError):
        with run_lock(state):
            pass


@pytest.fixture()
def client(monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "internal_api_token", "s3cret")
    with TestClient(main.app) as c:
        yield c


def test_internal_token_required(client):
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/press-releases").status_code == 401
    bad = client.get("/api/v1/press-releases", headers={"X-Internal-Token": "nope"})
    assert bad.status_code == 401
    assert client.get("/", headers={"X-Internal-Token": "nope"}).status_code == 401


def test_press_release_job_runs_in_background(client, monkeypatch):
    from app.api.v1 import press_releases as pr

    calls = []

    async def fake_generate(url, settings, title=None, name=None):
        calls.append(url)
        return SimpleNamespace(id="20261007_080000_fake")

    monkeypatch.setattr(pr, "generate_press_release", fake_generate)
    monkeypatch.setattr(pr, "find_draft_for_url", lambda url, out: None)
    h = {"X-Internal-Token": "s3cret"}

    started = client.post("/api/v1/press-releases/jobs", json={"url": "https://example.com/pr/1"}, headers=h)
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    for _ in range(50):
        status = client.get(f"/api/v1/press-releases/jobs/{job_id}", headers=h).json()
        if status["status"] != "running":
            break
        time.sleep(0.02)
    assert status["status"] == "done" and status["draft_id"] == "20261007_080000_fake"
    assert calls == ["https://example.com/pr/1"]

    bad = client.post("/api/v1/press-releases/jobs", json={"url": "ftp://x"}, headers=h)
    assert bad.status_code == 400
    assert client.get("/api/v1/press-releases/jobs/unknown", headers=h).status_code == 404


def test_press_release_job_reports_errors(client, monkeypatch):
    from app.api.v1 import press_releases as pr

    async def failing(url, settings, title=None, name=None):
        raise pr.PressReleaseError("Could not read the article text from the source page.")

    monkeypatch.setattr(pr, "generate_press_release", failing)
    monkeypatch.setattr(pr, "find_draft_for_url", lambda url, out: None)
    h = {"X-Internal-Token": "s3cret"}
    job_id = client.post(
        "/api/v1/press-releases/jobs", json={"url": "https://example.com/pr/2"}, headers=h
    ).json()["job_id"]
    for _ in range(50):
        status = client.get(f"/api/v1/press-releases/jobs/{job_id}", headers=h).json()
        if status["status"] != "running":
            break
        time.sleep(0.02)
    assert status["status"] == "error"
    assert "Could not read" in status["error"]


def test_existing_article_is_returned_without_rewriting(client, monkeypatch):
    from app.api.v1 import press_releases as pr

    async def must_not_run(*a, **k):
        raise AssertionError("should reuse the saved article")

    monkeypatch.setattr(pr, "generate_press_release", must_not_run)
    monkeypatch.setattr(pr, "find_draft_for_url", lambda url, out: SimpleNamespace(id="saved_1"))
    h = {"X-Internal-Token": "s3cret"}
    out = client.post("/api/v1/press-releases/jobs", json={"url": "https://example.com/pr/3"}, headers=h)
    assert out.json()["status"] == "done" and out.json()["draft_id"] == "saved_1"
