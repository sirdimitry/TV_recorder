# core/snapshot.py
"""Быстрый захват одного кадра из потока через ffmpeg — лёгкая замена живому
видео там, где нужно просто "видеть, что там сейчас показывают": один
JPEG-кадр стоит копейки по CPU/сети, в отличие от постоянно работающего
декодера, и его тривиально показать как обычную картинку в Tk-виджете."""
import io
import subprocess
from typing import Optional, Tuple

SNAPSHOT_WIDTH = 320
# Замеры по реальным каналам показали разброс 0.3-5.5с — 6с таймаут был
# впритык для медленных, но рабочих потоков (ложные "нет сигнала").
TIMEOUT = 10
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
        '-allowed_extensions', 'ALL',
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


def to_ctk_image(jpeg_bytes: bytes, size: Tuple[int, int]):
    """JPEG-байты -> CTkImage, вписанные в size с обрезкой по центру (не
    сжатием) — общая логика для панели записей и окна-монитора."""
    from PIL import Image
    import customtkinter as ctk

    w, h = size
    pil_img = Image.open(io.BytesIO(jpeg_bytes)).convert('RGB')
    src_ratio = pil_img.width / pil_img.height
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        new_height = h
        new_width = max(w, round(h * src_ratio))
    else:
        new_width = w
        new_height = max(h, round(w / src_ratio))
    resized = pil_img.resize((new_width, new_height), Image.LANCZOS)
    left = (new_width - w) // 2
    top = (new_height - h) // 2
    cropped = resized.crop((left, top, left + w, top + h))
    return ctk.CTkImage(light_image=cropped, dark_image=cropped, size=size)
