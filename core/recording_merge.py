"""Join compatible MP4 parts without encoding or removing the originals."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from core.disk_space import DiskGuard, FINALIZE_MARGIN, ensure_space
from core.media_probe import probe_media

SIGNATURE_FIELDS = ('codec_type', 'codec_name', 'profile', 'level', 'width', 'height',
                    'pix_fmt', 'sample_rate', 'channels', 'channel_layout', 'time_base',
                    'extradata_hash')


def _signature(path):
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_streams', '-show_data_hash', 'sha256',
             '-of', 'json', str(path)], capture_output=True, text=True, timeout=15, check=True)
        streams = json.loads(result.stdout).get('streams', [])
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ValueError(f'Не удалось прочитать параметры части «{path.name}». Исходники сохранены.') from error
    if not any(stream.get('codec_type') == 'video' for stream in streams):
        raise ValueError(f'В части отсутствует видео: {path.name}')
    return [tuple(stream.get(key) for key in SIGNATURE_FIELDS) for stream in streams]


def merge_mp4_parts(paths, output):
    paths = [Path(path).resolve() for path in paths]
    output = Path(output).resolve()
    if len(paths) < 2 or len(set(paths)) != len(paths):
        raise ValueError('Для объединения нужны как минимум две разные части')
    if output.exists():
        raise FileExistsError(f'Файл уже существует: {output}')
    signatures = [_signature(path) for path in paths]
    if any(signature != signatures[0] for signature in signatures[1:]):
        raise ValueError('Параметры частей различаются. Объединение без перекодирования отменено; исходники сохранены.')
    probes = [probe_media(path) for path in paths]
    if any(not probe.readable or not probe.has_video or not probe.duration for probe in probes):
        raise ValueError('Не все части читаются или имеют известную длительность')
    reserve = sum(path.stat().st_size for path in paths)
    ensure_space(output.parent, reserve=reserve+DiskGuard.reserved_bytes(output.parent),
                 minimum=FINALIZE_MARGIN, writable=True)
    temporary = output.with_name(f'.{output.stem}.{uuid.uuid4().hex}.joining.mp4')
    try:
        with tempfile.TemporaryDirectory(prefix='.tv-join-', dir=output.parent) as directory:
            manifest = Path(directory)/'parts.ffconcat'
            lines = ['ffconcat version 1.0']
            for path in paths:
                if '\n' in str(path) or '\r' in str(path):
                    raise ValueError('Имя части содержит перенос строки')
                escaped = str(path).replace("'", "'\\''")
                lines.append(f"file '{escaped}'")
            manifest.write_text('\n'.join(lines)+'\n', encoding='utf-8')
            with tempfile.TemporaryFile() as errors:
                result = subprocess.run(
                    ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'concat',
                     '-safe', '0', '-auto_convert', '0', '-i', str(manifest),
                     '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
                     '-movflags', '+faststart', str(temporary)],
                    stdout=subprocess.DEVNULL, stderr=errors, timeout=120)
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell()-1500))
                tail = errors.read().decode(errors='replace')
            if result.returncode:
                raise RuntimeError(f'Не удалось объединить части: {tail}')
        merged = probe_media(temporary)
        expected = sum(probe.duration for probe in probes)
        if (not merged.has_video or merged.has_audio != probes[0].has_audio
                or merged.duration is None or abs(merged.duration-expected) > max(1, expected*.01)):
            raise RuntimeError('Проверка объединённого файла не пройдена; исходники сохранены')
        if _signature(temporary) != signatures[0]:
            raise RuntimeError('Параметры объединённого файла изменились; исходники сохранены')
        with temporary.open('rb') as handle:
            os.fsync(handle.fileno())
        os.link(temporary, output)
        fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return output
    finally:
        temporary.unlink(missing_ok=True)
