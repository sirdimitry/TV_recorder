import unittest
from datetime import datetime
from unittest.mock import Mock

from core.scheduler import RecordingScheduler
from utils.timecode import format_clip_time, format_clock_time, parse_clip_time, parse_clock_time


class TimecodeTests(unittest.TestCase):
    def test_clip_hours_and_old_minutes_format(self):
        self.assertEqual(parse_clip_time('01:23:45'), 5025)
        self.assertEqual(parse_clip_time('83:45'), 5025)
        self.assertEqual(format_clip_time(5025), '01:23:45')
        self.assertIsNone(parse_clip_time('01:60:00'))

    def test_clock_seconds_and_old_schedule(self):
        self.assertEqual(parse_clock_time('09:30:15'), (9, 30, 15))
        self.assertEqual(parse_clock_time('09:30'), (9, 30, 0))
        self.assertEqual(format_clock_time('09:30'), '09:30:00')
        self.assertIsNone(parse_clock_time('25:00:00'))

    def test_scheduler_keeps_seconds_in_trigger_and_deadline(self):
        scheduler = RecordingScheduler.__new__(RecordingScheduler)
        scheduler.scheduler = Mock()
        scheduler._pre_record_check = Mock()
        item = {'start_time': '23:59:45', 'end_time': '00:00:20'}
        scheduler._add_job(0, item, {'name': 'Канал'})
        trigger = scheduler.scheduler.add_job.call_args.kwargs['trigger']
        self.assertIn("second='45'", str(trigger))
        deadline = scheduler._end_deadline(item, datetime(2026, 9, 28, 23, 59, 50))
        self.assertEqual(deadline, datetime(2026, 9, 29, 0, 0, 20))


if __name__ == '__main__':
    unittest.main()
