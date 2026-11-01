"""Google Drive storage helpers (service-account or OAuth-user based).

Config:
  GDRIVE_CREDENTIALS  - full service-account JSON (Heroku config var)
  GDRIVE_OAUTH_CLIENT_ID / GDRIVE_OAUTH_CLIENT_SECRET /
  GDRIVE_OAUTH_REFRESH_TOKEN - OAuth credentials for a user's My Drive
  GDRIVE_FOLDER       - target folder name in Drive (default "Books");
                        auto-created on first use

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
_lock = threading.RLock()


def enabled():
    oauth_keys = ("GDRIVE_OAUTH_CLIENT_ID", "GDRIVE_OAUTH_CLIENT_SECRET",
                  "GDRIVE_OAUTH_REFRESH_TOKEN")
    return bool(os.environ.get("GDRIVE_CREDENTIALS")
                or any(os.environ.get(key) for key in oauth_keys))


def _service():
    if _cache["svc"]:
        return _cache["svc"]
    with _lock:
        if _cache["svc"]:
            return _cache["svc"]
        from googleapiclient.discovery import build
        oauth_keys = ("GDRIVE_OAUTH_CLIENT_ID", "GDRIVE_OAUTH_CLIENT_SECRET",
                      "GDRIVE_OAUTH_REFRESH_TOKEN")
        oauth_values = [os.environ.get(key) for key in oauth_keys]
        if any(oauth_values):
            if not all(oauth_values):
                raise RuntimeError(
                    "Set all three GDRIVE_OAUTH_CLIENT_ID, "
                    "GDRIVE_OAUTH_CLIENT_SECRET, and "
                    "GDRIVE_OAUTH_REFRESH_TOKEN values.")
            from google.oauth2.credentials import Credentials
            creds = Credentials(
                token=None,
                refresh_token=oauth_values[2],
                token_uri="https://oauth2.googleapis.com/token",
                client_id=oauth_values[0],
                client_secret=oauth_values[1],
                scopes=["https://www.googleapis.com/auth/drive"])
        else:
            from google.oauth2 import service_account
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
        # The target folder usually lives in the *user's* Drive and is shared
        # with this service account, so search "shared with me" first.
        res = svc.files().list(
            q="mimeType='application/vnd.google-apps.folder' and name='%s' "
              "and trashed=false and sharedWithMe" % FOLDER_NAME.replace("'", "\\'"),
            spaces="drive", fields="files(id,name)",
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        files = res.get("files") or []
        if not files:
            # fall back to the service account's own Drive
            res = svc.files().list(
                q="mimeType='application/vnd.google-apps.folder' and name='%s' "
                  "and trashed=false" % FOLDER_NAME.replace("'", "\\'"),
                spaces="drive", fields="files(id,name)",
                supportsAllDrives=True).execute()
            files = res.get("files") or []
        if files:
            _cache["folder_id"] = files[0]["id"]
        else:
            f = svc.files().create(
                body={"name": FOLDER_NAME,
                      "mimeType": "application/vnd.google-apps.folder"},
                supportsAllDrives=True).execute()
            _cache["folder_id"] = f["id"]
        return _cache["folder_id"]


def ensure_subfolder(author):
    """Create/return an author subfolder under the Library folder."""
    if not author:
        return _folder_id()
    key = "sub:" + author
    with _lock:
        if _cache.get(key):
            return _cache[key]
        svc = _service()
        parent = _folder_id()
        q = ("mimeType='application/vnd.google-apps.folder' and name='%s' "
             "and '%s' in parents and trashed=false"
             % (author.replace("'", "\\'"), parent))
        res = svc.files().list(q=q, spaces="drive", fields="files(id,name)",
                               supportsAllDrives=True).execute()
        files = res.get("files") or []
        if files:
            fid = files[0]["id"]
        else:
            f = svc.files().create(
                body={"name": author, "parents": [parent],
                      "mimeType": "application/vnd.google-apps.folder"},
                supportsAllDrives=True).execute()
            fid = f["id"]
        _cache[key] = fid
        return fid


def upload(path, name=None, subfolder=None, attempts=4):
    """Upload a local file into the Drive folder (optionally an author
    subfolder). Retries transient API/rate-limit failures. Returns
    (file_id, size_bytes)."""
    import time as _time
    last = None
    for i in range(attempts):
        try:
            return _upload_once(path, name, subfolder)
        except Exception as e:
            last = e
            message = _drive_error_message(e)
            if not _is_retryable_drive_error(e):
                raise RuntimeError(message) from e
            log.warning("GDrive upload attempt %d/%d failed: %s",
                        i + 1, attempts, message)
            if i < attempts - 1:
                # reset caches in case folder access changed (e.g. re-shared)
                with _lock:
                    _cache["folder_id"] = None
                _time.sleep(10 + i * 20)  # 10s, 30s, 50s
    raise last


def _is_retryable_drive_error(error):
    status = getattr(getattr(error, "resp", None), "status", None)
    if status in (429, 500, 502, 503, 504):
        return True
    content = getattr(error, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    text = (str(content) + " " + str(error)).lower()
    return any(code in text for code in (
        "ratelimitexceeded", "userratelimitexceeded", "sharingratelimitexceeded"))


def _drive_error_message(error):
    content = getattr(error, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    try:
        payload = json.loads(content)
        details = payload.get("error", {}) if isinstance(payload, dict) else {}
        message = details.get("message") or str(error)
    except (TypeError, ValueError):
        message = str(error)
    lowered = message.lower()
    if "service account" in lowered and "quota" in lowered:
        return (
            "Google Drive rejected this upload because service accounts have no "
            "My Drive storage quota. The folder's Editor sharing is correct, but "
            "it does not grant the service account storage. Configure the "
            "GDRIVE_OAUTH_CLIENT_ID, GDRIVE_OAUTH_CLIENT_SECRET, and "
            "GDRIVE_OAUTH_REFRESH_TOKEN values to upload as your Google user.")
    return message


def _upload_once(path, name, subfolder):
    from googleapiclient.http import MediaFileUpload
    svc = _service()
    name = name or os.path.basename(path)
    parent = ensure_subfolder(subfolder) if subfolder else _folder_id()
    # replace any earlier copy with the same name
    old = svc.files().list(
        q="'%s' in parents and name='%s' and trashed=false"
          % (parent, name.replace("'", "\\'")),
        fields="files(id)").execute().get("files") or []
    for f in old:
        svc.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
    media = MediaFileUpload(path, resumable=False)
    meta = svc.files().create(
        body={"name": name, "parents": [parent]},
        media_body=media, fields="id,size",
        supportsAllDrives=True).execute()
    return meta["id"], int(meta.get("size") or os.path.getsize(path))


# ---- file browser operations (Drive tab) ----

def _invalidate_cache():
    with _lock:
        for k in list(_cache):
            if k != "svc":
                _cache[k] = None


def _esc(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def list_children(parent_id=None):
    """Children of the Library folder (or a subfolder by id)."""
    svc = _service()
    parent = parent_id or _folder_id()
    res = svc.files().list(
        q="'%s' in parents and trashed=false" % parent,
        spaces="drive",
        fields="files(id,name,mimeType,size,modifiedTime)",
        orderBy="folder,name",
        pageSize=200, supportsAllDrives=True,
        includeItemsFromAllDrives=True).execute()
    out = []
    for f in res.get("files") or []:
        out.append({
            "id": f["id"], "name": f["name"],
            "folder": f.get("mimeType") == "application/vnd.google-apps.folder",
            "size": int(f.get("size") or 0),
            "modified": f.get("modifiedTime") or "",
        })
    return {"parent": parent, "children": out}


def walk_library():
    """All non-folder files anywhere under the Library folder, recursively.
    Returns [{'id','name','size','path'}] where path is 'Author/file.epub'."""
    svc = _service()
    out = []
    def walk(parent, prefix, depth):
        if depth > 10:
            return
        page = svc.files().list(
            q="'%s' in parents and trashed=false" % parent,
            spaces="drive",
            fields="nextPageToken,files(id,name,mimeType)",
            pageSize=200, supportsAllDrives=True,
            includeItemsFromAllDrives=True)
        while True:
            res = page.execute()
            for f in res.get("files") or []:
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    walk(f["id"], prefix + f["name"] + "/", depth + 1)
                else:
                    out.append({
                        "id": f["id"], "name": f["name"],
                        "path": prefix + f["name"],
                        "mimeType": f.get("mimeType") or "",
                    })
            tok = res.get("nextPageToken")
            if not tok:
                break
            page = svc.files().list(
                q="'%s' in parents and trashed=false" % parent,
                spaces="drive",
                fields="nextPageToken,files(id,name,mimeType)",
                pageSize=200, pageToken=tok, supportsAllDrives=True,
                includeItemsFromAllDrives=True)
    walk(_folder_id(), "", 0)
    return out


def path_info(file_id):
    """Breadcrumb chain from the Library root down to this folder."""
    svc = _service()
    root = _folder_id()
    chain = []
    cur = file_id
    for _ in range(20):
        f = svc.files().get(fileId=cur, fields="id,name,parents",
                            supportsAllDrives=True).execute()
        chain.append({"id": f["id"], "name": f["name"]})
        parents = f.get("parents") or []
        if not parents or cur == root:
            break
        cur = parents[0]
    if chain and chain[-1]["id"] != root:
        chain.append({"id": root, "name": FOLDER_NAME})
    return chain[::-1]


def stat(file_id):
    """Metadata (id, name, size) for a single Drive file."""
    svc = _service()
    f = svc.files().get(fileId=file_id, fields="id,name,size",
                        supportsAllDrives=True).execute()
    return {"id": f["id"], "name": f["name"], "size": f.get("size")}


def mkdir(parent_id, name):
    svc = _service()
    parent = parent_id or _folder_id()
    f = svc.files().create(
        body={"name": name, "parents": [parent],
              "mimeType": "application/vnd.google-apps.folder"},
        fields="id,name", supportsAllDrives=True).execute()
    _invalidate_cache()
    return f


def rename(file_id, name):
    svc = _service()
    f = svc.files().update(
        fileId=file_id, body={"name": name},
        fields="id,name", supportsAllDrives=True,
        addParents=None).execute()
    _invalidate_cache()
    return f


def move(file_id, new_parent_id):
    svc = _service()
    new_parent = new_parent_id or _folder_id()
    f = svc.files().get(fileId=file_id, fields="parents",
                        supportsAllDrives=True).execute()
    old = (f.get("parents") or [None])[0]
    f = svc.files().update(
        fileId=file_id, addParents=new_parent,
        removeParents=old or "", body={},
        fields="id,name,parents", supportsAllDrives=True).execute()
    _invalidate_cache()
    return f


def delete(file_id):
    svc = _service()
    try:
        svc.files().delete(fileId=file_id, supportsAllDrives=True).execute()
    except Exception:
        # files shared with an SA/OAuth user may only be trashable
        svc.files().update(fileId=file_id, body={"trashed": True},
                           supportsAllDrives=True).execute()
    _invalidate_cache()
    return {"deleted": file_id}


def delete_library_contents():
    """Trash every file/folder inside the Library folder (fresh start)."""
    svc = _service()
    root = _folder_id()
    res = svc.files().list(
        q="'%s' in parents and trashed=false" % root,
        fields="files(id)", pageSize=1000,
        supportsAllDrives=True).execute()
    n = 0
    for f in res.get("files") or []:
        try:
            delete(f["id"])
            n += 1
        except Exception as e:
            log.warning("Could not delete Drive file %s: %s", f["id"], e)
    _invalidate_cache()
    return n


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
