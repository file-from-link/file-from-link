"""ImpersonateTarget regression (2026-11-16): _build_opts passed the bare
string "chrome" as yt-dlp's `impersonate` option. yt-dlp 2026.10.26
(YoutubeDL.__init__ -> _impersonate_target_available ->
rh.is_supported_target) asserts isinstance(target, ImpersonateTarget),
so EVERY download died at construction with a bare AssertionError
(no message) — 766 of 810 post-fix failures, all sites, median 0.5s.
The option must be an ImpersonateTarget; the string stays only as the
fallback for yt-dlp versions without the class."""
import os, sys, tempfile
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "jobs.db")
os.environ["DOWNLOAD_DIR"] = os.path.join(_tmp, "downloads")
os.environ["PUBLIC_DIR"] = os.path.join(_tmp, "public")
os.environ.setdefault("BOT_TOKEN", "dummy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yt_dlp
import app.downloader as dl


def test_impersonate_is_target_not_string():
    opts = dl._build_opts("720", "t", None, None, "https://x.com/example/status/1")
    imp = opts.get("impersonate")
    assert imp is not None, "impersonate option missing (curl-cffi is installed in the test env)"
    assert not isinstance(imp, str), f"impersonate must not be a bare string, got {imp!r}"
    from yt_dlp.networking.impersonate import ImpersonateTarget
    assert isinstance(imp, ImpersonateTarget), f"expected ImpersonateTarget, got {type(imp)}"
    assert imp.client == "chrome", imp


def test_youtubedl_construction_no_assertion_error():
    # Where yt-dlp's curl-cffi handler is registered (the server's
    # 2026.10.26) this constructs fine; where the handler is absent
    # (this VM's older yt-dlp) it raises YoutubeDLError "not available".
    # Either way it must NEVER raise the bare AssertionError that killed
    # every download at construction.
    opts = dl._build_opts("720", "t", None, None, "https://x.com/example/status/1")
    try:
        with yt_dlp.YoutubeDL(opts):
            pass
    except AssertionError:
        raise
    except yt_dlp.utils.YoutubeDLError as e:
        assert "not available" in str(e), f"unexpected YoutubeDLError: {e}"
