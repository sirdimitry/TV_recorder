# core/screen_capture.py
"""Захват экрана через ffmpeg (avfoundation) — запасной способ записи для
сайтов, чью прямую ссылку на поток получить не удаётся (см.
core/link_resolver.py): пользователь сам открывает такую ссылку во
встроенном окне-браузере (gui/browser_capture.py), а мы в это время
пишем именно область экрана под этим окном (не весь дисплей целиком —
см. build_screen_capture_cmd(crop=...) и core/recorder.py, который читает
реальные координаты окна из browser_capture.py и передаёт их сюда).

В отличие от всей остальной записи в проекте, здесь неизбежно идёт
перекодирование: avfoundation отдаёт сырые кадры экрана, а не уже
закодированный поток, который можно было бы просто -c copy.
"""
import re
import subprocess
from typing import Optional, Tuple

TARGET_HEIGHT = 720
BITRATE = "4M"
MAXRATE = "5M"


def get_retina_scale_factor() -> float:
    """Координаты окна из pywebview приходят в points, а avfoundation
    захватывает экран в физических пикселях — на retina-экране это не одно
    и то же (обычно 2x). Без пересчёта обрезка захвата уезжала бы вчетверо
    меньше нужной области."""
    try:
        import AppKit
        return float(AppKit.NSScreen.mainScreen().backingScaleFactor())
    except Exception:
        return 1.0


def _list_avfoundation_devices() -> str:
    result = subprocess.run(
        ['ffmpeg', '-f', 'avfoundation', '-list_devices', 'true', '-i', ''],
        capture_output=True, text=True, timeout=10,
    )
    return result.stderr


def find_device_index(name_substring: str, kind: str) -> Optional[int]:
    """kind: 'video' или 'audio'. Индексы устройств у avfoundation не
    постоянны между машинами (и даже между сессиями, если что-то виртуальное
    отключилось) — поэтому всегда ищем по подстроке имени, а не по номеру."""
    listing = _list_avfoundation_devices()
    section_re = re.compile(
        rf'AVFoundation {kind} devices:(.*?)(?:AVFoundation \w+ devices:|\Z)',
        re.IGNORECASE | re.DOTALL)
    section = section_re.search(listing)
    if not section:
        return None
    for line in section.group(1).splitlines():
        m = re.search(r'\[(\d+)]\s+(.+)', line)
        if m and name_substring.lower() in m.group(2).lower():
            return int(m.group(1))
    return None


def find_screen_device_index() -> Optional[int]:
    return find_device_index('Capture screen', 'video')


def find_loopback_audio_index() -> Optional[int]:
    """BlackHole — самый распространённый способ поймать системный звук на
    macOS. Само по себе наличие устройства не значит, что звук реально
    пишется: пользователю всё равно нужно завести Multi-Output Device в
    Audio MIDI Setup, чтобы вывод шёл одновременно на колонки и в BlackHole."""
    return find_device_index('BlackHole', 'audio')


def build_screen_capture_cmd(output_path: str, screen_index: int,
                              audio_index: Optional[int] = None,
                              crop: Optional[Tuple[int, int, int, int]] = None,
                              framerate: int = 30) -> list:
    """crop — (x, y, width, height) в пикселях (уже с поправкой на retina),
    обычно граница окна-браузера: без него пишется весь экран целиком.
    framerate выше 30 имеет смысл при ускоренном воспроизведении (см.
    core/recorder.py: start_browser_recording(speed_factor=...)) — при
    playbackRate=4 картинка на экране реально меняется в 4 раза чаще, и
    30 кадров/с исходной записи начинают заметно мылить/пропускать кадры
    после обратной растяжки по времени (build_timestretch_cmd)."""
    input_spec = f"{screen_index}:{audio_index}" if audio_index is not None else f"{screen_index}:none"
    vf = []
    if crop:
        x, y, w, h = crop
        vf.append(f'crop={w}:{h}:{x}:{y}')
    vf.append(f'scale=-2:{TARGET_HEIGHT}')
    cmd = [
        'ffmpeg', '-y',
        '-f', 'avfoundation', '-framerate', str(framerate), '-i', input_spec,
        '-vf', ','.join(vf),
        '-c:v', 'libx264', '-preset', 'veryfast',
        '-b:v', BITRATE, '-maxrate', MAXRATE, '-bufsize', '8M',
        '-pix_fmt', 'yuv420p',
    ]
    if audio_index is not None:
        # BlackHole 16ch и подобные виртуальные устройства отдают все свои
        # каналы как есть — без явного сведения в стерео aac либо откажется
        # кодировать, либо распределит звук по каналам, которые никто не
        # услышит в обычном плеере.
        cmd += ['-ac', '2', '-c:a', 'aac', '-b:a', '160k']
    cmd += ['-movflags', '+faststart', output_path]
    return cmd


def _chained_atempo(factor: float) -> str:
    """atempo принимает только 0.5-2.0 за один фильтр — для растяжки в
    большее число раз (например 0.25 = замедлить в 4 раза после записи на
    playbackRate=4) цепочку приходится собирать из нескольких фильтров
    подряд, каждый в допустимом диапазоне."""
    if 0.5 <= factor <= 2.0:
        return f'atempo={factor}'
    steps = []
    remaining = factor
    bound = 2.0 if remaining > 1.0 else 0.5
    while not (0.5 <= remaining <= 2.0):
        steps.append(bound)
        remaining /= bound
    steps.append(remaining)
    return ','.join(f'atempo={s}' for s in steps)


def build_timestretch_cmd(input_path: str, output_path: str, speed_factor: float,
                           has_audio: bool = True) -> list:
    """"Разжимает" по времени файл, записанный на ускоренном воспроизведении
    (см. gui/browser_capture.py: SPEED_CONTROL_JS) — обратная операция:
    видео замедляется в speed_factor раз (setpts), звук растягивается той
    же цепочкой atempo (см. _chained_atempo), пропорции/синхронизация не
    трогаются, звук уже пришёл с сохранённой плеером высотой тона (браузер
    сам не меняет pitch при playbackRate), atempo её тоже не трогает —
    только темп."""
    if has_audio:
        filter_complex = (
            f'[0:v]setpts={speed_factor}*PTS[v];'
            f'[0:a]{_chained_atempo(1.0 / speed_factor)}[a]'
        )
        maps = ['-map', '[v]', '-map', '[a]']
    else:
        filter_complex = f'[0:v]setpts={speed_factor}*PTS[v]'
        maps = ['-map', '[v]']
    cmd = [
        'ffmpeg', '-y', '-i', input_path,
        '-filter_complex', filter_complex,
        *maps,
        '-c:v', 'libx264', '-preset', 'veryfast',
        '-b:v', BITRATE, '-maxrate', MAXRATE, '-bufsize', '8M',
        '-pix_fmt', 'yuv420p',
    ]
    if has_audio:
        cmd += ['-c:a', 'aac', '-b:a', '160k']
    cmd += ['-movflags', '+faststart', output_path]
    return cmd
