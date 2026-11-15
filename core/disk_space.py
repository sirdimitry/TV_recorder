"""Disk checks shared by recording, downloads and MP4 finalization."""
from pathlib import Path
import os
import shutil
import tempfile
import threading

from utils.logger import logger

GIB = 1024**3
MIN_FREE = GIB
WARN_FREE = 3 * GIB
FINALIZE_MARGIN = 256 * 1024**2
CHECK_INTERVAL = 1.0


class DiskSpaceError(OSError):
    pass


def available_space(directory):
    directory = Path(directory)
    try:
        if not directory.is_dir():
            raise OSError('Папка отсутствует или диск отключён')
        return shutil.disk_usage(directory).free
    except OSError as error:
        raise DiskSpaceError(f'Не удалось проверить папку {directory}: {error}') from error


def ensure_space(directory, *, reserve=0, minimum=MIN_FREE, writable=False):
    directory = Path(directory)
    try:
        if writable:
            directory.mkdir(parents=True, exist_ok=True)
            # Real write/fsync, rather than an unreliable access() permission check.
            with tempfile.TemporaryFile(dir=directory) as probe:
                probe.write(b'TV Recorder disk check\n')
                probe.flush()
                os.fsync(probe.fileno())
        free = available_space(directory)
    except OSError as error:
        if isinstance(error, DiskSpaceError):
            raise
        raise DiskSpaceError(f'Папка недоступна для записи: {directory}. {error}') from error
    required = minimum + reserve
    if free < required:
        raise DiskSpaceError(
            f'Недостаточно места в {directory}: свободно {free/GIB:.2f} ГиБ, '
            f'нужно не менее {required/GIB:.2f} ГиБ '
            '(включая резерв для завершения файлов).')
    return free


class DiskGuard:
    """One stoppable watcher per task; reserves include concurrent raw recordings."""
    _lock = threading.Lock()
    _reservations = {}

    def __init__(self, directory, *, reserve_file=None, reserve_factor=1, on_warning=None, on_stop=None):
        self.directory = Path(directory)
        self.reserve_file = Path(reserve_file) if reserve_file else None
        self.reserve_factor = max(1, reserve_factor)
        self.on_warning = on_warning or (lambda message: None)
        self.on_stop = on_stop or (lambda message: None)
        self._done = threading.Event()
        self._warned = False
        self._thread = None
        self.last_free_bytes = None
        self.last_reserved_bytes = 0
        self.last_error = ''
        try:
            self._device = self.directory.stat().st_dev
        except OSError:
            self._device = None

    @classmethod
    def reserved_bytes(cls, directory):
        try:
            device = Path(directory).stat().st_dev
            with cls._lock:
                files = list(cls._reservations.values())
            total = 0
            for path, factor in files:
                try:
                    info = path.stat()
                    if path.parent.stat().st_dev == device:
                        total += int(info.st_size * factor)
                except FileNotFoundError:
                    # Another task may publish and remove its raw file concurrently.
                    continue
            return total
        except OSError as error:
            raise DiskSpaceError(f'Не удалось оценить резерв записи: {error}') from error

    def start(self):
        if self.reserve_file:
            with self._lock:
                self._reservations[id(self)] = (self.reserve_file, self.reserve_factor)
        self._thread = threading.Thread(target=self._run, daemon=True, name='DiskSpaceGuard')
        self._thread.start()

    def stop_monitor(self):
        self._done.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def close(self):
        self.stop_monitor()
        with self._lock:
            self._reservations.pop(id(self), None)

    def check(self):
        try:
            if self.directory.stat().st_dev != self._device:
                raise DiskSpaceError(f'Диск папки {self.directory} изменился или был отключён')
        except OSError as error:
            raise DiskSpaceError(f'Папка записи недоступна: {self.directory}. {error}') from error
        reserve = self.reserved_bytes(self.directory)
        free = ensure_space(self.directory, reserve=reserve)
        self.last_free_bytes = free
        self.last_reserved_bytes = reserve
        self.last_error = ''
        if free < WARN_FREE + reserve and not self._warned:
            self._warned = True
            self.on_warning(
                f'Заканчивается место в {self.directory}: свободно {free/GIB:.2f} ГиБ. '
                'Освободите место; при достижении резерва задача будет остановлена.')
        return free

    def _run(self):
        while not self._done.is_set():
            try:
                self.check()
            except DiskSpaceError as error:
                self.last_free_bytes = None
                self.last_error = str(error)
                self._done.set()
                self.on_stop(str(error))
                return
            except Exception:
                logger.exception('Ошибка контроля диска')
            self._done.wait(CHECK_INTERVAL)
