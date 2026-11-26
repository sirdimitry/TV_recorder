"""Crash-tolerant fragmented MP4 capture and lossless finalization."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from core.disk_space import DiskGuard, FINALIZE_MARGIN, ensure_space
from core.media_probe import probe_media
from core.screen_capture import build_timestretch_cmd
from utils.config import Config

_ACTIVE_MARKERS = {}

# Let FFmpeg inspect the first packets before writing the MP4 header.
# With empty_moov, FFmpeg 7.1 can miss automatic ADTS-to-ASC conversion and
# abort a transport-stream capture as soon as it tries to mux AAC.
FRAGMENT_FLAGS = '+frag_keyframe+default_base_moof'


def prepare_recording(output, task_id, *, speed=None, has_audio=True, max_duration=None):
    output = Path(output).resolve()
    capture = output.with_name(f'{output.stem}.{task_id}.recording.mp4')
    directory = Config.DATA_DIR / 'pending_recordings'
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / f'{task_id}.json'
    metadata = dict(capture=str(capture), output=str(output), speed=speed, has_audio=has_audio, max_duration=max_duration)
    temporary_marker = directory / f'.{task_id}.{uuid.uuid4().hex}.tmp'
    lock = temporary_marker.open('x+', encoding='utf-8')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        json.dump(metadata, lock)
        lock.flush()
        os.fsync(lock.fileno())
        os.link(temporary_marker, marker)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        lock.close()
        raise
    finally:
        temporary_marker.unlink(missing_ok=True)
    _ACTIVE_MARKERS[marker] = lock
    return capture, marker


def release_recording(marker):
    lock = _ACTIVE_MARKERS.pop(Path(marker), None)
    if lock:
        lock.close()


def finalize_recording(marker):
    with Path(marker).open('r') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _finalize_recording(marker)


def _finalize_recording(marker):
    """Publish only a validated result; retain capture and marker on any error.

    Hard-link publication is atomic and refuses to overwrite an existing file.
    Both files live on the same volume. Temporary output is expendable.
    """
    marker = Path(marker)
    metadata = json.loads(marker.read_text(encoding='utf-8'))
    capture, output = Path(metadata['capture']), Path(metadata['output'])
    if output.exists():
        raise FileExistsError(f'Итоговый файл уже существует: {output}')
    source_probe = probe_media(capture)
    if not source_probe.readable or not source_probe.has_video:
        raise RuntimeError(f'Временная запись пока не читается: {source_probe.error}')
    estimated_output = int(capture.stat().st_size * max(1, metadata.get('speed') or 1))
    ensure_space(output.parent, reserve=max(estimated_output, DiskGuard.reserved_bytes(output.parent)),
                 minimum=FINALIZE_MARGIN)
    temp = output.with_name(f'.{output.stem}.{uuid.uuid4().hex}.finalizing.mp4')
    max_duration = metadata.get('max_duration')
    try:
        if metadata.get('speed'):
            cmd = build_timestretch_cmd(str(capture), str(temp), metadata['speed'],
                                       has_audio=metadata.get('has_audio', True))
        else:
            cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(capture),
                   '-map', '0:v:0', '-map', '0:a?', '-c', 'copy']
            if max_duration and (source_probe.duration or 0) > max_duration:
                cmd.extend(['-t', str(max_duration)])
            cmd.extend(['-movflags', '+faststart', str(temp)])
        with tempfile.TemporaryFile() as errors:
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=errors,
                                    timeout=max(120, (source_probe.duration or 0)*3) if metadata.get('speed') else 120)
            errors.seek(0, os.SEEK_END)
            errors.seek(max(0, errors.tell()-1500))
            tail = errors.read().decode(errors='replace')
        if result.returncode:
            raise RuntimeError(f'Не удалось собрать MP4: {tail}')
        probe = probe_media(temp)
        expected_duration = (source_probe.duration or 0) * (metadata.get('speed') or 1)
        if max_duration:
            expected_duration = min(max_duration, expected_duration)
        if (not probe.readable or not probe.has_video
                or (source_probe.has_audio and not probe.has_audio)
                or (expected_duration and (probe.duration is None or probe.duration < expected_duration-max(1, expected_duration*.01)))):
            raise RuntimeError('Проверка итогового MP4 не пройдена')
        with temp.open('rb') as handle:
            os.fsync(handle.fileno())
        os.link(temp, output)
        # Capture is deliberately retained until the new directory entry is durable.
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        marker.unlink()
        capture.unlink()
        return output
    finally:
        temp.unlink(missing_ok=True)


def pending_recordings():
    directory = Config.DATA_DIR / 'pending_recordings'
    result = []
    for marker in sorted(directory.glob('*.json')) if directory.exists() else []:
        try:
            with marker.open('r') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result.append(marker)
        except (BlockingIOError, FileNotFoundError):
            continue
    return result
