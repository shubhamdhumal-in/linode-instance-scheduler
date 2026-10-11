#!/usr/bin/env python3

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import functools
import json
import math
import getpass
import os
import re
import secrets
import shlex
import sqlite3
import sys
import tempfile
import shutil
import signal
import socket
import threading
import time
from collections.abc import Callable, Collection
from contextlib import contextmanager, redirect_stdout
from contextlib import suppress as contextlib_suppress
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal, TypeGuard
from zoneinfo import ZoneInfo

import ipaddress
import subprocess
import requests.exceptions
from dotenv import dotenv_values, load_dotenv
from linode_api4 import Instance, Volume
from linode_api4.errors import ApiError

import linode_engine as engine
import object_storage_backup as osb
from linode_engine import validate_instance_name


UTC = timezone.utc

REGISTRY_PATH = engine.BASE_DIR / "state" / "instances.db"
MIGRATIONS_PATH = engine.BASE_DIR / "state" / "migrations.json"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class InstanceLockedError(RuntimeError):
    pass


class NotOnboardedError(engine.ConfigError):
    pass


class NeedsManualRecoveryError(engine.ConfigError):
    pass


class DependencyNotSatisfiedError(engine.ConfigError):
    pass


class GroupNotFoundError(engine.ConfigError):
    pass


class _OnboardRefusal(Exception):
    pass


_INSTANCE_COLUMNS = (
    "name, label, region, network_interface_model, network_config, network_helper_enabled,"
    " vpc_prefix, os_volume_id, data_volumes, reserved_ip, authorized_keys, tags,"
    " instance_attrs, group_id, current_linode_id, current_status, transitioning,"
    " manual_override_expires_at"
)
_INSTANCE_PLACEHOLDERS = ", ".join(["?"] * 18)
_INSTANCE_COLUMN_LIST = [c.strip() for c in _INSTANCE_COLUMNS.split(",")]
_INSTANCE_UPDATE_CLAUSE = ", ".join(
    f"{c} = excluded.{c}" for c in _INSTANCE_COLUMN_LIST if c != "name"
)


def _migrate_stale_schedule_events_schema(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='schedule_events'"
    ).fetchone()
    if row is None or "REFERENCES instances" not in row[0]:
        return
    conn.execute("ALTER TABLE schedule_events RENAME TO schedule_events_pre_fk_fix")
    conn.execute(
        "CREATE TABLE schedule_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " instance_name TEXT NOT NULL,"
        " action TEXT NOT NULL CHECK (action IN ('create', 'delete')),"
        " triggered_by TEXT NOT NULL CHECK (triggered_by IN ('schedule', 'manual', 'api')),"
        " timestamp TIMESTAMP NOT NULL,"
        " result TEXT NOT NULL,"
        " error_message TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO schedule_events"
        " (id, instance_name, action, triggered_by, timestamp, result, error_message)"
        " SELECT id, instance_name, action, triggered_by, timestamp, result, error_message"
        " FROM schedule_events_pre_fk_fix"
    )
    conn.execute("DROP TABLE schedule_events_pre_fk_fix")
    conn.commit()


def _migrate_stale_schedules_schema(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='schedules'"
    ).fetchone()
    if row is None or "ON DELETE CASCADE" in row[0]:
        return
    conn.execute("ALTER TABLE schedules RENAME TO schedules_pre_cascade_fix")
    conn.execute(
        "CREATE TABLE schedules ("
        " instance_name TEXT PRIMARY KEY REFERENCES instances(name) ON DELETE CASCADE,"
        " timezone TEXT NOT NULL,"
        " rules TEXT NOT NULL DEFAULT '[]',"
        " enabled INTEGER NOT NULL DEFAULT 1"
        ")"
    )
    conn.execute(
        "INSERT INTO schedules (instance_name, timezone, rules, enabled)"
        " SELECT instance_name, timezone, rules, enabled FROM schedules_pre_cascade_fix"
    )
    conn.execute("DROP TABLE schedules_pre_cascade_fix")
    conn.commit()


def _migrate_stale_schedule_groups_schema(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='schedule_groups'"
    ).fetchone()
    if row is None or "UNIQUE" in row[0]:
        return


    conn.execute(
        "CREATE TABLE schedule_groups_unique_fix_new ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL UNIQUE,"
        " timezone TEXT NOT NULL,"
        " rules TEXT NOT NULL DEFAULT '[]',"
        " enabled INTEGER NOT NULL DEFAULT 1"
        ")"
    )
    conn.execute(
        "INSERT INTO schedule_groups_unique_fix_new (id, name, timezone, rules, enabled)"
        " SELECT id, name, timezone, rules, enabled FROM schedule_groups"
    )
    conn.execute("DROP TABLE schedule_groups")
    conn.execute("ALTER TABLE schedule_groups_unique_fix_new RENAME TO schedule_groups")
    conn.commit()


def _migrate_stale_instances_group_id_fk(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='instances'"
    ).fetchone()


    if row is None or "schedule_groups_pre_unique_fix" not in row[0]:
        return
    conn.execute(
        "CREATE TABLE instances_fk_fix_new ("
        " name TEXT PRIMARY KEY,"
        " label TEXT,"
        " region TEXT,"
        " network_interface_model TEXT"
        "     CHECK (network_interface_model IS NULL OR network_interface_model IN"
        "         ('legacy_config', 'linode')),"
        " network_config TEXT,"
        " network_helper_enabled INTEGER,"
        " vpc_prefix INTEGER,"
        " os_volume_id INTEGER,"
        " data_volumes TEXT,"
        " reserved_ip TEXT,"
        " authorized_keys TEXT,"
        " tags TEXT,"
        " instance_attrs TEXT,"
        " group_id INTEGER REFERENCES schedule_groups(id),"
        " current_linode_id INTEGER,"
        " current_status TEXT"
        "     CHECK (current_status IS NULL OR current_status IN"
        "         ('running', 'stopped', 'unreachable', 'needs_manual_recovery')),"
        " transitioning INTEGER NOT NULL DEFAULT 0,"
        " manual_override_expires_at TIMESTAMP"
        ")"
    )
    conn.execute(
        f"INSERT INTO instances_fk_fix_new ({_INSTANCE_COLUMNS})"
        f" SELECT {_INSTANCE_COLUMNS} FROM instances"
    )
    conn.execute("DROP TABLE instances")
    conn.execute("ALTER TABLE instances_fk_fix_new RENAME TO instances")
    conn.commit()


def _migrate_add_manual_override_column(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='instances'"
    ).fetchone()
    if row is None:
        return
    columns = {r[1] for r in conn.execute("PRAGMA table_info(instances)").fetchall()}
    if "manual_override_expires_at" in columns:
        return
    conn.execute("ALTER TABLE instances ADD COLUMN manual_override_expires_at TIMESTAMP")
    conn.commit()


def _migrate_add_account_holidays_columns(conn: sqlite3.Connection) -> None:

    for table, ddl in (
        ("schedule_groups", "ALTER TABLE schedule_groups ADD COLUMN account_holidays TEXT NOT NULL "
                            "DEFAULT 'follow'"),
        ("instance_settings", "ALTER TABLE instance_settings ADD COLUMN account_holidays TEXT"),
    ):
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if columns and "account_holidays" not in columns:
            conn.execute(ddl)
            conn.commit()


def _migrate_add_schedule_events_actor_column(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schedule_events'"
    ).fetchone()
    if row is None:
        return
    columns = {r[1] for r in conn.execute("PRAGMA table_info(schedule_events)").fetchall()}
    if "actor" in columns:
        return
    conn.execute("ALTER TABLE schedule_events ADD COLUMN actor TEXT")
    conn.commit()


def _migrate_add_group_dependency_column(conn: sqlite3.Connection) -> None:

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schedule_groups'"
    ).fetchone()
    if row is None:
        return
    columns = {r[1] for r in conn.execute("PRAGMA table_info(schedule_groups)").fetchall()}
    if "depends_on_group_id" in columns:
        return
    conn.execute(
        "ALTER TABLE schedule_groups ADD COLUMN depends_on_group_id INTEGER"
        " REFERENCES schedule_groups(id)"
    )
    conn.commit()


def _migrate_group_dependencies_table(conn: sqlite3.Connection) -> None:

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('schedule_groups', 'group_dependencies')"
    ).fetchall()}
    if tables != {"schedule_groups", "group_dependencies"}:
        return
    columns = {r[1] for r in conn.execute("PRAGMA table_info(schedule_groups)").fetchall()}
    if "depends_on_group_id" not in columns:
        return
    rows = conn.execute(
        "SELECT id, depends_on_group_id FROM schedule_groups WHERE depends_on_group_id IS NOT NULL"
    ).fetchall()
    if not rows:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO group_dependencies (group_id, depends_on_group_id) VALUES (?, ?)",
        [(gid, dep) for gid, dep in rows if gid != dep],
    )
    conn.execute("UPDATE schedule_groups SET depends_on_group_id = NULL")
    conn.commit()


def _connect() -> sqlite3.Connection:

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(REGISTRY_PATH, timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _migrate_stale_schedule_events_schema(conn)
        _migrate_add_schedule_events_actor_column(conn)
        _migrate_stale_schedules_schema(conn)
        _migrate_stale_schedule_groups_schema(conn)
        _migrate_add_manual_override_column(conn)
        _migrate_stale_instances_group_id_fk(conn)
        _migrate_add_group_dependency_column(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA_PATH.read_text())

        _migrate_group_dependencies_table(conn)
        _migrate_add_account_holidays_columns(conn)
        _ensure_change_triggers(conn)
    except sqlite3.DatabaseError as e:
        conn.close()
        if isinstance(e, sqlite3.OperationalError):
            raise
        raise engine.ConfigError(
            f"the local registry database at {REGISTRY_PATH} appears corrupted or unreadable "
            f"({e}). Recovering local records from Linode's own tags needs a working, even if "
            f"empty, local database first -- move the corrupted file aside (e.g. `mv "
            f"{REGISTRY_PATH} {REGISTRY_PATH}.corrupted`) and re-run this command; a fresh, "
            f"empty database will be created automatically, and `rebuild` can then reconstruct "
            f"every managed instance's record from its Linode tags."
        ) from e
    return conn


_CHANGE_TRACKED_TABLES = (
    "instances", "schedule_groups", "group_dependencies", "schedules", "schedule_events",
    "migrations", "orphaned_migration_attempts", "instance_settings", "instance_hooks",
    "group_hooks", "hook_events", "activity_log", "migration_backups", "override_sources",
    "holidays", "deployment_secrets",
)

_API_TOKEN_TRACKED_COLUMNS = "token_hash, scopes, instances, groups, expires_at, revoked_at"


def _ensure_change_triggers(conn: sqlite3.Connection) -> None:
    bump = "UPDATE continuous_backup SET changes = changes + 1 WHERE id = 1;"
    statements = []
    for table in _CHANGE_TRACKED_TABLES:
        for event in ("INSERT", "UPDATE", "DELETE"):
            statements.append(
                f"CREATE TRIGGER IF NOT EXISTS lis_changes_{table}_{event.lower()} AFTER {event} "
                f"ON {table} BEGIN {bump} END;")
    for event in ("INSERT", "DELETE"):
        statements.append(
            f"CREATE TRIGGER IF NOT EXISTS lis_changes_api_tokens_{event.lower()} AFTER {event} "
            f"ON api_tokens BEGIN {bump} END;")
    statements.append(
        "CREATE TRIGGER IF NOT EXISTS lis_changes_api_tokens_update AFTER UPDATE OF "
        f"{_API_TOKEN_TRACKED_COLUMNS} ON api_tokens BEGIN {bump} END;")
    conn.executescript("\n".join(statements))


def _retry_db(fn, attempts=5, delay_s=0.2):

    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            transient = "locked" in str(e).lower() or "busy" in str(e).lower()
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(delay_s)


_NULLABLE_JSON_FIELDS = ("network_config", "authorized_keys", "tags", "instance_attrs")


def _record_from_row(row: tuple) -> dict:

    (
        _name, label, region, network_interface_model, network_config,
        network_helper_enabled, vpc_prefix, os_volume_id, data_volumes, reserved_ip,
        authorized_keys, tags, instance_attrs, group_id, current_linode_id, current_status,
        transitioning, manual_override_expires_at,
    ) = row
    raw = {
        "network_config": network_config,
        "authorized_keys": authorized_keys,
        "tags": tags,
        "instance_attrs": instance_attrs,
    }
    return {
        "label": label,
        "region": region,
        "network_interface_model": network_interface_model,
        **{k: (None if raw[k] is None else json.loads(raw[k])) for k in _NULLABLE_JSON_FIELDS},
        "network_helper_enabled": (
            None if network_helper_enabled is None else bool(network_helper_enabled)
        ),
        "vpc_prefix": vpc_prefix,
        "os_volume_id": os_volume_id,
        "data_volumes": None if data_volumes is None else json.loads(data_volumes),
        "reserved_ip": reserved_ip,
        "group_id": group_id,
        "current_linode_id": current_linode_id,
        "current_status": current_status,
        "transitioning": bool(transitioning),
        "manual_override_expires_at": manual_override_expires_at,
    }


_DEFAULT_RECORD: dict = {
    "label": "",
    "region": "",
    "network_interface_model": "legacy_config",
    "network_helper_enabled": None,
    "vpc_prefix": None,
    "os_volume_id": 0,
    "data_volumes": [],
    "reserved_ip": "",
    "group_id": None,
    "current_linode_id": None,
    "current_status": "stopped",
    "transitioning": False,
    "manual_override_expires_at": None,
}


def _sqlite_safe(value):

    if value is None or isinstance(value, (int, float, str, bytes)):
        return value
    return str(value)


def _row_values(name: str, record: dict) -> tuple:

    merged = {**_DEFAULT_RECORD, **record}
    network_helper_enabled = merged["network_helper_enabled"]
    nullable_json = {
        k: (None if k not in record else json.dumps(record[k], default=str))
        for k in _NULLABLE_JSON_FIELDS
    }
    return (
        name,
        _sqlite_safe(merged["label"]),
        _sqlite_safe(merged["region"]),
        _sqlite_safe(merged["network_interface_model"]),
        nullable_json["network_config"],
        None if network_helper_enabled is None else int(bool(network_helper_enabled)),
        _sqlite_safe(merged["vpc_prefix"]),
        _sqlite_safe(merged["os_volume_id"]),
        json.dumps(merged["data_volumes"], default=str),
        _sqlite_safe(merged["reserved_ip"]),
        nullable_json["authorized_keys"],
        nullable_json["tags"],
        nullable_json["instance_attrs"],
        _sqlite_safe(merged["group_id"]),
        _sqlite_safe(merged["current_linode_id"]),
        _sqlite_safe(merged["current_status"]),
        int(bool(merged["transitioning"])),
        _sqlite_safe(merged["manual_override_expires_at"]),
    )


def load_registry() -> dict:
    def _do():
        conn = _connect()
        try:
            return conn.execute(f"SELECT {_INSTANCE_COLUMNS} FROM instances").fetchall()
        finally:
            conn.close()

    rows = _retry_db(_do)
    return {row[0]: _record_from_row(row) for row in rows}


def save_registry(registry: dict) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM instances")
            for name, record in registry.items():
                conn.execute(
                    f"INSERT INTO instances ({_INSTANCE_COLUMNS})"
                    f" VALUES ({_INSTANCE_PLACEHOLDERS})",
                    _row_values(name, record),
                )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _save_one_record(name: str, record: dict) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute(
                f"INSERT INTO instances ({_INSTANCE_COLUMNS}) VALUES ({_INSTANCE_PLACEHOLDERS}) "
                f"ON CONFLICT(name) DO UPDATE SET {_INSTANCE_UPDATE_CLAUSE}",
                _row_values(name, record),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _delete_one_record(name: str) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM instances WHERE name = ?", (name,))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


@contextmanager
def _instance_lock(name: str):

    locks_dir = REGISTRY_PATH.parent / "locks"


    reached_open = False
    try:


        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_path = locks_dir / f"{name}.lock"
        reached_open = True
        lock_file = open(lock_path, "w")


    except (OSError, ValueError) as e:


        if reached_open and (isinstance(e, ValueError) or e.errno == errno.ENAMETOOLONG):
            raise engine.ConfigError(f"'{name}' is not a usable instance name: {e}") from e
        raise engine.ConfigError(
            f"could not create the lock file for '{name}' ({e}) -- this is a local filesystem "
            f"problem (permissions, disk space, or similar under {locks_dir}), not necessarily "
            "anything wrong with the name itself."
        ) from e
    with lock_file as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstanceLockedError(
                f"Another start/stop/rebuild/clear-lock process is already operating on "
                f"'{name}' right now. Wait for it to finish and try again."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


_MIGRATIONS_COLUMNS = "name, phase, checkpoint, updated_at"


def _now() -> str:
    return datetime.now(UTC).isoformat()


ACTIVITY_LOG_ENABLED = True
DEFAULT_ACTIVITY_LOG_RETENTION_DAYS = 30
ACTIVITY_LOG_MAX_MESSAGE = 4000
_ACTIVITY_PRUNE_EVERY_S = 3600
_last_activity_prune = 0.0


def activity_retention_days() -> int:

    try:
        days = int(os.environ.get("ACTIVITY_LOG_RETENTION_DAYS") or DEFAULT_ACTIVITY_LOG_RETENTION_DAYS)
    except ValueError:
        return DEFAULT_ACTIVITY_LOG_RETENTION_DAYS
    return days if days > 0 else DEFAULT_ACTIVITY_LOG_RETENTION_DAYS


def record_activity(
    message: str, *, level: Literal["info", "warning", "error"] = "info", source: str = "system",
    instance_name: str | None = None, action: str | None = None, actor: str | None = None,
) -> None:

    if not ACTIVITY_LOG_ENABLED or not message or not str(message).strip():
        return
    text = str(message).rstrip()[:ACTIVITY_LOG_MAX_MESSAGE]

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO activity_log (timestamp, level, source, instance_name, action, actor,"
                " message) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now(), level, source, instance_name, action, actor, text),
            )
            conn.commit()
        finally:
            conn.close()

    with contextlib_suppress(Exception):
        _retry_db(_do)


def _message_level(message: str) -> Literal["info", "warning", "error"]:
    head = message.lstrip().upper()
    if head.startswith(("ERROR", "SECURITY WARNING", "CONFIGURATION ERROR")):
        return "error"
    return "warning" if head.startswith("WARNING") else "info"


_FAILED_OUTCOME_WORDS = ("fail", "error", "incomplete", "refused", "aborted")


def _outcome_level(outcome: str) -> Literal["info", "warning", "error"]:
    return "error" if any(w in outcome for w in _FAILED_OUTCOME_WORDS) else "info"


def _logs_activity(action: str, *, name_arg: int = 1):

    import functools
    import inspect

    def decorate(fn):
        params = inspect.signature(fn).parameters

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not ACTIVITY_LOG_ENABLED:
                return fn(*args, **kwargs)
            name = kwargs.get("name", args[name_arg] if len(args) > name_arg else None)
            source = kwargs.get("triggered_by") or (
                params["triggered_by"].default if "triggered_by" in params else "manual"
            )
            actor = kwargs.get("actor")

            def log(message, level=None):
                record_activity(
                    message, level=level or _message_level(str(message)), source=source,
                    instance_name=name, action=action, actor=actor,
                )

            if "on_progress" in params:
                caller_progress = kwargs.get("on_progress")

                def on_progress(message):
                    log(message)
                    if caller_progress is not None:
                        caller_progress(message)
                kwargs["on_progress"] = on_progress
            if "on_warning" in params:
                caller_warning = kwargs.get("on_warning")

                def on_warning(message):
                    level = _message_level(str(message))
                    log(message, "error" if level == "error" else "warning")
                    if caller_warning is not None:
                        caller_warning(message)
                kwargs["on_warning"] = on_warning
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                log(f"{action} failed: {e}", "error")
                raise
            outcome = getattr(result, "outcome", None)
            if isinstance(outcome, str):
                detail = getattr(result, "detail", None)
                log(f"{action}: {outcome}" + (f" -- {detail}" if detail else ""), _outcome_level(outcome))
            return result
        return wrapper
    return decorate


def write_scheduler_heartbeat(
    *, interval_seconds: int | None, instances_checked: int, fired: int, failed: int,
) -> None:

    global _last_activity_prune
    if not ACTIVITY_LOG_ENABLED:
        return

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO scheduler_heartbeat (id, last_tick, interval_seconds,"
                " instances_checked, fired, failed) VALUES (1, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET last_tick = excluded.last_tick,"
                " interval_seconds = excluded.interval_seconds,"
                " instances_checked = excluded.instances_checked, fired = excluded.fired,"
                " failed = excluded.failed",
                (_now(), interval_seconds, instances_checked, fired, failed),
            )
            if time.monotonic() - _last_activity_prune > _ACTIVITY_PRUNE_EVERY_S:
                cutoff = (datetime.now(UTC) - timedelta(days=activity_retention_days())).isoformat()
                conn.execute("DELETE FROM activity_log WHERE timestamp < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()

    try:
        _retry_db(_do)
        if time.monotonic() - _last_activity_prune > _ACTIVITY_PRUNE_EVERY_S:
            _last_activity_prune = time.monotonic()
            prune_console_sessions()
    except Exception:
        pass


def get_scheduler_heartbeat() -> dict | None:
    def _do():
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT last_tick, interval_seconds, instances_checked, fired, failed"
                " FROM scheduler_heartbeat WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        return row

    row = _retry_db(_do)
    if row is None:
        return None
    keys = ("last_tick", "interval_seconds", "instances_checked", "fired", "failed")
    return dict(zip(keys, row, strict=True))


MAX_ACTIVITY_LIMIT = 1000


def get_activity(
    *, names: list[str] | None = None, level: str | None = None, source: str | None = None,
    query: str | None = None, after_id: int | None = None, before_id: int | None = None,
    limit: int = 200, include_unscoped: bool = True,
) -> list[dict]:

    if limit < 1 or limit > MAX_ACTIVITY_LIMIT:
        raise engine.ConfigError(f"limit must be from 1 to {MAX_ACTIVITY_LIMIT}.")
    levels = {"info": ("info", "warning", "error"), "warning": ("warning", "error"),
              "error": ("error",)}
    if level is not None and level not in levels:
        raise engine.ConfigError("level must be info, warning or error.")
    where: list[str] = []
    values: list[object] = []
    if names is not None:
        placeholders = ",".join("?" * len(names))
        clause = f"instance_name IN ({placeholders})" if names else "0"
        if include_unscoped:
            clause = f"({clause} OR instance_name IS NULL)"
        where.append(clause)
        values += names
    if level is not None:
        where.append(f"level IN ({','.join('?' * len(levels[level]))})")
        values += levels[level]
    if source is not None:
        where.append("source = ?")
        values.append(source)
    if query:
        where.append("instr(lower(message), lower(?)) > 0")
        values.append(query)
    if after_id is not None:
        where.append("id > ?")
        values.append(after_id)
    if before_id is not None:
        where.append("id < ?")
        values.append(before_id)
    sql = ("SELECT id, timestamp, level, source, instance_name, action, actor, message"
           " FROM activity_log" + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY id DESC LIMIT ?")
    values.append(limit)

    def _do():
        conn = _connect()
        try:
            return conn.execute(sql, values).fetchall()
        finally:
            conn.close()

    keys = ("id", "timestamp", "level", "source", "instance_name", "action", "actor", "message")
    return [dict(zip(keys, row, strict=True)) for row in _retry_db(_do)]


def load_migrations() -> dict:
    def _do():
        conn = _connect()
        try:
            return conn.execute("SELECT name, checkpoint FROM migrations").fetchall()
        finally:
            conn.close()

    rows = _retry_db(_do)
    return {name: json.loads(checkpoint) for name, checkpoint in rows}


def save_migrations(migrations: dict) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM migrations")
            for name, state in migrations.items():
                conn.execute(
                    f"INSERT INTO migrations ({_MIGRATIONS_COLUMNS}) VALUES (?, ?, ?, ?)",
                    (
                        name, _sqlite_safe(state.get("phase")),
                        json.dumps(state, default=str), _now(),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _save_one_migration(name: str, state: dict) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO migrations (name, phase, checkpoint, updated_at)"
                " VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET phase = excluded.phase, "
                "checkpoint = excluded.checkpoint, updated_at = excluded.updated_at",
                (
                    name, _sqlite_safe(state.get("phase")),
                    json.dumps(state, default=str), _now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _delete_one_migration(name: str) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM migrations WHERE name = ?", (name,))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _record_schedule_event(
    name: str, action: Literal["create", "delete"],
    triggered_by: Literal["schedule", "manual", "api"],
    result: Literal["success", "failure"], error_message: str | None = None,
    actor: str | None = None,
) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO schedule_events"
                " (instance_name, action, triggered_by, timestamp, result, error_message, actor)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, action, triggered_by, _now(), result, error_message, actor),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


_START_EVENT_RESULTS: dict[str, Literal["success", "failure"]] = {
    "started": "success",
    "reachability_check_failed": "failure",
    "create_failed": "failure",
}
_STOP_EVENT_RESULTS: dict[str, Literal["success", "failure"]] = {
    "stopped": "success",
    "confirmed_gone_reset_to_stopped": "success",
    "delete_failed": "failure",
    "prepare_failed": "failure",
    "pre_stop_hook_failed": "failure",
}


def _record_event(
    name: str, result: StartResult | StopResult, action: Literal["create", "delete"],
    outcome_map: dict[str, Literal["success", "failure"]],
    triggered_by: Literal["schedule", "manual", "api"],
    on_warning: Callable[[str], None] | None, actor: str | None,
) -> None:

    event_result = outcome_map.get(result.outcome)
    if event_result is None:
        return
    try:
        _record_schedule_event(name, action, triggered_by, event_result, result.detail, actor)
    except Exception as e:


        if on_warning is not None:
            on_warning(f"  WARNING: could not record audit event for '{name}': {e}")


def _record_start_event(
    name: str, result: StartResult, triggered_by: Literal["schedule", "manual", "api"],
    on_warning: Callable[[str], None] | None, actor: str | None = None,
) -> None:
    _record_event(name, result, "create", _START_EVENT_RESULTS, triggered_by, on_warning, actor)


def _record_stop_event(
    name: str, result: StopResult, triggered_by: Literal["schedule", "manual", "api"],
    on_warning: Callable[[str], None] | None, actor: str | None = None,
) -> None:
    _record_event(name, result, "delete", _STOP_EVENT_RESULTS, triggered_by, on_warning, actor)


_MAX_HISTORY_LIMIT = 10_000


def get_schedule_events(name: str, limit: int = 50) -> list[dict]:

    if limit < 0:
        raise engine.ConfigError(f"limit must be 0 or greater, got {limit}.")
    if limit > _MAX_HISTORY_LIMIT:
        raise engine.ConfigError(f"limit must be {_MAX_HISTORY_LIMIT} or less, got {limit}.")

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT action, triggered_by, timestamp, result, error_message, actor"
                " FROM schedule_events WHERE instance_name = ? ORDER BY timestamp DESC, id DESC"
                " LIMIT ?",
                (name, limit),
            ).fetchall()
        finally:
            conn.close()

    rows = _retry_db(_do)
    return [
        {
            "action": r[0], "triggered_by": r[1], "timestamp": r[2], "result": r[3],
            "error_message": r[4], "actor": r[5],
        }
        for r in rows
    ]


def get_schedule_events_since(name: str, since: datetime) -> list[dict]:

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT action, triggered_by, timestamp, result, error_message"
                " FROM schedule_events WHERE instance_name = ? AND timestamp >= ?"
                " ORDER BY timestamp ASC, id ASC",
                (name, since.isoformat()),
            ).fetchall()
        finally:
            conn.close()

    rows = _retry_db(_do)
    return [
        {"action": r[0], "triggered_by": r[1], "timestamp": r[2], "result": r[3], "error_message": r[4]}
        for r in rows
    ]


def event_already_succeeded_today(
    name: str, action: Literal["create", "delete"], tz: str = "UTC",
) -> bool:

    zone = ZoneInfo(tz)
    today = datetime.now(zone).date()
    for event in get_schedule_events(name, limit=200):
        if event["result"] != "success" or event["action"] not in ("create", "delete"):
            continue
        if event["action"] != action:
            return False
        return datetime.fromisoformat(event["timestamp"]).astimezone(zone).date() == today
    return False


def transition_recorded_since(name: str, since: datetime) -> bool:

    for event in get_schedule_events(name, limit=200):
        if event["result"] != "success" or event["action"] not in ("create", "delete"):
            continue


        return datetime.fromisoformat(event["timestamp"]) >= since
    return False


def load_orphan_archive() -> dict:

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT name, detail FROM orphaned_migration_attempts ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    rows = _retry_db(_do)
    archive: dict = {}
    for name, detail in rows:
        archive.setdefault(name, []).append(json.loads(detail))
    return archive


def save_orphan_archive(archive: dict) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM orphaned_migration_attempts")
            now = _now()
            for name, attempts in archive.items():
                for attempt in attempts:
                    conn.execute(
                        "INSERT INTO orphaned_migration_attempts"
                        " (name, dest_volume_id, archived_at, detail) VALUES (?, ?, ?, ?)",
                        (
                            name, _sqlite_safe(attempt.get("dest_volume_id")), now,
                            json.dumps(attempt, default=str),
                        ),
                    )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _archive_previous_attempts(
    client, name: str, previous_attempts: list,
    on_warning: Callable[[str], None] | None = None,
) -> None:

    if not previous_attempts:
        return


    def _do():
        conn = _connect()
        try:
            existing_ids = {
                row[0] for row in conn.execute(
                    "SELECT dest_volume_id FROM orphaned_migration_attempts WHERE name = ?",
                    (name,),
                ).fetchall()
            }
            new_attempts = [
                a for a in previous_attempts if a.get("dest_volume_id") not in existing_ids
            ]
            now = _now()
            for attempt in new_attempts:
                conn.execute(
                    "INSERT INTO orphaned_migration_attempts"
                    " (name, dest_volume_id, archived_at, detail) VALUES (?, ?, ?, ?)",
                    (
                        name, _sqlite_safe(attempt.get("dest_volume_id")), now,
                        json.dumps(attempt, default=str),
                    ),
                )
            conn.commit()
            return new_attempts
        finally:
            conn.close()

    new_attempts = _retry_db(_do)

    for attempt in new_attempts:
        vol_id = attempt.get("dest_volume_id")
        if vol_id is None:
            continue
        try:
            engine.tag_orphaned_migration_volume(client, vol_id, name)
        except (ApiError, engine.ConfigError, requests.exceptions.RequestException) as e:


            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not tag orphaned volume {vol_id} for disaster "
                    f"recovery ({e}) -- it's still recorded in the local archive."
                )


@dataclass
class MigrateStartResult:

    outcome: Literal["started", "identity_change_declined", "backup_declined"]
    instance_id: int | None = None
    backup_instance_id: int | None = None
    dest_volume_id: int | None = None
    dest_volume_size_gb: int | None = None
    local_disk_size_mb: int | None = None
    dest_volume_label: str | None = None


VLAN_ONLY_CLOUD_CFG = "/etc/cloud/cloud.cfg.d/99-linode-instance-scheduler.cfg"


def _is_vlan_only(network_config: list[dict] | None, model: str | None) -> bool:

    config = network_config or []
    if not config:
        return False
    if model == engine.INTERFACE_MODEL_LEGACY:
        return all(iface.get("purpose") == "vlan" for iface in config)
    return all(iface.get("vlan") and not iface.get("vpc") and not iface.get("public")
               for iface in config)


def _append_authorized_key_command(public_key: str) -> str:

    return (
        "(tail -c1 /root/.ssh/authorized_keys 2>/dev/null | read -r _ || "
        "printf '\\n' >> /root/.ssh/authorized_keys); "
        f"echo {shlex.quote(public_key)} >> /root/.ssh/authorized_keys"
    )


KEEP_HOST_KEYS_COMMAND = (
    f"mkdir -p /etc/cloud/cloud.cfg.d && printf '%s\\n' "
    f"'# Written by linode-instance-scheduler: keep SSH host keys across recreates.' "
    f"'ssh_deletekeys: false' > {VLAN_ONLY_CLOUD_CFG}"
)


def _take_pre_migration_backup(
    client, name: str, instance, confirm_backup: Callable[[dict | None], bool] | None,
    on_progress: Callable[[str], None] | None, on_warning: Callable[[str], None] | None,
    *, host: str | None = None, ssh_key: str | None = None, ssh_password: str | None = None,
) -> int | None:

    def say(msg: str) -> None:
        if on_progress is not None:
            on_progress(msg)

    if len(engine.migration_backup_tag(name)) > engine.MAX_TAG_LENGTH:
        raise engine.ConfigError(
            f"'{name}' is too long for the backup tag (Linode tags are at most "
            f"{engine.MAX_TAG_LENGTH} characters) -- use a shorter name. Nothing was changed.")
    existing = get_migration_backup(name)
    if existing is not None and existing["status"] in ("kept", "creating", "failed"):
        rec = existing["record"]
        if (existing["status"] == "kept"
                and rec.get("original", {}).get("instance_id") == instance.id):
            say(f"Reusing the existing backup of instance {instance.id} (backup instance "
                f"{rec['backup'].get('instance_id')}, taken {rec.get('created_at')}).")
            return rec["backup"].get("instance_id")
        raise engine.ConfigError(
            f"a pre-migration backup for '{name}' already exists (status {existing['status']}, "
            f"of instance {rec.get('original', {}).get('instance_id')}). Remove it first "
            f"(`backup-delete --name {name}`).")
    configs_raw = engine.retry_transient(
        lambda: client.get(f"/linode/instances/{instance.id}/configs")).get("data") or []
    sizes = []
    for entry in engine.attached_volumes((configs_raw[0] if configs_raw else {}).get("devices") or {}):
        sizes.append(engine.retry_transient(lambda v=entry["volume_id"]: client.load(Volume, v)).size)
    try:
        estimate = engine.estimate_migration_backup_cost(client, instance, sizes)
    except Exception as e:
        estimate = None
        if on_warning is not None:
            on_warning(f"WARNING: couldn't price the backup from Linode's price list ({e}).")
    say(f"A pre-migration backup keeps a powered-off native clone of instance {instance.id} and of "
        f"its {len(sizes)} attached volume(s). It is a billable resource: {_format_cost(estimate)}, "
        "until you delete it (`backup-delete`).")
    if confirm_backup is not None and not confirm_backup(estimate):
        return None
    if host:


        try:
            engine.ssh_run(host, ssh_key, KEEP_HOST_KEYS_COMMAND, trust_new=True,
                           password=ssh_password)
        except Exception as e:
            if on_warning is not None:
                on_warning(f"WARNING: could not set cloud-init to keep SSH host keys ({e}); after a "
                           "rollback the restored system may present new SSH host keys.")
    say(f"Powering off instance {instance.id} for a consistent copy...")
    engine.retry_transient_or_already_done(
        instance.shutdown,
        already_done=lambda: client.load(Instance, instance.id).status == "offline")
    engine.poll_until_status(lambda: client.load(Instance, instance.id), ("offline",),
                             timeout_s=900)
    instance = engine.retry_transient(lambda: client.load(Instance, instance.id))

    def _checkpoint(record: dict) -> None:
        record["estimated_monthly_cost"] = estimate
        _save_migration_backup(name, "creating", record)

    try:
        record = engine.create_migration_backup(client, instance, name=name, on_record=_checkpoint,
                                                on_progress=on_progress)
    except Exception as e:
        partial = get_migration_backup(name)
        if partial is not None:
            _save_migration_backup(name, "failed", partial["record"])
            _sync_migration_backup_record(name, on_warning)
        with contextlib_suppress(Exception):
            engine.retry_transient_or_already_done(
                instance.boot,
                already_done=lambda: client.load(Instance, instance.id).status in ("booting", "running"))
        raise engine.ConfigError(
            f"the pre-migration backup failed ({e}). Nothing was migrated; instance {instance.id} "
            f"was booted again. Remove any partial backup with `backup-delete --name {name}`, then "
            "retry.") from e
    record["estimated_monthly_cost"] = estimate
    _save_migration_backup(name, "kept", record)
    _sync_migration_backup_record(name, on_warning)
    say(f"Backup kept: powered-off instance {record['backup']['instance_id']} "
        f"('{record['backup']['label']}') with {len(record['backup']['volumes'])} cloned "
        f"volume(s). Roll back any time with `rollback --name {name}`.")
    return record["backup"]["instance_id"]


@_logs_activity("migrate-start")
def migrate_start_instance(
    client, name: str, instance_id: int, ssh_key: str | None, *,
    force: bool = False,
    ssh_password: str | None = None,
    install_public_key: str | None = None,
    confirm_identity_change: Callable[[], bool] | None = None,
    backup: bool = False,
    confirm_backup: Callable[[dict | None], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> MigrateStartResult:

    with _instance_lock(name):
        migrations = load_migrations()
        existing = migrations.get(name)
        if existing and not force:
            raise engine.ConfigError(
                f"a migration for '{name}' is already in progress "
                f"(phase={existing.get('phase')!r}). Use --force to restart it."
            )


        previous_attempts = []
        old_dest_volume_id = None
        if existing:
            previous_attempts = list(existing.get("previous_attempts", [])) + [
                {k: v for k, v in existing.items() if k != "previous_attempts"}
            ]
            old_dest_volume_id = existing.get("dest_volume_id")


            old_instance_id = existing.get("instance_id")
            if old_instance_id is not None and old_instance_id != instance_id:
                if on_progress is not None:


                    on_progress(
                        f"WARNING: --force restarting '{name}' at a DIFFERENT --instance-id "
                        "than the in-progress attempt being replaced:\n"
                        f"  instance id: {old_instance_id} -> {instance_id}\n"
                        "If this is unexpected, double-check --instance-id -- a typo here "
                        f"would abandon the real in-progress migration for '{name}' and start "
                        "a new one against unrelated infrastructure instead."
                    )
                if confirm_identity_change is not None and not confirm_identity_change():
                    return MigrateStartResult(outcome="identity_change_declined")

        try:


            instance = engine.retry_transient(lambda: client.load(Instance, instance_id))
        except (ApiError, requests.exceptions.RequestException) as e:


            raise engine.ConfigError(f"instance {instance_id} not found: {e}") from e

        try:
            engine.check_region_capabilities(client, instance.region.id)
        except engine.ConfigError as e:
            raise engine.ConfigError(
                f"instance {instance_id} is in an unsupported region: {e} This tool requires "
                "Block Storage (the OS must move onto a volume) -- reserved IPs are permanently "
                "locked to the region they were first reserved in, so a migration can't move an "
                "instance to a region that doesn't support it."
            ) from e
        except (ApiError, requests.exceptions.RequestException) as e:


            raise engine.ConfigError(
                f"could not check region capabilities for instance {instance_id}: {e}"
            ) from e

        if instance.status != "running":
            raise engine.ConfigError(
                f"instance {instance_id} is not running (status={instance.status!r}) -- must "
                "be running for pre-flight checks."
            )


        try:
            configs = list(instance.configs)
        except (ApiError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(f"could not read the instance's boot configs: {e}") from e
        if not configs:
            raise engine.ConfigError(f"Instance {instance.id} has no boot config.")
        host = engine.live_ssh_address(instance, configs)
        if not host:
            raise engine.ConfigError(
                "instance has no public interface and no static VPC/VLAN address -- nothing to "
                "reach it by over SSH."
            )

        if on_progress is not None:
            on_progress("Running pre-flight checks (cloud-init version, datasource, networking)...")
        _forget_unowned_host_key(host, on_progress)
        try:
            preflight = engine.check_path_b_preflight(host, ssh_key, password=ssh_password)
        except (engine.ConfigError, ApiError, RuntimeError) as e:
            raise engine.ConfigError(f"pre-flight checks failed: {e}") from e
        if on_progress is not None:
            if not preflight.get("cloud_init_installed", True):
                on_progress("  cloud-init: NOT INSTALLED")
            else:
                on_progress(f"  cloud-init: {preflight['cloud_init_version']} "
                            f"({'OK' if preflight['cloud_init_ok'] else 'TOO OLD, need >= 23.3.1'})")
                on_progress(f"  datasource: {preflight['datasource']} "
                            f"({'OK' if preflight['datasource_ok'] else 'NOT akamai'})")
        if not preflight["cloud_init_ok"] or not preflight["datasource_ok"]:


            reasons = []
            if not preflight.get("cloud_init_installed", True):
                reasons.append(
                    "cloud-init is not installed on this instance. This tool needs cloud-init "
                    "(>= 23.3.1, or a build with the Akamai datasource) to re-apply the network "
                    "configuration every time the instance is recreated. Install it, then retry; "
                    "images that cannot run cloud-init are not supported"
                )
            elif not preflight["cloud_init_ok"]:
                reasons.append(
                    f"cloud-init is {preflight['cloud_init_version']!r} (need >= 23.3.1) -- "
                    f"upgrade cloud-init on this instance, then retry. "
                    f"{engine.CLOUD_INIT_UPGRADE_INSTRUCTIONS}"
                )
            if preflight.get("cloud_init_installed", True) and not preflight["datasource_ok"]:
                reasons.append(
                    f"the datasource is {preflight['datasource']!r}, not 'akamai' -- this is "
                    "not a Linode/Akamai-managed cloud-init datasource, and there's no proven "
                    "fallback for this tool's network fix without it"
                )
            raise engine.ConfigError(
                "this instance does not meet the requirements for automated migration: "
                + "; ".join(reasons) + ". Not supported for now."
            )


        if len(configs) != 1:
            raise engine.ConfigError(
                f"Instance {instance.id} has {len(configs)} boot configs -- can't tell which "
                "one is the real one to migrate from (Linode's API has no such field) and "
                "refuses to guess. Delete or consolidate the extra config(s) manually first "
                "(Cloud Manager), then retry."
            )


        devices = configs[0].devices.dict
        sda = devices.get("sda")
        if not sda:
            raise engine.ConfigError(f"Instance {instance.id} has no device at /dev/sda.")
        if sda.get("filesystem_path"):
            raise engine.ConfigError(
                f"Instance {instance.id}'s /dev/sda is already a Block Storage volume -- "
                "nothing to migrate (Path B is for local-disk instances only)."
            )
        disk_id = sda.get("id")
        if disk_id is None:
            raise engine.ConfigError(f"Instance {instance.id}'s /dev/sda has no disk id.")
        try:
            disks = list(instance.disks)
        except (ApiError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(f"could not read the instance's disks: {e}") from e
        if not any(d.id == disk_id for d in disks):
            raise engine.ConfigError(f"Could not find disk {disk_id} on instance {instance.id}.")

        if install_public_key is not None:


            if on_progress is not None:
                on_progress(
                    "Installing this deployment's own SSH key so the rest of this migration "
                    "(and onboarding afterward) don't need the one-time credential again..."
                )
            try:
                current_keys = engine.ssh_run(
                    host, ssh_key, "cat /root/.ssh/authorized_keys 2>/dev/null", password=ssh_password,
                )
            except (ApiError, RuntimeError, requests.exceptions.RequestException) as e:
                raise engine.ConfigError(f"could not read authorized_keys: {e}") from e
            if install_public_key not in current_keys:
                try:
                    engine.ssh_run(
                        host, ssh_key, _append_authorized_key_command(install_public_key),
                        password=ssh_password,
                    )
                except (ApiError, RuntimeError, requests.exceptions.RequestException) as e:
                    raise engine.ConfigError(f"could not install this deployment's key: {e}") from e
        if on_progress is not None:
            if preflight["hand_configured_networking"]:


                suspect_lines = "\n".join(
                    f"    - {suspect['path']}: {suspect['reason']}"
                    for suspect in preflight["hand_configured_networking"]
                )
                on_progress(
                    "  WARNING: hand-configured networking found outside Network Helper:\n"
                    f"{suspect_lines}\n"
                    "  This will be overwritten by the network fix once this instance "
                    "is onboarded and recreated. Continuing, since this is "
                    "informational, not a hard block -- review before going further "
                    "if that's a problem."
                )
            else:
                on_progress("  networking: clean (no hand-configured files found)")


            if existing:
                on_progress(
                    f"  NOTE: '{name}' had an incomplete migration attempt "
                    f"(phase={existing.get('phase')!r}"
                    + (f", dest_volume_id={existing['dest_volume_id']!r}"
                       if "dest_volume_id" in existing else "")
                    + "). That volume is NOT deleted automatically -- check Cloud Manager and "
                    "clean it up manually if it's no longer needed. The old attempt's IDs are "
                    f"preserved under '{name}' in the local registry database, not discarded."
                )

            on_progress(f"Starting migration to Block Storage for '{name}' (instance {instance.id})...")

        old_volume_retag_done = False

        def _persist_migration_checkpoint(partial_state: dict) -> None:


            nonlocal old_volume_retag_done
            partial_state["name"] = name
            if previous_attempts:
                partial_state["previous_attempts"] = previous_attempts
            _save_one_migration(name, partial_state)


            if (
                old_dest_volume_id is not None
                and not old_volume_retag_done
                and partial_state.get("dest_volume_id") is not None
            ):


                try:
                    engine.retag_migration_volume_as_orphaned(client, old_dest_volume_id, name)
                    old_volume_retag_done = True
                except (ApiError, engine.ConfigError, requests.exceptions.RequestException) as e:


                    if on_warning is not None:
                        on_warning(f"  WARNING: could not re-tag orphaned volume "
                                   f"{old_dest_volume_id} ({e}) -- will retry on the next "
                                   "checkpoint; still recorded in previous_attempts above.")

        backup_instance_id = None
        if backup:
            backup_instance_id = _take_pre_migration_backup(
                client, name, instance, confirm_backup, on_progress, on_warning,
                host=host, ssh_key=ssh_key, ssh_password=ssh_password)
            if backup_instance_id is None:
                return MigrateStartResult(outcome="backup_declined")
            instance = engine.retry_transient(lambda: client.load(Instance, instance_id))
        try:
            if backup:
                migration_state = engine.start_path_b_migration(
                    client, instance, name=name, persist_fn=_persist_migration_checkpoint,
                    allow_offline=True,
                )
            else:
                migration_state = engine.start_path_b_migration(
                    client, instance, name=name, persist_fn=_persist_migration_checkpoint,
                )
        except (ApiError, RuntimeError, TimeoutError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(
                f"migration setup failed partway through: {e}. Whatever succeeded so far "
                f"(possibly including a billable destination volume) was saved to the local "
                f"registry database under '{name}' -- check there and in Cloud Manager before "
                "retrying with --force."
            ) from e
        migration_state["name"] = name
        if previous_attempts:
            migration_state["previous_attempts"] = previous_attempts
        if old_dest_volume_id is not None:
            migration_state["replaced_dest_volume_id"] = old_dest_volume_id
        _save_one_migration(name, migration_state)


        if old_dest_volume_id is not None and not old_volume_retag_done:
            try:
                engine.retag_migration_volume_as_orphaned(client, old_dest_volume_id, name)
            except (ApiError, engine.ConfigError, requests.exceptions.RequestException) as e:
                if on_warning is not None:
                    on_warning(f"  WARNING: could not re-tag orphaned volume "
                               f"{old_dest_volume_id} ({e}) -- it's still recorded in "
                               "previous_attempts above.")

        if not migration_state.get("remote_tagged", True) and on_warning is not None:
            on_warning(f"  WARNING: could not remotely tag destination volume "
                       f"{migration_state['dest_volume_id']} as an active migration attempt -- "
                       "it's still recorded locally in the registry database, but won't be "
                       "discoverable by tag if that local state is lost before this migration "
                       "completes.")

        dest_label = migration_state.get("dest_volume_label")
        if not dest_label:


            try:
                dest_label = client.load(Volume, migration_state["dest_volume_id"]).label
            except (ApiError, requests.exceptions.RequestException):
                dest_label = None
        return MigrateStartResult(
            outcome="started",
            instance_id=instance.id,
            backup_instance_id=backup_instance_id,
            dest_volume_id=migration_state["dest_volume_id"],
            dest_volume_size_gb=migration_state["dest_volume_size_gb"],
            local_disk_size_mb=migration_state["local_disk_size_mb"],
            dest_volume_label=dest_label,
        )


def cmd_migrate_start(client, args) -> int:
    def _confirm_different_instance_id() -> bool:
        answer = input(f"Type '{args.name}' to confirm this is intentional: ")
        return answer == args.name

    def _confirm_backup(_estimate) -> bool:
        return input("Create this billable backup? [y/N] ").strip().lower() in ("y", "yes")

    backup = getattr(args, "backup", False) is True
    try:
        result = migrate_start_instance(
            client, args.name, args.instance_id, args.ssh_key,
            force=args.force,
            confirm_identity_change=None if args.yes else _confirm_different_instance_id,
            backup=backup,
            confirm_backup=None if args.yes else _confirm_backup,
            on_progress=print, on_warning=_print_to_stderr,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "identity_change_declined":
        print("Aborted -- name didn't match.")
        return 1
    if result.outcome == "backup_declined":
        print("Aborted -- no backup was created and nothing was migrated.")
        return 1

    print()
    print("=" * 70)
    if result.backup_instance_id:
        print(f"Pre-migration backup kept: powered-off instance {result.backup_instance_id}. "
              f"If anything goes wrong: instance_manager.py rollback --name {args.name}")
    print(f"Destination volume created: {result.dest_volume_id} "
          f"({result.dest_volume_size_gb}GB)")
    print(f"Instance {result.instance_id} is now booted into Rescue Mode.")
    print()
    print("YOUR TURN -- this is the one manual step in the whole process:")
    print(f"  1. In Cloud Manager, open instance {result.instance_id} and click "
          "\"Launch LISH Console\".")
    print("  2. At the rescue shell (root@finnix), paste and run this one command. It finds the")
    print("     disks itself -- the new volume by its own ID, the original disk by its size")
    print(f"     (~{result.local_disk_size_mb}MB) -- and refuses, copying nothing, if either is")
    print("     ambiguous. (Rescue Mode's device letters vary between systems, so don't type a")
    print("     dd command with fixed device names.)")
    print()
    if result.dest_volume_label:
        print("       " + engine.rescue_copy_command(result.dest_volume_label, result.local_disk_size_mb or 0))
    else:
        print("       (the volume's label couldn't be looked up just now -- run `lsblk`, then")
        print(f"        dd if=<the ~{result.local_disk_size_mb}MB disk> of=<the {result.dest_volume_size_gb}GB volume> "
              "bs=4M conv=fsync status=progress && sync && echo COPY_DONE)")
    print()
    print("  3. Wait for COPY_DONE (this takes a few minutes). COPY_FAILED means the copy did not")
    print("     complete -- don't continue; run migrate-start --force to start over.")
    print("  4. If it printed 'Could not identify the disks', stop and check `lsblk`: the")
    print(f"     original disk is ~{result.local_disk_size_mb}MB, the new volume "
          f"~{result.dest_volume_size_gb}GB.")
    print("  5. Once it prints COPY_DONE, run:")
    print(f"       instance_manager.py migrate-resume --name {args.name}")
    print("=" * 70)
    return 0


@dataclass
class MigrateResumeResult:

    outcome: Literal["resumed", "incomplete"]
    instance_id: int | None = None
    os_volume_id: int | None = None
    reserved_ip: str | None = None
    previous_attempts_count: int = 0
    detail: str | None = None


    fstab_entries_disabled: list[str] = field(default_factory=list)


ABANDONABLE_MIGRATION_PHASES = (None, "volume_created", "requesting_rescue", "awaiting_manual_dd")


@dataclass
class MigrateAbandonResult:
    instance_id: int | None = None
    instance_state: str = "unknown"
    volume_id: int | None = None
    volume_state: str = "none"
    previous_attempts_archived: int = 0
    backup_kept: bool = False
    notes: list[str] = field(default_factory=list)


def abandon_migration(
    client, name: str, *, boot_original: bool = True, actor: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> MigrateAbandonResult:

    with _instance_lock(name):
        state = load_migrations().get(name)
        if state is None:
            raise engine.ConfigError(f"no unfinished migration for '{name}'.")
        phase = state.get("phase")
        if phase not in ABANDONABLE_MIGRATION_PHASES:
            raise engine.ConfigError(
                f"'{name}''s migration is already being finished (phase {phase!r}); run "
                f"`migrate-resume --name {name}` to complete it, or `rollback --name {name}` if a "
                "pre-migration backup was kept.")
        result = MigrateAbandonResult(instance_id=state.get("instance_id"),
                                      volume_id=state.get("dest_volume_id"))

        def _say(msg: str) -> None:
            if on_progress is not None:
                on_progress(msg)

        instance = None
        if result.instance_id is not None:
            try:
                instance = engine.retry_transient(lambda: client.load(Instance, result.instance_id))
            except ApiError as e:
                if e.status != 404:
                    raise
                result.instance_state = "gone"
        if instance is not None and instance.status != "offline":
            _say(f"Shutting down instance {instance.id} (leaving Rescue Mode)...")
            engine.retry_transient_or_already_done(
                instance.shutdown,
                lambda: client.load(Instance, instance.id).status in ("offline", "shutting_down"))
            engine.poll_until_status(lambda: client.load(Instance, instance.id), ("offline",),
                                     timeout_s=300)

        if result.volume_id is not None:
            try:
                volume = engine.retry_transient(lambda: client.load(Volume, result.volume_id))
            except ApiError as e:
                if e.status != 404:
                    raise
                result.volume_state = "already_gone"
                volume = None
            if volume is not None:
                owners = set(engine.names_from_tags(volume.tags))
                if owners - {name}:
                    result.volume_state = "kept"
                    result.notes.append(
                        f"volume {volume.id} is tagged for {', '.join(sorted(owners - {name}))}, "
                        "not this migration -- left alone.")
                else:
                    if getattr(volume, "linode_id", None):
                        _say(f"Detaching volume {volume.id}...")
                        engine.retry_transient_or_already_done(
                            volume.detach,
                            lambda: client.load(Volume, volume.id).linode_id is None)
                        deadline = time.monotonic() + 300
                        while engine.retry_transient(
                                lambda: client.load(Volume, volume.id)).linode_id is not None:
                            if time.monotonic() > deadline:
                                raise engine.ConfigError(
                                    f"volume {volume.id} didn't detach within 5 minutes.")
                            time.sleep(5)
                    _say(f"Deleting volume {volume.id} ('{volume.label}')...")
                    engine.retry_transient_or_already_done(
                        volume.delete, lambda: engine._resource_confirmed_gone(client, volume))
                    result.volume_state = "deleted"

        if instance is not None:
            if not boot_original:
                result.instance_state = "left_off"
            else:
                _say(f"Booting instance {instance.id} from its original boot config...")
                try:
                    configs = engine.retry_transient(lambda: list(instance.configs))
                    if len(configs) == 1:
                        engine.retry_transient_or_already_done(
                            lambda: instance.boot(config=configs[0]),
                            lambda: client.load(Instance, instance.id).status
                            in ("running", "booting"))
                    else:
                        engine.retry_transient_or_already_done(
                            instance.boot,
                            lambda: client.load(Instance, instance.id).status
                            in ("running", "booting"))
                    engine.poll_until_status(lambda: client.load(Instance, instance.id),
                                             ("running",), timeout_s=300)
                    result.instance_state = "booted"
                except Exception as e:
                    result.instance_state = "boot_failed"
                    result.notes.append(f"could not boot instance {instance.id}: {e} -- boot it "
                                        "from Cloud Manager.")

        previous = state.get("previous_attempts") or []
        if previous:
            _archive_previous_attempts(client, name, previous, on_warning=on_warning)
            result.previous_attempts_archived = len(previous)
        _delete_one_migration(name)
        result.backup_kept = (get_migration_backup(name) or {}).get("status") == "kept"
        record_activity(
            f"migration cancelled: instance {result.instance_id} {result.instance_state}, "
            f"destination volume {result.volume_id} {result.volume_state}",
            level="warning", source="system", instance_name=name, action="migrate-abandon",
            actor=actor)
        if on_warning is not None:
            for note in result.notes:
                on_warning(f"WARNING: {note}")
        return result


@_logs_activity("migrate-resume")
def migrate_resume_instance(
    client, name: str, ssh_key: str, *,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> MigrateResumeResult:

    with _instance_lock(name):
        migrations = load_migrations()
        migration_state = migrations.get(name)
        if migration_state is None:
            raise engine.ConfigError(
                f"no migration in progress for '{name}'. Run migrate-start first."
            )


        if migration_state.get("phase") != "awaiting_manual_dd":
            raise engine.ConfigError(
                f"'{name}' is not ready for migrate-resume -- its checkpoint is at phase "
                f"{migration_state.get('phase')!r}, not the expected 'awaiting_manual_dd'. "
                "This means the manual `dd` + `sync` step (or the rescue-mode request itself) "
                "was never confirmed to complete -- resuming now could build a boot config from "
                "an empty or partially-copied destination volume. If dd + sync is genuinely "
                "done and this is just stale bookkeeping, or you want to abandon this attempt "
                f"and start over, re-run `migrate-start --name {name} --instance-id <id> "
                "--force` instead."
            )

        if on_progress is not None:
            on_progress(f"Resuming migration for '{name}'...")

        def _persist_resume_checkpoint(partial_state: dict) -> None:


            _save_one_migration(name, partial_state)

        try:
            result = engine.resume_path_b_migration(
                client, migration_state, ssh_key_path=ssh_key,
                persist_fn=_persist_resume_checkpoint, on_progress=on_progress,
            )
        except (
            ApiError, RuntimeError, TimeoutError, engine.ConfigError,
            requests.exceptions.RequestException,
        ) as e:
            raise engine.ConfigError(
                f"resuming the migration failed: {e}. The migration checkpoint for '{name}' "
                "was left in place -- fix the underlying issue and re-run migrate-resume."
            ) from e


        previous_attempts = migration_state.get("previous_attempts") or []
        try:
            _archive_previous_attempts(client, name, previous_attempts, on_warning=on_warning)


            _delete_one_migration(name)
        except Exception as e:


            return MigrateResumeResult(
                outcome="incomplete",
                instance_id=result["instance_id"],
                os_volume_id=result["os_volume_id"],
                reserved_ip=result["reserved_ip"],
                detail=str(e),
                fstab_entries_disabled=result.get("fstab_entries_disabled", []),
            )

        return MigrateResumeResult(
            outcome="resumed",
            instance_id=result["instance_id"],
            os_volume_id=result["os_volume_id"],
            reserved_ip=result["reserved_ip"],
            previous_attempts_count=len(previous_attempts),
            fstab_entries_disabled=result.get("fstab_entries_disabled", []),
        )


def cmd_migrate_resume(client, args) -> int:
    try:
        result = migrate_resume_instance(
            client, args.name, args.ssh_key, on_progress=print, on_warning=_print_to_stderr,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    if result.outcome == "incomplete":
        print(
            f"Configuration error: the migration for '{args.name}' completed successfully "
            f"(instance {result.instance_id}, now booting from volume "
            f"{result.os_volume_id}, reserved ip {result.reserved_ip}), but recording "
            f"that locally failed: {result.detail}. Do NOT re-run migrate-start -- the migration "
            "itself is done. Fix the underlying issue (disk space, or a corrupted registry "
            "database under state/), then finish onboarding directly: "
            f"instance_manager.py onboard --name {args.name} --instance-id {result.instance_id}",
            file=sys.stderr,
        )
        return 1

    if result.previous_attempts_count:
        print()
        print(f"NOTE: {result.previous_attempts_count} earlier incomplete migration attempt(s) "
              f"for '{args.name}' were replaced by --force before this one. Their resource IDs "
              f"(most importantly any dest_volume_id) are preserved locally under '{args.name}' "
              f"-- those volumes were NOT deleted automatically and are likely still billing. "
              f"Run `migrate-orphans --name {args.name}` to see them, and `--cleanup` to delete "
              "them once you're sure they're no longer needed.")
    print()
    print("=" * 70)
    print(f"Migration complete for '{args.name}':")
    print(f"  instance: {result.instance_id}")
    print(f"  now booting from volume: {result.os_volume_id}")
    print(f"  reserved ip: {result.reserved_ip or 'none (no public interface; reached over its VPC/VLAN address)'}")
    if result.fstab_entries_disabled:
        print()
        print(f"  NOTE: {len(result.fstab_entries_disabled)} stale /etc/fstab entr"
              f"{'y' if len(result.fstab_entries_disabled) == 1 else 'ies'} disabled (device no "
              "longer present after migration -- would otherwise stall every future boot for "
              "systemd's ~90s device-wait timeout). Backed up before editing:")
        for line in result.fstab_entries_disabled:
            print(f"    {line}")
    print()
    print(f"Ready to onboard: instance_manager.py onboard --name {args.name} "
          f"--instance-id {result.instance_id}")
    print("=" * 70)
    return 0


def _deployment_public_key(ssh_key: str) -> str:
    path = Path(ssh_key + ".pub")
    try:
        text = path.read_text().strip()
    except OSError as e:
        raise engine.ConfigError(f"could not read this deployment's public key {path}: {e}") from e
    if not text:
        raise engine.ConfigError(f"this deployment's public key {path} is empty")
    return text


def _kept_backup_for(name: str, instance_id: int | None) -> dict | None:
    entry = get_migration_backup(name)
    if entry is None or entry["status"] != "kept":
        return None
    original_id = (entry["record"].get("original") or {}).get("instance_id")
    if instance_id is not None and original_id is not None and int(original_id) != int(instance_id):
        return None
    return entry


def assisted_copy_readiness(client, name: str, ssh_key: str) -> dict:

    reasons: list[str] = []
    migration = load_migrations().get(name)
    instance_id = migration.get("instance_id") if migration else None
    if migration is None:
        reasons.append(f"no migration in progress for '{name}'")
    elif migration.get("phase") != "awaiting_manual_dd":
        reasons.append(f"the migration is at phase {migration.get('phase')!r}, not waiting for the copy")
    backup_kept = _kept_backup_for(name, instance_id) is not None
    if not backup_kept:
        reasons.append("no pre-migration backup is kept for this migration (start it with a backup "
                       "to use the assisted copy)")
    public_key = None
    try:
        public_key = _deployment_public_key(ssh_key)
    except engine.ConfigError as e:
        reasons.append(str(e))
    status = {"username": None, "keys_allowed": False, "key_registered": False}
    gateway = None
    if public_key is not None:
        try:
            status = engine.lish_key_status(client, public_key)
        except (ApiError, requests.exceptions.RequestException) as e:
            reasons.append(f"could not read the Linode profile's Lish settings: {e}")
        else:
            if not status["keys_allowed"]:
                reasons.append("the Linode profile allows only password login to Lish; allow keys "
                               "(Profile > LISH Console Settings)")
            if not status["key_registered"]:
                reasons.append("this deployment's public key is not one of the profile's Lish keys "
                               "(Profile > LISH Console Settings)")
    if instance_id is not None:
        try:
            instance = engine.retry_transient(lambda: client.load(Instance, instance_id))
            gateway = engine.lish_gateway(instance.region.id)
        except (ApiError, requests.exceptions.RequestException) as e:
            reasons.append(f"could not load instance {instance_id}: {e}")
    return {
        "available": not reasons,
        "reasons": reasons,
        "username": status.get("username"),
        "gateway": gateway,
        "public_key": public_key,
        "key_registered": bool(status.get("key_registered")),
        "keys_allowed": bool(status.get("keys_allowed")),
        "backup_kept": backup_kept,
        "instance_id": instance_id,
    }


@dataclass
class AssistedCopyResult:

    outcome: Literal["resumed", "copied", "resume_failed"]
    resume: MigrateResumeResult | None = None
    detail: str | None = None


@_logs_activity("migrate-copy")
def run_assisted_migration_copy(
    client, name: str, ssh_key: str, *,
    resume: bool = True,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> AssistedCopyResult:

    readiness = assisted_copy_readiness(client, name, ssh_key)
    if not readiness["available"]:
        raise engine.ConfigError("the assisted copy can't run: " + "; ".join(readiness["reasons"]))
    with _instance_lock(name):
        migration = load_migrations().get(name)
        if migration is None or migration.get("phase") != "awaiting_manual_dd":
            raise engine.ConfigError(f"'{name}' is no longer waiting for its copy")
        try:
            instance = engine.retry_transient(lambda: client.load(Instance, migration["instance_id"]))
            volume = engine.retry_transient(lambda: client.load(Volume, migration["dest_volume_id"]))
            region, label, volume_label = instance.region.id, instance.label, volume.label
        except (ApiError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(f"could not load the instance or its new volume: {e}") from e
        command = engine.rescue_copy_command(volume_label, migration.get("local_disk_size_mb") or 0)
        log_dir = REGISTRY_PATH.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        if on_progress is not None:
            on_progress(f"Opening the Rescue Mode console of instance {instance.id} over Lish "
                        f"({readiness['username']}@{readiness['gateway']})...")
        try:
            engine.run_rescue_copy_over_lish(
                readiness["username"], region, label, ssh_key, command,
                on_progress=on_progress, log_path=log_dir / f"lish-{name}.log")
        except RuntimeError as e:
            raise engine.ConfigError(
                f"the assisted copy did not complete: {e}. The migration is still waiting for "
                "its copy: run it again, or run the copy by hand in the Lish console. Rollback "
                f"is available: instance_manager.py rollback --name {name}") from e
    if not resume:
        return AssistedCopyResult(outcome="copied")
    try:
        resumed = migrate_resume_instance(client, name, ssh_key, on_progress=on_progress,
                                          on_warning=on_warning)
    except (engine.ConfigError, InstanceLockedError) as e:
        return AssistedCopyResult(outcome="resume_failed", detail=str(e))
    return AssistedCopyResult(outcome="resumed", resume=resumed)


def cmd_migrate_copy(client, args) -> int:
    if getattr(args, "check", False):
        r = assisted_copy_readiness(client, args.name, args.ssh_key)
        print(json.dumps(r, indent=2))
        return 0 if r["available"] else 1
    try:
        result = run_assisted_migration_copy(
            client, args.name, args.ssh_key, resume=not args.no_resume,
            on_progress=print, on_warning=_print_to_stderr)
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 3
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "copied":
        print(f"Copy done. Next: instance_manager.py migrate-resume --name {args.name}")
        return 0
    if result.outcome == "resume_failed":
        print(f"Copy done, but migrate-resume failed: {result.detail}", file=sys.stderr)
        print(f"Run it again: instance_manager.py migrate-resume --name {args.name}", file=sys.stderr)
        return 1
    r = result.resume
    assert r is not None
    if r.outcome == "incomplete":
        print(f"Migration finished, but local bookkeeping failed: {r.detail}", file=sys.stderr)
        print(f"Onboard directly: instance_manager.py onboard --name {args.name} "
              f"--instance-id {r.instance_id}", file=sys.stderr)
        return 1
    print(f"Migration complete: instance {r.instance_id} now boots from volume {r.os_volume_id}.")
    print(f"Next: instance_manager.py onboard --name {args.name} --instance-id {r.instance_id}")
    return 0


def _migration_conflict_reason_text(c: dict) -> str:

    if c["reason"] == "multiple_names":
        return f"tagged for more than one name at once: {c['names']!r}"
    if c["reason"] == "multiple_roles":
        return f"carries both the active and orphaned migration role tags at once (names: {c['names']!r})"
    if c["reason"] == "managed_role_conflict":
        return (f"carries a migration role tag alongside a managed resource role {c['roles']!r} "
                f"(names: {c['names']!r}) -- this may be a live OS/data volume, not an abandoned attempt")
    return "carries a migration role tag but no scheduler name tag at all"


@dataclass
class MarkOrphanedResult:

    outcome: Literal["marked", "aborted_by_user"]
    volume_id: int | None = None


def mark_migration_volume_orphaned(
    client, name: str | None, volume_id: int | None, *,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> MarkOrphanedResult:

    if name is None or volume_id is None:
        raise engine.ConfigError("--mark-orphaned requires both --name and --volume-id.")

    with _instance_lock(name):


        migrations = load_migrations()
        for local_name, local_state in migrations.items():
            if local_state.get("dest_volume_id") == volume_id:
                raise engine.ConfigError(
                    f"volume {volume_id} is the CURRENT destination of a locally-known, "
                    f"resumable migration ('{local_name}', phase={local_state.get('phase')!r}) "
                    "-- refusing to mark it orphaned. This command is only for a volume whose "
                    f"local migration state is already gone. Resume the migration "
                    f"(`migrate-resume --name {local_name}`) or explicitly replace it "
                    f"(`migrate-start --name {local_name} --force`) instead."
                )

        try:
            tagged, conflicts = engine.find_migration_volumes_by_tag(client)
        except (ApiError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(f"could not scan remote tags: {e}") from e


        own_conflict = next((c for c in conflicts if c["id"] == volume_id), None)
        if own_conflict is not None:
            raise engine.ConfigError(
                f"volume {volume_id} has contradictory tags ({own_conflict['reason']}, "
                f"names={own_conflict['names']!r}) -- refusing to mark it orphaned until the "
                "tags are corrected by hand (Cloud Manager). Marking it now would guess at an "
                "ambiguous state instead of resolving it."
            )

        entry = tagged.get(name, {"active_migration_volume_ids": []})
        if volume_id not in entry["active_migration_volume_ids"]:
            raise engine.ConfigError(
                f"volume {volume_id} is not tagged as an active migration attempt for '{name}' "
                f"-- nothing to mark orphaned. Active volumes for '{name}': "
                f"{entry['active_migration_volume_ids']}."
            )

        if on_progress is not None:
            on_progress(f"This will mark volume {volume_id} (active migration attempt for "
                        f"'{name}') as orphaned. The volume itself is NOT deleted -- it becomes "
                        "eligible for `migrate-orphans --cleanup` afterward.")
        if confirm is not None and not confirm():
            return MarkOrphanedResult(outcome="aborted_by_user")

        try:
            engine.retag_migration_volume_as_orphaned(client, volume_id, name)
        except (ApiError, engine.ConfigError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(f"failed to retag volume {volume_id}: {e}") from e

        return MarkOrphanedResult(outcome="marked", volume_id=volume_id)


def _cmd_migrate_orphans_mark_orphaned(client, args) -> int:
    def _confirm() -> bool:
        answer = input(f"Type '{args.name}' to confirm: ")
        return answer == args.name

    try:
        result = mark_migration_volume_orphaned(
            client, args.name, args.volume_id,
            confirm=None if args.yes else _confirm,
            on_progress=print,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "aborted_by_user":
        print("Aborted -- name didn't match.")
        return 1
    print(f"Volume {result.volume_id} marked orphaned for '{args.name}'.")
    return 0


def _remove_resolved_orphan_attempts(name: str, resolved_volume_ids: set[int]) -> None:

    if not resolved_volume_ids:
        return

    def _do():
        conn = _connect()
        try:
            placeholders = ", ".join("?" * len(resolved_volume_ids))
            conn.execute(
                f"DELETE FROM orphaned_migration_attempts"
                f" WHERE name = ? AND dest_volume_id IN ({placeholders})",
                (name, *resolved_volume_ids),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


@dataclass
class MigrateOrphansResult:

    outcome: Literal["listed", "nothing_found", "aborted_by_user", "cleanup_complete", "cleanup_partial"]
    ok: bool


def list_or_cleanup_migration_orphans(
    client, name: str | None, *,
    volume_id: int | None = None, cleanup: bool = False,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> MigrateOrphansResult:

    if cleanup and name is None:
        raise engine.ConfigError("--cleanup requires --name.")

    if cleanup:
        assert name is not None
        with _instance_lock(name):
            return _list_or_cleanup_migration_orphans_impl(
                client, name, volume_id=volume_id, cleanup=cleanup,
                confirm=confirm, on_progress=on_progress, on_warning=on_warning,
            )
    return _list_or_cleanup_migration_orphans_impl(
        client, name, volume_id=volume_id, cleanup=cleanup,
        confirm=confirm, on_progress=on_progress, on_warning=on_warning,
    )


def _list_or_cleanup_migration_orphans_impl(
    client, name: str | None, *, volume_id: int | None, cleanup: bool,
    confirm: Callable[[], bool] | None,
    on_progress: Callable[[str], None] | None,
    on_warning: Callable[[str], None] | None,
) -> MigrateOrphansResult:
    archive = load_orphan_archive()
    if name is not None:
        archive = {name: archive[name]} if name in archive else {}


    scan_failed = False
    try:
        tagged, conflicts = engine.find_migration_volumes_by_tag(client)
    except (ApiError, requests.exceptions.RequestException) as e:
        scan_failed = True
        if cleanup:
            raise engine.ConfigError(
                f"could not scan remote tags for migration volumes ({e}) -- refusing to "
                "proceed with --cleanup. The remote conflict check cannot be skipped at a "
                "permanent-delete boundary: an unknown scan result must never be treated as "
                "proof no conflicts exist."
            ) from e
        if on_warning is not None:
            on_warning(f"  WARNING: could not scan remote tags for migration volumes ({e}) -- "
                       "showing local archive only. This listing is INCOMPLETE -- it cannot "
                       "confirm no remote conflicts exist.")
        tagged, conflicts = {}, []
    if name is not None:
        tagged = {name: tagged[name]} if name in tagged else {}


    local_orphan_ids = {
        n: {a["dest_volume_id"] for a in attempts if a.get("dest_volume_id") is not None}
        for n, attempts in archive.items()
    }


    remote_only: dict[str, list[int]] = {}
    for n, entry in tagged.items():
        extra = sorted(set(entry["orphaned_migration_volume_ids"]) - local_orphan_ids.get(n, set()))
        if extra:
            remote_only[n] = extra

    if not cleanup:
        if not scan_failed and not archive and not remote_only and not conflicts and not any(
            entry["active_migration_volume_ids"] for entry in tagged.values()
        ):
            scope = f" for '{name}'" if name else ""
            if on_progress is not None:
                on_progress(f"No orphaned migration attempts recorded{scope}.")
            return MigrateOrphansResult(outcome="nothing_found", ok=True)
        if scan_failed and not archive and on_progress is not None:
            scope = f" for '{name}'" if name else ""
            on_progress(f"No orphaned migration attempts recorded in the local archive{scope} -- "
                        "but the remote scan failed, so this is NOT a confirmed complete picture.")
        if on_progress is not None:
            for n, attempts in archive.items():
                on_progress(f"'{n}':")
                for attempt in attempts:
                    on_progress(f"  dest_volume_id={attempt.get('dest_volume_id')}  "
                                f"phase={attempt.get('phase')!r}")
            for n, vol_ids in remote_only.items():
                on_progress(f"'{n}' (found via remote tags only -- not in local archive):")
                for vol_id in vol_ids:
                    on_progress(f"  dest_volume_id={vol_id}  phase=unknown (no local record)")
            for n, entry in tagged.items():
                if entry["active_migration_volume_ids"]:
                    on_progress(f"'{n}': also has active (not orphaned) migration volume(s) "
                                f"{entry['active_migration_volume_ids']} -- not eligible for "
                                "cleanup here.")
            if conflicts:
                on_progress(f"WARNING: {len(conflicts)} migration volume(s) have contradictory "
                            "tags and were excluded above entirely -- they are NOT eligible for "
                            "automated cleanup or retagging until resolved by hand:")
                for c in conflicts:
                    on_progress(f"  volume {c['id']}: {_migration_conflict_reason_text(c)}")
                on_progress("  Review tags directly in Cloud Manager and correct them by hand "
                            "-- do not delete the volume based on a guess.")
            on_progress("")
            on_progress("Run with --cleanup --name <name> to delete these volumes (optionally "
                        "--volume-id to target just one).")


        if name is not None:
            relevant_conflicts = [c for c in conflicts if name in c.get("names", [])]
        else:
            relevant_conflicts = conflicts
        return MigrateOrphansResult(outcome="listed", ok=not (relevant_conflicts or scan_failed))

    assert name is not None
    attempts = archive.get(name, [])
    if volume_id is not None:
        attempts = [a for a in attempts if a.get("dest_volume_id") == volume_id]
    volume_id_set = {a["dest_volume_id"] for a in attempts if a.get("dest_volume_id") is not None}
    remote_extra = set(remote_only.get(name, []))
    if volume_id is not None:
        remote_extra = {v for v in remote_extra if v == volume_id}
    volume_ids = sorted(volume_id_set | remote_extra)


    conflicted_ids = {c["id"] for c in conflicts}
    conflicted_overlap = sorted(set(volume_ids) & conflicted_ids)
    if conflicted_overlap:
        lines = [
            f"refusing to delete -- {len(conflicted_overlap)} of the requested volume(s) for "
            f"'{name}' have contradictory remote tags and are NOT eligible for automated "
            f"cleanup: {conflicted_overlap}. Full candidate set was {volume_ids} -- nothing was "
            "deleted, and the local archive is untouched."
        ]
        for c in conflicts:
            if c["id"] in conflicted_overlap:
                lines.append(f"  volume {c['id']}: {c['reason']} (names: {c['names']!r})")
        lines.append("  Review tags directly in Cloud Manager and correct them by hand, then "
                     "re-run cleanup.")
        raise engine.ConfigError("\n".join(lines))

    if not volume_ids:
        if volume_id is not None and volume_id in conflicted_ids:


            conflict = next(c for c in conflicts if c["id"] == volume_id)
            raise engine.ConfigError(
                f"volume {volume_id} is tag-conflicted, not simply orphaned, for '{name}': "
                f"{_migration_conflict_reason_text(conflict)}. Not eligible for automated "
                "cleanup -- review tags directly in Cloud Manager and correct them by hand."
            )


        if volume_id is None:
            name_conflicts = [c for c in conflicts if name in c.get("names", [])]
            if name_conflicts:
                lines = [
                    f"'{name}' has no plain orphaned volumes to clean up, but "
                    f"{len(name_conflicts)} volume(s) with contradictory remote tags naming it "
                    "were found -- not eligible for automated cleanup:"
                ]
                for c in name_conflicts:
                    lines.append(f"  volume {c['id']}: {_migration_conflict_reason_text(c)}")
                lines.append("  Review tags directly in Cloud Manager and correct them by hand.")
                raise engine.ConfigError("\n".join(lines))
        if on_progress is not None:
            on_progress(f"No matching orphaned attempts for '{name}'.")
        return MigrateOrphansResult(outcome="nothing_found", ok=True)

    if on_progress is not None:
        on_progress(f"This will PERMANENTLY DELETE {len(volume_ids)} orphaned volume(s) for "
                    f"'{name}': {volume_ids}")
    if confirm is not None and not confirm():
        return MigrateOrphansResult(outcome="aborted_by_user", ok=False)


    verification_problems: list[str] = []
    verified_volumes: dict[int, Volume] = {}
    already_gone: set[int] = set()
    for vol_id in volume_ids:
        try:
            problems, vol = engine.verify_migration_volume_orphaned_and_unmanaged(
                client, vol_id, name
            )
        except ApiError as e:
            if e.status == 404:
                already_gone.add(vol_id)
                continue
            verification_problems.append(f"volume {vol_id}: could not verify current tags ({e})")
            continue
        except requests.exceptions.RequestException as e:


            verification_problems.append(f"volume {vol_id}: could not verify current tags ({e})")
            continue
        if problems:
            verification_problems.append(f"volume {vol_id}: {'; '.join(problems)}")
        else:
            verified_volumes[vol_id] = vol
    if verification_problems:
        lines = [
            "refusing to delete -- pre-delete verification found "
            f"{len(verification_problems)} volume(s) whose current tags no longer confirm "
            "they're a genuinely safe, unmanaged orphan (state may have changed since the "
            "scan above, or the volume is actually still managed):",
        ]
        lines.extend(f"  {msg}" for msg in verification_problems)
        lines.append("  Nothing was deleted, and the local archive is untouched. Re-run "
                     "`migrate-orphans` to see current state.")
        raise engine.ConfigError("\n".join(lines))

    resolved: set[int] = set(already_gone)
    if on_progress is not None:
        for vol_id in already_gone:
            on_progress(f"  volume {vol_id}: already gone")
    for vol_id in verified_volumes:


        try:
            problems, fresh_vol = engine.verify_migration_volume_orphaned_and_unmanaged(
                client, vol_id, name
            )
        except ApiError as e:
            if e.status == 404:
                if on_progress is not None:
                    on_progress(f"  volume {vol_id}: already gone")
                resolved.add(vol_id)
            elif on_warning is not None:
                on_warning(f"  FAILED to re-verify volume {vol_id} immediately before delete: "
                           f"{e} -- not deleted, left in the archive.")
            continue
        except requests.exceptions.RequestException as e:


            if on_warning is not None:
                on_warning(f"  FAILED to re-verify volume {vol_id} immediately before delete: "
                           f"{e} -- not deleted, left in the archive.")
            continue
        if problems:
            if on_warning is not None:
                on_warning(f"  volume {vol_id}: tags changed since the batch preflight above "
                           f"({'; '.join(problems)}) -- NOT deleted, left in the archive.")
            continue
        try:


            engine.retry_transient(fresh_vol.delete)
            if on_progress is not None:
                on_progress(f"  deleted volume {vol_id}")
            resolved.add(vol_id)
        except ApiError as e:
            if e.status == 404:
                if on_progress is not None:
                    on_progress(f"  volume {vol_id}: already gone")
                resolved.add(vol_id)
            elif on_warning is not None:
                on_warning(f"  FAILED to delete volume {vol_id}: {e}")
        except Exception as e:
            if on_warning is not None:
                on_warning(f"  FAILED to delete volume {vol_id}: {e}")

    if resolved:
        _remove_resolved_orphan_attempts(name, resolved)
    remaining = len(volume_ids) - len(resolved)
    if remaining:
        if on_warning is not None:
            on_warning(f"{remaining} volume(s) could not be confirmed deleted -- left in the "
                       "archive, safe to re-run --cleanup.")
        return MigrateOrphansResult(outcome="cleanup_partial", ok=False)
    if on_progress is not None:
        on_progress(f"All matching orphaned attempts for '{name}' resolved.")
    return MigrateOrphansResult(outcome="cleanup_complete", ok=True)


def cmd_migrate_orphans(client, args) -> int:

    if args.mark_orphaned:
        return _cmd_migrate_orphans_mark_orphaned(client, args)
    return _cmd_migrate_orphans_list_or_cleanup(client, args)


def _cmd_migrate_orphans_list_or_cleanup(client, args) -> int:
    def _confirm() -> bool:
        answer = input(f"Type '{args.name}' to confirm: ")
        return answer == args.name

    try:
        result = list_or_cleanup_migration_orphans(
            client, args.name, volume_id=args.volume_id, cleanup=args.cleanup,
            confirm=None if args.yes else _confirm,
            on_progress=print, on_warning=_print_to_stderr,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "aborted_by_user":
        print("Aborted -- name didn't match.")
        return 1
    return 0 if result.ok else 1


def find_vpc_id_for_subnet(client, subnet_id: int) -> int | None:

    for vpc in client.vpcs():
        for subnet in vpc.subnets:
            if subnet.id == subnet_id:
                return vpc.id
    return None


def _derive_vpc_prefix(
    client, network_config: list, network_interface_model: str, *,
    vpc_id: int | None, cannot_determine: Callable[[str], Exception],
) -> int | None:

    for iface in network_config:
        if network_interface_model == engine.INTERFACE_MODEL_LEGACY:
            if iface.get("purpose") != "vpc":
                continue
            subnet_id = iface.get("subnet_id")
            resolved_vpc_id = vpc_id
            if resolved_vpc_id is None:
                resolved_vpc_id = find_vpc_id_for_subnet(client, subnet_id)
            if resolved_vpc_id is None:
                raise cannot_determine(
                    "has a VPC interface but its vpc_id couldn't be determined automatically "
                    "(legacy_config model doesn't capture it) and no VPC was found containing "
                    "that subnet."
                )
        else:
            if not iface.get("vpc"):
                continue
            subnet_id = iface["vpc"].get("subnet_id")
            resolved_vpc_id = iface["vpc"].get("vpc_id") or vpc_id
            if resolved_vpc_id is None:
                raise cannot_determine(
                    "has a VPC interface but its vpc_id couldn't be determined (missing from "
                    "the captured interface, unexpectedly)."
                )
        return engine.get_vpc_subnet_prefix(client, resolved_vpc_id, subnet_id)
    return None


@dataclass
class OnboardResult:

    outcome: Literal["onboarded", "identity_change_declined"]
    record: dict | None = None


@_logs_activity("onboard")
def onboard_instance(
    client, name: str, instance_id: int, ssh_key: str | None, *,
    vpc_id: int | None = None, force: bool = False,
    ssh_password: str | None = None,
    install_public_key: str | None = None,
    confirm_identity_change: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> OnboardResult:

    with _instance_lock(name):


        registry = load_registry()
        if name in registry and not force:
            raise engine.AlreadyOnboardedError(name)
        pending = pending_deletion_for(name, "node")
        if pending is not None:


            raise engine.ConfigError(
                f"'{name}' was offboarded and is pending deletion (#{pending['id']}, due "
                f"{pending['due_at'][:10]}). Recover it, delete it now (Pending deletion page, or "
                f"`pending-deletions --delete-now {pending['id']}`), or use a different name.")

        try:


            instance = engine.retry_transient(lambda: client.load(Instance, instance_id))
        except (ApiError, requests.exceptions.RequestException) as e:


            raise engine.ConfigError(f"instance {instance_id} not found: {e}") from e

        try:
            engine.check_region_capabilities(client, instance.region.id)
        except engine.ConfigError as e:
            raise engine.ConfigError(
                f"instance {instance_id} is in an unsupported region: {e} This tool requires "
                "Block Storage (the OS lives on a volume, never local disk) -- reserved IPs are "
                "permanently locked to the region they were first reserved in, so onboarding "
                "can't move an instance to a region that doesn't support it."
            ) from e
        except (ApiError, requests.exceptions.RequestException) as e:


            raise engine.ConfigError(
                f"could not check region capabilities for instance {instance_id}: {e}"
            ) from e

        if instance.status != "running":
            raise engine.ConfigError(
                f"instance {instance_id} is not running (status={instance.status!r}) -- must "
                "be running to capture its network config and data volumes over SSH."
            )


        try:
            configs = list(instance.configs)
        except (ApiError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(f"could not read instance {instance_id}'s boot configs: {e}") from e
        if not configs:
            raise engine.ConfigError("instance has no boot config.")
        if len(configs) != 1:
            raise engine.ConfigError(
                f"instance {instance_id} has {len(configs)} boot configs -- this tool can't "
                "tell which one is actually active (Linode's API has no such field) and "
                "refuses to guess, since onboarding the wrong one would silently capture the "
                "wrong os_volume_id. Delete or consolidate the extra config(s) manually first "
                "(Cloud Manager), then retry."
            )
        devices = configs[0].devices.dict
        sda = devices.get("sda")


        if not sda or not sda.get("filesystem_path"):


            raise engine.NotMigratedError(instance_id)
        os_volume_id = sda["id"]


        if engine.instance_has_public_interface(instance, configs):
            reserved_ip = instance.ipv4[0]
            try:
                ip_info = engine.get_ip_details(client, reserved_ip)
            except (ApiError, requests.exceptions.RequestException) as e:


                raise engine.ConfigError(
                    f"could not check whether {reserved_ip} is reserved: {e}"
                ) from e
            if not ip_info.get("reserved"):
                raise engine.IPNotReservedError(reserved_ip)
        else:


            if not engine.instance_has_vpc_or_vlan_interface(instance, configs):
                raise engine.ConfigError(
                    "instance has no public IPv4 address and no VPC/VLAN interface -- "
                    "nothing to reach it by."
                )
            reserved_ip = None


        old_record = registry.get(name)
        identity_changed = False
        if old_record is not None:
            identity_changed = (
                old_record.get("current_linode_id") != instance.id
                or old_record.get("os_volume_id") != os_volume_id
                or old_record.get("reserved_ip") != reserved_ip
                or old_record.get("region") != instance.region.id
            )
            if identity_changed:
                if on_progress is not None:


                    on_progress(
                        f"WARNING: re-onboarding '{name}' at a DIFFERENT resource identity "
                        "than what's currently on record:\n"
                        f"  instance id:  {old_record.get('current_linode_id')} -> {instance.id}\n"
                        f"  os_volume_id: {old_record.get('os_volume_id')} -> {os_volume_id}\n"
                        f"  reserved_ip:  {old_record.get('reserved_ip')} -> {reserved_ip}\n"
                        f"  region:       {old_record.get('region')} -> {instance.region.id}\n"
                        "If this is unexpected, double-check --instance-id -- a typo here "
                        f"would silently redirect '{name}' to unrelated infrastructure from "
                        "now on."
                    )
                if confirm_identity_change is not None and not confirm_identity_change():
                    return OnboardResult(outcome="identity_change_declined")


        try:


            captured = engine.capture_network_config(instance, configs=configs)


            if (engine.legacy_nat_only(captured["network_config"], captured["network_interface_model"])
                    and len(engine.nat_1_1_addresses(
                        captured["network_config"], captured["network_interface_model"])) > 1):
                raise _OnboardRefusal(
                    "this instance maps more than one public address with VPC 1:1 NAT under the "
                    "older (legacy config) networking model; only one can be recreated after a stop."
                )


            for subnet_id, vpc_address in engine.vpc_interface_addresses(
                captured["network_config"], captured["network_interface_model"]
            ):
                clashes = _records_using_vpc_address(
                    load_registry(), subnet_id, vpc_address, exclude=name,
                )
                if clashes:
                    raise _OnboardRefusal(
                        f"this instance's VPC address {vpc_address} is already recorded for managed "
                        f"instance(s) {', '.join(clashes)} (Linode gave it out while that one "
                        "was stopped). Give one of them a different address first: change this "
                        "instance's VPC address in Cloud Manager, or move the stopped one with "
                        f"`set-vpc-address --name {clashes[0]} --address <free address>`."
                    )
            ssh_target = reserved_ip or engine.vpc_or_vlan_address(
                captured["network_config"], captured["network_interface_model"]
            )
            if not ssh_target:
                raise _OnboardRefusal(
                    "could not determine an address to reach this instance over SSH -- "
                    "the onboarding gate confirmed a VPC/VLAN interface exists, but no "
                    "explicit static IPv4 address was captured for it."
                )

            vpc_prefix = _derive_vpc_prefix(
                client, captured["network_config"], captured["network_interface_model"],
                vpc_id=vpc_id,
                cannot_determine=lambda msg: _OnboardRefusal(
                    f"this instance {msg} Pass --vpc-id explicitly."
                ),
            )

            if on_progress is not None:
                on_progress("Capturing authorized_keys over SSH and data volumes over the API...")


            _forget_unowned_host_key(ssh_target, on_progress)
            authorized_keys_raw = engine.ssh_run(
                ssh_target, ssh_key, "cat /root/.ssh/authorized_keys 2>/dev/null",
                trust_new=True, password=ssh_password,
            )
            authorized_keys = [
                line.strip() for line in authorized_keys_raw.splitlines() if line.strip()
            ]


            needs_key_install = install_public_key is not None and install_public_key not in authorized_keys
            if needs_key_install:
                assert install_public_key is not None
                authorized_keys.append(install_public_key)
            if not authorized_keys:
                raise _OnboardRefusal(
                    "could not read any authorized_keys from the instance -- refusing to "
                    "onboard without knowing how future recreates will grant SSH access."
                )


            if _is_vlan_only(captured["network_config"], captured["network_interface_model"]):
                if on_progress is not None:
                    on_progress(
                        "VLAN-only instance: telling cloud-init on the node to keep its SSH host "
                        f"keys across recreates ({VLAN_ONLY_CLOUD_CFG})..."
                    )
                engine.ssh_run(
                    ssh_target, ssh_key,
                    f"mkdir -p /etc/cloud/cloud.cfg.d && printf '%s\\n' "
                    f"'# Written by linode-instance-scheduler: keep SSH host keys across recreates.' "
                    f"'ssh_deletekeys: false' > {VLAN_ONLY_CLOUD_CFG}",
                    trust_new=True, password=ssh_password,
                )

            data_volumes = engine.capture_data_volumes(instance, os_volume_id, configs=configs)
            instance_attrs = engine.capture_instance_attributes(instance)
        except _OnboardRefusal as e:


            raise engine.ConfigError(str(e)) from e
        except (ApiError, RuntimeError, engine.ConfigError, requests.exceptions.RequestException) as e:


            raise engine.ConfigError(
                f"failed to capture '{name}' from instance {instance_id}: {e}"
            ) from e

        record = {
            "name": name,
            "region": instance.region.id,
            "os_volume_id": os_volume_id,
            "reserved_ip": reserved_ip,


            "group_id": registry.get(name, {}).get("group_id"),
            "network_interface_model": captured["network_interface_model"],
            "network_config": captured["network_config"],
            "network_helper_enabled": captured["network_helper_enabled"],
            "vpc_prefix": vpc_prefix,
            "data_volumes": data_volumes,
            "authorized_keys": authorized_keys,


            "label": instance.label,
            "tags": list(instance.tags),


            "instance_attrs": instance_attrs,
            "current_linode_id": instance.id,
            "current_status": "running",
            "transitioning": False,
        }
        tag_kwargs = {
            "os_volume_id": os_volume_id,
            "data_volume_ids": [dv["volume_id"] for dv in data_volumes],
            "reserved_ip": reserved_ip,
        }
        if on_progress is not None:
            on_progress("Tagging resources for disaster recovery...")
        try:
            engine.tag_managed_resources(client, name, **tag_kwargs)
        except engine.ResourceOwnershipConflict as e:


            raise engine.ConfigError(
                f"{e}\nOnboarding record was NOT saved. This name's disaster-recovery tags "
                "were never written, so `rebuild` cannot find this node if local state is lost."
            ) from e
        except engine.TagVerificationError as e:


            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not tag resources for disaster recovery ({e}) -- "
                    "onboarding still succeeded, but `rebuild` won't find this node if local "
                    "state is lost. Safe to ignore or retry later."
                )
        except (ApiError, requests.exceptions.RequestException) as e:


            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not tag resources for disaster recovery ({e}) -- "
                    "onboarding still succeeded, but `rebuild` won't find this node if local "
                    "state is lost. Safe to ignore or retry later."
                )

        if needs_key_install:
            assert install_public_key is not None


            if on_progress is not None:
                on_progress(
                    "Installing this deployment's own SSH key so future automated "
                    "operations don't need the one-time credential again..."
                )
            try:
                engine.ssh_run(
                    ssh_target, ssh_key, _append_authorized_key_command(install_public_key),
                    trust_new=True, password=ssh_password,
                )
            except (ApiError, RuntimeError, requests.exceptions.RequestException) as e:
                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not install this deployment's own SSH key ({e}) -- "
                        "onboarding still succeeded, but a future automated start/stop will need "
                        "the one-time credential again unless this is retried (e.g. `onboard "
                        "--force`) or the key is added manually."
                    )

        _save_one_record(name, record)


        if record.get("group_id") is not None:
            try:
                _sync_group_membership_tags_locked(
                    client, name, record, _group_row_by_id(record["group_id"]),
                )
            except Exception as e:

                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not sync group membership tags for '{name}' onto its "
                        f"OS volume ({e}) -- group membership itself is preserved locally and "
                        "fully in effect, but won't be recoverable via `rebuild` if the local "
                        "database is lost before this is retried (safe to retry: re-run "
                        "`group-add` for this instance)."
                    )


        if get_schedule_mode(name) == "manual":
            try:
                _sync_schedule_mode_tag_locked(client, name, record, "manual")
            except Exception as e:
                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not tag '{name}' as manual-only on its OS volume ({e})"
                        f" -- re-run `set-mode --name {name} --manual` to retry."
                    )


        existing_schedule = get_instance_schedule(name)
        if existing_schedule is not None:
            try:
                _sync_schedule_tags_locked(client, name, record, existing_schedule)
            except Exception as e:

                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not sync schedule tags for '{name}' onto its OS "
                        f"volume ({e}) -- the schedule itself is preserved locally and fully in "
                        "effect, but won't be recoverable via `rebuild` if the local database is "
                        "lost before this is retried (safe to retry: re-run `schedule-set` for "
                        "this instance)."
                    )


        try:
            _sync_hook_tags_locked(client, name, record, on_warning)
        except Exception as e:
            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not sync hook tags for '{name}' onto its OS volume ({e}) "
                    "-- hooks stay in effect locally; re-run hooks-set to retry."
                )


        try:
            matched_key_ids, _unmatched_keys = _classify_authorized_keys(
                client, record["authorized_keys"],
            )
            _sync_extra_recovery_tags_locked(client, name, record, matched_key_ids)
        except Exception as e:

            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not sync network-config/SSH-key recovery tags for "
                    f"'{name}' onto its OS volume ({e}) -- onboarding still succeeded and "
                    f"'{name}' is fully correct locally; this only affects recovery after a "
                    "total local database loss (safe to retry: `onboard --force`)."
                )
        osb.sync_object_storage_backup(
            name, _backup_payload(name, record), on_warning=on_warning,
        )


        try:
            fresh_os_volume = client.load(Volume, os_volume_id)
            other_names = [n for n in engine.names_from_tags(fresh_os_volume.tags) if n != name]
            if other_names and on_warning is not None:
                on_warning(
                    f"  WARNING: possible concurrent-onboard race detected -- immediately "
                    f"after tagging, OS volume {os_volume_id}'s tags also show {other_names!r} "
                    "as an owner. This usually means another onboard for a different name "
                    "targeted the same resource at nearly the same time. Verify manually in "
                    "Cloud Manager which name should actually own this volume before trusting "
                    "either name's disaster recovery."
                )
        except Exception as e:


            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not verify OS volume {os_volume_id}'s tags for a "
                    f"possible concurrent-onboard race ({e}) -- onboarding still succeeded. "
                    "Safe to ignore."
                )

        old_reserved_ip = old_record.get("reserved_ip") if old_record is not None else None
        if identity_changed and old_reserved_ip and old_reserved_ip != reserved_ip:


            try:
                engine.reset_known_host(old_reserved_ip)
            except Exception as e:
                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not clear the known-hosts entry for the old "
                        f"address {old_reserved_ip} ({e}) -- if it's ever reused by a "
                        f"different node, its first onboarding attempt may need "
                        f"`reset-host-key --ip {old_reserved_ip}` first."
                    )


        if on_warning is not None:
            try:
                findings = unmanaged_network_neighbours(client, name, record, load_registry())
            except Exception:
                findings = []
            if findings:
                on_warning(_neighbour_warning(name, findings))
            unreserved_nat = [
                a for a in engine.nat_1_1_addresses(record["network_config"], record["network_interface_model"])
                if not _is_reserved_ip(client, a)
            ]
            if unreserved_nat:
                on_warning(
                    f"WARNING: '{name}' reaches the internet through VPC 1:1 NAT public address "
                    f"{', '.join(unreserved_nat)}, which isn't reserved. Linode releases it when "
                    "the instance is stopped, so it gets a new public address on every start. "
                    "Reserve it in Cloud Manager to keep it."
                )

        return OnboardResult(outcome="onboarded", record=record)


def _is_reserved_ip(client, address: str) -> bool:

    try:
        return bool(client.get(f"/networking/reserved/ips/{address}").get("reserved"))
    except Exception:
        return False


def cmd_onboard(client, args) -> int:
    def _confirm_identity_change() -> bool:
        answer = input(f"Type '{args.name}' to confirm this is intentional: ")
        return answer == args.name

    try:
        result = onboard_instance(
            client, args.name, args.instance_id, args.ssh_key,
            vpc_id=args.vpc_id, force=args.force,
            confirm_identity_change=None if args.yes else _confirm_identity_change,
            on_progress=print, on_warning=_print_to_stderr,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "identity_change_declined":
        print("Aborted -- name didn't match.")
        return 1
    assert result.record is not None
    record = result.record
    instance_attrs = record["instance_attrs"]
    print(f"Onboarded '{args.name}':")
    print(f"  region: {record['region']}")
    print(f"  os_volume_id: {record['os_volume_id']}")
    print(f"  reserved_ip: {record['reserved_ip']}")
    print(f"  network_interface_model: {record['network_interface_model']}")
    print(f"  data_volumes: {len(record['data_volumes'])}")
    print(f"  label: {record['label']}")
    print(f"  tags: {record['tags']}")
    print(f"  plan_type: {instance_attrs['plan_type']}")
    print(f"  firewall_ids: {instance_attrs['firewall_ids']}")
    print(f"  placement_group_id: {instance_attrs['placement_group_id']}")
    print(f"  maintenance_policy: {instance_attrs['maintenance_policy']}")
    print(f"  watchdog_enabled: {instance_attrs['watchdog_enabled']}")
    print(f"  currently: running as instance {record['current_linode_id']} "
          "(not touched by onboarding)")
    print(f"Use `stop --name {args.name}` when ready to stop billing, "
          f"`start --name {args.name}` to bring it back.")
    return 0


def _on_retry(attempt, delay, exc):
    print(f"  failed to start — retrying in {delay}s (attempt {attempt}): {exc}")


def _raise_if_needs_manual_recovery(record, name: str) -> None:

    if record.get("current_status") != "needs_manual_recovery":
        return
    raise NeedsManualRecoveryError(
        f"'{name}' was only partially recovered by `rebuild` -- it was stopped when local "
        "state was lost, so its network config and SSH access couldn't be recovered (only "
        "its resource IDs could). Boot it once manually via Cloud Manager from its "
        f"os_volume_id ({record.get('os_volume_id')}), then run `onboard` again to fully "
        "restore management before using `start`/`stop`."
    )


@dataclass
class StartResult:

    outcome: Literal[
        "started", "already_running", "confirmed_gone_reset_to_stopped",
        "found_offline_marked_unreachable", "transitioning",
        "reachability_check_failed", "create_failed", "post_start_hook_failed",
    ]
    instance_id: int | None = None
    hook_output: str | None = None
    reserved_ip: str | None = None
    live_status: str | None = None
    security_warning: bool = False
    detail: str | None = None
    manual_override_expires_at: str | None = None


def _resolve_ssh_target(
    reserved_ip: str | None, network_config: list | None, network_interface_model: str | None, *,
    subject: str,
) -> str:

    if reserved_ip:
        return reserved_ip
    address = engine.vpc_or_vlan_address(network_config, network_interface_model)
    if not address:
        raise engine.ConfigError(
            f"'{subject}' has no reserved IP and no VPC/VLAN interface address to reach "
            "it by -- this should be impossible for an onboarded instance; the local "
            "record may be corrupted."
        )
    return address


def _forget_unowned_host_key(address: str, on_progress: Callable[[str], None] | None) -> None:

    try:
        if engine.read_known_host_entry(address) is None:
            return
        for other_name, rec in load_registry().items():
            try:
                if _ssh_target(rec, other_name) == address:
                    return
            except (engine.ConfigError, KeyError):
                continue
        engine.reset_known_host(address)
    except Exception:
        return
    if on_progress is not None:
        on_progress(
            f"  Forgot the SSH host key this tool held for {address}: no managed instance uses "
            "that address any more (an earlier instance had it)."
        )


def _ssh_target(record: dict, name: str) -> str:

    reserved_ip = record.get("reserved_ip")
    if reserved_ip:
        return reserved_ip
    return _resolve_ssh_target(
        None, record["network_config"], record["network_interface_model"], subject=name,
    )


@_logs_activity("start")
def start_instance(
    client, name: str, ssh_key: str, *,
    triggered_by: Literal["schedule", "manual", "api"] = "manual",
    override_window_hours: float | None = None,
    actor: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
    on_created: Callable[[int], None] | None = None,
    on_retry: Callable[[int, int, Exception], None] | None = None,
    skip_hooks: bool = False,
    respect_dependencies: bool = False,
) -> StartResult:

    _validate_override_window_hours(override_window_hours)
    if triggered_by != "schedule":
        if respect_dependencies:
            require_dependency(name, "start")
        else:
            _warn_about_dependency(name, "start", on_warning)
    result = _start_instance_locked(
        client, name, ssh_key, triggered_by=triggered_by, override_window_hours=override_window_hours,
        on_progress=on_progress, on_created=on_created, on_retry=on_retry, on_warning=on_warning,
    )
    _record_start_event(name, result, triggered_by, on_warning, actor)
    if result.outcome == "started":
        result = _run_post_start_hook_after_start(
            name, ssh_key, result, skip_hooks=skip_hooks, triggered_by=triggered_by,
            actor=actor, on_progress=on_progress, on_warning=on_warning,
        )
    return result


def _run_post_start_hook_after_start(
    name: str, ssh_key: str, result: StartResult, *, skip_hooks: bool,
    triggered_by: Literal["schedule", "manual", "api"], actor: str | None,
    on_progress: Callable[[str], None] | None, on_warning: Callable[[str], None] | None,
) -> StartResult:

    record = load_registry().get(name)
    if record is None:
        return result
    hook = resolve_effective_hooks(name, record)["post_start"]
    if hook is None:
        return result
    if skip_hooks:
        if on_warning is not None:
            on_warning(f"  WARNING: post-start check for '{name}' NOT run (--skip-hooks given).")
        _best_effort_hook_event(
            name, "post_start", triggered_by, "skipped", actor=actor, exit_code=None,
            output_tail=None, detail="skipped: --skip-hooks given", on_progress=on_warning,
        )
        return result
    hook_result = run_post_start_hook(
        name, hook, _ssh_target(record, name), ssh_key,
        triggered_by=triggered_by, actor=actor, on_progress=on_progress,
    )
    if hook_result.ok:
        return result
    return replace(
        result, outcome="post_start_hook_failed",
        detail=(
            f"'{name}' started and is running (and billing), but its {hook_result.summary}. "
            "It was left running for someone to investigate -- re-run the check with "
            f"`hooks-run --name {name} --post-start` once fixed."
        ),
        hook_output=hook_result.output_tail,
    )


def _compute_manual_override_expiry(
    name: str, record: dict, triggered_by: Literal["schedule", "manual", "api"],
    override_window_hours: float | None,
) -> str | None:

    if triggered_by == "schedule":
        return None
    if get_schedule_mode(name) == "manual":
        return None
    schedule, _via_group = resolve_effective_schedule(name, record)


    if not schedule_is_active(schedule):
        return None
    if is_within_scheduled_on_window(schedule, datetime.now(UTC)):
        return None
    hours = (
        override_window_hours if override_window_hours is not None
        else DEFAULT_MANUAL_OVERRIDE_WINDOW_HOURS
    )
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def _os_volume_holder(client, record: dict) -> int | None:

    volume = engine.retry_transient(lambda: client.load(Volume, record["os_volume_id"]))
    holder = getattr(volume, "linode_id", None)
    return holder if isinstance(holder, int) else None


def _adopt_holder_locked(
    client, name: str, record: dict, holder: int, *,
    on_warning: Callable[[str], None] | None, source: str,
) -> None:

    instance = engine.retry_transient(lambda: client.load(Instance, holder))
    previous = (record.get("current_status"), record.get("current_linode_id"))
    record["current_linode_id"] = holder
    record["current_status"] = "running" if instance.status == "running" else "unreachable"
    _save_one_record(name, record)
    message = (
        f"'{name}' was recorded as {previous[0]}"
        f"{f' (instance {previous[1]})' if previous[1] else ''}, but its OS volume is attached to "
        f"instance {holder} ({instance.status}) -- recorded that instance as the node "
        f"({record['current_status']}) instead of creating another one.")
    record_activity(message, level="warning", source=source, instance_name=name, action="heal")
    if on_warning is not None:
        on_warning(f"WARNING: {message}")


def _blank_leftovers(client, record: dict, *, attached: set[int] | None = None) -> list:

    label = record.get("label")
    if not label:
        return []
    found = engine.retry_transient(lambda: list(client.linode.instances(Instance.label == label)))
    leftovers = []
    for inst in found:
        if inst.label != label or inst.id == record.get("current_linode_id"):
            continue
        if attached is None:
            volumes = engine.retry_transient(lambda inst=inst: list(inst.volumes()))
            if volumes:
                continue
        elif inst.id in attached:
            continue
        if engine.retry_transient(lambda inst=inst: list(inst.disks)):
            continue
        leftovers.append(inst)
    return leftovers


def _delete_leftover(client, name: str, inst, *, source: str,
                     on_warning: Callable[[str], None] | None) -> None:


    inst_id, inst_label = inst.id, inst.label
    engine.retry_transient_or_already_done(
        inst.delete, lambda: engine._resource_confirmed_gone(client, inst))
    message = (f"deleted leftover instance {inst_id} ('{inst_label}'): it had '{name}''s label but "
               "no disks and none of its volumes -- an instance created for a start that never "
               "finished recording.")
    record_activity(message, level="warning", source=source, instance_name=name, action="heal")
    if on_warning is not None:
        on_warning(f"WARNING: {message}")


def _adopt_if_held_elsewhere_locked(
    client, name: str, record: dict, *, on_warning: Callable[[str], None] | None,
) -> bool:

    holder = _os_volume_holder(client, record)
    if holder is None or holder == record.get("current_linode_id"):
        return False
    _adopt_holder_locked(client, name, record, holder, on_warning=on_warning, source="system")
    return True


def _heal_stopped_record_locked(
    client, name: str, record: dict, *, on_warning: Callable[[str], None] | None,
    clean_leftovers: bool,
) -> None:

    if record.get("current_status") != "stopped":
        return
    try:
        if _adopt_if_held_elsewhere_locked(client, name, record, on_warning=on_warning):
            return
        if clean_leftovers:
            for inst in _blank_leftovers(client, record):
                _delete_leftover(client, name, inst, source="system", on_warning=on_warning)
    except (ApiError, requests.exceptions.RequestException, engine.ConfigError) as e:
        if on_warning is not None:
            on_warning(f"WARNING: could not compare '{name}' with the account ({e}); continuing.")
    except Exception:
        pass


LEFTOVER_MIN_AGE_SECONDS = 15 * 60
RECONCILE_INTERVAL_SECONDS = 5 * 60


@dataclass
class ReconcileResult:
    adopted: list[str] = field(default_factory=list)
    reset_to_stopped: list[str] = field(default_factory=list)
    leftovers_deleted: list[str] = field(default_factory=list)
    locks_cleared: list[str] = field(default_factory=list)
    busy: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.adopted or self.reset_to_stopped or self.leftovers_deleted
                    or self.locks_cleared)


def _instance_age_seconds(inst, now: datetime) -> float:
    created = getattr(inst, "created", None)
    if not isinstance(created, datetime):
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (now - created).total_seconds()


def reconcile_fleet(
    client, *, leftover_min_age_s: float = LEFTOVER_MIN_AGE_SECONDS,
    now: datetime | None = None, names: Collection[str] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> ReconcileResult:

    result = ReconcileResult()
    now = now or datetime.now(UTC)
    try:
        volumes = engine.retry_transient(lambda: list(client.volumes()))
        instances = engine.retry_transient(lambda: list(client.linode.instances()))
    except Exception as e:
        result.problems.append(f"could not list the account's volumes/instances: {e}")
        return result
    holder_of = {v.id: v.linode_id for v in volumes if getattr(v, "linode_id", None)}
    attached = set(holder_of.values())
    by_id = {i.id: i for i in instances}
    by_label: dict[str, list] = {}
    for inst in instances:
        by_label.setdefault(inst.label, []).append(inst)

    for name in sorted(names if names is not None else load_registry()):
        try:
            with _instance_lock(name):
                record = load_registry().get(name)
                if record is None or record.get("current_status") == "needs_manual_recovery":
                    continue
                if record.get("transitioning"):
                    engine.release_transition_lock(record, persist_fn=lambda r, n=name: _save_one_record(n, r))
                    result.locks_cleared.append(name)
                    record_activity(
                        f"cleared '{name}''s in-progress mark: no start/stop was running for it "
                        "(left behind by an interrupted one)",
                        level="warning", source="scheduler", instance_name=name, action="heal")
                holder = holder_of.get(record["os_volume_id"])
                if holder is not None and holder != record.get("current_linode_id"):
                    _adopt_holder_locked(client, name, record, holder, on_warning=on_warning,
                                         source="scheduler")
                    result.adopted.append(name)
                elif (holder is None and record.get("current_linode_id")
                      and record.get("current_status") in ("running", "unreachable")
                      and record["current_linode_id"] not in by_id):
                    gone = record["current_linode_id"]
                    record.update(current_status="stopped", current_linode_id=None,
                                  manual_override_expires_at=None)
                    _save_one_record(name, record)
                    result.reset_to_stopped.append(name)
                    record_activity(
                        f"'{name}' was recorded as running on instance {gone}, which no longer "
                        "exists and nothing holds its OS volume -- recorded as stopped.",
                        level="warning", source="scheduler", instance_name=name, action="heal")
                for inst in by_label.get(record.get("label") or "", []):
                    if (inst.id == record.get("current_linode_id") or inst.id in attached
                            or _instance_age_seconds(inst, now) < leftover_min_age_s):
                        continue
                    if engine.retry_transient(lambda inst=inst: list(inst.disks)):
                        continue
                    _delete_leftover(client, name, inst, source="scheduler", on_warning=on_warning)
                    result.leftovers_deleted.append(name)
        except InstanceLockedError:
            result.busy.append(name)
        except Exception as e:
            result.problems.append(f"{name}: {e}")
    if on_progress is not None and result.changed:
        on_progress(
            f"Self-healing: {len(result.adopted)} adopted, {len(result.reset_to_stopped)} reset "
            f"to stopped, {len(result.leftovers_deleted)} leftover instance(s) deleted, "
            f"{len(result.locks_cleared)} stale lock(s) cleared.")
    return result


def _start_instance_locked(
    client, name: str, ssh_key: str, *,
    triggered_by: Literal["schedule", "manual", "api"],
    override_window_hours: float | None,
    on_progress: Callable[[str], None] | None,
    on_created: Callable[[int], None] | None,
    on_retry: Callable[[int, int, Exception], None] | None,
    on_warning: Callable[[str], None] | None = None,
) -> StartResult:
    with _instance_lock(name):


        registry = load_registry()
        record = registry.get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")
        _heal_stopped_record_locked(client, name, record, on_warning=on_warning,
                                    clean_leftovers=True)

        def _confirmed_gone_reset_to_stopped(rec: dict, instance_id: int | None) -> StartResult:


            with contextlib_suppress(ApiError, requests.exceptions.RequestException):
                if _adopt_if_held_elsewhere_locked(client, name, rec, on_warning=on_warning):
                    if rec["current_status"] == "running":
                        return StartResult(outcome="already_running",
                                           instance_id=rec["current_linode_id"],
                                           reserved_ip=rec["reserved_ip"])
                    return StartResult(outcome="found_offline_marked_unreachable",
                                       instance_id=rec["current_linode_id"])


            rec["current_status"] = "stopped"
            rec["current_linode_id"] = None
            rec["manual_override_expires_at"] = None
            _save_one_record(name, rec)
            return StartResult(outcome="confirmed_gone_reset_to_stopped", instance_id=instance_id)

        if record["current_status"] == "running":


            def _already_running(rec: dict) -> StartResult:


                return StartResult(
                    outcome="already_running",
                    instance_id=rec["current_linode_id"], reserved_ip=rec["reserved_ip"],
                )


            try:
                instance = engine.retry_transient(
                    lambda: client.load(Instance, record["current_linode_id"])
                )
            except ApiError as e:
                if e.status == 404:
                    return _confirmed_gone_reset_to_stopped(record, record["current_linode_id"])
                return _already_running(record)
            except requests.exceptions.RequestException:
                return _already_running(record)
            if instance.status == "offline":


                record["current_status"] = "unreachable"


                record["manual_override_expires_at"] = None
                _save_one_record(name, record)
                return StartResult(
                    outcome="found_offline_marked_unreachable",
                    instance_id=record["current_linode_id"],
                )
            if instance.status != "running":


                return StartResult(
                    outcome="transitioning", instance_id=record["current_linode_id"],
                    live_status=instance.status,
                )
            return _already_running(record)
        _raise_if_needs_manual_recovery(record, name)

        if record["current_status"] == "unreachable":


            instance_id = record["current_linode_id"]
            if on_progress is not None:
                on_progress(
                    f"'{name}' has an unreachable instance ({instance_id}) from a previous "
                    "start attempt -- retrying the reachability check instead of creating a "
                    "new one..."
                )
            try:
                instance = engine.retry_transient(lambda: client.load(Instance, instance_id))
                if instance.status not in ("running", "booting"):


                    instance = engine.poll_until_status(
                        lambda: client.load(Instance, instance_id),
                        ("offline", "running", "booting"), timeout_s=120,
                    )
                    if instance.status == "offline":


                        engine.retry_transient_or_already_done(
                            instance.boot,
                            lambda: client.load(Instance, instance_id).status
                            in ("running", "booting"),
                        )
                engine.poll_until_status(
                    lambda: client.load(Instance, instance_id), ("running",), timeout_s=180
                )
                if on_progress is not None:
                    on_progress("  running -- verifying real network reachability...")
                if _first_contact_trust(name, record, registry):
                    engine.ssh_run(
                        _ssh_target(record, name), ssh_key, "echo ok", retries=12, retry_delay_s=10,
                        trust_new=True,
                    )
                else:
                    engine.ssh_run(_ssh_target(record, name), ssh_key, "echo ok", retries=12, retry_delay_s=10)
            except ApiError as e:
                if e.status == 404:


                    return _confirmed_gone_reset_to_stopped(record, instance_id)
                return StartResult(
                    outcome="reachability_check_failed", instance_id=instance_id,
                    security_warning=_is_host_key_mismatch(e), detail=str(e),
                )
            except requests.exceptions.RequestException as e:


                return StartResult(
                    outcome="reachability_check_failed", instance_id=instance_id,
                    security_warning=_is_host_key_mismatch(e), detail=str(e),
                )
            except (RuntimeError, TimeoutError) as e:
                return StartResult(
                    outcome="reachability_check_failed", instance_id=instance_id,
                    security_warning=_is_host_key_mismatch(e), detail=str(e),
                )
            record["current_status"] = "running"
            record["manual_override_expires_at"] = _compute_manual_override_expiry(
                name, record, triggered_by, override_window_hours,
            )
            _save_one_record(name, record)
            _refresh_recovery_tags_after_start(client, name, record, on_warning)
            return StartResult(
                outcome="started", instance_id=instance_id, reserved_ip=record["reserved_ip"],
                manual_override_expires_at=record["manual_override_expires_at"],
            )


        if on_progress is not None:
            on_progress(f"Starting '{name}'...")
        ip_problem = _reserved_ip_unavailable(client, record.get("reserved_ip"))
        if ip_problem is not None:
            return StartResult(outcome="create_failed", detail=ip_problem)
        trust_new_once = False
        try:
            trust_new_once = _reclaim_vpc_addresses_locked(
                client, name, record, registry, on_progress, on_warning,
            )
        except Exception as e:
            if on_warning is not None:
                on_warning(
                    f"WARNING: could not check whether '{name}''s VPC address is still free ({e}); "
                    "starting anyway."
                )
        try:


            golden_volume = engine.retry_transient(
                lambda: client.load(Volume, record["os_volume_id"])
            )
            data_volume_devices = engine.build_data_volume_devices(record["data_volumes"])


            network_config, lost_nat = engine.prepare_nat_1_1_for_recreate(
                client, record["network_config"], record["network_interface_model"],
            )
            with engine.transitioning(record, persist_fn=lambda r: _save_one_record(name, r)) as record:
                instance, _config = engine.create_and_boot_instance_with_retry(
                    client,
                    region=record["region"],
                    label=record["label"],
                    tags=record["tags"],
                    golden_volume=golden_volume,
                    reserved_ip=record["reserved_ip"],
                    captured_network={
                        "network_interface_model": record["network_interface_model"],
                        "network_config": network_config,
                        "network_helper_enabled": record["network_helper_enabled"],
                    },


                    authorized_keys=None,
                    root_pass=secrets.token_urlsafe(24),
                    preserve_host_keys=True,
                    vpc_prefix=record["vpc_prefix"],
                    data_volume_devices=data_volume_devices,
                    instance_attrs=record.get("instance_attrs"),
                    on_retry=on_retry,
                    on_created=on_created,
                )
                record["current_linode_id"] = instance.id


                record["current_status"] = "unreachable"
        except (ApiError, RuntimeError, requests.exceptions.RequestException) as e:
            detail = str(e)
            if "already in use in the subnet" in detail:
                detail += (
                    f" -- another instance took this node's VPC address while it was stopped. "
                    f"Move '{name}' to a free address with `set-vpc-address --name {name} "
                    "--address <free address>` (or from its dashboard page), then start it again."
                )
            return StartResult(outcome="create_failed", detail=detail)


        if on_progress is not None:
            on_progress(f"  instance: {instance.id}")
        if lost_nat and on_warning is not None:
            new_public = ", ".join(str(a) for a in (getattr(instance, "ipv4", None) or [])) or "a new address"
            on_warning(
                f"WARNING: '{name}''s VPC 1:1 NAT public address {', '.join(lost_nat)} wasn't "
                f"reserved, so Linode released it when the instance was stopped; it now has "
                f"{new_public}. Reserve the new address to keep it across stops and starts."
            )

        try:
            engine.poll_until_status(
                lambda: client.load(Instance, instance.id), ("running",), timeout_s=180
            )
            if on_progress is not None:
                on_progress("  running -- verifying real network reachability...")


            if trust_new_once:
                engine.ssh_run(
                    _ssh_target(record, name), ssh_key, "echo ok", retries=12, retry_delay_s=10,
                    trust_new=True,
                )
            else:
                engine.ssh_run(_ssh_target(record, name), ssh_key, "echo ok", retries=12, retry_delay_s=10)
        except (
            ApiError, RuntimeError, TimeoutError, requests.exceptions.RequestException,
        ) as e:


            return StartResult(
                outcome="reachability_check_failed", instance_id=instance.id,
                security_warning=_is_host_key_mismatch(e), detail=str(e),
            )
        record["current_status"] = "running"
        record["manual_override_expires_at"] = _compute_manual_override_expiry(
            name, record, triggered_by, override_window_hours,
        )
        _save_one_record(name, record)
        _refresh_recovery_tags_after_start(client, name, record, on_warning)
        return StartResult(
            outcome="started", instance_id=instance.id, reserved_ip=record["reserved_ip"],
            manual_override_expires_at=record["manual_override_expires_at"],
        )


def _refresh_recovery_tags_after_start(
    client, name: str, record: dict, on_warning: Callable[[str], None] | None,
) -> None:

    try:
        engine.tag_managed_resources(
            client, name, os_volume_id=record["os_volume_id"],
            data_volume_ids=[dv["volume_id"] for dv in (record.get("data_volumes") or [])],
            reserved_ip=record.get("reserved_ip"),
        )
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"  WARNING: could not refresh disaster-recovery tags for '{name}' after "
                f"starting it ({e}) -- it's running normally; the next stop retries this."
            )
    try:
        _sync_hook_tags_locked(client, name, record, on_warning)
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"  WARNING: could not refresh hook tags for '{name}' after starting it ({e})."
            )
    _resync_config_tags_locked(client, name, record, on_warning)


def _resync_config_tags_locked(
    client, name: str, record: dict, on_warning: Callable[[str], None] | None,
) -> None:

    if client is None or not record.get("os_volume_id"):
        return

    def _attempt(what: str, fn) -> None:
        try:
            fn()
        except Exception as e:
            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not refresh {what} tags for '{name}' ({e}) -- the setting "
                    "itself is in effect locally; the next start or stop retries this."
                )

    _attempt("schedule", lambda: _sync_schedule_tags_locked(
        client, name, record, get_instance_schedule(name)))

    def _group() -> None:
        group_id = record.get("group_id")
        group = _group_row_by_id(group_id) if group_id is not None else None
        if group_id is not None and group is None:
            return
        _sync_group_membership_tags_locked(client, name, record, group)

    _attempt("group", _group)
    _attempt("manual-only", lambda: _sync_schedule_mode_tag_locked(
        client, name, record, get_schedule_mode(name)))
    _attempt("holiday setting", lambda: _sync_holiday_setting_tag_locked(
        client, name, record, get_instance_account_holidays(name)))


def _is_host_key_mismatch(e: Exception) -> bool:
    return "host key" in str(e).lower() or "REMOTE HOST IDENTIFICATION" in str(e)


def _print_to_stderr(message: str) -> None:

    print(message, file=sys.stderr)


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BUSY = 3
EXIT_SECURITY = 4
EXIT_DEPENDENCY = 5


def _flag(args, attr: str) -> bool:


    return getattr(args, attr, False) is True


def _cli_error_exit(e: Exception, as_json: bool, name: str) -> int:

    if isinstance(e, InstanceLockedError):
        code = EXIT_BUSY
    elif isinstance(e, DependencyNotSatisfiedError):
        code = EXIT_DEPENDENCY
    else:
        code = EXIT_FAILED
    if as_json:
        print(json.dumps({"name": name, "ok": False, "error": str(e),
                          "error_type": type(e).__name__, "exit_code": code}))
    else:
        print(f"Configuration error: {e}", file=sys.stderr)
    return code


def _cli_result_exit(name: str, result, printer, as_json: bool) -> int:

    if not as_json:
        code = printer(name, result)
    else:
        with redirect_stdout(sys.stderr):
            code = printer(name, result)
    if getattr(result, "security_warning", False):
        code = EXIT_SECURITY
    if as_json:
        print(json.dumps({"name": name, "ok": code == EXIT_OK, "exit_code": code,
                          **asdict(result)}))
    return code


def cmd_group_action(client, args, action: Literal["start", "stop"]) -> int:
    as_json = _flag(args, "json")
    name = args.group_name
    if action == "stop" and not args.yes:
        if as_json:
            print("Configuration error: --json needs --yes (there's no prompt in JSON mode).",
                  file=sys.stderr)
            return EXIT_FAILED
        scope = " and every group that depends on it" if _flag(args, "with_dependencies") else ""
        answer = input(f"Stop every member of group '{name}'{scope}? [y/N] ")
        if answer.strip().lower() != "y":
            print("Aborted.")
            return EXIT_FAILED
    max_parallel = args.max_parallel if isinstance(args.max_parallel, int) else 1
    try:
        result = group_action(
            client, name, action, args.ssh_key,
            include_dependencies=_flag(args, "with_dependencies"),
            respect_dependencies=_flag(args, "respect_dependencies"),
            skip_hooks=_flag(args, "skip_hooks"),
            max_parallel=max_parallel,
            client_factory=_per_thread_client_factory() if max_parallel > 1 else None,
            on_progress=_print_to_stderr if as_json else print,
            on_warning=_print_to_stderr,
        )
    except (InstanceLockedError, engine.ConfigError) as e:
        return _cli_error_exit(e, as_json, name)
    if any(m.security_warning for m in result.members):
        code = EXIT_SECURITY
    else:
        code = EXIT_OK if result.ok else EXIT_FAILED
    if as_json:
        print(json.dumps({"group": name, "action": action, "ok": result.ok, "exit_code": code,
                          "stages": result.stages,
                          "members": [asdict(m) for m in result.members]}))
        return code
    for m in result.members:
        mark = "ok  " if m.ok else "FAIL"
        detail = f" -- {m.detail}" if m.detail and not m.ok else ""
        print(f"  [{mark}] {m.group}/{m.name}: {m.outcome}{detail}")
    done = sum(1 for m in result.members if m.ok)
    print(f"{done} of {len(result.members)} member(s) {action}ed"
          f"{'' if result.ok else ' -- see FAIL lines above'}.".replace("stoped", "stopped"))
    return code


def cmd_start(client, args) -> int:
    if isinstance(getattr(args, "group_name", None), str):
        return cmd_group_action(client, args, "start")
    as_json = _flag(args, "json")
    progress = _print_to_stderr if as_json else print
    try:
        result = start_instance(
            client, args.name, args.ssh_key,
            override_window_hours=args.override_window_hours,
            on_progress=progress, on_warning=_print_to_stderr,
            on_created=lambda i: progress(f"  instance created: {i} (booting...)"),
            on_retry=_on_retry,
            skip_hooks=_flag(args, "skip_hooks"),
            respect_dependencies=_flag(args, "respect_dependencies"),
        )
    except (InstanceLockedError, engine.ConfigError) as e:
        return _cli_error_exit(e, as_json, args.name)
    return _cli_result_exit(args.name, result, _print_start_result, as_json)


def _print_start_result(name: str, result: StartResult) -> int:

    if result.outcome == "already_running":
        ip_note = f"IP {result.reserved_ip}" if result.reserved_ip else "no public IP (VPC/VLAN-only)"
        print(f"'{name}' is already running (instance {result.instance_id}, {ip_note}).")
        return 0
    if result.outcome == "confirmed_gone_reset_to_stopped":
        print(
            f"Configuration error: instance {result.instance_id} no longer exists (confirmed "
            f"404) -- '{name}' has been reset to 'stopped'. Its OS volume, data volume(s), and "
            "reserved IP are unaffected. Run `start` again to recreate it.", file=sys.stderr,
        )
        return 1
    if result.outcome == "found_offline_marked_unreachable":
        print(f"'{name}' instance {result.instance_id} exists but is offline -- likely powered "
              "off out-of-band. Marking it for recovery...")
        print(
            f"Configuration error: '{name}' was found powered off, not deleted -- run "
            f"`start --name {name}` again to boot it back up and verify reachability.",
            file=sys.stderr,
        )
        return 1
    if result.outcome == "transitioning":
        print(f"'{name}' instance {result.instance_id} is currently transitioning "
              f"(status: {result.live_status}), not powered off -- try `start --name {name}` "
              "again shortly once it settles.", file=sys.stderr)
        return 1
    if result.outcome == "reachability_check_failed":
        _print_start_reachability_failure(
            name, result.instance_id, result.detail, result.security_warning,
        )
        return 1
    if result.outcome == "create_failed":
        print(f"Configuration error: failed to start '{name}': {result.detail}", file=sys.stderr)
        return 1
    if result.outcome == "post_start_hook_failed":
        print(f"Configuration error: {result.detail}", file=sys.stderr)
        _print_hook_output(result.hook_output)
        return 1

    if result.reserved_ip:
        print(f"'{name}' is up at {result.reserved_ip}.")
    else:


        print(f"'{name}' is up (reachable over its VPC/VLAN interface, no public IP).")
    if result.manual_override_expires_at is not None:
        print(f"  manually started outside its scheduled hours -- auto-stops at "
              f"{_format_override_expiry(result.manual_override_expires_at)} unless extended "
              f"(`extend --name {name}`).")
    return 0


def _print_hook_output(output: str | None) -> None:

    if not output:
        return
    print("  hook output (last lines):", file=sys.stderr)
    for line in output.splitlines()[-20:]:
        print(f"    {line}", file=sys.stderr)


def _format_override_expiry(iso_timestamp: str) -> str:

    return datetime.fromisoformat(iso_timestamp).strftime("%Y-%m-%d %H:%M UTC")


def _print_start_reachability_failure(
    name: str, instance_id: int | None, detail: str | None, security_warning: bool,
) -> None:

    if security_warning:
        print(
            f"SECURITY WARNING: '{name}' has instance {instance_id} but it presented a "
            f"DIFFERENT SSH host key than the one on record: {detail}. This should never happen "
            "after a steady-state recreate -- do not proceed. If this key change is genuinely "
            "expected (e.g. you intentionally replaced the disk), confirm out-of-band first, "
            f"then run `reset-host-key --name {name}` before retrying.",
            file=sys.stderr,
        )
    else:
        print(
            f"Configuration error: '{name}' has instance {instance_id} but it could not be "
            f"confirmed reachable: {detail}. Check Cloud Manager directly -- it may still be "
            f"booting; `status --name {name}` shows it as 'unreachable' (a real instance "
            f"exists, but its health check hasn't passed) until a retried `start --name {name}` "
            "succeeds.",
            file=sys.stderr,
        )


@dataclass
class StopResult:

    outcome: Literal[
        "already_stopped", "confirmed_gone_reset_to_stopped", "aborted_by_user",
        "prepare_failed", "pre_stop_hook_failed", "delete_failed", "stopped",
    ]
    instance_id: int | None = None
    detail: str | None = None
    hook_output: str | None = None


@_logs_activity("stop")
def stop_instance(
    client, name: str, ssh_key: str, *,
    skip_precapture: bool = False, force: bool = False, skip_hooks: bool = False,
    triggered_by: Literal["schedule", "manual", "api"] = "manual",
    actor: str | None = None,
    confirm: Callable[[dict], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
    respect_dependencies: bool = False,
) -> StopResult:

    if triggered_by != "schedule":
        if respect_dependencies:
            require_dependency(name, "stop")
        else:
            _warn_about_dependency(name, "stop", on_warning)
    result = _stop_instance_locked(
        client, name, ssh_key,
        skip_precapture=skip_precapture, force=force, skip_hooks=skip_hooks,
        triggered_by=triggered_by, actor=actor,
        confirm=confirm, on_progress=on_progress, on_warning=on_warning,
    )
    _record_stop_event(name, result, triggered_by, on_warning, actor)
    return result


def _stop_instance_locked(
    client, name: str, ssh_key: str, *,
    skip_precapture: bool,
    force: bool,
    skip_hooks: bool = False,
    triggered_by: Literal["schedule", "manual", "api"] = "manual",
    actor: str | None = None,
    confirm: Callable[[dict], bool] | None,
    on_progress: Callable[[str], None] | None,
    on_warning: Callable[[str], None] | None,
) -> StopResult:
    with _instance_lock(name):
        registry = load_registry()
        record = registry.get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded.")
        if record["current_status"] == "stopped":


            _heal_stopped_record_locked(client, name, record, on_warning=on_warning,
                                        clean_leftovers=False)
        if record["current_status"] == "stopped":
            return StopResult(outcome="already_stopped")
        _raise_if_needs_manual_recovery(record, name)

        def _reset_to_stopped_confirmed_gone(rec: dict) -> StopResult:


            old_instance_id = rec["current_linode_id"]
            rec["current_status"] = "stopped"
            rec["current_linode_id"] = None
            rec["manual_override_expires_at"] = None
            _save_one_record(name, rec)
            return StopResult(outcome="confirmed_gone_reset_to_stopped", instance_id=old_instance_id)

        if record["current_status"] in ("unreachable", "running"):


            try:


                engine.retry_transient(lambda: client.load(Instance, record["current_linode_id"]))
            except ApiError as e:
                if e.status == 404:


                    adopted = False
                    with contextlib_suppress(ApiError, requests.exceptions.RequestException):
                        adopted = _adopt_if_held_elsewhere_locked(client, name, record,
                                                                  on_warning=on_warning)
                    if not adopted:
                        return _reset_to_stopped_confirmed_gone(record)
            except requests.exceptions.RequestException:
                pass

        if confirm is not None and not confirm(record):
            return StopResult(outcome="aborted_by_user")

        try:


            try:


                instance = engine.retry_transient(
                    lambda: client.load(Instance, record["current_linode_id"])
                )
            except ApiError as e:
                if e.status != 404:
                    raise
                return _reset_to_stopped_confirmed_gone(record)


            golden_volume = engine.retry_transient(
                lambda: client.load(Volume, record["os_volume_id"])
            )

            if on_progress is not None:
                on_progress(
                    f"Re-capturing network config, data volumes, authorized_keys, label, tags, "
                    f"and instance attributes before stopping '{name}' (an out-of-band change "
                    "since the last start/stop shouldn't be silently reverted)..."
                )


            configs = list(instance.configs)
            fresh_network = engine.capture_network_config(instance, configs=configs)


            fresh_data_volumes = engine.capture_data_volumes(
                instance, record["os_volume_id"], configs=configs,
            )
            if skip_precapture:


                if on_warning is not None:
                    on_warning(
                        "  WARNING: --skip-precapture given -- using the last-known "
                        "authorized_keys from the registry instead of a fresh SSH capture. An "
                        "SSH key added/revoked out of band since the last start/stop cycle "
                        "would be silently missed."
                    )
                fresh_authorized_keys = record["authorized_keys"]
            else:


                authorized_keys_raw = engine.ssh_run(
                    _resolve_ssh_target(
                        record.get("reserved_ip"), fresh_network["network_config"],
                        fresh_network["network_interface_model"], subject=name,
                    ),
                    ssh_key, "cat /root/.ssh/authorized_keys 2>/dev/null",
                )
                fresh_authorized_keys = [
                    line.strip() for line in authorized_keys_raw.splitlines() if line.strip()
                ]
                if not fresh_authorized_keys:
                    raise RuntimeError(
                        "could not read any authorized_keys from the instance -- refusing to "
                        "proceed without knowing how the next `start` would grant SSH access "
                        "(use --skip-precapture to keep the last-known list instead, if that's "
                        "genuinely intended)"
                    )


            data_volumes_now = []
            still_recorded = []
            for dv in fresh_data_volumes:
                try:


                    data_volumes_now.append(
                        engine.retry_transient(lambda dv=dv: client.load(Volume, dv["volume_id"]))
                    )
                    still_recorded.append(dv)
                except ApiError as e:
                    if e.status != 404:
                        raise
                    if on_warning is not None:
                        on_warning(
                            f"  WARNING: data volume {dv['volume_id']} no longer exists "
                            "(detached or deleted out of band since the last cycle) -- "
                            f"dropping it from the recorded data volume list for '{name}'."
                        )
            fresh_data_volumes = still_recorded
            fresh_label = instance.label
            fresh_tags = list(instance.tags)
            fresh_instance_attrs = engine.capture_instance_attributes(instance)
        except (ApiError, RuntimeError, requests.exceptions.RequestException) as e:
            return StopResult(outcome="prepare_failed", detail=f"{e}. Nothing was deleted.")

        try:
            engine.tag_managed_resources(
                client, name, os_volume_id=record["os_volume_id"],
                data_volume_ids=[dv["volume_id"] for dv in fresh_data_volumes],
                reserved_ip=record["reserved_ip"],
            )
        except (engine.ResourceOwnershipConflict, engine.TagVerificationError) as e:


            raise engine.ConfigError(
                f"could not refresh disaster-recovery tags for '{name}' ({e}). Refusing to "
                "stop -- this would delete the live instance while the account-side recovery "
                "mapping is provably wrong. Resolve the conflict (or re-onboard) before "
                "retrying."
            ) from e
        except (ApiError, requests.exceptions.RequestException) as e:


            if not force:
                raise engine.ConfigError(
                    f"could not refresh disaster-recovery tags for '{name}' ({e}). Refusing to "
                    "stop -- pass --force to proceed anyway if this is a known transient API "
                    "issue."
                ) from e
            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not refresh disaster-recovery tags ({e}) -- --force "
                    "given, continuing with the stop anyway."
                )


        pre_stop_hook = resolve_effective_hooks(name, record)["pre_stop"]
        if pre_stop_hook is not None:
            skip_reason = (
                "--skip-hooks given" if skip_hooks
                else "--skip-precapture given (instance treated as unreachable over SSH)"
                if skip_precapture else None
            )
            if skip_reason is not None:
                if on_warning is not None:
                    on_warning(
                        f"  WARNING: pre-stop hook for '{name}' NOT run ({skip_reason}) -- "
                        "stopping without it."
                    )
                _best_effort_hook_event(
                    name, "pre_stop", triggered_by, "skipped", actor=actor, exit_code=None,
                    output_tail=None, detail=f"skipped: {skip_reason}", on_progress=on_warning,
                )
            else:
                hook_result = run_pre_stop_hook(
                    name, pre_stop_hook,

                    _resolve_ssh_target(
                        record.get("reserved_ip"), fresh_network["network_config"],
                        fresh_network["network_interface_model"], subject=name,
                    ),
                    ssh_key, triggered_by=triggered_by, actor=actor, on_progress=on_progress,
                )
                if not hook_result.ok:
                    if hook_result.on_failure == "abort":
                        return StopResult(
                            outcome="pre_stop_hook_failed", instance_id=instance.id,
                            detail=(
                                f"{hook_result.summary}. Stop aborted -- nothing was shut down "
                                f"or deleted; '{name}' is still running. Fix the cause and run "
                                "stop again, or use --skip-hooks to stop without the hook."
                            ),
                            hook_output=hook_result.output_tail,
                        )
                    if on_warning is not None:
                        on_warning(
                            f"  WARNING: {hook_result.summary} for '{name}' -- its on_failure "
                            "policy is 'continue', so stopping anyway."
                        )

        if on_progress is not None:
            on_progress(f"Stopping '{name}' (confirmed detach before delete)...")
        try:
            with engine.transitioning(record, persist_fn=lambda r: _save_one_record(name, r)) as record:
                engine.delete_instance_and_detach_volumes(
                    client, instance, [golden_volume, *data_volumes_now]
                )
                record["network_config"] = fresh_network["network_config"]
                record["network_helper_enabled"] = fresh_network["network_helper_enabled"]
                record["data_volumes"] = fresh_data_volumes
                record["authorized_keys"] = fresh_authorized_keys
                record["label"] = fresh_label
                record["tags"] = fresh_tags
                record["instance_attrs"] = fresh_instance_attrs
                record["current_linode_id"] = None
                record["current_status"] = "stopped"
                record["manual_override_expires_at"] = None
        except (ApiError, RuntimeError, requests.exceptions.RequestException) as e:


            return StopResult(
                outcome="delete_failed",
                detail=f"{e}. Check Cloud Manager directly -- it may be partially deleted; "
                f"`status --name {name}` reflects whatever was captured before the failure.",
            )


        try:
            matched_key_ids, _unmatched_keys = _classify_authorized_keys(client, fresh_authorized_keys)
            _sync_extra_recovery_tags_locked(client, name, record, matched_key_ids)


            _sync_hook_tags_locked(client, name, record, on_warning)
        except Exception as e:
            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not sync network-config/SSH-key recovery tags for "
                    f"'{name}' onto its OS volume ({e}) -- '{name}' is fully stopped and "
                    "correct locally; this only affects recovery after a total local database "
                    "loss (safe to retry: just run `stop`/`start` again)."
                )
        _resync_config_tags_locked(client, name, record, on_warning)
        osb.sync_object_storage_backup(
            name, _backup_payload(name, record), on_warning=on_warning,
        )

        return StopResult(outcome="stopped")


def cmd_stop(client, args) -> int:
    if isinstance(getattr(args, "group_name", None), str):
        return cmd_group_action(client, args, "stop")
    as_json = _flag(args, "json")

    def _confirm(record: dict) -> bool:
        answer = input(f"Stop '{args.name}' (instance {record['current_linode_id']})? [y/N] ")
        return answer.strip().lower() == "y"

    if as_json and not args.yes:
        print("Configuration error: --json needs --yes (there's no prompt in JSON mode).",
              file=sys.stderr)
        return EXIT_FAILED
    try:
        result = stop_instance(
            client, args.name, args.ssh_key,
            skip_precapture=args.skip_precapture, force=args.force,
            skip_hooks=_flag(args, "skip_hooks"),
            confirm=None if args.yes else _confirm,
            on_progress=_print_to_stderr if as_json else print,
            on_warning=_print_to_stderr,
            respect_dependencies=_flag(args, "respect_dependencies"),
        )
    except (InstanceLockedError, engine.ConfigError) as e:
        return _cli_error_exit(e, as_json, args.name)
    return _cli_result_exit(args.name, result, _print_stop_result, as_json)


def _print_stop_result(name: str, result: StopResult) -> int:

    if result.outcome == "already_stopped":
        print(f"'{name}' is already stopped.")
        return 0
    if result.outcome == "confirmed_gone_reset_to_stopped":
        print(
            f"'{name}': instance {result.instance_id} no longer exists (confirmed 404, "
            "presumably deleted out-of-band) -- reset to 'stopped'. Its OS volume, data "
            "volume(s), and reserved IP are unaffected."
        )
        return 0
    if result.outcome == "aborted_by_user":
        print("Aborted.")
        return 1
    if result.outcome == "prepare_failed":
        print(f"Configuration error: failed to prepare '{name}' for stopping: {result.detail}",
              file=sys.stderr)
        return 1
    if result.outcome == "pre_stop_hook_failed":
        print(f"Configuration error: {result.detail}", file=sys.stderr)
        _print_hook_output(result.hook_output)
        return 1
    if result.outcome == "delete_failed":
        print(f"Configuration error: failed to stop '{name}': {result.detail}", file=sys.stderr)
        return 1

    print(f"'{name}' stopped; OS volume, data volume(s), and reserved IP all survive.")
    return 0


def _set_override_source(name: str, source: str, expires_at: str) -> None:
    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO override_sources (instance_name, source, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(instance_name) DO UPDATE SET source = excluded.source, "
                "expires_at = excluded.expires_at", (name, source, expires_at))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def override_source(name: str, record: dict) -> str | None:

    expires = record.get("manual_override_expires_at")
    if not expires:
        return None

    def _do():
        conn = _connect()
        try:
            return conn.execute("SELECT source, expires_at FROM override_sources WHERE "
                                "instance_name = ?", (name,)).fetchone()
        finally:
            conn.close()

    row = _retry_db(_do)
    return row[0] if row and row[1] == expires else "instance"


def _extension_target(name: str, record: dict, hours: float, now: datetime) -> datetime:

    base = now
    schedule, _via = resolve_effective_schedule(name, record)
    if schedule_is_active(schedule) and is_within_scheduled_on_window(schedule, now):
        next_stop = next_scheduled_stop(schedule, now)
        if next_stop is not None:
            base = next_stop
    return base + timedelta(hours=hours)


def extend_manual_override(name: str, hours: float | None = None) -> str:

    _validate_override_window_hours(hours)
    window_hours = hours if hours is not None else DEFAULT_MANUAL_OVERRIDE_WINDOW_HOURS
    with _instance_lock(name):
        registry = load_registry()
        record = registry.get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded.")
        if record.get("current_status") != "running":
            raise engine.ConfigError(f"'{name}' is not currently running -- nothing to extend.")
        now = datetime.now(UTC)
        source = override_source(name, record)
        if source is None:
            if get_schedule_modes().get(name) == "manual":
                raise engine.ConfigError(
                    f"'{name}' is manual-only -- the scheduler never stops it, so there's nothing "
                    "to extend.")
            schedule, _via_group = resolve_effective_schedule(name, record)
            if not schedule_is_active(schedule):
                raise engine.ConfigError(
                    f"'{name}' has no active schedule -- it keeps running until you stop it, so "
                    "there's nothing to extend.")
        target = _extension_target(name, record, window_hours, now)
        if source == "instance":
            current = datetime.fromisoformat(record["manual_override_expires_at"])
            target = max(target, current)
        new_expiry = target.isoformat()
        record["manual_override_expires_at"] = new_expiry
        _save_one_record(name, record)
        _set_override_source(name, "instance", new_expiry)
        group_id = record.get("group_id")
    if group_id is not None:


        _hold_dependency_groups(group_id, new_expiry)
    return new_expiry


def _dependency_group_ids(group_id: int) -> set[int]:

    dep_map = _group_dependency_map()
    seen: set[int] = set()
    stack = list(dep_map.get(group_id, ()))
    while stack:
        gid = stack.pop()
        if gid in seen or gid == group_id:
            continue
        seen.add(gid)
        stack.extend(dep_map.get(gid, ()))
    return seen


def _hold_until(name: str, expiry: str, source: str) -> str:

    with _instance_lock(name):
        record = load_registry().get(name)
        if record is None or record.get("current_status") != "running":
            return "not running"
        current_source = override_source(name, record)
        if source == "group" and current_source == "instance":
            return "own"
        current = record.get("manual_override_expires_at")
        if current and datetime.fromisoformat(current) >= datetime.fromisoformat(expiry):
            return "later"
        record["manual_override_expires_at"] = expiry
        _save_one_record(name, record)
        _set_override_source(name, source, expiry)
        return "set"


def _hold_dependency_groups(group_id: int, expiry: str) -> list[str]:

    deps = _dependency_group_ids(group_id)
    if not deps:
        return []
    until = datetime.fromisoformat(expiry)
    modes = get_schedule_modes()
    held = []
    for name, record in load_registry().items():
        if record.get("group_id") not in deps or record.get("current_status") != "running":
            continue
        if modes.get(name) == "manual":
            continue
        schedule, _via = resolve_effective_schedule(name, record)
        if not schedule_is_active(schedule):
            continue
        next_stop = next_scheduled_stop(schedule, datetime.now(UTC))
        if next_stop is not None and next_stop >= until:
            continue
        with contextlib_suppress(InstanceLockedError):
            if _hold_until(name, expiry, "hold") == "set":
                held.append(name)
    return held


@dataclass
class GroupExtendResult:
    group: str
    stops_at: str
    extended: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    dependencies_held: list[str] = field(default_factory=list)


def extend_group(group_name: str, hours: float | None = None) -> GroupExtendResult:

    _validate_override_window_hours(hours)
    group = get_schedule_group(group_name)
    if group is None:
        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    if not group.get("enabled", True) or not group.get("rules"):
        raise engine.ConfigError(
            f"group '{group_name}' has no active schedule -- nothing stops its members, so "
            "there's nothing to extend.")
    now = datetime.now(UTC)
    window_hours = hours if hours is not None else DEFAULT_MANUAL_OVERRIDE_WINDOW_HOURS
    schedule = {"timezone": group["timezone"], "rules": group["rules"], "enabled": True}
    next_stop = next_scheduled_stop(schedule, now)
    base = next_stop if next_stop is not None and is_within_scheduled_on_window(schedule, now) else now
    expiry = (base + timedelta(hours=window_hours)).isoformat()
    result = GroupExtendResult(group=group_name, stops_at=expiry)
    modes = get_schedule_modes()
    for name, record in sorted(load_registry().items()):
        if record.get("group_id") != group["id"]:
            continue
        if record.get("current_status") != "running":
            result.skipped.append({"name": name, "reason": "not running"})
            continue
        if modes.get(name) == "manual":
            result.skipped.append({"name": name, "reason": "manual-only"})
            continue
        _schedule, via_group = resolve_effective_schedule(name, record)
        if via_group != group_name:
            result.skipped.append({"name": name, "reason": "follows its own schedule"})
            continue
        try:
            outcome = _hold_until(name, expiry, "group")
            if outcome == "set":
                result.extended.append(name)
            else:
                result.skipped.append({"name": name, "reason": {
                    "own": "has its own extension (it overrides the group's)",
                    "later": "already running later",
                    "not running": "not running"}[outcome]})
        except InstanceLockedError:
            result.skipped.append({"name": name, "reason": "busy (a start/stop is in progress)"})
    result.dependencies_held = _hold_dependency_groups(group["id"], expiry)
    return result


@_logs_activity("hook-run", name_arg=0)
def run_hook_now(
    name: str, hook_type: Literal["pre_stop", "post_start"], ssh_key: str, *,
    triggered_by: Literal["schedule", "manual", "api"] = "manual", actor: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> HookRunResult:

    def _load() -> dict:
        record = load_registry().get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded.")
        if record.get("current_status") != "running":
            raise engine.ConfigError(
                f"'{name}' is not running (status: {record.get('current_status')}) -- hooks run "
                "against a running instance."
            )
        return record

    def _hook_for(record: dict) -> dict:
        hook = resolve_effective_hooks(name, record)[hook_type]
        if hook is None:
            raise engine.ConfigError(f"'{name}' has no {hook_type.replace('_', '-')} hook configured.")
        return hook

    if hook_type == "pre_stop":
        with _instance_lock(name):
            record = _load()
            return run_pre_stop_hook(
                name, _hook_for(record), _ssh_target(record, name), ssh_key,
                triggered_by=triggered_by, actor=actor, on_progress=on_progress,
            )
    record = _load()
    return run_post_start_hook(
        name, _hook_for(record), _ssh_target(record, name), ssh_key,
        triggered_by=triggered_by, actor=actor, on_progress=on_progress,
    )


def _merge_hook_cli_args(existing: dict | None, args) -> dict:

    config = {t: (dict(existing[t]) if existing and existing.get(t) else None) for t in _HOOK_TYPES}
    for hook_type, cmd_attr, timeout_attr, clear_attr in (
        ("pre_stop", "pre_stop", "pre_stop_timeout", "clear_pre_stop"),
        ("post_start", "post_start", "post_start_timeout", "clear_post_start"),
    ):
        command = getattr(args, cmd_attr, None)
        timeout = getattr(args, timeout_attr, None)
        script_path = getattr(args, f"{cmd_attr}_script", None)
        script = None
        if script_path is not None:
            try:
                script = Path(script_path).read_text()
            except (OSError, UnicodeDecodeError) as e:
                raise engine.ConfigError(f"could not read script file {script_path}: {e}") from e
        if getattr(args, clear_attr, False):
            if command is not None or timeout is not None or script is not None:
                raise engine.ConfigError(
                    f"--{clear_attr.replace('_', '-')} can't be combined with other "
                    f"--{hook_type.replace('_', '-')} options."
                )
            config[hook_type] = None
            continue
        if command is not None:


            config[hook_type] = {
                **{k: v for k, v in (config[hook_type] or {}).items() if k != "script"},
                "command": command,
            }
        if script is not None:
            config[hook_type] = {
                **{k: v for k, v in (config[hook_type] or {}).items() if k != "command"},
                "script": script,
            }
        if timeout is not None:
            entry = config[hook_type]
            if entry is None:
                raise engine.ConfigError(
                    f"--{timeout_attr.replace('_', '-')} given but there's no "
                    f"{hook_type.replace('_', '-')} hook -- also pass --{cmd_attr.replace('_', '-')} "
                    f"or --{cmd_attr.replace('_', '-')}-script."
                )
            entry["timeout_s"] = timeout
    on_failure = getattr(args, "pre_stop_on_failure", None)
    if on_failure is not None:
        pre_stop = config["pre_stop"]
        if pre_stop is None:
            raise engine.ConfigError(
                "--pre-stop-on-failure given but there's no pre-stop hook -- also pass --pre-stop."
            )
        pre_stop["on_failure"] = on_failure
    return config


def cmd_hooks_set(client, args) -> int:
    try:
        if args.group_name is not None:
            config = _merge_hook_cli_args(get_group_hooks(args.group_name), args)
            normalized = set_group_hooks(
                args.group_name, config, client=client, on_warning=_print_to_stderr,
            )
            target = f"group '{args.group_name}'"
        else:
            config = _merge_hook_cli_args(get_instance_hooks(args.name), args)
            normalized = set_instance_hooks(
                args.name, config, client=client, on_warning=_print_to_stderr,
            )
            target = f"'{args.name}'"
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Hooks for {target}: {_hook_config_summary(normalized)}")
    print("  These commands run as root on the instance over SSH -- every change is recorded in history.")
    return 0


def cmd_hooks_show(args) -> int:
    try:
        if args.group_name is not None:
            config = get_group_hooks(args.group_name)
            print(json.dumps({"group": args.group_name, "hooks": config}, indent=2))
            return 0
        record = load_registry().get(args.name)
        if record is None:
            raise NotOnboardedError(f"'{args.name}' is not onboarded.")
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(json.dumps({
        "instance": args.name,
        "own_hooks": get_instance_hooks(args.name),
        "effective_hooks": resolve_effective_hooks(args.name, record),
    }, indent=2))
    return 0


def cmd_hooks_clear(client, args) -> int:
    try:
        if args.group_name is not None:
            cleared = clear_group_hooks(args.group_name, client=client, on_warning=_print_to_stderr)
            target = f"group '{args.group_name}'"
        else:
            if args.name not in load_registry():
                raise NotOnboardedError(f"'{args.name}' is not onboarded.")
            cleared = clear_instance_hooks(args.name, client=client, on_warning=_print_to_stderr)
            target = f"'{args.name}'"
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Hooks cleared for {target}." if cleared else f"No hooks were set for {target}.")
    return 0


def cmd_hooks_run(args) -> int:
    hook_type: Literal["pre_stop", "post_start"] = "pre_stop" if args.pre_stop else "post_start"
    if hook_type == "pre_stop" and not args.yes:
        answer = input(
            f"Run the pre-stop hook on '{args.name}' now? It will NOT be stopped afterward, so "
            "whatever the hook stops stays stopped until restarted. [y/N] "
        )
        if answer.strip().lower() != "y":
            print("Aborted.")
            return 1
    try:
        result = run_hook_now(args.name, hook_type, args.ssh_key, on_progress=print)
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.ok:
        print(f"'{args.name}': {result.summary}.")
        return 0
    print(f"Configuration error: '{args.name}': {result.summary}.", file=sys.stderr)
    _print_hook_output(result.output_tail)
    return 1


def cmd_extend(args) -> int:
    if isinstance(getattr(args, "group_name", None), str):
        try:
            result = extend_group(args.group_name, args.hours)
        except engine.ConfigError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            return 1
        print(f"Group '{result.group}' extended -- its members now stop at "
              f"{_format_override_expiry(result.stops_at)}: "
              f"{', '.join(result.extended) or '(none running)'}.")
        for item in result.skipped:
            print(f"  skipped {item['name']}: {item['reason']}")
        if result.dependencies_held:
            print(f"  also held (start order): {', '.join(result.dependencies_held)}")
        return 0
    try:
        new_expiry = extend_manual_override(args.name, args.hours)
    except NotOnboardedError as e:
        print(f"{e}", file=sys.stderr)
        return 1


    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"'{args.name}' extended -- its scheduled stop is skipped and it now stops at "
          f"{_format_override_expiry(new_expiry)}.")
    return 0


@dataclass
class OffboardResult:

    outcome: Literal["aborted_by_user", "incomplete", "offboarded", "pending_deletion"]
    detail: str | None = None

    pending_deletion: dict | None = None


DELETION_GRACE_DAYS = 7
PENDING_DELETE_TAG_PREFIX = "lis-pending-delete:"
PENDING_UNTAG_TAG_PREFIX = "lis-pending-untag:"
PENDING_OF_TAG_PREFIX = "lis-pending-of:"
_PENDING_TAG_PREFIXES = (PENDING_DELETE_TAG_PREFIX, PENDING_UNTAG_TAG_PREFIX, PENDING_OF_TAG_PREFIX)


def deletion_grace_days() -> int:

    raw = os.environ.get("LIS_DELETION_GRACE_DAYS", "").strip()
    try:
        return max(0, int(raw)) if raw else DELETION_GRACE_DAYS
    except ValueError:
        return DELETION_GRACE_DAYS


def pending_marker_from_tags(tags: list[str] | None, name: str) -> str | None:

    tags = tags or []
    if f"{PENDING_OF_TAG_PREFIX}{name}" not in tags:
        return None
    for t in tags:
        if t.startswith((PENDING_DELETE_TAG_PREFIX, PENDING_UNTAG_TAG_PREFIX)):
            return t.split(":", 1)[1]
    return None


def _load_pending_resource(client, res: dict):
    if res["type"] == "volume":
        return client.load(Volume, res["id"])
    if res["type"] == "ip":
        return client.load(engine.ReservedIPAddress, res["id"])
    return client.load(Instance, res["id"])


def _set_pending_tags(client, res: dict, name: str, due: str | None) -> None:

    try:
        obj = engine.retry_transient(lambda: _load_pending_resource(client, res))
    except ApiError as e:
        if e.status == 404:
            return
        raise
    current = list(getattr(obj, "tags", None) or [])
    tags = [t for t in current if not t.startswith(_PENDING_TAG_PREFIXES)]
    if due is not None:
        prefix = PENDING_UNTAG_TAG_PREFIX if res.get("action") == "untag" else PENDING_DELETE_TAG_PREFIX
        tags += [f"{prefix}{due[:10]}", f"{PENDING_OF_TAG_PREFIX}{name}"]
    if sorted(tags) == sorted(current):
        return
    obj.tags = tags
    engine.retry_transient(obj.save)
    fresh = engine.retry_transient(lambda: _load_pending_resource(client, res))
    if sorted(getattr(fresh, "tags", None) or []) != sorted(tags):
        raise engine.TagVerificationError(
            f"{res['type']} {res['id']}: pending-deletion tags didn't save.")


def _pending_row_to_dict(row) -> dict:
    keys = ("id", "name", "kind", "status", "created_at", "due_at", "resources", "bundle",
            "detail", "updated_at")
    entry = dict(zip(keys, row, strict=True))
    entry["resources"] = json.loads(entry["resources"] or "[]")
    entry["bundle"] = json.loads(entry["bundle"]) if entry["bundle"] else None
    return entry


_PENDING_COLUMNS = ("id, name, kind, status, created_at, due_at, resources, bundle, detail, "
                    "updated_at")


def list_pending_deletions(*, include_done: bool = False) -> list[dict]:
    def _do():
        conn = _connect()
        try:
            sql = f"SELECT {_PENDING_COLUMNS} FROM pending_deletions"
            if not include_done:
                sql += " WHERE status IN ('pending', 'failed')"
            return conn.execute(sql + " ORDER BY due_at, id").fetchall()
        finally:
            conn.close()

    return [_pending_row_to_dict(r) for r in _retry_db(_do)]


def get_pending_deletion(entry_id: int) -> dict | None:
    def _do():
        conn = _connect()
        try:
            return conn.execute(f"SELECT {_PENDING_COLUMNS} FROM pending_deletions WHERE id = ?",
                                (entry_id,)).fetchone()
        finally:
            conn.close()

    row = _retry_db(_do)
    return _pending_row_to_dict(row) if row else None


def pending_deletion_for(name: str, kind: str | None = None) -> dict | None:

    for entry in list_pending_deletions():
        if entry["name"] == name and (kind is None or entry["kind"] == kind):
            return entry
    return None


def _insert_pending_row(entry: dict) -> int:
    def _do():
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO pending_deletions (name, kind, status, created_at, due_at, resources, "
                "bundle, detail, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entry["name"], entry["kind"], entry.get("status", "pending"), entry["created_at"],
                 entry["due_at"], json.dumps(entry.get("resources") or []),
                 json.dumps(entry["bundle"]) if entry.get("bundle") is not None else None,
                 entry.get("detail"), entry.get("updated_at") or entry["created_at"]))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    return _retry_db(_do)


def _mirror_pending(entry: dict, on_warning: Callable[[str], None] | None = None) -> None:

    if not osb.is_configured():
        return
    try:
        if entry["status"] in ("pending", "failed"):
            osb.upload_pending_deletion(entry["kind"], entry["name"], entry)
        else:
            osb.delete_pending_deletion(entry["kind"], entry["name"])
    except Exception as e:
        if on_warning is not None:
            on_warning(f"WARNING: couldn't update the Object Storage copy of the pending deletion "
                       f"of '{entry['name']}' ({e}); its tags still mark it.")


def _update_pending(entry_id: int, **fields) -> dict | None:
    fields["updated_at"] = datetime.now(UTC).isoformat()

    def _do():
        conn = _connect()
        try:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE pending_deletions SET {sets} WHERE id = ?",
                         (*[json.dumps(v) if k in ("resources", "bundle") else v
                            for k, v in fields.items()], entry_id))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)
    entry = get_pending_deletion(entry_id)
    if entry is not None:
        _mirror_pending(entry)
    return entry


def schedule_pending_deletion(
    client, name: str, kind: str, resources: list[dict], *, bundle: dict | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> dict:

    now = datetime.now(UTC)
    due = (now + timedelta(days=deletion_grace_days())).isoformat()
    for res in resources:
        _set_pending_tags(client, res, name, due)
    entry = {"name": name, "kind": kind, "status": "pending", "created_at": now.isoformat(),
             "due_at": due, "resources": resources, "bundle": bundle}
    entry_id = _insert_pending_row(entry)
    entry = get_pending_deletion(entry_id) or {**entry, "id": entry_id}
    _mirror_pending(entry, on_warning)
    if on_progress is not None:
        what = ", ".join(f"{r['type']} {r['id']}" for r in resources) or "nothing"
        on_progress(f"  pending deletion #{entry_id}: {what} -- carried out automatically on "
                    f"{due[:10]} unless recovered or deleted sooner")
    record_activity(f"{kind.replace('_', ' ')} '{name}' pending deletion until {due[:10]} "
                    f"(#{entry_id})", level="warning", source="system", instance_name=name,
                    action="pending-deletion")
    return entry


def _carry_out_pending_resource(client, res: dict, name: str) -> str:

    try:
        obj = engine.retry_transient(lambda: _load_pending_resource(client, res))
    except ApiError as e:
        if e.status == 404:
            return "already gone"
        raise
    tags = list(getattr(obj, "tags", None) or [])
    if f"{PENDING_OF_TAG_PREFIX}{name}" not in tags:
        return "kept: no longer marked pending deletion"
    owners = set(engine.names_from_tags(tags))
    if owners - {name}:
        return f"kept: now belongs to {', '.join(sorted(owners))}"
    if res.get("action") == "untag":
        engine.untag_managed_resources(client, name, os_volume_id=res["id"] if res.get("role") == "os" else None,
                                       data_volume_ids=[res["id"]] if res.get("role") == "data" else [])
        if res.get("role") == "os":
            _strip_recovery_metadata_tags(client, res["id"])
        _set_pending_tags(client, res, name, None)
        return "kept, untagged"
    if res["type"] == "volume" and getattr(obj, "linode_id", None):
        return f"kept: attached to instance {obj.linode_id}"
    if res["type"] == "ip" and getattr(obj, "linode_id", None):
        return f"kept: assigned to instance {obj.linode_id}"
    engine.retry_transient_or_already_done(
        obj.delete, lambda: engine._resource_confirmed_gone(client, obj))
    if res["type"] == "ip":
        with contextlib_suppress(Exception):
            engine.reset_known_host(res["id"])
    return "deleted"


def delete_pending_now(client, entry_id: int, *, actor: str | None = None,
                       on_progress: Callable[[str], None] | None = None,
                       on_warning: Callable[[str], None] | None = None) -> dict:

    entry = get_pending_deletion(entry_id)
    if entry is None:
        raise engine.ConfigError(f"no pending deletion #{entry_id}.")
    if entry["status"] not in ("pending", "failed"):
        raise engine.ConfigError(f"pending deletion #{entry_id} is already {entry['status']}.")
    name = entry["name"]
    if entry["kind"] == "node" and name in load_registry():
        raise engine.ConfigError(f"a node named '{name}' is onboarded again -- this pending "
                                 "deletion can only be recovered or left to expire after it's "
                                 "offboarded.")
    results, failed = [], False
    with _instance_lock(name):
        if entry["kind"] == "migration_backup":
            backup = get_migration_backup(name)
            if backup is None:
                results.append("backup record already gone")
            else:
                problems = engine.delete_migration_backup_resources(
                    client, backup["record"], name, on_progress=on_progress)
                if problems:
                    failed = True
                    results.extend(problems)
                else:
                    results.append("backup instance and cloned volumes deleted")
                    _delete_migration_backup_row(name)
                    _sync_migration_backup_record(name, on_warning)
        else:
            order = {"volume": 0, "ip": 1}
            for res in sorted(entry["resources"], key=lambda r: order.get(r["type"], 9)):
                try:
                    outcome = _carry_out_pending_resource(client, res, name)
                except Exception as e:
                    outcome, failed = f"failed: {e}", True
                res["outcome"] = outcome
                results.append(f"{res['type']} {res['id']}: {outcome}")
                if on_progress is not None:
                    on_progress(f"  {res['type']} {res['id']}: {outcome}")
            if not failed:
                with contextlib_suppress(Exception):
                    if osb.is_configured():
                        osb.delete_instance_backup(name)
    status = "failed" if failed else "deleted"
    updated = _update_pending(entry_id, status=status, resources=entry["resources"],
                              detail="; ".join(results))
    record_activity(f"pending deletion #{entry_id} ({name}): {status} -- " + "; ".join(results),
                    level="warning" if failed else "info", source="system", instance_name=name,
                    action="pending-deletion", actor=actor)
    return updated or entry


def process_due_deletions(client, *, now: datetime | None = None,
                          on_warning: Callable[[str], None] | None = None) -> list[int]:

    now = now or datetime.now(UTC)
    handled = []
    for entry in list_pending_deletions():
        if datetime.fromisoformat(entry["due_at"]) > now:
            continue
        try:
            done = delete_pending_now(client, entry["id"], on_warning=on_warning)
            handled.append(entry["id"])
            if done["status"] == "failed" and on_warning is not None:
                on_warning(f"WARNING: pending deletion #{entry['id']} ({entry['name']}) didn't "
                           f"complete: {done['detail']} -- retried on the next pass.")
        except Exception as e:
            if on_warning is not None:
                on_warning(f"WARNING: pending deletion #{entry['id']} ({entry['name']}) "
                           f"failed: {e}")
    return handled


def recover_pending(client, entry_id: int, *, actor: str | None = None,
                    on_progress: Callable[[str], None] | None = None,
                    on_warning: Callable[[str], None] | None = None) -> dict:

    entry = get_pending_deletion(entry_id)
    if entry is None:
        raise engine.ConfigError(f"no pending deletion #{entry_id}.")
    if entry["status"] not in ("pending", "failed"):
        raise engine.ConfigError(f"pending deletion #{entry_id} is {entry['status']} -- only an "
                                 "open one can be recovered.")
    name = entry["name"]
    for res in entry["resources"]:
        try:
            engine.retry_transient(lambda res=res: _load_pending_resource(client, res))
        except ApiError as e:
            if e.status == 404:
                raise engine.ConfigError(f"{res['type']} {res['id']} no longer exists, so '{name}' "
                                         "can't be recovered.") from e
            raise
    if entry["kind"] == "migration_backup":
        for res in entry["resources"]:
            _set_pending_tags(client, res, name, None)
    else:
        bundle = entry.get("bundle") or {}
        record = bundle.get("record")
        if not record:
            raise engine.ConfigError("this pending deletion has no saved record to recover from.")
        problems: list[str] = []
        with _instance_lock(name):
            if name in load_registry():
                raise engine.ConfigError(f"a node named '{name}' is onboarded again -- offboard or "
                                         "deregister it before recovering this one.")
            record = {**record, "current_status": "stopped", "current_linode_id": None,
                      "transitioning": False, "manual_override_expires_at": None,
                      "group_id": None}
            _save_one_record(name, record)
            for res in entry["resources"]:
                try:
                    _set_pending_tags(client, res, name, None)
                except Exception as e:
                    problems.append(f"pending tag on {res['type']} {res['id']}: {e}")
        if on_progress is not None:
            on_progress(f"  '{name}' is back in the registry (stopped)")
        steps: list[tuple[str, Callable[[], object]]] = []
        schedule = bundle.get("schedule")
        group_name = bundle.get("group")
        if schedule:
            steps.append(("schedule", lambda: save_instance_schedule(
                client, name, schedule["timezone"], schedule["rules"],
                schedule.get("enabled", True), on_warning=on_warning)))
        if group_name:
            if get_schedule_group(group_name) is not None:
                steps.append(("group", lambda: assign_instance_to_group(
                    client, name, group_name, on_warning=on_warning)))
            else:
                problems.append(f"group '{group_name}' no longer exists")
        if bundle.get("hooks"):
            steps.append(("hooks", lambda: set_instance_hooks(
                name, bundle["hooks"], client=client, actor=actor, on_warning=on_warning)))
        if bundle.get("schedule_mode") == "manual":
            steps.append(("manual-only", lambda: set_schedule_mode(client, name, "manual")))
        if bundle.get("account_holidays"):
            steps.append(("holiday setting", lambda: set_instance_account_holidays(
                client, name, bundle["account_holidays"])))
        for label, step in steps:
            try:
                step()
                if on_progress is not None:
                    on_progress(f"  {label} restored")
            except Exception as e:
                problems.append(f"{label}: {e}")
        with contextlib_suppress(Exception):
            osb.sync_object_storage_backup(
                name, _backup_payload(name, load_registry().get(name) or record),
                on_warning=on_warning)
        if problems and on_warning is not None:
            on_warning("WARNING: recovered, but: " + "; ".join(problems))
    updated = _update_pending(entry_id, status="recovered")
    record_activity(f"pending deletion #{entry_id} ({name}) cancelled -- "
                    f"{entry['kind'].replace('_', ' ')} recovered", level="warning",
                    source="system", instance_name=name, action="pending-deletion", actor=actor)
    return updated or entry


def _node_recovery_bundle(name: str, record: dict, *, delete_volumes: bool) -> dict:
    group = _group_row_by_id(record["group_id"]) if record.get("group_id") else None
    bundle: dict = {"record": record, "delete_volumes": delete_volumes,
                    "group": group["name"] if group else None, "schedule": None, "hooks": None,
                    "schedule_mode": None, "account_holidays": None}
    with contextlib_suppress(Exception):
        bundle["schedule"] = get_instance_schedule(name)
    with contextlib_suppress(Exception):
        bundle["hooks"] = get_instance_hooks(name) or None
    with contextlib_suppress(Exception):
        bundle["schedule_mode"] = get_schedule_mode(name)
    with contextlib_suppress(Exception):
        bundle["account_holidays"] = get_instance_account_holidays(name)
    return bundle


def _node_pending_resources(record: dict, *, delete_volumes: bool) -> list[dict]:
    action = "delete" if delete_volumes else "untag"
    resources = []
    if record.get("os_volume_id"):
        resources.append({"type": "volume", "id": record["os_volume_id"], "role": "os",
                          "action": action})
    for dv in record.get("data_volumes") or []:
        resources.append({"type": "volume", "id": dv["volume_id"], "role": "data",
                          "action": action})
    if record.get("reserved_ip"):
        resources.append({"type": "ip", "id": record["reserved_ip"], "role": "ip",
                          "action": "delete"})
    return resources


def offboard_to_pending(client, name: str, record: dict, *, delete_volumes: bool,
                        on_progress: Callable[[str], None] | None = None,
                        on_warning: Callable[[str], None] | None = None) -> dict:

    bundle = _node_recovery_bundle(name, record, delete_volumes=delete_volumes)
    entry = schedule_pending_deletion(client, name, "node",
                                      _node_pending_resources(record, delete_volumes=delete_volumes),
                                      bundle=bundle, on_progress=on_progress, on_warning=on_warning)
    with contextlib_suppress(Exception):
        osb.sync_object_storage_backup(name, _backup_payload(name, record), on_warning=on_warning)
    _delete_one_record(name)
    return entry


def restore_pending_deletions_from_object_storage(
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> set[str]:

    keys = osb.list_pending_deletions() if osb.is_configured() else []
    if keys is None and on_warning is not None:
        on_warning("WARNING: couldn't list pending deletions in Object Storage; nodes pending "
                   "deletion are still recognised from their tags.")
    for kind, name in keys or []:
        if pending_deletion_for(name, kind) is not None:
            continue
        record = osb.download_pending_deletion(kind, name)
        if not record or record.get("status") not in ("pending", "failed"):
            continue
        _insert_pending_row(record)
        if on_progress is not None:
            on_progress(f"  pending deletion of {kind.replace('_', ' ')} '{name}' restored "
                        f"(due {record['due_at'][:10]})")
    return {e["name"] for e in list_pending_deletions() if e["kind"] == "node"}


def _rebuilt_node_to_pending(client, name: str, record: dict, due: str) -> dict:

    bundle = _node_recovery_bundle(name, record, delete_volumes=False)
    resources = _node_pending_resources(record, delete_volumes=False)
    for res in resources:
        with contextlib_suppress(Exception):
            obj = engine.retry_transient(lambda res=res: _load_pending_resource(client, res))
            tags = getattr(obj, "tags", None) or []
            if any(t.startswith(PENDING_DELETE_TAG_PREFIX) for t in tags):
                res["action"] = "delete"
    bundle["delete_volumes"] = any(r["action"] == "delete" for r in resources
                                   if r["type"] == "volume")
    now = datetime.now(UTC).isoformat()
    entry_id = _insert_pending_row({"name": name, "kind": "node", "status": "pending",
                                    "created_at": now, "due_at": f"{due}T00:00:00+00:00",
                                    "resources": resources, "bundle": bundle})
    _delete_one_record(name)
    entry = get_pending_deletion(entry_id) or {}
    _mirror_pending(entry)
    return entry


def _is_recovery_metadata_tag(tag: str) -> bool:

    return (_is_schedule_tag(tag)
            or _is_schedule_tag(tag, prefix=_GROUP_SCHEDULE_TAG_PREFIX)
            or tag.startswith(_GROUP_NAME_TAG_PREFIX)
            or tag.startswith(_GROUP_DEP_TAG_PREFIX)
            or tag in (_SCHEDULE_MODE_MANUAL_TAG, _GROUP_HOLIDAY_IGNORE_TAG)
            or tag.startswith(_HOLIDAY_SETTING_TAG_PREFIX)
            or _is_hook_tag(tag)
            or _is_extra_recovery_tag(tag))


def _strip_recovery_metadata_tags(client, os_volume_id: int | None) -> None:
    if not os_volume_id:
        return
    vol = engine.retry_transient(lambda: client.load(Volume, os_volume_id))
    tags = list(vol.tags or [])
    kept = [t for t in tags if not _is_recovery_metadata_tag(t)]
    if kept == tags:
        return
    vol.tags = kept
    engine.retry_transient(vol.save)
    _verify_os_volume_tag_write(client, os_volume_id, kept)


@_logs_activity("offboard")
def offboard_instance(
    client, name: str, *,
    delete_volumes: bool = False,
    grace: bool = False,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> OffboardResult:

    with _instance_lock(name):
        registry = load_registry()
        record = registry.get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded.")
        if record["current_status"] not in ("stopped", "needs_manual_recovery"):
            raise engine.ConfigError(
                f"'{name}' is currently {record['current_status']!r} -- stop it first "
                f"(`stop --name {name}`) before offboarding."
            )

        reserved_ip = record.get("reserved_ip")
        os_volume_id = record.get("os_volume_id")
        data_volume_ids = [dv["volume_id"] for dv in record.get("data_volumes", [])]

        grace_days = deletion_grace_days() if grace else 0
        if grace_days > 0:
            if on_progress is not None:
                on_progress(f"Offboarding '{name}' removes it from management now. Nothing is "
                            f"deleted yet: after {grace_days} day(s) the following happens "
                            "automatically, unless you recover it or delete it sooner from "
                            "Pending deletion:")
                if reserved_ip:
                    on_progress(f"  - reserved IP {reserved_ip} is released")
                if delete_volumes:
                    on_progress(f"  - the OS volume ({os_volume_id}) and {len(data_volume_ids)} "
                                f"data volume(s) {data_volume_ids} are permanently deleted")
                else:
                    on_progress(f"  - the OS volume ({os_volume_id}) and data volume(s) are kept, "
                                "with this node's tags removed")
            if confirm is not None and not confirm():
                return OffboardResult(outcome="aborted_by_user")
            try:
                entry = offboard_to_pending(client, name, record, delete_volumes=delete_volumes,
                                            on_progress=on_progress, on_warning=on_warning)
            except (ApiError, requests.exceptions.RequestException, engine.ConfigError) as e:
                return OffboardResult(
                    outcome="incomplete",
                    detail=f"Could not mark '{name}' pending deletion ({e}). Nothing was deleted "
                    f"and '{name}' is still onboarded. Re-run `offboard --name {name}` to retry.")
            return OffboardResult(outcome="pending_deletion", pending_deletion=entry)

        if on_progress is not None:
            on_progress(f"Offboarding '{name}' will permanently:")
            if reserved_ip:
                on_progress(
                    f"  - release reserved IP {reserved_ip} back to Linode's pool (never reused "
                    "-- a new address would be assigned if this node is ever re-onboarded)"
                )
            else:
                on_progress("  - no reserved IP on record for this node -- nothing to release")
            if delete_volumes:
                on_progress(
                    f"  - PERMANENTLY DELETE the OS volume ({os_volume_id}) and "
                    f"{len(data_volume_ids)} data volume(s) {data_volume_ids} -- this destroys "
                    "real data and cannot be undone"
                )
            else:
                on_progress(
                    f"  - leave the OS volume ({os_volume_id}) and {len(data_volume_ids)} data "
                    "volume(s) intact, untagged so `rebuild` won't resurrect this node "
                    "(pass --delete-volumes to also permanently remove them)"
                )
            on_progress(f"  - remove '{name}' from the local registry")

        if confirm is not None and not confirm():
            return OffboardResult(outcome="aborted_by_user")


        if not delete_volumes:
            try:
                engine.untag_managed_resources(
                    client, name, os_volume_id=os_volume_id, data_volume_ids=data_volume_ids,
                )
            except (ApiError, requests.exceptions.RequestException, engine.ConfigError) as e:


                return OffboardResult(
                    outcome="incomplete",
                    detail=f"Could not remove disaster-recovery tags ({e}). Offboard is "
                    f"INCOMPLETE -- nothing else was changed. Re-run `offboard --name {name}` "
                    "to retry; already-completed steps are safe to repeat.",
                )


            try:
                _strip_recovery_metadata_tags(client, os_volume_id)
            except Exception as e:
                if on_warning is not None:
                    on_warning(f"WARNING: could not remove schedule/group/hook recovery tags from "
                               f"kept OS volume {os_volume_id} ({e}); remove tags starting with "
                               "sched-, grp-, hk-, hkg-, net- or sshkeys by hand if you reuse it.")


        deleted_so_far: list[int | str] = []

        def _incomplete_note() -> str:
            if not deleted_so_far:
                return " -- nothing was deleted"
            return f" -- {deleted_so_far} already permanently deleted above before this step"

        if delete_volumes:


            candidates = [(os_volume_id, engine.REGISTRY_ROLE_TAG_OS)] if os_volume_id else []
            candidates += [(vid, engine.REGISTRY_ROLE_TAG_DATA) for vid in data_volume_ids]

            for vid, role_tag in candidates:
                try:
                    problems, vol = engine.verify_managed_volume_owned_by_name(
                        client, vid, name, role_tag
                    )
                except ApiError as e:
                    if e.status == 404:
                        if on_progress is not None:
                            on_progress(f"  volume {vid}: already gone")


                        deleted_so_far.append(vid)
                        continue
                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Could not verify volume {vid} before deleting it ({e}). "
                        f"Offboard is INCOMPLETE{_incomplete_note()}. Re-run `offboard --name "
                        f"{name} --delete-volumes` to retry.",
                    )
                except requests.exceptions.RequestException as e:


                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Could not verify volume {vid} before deleting it ({e}). "
                        f"Offboard is INCOMPLETE{_incomplete_note()}. Re-run `offboard --name "
                        f"{name} --delete-volumes` to retry.",
                    )
                if problems:
                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Refusing to delete volume {vid} -- its current tags don't "
                        f"confirm it still belongs to '{name}': {'; '.join(problems)}. Offboard "
                        f"is INCOMPLETE{_incomplete_note()}. Review tags directly in Cloud "
                        "Manager before retrying.",
                    )
                try:


                    engine.retry_transient(vol.delete)
                    if on_progress is not None:
                        on_progress(f"  deleted volume {vid}")
                    deleted_so_far.append(vid)
                except ApiError as e:
                    if e.status != 404:
                        return OffboardResult(
                            outcome="incomplete",
                            detail=f"Failed to delete volume {vid} ({e}). Offboard is "
                            f"INCOMPLETE{_incomplete_note()}. Re-run `offboard --name {name} "
                            "--delete-volumes` to retry; already-deleted volumes are safe to "
                            "skip.",
                        )
                    if on_progress is not None:
                        on_progress(f"  volume {vid}: already gone")
                    deleted_so_far.append(vid)
                except requests.exceptions.RequestException as e:


                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Failed to delete volume {vid} ({e}). Offboard is "
                        f"INCOMPLETE{_incomplete_note()}. Re-run `offboard --name {name} "
                        "--delete-volumes` to retry; already-deleted volumes are safe to skip.",
                    )

        if reserved_ip:


            ip = None
            try:
                problems, ip = engine.verify_managed_ip_owned_by_name(client, reserved_ip, name)
            except ApiError as e:
                if e.status != 404:
                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Could not verify reserved IP {reserved_ip} before releasing it "
                        f"({e}). Offboard is INCOMPLETE -- the reserved IP was NOT released"
                        f"{_incomplete_note()}. Re-run `offboard --name {name}` to retry.",
                    )
                if on_progress is not None:
                    on_progress(f"  reserved IP {reserved_ip}: already gone")


                deleted_so_far.append(reserved_ip)
            except requests.exceptions.RequestException as e:
                return OffboardResult(
                    outcome="incomplete",
                    detail=f"Could not verify reserved IP {reserved_ip} before releasing it "
                    f"({e}). Offboard is INCOMPLETE -- the reserved IP was NOT released"
                    f"{_incomplete_note()}. Re-run `offboard --name {name}` to retry.",
                )

            if ip is not None:
                if problems:
                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Refusing to release reserved IP {reserved_ip} -- its current "
                        f"tags don't confirm it still belongs to '{name}': "
                        f"{'; '.join(problems)}. Offboard is INCOMPLETE -- the reserved IP was "
                        f"NOT released{_incomplete_note()}. Review tags directly in Cloud "
                        "Manager before retrying.",
                    )
                try:


                    engine.retry_transient(ip.delete)
                    if on_progress is not None:
                        on_progress(f"  released reserved IP {reserved_ip}")
                except ApiError as e:
                    if e.status != 404:


                        return OffboardResult(
                            outcome="incomplete",
                            detail=f"Failed to release reserved IP {reserved_ip} ({e}). "
                            f"Offboard is INCOMPLETE -- the reserved IP was NOT released"
                            f"{_incomplete_note()}. Re-run `offboard --name {name}` to retry.",
                        )
                    if on_progress is not None:
                        on_progress(f"  reserved IP {reserved_ip}: already gone")


                    deleted_so_far.append(reserved_ip)
                except requests.exceptions.RequestException as e:


                    return OffboardResult(
                        outcome="incomplete",
                        detail=f"Failed to release reserved IP {reserved_ip} ({e}). Offboard is "
                        f"INCOMPLETE -- the reserved IP was NOT released{_incomplete_note()}. "
                        f"Re-run `offboard --name {name}` to retry.",
                    )


            try:
                engine.reset_known_host(reserved_ip)
            except Exception as e:
                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not clear the known-hosts entry for {reserved_ip} "
                        f"({e}) -- if this address is ever reused by a different node, its "
                        f"first onboarding attempt may need `reset-host-key --ip {reserved_ip}` "
                        "first."
                    )

        if not reserved_ip:


            try:
                private_address = _ssh_target(record, name)
                engine.reset_known_host(private_address)
            except Exception as e:
                if on_warning is not None:
                    on_warning(f"  WARNING: could not clear this node's known-hosts entry ({e}).")

        _delete_one_record(name)
        return OffboardResult(outcome="offboarded")


def cmd_offboard(client, args) -> int:
    def _confirm() -> bool:
        answer = input(f"Type '{args.name}' to confirm: ")
        return answer == args.name

    try:
        result = offboard_instance(
            client, args.name, delete_volumes=args.delete_volumes,
            grace=not getattr(args, "now", False),
            confirm=None if args.yes else _confirm,
            on_progress=print, on_warning=_print_to_stderr,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "aborted_by_user":
        print("Aborted -- name didn't match.")
        return 1
    if result.outcome == "incomplete":
        print(f"  {result.detail}", file=sys.stderr)
        return 1
    if result.outcome == "pending_deletion":
        entry = result.pending_deletion or {}
        print(f"'{args.name}' is offboarded and pending deletion (#{entry.get('id')}). Nothing "
              f"has been deleted yet: it happens automatically on {str(entry.get('due_at'))[:10]}."
              f"\n  Recover it:     pending-deletions --recover {entry.get('id')}"
              f"\n  Delete it now:  pending-deletions --delete-now {entry.get('id')}")
        return 0
    print(f"'{args.name}' fully offboarded.")
    return 0


VALID_SCHEDULE_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


_SCHEDULE_TAG_PREFIX = "sched-"
_GROUP_SCHEDULE_TAG_PREFIX = "grp-"


_SCHEDULE_DAY_BITS = {day: 1 << i for i, day in enumerate(VALID_SCHEDULE_DAYS)}


def _is_schedule_tag(tag: str, *, prefix: str = _SCHEDULE_TAG_PREFIX) -> bool:
    return tag.startswith((f"{prefix}tz:", f"{prefix}en:", f"{prefix}r"))


def _encode_schedule_as_tags(schedule: dict, *, prefix: str = _SCHEDULE_TAG_PREFIX) -> list[str]:

    tags = [
        f"{prefix}tz:{schedule['timezone']}",
        f"{prefix}en:{1 if schedule.get('enabled', True) else 0}",
    ]
    for i, rule in enumerate(schedule["rules"]):


        bitmask = sum(_SCHEDULE_DAY_BITS[d] for d in set(rule["days_of_week"]))
        start = rule["start_time"].replace(":", "")
        stop = rule["stop_time"].replace(":", "")
        tags.append(f"{prefix}r{i}:{bitmask}-{start}-{stop}")
    return tags


def _decode_schedule_from_tags(
    tags: list[str] | None, *, prefix: str = _SCHEDULE_TAG_PREFIX, allow_no_rules: bool = False,
) -> dict | None:

    tz_prefix, en_prefix, rule_prefix = f"{prefix}tz:", f"{prefix}en:", f"{prefix}r"
    timezone = None
    enabled = True
    rules_by_index: dict[int, dict] = {}
    for tag in tags or []:
        if tag.startswith(tz_prefix):
            timezone = tag[len(tz_prefix):]
        elif tag.startswith(en_prefix):
            enabled = tag[len(en_prefix):] == "1"
        elif tag.startswith(rule_prefix) and ":" in tag:
            rule_tag_prefix, _, rest = tag.partition(":")
            try:
                index = int(rule_tag_prefix[len(rule_prefix):])
                bitmask_str, start, stop = rest.split("-")
                bitmask = int(bitmask_str)
            except ValueError:
                continue
            days = [d for d in VALID_SCHEDULE_DAYS if bitmask & _SCHEDULE_DAY_BITS[d]]
            if not days or len(start) != 4 or len(stop) != 4:
                continue
            rules_by_index[index] = {
                "days_of_week": days,
                "start_time": f"{start[:2]}:{start[2:]}",
                "stop_time": f"{stop[:2]}:{stop[2:]}",
            }
    if timezone is None or (not rules_by_index and not allow_no_rules):
        return None
    rules = [rules_by_index[i] for i in sorted(rules_by_index)]
    return {"timezone": timezone, "rules": rules, "enabled": enabled}


def _verify_os_volume_tag_write(client, os_volume_id: int, expected_tags: list[str]) -> None:

    fresh = engine.retry_transient(lambda: client.load(Volume, os_volume_id))
    if set(fresh.tags or []) != set(expected_tags):
        raise engine.TagVerificationError(
            f"tag write for OS volume {os_volume_id} did not take effect as expected -- a "
            "fresh read shows different tags than what was just saved."
        )


def _sync_schedule_tags_locked(client, name: str, record: dict, schedule: dict | None) -> None:


    if get_instance_schedule(name) != schedule:
        return
    os_volume = engine.retry_transient(
        lambda: client.load(Volume, record["os_volume_id"])
    )
    kept = [t for t in (os_volume.tags or []) if not _is_schedule_tag(t)]
    new_tags = kept + (_encode_schedule_as_tags(schedule) if schedule else [])
    if new_tags != (os_volume.tags or []):
        os_volume.tags = new_tags
        os_volume.save()
        _verify_os_volume_tag_write(client, record["os_volume_id"], new_tags)


def _sync_schedule_tags(
    client, name: str, schedule: dict | None, *, on_warning: Callable[[str], None] | None = None
) -> None:

    try:


        with _instance_lock(name):


            registry = load_registry()
            record = registry.get(name)
            if record is None or not record.get("os_volume_id"):
                return
            _sync_schedule_tags_locked(client, name, record, schedule)
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"  WARNING: could not sync schedule tags for '{name}' onto its OS volume "
                f"({e}) -- the schedule itself was saved locally and is fully in effect, but "
                "won't be recoverable via `rebuild` if the local database is lost before this "
                "is retried (safe to retry: just run schedule-set again)."
            )


def _validate_schedule_rules(rules: list) -> None:

    if not rules or not isinstance(rules, list):
        raise engine.ConfigError("a schedule needs at least one rule.")
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise engine.ConfigError(f"rule {i}: must be an object with days_of_week/start_time/stop_time.")
        days = rule.get("days_of_week")
        if not days or not isinstance(days, list):
            raise engine.ConfigError(f"rule {i}: days_of_week must be a non-empty list.")
        for day in days:
            if day not in VALID_SCHEDULE_DAYS:
                raise engine.ConfigError(
                    f"rule {i}: invalid day {day!r} -- must be one of {VALID_SCHEDULE_DAYS}."
                )
        if len(set(days)) != len(days):


            raise engine.ConfigError(f"rule {i}: days_of_week has a repeated day -- {days!r}.")
        start_time = rule.get("start_time")
        stop_time = rule.get("stop_time")
        for label, value in (("start_time", start_time), ("stop_time", stop_time)):
            if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
                raise engine.ConfigError(f"rule {i}: {label} must be HH:MM 24-hour, got {value!r}.")
        assert isinstance(start_time, str) and isinstance(stop_time, str)
        if start_time == stop_time:
            raise engine.ConfigError(
                f"rule {i}: start_time and stop_time are both {start_time} -- they must differ "
                "(a stop time earlier than the start time means the stop is on the next day)."
            )


def _rule_stop_day_offset(rule: dict) -> int:

    return 1 if rule["stop_time"] < rule["start_time"] else 0


def _rule_on_minutes(rule: dict) -> int:

    start_h, start_m = (int(p) for p in rule["start_time"].split(":"))
    stop_h, stop_m = (int(p) for p in rule["stop_time"].split(":"))
    return (stop_h * 60 + stop_m - (start_h * 60 + start_m)) % (24 * 60)


def _validate_schedule_timezone(timezone: str) -> None:

    try:
        ZoneInfo(timezone)
    except Exception as e:

        raise engine.ConfigError(f"invalid timezone {timezone!r}: {e}") from e


def _save_schedule_row(name: str, timezone: str, rules: list, enabled: bool) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO schedules (instance_name, timezone, rules, enabled) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(instance_name) DO UPDATE SET timezone = excluded.timezone,"
                " rules = excluded.rules, enabled = excluded.enabled",
                (name, timezone, json.dumps(rules), 1 if enabled else 0),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def save_instance_schedule(
    client, name: str, timezone: str, rules: list, enabled: bool = True,
    on_warning: Callable[[str], None] | None = None,
) -> None:

    _validate_schedule_rules(rules)
    _validate_schedule_timezone(timezone)
    with _instance_lock(name):
        if name not in load_registry():
            raise NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")
        _refuse_if_manual_only(name, "have a schedule")
        _save_schedule_row(name, timezone, rules, enabled)
    _sync_schedule_tags(
        client, name, {"timezone": timezone, "rules": rules, "enabled": enabled},
        on_warning=on_warning,
    )
    _sync_instance_backup_record(name, on_warning)


def _sync_instance_backup_record(name: str, on_warning: Callable[[str], None] | None) -> None:

    if not osb.is_configured():
        return
    try:
        record = load_registry().get(name)
    except Exception as e:
        if on_warning is not None:
            on_warning(f"  WARNING: could not refresh the Object Storage record for '{name}' ({e}).")
        return
    if record is not None:
        osb.sync_object_storage_backup(name, _backup_payload(name, record), on_warning=on_warning)


def get_instance_schedule(name: str) -> dict | None:

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT timezone, rules, enabled FROM schedules WHERE instance_name = ?", (name,)
            ).fetchone()
        finally:
            conn.close()

    row = _retry_db(_do)
    if row is None:
        return None
    return {"timezone": row[0], "rules": json.loads(row[1]), "enabled": bool(row[2])}


def _clear_schedule_row(name: str) -> bool:

    def _do():
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM schedules WHERE instance_name = ?", (name,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    return _retry_db(_do)


def clear_instance_schedule(
    client, name: str, on_warning: Callable[[str], None] | None = None
) -> bool:

    with _instance_lock(name):
        cleared = _clear_schedule_row(name)
    if cleared:
        _sync_schedule_tags(client, name, None, on_warning=on_warning)
        _sync_instance_backup_record(name, on_warning)
    return cleared


DEFAULT_PRE_STOP_HOOK_TIMEOUT_S = 300
DEFAULT_POST_START_HOOK_TIMEOUT_S = 600
MAX_HOOK_TIMEOUT_S = 3600
MAX_HOOK_COMMAND_LENGTH = 4096


MAX_HOOK_SCRIPT_BYTES = 1024 * 1024


_HOOK_SCRIPT_RUNNER = (
    'f=$(mktemp /tmp/linode-scheduler-hook.XXXXXX) && cat > "$f" && chmod 700 "$f" && "$f"; '
    'rc=$?; rm -f "$f"; exit $rc'
)
POST_START_HOOK_RETRY_INTERVAL_S = 15
HOOK_OUTPUT_TAIL_CHARS = 4096
_HOOK_ON_FAILURE_VALUES = ("abort", "continue")
_HOOK_TYPES = ("pre_stop", "post_start")


def _validate_hook_config(config: dict) -> dict:

    if not isinstance(config, dict):
        raise engine.ConfigError("hook config must be an object with pre_stop/post_start keys.")
    unknown = set(config) - set(_HOOK_TYPES)
    if unknown:
        raise engine.ConfigError(
            f"unknown hook type(s) {sorted(unknown)} -- only pre_stop and post_start exist."
        )
    normalized: dict = {}
    for hook_type in _HOOK_TYPES:
        hook = config.get(hook_type)
        if hook is None:
            normalized[hook_type] = None
            continue
        if not isinstance(hook, dict):
            raise engine.ConfigError(f"{hook_type} must be an object (or null to unset it).")
        allowed = {"command", "script", "timeout_s"} | (
            {"on_failure"} if hook_type == "pre_stop" else set()
        )
        extra = set(hook) - allowed
        if extra:
            raise engine.ConfigError(f"{hook_type}: unknown field(s) {sorted(extra)}.")
        has_command, has_script = hook.get("command") is not None, hook.get("script") is not None
        if has_command == has_script:
            raise engine.ConfigError(
                f"{hook_type}: set exactly one of command (run as-is on the instance, e.g. a "
                "script already installed there) or script (uploaded script text)."
            )
        if has_command:
            command = hook["command"]
            if not isinstance(command, str) or not command.strip():
                raise engine.ConfigError(f"{hook_type}: command must be a non-empty string.")
            if len(command) > MAX_HOOK_COMMAND_LENGTH:
                raise engine.ConfigError(
                    f"{hook_type}: command is {len(command)} characters; the limit is "
                    f"{MAX_HOOK_COMMAND_LENGTH}. Upload it as a script instead."
                )
        else:
            script = hook["script"]
            if not isinstance(script, str) or not script.strip():
                raise engine.ConfigError(f"{hook_type}: script must be non-empty text.")
            size = len(script.encode("utf-8"))
            if size > MAX_HOOK_SCRIPT_BYTES:
                raise engine.ConfigError(
                    f"{hook_type}: script is {size} bytes; the limit is {MAX_HOOK_SCRIPT_BYTES}."
                )
        default_timeout = (
            DEFAULT_PRE_STOP_HOOK_TIMEOUT_S if hook_type == "pre_stop"
            else DEFAULT_POST_START_HOOK_TIMEOUT_S
        )
        timeout_s = hook.get("timeout_s", default_timeout)
        if (
            isinstance(timeout_s, bool) or not isinstance(timeout_s, int)
            or not 1 <= timeout_s <= MAX_HOOK_TIMEOUT_S
        ):
            raise engine.ConfigError(
                f"{hook_type}: timeout_s must be a whole number of seconds from 1 to "
                f"{MAX_HOOK_TIMEOUT_S}."
            )
        entry: dict = (
            {"command": hook["command"]} if has_command else {"script": hook["script"]}
        )
        entry["timeout_s"] = timeout_s
        if hook_type == "pre_stop":
            on_failure = hook.get("on_failure", "abort")
            if on_failure not in _HOOK_ON_FAILURE_VALUES:
                raise engine.ConfigError(
                    f"pre_stop: on_failure must be one of {list(_HOOK_ON_FAILURE_VALUES)}."
                )
            entry["on_failure"] = on_failure
        normalized[hook_type] = entry
    return normalized


def _record_hook_event(
    target: Literal["instance", "group"], name: str,
    hook: Literal["pre_stop", "post_start", "config"],
    triggered_by: Literal["schedule", "manual", "api"],
    result: Literal["success", "failure", "skipped", "warning", "changed"], *,
    actor: str | None = None, exit_code: int | None = None, output_tail: str | None = None,
    detail: str | None = None,
) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO hook_events (target, instance_name, hook, triggered_by, actor,"
                " timestamp, result, exit_code, output_tail, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (target, name, hook, triggered_by, actor, _now(), result, exit_code,
                 output_tail, detail),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def get_hook_events(name: str, limit: int = 50) -> list[dict]:

    if limit < 0 or limit > _MAX_HISTORY_LIMIT:
        raise engine.ConfigError(f"limit must be between 0 and {_MAX_HISTORY_LIMIT}.")

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT hook, triggered_by, actor, timestamp, result, exit_code, output_tail,"
                " detail FROM hook_events WHERE target = 'instance' AND instance_name = ?"
                " ORDER BY id DESC LIMIT ?",
                (name, limit),
            ).fetchall()
        finally:
            conn.close()

    return [
        {"hook": r[0], "triggered_by": r[1], "actor": r[2], "timestamp": r[3], "result": r[4],
         "exit_code": r[5], "output_tail": r[6], "detail": r[7]}
        for r in _retry_db(_do)
    ]


def _read_hook_row(sql: str, key) -> dict | None:
    def _do():
        conn = _connect()
        try:
            return conn.execute(sql, (key,)).fetchone()
        finally:
            conn.close()

    row = _retry_db(_do)
    return None if row is None else json.loads(row[0])


def get_instance_hooks(name: str) -> dict | None:

    return _read_hook_row("SELECT config FROM instance_hooks WHERE instance_name = ?", name)


def get_group_hooks(group_name: str) -> dict | None:

    group = get_schedule_group(group_name)
    if group is None:
        raise GroupNotFoundError(f"no schedule group named '{group_name}'.")
    return _read_hook_row("SELECT config FROM group_hooks WHERE group_id = ?", group["id"])


def _hook_config_summary(config: dict | None) -> str:

    if not config or not any(config.get(t) for t in _HOOK_TYPES):
        return "hooks cleared"
    parts = []
    for hook_type in _HOOK_TYPES:
        hook = config.get(hook_type)
        if hook is None:
            continue
        extra = f", on_failure={hook['on_failure']}" if hook_type == "pre_stop" else ""
        parts.append(f"{hook_type}={_hook_what(hook)} (timeout {hook['timeout_s']}s{extra})")
    return "; ".join(parts)


def _hook_what(hook: dict) -> str:

    if hook.get("script") is not None:
        size = len(hook["script"].encode("utf-8"))
        return f"<uploaded script, {size} bytes, sha256 {_entry_digest(hook)}>"
    return repr(hook["command"])


def _entry_digest(hook: dict) -> str:

    return osb.hook_spec_digest({k: v for k, v in hook.items() if k != "source"})


def _hook_exec(host: str, ssh_key: str, hook: dict, timeout_s: int) -> engine.SshExecResult:
    if hook.get("script") is not None:
        return engine.ssh_exec(
            host, ssh_key, _HOOK_SCRIPT_RUNNER, timeout_s=timeout_s, stdin=hook["script"],
        )
    return engine.ssh_exec(host, ssh_key, hook["command"], timeout_s=timeout_s)


def set_instance_hooks(
    name: str, config: dict, *,
    triggered_by: Literal["schedule", "manual", "api"] = "manual", actor: str | None = None,
    client=None, on_warning: Callable[[str], None] | None = None,
) -> dict:

    normalized = _validate_hook_config(config)


    _store_hook_entries(normalized)
    with _instance_lock(name):
        registry = load_registry()
        if name not in registry:
            raise NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")
        _write_instance_hooks_row(name, normalized)
    _record_hook_event(
        "instance", name, "config", triggered_by, "changed", actor=actor,
        detail=_hook_config_summary(normalized),
    )
    osb.sync_object_storage_backup(name, _backup_payload(name, registry[name]))
    _sync_hook_tags(client, name, on_warning)
    return normalized


def _write_instance_hooks_row(name: str, normalized: dict) -> None:

    def _do():
        conn = _connect()
        try:
            if not any(normalized.values()):
                conn.execute("DELETE FROM instance_hooks WHERE instance_name = ?", (name,))
            else:
                conn.execute(
                    "INSERT INTO instance_hooks (instance_name, config) VALUES (?, ?)"
                    " ON CONFLICT(instance_name) DO UPDATE SET config = excluded.config",
                    (name, json.dumps(normalized)),
                )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _backup_payload(name: str, record: dict) -> dict:

    return {**record, "hooks": get_instance_hooks(name), "schedule": get_instance_schedule(name)}


def _restore_instance_hooks_from_backup(
    name: str, on_warning: Callable[[str], None] | None,
) -> None:

    if not osb.is_configured() or get_instance_hooks(name) is not None:
        return
    backup = osb.download_instance_backup(name)
    hooks = (backup or {}).get("hooks")
    if not hooks:
        return
    try:
        normalized = _validate_hook_config(hooks)
        _write_instance_hooks_row(name, normalized)
        _record_hook_event(
            "instance", name, "config", "manual", "changed",
            detail="restored from Object Storage backup by rebuild: "
            + _hook_config_summary(normalized),
        )
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"  WARNING: could not restore hooks for '{name}' from its Object Storage backup "
                f"({e}) -- re-enter them with `hooks-set`."
            )


def clear_instance_hooks(
    name: str, *, triggered_by: Literal["schedule", "manual", "api"] = "manual",
    actor: str | None = None, client=None, on_warning: Callable[[str], None] | None = None,
) -> bool:

    with _instance_lock(name):
        def _do():
            conn = _connect()
            try:
                cur = conn.execute("DELETE FROM instance_hooks WHERE instance_name = ?", (name,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

        cleared = _retry_db(_do)
        record = load_registry().get(name)
    if cleared:
        _record_hook_event(
            "instance", name, "config", triggered_by, "changed", actor=actor,
            detail="instance hooks cleared (group hooks, if any, now apply)",
        )
        if record is not None:
            osb.sync_object_storage_backup(name, _backup_payload(name, record))
        _sync_hook_tags(client, name, on_warning)
    return cleared


def set_group_hooks(
    group_name: str, config: dict, *,
    triggered_by: Literal["schedule", "manual", "api"] = "manual", actor: str | None = None,
    client=None, on_warning: Callable[[str], None] | None = None,
) -> dict:

    normalized = _validate_hook_config(config)
    group = get_schedule_group(group_name)
    if group is None:
        raise GroupNotFoundError(f"no schedule group named '{group_name}'.")
    _store_hook_entries(normalized)

    def _do():
        conn = _connect()
        try:
            if not any(normalized.values()):
                conn.execute("DELETE FROM group_hooks WHERE group_id = ?", (group["id"],))
            else:
                conn.execute(
                    "INSERT INTO group_hooks (group_id, config) VALUES (?, ?)"
                    " ON CONFLICT(group_id) DO UPDATE SET config = excluded.config",
                    (group["id"], json.dumps(normalized)),
                )
            conn.commit()
        finally:
            conn.close()

    try:
        _retry_db(_do)
    except sqlite3.IntegrityError as e:
        raise GroupNotFoundError(f"no schedule group named '{group_name}'.") from e
    _record_hook_event(
        "group", group_name, "config", triggered_by, "changed", actor=actor,
        detail=_hook_config_summary(normalized),
    )
    for member in group["members"]:
        _sync_hook_tags(client, member, on_warning)
    _sync_group_object_backup(group_name, on_warning=on_warning)
    return normalized


def clear_group_hooks(
    group_name: str, *, triggered_by: Literal["schedule", "manual", "api"] = "manual",
    actor: str | None = None, client=None, on_warning: Callable[[str], None] | None = None,
) -> bool:

    group = get_schedule_group(group_name)
    if group is None:
        raise GroupNotFoundError(f"no schedule group named '{group_name}'.")

    def _do():
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM group_hooks WHERE group_id = ?", (group["id"],))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    cleared = _retry_db(_do)
    if cleared:
        _record_hook_event(
            "group", group_name, "config", triggered_by, "changed", actor=actor,
            detail="group hooks cleared",
        )
        for member in group["members"]:
            _sync_hook_tags(client, member, on_warning)
        _sync_group_object_backup(group_name, on_warning=on_warning)
    return cleared


def resolve_effective_hooks(name: str, record: dict) -> dict:

    own = get_instance_hooks(name) or {}
    group_cfg: dict = {}
    group_id = record.get("group_id")
    if group_id is not None:
        group_cfg = _read_hook_row(
            "SELECT config FROM group_hooks WHERE group_id = ?", group_id
        ) or {}
    effective: dict = {}
    for hook_type in _HOOK_TYPES:
        if own.get(hook_type):
            effective[hook_type] = {**own[hook_type], "source": "instance"}
        elif group_cfg.get(hook_type):
            effective[hook_type] = {**group_cfg[hook_type], "source": "group"}
        else:
            effective[hook_type] = None
    return effective


def _output_tail(stdout: str, stderr: str) -> str | None:

    combined = (stdout or "") + (stderr or "")
    combined = combined.strip()
    if not combined:
        return None
    return combined[-HOOK_OUTPUT_TAIL_CHARS:]


@dataclass
class HookRunResult:

    hook_type: str
    ran: bool
    ok: bool
    summary: str
    exit_code: int | None = None
    output_tail: str | None = None
    attempts: int = 0
    on_failure: str | None = None


def _describe_exec_failure(result: engine.SshExecResult, timeout_s: int) -> str:
    if result.timed_out:
        return f"timed out after {timeout_s}s"
    if result.connection_error:
        return "could not connect over SSH to run it"
    return f"exited with code {result.exit_code}"


def run_pre_stop_hook(
    name: str, hook: dict | None, host: str, ssh_key: str, *,
    triggered_by: Literal["schedule", "manual", "api"], actor: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> HookRunResult:

    if hook is None:
        return HookRunResult("pre_stop", ran=False, ok=True, summary="no pre-stop hook")
    if on_progress is not None:
        on_progress(f"  Running pre-stop hook ({hook['source']}, timeout {hook['timeout_s']}s)...")
    result = _hook_exec(host, ssh_key, hook, hook["timeout_s"])
    tail = _output_tail(result.stdout, result.stderr)
    if result.ok:
        summary = "pre-stop hook succeeded"
        event_result: Literal["success", "failure", "warning"] = "success"
    else:
        summary = f"pre-stop hook {_describe_exec_failure(result, hook['timeout_s'])}"
        event_result = "failure" if hook["on_failure"] == "abort" else "warning"
    _best_effort_hook_event(
        name, "pre_stop", triggered_by, event_result, actor=actor,
        exit_code=result.exit_code, output_tail=tail,
        detail=summary + ("" if result.ok else f" (on_failure={hook['on_failure']})"),
        on_progress=on_progress,
    )
    return HookRunResult(
        "pre_stop", ran=True, ok=result.ok, summary=summary, exit_code=result.exit_code,
        output_tail=tail, attempts=1, on_failure=hook["on_failure"],
    )


def run_post_start_hook(
    name: str, hook: dict | None, host: str, ssh_key: str, *,
    triggered_by: Literal["schedule", "manual", "api"], actor: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> HookRunResult:

    if hook is None:
        return HookRunResult("post_start", ran=False, ok=True, summary="no post-start hook")
    timeout_s = hook["timeout_s"]
    if on_progress is not None:
        on_progress(
            f"  Running post-start check ({hook['source']}, up to {timeout_s}s, "
            f"retrying every {POST_START_HOOK_RETRY_INTERVAL_S}s)..."
        )
    deadline = monotonic() + timeout_s
    attempts = 0
    result: engine.SshExecResult | None = None
    while True:
        remaining = max(1, int(deadline - monotonic()))
        result = _hook_exec(host, ssh_key, hook, remaining)
        attempts += 1
        if result.ok:
            break
        if monotonic() + POST_START_HOOK_RETRY_INTERVAL_S >= deadline:
            break
        sleep(POST_START_HOOK_RETRY_INTERVAL_S)
    tail = _output_tail(result.stdout, result.stderr)
    if result.ok:
        summary = f"post-start check succeeded (attempt {attempts})"
    else:
        summary = (
            f"post-start check still failing after {attempts} attempt(s) over {timeout_s}s "
            f"(last attempt {_describe_exec_failure(result, remaining)})"
        )
    _best_effort_hook_event(
        name, "post_start", triggered_by, "success" if result.ok else "failure", actor=actor,
        exit_code=result.exit_code, output_tail=tail, detail=summary, on_progress=on_progress,
    )
    return HookRunResult(
        "post_start", ran=True, ok=result.ok, summary=summary, exit_code=result.exit_code,
        output_tail=tail, attempts=attempts,
    )


def _best_effort_hook_event(
    name: str, hook: Literal["pre_stop", "post_start"],
    triggered_by: Literal["schedule", "manual", "api"],
    result: Literal["success", "failure", "skipped", "warning"], *,
    actor: str | None, exit_code: int | None, output_tail: str | None, detail: str,
    on_progress: Callable[[str], None] | None,
) -> None:

    try:
        _record_hook_event(
            "instance", name, hook, triggered_by, result, actor=actor, exit_code=exit_code,
            output_tail=output_tail, detail=detail,
        )
    except Exception as e:
        if on_progress is not None:
            on_progress(f"  WARNING: could not record hook audit event for '{name}': {e}")


_HOOK_TAG_CODES = {"pre_stop": "ps", "post_start": "pa"}
_HOOK_SCOPES = ("hk", "hkg")
_MAX_TAG_LENGTH_FOR_HOOKS = 50


def _is_hook_tag(tag: str) -> bool:
    return any(tag.startswith(f"{scope}-{code}") for scope in _HOOK_SCOPES
               for code in _HOOK_TAG_CODES.values())


def _hook_option_tag_value(hook_type: str, entry: dict) -> str:
    if hook_type == "pre_stop":
        return f"{entry['timeout_s']}:{'a' if entry['on_failure'] == 'abort' else 'c'}"
    return str(entry["timeout_s"])


def _encode_hook_tags(config: dict | None, scope: str, *, stored: bool) -> tuple[list[str], list[str]]:

    tags: list[str] = []
    unrecoverable: list[str] = []
    for hook_type, code in _HOOK_TAG_CODES.items():
        entry = (config or {}).get(hook_type)
        if not entry:
            continue
        if stored:
            tags.append(f"{scope}-{code}:{osb.hook_spec_digest(entry)}")
            continue
        command_tag = f"{scope}-{code}-c:{entry.get('command')}"
        if entry.get("command") is not None and len(command_tag) <= _MAX_TAG_LENGTH_FOR_HOOKS:
            tags += [command_tag, f"{scope}-{code}-o:{_hook_option_tag_value(hook_type, entry)}"]
        else:
            unrecoverable.append(hook_type)
    return tags, unrecoverable


def _decode_hook_tags(tags: list[str], scope: str) -> dict:

    found: dict = {}
    for hook_type, code in _HOOK_TAG_CODES.items():
        ref = next((t.split(":", 1)[1] for t in tags if t.startswith(f"{scope}-{code}:")), None)
        if ref:
            found[hook_type] = ("ref", ref)
            continue
        command = next((t.split(":", 1)[1] for t in tags if t.startswith(f"{scope}-{code}-c:")), None)
        options = next((t.split(":", 1)[1] for t in tags if t.startswith(f"{scope}-{code}-o:")), None)
        if not command or options is None:
            continue
        parts = options.split(":")
        try:
            entry: dict = {"command": command, "timeout_s": int(parts[0])}
        except ValueError:
            continue
        if hook_type == "pre_stop":
            entry["on_failure"] = "continue" if parts[1:] == ["c"] else "abort"
        found[hook_type] = ("inline", entry)
    return found


def _store_hook_entries(config: dict | None) -> bool:

    if not osb.is_configured():
        return False
    for entry in (config or {}).values():
        if not entry:
            continue
        try:
            osb.upload_hook_spec({k: v for k, v in entry.items() if k != "source"})
        except Exception as e:
            raise engine.ConfigError(f"could not store the hook in Object Storage ({e})") from e
    return True


def _group_hooks_by_id(group_id: int | None) -> dict | None:
    if group_id is None:
        return None
    return _read_hook_row("SELECT config FROM group_hooks WHERE group_id = ?", group_id)


def _sync_hook_tags_locked(
    client, name: str, record: dict, on_warning: Callable[[str], None] | None,
) -> None:

    if not record.get("os_volume_id"):
        return
    desired: list[str] = []
    unrecoverable: list[str] = []
    for scope, config in (("hk", get_instance_hooks(name)), ("hkg", _group_hooks_by_id(record.get("group_id")))):
        if not config:
            continue
        try:
            stored = _store_hook_entries(config)
        except engine.ConfigError as e:
            stored = False
            if on_warning is not None:
                on_warning(f"  WARNING: {e} -- falling back to inline tags for '{name}'.")
        tags, missing = _encode_hook_tags(config, scope, stored=stored)
        desired += tags
        unrecoverable += [f"{'group ' if scope == 'hkg' else ''}{t.replace('_', '-')}" for t in missing]
    if unrecoverable and on_warning is not None:
        on_warning(
            f"  WARNING: {', '.join(unrecoverable)} hook(s) for '{name}' can't be recorded in "
            "Linode tags (an uploaded script, or a command over the tag length limit, with Object "
            "Storage not configured) -- they're kept locally and in `backup` snapshots, but "
            "`rebuild` alone won't restore them. Configure Object Storage to make them recoverable."
        )
    os_volume = engine.retry_transient(lambda: client.load(Volume, record["os_volume_id"]))
    current = list(os_volume.tags or [])
    new_tags = [t for t in current if not _is_hook_tag(t)] + desired
    if new_tags != current:
        os_volume.tags = new_tags
        os_volume.save()
        _verify_os_volume_tag_write(client, record["os_volume_id"], new_tags)


def _sync_hook_tags(client, name: str, on_warning: Callable[[str], None] | None = None) -> None:

    if client is None:
        return
    try:
        with _instance_lock(name):
            record = load_registry().get(name)
            if record is None:
                return
            _sync_hook_tags_locked(client, name, record, on_warning)
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"  WARNING: could not record hooks for '{name}' in its disaster-recovery tags "
                f"({e}) -- the hooks are in effect locally; re-run hooks-set to retry."
            )


def _restore_hooks_from_tags(
    client, name: str, record: dict, on_progress: Callable[[str], None] | None,
    on_warning: Callable[[str], None] | None,
) -> bool:

    os_volume = engine.retry_transient(lambda: client.load(Volume, record["os_volume_id"]))
    tags = list(os_volume.tags) if isinstance(os_volume.tags, list) else []
    had_instance_hooks = False
    for scope in _HOOK_SCOPES:
        decoded = _decode_hook_tags(tags, scope)
        if not decoded:
            continue
        if scope == "hk":
            had_instance_hooks = True
            if get_instance_hooks(name) is not None:
                continue
        else:
            if record.get("group_id") is None or _group_hooks_by_id(record["group_id"]) is not None:
                continue
        config: dict = {}
        for hook_type, (kind, value) in decoded.items():
            entry = value if kind == "inline" else osb.download_hook_spec(value)
            if entry is None:
                if on_warning is not None:
                    on_warning(
                        f"  WARNING: '{name}': the {hook_type.replace('_', '-')} hook its tags point "
                        f"at ({value}) couldn't be fetched from Object Storage or didn't match its "
                        "digest -- not restored. Set it again with hooks-set."
                    )
                continue
            config[hook_type] = entry
        if not config:
            continue
        try:
            normalized = _validate_hook_config(config)
        except engine.ConfigError as e:
            if on_warning is not None:
                on_warning(f"  WARNING: '{name}': restored hook config is invalid ({e}) -- skipped.")
            continue
        summary = _hook_config_summary(normalized)
        if scope == "hk":
            _write_instance_hooks_row(name, normalized)
            _record_hook_event("instance", name, "config", "manual", "changed",
                               detail="restored by rebuild from tags: " + summary)
        else:
            def _write_group(gid=record["group_id"], cfg=normalized):
                conn = _connect()
                try:
                    conn.execute(
                        "INSERT INTO group_hooks (group_id, config) VALUES (?, ?)"
                        " ON CONFLICT(group_id) DO UPDATE SET config = excluded.config",
                        (gid, json.dumps(cfg)),
                    )
                    conn.commit()
                finally:
                    conn.close()
            _retry_db(_write_group)
            group = _group_row_by_id(record["group_id"]) or {}
            _record_hook_event("group", group.get("name", str(record["group_id"])), "config",
                               "manual", "changed", detail="restored by rebuild from tags: " + summary)
        if on_progress is not None:
            target = "its own" if scope == "hk" else "its group's"
            on_progress(f"  restored {target} hooks for '{name}' from tags: {summary}")
    return had_instance_hooks


def _parse_schedule_rules_from_cli_args(args) -> list | None:

    if args.rules_json is not None:
        if args.days is not None or args.start_time is not None or args.stop_time is not None:
            print(
                "Configuration error: --rules-json can't be combined with --days/--start-time/"
                "--stop-time -- use one or the other.",
                file=sys.stderr,
            )
            return None
        try:
            parsed = json.loads(args.rules_json)
        except json.JSONDecodeError as e:
            print(f"Configuration error: --rules-json is not valid JSON: {e}", file=sys.stderr)
            return None
        if parsed is None:


            print(
                "Configuration error: --rules-json must be a JSON array of rule objects, not "
                "null.",
                file=sys.stderr,
            )
            return None
        return parsed
    if args.days is None or args.start_time is None or args.stop_time is None:
        print(
            "Configuration error: pass --days/--start-time/--stop-time together (one rule), "
            "or --rules-json (one or more rules).",
            file=sys.stderr,
        )
        return None
    return [{
        "days_of_week": [d.strip() for d in args.days.split(",") if d.strip()],
        "start_time": args.start_time, "stop_time": args.stop_time,
    }]


def cmd_schedule_set(client, args) -> int:
    rules = _parse_schedule_rules_from_cli_args(args)
    if rules is None:
        return 1

    try:
        save_instance_schedule(
            client, args.name, args.timezone, rules, enabled=not args.disabled,
            on_warning=_print_to_stderr,
        )
    except NotOnboardedError as e:
        print(f"{e}", file=sys.stderr)
        return 1


    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Schedule set for '{args.name}': {len(rules)} rule(s), "
          f"{'enabled' if not args.disabled else 'disabled'}.")
    return 0


def cmd_schedule_show(args) -> int:
    schedule = get_instance_schedule(args.name)
    if schedule is None:
        print(f"No schedule set for '{args.name}'.")
        return 0
    print(json.dumps(schedule, indent=2))
    return 0


def cmd_schedule_clear(client, args) -> int:
    try:
        cleared = clear_instance_schedule(client, args.name, on_warning=_print_to_stderr)
    except InstanceLockedError as e:


        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if not cleared:
        print(f"'{args.name}' has no schedule to clear.")
        return 0
    print(f"Schedule cleared for '{args.name}'.")
    return 0


MAX_GROUP_NAME_LENGTH = 41


_GROUP_NAME_TAG_PREFIX = "grp-name:"


_GROUP_DEP_TAG_PREFIX = "grp-dep:"


_GROUP_HOLIDAY_IGNORE_TAG = "grp-hol:ignore"


def _group_id_for_name(conn: sqlite3.Connection, group_name: str) -> int:

    row = conn.execute("SELECT id FROM schedule_groups WHERE name = ?", (group_name,)).fetchone()
    if row is None:
        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    return row[0]


def _group_row_by_id(group_id: int) -> dict | None:

    def _do():
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT name, timezone, rules, enabled, account_holidays FROM schedule_groups "
                "WHERE id = ?",
                (group_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "id": group_id, "name": row[0], "timezone": row[1], "rules": json.loads(row[2]),
                "enabled": bool(row[3]), "depends_on": _group_dependency_names(conn, group_id),
                "account_holidays": row[4] or "follow",
            }
        finally:
            conn.close()

    return _retry_db(_do)


def _group_name_by_id(conn: sqlite3.Connection, group_id: int | None) -> str | None:
    if group_id is None:
        return None
    row = conn.execute("SELECT name FROM schedule_groups WHERE id = ?", (group_id,)).fetchone()
    return row[0] if row else None


def _group_dependency_names(conn: sqlite3.Connection, group_id: int) -> list[str]:

    return [r[0] for r in conn.execute(
        "SELECT g.name FROM group_dependencies d JOIN schedule_groups g"
        " ON g.id = d.depends_on_group_id WHERE d.group_id = ? ORDER BY g.name", (group_id,)
    ).fetchall()]


def _group_dependent_names(conn: sqlite3.Connection, group_id: int) -> list[str]:

    return [r[0] for r in conn.execute(
        "SELECT g.name FROM group_dependencies d JOIN schedule_groups g"
        " ON g.id = d.group_id WHERE d.depends_on_group_id = ? ORDER BY g.name", (group_id,)
    ).fetchall()]


def _group_backup_payload(group_name: str) -> dict | None:

    group = get_schedule_group(group_name)
    if group is None:
        return None
    return {
        "name": group["name"], "timezone": group["timezone"], "rules": group["rules"],
        "enabled": group["enabled"], "depends_on": group["depends_on"],
        "account_holidays": group.get("account_holidays", "follow"),
        "hooks": _read_hook_row("SELECT config FROM group_hooks WHERE group_id = ?", group["id"]),
    }


def _sync_group_object_backup(
    group_name: str, *, on_warning: Callable[[str], None] | None = None
) -> None:

    if not osb.is_configured():
        return
    try:
        payload = _group_backup_payload(group_name)
        if payload is None:
            osb.delete_group_backup(group_name)
        else:
            osb.upload_group_backup(group_name, payload)
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"  WARNING: could not back up group '{group_name}' to Object Storage ({e}) -- "
                "the change is in effect locally; run `backup` (or change the group again) to "
                "retry."
            )


def normalize_dependency_list(depends_on) -> list[str]:

    if depends_on is None:
        return []
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    names = []
    for item in depends_on:
        if not isinstance(item, str) or not item.strip():
            raise engine.ConfigError("each dependency must be a group name.")
        names.append(item.strip())
    return sorted(set(names))


def set_group_dependencies(
    client, group_name: str, depends_on, on_warning: Callable[[str], None] | None = None,
) -> list[str]:

    targets = normalize_dependency_list(depends_on)

    def _do():
        conn = _connect()
        try:
            group_id = _group_id_for_name(conn, group_name)
            target_ids = {}
            for target in targets:
                target_id = _group_id_for_name(conn, target)
                if target_id == group_id:
                    raise engine.ConfigError(f"group '{group_name}' can't depend on itself.")
                target_ids[target] = target_id


            edges: dict[int, set[int]] = {}
            for gid, dep in conn.execute(
                "SELECT group_id, depends_on_group_id FROM group_dependencies"
            ).fetchall():
                if gid != group_id:
                    edges.setdefault(gid, set()).add(dep)
            for target, target_id in target_ids.items():
                stack, seen = [target_id], set()
                while stack:
                    node = stack.pop()
                    if node == group_id:
                        raise engine.ConfigError(
                            f"'{target}' already depends (directly or indirectly) on "
                            f"'{group_name}', so '{group_name}' can't depend on it -- that "
                            "would be a cycle."
                        )
                    if node in seen:
                        continue
                    seen.add(node)
                    stack.extend(edges.get(node, ()))
            conn.execute("DELETE FROM group_dependencies WHERE group_id = ?", (group_id,))
            conn.executemany(
                "INSERT INTO group_dependencies (group_id, depends_on_group_id) VALUES (?, ?)",
                [(group_id, tid) for tid in target_ids.values()],
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)
    group = get_schedule_group(group_name)
    if group is None:
        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    for member in group["members"]:
        _sync_group_membership_tags(client, member, group, on_warning=on_warning)
    _sync_group_object_backup(group_name, on_warning=on_warning)
    return group["depends_on"]


def set_group_dependency(
    client, group_name: str, depends_on: str | None,
    on_warning: Callable[[str], None] | None = None,
) -> None:

    set_group_dependencies(client, group_name, depends_on, on_warning=on_warning)


def add_group_dependency(
    client, group_name: str, depends_on: str, on_warning: Callable[[str], None] | None = None,
) -> list[str]:

    group = get_schedule_group(group_name)
    if group is None:
        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    return set_group_dependencies(
        client, group_name, [*group["depends_on"], depends_on], on_warning=on_warning
    )


def remove_group_dependency(
    client, group_name: str, depends_on: str, on_warning: Callable[[str], None] | None = None,
) -> list[str]:

    group = get_schedule_group(group_name)
    if group is None:
        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    if depends_on not in group["depends_on"]:
        raise engine.ConfigError(f"group '{group_name}' doesn't depend on '{depends_on}'.")
    return set_group_dependencies(
        client, group_name, [d for d in group["depends_on"] if d != depends_on],
        on_warning=on_warning,
    )


def create_schedule_group(
    group_name: str, timezone: str, *, on_warning: Callable[[str], None] | None = None,
) -> int:

    if not group_name or not group_name.strip():
        raise engine.ConfigError("a group name is required.")
    if len(group_name) > MAX_GROUP_NAME_LENGTH:
        raise engine.ConfigError(
            f"group name {group_name!r} is {len(group_name)} characters -- must be "
            f"{MAX_GROUP_NAME_LENGTH} or fewer, so its disaster-recovery tag "
            f"('{_GROUP_NAME_TAG_PREFIX}{group_name}') fits Linode's 50-character tag limit."
        )
    _validate_schedule_timezone(timezone)

    def _do():
        conn = _connect()
        try:
            try:
                cur = conn.execute(
                    "INSERT INTO schedule_groups (name, timezone, rules, enabled)"
                    " VALUES (?, ?, '[]', 1)",
                    (group_name, timezone),
                )
                conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError as e:
                raise engine.ConfigError(f"a group named '{group_name}' already exists.") from e
        finally:
            conn.close()

    group_id = _retry_db(_do)
    _sync_group_object_backup(group_name, on_warning=on_warning)
    return group_id


def get_schedule_group(group_name: str) -> dict | None:

    def _do():
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, name, timezone, rules, enabled, account_holidays FROM schedule_groups "
                "WHERE name = ?",
                (group_name,),
            ).fetchone()
            if row is None:
                return None
            members = [
                r[0] for r in conn.execute(
                    "SELECT name FROM instances WHERE group_id = ? ORDER BY name", (row[0],)
                ).fetchall()
            ]
            return {
                "id": row[0], "name": row[1], "timezone": row[2], "rules": json.loads(row[3]),
                "enabled": bool(row[4]), "members": members,
                "account_holidays": row[5] or "follow",
                "depends_on": _group_dependency_names(conn, row[0]),
                "dependents": _group_dependent_names(conn, row[0]),
            }
        finally:
            conn.close()

    return _retry_db(_do)


def list_schedule_groups() -> list[dict]:

    def _do():
        conn = _connect()
        try:
            groups = conn.execute(
                "SELECT id, name, timezone, rules, enabled, account_holidays FROM schedule_groups "
                "ORDER BY name"
            ).fetchall()
            names_by_id = {g[0]: g[1] for g in groups}
            deps: dict[int, list[str]] = {}
            for gid, dep in conn.execute(
                "SELECT group_id, depends_on_group_id FROM group_dependencies"
            ).fetchall():
                if dep in names_by_id:
                    deps.setdefault(gid, []).append(names_by_id[dep])
            counts = dict(conn.execute(
                "SELECT group_id, COUNT(*) FROM instances"
                " WHERE group_id IS NOT NULL GROUP BY group_id"
            ).fetchall())
            return [
                {
                    "id": gid, "name": name, "timezone": tz, "rules": json.loads(rules),
                    "enabled": bool(enabled), "member_count": counts.get(gid, 0),
                    "depends_on": sorted(deps.get(gid, [])),
                    "account_holidays": account_holidays or "follow",
                }
                for gid, name, tz, rules, enabled, account_holidays in groups
            ]
        finally:
            conn.close()

    return _retry_db(_do)


def _sync_group_membership_tags_locked(client, name: str, record: dict, group: dict | None) -> None:


    if group is not None:
        if record.get("group_id") != group.get("id"):
            return
    elif record.get("group_id") is not None:
        return
    os_volume = engine.retry_transient(
        lambda vol_id=record["os_volume_id"]: client.load(Volume, vol_id)
    )
    kept = [
        t for t in (os_volume.tags or [])
        if not t.startswith(_GROUP_NAME_TAG_PREFIX)
        and not t.startswith(_GROUP_DEP_TAG_PREFIX)
        and t != _GROUP_HOLIDAY_IGNORE_TAG
        and not _is_schedule_tag(t, prefix=_GROUP_SCHEDULE_TAG_PREFIX)
    ]
    new_tags = kept
    if group is not None:
        new_tags = new_tags + [f"{_GROUP_NAME_TAG_PREFIX}{group['name']}"] + \
            _encode_schedule_as_tags(group, prefix=_GROUP_SCHEDULE_TAG_PREFIX)
        new_tags += [f"{_GROUP_DEP_TAG_PREFIX}{dep}" for dep in group.get("depends_on") or []]
        if group.get("account_holidays") == "ignore":
            new_tags.append(_GROUP_HOLIDAY_IGNORE_TAG)
    if new_tags != (os_volume.tags or []):
        os_volume.tags = new_tags
        os_volume.save()
        _verify_os_volume_tag_write(client, record["os_volume_id"], new_tags)


def _sync_group_membership_tags(
    client, name: str, group: dict | None, *, on_warning: Callable[[str], None] | None = None
) -> None:

    try:


        with _instance_lock(name):


            registry = load_registry()
            record = registry.get(name)
            if record is None or not record.get("os_volume_id"):
                return
            _sync_group_membership_tags_locked(client, name, record, group)
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"  WARNING: could not sync group membership tags for '{name}' onto its "
                f"OS volume ({e}) -- group membership itself is saved locally and fully in "
                "effect, but won't be recoverable via `rebuild` if the local database is lost "
                "before this is retried (safe to retry: re-run group-add for this instance)."
            )


_SIMPLE_NETWORK_CONFIG_TAG_PREFIX = "net-"
_SSH_KEY_TAG_PREFIX = "sshkeys"


_MAX_TAG_LENGTH = 50


def _is_extra_recovery_tag(tag: str) -> bool:

    if tag.startswith(_SIMPLE_NETWORK_CONFIG_TAG_PREFIX):
        return True
    if tag.startswith(_SSH_KEY_TAG_PREFIX):
        suffix = tag.partition(":")[0][len(_SSH_KEY_TAG_PREFIX):]
        return suffix.isdigit()
    return False


def _encode_simple_network_config_as_tags(
    network_interface_model: str | None, network_config: list | None, network_helper_enabled: bool | None
) -> list[str]:

    if network_interface_model != "legacy_config" or network_helper_enabled is None:
        return []
    if not isinstance(network_config, list) or len(network_config) != 1:
        return []
    iface = network_config[0]
    if not isinstance(iface, dict) or set(iface.keys()) - {"purpose", "primary"}:
        return []
    if iface.get("purpose") != "public":
        return []
    primary = 1 if iface.get("primary") else 0
    return [
        f"{_SIMPLE_NETWORK_CONFIG_TAG_PREFIX}if:pub-{primary}",
        f"{_SIMPLE_NETWORK_CONFIG_TAG_PREFIX}nh:{1 if network_helper_enabled else 0}",
    ]


def _decode_simple_network_config_from_tags(tags: list[str] | None) -> dict | None:

    if_prefix, nh_prefix = f"{_SIMPLE_NETWORK_CONFIG_TAG_PREFIX}if:", f"{_SIMPLE_NETWORK_CONFIG_TAG_PREFIX}nh:"
    primary = None
    network_helper_enabled = None
    for tag in tags or []:
        if tag.startswith(if_prefix):
            value = tag[len(if_prefix):]
            if value == "pub-0":
                primary = False
            elif value == "pub-1":
                primary = True
        elif tag.startswith(nh_prefix):
            network_helper_enabled = tag[len(nh_prefix):] == "1"
    if primary is None or network_helper_enabled is None:
        return None
    return {
        "network_interface_model": "legacy_config",
        "network_config": [{"purpose": "public", "primary": primary}],
        "network_helper_enabled": network_helper_enabled,
    }


def _classify_authorized_keys(client, raw_keys: list[str]) -> tuple[list[int], list[str]]:

    if not raw_keys:
        return [], []
    try:
        registered = {
            k.ssh_key: k.id
            for k in engine.retry_transient(lambda: list(client.profile.ssh_keys()))
        }
    except Exception:
        return [], list(raw_keys)
    matched_ids: list[int] = []
    unmatched: list[str] = []
    for key in raw_keys:
        key_id = registered.get(key)
        if key_id is not None:
            matched_ids.append(key_id)
        else:
            unmatched.append(key)
    return matched_ids, unmatched


def _encode_ssh_key_ids_as_tags(key_ids: list[int]) -> list[str]:

    tags: list[str] = []
    current: list[str] = []
    index = 0
    for key_id in key_ids:
        candidate = [*current, str(key_id)]
        candidate_tag = f"{_SSH_KEY_TAG_PREFIX}{index}:{'-'.join(candidate)}"
        if len(candidate_tag) > _MAX_TAG_LENGTH and current:
            tags.append(f"{_SSH_KEY_TAG_PREFIX}{index}:{'-'.join(current)}")
            index += 1
            current = [str(key_id)]
        else:
            current = candidate
    if current:
        tags.append(f"{_SSH_KEY_TAG_PREFIX}{index}:{'-'.join(current)}")
    return tags


def _decode_ssh_key_ids_from_tags(tags: list[str] | None) -> list[int]:

    ids: list[int] = []
    for tag in tags or []:
        prefix_part, sep, rest = tag.partition(":")
        if not sep or not prefix_part.startswith(_SSH_KEY_TAG_PREFIX):
            continue
        if not prefix_part[len(_SSH_KEY_TAG_PREFIX):].isdigit():
            continue
        for id_str in rest.split("-"):
            if id_str.isdigit():
                ids.append(int(id_str))
    return ids


def _resolve_ssh_key_ids_to_content(client, key_ids: list[int]) -> list[str]:

    if not key_ids:
        return []
    try:
        registered = {
            k.id: k.ssh_key
            for k in engine.retry_transient(lambda: list(client.profile.ssh_keys()))
        }
    except Exception:
        return []
    return [registered[key_id] for key_id in key_ids if key_id in registered]


def _sync_extra_recovery_tags_locked(client, name: str, record: dict, ssh_key_ids: list[int]) -> None:

    os_volume = engine.retry_transient(
        lambda: client.load(Volume, record["os_volume_id"])
    )
    kept = [t for t in (os_volume.tags or []) if not _is_extra_recovery_tag(t)]
    new_extra = (
        _encode_simple_network_config_as_tags(
            record.get("network_interface_model"), record.get("network_config"),
            record.get("network_helper_enabled"),
        )
        + _encode_ssh_key_ids_as_tags(ssh_key_ids)
    )
    new_tags = kept + new_extra
    if new_tags != (os_volume.tags or []):
        os_volume.tags = new_tags
        os_volume.save()
        _verify_os_volume_tag_write(client, record["os_volume_id"], new_tags)


@dataclass
class BackupResult:

    object_storage_configured: bool = False
    instances_synced: list[str] = field(default_factory=list)
    instances_failed: list[str] = field(default_factory=list)
    groups_synced: list[str] = field(default_factory=list)
    groups_failed: list[str] = field(default_factory=list)
    tokens_synced: list[str] = field(default_factory=list)
    tokens_failed: list[str] = field(default_factory=list)
    known_hosts_key: str | None = None
    known_hosts_error: str | None = None
    object_storage_snapshot_key: str | None = None
    object_storage_snapshot_error: str | None = None
    local_snapshot_path: str | None = None
    local_snapshot_error: str | None = None
    ssh_key_problems: list[str] = field(default_factory=list)
    snapshots_pruned: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:

        return not (
            self.instances_failed
            or self.groups_failed
            or self.tokens_failed
            or self.known_hosts_error
            or self.object_storage_snapshot_error
            or self.local_snapshot_error
        )


def backup_full_system(
    *, local_dir: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
    trigger: str = "cli",
) -> BackupResult:

    result = BackupResult(object_storage_configured=osb.is_configured())
    other = another_scheduler_active() if result.object_storage_configured else None
    if other is not None:


        result.object_storage_configured = False
        result.ssh_key_problems.append(
            f"skipped Object Storage: another scheduler ({other.get('host')}) is active")
        if on_warning is not None:
            on_warning(f"  WARNING: another scheduler ({other.get('host')}) holds the lease -- "
                       "not uploading this host's database; local snapshot only.")


    key_result = (protect_deployment_ssh_key(None, deployment_ssh_key_path()) if other is None
                  else KeyProtectionResult())
    result.ssh_key_problems += key_result.problems
    env_result = (backup_env_encrypted(verify_remote=True) if other is None
                  else EnvBackupResult("not_configured"))
    if env_result.outcome == "failed":
        result.ssh_key_problems.append(f"encrypted settings backup: {env_result.detail}")
    if other is None and result.object_storage_configured:
        try:
            backup_release_copy(on_progress=on_progress)
        except Exception as e:
            result.ssh_key_problems.append(f"release copy: {e}")
    for problem in key_result.problems:
        if on_warning is not None:
            on_warning(f"  WARNING: deployment SSH key: {problem}")

    if result.object_storage_configured:
        registry = load_registry()
        if on_progress is not None:
            on_progress(f"Re-syncing {len(registry)} instance record(s) to Object Storage...")
        for name, record in registry.items():
            try:
                osb.upload_instance_backup(name, _backup_payload(name, record))
                result.instances_synced.append(name)
            except Exception as e:
                result.instances_failed.append(name)
                if on_warning is not None:
                    on_warning(f"  WARNING: could not back up '{name}' to Object Storage ({e})")

        groups = list_schedule_groups()
        if on_progress is not None:
            on_progress(f"Re-syncing {len(groups)} group record(s) to Object Storage...")
        for g in groups:
            try:
                payload = _group_backup_payload(g["name"])
                if payload is not None:
                    osb.upload_group_backup(g["name"], payload)
                    result.groups_synced.append(g["name"])
            except Exception as e:
                result.groups_failed.append(g["name"])
                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not back up group '{g['name']}' to Object Storage ({e})"
                    )

        tokens = list_api_tokens()
        if tokens and on_progress is not None:
            on_progress(f"Re-syncing {len(tokens)} API token record(s) to Object Storage...")
        for t in tokens:
            try:
                record = _token_record(t["name"])
                if record is not None:
                    osb.upload_token_record(t["name"], record)
                    result.tokens_synced.append(t["name"])
            except Exception as e:
                result.tokens_failed.append(t["name"])
                if on_warning is not None:
                    on_warning(
                        f"  WARNING: could not back up API token '{t['name']}' to Object Storage ({e})"
                    )

        for entry in list_migration_backups():
            _sync_migration_backup_record(entry["name"], on_warning)
        _sync_holidays(on_warning)
        if on_progress is not None:
            on_progress("Uploading a full database snapshot to Object Storage...")
        try:
            result.object_storage_snapshot_key = osb.upload_database_snapshot(REGISTRY_PATH)
        except Exception as e:
            result.object_storage_snapshot_error = str(e)
            if on_warning is not None:
                on_warning(f"  WARNING: could not upload full database snapshot ({e})")
        else:

            try:
                result.snapshots_pruned = osb.prune_database_snapshots(
                    keep_all_hours=_env_int("LIS_BACKUP_KEEP_ALL_HOURS", 48),
                    keep_daily_days=_env_int("LIS_BACKUP_KEEP_DAILY_DAYS", 30))
            except Exception as e:
                if on_warning is not None:
                    on_warning(f"  WARNING: could not prune old snapshots ({e})")


        hosts = known_hosts_path()
        if hosts.exists():
            try:
                result.known_hosts_key = osb.upload_deployment_file(
                    KNOWN_HOSTS_OBJECT_NAME, hosts.read_bytes()
                )
            except Exception as e:
                result.known_hosts_error = str(e)
                if on_warning is not None:
                    on_warning(f"  WARNING: could not back up the trusted host keys ({e})")
    elif on_warning is not None:
        on_warning(
            "  WARNING: Object Storage is not configured (LINODE_OBJ_STORAGE_* unset) -- "
            "skipping the per-instance Object Storage re-sync and snapshot upload. See "
            "docs/OPERATIONS.md to set this up."
        )

    if local_dir is not None:
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / f"instances-{int(time.time())}.db"
        if on_progress is not None:
            on_progress(f"Writing a full database snapshot to {local_path}...")
        try:
            osb.snapshot_database(REGISTRY_PATH, local_path)
            result.local_snapshot_path = str(local_path)
            hosts = known_hosts_path()
            if hosts.exists():
                hosts_copy = local_dir / f"{local_path.stem}.known_hosts"
                hosts_copy.write_bytes(hosts.read_bytes())
                hosts_copy.chmod(0o600)
        except Exception as e:
            result.local_snapshot_error = str(e)
            if on_warning is not None:
                on_warning(f"  WARNING: could not write local database snapshot ({e})")

    _record_system_backup_run(result, trigger)
    return result


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


CONTINUOUS_BACKUP_MIN_INTERVAL_S = 60


def continuous_backup_state() -> dict:
    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT changes, backed_up, last_backup_at, last_attempt_at, last_key, last_error "
                "FROM continuous_backup WHERE id = 1").fetchone()
        finally:
            conn.close()

    row = _retry_db(_do) or (0, 0, None, None, None, None)
    return {"enabled": osb.is_configured(), "pending_changes": row[0] - row[1],
            "last_backup_at": row[2], "last_attempt_at": row[3], "last_key": row[4],
            "last_error": row[5], "min_interval_seconds": CONTINUOUS_BACKUP_MIN_INTERVAL_S}


def _update_continuous_backup(**fields) -> None:
    def _do():
        conn = _connect()
        try:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE continuous_backup SET {sets} WHERE id = 1", tuple(fields.values()))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def run_continuous_backup_if_due(*, now: datetime | None = None,
                                 min_interval_s: float = CONTINUOUS_BACKUP_MIN_INTERVAL_S) -> str:

    if not osb.is_configured():
        return "not_configured"
    now = now or datetime.now(UTC)
    try:
        state = continuous_backup_state()
    except Exception:
        return "failed"
    if state["pending_changes"] <= 0:
        return "no_changes"
    last = state["last_attempt_at"] or state["last_backup_at"]
    if last is not None:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < min_interval_s:
                return "not_due"
        except ValueError:
            pass

    def _changes() -> int:
        conn = _connect()
        try:
            return conn.execute("SELECT changes FROM continuous_backup WHERE id = 1").fetchone()[0]
        finally:
            conn.close()

    try:
        changes = _retry_db(_changes)
        _update_continuous_backup(last_attempt_at=now.isoformat())
        key = osb.upload_database_snapshot(REGISTRY_PATH, key=osb.CONTINUOUS_SNAPSHOT_KEY)
        hosts = known_hosts_path()
        if hosts.exists():
            osb.upload_deployment_file(KNOWN_HOSTS_OBJECT_NAME, hosts.read_bytes())
    except Exception as e:
        with contextlib_suppress(Exception):
            _update_continuous_backup(last_error=str(e)[:500])
        return "failed"
    with contextlib_suppress(Exception):
        _update_continuous_backup(backed_up=changes, last_backup_at=datetime.now(UTC).isoformat(),
                                  last_key=key, last_error=None)
    return "uploaded"


HEARTBEAT_URL_ENV = "LIS_HEARTBEAT_URL"
HEARTBEAT_INTERVAL_SECONDS = 60

LEASE_OBJECT_NAME = "scheduler-lease.json"
LEASE_TTL_SECONDS = 180
LEASE_RENEW_SECONDS = 30
_LEASE_CONFIRM_DELAY_S = 3.0


def scheduler_id() -> str:

    path = REGISTRY_PATH.parent / "scheduler-id"
    try:
        value = path.read_text().strip()
        if value:
            return value
    except OSError:
        pass
    value = f"{socket.gethostname()}-{secrets.token_hex(6)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")
    return value


def read_lease() -> dict | None:
    raw = osb.download_deployment_file(LEASE_OBJECT_NAME)
    if raw is None:
        return None
    try:
        lease = json.loads(raw)
    except ValueError:
        return None
    return lease if isinstance(lease, dict) else None


def _lease_age_seconds(lease: dict, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(lease["renewed_at"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        return float("inf")


@dataclass
class LeaseStatus:
    active: bool
    role: str
    holder: dict | None = None
    detail: str | None = None


def _record_lease_state(status: LeaseStatus, now: datetime) -> None:
    def _do():
        conn = _connect()
        try:
            holder = status.holder or {}
            conn.execute(
                "INSERT INTO scheduler_lease_state (id, role, holder_host, holder_renewed_at, updated_at) "
                "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET role = excluded.role, "
                "holder_host = excluded.holder_host, holder_renewed_at = excluded.holder_renewed_at, "
                "updated_at = excluded.updated_at",
                (status.role, holder.get("host"), holder.get("renewed_at"), now.isoformat()))
            conn.commit()
        finally:
            conn.close()

    with contextlib_suppress(Exception):
        _retry_db(_do)


def acquire_or_renew_lease(*, now: datetime | None = None, take_over: bool = False,
                           confirm_delay_s: float = _LEASE_CONFIRM_DELAY_S) -> LeaseStatus:

    now = now or datetime.now(UTC)
    if not osb.is_configured():
        return LeaseStatus(True, "unfenced", detail="no Object Storage -- only one scheduler may run")
    me = scheduler_id()
    try:
        lease = read_lease()
    except Exception as e:
        return _lease_unreachable(now, take_over, f"couldn't read the lease ({e})")
    mine = lease is not None and lease.get("id") == me
    if lease is not None and not mine and _lease_age_seconds(lease, now) < LEASE_TTL_SECONDS and not take_over:
        status = LeaseStatus(False, "standby", holder=lease,
                             detail=f"{lease.get('host')} holds the scheduler lease")
        _record_lease_state(status, now)
        return status
    record = {"id": me, "host": socket.gethostname(), "renewed_at": now.isoformat(),
              "acquired_at": lease.get("acquired_at") if mine and lease else now.isoformat()}
    if lease is not None and not mine:
        record["took_over_from"] = lease.get("host")
    try:
        osb.upload_deployment_file(LEASE_OBJECT_NAME, json.dumps(record).encode())
        if not mine and confirm_delay_s > 0:
            time.sleep(confirm_delay_s)
            check = read_lease()
            if check is not None and check.get("id") != me:
                status = LeaseStatus(False, "standby", holder=check,
                                     detail=f"{check.get('host')} took the lease at the same moment")
                _record_lease_state(status, now)
                return status
    except Exception as e:
        return _lease_unreachable(now, take_over, f"couldn't write the lease ({e})")
    status = LeaseStatus(True, "active", holder=record)
    _record_lease_state(status, now)
    _mark_lease_held(now)
    if lease is not None and not mine:
        record_activity(
            f"this scheduler took the lease over from {lease.get('host')}"
            f"{' (explicit take-over)' if take_over else ' (its lease had expired)'}",
            level="warning", source="scheduler", action="lease")
    return status


def _lease_held_path() -> Path:
    return REGISTRY_PATH.parent / "scheduler-lease-held"


def _mark_lease_held(now: datetime) -> None:
    with contextlib_suppress(OSError):
        path = _lease_held_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(now.isoformat() + "\n")


def _last_held_lease(now: datetime) -> datetime | None:
    try:
        return datetime.fromisoformat(_lease_held_path().read_text().strip())
    except (OSError, ValueError):
        return None


def _lease_unreachable(now: datetime, take_over: bool, why: str) -> LeaseStatus:

    held = _last_held_lease(now)
    if take_over or (held is not None and (now - held).total_seconds() < 2 * LEASE_TTL_SECONDS):
        if take_over:
            _mark_lease_held(now)
        return LeaseStatus(True, "active", detail=f"{why}; continuing as the active scheduler")
    status = LeaseStatus(False, "standby", detail=f"{why}; this host wasn't the active scheduler, "
                         "so it waits (use `scheduler-lease --take-over` if the old one is gone)")
    _record_lease_state(status, now)
    return status


def release_lease() -> None:

    if not osb.is_configured():
        return
    with contextlib_suppress(Exception):
        lease = read_lease()
        if lease is not None and lease.get("id") == scheduler_id():
            lease["renewed_at"] = datetime(2000, 1, 1, tzinfo=UTC).isoformat()
            lease["released"] = True
            osb.upload_deployment_file(LEASE_OBJECT_NAME, json.dumps(lease).encode())


def lease_state() -> dict | None:
    def _do():
        conn = _connect()
        try:
            return conn.execute("SELECT role, holder_host, holder_renewed_at, updated_at FROM "
                                "scheduler_lease_state WHERE id = 1").fetchone()
        finally:
            conn.close()

    row = None
    with contextlib_suppress(Exception):
        row = _retry_db(_do)
    if row is None:
        return None
    return {"role": row[0], "holder_host": row[1], "holder_renewed_at": row[2], "updated_at": row[3]}


STANDBY_ALLOWED_COMMANDS = frozenset({
    "list", "status", "history", "schedule-show", "group-show", "group-list", "hooks-show",
    "holiday-list", "backup-list", "api-token-list", "api-token-scopes", "alerts-check",
    "alerts-test", "scheduler-lease", "backup-verify", "backup", "backup-config", "restore",
    "ssh-key-backup", "ssh-key-restore", "env-backup", "env-restore", "env-backup-keygen",
    "console-session-delete", "poll", "serve-api", "host-watchdog", "recovery-drill-check",
    "release-backup", "restore-item",
})


DRILL_ALLOWED_COMMANDS = frozenset({
    "list", "status", "history", "schedule-show", "group-show", "group-list", "hooks-show",
    "holiday-list", "backup-list", "api-token-list", "api-token-scopes", "restore",
    "ssh-key-restore", "env-restore", "recovery-drill-check", "scheduler-lease",
})


def standby_refusal(command: str, *, read_only: bool = False) -> str | None:

    if os.environ.get("LIS_RECOVERY_DRILL", "").strip() == "1" and not (
            read_only or command in DRILL_ALLOWED_COMMANDS):
        return ("this host is running a recovery drill: it only restores and checks, and never "
                "changes nodes, tags, the lease or the bucket.")
    if read_only or command in STANDBY_ALLOWED_COMMANDS:
        return None
    if os.environ.get("LIS_ALLOW_STANDBY_CHANGES", "").strip() == "1":
        return None
    lease = another_scheduler_active()
    if lease is None:
        return None
    return (f"this host is a standby: {lease.get('host')} is the active scheduler (lease renewed "
            f"{lease.get('renewed_at')}). Make changes there -- two hosts changing the same nodes "
            "can undo each other. If that host is gone, run `scheduler-lease --take-over` here first.")


def another_scheduler_active(now: datetime | None = None) -> dict | None:

    if not osb.is_configured():
        return None
    now = now or datetime.now(UTC)
    with contextlib_suppress(Exception):
        lease = read_lease()
        if (lease is not None and lease.get("id") != scheduler_id()
                and _lease_age_seconds(lease, now) < LEASE_TTL_SECONDS):
            return lease
    return None


BACKUP_VERIFY_INTERVAL_SECONDS = 24 * 3600
BACKUP_MAX_AGE_SECONDS = 26 * 3600


@dataclass
class BackupVerifyResult:
    ok: bool
    snapshot_key: str | None = None
    problems: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)


def verify_backups(*, now: datetime | None = None) -> BackupVerifyResult:

    now = now or datetime.now(UTC)
    result = BackupVerifyResult(ok=False)
    if not osb.is_configured():
        result.problems.append("Object Storage isn't configured -- nothing off-host to verify")
        _record_backup_verify(result, now)
        return result
    keys = osb.list_database_snapshots()
    if not keys:
        result.problems.append("no database snapshot found in Object Storage")
        _record_backup_verify(result, now)
        return result
    result.snapshot_key = keys[-1]
    live_names = set(load_registry())
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "verify.db"
        try:
            osb.download_database_snapshot(result.snapshot_key, dest)
        except osb.ObjectStorageError as e:
            result.problems.append(f"the newest snapshot can't be downloaded intact: {e}")
            _record_backup_verify(result, now)
            return result
        problem = osb.database_integrity_problem(dest)
        if problem:
            result.problems.append(f"the newest snapshot fails its integrity check: {problem}")
        else:
            result.checks.append("newest snapshot downloads, checksum matches, integrity check ok")
            conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
            try:
                snap_names = {r[0] for r in conn.execute("SELECT name FROM instances")}
                secret = conn.execute("SELECT blob FROM deployment_secrets WHERE name = ?",
                                      (_DEPLOYMENT_KEY_SECRET,)).fetchone() if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'deployment_secrets'").fetchone() else None
            finally:
                conn.close()
            stamp = result.snapshot_key.rsplit("instances-", 1)[-1].removesuffix(".db")
            if stamp.isdigit() and now.timestamp() - int(stamp) > BACKUP_MAX_AGE_SECONDS:
                result.problems.append(f"the newest snapshot is {int((now.timestamp() - int(stamp)) // 3600)} hours old")
            if result.snapshot_key == osb.CONTINUOUS_SNAPSHOT_KEY:
                missing = live_names - snap_names
                if missing and continuous_backup_state()["pending_changes"] == 0:
                    result.problems.append(
                        "the continuous snapshot is missing node(s): " + ", ".join(sorted(missing)))
            result.checks.append(f"snapshot holds {len(snap_names)} node(s) (live: {len(live_names)})")
            key_path = deployment_ssh_key_path()
            passphrase = os.environ.get(SSH_KEY_PASSPHRASE_ENV, "")
            if key_path.is_file() and passphrase:
                if secret is None:
                    result.problems.append("the snapshot holds no copy of the deployment SSH key")
                else:
                    try:
                        if decrypt_ssh_key(bytes(secret[0]), passphrase) != key_path.read_bytes():
                            result.problems.append("the snapshot's key copy isn't the current key")
                        else:
                            result.checks.append("the snapshot's key copy decrypts to the current key")
                    except engine.ConfigError as e:
                        result.problems.append(f"the snapshot's key copy doesn't decrypt: {e}")
                blob = None
                with contextlib_suppress(Exception):
                    blob = osb.download_deployment_file(SSH_KEY_OBJECT_NAME)
                if blob is None:
                    result.problems.append("no encrypted key copy in Object Storage")
                else:
                    try:
                        if decrypt_ssh_key(blob, passphrase) == key_path.read_bytes():
                            result.checks.append("the Object Storage key copy decrypts to the current key")
                        else:
                            result.problems.append("the Object Storage key copy isn't the current key")
                    except engine.ConfigError as e:
                        result.problems.append(f"the Object Storage key copy doesn't decrypt: {e}")
    if os.environ.get(ENV_BACKUP_RECIPIENTS_ENV, "").strip():
        blob = None
        with contextlib_suppress(Exception):
            blob = osb.download_deployment_file(ENV_BACKUP_OBJECT_NAME)
        if blob is None or not blob.startswith(b"age-encryption.org/v1"):
            result.problems.append("the encrypted settings copy (env.age) is missing or not age-encrypted")
        else:
            result.checks.append("the encrypted settings copy is present (only your age key opens it)")
    result.ok = not result.problems
    _record_backup_verify(result, now)
    return result


def _record_backup_verify(result: BackupVerifyResult, now: datetime) -> None:
    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO backup_verify_status (id, checked_at, ok, snapshot_key, problems) "
                "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET checked_at = excluded.checked_at, "
                "ok = excluded.ok, snapshot_key = excluded.snapshot_key, problems = excluded.problems",
                (now.isoformat(), 1 if result.ok else 0, result.snapshot_key, json.dumps(result.problems)))
            conn.commit()
        finally:
            conn.close()

    with contextlib_suppress(Exception):
        _retry_db(_do)


def backup_verify_status() -> dict | None:
    def _do():
        conn = _connect()
        try:
            return conn.execute("SELECT checked_at, ok, snapshot_key, problems FROM "
                                "backup_verify_status WHERE id = 1").fetchone()
        finally:
            conn.close()

    row = None
    with contextlib_suppress(Exception):
        row = _retry_db(_do)
    if row is None:
        return None
    return {"checked_at": row[0], "ok": bool(row[1]), "snapshot_key": row[2],
            "problems": json.loads(row[3] or "[]")}


def _backup_problems(result: BackupResult) -> list[str]:
    problems = []
    if result.instances_failed:
        problems.append(f"instance records failed: {', '.join(result.instances_failed)}")
    if result.groups_failed:
        problems.append(f"group records failed: {', '.join(result.groups_failed)}")
    if result.tokens_failed:
        problems.append(f"token records failed: {', '.join(result.tokens_failed)}")
    for label, err in (("host keys", result.known_hosts_error),
                       ("Object Storage snapshot", result.object_storage_snapshot_error),
                       ("local snapshot", result.local_snapshot_error)):
        if err:
            problems.append(f"{label}: {err}")
    if not result.object_storage_configured and not result.local_snapshot_path:
        problems.append("nowhere to back up to (Object Storage not configured, no local folder)")
    return problems


def _record_system_backup_run(result: BackupResult, trigger: str) -> None:

    problems = _backup_problems(result)

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO system_backup_status (id, finished_at, ok, trigger, "
                "object_storage_key, local_path, problems) VALUES (1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET finished_at = excluded.finished_at, ok = excluded.ok, "
                "trigger = excluded.trigger, object_storage_key = excluded.object_storage_key, "
                "local_path = excluded.local_path, problems = excluded.problems",
                (datetime.now(UTC).isoformat(), 0 if problems else 1, trigger,
                 result.object_storage_snapshot_key, result.local_snapshot_path,
                 json.dumps(problems)))
            conn.commit()
        finally:
            conn.close()

    with contextlib_suppress(Exception):
        _retry_db(_do)


ENV_PATH = engine.BASE_DIR / ".env"
_env_mtime: float | None = None


def default_backup_dir() -> Path | None:

    configured = os.environ.get("LINODE_SCHEDULER_BACKUP_DIR")
    if configured:
        return Path(configured)
    candidate = REGISTRY_PATH.parent.parent / "backups"
    return candidate if candidate.is_dir() else None


RUNTIME_RELOADABLE_KEYS = (
    "LIS_ALERT_WEBHOOK_URL", "LIS_ALERT_EMAIL_TO", "LIS_ALERT_SMTP_HOST", "LIS_ALERT_SMTP_PORT",
    "LIS_ALERT_SMTP_SECURITY", "LIS_ALERT_SMTP_USER", "LIS_ALERT_SMTP_PASSWORD",
    "LIS_ALERT_EMAIL_FROM", "LIS_ENV_BACKUP_RECIPIENTS", "LIS_HEARTBEAT_URL",
    "LIS_BACKUP_KEEP_ALL_HOURS", "LIS_BACKUP_KEEP_DAILY_DAYS", "LIS_DELETION_GRACE_DAYS",
    "LINODE_API_TOKEN", "LINODE_OAUTH_CLIENT_SECRET", "LINODE_SSH_PREVIOUS_KEY_PATH",
    "LIS_RECOVERY_DRILL_DAYS",
)


def reload_object_storage_settings_if_changed() -> bool:

    global _env_mtime
    try:
        mtime = ENV_PATH.stat().st_mtime
    except OSError:
        return False
    if _env_mtime is None:
        _env_mtime = mtime
        return False
    if mtime == _env_mtime:
        return False
    _env_mtime = mtime
    values = dotenv_values(ENV_PATH)
    old_token = os.environ.get("LINODE_API_TOKEN")
    for key in tuple(osb.SETTING_KEYS) + RUNTIME_RELOADABLE_KEYS:
        value = values.get(key)
        if value:
            os.environ[key] = value
        elif key == "LINODE_API_TOKEN":
            continue
        else:
            os.environ.pop(key, None)
    new_token = os.environ.get("LINODE_API_TOKEN")
    if new_token and new_token != old_token:
        engine.set_token_on_clients(new_token)
    osb.reset_client()
    return True


def _write_env_settings(updates: dict[str, str | None]) -> None:

    global _env_mtime
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    out, done = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            if updates[key] is not None and key not in done:
                out.append(f"{key}={updates[key]}")
                done.add(key)
            continue
        out.append(line)
    for key, value in updates.items():
        if value is not None and key not in done:
            out.append(f"{key}={value}")
    tmp = ENV_PATH.with_name(ENV_PATH.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV_PATH)
    for key, value in updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    osb.reset_client()
    _env_mtime = ENV_PATH.stat().st_mtime


def get_system_backup_status() -> dict:
    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT finished_at, ok, trigger, object_storage_key, local_path, problems "
                "FROM system_backup_status WHERE id = 1").fetchone()
        finally:
            conn.close()

    row = _retry_db(_do)
    last = None
    if row is not None:
        last = {"finished_at": row[0], "ok": bool(row[1]), "trigger": row[2],
                "object_storage_key": row[3], "local_path": row[4],
                "problems": json.loads(row[5] or "[]")}
    local = default_backup_dir()
    continuous = None
    with contextlib_suppress(Exception):
        continuous = continuous_backup_state()
    return {"object_storage": osb.settings_summary(),
            "local_backup_dir": str(local) if local else None,
            "last_backup": last, "continuous": continuous,
            "ssh_key": deployment_key_status(), "env_backup": env_backup_status(),
            "verify": backup_verify_status(), "lease": lease_state()}


def configure_system_backup(bucket: str, endpoint: str, access_key: str, secret_key: str, *,
                            skip_test: bool = False) -> None:

    values = {"bucket": bucket, "endpoint": endpoint, "access key": access_key,
              "secret key": secret_key}
    missing = [k for k, v in values.items() if not (v or "").strip()]
    if missing:
        raise engine.ConfigError(f"missing: {', '.join(missing)}")
    if not endpoint.startswith("https://"):
        raise engine.ConfigError("the endpoint must be an https:// URL, e.g. "
                                 "https://in-maa-1.linodeobjects.com")
    for value in values.values():
        if any(c in value for c in "\n\r"):
            raise engine.ConfigError("values must be single-line")
    if not skip_test:
        try:
            osb.test_connection(bucket.strip(), endpoint.strip(), access_key.strip(),
                                secret_key.strip())
        except osb.ObjectStorageError as e:
            raise engine.ConfigError(f"settings not saved -- {e}") from e
    _write_env_settings({"LINODE_OBJ_STORAGE_BUCKET": bucket.strip(),
                         "LINODE_OBJ_STORAGE_ENDPOINT": endpoint.strip(),
                         "LINODE_OBJ_STORAGE_ACCESS_KEY": access_key.strip(),
                         "LINODE_OBJ_STORAGE_SECRET_KEY": secret_key.strip()})


    with contextlib_suppress(Exception):
        osb.reset_client()
        osb.enable_bucket_protection()


def disable_system_backup_object_storage() -> None:
    _write_env_settings({key: None for key in osb.SETTING_KEYS})


def test_system_backup_settings() -> None:

    if not osb.is_configured():
        raise engine.ConfigError("Object Storage is not configured.")
    try:
        osb.test_connection(os.environ["LINODE_OBJ_STORAGE_BUCKET"],
                            os.environ["LINODE_OBJ_STORAGE_ENDPOINT"],
                            os.environ["LINODE_OBJ_STORAGE_ACCESS_KEY"],
                            os.environ["LINODE_OBJ_STORAGE_SECRET_KEY"])
    except osb.ObjectStorageError as e:
        raise engine.ConfigError(str(e)) from e


def cmd_backup_config(args) -> int:
    try:
        if args.disable:
            disable_system_backup_object_storage()
            print("Object Storage backups disabled (settings removed from .env).")
        elif args.bucket or args.endpoint or args.access_key or args.secret_key:
            access = args.access_key or getpass.getpass("Access key: ")
            secret = args.secret_key or getpass.getpass("Secret key: ")
            configure_system_backup(args.bucket or "", args.endpoint or "", access, secret)
            print("Saved. The connection test passed (a test object was written, read back and "
                  "deleted). The running scheduler and API pick this up within seconds.")
        if args.test:
            test_system_backup_settings()
            print("Connection test passed.")
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    status = get_system_backup_status()
    if args.json:
        print(json.dumps(status, indent=2))
        return 0
    obj = status["object_storage"]
    if obj["configured"]:
        print(f"Object Storage: configured -- bucket {obj['bucket']} at {obj['endpoint']} "
              f"(access key {obj['access_key_hint']})")
    else:
        print("Object Storage: NOT configured -- backups stay on this host only. Set it up with "
              "backup-config --bucket ... --endpoint ...")
    print(f"Local backup folder: {status['local_backup_dir'] or '(none)'}")
    last = status["last_backup"]
    if last is None:
        print("Last backup: never")
    else:
        print(f"Last backup: {last['finished_at']} ({last['trigger']}) -- "
              + ("OK" if last["ok"] else "PROBLEMS: " + "; ".join(last["problems"])))
    return 0


def cmd_backup(args) -> int:
    if not osb.is_configured() and not args.backup_dir:
        print(
            "Configuration error: nothing to back up to -- Object Storage isn't configured "
            "(LINODE_OBJ_STORAGE_* unset in .env) and no --backup-dir was given. Configure "
            "Object Storage (see docs/OPERATIONS.md) or pass --backup-dir.",
            file=sys.stderr,
        )
        return 1

    local_dir = Path(args.backup_dir) if args.backup_dir else None
    result = backup_full_system(
        local_dir=local_dir, on_progress=print, on_warning=_print_to_stderr,
    )

    if result.object_storage_configured:
        summary = f"Object Storage: {len(result.instances_synced)} instance record(s) re-synced"
        if result.instances_failed:
            summary += f", {len(result.instances_failed)} failed"
        summary += f"; {len(result.groups_synced)} group record(s) re-synced"
        if result.groups_failed:
            summary += f", {len(result.groups_failed)} failed"
        if result.tokens_synced or result.tokens_failed:
            summary += f"; {len(result.tokens_synced)} API token record(s) re-synced"
            if result.tokens_failed:
                summary += f", {len(result.tokens_failed)} failed"
        print(summary + ".")
        if result.object_storage_snapshot_key:
            print(
                f"Object Storage: full database snapshot uploaded as "
                f"'{result.object_storage_snapshot_key}'."
            )
        if result.known_hosts_key:
            print(f"Object Storage: trusted host keys saved as '{result.known_hosts_key}'.")
    if result.local_snapshot_path:
        print(f"Local snapshot written to {result.local_snapshot_path}.")

    return 0 if result.ok else 1


KNOWN_HOSTS_OBJECT_NAME = "known_hosts"
SSH_KEY_OBJECT_NAME = "ssh-key.enc"
SSH_PUBLIC_KEY_OBJECT_NAME = "ssh-key.pub"
_SSH_KEY_BLOB_MAGIC = b"LISK1"
MIN_SSH_KEY_PASSPHRASE_LENGTH = 12


def known_hosts_path() -> Path:

    return engine.BASE_DIR / "state" / "known_hosts"


def encrypt_ssh_key(private_key: bytes, passphrase: str) -> bytes:

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    if len(passphrase) < MIN_SSH_KEY_PASSPHRASE_LENGTH:
        raise engine.ConfigError(
            f"the passphrase must be at least {MIN_SSH_KEY_PASSPHRASE_LENGTH} characters."
        )
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())
    return _SSH_KEY_BLOB_MAGIC + salt + nonce + AESGCM(key).encrypt(nonce, private_key, None)


def decrypt_ssh_key(blob: bytes, passphrase: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    head = len(_SSH_KEY_BLOB_MAGIC)
    if not blob.startswith(_SSH_KEY_BLOB_MAGIC) or len(blob) < head + 16 + 12 + 16:
        raise engine.ConfigError("the stored SSH key backup isn't in a recognised format.")
    salt, nonce, ciphertext = blob[head:head + 16], blob[head + 16:head + 28], blob[head + 28:]
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as e:
        raise engine.ConfigError("wrong passphrase (or the backup was altered).") from e


def backup_ssh_key(key_path: Path, passphrase: str) -> str:

    if not osb.is_configured():
        raise engine.ConfigError("Object Storage isn't configured (LINODE_OBJ_STORAGE_* in .env).")
    if not key_path.is_file():
        raise engine.ConfigError(f"no SSH private key at {key_path}.")
    private_key = key_path.read_bytes()
    if b"PRIVATE KEY" not in private_key:
        raise engine.ConfigError(f"{key_path} doesn't look like an SSH private key.")
    key = osb.upload_deployment_file(SSH_KEY_OBJECT_NAME, encrypt_ssh_key(private_key, passphrase))
    pub = key_path.with_name(key_path.name + ".pub")
    if pub.is_file():
        osb.upload_deployment_file(SSH_PUBLIC_KEY_OBJECT_NAME, pub.read_bytes())
    return key


def restore_ssh_key(dest: Path, passphrase: str, *, force: bool = False) -> Path:

    if not osb.is_configured():
        raise engine.ConfigError("Object Storage isn't configured (LINODE_OBJ_STORAGE_* in .env).")
    blob = osb.download_deployment_file(SSH_KEY_OBJECT_NAME)
    if blob is None:
        raise engine.ConfigError(
            "no SSH key backup in Object Storage -- it's only there if `ssh-key-backup` was run on "
            "the old host. Copy the original private key over instead."
        )
    private_key = decrypt_ssh_key(blob, passphrase)
    if dest.exists() and dest.read_bytes() != private_key and not force:
        raise engine.ConfigError(f"{dest} already holds a different key -- pass --force to replace it.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(private_key)
    dest.chmod(0o600)
    pub = osb.download_deployment_file(SSH_PUBLIC_KEY_OBJECT_NAME)
    if pub is not None:
        dest.with_name(dest.name + ".pub").write_bytes(pub)
    return dest


SSH_KEY_PASSPHRASE_ENV = "LIS_SSH_KEY_PASSPHRASE"


def deployment_ssh_key_path() -> Path:
    return Path(os.environ.get("LINODE_SSH_KEY_PATH",
                               str(Path.home() / ".ssh" / "linode_spike_key")))
_DEPLOYMENT_KEY_SECRET = "ssh_key"
LINODE_SSH_KEY_LABEL_PREFIX = "linode-instance-scheduler"


PASSPHRASE_ESCROW_LABEL_PREFIX = "lis-recovery-"
_ESCROW_LINE = "# passphrase: "


def _escrow_label(fingerprint: str) -> str:
    return f"{PASSPHRASE_ESCROW_LABEL_PREFIX}{fingerprint[:16]}"


def _escrow_script(passphrase: str) -> str:
    return ("#!/bin/sh\n"
            "# Linode Instance Scheduler: recovery data for the deployment SSH key. Not a deploy\n"
            "# script -- don't run it, don't delete it (a replacement scheduler host reads it).\n"
            f"{_ESCROW_LINE}{passphrase}\n"
            "exit 0\n")


def _escrow_passphrase_from(script: str) -> str | None:
    for line in (script or "").splitlines():
        if line.startswith(_ESCROW_LINE):
            return line[len(_ESCROW_LINE):].strip() or None
    return None


def _escrowed_stackscripts(client) -> list:
    found = engine.retry_transient(lambda: list(client.linode.stackscripts(mine_only=True)))
    return [s for s in found if str(getattr(s, "label", "")).startswith(PASSPHRASE_ESCROW_LABEL_PREFIX)]


def escrow_passphrase(client, fingerprint: str, passphrase: str) -> int:

    label = _escrow_label(fingerprint)
    script = _escrow_script(passphrase)
    for ss in _escrowed_stackscripts(client):
        if ss.label != label:
            continue
        if _escrow_passphrase_from(ss.script) == passphrase and not ss.is_public:
            return ss.id
        engine.retry_transient(ss.delete)
    created = engine.retry_transient(lambda: client.linode.stackscript_create(
        label, script, ["linode/ubuntu24.04"],
        desc="Linode Instance Scheduler recovery data (keep private, don't delete)", public=False))
    return created.id


def fetch_escrowed_passphrase(client, blob: bytes | None = None) -> str | None:

    candidates = []
    for ss in _escrowed_stackscripts(client):
        value = _escrow_passphrase_from(getattr(ss, "script", ""))
        if value:
            candidates.append(value)
    if blob is not None:
        for value in candidates:
            with contextlib_suppress(engine.ConfigError):
                decrypt_ssh_key(blob, value)
                return value
        return None
    return candidates[0] if len(candidates) == 1 else None


@dataclass
class KeyProtectionResult:
    fingerprint: str | None = None
    stored_in_database: bool = False
    object_storage: str = "not_configured"
    linode_ssh_key_id: int | None = None
    passphrase_escrowed: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _public_key_text(key_path: Path, private_key: bytes) -> str | None:
    pub = key_path.with_name(key_path.name + ".pub")
    if pub.is_file():
        text = pub.read_text().strip()
        if text:
            return text
    try:
        from cryptography.hazmat.primitives import serialization

        key = serialization.load_ssh_private_key(private_key, password=None)
        return key.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    except Exception:
        return None


def _same_public_key(a: str, b: str) -> bool:
    return a.split()[:2] == b.split()[:2]


def _deployment_secret_row() -> tuple | None:
    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT blob, fingerprint, public_key, object_storage_fingerprint, "
                "linode_ssh_key_id, updated_at, escrow_stackscript_id FROM deployment_secrets "
                "WHERE name = ?",
                (_DEPLOYMENT_KEY_SECRET,)).fetchone()
        finally:
            conn.close()

    return _retry_db(_do)


def _save_deployment_secret(**fields) -> None:
    fields = {**fields, "updated_at": datetime.now(UTC).isoformat()}

    def _do():
        conn = _connect()
        try:
            cols = ", ".join(fields)
            marks = ", ".join("?" for _ in fields)
            updates = ", ".join(f"{k} = excluded.{k}" for k in fields)
            conn.execute(
                f"INSERT INTO deployment_secrets (name, {cols}) VALUES (?, {marks}) "
                f"ON CONFLICT(name) DO UPDATE SET {updates}",
                (_DEPLOYMENT_KEY_SECRET, *fields.values()))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def deployment_key_status() -> dict:

    try:
        row = _deployment_secret_row()
    except Exception:
        row = None
    return {
        "passphrase_set": bool(os.environ.get(SSH_KEY_PASSPHRASE_ENV)),
        "stored_in_database": row is not None,
        "fingerprint": row[1] if row else None,
        "in_object_storage": bool(row and row[3] and row[3] == row[1]),
        "linode_ssh_key_id": row[4] if row else None,
        "updated_at": row[5] if row else None,
        "passphrase_escrowed": bool(row and row[6]),
    }


APP_DIR = Path(__file__).resolve().parent
RELEASE_OBJECT_LATEST = "release/latest.tar.gz"
_RELEASE_EXCLUDE = {".venv", "state", "keys", "backups", ".env", "node_modules", "__pycache__",
                    ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "test-results", "wheels"}


def release_version() -> str:
    try:
        value = (APP_DIR / "VERSION").read_text().strip()
        return value or "unknown"
    except OSError:
        return "unknown"


def _release_files() -> list[Path]:
    files = []
    for path in sorted(APP_DIR.rglob("*")):
        rel = path.relative_to(APP_DIR)
        if any(part in _RELEASE_EXCLUDE for part in rel.parts) or not path.is_file():
            continue
        if rel.name.startswith("test_") or rel.name == "conftest.py":
            continue
        files.append(path)
    return files


def _release_fingerprint(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(APP_DIR)).encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


RELEASE_WHEEL_PYTHONS = ("3.10", "3.11", "3.12", "3.13", "3.14")
_MANYLINUX_X86_64 = ("manylinux_2_17_x86_64", "manylinux2014_x86_64", "manylinux_2_28_x86_64",
                     "manylinux_2_34_x86_64")


def _lock_blocks(lock: Path) -> list[tuple[str, str, str]]:

    entries: list[tuple[str, str, str]] = []
    current: list[str] = []

    def _close() -> None:
        if not current:
            return
        head, _, marker = current[0].rstrip(" \\").partition(";")
        name = re.sub(r"[-_.]+", "-", head.split("==")[0].strip()).lower()
        block = "\n".join([head.strip() + " \\", *current[1:]]).rstrip(" \\")
        entries.append((name, marker.strip(), block))

    for line in lock.read_text().splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            _close()
            current = [line]
        elif current and line.strip().startswith("--hash"):
            current.append(line)
    _close()
    return entries


def _marker_applies(marker: str, python_version: str) -> bool:

    if not marker:
        return True
    try:
        from packaging.markers import Marker
    except ImportError:
        from pip._vendor.packaging.markers import Marker
    env = {"python_version": python_version, "python_full_version": python_version + ".0",
           "implementation_name": "cpython", "platform_python_implementation": "CPython",
           "sys_platform": "linux", "platform_system": "Linux", "platform_machine": "x86_64",
           "os_name": "posix"}
    return bool(Marker(marker).evaluate(env))


def _download_wheels_for_other_pythons(lock: Path, wheel_dir: Path) -> list[str]:

    entries = _lock_blocks(lock)
    here = f"{sys.version_info.major}.{sys.version_info.minor}"
    missed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        def _pip(blocks: list[str], extra: list[str]) -> bool:
            req_file = Path(tmp) / "req.txt"
            req_file.write_text("\n".join(blocks) + "\n")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "download", "--quiet", "--require-hashes",
                 "--no-deps", "-r", str(req_file), "-d", str(wheel_dir), *extra],
                capture_output=True, text=True, timeout=1800, check=False)
            return result.returncode == 0

        binary = ["--only-binary=:all:", "--implementation", "cp"]
        for platform in _MANYLINUX_X86_64:
            binary += ["--platform", platform]
        for version in RELEASE_WHEEL_PYTHONS:
            if version == here:
                continue
            blocks = [block for _, marker, block in entries if _marker_applies(marker, version)]
            if not blocks or _pip(blocks, [*binary, "--python-version", version]):
                continue


            ok = True
            for block in blocks:
                if not (_pip([block], [*binary, "--python-version", version])
                        or _pip([block], ["--no-binary=:all:"])):
                    ok = False
            if not ok:
                missed.append(version)
    return missed


def build_release_archive(dest: Path, *, with_wheels: bool = True) -> dict:

    import tarfile
    files = _release_files()
    with tempfile.TemporaryDirectory() as tmp:
        wheel_dir = Path(tmp) / "wheels"
        wheels: list[Path] = []
        lock = APP_DIR / "requirements.lock"
        if with_wheels and lock.is_file():
            result = subprocess.run(
                [sys.executable, "-m", "pip", "download", "--quiet", "--require-hashes",
                 "-r", str(lock), "-d", str(wheel_dir)],
                capture_output=True, text=True, timeout=900, check=False)
            if result.returncode != 0:
                raise engine.ConfigError(f"couldn't download the dependency wheels: "
                                         f"{result.stderr.strip()[-400:]}")
            _download_wheels_for_other_pythons(lock, wheel_dir)
            if any(not w.name.endswith(".whl") for w in wheel_dir.iterdir()):


                tools = subprocess.run(
                    [sys.executable, "-m", "pip", "download", "--quiet", "--only-binary=:all:",
                     "setuptools", "wheel", "-d", str(wheel_dir)],
                    capture_output=True, text=True, timeout=600, check=False)
                if tools.returncode != 0:
                    raise engine.ConfigError(f"couldn't download the build tools for the release "
                                             f"copy: {tools.stderr.strip()[-400:]}")
            wheels = sorted(wheel_dir.iterdir())
        with tarfile.open(dest, "w:gz") as tar:
            for path in files:
                tar.add(path, arcname=f"linode-instance-scheduler/{path.relative_to(APP_DIR)}")
            for wheel in wheels:
                tar.add(wheel, arcname=f"linode-instance-scheduler/wheels/{wheel.name}")
    return {"version": release_version(), "files": len(files), "wheels": len(wheels),
            "bytes": dest.stat().st_size, "fingerprint": _release_fingerprint(files)}


def _release_state_path() -> Path:
    return REGISTRY_PATH.parent / "release-backup.json"


def release_backup_status() -> dict:
    try:
        return json.loads(_release_state_path().read_text())
    except (OSError, ValueError):
        return {}


def backup_release_copy(*, force: bool = False,
                        on_progress: Callable[[str], None] | None = None) -> dict | None:

    if not osb.is_configured():
        return None
    fingerprint = _release_fingerprint(_release_files())
    state = release_backup_status()

    if (not force and state.get("fingerprint") == fingerprint and not state.get("last_error")
            and osb.deployment_file_exists(RELEASE_OBJECT_LATEST) is not False):
        return None
    now = datetime.now(UTC).isoformat()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "release.tar.gz"
            info = build_release_archive(archive)
            data = archive.read_bytes()
            version = re.sub(r"[^A-Za-z0-9._-]", "_", info["version"])
            osb.upload_deployment_file(f"release/{version}.tar.gz", data)
            osb.upload_deployment_file(RELEASE_OBJECT_LATEST, data)
    except Exception as e:
        state.update(last_error=str(e)[:500], last_attempt_at=now)
        _release_state_path().parent.mkdir(parents=True, exist_ok=True)
        _release_state_path().write_text(json.dumps(state))
        raise
    state = {**info, "fingerprint": fingerprint, "uploaded_at": now, "last_error": None}
    _release_state_path().parent.mkdir(parents=True, exist_ok=True)
    _release_state_path().write_text(json.dumps(state))
    record_activity(f"release copy {info['version']} saved to Object Storage "
                    f"({info['bytes'] // 1024} KiB, {info['wheels']} dependency wheels)",
                    source="system", action="release-backup")
    if on_progress is not None:
        on_progress(f"  release copy {info['version']} uploaded ({info['bytes'] // 1024} KiB, "
                    f"{info['wheels']} wheels)")
    return state


def protect_deployment_ssh_key(
    client, key_path: Path, *, passphrase: str | None = None,
    hostname: str | None = None,
) -> KeyProtectionResult:

    result = KeyProtectionResult()
    passphrase = passphrase or os.environ.get(SSH_KEY_PASSPHRASE_ENV) or ""
    if not key_path.is_file():
        result.problems.append(f"no deployment SSH key at {key_path}")
        return result
    private_key = key_path.read_bytes()
    result.fingerprint = hashlib.sha256(private_key).hexdigest()
    public_key = _public_key_text(key_path, private_key)
    row = None
    try:
        row = _deployment_secret_row()
    except Exception as e:
        result.problems.append(f"could not read the database: {e}")


    blob = row[0] if row else None
    if not passphrase:
        result.problems.append(
            f"{SSH_KEY_PASSPHRASE_ENV} isn't set in .env, so the key can't be stored encrypted "
            "(re-run install.sh to generate one)")
    elif len(passphrase) < MIN_SSH_KEY_PASSPHRASE_LENGTH:
        result.problems.append(
            f"{SSH_KEY_PASSPHRASE_ENV} must be at least {MIN_SSH_KEY_PASSPHRASE_LENGTH} characters")
    else:
        current = False
        if row is not None and row[1] == result.fingerprint:
            try:
                current = decrypt_ssh_key(bytes(row[0]), passphrase) == private_key
            except engine.ConfigError:
                current = False
        if not current:
            blob = encrypt_ssh_key(private_key, passphrase)
            try:
                _save_deployment_secret(blob=blob, fingerprint=result.fingerprint,
                                        public_key=public_key, object_storage_fingerprint=None)
                row = _deployment_secret_row()
            except Exception as e:
                result.problems.append(f"could not store the key in the database: {e}")
        result.stored_in_database = row is not None and row[1] == result.fingerprint


    if osb.is_configured() and blob is not None and result.stored_in_database:
        try:
            uploaded = row is not None and row[3] == result.fingerprint
            if uploaded and osb.download_deployment_file(SSH_KEY_OBJECT_NAME) is None:
                uploaded = False
            if uploaded:
                result.object_storage = "already_current"
            else:
                osb.upload_deployment_file(SSH_KEY_OBJECT_NAME, bytes(blob))
                if public_key:
                    osb.upload_deployment_file(SSH_PUBLIC_KEY_OBJECT_NAME, (public_key + "\n").encode())
                _save_deployment_secret(blob=bytes(blob), fingerprint=result.fingerprint,
                                        public_key=public_key,
                                        object_storage_fingerprint=result.fingerprint)
                result.object_storage = "uploaded"
        except Exception as e:
            result.object_storage = "failed"
            result.problems.append(f"could not store the key in Object Storage: {e}")


    if client is not None and public_key:
        try:
            existing = [k for k in engine.retry_transient(lambda: list(client.profile.ssh_keys()))
                        if _same_public_key(k.ssh_key or "", public_key)]
            if existing:
                result.linode_ssh_key_id = existing[0].id
            else:
                label = f"{LINODE_SSH_KEY_LABEL_PREFIX}-{hostname or socket.gethostname()}"[:64]
                key = engine.retry_transient(lambda: client.profile.ssh_key_upload(public_key, label))
                result.linode_ssh_key_id = key.id
            if row is not None and row[4] != result.linode_ssh_key_id:
                with contextlib_suppress(Exception):
                    def _do():
                        conn = _connect()
                        try:
                            conn.execute("UPDATE deployment_secrets SET linode_ssh_key_id = ? "
                                         "WHERE name = ?", (result.linode_ssh_key_id,
                                                            _DEPLOYMENT_KEY_SECRET))
                            conn.commit()
                        finally:
                            conn.close()
                    _retry_db(_do)
        except Exception as e:
            result.problems.append(f"could not register the public key with Linode: {e}")
    elif client is not None:
        result.problems.append("couldn't read the public key (needs the .pub file next to the key)")


    if client is not None and passphrase and result.stored_in_database:
        try:
            escrow_id = escrow_passphrase(client, result.fingerprint, passphrase)
            result.passphrase_escrowed = True
            with contextlib_suppress(Exception):
                def _save_escrow():
                    conn = _connect()
                    try:
                        conn.execute("UPDATE deployment_secrets SET escrow_stackscript_id = ? "
                                     "WHERE name = ?", (escrow_id, _DEPLOYMENT_KEY_SECRET))
                        conn.commit()
                    finally:
                        conn.close()
                _retry_db(_save_escrow)
        except Exception as e:
            result.problems.append(
                f"could not keep the passphrase's recovery copy in the Linode account (a private "
                f"StackScript; the API token needs StackScripts read/write): {e}")
    return result


RECOVERY_DRILL_TAG = "lis-recovery-drill"
RECOVERY_DRILL_OBJECT_NAME = "recovery-drill.json"


def recovery_drill_check(client) -> dict:

    report: dict = {"checked_at": datetime.now(UTC).isoformat(), "version": release_version(),
                    "ok": True, "checks": [], "problems": []}

    def ok(msg: str) -> None:
        report["checks"].append(msg)

    def bad(msg: str) -> None:
        report["problems"].append(msg)
        report["ok"] = False

    registry = load_registry()
    groups = list_schedule_groups()
    ok(f"database restored: {len(registry)} node(s), {len(groups)} group(s)")
    key_path = deployment_ssh_key_path()
    row = None
    with contextlib_suppress(Exception):
        row = _deployment_secret_row()
    if not key_path.is_file():
        bad(f"no deployment SSH key at {key_path}")
    else:
        public = _public_key_text(key_path, key_path.read_bytes())
        if row is not None and row[2] and public and not _same_public_key(row[2], public):
            bad("the restored SSH key isn't the one the database records")
        else:
            ok("deployment SSH key restored and matches the database")
    try:
        engine.auth_check(client)
        ok("Linode API token works")
    except Exception as e:
        bad(f"Linode API token: {e}")
        return report
    reachable = 0
    for name, record in registry.items():
        for vid in [record.get("os_volume_id")] + [d["volume_id"] for d in record.get("data_volumes") or []]:
            if not vid:
                continue
            try:
                engine.retry_transient(lambda vid=vid: client.load(Volume, vid))
            except Exception as e:
                bad(f"{name}: volume {vid}: {e}")
        if record.get("reserved_ip"):
            try:
                engine.retry_transient(lambda r=record: client.load(engine.ReservedIPAddress, r["reserved_ip"]))
            except Exception as e:
                bad(f"{name}: reserved IP {record['reserved_ip']}: {e}")
        if record.get("current_status") == "running":
            try:
                engine.ssh_run(_ssh_target(record, name), str(key_path), "true", retries=1,
                               timeout_s=15)
                reachable += 1
            except Exception as e:
                bad(f"{name}: running but not reachable with the restored key and host keys: "
                    f"{str(e)[:200]}")
    ok(f"every node's volumes and reserved IP exist; {reachable} running node(s) reachable over SSH "
       "with the restored key")
    return report


def cmd_recovery_drill_check(client, args) -> int:
    report = recovery_drill_check(client)
    text = json.dumps(report, indent=2)
    if args.report:
        Path(args.report).write_text(text)
    print(text)
    return 0 if report["ok"] else 1


def _drill_state_path() -> Path:
    return REGISTRY_PATH.parent / "recovery-drill.json"


def recovery_drill_status() -> dict:
    try:
        return json.loads(_drill_state_path().read_text())
    except (OSError, ValueError):
        return {}


def drill_interval_days() -> int:
    raw = os.environ.get("LIS_RECOVERY_DRILL_DAYS", "").strip()
    try:
        return max(0, int(raw)) if raw else 0
    except ValueError:
        return 0


def _record_drill(result: dict) -> None:
    _drill_state_path().parent.mkdir(parents=True, exist_ok=True)
    _drill_state_path().write_text(json.dumps(result))
    with contextlib_suppress(Exception):
        if osb.is_configured():
            osb.upload_deployment_file(RECOVERY_DRILL_OBJECT_NAME, json.dumps(result).encode())


def _scp(key_path: Path, src: Path, host: str, dest: str) -> None:
    subprocess.run(["scp", "-q", *engine.ssh_identity_args(key_path), "-o", "BatchMode=yes",
                    "-o", f"UserKnownHostsFile={engine.BASE_DIR / 'state' / 'known_hosts'}",
                    "-o", "StrictHostKeyChecking=yes", str(src), f"root@{host}:{dest}"],
                   check=True, capture_output=True, timeout=600)


def run_recovery_drill(client, *, region: str | None = None, keep_vm: bool = False,
                       on_progress: Callable[[str], None] | None = None) -> dict:

    say = on_progress or (lambda _m: None)
    started = datetime.now(UTC)
    result: dict = {"started_at": started.isoformat(), "ok": False, "problems": []}
    if not osb.is_configured():
        raise engine.ConfigError("a recovery drill needs Object Storage (the snapshot and the "
                                 "release copy come from the bucket).")
    key_path = deployment_ssh_key_path()
    public = _public_key_text(key_path, key_path.read_bytes()) if key_path.is_file() else None
    if not public:
        raise engine.ConfigError(f"no deployment SSH key at {key_path}.")

    for inst in engine.retry_transient(lambda: list(client.linode.instances())):
        if RECOVERY_DRILL_TAG in (inst.tags or []):
            with contextlib_suppress(Exception):
                engine.retry_transient(inst.delete)
    backup_release_copy()
    if region is None:
        host = None
        with contextlib_suppress(Exception):
            host = scheduler_instance(client)
        region = (str(host.region.id) if host is not None and host.region else None) or next(
            (r.get("region") for r in load_registry().values() if r.get("region")), None)
    if not region:
        raise engine.ConfigError("couldn't tell which region to use -- pass --region.")
    instance = None
    drill_ips: set[str] = set()
    try:
        label = f"lis-drill-{started:%Y%m%d%H%M}"
        say(f"Creating a throwaway VM ({label}, {region})")
        created = engine.retry_transient(lambda: client.linode.instance_create(
            "g6-nanode-1", region, image="linode/ubuntu24.04", label=label,
            root_pass=secrets.token_urlsafe(24), authorized_keys=[public],
            tags=[RECOVERY_DRILL_TAG]))


        instance = created[0] if isinstance(created, tuple) else created
        result["vm_id"] = instance.id
        engine.poll_until_status(lambda: client.load(Instance, instance.id), ("running",),
                                 timeout_s=600)
        ip = client.load(Instance, instance.id).ipv4[0]
        drill_ips.add(ip)
        engine.ssh_run(ip, str(key_path), "cloud-init status --wait >/dev/null 2>&1; true",
                       trust_new=True, retries=30, timeout_s=600)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "release.tar.gz"
            data = osb.download_deployment_file(RELEASE_OBJECT_LATEST)
            if data is None:
                raise engine.ConfigError("no release copy in the bucket")
            archive.write_bytes(data)
            env = Path(tmp) / "drill.env"
            lines = [f"{k}={os.environ[k]}" for k in
                     ("LINODE_API_TOKEN", *osb.SETTING_KEYS, SSH_KEY_PASSPHRASE_ENV)
                     if os.environ.get(k)]
            env.write_text("\n".join(lines + ["LIS_RECOVERY_DRILL=1"]) + "\n")
            env.chmod(0o600)
            say("Copying the release copy from the bucket to the VM")
            _scp(key_path, archive, ip, "/root/release.tar.gz")
            _scp(key_path, env, ip, "/root/drill.env")
        say("Running install.sh recover --drill on the VM (about 5-10 minutes)")
        out = engine.ssh_run(
            ip, str(key_path),
            "set -e; chmod 600 /root/drill.env; cd /root && tar xzf release.tar.gz && "
            "cd linode-instance-scheduler && ./install.sh recover --drill --env-file /root/drill.env "
            "--no-api > /root/drill.log 2>&1; rc=$?; cat /tmp/recovery-drill-report.json; exit $rc",
            retries=1, timeout_s=1800)
        report = json.loads(out[out.index("{"):]) if "{" in out else {}
        result["report"] = report
        result["ok"] = bool(report.get("ok"))
        result["problems"] = report.get("problems", [])
    except Exception as e:
        result["problems"].append(str(e)[:500])
        if instance is not None:
            with contextlib_suppress(Exception):
                result["log_tail"] = engine.ssh_run(
                    client.load(Instance, instance.id).ipv4[0], str(key_path),
                    "tail -40 /root/drill.log", retries=1)[-4000:]
    finally:
        if not keep_vm:


            with contextlib_suppress(Exception):
                for inst in engine.retry_transient(lambda: list(client.linode.instances())):
                    if RECOVERY_DRILL_TAG not in (inst.tags or []):
                        continue
                    with contextlib_suppress(Exception):
                        drill_ips.update(str(a) for a in (inst.ipv4 or []))
                    engine.retry_transient(inst.delete)
                    result["vm_deleted"] = True
            for drill_ip in drill_ips:
                with contextlib_suppress(Exception):
                    engine.reset_known_host(drill_ip)
    result["finished_at"] = datetime.now(UTC).isoformat()
    _record_drill(result)
    record_activity(f"recovery drill {'passed' if result['ok'] else 'FAILED'}"
                    + ("" if result["ok"] else ": " + "; ".join(result["problems"])[:400]),
                    level="info" if result["ok"] else "error", source="system",
                    action="recovery-drill")
    return result


def cmd_recovery_drill(client, args) -> int:
    try:
        result = run_recovery_drill(client, region=args.region, keep_vm=args.keep_vm,
                                    on_progress=print)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result["ok"]:
        print("Recovery drill passed: " + "; ".join(result.get("report", {}).get("checks", [])))
        return 0
    print("Recovery drill FAILED: " + "; ".join(result["problems"]), file=sys.stderr)
    if result.get("log_tail"):
        print(result["log_tail"], file=sys.stderr)
    return 1


ROTATION_PREVIOUS_OBJECT_NAME = "ssh-key-previous.enc"


def _key_body(public_key: str) -> str:
    parts = public_key.split()
    for i, part in enumerate(parts):
        if part.startswith(("ssh-", "ecdsa-", "sk-")) and i + 1 < len(parts):
            return parts[i + 1]
    return parts[1] if len(parts) > 1 else public_key


def _remove_authorized_key_command(public_key: str) -> str:

    body = shlex.quote(_key_body(public_key))
    return ("f=/root/.ssh/authorized_keys; "
            f"awk -v k={body} '{{keep=1; for(i=1;i<=NF;i++) if($i==k) keep=0}} keep' \"$f\" > \"$f.lis\" "
            "&& cat \"$f.lis\" > \"$f\" && rm -f \"$f.lis\"")


def key_rotation_state() -> dict | None:
    def _do():
        conn = _connect()
        try:
            row = conn.execute("SELECT started_at, previous_fingerprint, previous_public_key, "
                               "new_public_key, previous_linode_ssh_key_id FROM key_rotation "
                               "WHERE id = 1").fetchone()
            nodes = conn.execute("SELECT name, status, detail, updated_at FROM key_rotation_nodes "
                                 "ORDER BY name").fetchall()
            return row, nodes
        finally:
            conn.close()

    row, nodes = _retry_db(_do)
    if row is None:
        return None
    return {"started_at": row[0], "previous_fingerprint": row[1], "previous_public_key": row[2],
            "new_public_key": row[3], "previous_linode_ssh_key_id": row[4],
            "nodes": [{"name": n, "status": st, "detail": d, "updated_at": u} for n, st, d, u in nodes]}


def _set_rotation_node(name: str, status: str, detail: str | None = None) -> None:
    def _do():
        conn = _connect()
        try:
            conn.execute("INSERT INTO key_rotation_nodes (name, status, detail, updated_at) VALUES "
                         "(?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET status = excluded.status, "
                         "detail = excluded.detail, updated_at = excluded.updated_at",
                         (name, status, detail, datetime.now(UTC).isoformat()))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def previous_key_path() -> Path:
    key = deployment_ssh_key_path()
    return key.with_name(key.name + ".previous")


def _move_node_to_new_key(name: str, record: dict, key_path: Path, rotation: dict) -> str:

    host = _ssh_target(record, name)
    new_pub = rotation["new_public_key"]
    engine.ssh_run(host, str(key_path), "grep -qF " + shlex.quote(_key_body(new_pub))
                   + " /root/.ssh/authorized_keys || { " + _append_authorized_key_command(new_pub) + "; }",
                   retries=2)

    with engine.only_this_ssh_key():
        engine.ssh_run(host, str(key_path), "true", retries=2)
        engine.ssh_run(host, str(key_path), _remove_authorized_key_command(
            rotation["previous_public_key"]), retries=2)
        engine.ssh_run(host, str(key_path), "true", retries=2)
    return "moved to the new key"


@dataclass
class KeyRotationResult:
    started: bool = False
    moved: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


    blocked: list[str] = field(default_factory=list)
    finished: bool = False
    problems: list[str] = field(default_factory=list)


def rotate_deployment_ssh_key(client, *, on_progress: Callable[[str], None] | None = None,
                              on_warning: Callable[[str], None] | None = None) -> KeyRotationResult:

    say = on_progress or (lambda _m: None)
    if key_rotation_state() is not None:
        raise engine.ConfigError("a key rotation is already in progress -- `rotate-credentials ssh-key "
                                 "--continue` moves the remaining nodes.")
    passphrase = os.environ.get(SSH_KEY_PASSPHRASE_ENV) or ""
    if len(passphrase) < MIN_SSH_KEY_PASSPHRASE_LENGTH:
        raise engine.ConfigError(f"{SSH_KEY_PASSPHRASE_ENV} must be set (the old key is kept "
                                 "encrypted with it until the rotation finishes).")
    key_path = deployment_ssh_key_path()
    if not key_path.is_file():
        raise engine.ConfigError(f"no deployment SSH key at {key_path}.")
    old_private = key_path.read_bytes()
    old_public = _public_key_text(key_path, old_private)
    if not old_public:
        raise engine.ConfigError("couldn't read the current key's public half.")
    old_row = _deployment_secret_row()
    with tempfile.TemporaryDirectory() as tmp:
        new_path = Path(tmp) / "new_key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                        f"{LINODE_SSH_KEY_LABEL_PREFIX}-{socket.gethostname()}", "-f", str(new_path)],
                       check=True, capture_output=True, timeout=30)
        new_private = new_path.read_bytes()
        new_public = new_path.with_name("new_key.pub").read_text().strip()
    blob = encrypt_ssh_key(old_private, passphrase)
    names = list(load_registry())

    def _start():
        conn = _connect()
        try:
            conn.execute("INSERT INTO key_rotation (id, started_at, previous_blob, "
                         "previous_fingerprint, previous_public_key, new_public_key, "
                         "previous_linode_ssh_key_id) VALUES (1, ?, ?, ?, ?, ?, ?)",
                         (datetime.now(UTC).isoformat(), blob, hashlib.sha256(old_private).hexdigest(),
                          old_public, new_public, old_row[4] if old_row else None))
            for name in names:
                conn.execute("INSERT OR REPLACE INTO key_rotation_nodes (name, status, detail, "
                             "updated_at) VALUES (?, 'pending', NULL, ?)",
                             (name, datetime.now(UTC).isoformat()))
            conn.commit()
        finally:
            conn.close()


    _retry_db(_start)
    if osb.is_configured():
        osb.upload_deployment_file(ROTATION_PREVIOUS_OBJECT_NAME, bytes(blob))
    prev = previous_key_path()
    fd = os.open(prev, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(old_private)
    prev.with_name(prev.name + ".pub").write_text(old_public + "\n")
    _write_env_settings({engine.PREVIOUS_SSH_KEY_ENV: str(prev)})

    tmp_new = key_path.with_name(key_path.name + ".new")
    fd = os.open(tmp_new, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(new_private)
    os.replace(tmp_new, key_path)
    key_path.with_name(key_path.name + ".pub").write_text(new_public + "\n")
    protection = protect_deployment_ssh_key(client, key_path)
    result = KeyRotationResult(started=True, problems=list(protection.problems))
    say("New deployment SSH key in place; the old one is kept until every node trusts the new one.")
    record_activity("deployment SSH key rotation started", level="warning", source="system",
                    action="ssh-key-rotate")
    more = continue_key_rotation(client, on_progress=on_progress, on_warning=on_warning)
    result.moved, result.pending, result.failed = more.moved, more.pending, more.failed
    result.blocked = more.blocked
    result.finished = more.finished
    result.problems += more.problems
    return result


def continue_key_rotation(client, *, on_progress: Callable[[str], None] | None = None,
                          on_warning: Callable[[str], None] | None = None) -> KeyRotationResult:

    say = on_progress or (lambda _m: None)
    rotation = key_rotation_state()
    result = KeyRotationResult()
    if rotation is None:
        return result
    registry = load_registry()
    key_path = deployment_ssh_key_path()
    for node in rotation["nodes"]:
        name = node["name"]
        if node["status"] == "done":
            continue
        record = registry.get(name)
        if record is None:
            _set_rotation_node(name, "done", "no longer managed")
            continue
        if record.get("current_status") == "needs_manual_recovery":
            result.blocked.append(name)
            continue
        if record.get("current_status") != "running" or record.get("transitioning"):
            result.pending.append(name)
            continue
        try:
            with _instance_lock(name):
                detail = _move_node_to_new_key(name, record, key_path, rotation)
            _set_rotation_node(name, "done", detail)
            result.moved.append(name)
            say(f"  {name}: {detail}")
        except InstanceLockedError:
            result.pending.append(name)
        except Exception as e:
            _set_rotation_node(name, "failed", str(e)[:300])
            result.failed.append(name)
            if on_warning is not None:
                on_warning(f"WARNING: {name}: couldn't move to the new key ({e}); it keeps the old "
                           "key and is retried.")
    rotation = key_rotation_state()
    if rotation and all(n["status"] == "done" for n in rotation["nodes"]):
        _finish_key_rotation(client, rotation)
        result.finished = True
        say("Every node trusts the new key; the old key is removed everywhere.")
    return result


KEY_ROTATION_STALE_DAYS = 7


def key_rotation_health(*, now: datetime | None = None) -> list[HealthProblem]:

    rotation = key_rotation_state()
    if rotation is None:
        return []
    now = now or datetime.now(UTC)
    registry = load_registry()
    open_nodes = [n for n in rotation["nodes"] if n["status"] != "done"]
    blocked = [n["name"] for n in open_nodes
               if (registry.get(n["name"]) or {}).get("current_status") == "needs_manual_recovery"]
    failed = [n["name"] for n in open_nodes if n["status"] == "failed" and n["name"] not in blocked]
    age_days = (now - datetime.fromisoformat(rotation["started_at"])).days
    parts = []
    if blocked:
        parts.append(f"{', '.join(blocked)} need manual recovery and won't move by themselves "
                     "(recover with `onboard --force`, or offboard/deregister)")
    if failed:
        parts.append(f"moving {', '.join(failed)} keeps failing")
    if age_days >= KEY_ROTATION_STALE_DAYS and open_nodes:
        parts.append(f"open for {age_days} days with {len(open_nodes)} node(s) still on the old key")
    if not parts:
        return []
    return [HealthProblem("key-rotation", "the SSH key rotation isn't finishing, so the old key is "
                          "still trusted: " + "; ".join(parts))]


def _finish_key_rotation(client, rotation: dict) -> None:
    if client is not None and rotation.get("previous_linode_ssh_key_id"):
        with contextlib_suppress(Exception):
            for key in engine.retry_transient(lambda: list(client.profile.ssh_keys())):
                if key.id == rotation["previous_linode_ssh_key_id"]:
                    engine.retry_transient(key.delete)
    if osb.is_configured():
        with contextlib_suppress(Exception):
            osb.delete_deployment_file(ROTATION_PREVIOUS_OBJECT_NAME)
    _write_env_settings({engine.PREVIOUS_SSH_KEY_ENV: None})
    prev = previous_key_path()
    for path in (prev, prev.with_name(prev.name + ".pub")):
        with contextlib_suppress(FileNotFoundError):
            path.unlink()

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM key_rotation")
            conn.execute("DELETE FROM key_rotation_nodes")
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)
    record_activity("deployment SSH key rotation finished", source="system", action="ssh-key-rotate")


CREDENTIAL_KINDS = ("api-token", "bucket-key", "oauth-secret", "ssh-key")


def _wait_for_scheduler_reload(changed_at: datetime, timeout_s: float = 120) -> bool:

    beat = None
    with contextlib_suppress(Exception):
        beat = get_scheduler_heartbeat()
    last = datetime.fromisoformat(beat["last_tick"]) if beat and beat.get("last_tick") else None
    if last is None or (changed_at - last).total_seconds() > 600:
        return True
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib_suppress(Exception):
            beat = get_scheduler_heartbeat()
            if beat and datetime.fromisoformat(beat["last_tick"]) > changed_at:
                return True
        time.sleep(3)
    return False


def _current_token_object(client):
    token = engine.load_token()
    for t in engine.retry_transient(lambda: list(client.profile.tokens())):
        shown = (t.token or "").rstrip("*")
        if len(shown) >= 8 and token.startswith(shown):
            return t
    return None


def rotate_api_token(client, *, on_progress: Callable[[str], None] | None = None) -> dict:

    say = on_progress or (lambda _m: None)
    old = _current_token_object(client)
    if old is None:
        raise engine.ConfigError("couldn't find the current token among the profile's tokens "
                                 "(it may be an OAuth token) -- rotate it in Cloud Manager instead.")
    expiry = None
    if isinstance(old.expiry, datetime) and isinstance(old.created, datetime):
        expiry = datetime.now(UTC) + (old.expiry - old.created)
    created = engine.retry_transient(lambda: client.profile.token_create(
        label=f"instance-scheduler {datetime.now(UTC):%Y-%m-%d}"[:100], expiry=expiry,
        scopes=old.scopes))
    new_token = created.token
    check = engine.build_client(new_token)
    engine.auth_check(check)
    changed_at = datetime.now(UTC)
    _write_env_settings({"LINODE_API_TOKEN": new_token})
    engine.set_token_on_clients(new_token)
    say("  new API token written to .env and in use here")
    if not _wait_for_scheduler_reload(changed_at):
        raise engine.ConfigError(
            "the new token is in .env, but the scheduler hasn't picked it up yet -- the old token "
            f"('{old.label}') was NOT revoked; revoke it in Cloud Manager once the scheduler is running.")
    engine.retry_transient(old.delete)
    say(f"  old API token ('{old.label}') revoked")
    record_activity("Linode API token rotated", level="warning", source="system",
                    action="credentials-rotate")
    return {"kind": "api-token", "revoked": old.label}


def rotate_bucket_key(client, *, on_progress: Callable[[str], None] | None = None) -> dict:

    say = on_progress or (lambda _m: None)
    if not osb.is_configured():
        raise engine.ConfigError("Object Storage isn't configured.")
    old_access = os.environ["LINODE_OBJ_STORAGE_ACCESS_KEY"]
    old = next((k for k in engine.retry_transient(lambda: list(client.object_storage.keys()))
                if k.access_key == old_access), None)
    if old is None:
        raise engine.ConfigError("the current bucket key isn't one of this account's keys -- "
                                 "rotate it where it was created.")
    access = getattr(old, "bucket_access", None) or None
    bucket_access = ([{"region": getattr(a, "region", None) or a.get("region"),
                       "bucket_name": getattr(a, "bucket_name", None) or a.get("bucket_name"),
                       "permissions": getattr(a, "permissions", None) or a.get("permissions")}
                      for a in access] if access else None)
    new = engine.retry_transient(lambda: client.object_storage.keys_create(
        f"instance-scheduler {datetime.now(UTC):%Y-%m-%d}"[:50], bucket_access=bucket_access))
    configure_system_backup(os.environ["LINODE_OBJ_STORAGE_BUCKET"],
                            os.environ["LINODE_OBJ_STORAGE_ENDPOINT"], new.access_key, new.secret_key)
    say("  new bucket key tested and written to .env")
    engine.retry_transient(old.delete)
    say(f"  old bucket key ({old.label}) deleted")
    record_activity("Object Storage key rotated", level="warning", source="system",
                    action="credentials-rotate")
    return {"kind": "bucket-key", "revoked": old.label}


def rotate_oauth_secret(client, *, on_progress: Callable[[str], None] | None = None) -> dict:

    from linode_api4.objects import OAuthClient
    client_id = os.environ.get("LINODE_OAUTH_CLIENT_ID")
    if not client_id:
        raise engine.ConfigError("LINODE_OAUTH_CLIENT_ID isn't set -- no dashboard login to rotate.")
    secret = engine.retry_transient(lambda: OAuthClient(client, client_id).reset_secret())
    _write_env_settings({"LINODE_OAUTH_CLIENT_SECRET": secret})
    (on_progress or (lambda _m: None))("  new OAuth client secret written to .env")
    record_activity("dashboard OAuth client secret rotated", level="warning", source="system",
                    action="credentials-rotate")
    return {"kind": "oauth-secret"}


def cmd_rotate_credentials(client, args) -> int:
    try:
        if args.kind == "ssh-key":
            if args.continue_rotation:
                result = continue_key_rotation(client, on_progress=print, on_warning=_print_to_stderr)
            else:
                result = rotate_deployment_ssh_key(client, on_progress=print, on_warning=_print_to_stderr)
            for p in result.problems:
                print(f"  {p}", file=sys.stderr)
            if result.finished:
                print("Rotation finished: every node trusts only the new key.")
            elif key_rotation_state() is not None:
                print(f"Still to move (after their next start, automatically): "
                      f"{', '.join(result.pending + result.failed) or 'none'}")
                if result.blocked:
                    print(f"Won't move by themselves (needs manual recovery): "
                          f"{', '.join(result.blocked)}. The old key stays trusted until each is "
                          "recovered (`onboard --force` works during the rotation -- both keys are "
                          "offered), or removed with `offboard`/`deregister`.")
            return 1 if result.failed else 0
        fn = {"api-token": rotate_api_token, "bucket-key": rotate_bucket_key,
              "oauth-secret": rotate_oauth_secret}[args.kind]
        fn(client, on_progress=print)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"{args.kind} rotated.")
    return 0


def restore_previous_key_from_database(passphrase: str) -> Path | None:

    def _do():
        conn = _connect()
        try:
            return conn.execute("SELECT previous_blob, previous_public_key FROM key_rotation "
                                "WHERE id = 1").fetchone()
        finally:
            conn.close()

    row = _retry_db(_do)
    if row is None:
        return None
    prev = previous_key_path()
    prev.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(prev, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(decrypt_ssh_key(bytes(row[0]), passphrase))
    prev.with_name(prev.name + ".pub").write_text(row[1] + "\n")
    _write_env_settings({engine.PREVIOUS_SSH_KEY_ENV: str(prev)})
    return prev


def restore_ssh_key_from_database(dest: Path, passphrase: str, *, force: bool = False) -> Path:

    row = _deployment_secret_row()
    if row is None:
        raise engine.ConfigError("the database holds no copy of the deployment SSH key.")
    private_key = decrypt_ssh_key(bytes(row[0]), passphrase)
    if dest.exists() and dest.read_bytes() != private_key and not force:
        raise engine.ConfigError(f"{dest} already holds a different key -- pass --force to replace it.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(private_key)
    dest.chmod(0o600)
    if row[2]:
        dest.with_name(dest.name + ".pub").write_text(row[2] + "\n")
    return dest


ENV_BACKUP_RECIPIENTS_ENV = "LIS_ENV_BACKUP_RECIPIENTS"
ENV_BACKUP_OBJECT_NAME = "env.age"
SSH_KEY_AGE_OBJECT_NAME = "ssh-key.age"

_ENV_BACKUP_KEYS = (
    "LINODE_API_TOKEN", "LINODE_OAUTH_CLIENT_ID", "LINODE_OAUTH_CLIENT_SECRET",
    "LINODE_OAUTH_REDIRECT_URI", "LINODE_SSH_KEY_PATH", "LINODE_API_MAX_REQUESTS_PER_SECOND",
    "ACTIVITY_LOG_RETENTION_DAYS", "API_ALLOWED_ORIGINS", "LINODE_OBJ_STORAGE_BUCKET",
    "LINODE_OBJ_STORAGE_ENDPOINT", "LINODE_OBJ_STORAGE_ACCESS_KEY", "LINODE_OBJ_STORAGE_SECRET_KEY",
    "LIS_SSH_KEY_PASSPHRASE", "LIS_BACKUP_KEEP_ALL_HOURS", "LIS_BACKUP_KEEP_DAILY_DAYS",
    "LIS_ALERT_WEBHOOK_URL", "LIS_ALERT_EMAIL_TO", "LIS_ALERT_SMTP_HOST", "LIS_ALERT_SMTP_PORT",
    "LIS_ALERT_SMTP_SECURITY", "LIS_ALERT_SMTP_USER", "LIS_ALERT_SMTP_PASSWORD",
    "LIS_ALERT_EMAIL_FROM", "LIS_ENV_BACKUP_RECIPIENTS", "LINODE_SCHEDULER_BACKUP_DIR",
    "LIS_HEARTBEAT_URL",
)
ENV_BACKUP_STALE_SECONDS = 15 * 60


def parse_age_recipients(text: str | None) -> list:

    from pyrage import x25519
    out = []
    for item in re.split(r"[\s,]+", (text or "").strip()):
        if not item:
            continue
        try:
            out.append(x25519.Recipient.from_str(item))
        except Exception as e:
            raise engine.ConfigError(f"'{item[:20]}...' isn't an age public key (age1...): {e}") from e
    return out


def _parse_age_identities(text: str) -> list:
    from pyrage import x25519
    ids = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("AGE-SECRET-KEY-"):
            try:
                ids.append(x25519.Identity.from_str(line))
            except Exception as e:
                raise engine.ConfigError(f"an AGE-SECRET-KEY line isn't valid: {e}") from e
    if not ids:
        raise engine.ConfigError("no age identity (a line starting AGE-SECRET-KEY-) was given.")
    return ids


def _env_backup_source() -> bytes:
    if ENV_PATH.is_file():
        return ENV_PATH.read_bytes()
    lines = [f"{k}={os.environ[k]}" for k in _ENV_BACKUP_KEYS if os.environ.get(k)]
    return ("\n".join(lines) + "\n").encode() if lines else b""


def _env_backup_row() -> tuple | None:
    def _do():
        conn = _connect()
        try:
            return conn.execute("SELECT fingerprint, uploaded_at, last_attempt_at, last_error "
                                "FROM env_backup_status WHERE id = 1").fetchone()
        finally:
            conn.close()

    return _retry_db(_do)


def _set_env_backup_row(**fields) -> None:
    def _do():
        conn = _connect()
        try:
            conn.execute("INSERT OR IGNORE INTO env_backup_status (id) VALUES (1)")
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE env_backup_status SET {sets} WHERE id = 1", tuple(fields.values()))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


@dataclass
class EnvBackupResult:
    outcome: str
    detail: str | None = None
    ssh_key_included: bool = False


def backup_env_encrypted(*, force: bool = False, verify_remote: bool = False) -> EnvBackupResult:

    try:
        recipients = parse_age_recipients(os.environ.get(ENV_BACKUP_RECIPIENTS_ENV))
    except engine.ConfigError as e:
        _record_env_backup_failure(str(e))
        return EnvBackupResult("failed", str(e))
    if not recipients:
        return EnvBackupResult("not_configured")
    if not osb.is_configured():
        return EnvBackupResult("not_configured", "needs Object Storage")
    import pyrage
    data = _env_backup_source()
    key_path = deployment_ssh_key_path()
    key_bytes = key_path.read_bytes() if key_path.is_file() else b""
    fingerprint = hashlib.sha256(
        data + b"\0" + key_bytes + b"\0"
        + os.environ.get(ENV_BACKUP_RECIPIENTS_ENV, "").encode()).hexdigest()
    try:
        row = _env_backup_row()
        if (not force and row is not None and row[0] == fingerprint
                and (not verify_remote
                     or osb.download_deployment_file(ENV_BACKUP_OBJECT_NAME) is not None)):
            return EnvBackupResult("already_current", ssh_key_included=bool(key_bytes))
        _set_env_backup_row(last_attempt_at=datetime.now(UTC).isoformat())
        osb.upload_deployment_file(ENV_BACKUP_OBJECT_NAME, pyrage.encrypt(data, recipients))
        if key_bytes:
            osb.upload_deployment_file(SSH_KEY_AGE_OBJECT_NAME, pyrage.encrypt(key_bytes, recipients))
        _set_env_backup_row(fingerprint=fingerprint, uploaded_at=datetime.now(UTC).isoformat(),
                            last_error=None)
        return EnvBackupResult("uploaded", ssh_key_included=bool(key_bytes))
    except Exception as e:
        _record_env_backup_failure(str(e))
        return EnvBackupResult("failed", str(e))


def _record_env_backup_failure(message: str) -> None:
    with contextlib_suppress(Exception):
        _set_env_backup_row(last_error=message[:500], last_attempt_at=datetime.now(UTC).isoformat())


def env_backup_status() -> dict:
    configured = bool(os.environ.get(ENV_BACKUP_RECIPIENTS_ENV, "").strip())
    row = None
    with contextlib_suppress(Exception):
        row = _env_backup_row()
    recipients = [r for r in re.split(r"[\s,]+", os.environ.get(ENV_BACKUP_RECIPIENTS_ENV, "")) if r]
    return {"configured": configured, "recipients": [r[:12] + "..." + r[-6:] for r in recipients],
            "uploaded_at": row[1] if row else None, "last_error": row[3] if row else None}


@dataclass
class EnvRestoreResult:
    keys: list[str]
    written_to: str | None
    ssh_key_written_to: str | None
    text: str | None = None


def restore_env_from_backup(
    identity_text: str, *, dest: Path | None = None, ssh_key_dest: Path | None = None,
    keep: Collection[str] = (), force: bool = False, print_only: bool = False,
) -> EnvRestoreResult:

    import pyrage
    if not osb.is_configured():
        raise engine.ConfigError("Object Storage isn't configured -- the bucket settings are needed "
                                 "to fetch the backup.")
    identities = _parse_age_identities(identity_text)
    blob = osb.download_deployment_file(ENV_BACKUP_OBJECT_NAME)
    if blob is None:
        raise engine.ConfigError("no encrypted .env backup in Object Storage (deployment/env.age).")
    try:
        plain = pyrage.decrypt(blob, identities).decode()
    except Exception as e:
        raise engine.ConfigError("this age identity can't decrypt the backup (wrong key?).") from e
    if print_only:
        return EnvRestoreResult(keys=[], written_to=None, ssh_key_written_to=None, text=plain)
    import io
    restored = {k: v for k, v in dotenv_values(stream=io.StringIO(plain)).items() if v is not None}
    dest = dest or ENV_PATH
    current = dotenv_values(dest) if dest.exists() else {}
    if current.get("LINODE_API_TOKEN") and not force:
        raise engine.ConfigError(f"{dest} already has an API token -- pass --force to replace it.")
    for key in keep:
        value = current.get(key)
        if value:
            restored[key] = value
    tmp = dest.with_name(dest.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in restored.items()))
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    key_written = None
    if ssh_key_dest is not None:
        key_blob = osb.download_deployment_file(SSH_KEY_AGE_OBJECT_NAME)
        if key_blob is not None:
            private_key = pyrage.decrypt(key_blob, identities)
            if ssh_key_dest.exists() and ssh_key_dest.read_bytes() != private_key and not force:
                raise engine.ConfigError(
                    f"{ssh_key_dest} already holds a different key -- pass --force to replace it.")
            ssh_key_dest.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(ssh_key_dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(private_key)
            ssh_key_dest.chmod(0o600)
            pub = _public_key_text(ssh_key_dest, private_key)
            if pub:
                ssh_key_dest.with_name(ssh_key_dest.name + ".pub").write_text(pub + "\n")
            key_written = str(ssh_key_dest)
    return EnvRestoreResult(keys=sorted(restored), written_to=str(dest), ssh_key_written_to=key_written)


@dataclass
class RestoreResult:
    source: str
    instances: int = 0
    groups: int = 0
    known_hosts_restored: int = 0
    previous_database: str | None = None


def _registry_has_content(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("instances", "schedule_groups"):
                if table in tables and conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                    return True
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return True
    return False


def _validate_snapshot(path: Path) -> tuple[int, int]:

    try:
        conn = sqlite3.connect(path)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                raise engine.ConfigError(f"the snapshot failed SQLite's integrity check ({ok}).")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "instances" not in tables:
                raise engine.ConfigError("that file isn't a registry database (no instances table).")
            instances = conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
            groups = (conn.execute("SELECT COUNT(*) FROM schedule_groups").fetchone()[0]
                      if "schedule_groups" in tables else 0)
            return instances, groups
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise engine.ConfigError(f"that file isn't a readable SQLite database ({e}).") from e


def _merge_known_hosts(data: bytes) -> int:

    path = known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text().splitlines() if path.exists() else []
    seen = {line.strip() for line in existing if line.strip()}
    added = [line for line in data.decode(errors="replace").splitlines()
             if line.strip() and not line.startswith("#") and line.strip() not in seen]
    if added:
        with engine.known_hosts_file_lock(path), open(path, "a") as f:
            if existing and not path.read_text().endswith("\n"):
                f.write("\n")
            f.write("\n".join(added) + "\n")
        path.chmod(0o600)
    return len(added)


def list_database_snapshots() -> list[str]:
    if not osb.is_configured():
        raise engine.ConfigError("Object Storage isn't configured (LINODE_OBJ_STORAGE_* in .env).")
    keys = osb.list_database_snapshots()
    if keys is None:
        raise engine.ConfigError("couldn't list the bucket's database snapshots.")
    return keys


RESTORE_ITEM_KINDS = ("schedule", "hooks", "group")


def _open_snapshot(snapshot_key: str | None, from_file: Path | None, tmp: Path) -> sqlite3.Connection:
    staged = tmp / "item.db"
    if from_file is not None:
        if not from_file.is_file():
            raise engine.ConfigError(f"no file at {from_file}.")
        staged.write_bytes(from_file.read_bytes())
    else:
        key = snapshot_key or list_database_snapshots()[-1]
        try:
            osb.download_database_snapshot(key, staged)
        except osb.ObjectStorageError as e:
            raise engine.ConfigError(f"couldn't download snapshot {key}: {e}") from e
    problem = osb.database_integrity_problem(staged)
    if problem:
        raise engine.ConfigError(f"the snapshot fails its integrity check: {problem}")
    return sqlite3.connect(f"file:{staged}?mode=ro", uri=True)


def read_item_from_snapshot(kind: str, name: str, *, snapshot_key: str | None = None,
                            from_file: Path | None = None) -> dict | None:

    if kind not in RESTORE_ITEM_KINDS:
        raise engine.ConfigError(f"kind must be one of {', '.join(RESTORE_ITEM_KINDS)}.")
    with tempfile.TemporaryDirectory() as tmp:
        conn = _open_snapshot(snapshot_key, from_file, Path(tmp))
        try:
            if kind == "schedule":
                row = conn.execute("SELECT timezone, rules, enabled FROM schedules "
                                   "WHERE instance_name = ?", (name,)).fetchone()
                return None if row is None else {"timezone": row[0], "rules": json.loads(row[1]),
                                                 "enabled": bool(row[2])}
            if kind == "hooks":
                row = conn.execute("SELECT config FROM instance_hooks WHERE instance_name = ?",
                                   (name,)).fetchone()
                return None if row is None else json.loads(row[0])
            cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_groups)")}
            holidays = "account_holidays" if "account_holidays" in cols else "'follow'"
            row = conn.execute(f"SELECT id, timezone, rules, enabled, {holidays} FROM "
                               "schedule_groups WHERE name = ?", (name,)).fetchone()
            if row is None:
                return None
            group = {"timezone": row[1], "rules": json.loads(row[2] or "[]"),
                     "enabled": bool(row[3]), "account_holidays": row[4] or "follow",
                     "hooks": None, "depends_on": [], "members": []}
            with contextlib_suppress(sqlite3.Error):
                hooks = conn.execute("SELECT config FROM group_hooks WHERE group_id = ?",
                                     (row[0],)).fetchone()
                group["hooks"] = json.loads(hooks[0]) if hooks else None
            with contextlib_suppress(sqlite3.Error):
                group["depends_on"] = [r[0] for r in conn.execute(
                    "SELECT g.name FROM group_dependencies d JOIN schedule_groups g "
                    "ON g.id = d.depends_on_group_id WHERE d.group_id = ? ORDER BY g.name", (row[0],))]
            group["members"] = [r[0] for r in conn.execute(
                "SELECT name FROM instances WHERE group_id = ? ORDER BY name", (row[0],))]
            return group
        finally:
            conn.close()


@dataclass
class RestoreItemResult:
    kind: str
    name: str
    restored: dict
    applied: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def restore_item_from_snapshot(
    client, kind: str, name: str, *, snapshot_key: str | None = None,
    from_file: Path | None = None, with_members: bool = False, actor: str | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> RestoreItemResult:

    item = read_item_from_snapshot(kind, name, snapshot_key=snapshot_key, from_file=from_file)
    if item is None:
        raise engine.ConfigError(f"the snapshot has no {kind} for '{name}'.")
    result = RestoreItemResult(kind=kind, name=name, restored=item)
    if kind == "schedule":
        if name not in load_registry():
            raise NotOnboardedError(f"'{name}' is not onboarded now -- recover or onboard it first.")
        save_instance_schedule(client, name, item["timezone"], item["rules"], item["enabled"],
                               on_warning=on_warning)
        result.applied.append("schedule")
    elif kind == "hooks":
        if name not in load_registry():
            raise NotOnboardedError(f"'{name}' is not onboarded now -- recover or onboard it first.")
        set_instance_hooks(name, item, client=client, actor=actor, on_warning=on_warning)
        result.applied.append("hooks")
    else:
        if get_schedule_group(name) is None:
            create_schedule_group(name, item["timezone"], on_warning=on_warning)
            result.applied.append("group created")
        set_group_schedule(client, name, item["timezone"], item["rules"], item["enabled"],
                           on_warning=on_warning)
        result.applied.append("schedule")
        steps: list[tuple[str, Callable[[], object]]] = [
            ("holiday setting", lambda: set_group_account_holidays(
                client, name, item["account_holidays"], on_warning=on_warning))]
        if item["hooks"]:
            steps.append(("hooks", lambda: set_group_hooks(name, item["hooks"], client=client,
                                                            actor=actor, on_warning=on_warning)))
        existing = [g for g in item["depends_on"] if get_schedule_group(g) is not None]
        missing = sorted(set(item["depends_on"]) - set(existing))
        if missing:
            result.problems.append(f"start order: group(s) {', '.join(missing)} no longer exist")
        steps.append(("start order", lambda: set_group_dependencies(client, name, existing,
                                                                     on_warning=on_warning)))
        if with_members:
            registry = load_registry()
            for member in item["members"]:
                record = registry.get(member)
                if record is None:
                    result.problems.append(f"member {member}: not onboarded now")
                    continue
                if record.get("group_id") is not None:
                    current = _group_row_by_id(record["group_id"])
                    if current and current["name"] != name:
                        result.problems.append(f"member {member}: now in group '{current['name']}' -- left there")
                    continue
                def _add(m: str = member) -> object:
                    return assign_instance_to_group(client, m, name, on_warning=on_warning)
                steps.append((f"member {member}", _add))
        for label, step in steps:
            try:
                step()
                result.applied.append(label)
            except Exception as e:
                result.problems.append(f"{label}: {e}")
    record_activity(f"restored {kind} of '{name}' from "
                    f"{'file ' + str(from_file) if from_file else snapshot_key or 'the latest snapshot'}"
                    f": {', '.join(result.applied)}"
                    + (f" (problems: {'; '.join(result.problems)})" if result.problems else ""),
                    level="warning" if result.problems else "info", source="system",
                    instance_name=name if kind != "group" else None, action="restore-item",
                    actor=actor)
    return result


def cmd_restore_item(client, args) -> int:
    from_file = Path(args.from_file) if args.from_file else None
    try:
        if args.show:
            item = read_item_from_snapshot(args.kind, args.name, snapshot_key=args.snapshot,
                                           from_file=from_file)
            if item is None:
                print(f"The snapshot has no {args.kind} for '{args.name}'.")
                return 1
            print(json.dumps(item, indent=2))
            return 0
        result = restore_item_from_snapshot(
            client, args.kind, args.name, snapshot_key=args.snapshot, from_file=from_file,
            with_members=args.with_members, on_warning=_print_to_stderr)
    except InstanceLockedError as e:
        print(f"{e}", file=sys.stderr)
        return 3
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Restored {args.kind} of '{args.name}': {', '.join(result.applied)}.")
    for p in result.problems:
        print(f"  not restored: {p}", file=sys.stderr)
    return 1 if result.problems else 0


def restore_from_backup(
    *, snapshot_key: str | None = None, from_file: Path | None = None,
    known_hosts_file: Path | None = None, force: bool = False,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> RestoreResult:

    say = on_progress or (lambda _m: None)
    if _registry_has_content(REGISTRY_PATH) and not force:
        raise engine.ConfigError(
            f"{REGISTRY_PATH} already holds instances or groups -- restoring would replace them. "
            "Pass --force to move it aside first (it's kept, not deleted)."
        )
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "restore.db"
        if from_file is not None:
            if not from_file.is_file():
                raise engine.ConfigError(f"no file at {from_file}.")
            staged.write_bytes(from_file.read_bytes())
            source = str(from_file)
        else:
            if not osb.is_configured():
                raise engine.ConfigError(
                    "Object Storage isn't configured (LINODE_OBJ_STORAGE_* in .env) -- give a local "
                    "snapshot with --from-file, or set those up first."
                )
            if snapshot_key is None:
                keys = list_database_snapshots()
                if not keys:
                    raise engine.ConfigError("the bucket has no database snapshots -- run `rebuild` "
                                             "to recover from tags and Object Storage instead.")
                snapshot_key = keys[-1]
            say(f"Downloading {snapshot_key}...")
            try:
                osb.download_database_snapshot(snapshot_key, staged)
            except osb.ObjectStorageError as e:
                raise engine.ConfigError(str(e)) from e
            source = snapshot_key
        instances, groups = _validate_snapshot(staged)
        result = RestoreResult(source=source, instances=instances, groups=groups)
        if REGISTRY_PATH.exists():
            aside = REGISTRY_PATH.with_name(f"{REGISTRY_PATH.name}.pre-restore-{int(time.time())}")
            REGISTRY_PATH.rename(aside)
            result.previous_database = str(aside)
            say(f"Moved the existing database aside to {aside}.")
        for suffix in ("-wal", "-shm"):
            stale = REGISTRY_PATH.with_name(REGISTRY_PATH.name + suffix)
            if stale.exists():
                stale.unlink()
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY_PATH.write_bytes(staged.read_bytes())
    _connect().close()
    say(f"Restored {instances} instance(s) and {groups} group(s) from {source}.")
    try:
        restore_api_tokens_from_object_storage(on_progress=on_progress, on_warning=on_warning)
    except Exception as e:
        if on_warning is not None:
            on_warning(f"WARNING: couldn't reconcile API tokens from Object Storage ({e}); "
                       "run `restore` again or `rebuild` to retry.")
    try:
        restore_holidays_from_object_storage(on_progress=on_progress, on_warning=on_warning)
    except Exception as e:
        if on_warning is not None:
            on_warning(f"WARNING: couldn't restore holidays from Object Storage ({e}).")
    try:
        restore_migration_backups_from_object_storage(on_progress=on_progress,
                                                      on_warning=on_warning)
    except Exception as e:
        if on_warning is not None:
            on_warning(f"WARNING: couldn't reconcile pre-migration backups ({e}).")

    result.known_hosts_restored = restore_known_hosts(
        known_hosts_file, on_progress=on_progress, on_warning=on_warning,
    )
    return result


def restore_known_hosts(
    known_hosts_file: Path | None = None, *,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> int:

    hosts: bytes | None = None
    if known_hosts_file is not None:
        hosts = known_hosts_file.read_bytes()
    elif osb.is_configured():
        try:
            hosts = osb.download_deployment_file(KNOWN_HOSTS_OBJECT_NAME)
        except osb.ObjectStorageError as e:
            if on_warning is not None:
                on_warning(f"  WARNING: couldn't fetch the trusted host keys ({e}).")
    if hosts:
        added = _merge_known_hosts(hosts)
        if on_progress is not None:
            on_progress(f"Restored {added} trusted host key line(s).")
        return added
    if on_warning is not None:
        on_warning("  WARNING: no trusted host keys restored -- a node that's stopped now will "
                   "refuse its next start until `reset-host-key --name <node>` is run for it.")
    return 0


def cmd_restore(args) -> int:
    try:
        if args.known_hosts_only:
            restore_known_hosts(Path(args.known_hosts) if args.known_hosts else None,
                                on_progress=print, on_warning=_print_to_stderr)
            return 0
        if args.list:
            keys = list_database_snapshots()
            if not keys:
                print("No database snapshots in Object Storage.")
            for k in keys:
                print(k)
            return 0
        if not args.yes and _registry_has_content(REGISTRY_PATH) and args.force:
            answer = input(f"Move the current database ({REGISTRY_PATH}) aside and restore? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 1
        result = restore_from_backup(
            snapshot_key=args.snapshot,
            from_file=Path(args.from_file) if args.from_file else None,
            known_hosts_file=Path(args.known_hosts) if args.known_hosts else None,
            force=args.force, on_progress=print, on_warning=_print_to_stderr,
        )
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Restore complete from {result.source}. Now run `rebuild` to add anything onboarded "
          "after this snapshot, then start the poller.")
    return 0


def _read_passphrase(args, *, confirm: bool, allow_env: bool = False) -> str:
    if getattr(args, "passphrase_file", None):
        return Path(args.passphrase_file).read_text().rstrip("\n")
    if allow_env and os.environ.get(SSH_KEY_PASSPHRASE_ENV):
        return os.environ[SSH_KEY_PASSPHRASE_ENV]
    import getpass
    first = getpass.getpass("SSH key backup passphrase: ")
    if confirm and getpass.getpass("Repeat the passphrase: ") != first:
        raise engine.ConfigError("the passphrases didn't match.")
    return first


def cmd_ssh_key_backup(args) -> int:
    try:
        key = backup_ssh_key(Path(args.ssh_key), _read_passphrase(args, confirm=True))
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except osb.ObjectStorageError as e:
        print(f"Object Storage error: {e}", file=sys.stderr)
        return 1
    print(f"Encrypted SSH key stored as '{key}'. Keep the passphrase somewhere safe and separate -- "
          "it isn't stored anywhere, and the key can't be recovered without it.")
    return 0


def cmd_ssh_key_restore(args) -> int:

    dest_path = Path(args.ssh_key)
    try:
        passphrase = ""
        from_escrow = False
        if not getattr(args, "passphrase_file", None) and not os.environ.get(SSH_KEY_PASSPHRASE_ENV):

            with contextlib_suppress(Exception):
                client = engine.build_client(engine.load_token())
                blob = None
                with contextlib_suppress(Exception):
                    blob = osb.download_deployment_file(SSH_KEY_OBJECT_NAME) if osb.is_configured() else None
                if blob is None:
                    with contextlib_suppress(Exception):
                        row = _deployment_secret_row()
                        blob = bytes(row[0]) if row else None
                passphrase = fetch_escrowed_passphrase(client, blob) or ""
                from_escrow = bool(passphrase)
        if not passphrase:
            passphrase = _read_passphrase(args, confirm=False, allow_env=True)
        dest = None
        problems = []
        if not getattr(args, "from_db", False):
            try:
                dest = restore_ssh_key(dest_path, passphrase, force=args.force)
                source = "Object Storage"
            except (engine.ConfigError, osb.ObjectStorageError) as e:
                if "passphrase" in str(e) or "different key" in str(e):
                    raise
                problems.append(f"Object Storage: {e}")
        if dest is None:
            try:
                dest = restore_ssh_key_from_database(dest_path, passphrase, force=args.force)
                source = "the database"
            except engine.ConfigError as e:
                if "passphrase" in str(e) or "different key" in str(e):
                    raise
                problems.append(f"database: {e}")
        if dest is None:
            raise engine.ConfigError("no copy of the deployment SSH key found (" + "; ".join(problems) + ")")
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except osb.ObjectStorageError as e:
        print(f"Object Storage error: {e}", file=sys.stderr)
        return 1
    print(f"SSH key restored to {dest} (from {source}).")
    if from_escrow:
        print("The passphrase came from its recovery copy in your Linode account.")
        if getattr(args, "save_passphrase", False):
            _write_env_settings({SSH_KEY_PASSPHRASE_ENV: passphrase})
            print(f"Saved it to .env as {SSH_KEY_PASSPHRASE_ENV}.")

    try:
        prev = restore_previous_key_from_database(passphrase)
        if prev is not None:
            print(f"A key rotation is in progress: the previous key is restored to {prev} so nodes "
                  "not yet moved stay reachable.")
    except Exception as e:
        print(f"WARNING: a key rotation is in progress but the previous key couldn't be restored "
              f"({e}) -- nodes not yet moved may be unreachable.", file=sys.stderr)
    return 0


def cmd_env_backup(args) -> int:
    if args.set_recipients is not None:
        try:
            if args.set_recipients.strip():
                parse_age_recipients(args.set_recipients)
        except engine.ConfigError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            return 1
        _write_env_settings({ENV_BACKUP_RECIPIENTS_ENV: args.set_recipients.strip() or None})
        print("Recipients saved." if args.set_recipients.strip() else "Encrypted settings backup turned off.")
        if not args.set_recipients.strip():
            return 0
    result = backup_env_encrypted(force=True)
    if result.outcome == "not_configured":
        print(f"Not set up: {result.detail or f'set {ENV_BACKUP_RECIPIENTS_ENV} (your age public key) first -- env-backup --set-recipients age1...'}",
              file=sys.stderr)
        return 1
    if result.outcome == "failed":
        print(f"Configuration error: {result.detail}", file=sys.stderr)
        return 1
    print(f"Encrypted copy of the settings{' and the deployment SSH key' if result.ssh_key_included else ''} "
          f"uploaded to Object Storage (deployment/{ENV_BACKUP_OBJECT_NAME}). Only your age identity "
          "opens it; this host can't.")
    return 0


def cmd_env_restore(args) -> int:
    try:
        text = sys.stdin.read() if args.identity_file == "-" else Path(args.identity_file).read_text()
        result = restore_env_from_backup(
            text, ssh_key_dest=Path(args.ssh_key) if args.ssh_key else None,
            keep=("LINODE_OBJ_STORAGE_BUCKET", "LINODE_OBJ_STORAGE_ENDPOINT",
                  "LINODE_OBJ_STORAGE_ACCESS_KEY", "LINODE_OBJ_STORAGE_SECRET_KEY",
                  "LINODE_SSH_KEY_PATH"),
            force=args.force, print_only=args.print)
    except (engine.ConfigError, OSError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except osb.ObjectStorageError as e:
        print(f"Object Storage error: {e}", file=sys.stderr)
        return 1
    if args.print:
        sys.stdout.write(result.text or "")
        return 0
    print(f"Restored {len(result.keys)} setting(s) to {result.written_to}.")
    if result.ssh_key_written_to:
        print(f"Deployment SSH key restored to {result.ssh_key_written_to}.")
    return 0


def cmd_env_backup_keygen(args) -> int:
    from pyrage import x25519
    identity = x25519.Identity.generate()
    print("# Store this whole file somewhere safe and OFF this host (a password manager). It's the")
    print("# only thing that can open the encrypted settings backup. Generating it on your own machine")
    print("# with `age-keygen` is better still: then the secret never touches this host.")
    print(f"# public key: {identity.to_public()}")
    print(str(identity))
    return 0


def cmd_ssh_key_protect(client, args) -> int:
    result = protect_deployment_ssh_key(client, Path(args.ssh_key))
    print(f"Deployment SSH key {result.fingerprint[:16] if result.fingerprint else '?'}...:")
    print(f"  encrypted copy in the database: {'yes' if result.stored_in_database else 'no'}")
    print(f"  encrypted copy in Object Storage: {result.object_storage}")
    print(f"  public key in Linode SSH keys: "
          f"{'id ' + str(result.linode_ssh_key_id) if result.linode_ssh_key_id else 'no'}")
    print(f"  passphrase recovery copy in the Linode account: "
          f"{'yes' if result.passphrase_escrowed else 'no'}")
    for problem in result.problems:
        print(f"WARNING: {problem}", file=sys.stderr)
    return 0 if result.ok else 1


def _set_group_schedule_row(group_name: str, timezone: str, rules: list, enabled: bool) -> None:

    def _do():
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE schedule_groups SET timezone = ?, rules = ?, enabled = ? WHERE name = ?",
                (timezone, json.dumps(rules), 1 if enabled else 0, group_name),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    if _retry_db(_do) == 0:
        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")


def set_group_schedule(
    client, group_name: str, timezone: str, rules: list, enabled: bool = True,
    on_warning: Callable[[str], None] | None = None,
) -> None:

    _validate_schedule_rules(rules)
    _validate_schedule_timezone(timezone)
    existing = get_schedule_group(group_name)
    if existing is not None:
        modes = get_schedule_modes()
        manual = sorted(m for m in existing["members"] if modes.get(m) == "manual")
        if manual:
            raise engine.ConfigError(
                f"group '{group_name}' has manual-only member(s) {', '.join(manual)}, and a "
                "manual-only node can't be in a scheduled group. Remove them or switch them back "
                "with `set-mode --auto` first."
            )
    _set_group_schedule_row(group_name, timezone, rules, enabled)

    group = get_schedule_group(group_name)
    if group is None:


        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    for member in group["members"]:
        _sync_group_membership_tags(client, member, group, on_warning=on_warning)
    _sync_group_object_backup(group_name, on_warning=on_warning)


def delete_schedule_group(
    group_name: str, *, on_warning: Callable[[str], None] | None = None,
) -> None:

    def _do():
        conn = _connect()
        try:
            group_id = _group_id_for_name(conn, group_name)
            members = [r[0] for r in conn.execute(
                "SELECT name FROM instances WHERE group_id = ?", (group_id,)
            ).fetchall()]
            if members:
                raise engine.ConfigError(
                    f"group '{group_name}' still has {len(members)} member(s): "
                    f"{', '.join(members)} -- run `group-remove` for each one first."
                )
            dependents = _group_dependent_names(conn, group_id)
            if dependents:
                raise engine.ConfigError(
                    f"group(s) {', '.join(dependents)} depend on '{group_name}' -- run "
                    f"`group-depends --group-name <group> --remove {group_name}` for each one "
                    "first."
                )
            try:
                conn.execute("DELETE FROM schedule_groups WHERE id = ?", (group_id,))
                conn.commit()
            except sqlite3.IntegrityError as e:


                raise engine.ConfigError(
                    f"group '{group_name}' gained a new member while being deleted (a rare race "
                    "with a concurrent `group-add`) -- re-run `group-delete` once that's "
                    "resolved."
                ) from e
        finally:
            conn.close()

    _retry_db(_do)
    _sync_group_object_backup(group_name, on_warning=on_warning)


SCHEDULE_MODES = ("auto", "manual")

_SCHEDULE_MODE_MANUAL_TAG = "mode:manual"


def _group_has_schedule(group: dict | None) -> bool:

    return bool(group and group.get("rules"))


def get_schedule_mode(name: str) -> str:

    return get_schedule_modes().get(name, "auto")


def get_schedule_modes() -> dict[str, str]:

    def _do():
        conn = _connect()
        try:
            return dict(conn.execute(
                "SELECT instance_name, schedule_mode FROM instance_settings"
            ).fetchall())
        finally:
            conn.close()

    return _retry_db(_do)


def _write_schedule_mode_row(name: str, mode: str) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO instance_settings (instance_name, schedule_mode) VALUES (?, ?)"
                " ON CONFLICT(instance_name) DO UPDATE SET schedule_mode = excluded.schedule_mode",
                (name, mode),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _sync_schedule_mode_tag_locked(client, name: str, record: dict, mode: str) -> None:

    if client is None or not record.get("os_volume_id"):
        return
    os_volume = engine.retry_transient(
        lambda vol_id=record["os_volume_id"]: client.load(Volume, vol_id)
    )
    current = list(os_volume.tags or [])
    new_tags = [t for t in current if t != _SCHEDULE_MODE_MANUAL_TAG]
    if mode == "manual":
        new_tags.append(_SCHEDULE_MODE_MANUAL_TAG)
    if new_tags != current:
        os_volume.tags = new_tags
        engine.retry_transient(os_volume.save)
        _verify_os_volume_tag_write(client, record["os_volume_id"], new_tags)


def _refuse_if_manual_only(name: str, what: str) -> None:
    if get_schedule_mode(name) == "manual":
        raise engine.ConfigError(
            f"'{name}' is manual-only, so it can't {what}. Switch it back with "
            f"`set-mode --name {name} --auto` first."
        )


def set_schedule_mode(
    client, name: str, mode: str, *, on_warning: Callable[[str], None] | None = None,
) -> str:

    if mode not in SCHEDULE_MODES:
        raise engine.ConfigError(f"schedule mode must be one of {', '.join(SCHEDULE_MODES)}.")
    with _instance_lock(name):
        registry = load_registry()
        record = registry.get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")
        if mode == "manual":
            if get_instance_schedule(name) is not None:
                raise engine.ConfigError(
                    f"'{name}' has its own schedule. Clear it first (`schedule-clear --name "
                    f"{name}`) -- a manual-only node has no schedule."
                )
            group = _group_row_by_id(record["group_id"]) if record.get("group_id") else None
            if _group_has_schedule(group):
                assert group is not None
                raise engine.ConfigError(
                    f"'{name}' is in group '{group['name']}', which has a schedule. Remove it from "
                    f"the group first (`group-remove --name {name} --keep-manual`) -- a "
                    "manual-only node can't be in a scheduled group."
                )
            if record.get("manual_override_expires_at"):
                record["manual_override_expires_at"] = None
                _save_one_record(name, record)
        _write_schedule_mode_row(name, mode)
        try:
            _sync_schedule_mode_tag_locked(client, name, record, mode)
        except Exception as e:
            if on_warning is not None:
                on_warning(
                    f"WARNING: '{name}' is now {mode}, but its disaster-recovery tag couldn't be "
                    f"updated: {e} -- `rebuild` after a database loss may not restore the mode."
                )
    return mode


def assign_instance_to_group(
    client, name: str, group_name: str, on_warning: Callable[[str], None] | None = None
) -> int:

    with _instance_lock(name):
        registry = load_registry()
        if name not in registry:
            raise NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")
        if get_schedule_mode(name) == "manual" and _group_has_schedule(
            get_schedule_group(group_name)
        ):
            raise engine.ConfigError(
                f"'{name}' is manual-only and group '{group_name}' has a schedule -- a "
                f"manual-only node can't be in a scheduled group. Use `set-mode --name {name} "
                "--auto` first, or a group without a schedule."
            )

        def _do():
            conn = _connect()
            try:
                group_id = _group_id_for_name(conn, group_name)
                conn.execute("UPDATE instances SET group_id = ? WHERE name = ?", (group_id, name))
                conn.commit()
            finally:
                conn.close()

        _retry_db(_do)
    group = get_schedule_group(group_name)
    if group is None:


        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    _sync_group_membership_tags(client, name, group, on_warning=on_warning)
    _sync_hook_tags(client, name, on_warning)
    return group["id"]


def remove_instance_from_group(
    client, name: str, *, copy_group_rules_as_individual: bool,
    on_warning: Callable[[str], None] | None = None,
) -> str:

    with _instance_lock(name):
        registry = load_registry()
        if name not in registry:
            raise NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")
        record = registry[name]
        if record.get("group_id") is None:
            raise engine.ConfigError(f"'{name}' is not currently in any group.")

        existing_schedule = get_instance_schedule(name)


        if schedule_is_active(existing_schedule):
            outcome = "removed_had_individual_schedule"
        elif copy_group_rules_as_individual:
            outcome = "removed_copied_schedule"
        else:
            outcome = "removed_kept_manual"

        group_id = record["group_id"]


        group = _group_row_by_id(group_id) if outcome == "removed_copied_schedule" else None
        if outcome == "removed_copied_schedule" and group is not None:
            _validate_schedule_rules(group["rules"])
            _validate_schedule_timezone(group["timezone"])

        def _do():
            conn = _connect()
            try:
                conn.execute("UPDATE instances SET group_id = NULL WHERE name = ?", (name,))
                conn.commit()
            finally:
                conn.close()

        _retry_db(_do)
    _sync_group_membership_tags(client, name, None, on_warning=on_warning)
    _sync_hook_tags(client, name, on_warning)

    if outcome == "removed_copied_schedule":
        if group is None:


            if on_warning is not None:
                on_warning(
                    f"  WARNING: '{name}'s group was deleted concurrently -- nothing to copy, "
                    "left as manual-only."
                )
            return "removed_kept_manual"
        save_instance_schedule(
            client, name, group["timezone"], group["rules"],


            enabled=group.get("enabled", True), on_warning=on_warning,
        )

    return outcome


def cmd_group_create(args) -> int:
    try:
        group_id = create_schedule_group(args.name, args.timezone, on_warning=_print_to_stderr)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Created group '{args.name}' (id {group_id}), timezone {args.timezone}, no rules yet "
          f"-- use group-schedule-set to give it one.")
    return 0


def cmd_group_schedule_set(client, args) -> int:
    rules = _parse_schedule_rules_from_cli_args(args)
    if rules is None:
        return 1

    try:
        set_group_schedule(
            client, args.group_name, args.timezone, rules,
            enabled=not args.disabled, on_warning=_print_to_stderr,
        )
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Schedule set for group '{args.group_name}': {len(rules)} rule(s), "
          f"{'enabled' if not args.disabled else 'disabled'}.")
    return 0


def _csv(value) -> list[str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def cmd_api_token_create(args) -> int:
    try:
        created = create_api_token(
            args.name, _csv(args.scopes) or [], instances=_csv(args.instances),
            groups=_csv(args.groups), expires_days=args.expires_days, created_by="cli",
            on_warning=_print_to_stderr,
        )
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Created token '{created['name']}' (scopes: {', '.join(created['scopes'])}).")
    if created["instances"] or created["groups"]:
        print(f"  limited to nodes {created['instances'] or []} and groups {created['groups'] or []}")
    if created["expires_at"]:
        print(f"  expires {created['expires_at']}")
    print("Store it now -- it is not shown again:")
    print(f"  {created['token']}")
    print("Use it as:  Authorization: Bearer <token>")
    return 0


def cmd_api_token_list(args) -> int:
    tokens = list_api_tokens()
    if not tokens:
        print("No API tokens.")
        return 0
    for t in tokens:
        state = "revoked" if t["revoked_at"] else ("expires " + t["expires_at"] if t["expires_at"]
                                                    else "no expiry")
        limits = ""
        if t["instances"] or t["groups"]:
            limits = f"  nodes={t['instances'] or []} groups={t['groups'] or []}"
        print(f"{t['name']}  {t['token_prefix']}...  scopes={','.join(t['scopes'])}  {state}"
              f"  last used {t['last_used_at'] or 'never'}{limits}")
    return 0


def cmd_api_token_scopes(args) -> int:
    print("Per-operation scopes:")
    for scope, what in API_TOKEN_FINE_SCOPES.items():
        print(f"  {scope:<22} {what}")
    print("Bundles:")
    for bundle, members in API_TOKEN_SCOPE_BUNDLES.items():
        shown = "everything above" if bundle == "admin" else ", ".join(members)
        print(f"  {bundle:<22} {shown}")
    return 0


def cmd_api_token_revoke(args) -> int:
    if not revoke_api_token(args.name, on_warning=_print_to_stderr):
        if _token_record(args.name) is not None:
            print(f"Token '{args.name}' was already revoked (its backup record was re-synced).")
            return 0
        print(f"Configuration error: no active token named '{args.name}'.", file=sys.stderr)
        return 1
    print(f"Token '{args.name}' revoked.")
    return 0


def cmd_set_mode(client, args) -> int:
    mode = "manual" if args.manual else "auto"
    try:
        set_schedule_mode(client, args.name, mode, on_warning=_print_to_stderr)
    except InstanceLockedError as e:
        print(f"{e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if mode == "manual":
        print(f"'{args.name}' is now manual-only: the scheduler will never start or stop it, and "
              "a manual start never arms an auto-stop timer.")
    else:
        print(f"'{args.name}' can be scheduled again (set a schedule or add it to a group).")
    return 0


def cmd_group_depends(client, args) -> int:
    try:
        if args.add:
            deps = add_group_dependency(client, args.group_name, args.add,
                                        on_warning=_print_to_stderr)
        elif args.remove:
            deps = remove_group_dependency(client, args.group_name, args.remove,
                                           on_warning=_print_to_stderr)
        else:
            wanted = [] if args.clear else [
                part.strip() for item in args.on for part in item.split(",") if part.strip()
            ]
            deps = set_group_dependencies(client, args.group_name, wanted,
                                          on_warning=_print_to_stderr)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if not deps:
        print(f"Group '{args.group_name}' no longer depends on any other group.")
    else:
        names = ", ".join(f"'{d}'" for d in deps)
        print(f"Group '{args.group_name}' now depends on {names}: its members start only after "
              f"every member of each of those groups is running and ready, and those groups' "
              f"members stop only after every member of '{args.group_name}' is stopped.")
    return 0


def cmd_group_show(args) -> int:
    group = get_schedule_group(args.group_name)
    if group is None:
        print(f"Configuration error: no schedule group named '{args.group_name}' exists.", file=sys.stderr)
        return 1
    print(json.dumps(group, indent=2))
    return 0


def cmd_group_list(args) -> int:
    groups = list_schedule_groups()
    if not groups:
        print("No schedule groups exist yet -- create one with group-create.")
        return 0
    for g in groups:
        state = "enabled" if g["enabled"] else "disabled"
        dep = (f", depends on {', '.join(repr(d) for d in g['depends_on'])}"
               if g.get("depends_on") else "")
        print(f"{g['name']} (id {g['id']}, {g['timezone']}, {len(g['rules'])} rule(s), {state}, "
              f"{g['member_count']} member(s){dep})")
    return 0


def cmd_group_delete(args) -> int:
    try:
        delete_schedule_group(args.group_name, on_warning=_print_to_stderr)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Group '{args.group_name}' deleted.")
    return 0


def cmd_group_add(client, args) -> int:
    try:
        assign_instance_to_group(client, args.name, args.group_name, on_warning=_print_to_stderr)
    except NotOnboardedError as e:
        print(f"{e}", file=sys.stderr)
        return 1


    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"'{args.name}' added to group '{args.group_name}'.")
    return 0


def cmd_group_remove(client, args) -> int:
    try:
        registry = load_registry()
        if args.name not in registry:
            raise NotOnboardedError(f"'{args.name}' is not onboarded. Run `onboard` first.")
        record = registry[args.name]
        if record.get("group_id") is None:
            raise engine.ConfigError(f"'{args.name}' is not currently in any group.")

        copy_rules = args.copy_schedule
        if (
            not args.copy_schedule and not args.keep_manual


            and not schedule_is_active(get_instance_schedule(args.name)) and not args.yes
        ):
            group = _group_row_by_id(record["group_id"])
            group_label = group["name"] if group else "its group"
            answer = input(
                f"'{args.name}' has no individual schedule of its own and is governed entirely "
                f"by {group_label!r}'s schedule. Removing it from the group would leave it with "
                f"NO schedule at all unless you choose otherwise now.\n"
                f"Copy {group_label!r}'s current rules into a new individual schedule for "
                f"'{args.name}'? [y/N] "
            ).strip().lower()
            copy_rules = answer == "y"

        outcome = remove_instance_from_group(
            client, args.name, copy_group_rules_as_individual=copy_rules,
            on_warning=_print_to_stderr,
        )
    except NotOnboardedError as e:
        print(f"{e}", file=sys.stderr)
        return 1


    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    if outcome == "removed_had_individual_schedule":
        print(f"'{args.name}' removed from its group -- unaffected, it already has its own "
              f"individual schedule.")
    elif outcome == "removed_copied_schedule":
        print(f"'{args.name}' removed from its group -- the group's rules were copied into a "
              f"new individual schedule for it.")
    else:
        print(f"'{args.name}' removed from its group -- now manual-only (no schedule).")
    return 0


DEFAULT_POLL_INTERVAL_SECONDS = 15


POLL_RETRY_BACKOFF_SECONDS = (60, 120, 300, 600)
DEFAULT_POLL_WINDOW_SECONDS = 300


DEFAULT_POLL_CATCH_UP_SECONDS = 3600


DEFAULT_POLL_MAX_PARALLEL = 10
DEFAULT_GROUP_ACTION_MAX_PARALLEL = 10
MAX_POLL_MAX_PARALLEL = 50


DEFAULT_MANUAL_OVERRIDE_WINDOW_HOURS = 2.0


MAX_MANUAL_OVERRIDE_WINDOW_HOURS = 24 * 365 * 10


def _validate_override_window_hours(hours: float | None) -> None:

    if hours is None:
        return
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours):
        raise engine.ConfigError(f"override window hours must be a positive number, got {hours!r}.")
    if not (0 < hours <= MAX_MANUAL_OVERRIDE_WINDOW_HOURS):
        raise engine.ConfigError(
            f"override window hours must be greater than 0 and at most "
            f"{MAX_MANUAL_OVERRIDE_WINDOW_HOURS} (got {hours}) -- use a schedule instead for "
            "anything longer than that."
        )


def _local_time_to_utc(zone: ZoneInfo, day: date, time_str: str) -> datetime:

    hour, minute = (int(p) for p in time_str.split(":"))
    return datetime.combine(day, dtime(hour, minute), tzinfo=zone).astimezone(UTC)


def resolve_due_action(
    schedule: dict, now: datetime, window_seconds: int = DEFAULT_POLL_WINDOW_SECONDS,
) -> Literal["create", "delete", None]:

    due = resolve_due_boundary(schedule, now, window_seconds)
    return due[0] if due is not None else None


def resolve_due_boundary(
    schedule: dict, now: datetime, window_seconds: int = DEFAULT_POLL_WINDOW_SECONDS,
) -> tuple[Literal["create", "delete"], datetime] | None:

    if not schedule.get("enabled", True):
        return None
    zone = ZoneInfo(schedule["timezone"])
    now_utc = now.astimezone(UTC)
    local_today = now.astimezone(zone).date()
    best: tuple[datetime, Literal["create", "delete"]] | None = None


    for day in (local_today - timedelta(days=2), local_today - timedelta(days=1), local_today):
        weekday = VALID_SCHEDULE_DAYS[day.weekday()]
        for rule in schedule.get("rules", []):
            if weekday not in rule["days_of_week"]:
                continue
            candidates: list[tuple[date, str, Literal["create", "delete"]]] = [
                (day, rule["start_time"], "create"),
                (day + timedelta(days=_rule_stop_day_offset(rule)), rule["stop_time"], "delete"),
            ]
            for boundary_day, time_str, action in candidates:
                boundary = _local_time_to_utc(zone, boundary_day, time_str)
                if not boundary <= now_utc < boundary + timedelta(seconds=window_seconds):
                    continue
                if (best is None or boundary > best[0]
                        or (boundary == best[0] and action == "delete")):
                    best = (boundary, action)
    return (best[1], best[0]) if best is not None else None


def next_scheduled_stop(schedule: dict, now: datetime, within_hours: float = 24) -> datetime | None:

    if not schedule.get("enabled", True):
        return None
    zone = ZoneInfo(schedule["timezone"])
    now_utc = now.astimezone(UTC)
    local_today = now.astimezone(zone).date()
    best: datetime | None = None
    for day in (local_today - timedelta(days=1), local_today, local_today + timedelta(days=1)):
        weekday = VALID_SCHEDULE_DAYS[day.weekday()]
        for rule in schedule.get("rules", []):
            if weekday not in rule["days_of_week"]:
                continue
            boundary = _local_time_to_utc(zone, day + timedelta(days=_rule_stop_day_offset(rule)),
                                          rule["stop_time"])
            if now_utc < boundary <= now_utc + timedelta(hours=within_hours) and (
                    best is None or boundary < best):
                best = boundary
    return best


def schedule_is_active(schedule: dict | None) -> TypeGuard[dict]:

    return schedule is not None and schedule.get("enabled", True)


def is_within_scheduled_on_window(schedule: dict, now: datetime) -> bool:

    if not schedule.get("enabled", True):
        return False
    zone = ZoneInfo(schedule["timezone"])
    local_now = now.astimezone(zone)
    today = local_now.date()
    now_utc = now.astimezone(UTC)

    for day in (today - timedelta(days=1), today):
        weekday = VALID_SCHEDULE_DAYS[day.weekday()]
        for rule in schedule.get("rules", []):
            if weekday not in rule["days_of_week"]:
                continue
            start_utc = _local_time_to_utc(zone, day, rule["start_time"])
            stop_day = day + timedelta(days=_rule_stop_day_offset(rule))
            stop_utc = _local_time_to_utc(zone, stop_day, rule["stop_time"])
            if start_utc <= now_utc < stop_utc:
                return True
    return False


def resolve_effective_schedule(name: str, record: dict) -> tuple[dict | None, str | None]:

    schedule = get_instance_schedule(name)
    if schedule_is_active(schedule):
        return schedule, None
    if record.get("group_id") is not None:
        group = _group_row_by_id(record["group_id"])
        if group is not None and group["rules"]:
            return (
                {
                    "timezone": group["timezone"], "rules": group["rules"],
                    "enabled": group["enabled"],
                },
                group["name"],
            )
    if schedule is not None:
        return schedule, None
    return None, None


def compute_scheduled_savings_percent(schedule: dict) -> float | None:

    rules = schedule.get("rules") or []
    if not rules:
        return None
    total_hours_per_week = 0.0
    for rule in rules:
        hours_per_occurrence = _rule_on_minutes(rule) / 60
        total_hours_per_week += hours_per_occurrence * len(rule["days_of_week"])
    return round(max(0.0, (1 - total_hours_per_week / 168) * 100), 1)


def compute_actual_savings_percent(
    events: list[dict], window_start: datetime, window_end: datetime,
) -> float | None:

    successes = [e for e in events if e["result"] == "success"]
    if not successes:
        return None
    uptime_seconds = 0.0
    up_since: datetime | None = None
    seen_any_event_in_window = False
    for event in successes:
        ts = datetime.fromisoformat(event["timestamp"])
        if ts < window_start or ts >= window_end:
            continue
        if event["action"] == "create":
            if up_since is None:
                up_since = ts


            seen_any_event_in_window = True
        elif event["action"] == "delete":
            if up_since is not None:
                uptime_seconds += (ts - up_since).total_seconds()
                up_since = None
            elif not seen_any_event_in_window:
                uptime_seconds += (ts - window_start).total_seconds()


            seen_any_event_in_window = True
    if up_since is not None:
        uptime_seconds += (window_end - up_since).total_seconds()
    window_seconds = (window_end - window_start).total_seconds()
    if window_seconds <= 0:
        return None
    return round(max(0.0, (1 - uptime_seconds / window_seconds) * 100), 1)


@dataclass
class PollTickInstanceResult:

    name: str
    outcome: Literal[
        "no_schedule", "disabled", "not_due", "already_transitioning", "already_fired_today",
        "already_handled", "already_in_desired_state",
        "fired_success", "fired_noop", "fired_failure", "error", "auto_revert_window_reopened",
        "waiting_on_dependency", "waiting_on_dependents", "manual_only",


        "in_progress", "retry_backoff", "dispatched",

        "extended",

        "holiday",
    ]
    action: Literal["create", "delete"] | None = None
    detail: str | None = None
    via_group: str | None = None


    via_auto_revert: bool = False


    started_at: datetime | None = None
    finished_at: datetime | None = None
    worker: str | None = None

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


@dataclass
class PollTickResult:

    results: list[PollTickInstanceResult] = field(default_factory=list)

    @property
    def fired_count(self) -> int:
        return sum(1 for r in self.results if r.outcome == "fired_success")

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if r.outcome in ("fired_failure", "error"))

    @property
    def waiting_count(self) -> int:
        return sum(1 for r in self.results
                   if r.outcome in ("waiting_on_dependency", "waiting_on_dependents"))

    def _timed(self) -> list[PollTickInstanceResult]:
        return [r for r in self.results if r.started_at is not None and r.finished_at is not None]

    @property
    def peak_concurrency(self) -> int:

        edges: list[tuple[datetime, int]] = []
        for r in self._timed():
            if r.started_at is not None and r.finished_at is not None:
                edges.append((r.started_at, 1))
                edges.append((r.finished_at, -1))
        peak = current = 0
        for _, delta in sorted(edges, key=lambda e: (e[0], e[1])):
            current += delta
            peak = max(peak, current)
        return peak

    @property
    def jobs_window(self) -> tuple[datetime, datetime] | None:
        starts = [r.started_at for r in self._timed() if r.started_at is not None]
        finishes = [r.finished_at for r in self._timed() if r.finished_at is not None]
        if not starts or not finishes:
            return None
        return min(starts), max(finishes)


_POLL_NOOP_START_OUTCOMES = frozenset({"already_running"})
_POLL_NOOP_STOP_OUTCOMES = frozenset({"already_stopped", "aborted_by_user"})


POST_START_SETTLE_MARGIN_S = 120


def _group_dependency_map() -> dict[int, set[int]]:

    def _do():
        conn = _connect()
        try:
            dep_map: dict[int, set[int]] = {}
            for gid, dep in conn.execute(
                "SELECT group_id, depends_on_group_id FROM group_dependencies"
            ).fetchall():
                dep_map.setdefault(gid, set()).add(dep)
            return dep_map
        finally:
            conn.close()

    return _retry_db(_do)


def _latest_start_and_post_start(name: str) -> tuple[datetime | None, tuple[str, datetime] | None]:

    def _do():
        conn = _connect()
        try:
            started = conn.execute(
                "SELECT MAX(timestamp) FROM schedule_events WHERE instance_name = ?"
                " AND action = 'create' AND result = 'success'", (name,)
            ).fetchone()[0]
            hook = conn.execute(
                "SELECT result, timestamp FROM hook_events WHERE instance_name = ?"
                " AND hook = 'post_start' ORDER BY timestamp DESC, id DESC LIMIT 1", (name,)
            ).fetchone()
            return started, hook
        finally:
            conn.close()

    started, hook = _retry_db(_do)
    return (
        datetime.fromisoformat(started) if started else None,
        (hook[0], datetime.fromisoformat(hook[1])) if hook else None,
    )


def _member_ready(name: str, record: dict, now: datetime) -> str | None:

    status = record.get("current_status")
    if status != "running":
        return f"'{name}' is {status}"
    if record.get("transitioning"):
        return f"'{name}' is mid-transition"
    hook = resolve_effective_hooks(name, record).get("post_start")
    if hook is None:
        return None
    started, last_check = _latest_start_and_post_start(name)
    if started is None:
        return None
    if last_check is not None and last_check[1] >= started:
        if last_check[0] in ("success", "skipped"):
            return None
        return f"'{name}' post-start check failed"
    timeout = int(hook.get("timeout_s") or 0)
    if (now - started).total_seconds() <= timeout + POST_START_SETTLE_MARGIN_S:
        return f"'{name}' post-start check still running"
    return None


def dependency_wait_reason(record: dict, registry: dict, dep_map: dict[int, set[int]],
                           now: datetime) -> str | None:

    targets = dep_map.get(record.get("group_id"))
    if not targets:
        return None
    labelled = []
    for target in targets:
        target_group = _group_row_by_id(target)
        labelled.append((target_group["name"] if target_group else str(target), target))
    for label, target in sorted(labelled):
        for member, member_record in sorted(registry.items()):
            if member_record.get("group_id") != target:
                continue
            reason = _member_ready(member, member_record, now)
            if reason is not None:
                return f"waiting for group '{label}': {reason}"
    return None


def dependents_wait_reason(record: dict, registry: dict,
                           dep_map: dict[int, set[int]]) -> str | None:

    group_id = record.get("group_id")
    if group_id is None:
        return None
    dependents = {gid for gid, targets in dep_map.items() if group_id in targets}
    if not dependents:
        return None
    for member, member_record in sorted(registry.items()):
        if member_record.get("group_id") in dependents and member_record.get(
            "current_status"
        ) not in ("stopped", "needs_manual_recovery"):
            dep_group = _group_row_by_id(member_record["group_id"])
            label = f"group '{dep_group['name']}'" if dep_group else "a dependent group"
            return (f"waiting for {label} to stop first: '{member}' is "
                    f"{member_record.get('current_status')}")
    return None


def _dependency_reason(name: str, action: Literal["start", "stop"]) -> str | None:

    registry = load_registry()
    record = registry.get(name)
    if record is None:
        return None
    if record.get("current_status") == ("running" if action == "start" else "stopped"):
        return None
    dep_map = _group_dependency_map()
    if action == "start":
        return dependency_wait_reason(record, registry, dep_map, datetime.now(UTC))
    return dependents_wait_reason(record, registry, dep_map)


def _warn_about_dependency(name: str, action: Literal["start", "stop"],
                           on_warning: Callable[[str], None] | None) -> None:

    if on_warning is None:
        return
    try:
        reason = _dependency_reason(name, action)
    except Exception:
        return
    if reason is not None:
        verb = "starting" if action == "start" else "stopping"
        on_warning(f"WARNING: {verb} '{name}' anyway, though its group dependency isn't "
                   f"satisfied ({reason}).")


def require_dependency(name: str, action: Literal["start", "stop"]) -> None:

    reason = _dependency_reason(name, action)
    if reason is not None:
        raise DependencyNotSatisfiedError(
            f"not {'starting' if action == 'start' else 'stopping'} '{name}': its group "
            f"dependency isn't satisfied ({reason}). Retry later, or leave out "
            "--respect-dependencies to act anyway."
        )


_START_OK_OUTCOMES = ("started", "already_running")
_STOP_OK_OUTCOMES = ("stopped", "already_stopped", "confirmed_gone_reset_to_stopped")


@dataclass
class GroupMemberResult:
    name: str
    group: str
    outcome: str
    ok: bool
    detail: str | None = None
    security_warning: bool = False


@dataclass
class GroupActionResult:
    group: str
    action: Literal["start", "stop"]
    stages: list[str]
    members: list[GroupMemberResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(m.ok for m in self.members)


def _group_chain(group_name: str, action: Literal["start", "stop"]) -> list[str]:

    groups = {g["name"]: g for g in list_schedule_groups()}
    if group_name not in groups:
        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    if action == "start":
        edges = {name: list(g.get("depends_on") or []) for name, g in groups.items()}
    else:
        edges = {}
        for g in groups.values():
            for dep in g.get("depends_on") or []:
                edges.setdefault(dep, []).append(g["name"])
    order: list[str] = []
    visiting: set[str] = set()

    def _visit(name: str) -> None:
        if name in order or name in visiting:
            return
        visiting.add(name)
        for nxt in sorted(edges.get(name, [])):
            if nxt in groups:
                _visit(nxt)
        visiting.discard(name)
        order.append(name)

    _visit(group_name)
    return order


def group_action(
    client, group_name: str, action: Literal["start", "stop"], ssh_key: str, *,
    include_dependencies: bool = False,
    respect_dependencies: bool = False,
    triggered_by: Literal["schedule", "manual", "api"] = "manual",
    actor: str | None = None,
    skip_hooks: bool = False,
    max_parallel: int = DEFAULT_GROUP_ACTION_MAX_PARALLEL,
    client_factory: Callable[[], object] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
    on_member_done: Callable[[GroupMemberResult], None] | None = None,
) -> GroupActionResult:

    if not 1 <= max_parallel <= MAX_POLL_MAX_PARALLEL:
        raise engine.ConfigError(f"max_parallel must be between 1 and {MAX_POLL_MAX_PARALLEL}.")
    stages = _group_chain(group_name, action) if include_dependencies else [group_name]
    if not include_dependencies and get_schedule_group(group_name) is None:
        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    result = GroupActionResult(group=group_name, action=action, stages=stages)
    if respect_dependencies and not include_dependencies:
        for member in get_schedule_group(group_name)["members"]:
            require_dependency(member, action)
    ok_outcomes = _START_OK_OUTCOMES if action == "start" else _STOP_OK_OUTCOMES

    def _one(member: str, stage: str) -> GroupMemberResult:
        worker_client = client if client_factory is None else client_factory()
        try:
            if action == "start":
                r: StartResult | StopResult = start_instance(
                    worker_client, member, ssh_key, triggered_by=triggered_by, actor=actor,
                    on_progress=on_progress, on_warning=on_warning, skip_hooks=skip_hooks,
                )
            else:
                r = stop_instance(
                    worker_client, member, ssh_key, triggered_by=triggered_by, actor=actor,
                    on_progress=on_progress, on_warning=on_warning, skip_hooks=skip_hooks,
                )
        except Exception as e:
            done = GroupMemberResult(member, stage, "error", False, detail=str(e))
        else:
            done = GroupMemberResult(
                member, stage, r.outcome, r.outcome in ok_outcomes, detail=r.detail,
                security_warning=getattr(r, "security_warning", False),
            )
        if on_member_done is not None:
            on_member_done(done)
        return done

    for index, stage in enumerate(stages):
        group = get_schedule_group(stage)
        members = group["members"] if group else []
        if on_progress is not None:
            on_progress(f"{'Starting' if action == 'start' else 'Stopping'} group '{stage}' "
                        f"({len(members)} member(s))...")
        if len(members) <= 1 or max_parallel <= 1:
            stage_results = [_one(m, stage) for m in members]
        else:
            with ThreadPoolExecutor(max_workers=min(max_parallel, len(members))) as pool:
                stage_results = list(pool.map(lambda m, st=stage: _one(m, st), members))
        result.members.extend(stage_results)
        if index == len(stages) - 1:
            break
        blocker = next((m for m in stage_results if not m.ok), None)
        if blocker is None and action == "start":


            registry = load_registry()
            not_ready = [m for m in members
                         if _member_ready(m, registry.get(m, {}), datetime.now(UTC)) is not None]
            if not_ready:
                blocker = GroupMemberResult(not_ready[0], stage, "not_ready", False)
        if blocker is not None:
            reason = (f"group '{stage}' didn't fully {action}: '{blocker.name}' "
                      f"{blocker.outcome}")
            if on_warning is not None:
                on_warning(f"WARNING: {reason} -- not continuing to the next group.")
            for later in stages[index + 1:]:
                later_group = get_schedule_group(later)
                for m in (later_group["members"] if later_group else []):
                    result.members.append(GroupMemberResult(m, later, "skipped", False, detail=reason))
            break
    return result


_POLL_WAITING_OUTCOMES = frozenset({"waiting_on_dependency", "waiting_on_dependents"})
MAX_POLL_DEPENDENCY_ROUNDS = 20


def poll_tick(
    client, ssh_key: str, *,
    window_seconds: int = DEFAULT_POLL_WINDOW_SECONDS,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
    max_parallel: int = 1,
    client_factory: Callable[[], object] | None = None,
    in_flight: Collection[str] | None = None,
    hold_until: dict[str, datetime] | None = None,
    submit: Callable[[str, Callable[[], PollTickInstanceResult]], None] | None = None,
) -> PollTickResult:

    in_flight = set(in_flight or ())
    hold_until = hold_until or {}
    results: list[PollTickInstanceResult | None] = []
    jobs: list[tuple[int, str, Callable[[object], PollTickInstanceResult], str | None, str | None, bool]] = []
    tick_now = datetime.now(UTC)


    ready_now = tick_now
    final_results: list[PollTickInstanceResult] = []
    round_number = 1

    def _queue(job_name: str, fn: Callable[[object], PollTickInstanceResult], *,
               job_action: str | None, job_via_group: str | None, job_via_revert: bool) -> None:
        results.append(None)
        jobs.append((len(results) - 1, job_name, fn, job_action, job_via_group, job_via_revert))

    registry = load_registry()
    dep_map = _group_dependency_map()
    schedule_modes = get_schedule_modes()
    holiday_set = _holiday_set()
    round_names: list[str] = list(registry)
    while True:
        for name in round_names:
            record = registry.get(name)
            if record is None:
                continue
            action: Literal["create", "delete"] | None = None
            via_group: str | None = None
            if schedule_modes.get(name) == "manual":

                results.append(PollTickInstanceResult(name, "manual_only"))
                continue
            if name in in_flight:
                results.append(PollTickInstanceResult(name, "in_progress"))
                continue
            held = hold_until.get(name)
            if held is not None and tick_now < held:
                results.append(PollTickInstanceResult(
                    name, "retry_backoff", detail=f"retrying after {held.isoformat()}"))
                continue


            via_auto_revert = False
            try:
                expires_at_raw = record.get("manual_override_expires_at")
                is_due_for_revert = False
                if record.get("current_status") == "running" and expires_at_raw:


                    action = "delete"
                    via_auto_revert = True
                    is_due_for_revert = tick_now >= datetime.fromisoformat(expires_at_raw)
                    if not is_due_for_revert:
                        action = None
                        via_auto_revert = False
                if is_due_for_revert:
                    if record.get("transitioning"):


                        results.append(PollTickInstanceResult(
                            name, "already_transitioning", action="delete", via_auto_revert=True,
                        ))
                        continue


                    effective_schedule, _via_group_for_revert = resolve_effective_schedule(name, record)
                    if schedule_is_active(effective_schedule) and is_within_scheduled_on_window(
                        effective_schedule, tick_now,
                    ):
                        timer_cleared = False
                        with _instance_lock(name):
                            fresh = load_registry().get(name)
                            if fresh is not None and fresh.get("manual_override_expires_at") == expires_at_raw:
                                fresh["manual_override_expires_at"] = None
                                _save_one_record(name, fresh)
                                timer_cleared = True


                        if timer_cleared:
                            if on_progress is not None:
                                on_progress(
                                    f"'{name}': manual override expiry reached, but the instance is now "
                                    "inside its own scheduled on-window -- clearing the timer instead of "
                                    "stopping."
                                )
                            results.append(PollTickInstanceResult(
                                name, "auto_revert_window_reopened", via_auto_revert=True,
                            ))
                        else:


                            results.append(PollTickInstanceResult(
                                name, "not_due", via_auto_revert=True,
                                detail="manual override expiry was concurrently changed; skipped "
                                "this tick.",
                            ))
                        continue


                    with _instance_lock(name):
                        fresh_for_revert = load_registry().get(name)
                        still_due = (
                            fresh_for_revert is not None
                            and fresh_for_revert.get("manual_override_expires_at") == expires_at_raw
                        )
                    if not still_due:
                        results.append(PollTickInstanceResult(
                            name, "not_due", via_auto_revert=True,
                            detail="manual override expiry was concurrently changed; skipped "
                            "this tick.",
                        ))
                        continue
                    wait = dependents_wait_reason(record, registry, dep_map)
                    if wait is not None:
                        results.append(PollTickInstanceResult(
                            name, "waiting_on_dependents", action="delete", via_auto_revert=True,
                            detail=wait,
                        ))
                        continue
                    if on_progress is not None:
                        on_progress(f"'{name}': manual override expired, auto-reverting (stop)...")

                    def _revert(worker_client, name=name) -> PollTickInstanceResult:
                        revert_result = stop_instance(
                            worker_client, name, ssh_key, triggered_by="schedule",
                            on_progress=on_progress, on_warning=on_warning,
                        )
                        if _STOP_EVENT_RESULTS.get(revert_result.outcome) == "success":
                            return PollTickInstanceResult(
                                name, "fired_success", action="delete", via_auto_revert=True,
                            )
                        return PollTickInstanceResult(
                            name, "fired_failure", action="delete",
                            detail=f"outcome={revert_result.outcome}", via_auto_revert=True,
                        )
                    _queue(name, _revert, job_action="delete", job_via_group=None, job_via_revert=True)
                    continue

                schedule, via_group = resolve_effective_schedule(name, record)
                if schedule is None:
                    results.append(PollTickInstanceResult(name, "no_schedule"))
                    continue
                if not schedule.get("enabled", True):
                    results.append(PollTickInstanceResult(name, "disabled", via_group=via_group))
                    continue
                due = resolve_due_boundary(schedule, tick_now, window_seconds)
                if due is None:
                    results.append(PollTickInstanceResult(name, "not_due", via_group=via_group))
                    continue
                action, boundary = due
                if record.get("transitioning"):


                    results.append(PollTickInstanceResult(
                        name, "already_transitioning", action=action, via_group=via_group,
                    ))
                    continue
                if transition_recorded_since(name, boundary):


                    results.append(PollTickInstanceResult(
                        name, "already_handled", action=action, via_group=via_group,
                    ))
                    continue
                if action == "create":
                    local_date = boundary.astimezone(ZoneInfo(schedule["timezone"])).date()
                    holiday = holiday_for(name, record, local_date, holiday_set)
                    if holiday is not None:
                        results.append(PollTickInstanceResult(
                            name, "holiday", action=action, via_group=via_group,
                            detail=f"{local_date} is a holiday for {holiday}; scheduled start skipped",
                        ))
                        continue
                extended_until = record.get("manual_override_expires_at")
                if (action == "delete" and extended_until
                        and tick_now < datetime.fromisoformat(extended_until)):


                    results.append(PollTickInstanceResult(
                        name, "extended", action=action, via_group=via_group,
                        detail=f"extended until {extended_until}",
                    ))
                    continue
                desired = "running" if action == "create" else "stopped"
                if record.get("current_status") == desired:


                    results.append(PollTickInstanceResult(
                        name, "already_in_desired_state", action=action, via_group=via_group,
                    ))
                    continue


                wait = (dependency_wait_reason(record, registry, dep_map, ready_now)
                        if action == "create" else dependents_wait_reason(record, registry, dep_map))
                if wait is not None:
                    results.append(PollTickInstanceResult(
                        name, "waiting_on_dependency" if action == "create" else "waiting_on_dependents",
                        action=action, via_group=via_group, detail=wait,
                    ))
                    continue

                if on_progress is not None:
                    via = f" via group '{via_group}'" if via_group else ""
                    on_progress(f"'{name}': {action} due{via}, firing (triggered_by=schedule)...")

                def _fire(
                    worker_client, name=name, action=action, via_group=via_group,
                ) -> PollTickInstanceResult:
                    op_result: StartResult | StopResult
                    if action == "create":
                        op_result = start_instance(
                            worker_client, name, ssh_key, triggered_by="schedule",
                            on_progress=on_progress, on_warning=on_warning,
                        )
                        event_result = _START_EVENT_RESULTS.get(op_result.outcome)
                        is_noop = op_result.outcome in _POLL_NOOP_START_OUTCOMES
                    else:
                        op_result = stop_instance(
                            worker_client, name, ssh_key, triggered_by="schedule",
                            on_progress=on_progress, on_warning=on_warning,
                        )
                        event_result = _STOP_EVENT_RESULTS.get(op_result.outcome)
                        is_noop = op_result.outcome in _POLL_NOOP_STOP_OUTCOMES


                    if event_result == "success":
                        return PollTickInstanceResult(
                            name, "fired_success", action=action, via_group=via_group,
                        )
                    if is_noop:
                        return PollTickInstanceResult(
                            name, "fired_noop", action=action, detail=f"outcome={op_result.outcome}",
                            via_group=via_group,
                        )
                    return PollTickInstanceResult(
                        name, "fired_failure", action=action, detail=f"outcome={op_result.outcome}",
                        via_group=via_group,
                    )
                _queue(name, _fire, job_action=action, job_via_group=via_group, job_via_revert=False)
            except Exception as e:


                results.append(PollTickInstanceResult(
                    name, "error", action=action, detail=str(e), via_group=via_group,
                    via_auto_revert=via_auto_revert,
                ))

        def _run(job) -> tuple[int, PollTickInstanceResult]:
            index, job_name, fn, job_action, job_via_group, job_via_revert = job
            started = datetime.now(UTC)
            try:
                worker_client = client if client_factory is None else client_factory()
                result = fn(worker_client)
            except Exception as e:
                result = PollTickInstanceResult(
                    job_name, "error", action=job_action, detail=str(e), via_group=job_via_group,
                    via_auto_revert=job_via_revert,
                )
            result.started_at, result.finished_at = started, datetime.now(UTC)
            result.worker = threading.current_thread().name
            return index, result

        def _run_result(job) -> PollTickInstanceResult:
            return _run(job)[1]

        if submit is not None:
            for job in jobs:
                index, job_name, _fn, job_action, job_via_group, job_via_revert = job
                submit(job_name, functools.partial(_run_result, job))
                results[index] = PollTickInstanceResult(
                    job_name, "dispatched", action=job_action,
                    via_group=job_via_group, via_auto_revert=job_via_revert,
                )
            return PollTickResult(results=[r for r in results if r is not None])
        if max_parallel <= 1 or len(jobs) <= 1:
            outcomes = [_run(job) for job in jobs]
        else:
            with ThreadPoolExecutor(max_workers=min(max_parallel, len(jobs))) as pool:
                outcomes = list(pool.map(_run, jobs))
        for index, result in outcomes:
            results[index] = result
        round_results = [r for r in results if r is not None]
        waiting_names = [r.name for r in round_results if r.outcome in _POLL_WAITING_OUTCOMES]
        progressed = any(r.outcome == "fired_success" for r in round_results)


        if waiting_names and progressed and round_number < MAX_POLL_DEPENDENCY_ROUNDS:
            final_results.extend(r for r in round_results if r.outcome not in _POLL_WAITING_OUTCOMES)
            if on_progress is not None:
                on_progress(f"Re-checking {len(waiting_names)} instance(s) waiting on a dependency, "
                            "now that this round's starts/stops have finished...")
            results.clear()
            jobs.clear()
            round_names = waiting_names
            round_number += 1
            ready_now = datetime.now(UTC)
            registry = load_registry()
            dep_map = _group_dependency_map()
            schedule_modes = get_schedule_modes()
            continue
        final_results.extend(round_results)
        return PollTickResult(results=final_results)


def _per_thread_client_factory() -> Callable[[], object]:

    local = threading.local()
    token = engine.load_token()

    def _client():
        existing = getattr(local, "client", None)
        if existing is None:
            existing = local.client = engine.build_client(token)
        return existing

    return _client


class PollScheduler:


    def __init__(
        self, client, ssh_key: str, *, window_seconds: int, max_parallel: int,
        client_factory: Callable[[], object] | None = None,
        on_progress: Callable[[str], None] | None = None,
        on_warning: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._ssh_key = ssh_key
        self._window_seconds = window_seconds
        self._max_parallel = max_parallel
        self._client_factory = client_factory
        self._on_progress = on_progress
        self._on_warning = on_warning
        self._executor = ThreadPoolExecutor(max_workers=max_parallel,
                                            thread_name_prefix="poll-worker")
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._completed: list[PollTickInstanceResult] = []
        self._failures: dict[str, tuple[int, datetime]] = {}
        self.hold_until: dict[str, datetime] = {}
        self.wake = threading.Event()


        self._maintenance = ThreadPoolExecutor(max_workers=1, thread_name_prefix="poll-maintenance")
        self._maintenance_future: Future | None = None
        self._last_reconcile: datetime | None = None
        self._last_key_protect: datetime | None = None
        self._last_verify: datetime | None = None
        self._started_at = datetime.now(UTC)
        self._lease_checked_at: datetime | None = None
        self._last_ping: datetime | None = None
        self._last_ping_ok: bool | None = None
        self._lease_active = True
        self._lease_reported: str | None = None
        self._mismatches_reported: frozenset[str] | None = None

    @property
    def in_flight_count(self) -> int:
        with self._lock:
            return len(self._in_flight)

    def _submit(self, name: str, fn: Callable[[], PollTickInstanceResult]) -> None:
        with self._lock:
            self._in_flight.add(name)
        future = self._executor.submit(fn)
        future.add_done_callback(functools.partial(self._done, name))

    def _done(self, name: str, future) -> None:
        try:
            result = future.result()
        except BaseException as e:
            result = PollTickInstanceResult(name, "error", detail=f"job did not run: {e!r}")
        with self._lock:
            self._in_flight.discard(name)
            self._completed.append(result)
        self.wake.set()

    def _note_outcome(self, result: PollTickInstanceResult, now: datetime) -> None:
        if result.outcome in ("fired_failure", "error"):
            count, last = self._failures.get(result.name, (0, now))
            if (now - last).total_seconds() > self._window_seconds:
                count = 0
            count += 1
            self._failures[result.name] = (count, now)
            delay = POLL_RETRY_BACKOFF_SECONDS[min(count, len(POLL_RETRY_BACKOFF_SECONDS)) - 1]
            self.hold_until[result.name] = now + timedelta(seconds=delay)
            note = f"retrying in {delay}s"
            result.detail = f"{result.detail}; {note}" if result.detail else note
        elif result.outcome in ("fired_success", "fired_noop"):
            self._failures.pop(result.name, None)
            self.hold_until.pop(result.name, None)

    def take_completed(self) -> list[PollTickInstanceResult]:

        with self._lock:
            completed, self._completed = self._completed, []
        now = datetime.now(UTC)
        for result in completed:
            self._note_outcome(result, now)
        return completed

    def check(self) -> PollTickResult:

        self.wake.clear()
        with contextlib_suppress(Exception):
            if reload_object_storage_settings_if_changed() and self._on_progress is not None:
                self._on_progress("Object Storage backup settings changed in .env -- reloaded.")
        completed = self.take_completed()
        if not self._lease_held(datetime.now(UTC)):

            return PollTickResult(results=completed)
        with self._lock:
            in_flight = set(self._in_flight)
        tick = poll_tick(
            self._client, self._ssh_key, window_seconds=self._window_seconds,
            max_parallel=self._max_parallel, client_factory=self._client_factory,
            on_progress=self._on_progress, on_warning=self._on_warning,
            in_flight=in_flight, hold_until=dict(self.hold_until), submit=self._submit,
        )
        now = datetime.now(UTC)
        for result in tick.results:
            if result.outcome == "error":
                self._note_outcome(result, now)
        self._start_maintenance(now)
        return PollTickResult(results=completed + tick.results)

    def _lease_held(self, now: datetime) -> bool:

        last = self._lease_checked_at
        if last is not None and (now - last).total_seconds() < LEASE_RENEW_SECONDS:
            return self._lease_active
        self._lease_checked_at = now
        status = acquire_or_renew_lease(now=now)
        if status.active != self._lease_active or self._lease_reported is None:
            self._lease_reported = status.role
            if status.active:
                message = (f"scheduler active ({status.detail})" if status.detail
                           else "scheduler active (holds the lease)")
                record_activity(message, source="scheduler", action="lease")
                if self._on_progress is not None:
                    self._on_progress(message)
            else:
                holder = status.holder or {}
                message = (f"STANDING BY: another scheduler ({holder.get('host')}) holds the lease "
                           f"-- nothing is started or stopped here. If that host is gone for good, "
                           "run `scheduler-lease --take-over` here.")
                record_activity(message, level="error", source="scheduler", action="lease")
                if self._on_warning is not None:
                    self._on_warning(f"WARNING: {message}")
        self._lease_active = status.active
        return status.active

    def _start_maintenance(self, now: datetime) -> None:
        if self._maintenance_future is not None and not self._maintenance_future.done():
            return
        self._maintenance_future = self._maintenance.submit(self.run_maintenance, now)

    def _ping_heartbeat(self, now: datetime) -> None:
        url = os.environ.get(HEARTBEAT_URL_ENV, "").strip()
        if not url or (self._last_ping is not None
                       and (now - self._last_ping).total_seconds() < HEARTBEAT_INTERVAL_SECONDS):
            return
        self._last_ping = now
        ok = False
        with contextlib_suppress(Exception):
            import requests as _requests
            ok = _requests.get(url, timeout=10).status_code < 400
        if ok != self._last_ping_ok:
            self._last_ping_ok = ok
            record_activity(f"heartbeat ping to {HEARTBEAT_URL_ENV} {'works' if ok else 'is failing'}",
                            level="info" if ok else "warning", source="scheduler", action="heartbeat")

    def run_maintenance(self, now: datetime) -> None:

        with contextlib_suppress(Exception):
            self._ping_heartbeat(now)
        with contextlib_suppress(Exception):
            run_continuous_backup_if_due(now=now)
        with contextlib_suppress(Exception):
            backup_env_encrypted(verify_remote=self._last_key_protect is None
                                 or (now - self._last_key_protect).total_seconds() >= 3600)
        if (self._last_reconcile is None
                or (now - self._last_reconcile).total_seconds() >= RECONCILE_INTERVAL_SECONDS):
            self._last_reconcile = now
            with self._lock:
                busy = set(self._in_flight)
            with contextlib_suppress(Exception):
                names = [n for n in load_registry() if n not in busy]
                reconcile_fleet(self._client, names=names, on_progress=self._on_progress,
                                on_warning=self._on_warning)
            with contextlib_suppress(Exception):
                self._report_schedule_mismatches(now)
            with contextlib_suppress(Exception):
                process_due_deletions(self._client, now=now, on_warning=self._on_warning)
            with contextlib_suppress(Exception):
                if key_rotation_state() is not None:
                    continue_key_rotation(self._client, on_warning=self._on_warning)
            with contextlib_suppress(Exception):
                evaluate_alerts(now=now, include_scheduler=False, client=self._client)
        if ((self._last_verify is None and (now - self._started_at).total_seconds() >= 600)
                or (self._last_verify is not None and (now - self._last_verify).total_seconds()
                    >= BACKUP_VERIFY_INTERVAL_SECONDS)):
            self._last_verify = now
            with contextlib_suppress(Exception):
                if osb.is_configured():
                    verify = verify_backups(now=now)
                    record_activity(
                        f"backup restore test: {'passed' if verify.ok else 'FAILED'} "
                        f"({verify.snapshot_key}){'' if verify.ok else ': ' + '; '.join(verify.problems)}",
                        level="info" if verify.ok else "error", source="scheduler",
                        action="backup-verify")
        if (self._last_key_protect is None
                or (now - self._last_key_protect).total_seconds() >= 3600):
            self._last_key_protect = now
            with contextlib_suppress(Exception):
                backup_release_copy()
            with contextlib_suppress(Exception):
                days = drill_interval_days()
                last = recovery_drill_status().get("started_at")
                if days and (last is None or (now - datetime.fromisoformat(last)).days >= days):
                    run_recovery_drill(self._client)
            with contextlib_suppress(Exception):
                key = protect_deployment_ssh_key(self._client, deployment_ssh_key_path())
                for problem in key.problems:
                    record_activity(f"deployment SSH key: {problem}", level="warning",
                                    source="scheduler", action="ssh-key")

    def _report_schedule_mismatches(self, now: datetime) -> None:

        mismatches = find_schedule_mismatches(now=now)
        names = frozenset(m.name for m in mismatches)
        if names == self._mismatches_reported:
            return
        self._mismatches_reported = names
        if not mismatches:
            return
        listing = ", ".join(f"{m.name} ({m.current} -> {m.desired})" for m in mismatches)
        message = (f"{len(mismatches)} node(s) aren't in their scheduled state: {listing}. Review "
                   "and apply with `schedule-catch-up` or the dashboard's Instances page.")
        record_activity(message, level="warning", source="scheduler", action="schedule-check")
        if self._on_warning is not None:
            self._on_warning(f"WARNING: {message}")

    def wait(self, timeout: float) -> None:

        self.wake.wait(timeout)

    def shutdown(self) -> None:

        self._executor.shutdown(wait=True, cancel_futures=True)
        self._maintenance.shutdown(wait=True, cancel_futures=True)
        if self._lease_active:
            release_lease()


def _hms(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%H:%M:%S.%f")[:-3]


def cmd_poll(client, args) -> int:


    last_waiting: dict[str, tuple[str, str | None]] = {}

    def _report_tick(tick: PollTickResult, *, continuous: bool = False) -> None:
        for r in tick.results:
            if r.outcome not in ("waiting_on_dependency", "waiting_on_dependents", "in_progress",
                                 "extended", "holiday"):
                last_waiting.pop(r.name, None)
            if r.outcome not in (
                "fired_success", "fired_failure", "error", "auto_revert_window_reopened",
                "waiting_on_dependency", "waiting_on_dependents", "extended", "holiday",
            ):
                continue
            if r.outcome.startswith("waiting") or r.outcome in ("extended", "holiday"):
                if continuous and last_waiting.get(r.name) == (r.outcome, r.detail):
                    continue
                last_waiting[r.name] = (r.outcome, r.detail)
            detail = f" ({r.detail})" if r.detail else ""
            via = f" via_group={r.via_group}" if r.via_group else ""
            revert = " auto_revert=true" if r.via_auto_revert else ""
            timing = ""
            if r.started_at is not None and r.finished_at is not None:
                timing = (f" started={_hms(r.started_at)} finished={_hms(r.finished_at)} "
                          f"took={r.duration_s:.1f}s worker={r.worker}")
            print(f"  {r.name}: {r.outcome} action={r.action}{via}{revert}{timing}{detail}")
            record_activity(
                f"scheduler: {r.outcome} action={r.action}{via}{revert}{timing}{detail}",
                level="error" if r.outcome in ("fired_failure", "error") else (
                    "warning" if r.outcome.startswith("waiting") else "info"),
                source="scheduler", instance_name=r.name, action="tick",
            )
        write_scheduler_heartbeat(
            interval_seconds=None if args.once else args.interval_seconds,
            instances_checked=len(tick.results), fired=tick.fired_count, failed=tick.failed_count,
        )
        if continuous:
            dispatched = sum(1 for r in tick.results if r.outcome == "dispatched")
            finished = sum(1 for r in tick.results if r.started_at is not None)
            if dispatched or finished:
                waiting = (f", {tick.waiting_count} waiting on a dependency"
                           if tick.waiting_count else "")
                print(f"Check: {dispatched} started now, {finished} finished ({tick.fired_count} "
                      f"succeeded, {tick.failed_count} failed){waiting}.")
            return
        if tick.results:
            waiting = f", {tick.waiting_count} waiting on a dependency" if tick.waiting_count else ""
            print(f"Tick complete: {tick.fired_count} fired, {tick.failed_count} failed{waiting}, "
                  f"{len(tick.results)} instance(s) checked.")
            window = tick.jobs_window
            if window is not None:
                print(f"  Jobs ran {_hms(window[0])} -> {_hms(window[1])} "
                      f"({(window[1] - window[0]).total_seconds():.1f}s), "
                      f"peak concurrency {tick.peak_concurrency} of max {args.max_parallel}.")

    max_parallel = getattr(args, "max_parallel", 1)
    if isinstance(max_parallel, bool) or not isinstance(max_parallel, int):
        max_parallel = 1
    if not 1 <= max_parallel <= MAX_POLL_MAX_PARALLEL:
        print(f"Configuration error: --max-parallel must be from 1 to {MAX_POLL_MAX_PARALLEL}.",
              file=sys.stderr)
        return 1
    args.max_parallel = max_parallel
    client_factory = _per_thread_client_factory() if max_parallel > 1 else None
    if args.once:
        lease = acquire_or_renew_lease()
        if not lease.active:
            holder = (lease.holder or {}).get("host")
            print(f"Standing by: another scheduler ({holder}) holds the lease -- nothing done. "
                  "Run `scheduler-lease --take-over` here only if that host is gone for good.",
                  file=sys.stderr)
            return 1
        tick = poll_tick(
            client, args.ssh_key, window_seconds=args.window_seconds,
            max_parallel=args.max_parallel, client_factory=client_factory,
            on_progress=print, on_warning=_print_to_stderr,
        )
        _report_tick(tick)
        return 1 if tick.failed_count else 0

    if args.interval_seconds < 1:
        print("Configuration error: --interval-seconds must be at least 1.", file=sys.stderr)
        return 1


    if args.window_seconds < args.interval_seconds:
        print(
            f"Configuration error: --window-seconds ({args.window_seconds}) is smaller than "
            f"--interval-seconds ({args.interval_seconds}) -- a real gap would exist between "
            "checks where a scheduled action could be silently skipped for an entire day. Raise "
            "--window-seconds to at least --interval-seconds, or lower --interval-seconds.",
            file=sys.stderr,
        )
        return 1

    scheduler = PollScheduler(
        client, args.ssh_key, window_seconds=args.window_seconds, max_parallel=args.max_parallel,
        client_factory=client_factory, on_progress=print, on_warning=_print_to_stderr,
    )
    stopping = threading.Event()

    def _on_sigterm(_signum, _frame) -> None:
        stopping.set()
        scheduler.wake.set()

    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.signal(signal.SIGTERM, _on_sigterm)
    print(f"Checking schedules every {args.interval_seconds}s, and whenever a start/stop "
          f"finishes; up to {args.max_parallel} start/stop(s) at once (Ctrl-C to stop)...")
    record_activity(
        f"Scheduler started: checking every {args.interval_seconds}s, catch-up window "
        f"{args.window_seconds}s, up to {args.max_parallel} at once.", source="scheduler",
        action="tick",
    )
    try:
        while not stopping.is_set():
            _report_tick(scheduler.check(), continuous=True)
            if stopping.is_set():
                break
            scheduler.wait(args.interval_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        running = scheduler.in_flight_count
        if running:
            print(f"Stopping: waiting for {running} start/stop(s) already running to finish "
                  "(queued ones are not started)...")
        scheduler.shutdown()
        leftover = scheduler.take_completed()
        if leftover:
            _report_tick(PollTickResult(results=leftover), continuous=True)
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)
    record_activity("Scheduler stopped.", source="scheduler", action="tick")
    print("Stopped.")
    return 0


def _save_migration_backup(name: str, status: str, record: dict) -> None:
    now = datetime.now(UTC).isoformat()

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO migration_backups (name, status, record, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET status = excluded.status, "
                "record = excluded.record, updated_at = excluded.updated_at",
                (name, status, json.dumps(record), record.get("created_at") or now, now),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _delete_migration_backup_row(name: str) -> None:
    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM migration_backups WHERE name = ?", (name,))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def get_migration_backup(name: str) -> dict | None:

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT name, status, record, created_at, updated_at FROM migration_backups "
                "WHERE name = ?", (name,)).fetchone()
        finally:
            conn.close()

    row = _retry_db(_do)
    if row is None:
        return None
    return {"name": row[0], "status": row[1], "record": json.loads(row[2]),
            "created_at": row[3], "updated_at": row[4]}


def list_migration_backups() -> list[dict]:
    def _do():
        conn = _connect()
        try:
            return conn.execute("SELECT name FROM migration_backups ORDER BY name").fetchall()
        finally:
            conn.close()

    out = []
    for (name,) in _retry_db(_do):
        entry = get_migration_backup(name)
        if entry is not None:
            out.append(entry)
    return out


def migration_backup_summary(entry: dict) -> dict:

    record = entry["record"]
    orig = record.get("original", {})
    bk = record.get("backup", {})
    return {
        "name": entry["name"],
        "status": entry["status"],
        "created_at": entry["created_at"],
        "updated_at": entry["updated_at"],
        "original_instance_id": orig.get("instance_id"),
        "original_label": orig.get("label"),
        "region": orig.get("region"),
        "plan": orig.get("type"),
        "public_ipv4": orig.get("public_ipv4") or [],
        "backup_instance_id": bk.get("instance_id"),
        "backup_label": bk.get("label"),
        "backup_volumes": [{"slot": v.get("slot"), "volume_id": v.get("volume_id"),
                            "size": v.get("size"), "source_volume_id": v.get("source_volume_id")}
                           for v in bk.get("volumes") or []],
        "estimated_monthly_cost": record.get("estimated_monthly_cost"),
        "restored_at": record.get("restored_at"),
    }


def _sync_migration_backup_record(name: str, on_warning: Callable[[str], None] | None) -> None:

    if not osb.is_configured():
        return
    try:
        entry = get_migration_backup(name)
        if entry is None:
            osb.delete_migration_backup(name)
        else:
            osb.upload_migration_backup(name, {**entry["record"], "status": entry["status"]})
    except Exception as e:
        if on_warning is not None:
            on_warning(f"WARNING: could not mirror the backup record for '{name}' to Object "
                       f"Storage ({e}); it is still recorded locally and tagged on the backup.")


def restore_migration_backups_from_object_storage(
    *, on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> int:

    names = osb.list_migration_backups()
    if names is None:
        return 0
    added = 0
    for backup_name in names:
        if get_migration_backup(backup_name) is not None:
            continue
        stored = osb.download_migration_backup(backup_name)
        if not stored or stored.get("name") != backup_name or "backup" not in stored:
            if on_warning is not None:
                on_warning(f"WARNING: backup record '{backup_name}' in Object Storage is missing "
                           "or failed verification -- skipped.")
            continue
        status = stored.pop("status", "kept")
        _save_migration_backup(backup_name, status, stored)
        added += 1
        if on_progress is not None:
            on_progress(f"Restored the pre-migration backup record for '{backup_name}' "
                        f"({status}) from Object Storage.")
    return added


def untracked_backup_instances(client) -> list[tuple[str, int, str]]:

    out = []
    for inst in engine.retry_transient(lambda: list(client.linode.instances())):
        for tag in inst.tags or []:
            name = engine.migration_backup_name_from_tag(tag)
            if name is not None:
                entry = get_migration_backup(name)
                if entry is None or entry["record"].get("backup", {}).get("instance_id") != inst.id:
                    out.append((name, inst.id, inst.label))
    return out


def _strip_scheduler_tags_from_reserved_ip(client, address: str) -> None:
    ip = engine.retry_transient(lambda: client.load(engine.ReservedIPAddress, address))
    tags = list(ip.tags or [])
    kept = [t for t in tags if not t.startswith("linode-scheduler-")]
    if kept != tags:
        ip.tags = kept
        engine.retry_transient(ip.save)


def _restored_ssh_host(record: dict) -> str | None:
    orig = record["original"]
    public = orig.get("public_ipv4") or []
    has_public = any(
        (i.get("purpose") == "public") if orig["interface_generation"] == engine.INTERFACE_MODEL_LEGACY
        else bool(i.get("public")) for i in orig.get("network_config") or [])
    if public and (has_public or not orig.get("network_config")):
        return public[0]
    pairs = engine.vpc_interface_addresses(orig.get("network_config"), orig["interface_generation"])
    if pairs:
        return pairs[0][1]
    for iface in orig.get("network_config") or []:
        addr = iface.get("ipam_address") or ((iface.get("vlan") or {}).get("ipam_address"))
        if addr:
            return addr.split("/")[0]
    return public[0] if public else None


@dataclass
class RollbackResult:
    outcome: Literal["restored", "aborted_by_user"]
    backup_instance_id: int | None = None
    label: str | None = None
    public_ipv4: list[str] = field(default_factory=list)
    original_deleted_instance_id: int | None = None
    reachable: bool | None = None
    warnings: list[str] = field(default_factory=list)


@_logs_activity("rollback")
def rollback_to_backup(
    client, name: str, ssh_key: str | None, *, boot: bool = True,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> RollbackResult:

    warnings: list[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        if on_warning is not None:
            on_warning(msg)

    def say(msg: str) -> None:
        if on_progress is not None:
            on_progress(msg)

    with _instance_lock(name):
        entry = get_migration_backup(name)
        if entry is None:
            raise engine.ConfigError(f"no pre-migration backup is recorded for '{name}'.")
        if entry["status"] != "kept":
            raise engine.ConfigError(
                f"the backup for '{name}' is '{entry['status']}', not 'kept' -- "
                + ("it has already been restored." if entry["status"] == "restored"
                   else "it was never completed; `backup-delete` removes what exists."))
        record = entry["record"]
        orig = record["original"]
        backup_id = record["backup"].get("instance_id")
        if not backup_id:
            raise engine.ConfigError(f"the backup record for '{name}' has no backup instance.")
        managed = load_registry().get(name)
        if managed is not None:
            if managed.get("transitioning"):
                raise engine.ConfigError(f"'{name}' is mid start/stop; wait for it to finish.")
            if managed.get("current_status") in ("running", "unreachable") and managed.get(
                    "current_linode_id"):
                raise engine.ConfigError(
                    f"'{name}' is running as a managed instance. Stop it first (`stop --name "
                    f"{name}`, or `stop --name {name} --skip-precapture` if it can't be reached), "
                    "then roll back.")
        try:
            backup = engine.retry_transient(lambda: client.load(Instance, backup_id))
            _ = backup.status
        except ApiError as e:
            if e.status == 404:
                raise engine.ConfigError(
                    f"the backup instance {backup_id} no longer exists on Linode.") from e
            raise
        pairs = engine.vpc_interface_addresses(orig.get("network_config"),
                                               orig["interface_generation"])
        if pairs:
            live = engine.retry_transient(lambda: list(client.vpcs.ips()))
            allowed = {orig["instance_id"], backup_id}
            for subnet_id, address in pairs:
                holder = next((ip for ip in live if ip.subnet_id == subnet_id
                               and ip.address == address), None)
                if holder is not None and getattr(holder, "linode_id", None) not in allowed:
                    raise engine.ConfigError(
                        f"VPC address {address} (subnet {subnet_id}) is now used by instance "
                        f"{getattr(holder, 'linode_id', '?')}; free it, then roll back.")
        say(f"Rolling '{name}' back to its pre-migration backup (instance {backup_id}, taken "
            f"{record.get('created_at')}). The original's public address(es) "
            f"{', '.join(orig.get('public_ipv4') or []) or '(none)'} and VPC/VLAN addresses move "
            "to the backup and the scheduler stops managing this name. If the original instance "
            f"({orig['instance_id']}) still exists it is replaced: its volumes are detached and "
            "kept, the instance itself is deleted.")
        if confirm is not None and not confirm():
            return RollbackResult(outcome="aborted_by_user")

        result = engine.restore_migration_backup(client, record, boot=boot,
                                                 on_progress=on_progress, on_warning=warn)

        if managed is not None:
            say(f"Removing '{name}' from scheduling (its migrated volumes are kept, untagged)...")
            data_ids = [v["volume_id"] for v in managed.get("data_volumes") or []]
            try:
                engine.untag_managed_resources(client, name, os_volume_id=managed.get("os_volume_id"),
                                               data_volume_ids=data_ids)
                _strip_recovery_metadata_tags(client, managed.get("os_volume_id"))
            except Exception as e:
                warn(f"WARNING: could not untag the migrated volumes of '{name}' ({e}); a later "
                     "`rebuild` may list them again -- remove their linode-scheduler-* tags.")
            reserved = managed.get("reserved_ip")
            if reserved:
                try:
                    _strip_scheduler_tags_from_reserved_ip(client, reserved)
                except Exception as e:
                    warn(f"WARNING: could not untag reserved IP {reserved} ({e}).")
            _delete_one_record(name)
        migration = load_migrations().get(name)
        if migration is not None:
            dest = migration.get("dest_volume_id")
            if dest:
                try:
                    engine.retag_migration_volume_as_orphaned(client, dest, name)
                except Exception as e:
                    warn(f"WARNING: could not mark migration volume {dest} orphaned ({e}).")
                say(f"The migration's destination volume {dest} is kept and listed by "
                    "`migrate-orphans`; delete it there when you no longer need it.")
            _delete_one_migration(name)

        record["restored_at"] = datetime.now(UTC).isoformat()
        record["restore_result"] = result
        _save_migration_backup(name, "restored", record)
        _sync_migration_backup_record(name, on_warning)

    reachable = None
    host = _restored_ssh_host(record)
    if boot and host and ssh_key:
        try:
            engine.ssh_run(host, ssh_key, "true", trust_new=True)
            reachable = True
        except Exception as e:
            if "IDENTIFICATION HAS CHANGED" in str(e) or "host key" in str(e).lower():


                warn(f"WARNING: the restored instance presents new SSH host keys at {host} (it "
                     "was a new instance to cloud-init). Expect a host-key warning from your own "
                     "SSH clients; remove the old entry (ssh-keygen -R) after checking.")
                try:
                    engine.reset_known_host(host)
                    engine.ssh_run(host, ssh_key, "true", trust_new=True)
                    reachable = True
                except Exception as e2:
                    reachable = False
                    warn(f"WARNING: the restored instance didn't answer SSH at {host} ({e2}).")
            else:
                reachable = False
                warn(f"WARNING: the restored instance booted but didn't answer SSH at {host} yet "
                     f"({e}). Check it in the Linode console.")
    return RollbackResult(
        outcome="restored", backup_instance_id=backup_id, label=result.get("label"),
        public_ipv4=result.get("public_ipv4") or [],
        original_deleted_instance_id=orig["instance_id"] if result.get("original_deleted") else None,
        reachable=reachable, warnings=warnings,
    )


@dataclass
class BackupDeleteResult:
    outcome: Literal["deleted", "forgotten", "aborted_by_user", "incomplete", "pending_deletion"]
    problems: list[str] = field(default_factory=list)
    pending_deletion: dict | None = None


@_logs_activity("backup-delete")
def delete_migration_backup(
    client, name: str, *, grace: bool = False, confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> BackupDeleteResult:

    with _instance_lock(name):
        entry = get_migration_backup(name)
        if entry is None:
            raise engine.ConfigError(f"no pre-migration backup is recorded for '{name}'.")
        record = entry["record"]
        if entry["status"] == "restored":
            if on_progress is not None:
                on_progress(f"The backup for '{name}' was restored and is now the running system "
                            f"(instance {record['backup'].get('instance_id')}); only the record is "
                            "removed, nothing on Linode changes.")
            if confirm is not None and not confirm():
                return BackupDeleteResult(outcome="aborted_by_user")
            _delete_migration_backup_row(name)
            _sync_migration_backup_record(name, on_warning)
            return BackupDeleteResult(outcome="forgotten")
        if on_progress is not None:
            vols = record["backup"].get("volumes") or []
            on_progress(f"This permanently deletes the backup of '{name}': instance "
                        f"{record['backup'].get('instance_id')} and {len(vols)} cloned volume(s). "
                        "The original system can no longer be rolled back to after this.")
        if confirm is not None and not confirm():
            return BackupDeleteResult(outcome="aborted_by_user")
        if grace and deletion_grace_days() > 0:
            if pending_deletion_for(name, "migration_backup") is not None:
                raise engine.ConfigError(f"the backup of '{name}' is already pending deletion.")
            resources = []
            if record["backup"].get("instance_id"):
                resources.append({"type": "instance", "id": record["backup"]["instance_id"],
                                  "role": "backup", "action": "delete"})
            for vol in record["backup"].get("volumes") or []:
                resources.append({"type": "volume", "id": vol["volume_id"], "role": "backup",
                                  "action": "delete"})
            entry = schedule_pending_deletion(client, name, "migration_backup", resources,
                                              on_progress=on_progress, on_warning=on_warning)
            return BackupDeleteResult(outcome="pending_deletion", pending_deletion=entry)
        problems = engine.delete_migration_backup_resources(client, record, name,
                                                             on_progress=on_progress)
        if problems:
            return BackupDeleteResult(outcome="incomplete", problems=problems)
        _delete_migration_backup_row(name)
        _sync_migration_backup_record(name, on_warning)
        return BackupDeleteResult(outcome="deleted")


def _format_cost(estimate: dict | None) -> str:
    if not estimate:
        return "an amount Linode's price list couldn't confirm (about the instance's own plan price)"
    return (f"about ${estimate['total_monthly']:.2f}/month (${estimate['instance_monthly']:.2f} "
            f"for the powered-off {estimate['plan']} clone -- a powered-off Linode still bills -- "
            f"plus ${estimate['volumes_monthly']:.2f} for {estimate['volume_gb']} GB of cloned "
            "volumes)")


def cmd_backup_list(args) -> int:
    entries = [migration_backup_summary(e) for e in list_migration_backups()]
    if getattr(args, "json", False):
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("No pre-migration backups recorded.")
        return 0
    for e in entries:
        cost = e.get("estimated_monthly_cost") or {}
        print(f"{e['name']}: {e['status']}  backup instance {e['backup_instance_id']} "
              f"({e['backup_label']}), {len(e['backup_volumes'])} cloned volume(s), taken "
              f"{e['created_at']}" + (f", ~${cost['total_monthly']:.2f}/month" if cost else ""))
    return 0


def cmd_console_session_delete(args) -> int:
    try:
        removed = delete_console_session(args.name)
    except InstanceLockedError as e:
        print(f"Refused: {e} A migration copy may still be writing this session.",
              file=sys.stderr)
        return 3
    if not removed:
        print(f"No console session log for '{args.name}'.")
        return 0
    print(f"Deleted the console session for '{args.name}' ({', '.join(removed)}).")
    return 0


def cmd_rollback(client, args) -> int:
    def _confirm() -> bool:
        if args.yes:
            return True
        return input(f"Type '{args.name}' to roll back: ").strip() == args.name

    try:
        result = rollback_to_backup(client, args.name, args.ssh_key, boot=not args.no_boot,
                                    confirm=_confirm, on_progress=print,
                                    on_warning=_print_to_stderr)
    except InstanceLockedError as e:
        print(f"{e}", file=sys.stderr)
        return 3
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "aborted_by_user":
        print("Aborted; nothing changed.")
        return 1
    print(f"Rolled back: instance {result.backup_instance_id} ('{result.label}') is the original "
          f"system again" + (f", at {', '.join(result.public_ipv4)}" if result.public_ipv4 else "")
          + ("." if not args.no_boot else " (left powered off)."))
    if result.original_deleted_instance_id:
        print(f"The original instance {result.original_deleted_instance_id} was replaced (deleted; "
              "its volumes were detached and kept).")
    print(f"'{args.name}' is no longer managed by the scheduler. `backup-delete --name "
          f"{args.name}` removes the backup record (nothing on Linode is deleted for a restored "
          "backup).")
    return 0 if result.reachable in (True, None) else 1


def cmd_backup_delete(client, args) -> int:
    def _confirm() -> bool:
        if args.yes:
            return True
        return input(f"Type '{args.name}' to confirm: ").strip() == args.name

    try:
        result = delete_migration_backup(client, args.name, grace=not getattr(args, "now", False),
                                         confirm=_confirm, on_progress=print,
                                         on_warning=_print_to_stderr)
    except InstanceLockedError as e:
        print(f"{e}", file=sys.stderr)
        return 3
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "aborted_by_user":
        print("Aborted; nothing changed.")
        return 1
    if result.outcome == "incomplete":
        print("Backup NOT fully deleted -- the record is kept so you can retry:", file=sys.stderr)
        for p in result.problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    if result.outcome == "pending_deletion":
        entry = result.pending_deletion or {}
        print(f"The backup of '{args.name}' is pending deletion (#{entry.get('id')}). Nothing has "
              f"been deleted yet: it happens automatically on {str(entry.get('due_at'))[:10]}."
              f"\n  Keep it after all:  pending-deletions --recover {entry.get('id')}"
              f"\n  Delete it now:      pending-deletions --delete-now {entry.get('id')}")
        return 0
    print("Backup deleted." if result.outcome == "deleted" else "Backup record removed.")
    return 0


def cmd_release_backup(args) -> int:
    if args.output:
        info = build_release_archive(Path(args.output))
        print(f"Wrote {args.output}: release {info['version']}, {info['files']} files, "
              f"{info['wheels']} dependency wheels, {info['bytes'] // 1024} KiB.")
        return 0
    if not osb.is_configured():
        print("Object Storage isn't configured -- use --output to write the archive locally.",
              file=sys.stderr)
        return 1
    try:
        state = backup_release_copy(force=args.force, on_progress=print)
    except Exception as e:
        print(f"Couldn't save the release copy: {e}", file=sys.stderr)
        return 1
    if state is None:
        print(f"Release {release_version()} is already saved and unchanged (use --force to upload again).")
    return 0


def cmd_pending_deletions(client, args) -> int:
    if args.recover is None and args.delete_now is None:
        entries = list_pending_deletions(include_done=args.all)
        if args.json:
            print(json.dumps(entries, indent=2, default=str))
            return 0
        if not entries:
            print("Nothing is pending deletion.")
            return 0
        for e in entries:
            what = ", ".join(f"{r['type']} {r['id']}" + (" (kept)" if r.get("action") == "untag"
                                                        else "") for r in e["resources"])
            kind = "node" if e["kind"] == "node" else "pre-migration backup"
            print(f"#{e['id']}  {kind} '{e['name']}'  {e['status']}  due {e['due_at'][:10]}  "
                  f"{what}" + (f"\n      {e['detail']}" if e.get("detail") else ""))
        return 0
    entry_id = args.recover if args.recover is not None else args.delete_now
    entry = get_pending_deletion(entry_id)
    if entry is None:
        print(f"No pending deletion #{entry_id}.", file=sys.stderr)
        return 1
    if args.delete_now is not None and not args.yes:
        answer = input(f"This permanently deletes what's listed for '{entry['name']}' now -- it "
                       f"can't be recovered afterwards. Type '{entry['name']}' to confirm: ")
        if answer.strip() != entry["name"]:
            print("Aborted; nothing changed.")
            return 1
    try:
        if args.recover is not None:
            done = recover_pending(client, entry_id, on_progress=print, on_warning=_print_to_stderr)
            print(f"'{done['name']}' recovered.")
            return 0
        done = delete_pending_now(client, entry_id, on_progress=print,
                                  on_warning=_print_to_stderr)
    except InstanceLockedError as e:
        print(f"{e}", file=sys.stderr)
        return 3
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if done["status"] == "failed":
        print(f"Not fully deleted -- kept open and retried automatically: {done['detail']}",
              file=sys.stderr)
        return 1
    print(f"Deleted: {done['detail']}")
    return 0


_HOLIDAY_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
MAX_HOLIDAY_RANGE_DAYS = 366


def _parse_holiday_date(value: str) -> date:
    if not isinstance(value, str) or not _HOLIDAY_DATE_RE.fullmatch(value.strip()):
        raise engine.ConfigError(f"'{value}' is not a date in YYYY-MM-DD form.")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as e:
        raise engine.ConfigError(f"'{value}' is not a valid date.") from e


def _holiday_scope(group_name: str | None, name: str | None) -> tuple[str, str]:
    if group_name and name:
        raise engine.ConfigError("a holiday is for every node, one group, or one node -- not both.")
    if group_name:
        if get_schedule_group(group_name) is None:
            raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
        return "group", group_name
    if name:
        if name not in load_registry():
            raise NotOnboardedError(f"'{name}' is not onboarded.")
        return "instance", name
    return "all", ""


def list_holidays(*, since: date | None = None) -> list[dict]:
    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT date, scope, target, note, created_at FROM holidays ORDER BY date, scope, "
                "target").fetchall()
        finally:
            conn.close()

    out = []
    for d, scope, target, note, created in _retry_db(_do):
        if since is not None and d < since.isoformat():
            continue
        out.append({"date": d, "scope": scope, "target": target or None, "note": note,
                    "created_at": created})
    return out


def _sync_holidays(on_warning: Callable[[str], None] | None = None) -> None:
    if not osb.is_configured():
        return
    try:
        osb.upload_holidays({"holidays": list_holidays()})
    except Exception as e:
        if on_warning is not None:
            on_warning(f"WARNING: could not back up holidays to Object Storage ({e}).")


def add_holidays(start: str, end: str | None = None, *, group_name: str | None = None,
                 name: str | None = None, note: str | None = None,
                 on_warning: Callable[[str], None] | None = None) -> list[str]:

    first = _parse_holiday_date(start)
    last = _parse_holiday_date(end) if end else first
    if last < first:
        raise engine.ConfigError("the end date is before the start date.")
    if (last - first).days >= MAX_HOLIDAY_RANGE_DAYS:
        raise engine.ConfigError(f"a range can cover at most {MAX_HOLIDAY_RANGE_DAYS} days.")
    if note is not None and (len(note) > 200 or "\n" in note):
        raise engine.ConfigError("a note is one line of at most 200 characters.")
    scope, target = _holiday_scope(group_name, name)
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    now = datetime.now(UTC).isoformat()

    def _do():
        conn = _connect()
        try:
            added = []
            for d in days:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO holidays (date, scope, target, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?)", (d, scope, target, note, now))
                if cur.rowcount:
                    added.append(d)
            conn.commit()
            return added
        finally:
            conn.close()

    added = _retry_db(_do)
    _sync_holidays(on_warning)
    return added


def remove_holidays(start: str, end: str | None = None, *, group_name: str | None = None,
                    name: str | None = None,
                    on_warning: Callable[[str], None] | None = None) -> int:

    first = _parse_holiday_date(start)
    last = _parse_holiday_date(end) if end else first
    if last < first:
        raise engine.ConfigError("the end date is before the start date.")
    scope, target = ("group", group_name) if group_name else (
        ("instance", name) if name else ("all", ""))

    def _do():
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM holidays WHERE date >= ? AND date <= ? AND scope = ? "
                               "AND target = ?", (first.isoformat(), last.isoformat(), scope, target))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    removed = _retry_db(_do)
    _sync_holidays(on_warning)
    return removed


def _holiday_set() -> set[tuple[str, str, str]]:
    return {(h["date"], h["scope"], h["target"] or "") for h in list_holidays()}


ALERT_WEBHOOK_ENV = "LIS_ALERT_WEBHOOK_URL"
ALERT_REPEAT_SECONDS = 6 * 3600
SCHEDULER_STALE_MIN_SECONDS = 600


@dataclass
class HealthProblem:
    key: str
    message: str


TOKEN_EXPIRY_WARN_DAYS = 14
DISK_FREE_MIN_BYTES = 1024 ** 3
DISK_FREE_MIN_FRACTION = 0.05
_token_check_cache: dict = {"at": None, "problems": []}


def token_health(client, *, now: datetime | None = None) -> list[HealthProblem]:

    now = now or datetime.now(UTC)
    cached_at = _token_check_cache["at"]
    if cached_at is not None and (now - cached_at).total_seconds() < 3600:
        return list(_token_check_cache["problems"])
    problems: list[HealthProblem] = []
    try:
        engine.auth_check(client)
    except Exception as e:
        problems.append(HealthProblem("api-token", f"the Linode API token is being rejected: {e}"))
    else:
        with contextlib_suppress(Exception):
            token = engine.load_token()
            for t in engine.retry_transient(lambda: list(client.profile.tokens())):
                shown = (t.token or "").rstrip("*")
                if len(shown) >= 8 and token.startswith(shown):
                    expiry = t.expiry
                    if isinstance(expiry, datetime):
                        expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=UTC)
                        days = (expiry - now).total_seconds() / 86400
                        if days <= TOKEN_EXPIRY_WARN_DAYS:
                            problems.append(HealthProblem(
                                "api-token-expiry",
                                f"the Linode API token ('{t.label}') expires "
                                f"{expiry.strftime('%Y-%m-%d')} -- create a new one and put it in "
                                "LINODE_API_TOKEN before then"))
                    break
    with contextlib_suppress(Exception):
        host = set_host_watchdog(client, enable=None)
        if host and not host["watchdog_enabled"]:
            problems.append(HealthProblem(
                "watchdog", f"Shutdown Watchdog is off on this scheduler's host ({host['label']}): "
                "if it crashes it stays down -- turn it on with `host-watchdog --enable`"))
    _token_check_cache.update(at=now, problems=problems)
    return problems


CLOCK_SKEW_LIMIT_SECONDS = 60
_CLOCK_CHECK_INTERVAL_S = 900
_clock_cache: dict = {"at": 0.0, "problems": []}


_bucket_protection_cache: dict = {"at": None, "status": {}}


def _bucket_protection_cached(now: datetime) -> dict:

    at = _bucket_protection_cache["at"]
    if at is not None and (now - at).total_seconds() < 3600:
        return _bucket_protection_cache["status"]
    status = osb.bucket_protection_status()
    if not status.get("error") and (status.get("versioning") != "Enabled" or not status.get("lifecycle")):
        try:
            status = osb.enable_bucket_protection()
            record_activity("turned on versioning (and old-version expiry) for the backup bucket",
                            source="system", action="bucket-protection")
        except Exception as e:
            status = {**status, "enable_error": str(e)}
    _bucket_protection_cache.update(at=now, status=status)
    return status


def cmd_bucket_protection(args) -> int:
    if not osb.is_configured():
        print("Object Storage isn't configured.", file=sys.stderr)
        return 1
    try:
        if args.restore_version:
            if not args.key:
                print("--restore-version needs --key.", file=sys.stderr)
                return 1
            osb.restore_object_version(args.key, args.restore_version)
            print(f"{args.key}: version {args.restore_version} is the current one again.")
            return 0
        if args.key:
            for v in osb.list_object_versions(args.key):
                state = "deleted" if v["deleted"] else f"{v['size']} bytes"
                print(f"{v['version_id']}  {v['last_modified']}  {state}"
                      + ("  (current)" if v["latest"] else ""))
            return 0
        status = osb.enable_bucket_protection() if args.enable else osb.bucket_protection_status()
    except Exception as e:
        print(f"Couldn't {'change' if args.enable else 'read'} the bucket settings: {e}", file=sys.stderr)
        return 1
    print(f"Versioning: {status.get('versioning')}; old-version expiry rule: "
          f"{'yes' if status.get('lifecycle') else 'no'}"
          + (f"; problem: {status['error']}" if status.get("error") else ""))
    return 0 if status.get("versioning") == "Enabled" else 1


def clock_skew_seconds(url: str = "https://api.linode.com/v4/regions") -> float | None:

    started = time.time()
    response = requests.get(url, timeout=10)
    finished = time.time()
    header = response.headers.get("Date")
    if not header:
        return None
    remote = parsedate_to_datetime(header).timestamp()

    return (started + finished) / 2 - remote


def _ntp_synchronized() -> bool | None:

    if shutil.which("timedatectl") is None:
        return None
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return {"yes": True, "no": False}.get(out)


def clock_health(*, force: bool = False) -> list[HealthProblem]:

    if not force and time.monotonic() - _clock_cache["at"] < _CLOCK_CHECK_INTERVAL_S:
        return list(_clock_cache["problems"])
    problems = []
    skew = None
    with contextlib_suppress(Exception):
        skew = clock_skew_seconds()
    if skew is not None and abs(skew) > CLOCK_SKEW_LIMIT_SECONDS:
        problems.append(HealthProblem(
            "clock", f"this host's clock is {abs(skew):.0f}s {'ahead of' if skew > 0 else 'behind'} "
            "Linode's -- schedules fire at the wrong time and the scheduler lease can misjudge "
            "another host. Enable time synchronization (e.g. `timedatectl set-ntp true`)."))
    elif _ntp_synchronized() is False:
        problems.append(HealthProblem(
            "clock", "the system clock isn't time-synchronized (timedatectl reports "
            "NTPSynchronized=no); it will drift. Enable it with `timedatectl set-ntp true`."))
    _clock_cache.update(at=time.monotonic(), problems=problems)
    return problems


def disk_health() -> list[HealthProblem]:
    try:
        usage = shutil.disk_usage(REGISTRY_PATH.parent if REGISTRY_PATH.parent.exists() else Path("."))
    except OSError:
        return []
    if usage.free < DISK_FREE_MIN_BYTES or usage.free < usage.total * DISK_FREE_MIN_FRACTION:
        return [HealthProblem("disk", f"only {usage.free // (1024 ** 2)} MB free on the disk holding "
                              "the database -- writes will start failing when it fills")]
    return []


def check_health(*, now: datetime | None = None, include_scheduler: bool = True,
                 client=None) -> list[HealthProblem]:

    now = now or datetime.now(UTC)
    problems: list[HealthProblem] = list(disk_health())
    if client is not None:
        problems += token_health(client, now=now)
        problems += clock_health()
    if include_scheduler:
        beat = None
        with contextlib_suppress(Exception):
            beat = get_scheduler_heartbeat()
        if beat is None:
            problems.append(HealthProblem("scheduler", "the scheduler has never run on this host"))
        else:
            interval = beat.get("interval_seconds") or 300
            age = (now - datetime.fromisoformat(beat["last_tick"])).total_seconds()
            if age > max(3 * interval, SCHEDULER_STALE_MIN_SECONDS):
                problems.append(HealthProblem(
                    "scheduler", f"the scheduler hasn't checked in for {int(age // 60)} minutes"))
    with contextlib_suppress(Exception):
        cont = continuous_backup_state()
        if cont["enabled"] and cont["last_error"]:
            problems.append(HealthProblem("backup", f"backups are failing: {cont['last_error'][:200]}"))
    with contextlib_suppress(Exception):
        last = get_system_backup_status().get("last_backup")
        if last and not last["ok"]:
            problems.append(HealthProblem(
                "backup-run", f"the last backup had problems: {'; '.join(last['problems'])[:300]}"))
    key = deployment_key_status()
    if deployment_ssh_key_path().is_file():
        missing = []
        if not key["passphrase_set"]:
            missing.append(f"no {SSH_KEY_PASSPHRASE_ENV} in .env")
        if not key["stored_in_database"]:
            missing.append("no encrypted copy in the database")
        if osb.is_configured() and not key["in_object_storage"]:
            missing.append("no encrypted copy in Object Storage")
        if not key.get("passphrase_escrowed"):
            missing.append("no recovery copy of the passphrase in the Linode account")
        if missing:
            problems.append(HealthProblem(
                "ssh-key", "the deployment SSH key isn't fully protected: " + ", ".join(missing)))
    lease = lease_state()
    if lease is not None and lease["role"] == "standby":
        problems.append(HealthProblem(
            "lease", f"this scheduler is standing by: {lease['holder_host']} holds the scheduler "
            "lease, so nothing is started or stopped from here. If that host is gone for good, "
            "run `scheduler-lease --take-over`."))
    verify = backup_verify_status()
    if verify is not None and not verify["ok"]:
        problems.append(HealthProblem(
            "backup-verify", "the backup restore test failed: " + "; ".join(verify["problems"])[:300]))
    elif (verify is not None and osb.is_configured()
          and (now - datetime.fromisoformat(verify["checked_at"])).total_seconds()
          > 2 * BACKUP_VERIFY_INTERVAL_SECONDS + 3600):
        problems.append(HealthProblem("backup-verify", "the backups haven't been restore-tested for two days"))
    with contextlib_suppress(Exception):
        env = env_backup_status()
        if env["configured"] and env["last_error"]:
            problems.append(HealthProblem(
                "env-backup", f"the encrypted settings (.env) backup is failing: {env['last_error'][:200]}"))
    with contextlib_suppress(Exception):
        off = [m for m in find_schedule_mismatches(now=now) if not m.changed_by_hand]
        if off:
            problems.append(HealthProblem(
                "schedule", f"{len(off)} node(s) off their schedule: "
                + ", ".join(f"{m.name} ({m.current}, should be {m.desired})" for m in off[:10])))
    with contextlib_suppress(Exception):
        problems += key_rotation_health(now=now)
    with contextlib_suppress(Exception):
        failing = find_failing_nodes(now=now)
        if failing:
            problems.append(HealthProblem(
                "node-failing", f"{len(failing)} node(s) keep failing to start/stop: "
                + "; ".join(f"{f['name']} ({f['failures']} failures in a row, last: "
                            f"{f['last_error'][:120] or 'no detail'})" for f in failing[:10])))
    with contextlib_suppress(Exception):
        if osb.is_configured():
            protection = _bucket_protection_cached(now)
            if protection.get("error"):
                problems.append(HealthProblem("bucket-versioning", protection["error"]))
            elif protection.get("versioning") != "Enabled":
                problems.append(HealthProblem(
                    "bucket-versioning", "versioning is off on the backup bucket -- an overwritten "
                    "or deleted backup can't be recovered. Run `bucket-protection --enable` (the "
                    "access key needs permission to change bucket settings)."))
    with contextlib_suppress(Exception):
        drill = recovery_drill_status()
        days = drill_interval_days()
        if drill and not drill.get("ok"):
            problems.append(HealthProblem(
                "recovery-drill", "the last recovery drill failed: "
                + "; ".join(drill.get("problems") or [])[:300]))
        elif days and drill.get("finished_at") and (
                now - datetime.fromisoformat(drill["finished_at"])).days > days + 1:
            problems.append(HealthProblem("recovery-drill",
                                          f"no recovery drill has run for over {days + 1} days"))
    with contextlib_suppress(Exception):
        release = release_backup_status()
        if osb.is_configured() and release.get("last_error"):
            problems.append(HealthProblem(
                "release-copy", "the copy of this release in Object Storage couldn't be saved: "
                f"{release['last_error'][:200]}"))
    with contextlib_suppress(Exception):
        stuck = [e for e in list_pending_deletions()
                 if e["status"] == "failed"
                 or datetime.fromisoformat(e["due_at"]) < now - timedelta(days=1)]
        if stuck:
            problems.append(HealthProblem(
                "pending-deletion", f"{len(stuck)} pending deletion(s) overdue or failing: "
                + ", ".join(f"#{e['id']} {e['name']}" for e in stuck[:10])
                + " -- see the Pending deletion page"))
    return problems


def _alert_state() -> dict[str, tuple[str, str | None]]:
    def _do():
        conn = _connect()
        try:
            return conn.execute("SELECT key, message, last_sent_at FROM alert_state").fetchall()
        finally:
            conn.close()

    return {k: (m, t) for k, m, t in _retry_db(_do)}


def _set_alert_state(key: str, message: str | None, sent_at: str | None) -> None:
    def _do():
        conn = _connect()
        try:
            if message is None:
                conn.execute("DELETE FROM alert_state WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO alert_state (key, message, last_sent_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET message = excluded.message, "
                    "last_sent_at = COALESCE(excluded.last_sent_at, alert_state.last_sent_at)",
                    (key, message, sent_at))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


ALERT_EMAIL_ENV = "LIS_ALERT_EMAIL_TO"


def alert_channels() -> dict:

    return {
        "webhook": bool(os.environ.get(ALERT_WEBHOOK_ENV, "").strip()),
        "email": bool(os.environ.get(ALERT_EMAIL_ENV, "").strip()
                      and os.environ.get("LIS_ALERT_SMTP_HOST", "").strip()),
        "email_to": [a.strip() for a in os.environ.get(ALERT_EMAIL_ENV, "").split(",") if a.strip()],
    }


ALERT_SETTING_KEYS = {
    "webhook_url": "LIS_ALERT_WEBHOOK_URL", "email_to": "LIS_ALERT_EMAIL_TO",
    "smtp_host": "LIS_ALERT_SMTP_HOST", "smtp_port": "LIS_ALERT_SMTP_PORT",
    "smtp_security": "LIS_ALERT_SMTP_SECURITY", "smtp_user": "LIS_ALERT_SMTP_USER",
    "smtp_password": "LIS_ALERT_SMTP_PASSWORD", "email_from": "LIS_ALERT_EMAIL_FROM",
}
_EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def alert_settings_summary() -> dict:

    env = os.environ
    url = env.get("LIS_ALERT_WEBHOOK_URL", "").strip()
    host = ""
    if url:
        with contextlib_suppress(Exception):
            from urllib.parse import urlparse
            host = urlparse(url).hostname or ""
    return {
        "webhook_set": bool(url), "webhook_host": host or None,
        "email_to": alert_channels()["email_to"],
        "smtp_host": env.get("LIS_ALERT_SMTP_HOST", "").strip() or None,
        "smtp_port": env.get("LIS_ALERT_SMTP_PORT", "").strip() or None,
        "smtp_security": env.get("LIS_ALERT_SMTP_SECURITY", "").strip() or "starttls",
        "smtp_user": env.get("LIS_ALERT_SMTP_USER", "").strip() or None,
        "smtp_password_set": bool(env.get("LIS_ALERT_SMTP_PASSWORD", "")),
        "email_from": env.get("LIS_ALERT_EMAIL_FROM", "").strip() or None,
    }


def configure_alerts(changes: dict[str, str | None]) -> dict:

    updates: dict[str, str | None] = {}
    for field_name, value in changes.items():
        if field_name not in ALERT_SETTING_KEYS:
            raise engine.ConfigError(f"unknown alert setting '{field_name}'")
        value = (value or "").strip() if field_name != "smtp_password" else (value or "")
        if field_name == "webhook_url" and value and not value.startswith(("https://", "http://")):
            raise engine.ConfigError("the webhook URL must start with https:// (or http://)")
        if field_name in ("email_to", "email_from") and value:
            for addr in [a.strip() for a in value.split(",") if a.strip()]:
                if not _EMAIL_RE.match(addr):
                    raise engine.ConfigError(f"'{addr}' isn't an email address")
        if field_name == "smtp_port" and value and not (value.isdigit() and 0 < int(value) < 65536):
            raise engine.ConfigError("the SMTP port must be a number from 1 to 65535")
        if field_name == "smtp_security" and value and value not in ("starttls", "ssl", "none"):
            raise engine.ConfigError("SMTP security must be starttls, ssl or none")
        updates[ALERT_SETTING_KEYS[field_name]] = value or None
    if updates:
        _write_env_settings(updates)
        record_activity("alert settings changed: " + ", ".join(sorted(changes)), source="system",
                        action="alerts-config")
    return alert_settings_summary()


def _send_webhook(text: str, event: str, details: dict) -> bool:
    url = os.environ.get(ALERT_WEBHOOK_ENV, "").strip()
    if not url:
        return False
    try:
        import requests as _requests
        response = _requests.post(url, json={"text": text, "event": event, "details": details,
                                             "host": socket.gethostname()}, timeout=10)
        return response.status_code < 400
    except Exception:
        return False


def _send_email(text: str, event: str, details: dict) -> bool:

    recipients = alert_channels()["email_to"]
    host = os.environ.get("LIS_ALERT_SMTP_HOST", "").strip()
    if not recipients or not host:
        return False
    import smtplib
    from email.message import EmailMessage

    security = os.environ.get("LIS_ALERT_SMTP_SECURITY", "starttls").strip().lower()
    port = _env_int("LIS_ALERT_SMTP_PORT", 465 if security == "ssl" else 587)
    user = os.environ.get("LIS_ALERT_SMTP_USER", "").strip()
    sender = os.environ.get("LIS_ALERT_EMAIL_FROM", "").strip() or user or f"scheduler@{socket.gethostname()}"
    msg = EmailMessage()
    status = details.get("status", "")
    msg["Subject"] = f"[Linode Instance Scheduler] {'RESOLVED' if status == 'resolved' else 'ALERT'}: {event}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(f"{text}\n\nHost: {socket.gethostname()}\nEvent: {event}\n")
    try:
        smtp_cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
        with smtp_cls(host, port, timeout=15) as smtp:
            if security == "starttls":
                smtp.starttls()
            if user:
                smtp.login(user, os.environ.get("LIS_ALERT_SMTP_PASSWORD", ""))
            smtp.send_message(msg)
        return True
    except Exception:
        return False


def send_alert(text: str, *, event: str, details: dict | None = None) -> bool:

    details = details or {}
    delivered = _send_webhook(text, event, details)
    delivered = _send_email(text, event, details) or delivered
    return delivered


def evaluate_alerts(*, now: datetime | None = None, include_scheduler: bool = True,
                    client=None) -> list[HealthProblem]:

    now = now or datetime.now(UTC)
    problems = check_health(now=now, include_scheduler=include_scheduler, client=client)
    current = {p.key: p for p in problems}
    try:
        previous = _alert_state()
    except Exception:
        return problems
    for key, problem in current.items():
        old = previous.get(key)
        due = old is None or old[1] is None or (
            now - datetime.fromisoformat(old[1])).total_seconds() >= ALERT_REPEAT_SECONDS
        if old is None:
            record_activity(f"ALERT: {problem.message}", level="error", source="system",
                            action="alert")
        if due:
            sent = send_alert(f"Linode Instance Scheduler: {problem.message}", event=key,
                              details={"status": "problem"})
            with contextlib_suppress(Exception):
                _set_alert_state(key, problem.message, now.isoformat() if sent else None)
        elif old is not None and old[0] != problem.message:
            with contextlib_suppress(Exception):
                _set_alert_state(key, problem.message, None)
    for key, (message, _sent) in previous.items():
        if key in current:
            continue
        if key == "scheduler" and not include_scheduler:
            continue
        record_activity(f"resolved: {message}", source="system", action="alert")
        send_alert(f"Linode Instance Scheduler: resolved -- {message}", event=key,
                   details={"status": "resolved"})
        with contextlib_suppress(Exception):
            _set_alert_state(key, None, None)
    return problems


MISSED_SCHEDULE_LOOKBACK_SECONDS = 3 * 24 * 3600


@dataclass
class ScheduleMismatch:
    name: str
    current: str
    desired: str
    via_group: str | None
    missed_at: str | None
    changed_by_hand: bool
    reason: str


def find_schedule_mismatches(*, now: datetime | None = None,
                             names: Collection[str] | None = None) -> list[ScheduleMismatch]:

    now = now or datetime.now(UTC)
    registry = load_registry()
    mismatches = []
    for name in sorted(names if names is not None else registry):
        record = registry.get(name)
        if record is None or record.get("transitioning"):
            continue
        current = record.get("current_status")
        if current not in ("running", "stopped"):
            continue
        if get_schedule_mode(name) == "manual":
            continue
        schedule, via = resolve_effective_schedule(name, record)
        if not schedule_is_active(schedule):
            continue
        want_on = is_within_scheduled_on_window(schedule, now)
        if want_on:
            zone = ZoneInfo(schedule["timezone"])
            if holiday_for(name, record, now.astimezone(zone).date()) is not None:
                continue
        else:
            expires = record.get("manual_override_expires_at")
            if current == "running" and expires:
                with contextlib_suppress(ValueError, TypeError):
                    if datetime.fromisoformat(expires) > now:
                        continue
        desired = "running" if want_on else "stopped"
        if current == desired:
            continue
        due = resolve_due_boundary(schedule, now, MISSED_SCHEDULE_LOOKBACK_SECONDS)
        missed_at = due[1] if due is not None else None
        if (missed_at is not None
                and (now - missed_at).total_seconds() < DEFAULT_POLL_CATCH_UP_SECONDS + 300):
            continue
        by_hand = bool(missed_at is not None and _changed_by_hand_since(name, missed_at, current))
        if by_hand:
            reason = (f"{'started' if current == 'running' else 'stopped'} by hand after the "
                      "scheduled time")
        elif missed_at is not None:
            reason = (f"the scheduled {'start' if desired == 'running' else 'stop'} at "
                      f"{missed_at.strftime('%Y-%m-%d %H:%M')} UTC never happened")
        else:
            reason = f"its schedule wants it {desired}"
        mismatches.append(ScheduleMismatch(
            name=name, current=current, desired=desired, via_group=via,
            missed_at=missed_at.isoformat() if missed_at else None,
            changed_by_hand=by_hand, reason=reason))
    return mismatches


FAILING_NODE_MIN_FAILURES = 3
FAILING_NODE_LOOKBACK = timedelta(hours=24)


def find_failing_nodes(*, now: datetime | None = None) -> list[dict]:

    now = now or datetime.now(UTC)
    failing = []
    for name, record in sorted(load_registry().items()):
        if record.get("current_status") == "needs_manual_recovery":
            continue
        streak: list[dict] = []
        for event in get_schedule_events(name, limit=FAILING_NODE_MIN_FAILURES * 4):
            if event["action"] not in ("create", "delete"):
                continue
            if event["result"] == "success":
                break
            streak.append(event)
        if len(streak) < FAILING_NODE_MIN_FAILURES:
            continue
        newest = datetime.fromisoformat(streak[0]["timestamp"])
        if now - newest > FAILING_NODE_LOOKBACK:
            continue
        failing.append({"name": name, "status": record.get("current_status"),
                        "failures": len(streak), "last_failure_at": streak[0]["timestamp"],
                        "last_error": (streak[0].get("error_message") or "")[:200]})
    return failing


def _changed_by_hand_since(name: str, since: datetime, current: str) -> bool:

    wanted = "create" if current == "running" else "delete"
    for event in get_schedule_events(name, limit=200):
        if event["result"] != "success" or event["action"] not in ("create", "delete"):
            continue
        if datetime.fromisoformat(event["timestamp"]) < since:
            return False
        return event["triggered_by"] in ("manual", "api") and event["action"] == wanted
    return False


@dataclass
class CatchUpItem:
    name: str
    action: str
    outcome: str
    ok: bool
    detail: str | None = None


@dataclass
class CatchUpResult:
    items: list[CatchUpItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(i.ok for i in self.items)


def _start_depth(record: dict) -> int:

    group = _group_row_by_id(record["group_id"]) if record.get("group_id") is not None else None
    if group is None:
        return 0
    with contextlib_suppress(Exception):
        return len(_group_chain(group["name"], "start")) - 1
    return 0


def apply_schedule_catch_up(
    client, ssh_key: str, names: Collection[str], *, actor: str | None = None,
    max_parallel: int = 5, client_factory: Callable[[], object] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> CatchUpResult:

    result = CatchUpResult()
    wanted = set(names)
    pending = [m for m in find_schedule_mismatches() if m.name in wanted]
    for name in sorted(wanted - {m.name for m in pending}):
        result.items.append(CatchUpItem(name, "-", "not_needed", True,
                                        "already in its scheduled state (or not scheduled)"))
    registry = load_registry()

    def _run(m: ScheduleMismatch) -> CatchUpItem:
        c = client_factory() if client_factory is not None else client
        try:
            if m.desired == "running":
                r = start_instance(c, m.name, ssh_key, triggered_by="manual", actor=actor,
                                   on_warning=on_warning)
                ok = r.outcome in ("started", "already_running")
                return CatchUpItem(m.name, "start", r.outcome, ok, r.detail)
            r2 = stop_instance(c, m.name, ssh_key, triggered_by="manual", actor=actor,
                               on_warning=on_warning)
            return CatchUpItem(m.name, "stop", r2.outcome,
                               r2.outcome in ("stopped", "already_stopped",
                                              "confirmed_gone_reset_to_stopped"), r2.detail)
        except Exception as e:
            return CatchUpItem(m.name, "start" if m.desired == "running" else "stop", "error",
                               False, str(e))

    def _levels(items: list[ScheduleMismatch], deepest_first: bool) -> list[list[ScheduleMismatch]]:
        by_depth: dict[int, list[ScheduleMismatch]] = {}
        for m in items:
            by_depth.setdefault(_start_depth(registry.get(m.name) or {}), []).append(m)
        return [by_depth[d] for d in sorted(by_depth, reverse=deepest_first)]

    stops = [m for m in pending if m.desired == "stopped"]
    starts = [m for m in pending if m.desired == "running"]
    workers = max(1, min(max_parallel, MAX_POLL_MAX_PARALLEL))
    for level in _levels(stops, True) + _levels(starts, False):
        if on_progress is not None:
            on_progress("  " + ", ".join(
                f"{'start' if m.desired == 'running' else 'stop'} {m.name}" for m in level))
        if len(level) == 1 or workers == 1 or client_factory is None:
            items = [_run(m) for m in level]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                items = list(pool.map(_run, level))
        result.items.extend(items)
        for item in items:
            record_activity(
                f"schedule catch-up: {item.action} -> {item.outcome}"
                f"{f' ({item.detail})' if item.detail else ''}",
                level="info" if item.ok else "error", source="system",
                instance_name=item.name, action="schedule-catch-up", actor=actor)
    return result


def holiday_for(name: str, record: dict, local_date: date,
                holidays: set[tuple[str, str, str]] | None = None) -> str | None:

    holidays = _holiday_set() if holidays is None else holidays
    d = local_date.isoformat()
    group_id = record.get("group_id")
    group = _group_row_by_id(group_id) if group_id is not None else None
    if (d, "all", "") in holidays and effective_account_holidays(name, group)[0] == "follow":
        return "every node"
    if (d, "instance", name) in holidays:
        return "this node"
    if group is not None and (d, "group", group["name"]) in holidays:
        return f"group '{group['name']}'"
    return None


def holiday_today(name: str, record: dict) -> str | None:

    schedule, _via = resolve_effective_schedule(name, record)
    zone = ZoneInfo(schedule["timezone"]) if schedule else UTC
    return holiday_for(name, record, datetime.now(UTC).astimezone(zone).date())


ACCOUNT_HOLIDAY_MODES = ("follow", "ignore")
_HOLIDAY_SETTING_TAG_PREFIX = "hol:"


def get_instance_account_holidays(name: str) -> str | None:

    def _do():
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT account_holidays FROM instance_settings WHERE instance_name = ?", (name,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    value = _retry_db(_do)
    return value if value in ACCOUNT_HOLIDAY_MODES else None


def effective_account_holidays(name: str, group: dict | None) -> tuple[str, str]:

    own = get_instance_account_holidays(name)
    if own is not None:
        return own, "instance"
    if group is not None and group.get("account_holidays") in ACCOUNT_HOLIDAY_MODES:
        if group["account_holidays"] == "ignore":
            return "ignore", "group"
        return "follow", "group"
    return "follow", "default"


def _write_instance_account_holidays_row(name: str, mode: str | None) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO instance_settings (instance_name, account_holidays) VALUES (?, ?)"
                " ON CONFLICT(instance_name) DO UPDATE SET account_holidays ="
                " excluded.account_holidays",
                (name, mode),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


def _sync_holiday_setting_tag_locked(client, name: str, record: dict, mode: str | None) -> None:

    if client is None or not record.get("os_volume_id"):
        return
    os_volume = engine.retry_transient(
        lambda vol_id=record["os_volume_id"]: client.load(Volume, vol_id)
    )
    current = list(os_volume.tags or [])
    new_tags = [t for t in current if not t.startswith(_HOLIDAY_SETTING_TAG_PREFIX)]
    if mode in ACCOUNT_HOLIDAY_MODES:
        new_tags.append(f"{_HOLIDAY_SETTING_TAG_PREFIX}{mode}")
    if new_tags != current:
        os_volume.tags = new_tags
        engine.retry_transient(os_volume.save)
        _verify_os_volume_tag_write(client, record["os_volume_id"], new_tags)


def set_instance_account_holidays(
    client, name: str, mode: str | None, *, on_warning: Callable[[str], None] | None = None,
) -> dict:

    if mode is not None and mode not in ACCOUNT_HOLIDAY_MODES:
        raise engine.ConfigError("account holidays must be 'follow', 'ignore' or inherited.")
    with _instance_lock(name):
        record = load_registry().get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")
        _write_instance_account_holidays_row(name, mode)
        try:
            _sync_holiday_setting_tag_locked(client, name, record, mode)
        except Exception as e:
            if on_warning is not None:
                on_warning(
                    f"WARNING: the holiday setting for '{name}' is in effect, but its "
                    f"disaster-recovery tag couldn't be updated: {e} -- the next start or stop "
                    "retries it."
                )
        group = _group_row_by_id(record["group_id"]) if record.get("group_id") else None
    effective, source = effective_account_holidays(name, group)
    return {"own": mode, "effective": effective, "source": source}


def set_group_account_holidays(
    client, group_name: str, mode: str, *, on_warning: Callable[[str], None] | None = None,
) -> str:

    if mode not in ACCOUNT_HOLIDAY_MODES:
        raise engine.ConfigError("account holidays must be 'follow' or 'ignore'.")

    def _do():
        conn = _connect()
        try:
            _group_id_for_name(conn, group_name)
            conn.execute("UPDATE schedule_groups SET account_holidays = ? WHERE name = ?",
                         (mode, group_name))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)
    group = get_schedule_group(group_name)
    if group is None:
        raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    for member in group["members"]:
        _sync_group_membership_tags(client, member, group, on_warning=on_warning)
    _sync_group_object_backup(group_name, on_warning=on_warning)
    return mode


def _restore_group_account_holidays(group_name: str, mode: str | None) -> None:
    if mode == "ignore":
        def _do():
            conn = _connect()
            try:
                conn.execute("UPDATE schedule_groups SET account_holidays = 'ignore' "
                             "WHERE name = ?", (group_name,))
                conn.commit()
            finally:
                conn.close()

        _retry_db(_do)


def restore_holidays_from_object_storage(
    *, on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> int:

    stored = osb.download_holidays()
    if not stored or not isinstance(stored.get("holidays"), list):
        return 0
    existing = _holiday_set()
    rows = []
    for h in stored["holidays"]:
        try:
            d = _parse_holiday_date(h["date"]).isoformat()
            scope = h["scope"]
            target = h.get("target") or ""
        except Exception:
            continue
        if scope in ("all", "group", "instance") and (d, scope, target) not in existing:
            rows.append((d, scope, target, h.get("note"), h.get("created_at") or
                         datetime.now(UTC).isoformat()))

    def _do():
        conn = _connect()
        try:
            conn.executemany("INSERT OR IGNORE INTO holidays (date, scope, target, note, created_at) "
                             "VALUES (?, ?, ?, ?, ?)", rows)
            conn.commit()
        finally:
            conn.close()

    if rows:
        _retry_db(_do)
        if on_progress is not None:
            on_progress(f"Restored {len(rows)} holiday(s) from Object Storage.")
    return len(rows)


def cmd_holiday_settings(client, args) -> int:

    if args.follow_account_wide:
        mode: str | None = "follow"
    elif args.ignore_account_wide:
        mode = "ignore"
    elif args.inherit:
        mode = None
    else:
        mode = "show"
    try:
        if args.group_name:
            if args.inherit:
                print("Configuration error: --inherit applies to a node (--name), not a group.",
                      file=sys.stderr)
                return 1
            if mode == "show":
                group = get_schedule_group(args.group_name)
                if group is None:
                    raise GroupNotFoundError(f"no schedule group named '{args.group_name}' exists.")
                print(f"Group '{args.group_name}': {group['account_holidays']} account-wide holidays.")
                return 0
            assert mode is not None
            set_group_account_holidays(client, args.group_name, mode, on_warning=_print_to_stderr)
            print(f"Group '{args.group_name}' now {'follows' if mode == 'follow' else 'ignores'} "
                  "account-wide holidays (members with their own setting keep it).")
            return 0
        if mode == "show":
            r = get_instance_status(args.name)["account_holidays"]
        else:
            r = set_instance_account_holidays(client, args.name, mode, on_warning=_print_to_stderr)
        own = r["own"] or f"inherit (from {r['source']})"
        print(f"'{args.name}': {r['effective']} account-wide holidays (own setting: {own}).")
        return 0
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 3
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1


def cmd_holiday_add(args) -> int:
    try:
        added = add_holidays(args.date, args.to, group_name=args.group_name, name=args.name,
                             note=args.note, on_warning=_print_to_stderr)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    who = (f"group '{args.group_name}'" if args.group_name else
           f"'{args.name}'" if args.name else "every node")
    print(f"Holiday(s) added for {who}: {', '.join(added) or '(already present)'}. Scheduled starts "
          "on these dates are skipped; scheduled stops still happen.")
    return 0


def cmd_holiday_remove(args) -> int:
    try:
        removed = remove_holidays(args.date, args.to, group_name=args.group_name, name=args.name,
                                  on_warning=_print_to_stderr)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Removed {removed} holiday(s).")
    return 0


def cmd_holiday_list(args) -> int:
    since = None if args.all else datetime.now(UTC).date() - timedelta(days=1)
    items = list_holidays(since=since)
    if args.json:
        print(json.dumps(items, indent=2))
        return 0
    if not items:
        print("No holidays" + ("" if args.all else " from today on") + ".")
        return 0
    for h in items:
        who = ("every node" if h["scope"] == "all" else
               f"group {h['target']}" if h["scope"] == "group" else f"node {h['target']}")
        print(f"{h['date']}  {who}" + (f"  -- {h['note']}" if h["note"] else ""))
    return 0


def cmd_serve_api(args) -> int:

    import api_server

    try:
        api_server.run_server(args.host, args.port)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    return 0


def list_instances() -> dict:

    registry = load_registry()
    modes = get_schedule_modes()
    holidays = _holiday_set()
    for name, record in registry.items():
        record["schedule_mode"] = modes.get(name, "auto")
        holiday = _holiday_today_cached(name, record, holidays)
        if holiday:
            record["holiday_today"] = holiday
    return registry


def _holiday_today_cached(name: str, record: dict, holidays: set[tuple[str, str, str]]) -> str | None:
    if not holidays:
        return None
    try:
        schedule, _via = resolve_effective_schedule(name, record)
        zone = ZoneInfo(schedule["timezone"]) if schedule else UTC
        return holiday_for(name, record, datetime.now(UTC).astimezone(zone).date(), holidays)
    except Exception:
        return None


def cmd_list(args) -> int:
    registry = list_instances()
    if _flag(args, "json"):
        print(json.dumps(registry, default=str))
        return 0
    if not registry:
        print("No instances onboarded yet.")
        return 0
    for name, record in registry.items():
        lock = " [LOCKED]" if record.get("transitioning") else ""
        override = ""
        if record.get("manual_override_expires_at"):
            override = f"  [override expires {_format_override_expiry(record['manual_override_expires_at'])}]"
        manual = "  [manual-only]" if record.get("schedule_mode") == "manual" else ""
        print(f"{name}: {record['current_status']}{lock}{manual}  "
              f"ip={record['reserved_ip']}  region={record['region']}  "
              f"linode_id={record['current_linode_id']}{override}")
    return 0


def get_instance_status(name: str) -> dict:

    registry = load_registry()
    record = registry.get(name)
    if record is None:
        raise NotOnboardedError(f"'{name}' is not onboarded.")
    record["schedule_mode"] = get_schedule_mode(name)
    holiday = _holiday_today_cached(name, record, _holiday_set())
    if holiday:
        record["holiday_today"] = holiday
    group = _group_row_by_id(record["group_id"]) if record.get("group_id") else None
    effective, source = effective_account_holidays(name, group)
    record["account_holidays"] = {"own": get_instance_account_holidays(name),
                                  "effective": effective, "source": source}
    schedule, via = resolve_effective_schedule(name, record)
    record["effective_schedule"] = {
        "source": None if schedule is None else (f"group:{via}" if via else "individual"),
        "active": schedule_is_active(schedule)}
    return record


def cmd_status(args) -> int:
    try:
        record = get_instance_status(args.name)
    except NotOnboardedError as e:


        print(f"{e}", file=sys.stderr)
        return 1
    if record.get("manual_override_expires_at"):


        print(f"'{args.name}': manually started outside its scheduled hours, auto-stops at "
              f"{_format_override_expiry(record['manual_override_expires_at'])} unless extended "
              f"(`extend --name {args.name}`).", file=sys.stderr)

    for line in _hook_status_lines(args.name, record):
        print(line, file=sys.stderr)
    print(json.dumps(record, indent=2, default=str))
    if getattr(args, "check_network", False) is True:
        try:
            client = engine.build_client(engine.load_token())
            findings = unmanaged_network_neighbours(client, args.name, record, load_registry())
        except (engine.ConfigError, ApiError, requests.exceptions.RequestException) as e:
            print(f"Could not check '{args.name}''s network neighbours: {e}", file=sys.stderr)
            return 1
        if findings:
            print(_neighbour_warning(args.name, findings), file=sys.stderr)
        else:
            print(f"'{args.name}''s VPC subnet(s) and VLAN(s) have no instances outside this tool's management.", file=sys.stderr)
    return 0


def get_last_hook_failure(name: str, hook: Literal["pre_stop", "post_start"]) -> dict | None:

    for event in get_hook_events(name, limit=_MAX_HISTORY_LIMIT):
        if event["hook"] != hook or event["result"] == "skipped":
            continue
        return event if event["result"] in ("failure", "warning") else None
    return None


def _hook_status_lines(name: str, record: dict) -> list[str]:
    lines = []
    effective = resolve_effective_hooks(name, record)
    for hook_type, label in (("pre_stop", "pre-stop hook"), ("post_start", "post-start check")):
        hook = effective[hook_type]
        if hook is None:
            continue
        policy = f", on failure: {hook['on_failure']}" if hook_type == "pre_stop" else ""
        lines.append(
            f"'{name}' {label} (from {hook['source']}, timeout {hook['timeout_s']}s{policy}): "
            f"{_hook_what(hook)}"
        )
    last_failure = get_last_hook_failure(name, "post_start")
    if last_failure is not None and record.get("current_status") == "running":
        lines.append(
            f"'{name}': last post-start check FAILED at {last_failure['timestamp']} -- "
            f"{last_failure['detail']}"
        )
    return lines


def cmd_history(args) -> int:

    events = get_schedule_events(args.name, limit=args.limit)
    if not events:
        print(f"No events recorded for '{args.name}'.")
        return 0
    for event in events:
        line = (
            f"{event['timestamp']}  {event['action']:6}  {event['result']:7}  "
            f"triggered_by={event['triggered_by']}"
        )
        if event.get("actor"):
            line += f"  actor={event['actor']}"
        if event["error_message"]:
            line += f"  ({event['error_message']})"
        print(line)
    return 0


@dataclass
class ClearLockResult:

    outcome: Literal["not_locked", "aborted_by_user", "cleared"]


def clear_instance_lock(
    name: str, *,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ClearLockResult:

    with _instance_lock(name):
        registry = load_registry()
        record = registry.get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded.")
        if not record.get("transitioning"):
            return ClearLockResult(outcome="not_locked")
        if on_progress is not None:
            on_progress(
                "WARNING: this forcibly clears the transitioning lock without confirming the "
                "operation it was protecting actually finished. Only do this if you've "
                f"independently confirmed (e.g. via Cloud Manager) that no create/delete is "
                f"genuinely still in flight for '{name}'."
            )
        if confirm is not None and not confirm():
            return ClearLockResult(outcome="aborted_by_user")


        engine.release_transition_lock(record, persist_fn=lambda r, n=name: _save_one_record(n, r))
        return ClearLockResult(outcome="cleared")


def cmd_heal(client, args) -> int:
    result = reconcile_fleet(
        client, leftover_min_age_s=args.leftover_min_age_minutes * 60,
        names=args.name or None, on_progress=print, on_warning=_print_to_stderr)
    if not result.changed:
        print("Every node matches the account -- nothing to fix.")
    for label, names in (("adopted a running instance", result.adopted),
                         ("reset to stopped", result.reset_to_stopped),
                         ("leftover instance deleted", result.leftovers_deleted),
                         ("stale in-progress mark cleared", result.locks_cleared),
                         ("skipped (a start/stop is running)", result.busy)):
        if names:
            print(f"  {label}: {', '.join(names)}")
    for problem in result.problems:
        print(f"WARNING: {problem}", file=sys.stderr)
    return 1 if result.problems else 0


def cmd_migrate_abandon(client, args) -> int:
    if not args.yes:
        state = load_migrations().get(args.name)
        if state is None:
            print(f"Configuration error: no unfinished migration for '{args.name}'.", file=sys.stderr)
            return 1
        print(f"This cancels '{args.name}''s migration: instance {state.get('instance_id')} is "
              f"taken out of Rescue Mode{' and booted from its original disk' if not args.no_boot else ''}, "
              f"and the destination volume {state.get('dest_volume_id')} is deleted.")
        if input(f"Type '{args.name}' to confirm: ").strip() != args.name:
            print("Aborted.")
            return 1
    try:
        result = abandon_migration(client, args.name, boot_original=not args.no_boot,
                                   on_progress=print, on_warning=_print_to_stderr)
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 3
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Migration cancelled. Instance {result.instance_id}: {result.instance_state}; "
          f"destination volume {result.volume_id}: {result.volume_state}.")
    if result.previous_attempts_archived:
        print(f"{result.previous_attempts_archived} earlier attempt volume(s) moved to the orphan "
              "archive -- review with `migrate-orphans`.")
    if result.backup_kept:
        print(f"The pre-migration backup is kept; remove it with `backup-delete --name {args.name}`.")
    return 0 if result.instance_state != "boot_failed" else 1


def _mismatch_dict(m: ScheduleMismatch) -> dict:
    return asdict(m)


def cmd_schedule_catch_up(client, args) -> int:
    mismatches = find_schedule_mismatches(names=args.name or None)
    if args.json and not args.apply:
        print(json.dumps([_mismatch_dict(m) for m in mismatches], indent=2))
        return 0
    if not mismatches:
        print("Every scheduled node is in the state its schedule wants.")
        return 0
    print(f"{len(mismatches)} node(s) aren't in their scheduled state:")
    for m in mismatches:
        via = f" (group {m.via_group})" if m.via_group else ""
        print(f"  {m.name}{via}: {m.current}, schedule wants {m.desired} -- {m.reason}")
    if not args.apply:
        print("Run again with --apply to bring them to their scheduled state "
              "(nodes changed by hand are left alone unless --include-changed-by-hand).")
        return 0
    chosen = [m for m in mismatches if args.include_changed_by_hand or not m.changed_by_hand]
    if not chosen:
        print("Nothing selected (every node was changed by hand; add --include-changed-by-hand).")
        return 0
    print("Will " + "; ".join(
        f"{'start' if m.desired == 'running' else 'stop'} {m.name}" for m in chosen) + ".")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return 1
    result = apply_schedule_catch_up(
        client, args.ssh_key, [m.name for m in chosen], max_parallel=args.max_parallel,
        client_factory=_per_thread_client_factory() if args.max_parallel > 1 else None,
        on_progress=print, on_warning=_print_to_stderr)
    for item in result.items:
        detail = f" ({item.detail})" if item.detail else ""
        print(f"  {item.name}: {item.action} -> {item.outcome}{detail}")
    return 0 if result.ok else 1


def cmd_host_watchdog(client, args) -> int:
    enable = True if args.enable else (False if args.disable else None)
    info = set_host_watchdog(client, enable=enable)
    if not info:
        print("This host isn't a Linode instance on this account (e.g. a Kubernetes pod) -- "
              "nothing to set.")
        return 0
    print(f"{info['label']} (instance {info['instance_id']}): Shutdown Watchdog "
          f"{'on' if info['watchdog_enabled'] else 'OFF'}")
    return 0


def cmd_scheduler_lease(args) -> int:
    if not osb.is_configured():
        print("Object Storage isn't configured: there's no shared lease, so make sure only one "
              "scheduler runs.")
        return 0
    if args.take_over:
        lease = read_lease()
        if (lease is not None and lease.get("id") != scheduler_id() and not args.yes
                and _lease_age_seconds(lease, datetime.now(UTC)) < LEASE_TTL_SECONDS):
            print(f"{lease.get('host')} renewed the lease {int(_lease_age_seconds(lease, datetime.now(UTC)))}s "
                  "ago -- it looks alive. Taking over while it runs means two schedulers until it "
                  "notices (within a minute) and stands by.")
            if input("Take over anyway? [y/N] ").strip().lower() != "y":
                print("Aborted.")
                return 1
        status = acquire_or_renew_lease(take_over=True)
        print("This host now holds the scheduler lease." if status.active
              else f"Couldn't take the lease: {status.detail}")
        return 0 if status.active else 1
    lease = read_lease()
    if lease is None:
        print("No scheduler holds the lease.")
        return 0
    age = int(_lease_age_seconds(lease, datetime.now(UTC)))
    who = "this host" if lease.get("id") == scheduler_id() else lease.get("host")
    fresh = age < LEASE_TTL_SECONDS
    print(f"Held by {who} -- renewed {age}s ago ({'active' if fresh else 'expired'}).")
    return 0


def cmd_backup_verify(args) -> int:
    result = verify_backups()
    for check in result.checks:
        print(f"ok: {check}")
    for problem in result.problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    print("Backup restore test passed." if result.ok else "Backup restore test FAILED.")
    return 0 if result.ok else 1


def cmd_alerts_check(args) -> int:

    client = None
    with contextlib_suppress(Exception):
        client = engine.build_client(engine.load_token())
    problems = (evaluate_alerts(client=client) if args.send
                else check_health(client=client))
    if not problems:
        print("OK: nothing needs attention.")
        return 0
    for p in problems:
        print(f"PROBLEM [{p.key}]: {p.message}")
    return 1


def cmd_alerts_config(args) -> int:
    changes: dict[str, str | None] = {}
    for field_name in ("webhook_url", "email_to", "smtp_host", "smtp_port", "smtp_security",
                       "smtp_user", "email_from"):
        value = getattr(args, field_name, None)
        if value is not None:
            changes[field_name] = value
    if args.smtp_password:
        import getpass
        changes["smtp_password"] = getpass.getpass("SMTP password: ")
    if args.clear_email:
        changes.update({k: "" for k in ("email_to", "smtp_host", "smtp_port", "smtp_security",
                                         "smtp_user", "smtp_password", "email_from")})
    if args.clear_webhook:
        changes["webhook_url"] = ""
    try:
        summary = configure_alerts(changes) if changes else alert_settings_summary()
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    print(f"Webhook: {summary['webhook_host'] or 'not set'}")
    if summary["email_to"]:
        print(f"Email to {', '.join(summary['email_to'])} via {summary['smtp_host']}:"
              f"{summary['smtp_port'] or ('465' if summary['smtp_security'] == 'ssl' else '587')} "
              f"({summary['smtp_security']}), user {summary['smtp_user'] or '-'}, password "
              f"{'set' if summary['smtp_password_set'] else 'not set'}, from "
              f"{summary['email_from'] or summary['smtp_user'] or '(default)'}")
    else:
        print("Email: not set")
    if changes:
        print("Saved. Send a test with `alerts-test`.")
    return 0


def cmd_alerts_test(args) -> int:
    channels = alert_channels()
    if not channels["webhook"] and not channels["email"]:
        print(f"Configuration error: no alert channel set up -- set {ALERT_WEBHOOK_ENV} and/or "
              f"{ALERT_EMAIL_ENV} plus LIS_ALERT_SMTP_HOST in .env.", file=sys.stderr)
        return 1
    text = "Linode Instance Scheduler: test alert -- alerts reach this channel."
    results = {}
    if channels["webhook"]:
        results["webhook"] = _send_webhook(text, "test", {"status": "test"})
    if channels["email"]:
        results["email"] = _send_email(text, "test", {"status": "test"})
    for channel, ok in results.items():
        print(f"{channel}: {'sent' if ok else 'FAILED'}")
    return 0 if all(results.values()) else 1


def cmd_clear_lock(args) -> int:
    def _confirm() -> bool:
        answer = input(f"Clear the lock on '{args.name}' anyway? [y/N] ")
        return answer.strip().lower() == "y"

    try:
        result = clear_instance_lock(
            args.name, confirm=None if args.yes else _confirm, on_progress=print,
        )
    except InstanceLockedError as e:


        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except NotOnboardedError as e:
        print(f"{e}", file=sys.stderr)
        return 1
    if result.outcome == "not_locked":
        print(f"'{args.name}' is not locked -- nothing to do.")
        return 0
    if result.outcome == "aborted_by_user":
        print("Aborted.")
        return 1
    print("Lock cleared.")
    return 0


@dataclass
class DeregisterResult:

    outcome: Literal["not_onboarded", "aborted_by_user", "deregistered"]


def _records_using_vpc_address(
    registry: dict, subnet_id, address: str, *, exclude: str | None = None
) -> list[str]:

    names = []
    for other, rec in registry.items():
        if other == exclude:
            continue
        pairs = engine.vpc_interface_addresses(rec.get("network_config"), rec.get("network_interface_model"))
        if any(sid == subnet_id and addr == address for sid, addr in pairs):
            names.append(other)
    return names


def _local_ipv4_addresses() -> set[str]:

    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {a for a in out.split() if "." in a}


def scheduler_host_addresses(client) -> set[str]:

    local = _local_ipv4_addresses()
    instances = engine.retry_transient(lambda: list(client.linode.instances()))
    vpc_ips = engine.retry_transient(lambda: list(client.vpcs.ips()))
    own = {i.id for i in instances if local & set(i.ipv4 or [])}
    own |= {ip.linode_id for ip in vpc_ips if ip.address in local and ip.linode_id}
    addresses = set(local)
    for inst in instances:
        if inst.id in own:
            addresses |= set(inst.ipv4 or [])
    addresses |= {ip.nat_1_1 for ip in vpc_ips if ip.linode_id in own and getattr(ip, "nat_1_1", None)}
    return addresses


def scheduler_instance(client):

    local = _local_ipv4_addresses()
    instances = engine.retry_transient(lambda: list(client.linode.instances()))
    for inst in instances:
        if local & set(inst.ipv4 or []):
            return inst
    vpc_ips = engine.retry_transient(lambda: list(client.vpcs.ips()))
    ids = {ip.linode_id for ip in vpc_ips if ip.address in local and ip.linode_id}
    return next((i for i in instances if i.id in ids), None)


def set_host_watchdog(client, *, enable: bool | None = True) -> dict:

    inst = scheduler_instance(client)
    if inst is None:
        return {}
    if enable is not None and bool(inst.watchdog_enabled) != enable:
        inst.watchdog_enabled = enable
        engine.retry_transient(inst.save)
        inst = engine.retry_transient(lambda: client.load(Instance, inst.id))
        record_activity(f"Shutdown Watchdog {'enabled' if enable else 'disabled'} on this "
                        f"scheduler's host ({inst.label})", source="system", action="watchdog")
    return {"instance_id": inst.id, "label": inst.label, "watchdog_enabled": bool(inst.watchdog_enabled)}


def _vlan_labels(network_config: list[dict] | None, model: str | None) -> list[str]:
    labels = []
    for iface in network_config or []:
        if model == engine.INTERFACE_MODEL_LEGACY:
            if iface.get("purpose") == "vlan" and iface.get("label"):
                labels.append(iface["label"])
        elif iface.get("vlan") and iface["vlan"].get("vlan_label"):
            labels.append(iface["vlan"]["vlan_label"])
    return labels


def unmanaged_network_neighbours(client, name: str, record: dict, registry: dict) -> list[str]:

    managed = {rec.get("current_linode_id") for rec in registry.values()} | {record.get("current_linode_id")}
    managed.discard(None)
    local = _local_ipv4_addresses()
    instances = {i.id: i for i in engine.retry_transient(lambda: list(client.linode.instances()))}
    own = {iid for iid, inst in instances.items() if local & set(inst.ipv4 or [])}
    findings = []
    pairs = engine.vpc_interface_addresses(record.get("network_config"), record.get("network_interface_model"))
    if pairs:
        live = engine.retry_transient(lambda: list(client.vpcs.ips()))
        own |= {ip.linode_id for ip in live if ip.address in local}
        for subnet_id in sorted({sid for sid, _ in pairs}):
            others = sorted({
                (ip.linode_id, ip.address) for ip in live
                if ip.subnet_id == subnet_id and ip.linode_id and ip.linode_id not in managed
                and ip.linode_id not in own
            })
            if others:
                listed = ", ".join(
                    f"{getattr(instances.get(lid), 'label', lid)} ({addr})" for lid, addr in others
                )
                findings.append(
                    f"VPC subnet {subnet_id} also has instance(s) not managed by this tool: {listed}."
                )
    labels = _vlan_labels(record.get("network_config"), record.get("network_interface_model"))
    if labels:
        region = record.get("region")
        for vlan in engine.retry_transient(lambda: list(client.networking.vlans())):
            vregion = getattr(vlan.region, "id", vlan.region)
            if vlan.label not in labels or (region and vregion != region):
                continue
            others = sorted(lid for lid in (vlan.linodes or []) if lid not in managed and lid not in own)
            if others:
                listed = ", ".join(str(getattr(instances.get(lid), "label", lid)) for lid in others)
                findings.append(
                    f"VLAN '{vlan.label}' also has instance(s) not managed by this tool: {listed}."
                )
    return findings


def _neighbour_warning(name: str, findings: list[str]) -> str:
    return (
        f"WARNING: '{name}' shares its private network with instances this tool doesn't manage:\n  "
        + "\n  ".join(findings)
        + "\nRecommended: give the instances this tool schedules their own VPC subnet and VLAN "
        "label, and don't create other instances there by hand. While a node is stopped, another "
        "instance can be given its VPC address (its next start then moves it to a free one), and "
        "an instance given the same VLAN address conflicts with it silently."
    )


def _move_vpc_address_locked(
    name: str, record: dict, registry: dict, subnet_id, old: str, address: str,
    on_warning: Callable[[str], None] | None,
) -> bool:

    record["network_config"] = engine.replace_vpc_address(
        record["network_config"], record.get("network_interface_model"), old, address,
    )
    _save_one_record(name, record)
    registry[name] = record
    shared = bool(_records_using_vpc_address(registry, subnet_id, old, exclude=name))
    carried = False
    try:
        old_entry = None if shared else engine.read_known_host_entry(old)
        if old_entry:
            moved = "".join(
                f"{address} {line.split(None, 1)[1]}\n"
                for line in old_entry.splitlines() if len(line.split(None, 1)) == 2
            )
            engine.restore_known_host_entry(address, moved)
            carried = True
        else:
            engine.reset_known_host(address)


        if not shared:
            engine.reset_known_host(old)
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"WARNING: could not move the trusted SSH host key from {old} to {address} "
                f"({e}). If the next start reports a host-key problem, run "
                f"`reset-host-key --name {name}` once."
            )
    return carried


@contextmanager
def _vpc_subnet_lock(subnet_id):

    locks_dir = Path(REGISTRY_PATH).parent / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    with open(locks_dir / f"vpc-subnet-{subnet_id}.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _free_vpc_address(registry: dict, subnet_id, current: str, prefix, live: list) -> str | None:

    if not prefix:
        return None
    network = ipaddress.ip_network(f"{current}/{prefix}", strict=False)
    if network.num_addresses > 65536 or network.num_addresses < 8:
        return None
    hosts = list(network.hosts())
    used = {ip.address for ip in live if ip.subnet_id == subnet_id}
    for rec in registry.values():
        for sid, addr in engine.vpc_interface_addresses(
            rec.get("network_config"), rec.get("network_interface_model")
        ):
            if sid == subnet_id:
                used.add(addr)
    for host in reversed(hosts[2:-2]):
        if str(host) not in used:
            return str(host)
    return None


def _reserved_ip_unavailable(client, address: str | None) -> str | None:

    if not address:
        return None
    try:
        ip = engine.retry_transient(lambda: client.get(f"/networking/reserved/ips/{address}"))
    except ApiError as e:
        if e.status == 404:
            return (
                f"reserved IP {address} is no longer reserved on this account, so it can't be given "
                "back to this node. Reserve it again in Cloud Manager (if it's still free), or "
                "re-onboard the node with a new address."
            )
        return None
    except requests.exceptions.RequestException:
        return None
    if not isinstance(ip, dict):
        return None
    holder = ip.get("linode_id")
    if holder:
        label = (ip.get("assigned_entity") or {}).get("label") or holder
        return (
            f"reserved IP {address} is assigned to another instance ({label}). Unassign it in "
            "Cloud Manager (Networking -> Reserved IPs), then start this node again."
        )
    return None


def _reclaim_vpc_addresses_locked(
    client, name: str, record: dict, registry: dict,
    on_progress: Callable[[str], None] | None, on_warning: Callable[[str], None] | None,
) -> bool:

    pairs = engine.vpc_interface_addresses(
        record.get("network_config"), record.get("network_interface_model")
    )
    live = engine.retry_transient(lambda: list(client.vpcs.ips())) if pairs else []
    trust_new_once = False
    for subnet_id, address in pairs:
        holder = next(
            (ip for ip in live if ip.subnet_id == subnet_id and ip.address == address), None
        )
        if holder is None:
            continue


        with _vpc_subnet_lock(subnet_id):
            registry.update({k: v for k, v in load_registry().items() if k != name})
            new_address = _free_vpc_address(
                registry, subnet_id, address, record.get("vpc_prefix"), live,
            )
            if new_address is not None:
                carried = _move_vpc_address_locked(
                    name, record, registry, subnet_id, address, new_address, on_warning,
                )
        if new_address is None:
            if on_warning is not None:
                on_warning(
                    f"WARNING: '{name}''s VPC address {address} is in use by instance "
                    f"{holder.linode_id} and no free address was found to move it to -- the start "
                    "will fail. Free an address in the subnet, or move it with set-vpc-address."
                )
            continue
        trust_new_once = trust_new_once or not carried
        if on_warning is not None:
            on_warning(
                f"WARNING: '{name}''s VPC address {address} was taken by instance "
                f"{holder.linode_id} while it was stopped; moved '{name}' to {new_address}. "
                "Anything that reaches it by its VPC address needs the new one."
            )
        live.append(type("_Held", (), {"subnet_id": subnet_id, "address": new_address})())
    return _first_contact_trust(name, record, registry) or trust_new_once


def _key_material(entry: str | None) -> set[tuple[str, str]]:

    out: set[tuple[str, str]] = set()
    for line in (entry or "").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            out.add((parts[1], parts[2]))
    return out


def _first_contact_trust(name: str, record: dict, registry: dict) -> bool:

    if record.get("reserved_ip"):
        return False
    target = _ssh_target(record, name)
    for subnet_id, address in engine.vpc_interface_addresses(
        record.get("network_config"), record.get("network_interface_model")
    ):
        if address == target and _records_using_vpc_address(registry, subnet_id, address, exclude=name):
            engine.reset_known_host(address)
            return True
    stored = _key_material(engine.read_known_host_entry(target))
    if not stored:
        return True
    for other_name, other in registry.items():
        if other_name == name:
            continue
        try:
            other_target = _ssh_target(other, other_name)
        except Exception:
            continue
        if other_target and other_target != target and stored & _key_material(
            engine.read_known_host_entry(other_target)
        ):
            engine.reset_known_host(target)
            return True
    return False


@_logs_activity("set-vpc-address")
def set_vpc_address(
    client, name: str, address: str, *, current: str | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> str:

    try:
        new_ip = ipaddress.ip_address(address)
    except ValueError as e:
        raise engine.ConfigError(f"{address!r} is not an IP address.") from e
    with _instance_lock(name):
        registry = load_registry()
        record = registry.get(name)
        if record is None:
            raise NotOnboardedError(f"'{name}' is not onboarded.")
        if record.get("current_status") != "stopped":
            raise engine.ConfigError(
                f"'{name}' is {record.get('current_status')!r}; its VPC address can only be "
                "changed while it's stopped (stop it first)."
            )
        model = record.get("network_interface_model")
        pairs = engine.vpc_interface_addresses(record.get("network_config"), model)
        if current is not None:
            pairs = [p for p in pairs if p[1] == current]
        if not pairs:
            raise engine.ConfigError(
                f"'{name}' has no VPC interface" + (f" with address {current}." if current else ".")
            )
        if len(pairs) > 1:
            raise engine.ConfigError(
                f"'{name}' has several VPC interfaces ({', '.join(p[1] for p in pairs)}); say "
                "which one with --current."
            )
        subnet_id, old = pairs[0]
        if old == address:
            return old
        prefix = record.get("vpc_prefix")
        if prefix:
            network = ipaddress.ip_network(f"{old}/{prefix}", strict=False)
            hosts = list(network.hosts()) if network.num_addresses <= 65536 else None
            if new_ip not in network:
                raise engine.ConfigError(f"{address} is not in this instance's subnet {network}.")
            if new_ip in (network.network_address, network.broadcast_address) or (
                hosts and new_ip == hosts[0]
            ):
                raise engine.ConfigError(
                    f"{address} is reserved in {network} (network, gateway or broadcast address)."
                )
        others = _records_using_vpc_address(registry, subnet_id, address, exclude=name)
        if others:
            raise engine.ConfigError(
                f"{address} is already recorded for managed instance(s) {', '.join(others)}."
            )
        try:
            live = engine.retry_transient(lambda: list(client.vpcs.ips()))
        except (ApiError, requests.exceptions.RequestException) as e:
            raise engine.ConfigError(f"could not check which VPC addresses are in use: {e}") from e
        holder = next((ip for ip in live if ip.subnet_id == subnet_id and ip.address == address), None)
        if holder is not None:
            raise engine.ConfigError(
                f"{address} is in use in this subnet right now (instance {holder.linode_id})."
            )
        carried = _move_vpc_address_locked(
            name, record, registry, subnet_id, old, address, on_warning,
        )
        if not carried and on_warning is not None:
            on_warning(
                f"WARNING: no trusted SSH host key could be carried to {address}; the next "
                "start accepts the node's key on first contact."
            )
    osb.sync_object_storage_backup(name, _backup_payload(name, record), on_warning=on_warning)
    return old


@_logs_activity("deregister", name_arg=0)
def deregister_instance(
    name: str, *,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> DeregisterResult:

    with _instance_lock(name):
        registry = load_registry()
        if name not in registry:
            return DeregisterResult(outcome="not_onboarded")
        if on_progress is not None:
            on_progress(
                f"This removes '{name}' from this tool's own tracking only -- nothing on "
                "Linode changes (the instance, its volumes, its reserved IP, and its tags are "
                "all left exactly as they are). Use this when the local record itself is wrong "
                "or unsafe, not as a way to decommission the instance (use offboard for that)."
            )
        if confirm is not None and not confirm():
            return DeregisterResult(outcome="aborted_by_user")
        _delete_one_record(name)
        return DeregisterResult(outcome="deregistered")


def cmd_set_vpc_address(client, args) -> int:
    try:
        old = set_vpc_address(client, args.name, args.address, current=args.current,
                              on_warning=_print_to_stderr)
    except InstanceLockedError as e:
        print(f"{e}", file=sys.stderr)
        return 3
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if old == args.address:
        print(f"'{args.name}' already uses {args.address}.")
    else:
        print(f"'{args.name}' moves from {old} to {args.address} on its next start.")
    return 0


def cmd_deregister(args) -> int:
    def _confirm() -> bool:
        answer = input(f"Remove '{args.name}' from this tool's tracking? [y/N] ")
        return answer.strip().lower() == "y"


    try:
        result = deregister_instance(
            args.name, confirm=None if args.yes else _confirm, on_progress=print,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "not_onboarded":
        print(f"'{args.name}' is not onboarded -- nothing to do.")
        return 0
    if result.outcome == "aborted_by_user":
        print("Aborted.")
        return 1
    print(f"'{args.name}' removed from tracking. The Linode instance itself was not touched.")
    return 0


@dataclass
class ResetHostKeyResult:

    outcome: Literal["aborted_by_user", "reset"]
    confirm_label: str | None = None
    reserved_ip: str | None = None


def reset_host_key(
    name: str | None, ip: str | None, ssh_key: str, *,
    confirm: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ResetHostKeyResult:

    if bool(name) == bool(ip):
        raise engine.ConfigError("pass exactly one of --name or --ip.")

    if name:
        with _instance_lock(name):
            registry = load_registry()
            record = registry.get(name)
            if record is None:
                raise engine.ConfigError(
                    f"'{name}' is not onboarded. Use --ip instead for a node that isn't "
                    "onboarded yet."
                )


            ssh_target = _resolve_ssh_target(
                record.get("reserved_ip"), record["network_config"],
                record["network_interface_model"], subject=name,
            )
            return _reset_host_key_confirm_and_apply(
                confirm_label=name, reserved_ip=ssh_target, ssh_key=ssh_key,
                confirm=confirm, on_progress=on_progress,
            )
    assert ip is not None
    return _reset_host_key_confirm_and_apply(
        confirm_label=ip, reserved_ip=ip, ssh_key=ssh_key,
        confirm=confirm, on_progress=on_progress,
    )


def _reset_host_key_confirm_and_apply(
    *, confirm_label: str, reserved_ip: str, ssh_key: str,
    confirm: Callable[[], bool] | None,
    on_progress: Callable[[str], None] | None,
) -> ResetHostKeyResult:
    if on_progress is not None:
        on_progress(f"This will forget the SSH host key currently trusted for '{confirm_label}' "
                    f"({reserved_ip}) and trust whatever key it presents next.")
        on_progress("Only do this after independently confirming the new key is legitimate "
                    "(e.g. via Cloud Manager's Lish console) -- this is exactly the check that "
                    "would otherwise catch a MITM or a wrong-disk mistake, so don't bypass it "
                    "casually.")
    if confirm is not None and not confirm():
        return ResetHostKeyResult(outcome="aborted_by_user")


    with _instance_lock(f"known-host-{reserved_ip}"):


        had_backup = engine.read_known_host_entry(reserved_ip) is not None
        try:
            engine.reset_and_reestablish_trust(reserved_ip, ssh_key)
        except (ApiError, RuntimeError) as e:
            raise engine.ConfigError(
                f"could not reach '{confirm_label}' to establish new trust: {e}."
                + (" The previously-trusted key was restored -- nothing was left in a worse "
                   "state than before this command ran." if had_backup else " There was no "
                   "previous entry to restore -- nothing is trusted for this address yet.")
            ) from e
        return ResetHostKeyResult(outcome="reset", confirm_label=confirm_label, reserved_ip=reserved_ip)


def cmd_reset_host_key(args) -> int:
    def _confirm() -> bool:
        label = args.name if args.name else args.ip
        answer = input(f"Type '{label}' to confirm: ")
        return answer == label

    try:
        result = reset_host_key(
            args.name, args.ip, args.ssh_key,
            confirm=None if args.yes else _confirm,
            on_progress=print,
        )
    except InstanceLockedError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.outcome == "aborted_by_user":
        print("Aborted -- name didn't match.")
        return 1
    print(f"New host key for '{result.confirm_label}' ({result.reserved_ip}) is now trusted.")
    return 0


@dataclass
class RebuildResult:

    nothing_found: bool = False
    scanned_count: int = 0
    recovered: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    incomplete: list[tuple[str, dict]] = field(default_factory=list)
    ambiguous: list[tuple[str, dict]] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)

    pending: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:

        return not (self.failed or self.incomplete or self.ambiguous or self.invalid or self.conflicts)


def _restore_groups_from_object_storage(
    client, groups_from_tags: set[str],
    on_progress: Callable[[str], None] | None,
    on_warning: Callable[[str], None] | None,
) -> dict[str, set[str]]:

    dependencies: dict[str, set[str]] = {}
    names = osb.list_group_backups()
    if names is None:
        if osb.is_configured() and on_warning is not None:
            on_warning("  WARNING: couldn't list group records in Object Storage -- only groups "
                       "with surviving members were recovered; re-run rebuild to retry.")
        return dependencies
    for group_name in names:
        try:
            record = osb.download_group_backup(group_name)
            if record is None:
                if on_warning is not None:
                    on_warning(f"  WARNING: group '{group_name}''s Object Storage record is "
                               "missing or failed verification -- not restored from it.")
                continue
            if group_name not in groups_from_tags:
                dependencies[group_name] = set(normalize_dependency_list(
                    record.get("depends_on")
                ))
            if get_schedule_group(group_name) is not None:
                continue
            timezone = record.get("timezone") or "UTC"
            rules = record.get("rules") or []
            if rules:
                _validate_schedule_rules(rules)
            create_schedule_group(group_name, timezone)
            if rules:
                _set_group_schedule_row(group_name, timezone, rules, bool(record.get("enabled", True)))
            elif not record.get("enabled", True):
                _set_group_schedule_row(group_name, timezone, [], False)
            _restore_group_account_holidays(group_name, record.get("account_holidays"))
            if on_progress is not None:
                on_progress(f"  recreated group '{group_name}' from Object Storage.")
            hooks = record.get("hooks")
            if hooks and any(hooks.values()):
                set_group_hooks(group_name, hooks, actor="rebuild", client=client,
                                on_warning=on_warning)
                if on_progress is not None:
                    on_progress(f"  restored group '{group_name}''s hooks from Object Storage.")
        except Exception as e:
            if on_warning is not None:
                on_warning(f"  WARNING: could not restore group '{group_name}' from Object "
                           f"Storage: {e} -- recreate it with group-create.")
    return dependencies


def _restore_group_dependencies(
    client, from_tags: dict[str, list[set[str]]], from_objects: dict[str, set[str]],
    on_progress: Callable[[str], None] | None,
    on_warning: Callable[[str], None] | None,
) -> None:

    wanted: dict[str, tuple[set[str], str]] = {}
    for group_name, member_sets in from_tags.items():
        union = set().union(*member_sets) if member_sets else set()
        if any(m != union for m in member_sets) and on_warning is not None:
            on_warning(f"  WARNING: group '{group_name}''s members disagree about its "
                       f"dependencies -- restoring all of them ({', '.join(sorted(union))}); "
                       "check with group-show.")
        wanted[group_name] = (union, "tags")
    for group_name, deps in from_objects.items():
        wanted.setdefault(group_name, (deps, "Object Storage"))
    for group_name, (deps, source) in sorted(wanted.items()):
        if not deps:
            continue
        try:
            group = get_schedule_group(group_name)
            if group is None or group.get("depends_on"):
                continue
            missing = sorted(d for d in deps if get_schedule_group(d) is None)
            present = sorted(d for d in deps if d not in missing)
            if missing and on_warning is not None:
                on_warning(
                    f"  WARNING: group '{group_name}' depended on {', '.join(missing)}, which "
                    "could not be recovered -- recreate it and run group-depends --group-name "
                    f"{group_name} --add <group>."
                )
            if not present:
                continue
            set_group_dependencies(client, group_name, present, on_warning=on_warning)
            if on_progress is not None:
                on_progress(f"  restored group '{group_name}''s dependencies on "
                            f"{', '.join(present)} from {source}.")
        except Exception as e:
            if on_warning is not None:
                on_warning(
                    f"  WARNING: could not restore group '{group_name}''s dependencies "
                    f"({', '.join(sorted(deps))}): {e} -- set them again with group-depends."
                )


def _discover_from_object_storage(
    client, found: dict[str, dict], on_warning: Callable[[str], None] | None,
) -> list[str]:

    names = osb.list_instance_backups() if osb.is_configured() else None
    added: list[str] = []
    for name in names or []:
        partial = found.get(name)


        if partial is not None and (partial.get("os_volume_id_candidates") or partial.get("os_volume_id")):
            continue
        try:
            engine.validate_instance_name(name)
            record = osb.download_instance_backup(name)
            os_id = (record or {}).get("os_volume_id")
            if not isinstance(os_id, int):
                continue
            try:
                volume = engine.retry_transient(lambda os_id=os_id: client.load(Volume, os_id))
            except ApiError as e:
                if e.status == 404:
                    continue
                raise
            owners = set(engine.names_from_tags(volume.tags))
            if owners - {name}:
                continue
            data_ids = []
            for dv in (record or {}).get("data_volumes") or []:
                vid = dv.get("volume_id") if isinstance(dv, dict) else None
                if not isinstance(vid, int):
                    continue
                with contextlib_suppress(ApiError):
                    dvol = engine.retry_transient(lambda vid=vid: client.load(Volume, vid))
                    if not set(engine.names_from_tags(dvol.tags)) - {name}:
                        data_ids.append(vid)
            ip = (record or {}).get("reserved_ip")
            if ip:
                try:
                    rip = engine.retry_transient(
                        lambda ip=ip: client.load(engine.ReservedIPAddress, ip))
                    if set(engine.names_from_tags(getattr(rip, "tags", None))) - {name}:
                        ip = None
                except ApiError:
                    ip = None
            region = getattr(volume, "region", None)
            region_id = getattr(region, "id", region)
            if partial is not None:

                data_ids = sorted(set(data_ids) | set(partial.get("data_volume_ids") or []))
                ip = partial.get("reserved_ip") or ip
            found[name] = {
                "os_volume_id": os_id, "os_volume_id_candidates": [os_id],
                "data_volume_ids": sorted(data_ids), "reserved_ip": ip,
                "reserved_ip_candidates": [ip] if ip else [],
                "region": region_id, "region_candidates": [region_id] if region_id else [],
            }
            added.append(name)
            if on_warning is not None:
                on_warning(
                    f"  WARNING: '{name}' had no recovery tags left; found through its Object "
                    "Storage record instead (its tags are written again on its next start/stop).")
        except Exception as e:
            if on_warning is not None:
                on_warning(f"  WARNING: could not check Object Storage record '{name}': {e}")
    return added


def rebuild_instances(
    client, ssh_key: str, *,
    vpc_id: int | None = None, force: bool = False,
    on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> RebuildResult:

    registry = load_registry()
    try:
        found, conflicts = engine.find_managed_resources_by_tag(client)
    except (ApiError, requests.exceptions.RequestException) as e:


        raise engine.ConfigError(f"could not scan tagged resources: {e}") from e
    _discover_from_object_storage(client, found, on_warning)
    pending_names: set[str] = set()
    try:
        pending_names = restore_pending_deletions_from_object_storage(on_progress, on_warning)
    except Exception as e:
        if on_warning is not None:
            on_warning(f"WARNING: couldn't restore pending deletions from Object Storage ({e}).")
    if not found and not conflicts:
        return RebuildResult(nothing_found=True)

    recovered, partial, skipped, incomplete, failed, invalid = [], [], [], [], [], []
    pending_found: list[str] = []
    ambiguous: list[tuple[str, dict]] = []


    pending_dependencies: dict[str, list[set[str]]] = {}

    for name, resources in found.items():


        try:
            engine.validate_instance_name(name)
        except argparse.ArgumentTypeError as e:
            if on_warning is not None:
                on_warning(f"  WARNING: skipping a tag-derived name that fails validation: {e}")
            invalid.append(name)
            continue

        if name in pending_names and name not in registry:


            pending_found.append(name)
            continue
        if name in registry and not force:
            skipped.append(name)
            continue


        if len(resources.get("os_volume_id_candidates", [])) > 1 or \
                len(resources.get("reserved_ip_candidates", [])) > 1 or \
                len(resources.get("region_candidates", [])) > 1:
            ambiguous.append((name, resources))
            continue


        if resources.get("os_volume_id") is None:
            incomplete.append((name, resources))
            continue


        try:
            with _instance_lock(name):


                fresh_registry = load_registry()
                if name in fresh_registry and not force:
                    if on_warning is not None:
                        on_warning(
                            f"  WARNING: '{name}' was onboarded by a concurrent process while "
                            "this scan was running -- skipping to avoid overwriting it (re-run "
                            "with --force if you actually want rebuild's reconstruction to "
                            "win)."
                        )
                    skipped.append(name)
                    continue
                record = _rebuild_one_record(
                    client, name, resources, ssh_key=ssh_key, vpc_id=vpc_id
                )


                existing = fresh_registry.get(name)
                if existing is not None and record.get("group_id") is None:
                    record["group_id"] = existing.get("group_id")
                _save_one_record(name, record)

                if record.get("current_status") == "running" and on_warning is not None:


                    on_warning(
                        f"  WARNING: '{name}' recovered as running, but any active manual-"
                        "override auto-revert timer it had could not be recovered (this isn't "
                        "tracked in Linode's own tags) -- if it was manually started outside its "
                        "schedule's on-window, run `extend`/`stop` for it manually as needed."
                    )


                try:
                    os_volume = engine.retry_transient(
                        lambda vol_id=record["os_volume_id"]: client.load(Volume, vol_id)
                    )
                except Exception as e:
                    os_volume = None
                    if on_warning is not None:
                        on_warning(
                            f"  WARNING: '{name}' recovered, but its OS volume's tags could "
                            f"not be read to check for a schedule or group membership ({e}) -- "
                            "re-run schedule-set/group-add for it manually if it had either."
                        )

                    try:
                        _restore_individual_schedule(name, None, on_progress, on_warning)
                    except Exception as e2:
                        if on_warning is not None:
                            on_warning(f"  WARNING: '{name}': schedule not restored ({e2}).")

                if os_volume is not None:
                    try:
                        _restore_individual_schedule(name, os_volume.tags, on_progress, on_warning)
                    except Exception as e:

                        if on_warning is not None:
                            on_warning(
                                f"  WARNING: '{name}' recovered, but its individual schedule "
                                f"tags could not be restored: {e} -- re-run schedule-set for it "
                                "manually if it had one."
                            )

                    try:
                        group_name = next(
                            (
                                t[len(_GROUP_NAME_TAG_PREFIX):] for t in (os_volume.tags or [])
                                if t.startswith(_GROUP_NAME_TAG_PREFIX)
                            ),
                            None,
                        )
                        if group_name is not None:
                            existing_group = get_schedule_group(group_name)
                            if existing_group is not None:
                                group_id = existing_group["id"]
                            else:


                                snapshot = _decode_schedule_from_tags(
                                    os_volume.tags, prefix=_GROUP_SCHEDULE_TAG_PREFIX,
                                    allow_no_rules=True,
                                )
                                if snapshot is None:
                                    raise engine.ConfigError(
                                        f"'{name}' is tagged for group '{group_name}', but no "
                                        "group-schedule snapshot was found in its tags to "
                                        "recreate the group from."
                                    )


                                if snapshot["rules"]:
                                    _validate_schedule_rules(snapshot["rules"])
                                group_id = create_schedule_group(group_name, snapshot["timezone"])
                                if snapshot["rules"]:
                                    _set_group_schedule_row(
                                        group_name, snapshot["timezone"], snapshot["rules"],
                                        snapshot["enabled"],
                                    )
                                if _GROUP_HOLIDAY_IGNORE_TAG in (os_volume.tags or []):
                                    _restore_group_account_holidays(group_name, "ignore")
                                if on_progress is not None:
                                    on_progress(
                                        f"  recreated group '{group_name}' from '{name}''s tags."
                                    )

                            def _assign_group(group_id=group_id, name=name):
                                conn = _connect()
                                try:
                                    conn.execute(
                                        "UPDATE instances SET group_id = ? WHERE name = ?",
                                        (group_id, name),
                                    )
                                    conn.commit()
                                finally:
                                    conn.close()

                            _retry_db(_assign_group)
                            if on_progress is not None:
                                on_progress(
                                    f"  restored '{name}''s membership in group "
                                    f"'{group_name}' from tags."
                                )


                            pending_dependencies.setdefault(group_name, []).append({
                                t[len(_GROUP_DEP_TAG_PREFIX):]
                                for t in (os_volume.tags or [])
                                if t.startswith(_GROUP_DEP_TAG_PREFIX)
                            })
                    except Exception as e:
                        if on_warning is not None:
                            on_warning(
                                f"  WARNING: '{name}' recovered, but its group membership tags "
                                f"could not be restored: {e} -- re-run group-add for it "
                                "manually if it belonged to a group."
                            )
                if os_volume is not None:
                    try:
                        vol_tags = os_volume.tags if isinstance(os_volume.tags, list) else []
                        if _SCHEDULE_MODE_MANUAL_TAG in vol_tags and name not in (
                            get_schedule_modes()
                        ):
                            _write_schedule_mode_row(name, "manual")
                            if on_progress is not None:
                                on_progress(f"  restored '{name}' as manual-only from tags.")
                        own_hol = next((t[len(_HOLIDAY_SETTING_TAG_PREFIX):] for t in vol_tags
                                        if t.startswith(_HOLIDAY_SETTING_TAG_PREFIX)), None)
                        if (own_hol in ACCOUNT_HOLIDAY_MODES
                                and get_instance_account_holidays(name) is None):
                            _write_instance_account_holidays_row(name, own_hol)
                            if on_progress is not None:
                                on_progress(f"  restored '{name}''s holiday setting "
                                            f"({own_hol} account-wide holidays) from tags.")
                    except Exception as e:
                        if on_warning is not None:
                            on_warning(
                                f"  WARNING: '{name}' was manual-only, but that couldn't be "
                                f"restored: {e} -- run `set-mode --name {name} --manual`."
                            )


                try:
                    record["group_id"] = (load_registry().get(name) or {}).get("group_id")
                    found_in_tags = _restore_hooks_from_tags(
                        client, name, record, on_progress, on_warning,
                    )
                    if not found_in_tags:
                        _restore_instance_hooks_from_backup(name, on_warning)
                except Exception as e:
                    if on_warning is not None:
                        on_warning(
                            f"  WARNING: '{name}' recovered, but its hooks could not be "
                            f"restored: {e} -- set them again with hooks-set."
                        )


                due = None
                with contextlib_suppress(Exception):
                    os_vol_id = record["os_volume_id"]
                    os_vol = engine.retry_transient(
                        lambda: client.load(Volume, os_vol_id))
                    due = pending_marker_from_tags(os_vol.tags, name)
                if due is not None:
                    _rebuilt_node_to_pending(client, name, load_registry().get(name) or record, due)
                    if on_progress is not None:
                        on_progress(f"  '{name}' is pending deletion (due {due}) -- restored as a "
                                    "pending deletion; recover it from Pending deletion if needed")
                    pending_found.append(name)
                    continue
        except InstanceLockedError as e:
            if on_warning is not None:
                on_warning(f"  WARNING: {e} -- skipping '{name}' this pass, safe to re-run "
                           "`rebuild` once it's finished.")
            failed.append(name)
            continue
        except (ApiError, RuntimeError, engine.ConfigError, requests.exceptions.RequestException) as e:


            if on_warning is not None:
                on_warning(f"  WARNING: failed to rebuild '{name}': {e} -- skipping, "
                           "other names are unaffected.")
            failed.append(name)
            continue

        registry[name] = record


        if record["current_status"] in ("running", "stopped"):
            recovered.append(name)
        else:
            partial.append(name)

    object_dependencies = _restore_groups_from_object_storage(
        client, set(pending_dependencies), on_progress, on_warning,
    )
    _restore_group_dependencies(
        client, pending_dependencies, object_dependencies, on_progress, on_warning,
    )
    try:
        restore_api_tokens_from_object_storage(on_progress=on_progress, on_warning=on_warning)
    except Exception as e:
        if on_warning is not None:
            on_warning(f"WARNING: couldn't reconcile API tokens from Object Storage ({e}).")
    try:
        restore_holidays_from_object_storage(on_progress=on_progress, on_warning=on_warning)
    except Exception as e:
        if on_warning is not None:
            on_warning(f"WARNING: couldn't restore holidays from Object Storage ({e}).")
    try:
        restore_migration_backups_from_object_storage(on_progress=on_progress,
                                                      on_warning=on_warning)
        for backup_name, backup_id, backup_label in untracked_backup_instances(client):
            if on_warning is not None:
                on_warning(f"WARNING: instance {backup_id} ('{backup_label}') is tagged as the "
                           f"pre-migration backup of '{backup_name}' but no record of it was "
                           "recovered. It still bills; keep it or delete it in Cloud Manager.")
    except Exception as e:
        if on_warning is not None:
            on_warning(f"WARNING: couldn't check pre-migration backups ({e}).")

    if on_progress is not None:
        on_progress(f"Scanned tags: {len(found)} name(s) found.")
        if recovered:
            on_progress(f"  fully recovered: {', '.join(recovered)}")
        if partial:
            on_progress(
                f"  partially recovered (was stopped, needs manual recovery): "
                f"{', '.join(partial)}"
            )
            on_progress("    -- boot each of these manually once via Cloud Manager from its "
                        "os_volume_id, using its network_config from Cloud Manager's own UI, "
                        "then re-run `onboard` to fully restore management.")
        if skipped:
            on_progress(f"  skipped (already in local registry, use --force to overwrite): "
                        f"{', '.join(skipped)}")
        if failed:
            on_progress(f"  FAILED to rebuild (see warnings above, safe to re-run `rebuild` "
                        f"later for just these): {', '.join(failed)}")
        if incomplete:
            for name, resources in incomplete:
                on_progress(
                    f"  WARNING: '{name}' has incomplete tags (found: {resources}) -- skipped."
                )
        if ambiguous:
            for name, resources in ambiguous:
                on_progress(
                    f"  WARNING: '{name}' has AMBIGUOUS tags -- multiple candidates found "
                    f"(os_volume_id_candidates={resources.get('os_volume_id_candidates')}, "
                    f"reserved_ip_candidates={resources.get('reserved_ip_candidates')}, "
                    f"region_candidates={resources.get('region_candidates')}) -- skipped, "
                    "not guessed. This usually means a stale tag was left on an old resource "
                    "(e.g. before a forced re-onboard, or an out-of-band change), or the "
                    "resources for this name genuinely span more than one region (should never "
                    "happen -- investigate). Review tags directly in Cloud Manager and remove "
                    "the stale one, then re-run rebuild."
                )
        if pending_found:
            on_progress(f"  pending deletion (kept as such, recoverable from Pending deletion): "
                        f"{', '.join(pending_found)}")
        if invalid:
            on_progress(
                f"  WARNING: skipped {len(invalid)} tag-derived name(s) that failed validation "
                f"(see warnings above): {invalid!r}. Check the raw tags on your Linode "
                "resources (Cloud Manager) for how these got there."
            )
        if conflicts:
            on_progress(
                f"  WARNING: {len(conflicts)} resource(s) are ambiguously tagged and were "
                "excluded from every name's candidate set entirely (not just left ambiguous "
                "for one name):"
            )
            for c in conflicts:
                if c["reason"] == "multiple_names":
                    on_progress(f"    {c['kind']} {c['id']}: tagged for more than one name at "
                                f"once: {c['names']!r}")
                else:
                    on_progress(f"    {c['kind']} {c['id']}: carries both the OS and data role "
                                f"tags at once (names: {c['names']!r})")
            on_progress("    Review tags directly in Cloud Manager and remove whichever is "
                        "stale, then re-run rebuild.")

    return RebuildResult(
        scanned_count=len(found), recovered=recovered, partial=partial, skipped=skipped,
        failed=failed, incomplete=incomplete, ambiguous=ambiguous, invalid=invalid,
        conflicts=conflicts, pending=pending_found,
    )


def cmd_rebuild(client, args) -> int:
    try:
        result = rebuild_instances(
            client, args.ssh_key, vpc_id=args.vpc_id, force=args.force,
            on_progress=print, on_warning=_print_to_stderr,
        )
    except engine.ConfigError as e:


        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    if result.nothing_found:
        print("No tagged resources found on this account -- nothing to rebuild.")
        return 0
    return 0 if result.ok else 1


def _schedule_from_object_storage(name: str) -> dict | None:

    if not osb.is_configured():
        return None
    try:
        backup = osb.download_instance_backup(name)
    except Exception:
        return None
    sched = (backup or {}).get("schedule")
    if not isinstance(sched, dict):
        return None
    return {"timezone": sched.get("timezone"), "rules": sched.get("rules"),
            "enabled": bool(sched.get("enabled", True))}


def _restore_individual_schedule(
    name: str, tags: list | None,
    on_progress: Callable[[str], None] | None, on_warning: Callable[[str], None] | None,
) -> None:

    problems: list[str] = []
    candidates = [("tags", lambda: _decode_schedule_from_tags(tags)),
                  ("Object Storage", lambda: _schedule_from_object_storage(name))]
    for source, load in candidates:
        if source == "Object Storage" and get_instance_schedule(name) is not None:
            return
        try:
            decoded = load()
            if decoded is None:
                continue
            _validate_schedule_timezone(decoded["timezone"])
            _validate_schedule_rules(decoded["rules"])
        except Exception as e:
            problems.append(f"{source}: {e}")
            continue
        _save_schedule_row(name, decoded["timezone"], decoded["rules"], decoded["enabled"])
        if on_progress is not None:
            on_progress(f"  restored schedule for '{name}' from {source}.")
        if problems and on_warning is not None:
            on_warning(f"  WARNING: ignored an unusable schedule copy for '{name}' ({'; '.join(problems)}).")
        return
    if problems:
        raise engine.ConfigError("; ".join(problems))

def _rebuild_one_record(
    client, name: str, resources: dict, *, ssh_key: str, vpc_id: int | None = None
) -> dict:


    reserved_ip = resources.get("reserved_ip")
    if reserved_ip is not None:
        ip_info = engine.retry_transient(
            lambda: engine.get_ip_details(client, reserved_ip)
        )
        linode_id = ip_info.get("linode_id")
    else:


        os_volume = engine.retry_transient(
            lambda: client.load(Volume, resources["os_volume_id"])
        )
        linode_id = os_volume.linode_id

    if linode_id:
        instance = engine.retry_transient(lambda: client.load(Instance, linode_id))


        configs = list(instance.configs)
        captured_network = engine.capture_network_config(instance, configs=configs)


        live_sda = configs[0].devices.dict.get("sda")


        if not live_sda or not live_sda.get("filesystem_path"):
            raise engine.ConfigError(
                f"'{name}': live instance {linode_id}'s /dev/sda is not a Block Storage "
                "volume -- can't establish os_volume_id from the live boot config."
            )
        os_volume_id = live_sda["id"]
        if os_volume_id != resources["os_volume_id"]:
            raise engine.ConfigError(
                f"'{name}': the OS volume tagged for this name ({resources['os_volume_id']}) "
                f"doesn't match what's actually attached at /dev/sda on the live instance "
                f"({os_volume_id}) -- most likely an out-of-band boot-volume swap. Refusing "
                "to guess which one is right; reconcile manually (Cloud Manager: which "
                "volume should carry this name's disaster-recovery tags) before re-running "
                "rebuild."
            )


        vpc_prefix = _derive_vpc_prefix(
            client, captured_network["network_config"],
            captured_network["network_interface_model"], vpc_id=vpc_id,
            cannot_determine=lambda msg: engine.ConfigError(
                f"'{name}' {msg} Re-run rebuild with --vpc-id."
            ),
        )

        data_volumes = engine.capture_data_volumes(instance, os_volume_id, configs=configs)


        authorized_keys_raw = engine.ssh_run(
            _resolve_ssh_target(
                reserved_ip, captured_network["network_config"],
                captured_network["network_interface_model"], subject=name,
            ),
            ssh_key,
            "cat /root/.ssh/authorized_keys 2>/dev/null",
            trust_new=True,
        )
        authorized_keys = [
            line.strip() for line in authorized_keys_raw.splitlines() if line.strip()
        ]
        if not authorized_keys:


            raise engine.ConfigError(
                f"'{name}' -- could not read any authorized_keys from the instance -- "
                "refusing to recover a record without knowing how the next start would "
                "grant SSH access."
            )
        instance_attrs = engine.capture_instance_attributes(instance)


        engine.tag_managed_resources(
            client, name, os_volume_id=os_volume_id,
            data_volume_ids=[dv["volume_id"] for dv in data_volumes],
            reserved_ip=resources["reserved_ip"],
        )
        return {
            "name": name,
            "region": resources["region"],
            "os_volume_id": os_volume_id,
            "reserved_ip": resources["reserved_ip"],
            "network_interface_model": captured_network["network_interface_model"],
            "network_config": captured_network["network_config"],
            "network_helper_enabled": captured_network["network_helper_enabled"],
            "vpc_prefix": vpc_prefix,
            "data_volumes": data_volumes,
            "authorized_keys": authorized_keys,
            "label": instance.label,
            "tags": list(instance.tags),
            "instance_attrs": instance_attrs,
            "current_linode_id": linode_id,
            "current_status": "running",
            "transitioning": False,
        }


    slot_names = engine.DATA_VOLUME_DEVICE_SLOTS
    data_volume_ids = resources["data_volume_ids"]
    if len(data_volume_ids) > len(slot_names):
        raise engine.ConfigError(
            f"'{name}' has {len(data_volume_ids)} data volumes, more than the "
            f"{len(slot_names)} non-OS device slots a boot config supports -- refusing "
            "to silently drop any of them from the reconstructed record."
        )
    record = {
        "name": name,
        "region": resources["region"],
        "os_volume_id": resources["os_volume_id"],
        "reserved_ip": resources["reserved_ip"],
        "data_volumes": [
            {"volume_id": vid, "device_slot": slot}


            for vid, slot in zip(resources["data_volume_ids"], slot_names)
        ],
        "current_linode_id": None,
        "current_status": "needs_manual_recovery",
        "transitioning": False,
    }


    try:
        os_volume = engine.retry_transient(
            lambda: client.load(Volume, resources["os_volume_id"])
        )


        os_volume_tags = os_volume.tags if isinstance(os_volume.tags, list) else []
    except Exception:
        os_volume_tags = []

    simple_network = _decode_simple_network_config_from_tags(os_volume_tags)
    if simple_network is not None:
        record.update(simple_network)

    tag_ssh_key_ids = _decode_ssh_key_ids_from_tags(os_volume_tags)
    tag_authorized_keys = (
        _resolve_ssh_key_ids_to_content(client, tag_ssh_key_ids) if tag_ssh_key_ids else []
    )

    backup = osb.download_instance_backup(name)

    if simple_network is None and backup is not None:
        record["network_interface_model"] = backup.get("network_interface_model")
        record["network_config"] = backup.get("network_config")
        record["network_helper_enabled"] = backup.get("network_helper_enabled")

    merged_keys = list(tag_authorized_keys)
    if backup is not None:


        for key in backup.get("authorized_keys") or []:
            if key not in merged_keys:
                merged_keys.append(key)
    if merged_keys:
        record["authorized_keys"] = merged_keys

    if backup is not None:
        record["vpc_prefix"] = backup.get("vpc_prefix")
        record["label"] = backup.get("label")
        record["tags"] = backup.get("tags")
        record["instance_attrs"] = backup.get("instance_attrs")


    has_network = (
        record.get("network_interface_model") is not None
        and record.get("network_config") is not None
        and (
            record.get("network_interface_model") != "legacy_config"
            or record.get("network_helper_enabled") is not None
        )
    )
    if has_network and record.get("authorized_keys"):
        record["current_status"] = "stopped"

    return record


DEFAULT_SESSION_LIFETIME_HOURS = 24.0


def create_api_session(
    linode_username: str, hours: float = DEFAULT_SESSION_LIFETIME_HOURS
) -> dict:

    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires_at = (now + timedelta(hours=hours)).isoformat()

    def _do():
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO api_sessions (token, linode_username, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (token, linode_username, now.isoformat(), expires_at),
            )
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)
    return {"token": token, "expires_at": expires_at}


def get_api_session(token: str) -> str | None:

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                "SELECT linode_username, expires_at FROM api_sessions WHERE token = ?", (token,)
            ).fetchone()
        finally:
            conn.close()

    row = _retry_db(_do)
    if row is None:
        return None
    linode_username, expires_at = row
    if datetime.now(UTC) >= datetime.fromisoformat(expires_at):
        return None
    return linode_username


def delete_api_session(token: str) -> None:

    def _do():
        conn = _connect()
        try:
            conn.execute("DELETE FROM api_sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()

    _retry_db(_do)


API_TOKEN_FINE_SCOPES: dict[str, str] = {
    "instances:list": "list instances",
    "instances:status": "instance status (and migration status)",
    "instances:history": "start/stop history",
    "savings:read": "savings for instances and groups",
    "activity:read": "activity log",
    "instances:start": "start an instance",
    "instances:stop": "stop an instance",
    "instances:extend": "extend a manual-override window",
    "instances:clear-lock": "clear a stuck in-progress mark (only when no start/stop is running)",
    "schedules:catch-up": "start/stop nodes that missed their schedule (after review)",
    "system:read": "backup, key-protection, restore-test, lease, watchdog and alert status",
    "system:backup": "run a backup, a restore test, the settings backup, key protection",
    "system:configure": "heal, take over the scheduler lease, Shutdown Watchdog, the age key for "
                        "the settings backup (credentials -- Object Storage, SMTP -- stay admin-only)",
    "groups:start": "start a whole group",
    "groups:stop": "stop a whole group",
    "schedules:read": "read an instance's schedule",
    "schedules:write": "set or clear an instance's schedule",
    "groups:read": "list and show groups",
    "groups:write": "create/delete groups and set a group's schedule",
    "groups:membership": "add an instance to a group or remove it",
    "dependencies:write": "set a group's start order",
    "mode:write": "switch an instance between manual-only and schedulable",
    "hooks:read": "read hooks and hook run history",
    "hooks:write": "set or clear hooks (they run as root)",
    "hooks:run": "run a hook now",
    "instances:onboard": "list account instances, reserve an IP and onboard",
    "instances:migrate": "move an instance off local disk (migrate-start/resume/status)",
    "instances:offboard": "offboard or deregister an instance",
    "instances:vpc-address": "move a stopped instance to another VPC address",
    "tokens:manage": "create, list and revoke API tokens (never wider than its own)",
    "logs:read": "service log files",
    "deletions:read": "list nodes and backups pending deletion",
    "deletions:recover": "recover a node or backup pending deletion",
    "deletions:delete-now": "carry out a pending deletion now (it can't be recovered afterwards)",
    "backups:read": "list database snapshots and preview an item in one",
    "backups:restore-item": "restore one schedule, instance's hooks or group from a snapshot",
    "backups:release-copy": "save a copy of the installed release to Object Storage",
    "backups:versioning": "turn on bucket versioning, list and bring back object versions",
    "backups:drill": "run a recovery drill (creates and deletes a throwaway VM)",
    "credentials:rotate": "rotate the Linode API token, bucket keys, OAuth secret or deployment "
                          "SSH key (admin-level: it replaces credentials)",
}
API_TOKEN_SCOPE_BUNDLES: dict[str, tuple[str, ...]] = {
    "read": ("instances:list", "instances:status", "instances:history", "savings:read",
             "activity:read", "schedules:read", "groups:read", "hooks:read", "deletions:read",
             "backups:read", "system:read"),
    "operate": ("instances:start", "instances:stop", "instances:extend", "groups:start",
                "groups:stop", "hooks:run", "instances:clear-lock", "schedules:catch-up",
                "system:backup", "backups:release-copy"),
    "configure": ("schedules:write", "groups:write", "groups:membership", "dependencies:write",
                  "mode:write", "instances:vpc-address", "deletions:recover",
                  "backups:restore-item", "backups:versioning", "backups:drill",
                  "system:configure"),


    "admin": tuple(API_TOKEN_FINE_SCOPES),
}
API_TOKEN_SCOPES = tuple(API_TOKEN_SCOPE_BUNDLES) + tuple(API_TOKEN_FINE_SCOPES)


def expand_token_scopes(scopes: list[str] | tuple[str, ...]) -> set[str]:

    granted: set[str] = set()
    for scope in scopes:
        granted.update(API_TOKEN_SCOPE_BUNDLES.get(scope, (scope,)))
    return granted
_API_TOKEN_PREFIX = "lis_"
_API_TOKEN_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")


def _hash_api_token(token: str) -> str:


    return hashlib.sha256(token.encode()).hexdigest()


def _token_row_to_dict(row: tuple) -> dict:
    (name, prefix, scopes, instances, groups, created_by, created_at, expires_at, revoked_at,
     last_used_at) = row
    return {
        "name": name, "token_prefix": prefix, "scopes": json.loads(scopes),
        "instances": json.loads(instances) if instances else None,
        "groups": json.loads(groups) if groups else None,
        "created_by": created_by, "created_at": created_at, "expires_at": expires_at,
        "revoked_at": revoked_at, "last_used_at": last_used_at,
    }


_TOKEN_COLUMNS = ("name, token_prefix, scopes, instances, groups, created_by, created_at,"
                  " expires_at, revoked_at, last_used_at")


_TOKEN_RECORD_FIELDS = ("name", "token_hash", "token_prefix", "scopes", "instances", "groups",
                        "created_by", "created_at", "expires_at", "revoked_at")


def _token_record(name: str) -> dict | None:

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                f"SELECT {', '.join(_TOKEN_RECORD_FIELDS)} FROM api_tokens WHERE name = ?", (name,)
            ).fetchone()
        finally:
            conn.close()

    row = _retry_db(_do)
    return dict(zip(_TOKEN_RECORD_FIELDS, row, strict=True)) if row else None


def _sync_token_record(name: str, on_warning: Callable[[str], None] | None) -> bool:

    if not osb.is_configured():
        return True
    try:
        record = _token_record(name)
        if record is not None:
            osb.upload_token_record(name, record)
        return True
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"WARNING: could not back up API token '{name}' to Object Storage ({e}). The change "
                "is in effect now, but a restore from an older database snapshot wouldn't know "
                f"about it -- run `backup` (or `api-token-revoke --name {name}` again for a "
                "revocation) to retry."
            )
        return False


def create_api_token(
    name: str, scopes: list[str], *, instances: list[str] | None = None,
    groups: list[str] | None = None, expires_days: float | None = None,
    created_by: str | None = None, on_warning: Callable[[str], None] | None = None,
) -> dict:

    if not _API_TOKEN_NAME_RE.fullmatch(name or ""):
        raise engine.ConfigError(
            "token name must be 1-48 characters: lowercase letters, digits, '.', '_' or '-'."
        )
    scopes = sorted(set(scopes))
    if not scopes or any(sc not in API_TOKEN_SCOPES for sc in scopes):
        raise engine.ConfigError(
            "scopes must be one or more of: " + ", ".join(API_TOKEN_SCOPES)
            + " (see `api-token-scopes`)."
        )
    for group_name in groups or []:
        if get_schedule_group(group_name) is None:
            raise GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    if expires_days is not None and not (0 < expires_days <= 3650):
        raise engine.ConfigError("expiry must be between 0 and 3650 days.")
    token = _API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires_at = (now + timedelta(days=expires_days)).isoformat() if expires_days else None

    def _do():
        conn = _connect()
        try:
            try:
                conn.execute(
                    "INSERT INTO api_tokens (name, token_hash, token_prefix, scopes, instances,"
                    " groups, created_by, created_at, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (name, _hash_api_token(token), token[:10], json.dumps(scopes),
                     json.dumps(sorted(set(instances))) if instances else None,
                     json.dumps(sorted(set(groups))) if groups else None,
                     created_by, now.isoformat(), expires_at),
                )
                conn.commit()
            except sqlite3.IntegrityError as e:
                raise engine.ConfigError(f"a token named '{name}' already exists.") from e
        finally:
            conn.close()

    _retry_db(_do)
    _sync_token_record(name, on_warning)
    meta = next(t for t in list_api_tokens() if t["name"] == name)
    return {**meta, "token": token}


def list_api_tokens() -> list[dict]:

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                f"SELECT {_TOKEN_COLUMNS} FROM api_tokens ORDER BY created_at DESC, name"
            ).fetchall()
        finally:
            conn.close()

    return [_token_row_to_dict(r) for r in _retry_db(_do)]


def revoke_api_token(name: str, *, on_warning: Callable[[str], None] | None = None) -> bool:

    def _do():
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                (datetime.now(UTC).isoformat(), name),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    revoked = bool(_retry_db(_do))
    _sync_token_record(name, on_warning)
    return revoked


def _earliest(a: str | None, b: str | None) -> str | None:
    values = [v for v in (a, b) if v]
    return min(values, key=datetime.fromisoformat) if values else None


def restore_api_tokens_from_object_storage(
    *, on_progress: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> int:

    names = osb.list_token_records()
    if names is None:
        if osb.is_configured() and on_warning is not None:
            on_warning("WARNING: couldn't list API token records in Object Storage -- tokens were "
                       "not reconciled; re-run to retry.")
        return 0
    changed = 0
    for token_name in names:
        record = osb.download_token_record(token_name)
        if (record is None or record.get("name") != token_name
                or not isinstance(record.get("token_hash"), str)
                or not _API_TOKEN_NAME_RE.fullmatch(token_name)):
            if on_warning is not None:
                on_warning(f"WARNING: API token record '{token_name}' in Object Storage is missing "
                           "or failed verification -- skipped.")
            continue
        local = _token_record(token_name)
        if local is None:
            def _insert(rec=record):
                conn = _connect()
                try:
                    conn.execute(
                        f"INSERT INTO api_tokens ({', '.join(_TOKEN_RECORD_FIELDS)})"
                        f" VALUES ({', '.join('?' * len(_TOKEN_RECORD_FIELDS))})",
                        tuple(rec.get(f) for f in _TOKEN_RECORD_FIELDS),
                    )
                    conn.commit()
                finally:
                    conn.close()

            try:
                _retry_db(_insert)
            except sqlite3.IntegrityError as e:
                if on_warning is not None:
                    on_warning(f"WARNING: couldn't restore API token '{token_name}' ({e}).")
                continue
            changed += 1
            if on_progress is not None:
                state = "revoked" if record.get("revoked_at") else "active"
                on_progress(f"  restored API token '{token_name}' ({state}) from Object Storage.")
            continue
        if local["token_hash"] != record["token_hash"]:
            if on_warning is not None:
                on_warning(f"WARNING: API token '{token_name}' differs between the database and "
                           "Object Storage -- kept the local one.")
            continue
        revoked_at = _earliest(local["revoked_at"], record.get("revoked_at"))
        expires_at = _earliest(local["expires_at"], record.get("expires_at"))
        if revoked_at != local["revoked_at"] or expires_at != local["expires_at"]:
            def _update(n=token_name, r=revoked_at, x=expires_at):
                conn = _connect()
                try:
                    conn.execute("UPDATE api_tokens SET revoked_at = ?, expires_at = ? WHERE name = ?",
                                 (r, x, n))
                    conn.commit()
                finally:
                    conn.close()

            _retry_db(_update)
            changed += 1
            if on_progress is not None:
                on_progress(f"  applied the revocation/expiry recorded for API token "
                            f"'{token_name}' in Object Storage.")
    return changed


def verify_api_token(token: str) -> dict | None:

    if not token.startswith(_API_TOKEN_PREFIX):
        return None
    token_hash = _hash_api_token(token)

    def _do():
        conn = _connect()
        try:
            return conn.execute(
                f"SELECT {_TOKEN_COLUMNS} FROM api_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        finally:
            conn.close()

    row = _retry_db(_do)
    if row is None:
        return None
    meta = _token_row_to_dict(row)
    if meta["revoked_at"] is not None:
        return None
    if meta["expires_at"] and datetime.fromisoformat(meta["expires_at"]) <= datetime.now(UTC):
        return None

    def _touch():
        conn = _connect()
        try:
            conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE name = ?",
                         (datetime.now(UTC).isoformat(), meta["name"]))
            conn.commit()
        finally:
            conn.close()

    with contextlib_suppress(Exception):
        _retry_db(_touch)
    return meta


def api_token_allows_instance(meta: dict, name: str) -> bool:

    if meta.get("instances") is None and meta.get("groups") is None:
        return True
    if name in (meta.get("instances") or []):
        return True
    record = load_registry().get(name)
    if record and record.get("group_id") is not None and meta.get("groups"):
        group = _group_row_by_id(record["group_id"])
        return bool(group and group["name"] in meta["groups"])
    return False


def api_token_allows_group(meta: dict, group_name: str) -> bool:
    if meta.get("instances") is None and meta.get("groups") is None:
        return True
    return group_name in (meta.get("groups") or [])


def build_arg_parser() -> argparse.ArgumentParser:


    load_dotenv(engine.BASE_DIR / ".env")
    default_ssh_key = os.environ.get(
        "LINODE_SSH_KEY_PATH", str(Path.home() / ".ssh" / "linode_spike_key")
    )

    parser = argparse.ArgumentParser(
        prog="instance_manager.py",
        description="Onboard existing Linode instances and start/stop them by name.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate_start_parser = subparsers.add_parser(
        "migrate-start",
        help="Path B: start migrating a local-disk instance onto Block Storage.",
    )
    migrate_start_parser.add_argument("--name", required=True, type=validate_instance_name, help="A short name to track this migration by.")
    migrate_start_parser.add_argument("--instance-id", type=int, required=True)
    migrate_start_parser.add_argument("--ssh-key", default=default_ssh_key)
    migrate_start_parser.add_argument("--force", action="store_true", help="Restart an in-progress migration.")
    migrate_start_parser.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompts (a --force restart at a DIFFERENT --instance-id; the "
        "cost of --backup).",
    )
    migrate_start_parser.add_argument(
        "--backup", action="store_true",
        help="Before migrating, power the instance off and keep a native clone of it (and of its "
        "attached volumes) as a powered-off rollback point. Billable until deleted "
        "(`backup-delete`); `rollback` puts the original back.",
    )

    for cmd, helptext in (("holiday-add", "Add a holiday: scheduled starts on that date (or "
                                             "range) are skipped; stops still happen."),
                          ("holiday-remove", "Remove a holiday (or a range of them).")):
        hp = subparsers.add_parser(cmd, help=helptext)
        hp.add_argument("--date", required=True, help="YYYY-MM-DD (in each schedule's timezone).")
        hp.add_argument("--to", help="Last date of a range, YYYY-MM-DD (inclusive).")
        scope = hp.add_mutually_exclusive_group()
        scope.add_argument("--group-name", type=validate_instance_name,
                           help="Only this group's members (default: every node).")
        scope.add_argument("--name", type=validate_instance_name,
                           help="Only this node (default: every node).")
        if cmd == "holiday-add":
            hp.add_argument("--note", help="e.g. 'Diwali'.")
    holiday_list_parser = subparsers.add_parser("holiday-list", help="List holidays.")
    holiday_list_parser.add_argument("--all", action="store_true",
                                     help="Include past dates (default: from yesterday on).")
    holiday_list_parser.add_argument("--json", action="store_true")
    holiday_settings_parser = subparsers.add_parser(
        "holiday-settings",
        help="Show or set whether account-wide holidays apply to a group or a node. A node's own "
        "setting overrides its group's; group and node holidays always apply.",
    )
    hs_target = holiday_settings_parser.add_mutually_exclusive_group(required=True)
    hs_target.add_argument("--group-name", type=validate_instance_name)
    hs_target.add_argument("--name", type=validate_instance_name)
    hs_mode = holiday_settings_parser.add_mutually_exclusive_group()
    hs_mode.add_argument("--follow-account-wide", action="store_true",
                         help="Account-wide holidays skip scheduled starts (the default).")
    hs_mode.add_argument("--ignore-account-wide", action="store_true",
                         help="Account-wide holidays don't skip scheduled starts.")
    hs_mode.add_argument("--inherit", action="store_true",
                         help="Node only: drop its own setting and use its group's.")

    backup_config_parser = subparsers.add_parser(
        "backup-config",
        help="Show or change where this tool backs itself up (Object Storage settings in .env), "
        "test the connection, and see the last backup's result.",
    )
    backup_config_parser.add_argument("--bucket")
    backup_config_parser.add_argument("--endpoint", help="e.g. https://in-maa-1.linodeobjects.com")
    backup_config_parser.add_argument("--access-key", help="Prompted for if omitted.")
    backup_config_parser.add_argument("--secret-key", help="Prompted for if omitted (recommended).")
    backup_config_parser.add_argument("--test", action="store_true",
                                      help="Test the saved settings (writes and deletes a test object).")
    backup_config_parser.add_argument("--disable", action="store_true",
                                      help="Remove the Object Storage settings.")
    backup_config_parser.add_argument("--json", action="store_true")

    backup_list_parser = subparsers.add_parser(
        "backup-list", help="List pre-migration backups (migrate-start --backup).")
    backup_list_parser.add_argument("--json", action="store_true")

    console_session_delete_parser = subparsers.add_parser(
        "console-session-delete",
        help="Delete a migration's Rescue Mode console session log (lish-<name>.log). Sessions "
        "not written to for ACTIVITY_LOG_RETENTION_DAYS (default 30) are removed automatically.")
    console_session_delete_parser.add_argument("--name", required=True,
                                               type=validate_instance_name)

    rollback_parser = subparsers.add_parser(
        "rollback",
        help="Put the original system back from its pre-migration backup: the backup instance "
        "takes the original's public/VPC/VLAN addresses and label and is booted; the scheduler "
        "stops managing the name. Nothing is deleted.",
    )
    rollback_parser.add_argument("--name", required=True, type=validate_instance_name)
    rollback_parser.add_argument("--ssh-key", default=default_ssh_key)
    rollback_parser.add_argument("--no-boot", action="store_true",
                                 help="Restore everything but leave the backup powered off.")
    rollback_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")

    backup_delete_parser = subparsers.add_parser(
        "backup-delete",
        help="Permanently delete a kept pre-migration backup (its clone instance and cloned "
        "volumes). For a restored backup only the record is removed.",
    )
    backup_delete_parser.add_argument("--name", required=True, type=validate_instance_name)
    backup_delete_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    backup_delete_parser.add_argument(
        "--now", action="store_true",
        help="Delete at once instead of holding it for the grace period (see pending-deletions).")

    drill_parser = subparsers.add_parser(
        "recovery-drill",
        help="Prove a new host could take over: a throwaway VM installs the release copy and the "
        "latest snapshot (read-only), checks everything works, and is deleted.",
    )
    drill_parser.add_argument("--region", help="Region for the VM (default: this host's).")
    drill_parser.add_argument("--keep-vm", action="store_true", help="Don't delete the VM (to inspect it).")
    drill_check_parser = subparsers.add_parser(
        "recovery-drill-check",
        help="On a host restored by `install.sh recover --drill`: check everything would work.")
    drill_check_parser.add_argument("--report", help="Also write the JSON report to this file.")

    rot_parser = subparsers.add_parser(
        "rotate-credentials",
        help="Replace a credential (e.g. after it may have leaked): the Linode API token, the "
        "Object Storage key, the dashboard OAuth secret, or the deployment SSH key on every node.",
    )
    rot_parser.add_argument("kind", choices=CREDENTIAL_KINDS)
    rot_parser.add_argument("--continue", dest="continue_rotation", action="store_true",
                            help="ssh-key: move the nodes not yet moved (the scheduler also does this).")

    ri_parser = subparsers.add_parser(
        "restore-item",
        help="Put back one item as it was in a database snapshot -- an instance's schedule or "
        "hooks, or a group -- without restoring the whole database.",
    )
    ri_parser.add_argument("kind", choices=RESTORE_ITEM_KINDS)
    ri_parser.add_argument("--name", required=True, help="The instance (schedule/hooks) or group.")
    ri_parser.add_argument("--snapshot", help="A snapshot key in Object Storage (default: newest).")
    ri_parser.add_argument("--from-file", help="A local snapshot file instead.")
    ri_parser.add_argument("--show", action="store_true", help="Only print what the snapshot holds.")
    ri_parser.add_argument("--with-members", action="store_true",
                           help="group: also add back members that exist and aren't in another group.")

    bp_parser = subparsers.add_parser(
        "bucket-protection",
        help="Show or turn on versioning for the backup bucket (an overwritten or deleted backup "
        "stays recoverable for 30 days), list an object's versions, or bring one back.",
    )
    bp_parser.add_argument("--enable", action="store_true")
    bp_parser.add_argument("--key", help="An object key, e.g. full-backups/instances-continuous.db")
    bp_parser.add_argument("--restore-version", metavar="VERSION_ID",
                           help="Make this version of --key the current one again.")

    release_parser = subparsers.add_parser(
        "release-backup",
        help="Save a copy of this installed release (code, dashboard, and every dependency as a "
        "wheel) to Object Storage, so a new host can be installed without GitHub or PyPI. Done "
        "automatically when the code changes.",
    )
    release_parser.add_argument("--force", action="store_true", help="Upload even if unchanged.")
    release_parser.add_argument("--output", help="Write the archive to this local file instead.")

    pending_parser = subparsers.add_parser(
        "pending-deletions",
        help="List offboarded nodes and deleted backups waiting out their grace period; "
        "recover one, or delete it now.",
    )
    pending_action = pending_parser.add_mutually_exclusive_group()
    pending_action.add_argument("--recover", type=int, metavar="ID",
                                help="Cancel this deletion and bring the node (or backup) back.")
    pending_action.add_argument("--delete-now", type=int, metavar="ID",
                                help="Carry out this deletion now instead of waiting.")
    pending_parser.add_argument("--all", action="store_true",
                                help="Also list deletions already carried out or recovered.")
    pending_parser.add_argument("--json", action="store_true")
    pending_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")

    migrate_resume_parser = subparsers.add_parser(
        "migrate-resume",
        help="Path B: finish a migration after the manual dd step is done.",
    )
    migrate_resume_parser.add_argument("--name", required=True, type=validate_instance_name)
    migrate_resume_parser.add_argument("--ssh-key", default=default_ssh_key)

    migrate_copy_parser = subparsers.add_parser(
        "migrate-copy",
        help="Path B: run the copy step for you over the Lish console, then migrate-resume. "
        "Needs a kept pre-migration backup and this deployment's key in the profile's Lish keys.",
    )
    migrate_copy_parser.add_argument("--name", required=True, type=validate_instance_name)
    migrate_copy_parser.add_argument("--ssh-key", default=default_ssh_key)
    migrate_copy_parser.add_argument("--no-resume", action="store_true",
                                     help="Stop after the copy instead of running migrate-resume.")
    migrate_copy_parser.add_argument("--check", action="store_true",
                                     help="Only report whether the assisted copy can run (JSON).")

    migrate_orphans_parser = subparsers.add_parser(
        "migrate-orphans",
        help="List or clean up destination volumes orphaned by --force-replaced migration "
        "attempts.",
    )
    migrate_orphans_parser.add_argument(
        "--name", type=validate_instance_name, help="Limit to one name. Required with --cleanup.",
    )
    migrate_orphans_parser.add_argument(
        "--volume-id", type=int, default=None,
        help="With --cleanup, delete only this one volume instead of every orphaned volume for "
        "--name. Required (identifies exactly one volume) with --mark-orphaned.",
    )


    cleanup_group = migrate_orphans_parser.add_mutually_exclusive_group()
    cleanup_group.add_argument(
        "--cleanup", action="store_true",
        help="Delete the matching orphaned volume(s) (requires --name) instead of just listing "
        "them. Idempotent -- a 404 counts as already resolved.",
    )
    cleanup_group.add_argument(
        "--mark-orphaned", action="store_true",
        help="Retag a remote-only ACTIVE migration volume (found via tag, no local "
        "record) as orphaned, for a volume the operator has confirmed by "
        "hand (Cloud Manager / direct knowledge) is dead and unresumable. Requires --name "
        "and --volume-id, and typing --name to confirm unless --yes. Does not delete "
        "anything -- once marked, the volume is eligible for the existing --cleanup path "
        "-- run this as a separate, later "
        "invocation, not combined with --cleanup in the same one.",
    )
    migrate_orphans_parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")

    onboard_parser = subparsers.add_parser(
        "onboard", help="Register an existing, already volume-based instance."
    )
    onboard_parser.add_argument("--name", required=True, type=validate_instance_name, help="A short name to refer to this instance by.")
    onboard_parser.add_argument("--instance-id", type=int, required=True, help="The live Linode instance ID.")
    onboard_parser.add_argument(
        "--vpc-id", type=int, default=None,
        help="Required only if the instance has a VPC interface under the legacy_config model "
        "(that model doesn't expose vpc_id directly) and no matching VPC can be found "
        "automatically. Also used as a fallback for the newer 'linode' interface model if "
        "its own captured vpc_id is ever missing (not expected in practice).",
    )
    onboard_parser.add_argument("--ssh-key", default=default_ssh_key)
    onboard_parser.add_argument("--force", action="store_true", help="Re-onboard even if already registered.")
    onboard_parser.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt shown when --force changes the underlying resource "
        "identity (a different instance/volume/IP than what's currently on record).",
    )

    start_parser = subparsers.add_parser("start", help="Start (create) an onboarded instance.")
    start_target = start_parser.add_mutually_exclusive_group(required=True)
    start_target.add_argument("--name", type=validate_instance_name)
    start_target.add_argument(
        "--group-name", help="Start every member of this group at once (in parallel).",
    )
    start_parser.add_argument(
        "--with-dependencies", action="store_true",
        help="With --group-name: also start the groups in its dependency chain, in order.",
    )
    start_parser.add_argument(
        "--max-parallel", type=int, default=DEFAULT_GROUP_ACTION_MAX_PARALLEL,
        help="With --group-name: members acted on at once (default "
        f"{DEFAULT_GROUP_ACTION_MAX_PARALLEL}, max {MAX_POLL_MAX_PARALLEL}).",
    )
    start_parser.add_argument(
        "--respect-dependencies", action="store_true",
        help="Refuse (exit code 5) instead of warning when the node's group dependency isn't "
        "satisfied.",
    )
    start_parser.add_argument(
        "--json", action="store_true",
        help="Print the result as one JSON object on stdout (progress goes to stderr).",
    )
    start_parser.add_argument("--ssh-key", default=default_ssh_key)
    start_parser.add_argument(
        "--override-window-hours", type=float, default=None,
        help="Only relevant for a manual start outside a configured schedule's own on-window: "
        f"how many hours before it auto-stops (default {DEFAULT_MANUAL_OVERRIDE_WINDOW_HOURS} "
        "-- see `extend` to push an already-running override further out).",
    )

    start_parser.add_argument(
        "--skip-hooks", action="store_true",
        help="Start without running the instance's post-start check (recorded in history).",
    )

    stop_parser = subparsers.add_parser("stop", help="Stop (delete) an onboarded instance.")
    stop_target = stop_parser.add_mutually_exclusive_group(required=True)
    stop_target.add_argument("--name", type=validate_instance_name)
    stop_target.add_argument(
        "--group-name", help="Stop every member of this group at once (in parallel).",
    )
    stop_parser.add_argument(
        "--with-dependencies", action="store_true",
        help="With --group-name: also stop the groups in its dependency chain, in order.",
    )
    stop_parser.add_argument(
        "--max-parallel", type=int, default=DEFAULT_GROUP_ACTION_MAX_PARALLEL,
        help="With --group-name: members acted on at once (default "
        f"{DEFAULT_GROUP_ACTION_MAX_PARALLEL}, max {MAX_POLL_MAX_PARALLEL}).",
    )
    stop_parser.add_argument(
        "--respect-dependencies", action="store_true",
        help="Refuse (exit code 5) instead of warning when the node's group dependency isn't "
        "satisfied.",
    )
    stop_parser.add_argument(
        "--json", action="store_true",
        help="Print the result as one JSON object on stdout (progress goes to stderr).",
    )
    stop_parser.add_argument("--ssh-key", default=default_ssh_key)
    stop_parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    stop_parser.add_argument(
        "--skip-hooks", action="store_true",
        help="Stop without running the instance's pre-stop hook (recorded in history).",
    )
    stop_parser.add_argument(
        "--force", action="store_true",
        help="Proceed with the delete even if the disaster-recovery tag refresh fails with a "
        "transient API error. Never bypasses a ResourceOwnershipConflict or "
        "TagVerificationError -- those mean the recovery mapping is provably wrong or "
        "contested, not just temporarily unreachable.",
    )
    stop_parser.add_argument(
        "--skip-precapture", action="store_true",


        help="Skip the SSH-based data-volume AND authorized_keys re-capture, using the "
        "last-known data_volumes/authorized_keys from the registry instead. Only use this "
        "when the node is confirmed SSH-unreachable (a guest firewall rule, crashed sshd, "
        "wedged network stack) -- without it, an unreachable node can never be stopped "
        "through this tool at all, even though it keeps billing. A data volume attached, or "
        "an SSH key added/revoked, out of band since the last start/stop cycle would be "
        "silently missed.",
    )

    offboard_parser = subparsers.add_parser(
        "offboard",
        help="Permanently decommission a stopped instance: release its reserved IP, "
        "remove it from the registry, and optionally delete its volumes.",
    )
    offboard_parser.add_argument("--name", required=True, type=validate_instance_name)
    offboard_parser.add_argument(
        "--delete-volumes", action="store_true",
        help="Also permanently delete the OS and data volume(s), not just release the IP. "
        "Destroys real data -- omit to keep the volumes (untagged, so `rebuild` won't find them).",
    )
    offboard_parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    offboard_parser.add_argument(
        "--now", action="store_true",
        help="Carry out the deletion at once instead of holding it for the grace period "
        f"(default {DELETION_GRACE_DAYS} days, LIS_DELETION_GRACE_DAYS) during which it can be "
        "recovered.")

    poll_parser = subparsers.add_parser(
        "poll",
        help="Run the scheduler: check every onboarded instance's individual schedule and "
        "start/stop it if due.",
    )
    poll_parser.add_argument(
        "--interval-seconds", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"How often to check what is due, in seconds (default {DEFAULT_POLL_INTERVAL_SECONDS}). "
        "A check only reads the local database; starts/stops run on separate workers, and a "
        "check also runs as soon as one finishes. Ignored with --once.",
    )
    poll_parser.add_argument(
        "--once", action="store_true",
        help="Run exactly one tick and exit, instead of looping forever. Useful for testing, or "
        "for running this from cron instead of a supervisor.",
    )
    poll_parser.add_argument(
        "--window-seconds", type=int, default=DEFAULT_POLL_CATCH_UP_SECONDS,
        help="How long after a rule's start_time/stop_time the poller keeps acting on it if "
        "nothing has happened to the instance since (default "
        f"{DEFAULT_POLL_CATCH_UP_SECONDS} -- catches up after a long tick or a short outage, and "
        "retries a failed start; never overrides a manual start/stop made after that time).",
    )
    poll_parser.add_argument(
        "--max-parallel", type=int, default=DEFAULT_POLL_MAX_PARALLEL,
        help=f"How many due starts/stops to run at once (default {DEFAULT_POLL_MAX_PARALLEL}, "
        f"max {MAX_POLL_MAX_PARALLEL}). 1 runs them one at a time.",
    )
    poll_parser.add_argument("--ssh-key", default=default_ssh_key)

    serve_api_parser = subparsers.add_parser(
        "serve-api",
        help="Run the customer-facing REST API server. Binds to localhost "
        "by default -- pass --host to expose it further, e.g. behind a reverse proxy.",
    )
    serve_api_parser.add_argument("--host", default="127.0.0.1")
    serve_api_parser.add_argument("--port", type=int, default=8000)

    list_parser = subparsers.add_parser(
        "list", help="List all onboarded instances and their current status.",
    )
    list_parser.add_argument("--json", action="store_true", help="Print as one JSON object.")

    status_parser = subparsers.add_parser("status", help="Show one onboarded instance's full record.")
    status_parser.add_argument("--name", required=True, type=validate_instance_name)
    status_parser.add_argument(
        "--check-network", action="store_true",
        help="Also list instances this tool doesn't manage in the node's VPC subnet(s) or VLAN(s).",
    )

    history_parser = subparsers.add_parser(
        "history",
        help="Show recent create/delete events for one instance (the audit trail).",
    )
    history_parser.add_argument("--name", required=True, type=validate_instance_name)
    history_parser.add_argument(
        "--limit", type=int, default=50,
        help="Maximum number of most-recent events to show (default 50).",
    )

    schedule_set_parser = subparsers.add_parser(
        "schedule-set",
        help="Create or replace one instance's individual start/stop schedule.",
    )
    schedule_set_parser.add_argument("--name", required=True, type=validate_instance_name)
    schedule_set_parser.add_argument(
        "--timezone", required=True,
        help="IANA timezone name (e.g. Asia/Kolkata, US/Pacific) -- this schedule's own "
        "day/time rules are resolved in this timezone, not UTC.",
    )
    schedule_set_parser.add_argument(
        "--days", help="Comma-separated days for a single rule, e.g. mon,tue,wed,thu,fri. "
        "Pass with --start-time/--stop-time; mutually exclusive with --rules-json.",
    )
    schedule_set_parser.add_argument(
        "--start-time", help="HH:MM 24-hour, e.g. 09:00. Pass with --days/--stop-time.",
    )
    schedule_set_parser.add_argument(
        "--stop-time", help="HH:MM 24-hour, e.g. 18:00. Pass with --days/--start-time.",
    )
    schedule_set_parser.add_argument(
        "--rules-json",
        help="A full JSON array of {days_of_week, start_time, stop_time} rules, for more than "
        "one rule in a single call. Mutually exclusive with --days/--start-time/--stop-time.",
    )
    schedule_set_parser.add_argument(
        "--disabled", action="store_true",
        help="Create the schedule disabled (the poller ignores it until re-enabled).",
    )

    schedule_show_parser = subparsers.add_parser(
        "schedule-show", help="Show one instance's individual schedule, if any.",
    )
    schedule_show_parser.add_argument("--name", required=True, type=validate_instance_name)

    schedule_clear_parser = subparsers.add_parser(
        "schedule-clear", help="Remove one instance's individual schedule.",
    )
    schedule_clear_parser.add_argument("--name", required=True, type=validate_instance_name)

    def _add_hook_target(p) -> None:
        target = p.add_mutually_exclusive_group(required=True)
        target.add_argument("--name", type=validate_instance_name, help="An onboarded instance.")
        target.add_argument(
            "--group-name", help="A schedule group (applies to members without their own).",
        )

    hooks_set_parser = subparsers.add_parser(
        "hooks-set",
        help="Set an instance's or group's pre-stop hook and/or post-start check. Only the "
        "options given change; others are kept. Hooks run as root on the instance over SSH. "
        "With Object Storage configured, every hook is also stored there and referenced from the "
        "instance's tags, so `rebuild` can restore it after losing the local database.",
    )
    _add_hook_target(hooks_set_parser)
    pre_stop_what = hooks_set_parser.add_mutually_exclusive_group()
    pre_stop_what.add_argument(
        "--pre-stop", metavar="COMMAND",
        help="Run right before every stop (e.g. stop PostgreSQL cleanly) -- an inline command, "
        "or the path of a script already installed on the instance.",
    )
    pre_stop_what.add_argument(
        "--pre-stop-script", metavar="FILE",
        help="Upload this local script file as the pre-stop hook (stored by the scheduler, "
        f"copied to the instance and run each time; up to {MAX_HOOK_SCRIPT_BYTES // 1024} KB).",
    )
    hooks_set_parser.add_argument(
        "--pre-stop-timeout", type=int, metavar="SECONDS",
        help=f"Default {DEFAULT_PRE_STOP_HOOK_TIMEOUT_S}, max {MAX_HOOK_TIMEOUT_S}.",
    )
    hooks_set_parser.add_argument(
        "--pre-stop-on-failure", choices=_HOOK_ON_FAILURE_VALUES,
        help="abort (default): keep the instance running and record the stop as failed. "
        "continue: stop anyway.",
    )
    hooks_set_parser.add_argument("--clear-pre-stop", action="store_true")
    post_start_what = hooks_set_parser.add_mutually_exclusive_group()
    post_start_what.add_argument(
        "--post-start", metavar="COMMAND",
        help="Readiness check run after every start, retried every "
        f"{POST_START_HOOK_RETRY_INTERVAL_S}s until it succeeds or times out -- an inline "
        "command, or the path of a script already installed on the instance.",
    )
    post_start_what.add_argument(
        "--post-start-script", metavar="FILE",
        help="Upload this local script file as the post-start check.",
    )
    hooks_set_parser.add_argument(
        "--post-start-timeout", type=int, metavar="SECONDS",
        help=f"Total time allowed. Default {DEFAULT_POST_START_HOOK_TIMEOUT_S}, max {MAX_HOOK_TIMEOUT_S}.",
    )
    hooks_set_parser.add_argument("--clear-post-start", action="store_true")

    hooks_show_parser = subparsers.add_parser(
        "hooks-show", help="Show an instance's own and effective hooks, or a group's hooks.",
    )
    _add_hook_target(hooks_show_parser)

    hooks_clear_parser = subparsers.add_parser(
        "hooks-clear",
        help="Remove all of an instance's own hooks (its group's then apply) or a group's hooks.",
    )
    _add_hook_target(hooks_clear_parser)

    hooks_run_parser = subparsers.add_parser(
        "hooks-run", help="Run an instance's hook now, without starting or stopping it.",
    )
    hooks_run_parser.add_argument("--name", required=True, type=validate_instance_name)
    hooks_run_parser.add_argument("--ssh-key", default=default_ssh_key)
    which_hook = hooks_run_parser.add_mutually_exclusive_group(required=True)
    which_hook.add_argument("--pre-stop", action="store_true")
    which_hook.add_argument("--post-start", action="store_true")
    hooks_run_parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt (pre-stop only).",
    )

    extend_parser = subparsers.add_parser(
        "extend",
        help="Keep a running node (--name) or every running member of a group (--group-name) "
        "running longer: pushes a manual start's auto-stop out, or skips today's scheduled stop. "
        "The groups it depends on are held to the same time.",
    )
    extend_target = extend_parser.add_mutually_exclusive_group(required=True)
    extend_target.add_argument("--name", type=validate_instance_name)
    extend_target.add_argument("--group-name", type=validate_instance_name)
    extend_parser.add_argument(
        "--hours", type=float, default=None,
        help=f"How many hours to extend by (default {DEFAULT_MANUAL_OVERRIDE_WINDOW_HOURS}): "
        "for a manually started instance, from now; for one running on its schedule, past "
        "today's scheduled stop (which is then skipped).",
    )

    group_create_parser = subparsers.add_parser(
        "group-create",
        help="Create a new, empty schedule group -- give it a schedule "
        "afterward with group-schedule-set.",
    )
    group_create_parser.add_argument("--name", required=True, help="The group's display name.")
    group_create_parser.add_argument(
        "--timezone", required=True,
        help="IANA timezone name (e.g. Asia/Kolkata, US/Pacific) -- the group's own day/time "
        "rules are resolved in this timezone, not UTC.",
    )

    group_schedule_set_parser = subparsers.add_parser(
        "group-schedule-set",
        help="Create or replace a schedule group's start/stop schedule.",
    )
    group_schedule_set_parser.add_argument("--group-name", required=True)
    group_schedule_set_parser.add_argument(
        "--timezone", required=True,
        help="IANA timezone name (e.g. Asia/Kolkata, US/Pacific) -- this group's own day/time "
        "rules are resolved in this timezone, not UTC. Required on every call, the same as "
        "group-create's own --timezone -- this is the only way to change a group's timezone "
        "after creation.",
    )
    group_schedule_set_parser.add_argument(
        "--days", help="Comma-separated days for a single rule, e.g. mon,tue,wed,thu,fri. "
        "Pass with --start-time/--stop-time; mutually exclusive with --rules-json.",
    )
    group_schedule_set_parser.add_argument(
        "--start-time", help="HH:MM 24-hour, e.g. 09:00. Pass with --days/--stop-time.",
    )
    group_schedule_set_parser.add_argument(
        "--stop-time", help="HH:MM 24-hour, e.g. 18:00. Pass with --days/--start-time.",
    )
    group_schedule_set_parser.add_argument(
        "--rules-json",
        help="A full JSON array of {days_of_week, start_time, stop_time} rules, for more than "
        "one rule in a single call. Mutually exclusive with --days/--start-time/--stop-time.",
    )
    group_schedule_set_parser.add_argument(
        "--disabled", action="store_true",
        help="Set the schedule disabled (the poller ignores it, for every member, until "
        "re-enabled).",
    )

    group_show_parser = subparsers.add_parser(
        "group-show", help="Show one schedule group's schedule and current members.",
    )
    group_show_parser.add_argument("--group-name", required=True)

    subparsers.add_parser("group-list", help="List every schedule group.")

    token_create_parser = subparsers.add_parser(
        "api-token-create", help="Create an API token for scripts (shown once).",
    )
    token_create_parser.add_argument("--name", required=True)
    token_create_parser.add_argument(
        "--scopes", required=True,
        help="Comma-separated scopes: per-operation ones (e.g. instances:start,savings:read) "
        "and/or the bundles read, operate, configure, admin. `api-token-scopes` lists them all.",
    )
    token_create_parser.add_argument("--instances", help="Comma-separated node names it may act on.")
    token_create_parser.add_argument("--groups", help="Comma-separated groups it may act on.")
    token_create_parser.add_argument("--expires-days", type=float, help="Default: no expiry.")
    subparsers.add_parser("api-token-list", help="List API tokens (never the tokens themselves).")
    subparsers.add_parser("api-token-scopes", help="List the scopes an API token can have.")
    token_revoke_parser = subparsers.add_parser("api-token-revoke", help="Revoke an API token.")
    token_revoke_parser.add_argument("--name", required=True)

    set_mode_parser = subparsers.add_parser(
        "set-mode",
        help="Make a node manual-only (never started or stopped by the scheduler) or schedulable "
        "again.",
    )
    set_mode_parser.add_argument("--name", required=True, type=validate_instance_name)
    which_mode = set_mode_parser.add_mutually_exclusive_group(required=True)
    which_mode.add_argument("--manual", action="store_true", help="Manual-only.")
    which_mode.add_argument("--auto", action="store_true", help="Schedulable (the default).")

    group_depends_parser = subparsers.add_parser(
        "group-depends",
        help="Make a group's members start only after another group's members are running and "
        "ready (post-start hooks included), and stop only after this group's are down.",
    )
    group_depends_parser.add_argument("--group-name", required=True)
    depends_target = group_depends_parser.add_mutually_exclusive_group(required=True)
    depends_target.add_argument(
        "--on", action="append", metavar="GROUP",
        help="Set the full list of groups this group depends on, replacing any existing ones. "
        "Repeat it, or give a comma-separated list (--on db,cache).",
    )
    depends_target.add_argument("--add", metavar="GROUP", help="Add one dependency.")
    depends_target.add_argument("--remove", metavar="GROUP", help="Remove one dependency.")
    depends_target.add_argument("--clear", action="store_true", help="Remove all dependencies.")

    group_delete_parser = subparsers.add_parser(
        "group-delete",
        help="Permanently delete a schedule group. Refuses if it still has members -- "
        "group-remove each one first.",
    )
    group_delete_parser.add_argument("--group-name", required=True)

    group_add_parser = subparsers.add_parser(
        "group-add", help="Add (or move) an onboarded instance into a schedule group.",
    )
    group_add_parser.add_argument("--name", required=True, type=validate_instance_name)
    group_add_parser.add_argument("--group-name", required=True)

    group_remove_parser = subparsers.add_parser(
        "group-remove",
        help="Remove an instance from its current schedule group. If it has no individual "
        "schedule of its own, prompts whether to copy the group's rules into a new one for it "
        "(or pass --keep-manual/--copy-schedule to decide non-interactively).",
    )
    group_remove_parser.add_argument("--name", required=True, type=validate_instance_name)
    group_remove_exclusive = group_remove_parser.add_mutually_exclusive_group()
    group_remove_exclusive.add_argument(
        "--keep-manual", action="store_true",
        help="If it has no individual schedule, leave it manual-only -- don't copy the group's "
        "rules. Skips the interactive prompt.",
    )
    group_remove_exclusive.add_argument(
        "--copy-schedule", action="store_true",
        help="If it has no individual schedule, copy the group's current rules into a new "
        "individual schedule for it. Skips the interactive prompt.",
    )
    group_remove_parser.add_argument(
        "--yes", action="store_true",
        help="Non-interactive: if neither --keep-manual nor --copy-schedule is given and a "
        "prompt would otherwise be shown, default to keeping it manual-only rather than "
        "prompting.",
    )

    clear_lock_parser = subparsers.add_parser(
        "clear-lock", help="Forcibly clear a stuck transitioning lock for one instance."
    )
    clear_lock_parser.add_argument("--name", required=True, type=validate_instance_name)
    clear_lock_parser.add_argument("--yes", action="store_true")

    heal_parser = subparsers.add_parser(
        "heal",
        help="Compare every node with the account and fix the database where it's wrong: adopt "
        "an instance holding a node's OS volume, mark a node whose instance is gone as stopped, "
        "delete a leftover blank instance from an unfinished start, clear a stale in-progress "
        "mark. The scheduler does this every few minutes.")
    heal_parser.add_argument("--name", action="append", type=validate_instance_name,
                             help="Only this node (repeatable). Default: every node.")
    heal_parser.add_argument("--leftover-min-age-minutes", type=float, default=15,
                             help="Delete a leftover instance only once it's this old (default 15).")

    alerts_check_parser = subparsers.add_parser(
        "alerts-check",
        help="Show anything that needs attention (scheduler not running, backups failing, the "
        "deployment key not fully protected, nodes off schedule); exit 1 if so. --send also "
        f"sends/clears alerts to {ALERT_WEBHOOK_ENV}.")
    alerts_check_parser.add_argument("--send", action="store_true")
    wd_parser = subparsers.add_parser(
        "host-watchdog",
        help="Linode's Shutdown Watchdog on this scheduler's own instance: if it crashes or powers "
        "off unexpectedly, Linode boots it again. Shows it; --enable/--disable change it.")
    wd_group = wd_parser.add_mutually_exclusive_group()
    wd_group.add_argument("--enable", action="store_true")
    wd_group.add_argument("--disable", action="store_true")
    lease_parser = subparsers.add_parser(
        "scheduler-lease",
        help="Which host is the active scheduler (the lease in Object Storage); --take-over makes "
        "this host the active one -- only when the other host is gone for good.")
    lease_parser.add_argument("--take-over", action="store_true")
    lease_parser.add_argument("--yes", action="store_true")
    subparsers.add_parser(
        "backup-verify",
        help="Restore-test the backups now: download the newest snapshot, check it, and check the "
        "stored copies of the SSH key decrypt (the scheduler does this daily).")
    ac = subparsers.add_parser(
        "alerts-config",
        help="Show or change where alerts go: a webhook and/or email over SMTP. Running services "
        "pick changes up without a restart.")
    ac.add_argument("--webhook-url", dest="webhook_url", help="Slack/Teams/any JSON-POST URL ('' clears).")
    ac.add_argument("--email-to", dest="email_to", help="Comma-separated recipients.")
    ac.add_argument("--smtp-host", dest="smtp_host")
    ac.add_argument("--smtp-port", dest="smtp_port", help="587 (starttls, default) or 465 (ssl).")
    ac.add_argument("--smtp-security", dest="smtp_security", choices=("starttls", "ssl", "none"))
    ac.add_argument("--smtp-user", dest="smtp_user")
    ac.add_argument("--smtp-password", action="store_true", help="Prompt for the SMTP password.")
    ac.add_argument("--email-from", dest="email_from")
    ac.add_argument("--clear-email", action="store_true", help="Turn email alerts off.")
    ac.add_argument("--clear-webhook", action="store_true", help="Turn webhook alerts off.")
    subparsers.add_parser("alerts-test", help="Send a test alert to every configured channel "
                          f"({ALERT_WEBHOOK_ENV}, and email via {ALERT_EMAIL_ENV} + SMTP settings).")

    abandon_parser = subparsers.add_parser(
        "migrate-abandon",
        help="Cancel an unfinished migration: take the original out of Rescue Mode and boot it "
        "from its original disk, and delete the destination volume.")
    abandon_parser.add_argument("--name", required=True, type=validate_instance_name)
    abandon_parser.add_argument("--no-boot", action="store_true",
                                help="Leave the original instance powered off.")
    abandon_parser.add_argument("--yes", action="store_true")

    catch_up_parser = subparsers.add_parser(
        "schedule-catch-up",
        help="List scheduled nodes that aren't in the state their schedule wants (e.g. after an "
        "outage longer than the catch-up window); with --apply, start/stop them after confirming.")
    catch_up_parser.add_argument("--name", action="append", type=validate_instance_name,
                                 help="Only this node (repeatable).")
    catch_up_parser.add_argument("--apply", action="store_true")
    catch_up_parser.add_argument("--include-changed-by-hand", action="store_true",
                                 help="Also act on nodes someone started/stopped by hand after the "
                                 "scheduled time.")
    catch_up_parser.add_argument("--max-parallel", type=int, default=5)
    catch_up_parser.add_argument("--ssh-key", default=default_ssh_key)
    catch_up_parser.add_argument("--yes", action="store_true")
    catch_up_parser.add_argument("--json", action="store_true")

    set_vpc_parser = subparsers.add_parser(
        "set-vpc-address",
        help="Give a stopped instance's VPC interface a different address, used from its next "
        "start (e.g. after a new instance took its address while it was stopped).",
    )
    set_vpc_parser.add_argument("--name", required=True, type=validate_instance_name)
    set_vpc_parser.add_argument("--address", required=True)
    set_vpc_parser.add_argument("--current", help="The VPC address to change, if it has several.")

    deregister_parser = subparsers.add_parser(
        "deregister",
        help="Remove an instance from this tool's own tracking only -- the Linode instance, "
        "its volumes, its reserved IP, and its tags are never touched. Use this when the local "
        "record itself is wrong or unsafe (not to decommission the instance -- use offboard "
        "for that).",
    )
    deregister_parser.add_argument("--name", required=True, type=validate_instance_name)
    deregister_parser.add_argument("--yes", action="store_true")

    reset_host_key_parser = subparsers.add_parser(
        "reset-host-key",
        help="Forget a node's trusted SSH host key and trust whatever it presents next. "
        "Only use after independently confirming a key change is legitimate.",
    )
    reset_host_key_parser.add_argument(
        "--name", type=validate_instance_name,
        help="An already-onboarded node's name. Mutually exclusive with --ip.",
    )
    reset_host_key_parser.add_argument(
        "--ip", type=engine.validate_ip_address,
        help="Reset trust by IP address directly, for a node that isn't onboarded yet -- e.g. "
        "a newly reused/reissued address with a stale entry from a previous node. Mutually "
        "exclusive with --name.",
    )
    reset_host_key_parser.add_argument("--ssh-key", default=default_ssh_key)
    reset_host_key_parser.add_argument("--yes", action="store_true")

    rebuild_parser = subparsers.add_parser(
        "rebuild",
        help="Disaster recovery: reconstruct the registry from tags on Linode's "
        "own account state, in case the local instances.json is lost.",
    )
    rebuild_parser.add_argument("--ssh-key", default=default_ssh_key)
    rebuild_parser.add_argument(
        "--vpc-id", type=int, default=None,
        help="Required only if a recovered instance has a VPC interface under the "
        "legacy_config model (that model doesn't expose vpc_id directly) and no VPC "
        "containing its subnet can be found automatically. Also used as a fallback for the "
        "newer 'linode' interface model if its own captured vpc_id is ever missing (not "
        "expected in practice). Applies to every recovered name this run.",
    )
    rebuild_parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing local entry with the rebuilt one, if one already exists.",
    )

    backup_parser = subparsers.add_parser(
        "backup",
        help="On-demand, whole-system backup -- re-syncs every instance's Object Storage "
        "record and takes a full database snapshot. Meant to be run on a schedule (cron/"
        "systemd timer), not just invoked manually.",
    )
    backup_parser.add_argument(
        "--backup-dir", default=None,
        help="Also write a full local database snapshot into this directory, independent of "
        "Object Storage. Give this, configure Object Storage (LINODE_OBJ_STORAGE_* in .env), "
        "or both -- refuses if neither is available, since there'd be nothing to actually do.",
    )

    restore_parser = subparsers.add_parser(
        "restore",
        help="Put a full database snapshot in place on a new host (from Object Storage or a "
        "local file), plus the trusted host keys. Run `rebuild` afterward. Stop the poller first.",
    )
    restore_source = restore_parser.add_mutually_exclusive_group()
    restore_source.add_argument("--list", action="store_true",
                                help="List the database snapshots in Object Storage and exit.")
    restore_source.add_argument("--snapshot", metavar="KEY",
                                help="A specific Object Storage snapshot (default: the latest).")
    restore_source.add_argument("--from-file", metavar="PATH",
                                help="A local snapshot file (e.g. from `backup --backup-dir`).")
    restore_source.add_argument("--known-hosts-only", action="store_true",
                                help="Only restore the trusted host keys (e.g. before a `rebuild` "
                                "with no snapshot).")
    restore_parser.add_argument("--known-hosts", metavar="PATH",
                                help="Trusted host keys file to restore (default: from Object "
                                "Storage, when configured).")
    restore_parser.add_argument("--force", action="store_true",
                                help="Replace a database that already holds instances or groups "
                                "(it's moved aside, not deleted).")
    restore_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")

    for cmd, helptext in (
        ("ssh-key-backup", "Store the deployment's SSH private key in Object Storage, encrypted "
         "with a passphrase you choose (and keep yourself), so a replacement host can get it back."),
        ("ssh-key-restore", "Fetch and decrypt the deployment SSH key from Object Storage onto "
         "this host."),
    ):
        p_ = subparsers.add_parser(cmd, help=helptext)
        p_.add_argument("--ssh-key", default=default_ssh_key,
                        help="Path of the private key (default: LINODE_SSH_KEY_PATH, or the "
                        "tool's default key path).")
        p_.add_argument("--passphrase-file", metavar="PATH",
                        help="Read the passphrase from this file instead of prompting.")
        if cmd == "ssh-key-restore":
            p_.add_argument("--force", action="store_true",
                            help="Overwrite a different key already at that path.")
            p_.add_argument("--from-db", action="store_true",
                            help="Use only the encrypted copy kept in the local database.")
            p_.add_argument("--save-passphrase", action="store_true",
                            help=f"If the passphrase came from the Linode account's recovery copy, "
                            f"write it to .env ({SSH_KEY_PASSPHRASE_ENV}).")
    p_ = subparsers.add_parser(
        "env-backup",
        help="Upload an age-encrypted copy of the settings (.env) and the deployment SSH key to "
        f"Object Storage, encrypted to your age public key(s) ({ENV_BACKUP_RECIPIENTS_ENV}); done "
        "automatically whenever they change. Only your age identity can open it.")
    p_.add_argument("--set-recipients", metavar="AGE_PUBLIC_KEYS",
                    help="Save your age public key(s) (age1..., comma-separated); empty turns it off.")
    p_ = subparsers.add_parser(
        "env-restore",
        help="On a replacement host: decrypt the settings backup with your age identity into .env "
        "(bucket settings and the key path already in .env are kept), plus the SSH key.")
    p_.add_argument("--identity-file", required=True, metavar="PATH",
                    help="File with your AGE-SECRET-KEY line ('-' reads it from stdin).")
    p_.add_argument("--ssh-key", metavar="PATH", help="Also restore the deployment SSH key here.")
    p_.add_argument("--force", action="store_true", help="Replace an existing .env / key.")
    p_.add_argument("--print", action="store_true", help="Print the settings instead of writing .env "
                    "(e.g. to recreate a Kubernetes Secret).")
    subparsers.add_parser(
        "env-backup-keygen",
        help="Print a new age keypair for the settings backup (better: run age-keygen on your own machine).")
    p_ = subparsers.add_parser(
        "ssh-key-protect",
        help="Keep the deployment SSH key recoverable: store it encrypted (passphrase from "
        f"{SSH_KEY_PASSPHRASE_ENV} in .env) in the database and Object Storage, and register the "
        "public key under the Linode profile's SSH keys. Done automatically; safe to re-run.")
    p_.add_argument("--ssh-key", default=default_ssh_key, help="Path of the private key.")

    return parser


SERVICE_LOGS = {"poll": "scheduler", "serve-api": "api", "backup": "backup"}
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 3


def service_log_dir() -> Path:
    return REGISTRY_PATH.parent / "logs"


CONSOLE_SESSION_PREFIX = "lish-"


def console_session_files(name: str) -> list[Path]:

    base = service_log_dir() / f"{CONSOLE_SESSION_PREFIX}{name}.log"
    return [p for p in (base, base.with_name(base.name + ".1")) if p.exists()]


def delete_console_session(name: str) -> list[str]:

    with _instance_lock(name):
        removed = []
        for path in console_session_files(name):
            path.unlink(missing_ok=True)
            removed.append(path.name)
        return removed


def prune_console_sessions(max_age_days: int | None = None) -> list[str]:

    days = max_age_days if max_age_days is not None else activity_retention_days()
    cutoff = time.time() - days * 86400
    removed: list[str] = []
    try:
        paths = list(service_log_dir().glob(f"{CONSOLE_SESSION_PREFIX}*.log"))
    except OSError:
        return removed
    for path in paths:
        name = path.name[len(CONSOLE_SESSION_PREFIX):-len(".log")]
        try:
            files = console_session_files(name)
            if not files or max(f.stat().st_mtime for f in files) >= cutoff:
                continue
            delete_console_session(name)
            removed.append(name)
        except Exception:
            continue
    return removed


class _RotatingLogFile:


    def __init__(self, name: str):
        self.path = service_log_dir() / f"{name}.log"
        self._lock = threading.Lock()

    def write_line(self, stream: str, line: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > LOG_FILE_MAX_BYTES:
                    for i in range(LOG_FILE_BACKUPS, 0, -1):
                        src = self.path.with_name(f"{self.path.name}.{i - 1}" if i > 1 else self.path.name)
                        if src.exists():
                            src.replace(self.path.with_name(f"{self.path.name}.{i}"))
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(f"{stamp} {stream} {line}\n")
        except OSError:
            pass


class _TeeStream:


    def __init__(self, original, log: _RotatingLogFile, stream: str):
        self._original = original
        self._log = log
        self._stream = stream
        self._buffer = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        written = self._original.write(text)
        with self._lock:
            self._buffer += text
            *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            self._log.write_line(self._stream, line)
        return written if isinstance(written, int) else len(text)

    def flush(self) -> None:
        self._original.flush()

    def __getattr__(self, attr):
        return getattr(self._original, attr)


def _tee_to_service_log(command: str | None) -> None:
    name = SERVICE_LOGS.get(command) if command else None
    if name is None or isinstance(sys.stdout, _TeeStream):
        return
    log = _RotatingLogFile(name)
    sys.stdout = _TeeStream(sys.stdout, log, "OUT")
    sys.stderr = _TeeStream(sys.stderr, log, "ERR")


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if argv is None:
        _tee_to_service_log(getattr(args, "command", None))
    if os.environ.get("LIS_NONINTERACTIVE") == "1":

        import builtins

        def _no_prompt(prompt: object = "") -> str:
            print(f"{str(prompt).strip()}\nThis command asks for confirmation, which the console "
                  "can't answer. Add --yes to run it.", file=sys.stderr)
            raise SystemExit(2)

        builtins.input = _no_prompt

    try:
        return _dispatch(args)
    except Exception as e:
        print(f"Configuration error: unexpected internal error: {e}", file=sys.stderr)
        return 1


def _dispatch(args) -> int:

    read_only = (args.command == "pending-deletions" and getattr(args, "recover", None) is None
                 and getattr(args, "delete_now", None) is None)
    refusal = standby_refusal(args.command, read_only=read_only)
    if refusal is not None:
        print(f"Refused: {refusal}", file=sys.stderr)
        return 1
    try:
        return _route(args)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1


def _route(args) -> int:
    if args.command == "list":
        return cmd_list(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "history":
        return cmd_history(args)
    if args.command == "schedule-show":
        return cmd_schedule_show(args)
    if args.command == "extend":
        return cmd_extend(args)
    if args.command == "serve-api":
        return cmd_serve_api(args)
    if args.command == "group-create":
        return cmd_group_create(args)
    if args.command == "group-show":
        return cmd_group_show(args)
    if args.command == "group-list":
        return cmd_group_list(args)
    if args.command == "group-delete":
        return cmd_group_delete(args)
    if args.command == "api-token-create":
        return cmd_api_token_create(args)
    if args.command == "api-token-list":
        return cmd_api_token_list(args)
    if args.command == "api-token-scopes":
        return cmd_api_token_scopes(args)
    if args.command == "api-token-revoke":
        return cmd_api_token_revoke(args)
    if args.command == "clear-lock":
        return cmd_clear_lock(args)
    if args.command == "alerts-check":
        return cmd_alerts_check(args)
    if args.command == "env-backup":
        return cmd_env_backup(args)
    if args.command == "env-restore":
        return cmd_env_restore(args)
    if args.command == "env-backup-keygen":
        return cmd_env_backup_keygen(args)
    if args.command == "alerts-test":
        return cmd_alerts_test(args)
    if args.command == "alerts-config":
        return cmd_alerts_config(args)
    if args.command == "backup-verify":
        return cmd_backup_verify(args)
    if args.command == "scheduler-lease":
        return cmd_scheduler_lease(args)
    if args.command == "deregister":
        return cmd_deregister(args)
    if args.command == "backup-list":
        return cmd_backup_list(args)
    if args.command == "release-backup":
        return cmd_release_backup(args)
    if args.command == "bucket-protection":
        return cmd_bucket_protection(args)
    if args.command == "pending-deletions" and args.recover is None and args.delete_now is None:
        return cmd_pending_deletions(None, args)
    if args.command == "console-session-delete":
        return cmd_console_session_delete(args)
    if args.command == "backup-config":
        return cmd_backup_config(args)
    if args.command == "holiday-add":
        return cmd_holiday_add(args)
    if args.command == "holiday-remove":
        return cmd_holiday_remove(args)
    if args.command == "holiday-list":
        return cmd_holiday_list(args)
    if args.command == "reset-host-key":
        return cmd_reset_host_key(args)
    if args.command == "backup":
        return cmd_backup(args)
    if args.command == "restore":
        return cmd_restore(args)
    if args.command == "ssh-key-backup":
        return cmd_ssh_key_backup(args)
    if args.command == "ssh-key-restore":
        return cmd_ssh_key_restore(args)
    if args.command == "hooks-show":
        return cmd_hooks_show(args)
    if args.command == "hooks-run":
        return cmd_hooks_run(args)

    try:
        token = engine.load_token()
        client = engine.build_client(token)
        engine.auth_check(client)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except (ApiError, requests.exceptions.RequestException) as e:


        print(f"Configuration error: could not verify API access: {e}", file=sys.stderr)
        return 1

    if args.command == "migrate-start":
        return cmd_migrate_start(client, args)
    if args.command == "migrate-resume":
        return cmd_migrate_resume(client, args)
    if args.command == "migrate-copy":
        return cmd_migrate_copy(client, args)
    if args.command == "migrate-orphans":
        return cmd_migrate_orphans(client, args)
    if args.command == "onboard":
        return cmd_onboard(client, args)
    if args.command == "start":
        return cmd_start(client, args)
    if args.command == "stop":
        return cmd_stop(client, args)
    if args.command == "offboard":
        return cmd_offboard(client, args)
    if args.command == "poll":
        return cmd_poll(client, args)
    if args.command == "rebuild":
        return cmd_rebuild(client, args)
    if args.command == "schedule-set":
        return cmd_schedule_set(client, args)
    if args.command == "schedule-clear":
        return cmd_schedule_clear(client, args)
    if args.command == "group-schedule-set":
        return cmd_group_schedule_set(client, args)
    if args.command == "group-depends":
        return cmd_group_depends(client, args)
    if args.command == "holiday-settings":
        return cmd_holiday_settings(client, args)
    if args.command == "set-mode":
        return cmd_set_mode(client, args)
    if args.command == "group-add":
        return cmd_group_add(client, args)
    if args.command == "group-remove":
        return cmd_group_remove(client, args)
    if args.command == "hooks-set":
        return cmd_hooks_set(client, args)
    if args.command == "hooks-clear":
        return cmd_hooks_clear(client, args)
    if args.command == "set-vpc-address":
        return cmd_set_vpc_address(client, args)
    if args.command == "rollback":
        return cmd_rollback(client, args)
    if args.command == "backup-delete":
        return cmd_backup_delete(client, args)
    if args.command == "pending-deletions":
        return cmd_pending_deletions(client, args)
    if args.command == "recovery-drill":
        return cmd_recovery_drill(client, args)
    if args.command == "recovery-drill-check":
        return cmd_recovery_drill_check(client, args)
    if args.command == "rotate-credentials":
        return cmd_rotate_credentials(client, args)
    if args.command == "restore-item":
        if args.show:
            return cmd_restore_item(None, args)
        return cmd_restore_item(client, args)
    if args.command == "heal":
        return cmd_heal(client, args)
    if args.command == "host-watchdog":
        return cmd_host_watchdog(client, args)
    if args.command == "migrate-abandon":
        return cmd_migrate_abandon(client, args)
    if args.command == "schedule-catch-up":
        return cmd_schedule_catch_up(client, args)
    if args.command == "ssh-key-protect":
        return cmd_ssh_key_protect(client, args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
