# core/snapshot.py
"""Быстрый захват одного кадра из потока через ffmpeg — лёгкая замена живому
видео там, где нужно просто "видеть, что там сейчас показывают": один
JPEG-кадр стоит копейки по CPU/сети, в отличие от постоянно работающего
декодера, и его тривиально показать как обычную картинку в Tk-виджете."""
import subprocess
from typing import Optional

SNAPSHOT_WIDTH = 320
TIMEOUT = 6
DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def grab_snapshot(url: str, headers: Optional[dict] = None,
                   width: int = SNAPSHOT_WIDTH, timeout: int = TIMEOUT) -> Optional[bytes]:
    """Возвращает байты одного JPEG-кадра либо None — никогда не бросает исключений
    (вызывается из фоновых потоков, где падать нежелательно)."""
    if not url:
        return None

    if headers:
        header_str = ''.join(f"{k}: {v}\r\n" for k, v in headers.items())
    else:
        header_str = f"User-Agent: {DEFAULT_UA}\r\n"

    cmd = [
        'ffmpeg', '-y',
        '-headers', header_str,
        '-analyzeduration', '2000000',
        '-probesize', '1000000',
        '-i', url,
        '-vframes', '1',
        '-vf', f'scale={width}:-1',
        '-q:v', '5',
        '-f', 'image2pipe',
        '-vcodec', 'mjpeg',
        'pipe:1',
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode == 0 and result.stdout and len(result.stdout) > 200:
            return result.stdout
    except Exception:
        pass
    return None
