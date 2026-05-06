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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from dateutil import parser as date_parser


BASE_URL = "https://www.ungm.org"
NOTICE_LIST_URL = f"{BASE_URL}/Public/Notice"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_SENT_FILE = "sent_ids.json"
DEFAULT_MAX_PAGES = 20
RECENT_PUBLISHED_DAYS = 3
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_TIMEOUT_SECONDS = 20
DEFAULT_DEEPSEEK_CONCURRENCY = 4
PAGE_TIMEOUT_MS = 45_000
PAGE_STABILITY_CHECKS = 3
PAGE_STABILITY_INTERVAL_MS = 700
PAGE_STABILITY_MAX_ATTEMPTS = 12
AUTO_SCROLL_MAX_STEPS = 220
AUTO_SCROLL_INTERVAL_MS = 700
AUTO_SCROLL_IDLE_CHECKS = 60
DETAIL_ENRICH_CONCURRENCY = 4
DEBUG_DIR = Path("debug")
COMPANY_COUNTRY = "China"

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
    "tents": ["tents", "tent", "family tents", "shelter tents", "emergency shelter", "tarpaulins", "tarpaulin"],
    "household items": [
        "household items",
        "household item",
        "household goods",
        "household good",
        "household supplies",
        "household supply",
        "household kits",
        "household kit",
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

LOCAL_SUPPLIER_TERMS = (
    r"local\s+(?:suppliers?|vendors?|companies?|firms?|business(?:es)?|manufacturers?|distributors?|contractors?)"
)
MANDATORY_LOCAL_SUPPLIER_PATTERNS = [
    re.compile(rf"\b(?:only|exclusively)\s+(?:registered\s+)?{LOCAL_SUPPLIER_TERMS}\b", re.IGNORECASE),
    re.compile(
        rf"\b{LOCAL_SUPPLIER_TERMS}\s+(?:only|are\s+eligible|will\s+be\s+considered|shall\s+be\s+considered)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:must|shall|mandatory|required|requires?|restricted\s+to|limited\s+to)\b.{{0,100}}\b{LOCAL_SUPPLIER_TERMS}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{LOCAL_SUPPLIER_TERMS}\b.{{0,100}}\b(?:must|shall|required|mandatory|eligible|restricted|limited)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:强制|必须|仅限|只限|限于).{0,40}(?:本地|当地|本国).{0,20}(?:供应商|供货商|公司|企业)"),
    re.compile(r"(?:本地|当地|本国).{0,20}(?:供应商|供货商|公司|企业).{0,40}(?:强制|必须|仅限|只限|限于)"),
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


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    model: str
    base_url: str
    timeout_seconds: float
    concurrency: int


@dataclass(frozen=True)
class DeepSeekDecision:
    match: bool
    confidence: float
    matched_categories: list[str]
    reason: str


class DeepSeekClassificationError(RuntimeError):
    pass


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
    return f"{value.day:02d}-{UNGM_DATE_MONTHS[value.month - 1]}-{value:%y}"


def recent_published_cutoff(today: date) -> date:
    return today - timedelta(days=RECENT_PUBLISHED_DAYS)


def published_recent_enough(notice: Notice, today: date) -> tuple[bool, str]:
    notice.published_date = parse_ungm_date(notice.published_raw)
    if not notice.published_date:
        return False, "missing or invalid published date"
    cutoff = recent_published_cutoff(today)
    if notice.published_date < cutoff:
        return False, f"published {notice.published_date} is before {cutoff}"
    if notice.published_date > today + timedelta(days=1):
        return False, f"published {notice.published_date} is unexpectedly in the future"
    return True, ""


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
          const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
          };
          const hasNoticeId = (value) => /\/Public\/Notice\/\d+/i.test(value) || /\bNotice\s*ID\s*:?\s*\d+/i.test(value);
          const noResultElements = Array.from(document.querySelectorAll('td, [role="cell"], p, span, div, li'))
            .filter(isVisible)
            .map((el) => clean(el.innerText || el.textContent))
            .filter((text) => text.length <= 140 && /No procurement opportunit(?:y|ies)\s+(?:was|were)\s+found/i.test(text));
          const summaryText = clean(document.querySelector('#noticesTotal')?.innerText || '');
          const summaryMatch = summaryText.match(/Displaying results\s+(\d+)\s+to\s+(\d+)\s+of\s+(\d+)/i);
          const noticeLinks = Array.from(document.querySelectorAll('a[href*="/Public/Notice/"]')).filter(isVisible);
          const rowCount = Array.from(document.querySelectorAll('tbody tr, [role="row"]'))
            .filter((row) => {
              if (!isVisible(row) || row.querySelector('th, [role="columnheader"]')) return false;
              const cells = Array.from(row.querySelectorAll('td, [role="cell"]')).filter(isVisible);
              const rowText = clean(row.innerText);
              return cells.length >= 5 && (row.querySelector('a[href*="/Public/Notice/"]') || hasNoticeId(rowText));
            }).length;
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
          return {
            row_count: rowCount,
            notice_link_count: noticeLinks.length,
            unique_notice_count: uniqueIds.length,
            first_notice_id: uniqueIds[0] || '',
            last_notice_id: uniqueIds[uniqueIds.length - 1] || '',
            no_results: noResultElements.length > 0,
            no_results_contexts: noResultElements.slice(0, 3),
            result_displayed_from: summaryMatch ? Number(summaryMatch[1]) : 0,
            result_displayed_to: summaryMatch ? Number(summaryMatch[2]) : 0,
            result_total: summaryMatch ? Number(summaryMatch[3]) : 0,
            body_text_length: document.body.innerText.length,
            scroll_height: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
            next_candidates: nextCandidates,
          };
        }
        """
    )


def page_signature(stats: dict[str, Any]) -> str:
    return "|".join(
        [
            str(stats.get("row_count", 0)),
            str(stats.get("unique_notice_count", 0)),
            str(stats.get("result_displayed_to", 0)),
            str(stats.get("result_total", 0)),
            str(stats.get("first_notice_id", "")),
            str(stats.get("last_notice_id", "")),
        ]
    )


async def auto_scroll_page(page: Any) -> None:
    result = await page.evaluate(
        """
        async ({maxSteps, intervalMs, idleChecks}) => {
          const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
          };
          const countNoticeIds = () => {
            const ids = Array.from(document.querySelectorAll('a[href*="/Public/Notice/"]')).filter(isVisible)
              .map((link) => {
                const value = `${link.href || ''} ${link.innerText || ''}`;
                const match = value.match(/\\/Public\\/Notice\\/(\\d+)/i) || value.match(/\\bNotice\\s*ID\\s*:?\\s*(\\d+)/i);
                return match ? match[1] : '';
              })
              .filter(Boolean);
            return Array.from(new Set(ids)).length;
          };
          const resultSummary = () => {
            const text = clean(document.querySelector('#noticesTotal')?.innerText || '');
            const match = text.match(/Displaying results\\s+(\\d+)\\s+to\\s+(\\d+)\\s+of\\s+(\\d+)/i);
            return match ? {from: Number(match[1]), to: Number(match[2]), total: Number(match[3])} : {from: 0, to: 0, total: 0};
          };
          let lastLoaded = 0;
          let unchanged = 0;
          let steps = 0;
          let latestSummary = resultSummary();
          let latestUnique = countNoticeIds();
          while (steps < maxSteps) {
            const height = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
            if (unchanged > 0) {
              window.scrollBy(0, -800);
              await wait(100);
            }
            window.scrollTo(0, height);
            window.dispatchEvent(new Event('scroll'));
            await wait(intervalMs);
            latestSummary = resultSummary();
            latestUnique = countNoticeIds();
            const loaded = Math.max(latestUnique, latestSummary.to || 0);
            if (loaded > lastLoaded) {
              unchanged = 0;
              lastLoaded = loaded;
            } else {
              unchanged += 1;
            }
            steps += 1;
            if (latestSummary.total && loaded >= latestSummary.total) break;
            if (!latestSummary.total && unchanged >= 8) break;
            if (latestSummary.total && unchanged >= idleChecks) break;
          }
          return {
            steps,
            unchanged,
            height: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
            unique_notices: latestUnique,
            displayed_to: latestSummary.to,
            result_total: latestSummary.total,
          };
        }
        """,
        {
            "maxSteps": AUTO_SCROLL_MAX_STEPS,
            "intervalMs": AUTO_SCROLL_INTERVAL_MS,
            "idleChecks": AUTO_SCROLL_IDLE_CHECKS,
        },
    )
    logging.info(
        "Auto-scroll completed after %s steps; notices=%s displayed_to=%s total=%s page height=%s",
        result.get("steps"),
        result.get("unique_notices"),
        result.get("displayed_to"),
        result.get("result_total"),
        result.get("height"),
    )


async def wait_for_rows_stable(page: Any) -> dict[str, Any]:
    stable_count = 0
    previous_signature = ""
    latest_stats: dict[str, Any] = {}
    for attempt in range(1, PAGE_STABILITY_MAX_ATTEMPTS + 1):
        latest_stats = await page_loading_stats(page)
        signature = page_signature(latest_stats)
        logging.info(
            (
                "Load stability check %d/%d: rows=%s notice_links=%s unique_notices=%s "
                "displayed_to=%s total=%s no_results=%s first=%s last=%s"
            ),
            attempt,
            PAGE_STABILITY_MAX_ATTEMPTS,
            latest_stats.get("row_count"),
            latest_stats.get("notice_link_count"),
            latest_stats.get("unique_notice_count"),
            latest_stats.get("result_displayed_to"),
            latest_stats.get("result_total"),
            latest_stats.get("no_results"),
            latest_stats.get("first_notice_id") or "N/A",
            latest_stats.get("last_notice_id") or "N/A",
        )
        if signature == previous_signature:
            stable_count += 1
        else:
            stable_count = 1
            previous_signature = signature
        ready = (
            latest_stats.get("unique_notice_count", 0)
            or latest_stats.get("no_results", False)
        )
        result_total = int(latest_stats.get("result_total", 0) or 0)
        result_loaded = max(
            int(latest_stats.get("unique_notice_count", 0) or 0),
            int(latest_stats.get("result_displayed_to", 0) or 0),
        )
        complete_or_unknown = not result_total or result_loaded >= result_total
        if stable_count >= PAGE_STABILITY_CHECKS and ready and complete_or_unknown:
            return latest_stats
        await page.wait_for_timeout(PAGE_STABILITY_INTERVAL_MS)
    logging.warning("Notice rows did not fully stabilize before timeout; continuing with latest page state")
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
              (() => {
                const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const isVisible = (el) => {
                  if (!el) return false;
                  const style = window.getComputedStyle(el);
                  const rect = el.getBoundingClientRect();
                  return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                };
                const hasNoticeId = (value) => /\\/Public\\/Notice\\/\\d+/i.test(value) || /\\bNotice\\s*ID\\s*:?\\s*\\d+/i.test(value);
                const noResults = Array.from(document.querySelectorAll('td, [role="cell"], p, span, div, li'))
                  .filter(isVisible)
                  .map((el) => clean(el.innerText || el.textContent))
                  .some((text) => text.length <= 140 && /No procurement opportunit(?:y|ies)\\s+(?:was|were)\\s+found/i.test(text));
                const rows = Array.from(document.querySelectorAll('tbody tr, [role="row"]'))
                  .filter((row) => {
                    if (!isVisible(row) || row.querySelector('th, [role="columnheader"]')) return false;
                    const cells = Array.from(row.querySelectorAll('td, [role="cell"]')).filter(isVisible);
                    const rowText = clean(row.innerText);
                    return cells.length >= 5 && (row.querySelector('a[href*="/Public/Notice/"]') || hasNoticeId(rowText));
                  });
                const noticeLinks = Array.from(document.querySelectorAll('a[href*="/Public/Notice/"]')).filter(isVisible);
                const resultSummary = Array.from(document.querySelectorAll('body *'))
                  .some((el) => {
                    const text = clean(el.innerText || el.textContent);
                    return text.length <= 180 && /Displaying results\\s+\\d+\\s+to\\s+\\d+\\s+of\\s+\\d+/i.test(text);
                  });
                return noticeLinks.length > 0 || rows.length > 0 || resultSummary || noResults;
              })()
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


async def dismiss_language_preference_modal(page: Any) -> None:
    try:
        dismissed = await page.evaluate(
            """
            () => {
              const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const dialogs = Array.from(document.querySelectorAll('[role="dialog"], .ui-dialog, .modal, body'))
                .filter(isVisible)
                .filter((el) => /Language preferences/i.test(clean(el.innerText || el.textContent)));
              for (const dialog of dialogs) {
                const controls = Array.from(dialog.querySelectorAll('button, a, input[type="button"], input[type="submit"]'))
                  .filter(isVisible);
                const reject = controls.find((el) => {
                  const text = clean(el.innerText || el.value || el.textContent);
                  return /No,? thank/i.test(text) || /不.*谢/.test(text) || /No,? gracias/i.test(text);
                });
                if (reject) {
                  reject.click();
                  return true;
                }
                const close = controls.find((el) => {
                  const text = clean(el.innerText || el.value || el.textContent || el.getAttribute('aria-label'));
                  return text === '×' || /close/i.test(text);
                });
                if (close) {
                  close.click();
                  return true;
                }
              }
              return false;
            }
            """
        )
        if dismissed:
            logging.info("Dismissed UNGM language preference modal")
            await page.wait_for_timeout(500)
    except Exception as exc:
        logging.info("Could not dismiss UNGM language preference modal: %s", exc)


async def apply_notice_search_filter(page: Any, today: date) -> bool:
    published_from = format_ungm_filter_date(recent_published_cutoff(today))
    published_to = format_ungm_filter_date(today)
    minimum_deadline = today + timedelta(days=10)
    deadline_from = format_ungm_filter_date(minimum_deadline)
    deadline_to = format_ungm_filter_date(today + timedelta(days=730))
    before_stats = await page_loading_stats(page)
    before_signature = page_signature(before_stats)
    logging.info(
        "Applying UNGM search filter: Published between %s and %s; Deadline between %s and %s",
        published_from,
        published_to,
        deadline_from,
        deadline_to,
    )

    result = await page.evaluate(
        """
        ({publishedFrom, publishedTo, deadlineFrom, deadlineTo}) => {
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
          const findInput = (selectors) => {
            for (const selector of selectors) {
              const input = document.querySelector(selector);
              if (input) return input;
            }
            return null;
          };
          const visibleTextInputs = () => Array.from(document.querySelectorAll(
            'input:not([type]), input[type="text"], input[type="search"], input[type="date"]'
          )).filter(isVisible);
          const deadlineFromInput = findInput([
            '#txtNoticeDeadlineFrom',
            '#txtNoticeDeadlineDateFrom',
            '#txtDeadlineFrom',
            'input[name="NoticeDeadlineFrom"]',
            'input[name="DeadlineFrom"]',
          ]);
          const deadlineToInput = findInput([
            '#txtNoticeDeadlineTo',
            '#txtNoticeDeadlineDateTo',
            '#txtDeadlineTo',
            'input[name="NoticeDeadlineTo"]',
            'input[name="DeadlineTo"]',
          ]);
          if (!deadlineFromInput || !deadlineToInput) {
            return {ok: false, reason: 'Deadline inputs not found by id'};
          }

          let publishedFromInput = findInput([
            '#txtNoticePublishedFrom',
            '#txtNoticePublishedDateFrom',
            '#txtNoticePublicationFrom',
            '#txtPublishedFrom',
            '#txtPublicationFrom',
            'input[name="NoticePublishedFrom"]',
            'input[name="PublishedFrom"]',
            'input[name="PublicationFrom"]',
          ]);
          let publishedToInput = findInput([
            '#txtNoticePublishedTo',
            '#txtNoticePublishedDateTo',
            '#txtNoticePublicationTo',
            '#txtPublishedTo',
            '#txtPublicationTo',
            'input[name="NoticePublishedTo"]',
            'input[name="PublishedTo"]',
            'input[name="PublicationTo"]',
          ]);
          if (!publishedFromInput || !publishedToInput) {
            const inputs = visibleTextInputs();
            const deadlineIndex = inputs.indexOf(deadlineFromInput);
            if (deadlineIndex >= 2) {
              publishedFromInput = inputs[deadlineIndex - 2];
              publishedToInput = inputs[deadlineIndex - 1];
            }
          }
          if (!publishedFromInput || !publishedToInput) {
            return {ok: false, reason: 'Published inputs not found'};
          }

          setNativeValue(publishedFromInput, publishedFrom);
          setNativeValue(publishedToInput, publishedTo);
          setNativeValue(deadlineFromInput, deadlineFrom);
          setNativeValue(deadlineToInput, deadlineTo);

          const activeCheckbox = document.querySelector('#chkIsActive');
          if (activeCheckbox && !activeCheckbox.checked) {
            activeCheckbox.click();
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
            published_from: publishedFromInput.value,
            published_to: publishedToInput.value,
            published_from_id: publishedFromInput.id || publishedFromInput.name || '',
            published_to_id: publishedToInput.id || publishedToInput.name || '',
            deadline_from: deadlineFromInput.value,
            deadline_to: deadlineToInput.value,
            deadline_from_id: deadlineFromInput.id || deadlineFromInput.name || '',
            deadline_to_id: deadlineToInput.id || deadlineToInput.name || '',
            active_only: activeCheckbox ? Boolean(activeCheckbox.checked) : null,
          };
        }
        """,
        {
            "publishedFrom": published_from,
            "publishedTo": published_to,
            "deadlineFrom": deadline_from,
            "deadlineTo": deadline_to,
        },
    )
    if not result.get("ok"):
        logging.warning("Could not apply UNGM browser-side search filter: %s", result)
        await save_page_debug_artifacts(page, "ungm-filter-not-applied")
        return False

    logging.info("UNGM browser-side search filter applied: %s", result)
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
              const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const hasNoticeId = (value) => /\\/Public\\/Notice\\/\\d+/i.test(value) || /\\bNotice\\s*ID\\s*:?\\s*\\d+/i.test(value);
              const noResults = Array.from(document.querySelectorAll('td, [role="cell"], p, span, div, li'))
                .filter(isVisible)
                .map((el) => clean(el.innerText || el.textContent))
                .some((text) => text.length <= 140 && /No procurement opportunit(?:y|ies)\\s+(?:was|were)\\s+found/i.test(text));
              const rowCount = Array.from(document.querySelectorAll('tbody tr, [role="row"]'))
                .filter((row) => {
                  if (!isVisible(row) || row.querySelector('th, [role="columnheader"]')) return false;
                  const cells = Array.from(row.querySelectorAll('td, [role="cell"]')).filter(isVisible);
                  const rowText = clean(row.innerText);
                  return cells.length >= 5 && (row.querySelector('a[href*="/Public/Notice/"]') || hasNoticeId(rowText));
                }).length;
              const ids = Array.from(document.querySelectorAll('a[href*="/Public/Notice/"]')).filter(isVisible)
                .map((link) => {
                  const value = `${link.href || ''} ${link.innerText || ''}`;
                  const match = value.match(/\\/Public\\/Notice\\/(\\d+)/i) || value.match(/\\bNotice\\s*ID\\s*:?\\s*(\\d+)/i);
                  return match ? match[1] : '';
                })
                .filter(Boolean);
              const uniqueIds = Array.from(new Set(ids));
              const signature = [
                String(rowCount),
                String(uniqueIds.length),
                uniqueIds[0] || '',
                uniqueIds[uniqueIds.length - 1] || '',
              ].join('|');
              return signature !== previousSignature || uniqueIds.length > 0 || noResults;
            }
            """,
            arg=before_signature,
            timeout=15_000,
        )
    except Exception as exc:
        if not is_playwright_timeout(exc):
            raise
        logging.warning("Search results did not visibly change after applying deadline filter; continuing with current content")
    return True


async def load_unfiltered_notice_list(page: Any) -> None:
    logging.warning("Browser-side search filter returned no notices; falling back to the unfiltered UNGM list")
    await page.goto(NOTICE_LIST_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    await wait_for_search_form(page)
    await dismiss_language_preference_modal(page)
    await wait_for_results(page)


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
        logging.info("No enabled next page control found. Diagnostics: %s", click_result.get("diagnostics", []))
        await save_page_debug_artifacts(page, "ungm-no-next-page")
        return False
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

    service_text = " ".join(
        [
            notice.title,
            notice.opportunity_type,
            notice.reference,
            notice.description,
        ]
    )
    for pattern in SERVICE_CATEGORY_PATTERNS:
        if pattern.search(service_text):
            reasons.append(f"service UNSPSC/category pattern: {pattern.pattern}")

    excluded = matched_terms(service_text, SERVICE_EXCLUSION_KEYWORDS)
    reasons.extend(f"service keyword: {item}" for item in excluded)
    return bool(reasons), reasons


def goods_confirmation(notice: Notice) -> str:
    combined = " ".join([notice.title, notice.reference, notice.opportunity_type, notice.description])
    for pattern in GOODS_CONFIRMATION_PATTERNS:
        match = pattern.search(combined)
        if match:
            return normalize_space(match.group(0))
    if any(term_matches(combined, word) for word in ["goods", "supplies", "equipment", "products", "items"]):
        return "goods/supplies wording"
    return ""


def mandatory_local_supplier_reason(notice: Notice) -> str:
    combined = " ".join(
        [
            notice.title,
            notice.reference,
            notice.opportunity_type,
            notice.description,
            notice.detail_text,
        ]
    )
    for pattern in MANDATORY_LOCAL_SUPPLIER_PATTERNS:
        match = pattern.search(combined)
        if match:
            return (
                "mandatory local supplier requirement "
                f"for a company established in {COMPANY_COUNTRY}: {normalize_space(match.group(0))}"
            )
    return ""


def parse_int_env(name: str, default: int, minimum: int = 1) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logging.warning("Invalid %s=%r; using default %s", name, value, default)
        return default
    if parsed < minimum:
        logging.warning("%s=%s is below minimum %s; using default %s", name, parsed, minimum, default)
        return default
    return parsed


def parse_float_env(name: str, default: float, minimum: float = 0.1) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        logging.warning("Invalid %s=%r; using default %s", name, value, default)
        return default
    if parsed < minimum:
        logging.warning("%s=%s is below minimum %s; using default %s", name, parsed, minimum, default)
        return default
    return parsed


def deepseek_config_from_env() -> DeepSeekConfig | None:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None
    return DeepSeekConfig(
        api_key=api_key,
        model=os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip() or DEFAULT_DEEPSEEK_MODEL,
        base_url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).strip() or DEFAULT_DEEPSEEK_BASE_URL,
        timeout_seconds=parse_float_env("DEEPSEEK_TIMEOUT_SECONDS", DEFAULT_DEEPSEEK_TIMEOUT_SECONDS),
        concurrency=parse_int_env("DEEPSEEK_CONCURRENCY", DEFAULT_DEEPSEEK_CONCURRENCY),
    )


def truncate_for_prompt(value: str, limit: int = 4000) -> str:
    value = normalize_space(value)
    if len(value) <= limit:
        return value
    return f"{value[:limit].rstrip()}..."


def deepseek_notice_payload(notice: Notice) -> dict[str, str]:
    return {
        "title": notice.title,
        "reference": notice.reference,
        "opportunity_type": notice.opportunity_type,
        "organization": notice.organization,
        "country": notice.country,
        "description": truncate_for_prompt(notice.description),
    }


def deepseek_system_prompt() -> str:
    return """
You are classifying UNGM procurement notices for a light-industry goods supplier.
Return only valid JSON.

The company wants supply/manufacturing opportunities for light-industry goods, including:
- stationery, office supplies, school supplies, learning kits
- toys, recreational items, sports balls, sports equipment, playground equipment
- school bags, backpacks, bags, luggage
- plastic goods, textile goods, tents, tarpaulins, emergency shelter items
- household items, household kits, kitchenware, hygiene kits
- garments, clothing, uniforms, gift items, children's products, educational supplies

Exclude service-heavy opportunities, including:
- consulting, training, maintenance, construction or civil works
- research, audit, surveys, assessments, recruitment
- IT services, software/system integration, logistics/transport, event services

Judge whether the notice is likely a relevant goods supply opportunity for this company.
If the notice combines goods and services, match only when goods supply is the main procurement object.

Return this JSON shape:
{
  "match": true,
  "confidence": 0.85,
  "matched_categories": ["tents", "household items"],
  "reason": "中文简短说明，说明为什么符合或不符合"
}
""".strip()


def deepseek_user_prompt(notice: Notice) -> str:
    return (
        "Classify this UNGM notice. Respond in json only.\n\n"
        + json.dumps(deepseek_notice_payload(notice), ensure_ascii=False, indent=2)
    )


def normalize_deepseek_decision(raw: Any) -> DeepSeekDecision:
    if not isinstance(raw, dict):
        raise DeepSeekClassificationError("DeepSeek JSON response is not an object")
    match = raw.get("match")
    if not isinstance(match, bool):
        raise DeepSeekClassificationError("DeepSeek JSON response missing boolean match")
    confidence_raw = raw.get("confidence", 0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    categories_raw = raw.get("matched_categories", [])
    if isinstance(categories_raw, list):
        categories = [normalize_space(str(item)) for item in categories_raw if normalize_space(str(item))]
    elif categories_raw:
        categories = [normalize_space(str(categories_raw))]
    else:
        categories = []
    reason = normalize_space(str(raw.get("reason", "")))
    if not reason:
        reason = "DeepSeek 未提供具体理由"
    return DeepSeekDecision(
        match=match,
        confidence=confidence,
        matched_categories=categories,
        reason=reason,
    )


def call_deepseek_for_notice(notice: Notice, config: DeepSeekConfig) -> DeepSeekDecision:
    endpoint = f"{config.base_url.rstrip('/')}/chat/completions"
    body = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": deepseek_system_prompt()},
            {"role": "user", "content": deepseek_user_prompt(notice)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 600,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DeepSeekClassificationError(f"DeepSeek HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise DeepSeekClassificationError(f"DeepSeek request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise DeepSeekClassificationError("DeepSeek request timed out") from exc
    except json.JSONDecodeError as exc:
        raise DeepSeekClassificationError("DeepSeek response was not valid JSON") from exc

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DeepSeekClassificationError("DeepSeek response missing message content") from exc
    if not normalize_space(str(content)):
        raise DeepSeekClassificationError("DeepSeek returned empty content")
    try:
        decision_raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DeepSeekClassificationError("DeepSeek message content was not valid JSON") from exc
    return normalize_deepseek_decision(decision_raw)


def format_deepseek_match_reason(decision: DeepSeekDecision) -> str:
    parts = [
        f"DeepSeek判断：{decision.reason}",
        f"置信度 {decision.confidence:.2f}",
    ]
    if decision.matched_categories:
        parts.append(f"匹配类别：{', '.join(decision.matched_categories)}")
    return "；".join(parts)


def passes_final_hard_filters(notice: Notice, today: date, sent_ids: set[str]) -> tuple[bool, str]:
    if notice.notice_id in sent_ids:
        return False, "already sent"
    notice.published_date = parse_ungm_date(notice.published_raw)
    notice.deadline_date = parse_ungm_date(notice.deadline_raw)

    if not notice.title:
        return False, "missing title"
    recent, recent_reason = published_recent_enough(notice, today)
    if not recent:
        return False, recent_reason
    if not notice.deadline_date:
        return False, "missing or invalid deadline date"

    minimum_deadline = today + timedelta(days=10)
    if notice.deadline_date < minimum_deadline:
        return False, f"deadline {notice.deadline_date} is before {minimum_deadline}"

    local_supplier_reason = mandatory_local_supplier_reason(notice)
    if local_supplier_reason:
        return False, local_supplier_reason

    service, service_reasons = is_service_notice(notice)
    if service:
        return False, "; ".join(service_reasons)
    return True, ""


def apply_filters(notice: Notice, today: date) -> tuple[bool, str]:
    notice.published_date = parse_ungm_date(notice.published_raw)
    notice.deadline_date = parse_ungm_date(notice.deadline_raw)

    if not notice.title:
        return False, "missing title"
    recent, recent_reason = published_recent_enough(notice, today)
    if not recent:
        return False, recent_reason
    if not notice.deadline_date:
        return False, "missing or invalid deadline date"

    minimum_deadline = today + timedelta(days=10)
    if notice.deadline_date < minimum_deadline:
        return False, f"deadline {notice.deadline_date} is before {minimum_deadline}"

    local_supplier_reason = mandatory_local_supplier_reason(notice)
    if local_supplier_reason:
        return False, local_supplier_reason

    service, service_reasons = is_service_notice(notice)
    if service:
        return False, "; ".join(service_reasons)

    keyword_text = " ".join(
        [
            notice.title,
            notice.reference,
            notice.opportunity_type,
            notice.description,
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
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = await context.new_page()
            try:
                logging.info("Opening UNGM notice list: %s", NOTICE_LIST_URL)
                await page.goto(NOTICE_LIST_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                await wait_for_search_form(page)
                await dismiss_language_preference_modal(page)
                filter_applied = await apply_notice_search_filter(page, today)
                await wait_for_results(page)
                if filter_applied:
                    filtered_stats = await page_loading_stats(page)
                    if (
                        not filtered_stats.get("unique_notice_count", 0)
                        and filtered_stats.get("no_results", False)
                    ):
                        await save_page_debug_artifacts(page, "ungm-filter-empty-results")
                        await load_unfiltered_notice_list(page)
                for page_no in range(1, max_pages + 1):
                    page_notices = await extract_notices_from_page(page)
                    logging.info("Extracted %d notices from list page %d", len(page_notices), page_no)
                    if not page_notices:
                        await save_page_debug_artifacts(page, f"ungm-empty-results-page-{page_no}")
                    for notice in page_notices:
                        notices_by_id.setdefault(notice.notice_id, notice)
                    if not page_notices:
                        break
                    has_next = await click_next_page(page)
                    if not has_next:
                        logging.info("No next page control found after page %d", page_no)
                        break
            finally:
                await context.close()
        finally:
            await browser.close()
    notices = list(notices_by_id.values())
    logging.info("Found %d unique notices before detail enrichment", len(notices))
    return notices


async def enrich_notices(notices: list[Notice], headless: bool) -> None:
    if not notices:
        return
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        try:
            logging.info(
                "Enriching %d notices with detail concurrency %d",
                len(notices),
                DETAIL_ENRICH_CONCURRENCY,
            )
            semaphore = asyncio.Semaphore(DETAIL_ENRICH_CONCURRENCY)

            async def enrich_with_limit(notice: Notice) -> None:
                async with semaphore:
                    await enrich_notice_detail(browser, notice)

            await asyncio.gather(*(enrich_with_limit(notice) for notice in notices))
        finally:
            await browser.close()


def passes_preliminary_filters(notice: Notice, today: date, sent_ids: set[str]) -> bool:
    if notice.notice_id in sent_ids:
        logging.info("Skipping already sent notice %s before detail enrichment", notice.notice_id)
        return False

    recent, recent_reason = published_recent_enough(notice, today)
    if not recent:
        logging.info("Skipping notice %s by preliminary published date: %s", notice.notice_id, recent_reason)
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


async def classify_notice_with_deepseek(
    notice: Notice,
    config: DeepSeekConfig,
    semaphore: asyncio.Semaphore,
) -> tuple[Notice, DeepSeekDecision | None, str]:
    async with semaphore:
        try:
            decision = await asyncio.to_thread(call_deepseek_for_notice, notice, config)
        except DeepSeekClassificationError as exc:
            return notice, None, str(exc)
        except Exception as exc:
            return notice, None, f"unexpected DeepSeek error: {exc}"
    return notice, decision, ""


async def filter_notices_with_deepseek(
    notices: Iterable[Notice],
    today: date,
    sent_ids: set[str],
    config: DeepSeekConfig,
) -> list[Notice]:
    hard_passed: list[Notice] = []
    for notice in notices:
        keep, reason = passes_final_hard_filters(notice, today, sent_ids)
        if keep:
            hard_passed.append(notice)
        else:
            logging.info("Filtered out notice %s before DeepSeek: %s", notice.notice_id or "(unknown)", reason)

    logging.info(
        "Classifying %d notices with DeepSeek model=%s concurrency=%d",
        len(hard_passed),
        config.model,
        config.concurrency,
    )
    semaphore = asyncio.Semaphore(config.concurrency)
    tasks = [classify_notice_with_deepseek(notice, config, semaphore) for notice in hard_passed]

    matched: list[Notice] = []
    ai_matches = 0
    ai_rejections = 0
    fallback_matches = 0
    fallback_rejections = 0
    errors = 0
    for notice, decision, error in await asyncio.gather(*tasks):
        if decision is None:
            errors += 1
            keep, fallback_reason = apply_filters(notice, today)
            if keep:
                fallback_matches += 1
                notice.match_reason = f"DeepSeek fallback：{fallback_reason}"
                matched.append(notice)
                logging.info(
                    "Matched notice %s by legacy fallback after DeepSeek error: %s",
                    notice.notice_id,
                    error,
                )
            else:
                fallback_rejections += 1
                logging.info(
                    "Filtered out notice %s by legacy fallback after DeepSeek error (%s): %s",
                    notice.notice_id or "(unknown)",
                    error,
                    fallback_reason,
                )
            continue

        if decision.match:
            ai_matches += 1
            notice.match_reason = format_deepseek_match_reason(decision)
            matched.append(notice)
            logging.info(
                "Matched notice %s by DeepSeek: confidence=%.2f categories=%s",
                notice.notice_id,
                decision.confidence,
                ", ".join(decision.matched_categories) or "N/A",
            )
        else:
            ai_rejections += 1
            logging.info(
                "Filtered out notice %s by DeepSeek: confidence=%.2f reason=%s",
                notice.notice_id or "(unknown)",
                decision.confidence,
                decision.reason,
            )

    logging.info(
        (
            "DeepSeek classification summary: ai_matches=%d ai_rejections=%d "
            "fallback_matches=%d fallback_rejections=%d errors=%d"
        ),
        ai_matches,
        ai_rejections,
        fallback_matches,
        fallback_rejections,
        errors,
    )
    return matched


async def filter_notices_for_business(
    notices: Iterable[Notice],
    today: date,
    sent_ids: set[str],
) -> list[Notice]:
    config = deepseek_config_from_env()
    if not config:
        logging.info("DEEPSEEK_API_KEY is not configured; using legacy keyword business matching")
        return filter_notices(notices, today, sent_ids)
    return await filter_notices_with_deepseek(notices, today, sent_ids, config)


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
            <p style="color:#666;">筛选条件：截止日期至少还有 10 天、符合轻工业产品供货范围、排除强制本地供应商项目、排除服务类项目、已发送过的 notice id 不重复发送。</p>
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

    matched = await filter_notices_for_business(candidates, report_date, sent_ids)
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
