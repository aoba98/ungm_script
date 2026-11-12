"""Offline listing regressions: no HTTP, browser, AI or SMTP."""
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import ungm_watch as w

class ListLoading(unittest.IsolatedAsyncioTestCase):
    async def test_old_rows_do_not_confirm_search(self):
        stats = dict(row_count=30, unique_notice_count=30, result_displayed_to=30,
                     result_total=83, first_notice_id='1', last_notice_id='30')
        page = AsyncMock()
        with patch.object(w,'page_loading_stats',AsyncMock(return_value=stats)), patch.object(w,'record_fetch_issue') as issue, patch.object(w,'save_page_debug_artifacts',AsyncMock()):
            self.assertFalse(await w.wait_for_search_change(page,stats))
            self.assertEqual(page.wait_for_timeout.await_count,30)
            issue.assert_called_once()

    async def test_delayed_changed_results_confirm_search(self):
        old = dict(row_count=30, unique_notice_count=30, result_displayed_to=30,
                   result_total=83, first_notice_id='1', last_notice_id='30')
        new = dict(old, first_notice_id='101', last_notice_id='130')
        page=AsyncMock()
        with patch.object(w,'page_loading_stats',AsyncMock(side_effect=[old,old,new])):
            self.assertTrue(await w.wait_for_search_change(page,old))
            self.assertEqual(page.wait_for_timeout.await_count,2)

    async def test_changed_empty_results_confirm_search(self):
        with patch.object(w,'page_loading_stats',AsyncMock(return_value={'no_results':True})):
            self.assertTrue(await w.wait_for_search_change(AsyncMock(),{}))

    async def test_stalled_list_recovers_once_and_retains_ids(self):
        from test_run_health import BrowserHealthTests, make_notice, TODAY
        with BrowserHealthTests().mocked_scraper([make_notice()],{'result_total':3,'result_displayed_to':1}) as (page,_,_):
            page.reload.return_value=SimpleNamespace(status=200)
            result=await w.scrape_notices(4,True,TODAY)
            page.reload.assert_awaited_once()
            self.assertEqual(len(result),1)

    async def test_failed_recovery_keeps_partial_results(self):
        from test_run_health import BrowserHealthTests, make_notice, TODAY, recording_health
        from playwright.async_api import Error
        for error in (Error('connection reset'),None):
            with BrowserHealthTests().mocked_scraper([make_notice()],{'result_total':3,'result_displayed_to':1}) as (page,_,_), recording_health() as health:
                page.reload.side_effect=error
                page.reload.return_value=SimpleNamespace(status=503)
                self.assertEqual(len(await w.scrape_notices(4,True,TODAY)),1)
                self.assertTrue(health.fetch_issues)

    def test_real_candidate_js_excludes_calendar_hidden_and_dialog_next(self):
        js='''const make=(id,cls='',width=10,dialog=false)=>({id,className:cls,getBoundingClientRect:()=>({width,height:10}),closest:()=>dialog?{}:null});
const elements=[make('calendar','ui-datepicker-next'),make('hidden','',0),make('modal','',10,true),make('pager')];
global.document={querySelectorAll:()=>elements};global.window={getComputedStyle:()=>({display:'block',visibility:'visible'})};
console.log(JSON.stringify(('''+w.NOTICE_NEXT_CANDIDATES_JS+''')().map(e=>e.id)));'''
        result=subprocess.run(['node','-e',js],text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(result.stdout),['pager'])

    async def test_actual_stats_and_click_js_parse(self):
        page=AsyncMock();page.evaluate.return_value={'clicked':False}
        with patch.object(w,'page_loading_stats',AsyncMock(return_value={})),patch.object(w,'save_page_debug_artifacts',AsyncMock()):
            self.assertFalse(await w.click_next_page(page))
        click_js=page.evaluate.call_args.args[0]
        page=AsyncMock();await w.page_loading_stats(page)
        for source in (click_js,page.evaluate.call_args.args[0]):
            subprocess.run(['node','-e','new Function("return (" + '+json.dumps(source)+' + ")");'],check=True,capture_output=True)
