"""Flask frontend: native Python IRC book client (irc.irchighway.net #ebooks)
with library catalog + download queue + Google Drive uploads."""
import json
import os
import secrets
import threading
import time
from datetime import timedelta

from flask import Flask, jsonify, redirect, render_template, request, url_for

import db
import irc_client
import library
import opds
import queue_worker
import auth

DOWNLOAD_DIR = os.environ.get("OPENBOOKS_DOWNLOAD_DIR", "/tmp/openbooks")

DOWNLOAD_TIMEOUT = 300  # seconds, must match client.download deadline
SEARCH_TIMEOUT = 90     # seconds, must match client.search deadline
CONNECT_LOCK_TIMEOUT = 10
active_download = {"state": "idle"}  # shared progress info for the UI
active_search = {"state": "idle"}

app = Flask(__name__)
# Stable secret across dynos/restarts; env var on Heroku.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
if not os.environ.get("SECRET_KEY"):
    app.logger.warning("SECRET_KEY not set - sessions reset on restart")
app.permanent_session_lifetime = timedelta(hours=24 * 7)

client = irc_client.IrcBookClient(download_dir=os.path.join(DOWNLOAD_DIR, "books"))


def _client_search(query):
    active_search.update({"state": "connecting", "started": time.time()})
    try:
        client.ensure_connected()
        active_search.update({"state": "searching", "started": time.time()})
        res = client.search(query)
        state = "done" if not res.get("error") else "error"
        active_search.update({"state": state})
        return res
    except Exception:
        active_search.update({"state": "error"})
        raise


def _client_download(book, size_bytes):
    client.ensure_connected()
    started = time.time()
    active_download.update({"state": "downloading", "started": started,
                            "size": size_bytes or None})
    res = client.download(book, size_bytes)
    active_download.update({"state": "done"})
    return res


db.init_db()
auth.ensure_admin()
app.register_blueprint(auth.bp)
app.register_blueprint(library.bp)
app.register_blueprint(opds.bp)
queue_worker.start_worker(lambda: client)


# ---------------------------------------------------------------------------
# Global auth gate
# ---------------------------------------------------------------------------
OPEN_PATHS = ("/login", "/logout", "/static", "/favicon.ico")


@app.before_request
def _auth_gate():
    p = request.path
    if any(p == x or p.startswith(x + "/") for x in OPEN_PATHS):
        return None
    # Admin session unlocks everything (including OPDS).
    if auth.is_admin():
        return None
    # OPDS: admin OR regular user via HTTP Basic auth (e-reader clients).
    if p == "/opds" or p.startswith("/opds/"):
        if auth.opds_allowed():
            return None
        # no/invalid credentials: challenge for Basic auth
        resp = jsonify({"error": "authentication required"})
        resp.status_code = 401
        resp.headers["WWW-Authenticate"] = 'Basic realm="open-library OPDS"'
        return resp
    # Everything else (web UI + APIs): admin only.
    if p.startswith("/api/") or p.startswith("/books") or request.is_json:
        return jsonify({"error": "admin login required"}), 401
    return redirect(url_for("auth.login_page", next=p))

# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    """IRC connection health for the dashboard."""
    try:
        client.ensure_connected()
        return jsonify({"connected": True, "nick": client.nick or ""})
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)}), 502


@app.route("/api/search", methods=["POST"])
def api_search():
    query = (request.json or {}).get("query", "").strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    return jsonify(_client_search(query))


@app.route("/api/search/status")
def api_search_status():
    d = dict(active_search)
    state = d.get("state", "idle")
    if state in ("done", "timeout", "error"):
        return jsonify({"state": "idle" if state == "done" else state,
                        "percent": 100 if state in ("timeout", "error") else 0})
    elapsed = time.time() - d.get("started", time.time())
    percent = min(99, int(elapsed * 100 / SEARCH_TIMEOUT))
    return jsonify({"state": state, "percent": percent, "elapsed": round(elapsed, 1)})


@app.route("/api/download", methods=["POST"])
def api_download():
    body = request.json or {}
    book = body.get("book", "").strip()
    if not book:
        return jsonify({"error": "book required"}), 400
    try:
        return jsonify(_client_download(book, _parse_size(body.get("size"))))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


def download_percent(d):
    """0-99 progress for an active/timeout download dict from active_download."""
    elapsed = time.time() - d.get("started", time.time())
    state = d.get("state")
    if state == "timeout":
        return 100
    size = d.get("size")
    done_bytes = _find_temp_bytes() if state == "downloading" else None
    if size and done_bytes is not None:
        # byte-based progress; cap by bytes and don't let it stall at ~99 forever
        percent = min(99, int(done_bytes * 100 / max(size, 1)))
        if size and done_bytes >= size:
            percent = 99  # transfer done; server still extracting/renaming
        # blend with time so the bar keeps moving when size is unknown/off
        time_pct = min(99, int(elapsed * 100 / DOWNLOAD_TIMEOUT))
        percent = max(percent, min(time_pct, 90))
    else:
        percent = min(99, int(elapsed * 100 / DOWNLOAD_TIMEOUT))
    return percent


@app.route("/api/download/status")
def api_download_status():
    """Progress for the in-flight download: real % from .temp bytes when size is
    known, otherwise time-elapsed relative to the 300s timeout."""
    d = dict(active_download)
    state = d.get("state", "idle")
    if state not in ("downloading", "timeout"):
        return jsonify({"state": "idle" if state == "idle" else state, "percent": 0})
    percent = download_percent(d)
    return jsonify({
        "state": state,
        "percent": percent,
        "elapsed": round(time.time() - d.get("started", time.time()), 1),
        "bytes": _find_temp_bytes() if state == "downloading" else None,
        "size": d.get("size"),
    })


@app.route("/api/books")
def api_book_list():
    """List completed books in the download directory."""
    books_dir = os.path.join(DOWNLOAD_DIR, "books")
    if not os.path.isdir(books_dir):
        return jsonify([])
    books = []
    for name in os.listdir(books_dir):
        if name.startswith(".") or name.endswith(".temp"):
            continue
        books.append({"name": name, "url": f"/api/books/{name}"})
    return jsonify(books)


@app.route("/api/books/<path:name>")
def api_book_file(name):
    return flask_send_file(os.path.join(DOWNLOAD_DIR, "books", name))


def flask_send_file(path):
    from flask import send_file
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
