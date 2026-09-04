"""The loopback server behind `grogu review`, and the Mermaid asset installer.

This module owns a web server on the user's own machine and the boundary
around it. Three things are load-bearing and none of them may be simplified
away:

* **The agent-identity trap.** ``PlanStore.read_stage`` binds an *identified*
  agent to one role for the life of a plan. The user's own shell may already
  carry another role's binding, so before the first read we set a dedicated
  ``GROGU_AGENT`` with ``setdefault`` -- only when one is not already set -- and
  we never set, unset or change ``GROGU_ROLE`` anywhere.
* **Role isolation.** Every stage body is read through
  ``PlanStore.read_stage`` with the effective role, so a sealed stage never
  appears in any response byte for a role that may not read it. The server adds
  no second read path.
* **Approval authority.** ``POST /api/approve`` calls ``ReviewStore.approve``
  unchanged, which calls ``PlanStore.approve``, which refuses any caller with
  ``current_role()`` set. The workspace grants no authority the CLI does not.

The security envelope (a ``Host`` allowlist, an ``Origin`` allowlist, a
``Sec-Fetch-Site`` check, a single-use launch token exchanged for an
``HttpOnly``/``SameSite=Strict`` cookie, an ``X-Grogu-Review`` header on every
API call, no CORS headers ever, a strict CSP, and path-traversal-proof static
serving) is a DNS-rebinding and cross-origin defence, not decoration. It is
built before the routes, deliberately.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

import grogu_plans
import grogu_review

# -- the workspace's own files ------------------------------------------------

WORKSPACE_DIR = Path(__file__).resolve().parent / "review_workspace"
STATIC_FILES = ("app.js", "format.js", "anchors.js", "mermaid_anchors.js", "app.css")

# -- the optional, pinned, hash-verified Mermaid asset ------------------------

MERMAID_VERSION = "11.17.2"
# Downloaded from the URL below on 2026-09-04 and hashed with SHA-256; the
# jsdelivr CDN is content-addressed and immutable per version, so this pin
# cannot move underneath us. `--install` refuses on any mismatch and installs
# nothing, which turns a supply-chain substitution into a hard stop rather than
# a silent execution of unexpected code inside the review page.
MERMAID_URL = f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/dist/mermaid.min.js"
MERMAID_SHA256 = "581ed7d74bd9048d0e3a91363927d72ef22942d7722546b27f7cc29e35390eb8"
MERMAID_LICENSE_URL = f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/LICENSE"

MAX_BODY_BYTES = 64 * 1024
DEFAULT_TIMEOUT = 3600

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)


class ReviewServerError(Exception):
    """A usage error in starting or configuring the server."""


# -- Mermaid asset management -------------------------------------------------


def grogu_home() -> Path:
    home = os.environ.get("GROGU_HOME", "").strip()
    return Path(home) if home else Path.home() / ".grogu"


def _installed_asset_dir() -> Path:
    return grogu_home() / "assets" / "review" / f"mermaid-{MERMAID_VERSION}"


def mermaid_asset_path() -> Optional[Path]:
    """The Mermaid bundle to serve, or ``None`` when none is available.

    ``GROGU_REVIEW_MERMAID`` points at an existing file used as-is; this is the
    air-gapped path. Otherwise the pinned install location is used when present.
    """
    override = os.environ.get("GROGU_REVIEW_MERMAID", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    installed = _installed_asset_dir() / "mermaid.min.js"
    return installed if installed.is_file() else None


def assets_status() -> dict:
    path = mermaid_asset_path()
    return {
        "mermaid": path is not None,
        "version": MERMAID_VERSION,
        "path": str(path) if path else "",
        "url": MERMAID_URL,
        "sha256": MERMAID_SHA256,
    }


def install_asset(*, source: str = "") -> dict:
    """Install the pinned Mermaid bundle, verifying its digest.

    With ``source`` the file is copied from a local path (no network). Without
    it the pinned URL is downloaded. Either way the digest must match or nothing
    is written.
    """
    if source:
        data = Path(source).expanduser().read_bytes()
    else:
        with urllib.request.urlopen(MERMAID_URL, timeout=120) as response:  # noqa: S310
            data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(digest, MERMAID_SHA256):
        raise ReviewServerError(
            f"the digest did not match (expected {MERMAID_SHA256}, got {digest}); "
            "nothing was installed"
        )
    target_dir = _installed_asset_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "mermaid.min.js"
    tmp = target.with_name(f"mermaid.min.js.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    # The upstream licence travels with the bundle. A failure to fetch it is not
    # a reason to refuse the install, but we record what we tried.
    license_path = target_dir / "LICENSE"
    license_written = False
    if source:
        candidate = Path(source).expanduser().parent / "LICENSE"
        if candidate.is_file():
            license_path.write_bytes(candidate.read_bytes())
            license_written = True
    else:
        try:
            with urllib.request.urlopen(MERMAID_LICENSE_URL, timeout=60) as response:  # noqa: S310
                license_path.write_bytes(response.read())
            license_written = True
        except OSError:
            license_written = False
    return {
        "installed": str(target),
        "version": MERMAID_VERSION,
        "bytes": len(data),
        "sha256": digest,
        "license": license_written,
    }


# -- the server ---------------------------------------------------------------


class _ReviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, context: "_Context") -> None:
        super().__init__(address, handler)
        self.context = context


class _Context:
    """Everything a request handler needs, assembled once per run."""

    def __init__(
        self,
        *,
        plan_id: str,
        role: str,
        store: grogu_plans.PlanStore,
        review: grogu_review.ReviewStore,
        stage: str,
        timeout: float,
    ) -> None:
        self.plan_id = plan_id
        self.role = role
        self.store = store
        self.review = review
        self.stage = stage
        self.timeout = timeout
        self.token = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
        self.host = "127.0.0.1"
        self.port = 0
        self.assets = assets_status()
        self._last_activity = time.monotonic()
        self._lock = threading.Lock()
        self.httpd: Optional[_ReviewServer] = None

    def touch(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_activity

    def host_allowlist(self) -> frozenset:
        return frozenset(
            {
                f"127.0.0.1:{self.port}",
                f"localhost:{self.port}",
                f"[::1]:{self.port}",
            }
        )

    def origin_allowlist(self) -> frozenset:
        return frozenset(
            {
                f"http://127.0.0.1:{self.port}",
                f"http://localhost:{self.port}",
                f"http://[::1]:{self.port}",
            }
        )


class _Handler(BaseHTTPRequestHandler):
    server_version = "grogu-review"
    protocol_version = "HTTP/1.1"

    # -- silence ------------------------------------------------------------

    def log_message(self, *args, **kwargs) -> None:
        # The token, the cookie and comment text must never reach stdout, the
        # activity log, telemetry, or a traceback. Say nothing.
        return

    # -- helpers ------------------------------------------------------------

    @property
    def context(self) -> _Context:
        return self.server.context  # type: ignore[attr-defined]

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")

    def _send(self, status: int, body: bytes, content_type: str, *, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        for key, value in (extra or []):
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        # An error may be returned before a POST body has been read; on a
        # keep-alive connection the unread body would desync the next request.
        # Close the connection rather than risk it.
        self.close_connection = True
        self._json(status, {"error": message})

    def _cookie_present(self) -> bool:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "grogu_review" and hmac.compare_digest(value, self.context.cookie):
                return True
        return False

    def _envelope_ok(self, path: str) -> bool:
        """The DNS-rebinding and cross-origin defence, run before any route."""
        ctx = self.context
        host = self.headers.get("Host", "")
        if host not in ctx.host_allowlist():
            self._error(HTTPStatus.FORBIDDEN, "host not allowed")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ctx.origin_allowlist():
            self._error(HTTPStatus.FORBIDDEN, "origin not allowed")
            return False
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site not in ("same-origin", "none"):
            self._error(HTTPStatus.FORBIDDEN, "cross-site request refused")
            return False
        if path.startswith("/api/"):
            if self.headers.get("X-Grogu-Review") != "1":
                self._error(HTTPStatus.FORBIDDEN, "missing X-Grogu-Review header")
                return False
        return True

    def _read_body(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY_BYTES:
            self._error(HTTPStatus.BAD_REQUEST, "request body too large")
            return None
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf8"))
        except (ValueError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid JSON body")
            return None
        return value if isinstance(value, dict) else {}

    # -- dispatch -----------------------------------------------------------

    def do_GET(self) -> None:
        self.context.touch()
        parsed = urlsplit(self.path)
        path = parsed.path
        if not self._envelope_ok(path):
            return
        # The one unauthenticated route: exchange the launch token for a
        # cookie, then redirect so the token does not persist in history.
        if path == "/" and "t=" in (parsed.query or ""):
            self._token_exchange(parse_qs(parsed.query))
            return
        if not self._cookie_present():
            self._error(HTTPStatus.FORBIDDEN, "no review session; launch with `grogu review`")
            return
        if path == "/":
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if name not in STATIC_FILES:
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            self._serve_static(name)
            return
        if path == "/vendor/mermaid.min.js":
            self._serve_asset()
            return
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/plan":
            self._api_plan()
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        self.context.touch()
        parsed = urlsplit(self.path)
        path = parsed.path
        if not self._envelope_ok(path):
            return
        if not self._cookie_present():
            self._error(HTTPStatus.FORBIDDEN, "no review session; launch with `grogu review`")
            return
        if not path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        body = self._read_body()
        if body is None:
            return
        try:
            self._route_post(path, body)
        except grogu_review.ReviewError as error:
            self._error(HTTPStatus.CONFLICT, str(error))
        except grogu_plans.PlanError as error:
            # Approval and steering refusals surface here unchanged.
            self._error(HTTPStatus.CONFLICT, str(error))
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))

    def _route_post(self, path: str, body: dict) -> None:
        ctx = self.context
        plan_id = ctx.plan_id
        if path == "/api/threads":
            stage = str(body.get("stage", ""))
            anchor = body.get("anchor")
            text = str(body.get("body", ""))
            if not isinstance(anchor, dict):
                self._error(HTTPStatus.BAD_REQUEST, "anchor is required")
                return
            thread = ctx.review.add_thread(
                plan_id, stage=stage, anchor=anchor, body=text, author=grogu_plans_actor()
            )
            self._json(HTTPStatus.OK, thread)
            return
        if path.startswith("/api/threads/"):
            self._route_thread(path, body)
            return
        if path == "/api/request-changes":
            note = str(body.get("note", ""))
            result = ctx.review.request_changes(plan_id, note=note, role=ctx.role)
            self._json(HTTPStatus.OK, result)
            return
        if path == "/api/approve":
            confirm = bool(body.get("confirm_open", False))
            note = str(body.get("note", ""))
            result = ctx.review.approve(plan_id, confirm_open=confirm, note=note)
            self._json(HTTPStatus.OK, result)
            return
        if path == "/api/shutdown":
            self._json(HTTPStatus.OK, {"ok": True})
            threading.Thread(target=ctx.httpd.shutdown, daemon=True).start()
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def _route_thread(self, path: str, body: dict) -> None:
        ctx = self.context
        rest = path[len("/api/threads/"):]
        parts = rest.split("/")
        thread_id = parts[0]
        action = parts[1] if len(parts) > 1 else ""
        if action == "comments":
            text = str(body.get("body", ""))
            thread = ctx.review.reply(ctx.plan_id, thread_id, text, author=grogu_plans_actor())
            self._json(HTTPStatus.OK, thread)
            return
        if action == "resolve":
            note = str(body.get("note", ""))
            thread = ctx.review.resolve_thread(
                ctx.plan_id, thread_id, note=note, author=grogu_plans_actor()
            )
            self._json(HTTPStatus.OK, thread)
            return
        if action == "reopen":
            thread = ctx.review.reopen_thread(ctx.plan_id, thread_id)
            self._json(HTTPStatus.OK, thread)
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    # -- route bodies -------------------------------------------------------

    def _token_exchange(self, query: dict) -> None:
        supplied = (query.get("t") or [""])[0]
        if not hmac.compare_digest(supplied, self.context.token):
            self._error(HTTPStatus.FORBIDDEN, "invalid launch token")
            return
        cookie = (
            f"grogu_review={self.context.cookie}; HttpOnly; SameSite=Strict; Path=/"
        )
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def _serve_static(self, name: str) -> None:
        self._serve_file(WORKSPACE_DIR, name)

    def _serve_asset(self) -> None:
        path = mermaid_asset_path()
        if path is None:
            self._error(HTTPStatus.NOT_FOUND, "mermaid asset not installed")
            return
        try:
            data = path.read_bytes()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "mermaid asset not installed")
            return
        self._send(HTTPStatus.OK, data, "text/javascript; charset=utf-8")

    def _serve_file(self, root: Path, name: str) -> None:
        root = root.resolve()
        try:
            resolved = (root / name).resolve()
        except (OSError, ValueError):
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        # No `..`, no symlink escape, no directory listing.
        if resolved.parent != root or not resolved.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        try:
            data = resolved.read_bytes()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        self._send(HTTPStatus.OK, data, _content_type(resolved.name))

    def _api_plan(self) -> None:
        ctx = self.context
        try:
            payload = build_plan_state(ctx.store, ctx.review, ctx.plan_id, role=ctx.role)
        except grogu_review.ReviewError as error:
            self._error(HTTPStatus.CONFLICT, str(error))
            return
        except grogu_plans.PlanError as error:
            self._error(HTTPStatus.NOT_FOUND, str(error))
            return
        payload["assets"] = {"mermaid": ctx.assets["mermaid"], "version": MERMAID_VERSION}
        payload["default_stage"] = ctx.stage
        self._json(HTTPStatus.OK, payload)


def grogu_plans_actor() -> str:
    return grogu_plans.actor()


def _content_type(name: str) -> str:
    if name.endswith(".html"):
        return "text/html; charset=utf-8"
    if name.endswith(".js"):
        return "text/javascript; charset=utf-8"
    if name.endswith(".css"):
        return "text/css; charset=utf-8"
    if name.endswith(".svg"):
        return "image/svg+xml"
    return "application/octet-stream"


# -- the reviewable state, shared with the CLI --------------------------------


def effective_role() -> str:
    """I3: the session's role when set, otherwise reviewer."""
    return grogu_plans.current_role() or grogu_plans.REVIEWER


def build_plan_state(
    store: grogu_plans.PlanStore,
    review: grogu_review.ReviewStore,
    plan_id: str,
    *,
    role: str,
) -> dict:
    """The whole reviewable state for `GET /api/plan`, minus assets.

    Re-anchors first (every read of review state runs `sync`), then assembles
    only stages the role may read. A sealed stage body never appears here.
    """
    review.sync(plan_id, role=role)
    manifest = store.load(plan_id)
    readable = [
        stage for stage in grogu_plans.STAGES if stage in grogu_plans.ROLE_READABLE_STAGES[role]
    ]
    declined = manifest.get("declined_stages", {}) or {}
    written = manifest.get("stage_written", {}) or {}
    stages = []
    for stage in grogu_plans.STAGES:
        if stage not in readable:
            stages.append(
                {"stage": stage, "state": "sealed", "written": False, "readable": False}
            )
            continue
        if written.get(stage):
            entry = review.stage_view(plan_id, stage, role=role)
            entry.setdefault("readable", True)
            stages.append(entry)
        elif stage in declined:
            stages.append(
                {
                    "stage": stage,
                    "state": "declined",
                    "written": False,
                    "readable": True,
                    "why": declined[stage].get("why", "") if isinstance(declined[stage], dict) else "",
                }
            )
        else:
            stages.append(
                {"stage": stage, "state": "pending", "written": False, "readable": True}
            )
    round_state = review.load(plan_id).get("rounds") or []
    current_round = round_state[-1] if round_state else None
    gate = store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)
    return {
        "plan": plan_id,
        "title": manifest.get("title", ""),
        "status": manifest.get("status"),
        "review_required": manifest.get("review_required", False),
        "approved_at": manifest.get("approved_at", ""),
        "role": role,
        "readable_stages": readable,
        "stages": stages,
        "threads": review.threads(plan_id),
        "round": current_round,
        "summary": review.summary(plan_id),
        "gate": {
            "implement": {"allowed": gate["allowed"], "blockers": gate["blockers"]},
        },
    }


# -- lifecycle ----------------------------------------------------------------


def serve(
    plan_id: str,
    *,
    role: str = "",
    stage: str = "",
    host: str = "127.0.0.1",
    port: int = 0,
    timeout: float = DEFAULT_TIMEOUT,
    store: Optional[grogu_plans.PlanStore] = None,
    on_ready: Optional[Callable[[dict], None]] = None,
    block: bool = True,
) -> dict:
    """Bind a loopback server for one plan and run it until it is stopped.

    Returns ``{"url", "host", "port", "plan", "role", "assets"}``. ``on_ready``
    is called with that dict once the port is known, so the caller can print and
    open a browser; ``block`` runs the server until ``POST /api/shutdown``, the
    idle timeout, or ``KeyboardInterrupt``.
    """
    if host not in ("127.0.0.1", "::1"):
        raise ReviewServerError(
            f"refusing to bind {host!r}; the review server is loopback-only "
            "(127.0.0.1 or ::1)"
        )
    store = store or grogu_plans.PlanStore()
    plan_id = store.resolve(plan_id)
    # The agent-identity trap: give the server its own identity so reading as
    # the effective role does not collide with a role binding the user's shell
    # already carries. setdefault, so an agent that launched us keeps theirs.
    # GROGU_ROLE is never touched.
    os.environ.setdefault("GROGU_AGENT", f"grogu-review-{plan_id}")
    role = role or effective_role()
    review = grogu_review.ReviewStore(store)
    context = _Context(
        plan_id=plan_id,
        role=role,
        store=store,
        review=review,
        stage=stage,
        timeout=timeout,
    )
    context.host = host
    address_family_host = host
    httpd = _ReviewServer((address_family_host, port), _Handler, context=context)
    context.httpd = httpd
    bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
    context.port = bound_port
    display_host = "127.0.0.1" if host == "127.0.0.1" else "[::1]"
    url = f"http://{display_host}:{bound_port}"
    info = {
        "url": url,
        "host": host,
        "port": bound_port,
        "plan": plan_id,
        "role": role,
        "assets": context.assets,
        "token": context.token,
    }
    if on_ready is not None:
        on_ready(info)
    watchdog = threading.Thread(target=_watchdog, args=(context,), daemon=True)
    watchdog.start()
    if not block:
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True
        )
        thread.start()
        return info
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return info


def _watchdog(context: _Context) -> None:
    """Shut the server down after `timeout` seconds with no request."""
    if not context.timeout or context.timeout <= 0:
        return
    while True:
        time.sleep(min(5.0, context.timeout))
        if context.idle_seconds() >= context.timeout:
            if context.httpd is not None:
                context.httpd.shutdown()
            return
