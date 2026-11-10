"""Flask chat bot backed by Amazon Bedrock (Converse API) with SQLite history
and email/password authentication.

Uses the same setup as test_bedrock.py: boto3 session, us-east-1,
global.anthropic.claude-opus-4-6-v1.
"""
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from flask import (Flask, Response, abort, g, jsonify, redirect, render_template, request,
                   session, stream_with_context, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-6-v1")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "2048"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.5"))
MAX_HISTORY = 40  # most recent messages sent to the model
MAX_MESSAGE_CHARS = 20000
MAX_TITLE_CHARS = 80

FREE_MESSAGE_LIMIT = int(os.environ.get("FREE_MESSAGE_LIMIT", "10"))  # messages per free account

MIN_PASSWORD = 8
MAX_PASSWORD = 128
MAX_FAILED_LOGINS = 8          # per (ip, email) ...
FAILED_LOGIN_WINDOW = 15 * 60  # ... within this many seconds

SYSTEM_PROMPT = (
    "You are a friendly, knowledgeable and concise general-purpose assistant. "
    "Answer clearly, use Markdown for lists and code blocks when helpful, "
    "and ask a clarifying question if a request is ambiguous."
)

app = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", os.path.join(app.root_path, "chat.db"))


def _load_secret_key():
    """SECRET_KEY env var, else a random key persisted next to the database."""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), ".secret_key")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        key = secrets.token_hex(32)
        with os.fdopen(fd, "w") as f:
            f.write(key)
        return key
    except FileExistsError:
        with open(path) as f:
            return f.read().strip()


app.config.update(
    SECRET_KEY=_load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set COOKIE_SECURE=1 once the app is served over HTTPS.
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

_session = boto3.Session(region_name=AWS_REGION)
_bedrock = _session.client("bedrock-runtime")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                plan          TEXT NOT NULL DEFAULT 'free',
                messages_used INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL
                    REFERENCES conversations(id) ON DELETE CASCADE,
                role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content         TEXT NOT NULL,
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
            CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);
            """
        )
        # Migrations for databases created by earlier versions.
        msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        for col in ("input_tokens", "output_tokens"):
            if col not in msg_cols:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} INTEGER")
        # Existing accounts start on the free plan with a fresh allowance.
        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "plan" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
        if "messages_used" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN messages_used INTEGER NOT NULL DEFAULT 0")
        conv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
        if "user_id" not in conv_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC)")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


init_db()


# --------------------------------------------------------------------------
# Authentication helpers
# --------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Used to keep login timing similar whether or not the email exists.
_DUMMY_HASH = generate_password_hash(secrets.token_hex(8))
_failed_logins = {}  # "ip|email" -> [timestamps]; in-memory, fine for a single worker process


def _too_many_failures(key):
    cutoff = time.time() - FAILED_LOGIN_WINDOW
    attempts = [t for t in _failed_logins.get(key, []) if t > cutoff]
    _failed_logins[key] = attempts
    return len(attempts) >= MAX_FAILED_LOGINS


def _record_failure(key):
    _failed_logins.setdefault(key, []).append(time.time())


def current_user():
    """The logged-in user row (cached per request) or None."""
    if "user" not in g:
        g.user = None
        uid = session.get("uid")
        if uid is not None:
            with db() as conn:
                g.user = conn.execute(
                    "SELECT id, email, plan, messages_used FROM users WHERE id = ?", (uid,)
                ).fetchone()
            if g.user is None:
                session.clear()
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required."), 401
            return redirect(url_for("login_page"))
        return view(*args, **kwargs)
    return wrapped


def _credentials_from_request():
    body = request.get_json(silent=True) or {}
    email = body.get("email")
    password = body.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        return None, None
    return email.strip().lower(), password


def _quota(plan, used):
    """Allowance summary; limit/remaining are None for paid accounts (unlimited)."""
    if plan == "paid":
        return {"plan": plan, "limit": None, "used": used, "remaining": None}
    return {"plan": plan, "limit": FREE_MESSAGE_LIMIT, "used": used,
            "remaining": max(FREE_MESSAGE_LIMIT - used, 0)}


def _reserve_message(user_id):
    """Atomically use one message of the allowance. False if a free account is out."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE users SET messages_used = messages_used + 1 "
            "WHERE id = ? AND (plan = 'paid' OR messages_used < ?)",
            (user_id, FREE_MESSAGE_LIMIT),
        )
        return cur.rowcount == 1


def _release_message(user_id):
    """Give the message back when the exchange failed, so errors don't cost the user."""
    with db() as conn:
        conn.execute("UPDATE users SET messages_used = MAX(messages_used - 1, 0) WHERE id = ?", (user_id,))


def _start_session(user_id):
    session.clear()  # drop any previous session data
    session["uid"] = user_id
    session.permanent = True


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _friendly_error(error):
    if isinstance(error, NoCredentialsError):
        return "AWS credentials not found. Run `aws configure` and restart the app."
    if isinstance(error, ClientError):
        return f"{error.response['Error']['Code']}: {error.response['Error']['Message']}"
    return "Unexpected server error."


def _fallback_title(text):
    title = " ".join(text.split())
    return (title[:MAX_TITLE_CHARS - 1] + "…") if len(title) > MAX_TITLE_CHARS else title or "New chat"


def _generate_title(user_text, reply):
    """Ask the model for a short title; fall back to the first user message."""
    try:
        resp = _bedrock.converse(
            modelId=MODEL_ID,
            messages=[{
                "role": "user",
                "content": [{"text": (
                    "Write a short title (max 5 words, no quotes, no trailing punctuation) "
                    "for the conversation below. Reply with the title only.\n\n"
                    f"User: {user_text[:500]}\n\nAssistant: {reply[:500]}"
                )}],
            }],
            inferenceConfig={"maxTokens": 30, "temperature": 0.3},
        )
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        title = text.strip().strip('"\'').splitlines()[0].strip() if text.strip() else ""
        return title[:MAX_TITLE_CHARS] or _fallback_title(user_text)
    except Exception:  # noqa: BLE001 - titles are cosmetic
        app.logger.warning("Title generation failed", exc_info=True)
        return _fallback_title(user_text)


def _load_history(conn, conversation_id):
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (conversation_id, MAX_HISTORY),
    ).fetchall()
    messages = [{"role": r["role"], "content": [{"text": r["content"]}]} for r in reversed(rows)]
    while messages and messages[0]["role"] != "user":  # Converse must start with a user turn
        messages.pop(0)
    return messages


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.get("/")
@app.get("/c/<int:conversation_id>")
@login_required
def index(**_route_args):  # /c/<id> is resolved client-side
    return render_template("index.html", model_id=MODEL_ID, region=AWS_REGION, email=g.user["email"])


@app.get("/login")
def login_page():
    if current_user():
        return redirect(url_for("index"))
    return render_template("auth.html", mode="login")


@app.get("/signup")
def signup_page():
    if current_user():
        return redirect(url_for("index"))
    return render_template("auth.html", mode="signup")


@app.get("/health")
def health():
    return jsonify(status="ok", model=MODEL_ID, region=AWS_REGION)


# --------------------------------------------------------------------------
# Auth API
# --------------------------------------------------------------------------
@app.post("/api/auth/signup")
def signup():
    email, password = _credentials_from_request()
    if email is None:
        return jsonify(error="Email and password are required."), 400
    if len(email) > 254 or not EMAIL_RE.match(email):
        return jsonify(error="Please enter a valid email address."), 400
    if not (MIN_PASSWORD <= len(password) <= MAX_PASSWORD):
        return jsonify(error=f"Password must be {MIN_PASSWORD}-{MAX_PASSWORD} characters."), 400

    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, generate_password_hash(password), now()),
            )
            user_id = cur.lastrowid
            # The first account adopts conversations created before accounts existed.
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1:
                conn.execute("UPDATE conversations SET user_id = ? WHERE user_id IS NULL", (user_id,))
    except sqlite3.IntegrityError:
        return jsonify(error="An account with this email already exists."), 409

    _start_session(user_id)
    return jsonify(email=email), 201


@app.post("/api/auth/login")
def login():
    email, password = _credentials_from_request()
    if email is None:
        return jsonify(error="Email and password are required."), 400

    key = f"{request.remote_addr}|{email}"
    if _too_many_failures(key):
        return jsonify(error="Too many failed attempts. Try again in a few minutes."), 429

    with db() as conn:
        user = conn.execute("SELECT id, email, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    # Always run a hash check so response time doesn't reveal whether the email exists.
    valid = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
    if not user or not valid:
        _record_failure(key)
        return jsonify(error="Invalid email or password."), 401

    _failed_logins.pop(key, None)
    _start_session(user["id"])
    return jsonify(email=user["email"])


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return "", 204


@app.get("/api/me")
@login_required
def me():
    return jsonify(email=g.user["email"], quota=_quota(g.user["plan"], g.user["messages_used"]))


# --------------------------------------------------------------------------
# Conversation API (always scoped to the logged-in user)
# --------------------------------------------------------------------------
def _owned_conversation(conn, conversation_id, columns="id"):
    return conn.execute(
        f"SELECT {columns} FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, g.user["id"]),
    ).fetchone()


@app.get("/api/conversations")
@login_required
def list_conversations():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
            (g.user["id"],),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/conversations/<int:conversation_id>")
@login_required
def get_conversation(conversation_id):
    with db() as conn:
        conv = _owned_conversation(conn, conversation_id, "id, title, created_at, updated_at")
        if not conv:
            abort(404)
        msgs = conn.execute(
            "SELECT role, content, created_at, input_tokens, output_tokens "
            "FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    messages = [dict(m) for m in msgs]
    usage = {
        "input_tokens": sum(m["input_tokens"] or 0 for m in messages),
        "output_tokens": sum(m["output_tokens"] or 0 for m in messages),
    }
    return jsonify({**dict(conv), "messages": messages, "usage": usage})


@app.patch("/api/conversations/<int:conversation_id>")
@login_required
def rename_conversation(conversation_id):
    title = ((request.get_json(silent=True) or {}).get("title") or "").strip()
    if not title:
        return jsonify(error="Title cannot be empty."), 400
    title = title[:MAX_TITLE_CHARS]
    with db() as conn:
        cur = conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
            (title, conversation_id, g.user["id"]),
        )
        if cur.rowcount == 0:
            abort(404)
    return jsonify(id=conversation_id, title=title)


@app.delete("/api/conversations/<int:conversation_id>")
@login_required
def delete_conversation(conversation_id):
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, g.user["id"]),
        )
        if cur.rowcount == 0:
            abort(404)
    return "", 204


# --------------------------------------------------------------------------
# Chat (streaming)
# --------------------------------------------------------------------------
@app.post("/api/chat")
@login_required
def chat():
    body = request.get_json(silent=True) or {}
    text = body.get("message")
    conversation_id = body.get("conversation_id")
    user_id = g.user["id"]

    if not isinstance(text, str) or not text.strip():
        return jsonify(error="Message cannot be empty."), 400
    text = text.strip()
    if len(text) > MAX_MESSAGE_CHARS:
        return jsonify(error="Message is too long."), 400

    history = []
    if conversation_id is not None:
        if not isinstance(conversation_id, int):
            return jsonify(error="Invalid conversation id."), 400
        with db() as conn:
            if not _owned_conversation(conn, conversation_id):
                return jsonify(error="Conversation not found."), 404
            history = _load_history(conn, conversation_id)

    messages = history + [{"role": "user", "content": [{"text": text}]}]

    # Enforce the free allowance on the server (the UI lock is only a convenience).
    if not _reserve_message(user_id):
        return jsonify(
            error=f"You've used all {FREE_MESSAGE_LIMIT} free messages. Upgrade to a paid account to keep chatting.",
            code="limit_reached",
            quota=_quota("free", FREE_MESSAGE_LIMIT),
        ), 402

    def generate():
        reply_parts = []
        saved = False
        try:
            response = _bedrock.converse_stream(
                modelId=MODEL_ID,
                messages=messages,
                system=[{"text": SYSTEM_PROMPT}],
                inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
            )
            stop_reason = None
            usage = {}
            for event in response["stream"]:
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"]["delta"]
                    if "text" in delta:
                        reply_parts.append(delta["text"])
                        yield _sse("token", {"text": delta["text"]})
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason")
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})

            reply = "".join(reply_parts)
            if not reply.strip():
                yield _sse("error", {"message": "The assistant returned an empty response."})
                return

            # Persist only after a complete exchange so history always alternates correctly.
            in_tokens = usage.get("inputTokens")
            out_tokens = usage.get("outputTokens")
            is_new = conversation_id is None
            ts = now()
            with db() as conn:
                cid = conversation_id
                if is_new:
                    cur = conn.execute(
                        "INSERT INTO conversations (title, created_at, updated_at, user_id) VALUES (?, ?, ?, ?)",
                        (_fallback_title(text), ts, ts, user_id),
                    )
                    cid = cur.lastrowid
                conn.executemany(
                    "INSERT INTO messages (conversation_id, role, content, created_at, input_tokens, output_tokens) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(cid, "user", text, ts, None, None),
                     (cid, "assistant", reply, ts, in_tokens, out_tokens)],
                )
                conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, cid))
                title = conn.execute("SELECT title FROM conversations WHERE id = ?", (cid,)).fetchone()["title"]
                account = conn.execute(
                    "SELECT plan, messages_used FROM users WHERE id = ?", (user_id,)
                ).fetchone()
            saved = True

            yield _sse("saved", {"conversation_id": cid, "title": title, "is_new": is_new,
                                 "updated_at": ts, "stop_reason": stop_reason,
                                 "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
                                 "quota": _quota(account["plan"], account["messages_used"])})
            yield _sse("done", {})

            if is_new:  # nicer title, generated after the reply so it never delays the answer
                better = _generate_title(text, reply)
                with db() as conn:
                    conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (better, cid))
                yield _sse("title", {"conversation_id": cid, "title": better})
        except (ClientError, NoCredentialsError) as exc:
            yield _sse("error", {"message": _friendly_error(exc)})
        except Exception:  # noqa: BLE001
            app.logger.exception("Chat stream failed")
            yield _sse("error", {"message": "Unexpected server error."})
        finally:
            if not saved:  # failed, empty or abandoned (client disconnected): refund the message
                _release_message(user_id)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=True)
