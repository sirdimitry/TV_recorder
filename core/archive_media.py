"""Desktop cancellation and exact-video encoding for the OnTVTime port."""
import asyncio
import json
import socket
import time
from pathlib import Path


class ArchiveCanceled(Exception):
    pass


class ArchiveProgress:
    def __init__(self):
        self.percent = '0%'
        self.stage = 'Проверка интервала'
        self.stop_requested = False
        self.stalled = False


def resolve_public(host, port):
    # The archive transport separately permits only OnTVTime's own hosts.
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def _probe_duration(path: Path):
    proc = await asyncio.create_subprocess_exec(
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
        if proc.returncode:
            raise ValueError('Не удалось проверить видео через ffprobe')
        return float(json.loads(out)['format']['duration'])
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def _encode(cmd, duration, progress):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    async def drain():
        tail = bytearray()
        while chunk := await proc.stderr.read(4096):
            tail.extend(chunk)
            del tail[:-4096]
        return bytes(tail)
    async def track():
        while line := await proc.stdout.readline():
            if line.startswith(b'out_time_us='):
                try:
                    value = float(line.split(b'=')[1]) / 1_000_000
                    progress.percent = f'{min(99, value / duration * 100):.0f}%'
                except ValueError:
                    pass
    stderr = asyncio.create_task(drain())
    reader = asyncio.create_task(track())
    started = time.monotonic()
    try:
        while proc.returncode is None:
            if progress.stop_requested:
                raise ArchiveCanceled('Загрузка отменена')
            if time.monotonic() - started > max(180, duration * 3):
                raise RuntimeError('Обработка видео заняла слишком много времени')
            await asyncio.sleep(.15)
        error = await stderr
        await reader
        if proc.returncode:
            raise RuntimeError(error.decode(errors='replace')[-500:])
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        await asyncio.gather(stderr, reader, return_exceptions=True)


async def _run_auto_video(inputs, outputs, *, duration, progress):
    # Same exact trim as srch-dwnld; VideoToolbox first on Apple Silicon,
    # then software H.264 if a source cannot be encoded in hardware.
    for codec in (['-c:v', 'h264_videotoolbox', '-b:v', '5000k'],
                  ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20']):
        try:
            await _encode([*inputs, '-progress', 'pipe:1', '-nostats', *codec, *outputs], duration, progress)
            return
        except ArchiveCanceled:
            raise
        except RuntimeError:
            if codec[1] == 'libx264':
                raise
