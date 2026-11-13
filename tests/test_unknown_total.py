import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import ungm_watch as w
from test_run_health import recording_health, make_notice, TODAY
import test_run_health as fixtures

class UnknownTotal(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_total_does_not_mean_complete(self):
        stats={'row_count':15,'unique_notice_count':15,'result_total':0,'result_displayed_to':0}
        with recording_health() as health, patch.object(w,'page_loading_stats',AsyncMock(return_value=stats)), patch.object(w,'PAGE_STABILITY_MAX_ATTEMPTS',3):
            await w.wait_for_rows_stable(AsyncMock())
            self.assertTrue(health.fetch_issues)

    async def test_explicit_empty_still_completes(self):
        stats={'no_results':True,'result_total':0}
        with recording_health() as health, patch.object(w,'page_loading_stats',AsyncMock(return_value=stats)):
            await w.wait_for_rows_stable(AsyncMock())
            self.assertFalse(health.fetch_issues)

    async def test_unknown_total_recovers_once_and_warns(self):
        stats={'row_count':15,'unique_notice_count':15,'result_total':0,'result_displayed_to':0}
        with fixtures.BrowserHealthTests().mocked_scraper([make_notice()],stats) as (page,_,_), recording_health() as health:
            page.reload.return_value=SimpleNamespace(status=200)
            result=await w.scrape_notices(4,True,TODAY)
            page.reload.assert_awaited_once()
            self.assertEqual(len(result),1)
            self.assertIn('列表总数未能确认',health.summary())

    async def test_unknown_total_warns_without_recovery_budget(self):
        with fixtures.BrowserHealthTests().mocked_scraper([make_notice()],{'result_total':0}) as (page,_,_), recording_health() as health:
            await w.scrape_notices(1,True,TODAY)
            page.reload.assert_not_awaited()
            self.assertIn('列表总数未能确认',health.summary())

    def test_counter_parser_and_fallback(self):
        cases=[({'#searchCountNo':'15','#noticeSearchTotal':'1,234'},'',{'from':1,'to':15,'total':1234}),({},'Displaying results 1 to 15 of 247',{'from':1,'to':15,'total':247}),({},'Loading...',{'from':0,'to':0,'total':0}),({'#searchCountNo':'15','#noticeSearchTotal':''},'',{'from':0,'to':0,'total':0})]
        for counters,summary,expected in cases:
            js='const c='+json.dumps(counters)+'; const s='+json.dumps(summary)+'; global.document={querySelectorAll:()=>[],querySelector:k=>k==="#noticesTotal"?{innerText:s}:k in c?{textContent:c[k]}:null};console.log(JSON.stringify(('+w.NOTICE_RESULT_SUMMARY_JS+')()));'
            r=subprocess.run(['node','-e',js],check=True,text=True,capture_output=True)
            self.assertEqual(json.loads(r.stdout),expected)
