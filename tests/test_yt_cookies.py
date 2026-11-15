"""YouTube cookie round-robin (2026-11-15): five burner-account exports
in YT_COOKIES_DIR as yt-*.txt, one per request, cycling in order.
Partial exports (no full session) are skipped, never poisoning the
rotation; an empty directory falls back to the single COOKIES_FILE."""
import os, sys, tempfile
from pathlib import Path
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "jobs.db")
os.environ["DOWNLOAD_DIR"] = os.path.join(_tmp, "downloads")
os.environ["PUBLIC_DIR"] = os.path.join(_tmp, "public")
os.environ.setdefault("BOT_TOKEN", "dummy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_COOKIE_DIR = Path(_tmp) / "yt_cookies"
_COOKIE_DIR.mkdir()
os.environ["YT_COOKIES_DIR"] = str(_COOKIE_DIR)

import app.downloader as dl

FULL = ["SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO", "PREF"]
PARTIAL = ["__Secure-3PSID", "LOGIN_INFO", "PREF"]  # no SID / 1PSID


def _write_cookie(name, names):
    p = _COOKIE_DIR / name
    lines = ["# Netscape HTTP Cookie File", ""]
    for i, n in enumerate(names):
        lines.append(f".youtube.com\tTRUE\t/\tTRUE\t1829287965\t{n}\tvalue{i}")
    p.write_text("\n".join(lines) + "\n")
    return p


def _reset():
    dl._YT_COOKIE_FILES = None
    dl._YT_COOKIE_CYCLE = None
    dl._YT_COOKIE_SEEN = {}
    for p in _COOKIE_DIR.iterdir():
        p.unlink()


def test_rotation_cycles_in_order():
    _reset()
    _write_cookie("yt-1.txt", FULL)
    _write_cookie("yt-2.txt", FULL)
    _write_cookie("yt-3.txt", FULL)
    got = [dl._next_yt_cookie().name for _ in range(7)]
    assert got == ["yt-1.txt", "yt-2.txt", "yt-3.txt",
                   "yt-1.txt", "yt-2.txt", "yt-3.txt", "yt-1.txt"], got


def test_partial_file_skipped():
    _reset()
    _write_cookie("yt-1.txt", PARTIAL)  # no SID: must never be served
    _write_cookie("yt-2.txt", FULL)
    _write_cookie("yt-3.txt", FULL)
    got = {dl._next_yt_cookie().name for _ in range(6)}
    assert got == {"yt-2.txt", "yt-3.txt"}, got


def test_falls_back_to_single_cookie_file():
    _reset()
    single = Path(_tmp) / "cookies.txt"
    single.write_text("# Netscape HTTP Cookie File\n")
    orig = dl.COOKIES_FILE
    dl.COOKIES_FILE = str(single)
    try:
        assert dl._next_yt_cookie() == single
        assert dl._next_yt_cookie() == single  # one file: same every time
    finally:
        dl.COOKIES_FILE = orig


def test_no_cookies_returns_none():
    _reset()
    orig = dl.COOKIES_FILE
    dl.COOKIES_FILE = ""
    try:
        assert dl._next_yt_cookie() is None
        assert dl._yt_cookie_files() == []
    finally:
        dl.COOKIES_FILE = orig


def test_build_opts_youtube_rotates_and_others_unchanged():
    _reset()
    _write_cookie("yt-1.txt", FULL)
    _write_cookie("yt-2.txt", FULL)
    yt = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    first = dl._build_opts("720", "j1", None, url=yt)["cookiefile"]
    second = dl._build_opts("720", "j1", None, url=yt)["cookiefile"]
    assert first != second, (first, second)
    assert {Path(first).name, Path(second).name} == {"yt-1.txt", "yt-2.txt"}
    # X keeps its own cookie file, generic sites keep the single fallback
    xfile = Path(_tmp) / "x.txt"
    xfile.write_text("x")
    single = Path(_tmp) / "cookies.txt"
    single.write_text("c")
    orig_x, orig_c = dl.X_COOKIES_FILE, dl.COOKIES_FILE
    dl.X_COOKIES_FILE, dl.COOKIES_FILE = str(xfile), str(single)
    try:
        assert dl._build_opts("720", "j1", None, url="https://x.com/a/status/1")["cookiefile"] == str(xfile)
        assert dl._build_opts("720", "j1", None, url="https://example.com/v")["cookiefile"] == str(single)
    finally:
        dl.X_COOKIES_FILE, dl.COOKIES_FILE = orig_x, orig_c


def test_cycle_rebuilds_when_files_change():
    _reset()
    _write_cookie("yt-1.txt", FULL)
    _write_cookie("yt-2.txt", FULL)
    assert [dl._next_yt_cookie().name for _ in range(2)] == ["yt-1.txt", "yt-2.txt"]
    _write_cookie("yt-3.txt", FULL)  # new export lands: joins rotation
    got = [dl._next_yt_cookie().name for _ in range(3)]
    assert got == ["yt-1.txt", "yt-2.txt", "yt-3.txt"], got


def test_refreshed_partial_file_joins_rotation():
    _reset()
    _write_cookie("yt-1.txt", PARTIAL)
    _write_cookie("yt-2.txt", FULL)
    assert {dl._next_yt_cookie().name for _ in range(4)} == {"yt-2.txt"}
    _write_cookie("yt-1.txt", FULL)  # re-exported complete: picked up
    got = {dl._next_yt_cookie().name for _ in range(4)}
    assert got == {"yt-1.txt", "yt-2.txt"}, got
