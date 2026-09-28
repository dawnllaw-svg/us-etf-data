import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pandas as pd

import download
from release_data import newest, validate_files, publish
from update_cn_stock_monthly import merge_preserving, recent_quarters, bs_frame


class PipelineTests(unittest.TestCase):
    def test_intraday_candle_excluded_until_market_close(self):
        frame = pd.DataFrame({'A': [1, 2]}, index=pd.to_datetime(['2026-09-25', '2026-09-28']))
        china = download.completed_sessions(frame, 'Asia/Shanghai', 15, '2026-09-28T04:00:00Z')
        self.assertEqual(len(china), 1)
        us = download.completed_sessions(frame, 'America/New_York', 16, '2026-09-28T22:30:00Z')
        self.assertEqual(len(us), 2)

    def test_restatement_does_not_erase_balance_sheet(self):
        old = pd.DataFrame([dict(code6='000001', report_date='20260331', debt_ratio=50, eps=1)])
        new = pd.DataFrame([dict(code6='000001', report_date='20260331', eps=2)])
        result = merge_preserving(old, new, ['code6', 'report_date'])
        self.assertEqual(result.iloc[0].debt_ratio, 50)
        self.assertEqual(result.iloc[0].eps, 2)
        self.assertEqual(len(result), 1)

    def test_reporting_window_does_not_require_unreported_quarter(self):
        self.assertEqual(recent_quarters('2026-10-01')[-1], '20260630')
        self.assertEqual(recent_quarters('2026-11-01')[-1], '20260930')
        self.assertEqual(recent_quarters('2026-04-01')[-1], '20250930')

    def test_release_selection_ignores_other_types_and_drafts(self):
        releases = [dict(tag_name=f'cn-cb-{i}', published_at='2026-09-28') for i in range(50)]
        releases += [dict(tag_name='cn-stock-old', published_at='2026-08-13'),
                     dict(tag_name='cn-stock-draft', published_at='2026-09-28', draft=True)]
        self.assertEqual(newest(releases, 'cn-stock-')['tag_name'], 'cn-stock-old')

    def test_truncated_gzip_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data.csv.gz'
            path.write_bytes(gzip.compress(b'code,value\n000001,1\n')[:-5])
            with self.assertRaises((EOFError, OSError)):
                validate_files([path])

    def test_failed_upload_leaves_draft_unpublished(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'data.csv').write_text('x\n1\n')
            with patch.dict('os.environ', GITHUB_REPOSITORY='owner/repo'):
                with patch('release_data.gh', side_effect=['', RuntimeError('upload failure')]) as gh:
                    with self.assertRaises(RuntimeError):
                        publish('tag', Path(folder), '*')
                    self.assertEqual(gh.call_count, 2)
                    self.assertIn('--draft', gh.call_args_list[0].args)

    def test_missing_download_column_preserves_history_and_reports_date(self):
        with tempfile.TemporaryDirectory() as folder:
            filename = str(Path(folder) / 'etf.csv')
            idx = pd.date_range('2026-09-20', periods=3)
            pd.DataFrame({'A': [1., 2., 3.], 'B': [4., 5., 6.]}, index=idx).to_csv(filename)
            new = pd.DataFrame({'A': [2., 3., 4.]}, index=idx)
            q = download.merge_and_save(new, filename, min_rows=2, expected=['A', 'B', 'C'], today='2026-09-28')
            result = pd.read_csv(filename)
            self.assertEqual(result.B.tolist(), [4., 5., 6.])
            self.assertEqual(q['columns']['B']['latest_date'], '2026-09-22')
            self.assertIsNone(q['columns']['C']['latest_date'])
            self.assertTrue(q['warnings'])

    def test_historical_universe_provider_error_is_fatal(self):
        class Result:
            error_code = '1'
            error_msg = 'unavailable'
        with self.assertRaises(RuntimeError):
            bs_frame(Result())


if __name__ == '__main__':
    unittest.main()
