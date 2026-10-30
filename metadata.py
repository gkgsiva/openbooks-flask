"""Book metadata from Google Books + OpenLibrary + Hardcover (token optional)."""
import json
import os
import re
import requests

UA = {"User-Agent": "openbooks-library/1.0"}
S = requests.Session()
S.headers.update(UA)


def _get(url, params, timeout=15):
    try:
        return S.get(url, params=params, timeout=timeout)
    except requests.exceptions.SSLError:
        # Corporate proxies that intercept TLS may not chain to the bundled CAs.
        # Only used when explicitly enabled or as a last-resort fallback.
        return S.get(url, params=params, timeout=timeout,
                     verify=os.environ.get("REQUESTS_CA_BUNDLE", False))


def _int_year(s):
    if not s:
        return None
    m = re.search(r"(\d{4})", str(s))
    return int(m.group(1)) if m else None


def _cover_gb(item):
    info = item.get("volumeInfo", {})
    return (info.get("imageLinks") or {}).get("thumbnail", "").replace("http://", "https://")


def google_books(q, limit=40):
    """Search Google Books; returns list of normalized book dicts.
    GOOGLE_BOOKS_API_KEY (optional) lifts the shared anonymous 1k/day quota to 1k/day private."""
    import os
    params = {"q": q, "maxResults": min(limit, 40), "country": "US"}
    key = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
    if key:
        params["key"] = key
    try:
        r = _get("https://www.googleapis.com/books/v1/volumes", params=params)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out = []
    for item in data.get("items", []):
        info = item.get("volumeInfo", {})
        ids = info.get("industryIdentifiers", [])
        isbn13 = next((i["identifier"] for i in ids if i["type"] == "ISBN_13"), None)
        isbn10 = next((i["identifier"] for i in ids if i["type"] == "ISBN_10"), None)
        out.append({
            "title": info.get("title", ""),
            "author": (info.get("authors") or [""])[0],
            "authors_all": ", ".join(info.get("authors", [])),
            "year": _int_year(info.get("publishedDate")),
            "isbn": isbn13 or isbn10 or "",
            "language": info.get("language", ""),
            "format": info.get("printType", ""),
            "pages": info.get("pageCount"),
            "description": (info.get("description") or "")[:500],
            "cover_url": _cover_gb(item),
            "source": "google_books",
        })
    return out


def openlibrary(q, limit=40, with_series=True, series_cap=25):
    """Search OpenLibrary; returns list of normalized book dicts.
    Series data is fetched per-work from works.json (search.json has none)."""
    try:
        r = _get("https://openlibrary.org/search.json",
                  params={"q": q, "limit": min(limit, 100), "fields": "key,title,author_name,first_publish_year,isbn,language,number_of_pages_median,cover_i"})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out = []
    series_cap = 25
    for i, doc in enumerate(data.get("docs", [])):
        cover_id = doc.get("cover_i")
        cover = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else ""
        b = {
            "title": doc.get("title", ""),
            "author": (doc.get("author_name") or [""])[0],
            "authors_all": ", ".join(doc.get("author_name", [])),
            "year": _int_year(doc.get("first_publish_year")),
            "isbn": (doc.get("isbn") or [""])[0],
            "language": (doc.get("language") or [""])[0] or "en",
            "format": "",
            "pages": doc.get("number_of_pages_median"),
            "description": "",
            "cover_url": cover,
            "series": "",
            "series_num": None,
            "source": "openlibrary",
        }
        if with_series and i < series_cap:  # bound the extra works.json requests
            b["series"], b["series_num"] = _work_series(doc.get("key"))
        out.append(b)
    return out


def _series_name(skey):
    """Fetch /series/{id}.json and return its name field."""
    sid = skey.strip("/").split("/")[-1]
    try:
        r = _get(f"https://openlibrary.org/series/{sid}.json", params=None, timeout=8)
        r.raise_for_status()
        return (r.json().get("name") or "").strip() or None
    except Exception:
        return None


def _work_series(key):
    """Fetch /works/{key}.json and return (series_name, series_num).
    search.json has no series data; works.json has a series list with
    position, and subjects contain 'series:<Name>' entries with the name.
    Falls back to the series entity's own name via /series/{id}.json."""
    if not key:
        return None, None
    wid = key.strip("/").split("/")[-1]  # "/works/OL82563W" -> "OL82563W"
    try:
        r = _get(f"https://openlibrary.org/works/{wid}.json", params=None, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None, None
    entries = data.get("series") or []
    entry = next(iter(entries), None)
    pos = None
    if isinstance(entry, dict):
        raw = entry.get("position")
        pos = str(raw).strip() if raw is not None else None
        skey = (entry.get("series") or {}).get("key")
    else:
        skey = None
    name = None
    for subj in data.get("subjects", []):
        if isinstance(subj, str) and subj.lower().startswith("series:"):
            name = subj.split(":", 1)[1].replace("_", " ").strip()
            break
    if not name and skey:
        name = _series_name(skey)
    if name and pos:
        return name, (int(pos) if "." not in pos and pos.lstrip("-").isdigit() else pos)
    return name, pos


def openlib_series_lookup(isbns, limit=20):
    """Look up series info by ISBN by resolving the work key from search.json.
    Used to backfill series data for Google Books results, which never
    include a series field."""
    out = {}
    for isbn in [i for i in isbns if i][:limit]:
        try:
            r = _get("https://openlibrary.org/search.json",
                     params={"q": isbn, "limit": 1, "fields": "key,isbn"})
            r.raise_for_status()
            docs = r.json().get("docs") or []
        except Exception:
            continue
        for doc in docs:
            if isbn in (doc.get("isbn") or []):
                name, num = _work_series(doc.get("key"))
                if name:
                    out[isbn] = {"series": name, "series_num": num}
                break
    return out


def _post(url, payload, timeout=15, headers=None):
    try:
        return S.post(url, json=payload, timeout=timeout, headers=headers)
    except requests.exceptions.SSLError:
        return S.post(url, json=payload, timeout=timeout, headers=headers,
                     verify=os.environ.get("REQUESTS_CA_BUNDLE", False))


def hardcover(q, limit=40):
    """Search Hardcover.app (GraphQL API, free token required).
    Series name + position are first-class fields — much better than
    Google/OpenLibrary for series metadata. Returns [] if no token set.
    Set HARDCOVER_API_KEY via `heroku config:set HARDCOVER_API_KEY=...`."""
    token = os.environ.get("HARDCOVER_API_KEY", "").strip()
    if not token:
        return []
    # Hardcover's search uses a Typesense-style index: `results` is a JSON
    # scalar with hits[].document (not the normal GraphQL object schema).
    query = """query Search($q: String!, $n: Int!) {
      search(query: $q, query_type: "Book", per_page: $n, page: 1) {
        results
      }
    }"""
    try:
        r = _post("https://api.hardcover.app/v1/graphql",
                  {"query": query,
                   "variables": {"q": q, "n": min(limit, 40)}},
                  headers={"Authorization": "Bearer %s" % token})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    if data.get("errors"):
        return []
    results = ((data.get("data") or {}).get("search") or {}).get("results")
    if isinstance(results, str):
        try:
            results = json.loads(results)
        except Exception:
            return []
    docs = [h.get("document") or {} for h in (results or {}).get("hits") or []]
    out = []
    for rec in docs:
        fs = rec.get("featured_series") or {}
        ser = fs.get("series") or {}
        isbns = rec.get("isbns") or []
        isbn = next((i for i in isbns if len(str(i)) == 13), isbns[0] if isbns else "")
        out.append({
            "title": rec.get("title", ""),
            "author": (", ".join(rec.get("author_names") or [])).split(",")[0].strip(),
            "authors_all": ", ".join(rec.get("author_names") or []),
            "year": rec.get("release_year"),
            "isbn": str(isbn) if isbn else "",
            "language": "",
            "format": "",
            "pages": None,
            "description": "",
            "cover_url": ((rec.get("image") or {}).get("url") or "").replace("http://", "https://"),
            "series": ser.get("name", ""),
            "series_num": int(fs["position"]) if fs.get("position") else fs.get("position"),
            "source": "hardcover",
        })
    return out


def _hc_post(query, variables):
    """Run a Hardcover GraphQL query; returns data dict or {} on any failure."""
    token = os.environ.get("HARDCOVER_API_KEY", "").strip()
    if not token:
        return {}
    try:
        r = _post("https://api.hardcover.app/v1/graphql",
                  {"query": query, "variables": variables},
                  headers={"Authorization": "Bearer %s" % token})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return {}
    if data.get("errors"):
        return {}
    return data.get("data") or {}


def _hc_book_row(b, author=None):
    """Map a Hardcover `books` object to our standard result row."""
    fbs = b.get("featured_book_series") or {}
    ser = fbs.get("series") or {}
    pos = fbs.get("position")
    out = {
        "title": b.get("title", ""),
        "author": author or "",
        "authors_all": author or "",
        "year": b.get("release_year"),
        "isbn": "",
        "language": "",
        "format": "",
        "pages": None,
        "description": "",
        "cover_url": "",
        "series": ser.get("name", ""),
        "series_num": int(pos) if pos else pos,
        "source": "hardcover",
    }
    # cover: b.image is an object only if selected; keep API-independent
    img = b.get("image")
    if isinstance(img, dict):
        out["cover_url"] = (img.get("url") or "").replace("http://", "https://")
    return out


def _dedup_books(rows):
    """Near-duplicate books appear a lot on Hardcover (different editions of
    the same work share a title). Keep one row per (title, year) preferring
    the one that has series data."""
    def base_key(r):
        t = (r.get("title") or "").lower()
        t = re.split(r"[:;/]| book \d+", t)[0].strip(" .!,")
        t = re.sub(r"^(a|an|the)\s+", "", t)
        return (t, r.get("year"))
    seen = {}
    for r in rows:
        key = base_key(r)
        if key in seen:
            old = seen[key]
            # prefer the plainest title (no subtitle), keep donor's extras
            if len(r.get("title") or "") < len(old.get("title") or ""):
                old, r = r, old
                seen[key] = old
            if not old.get("series") and r.get("series"):
                old["series"], old["series_num"] = r["series"], r.get("series_num")
            if not old.get("cover_url") and r.get("cover_url"):
                old["cover_url"] = r["cover_url"]
            if not old.get("isbn") and r.get("isbn"):
                old["isbn"] = r["isbn"]
        else:
            seen[key] = r
    return sorted(seen.values(), key=lambda x: (x.get("year") is None, x.get("year") or 0, x.get("series_num") is None, x.get("series_num") or 0))


def hardcover_author(name, limit=300):
    """ALL books by an author: lookup author id via search, then query the
    books table filtered by contribution author_id (English editions only).
    This returns the full bibliography — much more complete than the plain
    search endpoint."""
    data = _hc_post(
        'query($q: String!, $n: Int!) { search(query: $q, query_type: "Author", '
        'per_page: $n, page: 1) { results } }', {"q": name, "n": 5})
    results = ((data.get("search") or {}).get("results")) or {}
    if isinstance(results, str):
        try:
            results = json.loads(results)
        except Exception:
            results = {}
    hits = [h.get("document") or {} for h in results.get("hits") or []]
    hits = [h for h in hits if h.get("id") and h.get("books_count", 0) >= 1]
    if not hits:
        # fallback: treat query as a plain search
        return hardcover(name, limit=40)
    hits.sort(key=lambda h: -(h.get("books_count") or 0))
    author_id = int(hits[0]["id"])
    q = '''query($id: Int!, $n: Int!) {
      books(
        where: {contributions: {author_id: {_eq: $id}, contributable_type: {_eq: "Book"}}}
        order_by: {release_year: asc}, limit: $n
      ) { title release_year image { url } featured_book_series { position series { name } }
          editions(where: {language_id: {_eq: 1}}, limit: 1) { isbn_13 } }
    }'''
    data = _hc_post(q, {"id": author_id, "n": limit})
    books = data.get("books") or []
    rows = []
    for b in books:
        if not b.get("editions"):  # no English edition — skip translations
            continue
        row = _hc_book_row(b, author=name)
        ed = b["editions"][0]
        row["isbn"] = str(ed.get("isbn_13") or "") if ed.get("isbn_13") else ""
        rows.append(row)
    return _dedup_books(rows)


def hardcover_series(name, limit=300):
    """ALL books in a series: lookup series id via search, then query the
    full series book list ordered by position."""
    data = _hc_post(
        'query($q: String!, $n: Int!) { search(query: $q, query_type: "Series", '
        'per_page: $n, page: 1) { results } }', {"q": name, "n": 5})
    results = ((data.get("search") or {}).get("results")) or {}
    if isinstance(results, str):
        try:
            results = json.loads(results)
        except Exception:
            results = {}
    hits = [h.get("document") or {} for h in results.get("hits") or []]
    hits = [h for h in hits if h.get("id")]
    if not hits:
        return hardcover(name, limit=40)
    hits.sort(key=lambda h: -(h.get("books_count") or 0))
    series_id = int(hits[0]["id"])
    q = '''query($id: Int!, $n: Int!) {
      series(where: {id: {_eq: $id}}) {
        name
        book_series(where: {position: {_is_null: false}}, order_by: {position: asc}, limit: $n) {
          position
          book { title release_year image { url } featured_book_series { position series { name } }
                 editions(where: {language_id: {_eq: 1}}, limit: 1) { isbn_13 } }
        }
      }
    }'''
    data = _hc_post(q, {"id": series_id, "n": limit})
    series = (data.get("series") or [{}])[0] or {}
    rows = []
    for bs in series.get("book_series") or []:
        b = bs.get("book")
        if not b or not b.get("editions"):  # skip translations w/o EN edition
            continue
        row = _hc_book_row(b, author=None)
        row["series"] = series.get("name", "") or row["series"]
        row["series_num"] = bs.get("position")
        if row["series_num"] is not None and float(row["series_num"]).is_integer():
            row["series_num"] = int(row["series_num"])
        ed = b["editions"][0]
        row["isbn"] = str(ed.get("isbn_13") or "") if ed.get("isbn_13") else ""
        rows.append(row)
    return _dedup_books(rows)  # sorted by year; positions stay in row


def search_all(q, limit=40, ol_q=None, order="google"):
    """Query both sources. Google Books first by default — it produces richer,
    more complete bibliographies for author searches (e.g. "jeffrey archer"),
    and OpenLibrary series data is backfilled onto every result by ISBN
    lookup regardless of order. ol_q lets the caller use a different query
    for OpenLibrary (useful when searching by series name)."""
    ol = openlibrary(ol_q or q, limit)
    gb = google_books(q, limit)
    primary, secondary = (gb, ol) if order != "openlibrary" else (ol, gb)
    seen = {(b["isbn"], b["title"].lower()) for b in primary}
    kb = [b["title"].lower() for b in primary if b.get("series")]
    merged = primary + [b for b in secondary
                        if (b["isbn"], b["title"].lower()) not in seen
                        and b["title"].lower() not in kb]
    missing = [b for b in merged if b.get("isbn") and not b.get("series")][:25]
    if missing:
        lookup = openlib_series_lookup([b["isbn"] for b in missing])
        for b in missing:
            s = lookup.get(b["isbn"])
            if s:
                b["series"] = s["series"]
                b["series_num"] = s["series_num"]
    return merged[:limit * 2]
