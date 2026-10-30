"""Book metadata from Google Books + OpenLibrary (free, no API keys)."""
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
    """Search Google Books; returns list of normalized book dicts."""
    try:
        r = _get("https://www.googleapis.com/books/v1/volumes",
                  params={"q": q, "maxResults": min(limit, 40), "country": "US"})
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


def openlibrary(q, limit=40, with_series=True):
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
        if with_series and i < 15:  # bound the extra works.json requests
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


def search_all(q, limit=40, ol_q=None):
    """Query both sources, OpenLibrary results first (better series data),
    then Google Books results not duplicated by isbn/title. Google Books has
    no series field, so series/series_num is backfilled from OpenLibrary
    by ISBN lookup. ol_q lets the caller use a different query for
    OpenLibrary (useful when searching by series name)."""
    ol = openlibrary(ol_q or q, limit)
    gb = google_books(q, limit)
    seen = {(b["isbn"], b["title"].lower()) for b in ol}
    kb = [b["title"].lower() for b in ol if b.get("series")]
    merged = ol + [b for b in gb
                   if (b["isbn"], b["title"].lower()) not in seen
                   and b["title"].lower() not in kb]
    missing = [b for b in merged[:20] if b.get("isbn") and not b.get("series")]
    if missing:
        lookup = openlib_series_lookup([b["isbn"] for b in missing])
        for b in missing:
            s = lookup.get(b["isbn"])
            if s:
                b["series"] = s["series"]
                b["series_num"] = s["series_num"]
    return merged[:limit * 2]
