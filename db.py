"""Database layer: Postgres on Heroku (DATABASE_URL), SQLite fallback locally."""
import os
import sqlite3

try:
    import psycopg2
    import psycopg2.extras
    HAVE_PG = True
except ImportError:
    HAVE_PG = False

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):  # heroku legacy url
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    os.environ["DATABASE_URL"] = DATABASE_URL
IS_PG = bool(DATABASE_URL and HAVE_PG)

# Shared database: all tables get this prefix so they don't clash with
# other applications using the same database. Override via env if needed.
PREFIX = os.environ.get("LIBRARY_TABLE_PREFIX", "ollib_")

T_AUTHORS = PREFIX + "authors"
T_SERIES = PREFIX + "series"
T_BOOKS = PREFIX + "books"
T_DOWNLOADS = PREFIX + "downloads"
T_USERS = PREFIX + "users"


def _is_local(url):
    """True when the host in the URL is a local address (no-SSL dev servers)."""
    import re
    m = re.search(r"@(localhost|127\.0\.0\.1|\[::1\])", url or "")
    return bool(m)


def _ddl():
    return f"""
CREATE TABLE IF NOT EXISTS {T_AUTHORS} (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  notes TEXT DEFAULT '',
  created_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS {T_SERIES} (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  author_id INTEGER,
  notes TEXT DEFAULT '',
  created_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS {T_BOOKS} (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  author_id INTEGER,
  series_id INTEGER,
  series_num INTEGER,
  year INTEGER,
  isbn TEXT DEFAULT '',
  language TEXT DEFAULT 'en',
  format TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',
  filename TEXT DEFAULT '',
  size_bytes INTEGER DEFAULT 0,
  source TEXT DEFAULT 'manual',
  cover_url TEXT DEFAULT '',
  irc_query TEXT DEFAULT '',
  irc_ref TEXT DEFAULT '',
  gdrive_id TEXT DEFAULT '',
  created_at REAL DEFAULT 0,
  updated_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS {T_DOWNLOADS} (
  id INTEGER PRIMARY KEY,
  book_id INTEGER,
  status TEXT DEFAULT 'queued',
  error TEXT DEFAULT '',
  retries INTEGER DEFAULT 0,
  started_at REAL DEFAULT 0,
  finished_at REAL DEFAULT 0,
  created_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS {T_USERS} (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  is_admin INTEGER DEFAULT 0,
  created_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_books_author ON {T_BOOKS}(author_id);
CREATE INDEX IF NOT EXISTS idx_books_series ON {T_BOOKS}(series_id);
"""


def connect():
    if IS_PG:
        # Respect sslmode parameter already present in DATABASE_URL (Heroku
        # sets ?sslmode=require); otherwise require SSL when connecting
        # remotely, but allow local servers without SSL.
        url = DATABASE_URL or ""
        already = ("sslmode=" in url) or ("ssl=true" in url)
        sslmode = "require" if (already or not _is_local(url)) else "prefer"
        return psycopg2.connect(url, sslmode=sslmode,
                                 cursor_factory=psycopg2.extras.RealDictCursor)
    path = os.environ.get("LIBRARY_SQLITE",
                          os.path.join(os.path.dirname(__file__), "library.db"))
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = connect()
    if IS_PG:
        cur = conn.cursor()
        for stmt in [s.strip() for s in _ddl().split(";")
                     if s.strip() and "CREATE TABLE" in s]:
            cur.execute(stmt)
        cur.execute(f"""
        DO $$ DECLARE t TEXT;
        BEGIN
          FOREACH t IN ARRAY ARRAY[{', '.join(f"'{t}'" for t in _TABLES)}] LOOP
            EXECUTE format('CREATE SEQUENCE IF NOT EXISTS %I_id_seq', t);
            EXECUTE format('ALTER TABLE %I ALTER COLUMN id SET DEFAULT nextval(''%I_id_seq'')', t, t);
          END LOOP;
        END $$;
        """)
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_books_author ON {T_BOOKS}(author_id)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_books_series ON {T_BOOKS}(series_id)")
        # add columns introduced after initial deploy (no-op if present)
        for stmt in _migrations():
            try:
                cur.execute(stmt)
            except Exception:
                pass
        conn.commit()
    else:
        conn.executescript(_ddl())
    conn.close()


_TABLES = [T_AUTHORS, T_SERIES, T_BOOKS, T_DOWNLOADS, T_USERS]


def _migrations():
    """Column additions for live databases (idempotent, error-swallowing)."""
    return [
        f"ALTER TABLE {T_DOWNLOADS} ADD COLUMN retries INTEGER DEFAULT 0",
    ]


def _prefix_sql(sql, params):
    """Prefix table references in raw SQL so all callers see prefixed tables.

    Replaces whole-word authors/series/books/downloads with prefixed names,
    only in FROM/INTO/UPDATE/JOIN/TABLE contexts (word boundaries keep
    column aliases like b, d, s, a and keywords intact).
    """
    if not PREFIX:
        return sql
    import re
    out = sql
    for t in _TABLES:
        short = t[len(PREFIX):]
        # Matches: FROM books / INTO books / UPDATE books / JOIN books /
        # TABLE IF NOT EXISTS books / TABLE books, optional alias after
        out = re.sub(
            r"\b(FROM|INTO|UPDATE|JOIN|TABLE)(\s+(?:IF\s+NOT\s+EXISTS\s+)?)(" + short + r")\b",
            lambda m, t=t: m.group(1) + m.group(2) + t, out)
    return out



def _sql(sql, params):
    if IS_PG and params:
        sql = sql.replace("?", "%s")
    return _prefix_sql(sql, params)


def query(sql, params=()):
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(_sql(sql, params), params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def execute(sql, params=()):
    """Single INSERT/UPDATE/DELETE. Returns new id for INSERTs."""
    is_insert = sql.strip().upper().startswith("INSERT")
    if IS_PG and is_insert and "RETURNING" not in sql:
        sql += " RETURNING id"
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(_sql(sql, params), params)
        rowid = None
        if is_insert:
            if IS_PG:
                row = cur.fetchone()
                rowid = row["id"] if row else None
            else:
                rowid = cur.lastrowid
        conn.commit()
        return rowid
    finally:
        conn.close()
