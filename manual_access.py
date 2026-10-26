"""Fixed-term grants. User lists live only in SQLite, never in this module."""

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json


def normalize_username(username):
    value = (username or "").strip().removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{5,32}", value):
        raise ValueError("Invalid Telegram username")
    return value


def _datetime(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("Grant timestamps must include a timezone")
    return dt.astimezone(timezone.utc)


def reserve_grants(db_path, batch_id, grants, *, issued_at=None):
    """Reserve once, including people not yet in clients. Replay keeps dates."""
    now = _datetime(issued_at) if issued_at else datetime.now(timezone.utc)
    entries = []
    for grant in grants:
        username = normalize_username(grant["username"])
        tier = grant["tier"]
        days = {"1m": 30, "3m": 90}[tier]
        if grant.get("days", days) != days:
            raise ValueError("Grant duration differs from the paid tariff")
        entries.append((username, tier, days))
    if not batch_id or len({x[0] for x in entries}) != len(entries):
        raise ValueError("Missing batch ID or duplicate usernames")
    with closing(sqlite3.connect(db_path, timeout=30)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS manual_access_grants (
            batch_id TEXT NOT NULL,
            username TEXT NOT NULL,
            tier TEXT NOT NULL,
            days INTEGER NOT NULL,
            issued_at TEXT NOT NULL,
            paid_until TEXT NOT NULL,
            user_id TEXT,
            claimed_at TEXT,
            PRIMARY KEY (batch_id, username),
            UNIQUE (batch_id, user_id)
        )""")
        existing = db.execute(
            "SELECT username, tier, days, issued_at FROM manual_access_grants WHERE batch_id=?",
            (batch_id,),
        ).fetchall()
        if existing:
            if {(r[0], r[1], r[2]) for r in existing} != set(entries):
                raise ValueError("Existing batch differs; refusing to alter it")
            return existing[0][3]
        for username, tier, days in entries:
            db.execute(
                "INSERT INTO manual_access_grants "
                "(batch_id,username,tier,days,issued_at,paid_until) VALUES (?,?,?,?,?,?)",
                (batch_id, username, tier, days, now.isoformat(),
                 (now + timedelta(days=days)).isoformat()),
            )
    return now.isoformat()


def claim_grants(db_path, user_id, username, renewal_schedule):
    """Bind a current Telegram username to its ID exactly once, atomically.

    renewal_schedule is the running bot's next_renewal_schedule function.
    No payment is invented and an existing longer paid period is preserved.
    """
    if not username:
        return []
    try:
        username = normalize_username(username)
    except ValueError:
        return []
    user_id = str(user_id)
    now = datetime.now(timezone.utc)
    results = []
    with closing(sqlite3.connect(db_path, timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        # Old databases have no grant table until the operator imports a batch.
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_access_grants'"
        ).fetchone():
            return []
        grants = db.execute(
            "SELECT * FROM manual_access_grants WHERE username=? AND claimed_at IS NULL",
            (username,),
        ).fetchall()
        for grant in grants:
            current = db.execute(
                "SELECT * FROM subscriptions WHERE user_id=?", (user_id,),
            ).fetchone()
            until = _datetime(grant["paid_until"])
            tier = grant["tier"]
            if current and current["status"] == "active":
                if not current["paid_until"]:
                    raise ValueError("Refusing to replace existing unlimited access")
                current_until = _datetime(current["paid_until"])
                if current_until > until:
                    until, tier = current_until, current["tier"]
            schedule = renewal_schedule(until.isoformat(), 1)
            stage, next_at = (schedule[0] - 1, schedule[1]) if schedule else (5, None)
            status = "active" if until > now else "expired"
            db.execute(
                """INSERT INTO subscriptions
                   (user_id,status,tier,paid_until,funnel_step,funnel_next_at,
                    renewal_reminder_sent,renewal_stage,renewal_next_at,
                    blocked,created_at,updated_at)
                   VALUES (?,?,?,?,0,NULL,0,?,?,0,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     status=excluded.status,tier=excluded.tier,paid_until=excluded.paid_until,
                     funnel_next_at=NULL,renewal_reminder_sent=0,
                     renewal_stage=excluded.renewal_stage,renewal_next_at=excluded.renewal_next_at,
                     updated_at=excluded.updated_at""",
                (user_id, status, tier, until.isoformat(), stage, next_at,
                 now.isoformat(), now.isoformat()),
            )
            db.execute(
                "UPDATE manual_access_grants SET user_id=?,claimed_at=? "
                "WHERE batch_id=? AND username=? AND claimed_at IS NULL",
                (user_id, now.isoformat(), grant["batch_id"], username),
            )
            result = {
                "batch_id": grant["batch_id"], "username": username,
                "tier": tier, "issued_at": grant["issued_at"],
                "grant_paid_until": grant["paid_until"], "paid_until": until.isoformat(),
                "renewal_stage": stage, "renewal_next_at": next_at,
            }
            db.execute(
                "INSERT INTO events (user_id,event_type,metadata_json,created_at) VALUES (?,?,?,?)",
                (user_id, "manual_access_granted", json.dumps(result), now.isoformat()),
            )
            results.append(result)
    return results
