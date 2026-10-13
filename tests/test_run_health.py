"""Offline regressions for incomplete runs, failure mail, and delivery state.

All SMTP, provider HTTP, and browser entry points are mocked. These tests never
contact UNGM/DeepSeek, send an email, or change the repository's sent-ID file.
"""

from __future__ import annotations

import argparse
import io
import json
import smtplib
import sys
import tempfile
import unittest
import urllib.error
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import ungm_notify as notify
import ungm_watch as watch


TODAY = date(2026, 10, 13)
CLEAN_EMPTY_TEXT = "今天没有发现新的、符合筛选条件的 UNGM 货物采购机会。"
SMTP_ENV = {
    "SMTP_HOST": "smtp.invalid",
    "SMTP_PORT": "587",
    "SMTP_USER": "fixture-user",
    "SMTP_PASSWORD": "fixture-password",
    "MAIL_FROM": "sender@example.invalid",
    "MAIL_TO": "first@example.invalid, second@example.invalid",
    "DEEPSEEK_API_KEY": "fixture-api-key",
    "GITHUB_REPOSITORY": "fixture/ungm",
    "GITHUB_RUN_ID": "123",
}
CONFIG = watch.DeepSeekConfig("fixture-api-key", "fixture-model", "https://api.invalid", 1, 1)


def make_notice(notice_id: str = "100", *, keyword_match: bool = True) -> watch.Notice:
    return watch.Notice(
        notice_id=notice_id,
        title="Supply of school stationery kits" if keyword_match else "Supply of industrial turbines",
        organization="UNICEF",
        country="Kenya",
        published_raw="12-Oct-2026",
        deadline_raw="30-Oct-2026",
        opportunity_type="Invitation to bid",
        reference="RFQ fixture",
        url=f"https://www.ungm.org/Public/Notice/{notice_id}",
        description="Supply of school stationery kits" if keyword_match else "Industrial turbines",
    )


@contextmanager
def recording_health():
    health = watch.RunHealth()
    token = watch.RUN_HEALTH.set(health)
    try:
        yield health
    finally:
        watch.RUN_HEALTH.reset(token)


def response_bytes(payload: bytes) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = payload
    return response


def decision_response(match: bool) -> MagicMock:
    decision = {"match": match, "confidence": 0.9, "reason": "Fixture decision"}
    return response_bytes(json.dumps({"choices": [{"message": {"content": json.dumps(decision)}}]}).encode())


class OfflineFixtures:
    def setUp(self) -> None:
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict("os.environ", SMTP_ENV, clear=True))
        self.tempdir = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.marker = self.tempdir / ".ungm-alert-sent"
        self.stack.enter_context(patch.object(notify, "ALERT_MARKER", self.marker))
        self.stack.enter_context(patch.object(watch, "ALERT_MARKER", self.marker))
        # Fail closed if a test forgets a transport-specific fixture.
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Unexpected network access")))
        self.stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected provider HTTP")))
        self.stack.enter_context(patch("smtplib.SMTP", side_effect=AssertionError("Unexpected SMTP connection")))
        self.stack.enter_context(patch("smtplib.SMTP_SSL", side_effect=AssertionError("Unexpected SMTP SSL connection")))
        self.stack.enter_context(patch("playwright.async_api.async_playwright", side_effect=AssertionError("Unexpected browser launch")))


class EmailHealthTests(OfflineFixtures, unittest.TestCase):
    def test_healthy_empty_digest_keeps_normal_no_match_message(self) -> None:
        health = watch.RunHealth()
        with patch.object(watch, "send_message") as send:
            watch.send_email([], TODAY, health)
        subject, plain, rich = send.call_args.args
        self.assertFalse(health.degraded)
        self.assertNotIn("异常", subject)
        self.assertNotIn("运行失败", subject)
        self.assertIn(CLEAN_EMPTY_TEXT, plain)
        self.assertIn(CLEAN_EMPTY_TEXT, rich)

    def test_each_degraded_empty_digest_replaces_clean_no_match_claim(self) -> None:
        cases = [
            watch.RunHealth(ai_attempted=1, ai_failures={"HTTP 402（余额不足）": 1}, fallback_rejections=1),
            watch.RunHealth(fetch_issues={"列表抓取超时": 1}),
            watch.RunHealth(detail_failures={"100"}),
            watch.RunHealth(keyword_only=True),
            watch.RunHealth(fatal_stage="列表抓取"),
        ]
        for health in cases:
            with self.subTest(health=health), patch.object(watch, "send_message") as send:
                watch.send_email([], TODAY, health)
                subject, plain, rich = send.call_args.args
                self.assertTrue(health.degraded)
                self.assertTrue("异常提醒" in subject or "运行失败" in subject)
                for body in (plain, rich):
                    self.assertNotIn(CLEAN_EMPTY_TEXT, body)
                    self.assertIn("不能视为正常的无匹配结果", body)
                    self.assertIn("https://github.com/fixture/ungm/actions/runs/123", body)

    def test_degraded_digest_preserves_matches_and_escapes_summary_html(self) -> None:
        notice = make_notice()
        health = watch.RunHealth(fetch_issues={"<incomplete & delayed>": 1})
        with patch.object(watch, "send_message") as send:
            watch.send_email([notice], TODAY, health)
        subject, plain, rich = send.call_args.args
        self.assertIn("异常提醒", subject)
        self.assertIn(notice.title, plain)
        self.assertIn(notice.url, rich)
        self.assertIn("<incomplete & delayed>", plain)
        self.assertIn("&lt;incomplete &amp; delayed&gt;", rich)
        self.assertNotIn("<incomplete & delayed>", rich)

    def test_alert_summary_distinguishes_partial_and_total_ai_failure(self) -> None:
        for attempted, expected in ((2, "部分"), (1, "全部")):
            with self.subTest(attempted=attempted):
                health = watch.RunHealth(ai_attempted=attempted, ai_failures={"HTTP 402（余额不足）": 1})
                self.assertIn(f"AI 判断{expected}失败", health.summary())
                self.assertIn(f"1/{attempted}", health.summary())


class AiHealthTests(OfflineFixtures, unittest.IsolatedAsyncioTestCase):
    async def test_provider_failures_are_counted_and_keyword_fallback_remains_available(self) -> None:
        cases = [
            (urllib.error.HTTPError("https://api.invalid", 402, "secret-provider-body", {}, None), "HTTP 402"),
            (urllib.error.URLError("secret-network-address"), "网络请求失败"),
            (TimeoutError("secret-timeout-context"), "请求超时"),
            (urllib.error.URLError(TimeoutError("timed out")), "请求超时"),
            (response_bytes(b"not json: secret-provider-body"), "返回格式异常"),
            (response_bytes(b'{"choices": []}'), "返回格式异常"),
            (response_bytes(b'{"choices":[{"message":{"content":"not JSON"}}]}'), "返回格式异常"),
            (response_bytes(json.dumps({"choices": [{"message": {"content": '{"match": "yes"}'}}]}).encode()), "返回格式异常"),
        ]
        for outcome, expected in cases:
            with self.subTest(expected=expected, outcome=type(outcome).__name__), recording_health() as health:
                kwargs = {"side_effect": outcome} if isinstance(outcome, BaseException) else {"return_value": outcome}
                with patch("urllib.request.urlopen", **kwargs) as http:
                    matched = await watch.filter_notices_with_deepseek([make_notice()], TODAY, set(), CONFIG)
                self.assertEqual([item.notice_id for item in matched], ["100"])
                self.assertEqual(http.call_count, 1)
                self.assertEqual(health.ai_attempted, 1)
                self.assertEqual(sum(health.ai_failures.values()), 1)
                self.assertEqual(health.fallback_matches, 1)
                self.assertEqual(health.fallback_rejections, 0)
                self.assertIn(expected, health.summary())
                self.assertIn("全部失败", health.summary())
                self.assertNotIn("secret-", health.summary())
                self.assertNotIn(CONFIG.api_key, health.summary())

    async def test_partial_failure_combines_ai_result_and_fallback_counts(self) -> None:
        notices = [make_notice("100"), make_notice("101")]
        error = urllib.error.HTTPError("https://api.invalid", 402, "Payment required", {}, None)
        with recording_health() as health, patch("urllib.request.urlopen", side_effect=[decision_response(False), error]):
            matched = await watch.filter_notices_with_deepseek(notices, TODAY, set(), CONFIG)
        self.assertEqual([item.notice_id for item in matched], ["101"])
        self.assertEqual(health.ai_attempted, 2)
        self.assertEqual(sum(health.ai_failures.values()), 1)
        self.assertEqual(health.fallback_matches, 1)
        self.assertIn("部分失败：1/2", health.summary())

    async def test_all_failed_fallback_rejections_produce_abnormal_empty_digest(self) -> None:
        with recording_health() as health, patch("urllib.request.urlopen", side_effect=TimeoutError):
            matched = await watch.filter_notices_with_deepseek([make_notice(keyword_match=False)], TODAY, set(), CONFIG)
        self.assertEqual(matched, [])
        self.assertEqual(health.fallback_rejections, 1)
        self.assertEqual(health.fallback_matches, 0)
        with patch.object(watch, "send_message") as send:
            watch.send_email(matched, TODAY, health)
        self.assertIn("异常提醒", send.call_args.args[0])
        self.assertNotIn(CLEAN_EMPTY_TEXT, send.call_args.args[1])

    async def test_successful_ai_rejection_is_healthy(self) -> None:
        with recording_health() as health, patch("urllib.request.urlopen", return_value=decision_response(False)):
            matched = await watch.filter_notices_with_deepseek([make_notice()], TODAY, set(), CONFIG)
        self.assertEqual(matched, [])
        self.assertEqual(health.ai_attempted, 1)
        self.assertFalse(health.degraded)

    async def test_missing_api_key_is_reported_as_keyword_only(self) -> None:
        with recording_health() as health, patch.dict("os.environ", {}, clear=True):
            matched = await watch.filter_notices_for_business([make_notice()], TODAY, set())
        self.assertEqual(len(matched), 1)
        self.assertTrue(health.keyword_only)
        self.assertIn("仅使用关键词规则", health.summary())

    async def test_hard_filtered_notices_do_not_count_as_ai_failures(self) -> None:
        with recording_health() as health:
            matched = await watch.filter_notices_with_deepseek([make_notice()], TODAY, {"100"}, CONFIG)
        self.assertEqual(matched, [])
        self.assertEqual(health.ai_attempted, 0)
        self.assertFalse(health.degraded)


class BrowserHealthTests(OfflineFixtures, unittest.IsolatedAsyncioTestCase):
    async def test_table_timeout_is_recorded_even_if_extraction_can_continue(self) -> None:
        page = AsyncMock()
        page.wait_for_function.side_effect = PlaywrightTimeoutError("fixture table timeout")
        with recording_health() as health, patch.object(watch, "auto_scroll_page", new_callable=AsyncMock), patch.object(watch, "wait_for_rows_stable", new_callable=AsyncMock):
            await watch.wait_for_results(page)
        self.assertTrue(health.fetch_issues)
        self.assertIn("超时", health.summary())

    async def test_network_idle_timeout_alone_does_not_create_false_alarm(self) -> None:
        page = AsyncMock()
        page.wait_for_load_state.side_effect = PlaywrightTimeoutError("fixture background traffic")
        with recording_health() as health, patch.object(watch, "auto_scroll_page", new_callable=AsyncMock), patch.object(watch, "wait_for_rows_stable", new_callable=AsyncMock):
            await watch.wait_for_results(page)
        self.assertFalse(health.degraded)

    async def test_rows_distinguish_incomplete_unknown_and_explicit_empty(self) -> None:
        cases = [
            ({"unique_notice_count": 1, "result_total": 10, "result_displayed_to": 1}, True),
            ({"unique_notice_count": 0, "no_results": False}, True),
            ({"unique_notice_count": 0, "no_results": True}, False),
            ({"unique_notice_count": 2, "result_total": 2}, False),
        ]
        for stats, degraded in cases:
            with self.subTest(stats=stats), recording_health() as health, patch.object(watch, "PAGE_STABILITY_MAX_ATTEMPTS", 3), patch.object(watch, "PAGE_STABILITY_CHECKS", 2), patch.object(watch, "page_loading_stats", new_callable=AsyncMock, return_value=stats):
                await watch.wait_for_rows_stable(AsyncMock())
                self.assertEqual(health.degraded, degraded)

    @contextmanager
    def mocked_scraper(self, notices, stats, *, next_page=False):
        page = AsyncMock()
        page.goto.return_value = SimpleNamespace(status=200)
        context = AsyncMock()
        context.new_page.return_value = page
        browser = AsyncMock()
        browser.new_context.return_value = context
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=playwright)
        manager.__aexit__ = AsyncMock(return_value=False)
        with ExitStack() as stack:
            stack.enter_context(patch("playwright.async_api.async_playwright", return_value=manager))
            for name in ("wait_for_search_form", "dismiss_language_preference_modal", "wait_for_results", "save_page_debug_artifacts"):
                stack.enter_context(patch.object(watch, name, new_callable=AsyncMock))
            stack.enter_context(patch.object(watch, "apply_notice_search_filter", new_callable=AsyncMock, return_value=False))
            stack.enter_context(patch.object(watch, "extract_notices_from_page", new_callable=AsyncMock, return_value=notices))
            stack.enter_context(patch.object(watch, "page_loading_stats", new_callable=AsyncMock, return_value=stats))
            stack.enter_context(patch.object(watch, "click_next_page", new_callable=AsyncMock, return_value=next_page))
            yield page, context, browser

    async def test_scrape_empty_requires_explicit_no_results_evidence(self) -> None:
        for explicit_empty in (True, False):
            with self.subTest(explicit_empty=explicit_empty), recording_health() as health, self.mocked_scraper([], {"no_results": explicit_empty}) as (_, context, browser):
                result = await watch.scrape_notices(2, True, TODAY)
                self.assertEqual(result, [])
                self.assertEqual(health.degraded, not explicit_empty)
                context.close.assert_awaited_once()
                browser.close.assert_awaited_once()

    async def test_scrape_detects_reported_total_exceeding_extracted_count(self) -> None:
        with recording_health() as health, self.mocked_scraper([make_notice()], {"result_total": 5}):
            result = await watch.scrape_notices(2, True, TODAY)
        self.assertEqual(len(result), 1)
        self.assertTrue(health.fetch_issues)
        self.assertIn("少于", health.summary())

    async def test_scrape_page_limit_is_reported_as_incomplete(self) -> None:
        with recording_health() as health, self.mocked_scraper([make_notice()], {"result_total": 5}, next_page=True):
            await watch.scrape_notices(1, True, TODAY)
        self.assertIn("分页上限", health.summary())

    async def test_detail_network_http_and_empty_description_failures_are_recorded(self) -> None:
        for failure in ("timeout", "http", "empty"):
            with self.subTest(failure=failure):
                page = AsyncMock()
                page.goto.return_value = SimpleNamespace(status=503 if failure == "http" else 200)
                page.content.return_value = "<html><h1>Unusable detail page</h1></html>"
                if failure == "timeout":
                    page.goto.side_effect = PlaywrightTimeoutError("fixture detail timeout")
                browser = SimpleNamespace(new_page=AsyncMock(return_value=page))
                with recording_health() as health, patch.object(watch, "auto_scroll_page", new_callable=AsyncMock):
                    await watch.enrich_notice_detail(browser, make_notice())
                self.assertEqual(health.detail_failures, {"100"})
                page.close.assert_awaited_once()

    async def test_missing_detail_url_is_recorded_without_browser_access(self) -> None:
        notice = make_notice()
        notice.url = ""
        browser = SimpleNamespace(new_page=AsyncMock())
        with recording_health() as health:
            await watch.enrich_notice_detail(browser, notice)
        self.assertEqual(health.detail_failures, {"100"})
        browser.new_page.assert_not_awaited()


class RunPipelineTests(OfflineFixtures, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sent_file = self.tempdir / "sent_ids.json"
        self.original_state = '{"sent_ids": ["already-sent"]}\n'
        self.sent_file.write_text(self.original_state, encoding="utf-8")
        self.args = argparse.Namespace(timezone="UTC", sent_file=str(self.sent_file), max_pages=2, headful=False, dry_run=False)
        self.stack.enter_context(patch.object(watch, "today_in_timezone", return_value=TODAY))
        self.scrape = self.stack.enter_context(patch.object(watch, "scrape_notices", new_callable=AsyncMock, return_value=[]))
        self.enrich = self.stack.enter_context(patch.object(watch, "enrich_notices", new_callable=AsyncMock))
        self.classify = self.stack.enter_context(patch.object(watch, "filter_notices_for_business", new_callable=AsyncMock, return_value=[]))
        self.deliver = self.stack.enter_context(patch.object(watch, "send_message"))

    async def test_clean_no_match_run_sends_normal_digest_and_succeeds(self) -> None:
        self.assertEqual(await watch.run(self.args), 0)
        self.deliver.assert_called_once()
        self.assertIn(CLEAN_EMPTY_TEXT, self.deliver.call_args.args[1])
        self.assertFalse(self.marker.exists())
        self.assertEqual(watch.load_sent_ids(self.sent_file), {"already-sent"})
        self.assertIsNone(watch.RUN_HEALTH.get())

    async def test_degraded_run_sends_one_abnormal_digest_then_saves_accepted_ids(self) -> None:
        notice = make_notice()
        self.scrape.return_value = [notice]

        async def classify(*args):
            health = watch.RUN_HEALTH.get()
            health.ai_attempted = 1
            health.ai_failures["HTTP 402（余额不足）"] += 1
            health.fallback_matches = 1
            return [notice]

        self.classify.side_effect = classify

        def accept_delivery(*args):
            self.assertEqual(self.sent_file.read_text(), self.original_state)
            self.assertFalse(self.marker.exists())

        self.deliver.side_effect = accept_delivery
        self.assertEqual(await watch.run(self.args), 0)
        self.deliver.assert_called_once()
        self.assertIn("异常提醒", self.deliver.call_args.args[0])
        # A later workflow-step failure still needs its own fatal alert.
        self.assertFalse(self.marker.exists())
        self.assertEqual(watch.load_sent_ids(self.sent_file), {"already-sent", "100"})

    async def test_degraded_empty_run_is_successful_only_after_abnormal_delivery(self) -> None:
        async def scrape(**kwargs):
            watch.record_fetch_issue("列表未完整加载")
            return []

        self.scrape.side_effect = scrape
        self.assertEqual(await watch.run(self.args), 0)
        self.deliver.assert_called_once()
        self.assertIn("异常提醒", self.deliver.call_args.args[0])
        self.assertNotIn(CLEAN_EMPTY_TEXT, self.deliver.call_args.args[1])
        self.assertFalse(self.marker.exists())

    async def test_degraded_digest_does_not_suppress_later_state_save_failure_alert(self) -> None:
        async def classify(*args):
            watch.RUN_HEALTH.get().keyword_only = True
            return [make_notice()]

        self.classify.side_effect = classify
        self.deliver.side_effect = [None, smtplib.SMTPException("fixture fatal alert failure")]
        with patch.object(watch, "save_sent_ids", side_effect=OSError("fixture state save failure")):
            self.assertEqual(await watch.run(self.args), 1)
        self.assertEqual(self.deliver.call_count, 2)
        self.assertIn("异常提醒", self.deliver.call_args_list[0].args[0])
        self.assertIn("运行失败", self.deliver.call_args_list[1].args[0])
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.sent_file.read_text(), self.original_state)
        with patch.object(notify, "send_message") as fallback, redirect_stdout(io.StringIO()):
            self.assertEqual(notify.notify_workflow_failure(), 0)
        fallback.assert_called_once()
        self.assertTrue(self.marker.exists())

    async def test_top_level_scrape_enrichment_and_classification_failures_alert_and_fail(self) -> None:
        for failing_stage in (self.scrape, self.enrich, self.classify):
            with self.subTest(stage=failing_stage._mock_name):
                self.deliver.reset_mock()
                self.marker.unlink(missing_ok=True)
                failing_stage.side_effect = RuntimeError("secret-sensitive-error-context")
                self.assertEqual(await watch.run(self.args), 1)
                failing_stage.side_effect = None
                self.deliver.assert_called_once()
                subject, plain, rich = self.deliver.call_args.args
                self.assertIn("运行失败", subject)
                self.assertIn("运行失败阶段", plain)
                self.assertNotIn("secret-sensitive-error-context", plain + rich)
                self.assertNotIn(CLEAN_EMPTY_TEXT, plain + rich)
                self.assertTrue(self.marker.exists())
                self.assertEqual(self.sent_file.read_text(), self.original_state)
                self.assertIsNone(watch.RUN_HEALTH.get())

    async def test_delivery_failure_leaves_ids_untouched_and_reports_failure(self) -> None:
        notice = make_notice()
        self.scrape.return_value = [notice]
        self.classify.return_value = [notice]
        self.deliver.side_effect = smtplib.SMTPException("fixture SMTP failure")
        self.assertEqual(await watch.run(self.args), 1)
        self.assertGreaterEqual(self.deliver.call_count, 1)
        self.assertEqual(self.sent_file.read_text(), self.original_state)
        self.assertFalse(self.marker.exists())

    async def test_failed_digest_then_accepted_failure_alert_does_not_mark_notices_sent(self) -> None:
        self.classify.return_value = [make_notice()]
        self.deliver.side_effect = [smtplib.SMTPException("fixture digest rejected"), None]
        self.assertEqual(await watch.run(self.args), 1)
        self.assertEqual(self.deliver.call_count, 2)
        self.assertIn("运行失败", self.deliver.call_args.args[0])
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.sent_file.read_text(), self.original_state)

    async def test_partial_smtp_refusal_leaves_sent_ids_untouched(self) -> None:
        self.classify.return_value = [make_notice()]
        self.deliver.side_effect = notify.send_message
        with patch.object(smtplib, "SMTP") as connect:
            connect.return_value.__enter__.return_value.send_message.return_value = {
                "second@example.invalid": (550, b"fixture refused")
            }
            self.assertEqual(await watch.run(self.args), 1)
        self.assertEqual(self.sent_file.read_text(), self.original_state)
        self.assertFalse(self.marker.exists())

    async def test_dry_run_sends_nothing_and_preserves_state_and_marker(self) -> None:
        self.args.dry_run = True
        self.classify.return_value = [make_notice()]
        self.marker.write_text("existing marker\n")
        self.assertEqual(await watch.run(self.args), 0)
        self.deliver.assert_not_called()
        self.assertEqual(self.sent_file.read_text(), self.original_state)
        self.assertEqual(self.marker.read_text(), "existing marker\n")

    async def test_dry_run_failure_sends_no_failure_email(self) -> None:
        self.args.dry_run = True
        self.scrape.side_effect = RuntimeError("fixture scrape failure")
        self.assertEqual(await watch.run(self.args), 1)
        self.deliver.assert_not_called()
        self.assertEqual(self.sent_file.read_text(), self.original_state)

    async def test_dry_run_does_not_create_missing_state_file(self) -> None:
        self.args.dry_run = True
        self.sent_file.unlink()
        self.assertEqual(await watch.run(self.args), 0)
        self.assertFalse(self.sent_file.exists())
        self.deliver.assert_not_called()

    async def test_context_is_restored_after_success_and_failure(self) -> None:
        with recording_health() as outer:
            self.assertEqual(await watch.run(self.args), 0)
            self.assertIs(watch.RUN_HEALTH.get(), outer)
            self.scrape.side_effect = RuntimeError("fixture scrape failure")
            self.assertEqual(await watch.run(self.args), 1)
            self.assertIs(watch.RUN_HEALTH.get(), outer)
            self.assertFalse(outer.degraded)


class SmtpAndWorkflowTests(OfflineFixtures, unittest.TestCase):
    def test_existing_smtp_settings_support_starttls_and_ssl(self) -> None:
        for port, transport in ((587, "SMTP"), (465, "SMTP_SSL")):
            with self.subTest(port=port), patch.dict("os.environ", {"SMTP_PORT": str(port)}), patch.object(smtplib, transport) as connect:
                server = connect.return_value.__enter__.return_value
                server.send_message.return_value = {}
                notify.send_message("Fixture subject", "Fixture plain text", "<p>Fixture HTML</p>")
                connect.assert_called_once_with("smtp.invalid", port, timeout=30)
                server.login.assert_called_once_with("fixture-user", "fixture-password")
                message = server.send_message.call_args.args[0]
                self.assertEqual(message["To"], "first@example.invalid, second@example.invalid")
                self.assertEqual(message["Subject"], "Fixture subject")
                self.assertIn("Fixture plain text", message.get_body(preferencelist=("plain",)).get_content())
                self.assertIn("Fixture HTML", message.get_body(preferencelist=("html",)).get_content())
                if port == 587:
                    server.starttls.assert_called_once()
                    self.assertEqual(server.ehlo.call_count, 2)
                else:
                    server.starttls.assert_not_called()

    def test_partial_recipient_refusal_is_a_delivery_failure(self) -> None:
        with patch.object(smtplib, "SMTP") as connect:
            server = connect.return_value.__enter__.return_value
            server.send_message.return_value = {"second@example.invalid": (550, b"fixture refused")}
            with self.assertRaises(smtplib.SMTPRecipientsRefused):
                notify.send_message("Fixture subject", "Fixture", "<p>Fixture</p>")

    def test_invalid_smtp_configuration_fails_before_connecting(self) -> None:
        cases = ({"SMTP_PORT": "invalid"}, {"MAIL_TO": " , "})
        for env in cases:
            with self.subTest(env=env), patch.dict("os.environ", env), self.assertRaises(RuntimeError):
                notify.send_message("Fixture", "Fixture", "Fixture")
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(RuntimeError):
            notify.send_message("Fixture", "Fixture", "Fixture")

    def test_workflow_failure_fallback_sends_once_then_suppresses_duplicate(self) -> None:
        with patch.object(notify, "send_message") as send, redirect_stdout(io.StringIO()):
            self.assertEqual(notify.notify_workflow_failure(), 0)
            self.assertTrue(self.marker.exists())
            self.assertEqual(notify.notify_workflow_failure(), 0)
        send.assert_called_once()
        subject, plain, rich = send.call_args.args
        self.assertIn("运行失败", subject)
        self.assertIn("https://github.com/fixture/ungm/actions/runs/123", plain)
        self.assertIn("最近 3 天", plain)
        self.assertNotIn(CLEAN_EMPTY_TEXT, plain + rich)

    def test_workflow_failure_delivery_failure_keeps_retry_possible_without_secret_leak(self) -> None:
        output = io.StringIO()
        with patch.object(notify, "send_message", side_effect=RuntimeError("secret-password-and-provider-data")) as send, redirect_stdout(output):
            self.assertEqual(notify.notify_workflow_failure(), 1)
        send.assert_called_once()
        self.assertFalse(self.marker.exists())
        self.assertIn("::error::", output.getvalue())
        self.assertNotIn("secret-password-and-provider-data", output.getvalue())

    def test_workflow_notifier_has_no_third_party_import_dependencies(self) -> None:
        # A dependency-install failure must still leave this fallback importable.
        import ast

        tree = ast.parse(Path(notify.__file__).read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append((node.module or "").split(".")[0])
        self.assertLessEqual(set(imports), sys.stdlib_module_names)


if __name__ == "__main__":
    unittest.main()
