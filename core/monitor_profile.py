"""Adaptive thumbnail budgets; recording quality is independent of these values."""
from dataclasses import dataclass

MODES = ('economy', 'normal', 'off')


@dataclass(frozen=True)
class MonitorProfile:
    fps: float
    width: int
    keyframes_only: bool


def monitor_profile(mode, active_count):
    if mode not in MODES:
        raise ValueError('Неизвестный режим монитора')
    if mode == 'off' or active_count <= 0:
        return None
    if mode == 'economy':
        return MonitorProfile(1 if active_count <= 4 else .5, 220, True)
    return MonitorProfile(4 if active_count <= 3 else 2 if active_count <= 8 else 1,
                          320 if active_count <= 3 else 280 if active_count <= 8 else 220, False)
