

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import ipaddress
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import urllib.parse
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, NoReturn, TypeVar

import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from linode_api4 import Instance, Volume
from linode_api4.errors import ApiError
from pydantic import BaseModel

import linode_engine as engine
import instance_manager as im


UTC = timezone.utc

LINODE_OAUTH_AUTHORIZE_URL = "https://login.linode.com/oauth/authorize"
LINODE_OAUTH_TOKEN_URL = "https://login.linode.com/oauth/token"
LINODE_PROFILE_URL = "https://api.linode.com/v4/profile"


OAUTH_SCOPES = "account:read_only"

DEFAULT_SAVINGS_WINDOW_DAYS = 7


def _oauth_config() -> tuple[str, str, str]:

    load_dotenv(engine.BASE_DIR / ".env")
    client_id = os.environ.get("LINODE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("LINODE_OAUTH_CLIENT_SECRET")
    redirect_uri = os.environ.get("LINODE_OAUTH_REDIRECT_URI")
    missing = [
        name for name, value in (
            ("LINODE_OAUTH_CLIENT_ID", client_id),
            ("LINODE_OAUTH_CLIENT_SECRET", client_secret),
            ("LINODE_OAUTH_REDIRECT_URI", redirect_uri),
        ) if not value
    ]
    if missing:
        raise engine.ConfigError(
            f"{', '.join(missing)} not set. Register an OAuth Client in your own Linode account "
            "(Cloud Manager -> Profile -> OAuth Apps), then copy .env.example to .env and fill "
            "these in alongside LINODE_API_TOKEN."
        )
    assert client_id and client_secret and redirect_uri
    return client_id, client_secret, redirect_uri


_pending_oauth_states: dict[str, datetime] = {}
_pending_oauth_states_lock = threading.Lock()
_OAUTH_STATE_LIFETIME = timedelta(minutes=10)


def _sweep_old_oauth_states_locked() -> None:

    now = datetime.now(UTC)
    stale = [s for s, issued_at in _pending_oauth_states.items() if now - issued_at > _OAUTH_STATE_LIFETIME]
    for s in stale:
        del _pending_oauth_states[s]


ALERT_CHECK_INTERVAL_S = 300


@asynccontextmanager
async def lifespan(app: FastAPI):

    token = engine.load_token()
    app.state.linode_client = engine.build_client(token)
    app.state.ssh_key = os.environ.get(
        "LINODE_SSH_KEY_PATH", str(Path.home() / ".ssh" / "linode_spike_key"),
    )


    stop_alerts = threading.Event()

    def _alert_loop() -> None:
        if stop_alerts.wait(60):
            return
        while True:
            with contextlib.suppress(Exception):
                im.reload_object_storage_settings_if_changed()
                im.evaluate_alerts(include_scheduler=True, client=app.state.linode_client)
            if stop_alerts.wait(ALERT_CHECK_INTERVAL_S):
                return

    threading.Thread(target=_alert_loop, daemon=True, name="alert-check").start()
    yield
    stop_alerts.set()


app = FastAPI(
    title="Linode Instance Scheduler API",
    description="Customer-facing REST API for the Linode Instance Scheduler.",
    lifespan=lifespan,
)


_allowed_origins = [o.strip() for o in os.environ.get("API_ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=_allowed_origins, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )


def _client(request: Request):
    return request.app.state.linode_client


def _ssh_key(request: Request) -> str:
    return request.app.state.ssh_key


@app.get("/login")
def login() -> RedirectResponse:

    client_id, _secret, redirect_uri = _oauth_config()
    state = secrets.token_urlsafe(24)
    with _pending_oauth_states_lock:
        _sweep_old_oauth_states_locked()
        _pending_oauth_states[state] = datetime.now(UTC)


    query = urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "scope": OAUTH_SCOPES,
        "redirect_uri": redirect_uri, "state": state,
    })
    return RedirectResponse(f"{LINODE_OAUTH_AUTHORIZE_URL}?{query}")


@app.get("/oauth/callback")
def oauth_callback(code: str, state: str) -> RedirectResponse:

    with _pending_oauth_states_lock:
        issued_at = _pending_oauth_states.pop(state, None)
    if issued_at is None:
        raise HTTPException(400, "invalid or already-used OAuth state -- start over at /login.")
    if datetime.now(UTC) - issued_at > _OAUTH_STATE_LIFETIME:
        raise HTTPException(400, "OAuth login took too long -- start over at /login.")

    client_id, client_secret, redirect_uri = _oauth_config()
    try:
        token_response = requests.post(
            LINODE_OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code", "code": code,
                "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]

        profile_response = requests.get(
            LINODE_PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15,
        )
        profile_response.raise_for_status()
        linode_username = profile_response.json()["username"]
    except requests.exceptions.RequestException as e:
        raise HTTPException(502, f"could not complete login with Linode: {e}") from e
    except (KeyError, ValueError) as e:
        raise HTTPException(502, f"unexpected response from Linode during login: {e}") from e

    session = im.create_api_session(linode_username)
    query = urllib.parse.urlencode({
        "session_token": session["token"], "expires_at": session["expires_at"],
    })
    return RedirectResponse(f"/ui/?{query}")


@app.post("/logout")
def logout(authorization: str | None = Header(default=None)) -> dict:
    token = _bearer_token(authorization)
    if token is not None:
        im.delete_api_session(token)
    return {"ok": True}


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None or not authorization.startswith("Bearer "):
        return None
    return authorization[len("Bearer "):]


_BODY_CHECKED = "<checked in handler>"
_ROUTE_SCOPES: dict[tuple[str, str], str | None] = {
    ("GET", "/operations/{op_id}"): None,
    ("GET", "/instances"): "instances:list",
    ("GET", "/instances/{name}/status"): "instances:status",
    ("GET", "/instances/{name}/history"): "instances:history",
    ("GET", "/instances/{name}/savings"): "savings:read",
    ("GET", "/groups/{group_name}/savings"): "savings:read",
    ("GET", "/activity"): "activity:read",
    ("POST", "/instances/{name}/start"): "instances:start",
    ("POST", "/instances/{name}/stop"): "instances:stop",
    ("POST", "/instances/{name}/extend"): "instances:extend",
    ("POST", "/groups/{group_name}/start"): "groups:start",
    ("POST", "/groups/{group_name}/stop"): "groups:stop",
    ("GET", "/instances/{name}/schedule"): "schedules:read",
    ("POST", "/instances/{name}/schedule"): "schedules:write",
    ("DELETE", "/instances/{name}/schedule"): "schedules:write",
    ("GET", "/groups"): "groups:read",
    ("GET", "/groups/{group_name}"): "groups:read",
    ("POST", "/groups"): "groups:write",
    ("DELETE", "/groups/{group_name}"): "groups:write",
    ("POST", "/groups/{group_name}/schedule"): "groups:write",
    ("PATCH", "/groups/{group_name}"): "dependencies:write",
    ("PATCH", "/instances/{name}"): _BODY_CHECKED,
    ("GET", "/instances/{name}/hooks"): "hooks:read",
    ("GET", "/instances/{name}/hook-events"): "hooks:read",
    ("GET", "/groups/{group_name}/hooks"): "hooks:read",
    ("PUT", "/instances/{name}/hooks"): "hooks:write",
    ("DELETE", "/instances/{name}/hooks"): "hooks:write",
    ("PUT", "/groups/{group_name}/hooks"): "hooks:write",
    ("DELETE", "/groups/{group_name}/hooks"): "hooks:write",
    ("POST", "/instances/{name}/hooks/run"): "hooks:run",
    ("GET", "/linode/instances"): "instances:onboard",
    ("GET", "/linode/ssh-check"): "instances:onboard",
    ("POST", "/linode/ips/{address}/reserve"): "instances:onboard",
    ("POST", "/instances"): "instances:onboard",
    ("POST", "/instances/{name}/migrate-start"): "instances:migrate",
    ("POST", "/instances/{name}/migrate-resume"): "instances:migrate",
    ("GET", "/instances/{name}/migrate-status"): "instances:migrate",
    ("GET", "/instances/{name}/migrate-copy"): "instances:migrate",
    ("PUT", "/groups/{group_name}/holiday-setting"): "schedules:write",
    ("PUT", "/instances/{name}/holiday-setting"): "schedules:write",
    ("POST", "/instances/{name}/migrate-copy"): "instances:migrate",
    ("GET", "/linode/instances/{instance_id}/backup-estimate"): "instances:migrate",
    ("GET", "/backups"): "instances:migrate",
    ("GET", "/backups/{name}"): "instances:migrate",
    ("POST", "/backups/{name}/rollback"): "instances:migrate",
    ("POST", "/groups/{group_name}/extend"): "instances:extend",
    ("GET", "/holidays"): "schedules:read",
    ("POST", "/holidays"): "schedules:write",
    ("DELETE", "/holidays"): "schedules:write",
    ("DELETE", "/backups/{name}"): "instances:migrate",
    ("POST", "/instances/{name}/offboard"): "instances:offboard",
    ("POST", "/instances/{name}/deregister"): "instances:offboard",
    ("POST", "/instances/{name}/clear-lock"): "instances:clear-lock",
    ("POST", "/instances/{name}/migrate-abandon"): "instances:migrate",
    ("GET", "/schedule/missed"): "schedules:read",
    ("POST", "/schedule/missed/apply"): "schedules:catch-up",
    ("GET", "/system/health"): "activity:read",
    ("GET", "/system/backup"): "system:read",
    ("GET", "/system/alerts"): "system:read",
    ("GET", "/system/host-watchdog"): "system:read",
    ("POST", "/system/backup/run"): "system:backup",
    ("POST", "/system/backup/verify"): "system:backup",
    ("POST", "/system/env-backup/run"): "system:backup",
    ("POST", "/system/ssh-key/protect"): "system:backup",
    ("POST", "/system/heal"): "system:configure",
    ("POST", "/system/lease/take-over"): "system:configure",
    ("PUT", "/system/host-watchdog"): "system:configure",
    ("PUT", "/system/env-backup/recipients"): "system:configure",
    ("POST", "/system/alerts/test"): "system:configure",
    ("POST", "/instances/{name}/vpc-address"): "instances:vpc-address",
    ("POST", "/tokens"): "tokens:manage",
    ("GET", "/tokens"): "tokens:manage",
    ("DELETE", "/tokens/{token_name}"): "tokens:manage",
    ("GET", "/logs"): "logs:read",
    ("GET", "/logs/{service}"): "logs:read",
    ("DELETE", "/logs/{service}"): "instances:migrate",
}


_LIMITED_TOKEN_OPEN_ROUTES = {
    ("GET", "/instances"), ("GET", "/groups"), ("GET", "/operations/{op_id}"),
    ("GET", "/activity"), ("GET", "/schedule/missed"), ("POST", "/schedule/missed/apply"),
}


def _required_scope(method: str, path: str) -> str | None:

    return _ROUTE_SCOPES.get((method, path), "admin")


def _token_grants(meta: dict, scope: str) -> bool:
    if "admin" in meta["scopes"]:
        return True
    return scope != "admin" and scope in im.expand_token_scopes(meta["scopes"])


def _require_token_scope(request: Request, scope: str) -> None:

    meta = getattr(request.state, "api_token", None)
    if meta is not None and not _token_grants(meta, scope):
        raise HTTPException(403, f"this token doesn't have the '{scope}' scope.")


def _token_is_limited(meta: dict) -> bool:
    return meta.get("instances") is not None or meta.get("groups") is not None


def require_session(
    request: Request, authorization: str | None = Header(default=None),
) -> str:

    token = _bearer_token(authorization)
    if token is None:
        raise HTTPException(401, "missing or malformed Authorization header -- log in at /login.")
    username = im.get_api_session(token)
    if username is not None:
        request.state.api_token = None
        return username
    meta = im.verify_api_token(token)
    if meta is None:
        raise HTTPException(401, "session token is invalid or has expired -- log in again at /login.")
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    method = request.method
    needed = _required_scope(method, path)
    if needed is not None and needed != _BODY_CHECKED and not _token_grants(meta, needed):
        raise HTTPException(403, f"this token doesn't have the '{needed}' scope.")
    if _token_is_limited(meta):
        params = request.path_params
        if "name" in params and not im.api_token_allows_instance(meta, params["name"]):
            raise HTTPException(403, f"this token isn't allowed to act on '{params['name']}'.")
        if "group_name" in params and not im.api_token_allows_group(meta, params["group_name"]):
            raise HTTPException(
                403, f"this token isn't allowed to act on group '{params['group_name']}'.")
        if (not {"name", "group_name"} & set(params)
                and (method, path) not in _LIMITED_TOKEN_OPEN_ROUTES):
            raise HTTPException(
                403, "this token is limited to particular nodes/groups and can't use this route.")
    request.state.api_token = meta
    return f"token:{meta['name']}"


def _limited_token(request: Request) -> dict | None:
    meta = getattr(request.state, "api_token", None)
    return meta if meta is not None and _token_is_limited(meta) else None


def _visible_instance_names(request: Request) -> list[str] | None:

    meta = _limited_token(request)
    if meta is None:
        return None
    return [n for n in im.load_registry() if im.api_token_allows_instance(meta, n)]


@app.get("/health")
def health() -> dict:

    return {"ok": True}


@app.exception_handler(im.NotOnboardedError)
def _handle_not_onboarded(request: Request, exc: im.NotOnboardedError):
    return _error_response(404, exc)


@app.exception_handler(im.GroupNotFoundError)
def _handle_group_not_found(request: Request, exc: im.GroupNotFoundError):
    return _error_response(404, exc)


@app.exception_handler(im.NeedsManualRecoveryError)
def _handle_needs_manual_recovery(request: Request, exc: im.NeedsManualRecoveryError):
    return _error_response(409, exc)


@app.exception_handler(im.DependencyNotSatisfiedError)
def _handle_dependency_not_satisfied(request: Request, exc: im.DependencyNotSatisfiedError):
    return _error_response(409, exc)


@app.exception_handler(im.InstanceLockedError)
def _handle_instance_locked(request: Request, exc: im.InstanceLockedError):
    return _error_response(409, exc)


@app.exception_handler(engine.ResourceOwnershipConflict)
def _handle_ownership_conflict(request: Request, exc: engine.ResourceOwnershipConflict):
    return _error_response(409, exc)


@app.exception_handler(engine.IPNotReservedError)
def _handle_ip_not_reserved(request: Request, exc: engine.IPNotReservedError):


    return JSONResponse(
        status_code=409,
        content=_content_with_any_warnings({"detail": str(exc), "unreserved_ip": exc.address}, exc),
    )


@app.exception_handler(engine.NotMigratedError)
def _handle_not_migrated(request: Request, exc: engine.NotMigratedError):


    return JSONResponse(
        status_code=409,
        content=_content_with_any_warnings(
            {"detail": str(exc), "not_migrated_instance_id": exc.instance_id}, exc,
        ),
    )


@app.exception_handler(engine.TagVerificationError)
def _handle_tag_verification(request: Request, exc: engine.TagVerificationError):
    return _error_response(409, exc)


@app.exception_handler(engine.AlreadyOnboardedError)
def _handle_already_onboarded(request: Request, exc: engine.AlreadyOnboardedError):


    return JSONResponse(
        status_code=409,
        content=_content_with_any_warnings(
            {"detail": str(exc), "already_onboarded_name": exc.name}, exc,
        ),
    )


@app.exception_handler(engine.ConfigError)
def _handle_config_error(request: Request, exc: engine.ConfigError):
    return _error_response(400, exc)


@app.exception_handler(ApiError)
def _handle_linode_api_error(request: Request, exc: ApiError):


    return _error_response(502, exc)


@app.exception_handler(requests.exceptions.RequestException)
def _handle_request_exception(request: Request, exc: requests.exceptions.RequestException):


    return _error_response(502, exc)


def _content_with_any_warnings(content: dict, exc: Exception) -> dict:

    warnings = getattr(exc, "warnings", None)
    if warnings:
        content["warnings"] = warnings
    return content


def _reraise_with_warnings(exc: Exception, warnings: list[str]) -> NoReturn:

    if warnings:
        exc.warnings = warnings
    raise exc


_T = TypeVar("_T")


def _call_collecting_warnings(fn: Callable[[Callable[[str], None]], _T]) -> tuple[_T, list[str]]:

    warnings: list[str] = []
    try:
        result = fn(warnings.append)
    except Exception as e:
        _reraise_with_warnings(e, warnings)
    return result, warnings


def _call_collecting_warnings_with_progress(
    fn: Callable[[Callable[[str], None], Callable[[str], None]], _T],
) -> tuple[_T, list[str]]:

    warnings: list[str] = []

    def _on_progress(message: str) -> None:
        if _progress_message_is_warning(message):
            warnings.append(message)

    try:
        result = fn(_on_progress, warnings.append)
    except Exception as e:
        _reraise_with_warnings(e, warnings)
    return result, warnings


def _error_response(status_code: int, exc: Exception) -> JSONResponse:


    return JSONResponse(
        status_code=status_code, content=_content_with_any_warnings({"detail": str(exc)}, exc),
    )


_TOTAL_STEPS: dict[str, int] = {


    "start": 3, "stop": 2,


    "migrate_start": 5, "migrate_resume": 8, "migrate_copy": 12,
}
_OPERATION_TTL = timedelta(minutes=10)


@dataclasses.dataclass
class _Operation:
    action: str
    total_steps: int
    status: Literal["running", "done", "error"] = "running"
    completed_steps: int = 0
    current_step: str | None = None


    warnings: list[str] = dataclasses.field(default_factory=list)
    result: dict | None = None
    error_status: int = 500
    error_body: dict | None = None


    created_at: datetime = dataclasses.field(default_factory=lambda: datetime.now(UTC))


    finished_at: datetime | None = None
    actor: str | None = None


_operations: OrderedDict[str, _Operation] = OrderedDict()
_operations_lock = threading.Lock()


def _classify_exception(e: Exception) -> tuple[int, dict]:

    if isinstance(e, engine.IPNotReservedError):
        return 409, {"detail": str(e), "unreserved_ip": e.address}


    if isinstance(e, engine.NotMigratedError):
        return 409, {"detail": str(e), "not_migrated_instance_id": e.instance_id}
    if isinstance(e, engine.AlreadyOnboardedError):
        return 409, {"detail": str(e), "already_onboarded_name": e.name}
    if isinstance(e, (im.NotOnboardedError, im.GroupNotFoundError)):
        return 404, {"detail": str(e)}
    if isinstance(
        e, (im.NeedsManualRecoveryError, im.InstanceLockedError, im.DependencyNotSatisfiedError,
            engine.ResourceOwnershipConflict, engine.TagVerificationError),
    ):
        return 409, {"detail": str(e)}
    if isinstance(e, engine.ConfigError):
        return 400, {"detail": str(e)}
    if isinstance(e, (ApiError, requests.exceptions.RequestException)):


        return 502, {"detail": str(e)}
    return 500, {"detail": str(e)}


def _sweep_old_operations_locked() -> None:

    now = datetime.now(UTC)
    stale = [
        op_id for op_id, op in _operations.items()
        if op.status != "running" and now - (op.finished_at or op.created_at) > _OPERATION_TTL
    ]
    for op_id in stale:
        del _operations[op_id]


def _start_operation(action: str, total_steps: int | None = None, actor: str | None = None) -> str:
    op_id = uuid.uuid4().hex
    with _operations_lock:
        _sweep_old_operations_locked()
        _operations[op_id] = _Operation(
            action=action, total_steps=total_steps if total_steps is not None else _TOTAL_STEPS[action],
            actor=actor,
        )
    return op_id


def _make_progress_reporter(op_id: str) -> Callable[[str], None]:
    def _on_progress(message: str) -> None:
        with _operations_lock:
            op = _operations.get(op_id)
            if op is None:
                return
            op.completed_steps += 1
            op.current_step = message
    return _on_progress


def _make_warning_reporter(op_id: str) -> Callable[[str], None]:
    def _on_warning(message: str) -> None:
        with _operations_lock:
            op = _operations.get(op_id)
            if op is None:
                return
            op.warnings.append(message)
    return _on_warning


def _progress_message_is_warning(message: str) -> bool:

    return message.strip().startswith("WARNING")


def _make_progress_and_warning_reporter(op_id: str) -> Callable[[str], None]:

    progress = _make_progress_reporter(op_id)
    warn = _make_warning_reporter(op_id)

    def _on_progress(message: str) -> None:
        progress(message)
        if _progress_message_is_warning(message):
            warn(message)
    return _on_progress


def _run_job(op_id: str, fn: Callable[[], object]) -> None:

    try:
        result = fn()
        with _operations_lock:
            op = _operations.get(op_id)
            if op is not None:
                op.status = "done"
                op.result = _asdict(result)
                op.finished_at = datetime.now(UTC)
    except Exception as e:


        status, body = _classify_exception(e)
        with _operations_lock:
            op = _operations.get(op_id)
            if op is not None:
                op.status = "error"
                op.error_status = status
                op.error_body = body
                op.finished_at = datetime.now(UTC)


WAIT_MAX_SECONDS = 3600


def _kickoff(
    action: str, total_steps: int, fn: Callable[[str], object], wait: bool,
    actor: str | None = None,
) -> JSONResponse:

    op_id = _start_operation(action, total_steps=total_steps, actor=actor)
    thread = threading.Thread(target=_run_job, args=(op_id, lambda: fn(op_id)), daemon=True)
    thread.start()
    if not wait:
        return JSONResponse({"operation_id": op_id, "total_steps": total_steps})
    thread.join(timeout=WAIT_MAX_SECONDS)
    response = _operation_response(op_id)
    if thread.is_alive():
        response.status_code = 202
    return response


def _operation_response(op_id: str) -> JSONResponse:
    with _operations_lock:
        op = _operations.get(op_id)
        if op is None:
            raise HTTPException(404, "unknown or expired operation id.")
        percent = (
            round(min(op.completed_steps, op.total_steps) / op.total_steps * 100)
            if op.total_steps else 100
        )
        body: dict = {
            "status": op.status, "action": op.action, "percent": percent,
            "current_step": op.current_step, "warnings": op.warnings, "operation_id": op_id,
        }
        if op.status == "done":
            body["result"] = op.result
            return JSONResponse(status_code=200, content=body)
        if op.status == "error":
            assert op.error_body is not None
            body.update(op.error_body)
            return JSONResponse(status_code=op.error_status, content=body)
        return JSONResponse(status_code=200, content=body)


@app.get("/operations/{op_id}")
def api_get_operation(
    op_id: str, request: Request, user: str = Depends(require_session),
) -> JSONResponse:
    if _limited_token(request) is not None:
        with _operations_lock:
            op = _operations.get(op_id)
            if op is not None and op.actor != user:
                raise HTTPException(404, "unknown or expired operation id.")
    return _operation_response(op_id)


class ScheduleRule(BaseModel):
    days_of_week: list[str]
    start_time: str
    stop_time: str


class ScheduleSetRequest(BaseModel):
    timezone: str
    rules: list[ScheduleRule]
    enabled: bool = True


class StartRequest(BaseModel):
    override_window_hours: float | None = None
    skip_hooks: bool = False

    respect_dependencies: bool = False


class StopRequest(BaseModel):
    skip_precapture: bool = False
    force: bool = False
    skip_hooks: bool = False
    respect_dependencies: bool = False


class HookConfigRequest(BaseModel):

    pre_stop: dict | None = None
    post_start: dict | None = None


class HookRunRequest(BaseModel):
    hook: Literal["pre_stop", "post_start"]


class ExtendRequest(BaseModel):
    hours: float | None = None


class OnboardRequest(BaseModel):

    name: str
    instance_id: int
    vpc_id: int | None = None
    force: bool = False


    ssh_private_key: str | None = None
    ssh_password: str | None = None


class OffboardRequest(BaseModel):
    delete_volumes: bool = False


class MigrateStartRequest(BaseModel):
    instance_id: int


    ssh_private_key: str | None = None
    ssh_password: str | None = None


    force: bool = False


    backup: bool = False


class GroupCreateRequest(BaseModel):
    name: str
    timezone: str


class GroupPatchInstanceRequest(BaseModel):

    group_name: str | None = None
    copy_group_rules_as_individual: bool | None = None


    schedule_mode: Literal["auto", "manual"] | None = None


def _rules_as_dicts(rules: list[ScheduleRule]) -> list[dict]:
    return [r.model_dump() for r in rules]


@app.get("/linode/instances")
def api_list_linode_instances(request: Request, user: str = Depends(require_session)) -> list[dict]:

    client = _client(request)
    registry = im.load_registry()
    onboarded_by_linode_id = {
        rec["current_linode_id"]: name
        for name, rec in registry.items() if rec.get("current_linode_id")
    }


    instances = engine.retry_transient(lambda: list(client.linode.instances()))
    return [
        {
            "id": inst.id,
            "label": inst.label,
            "region": inst.region.id,
            "status": inst.status,
            "tags": list(inst.tags) if inst.tags else [],
            "ipv4": [str(ip) for ip in inst.ipv4] if inst.ipv4 else [],
            "onboarded_as": onboarded_by_linode_id.get(inst.id),
        }
        for inst in instances
        if inst.lke_cluster_id is None
    ]


@app.post("/linode/ips/{address}/reserve")
def api_reserve_ip(address: str, request: Request, user: str = Depends(require_session)) -> dict:

    engine.reserve_existing_ip(_client(request), address)
    return {"reserved": True}


def _reject_internal_network_target(host: str) -> None:

    try:
        candidates = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError as e:
        raise HTTPException(422, f"could not resolve host {host!r}: {e}") from e
    for candidate in candidates:
        addr = ipaddress.ip_address(candidate)


        if (
            not addr.is_global or addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified
        ):
            raise HTTPException(
                422, f"host {host!r} resolves to a private/internal address ({candidate}) -- "
                "this check is only for reaching a real, public instance IP.",
            )


@app.get("/linode/ssh-check")
def api_ssh_check(
    request: Request, host: str | None = None, instance_id: int | None = None, port: int = 22,
    user: str = Depends(require_session),
) -> dict:

    if instance_id is not None:
        client = _client(request)
        try:
            instance = engine.retry_transient(lambda: client.load(engine.Instance, instance_id))
            configs = engine.retry_transient(lambda: list(instance.configs))
        except ApiError as e:
            if e.status == 404:
                raise HTTPException(404, f"no instance {instance_id} in this account.") from e
            raise
        if not configs:
            raise HTTPException(422, f"instance {instance_id} has no boot config.")
        address = engine.live_ssh_address(instance, configs)
        if not address:
            raise HTTPException(
                422, f"instance {instance_id} has no public interface and no static VPC/VLAN "
                "address -- nothing to reach it by.",
            )
        return {**engine.check_ssh_port_reachable(address, port), "host": address}
    if not host:
        raise HTTPException(422, "pass instance_id (or host).")
    _reject_internal_network_target(host)
    return {**engine.check_ssh_port_reachable(host, port), "host": host}


class TokenCreateRequest(BaseModel):
    name: str
    scopes: list[str]
    instances: list[str] | None = None
    groups: list[str] | None = None
    expires_days: float | None = None


def _check_token_not_wider(request: Request, scopes: list[str]) -> None:

    meta = getattr(request.state, "api_token", None)
    if meta is None or "admin" in meta["scopes"]:
        return
    if "admin" in scopes or not im.expand_token_scopes(scopes) <= im.expand_token_scopes(
            meta["scopes"]):
        raise HTTPException(403, "a token can't create or revoke a token wider than itself.")


@app.post("/tokens")
def api_create_token(
    body: TokenCreateRequest, request: Request, user: str = Depends(require_session),
) -> dict:

    _check_token_not_wider(request, body.scopes)
    warnings: list[str] = []
    created = im.create_api_token(
        body.name, body.scopes, instances=body.instances, groups=body.groups,
        expires_days=body.expires_days, created_by=user, on_warning=warnings.append,
    )
    return {**created, "warnings": warnings} if warnings else created


@app.get("/tokens")
def api_list_tokens(user: str = Depends(require_session)) -> list[dict]:
    return im.list_api_tokens()


@app.delete("/tokens/{token_name}")
def api_revoke_token(
    token_name: str, request: Request, user: str = Depends(require_session),
) -> dict:
    target = next((t for t in im.list_api_tokens() if t["name"] == token_name), None)
    if target is not None:
        _check_token_not_wider(request, target["scopes"])
    warnings: list[str] = []
    if not im.revoke_api_token(token_name, on_warning=warnings.append):
        raise HTTPException(404, f"no active token named '{token_name}'.")
    return {"revoked": True, **({"warnings": warnings} if warnings else {})}


@app.get("/instances")
def api_list_instances(request: Request, user: str = Depends(require_session)) -> dict:
    instances = im.list_instances()
    meta = _limited_token(request)
    if meta is not None:
        instances = {n: r for n, r in instances.items() if im.api_token_allows_instance(meta, n)}
    return instances


def _write_temp_ssh_key(key_text: str) -> str:

    fd, path = tempfile.mkstemp(prefix="onboard-ssh-key-")
    try:
        text = key_text if key_text.endswith("\n") else key_text + "\n"
        with os.fdopen(fd, "w") as f:
            f.write(text)
    except BaseException:
        os.remove(path)
        raise
    os.chmod(path, 0o600)
    return path


def _default_public_key_text(request: Request) -> str:

    pub_path = Path(_ssh_key(request) + ".pub")
    try:
        return pub_path.read_text().strip()
    except OSError as e:
        raise engine.ConfigError(
            f"could not read this deployment's own public key at {pub_path} -- needed to "
            "install it on the target instance after a one-time credential is used."
        ) from e


def _resolve_ssh_credential(
    request: Request, ssh_private_key: str | None, ssh_password: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:

    if ssh_private_key:
        temp_key_path = _write_temp_ssh_key(ssh_private_key)
        try:
            return temp_key_path, None, _default_public_key_text(request), temp_key_path
        except Exception:
            with suppress(FileNotFoundError):
                os.remove(temp_key_path)
            raise
    if ssh_password:
        return None, ssh_password, _default_public_key_text(request), None
    return _ssh_key(request), None, None, None


@app.post("/instances")
def api_onboard(
    body: OnboardRequest, request: Request, user: str = Depends(require_session),
) -> dict:
    try:
        name = engine.validate_instance_name(body.name)
    except argparse.ArgumentTypeError as e:
        raise engine.ConfigError(str(e)) from e
    if body.ssh_private_key and body.ssh_password:
        raise HTTPException(
            422, "ssh_private_key and ssh_password are mutually exclusive -- provide at most one."
        )

    temp_key_path: str | None = None
    try:
        ssh_key, ssh_password, install_public_key, temp_key_path = _resolve_ssh_credential(
            request, body.ssh_private_key, body.ssh_password
        )


        result, warnings = _call_collecting_warnings_with_progress(
            lambda on_progress, on_warning: im.onboard_instance(
                _client(request), name, body.instance_id, ssh_key,
                vpc_id=body.vpc_id, force=body.force, ssh_password=ssh_password,
                install_public_key=install_public_key,
                on_progress=on_progress, on_warning=on_warning,
            )
        )
        body_dict = _asdict(result)
        if warnings:
            body_dict["warnings"] = warnings
        return body_dict
    finally:


        if temp_key_path is not None:
            with suppress(FileNotFoundError):
                os.remove(temp_key_path)


@app.post("/instances/{name}/offboard")
def api_offboard(
    name: str, body: OffboardRequest, request: Request, user: str = Depends(require_session),
) -> dict:

    result, warnings = _call_collecting_warnings_with_progress(
        lambda on_progress, on_warning: im.offboard_instance(
            _client(request), name, delete_volumes=body.delete_volumes,
            on_progress=on_progress, on_warning=on_warning,
        )
    )
    body_dict = _asdict(result)
    if warnings:
        body_dict["warnings"] = warnings
    return body_dict


class VpcAddressRequest(BaseModel):
    address: str

    current: str | None = None


@app.post("/instances/{name}/vpc-address")
def api_set_vpc_address(
    name: str, body: VpcAddressRequest, request: Request, user: str = Depends(require_session),
) -> dict:

    previous, warnings = _call_collecting_warnings(
        lambda on_warning: im.set_vpc_address(
            _client(request), name, body.address, current=body.current, on_warning=on_warning,
        )
    )
    return {"name": name, "previous_address": previous, "address": body.address, "warnings": warnings}


@app.post("/instances/{name}/deregister")
def api_deregister(name: str, user: str = Depends(require_session)) -> dict:

    result = im.deregister_instance(name)
    return _asdict(result)


@app.post("/instances/{name}/clear-lock")
def api_clear_lock(name: str, user: str = Depends(require_session)) -> dict:

    result = im.clear_instance_lock(name)
    im.record_activity(f"in-progress mark {result.outcome.replace('_', ' ')}", source="api",
                       instance_name=name, action="clear-lock", actor=user)
    return {"name": name, "outcome": result.outcome}


class MigrateAbandonRequest(BaseModel):
    boot_original: bool = True


@app.post("/instances/{name}/migrate-abandon")
def api_migrate_abandon(name: str, request: Request, body: MigrateAbandonRequest | None = None,
                        wait: bool = False, user: str = Depends(require_session)) -> JSONResponse:

    client = _client(request)
    boot = body.boot_original if body is not None else True

    def _do(op_id: str) -> im.MigrateAbandonResult:
        return im.abandon_migration(client, name, boot_original=boot, actor=user,
                                    on_progress=_make_progress_reporter(op_id),
                                    on_warning=_make_warning_reporter(op_id))

    return _kickoff("migrate_abandon", 4, _do, wait, actor=user)


@app.post("/system/heal")
def api_heal(request: Request, user: str = Depends(require_session)) -> dict:

    result = im.reconcile_fleet(_client(request))
    return dataclasses.asdict(result)


class EnvBackupRecipientsRequest(BaseModel):
    recipients: str


@app.put("/system/env-backup/recipients")
def api_env_backup_recipients(body: EnvBackupRecipientsRequest, user: str = Depends(require_session)) -> dict:

    value = body.recipients.strip()
    if value:
        im.parse_age_recipients(value)
    im._write_env_settings({im.ENV_BACKUP_RECIPIENTS_ENV: value or None})
    im.record_activity(f"encrypted settings backup {'set up' if value else 'turned off'}",
                       source="api", action="env-backup", actor=user)
    result = im.backup_env_encrypted(force=True) if value else None
    return {"status": im.env_backup_status(),
            "result": dataclasses.asdict(result) if result else None}


@app.post("/system/backup/verify")
def api_backup_verify(user: str = Depends(require_session)) -> dict:

    return dataclasses.asdict(im.verify_backups())


@app.post("/system/lease/take-over")
def api_lease_take_over(user: str = Depends(require_session)) -> dict:

    status = im.acquire_or_renew_lease(take_over=True)
    im.record_activity(f"scheduler lease take-over {'succeeded' if status.active else 'failed'}",
                       level="warning", source="api", action="lease", actor=user)
    return dataclasses.asdict(status)


class WatchdogRequest(BaseModel):
    enabled: bool


@app.get("/system/host-watchdog")
def api_host_watchdog(request: Request, user: str = Depends(require_session)) -> dict:
    return im.set_host_watchdog(_client(request), enable=None)


@app.put("/system/host-watchdog")
def api_set_host_watchdog(body: WatchdogRequest, request: Request,
                          user: str = Depends(require_session)) -> dict:
    return im.set_host_watchdog(_client(request), enable=body.enabled)


@app.post("/system/env-backup/run")
def api_env_backup_run(user: str = Depends(require_session)) -> dict:

    result = im.backup_env_encrypted(force=True)
    if result.outcome == "failed":
        raise HTTPException(502, f"encrypted settings backup failed: {result.detail}")
    return dataclasses.asdict(result)


@app.post("/system/ssh-key/protect")
def api_protect_ssh_key(request: Request, user: str = Depends(require_session)) -> dict:

    result = im.protect_deployment_ssh_key(_client(request), im.deployment_ssh_key_path())
    return {**dataclasses.asdict(result), "ok": result.ok}


@app.get("/system/health")
def api_health_problems(user: str = Depends(require_session)) -> dict:

    channels = im.alert_channels()
    return {"problems": [dataclasses.asdict(p) for p in im.check_health()],
            "alerts": {"webhook": channels["webhook"], "email": channels["email"],
                       "email_to": channels["email_to"]}}


class AlertSettingsRequest(BaseModel):
    webhook_url: str | None = None
    email_to: str | None = None
    smtp_host: str | None = None
    smtp_port: str | None = None
    smtp_security: str | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    email_from: str | None = None


@app.get("/system/alerts")
def api_alert_settings(user: str = Depends(require_session)) -> dict:

    return im.alert_settings_summary()


@app.put("/system/alerts")
def api_set_alert_settings(body: AlertSettingsRequest, user: str = Depends(require_session)) -> dict:

    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    summary = im.configure_alerts(changes)
    im.record_activity("alert settings changed", source="api", action="alerts-config", actor=user)
    return summary


@app.post("/system/alerts/test")
def api_alerts_test(user: str = Depends(require_session)) -> dict:

    channels = im.alert_channels()
    if not channels["webhook"] and not channels["email"]:
        raise HTTPException(400, f"no alert channel set up -- set {im.ALERT_WEBHOOK_ENV} and/or "
                                 f"{im.ALERT_EMAIL_ENV} plus LIS_ALERT_SMTP_HOST in .env.")
    text = "Linode Instance Scheduler: test alert -- alerts reach this channel."
    results = {}
    if channels["webhook"]:
        results["webhook"] = im._send_webhook(text, "test", {"status": "test"})
    if channels["email"]:
        results["email"] = im._send_email(text, "test", {"status": "test"})
    return {"results": results}


@app.get("/schedule/missed")
def api_schedule_missed(request: Request, user: str = Depends(require_session)) -> dict:

    names = _visible_instance_names(request)
    return {"mismatches": [dataclasses.asdict(m) for m in im.find_schedule_mismatches(names=names)]}


class CatchUpRequest(BaseModel):
    names: list[str]


@app.post("/schedule/missed/apply")
def api_schedule_missed_apply(body: CatchUpRequest, request: Request, wait: bool = False,
                              user: str = Depends(require_session)) -> JSONResponse:

    allowed = _visible_instance_names(request)
    if allowed is not None:
        outside = sorted(set(body.names) - set(allowed))
        if outside:
            raise HTTPException(403, f"this token doesn't cover: {', '.join(outside)}")
    for n in body.names:
        im.validate_instance_name(n)
    client, ssh_key = _client(request), _ssh_key(request)

    def _do(op_id: str) -> im.CatchUpResult:
        return im.apply_schedule_catch_up(
            client, ssh_key, body.names, actor=user,
            max_parallel=min(5, max(1, len(body.names))),
            client_factory=im._per_thread_client_factory() if len(body.names) > 1 else None,
            on_progress=_make_progress_reporter(op_id),
            on_warning=_make_warning_reporter(op_id))

    return _kickoff("schedule_catch_up", max(1, len(body.names)), _do, wait, actor=user)


@app.get("/instances/{name}/status")
def api_get_status(name: str, user: str = Depends(require_session)) -> dict:
    return im.get_instance_status(name)


@app.get("/instances/{name}/history")
def api_get_history(name: str, limit: int = 50, user: str = Depends(require_session)) -> list[dict]:
    return im.get_schedule_events(name, limit=limit)


@app.post("/instances/{name}/start")
def api_start(
    name: str, body: StartRequest, request: Request, wait: bool = False,
    user: str = Depends(require_session),
) -> JSONResponse:
    client, ssh_key = _client(request), _ssh_key(request)
    if body.respect_dependencies:
        im.require_dependency(name, "start")
    total_steps = _TOTAL_STEPS["start"] + _hook_progress_steps(name, "post_start", body.skip_hooks)

    def _do(op_id: str) -> im.StartResult:
        return im.start_instance(
            client, name, ssh_key, triggered_by="api",
            override_window_hours=body.override_window_hours, actor=user,
            on_progress=_make_progress_reporter(op_id), on_warning=_make_warning_reporter(op_id),
            skip_hooks=body.skip_hooks, respect_dependencies=body.respect_dependencies,
        )

    return _kickoff("start", total_steps, _do, wait, actor=user)


@app.post("/instances/{name}/stop")
def api_stop(
    name: str, body: StopRequest, request: Request, wait: bool = False,
    user: str = Depends(require_session),
) -> JSONResponse:
    client, ssh_key = _client(request), _ssh_key(request)
    if body.respect_dependencies:
        im.require_dependency(name, "stop")
    total_steps = _TOTAL_STEPS["stop"] + _hook_progress_steps(
        name, "pre_stop", body.skip_hooks or body.skip_precapture,
    )

    def _do(op_id: str) -> im.StopResult:
        return im.stop_instance(
            client, name, ssh_key,
            skip_precapture=body.skip_precapture, force=body.force, skip_hooks=body.skip_hooks,
            triggered_by="api", actor=user,
            on_progress=_make_progress_reporter(op_id), on_warning=_make_warning_reporter(op_id),
            respect_dependencies=body.respect_dependencies,
        )

    return _kickoff("stop", total_steps, _do, wait, actor=user)


def _hook_progress_steps(name: str, hook_type: str, skipped: bool) -> int:

    if skipped:
        return 0
    record = im.load_registry().get(name)
    if record is None:
        return 0
    return 1 if im.resolve_effective_hooks(name, record)[hook_type] is not None else 0


@app.post("/instances/{name}/migrate-start")
def api_migrate_start(
    name: str, body: MigrateStartRequest, request: Request, user: str = Depends(require_session),
) -> dict:

    if body.ssh_private_key and body.ssh_password:


        raise HTTPException(
            422, "ssh_private_key and ssh_password are mutually exclusive -- provide at most one."
        )
    client = _client(request)


    ssh_key, ssh_password, install_public_key, temp_key_path = _resolve_ssh_credential(
        request, body.ssh_private_key, body.ssh_password
    )


    total_steps = (_TOTAL_STEPS["migrate_start"] + (1 if install_public_key is not None else 0)
                   + (5 if body.backup else 0))
    op_id = _start_operation("migrate_start", total_steps=total_steps)

    def _do() -> im.MigrateStartResult:
        try:
            return im.migrate_start_instance(
                client, name, body.instance_id, ssh_key,
                ssh_password=ssh_password, install_public_key=install_public_key,
                force=body.force, backup=body.backup,
                on_progress=_make_progress_and_warning_reporter(op_id),
                on_warning=_make_warning_reporter(op_id),
            )
        finally:
            if temp_key_path is not None:
                with suppress(FileNotFoundError):
                    os.remove(temp_key_path)

    threading.Thread(target=_run_job, args=(op_id, _do), daemon=True).start()
    return {"operation_id": op_id, "total_steps": total_steps}


@app.get("/linode/instances/{instance_id}/backup-estimate")
def api_backup_estimate(
    instance_id: int, request: Request, user: str = Depends(require_session),
) -> dict:

    client = _client(request)
    try:
        instance = engine.retry_transient(lambda: client.load(Instance, instance_id))
        _ = instance.status
    except ApiError as e:
        if e.status == 404:
            raise HTTPException(404, f"instance {instance_id} not found.") from e
        raise
    configs = engine.retry_transient(
        lambda: client.get(f"/linode/instances/{instance_id}/configs")).get("data") or []
    devices = (configs[0].get("devices") if len(configs) == 1 else None) or {}
    sizes = [engine.retry_transient(lambda v=e["volume_id"]: client.load(Volume, v)).size
             for e in engine.attached_volumes(devices)]
    return engine.estimate_migration_backup_cost(client, instance, sizes)


@app.get("/backups")
def api_list_backups(user: str = Depends(require_session)) -> list:
    return [im.migration_backup_summary(e) for e in im.list_migration_backups()]


@app.get("/backups/{name}")
def api_get_backup(name: str, user: str = Depends(require_session)) -> dict:
    entry = im.get_migration_backup(name)
    if entry is None:
        raise HTTPException(404, f"no pre-migration backup is recorded for '{name}'.")
    return im.migration_backup_summary(entry)


class RollbackRequest(BaseModel):
    boot: bool = True


@app.post("/backups/{name}/rollback")
def api_rollback(
    name: str, body: RollbackRequest, request: Request, wait: bool = False,
    user: str = Depends(require_session),
) -> JSONResponse:

    if im.get_migration_backup(name) is None:
        raise HTTPException(404, f"no pre-migration backup is recorded for '{name}'.")
    client, ssh_key = _client(request), _ssh_key(request)

    def _do(op_id: str) -> im.RollbackResult:
        return im.rollback_to_backup(
            client, name, ssh_key, boot=body.boot,
            on_progress=_make_progress_reporter(op_id), on_warning=_make_warning_reporter(op_id),
        )

    return _kickoff("rollback", 8, _do, wait, actor=user)


@app.delete("/backups/{name}")
def api_delete_backup(
    name: str, request: Request, wait: bool = False, user: str = Depends(require_session),
) -> JSONResponse:

    if im.get_migration_backup(name) is None:
        raise HTTPException(404, f"no pre-migration backup is recorded for '{name}'.")
    client = _client(request)

    def _do(op_id: str) -> im.BackupDeleteResult:
        return im.delete_migration_backup(
            client, name, on_progress=_make_progress_reporter(op_id),
            on_warning=_make_warning_reporter(op_id),
        )

    return _kickoff("backup_delete", 4, _do, wait, actor=user)


class SystemBackupConfigRequest(BaseModel):
    bucket: str
    endpoint: str
    access_key: str
    secret_key: str


class HolidayRequest(BaseModel):
    date: str
    to: str | None = None
    group_name: str | None = None
    name: str | None = None
    note: str | None = None


@app.get("/holidays")
def api_list_holidays(all: bool = False, user: str = Depends(require_session)) -> list:

    since = None if all else datetime.now(UTC).date() - timedelta(days=1)
    return im.list_holidays(since=since)


@app.post("/holidays")
def api_add_holidays(body: HolidayRequest, user: str = Depends(require_session)) -> dict:

    added, warnings = _call_collecting_warnings(
        lambda on_warning: im.add_holidays(body.date, body.to, group_name=body.group_name,
                                           name=body.name, note=body.note, on_warning=on_warning))
    return {"added": added, "warnings": warnings}


@app.delete("/holidays")
def api_remove_holidays(date: str, to: str | None = None, group_name: str | None = None,
                        name: str | None = None, user: str = Depends(require_session)) -> dict:
    removed, warnings = _call_collecting_warnings(
        lambda on_warning: im.remove_holidays(date, to, group_name=group_name, name=name,
                                              on_warning=on_warning))
    return {"removed": removed, "warnings": warnings}


@app.get("/system/backup")
def api_system_backup_status(user: str = Depends(require_session)) -> dict:

    im.reload_object_storage_settings_if_changed()
    return im.get_system_backup_status()


@app.put("/system/backup/config")
def api_system_backup_configure(
    body: SystemBackupConfigRequest, user: str = Depends(require_session),
) -> dict:

    im.configure_system_backup(body.bucket, body.endpoint, body.access_key, body.secret_key)
    im.record_activity(f"System backup Object Storage settings changed (bucket {body.bucket}).",
                       source="api", actor=user, action="backup-config")
    return im.get_system_backup_status()


@app.delete("/system/backup/config")
def api_system_backup_disable(user: str = Depends(require_session)) -> dict:
    im.disable_system_backup_object_storage()
    im.record_activity("System backup Object Storage settings removed.", source="api", actor=user,
                       action="backup-config")
    return im.get_system_backup_status()


@app.post("/system/backup/test")
def api_system_backup_test(user: str = Depends(require_session)) -> dict:
    im.reload_object_storage_settings_if_changed()
    im.test_system_backup_settings()
    return {"ok": True}


@app.post("/system/backup/run")
def api_system_backup_run(wait: bool = False, user: str = Depends(require_session)) -> JSONResponse:

    im.reload_object_storage_settings_if_changed()
    local_dir = im.default_backup_dir()
    if not im.osb.is_configured() and local_dir is None:
        raise engine.ConfigError("nothing to back up to -- configure Object Storage first.")

    def _do(op_id: str) -> dict:
        result = im.backup_full_system(
            local_dir=local_dir, trigger="api", on_progress=_make_progress_reporter(op_id),
            on_warning=_make_warning_reporter(op_id))
        return {"ok": result.ok, "problems": im._backup_problems(result),
                "object_storage_key": result.object_storage_snapshot_key,
                "local_path": result.local_snapshot_path}

    return _kickoff("system_backup", 6, _do, wait, actor=user)


@app.get("/instances/{name}/migrate-status")
def api_migrate_status(name: str, user: str = Depends(require_session)) -> dict:

    state = im.load_migrations().get(name)
    if state is None:
        return {"in_progress": False}
    return {
        "in_progress": True,
        "phase": state.get("phase"),
        "instance_id": state.get("instance_id"),
        "dest_volume_id": state.get("dest_volume_id"),
        "dest_volume_size_gb": state.get("dest_volume_size_gb"),
        "local_disk_size_mb": state.get("local_disk_size_mb"),
    }


@app.post("/instances/{name}/migrate-resume")
def api_migrate_resume(name: str, request: Request, user: str = Depends(require_session)) -> dict:

    client, ssh_key = _client(request), _ssh_key(request)
    op_id = _start_operation("migrate_resume")

    def _do() -> im.MigrateResumeResult:


        return im.migrate_resume_instance(
            client, name, ssh_key, on_progress=_make_progress_and_warning_reporter(op_id),
            on_warning=_make_warning_reporter(op_id),
        )

    threading.Thread(target=_run_job, args=(op_id, _do), daemon=True).start()
    return {"operation_id": op_id, "total_steps": _TOTAL_STEPS["migrate_resume"]}


@app.get("/instances/{name}/migrate-copy")
def api_migrate_copy_readiness(name: str, request: Request, user: str = Depends(require_session)) -> dict:

    return im.assisted_copy_readiness(_client(request), name, _ssh_key(request))


@app.post("/instances/{name}/migrate-copy")
def api_migrate_copy(name: str, request: Request, user: str = Depends(require_session)) -> dict:

    client, ssh_key = _client(request), _ssh_key(request)
    op_id = _start_operation("migrate_copy")
    counted = _make_progress_and_warning_reporter(op_id)

    def _on_progress(message: str) -> None:
        if message.startswith("Copied ") and " so far " in message:
            with _operations_lock:
                op = _operations.get(op_id)
                if op is not None:
                    op.current_step = message
            return
        counted(message)

    def _do() -> im.AssistedCopyResult:
        return im.run_assisted_migration_copy(
            client, name, ssh_key, on_progress=_on_progress, on_warning=_make_warning_reporter(op_id))

    threading.Thread(target=_run_job, args=(op_id, _do), daemon=True).start()
    return {"operation_id": op_id, "total_steps": _TOTAL_STEPS["migrate_copy"]}


@app.post("/instances/{name}/extend")
def api_extend(name: str, body: ExtendRequest, user: str = Depends(require_session)) -> dict:
    new_expiry = im.extend_manual_override(name, body.hours)
    return {"manual_override_expires_at": new_expiry}


@app.post("/groups/{group_name}/extend")
def api_extend_group(group_name: str, body: ExtendRequest, user: str = Depends(require_session)) -> dict:

    return _asdict(im.extend_group(group_name, body.hours))


@app.get("/instances/{name}/hooks")
def api_get_instance_hooks(name: str, user: str = Depends(require_session)) -> dict:

    record = im.load_registry().get(name)
    if record is None:
        raise im.NotOnboardedError(f"'{name}' is not onboarded.")
    return {
        "own": im.get_instance_hooks(name),
        "effective": im.resolve_effective_hooks(name, record),
        "last_post_start_failure": im.get_last_hook_failure(name, "post_start"),
    }


@app.put("/instances/{name}/hooks")
def api_set_instance_hooks(
    name: str, body: HookConfigRequest, request: Request, user: str = Depends(require_session),
) -> dict:

    own, warnings = _call_collecting_warnings(
        lambda on_warning: im.set_instance_hooks(
            name, body.model_dump(), triggered_by="api", actor=user,
            client=_client(request), on_warning=on_warning,
        )
    )
    result: dict = {"own": own}
    if warnings:
        result["warnings"] = warnings
    return result


@app.delete("/instances/{name}/hooks")
def api_clear_instance_hooks(
    name: str, request: Request, user: str = Depends(require_session),
) -> dict:
    if name not in im.load_registry():
        raise im.NotOnboardedError(f"'{name}' is not onboarded.")
    cleared, warnings = _call_collecting_warnings(
        lambda on_warning: im.clear_instance_hooks(
            name, triggered_by="api", actor=user, client=_client(request), on_warning=on_warning,
        )
    )
    result: dict = {"cleared": cleared}
    if warnings:
        result["warnings"] = warnings
    return result


@app.post("/instances/{name}/hooks/run")
def api_run_instance_hook(
    name: str, body: HookRunRequest, request: Request, user: str = Depends(require_session),
) -> dict:

    result = im.run_hook_now(
        name, body.hook, _ssh_key(request), triggered_by="api", actor=user,
    )
    return {
        "ok": result.ok, "summary": result.summary, "exit_code": result.exit_code,
        "output_tail": result.output_tail, "attempts": result.attempts,
    }


@app.get("/instances/{name}/hook-events")
def api_get_hook_events(
    name: str, limit: int = 50, user: str = Depends(require_session),
) -> list[dict]:
    return im.get_hook_events(name, limit=limit)


@app.get("/groups/{group_name}/hooks")
def api_get_group_hooks(group_name: str, user: str = Depends(require_session)) -> dict:
    return {"hooks": im.get_group_hooks(group_name)}


@app.put("/groups/{group_name}/hooks")
def api_set_group_hooks(
    group_name: str, body: HookConfigRequest, request: Request,
    user: str = Depends(require_session),
) -> dict:
    hooks, warnings = _call_collecting_warnings(
        lambda on_warning: im.set_group_hooks(
            group_name, body.model_dump(), triggered_by="api", actor=user,
            client=_client(request), on_warning=on_warning,
        )
    )
    result: dict = {"hooks": hooks}
    if warnings:
        result["warnings"] = warnings
    return result


@app.delete("/groups/{group_name}/hooks")
def api_clear_group_hooks(
    group_name: str, request: Request, user: str = Depends(require_session),
) -> dict:
    cleared, warnings = _call_collecting_warnings(
        lambda on_warning: im.clear_group_hooks(
            group_name, triggered_by="api", actor=user, client=_client(request),
            on_warning=on_warning,
        )
    )
    result: dict = {"cleared": cleared}
    if warnings:
        result["warnings"] = warnings
    return result


@app.get("/instances/{name}/schedule")
def api_get_schedule(name: str, user: str = Depends(require_session)) -> dict | None:
    return im.get_instance_schedule(name)


@app.post("/instances/{name}/schedule")
def api_set_schedule(
    name: str, body: ScheduleSetRequest, request: Request, user: str = Depends(require_session),
) -> dict:


    _, warnings = _call_collecting_warnings(
        lambda on_warning: im.save_instance_schedule(
            _client(request), name, body.timezone, _rules_as_dicts(body.rules),
            enabled=body.enabled, on_warning=on_warning,
        )
    )
    schedule = im.get_instance_schedule(name)
    if schedule is None:


        raise im.NotOnboardedError(f"'{name}' is not onboarded. Run `onboard` first.")
    result: dict = dict(schedule)
    if warnings:
        result["warnings"] = warnings
    return result


@app.delete("/instances/{name}/schedule")
def api_clear_schedule(name: str, request: Request, user: str = Depends(require_session)) -> dict:
    cleared, warnings = _call_collecting_warnings(
        lambda on_warning: im.clear_instance_schedule(_client(request), name, on_warning=on_warning)
    )
    result: dict = {"cleared": cleared}
    if warnings:
        result["warnings"] = warnings
    return result


@app.patch("/instances/{name}")
def api_patch_instance(
    name: str, body: GroupPatchInstanceRequest, request: Request,
    user: str = Depends(require_session),
) -> dict:
    if body.schedule_mode is not None and "group_name" not in body.model_fields_set:
        _require_token_scope(request, "mode:write")
        mode: str = body.schedule_mode
        _, warnings = _call_collecting_warnings(
            lambda on_warning: im.set_schedule_mode(
                _client(request), name, mode, on_warning=on_warning,
            )
        )
        result: dict = {"schedule_mode": mode}
        if warnings:
            result["warnings"] = warnings
        return result
    _require_token_scope(request, "groups:membership")
    if body.group_name is not None:


        group_name: str = body.group_name
        group_id, warnings = _call_collecting_warnings(
            lambda on_warning: im.assign_instance_to_group(
                _client(request), name, group_name, on_warning=on_warning,
            )
        )
        result = {"group_id": group_id}
        if warnings:
            result["warnings"] = warnings
        return result
    if body.copy_group_rules_as_individual is None:
        raise HTTPException(
            422,
            "clearing group_name requires copy_group_rules_as_individual (true/false) -- there's "
            "no interactive prompt over HTTP, so the caller must decide up front, the same "
            "choice the CLI's own --keep-manual/--copy-schedule flags make explicit.",
        )
    copy_group_rules_as_individual: bool = body.copy_group_rules_as_individual
    outcome, warnings = _call_collecting_warnings(
        lambda on_warning: im.remove_instance_from_group(
            _client(request), name,
            copy_group_rules_as_individual=copy_group_rules_as_individual,
            on_warning=on_warning,
        )
    )
    result = {"outcome": outcome}
    if warnings:
        result["warnings"] = warnings
    return result


@app.post("/groups")
def api_create_group(body: GroupCreateRequest, user: str = Depends(require_session)) -> dict:
    im.create_schedule_group(body.name, body.timezone)
    group = im.get_schedule_group(body.name)
    if group is None:


        raise im.GroupNotFoundError(f"no schedule group named '{body.name}' exists.")
    return group


@app.get("/groups")
def api_list_groups(request: Request, user: str = Depends(require_session)) -> list[dict]:
    groups = im.list_schedule_groups()
    meta = _limited_token(request)
    if meta is not None:
        groups = [g for g in groups if im.api_token_allows_group(meta, g["name"])]
    return groups


@app.get("/groups/{group_name}")
def api_get_group(group_name: str, user: str = Depends(require_session)) -> dict:
    group = im.get_schedule_group(group_name)
    if group is None:
        raise HTTPException(404, f"no schedule group named '{group_name}' exists.")
    return group


class GroupActionRequest(BaseModel):


    with_dependencies: bool = False
    respect_dependencies: bool = False
    skip_hooks: bool = False
    max_parallel: int = im.DEFAULT_GROUP_ACTION_MAX_PARALLEL


def _group_action_route(
    group_name: str, action: str, body: GroupActionRequest, request: Request, wait: bool,
    user: str,
) -> JSONResponse:
    client, ssh_key = _client(request), _ssh_key(request)
    act: Literal["start", "stop"] = "start" if action == "start" else "stop"
    stages = im._group_chain(group_name, act) if body.with_dependencies else [group_name]
    if not body.with_dependencies and im.get_schedule_group(group_name) is None:
        raise im.GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    if body.respect_dependencies and not body.with_dependencies:
        for member in im.get_schedule_group(group_name)["members"]:
            im.require_dependency(member, act)
    total = sum(len((im.get_schedule_group(g) or {}).get("members", [])) for g in stages)

    def _do(op_id: str) -> im.GroupActionResult:
        def _step(message: str) -> None:
            with _operations_lock:
                op = _operations.get(op_id)
                if op is not None:
                    op.current_step = message

        def _member_done(member: im.GroupMemberResult) -> None:
            with _operations_lock:
                op = _operations.get(op_id)
                if op is not None:
                    op.completed_steps += 1
                    op.current_step = f"{member.group}/{member.name}: {member.outcome}"

        return im.group_action(
            client, group_name, act, ssh_key,
            include_dependencies=body.with_dependencies,
            respect_dependencies=body.respect_dependencies,
            triggered_by="api", actor=user, skip_hooks=body.skip_hooks,
            max_parallel=body.max_parallel,
            client_factory=im._per_thread_client_factory() if body.max_parallel > 1 else None,
            on_progress=_step, on_warning=_make_warning_reporter(op_id),
            on_member_done=_member_done,
        )

    return _kickoff(f"group_{act}", max(total, 1), _do, wait, actor=user)


@app.post("/groups/{group_name}/start")
def api_group_start(
    group_name: str, body: GroupActionRequest, request: Request, wait: bool = False,
    user: str = Depends(require_session),
) -> JSONResponse:
    return _group_action_route(group_name, "start", body, request, wait, user)


@app.post("/groups/{group_name}/stop")
def api_group_stop(
    group_name: str, body: GroupActionRequest, request: Request, wait: bool = False,
    user: str = Depends(require_session),
) -> JSONResponse:
    return _group_action_route(group_name, "stop", body, request, wait, user)


class GroupPatchRequest(BaseModel):


    depends_on: list[str] | str | None


@app.patch("/groups/{group_name}")
def api_patch_group(
    group_name: str, body: GroupPatchRequest, request: Request,
    user: str = Depends(require_session),
) -> dict:
    meta = _limited_token(request)
    if meta is not None:


        targets = im.normalize_dependency_list(body.depends_on)
        outside = [t for t in targets if not im.api_token_allows_group(meta, t)]
        if outside:
            raise HTTPException(
                status_code=403,
                detail=f"this token isn't allowed to act on group(s) {', '.join(outside)}.",
            )
    _, warnings = _call_collecting_warnings(
        lambda on_warning: im.set_group_dependencies(
            _client(request), group_name, body.depends_on, on_warning=on_warning,
        )
    )
    group = im.get_schedule_group(group_name)
    if group is None:
        raise im.GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    result: dict = dict(group)
    if warnings:
        result["warnings"] = warnings
    return result


class HolidaySettingRequest(BaseModel):

    account_holidays: Literal["follow", "ignore", "inherit"]


@app.put("/groups/{group_name}/holiday-setting")
def api_set_group_holiday_setting(
    group_name: str, body: HolidaySettingRequest, request: Request,
    user: str = Depends(require_session),
) -> dict:
    if body.account_holidays == "inherit":
        raise HTTPException(422, "a group follows or ignores account-wide holidays; 'inherit' is "
                                 "for a node.")
    mode: str = body.account_holidays
    _, warnings = _call_collecting_warnings(
        lambda on_warning: im.set_group_account_holidays(
            _client(request), group_name, mode, on_warning=on_warning)
    )
    result: dict = {"account_holidays": mode}
    if warnings:
        result["warnings"] = warnings
    return result


@app.put("/instances/{name}/holiday-setting")
def api_set_instance_holiday_setting(
    name: str, body: HolidaySettingRequest, request: Request,
    user: str = Depends(require_session),
) -> dict:
    mode = None if body.account_holidays == "inherit" else body.account_holidays
    result, warnings = _call_collecting_warnings(
        lambda on_warning: im.set_instance_account_holidays(
            _client(request), name, mode, on_warning=on_warning)
    )
    out: dict = dict(result)
    if warnings:
        out["warnings"] = warnings
    return out


@app.delete("/groups/{group_name}")
def api_delete_group(group_name: str, user: str = Depends(require_session)) -> dict:
    im.delete_schedule_group(group_name)
    return {"deleted": True}


@app.post("/groups/{group_name}/schedule")
def api_set_group_schedule(
    group_name: str, body: ScheduleSetRequest, request: Request,
    user: str = Depends(require_session),
) -> dict:


    _, warnings = _call_collecting_warnings(
        lambda on_warning: im.set_group_schedule(
            _client(request), group_name, body.timezone, _rules_as_dicts(body.rules),
            enabled=body.enabled, on_warning=on_warning,
        )
    )
    group = im.get_schedule_group(group_name)
    if group is None:


        raise im.GroupNotFoundError(f"no schedule group named '{group_name}' exists.")
    result: dict = dict(group)
    if warnings:
        result["warnings"] = warnings
    return result


MAX_SAVINGS_WINDOW_DAYS = 3650


def _validate_savings_days(days: int) -> None:
    if not (0 <= days <= MAX_SAVINGS_WINDOW_DAYS):
        raise engine.ConfigError(
            f"days must be between 0 and {MAX_SAVINGS_WINDOW_DAYS}, got {days}."
        )


def _savings_for_schedule(name_for_events: str, schedule: dict | None, days: int) -> dict:

    _validate_savings_days(days)
    if not im.schedule_is_active(schedule):
        return {
            "scheduled_savings_percent": None, "actual_savings_percent": None,
            "window_days": days, "schedule_state": "none" if schedule is None else "disabled",
        }
    scheduled = im.compute_scheduled_savings_percent(schedule)
    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(days=days)
    events = im.get_schedule_events_since(name_for_events, window_start)
    actual = im.compute_actual_savings_percent(events, window_start, window_end)
    return {
        "scheduled_savings_percent": scheduled, "actual_savings_percent": actual,
        "window_days": days, "schedule_state": "active",
    }


@app.get("/instances/{name}/savings")
def api_instance_savings(
    name: str, days: int = DEFAULT_SAVINGS_WINDOW_DAYS, user: str = Depends(require_session),
) -> dict:
    record = im.load_registry().get(name)
    if record is None:
        raise HTTPException(404, f"'{name}' is not onboarded.")
    schedule, _via_group = im.resolve_effective_schedule(name, record)
    return _savings_for_schedule(name, schedule, days)


@app.get("/groups/{group_name}/savings")
def api_group_savings(
    group_name: str, days: int = DEFAULT_SAVINGS_WINDOW_DAYS, user: str = Depends(require_session),
) -> dict:


    group = im.get_schedule_group(group_name)
    if group is None:
        raise HTTPException(404, f"no schedule group named '{group_name}' exists.")
    _validate_savings_days(days)
    schedule = {"timezone": group["timezone"], "rules": group["rules"], "enabled": group["enabled"]}


    active = im.schedule_is_active(schedule)
    scheduled = im.compute_scheduled_savings_percent(schedule) if active else None


    schedule_state = "none" if not schedule["rules"] else ("active" if active else "disabled")
    return {
        "scheduled_savings_percent": scheduled,
        "actual_savings_percent": None,
        "window_days": days,
        "schedule_state": schedule_state,
        "note": "actual_savings_percent isn't computed at the group level -- each member has its "
        "own uptime history; check GET /instances/{name}/savings per member.",
    }


def _asdict(result) -> dict:

    if isinstance(result, dict):
        return result
    data = dataclasses.asdict(result)
    if isinstance(result, im.GroupActionResult):
        data["ok"] = result.ok
    return data


_WEB_DIST = engine.BASE_DIR / "web" / "dist"
_DASHBOARD_BUILT = _WEB_DIST.is_dir()
if _DASHBOARD_BUILT:
    from fastapi.staticfiles import StaticFiles

    app.mount("/ui", StaticFiles(directory=str(_WEB_DIST), html=True), name="ui")


@app.get("/", include_in_schema=False, response_model=None)
def root_redirect() -> RedirectResponse | dict:


    if not _DASHBOARD_BUILT:
        return {"message": "Dashboard not built (web/dist missing) -- API docs at /docs."}
    return RedirectResponse("/ui/")


SCHEDULER_STALE_TICKS = 3


def _scheduler_status() -> dict:
    beat = im.get_scheduler_heartbeat()
    if beat is None:
        return {"running": False, "last_tick": None, "interval_seconds": None,
                "message": "The scheduler hasn't recorded a check yet."}
    interval = beat.get("interval_seconds") or im.DEFAULT_POLL_INTERVAL_SECONDS
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(beat["last_tick"])).total_seconds()
    running = age <= interval * SCHEDULER_STALE_TICKS + 60
    return {**beat, "age_seconds": int(age), "running": running,
            "message": None if running else "The scheduler hasn't checked in recently; it may be stopped."}


@app.get("/activity")
def api_activity(
    request: Request, name: str | None = None, group_name: str | None = None,
    level: str | None = None, source: str | None = None, q: str | None = None,
    after_id: int | None = None, before_id: int | None = None, limit: int = 200,
    user: str = Depends(require_session),
) -> dict:

    names: list[str] | None = None
    include_unscoped = True
    if name is not None:
        names, include_unscoped = [name], False
    elif group_name is not None:
        group = im.get_schedule_group(group_name)
        if group is None:
            raise im.GroupNotFoundError(f"no schedule group named '{group_name}'.")
        names = [n for n, r in im.load_registry().items() if r.get("group_id") == group["id"]]
        include_unscoped = False
    limited = _limited_token(request)
    if limited is not None:
        allowed = [n for n in im.load_registry() if im.api_token_allows_instance(limited, n)]
        names = allowed if names is None else [n for n in names if n in allowed]
        include_unscoped = False
    entries = im.get_activity(
        names=names, level=level, source=source, query=q, after_id=after_id,
        before_id=before_id, limit=limit, include_unscoped=include_unscoped,
    )
    return {"entries": entries, "scheduler": _scheduler_status()}


LOG_SERVICES = ("scheduler", "api", "backup", "console")
MAX_LOG_TAIL_LINES = 5000
_MAX_LOG_CHUNK_BYTES = 1024 * 1024


_CONSOLE_SESSION_RE = re.compile(r"lish-[a-z0-9][a-z0-9-]{0,63}")


def _log_path(service: str) -> Path:

    if service in LOG_SERVICES or _CONSOLE_SESSION_RE.fullmatch(service):
        return im.service_log_dir() / f"{service}.log"
    raise HTTPException(404, f"no log named '{service}' (available: {', '.join(LOG_SERVICES)}, "
                             "or lish-<name> for a migration's console session).")


def _console_sessions() -> list[dict]:

    out = []
    for path in im.service_log_dir().glob("lish-*.log"):
        name = path.stem
        if not _CONSOLE_SESSION_RE.fullmatch(name):
            continue
        st = path.stat()
        out.append({"name": name, "instance": name[len("lish-"):], "exists": True,
                    "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()})
    return sorted(out, key=lambda e: e["modified"], reverse=True)


@app.get("/logs")
def api_logs_index(user: str = Depends(require_session)) -> dict:

    services = []
    for service in LOG_SERVICES:
        path = im.service_log_dir() / f"{service}.log"
        exists = path.exists()
        services.append({
            "name": service, "exists": exists,
            "size": path.stat().st_size if exists else 0,
            "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            if exists else None,
        })
    db = Path(im.REGISTRY_PATH)
    wal = db.with_name(db.name + "-wal")
    database = {
        "path": str(db), "size": db.stat().st_size if db.exists() else 0,
        "wal_size": wal.stat().st_size if wal.exists() else 0,
        "activity_retention_days": im.activity_retention_days(),
    }
    return {"services": services, "console_sessions": _console_sessions(), "database": database,
            "scheduler": _scheduler_status()}


@app.get("/logs/{service}")
def api_log_tail(
    service: str, lines: int = 500, offset: int | None = None,
    user: str = Depends(require_session),
) -> dict:

    if lines < 1 or lines > MAX_LOG_TAIL_LINES:
        raise HTTPException(400, f"lines must be from 1 to {MAX_LOG_TAIL_LINES}.")
    path = _log_path(service)
    if not path.exists():
        return {"lines": [], "offset": 0, "reset": offset is not None and offset > 0}
    size = path.stat().st_size
    reset = False
    with path.open("rb") as f:
        if offset is not None:
            if offset > size:
                offset, reset = 0, True
            f.seek(max(offset, size - _MAX_LOG_CHUNK_BYTES))
            data = f.read(_MAX_LOG_CHUNK_BYTES)
            end = f.tell()
            text = data.decode("utf-8", "replace")
            if not text.endswith("\n") and "\n" in text:
                cut = text.rindex("\n") + 1
                end -= len(text[cut:].encode("utf-8"))
                text = text[:cut]
            elif not text.endswith("\n"):
                end -= len(data)
                text = ""
            return {"lines": text.splitlines(), "offset": end, "reset": reset}
        f.seek(max(0, size - _MAX_LOG_CHUNK_BYTES))
        tail = f.read().decode("utf-8", "replace").splitlines()[-lines:]
        return {"lines": tail, "offset": size, "reset": False}


@app.delete("/logs/{service}")
def api_delete_console_session(service: str, user: str = Depends(require_session)) -> dict:

    if not _CONSOLE_SESSION_RE.fullmatch(service):
        raise HTTPException(400, "only a migration console session (lish-<name>) can be deleted.")
    name = service[len("lish-"):]
    if not im.console_session_files(name):
        raise HTTPException(404, f"no console session log for '{name}'.")
    removed = im.delete_console_session(name)
    im.record_activity(f"Deleted the console session log for '{name}'.", source="api",
                       instance_name=name, action="console-session-delete", actor=user)
    return {"deleted": removed}


CONSOLE_COMMANDS = (
    "list", "status", "history", "start", "stop", "extend", "onboard", "offboard", "deregister",
    "migrate-start", "migrate-resume", "migrate-copy", "migrate-orphans", "schedule-set",
    "schedule-show",
    "schedule-clear", "hooks-set", "hooks-show", "hooks-clear", "hooks-run", "set-mode",
    "group-create", "group-schedule-set", "group-show", "group-list", "group-depends",
    "group-delete", "group-add", "group-remove", "api-token-list", "api-token-scopes",
    "api-token-revoke",
    "clear-lock", "set-vpc-address", "reset-host-key", "rebuild", "backup", "heal",
    "migrate-abandon", "schedule-catch-up", "ssh-key-protect", "env-backup", "alerts-check",
    "backup-verify", "scheduler-lease", "host-watchdog", "alerts-test",
    "backup-list", "rollback", "backup-delete", "console-session-delete", "holiday-add", "holiday-remove", "holiday-list", "holiday-settings",
)
CONSOLE_MAX_RUNTIME_S = 3 * 3600
CONSOLE_MAX_LINES = 20000
_CONSOLE_KEEP_RUNS = 50


class ConsoleRequest(BaseModel):
    command: str


@dataclasses.dataclass
class _ConsoleRun:
    id: str
    command: str
    actor: str
    started_at: str
    status: str = "running"
    exit_code: int | None = None
    finished_at: str | None = None
    lines: list[str] = dataclasses.field(default_factory=list)
    truncated: bool = False
    process: object = None


_console_runs: OrderedDict[str, _ConsoleRun] = OrderedDict()
_console_lock = threading.Lock()


def _parse_console_command(command: str) -> list[str]:
    import shlex

    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise HTTPException(400, f"could not parse the command: {e}") from e
    if argv and argv[0] in ("instance_manager.py", "./instance_manager.py"):
        argv = argv[1:]
    if not argv:
        raise HTTPException(400, "type a command, e.g. `status --name web-1` or `--help`.")
    if argv[0] in ("-h", "--help", "help"):
        return ["--help"]
    if argv[0] not in CONSOLE_COMMANDS:
        raise HTTPException(
            400, f"'{argv[0]}' isn't available in the console. Available: "
            f"{', '.join(CONSOLE_COMMANDS)}.")
    return argv


def _console_view(run: _ConsoleRun, offset: int = 0) -> dict:
    return {
        "id": run.id, "command": run.command, "actor": run.actor, "status": run.status,
        "exit_code": run.exit_code, "started_at": run.started_at, "finished_at": run.finished_at,
        "lines": run.lines[offset:], "next_offset": len(run.lines), "truncated": run.truncated,
    }


def _require_dashboard_session(request: Request) -> None:
    if getattr(request.state, "api_token", None) is not None:
        raise HTTPException(403, "the console is only available to dashboard logins, not API tokens.")


@app.get("/console/commands")
def api_console_commands(request: Request, user: str = Depends(require_session)) -> dict:
    _require_dashboard_session(request)
    return {"commands": list(CONSOLE_COMMANDS)}


@app.post("/console/run")
def api_console_run(
    body: ConsoleRequest, request: Request, user: str = Depends(require_session),
) -> dict:

    import subprocess

    _require_dashboard_session(request)
    argv = _parse_console_command(body.command)
    run = _ConsoleRun(id=uuid.uuid4().hex, command=" ".join(argv), actor=user,
                      started_at=datetime.now(timezone.utc).isoformat())
    log = im._RotatingLogFile("console")
    log.write_line("RUN", f"{user} $ {run.command}")
    im.record_activity(f"$ {run.command}", source="console", actor=user, action="console")
    env = {**os.environ, "LIS_NONINTERACTIVE": "1", "PYTHONUNBUFFERED": "1"}
    script = Path(im.__file__).resolve()
    try:
        process = subprocess.Popen(
            [sys.executable, str(script), *argv], cwd=str(script.parent), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except OSError as e:
        raise HTTPException(500, f"could not start the command: {e}") from e
    run.process = process
    with _console_lock:
        _console_runs[run.id] = run
        while len(_console_runs) > _CONSOLE_KEEP_RUNS:
            oldest = next(iter(_console_runs.values()))
            if oldest.status == "running":
                break
            _console_runs.popitem(last=False)

    def _reader() -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.rstrip("\n")
            log.write_line("OUT", line)
            with _console_lock:
                if len(run.lines) < CONSOLE_MAX_LINES:
                    run.lines.append(line)
                else:
                    run.truncated = True

    def _watch() -> None:
        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        try:
            code = process.wait(timeout=CONSOLE_MAX_RUNTIME_S)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait()
            with _console_lock:
                run.lines.append(f"[stopped: ran longer than {CONSOLE_MAX_RUNTIME_S // 3600} hours]")
        reader.join(timeout=10)
        with _console_lock:
            run.exit_code = code
            if run.status != "cancelled":
                run.status = "done" if code == 0 else "error"
            run.finished_at = datetime.now(timezone.utc).isoformat()
        log.write_line("RUN", f"{user} $ {run.command} -> exit {code}")
        im.record_activity(
            f"$ {run.command} -> exit {code}", level="info" if code == 0 else "warning",
            source="console", actor=user, action="console",
        )

    threading.Thread(target=_watch, daemon=True).start()
    return _console_view(run)


@app.get("/console/runs")
def api_console_runs(request: Request, user: str = Depends(require_session)) -> list[dict]:
    _require_dashboard_session(request)
    with _console_lock:
        return [
            {k: v for k, v in _console_view(r).items() if k != "lines"}
            for r in reversed(_console_runs.values())
        ]


@app.get("/console/runs/{run_id}")
def api_console_run_output(
    run_id: str, request: Request, offset: int = 0, user: str = Depends(require_session),
) -> dict:
    _require_dashboard_session(request)
    with _console_lock:
        run = _console_runs.get(run_id)
        if run is None:
            raise HTTPException(404, "no such console run (it may have expired).")
        return _console_view(run, max(0, offset))


@app.post("/console/runs/{run_id}/cancel")
def api_console_cancel(run_id: str, request: Request, user: str = Depends(require_session)) -> dict:
    _require_dashboard_session(request)
    with _console_lock:
        run = _console_runs.get(run_id)
        if run is None:
            raise HTTPException(404, "no such console run (it may have expired).")
        if run.status == "running":
            run.status = "cancelled"
            run.lines.append(f"[cancelled by {user}]")
            proc = run.process
    if run.status == "cancelled" and proc is not None:
        with suppress(Exception):
            proc.terminate()
    return _console_view(run)


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Linode Instance Scheduler API server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        run_server(args.host, args.port)
    except engine.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
