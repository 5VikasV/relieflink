"""SQLite locally and persistent PostgreSQL on Vercel, with atomic writes."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

from .migrations import LATEST_SCHEMA_VERSION, MIGRATIONS

ITEMS = ("food", "water", "medical")
GRAMS = {"food": 800, "water": 1000, "medical": 1500}
ZERO = {k: 0 for k in ITEMS}
ROOT = Path(__file__).resolve().parent.parent
# Transaction-scoped and compatible with Neon's transaction pooler. All short
# writes share this lock across function instances; solving happens outside it.
WRITE_LOCK = 724_304_619_821


class DatabaseConfigurationError(RuntimeError):
    pass


class DatabaseUnavailableError(RuntimeError):
    pass


class DatabaseIntegrityError(RuntimeError):
    pass


def is_postgres(target):
    return isinstance(target, str) and target.startswith(("postgres://", "postgresql://"))


def database_target(explicit=None):
    """Explicit targets win; otherwise prefer the server-only DATABASE_URL."""
    if explicit is not None:
        if is_postgres(explicit):
            return explicit
        if isinstance(explicit, str) and "://" in explicit:
            raise DatabaseConfigurationError("Only PostgreSQL URLs or local SQLite file paths are supported.")
        return Path(explicit)
    if os.environ.get("VERCEL") != "1":
        load_dotenv(ROOT / ".env.local", override=False)
        load_dotenv(ROOT / ".env", override=False)
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        if not is_postgres(url):
            raise DatabaseConfigurationError("DATABASE_URL must be a postgres:// or postgresql:// connection URL.")
        return url
    if os.environ.get("VERCEL") == "1":
        raise DatabaseConfigurationError("Set the server-only DATABASE_URL environment variable before deploying to Vercel.")
    return Path(os.environ.get("RELIEFLINK_DB") or ROOT / "data" / "relieflink.db")


def database_kind(target):
    return "postgresql" if is_postgres(target) else "sqlite"


class Record(dict):
    """Support both named and positional access, like the original SQLite rows."""
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Cursor:
    def __init__(self, cursor):
        self.raw = cursor

    @staticmethod
    def record(row):
        return Record(row) if isinstance(row, dict) else row

    def fetchone(self):
        return self.record(self.raw.fetchone())

    def fetchall(self):
        return [self.record(row) for row in self.raw.fetchall()]

    def __iter__(self):
        return (self.record(row) for row in self.raw)


def postgres_parameters(statement):
    # Static queries use '?' parameters. Preserve quoted question marks and
    # escape literal percent characters for psycopg's parameter binder.
    chunks = re.split(r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")", statement)
    return "".join(chunk.replace("%", "%%").replace("?", "%s") if i % 2 == 0
                   else chunk.replace("%", "%%") for i, chunk in enumerate(chunks))


class Connection:
    def __init__(self, raw, postgres=False):
        self.raw, self.postgres = raw, postgres

    def execute(self, statement, parameters=None):
        try:
            if self.postgres:
                query = postgres_parameters(statement) if parameters is not None else statement
                cursor = self.raw.execute(query, parameters)
            else:
                cursor = self.raw.execute(statement, parameters if parameters is not None else ())
            return Cursor(cursor)
        except (sqlite3.IntegrityError, psycopg.IntegrityError):
            raise DatabaseIntegrityError("The operation conflicts with existing records.") from None
        except (sqlite3.OperationalError, psycopg.Error):
            raise DatabaseUnavailableError("The database is unavailable or not initialised. Run the database setup command and try again.") from None

    def begin(self, *, write=False):
        if self.postgres:
            self.execute("BEGIN" if write else "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if write:
                self.execute("SELECT pg_advisory_xact_lock(?)", (WRITE_LOCK,))
        else:
            self.execute("BEGIN IMMEDIATE" if write else "BEGIN")

    def commit(self):
        try:
            self.raw.commit()
        except (sqlite3.Error, psycopg.Error):
            raise DatabaseUnavailableError("The database could not commit this operation. Refresh before trying again.") from None

    def rollback(self):
        try:
            self.raw.rollback()
        except (sqlite3.Error, psycopg.Error):
            raise DatabaseUnavailableError("The database connection was interrupted. Refresh before trying again.") from None

    def close(self):
        self.raw.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return f"{prefix}-{secrets.token_hex(6)}"


def dumps(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()
    return f"pbkdf2_sha256$600000${salt}${digest}"


def password_matches(password, encoded):
    try:
        _, rounds, salt, expected = encoded.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def connect(target):
    try:
        if is_postgres(target):
            config = urlsplit(target)
            options = {"autocommit": True, "row_factory": dict_row,
                       "connect_timeout": 10, "prepare_threshold": None}
            if config.hostname not in ("localhost", "127.0.0.1", "::1") and "sslmode" not in parse_qs(config.query):
                options["sslmode"] = "require"
            return Connection(psycopg.connect(target, **options), postgres=True)
        raw = sqlite3.connect(str(target), timeout=15, isolation_level=None)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("PRAGMA busy_timeout=15000")
        return Connection(raw)
    except (sqlite3.Error, psycopg.Error, ValueError):
        # A driver error can contain a credential-bearing URL. Keep it private.
        raise DatabaseUnavailableError("Could not connect to the database. Check its configuration and availability.") from None


@contextmanager
def transaction(path):
    db = connect(path)
    try:
        db.begin(write=True)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def read_transaction(target):
    db = connect(target)
    try:
        db.begin()
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _migrate(db):
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    current = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
    if current > LATEST_SCHEMA_VERSION:
        raise DatabaseConfigurationError("This database uses a newer schema. Deploy a compatible application version.")
    for migration_version, statements in MIGRATIONS:
        if migration_version > current:
            for statement in statements:
                db.execute(statement)
            db.execute("INSERT INTO schema_migrations VALUES(?,?)", (migration_version, now()))


def initialize(target):
    """Migrate and seed once. App startup calls this only for local SQLite."""
    if not is_postgres(target):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(target)) as db:
            db.execute("PRAGMA journal_mode=WAL")
    with transaction(target) as db:
        _migrate(db)
        if not db.execute("SELECT value FROM meta WHERE key='initialized'").fetchone():
            from .seed import seed_demo
            seed_demo(db)


def check_ready(target):
    with read_transaction(target) as db:
        row = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        if not row or row[0] != LATEST_SCHEMA_VERSION:
            raise DatabaseUnavailableError("Database schema is not ready. Run python -m backend.seed once.")
        if not db.execute("SELECT value FROM meta WHERE key='initialized'").fetchone():
            raise DatabaseUnavailableError("Database is not initialised. Run python -m backend.seed once.")


def put_meta(db, key, value):
    db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def version(db):
    row = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    return int(row[0]) if row else 1


def bump(db):
    value = version(db) + 1
    put_meta(db, "version", str(value))
    return value


def log(db, user, action, detail):
    db.execute("INSERT INTO activity VALUES(?,?,?,?,?)", (uid("event"), now(), user, action, detail))


def public_user(row):
    return {k: row[k] for k in ("id", "name", "email", "role")}


def read_state(db, user=None):
    locations = [dict(r) for r in db.execute("SELECT * FROM locations ORDER BY id")]
    warehouses = [dict(r) for r in db.execute("SELECT * FROM warehouses ORDER BY id")]
    plans = [dict(r) for r in db.execute("SELECT * FROM plans ORDER BY created_at DESC,id DESC")]
    deliveries = [dict(r) for r in db.execute("SELECT * FROM deliveries ORDER BY created_at DESC,sequence,id")]
    roads = [dict(r) for r in db.execute("SELECT * FROM roads ORDER BY id")]
    vehicles = [dict(r) for r in db.execute("SELECT * FROM vehicles ORDER BY id")]
    committed = {loc["id"]: dict(ZERO) for loc in locations}
    for delivery in deliveries:
        delivery["manifest"] = json.loads(delivery["manifest"])
        delivery["received"] = json.loads(delivery["received"]) if delivery["received"] else None
        if delivery["status"] in ("reserved", "dispatched"):
            for item in ITEMS:
                committed[delivery["location_id"]][item] += delivery["manifest"][item]
    for location in locations:
        location["demands"] = json.loads(location["demands"])
        location["received"] = json.loads(location["received"])
        location["committed"] = committed[location["id"]]
    for warehouse in warehouses:
        warehouse["stock"] = json.loads(warehouse["stock"])
        warehouse["reserved"] = json.loads(warehouse["reserved"])
    for plan in plans:
        plan["result"] = json.loads(plan["result"])
    for road in roads:
        road["blocked"] = bool(road["blocked"])
    settings = json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
    return {
        "user": user, "version": version(db), "demo": True,
        "storage": "postgresql" if db.postgres else "sqlite",
        "locations": locations, "warehouses": warehouses, "vehicles": vehicles,
        "roads": roads, "plans": plans, "deliveries": deliveries,
        "activity": [dict(r) for r in db.execute("SELECT * FROM activity ORDER BY created_at DESC,id DESC LIMIT 250")],
        "settings": settings,
        "summary": {
            "population": sum(l["population"] for l in locations), "locations": len(locations),
            "available_kg": round(sum((w["stock"][i]-w["reserved"][i])*GRAMS[i] for w in warehouses for i in ITEMS)/1000, 1),
            "unmet_units": sum(max(0,l["demands"][i]-l["received"][i]-l["committed"][i]) for l in locations for i in ITEMS),
            "active_deliveries": sum(d["status"] in ("reserved","dispatched") for d in deliveries),
            "completed_deliveries": sum(d["status"] == "verified" for d in deliveries),
        },
    }
