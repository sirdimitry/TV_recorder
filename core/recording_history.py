"""Durable session snapshots and event journal; media files are never modified."""
import fcntl
from datetime import datetime
from pathlib import Path
import re
import threading

from core.storage import Storage, StorageError

ACTIVE_STATES = {'recording', 'paused', 'connecting', 'reconnecting', 'retrying', 'finalizing'}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def safe_error(value):
    return re.sub(r'https?://\S+', '[адрес источника]', str(value or ''))[:1500]


class RecordingHistory:
    _lock = threading.RLock()

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.storage = object.__new__(Storage)
        self._owners = {}
        self.mark_interrupted()

    def _path(self, task_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', task_id):
            raise ValueError('Некорректный ID записи')
        return self.directory/f'{task_id}.json'

    def save(self, snapshot, event, detail=None):
        task_id = snapshot['task_id']
        path = self._path(task_id)
        with self._lock:
            if task_id not in self._owners:
                handle = path.with_suffix('.lock').open('a+')
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except Exception:
                    handle.close()
                    raise
                self._owners[task_id] = handle
            records = self.storage._load_json(path)
            record = records[0] if records else {'version': 1, 'events': []}
            record['session'] = snapshot
            record['updated_at'] = now_iso()
            record['events'].append({'at': now_iso(), 'type': event, 'detail': detail or {}})
            self.storage._save_json(path, [record])

    def release(self, task_id):
        with self._lock:
            handle = self._owners.pop(task_id, None)
            if handle:
                handle.close()

    def read(self, task_id):
        with self._lock:
            records = self.storage._load_json(self._path(task_id))
            if not records:
                return None
            record = records[0]
            if (not isinstance(record, dict) or not isinstance(record.get('session'), dict)
                    or not isinstance(record.get('events'), list)):
                raise StorageError('Некорректная структура истории: '+str(task_id))
            return record

    def list_records(self, limit=100):
        paths = sorted(self.directory.glob('*.json'), key=lambda path: path.stat().st_mtime, reverse=True)
        result = []
        for path in paths[:limit]:
            try:
                record = self.read(path.stem)
                if record:
                    result.append(record)
            except (StorageError, ValueError) as error:
                result.append({'session': {'task_id': path.stem, 'name': 'История недоступна',
                                          'state': 'history_error', 'error': str(error)}, 'events': []})
        return result

    def mark_interrupted(self):
        # A second running instance must not mark another instance's task interrupted.
        with self._lock:
            for path in self.directory.glob('*.json'):
                handle = path.with_suffix('.lock').open('a+')
                try:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    try:
                        records = self.storage._load_json(path)
                    except StorageError:
                        continue
                    if (not records or not isinstance(records[0], dict)
                            or not isinstance(records[0].get('session'), dict)
                            or not isinstance(records[0].get('events'), list)
                            or records[0]['session'].get('state') not in ACTIVE_STATES):
                        continue
                    record = records[0]
                    record['session']['state'] = 'interrupted'
                    record['session']['error'] = 'Приложение завершилось до окончания записи; доступны сохранённые части.'
                    record['updated_at'] = now_iso()
                    record['events'].append({'at': now_iso(), 'type': 'interrupted', 'detail': {}})
                    self.storage._save_json(path, [record])
                finally:
                    handle.close()

    def record_recovered(self, capture, output):
        import uuid
        from core.media_probe import probe_media
        capture, output = str(capture), str(output)
        matched = None
        for record in self.list_records(limit=1000000):
            session = record['session']
            if (session.get('capture_path') == capture
                    or any(part.get('path') == capture for part in session.get('parts', []))):
                matched = session
                break
        session = dict(matched) if matched else dict(
            task_id=uuid.uuid4().hex, name=Path(output).stem, source='recovery', parts=[])
        probe = probe_media(output)
        part = dict(path=output, status='partial', duration=probe.video_duration or probe.duration,
                    container_duration=probe.duration,
                    has_video=probe.has_video, has_audio=probe.has_audio,
                    error='Восстановлено после аварии; полнота не подтверждена')
        parts = [dict(item) for item in session.get('parts', [])]
        position = next((i for i, item in enumerate(parts) if item.get('path') == capture), None)
        if position is None:
            parts.append(part)
        else:
            parts[position] = {**parts[position], **part}
        session.update(parts=parts, output_path=output, state='partial', error=part['error'])
        session['actual_seconds'] = sum(item.get('duration') or 0 for item in parts)
        expected = session.get('expected_seconds')
        session['missing_seconds'] = max(0, expected-session['actual_seconds']) if expected else None
        try:
            self.save(session, 'file_recovered', {'path': output})
        finally:
            self.release(session['task_id'])

    def close(self):
        for task_id in list(self._owners):
            self.release(task_id)
