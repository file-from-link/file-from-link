#!/usr/bin/env python3
"""Fetch YouTube cookie files from Drive into /data/yt_cookies/.

Runs on the EC2 host after the code deploy (the downloader reads the
directory on every request, so files fetched later are picked up with
no restart):
    /opt/ffl-drive/venv/bin/python3 ops/drive_fetch.py <folder-id> [/data/yt_cookies]

Staging: the owner drops yt-1..5.txt exports into the "File From Link
cookies" Drive folder (same user OAuth token as the nightly backup
uploader — see ops/drive_upload.py, whose auth helpers are reused here).
Each file is written mode 600 and validated: Netscape format + a full
session (SID + __Secure-1PSID + __Secure-3PSID). A partial export is
still fetched but reported PARTIAL — the downloader skips partial files
in the rotation, so a re-export can replace it later and this script
just re-runs. Prints FETCHED=<n> PARTIAL=<names|none>; exits 0 only
when every file in the folder was downloaded.
"""
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from drive_upload import build_service  # noqa: E402  (same-dir script, __main__-guarded)

SESSION_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID"}


def validate_cookie_file(path: Path) -> tuple:
    """(cookie_count, session_full). Raises ValueError on bad format."""
    lines = path.read_text().splitlines()
    names, count = set(), 0
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            raise ValueError(f"{path.name}: bad line (want 7 tab fields): {line[:40]!r}")
        names.add(parts[5])
        count += 1
    if count == 0:
        raise ValueError(f"{path.name}: no cookies")
    return count, SESSION_COOKIES <= names


def main(argv) -> int:
    folder_id = argv[1] if len(argv) > 1 else os.environ.get("FFL_YT_COOKIES_FOLDER_ID", "")
    dest = Path(argv[2] if len(argv) > 2 else "/data/yt_cookies")
    if not folder_id:
        print("usage: drive_fetch.py <drive-folder-id> [dest-dir]", file=sys.stderr)
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    service = build_service()
    listed = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name)", pageSize=50).execute().get("files", [])
    if not listed:
        print(f"no files in Drive folder {folder_id}", file=sys.stderr)
        return 2
    from googleapiclient.http import MediaIoBaseDownload
    partial, fetched = [], 0
    for f in sorted(listed, key=lambda x: x["name"]):
        target = dest / f["name"]
        req = service.files().get_media(fileId=f["id"])
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        target.write_bytes(buf.getvalue())
        os.chmod(target, 0o600)
        try:
            count, full = validate_cookie_file(target)
        except ValueError as e:
            print(f"INVALID {f['name']}: {e}", file=sys.stderr)
            target.unlink(missing_ok=True)
            return 2
        print(f"NAME={f['name']} COOKIES={count} SESSION={'full' if full else 'partial'}")
        if not full:
            partial.append(f["name"])
        fetched += 1
    print(f"FETCHED={fetched} PARTIAL={','.join(partial) if partial else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
