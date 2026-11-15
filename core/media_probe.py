"""Проверка готовых медиафайлов через ffprobe."""
import json
import math
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
    video_duration: Optional[float] = None
    audio_duration: Optional[float] = None


def probe_media(path: str | Path, timeout: float = 15.0) -> MediaProbeResult:
    """Проверяет, что ffprobe читает контейнер и видит медиапотоки."""
    media_path = Path(path)
    if not media_path.is_file() or media_path.stat().st_size <= 1024:
        return MediaProbeResult(False, error='Файл не создан или слишком мал')

    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error', '-print_format', 'json',
                '-show_entries', 'stream=codec_type,duration:format=duration',
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
        duration = _finite_duration(raw_duration)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return MediaProbeResult(False, error=f'Некорректный ответ ffprobe: {error}')

    has_video = 'video' in stream_types
    has_audio = 'audio' in stream_types
    readable = has_video or has_audio
    return MediaProbeResult(readable, has_video, has_audio, duration,
                            '' if readable else 'В файле нет аудио- или видеопотоков',
                            _stream_duration(data, 'video'), _stream_duration(data, 'audio'))


def _finite_duration(value):
    if value in (None, 'N/A'):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _stream_duration(data, kind):
    values = [_finite_duration(stream.get('duration')) for stream in data.get('streams', [])
              if stream.get('codec_type') == kind]
    return max((value for value in values if value is not None), default=None)


def duration_tolerance(expected):
    return max(1.0, min(5.0, (expected or 0)*.02))


@dataclass(frozen=True)
class MediaAssessment:
    status: str
    reasons: tuple[str, ...]
    expected_duration: Optional[float]
    actual_duration: Optional[float]
    missing_seconds: Optional[float]


def assess_probe(probe, process_ok, *, expect_audio=False, expected_duration=None):
    expected = _finite_duration(expected_duration)
    reasons = []
    if not probe.readable or not probe.has_video:
        reasons.append(probe.error or 'В итоговом файле отсутствует видео')
        return MediaAssessment(PROCESSING_ERROR, tuple(reasons), expected, None, None)
    if not process_ok:
        reasons.append('Запись прервана или обработка завершилась с ошибкой')
    if expect_audio and not probe.has_audio:
        reasons.append('В итоговом файле отсутствует ожидаемая звуковая дорожка')
    actual = _finite_duration(probe.video_duration) or _finite_duration(probe.duration)
    missing = max(0, expected-actual) if expected is not None and actual is not None else None
    if expected is not None:
        if actual is None:
            reasons.append('Не удалось подтвердить длительность записи')
        elif missing > duration_tolerance(expected):
            reasons.append(f'Недобор длительности: ожидалось {expected:.1f} с, сохранено {actual:.1f} с, не хватает {missing:.1f} с')
    if (expect_audio and probe.has_audio and probe.video_duration is not None
            and probe.audio_duration is not None
            and probe.video_duration-probe.audio_duration > duration_tolerance(probe.video_duration)):
        reasons.append('Звуковая дорожка заметно короче видео')
    elif (expect_audio and probe.has_audio and probe.video_duration is not None
            and probe.audio_duration is not None
            and probe.audio_duration-probe.video_duration > duration_tolerance(probe.audio_duration)):
        reasons.append('Видеодорожка заметно короче звука')
    return MediaAssessment(PARTIAL if reasons else COMPLETED, tuple(reasons), expected, actual, missing)


def classify_media(path: str | Path, process_ok: bool,
                   expect_audio: bool = False) -> tuple[str, MediaProbeResult]:
    """Возвращает completed, partial или processing_error."""
    probe = probe_media(path)
    assessment = assess_probe(probe, process_ok, expect_audio=expect_audio)
    return assessment.status, probe
