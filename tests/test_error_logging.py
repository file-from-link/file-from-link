"""Empty-message exception diagnosis (2026-11-15): failures whose str(e)
is '' were recorded with a blank error and never logged, so neither the
DB nor the worker logs could name the cause. The failure handlers now log
repr(e) and fall back to the exception type name."""
import os, sys, tempfile, types
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "jobs.db")
os.environ["DOWNLOAD_DIR"] = os.path.join(_tmp, "downloads")
os.environ["PUBLIC_DIR"] = os.path.join(_tmp, "public")
os.environ.setdefault("BOT_TOKEN", "dummy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_fake_httpx = types.ModuleType("httpx")
class _FakeClient:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def post(self, *a, **k): raise RuntimeError("no network in tests")
_fake_httpx.Client = _FakeClient
sys.modules.setdefault("httpx", _fake_httpx)

import yt_dlp
from app import db
import app.tasks as tasks
import app.downloader as dl
from app.downloader import DownloadError


def _reset_db():
    import sqlite3
    con = sqlite3.connect(os.environ["DATABASE_PATH"])
    con.execute("DELETE FROM jobs")
    con.commit(); con.close()


def test_err_text_empty_message_uses_type_name():
    assert tasks._err_text(DownloadError("")) == "DownloadError (no message)"
    assert tasks._err_text(ValueError()) == "ValueError (no message)"
    assert tasks._err_text(DownloadError("boom")) == "boom"
    assert tasks._err_text(ValueError("bad")) == "bad"


def _run_process_job_with_failure(exc):
    _reset_db()
    jid = db.create_job(1, 11, "https://example.com/v", "best", "en", None)
    sent = []
    def _boom(*a, **k):
        raise exc
    orig_download, orig_send, orig_sweep = tasks.download, tasks.send_text, tasks.sweep_public
    tasks.download = _boom
    tasks.send_text = lambda *a, **k: sent.append((a, k))
    tasks.sweep_public = lambda: None
    try:
        tasks.process_job(jid)
    finally:
        tasks.download, tasks.send_text, tasks.sweep_public = orig_download, orig_send, orig_sweep
    return db.get_job(jid), sent


def test_process_job_records_type_name_on_empty_download_error():
    job, sent = _run_process_job_with_failure(DownloadError(""))
    assert job["status"] == "failed", job
    assert job["error"] == "DownloadError (no message)", job
    assert sent, sent
    assert "DownloadError (no message)" in str(sent), sent


def test_process_job_records_type_name_on_empty_generic_error():
    job, sent = _run_process_job_with_failure(ValueError())
    assert job["status"] == "failed", job
    assert job["error"] == "ValueError (no message)", job
    assert sent, sent  # user still gets the generic failure message


def test_process_job_keeps_real_message():
    job, _sent = _run_process_job_with_failure(DownloadError("boom"))
    assert job["status"] == "failed", job
    assert job["error"] == "boom", job


def test_downloader_empty_ytdlp_error_keeps_type_name():
    def fake_extract(url, choice, job_id, on_progress, client=None, force_best=False):
        raise yt_dlp.utils.DownloadError("")
    orig = dl._extract_once
    dl._extract_once = fake_extract
    try:
        try:
            dl._download_ytdlp("https://example.com/v", "720", "j1")
            raise AssertionError("should have raised")
        except DownloadError as e:
            assert str(e) == "DownloadError (no message)", e
    finally:
        dl._extract_once = orig


def test_on_job_failure_empty_reason_uses_type_name():
    _reset_db()
    jid = db.create_job(1, 11, "https://example.com/v", "best", "en", None)
    sent = []
    orig_send = tasks.send_text
    tasks.send_text = lambda *a, **k: sent.append((a, k))
    class _FakeRQJob:
        args = (jid,)
    try:
        tasks.on_job_failure(_FakeRQJob(), None, ValueError, ValueError(), None)
    finally:
        tasks.send_text = orig_send
    job = db.get_job(jid)
    assert job["status"] == "failed", job
    assert job["error"] == "ValueError", job
    assert sent, sent
