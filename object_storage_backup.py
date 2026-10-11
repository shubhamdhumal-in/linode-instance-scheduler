
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from collections.abc import Callable

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    boto3 = None
    BotoConfig = None
    BotoCoreError = ClientError = Exception

DEFAULT_ATTEMPTS = 5
DEFAULT_DELAY_S = 3
_MD5_METADATA_KEY = "content-md5-hex"


class ObjectStorageError(Exception):
    pass


def is_configured() -> bool:

    return bool(
        os.environ.get("LINODE_OBJ_STORAGE_BUCKET")
        and os.environ.get("LINODE_OBJ_STORAGE_ENDPOINT")
        and os.environ.get("LINODE_OBJ_STORAGE_ACCESS_KEY")
        and os.environ.get("LINODE_OBJ_STORAGE_SECRET_KEY")
    )


SETTING_KEYS = ("LINODE_OBJ_STORAGE_BUCKET", "LINODE_OBJ_STORAGE_ENDPOINT",
                "LINODE_OBJ_STORAGE_ACCESS_KEY", "LINODE_OBJ_STORAGE_SECRET_KEY")


def settings_summary() -> dict:

    access = os.environ.get("LINODE_OBJ_STORAGE_ACCESS_KEY") or ""
    return {
        "configured": is_configured(),
        "bucket": os.environ.get("LINODE_OBJ_STORAGE_BUCKET") or None,
        "endpoint": os.environ.get("LINODE_OBJ_STORAGE_ENDPOINT") or None,
        "access_key_hint": (access[:4] + "..." + access[-2:]) if len(access) > 8 else (
            "set" if access else None),
        "secret_key_set": bool(os.environ.get("LINODE_OBJ_STORAGE_SECRET_KEY")),
    }


def reset_client() -> None:

    global _cached_client, _cached_bucket
    _cached_client = None
    _cached_bucket = None


def test_connection(bucket: str, endpoint: str, access_key: str, secret_key: str) -> None:

    if boto3 is None:
        raise ObjectStorageError("the boto3 package is not installed")
    try:
        client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key,
                              aws_secret_access_key=secret_key,
                              config=BotoConfig(signature_version="s3v4"))
        key = f"{_DEPLOYMENT_PREFIX_FOR_TEST}connection-test"
        client.put_object(Bucket=bucket, Key=key, Body=b"ok")
        got = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        client.delete_object(Bucket=bucket, Key=key)
    except Exception as e:
        raise ObjectStorageError(f"could not write to bucket {bucket!r} at {endpoint}: {e}") from e
    if got != b"ok":
        raise ObjectStorageError("the test object read back differently than written")


_DEPLOYMENT_PREFIX_FOR_TEST = "deployment/"


_cached_client = None
_cached_bucket: str | None = None


def build_object_storage_client():

    global _cached_client, _cached_bucket
    if not is_configured() or boto3 is None:
        return None
    if _cached_client is not None:
        return _cached_client
    _cached_bucket = os.environ["LINODE_OBJ_STORAGE_BUCKET"]
    _cached_client = boto3.client(
        "s3",
        endpoint_url=os.environ["LINODE_OBJ_STORAGE_ENDPOINT"],
        aws_access_key_id=os.environ["LINODE_OBJ_STORAGE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["LINODE_OBJ_STORAGE_SECRET_KEY"],
        config=BotoConfig(signature_version="s3v4"),
    )
    return _cached_client


def _object_key(name: str) -> str:
    return f"instances/{name}.json"


def _is_transient(e: Exception) -> bool:

    if isinstance(e, ClientError):
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return status >= 500
    return isinstance(e, BotoCoreError)


def _retry(fn, *, attempts: int = DEFAULT_ATTEMPTS, delay_s: float = DEFAULT_DELAY_S):

    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except (ClientError, BotoCoreError) as e:
            last_exc = e
            if not _is_transient(e) or attempt == attempts - 1:
                raise
            time.sleep(delay_s)
    assert last_exc is not None
    raise last_exc


def _serialize(record: dict) -> tuple[bytes, str]:

    body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    md5_hex = hashlib.md5(body).hexdigest()
    return body, md5_hex


def upload_instance_backup(
    name: str, record: dict, *, attempts: int = DEFAULT_ATTEMPTS, delay_s: float = DEFAULT_DELAY_S
) -> None:

    client = build_object_storage_client()
    if client is None:
        raise ObjectStorageError("Object Storage is not configured (LINODE_OBJ_STORAGE_* unset)")
    bucket = _cached_bucket
    body, md5_hex = _serialize(record)

    def _do():
        response = client.put_object(
            Bucket=bucket,
            Key=_object_key(name),
            Body=body,
            ContentType="application/json",
            Metadata={_MD5_METADATA_KEY: md5_hex},
        )
        etag = response.get("ETag", "").strip('"')
        if etag and etag != md5_hex:
            raise ObjectStorageError(
                f"Object Storage backup for '{name}' did not verify: expected ETag {md5_hex}, "
                f"got {etag} -- the write may not have persisted the exact bytes sent."
            )

    _retry(_do, attempts=attempts, delay_s=delay_s)


HOOK_DIGEST_LENGTH = 32


def hook_spec_bytes(entry: dict) -> bytes:

    return json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")


def hook_spec_digest(entry: dict) -> str:
    return hashlib.sha256(hook_spec_bytes(entry)).hexdigest()[:HOOK_DIGEST_LENGTH]


def _hook_object_key(digest: str) -> str:
    return f"hooks/{digest}.json"


def upload_hook_spec(
    entry: dict, *, attempts: int = DEFAULT_ATTEMPTS, delay_s: float = DEFAULT_DELAY_S
) -> str:

    client = build_object_storage_client()
    if client is None:
        raise ObjectStorageError("Object Storage is not configured (LINODE_OBJ_STORAGE_* unset)")
    bucket = _cached_bucket
    body = hook_spec_bytes(entry)
    digest = hashlib.sha256(body).hexdigest()[:HOOK_DIGEST_LENGTH]
    md5_hex = hashlib.md5(body).hexdigest()

    def _do():
        response = client.put_object(
            Bucket=bucket, Key=_hook_object_key(digest), Body=body,
            ContentType="application/json", Metadata={_MD5_METADATA_KEY: md5_hex},
        )
        etag = response.get("ETag", "").strip('"')
        if etag and etag != md5_hex:
            raise ObjectStorageError(
                f"hook spec {digest} did not verify: expected ETag {md5_hex}, got {etag}."
            )

    _retry(_do, attempts=attempts, delay_s=delay_s)
    return digest


def download_hook_spec(digest: str) -> dict | None:

    client = build_object_storage_client()
    if client is None:
        return None
    try:
        response = _retry(lambda: client.get_object(Bucket=_cached_bucket, Key=_hook_object_key(digest)))
    except (ClientError, BotoCoreError, ObjectStorageError):
        return None
    body = response["Body"].read()
    if hashlib.sha256(body).hexdigest()[:HOOK_DIGEST_LENGTH] != digest:
        return None
    try:
        entry = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return entry if isinstance(entry, dict) else None


def snapshot_database(db_path: Path, dest_path: Path) -> None:

    source = sqlite3.connect(db_path)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


CONTINUOUS_SNAPSHOT_KEY = "full-backups/instances-continuous.db"


def upload_database_snapshot(
    db_path: Path, *, attempts: int = DEFAULT_ATTEMPTS, delay_s: float = DEFAULT_DELAY_S,
    key: str | None = None,
) -> str:

    client = build_object_storage_client()
    if client is None:
        raise ObjectStorageError("Object Storage is not configured (LINODE_OBJ_STORAGE_* unset)")
    bucket = _cached_bucket

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "snapshot.db"
        snapshot_database(db_path, tmp_path)
        body = tmp_path.read_bytes()

    md5_hex = hashlib.md5(body).hexdigest()


    if key is None:
        key = f"full-backups/instances-{int(time.time())}.db"

    def _do():
        response = client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/x-sqlite3",
            Metadata={_MD5_METADATA_KEY: md5_hex},
        )
        etag = response.get("ETag", "").strip('"')
        if etag and etag != md5_hex:
            raise ObjectStorageError(
                f"Full database snapshot upload to '{key}' did not verify: expected ETag "
                f"{md5_hex}, got {etag} -- the write may not have persisted the exact bytes sent."
            )

    _retry(_do, attempts=attempts, delay_s=delay_s)
    return key


_SNAPSHOT_PREFIX = "full-backups/"
_DEPLOYMENT_PREFIX = "deployment/"


def list_database_snapshots() -> list[str] | None:

    client = build_object_storage_client()
    if client is None:
        return None
    found: list[tuple[str, float]] = []
    token = None
    try:
        while True:
            kwargs = {"Bucket": _cached_bucket, "Prefix": _SNAPSHOT_PREFIX}
            if token:
                kwargs["ContinuationToken"] = token
            page = _retry(lambda kwargs=kwargs: client.list_objects_v2(**kwargs))
            for o in page.get("Contents", []) or []:
                key = o.get("Key", "")
                if not key.endswith(".db"):
                    continue
                modified = o.get("LastModified")
                found.append((key, modified.timestamp() if hasattr(modified, "timestamp") else 0.0))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
    except (ClientError, BotoCoreError, ObjectStorageError):
        return None

    def _ts(item: tuple[str, float]) -> float:
        key, modified = item
        stem = key[len(_SNAPSHOT_PREFIX):].removeprefix("instances-").removesuffix(".db")
        return float(stem) if stem.isdigit() else modified

    return [key for key, _ in sorted(found, key=lambda item: (_ts(item), item[0]))]


def prune_database_snapshots(*, keep_all_hours: int = 48, keep_daily_days: int = 30,
                             now: float | None = None) -> list[str]:

    keys = list_database_snapshots()
    if not keys:
        return []
    client = build_object_storage_client()
    now = time.time() if now is None else now
    stamped = []
    for key in keys:
        stem = key[len(_SNAPSHOT_PREFIX):].removeprefix("instances-").removesuffix(".db")
        if stem.isdigit():
            stamped.append((int(stem), key))
    if not stamped:
        return []
    stamped.sort()
    newest = stamped[-1][1]
    keep: set[str] = {newest}
    newest_of_day: dict[str, str] = {}
    for ts, key in stamped:
        age = now - ts
        if age <= keep_all_hours * 3600:
            keep.add(key)
        elif age <= keep_daily_days * 86400:
            newest_of_day[time.strftime("%Y-%m-%d", time.gmtime(ts))] = key
    keep.update(newest_of_day.values())
    deleted = []
    for _ts, key in stamped:
        if key in keep:
            continue
        try:
            _retry(lambda key=key: client.delete_object(Bucket=_cached_bucket, Key=key))
            deleted.append(key)
        except (ClientError, BotoCoreError, ObjectStorageError):
            continue
    return deleted


def _get_verified(key: str) -> bytes:

    client = build_object_storage_client()
    if client is None:
        raise ObjectStorageError("Object Storage is not configured (LINODE_OBJ_STORAGE_* unset)")
    try:
        response = _retry(lambda: client.get_object(Bucket=_cached_bucket, Key=key))
    except (ClientError, BotoCoreError) as e:
        raise ObjectStorageError(f"could not download '{key}': {e}") from e
    body = response["Body"].read()
    stored_md5 = response.get("Metadata", {}).get(_MD5_METADATA_KEY)
    if stored_md5 and hashlib.md5(body).hexdigest() != stored_md5:
        raise ObjectStorageError(f"'{key}' failed its checksum -- not using it.")
    return body


def download_database_snapshot(key: str, dest_path: Path) -> None:

    body = _get_verified(key)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(body)


def upload_deployment_file(
    name: str, data: bytes, *, attempts: int = DEFAULT_ATTEMPTS, delay_s: float = DEFAULT_DELAY_S
) -> str:

    client = build_object_storage_client()
    if client is None:
        raise ObjectStorageError("Object Storage is not configured (LINODE_OBJ_STORAGE_* unset)")
    bucket = _cached_bucket
    key = f"{_DEPLOYMENT_PREFIX}{name}"
    md5_hex = hashlib.md5(data).hexdigest()

    def _do():
        response = client.put_object(
            Bucket=bucket, Key=key, Body=data, ContentType="application/octet-stream",
            Metadata={_MD5_METADATA_KEY: md5_hex},
        )
        etag = response.get("ETag", "").strip('"')
        if etag and etag != md5_hex:
            raise ObjectStorageError(f"'{key}' did not verify: expected ETag {md5_hex}, got {etag}.")

    _retry(_do, attempts=attempts, delay_s=delay_s)
    return key


def download_deployment_file(name: str) -> bytes | None:

    client = build_object_storage_client()
    if client is None:
        raise ObjectStorageError("Object Storage is not configured (LINODE_OBJ_STORAGE_* unset)")
    key = f"{_DEPLOYMENT_PREFIX}{name}"
    try:
        response = _retry(lambda: client.get_object(Bucket=_cached_bucket, Key=key))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise ObjectStorageError(f"could not download '{key}': {e}") from e
    except BotoCoreError as e:
        raise ObjectStorageError(f"could not download '{key}': {e}") from e
    body = response["Body"].read()
    stored_md5 = response.get("Metadata", {}).get(_MD5_METADATA_KEY)
    if stored_md5 and hashlib.md5(body).hexdigest() != stored_md5:
        raise ObjectStorageError(f"'{key}' failed its checksum -- not using it.")
    return body


def list_instance_backups() -> list[str] | None:

    return _list_record_names("instances/")


def download_instance_backup(name: str) -> dict | None:

    client = build_object_storage_client()
    if client is None:
        return None

    def _do():
        return client.get_object(Bucket=_cached_bucket, Key=_object_key(name))

    try:
        response = _retry(_do)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        return None
    except (BotoCoreError, ObjectStorageError):
        return None

    body = response["Body"].read()
    stored_md5 = response.get("Metadata", {}).get(_MD5_METADATA_KEY)
    if stored_md5 and hashlib.md5(body).hexdigest() != stored_md5:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


_GROUP_PREFIX = "groups/"


def _group_object_key(group_name: str) -> str:
    return f"{_GROUP_PREFIX}{group_name}.json"


def _upload_record(key: str, label: str, record: dict, *, attempts: int, delay_s: float) -> None:

    client = build_object_storage_client()
    if client is None:
        raise ObjectStorageError("Object Storage is not configured (LINODE_OBJ_STORAGE_* unset)")
    bucket = _cached_bucket
    body, md5_hex = _serialize(record)

    def _do():
        response = client.put_object(
            Bucket=bucket, Key=key, Body=body,
            ContentType="application/json", Metadata={_MD5_METADATA_KEY: md5_hex},
        )
        etag = response.get("ETag", "").strip('"')
        if etag and etag != md5_hex:
            raise ObjectStorageError(
                f"Object Storage record for {label} did not verify: expected ETag "
                f"{md5_hex}, got {etag}."
            )

    _retry(_do, attempts=attempts, delay_s=delay_s)


def _delete_record(key: str) -> None:
    client = build_object_storage_client()
    if client is None:
        raise ObjectStorageError("Object Storage is not configured (LINODE_OBJ_STORAGE_* unset)")
    bucket = _cached_bucket
    _retry(lambda: client.delete_object(Bucket=bucket, Key=key))


def _download_record(key: str) -> dict | None:

    client = build_object_storage_client()
    if client is None:
        return None
    try:
        response = _retry(lambda: client.get_object(Bucket=_cached_bucket, Key=key))
    except (ClientError, BotoCoreError, ObjectStorageError):
        return None
    body = response["Body"].read()
    stored_md5 = response.get("Metadata", {}).get(_MD5_METADATA_KEY)
    if stored_md5 and hashlib.md5(body).hexdigest() != stored_md5:
        return None
    try:
        record = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _list_record_names(prefix: str) -> list[str] | None:

    client = build_object_storage_client()
    if client is None:
        return None
    names: list[str] = []
    token = None
    try:
        while True:
            kwargs = {"Bucket": _cached_bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = _retry(lambda kwargs=kwargs: client.list_objects_v2(**kwargs))
            for obj in page.get("Contents", []) or []:
                key = obj.get("Key", "")
                if key.startswith(prefix) and key.endswith(".json"):
                    names.append(key[len(prefix):-len(".json")])
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
    except (ClientError, BotoCoreError, ObjectStorageError):
        return None
    return sorted(names)


def upload_group_backup(
    group_name: str, record: dict, *, attempts: int = DEFAULT_ATTEMPTS,
    delay_s: float = DEFAULT_DELAY_S,
) -> None:

    _upload_record(_group_object_key(group_name), f"group '{group_name}'", record,
                   attempts=attempts, delay_s=delay_s)


def delete_group_backup(group_name: str) -> None:

    _delete_record(_group_object_key(group_name))


def download_group_backup(group_name: str) -> dict | None:

    return _download_record(_group_object_key(group_name))


def list_group_backups() -> list[str] | None:

    return _list_record_names(_GROUP_PREFIX)


_TOKEN_PREFIX = "tokens/"


def _token_object_key(token_name: str) -> str:
    return f"{_TOKEN_PREFIX}{token_name}.json"


def upload_token_record(
    token_name: str, record: dict, *, attempts: int = DEFAULT_ATTEMPTS,
    delay_s: float = DEFAULT_DELAY_S,
) -> None:
    _upload_record(_token_object_key(token_name), f"API token '{token_name}'", record,
                   attempts=attempts, delay_s=delay_s)


def download_token_record(token_name: str) -> dict | None:
    return _download_record(_token_object_key(token_name))


def list_token_records() -> list[str] | None:
    return _list_record_names(_TOKEN_PREFIX)


_MIGRATION_BACKUP_PREFIX = "migration-backups/"


def _migration_backup_key(name: str) -> str:
    return f"{_MIGRATION_BACKUP_PREFIX}{name}.json"


def upload_migration_backup(
    name: str, record: dict, *, attempts: int = DEFAULT_ATTEMPTS,
    delay_s: float = DEFAULT_DELAY_S,
) -> None:
    _upload_record(_migration_backup_key(name), f"pre-migration backup of '{name}'", record,
                   attempts=attempts, delay_s=delay_s)


def download_migration_backup(name: str) -> dict | None:
    return _download_record(_migration_backup_key(name))


def delete_migration_backup(name: str) -> None:
    _delete_record(_migration_backup_key(name))


def list_migration_backups() -> list[str] | None:
    return _list_record_names(_MIGRATION_BACKUP_PREFIX)


_HOLIDAYS_KEY = "holidays/all.json"


def upload_holidays(record: dict, *, attempts: int = DEFAULT_ATTEMPTS,
                    delay_s: float = DEFAULT_DELAY_S) -> None:
    _upload_record(_HOLIDAYS_KEY, "holidays", record, attempts=attempts, delay_s=delay_s)


def download_holidays() -> dict | None:
    return _download_record(_HOLIDAYS_KEY)

def sync_object_storage_backup(
    name: str, record: dict, *, on_warning: Callable[[str], None] | None = None
) -> None:

    if not is_configured():
        return
    try:
        upload_instance_backup(name, record)
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"  WARNING: could not back up '{name}' to Object Storage ({e}) -- the local "
                "database write already succeeded and is fully in effect; this backup will "
                "self-heal on the next successful write for this instance (safe to retry: "
                "just run any command that touches it again)."
            )
