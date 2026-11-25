"""
PR GRAM Helper v3.1  -  multi-user helper for the PR GRAM task bot
======================================================================
Every person logs in with their OWN Telegram account through the control bot
(QR or phone) and gets an isolated worker: own session, settings, stats,
flood / limit state and speed profile. One account's flood wait or error never
slows another account down.

Per account the helper watches the chat with the PR GRAM bot:

  * "Earnings" -> category menu -> presses "Channels" (PG_CATEGORY) -> task list
  * task lists  ("$ 899 | Subscribe"  +  "Check" buttons, paged "1 < 1 > 25")
        -> joins the channel (saved in the DB), presses "Check" right after the join
  * "Task No... completed / You received +N GRAM / Balance: ..."  -> dashboard
  * "Please verify that you are human" -> STOPS that account and pings its owner.
        The puzzle has to be solved by a person in the normal Telegram app.
  * limit / flood notices written by the PR GRAM bot itself -> detected, shown, obeyed

What is new in 3.2
  * REDESIGN  Telegram now only exposes Login, Start/Stop and Logout (+ the Dashboard link button). Every setting,
              every risk control and all admin management moved to a per-account web dashboard, opened from a
              button in Telegram. The old inline Settings/Admin/Leave/Profile/Stats menus are gone from Telegram.
  * SAFETY    Even in the web dashboard's "Advanced" mode, the join gap / batch size / parallel-join count can
              never be pushed past hard safety floors (HARD_MIN_GAP / HARD_MAX_BATCH / HARD_MAX_CONC below) -
              "configurable" never means "unthrottled"; it protects the account from flooding/limits either way.
  * DASHBOARD users sign in with private usernames and passwords; first-time and reset credentials are delivered
              through the user's private Telegram chat. Permissions remain tied to the Telegram account.

What is new in 3.1
  * NOCAP  removed every artificial hourly/daily join ceiling from every profile (including SMART). The only thing
           the helper still obeys is a REAL flood wait Telegram itself returns - that cannot be bypassed because
           it is enforced server-side; the helper just reacts correctly to it (slows down, then ramps back up).
  * SMART  🧠 natural human-pace profile (default for new users): ~300/day, 60/h at full pace, irregular gaps,
           2 workers, 4-8 task batches, light warm-up for brand-new accounts only, short breaks
  * SPEED  new ⚡ MAX profile (default): short join gaps, 4 parallel workers, 20 tasks per batch,
           instant re-fetch after a productive round, fetch interval 3s
  * SPEED  adaptive flood control: on a Telegram flood wait the user is WARNED, speed drops 3x
           (gap x3, hourly/daily budget /3), then after the wait it ramps back up to 100% by itself
           (~15 min). A repeated flood while still slowed escalates (x4.5, x6.7 ... max x9).
           Flood waits are always obeyed in full - never bypassed.
  * JOIN   cheap membership probe BEFORE taking a join slot (already-member / expired / join-request
           links no longer waste a slot), invite links checked with CheckChatInvite,
           more error types mapped (UserChannelsTooMuch, UserBannedInChannel, UsersTooMuch...)
  * FIX    styled-button fallback no longer drops the message / is no longer triggered by unrelated errors
  * FIX    "try again later" inside a failed-Check alert is no longer mistaken for a bot-side limit
  * FIX    menu click throttle (8s) removed -> it capped the whole cycle at one round per 8s
  * FIX    old config.json can no longer override the new speed defaults (only owner/admin/access persist)
  * UI     profile switcher on the main panel, speed meter, recent-events log, 🧯 full-speed override,
           more colored buttons, orange "slowed" status

Install:   pip install -U telethon qrcode pillow python-dotenv cryptg cryptography
Env:       BOT_TOKEN, OWNER_ID, ADMIN_IDS, API_ID / API_HASH (use your OWN from my.telegram.org),
           OPEN_ACCESS (1), MAX_USERS (500), GLOBAL_CONC (60), SESSION_KEY, USE_PROXY / PROXY_URL,
           PG_BOT (gram_piarbot), PG_EARN_TEXT (Earnings), PG_CATEGORY (Channels),
           PG_JOIN_INTERVAL (8), PG_FETCH_INTERVAL (3),   [no PG_JOINS_HOUR/DAY any more - no artificial ceiling]
           WEB_PORT (optional: the port your host assigned you, e.g. on
           Katabump/Pterodactyl-style panels - also auto-detected from SERVER_PORT/PORT/etc. if WEB_PORT is unset)
           PG_CONC (4), PG_BATCH (20)

v3.3 (patch.py): web dashboard, hard safety controls, deep clean - Telegram keeps only Login / Start-Stop / Logout.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html as _html
import io
import json
import logging
import logging.handlers
import os
import random
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from telethon import Button, TelegramClient, events, utils
    from telethon import errors as tgerr
    from telethon.errors import (
        MessageNotModifiedError, SessionPasswordNeededError, FloodWaitError,
        PhoneCodeInvalidError, PhoneCodeExpiredError, PasswordHashInvalidError,
    )
    from telethon.sessions import StringSession
    from telethon.tl import types as tltypes
    from telethon.tl.types import BotCommand, BotCommandScopeDefault
    from telethon.tl.functions.bots import SetBotCommandsRequest
except ImportError:
    print("ERROR: telethon not installed. Run: pip install telethon", file=sys.stderr)
    sys.exit(1)

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False


def _err(name: str):
    return getattr(tgerr, name, type(f"_Missing{name}", (Exception,), {}))


UserAlreadyParticipantError = _err("UserAlreadyParticipantError")
InviteRequestSentError = _err("InviteRequestSentError")
InviteHashExpiredError = _err("InviteHashExpiredError")
InviteHashInvalidError = _err("InviteHashInvalidError")
ChannelsTooMuchError = _err("ChannelsTooMuchError")
UserChannelsTooMuchError = _err("UserChannelsTooMuchError")
UsernameNotOccupiedError = _err("UsernameNotOccupiedError")
UsernameInvalidError = _err("UsernameInvalidError")
BotResponseTimeoutError = _err("BotResponseTimeoutError")
ChannelPrivateError = _err("ChannelPrivateError")
ChannelInvalidError = _err("ChannelInvalidError")
UserNotParticipantError = _err("UserNotParticipantError")
UserBannedInChannelError = _err("UserBannedInChannelError")
UsersTooMuchError = _err("UsersTooMuchError")
InviteHashEmptyError = _err("InviteHashEmptyError")
MessageIdInvalidError = _err("MessageIdInvalidError")

# links that can never be joined -> remember as "bad" and move on
SKIP_ERRORS = (InviteRequestSentError, InviteHashExpiredError, InviteHashInvalidError, InviteHashEmptyError,
               UsernameNotOccupiedError, UsernameInvalidError, ChannelPrivateError, ChannelInvalidError,
               UserBannedInChannelError, UsersTooMuchError)
FULL_ERRORS = (ChannelsTooMuchError, UserChannelsTooMuchError)

DEAD_ERRORS = tuple(
    getattr(tgerr, n) for n in (
        "AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError",
        "UserDeactivatedError", "UserDeactivatedBanError", "AuthKeyDuplicatedError",
    ) if hasattr(tgerr, n)
)

VERSION = "3.4"
PATCH_3_4_APPLIED = True


class FatalConfig(Exception):
    pass


try:
    from telethon.tl.functions.channels import (
        GetParticipantRequest, JoinChannelRequest, LeaveChannelRequest)
    from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
    from telethon.tl.types import Channel
except ImportError:
    print("ERROR: this telethon version is too old. Run: pip install -U telethon", file=sys.stderr)
    sys.exit(1)

# ===========================================================================
# CONSTANTS
# ===========================================================================
NO_CAP = 10 ** 9   # "no fixed ceiling" imposed by the helper - the only real ceiling is Telegram's own flood wait
LEAVE_AFTER_DAYS = 8   # fallback default; the real, per-account value is leave_after_days(uid) (web-configurable)
RESOLVE_RETRY = 60.0
DEDUPE_TTL = 300.0
URL_TTL = 3600.0
STATE_TTL = 600.0
PANEL_IDLE = 12 * 3600.0
MAX_BOT_WAIT = 6 * 3600

# adaptive flood control
SLOW_FACTOR = 3.0        # first flood: everything gets 3x slower
SLOW_MAX = 9.0           # repeated floods while still slowed escalate up to this
RECOVER_SECS = 900.0     # after the flood wait ends, speed ramps back to 100% over this time

# ===========================================================================
# CONFIG & PATHS
# ===========================================================================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
USERS_DIR = DATA_DIR / "users"
LOGS_DIR = DATA_DIR / "logs"
for _d in (DATA_DIR, USERS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_FILE = DATA_DIR / "prgram.db"
CONFIG_FILE = DATA_DIR / "config.json"
BOT_SESSION_FILE = DATA_DIR / "bot.session"
KEY_FILE = DATA_DIR / "secret.key"

PUBLIC_API_ID = 6
PUBLIC_API_HASH = "eb06d4abfb49dc3eeb1aeb98ae0f581e"


def _env_str(k: str, d: str = "") -> str:
    return (os.environ.get(k) or "").strip() or d


def _env_int(k: str, d: int) -> int:
    raw = (os.environ.get(k) or "").strip()
    try:
        return int(raw) if raw else d
    except ValueError:
        return d


def _env_bool(k: str, d: bool) -> bool:
    raw = (os.environ.get(k) or "").strip().lower()
    return d if not raw else raw in ("1", "true", "yes", "on", "y", "t")


CONFIG: Dict[str, Any] = {
    "api_id": _env_int("API_ID", PUBLIC_API_ID),
    "api_hash": _env_str("API_HASH", PUBLIC_API_HASH),
    "bot_token": _env_str("BOT_TOKEN", ""),
    "owner_id": _env_int("OWNER_ID", 0),
    "admin_ids": _env_str("ADMIN_IDS", ""),
    "open_access": _env_bool("OPEN_ACCESS", True),
    "max_users": _env_int("MAX_USERS", 500),
    "global_conc": _env_int("GLOBAL_CONC", 60),
    "use_proxy": _env_bool("USE_PROXY", False),
    "proxy_url": _env_str("PROXY_URL", "socks5://127.0.0.1:1080"),
    "pg_bot": _env_str("PG_BOT", "gram_piarbot"),
    "pg_earn_text": _env_str("PG_EARN_TEXT", "Earnings"),
    "pg_fetch_interval": _env_int("PG_FETCH_INTERVAL", 3),
    "pg_category": _env_str("PG_CATEGORY", "Channels"),
    "pg_join_interval": _env_int("PG_JOIN_INTERVAL", 8),
    "pg_conc": min(3, max(1, _env_int("PG_CONC", 3))),
    "pg_batch": min(10, max(1, _env_int("PG_BATCH", 10))),
    "pg_max_pages": _env_int("PG_MAX_PAGES", 25),
}

# only these survive in config.json (an old file must never override the speed defaults)
_PERSIST_KEYS = ("owner_id", "admin_ids", "open_access")

if CONFIG_FILE.exists():
    try:
        _saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(_saved, dict):
            for k, v in _saved.items():
                if k in _PERSIST_KEYS and (k == "open_access" or not (os.environ.get(k.upper()) or "").strip()):
                    CONFIG[k] = v
    except Exception:
        pass


def save_config() -> None:
    try:
        tmp = CONFIG_FILE.with_suffix(".tmp")
        data = {k: CONFIG[k] for k in _PERSIST_KEYS}
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
    except Exception as e:
        logging.getLogger("job").error(f"save_config: {e}")


# speed profiles: only the join-GAP factor matters now - no profile imposes its own hourly/daily ceiling any more.
# (the hourly/daily factor fields are kept at 1.0 for backward compatibility with old rows but are no longer used)
PROFILES: Dict[str, Tuple[float, float, float]] = {
    "safe": (1.0, 1.0, 2.0),
    "normal": (1.0, 1.0, 1.0),
    "fast": (1.0, 1.0, 0.55),
    "max": (1.0, 1.0, 0.25),
    "smart": (1.0, 1.0, 1.0),     # placeholder: the SMART profile computes its own gap (see smart_limits)
}
PROFILE_ORDER = ("smart", "safe", "normal", "fast", "max")
DEFAULT_PROFILE = "smart"


def prof_limits(name: str) -> Tuple[int, int, float]:
    _fh, _fd, fi = PROFILES.get(name, PROFILES[DEFAULT_PROFILE])
    return (NO_CAP, NO_CAP, max(1.5, float(CONFIG["pg_join_interval"]) * fi))


# ===========================================================================
# LOGGING
# ===========================================================================
_fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%H:%M:%S")
_root = logging.getLogger("job")
_root.setLevel(logging.INFO)
_root.propagate = False
_fh = logging.handlers.RotatingFileHandler(LOGS_DIR / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
_fh.setFormatter(_fmt)
_root.addHandler(_fh)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
_root.addHandler(_ch)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = _root


# [patch-hardening:swallow] - `except Exception: pass` sites are now counted and (rarely) logged instead of silent
_SWALLOWED = {}


def _swallow(exc=None) -> None:
    try:
        tb = getattr(exc, "__traceback__", None)
        key = (f"line {tb.tb_lineno} in {tb.tb_frame.f_code.co_name}: " if tb else "") + type(exc).__name__
        n = _SWALLOWED.get(key, 0) + 1
        if n == 1 or key in _SWALLOWED or len(_SWALLOWED) < 500:
            _SWALLOWED[key] = n
        if n == 1 or n % 1000 == 0:
            log.debug(f"swallowed exception x{n} at {key}: {exc!r}")
    except Exception as _swallow_e:
        _swallow(_swallow_e)


def evt(uid: int, event: str, detail: str = "", level: int = logging.INFO) -> None:
    log.log(level, f"u{uid:<11} | {event:<6} | {detail}")


# ===========================================================================
# DATABASE
# ===========================================================================
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pg_cfg (
    uid        INTEGER PRIMARY KEY,
    auto_join  INTEGER DEFAULT 0,
    auto_fetch INTEGER DEFAULT 0,
    paused     INTEGER DEFAULT 0,
    auto_leave INTEGER DEFAULT 0,
    profile    TEXT DEFAULT 'smart',
    lang       TEXT DEFAULT '',
    leave_days INTEGER DEFAULT 8,
    adv_mode   INTEGER DEFAULT 0,
    adv_gap    REAL DEFAULT 0,
    adv_batch  INTEGER DEFAULT 0,
    adv_conc   INTEGER DEFAULT 0,
    night_start INTEGER DEFAULT 2,
    night_end   INTEGER DEFAULT 6,
    warmup_ts   REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pg_webtok (uid INTEGER PRIMARY KEY, token TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pg_stats (
    uid        INTEGER PRIMARY KEY,
    tasks_seen INTEGER DEFAULT 0,
    joined     INTEGER DEFAULT 0,
    skipped    INTEGER DEFAULT 0,
    checked    INTEGER DEFAULT 0,
    completed  INTEGER DEFAULT 0,
    gram       REAL    DEFAULT 0,
    errors     INTEGER DEFAULT 0,
    captchas   INTEGER DEFAULT 0,
    balance    TEXT    DEFAULT '',
    last_ev    TEXT    DEFAULT '',
    last_ev_ts REAL    DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pg_joins (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    uid       INTEGER NOT NULL,
    peer_id   INTEGER DEFAULT 0,
    ref       TEXT DEFAULT '',
    title     TEXT DEFAULT '',
    kind      TEXT DEFAULT '',
    joined_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_joins_uid_ts ON pg_joins(uid, joined_at);
CREATE TABLE IF NOT EXISTS pg_bad (
    uid INTEGER NOT NULL,
    ref TEXT NOT NULL,
    reason TEXT DEFAULT '',
    ts REAL DEFAULT 0,
    PRIMARY KEY (uid, ref)
);
CREATE TABLE IF NOT EXISTS pg_state (
    uid INTEGER PRIMARY KEY,
    flood_until REAL DEFAULT 0,
    last_wait REAL DEFAULT 0,
    last_ts REAL DEFAULT 0,
    jint REAL DEFAULT 0,
    mult REAL DEFAULT 1,
    hour_cap REAL DEFAULT 0,
    cap_until REAL DEFAULT 0,
    day_cap REAL DEFAULT 0,
    day_until REAL DEFAULT 0,
    slow0 REAL DEFAULT 1,
    slow_end REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pg_ban (uid INTEGER PRIMARY KEY, ts REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS pg_allow (uid INTEGER PRIMARY KEY, ts REAL DEFAULT 0);
"""

_STAT_COLS = ("tasks_seen", "joined", "skipped", "checked", "completed", "gram", "errors", "captchas")
_CFG_INT = ("auto_join", "auto_fetch", "paused", "auto_leave", "adv_mode", "adv_batch", "adv_conc",
            "leave_days", "night_start", "night_end")
_CFG_TXT = ("profile", "lang")
_CFG_REAL = ("adv_gap", "warmup_ts")

# hard safety floors/ceilings - no setting (profile, advanced mode, admin override) can ever cross these.
HARD_MIN_GAP = 3.0        # seconds between joins, no matter what
HARD_MAX_BATCH = 10       # tasks handled per round, no matter what
HARD_MAX_CONC = 3         # parallel joins, no matter what
MIN_LEAVE_DAYS, MAX_LEAVE_DAYS = 3, 30
_CFG_CACHE: Dict[int, dict] = {}
_RECENT: Dict[int, List[Tuple[float, str]]] = {}


def init_db() -> None:
    global _conn
    if _conn is not None:
        return
    c = sqlite3.connect(str(DB_FILE), timeout=30.0, isolation_level=None, check_same_thread=False)
    for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "busy_timeout=30000", "temp_store=MEMORY",
                   "cache_size=-32768", "mmap_size=134217728", "wal_autocheckpoint=2000"):
        try:
            c.execute("PRAGMA " + pragma)
        except sqlite3.Error:
            pass
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    stmts = list(_DB_ALTERS) + [f"ALTER TABLE pg_cfg ADD COLUMN {k} {t} DEFAULT {d}" for k, (t, d) in EXTRA_COLS.items()]
    for stmt in stmts:
        try:
            c.execute(stmt)
        except sqlite3.OperationalError:
            pass
    _migrate(c)
    _conn = c


def db_exec(sql: str, params: tuple = ()):
    init_db()
    return _conn.execute(sql, params)


def ensure_user(uid: int, username: str = "", first_name: str = "") -> dict:
    row = db_exec("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row is None:
        db_exec("INSERT INTO users (id,username,first_name,created_at) VALUES (?,?,?,?)",
                (uid, username, first_name, datetime.now(timezone.utc).isoformat()))
    else:
        if username and username != row["username"]:
            db_exec("UPDATE users SET username=? WHERE id=?", (username, uid))
        if first_name and first_name != row["first_name"]:
            db_exec("UPDATE users SET first_name=? WHERE id=?", (first_name, uid))
    return dict(db_exec("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def user_row(uid: int) -> dict:
    r = db_exec("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(r) if r else {"id": uid, "username": "", "first_name": ""}


def users_count() -> int:
    return int(db_exec("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


def user_known(uid: int) -> bool:
    return db_exec("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone() is not None


_CFG_DEFAULTS = {"auto_join": 0, "auto_fetch": 0, "paused": 0, "auto_leave": 0, "profile": DEFAULT_PROFILE, "lang": "",
                 "leave_days": 8, "adv_mode": 0, "adv_gap": 0.0, "adv_batch": 0, "adv_conc": 0,
                 "night_start": 2, "night_end": 6, "warmup_ts": 0.0}


def get_cfg(uid: int) -> dict:
    c = _CFG_CACHE.get(uid)
    if c is None:
        c = dict(_CFG_DEFAULTS)
        for k, (_t, d) in EXTRA_COLS.items():
            c[k] = d
        r = db_exec("SELECT * FROM pg_cfg WHERE uid=?", (uid,)).fetchone()
        if r is not None:
            keys = set(r.keys())
            for k in _CFG_INT:
                if k in keys:
                    c[k] = int(r[k] or 0)
            prof = r["profile"] or DEFAULT_PROFILE
            c["profile"] = prof if prof in PROFILES else DEFAULT_PROFILE
            c["lang"] = r["lang"] or ""
            for k in _CFG_REAL:
                if k in keys:
                    c[k] = float(r[k] or 0.0)
            c["leave_days"] = max(MIN_LEAVE_DAYS, min(MAX_LEAVE_DAYS, c.get("leave_days") or 8))
            c["night_start"] = max(0, min(23, c.get("night_start", 2)))
            c["night_end"] = max(0, min(23, c.get("night_end", 6)))
            for k, (t, d) in EXTRA_COLS.items():
                if k in keys and r[k] is not None:
                    try:
                        c[k] = float(r[k]) if t == "REAL" else int(r[k])
                    except (TypeError, ValueError):
                        c[k] = d
        _CFG_CACHE[uid] = c
    return dict(c)


def set_cfg(uid: int, **kw: Any) -> None:
    db_exec("INSERT OR IGNORE INTO pg_cfg (uid, profile) VALUES (?, ?)", (uid, DEFAULT_PROFILE))
    for k, v in kw.items():
        if k in EXTRA_COLS:
            v = float(v) if EXTRA_COLS[k][0] == "REAL" else int(v)
            db_exec(f"UPDATE pg_cfg SET {k}=? WHERE uid=?", (v, uid))      # k comes from the EXTRA_COLS whitelist
        elif k == "leave_days":
            v = max(MIN_LEAVE_DAYS, min(MAX_LEAVE_DAYS, int(v)))
            db_exec("UPDATE pg_cfg SET leave_days=? WHERE uid=?", (v, uid))
        elif k in ("night_start", "night_end"):
            v = max(0, min(23, int(v)))
            db_exec(f"UPDATE pg_cfg SET {k}=? WHERE uid=?", (v, uid))
        elif k == "adv_batch":
            db_exec("UPDATE pg_cfg SET adv_batch=? WHERE uid=?", (max(0, min(HARD_MAX_BATCH, int(v))), uid))
        elif k == "adv_conc":
            db_exec("UPDATE pg_cfg SET adv_conc=? WHERE uid=?", (max(0, min(HARD_MAX_CONC, int(v))), uid))
        elif k in _CFG_INT:
            db_exec(f"UPDATE pg_cfg SET {k}=? WHERE uid=?", (1 if v else 0, uid))
        elif k in _CFG_TXT:
            db_exec(f"UPDATE pg_cfg SET {k}=? WHERE uid=?", (str(v), uid))
        elif k == "adv_gap":
            v = 0.0 if float(v) <= 0 else max(HARD_MIN_GAP, float(v))
            db_exec("UPDATE pg_cfg SET adv_gap=? WHERE uid=?", (v, uid))
        elif k in _CFG_REAL:
            db_exec(f"UPDATE pg_cfg SET {k}=? WHERE uid=?", (float(v), uid))
    _CFG_CACHE.pop(uid, None)
    if "auto_join" in kw or "auto_fetch" in kw or "paused" in kw:
        _NEXT_TICK.pop(uid, None)                     # state changed -> the scheduler looks at this account right away


def is_paused(uid: int) -> bool:
    return bool(get_cfg(uid)["paused"])


def leave_after_days(uid: int) -> int:
    """Per-account, web-configurable (clamped to [MIN_LEAVE_DAYS, MAX_LEAVE_DAYS])."""
    return int(get_cfg(uid).get("leave_days") or LEAVE_AFTER_DAYS)


def L(uid: int, en: str, fa: str) -> str:
    """Pick the user's language (English by default)."""
    return fa if get_cfg(uid)["lang"] == "fa" else en


def stat_add(uid: int, **inc: float) -> None:
    b = _STAT_BUF.setdefault(uid, {})
    for col, val in inc.items():
        if col in _STAT_COLS and val:
            b[col] = b.get(col, 0) + (float(val) if col == "gram" else int(val))


def stat_note(uid: int, text: str) -> None:
    now = time.time()
    _STAT_EV[uid] = (text[:100], now)
    ring = _RECENT.setdefault(uid, [])
    ring.append((now, text[:70]))
    if len(ring) > 8:
        del ring[:-8]


def stat_set_balance(uid: int, bal: str) -> None:
    db_exec("INSERT OR IGNORE INTO pg_stats (uid) VALUES (?)", (uid,))
    db_exec("UPDATE pg_stats SET balance=? WHERE uid=?", (bal[:30], uid))


def stat_get(uid: int) -> dict:
    flush_stats(uid)
    r = db_exec("SELECT * FROM pg_stats WHERE uid=?", (uid,)).fetchone()
    if r is None:
        return {"tasks_seen": 0, "joined": 0, "skipped": 0, "checked": 0, "completed": 0,
                "gram": 0.0, "errors": 0, "captchas": 0, "balance": "", "last_ev": "", "last_ev_ts": 0.0}
    return dict(r)


def stat_reset(uid: int) -> None:
    _STAT_BUF.pop(uid, None)
    _STAT_EV.pop(uid, None)
    db_exec("DELETE FROM pg_stats WHERE uid=?", (uid,))
    _RECENT.pop(uid, None)


def joins_record(uid: int, ref: str, title: str, peer_id: int, kind: str = "") -> None:
    db_exec("INSERT INTO pg_joins (uid,peer_id,ref,title,kind,joined_at) VALUES (?,?,?,?,?,?)",
            (uid, int(peer_id or 0), ref[:120], title[:120], kind, time.time()))
    _JS_CACHE.pop(uid, None)


def joins_all(uid: int) -> list:
    return list(db_exec("SELECT * FROM pg_joins WHERE uid=? ORDER BY joined_at", (uid,)).fetchall())


def joins_count(uid: int) -> int:
    r = db_exec("SELECT COUNT(*) AS n FROM pg_joins WHERE uid=?", (uid,)).fetchone()
    return int(r["n"]) if r else 0


def joins_since(uid: int, secs: float) -> int:
    now, key = time.time(), int(secs)
    ent = _JS_CACHE.get(uid, {}).get(key)
    if ent is not None and now - ent[0] < 3.0:
        return ent[1]
    r = db_exec("SELECT COUNT(*) AS n FROM pg_joins WHERE uid=? AND joined_at>?", (uid, now - secs)).fetchone()
    val = int(r["n"]) if r else 0
    _JS_CACHE.setdefault(uid, {})[key] = (now, val)
    return val


def joins_older_than(uid: int, secs: float) -> list:
    return list(db_exec("SELECT * FROM pg_joins WHERE uid=? AND joined_at<? ORDER BY joined_at",
                        (uid, time.time() - secs)).fetchall())


def bad_add(uid: int, ref: str, reason: str = "") -> None:
    db_exec("INSERT OR REPLACE INTO pg_bad (uid,ref,reason,ts) VALUES (?,?,?,?)", (uid, ref[:120], reason[:40], time.time()))


def is_bad(uid: int, ref: str) -> bool:
    return db_exec("SELECT 1 FROM pg_bad WHERE uid=? AND ref=?", (uid, ref[:120])).fetchone() is not None


def joins_delete(row_id: int) -> None:
    db_exec("DELETE FROM pg_joins WHERE id=?", (row_id,))


def db_housekeeping() -> None:
    db_exec("DELETE FROM pg_bad WHERE ts<?", (time.time() - 7 * 86400,))
    db_exec("PRAGMA wal_checkpoint(PASSIVE)")


# --- access control ---------------------------------------------------------
def owner_id() -> int:
    return int(CONFIG.get("owner_id") or 0)


def admin_set() -> set:
    s = set()
    for part in re.split(r"[,\s]+", str(CONFIG.get("admin_ids") or "")):
        if part.strip().isdigit():
            s.add(int(part))
    if owner_id():
        s.add(owner_id())
    return s


def is_admin(uid: int) -> bool:
    return uid in admin_set()


def is_banned(uid: int) -> bool:
    return db_exec("SELECT 1 FROM pg_ban WHERE uid=?", (uid,)).fetchone() is not None


def is_allowlisted(uid: int) -> bool:
    return db_exec("SELECT 1 FROM pg_allow WHERE uid=?", (uid,)).fetchone() is not None


def allowed(uid: int) -> bool:
    if is_admin(uid):
        return True
    if is_banned(uid):
        return False
    if not CONFIG["open_access"] and not is_allowlisted(uid):
        return False
    if user_known(uid):
        return True
    return users_count() < int(CONFIG["max_users"])


# --- per-account web dashboard token: a regular user's link only ever controls THEIR OWN account;
#     only the separate admin token (see WebPanel) can additionally open anyone's page. ---------------------
def user_web_token(uid: int) -> str:
    """Stateless dashboard token  <uid>.<epoch>.<hmac>  - nothing secret is stored in the database; bumping the
    epoch (reset / logout) or banning the user revokes every older link at once."""
    e = _epoch(uid)
    return f"{uid}.{e}.{_tok_sig(uid, e)}"


def reset_user_web_token(uid: int) -> str:
    e = _epoch(uid) + 1
    db_exec("INSERT OR REPLACE INTO pg_webep (uid, epoch) VALUES (?,?)", (uid, e))
    _EPOCH[uid] = e
    return user_web_token(uid)


def uid_of_web_token(tok: str) -> Optional[int]:
    if not tok or len(tok) > 96:
        return None
    parts = tok.split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    uid, e = int(parts[0]), int(parts[1])
    try:
        ok = hmac.compare_digest(_tok_sig(uid, e).encode(), parts[2].encode())
    except Exception:
        return None
    if not ok or e != _epoch(uid) or is_banned(uid):
        return None
    return uid


# ===========================================================================
# SESSION STORAGE (encrypted at rest)
# ===========================================================================
_FERNET: Any = None


def _fernet():
    global _FERNET
    if _FERNET is not None:
        return _FERNET or None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        _FERNET = False
        log.warning("`cryptography` not installed - Telegram sessions are stored UNENCRYPTED. "
                    "Run: pip install cryptography")
        return None
    key = _env_str("SESSION_KEY")
    if not key:
        if KEY_FILE.exists():
            key = KEY_FILE.read_text(encoding="utf-8").strip()
        else:
            key = Fernet.generate_key().decode()
            KEY_FILE.write_text(key, encoding="utf-8")
            try:
                os.chmod(KEY_FILE, 0o600)
            except Exception as _swallow_e:
                _swallow(_swallow_e)
    try:
        _FERNET = Fernet(key.encode())
    except Exception as e:
        log.error(f"bad SESSION_KEY: {e}")
        _FERNET = False
        return None
    return _FERNET


def user_session_file(uid: int) -> Path:
    p = USERS_DIR / str(uid)
    p.mkdir(parents=True, exist_ok=True)
    return p / "session.txt"


def save_user_session(uid: int, client: TelegramClient) -> None:
    try:
        p = user_session_file(uid)
        raw = StringSession.save(client.session)
        f = _fernet()
        data = ("enc:" + f.encrypt(raw.encode()).decode()) if f else raw
        tmp = p.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception as _swallow_e:
            _swallow(_swallow_e)
        os.replace(tmp, p)
    except Exception as e:
        log.error(f"save_session({uid}): {e}")


def load_user_session(uid: int) -> str:
    p = USERS_DIR / str(uid) / "session.txt"          # reading must not create folders (bug 16)
    try:
        data = p.read_text(encoding="utf-8").strip() if p.exists() else ""
    except Exception:
        return ""
    if data.startswith("enc:"):
        f = _fernet()
        if not f:
            log.error(f"u{uid}: session is encrypted but no key available")
            return ""
        try:
            return f.decrypt(data[4:].encode()).decode()
        except Exception:
            log.error(f"u{uid}: cannot decrypt session (wrong SESSION_KEY?)")
            return ""
    return data


def drop_user_session(uid: int) -> None:
    try:
        user_session_file(uid).unlink(missing_ok=True)
    except Exception as _swallow_e:
        _swallow(_swallow_e)


def list_stored_users() -> List[int]:
    out: List[int] = []
    for p in USERS_DIR.iterdir():
        if p.is_dir() and p.name.isdigit() and (p / "session.txt").exists():
            out.append(int(p.name))
    return sorted(out)


class Runtime:
    def __init__(self):
        self.bot: Optional[TelegramClient] = None
        self.stop_evt: Optional[asyncio.Event] = None
        self.watchdog: Optional[asyncio.Task] = None
        self.qr_wait_tasks: Dict[int, asyncio.Task] = {}
        self.gsem: Optional[asyncio.Semaphore] = None
        self.bot_sem: Optional[asyncio.Semaphore] = None
        self.edit_sem: Optional[asyncio.Semaphore] = None
        self.load_sem: Optional[asyncio.Semaphore] = None
        self.web: Optional[Any] = None


RT = Runtime()
ACCOUNTS: Dict[int, TelegramClient] = {}
PENDING: Dict[int, TelegramClient] = {}
_STATE: Dict[int, dict] = {}
_HANDLED: Dict[int, TelegramClient] = {}
_TICKING: set = set()
_LOADING: set = set()
PANELS: Dict[int, dict] = {}

_TGT: Dict[int, dict] = {}
_DEDUPE: Dict[int, Dict[Tuple[str, int, int], float]] = {}
_LOCKS: Dict[Tuple[int, str], asyncio.Lock] = {}
_FARM: Dict[int, dict] = {}
_URL_CLAIMS: Dict[int, Dict[str, float]] = {}

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def gsem() -> asyncio.Semaphore:
    if RT.gsem is None:
        RT.gsem = asyncio.Semaphore(max(4, int(CONFIG["global_conc"])))
    return RT.gsem


def _set_state(uid: int, d: dict) -> None:
    d["ts"] = time.time()
    _STATE[uid] = d


def cleanup_user(uid: int) -> None:
    for dct in (_FARM, _URL_CLAIMS, _DEDUPE, _TGT, _HANDLED, _STATE, PANELS, _RECENT):
        dct.pop(uid, None)
    for k in [k for k in _LOCKS if k[0] == uid]:
        _LOCKS.pop(k, None)
    _TICKING.discard(uid)


def _proxy_tuple() -> Optional[tuple]:
    if not CONFIG["use_proxy"]:
        return None
    try:
        import socks
        from urllib.parse import urlparse
        u = urlparse(CONFIG["proxy_url"])
        m = {"socks5": socks.SOCKS5, "socks4": socks.SOCKS4, "http": socks.HTTP}
        scheme = (u.scheme or "socks5").lower()
        if scheme not in m:
            return None
        return (m[scheme], u.hostname or "127.0.0.1", int(u.port or 1080), True, u.username, u.password)
    except Exception as e:
        log.warning(f"proxy: {e}")
        return None


def _api_creds() -> tuple:
    return (int(CONFIG.get("api_id") or 0) or PUBLIC_API_ID,
            str(CONFIG.get("api_hash") or "") or PUBLIC_API_HASH)


# user accounts: a LOW sleep threshold so floods are raised (and tracked) instead of silently slept
_USER_KW = dict(auto_reconnect=True, connection_retries=10, retry_delay=2,
                request_retries=5, flood_sleep_threshold=2)
_BOT_KW = dict(auto_reconnect=True, connection_retries=10, retry_delay=2,
               request_retries=5, flood_sleep_threshold=10)

_DEVICES = ["Pixel 7 Pro", "Pixel 8", "Galaxy S23", "Galaxy S22", "Xiaomi 13", "OnePlus 11"]


def _make_user_client(uid: int) -> TelegramClient:
    api_id, api_hash = _api_creds()
    return TelegramClient(
        StringSession(load_user_session(uid)), api_id, api_hash, proxy=_proxy_tuple(),
        device_model=_DEVICES[uid % len(_DEVICES)], system_version="14", app_version="10.2.0",
        lang_code="en", system_lang_code="en-US", **_USER_KW)


def _make_bot_client() -> TelegramClient:
    api_id, api_hash = _api_creds()
    return TelegramClient(str(BOT_SESSION_FILE), api_id, api_hash, proxy=_proxy_tuple(), **_BOT_KW)


def spawn(coro: Awaitable[Any], name: str) -> asyncio.Task:
    t = asyncio.create_task(coro, name=name)

    def _done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(f"task {name} failed: {type(exc).__name__}: {exc}")
    t.add_done_callback(_done)
    return t


# ===========================================================================
# BUTTONS (colored when this Telegram/Telethon build supports button styles)
# ===========================================================================
HAS_BTN_STYLE = hasattr(tltypes, "KeyboardButtonStyle")
_STYLE_OK = [HAS_BTN_STYLE]


def cb(text: str, data: bytes, style: Optional[str] = None):
    """Inline callback button. style: 'primary' (blue) | 'success' (green) | 'danger' (red).
    Falls back to a plain button when styles are not supported."""
    style = style or "primary"          # default = blue, so the whole keyboard is colored
    if style in ("primary", "success", "danger") and _STYLE_OK[0]:
        try:
            st = tltypes.KeyboardButtonStyle(**{f"bg_{style}": True})
            return tltypes.KeyboardButtonCallback(text=text, data=data, style=st)
        except Exception:
            _STYLE_OK[0] = False
    return Button.inline(text, data)


def _kb_sig(kb) -> tuple:
    return tuple((getattr(b, "text", ""), repr(getattr(b, "style", None))) for row in (kb or []) for b in row)


def _unstyle(kb):
    """Rebuild a keyboard with plain buttons (used when the server rejected the styled ones)."""
    if not kb:
        return kb
    out = []
    for row in kb:
        r = []
        for b in (row if isinstance(row, (list, tuple)) else [row]):
            if isinstance(b, tltypes.KeyboardButtonCallback):
                r.append(Button.inline(b.text, b.data))
            elif isinstance(b, tltypes.KeyboardButtonUrl):
                r.append(Button.url(b.text, b.url))
            else:
                r.append(b)
        out.append(r)
    return out


def _style_error(e: BaseException) -> bool:
    """True only when the error really looks like 'styled buttons are not accepted'."""
    s = f"{type(e).__name__} {e}".upper()
    return isinstance(e, (TypeError, ValueError, AttributeError)) or "BUTTON" in s or "STYLE" in s


def _disable_styles(e: BaseException) -> None:
    _STYLE_OK[0] = False
    log.warning(f"styled buttons rejected ({type(e).__name__}); using plain buttons")


# ===========================================================================
# SAFE SENDING
# ===========================================================================
def h(s: Any) -> str:
    return _html.escape(str(s or ""), quote=False)


async def _bot_send(uid: int, text: str, buttons=None) -> Optional[Any]:
    if RT.bot is None:
        return None
    if RT.bot_sem is None:
        RT.bot_sem = asyncio.Semaphore(15)
    async with RT.bot_sem:
        for _attempt in range(3):
            await _bot_throttle()
            try:
                return await RT.bot.send_message(uid, text, buttons=buttons, parse_mode="html", link_preview=False)
            except FloodWaitError as e:
                await asyncio.sleep(min(int(getattr(e, "seconds", 1) or 1) + 1, 30))
            except Exception as e:
                if buttons and _STYLE_OK[0] and HAS_BTN_STYLE and _style_error(e):
                    _disable_styles(e)
                    buttons = _unstyle(buttons)
                    continue
                return None
    return None


async def notify(uid: int, text: str) -> None:
    await _bot_send(uid, text)


async def notify_btn(uid: int, text: str, buttons) -> None:
    await _bot_send(uid, text, buttons)


# ===========================================================================
# FLOOD TRACKING + ADAPTIVE SPEED  (every code path reports through here)
# ===========================================================================
_WAIT_RE = re.compile(r"wait of (\d+) seconds?", re.I)


def flood_secs(exc: BaseException) -> int:
    """Seconds Telegram asked us to wait (0 if `exc` is not a flood/slow-mode style error)."""
    if isinstance(exc, FloodWaitError):
        return int(getattr(exc, "seconds", 0) or 30)
    name = type(exc).__name__
    s = getattr(exc, "seconds", None)
    if isinstance(s, int) and s > 0 and ("Flood" in name or "Wait" in name or "SlowMode" in name):
        return s
    m = _WAIT_RE.search(str(exc))
    if m:
        return int(m.group(1))
    if "PeerFlood" in name:
        return 1800
    return 0


def slow_eff(uid: int) -> float:
    """Current slow-down factor: 1.0 = full speed, 3.0 = 3x slower. Constant during the flood wait,
    then a linear ramp back to 1.0 over RECOVER_SECS."""
    f = _farm(uid)
    s0 = float(f.get("slow0", 1.0))
    if s0 <= 1.0:
        return 1.0
    now, end = time.time(), float(f.get("slow_end", 0.0))
    if now <= end:
        return s0
    prog = (now - end) / RECOVER_SECS
    if prog >= 1.0:
        f["slow0"] = 1.0
        return 1.0
    return s0 - (s0 - 1.0) * prog


def slow_left(uid: int) -> float:
    """Seconds until full speed again (0 when already at full speed)."""
    f = _farm(uid)
    if float(f.get("slow0", 1.0)) <= 1.0:
        return 0.0
    return max(0.0, float(f.get("slow_end", 0.0)) + RECOVER_SECS - time.time())


def set_flood(uid: int, wait: int) -> bool:
    """Register a flood wait. -> True when this is the first flood of a burst (= the user should be warned)."""
    f, now = _farm(uid), time.time()
    f["flood_until"] = max(f.get("flood_until", 0.0), now + int(wait) + 5)
    f["last_wait"], f["last_ts"] = float(wait), now
    fresh = now - f.get("flood_evt", 0.0) > 10      # several parallel tasks hit the same flood -> count once
    f["flood_evt"] = now
    if fresh:
        cur = slow_eff(uid)
        # a repeated flood can never be MILDER than the first one
        f["slow0"] = SLOW_FACTOR if cur < 1.5 else min(SLOW_MAX, max(SLOW_FACTOR, cur * 1.5))
        f["flood_n"] = int(f.get("flood_n", 0)) + 1
        f["slow_notify"] = True
    f["slow_end"] = f["flood_until"]
    _state_save(uid)
    if fresh:
        ts = f.setdefault("flood_times", [])
        ts.append(now)
        del ts[:-50]
        lim = int(get_cfg(uid).get("flood_stop", 0) or 0)
        if lim > 0 and len([x for x in ts if now - x < 6 * 3600]) >= lim:
            set_cfg(uid, auto_join=False, auto_fetch=False)
            stat_note(uid, "⛔ auto-stopped (repeated floods)")
            spawn(notify(uid, L(uid,
                                f"⛔ <b>Auto-stop</b> — {lim} flood waits in 6 hours. Your account needs a rest. "
                                "Open the dashboard, lower the speed or caps, then press Start.",
                                f"⛔ <b>توقف خودکار</b> — {lim} فلود در ۶ ساعت. اکانتت نیاز به استراحت داره. "
                                "داشبورد رو باز کن، سرعت یا سقف‌ها رو کم کن و بعد شروع رو بزن.")), f"fstop-{uid}")
    return fresh


async def absorb(uid: int, exc: BaseException, where: str = "") -> bool:
    """If `exc` is a flood wait: remember it, slow down 3x, warn the user. -> True when it was one."""
    w = flood_secs(exc)
    if not w:
        return False
    fresh = set_flood(uid, w)
    _bad(uid, w)
    stat_note(uid, f"flood wait {w}s ({where})" if where else f"flood wait {w}s")
    evt(uid, "FLOOD", f"{where}: {w}s  speed /{slow_eff(uid):.1f}", logging.WARNING)
    if fresh:
        sl = slow_eff(uid)
        n = int(_farm(uid).get("flood_n", 1))
        await warn_once(uid, "flood", L(
            uid,
            f"⚠️ <b>Telegram flood warning{' (#%d)' % n if n > 1 else ''}</b>\n"
            f"<blockquote>⏳ Required wait: <b>{_fmt_dur(w)}</b>\n"
            f"🐢 Speed cut to <b>1/{sl:.1f}</b> — join gap <b>{_jint(uid):.0f}s</b>\n"
            f"📈 After the wait the speed climbs back to 100% by itself (~{int(RECOVER_SECS // 60)} min).</blockquote>"
            "I continue automatically — nothing to do.",
            f"⚠️ <b>هشدار فلود تلگرام{' (#%d)' % n if n > 1 else ''}</b>\n"
            f"<blockquote>⏳ مدت انتظار: <b>{_fmt_dur(w)}</b>\n"
            f"🐢 سرعت <b>{sl:.1f} برابر</b> کم شد — فاصله عضویت <b>{_jint(uid):.0f} ثانیه</b>\n"
            f"📈 بعد از انتظار، سرعت خودکار تا ۱۰۰٪ برمی‌گرده (~{int(RECOVER_SECS // 60)} دقیقه).</blockquote>"
            "خودکار ادامه می‌دم — کاری لازم نیست."), 20)
    return True


# ===========================================================================
# PR GRAM LOGIC
# ===========================================================================
def pg_bot_name() -> str:
    return str(CONFIG["pg_bot"]).strip().lstrip("@")


def _flat(text: str) -> str:
    t = (text or "").translate(_DIGITS)
    t = re.sub(r"[‌‍‎‏‪-‮️]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _lock(uid: int, key: str) -> asyncio.Lock:
    return _LOCKS.setdefault((uid, key), asyncio.Lock())


def _seen_before(uid: int, kind: str, chat_id: int, msg_id: int) -> bool:
    d = _DEDUPE.setdefault(uid, {})
    now = time.time()
    for k in [k for k, ts in d.items() if now - ts > DEDUPE_TTL]:
        del d[k]
    k = (kind, chat_id, msg_id)
    if k in d:
        return True
    d[k] = now
    return False


def _farm(uid: int) -> dict:
    f = _FARM.get(uid)
    if f is None:
        f = _FARM[uid] = {"last_fetch": time.time() - random.uniform(0, 12), "flood_until": 0.0,
                          "captcha": None, "warned": {}, "t0": time.time(),
                          "sess_gram": 0.0, "sess_done": 0, "slow0": 1.0, "slow_end": 0.0,
                          "bot_until": 0.0, "bot_note": ""}
        _state_load(uid, f)
    return f


def _state_load(uid: int, f: dict) -> None:
    try:
        r = db_exec("SELECT * FROM pg_state WHERE uid=?", (uid,)).fetchone()
        if r is None:
            return
        now = time.time()
        f["flood_until"] = float(r["flood_until"] or 0.0)
        f["last_wait"] = float(r["last_wait"] or 0.0)
        f["last_ts"] = float(r["last_ts"] or 0.0)
        if r["cap_until"] and float(r["cap_until"]) > now:
            f["hour_cap"], f["cap_until"] = float(r["hour_cap"]), float(r["cap_until"])
        if r["day_until"] and float(r["day_until"]) > now:
            f["day_cap"], f["day_until"] = float(r["day_cap"]), float(r["day_until"])
        s0, se = float(r["slow0"] or 1.0), float(r["slow_end"] or 0.0)
        if s0 > 1.0 and se + RECOVER_SECS > now:
            f["slow0"], f["slow_end"] = s0, se
    except Exception as e:
        log.warning(f"state load u{uid}: {e}")


def _state_save(uid: int) -> None:
    f = _FARM.get(uid)
    if not f:
        return
    try:
        db_exec("INSERT OR REPLACE INTO pg_state (uid,flood_until,last_wait,last_ts,jint,mult,hour_cap,cap_until,"
                "day_cap,day_until,slow0,slow_end) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, float(f.get("flood_until", 0.0)), float(f.get("last_wait", 0.0)), float(f.get("last_ts", 0.0)),
                 0.0, 1.0, float(f.get("hour_cap", 0.0)), float(f.get("cap_until", 0.0)),
                 float(f.get("day_cap", 0.0)), float(f.get("day_until", 0.0)),
                 float(f.get("slow0", 1.0)), float(f.get("slow_end", 0.0))))
    except Exception as e:
        log.warning(f"state save u{uid}: {e}")


def _claim_url(uid: int, url: str) -> bool:
    d = _URL_CLAIMS.setdefault(uid, {})
    now = time.time()
    for k in [k for k, ts in d.items() if now - ts > URL_TTL]:
        del d[k]
    if url in d:
        return False
    d[url] = now
    return True


def _release_url(uid: int, url: str) -> None:
    _URL_CLAIMS.get(uid, {}).pop(url, None)


async def warn_once(uid: int, key: str, text: str, ttl: float = 3600.0) -> None:
    if key in ("flood", "budget", "botlimit") and not get_cfg(uid)["notify"]:
        return
    w = _farm(uid)["warned"]
    if time.time() - w.get(key, 0.0) < ttl:
        return
    w[key] = time.time()
    await notify(uid, text)


# --- message recognition ---------------------------------------------------
def _num(s: str) -> float:
    try:
        return float(s.replace(",", "").replace("٬", "").strip("."))
    except ValueError:
        return 0.0


def is_captcha_message(text: str) -> bool:
    t = _flat(text).lower()
    return "verify" in t and "human" in t


def is_completed_message(text: str) -> bool:
    t = _flat(text).lower()
    return "completed" in t and "gram" in t


# NOTE: a bare "try again later" is NOT a limit (failed Check alerts say that) - it needs a number / the word limit
LIMIT_RE = re.compile(
    r"too many|limit(?:ed)? (?:reached|exceeded)|reached (?:the |your )?(?:daily |hourly )?limit|"
    r"daily limit|hourly limit|slow down|flood|(?:try again|come back|wait)\D{0,25}\d",
    re.I)
_DUR_RE = re.compile(r"(\d+)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)\b", re.I)


def is_limit_message(text: str) -> bool:
    return bool(LIMIT_RE.search(_flat(text)))


def parse_wait_seconds(text: str) -> int:
    total = 0
    for n, unit in _DUR_RE.findall(_flat(text)):
        u = unit.lower()
        total += int(n) * (3600 if u.startswith("h") else 60 if u.startswith("m") else 1)
    return total


def parse_reward(text: str) -> float:
    m = re.search(r"\+\s*(\d[\d,.]*)\s*GRAM", _flat(text), re.I)
    return _num(m.group(1)) if m else 0.0


def parse_balance(text: str) -> str:
    m = re.search(r"Balance:\s*(\d[\d,.]*)", _flat(text), re.I)
    return m.group(1).rstrip(".") if m else ""


_TME = re.compile(r"(?:https?://)?(?:t|telegram)\.me/([^?#]+)", re.I)
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")


def parse_link(url: str) -> Tuple[str, str]:
    m = _TME.search((url or "").strip())
    if not m:
        return ("skip", "")
    path = m.group(1).strip("/")
    if path.startswith("+"):
        return ("invite", path[1:])
    if path.lower().startswith("joinchat/"):
        return ("invite", path.split("/", 1)[1])
    name = path.split("/")[0]
    if _USERNAME.fullmatch(name) and not name.lower().endswith("bot"):
        return ("public", name)
    return ("skip", "")


def _btn_rows(msg):
    return getattr(msg, "buttons", None) or []


_REWARD = re.compile(r"\+?\s*(\d[\d,.]*)")


def extract_tasks_ex(msg) -> List[dict]:
    out: List[dict] = []
    for i, row in enumerate(_btn_rows(msg)):
        url, chk, reward = None, None, 0.0
        for j, b in enumerate(row):
            u = getattr(b, "url", None)
            txt = _flat(getattr(b, "text", "") or "")
            if u and url is None:
                url = u
                m = _REWARD.search(txt)
                reward = _num(m.group(1)) if m else 0.0
            elif not u and "check" in txt.lower() and chk is None:
                chk = j
        if url and chk is not None:
            out.append({"row": i, "col": chk, "url": url, "reward": reward})
    return out


def extract_tasks(msg) -> List[Tuple[int, int, str]]:
    return [(t["row"], t["col"], t["url"]) for t in extract_tasks_ex(msg)]


def _locate(msg, url: str) -> Optional[Tuple[int, int]]:
    for t in extract_tasks_ex(msg):
        if t["url"] == url:
            return t["row"], t["col"]
    return None


def extract_pager(msg) -> Optional[Tuple[int, int, int, int]]:
    for i, row in enumerate(_btn_rows(msg)):
        texts = [_flat(getattr(b, "text", "") or "") for b in row]
        if ">" not in texts or "<" not in texts:
            continue
        nums = [int(t) for t in texts if t.isdigit()]
        cur = nums[1] if len(nums) >= 3 else (nums[0] if nums else 0)
        last = nums[-1] if nums else 0
        return i, texts.index(">"), cur, last
    return None


def find_category_button(msg) -> Optional[Tuple[int, int]]:
    want = _flat(str(CONFIG["pg_category"])).lower()
    for i, row in enumerate(_btn_rows(msg)):
        for j, b in enumerate(row):
            if getattr(b, "data", None) is None:
                continue
            if want and want in _flat(getattr(b, "text", "") or "").lower():
                return i, j
    return None


# --- bot entity -----------------------------------------------------------
async def ensure_targets(uid: int, client: TelegramClient) -> None:
    d = _TGT.setdefault(uid, {"bot_id": None, "next_try": 0.0})
    if d["bot_id"] or time.time() < d["next_try"]:
        return
    d["next_try"] = time.time() + RESOLVE_RETRY
    try:
        async with gsem():
            ent = await client.get_entity(pg_bot_name())
        d["bot_id"] = ent.id
        evt(uid, "ENT", f"bot @{pg_bot_name()} id {ent.id}")
    except DEAD_ERRORS:
        await handle_dead(uid)
    except Exception as e:
        if await absorb(uid, e, "resolve"):
            d["next_try"] = time.time() + max(RESOLVE_RETRY, flood_secs(e) + 5)
        else:
            evt(uid, "ENT", f"bot @{pg_bot_name()} failed: {type(e).__name__}", logging.WARNING)


# --- limits / pacing ---------------------------------------------------------
def is_smart(uid: int) -> bool:
    return get_cfg(uid)["profile"] == "smart"


def acct_age_days(uid: int) -> float:
    """Days since this account was first linked to the helper (warm-up clock for new / virtual numbers).
    A web "reset warm-up" action sets warmup_ts to now, which restarts this clock - useful when the person
    wants a fresh, cautious ramp-up for a newly re-used virtual number even on an account the helper saw before."""
    try:
        ts = datetime.fromisoformat(user_row(uid).get("created_at") or "")
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        linked_ts = ts.timestamp()
    except Exception:
        return 30.0
    wu = float(get_cfg(uid).get("warmup_ts") or 0.0)
    start = max(linked_ts, wu)
    return max(0.0, (time.time() - start) / 86400.0)


def smart_limits(uid: int) -> Tuple[int, int, float]:
    d = acct_age_days(uid)
    return NO_CAP, NO_CAP, max(8.0, 14.0 - d * 2.0)     # night factor is applied for every profile in eff_limits


def smart_rest(uid: int) -> float:
    """Seconds the account must stay idle: outside active hours, or during a human-like break."""
    sw = sched_wait(uid)
    if sw > 0:
        return sw
    cfg = get_cfg(uid)
    if not (is_smart(uid) or cfg["rest_on"]):
        return 0.0
    f, now = _farm(uid), time.time()
    if now < f.get("rest_until", 0.0):
        return f["rest_until"] - now
    be = f.get("burst_end", 0.0)
    if not be:
        f["burst_end"] = now + max(300, cfg["rest_every"] * 60 * random.uniform(0.7, 1.3))
        return 0.0
    if now >= be:
        f["rest_until"] = now + max(30, cfg["rest_len"] * 60 * random.uniform(0.7, 1.5))
        f["burst_end"] = 0.0
        return f["rest_until"] - now
    return 0.0


def _batch_size(uid: int) -> int:
    cfg = get_cfg(uid)
    if cfg["adv_mode"] and cfg["adv_batch"] > 0:
        return max(1, min(HARD_MAX_BATCH, int(cfg["adv_batch"])))
    n = random.randint(4, 8) if is_smart(uid) else int(CONFIG["pg_batch"])
    return max(1, min(HARD_MAX_BATCH, n))


def _check_delay(uid: int) -> float:
    """Pause between a join and pressing Check."""
    if is_smart(uid):
        return random.uniform(0.8, 2.2) * max(1.0, slow_eff(uid))
    return random.uniform(0.1, 0.3) * _mult(uid)


def eff_limits(uid: int) -> Tuple[int, int, float]:
    """(hourly cap, daily cap, join gap). NO_CAP = no fixed ceiling. The gap never goes below HARD_MIN_GAP."""
    cfg = get_cfg(uid)
    prof = cfg["profile"]
    hl, dl, gi = smart_limits(uid) if prof == "smart" else prof_limits(prof)
    if cfg["adv_mode"] and cfg["adv_gap"] > 0:
        gi = float(cfg["adv_gap"])
    gi = max(HARD_MIN_GAP, gi) * night_mult(uid)
    f, now = _farm(uid), time.time()
    if now < f.get("cap_until", 0.0):          # a REAL Telegram flood taught us the account's capacity
        hl = max(1, int(f.get("hour_cap", hl)))
    if now < f.get("day_until", 0.0):
        dl = max(1, int(f.get("day_cap", dl)))
    if cfg["cap_hour"] > 0:
        hl = min(hl, int(cfg["cap_hour"]))
    if cfg["cap_day"] > 0:
        dl = min(dl, int(cfg["cap_day"]))
    return hl, dl, gi * slow_eff(uid)


def budget_left(uid: int) -> int:
    hl, dl, _ = eff_limits(uid)
    if hl >= NO_CAP and dl >= NO_CAP:
        return NO_CAP
    return max(0, min(hl - joins_since(uid, 3600), dl - joins_since(uid, 86400)))


def budget_ok(uid: int) -> Tuple[bool, str]:
    if budget_left(uid) > 0:
        return True, ""
    hl, dl, _ = eff_limits(uid)
    return False, (f"join limit reached ({joins_since(uid, 3600)}/{hl if hl < NO_CAP else '∞'} h · "
                   f"{joins_since(uid, 86400)}/{dl if dl < NO_CAP else '∞'} day)")


def _jint(uid: int) -> float:
    return eff_limits(uid)[2]


def _mult(uid: int) -> float:
    """Multiplier for the small human-like delays: faster profiles shrink them, a flood stretches them."""
    fi = PROFILES.get(get_cfg(uid)["profile"], PROFILES[DEFAULT_PROFILE])[2]
    base = 1.3 if is_smart(uid) else max(0.35, min(1.0, fi))
    return base * slow_eff(uid)


def _gap(uid: int, lo: float, hi: float) -> float:
    return random.uniform(lo, hi) * _mult(uid)


def _bad(uid: int, wait: int = 0) -> None:
    """Learn Telegram's real capacity for this account from long flood waits."""
    f, now = _farm(uid), time.time()
    if wait >= 600:    # long wait -> the real hourly capacity is lower than ours
        f["hour_cap"] = max(5, int(joins_since(uid, 3600) * 0.9))
        f["cap_until"] = now + 3600
    if wait >= 3600:   # very long wait -> the real daily capacity too
        f["day_cap"] = max(10, int(joins_since(uid, 86400) * 0.9))
        f["day_until"] = now + 86400
    _state_save(uid)


async def _join_slot(uid: int) -> None:
    f = _farm(uid)
    async with _lock(uid, "pace"):
        wait = f.get("next_join", 0.0) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        base = _jint(uid)
        if is_smart(uid):
            g = base * random.uniform(0.7, 1.5)                # irregular rhythm
            if random.random() < 0.08:
                g += random.uniform(8, 25)                     # occasional short "got distracted" pause
        else:
            g = base * random.uniform(0.9, 1.25)
        f["next_join"] = time.time() + max(HARD_MIN_GAP, g)    # the hard floor also survives the jitter


def _fail_inc(uid: int, ref: str) -> int:
    d = _farm(uid).setdefault("fails", {})
    if len(d) > 500:
        d.clear()
    d[ref] = d.get(ref, 0) + 1
    return d[ref]


def _peer_of(obj: Any) -> int:
    try:
        return utils.get_peer_id(obj) if obj is not None else 0
    except Exception:
        return 0


async def probe_channel(uid: int, client: TelegramClient, kind: str, ref: str) -> Tuple[str, str, int, int, Any]:
    """Cheap pre-check that needs NO join slot.
    -> (status, title, peer_id, wait, entity); status: already | need | skip | flood"""
    try:
        if kind == "public":
            ent = await client.get_entity(ref)
            if not isinstance(ent, Channel):
                return "skip", ref, 0, 0, None
            try:
                await client(GetParticipantRequest(ent, "me"))
                return "already", ent.title, _peer_of(ent), 0, ent
            except UserNotParticipantError:
                pass
            return "need", ent.title, _peer_of(ent), 0, ent
        res = await client(CheckChatInviteRequest(ref))
        if isinstance(res, tltypes.ChatInviteAlready):
            ch = getattr(res, "chat", None)
            return "already", getattr(ch, "title", ref), _peer_of(ch), 0, None
        if getattr(res, "request_needed", False):   # needs admin approval -> cannot be joined automatically
            return "skip", ref, 0, 0, None
        return "need", getattr(res, "title", ref) or ref, 0, 0, None
    except DEAD_ERRORS:
        raise
    except SKIP_ERRORS:
        return "skip", ref, 0, 0, None
    except FULL_ERRORS:
        return "need", ref, 0, 0, None
    except Exception as e:
        w = flood_secs(e)
        if w:
            return "flood", ref, 0, w, None
        return "need", ref, 0, 0, None     # unknown probe problem: just try the join itself


async def do_join(uid: int, client: TelegramClient, kind: str, ref: str, ent: Any) -> Tuple[str, str, int, int]:
    """The actual join. -> (status, title, peer_id, wait); status: joined|already|skip|full|flood|error"""
    try:
        if kind == "public":
            if ent is None:
                ent = await client.get_entity(ref)
            await client(JoinChannelRequest(ent))
            return "joined", getattr(ent, "title", ref), _peer_of(ent), 0
        res = await client(ImportChatInviteRequest(ref))
        chats = getattr(res, "chats", None) or []
        ch = chats[0] if chats else None
        return "joined", getattr(ch, "title", ref), _peer_of(ch), 0
    except UserAlreadyParticipantError:
        return "already", ref, 0, 0
    except SKIP_ERRORS:
        return "skip", ref, 0, 0
    except FULL_ERRORS:
        return "full", ref, 0, 0
    except DEAD_ERRORS:
        raise
    except Exception as e:
        w = flood_secs(e)
        if w:
            return "flood", ref, 0, w
        evt(uid, "JOIN", f"{kind}:{ref} {type(e).__name__}: {e}", logging.WARNING)
        return "error", ref, 0, 0


async def _refetch(uid: int, client: TelegramClient, chat_id: int, msg_id: int):
    await asyncio.sleep(max(0.6, 0.3 * _mult(uid)))
    try:
        return await client.get_messages(chat_id, ids=msg_id)
    except DEAD_ERRORS:
        raise
    except Exception as e:
        await absorb(uid, e, "refresh")
        return None


async def handle_menu(uid: int, client: TelegramClient, msg) -> None:
    try:
        cfg = get_cfg(uid)
        f = _farm(uid)
        if cfg["paused"] or not cfg["auto_join"] or time.time() < f["flood_until"] or time.time() < f["bot_until"]:
            return
        pos = find_category_button(msg)
        if pos is None:
            return
        if time.time() - f.get("menu_ts", 0.0) < 1.5:    # only guards against a double-fired event
            return
        f["menu_ts"] = time.time()
        f["hops"] = 0
        await asyncio.sleep(_gap(uid, 0.15, 0.4))
        if is_paused(uid):
            return
        try:
            await msg.click(pos[0], pos[1])
            stat_note(uid, f"category: {CONFIG['pg_category']}")
            evt(uid, "MENU", f"opened category {CONFIG['pg_category']}")
        except BotResponseTimeoutError:
            pass
    except DEAD_ERRORS:
        await handle_dead(uid)
    except Exception as e:
        if not await absorb(uid, e, "menu"):
            log.warning(f"menu u{uid}: {type(e).__name__}: {e}")


async def _join_batch(uid: int, client: TelegramClient, batch: List[dict], ctx: dict) -> list:
    """Probe -> (wait for a join slot) -> join -> press Check right away, for every task of the batch in parallel.
    Already-member / dead links never consume a join slot. A crashing task releases its URL claim."""
    sem = asyncio.Semaphore(conc_of(uid))
    f = _farm(uid)

    def _stop() -> bool:
        return is_paused(uid) or time.time() < f["flood_until"]

    async def one(t: dict):
        try:
            async with sem:
                if _stop():
                    return t, ("abort", "", 0, 0)
                await _probe_slot(uid)
                async with gsem():
                    pstat, title, peer_id, pwait, ent = await probe_channel(uid, client, t["kind"], t["ref"])
                if pstat == "need":
                    if _stop():
                        return t, ("abort", "", 0, 0)
                    await _join_slot(uid)
                    if _stop():
                        return t, ("abort", "", 0, 0)
                    async with gsem():
                        res = await do_join(uid, client, t["kind"], t["ref"], ent)
                    status, title2, peer_id, _w = res
                    title = title2 if (title2 and title2 != t["ref"]) else title
                    res = (status, title, peer_id, _w)
                else:
                    res = (pstat, title, peer_id, pwait)
                status, title, peer_id, _wait = res
                if status in ("joined", "already"):
                    t["title"] = title
                    if status == "joined":   # count it right away so the hourly/daily budget is always exact
                        joins_record(uid, t["ref"], title, peer_id, t["kind"])
                        stat_add(uid, joined=1)
                        evt(uid, "JOIN", f"{title[:40]}")
                    await asyncio.sleep(_check_delay(uid))   # let Telegram register the membership
                    async with ctx["lock"]:                  # one click at a time on the shared message
                        if is_paused(uid):
                            t["check"] = "paused"
                        else:
                            t["check"], ctx["cur"] = await _press_check(uid, client, ctx["cur"], ctx["chat"], ctx["id"], t["url"])
                return t, res
        except DEAD_ERRORS:
            _release_url(uid, t["url"])
            raise
        except Exception as e:
            _release_url(uid, t["url"])
            w = flood_secs(e)
            if w:
                return t, ("flood", "", 0, w)
            evt(uid, "JOIN", f"task crashed {type(e).__name__}: {e}", logging.WARNING)
            return t, ("error", "", 0, 0)

    return await asyncio.gather(*[one(t) for t in batch], return_exceptions=True)


async def _press_check(uid: int, client: TelegramClient, cur, chat_id: int, msg_id: int, url: str):
    pos = _locate(cur, url)
    if pos is None:
        return "ok", cur
    row, col = pos
    for attempt in (1, 2):
        try:
            ans = await cur.click(row, col)
            if attempt == 1:
                stat_add(uid, checked=1)
            alert = (getattr(ans, "message", "") or "") if ans else ""
            if alert:
                stat_note(uid, f"check: {_flat(alert)[:80]}")
                if is_limit_message(alert):
                    await _apply_bot_limit(uid, alert)
                    return "flood", cur
        except BotResponseTimeoutError:
            if attempt == 1:
                stat_add(uid, checked=1)
        except DEAD_ERRORS:
            raise
        except Exception as e:
            if await absorb(uid, e, "check"):
                return "flood", cur
            stat_add(uid, errors=1)
            evt(uid, "CHECK", f"{type(e).__name__}: {e}", logging.WARNING)
        nxt = await _refetch(uid, client, chat_id, msg_id)
        if nxt is None:
            return "unknown", cur
        cur = nxt
        pos = _locate(cur, url)
        if pos is None:
            return "ok", cur
        row, col = pos
        await asyncio.sleep(_gap(uid, 0.4, 0.9))
    return "fail", cur


class _Flood(Exception):
    """Internal stand-in carrying a wait (a FloodWaitError cannot be built without a request object)."""
    def __init__(self, seconds: int):
        super().__init__(f"A wait of {int(seconds)} seconds is required")
        self.seconds = int(seconds)


async def handle_task_list(uid: int, client: TelegramClient, msg) -> None:
    f = _farm(uid)
    try:
        if not extract_tasks(msg):
            return
        f["last_task_ts"] = time.time()
        f["empty"] = 0
        async with _lock(uid, "farm"):
            cur = msg
            chat_id, msg_id = msg.chat_id, msg.id
            for _round in range(400):
                if cur is None:
                    return
                cfg = get_cfg(uid)
                if cfg["paused"] or not cfg["auto_join"] or time.time() < f["flood_until"] or time.time() < f["bot_until"]:
                    return
                if smart_rest(uid) > 0:
                    return
                f["last_task_ts"] = time.time()

                batch: List[dict] = []
                bsz = _batch_size(uid)
                for t in sorted(extract_tasks_ex(cur), key=lambda x: -x["reward"]):
                    if len(batch) >= bsz:
                        break
                    if not _claim_url(uid, t["url"]):
                        continue
                    kind, ref = parse_link(t["url"])
                    if kind == "skip" or is_bad(uid, ref):
                        stat_add(uid, skipped=1)
                        continue
                    t["kind"], t["ref"] = kind, ref
                    batch.append(t)

                if not batch:
                    pg = extract_pager(cur)
                    if not pg or pg[2] >= pg[3] or f.get("hops", 0) >= int(CONFIG["pg_max_pages"]):
                        return
                    f["hops"] = f.get("hops", 0) + 1
                    await asyncio.sleep(_gap(uid, 0.15, 0.4))
                    try:
                        await cur.click(pg[0], pg[1])
                    except BotResponseTimeoutError:
                        pass
                    except DEAD_ERRORS:
                        raise
                    except Exception as e:
                        if not await absorb(uid, e, "paging"):
                            evt(uid, "PAGE", f"{type(e).__name__}: {e}", logging.WARNING)
                        return
                    cur = await _refetch(uid, client, chat_id, msg_id) or cur
                    continue

                room = budget_left(uid)
                if room <= 0:
                    for t in batch:
                        _release_url(uid, t["url"])
                    why = budget_ok(uid)[1]
                    stat_note(uid, why)
                    await warn_once(uid, "budget", L(uid, f"⏳ {why}. I continue automatically later.",
                                                     f"⏳ سقف مجاز پر شد ({joins_since(uid, 3600)} در ساعت · {joins_since(uid, 86400)} در روز). بعداً خودکار ادامه می‌دم."))
                    return
                for t in batch[room:]:
                    _release_url(uid, t["url"])
                batch = batch[:room]
                stat_add(uid, tasks_seen=len(batch))

                ctx = {"cur": cur, "chat": chat_id, "id": msg_id, "lock": asyncio.Lock()}
                results = await _join_batch(uid, client, batch, ctx)
                cur = ctx["cur"] or cur
                ready: List[dict] = []
                stop = full = False
                for item in results:
                    if isinstance(item, BaseException):
                        if DEAD_ERRORS and isinstance(item, DEAD_ERRORS):
                            raise item
                        if not await absorb(uid, item, "join"):
                            stat_add(uid, errors=1)
                            evt(uid, "JOIN", f"batch error {type(item).__name__}: {item}", logging.WARNING)
                        else:
                            stop = True
                        continue
                    t, (status, title, peer_id, wait) = item
                    url, ref = t["url"], t["ref"]
                    if status == "abort":
                        _release_url(uid, url)
                        stop = True
                    elif status == "flood":
                        _release_url(uid, url)
                        await absorb(uid, _Flood(wait), "join")
                        stop = True
                    elif status == "full":
                        _release_url(uid, url)
                        full = True
                    elif status == "skip":
                        bad_add(uid, ref, "skip")
                        stat_add(uid, skipped=1)
                    elif status == "error":
                        stat_add(uid, errors=1)
                        if _fail_inc(uid, ref) >= 3:
                            bad_add(uid, ref, "errors")
                    else:   # joined | already -> its Check was already pressed inside _join_batch
                        t["title"] = title
                        ready.append(t)

                if full:
                    f["full_until"] = time.time() + 600        # back-off instead of retrying every round (bug 9)
                    if cfg["auto_leave"]:
                        await warn_once(uid, "full", L(uid, f"🧹 Channel limit reached — leaving channels older than {leave_after_days(uid)} days.",
                                                       f"🧹 سقف تعداد کانال پر شد — کانال‌های قدیمی‌تر از {leave_after_days(uid)} روز ترک می‌شن."), 600)
                        spawn(leave_old(uid, client, True), f"room-{uid}")
                    else:
                        set_cfg(uid, auto_join=False)
                        await notify(uid, L(uid, "🛑 This account reached Telegram's maximum number of channels. Auto-join is OFF — "
                                                 "use <b>Leave ALL</b> or enable <b>Auto-leave</b>, then start again.",
                                            "🛑 این اکانت به حداکثر تعداد کانال تلگرام رسید. پیوستن خودکار خاموش شد — "
                                            "از «ترک همه» یا «ترک خودکار» استفاده کن و دوباره شروع کن."))
                    stop = True

                for t in ready:
                    res = t.get("check", "unknown")
                    title = t.get("title", t["ref"])
                    if res == "paused":
                        return
                    if res == "ok":
                        evt(uid, "OK", f"confirmed: {title[:40]}")
                        stat_note(uid, f"✔ confirmed: {title[:40]}")
                    elif res == "fail":
                        stat_add(uid, errors=1)
                        evt(uid, "FAIL", f"not confirmed: {title[:40]}", logging.WARNING)
                        stat_note(uid, f"✖ not confirmed: {title[:40]}")
                        if _fail_inc(uid, t["ref"]) >= 2:
                            bad_add(uid, t["ref"], "check-fail")
                    elif res == "flood":
                        return
                if ready and not stop:
                    f["again"] = True      # productive round -> fetch the next list immediately
                if stop:
                    return
    except DEAD_ERRORS:
        await handle_dead(uid)
    except Exception as e:
        if not await absorb(uid, e, "tasks"):
            log.warning(f"task list u{uid}: {type(e).__name__}: {e}")


# --- completion / human check / bot-side limits --------------------------------
async def handle_completed(uid: int, msg) -> None:
    if _seen_before(uid, "done", msg.chat_id or 0, msg.id):
        return
    text = msg.raw_text or ""
    gain = parse_reward(text)
    bal = parse_balance(text)
    stat_add(uid, completed=1, gram=gain)
    _f = _farm(uid)
    _f["sess_gram"] = _f.get("sess_gram", 0.0) + gain
    _f["sess_done"] = _f.get("sess_done", 0) + 1
    if bal:
        stat_set_balance(uid, bal)
    stat_note(uid, f"+{gain:g} GRAM")
    evt(uid, "DONE", f"+{gain:g} GRAM balance {bal or '?'}")


async def handle_captcha(uid: int, msg) -> None:
    if _seen_before(uid, "cap", msg.chat_id or 0, msg.id):
        return
    _farm(uid)["captcha"] = (msg.chat_id, msg.id)
    stat_add(uid, captchas=1)
    stat_note(uid, "⏸ human check")
    was_paused = is_paused(uid)
    set_cfg(uid, paused=True)
    evt(uid, "HUMAN", "verification requested - paused", logging.WARNING)
    if was_paused:
        return
    await notify_btn(
        uid,
        L(uid,
          "⏸ <b>PR GRAM asks you to verify that you are human.</b>\n\n"
          f"Everything is paused. Open the chat with @{pg_bot_name()} in your normal Telegram app, "
          "solve the puzzle yourself, then press <b>Resume</b>.",
          "⏸ <b>PR GRAM می‌خواد ثابت کنی انسان هستی.</b>\n\n"
          f"همه‌چیز متوقف شد. چت @{pg_bot_name()} رو توی تلگرام خودت باز کن، پازل رو خودت حل کن و بعد "
          "<b>ادامه</b> رو بزن."),
        [[cb(L(uid, "▶️ Resume", "▶️ ادامه"), b"pg:resume", "success")]])


async def _apply_bot_limit(uid: int, text: str) -> None:
    flat = _flat(text)
    secs = min(parse_wait_seconds(flat) or 900, MAX_BOT_WAIT)
    f = _farm(uid)
    f["bot_until"] = time.time() + secs + 5
    f["bot_note"] = flat[:90]
    stat_note(uid, "🤖 bot limit")
    evt(uid, "BOTLIM", f"{secs}s :: {flat[:80]}", logging.WARNING)
    await warn_once(uid, "botlimit", L(
        uid,
        f"🤖 <b>PR GRAM reported a limit</b> — pausing requests for <b>{_fmt_dur(secs)}</b>.\n<i>{h(flat[:160])}</i>",
        f"🤖 <b>PR GRAM محدودیت اعلام کرد</b> — درخواست‌ها برای <b>{_fmt_dur(secs)}</b> متوقف شد.\n<i>{h(flat[:160])}</i>"), 900)


async def handle_bot_limit(uid: int, msg) -> None:
    if _seen_before(uid, "lim", msg.chat_id or 0, msg.id):
        return
    await _apply_bot_limit(uid, msg.raw_text or "")


async def resume_flow(uid: int) -> None:
    set_cfg(uid, paused=False)
    f = _farm(uid)
    f["last_fetch"] = 0.0
    client, info = ACCOUNTS.get(uid), f.get("captcha")
    if client is None or not info:
        return
    try:
        m = await client.get_messages(info[0], ids=info[1])
        if m is not None and getattr(m, "buttons", None):
            await m.click(text=re.compile(r"continue", re.I).match)
    except Exception as e:
        await absorb(uid, e, "resume")
    f["captcha"] = None


# --- fetching tasks --------------------------------------------------------
async def request_tasks(uid: int, client: TelegramClient) -> bool:
    try:
        _farm(uid)["hops"] = 0
        async with gsem():
            await client.send_message(pg_bot_name(), str(CONFIG["pg_earn_text"]))
        evt(uid, "FETCH", f"sent '{CONFIG['pg_earn_text']}' to @{pg_bot_name()}")
        return True
    except DEAD_ERRORS:
        raise
    except Exception as e:
        if not await absorb(uid, e, "fetch"):
            stat_add(uid, errors=1)
            stat_note(uid, f"fetch failed: {type(e).__name__}")
        return False


async def farm_tick(uid: int, client: TelegramClient) -> None:
    cfg = get_cfg(uid)
    f, now = _farm(uid), time.time()
    # tell the user once when the speed is fully restored after a flood
    if f.get("slow_notify") and now > f["flood_until"] and slow_eff(uid) <= 1.0:
        f["slow_notify"] = False
        await notify(uid, L(uid, "✅ <b>Back to full speed</b> — the flood slow-down is over.",
                            "✅ <b>سرعت دوباره کامل شد</b> — دوره کاهش سرعت فلود تموم شد."))
    if cfg["auto_leave"] and now - f.get("leave_ts", 0.0) > 900 and not _lock(uid, "leave").locked():
        f["leave_ts"] = now
        if joins_older_than(uid, leave_after_days(uid) * 86400):
            spawn(leave_old(uid, client, True), f"autoleave-{uid}")
    if cfg["paused"] or not (cfg["auto_fetch"] and cfg["auto_join"]):
        return
    if not (_TGT.get(uid) or {}).get("bot_id"):
        return
    if now < f["flood_until"] or now < f["bot_until"] or now < f.get("full_until", 0.0):
        return
    if _lock(uid, "farm").locked() or not budget_ok(uid)[0]:
        return
    if smart_rest(uid) > 0:
        return
    base = max(2, int(CONFIG["pg_fetch_interval"]))
    interval = base * (1 + min(int(f.get("empty", 0)), 4)) * random.uniform(0.9, 1.25) * max(1.0, slow_eff(uid) ** 0.5)
    if is_smart(uid):
        interval = random.uniform(7, 20) * max(1.0, slow_eff(uid) ** 0.5)
    if f.pop("again", False):
        interval = random.uniform(2, 6) if is_smart(uid) else 0.8
    if now - f["last_fetch"] < interval:
        return
    if f["last_fetch"] and f.get("last_task_ts", 0.0) < f["last_fetch"]:
        f["empty"] = min(int(f.get("empty", 0)) + 1, 6)
    f["last_fetch"] = now
    try:
        await request_tasks(uid, client)
    except DEAD_ERRORS:
        await handle_dead(uid)


async def _dialog_channels(client: TelegramClient) -> Dict[int, Any]:
    out: Dict[int, Any] = {}
    async for d in client.iter_dialogs():
        if isinstance(d.entity, Channel):
            out[utils.get_peer_id(d.entity)] = d.entity
    return out


async def leave_rows(uid: int, client: TelegramClient, rows: list, label: str, quiet: bool = False) -> None:
    if not rows:
        if not quiet:
            await notify(uid, L(uid, f"🧹 Nothing to leave ({h(label)}).", "🧹 چیزی برای ترک کردن نیست."))
        return
    lock = _lock(uid, "leave")
    if lock.locked():
        if not quiet:
            await notify(uid, L(uid, "🧹 A leave job is already running.", "🧹 یک عملیات ترک در حال اجراست."))
        return
    async with lock:
        if not quiet:
            await notify(uid, L(uid, f"🧹 Leaving <b>{len(rows)}</b> channel(s) ({h(label)}). About {len(rows) * 3 // 60 + 1} min…",
                                f"🧹 در حال ترک <b>{len(rows)}</b> کانال… حدود {len(rows) * 3 // 60 + 1} دقیقه."))
        try:
            async with gsem():
                dmap = await _dialog_channels(client)
        except DEAD_ERRORS:
            await handle_dead(uid)
            return
        except Exception as e:
            # IMPORTANT: without the dialog list we cannot tell "left" from "unknown" -> change nothing
            if not await absorb(uid, e, "dialogs"):
                evt(uid, "LEAVE", f"dialog list failed: {type(e).__name__}", logging.WARNING)
            if not quiet:
                await notify(uid, L(uid, "❌ Could not load your chats; nothing was changed. Try again later.",
                                    "❌ لیست چت‌ها لود نشد؛ هیچ تغییری داده نشد. بعداً دوباره تلاش کن."))
            return
        left = gone = failed = 0
        for r in rows:
            try:
                ent = dmap.get(int(r["peer_id"])) if r["peer_id"] else None
                if ent is None and r["kind"] == "public" and r["ref"]:
                    try:
                        ent = await client.get_entity(r["ref"])
                        if ent is not None and utils.get_peer_id(ent) not in dmap:
                            ent = None   # resolvable but we are not a member -> already gone
                    except Exception:
                        ent = None
                if ent is None:
                    joins_delete(r["id"])
                    gone += 1
                    continue
                async with gsem():
                    await client(LeaveChannelRequest(ent))
                joins_delete(r["id"])
                left += 1
            except DEAD_ERRORS:
                await handle_dead(uid)
                return
            except Exception as e:
                if await absorb(uid, e, "leave"):
                    break
                _nm = type(e).__name__
                if "NotParticipant" in _nm or "ChannelPrivate" in _nm or "ChatIdInvalid" in _nm:
                    joins_delete(r["id"])                                  # already gone = success, not failure (bug 10)
                    gone += 1
                    continue
                failed += 1
            await asyncio.sleep(random.uniform(1.5, 3.5))
        if not quiet or left:
            await notify(uid, L(uid,
                                f"🧹 Done · left <b>{left}</b>" + (f" · already gone {gone}" if gone else "")
                                + (f" · failed {failed}" if failed else "") + f" · still tracked <b>{joins_count(uid)}</b>.",
                                f"🧹 انجام شد · ترک شد <b>{left}</b>" + (f" · قبلاً خارج‌شده {gone}" if gone else "")
                                + (f" · ناموفق {failed}" if failed else "") + f" · هنوز ردیابی‌شده <b>{joins_count(uid)}</b>."))


async def leave_old(uid: int, client: TelegramClient, quiet: bool = False) -> None:
    await leave_rows(uid, client, joins_older_than(uid, leave_after_days(uid) * 86400),
                     f"older than {leave_after_days(uid)} days", quiet)


async def leave_all(uid: int, client: TelegramClient) -> None:
    set_cfg(uid, auto_join=False, auto_fetch=False)
    await leave_rows(uid, client, joins_all(uid), "ALL tracked channels")


# --- event wiring ----------------------------------------------------------
def attach_user_handlers(uid: int, client: TelegramClient) -> None:
    if _HANDLED.get(uid) is client:
        return
    _HANDLED[uid] = client
    spawn(ensure_targets(uid, client), f"resolve-{uid}")

    async def _on_msg(event, edited: bool = False):
        try:
            if not event.is_private:
                return
            if not (_TGT.get(uid) or {}).get("bot_id"):
                await ensure_targets(uid, client)
            bot_id = (_TGT.get(uid) or {}).get("bot_id")
            if not bot_id or event.sender_id != bot_id:
                return
            msg = event.message
            text = msg.raw_text or ""
            has_btn = bool(getattr(msg, "buttons", None))
            if is_captcha_message(text):
                await handle_captcha(uid, msg)
            elif is_completed_message(text) and not (has_btn and extract_tasks(msg)):
                if not edited:
                    await handle_completed(uid, msg)
            elif has_btn and extract_tasks(msg):
                if edited and _lock(uid, "farm").locked():
                    return
                spawn(handle_task_list(uid, client, msg), f"tasks-{uid}-{msg.id}")
            elif has_btn and find_category_button(msg):
                spawn(handle_menu(uid, client, msg), f"menu-{uid}-{msg.id}")
            elif len(text) < 400 and is_limit_message(text):
                await handle_bot_limit(uid, msg)
            elif not edited:
                evt(uid, "BOT", _flat(text)[:100])
        except DEAD_ERRORS:
            await handle_dead(uid)
        except Exception as e:
            if not await absorb(uid, e, "handler"):
                log.warning(f"handler u{uid}: {type(e).__name__}: {e}")

    @client.on(events.NewMessage(incoming=True))
    async def _new(event):
        await _on_msg(event, False)

    @client.on(events.MessageEdited(incoming=True))
    async def _edit(event):
        await _on_msg(event, True)


async def handle_dead(uid: int) -> None:
    c = ACCOUNTS.pop(uid, None)
    if c is None and uid not in _HANDLED:
        return   # already handled
    lang_uid = uid
    cleanup_user(uid)
    if c:
        try:
            await c.disconnect()
        except Exception as _swallow_e:
            _swallow(_swallow_e)
    drop_user_session(uid)
    set_cfg(uid, auto_join=False, auto_fetch=False)
    await notify(lang_uid, L(lang_uid, "⚠️ Your account session was revoked. Log in again with /start.",
                             "⚠️ نشست اکانتت باطل شد. با /start دوباره وارد شو."))


# ===========================================================================
# UI  (HTML text, blockquotes, usage bars, colored buttons, live panel)
# ===========================================================================
DIV = "━━━━━━━━━━━━━━━"


def _fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    hh, r = divmod(sec, 3600)
    m, sc = divmod(r, 60)
    return (f"{hh}h " if hh else "") + (f"{m}m " if (m or hh) else "") + f"{sc}s"


def _fmt_coarse(sec: float) -> str:
    """Countdown that changes rarely, so the live panel is not edited every few seconds."""
    sec = int(max(0, sec))
    if sec >= 3600:
        return f"{sec // 3600}h {(sec % 3600) // 60}m"
    if sec >= 120:
        return f"{sec // 60}m"
    return f"{max(10, ((sec + 9) // 10) * 10)}s" if sec > 0 else "0s"


def bar(v: float, mx: float, n: int = 10) -> str:
    mx = max(1.0, float(mx))
    k = max(0, min(n, int(round(n * float(v) / mx))))
    if v > 0 and k == 0:
        k = 1
    return "▰" * k + "▱" * (n - k)


def _ago(ts: float) -> str:
    return _fmt_coarse(time.time() - ts) if ts else "—"


def status_of(uid: int) -> Tuple[str, str]:
    cfg, f, now = get_cfg(uid), _farm(uid), time.time()
    if cfg["paused"]:
        if f.get("captcha"):
            return "🔴", L(uid, "Paused — human check", "متوقف — تایید انسان بودن")
        return "🔴", L(uid, "Paused", "متوقف")
    if now < f["flood_until"]:
        return "🟡", L(uid, f"Flood wait · {_fmt_coarse(f['flood_until'] - now)} left",
                       f"فلود ویت · {_fmt_coarse(f['flood_until'] - now)} مانده")
    if now < f["bot_until"]:
        return "🟡", L(uid, f"Bot limit · {_fmt_coarse(f['bot_until'] - now)} left",
                       f"محدودیت ربات · {_fmt_coarse(f['bot_until'] - now)} مانده")
    if cfg["auto_join"] and budget_left(uid) <= 0:
        return "🟡", L(uid, "Join limit reached — waiting", "سقف عضویت پر شد — در انتظار")
    if cfg["auto_join"] and is_smart(uid) and now < f.get("rest_until", 0.0):
        return "😴", L(uid, f"Resting (human-like break) · {_fmt_coarse(f['rest_until'] - now)}",
                       f"استراحت (شبیه انسان) · {_fmt_coarse(f['rest_until'] - now)}")
    sl = slow_eff(uid)
    if cfg["auto_join"] and sl > 1.05:
        return "🟠", L(uid, f"Running slowed ×{sl:.1f} — speeding up", f"در حال کار با سرعت کم ×{sl:.1f} — در حال افزایش")
    if cfg["auto_join"]:
        return "🟢", L(uid, "Running", "در حال کار")
    return "⚪️", L(uid, "Idle — press Start", "غیرفعال — شروع رو بزن")


def prof_label(uid: int, name: str) -> str:
    return {"safe": L(uid, "🐢 Safe", "🐢 امن"), "normal": L(uid, "⚖️ Normal", "⚖️ متعادل"),
            "fast": L(uid, "🚀 Fast", "🚀 سریع"), "smart": L(uid, "🧠 Smart", "🧠 هوشمند"), "max": L(uid, "⚡ Max", "⚡ حداکثر")}.get(name, name)


def limits_block(uid: int) -> str:
    f, now = _farm(uid), time.time()
    hl, dl, _ = eff_limits(uid)
    hu, du = joins_since(uid, 3600), joins_since(uid, 86400)
    rows = []
    if now < f["flood_until"]:
        rows.append(L(uid, f"⏳ Flood: <b>{_fmt_coarse(f['flood_until'] - now)}</b> left (asked {_fmt_dur(f.get('last_wait', 0))})",
                      f"⏳ فلود: <b>{_fmt_coarse(f['flood_until'] - now)}</b> مانده (درخواست {_fmt_dur(f.get('last_wait', 0))})"))
    elif f.get("last_ts"):
        rows.append(L(uid, f"✅ Flood: none now · last {_fmt_dur(f.get('last_wait', 0))}, {_ago(f['last_ts'])} ago",
                      f"✅ فلود: الان نیست · آخرین {_fmt_dur(f.get('last_wait', 0))}، {_ago(f['last_ts'])} پیش"))
    else:
        rows.append(L(uid, "✅ Flood: none so far", "✅ فلود: تا الان نداشتی"))
    sl = slow_eff(uid)
    if sl > 1.05:
        pct = int(round(100 / sl))
        rows.append(L(uid, f"🐢 Speed  {bar(pct, 100)}  <b>{pct}%</b> · full in {_fmt_coarse(slow_left(uid))}",
                      f"🐢 سرعت  {bar(pct, 100)}  <b>{pct}٪</b> · کامل تا {_fmt_coarse(slow_left(uid))}"))
    else:
        rows.append(L(uid, f"⚡ Speed  {bar(100, 100)}  <b>100%</b>", f"⚡ سرعت  {bar(100, 100)}  <b>۱۰۰٪</b>"))
    if hl >= NO_CAP and dl >= NO_CAP:
        rows.append(L(uid, f"🕐 Hour <b>{hu}</b> joined · 📅 Day <b>{du}</b> joined — <i>no fixed ceiling</i>",
                      f"🕐 ساعت <b>{hu}</b> عضویت · 📅 روز <b>{du}</b> عضویت — <i>بدون سقف ثابت</i>"))
    else:
        rows.append(L(uid, "🕐 Hour", "🕐 ساعت") + f"  {bar(hu, hl)}  <b>{hu}/{hl}</b>")
        rows.append(L(uid, "📅 Day ", "📅 روز ") + f"  {bar(du, dl)}  <b>{du}/{dl}</b>")
    caps = []
    if now < f.get("cap_until", 0.0):
        caps.append(L(uid, f"Telegram taught a lower hourly pace for {_fmt_coarse(f['cap_until'] - now)}",
                      f"تلگرام سرعت ساعتی پایین‌تری رو تا {_fmt_coarse(f['cap_until'] - now)} یاد داده"))
    if now < f.get("day_until", 0.0):
        caps.append(L(uid, f"Telegram taught a lower daily pace for {_fmt_coarse(f['day_until'] - now)}",
                      f"تلگرام سرعت روزانهٔ پایین‌تری رو تا {_fmt_coarse(f['day_until'] - now)} یاد داده"))
    if caps:
        rows.append("⚠️ " + " · ".join(caps))
    if is_smart(uid):
        rs = max(0.0, f.get("rest_until", 0.0) - now)
        rows.append(L(uid, f"🧠 Smart · warm-up day <b>{acct_age_days(uid):.1f}</b>" + (f" · break {_fmt_coarse(rs)}" if rs else " · working"),
                      f"🧠 هوشمند · روز گرم‌کردن <b>{acct_age_days(uid):.1f}</b>" + (f" · استراحت {_fmt_coarse(rs)}" if rs else " · در حال کار")))
    rows.append(L(uid, f"⏱ Join gap <b>{_jint(uid):.0f}s</b> · {prof_label(uid, get_cfg(uid)['profile'])}",
                  f"⏱ فاصله عضویت <b>{_jint(uid):.0f} ثانیه</b> · {prof_label(uid, get_cfg(uid)['profile'])}"))
    if now < f["bot_until"]:
        rows.append(L(uid, f"🤖 Bot limit: <b>{_fmt_coarse(f['bot_until'] - now)}</b> — <i>{h(f.get('bot_note', ''))}</i>",
                      f"🤖 محدودیت ربات: <b>{_fmt_coarse(f['bot_until'] - now)}</b> — <i>{h(f.get('bot_note', ''))}</i>"))
    return "\n".join(rows)


def recent_block(uid: int) -> str:
    ring = _RECENT.get(uid) or []
    if not ring:
        return "—"
    return "\n".join(f"<code>{datetime.fromtimestamp(ts).strftime('%H:%M:%S')}</code> {h(tx)}" for ts, tx in ring[-6:][::-1])


def main_text(uid: int) -> str:
    head = f"🪙 <b>PR GRAM Helper</b>  <code>v{VERSION}</code>\n{DIV}\n"
    if uid not in ACCOUNTS:
        return head + L(
            uid,
            "\n<b>Welcome!</b> Link your own Telegram account to start earning.\n\n"
            "<blockquote>📷 <b>QR</b> — scan from <i>Settings › Devices</i>\n"
            "📱 <b>Phone</b> — code arrives in Telegram or SMS</blockquote>\n"
            "🔒 Your session is stored encrypted and only used for your own account.",
            "\n<b>خوش اومدی!</b> برای شروع، اکانت تلگرام خودت رو وصل کن.\n\n"
            "<blockquote>📷 <b>QR</b> — از <i>تنظیمات › دستگاه‌ها</i> اسکن کن\n"
            "📱 <b>شماره</b> — کد داخل تلگرام یا پیامک میاد</blockquote>\n"
            "🔒 نشست تو رمزنگاری‌شده ذخیره می‌شه و فقط برای اکانت خودت استفاده می‌شه.")
    u = user_row(uid)
    cfg, s = get_cfg(uid), stat_get(uid)
    f = _farm(uid)
    em, st = status_of(uid)
    ready = bool((_TGT.get(uid) or {}).get("bot_id"))
    hrs = max((time.time() - f.get("t0", time.time())) / 3600.0, 1 / 60)
    rate = f.get("sess_gram", 0.0) / hrs
    on, off = "🟢", "⚪️"
    last = h(s["last_ev"]) or "—"
    if s.get("last_ev_ts"):
        last += f"  <i>({_ago(s['last_ev_ts'])})</i>"
    parts = [
        f"{em} <b>{h(st)}</b>",
        "",
        f"👤 <b>{h(u.get('first_name')) or '—'}</b>  ·  <code>{uid}</code>",
        f"🤖 <code>@{h(pg_bot_name())}</code>" + ("" if ready else L(uid, "  ·  ⚠️ not resolved yet", "  ·  ⚠️ هنوز شناسایی نشده")),
        "",
        L(uid, "<b>🛡 Limits & speed</b>", "<b>🛡 محدودیت و سرعت</b>"),
        f"<blockquote>{limits_block(uid)}</blockquote>",
        L(uid, "<b>💰 Earnings</b>", "<b>💰 درآمد</b>"),
        "<blockquote>" + L(
            uid,
            f"🪙 GRAM earned  <b>{s['gram']:,.0f}</b>\n🏦 Balance  <b>{h(s['balance']) or '—'}</b>\n"
            f"🚀 This session  <b>+{f.get('sess_gram', 0.0):,.0f}</b>  ·  <b>{rate:,.0f}</b>/h",
            f"🪙 GRAM کسب‌شده  <b>{s['gram']:,.0f}</b>\n🏦 موجودی  <b>{h(s['balance']) or '—'}</b>\n"
            f"🚀 این نشست  <b>+{f.get('sess_gram', 0.0):,.0f}</b>  ·  <b>{rate:,.0f}</b> در ساعت") + "</blockquote>",
        L(uid, "<b>📊 Activity</b>", "<b>📊 فعالیت</b>"),
        "<blockquote expandable>" + L(
            uid,
            f"📋 Tasks seen  <b>{s['tasks_seen']}</b>\n➕ Joined  <b>{s['joined']}</b>   ⏭ Skipped  <b>{s['skipped']}</b>\n"
            f"☑️ Checks  <b>{s['checked']}</b>   🏁 Completed  <b>{s['completed']}</b>\n"
            f"🧩 Human checks  <b>{s['captchas']}</b>   🛑 Errors  <b>{s['errors']}</b>\n"
            f"📚 Tracked channels  <b>{joins_count(uid)}</b>",
            f"📋 تسک دیده‌شده  <b>{s['tasks_seen']}</b>\n➕ عضو شده  <b>{s['joined']}</b>   ⏭ رد شده  <b>{s['skipped']}</b>\n"
            f"☑️ چک زده  <b>{s['checked']}</b>   🏁 کامل‌شده  <b>{s['completed']}</b>\n"
            f"🧩 تایید انسان  <b>{s['captchas']}</b>   🛑 خطا  <b>{s['errors']}</b>\n"
            f"📚 کانال‌های ردیابی‌شده  <b>{joins_count(uid)}</b>") + "</blockquote>",
        L(uid, "<b>📜 Recent events</b>", "<b>📜 رویدادهای اخیر</b>"),
        f"<blockquote expandable>{recent_block(uid)}</blockquote>",
        L(uid,
          f"{on if cfg['auto_join'] else off} Join   {on if cfg['auto_fetch'] else off} Fetch   {on if cfg['auto_leave'] else off} Leave 8d+",
          f"{on if cfg['auto_join'] else off} عضویت   {on if cfg['auto_fetch'] else off} دریافت   {on if cfg['auto_leave'] else off} ترک ۸ روزه"),
        f"🕒 {last}",
        L(uid, "<i>🔄 live panel — updates by itself</i>", "<i>🔄 پنل زنده — خودکار به‌روز می‌شه</i>"),
    ]
    return head + "\n".join(parts)


def admin_text() -> str:
    flush_stats()
    now = time.time()
    online = len(ACCOUNTS)
    running = paused = flooded = slowed = 0
    for u in list(ACCOUNTS):
        cfg = get_cfg(u)
        if cfg["paused"]:
            paused += 1
        elif cfg["auto_join"]:
            running += 1
        if _farm(u).get("flood_until", 0.0) > now:
            flooded += 1
        if slow_eff(u) > 1.05:
            slowed += 1
    r = db_exec("SELECT COALESCE(SUM(gram),0) AS g, COALESCE(SUM(joined),0) AS j, "
                "COALESCE(SUM(completed),0) AS c, COALESCE(SUM(errors),0) AS e FROM pg_stats").fetchone()
    banned = db_exec("SELECT COUNT(*) AS n FROM pg_ban").fetchone()["n"]
    top = db_exec("SELECT s.uid AS uid, s.gram AS gram, u.first_name AS nm FROM pg_stats s "
                  "LEFT JOIN users u ON u.id=s.uid ORDER BY s.gram DESC LIMIT 5").fetchall()
    toplines = "\n".join(f"{i}. {h(t['nm']) or t['uid']} — <b>{float(t['gram']):,.0f}</b>" for i, t in enumerate(top, 1)) or "—"
    return (f"🛠 <b>Admin</b>  <code>v{VERSION}</code>\n{DIV}\n\n"
            f"<blockquote>👥 Registered  <b>{users_count()}</b> / {CONFIG['max_users']}\n"
            f"🟢 Online accounts  <b>{online}</b>\n"
            f"▶️ Running <b>{running}</b>  ⏸ Paused <b>{paused}</b>  ⏳ Flood <b>{flooded}</b>  🐢 Slowed <b>{slowed}</b>\n"
            f"🔓 Access  <b>{'open' if CONFIG['open_access'] else 'invite-only'}</b>   🚫 Banned <b>{banned}</b></blockquote>\n"
            f"<blockquote>🪙 Total GRAM  <b>{float(r['g']):,.0f}</b>\n"
            f"➕ Joins <b>{int(r['j'])}</b>  🏁 Completed <b>{int(r['c'])}</b>  🛑 Errors <b>{int(r['e'])}</b></blockquote>\n"
            f"<b>🏆 Top earners</b>\n{toplines}\n\n"
            "<code>/ban ID  /unban ID  /allow ID  /disallow ID  /open  /closed</code>")


def render(uid: int, view: str) -> Tuple[str, list]:
    if view == "admin" and is_admin(uid):                      # /admin works again (bug 7)
        kb = [[cb("🔄 Refresh", b"adm:view", "primary")]]
        _au = dash_url(uid, True)
        if _au:
            kb.append([ub("🛠 Admin dashboard", _au, "primary")])
        kb.append([cb("◀️ Back", b"nav:main", "primary")])
        return admin_text(), kb
    if uid not in ACCOUNTS:
        kb = [[cb(L(uid, "📷 Login with QR", "📷 ورود با QR"), b"auth:qr", "primary")],
              [cb(L(uid, "📱 Login with phone", "📱 ورود با شماره"), b"auth:phone", "success")]]
        if is_admin(uid) and dash_url(uid, True):
            kb.append([ub("🛠 Admin dashboard", dash_url(uid, True), "primary")])
        return main_text(uid), kb
    if view == "logout":
        return (L(uid, "🚪 <b>Log out?</b>\n\nThe session is terminated and deleted from this server. Your statistics stay saved.",
                  "🚪 <b>خروج از حساب؟</b>\n\nنشست بسته و از این سرور پاک می‌شه. آمار تو ذخیره می‌مونه."),
                [[cb(L(uid, "✅ Yes, log out", "✅ بله، خارج شو"), b"auth:logout_yes", "danger")],
                 [cb(L(uid, "◀️ Cancel", "◀️ انصراف"), b"nav:main", "primary")]])
    cfg = get_cfg(uid)
    running = bool(cfg["auto_join"] and cfg["auto_fetch"])
    em, st = status2(uid)
    kb = [[cb(f"{em} {st}"[:44], b"pg:view", {"🟢": "success", "🔴": "danger"}.get(em, "primary"))],
          [cb(L(uid, "⏹ Stop", "⏹ توقف"), b"pg:stop", "danger") if running
           else cb(L(uid, "▶️ Start", "▶️ شروع"), b"pg:start", "success")]]
    url = dash_url(uid)
    if url:
        kb.append([ub(L(uid, "🖥 Open dashboard", "🖥 باز کردن داشبورد"), url, "primary")])
    if is_admin(uid) and dash_url(uid, True):
        kb.append([ub("🛠 Admin dashboard", dash_url(uid, True), "primary")])
    kb.append([cb(L(uid, "🚪 Logout", "🚪 خروج"), b"auth:logout", "danger")])
    return compact_text(uid), kb


# --- panels (the dashboard message keeps itself up to date) ------------------
def _panel_set(uid: int, chat_id: int, msg_id: int, view: str, text: str, kb) -> None:
    PANELS[uid] = {"chat": chat_id, "id": msg_id, "view": view, "sig": hash((text, _kb_sig(kb))),
                   "ts": time.time(), "seen": time.time(), "fails": 0}


async def _edit_sem():
    if RT.edit_sem is None:
        RT.edit_sem = asyncio.Semaphore(4)
    return RT.edit_sem


async def ui_edit(event, uid: int, view: str) -> None:
    for attempt in (1, 2):
        text, kb = render(uid, view)
        try:
            await event.edit(text, buttons=kb, parse_mode="html", link_preview=False)
            _panel_set(uid, event.chat_id or uid, event.message_id, view, text, kb)
            return
        except MessageNotModifiedError:
            _panel_set(uid, event.chat_id or uid, event.message_id, view, text, kb)
            return
        except FloodWaitError:
            return
        except Exception as e:
            if attempt == 1 and _STYLE_OK[0] and HAS_BTN_STYLE and _style_error(e):
                _disable_styles(e)
                continue
            try:
                m = await event.respond(text, buttons=kb, parse_mode="html", link_preview=False)
                _panel_set(uid, getattr(m, "chat_id", None) or uid, m.id, view, text, kb)
            except Exception as _swallow_e:
                _swallow(_swallow_e)
            return


async def ui_send(uid: int, view: str = "main", respond_to=None) -> None:
    old = PANELS.get(uid)
    text, kb = render(uid, view)
    m = None
    for _attempt in range(3):
        try:
            if respond_to is not None:
                m = await respond_to.respond(text, buttons=kb, parse_mode="html", link_preview=False)
            elif RT.bot is not None:
                m = await RT.bot.send_message(uid, text, buttons=kb, parse_mode="html", link_preview=False)
            break
        except FloodWaitError as e:
            await asyncio.sleep(min(int(getattr(e, "seconds", 1) or 1) + 1, 20))
        except Exception as e:
            if _STYLE_OK[0] and HAS_BTN_STYLE and _style_error(e):
                _disable_styles(e)
                text, kb = render(uid, view)
                continue
            return
    if m is None:
        return
    _panel_set(uid, getattr(m, "chat_id", None) or uid, m.id, view, text, kb)
    if old and old.get("id") and old["id"] != m.id and RT.bot is not None:   # keep the chat tidy: one live panel
        try:
            await RT.bot.delete_messages(old["chat"], [old["id"]])
        except Exception as _swallow_e:
            _swallow(_swallow_e)


async def _refresh_panel(uid: int) -> None:
    p = PANELS.get(uid)
    if not p or p["view"] != "main" or uid not in ACCOUNTS or RT.bot is None:
        return
    async with await _edit_sem():
        text, kb = render(uid, "main")
        sig = hash((text, _kb_sig(kb)))
        p["ts"] = time.time()
        if sig == p["sig"]:
            return
        await _bot_throttle()
        for attempt in (1, 2):
            try:
                await RT.bot.edit_message(p["chat"], p["id"], text, buttons=kb, parse_mode="html", link_preview=False)
                p["sig"], p["fails"] = sig, 0
                return
            except MessageNotModifiedError:
                p["sig"] = sig
                return
            except FloodWaitError as e:
                p["ts"] = time.time() + min(int(getattr(e, "seconds", 5) or 5), 120)
                return
            except MessageIdInvalidError:
                PANELS.pop(uid, None)
                return
            except Exception as e:
                if attempt == 1 and _STYLE_OK[0] and HAS_BTN_STYLE and _style_error(e):
                    _disable_styles(e)
                    text, kb = render(uid, "main")
                    sig = hash((text, _kb_sig(kb)))
                    continue
                p["fails"] = p.get("fails", 0) + 1
                if p["fails"] >= 3:
                    PANELS.pop(uid, None)
                log.warning(f"panel u{uid}: {type(e).__name__}")
                return


async def _panel_tick() -> None:
    now = time.time()
    interval = max(15.0, len(PANELS) * 0.7)
    due = []
    for u, p in list(PANELS.items()):
        if now - p["seen"] > PANEL_IDLE:
            PANELS.pop(u, None)
        elif p["view"] == "main" and now - p["ts"] >= interval:
            due.append(u)
    for u in due[:8]:
        PANELS[u]["ts"] = now + 1
        spawn(_refresh_panel(u), f"panel-{u}")


async def announce_state(uid: int) -> None:
    await asyncio.sleep(1.0 + random.uniform(0, max(8.0, len(ACCOUNTS) * 0.12)))
    if uid in ACCOUNTS:
        await ui_send(uid, "main")


async def answer(event, text: str = "", alert: bool = False) -> None:
    try:
        event._answered = True
        await asyncio.wait_for(event.answer(text, alert=alert), timeout=5.0)
    except Exception as _swallow_e:
        _swallow(_swallow_e)


# ===========================================================================
# QR LOGIN
# ===========================================================================
def _render_qr_png(url: str) -> Optional[bytes]:
    if not HAS_QRCODE:
        return None
    try:
        qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        log.warning(f"QR render failed: {e}")
        return None


def qr_caption(uid: int) -> str:
    return L(uid,
             f"📷 <b>QR Login</b>\n{DIV}\n\n<b>Method A — Scan</b>\n1. Open Telegram on your phone\n"
             "2. <b>Settings › Devices › Link Desktop Device</b>\n3. <b>Scan</b> this QR\n\n"
             "<b>Method B — No scan</b>\nTap the button below.\n\n⏱ The QR refreshes automatically.\n"
             "⚠️ <b>Never send this QR to anyone.</b>",
             f"📷 <b>ورود با QR</b>\n{DIV}\n\n<b>روش الف — اسکن</b>\n۱. تلگرام رو روی گوشی باز کن\n"
             "۲. <b>تنظیمات › دستگاه‌ها › اتصال دستگاه</b>\n۳. این QR رو <b>اسکن</b> کن\n\n"
             "<b>روش ب — بدون اسکن</b>\nدکمه پایین رو بزن.\n\n⏱ QR خودکار تازه می‌شه.\n"
             "⚠️ <b>این QR رو به هیچ‌کس نفرست.</b>")


async def _send_qr(uid: int, url: str):
    png = _render_qr_png(url)
    btn = Button.url(L(uid, "🔐 Confirm Login (no scan)", "🔐 تایید ورود (بدون اسکن)"), url)
    cap = qr_caption(uid)
    if png:
        bio = io.BytesIO(png)
        bio.name = "qr.png"
        try:
            return await RT.bot.send_file(uid, bio, caption=cap, parse_mode="html", buttons=[[btn]])
        except Exception as e:
            log.warning(f"QR send failed: {e}")
    return await RT.bot.send_message(uid, f"{cap}\n\n<code>{h(url)}</code>", parse_mode="html", buttons=[[btn]])


async def _qr_wait_task(uid: int, qr, last_msg) -> None:
    bot = RT.bot
    if bot is None:
        return
    deadline = time.time() + 240.0

    async def _del(m):
        if m is not None:
            try:
                await m.delete()
            except Exception as _swallow_e:
                _swallow(_swallow_e)

    async def _ask_2fa():
        await _del(last_msg)
        _set_state(uid, {"login": "2fa", "client": PENDING.get(uid)})
        try:
            await bot.send_message(uid, L(uid, "🔐 Send your two-factor (2FA) password:", "🔐 رمز دومرحله‌ای (2FA) رو بفرست:"),
                                   buttons=[[cb(L(uid, "✖️ Cancel", "✖️ انصراف"), b"nav:main", "danger")]])
        except Exception as _swallow_e:
            _swallow(_swallow_e)

    async def _expired():
        await _del(last_msg)
        await _drop_pending(uid)
        try:
            await bot.send_message(uid, L(uid, "⏱ <b>QR expired.</b> Send /start to try again.",
                                          "⏱ <b>QR منقضی شد.</b> برای تلاش دوباره /start رو بفرست."), parse_mode="html")
        except Exception as _swallow_e:
            _swallow(_swallow_e)

    try:
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise asyncio.TimeoutError
            try:
                await qr.wait(timeout=min(28.0, left))
                break
            except asyncio.TimeoutError:
                if time.time() >= deadline:
                    raise
                try:
                    await qr.recreate()
                except SessionPasswordNeededError:
                    await _ask_2fa()
                    return
                except Exception:
                    await _expired()
                    return
                new = await _send_qr(uid, qr.url)
                await _del(last_msg)
                last_msg = new
    except asyncio.TimeoutError:
        await _expired()
        return
    except SessionPasswordNeededError:
        await _ask_2fa()
        return
    except asyncio.CancelledError:
        await _del(last_msg)
        return
    except Exception as e:
        await _del(last_msg)
        await _drop_pending(uid)
        await notify(uid, f"❌ QR: <code>{h(type(e).__name__)}</code>")
        return
    await _del(last_msg)
    client = PENDING.get(uid)
    if client is None:
        return
    await _finish_login(uid, client)
    await notify(uid, L(uid, "✅ <b>Logged in!</b>", "✅ <b>وارد شدی!</b>"))
    await ui_send(uid, "main")


async def _drop_pending(uid: int) -> None:
    _STATE.pop(uid, None)
    pc = PENDING.pop(uid, None)
    if pc is not None and ACCOUNTS.get(uid) is not pc:
        try:
            await pc.disconnect()
        except Exception as _swallow_e:
            _swallow(_swallow_e)


async def _handle_qr_login(event, uid: int) -> None:
    if not HAS_QRCODE:
        await event.respond("❌ QR login requires <code>qrcode</code> and <code>Pillow</code>.", parse_mode="html")
        return
    old = RT.qr_wait_tasks.pop(uid, None)
    if old and not old.done():
        old.cancel()
    _STATE.pop(uid, None)
    try:
        client = PENDING.get(uid) or _make_user_client(uid)
        PENDING[uid] = client
        if not client.is_connected():
            await client.connect()
        if await client.is_user_authorized():
            await _finish_login(uid, client)
            await ui_send(uid, "main", respond_to=event)
            return
        qr = await client.qr_login()
        first = await _send_qr(uid, qr.url)
    except Exception as e:
        await _drop_pending(uid)
        w = flood_secs(e)
        await event.respond(L(uid, f"⏳ Telegram asks to wait {_fmt_dur(w)} before a new login." if w
                              else f"❌ <code>{h(type(e).__name__)}</code>",
                              f"⏳ تلگرام می‌گه {_fmt_dur(w)} صبر کن." if w else f"❌ <code>{h(type(e).__name__)}</code>"),
                            parse_mode="html")
        return
    RT.qr_wait_tasks[uid] = spawn(_qr_wait_task(uid, qr, first), f"qr-wait-{uid}")


# ===========================================================================
# LOGIN FINISH / LOGOUT
# ===========================================================================
async def _finish_login(uid: int, client: TelegramClient) -> None:
    try:
        me = await client.get_me()
    except Exception as e:
        log.error(f"get_me u{uid}: {type(e).__name__}: {e}")
        await _drop_pending(uid)
        await notify(uid, L(uid, "❌ Login could not be completed. Send /start and try again.",
                            "❌ ورود کامل نشد. /start رو بفرست و دوباره تلاش کن."))
        raise LoginAborted(type(e).__name__)
    ACCOUNTS[uid] = client
    PENDING.pop(uid, None)
    save_user_session(uid, client)
    ensure_user(uid, username=me.username or "", first_name=me.first_name or "")
    if not get_cfg(uid)["warmup_ts"]:
        set_cfg(uid, warmup_ts=time.time())       # the warm-up clock starts at the real login
    attach_user_handlers(uid, client)
    await ensure_targets(uid, client)
    spawn(_warm(client), f"warm-{uid}")
    evt(uid, "LOGIN", "account logged in")


async def _warm(client: TelegramClient) -> None:
    try:
        await client.get_dialogs(limit=50)
    except Exception as _swallow_e:
        _swallow(_swallow_e)


async def do_logout(uid: int) -> None:
    t = RT.qr_wait_tasks.pop(uid, None)
    if t and not t.done():
        t.cancel()
    old_panel = PANELS.get(uid)
    c = ACCOUNTS.pop(uid, None)
    pc = PENDING.pop(uid, None)
    cleanup_user(uid)
    set_cfg(uid, auto_join=False, auto_fetch=False, paused=False)
    for cl in (c, pc):
        if cl is not None:
            try:
                await cl.log_out()
            except Exception as _swallow_e:
                _swallow(_swallow_e)
            try:
                await cl.disconnect()
            except Exception as _swallow_e:
                _swallow(_swallow_e)
    drop_user_session(uid)
    reset_user_web_token(uid)                      # old dashboard links die with the session
    if old_panel and RT.bot is not None:
        try:
            await RT.bot.delete_messages(old_panel["chat"], [old_panel["id"]])
        except Exception as _swallow_e:
            _swallow(_swallow_e)
    await notify(uid, L(uid, "✅ Logged out.", "✅ خارج شدی."))
    await ui_send(uid, "main")


async def kick_user(uid: int) -> None:
    c = ACCOUNTS.pop(uid, None)
    cleanup_user(uid)
    set_cfg(uid, auto_join=False, auto_fetch=False)
    if c is not None:
        try:
            await c.disconnect()
        except Exception as _swallow_e:
            _swallow(_swallow_e)


# ===========================================================================
# BOT ROUTING
# ===========================================================================
async def route_cb(event, data: str) -> None:
    """Telegram keeps only: Login (QR / phone), Start / Stop / Resume, status refresh, Logout, admin refresh.
    Everything else lives in the web dashboard."""
    uid = event.sender_id
    ensure_user(uid)
    p = PANELS.get(uid)
    if p:
        p["seen"] = time.time()

    if data == "adm:view":
        if not is_admin(uid):
            await answer(event, "⛔", alert=True)
            return
        await ui_edit(event, uid, "admin")
        return
    if data == "nav:main":
        _STATE.pop(uid, None)
        await ui_edit(event, uid, "main")
        return
    if data == "auth:qr":
        try:
            await event.edit(L(uid, "📷 Preparing QR…", "📷 در حال ساخت QR…"),
                             buttons=[[cb(L(uid, "◀️ Back", "◀️ بازگشت"), b"nav:main")]])
        except Exception as _swallow_e:
            _swallow(_swallow_e)
        await _handle_qr_login(event, uid)
        return
    if data == "auth:phone":
        t = RT.qr_wait_tasks.pop(uid, None)
        if t and not t.done():
            t.cancel()
        _set_state(uid, {"login": "phone"})
        try:
            await event.edit(L(uid, "📱 Send your phone number in international format, e.g. <code>+989121234567</code>.",
                               "📱 شماره‌ات رو با فرمت بین‌المللی بفرست، مثلاً <code>+989121234567</code>."),
                             buttons=[[cb(L(uid, "✖️ Cancel", "✖️ انصراف"), b"nav:main", "danger")]], parse_mode="html")
        except Exception as _swallow_e:
            _swallow(_swallow_e)
        return
    if data == "auth:logout":
        await ui_edit(event, uid, "logout" if uid in ACCOUNTS else "main")
        return
    if data == "auth:logout_yes":
        if uid in ACCOUNTS:
            try:
                await event.edit(L(uid, "⏳ Logging out…", "⏳ در حال خروج…"), buttons=None)
            except Exception as _swallow_e:
                _swallow(_swallow_e)
            spawn(do_logout(uid), f"logout-{uid}")
        else:
            await ui_edit(event, uid, "main")
        return
    if data.startswith("pg:"):
        if uid not in ACCOUNTS:
            await answer(event, L(uid, "Please log in first.", "اول وارد شو."), alert=True)
            await ui_edit(event, uid, "main")
            return
        k = data.split(":")[1]
        client = ACCOUNTS.get(uid)
        if k == "start":
            if is_paused(uid):
                if _farm(uid).get("captcha"):
                    await answer(event, L(uid, "A human check is pending — solve it in the PR GRAM chat, then press Resume.",
                                          "تایید انسان بودن در انتظاره — داخل چت PR GRAM حل کن و بعد «ادامه» رو بزن."), alert=True)
                    await ui_edit(event, uid, "main")
                    return
                await resume_flow(uid)
            set_cfg(uid, auto_join=True, auto_fetch=True)
            _farm(uid)["last_fetch"] = 0.0
            await answer(event, L(uid, "🟢 Started", "🟢 شروع شد"))
        elif k == "stop":
            set_cfg(uid, auto_join=False, auto_fetch=False)
            await answer(event, L(uid, "⏹ Stopped", "⏹ متوقف شد"))
        elif k == "resume":
            await resume_flow(uid)
            await answer(event, L(uid, "▶️ Resumed", "▶️ ادامه پیدا کرد"))
        elif k == "view" and client is not None:
            d = _TGT.get(uid)
            if d:
                d["next_try"] = 0.0
            await ensure_targets(uid, client)
        await ui_edit(event, uid, "main")
        return
    await ui_edit(event, uid, "main")


async def on_text(event) -> None:
    uid = event.sender_id
    raw = (event.raw_text or "").strip()
    st = _STATE.get(uid)
    if not raw or raw.startswith("/") or not st:
        return
    if time.time() - st.get("ts", 0) > STATE_TTL:
        await _drop_pending(uid)
        await event.respond(L(uid, "⏱ Login expired. Send /start to try again.", "⏱ ورود منقضی شد. /start رو بفرست."))
        return

    async def _del():
        try:
            await event.delete()
        except Exception as _swallow_e:
            _swallow(_swallow_e)

    if st.get("login") == "phone":
        await _del()
        phone = re.sub(r"[^\d+]", "", raw.translate(_DIGITS))
        if not phone.startswith("+"):
            phone = ("+" + _env_str("DEFAULT_COUNTRY_CODE", "98").lstrip("+") + phone[1:]) if phone.startswith("0") else ("+" + phone)
        try:
            client = PENDING.get(uid) or _make_user_client(uid)
            PENDING[uid] = client
            if not client.is_connected():
                await client.connect()
            sent = await client.send_code_request(phone)
            _set_state(uid, {"login": "code", "phone": phone, "hash": sent.phone_code_hash, "client": client})
            await event.respond(L(uid,
                                  f"Code sent to <code>{h(phone)}</code>.\nSend the code <b>with spaces between digits</b> "
                                  "(e.g. <code>1 2 3 4 5</code>) or Telegram will invalidate it:",
                                  f"کد به <code>{h(phone)}</code> ارسال شد.\nکد رو <b>با فاصله بین رقم‌ها</b> بفرست "
                                  "(مثلاً <code>1 2 3 4 5</code>) وگرنه تلگرام باطلش می‌کنه:"), parse_mode="html")
        except Exception as e:
            w = flood_secs(e)
            await event.respond(L(uid, f"⏳ Telegram asks to wait {_fmt_dur(w)}." if w else f"❌ {h(type(e).__name__)}: {h(e)}",
                                  f"⏳ تلگرام می‌گه {_fmt_dur(w)} صبر کن." if w else f"❌ {h(type(e).__name__)}: {h(e)}"), parse_mode="html")
            await _drop_pending(uid)
        return

    if st.get("login") == "code":
        await _del()
        client = st["client"]
        digits = re.sub(r"\D", "", raw.translate(_DIGITS))[:8]
        if len(digits) < 4:
            await event.respond(L(uid, "⚠️ Send the full code (digits only).", "⚠️ کد کامل رو بفرست (فقط رقم)."))
            return
        try:
            await client.sign_in(phone=st["phone"], code=digits, phone_code_hash=st["hash"])
        except SessionPasswordNeededError:
            _set_state(uid, {"login": "2fa", "client": client})
            await event.respond(L(uid, "🔐 Send your two-factor (2FA) password:", "🔐 رمز دومرحله‌ای (2FA) رو بفرست:"))
            return
        except PhoneCodeInvalidError:
            await event.respond(L(uid, "❌ Wrong code. Send it again.", "❌ کد اشتباهه. دوباره بفرست."))
            return
        except PhoneCodeExpiredError:
            await _drop_pending(uid)
            await event.respond(L(uid, "❌ Code expired. Send /start again.", "❌ کد منقضی شد. /start رو بفرست."))
            return
        except Exception as e:
            await event.respond(f"❌ {h(type(e).__name__)}: {h(e)}", parse_mode="html")
            return
        await _finish_login(uid, client)
        _STATE.pop(uid, None)
        await event.respond(L(uid, "✅ <b>Logged in!</b>", "✅ <b>وارد شدی!</b>"), parse_mode="html")
        await ui_send(uid, "main")
        return

    if st.get("login") == "2fa":
        await _del()
        client = st["client"]
        try:
            await client.sign_in(password=raw)
        except PasswordHashInvalidError:
            await event.respond(L(uid, "❌ Wrong password. Send it again.", "❌ رمز اشتباهه. دوباره بفرست."))
            return
        except Exception as e:
            await event.respond(f"❌ {h(type(e).__name__)}: {h(e)}", parse_mode="html")
            return
        await _finish_login(uid, client)
        _STATE.pop(uid, None)
        await event.respond(L(uid, "✅ <b>Logged in!</b>", "✅ <b>وارد شدی!</b>"), parse_mode="html")
        await ui_send(uid, "main")
        return


def register_handlers(bot: TelegramClient) -> None:
    @bot.on(events.NewMessage(pattern=r"^/(start|drops|stats|help)(?:@\w+)?$", func=lambda e: e.is_private))
    async def _start(event):
        uid = event.sender_id
        if not admin_set() and _env_bool("ALLOW_FIRST_OWNER", False):
            CONFIG["owner_id"] = uid
            save_config()
        if not allowed(uid):
            await event.respond("⛔ You are blocked." if is_banned(uid) else "⛔ Access is closed or the user limit is reached.")
            return
        me = await event.get_sender()
        ensure_user(uid, username=getattr(me, "username", "") or "", first_name=getattr(me, "first_name", "") or "")
        if not get_cfg(uid)["lang"]:
            set_cfg(uid, lang="fa" if str(getattr(me, "lang_code", "") or "").lower().startswith("fa") else "en")
        _STATE.pop(uid, None)
        await ui_send(uid, "main", respond_to=event)

    @bot.on(events.NewMessage(pattern=r"^/cancel(?:@\w+)?$", func=lambda e: e.is_private))
    async def _cancel(event):
        if not allowed(event.sender_id):
            return
        await _drop_pending(event.sender_id)
        await ui_send(event.sender_id, "main", respond_to=event)

    @bot.on(events.NewMessage(pattern=r"^/admin(?:@\w+)?$", func=lambda e: e.is_private))
    async def _admin(event):
        if is_admin(event.sender_id):
            await ui_send(event.sender_id, "admin", respond_to=event)

    @bot.on(events.NewMessage(pattern=r"^/(ban|unban|allow|disallow)(?:@\w+)?\s+(\d+)$", func=lambda e: e.is_private))
    async def _admin_cmd(event):
        if not is_admin(event.sender_id):
            return
        cmd, target = event.pattern_match.group(1), int(event.pattern_match.group(2))
        if cmd == "ban":
            if is_admin(target):
                await event.respond("Cannot ban an admin.")
                return
            db_exec("INSERT OR REPLACE INTO pg_ban (uid,ts) VALUES (?,?)", (target, time.time()))
            await kick_user(target)
        elif cmd == "unban":
            db_exec("DELETE FROM pg_ban WHERE uid=?", (target,))
        elif cmd == "allow":
            db_exec("INSERT OR REPLACE INTO pg_allow (uid,ts) VALUES (?,?)", (target, time.time()))
        else:
            db_exec("DELETE FROM pg_allow WHERE uid=?", (target,))
        await event.respond(f"✅ <code>{cmd} {target}</code>", parse_mode="html")

    @bot.on(events.NewMessage(pattern=r"^/(open|closed)(?:@\w+)?$", func=lambda e: e.is_private))
    async def _access(event):
        if not is_admin(event.sender_id):
            return
        CONFIG["open_access"] = event.pattern_match.group(1) == "open"
        save_config()
        await event.respond(f"🔓 Access is now <b>{'open' if CONFIG['open_access'] else 'invite-only'}</b>.", parse_mode="html")

    @bot.on(events.CallbackQuery())
    async def _cb(event):
        uid = event.sender_id
        if not allowed(uid):
            await answer(event, "⛔", alert=True)
            return
        data = event.data.decode() if event.data else ""
        try:
            await route_cb(event, data)
        except Exception as e:
            log.error(f"cb {data}: {type(e).__name__}: {e}")
        finally:
            if not getattr(event, "_answered", False):
                await answer(event)

    @bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _text(event):
        if not allowed(event.sender_id):
            return
        try:
            await on_text(event)
        except Exception as e:
            log.error(f"on_text: {type(e).__name__}: {e}")


# ===========================================================================
# WATCHDOG & BOOT
# ===========================================================================
async def _tick_user(uid: int, c: TelegramClient) -> None:
    global _TICK_SEM
    if _TICK_SEM is None:
        _TICK_SEM = asyncio.Semaphore(TICK_CONC)       # bounded: 500 accounts never stampede the loop
    try:
        async with _TICK_SEM:
            await asyncio.wait_for(ensure_targets(uid, c), 60)
            await asyncio.wait_for(farm_tick(uid, c), 120)
    except asyncio.TimeoutError:
        log.warning(f"tick u{uid}: timeout")
    except DEAD_ERRORS:
        await handle_dead(uid)
    except Exception as e:
        if not await absorb(uid, e, "tick"):
            log.warning(f"tick u{uid}: {type(e).__name__}: {e}")
    finally:
        _TICKING.discard(uid)


async def _reconnect(name: str, c: TelegramClient) -> None:
    if c is not None and not c.is_connected():
        try:
            await asyncio.wait_for(c.connect(), 30)
        except Exception as e:
            log.error(f"reconnect {name}: {type(e).__name__}")


async def _watchdog() -> None:
    n = 0
    while True:
        await asyncio.sleep(2)
        n += 1
        try:
            if n % 15 == 0:
                items = [("bot", RT.bot)] + [(f"user{u}", c) for u, c in list(ACCOUNTS.items())]
                await asyncio.gather(*[_reconnect(nm, c) for nm, c in items], return_exceptions=True)
            # --- staggered, bounded per-account scheduling -------------------------------------------
            now = time.time()
            accs = list(ACCOUNTS.items())
            if accs:
                k = n % len(accs)
                accs = accs[k:] + accs[:k]               # rotate so nobody is always served last
            spawned = 0
            for u, c in accs:
                if u in _TICKING or now < _NEXT_TICK.get(u, 0.0):
                    continue
                _NEXT_TICK[u] = now + _tick_gap(u)
                _TICKING.add(u)
                spawn(_tick_user(u, c), f"tick-{u}")
                spawned += 1
                if spawned >= TICK_BURST:
                    break
            await _panel_tick()
            flush_stats()                                  # write-behind buffer -> one transaction every 2 s
            if n % 30 == 0:
                now = time.time()
                for u, st in list(_STATE.items()):
                    if now - st.get("ts", now) > STATE_TTL:
                        await _drop_pending(u)
                for u in list(PENDING):
                    if u not in _STATE and u not in RT.qr_wait_tasks and u not in ACCOUNTS:
                        await _drop_pending(u)
                # accounts that failed to load (network blip at boot) are retried automatically
                for u in list_stored_users():
                    if u not in ACCOUNTS and u not in _LOADING:
                        spawn(_load_account(u, quiet=True), f"reload-{u}")
                gc_idle()
            if n % 1800 == 0:
                db_housekeeping()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"watchdog: {e}")


async def _load_account(uid: int, quiet: bool = False) -> None:
    if uid in ACCOUNTS or uid in _LOADING or is_banned(uid) or not load_user_session(uid):
        return
    if RT.load_sem is None:
        RT.load_sem = asyncio.Semaphore(4)
    _LOADING.add(uid)
    try:
        async with RT.load_sem:
            c = _make_user_client(uid)
            try:
                await asyncio.wait_for(c.connect(), 60)
                if not await c.is_user_authorized():
                    await c.disconnect()
                    drop_user_session(uid)
                    return
                me = await c.get_me()
                ACCOUNTS[uid] = c
                ensure_user(uid, username=me.username or "", first_name=me.first_name or "")
                save_user_session(uid, c)
                attach_user_handlers(uid, c)
                await ensure_targets(uid, c)
                spawn(announce_state(uid), f"announce-{uid}")
                spawn(_warm(c), f"warm-{uid}")
                evt(uid, "LOAD", "account loaded" + (" (retry)" if quiet else ""))
                await asyncio.sleep(0.3)
            except DEAD_ERRORS:
                try:
                    await c.disconnect()
                except Exception as _swallow_e:
                    _swallow(_swallow_e)
                drop_user_session(uid)
                await notify(uid, L(uid, "⚠️ Your account session was revoked. Log in again with /start.",
                                    "⚠️ نشست اکانتت باطل شد. با /start دوباره وارد شو."))
            except Exception as e:
                ACCOUNTS.pop(uid, None)   # keep the session file: the watchdog retries later
                log.warning(f"load account {uid}: {type(e).__name__}")
                try:
                    await c.disconnect()
                except Exception as _swallow_e:
                    _swallow(_swallow_e)
    finally:
        _LOADING.discard(uid)


async def boot_bot() -> None:
    if not CONFIG["bot_token"]:
        raise FatalConfig("BOT_TOKEN is empty")
    RT.bot = _make_bot_client()
    await RT.bot.start(bot_token=CONFIG["bot_token"])
    me = await RT.bot.get_me()
    log.info(f"bot started as @{me.username} (id={me.id})")
    try:
        await RT.bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(), lang_code="",
            commands=[BotCommand("start", "Dashboard"), BotCommand("stats", "Live stats"), BotCommand("cancel", "Cancel login")]))
        await RT.bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(), lang_code="fa",
            commands=[BotCommand("start", "داشبورد"), BotCommand("stats", "آمار زنده"), BotCommand("cancel", "لغو ورود")]))
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    register_handlers(RT.bot)
    await asyncio.gather(*[_load_account(u) for u in list_stored_users()], return_exceptions=True)
    log.info(f"{len(ACCOUNTS)} account(s) loaded")
    if RT.web is None:
        RT.web = WebPanel()
    await RT.web.start()
    RT.watchdog = spawn(_watchdog(), "watchdog")
    log.info("bot ready — send /start")
    await RT.stop_evt.wait()


async def shutdown() -> None:
    if RT.web is not None:
        try:
            await RT.web.stop()
        except Exception as _swallow_e:
            _swallow(_swallow_e)
    if RT.watchdog and not RT.watchdog.done():
        RT.watchdog.cancel()
    for t in list(RT.qr_wait_tasks.values()):
        if t and not t.done():
            t.cancel()
    RT.qr_wait_tasks.clear()
    flush_stats()
    for u, c in list(ACCOUNTS.items()):
        save_user_session(u, c)
        _state_save(u)
    for c in list(ACCOUNTS.values()) + list(PENDING.values()):
        try:
            await c.disconnect()
        except Exception as _swallow_e:
            _swallow(_swallow_e)
    ACCOUNTS.clear()
    PENDING.clear()
    _HANDLED.clear()
    _TGT.clear()
    _DEDUPE.clear()
    _TICKING.clear()
    _LOADING.clear()
    PANELS.clear()
    if RT.bot:
        try:
            await RT.bot.disconnect()
        except Exception as _swallow_e:
            _swallow(_swallow_e)
        RT.bot = None


# ===========================================================================
# PATCH 3.3  -  web dashboard, hard safety controls, deep clean
# ===========================================================================
import hmac
from datetime import timedelta
from collections import deque

try:
    from telethon.tl.functions.messages import DeleteChatUserRequest
except ImportError:
    DeleteChatUserRequest = None

PATCH_3_3_APPLIED = True

# ---------------------------------------------------------------------------
# extra per-account settings (stored in pg_cfg, edited from the web dashboard)
# ---------------------------------------------------------------------------
EXTRA_COLS = {
    "cap_hour": ("INTEGER", 40),       # own hourly join ceiling (0 = off)
    "cap_day": ("INTEGER", 300),       # own daily join ceiling (0 = off)
    "night_mode": ("INTEGER", 1),      # calmer pace at night
    "night_factor": ("REAL", 1.4),     # join gap multiplier at night
    "rest_on": ("INTEGER", 0),         # human-like breaks for every profile (smart always has them)
    "rest_every": ("INTEGER", 35),     # minutes of work between breaks
    "rest_len": ("INTEGER", 2),        # minutes per break
    "active_on": ("INTEGER", 0),       # only work inside an active-hours window
    "active_start": ("INTEGER", 8),
    "active_end": ("INTEGER", 23),
    "tz_min": ("INTEGER", -1000),      # -1000 = server time, otherwise minutes from UTC
    "notify": ("INTEGER", 1),          # warning pings in Telegram (flood / budget / bot limit)
    "clean_gap": ("REAL", 2.5),        # seconds between leaves in a deep clean
    "flood_stop": ("INTEGER", 3),      # auto-stop after N floods within 6h (0 = off)
}

SETTINGS_SPEC = {
    "profile": ("choice", tuple(PROFILE_ORDER), None),
    "lang": ("choice", ("en", "fa"), None),
    "auto_leave": ("bool", 0, 1),
    "leave_days": ("int", MIN_LEAVE_DAYS, MAX_LEAVE_DAYS),
    "adv_mode": ("bool", 0, 1),
    "adv_gap": ("float", 0.0, 120.0),
    "adv_batch": ("int", 0, HARD_MAX_BATCH),
    "adv_conc": ("int", 0, HARD_MAX_CONC),
    "cap_hour": ("int", 0, 2000),
    "cap_day": ("int", 0, 20000),
    "night_mode": ("bool", 0, 1),
    "night_start": ("int", 0, 23),
    "night_end": ("int", 0, 23),
    "night_factor": ("float", 1.0, 4.0),
    "rest_on": ("bool", 0, 1),
    "rest_every": ("int", 5, 180),
    "rest_len": ("int", 1, 30),
    "active_on": ("bool", 0, 1),
    "active_start": ("int", 0, 23),
    "active_end": ("int", 0, 23),
    "tz_min": ("tz", 0, 0),
    "notify": ("bool", 0, 1),
    "flood_stop": ("int", 0, 10),
    "clean_gap": ("float", 1.5, 30.0),
}

PRESETS = {
    "careful": dict(profile="smart", adv_mode=0, cap_hour=20, cap_day=150, rest_on=1, rest_every=25, rest_len=3,
                    night_mode=1, night_factor=1.6, flood_stop=2, notify=1),
    "balanced": dict(profile="smart", adv_mode=0, cap_hour=40, cap_day=300, rest_on=1, rest_every=35, rest_len=2,
                     night_mode=1, night_factor=1.4, flood_stop=3, notify=1),
    "fast": dict(profile="fast", adv_mode=1, adv_gap=5.0, adv_batch=8, adv_conc=2, cap_hour=80, cap_day=600,
                 rest_on=1, rest_every=45, rest_len=1, night_mode=1, night_factor=1.3, flood_stop=4, notify=1),
}


def _spec_clean(key: str, val: Any) -> Any:
    kind, lo, hi = SETTINGS_SPEC[key]
    if kind == "bool":
        return 1 if (val is True or str(val).strip().lower() in ("1", "true", "on", "yes")) else 0
    if kind == "int":
        return max(lo, min(hi, int(float(val))))
    if kind == "float":
        return max(lo, min(hi, float(val)))
    if kind == "choice":
        s = str(val)
        return s if s in lo else None
    if kind == "tz":
        v = int(float(val))
        return -1000 if v == -1000 else max(-720, min(840, v))
    return None


# ---------------------------------------------------------------------------
# time helpers: time zone, active hours, night hours
# ---------------------------------------------------------------------------
def local_now(uid: int) -> datetime:
    tz = int(get_cfg(uid).get("tz_min", -1000))
    if tz == -1000:
        return datetime.now()
    return datetime.now(timezone.utc) + timedelta(minutes=tz)


def _in_win(hour: int, start: int, end: int) -> bool:
    if start < end:
        return start <= hour < end
    if start > end:
        return hour >= start or hour < end
    return False


def sched_wait(uid: int) -> float:
    """Seconds until the active-hours window opens (0 = inside the window / schedule off)."""
    cfg = get_cfg(uid)
    if not cfg["active_on"] or cfg["active_start"] == cfg["active_end"]:
        return 0.0
    now = local_now(uid)
    if _in_win(now.hour, cfg["active_start"], cfg["active_end"]):
        return 0.0
    nxt = now.replace(hour=cfg["active_start"], minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def night_mult(uid: int) -> float:
    cfg = get_cfg(uid)
    if cfg["night_mode"] and _in_win(local_now(uid).hour, cfg["night_start"], cfg["night_end"]):
        return max(1.0, float(cfg["night_factor"]))
    return 1.0


# ---------------------------------------------------------------------------
# pacing overrides (hard floors always win; user caps are applied on top)
# ---------------------------------------------------------------------------


def conc_of(uid: int) -> int:
    cfg = get_cfg(uid)
    if cfg["adv_mode"] and cfg["adv_conc"] > 0:
        n = int(cfg["adv_conc"])
    else:
        n = 2 if is_smart(uid) else int(CONFIG["pg_conc"])
    return max(1, min(HARD_MAX_CONC, n))


def status2(uid: int) -> Tuple[str, str]:
    em, st = status_of(uid)
    cfg = get_cfg(uid)
    if cfg["auto_join"] and not cfg["paused"] and em in ("🟢", "🟠", "😴"):
        sw = sched_wait(uid)
        if sw > 0:
            return "🌙", L(uid, f"Sleeping (schedule) · starts in {_fmt_coarse(sw)}",
                           f"خواب (برنامه زمانی) · شروع تا {_fmt_coarse(sw)}")
        rl = max(0.0, _farm(uid).get("rest_until", 0.0) - time.time())
        if rl > 0 and em != "😴":
            return "😴", L(uid, f"Resting (human-like break) · {_fmt_coarse(rl)}",
                           f"استراحت (شبیه انسان) · {_fmt_coarse(rl)}")
    return em, st


_CODES = {"🟢": "running", "🟠": "slowed", "🟡": "wait", "🔴": "paused", "😴": "rest", "🌙": "sleep",
          "⚪️": "idle", "⚫️": "offline"}


def risk_of(uid: int) -> dict:
    """Heuristic 'account safety' score (100 = calm). It is a guide, not a guarantee."""
    cfg, f, now = get_cfg(uid), _farm(uid), time.time()
    hl, dl, gi = eff_limits(uid)
    pen: List[Tuple[str, int]] = []
    ft = [x for x in f.get("flood_times", []) if now - x < 86400]
    if ft:
        pen.append(("flood24", min(40, 15 * len(ft))))
    if slow_eff(uid) > 1.05:
        pen.append(("slowed", 10))
    if hl < NO_CAP and joins_since(uid, 3600) >= 0.8 * hl:
        pen.append(("hour_high", 10))
    if hl >= NO_CAP and dl >= NO_CAP:
        pen.append(("no_caps", 15))
    elif hl >= NO_CAP or dl >= NO_CAP:
        pen.append(("no_caps", 7))
    base = gi / max(slow_eff(uid), 1e-9)
    if base < 5:
        pen.append(("gap_low", 15))
    elif base < 8:
        pen.append(("gap_low", 6))
    if conc_of(uid) > 2:
        pen.append(("conc_high", 6))
    bmax = cfg["adv_batch"] if (cfg["adv_mode"] and cfg["adv_batch"]) else (8 if is_smart(uid) else min(HARD_MAX_BATCH, int(CONFIG["pg_batch"])))
    if bmax > 6:
        pen.append(("batch_high", 6))
    if not (cfg["rest_on"] or is_smart(uid)):
        pen.append(("no_rest", 5))
    if acct_age_days(uid) < 3 and not is_smart(uid):
        pen.append(("new_acct", 12))
    score = max(0, 100 - sum(p for _, p in pen))
    return {"score": score, "level": "good" if score >= 75 else "ok" if score >= 50 else "risky",
            "pen": [{"k": k, "pts": p} for k, p in pen]}


# ---------------------------------------------------------------------------
# Deep clean: leave groups / channels / legacy chats joined outside this helper
# ---------------------------------------------------------------------------
def _dialog_kind(e: Any) -> Optional[str]:
    if isinstance(e, Channel):
        return "group" if getattr(e, "megagroup", False) else "channel"
    if isinstance(e, tltypes.Chat):
        return "chat"
    return None


async def clean_scan(uid: int, client: TelegramClient) -> List[dict]:
    tracked = {int(r["peer_id"]): r for r in joins_all(uid) if r["peer_id"]}
    now, out = time.time(), []
    async for d in client.iter_dialogs():      # no global semaphore held for minutes: the scan is read-only
        e = d.entity
        kind = _dialog_kind(e)
        if kind is None or getattr(e, "left", False) or getattr(e, "deactivated", False):
            continue
        pid = utils.get_peer_id(e)
        tr = tracked.get(pid)
        out.append({
            "id": pid, "title": (getattr(e, "title", "") or str(pid))[:80], "kind": kind,
            "username": getattr(e, "username", "") or "", "members": getattr(e, "participants_count", None),
            "owner": bool(getattr(e, "creator", False)),
            "admin": bool(getattr(e, "admin_rights", None)),
            "tracked": tr is not None,
            "age_days": round((now - float(tr["joined_at"])) / 86400.0, 1) if tr is not None else None,
            "archived": getattr(d, "archived", False) or getattr(d, "folder_id", None) == 1,
        })
    out.sort(key=lambda x: (x["tracked"], x["title"].lower()))
    return out


async def _dialog_entities(client: TelegramClient) -> Dict[int, Any]:
    out: Dict[int, Any] = {}
    async for d in client.iter_dialogs():
        e = d.entity
        if _dialog_kind(e) and not getattr(e, "left", False) and not getattr(e, "deactivated", False):
            out[utils.get_peer_id(e)] = e
    return out


async def _leave_entity(client: TelegramClient, ent: Any) -> None:
    if isinstance(ent, Channel):
        await client(LeaveChannelRequest(ent))
    elif DeleteChatUserRequest is not None:
        await client(DeleteChatUserRequest(chat_id=ent.id, user_id=tltypes.InputUserSelf()))
    else:
        raise RuntimeError("telethon too old for legacy chats")


async def clean_job(uid: int, client: TelegramClient, ids: List[int], resume: bool) -> None:
    f, lock = _farm(uid), _lock(uid, "leave")
    if lock.locked():
        return
    cfg = get_cfg(uid)
    job = {"state": "scanning", "total": len(ids), "done": 0, "left": 0, "failed": 0, "gone": 0, "flood": 0,
           "t0": time.time(), "t1": 0.0, "cancel": False, "wait_until": 0.0, "log": [], "current": ""}
    f["clean"] = job
    was_running = bool(cfg["auto_join"] or cfg["auto_fetch"])
    if was_running:
        set_cfg(uid, auto_join=False, auto_fetch=False)      # never join while cleaning
    stopped = False
    async with lock:
        try:
            dmap = await _dialog_entities(client)
            queue = [dmap[i] for i in ids if i in dmap]
            job["gone"] = len(ids) - len(queue)
            job["total"] = len(queue)
            job["state"] = "running"
            for ent in queue:
                if job["cancel"]:
                    job["state"] = "cancelled"
                    break
                pid, title = utils.get_peer_id(ent), (getattr(ent, "title", "") or "")[:40]
                _mark = job["left"] + job["gone"] + job["failed"]
                job["current"] = title
                for _attempt in range(3):
                    try:
                        async with gsem():
                            await _leave_entity(client, ent)
                        job["left"] += 1
                        db_exec("DELETE FROM pg_joins WHERE uid=? AND peer_id=?", (uid, pid))
                        break
                    except DEAD_ERRORS:
                        await handle_dead(uid)
                        job["state"] = "dead"
                        return
                    except Exception as e:
                        w = flood_secs(e)
                        if w:
                            await absorb(uid, e, "clean")
                            job["flood"] += 1
                            if w > 900:
                                job["state"], stopped = "flood_stop", True
                                job["wait_until"] = time.time() + w
                                break
                            job["state"], job["wait_until"] = "waiting", time.time() + w + 3
                            await asyncio.sleep(w + 3)
                            job["state"] = "running"
                            continue
                        name = type(e).__name__
                        if "NotParticipant" in name or "ChannelPrivate" in name or "ChatIdInvalid" in name:
                            job["gone"] += 1
                            break
                        job["failed"] += 1
                        job["log"] = (job["log"] + [f"{title[:28]}: {name}"])[-40:]
                        break
                if stopped:
                    break
                if job["left"] + job["gone"] + job["failed"] == _mark:      # all retries used up -> not silent (bug 11)
                    job["failed"] += 1
                    job["log"] = (job["log"] + [f"{title[:28]}: retry limit"])[-40:]
                job["done"] += 1
                gap = max(1.5, float(get_cfg(uid)["clean_gap"])) * max(1.0, slow_eff(uid))
                await asyncio.sleep(random.uniform(0.8, 1.4) * gap)
            if job["state"] == "running":
                job["state"] = "done"
        except DEAD_ERRORS:
            await handle_dead(uid)
            job["state"] = "dead"
            return
        except Exception as e:
            job["state"] = "error"
            job["log"] = (job["log"] + [f"{type(e).__name__}: {e}"])[-40:]
            log.warning(f"clean u{uid}: {type(e).__name__}: {e}")
        finally:
            job["t1"] = time.time()
            job["current"] = ""
    if resume and was_running and job["state"] in ("done", "cancelled") and uid in ACCOUNTS:
        set_cfg(uid, auto_join=True, auto_fetch=True)
    evt(uid, "CLEAN", f"{job['state']} left={job['left']} failed={job['failed']} gone={job['gone']}")
    await notify(uid, L(uid,
                        f"🧹 <b>Deep clean {job['state']}</b> — left <b>{job['left']}</b>"
                        + (f" · failed {job['failed']}" if job["failed"] else "")
                        + (f" · already gone {job['gone']}" if job["gone"] else ""),
                        f"🧹 <b>پاکسازی کامل: {job['state']}</b> — ترک شد <b>{job['left']}</b>"
                        + (f" · ناموفق {job['failed']}" if job["failed"] else "")
                        + (f" · قبلاً خارج‌شده {job['gone']}" if job["gone"] else "")))


# ---------------------------------------------------------------------------
# dashboard state / actions
# ---------------------------------------------------------------------------
def acct_state(uid: int) -> dict:
    online = uid in ACCOUNTS
    u, cfg, s, now = user_row(uid), get_cfg(uid), stat_get(uid), time.time()
    f = _farm(uid) if online else {}
    em, st = status2(uid) if online else ("⚫️", "Offline")
    hl, dl, gi = eff_limits(uid) if online else (NO_CAP, NO_CAP, 0.0)
    sl = slow_eff(uid) if online else 1.0
    hrs = max((now - f.get("t0", now)) / 3600.0, 1 / 60)
    ft24 = len([x for x in f.get("flood_times", []) if now - x < 86400])
    oldest = joins_oldest(uid)
    clean = None
    if f.get("clean"):
        j = dict(f["clean"])
        j.pop("cancel", None)
        clean = j
    return {
        "uid": uid, "name": u.get("first_name") or "", "username": u.get("username") or "",
        "online": online, "banned": is_banned(uid),
        "status": {"emoji": em, "text": st, "code": _CODES.get(em, "idle")},
        "cfg": {k: cfg.get(k) for k in SETTINGS_SPEC},
        "stats": {k: s.get(k) for k in ("gram", "balance", "tasks_seen", "joined", "skipped", "checked",
                                        "completed", "errors", "captchas")},
        "sess": {"gram": round(f.get("sess_gram", 0.0), 1), "done": f.get("sess_done", 0),
                 "rate": round(f.get("sess_gram", 0.0) / hrs, 1) if online else 0},
        "lim": {
            "hour_used": joins_since(uid, 3600), "day_used": joins_since(uid, 86400),
            "hour_cap": hl if hl < NO_CAP else None, "day_cap": dl if dl < NO_CAP else None,
            "gap": round(gi, 1), "speed_pct": int(round(100 / sl)), "slow_left": round(slow_left(uid)) if online else 0,
            "flood_left": max(0, round(f.get("flood_until", 0.0) - now)), "flood_wait": f.get("last_wait", 0),
            "flood24": ft24,
            "bot_left": max(0, round(f.get("bot_until", 0.0) - now)), "bot_note": f.get("bot_note", ""),
            "rest_left": max(0, round(f.get("rest_until", 0.0) - now)),
            "sleep_left": round(sched_wait(uid)) if online else 0,
            "age_days": round(acct_age_days(uid), 1), "night_now": night_mult(uid) > 1.0 if online else False,
            "batch": _batch_cap(uid) if online else 0, "conc": conc_of(uid) if online else 0,
        },
        "risk": risk_of(uid) if online else {"score": 0, "level": "ok", "pen": []},
        "recent": [[ts, tx] for ts, tx in (_RECENT.get(uid) or [])][-10:][::-1],
        "tracked": joins_count(uid), "oldest_days": round((now - float(oldest[0]["joined_at"])) / 86400, 1) if oldest else 0,
        "clean": clean,
        "hard": {"min_gap": HARD_MIN_GAP, "max_batch": HARD_MAX_BATCH, "max_conc": HARD_MAX_CONC},
    }


def admin_state() -> dict:
    """Admin overview. Cached for 3 s and built from 4 bulk queries instead of 3 queries per account."""
    if _ADM["val"] is not None and time.time() - _ADM["ts"] < 3.0:
        return _ADM["val"]
    flush_stats()
    now = time.time()
    stats = {int(r["uid"]): r for r in db_exec("SELECT uid, gram, errors FROM pg_stats").fetchall()}
    users = {int(r["id"]): r for r in db_exec("SELECT id, username, first_name FROM users").fetchall()}
    banned = {int(r["uid"]) for r in db_exec("SELECT uid FROM pg_ban").fetchall()}
    rows = []
    for uid in sorted(set(ACCOUNTS) | set(list_stored_users())):
        u, s = users.get(uid), stats.get(uid)
        online, cfg = uid in ACCOUNTS, get_cfg(uid)
        em, st = status2(uid) if online else ("⚫️", "Offline")
        rows.append({"uid": uid, "name": (u["first_name"] if u else "") or "", "username": (u["username"] if u else "") or "",
                     "online": online, "banned": uid in banned, "code": _CODES.get(em, "idle"), "status": st,
                     "running": bool(cfg["auto_join"] and cfg["auto_fetch"]), "paused": bool(cfg["paused"]),
                     "speed_pct": int(round(100 / slow_eff(uid))) if online else 100,
                     "gram": float(s["gram"]) if s else 0.0, "joined_d": joins_since(uid, 86400),
                     "errors": int(s["errors"]) if s else 0})
    t = db_exec("SELECT COALESCE(SUM(gram),0) AS g, COALESCE(SUM(joined),0) AS j, "
                "COALESCE(SUM(completed),0) AS c, COALESCE(SUM(errors),0) AS e FROM pg_stats").fetchone()
    val = {"ok": True, "accounts": rows, "open_access": bool(CONFIG["open_access"]), "users_total": users_count(),
           "max_users": int(CONFIG["max_users"]), "online": len(ACCOUNTS), "now": now,
           "totals": {"gram": float(t["g"]), "joined": int(t["j"]), "completed": int(t["c"]), "errors": int(t["e"])},
           "allowed": [int(r["uid"]) for r in db_exec("SELECT uid FROM pg_allow").fetchall()]}
    _ADM["ts"], _ADM["val"] = time.time(), val
    return val


def apply_settings(uid: int, values: dict) -> dict:
    clean: Dict[str, Any] = {}
    for k, v in (values or {}).items():
        if k not in SETTINGS_SPEC:
            continue
        try:
            cv = _spec_clean(k, v)
        except Exception:
            continue
        if cv is not None:
            clean[k] = cv
    if clean:
        set_cfg(uid, **clean)
    return clean


def _jerr(msg: str) -> dict:
    return {"ok": False, "error": msg}


async def panel_action(role: str, uid: int, d: dict) -> dict:
    act = str(d.get("action", "")).strip()
    if act in ("toggle_access", "ban", "unban", "allow", "disallow"):
        if role != "admin":
            return _jerr("admin only")
        if act == "toggle_access":
            CONFIG["open_access"] = not CONFIG["open_access"]
            save_config()
            return {"ok": True, "msg": "access: " + ("open" if CONFIG["open_access"] else "invite-only")}
        try:
            target = int(d.get("target") or uid or 0)
        except Exception:
            target = 0
        if not target:
            return _jerr("missing target id")
        if act == "ban":
            if is_admin(target):
                return _jerr("cannot ban an admin")
            db_exec("INSERT OR REPLACE INTO pg_ban (uid,ts) VALUES (?,?)", (target, time.time()))
            await kick_user(target)
        elif act == "unban":
            db_exec("DELETE FROM pg_ban WHERE uid=?", (target,))
        elif act == "allow":
            db_exec("INSERT OR REPLACE INTO pg_allow (uid,ts) VALUES (?,?)", (target, time.time()))
        else:
            db_exec("DELETE FROM pg_allow WHERE uid=?", (target,))
        return {"ok": True, "msg": f"{act} {target}"}
    if not uid:
        return _jerr("no account selected")
    client = ACCOUNTS.get(uid)
    needs_online = ("start", "fetch", "boost", "leave_old", "leave_all", "leave_row", "clean_scan", "clean_start")
    if act in needs_online and client is None:
        return _jerr("account is offline - log in from Telegram first")

    if act == "start":
        if is_paused(uid):
            if _farm(uid).get("captcha"):                       # bug 12
                return _jerr("human check pending - solve it in the PR GRAM chat, then press Resume")
            await resume_flow(uid)
        set_cfg(uid, auto_join=True, auto_fetch=True)
        _farm(uid)["last_fetch"] = 0.0
    elif act == "stop":
        set_cfg(uid, auto_join=False, auto_fetch=False)
    elif act == "pause":
        set_cfg(uid, paused=True)
    elif act == "resume":
        await resume_flow(uid)
    elif act == "fetch":
        if is_paused(uid):
            return _jerr("paused - resume first")
        if time.time() < _farm(uid)["flood_until"]:
            return _jerr("a flood wait is active")
        ok = await request_tasks(uid, client)
        return {"ok": ok, "msg": "requested" if ok else "failed"}
    elif act == "boost":
        f = _farm(uid)
        f["slow0"], f["slow_end"], f["slow_notify"] = 1.0, 0.0, False
        f["hour_cap"] = f["cap_until"] = f["day_cap"] = f["day_until"] = 0.0
        _state_save(uid)
        return {"ok": True, "msg": "slow-down cleared (a running flood wait is still obeyed; your own caps stay)"}
    elif act == "preset":
        p = PRESETS.get(str(d.get("name")))
        if not p:
            return _jerr("unknown preset")
        apply_settings(uid, p)
        return {"ok": True, "msg": f"preset {d.get('name')} applied"}
    elif act == "reset_warmup":
        set_cfg(uid, warmup_ts=time.time())
    elif act == "reset_stats":
        stat_reset(uid)
    elif act == "reset_token":
        return {"ok": True, "token": reset_user_web_token(uid), "msg": "new private link created"}
    elif act == "logout":
        await do_logout(uid)
    elif act == "leave_old":
        spawn(leave_old(uid, client), f"leave-{uid}")
    elif act == "leave_all":
        spawn(leave_all(uid, client), f"leaveall-{uid}")
    elif act == "leave_row":
        want = {int(x) for x in (d.get("ids") or []) if str(x).lstrip("-").isdigit()}
        rows = [r for r in joins_all(uid) if int(r["id"]) in want]
        spawn(leave_rows(uid, client, rows, "selected"), f"leaverow-{uid}")
    elif act == "clean_scan":
        if _lock(uid, "leave").locked():
            return _jerr("another leave job is running")
        try:
            items = await asyncio.wait_for(clean_scan(uid, client), 240)
        except asyncio.TimeoutError:
            return _jerr("scan timed out - try again")
        except DEAD_ERRORS:
            await handle_dead(uid)
            return _jerr("session revoked")
        except Exception as e:
            if await absorb(uid, e, "scan"):
                return _jerr("flood wait - try again later")
            return _jerr(f"{type(e).__name__}")
        return {"ok": True, "items": items[:4000], "total": len(items)}
    elif act == "clean_start":
        j = _farm(uid).get("clean")
        if (j and j.get("state") in ("scanning", "running", "waiting")) or _lock(uid, "leave").locked():
            return _jerr("a clean job is already running")
        ids = []
        for x in (d.get("ids") or [])[:5000]:
            try:
                ids.append(int(x))
            except Exception as _swallow_e:
                _swallow(_swallow_e)
        if not ids:
            return _jerr("nothing selected")
        spawn(clean_job(uid, client, ids, bool(d.get("resume", True))), f"clean-{uid}")
        return {"ok": True, "msg": f"cleaning {len(ids)} chats"}
    elif act == "clean_cancel":
        j = _farm(uid).get("clean")
        if j:
            j["cancel"] = True
    else:
        return _jerr(f"unknown action '{act}'")
    return {"ok": True, "msg": "ok"}


async def web_api(role: str, wuid: int, path: str, qs: Dict[str, List[str]], data: dict) -> dict:
    def q(k: str, dflt: str = "") -> str:
        return (qs.get(k, [dflt])[0] or dflt)
    try:
        uid = wuid if role == "user" else int(q("uid") or data.get("uid") or 0)
    except Exception:
        uid = 0
    if path == "/api/me":
        return {"ok": True, "role": role, "uid": uid, "version": VERSION,
                "hard": {"min_gap": HARD_MIN_GAP, "max_batch": HARD_MAX_BATCH, "max_conc": HARD_MAX_CONC}}
    if path == "/api/admin":
        return admin_state() if role == "admin" else _jerr("admin only")
    if path == "/api/action" and (data.get("action") in ("toggle_access", "ban", "unban", "allow", "disallow")):
        return await panel_action(role, uid, data)
    if not uid:
        return _jerr("no account selected")
    if uid not in ACCOUNTS and not user_known(uid) and uid not in list_stored_users():
        return _jerr("unknown account")
    if path == "/api/state":
        return {"ok": True, **acct_state(uid)}
    if path == "/api/logs":
        try:
            n = max(20, min(500, int(q("n") or 200)))
        except Exception:
            n = 200
        lvl = q("level")
        try:
            since = float(q("since") or 0)
        except Exception:
            since = 0.0
        out = []
        for e in list(LOG_RING or [])[-2000:]:
            if since and e["ts"] <= since:
                continue
            m = re.match(r"u(\d+)\s", e["msg"])
            if role == "user" and not (m and int(m.group(1)) == uid):
                continue
            if role == "admin" and q("all") != "1" and not (m and int(m.group(1)) == uid):
                continue
            if lvl == "WARNING" and e["level"] not in ("WARNING", "ERROR"):
                continue
            if lvl == "ERROR" and e["level"] != "ERROR":
                continue
            out.append(e)
        return {"ok": True, "logs": out[-n:]}
    if path == "/api/channels":
        now = time.time()
        rows = [{"id": r["id"], "title": r["title"] or r["ref"], "ref": r["ref"], "kind": r["kind"],
                 "age_days": round((now - float(r["joined_at"])) / 86400, 1)} for r in joins_all(uid)]
        return {"ok": True, "rows": rows[::-1][:1000], "total": len(rows), "leave_days": leave_after_days(uid)}
    if path == "/api/settings" and data is not None:
        applied = apply_settings(uid, data.get("values") or {})
        return {"ok": True, "applied": list(applied), **{"state": acct_state(uid)}}
    if path == "/api/action":
        return await panel_action(role, uid, data)
    return _jerr("not found")


# ---------------------------------------------------------------------------
# Telegram side: only Login / Start-Stop / Dashboard / Logout
# ---------------------------------------------------------------------------
def dash_base() -> str:
    b = _env_str("WEB_BASE_URL", "").rstrip("/")
    if b:
        return b
    w = RT.web
    if w is not None and w.server is not None and w.public_ip:
        return f"http://{w.public_ip}:{w.port}"
    return ""


def dash_url(uid: int, admin: bool = False) -> str:
    base = dash_base()
    if not base or RT.web is None:
        return ""
    return f"{base}/?t={RT.web.token if admin else user_web_token(uid)}"


def ub(text: str, url: str, style: Optional[str] = "primary"):
    """URL button (coloured when this Telegram/Telethon build supports button styles)."""
    if style in ("primary", "success", "danger") and _STYLE_OK[0]:
        try:
            st = tltypes.KeyboardButtonStyle(**{f"bg_{style}": True})
            return tltypes.KeyboardButtonUrl(text=text, url=url, style=st)
        except Exception as _swallow_e:
            _swallow(_swallow_e)
    return Button.url(text, url)


def compact_text(uid: int) -> str:
    u, s, f = user_row(uid), stat_get(uid), _farm(uid)
    em, st = status2(uid)
    hl, dl, gi = eff_limits(uid)
    hu, du = joins_since(uid, 3600), joins_since(uid, 86400)
    rk, sl = risk_of(uid), slow_eff(uid)
    cap = lambda n, c: f"{n}/{c}" if c < NO_CAP else f"{n}"
    safety = {"good": "🟢", "ok": "🟡", "risky": "🔴"}[rk["level"]]
    last = h(s["last_ev"]) or "—"
    if s.get("last_ev_ts"):
        last += f" <i>({_ago(s['last_ev_ts'])})</i>"
    body = L(
        uid,
        f"🪙 GRAM <b>{s['gram']:,.0f}</b> · 🏦 <b>{h(s['balance']) or '—'}</b> · session <b>+{f.get('sess_gram', 0.0):,.0f}</b>\n"
        f"➕ Joins  hour <b>{cap(hu, hl)}</b> · day <b>{cap(du, dl)}</b>\n"
        f"⚡ Speed <b>{int(round(100 / sl))}%</b> · gap <b>{gi:.0f}s</b> · {safety} safety <b>{rk['score']}</b>/100\n"
        f"🕒 {last}",
        f"🪙 GRAM <b>{s['gram']:,.0f}</b> · 🏦 <b>{h(s['balance']) or '—'}</b> · این نشست <b>+{f.get('sess_gram', 0.0):,.0f}</b>\n"
        f"➕ عضویت  ساعت <b>{cap(hu, hl)}</b> · روز <b>{cap(du, dl)}</b>\n"
        f"⚡ سرعت <b>{int(round(100 / sl))}٪</b> · فاصله <b>{gi:.0f} ثانیه</b> · {safety} ایمنی <b>{rk['score']}</b>/۱۰۰\n"
        f"🕒 {last}")
    hint = L(uid, "<i>Every setting, limit and cleanup tool lives in the web dashboard 👇</i>",
             "<i>همهٔ تنظیمات، محدودیت‌ها و ابزار پاکسازی توی داشبورد وب هست 👇</i>")
    if not dash_base():
        hint = L(uid, "<i>⚠️ Dashboard address unknown — set WEB_BASE_URL (or open the server's web port).</i>",
                 "<i>⚠️ آدرس داشبورد مشخص نیست — WEB_BASE_URL رو تنظیم کن (یا پورت وب سرور رو باز کن).</i>")
    return (f"🪙 <b>PR GRAM Helper</b>  <code>v{VERSION}</code>\n{DIV}\n\n{em} <b>{h(st)}</b>\n\n"
            f"👤 <b>{h(u.get('first_name')) or '—'}</b> · <code>{uid}</code>\n<blockquote>{body}</blockquote>\n{hint}")


# ---------------------------------------------------------------------------
# WEB SERVER (stdlib asyncio, no dependencies)
# ---------------------------------------------------------------------------
LOG_RING: Optional[Any] = None


def _init_log_ring() -> None:
    global LOG_RING
    if LOG_RING is not None:
        return

    class _RingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                LOG_RING.append({"ts": record.created, "level": record.levelname,
                                 "logger": record.name, "msg": record.getMessage()})
            except Exception as _swallow_e:
                _swallow(_swallow_e)

    LOG_RING = deque(maxlen=2500)
    rh = _RingHandler()
    rh.setLevel(logging.INFO)
    log.addHandler(rh)


WEB_TOKEN_FILE = DATA_DIR / "web_token.txt"
_PORT_ENV_CANDIDATES = ("WEB_PORT", "SERVER_PORT", "PTERODACTYL_SERVER_PORT", "ALLOCATION_PORT",
                        "PANEL_PORT", "APP_PORT", "HTTP_PORT", "PORT")


def _detect_assigned_port() -> Tuple[Optional[int], str]:
    for key in _PORT_ENV_CANDIDATES:
        raw = (os.environ.get(key) or "").strip()
        if raw.isdigit() and 0 < int(raw) < 65536:
            return int(raw), key
    return None, ""


def _web_token() -> str:
    tok = _env_str("WEB_TOKEN", "")
    if tok:
        return tok
    if WEB_TOKEN_FILE.exists():
        t = WEB_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    import secrets
    t = secrets.token_urlsafe(18)
    try:
        WEB_TOKEN_FILE.write_text(t, encoding="utf-8")
        os.chmod(WEB_TOKEN_FILE, 0o600)
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    return t


def _local_outbound_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


async def _public_ip() -> Optional[str]:
    import urllib.request

    def _fetch() -> Optional[str]:
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=3) as r:
                return r.read().decode().strip() or None
        except Exception:
            return None
    try:
        return await asyncio.wait_for(asyncio.to_thread(_fetch), 4)
    except Exception:
        return None


class WebPanel:
    def __init__(self) -> None:
        self.server: Optional[asyncio.base_events.Server] = None
        self.port = 0
        self.local_ip = "127.0.0.1"
        self.public_ip: Optional[str] = None
        self.token = _web_token()
        self._fails: Dict[str, List[float]] = {}

    async def start(self) -> None:
        _init_log_ring()
        self.local_ip = _local_outbound_ip()
        self.public_ip = await _public_ip()
        assigned, src = _detect_assigned_port()
        if assigned:
            try:
                self.server = await asyncio.start_server(self._client, host="0.0.0.0", port=assigned)
                self.port = assigned
                log.info(f"🖥  web dashboard: host-assigned port {assigned} (from ${src})")
            except OSError as e:
                log.error(f"🖥  assigned port {assigned} (${src}) is busy ({e}) - scanning for a free one")
        if self.server is None:
            for p in range(8787, 8812):
                try:
                    self.server = await asyncio.start_server(self._client, host="0.0.0.0", port=p)
                    self.port = p
                    break
                except OSError:
                    continue
        if self.server is None:
            log.error("web dashboard: no free port - disabled")
            return
        base = dash_base() or f"http://<your-ip>:{self.port}"
        log.info("admin dashboard is up - the private admin link is printed to the console only (not to app.log)")
        print("web dashboard: sign in with credentials delivered through Telegram.", flush=True)
        log.info("🖥  users get their own private link from the Telegram bot (button 'Open dashboard'). "
                 "Behind HTTPS/a domain? set WEB_BASE_URL=https://your.domain")

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception as _swallow_e:
                _swallow(_swallow_e)
            self.server = None

    def _blocked(self, ip: str) -> bool:
        ts = [t for t in self._fails.get(ip, []) if time.time() - t < 600]
        if ts:
            self._fails[ip] = ts
        else:
            self._fails.pop(ip, None)
        return len(ts) >= 15

    def _fail(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.time())

    def _auth(self, headers: Dict[str, str], qs: Dict[str, List[str]]) -> Optional[Tuple[str, int]]:
        tok = (headers.get("x-token") or ((qs.get("t", [""])[0] or "") if _env_bool("ALLOW_QUERY_TOKEN", False) else "")).strip()  # [patch-hardening:header-token]
        if not tok:
            return None
        try:
            if hmac.compare_digest(tok.encode(), self.token.encode()):
                return ("admin", 0)
        except Exception:
            return None
        uid = uid_of_web_token(tok)
        return ("user", uid) if uid else None

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not hasattr(self, "_conn_sem"):
            self._conn_sem = asyncio.Semaphore(96)
        if self._conn_sem.locked():                       # connection cap: a flood cannot starve the bot
            try:
                await self._send(writer, 429, "text/plain", b"busy")
            except Exception as _swallow_e:
                _swallow(_swallow_e)
            finally:
                writer.close()
            return
        async with self._conn_sem:
            await self._client_inner(reader, writer)

    async def _client_inner(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), 10)
            except asyncio.TimeoutError:
                return
            if not line:
                return
            try:
                method, path, _ver = line.decode("latin1").strip().split(" ")
            except Exception:
                return
            headers: Dict[str, str] = {}
            _nh = 0
            while True:
                _nh += 1
                if _nh > 64:
                    return
                hl = await asyncio.wait_for(reader.readline(), 10)
                if not hl or hl in (b"\r\n", b"\n"):
                    break
                if b":" in hl:
                    k, _, v = hl.decode("latin1").partition(":")
                    headers[k.strip().lower()] = v.strip()
            body = b""
            clen = int(headers.get("content-length", "0") or "0")
            if clen > (16_000_000 if path.startswith("/api/owner/backup/upload") else 262_144):
                await self._send(writer, 413, "text/plain", b"too large")
                return
            if clen > 0:
                body = await asyncio.wait_for(reader.readexactly(clen), 15)
            peer = writer.get_extra_info("peername")
            ip = ((headers.get("x-forwarded-for", "").split(",")[0].strip() if TRUST_PROXY else "") or (peer[0] if peer else "?"))
            await self._route(method, path, headers, body, writer, ip)
        except Exception as e:
            try:
                await self._send(writer, 500, "text/plain", f"error: {type(e).__name__}".encode())
            except Exception as _swallow_e:
                _swallow(_swallow_e)
        finally:
            try:
                writer.close()
            except Exception as _swallow_e:
                _swallow(_swallow_e)

    async def _send(self, writer: asyncio.StreamWriter, status: int, ctype: str, body: bytes) -> None:
        reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 413: "Too Large",
                   429: "Too Many Requests", 500: "Server Error"}
        head = (f"HTTP/1.1 {status} {reasons.get(status, '')}\r\n"
                f"Content-Type: {ctype}; charset=utf-8\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
                "Referrer-Policy: no-referrer\r\n"
                "Content-Security-Policy: default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "img-src data:; connect-src 'self'; frame-ancestors 'none'\r\n\r\n").encode("latin1")
        writer.write(head + body)
        await writer.drain()

    async def _route(self, method: str, path: str, headers: Dict[str, str], body: bytes,
                     writer: asyncio.StreamWriter, ip: str) -> None:
        from urllib.parse import urlparse, parse_qs
        u = urlparse(path)
        qs = parse_qs(u.query)
        if u.path in ("/", "/index.html") and method == "GET":
            await self._send(writer, 200, "text/html", _PANEL_TPL.encode("utf-8"))      # the shell holds no secret
            return
        if u.path == "/healthz":
            await self._send(writer, 200, "text/plain", b"ok")
            return
        if not u.path.startswith("/api/"):
            await self._send(writer, 404, "text/plain", b"not found")
            return
        if self._blocked(ip):
            await self._send(writer, 429, "application/json", b'{"ok":false,"error":"too many attempts"}')
            return
        who = self._auth(headers, qs)
        if who is None:
            self._fail(ip)
            await self._send(writer, 401, "application/json", b'{"ok":false,"error":"unauthorized"}')
            return
        data: dict = {}
        if method == "POST":
            try:
                data = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
        try:
            res = await web_api(who[0], who[1], u.path, qs, data)
        except Exception as e:
            log.warning(f"web api {u.path}: {type(e).__name__}: {e}")
            res = {"ok": False, "error": type(e).__name__}
        await self._send(writer, 200, "application/json", json.dumps(res, default=str).encode("utf-8"))


_PANEL_TPL = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex"><meta name="theme-color" content="#0b1020"><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Ccircle cx='32' cy='32' r='28' fill='%23f2b84b'/%3E%3Ccircle cx='32' cy='32' r='20' fill='none' stroke='%231c1400' stroke-width='4'/%3E%3Cpath d='M32 20v24M24 28h12a4 4 0 010 8H24' stroke='%231c1400' stroke-width='4' fill='none' stroke-linecap='round'/%3E%3C/svg%3E"><title>PR GRAM Helper</title><!--prgram-ui-patch-v4-->
<style>
:root{
  --bg:#0b1020;--bg2:#0f1629;--card:#141c33;--card2:#1b2540;--line:#27345a;
  --txt:#eaf0ff;--mut:#8f9cbc;--acc:#f2b84b;--on-acc:#1c1400;
  --ok:#38d39f;--warn:#ffb020;--bad:#ff6b6b;--vio:#a78bfa;
  --r:18px;--r2:12px;--r3:9px;--shadow:0 10px 30px rgba(3,6,18,.45);
  color-scheme:dark}
:root[data-theme=light]{
  --bg:#eef1f7;--bg2:#ffffff;--card:#ffffff;--card2:#f1f4fa;--line:#dbe2ef;
  --txt:#14203a;--mut:#62708d;--acc:#b9791a;--on-acc:#ffffff;
  --ok:#0f9d73;--warn:#c77700;--bad:#d63c3c;--vio:#6d4fd8;
  --shadow:0 8px 24px rgba(30,45,90,.10);color-scheme:light}
*{box-sizing:border-box}
html,body{margin:0}
body{background:
  radial-gradient(900px 480px at 85% -10%,color-mix(in srgb,var(--acc) 10%,transparent),transparent 60%),
  var(--bg);
  color:var(--txt);
  font:14px/1.6 Vazirmatn,"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,Tahoma,sans-serif;
  font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
::selection{background:color-mix(in srgb,var(--acc) 35%,transparent)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:99px;border:2px solid transparent;background-clip:content-box}

/* layout */
.app{display:grid;grid-template-columns:248px 1fr;min-height:100vh}
nav{background:color-mix(in srgb,var(--bg2) 92%,transparent);backdrop-filter:blur(10px);
  border-inline-end:1px solid var(--line);padding:20px 14px;position:sticky;top:0;height:100vh;
  display:flex;flex-direction:column;gap:4px}
.brand{font-weight:800;font-size:17px;letter-spacing:.2px;padding:2px 10px 18px;display:flex;gap:10px;align-items:center}
.brand small{color:var(--mut);font-weight:500;font-size:11px;margin-inline-start:auto;
  border:1px solid var(--line);border-radius:99px;padding:1px 8px}
nav a{position:relative;display:flex;gap:12px;align-items:center;padding:10px 14px;border-radius:var(--r2);
  color:var(--mut);cursor:pointer;user-select:none;transition:background .15s,color .15s}
nav a span:first-child{width:22px;text-align:center;font-size:16px}
nav a:hover{background:var(--card2);color:var(--txt)}
nav a.on{background:color-mix(in srgb,var(--acc) 14%,transparent);color:var(--txt);font-weight:700}
nav a.on:before{content:"";position:absolute;inset-inline-start:-14px;top:9px;bottom:9px;width:4px;
  border-radius:0 4px 4px 0;background:var(--acc)}
[dir=rtl] nav a.on:before{border-radius:4px 0 0 4px}
nav .sp{flex:1}
main{padding:28px 30px;max-width:1120px;width:100%;margin:0 auto}
header.top{display:flex;gap:12px;align-items:center;margin-bottom:22px;flex-wrap:wrap}
header.top h2{margin:0;font-size:24px;font-weight:800;letter-spacing:-.01em}
header.top .sp{flex:1}

/* chips, dots */
.chip{display:inline-flex;gap:8px;align-items:center;background:var(--card);border:1px solid var(--line);
  border-radius:99px;padding:5px 14px;font-size:12.5px;color:var(--mut)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut);display:inline-block;flex:none}
.dot.running{background:var(--ok);box-shadow:0 0 0 4px color-mix(in srgb,var(--ok) 25%,transparent);animation:pulse 2.4s ease-in-out infinite}
.dot.slowed,.dot.wait{background:var(--warn)}
.dot.paused{background:var(--bad)}
.dot.rest,.dot.sleep{background:var(--vio)}
@keyframes pulse{50%{box-shadow:0 0 0 7px color-mix(in srgb,var(--ok) 0%,transparent)}}

/* surfaces */
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:20px;margin-bottom:16px;
  box-shadow:var(--shadow)}
.card h3{margin:0 0 14px;font-size:15px;font-weight:700;display:flex;gap:10px;align-items:center}
.card h3 small{color:var(--mut);font-weight:500;font-size:12px;margin-inline-start:auto}
.grid{display:grid;gap:14px}
.g2{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.g4{grid-template-columns:repeat(auto-fit,minmax(160px,1fr))}
.tile{background:var(--card);border:1px solid var(--line);border-radius:var(--r2);padding:14px 16px}
.tile span{display:block;color:var(--mut);font-size:12px;margin-bottom:2px}
.tile b{font-size:24px;font-weight:800;letter-spacing:-.01em}
.tile:first-child b{color:var(--acc)}
.hero{display:flex;gap:18px;align-items:center;flex-wrap:wrap;padding:24px;
  background:
    radial-gradient(520px 200px at 0% 0%,color-mix(in srgb,var(--acc) 16%,transparent),transparent 70%),
    var(--card)}
.hero .st{flex:1;min-width:220px}
.hero .st b{font-size:24px;font-weight:800;display:flex;gap:10px;align-items:center;letter-spacing:-.01em}
.hero .st small{color:var(--mut);display:block;margin-top:2px}

/* buttons */
.btn{background:var(--card2);border:1px solid var(--line);border-radius:var(--r2);padding:9px 16px;cursor:pointer;
  font-weight:600;transition:transform .12s,border-color .15s,background .15s}
.btn:hover{border-color:color-mix(in srgb,var(--acc) 55%,var(--line));transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn:disabled{opacity:.45;cursor:not-allowed;transform:none}
.btn.ok{background:color-mix(in srgb,var(--ok) 20%,var(--card2));border-color:var(--ok)}
.btn.bad{background:color-mix(in srgb,var(--bad) 18%,var(--card2));border-color:var(--bad)}
.btn.pri{background:var(--acc);border-color:var(--acc);color:var(--on-acc)}
.btn.sm{padding:5px 12px;font-size:12.5px;border-radius:var(--r3)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}

/* meters */
.bar{height:10px;background:var(--card2);border-radius:99px;overflow:hidden;margin:7px 0 3px;
  box-shadow:inset 0 0 0 1px var(--line)}
.bar i{display:block;height:100%;background:var(--acc);border-radius:99px;transition:width .6s cubic-bezier(.2,.8,.2,1)}
.bar.warn i{background:var(--warn)}
.bar.bad i{background:var(--bad)}
.bar.ok i{background:var(--ok)}
.lbl{display:flex;justify-content:space-between;color:var(--mut);font-size:12.5px}
.lbl b{color:var(--txt)}
.ring{--p:0;--c:var(--ok);width:104px;height:104px;border-radius:50%;flex:none;display:grid;place-items:center;
  background:conic-gradient(var(--c) calc(var(--p)*1%),var(--card2) 0)}
.ring b{width:82px;height:82px;border-radius:50%;background:var(--card);display:grid;place-items:center;font-size:26px;font-weight:800}
.pen{display:flex;justify-content:space-between;gap:10px;font-size:13px;color:var(--mut);padding:5px 0;border-bottom:1px dashed var(--line)}
.pen b{color:var(--warn)}

/* banners, log lines, toasts */
.banner{border-radius:var(--r2);padding:12px 16px;margin-bottom:14px;border:1px solid var(--line);background:var(--card)}
.banner.warn{border-color:var(--warn);background:color-mix(in srgb,var(--warn) 11%,var(--card))}
.banner.bad{border-color:var(--bad);background:color-mix(in srgb,var(--bad) 11%,var(--card))}
.ev{font:12.5px/1.7 ui-monospace,"Cascadia Mono",Menlo,Consolas,monospace;color:var(--mut)}
.ev b{color:var(--txt);font-weight:400}
#toasts{position:fixed;inset-block-end:18px;inset-inline-end:18px;display:flex;flex-direction:column;gap:8px;z-index:50}
.toast{background:var(--card);border:1px solid var(--line);border-inline-start:4px solid var(--ok);border-radius:var(--r2);
  padding:11px 16px;box-shadow:var(--shadow);max-width:360px;animation:tin .2s ease-out}
.toast.err{border-inline-start-color:var(--bad)}
@keyframes tin{from{opacity:0;transform:translateY(8px)}}

/* settings form */
.fld{display:flex;justify-content:space-between;gap:20px;align-items:center;padding:14px 0;border-bottom:1px solid var(--line)}
.fld:last-child{border:0;padding-bottom:0}
.fld.off{opacity:.4}
.fl b{display:block;font-weight:700}
.fl small{color:var(--mut);display:block;max-width:52ch}
.fc{flex:none;min-width:160px;display:flex;justify-content:flex-end}
.rng{display:flex;gap:12px;align-items:center}
.rng input{width:160px;accent-color:var(--acc)}
.rng output{min-width:56px;text-align:end;font-weight:700;color:var(--acc)}
select,.inp{background:var(--card2);border:1px solid var(--line);border-radius:var(--r3);padding:7px 12px}
.sw{position:relative;width:48px;height:28px;display:inline-block}
.sw input{opacity:0;width:0;height:0}
.sw i{position:absolute;inset:0;background:var(--card2);border:1px solid var(--line);border-radius:99px;transition:.2s;cursor:pointer}
.sw i:before{content:"";position:absolute;width:20px;height:20px;top:3px;inset-inline-start:3px;background:var(--mut);border-radius:50%;transition:.2s}
.sw input:checked+i{background:color-mix(in srgb,var(--ok) 35%,var(--card2));border-color:var(--ok)}
.sw input:checked+i:before{background:var(--ok);transform:translateX(20px)}
[dir=rtl] .sw input:checked+i:before{transform:translateX(-20px)}
.sw input:focus-visible+i{outline:2px solid var(--acc);outline-offset:2px}
.seg{display:flex;flex-wrap:wrap;gap:4px;background:var(--card2);padding:4px;border-radius:var(--r2);border:1px solid var(--line)}
.seg button{border:0;background:transparent;padding:7px 13px;border-radius:var(--r3);cursor:pointer;color:var(--mut);font-weight:600}
.seg button:hover{color:var(--txt)}
.seg button.on{background:var(--acc);color:var(--on-acc)}
.savebar{position:sticky;bottom:12px;background:var(--card);border:1px solid var(--acc);border-radius:var(--r);
  padding:12px 18px;display:flex;gap:12px;align-items:center;box-shadow:0 12px 36px rgba(0,0,0,.45);z-index:4}
.savebar .sp{flex:1}

/* tables, tags, logs, modal, login */
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:start;padding:9px 10px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;font-size:12.5px;position:sticky;top:0;background:var(--card)}
tbody tr:hover{background:color-mix(in srgb,var(--card2) 70%,transparent)}
.scroll{max-height:460px;overflow:auto;border:1px solid var(--line);border-radius:var(--r2)}
.tag{font-size:11.5px;padding:2px 9px;border-radius:99px;background:var(--card2);color:var(--mut);border:1px solid var(--line);margin-inline-end:4px;display:inline-block}
.tag.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 45%,var(--line))}
.tag.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,var(--line))}
.tag.bad{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 45%,var(--line))}
#log{background:var(--bg);border:1px solid var(--line);border-radius:var(--r2);padding:12px;height:62vh;overflow:auto;
  font:12.5px/1.6 ui-monospace,"Cascadia Mono",Menlo,Consolas,monospace}
.l{white-space:pre-wrap;word-break:break-word}
.l.WARNING{color:var(--warn)}.l.ERROR{color:var(--bad)}.l.INFO{color:var(--mut)}
.modal{position:fixed;inset:0;background:rgba(4,8,20,.65);backdrop-filter:blur(4px);display:grid;place-items:center;z-index:60;padding:16px}
.modal>div{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:24px;max-width:460px;width:100%;box-shadow:var(--shadow)}
.login{min-height:100vh;display:grid;place-items:center}
.login .card{width:min(430px,92vw);padding:28px}

/* mobile: bottom tab bar */
@media(max-width:820px){
  .app{grid-template-columns:1fr}
  nav{position:fixed;inset:auto 0 0 0;height:auto;flex-direction:row;justify-content:space-around;border:0;
    border-top:1px solid var(--line);z-index:20;padding:6px 6px calc(6px + env(safe-area-inset-bottom,0px));overflow-x:auto}
  nav .brand,nav .sp{display:none}
  nav a{flex-direction:column;gap:0;font-size:11px;padding:6px 12px}
  nav a.on:before{display:none}
  main{padding:18px 14px 96px}
  header.top h2{font-size:21px}
  .fld{flex-direction:column;align-items:stretch;gap:10px}
  .fc{justify-content:flex-start}
  .rng input{width:100%;flex:1}
  .g2{grid-template-columns:1fr}
}
.rng output{display:none}
.rng .num{width:84px;text-align:center;font-weight:700;color:var(--acc);padding:6px 8px}
.rng .u{color:var(--mut);min-width:22px;font-size:12.5px}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

.ic{display:inline-block;width:1.15em;height:1.15em;vertical-align:-.2em;flex:none}
.ic svg{width:100%;height:100%;display:block;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.ic.f svg{fill:currentColor;stroke:none}
.brand .ic{width:26px;height:26px;color:var(--acc)}
nav a>span:first-child{display:grid;place-items:center}
nav a>span:first-child .ic{width:19px;height:19px}
.card h3 .ic{width:1.25em;height:1.25em;color:var(--acc)}
.banner .ic{color:var(--warn)}
.banner.bad .ic{color:var(--bad)}
.btn .ic{width:1.05em;height:1.05em;margin-inline-end:6px;vertical-align:-.17em}
.tag .ic,.chip .ic{width:1em;height:1em;margin-inline-end:3px}
.toast .ic{margin-inline-end:4px}
.btn.pri{background:linear-gradient(180deg,color-mix(in srgb,var(--acc) 86%,#fff),var(--acc))}
tbody tr:nth-child(even){background:color-mix(in srgb,var(--card2) 38%,transparent)}
input[type=checkbox]{accent-color:var(--acc)}
input[type=range]{-webkit-appearance:none;appearance:none;height:6px;border-radius:99px;background:var(--card2);box-shadow:inset 0 0 0 1px var(--line);cursor:pointer}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:var(--acc);border:3px solid var(--card);box-shadow:0 0 0 1px var(--acc)}
input[type=range]::-moz-range-thumb{width:14px;height:14px;border-radius:50%;background:var(--acc);border:3px solid var(--card);box-shadow:0 0 0 1px var(--acc)}
input[type=range]:disabled{opacity:.5;cursor:not-allowed}
main{animation:rise .35s ease-out}
@keyframes rise{from{opacity:0;transform:translateY(6px)}}
</style></head><body><div id="root"></div><div id="toasts"></div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let LANG=localStorage.getItem('lang')||(navigator.language||'').startsWith('fa')?'fa':'en';
if(localStorage.getItem('lang'))LANG=localStorage.getItem('lang');
let TOKEN='',ROLE='',UID=0,HARD={},ST=null,ADM=null,PAGE='overview',DRAFT=null,CL=null,CH=null,LOGS=[],LOGLV='',LOGQ='',BUSY=false;
const tr=(e,f)=>LANG==='fa'?f:e;
const fmtDur=s=>{s=Math.max(0,Math.round(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?h+'h '+m+'m':m?m+'m '+(s%60)+'s':s+'s'};
const num=(n,d)=>Number(n||0).toLocaleString(undefined,{maximumFractionDigits:d||0});
const ago=ts=>fmtDur(Date.now()/1000-ts)+tr(' ago',' پیش');
function toast(m,err){const d=document.createElement('div');d.className='toast'+(err?' err':'');d.textContent=m;$('#toasts').appendChild(d);setTimeout(()=>d.remove(),3800)}
function applyTheme(){const t=localStorage.getItem('theme')||(matchMedia('(prefers-color-scheme:light)').matches?'light':'dark');document.documentElement.dataset.theme=t;document.documentElement.dir=LANG==='fa'?'rtl':'ltr';document.documentElement.lang=LANG}
async function api(path,body){
  const o={headers:{'x-token':TOKEN}};let p=path;
  if(body!==undefined){o.method='POST';o.headers['Content-Type']='application/json';o.body=JSON.stringify(Object.assign({uid:UID},body))}
  else if(UID)p+=(p.includes('?')?'&':'?')+'uid='+UID;
  const r=await fetch(p,o);
  if(r.status===401){sessionStorage.removeItem('tok');TOKEN='';showLogin(tr('Invalid or expired link','لینک نامعتبر یا منقضی شده'));throw new Error('401')}
  if(r.status===429){toast(tr('Too many attempts','تلاش بیش از حد'),1);throw new Error('429')}
  return r.json();
}
async function doAct(action,extra,okMsg){
  try{const r=await api('/api/action',Object.assign({action},extra||{}));
    if(r.ok===false)toast(r.error||'error',1);else toast(okMsg||r.msg||'OK');
    if(r.token&&ROLE!=='admin'){TOKEN=r.token;sessionStorage.setItem('tok',TOKEN)}
    await refresh();return r}catch(e){return {ok:false}}
}
function confirmBox(o){return new Promise(res=>{const m=document.createElement('div');m.className='modal';
  m.innerHTML='<div><h3 style="margin-top:0">'+o.title+'</h3><p>'+o.body+'</p>'+(o.word?'<p>'+tr('Type','تایپ کن')+' <b>'+esc(o.word)+'</b> '+tr('to confirm','برای تایید')+'</p><input class="inp" id="cw" style="width:100%">':'')+
  '<div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn" id="cn">'+tr('Cancel','انصراف')+'</button><button class="btn '+(o.danger?'bad':'pri')+'" id="cy">'+(o.ok||tr('Confirm','تایید'))+'</button></div></div>';
  document.body.appendChild(m);const done=v=>{m.remove();res(v)};
  m.querySelector('#cn').onclick=()=>done(false);
  m.querySelector('#cy').onclick=()=>{if(o.word&&(m.querySelector('#cw').value||'').trim().toUpperCase()!==o.word.toUpperCase()){toast(tr('Word does not match','کلمه مطابقت ندارد'),1);return}done(true)}})}

/* ------------------------------ settings model ------------------------------ */
const L2={smart:['🧠 Smart','🧠 هوشمند'],safe:['🐢 Safe','🐢 امن'],normal:['⚖️ Normal','⚖️ متعادل'],fast:['🚀 Fast','🚀 سریع'],max:['⚡ Max','⚡ حداکثر']};
const TZ=[[-1000,'Server time']].concat(Array.from({length:53},(_,i)=>{const m=-720+i*30;const a=Math.abs(m);return [m,'UTC'+(m<0?'-':'+')+Math.floor(a/60)+(a%60?':30':'')]}));
const SECTIONS=[
{id:'speed',ic:'⚡',en:'Speed & pacing',fa:'سرعت و ریتم',f:[
 {k:'profile',t:'seg',opts:['smart','safe','normal','fast','max'],en:'Speed profile',fa:'پروفایل سرعت',he:'Smart imitates a human: irregular gaps, short breaks, light warm-up for new accounts.',hf:'هوشمند شبیه انسان کار می‌کنه: فاصله نامنظم، استراحت کوتاه، گرم‌کردن اکانت تازه.'},
 {k:'adv_mode',t:'sw',en:'Advanced: use my own numbers',fa:'پیشرفته: عددهای خودم',he:'Overrides the profile gap / batch / parallel joins below. Hard floors still apply.',hf:'فاصله/دسته/موازی پروفایل رو لغو می‌کنه. کف‌های امنیتی همچنان اعمال می‌شن.'},
 {k:'adv_gap',t:'rng',min:0,max:60,step:1,unit:'s',zero:1,dep:'adv_mode',en:'Join gap',fa:'فاصله بین عضویت‌ها',he:'Seconds between two joins. 0 = use profile. Never below the hard minimum.',hf:'ثانیه بین دو عضویت. ۰ = پروفایل. هیچ‌وقت کمتر از حداقل امنیتی نمی‌شه.'},
 {k:'adv_batch',t:'rng',min:0,max:10,step:1,zero:1,dep:'adv_mode',en:'Tasks per round',fa:'تعداد تسک در هر دور',he:'0 = use profile. Max is capped by the hard limit.',hf:'۰ = پروفایل. سقف توسط محدودیت امنیتی کنترل می‌شه.'},
 {k:'adv_conc',t:'rng',min:0,max:3,step:1,zero:1,dep:'adv_mode',en:'Parallel joins',fa:'عضویت همزمان',he:'Fewer is calmer. 0 = use profile.',hf:'کمتر = آرام‌تر. ۰ = پروفایل.'}]},
{id:'limits',ic:'🛡',en:'Join limits',fa:'سقف عضویت',f:[
 {k:'cap_hour',t:'rng',min:0,max:2000,step:50,zero:1,unit:'/h',en:'Hourly cap',fa:'سقف ساعتی',he:'Hard ceiling of joins per hour. 0 = off (not recommended).',hf:'حداکثر عضویت در ساعت. ۰ = خاموش (توصیه نمی‌شه).'},
 {k:'cap_day',t:'rng',min:0,max:20000,step:250,zero:1,unit:'/d',en:'Daily cap',fa:'سقف روزانه',he:'Hard ceiling of joins per 24h. 0 = off (not recommended).',hf:'حداکثر عضویت در ۲۴ ساعت. ۰ = خاموش (توصیه نمی‌شه).'},
 {k:'flood_stop',t:'rng',min:0,max:10,step:1,zero:1,unit:'×',en:'Auto-stop after floods',fa:'توقف خودکار بعد فلود',he:'Stops everything after this many flood waits within 6 hours. 0 = off.',hf:'بعد از این تعداد فلود در ۶ ساعت همه‌چیز متوقف می‌شه. ۰ = خاموش.'}]},
{id:'rhythm',ic:'😴',en:'Human rhythm',fa:'ریتم انسانی',f:[
 {k:'rest_on',t:'sw',en:'Breaks (all profiles)',fa:'استراحت (همه پروفایل‌ها)',he:'Smart profile always rests; this adds breaks to the other profiles.',hf:'پروفایل هوشمند همیشه استراحت داره؛ این برای بقیه پروفایل‌هاست.'},
 {k:'rest_every',t:'rng',min:5,max:180,step:5,unit:'m',dep:'rest_on',en:'Work between breaks',fa:'کار بین استراحت‌ها',he:'Minutes of work before a break.',hf:'دقیقه کار قبل از استراحت.'},
 {k:'rest_len',t:'rng',min:1,max:30,step:1,unit:'m',dep:'rest_on',en:'Break length',fa:'طول استراحت',he:'Average minutes per break.',hf:'میانگین دقیقه در هر استراحت.'},
 {k:'night_mode',t:'sw',en:'Calmer at night',fa:'آرام‌تر در شب',he:'Stretches the join gap during night hours.',hf:'در ساعات شب فاصله عضویت بیشتر می‌شه.'},
 {k:'night_start',t:'hour',dep:'night_mode',en:'Night starts',fa:'شروع شب',he:'',hf:''},
 {k:'night_end',t:'hour',dep:'night_mode',en:'Night ends',fa:'پایان شب',he:'',hf:''},
 {k:'night_factor',t:'rng',min:1,max:4,step:.1,unit:'×',dep:'night_mode',en:'Night slow-down',fa:'کندی شب',he:'Join gap multiplier at night.',hf:'ضریب فاصله عضویت در شب.'}]},
{id:'schedule',ic:'🗓',en:'Active hours & time zone',fa:'ساعت کار و منطقه زمانی',f:[
 {k:'active_on',t:'sw',en:'Work only inside a window',fa:'فقط در بازه مشخص کار کن',he:'Outside the window the account stays completely quiet, like a person sleeping.',hf:'بیرون بازه اکانت کاملاً ساکته، مثل آدمی که خوابه.'},
 {k:'active_start',t:'hour',dep:'active_on',en:'From',fa:'از',he:'',hf:''},
 {k:'active_end',t:'hour',dep:'active_on',en:'To',fa:'تا',he:'',hf:''},
 {k:'tz_min',t:'tz',en:'Time zone',fa:'منطقه زمانی',he:'Used for night hours and the window above.',hf:'برای ساعات شب و بازه بالا استفاده می‌شه.'}]},
{id:'cleanup',ic:'🧹',en:'Cleanup',fa:'پاکسازی',f:[
 {k:'auto_leave',t:'sw',en:'Auto-leave old channels',fa:'ترک خودکار کانال‌های قدیمی',he:'Leaves channels joined by the helper after the number of days below.',hf:'کانال‌هایی که ربات عضو شده رو بعد از تعداد روز پایین ترک می‌کنه.'},
 {k:'leave_days',t:'rng',min:3,max:30,step:1,unit:'d',dep:'auto_leave',en:'Leave after',fa:'ترک بعد از',he:'PR GRAM may revoke rewards if you leave earlier than 7 days.',hf:'اگه زودتر از ۷ روز ترک کنی ممکنه PR GRAM پاداش رو پس بگیره.'},
 {k:'clean_gap',t:'rng',min:1.5,max:30,step:.5,unit:'s',en:'Deep-clean pace',fa:'سرعت پاکسازی کامل',he:'Seconds between two leaves in a deep clean.',hf:'ثانیه بین دو ترک در پاکسازی کامل.'}]},
{id:'general',ic:'⚙️',en:'General',fa:'عمومی',f:[
 {k:'lang',t:'seg',opts:['en','fa'],lab:{en:['English','English'],fa:['فارسی','فارسی']},en:'Language (Telegram messages)',fa:'زبان (پیام‌های تلگرام)',he:'',hf:''},
 {k:'notify',t:'sw',en:'Warning messages in Telegram',fa:'پیام‌های هشدار در تلگرام',he:'Flood, limit and bot-limit pings. Human-check and revoked-session alerts always arrive.',hf:'هشدار فلود و محدودیت. هشدار تایید انسان و نشست باطل همیشه میاد.'}]}];
const PEN={flood24:['Recent flood waits','فلود اخیر'],slowed:['Running slowed after a flood','کار با سرعت کم بعد فلود'],hour_high:['Close to your hourly cap','نزدیک سقف ساعتی'],no_caps:['No hourly / daily cap set','سقف ساعتی/روزانه تنظیم نشده'],gap_low:['Join gap is short','فاصله عضویت کمه'],conc_high:['Many parallel joins','عضویت موازی زیاد'],batch_high:['Large batch per round','دسته بزرگ در هر دور'],no_rest:['No breaks enabled','استراحت فعال نیست'],new_acct:['New account on a fast profile','اکانت تازه با پروفایل سریع']};
const STXT={running:['Running','در حال کار'],slowed:['Running slowed','کند (بعد فلود)'],wait:['Waiting','در انتظار'],paused:['Paused','متوقف'],rest:['Break','استراحت'],sleep:['Sleeping (schedule)','خواب (برنامه)'],idle:['Idle','غیرفعال'],offline:['Offline','آفلاین']};

/* ------------------------------ shell ------------------------------ */
function showLogin(msg){clearInterval(window._t);$('#root').innerHTML='<div class="login"><div class="card"><h3>🪙 PR GRAM Helper</h3><p style="color:var(--mut)">'+esc(msg||tr('Open the dashboard from the Telegram bot, or paste your access token.','داشبورد رو از ربات تلگرام باز کن یا توکن دسترسی رو اینجا بذار.'))+'</p><input class="inp" id="tk" style="width:100%" placeholder="token"><div class="row" style="margin-top:12px"><button class="btn pri" id="go">'+tr('Enter','ورود')+'</button></div></div></div>';
  $('#go').onclick=()=>{sessionStorage.setItem('tok',$('#tk').value.trim());boot()}}
function shell(){
  const nav=[['overview','📊',tr('Overview','نمای کلی')],['settings','⚙️',tr('Settings','تنظیمات')],['channels','📚',tr('Channels','کانال‌ها')],['logs','🧾',tr('Logs','لاگ')]];
  if(ROLE==='admin')nav.push(['admin','🛠',tr('Admin','مدیریت')]);
  $('#root').innerHTML='<div class="app"><nav><div class="brand">🪙 PR GRAM <small>v'+esc(window.VER||'')+'</small></div>'+nav.map(n=>'<a data-p="'+n[0]+'" class="'+(PAGE===n[0]?'on':'')+'"><span>'+n[1]+'</span>'+n[2]+'</a>').join('')+'<div class="sp"></div><a id="th">🌓 <span>'+tr('Theme','تم')+'</span></a><a id="lg">🌐 <span>'+(LANG==='fa'?'English':'فارسی')+'</span></a></nav><main><header class="top" id="top"></header><div id="view"></div></main></div>';
  document.querySelectorAll('nav a[data-p]').forEach(a=>a.onclick=()=>go(a.dataset.p));
  $('#th').onclick=()=>{localStorage.setItem('theme',document.documentElement.dataset.theme==='dark'?'light':'dark');applyTheme()};
  $('#lg').onclick=()=>{LANG=LANG==='fa'?'en':'fa';localStorage.setItem('lang',LANG);applyTheme();shell();paintTop();paint(true)};
}
function go(p){PAGE=p;if(p==='settings')DRAFT=ST?Object.assign({},ST.cfg):null;if(p==='channels'){CH=null;CL=null}document.querySelectorAll('nav a[data-p]').forEach(a=>a.classList.toggle('on',a.dataset.p===p));paint(true)}
function paintTop(){
  const s=ST;let h='<h2>'+({overview:tr('Overview','نمای کلی'),settings:tr('Settings','تنظیمات'),channels:tr('Channels & cleanup','کانال‌ها و پاکسازی'),logs:tr('Live log','لاگ زنده'),admin:tr('Admin','مدیریت')}[PAGE])+'</h2><span class="sp"></span>';
  if(ROLE==='admin'&&ADM)h+='<select id="pick"><option value="0">'+tr('— choose account —','— انتخاب اکانت —')+'</option>'+ADM.accounts.map(a=>'<option value="'+a.uid+'"'+(a.uid===UID?' selected':'')+'>'+esc(a.name||a.uid)+' · '+a.uid+'</option>').join('')+'</select>';
  if(s)h+='<span class="chip"><i class="dot '+s.status.code+'"></i>'+esc(s.name||s.uid)+' · '+tr(STXT[s.status.code][0],STXT[s.status.code][1])+'</span>';
  const _tp=$('#top');if(_tp.dataset.sig!==h&&document.activeElement!==$('#pick')){_tp.innerHTML=h;_tp.dataset.sig=h}const pk=$('#pick');if(pk)pk.onchange=async()=>{UID=+pk.value;ST=null;DRAFT=null;CL=null;CH=null;if(UID){await refresh()}else{paintTop();paint(true)}};
}
async function refresh(){
  if(ROLE==='admin'){try{ADM=await api('/api/admin')}catch(e){}}
  if(UID){try{const r=await api('/api/state');if(r.ok){ST=r;if(!DRAFT)DRAFT=Object.assign({},r.cfg)}}catch(e){}}
  paintTop();paint(false);
}
function noAcct(){return '<div class="card">'+tr('Choose an account from the list above.','یک اکانت از لیست بالا انتخاب کن.')+'</div>'}
function paint(force){
  const v=$('#view');if(!v)return;
  if(PAGE==='admin'){const h=vAdmin(),a=document.activeElement;if(force||(v.dataset.sig!==h&&!(a&&a.closest&&a.closest('#view')&&/INPUT|SELECT/.test(a.tagName)))){v.innerHTML=h;v.dataset.sig=h}return}
  if(!ST){v.innerHTML=noAcct();return}
  if(PAGE==='overview')v.innerHTML=vOverview();
  else if(PAGE==='settings'){if(!DRAFT)DRAFT=Object.assign({},ST.cfg);if(force||!$('#sets'))v.innerHTML=vSettings()}
  else if(PAGE==='channels'){if(force||!$('#chv'))v.innerHTML=vChannels();else paintClean();if(force&&!CH)loadChannels()}
  else if(PAGE==='logs'){if(force||!$('#log')){v.innerHTML=vLogs()}loadLogs()}
}

/* ------------------------------ overview ------------------------------ */
function bar(used,cap,label){if(cap==null)return '<div class="lbl"><span>'+label+'</span><b>'+used+' · '+tr('no cap','بدون سقف')+'</b></div>';const p=Math.min(100,used/Math.max(1,cap)*100);return '<div class="lbl"><span>'+label+'</span><b>'+used+' / '+cap+'</b></div><div class="bar '+(p>=90?'bad':p>=70?'warn':'')+'"><i style="width:'+p+'%"></i></div>'}
function vOverview(){
  const s=ST,l=s.lim,c=s.cfg,k=s.stats,run=c.auto_join&&c.auto_fetch,code=s.status.code;
  let ban='';
  if(!s.online)ban+='<div class="banner bad">'+tr('This account is offline. Log in again from the Telegram bot.','این اکانت آفلاینه. از ربات تلگرام دوباره وارد شو.')+'</div>';
  if(l.flood_left)ban+='<div class="banner warn">⏳ '+tr('Telegram flood wait','فلود ویت تلگرام')+': <b>'+fmtDur(l.flood_left)+'</b> '+tr('left — obeyed automatically.','مانده — خودکار رعایت می‌شه.')+'</div>';
  if(l.bot_left)ban+='<div class="banner warn">🤖 '+tr('PR GRAM limit','محدودیت PR GRAM')+': <b>'+fmtDur(l.bot_left)+'</b> — '+esc(l.bot_note)+'</div>';
  if(s.stats.captchas&&c.paused)ban+='<div class="banner bad">🧩 '+tr('Human check requested: solve it in the PR GRAM chat, then press Resume.','تایید انسان بودن خواسته شده: داخل چت PR GRAM حل کن و ادامه رو بزن.')+'</div>';
  if(l.sleep_left)ban+='<div class="banner">🌙 '+tr('Outside active hours — starts in','خارج از ساعت کار — شروع تا')+' <b>'+fmtDur(l.sleep_left)+'</b></div>';
  const rk=s.risk,rc=rk.score>=75?'var(--ok)':rk.score>=50?'var(--warn)':'var(--bad)';
  return ban+
  '<div class="card hero"><div class="st"><b><i class="dot '+code+'"></i> '+tr(STXT[code][0],STXT[code][1])+'</b><small>'+esc(s.status.text)+'</small></div><div class="row">'+
   (run?'<button class="btn bad" data-x="stop">⏹ '+tr('Stop','توقف')+'</button>':'<button class="btn ok" data-x="start">▶️ '+tr('Start','شروع')+'</button>')+
   (c.paused?'<button class="btn ok" data-x="resume">▶️ '+tr('Resume','ادامه')+'</button>':'<button class="btn" data-x="pause">⏸ '+tr('Pause','مکث')+'</button>')+
   '<button class="btn" data-x="fetch">📥 '+tr('Fetch now','دریافت تسک')+'</button>'+(l.speed_pct<95?'<button class="btn" data-x="boost">🧯 '+tr('Clear slow-down','لغو کاهش سرعت')+'</button>':'')+'</div></div>'+
  '<div class="grid g4" style="margin-bottom:14px"><div class="tile"><span>GRAM</span><b>'+num(k.gram)+'</b></div><div class="tile"><span>'+tr('Balance','موجودی')+'</span><b>'+esc(k.balance||'—')+'</b></div><div class="tile"><span>'+tr('This session','این نشست')+'</span><b>+'+num(s.sess.gram)+'</b></div><div class="tile"><span>'+tr('Rate / hour','نرخ / ساعت')+'</span><b>'+num(s.sess.rate)+'</b></div><div class="tile"><span>'+tr('Completed','کامل‌شده')+'</span><b>'+num(k.completed)+'</b></div><div class="tile"><span>'+tr('Joined','عضو شده')+'</span><b>'+num(k.joined)+'</b></div><div class="tile"><span>'+tr('Errors','خطا')+'</span><b>'+num(k.errors)+'</b></div><div class="tile"><span>'+tr('Tracked channels','کانال‌های ردیابی')+'</span><b>'+num(s.tracked)+'</b></div></div>'+
  '<div class="grid g2"><div class="card"><h3>🛡 '+tr('Limits & speed','محدودیت و سرعت')+'</h3>'+bar(l.hour_used,l.hour_cap,tr('This hour','این ساعت'))+bar(l.day_used,l.day_cap,tr('Last 24h','۲۴ ساعت اخیر'))+
   '<div class="lbl" style="margin-top:8px"><span>'+tr('Speed','سرعت')+'</span><b>'+l.speed_pct+'%'+(l.slow_left?' · '+tr('full in','کامل تا')+' '+fmtDur(l.slow_left):'')+'</b></div><div class="bar ok"><i style="width:'+l.speed_pct+'%"></i></div>'+
   '<div class="row" style="margin-top:8px"><span class="tag">⏱ '+l.gap+'s</span><span class="tag">📦 '+l.batch+'</span><span class="tag">⚙ ×'+l.conc+'</span><span class="tag">'+esc(tr(L2[c.profile][0],L2[c.profile][1]))+'</span>'+(l.night_now?'<span class="tag warn">🌙 '+tr('night pace','سرعت شب')+'</span>':'')+'<span class="tag">'+tr('floods 24h','فلود ۲۴ساعت')+': '+l.flood24+'</span></div></div>'+
  '<div class="card"><h3>🧭 '+tr('Account safety','ایمنی اکانت')+'<small>'+tr('estimate','تخمینی')+'</small></h3><div class="row" style="flex-wrap:nowrap;align-items:flex-start"><div class="ring" style="--p:'+rk.score+';--c:'+rc+'"><b>'+rk.score+'</b></div><div style="flex:1">'+
   (rk.pen.length?rk.pen.map(p=>'<div class="pen"><span>'+tr(PEN[p.k][0],PEN[p.k][1])+'</span><b>−'+p.pts+'</b></div>').join(''):'<div style="color:var(--ok)">✔ '+tr('Nothing risky detected.','مورد پرخطری دیده نشد.')+'</div>')+
   '</div></div><div class="row" style="margin-top:10px"><button class="btn sm" data-p="careful">🐢 '+tr('Careful','محتاط')+'</button><button class="btn sm" data-p="balanced">⚖️ '+tr('Balanced','متعادل')+'</button><button class="btn sm" data-p="fast">🚀 '+tr('Fast','سریع')+'</button></div></div></div>'+
  '<div class="card"><h3>📜 '+tr('Recent events','رویدادهای اخیر')+'</h3><div class="ev">'+(s.recent.length?s.recent.map(e=>'<div>'+new Date(e[0]*1000).toTimeString().slice(0,8)+' <b>'+esc(e[1])+'</b></div>').join(''):'—')+'</div></div>'+
  '<div class="card"><h3>🔐 '+tr('Account','حساب')+'</h3><div class="row"><button class="btn sm" data-x="reset_stats">♻️ '+tr('Reset stats','ریست آمار')+'</button><button class="btn sm" data-x="reset_warmup">🔥 '+tr('Restart warm-up','شروع دوباره گرم‌کردن')+'</button><button class="btn sm" data-x="reset_token">🔑 '+tr('New private link','لینک خصوصی جدید')+'</button><button class="btn sm bad" data-x="logout">🚪 '+tr('Logout','خروج')+'</button></div><small style="color:var(--mut)">'+tr('Warm-up age','سن گرم‌کردن')+': '+s.lim.age_days+' '+tr('days','روز')+'</small></div>';
}

/* ------------------------------ settings ------------------------------ */
function fmtV(f,v){if(f.zero&&+v===0)return tr('auto','خودکار');return (f.k==='night_factor'||f.k==='clean_gap'?(+v).toFixed(1):v)+(f.unit||'')}
function fieldHTML(f){
  const v=DRAFT[f.k],off=f.dep&&!DRAFT[f.dep],dis=off?' disabled':'';let ctl='';
  if(f.t==='sw')ctl='<label class="sw"><input type="checkbox" data-k="'+f.k+'"'+(v?' checked':'')+'><i></i></label>';
  else if(f.t==='rng')ctl='<div class="rng"><input type="range" data-k="'+f.k+'" min="'+f.min+'" max="'+f.max+'" step="'+(f.step||1)+'" value="'+v+'"'+dis+'><output>'+fmtV(f,v)+'</output><input type="number" class="inp num" data-n="'+f.k+'" min="'+f.min+'" max="'+f.max+'" step="'+(f.step||1)+'" value="'+v+'"'+dis+'><span class="u">'+(f.unit||'')+'</span></div>';
  else if(f.t==='seg')ctl='<div class="seg">'+f.opts.map(o=>'<button type="button" data-seg="'+f.k+'" data-v="'+o+'" class="'+(v===o?'on':'')+'">'+(f.lab?f.lab[o][0]:tr(L2[o][0],L2[o][1]))+'</button>').join('')+'</div>';
  else if(f.t==='hour')ctl='<select data-k="'+f.k+'"'+dis+'>'+Array.from({length:24},(_,h)=>'<option value="'+h+'"'+(+v===h?' selected':'')+'>'+String(h).padStart(2,'0')+':00</option>').join('')+'</select>';
  else if(f.t==='tz')ctl='<div class="row"><select data-k="tz_min">'+TZ.map(o=>'<option value="'+o[0]+'"'+(+v===o[0]?' selected':'')+'>'+o[1]+'</option>').join('')+'</select><button class="btn sm" data-x="mytz">'+tr('My zone','منطقه من')+'</button></div>';
  return '<div class="fld'+(off?' off':'')+'"><div class="fl"><b>'+tr(f.en,f.fa)+'</b><small>'+tr(f.he,f.hf)+'</small></div><div class="fc">'+ctl+'</div></div>';
}
function dirty(){return Object.keys(DRAFT).filter(k=>String(DRAFT[k])!==String(ST.cfg[k])&&!(typeof DRAFT[k]==='boolean'&&(+DRAFT[k])===(+ST.cfg[k])))}
function vSettings(){
  const h=ST.hard;
  return '<div id="sets"><div class="card"><h3>🎛 '+tr('Presets','پریست‌ها')+'<small>'+tr('one tap, then fine-tune below','یک ضربه، بعد دقیق‌تر تنظیم کن')+'</small></h3><div class="row"><button class="btn" data-p="careful">🐢 '+tr('Careful — slowest, safest','محتاط — کندترین و امن‌ترین')+'</button><button class="btn" data-p="balanced">⚖️ '+tr('Balanced (recommended)','متعادل (پیشنهادی)')+'</button><button class="btn" data-p="fast">🚀 '+tr('Fast — more risk','سریع — ریسک بیشتر')+'</button></div>'+
  '<div class="banner" style="margin:12px 0 0">🔒 '+tr('Hard limits no setting can cross','محدودیت‌های سختی که هیچ تنظیمی از اون رد نمی‌شه')+': '+tr('gap ≥ ','فاصله ≥ ')+h.min_gap+'s · '+tr('≤ '+h.max_batch+' tasks/round · ≤ '+h.max_conc+' parallel joins','حداکثر '+h.max_batch+' تسک در دور · حداکثر '+h.max_conc+' عضویت همزمان')+'</div></div>'+
  SECTIONS.map(s=>'<div class="card"><h3>'+s.ic+' '+tr(s.en,s.fa)+'</h3>'+s.f.map(fieldHTML).join('')+'</div>').join('')+'<div id="sbar"></div></div>';
}
function paintSave(){
  const d=dirty(),b=$('#sbar');if(!b)return;
  b.innerHTML=d.length?'<div class="savebar"><span>✏️ '+d.length+' '+tr('unsaved change(s)','تغییر ذخیره‌نشده')+'</span><span class="sp"></span><button class="btn" data-x="discard">'+tr('Discard','لغو')+'</button><button class="btn pri" data-x="save">'+tr('Save changes','ذخیره')+'</button></div>':'';
}
async function saveSettings(){
  const d=dirty(),vals={};d.forEach(k=>vals[k]=typeof DRAFT[k]==='boolean'?(DRAFT[k]?1:0):DRAFT[k]);
  const r=await api('/api/settings',{values:vals});
  if(r.ok){ST=Object.assign(ST,r.state);DRAFT=Object.assign({},ST.cfg);toast(tr('Saved','ذخیره شد'));if(vals.lang){LANG=vals.lang;localStorage.setItem('lang',LANG);applyTheme();shell()}paintTop();paint(true)}else toast(r.error||'error',1)
}

/* ------------------------------ channels + deep clean ------------------------------ */
async function loadChannels(){const r=await api('/api/channels');if(r.ok){CH=r;if(PAGE==='channels')paintTracked()}}
function paintTracked(){const b=$('#trk');if(!b||!CH)return;
  b.innerHTML='<table><thead><tr><th>'+tr('Channel','کانال')+'</th><th>'+tr('Age','سن')+'</th><th></th></tr></thead><tbody>'+CH.rows.slice(0,300).map(r=>'<tr><td>'+esc(r.title)+' <span class="tag">'+esc(r.kind)+'</span></td><td><span class="tag '+(r.age_days>=CH.leave_days?'ok':'warn')+'">'+r.age_days+'d</span></td><td><button class="btn sm" data-lr="'+r.id+'">'+tr('Leave','ترک')+'</button></td></tr>').join('')+'</tbody></table>'}
function vChannels(){
  const s=ST;return '<div id="chv"><div class="card"><h3>📚 '+tr('Channels joined by the helper','کانال‌های عضوشده توسط ربات')+'<small>'+s.tracked+'</small></h3><div class="row" style="margin-bottom:10px"><button class="btn" data-x="leave_old">🧹 '+tr('Leave older than','ترک قدیمی‌تر از')+' '+s.cfg.leave_days+' '+tr('days','روز')+'</button><button class="btn bad" data-x="leave_all">🗑 '+tr('Leave all tracked','ترک همه ردیابی‌شده')+'</button></div><div class="scroll" id="trk" style="max-height:260px"></div></div>'+
  '<div class="card"><h3>✨ '+tr('Deep clean — leave everything else','پاکسازی کامل — ترک بقیه همه‌چیز')+'</h3><p style="color:var(--mut);margin-top:0">'+tr('Scans every group, channel and old-style chat on this account. By default it selects everything the helper did NOT join, skipping chats you own or administer and anything the helper joined recently (PR GRAM revokes rewards for early leaves). Leaving is paced and can be cancelled.','همهٔ گروه‌ها، کانال‌ها و چت‌های قدیمی این اکانت رو اسکن می‌کنه. به‌طور پیش‌فرض همه‌چیزهایی که ربات عضو نشده انتخاب می‌شه؛ چیزهایی که خودت مالک/ادمینشی و چیزهایی که ربات تازه عضو شده (PR GRAM برای ترک زودهنگام پاداش رو پس می‌گیره) رد می‌شن. ترک کردن با فاصله انجام می‌شه و قابل لغوه.')+'</p><div class="row"><button class="btn pri" data-x="scan" id="scanb">🔍 '+tr('Scan account','اسکن اکانت')+'</button></div><div id="cl"></div></div></div>';
}
function clFiltered(){const f=CL.f,q=f.q.toLowerCase();return CL.items.filter(i=>f.kinds[i.kind]&&(f.show==='all'||(f.show==='ext'&&!i.tracked)||(f.show==='trk'&&i.tracked))&&(!q||i.title.toLowerCase().includes(q)||i.username.toLowerCase().includes(q)))}
function selectable(i){return !i.owner&&(CL.f.admin||!i.admin)}
function paintClean(){
  const box=$('#cl');if(!box)return;const j=ST&&ST.clean;let h='';
  if(j&&['scanning','running','waiting'].includes(j.state)){
    const p=j.total?Math.round(j.done/j.total*100):0,eta=j.total>j.done?(j.total-j.done)*(ST.cfg.clean_gap*1.1):0;
    h+='<div class="banner" style="margin-top:12px"><b>🧹 '+tr('Cleaning…','در حال پاکسازی…')+' '+j.done+' / '+j.total+'</b> · '+tr('left','ترک شد')+' '+j.left+' · '+tr('failed','ناموفق')+' '+j.failed+(j.state==='waiting'?' · ⏳ '+tr('flood wait','فلود ویت'):'')+'<div class="bar ok"><i style="width:'+p+'%"></i></div><small style="color:var(--mut)">'+esc(j.current||'')+(eta?' · ~'+fmtDur(eta):'')+'</small><div style="margin-top:8px"><button class="btn sm bad" data-x="cancelclean">'+tr('Cancel','لغو')+'</button></div></div>';
    box.innerHTML=h;return}
  if(j&&['done','cancelled','flood_stop','error'].includes(j.state)&&!CL){h+='<div class="banner'+(j.state==='done'?'':' warn')+'" style="margin-top:12px">🧹 '+esc(j.state)+' — '+tr('left','ترک شد')+' <b>'+j.left+'</b> · '+tr('already gone','قبلاً خارج')+' '+j.gone+' · '+tr('failed','ناموفق')+' '+j.failed+(j.state==='flood_stop'?'<br>'+tr('Telegram asked for a long wait — try again later.','تلگرام انتظار طولانی خواست — بعداً دوباره امتحان کن.'):'')+'</div>'}
  if(CL){
    const f=CL.f,vis=clFiltered(),sel=CL.items.filter(i=>CL.sel.has(i.id)&&selectable(i)).length,cnt={ext:CL.items.filter(i=>!i.tracked).length,trk:CL.items.filter(i=>i.tracked).length};
    h+='<div style="margin-top:12px"><div class="row" style="margin-bottom:8px"><select id="fshow"><option value="ext"'+(f.show==='ext'?' selected':'')+'>'+tr('Not joined by helper','بدون ربات',)+' ('+cnt.ext+')</option><option value="trk"'+(f.show==='trk'?' selected':'')+'>'+tr('Joined by helper','با ربات')+' ('+cnt.trk+')</option><option value="all"'+(f.show==='all'?' selected':'')+'>'+tr('Everything','همه')+' ('+CL.items.length+')</option></select>'+
     ['channel','group','chat'].map(k=>'<label class="tag"><input type="checkbox" data-fk="'+k+'"'+(f.kinds[k]?' checked':'')+'> '+tr({channel:'Channels',group:'Groups',chat:'Old chats'}[k],{channel:'کانال',group:'گروه',chat:'چت قدیمی'}[k])+'</label>').join('')+
     '<label class="tag"><input type="checkbox" id="fadm"'+(f.admin?' checked':'')+'> '+tr('allow admin chats','شامل چت‌های ادمین')+'</label><input class="inp" id="fq" placeholder="'+tr('search','جستجو')+'" value="'+esc(f.q)+'" style="flex:1;min-width:120px"></div>'+
     '<div class="row" style="margin-bottom:8px"><button class="btn sm" data-x="selall">'+tr('Select shown','انتخاب نمایش‌داده‌ها')+'</button><button class="btn sm" data-x="selnone">'+tr('Clear','پاک کردن انتخاب')+'</button><span class="sp"></span><b>'+sel+'</b> '+tr('selected','انتخاب شده')+'</div>'+
     '<div class="scroll"><table><tbody>'+vis.slice(0,400).map(i=>'<tr><td style="width:30px"><input type="checkbox" data-id="'+i.id+'"'+(CL.sel.has(i.id)&&selectable(i)?' checked':'')+(selectable(i)?'':' disabled')+'></td><td>'+esc(i.title)+(i.username?' <small style="color:var(--mut)">@'+esc(i.username)+'</small>':'')+'</td><td><span class="tag">'+i.kind+'</span>'+(i.members?'<span class="tag">👥 '+num(i.members)+'</span>':'')+(i.owner?'<span class="tag bad">'+tr('owner','مالک')+'</span>':'')+(i.admin&&!i.owner?'<span class="tag warn">admin</span>':'')+(i.tracked?'<span class="tag '+(i.age_days>=ST.cfg.leave_days?'ok':'warn')+'">'+i.age_days+'d</span>':'')+'</td></tr>').join('')+'</tbody></table></div>'+(vis.length>400?'<small style="color:var(--mut)">'+tr('Showing 400 of ','نمایش ۴۰۰ از ')+vis.length+' — '+tr('"Select shown" still covers all of them','«انتخاب نمایش‌داده‌ها» همه رو شامل می‌شه')+'</small>':'')+
     '<div class="row" style="margin-top:12px"><button class="btn bad" data-x="doclean"'+(sel?'':' disabled')+'>🧹 '+tr('Leave selected','ترک انتخاب‌شده‌ها')+' ('+sel+')</button><label class="tag"><input type="checkbox" id="fres"'+(CL.resume?' checked':'')+'> '+tr('restart auto-join afterwards','بعدش عضویت خودکار رو دوباره روشن کن')+'</label></div></div>';
  }
  box.innerHTML=h;
}
async function runScan(){
  const b=$('#scanb');b.disabled=true;b.textContent='⏳ '+tr('Scanning…','در حال اسکن…');
  try{const r=await api('/api/action',{action:'clean_scan'});
    if(!r.ok){toast(r.error||'error',1)}else{
      CL={items:r.items,sel:new Set(),resume:true,f:{show:'ext',kinds:{channel:1,group:1,chat:1},admin:false,q:''}};
      CL.items.forEach(i=>{if(!i.tracked&&!i.owner&&!i.admin)CL.sel.add(i.id)});
      toast(tr('Found ','پیدا شد ')+r.total+tr(' chats',' چت'))}}catch(e){}
  b.disabled=false;b.textContent='🔍 '+tr('Scan account','اسکن اکانت');paintClean();
}

/* ------------------------------ logs ------------------------------ */
function vLogs(){return '<div class="row" style="margin-bottom:10px"><select id="llv"><option value="">'+tr('All levels','همه سطح‌ها')+'</option><option value="WARNING"'+(LOGLV==='WARNING'?' selected':'')+'>'+tr('Warnings+','هشدار به بالا')+'</option><option value="ERROR"'+(LOGLV==='ERROR'?' selected':'')+'>'+tr('Errors','خطاها')+'</option></select><input class="inp" id="lq" placeholder="'+tr('filter','فیلتر')+'" value="'+esc(LOGQ)+'"><span class="sp"></span><button class="btn sm" data-x="dl">⬇️ '+tr('Download','دانلود')+'</button></div><div id="log"></div>'}
async function loadLogs(){
  try{const r=await api('/api/logs?n=300'+(LOGLV?'&level='+LOGLV:'')+(ROLE==='admin'&&!UID?'&all=1':''));if(!r.ok)return;LOGS=r.logs}catch(e){return}
  const el=$('#log');if(!el)return;const at=el.scrollHeight-el.scrollTop-el.clientHeight<40;
  el.innerHTML=LOGS.filter(l=>!LOGQ||l.msg.toLowerCase().includes(LOGQ.toLowerCase())).map(l=>'<div class="l '+l.level+'">'+new Date(l.ts*1000).toTimeString().slice(0,8)+' | '+esc(l.msg)+'</div>').join('');
  if(at)el.scrollTop=el.scrollHeight;
}

/* ------------------------------ admin ------------------------------ */
function vAdmin(){
  if(!ADM)return '<div class="card">…</div>';const t=ADM.totals;
  return '<div class="grid g4" style="margin-bottom:14px"><div class="tile"><span>'+tr('Users','کاربران')+'</span><b>'+ADM.users_total+' / '+ADM.max_users+'</b></div><div class="tile"><span>'+tr('Online','آنلاین')+'</span><b>'+ADM.online+'</b></div><div class="tile"><span>GRAM</span><b>'+num(t.gram)+'</b></div><div class="tile"><span>'+tr('Joins','عضویت‌ها')+'</span><b>'+num(t.joined)+'</b></div><div class="tile"><span>'+tr('Completed','کامل‌شده')+'</span><b>'+num(t.completed)+'</b></div><div class="tile"><span>'+tr('Errors','خطا')+'</span><b>'+num(t.errors)+'</b></div></div>'+
  '<div class="card"><h3>🔓 '+tr('Access','دسترسی')+'</h3><div class="row"><span class="tag">'+(ADM.open_access?tr('open','باز'):tr('invite-only','فقط دعوتی'))+'</span><button class="btn sm" data-x="access">'+tr('Toggle','تغییر')+'</button><input class="inp" id="tid" placeholder="user id" style="width:150px"><button class="btn sm" data-x="allow">'+tr('Allow','اجازه')+'</button><button class="btn sm" data-x="disallow">'+tr('Disallow','حذف اجازه')+'</button><button class="btn sm bad" data-x="ban">'+tr('Ban','مسدود')+'</button><button class="btn sm" data-x="unban">'+tr('Unban','رفع مسدودی')+'</button></div></div>'+
  '<div class="card"><h3>👥 '+tr('Accounts','اکانت‌ها')+'</h3><div class="scroll"><table><thead><tr><th>'+tr('User','کاربر')+'</th><th>'+tr('Status','وضعیت')+'</th><th>'+tr('Speed','سرعت')+'</th><th>GRAM</th><th>24h</th><th></th></tr></thead><tbody>'+
  ADM.accounts.map(a=>'<tr><td>'+esc(a.name||a.uid)+'<br><small style="color:var(--mut)">'+a.uid+(a.username?' · @'+esc(a.username):'')+'</small></td><td><i class="dot '+a.code+'"></i> '+esc(a.status)+(a.banned?' <span class="tag bad">banned</span>':'')+'</td><td>'+a.speed_pct+'%</td><td>'+num(a.gram)+'</td><td>'+a.joined_d+'</td><td class="row"><button class="btn sm" data-open="'+a.uid+'">'+tr('Open','باز کردن')+'</button>'+(a.online?(a.running?'<button class="btn sm bad" data-ax="stop" data-u="'+a.uid+'">⏹</button>':'<button class="btn sm ok" data-ax="start" data-u="'+a.uid+'">▶️</button>'):'')+'</td></tr>').join('')+'</tbody></table></div></div>';
}

/* ------------------------------ events ------------------------------ */
document.addEventListener('click',async e=>{
  const t=e.target.closest('button,[data-p]');if(!t)return;
  if(t.dataset.x){const x=t.dataset.x;
    if(['start','stop','pause','resume','fetch','boost','reset_warmup','leave_old'].includes(x))return doAct(x);
    if(x==='reset_stats'){if(await confirmBox({title:tr('Reset stats?','ریست آمار؟'),body:tr('Counters go back to zero.','شمارنده‌ها صفر می‌شن.'),danger:1}))doAct(x)}
    else if(x==='reset_token'){if(await confirmBox({title:tr('Create a new private link?','لینک خصوصی جدید؟'),body:tr('The old link stops working. Use the new link from Telegram next time.','لینک قبلی از کار می‌افته. دفعه بعد از تلگرام لینک جدید رو باز کن.')}))doAct(x)}
    else if(x==='logout'){if(await confirmBox({title:tr('Log out?','خروج؟'),body:tr('The session is deleted from the server.','نشست از سرور پاک می‌شه.'),danger:1}))doAct(x)}
    else if(x==='leave_all'){if(await confirmBox({title:tr('Leave ALL tracked channels?','ترک همه کانال‌های ردیابی‌شده؟'),body:tr('PR GRAM may revoke rewards for channels left before 7 days. Auto-join stops.','PR GRAM ممکنه برای کانال‌های کمتر از ۷ روز پاداش رو پس بگیره. عضویت خودکار متوقف می‌شه.'),danger:1,word:'LEAVE'}))doAct(x)}
    else if(x==='save')saveSettings();
    else if(x==='discard'){DRAFT=Object.assign({},ST.cfg);paint(true)}
    else if(x==='mytz'){DRAFT.tz_min=-new Date().getTimezoneOffset();paint(true);paintSave()}
    else if(x==='scan')runScan();
    else if(x==='selall'){clFiltered().forEach(i=>selectable(i)&&CL.sel.add(i.id));paintClean()}
    else if(x==='selnone'){CL.sel.clear();paintClean()}
    else if(x==='cancelclean')doAct('clean_cancel');
    else if(x==='doclean'){const ids=CL.items.filter(i=>CL.sel.has(i.id)&&selectable(i)).map(i=>i.id);
      if(await confirmBox({title:tr('Leave '+ids.length+' chats?','ترک '+ids.length+' چت؟'),body:tr('They will be left one by one with safe pauses. This cannot be undone automatically. Auto-join is paused meanwhile.','یکی‌یکی با فاصله امن ترک می‌شن. به‌صورت خودکار قابل برگشت نیست. عضویت خودکار تو این مدت متوقف می‌شه.'),danger:1,word:'LEAVE',ok:tr('Leave them','ترک کن')})){
        const r=await doAct('clean_start',{ids,resume:CL.resume});if(r.ok!==false){CL=null;paintClean()}}}
    else if(x==='dl'){const b=new Blob([LOGS.map(l=>new Date(l.ts*1000).toISOString()+' '+l.level+' '+l.msg).join('\n')],{type:'text/plain'});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='prgram.log';a.click()}
    else if(x==='access')doAct('toggle_access');
    else if(['allow','disallow','ban','unban'].includes(x)){const id=+($('#tid').value||0);if(!id)return toast(tr('Enter a user id','آیدی کاربر رو وارد کن'),1);const r=await api('/api/action',{action:x,target:id});toast(r.ok?r.msg:r.error,!r.ok);refresh()}
    return}
  if(t.dataset.p&&['careful','balanced','fast'].includes(t.dataset.p)){if(await confirmBox({title:tr('Apply preset?','اعمال پریست؟'),body:tr('This overwrites your speed, caps and rhythm settings.','سرعت، سقف‌ها و ریتم تو رو بازنویسی می‌کنه.')})){await doAct('preset',{name:t.dataset.p});DRAFT=null;await refresh();paint(true)}return}
  if(t.dataset.seg){DRAFT[t.dataset.seg]=t.dataset.v;paint(true);paintSave();return}
  if(t.dataset.lr){doAct('leave_row',{ids:[+t.dataset.lr]});return}
  if(t.dataset.open){UID=+t.dataset.open;ST=null;DRAFT=null;PAGE='overview';shell();await refresh();return}
  if(t.dataset.ax){await api('/api/action',{action:t.dataset.ax,uid:+t.dataset.u});toast('OK');refresh()}
});
document.addEventListener('input',e=>{
  const t=e.target;
  if(t.dataset.k&&DRAFT){let v=t.type==='checkbox'?t.checked:t.type==='range'?+t.value:(t.tagName==='SELECT'&&/^-?\d+$/.test(t.value)?+t.value:t.value);DRAFT[t.dataset.k]=v;
    const f=SECTIONS.flatMap(s=>s.f).find(f=>f.k===t.dataset.k);
    if(t.type==='range'){t.nextElementSibling.textContent=fmtV(f,v)}
    if(t.type==='checkbox')paint(true);paintSave();return}
  if(t.dataset.id&&CL){const id=+t.dataset.id;t.checked?CL.sel.add(id):CL.sel.delete(id);paintClean();return}
  if(t.dataset.fk&&CL){CL.f.kinds[t.dataset.fk]=t.checked?1:0;paintClean();return}
  if(t.id==='fadm'&&CL){CL.f.admin=t.checked;paintClean();return}
  if(t.id==='fres'&&CL){CL.resume=t.checked;return}
  if(t.id==='fq'&&CL){CL.f.q=t.value;const p=t.selectionStart;paintClean();const n=$('#fq');n.focus();n.setSelectionRange(p,p);return}
  if(t.id==='lq'){LOGQ=t.value;loadLogs()}
});
document.addEventListener('change',e=>{
  if(e.target.id==='fshow'&&CL){CL.f.show=e.target.value;paintClean()}
  if(e.target.id==='llv'){LOGLV=e.target.value;loadLogs()}
});

async function boot(){
  applyTheme();
  const u=new URL(location.href);let t=u.searchParams.get('t')||new URLSearchParams(location.hash.slice(1)).get('t');
  if(t){sessionStorage.setItem('tok',t);history.replaceState(null,'',location.pathname)}
  TOKEN=sessionStorage.getItem('tok')||'';if(!TOKEN)return showLogin();
  let me;try{me=await fetch('/api/me',{headers:{'x-token':TOKEN}});if(me.status!==200){sessionStorage.removeItem('tok');return showLogin(tr('Invalid or expired link','لینک نامعتبر یا منقضی شده'))}me=await me.json()}catch(e){return showLogin('offline')}
  ROLE=me.role;UID=me.uid;HARD=me.hard;window.VER=me.version;PAGE=ROLE==='admin'?'admin':'overview';
  shell();await refresh();
  clearInterval(window._t);window._t=setInterval(()=>{if(!document.hidden)refresh().then(()=>{if(PAGE==='channels')paintClean()})},3000);
}

/* typed numbers <-> sliders */
document.addEventListener('input',e=>{
  const t=e.target;
  if(t.type==='range'&&t.dataset.k){const n=document.querySelector('input[data-n="'+t.dataset.k+'"]');if(n)n.value=t.value;return}
  if(!t.dataset.n||!DRAFT||t.value==='')return;
  let v=parseFloat(t.value);if(isNaN(v))return;
  v=Math.max(+t.min,Math.min(+t.max,v));if(Number.isInteger(+t.step))v=Math.round(v);
  DRAFT[t.dataset.n]=v;
  const r=document.querySelector('input[type=range][data-k="'+t.dataset.n+'"]');if(r)r.value=v;
  paintSave();
});
document.addEventListener('change',e=>{
  const t=e.target;if(!t.dataset.n||!DRAFT)return;
  let v=parseFloat(t.value);if(isNaN(v))v=+DRAFT[t.dataset.n];
  v=Math.max(+t.min,Math.min(+t.max,v));if(Number.isInteger(+t.step))v=Math.round(v);
  t.value=v;DRAFT[t.dataset.n]=v;
  const r=document.querySelector('input[type=range][data-k="'+t.dataset.n+'"]');if(r)r.value=v;
  paintSave();
});


/* emoji -> svg icons */
const ICO={"coin": ["<circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M14.5 9.5c-.5-1-1.5-1.5-2.5-1.5-1.5 0-2.5.8-2.5 2s1 1.7 2.5 2 2.5.8 2.5 2-1 2-2.5 2c-1 0-2-.5-2.5-1.5M12 6v2M12 16v2\"/>", false], "chart": ["<path d=\"M3 3v18h18\"/><path d=\"M8 16v-4M13 16V8M18 16v-6\"/>", false], "sliders": ["<path d=\"M4 6h8M16 6h4M4 12h2M10 12h10M4 18h12M20 18h0\"/><circle cx=\"14\" cy=\"6\" r=\"2\"/><circle cx=\"8\" cy=\"12\" r=\"2\"/><circle cx=\"18\" cy=\"18\" r=\"2\"/>", false], "layers": ["<path d=\"M12 3 3 8l9 5 9-5z\"/><path d=\"M3 12.5l9 5 9-5M3 17l9 5 9-5\"/>", false], "file": ["<path d=\"M6 3h9l4 4v14H6z\"/><path d=\"M14 3v5h5M9 13h6M9 17h6\"/>", false], "wrench": ["<path d=\"M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.2-.7-.7-2.2z\"/>", false], "theme": ["<circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M12 3a9 9 0 0 1 0 18z\" fill=\"currentColor\"/>", false], "globe": ["<circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18\"/>", false], "stop": ["<rect x=\"6\" y=\"6\" width=\"12\" height=\"12\" rx=\"2\"/>", true], "play": ["<path d=\"M7 4l13 8-13 8z\"/>", true], "pause": ["<rect x=\"6\" y=\"5\" width=\"4\" height=\"14\" rx=\"1\"/><rect x=\"14\" y=\"5\" width=\"4\" height=\"14\" rx=\"1\"/>", true], "download": ["<path d=\"M12 3v12M7 10l5 5 5-5M4 20h16\"/>", false], "zap": ["<path d=\"M13 2 4 14h7l-1 8 9-12h-7z\"/>", false], "shield": ["<path d=\"M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z\"/>", false], "compass": ["<circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M15.5 8.5l-2 5-5 2 2-5z\"/>", false], "list": ["<path d=\"M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01\"/>", false], "lock": ["<rect x=\"5\" y=\"11\" width=\"14\" height=\"10\" rx=\"2\"/><path d=\"M8 11V7a4 4 0 0 1 8 0v4\"/>", false], "unlock": ["<rect x=\"5\" y=\"11\" width=\"14\" height=\"10\" rx=\"2\"/><path d=\"M8 11V7a4 4 0 0 1 7.5-2\"/>", false], "refresh": ["<path d=\"M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5\"/>", false], "flame": ["<path d=\"M12 3c1 4 5 5 5 10a5 5 0 0 1-10 0c0-2 1-3 2-4 .5 2 1.5 2.5 2 2 .5-3-1-5 1-8z\"/>", false], "key": ["<circle cx=\"8\" cy=\"15\" r=\"4\"/><path d=\"M10.8 12.2 20 3M16 7l3 3\"/>", false], "logout": ["<path d=\"M9 21H5V3h4M16 17l5-5-5-5M21 12H9\"/>", false], "hourglass": ["<path d=\"M6 2h12M6 22h12M8 2v4l4 6-4 6v4M16 2v4l-4 6 4 6v4\"/>", false], "bot": ["<rect x=\"4\" y=\"8\" width=\"16\" height=\"12\" rx=\"3\"/><path d=\"M12 8V4M9 14h.01M15 14h.01\"/>", false], "puzzle": ["<path d=\"M19.4 13H17a2 2 0 1 0 0-4h2.4V5.6a.6.6 0 0 0-.6-.6H15V3a2 2 0 0 0-4 0v2H7.2a.6.6 0 0 0-.6.6V9H4a2 2 0 1 0 0 4h2.6v4.4c0 .3.3.6.6.6H11v-2a2 2 0 1 1 4 0v2h3.8c.3 0 .6-.3.6-.6z\"/>", false], "moon": ["<path d=\"M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z\"/>", false], "coffee": ["<path d=\"M4 8h13v6a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5zM17 10h2a2 2 0 0 1 0 4h-2M7 2v3M11 2v3\"/>", false], "cpu": ["<rect x=\"6\" y=\"6\" width=\"12\" height=\"12\" rx=\"2\"/><rect x=\"10\" y=\"10\" width=\"4\" height=\"4\"/><path d=\"M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4\"/>", false], "slow": ["<path d=\"M22 17l-8.5-8.5-5 5L2 7M16 17h6v-6\"/>", false], "scale": ["<path d=\"M12 3v18M5 21h14M5 7h14M5 7l-3 7a3 3 0 0 0 6 0zM19 7l-3 7a3 3 0 0 0 6 0z\"/>", false], "rocket": ["<path d=\"M5 15c-1 1-2 4-2 6 2 0 5-1 6-2M12 15l-3-3a22 22 0 0 1 3-8c3-3 8-2 9-2 0 1 1 6-2 9a22 22 0 0 1-8 3z\"/><circle cx=\"15\" cy=\"9\" r=\"1\"/>", false], "sparkles": ["<path d=\"M12 3l2 6 6 2-6 2-2 6-2-6-6-2 6-2z\"/>", false], "broom": ["<path d=\"M14 3l7 7-9 9-7-7zM3 21l4-4\"/>", false], "trash": ["<path d=\"M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3\"/>", false], "search": ["<circle cx=\"11\" cy=\"11\" r=\"7\"/><path d=\"M21 21l-4.3-4.3\"/>", false], "pencil": ["<path d=\"M4 20l1-4L16.5 4.5a2.1 2.1 0 0 1 3 3L8 19z\"/>", false], "users": ["<circle cx=\"9\" cy=\"7\" r=\"4\"/><path d=\"M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8\"/>", false], "user": ["<circle cx=\"12\" cy=\"8\" r=\"4\"/><path d=\"M4 21a8 8 0 0 1 16 0\"/>", false], "check": ["<path d=\"M5 12l5 5 9-10\"/>", false], "x": ["<path d=\"M6 6l12 12M18 6 6 18\"/>", false], "clock": ["<circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M12 7v5l3 2\"/>", false], "timer": ["<circle cx=\"12\" cy=\"13\" r=\"8\"/><path d=\"M12 9v4l2 2M9 2h6\"/>", false], "package": ["<path d=\"M21 8l-9-5-9 5v8l9 5 9-5zM3 8l9 5 9-5M12 13v8\"/>", false], "alert": ["<path d=\"M12 3 2 20h20z\"/><path d=\"M12 10v4M12 17h.01\"/>", false]},EMO={"🪙": "coin", "📊": "chart", "⚙": "sliders", "🎛": "sliders", "📚": "layers", "🧾": "file", "🛠": "wrench", "🌓": "theme", "🌐": "globe", "⏹": "stop", "▶": "play", "⏸": "pause", "📥": "download", "⬇": "download", "🧯": "zap", "⚡": "zap", "🛡": "shield", "🧭": "compass", "📜": "list", "🔐": "lock", "🔒": "lock", "🔓": "unlock", "♻": "refresh", "🔥": "flame", "🔑": "key", "🚪": "logout", "⏳": "hourglass", "🤖": "bot", "🧩": "puzzle", "🌙": "moon", "😴": "coffee", "🧠": "cpu", "🐢": "slow", "⚖": "scale", "🚀": "rocket", "✨": "sparkles", "🧹": "broom", "🗑": "trash", "🔍": "search", "✏": "pencil", "👥": "users", "👤": "user", "✔": "check", "✅": "check", "✖": "x", "🕐": "clock", "⏱": "timer", "📦": "package", "⚠": "alert"};
const EMT=new RegExp('(?:'+Object.keys(EMO).join('|')+')\uFE0F?');
const EMG=new RegExp(EMT.source,'g');
function mkIcon(n){const d=ICO[n];if(!d)return null;const s=document.createElement('span');s.className='ic'+(d[1]?' f':'');
  s.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true">'+d[0]+'</svg>';return s}
function iconify(root){
  const w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode:n=>{
    if(!EMT.test(n.nodeValue))return NodeFilter.FILTER_REJECT;
    const p=n.parentElement;if(!p||p.closest('option,textarea,script,style,title,#log'))return NodeFilter.FILTER_REJECT;
    return NodeFilter.FILTER_ACCEPT}});
  const list=[];while(w.nextNode())list.push(w.currentNode);
  list.forEach(n=>{
    const s=n.nodeValue,frag=document.createDocumentFragment();let last=0,m;EMG.lastIndex=0;
    while((m=EMG.exec(s))){
      if(m.index>last)frag.appendChild(document.createTextNode(s.slice(last,m.index)));
      const ic=mkIcon(EMO[m[0].replace(/\uFE0F/g,'')]);frag.appendChild(ic||document.createTextNode(m[0]));
      last=m.index+m[0].length}
    if(last<s.length)frag.appendChild(document.createTextNode(s.slice(last)));
    n.parentNode.replaceChild(frag,n)})}
const ICOOBS=new MutationObserver(()=>{ICOOBS.disconnect();try{iconify(document.body)}catch(e){}ICOGO()});
function ICOGO(){ICOOBS.observe(document.body,{childList:true,subtree:true,characterData:true})}
iconify(document.body);ICOGO();

boot();
</script></body></html>"""


# ===========================================================================
# PATCH 3.4  -  scalability layer, stateless web tokens, helpers
# ===========================================================================
TICK_CONC = max(8, _env_int("TICK_CONC", 48))          # accounts ticking at the same moment
TICK_BURST = max(20, _env_int("TICK_BURST", 200))      # new ticks started per 2 s watchdog cycle
BOT_RATE = float(max(5, _env_int("BOT_RATE", 25)))     # bot messages / edits per second (Bot API allows ~30)
TRUST_PROXY = _env_bool("TRUST_PROXY", False)          # trust X-Forwarded-For only behind your own reverse proxy

_TICK_SEM: Optional[asyncio.Semaphore] = None
_NEXT_TICK: Dict[int, float] = {}
_STAT_BUF: Dict[int, Dict[str, float]] = {}
_STAT_EV: Dict[int, Tuple[str, float]] = {}
_JS_CACHE: Dict[int, Dict[int, Tuple[float, int]]] = {}
_EPOCH: Dict[int, int] = {}
_ADM: Dict[str, Any] = {"ts": 0.0, "val": None}
_TB: Dict[str, float] = {"tokens": BOT_RATE, "ts": time.monotonic()}
_WEB_SECRET: Optional[bytes] = None

_DB_ALTERS = (
    "ALTER TABLE pg_joins ADD COLUMN kind TEXT DEFAULT ''",
    "ALTER TABLE pg_cfg ADD COLUMN auto_leave INTEGER DEFAULT 0",
    "ALTER TABLE pg_cfg ADD COLUMN profile TEXT DEFAULT 'max'",
    "ALTER TABLE pg_cfg ADD COLUMN lang TEXT DEFAULT ''",
    "ALTER TABLE pg_stats ADD COLUMN last_ev_ts REAL DEFAULT 0",
    "ALTER TABLE pg_state ADD COLUMN day_cap REAL DEFAULT 0",
    "ALTER TABLE pg_state ADD COLUMN day_until REAL DEFAULT 0",
    "ALTER TABLE pg_state ADD COLUMN slow0 REAL DEFAULT 1",
    "ALTER TABLE pg_state ADD COLUMN slow_end REAL DEFAULT 0",
    "ALTER TABLE pg_cfg ADD COLUMN leave_days INTEGER DEFAULT 8",
    "ALTER TABLE pg_cfg ADD COLUMN adv_mode INTEGER DEFAULT 0",
    "ALTER TABLE pg_cfg ADD COLUMN adv_gap REAL DEFAULT 0",
    "ALTER TABLE pg_cfg ADD COLUMN adv_batch INTEGER DEFAULT 0",
    "ALTER TABLE pg_cfg ADD COLUMN adv_conc INTEGER DEFAULT 0",
    "ALTER TABLE pg_cfg ADD COLUMN night_start INTEGER DEFAULT 2",
    "ALTER TABLE pg_cfg ADD COLUMN night_end INTEGER DEFAULT 6",
    "ALTER TABLE pg_cfg ADD COLUMN warmup_ts REAL DEFAULT 0",
)


class LoginAborted(Exception):
    """Raised by _finish_login when the account could not be activated (the user was already told)."""


def _migrate(c: sqlite3.Connection) -> None:
    """Versioned schema migrations (PRAGMA user_version)."""
    ver = int(c.execute("PRAGMA user_version").fetchone()[0])
    if ver < 1:
        try:
            c.execute("UPDATE pg_cfg SET profile='max' WHERE profile='normal'")
        except sqlite3.Error:
            pass
        c.execute("PRAGMA user_version=1")
        ver = 1
    if ver < 2:
        for stmt in ("CREATE INDEX IF NOT EXISTS idx_joins_uid_peer ON pg_joins(uid, peer_id)",
                     "CREATE TABLE IF NOT EXISTS pg_webep (uid INTEGER PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0)",
                     "DELETE FROM pg_webtok"):           # plaintext tokens of v3.3 are gone for good
            try:
                c.execute(stmt)
            except sqlite3.Error as e:
                log.warning(f"migration v2: {e}")
        c.execute("PRAGMA user_version=2")


# --- write-behind statistics ------------------------------------------------------------------------
def flush_stats(uid: Optional[int] = None) -> None:
    """Write buffered counters / last-event rows in ONE transaction (all users, or just `uid`)."""
    pending = set(_STAT_BUF) | set(_STAT_EV)
    uids = pending if uid is None else ({uid} & pending)
    if not uids:
        return
    batch = {u: (_STAT_BUF.pop(u, None), _STAT_EV.pop(u, None)) for u in uids}
    try:
        db_exec("BEGIN")
        try:
            for u, (inc, ev) in batch.items():
                db_exec("INSERT OR IGNORE INTO pg_stats (uid) VALUES (?)", (u,))
                for col, val in (inc or {}).items():
                    db_exec(f"UPDATE pg_stats SET {col}={col}+? WHERE uid=?", (val, u))   # col is whitelisted
                if ev:
                    db_exec("UPDATE pg_stats SET last_ev=?, last_ev_ts=? WHERE uid=?", (ev[0], ev[1], u))
            db_exec("COMMIT")
        except Exception:
            db_exec("ROLLBACK")
            raise
    except Exception as e:
        for u, (inc, ev) in batch.items():                  # nothing is lost: put it back and retry next cycle
            b = _STAT_BUF.setdefault(u, {})
            for col, val in (inc or {}).items():
                b[col] = b.get(col, 0) + val
            if ev and u not in _STAT_EV:
                _STAT_EV[u] = ev
        log.warning(f"flush_stats: {type(e).__name__}: {e}")


def joins_oldest(uid: int) -> list:
    return list(db_exec("SELECT joined_at FROM pg_joins WHERE uid=? ORDER BY joined_at LIMIT 1", (uid,)).fetchall())


# --- stateless dashboard tokens ---------------------------------------------------------------------
def _web_secret() -> bytes:
    global _WEB_SECRET
    if _WEB_SECRET:
        return _WEB_SECRET
    raw = _env_str("WEB_SECRET")
    if not raw:
        p = DATA_DIR / "web_secret.key"
        if p.exists():
            raw = p.read_text(encoding="utf-8").strip()
        if not raw:
            import secrets
            raw = secrets.token_urlsafe(48)
            p.write_text(raw, encoding="utf-8")
            try:
                os.chmod(p, 0o600)
            except Exception as _swallow_e:
                _swallow(_swallow_e)
    _WEB_SECRET = raw.encode()
    return _WEB_SECRET


def _tok_sig(uid: int, epoch: int) -> str:
    mac = hmac.new(_web_secret(), f"u:{uid}:{epoch}".encode(), hashlib.sha256).digest()[:18]
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def _epoch(uid: int) -> int:
    e = _EPOCH.get(uid)
    if e is None:
        r = db_exec("SELECT epoch FROM pg_webep WHERE uid=?", (uid,)).fetchone()
        e = int(r["epoch"]) if r else 0
        _EPOCH[uid] = e
    return e


# --- scheduling / throttling helpers ----------------------------------------------------------------
def _tick_gap(uid: int) -> float:
    """How long this account may sleep before the scheduler looks at it again."""
    cfg = get_cfg(uid)
    if cfg["paused"]:
        return random.uniform(10, 15)
    if cfg["auto_join"] and cfg["auto_fetch"]:
        return random.uniform(1.8, 2.6)
    if cfg["auto_leave"]:
        return random.uniform(12, 18)
    return random.uniform(20, 30)


async def _bot_throttle() -> None:
    """Token bucket: all bot messages / edits of ALL users together stay under BOT_RATE per second."""
    while True:
        now = time.monotonic()
        _TB["tokens"] = min(BOT_RATE, _TB["tokens"] + (now - _TB["ts"]) * BOT_RATE)
        _TB["ts"] = now
        if _TB["tokens"] >= 1.0:
            _TB["tokens"] -= 1.0
            return
        await asyncio.sleep((1.0 - _TB["tokens"]) / BOT_RATE + 0.01)


async def _probe_slot(uid: int) -> None:
    """Light pacing for the pre-join probes (ResolveUsername / GetParticipant have their own flood limits)."""
    f = _farm(uid)
    async with _lock(uid, "probe"):
        wait = f.get("next_probe", 0.0) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        f["next_probe"] = time.time() + random.uniform(0.35, 0.8) * max(1.0, slow_eff(uid))


def _batch_cap(uid: int) -> int:
    """Largest batch this account can use (stable number for the dashboard)."""
    cfg = get_cfg(uid)
    if cfg["adv_mode"] and cfg["adv_batch"] > 0:
        return min(HARD_MAX_BATCH, int(cfg["adv_batch"]))
    return min(HARD_MAX_BATCH, 8 if is_smart(uid) else int(CONFIG["pg_batch"]))


def gc_idle() -> None:
    """Periodic garbage collection of per-user runtime state."""
    now = time.time()
    for u in list(_URL_CLAIMS):
        d = _URL_CLAIMS[u]
        for k in [k for k, ts in d.items() if now - ts > URL_TTL]:
            del d[k]
        if not d:
            _URL_CLAIMS.pop(u, None)
    for u in list(_DEDUPE):
        d = _DEDUPE[u]
        for k in [k for k, ts in d.items() if now - ts > DEDUPE_TTL]:
            del d[k]
        if not d:
            _DEDUPE.pop(u, None)
    for u, f in list(_FARM.items()):
        fl = f.get("fails")
        if fl is not None and len(fl) > 200:
            fl.clear()
        ft = f.get("flood_times")
        if ft:
            ft[:] = [x for x in ft if now - x < 86400]
    for u in list(_NEXT_TICK):
        if u not in ACCOUNTS:
            _NEXT_TICK.pop(u, None)
    for u in list(_JS_CACHE):
        d = _JS_CACHE[u]
        for k in [k for k, (ts, _v) in d.items() if now - ts > 60]:
            del d[k]
        if not d:
            _JS_CACHE.pop(u, None)
    if len(_CFG_CACHE) > 5000:
        _CFG_CACHE.clear()
    w = RT.web
    if w is not None:
        for ip in list(w._fails):
            keep = [t for t in w._fails[ip] if now - t < 600]
            if keep:
                w._fails[ip] = keep
            else:
                w._fails.pop(ip, None)


async def cmd_run() -> None:
    init_db()
    _init_log_ring()
    loop = asyncio.get_running_loop()
    RT.stop_evt = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, RT.stop_evt.set)
        except (NotImplementedError, RuntimeError):
            pass
    log.info(f"PR GRAM Helper v{VERSION} starting…  (colored buttons: {'on' if HAS_BTN_STYLE else 'emoji fallback'})")
    _fernet()
    backoff = 3.0
    try:
        while not RT.stop_evt.is_set():
            started = time.time()
            try:
                await boot_bot()
                break
            except FatalConfig as e:
                log.error(f"❌ {e}")
                break
            except (KeyboardInterrupt, asyncio.CancelledError):
                break
            except Exception as e:
                log.error(f"boot failed: {type(e).__name__}: {e}")
                await shutdown()
                if time.time() - started > 120:
                    backoff = 3.0
                try:
                    await asyncio.wait_for(RT.stop_evt.wait(), backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
    finally:
        await shutdown()


# [patch-hardening:startup]
def _startup_security_errors() -> list:
    errs = []
    if not _env_bool("ALLOW_PLAINTEXT_SESSIONS", False):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            errs.append("`cryptography` is not installed: Telegram sessions would be stored in PLAIN TEXT. "
                        "pip install cryptography   (or set ALLOW_PLAINTEXT_SESSIONS=1 to accept the risk)")
    if _env_bool("STRICT_KEY", False) and not _env_str("SESSION_KEY"):
        errs.append("STRICT_KEY=1 but SESSION_KEY is not set in the environment. Generate one with: "
                    "python -c \"from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())\"")
    if not _env_str("SESSION_KEY") and KEY_FILE.exists():
        print(f"⚠️  The session encryption key lives in {KEY_FILE} next to the data. Move it to the SESSION_KEY "
              "environment variable and delete the file (keep a copy somewhere else, without it sessions are lost).",
              file=sys.stderr)
    return errs


def main() -> int:
    for _m in _startup_security_errors():
        print("❌ " + _m, file=sys.stderr)
        return 1
    if not CONFIG["bot_token"]:
        print("❌ BOT_TOKEN is not set.", file=sys.stderr)
        return 1
    if not admin_set() and not _env_bool("ALLOW_FIRST_OWNER", False):
        print("❌ Set OWNER_ID (or ADMIN_IDS) in .env. (ALLOW_FIRST_OWNER=1 restores the old 'first /start wins' behaviour - not recommended.)", file=sys.stderr)
        return 1
    if _api_creds()[0] == PUBLIC_API_ID:
        print("⚠️  Using the shared public API_ID. For many users create your own at my.telegram.org "
              "and set API_ID / API_HASH.", file=sys.stderr)
    try:
        asyncio.run(cmd_run())
    except KeyboardInterrupt:
        pass
    return 0


# ===========================================================================
# PATCH 3.5  -  VIEWS mode: while joining is flooded, earn from "Views" instead
# ===========================================================================
PATCH_VIEWS_APPLIED = True
VERSION = "3.5"

VIEWS_HARD_MIN_GAP = 2.0     # seconds between two "Next Post" presses - no setting can go below this
VIEWS_MIN_FLOOD = 45.0       # a join flood shorter than this is simply waited out
_VW_EM = "👁"

CONFIG.setdefault("pg_mirror", _env_str("PG_MIRROR", "gram_prbot"))
CONFIG.setdefault("pg_views_category", _env_str("PG_VIEWS_CATEGORY", "Views"))

EXTRA_COLS.update({
    "views_on": ("INTEGER", 1),        # auto-switch to Views when joining is flooded
    "views_mirror": ("INTEGER", 1),    # allow the mirror bot when the main bot rate-limits
    "views_gap": ("REAL", 6.0),        # seconds between two posts
})
SETTINGS_SPEC.update({
    "views_on": ("bool", 0, 1),
    "views_mirror": ("bool", 0, 1),
    "views_gap": ("float", VIEWS_HARD_MIN_GAP, 60.0),
})
_STAT_COLS = tuple(_STAT_COLS) + ("views",)
_DB_ALTERS = tuple(_DB_ALTERS) + ("ALTER TABLE pg_stats ADD COLUMN views INTEGER DEFAULT 0",)
_CODES[_VW_EM] = "views"

VIEWS_LIMIT_RE = re.compile(r"rate\s*limits?\s*exceeded|wait\s+\d[\d,\.]*\s*(?:seconds?|secs?)\s+before", re.I)
_VW_WAIT_RE = re.compile(r"wait\s+(\d[\d,\.]*)\s*(?:seconds?|secs?|s)\b", re.I)
_VW_EARN_RE = re.compile(r"earned\s+(\d[\d,\.]*)\s*grams?", re.I)
_VW_POST_RE = re.compile(r"post\s*#\s*(\d[\d,]*)", re.I)
_VW_BOT_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})")


# --- bug fix: "1,135 seconds" used to be read as 135 seconds ------------------------------------------
_orig_parse_wait_seconds = parse_wait_seconds


def parse_wait_seconds(text: str) -> int:
    return _orig_parse_wait_seconds(re.sub(r"(?<=\d),(?=\d{3}\b)", "", text or ""))


# --- per-account views state -----------------------------------------------------------------------------
def _vw(uid: int) -> dict:
    f = _farm(uid)
    vw = f.get("vw")
    if vw is None:
        vw = f["vw"] = {"on": False, "bot": None, "stage": "idle", "gen": 0, "cur": None,
                        "last_evt": 0.0, "main_until": 0.0, "mirror_until": 0.0, "hold_until": 0.0,
                        "retry_at": 0.0, "mirror": "", "ids": {}, "started": False, "stalls": 0,
                        "n": 0, "gram": 0.0}
    return vw


def _vw_name(uid: int, which: str) -> str:
    if which == "mirror":
        return (_vw(uid).get("mirror") or str(CONFIG["pg_mirror"])).strip().lstrip("@")
    return pg_bot_name()


def _vw_free(uid: int, which: str, now: Optional[float] = None) -> bool:
    now = now or time.time()
    if which == "mirror" and not get_cfg(uid)["views_mirror"]:
        return False
    return now >= _vw(uid).get(f"{which}_until", 0.0)


def _vw_pick(uid: int) -> Optional[str]:
    cur = _vw(uid).get("bot")
    if cur and _vw_free(uid, cur):                 # sticky: stay on a bot until it rate-limits us
        return cur
    for w in ("main", "mirror"):
        if _vw_free(uid, w):
            return w
    return None


def _vw_gap(uid: int) -> float:
    base = max(VIEWS_HARD_MIN_GAP, float(get_cfg(uid).get("views_gap") or 6.0))
    g = base * random.uniform(0.75, 1.45) * night_mult(uid)
    if random.random() < 0.06:
        g += random.uniform(6, 20)                 # occasional short "got distracted" pause
    return max(VIEWS_HARD_MIN_GAP, g)


def _vw_button(msg) -> Tuple[Optional[Tuple[int, int]], str]:
    """Pick the next button to press: Next Post > View Post (first) > the Views category. Never 'Report'."""
    want = _flat(str(CONFIG["pg_views_category"])).lower()
    found: Dict[str, Tuple[int, int]] = {}
    for i, row in enumerate(_btn_rows(msg)):
        for j, b in enumerate(row):
            if getattr(b, "data", None) is None:
                continue
            t = _flat(getattr(b, "text", "") or "").lower()
            if "next post" in t:
                found.setdefault("next", (i, j))
            elif "view post" in t:
                found.setdefault("post", (i, j))
            elif want and want in t:
                found.setdefault("cat", (i, j))
    for k in ("next", "post", "cat"):
        if k in found:
            return found[k], k
    return None, ""


def _vw_kick(uid: int, client: TelegramClient, delay: float) -> None:
    """Schedule ONE click (a newer kick cancels the older one)."""
    vw = _vw(uid)
    vw["gen"] += 1
    spawn(_vw_act(uid, client, vw["gen"], delay), f"vact-{uid}")


async def _vw_act(uid: int, client: TelegramClient, gen: int, delay: float) -> None:
    await asyncio.sleep(delay)
    vw = _vw(uid)
    if gen != vw["gen"] or not vw["on"] or is_paused(uid):
        return
    try:
        async with _lock(uid, "views"):
            if gen != vw["gen"] or not vw["on"] or is_paused(uid):
                return
            msg = vw.get("cur")
            if msg is None:
                return
            pos, _kind = _vw_button(msg)
            if pos is None:
                return
            vw["cur"] = None
            vw["last_evt"] = time.time()
            try:
                async with gsem():
                    ans = await msg.click(pos[0], pos[1])
            except BotResponseTimeoutError:
                return
            alert = (getattr(ans, "message", "") or "") if ans else ""
            if alert:
                if VIEWS_LIMIT_RE.search(_flat(alert)):
                    await views_limited(uid, vw.get("bot") or "main", alert)
                else:
                    stat_note(uid, f"{_VW_EM} {_flat(alert)[:70]}")
    except DEAD_ERRORS:
        await handle_dead(uid)
    except Exception as e:
        if await absorb(uid, e, "views"):
            vw["hold_until"] = max(vw.get("hold_until", 0.0), _farm(uid)["flood_until"])
            vw["stage"] = "start"
        else:
            stat_add(uid, errors=1)
            evt(uid, "VIEWS", f"click {type(e).__name__}: {e}", logging.WARNING)


async def views_limited(uid: int, which: str, text: str) -> None:
    """The bot said "Telegram rate limits exceeded ... wait N seconds" -> park that bot, use the other one."""
    vw, flat, now = _vw(uid), _flat(text), time.time()
    m = _VW_WAIT_RE.search(flat)
    secs = int(re.sub(r"\D", "", m.group(1)) or 0) if m else 0
    if secs <= 0:
        secs = parse_wait_seconds(flat) or 900
    secs = max(5, min(secs, MAX_BOT_WAIT))
    vw[f"{which}_until"] = now + secs + 3
    mirror_note = ""
    if which == "main":
        me = pg_bot_name().lower()
        for nm in _VW_BOT_RE.findall(text or ""):          # the message names the mirror bot
            if nm.lower() != me and nm.lower().endswith("bot"):
                if nm.lower() != (vw.get("mirror") or "").lower():
                    vw["mirror"] = nm
                    vw["ids"].pop("mirror", None)
                break
        if get_cfg(uid)["views_mirror"]:
            mirror_note = _vw_name(uid, "mirror")
    vw.update(bot=None, stage="start", cur=None, started=False)
    vw["gen"] += 1
    stat_note(uid, f"{_VW_EM} @{_vw_name(uid, which)} rate limit {secs}s")
    evt(uid, "VIEWLIM", f"{which} {secs}s", logging.WARNING)
    if get_cfg(uid)["notify"]:
        nm = h(_vw_name(uid, which))
        await warn_once(uid, "vlimit-" + which, L(
            uid,
            f"🤖 <b>@{nm} limited Views</b> for <b>{_fmt_dur(secs)}</b>."
            + (f"\nSwitching to the mirror bot <b>@{h(mirror_note)}</b>." if mirror_note else ""),
            f"🤖 <b>@{nm} ویو رو محدود کرد</b> برای <b>{_fmt_dur(secs)}</b>."
            + (f"\nمی‌رم سراغ ربات آینه <b>@{h(mirror_note)}</b>." if mirror_note else "")), 900)
    c = ACCOUNTS.get(uid)
    if c is not None:
        spawn(_views_tick(uid, c), f"vtick-{uid}")


async def _vw_captcha(uid: int, msg, which: str) -> None:
    """Human check on the MIRROR bot (the main bot's one is handled by handle_captcha)."""
    if _seen_before(uid, "cap", msg.chat_id or 0, msg.id):
        return
    _farm(uid)["captcha"] = (msg.chat_id, msg.id)
    stat_add(uid, captchas=1)
    stat_note(uid, "⏸ human check (mirror)")
    was = is_paused(uid)
    set_cfg(uid, paused=True)
    evt(uid, "HUMAN", f"verification requested by @{_vw_name(uid, which)} - paused", logging.WARNING)
    if was:
        return
    await notify_btn(
        uid,
        L(uid,
          f"⏸ <b>@{h(_vw_name(uid, which))} asks you to verify that you are human.</b>\n\n"
          "Everything is paused. Open that chat in your normal Telegram app, solve the puzzle yourself, "
          "then press <b>Resume</b>.",
          f"⏸ <b>@{h(_vw_name(uid, which))} می‌خواد ثابت کنی انسان هستی.</b>\n\n"
          "همه‌چیز متوقف شد. اون چت رو توی تلگرام خودت باز کن، پازل رو خودت حل کن و بعد <b>ادامه</b> رو بزن."),
        [[cb(L(uid, "▶️ Resume", "▶️ ادامه"), b"pg:resume", "success")]])


async def views_handle(uid: int, client: TelegramClient, msg, which: str, edited: bool) -> None:
    vw = _vw(uid)
    text = msg.raw_text or ""
    flat = _flat(text)
    vw["last_evt"] = time.time()

    if VIEWS_LIMIT_RE.search(flat):
        if not _seen_before(uid, "vlim", msg.chat_id or 0, msg.id):
            await views_limited(uid, which, text)
        return
    if is_captcha_message(text):
        vw["gen"] += 1
        vw["cur"] = None
        if which == "mirror":
            await _vw_captcha(uid, msg, which)
        return                                     # main bot: handle_captcha already does it

    m = _VW_EARN_RE.search(flat)
    if m and "view" in flat.lower():               # "You have earned 300 grams for viewing the post #..."
        mp = _VW_POST_RE.search(flat)
        post = int(re.sub(r"\D", "", mp.group(1)) or 0) if mp else 0
        if not _seen_before(uid, "vdone", msg.chat_id or 0, post or msg.id):
            gain = _num(m.group(1))
            stat_add(uid, views=1, gram=gain)
            f = _farm(uid)
            f["sess_gram"] = f.get("sess_gram", 0.0) + gain
            f["sess_done"] = f.get("sess_done", 0) + 1
            vw["n"] += 1
            vw["gram"] += gain
            vw["stalls"] = 0
            bal = parse_balance(text)
            if bal:
                stat_set_balance(uid, bal)
            stat_note(uid, f"{_VW_EM} +{gain:g} GRAM")
            evt(uid, "VIEW", f"+{gain:g} GRAM balance {bal or '?'} via {which}")

    pos, kind = _vw_button(msg)
    if pos is None:
        return
    vw["cur"] = msg
    vw["stage"] = "run"
    _vw_kick(uid, client, _vw_gap(uid) if kind == "next" else random.uniform(0.8, 2.2))


async def _vw_start(uid: int, client: TelegramClient) -> None:
    """Open the Earnings menu on the current views bot (mirror: /start first)."""
    vw, f = _vw(uid), _farm(uid)
    which = vw["bot"]
    name = _vw_name(uid, which)
    vw.update(stage="run", cur=None, last_evt=time.time())
    vw["gen"] += 1
    try:
        if which == "main":
            await ensure_targets(uid, client)
            if not (_TGT.get(uid) or {}).get("bot_id"):
                vw.update(stage="start", retry_at=time.time() + 15)
                return
        elif not vw["ids"].get("mirror"):
            async with gsem():
                ent = await client.get_entity(name)
            vw["ids"]["mirror"] = ent.id
        if which == "mirror" and (not vw["started"] or vw["stalls"] >= 2):
            async with gsem():
                await client.send_message(name, "/start")
            vw["started"] = True
            await asyncio.sleep(random.uniform(2.0, 4.0))
        async with gsem():
            await client.send_message(name, str(CONFIG["pg_earn_text"]))
        vw["last_evt"] = time.time()
        evt(uid, "VIEWS", f"sent '{CONFIG['pg_earn_text']}' to @{name}")
    except DEAD_ERRORS:
        raise
    except Exception as e:
        vw["stage"] = "start"
        if await absorb(uid, e, "views"):
            vw["hold_until"] = max(vw.get("hold_until", 0.0), f["flood_until"])
        elif which == "mirror" and isinstance(e, (UsernameNotOccupiedError, UsernameInvalidError)):
            vw["mirror_until"] = time.time() + 3600
            vw["bot"] = None
            stat_note(uid, f"{_VW_EM} mirror bot @{name} not found")
        else:
            stat_add(uid, errors=1)
            vw["retry_at"] = time.time() + 20
            evt(uid, "VIEWS", f"start {type(e).__name__}: {e}", logging.WARNING)


async def _views_tick(uid: int, client: TelegramClient) -> None:
    lk = _lock(uid, "vtick")
    if lk.locked():
        return
    async with lk:
        await _views_tick_inner(uid, client)


async def _views_tick_inner(uid: int, client: TelegramClient) -> None:
    cfg, f, vw, now = get_cfg(uid), _farm(uid), _vw(uid), time.time()
    left = f["flood_until"] - now
    calm = sched_wait(uid) <= 0 and now >= f.get("rest_until", 0.0)       # respect active hours / breaks
    ok = bool(cfg["views_on"] and cfg["auto_join"] and cfg["auto_fetch"] and not cfg["paused"]
              and calm and now >= vw["hold_until"] and uid in ACCOUNTS)
    want = ok and ((left >= VIEWS_MIN_FLOOD) if not vw["on"] else (left > 0))

    if want and not vw["on"]:                      # ---- enter views mode
        vw.update(on=True, bot=None, stage="start", cur=None, n=0, gram=0.0, stalls=0, started=False,
                  retry_at=0.0, last_evt=now)
        vw["gen"] += 1
        stat_note(uid, f"{_VW_EM} join flood -> Views mode")
        evt(uid, "VIEWS", f"on (flood {int(left)}s)")
        if cfg["notify"]:
            main = h(pg_bot_name())
            await warn_once(uid, "views_on", L(
                uid,
                f"{_VW_EM} <b>Join flood → Views mode</b>\n<blockquote>⏳ Telegram blocked joining for <b>{_fmt_dur(left)}</b>.\n"
                f"Instead of idling I earn from <b>Views</b> on @{main}"
                + ("; if it rate-limits me I move to the mirror bot" if cfg["views_mirror"] else "")
                + ".\nWhen the flood ends I return to channel tasks by myself.</blockquote>",
                f"{_VW_EM} <b>فلود عضویت → حالت ویو</b>\n<blockquote>⏳ تلگرام عضویت رو برای <b>{_fmt_dur(left)}</b> بلاک کرده.\n"
                f"به‌جای بیکار ماندن از بخش <b>ویو</b> روی @{main} درآمد می‌گیرم"
                + ("؛ اگه محدودم کنه می‌رم سراغ ربات آینه" if cfg["views_mirror"] else "")
                + ".\nبعد از پایان فلود خودم برمی‌گردم به تسک‌های کانال.</blockquote>"), 600)

    if not want:                                   # ---- leave views mode
        if vw["on"]:
            vw.update(on=False, bot=None, stage="idle", cur=None)
            vw["gen"] += 1
            evt(uid, "VIEWS", f"off ({vw['n']} posts, +{vw['gram']:g} GRAM)")
            if left <= 0 and vw["n"] > 0 and cfg["notify"]:
                await notify(uid, L(
                    uid,
                    f"✅ <b>Flood over</b> — back to channel tasks.\n{_VW_EM} This round: <b>{vw['n']}</b> posts · <b>+{vw['gram']:,.0f}</b> GRAM",
                    f"✅ <b>فلود تموم شد</b> — برگشتم به تسک‌های کانال.\n{_VW_EM} این دور: <b>{vw['n']}</b> پست · <b>+{vw['gram']:,.0f}</b> GRAM"))
        return

    if now < vw.get("retry_at", 0.0):
        return
    if vw["bot"] is None or not _vw_free(uid, vw["bot"], now):
        pick = _vw_pick(uid)
        if pick is None:                           # both bots rate-limited: wait
            vw["bot"] = None
            return
        if pick != vw["bot"]:
            vw.update(bot=pick, stage="start", started=False, stalls=0, cur=None)
            vw["gen"] += 1
            stat_note(uid, f"{_VW_EM} using @{_vw_name(uid, pick)}")
    if vw["stage"] == "start":
        await _vw_start(uid, client)
    elif now - vw["last_evt"] > min(300.0, 40.0 + 30.0 * vw["stalls"]):     # the bot went quiet
        vw["stalls"] += 1
        vw["gen"] += 1
        vw["cur"] = None
        if vw["stalls"] >= 6:                      # this bot does not answer: park it, try the other one
            vw[f"{vw['bot']}_until"] = now + 600
            vw.update(bot=None, stalls=0)
            stat_note(uid, f"{_VW_EM} bot silent - parked 10 min")
        else:
            vw["stage"] = "start"
            stat_note(uid, f"{_VW_EM} no reply - restarting")


# --- wiring: wrappers around the existing functions ------------------------------------------------------
_orig_farm_tick = farm_tick


async def farm_tick(uid: int, client: TelegramClient) -> None:
    try:
        await _views_tick(uid, client)
    except DEAD_ERRORS:
        raise
    except Exception as e:
        if not await absorb(uid, e, "views"):
            log.warning(f"views tick u{uid}: {type(e).__name__}: {e}")
    await _orig_farm_tick(uid, client)             # during a flood this only does its housekeeping


_orig_apply_bot_limit = _apply_bot_limit


async def _apply_bot_limit(uid: int, text: str) -> None:
    if VIEWS_LIMIT_RE.search(_flat(text)) and _vw(uid).get("on"):
        return                                     # views mode handles this limit itself (views_limited)
    await _orig_apply_bot_limit(uid, text)


_VW_ATTACHED: Dict[int, Any] = {}
_orig_attach_user_handlers = attach_user_handlers


def attach_user_handlers(uid: int, client: TelegramClient) -> None:
    _orig_attach_user_handlers(uid, client)
    if _VW_ATTACHED.get(uid) is client:
        return
    _VW_ATTACHED[uid] = client

    async def _vw_on(event, edited: bool) -> None:
        try:
            if not event.is_private:
                return
            vw = _vw(uid)
            if not vw["on"] or not vw["bot"]:
                return
            sid = event.sender_id
            if vw["bot"] == "main":
                ok = sid == (_TGT.get(uid) or {}).get("bot_id")
            else:
                ok = sid == vw["ids"].get("mirror")
            if ok:
                await views_handle(uid, client, event.message, vw["bot"], edited)
        except DEAD_ERRORS:
            await handle_dead(uid)
        except Exception as e:
            if not await absorb(uid, e, "views"):
                log.warning(f"views handler u{uid}: {type(e).__name__}: {e}")

    async def _vw_new(event):
        await _vw_on(event, False)

    async def _vw_edit(event):
        await _vw_on(event, True)

    client.add_event_handler(_vw_new, events.NewMessage(incoming=True))
    client.add_event_handler(_vw_edit, events.MessageEdited(incoming=True))


_orig_cleanup_user = cleanup_user


def cleanup_user(uid: int) -> None:
    _orig_cleanup_user(uid)
    _VW_ATTACHED.pop(uid, None)


_orig_status2 = status2


def status2(uid: int) -> Tuple[str, str]:
    em, st = _orig_status2(uid)
    vw = (_FARM.get(uid) or {}).get("vw")
    if vw and vw.get("on") and uid in ACCOUNTS:
        which = vw.get("bot")
        if which:
            nm = _vw_name(uid, which)
            return _VW_EM, L(uid, f"Views mode · @{nm} · {vw['n']} viewed (+{vw['gram']:,.0f})",
                             f"حالت ویو · @{nm} · {vw['n']} ویو (+{vw['gram']:,.0f})")
        return _VW_EM, L(uid, "Views mode · waiting for the rate limit to pass", "حالت ویو · در انتظار پایان محدودیت")
    return em, st


_orig_compact_text = compact_text


def compact_text(uid: int) -> str:
    t = _orig_compact_text(uid)
    try:
        vw = (_FARM.get(uid) or {}).get("vw") or {}
        n = int(stat_get(uid).get("views", 0) or 0)
        if vw.get("on") or n:
            line = L(uid, f"{_VW_EM} Views <b>{n}</b>" + (f" · active now (+{vw.get('gram', 0.0):,.0f})" if vw.get("on") else ""),
                     f"{_VW_EM} ویو <b>{n}</b>" + (f" · الان فعاله (+{vw.get('gram', 0.0):,.0f})" if vw.get("on") else ""))
            t = t.replace("</blockquote>\n", "</blockquote>\n" + line + "\n", 1)
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    return t


_orig_acct_state = acct_state


def acct_state(uid: int) -> dict:
    r = _orig_acct_state(uid)
    try:
        now = time.time()
        vw = (_FARM.get(uid) or {}).get("vw") or {}
        r["stats"]["views"] = int(stat_get(uid).get("views", 0) or 0)
        which = vw.get("bot") or ""
        r["views"] = {"on": bool(vw.get("on")), "which": which, "bot": _vw_name(uid, which) if which else "",
                      "round_n": int(vw.get("n", 0)), "round_gram": round(float(vw.get("gram", 0.0)), 1),
                      "main_left": max(0, round(vw.get("main_until", 0.0) - now)),
                      "mirror_left": max(0, round(vw.get("mirror_until", 0.0) - now))}
    except Exception as e:
        log.warning(f"views state: {type(e).__name__}: {e}")
    return r


# --- web dashboard: new section + overview bits (patched into the HTML template at start-up) -------------
def _tpl_patch(old: str, new: str) -> None:
    global _PANEL_TPL
    if _PANEL_TPL.count(old) == 1:
        _PANEL_TPL = _PANEL_TPL.replace(old, new)
    else:
        # [patch-hardening:strict-ui]
        if _env_bool("STRICT_UI_PATCH", False):
            raise RuntimeError(f"dashboard template anchor not found: {old[:60]!r}")
        log.warning(f"views patch: dashboard anchor not found ({old[:40]!r}) - that part of the UI is skipped")


_tpl_patch(".dot.rest,.dot.sleep{background:var(--vio)}", ".dot.rest,.dot.sleep,.dot.views{background:var(--vio)}")
_tpl_patch("offline:['Offline','آفلاین']", "offline:['Offline','آفلاین'],views:['Views mode','حالت ویو']")
_tpl_patch("{id:'cleanup',", r"""{id:'views',ic:'👁',en:'Views mode (when joining is flooded)',fa:'حالت ویو (وقتی عضویت فلود می‌خوره)',f:[
 {k:'views_on',t:'sw',en:'Auto-switch to Views on a join flood',fa:'سوییچ خودکار به ویو هنگام فلود عضویت',he:'While Telegram blocks joining, the account earns GRAM by viewing posts instead of idling. When the flood ends it returns to channels by itself.',hf:'تا وقتی تلگرام عضویت رو بلاک کرده، اکانت به‌جای بیکار ماندن با دیدن پست‌ها GRAM می‌گیره و بعد از پایان فلود خودکار برمی‌گرده به کانال‌ها.'},
 {k:'views_mirror',t:'sw',dep:'views_on',en:'Use the mirror bot when the main bot rate-limits',fa:'استفاده از ربات آینه وقتی ربات اصلی محدود می‌کنه',he:'The bot names its mirror (default gram_prbot) in the rate-limit message. The account goes there, runs /start and continues.',hf:'ربات توی پیام محدودیت اسم ربات آینه رو میاره (پیش‌فرض gram_prbot). اکانت می‌ره اونجا، /start می‌زنه و ادامه می‌ده.'},
 {k:'views_gap',t:'rng',min:2,max:60,step:1,unit:'s',dep:'views_on',en:'Seconds between posts',fa:'ثانیه بین پست‌ها',he:'Pause before pressing Next Post (random +-40%). Never below 2 s; the night pace applies too.',hf:'مکث قبل از زدن Next Post (تصادفی حدود ۴۰٪). هیچ‌وقت کمتر از ۲ ثانیه نمی‌شه و سرعت شب هم اعمال می‌شه.'}]},
{id:'cleanup',""")
_tpl_patch("num(k.errors)+'</b></div>",
           "num(k.errors)+'</b></div><div class=\"tile\"><span>👁 '+tr('Views','ویو')+'</span><b>'+num(k.views)+'</b></div>")
_tpl_patch("const rk=s.risk,rc=",
           "if(s.views&&s.views.on)ban+='<div class=\"banner\">👁 '+tr('Join flood: earning from Views on','فلود عضویت: درآمد از ویو روی')+' <b>@'+esc(s.views.bot||'...')+'</b> · +'+num(s.views.round_gram)+' GRAM ('+s.views.round_n+' '+tr('posts','پست')+')</div>';const rk=s.risk,rc=")


# ===========================================================================
# PATCH 3.5.1  -  join caps 2000/h and 20000/day for everyone
# ===========================================================================
PATCH_CAPS_APPLIED = True
CAP_HOUR_DEFAULT = 2000
CAP_DAY_DEFAULT = 20000

EXTRA_COLS["cap_hour"] = ("INTEGER", CAP_HOUR_DEFAULT)
EXTRA_COLS["cap_day"] = ("INTEGER", CAP_DAY_DEFAULT)
for _p in PRESETS.values():
    _p["cap_hour"] = CAP_HOUR_DEFAULT
    _p["cap_day"] = CAP_DAY_DEFAULT

_orig_migrate_caps = _migrate


def _migrate(c: sqlite3.Connection) -> None:
    _orig_migrate_caps(c)
    ver = int(c.execute("PRAGMA user_version").fetchone()[0])
    if ver < 3:
        try:
            c.execute("UPDATE pg_cfg SET cap_hour=?, cap_day=?", (CAP_HOUR_DEFAULT, CAP_DAY_DEFAULT))
        except sqlite3.Error as e:
            log.warning(f"caps migration: {e}")
        c.execute("PRAGMA user_version=3")


# ===========================================================================
# PATCH 3.5.2  -  forced join caps: 2000 / hour, 20000 / day
# ===========================================================================
PATCH_FORCE_APPLIED = True
FORCE_CAPS = True              # False = the dashboard sliders work again (rows are still created with 2000/20000)
CAP_HOUR_FORCED = 2000
CAP_DAY_FORCED = 20000

# --- layer 3: every reader of the settings sees the forced values ------------------------------------
_orig_get_cfg_force = get_cfg


def get_cfg(uid: int) -> dict:
    c = _orig_get_cfg_force(uid)
    if FORCE_CAPS:
        c["cap_hour"] = CAP_HOUR_FORCED
        c["cap_day"] = CAP_DAY_FORCED
    return c


# --- layer 1: new accounts are created with the forced caps ------------------------------------------
_orig_set_cfg_force = set_cfg


def set_cfg(uid: int, **kw: Any) -> None:
    try:
        db_exec("INSERT OR IGNORE INTO pg_cfg (uid, profile, cap_hour, cap_day) VALUES (?,?,?,?)",
                (uid, DEFAULT_PROFILE, CAP_HOUR_FORCED, CAP_DAY_FORCED))
    except Exception as e:
        log.warning(f"forced caps (new row): {type(e).__name__}: {e}")
    if FORCE_CAPS:
        kw.pop("cap_hour", None)
        kw.pop("cap_day", None)
    _orig_set_cfg_force(uid, **kw)


# --- layer 2: all existing accounts are rewritten on every start -------------------------------------
_orig_init_db_force = init_db


def init_db() -> None:
    first = _conn is None
    _orig_init_db_force()
    if first and _conn is not None and FORCE_CAPS:
        try:
            n = _conn.execute("UPDATE pg_cfg SET cap_hour=?, cap_day=?",
                              (CAP_HOUR_FORCED, CAP_DAY_FORCED)).rowcount
            _CFG_CACHE.clear()
            log.info(f"join caps forced to {CAP_HOUR_FORCED}/h and {CAP_DAY_FORCED}/day for {n} stored account(s)")
        except Exception as e:
            log.warning(f"forced caps (existing rows): {type(e).__name__}: {e}")



# ===== PATCH 3.6 - forced 2000/h caps (no learned caps) + owner-managed sub-admins =====
PATCH_3_6_APPLIED = True
VERSION = "3.6"

# ---------------------------------------------------------------------------------------------
# 1) a flood wait can never lower the 2000/h and 20000/day caps again
# ---------------------------------------------------------------------------------------------
_LEARNED = ("hour_cap", "cap_until", "day_cap", "day_until")


def _bad(uid: int, wait: int = 0) -> None:
    """Disabled: flood waits used to teach a lower hourly/daily cap (that is where 45 came from)."""
    return None


_orig_state_load_36 = _state_load


def _state_load(uid: int, f: dict) -> None:
    _orig_state_load_36(uid, f)
    for k in _LEARNED:
        f.pop(k, None)


_orig_eff_limits_36 = eff_limits


def eff_limits(uid: int) -> Tuple[int, int, float]:
    f = _farm(uid)
    for k in _LEARNED:                     # also cleans accounts that are already running in memory
        f.pop(k, None)
    hl, dl, gi = _orig_eff_limits_36(uid)
    if FORCE_CAPS:
        hl, dl = CAP_HOUR_FORCED, CAP_DAY_FORCED
    return hl, dl, gi


# ---------------------------------------------------------------------------------------------
# 2) sub-admins: storage
# ---------------------------------------------------------------------------------------------
PERM_KEYS = ("view_accounts", "view_money", "open_accounts", "view_logs", "control",
             "leave_clean", "reset", "manage_access", "ban")
LIMIT_KEYS = {"adv_mode", "adv_gap", "adv_batch", "adv_conc", "cap_hour", "cap_day", "flood_stop"}
FULL_PERMS: Dict[str, int] = dict({k: 1 for k in PERM_KEYS}, settings=2)

SUB_SCHEMA = ("CREATE TABLE IF NOT EXISTS pg_subadmin (uid INTEGER PRIMARY KEY, perms TEXT NOT NULL DEFAULT '{}', "
              "scope TEXT NOT NULL DEFAULT 'all', note TEXT DEFAULT '', active INTEGER DEFAULT 1, "
              "epoch INTEGER DEFAULT 0, created_at REAL DEFAULT 0)")
_SUBC: Dict[str, Any] = {"rows": None}

_orig_init_db_36 = init_db


def init_db() -> None:
    first = _conn is None
    _orig_init_db_36()
    if first and _conn is not None:
        try:
            _conn.execute(SUB_SCHEMA)
            n = _conn.execute("UPDATE pg_state SET hour_cap=0, cap_until=0, day_cap=0, day_until=0 "
                              "WHERE hour_cap!=0 OR cap_until!=0 OR day_cap!=0 OR day_until!=0").rowcount
            if FORCE_CAPS:
                _conn.execute("UPDATE pg_cfg SET cap_hour=?, cap_day=?", (CAP_HOUR_FORCED, CAP_DAY_FORCED))
            _CFG_CACHE.clear()
            if n:
                log.info(f"learned flood caps wiped for {n} account(s); caps are {CAP_HOUR_FORCED}/h and {CAP_DAY_FORCED}/day")
        except Exception as e:
            log.warning(f"patch 3.6 init: {type(e).__name__}: {e}")


def _norm_perms(raw: Any) -> Dict[str, int]:
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, int] = {}
    for k in PERM_KEYS:
        v = raw.get(k)
        out[k] = 1 if (v is True or str(v).strip().lower() in ("1", "true", "on", "yes")) else 0
    try:
        out["settings"] = max(0, min(2, int(float(raw.get("settings", 0)))))
    except Exception:
        out["settings"] = 0
    return out


def _norm_scope(raw: Any):
    """'all' or a sorted list of user ids."""
    if raw in (None, "", "all"):
        return "all"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = [x for x in re.split(r"[,\s]+", raw) if x]
    if raw == "all":
        return "all"
    ids = []
    for x in (raw if isinstance(raw, (list, tuple)) else []):
        try:
            ids.append(int(x))
        except Exception as _swallow_e:
            _swallow(_swallow_e)
    return sorted(set(i for i in ids if i > 0))


def _sub_rows() -> Dict[int, dict]:
    rows = _SUBC["rows"]
    if rows is not None:
        return rows
    rows = {}
    try:
        for r in db_exec("SELECT * FROM pg_subadmin").fetchall():
            try:
                perms = json.loads(r["perms"] or "{}")
            except Exception:
                perms = {}
            rows[int(r["uid"])] = {"perms": _norm_perms(perms), "scope": _norm_scope(r["scope"]),
                                   "note": r["note"] or "", "active": bool(r["active"]),
                                   "epoch": int(r["epoch"] or 0), "created_at": float(r["created_at"] or 0)}
        _SUBC["rows"] = rows
    except Exception as e:
        log.warning(f"sub-admin table: {type(e).__name__}: {e}")
    return rows


def _sub_active(uid: int) -> bool:
    r = _sub_rows().get(uid)
    return bool(r and r["active"])


def sub_save(uid: int, perms: dict, scope: Any, note: str) -> None:
    db_exec("INSERT OR IGNORE INTO pg_subadmin (uid,perms,scope,note,active,epoch,created_at) VALUES (?,?,?,?,1,0,?)",
            (uid, "{}", "all", "", time.time()))
    db_exec("UPDATE pg_subadmin SET perms=?, scope=?, note=?, active=1 WHERE uid=?",
            (json.dumps(_norm_perms(perms)), json.dumps(_norm_scope(scope)), note[:80], uid))
    _SUBC["rows"] = None


# a limited admin (a row exists) is no longer a full admin: no /ban, /allow, /open, /closed in Telegram
_orig_is_admin_36 = is_admin


def is_admin(uid: int) -> bool:
    if not _orig_is_admin_36(uid):
        return False
    if uid == owner_id():
        return True
    return not _sub_active(uid)


_orig_allowed_36 = allowed


def allowed(uid: int) -> bool:
    if _sub_active(uid) and not is_banned(uid):
        return True
    return _orig_allowed_36(uid)


def staff_perms(uid: int):
    """(perms, scope) of a non-owner admin, or None. ADMIN_IDS entries without a row keep full access."""
    r = _sub_rows().get(uid)
    if r is not None and r["active"]:
        return r["perms"], r["scope"]
    if _orig_is_admin_36(uid) and uid != owner_id() and (r is None or not r["active"]):
        return dict(FULL_PERMS), "all"
    return None


def _staff_sig(uid: int, epoch: int) -> str:
    mac = hmac.new(_web_secret(), f"a:{uid}:{epoch}".encode(), hashlib.sha256).digest()[:18]
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def staff_epoch(uid: int) -> int:
    r = _sub_rows().get(uid)
    return int(r["epoch"]) if r else 0


def sub_token(uid: int) -> str:
    e = staff_epoch(uid)
    return f"a.{uid}.{e}.{_staff_sig(uid, e)}"


def uid_of_staff_token(tok: str) -> Optional[int]:
    if not tok or len(tok) > 120 or not tok.startswith("a."):
        return None
    parts = tok.split(".")
    if len(parts) != 4 or not parts[1].isdigit() or not parts[2].isdigit():
        return None
    uid, e = int(parts[1]), int(parts[2])
    try:
        ok = hmac.compare_digest(_staff_sig(uid, e).encode(), parts[3].encode())
    except Exception:
        return None
    if not ok or e != staff_epoch(uid) or is_banned(uid) or staff_perms(uid) is None:
        return None
    return uid


_orig_auth_36 = WebPanel._auth


def _auth_36(self, headers, qs):
    r = _orig_auth_36(self, headers, qs)
    if r is not None:
        return r
    tok = (headers.get("x-token") or ((qs.get("t", [""])[0] or "") if _env_bool("ALLOW_QUERY_TOKEN", False) else "")).strip()  # [patch-hardening:header-token]
    u = uid_of_staff_token(tok)
    return ("sub", u) if u else None


WebPanel._auth = _auth_36

# non-owner admins get their own limited dashboard link; only the owner gets the master link
_orig_dash_url_36 = dash_url


def dash_url(uid: int, admin: bool = False) -> str:
    if admin and uid != owner_id():
        base = dash_base()
        if not base or RT.web is None or staff_perms(uid) is None:
            return ""
        return f"{base}/?t={sub_token(uid)}"
    return _orig_dash_url_36(uid, admin)


_orig_render_36 = render


def render(uid: int, view: str):
    text, kb = _orig_render_36(uid, view)
    try:
        if view != "admin" and _sub_active(uid):
            url = dash_url(uid, True)
            if url:
                rows = list(kb)
                row = [ub("🛠 Admin dashboard", url, "primary")]
                if uid in ACCOUNTS and rows:
                    rows.insert(len(rows) - 1, row)
                else:
                    rows.append(row)
                kb = rows
    except Exception as e:
        log.warning(f"render sub-admin button: {type(e).__name__}: {e}")
    return text, kb


# ---------------------------------------------------------------------------------------------
# 3) sub-admin API (every request is checked on the server: permission + account scope)
# ---------------------------------------------------------------------------------------------
_SUB_ACTIONS = {
    "control": ("start", "stop", "pause", "resume", "fetch", "boost"),
    "leave_clean": ("leave_old", "leave_all", "leave_row", "clean_scan", "clean_start", "clean_cancel"),
    "reset": ("reset_stats", "reset_warmup", "reset_token", "logout"),
    "manage_access": ("toggle_access", "allow", "disallow"),
    "ban": ("ban", "unban"),
}
_TARGET_ACTS = ("ban", "unban", "allow", "disallow")


def _q_uid(qs: dict, data: dict) -> int:
    try:
        return int(((qs.get("uid") or [""])[0]) or data.get("uid") or 0)
    except Exception:
        return 0


def _in_scope(scope: Any, uid: int) -> bool:
    return scope == "all" or uid in scope


def _is_staff_uid(uid: int) -> bool:
    return uid == owner_id() or _orig_is_admin_36(uid) or _sub_active(uid)


def _sub_target_err(auid: int, scope: Any, tgt: int) -> str:
    if not tgt:
        return "no account selected"
    if tgt == auid:
        return ""
    if _is_staff_uid(tgt):
        return "this account belongs to an admin"
    if not _in_scope(scope, tgt):
        return "outside your allowed accounts"
    return ""


def _sub_admin_view(auid: int, perms: dict, scope: Any) -> dict:
    base = admin_state()
    show = bool(perms.get("view_accounts"))
    money = bool(perms.get("view_money"))
    accs = []
    if show:
        for a in base.get("accounts", []):
            if _is_staff_uid(a["uid"]) or not _in_scope(scope, a["uid"]):
                continue
            a = dict(a)
            if not money:
                a["gram"] = 0.0
            accs.append(a)
    zero = {"gram": 0.0, "joined": 0, "completed": 0, "errors": 0}
    return {"ok": True, "accounts": accs, "open_access": base.get("open_access"),
            "users_total": base.get("users_total") if scope == "all" else len(accs),
            "max_users": base.get("max_users"), "online": len([a for a in accs if a["online"]]),
            "now": base.get("now"), "totals": base.get("totals") if (money and scope == "all") else zero,
            "allowed": base.get("allowed", []) if perms.get("manage_access") else []}


async def _sub_api(auid: int, path: str, qs: dict, data: dict) -> dict:
    st = staff_perms(auid)
    if st is None:
        return _jerr("unauthorized")
    perms, scope = st
    if path == "/api/me":
        return {"ok": True, "role": "admin", "owner": False, "uid": 0, "version": VERSION, "perms": perms,
                "hard": {"min_gap": HARD_MIN_GAP, "max_batch": HARD_MAX_BATCH, "max_conc": HARD_MAX_CONC}}
    if path.startswith("/api/owner/"):
        return _jerr("owner only")
    if path == "/api/admin":
        return _sub_admin_view(auid, perms, scope)
    qs, data = dict(qs), dict(data)
    uid = _q_uid(qs, data)
    act = str(data.get("action", "")).strip() if path == "/api/action" else ""
    tgt = uid
    if path == "/api/action":
        need = next((p for p, names in _SUB_ACTIONS.items() if act in names), None)
        if act == "preset":
            if perms.get("settings", 0) < 2:
                return _jerr("no permission: full settings")
        elif need is None:
            return _jerr("not allowed")
        elif not perms.get(need):
            return _jerr("no permission: " + need)
        if act in _TARGET_ACTS:
            try:
                tgt = int(data.get("target") or uid or 0)
            except Exception:
                tgt = 0
            data["target"] = tgt
        if act == "toggle_access":
            if scope != "all":
                return _jerr("needs access to all accounts")
        else:
            err = _sub_target_err(auid, scope, tgt)
            if err:
                return _jerr(err)
    else:
        need = {"/api/state": "open_accounts", "/api/channels": "open_accounts", "/api/logs": "view_logs"}.get(path)
        if path == "/api/settings":
            if perms.get("settings", 0) < 1:
                return _jerr("no permission: settings")
        elif need is None:
            return _jerr("not found")
        elif not perms.get(need):
            return _jerr("no permission: " + need)
        err = _sub_target_err(auid, scope, uid)
        if err:
            return _jerr(err)
        if path == "/api/logs":
            qs.pop("all", None)
        if path == "/api/settings":
            vals = data.get("values")
            lvl = int(perms.get("settings", 0))
            data["values"] = ({k: v for k, v in vals.items() if lvl >= 2 or k not in LIMIT_KEYS}
                              if isinstance(vals, dict) else {})
    r = await _orig_web_api_36("admin", 0, path, qs, data)
    if isinstance(r, dict):
        r.pop("token", None)                         # never hand a user's private link to a sub-admin
        if path == "/api/state" and not perms.get("view_money") and r.get("ok"):
            try:
                r["stats"]["gram"], r["stats"]["balance"] = 0, ""
                r["sess"]["gram"], r["sess"]["rate"] = 0, 0
            except Exception as _swallow_e:
                _swallow(_swallow_e)
        if r.get("ok") and path in ("/api/action", "/api/settings"):
            evt(tgt or auid, "STAFF", f"admin {auid}: {act or 'settings'}")
    return r


# ---------------------------------------------------------------------------------------------
# 4) owner API: define / edit / remove admins
# ---------------------------------------------------------------------------------------------
def _admin_row(uid: int) -> dict:
    u = user_row(uid)
    r = _sub_rows().get(uid)
    if r is not None and not r["active"]:
        r = None
    return {"uid": uid, "name": u.get("first_name") or "", "username": u.get("username") or "",
            "perms": r["perms"] if r else dict(FULL_PERMS), "scope": r["scope"] if r else "all",
            "note": r["note"] if r else "", "legacy": uid in admin_set(), "implicit": r is None}


async def _owner_api(path: str, qs: dict, data: dict) -> dict:
    def tid() -> int:
        try:
            return int((qs.get("target") or [""])[0] or data.get("target") or 0)
        except Exception:
            return 0

    rows = _sub_rows()
    if path == "/api/owner/list":
        ids = [u for u, r in rows.items() if r["active"]]
        ids += [u for u in admin_set() if u != owner_id() and u not in ids]
        return {"ok": True, "owner": owner_id(), "base": bool(dash_base()),
                "admins": [_admin_row(u) for u in sorted(set(ids))]}
    target = tid()
    if target <= 0 or target == owner_id():
        return _jerr("invalid user id (the owner cannot be changed)")
    if path == "/api/owner/save":
        scope = _norm_scope(data.get("scope"))
        if scope != "all" and not scope:
            return _jerr("pick at least one account, or choose all accounts")
        was = _sub_active(target)
        sub_save(target, data.get("perms") or {}, scope, str(data.get("note") or ""))
        evt(owner_id(), "STAFF", f"admin {target} saved")
        if not was:
            spawn(notify(target, L(target,
                                   "🛠 You were made an admin helper. Send /start to see the <b>Admin dashboard</b> button.",
                                   "🛠 به‌عنوان ادمین انتخاب شدی. /start رو بفرست تا دکمهٔ <b>داشبورد ادمین</b> رو ببینی.")),
                  f"adm-note-{target}")
        return {"ok": True, "msg": "saved"}
    if path == "/api/owner/remove":
        if target in admin_set():
            return _jerr("listed in ADMIN_IDS: switch all its permissions off (or delete it from ADMIN_IDS in .env)")
        db_exec("UPDATE pg_subadmin SET active=0, epoch=epoch+1 WHERE uid=?", (target,))
        _SUBC["rows"] = None
        return {"ok": True, "msg": "removed"}
    if path == "/api/owner/relink":
        n = db_exec("UPDATE pg_subadmin SET epoch=epoch+1 WHERE uid=? AND active=1", (target,)).rowcount
        _SUBC["rows"] = None
        return {"ok": True, "msg": "new link created, the old one is dead"} if n else _jerr("save its permissions first")
    if path == "/api/owner/link":
        if staff_perms(target) is None:
            return _jerr("not an admin")
        url = dash_url(target, True)
        return {"ok": True, "url": url} if url else _jerr("dashboard address unknown - set WEB_BASE_URL")
    return _jerr("not found")


_orig_web_api_36 = web_api


async def web_api(role: str, wuid: int, path: str, qs: Dict[str, List[str]], data: dict) -> dict:
    if role == "sub":
        return await _sub_api(wuid, path, qs, data)
    if role == "admin":
        if path.startswith("/api/owner/"):
            return await _owner_api(path, qs, data)
        if path == "/api/me":
            r = await _orig_web_api_36(role, wuid, path, qs, data)
            r["owner"] = True
            return r
    return await _orig_web_api_36(role, wuid, path, qs, data)


# ---------------------------------------------------------------------------------------------
# 5) dashboard: "Owner" page (JS is added to the HTML template at start-up)
# ---------------------------------------------------------------------------------------------
_OWNER_JS = r"""
/* ---- owner panel: sub-admins ---- */
let OWN=null,_OWL=0;
const OPERMS=[
 ['view_accounts','See the accounts list','دیدن لیست اکانت‌ها','accounts','اکانت‌ها'],
 ['view_money','See GRAM, balances and totals','دیدن GRAM، موجودی و آمار کل','GRAM','GRAM'],
 ['open_accounts','Open a user dashboard (status, channels)','باز کردن داشبورد کاربر (وضعیت، کانال‌ها)','open','باز کردن'],
 ['view_logs','Read user logs','خواندن لاگ کاربران','logs','لاگ'],
 ['control','Start, stop, pause, fetch tasks','شروع، توقف، مکث، دریافت تسک','control','کنترل'],
 ['leave_clean','Leave channels and deep clean','ترک کانال‌ها و پاکسازی کامل','clean','پاکسازی'],
 ['reset','Reset stats and links, log users out','ریست آمار و لینک، خروج کاربر','reset','ریست'],
 ['manage_access','Open or invite-only access, allow-list','دسترسی باز یا دعوتی و لیست مجاز','access','دسترسی'],
 ['ban','Ban and unban users','مسدود کردن و رفع مسدودی','ban','بن']];
const OTPL={viewer:{view_accounts:1,open_accounts:1,view_logs:1,settings:0},
 operator:{view_accounts:1,open_accounts:1,view_logs:1,control:1,settings:1},
 full:{view_accounts:1,view_money:1,open_accounts:1,view_logs:1,control:1,leave_clean:1,reset:1,manage_access:1,ban:1,settings:2}};
async function ownerLoad(){if(_OWL)return;_OWL=1;try{const r=await api('/api/owner/list');if(r.ok)OWN=r;else toast(r.error||'error',1)}catch(e){}_OWL=0}
function ownerPaint(force){const v=$('#view');
  if(force||!OWN){v.innerHTML='<div class="card">…</div>';ownerLoad().then(()=>{if(PAGE==='owner'&&OWN)v.innerHTML=ownerHTML()});return}
  if(!$('#ownv'))v.innerHTML=ownerHTML()}
function ownTags(p){
  const a=OPERMS.filter(o=>p[o[0]]).map(o=>'<span class="tag ok">'+tr(o[3],o[4])+'</span>').join('');
  const s=p.settings?'<span class="tag '+(p.settings>1?'warn':'ok')+'">'+tr('settings: ','تنظیمات: ')+(p.settings>1?tr('full','کامل'):tr('basic','پایه'))+'</span>':'';
  return (a+s)||'<span class="tag bad">'+tr('no access','بدون دسترسی')+'</span>'}
function ownerHTML(){
  const rows=OWN.admins.map(x=>'<tr><td>'+esc(x.name||x.uid)+'<br><small style="color:var(--mut)">'+x.uid+(x.username?' · @'+esc(x.username):'')+(x.note?' · '+esc(x.note):'')+'</small></td><td>'+ownTags(x.perms)+(x.implicit?'<span class="tag warn">ADMIN_IDS · '+tr('full access','دسترسی کامل')+'</span>':'')+'</td><td>'+(x.scope==='all'?tr('all accounts','همه اکانت‌ها'):x.scope.length+' '+tr('accounts','اکانت'))+'</td><td><div class="row"><button class="btn sm" data-ox="edit" data-u="'+x.uid+'">'+tr('Edit','ویرایش')+'</button><button class="btn sm" data-ox="link" data-u="'+x.uid+'">'+tr('Copy link','کپی لینک')+'</button>'+(x.implicit?'':'<button class="btn sm" data-ox="relink" data-u="'+x.uid+'">'+tr('New link','لینک جدید')+'</button>')+(x.legacy?'':'<button class="btn sm bad" data-ox="remove" data-u="'+x.uid+'">'+tr('Remove','حذف')+'</button>')+'</div></td></tr>').join('');
  const perms=OPERMS.map(o=>'<div class="fld"><div class="fl"><b>'+tr(o[1],o[2])+'</b></div><div class="fc"><label class="sw"><input type="checkbox" data-pm="'+o[0]+'"><i></i></label></div></div>').join('');
  return '<div id="ownv"><div class="card"><h3>🔑 '+tr('Admins','ادمین‌ها')+'<small>'+OWN.admins.length+'</small></h3>'+
   (OWN.base?'':'<div class="banner warn">'+tr('Dashboard address unknown: set WEB_BASE_URL so links and the Telegram button work.','آدرس داشبورد مشخص نیست: WEB_BASE_URL رو تنظیم کن تا لینک و دکمهٔ تلگرام کار کنه.')+'</div>')+
   (rows?'<div class="scroll"><table><thead><tr><th>'+tr('Admin','ادمین')+'</th><th>'+tr('Permissions','دسترسی‌ها')+'</th><th>'+tr('Scope','محدوده')+'</th><th></th></tr></thead><tbody>'+rows+'</tbody></table></div>':'<p style="color:var(--mut)">'+tr('No admins yet.','هنوز ادمینی تعریف نشده.')+'</p>')+
   '<small style="color:var(--mut)">'+tr('Admins never see this Owner page. Each one only gets what you tick below.','ادمین‌ها هیچ‌وقت این صفحه رو نمی‌بینن. هر کدوم فقط چیزی رو داره که پایین تیک می‌زنی.')+'</small></div>'+
   '<div class="card" id="owform"><h3>➕ '+tr('Add or edit an admin','افزودن یا ویرایش ادمین')+'</h3>'+
   '<div class="row" style="margin-bottom:10px"><input class="inp" id="ow-id" placeholder="Telegram user id" style="width:200px"><input class="inp" id="ow-note" placeholder="'+tr('note (optional)','یادداشت (اختیاری)')+'" style="flex:1;min-width:160px"></div>'+
   '<div class="row" style="margin-bottom:6px"><button class="btn sm" data-ox="tpl" data-t="viewer">'+tr('Viewer','بیننده')+'</button><button class="btn sm" data-ox="tpl" data-t="operator">'+tr('Operator','اپراتور')+'</button><button class="btn sm" data-ox="tpl" data-t="full">'+tr('Everything except owner panel','همه‌چیز جز پنل مالک')+'</button></div>'+
   perms+
   '<div class="fld"><div class="fl"><b>'+tr('Settings of users','تنظیمات کاربران')+'</b><small>'+tr('Basic = profile, rhythm, schedule, cleanup, views. Full also allows speed numbers, caps and presets.','پایه = پروفایل، ریتم، برنامه زمانی، پاکسازی، ویو. کامل شامل عددهای سرعت، سقف‌ها و پریست هم می‌شه.')+'</small></div><div class="fc"><select id="ow-lvl"><option value="0">'+tr('no access','بدون دسترسی')+'</option><option value="1">'+tr('basic','پایه')+'</option><option value="2">'+tr('full','کامل')+'</option></select></div></div>'+
   '<div class="fld"><div class="fl"><b>'+tr('Which accounts','کدام اکانت‌ها')+'</b><small>'+tr('All accounts, or only the user ids listed (comma separated). Owner and other admins are never reachable.','همه اکانت‌ها یا فقط آیدی‌های واردشده (با کاما). مالک و ادمین‌های دیگه هیچ‌وقت در دسترس نیستن.')+'</small></div><div class="fc row"><select id="ow-scope"><option value="all">'+tr('all accounts','همه اکانت‌ها')+'</option><option value="list">'+tr('only these ids','فقط این آیدی‌ها')+'</option></select><input class="inp" id="ow-ids" placeholder="111, 222" style="width:180px"></div></div>'+
   '<div class="row" style="margin-top:14px"><button class="btn pri" data-ox="save">'+tr('Save admin','ذخیره ادمین')+'</button></div></div></div>'}
function fillForm(a){
  $('#ow-id').value=a?a.uid:'';$('#ow-note').value=a?(a.note||''):'';
  const p=a?a.perms:{};document.querySelectorAll('[data-pm]').forEach(c=>c.checked=!!p[c.dataset.pm]);
  $('#ow-lvl').value=String(p.settings||0);const all=!a||a.scope==='all';
  $('#ow-scope').value=all?'all':'list';$('#ow-ids').value=all?'':a.scope.join(', ')}
async function ownerSave(){
  const id=+($('#ow-id').value||0);if(!id)return toast(tr('Enter a user id','آیدی کاربر رو وارد کن'),1);
  const perms={};document.querySelectorAll('[data-pm]').forEach(c=>perms[c.dataset.pm]=c.checked?1:0);perms.settings=+$('#ow-lvl').value;
  const sc=$('#ow-scope').value==='all'?'all':($('#ow-ids').value||'').split(/[,\s]+/).filter(Boolean).map(Number).filter(n=>n>0);
  const r=await api('/api/owner/save',{target:id,perms:perms,scope:sc,note:$('#ow-note').value});
  if(r.ok){toast(tr('Saved','ذخیره شد'));await ownerLoad();if(PAGE==='owner')$('#view').innerHTML=ownerHTML()}else toast(r.error||'error',1)}
document.addEventListener('click',async e=>{
  const t=e.target.closest('[data-ox]');if(!t||!OWN)return;const x=t.dataset.ox,u=+t.dataset.u||0;
  if(x==='save')return ownerSave();
  if(x==='edit'){fillForm(OWN.admins.find(z=>z.uid===u));$('#owform').scrollIntoView({behavior:'smooth'});return}
  if(x==='tpl'){const p=OTPL[t.dataset.t];document.querySelectorAll('[data-pm]').forEach(c=>c.checked=!!p[c.dataset.pm]);$('#ow-lvl').value=String(p.settings||0);return}
  if(x==='link'){const r=await api('/api/owner/link?target='+u);
    if(r.ok){try{await navigator.clipboard.writeText(r.url);toast(tr('Link copied','لینک کپی شد'))}catch(_){prompt('Link',r.url)}}else toast(r.error||'error',1);return}
  if(x==='relink'||x==='remove'){
    const ok=await confirmBox({title:x==='remove'?tr('Remove this admin?','حذف این ادمین؟'):tr('Create a new link?','ساخت لینک جدید؟'),body:x==='remove'?tr('Access stops immediately.','دسترسی فوراً قطع می‌شه.'):tr('The old link stops working at once.','لینک قبلی فوراً از کار می‌افته.'),danger:1});
    if(!ok)return;const r=await api('/api/owner/'+x,{target:u});toast(r.ok?(r.msg||'OK'):r.error,!r.ok);
    await ownerLoad();if(PAGE==='owner')$('#view').innerHTML=ownerHTML()}
});
"""

_tpl_patch("nav.push(['admin','🛠',tr('Admin','مدیریت')]);",
           "nav.push(['admin','🛠',tr('Admin','مدیریت')]);if(window.IS_OWNER)nav.push(['owner','🔑',tr('Owner','مالک')]);")
_tpl_patch("admin:tr('Admin','مدیریت')}[PAGE])",
           "admin:tr('Admin','مدیریت'),owner:tr('Owner panel','پنل مالک')}[PAGE])")
_tpl_patch("if(PAGE==='admin'){const h=vAdmin()",
           "if(PAGE==='owner'){ownerPaint(force);return}if(PAGE==='admin'){const h=vAdmin()")
_tpl_patch("ROLE=me.role;UID=me.uid;", "ROLE=me.role;UID=me.uid;window.IS_OWNER=!!me.owner;")
_tpl_patch("boot();\n</script></body></html>", _OWNER_JS + "\nboot();\n</script></body></html>")



# ===== PATCH 3.6.1 - hide what a sub-admin may not use =====
PATCH_3_6_1_APPLIED = True
VERSION = "3.6.1"

_UI_JS = r"""
/* ---- sub-admin UI gating ---- */
function can(k){return !window.PERMS||!!PERMS[k]}
function stripBtn(h,attr,vals){vals.forEach(v=>{h=h.replace(new RegExp('<button[^>]*'+attr+'="'+v+'"[^>]*>[\\s\\S]*?</button>','g'),'')});return h}
const _vOv0=vOverview;
vOverview=function(){let h=_vOv0();if(!window.PERMS)return h;
 if(!PERMS.control)h=stripBtn(h,'data-x',['start','stop','pause','resume','fetch','boost']);
 if(!(PERMS.settings>=2))h=stripBtn(h,'data-p',['careful','balanced','fast']);
 if(!PERMS.reset)h=stripBtn(h,'data-x',['reset_stats','reset_warmup','reset_token','logout']);
 return h};
const _vCh0=vChannels;
vChannels=function(){let h=_vCh0();if(!window.PERMS)return h;
 if(!PERMS.leave_clean)h=stripBtn(h,'data-x',['leave_old','leave_all','scan']);
 return h};
const _pt0=paintTracked;
paintTracked=function(){_pt0();if(!can('leave_clean')){const b=$('#trk');if(b)b.querySelectorAll('[data-lr]').forEach(x=>x.remove())}};
const _LIM=['adv_mode','adv_gap','adv_batch','adv_conc','cap_hour','cap_day','flood_stop'];
const _fh0=fieldHTML;
fieldHTML=function(f){if(window.PERMS&&!(PERMS.settings>=2)&&_LIM.includes(f.k))return '';return _fh0(f)};
const _vs0=vSettings;
vSettings=function(){let h=_vs0();if(!window.PERMS)return h;
 h=h.replace(/<div class="card"><h3>[^<]*<\/h3><\/div>/g,'');
 if(!(PERMS.settings>=2)){const a=h.indexOf('<div class="card"><h3>🎛');
  if(a>=0){let b=h.indexOf('<div class="card"><h3>',a+10);if(b<0)b=h.indexOf('<div id="sbar">');h=h.slice(0,a)+h.slice(b)}}
 return h};
const _vAd0=vAdmin;
vAdmin=function(){let h=_vAd0();if(!window.PERMS)return h;
 if(!PERMS.manage_access)h=stripBtn(h,'data-x',['access','allow','disallow']);
 if(!PERMS.ban)h=stripBtn(h,'data-x',['ban','unban']);
 if(!PERMS.manage_access&&!PERMS.ban){const a=h.indexOf('<div class="card"><h3>🔓'),b=h.indexOf('<div class="card"><h3>👥');if(a>=0&&b>a)h=h.slice(0,a)+h.slice(b)}
 return h};
"""

_tpl_patch("window.IS_OWNER=!!me.owner;", "window.IS_OWNER=!!me.owner;window.PERMS=me.perms||null;")
_tpl_patch("if(window.IS_OWNER)nav.push(['owner','🔑',tr('Owner','مالک')]);",
           "if(window.IS_OWNER)nav.push(['owner','🔑',tr('Owner','مالک')]);"
           "if(window.PERMS){for(let i=nav.length-1;i>=0;i--){const n=nav[i][0];"
           "if(((n==='overview'||n==='channels')&&!PERMS.open_accounts)||(n==='settings'&&!(PERMS.settings>=1))||(n==='logs'&&!PERMS.view_logs))nav.splice(i,1)}}")
_tpl_patch("<td class=\"row\"><button class=\"btn sm\" data-open=\"'+a.uid+'\">'+tr('Open','باز کردن')+'</button>'+(a.online?(a.running?",
           "<td class=\"row\">'+(can('open_accounts')?'<button class=\"btn sm\" data-open=\"'+a.uid+'\">'+tr('Open','باز کردن')+'</button>':'')+(a.online&&can('control')?(a.running?")
_tpl_patch("boot();\n</script></body></html>", _UI_JS + "\nboot();\n</script></body></html>")



# ===== PATCH 3.7 - new dashboard UI, address-style URLs, history charts =====
PATCH_3_7_APPLIED = True
VERSION = "3.7"

import re as _re37

_PRETTY_RE = _re37.compile(r"^/(?:u/\d+/panel|admin/panel|owner/panel)(?:/[A-Za-z0-9_-]*)?/?$")
_UI = {"mtime": 0.0, "html": None}


def _ui_tpl() -> str:
    """panel_ui.html next to the helper (hot reload); falls back to the built-in old template."""
    p = BASE_DIR / "panel_ui.html"
    try:
        m = p.stat().st_mtime
        if _UI["html"] is None or m != _UI["mtime"]:
            _UI["html"], _UI["mtime"] = p.read_text(encoding="utf-8"), m
        return _UI["html"]
    except Exception:
        return _PANEL_TPL


_orig_route_37 = WebPanel._route


async def _route_37(self, method, path, headers, body, writer, ip):
    from urllib.parse import urlparse
    p = urlparse(path).path
    if method == "GET" and (p in ("/", "/index.html") or _PRETTY_RE.match(p)):
        await self._send(writer, 200, "text/html", _ui_tpl().encode("utf-8"))
        return
    await _orig_route_37(self, method, path, headers, body, writer, ip)


WebPanel._route = _route_37

# --- pretty links in Telegram buttons / console ------------------------------------------------------
_orig_dash_url_37 = dash_url


def dash_url(uid: int, admin: bool = False) -> str:
    u = _orig_dash_url_37(uid, admin)
    if not u or "/?t=" not in u:
        return u
    base, tok = u.split("/?t=", 1)
    return f"{base}{'/admin/panel/accounts' if admin else f'/u/{uid}/panel/overview'}?t={tok}"


# --- history sampler ---------------------------------------------------------------------------------
HIST_EVERY = 300.0
_HIST = {"ts": 0.0, "clean": 0.0}
_orig_init_db_37 = init_db


def init_db() -> None:
    first = _conn is None
    _orig_init_db_37()
    if first and _conn is not None:
        try:
            _conn.execute("CREATE TABLE IF NOT EXISTS pg_hist (uid INTEGER NOT NULL, ts REAL NOT NULL, gram REAL DEFAULT 0, "
                          "joined INTEGER DEFAULT 0, completed INTEGER DEFAULT 0, views INTEGER DEFAULT 0, errors INTEGER DEFAULT 0)")
            _conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_uid_ts ON pg_hist(uid, ts)")
        except Exception as e:
            log.warning(f"patch 3.7 init: {type(e).__name__}: {e}")


def _hist_sample() -> None:
    if _conn is None or not ACCOUNTS:
        return
    now = time.time()
    try:
        rows = _conn.execute("SELECT uid,gram,joined,completed,views,errors FROM pg_stats").fetchall()
        data = [(int(r["uid"]), now, float(r["gram"] or 0), int(r["joined"] or 0), int(r["completed"] or 0),
                 int(r["views"] or 0), int(r["errors"] or 0)) for r in rows if int(r["uid"]) in ACCOUNTS]
        if data:
            _conn.executemany("INSERT INTO pg_hist (uid,ts,gram,joined,completed,views,errors) VALUES (?,?,?,?,?,?,?)", data)
        if now - _HIST["clean"] > 3600:
            _HIST["clean"] = now
            _conn.execute("DELETE FROM pg_hist WHERE ts<?", (now - 15 * 86400,))
    except Exception as e:
        log.warning(f"history sample: {type(e).__name__}: {e}")


_orig_flush_stats_37 = flush_stats


def flush_stats(uid: Optional[int] = None) -> None:
    _orig_flush_stats_37(uid)
    if uid is None and time.time() - _HIST["ts"] >= HIST_EVERY:
        _HIST["ts"] = time.time()
        _hist_sample()


def _hist_api(uid: int, qs: dict) -> dict:
    try:
        hours = max(1, min(336, int((qs.get("hours") or ["24"])[0])))
    except Exception:
        hours = 24
    now = time.time()
    bucket = max(300, int(hours * 3600 / 96))
    rows = db_exec("SELECT ts,gram,joined,completed,views,errors FROM pg_hist WHERE uid=? AND ts>=? ORDER BY ts",
                   (uid, now - hours * 3600 - bucket)).fetchall()
    out: list = []
    last = None
    for r in rows:
        b = int(r["ts"] // bucket)
        item = [float(r["ts"]), float(r["gram"]), int(r["joined"]), int(r["completed"]), int(r["views"]), int(r["errors"])]
        if b == last:
            out[-1] = item
        else:
            out.append(item)
            last = b
    s = stat_get(uid)
    out.append([now, float(s["gram"]), int(s["joined"]), int(s["completed"]), int(s.get("views", 0) or 0), int(s["errors"])])
    return {"ok": True, "hours": hours, "bucket": bucket, "points": out}


_orig_web_api_37 = web_api


async def web_api(role: str, wuid: int, path: str, qs: Dict[str, List[str]], data: dict) -> dict:
    if path == "/api/history":
        if role == "user":
            uid = wuid
        elif role == "admin":
            uid = _q_uid(qs, data)
        else:
            st = staff_perms(wuid)
            if st is None:
                return _jerr("unauthorized")
            perms, scope = st
            if not perms.get("open_accounts"):
                return _jerr("no permission: open_accounts")
            uid = _q_uid(qs, data)
            err = _sub_target_err(wuid, scope, uid)
            if err:
                return _jerr(err)
        if not uid:
            return _jerr("no account selected")
        if uid not in ACCOUNTS and not user_known(uid):
            return _jerr("unknown account")
        return _hist_api(uid, qs)
    return await _orig_web_api_37(role, wuid, path, qs, data)


# >>>>> PATCH 3.8 BEGIN (written by patch.py) >>>>>
import math

PATCH_3_8_APPLIED = True
VERSION = "3.8"

HOUR_CAP_MIN = max(1, _env_int("HOUR_CAP_MIN", 45))
HOUR_CAP_MAX = max(HOUR_CAP_MIN, _env_int("HOUR_CAP_MAX", 90))
IDLE_ROUNDS = 6                      # "nothing to do" observations needed before an auto-stop
CONFIG.setdefault("pg_group_category", _env_str("PG_GROUP_CATEGORY", "Groups"))

# ---------------------------------------------------------------------------------------------
# 0) new per-account settings (stored in pg_cfg, shown in the dashboard)
# ---------------------------------------------------------------------------------------------
EXTRA_COLS.update({
    "do_channels": ("INTEGER", 1),    # take "Channels" tasks
    "do_groups": ("INTEGER", 1),      # take "Groups" tasks
    "idle_stop": ("INTEGER", 1),      # stop by itself when no tasks are left
    "spread": ("INTEGER", 1),         # accounts get their own rhythm and never all work at once
    "auto_sleep": ("INTEGER", 1),     # a few hours of sleep per day at a random time
})
SETTINGS_SPEC.update({
    "do_channels": ("bool", 0, 1),
    "do_groups": ("bool", 0, 1),
    "idle_stop": ("bool", 0, 1),
    "spread": ("bool", 0, 1),
    "auto_sleep": ("bool", 0, 1),
})
# presets: only human flavours remain ("fast" is just an alias of balanced now)
for _n in ("careful", "balanced"):
    PRESETS[_n].update(profile="smart", adv_mode=0)
PRESETS["fast"] = dict(PRESETS["balanced"])

# ---------------------------------------------------------------------------------------------
# 1) one single mode: the human mode (profile / advanced numbers can no longer be changed)
# ---------------------------------------------------------------------------------------------
_orig_get_cfg_38 = get_cfg


def get_cfg(uid: int) -> dict:
    c = _orig_get_cfg_38(uid)
    c["profile"] = "smart"
    c["adv_mode"] = 0
    c["adv_gap"] = 0.0
    c["adv_batch"] = 0
    c["adv_conc"] = 0
    return c


# ---------------------------------------------------------------------------------------------
# 2) personality of every account (stable per user, different between users)
# ---------------------------------------------------------------------------------------------
_PERS: Dict[int, dict] = {}


def _persona(uid: int) -> dict:
    p = _PERS.get(uid)
    if p is None:
        r = random.Random(f"p38:{uid}:{_web_secret().decode(errors='ignore')[:24]}")
        p = {
            "speed": r.uniform(0.75, 1.35),      # >1 = a bit quicker person
            "react": r.uniform(0.7, 1.6),        # reaction time factor
            "work": r.uniform(12, 48),           # typical work burst (minutes)
            "brk": r.uniform(3, 14),             # typical break (minutes)
            "conc": 1 if r.random() < 0.7 else 2,
            "peak": r.uniform(9, 23),            # hour of the day this person is most active
            "sleep_at": r.randint(30, 300),      # minutes after midnight the sleep starts (+- daily jitter)
            "sleep_len": r.randint(190, 400),    # sleep length in minutes
            "delay": r.uniform(0.3, 1.6),        # how late this account starts after "Start"
            "skip": r.uniform(0.04, 0.14),       # share of tasks this person ignores
        }
        _PERS[uid] = p
    return p


def _activity(uid: int) -> float:
    """0..1 - how 'awake' this person is right now (their personal daily curve)."""
    n = local_now(uid)
    h = n.hour + n.minute / 60.0
    return 0.5 + 0.5 * math.cos((h - _persona(uid)["peak"]) / 24.0 * 2.0 * math.pi)


def _react(uid: int, med: float = 2.0) -> float:
    """Human reaction time in seconds (log-normal, occasional 'looked away')."""
    v = random.lognormvariate(math.log(med * _persona(uid)["react"]), 0.55)
    if random.random() < 0.04:
        v += random.uniform(6, 30)
    return min(max(0.7, v), 60.0) * max(1.0, slow_eff(uid) ** 0.5)


# ---------------------------------------------------------------------------------------------
# 3) random hourly cap (45-90) - chosen by the code and re-rolled about once per hour
# ---------------------------------------------------------------------------------------------
def _hour_cap(uid: int) -> int:
    f, now = _farm(uid), time.time()
    if now >= f.get("hc_until", 0.0):
        f["hc"] = random.randint(HOUR_CAP_MIN, HOUR_CAP_MAX)
        f["hc_until"] = now + random.uniform(2700, 4500)
        log.info(f"u{uid:<11} | PACE   | hourly pace for the next ~hour: {f['hc']}/h")
    return int(f["hc"])


_orig_eff_limits_38 = eff_limits


def eff_limits(uid: int) -> Tuple[int, int, float]:
    _hl, dl, gi = _orig_eff_limits_38(uid)
    return _hour_cap(uid), dl, gi


# ---------------------------------------------------------------------------------------------
# 4) human timing: gaps, reactions, batches, order, skipped tasks
# ---------------------------------------------------------------------------------------------
def _human_gap(uid: int) -> float:
    hl, _dl, gi = eff_limits(uid)
    P = _persona(uid)
    rate_gap = 3600.0 / max(1, hl) * 0.85 * night_mult(uid) * slow_eff(uid) / P["speed"]
    mean = max(gi, rate_gap)
    g = mean * random.lognormvariate(-0.18, 0.6)          # irregular, mean ~ rate that fits the hourly cap
    r = random.random()
    if r < 0.06:
        g += random.uniform(15, 90)                       # got distracted
    elif r < 0.10:
        g *= random.uniform(0.35, 0.6)                    # quick streak
    return min(max(HARD_MIN_GAP * 1.3, g), max(60.0, mean * 6.0))


async def _join_slot(uid: int) -> None:
    f = _farm(uid)
    async with _lock(uid, "pace"):
        wait = f.get("next_join", 0.0) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        f["next_join"] = time.time() + max(HARD_MIN_GAP, _human_gap(uid))


async def _probe_slot(uid: int) -> None:
    f = _farm(uid)
    async with _lock(uid, "probe"):
        wait = f.get("next_probe", 0.0) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        f["next_probe"] = time.time() + random.uniform(0.8, 2.6) * max(1.0, slow_eff(uid))


def _check_delay(uid: int) -> float:
    return min(12.0, _react(uid, 2.4))


def _gap(uid: int, lo: float, hi: float) -> float:
    v = random.uniform(lo, hi) * _mult(uid) + abs(random.gauss(0.8, 0.5))
    if random.random() < 0.08:
        v += random.uniform(1.5, 6.0)
    return v


def _batch_size(uid: int) -> int:
    r = random.random()
    n = 1 if r < 0.22 else 2 if r < 0.5 else 3 if r < 0.75 else random.randint(4, 6)
    return max(1, min(HARD_MAX_BATCH, n))


def _batch_cap(uid: int) -> int:
    return min(HARD_MAX_BATCH, 6)


def conc_of(uid: int) -> int:
    return max(1, min(HARD_MAX_CONC, int(_persona(uid)["conc"])))


# tasks come out in a weighted-random order instead of "highest reward first" every time
_orig_extract_tasks_ex_38 = extract_tasks_ex


def extract_tasks_ex(msg) -> List[dict]:
    out = _orig_extract_tasks_ex_38(msg)
    for t in out:
        t["reward"] = float(t.get("reward") or 0.0) * random.uniform(0.25, 1.75)
    return out


# some tasks are simply ignored (a person does not take every offer)
_orig_claim_url_38 = _claim_url


def _claim_url(uid: int, url: str) -> bool:
    if random.random() < _persona(uid)["skip"]:
        return False
    return _orig_claim_url_38(uid, url)


# a real client marks the bot's messages as read after a moment
_H38_ATTACHED: Dict[int, Any] = {}
_orig_attach_38 = attach_user_handlers


def attach_user_handlers(uid: int, client: TelegramClient) -> None:
    _orig_attach_38(uid, client)
    if _H38_ATTACHED.get(uid) is client:
        return
    _H38_ATTACHED[uid] = client

    async def _read(event) -> None:
        try:
            if not event.is_private:
                return
            bot_id = (_TGT.get(uid) or {}).get("bot_id")
            if not bot_id or event.sender_id != bot_id:
                return
            f = _farm(uid)
            if time.time() - f.get("read_ts", 0.0) < random.uniform(12, 45) or random.random() < 0.15:
                return
            f["read_ts"] = time.time()
            await asyncio.sleep(random.uniform(0.8, 6.0))
            await client.send_read_acknowledge(event.chat_id, max_id=event.message.id)
        except DEAD_ERRORS:
            await handle_dead(uid)
        except Exception as e:
            await absorb(uid, e, "read")

    client.add_event_handler(_read, events.NewMessage(incoming=True))


_orig_cleanup_38 = cleanup_user


def cleanup_user(uid: int) -> None:
    _orig_cleanup_38(uid)
    _H38_ATTACHED.pop(uid, None)
    _WORKING.pop(uid, None)


# ---------------------------------------------------------------------------------------------
# 5) sleep, work / break cycles, staggered accounts
# ---------------------------------------------------------------------------------------------
_WORKING: Dict[int, float] = {}          # uid -> end of its current work burst
_NRUN: Dict[str, float] = {"ts": 0.0, "n": 0}


def _sleep_left(uid: int) -> float:
    P = _persona(uid)
    n = local_now(uid)
    r = random.Random(f"s38:{uid}:{n.date().toordinal()}")
    start = max(0, P["sleep_at"] + r.randint(-50, 50))
    dur = max(150, P["sleep_len"] + r.randint(-40, 40))
    cur = n.hour * 60 + n.minute + n.second / 60.0
    if start <= cur < start + dur:
        return (start + dur - cur) * 60.0
    return 0.0


_orig_sched_wait_38 = sched_wait


def sched_wait(uid: int) -> float:
    w = _orig_sched_wait_38(uid)
    if w > 0:
        return w
    try:
        c = get_cfg(uid)
        if c.get("auto_sleep", 1) and c["auto_join"] and c["auto_fetch"]:
            return _sleep_left(uid)
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    return 0.0


def _n_running() -> int:
    now = time.time()
    if now - _NRUN["ts"] > 5.0:
        n = 0
        for u in list(ACCOUNTS):
            c = get_cfg(u)
            if c["auto_join"] and c["auto_fetch"] and not c["paused"]:
                n += 1
        _NRUN["ts"], _NRUN["n"] = now, n
    return int(_NRUN["n"])


def _working_live(now: float) -> set:
    for u in [u for u, be in _WORKING.items() if be <= now or u not in ACCOUNTS]:
        _WORKING.pop(u, None)
    return set(_WORKING)


def _global_frac(now: float) -> float:
    """Share of the running accounts allowed to work at the same moment (drifts between ~30% and ~70%)."""
    return 0.5 + 0.2 * math.sin(now / 4200.0)


def _begin_break(uid: int, f: dict, now: float) -> float:
    P, act = _persona(uid), _activity(uid)
    ln = random.lognormvariate(math.log(P["brk"] * (1.6 - 0.9 * act)), 0.6)       # minutes
    if random.random() < 0.08:
        ln = random.uniform(25, 70)                                                # went away for a while
    ln = min(max(1.5, ln), 90.0)
    f["rest_until"], f["burst_end"] = now + ln * 60.0, 0.0
    _WORKING.pop(uid, None)
    return f["rest_until"] - now


def _begin_burst(uid: int, f: dict, cfg: dict, now: float) -> float:
    P = _persona(uid)
    if cfg.get("spread", 1):
        nrun = _n_running()
        if nrun > 2:
            live = _working_live(now)
            lim = max(1, int(round(nrun * _global_frac(now))))
            if uid not in live and len(live) >= lim:      # too many accounts busy right now -> wait a bit
                w = random.uniform(60, 360)
                f["rest_until"] = now + w
                return w
    ln = random.lognormvariate(math.log(P["work"] * (0.6 + 0.8 * _activity(uid))), 0.45)
    ln = min(max(4.0, ln), 150.0)
    f["burst_end"] = now + ln * 60.0
    _WORKING[uid] = f["burst_end"]
    return 0.0


def smart_rest(uid: int) -> float:
    """Seconds the account must stay idle: sleeping, staggered start, break, or waiting for a free 'slot'."""
    f, now = _farm(uid), time.time()
    fresh = now - f.get("sr_ts", 0.0) > 300.0           # it was stopped / blocked for a while -> new start
    f["sr_ts"] = now
    sw = sched_wait(uid)
    if sw > 0:
        _WORKING.pop(uid, None)
        f["burst_end"] = 0.0
        return sw
    cfg = get_cfg(uid)
    if fresh:
        f["burst_end"], f["rest_until"] = 0.0, 0.0
        f["dry"], f["dry_since"], f["dry_last"] = 0, 0.0, 0.0
        if cfg.get("spread", 1):
            mx = 45.0 if _n_running() <= 2 else 600.0
            d = (random.random() ** 1.6) * mx * _persona(uid)["delay"]
            if d > 4.0:
                f["rest_until"] = now + d
                return d
    if now < f.get("rest_until", 0.0):
        return f["rest_until"] - now
    be = f.get("burst_end", 0.0)
    if not be:
        return _begin_burst(uid, f, cfg, now)
    if now >= be:
        return _begin_break(uid, f, now)
    return 0.0


# ---------------------------------------------------------------------------------------------
# 6) categories: Channels + Groups
# ---------------------------------------------------------------------------------------------
_CAT_COUNT_RE = re.compile(r"(\d[\d,\.]*)\s*$")


def _menu_cats(msg) -> Dict[str, Tuple[int, int, Optional[int]]]:
    """{'channels': (row, col, count), 'groups': (row, col, count)} read from the menu buttons."""
    spec = (("channels", _flat(str(CONFIG["pg_category"])).lower()),
            ("groups", _flat(str(CONFIG.get("pg_group_category") or "Groups")).lower()))
    out: Dict[str, Tuple[int, int, Optional[int]]] = {}
    for i, row in enumerate(_btn_rows(msg)):
        for j, b in enumerate(row):
            if getattr(b, "data", None) is None:
                continue
            t = _flat(getattr(b, "text", "") or "")
            tl = t.lower()
            for key, want in spec:
                if key in out or not want or want not in tl:
                    continue
                m = _CAT_COUNT_RE.search(t)
                cnt = int(re.sub(r"\D", "", m.group(1)) or 0) if m else None
                out[key] = (i, j, cnt)
    return out


def find_category_button(msg) -> Optional[Tuple[int, int]]:
    c = _menu_cats(msg)
    for k in ("channels", "groups"):
        if k in c:
            return c[k][0], c[k][1]
    return None


async def handle_menu(uid: int, client: TelegramClient, msg) -> None:
    try:
        cfg, f, now = get_cfg(uid), _farm(uid), time.time()
        if cfg["paused"] or not cfg["auto_join"] or now < f["flood_until"] or now < f["bot_until"]:
            return
        cats = _menu_cats(msg)
        if not cats or now - f.get("menu_ts", 0.0) < 1.5:      # 2nd check only guards a double-fired event
            return
        f["menu_ts"], f["menu_seen_ts"], f["hops"] = now, now, 0
        opts: List[Tuple[str, Optional[int]]] = []
        for key, flag in (("channels", "do_channels"), ("groups", "do_groups")):
            if key in cats and cfg.get(flag, 1):
                cnt = cats[key][2]
                if cnt is None or cnt > 0:                      # never open an empty category
                    opts.append((key, cnt))
        if not opts:
            stat_note(uid, "no tasks in your categories")
            evt(uid, "MENU", "no tasks available in the selected categories")
            _dry_hit(uid)
            return
        keys = [k for k, _c in opts]
        last = f.get("cat_last")
        if last in keys and random.random() < 0.7:               # people stay on a category for a while
            pick = last
        else:
            pick = random.choices(keys, weights=[max(1, (c or 50)) ** 0.5 for _k, c in opts])[0]
        await asyncio.sleep(_react(uid, 2.2))                    # reading the menu, moving the finger...
        if is_paused(uid):
            return
        i, j, _c = cats[pick]
        f["cat_last"] = pick
        try:
            await msg.click(i, j)
            stat_note(uid, f"category: {pick}")
            evt(uid, "MENU", f"opened category {pick}")
        except BotResponseTimeoutError:
            pass
    except DEAD_ERRORS:
        await handle_dead(uid)
    except Exception as e:
        if not await absorb(uid, e, "menu"):
            log.warning(f"menu u{uid}: {type(e).__name__}: {e}")


# groups: old-style groups (basic chats) can be left as well
async def _dialog_channels(client: TelegramClient) -> Dict[int, Any]:
    out: Dict[int, Any] = {}
    async for d in client.iter_dialogs():
        if isinstance(d.entity, (Channel, tltypes.Chat)):
            out[utils.get_peer_id(d.entity)] = d.entity
    return out


_orig_LeaveChannelRequest_38 = LeaveChannelRequest


def LeaveChannelRequest(ent):
    if isinstance(ent, tltypes.Chat) and DeleteChatUserRequest is not None:
        return DeleteChatUserRequest(chat_id=ent.id, user_id=tltypes.InputUserSelf())
    return _orig_LeaveChannelRequest_38(ent)


# ---------------------------------------------------------------------------------------------
# 7) idle stop: no tasks left -> the account switches itself off
# ---------------------------------------------------------------------------------------------
def _mark_prod(uid: int) -> None:
    f = _farm(uid)
    f["prod_n"] = f.get("prod_n", 0) + 1
    f["dry"], f["dry_since"], f["dry_last"] = 0, 0.0, 0.0


def _dry_hit(uid: int) -> None:
    f, now = _farm(uid), time.time()
    if now - f.get("dry_last", 0.0) > 1200 or not f.get("dry_since"):
        f["dry"], f["dry_since"] = 0, now
        f["idle_limit"] = random.uniform(480, 900)               # every account has its own patience
    f["dry"] = f.get("dry", 0) + 1
    f["dry_last"] = now


def _blocked_38(uid: int) -> bool:
    """True while the account could not work anyway (so 'no progress' is not evidence of 'no tasks')."""
    cfg, f, now = get_cfg(uid), _farm(uid), time.time()
    if cfg["paused"] or not cfg["auto_join"] or not cfg["auto_fetch"]:
        return True
    if now < max(f["flood_until"], f["bot_until"], f.get("full_until", 0.0), f.get("rest_until", 0.0)):
        return True
    if sched_wait(uid) > 0 or budget_left(uid) <= 0:
        return True
    vw = f.get("vw")
    return bool(vw and vw.get("on"))


_orig_joins_record_38 = joins_record


def joins_record(uid: int, ref: str, title: str, peer_id: int, kind: str = "") -> None:
    _orig_joins_record_38(uid, ref, title, peer_id, kind)
    _mark_prod(uid)


_orig_handle_completed_38 = handle_completed


async def handle_completed(uid: int, msg) -> None:
    await _orig_handle_completed_38(uid, msg)
    _mark_prod(uid)


_orig_handle_task_list_38 = handle_task_list


async def handle_task_list(uid: int, client: TelegramClient, msg) -> None:
    f = _farm(uid)
    before = f.get("prod_n", 0)
    await _orig_handle_task_list_38(uid, client, msg)
    try:
        if f.get("prod_n", 0) == before and not _blocked_38(uid):
            _dry_hit(uid)                                        # a whole list and nothing doable in it
    except Exception as _swallow_e:
        _swallow(_swallow_e)


async def _idle_stop(uid: int) -> None:
    f, cfg = _farm(uid), get_cfg(uid)
    set_cfg(uid, auto_join=False, auto_fetch=False)
    f["dry"], f["dry_since"], f["dry_last"] = 0, 0.0, 0.0
    _WORKING.pop(uid, None)
    stat_note(uid, "⏹ auto-stop: no tasks left")
    evt(uid, "IDLE", "no tasks left - stopped automatically")
    names = ", ".join(n for n, fl in (("Channels", "do_channels"), ("Groups", "do_groups")) if cfg.get(fl, 1)) or "—"
    await notify(uid, L(
        uid,
        f"⏹ <b>No tasks left</b> — PR GRAM has nothing more for me in <b>{names}</b>.\n"
        "I stopped by myself so the account does not sit there polling. Press <b>Start</b> later when new tasks appear.",
        f"⏹ <b>تسکی باقی نمونده</b> — PR GRAM توی <b>{names}</b> چیزی برای من نداره.\n"
        "خودم متوقف شدم که اکانت بیخودی پرسه نزنه. وقتی تسک جدید اومد دوباره <b>شروع</b> رو بزن."))


# ---------------------------------------------------------------------------------------------
# 8) the farming tick (replaces the original one; the views wrapper of 3.5 keeps calling it)
# ---------------------------------------------------------------------------------------------
def _fetch_interval(uid: int, f: dict) -> float:
    v = random.lognormvariate(math.log(11.0), 0.6) / _persona(uid)["speed"]
    v *= 1.0 + 0.8 * min(int(f.get("dry", 0)), 6)               # nothing happening -> check back less often
    v *= max(1.0, slow_eff(uid) ** 0.5)
    if random.random() < 0.05:
        v += random.uniform(30, 150)
    return min(max(4.0, v), 600.0)


def _again_interval(uid: int) -> float:
    return min(max(2.0, random.lognormvariate(math.log(4.0), 0.5) * _persona(uid)["react"]), 20.0)


async def _farm_tick_38(uid: int, client: TelegramClient) -> None:
    cfg = get_cfg(uid)
    f, now = _farm(uid), time.time()
    if f.get("slow_notify") and now > f["flood_until"] and slow_eff(uid) <= 1.0:
        f["slow_notify"] = False
        await notify(uid, L(uid, "✅ <b>Back to full speed</b> — the flood slow-down is over.",
                            "✅ <b>سرعت دوباره کامل شد</b> — دوره کاهش سرعت فلود تموم شد."))
    if cfg["auto_leave"] and now - f.get("leave_ts", 0.0) > 900 and not _lock(uid, "leave").locked():
        f["leave_ts"] = now
        if joins_older_than(uid, leave_after_days(uid) * 86400):
            spawn(leave_old(uid, client, True), f"autoleave-{uid}")
    if cfg["paused"] or not (cfg["auto_fetch"] and cfg["auto_join"]):
        _WORKING.pop(uid, None)
        return
    if (cfg.get("idle_stop", 1) and f.get("dry", 0) >= IDLE_ROUNDS and f.get("dry_since")
            and now - f["dry_since"] >= f.get("idle_limit", 600.0) and now - f.get("dry_last", 0.0) < 1200):
        await _idle_stop(uid)
        return
    if not (_TGT.get(uid) or {}).get("bot_id"):
        return
    if now < f["flood_until"] or now < f["bot_until"] or now < f.get("full_until", 0.0):
        return
    if _lock(uid, "farm").locked() or not budget_ok(uid)[0]:
        return
    if smart_rest(uid) > 0:
        return
    if not f.get("last_fetch"):
        f["fetch_at"] = 0.0                                      # "Start" asked for an immediate first fetch
    fa = float(f.get("fetch_at", 0.0))
    if f.pop("again", False):                                    # productive round -> look again soon
        soon = now + _again_interval(uid)
        fa = soon if not fa else min(fa, soon)
        f["fetch_at"] = fa
    if now < fa:
        return
    if f.get("last_fetch") and f.get("last_task_ts", 0.0) < f["last_fetch"]:
        f["empty"] = min(int(f.get("empty", 0)) + 1, 6)
        _dry_hit(uid)                                            # the last request brought no task list at all
    f["last_fetch"] = now
    f["fetch_at"] = now + _fetch_interval(uid, f)
    try:
        await request_tasks(uid, client)
    except DEAD_ERRORS:
        await handle_dead(uid)


_orig_farm_tick = _farm_tick_38        # farm_tick (views wrapper) resolves this name at call time


# ---------------------------------------------------------------------------------------------
# 9) dashboard: honest risk score, hide removed modes, new "Human behaviour" section
# ---------------------------------------------------------------------------------------------
_orig_risk_of_38 = risk_of


def risk_of(uid: int) -> dict:
    r = _orig_risk_of_38(uid)
    pen = [p for p in r["pen"] if p["k"] not in ("batch_high", "conc_high")]
    score = max(0, 100 - sum(p["pts"] for p in pen))
    r["pen"], r["score"] = pen, score
    r["level"] = "good" if score >= 75 else "ok" if score >= 50 else "risky"
    return r


_UI_CSS_38 = (
    "#sec-speed,[data-act=sec][data-v=speed],.pcard[data-v=fast],"
    ".fld[data-f=rest_on],.fld[data-f=rest_every],.fld[data-f=rest_len]{display:none!important}\n"
)
_UI_SECTION_38 = r"""{id:'human',ic:'bot',en:'Human behaviour',fa:'رفتار انسانی',f:[
 {k:'do_channels',t:'sw',en:'Join channels',fa:'عضویت در کانال‌ها',he:'Take the Channels tasks of PR GRAM.',hf:'تسک‌های بخش کانال‌ها انجام بشه.'},
 {k:'do_groups',t:'sw',en:'Join groups',fa:'عضویت در گروه‌ها',he:'Take the Groups tasks too. The helper switches between categories like a person and skips a category that has no tasks.',hf:'تسک‌های بخش گروه‌ها هم انجام بشه. بین دسته‌ها مثل یه آدم جابه‌جا می‌شه و دسته‌ای که تسک نداره رو رد می‌کنه.'},
 {k:'spread',t:'sw',en:'Spread my accounts apart',fa:'پخش‌کردن اکانت‌ها از هم',he:'Every account gets its own rhythm: staggered start, different work and break lengths, and only a part of the running accounts works at the same moment.',hf:'هر اکانت ریتم خودش رو داره: شروع نامنظم، کار و استراحت متفاوت و فقط بخشی از اکانت‌های فعال هم‌زمان کار می‌کنن.'},
 {k:'auto_sleep',t:'sw',en:'Daily sleep',fa:'خواب روزانه',he:'Each account sleeps a few hours per day at its own random time, like a person.',hf:'هر اکانت هر روز چند ساعت توی زمان تصادفی خودش می‌خوابه، مثل یه آدم.'},
 {k:'idle_stop',t:'sw',en:'Stop automatically when no tasks are left',fa:'توقف خودکار وقتی تسکی نمونده',he:'If PR GRAM has nothing left to do for a while, the account switches itself off and you get a message.',hf:'اگه مدتی تسکی نباشه اکانت خودش خاموش می‌شه و بهت پیام میاد.'}]},
"""

_orig_ui_tpl_38 = _ui_tpl


def _ui_tpl() -> str:
    h = _orig_ui_tpl_38()
    if "id:'human'" not in h:
        if h.count("</style></head>") == 1:
            h = h.replace("</style></head>", _UI_CSS_38 + "</style></head>", 1)
        if h.count("{id:'cleanup',ic:'broom',") == 1:
            h = h.replace("{id:'cleanup',ic:'broom',", _UI_SECTION_38 + "{id:'cleanup',ic:'broom',", 1)
    return h


log.info(f"patch 3.8 loaded: groups + human mode ({HOUR_CAP_MIN}-{HOUR_CAP_MAX}/h random), spread, idle-stop")
# <<<<< PATCH 3.8 END <<<<<


# >>>>> PATCH 3.9 BEGIN (written by patch.py) >>>>>
import math

PATCH_3_9_APPLIED = True
VERSION = "3.9.1"

try:
    from telethon.tl.functions.account import UpdateStatusRequest, GetAuthorizationsRequest
except ImportError:
    UpdateStatusRequest = GetAuthorizationsRequest = None
try:
    from telethon.tl.functions.messages import GetMessagesViewsRequest
except ImportError:
    GetMessagesViewsRequest = None

DEVICE_PLATFORMS = _env_str("DEVICE_PLATFORMS", "")
DEVICE_VARY_APP_LANG = _env_bool("DEVICE_VARY_APP_LANG", False)

# ---------------------------------------------------------------------------------------------
# 0) new settings
# ---------------------------------------------------------------------------------------------
EXTRA_COLS.update({
    "hc_min": ("INTEGER", HOUR_CAP_MIN),     # lowest hourly join limit the code may pick
    "hc_max": ("INTEGER", HOUR_CAP_MAX),     # highest hourly join limit the code may pick
    "day_variety": ("INTEGER", 1),           # quiet / light / busy days
    "presence": ("INTEGER", 1),              # online while working, offline on breaks
    "browse": ("INTEGER", 1),                # sometimes look at a few posts after joining
    "do_views": ("INTEGER", 0),              # earn from the Views category (alone or mixed with channels / groups)
})
SETTINGS_SPEC.update({
    "hc_min": ("int", 10, 300),
    "hc_max": ("int", 10, 300),
    "day_variety": ("bool", 0, 1),
    "presence": ("bool", 0, 1),
    "browse": ("bool", 0, 1),
    "do_views": ("bool", 0, 1),
})
LIMIT_KEYS.update({"hc_min", "hc_max"})      # sub-admins need FULL settings to change them

_orig_init_db_39 = init_db


def init_db() -> None:
    first = _conn is None
    _orig_init_db_39()
    if first and _conn is not None:
        try:
            _conn.execute("CREATE TABLE IF NOT EXISTS pg_device (uid INTEGER PRIMARY KEY, "
                          "data TEXT NOT NULL, ts REAL DEFAULT 0)")
        except Exception as e:
            log.warning(f"patch 3.9 init: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------------------------
# 1) DEVICE IDENTITIES  (pure generator - no dependency on the rest of the helper)
# ---------------------------------------------------------------------------------------------
# --- DEVICE-GEN BEGIN
_PLAT_W = {"android": 62, "ios": 14, "windows": 12, "macos": 6, "linux": 6}
_PLAT_LABEL = {"android": "Android", "ios": "iOS", "windows": "Windows", "macos": "macOS", "linux": "Linux"}

_ANDROID = [  # (device model as Telegram reports it, lowest SDK, highest SDK)
    ("Samsung SM-S928B", 34, 35), ("Samsung SM-S926B", 34, 35), ("Samsung SM-S918B", 33, 35),
    ("Samsung SM-S911B", 33, 35), ("Samsung SM-S908B", 32, 34), ("Samsung SM-S901B", 32, 34),
    ("Samsung SM-G998B", 31, 34), ("Samsung SM-G991B", 31, 34), ("Samsung SM-G781B", 30, 33),
    ("Samsung SM-A546B", 33, 35), ("Samsung SM-A346B", 33, 35), ("Samsung SM-A236B", 33, 34),
    ("Samsung SM-A528B", 31, 34), ("Samsung SM-A556B", 34, 35), ("Samsung SM-M546B", 33, 34),
    ("Samsung SM-F946B", 33, 35), ("Samsung SM-A155F", 34, 35), ("Samsung SM-A256B", 34, 35),
    ("Xiaomi 2201123G", 31, 34), ("Xiaomi 2210132G", 33, 34), ("Xiaomi 23049PCD8G", 33, 34),
    ("Xiaomi M2101K6G", 30, 33), ("Xiaomi 2201117TG", 31, 33), ("Xiaomi 23021RAAEG", 33, 34),
    ("Xiaomi 23129RAA4G", 33, 35), ("Xiaomi 2312DRA50G", 34, 35), ("Xiaomi 22071212AG", 32, 34),
    ("Google Pixel 8", 34, 35), ("Google Pixel 8 Pro", 34, 35), ("Google Pixel 7", 33, 35),
    ("Google Pixel 7 Pro", 33, 35), ("Google Pixel 6a", 33, 35), ("Google Pixel 6", 32, 35),
    ("OnePlus CPH2449", 33, 35), ("OnePlus CPH2581", 34, 35), ("OnePlus LE2123", 31, 33),
    ("OPPO CPH2371", 33, 34), ("OPPO CPH2557", 34, 35), ("realme RMX3630", 33, 34),
    ("realme RMX3771", 33, 34), ("vivo V2250", 33, 34), ("motorola moto g84 5G", 33, 34),
    ("motorola XT2347-2", 33, 34), ("HONOR RMO-NX1", 33, 34), ("Nothing A063", 33, 35),
]
_ANDROID_OS = {30: "11", 31: "12", 32: "12L", 33: "13", 34: "14", 35: "15"}
_AND_VER = [("10.9.2", 4608), ("10.10.1", 4640), ("10.11.2", 4700), ("10.12.0", 4715), ("10.13.1", 4748),
            ("10.14.5", 4790), ("11.0.2", 4890), ("11.1.3", 4950), ("11.2.2", 5010), ("11.3.3", 5060),
            ("11.4.2", 5110), ("11.5.1", 5150)]
_IOS_OS = ["16.7.8", "17.1.2", "17.2.1", "17.3.1", "17.4.1", "17.5.1", "17.6.1", "17.7", "18.0.1", "18.1.1",
           "18.2.1", "18.3.2", "18.4.1"]
_IOS = [("iPhone 16 Pro Max", 8), ("iPhone 16 Pro", 8), ("iPhone 16", 8), ("iPhone 15 Pro Max", 1),
        ("iPhone 15 Pro", 1), ("iPhone 15 Plus", 1), ("iPhone 15", 1), ("iPhone 14 Pro Max", 0),
        ("iPhone 14 Pro", 0), ("iPhone 14", 0), ("iPhone 13 Pro", 0), ("iPhone 13", 0), ("iPhone 13 mini", 0),
        ("iPhone 12", 0), ("iPhone 12 Pro", 0), ("iPhone 11", 0), ("iPhone SE (3rd generation)", 0),
        ("iPhone XR", 0), ("iPhone XS", 0)]
_IOS_VER = ["10.9.2", "10.10.1", "10.11.2", "10.12.1", "10.13.2", "10.14.1", "11.0.1", "11.1.2", "11.2.3",
            "11.3.1", "11.4.2"]
_WIN_OS = ["Windows 10", "Windows 11"]
_TD_VER = ["4.16.8", "5.0.5", "5.1.7", "5.2.3", "5.3.2", "5.4.1", "5.5.5", "5.6.3", "5.7.3", "5.8.3", "5.9.0",
           "5.10.7", "5.11.1"]
_MAC = ["MacBookPro17,1", "MacBookPro18,3", "MacBookPro18,1", "MacBookPro19,1", "MacBookAir10,1", "Mac14,2",
        "Mac14,5", "Mac15,3", "iMac21,1", "Macmini9,1"]
_MAC_OS = ["macOS 12.7.4", "macOS 13.6.7", "macOS 14.4.1", "macOS 14.5", "macOS 14.6.1", "macOS 15.0.1",
           "macOS 15.2"]
_LINUX_OS = ["Ubuntu 22.04", "Ubuntu 24.04", "Debian GNU/Linux 12", "Fedora Linux 40", "Linux Mint 21.3",
             "Arch Linux", "openSUSE Tumbleweed", "Pop!_OS 22.04"]
_LANGS = [("en", 1.4), ("ru", 1.0), ("es", 1.0), ("it", 0.9), ("fr", 1.0), ("de", 1.0)]
_SYSLANG = {"en": ["en-US", "en-GB", "en-CA", "en-AU"], "ru": ["ru-RU", "ru-UA", "ru-KZ"],
            "es": ["es-ES", "es-MX", "es-AR", "es-CO"], "it": ["it-IT", "it-CH"],
            "fr": ["fr-FR", "fr-CA", "fr-BE"], "de": ["de-DE", "de-AT", "de-CH"]}
_LANG_NAME = {"en": ("English", "انگلیسی"), "ru": ("Russian", "روسی"), "es": ("Spanish", "اسپانیایی"),
              "it": ("Italian", "ایتالیایی"), "fr": ("French", "فرانسوی"), "de": ("German", "آلمانی")}


def _gen_one(rng, plat_w, vary_lang):
    names = list(plat_w)
    plat = rng.choices(names, weights=[plat_w[n] for n in names])[0]
    if plat == "android":
        model, lo, hi = rng.choice(_ANDROID)
        sdk = rng.randint(lo, hi)
        n = len(_AND_VER)
        vn, vc = _AND_VER[rng.choices(range(n), weights=[1 + i for i in range(n)])[0]]
        d = {"device_model": model, "system_version": f"SDK {sdk}", "app_version": f"{vn} ({vc})"}
    elif plat == "ios":
        model, mi = rng.choice(_IOS)
        n = len(_IOS_VER)
        d = {"device_model": model, "system_version": _IOS_OS[rng.randint(mi, len(_IOS_OS) - 1)],
             "app_version": _IOS_VER[rng.choices(range(n), weights=[1 + i for i in range(n)])[0]]}
    elif plat == "windows":
        d = {"device_model": rng.choice(["PC 64bit", "Desktop"]),
             "system_version": rng.choices(_WIN_OS, weights=[40, 60])[0], "app_version": rng.choice(_TD_VER)}
    elif plat == "macos":
        d = {"device_model": rng.choice(_MAC), "system_version": rng.choice(_MAC_OS),
             "app_version": rng.choice(_TD_VER)}
    else:
        d = {"device_model": rng.choice(["PC 64bit", "Desktop"]), "system_version": rng.choice(_LINUX_OS),
             "app_version": rng.choice(_TD_VER)}
    lang = rng.choices([x for x, _w in _LANGS], weights=[w for _x, w in _LANGS])[0]
    d["system_lang_code"] = rng.choice(_SYSLANG[lang])
    d["lang_code"] = lang if vary_lang else "en"
    d["platform"] = plat
    d["id"] = "DV-" + "".join(rng.choice("0123456789ABCDEF") for _ in range(12))
    return d


def _dev_key(d):
    return (d.get("device_model"), d.get("system_version"), d.get("app_version"), d.get("system_lang_code"))


def _gen_unique(rng, plat_w, vary_lang, used):
    d = None
    for _ in range(400):
        d = _gen_one(rng, plat_w, vary_lang)
        if _dev_key(d) not in used:
            return d
    return d                       # the pool is exhausted: accept a repeat (practically never happens)


def _os_label(d):
    p, sv = d.get("platform"), str(d.get("system_version") or "")
    if p == "android":
        m = re.search(r"(\d+)", sv)
        return "Android " + _ANDROID_OS.get(int(m.group(1)) if m else 0, "?")
    if p == "ios":
        return "iOS " + sv
    return sv


def _app_label(d):
    p = d.get("platform")
    return {"android": "Telegram Android", "ios": "Telegram iOS"}.get(p, "Telegram Desktop") + " " + str(d.get("app_version") or "")
# --- DEVICE-GEN END

_DEV: Dict[int, dict] = {}


def _plat_weights() -> dict:
    raw = DEVICE_PLATFORMS.strip().lower()
    if raw:
        w = {k: _PLAT_W[k] for k in (x.strip() for x in raw.split(",")) if k in _PLAT_W}
        if w:
            return w
    if _api_creds()[0] == PUBLIC_API_ID:      # the shared public API id is the official ANDROID app
        return {"android": 1}
    return dict(_PLAT_W)


def _dev_used(exclude: int = 0) -> set:
    used = set()
    try:
        for r in db_exec("SELECT uid, data FROM pg_device").fetchall():
            if int(r["uid"]) == exclude:
                continue
            try:
                used.add(_dev_key(json.loads(r["data"])))
            except Exception as _swallow_e:
                _swallow(_swallow_e)
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    return used


def _new_device(uid: int) -> dict:
    rng = random.SystemRandom()
    d = _gen_unique(rng, _plat_weights(), DEVICE_VARY_APP_LANG, _dev_used(uid))
    d["created"] = time.time()
    try:
        db_exec("INSERT OR REPLACE INTO pg_device (uid, data, ts) VALUES (?,?,?)", (uid, json.dumps(d), time.time()))
    except Exception as e:
        log.warning(f"device save u{uid}: {type(e).__name__}: {e}")
    log.info(f"u{uid:<11} | DEVICE | new identity {d['id']}: {_os_label(d)} · {d['device_model']} · "
             f"{_app_label(d)} · {d['system_lang_code']}")
    return d


def device_of(uid: int) -> dict:
    d = _DEV.get(uid)
    if d is not None:
        return d
    try:
        r = db_exec("SELECT data FROM pg_device WHERE uid=?", (uid,)).fetchone()
        d = json.loads(r["data"]) if r else None
    except Exception:
        d = None
    if not d or any(k not in d for k in ("device_model", "system_version", "app_version", "lang_code", "system_lang_code")):
        d = _new_device(uid)
    _DEV[uid] = d
    return d


def _make_user_client(uid: int) -> TelegramClient:
    """Every user logs in (and reconnects) with HIS OWN device identity."""
    api_id, api_hash = _api_creds()
    d = device_of(uid)
    return TelegramClient(
        StringSession(load_user_session(uid)), api_id, api_hash, proxy=_proxy_tuple(),
        device_model=d["device_model"], system_version=d["system_version"], app_version=d["app_version"],
        lang_code=d["lang_code"], system_lang_code=d["system_lang_code"], **_USER_KW)


def device_public(uid: int) -> dict:
    d = device_of(uid)
    code = str(d.get("system_lang_code") or "en")[:2]
    en, fa = _LANG_NAME.get(code, (code, code))
    return {"id": d.get("id", ""), "platform": _PLAT_LABEL.get(d.get("platform"), "?"), "os": _os_label(d),
            "model": d.get("device_model", ""), "app": _app_label(d), "sys_lang": d.get("system_lang_code", ""),
            "app_lang": d.get("lang_code", ""), "lang_en": en, "lang_fa": fa}


def dev_short(uid: int) -> str:
    d = device_of(uid)
    return f"{_os_label(d)} · {d.get('device_model', '')}"


async def _device_check(uid: int, client: TelegramClient) -> dict:
    """Ask Telegram how it sees THIS session (device, platform, app, country)."""
    if GetAuthorizationsRequest is None:
        return _jerr("telethon too old")
    try:
        async with gsem():
            res = await client(GetAuthorizationsRequest())
    except DEAD_ERRORS:
        await handle_dead(uid)
        return _jerr("session revoked")
    except Exception as e:
        if await absorb(uid, e, "device"):
            return _jerr("flood wait - try again later")
        return _jerr(type(e).__name__)
    auths = list(getattr(res, "authorizations", None) or [])
    cur = next((a for a in auths if getattr(a, "current", False)), None)
    if cur is None:
        return _jerr("the current session was not reported")
    parts = [f"{getattr(cur, 'app_name', '')} {getattr(cur, 'app_version', '')}".strip(),
             getattr(cur, "device_model", ""),
             f"{getattr(cur, 'platform', '')} {getattr(cur, 'system_version', '')}".strip(),
             getattr(cur, "country", "")]
    txt = " · ".join(p for p in parts if p)
    _farm(uid)["dev_seen"] = {"text": txt, "ts": time.time(), "sessions": len(auths)}
    return {"ok": True, "msg": txt}


async def _soft_reconnect(uid: int, c: TelegramClient) -> None:
    try:
        await asyncio.sleep(random.uniform(1, 4))
        await c.disconnect()
        await asyncio.sleep(random.uniform(2, 5))
        await c.connect()
    except Exception as e:
        log.warning(f"soft reconnect u{uid}: {type(e).__name__}: {e}")


async def _device_regen(uid: int) -> dict:
    _DEV.pop(uid, None)
    try:
        db_exec("DELETE FROM pg_device WHERE uid=?", (uid,))
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    d = device_of(uid)
    c = ACCOUNTS.get(uid)
    if c is not None:
        try:
            ir = getattr(c, "_init_request", None)
            if ir is not None:
                ir.device_model, ir.system_version, ir.app_version = d["device_model"], d["system_version"], d["app_version"]
                ir.lang_code, ir.system_lang_code = d["lang_code"], d["system_lang_code"]
                if not _lock(uid, "farm").locked():
                    spawn(_soft_reconnect(uid, c), f"devre-{uid}")
        except Exception as e:
            log.warning(f"device live apply u{uid}: {type(e).__name__}: {e}")
    return {"ok": True, "msg": "new device identity: " + dev_short(uid) + " (fully applied after the next reconnect / restart)"}


_orig_panel_action_39 = panel_action


async def panel_action(role: str, uid: int, d: dict) -> dict:
    act = str(d.get("action", "")).strip()
    if act in ("device_check", "device_new"):
        if not uid:
            return _jerr("no account selected")
        if act == "device_new":
            return await _device_regen(uid)
        c = ACCOUNTS.get(uid)
        if c is None:
            return _jerr("account is offline - log in from Telegram first")
        return await _device_check(uid, c)
    return await _orig_panel_action_39(role, uid, d)


_SUB_ACTIONS["control"] = tuple(_SUB_ACTIONS["control"]) + ("device_check",)
_SUB_ACTIONS["reset"] = tuple(_SUB_ACTIONS["reset"]) + ("device_new",)

_orig_acct_state_39 = acct_state


def acct_state(uid: int) -> dict:
    r = _orig_acct_state_39(uid)
    try:
        r["device"] = device_public(uid)
        seen = (_FARM.get(uid) or {}).get("dev_seen")
        if seen:
            r["device"]["seen"] = seen
    except Exception as e:
        log.warning(f"device state u{uid}: {type(e).__name__}: {e}")
    return r


_orig_admin_state_39 = admin_state


def admin_state() -> dict:
    v = _orig_admin_state_39()
    try:
        for a in v.get("accounts", []):
            if "dev" not in a:
                a["dev"] = dev_short(a["uid"])
    except Exception as e:
        log.warning(f"device column: {type(e).__name__}: {e}")
    return v


# ---------------------------------------------------------------------------------------------
# 2) HUMAN MODE: personality + mood + day type + weekly drift  (all values live inside ranges)
# ---------------------------------------------------------------------------------------------
def _P(uid: int) -> dict:
    p = _persona(uid)
    if "off_p" not in p:
        r = random.Random(f"p39:{uid}:{_web_secret().decode(errors='ignore')[:24]}")
        p.update({
            "off_p": r.uniform(0.02, 0.07), "light_p": r.uniform(0.12, 0.28), "heavy_p": r.uniform(0.08, 0.20),
            "browse_p": r.uniform(0.15, 0.50), "sig": r.uniform(0.45, 0.80),
            "mood_w": [(0.5, r.uniform(1800, 5400), r.uniform(0, 6.28)),
                       (0.3, r.uniform(7000, 16000), r.uniform(0, 6.28)),
                       (0.2, r.uniform(20000, 60000), r.uniform(0, 6.28))],
        })
    return p


def _mood(uid: int) -> float:
    """Smooth slowly-changing noise in [-1, 1]: > 0 = in the mood (quicker), < 0 = sluggish."""
    t = time.time()
    return max(-1.0, min(1.0, sum(a * math.sin(t / T + ph) for a, T, ph in _P(uid)["mood_w"])))


def _wk_factor(uid: int) -> float:
    n = local_now(uid)
    r = random.Random(f"w39:{uid}:{n.toordinal() // 7}")
    return r.uniform(0.88, 1.12)


_DAYP: Dict[int, Tuple[int, dict]] = {}


def _day_profile(uid: int) -> dict:
    n = local_now(uid)
    od = n.toordinal()
    c = _DAYP.get(uid)
    if c and c[0] == od:
        return c[1]
    P = _P(uid)
    r = random.Random(f"d39:{uid}:{od}:{_web_secret().decode(errors='ignore')[:24]}")
    weekend = n.weekday() >= 5
    x = r.random()
    off_p = P["off_p"] * (1.6 if weekend else 1.0)
    light_p = P["light_p"] * (1.3 if weekend else 1.0)
    if x < off_p:
        kind, scale = "quiet", r.uniform(0.10, 0.20)
    elif x < off_p + light_p:
        kind, scale = "light", r.uniform(0.35, 0.65)
    elif x > 1.0 - P["heavy_p"] * (0.6 if weekend else 1.0):
        kind, scale = "busy", r.uniform(1.25, 1.60)
    else:
        kind, scale = "normal", r.uniform(0.85, 1.15)
    prof = {"kind": kind, "scale": scale, "cap_frac": r.uniform(0.2, 0.8)}
    _DAYP[uid] = (od, prof)
    return prof


def _day_scale(uid: int) -> float:
    try:
        if get_cfg(uid).get("day_variety", 1):
            return float(_day_profile(uid)["scale"])
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    return 1.0


def _react(uid: int, med: float = 2.0) -> float:
    v = random.lognormvariate(math.log(med * _P(uid)["react"] * (1.0 - 0.2 * _mood(uid))), 0.55)
    if random.random() < 0.04:
        v += random.uniform(6, 30)
    return min(max(0.7, v), 60.0) * max(1.0, slow_eff(uid) ** 0.5)


def _hc_range(uid: int) -> Tuple[int, int]:
    c = get_cfg(uid)
    lo = max(5, min(300, int(c.get("hc_min") or HOUR_CAP_MIN)))
    hi = max(lo, min(300, int(c.get("hc_max") or HOUR_CAP_MAX)))
    return lo, hi


def _hour_cap(uid: int) -> int:
    """Hourly join limit: random inside the owner's range, centred on a daily value, re-rolled every 30-90 min."""
    f, now = _farm(uid), time.time()
    if now >= f.get("hc_until", 0.0) or now >= f.get("hc_chk", 0.0):
        f["hc_chk"] = now + 30.0
        lo, hi = _hc_range(uid)
        if now >= f.get("hc_until", 0.0) or f.get("hc_rng") != (lo, hi):
            centre = lo + (hi - lo) * _day_profile(uid)["cap_frac"]
            f["hc"] = int(min(hi, max(lo, round(random.gauss(centre, max(1.0, (hi - lo) * 0.22))))))
            f["hc_rng"] = (lo, hi)
            f["hc_until"] = now + random.uniform(1800, 5400)
            log.info(f"u{uid:<11} | PACE   | hourly pace for the next while: {f['hc']}/h  (range {lo}-{hi})")
    return int(f["hc"])


def _human_gap(uid: int) -> float:
    hl, _dl, gi = eff_limits(uid)
    P, f = _P(uid), _farm(uid)
    sc = _day_scale(uid)
    speed = P["speed"] * _wk_factor(uid) * (1.0 + 0.18 * _mood(uid)) * float(f.get("pace_k", 1.0))
    speed *= max(0.5, min(1.4, sc ** 0.5))
    rate_gap = 3600.0 / max(1, hl) * 0.85 * night_mult(uid) * slow_eff(uid) / max(0.3, speed)
    mean = max(gi, rate_gap)
    sig = P["sig"]
    g = mean * random.lognormvariate(-(sig ** 2) / 2.0, sig)     # unit-mean log-normal: irregular every time
    r = random.random()
    top = max(90.0, mean * 7.0)
    if r < 0.05:
        g += random.uniform(15, 90)                              # got distracted
    elif r < 0.075:
        g += random.uniform(120, 600)                            # phone call / coffee
        top = 760.0
    elif r < 0.12:
        g *= random.uniform(0.35, 0.60)                          # quick streak
    return min(max(HARD_MIN_GAP * 1.3, g), top)


def _begin_break(uid: int, f: dict, now: float) -> float:
    P, act, sc = _P(uid), _activity(uid), _day_scale(uid)
    ln = random.lognormvariate(math.log(P["brk"] * (1.6 - 0.9 * act) / max(0.35, min(1.6, sc))), 0.6)
    if random.random() < 0.08:
        ln = random.uniform(25, 70)                               # went away for a while
    ln = min(max(1.5, ln), 120.0)
    f["rest_until"], f["burst_end"] = now + ln * 60.0, 0.0
    _WORKING.pop(uid, None)
    return f["rest_until"] - now


def _begin_burst(uid: int, f: dict, cfg: dict, now: float) -> float:
    P = _P(uid)
    if cfg.get("spread", 1):
        nrun = _n_running()
        if nrun > 2:
            live = _working_live(now)
            lim = max(1, int(round(nrun * _global_frac(now))))
            if uid not in live and len(live) >= lim:
                w = random.uniform(60, 360)
                f["rest_until"] = now + w
                return w
    sc = _day_scale(uid)
    ln = random.lognormvariate(math.log(P["work"] * (0.6 + 0.8 * _activity(uid)) * max(0.12, sc)
                                        * (1.0 + 0.25 * _mood(uid))), 0.5)
    ln = min(max(3.0, ln), 170.0)
    f["burst_end"] = now + ln * 60.0
    f["pace_k"] = random.choice((0.7, 0.85, 1.0, 1.0, 1.15, 1.35))   # every burst has its own tempo
    src = "tasks"                                                     # what this burst earns from
    if cfg.get("do_views", 0):
        if not (cfg.get("do_channels", 1) or cfg.get("do_groups", 1)):
            src = "views"
        elif random.random() < 0.4:
            src = "views"
    f["burst_src"] = src
    _WORKING[uid] = f["burst_end"]
    return 0.0


def prof_label(uid: int, name: str) -> str:
    return L(uid, "🧠 Human", "🧠 انسانی")


# sometimes "/start" instead of "Earnings" (both open the category menu)
async def request_tasks(uid: int, client: TelegramClient) -> bool:
    try:
        _farm(uid)["hops"] = 0
        txt = str(CONFIG["pg_earn_text"])
        if random.random() < 0.08:
            txt = "/start"
        async with gsem():
            await client.send_message(pg_bot_name(), txt)
        evt(uid, "FETCH", f"sent '{txt}' to @{pg_bot_name()}")
        return True
    except DEAD_ERRORS:
        raise
    except Exception as e:
        if not await absorb(uid, e, "fetch"):
            stat_add(uid, errors=1)
            stat_note(uid, f"fetch failed: {type(e).__name__}")
        return False


# ---------------------------------------------------------------------------------------------
# 3) presence, peeking at the chat list, looking at posts after a join
# ---------------------------------------------------------------------------------------------
async def _set_online(uid: int, online: bool) -> None:
    f = _farm(uid)
    try:
        c = ACCOUNTS.get(uid)
        if c is None or UpdateStatusRequest is None:
            return
        await asyncio.sleep(random.uniform(1, 25) if online else random.uniform(3, 70))
        async with gsem():
            await c(UpdateStatusRequest(offline=not online))
        f["online"], f["online_ts"] = online, time.time()
    except DEAD_ERRORS:
        await handle_dead(uid)
    except Exception as e:
        await absorb(uid, e, "presence")
    finally:
        f["pres_busy"] = False


async def _peek_dialogs(uid: int, client: TelegramClient) -> None:
    try:
        async with gsem():
            await client.get_dialogs(limit=random.randint(8, 30))
    except DEAD_ERRORS:
        await handle_dead(uid)
    except Exception as e:
        await absorb(uid, e, "peek")


async def _browse(uid: int, ref: str, kind: str, peer_id: int) -> None:
    await asyncio.sleep(random.uniform(15, 200))
    c, f = ACCOUNTS.get(uid), _farm(uid)
    if c is None or is_paused(uid) or time.time() < f["flood_until"]:
        return
    try:
        ent = None
        if peer_id:
            try:
                ent = await c.get_input_entity(peer_id)
            except Exception:
                ent = None
        if ent is None and kind == "public" and ref:
            async with gsem():
                ent = await c.get_input_entity(ref)
        if ent is None:
            return
        async with gsem():
            msgs = await c.get_messages(ent, limit=random.randint(3, 9))
        ids = [m.id for m in (msgs or []) if getattr(m, "id", None)]
        if ids and GetMessagesViewsRequest is not None:
            try:
                await c(GetMessagesViewsRequest(peer=ent, id=ids[:random.randint(2, 6)], increment=True))
            except Exception as _swallow_e:
                _swallow(_swallow_e)
        evt(uid, "BROWSE", f"looked at {len(ids)} posts")
    except DEAD_ERRORS:
        await handle_dead(uid)
    except Exception as e:
        await absorb(uid, e, "browse")


_orig_joins_record_39 = joins_record


def joins_record(uid: int, ref: str, title: str, peer_id: int, kind: str = "") -> None:
    _orig_joins_record_39(uid, ref, title, peer_id, kind)
    try:
        if get_cfg(uid).get("browse", 1) and random.random() < _P(uid)["browse_p"]:
            f = _farm(uid)
            if time.time() - f.get("browse_ts", 0.0) > 120:
                f["browse_ts"] = time.time()
                spawn(_browse(uid, ref, kind, peer_id), f"browse-{uid}")
    except Exception as _swallow_e:
        _swallow(_swallow_e)


async def _presence_tick(uid: int, client: TelegramClient) -> None:
    cfg, f, now = get_cfg(uid), _farm(uid), time.time()
    running = bool(cfg["auto_join"] and cfg["auto_fetch"] and not cfg["paused"])
    working = running and now >= f.get("rest_until", 0.0) and f.get("burst_end", 0.0) > now and sched_wait(uid) <= 0
    if cfg.get("presence", 1) and not f.get("pres_busy"):
        if working:
            if not f.get("online") or now - f.get("online_ts", 0.0) > f.get("pres_every", 400.0):
                f["pres_busy"], f["pres_every"] = True, random.uniform(240, 560)
                spawn(_set_online(uid, True), f"online-{uid}")
        elif f.get("online"):
            f["pres_busy"] = True
            spawn(_set_online(uid, False), f"offline-{uid}")
    if working and now >= f.get("dlg_at", 0.0):
        if f.get("dlg_at"):
            spawn(_peek_dialogs(uid, client), f"peek-{uid}")
        f["dlg_at"] = now + random.uniform(1500, 5400)


_orig_farm_tick_39 = _orig_farm_tick        # the 3.8 tick; the 3.5 farm_tick wrapper resolves _orig_farm_tick by name


async def _farm_tick_39(uid: int, client: TelegramClient) -> None:
    try:
        await _presence_tick(uid, client)
    except DEAD_ERRORS:
        raise
    except Exception as e:
        log.warning(f"presence tick u{uid}: {type(e).__name__}: {e}")
    try:
        cfg, f, now = get_cfg(uid), _farm(uid), time.time()
        if _views_forced(uid) and cfg["auto_join"] and cfg["auto_fetch"] and not cfg["paused"]:
            smart_rest(uid)                                      # keeps start delay / bursts / breaks / sleep going
            if cfg["auto_leave"] and now - f.get("leave_ts", 0.0) > 900 and not _lock(uid, "leave").locked():
                f["leave_ts"] = now
                c = ACCOUNTS.get(uid)
                if c is not None and joins_older_than(uid, leave_after_days(uid) * 86400):
                    spawn(leave_old(uid, c, True), f"autoleave-{uid}")
            return                                               # no channel / group fetching during views
    except DEAD_ERRORS:
        raise
    except Exception as e:
        log.warning(f"views source tick u{uid}: {type(e).__name__}: {e}")
    await _orig_farm_tick_39(uid, client)


_orig_farm_tick = _farm_tick_39



# ---------------------------------------------------------------------------------------------
# 3b) SOURCES: Channels / Groups / Views toggles (Views can now be a normal source, not only a flood fallback)
# ---------------------------------------------------------------------------------------------
def _views_forced(uid: int) -> bool:
    """True while this account should earn from Views: views-only, or a 'views' burst of a mixed account."""
    try:
        c = get_cfg(uid)
        if not c.get("do_views", 0):
            return False
        if not (c.get("do_channels", 1) or c.get("do_groups", 1)):
            return True
        return _farm(uid).get("burst_src") == "views"
    except Exception:
        return False


def _vw_gap(uid: int) -> float:
    base = max(VIEWS_HARD_MIN_GAP, float(get_cfg(uid).get("views_gap") or 6.0))
    g = base * random.lognormvariate(-0.18, 0.6) * night_mult(uid)
    g /= max(0.5, _P(uid)["speed"] * (1.0 + 0.18 * _mood(uid)))
    r = random.random()
    if r < 0.06:
        g += random.uniform(6, 25)
    elif r < 0.08:
        g += random.uniform(40, 200)
    return min(max(VIEWS_HARD_MIN_GAP, g), 400.0)


async def _views_tick_inner(uid: int, client: TelegramClient) -> None:
    cfg, f, vw, now = get_cfg(uid), _farm(uid), _vw(uid), time.time()
    left = f["flood_until"] - now
    force = _views_forced(uid)
    calm = sched_wait(uid) <= 0 and now >= f.get("rest_until", 0.0)       # respect active hours / breaks
    ok = bool((cfg["views_on"] or force) and cfg["auto_join"] and cfg["auto_fetch"] and not cfg["paused"]
              and calm and now >= vw["hold_until"] and uid in ACCOUNTS)
    want = ok and (force or ((left >= VIEWS_MIN_FLOOD) if not vw["on"] else (left > 0)))

    if want and not vw["on"]:                      # ---- enter views mode
        vw.update(on=True, bot=None, stage="start", cur=None, n=0, gram=0.0, stalls=0, started=False,
                  retry_at=0.0, last_evt=now)
        vw["gen"] += 1
        stat_note(uid, f"{_VW_EM} Views" if force else f"{_VW_EM} join flood -> Views mode")
        evt(uid, "VIEWS", "on (views source)" if force else f"on (flood {int(left)}s)")
        if cfg["notify"] and not force:
            main = h(pg_bot_name())
            await warn_once(uid, "views_on", L(
                uid,
                f"{_VW_EM} <b>Join flood → Views mode</b>\n<blockquote>⏳ Telegram blocked joining for <b>{_fmt_dur(left)}</b>.\n"
                f"Instead of idling I earn from <b>Views</b> on @{main}"
                + ("; if it rate-limits me I move to the mirror bot" if cfg["views_mirror"] else "")
                + ".\nWhen the flood ends I return to channel tasks by myself.</blockquote>",
                f"{_VW_EM} <b>فلود عضویت → حالت ویو</b>\n<blockquote>⏳ تلگرام عضویت رو برای <b>{_fmt_dur(left)}</b> بلاک کرده.\n"
                f"به‌جای بیکار ماندن از بخش <b>ویو</b> روی @{main} درآمد می‌گیرم"
                + ("؛ اگه محدودم کنه می‌رم سراغ ربات آینه" if cfg["views_mirror"] else "")
                + ".\nبعد از پایان فلود خودم برمی‌گردم به تسک‌های کانال.</blockquote>"), 600)

    if not want:                                   # ---- leave views mode
        if vw["on"]:
            vw.update(on=False, bot=None, stage="idle", cur=None)
            vw["gen"] += 1
            evt(uid, "VIEWS", f"off ({vw['n']} posts, +{vw['gram']:g} GRAM)")
            if left <= 0 and vw["n"] > 0 and cfg["notify"] and not force:
                await notify(uid, L(
                    uid,
                    f"✅ <b>Flood over</b> — back to channel tasks.\n{_VW_EM} This round: <b>{vw['n']}</b> posts · <b>+{vw['gram']:,.0f}</b> GRAM",
                    f"✅ <b>فلود تموم شد</b> — برگشتم به تسک‌های کانال.\n{_VW_EM} این دور: <b>{vw['n']}</b> پست · <b>+{vw['gram']:,.0f}</b> GRAM"))
        return

    if now < vw.get("retry_at", 0.0):
        return
    if vw["bot"] is None or not _vw_free(uid, vw["bot"], now):
        pick = _vw_pick(uid)
        if pick is None:                           # both bots rate-limited: wait
            vw["bot"] = None
            return
        if pick != vw["bot"]:
            vw.update(bot=pick, stage="start", started=False, stalls=0, cur=None)
            vw["gen"] += 1
            stat_note(uid, f"{_VW_EM} using @{_vw_name(uid, pick)}")
    if vw["stage"] == "start":
        await _vw_start(uid, client)
    elif now - vw["last_evt"] > min(300.0, 40.0 + 30.0 * vw["stalls"]):     # the bot went quiet
        vw["stalls"] += 1
        vw["gen"] += 1
        vw["cur"] = None
        if vw["stalls"] >= 6:                      # this bot does not answer: park it, try the other one
            vw[f"{vw['bot']}_until"] = now + 600
            vw.update(bot=None, stalls=0)
            stat_note(uid, f"{_VW_EM} bot silent - parked 10 min")
        else:
            vw["stage"] = "start"
            stat_note(uid, f"{_VW_EM} no reply - restarting")


# while Views is the source, the category menu belongs to the views engine
_orig_handle_menu_39 = handle_menu


async def handle_menu(uid: int, client: TelegramClient, msg) -> None:
    vw = (_FARM.get(uid) or {}).get("vw")
    if vw and vw.get("on"):
        return
    await _orig_handle_menu_39(uid, client, msg)


# ---------------------------------------------------------------------------------------------
# 4) BULK: apply settings to many users at once
# ---------------------------------------------------------------------------------------------
BULK_GROUPS = {
    "sources": ("do_channels", "do_groups", "do_views"),
    "human": ("do_channels", "do_groups", "spread", "auto_sleep", "day_variety", "presence", "browse", "idle_stop"),
    "range": ("hc_min", "hc_max"),
    "rhythm": ("night_mode", "night_start", "night_end", "night_factor"),
    "schedule": ("active_on", "active_start", "active_end", "tz_min"),
    "views": ("views_on", "views_mirror", "views_gap"),
    "cleanup": ("auto_leave", "leave_days", "clean_gap"),
    "safety": ("flood_stop",),
    "messages": ("lang", "notify"),
}


def _default_values() -> dict:
    d = dict(_CFG_DEFAULTS)
    for k, (_t, v) in EXTRA_COLS.items():
        d[k] = v
    return d


def _bulk_targets(role: str, wuid: int) -> List[int]:
    ids = set(ACCOUNTS) | set(list_stored_users())
    try:
        ids |= {int(r["id"]) for r in db_exec("SELECT id FROM users").fetchall()}
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    if role == "sub":
        st = staff_perms(wuid)
        if st is None:
            return []
        _p, scope = st
        ids = {u for u in ids if not _is_staff_uid(u) and _in_scope(scope, u)}
    return sorted(ids)


async def _bulk_apply(role: str, wuid: int, data: dict) -> dict:
    scope: Any = "all"
    if role == "sub":
        st = staff_perms(wuid)
        if st is None:
            return _jerr("unauthorized")
        perms, scope = st
        if perms.get("settings", 0) < 2:
            return _jerr("no permission: full settings")
    groups = [g for g in (data.get("groups") or []) if g in BULK_GROUPS]
    if not groups:
        return _jerr("pick at least one group")
    keys = [k for g in groups for k in BULK_GROUPS[g] if k in SETTINGS_SPEC]
    try:
        src = int(data.get("src") or 0)
    except Exception:
        src = 0
    if src:
        if role == "sub":
            err = _sub_target_err(wuid, scope, src)
            if err:
                return _jerr(err)
        if src not in ACCOUNTS and not user_known(src):
            return _jerr("unknown source account")
        base = get_cfg(src)
    else:
        base = _default_values()
    values = {k: base[k] for k in keys if k in base}
    n = 0
    for i, u in enumerate([x for x in _bulk_targets(role, wuid) if x != src]):
        apply_settings(u, values)
        n += 1
        if i % 10 == 9:
            await asyncio.sleep(0)
    evt(owner_id() or 0, "BULK", f"{'+'.join(groups)} -> {n} accounts (from {src or 'defaults'}) by {role} {wuid}")
    return {"ok": True, "count": n, "keys": len(values)}


_orig_web_api_39 = web_api


async def web_api(role: str, wuid: int, path: str, qs: Dict[str, List[str]], data: dict) -> dict:
    if path == "/api/bulk":
        if role == "user":
            return _jerr("admin only")
        return await _bulk_apply(role, wuid, data or {})
    if role == "user" and path == "/api/settings" and isinstance(data, dict) and isinstance(data.get("values"), dict):
        data = dict(data)       # users may never touch the owner's hourly range
        data["values"] = {k: v for k, v in data["values"].items() if k not in ("hc_min", "hc_max")}
    return await _orig_web_api_39(role, wuid, path, qs, data)


# ---------------------------------------------------------------------------------------------
# 5) DASHBOARD
# ---------------------------------------------------------------------------------------------
_UI_SECTION_38 = r"""{id:'sources',ic:'layers',en:'What to earn from',fa:'از چی درآمد بگیره',f:[
 {k:'do_channels',t:'sw',en:'Channels',fa:'کانال‌ها',he:'Join channels for GRAM. Turn it off together with Groups to work on Views only.',hf:'برای GRAM عضو کانال می‌شه. اگه همراه گروه‌ها خاموشش کنی فقط ویو می‌زنه.'},
 {k:'do_groups',t:'sw',en:'Groups',fa:'گروه‌ها',he:'Join groups for GRAM. Turn it off together with Channels to work on Views only.',hf:'برای GRAM عضو گروه می‌شه. اگه همراه کانال‌ها خاموشش کنی فقط ویو می‌زنه.'},
 {k:'do_views',t:'sw',en:'Views',fa:'ویو',he:'View posts for GRAM. Turn Channels and Groups off to make this account do Views only. With several on, the account switches between them burst by burst.',hf:'پست‌ها رو می‌بینه و GRAM می‌گیره. کانال‌ها و گروه‌ها رو خاموش کنی فقط ویو می‌زنه؛ اگه چند تا روشن باشه، دور به دور بینشون عوض می‌کنه.'}]},
{id:'human',ic:'bot',en:'Human behaviour',fa:'رفتار انسانی',f:[
 {k:'spread',t:'sw',en:'Spread my accounts apart',fa:'پخش‌کردن اکانت‌ها از هم',he:'Every account gets its own rhythm: staggered start, different work and break lengths, and only a part of the running accounts works at the same moment.',hf:'هر اکانت ریتم خودش رو داره: شروع نامنظم، کار و استراحت متفاوت و فقط بخشی از اکانت‌های فعال هم‌زمان کار می‌کنن.'},
 {k:'auto_sleep',t:'sw',en:'Daily sleep',fa:'خواب روزانه',he:'Each account sleeps a few hours per day at its own random time, like a person.',hf:'هر اکانت هر روز چند ساعت توی زمان تصادفی خودش می‌خوابه، مثل یه آدم.'},
 {k:'day_variety',t:'sw',en:'Vary the days',fa:'روزها با هم فرق داشته باشن',he:'Quiet, light, normal and busy days at random, a bit calmer at weekends. No two days look alike.',hf:'روز خیلی کم‌کار، کم‌کار، معمولی و پرکار به‌صورت تصادفی و آخر هفته کمی آرام‌تر. هیچ دو روزی شبیه هم نیست.'},
 {k:'presence',t:'sw',en:'Online / offline like a person',fa:'آنلاین و آفلاین شدن مثل آدم',he:'The account shows online while it works and goes offline during breaks.',hf:'اکانت وقتی کار می‌کنه آنلاین می‌شه و توی استراحت آفلاین.'},
 {k:'browse',t:'sw',en:'Look at a few posts after joining',fa:'بعد از عضویت چند پست رو ببین',he:'Sometimes opens a freshly joined channel and views a few posts, like a real member.',hf:'گاهی کانال تازه‌عضوشده رو باز می‌کنه و چند پست رو می‌بینه، مثل یه عضو واقعی.'},
 {k:'idle_stop',t:'sw',en:'Stop automatically when no tasks are left',fa:'توقف خودکار وقتی تسکی نمونده',he:'If PR GRAM has nothing left to do for a while, the account switches itself off and you get a message.',hf:'اگه مدتی تسکی نباشه اکانت خودش خاموش می‌شه و بهت پیام میاد.'},
 {k:'hc_min',t:'rng',min:10,max:300,step:1,unit:'/h',lim:1,own:1,en:'Hourly joins - lowest',fa:'عضویت ساعتی - حداقل',he:'The code picks a random hourly limit between this and the highest value, and changes it every 30-90 minutes.',hf:'کد یه سقف ساعتی تصادفی بین این عدد و حداکثر انتخاب می‌کنه و هر ۳۰ تا ۹۰ دقیقه عوضش می‌کنه.'},
 {k:'hc_max',t:'rng',min:10,max:300,step:1,unit:'/h',lim:1,own:1,en:'Hourly joins - highest',fa:'عضویت ساعتی - حداکثر',he:'Upper end of the random hourly limit.',hf:'سقف بالای بازه تصادفی عضویت ساعتی.'}]},
"""

_UI_JS_39 = r"""
/* ================= bulk settings + device card (patch 3.9) ================= */
const BK={src:0,sel:{sources:0,human:1,range:0,rhythm:1,schedule:0,views:0,cleanup:0,safety:0,messages:0}};
const BK_G=[['sources','What to earn from','نوع درآمد'],['human','Human behaviour','رفتار انسانی'],['range','Hourly range','بازه ساعتی'],['rhythm','Night rhythm','ریتم شب'],['schedule','Active hours','ساعت کار'],['views','Views mode','حالت ویو'],['cleanup','Cleanup','پاکسازی'],['safety','Safety','ایمنی'],['messages','Telegram messages','پیام‌های تلگرام']];
function drawBulk(){const el=$('#ac-bulkcard');if(!el)return;const A=S.adm;
 if(!A||!fullSet()){setHtml(el,'');return}
 const n=A.accounts.length;
 const opts='<option value="0"'+(BK.src===0?' selected':'')+'>'+tr('Human defaults','پیش‌فرض انسانی')+'</option>'+A.accounts.map(a=>'<option value="'+a.uid+'"'+(BK.src===a.uid?' selected':'')+'>'+esc(a.name||a.uid)+' · '+a.uid+'</option>').join('');
 const chips=BK_G.map(g=>'<label class="tag"><input type="checkbox" data-bkg="'+g[0]+'"'+(BK.sel[g[0]]?' checked':'')+'> '+tr(g[1],g[2])+'</label>').join('');
 setHtml(el,'<div class="card"><h3>'+ic('users')+tr('Apply settings to all users','اعمال تنظیمات روی همه کاربران')+'<small>'+n+' '+tr('accounts','اکانت')+'</small></h3>'+
  '<p style="color:var(--mut);margin-top:0">'+tr('Pick the groups of settings and where the values come from. They overwrite the current values of every user.','گروه‌های تنظیمات و منبع مقدارها رو انتخاب کن. مقدارهای فعلی همهٔ کاربرها بازنویسی می‌شه.')+'</p>'+
  '<div class="row" style="margin-bottom:10px"><span>'+tr('Values from','مقدارها از')+'</span><select id="bk-src">'+opts+'</select></div>'+
  '<div class="row" style="margin-bottom:12px">'+chips+'</div>'+
  '<div class="row"><button class="btn pri" data-act="bk-apply"'+(n?'':' disabled')+'>'+ic('zap')+tr('Apply to all','اعمال روی همه')+'</button></div></div>')}
document.addEventListener('change',e=>{const t=e.target;
 if(t.id==='bk-src'){BK.src=+t.value;drawBulk();return}
 if(t.dataset&&t.dataset.bkg){BK.sel[t.dataset.bkg]=t.checked?1:0;drawBulk()}});
document.addEventListener('click',async e=>{const t=e.target.closest('[data-act="bk-apply"]');if(!t)return;
 const groups=Object.keys(BK.sel).filter(k=>BK.sel[k]);
 if(!groups.length)return toast(tr('Pick at least one group','حداقل یک گروه رو انتخاب کن'),1);
 const n=S.adm?S.adm.accounts.length:0;
 if(!await confirmBox({title:tr('Apply to '+n+' accounts?','اعمال روی '+n+' اکانت؟'),body:tr('Their current values for the selected groups will be overwritten.','مقدارهای فعلی گروه‌های انتخاب‌شده بازنویسی می‌شن.'),danger:1,word:'APPLY'}))return;
 t.classList.add('busy');
 try{const r=await api('/api/bulk',{src:BK.src,groups:groups},{uid:0});
  if(r.ok){toast(tr('Applied to ','اعمال شد روی ')+r.count+tr(' accounts',' اکانت'));if(S.role==='admin')loadAdmin()}else toast(r.error||'error',1)}catch(_){}
 finally{t.classList.remove('busy')}});
function devCard(s){const d=s.device;if(!d)return '';
 let h='<div class="card"><h3>'+ic('bot')+tr('Logged-in device','دستگاه واردشده')+'<small>'+esc(d.id)+'</small></h3>'+
  '<div class="row" style="margin-bottom:8px"><span class="tag ok">'+esc(d.platform)+'</span><span class="tag">'+esc(d.os)+'</span><span class="tag">'+esc(d.model)+'</span><span class="tag">'+esc(d.app)+'</span><span class="tag vio">'+esc(tr(d.lang_en,d.lang_fa))+' · '+esc(d.sys_lang)+'</span></div>';
 if(d.seen)h+='<div class="pen"><span>'+tr('Telegram reports','تلگرام گزارش می‌ده')+'</span><b style="color:var(--txt)">'+esc(d.seen.text)+'</b></div>';
 if(can('reset'))h+='<div class="row" style="margin-top:10px"><button class="btn sm" data-act="device_check">'+ic('refresh')+tr('Check at Telegram','بررسی در تلگرام')+'</button><button class="btn sm" data-act="device_new">'+ic('zap')+tr('New device identity','هویت دستگاه جدید')+'</button></div>';
 return h+'</div>'}
"""

_UI_CASES_39 = (
    "case'reset_warmup':doAct(act);break;"
    "case'device_check':busy(()=>doAct(act));break;"
    "case'device_new':if(await confirmBox({title:tr('New device identity?','هویت دستگاه جدید؟'),"
    "body:tr('A different device model, OS and app version will be used from the next reconnect.','از اتصال بعدی یه مدل دستگاه، سیستم‌عامل و نسخه اپ متفاوت استفاده می‌شه.'),danger:1}))doAct(act);break;"
)

_UI39: Dict[str, Any] = {"src": None, "out": None}
_UI39_WARNED: Dict[str, int] = {}


def _rep39(h: str, old: str, new: str) -> str:
    if h.count(old) == 1:
        return h.replace(old, new)
    if not _UI39_WARNED.get(old):
        _UI39_WARNED[old] = 1
        log.error(f"patch 3.9: dashboard anchor not found, that part of the UI is skipped: {old[:55]!r}")
    return h


_orig_ui_tpl_39 = _ui_tpl


def _ui_tpl() -> str:
    h = _orig_ui_tpl_39()                           # 3.8 wrapper: injects the "Human behaviour" section
    if h is _UI39["src"]:
        return _UI39["out"]
    src = h
    h = _rep39(h, "smart:['Smart','هوشمند']", "smart:['Human','انسانی']")
    h = _rep39(h, "s.f.filter(f=>fullSet()||!f.lim)", "s.f.filter(f=>(fullSet()||!f.lim)&&(!f.own||S.role!=='user'))")
    # (presets are removed directly in panel_ui.html)
    h = _rep39(h, "case'reset_warmup':doAct(act);break;", _UI_CASES_39)
    h = _rep39(h, "function ovHTML(s,hist){", "function devCard(s){return ''}\nfunction ovHTML(s,hist){")
    h = _rep39(h, "+ev+accb+'</div>'}", "+ev+accb+'</div>'+devCard(s)}")
    h = _rep39(h, "th('speed_pct',tr('Speed','سرعت'))", "th('dev',tr('Device','دستگاه'))+th('speed_pct',tr('Speed','سرعت'))")
    h = _rep39(h, "'</td><td>'+a.speed_pct+'%</td>'",
               "'</td><td><small style=\"color:var(--mut)\">'+esc(a.dev||'—')+'</small></td><td>'+a.speed_pct+'%</td>'")
    h = _rep39(h, '<div id="ac-access"></div>', '<div id="ac-access"></div><div id="ac-bulkcard"></div>')
    h = _rep39(h, "function drawAcc(){const A=S.adm;if(!A){return}", "function drawAcc(){const A=S.adm;if(!A){return}drawBulk();")
    h = _rep39(h, "/* ================= pages ================= */",
               _UI_JS_39.replace("function devCard(s){", "function devCard2(s){") + "\n/* ================= pages ================= */")
    # the real device card replaces the placeholder defined above
    h = h.replace("function devCard(s){return ''}", "function devCard(s){return devCard2(s)}")
    _UI39["src"], _UI39["out"] = src, h
    return h


log.info("patch 3.9 loaded: human mode (ranges + mood + day types), per-user device identities, bulk settings")
# <<<<< PATCH 3.9 END <<<<<


# ===========================================================================
# [patch-hardening:http]  CSP nonce, Permissions-Policy, /metrics
# ===========================================================================
import secrets as _secrets_h

_START_TS_H = time.time()
_ON_HANDLER_RE = re.compile(rb"\son[a-z]+\s*=", re.I)


async def _send_hardened(self, writer, status: int, ctype: str, body: bytes) -> None:
    reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 413: "Too Large",
               429: "Too Many Requests", 500: "Server Error"}
    script_src = "'unsafe-inline'"
    if ctype.startswith("text/html") and _env_bool("CSP_NONCE", True):
        try:
            if b"<script>" in body and not _ON_HANDLER_RE.search(body):
                nonce = _secrets_h.token_urlsafe(16)
                body = body.replace(b"<script>", b'<script nonce="' + nonce.encode() + b'">')
                script_src = f"'nonce-{nonce}'"
        except Exception as _swallow_e:
            _swallow(_swallow_e)
    head = (f"HTTP/1.1 {status} {reasons.get(status, '')}\r\n"
            f"Content-Type: {ctype}; charset=utf-8\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
            "Referrer-Policy: no-referrer\r\n"
            "Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()\r\n"
            f"Content-Security-Policy: default-src 'self'; style-src 'unsafe-inline'; script-src {script_src}; "
            "img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'\r\n\r\n").encode("latin1")
    writer.write(head + body)
    await writer.drain()


WebPanel._send = _send_hardened


def _metrics_text() -> str:
    L = []

    def g(name, val, help_=""):
        if help_:
            L.append(f"# HELP {name} {help_}")
        L.append(f"{name} {val}")

    g("prgram_uptime_seconds", int(time.time() - _START_TS_H), "process uptime")
    try:
        g("prgram_users_total", users_count(), "known users")
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    try:
        g("prgram_asyncio_tasks", len(asyncio.all_tasks()), "running asyncio tasks")
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    try:
        g("prgram_db_bytes", DB_FILE.stat().st_size, "sqlite file size")
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    g("prgram_swallowed_exceptions_total", sum(_SWALLOWED.values()), "exceptions caught by silent except blocks")
    for k, v in sorted(_SWALLOWED.items(), key=lambda kv: -kv[1])[:20]:
        lab = k.replace("\\", "\\\\").replace('"', "'")
        L.append(f'prgram_swallowed_exceptions{{site="{lab}"}} {v}')
    return "\n".join(L) + "\n"


_orig_route_hard = WebPanel._route


async def _route_hard(self, method, path, headers, body, writer, ip):
    from urllib.parse import urlparse
    if method == "GET" and urlparse(path).path == "/metrics":
        if self._blocked(ip):
            await self._send(writer, 429, "text/plain", b"too many attempts")
            return
        tok = (headers.get("x-token") or "").strip()
        if not tok or not hmac.compare_digest(tok.encode(), self.token.encode()):
            self._fail(ip)
            await self._send(writer, 401, "text/plain", b"unauthorized")
            return
        await self._send(writer, 200, "text/plain", _metrics_text().encode("utf-8"))
        return
    await _orig_route_hard(self, method, path, headers, body, writer, ip)


WebPanel._route = _route_hard


# ===========================================================================
# [patch-ui:v5]  gzip, self-hosted font, PWA manifest + icon, incremental logs
# ===========================================================================
import gzip as _gzip_ui
import contextvars as _cv_ui

_AE_UI = _cv_ui.ContextVar("ae_ui", default="")
_ICON_SVG = (b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='14' fill='#0d0d0d'/>"
             b"<circle cx='32' cy='32' r='15' fill='none' stroke='#fff' stroke-width='5'/><circle cx='32' cy='32' r='5' fill='#fff'/></svg>")
_MANIFEST = json.dumps({
    "name": "PR GRAM", "short_name": "PR GRAM", "start_url": "/", "display": "standalone",
    "background_color": "#ffffff", "theme_color": "#ffffff",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
}).encode("utf-8")


class _GzWriter:
    """Wraps the StreamWriter: gzips the body of text/json responses when the client accepts it."""
    def __init__(self, w):
        self._w = w

    def write(self, data):
        try:
            i = data.find(b"\r\n\r\n")
            head, body = data[:i], data[i + 4:]
            if i > 0 and len(body) >= 1024 and (b"text/html" in head or b"application/json" in head or b"text/plain" in head):
                z = _gzip_ui.compress(body, 4)
                head = re.sub(rb"Content-Length: \d+", b"Content-Length: " + str(len(z)).encode(), head)
                data = head + b"\r\nContent-Encoding: gzip\r\nVary: Accept-Encoding\r\n\r\n" + z
        except Exception as _swallow_e:
            _swallow(_swallow_e)
        self._w.write(data)

    def __getattr__(self, name):
        return getattr(self._w, name)


_orig_send_ui = WebPanel._send


async def _send_ui(self, writer, status, ctype, body):
    if "gzip" in (_AE_UI.get() or "").lower():
        writer = _GzWriter(writer)
    await _orig_send_ui(self, writer, status, ctype, body)


WebPanel._send = _send_ui

_orig_route_ui = WebPanel._route


async def _route_ui(self, method, path, headers, body, writer, ip):
    from urllib.parse import urlparse
    _AE_UI.set(headers.get("accept-encoding", ""))
    p = urlparse(path).path
    if method == "GET" and p in ("/vazirmatn.woff2", "/manifest.webmanifest", "/icon.svg"):
        if p == "/vazirmatn.woff2":
            fp = BASE_DIR / "vazirmatn.woff2"
            if not fp.exists():
                await self._send(writer, 404, "text/plain", b"not found")
                return
            data, ct = fp.read_bytes(), "font/woff2"
        elif p == "/icon.svg":
            data, ct = _ICON_SVG, "image/svg+xml"
        else:
            data, ct = _MANIFEST, "application/manifest+json"
        head = (f"HTTP/1.1 200 OK\r\nContent-Type: {ct}\r\nContent-Length: {len(data)}\r\n"
                "Cache-Control: public, max-age=604800\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n").encode("latin1")
        writer.write(head + data)
        await writer.drain()
        return
    await _orig_route_ui(self, method, path, headers, body, writer, ip)


WebPanel._route = _route_ui



# ===========================================================================
# [patch-backup]  Session backup / restore (owner only)
#   * archive = zip: users/<uid>/session.txt (+ other small files), manifest.json,
#     optional secret.key, optional db/prgram.db (consistent sqlite snapshot)
#   * optional password: whole archive sealed with PBKDF2 + Fernet (".prgbak")
#   * the key is only ever put into a password-protected archive
#   * restore: inspect first, pick accounts, never touches running accounts,
#     re-encrypts sessions with the current key, makes a "pre-restore" snapshot
# ===========================================================================
import base64 as _b64_bk
import hashlib as _hl_bk
import io as _io_bk
import sqlite3 as _sq_bk
import zipfile as _zf_bk

BK_DIR = DATA_DIR / "backups"
BK_CFG_FILE = DATA_DIR / "backup_cfg.json"
_BK_MAGIC = b"PRGBAK1\n"
_BK_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}\.(zip|prgbak)$")
_BK_MEMBER_RE = re.compile(r"^users/(\d{1,15})/([A-Za-z0-9_.\-]{1,80})$")
_BK_FILE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")
_BK_MAX_UPLOAD = 12_000_000
_BK_MAX_UNPACKED = 400_000_000


def _bk_cfg() -> dict:
    c = {"auto": False, "hours": 24, "keep": 7}
    try:
        if BK_CFG_FILE.exists():
            d = json.loads(BK_CFG_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                c["auto"] = bool(d.get("auto", False))
                c["hours"] = max(1, min(720, int(d.get("hours", 24))))
                c["keep"] = max(1, min(60, int(d.get("keep", 7))))
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    return c


def _bk_cfg_save(auto: Any, hours: Any, keep: Any) -> dict:
    c = {"auto": bool(auto), "hours": max(1, min(720, int(hours))), "keep": max(1, min(60, int(keep)))}
    BK_CFG_FILE.write_text(json.dumps(c), encoding="utf-8")
    return c


def _bk_path(name: Any) -> Path:
    name = str(name or "")
    if not _BK_NAME_RE.match(name):
        raise ValueError("bad backup name")
    p = BK_DIR / name
    if p.resolve().parent != BK_DIR.resolve():
        raise ValueError("bad backup name")
    return p


def _bk_fernet_pw(pw: str, salt: bytes):
    from cryptography.fernet import Fernet
    k = _hl_bk.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 300_000, 32)
    return Fernet(_b64_bk.urlsafe_b64encode(k))


def _bk_seal(raw: bytes, pw: str) -> bytes:
    if not pw:
        return raw
    try:
        import cryptography  # noqa: F401
    except ImportError:
        raise ValueError("a password needs the `cryptography` package (pip install cryptography)")
    salt = os.urandom(16)
    return _BK_MAGIC + salt + _bk_fernet_pw(pw, salt).encrypt(raw)


def _bk_unseal(blob: bytes, pw: str) -> bytes:
    if not blob.startswith(_BK_MAGIC):
        return blob
    if not pw:
        raise ValueError("this backup is password protected - enter its password")
    salt = blob[len(_BK_MAGIC):len(_BK_MAGIC) + 16]
    try:
        return _bk_fernet_pw(pw, salt).decrypt(blob[len(_BK_MAGIC) + 16:])
    except Exception:
        raise ValueError("wrong password (or the file is damaged)")


def _bk_build(include_key: bool, include_db: bool) -> bytes:
    buf = _io_bk.BytesIO()
    uids: List[int] = []
    n_enc = n_plain = 0
    with _zf_bk.ZipFile(buf, "w", _zf_bk.ZIP_DEFLATED) as z:
        if USERS_DIR.exists():
            for d in sorted(USERS_DIR.iterdir(), key=lambda x: x.name):
                if not (d.is_dir() and d.name.isdigit()):
                    continue
                for f in sorted(d.iterdir(), key=lambda x: x.name):
                    if not f.is_file() or f.suffix == ".tmp" or not _BK_FILE_RE.match(f.name):
                        continue
                    if f.stat().st_size > 5_000_000:
                        continue
                    data = f.read_bytes()
                    z.writestr(f"users/{d.name}/{f.name}", data)
                    if f.name == "session.txt" and data.strip():
                        uids.append(int(d.name))
                        if data.startswith(b"enc:"):
                            n_enc += 1
                        else:
                            n_plain += 1
        has_key = False
        if include_key:
            key = _env_str("SESSION_KEY")
            if not key and KEY_FILE.exists():
                key = KEY_FILE.read_text(encoding="utf-8").strip()
            if key:
                z.writestr("secret.key", key)
                has_key = True
        has_db = False
        if include_db and DB_FILE.exists():
            BK_DIR.mkdir(parents=True, exist_ok=True)
            tmp = BK_DIR / ".snapshot.tmp"
            src = _sq_bk.connect(str(DB_FILE), timeout=30)
            dst = _sq_bk.connect(str(tmp))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            z.write(str(tmp), "db/prgram.db")
            tmp.unlink(missing_ok=True)
            has_db = True
        z.writestr("manifest.json", json.dumps({
            "format": 1, "app": "PR GRAM", "version": VERSION, "created": int(time.time()),
            "uids": uids, "sessions_encrypted": n_enc, "sessions_plain": n_plain,
            "key": has_key, "db": has_db}))
    return buf.getvalue()


def _bk_make(kind: str, password: str = "", include_key: bool = False, include_db: bool = True) -> Path:
    if include_key and not password:
        raise ValueError("set a password to put the encryption key inside the backup")
    if password and len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    BK_DIR.mkdir(parents=True, exist_ok=True)
    blob = _bk_seal(_bk_build(include_key, include_db), password)
    ext = "prgbak" if password else "zip"
    base = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}"
    p = BK_DIR / f"{base}.{ext}"
    i = 1
    while p.exists():
        i += 1
        p = BK_DIR / f"{base}-{i}.{ext}"
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(blob)
    try:
        os.chmod(tmp, 0o600)
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    os.replace(tmp, p)
    return p


def _bk_prune(keep: int) -> None:
    autos = sorted(BK_DIR.glob("auto-*"), key=lambda x: x.stat().st_mtime, reverse=True)
    for old in autos[keep:]:
        try:
            old.unlink()
        except Exception as _swallow_e:
            _swallow(_swallow_e)


def _bk_row(p: Path) -> dict:
    st = p.stat()
    kind = p.name.split("-", 1)[0]
    row = {"name": p.name, "size": st.st_size, "ts": int(st.st_mtime), "kind": kind,
           "enc": False, "users": None, "key": False, "db": False}
    try:
        with open(p, "rb") as fh:
            head = fh.read(len(_BK_MAGIC))
        if head == _BK_MAGIC:
            row["enc"] = True
        else:
            with _zf_bk.ZipFile(str(p)) as z:
                m = json.loads(z.read("manifest.json"))
            row["users"] = len(m.get("uids") or [])
            row["key"] = bool(m.get("key"))
            row["db"] = bool(m.get("db"))
            row["ts"] = int(m.get("created") or row["ts"])
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    return row


def _bk_list() -> List[dict]:
    if not BK_DIR.exists():
        return []
    rows = [_bk_row(p) for p in BK_DIR.iterdir() if p.is_file() and _BK_NAME_RE.match(p.name)]
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


def _bk_open(name: str, pw: str):
    p = _bk_path(name)
    if not p.exists():
        raise ValueError("backup not found")
    raw = _bk_unseal(p.read_bytes(), pw)
    try:
        z = _zf_bk.ZipFile(_io_bk.BytesIO(raw))
        total = sum(i.file_size for i in z.infolist())
        if total > _BK_MAX_UNPACKED:
            raise ValueError("archive is too large")
        m = json.loads(z.read("manifest.json"))
    except ValueError:
        raise
    except Exception:
        raise ValueError("this is not a PR GRAM backup")
    if not isinstance(m, dict) or m.get("app") != "PR GRAM":
        raise ValueError("this is not a PR GRAM backup")
    return z, m


def _bk_arc_fernet(z):
    try:
        from cryptography.fernet import Fernet
        return Fernet(z.read("secret.key").strip())
    except Exception:
        return None


def _bk_dec(f, text: str) -> Optional[str]:
    try:
        return f.decrypt(text[4:].encode()).decode() if f else None
    except Exception:
        return None


def _bk_sessions(z) -> Dict[int, Dict[str, bytes]]:
    out: Dict[int, Dict[str, bytes]] = {}
    for info in z.infolist():
        mt = _BK_MEMBER_RE.match(info.filename)
        if not mt or info.file_size > 5_000_000:
            continue
        out.setdefault(int(mt.group(1)), {})[mt.group(2)] = z.read(info)
    return {u: f for u, f in out.items() if "session.txt" in f}


def _bk_state(uid: int) -> str:
    if uid in ACCOUNTS or uid in _LOADING:
        return "online"
    return "exists" if (USERS_DIR / str(uid) / "session.txt").exists() else "new"


def _bk_final_session(text: str, arc_f) -> Tuple[Optional[str], str]:
    """-> (text to store, reason if unusable)."""
    text = text.strip()
    if not text:
        return None, "empty"
    cur = _fernet()
    if text.startswith("enc:"):
        if cur and _bk_dec(cur, text) is not None:
            return text, ""
        raw = _bk_dec(arc_f, text)
        if raw is None:
            return None, "key"
        return (("enc:" + cur.encrypt(raw.encode()).decode()) if cur else raw), ""
    return (("enc:" + cur.encrypt(text.encode()).decode()) if cur else text), ""


def _bk_inspect(name: str, pw: str) -> dict:
    z, m = _bk_open(name, pw)
    arc_f = _bk_arc_fernet(z)
    accounts = []
    for uid, files in sorted(_bk_sessions(z).items()):
        try:
            u = user_row(uid) or {}
        except Exception:
            u = {}
        final, why = _bk_final_session(files["session.txt"].decode("utf-8", "ignore"), arc_f)
        accounts.append({"uid": uid, "name": u.get("first_name") or "", "username": u.get("username") or "",
                         "state": _bk_state(uid), "usable": final is not None, "why": why})
    return {"ok": True, "created": m.get("created"), "version": m.get("version"), "key": bool(m.get("key")),
            "db": bool(m.get("db")), "accounts": accounts}


def _bk_write_user(uid: int, files: Dict[str, bytes], session_text: str) -> None:
    d = USERS_DIR / str(uid)
    d.mkdir(parents=True, exist_ok=True)
    for fn, data in files.items():
        if fn == "session.txt" or fn.endswith(".tmp") or not _BK_FILE_RE.match(fn):
            continue
        tmp = d / (fn + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, d / fn)
    tmp = d / "session.tmp"
    tmp.write_text(session_text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception as _swallow_e:
        _swallow(_swallow_e)
    os.replace(tmp, d / "session.txt")


def _bk_restore(name: str, pw: str, uids: List[int], overwrite: bool, restore_db: bool) -> dict:
    z, m = _bk_open(name, pw)
    arc_f = _bk_arc_fernet(z)
    sess = _bk_sessions(z)
    pick = [u for u in uids if u in sess]
    if not pick and not restore_db:
        raise ValueError("nothing selected")
    pre = _bk_make("pre-restore", "", False, True)           # safety net: current state, no key, no password
    done: List[int] = []
    skipped: List[dict] = []
    for uid in pick:
        files = sess[uid]
        st = _bk_state(uid)
        if st == "online":
            skipped.append({"uid": uid, "why": "online"})
            continue
        if st == "exists" and not overwrite:
            skipped.append({"uid": uid, "why": "exists"})
            continue
        final, why = _bk_final_session(files["session.txt"].decode("utf-8", "ignore"), arc_f)
        if final is None:
            skipped.append({"uid": uid, "why": why or "key"})
            continue
        _bk_write_user(uid, files, final)
        done.append(uid)
    db_ok = False
    if restore_db:
        if "db/prgram.db" not in z.namelist():
            raise ValueError("this backup has no database")
        tmp = BK_DIR / ".restore.tmp"
        tmp.write_bytes(z.read("db/prgram.db"))
        try:
            chk = _sq_bk.connect(str(tmp))
            try:
                if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("the database inside the backup is damaged")
            finally:
                chk.close()
            src = _sq_bk.connect(str(tmp))
            dst = _sq_bk.connect(str(DB_FILE), timeout=60)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            db_ok = True
        finally:
            tmp.unlink(missing_ok=True)
    return {"ok": True, "restored": done, "skipped": skipped, "db": db_ok, "pre": pre.name, "restart": db_ok}


async def _bk_send(name: str) -> dict:
    p = _bk_path(name)
    if not p.exists():
        return _jerr("backup not found")
    with open(p, "rb") as fh:
        if fh.read(len(_BK_MAGIC)) != _BK_MAGIC:
            return _jerr("only password-protected backups can be sent over Telegram")
    if RT.bot is None or not owner_id():
        return _jerr("bot is not ready")
    await RT.bot.send_file(owner_id(), str(p), caption="🗄 PR GRAM backup\n" + p.name)
    return {"ok": True, "msg": "sent to your Telegram"}


async def _bk_api(path: str, qs: dict, data: dict) -> dict:
    act = path.rsplit("/", 1)[-1]
    pw = str(data.get("password") or "")
    try:
        if act == "list":
            return {"ok": True, "items": await asyncio.to_thread(_bk_list), "cfg": _bk_cfg(),
                    "crypto": _fernet() is not None, "sessions": len(list_stored_users())}
        if act == "create":
            p = await asyncio.to_thread(_bk_make, "manual", pw, bool(data.get("include_key")), bool(data.get("include_db", True)))
            evt(owner_id(), "BACKUP", f"created {p.name}")
            return {"ok": True, "name": p.name}
        if act == "upload":
            raw = _b64_bk.b64decode(str(data.get("data") or ""), validate=False)
            if not raw or len(raw) > _BK_MAX_UPLOAD:
                return _jerr("file is empty or too large")
            if not raw.startswith(_BK_MAGIC):
                try:
                    with _zf_bk.ZipFile(_io_bk.BytesIO(raw)) as z:
                        if json.loads(z.read("manifest.json")).get("app") != "PR GRAM":
                            raise ValueError
                except Exception:
                    return _jerr("this is not a PR GRAM backup")
            BK_DIR.mkdir(parents=True, exist_ok=True)
            ext = "prgbak" if raw.startswith(_BK_MAGIC) else "zip"
            p = BK_DIR / f"upload-{time.strftime('%Y%m%d-%H%M%S')}.{ext}"
            i = 1
            while p.exists():
                i += 1
                p = BK_DIR / f"upload-{time.strftime('%Y%m%d-%H%M%S')}-{i}.{ext}"
            p.write_bytes(raw)
            try:
                os.chmod(p, 0o600)
            except Exception as _swallow_e:
                _swallow(_swallow_e)
            evt(owner_id(), "BACKUP", f"uploaded {p.name}")
            return {"ok": True, "name": p.name}
        if act == "download":
            p = _bk_path(data.get("name"))
            if not p.exists():
                return _jerr("backup not found")
            return {"ok": True, "name": p.name, "data": _b64_bk.b64encode(p.read_bytes()).decode()}
        if act == "delete":
            p = _bk_path(data.get("name"))
            p.unlink(missing_ok=True)
            evt(owner_id(), "BACKUP", f"deleted {p.name}")
            return {"ok": True}
        if act == "inspect":
            return await asyncio.to_thread(_bk_inspect, str(data.get("name") or ""), pw)
        if act == "restore":
            uids = [int(u) for u in (data.get("uids") or []) if str(u).lstrip("-").isdigit()]
            r = await asyncio.to_thread(_bk_restore, str(data.get("name") or ""), pw, uids,
                                        bool(data.get("overwrite")), bool(data.get("restore_db")))
            evt(owner_id(), "BACKUP", f"restore {data.get('name')}: {len(r['restored'])} ok, {len(r['skipped'])} skipped, db={r['db']}")
            for u in r["restored"]:
                spawn(_load_account(u), f"bk-load-{u}")
            return r
        if act == "send":
            return await _bk_send(str(data.get("name") or ""))
        if act == "cfg":
            return {"ok": True, "cfg": _bk_cfg_save(data.get("auto"), data.get("hours", 24), data.get("keep", 7))}
    except ValueError as e:
        return _jerr(str(e))
    except Exception as e:
        log.error(f"backup api {act}: {e}")
        return _jerr("backup failed: " + str(e)[:160])
    return _jerr("not found")


_orig_web_api_bk = web_api


async def web_api(role: str, wuid: int, path: str, qs: Dict[str, List[str]], data: dict) -> dict:
    if path.startswith("/api/owner/backup/"):
        if role != "admin":
            return _jerr("owner only")
        return await _bk_api(path, qs, data if isinstance(data, dict) else {})
    return await _orig_web_api_bk(role, wuid, path, qs, data)


async def _bk_auto_loop() -> None:
    await asyncio.sleep(120)
    while True:
        try:
            cfg = _bk_cfg()
            if cfg["auto"]:
                last = max((p.stat().st_mtime for p in BK_DIR.glob("auto-*")), default=0.0) if BK_DIR.exists() else 0.0
                if time.time() - last >= cfg["hours"] * 3600 - 60:
                    p = await asyncio.to_thread(_bk_make, "auto", "", False, True)
                    await asyncio.to_thread(_bk_prune, cfg["keep"])
                    evt(owner_id() or 0, "BACKUP", f"auto {p.name}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"auto backup: {e}")
        await asyncio.sleep(600)


_orig_web_start_bk = WebPanel.start


async def _web_start_bk(self, *a, **k):
    r = await _orig_web_start_bk(self, *a, **k)
    spawn(_bk_auto_loop(), "auto-backup")
    return r


WebPanel.start = _web_start_bk
log.info("backup/restore loaded (owner dashboard > Backup)")


# ===========================================================================
# Password-only web access (replaces legacy URL/header tokens)
# ===========================================================================
import secrets as _secrets_login
import hashlib as _hashlib_login
import hmac as _hmac_login
from http.cookies import SimpleCookie as _SimpleCookie_login
from urllib.parse import urlparse as _urlparse_login

_LOGIN_ITERATIONS = 310_000
_LOGIN_SESSION_SECONDS = 14 * 24 * 3600
_LOGIN_COOKIE = "pg_session"
_LOGIN_DUMMY_SALT = b"prgram-login-dummy-salt"
_LOGIN_DUMMY_HASH = _hashlib_login.pbkdf2_hmac("sha256", b"not-a-password", _LOGIN_DUMMY_SALT, _LOGIN_ITERATIONS)


def _login_tables():
    db_exec("CREATE TABLE IF NOT EXISTS pg_web_login (uid INTEGER PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE, salt BLOB NOT NULL, password_hash BLOB NOT NULL, must_change INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL)")
    db_exec("CREATE TABLE IF NOT EXISTS pg_web_session (sid_hash TEXT PRIMARY KEY, uid INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL)")
    db_exec("CREATE INDEX IF NOT EXISTS idx_pg_web_session_uid ON pg_web_session(uid)")
    db_exec("CREATE TABLE IF NOT EXISTS pg_lang_choice (uid INTEGER PRIMARY KEY, chosen_at REAL NOT NULL)")


def _login_hash(password: str, salt: bytes) -> bytes:
    return _hashlib_login.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _LOGIN_ITERATIONS)


def _login_new_username() -> str:
    return "pg-" + _secrets_login.token_hex(5)


def _login_issue(uid: int, reset: bool = False) -> dict:
    """Create credentials, or issue a one-time temporary password while retaining the username."""
    _login_tables()
    uid = int(uid)
    if uid <= 0:
        raise ValueError("invalid account")
    if not user_known(uid):
        ensure_user(uid)
    row = db_exec("SELECT username FROM pg_web_login WHERE uid=?", (uid,)).fetchone()
    if row and not reset:
        return {"created": False, "username": str(row["username"]), "password": ""}
    password = _secrets_login.token_urlsafe(15)
    salt = _secrets_login.token_bytes(16)
    pw_hash = _login_hash(password, salt)
    if row:
        username = str(row["username"])
        db_exec("UPDATE pg_web_login SET salt=?, password_hash=?, must_change=1, updated_at=? WHERE uid=?",
                (salt, pw_hash, time.time(), uid))
    else:
        username = _login_new_username()
        for _ in range(5):
            try:
                db_exec("INSERT INTO pg_web_login(uid,username,salt,password_hash,must_change,updated_at) VALUES(?,?,?,?,1,?)",
                        (uid, username, salt, pw_hash, time.time()))
                break
            except sqlite3.IntegrityError:
                username = _login_new_username()
        else:
            raise RuntimeError("could not create a unique username")
    if reset:
        db_exec("DELETE FROM pg_web_session WHERE uid=?", (uid,))
    return {"created": True, "username": username, "password": password}


def _login_send_credentials(uid: int, credential: dict, reset: bool = False) -> None:
    if not credential.get("created"):
        return
    text = L(uid,
        "🔐 <b>PR GRAM web sign-in</b>\n\nUsername: <code>{username}</code>\nTemporary password: <code>{password}</code>\n\nSign in to the dashboard and choose a new password. Keep this message private. For a new temporary password, send /webpass here.",
        "🔐 <b>ورود به پنل وب PR GRAM</b>\n\nنام کاربری: <code>{username}</code>\nرمز موقت: <code>{password}</code>\n\nوارد پنل شو و رمز تازه انتخاب کن. این پیام را خصوصی نگه دار. برای دریافت رمز موقت تازه، همین‌جا دستور /webpass را بفرست.")
    text = text.format(username=credential["username"], password=credential["password"])
    spawn(notify(uid, text), f"web-login-credential-{uid}")


def _login_cookie_value(headers: dict) -> str:
    try:
        c = _SimpleCookie_login()
        c.load(headers.get("cookie", ""))
        m = c.get(_LOGIN_COOKIE)
        return m.value if m else ""
    except Exception:
        return ""


def _login_role(uid: int) -> Optional[str]:
    if uid <= 0 or is_banned(uid) or not allowed(uid):
        return None
    if uid == owner_id():
        return "admin"
    if staff_perms(uid) is not None:
        return "sub"
    return "user"


def _login_session_create(uid: int) -> str:
    _login_tables()
    now = time.time()
    db_exec("DELETE FROM pg_web_session WHERE expires_at<?", (now,))
    sid = _secrets_login.token_urlsafe(32)
    sid_hash = _hashlib_login.sha256(sid.encode()).hexdigest()
    db_exec("INSERT INTO pg_web_session(sid_hash,uid,created_at,expires_at) VALUES(?,?,?,?)",
            (sid_hash, int(uid), now, now + _LOGIN_SESSION_SECONDS))
    return sid


_ALLOW_INSECURE_WEB_LOGIN = _env_bool("ALLOW_INSECURE_WEB_LOGIN", False)
if _ALLOW_INSECURE_WEB_LOGIN:
    log.warning("INSECURE WEB LOGIN enabled: HTTP credentials and session cookies can be intercepted")


def _login_is_secure(headers: dict) -> bool:
    # The app server itself is plain HTTP. Trust the forwarded scheme only when
    # explicitly deployed behind the owner's own reverse proxy. HTTP is opt-in.
    forwarded_https = TRUST_PROXY and headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower() == "https"
    return bool(forwarded_https or _ALLOW_INSECURE_WEB_LOGIN)


def _login_cookie_header(headers: dict, sid: str, clear: bool = False) -> str:
    # Never mark a cookie Secure for an HTTP connection, or the browser won't send it back.
    secure = bool(TRUST_PROXY and headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower() == "https")
    parts = [f"{_LOGIN_COOKIE}={sid if not clear else ''}", "Path=/", "HttpOnly", "SameSite=Strict"]
    if secure:
        parts.append("Secure")
    parts.append("Max-Age=0" if clear else f"Max-Age={_LOGIN_SESSION_SECONDS}")
    return "; ".join(parts)


async def _login_send_json(writer, status: int, payload: dict, cookie: str = ""):
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 429: "Too Many Requests", 500: "Server Error"}.get(status, "OK")
    head = (f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\nCache-Control: no-store\r\n"
            "X-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
            "Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()\r\n"
            "Content-Security-Policy: default-src 'self'; style-src 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'\r\n")
    if cookie:
        head += f"Set-Cookie: {cookie}\r\n"
    writer.write((head + "\r\n").encode("latin1") + body)
    await writer.drain()


_orig_auth_password_login = WebPanel._auth

def _auth_password_login(self, headers, qs):
    sid = _login_cookie_value(headers)
    if not sid or len(sid) > 160:
        return None
    _login_tables()
    key = _hashlib_login.sha256(sid.encode()).hexdigest()
    row = db_exec("SELECT uid,expires_at FROM pg_web_session WHERE sid_hash=?", (key,)).fetchone()
    if not row:
        return None
    uid = int(row["uid"])
    if float(row["expires_at"]) <= time.time() or _login_role(uid) is None:
        db_exec("DELETE FROM pg_web_session WHERE sid_hash=?", (key,))
        return None
    return (_login_role(uid), uid)

WebPanel._auth = _auth_password_login


_orig_web_api_password_login = web_api
async def web_api(role: str, wuid: int, path: str, qs: Dict[str, List[str]], data: dict) -> dict:
    if path == "/api/action" and isinstance(data, dict) and data.get("action") == "reset_token":
        return _jerr("Private dashboard links are no longer used")
    if path in ("/api/owner/link", "/api/owner/relink"):
        return _jerr("Password sign-in is enabled; private links are no longer used")
    if path == "/api/owner/password-reset":
        if role != "admin" or int(wuid) != owner_id():
            return _jerr("owner only")
        try:
            target = int((qs.get("target") or [data.get("target") or 0])[0])
        except Exception:
            target = 0
        if target <= 0 or (not user_known(target) and not is_admin(target) and staff_perms(target) is None):
            return _jerr("unknown user")
        if is_banned(target):
            return _jerr("account is banned")
        credential = _login_issue(target, reset=True)
        _login_send_credentials(target, credential, reset=True)
        evt(owner_id(), "LOGIN", f"temporary web password reset for {target}")
        return {"ok": True, "username": credential["username"], "password": credential["password"], "target": target}
    result = await _orig_web_api_password_login(role, wuid, path, qs, data)
    if path == "/api/me" and isinstance(result, dict) and result.get("ok"):
        result["uid"] = int(wuid)
        _login_tables()
        row = db_exec("SELECT username,must_change FROM pg_web_login WHERE uid=?", (int(wuid),)).fetchone()
        if row:
            result["username"] = str(row["username"])
            result["must_change"] = bool(row["must_change"])
        result["lang"] = get_cfg(int(wuid)).get("lang") or "en"
    return result


_orig_route_password_login = WebPanel._route
async def _route_password_login(self, method, path, headers, body, writer, ip):
    u = _urlparse_login(path)
    if method == "POST" and u.path in ("/api/login", "/api/logout", "/api/password"):
        if self._blocked(ip):
            await _login_send_json(writer, 429, {"ok": False, "error": "too many attempts"})
            return
        try:
            data = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        if u.path == "/api/login":
            if not _login_is_secure(headers):
                await _login_send_json(writer, 400, {"ok": False, "error": "secure connection required"})
                return
            username = str(data.get("username") or "").strip()[:80]
            password = str(data.get("password") or "")[:256]
            _login_tables()
            row = db_exec("SELECT uid,salt,password_hash FROM pg_web_login WHERE username=? COLLATE NOCASE", (username,)).fetchone() if username else None
            salt = bytes(row["salt"]) if row else _LOGIN_DUMMY_SALT
            expected = bytes(row["password_hash"]) if row else _LOGIN_DUMMY_HASH
            actual = _login_hash(password, salt)
            valid = _hmac_login.compare_digest(actual, expected) and row is not None
            uid = int(row["uid"]) if valid else 0
            role = _login_role(uid) if valid else None
            if not valid or role is None:
                self._fail(ip)
                await _login_send_json(writer, 401, {"ok": False, "error": "Invalid username or password"})
                return
            sid = _login_session_create(uid)
            await _login_send_json(writer, 200, {"ok": True}, _login_cookie_header(headers, sid))
            return
        if u.path == "/api/logout":
            sid = _login_cookie_value(headers)
            if sid:
                db_exec("DELETE FROM pg_web_session WHERE sid_hash=?", (_hashlib_login.sha256(sid.encode()).hexdigest(),))
            await _login_send_json(writer, 200, {"ok": True}, _login_cookie_header(headers, "", clear=True))
            return
        if not _login_is_secure(headers):
            await _login_send_json(writer, 400, {"ok": False, "error": "secure connection required"})
            return
        auth = self._auth(headers, {})
        if not auth:
            await _login_send_json(writer, 401, {"ok": False, "error": "Sign in again"})
            return
        uid = int(auth[1])
        _login_tables()
        row = db_exec("SELECT salt,password_hash,must_change FROM pg_web_login WHERE uid=?", (uid,)).fetchone()
        if not row:
            await _login_send_json(writer, 401, {"ok": False, "error": "Sign in again"})
            return
        current = str(data.get("currentPassword") or "")[:256]
        new_password = str(data.get("newPassword") or "")
        if len(new_password) < 10 or len(new_password) > 256:
            await _login_send_json(writer, 400, {"ok": False, "error": "Use a password with at least 10 characters"})
            return
        if not bool(row["must_change"]):
            actual = _login_hash(current, bytes(row["salt"]))
            if not current or not _hmac_login.compare_digest(actual, bytes(row["password_hash"])):
                await _login_send_json(writer, 400, {"ok": False, "error": "Current password is incorrect"})
                return
        salt = _secrets_login.token_bytes(16)
        db_exec("UPDATE pg_web_login SET salt=?,password_hash=?,must_change=0,updated_at=? WHERE uid=?",
                (salt, _login_hash(new_password, salt), time.time(), uid))
        db_exec("DELETE FROM pg_web_session WHERE uid=?", (uid,))
        sid = _login_session_create(uid)
        await _login_send_json(writer, 200, {"ok": True}, _login_cookie_header(headers, sid))
        return
    await _orig_route_password_login(self, method, path, headers, body, writer, ip)

WebPanel._route = _route_password_login


# Ask for language before sending credentials or showing the bot dashboard.
_LOGIN_LANG_PENDING = {}


def _login_language_done(uid: int) -> bool:
    _login_tables()
    return db_exec("SELECT 1 FROM pg_lang_choice WHERE uid=?", (int(uid),)).fetchone() is not None


async def _login_language_prompt(uid: int) -> None:
    text = "Choose your language / زبان کاربری را انتخاب کن"
    buttons = [[cb("فارسی", b"weblang:fa", "primary"), cb("English", b"weblang:en", "primary")]]
    await notify_btn(int(uid), text, buttons)


_orig_ui_send_password_login = ui_send
async def ui_send(uid: int, view: str = "main", respond_to=None) -> None:
    try:
        if int(uid) > 0 and not is_banned(int(uid)) and allowed(int(uid)):
            _login_tables()
            if not _login_language_done(int(uid)):
                _LOGIN_LANG_PENDING[int(uid)] = view
                await _login_language_prompt(int(uid))
                return
            row = db_exec("SELECT 1 FROM pg_web_login WHERE uid=?", (int(uid),)).fetchone()
            if not row:
                credential = _login_issue(int(uid), reset=False)
                _login_send_credentials(int(uid), credential)
    except Exception as e:
        log.warning(f"web login setup for {uid}: {type(e).__name__}")
    await _orig_ui_send_password_login(uid, view, respond_to)


_orig_on_text_password_login = on_text
async def on_text(event):
    if (getattr(event, "raw_text", "") or "").strip().split(" ", 1)[0].split("@", 1)[0].lower() == "/webpass":
        return
    await _orig_on_text_password_login(event)


_orig_register_password_login = register_handlers
def register_handlers(bot: TelegramClient) -> None:
    _orig_register_password_login(bot)
    @bot.on(events.NewMessage(pattern=r"^/webpass(?:@\w+)?$", func=lambda e: e.is_private))
    async def _webpass(event):
        uid = int(event.sender_id or 0)
        if uid <= 0 or is_banned(uid) or not allowed(uid):
            await event.respond("Access is not available for this account.")
            return
        try:
            if not _login_language_done(uid):
                await _login_language_prompt(uid)
                return
            credential = _login_issue(uid, reset=True)
            _login_send_credentials(uid, credential, reset=True)
            await event.respond(L(uid, "A new temporary password was sent in this chat. Previous web sessions have been signed out.", "رمز موقت تازه در همین گفت‌وگو فرستاده شد؛ نشست‌های قبلی پنل هم خارج شدند."))
        except Exception as e:
            log.warning(f"web password reset {uid}: {type(e).__name__}")
            await event.respond(L(uid, "Could not reset the web password. Please try again later.", "بازنشانی رمز انجام نشد. کمی بعد دوباره تلاش کن."))


# Persist language-button choices from the first bot interaction.
_orig_route_cb_language_login = route_cb
async def route_cb(event, data: str) -> None:
    if data in ("weblang:fa", "weblang:en"):
        uid = int(event.sender_id or 0)
        lang = data.split(":", 1)[1]
        if uid <= 0 or is_banned(uid) or not allowed(uid):
            await answer(event, "⛔", alert=True)
            return
        set_cfg(uid, lang=lang)
        _login_tables()
        db_exec("INSERT OR REPLACE INTO pg_lang_choice(uid,chosen_at) VALUES(?,?)", (uid, time.time()))
        await answer(event, L(uid, "English selected.", "زبان فارسی انتخاب شد."))
        await ui_send(uid, _LOGIN_LANG_PENDING.pop(uid, "main"))
        return
    await _orig_route_cb_language_login(event, data)


# Dashboard links now open the login page; credentials are never placed in URLs.
def dash_url(uid: int, admin: bool = False) -> str:
    base = dash_base()
    if not base or RT.web is None:
        return ""
    return f"{base}{'/admin/panel/accounts' if admin else f'/u/{int(uid)}/panel/overview'}"

# The legacy master token is not generated or accepted in this version.
def _web_token() -> str:
    return ""

log.info("password-only web login enabled; legacy token authentication is disabled")


if __name__ == "__main__":
    sys.exit(main())
