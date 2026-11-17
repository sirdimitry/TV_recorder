"""Isolated yt-dlp worker: stable partial files, fresh URLs and pinned formats."""
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit


def supported(url, clip_end=None):
    host = (urlsplit(url).hostname or '').lower()
    return clip_end is None and (host == 'youtu.be' or host == 'youtube.com' or host.endswith('.youtube.com'))


def identity(info):
    formats = info.get('requested_formats') or [info]
    return {'id': info.get('id'), 'duration': info.get('duration'), 'formats': [
        {key: item.get(key) for key in ('format_id', 'ext', 'vcodec', 'acodec', 'filesize')}
        for item in formats]}


def compatible(old, new):
    if old['id'] != new['id'] or len(old['formats']) != len(new['formats']):
        return False
    if old.get('duration') and new.get('duration') and abs(old['duration']-new['duration']) > 1:
        return False
    return all(a.get(key) == b.get(key) for a, b in zip(old['formats'], new['formats'])
               for key in ('format_id', 'ext', 'vcodec', 'acodec', 'filesize')
               if a.get(key) is not None and b.get(key) is not None)


def emit(event, **values):
    print(json.dumps(dict(event=event, **values)), flush=True)


def main():
    from yt_dlp import YoutubeDL
    url, folder, height = sys.argv[1:4]
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    checkpoint = folder/'identity.json'
    old = json.loads(checkpoint.read_text()) if checkpoint.exists() else None
    if old is None and any(folder.glob('media*')):
        emit('failed', fatal=True, message='Нет описания форматов сохранённых частей. Они оставлены без изменений; начните отдельную загрузку.')
        return 1
    selector = '+'.join(item['format_id'] for item in old['formats']) if old else (
        f'bestvideo[height<={int(height)}][ext=mp4]+bestaudio[ext=m4a]/best[height<={int(height)}][ext=mp4]')

    class QuietLogger:
        def debug(self, message):
            pass
        def warning(self, message):
            pass
        def error(self, message):
            # Signed URLs/cookies from yt-dlp diagnostics must not enter history.
            pass

    last_emit = [0.]
    def progress(data):
        if time.monotonic()-last_emit[0] < .3 and data.get('status') != 'finished':
            return
        last_emit[0] = time.monotonic()
        details = data.get('info_dict', {})
        emit('progress', format_id=details.get('format_id', 'media'),
             downloaded=data.get('downloaded_bytes', 0),
             total=data.get('total_bytes') or data.get('total_bytes_estimate'),
             estimated=not bool(data.get('total_bytes')), finished=data.get('status') == 'finished',
             speed=data.get('speed'), eta=data.get('eta'))

    opts = dict(format=selector, outtmpl=str(folder/'media.%(ext)s'), noplaylist=True,
                continuedl=True, nopart=False, retries=2, fragment_retries=2,
                skip_unavailable_fragments=False, socket_timeout=20, concurrent_fragment_downloads=1,
                merge_output_format='mp4', keepvideo=True, overwrites=False,
                progress_hooks=[progress], logger=QuietLogger(),
                postprocessor_hooks=[lambda data: emit('processing')])
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info or info.get('_type') in ('playlist', 'multi_video') or info.get('is_live'):
                raise ValueError('Для паузы требуется отдельное завершённое видео')
            current = identity(info)
            if old and not compatible(old, current):
                raise ValueError('Форматы видео изменились. Части сохранены; продолжение отменено для защиты файла')
            temporary = checkpoint.with_suffix('.tmp')
            with temporary.open('w') as handle:
                json.dump(current, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, checkpoint)
            streams = []
            for item in info.get('requested_formats') or [info]:
                size = item.get('filesize') or item.get('filesize_approx')
                if not size and item.get('tbr') and info.get('duration'):
                    size = item['tbr']*1000*info['duration']/8
                streams.append(dict(id=item.get('format_id', 'media'), total=size,
                                    estimated=not bool(item.get('filesize'))))
            emit('metadata', title=info.get('title'), thumbnail=info.get('thumbnail'),
                 duration=info.get('duration'), streams=streams)
            ydl.process_info(info)
            emit('finished')
    except KeyboardInterrupt:
        return 130
    except ValueError as error:
        emit('failed', fatal=True, message=str(error))
        return 1
    except Exception:
        emit('failed', message='Источник недоступен, изменился либо не завершил передачу. Временные части сохранены.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
