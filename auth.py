"""DB-backed authentication: one admin (full app) + regular users (OPDS only).

- Web UI and all /api + library routes require an admin session.
- /opds/* accepts admin OR regular users via HTTP Basic auth (e-reader
  clients like KOReader only support Basic auth; sessions don't work there).
- Passwords hashed with scrypt (stdlib hashlib), per-user random salt.
"""
import functools
import hashlib
import hmac
import os
import secrets
import time

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

import db

bp = Blueprint("auth", __name__)

SESSION_HOURS = 24 * 7


# ---------------------------------------------------------------------------
# password hashing
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + "$" + h.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, h_hex = stored.split("$", 1)
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                           n=16384, r=8, p=1)
        return hmac.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# user store
# ---------------------------------------------------------------------------
def ensure_admin():
    """Create the default admin on first run (username 'admin') so the app
    is never locked out. Password comes from ADMIN_PASSWORD env, else random
    (printed once to the log on Heroku: `heroku logs --tail | grep admin`)."""
    rows = db.query("SELECT id, password_hash FROM users WHERE is_admin=1")
    env_pw = os.environ.get("ADMIN_PASSWORD")
    if rows:
        row = rows[0]
        # keep admin password in sync with ADMIN_PASSWORD so a config
        # change takes effect on dyno restart without manual reset
        if env_pw and not verify_password(env_pw, row["password_hash"]):
            db.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (hash_password(env_pw), row["id"]))
            return row["id"]
        return None
    pw = env_pw or secrets.token_urlsafe(12)
    uid = db.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) "
        "VALUES (?,?,1,?)", ("admin", hash_password(pw), time.time()))
    # marker row lets us show "first-run" password in the log only once
    if not env_pw:
        import logging
        logging.getLogger(__name__).warning(
            "OPENLIB: created admin user, temporary password: %s "
            "(login and change it immediately)", pw)
    return uid


def get_user(username):
    rows = db.query("SELECT * FROM users WHERE username=?", (username,))
    return rows[0] if rows else None


def _load_session_user():
    uid = session.get("uid")
    if not uid:
        return None
    rows = db.query("SELECT id, username, is_admin FROM users WHERE id=?", (uid,))
    return rows[0] if rows else None


def current_user():
    return _load_session_user()


def is_admin():
    u = _load_session_user()
    return bool(u and u["is_admin"])


# ---------------------------------------------------------------------------
# decorators
# ---------------------------------------------------------------------------
def admin_required(view):
    """Page/API gate for the browser app. Redirects humans to /login,
    returns 401 JSON for API/XHR calls."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if is_admin():
            return view(*args, **kwargs)
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"error": "admin login required"}), 401
        return redirect(url_for("auth.login_page", next=request.path))
    return wrapped


def _basic_credentials():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return None
    import base64
    try:
        raw = base64.b64decode(header[6:]).decode()
        user, _, pw = raw.partition(":")
        return user, pw
    except Exception:
        return None


def opds_allowed():
    """True if the requester may read OPDS: admin session, or any valid
    user via HTTP Basic auth (KOReader/Panels style)."""
    u = _load_session_user()
    if u:
        return True
    creds = _basic_credentials()
    if not creds:
        return False
    user = get_user(creds[0])
    if user and verify_password(creds[1], user["password_hash"]):
        return True
    return False


def login_required_api(view):
    """For OPDS JSON/management endpoints: admin session or Basic auth user."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not opds_allowed():
            resp = jsonify({"error": "authentication required"}), 401
            return resp
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@bp.route("/login", methods=["GET"])
def login_page():
    if is_admin():
        return redirect(url_for("index"))
    nxt = request.args.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    return render_template("login.html", next=nxt)


@bp.route("/login", methods=["POST"])
def login_submit():
    body = request.json or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    user = get_user(username)
    if not user or not verify_password(password, user["password_hash"]):
        return jsonify({"error": "Invalid username or password"}), 401
    session.permanent = True
    session["uid"] = user["id"]
    if not user["is_admin"]:
        # non-admins only get OPDS access; no web UI
        return jsonify({"ok": True, "opds_only": True, "username": username})
    return jsonify({"ok": True, "opds_only": False, "username": username})


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@bp.route("/me")
def me():
    u = _load_session_user()
    if not u:
        return jsonify({"user": None})
    return jsonify({"user": {"username": u["username"], "is_admin": bool(u["is_admin"])}})


# --- admin user management (admin session required) ---
@bp.route("/users", methods=["GET"])
@admin_required
def users_list():
    rows = db.query(
        "SELECT id, username, is_admin, created_at FROM users ORDER BY id")
    return jsonify({"users": rows})


@bp.route("/users", methods=["POST"])
@admin_required
def users_create():
    body = request.json or {}
    username = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    if not username or len(password) < 4:
        return jsonify({"error": "username and password (min 4 chars) required"}), 400
    if get_user(username):
        return jsonify({"error": "username already exists"}), 409
    db.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) "
        "VALUES (?,?,0,?)", (username, hash_password(password), time.time()))
    return jsonify({"ok": True})


@bp.route("/users/<int:uid>", methods=["DELETE"])
@admin_required
def users_delete(uid):
    rows = db.query("SELECT is_admin FROM users WHERE id=?", (uid,))
    if not rows:
        return jsonify({"error": "not found"}), 404
    if rows[0]["is_admin"]:
        return jsonify({"error": "cannot delete admin user"}), 400
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    return jsonify({"ok": True})


@bp.route("/users/<int:uid>/password", methods=["POST"])
@admin_required
def users_password(uid):
    body = request.json or {}
    newpw = body.get("password") or ""
    if len(newpw) < 4:
        return jsonify({"error": "password min 4 chars"}), 400
    rows = db.query("SELECT id FROM users WHERE id=?", (uid,))
    if not rows:
        return jsonify({"error": "not found"}), 404
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (hash_password(newpw), uid))
    return jsonify({"ok": True})
