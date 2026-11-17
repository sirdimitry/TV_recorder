import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from core.downloader import Downloader, DownloadTask
from core.youtube_transfer import identity, compatible, supported
from gui.app_window import AppWindow


class ResumePolicyTests(unittest.TestCase):
    def test_only_full_youtube_videos_use_resumable_engine(self):
        for url in ('https://youtu.be/abc', 'https://www.youtube.com/watch?v=abc', 'https://m.youtube.com/shorts/abc'):
            self.assertTrue(supported(url))
            self.assertFalse(supported(url, 10))
        self.assertFalse(supported('https://youtube.com.example/video'))

    def test_changed_format_or_content_is_never_appended(self):
        info = dict(id='one', duration=100, format_id='137', ext='mp4', vcodec='avc1', acodec='none', filesize=1000)
        original = identity(info)
        for key, value in (('id', 'other'), ('format_id', '248'), ('filesize', 2000), ('duration', 110), ('vcodec', 'vp9')):
            changed = dict(info, **{key: value})
            self.assertFalse(compatible(original, identity(changed)))
        self.assertTrue(compatible(original, identity(dict(info, url='https://fresh/signed-url'))))

    def test_restore_is_paused_and_never_launches_network(self):
        with tempfile.TemporaryDirectory() as folder:
            task = DownloadTask('a'*32, 'https://youtu.be/test', 720, Path(folder))
            task.resume_supported = True
            task.status = 'downloading'
            task.downloaded_bytes = 123
            owner = Downloader()
            with patch('subprocess.Popen') as launch:
                restored = owner.restore_download(task.to_dict())
            launch.assert_not_called()
            self.assertEqual(restored.status, 'paused')
            self.assertEqual(restored.downloaded_bytes, 123)
            self.assertEqual(restored.work_dir, task.work_dir)
            self.assertIs(owner.restore_download(task.to_dict()), restored)

    def test_pause_and_cancel_have_different_disk_semantics(self):
        with tempfile.TemporaryDirectory() as folder:
            task = DownloadTask('a'*32, 'https://youtu.be/test', 720, Path(folder))
            task.resume_supported = True
            task.status = 'downloading'
            Path(task.work_dir).mkdir()
            part = Path(task.work_dir)/'media.mp4.part'
            part.write_bytes(b'partial')
            owner = Downloader()
            owner.tasks[task.task_id] = task
            self.assertTrue(owner.pause_download(task.task_id))
            self.assertTrue(part.exists())
            task.status = 'paused'
            owner.cancel_download(task.task_id)
            self.assertFalse(part.exists())

    def test_pause_cannot_interrupt_merging_or_resume_twice(self):
        task = DownloadTask('a'*32, 'https://youtu.be/test', 720, Path('/tmp'))
        task.resume_supported = True
        task.status = 'processing'
        owner = Downloader()
        owner.tasks[task.task_id] = task
        self.assertFalse(owner.pause_download(task.task_id))
        task.status, task.worker_active = 'paused', True
        self.assertFalse(owner.resume_download(task.task_id))

    def test_download_tab_stops_and_hides_preview_then_restores_it(self):
        app = object.__new__(AppWindow)
        app._tab_pages = {'downloads': Mock(), 'channels': Mock()}
        app.preview_panel = Mock()
        app.btn_toolbar_check = Mock()
        app.tab_pages = Mock()
        app.right_paned = 'right'
        app.paned = Mock()
        app.paned.panes.return_value = ['right']
        app._on_left_tab_changed('downloads')
        app.preview_panel.clear.assert_called_once()
        app.preview_panel.pack_forget.assert_called_once()
        app.paned.forget.assert_called_once_with('right')
        app.preview_panel.winfo_manager.return_value = ''
        app.paned.panes.return_value = []
        app._on_left_tab_changed('channels')
        app.preview_panel.pack.assert_called_once()
        app.paned.add.assert_called_once_with('right', weight=1)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class RealResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.media = cls.root/'source.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=640x360:rate=25', '-f', 'lavfi', '-i', 'sine=frequency=440',
                        '-t', '8', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '0', '-c:a', 'aac',
                        '-movflags', '+faststart', str(cls.media)], check=True)
        cls.payload = cls.media.read_bytes()
        cls.ranges = []
        cls.server_fail = False
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                header = self.headers.get('Range', '')
                start = int(header.split('=')[1].split('-')[0]) if header else 0
                cls.ranges.append(start)
                self.send_response(206 if header else 200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(len(cls.payload)-start))
                self.send_header('Accept-Ranges', 'bytes')
                if header:
                    self.send_header('Content-Range', f'bytes {start}-{len(cls.payload)-1}/{len(cls.payload)}')
                self.end_headers()
                try:
                    for offset in range(start, len(cls.payload), 32768):
                        self.wfile.write(cls.payload[offset:offset+32768])
                        self.wfile.flush()
                        time.sleep(.015)
                        if cls.server_fail:
                            self.close_connection = True
                            return
                except (BrokenPipeError, ConnectionResetError):
                    pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        info = dict(id='local-video', title='Resume fixture', url=f'http://127.0.0.1:{cls.server.server_port}/video.mp4',
                    ext='mp4', format_id='test', format='test', protocol='http', duration=8,
                    vcodec='avc1', acodec='aac', filesize=len(cls.payload), height=360,
                    extractor='test', extractor_key='Test', webpage_url='https://youtu.be/test')
        cls.script = cls.root/'worker.py'
        repo = str(Path(__file__).resolve().parents[1])
        cls.script.write_text('import sys,copy\nsys.path.insert(0,'+repr(repo)+')\n'
            'from yt_dlp import YoutubeDL\n'
            'from core.youtube_transfer import main\n'
            'INFO='+repr(info)+'\n'
            'YoutubeDL.extract_info=lambda self,*a,**k:copy.deepcopy(INFO)\n'
            'sys.exit(main())\n')

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temporary.cleanup()

    def wait_for(self, condition, timeout=20):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.025)
        self.fail('Timed out waiting for download state')

    def cmd(self, task):
        return [sys.executable, str(self.script), task.url, task.work_dir, str(task.target_height)]

    def test_pause_restart_app_and_resume_uses_range_and_preserves_exact_bytes(self):
        type(self).ranges.clear()
        type(self).server_fail = False
        with tempfile.TemporaryDirectory() as folder, patch('core.resumable_download.command', side_effect=self.cmd):
            owner = Downloader()
            task_id = owner.start_download('https://youtu.be/test', 720, Path(folder))
            task = owner.tasks[task_id]
            part = Path(task.work_dir)/'media.mp4.part'
            self.wait_for(lambda: part.exists() and part.stat().st_size > 131072)
            self.assertTrue(owner.pause_download(task_id))
            self.wait_for(lambda: not task.worker_active)
            self.assertEqual(task.status, 'paused', task.error_message)
            self.assertTrue(part.exists())
            saved = task.to_dict()
            owner.shutdown()
            restarted = Downloader()
            resumed = restarted.restore_download(saved)
            self.assertTrue(restarted.resume_download(task_id))
            self.wait_for(lambda: not resumed.worker_active)
            self.assertEqual(resumed.status, 'done', resumed.error_message)
            self.assertEqual(Path(resumed.output_path).read_bytes(), self.payload)
            self.assertTrue(any(value > 0 for value in self.ranges), self.ranges)
            self.assertFalse(Path(resumed.work_dir).exists())
            restarted.shutdown()

    def test_separate_video_and_audio_resume_then_merge_validates_both_tracks(self):
        import ast
        from core.media_probe import probe_media
        tree = ast.parse(self.script.read_text())
        info = next(ast.literal_eval(node.value) for node in tree.body
                    if isinstance(node, ast.Assign) and node.targets[0].id == 'INFO')
        video = dict(info, format_id='v', acodec='none')
        audio = dict(info, format_id='a', vcodec='none')
        info.update(format_id='v+a', format='v+a', protocol='http+http', requested_formats=[video, audio])
        info.pop('url')
        script = self.root/'separate-worker.py'
        script.write_text(self.script.read_text().replace(
            next(line for line in self.script.read_text().splitlines() if line.startswith('INFO=')),
            'INFO='+repr(info)))
        type(self).server_fail = False
        type(self).ranges.clear()
        def separate(task):
            return [sys.executable, str(script), task.url, task.work_dir, str(task.target_height)]
        with tempfile.TemporaryDirectory() as folder, patch('core.resumable_download.command', side_effect=separate):
            owner = Downloader()
            task_id = owner.start_download('https://youtu.be/test', 720, Path(folder))
            task = owner.tasks[task_id]
            video_file = Path(task.work_dir)/'media.fv.mp4'
            audio_part = Path(task.work_dir)/'media.fa.mp4.part'
            self.wait_for(lambda: not task.worker_active or video_file.exists() and audio_part.exists() and audio_part.stat().st_size > 65536)
            self.assertTrue(task.worker_active, task.error_message+'; files='+str(list(Path(task.work_dir).glob('*'))))
            self.assertTrue(owner.pause_download(task_id))
            self.wait_for(lambda: not task.worker_active)
            self.assertTrue(video_file.exists())
            self.assertTrue(owner.resume_download(task_id))
            self.wait_for(lambda: not task.worker_active)
            self.assertEqual(task.status, 'done', task.error_message)
            result = probe_media(task.output_path)
            self.assertTrue(result.has_video and result.has_audio)
            self.assertAlmostEqual(result.video_duration, 8, delta=.2)
            self.assertAlmostEqual(result.audio_duration, 8, delta=.2)
            self.assertTrue(any(offset > 0 for offset in self.ranges))
            owner.shutdown()

    def test_network_failure_exhausts_attempts_and_leaves_manual_resume(self):
        type(self).server_fail = True
        try:
            with tempfile.TemporaryDirectory() as folder, \
                    patch('core.resumable_download.command', side_effect=self.cmd), \
                    patch('core.resumable_download.RETRY_DELAYS', (0,)):
                owner = Downloader()
                task_id = owner.start_download('https://youtu.be/test', 720, Path(folder))
                task = owner.tasks[task_id]
                self.wait_for(lambda: not task.worker_active, timeout=25)
                self.assertEqual(task.status, 'paused', task.error_message)
                self.assertIn('исчерпаны', task.error_message)
                self.assertTrue(list(Path(task.work_dir).glob('*.part')))
                type(self).server_fail = False
                self.assertTrue(owner.resume_download(task_id))
                self.wait_for(lambda: not task.worker_active)
                self.assertEqual(task.status, 'done', task.error_message)
                self.assertEqual(Path(task.output_path).read_bytes(), self.payload)
                owner.shutdown()
        finally:
            type(self).server_fail = False


if __name__ == '__main__':
    unittest.main()
