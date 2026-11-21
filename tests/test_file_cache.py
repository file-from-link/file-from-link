"""file_id cache (2026-11-20): a repeat of an already-delivered (url, fmt)
is re-sent by file_id — no download, no upload, no egress. Covers the
DB layer (key, TTL, delete), the sender's file_id extraction, and the
process_job paths: hit skips download, miss stores after send, and a
dead file_id falls back to a fresh download instead of failing."""
import os, sqlite3, sys, tempfile, time, types
from pathlib import Path
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

import yt_dlp  # noqa: F401  (app.downloader imports it)
from app import db
import app.tasks as tasks
from app.sender import result_file_id

URL = "https://x.com/someone/status/123/video/1"


def _reset():
    con = sqlite3.connect(os.environ["DATABASE_PATH"])
    con.execute("DELETE FROM jobs")
    con.execute("DELETE FROM file_cache")
    con.commit(); con.close()


def _store(**kw):
    d = dict(url=URL, fmt="best", file_id="FILEID1", kind="video",
             filesize=1234, title="T", description="D" * 40,
             has_video=True, duration=12.5)
    d.update(kw)
    db.store_file_cache(**d)
    return d


def test_store_lookup_round_trip():
    _reset()
    assert db.lookup_file_cache(URL, "best") is None
    row = _store()
    got = db.lookup_file_cache(URL, "best")
    assert got is not None, got
    assert got["file_id"] == "FILEID1" and got["kind"] == "video"
    assert got["filesize"] == 1234 and got["title"] == "T"
    assert got["description"] == "D" * 40
    assert got["has_video"] == 1 and got["duration"] == 12.5


def test_fmt_is_part_of_the_key():
    _reset()
    _store()
    assert db.lookup_file_cache(URL, "h720") is None
    assert db.lookup_file_cache("https://x.com/other/status/9", "best") is None


def test_expired_rows_are_missed_and_pruned_on_store():
    _reset()
    old = time.time() - 31 * 86400
    con = sqlite3.connect(os.environ["DATABASE_PATH"])
    con.execute("INSERT INTO file_cache (url, fmt, file_id, kind, filesize, title, description, has_video, duration, created, updated) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (URL, "best", "OLDID", "video", 1, "T", "", 0, None, old, old))
    con.commit(); con.close()
    assert db.lookup_file_cache(URL, "best") is None
    _store(url="https://x.com/other/status/9")
    con = sqlite3.connect(os.environ["DATABASE_PATH"])
    n = con.execute("SELECT COUNT(*) c FROM file_cache WHERE file_id='OLDID'").fetchone()[0]
    con.close()
    assert n == 0


def test_delete_file_cache():
    _reset()
    _store()
    db.delete_file_cache(URL, "best")
    assert db.lookup_file_cache(URL, "best") is None


def test_cache_hit_column_exists_with_default_zero():
    _reset()
    cols = [r[1] for r in sqlite3.connect(os.environ["DATABASE_PATH"]).execute("PRAGMA table_info(jobs)")]
    assert "cache_hit" in cols, cols
    jid = db.create_job(1, 11, URL, "best", "en", None)
    assert db.get_job(jid)["cache_hit"] == 0


def test_migration_adds_cache_hit_to_old_jobs_table():
    """A pre-existing jobs table (created before this change, no
    cache_hit column) gains the column on init(), and its old rows
    read back as 0."""
    old_db = os.path.join(_tmp, "old-jobs.db")
    con = sqlite3.connect(old_db)
    con.execute("""CREATE TABLE jobs(
      id TEXT PRIMARY KEY, chat_id INTEGER, user_id INTEGER, url TEXT,
      fmt TEXT, status TEXT, title TEXT, filepath TEXT, filesize INTEGER,
      error TEXT, created REAL, updated REAL)""")
    con.execute("INSERT INTO jobs (id, chat_id, user_id, url, fmt, status, created, updated) "
                "VALUES ('oldjob1', 1, 11, ?, 'best', 'done', 1, 1)", (URL,))
    con.commit(); con.close()
    prev = db.DATABASE_PATH
    db.DATABASE_PATH = old_db
    try:
        db.init()
        con = sqlite3.connect(old_db)
        cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)")]
        row = con.execute("SELECT cache_hit FROM jobs WHERE id='oldjob1'").fetchone()
        con.close()
    finally:
        db.DATABASE_PATH = prev
    assert "cache_hit" in cols, cols
    assert row is not None and row[0] == 0, row


def test_result_file_id_extraction():
    assert result_file_id({"result": {"video": {"file_id": "V1"}}}, "video") == "V1"
    assert result_file_id({"result": {"audio": {"file_id": "A1"}}}, "audio") == "A1"
    assert result_file_id({"result": {"document": {"file_id": "D1"}}}, "document") == "D1"
    assert result_file_id({"result": {"animation": {"file_id": "G1"}}}, "animation") == "G1"
    assert result_file_id({"result": {"photo": [{"file_id": "P1"}, {"file_id": "P2"}]}}, "photo") == "P2"
    assert result_file_id({"result": {}}, "video") is None
    assert result_file_id(None, "video") is None
    assert result_file_id({"ok": False}, "video") is None


def test_is_bad_file_id():
    assert tasks._is_bad_file_id(Exception("Bad Request: wrong file identifier/HTTP URL specified"))
    assert tasks._is_bad_file_id(Exception("FILE_ID_INVALID"))
    assert not tasks._is_bad_file_id(Exception("boom"))
    assert not tasks._is_bad_file_id(Exception("Too Many Requests: retry after 5"))


def _run_job(monkey, seed=None):
    """Run one process_job with the given monkeypatches; returns (job, calls).
    seed, if given, runs after the DB reset so cache rows survive it."""
    _reset()
    if seed:
        seed()
    jid = db.create_job(1, 11, URL, "best", "en", None)
    calls = {"send_file_id": [], "deliver": [], "download": 0, "sent": []}
    orig = {n: getattr(tasks, n) for n in
            ("download", "deliver", "send_file_id", "send_text", "sweep_public", "probe_video")}
    tasks.sweep_public = lambda: None
    tasks.send_text = lambda *a, **k: calls["sent"].append((a, k))
    tasks.probe_video = lambda p: (False, None)
    for name, fn in monkey.items():
        setattr(tasks, name, fn)
    try:
        tasks.process_job(jid)
    finally:
        for n, fn in orig.items():
            setattr(tasks, n, fn)
    return db.get_job(jid), calls


def test_cache_hit_skips_download_and_resends_by_file_id():
    def _boom(*a, **k):
        raise AssertionError("download must not run on a cache hit")
    def _send_file_id(chat_id, kind, file_id, caption=""):
        calls_holder.append((chat_id, kind, file_id))
    calls_holder = []
    job, calls = _run_job({"download": _boom, "send_file_id": _send_file_id}, seed=_store)
    assert job["status"] == "done", job
    assert job["cache_hit"] == 1, job
    assert calls_holder == [(1, "video", "FILEID1")], calls_holder
    assert not any("failed" in str(s).lower() for s in calls["sent"]), calls["sent"]


def test_cache_miss_downloads_and_stores():
    f = Path(_tmp) / "fake.mp4"
    f.write_bytes(b"fake-video-bytes")
    meta = {"title": "Fresh", "description": "D" * 40, "subtitles": [], "files": []}
    def _download(url, fmt, jid, prog, lang):
        return f, meta
    def _deliver(chat_id, path, title, lang, direct):
        assert path == f
        return "document", "NEWFILEID"
    job, calls = _run_job({"download": _download, "deliver": _deliver})
    assert job["status"] == "done", job
    assert job["cache_hit"] == 0, job
    got = db.lookup_file_cache(URL, "best")
    assert got is not None and got["file_id"] == "NEWFILEID", got
    assert got["kind"] == "document" and got["title"] == "Fresh"


def test_dead_file_id_falls_back_to_download():
    f = Path(_tmp) / "fake2.mp4"
    f.write_bytes(b"fake-video-bytes-2")
    meta = {"title": "Fresh2", "description": "", "subtitles": [], "files": []}
    def _send_file_id(chat_id, kind, file_id, caption=""):
        raise Exception("Bad Request: wrong file identifier/HTTP URL specified")
    def _download(url, fmt, jid, prog, lang):
        return f, meta
    def _deliver(chat_id, path, title, lang, direct):
        return "video", "REPLACEDID"
    job, calls = _run_job({"download": _download, "deliver": _deliver,
                           "send_file_id": _send_file_id}, seed=_store)
    assert job["status"] == "done", job
    assert job["cache_hit"] == 0, job  # fell back to a download: not a cache hit
    got = db.lookup_file_cache(URL, "best")
    assert got is not None and got["file_id"] == "REPLACEDID", got
