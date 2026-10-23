from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import ungm_watch as watch


def response(status=200, retry_after='', body='<h1>Supply</h1> Description School kits Contacts'):
    return SimpleNamespace(status=status, headers={'retry-after': retry_after},
                           text=AsyncMock(return_value=body), dispose=AsyncMock())


def notice(notice_id='100'):
    return watch.Notice(notice_id, 'Supply', '', '', '', '', '', '',
                        'https://www.ungm.org/Public/Notice/' + notice_id)


class DetailFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_429_recovery_honors_retry_after_and_disposes_responses(self):
        limited, ok = response(429, '90'), response()
        context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(side_effect=[limited, ok])))
        with patch.object(watch.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            self.assertIn('School kits', await watch.fetch_detail_html(context, notice().url))
        sleep.assert_awaited_once_with(90)
        self.assertEqual(context.request.get.await_count, 2)
        limited.dispose.assert_awaited_once()
        ok.dispose.assert_awaited_once()

    async def test_long_retry_after_stops_without_retrying_early(self):
        limited = response(429, '3600')
        context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=limited)))
        with patch.object(watch.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(watch.NoticeRateLimitError):
                await watch.fetch_detail_html(context, notice().url)
        sleep.assert_not_awaited()
        context.request.get.assert_awaited_once()
        limited.dispose.assert_awaited_once()

    async def test_other_http_errors_are_not_retried(self):
        forbidden = response(403)
        context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=forbidden)))
        with patch.object(watch.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
                await watch.fetch_detail_html(context, notice().url)
        sleep.assert_not_awaited()
        forbidden.dispose.assert_awaited_once()

    async def test_sequential_batch_shares_context_and_stops_on_persistent_limit(self):
        for limited in (False, True):
            with self.subTest(limited=limited):
                result = response(429 if limited else 200)
                context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=result)), close=AsyncMock())
                browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
                launch = AsyncMock(return_value=browser)
                manager = MagicMock()
                manager.__aenter__ = AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch=launch)))
                manager.__aexit__ = AsyncMock(return_value=False)
                notices = [notice('100'), notice('101')]
                with patch('playwright.async_api.async_playwright', return_value=manager), patch.object(watch.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
                    if limited:
                        with self.assertRaises(watch.NoticeRateLimitError):
                            await watch.enrich_notices(notices, headless=False)
                        self.assertEqual(context.request.get.await_count, 3)
                        self.assertTrue(all(c.args[0] == notices[0].url for c in context.request.get.call_args_list))
                        self.assertEqual(sleep.await_args_list, [call(60), call(60)])
                    else:
                        await watch.enrich_notices(notices, headless=False)
                        self.assertEqual([n.description for n in notices], ['School kits', 'School kits'])
                        sleep.assert_awaited_once_with(watch.DETAIL_REQUEST_INTERVAL_SECONDS)
                launch.assert_awaited_once_with(headless=False)
                browser.new_context.assert_awaited_once()
                context.close.assert_awaited_once()
                browser.close.assert_awaited_once()

    async def test_explicitly_empty_published_description_is_not_a_fetch_failure(self):
        body = '<h1>Supply</h1><div class="ungm-list-item"><div class="title">Description</div><div><p><br></p></div></div>'
        context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response(body=body))))
        health = watch.RunHealth()
        token = watch.RUN_HEALTH.set(health)
        try:
            item = notice()
            await watch.enrich_notice_detail(context, item)
            self.assertEqual(item.description, '')
            self.assertFalse(health.detail_failures)
        finally:
            watch.RUN_HEALTH.reset(token)


class DetailParsingTests(unittest.TestCase):
    def test_retry_after_supports_http_dates_and_invalid_headers(self):
        now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        self.assertEqual(watch.retry_after_seconds('Fri, 23 Oct 2026 12:02:00 GMT', now), 120)
        for value in ('', 'invalid', '-1', '0', 'Fri, 23 Oct 2026 11:00:00 GMT'):
            self.assertEqual(watch.retry_after_seconds(value, now), 60)

    def test_description_section_does_not_truncate_words_matching_other_tab_names(self):
        body = '<h1>Supply</h1><div class="ungm-list-item"><div class="title">Description</div><div>Documents include school kits. Contacts are listed below.</div></div><div>Contacts unrelated footer</div>'
        description, _, title = watch.parse_detail_html(body)
        self.assertEqual(description, 'Documents include school kits. Contacts are listed below.')
        self.assertEqual(title, 'Supply')
