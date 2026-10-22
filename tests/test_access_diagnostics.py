"""Offline/localhost-only access diagnostics regressions. Never contact UNGM."""

from __future__ import annotations

import ast
import json
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import chdir
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import ungm_access as access
import ungm_watch as watch


class SafeSummaryTests(unittest.TestCase):
    def test_url_drops_userinfo_query_fragment_and_unknown_paths(self):
        self.assertEqual(access.safe_url("https://secret:password@www.ungm.org/Public/Notice?token=secret#secret"), access.NOTICE_LIST_URL)
        self.assertEqual(access.safe_url("https://www.ungm.org/token/secret"), "https://www.ungm.org/[redacted-path]")
        self.assertEqual(access.safe_url("https://secret.example/"), "[redacted-origin]")

    def test_header_names_and_values_are_both_allowlisted(self):
        values = {"Server": "cloudflare", "Content-Type": "text/html; secret=secret",
                  "Set-Cookie": "secret", "Authorization": "secret", "CF-Ray": "secret",
                  "CF-Mitigated": "challenge", "Retry-After": "30", "Location": "https://secret/"}
        self.assertEqual(access.safe_headers(values), {"server": "cloudflare", "content-type": "text/html", "cf-mitigated": "challenge", "retry-after": "30"})
        self.assertEqual(access.safe_headers({"server": "cloudflare secret", "content-type": "secret", "retry-after": "secret", "cf-mitigated": "secret"}), {})

    def test_body_contains_labels_never_raw_text_or_secrets(self):
        body = b"<title>secret</title>Access denied. Cloudflare. Enable JavaScript. captcha secret-token=user@example.com"
        summary = access.body_summary(body)
        self.assertNotIn("secret", json.dumps(summary))
        self.assertNotIn("example.com", json.dumps(summary))
        self.assertEqual(summary["markers"], ["access_denied", "cloudflare", "javascript_required", "captcha"])
        self.assertTrue(access.body_summary(b"x" * 70000)["sample_truncated"])

    def test_diagnostic_imports_cannot_reach_watcher_mail_ai_or_dotenv(self):
        tree = ast.parse(Path(access.__file__).read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append((node.module or "").split(".")[0])
        self.assertFalse(set(imports) & {"ungm_watch", "ungm_notify", "smtplib", "dotenv"})

    def test_workflow_routes_only_marked_first_push_to_diagnostics(self):
        workflow = (Path(watch.__file__).parent / ".github/workflows/ungm-watch.yml").read_text()
        production, diagnostic = workflow.split("  diagnose-access:")
        self.assertIn("if: github.event_name != 'push' || !contains(github.event.head_commit.message, '[ungm-diagnostic]')", production)
        self.assertIn("if: github.event_name == 'push' && contains(github.event.head_commit.message, '[ungm-diagnostic]') && github.run_attempt == 1", diagnostic)
        self.assertNotIn("secrets.", diagnostic)
        self.assertNotIn("sent_ids", diagnostic)
        self.assertNotIn("ungm_watch.py", diagnostic)
        self.assertNotIn("ungm_notify.py", diagnostic)
        self.assertEqual(diagnostic.count("run: python ungm_access.py"), 1)
        self.assertEqual(workflow.count("path: debug/ungm-access.json"), 2)
        self.assertNotIn("path: debug/\n", workflow)
        self.assertIn('cron: "55 0 * * 0-6"', workflow)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_log_and_file_are_safe_even_with_hostile_headers_body_url(self):
        response = SimpleNamespace(url=access.NOTICE_LIST_URL + "?secret=secret", status=403,
                                   headers={"server": "cloudflare", "Set-Cookie": "secret"},
                                   body=AsyncMock(return_value=b"Forbidden secret-password"))
        with tempfile.TemporaryDirectory() as temp, self.assertLogs(level="INFO") as logs:
            path = Path(temp) / "report.json"
            report = await access.save_access_diagnostics(response, path)
            serialized = path.read_text() + str(logs.output)
        self.assertNotIn("secret", serialized)
        self.assertEqual(report["status"], 403)
        self.assertEqual(report["transport"], "chromium-navigation")
        response.body.assert_awaited_once()

    async def test_body_and_file_failures_do_not_mask_status(self):
        response = SimpleNamespace(url=access.NOTICE_LIST_URL, status=503, headers={},
                                   body=AsyncMock(side_effect=RuntimeError("secret")))
        with patch.object(Path, "mkdir", side_effect=OSError("secret")), self.assertLogs(level="INFO") as logs:
            report = await access.save_access_diagnostics(response)
        self.assertEqual(report["status"], 503)
        self.assertTrue(report["body_unavailable"])
        self.assertNotIn("secret", str(logs.output))

    async def test_production_captures_before_close_and_preserves_http_failure(self):
        for capture_fails in (False, True):
            with self.subTest(capture_fails=capture_fails):
                events = []
                response = SimpleNamespace(status=403)
                page = SimpleNamespace(goto=AsyncMock(return_value=response))
                context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock(side_effect=lambda: events.append("close")))
                browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
                manager = MagicMock()
                manager.__aenter__ = AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))))
                manager.__aexit__ = AsyncMock(return_value=False)
                async def capture(_):
                    events.append("capture")
                    if capture_fails:
                        raise RuntimeError("secret")
                with patch("playwright.async_api.async_playwright", return_value=manager), patch.object(watch, "save_access_diagnostics", side_effect=capture), patch.object(watch, "wait_for_search_form", new_callable=AsyncMock) as search:
                    with self.assertRaisesRegex(RuntimeError, "Notice list HTTP 403"):
                        await watch.scrape_notices(1, True, date(2026, 10, 22))
                    search.assert_not_awaited()
                self.assertEqual(events, ["capture", "close"])
                browser.close.assert_awaited_once()


class LocalhostRequestTests(unittest.IsolatedAsyncioTestCase):
    """Real Playwright routing against a local fixture, never an external host."""

    async def test_exactly_one_request_for_403_200_and_redirect(self):
        from playwright.async_api import BrowserType, async_playwright
        original_launch = BrowserType.launch
        # Prefer pinned Playwright browser in CI; local fallback only for this test.
        async with async_playwright() as pw:
            installed = Path(pw.chromium.executable_path).exists()
        executable = None if installed else shutil.which("chromium")
        if not installed and not executable:
            self.fail("Chromium required for localhost-only single-request regression")

        async def launch(browser_type, **kwargs):
            if executable:
                kwargs["executable_path"] = executable
            return await original_launch(browser_type, **kwargs)

        for status in (403, 200, 302):
            with self.subTest(status=status):
                seen = []
                class Handler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        seen.append(self.path)
                        self.send_response(status)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Location", "/must-not-follow?secret=secret")
                        self.end_headers()
                        self.wfile.write(b'<html>Forbidden secret-token<script src="/must-not-fetch"></script><img src="/must-not-fetch"><meta http-equiv="refresh" content="0;url=/must-not-follow"></html>')
                    def log_message(self, *args):
                        pass
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with tempfile.TemporaryDirectory() as temp, chdir(temp), patch.object(access, "NOTICE_LIST_URL", f"http://127.0.0.1:{server.server_port}/entry"), patch.object(BrowserType, "launch", launch), patch.dict(os.environ, {"SMTP_PASSWORD": "secret", "DEEPSEEK_API_KEY": "secret"}):
                        state = Path("sent_ids.json")
                        state.write_text('{"sent_ids":["unchanged"]}')
                        marker = Path(".ungm-alert-sent")
                        marker.write_text("unchanged")
                        before = {str(p): p.read_bytes() for p in (state, marker)}
                        result = await access.diagnose_access()
                        self.assertEqual(result, 0 if status == 200 else 1)
                        self.assertEqual(seen, ["/entry"])
                        self.assertEqual(before, {str(p): p.read_bytes() for p in (state, marker)})
                        self.assertEqual(sorted(str(p) for p in Path("debug").iterdir()), ["debug/ungm-access.json"])
                        report = json.loads(Path("debug/ungm-access.json").read_text())
                        self.assertEqual(report["transport"], "playwright-route-fetch")
                        self.assertEqual(report["status"], status)
                        self.assertNotIn("secret", json.dumps(report))
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()


if __name__ == "__main__":
    unittest.main()
