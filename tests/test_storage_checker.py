import json
import signal
import tempfile
import threading
import time
from datetime import datetime
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.checker import StreamChecker, StreamStatus
from core.downloader import DownloadTask, Downloader
from core.downloader import build_download_path
from core.media_probe import (COMPLETED, PARTIAL, PROCESSING_ERROR,
                              MediaProbeResult, classify_media, probe_media)
from core.link_resolver import resolve_link
from core.recorder import Recorder, RecordingTask
from core.scheduler import RecordingScheduler
from core.storage import Storage
from gui.mini_player import MiniPlayer
from gui.app_window import AppWindow
from core.stream_resolver import hls_opts
from core.stream_resolver import resolve_variant_url


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.storage = object.__new__(Storage)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_atomic_save_keeps_backup_and_recovers_corrupt_file(self):
        path = self.root / 'items.json'
        self.storage._save_json(path, [{'version': 1}])
        self.storage._save_json(path, [{'version': 2}])

        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), [{'version': 2}])
        self.assertEqual(json.loads((self.root / 'items.json.bak').read_text(encoding='utf-8')),
                         [{'version': 1}])

        path.write_text('{broken', encoding='utf-8')
        self.assertEqual(self.storage._load_json(path), [{'version': 1}])
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), [{'version': 1}])

    def test_concurrent_updates_do_not_overwrite_each_other(self):
        self.storage.downloads_file = self.root / 'downloads.json'
        self.storage._save_json(self.storage.downloads_file, [])

        threads = [threading.Thread(target=self.storage.save_download, args=({'id': str(i)},))
                   for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(self.storage.get_downloads()), 20)

    def test_batch_download_update_uses_one_atomic_save(self):
        self.storage.downloads_file = self.root / 'downloads.json'
        self.storage._save_json(self.storage.downloads_file, [{'id': 'old', 'status': 'done'}])

        self.storage.save_downloads([
            {'id': 'one', 'status': 'downloading'},
            {'id': 'two', 'status': 'done'},
        ])

        downloads = {item['id']: item for item in self.storage.get_downloads()}
        self.assertEqual(set(downloads), {'old', 'one', 'two'})

    def test_migration_assigns_ids_and_connects_legacy_schedule(self):
        self.storage.channels_file = self.root / 'channels.json'
        self.storage.links_file = self.root / 'links.json'
        self.storage.schedule_file = self.root / 'schedule.json'
        self.storage._save_json(self.storage.channels_file, [{'name': 'Канал', 'url': 'stream'}])
        self.storage._save_json(self.storage.links_file, [{'name': 'Ссылка', 'url': 'page'}])
        self.storage._save_json(self.storage.schedule_file, [
            {'channel_name': 'Канал', 'source_type': 'channel'},
            {'channel_name': 'Ссылка', 'source_type': 'link'},
        ])

        self.storage._ensure_source_ids()

        channel = self.storage.get_channels()[0]
        link = self.storage.get_links()[0]
        schedule = self.storage.get_schedule()
        self.assertEqual(schedule[0]['source_id'], channel['id'])
        self.assertEqual(schedule[1]['source_id'], link['id'])

    def test_rename_preserves_extra_fields_and_updates_schedule(self):
        self.storage.channels_file = self.root / 'channels.json'
        self.storage.links_file = self.root / 'links.json'
        self.storage.schedule_file = self.root / 'schedule.json'
        self.storage._save_json(self.storage.channels_file, [
            {'id': 'channel-id', 'name': 'Старое', 'url': 'old',
             'audio_url': 'audio', 'alt_urls': ['backup']},
        ])
        self.storage._save_json(self.storage.links_file, [])
        self.storage._save_json(self.storage.schedule_file, [
            {'source_id': 'channel-id', 'channel_name': 'Старое', 'source_type': 'channel'},
        ])

        self.storage.save_channel({'id': 'channel-id', 'name': 'Новое', 'url': 'new'})

        saved = self.storage.get_channels()[0]
        schedule_item = self.storage.get_schedule()[0]
        self.assertEqual(saved['audio_url'], 'audio')
        self.assertEqual(saved['alt_urls'], ['backup'])
        self.assertEqual(schedule_item['channel_name'], 'Новое')
        self.assertEqual(schedule_item['source_id'], 'channel-id')


class RecorderResourceTests(unittest.TestCase):
    def test_recording_period_includes_start_date(self):
        task = RecordingTask('id', 'Channel', 'url', '/tmp/out.mp4')
        task.started_at = datetime(2026, 9, 22, 18, 17)
        self.assertEqual(task.format_recording_period(), '22.09.2026 · 18:17 – сейчас')

        task.finished_at = datetime(2026, 9, 23, 1, 5)
        self.assertEqual(
            task.format_recording_period(),
            '22.09.2026 · 18:17 – 23.09.2026 · 01:05',
        )

    def test_parallel_recording_names_and_task_ids_cannot_collide(self):
        recorded_at = datetime(2026, 9, 22, 12, 0, 0)
        paths = {Recorder.build_output_path('Канал', recorded_at) for _ in range(20)}
        download_paths = {build_download_path('Канал', Path('/tmp')) for _ in range(20)}

        self.assertEqual(len(paths), 20)
        self.assertEqual(len(download_paths), 20)
        self.assertTrue(all(path.name.startswith('Kanal_2026-09-22_12-00-00-000000_')
                            for path in paths))

    def test_snapshot_stream_exists_only_while_monitor_is_open(self):
        recorder = Recorder()
        task = RecordingTask('id', 'Channel', 'https://example/live.m3u8', '/tmp/out.mp4')
        task.is_recording = True
        recorder.tasks[task.task_id] = task

        stream = Mock()
        with patch('core.recorder.LiveThumbnailStream', return_value=stream):
            recorder.acquire_snapshot_consumer()
            self.assertIs(task.snapshot_stream, stream)
            stream.start.assert_called_once()

            recorder.release_snapshot_consumer()
            stream.stop.assert_called_once()
            self.assertIsNone(task.snapshot_stream)

    def test_pause_and_resume_use_named_posix_signals(self):
        recorder = Recorder()
        task = RecordingTask('id', 'Channel', 'url', '/tmp/out.mp4')
        task.process = Mock()
        task.process.poll.return_value = None
        task.is_recording = True
        task.start_time = time.time()
        recorder.tasks[task.task_id] = task

        recorder.pause_recording(task.task_id)
        self.assertTrue(task.is_paused)
        task.process.send_signal.assert_called_with(signal.SIGSTOP)

        recorder.pause_recording(task.task_id)
        self.assertFalse(task.is_paused)
        task.process.send_signal.assert_called_with(signal.SIGCONT)

    def test_stopping_paused_process_resumes_before_termination(self):
        recorder = Recorder()
        task = RecordingTask('id', 'Channel', 'url', '/tmp/out.mp4')
        task.process = Mock()
        task.process.poll.return_value = None
        task.is_recording = True
        task.is_paused = True
        task.pause_time = time.time()
        task.start_time = time.time()
        recorder.tasks[task.task_id] = task

        recorder.stop_recording(task.task_id)

        self.assertFalse(task.is_paused)
        self.assertEqual(task.process.method_calls[:4], [
            unittest.mock.call.poll(),
            unittest.mock.call.poll(),
            unittest.mock.call.send_signal(signal.SIGCONT),
            unittest.mock.call.terminate(),
        ])

    def test_recording_does_not_start_after_schedule_deadline(self):
        recorder = Recorder()
        with tempfile.TemporaryDirectory() as directory, \
                patch('core.recorder.resolve_variant_url', return_value='https://example/live.m3u8'), \
                patch('core.recorder.subprocess.Popen') as popen:
            task_id = recorder.start_recording(
                'Channel', 'https://example/live.m3u8', str(Path(directory) / 'out.mp4'),
                duration_limit_seconds=60, start_deadline_timestamp=time.time() - 1,
            )

        self.assertEqual(task_id, '')
        popen.assert_not_called()

    def test_shutdown_cancels_timers_and_stops_active_processes(self):
        recorder = Recorder()
        task = RecordingTask('id', 'Channel', 'url', '/tmp/out.mp4')
        task.process = Mock()
        task.process.poll.side_effect = [None, None, 0]
        task.is_recording = True
        task.start_time = time.time()
        recorder.tasks[task.task_id] = task
        timer = Mock()
        recorder._stop_timers[task.task_id] = timer

        recorder.stop_all(timeout=0.1)

        timer.cancel.assert_called_once()
        task.process.terminate.assert_called_once()
        self.assertTrue(recorder._closing)
        self.assertEqual(recorder.start_recording('New', 'url', '/tmp/new.mp4'), '')


class DownloaderShutdownTests(unittest.TestCase):
    def test_cancel_while_resolving_never_starts_ffmpeg(self):
        downloader = Downloader()
        resolving = threading.Event()
        release = threading.Event()

        def slow_resolve(*_args, **_kwargs):
            resolving.set()
            release.wait(timeout=1)
            return Mock(ok=True, title='Video', video_url='https://example/video',
                        audio_url=None, thumbnail='', duration=10, headers={})

        with patch('core.downloader.resolve_link', side_effect=slow_resolve), \
                patch('core.downloader.subprocess.Popen') as popen:
            task_id = downloader.start_download('https://example/page', 720, Path('/tmp'))
            self.assertTrue(resolving.wait(timeout=1))
            downloader.cancel_download(task_id)
            release.set()
            for worker in list(downloader._workers):
                worker.join(timeout=1)

        self.assertEqual(downloader.tasks[task_id].status, 'canceled')
        popen.assert_not_called()

    def test_cancel_marks_status_before_terminating_process(self):
        downloader = Downloader()
        task = DownloadTask('id', 'url', 720, Path('/tmp'))
        process = Mock()
        process.poll.return_value = None
        process.terminate.side_effect = lambda: self.assertEqual(task.status, 'canceled')
        task.process = process
        task.status = 'downloading'
        downloader.tasks[task.task_id] = task

        downloader.cancel_download(task.task_id)

        process.terminate.assert_called_once()
        self.assertEqual(task.status, 'canceled')

    def test_late_cancel_does_not_delete_completed_download(self):
        downloader = Downloader()
        with tempfile.NamedTemporaryFile(delete=False) as completed_file:
            completed_path = Path(completed_file.name)
        self.addCleanup(completed_path.unlink, missing_ok=True)
        task = DownloadTask('id', 'url', 720, completed_path.parent)
        task.status = 'done'
        task.output_path = str(completed_path)
        downloader.tasks[task.task_id] = task

        downloader.cancel_download(task.task_id)

        self.assertEqual(task.status, 'done')
        self.assertTrue(completed_path.exists())

    def test_new_downloads_receive_full_unique_ids(self):
        downloader = Downloader()
        with patch.object(downloader, '_run_worker'):
            first = downloader.start_download('https://example/one', 720, Path('/tmp'))
            second = downloader.start_download('https://example/two', 720, Path('/tmp'))

        self.assertEqual(len(first), 32)
        self.assertEqual(len(second), 32)
        self.assertNotEqual(first, second)

    def test_shutdown_stops_process_and_rejects_new_downloads(self):
        downloader = Downloader()
        task = DownloadTask('id', 'url', 720, Path('/tmp'))
        task.status = 'downloading'
        task.process = Mock()
        task.process.poll.side_effect = [None, 0]
        downloader.tasks[task.task_id] = task

        downloader.shutdown(timeout=0.1)

        task.process.terminate.assert_called_once()
        self.assertEqual(task.status, 'canceled')
        self.assertEqual(downloader.start_download('url', 720, Path('/tmp')), '')


class PreviewShutdownTests(unittest.TestCase):
    def test_all_external_preview_processes_are_stopped(self):
        process = Mock()
        process.poll.side_effect = [None, None]
        MiniPlayer._closing = False
        MiniPlayer._active_players = {'Channel': [process]}

        MiniPlayer.stop_all(timeout=0.1)

        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        self.assertTrue(MiniPlayer._closing)
        self.assertEqual(MiniPlayer._active_players, {})


class ScreenCapturePermissionTests(unittest.TestCase):
    def test_screen_capture_permission_is_asked_once_per_session(self):
        app = object.__new__(AppWindow)
        app._capture_permissions = {'full_screen': None, 'without_audio': None}
        app._capture_permission_lock = threading.Lock()
        app._closing = False
        app.root = Mock()

        with patch('gui.app_window.messagebox.askyesno', return_value=True) as ask:
            self.assertTrue(app._confirm_screen_capture_mode('Channel', True, True))
            self.assertTrue(app._confirm_screen_capture_mode('Another', True, True))

        ask.assert_called_once()
        self.assertEqual(app._capture_permissions,
                         {'full_screen': True, 'without_audio': True})


class MediaProbeTests(unittest.TestCase):
    def test_probe_reads_video_and_audio_streams(self):
        with tempfile.NamedTemporaryFile() as media_file:
            media_file.write(b'x' * 2048)
            media_file.flush()
            completed = Mock(
                returncode=0,
                stdout=json.dumps({
                    'streams': [{'codec_type': 'video'}, {'codec_type': 'audio'}],
                    'format': {'duration': '12.5'},
                }),
                stderr='',
            )
            with patch('core.media_probe.subprocess.run', return_value=completed):
                result = probe_media(media_file.name)

        self.assertTrue(result.readable)
        self.assertTrue(result.has_video)
        self.assertTrue(result.has_audio)
        self.assertEqual(result.duration, 12.5)

    def test_classification_distinguishes_all_three_results(self):
        complete = MediaProbeResult(True, has_video=True, has_audio=True)
        video_only = MediaProbeResult(True, has_video=True, has_audio=False)
        broken = MediaProbeResult(False, error='broken container')

        with patch('core.media_probe.probe_media', return_value=complete):
            self.assertEqual(classify_media('/tmp/file', True, True)[0], COMPLETED)
        with patch('core.media_probe.probe_media', return_value=video_only):
            self.assertEqual(classify_media('/tmp/file', True, True)[0], PARTIAL)
        with patch('core.media_probe.probe_media', return_value=broken):
            self.assertEqual(classify_media('/tmp/file', True, False)[0], PROCESSING_ERROR)


class SchedulerTimingTests(unittest.TestCase):
    def test_deadline_uses_absolute_end_time(self):
        now = datetime(2026, 9, 22, 12, 5, 30)
        deadline = RecordingScheduler._end_deadline(
            {'start_time': '12:00', 'end_time': '12:30'}, now)
        self.assertEqual(deadline, datetime(2026, 9, 22, 12, 30))

    def test_deadline_crosses_midnight(self):
        now = datetime(2026, 9, 22, 23, 10)
        deadline = RecordingScheduler._end_deadline(
            {'start_time': '23:00', 'end_time': '01:00'}, now)
        self.assertEqual(deadline, datetime(2026, 9, 23, 1, 0))


class StreamCheckerTests(unittest.TestCase):
    def test_hls_uses_recording_headers_and_get_for_segment(self):
        checker = StreamChecker()
        playlist = Mock(status_code=200, text='#EXTM3U\n#EXTINF:2,\nsegment.ts?token=x')
        segment = Mock(status_code=206)
        segment.__enter__ = Mock(return_value=segment)
        segment.__exit__ = Mock(return_value=False)

        with patch('core.checker.requests.get', side_effect=[playlist, segment]) as request:
            status, _ = checker.check({
                'name': 'НТВ',
                'type': 'iptv',
                'url': 'https://media.example/live.m3u8',
            })

        self.assertIs(status, StreamStatus.GREEN)
        self.assertEqual(request.call_args_list[0].kwargs['headers']['Referer'], 'https://www.ntv.ru')
        self.assertEqual(request.call_args_list[1].args[0],
                         'https://media.example/segment.ts?token=x')
        self.assertTrue(request.call_args_list[1].kwargs['stream'])

    def test_non_hls_check_does_not_probe_external_connectivity(self):
        checker = StreamChecker()
        response = Mock(status_code=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with patch('core.checker.requests.get', return_value=response) as request:
            status, _ = checker.check({
                'name': 'Канал',
                'type': 'iptv',
                'url': 'https://media.example/live',
            })

        self.assertIs(status, StreamStatus.GREEN)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], 'https://media.example/live')
        self.assertEqual(request.call_args.kwargs['headers']['Range'], 'bytes=0-1023')


class StreamReliabilityTests(unittest.TestCase):
    def test_hls_retries_failed_segments(self):
        options = hls_opts('https://cdn.example/live.m3u8')
        self.assertEqual(options[options.index('-seg_max_retry') + 1], '10')
        self.assertEqual(hls_opts('https://cdn.example/video.mp4'), [])

    def test_smotrim_uses_player_api_before_ytdlp(self):
        response = Mock()
        response.json.return_value = {
            'data': {
                'streams': {'m3u8': 'https://cdn.example/live.m3u8'},
                'episode': {'title': 'Выпуск'},
            }
        }
        with patch('core.link_resolver.requests.get', return_value=response), \
                patch('core.link_resolver._resolve_via_ytdlp') as ytdlp:
            info = resolve_link('https://smotrim.ru/video/12345')

        self.assertTrue(info.ok)
        self.assertEqual(info.video_url, 'https://cdn.example/live.m3u8')
        self.assertEqual(info.title, 'Выпуск')
        ytdlp.assert_not_called()

    def test_long_1tv_news_url_uses_same_api_as_short_url(self):
        response = Mock(status_code=200)
        response.json.return_value = [{
            'title': 'Новость',
            'poster': 'poster.jpg',
            'duration': 214,
            'sources': [{
                'type': 'application/x-mpegURL',
                'src': 'https://balancer-vod.1tv.ru/video/master.m3u8',
            }],
        }]
        long_url = ('https://www.1tv.ru/news/2025-09-21/'
                    '521255-nazvany_laureaty_premii_imeni_sergeya_puskepalisa?ysclid=test')
        with patch('core.link_resolver.requests.get', return_value=response) as request, \
                patch('core.link_resolver._resolve_via_ytdlp') as ytdlp:
            info = resolve_link(long_url)

        self.assertTrue(info.ok)
        self.assertEqual(info.video_url, 'https://balancer-vod.1tv.ru/video/master.m3u8')
        self.assertIn('news_id=521255', request.call_args.args[0])
        ytdlp.assert_not_called()

    def test_vk_live_url_is_normalized_for_ytdlp(self):
        expected = Mock(ok=True)
        with patch('core.link_resolver._resolve_known_site', return_value=None), \
                patch('core.link_resolver.YTDLP_AVAILABLE', True), \
                patch('core.link_resolver._resolve_via_ytdlp', return_value=expected) as ytdlp:
            result = resolve_link('https://vkvideo.ru/live-10_20')

        self.assertIs(result, expected)
        self.assertEqual(ytdlp.call_args.args[0], 'https://vkvideo.ru/video-10_20')

    def test_variant_resolver_handles_parent_relative_url(self):
        playlist = Mock(status_code=200, text=(
            '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n'
            '../720/index.m3u8?token=abc\n'))
        with patch('core.stream_resolver.requests.get', return_value=playlist):
            selected = resolve_variant_url('https://cdn.example/master/live.m3u8?auth=x')

        self.assertEqual(selected, 'https://cdn.example/720/index.m3u8?token=abc')


if __name__ == '__main__':
    unittest.main()
