"""Temporary macOS idle-sleep assertion owned by recording-session leases."""
import os
import subprocess
import sys
import threading
import time

from utils.logger import logger


class SleepInhibitor:
    def __init__(self, on_error=None, keep_display_awake=False):
        self._lock = threading.RLock()
        self._leases = set()
        self._process = None
        self._closed = False
        self._retry_after = 0.0
        self._notified = False
        self.on_error = on_error
        self.keep_display_awake = keep_display_awake

    @property
    def active(self):
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def _error(self, detail):
        logger.warning('Защита от автоматического сна недоступна: %s', detail)
        if self.on_error and not self._notified:
            self._notified = True
            try:
                self.on_error('Не удалось включить защиту от автоматического сна. Проверьте настройки сна перед длительной записью.')
            except Exception:
                logger.exception('Не удалось показать предупреждение о защите от сна')

    def _ensure_started(self):
        if self._closed or not self._leases or sys.platform != 'darwin':
            return
        if self._process is not None:
            if self._process.poll() is None:
                return
            self._process.wait()
            self._process = None
            self._error('caffeinate завершился во время записи')
        if time.monotonic() < self._retry_after:
            return
        try:
            # -i prevents idle system sleep; screen capture additionally uses -d.
            # -w releases assertions if the application crashes or is force-quit.
            flags = ['-i', '-d'] if self.keep_display_awake else ['-i']
            self._process = subprocess.Popen(
                ['/usr/bin/caffeinate', *flags, '-w', str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._retry_after = time.monotonic()+10
            logger.info('Защита от автоматического сна включена (pid=%s)', self._process.pid)
        except OSError as error:
            self._process = None
            self._retry_after = time.monotonic()+10
            self._error(str(error))

    def acquire(self, lease):
        with self._lock:
            if self._closed:
                return
            self._leases.add(lease)
            self._ensure_started()

    def refresh(self):
        with self._lock:
            self._ensure_started()

    def _stop(self):
        process, self._process = self._process, None
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    logger.exception('Не удалось принудительно завершить caffeinate')
                    if process.poll() is None:
                        self._process = process
            except OSError:
                logger.exception('Не удалось завершить caffeinate')
                if process.poll() is None:
                    self._process = process
            if self._process is None:
                logger.info('Защита от автоматического сна снята')
        self._retry_after = 0.0
        self._notified = False

    def release(self, lease):
        with self._lock:
            self._leases.discard(lease)
            if not self._leases:
                self._stop()

    def close(self):
        with self._lock:
            self._closed = True
            self._leases.clear()
            self._stop()
