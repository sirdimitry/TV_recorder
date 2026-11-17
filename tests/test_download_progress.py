from types import SimpleNamespace
import unittest
from core.download_progress import configure_streams, update_progress


class AggregateProgressTests(unittest.TestCase):
    def task(self):
        return SimpleNamespace(stream_progress={}, speed_bps=None)

    def test_video_progress_includes_audio_yet_to_download(self):
        task = self.task()
        configure_streams(task, [{'id': 'v', 'total': 100}, {'id': 'a', 'total': 20}])
        update_progress(task, dict(format_id='v', downloaded=50, total=100, speed=10))
        self.assertAlmostEqual(task.progress, 100*50/120)
        self.assertEqual(task.eta_seconds, 7)
        update_progress(task, dict(format_id='v', downloaded=100, finished=True))
        self.assertLess(task.progress, 84)
        update_progress(task, dict(format_id='a', downloaded=10, total=20, speed=5))
        self.assertEqual(task.downloaded_bytes, 110)
        self.assertEqual(task.total_bytes, 120)
        self.assertEqual(task.eta_seconds, 2)

    def test_resume_keeps_previous_stream_bytes_but_accepts_server_restart(self):
        task = self.task()
        streams = [{'id': 'v', 'total': 100}, {'id': 'a', 'total': 20}]
        configure_streams(task, streams)
        update_progress(task, dict(format_id='v', downloaded=60))
        configure_streams(task, streams)
        self.assertEqual(task.downloaded_bytes, 60)
        update_progress(task, dict(format_id='v', downloaded=10))
        self.assertEqual(task.downloaded_bytes, 10)

    def test_unknown_total_does_not_invent_percent_or_eta(self):
        task = self.task()
        configure_streams(task, [{'id': 'v', 'total': 100}, {'id': 'a'}])
        update_progress(task, dict(format_id='v', downloaded=50, speed=10))
        self.assertIsNone(task.progress)
        self.assertIsNone(task.total_bytes)
        self.assertIsNone(task.eta_seconds)

    def test_estimates_are_marked_and_completion_waits_for_validation(self):
        task = self.task()
        configure_streams(task, [{'id': 'v', 'total': 100, 'estimated': True}])
        self.assertTrue(task.total_is_estimate)
        update_progress(task, dict(format_id='v', downloaded=120, finished=True))
        self.assertEqual(task.total_bytes, 120)
        self.assertFalse(task.total_is_estimate)
        self.assertEqual(task.progress, 99)
