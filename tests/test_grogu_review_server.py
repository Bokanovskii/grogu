import http.client
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_plans  # noqa: E402
import grogu_review  # noqa: E402
import grogu_review_server  # noqa: E402


class GroguReviewServerTests(unittest.TestCase):
    def _create_git_repo(self, path: Path) -> grogu_plans.PlanStore:
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Server Tester"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Server Tester"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "server@example.com"], cwd=path, check=True)
        return grogu_plans.PlanStore(path)

    def _start_server(self, store, plan_id, role="", **kwargs):
        kwargs.setdefault("timeout", 60)
        agent = f"srv-{secrets.token_hex(4)}"
        with mock.patch.dict(os.environ, {"GROGU_ROLE": role, "GROGU_AGENT": agent}):
            info = grogu_review_server.serve(plan_id, block=False, store=store, role=role, **kwargs)
        port = info["port"]

        def _cleanup():
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("GET", f"/?t={info['token']}", headers={"Host": f"127.0.0.1:{port}"})
                resp = conn.getresponse()
                cookie = resp.getheader("Set-Cookie", "").split(";")[0]
                resp.read()
                conn.request(
                    "POST",
                    "/api/shutdown",
                    body=b"{}",
                    headers={
                        "Host": f"127.0.0.1:{port}",
                        "Cookie": cookie,
                        "X-Grogu-Review": "1",
                        "Content-Type": "application/json",
                    },
                )
                r = conn.getresponse()
                r.read()
                conn.close()
            except Exception:
                pass

        self.addCleanup(_cleanup)
        return info

    # 41. Rejects hostile framing
    def test_41_rejects_hostile_framing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            store.write_stage(plan_id, "implementation", "# Implementation\nSecret text here", role="architect")
            info = self._start_server(store, plan_id)
            port = info["port"]
            token = info["token"]

            # 1. Host: evil.example.com -> 403
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/", headers={"Host": "evil.example.com"})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 403)
            self.assertNotIn(b"Secret text", body)

            # 2. Host: 127.0.0.1:1 (wrong port) -> 403
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/", headers={"Host": "127.0.0.1:1"})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 403)
            self.assertNotIn(b"Secret text", body)

            # 3. Origin: https://evil.example.com -> 403
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}", "Origin": "https://evil.example.com"})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 403)
            self.assertNotIn(b"Secret text", body)

            # 4. Sec-Fetch-Site: cross-site -> 403
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}", "Sec-Fetch-Site": "cross-site"})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 403)
            self.assertNotIn(b"Secret text", body)

            # 5. POST /api/threads without X-Grogu-Review header -> 403
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request(
                "POST",
                "/api/threads",
                body=b"{}",
                headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 403)
            self.assertNotIn(b"Secret text", body)

            # 6. Any /api/* request with no session cookie -> 403
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request(
                "GET",
                "/api/plan",
                headers={"Host": f"127.0.0.1:{port}", "X-Grogu-Review": "1"},
            )
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 403)
            self.assertNotIn(b"Secret text", body)

            # 7. Valid token exchange -> 303 + Cookie
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", f"/?t={token}", headers={"Host": f"127.0.0.1:{port}"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 303)
            cookie = resp.getheader("Set-Cookie", "").split(";")[0]
            resp.read()
            conn.close()

            # 8. Second GET /?t=<token> reusing already-spent token -> 403
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", f"/?t={token}", headers={"Host": f"127.0.0.1:{port}"})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 403)
            self.assertNotIn(b"Secret text", body)

            # 9. Valid request with Host: localhost:<port> and no Origin succeeds
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request(
                "GET",
                "/api/plan",
                headers={
                    "Host": f"localhost:{port}",
                    "Cookie": cookie,
                    "X-Grogu-Review": "1",
                },
            )
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 200)

    # 42. No CORS, and the right headers
    def test_42_no_cors_and_right_headers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            store.write_stage(plan_id, "implementation", "# Implementation\nBody", role="architect")
            info = self._start_server(store, plan_id)
            port = info["port"]
            token = info["token"]

            # Token exchange to get cookie
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", f"/?t={token}", headers={"Host": f"127.0.0.1:{port}"})
            resp = conn.getresponse()
            cookie = resp.getheader("Set-Cookie", "").split(";")[0]
            resp.read()
            conn.close()

            endpoints = [
                ("GET", "/", {"Host": f"127.0.0.1:{port}", "Cookie": cookie}),  # Index
                ("GET", "/static/app.css", {"Host": f"127.0.0.1:{port}", "Cookie": cookie}),  # Static file
                ("GET", "/api/plan", {"Host": f"127.0.0.1:{port}", "Cookie": cookie, "X-Grogu-Review": "1"}),  # JSON API
                ("GET", "/api/plan", {"Host": f"127.0.0.1:{port}"}),  # 403 Forbidden
            ]

            for method, path, headers in endpoints:
                conn = http.client.HTTPConnection("127.0.0.1", port)
                conn.request(method, path, headers=headers)
                resp = conn.getresponse()
                resp.read()
                conn.close()

                # No Access-Control-* headers
                for header_name, _ in resp.getheaders():
                    self.assertFalse(
                        header_name.lower().startswith("access-control-"),
                        f"Found CORS header {header_name} in {path}",
                    )

                # Required security headers
                csp = resp.getheader("Content-Security-Policy")
                self.assertIsNotNone(csp, f"Missing CSP on {path}")
                self.assertIn("default-src 'none'", csp)
                self.assertIn("connect-src 'self'", csp)
                self.assertIn("frame-ancestors 'none'", csp)
                self.assertNotIn("'unsafe-inline'", csp.split("script-src")[1].split(";")[0])
                self.assertNotIn("'unsafe-eval'", csp)

                self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")
                self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
                self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    # 43. Loopback only
    def test_43_loopback_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            store.write_stage(plan_id, "implementation", "# Implementation\nBody", role="architect")

            # Refuse 0.0.0.0
            with self.assertRaises(grogu_review_server.ReviewServerError):
                grogu_review_server.serve(plan_id, host="0.0.0.0", store=store)

            info = self._start_server(store, plan_id)
            self.assertEqual(info["host"], "127.0.0.1")

            # Try to resolve external IP of this machine
            try:
                hostname = socket.gethostname()
                external_ip = socket.gethostbyname(hostname)
            except Exception:
                external_ip = None

            if external_ip and not external_ip.startswith("127."):
                conn = http.client.HTTPConnection(external_ip, info["port"], timeout=1)
                try:
                    conn.request("GET", "/", headers={"Host": f"{external_ip}:{info['port']}"})
                    resp = conn.getresponse()
                    resp.read()
                    conn.close()
                    self.fail("Server unexpectedly reached on non-loopback interface")
                except (OSError, socket.timeout):
                    pass

    # 44. Path traversal
    def test_44_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            store.write_stage(plan_id, "implementation", "# Implementation\nBody", role="architect")
            info = self._start_server(store, plan_id)
            port = info["port"]
            token = info["token"]

            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", f"/?t={token}", headers={"Host": f"127.0.0.1:{port}"})
            resp = conn.getresponse()
            cookie = resp.getheader("Set-Cookie", "").split(";")[0]
            resp.read()
            conn.close()

            traversal_paths = [
                "/static/../../../../etc/passwd",
                "/static/..%2f..%2fgrogu_plans.py",
                "/static/",
                "/vendor/../../../../etc/passwd",
            ]
            for path in traversal_paths:
                conn = http.client.HTTPConnection("127.0.0.1", port)
                conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}", "Cookie": cookie})
                resp = conn.getresponse()
                body = resp.read()
                conn.close()
                self.assertIn(resp.status, (403, 404))
                self.assertNotIn(b"PlanStore", body)
                self.assertNotIn(b"root:x:0:0", body)

    # 45. Role isolation end to end - the seal
    def test_45_role_isolation_end_to_end_seal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan", design=True, evaluation=True)
            plan_id = plan["id"]
            sentinel = "DISTINCTIVE_SEALED_SENTINEL_STRING_XYZ_987654"
            store.write_stage(plan_id, "implementation", "# Implementation\nImpl body", role="architect")
            store.write_stage(plan_id, "testing", f"# Testing\n{sentinel}", role="architect")
            store.write_stage(plan_id, "evaluation", f"# Evaluation\n{sentinel}", role="architect")

            # 1. Engineer role
            info_eng = self._start_server(store, plan_id, role="engineer")
            port_eng = info_eng["port"]
            token_eng = info_eng["token"]  # grogu-allow-secret: ephemeral test token

            conn = http.client.HTTPConnection("127.0.0.1", port_eng)
            conn.request("GET", f"/?t={token_eng}", headers={"Host": f"127.0.0.1:{port_eng}"})
            resp = conn.getresponse()
            cookie_eng = resp.getheader("Set-Cookie", "").split(";")[0]
            resp.read()
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port_eng)
            conn.request("GET", "/api/plan", headers={
                "Host": f"127.0.0.1:{port_eng}",
                "Cookie": cookie_eng,
                "X-Grogu-Review": "1",
            })
            resp_eng = conn.getresponse()
            raw_eng = resp_eng.read()
            conn.close()
            data_eng = json.loads(raw_eng.decode("utf8"))

            self.assertIn("implementation", data_eng["readable_stages"])
            self.assertIn("design", data_eng["readable_stages"])
            self.assertNotIn("testing", data_eng["readable_stages"])
            self.assertNotIn("evaluation", data_eng["readable_stages"])
            self.assertNotIn(sentinel.encode("utf8"), raw_eng)

            # 2. Tester role
            info_test = self._start_server(store, plan_id, role="tester")
            port_test = info_test["port"]
            token_test = info_test["token"]  # grogu-allow-secret: ephemeral test token

            conn = http.client.HTTPConnection("127.0.0.1", port_test)
            conn.request("GET", f"/?t={token_test}", headers={"Host": f"127.0.0.1:{port_test}"})
            resp = conn.getresponse()
            cookie_test = resp.getheader("Set-Cookie", "").split(";")[0]
            resp.read()
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port_test)
            conn.request("GET", "/api/plan", headers={
                "Host": f"127.0.0.1:{port_test}",
                "Cookie": cookie_test,
                "X-Grogu-Review": "1",
            })
            resp_test = conn.getresponse()
            raw_test = resp_test.read()
            conn.close()
            data_test = json.loads(raw_test.decode("utf8"))

            self.assertNotIn("implementation", data_test["readable_stages"])
            self.assertIn("testing", data_test["readable_stages"])

            # 3. No role (reviewer/user)
            info_rev = self._start_server(store, plan_id, role="")
            port_rev = info_rev["port"]
            token_rev = info_rev["token"]  # grogu-allow-secret: ephemeral test token

            conn = http.client.HTTPConnection("127.0.0.1", port_rev)
            conn.request("GET", f"/?t={token_rev}", headers={"Host": f"127.0.0.1:{port_rev}"})
            resp = conn.getresponse()
            cookie_rev = resp.getheader("Set-Cookie", "").split(";")[0]
            resp.read()
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port_rev)
            conn.request("GET", "/api/plan", headers={
                "Host": f"127.0.0.1:{port_rev}",
                "Cookie": cookie_rev,
                "X-Grogu-Review": "1",
            })
            resp_rev = conn.getresponse()
            raw_rev = resp_rev.read()
            conn.close()
            data_rev = json.loads(raw_rev.decode("utf8"))

            self.assertEqual(len(data_rev["readable_stages"]), 4)

    # 46. Lifecycle and limits
    def test_46_lifecycle_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            store.write_stage(plan_id, "implementation", "# Implementation\nBody", role="architect")
            info = self._start_server(store, plan_id)
            port = info["port"]
            token = info["token"]

            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", f"/?t={token}", headers={"Host": f"127.0.0.1:{port}"})
            resp = conn.getresponse()
            cookie = resp.getheader("Set-Cookie", "").split(";")[0]
            resp.read()
            conn.close()

            # 1. Body over 64 KiB rejected with 400 and server stays up
            large_body = b'{"data": "' + (b"x" * (65 * 1024)) + b'"}'
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request(
                "POST",
                "/api/threads",
                body=large_body,
                headers={
                    "Host": f"127.0.0.1:{port}",
                    "Cookie": cookie,
                    "X-Grogu-Review": "1",
                    "Content-Type": "application/json",
                },
            )
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 400)

            # Server stays up for next request
            conn2 = http.client.HTTPConnection("127.0.0.1", port)
            conn2.request("GET", "/api/plan", headers={
                "Host": f"127.0.0.1:{port}",
                "Cookie": cookie,
                "X-Grogu-Review": "1",
            })
            resp2 = conn2.getresponse()
            resp2.read()
            conn2.close()
            self.assertEqual(resp2.status, 200)

            # 2. POST /api/shutdown stops it
            conn3 = http.client.HTTPConnection("127.0.0.1", port)
            conn3.request("POST", "/api/shutdown", body=b"{}", headers={
                "Host": f"127.0.0.1:{port}",
                "Cookie": cookie,
                "X-Grogu-Review": "1",
                "Content-Type": "application/json",
            })
            resp3 = conn3.getresponse()
            resp3.read()
            conn3.close()
            self.assertEqual(resp3.status, 200)

            # Give thread a moment to shut down
            time.sleep(0.5)
            with self.assertRaises((OSError, ConnectionRefusedError)):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                c.request("GET", "/api/plan")
                c.getresponse()

            # 3. Server with 1-second idle timeout exits on its own
            info_idle = grogu_review_server.serve(plan_id, block=False, store=store, timeout=1.0)
            port_idle = info_idle["port"]
            time.sleep(2.0)
            with self.assertRaises((OSError, ConnectionRefusedError)):
                c = http.client.HTTPConnection("127.0.0.1", port_idle, timeout=1)
                c.request("GET", "/api/plan")
                c.getresponse()


if __name__ == "__main__":
    unittest.main()
