"""Проверка готовых медиафайлов через ffprobe."""
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


COMPLETED = 'completed'
PARTIAL = 'partial'
PROCESSING_ERROR = 'processing_error'


@dataclass(frozen=True)
class MediaProbeResult:
    readable: bool
    has_video: bool = False
    has_audio: bool = False
    duration: Optional[float] = None
    error: str = ''


def probe_media(path: str | Path, timeout: float = 15.0) -> MediaProbeResult:
    """Проверяет, что ffprobe читает контейнер и видит медиапотоки."""
    media_path = Path(path)
    if not media_path.is_file() or media_path.stat().st_size <= 1024:
        return MediaProbeResult(False, error='Файл не создан или слишком мал')

    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error', '-print_format', 'json',
                '-show_entries', 'stream=codec_type:format=duration',
                str(media_path),
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return MediaProbeResult(False, error='ffprobe не найден')
    except subprocess.TimeoutExpired:
        return MediaProbeResult(False, error='ffprobe не завершил проверку вовремя')
    except OSError as error:
        return MediaProbeResult(False, error=f'Не удалось запустить ffprobe: {error}')

    if result.returncode != 0:
        error = result.stderr.strip()[-300:] or f'ffprobe завершился с кодом {result.returncode}'
        return MediaProbeResult(False, error=error)

    try:
        data = json.loads(result.stdout)
        stream_types = {stream.get('codec_type') for stream in data.get('streams', [])}
        raw_duration = data.get('format', {}).get('duration')
        duration = float(raw_duration) if raw_duration not in (None, 'N/A') else None
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return MediaProbeResult(False, error=f'Некорректный ответ ffprobe: {error}')

    has_video = 'video' in stream_types
    has_audio = 'audio' in stream_types
    readable = has_video or has_audio
    return MediaProbeResult(readable, has_video, has_audio, duration,
                            '' if readable else 'В файле нет аудио- или видеопотоков')


def classify_media(path: str | Path, process_ok: bool,
                   expect_audio: bool = False) -> tuple[str, MediaProbeResult]:
    """Возвращает completed, partial или processing_error."""
    probe = probe_media(path)
    if not probe.readable or not probe.has_video:
        return PROCESSING_ERROR, probe
    if not process_ok or (expect_audio and not probe.has_audio):
        return PARTIAL, probe
    return COMPLETED, probe
