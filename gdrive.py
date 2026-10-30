"""Google Drive storage helpers (service-account based).

Config:
  GDRIVE_CREDENTIALS  - full service-account JSON (Heroku config var)
  GDRIVE_FOLDER       - folder name in the service account's/shared Drive
                        (default "Books"); auto-created on first use

Books live in Drive; the local (ephemeral) file is deleted after upload.
"""
import io
import json
import logging
import os
import threading

log = logging.getLogger(__name__)

FOLDER_NAME = os.environ.get("GDRIVE_FOLDER", "Books")
_cache = {"svc": None, "folder_id": None}
_lock = threading.Lock()


def enabled():
    return bool(os.environ.get("GDRIVE_CREDENTIALS"))


def _service():
    if _cache["svc"]:
        return _cache["svc"]
    with _lock:
        if _cache["svc"]:
            return _cache["svc"]
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        info = json.loads(os.environ["GDRIVE_CREDENTIALS"])
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"])
        _cache["svc"] = build("drive", "v3", credentials=creds, cache_discovery=False)
        return _cache["svc"]


def _folder_id():
    if _cache["folder_id"]:
        return _cache["folder_id"]
    with _lock:
        if _cache["folder_id"]:
            return _cache["folder_id"]
        svc = _service()
        res = svc.files().list(
            q="mimeType='application/vnd.google-apps.folder' and name='%s' and trashed=false"
              % FOLDER_NAME.replace("'", "\\'"),
            spaces="drive", fields="files(id,name)").execute()
        files = res.get("files") or []
        if files:
            _cache["folder_id"] = files[0]["id"]
        else:
            f = svc.files().create(
                body={"name": FOLDER_NAME,
                      "mimeType": "application/vnd.google-apps.folder"}).execute()
            _cache["folder_id"] = f["id"]
        return _cache["folder_id"]


def upload(path, name=None):
    """Upload a local file into the Drive folder. Returns (file_id, size_bytes)."""
    from googleapiclient.http import MediaFileUpload
    svc = _service()
    name = name or os.path.basename(path)
    # replace any earlier copy with the same name
    old = svc.files().list(
        q="'%s' in parents and name='%s' and trashed=false"
          % (_folder_id(), name.replace("'", "\\'")),
        fields="files(id)").execute().get("files") or []
    for f in old:
        svc.files().delete(fileId=f["id"]).execute()
    media = MediaFileUpload(path, resumable=False)
    meta = svc.files().create(
        body={"name": name, "parents": [_folder_id()]},
        media_body=media, fields="id,size").execute()
    return meta["id"], int(meta.get("size") or os.path.getsize(path))


def open_file(file_id):
    """Return a file-like object streaming the Drive file content."""
    from googleapiclient.http import MediaIoBaseDownload
    svc = _service()
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf


def get_meta(file_id):
    """Filename + size for a Drive file (used when local copy is gone)."""
    svc = _service()
    return svc.files().get(fileId=file_id, fields="name,size").execute()
