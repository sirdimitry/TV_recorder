from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from core.recorder import Recorder, RecordingTask
from core.recording_progress import RecordingProgress
from gui.recording_panel import RecordingPanel
from gui.recording_presenter import card_model, task_state, visible_tasks, summary_label, size_label


class RecordingFeedbackTests(unittest.TestCase):
    def task(self, state='recording'):
        task = RecordingTask(state, 'Телеканал', 'local-test', '/tmp/unused-feedback.mp4')
        task.phase = state
        task.connection_state = state
        task.is_recording = state in ('recording', 'connecting', 'reconnecting')
        if state in ('completed', 'partial', 'processing_error'):
            task.result_status = state
            task.success = state == 'completed'
        return task

    def test_processing_is_active_even_after_ffmpeg_exits(self):
        for state in ('stopping', 'finalizing', 'checking'):
            task = self.task(state)
            model = card_model(task)
            self.assertEqual(model['state'], state)
            self.assertFalse(model['can_pause'])
            self.assertFalse(model['can_stop'])
            self.assertFalse(model['can_remove'])
            self.assertEqual(visible_tasks([task], 'Активные'), [task])
        task.merging = True
        self.assertEqual(task_state(task), 'joining')
        self.assertFalse(card_model(task)['can_remove'])

    def test_terminal_outcomes_and_early_source_are_distinct(self):
        for state in ('completed', 'partial', 'processing_error'):
            task = self.task(state)
            self.assertEqual(task_state(task), state)
            self.assertTrue(card_model(task)['can_remove'])
        task = self.task('completed')
        task.ended_early = True
        self.assertEqual(task_state(task), 'ended_early')

    def test_filters_and_counts_keep_simultaneous_tasks_separate(self):
        tasks = [self.task(state) for state in ('recording', 'checking', 'completed', 'partial')]
        paused = self.task()
        paused.is_paused = True
        tasks.append(paused)
        self.assertEqual(len(visible_tasks(tasks, 'Активные')), 3)
        self.assertEqual(visible_tasks(tasks, 'Требуют внимания'), [tasks[3]])
        self.assertEqual(summary_label(tasks), 'В работе: 2 · Пауза: 1 · Готово: 1 · Внимание: 1')

    def test_live_pause_explains_missing_airtime_and_stop_disables_controls(self):
        task = self.task()
        task.is_live = task.is_paused = True
        self.assertIn('Эфир на паузе пропускается', card_model(task)['reason'])
        self.assertTrue(card_model(task)['can_pause'])
        task.stop_requested = True
        self.assertFalse(card_model(task)['can_pause'])
        self.assertFalse(card_model(task)['can_stop'])

    def test_disk_thresholds_include_shared_reserve_and_errors(self):
        task = self.task()
        task.disk_reserved_bytes = 1024**3
        for free, color in ((5, 'muted'), (3, 'yellow'), (1.5, 'red')):
            task.disk_free_bytes = free*1024**3
            self.assertEqual(card_model(task)['disk_color'], color)
        task.storage_error = 'Недостаточно места'
        self.assertEqual(card_model(task)['reason'], task.storage_error)
        task.storage_error = ''
        task.disk_error = 'Диск отключён'
        self.assertEqual(card_model(task)['disk'], 'Диск недоступен')

    def test_partial_and_merge_errors_both_remain_visible(self):
        task = self.task('partial')
        task.error_message = 'Пропуск 68 секунд'
        task.merge_error = 'Не удалось собрать MP4'
        self.assertIn(task.error_message, card_model(task)['reason'])
        self.assertIn(task.merge_error, card_model(task)['reason'])
        task.error_message = task.merge_error = ''
        task.missing_seconds = 68
        self.assertIn('68.0', card_model(task)['reason'])
        task.parts = [{'path': 'a'}, {'path': 'b'}]
        self.assertTrue(card_model(task)['can_merge'])
        task.merged_path = 'joined.mp4'
        self.assertFalse(card_model(task)['can_merge'])
        self.assertIn('Единый MP4 · частей 2', card_model(task)['metrics'])

    def test_metrics_count_raw_or_output_once_and_merged_output_alone(self):
        recorder = Recorder()
        with tempfile.TemporaryDirectory() as directory:
            output, raw, previous, joined = [Path(directory)/name for name in ('out.mp4', 'raw.mp4', 'part.mp4', 'joined.mp4')]
            for path, size in ((output, 20), (raw, 30), (previous, 10), (joined, 39)):
                path.write_bytes(b'x'*size)
            task = self.task()
            task.output_path, task.capture_path = str(output), str(raw)
            task.parts = [{'path': str(previous)}, {'path': str(previous)}]
            task.disk_guard = SimpleNamespace(last_free_bytes=5*1024**3, last_reserved_bytes=0, last_error='')
            task.storage_warning = 'Мало места'
            recorder._refresh_task_metrics(task)
            self.assertEqual(task.file_size_bytes, 40)
            self.assertTrue(task.output_available)
            self.assertEqual(task.storage_warning, '')
            raw.unlink()
            recorder._refresh_task_metrics(task)
            self.assertEqual(task.file_size_bytes, 30)
            task.merged_path = task.output_path = str(joined)
            recorder._refresh_task_metrics(task)
            self.assertEqual(task.file_size_bytes, 39)
            joined.unlink()
            recorder._refresh_task_metrics(task)
            self.assertFalse(task.output_available)
            self.assertIsNone(task.file_size_bytes)

    def test_late_progress_cannot_replace_processing_phase(self):
        for phase in ('stopping', 'finalizing', 'checking'):
            task = self.task(phase)
            monitor = RecordingProgress(Mock(), task, Mock())
            monitor.feed('out_time_us=2000000')
            self.assertEqual(task.phase, phase)

    def test_ui_updates_are_coalesced_until_refresh_runs(self):
        panel = object.__new__(RecordingPanel)
        panel._refresh_pending = False
        panel.after = Mock()
        panel.refresh = Mock()
        for _ in range(100):
            panel._schedule_refresh()
        panel.after.assert_called_once()
        callback = panel.after.call_args.args[1]
        callback()
        panel.refresh.assert_called_once()
        self.assertFalse(panel._refresh_pending)
        panel._schedule_refresh()
        self.assertEqual(panel.after.call_count, 2)

    def test_sizes_and_connecting_labels(self):
        self.assertEqual(size_label(None), '—')
        self.assertEqual(size_label(1024**3), '1.0 ГиБ')
        for state, label in (('connecting', 'Подключение'), ('reconnecting', 'Переподключение')):
            self.assertEqual(card_model(self.task(state))['label'], label)


if __name__ == '__main__':
    unittest.main()
