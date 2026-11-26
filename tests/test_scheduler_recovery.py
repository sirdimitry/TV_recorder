from datetime import datetime, timedelta
import threading
import unittest
from unittest.mock import Mock, patch

from core.scheduler import RecordingScheduler


class SchedulerRecoveryTests(unittest.TestCase):
    now = datetime(2026, 10, 23, 9, 10)

    def make_scheduler(self):
        with patch('core.scheduler.Storage'), patch('core.scheduler.Notifier'):
            scheduler = RecordingScheduler(recorder=Mock())
        scheduler._running = True
        scheduler._pre_record_check = Mock(return_value=True)
        self.item = dict(channel_name='Звезда', source_id='zvezda', source_type='channel',
                         start_time='09:00:00', end_time='09:27:00', days=[4], enabled=True)
        self.target = dict(name='Звезда', id='zvezda', url='https://example.test/live.m3u8')
        scheduler._entries = [(self.target, self.item, 0)]
        return scheduler

    def run_slot(self, scheduler, now=None, index=0):
        scheduler._run_scheduled_recording(self.target, self.item, index, now=now or self.now)

    def test_wake_inside_window_recovers_once_and_keeps_original_end(self):
        s = self.make_scheduler()
        done = threading.Event()
        s._pre_record_check.side_effect = lambda *a, **kw: (done.set() or True)
        s.check_schedules(self.now)
        self.assertTrue(done.wait(2))
        self.run_slot(s)  # cron waking concurrently must not duplicate recovery
        s.check_schedules(self.now + timedelta(seconds=5))
        s._pre_record_check.assert_called_once_with(
            self.target, self.item, 0, deadline=datetime(2026, 10, 23, 9, 27))

    def test_cron_and_watchdog_cannot_duplicate_inflight_start(self):
        s = self.make_scheduler()
        entered, release = threading.Event(), threading.Event()

        def blocked(*args, **kwargs):
            entered.set()
            release.wait(2)
            return True

        s._pre_record_check.side_effect = blocked
        worker = threading.Thread(target=lambda: self.run_slot(s))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.run_slot(s)
            s._pre_record_check.assert_called_once()
        finally:
            release.set()
            worker.join(2)

    def test_reload_and_row_reordering_do_not_restart_same_occurrence(self):
        s = self.make_scheduler()
        self.run_slot(s)
        s._entries = [(dict(self.target), dict(self.item), 3)]
        self.run_slot(s, index=3)
        s._pre_record_check.assert_called_once()
        self.run_slot(s, self.now + timedelta(days=7), index=3)
        self.assertEqual(s._pre_record_check.call_count, 2)

    def test_wrong_day_disabled_before_start_and_expired_are_ignored(self):
        s = self.make_scheduler()
        for now in (self.now - timedelta(days=1), self.now.replace(hour=8),
                    self.now.replace(minute=27), self.now.replace(hour=10)):
            self.run_slot(s, now)
        self.item['enabled'] = False
        self.run_slot(s)
        s._pre_record_check.assert_not_called()

    def test_specific_date_does_not_repeat_next_week(self):
        s = self.make_scheduler()
        self.item['recording_date'] = '2026-10-23'
        self.run_slot(s, self.now + timedelta(days=7))
        self.run_slot(s)
        s._pre_record_check.assert_called_once()

    def test_overnight_recovery_uses_previous_days_start_and_todays_end(self):
        s = self.make_scheduler()
        self.item.update(start_time='23:50:00', end_time='00:20:00')
        self.run_slot(s, datetime(2026, 10, 24, 0, 5))
        s._pre_record_check.assert_called_once_with(
            self.target, self.item, 0, deadline=datetime(2026, 10, 24, 0, 20))
        self.run_slot(s, datetime(2026, 10, 24, 0, 20))
        self.assertEqual(s._pre_record_check.call_count, 1)

    def test_failed_start_retries_after_thirty_seconds_within_window(self):
        s = self.make_scheduler()
        s._pre_record_check.side_effect = [False, True]
        with patch('core.scheduler.datetime') as clock:
            clock.now.return_value = self.now
            self.run_slot(s)
        self.run_slot(s, self.now + timedelta(seconds=5))
        self.assertEqual(s._pre_record_check.call_count, 1)
        self.run_slot(s, self.now + timedelta(seconds=30))
        self.run_slot(s, self.now + timedelta(seconds=35))
        self.assertEqual(s._pre_record_check.call_count, 2)

    def test_shutdown_and_deleted_entries_do_not_start(self):
        s = self.make_scheduler()
        s._entries = []
        self.run_slot(s)
        s._entries = [(self.target, self.item, 0)]
        s._running = False
        self.run_slot(s)
        s._pre_record_check.assert_not_called()

    def test_interrupted_recording_restarts_but_manual_stop_does_not(self):
        s = self.make_scheduler()
        self.run_slot(s)
        with patch('core.scheduler.datetime') as clock:
            clock.now.return_value = self.now
            s._on_recording_complete(True, 'Звезда', '/tmp/clip.mp4',
                                     schedule_item=self.item, index=0)
            self.run_slot(s, self.now + timedelta(seconds=30))
            s._pre_record_check.assert_called_once()
            s._on_recording_complete(False, 'Звезда', '/tmp/clip.mp4',
                                     schedule_item=self.item, index=0)
        self.run_slot(s, self.now + timedelta(seconds=30))
        self.assertEqual(s._pre_record_check.call_count, 2)

    def test_resume_after_stop_time_stops_previous_recording_without_restart(self):
        s = self.make_scheduler()
        s._deadlines['existing'] = self.now.replace(minute=27)
        s.check_schedules(self.now.replace(hour=10))
        s.recorder.stop_recording.assert_called_once_with('existing')
        s._pre_record_check.assert_not_called()
        s.check_schedules(self.now.replace(hour=10))
        s.recorder.stop_recording.assert_called_once()

    def test_real_precheck_starts_remaining_portion_with_original_deadline(self):
        s = self.make_scheduler()
        s._pre_record_check = RecordingScheduler._pre_record_check.__get__(s)
        s.recorder.channel_headers.return_value = {}
        s.recorder.start_recording.return_value = 'recording-id'
        with patch('core.scheduler.datetime') as clock:
            clock.now.return_value = self.now
            self.run_slot(s)
        kwargs = s.recorder.start_recording.call_args.kwargs
        self.assertEqual(kwargs['start_deadline_timestamp'],
                         self.now.replace(minute=27).timestamp())
        self.assertEqual(kwargs['expected_duration_seconds'], 27 * 60)
        s.recorder.schedule_stop.assert_called_once_with('recording-id', 17 * 60)
        self.assertEqual(s._deadlines['recording-id'], self.now.replace(minute=27))


if __name__ == '__main__':
    unittest.main()
