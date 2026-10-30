"""Flask frontend for the openbooks binary (server mode, WebSocket client)."""
import json
import os
import subprocess
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request
import websocket  # websocket-client

import db
import library
import queue_worker

OPENBOOKS_BINARY = os.environ.get("OPENBOOKS_BINARY", os.path.join(os.path.dirname(__file__), "openbooks"))
OPENBOOKS_URL = os.environ.get("OPENBOOKS_URL", "ws://127.0.0.1:8080/ws")
OPENBOOKS_PORT = os.environ.get("OPENBOOKS_PORT", "8080")  # internal port of the openbooks binary
FLASK_PORT = os.environ.get("PORT", "5000")  # Heroku sets PORT
OPENBOOKS_NAME = os.environ.get("OPENBOOKS_NAME", f"user{uuid.uuid4().hex[:6]}")
OPENBOOKS_IRC_SERVER = os.environ.get("OPENBOOKS_IRC_SERVER", "irc.irchighway.net:6697")
OPENBOOKS_TLS = os.environ.get("OPENBOOKS_TLS", "true")
DOWNLOAD_DIR = os.environ.get("OPENBOOKS_DOWNLOAD_DIR", "/tmp/openbooks")

# Message types (server/messages.go): 0 STATUS, 1 CONNECT, 2 SEARCH, 3 DOWNLOAD, 4 RATELIMIT
TYPE_STATUS, TYPE_CONNECT, TYPE_SEARCH, TYPE_DOWNLOAD, TYPE_RATELIMIT = 0, 1, 2, 3, 4

app = Flask(__name__)

# ---------------------------------------------------------------------------
# OpenBooks process + websocket bridge
# ---------------------------------------------------------------------------
_proc = None  # openbooks subprocess handle


def start_openbooks_subprocess():
    """Launch the openbooks binary in server mode (idempotent)."""
    global _proc
    if _proc is not None and _proc.poll() is None:
        return _proc
    _proc = subprocess.Popen(
        [OPENBOOKS_BINARY, "server", "--log", "--persist", "--server", OPENBOOKS_IRC_SERVER,
         "--tls=" + OPENBOOKS_TLS,  # Go boolean flag: must use "=" syntax
         "--name", OPENBOOKS_NAME, "--port", OPENBOOKS_PORT, "--dir", DOWNLOAD_DIR],
        stdout=None,  # inherit: openbooks logs flow to Heroku's log stream
    )
    # wait for the HTTP server to come up
    import urllib.request
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:8080/servers", timeout=1)
            return _proc
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("openbooks binary did not start; check /tmp/openbooks_flask.log")


def _find_temp_bytes():
    """Size of the in-progress *.temp file openbooks is writing, if any."""
    books_dir = os.path.join(DOWNLOAD_DIR, "books")
    if not os.path.isdir(books_dir):
        return 0
    total = 0
    for name in os.listdir(books_dir):
        if name.endswith(".temp"):
            try:
                total += os.path.getsize(os.path.join(books_dir, name))
            except OSError:
                pass
    return total


def _parse_size(text):
    """'394.7KB' / '1.2MB' -> bytes, or None."""
    import re
    m = re.match(r"([\d.]+)\s*([KMG]?)B?", (text or "").strip(), re.IGNORECASE)
    if not m:
        return None
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[m.group(2).upper()]
    try:
        return int(float(m.group(1)) * mult)
    except ValueError:
        return None


DOWNLOAD_TIMEOUT = 300  # seconds, must match client.download deadline
SEARCH_TIMEOUT = 60     # seconds, must match client.search deadline
active_download = {"state": "idle"}  # shared progress info for the UI
active_search = {"state": "idle"}


class OpenBooksClient(threading.Thread):
    """Maintains one websocket connection to the openbooks server and
    multiplexes search/download results to waiting request threads via events."""

    def __init__(self):
        super().__init__(daemon=True)
        self.conn = None
        self.lock = threading.Lock()
        self.connected_event = threading.Event()
        self.connected_name = None
        self.search_results = None    # set by SEARCH response
        self.search_event = threading.Event()
        self.last_download = None      # (name, path) from DOWNLOAD response
        self.download_event = threading.Event()
        self.status = []
        self.name = None

    def connect(self):
        self.conn = websocket.create_connection(OPENBOOKS_URL, timeout=10)
        # Drop the 10s connect timeout so idle recv() doesn't tear down
        # a healthy connection between user searches.
        self.conn.settimeout(None)
        # CONNECT response is dispatched by the reader thread (run());
        # this method only waits on the event it sets. Never recv() here —
        # concurrent recv() calls from two threads are undefined behavior.
        self.connected_event = threading.Event()
        self.send({"type": TYPE_CONNECT, "payload": {}})
        if not self.connected_event.wait(timeout=30):
            self.conn = None
            raise RuntimeError("openbooks did not confirm the websocket connection (CONNECT timeout)")
        self.name = self.connected_name

    def ensure_connected(self):
        with self.lock:
            if self.conn is None:
                self.connect()

    def send(self, obj):
        self.conn.send(json.dumps(obj))

    def send_request(self, request_type, payload):
        # payload must be a JSON object (Go uses json.RawMessage), not a string
        return {"type": request_type, "payload": payload}

    def search(self, query):
        self.ensure_connected()
        self.search_event.clear()
        started = time.time()
        active_search.update({"state": "searching", "started": started})
        with self.lock:
            self.send(self.send_request(TYPE_SEARCH, {"query": query}))
        # the search response arrives asynchronously; poll until SEARCH result
        deadline = started + SEARCH_TIMEOUT
        while time.time() < deadline:
            if self.search_event.is_set():
                active_search.update({"state": "done"})
                return self.search_results
            time.sleep(0.2)
        active_search.update({"state": "timeout"})
        return {"error": "search timed out"}

    def download(self, book, size_bytes=None):
        self.ensure_connected()
        self.download_event.clear()
        with self.lock:
            self.send(self.send_request(TYPE_DOWNLOAD, {"book": book}))
        started = time.time()
        active_download.update({
            "state": "downloading", "started": started,
            "size": size_bytes or None,
        })
        deadline = started + DOWNLOAD_TIMEOUT  # downloads can take minutes
        while time.time() < deadline:
            if self.download_event.is_set():
                active_download.update({"state": "done"})
                return self.last_download
            time.sleep(0.5)
        active_download.update({"state": "timeout"})
        return {"error": "download timed out"}

    def run(self):
        """Background reader: dispatches incoming websocket messages."""
        while True:
            try:
                if self.conn is None:
                    time.sleep(0.5)
                    continue
                msg = json.loads(self.conn.recv())
            except Exception:
                self.conn = None
                time.sleep(1)
                continue

            mtype = msg.get("type")
            if mtype == TYPE_CONNECT:
                self.connected_name = msg.get("name")
                self.connected_event.set()
            elif mtype == TYPE_SEARCH and "books" in msg:
                self.search_results = {"books": msg["books"], "errors": msg.get("errors", [])}
                self.search_event.set()
            elif mtype == TYPE_DOWNLOAD:
                self.last_download = {
                    "name": msg.get("name") or "",
                    "detail": msg.get("detail"),
                    "path": msg.get("downloadPath"),
                }
                self.download_event.set()
            elif mtype == TYPE_RATELIMIT:
                self.search_results = {"error": "rate limited: " + msg.get("title", "")}
                self.search_event.set()
            # STATUS / other messages are informational


client = OpenBooksClient()
client.start()

db.init_db()
app.register_blueprint(library.bp)
queue_worker.start_worker(lambda: client)

# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search", methods=["POST"])
def api_search():
    query = (request.json or {}).get("query", "").strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    start_openbooks_subprocess()
    return jsonify(client.search(query))


@app.route("/api/search/status")
def api_search_status():
    d = dict(active_search)
    state = d.get("state", "idle")
    if state in ("done", "timeout"):
        return jsonify({"state": "idle" if state != "timeout" else "timeout",
                        "percent": 100 if state == "timeout" else 0})
    elapsed = time.time() - d.get("started", time.time())
    percent = min(99, int(elapsed * 100 / SEARCH_TIMEOUT))
    return jsonify({"state": state, "percent": percent, "elapsed": round(elapsed, 1)})


@app.route("/api/download", methods=["POST"])
def api_download():
    body = request.json or {}
    book = body.get("book", "").strip()
    if not book:
        return jsonify({"error": "book required"}), 400
    start_openbooks_subprocess()
    return jsonify(client.download(book, _parse_size(body.get("size"))))


@app.route("/api/download/status")
def api_download_status():
    """Progress for the in-flight download: real % from .temp bytes when size is
    known, otherwise time-elapsed relative to the 300s timeout."""
    d = dict(active_download)
    state = d.get("state", "idle")
    if state not in ("downloading", "timeout"):
        return jsonify({"state": "idle" if state == "idle" else state, "percent": 0})

    elapsed = time.time() - d.get("started", time.time())
    size = d.get("size")
    done_bytes = _find_temp_bytes() if state == "downloading" else None
    if state == "timeout":
        percent = 100
    elif size and done_bytes is not None:
        # byte-based progress; cap by bytes and don't let it stall at ~99 forever
        percent = min(99, int(done_bytes * 100 / max(size, 1)))
        if size and done_bytes >= size:
            percent = 99  # transfer done; server still extracting/renaming
        # blend with time so the bar keeps moving when size is unknown/off
        time_pct = min(99, int(elapsed * 100 / DOWNLOAD_TIMEOUT))
        percent = max(percent, min(time_pct, 90))
    else:
        percent = min(99, int(elapsed * 100 / DOWNLOAD_TIMEOUT))
    return jsonify({
        "state": state,
        "percent": percent,
        "elapsed": round(elapsed, 1),
        "bytes": done_bytes,
        "size": size,
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
    start_openbooks_subprocess()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
