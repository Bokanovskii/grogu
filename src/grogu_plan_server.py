"""Secured loopback HTTP surface for `.plan` document packages.

The server deliberately reuses the review workspace's proven envelope: exact
Host and Origin allowlists, same-site session cookies, a double-submit token,
strict CSP, bounded JSON bodies, silent request logging, and a single-use
launch token.  It serves only the committed React build; Node is never needed
at runtime.
"""

from __future__ import annotations

import copy
import hmac
import json
import os
import secrets
import stat
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote, urlsplit

import grogu_markdown
import grogu_plandoc
import grogu_plandoc_canon
import grogu_plandoc_compile
import grogu_plandoc_patch
import grogu_plans
import grogu_review

WORKSPACE_DIR = Path(__file__).resolve().parent / "plan_workspace" / "dist"
MAX_BODY_BYTES = 256 * 1024
MAX_PATCH_OPS = 500
DEFAULT_TIMEOUT = 3600
MODES = ("control", "document", "canvas", "dependencies", "revision")

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
}


class PlanServerError(Exception):
    """The local plan server cannot be started safely."""


class _EventHub:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._sequence = 0
        self._events: deque[tuple[int, str, dict]] = deque(maxlen=512)

    def publish(self, kind: str, payload: Optional[dict] = None) -> None:
        with self._condition:
            self._sequence += 1
            self._events.append((self._sequence, kind, payload or {}))
            self._condition.notify_all()

    def wait(self, after: int, timeout: float) -> tuple[int, list[tuple[str, dict]]]:
        with self._condition:
            if self._sequence <= after:
                self._condition.wait(timeout)
            values = [
                (kind, payload)
                for sequence, kind, payload in self._events
                if sequence > after
            ]
            return self._sequence, values


class _PlanHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, context: "_Context") -> None:
        super().__init__(address, handler)
        self.context = context


class _Context:
    def __init__(
        self,
        *,
        plan_id: str,
        role: str,
        store: grogu_plans.PlanStore,
        documents: grogu_plans.PlanDocumentStore,
        stage: str,
        mode: str,
        timeout: float,
    ) -> None:
        self.plan_id = plan_id
        self.role = role
        self.store = store
        self.documents = documents
        self.review = grogu_review.ReviewStore(store)
        self.stage = stage
        self.mode = mode
        self.timeout = timeout
        self.token = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
        self._token_spent = False
        self._last_activity = time.monotonic()
        self._lock = threading.Lock()
        self.host = "127.0.0.1"
        self.port = 0
        self.httpd: Optional[_PlanHTTPServer] = None
        self.events = _EventHub()
        self.control = documents.control_room(role=role)
        self._last_head = documents.head(repair=True)
        self._shutdown = False

    def consume_token(self, supplied: str) -> bool:
        with self._lock:
            if self._token_spent or not self.token:
                return False
            if not supplied or not hmac.compare_digest(supplied, self.token):
                return False
            self._token_spent = True
            # Defect d10: burning the value itself makes a later accidental
            # comparison fail closed even if the spent flag is regressed.
            self.token = ""
            return True

    def touch(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_activity

    def host_allowlist(self) -> frozenset[str]:
        return frozenset(
            {
                f"127.0.0.1:{self.port}",
                f"localhost:{self.port}",
                f"[::1]:{self.port}",
            }
        )

    def origin_allowlist(self) -> frozenset[str]:
        return frozenset(
            {
                f"http://127.0.0.1:{self.port}",
                f"http://localhost:{self.port}",
                f"http://[::1]:{self.port}",
            }
        )

    def poll_head(self) -> None:
        try:
            head = self.documents.head(repair=True)
        except grogu_plans.PlanError:
            return
        with self._lock:
            if head != self._last_head:
                self._last_head = head
                self.events.publish("revision", {"revision": head})

    def mark_revision(self, revision: str) -> None:
        with self._lock:
            self._last_head = revision
        self.events.publish("revision", {"revision": revision})


class _Handler(BaseHTTPRequestHandler):
    server_version = "grogu-plan"
    protocol_version = "HTTP/1.1"

    @property
    def context(self) -> _Context:
        return self.server.context  # type: ignore[attr-defined]

    def log_message(self, *args, **kwargs) -> None:
        # URLs contain launch tokens and bodies contain the user's plan.
        return

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra=(),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, *, extra=()) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf8"
            ),
            "application/json; charset=utf-8",
            extra=extra,
        )

    def _error(
        self,
        status: int,
        error: str,
        message: str,
        *,
        extra: Optional[dict] = None,
        close: bool = False,
    ) -> None:
        if close:
            self.close_connection = True
        payload = {"error": error, "message": message}
        if extra:
            payload.update(extra)
        self._json(status, payload)

    def _cookie_present(self) -> bool:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "grogu_session" and hmac.compare_digest(
                value, self.context.cookie
            ):
                return True
        return False

    def _envelope_ok(self, path: str, *, mutation: bool = False) -> bool:
        host = self.headers.get("Host", "")
        if host not in self.context.host_allowlist():
            self._error(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "host not allowed",
                close=True,
            )
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.context.origin_allowlist():
            self._error(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "origin not allowed",
                close=True,
            )
            return False
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site not in {"same-origin", "none"}:
            self._error(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "cross-site request refused",
                close=True,
            )
            return False
        if path == "/api/health" and not mutation:
            return True
        if path.startswith("/api/") and not self._cookie_present():
            self._error(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "no plan session",
                close=mutation,
            )
            return False
        if mutation:
            if origin not in self.context.origin_allowlist():
                self._error(
                    HTTPStatus.FORBIDDEN,
                    "forbidden",
                    "a matching Origin is required",
                    close=True,
                )
                return False
            supplied = self.headers.get("X-Grogu-Token", "")
            # The launch value is burned; the readable CSRF cookie/header uses
            # the value captured by the handler context at exchange time.
            expected = getattr(self.context, "csrf_token", "")
            if not expected or not hmac.compare_digest(supplied, expected):
                self._error(
                    HTTPStatus.FORBIDDEN,
                    "forbidden",
                    "missing or invalid X-Grogu-Token",
                    close=True,
                )
                return False
        return True

    def _read_body(self) -> Optional[dict]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length or "0")
        except ValueError:
            self._error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "invalid Content-Length",
                close=True,
            )
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "body_too_large",
                f"request body exceeds {MAX_BODY_BYTES} bytes",
                close=True,
            )
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf8"))
        except (UnicodeDecodeError, ValueError):
            self._error(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "request body is not valid JSON",
            )
            return None
        if not isinstance(value, dict):
            self._error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "request body must be a JSON object",
            )
            return None
        return value

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        self.context.touch()
        parsed = urlsplit(self.path)
        path = parsed.path
        if not self._envelope_ok(path):
            return
        if path == "/" and "t=" in (parsed.query or ""):
            self._token_exchange(parse_qs(parsed.query))
            return
        if path != "/api/health" and not self._cookie_present():
            self._error(
                HTTPStatus.FORBIDDEN,
                "forbidden",
                "no plan session; launch with `grogu plan doc open`",
            )
            return
        try:
            if path == "/":
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/") :])
            elif path in {"/app.js", "/app.css", "/LICENSES.txt"}:
                self._serve_static(path[1:])
            elif path == "/api/health":
                self._api_health()
            elif path == "/api/doc":
                self._api_doc(parsed.query)
            elif path == "/api/projection":
                self._api_projection(parsed.query)
            elif path == "/api/revisions":
                self._json(
                    HTTPStatus.OK,
                    {
                        "revisions": list(
                            reversed(
                                self.context.documents.revisions(
                                    role=self.context.role
                                )
                            )
                        )
                    },
                )
            elif path == "/api/diff":
                self._api_diff(parsed.query)
            elif path == "/api/proposals":
                self._json(
                    HTTPStatus.OK,
                    {
                        "proposals": self.context.documents.proposals(
                            role=self.context.role
                        )
                    },
                )
            elif path.startswith("/api/proposals/") and path.endswith("/preview"):
                proposal_id = unquote(path.split("/")[3])
                self._json(
                    HTTPStatus.OK,
                    self.context.documents.proposal_preview(
                        proposal_id, role=self.context.role
                    ),
                )
            elif path == "/api/control":
                self._api_control(parsed.query)
            elif path.startswith("/api/control/"):
                agent_key = unquote(path[len("/api/control/") :])
                self._json(
                    HTTPStatus.OK,
                    self.context.documents.control_drill(
                        agent_key,
                        role=self.context.role,
                        room=self.context.control,
                    ),
                )
            elif path == "/api/feedback":
                self._json(
                    HTTPStatus.OK,
                    {
                        "feedback": self.context.documents.feedback_records(
                            role=self.context.role
                        )
                    },
                )
            elif path == "/api/activity":
                # Object evidence is emitted only when the normalized
                # control-room reader authorizes it. The current reader has no
                # such source, so the honest response is empty.
                self._json(HTTPStatus.OK, {"events": []})
            elif path == "/api/events":
                self._serve_events()
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "not found")
        except Exception as error:  # routed through one non-leaking mapper
            self._route_exception(error)

    def do_POST(self) -> None:
        self.context.touch()
        parsed = urlsplit(self.path)
        path = parsed.path
        if not self._envelope_ok(path, mutation=True):
            return
        body = self._read_body()
        if body is None:
            return
        try:
            self._route_post(path, body)
        except Exception as error:
            self._route_exception(error)

    def _token_exchange(self, query: dict) -> None:
        supplied = (query.get("t") or [""])[0]
        if not self.context.consume_token(supplied):
            try:
                page = self._static_bytes("index.html")
            except PlanServerError:
                page = b"This session link has already been used."
            self._send(
                HTTPStatus.FORBIDDEN,
                page,
                "text/html; charset=utf-8",
                extra=(
                    (
                        "Set-Cookie",
                        "grogu_notice=token_spent; SameSite=Strict; Path=/",
                    ),
                ),
            )
            return
        self.context.csrf_token = supplied
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"grogu_session={self.context.cookie}; HttpOnly; SameSite=Strict; Path=/",
        )
        self.send_header(
            "Set-Cookie",
            f"grogu_csrf={supplied}; SameSite=Strict; Path=/",
        )
        self.send_header(
            "Set-Cookie", "grogu_notice=; SameSite=Strict; Path=/; Max-Age=0"
        )
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def _static_bytes(self, name: str) -> bytes:
        if (
            not name
            or "/" in name
            or "\\" in name
            or name in {".", ".."}
        ):
            raise PlanServerError("invalid static path")
        root = WORKSPACE_DIR.resolve()
        path = root / name
        try:
            info = path.lstat()
        except OSError as error:
            raise PlanServerError("static asset not found") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise PlanServerError("static asset not found")
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise PlanServerError("static asset not found") from error
        if resolved.parent != root:
            raise PlanServerError("static asset not found")
        return resolved.read_bytes()

    def _serve_static(self, name: str) -> None:
        try:
            data = self._static_bytes(name)
        except PlanServerError:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "not found")
            return
        content_type = CONTENT_TYPES.get(
            Path(name).suffix.lower(), "application/octet-stream"
        )
        self._send(HTTPStatus.OK, data, content_type)

    def _api_health(self) -> None:
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "plan": self.context.plan_id,
                "revision": self.context.documents.head(repair=True),
                "role": self.context.role,
                "modes": list(MODES),
            },
        )

    def _api_doc(self, query: str) -> None:
        params = parse_qs(query)
        stage = (params.get("stage") or [""])[0]
        stages = [stage] if stage else None
        document = self.context.documents.load(
            role=self.context.role,
            stages=stages,
            record=False,
        )
        manifest = self.context.store.load(self.context.plan_id)
        readable = [
            item
            for item in manifest.get("stages", [])
            if item
            in grogu_plans.ROLE_READABLE_STAGES.get(self.context.role, ())
        ]
        sealed = [
            item for item in manifest.get("stages", []) if item not in readable
        ]
        nodes = []
        for node in sorted(
            document["nodes"].values(), key=grogu_plandoc.node_sort_key
        ):
            value = copy.deepcopy(node)
            value["body_html"] = grogu_markdown.render_document(
                value.get("body", "")
            )["html"]
            if value.get("kind") == "thread":
                value.setdefault("ext", {})[
                    "dev.grogu.thread"
                ] = self.context.documents._thread_dto(value)
            nodes.append(value)
        self._json(
            HTTPStatus.OK,
            {
                "plan": self.context.plan_id,
                "title": document["title"],
                "revision": document["revision"],
                "role": self.context.role,
                "stages": {
                    "readable": readable,
                    "sealed": sealed,
                    "written": {
                        item: grogu_plans.STAGE_OWNERS[item]
                        for item in manifest.get("stages", [])
                        if manifest.get("stage_written", {}).get(item)
                    },
                },
                "nodes": nodes,
                "edges": sorted(
                    document["edges"].values(),
                    key=grogu_plandoc.edge_sort_key,
                ),
                # Counters are a convenience for local allocation, not an
                # oracle for ids in unreadable partitions.
                "counters": self.context.documents._counter_state(document),
                "sealed_edge_count": int(
                    document.get("provenance", {}).get(
                        "sealed_relationships", 0
                    )
                ),
            },
        )

    def _api_projection(self, query: str) -> None:
        params = parse_qs(query)
        requested_role = (params.get("role") or [self.context.role])[0]
        if requested_role != self.context.role:
            raise grogu_plans.PlanError(
                "the server session cannot project as a different role"
            )
        stage = (params.get("stage") or [""])[0]
        include = (params.get("include") or ["normative"])[0]
        format_name = (params.get("format") or ["md"])[0]
        budget_raw = (params.get("budget") or [""])[0]
        budget = int(budget_raw) if budget_raw else None
        since = (params.get("since") or [""])[0]
        result = self.context.documents.projection(
            role=self.context.role,
            stages=[stage] if stage else None,
            include=include,
            budget=budget,
            since=since,
            format=format_name,
        )
        etag = result["etag"]
        etag_header = f'"{etag}"'
        if self.headers.get("If-None-Match") in {etag, etag_header}:
            self._send(
                HTTPStatus.NOT_MODIFIED,
                b"",
                "application/json; charset=utf-8",
                extra=(("ETag", etag_header),),
            )
            return
        if format_name == "json":
            payload = result["payload"]
        else:
            payload = {
                "markdown": result["payload"],
                "digest": etag,
                "role": self.context.role,
                "stage": stage,
                "include": include,
                "budget": budget,
                "elided": result.get("elided", []),
                "nonequivalent": None,
            }
        self._json(HTTPStatus.OK, payload, extra=(("ETag", etag_header),))

    def _projection_at(self, revision: str, stage: str) -> str:
        document = self.context.documents.load(
            role=self.context.role,
            revision=revision,
            stages=[stage],
            record=False,
        )
        spec = self.context.documents._projection_spec(
            self.context.role, [stage]
        )
        return grogu_plandoc_compile.compile_checked(document, spec)[
            "markdown"
        ]

    def _api_diff(self, query: str) -> None:
        params = parse_qs(query)
        before = (params.get("from") or [""])[0]
        after = (params.get("to") or [""])[0]
        stage = self.context.stage or grogu_plans.IMPLEMENTATION
        self._json(
            HTTPStatus.OK,
            {
                "from": before,
                "to": after,
                "before": self._projection_at(before, stage),
                "after": self._projection_at(after, stage),
            },
        )

    def _api_control(self, query: str) -> None:
        params = parse_qs(query)
        window_raw = (params.get("window") or ["120"])[0]
        try:
            window = max(1, min(int(window_raw), 7 * 24 * 60))
        except ValueError:
            raise grogu_plans.PlanError("window must be an integer")
        self._json(
            HTTPStatus.OK,
            self.context.documents.control_snapshot(
                role=self.context.role,
                room=self.context.control,
                plan=(params.get("plan") or [""])[0],
                filter_role=(params.get("role") or [""])[0],
                workstream=(params.get("workstream") or [""])[0],
                state=(params.get("state") or [""])[0],
                window_minutes=window,
            ),
        )

    def _route_post(self, path: str, body: dict) -> None:
        documents = self.context.documents
        role = self.context.role
        if path == "/api/patch":
            operations = body.get("ops", [])
            if not isinstance(operations, list):
                raise grogu_plans.PlanError("ops must be an array")
            if len(operations) > MAX_PATCH_OPS:
                raise grogu_plans.PlanError("patch op count exceeds 500")
            result = documents.patch(
                role=role,
                base=str(body.get("base", "")),
                operations=operations,
                intent=str(body.get("intent", "")),
                origin=str(body.get("origin", "workspace")),
            )
            self.context.mark_revision(result["revision"])
            self._json(
                HTTPStatus.OK,
                {
                    "revision": result["revision"],
                    "digest": result["digest"],
                    "changed": result["changed"],
                },
            )
            return
        if path == "/api/threads":
            selector = body.get("selector")
            if not isinstance(selector, dict):
                raise grogu_plans.PlanError("selector is required")
            thread = documents.add_thread(
                role=role,
                selector=selector,
                body=str(body.get("body", "")),
                kind=str(body.get("kind", "discussion")),
            )
            self.context.events.publish("thread", {"id": thread["id"]})
            self._json(HTTPStatus.OK, {"thread": thread})
            return
        if path.startswith("/api/threads/"):
            self._route_thread(path, body)
            return
        if path == "/api/proposals":
            operations = body.get("ops", [])
            if body.get("restore_from"):
                current = documents.load(role=role, record=False)
                restored = documents.load(
                    role=role,
                    revision=str(body["restore_from"]),
                    record=False,
                )
                operations = [
                    operation
                    for operation in grogu_plandoc_patch.diff(current, restored)
                    if str(operation.get("path", "")).startswith(
                        ("/nodes/", "/edges/", "/canvas")
                    )
                ]
            proposal = documents.propose(
                role=role,
                operations=operations,
                why=str(body.get("why", "")),
                base=str(body.get("base", "")),
                from_thread=str(body.get("from_thread", "")),
            )
            self.context.events.publish("proposal", {"id": proposal["id"]})
            self._json(HTTPStatus.OK, {"proposal": proposal})
            return
        if path.startswith("/api/proposals/"):
            self._route_proposal(path, body)
            return
        if path == "/api/impact":
            selector = body.get("selector")
            if not isinstance(selector, dict):
                raise grogu_plans.PlanError("selector is required")
            depth = body.get("depth")
            self._json(
                HTTPStatus.OK,
                documents.impact(
                    role=role,
                    selector=selector,
                    depth=int(depth) if depth is not None else None,
                ),
            )
            return
        if path == "/api/layout":
            self._json(HTTPStatus.OK, self._layout(body))
            return
        if path.startswith("/api/control/") and path.endswith("/feedback"):
            agent_key = unquote(
                path[len("/api/control/") : -len("/feedback")].rstrip("/")
            )
            receipt = documents.route_feedback(
                role=role,
                scope={
                    "kind": "agent",
                    "agent_key": agent_key,
                    "label": f"agent {agent_key}",
                },
                text=str(body.get("text", "")),
                binding=bool(body.get("binding", False)),
                room=self.context.control,
            )
            self.context.events.publish("control", {})
            self._json(HTTPStatus.OK, receipt)
            return
        if path == "/api/feedback":
            scope = body.get("scope")
            if not isinstance(scope, dict):
                raise grogu_plans.PlanError("feedback scope is required")
            receipt = documents.route_feedback(
                role=role,
                scope=scope,
                text=str(body.get("text", "")),
                binding=bool(body.get("binding", False)),
                room=self.context.control,
            )
            self.context.events.publish("control", {})
            self._json(HTTPStatus.OK, receipt)
            return
        if path.startswith("/api/feedback/") and path.endswith("/withdraw"):
            feedback_id = unquote(
                path[len("/api/feedback/") : -len("/withdraw")].rstrip("/")
            )
            self._json(
                HTTPStatus.OK,
                documents.withdraw_feedback(feedback_id, role=role),
            )
            self.context.events.publish("control", {})
            return
        if path == "/api/request-changes":
            result = self.context.review.request_changes(
                self.context.plan_id,
                note=str(body.get("note", "")),
                role=role,
            )
            self._json(HTTPStatus.OK, {"ok": True, "round": result})
            return
        if path == "/api/approve":
            if (
                self.context.role != grogu_plans.REVIEWER
                or grogu_plans.current_role()
            ):
                self._error(
                    HTTPStatus.FORBIDDEN,
                    "role_set",
                    "cannot approve while a role is set",
                )
                return
            result = self.context.review.approve(
                self.context.plan_id,
                confirm_open=bool(body.get("confirm_open", False)),
                note=str(body.get("note", "")),
            )
            self._json(HTTPStatus.OK, {"ok": True, "plan": result["id"]})
            return
        if path == "/api/shutdown":
            self._json(HTTPStatus.OK, {"ok": True})
            self.context._shutdown = True
            self.context.events.publish("shutdown", {})
            threading.Thread(
                target=self.context.httpd.shutdown, daemon=True
            ).start()
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "not found")

    def _route_thread(self, path: str, body: dict) -> None:
        parts = path[len("/api/threads/") :].split("/")
        if len(parts) != 2:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "not found")
            return
        thread_id, action = unquote(parts[0]), parts[1]
        documents = self.context.documents
        if action == "reply":
            thread = documents.reply_thread(
                thread_id, str(body.get("body", "")), role=self.context.role
            )
        elif action == "resolve":
            thread = documents.set_thread_status(
                thread_id,
                "resolved",
                role=self.context.role,
                note=str(body.get("note", "")),
            )
        elif action == "reopen":
            thread = documents.set_thread_status(
                thread_id, "open", role=self.context.role
            )
        elif action == "revise":
            instruction = str(body.get("instruction", "")).strip()
            if not instruction:
                raise grogu_plans.PlanError(
                    "a revision request needs an instruction"
                )
            if not any(
                thread.get("id") == thread_id
                for thread in documents.threads(role=self.context.role)
            ):
                self._error(HTTPStatus.NOT_FOUND, "not_found", "not found")
                return
            request = documents.route_feedback(
                role=self.context.role,
                scope={
                    "kind": "role_plan",
                    "role": grogu_plans.ARCHITECT,
                    "plan": documents.plan_id,
                    "label": "architects on this plan",
                },
                text=f"Revision request for {thread_id}: {instruction}",
                binding=True,
            )
            current = documents.load(
                role=self.context.role,
                record=False,
            )
            try:
                documents.patch(
                    role=self.context.role,
                    base=current["revision"],
                    operations=[
                        {
                            "op": "add",
                            "path": (
                                f"/nodes/{thread_id}/attrs/revision_request"
                            ),
                            "value": {
                                "id": request["seq"],
                                "state": request["record"]["state"],
                                "instruction": instruction,
                                "at": request["record"]["at"],
                            },
                        }
                    ],
                    intent=f"record revision request {request['seq']}",
                    origin="revision-request",
                )
            except Exception:
                documents.withdraw_feedback(
                    request["seq"], role=self.context.role
                )
                raise
            self.context.events.publish(
                "feedback", {"id": request["seq"], "thread": thread_id}
            )
            self._json(HTTPStatus.OK, {"request": request})
            return
        else:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "not found")
            return
        self.context.events.publish("thread", {"id": thread["id"]})
        self._json(HTTPStatus.OK, {"thread": thread})

    def _route_proposal(self, path: str, body: dict) -> None:
        parts = path[len("/api/proposals/") :].split("/")
        if len(parts) != 2:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "not found")
            return
        proposal_id, action = unquote(parts[0]), parts[1]
        if action == "accept":
            result = self.context.documents.accept_proposal(
                proposal_id, role=self.context.role
            )
            self.context.mark_revision(result["revision"])
            self._json(HTTPStatus.OK, {"revision": result["revision"]})
        elif action == "reject":
            proposal = self.context.documents.reject_proposal(
                proposal_id,
                role=self.context.role,
                why_not=str(body.get("why_not", "")),
            )
            self.context.events.publish("proposal", {"id": proposal_id})
            self._json(HTTPStatus.OK, {"proposal": proposal})
        else:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "not found")

    def _layout(self, body: dict) -> dict:
        if body.get("algorithm") != "dagre":
            raise grogu_plans.PlanError("layout algorithm must be dagre")
        scope = str(body.get("scope", "canvas"))
        if scope not in {"canvas", "stage"}:
            raise grogu_plans.PlanError("layout scope must be canvas or stage")
        stage = str(body.get("stage", ""))
        if scope == "stage" and stage not in grogu_plans.STAGES:
            raise grogu_plans.PlanError("stage layout requires a known stage")
        direction = str(body.get("direction", "TB"))
        if direction not in {"TB", "LR"}:
            raise grogu_plans.PlanError("layout direction must be TB or LR")
        document = self.context.documents.load(
            role=self.context.role, record=False
        )
        nodes = [
            node
            for node in sorted(
                document["nodes"].values(), key=grogu_plandoc.node_sort_key
            )
            if node.get("kind") != "thread"
            and node.get("geometry")
            and (scope != "stage" or node.get("stage") == stage)
        ]
        # A deterministic standard-library fallback. The contract freezes the
        # returned patch, not a Node runtime; the React build's explicit
        # auto-layout action applies these integer positions as one revision.
        columns = max(1, int(len(nodes) ** 0.5))
        operations = []
        for index, node in enumerate(nodes):
            row, column = divmod(index, columns)
            x, y = (
                (column * 320, row * 220)
                if direction == "TB"
                else (row * 320, column * 220)
            )
            geometry = {**node["geometry"], "x": x, "y": y}
            operations.append(
                {
                    "op": "replace",
                    "path": f"/nodes/{node['id']}/geometry",
                    "value": geometry,
                }
            )
        return {"ops": operations}

    def _serve_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self._security_headers()
        self.end_headers()
        sequence = 0
        try:
            self.wfile.write(b"event: control\ndata: {}\n\n")
            self.wfile.flush()
            while not self.context._shutdown:
                self.context.poll_head()
                sequence, events = self.context.events.wait(sequence, 1.0)
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for kind, payload in events:
                    line = (
                        f"event: {kind}\ndata: "
                        + json.dumps(payload, separators=(",", ":"))
                        + "\n\n"
                    )
                    self.wfile.write(line.encode("utf8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _route_exception(self, error: Exception) -> None:
        if isinstance(error, grogu_plandoc_patch.StaleRevision):
            self._error(
                HTTPStatus.CONFLICT,
                "stale",
                str(error),
                extra={
                    "revision": error.current_revision,
                    "ops_since": getattr(error, "ops_since", []),
                },
            )
        elif isinstance(error, grogu_plandoc_patch.ForbiddenPatch):
            self._error(
                HTTPStatus.FORBIDDEN,
                "forbidden_stage",
                str(error),
            )
        elif isinstance(error, grogu_plandoc_patch.PatchError):
            self._error(
                error.status,
                error.code,
                str(error),
                extra={"why": str(error), "path": error.path},
            )
        elif isinstance(error, grogu_plandoc_compile.CompileError):
            self._error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "nonequivalent",
                str(error),
                extra={"field": error.field},
            )
        elif isinstance(error, grogu_review.ReviewError):
            self._error(HTTPStatus.CONFLICT, "review_conflict", str(error))
        elif isinstance(error, grogu_plans.PlanError):
            message = str(error)
            status = (
                HTTPStatus.FORBIDDEN
                if "may not" in message or "role" in message and "cannot" in message
                else HTTPStatus.UNPROCESSABLE_ENTITY
            )
            self._error(status, "plan_error", message)
        elif isinstance(error, (ValueError, TypeError)):
            self._error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "invalid_request",
                str(error),
            )
        else:
            # Never include repr(error), request data, paths from event logs, or
            # any other accidental private payload.
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "server_error",
                "the local plan server could not complete the request",
            )


def serve(
    plan_id: str,
    *,
    role: str = "",
    stage: str = "",
    mode: str = "document",
    host: str = "127.0.0.1",
    port: int = 0,
    timeout: float = DEFAULT_TIMEOUT,
    store: Optional[grogu_plans.PlanStore] = None,
    on_ready: Optional[Callable[[dict], None]] = None,
    block: bool = True,
) -> dict:
    """Serve one `.plan` package from a loopback-only ephemeral session."""
    if host not in {"127.0.0.1", "::1"}:
        raise PlanServerError(
            f"refusing to bind {host!r}; the plan server is loopback-only"
        )
    if mode not in MODES:
        raise PlanServerError(f"unknown workspace mode {mode!r}")
    store = store or grogu_plans.PlanStore()
    plan_id = store.resolve(plan_id)
    if not store.is_document_plan(plan_id):
        raise PlanServerError(
            f"plan {plan_id} uses the legacy layout; migrate it before opening "
            "the plan workspace"
        )
    os.environ.setdefault("GROGU_AGENT", f"grogu-plan-{plan_id}")
    effective_role = role or grogu_plans.current_role() or grogu_plans.REVIEWER
    documents = grogu_plans.PlanDocumentStore.for_plan(store, plan_id)
    # Bind and authorize before a socket is exposed.
    documents.load(role=effective_role, stages=[stage] if stage else None)
    context = _Context(
        plan_id=plan_id,
        role=effective_role,
        store=store,
        documents=documents,
        stage=stage,
        mode=mode,
        timeout=timeout,
    )
    context.host = host
    httpd = _PlanHTTPServer((host, port), _Handler, context=context)
    context.httpd = httpd
    context.port = int(httpd.server_address[1])
    display_host = "127.0.0.1" if host == "127.0.0.1" else "[::1]"
    info = {
        "url": f"http://{display_host}:{context.port}",
        "host": host,
        "port": context.port,
        "plan": plan_id,
        "role": effective_role,
        "mode": mode,
        "token": context.token,
    }
    if on_ready:
        on_ready(info)
    threading.Thread(target=_watchdog, args=(context,), daemon=True).start()
    if not block:
        def run_server() -> None:
            try:
                httpd.serve_forever(poll_interval=0.25)
            finally:
                httpd.server_close()

        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()
        info["server"] = httpd
        info["thread"] = thread
        return info
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        context._shutdown = True
        httpd.shutdown()
        httpd.server_close()
    return info


def _watchdog(context: _Context) -> None:
    if not context.timeout or context.timeout <= 0:
        return
    while not context._shutdown:
        time.sleep(min(5.0, context.timeout))
        if context.idle_seconds() >= context.timeout:
            context._shutdown = True
            context.events.publish("shutdown", {})
            if context.httpd is not None:
                context.httpd.shutdown()
            return
