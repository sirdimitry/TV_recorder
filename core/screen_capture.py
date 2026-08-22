# core/screen_capture.py
"""Захват экрана через ffmpeg (avfoundation) — запасной способ записи для
сайтов, чью прямую ссылку на поток получить не удаётся (см.
core/link_resolver.py): пользователь сам открывает такую ссылку во
встроенном окне-браузере (gui/browser_capture.py) и включает fullscreen
в плеере страницы, а мы в это время просто пишем экран.

В отличие от всей остальной записи в проекте, здесь неизбежно идёт
перекодирование: avfoundation отдаёт сырые кадры экрана, а не уже
закодированный поток, который можно было бы просто -c copy.
"""
import re
import subprocess
from typing import Optional

TARGET_HEIGHT = 720
BITRATE = "4M"
MAXRATE = "5M"


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
                              audio_index: Optional[int] = None) -> list:
    input_spec = f"{screen_index}:{audio_index}" if audio_index is not None else f"{screen_index}:none"
    cmd = [
        'ffmpeg', '-y',
        '-f', 'avfoundation', '-framerate', '30', '-i', input_spec,
        '-vf', f'scale=-2:{TARGET_HEIGHT}',
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
