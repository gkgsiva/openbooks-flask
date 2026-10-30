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


def openlibrary(q, limit=40):
    """Search OpenLibrary; returns list of normalized book dicts."""
    try:
        r = _get("https://openlibrary.org/search.json",
                  params={"q": q, "limit": min(limit, 100)})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out = []
    for doc in data.get("docs", []):
        cover_id = doc.get("cover_i")
        cover = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else ""
        out.append({
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
            "series": ", ".join(doc.get("series", [])[:1]),
            "series_num": next(iter(doc.get("series_number") or []), None),
            "source": "openlibrary",
        })
    return out


def search_all(q, limit=40):
    """Query both sources, OpenLibrary results first (better series data),
    then Google Books results not duplicated by isbn/title."""
    ol = openlibrary(q, limit)
    gb = google_books(q, limit)
    seen = {(b["isbn"], b["title"].lower()) for b in ol}
    merged = ol + [b for b in gb if (b["isbn"], b["title"].lower()) not in seen]
    return merged[:limit * 2]
