"""Bounded, non-production access diagnostics. Never emit raw response data."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

NOTICE_LIST_URL = "https://www.ungm.org/Public/Notice"
ACCESS_REPORT = Path("debug/ungm-access.json")


def safe_url(url: str) -> str:
    """Only a recognized UNGM origin/path is public; omit all query/fragment data."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "www.ungm.org":
            return "[redacted-origin]"
        paths = {"/Public/Notice", "/Account/Login", "/Account/LogIn", "/Account/LogOn", "/GenericError.htm"}
        path = parsed.path if parsed.path in paths else "/[redacted-path]"
        return "https://www.ungm.org" + path
    except (TypeError, ValueError):
        return "[invalid-url]"


def safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Both header names AND values are allowlisted, not merely secret-redacted."""
    headers = {key.lower(): value.lower().strip() for key, value in headers.items()}
    result = {}
    content_type = headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type in {"text/html", "text/plain", "application/json", "application/xml"}:
        result["content-type"] = content_type
    server = headers.get("server", "")
    for product in ("cloudflare", "microsoft-iis", "nginx", "apache", "akamaighost", "awselb", "envoy"):
        if re.fullmatch(re.escape(product) + r"(?:/[0-9.]+)?", server):
            result["server"] = product
            break
    if headers.get("cf-mitigated") == "challenge":
        result["cf-mitigated"] = "challenge"
    retry = headers.get("retry-after", "")
    if re.fullmatch(r"[0-9]{1,6}", retry):
        result["retry-after"] = retry
    return result


def body_summary(body: bytes) -> dict[str, Any]:
    """Output fixed labels only: no excerpts, page titles, identifiers, or tokens."""
    sample = body[:65536].decode("utf-8", errors="replace").lower()
    patterns = {
        "forbidden": r"\bforbidden\b",
        "access_denied": r"access\s+denied|request\s+(?:was\s+)?blocked",
        "cloudflare": r"cloudflare|cf-chl-|challenge-platform",
        "javascript_required": r"enable\s+javascript|javascript\s+(?:is\s+)?required",
        "captcha": r"captcha|verify\s+(?:that\s+)?you\s+are\s+(?:a\s+)?human",
        "rate_limit": r"too\s+many\s+requests|rate\s+limit",
        "authentication_required": r"authentication\s+required|please\s+(?:log|sign)\s+in",
        "azure_waf": r"azure\s+front\s+door|web\s+application\s+firewall",
        "akamai": r"akamai",
    }
    return {
        "bytes": len(body),
        "sample_truncated": len(body) > 65536,
        "markers": [name for name, pattern in patterns.items() if re.search(pattern, sample)],
    }


async def save_access_diagnostics(response: Any, path: Path = ACCESS_REPORT, *, diagnostic: bool = False) -> dict[str, Any]:
    """Capture the existing response only. Capture failures never hide HTTP errors."""
    report: dict[str, Any] = {"schema_version": 1, "url": safe_url(response.url), "status": int(response.status)}
    report["transport"] = "playwright-route-fetch" if diagnostic else "chromium-navigation"
    try:
        report["headers"] = safe_headers(response.headers)
    except Exception:
        report["headers_unavailable"] = True
    try:
        report["body"] = body_summary(await asyncio.wait_for(response.body(), timeout=5))
    except Exception:
        report["body_unavailable"] = True
    # All dynamic strings above are strict allowlists. Never log exceptions or raw bodies.
    logging.info("UNGM access diagnostic: %s", json.dumps(report, sort_keys=True))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        logging.warning("Could not save safe access report; see diagnostic log")
    return report


async def diagnose_access() -> int:
    """One document request, no redirects/subresources/scripts, retries, AI or mail."""
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    locale="en-US", extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                    java_script_enabled=False, service_workers="block",
                )
                try:
                    page = await context.new_page()
                    requested = False
                    reports = []

                    async def one_request(route: Any) -> None:
                        nonlocal requested
                        request = route.request
                        if (not requested and request.is_navigation_request()
                                and request.frame == page.main_frame and request.url == NOTICE_LIST_URL):
                            requested = True
                            try:
                                # continue_() follows HTTP redirects without re-routing. Fetch
                                # explicitly disables redirects/retries, then abort the original
                                # browser request so no response is rendered or followed.
                                response = await route.fetch(max_redirects=0, max_retries=0, timeout=45000)
                                reports.append(await save_access_diagnostics(response, diagnostic=True))
                            except Exception:
                                logging.error("Diagnostic request or capture failed; no retry will be made")
                            finally:
                                await route.abort()
                        else:
                            await route.abort()

                    await context.route("**/*", one_request)
                    try:
                        await page.goto(NOTICE_LIST_URL, wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        # The original browser navigation is intentionally aborted after capture.
                        pass
                    if not reports:
                        logging.error("Diagnostic received no HTTP response; no retry will be made")
                        return 1
                    report = reports[0]
                    # A 2xx here verifies only entry access, NOT the full procurement workflow.
                    return 0 if 200 <= report["status"] < 300 else 1
                finally:
                    await context.close()
            finally:
                await browser.close()
    except Exception:
        logging.error("Access diagnostic failed; no retry, mail or state update was attempted")
        return 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(diagnose_access())


if __name__ == "__main__":
    raise SystemExit(main())
