#!/usr/bin/env python3
"""Daily UNGM procurement opportunity watcher.

The script renders UNGM's public procurement page with Playwright, extracts
active notices, filters them for light-industry goods opportunities, and sends
an HTML email through SMTP. Sent notice IDs are stored locally in JSON so the
same opportunity is not sent twice.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import re
import smtplib
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from dateutil import parser as date_parser


BASE_URL = "https://www.ungm.org"
NOTICE_LIST_PATH = "/Public/Notice"
NOTICE_LIST_URL = f"{BASE_URL}{NOTICE_LIST_PATH}"
NOTICE_LIST_PARAMS: dict[str, str] = {
    "PageSize": "100",
    "SortField": "DeadlineDate",
    "SortAscending": "true",
}
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_SENT_FILE = "sent_ids.json"
DEFAULT_MAX_PAGES = 30
PAGE_TIMEOUT_MS = 45_000
PAGE_STABILITY_CHECKS = 3
PAGE_STABILITY_INTERVAL_MS = 700
PAGE_STABILITY_MAX_ATTEMPTS = 24
AUTO_SCROLL_MAX_STEPS = 60
AUTO_SCROLL_INTERVAL_MS = 500
AUTO_SCROLL_UNCHANGED_LIMIT = 4
DEBUG_DIR = Path("debug")

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
UNGM_DATE_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


BUSINESS_KEYWORDS: dict[str, list[str]] = {
    "stationery": ["stationery", "office stationery", "writing supplies", "paper supplies"],
    "office supplies": ["office supplies", "office equipment and accessories and supplies"],
    "school supplies": ["school supplies", "school kit", "student kit", "learning kit"],
    "toys": ["toys", "toy", "play materials", "recreational items"],
    "sports balls": ["sports balls", "football", "soccer ball", "basketball", "volleyball"],
    "sports equipment": ["sports equipment", "sporting goods", "sports goods", "playground equipment"],
    "school bags": ["school bags", "schoolbag", "school bag"],
    "backpacks": ["backpacks", "backpack", "rucksack"],
    "bags": ["bags", "luggage", "tote bags", "carry bags"],
    "plastic goods": ["plastic goods", "plastic items", "plastic products", "plastic containers"],
    "textile goods": ["textile goods", "textiles", "fabric", "blankets", "bedding"],
    "tents": ["tents", "tent", "family tents", "shelter tents", "tarpaulins", "tarpaulin"],
    "household items": [
        "household items",
        "household goods",
        "household supplies",
        "household kits",
        "kitchenware",
        "kitchen utensils",
        "cooking sets",
        "hygiene kits",
    ],
    "garments": ["garments", "clothing", "apparel", "uniforms", "t-shirts", "vests"],
    "gift items": ["gift items", "promotional items", "souvenirs", "giveaways"],
    "children products": ["children products", "child-friendly items", "children's products", "kids items"],
    "educational supplies": [
        "educational supplies",
        "teaching aids",
        "learning materials",
        "education materials",
        "classroom supplies",
    ],
}

SERVICE_EXCLUSION_KEYWORDS: dict[str, list[str]] = {
    "consulting": ["consulting", "consultancy", "consultant", "advisory services"],
    "training": ["training", "capacity building", "workshop facilitation"],
    "maintenance": ["maintenance", "repair services", "operation and maintenance"],
    "construction services": ["construction services", "civil works", "building works", "renovation works"],
    "research": ["research", "study services", "desk review"],
    "audit": ["audit", "auditing"],
    "survey": ["survey", "data collection"],
    "assessment": ["assessment", "evaluation services", "needs assessment"],
    "recruitment": ["recruitment", "staffing services", "human resources services"],
    "IT services": ["it services", "information technology services", "software development", "system integration"],
    "logistics / transport": ["logistics", "transport services", "transportation services", "freight forwarding"],
    "event services": ["event services", "event management", "conference services"],
}

SERVICE_OPPORTUNITY_TYPES = {
    "call for individual consultants",
    "call for implementing partners",
}

SERVICE_CATEGORY_PATTERNS = [
    re.compile(r"\bJ\s*-\s*Services\b", re.IGNORECASE),
    re.compile(r"\b80000000\s*-\s*Management and Business Professionals", re.IGNORECASE),
    re.compile(r"\b83000000\s*-\s*Public Utilities and Public Sector Related Services", re.IGNORECASE),
]

GOODS_CONFIRMATION_PATTERNS = [
    re.compile(r"\bG\s*-\s*Business, Communication & Technology Equipment & Supplies\b", re.IGNORECASE),
    re.compile(r"\bOffice Equipment and Accessories and Supplies\b", re.IGNORECASE),
    re.compile(r"\bSchool supplies\b", re.IGNORECASE),
    re.compile(r"\bSporting goods\b", re.IGNORECASE),
    re.compile(r"\bTextiles\b", re.IGNORECASE),
    re.compile(r"\bApparel\b", re.IGNORECASE),
    re.compile(r"\bLuggage\b", re.IGNORECASE),
]


@dataclass
class Notice:
    notice_id: str
    title: str
    organization: str
    country: str
    published_raw: str
    deadline_raw: str
    opportunity_type: str
    reference: str
    url: str
    published_date: date | None = None
    deadline_date: date | None = None
    description: str = ""
    detail_text: str = ""
    match_reason: str = ""
    matched_keywords: list[str] = field(default_factory=list)


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logging.warning("Could not read %s: %s", path, exc)
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_space(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def term_matches(text: str, term: str) -> bool:
    escaped = re.escape(term.lower()).replace(r"\ ", r"\s+")
    return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text.lower()) is not None


def matched_terms(text: str, keyword_map: dict[str, list[str]]) -> list[str]:
    found: list[str] = []
    for label, terms in keyword_map.items():
        if any(term_matches(text, term) for term in terms):
            found.append(label)
    return found


def parse_ungm_date(raw: str) -> date | None:
    value = normalize_space(raw)
    if not value or value in {"-", "N/A"}:
        return None
    value = re.sub(r"\(GMT[^)]*\)", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"\bGMT\s*[+-]?\d+(?:\.\d+)?", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"\bExpires within\s+\d+\s+(?:hour|hours|day|days)\b.*$", "", value, flags=re.IGNORECASE).strip()
    try:
        return date_parser.parse(value, dayfirst=True, fuzzy=True).date()
    except (ValueError, OverflowError, TypeError) as exc:
        logging.warning("Could not parse date %r: %s", raw, exc)
        return None


def today_in_timezone(timezone_name: str) -> date:
    return datetime.now(ZoneInfo(timezone_name)).date()


def format_ungm_filter_date(value: date) -> str:
    return f"{value.day:02d}-{UNGM_DATE_MONTHS[value.month - 1]}-{value.year}"


def load_sent_ids(path: Path) -> set[str]:
    if not path.exists():
        logging.info("Sent IDs file does not exist yet: %s", path)
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Could not read %s, starting with empty sent IDs: %s", path, exc)
        return set()

    if isinstance(payload, list):
        return {str(item) for item in payload}
    if isinstance(payload, dict):
        return {str(item) for item in payload.get("sent_ids", [])}
    logging.warning("Unexpected sent IDs format in %s, starting empty", path)
    return set()


def save_sent_ids(path: Path, sent_ids: set[str]) -> None:
    payload = {
        "sent_ids": sorted(sent_ids),
        "updated_at": datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).isoformat(timespec="seconds"),
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    logging.info("Saved %d sent IDs to %s", len(sent_ids), path)


def is_playwright_timeout(exc: Exception) -> bool:
    return exc.__class__.__name__ == "TimeoutError" and "playwright" in exc.__class__.__module__


async def page_loading_stats(page: Any) -> dict[str, Any]:
    return await page.evaluate(
        """
        () => {
          const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const noticeLinks = Array.from(document.querySelectorAll('a[href*="/Public/Notice/"]'));
          const rowCount = Array.from(document.querySelectorAll('tbody tr, [role="row"]'))
            .filter((row) => !row.querySelector('th') && clean(row.innerText)).length;
          const ids = noticeLinks
            .map((link) => {
              const value = `${link.href || ''} ${link.innerText || ''}`;
              const match = value.match(/\\/Public\\/Notice\\/(\\d+)/i) || value.match(/\\bNotice\\s*ID\\s*:?\\s*(\\d+)/i);
              return match ? match[1] : '';
            })
            .filter(Boolean);
          const uniqueIds = Array.from(new Set(ids));
          const nextCandidates = Array.from(document.querySelectorAll('a, button'))
            .map((el) => ({
              text: clean(el.innerText || el.textContent),
              aria: clean(el.getAttribute('aria-label')),
              title: clean(el.getAttribute('title')),
              rel: clean(el.getAttribute('rel')),
              disabled: Boolean(
                el.disabled ||
                el.getAttribute('aria-disabled') === 'true' ||
                /disabled/i.test(el.className || '') ||
                (el.closest('li') && /disabled/i.test(el.closest('li').className || ''))
              ),
            }))
            .filter((item) => {
              const text = item.text.toLowerCase();
              const aria = item.aria.toLowerCase();
              const title = item.title.toLowerCase();
              const rel = item.rel.toLowerCase();
              return text === 'next' || text === '>' || text === '>>' || text === '»' ||
                aria.includes('next') || title.includes('next') || rel === 'next';
            })
            .slice(0, 8);
          const bodyText = (document.body && document.body.innerText) || '';
          let expectedTotal = null;
          const totalPatterns = [
            /([0-9][0-9\\s,\\u00a0\\u202f.]*)\\s*(?:procurement\\s+opportunit(?:y|ies)|notices?|results?)\\s+(?:were\\s+)?found/i,
            /(?:found|showing)\\s+([0-9][0-9\\s,\\u00a0\\u202f.]*)\\s*(?:procurement\\s+opportunit(?:y|ies)|notices?|results?)/i,
            /([0-9][0-9\\s,\\u00a0\\u202f.]*)\\s*(?:procurement\\s+opportunit(?:y|ies)|notices?|results?)/i,
          ];
          for (const pattern of totalPatterns) {
            const match = bodyText.match(pattern);
            if (match && match[1]) {
              const digits = match[1].replace(/[^0-9]/g, '');
              if (digits) {
                const value = parseInt(digits, 10);
                if (Number.isFinite(value) && value > 0) {
                  expectedTotal = value;
                  break;
                }
              }
            }
          }
          return {
            row_count: rowCount,
            notice_link_count: noticeLinks.length,
            unique_notice_count: uniqueIds.length,
            first_notice_id: uniqueIds[0] || '',
            last_notice_id: uniqueIds[uniqueIds.length - 1] || '',
            no_results: bodyText.includes('No procurement opportunity'),
            body_text_length: bodyText.length,
            scroll_height: Math.max(
              document.body ? document.body.scrollHeight : 0,
              document.documentElement ? document.documentElement.scrollHeight : 0,
            ),
            next_candidates: nextCandidates,
            expected_total: expectedTotal,
          };
        }
        """
    )


def page_signature(stats: dict[str, Any]) -> str:
    return "|".join(
        [
            str(stats.get("unique_notice_count", 0)),
            str(stats.get("first_notice_id", "")),
            str(stats.get("last_notice_id", "")),
        ]
    )


async def auto_scroll_page(page: Any) -> dict[str, Any]:
    result = await page.evaluate(
        """
        async ({maxSteps, intervalMs, unchangedLimit}) => {
          const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const docHeight = () => Math.max(
            document.body ? document.body.scrollHeight : 0,
            document.documentElement ? document.documentElement.scrollHeight : 0,
          );
          const noticeLinks = () => Array.from(document.querySelectorAll('a[href*="/Public/Notice/"]'));
          const noticeCount = () => {
            const ids = noticeLinks()
              .map((link) => {
                const value = `${link.href || ''} ${link.innerText || ''}`;
                const match = value.match(/\\/Public\\/Notice\\/(\\d+)/i) || value.match(/\\bNotice\\s*ID\\s*:?\\s*(\\d+)/i);
                return match ? match[1] : '';
              })
              .filter(Boolean);
            return new Set(ids).size;
          };
          const scrollableAncestor = (node) => {
            let current = node && node.parentElement;
            while (current) {
              const style = window.getComputedStyle(current);
              const overflowY = style.overflowY;
              if ((overflowY === 'auto' || overflowY === 'scroll') && current.scrollHeight > current.clientHeight + 4) {
                return current;
              }
              current = current.parentElement;
            }
            return null;
          };

          let lastHeight = -1;
          let lastCount = -1;
          let unchanged = 0;
          let steps = 0;
          while (steps < maxSteps && unchanged < unchangedLimit) {
            // 1) scroll the document
            window.scrollTo(0, docHeight());
            // 2) scroll the closest scrollable ancestor of the notice list, if any
            const links = noticeLinks();
            if (links.length > 0) {
              const last = links[links.length - 1];
              const ancestor = scrollableAncestor(last);
              if (ancestor) {
                ancestor.scrollTop = ancestor.scrollHeight;
              }
              try {
                last.scrollIntoView({block: 'end', inline: 'nearest', behavior: 'instant'});
              } catch (_err) {
                last.scrollIntoView(false);
              }
            }
            await wait(intervalMs);
            const height = docHeight();
            const count = noticeCount();
            if (height === lastHeight && count === lastCount) {
              unchanged += 1;
            } else {
              unchanged = 0;
              lastHeight = height;
              lastCount = count;
            }
            steps += 1;
          }
          return {steps, height: lastHeight, notice_count: lastCount};
        }
        """,
        {
            "maxSteps": AUTO_SCROLL_MAX_STEPS,
            "intervalMs": AUTO_SCROLL_INTERVAL_MS,
            "unchangedLimit": AUTO_SCROLL_UNCHANGED_LIMIT,
        },
    )
    logging.info(
        "Auto-scroll completed after %s steps; page height=%s notices=%s",
        result.get("steps"),
        result.get("height"),
        result.get("notice_count"),
    )
    return result


async def wait_for_rows_stable(page: Any) -> dict[str, Any]:
    stable_count = 0
    previous_signature = ""
    latest_stats: dict[str, Any] = {}
    expected_total: int | None = None
    for attempt in range(1, PAGE_STABILITY_MAX_ATTEMPTS + 1):
        latest_stats = await page_loading_stats(page)
        signature = page_signature(latest_stats)
        candidate_total = latest_stats.get("expected_total")
        if isinstance(candidate_total, int) and candidate_total > 0:
            expected_total = candidate_total
        unique_count = int(latest_stats.get("unique_notice_count") or 0)
        logging.info(
            "Load stability check %d/%d: rows=%s notice_links=%s unique_notices=%s/%s first=%s last=%s",
            attempt,
            PAGE_STABILITY_MAX_ATTEMPTS,
            latest_stats.get("row_count"),
            latest_stats.get("notice_link_count"),
            unique_count,
            expected_total if expected_total is not None else "?",
            latest_stats.get("first_notice_id") or "N/A",
            latest_stats.get("last_notice_id") or "N/A",
        )
        if signature == previous_signature:
            stable_count += 1
        else:
            stable_count = 1
            previous_signature = signature
        # If we know the expected total and we've reached or exceeded it, stop early.
        if expected_total is not None and unique_count >= expected_total:
            logging.info(
                "Reached expected total of %s notices on this page; stopping stability wait",
                expected_total,
            )
            return latest_stats
        if stable_count >= PAGE_STABILITY_CHECKS:
            # Only treat the page as stable if (a) we don't know the expected
            # total, or (b) we've reached it. Otherwise, keep nudging the page
            # to try to load more rows.
            if expected_total is None or unique_count >= expected_total:
                return latest_stats
            logging.info(
                "Signature stable but only %s/%s notices loaded; nudging the page to keep scrolling",
                unique_count,
                expected_total,
            )
            stable_count = 0
            previous_signature = ""
            await auto_scroll_page(page)
            continue
        await page.wait_for_timeout(PAGE_STABILITY_INTERVAL_MS)
    logging.warning(
        "Notice rows did not fully stabilize before timeout; continuing with %s/%s loaded",
        latest_stats.get("unique_notice_count"),
        expected_total if expected_total is not None else "?",
    )
    return latest_stats


async def wait_for_results(page: Any) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
    except Exception as exc:
        if not is_playwright_timeout(exc):
            raise
        logging.info("Network did not become fully idle; continuing with visible page content")
    try:
        await page.wait_for_function(
            """
            () => (
              document.querySelector('table, [role="table"]') ||
              document.body.innerText.includes('No procurement opportunity')
            )
            """,
            timeout=PAGE_TIMEOUT_MS,
        )
    except Exception as exc:
        if not is_playwright_timeout(exc):
            raise
        logging.warning("Timed out waiting for the notice table; attempting extraction anyway")
    await auto_scroll_page(page)
    await wait_for_rows_stable(page)


async def wait_for_search_form(page: Any) -> None:
    try:
        await page.wait_for_function(
            """
            () => (
              document.body.innerText.includes('Deadline between') &&
              Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"]'))
                .some((el) => /search/i.test(el.innerText || el.value || el.textContent || ''))
            )
            """,
            timeout=PAGE_TIMEOUT_MS,
        )
    except Exception as exc:
        if not is_playwright_timeout(exc):
            raise
        logging.warning("Timed out waiting for UNGM search form; continuing without browser-side deadline filter")


async def apply_deadline_search_filter(page: Any, today: date) -> None:
    minimum_deadline = today + timedelta(days=10)
    deadline_from = format_ungm_filter_date(minimum_deadline)
    before_stats = await page_loading_stats(page)
    before_signature = page_signature(before_stats)
    logging.info("Applying UNGM search filter: Deadline between %s and no upper bound", deadline_from)

    result = await page.evaluate(
        """
        ({deadlineFrom}) => {
          const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
          };
          const setNativeValue = (el, value) => {
            const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            setter ? setter.call(el, value) : (el.value = value);
            for (const type of ['input', 'change', 'blur']) {
              el.dispatchEvent(new Event(type, {bubbles: true}));
            }
          };
          const textElements = Array.from(document.querySelectorAll('label, span, div, td, th, p'))
            .filter(isVisible)
            .map((el) => ({el, text: clean(el.innerText || el.textContent), rect: el.getBoundingClientRect()}));
          const label = textElements
            .filter((item) => item.text === 'Deadline between' || item.text.includes('Deadline between'))
            .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height))[0];
          if (!label) {
            return {ok: false, reason: 'Deadline between label not found'};
          }

          const labelCenterY = label.rect.top + label.rect.height / 2;
          const inputs = Array.from(document.querySelectorAll('input'))
            .filter((el) => {
              const type = (el.getAttribute('type') || 'text').toLowerCase();
              return isVisible(el) && ['text', 'search', 'date', ''].includes(type);
            })
            .map((el) => ({el, rect: el.getBoundingClientRect(), value: el.value || ''}));
          const sameRowInputs = inputs
            .filter((item) => Math.abs((item.rect.top + item.rect.height / 2) - labelCenterY) <= 32 && item.rect.left > label.rect.left)
            .sort((a, b) => a.rect.left - b.rect.left);
          if (sameRowInputs.length < 2) {
            return {
              ok: false,
              reason: 'Could not find two deadline inputs on the Deadline between row',
              same_row_input_count: sameRowInputs.length,
            };
          }

          setNativeValue(sameRowInputs[0].el, deadlineFrom);
          setNativeValue(sameRowInputs[1].el, '');

          const activeText = textElements.find((item) => item.text.includes('Only currently active'));
          if (activeText) {
            const activeCenterY = activeText.rect.top + activeText.rect.height / 2;
            const checkbox = Array.from(document.querySelectorAll('input[type="checkbox"]'))
              .filter(isVisible)
              .map((el) => ({el, rect: el.getBoundingClientRect()}))
              .filter((item) => Math.abs((item.rect.top + item.rect.height / 2) - activeCenterY) <= 32)
              .sort((a, b) => a.rect.left - b.rect.left)[0]?.el;
            if (checkbox && !checkbox.checked) {
              checkbox.click();
            }
          }

          const search = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"]'))
            .filter(isVisible)
            .find((el) => /^search$/i.test(clean(el.innerText || el.value || el.textContent)));
          if (!search) {
            return {ok: false, reason: 'Search button not found after setting deadline filter'};
          }
          search.click();
          return {
            ok: true,
            deadline_from: sameRowInputs[0].el.value,
            deadline_to: sameRowInputs[1].el.value,
          };
        }
        """,
        {"deadlineFrom": deadline_from},
    )
    if not result.get("ok"):
        logging.warning("Could not apply UNGM browser-side deadline filter: %s", result)
        await save_page_debug_artifacts(page, "ungm-filter-not-applied")
        return

    logging.info("UNGM browser-side deadline filter applied: %s", result)
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception as exc:
        if not is_playwright_timeout(exc):
            raise
        logging.info("Network did not become fully idle after search; continuing with visible filtered content")
    try:
        await page.wait_for_function(
            """
            (previousSignature) => {
              const ids = Array.from(document.querySelectorAll('a[href*="/Public/Notice/"]'))
                .map((link) => {
                  const value = `${link.href || ''} ${link.innerText || ''}`;
                  const match = value.match(/\\/Public\\/Notice\\/(\\d+)/i) || value.match(/\\bNotice\\s*ID\\s*:?\\s*(\\d+)/i);
                  return match ? match[1] : '';
                })
                .filter(Boolean);
              const uniqueIds = Array.from(new Set(ids));
              const signature = [
                String(uniqueIds.length),
                uniqueIds[0] || '',
                uniqueIds[uniqueIds.length - 1] || '',
              ].join('|');
              return signature !== previousSignature || document.body.innerText.includes('No procurement opportunity');
            }
            """,
            arg=before_signature,
            timeout=15_000,
        )
    except Exception as exc:
        if not is_playwright_timeout(exc):
            raise
        logging.warning("Search results did not visibly change after applying deadline filter; continuing with current content")


async def save_page_debug_artifacts(page: Any, label: str) -> None:
    safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label).strip("-") or "page"
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    html_path = DEBUG_DIR / f"{safe_label}.html"
    screenshot_path = DEBUG_DIR / f"{safe_label}.png"
    try:
        html_path.write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(screenshot_path), full_page=True)
        logging.info("Saved page debug artifacts: %s and %s", html_path, screenshot_path)
    except Exception as exc:
        logging.warning("Could not save page debug artifacts for %s: %s", label, exc)


async def extract_notices_from_page(page: Any) -> list[Notice]:
    raw_rows = await page.evaluate(
        """
        () => {
          const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const absolute = (href) => href ? new URL(href, window.location.origin).href : '';
          const noticeIdFrom = (href, text) => {
            const value = `${href || ''} ${text || ''}`;
            const match = value.match(/\\/Public\\/Notice\\/(\\d+)/i) || value.match(/\\bNotice\\s*ID\\s*:?\\s*(\\d+)/i);
            return match ? match[1] : '';
          };
          const pick = (headers, cells, candidates, fallbackIndex) => {
            for (const candidate of candidates) {
              const idx = headers.findIndex((header) => header.toLowerCase().includes(candidate));
              if (idx >= 0 && idx < cells.length) return cells[idx];
            }
            return fallbackIndex < cells.length ? cells[fallbackIndex] : '';
          };
          const rows = [];
          for (const table of Array.from(document.querySelectorAll('table, [role="table"]'))) {
            const headerCells = Array.from(table.querySelectorAll('thead th, [role="columnheader"]')).map((cell) => clean(cell.innerText));
            const bodyRows = Array.from(table.querySelectorAll('tbody tr, [role="row"]'));
            for (const tr of bodyRows) {
              if (tr.querySelector('th')) continue;
              const cells = Array.from(tr.querySelectorAll('td, [role="cell"]')).map((cell) => clean(cell.innerText));
              if (cells.length < 5) continue;
              const rowText = clean(tr.innerText);
              if (!rowText || /No procurement opportunity was found/i.test(rowText)) continue;
              const link = tr.querySelector('a[href*="/Public/Notice/"]');
              const href = link ? absolute(link.getAttribute('href')) : '';
              const noticeId = noticeIdFrom(href, rowText);
              if (!noticeId) continue;
              rows.push({
                notice_id: noticeId,
                title: clean(link ? link.innerText : pick(headerCells, cells, ['title'], 0)),
                deadline_raw: pick(headerCells, cells, ['deadline'], 1),
                published_raw: pick(headerCells, cells, ['published'], 2),
                organization: pick(headerCells, cells, ['organization'], 3),
                opportunity_type: pick(headerCells, cells, ['type of opportunity', 'type'], 4),
                reference: pick(headerCells, cells, ['reference'], 5),
                country: pick(headerCells, cells, ['beneficiary country', 'country', 'territory'], 6),
                url: href,
              });
            }
          }
          if (rows.length === 0) {
            for (const link of Array.from(document.querySelectorAll('a[href*="/Public/Notice/"]'))) {
              const container = link.closest('tr, article, li, .row, .card, div') || link.parentElement;
              const rowText = clean(container ? container.innerText : link.innerText);
              const href = absolute(link.getAttribute('href'));
              const noticeId = noticeIdFrom(href, rowText);
              if (!noticeId || /Procurement opportunities/i.test(rowText)) continue;
              rows.push({
                notice_id: noticeId,
                title: clean(link.innerText),
                deadline_raw: '',
                published_raw: '',
                organization: '',
                opportunity_type: '',
                reference: '',
                country: '',
                url: href,
              });
            }
          }
          const unique = new Map();
          for (const row of rows) {
            if (!unique.has(row.notice_id)) unique.set(row.notice_id, row);
          }
          return Array.from(unique.values());
        }
        """
    )
    notices: list[Notice] = []
    for row in raw_rows:
        notices.append(
            Notice(
                notice_id=str(row.get("notice_id", "")),
                title=normalize_space(row.get("title", "")),
                organization=normalize_space(row.get("organization", "")),
                country=normalize_space(row.get("country", "")),
                published_raw=normalize_space(row.get("published_raw", "")),
                deadline_raw=normalize_space(row.get("deadline_raw", "")),
                opportunity_type=normalize_space(row.get("opportunity_type", "")),
                reference=normalize_space(row.get("reference", "")),
                url=normalize_space(row.get("url", "")),
            )
        )
    return notices


def build_notice_list_url(extra_params: dict[str, str] | None = None) -> str:
    params = {**NOTICE_LIST_PARAMS}
    if extra_params:
        for key, value in extra_params.items():
            params[key] = str(value)
    if not params:
        return NOTICE_LIST_URL
    return f"{NOTICE_LIST_URL}?{urlencode(params)}"


def url_with_page_index(current_url: str, page_index: int) -> str:
    parsed = urlparse(current_url or NOTICE_LIST_URL)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    out: list[tuple[str, str]] = []
    seen_page_index = False
    for key, value in query_pairs:
        if key.lower() == "pageindex":
            out.append((key, str(page_index)))
            seen_page_index = True
        else:
            out.append((key, value))
    if not seen_page_index:
        out.append(("PageIndex", str(page_index)))
    # Make sure our preferred PageSize / SortField are present.
    keys_lower = {key.lower() for key, _ in out}
    for key, value in NOTICE_LIST_PARAMS.items():
        if key.lower() not in keys_lower:
            out.append((key, value))
    new_query = urlencode(out)
    if not parsed.netloc:
        return f"{NOTICE_LIST_URL}?{new_query}"
    return urlunparse(parsed._replace(query=new_query))


def parse_page_index(current_url: str) -> int:
    parsed = urlparse(current_url or "")
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() == "pageindex":
            try:
                return max(1, int(value))
            except ValueError:
                continue
    return 1


async def navigate_to_next_page_via_url(page: Any) -> bool:
    current_url = page.url or NOTICE_LIST_URL
    current_index = parse_page_index(current_url)
    next_index = current_index + 1
    next_url = url_with_page_index(current_url, next_index)
    logging.info("Falling back to URL-based pagination: %s", next_url)
    try:
        await page.goto(next_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    except Exception as exc:
        if not is_playwright_timeout(exc):
            raise
        logging.warning("Timed out navigating to %s; continuing with current page", next_url)
        return False
    await wait_for_results(page)
    return True


async def click_next_page(page: Any) -> bool:
    before_stats = await page_loading_stats(page)
    before_signature = "|".join(
        [
            str(before_stats.get("unique_notice_count", 0)),
            str(before_stats.get("first_notice_id", "")),
            str(before_stats.get("last_notice_id", "")),
        ]
    )
    logging.info("Next-page candidates before click: %s", before_stats.get("next_candidates", []))

    click_result = await page.evaluate(
        """
        () => {
          const isDisabled = (el) => (
            el.disabled ||
            el.getAttribute('aria-disabled') === 'true' ||
            /disabled/i.test(el.className || '') ||
            (el.closest('li') && /disabled/i.test(el.closest('li').className || ''))
          );
          const candidates = Array.from(document.querySelectorAll('a, button'));
          const diagnostics = [];
          for (const el of candidates) {
            const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const aria = (el.getAttribute('aria-label') || '').toLowerCase();
            const title = (el.getAttribute('title') || '').toLowerCase();
            const rel = (el.getAttribute('rel') || '').toLowerCase();
            const looksNext = text === 'next' || text === '>' || text === '>>' || text === '»' || aria.includes('next') || title.includes('next') || rel === 'next';
            if (looksNext) diagnostics.push({text, aria, title, rel, disabled: isDisabled(el)});
            if (looksNext && !isDisabled(el)) {
              el.click();
              return {clicked: true, diagnostics};
            }
          }
          return {clicked: false, diagnostics};
        }
        """
    )
    if not click_result.get("clicked"):
        logging.info(
            "No enabled next page control found. Diagnostics: %s",
            click_result.get("diagnostics", []),
        )
        # Fall back to URL-based pagination so a DOM change in the next-page
        # button does not stop the watcher early.
        navigated = await navigate_to_next_page_via_url(page)
        if not navigated:
            await save_page_debug_artifacts(page, "ungm-no-next-page")
            return False
        after_stats = await page_loading_stats(page)
        after_signature = page_signature(after_stats)
        if after_signature == before_signature:
            logging.info("URL-based next page returned the same notices; treating as end of list")
            return False
        return True
    try:
        await page.wait_for_function(
            """
            (previousSignature) => {
              const ids = Array.from(document.querySelectorAll('a[href*="/Public/Notice/"]'))
                .map((link) => {
                  const value = `${link.href || ''} ${link.innerText || ''}`;
                  const match = value.match(/\\/Public\\/Notice\\/(\\d+)/i) || value.match(/\\bNotice\\s*ID\\s*:?\\s*(\\d+)/i);
                  return match ? match[1] : '';
                })
                .filter(Boolean);
              const uniqueIds = Array.from(new Set(ids));
              const signature = [
                String(uniqueIds.length),
                uniqueIds[0] || '',
                uniqueIds[uniqueIds.length - 1] || '',
              ].join('|');
              return signature !== previousSignature;
            }
            """,
            arg=before_signature,
            timeout=15_000,
        )
    except Exception as exc:
        if not is_playwright_timeout(exc):
            raise
        logging.warning("Next page click did not change notice signature within timeout; continuing with current content")
    await wait_for_results(page)
    return True


def parse_detail_html(html_text: str) -> tuple[str, str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    full_text = normalize_space(soup.get_text(" ", strip=True))

    description = ""
    title = ""
    title_node = soup.select_one("h1, h2, [data-testid='notice-title']")
    if title_node:
        title = normalize_space(title_node.get_text(" ", strip=True))
    if not title:
        title_tag = soup.find("title")
        if title_tag:
            title = normalize_space(title_tag.get_text(" ", strip=True))
            title = re.sub(r"\s*\|\s*UNGM.*$", "", title, flags=re.IGNORECASE)
    match = re.search(
        r"\bDescription\b\s*(.*?)(?:\bDocuments\b|\bContacts\b|\bLinks\b|\bCountries or territories\b|\bUNSPSC codes\b|$)",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        description = normalize_space(match.group(1))
    return description, full_text, title


async def enrich_notice_detail(browser: Any, notice: Notice) -> None:
    if not notice.url:
        logging.warning("Notice %s has no detail URL", notice.notice_id)
        return
    page = await browser.new_page()
    try:
        logging.info("Loading detail page for notice %s", notice.notice_id)
        await page.goto(notice.url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception as exc:
            if not is_playwright_timeout(exc):
                raise
        await auto_scroll_page(page)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception as exc:
            if not is_playwright_timeout(exc):
                raise
        html_text = await page.content()
        description, detail_text, detail_title = parse_detail_html(html_text)
        notice.description = description
        notice.detail_text = detail_text
        if not notice.title and detail_title:
            notice.title = detail_title

        if not notice.published_raw:
            notice.published_raw = extract_labeled_value(detail_text, "Published on")
        if not notice.deadline_raw:
            notice.deadline_raw = extract_labeled_value(detail_text, "Deadline on")
        if not notice.organization:
            notice.organization = extract_detail_organization(detail_text)
        if not notice.country:
            notice.country = extract_labeled_value(detail_text, "Beneficiary countries or territories")
        if not notice.opportunity_type:
            notice.opportunity_type = extract_detail_opportunity_type(detail_text)
        if not notice.reference:
            notice.reference = extract_labeled_value(detail_text, "Reference")
    except Exception as exc:
        logging.warning("Could not enrich notice %s from %s: %s", notice.notice_id, notice.url, exc)
    finally:
        await page.close()


def extract_labeled_value(text: str, label: str) -> str:
    pattern = rf"{re.escape(label)}\s*:?\s*(.*?)(?:\s+[A-Z][A-Za-z ]{{2,30}}\s*:|$)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return normalize_space(match.group(1)) if match else ""


def extract_detail_organization(text: str) -> str:
    parts = text.split(" ")
    if len(parts) < 3:
        return ""
    # Detail pages usually start with: title, organization, title, type.
    # Keep this conservative; list extraction remains the source of truth.
    known_org_match = re.search(r"\b(UNICEF|UNDP|UNOPS|WFP|WHO|UNHCR|UNFPA|UN Secretariat|UNESCO|FAO|IOM)\b", text)
    return known_org_match.group(1) if known_org_match else ""


def extract_detail_opportunity_type(text: str) -> str:
    known_types = [
        "Request for EOI",
        "Request for proposal",
        "Request for quotation",
        "Invitation to bid",
        "Request for pre-qualification",
        "Request for information",
        "Grant support-call for proposal",
        "Pre-bid notice",
        "Call for individual consultants",
        "Call for implementing partners",
    ]
    lowered = text.lower()
    for known_type in known_types:
        if known_type.lower() in lowered:
            return known_type
    return ""


def is_service_notice(notice: Notice) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    opportunity_type = notice.opportunity_type.lower()
    if opportunity_type in SERVICE_OPPORTUNITY_TYPES:
        reasons.append(f"service opportunity type: {notice.opportunity_type}")

    combined = " ".join(
        [
            notice.title,
            notice.opportunity_type,
            notice.reference,
            notice.description,
            notice.detail_text,
        ]
    )
    for pattern in SERVICE_CATEGORY_PATTERNS:
        if pattern.search(combined):
            reasons.append(f"service UNSPSC/category pattern: {pattern.pattern}")

    excluded = matched_terms(combined, SERVICE_EXCLUSION_KEYWORDS)
    reasons.extend(f"service keyword: {item}" for item in excluded)
    return bool(reasons), reasons


def goods_confirmation(notice: Notice) -> str:
    combined = " ".join([notice.title, notice.description, notice.detail_text])
    for pattern in GOODS_CONFIRMATION_PATTERNS:
        match = pattern.search(combined)
        if match:
            return normalize_space(match.group(0))
    if any(term_matches(combined, word) for word in ["goods", "supplies", "equipment", "products", "items"]):
        return "goods/supplies wording"
    return ""


def apply_filters(notice: Notice, today: date) -> tuple[bool, str]:
    notice.published_date = parse_ungm_date(notice.published_raw)
    notice.deadline_date = parse_ungm_date(notice.deadline_raw)

    if not notice.title:
        return False, "missing title"
    if not notice.deadline_date:
        return False, "missing or invalid deadline date"

    minimum_deadline = today + timedelta(days=10)
    if notice.deadline_date < minimum_deadline:
        return False, f"deadline {notice.deadline_date} is before {minimum_deadline}"

    service, service_reasons = is_service_notice(notice)
    if service:
        return False, "; ".join(service_reasons)

    keyword_text = " ".join(
        [
            notice.title,
            notice.reference,
            notice.opportunity_type,
            notice.description,
            notice.detail_text,
        ]
    )
    notice.matched_keywords = matched_terms(keyword_text, BUSINESS_KEYWORDS)
    if not notice.matched_keywords:
        return False, "no business keyword match"

    confirmation = goods_confirmation(notice)
    reason_parts = [
        f"命中业务关键词：{', '.join(notice.matched_keywords)}",
        f"截止日期 {notice.deadline_date.isoformat()} 距今天至少 10 天",
        "未命中服务类排除词或服务类采购类型",
    ]
    if confirmation:
        reason_parts.append(f"货物/用品依据：{confirmation}")
    notice.match_reason = "；".join(reason_parts)
    return True, notice.match_reason


async def scrape_notices(max_pages: int, headless: bool, today: date) -> list[Notice]:
    from playwright.async_api import async_playwright

    notices_by_id: dict[str, Notice] = {}
    pages_visited = 0
    expected_total: int | None = None
    initial_url = build_notice_list_url()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        try:
            page = await browser.new_page()
            try:
                logging.info("Opening UNGM notice list: %s", initial_url)
                await page.goto(initial_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                await wait_for_search_form(page)
                await apply_deadline_search_filter(page, today)
                await wait_for_results(page)
                for page_no in range(1, max_pages + 1):
                    pages_visited = page_no
                    page_notices = await extract_notices_from_page(page)
                    logging.info("Extracted %d notices from list page %d", len(page_notices), page_no)
                    before_total = len(notices_by_id)
                    for notice in page_notices:
                        notices_by_id.setdefault(notice.notice_id, notice)
                    new_count = len(notices_by_id)
                    page_stats = await page_loading_stats(page)
                    candidate_total = page_stats.get("expected_total")
                    if isinstance(candidate_total, int) and candidate_total > 0:
                        expected_total = candidate_total
                    logging.info(
                        "Collected %s/%s unique notices so far (page %d added %d)",
                        new_count,
                        expected_total if expected_total is not None else "?",
                        page_no,
                        new_count - before_total,
                    )
                    if not page_notices:
                        logging.info("No notices found on page %d; stopping", page_no)
                        break
                    if expected_total is not None and new_count >= expected_total:
                        logging.info(
                            "Reached expected total of %s notices; stopping pagination",
                            expected_total,
                        )
                        break
                    has_next = await click_next_page(page)
                    if not has_next:
                        # The next-page button may be missing because the list
                        # is actually a single infinite-scroll page. Try one
                        # more aggressive scroll cycle before giving up.
                        if expected_total is None or new_count < expected_total:
                            logging.info(
                                "No next page after page %d; trying one more infinite-scroll pass",
                                page_no,
                            )
                            await auto_scroll_page(page)
                            await wait_for_rows_stable(page)
                            extra_notices = await extract_notices_from_page(page)
                            for notice in extra_notices:
                                notices_by_id.setdefault(notice.notice_id, notice)
                            if len(notices_by_id) > new_count:
                                logging.info(
                                    "Infinite-scroll pass added %d notices",
                                    len(notices_by_id) - new_count,
                                )
                                continue
                        logging.info("No more pages available after page %d", page_no)
                        break
            finally:
                await page.close()
        finally:
            await browser.close()
    notices = list(notices_by_id.values())
    logging.info(
        "Scraped %d unique notices over %d list page(s) (expected total: %s)",
        len(notices),
        pages_visited,
        expected_total if expected_total is not None else "unknown",
    )
    if expected_total is not None and len(notices) < expected_total:
        logging.warning(
            "Only collected %d of %d advertised notices; some may have been missed",
            len(notices),
            expected_total,
        )
    return notices


async def enrich_notices(notices: list[Notice], headless: bool) -> None:
    if not notices:
        return
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        try:
            for notice in notices:
                await enrich_notice_detail(browser, notice)
        finally:
            await browser.close()


def passes_preliminary_filters(notice: Notice, today: date, sent_ids: set[str]) -> bool:
    if notice.notice_id in sent_ids:
        logging.info("Skipping already sent notice %s before detail enrichment", notice.notice_id)
        return False

    if notice.opportunity_type.lower() in SERVICE_OPPORTUNITY_TYPES:
        logging.info("Skipping service opportunity type before detail enrichment: %s", notice.notice_id)
        return False

    deadline = parse_ungm_date(notice.deadline_raw)
    if deadline:
        minimum_deadline = today + timedelta(days=10)
        if deadline < minimum_deadline:
            logging.info("Skipping notice %s by preliminary deadline %s", notice.notice_id, deadline)
            return False
    return True


def filter_notices(notices: Iterable[Notice], today: date, sent_ids: set[str]) -> list[Notice]:
    matched: list[Notice] = []
    for notice in notices:
        if notice.notice_id in sent_ids:
            logging.info("Skipping already sent notice %s", notice.notice_id)
            continue
        keep, reason = apply_filters(notice, today)
        if keep:
            logging.info("Matched notice %s: %s", notice.notice_id, notice.title)
            matched.append(notice)
        else:
            logging.info("Filtered out notice %s: %s", notice.notice_id or "(unknown)", reason)
    return matched


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def smtp_config() -> dict[str, str | int]:
    host = require_env("SMTP_HOST")
    port_raw = require_env("SMTP_PORT")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT must be an integer") from exc
    return {
        "host": host,
        "port": port,
        "user": require_env("SMTP_USER"),
        "password": require_env("SMTP_PASSWORD"),
        "mail_from": require_env("MAIL_FROM"),
        "mail_to": require_env("MAIL_TO"),
    }


def build_email_html(notices: list[Notice], report_date: date) -> str:
    if not notices:
        return f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #222;">
            <h2>UNGM 每日投标机会 - {report_date.isoformat()}</h2>
            <p>今天没有发现新的、符合筛选条件的 UNGM 货物采购机会。</p>
            <p style="color:#666;">筛选条件：截止日期至少还有 10 天、符合轻工业产品供货范围、排除服务类项目、已发送过的 notice id 不重复发送。</p>
          </body>
        </html>
        """

    items = []
    for notice in notices:
        title = html.escape(notice.title)
        url = html.escape(notice.url)
        organization = html.escape(notice.organization or "N/A")
        country = html.escape(notice.country or "N/A")
        published = html.escape(notice.published_date.isoformat() if notice.published_date else notice.published_raw)
        deadline = html.escape(notice.deadline_date.isoformat() if notice.deadline_date else notice.deadline_raw)
        reason = html.escape(notice.match_reason)
        items.append(
            f"""
            <tr>
              <td style="padding:12px;border-bottom:1px solid #ddd;">
                <a href="{url}" style="font-weight:700;color:#0b57d0;text-decoration:none;">{title}</a>
                <div style="margin-top:8px;color:#333;">
                  <strong>机构：</strong>{organization}<br>
                  <strong>国家：</strong>{country}<br>
                  <strong>发布时间：</strong>{published}<br>
                  <strong>截止日期：</strong>{deadline}<br>
                  <strong>匹配理由：</strong>{reason}
                </div>
              </td>
            </tr>
            """
        )

    return f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #222;">
        <h2>UNGM 每日投标机会 - {report_date.isoformat()}</h2>
        <p>发现 {len(notices)} 个新的匹配项目。</p>
        <table role="presentation" cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;max-width:920px;">
          {''.join(items)}
        </table>
      </body>
    </html>
    """


def build_plain_text(notices: list[Notice], report_date: date) -> str:
    if not notices:
        return (
            f"UNGM 每日投标机会 - {report_date.isoformat()}\n\n"
            "今天没有发现新的、符合筛选条件的 UNGM 货物采购机会。\n"
        )
    blocks = [f"UNGM 每日投标机会 - {report_date.isoformat()}", f"发现 {len(notices)} 个新的匹配项目。"]
    for notice in notices:
        blocks.append(
            "\n".join(
                [
                    "",
                    notice.title,
                    notice.url,
                    f"机构：{notice.organization or 'N/A'}",
                    f"国家：{notice.country or 'N/A'}",
                    f"发布时间：{notice.published_date.isoformat() if notice.published_date else notice.published_raw}",
                    f"截止日期：{notice.deadline_date.isoformat() if notice.deadline_date else notice.deadline_raw}",
                    f"匹配理由：{notice.match_reason}",
                ]
            )
        )
    return "\n".join(blocks)


def send_email(notices: list[Notice], report_date: date) -> None:
    config = smtp_config()
    recipients = [item.strip() for item in str(config["mail_to"]).split(",") if item.strip()]
    if not recipients:
        raise RuntimeError("MAIL_TO must contain at least one recipient")

    subject = f"UNGM 每日投标机会 - {report_date.isoformat()}"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = str(config["mail_from"])
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(build_plain_text(notices, report_date))
    msg.add_alternative(build_email_html(notices, report_date), subtype="html")

    host = str(config["host"])
    port = int(config["port"])
    logging.info("Sending email to %s via %s:%s", ", ".join(recipients), host, port)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as server:
            server.login(str(config["user"]), str(config["password"]))
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(str(config["user"]), str(config["password"]))
            server.send_message(msg)
    logging.info("Email sent successfully")


async def run(args: argparse.Namespace) -> int:
    report_date = today_in_timezone(args.timezone)
    sent_file = Path(args.sent_file)
    sent_ids = load_sent_ids(sent_file)
    logging.info("Report date is %s in timezone %s", report_date, args.timezone)

    try:
        notices = await scrape_notices(max_pages=args.max_pages, headless=not args.headful, today=report_date)
    except Exception as exc:
        logging.exception("UNGM scraping failed: %s", exc)
        return 1

    candidates = [notice for notice in notices if passes_preliminary_filters(notice, report_date, sent_ids)]
    logging.info("Enriching %d candidate notices after preliminary filtering", len(candidates))
    try:
        await enrich_notices(candidates, headless=not args.headful)
    except Exception as exc:
        logging.exception("Detail enrichment failed: %s", exc)
        return 1

    matched = filter_notices(candidates, report_date, sent_ids)
    logging.info("Matched %d new notices after filtering and de-duplication", len(matched))

    if args.dry_run:
        logging.info("Dry run enabled; email will not be sent and sent_ids.json will not be updated")
        for notice in matched:
            logging.info("DRY RUN MATCH %s %s", notice.notice_id, notice.title)
        if not sent_file.exists():
            save_sent_ids(sent_file, sent_ids)
        return 0

    try:
        send_email(matched, report_date)
    except Exception as exc:
        logging.exception("Email sending failed; sent IDs were not updated: %s", exc)
        return 1

    if matched:
        sent_ids.update(notice.notice_id for notice in matched)
    save_sent_ids(sent_file, sent_ids)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor UNGM procurement opportunities and email matching goods notices.")
    parser.add_argument("--max-pages", type=int, default=int(os.getenv("UNGM_MAX_PAGES", DEFAULT_MAX_PAGES)))
    parser.add_argument("--sent-file", default=os.getenv("SENT_IDS_FILE", DEFAULT_SENT_FILE))
    parser.add_argument("--timezone", default=os.getenv("TIMEZONE", DEFAULT_TIMEZONE))
    parser.add_argument("--headful", action="store_true", help="Run Playwright with a visible browser window.")
    parser.add_argument("--dry-run", action="store_true", help="Scrape and filter without sending email or marking notices sent.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    load_dotenv()
    args = parse_args(argv or sys.argv[1:])
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        logging.warning("Interrupted by user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
