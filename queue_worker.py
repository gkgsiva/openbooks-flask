"""Background download queue: consumes 'downloads' rows, runs IRC search via
openbooks, picks the best match, downloads with rate-limit gaps between books."""
import os
import threading
import time

import db

BOOKS_DIR = os.path.join(os.environ.get("OPENBOOKS_DOWNLOAD_DIR", "/tmp/openbooks"), "books")
RATE_LIMIT_SECONDS = 3  # pause between queued downloads (respects IRC/openbooks limits)
RATE_LIMIT_RETRY = 15   # wait before retrying an item that was rate-limited


def _fmt_query(book):
    """Prefer explicit irc_query, else 'title author' built from FK'd rows."""
    if book.get("irc_query"):
        return book["irc_query"]
    parts = [book["title"]]
    if book.get("author_name"):
        parts.append(book["author_name"])
    return " ".join(p for p in parts if p).strip()


def _pick(results, book):
    """Best IRC match: prefer epub over other formats."""
    irc_books = results.get("books") or []
    if not irc_books:
        return None
    scored = []
    for b in irc_books:
        score = 2 if (b.get("format") or "").lower() == "epub" else 0
        score += 1 if b.get("server") else 0
        scored.append((score, b))
    scored.sort(key=lambda t: -t[0])
    return scored[0][1]


def worker_loop(app_client_getter, stop_event):
    """Main queue consumer. app_client_getter() returns the OpenBooksClient
    (imported lazily to avoid circular imports with app.py)."""
    while not stop_event.is_set():
        rows = db.query(
            "SELECT d.id dl_id, d.book_id, b.* , 0 AS _x FROM downloads d "
            "JOIN books b ON d.book_id = b.id "
            "WHERE d.status = 'queued' ORDER BY d.id LIMIT 1")
        if not rows:
            stop_event.wait(2)
            continue
        row = rows[0]
        row.pop("_x", None)
        db.execute("UPDATE downloads SET status='running', started_at=? WHERE id=?",
                   (time.time(), row["dl_id"]))
        try:
            client = app_client_getter()
            query = _fmt_query(row)
            results = client.search(query)
            err = results.get("error") or ""
            if "rate limited" in err or "frequently" in err:
                # transient; wait longer and retry this item next loop
                db.execute("UPDATE downloads SET status='queued', error=NULL WHERE id=?",
                           (row["dl_id"],))
                stop_event.wait(RATE_LIMIT_RETRY)
                continue
            if results.get("error"):
                raise RuntimeError(results["error"])
            match = _pick(results, row)
            if not match:
                raise RuntimeError("no IRC results found")
            dl = client.download(match["full"], _size_bytes(match.get("size")))
            if dl.get("error"):
                raise RuntimeError(dl["error"])
            # openbooks never sets msg["name"; the saved filename lives in "detail"
            name = dl.get("name") or dl.get("detail") or os.path.basename(match["full"])
            _move_if_needed(name, row["id"])
            db.execute("UPDATE books SET status='downloaded', filename=?, irc_ref=?, "
                       "size_bytes=?, updated_at=? WHERE id=?",
                       (name, match["full"], _size_bytes(match.get("size")) or row.get("size_bytes") or 0,
                        time.time(), row["id"]))
            db.execute("UPDATE downloads SET status='done', finished_at=? WHERE id=?",
                       (time.time(), row["dl_id"]))
        except Exception as e:
            msg = str(e)[:500]
            db.execute("UPDATE downloads SET status='error', error=?, finished_at=? WHERE id=?",
                       (msg, time.time(), row["dl_id"]))
            # book status stays 'queued' so the user can retry from the UI
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
