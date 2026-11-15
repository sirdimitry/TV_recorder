"""Read FFmpeg progress without treating repeated heartbeats as media progress."""
import threading
import time

STARTUP_TIMEOUT = 90.0
STALL_TIMEOUT = 60.0
STOP_GRACE = 5.0
RETRY_DELAYS = (2.0, 5.0, 10.0)


class RecordingProgress:
    def __init__(self, process, task, on_stall, on_update=lambda: None, on_deadline=lambda: None):
        self.process = process
        self.task = task
        self.on_stall = on_stall
        self.on_update = on_update
        self.on_deadline = on_deadline
        self.started = time.monotonic()
        self.last_progress = self.started
        self.has_progress = False
        self.last_bytes = 0
        self.last_media_us = 0
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._reader = None
        self._watcher = None

    def feed(self, line, now=None):
        now = time.monotonic() if now is None else now
        key, separator, value = line.strip().partition('=')
        if not separator or key not in ('out_time_us', 'total_size'):
            return
        try:
            value = int(value)
        except ValueError:
            return
        with self._lock:
            previous = self.last_media_us if key == 'out_time_us' else self.last_bytes
            if value <= previous:
                return
            if key == 'out_time_us':
                self.last_media_us = value
                self.task.media_seconds = value / 1_000_000
            else:
                self.last_bytes = value
                self.task.recorded_bytes = value
            self.last_progress = now
            self.has_progress = True
            self.task.connection_state = 'recording'
            if (not self.task.stop_requested and not self.task.is_paused
                    and self.task.phase not in ('stopping', 'finalizing', 'checking')):
                self.task.phase = 'recording'

    def check(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.task.is_paused:
                self.last_progress = now
                return False
            elapsed = now - self.last_progress
            timeout = STALL_TIMEOUT if self.has_progress else STARTUP_TIMEOUT
            state = 'reconnecting' if self.has_progress and elapsed >= 10 else (
                'recording' if self.has_progress else 'connecting')
            self.task.connection_state = state
            return elapsed >= timeout

    def start(self):
        def read():
            try:
                while not self._done.is_set():
                    line = self.process.stdout.readline(4096)
                    if not line:
                        break
                    self.feed(line.decode(errors='replace') if isinstance(line, bytes) else line)
            except (OSError, ValueError):
                pass
        def watch():
            while not self._done.wait(.5):
                if self.process.poll() is not None:
                    return
                if self.task.deadline is not None and time.time() >= self.task.deadline:
                    self.on_deadline()
                    return
                if self.check():
                    self.on_stall()
                    return
                self.on_update()
        self._reader = threading.Thread(target=read, daemon=True, name='RecordingProgressReader')
        self._watcher = threading.Thread(target=watch, daemon=True, name='RecordingProgressWatchdog')
        self._reader.start()
        self._watcher.start()

    def close(self):
        self._done.set()
        for thread in (self._reader, self._watcher):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=1)
