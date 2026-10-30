"""Library creator API: catalog search, add, queue, edit, delete, import."""
import os
import time

from flask import Blueprint, jsonify, request, send_file

import db
import metadata
import queue_worker

bp = Blueprint("library", __name__, url_prefix="/api/library")

BOOKS_DIR = os.path.join(os.environ.get("OPENBOOKS_DOWNLOAD_DIR", "/tmp/openbooks"), "books")


def _now():
    return time.time()


def _get_or_create(table, name, extra_sql="", extra_params=()):
    """Find row by name or create it. Returns id (None if name empty)."""
    name = (name or "").strip()
    if not name:
        return None
    rows = db.query(f"SELECT id FROM {table} WHERE name = ?", (name,))
    if rows:
        return rows[0]["id"]
    extra_cols = f", {extra_sql}" if extra_sql else ""
    extra_vals = f", {', '.join(['?'] * len(extra_params))}" if extra_params else ""
    return db.execute(
        f"INSERT INTO {table} (name, created_at{extra_cols}) VALUES (?, ?{extra_vals})",
        (name, _now(), *extra_params))


def _book_row(bid):
    rows = db.query(
        "SELECT b.*, a.name author_name, s.name series_name, "
        "(SELECT status FROM downloads d WHERE d.book_id = b.id "
        " ORDER BY d.id DESC LIMIT 1) AS queue_status "
        "FROM books b LEFT JOIN authors a ON b.author_id = a.id "
        "LEFT JOIN series s ON b.series_id = s.id WHERE b.id = ?", (bid,))
    return rows[0] if rows else None


def queue_books(book_ids):
    n = 0
    for bid in book_ids:
        if not bid:
            continue
        db.execute("INSERT INTO downloads (book_id, status, created_at) VALUES (?,?,?)",
                   (bid, "queued", _now()))
        db.execute("UPDATE books SET status='queued' WHERE id=?", (bid,))
        n += 1
    return n


# --- metadata search from external APIs (Google Books / OpenLibrary) --------

@bp.route("/search", methods=["POST"])
def lib_search():
    body = request.json or {}
    q = (body.get("q") or "").strip()
    stype = body.get("type", "book")
    source = body.get("source", "hardcover")  # hardcover | google | openlibrary | both
    if not q:
        return jsonify({"error": "q required"}), 400
    if stype == "author":
        q2 = f'inauthor:"{q}"'    # Google Books author-bibliography operator
        ol_q = f'author:"{q}"'    # OpenLibrary operator
        hc_q = q                   # Hardcover searches names directly
    elif stype == "series":
        q2, ol_q = q, q  # no series operator exists on either API
        hc_q = q
    else:
        q2, ol_q, hc_q = q, q, q
    if source == "hardcover":
        if stype == "author":
            results = metadata.hardcover_author(hc_q)
        elif stype == "series":
            results = metadata.hardcover_series(hc_q)
        else:
            results = metadata.hardcover(hc_q, limit=40)
        if not results:  # token missing or API error -> fall back
            results = metadata.search_all(q2, limit=40, ol_q=ol_q)
    elif source == "google":
        results = metadata.google_books(q2, limit=40)
        missing = [b for b in results if b.get("isbn")][:25]
        lookup = metadata.openlib_series_lookup([b["isbn"] for b in missing]) if missing else {}
        for b in missing:
            s = lookup.get(b["isbn"])
            if s:
                b["series"], b["series_num"] = s["series"], s["series_num"]
    elif source == "openlibrary":
        results = metadata.openlibrary(ol_q, limit=40)
    else:
        results = metadata.search_all(q2, limit=40, ol_q=ol_q)
    return jsonify({"results": results})


# --- add books to catalog (creates authors/series) ---------------------------

@bp.route("/add", methods=["POST"])
def lib_add():
    body = request.json or {}
    books = body.get("books") or []
    if not books:
        return jsonify({"error": "books required"}), 400
    added = []
    for b in books:
        author_id = _get_or_create("authors", b.get("author"))
        series_id = None
        if b.get("series"):
            series_id = _get_or_create("series", b["series"],
                                       extra_sql="author_id", extra_params=(author_id,))
        isbn = b.get("isbn") or ""
        # IS NOT DISTINCT FROM semantics so NULL author_id matches NULL
        # (plain `= ?` fails on NULL/NULL; `IS ?` is invalid in Postgres)
        existing = db.query(
            "SELECT id FROM books WHERE title = ? "
            f"AND author_id {'IS' if author_id is None else '='} ?",
            (b.get("title", ""), author_id))
        if existing:
            book_id = existing[0]["id"]
            if isbn:
                db.execute("UPDATE books SET isbn=? WHERE id=?", (isbn, book_id))
        else:
            book_id = db.execute(
                "INSERT INTO books (title, author_id, series_id, series_num, year, isbn, "
                "language, format, status, cover_url, irc_query, source, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (b.get("title"), author_id, series_id, b.get("series_num"),
                 b.get("year"), isbn, b.get("language") or "", b.get("format") or "",
                 "pending", b.get("cover_url") or "",
                 f'{b.get("title","")} {b.get("author","")}'.strip(),
                 b.get("source", "manual"), _now(), _now()))
        added.append(book_id)
    if body.get("download"):
        queue_books(added)
    return jsonify({"added": added, "queued": len(added) if body.get("download") else 0})


# --- queue management --------------------------------------------------------

@bp.route("/queue", methods=["POST"])
def lib_queue():
    ids = (request.json or {}).get("book_ids") or []
    if not ids:
        return jsonify({"error": "book_ids required"}), 400
    return jsonify({"queued": queue_books(ids)})


@bp.route("/queue/status")
def lib_queue_status():
    rows = db.query(
        "SELECT d.id, d.status, d.error, d.book_id, b.title, b.status book_status, "
        "a.name author_name FROM downloads d JOIN books b ON d.book_id=b.id "
        "LEFT JOIN authors a ON b.author_id=a.id ORDER BY d.id DESC LIMIT 50")
    # merge live progress from the openbooks client (download %) when a
    # queued item is actually running
    try:
        import app as _app
        d = dict(_app.active_download)
        if d.get("state") == "downloading":
            pct = _app.download_percent(d)
            for r in rows:
                if r["status"] == "running":
                    r["status"] = "downloading"
                    r["progress"] = pct
        else:
            # worker is searching IRC / picking best match; surface that
            for r in rows:
                if r["status"] == "running":
                    r["status"] = "searching"
        if d.get("state") == "timeout":
            for r in rows:
                if r["status"] == "running":
                    r["status"] = "error"
                    r["error"] = "download timed out (still running server-side)"
    except Exception:
        pass
    return jsonify(rows)


# --- authors / series with summaries -----------------------------------------

@bp.route("/authors")
def lib_authors():
    return jsonify(db.query(
        "SELECT a.id, a.name, COUNT(b.id) total, "
        "SUM(CASE WHEN b.status='downloaded' THEN 1 ELSE 0 END) downloaded, "
        "SUM(CASE WHEN b.status!='downloaded' THEN 1 ELSE 0 END) pending "
        "FROM authors a LEFT JOIN books b ON b.author_id=a.id "
        "GROUP BY a.id, a.name ORDER BY a.name"))


@bp.route("/series")
def lib_series():
    return jsonify(db.query(
        "SELECT s.id, s.name, a.name author_name, COUNT(b.id) total, "
        "SUM(CASE WHEN b.status='downloaded' THEN 1 ELSE 0 END) downloaded, "
        "SUM(CASE WHEN b.status!='downloaded' THEN 1 ELSE 0 END) pending "
        "FROM series s LEFT JOIN books b ON b.series_id=s.id "
        "LEFT JOIN authors a ON s.author_id=a.id "
        "GROUP BY s.id, s.name, a.name ORDER BY s.name"))


def _books_list(where, params):
    return db.query(
        "SELECT b.*, a.name author_name, s.name series_name, "
        "(SELECT status FROM downloads d WHERE d.book_id = b.id "
        " ORDER BY d.id DESC LIMIT 1) AS queue_status "
        "FROM books b LEFT JOIN authors a ON b.author_id = a.id "
        "LEFT JOIN series s ON b.series_id = s.id WHERE " + where +
        " ORDER BY COALESCE(a.name,''), s.name, b.series_num, b.title", params)


@bp.route("/authors/<int:aid>")
def lib_author(aid):
    return jsonify(_books_list("b.author_id = ?", (aid,)))


@bp.route("/series/<int:sid>")
def lib_series_books(sid):
    return jsonify(_books_list("b.series_id = ?", (sid,)))


@bp.route("/books")
def lib_books():
    return jsonify(_books_list("1=1", ()))


# --- edit / delete -----------------------------------------------------------

@bp.route("/books/<int:bid>", methods=["GET"])
def lib_get_book(bid):
    row = _book_row(bid)
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row)


@bp.route("/books/<int:bid>", methods=["PATCH"])
def lib_edit_book(bid):
    body = request.json or {}
    fields, params = [], []
    for col in ("title", "year", "isbn", "language", "series_num", "cover_url", "irc_query"):
        if col in body:
            fields.append(f"{col} = ?")
            params.append(body[col])
    if "author" in body:
        fields.append("author_id = ?")
        params.append(_get_or_create("authors", body["author"]))
    if "series" in body:
        if body["series"]:
            fields.append("series_id = ?")
            params.append(_get_or_create("series", body["series"]))
        else:
            fields.append("series_id = NULL")
    if "status" in body and body["status"] in ("pending", "downloaded", "queued"):
        fields.append("status = ?")
        params.append(body["status"])
    if not fields:
        return jsonify({"error": "nothing to update"}), 400
    fields.append("updated_at = ?")
    params.append(_now())
    params.append(bid)
    db.execute(f"UPDATE books SET {', '.join(fields)} WHERE id = ?", params)
    return jsonify(_book_row(bid))


@bp.route("/books/<int:bid>", methods=["DELETE"])
def lib_delete_book(bid):
    row = db.query("SELECT filename FROM books WHERE id=?", (bid,))
    if not row:
        return jsonify({"error": "not found"}), 404
    if row[0]["filename"]:
        try:
            os.remove(os.path.join(BOOKS_DIR, row[0]["filename"]))
        except OSError:
            pass
    db.execute("DELETE FROM downloads WHERE book_id=?", (bid,))
    db.execute("DELETE FROM books WHERE id=?", (bid,))
    return jsonify({"deleted": bid})


@bp.route("/books/<int:bid>/file")
def lib_book_file(bid):
    row = db.query("SELECT filename FROM books WHERE id=?", (bid,))
    if not row or not row[0]["filename"]:
        return jsonify({"error": "no file"}), 404
    return send_file(os.path.join(BOOKS_DIR, row[0]["filename"]), as_attachment=True)


# --- summary ------------------------------------------------------------------

@bp.route("/summary")
def lib_summary():
    tot = db.query("SELECT COUNT(*) c FROM books")[0]["c"]
    dl = db.query("SELECT COUNT(*) c FROM books WHERE status='downloaded'")[0]["c"]
    qd = db.query(
        "SELECT COUNT(*) c FROM downloads WHERE status IN ('queued','running')")[0]["c"]
    return jsonify({"total_books": tot, "downloaded": dl,
                    "pending": tot - dl, "in_queue": qd})


# --- import external files ------------------------------------------------------

@bp.route("/import", methods=["POST"])
def lib_import():
    body = request.json or {}
    filename = (body.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename required"}), 400
    path = os.path.join(BOOKS_DIR, filename)
    if not os.path.isfile(path):
        return jsonify({"error": f"file not found in books dir: {filename}"}), 404
    title = body.get("title") or os.path.splitext(filename)[0].replace("_", " ").title()
    author_id = _get_or_create("authors", body.get("author"))
    series_id = None
    if body.get("series"):
        series_id = _get_or_create("series", body["series"],
                                   extra_sql="author_id", extra_params=(author_id,))
    size = os.path.getsize(path)
    book_id = db.execute(
        "INSERT INTO books (title, author_id, series_id, series_num, year, status, "
        "filename, size_bytes, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (title, author_id, series_id, body.get("series_num"), body.get("year"),
         "downloaded", filename, size, "imported", _now(), _now()))
    return jsonify({"imported": book_id})


@bp.route("/files")
def lib_files():
    """Files in the books dir not yet imported (for the import picker)."""
    imported = {r["filename"] for r in db.query(
        "SELECT filename FROM books WHERE filename != ''")}
    out = []
    if os.path.isdir(BOOKS_DIR):
        for name in sorted(os.listdir(BOOKS_DIR)):
            if name.startswith(".") or name.endswith(".temp") or name in imported:
                continue
            out.append(name)
    return jsonify(out)


# --- authors/series CRUD --------------------------------------------------------

@bp.route("/authors/<int:aid>", methods=["PATCH"])
def lib_edit_author(aid):
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    db.execute("UPDATE authors SET name=? WHERE id=?", (name, aid))
    return jsonify({"updated": aid})


@bp.route("/authors/<int:aid>", methods=["DELETE"])
def lib_delete_author(aid):
    db.execute("UPDATE books SET author_id=NULL WHERE author_id=?", (aid,))
    db.execute("DELETE FROM authors WHERE id=?", (aid,))
    return jsonify({"deleted": aid})


@bp.route("/series/<int:sid>", methods=["PATCH"])
def lib_edit_series(sid):
    body = request.json or {}
    if "name" in body and body["name"].strip():
        db.execute("UPDATE series SET name=? WHERE id=?", (body["name"].strip(), sid))
    if "author" in body and body["author"].strip():
        db.execute("UPDATE series SET author_id=? WHERE id=?",
                   (_get_or_create("authors", body["author"]), sid))
    return jsonify({"updated": sid})


@bp.route("/series/<int:sid>", methods=["DELETE"])
def lib_delete_series(sid):
    db.execute("UPDATE books SET series_id=NULL WHERE series_id=?", (sid,))
    db.execute("DELETE FROM series WHERE id=?", (sid,))
    return jsonify({"deleted": sid})
