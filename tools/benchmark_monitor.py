"""Offline benchmark: real Recorder + thumbnails, paced local 720p fixture.
CPU includes this Python process and reaped FFmpeg/ffprobe children.
RSS is the largest reaped child's peak, NOT total application RSS.
No GUI, network or production settings are involved.
"""
import argparse
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def usage():
    own = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + child.ru_utime + child.ru_stime


def scenario(fixture, count, thumbnails, seconds, mode):
    from core.recorder import Recorder
    from utils.config import Config
    recorder = Recorder()
    recorder.set_snapshot_mode(mode, persist=False)
    real_popen = subprocess.Popen

    def paced(cmd, *args, **kwargs):
        cmd = list(cmd)
        # Recorder normally consumes HTTP. For a local fixture, remove only
        # protocol-specific input options (no application code is changed).
        if cmd[0] == 'ffmpeg':
            from core.stream_resolver import RECONNECT_OPTS
            network_options = set(RECONNECT_OPTS[::2]) | {'-headers'}
            filtered = []
            i = 0
            while i < len(cmd):
                if cmd[i] in network_options:
                    i += 2
                else:
                    filtered.append(cmd[i])
                    i += 1
            cmd = filtered
        if cmd[0] == 'ffmpeg' and '-i' in cmd and '-re' not in cmd:
            cmd.insert(cmd.index('-i'), '-re')
        return real_popen(cmd, *args, **kwargs)

    with tempfile.TemporaryDirectory(prefix='tv-baseline-') as directory:
        Config.RECORDINGS_DIR = Path(directory)
        Config.SETTINGS_FILE = Path(directory) / 'settings.json'
        before = usage()
        started = time.monotonic()
        tasks = []
        with patch('core.recorder.subprocess.Popen', side_effect=paced):
            try:
                if thumbnails:
                    recorder.acquire_snapshot_consumer()
                for i in range(count):
                    task_id = recorder.start_recording(
                        f'baseline-{i}', fixture, str(Path(directory) / f'{i}.mp4'),
                        duration_limit_seconds=seconds)
                    if not task_id:
                        raise RuntimeError('Recorder did not start')
                    tasks.append(recorder.tasks[task_id])
                deadline = time.monotonic() + seconds + 20
                while any(t.result_status is None for t in tasks):
                    if time.monotonic() > deadline:
                        raise TimeoutError('Recording did not finish')
                    time.sleep(.1)
            finally:
                recorder.stop_all()
        wall = time.monotonic() - started
        return dict(recordings=count, thumbnails=thumbnails, mode=mode, wall_seconds=round(wall, 3),
                    cpu_seconds=round(usage()-before, 3),
                    cpu_percent_one_core=round(100*(usage()-before)/wall, 1),
                    largest_child_peak_rss_mib=round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss/1024**2, 1),
                    statuses=[t.result_status for t in tasks],
                    frames=[t.snapshot_seq for t in tasks],
                    output_bytes=sum(Path(t.output_path).stat().st_size for t in tasks))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture')
    parser.add_argument('--count', type=int, default=1)
    parser.add_argument('--thumbnails', action='store_true')
    parser.add_argument('--seconds', type=int, default=8)
    parser.add_argument('--mode', choices=['economy','normal','off'], default='economy')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.fixture:
        result = scenario(args.fixture, args.count, args.thumbnails, args.seconds, args.mode)
    else:
        with tempfile.TemporaryDirectory(prefix='tv-fixture-') as directory:
            fixture = str(Path(directory) / 'source.mp4')
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=size=1280x720:rate=25', '-f', 'lavfi', '-i',
                            'sine=frequency=1000:sample_rate=48000', '-t', '30',
                            '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '50', '-b:v', '3M',
                            '-c:a', 'aac', fixture], check=True)
            result = []
            for count, thumbnails in ((1, False), (1, True), (4, False), (4, True)):
                output = Path(directory) / 'result.json'
                cmd = [sys.executable, __file__, '--fixture', fixture, '--count', str(count),
                       '--seconds', str(args.seconds), '--mode', args.mode, '--output', str(output)]
                if thumbnails:
                    cmd.append('--thumbnails')
                subprocess.run(cmd, check=True, timeout=args.seconds+45)
                result.append(json.loads(output.read_text()))
    Path(args.output).write_text(json.dumps(result, indent=2)+'\n')


if __name__ == '__main__':
    main()
