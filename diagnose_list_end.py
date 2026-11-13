"""One normal list query; never imports watcher, SMTP or AI code.
Only allowlisted counters and structural counts are printed. No raw responses,
headers, query strings, credentials, titles or notice IDs leave the process.
"""
import asyncio
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

KEYS = {"page", "pageindex", "pagenumber", "pagesize", "skip", "take", "start",
        "offset", "limit", "total", "totalcount", "count", "hasmore", "hasnext",
        "islastpage", "end", "endofresults", "hasmoredata"}
def safe_numbers(obj):
    if not isinstance(obj, dict):
        return {}
    return {k: v for k, v in obj.items() if k.lower() in KEYS
            and isinstance(v, (int, float, bool)) and not isinstance(v, str)}

def summarize_body(text, content_type):
    if "json" in content_type:
        try:
            obj = json.loads(text)
        except ValueError:
            return {"kind": "invalid_json"}
        if isinstance(obj, list):
            return {"kind": "json_array", "items": len(obj)}
        if isinstance(obj, dict):
            out = {"kind": "json_object", "counters": safe_numbers(obj)}
            for key in ("data", "Data", "results", "Results", "items", "Items", "notices", "Notices"):
                if key in obj and isinstance(obj[key], list):
                    out["item_count"] = len(obj[key])
                elif key in obj and isinstance(obj[key], dict):
                    out["nested_counters"] = safe_numbers(obj[key])
            return out
        return {"kind": "json_other"}
    soup = BeautifulSoup(text, "html.parser")
    links = soup.select('a[href*="/Public/Notice/"]')
    ids = {m.group(1) for a in links if (m := re.search(r"/Public/Notice/(\d+)", a.get("href", "")))}
    return {"kind": "html", "notice_count": len(ids),
            "rows": len(soup.select('[role="row"], tr')),
            "blank_body": not bool(text.strip()),
            "explicit_empty": bool(re.search(r"No procurement opportunit(?:y|ies).*?(?:was|were) found", soup.get_text(" "), re.I)),
            "counter_nodes": len(soup.select("#noticesTotal,#searchCountNo,#noticeSearchTotal"))}

async def main():
    responses = []
    tasks = []
    stop = asyncio.Event()
    async def record(response):
        u = urlsplit(response.url)
        if response.request.resource_type not in ("xhr", "fetch") or u.hostname != "www.ungm.org":
            return
        # Only notice-list routes, not notice details or unrelated app calls.
        if not re.fullmatch(r"/Public/Notice(?:/[A-Za-z]+)*", u.path):
            return
        if len(responses) >= 40:
            stop.set()
            return
        item = {"sequence": len(responses)+1, "path": u.path, "status": response.status}
        responses.append(item)
        raw = response.request.post_data or ""
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = {k:int(v[0]) for k,v in parse_qs(raw).items() if k.lower() in KEYS and len(v)==1 and re.fullmatch(r"\d+",v[0])}
        item["request_counters"] = safe_numbers(obj)
        if response.status in (403, 429) or response.status >= 500:
            stop.set()
            item["stopped_on_http_error"] = True
            return
        try:
            item.update(summarize_body(await response.text(), response.headers.get("content-type","")))
        except Exception:
            item["body_unavailable"] = True

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()
        page.on("response", lambda r: tasks.append(asyncio.create_task(record(r))))
        try:
            response = await page.goto("https://www.ungm.org/Public/Notice", wait_until="domcontentloaded", timeout=30000)
            print(json.dumps({"entry_status": response.status if response else None}), flush=True)
            if response and response.status >= 400:
                return
            await page.locator("#lnkSearch").wait_for()
            # Dismiss only the normal language-preference dialog, not challenges.
            close = page.get_by_role("button", name="No, thank you", exact=True)
            if await close.count() and await close.is_visible():
                await close.click()
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            for selector, d in (
                ("#txtNoticePublishedFrom",today-timedelta(days=3)),
                ("#txtNoticePublishedTo",today),
                ("#txtNoticeDeadlineFrom",today+timedelta(days=10)),
                ("#txtNoticeDeadlineTo",today+timedelta(days=730))):
                await page.locator(selector).fill(d.strftime("%d-%b-%Y"))
            await page.locator("#lnkSearch").click()
            await page.wait_for_timeout(2000)
            previous = 0
            unchanged = 0
            for step in range(40):
                if stop.is_set():
                    break
                await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                await page.wait_for_timeout(1400)
                count = await page.locator('#tblNotices a[href*="/Public/Notice/"]').count()
                print(json.dumps({"step":step+1,"visible_link_count":count,"captured_responses":len(responses)}),flush=True)
                unchanged = unchanged+1 if count == previous else 0
                previous = count
                if unchanged >= 8:
                    break
                # A normal short upward move permits the next bottom scroll event.
                await page.evaluate("window.scrollBy(0, -600)")
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            print("SAFE_LIST_RESPONSES="+json.dumps(responses),flush=True)
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
