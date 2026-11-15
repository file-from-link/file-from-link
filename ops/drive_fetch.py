#!/usr/bin/env python3
"""Fetch YouTube cookie files from Drive into /data/yt_cookies/.

Runs on the EC2 host after the code deploy (the downloader reads the
directory on every request, so files fetched later are picked up with
no restart):
    /opt/ffl-drive/venv/bin/python3 ops/drive_fetch.py <folder-id> [/data/yt_cookies]

Staging: the owner drops yt-1..5.txt exports into the "File From Link
cookies" Drive folder. The folder is shared (Reader) with the project's
service account file-from-link-backup@... — the fetch reads through the
SA, NOT the user token: the user token carries only the drive.file scope
(ops/drive_upload.py), which sees solely files the app itself created,
so owner-dropped files are invisible to it (verified 2026-11-15: both
the cookies folder and the backups folder 404 for that token, while a
list still shows the 5 backup files the app created). A service account
needs no consent flow, so this script mints its token with the
drive.readonly scope instead, which can read anything shared with it.
SA key: /opt/ffl-secrets/drive-sa.json (FFL_DRIVE_SA_KEY).
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

SA_KEY_PATH = Path(os.environ.get("FFL_DRIVE_SA_KEY", "/opt/ffl-secrets/drive-sa.json"))
SA_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

SESSION_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID"}


def build_service():
    """Drive client authenticated as the project's service account with
    the readonly scope (see module docstring for why the user token
    cannot do this fetch). Raises FileNotFoundError when the key is
    missing on the host."""
    if not SA_KEY_PATH.exists():
        raise FileNotFoundError(f"service-account key not found: {SA_KEY_PATH}")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        str(SA_KEY_PATH), scopes=SA_SCOPES)
    return build("drive", "v3", credentials=creds)


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
