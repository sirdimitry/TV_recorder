"""Aggregate bytes across separately downloaded video/audio formats."""


def configure_streams(task, streams):
    previous = task.stream_progress
    task.stream_progress = {
        item['id']: dict(downloaded=previous.get(item['id'], {}).get('downloaded', 0),
                         total=item.get('total'), estimated=item.get('estimated', False))
        for item in streams
    }
    aggregate(task)


def update_progress(task, event):
    stream = task.stream_progress.setdefault(event['format_id'], {})
    stream['downloaded'] = max(0, event.get('downloaded') or 0)
    if event.get('total'):
        stream['total'] = event['total']
        stream['estimated'] = event.get('estimated', False)
    if event.get('finished'):
        stream['total'] = stream['downloaded']
        stream['estimated'] = False
    task.speed_bps = event.get('speed')
    aggregate(task)


def aggregate(task):
    streams = list(task.stream_progress.values())
    task.downloaded_bytes = sum(item.get('downloaded', 0) for item in streams)
    known = bool(streams) and all(item.get('total') and item['total'] > 0 for item in streams)
    task.total_bytes = sum(max(item['total'], item.get('downloaded', 0)) for item in streams) if known else None
    task.total_is_estimate = any(item.get('estimated', False) for item in streams) if known else False
    task.progress = min(99., 100*task.downloaded_bytes/task.total_bytes) if known else None
    remaining = max(0, task.total_bytes-task.downloaded_bytes) if known else None
    task.eta_seconds = remaining/task.speed_bps if remaining is not None and task.speed_bps and task.speed_bps > 0 else None
