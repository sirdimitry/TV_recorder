from datetime import datetime
import unittest
from unittest.mock import Mock

from core.scheduler import RecordingScheduler


class SchedulerDateTests(unittest.TestCase):
    def trigger_for(self, item):
        scheduler = object.__new__(RecordingScheduler)
        scheduler.scheduler = Mock()
        scheduler._add_job(0, item, {'name': 'ОТР'})
        return scheduler.scheduler.add_job.call_args.kwargs['trigger']

    def test_calendar_date_runs_once_at_requested_time(self):
        trigger = self.trigger_for({'start_time': '20:00:00', 'days': [4],
                                    'recording_date': '2026-10-16'})
        now = datetime.fromisoformat('2026-10-16T19:00:00+03:00')
        fire = trigger.get_next_fire_time(None, now)
        self.assertEqual(fire.isoformat(), '2026-10-16T20:00:00+03:00')
        self.assertIsNone(trigger.get_next_fire_time(fire, fire))

    def test_existing_weekly_schedule_still_repeats(self):
        trigger = self.trigger_for({'start_time': '20:00:00', 'days': [4]})
        now = datetime.fromisoformat('2026-10-16T19:00:00+03:00')
        fire = trigger.get_next_fire_time(None, now)
        self.assertEqual(trigger.get_next_fire_time(fire, fire).isoformat(),
                         '2026-10-23T20:00:00+03:00')
