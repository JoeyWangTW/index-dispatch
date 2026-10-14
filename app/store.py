import hashlib
import json
import sqlite3
import time
import uuid

from cryptography.fernet import Fernet


class Store:
    """One process, one persistent SQLite volume. Async operations never hold transactions open."""

    def __init__(self, settings):
        self.settings = settings
        settings.prepare_directory()
        self.db = sqlite3.connect(settings.database, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.cipher = Fernet(settings.encryption_key.encode())
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS requests (
              id TEXT PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
              transcript TEXT NOT NULL, recorded_at INTEGER, created REAL NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued', route TEXT, destination TEXT, body TEXT,
              detail TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt REAL NOT NULL DEFAULT 0, subscription_id TEXT
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
              id TEXT PRIMARY KEY, client_id TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
              expires REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS oauth (kind TEXT, key TEXT, value TEXT NOT NULL,
              expires REAL NOT NULL, PRIMARY KEY(kind,key));
        """)
        # Additive migration: old deliveries have unknown acknowledgment/timing, not invented dates.
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(requests)")}
        for column in ("delivered_at", "acknowledged_at", "progress_at", "finished_at"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE requests ADD COLUMN {column} REAL")

    def recover(self):
        self.db.execute("UPDATE requests SET status='queued' WHERE status='routing'")
        self.db.execute("UPDATE requests SET status='ready' WHERE status='delivering'")
        self.db.execute(
            "UPDATE requests SET status='needs_review', detail=? WHERE status='saving'",
            ("Service restarted during a Notion write. Verify the page before retrying.",),
        )

    def add(self, transcript, recorded_at=None, source="ring", key=None):
        fingerprint = hashlib.sha256(json.dumps([transcript, recorded_at, source]).encode()).hexdigest()
        dedupe = (
            hashlib.sha256(key.encode()).hexdigest()
            if key
            else (fingerprint if recorded_at is not None else str(uuid.uuid4()))
        )
        existing = self.db.execute("SELECT * FROM requests WHERE dedupe=?", (dedupe,)).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ValueError("Idempotency key already used for different content")
            return dict(existing), False
        request_id = str(uuid.uuid4())
        self.db.execute(
            """INSERT INTO requests(id,dedupe,fingerprint,transcript,recorded_at,created)
                           VALUES(?,?,?,?,?,?)""",
            (request_id, dedupe, fingerprint, transcript, recorded_at, time.time()),
        )
        return self.get(request_id), True

    def get(self, request_id):
        row = self.db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def recent(self):
        return [
            self.view(dict(r))
            for r in self.db.execute("SELECT * FROM requests ORDER BY created DESC LIMIT 100")
        ]

    def view(self, row, now=None):
        now = time.time() if now is None else now
        result = {**row, "display_status": row["status"], "attention": False, "attention_detail": ""}
        if row["route"] != "dots":
            return result
        if row["status"] == "delivered":
            result["display_status"] = "awaiting_ack"
            if row["delivered_at"] is None:
                result.update(
                    display_status="unconfirmed",
                    attention=True,
                    attention_detail="Older delivery: no agent acknowledgment was recorded. Check Kiro's chat before taking action.",
                )
            elif now - row["delivered_at"] >= self.settings.dots_ack_timeout:
                result.update(
                    display_status="no_ack",
                    attention=True,
                    attention_detail="ChatGPT accepted this request, but the agent has not acknowledged it. Check Kiro's chat using the task ID below.",
                )
        elif row["status"] == "in_progress":
            if row["progress_at"] is None or now - row["progress_at"] >= self.settings.dots_progress_timeout:
                result.update(
                    display_status="stalled",
                    attention=True,
                    attention_detail="The agent started this request but has not reported a recent update or result. Completion is unconfirmed; check Kiro's chat.",
                )
        elif row["status"] in {"needs_input", "failed"}:
            result["attention"] = True
        return result

    def update(self, request_id, **fields):
        allowed = {
            "status",
            "route",
            "destination",
            "body",
            "detail",
            "attempts",
            "next_attempt",
            "subscription_id",
            "delivered_at",
            "acknowledged_at",
            "progress_at",
            "finished_at",
        }
        assert fields and fields.keys() <= allowed
        self.db.execute(
            f"UPDATE requests SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
            (*fields.values(), request_id),
        )

    def pending(self):
        return [
            dict(r)
            for r in self.db.execute(
                """SELECT * FROM requests
          WHERE status IN ('queued','ready','waiting_subscription') AND next_attempt<=?
          ORDER BY created LIMIT 10""",
                (time.time(),),
            )
        ]

    def oauth_put(self, kind, key, value, lifetime=600):
        self.db.execute("DELETE FROM oauth WHERE expires < ?", (time.time(),))
        self.db.execute(
            "INSERT OR REPLACE INTO oauth VALUES(?,?,?,?)",
            (kind, key, json.dumps(value), time.time() + lifetime),
        )

    def oauth_get(self, kind, key, consume=False):
        row = self.db.execute(
            "SELECT value FROM oauth WHERE kind=? AND key=? AND expires>?", (kind, key, time.time())
        ).fetchone()
        if consume:
            self.db.execute("DELETE FROM oauth WHERE kind=? AND key=?", (kind, key))
        return json.loads(row[0]) if row else None

    def subscription(self):
        row = self.db.execute(
            "SELECT * FROM subscriptions WHERE active=1 AND expires>? LIMIT 1", (time.time(),)
        ).fetchone()
        return dict(row) if row else None

    def save_subscription(self, sub_id, client_id, url, secret, expires):
        self.db.execute(
            "INSERT OR REPLACE INTO subscriptions VALUES(?,?,?,?,?,1)",
            (sub_id, client_id, url, self.cipher.encrypt(secret.encode()).decode(), expires),
        )

    def close(self):
        self.db.close()
