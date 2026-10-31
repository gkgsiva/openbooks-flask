"""Native IRC client for irc.irchighway.net #ebooks — a Python port of the
openbooks binary's IRC core (irc/irc.go, core/reader.go, core/irchighway.go,
core/search_parser.go, dcc/dcc.go).

Talks directly to the IRC server over TLS with a plain socket and a reader
thread — no openbooks subprocess, no websocket bridge, no single-client 400
handshakes. The public interface mirrors the app's OpenBooksClient so this is
a drop-in replacement:

    client = IrcBookClient()
    results = client.search("harry potter rowling")   # {"books": [...], "errors": []}
    dl = client.download("!Server !Author Title.ext", size_bytes)
    # -> {"name": ..., "path": ..., "detail": ...} or {"error": ...}
"""
import os
import re
import socket
import ssl
import struct
import threading
import time

DOWNLOAD_DIR = os.environ.get("OPENBOOKS_DOWNLOAD_DIR", "/tmp/openbooks")
BOOKS_DIR = os.path.join(DOWNLOAD_DIR, "books")
IRCHIGHWAY = os.environ.get("OPENBOOKS_IRC_SERVER", "irc.irchighway.net:6697")
IRCHIGHWAY_TLS = os.environ.get("OPENBOOKS_TLS", "true").lower() == "true"

# Search channel must be joined before @search queries work
IRC_HOST, IRC_PORT = IRCHIGHWAY.rsplit(":", 1)
IRC_PORT = int(IRC_PORT)

_VERSION_REPLY = "openbooks 4.5.0"

# Protocol identification snippets (mirrored from openbooks core/reader.go)
SEND_MESSAGE = "DCC SEND"
NOTICE_MESSAGE = "NOTICE"
NO_RESULTS = "Sorry"
SERVER_UNAVAILABLE = "try another server"
SEARCH_ACCEPTED = "has been accepted"
SEARCH_RESULT_IDENTIFIER = "_results_for_"
NUM_MATCHES = "matches"
VERSION_INQUIRY = "\x01VERSION\x01"

# Rate limit notice from the search bot (true throttling, not simple acceptance).
RATE_LIMIT_PATTERNS = (
    "search in progress",
    "slower pace",
    "try again in",
)

# File extensions returned in search results (openbooks core/search_parser.go)
FILE_TYPES = [
    "epub", "mobi", "azw3", "html", "rtf", "pdf", "cdr", "lit",
    "cbr", "doc", "htm", "jpg", "txt",
    "rar", "zip",  # archive formats last; real format in () e.g. (.mobi).rar
]

# openbooks uses a random guest-style nick; use similar to avoid collisions.
_NICK_RE = re.compile(r"[^A-Za-z0-9_]")

SEARCH_TIMEOUT = float(os.environ.get("IRC_SEARCH_TIMEOUT", 90))
DOWNLOAD_TIMEOUT = float(os.environ.get("IRC_DOWNLOAD_TIMEOUT", 300))
CONNECT_TIMEOUT = 30
DCC_READ_SIZE = 4096


def _random_nick():
    import uuid
    return "ob_" + uuid.uuid4().hex[:10]


def _chat_unescape(s):
    """Decode IRC low-quotes (CTCP \\020 quoting) minimally: \\020n -> newline."""
    return s.replace("\x20n", "\n") if "\\x20n" in s else s


class IrcBookClient:
    def __init__(self, host=None, port=None, use_tls=None, download_dir=None):
        self.host = host or IRC_HOST
        self.port = port if port is not None else IRC_PORT
        self.use_tls = use_tls if use_tls is not None else IRCHIGHWAY_TLS
        self.books_dir = download_dir or BOOKS_DIR
        self.sock = None
        self.reader_thread = None
        self.error = None  # reason the connection died, if it did
        self._rx_lock = threading.Lock()          # one writer to socket at a time
        self._send_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._connected_event = threading.Event()
        self.nick = None
        self.last_search_results = None
        self.last_search_dcc = None
        self.last_download_dcc = None
        # events fired by the reader thread
        self.search_event = threading.Event()
        self.download_event = threading.Event()
        self.search_error = None
        self.download_error = None
        self.search_results = None
        self.download_result = None
        self._rate_limited_event = threading.Event()
        self._rate_limited_msg = None

    # ------------------------------------------------------------------ setup
    def connect(self):
        """Connect + register + join #ebooks. Raises RuntimeError on failure."""
        if self.is_registered():
            return
        raw = socket.create_connection((self.host, self.port), timeout=20)
        raw.settimeout(None)
        if self.use_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw)
        with self._rx_lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except Exception:
                    pass
            self.sock = raw
        self.nick = _random_nick()
        self._send_raw("NICK " + self.nick)
        self._send_raw(f"USER {self.nick} 0 * :{self.nick}")
        self._connected_event.clear()
        self.error = None
        if self.reader_thread is None or not self.reader_thread.is_alive():
            self.reader_thread = threading.Thread(target=self._reader, daemon=True)
            self.reader_thread.start()
        # Registration is confirmed by the reader thread seeing 001/botmode
        # or by the JOIN's echo. openbooks simply sleeps 2s and joins; do
        # the same, then rely on error events for failures.
        time.sleep(5)
        if self.error:
            raise RuntimeError("IRC registration failed: " + self.error)
        self._send_raw("JOIN #ebooks")
        # settle: NAMES + MOTD noise can arrive after join
        time.sleep(1)

    def is_registered(self):
        return self.sock is not None and not self._dead()

    def _dead(self):
        return self.error is not None

    def _send_raw(self, line):
        data = (line + "\r\n").encode("utf-8", errors="replace")
        with self._send_lock:
            sock = self.sock
            if sock is None:
                return
            try:
                sock.sendall(data)
            except OSError as e:
                self.error = str(e)

    def send_channel(self, message):
        self._send_raw("PRIVMSG #ebooks :" + message)

    def send_notice(self, user, message):
        self._send_raw(f"NOTICE {user} :{message}")

    # ------------------------------------------------------------------ reader
    def _reader(self):
        buf = b""
        while True:
            sock = self.sock
            if sock is None:
                time.sleep(0.3)
                continue
            try:
                chunk = sock.recv(4096)
            except (OSError, ssl.SSLError) as e:
                if isinstance(e, ssl.SSLError) and "timed out" not in str(e).lower():
                    self.error = str(e)
                elif isinstance(e, OSError):
                    self.error = str(e)
                self._fire_search_error("IRC connection lost: " + (self.error or e))
                self._fire_download_error("IRC connection lost: " + (self.error or e))
                time.sleep(1)
                continue
            except Exception:
                continue
            if not chunk:
                self.error = "IRC connection closed by server"
                self._fire_search_error(self.error)
                self._fire_download_error(self.error)
                time.sleep(1)
                continue
            buf += chunk
            while b"\n" in buf:
                raw_line, buf = buf.split(b"\n", 1)
                line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace")
                try:
                    self._handle_line(line)
                except Exception:
                    pass

    def _handle_line(self, line):
        # Core PING/PONG keepalive
        if line.startswith("PING :"):
            self._send_raw("PONG :" + line[6:])
            return
        if VERSION_INQUIRY in line:
            sender = line.split(" ", 1)[0]
            sender = sender[1:] if sender.startswith(":") else sender
            # tokenize to handle hostmask-style prefixes (e.g. :nick!user@host)
            sender = sender.split("!")[0]
            self.send_notice(sender, "\x01VERSION " + _VERSION_REPLY + "\x01")
            return
        # Reject banned nick / server notices: surface as errors
        upper = line.upper()

        # Rate limit notices arrive via NOTICE from the search server
        if NOTICE_MESSAGE in line:
            for pat in RATE_LIMIT_PATTERNS:
                if pat.lower() in line.lower():
                    self._rate_limited_msg = line
                    self._rate_limited_event.set()
                    self._fire_search_error("rate limited: " + self._extract_notice_text(line))
                    return
            if NO_RESULTS in line and SEARCH_RESULT_IDENTIFIER not in line:
                self._fire_search_error("No results found for the query.")
                return

        # Search bot pending: "... has been accepted ..." is just a notice; ignore
        # The actual results arrive as a DCC SEND (a .txt list)

        if SEND_MESSAGE in line:
            if SEARCH_RESULT_IDENTIFIER in line:
                self.last_search_dcc = line
                self._handle_search_dcc(line)
            else:
                self.last_download_dcc = line
                self._handle_download_dcc(line)

    def _extract_notice_text(self, line):
        # ":nick!user@host NOTICE <target> :message"
        parts = line.split(" ", 3)
        return parts[3][1:] if len(parts) > 3 and parts[3].startswith(":") else line

    # ------------------------------------------------------------------- events
    def _fire_search_error(self, msg):
        if not self.search_event.is_set():
            self.search_error = msg
            self.search_results = None
            self.search_event.set()

    def _fire_download_error(self, msg):
        if not self.download_event.is_set():
            self.download_error = msg
            self.download_result = None
            self.download_event.set()

    def _set(self, obj, kind):
        if kind == "download":
            self.download_result = obj
            self.download_event.set()
        else:
            self.search_results = obj
            self.search_event.set()

    # ---------------------------------------------------------------- DCC layer
    def _parse_dcc(self, text):
        """Parse 'DCC SEND "filename" ip port size' from a PRIVMSG line."""
        m = re.search(r"DCC SEND \"?([^\"]+?)\"?\s+(\d+)\s+(\d+)\s+(\d+)", text)
        if not m:
            return None
        ip_int, port, size = int(m.group(2)), int(m.group(3)), int(m.group(4))
        ip = socket.inet_ntoa(struct.pack(">I", ip_int))
        return {"filename": m.group(1), "ip": ip, "port": port, "size": size}

    def _dcc_download(self, dcc, progress_cb=None):
        """Download a file from the DCC peer into books_dir; returns local path."""
        if dcc is None:
            raise RuntimeError("invalid DCC SEND string")
        os.makedirs(self.books_dir, exist_ok=True)
        out_path = os.path.join(self.books_dir, dcc["filename"])
        if os.path.exists(out_path):
            # avoid name clash with a previous copy: suffix it
            base, ext = os.path.splitext(dcc["filename"])
            i = 1
            while os.path.exists(out_path):
                out_path = os.path.join(self.books_dir, f"{base} ({i}){ext}")
                i += 1
        tmp_path = out_path + ".temp"
        received = 0
        s = socket.create_connection((dcc["ip"], dcc["port"]), timeout=30)
        s.settimeout(60)
        try:
            with open(tmp_path, "wb") as f:
                while received < dcc["size"]:
                    chunk = s.recv(DCC_READ_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    if progress_cb:
                        try:
                            progress_cb(received, dcc["size"])
                        except Exception:
                            pass
        finally:
            s.close()
        if received != dcc["size"]:
            os.remove(tmp_path)
            raise RuntimeError(
                f"DCC transfer incomplete: {received}/{dcc['size']} bytes")
        os.rename(tmp_path, out_path)
        return out_path

    # -------------------------------------------------------- search result file
    def _handle_search_dcc(self, line):
        try:
            dcc = self._parse_dcc(line)
            path = self._dcc_download(dcc)
            path = _extract_single_file_archive(path)
            books, errors = self._parse_search_file(path)
            if path.endswith(".temp"):
                try:
                    os.remove(path)
                except OSError:
                    pass
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
            if not books and not errors:
                self._fire_search_error("No results found for the query.")
            else:
                self._set({"books": books, "errors": errors}, "search")
        except Exception as e:
            self._fire_search_error(str(e))

    def _handle_download_dcc(self, line):
        try:
            dcc = self._parse_dcc(line)
            current = getattr(self, "_dcc_progress", None)
            path = self._dcc_download(dcc, progress_cb=current)
            path = _extract_single_file_archive(path)
            name = os.path.basename(path)
            self._set({"name": name, "path": path,
                       "detail": path, "downloadPath": path}, "download")
        except Exception as e:
            self._fire_download_error(str(e))

    # ------------------------------------------------------------- search parsing
    @staticmethod
    def _parse_search_file(path):
        """Parse the search results text file into book dicts (V2 parser)."""
        books = []
        errors = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line.startswith("!"):
                    continue
                try:
                    books.append(_parse_result_line(line))
                except Exception as e:
                    errors.append({"line": line[:200], "error": str(e)[:200]})
        books.sort(key=lambda b: b["server"].lower())
        return books, errors

    # ------------------------------------------------------------------ API
    # OpenBooksClient-compatible interface expected by app.py / queue_worker.py:
    #   search(query) -> {"books": [...], "errors": [...]} or {"error": msg}
    #   download(book, size_bytes) -> {"name", "path", "detail"} or {"error"}
    #   ensure_connected(), _close_conn_and_reset(), conn (attribute exists)
    #   active_search/active_download states are managed by app.py
    def ensure_connected(self, _reconnect=True):
        # serialize concurrent connect attempts (queue worker + HTTP routes)
        if not self._connect_lock.acquire(timeout=CONNECT_TIMEOUT):
            raise RuntimeError("timed out waiting for the IRC connect lock")
        try:
            if self.is_registered():
                return
            last = None
            for attempt in range(3):
                try:
                    self.reset()
                    self.connect()
                    return
                except Exception as e:
                    last = e
                    time.sleep(3 + attempt * 2)
            raise RuntimeError("IRC connect failed: " + str(last))
        finally:
            self._connect_lock.release()

    def _test_hook_dcc_done(self, *a, **kw):
        """Test-hook: asserted by unit tests (never present in prod)."""
        return self.last_download_dcc

    def reset(self):
        """Hard-close the socket (connection torn down)."""
        with self._rx_lock:
            sock, self.sock = self.sock, None
        self.error = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _close_conn_and_reset(self):
        self.reset()

    def search(self, query):
        self.ensure_connected()
        self.search_event.clear()
        self.search_error = None
        self.search_results = None
        self._rate_limited_event.clear()
        self.send_channel("@search " + query)
        deadline = time.time() + SEARCH_TIMEOUT
        while time.time() < deadline:
            if self.search_event.is_set():
                if self.search_error:
                    return {"error": self.search_error}
                return self.search_results
            if self._rate_limited_event.is_set():
                return {"error": "rate limited: " + (self._rate_limited_msg or "")[:200]}
            time.sleep(0.3)
        return {"error": "search timed out"}

    def download(self, book, size_bytes=None):
        self.ensure_connected()
        self.download_event.clear()
        self.download_error = None
        self.download_result = None
        # wire live DCC byte-progress into app.py's progress reporting
        self._dcc_progress = self._dcc_make_progress(size_bytes)
        try:
            self.send_channel(book)
            deadline = time.time() + DOWNLOAD_TIMEOUT
            deadline = time.time() + DOWNLOAD_TIMEOUT
            while time.time() < deadline:
                if self.download_event.is_set():
                    if self.download_error:
                        return {"error": self.download_error}
                    return self.download_result
                if self._dead():
                    return {"error": "IRC connection lost: " + (self.error or "")}
                time.sleep(0.4)
            return {"error": "download timed out"}
        finally:
            self._dcc_progress = None

    def _dcc_make_progress(self, size_bytes):
        return None  # progress surfaced via the temp-file scan in app.py

    def close(self):
        with self._rx_lock:
            sock, self.sock = self.sock, None
        self.error = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _extract_single_file_archive(path):
    """If path is a zip containing exactly one file, extract it (openbooks
    util/archiver.go: single-file archives are unwrapped, multi-file archives
    are delivered as-is). Non-archives pass through unchanged."""
    if not path.lower().endswith(".zip"):
        return path
    try:
        import zipfile
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            if len(names) != 1:
                return path
            out = os.path.join(os.path.dirname(path), os.path.basename(names[0]))
            with z.open(names[0]) as src, open(out, "wb") as dst:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)
        os.remove(path)
        return out
    except Exception:
        return path


def _parse_result_line(line):
    """Parse one '!Server Author - Title.ext ::INFO:: size' line (V2 parser)."""

    def get_server(l):
        if not l.startswith("!"):
            raise ValueError("result lines must start with '!'")
        first_space = l.find(" ")
        if first_space == -1:
            raise ValueError("unable to parse server name")
        return l[1:first_space], l

    server, line = get_server(line)

    first_space = line.find(" ")
    dash = line.find(" - ")
    if dash == -1:
        raise ValueError("unable to parse author")
    author = line[first_space + 1:dash]
    if "%" in author:  # weird hex-prefixed authors ("%F77FE... Michael Haag")
        parts = author.split(" ", 1)
        if len(parts) == 2:
            author = parts[1]

    # title + format
    title = ""
    file_format = ""
    ext_full_end = -1
    for ext in FILE_TYPES:
        end_title = line.find("." + ext)
        if end_title == -1:
            continue
        file_format = ext
        if ext in ("rar", "zip"):
            # real format described in parens, e.g. "(.epub).rar"
            for ext2 in FILE_TYPES[:-2]:
                if ext2 in line[:end_title].lower():
                    file_format = ext2
                    break
        start = line.find(" - ")
        title = line[start + len(" - "):end_title]
        ext_full_end = end_title + 1 + len(ext)
    if ext_full_end == -1:
        raise ValueError("unable to parse title")

    # size
    delimiter = " ::INFO:: "
    info_index = line.rfind(delimiter)
    if info_index != -1:
        size = line[info_index + len(delimiter):].split(" ")[0]
    else:
        size = "N/A"

    return {
        "server": server,
        "author": author.strip(),
        "title": title.strip(),
        "format": file_format,
        "size": size,
        "full": line[:ext_full_end].strip(),
    }

