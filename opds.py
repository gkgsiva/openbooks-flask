"""OPDS catalog for the library (browsable by KOReader, Panels, etc.).

Routes:
  /opds                       root navigation feed
  /opds/authors               author list
  /opds/authors/<id>          books by author
  /opds/all                   every downloaded book
  /opds/download/<book_id>    streams the epub/mobi from Google Drive
"""
import xml.etree.ElementTree as ET

from flask import Blueprint, Response, abort

import db
import gdrive

bp = Blueprint("opds", __name__)

ATOM = "http://www.w3.org/2005/Atom"
OPDS = "http://opds-spec.org/2010/catalog"
DC = "http://purl.org/dc/terms/"
MIME = {
    "epub": "application/epub+zip",
    "mobi": "application/x-mobipocket-ebook",
    "pdf": "application/pdf",
    "azw3": "application/vnd.amazon.ebook",
}
TYPE_NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
TYPE_ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _xml_root(kind):
    ET.register_namespace("opds", OPDS)
    ET.register_namespace("dc", DC)
    ET.register_namespace("", ATOM)
    root = ET.Element(f"{{{ATOM}}}feed", {
        f"{{{DC}}}dummy": "",
        f"{{{OPDS}}}dummy2": "",
    })
    # remove the placeholder attributes used only to force ns declarations
    root.attrib.pop(f"{{{DC}}}dummy")
    root.attrib.pop(f"{{{OPDS}}}dummy2")
    ET.SubElement(root, f"{{{ATOM}}}id").text = f"urn:openlib:{kind}"
    ET.SubElement(root, f"{{{ATOM}}}title").text = kind.title()
    ET.SubElement(root, f"{{{ATOM}}}updated").text = _now()
    return root


def _entry(root, id_, title, content, href, type_=TYPE_NAV, rel="subsection"):
    e = ET.SubElement(root, f"{{{ATOM}}}entry")
    ET.SubElement(e, f"{{{ATOM}}}id").text = id_
    ET.SubElement(e, f"{{{ATOM}}}title").text = title
    ET.SubElement(e, f"{{{ATOM}}}link",
                  {"href": href, "type": type_, "rel": rel})
    if content:
        ET.SubElement(e, f"{{{ATOM}}}content",
                      {"type": "text"}).text = content
    return e


def _feed_response(root):
    return Response(ET.tostring(root, encoding="unicode"),
                    mimetype="application/atom+xml")


def _guess_mime(filename):
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    return MIME.get(ext, "application/octet-stream")


@bp.route("/opds")
def opds_root():
    root = _xml_root("root")
    root.find(f"{{{ATOM}}}title").text = "Open Library"
    _entry(root, "urn:openlib:authors", "By Author",
           "Browse all authors", "/opds/authors")
    _entry(root, "urn:openlib:all", "All Books",
           "All downloaded books", "/opds/all")
    return _feed_response(root)


@bp.route("/opds/authors")
def opds_authors():
    root = _xml_root("authors")
    rows = db.query(
        "SELECT a.id, a.name, COUNT(b.id) total FROM authors a "
        "JOIN books b ON b.author_id=a.id AND b.status='downloaded' "
        "GROUP BY a.id ORDER BY a.name")
    for r in rows:
        _entry(root, f"urn:openlib:author:{r['id']}", r["name"],
               f"{r['total']} book(s)", f"/opds/authors/{r['id']}")
    return _feed_response(root)


def _fetch_books(aid=None):
    sql = (
        "SELECT b.id, b.title, b.filename, a.name author_name, s.name series, "
        "b.series_num, b.size_bytes FROM books b "
        "LEFT JOIN authors a ON b.author_id=a.id "
        "LEFT JOIN series s ON b.series_id=s.id "
        "WHERE b.status='downloaded' ")
    params = ()
    if aid is not None:
        sql += "AND b.author_id=? "
        params = (aid,)
    sql += ("ORDER BY a.name, COALESCE(s.name,''), b.series_num, b.title")
    return db.query(sql, params)


@bp.route("/opds/authors/<int:aid>")
def opds_author(aid):
    root = _xml_root(f"books:author:{aid}")
    _acq_entries(root, _fetch_books(aid))
    return _feed_response(root)


@bp.route("/opds/all")
def opds_all():
    root = _xml_root("books:all")
    _acq_entries(root, _fetch_books())
    return _feed_response(root)


def _acq_entries(root, rows):
    for b in rows:
        e = ET.SubElement(root, f"{{{ATOM}}}entry")
        ET.SubElement(e, f"{{{ATOM}}}id").text = f"urn:openlib:book:{b['id']}"
        title = b["title"]
        if b.get("series") and b.get("series_num"):
            title = f"{title} ({b['series']} #{b['series_num']})"
        ET.SubElement(e, f"{{{ATOM}}}title").text = title
        if b.get("author_name"):
            a = ET.SubElement(e, f"{{{ATOM}}}author")
            ET.SubElement(a, f"{{{ATOM}}}name").text = b["author_name"]
        if b.get("series"):
            ET.SubElement(e, f"{{{DC}}}series").text = b["series"]
        ET.SubElement(e, f"{{{ATOM}}}link", {
            "href": f"/opds/download/{b['id']}",
            "type": _guess_mime(b.get("filename")),
            "rel": "http://opds-spec.org/acquisition",
        })


@bp.route("/opds/download/<int:bid>")
def opds_download(bid):
    if not gdrive.enabled():
        abort(503, "Google Drive not configured")
    rows = db.query("SELECT filename, gdrive_id FROM books WHERE id=? "
                    "AND status='downloaded'", (bid,))
    row = rows[0] if rows else None
    if not row or not row["gdrive_id"]:
        abort(404, "book not found")
    try:
        stream = gdrive.open_file(row["gdrive_id"])
    except Exception:
        import logging
        import traceback
        logging.getLogger(__name__).error(
            "OPDS download failed for %s: %s", bid, traceback.format_exc())
        abort(502, "Drive read failed")
    name = (row["filename"] or f"book_{bid}").replace('"', "").replace("\\", "")
    return Response(stream, mimetype=_guess_mime(name),
                    headers={"Content-Disposition":
                             f'attachment; filename="{name}"'})
