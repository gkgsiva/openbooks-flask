"""Flask frontend for the openbooks binary (server mode, WebSocket client)."""
import json
import os
import subprocess
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request
import websocket  # websocket-client

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
        with self.lock:
            self.send(self.send_request(TYPE_SEARCH, {"query": query}))
        # the search response arrives asynchronously; poll until SEARCH result
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.search_event.is_set():
                return self.search_results
            time.sleep(0.2)
        return {"error": "search timed out"}

    def download(self, book):
        self.ensure_connected()
        self.download_event.clear()
        with self.lock:
            self.send(self.send_request(TYPE_DOWNLOAD, {"book": book}))
        deadline = time.time() + 300  # downloads can take minutes
        while time.time() < deadline:
            if self.download_event.is_set():
                return self.last_download
            time.sleep(0.5)
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
            elif mtype == TYPE_DOWNLOAD and "name" in msg:
                self.last_download = {"name": msg["name"], "path": msg.get("downloadPath")}
                self.download_event.set()
            elif mtype == TYPE_RATELIMIT:
                self.search_results = {"error": "rate limited: " + msg.get("title", "")}
                self.search_event.set()
            # STATUS / other messages are informational


client = OpenBooksClient()
client.start()

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


@app.route("/api/download", methods=["POST"])
def api_download():
    book = (request.json or {}).get("book", "").strip()
    if not book:
        return jsonify({"error": "book required"}), 400
    start_openbooks_subprocess()
    return jsonify(client.download(book))


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
