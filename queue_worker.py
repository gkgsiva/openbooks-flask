"""Background download queue: consumes 'downloads' rows, runs IRC search via
openbooks, picks the best match, downloads with rate-limit gaps between books."""
import os
import threading
import time

import db

BOOKS_DIR = os.path.join(os.environ.get("OPENBOOKS_DOWNLOAD_DIR", "/tmp/openbooks"), "books")
RATE_LIMIT_SECONDS = 3  # pause between queued downloads (respects IRC/openbooks limits)
RATE_LIMIT_RETRY = 15   # wait before retrying an item that was rate-limited
IRC_RETRY_ATTEMPTS = 3
_active_lock = threading.Lock()
_active_job = {"id": None, "phase": "idle", "started": None}
_drive_upload_blocked = threading.Event()


def active_job():
    with _active_lock:
        return dict(_active_job)


def _set_active_job(job_id, phase, started=None):
    with _active_lock:
        _active_job.update({"id": job_id, "phase": phase})
        if started is not None:
            _active_job["started"] = started


def drive_upload_paused():
    return _drive_upload_blocked.is_set()


def _fmt_query(book):
    """Prefer explicit irc_query, else 'title author' built from FK'd rows."""
    if book.get("irc_query"):
        return book["irc_query"]
    parts = [book["title"]]
    if book.get("author_name"):
        parts.append(book["author_name"])
    return " ".join(p for p in parts if p).strip()


FORMAT_RANK = {"epub": 5, "mobi": 4, "azw3": 3, "pdf": 2, "txt": 1, "html": 1, "rtf": 1}


def _norm_tokens(*texts):
    """Lowercase word tokens from one or more strings, minus stopwords."""
    import re
    stop = {"the", "a", "an", "of", "and", "by", "in", "on", "for", "to"}
    out = set()
    for t in texts:
        for w in re.findall(r"[a-z0-9]+", (t or "").lower()):
            if w not in stop and len(w) > 1:
                out.add(w)
    return out


def _match_score(irc, title, author):
    """Title/author token overlap with the requested book (0..3)."""
    want_t = _norm_tokens(title)
    want_a = _norm_tokens(author)
    got_t = _norm_tokens(irc.get("title"))
    got_a = _norm_tokens(irc.get("author"))
    score = 0.0
    if want_t and got_t:
        overlap = len(want_t & got_t) / max(len(want_t), 1)
        score += 2.0 * overlap
        # exact-ish title is a strong signal
        if overlap >= 0.8:
            score += 0.5
    if want_a and got_a:
        score += 1.0 * (len(want_a & got_a) / max(len(want_a), 1))
    return score


def _pick(results, book):
    """Best IRC match: score title/author similarity, format preference, size sanity."""
    irc_books = results.get("books") or []
    if not irc_books:
        return None
    best = None
    best_key = None
    for b in irc_books:
        fmt = (b.get("format") or "").lower().lstrip("~")
        score = FORMAT_RANK.get(fmt, 0) * 2.0
        score += _match_score(b, book.get("title"), book.get("author_name"))
        size = _size_bytes(b.get("size")) or 0
        if size:
            if size < 50 * 1024:
                score -= 2  # tiny: likely corrupt/sample
            elif size < 200 * 1024:
                score -= 0.5
            elif 300 * 1024 <= size <= 30 * 1024 * 1024:
                score += 1  # sane book size
            elif size > 80 * 1024 * 1024:
                score -= 1.5  # huge: likely scanned pdf/omnibus
        if b.get("server"):
            score += 0.25
        # deterministic tie-break on larger size
        key = (round(score, 2), size)
        if best_key is None or key > best_key:
            best, best_key = b, key
    return best


def worker_loop(app_client_getter, stop_event):
    """Main queue consumer. app_client_getter() returns the OpenBooksClient
    (imported lazily to avoid circular imports with app.py)."""
    # one-time recovery: re-queue downloads left in 'error' by a previous run
    # (e.g. old connection bug) so books stuck as 'Queued' in the UI can finish;
    # also re-queue 'running' rows orphaned when a deploy/dyno restart killed
    # the worker mid-download (worker is single-threaded; a real 'running' row
    # can only belong to this process, which starts with none).
    rows = db.query("SELECT id, book_id FROM downloads "
                    "WHERE status IN ('error','running')")
    if rows:
        db.execute("UPDATE downloads SET status='queued', error=NULL, "
                   "started_at=NULL WHERE status IN ('error','running')")
    while not stop_event.is_set():
        if _drive_upload_blocked.is_set():
            stop_event.wait(10)
            continue
        rows = db.query(
            "SELECT d.id dl_id, d.book_id, b.* , 0 AS _x FROM downloads d "
            "JOIN books b ON d.book_id = b.id "
            "WHERE d.status = 'queued' ORDER BY d.id LIMIT 1")
        if not rows:
            stop_event.wait(2)
            continue
        row = rows[0]
        row.pop("_x", None)
        job_started = time.time()
        _set_active_job(row["dl_id"], "connecting", job_started)
        db.execute("UPDATE downloads SET status='running', started_at=? WHERE id=?",
                   (time.time(), row["dl_id"]))
        try:
            client = app_client_getter()

            def _with_irc_retry(fn, *a, **kw):
                # openbooks is single-client: back-to-back downloads can hit a
                # lingering server-side registration -> websocket 400, or a
                # ConnectionRefused while the binary is mid-restart. The app
                # client already survives both; retry here as a safety net
                # with escalating waits instead of failing the queue item.
                import websocket as _ws
                last = None
                for attempt in range(IRC_RETRY_ATTEMPTS):
                    try:
                        return fn(*a, **kw)
                    except (_ws.WebSocketException, ConnectionRefusedError,
                            OSError, RuntimeError) as e:
                        last = e
                        client._close_conn_and_reset()
                        if attempt < IRC_RETRY_ATTEMPTS - 1:
                            stop_event.wait(5 + attempt * 5)
                raise last

            query = _fmt_query(row)
            _set_active_job(row["dl_id"], "searching")
            results = _with_irc_retry(client.search, query)
            err = results.get("error") or ""
            if "rate limited" in err or "frequently" in err:
                # transient; wait longer and retry this item next loop
                db.execute("UPDATE downloads SET status='queued', error=NULL WHERE id=?",
                           (row["dl_id"],))
                stop_event.wait(RATE_LIMIT_RETRY)
                continue
            if results.get("error"):
                raise RuntimeError(results["error"])
            _set_active_job(row["dl_id"], "selecting")
            match = _pick(results, row)
            if not match:
                raise RuntimeError("no IRC results found")
            _set_active_job(row["dl_id"], "downloading")
            dl = _with_irc_retry(client.download, match["full"], _size_bytes(match.get("size")))
            if dl.get("error"):
                raise RuntimeError(dl["error"])
            # After a download the server tears down / re-registers the ws
            # client at an unpredictable moment. Close it deterministically
            # (proper close frame -> server unregisters immediately) so the
            # next queue item gets a clean handshake instead of a 400.
            client._close_conn_and_reset()
            chosen = "%s [%s, %s]" % ((match.get("author") or match.get("title") or "?").strip(),
                                      (match.get("format") or "?").lstrip("~"), match.get("size") or "?")
            # openbooks never sets msg["name"; the saved filename lives in "detail"
            name = dl.get("name") or dl.get("detail") or os.path.basename(match["full"])
            _move_if_needed(name, row["id"])
            gdrive_id = ""
            gdrive_id_len = 0
            gd_note = ""
            try:
                _set_active_job(row["dl_id"], "uploading")
                import gdrive
                if gdrive.enabled():
                    path = os.path.join(BOOKS_DIR, name)
                    if os.path.isfile(path):
                        author = (row.get("author_name") or match.get("author") or "").strip()
                        gdrive_id, gd_size = gdrive.upload(path, name, subfolder=author or None)
                        os.remove(path)  # free ephemeral disk; Drive is the store now
                        gdrive_id_len = gd_size
            except Exception as ge:
                gd_note = "; GDrive upload failed: " + str(ge)[:400]
                if "service accounts have no my drive storage quota" in str(ge).lower():
                    _drive_upload_blocked.set()
            db.execute("UPDATE books SET status='downloaded', filename=?, irc_ref=?, "
                       "gdrive_id=?, size_bytes=?, updated_at=? WHERE id=?",
                       (name, match["full"], gdrive_id,
                        (gdrive_id_len if gdrive_id else
                         (_size_bytes(match.get("size")) or row.get("size_bytes") or 0)),
                        time.time(), row["id"]))
            db.execute("UPDATE downloads SET status='done', finished_at=?, error=? WHERE id=?",
                       (time.time(), "Chosen IRC source: " + chosen + gd_note, row["dl_id"]))
        except Exception as e:
            msg = str(e)[:500]
            db.execute("UPDATE downloads SET status='error', error=?, finished_at=? WHERE id=?",
                       (msg, time.time(), row["dl_id"]))
            # book status stays 'queued' so the user can retry from the UI
        finally:
            _set_active_job(None, "idle")
        time.sleep(RATE_LIMIT_SECONDS)  # respect openbooks frequency


def _size_bytes(text):
    """'394.7KB' -> bytes"""
    import re
    m = re.match(r"([\d.]+)\s*([KMG]?)B?", (text or "").strip(), re.IGNORECASE)
    if not m:
        return None
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[m.group(2).upper()]
    try:
        return int(float(m.group(1)) * mult)
    except ValueError:
        return None


def _move_if_needed(name, book_id):
    """openbooks saves to <dir>/books; keep it there. Hook for GDrive phase 2."""
    return


def start_worker(client_getter):
    stop = threading.Event()
    t = threading.Thread(target=worker_loop, args=(client_getter, stop), daemon=True)
    t.start()
    return stop, t
