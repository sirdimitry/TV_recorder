"""Время по часам и позиция в ролике — разные значения с похожим видом."""


def parse_clock_time(value: str) -> tuple[int, int, int] | None:
    """ЧЧ:ММ[:СС], с поддержкой старых расписаний без секунд."""
    parts = value.strip().split(':')
    if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
        return None
    hour, minute = map(int, parts[:2])
    second = int(parts[2]) if len(parts) == 3 else 0
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        return None
    return hour, minute, second


def format_clock_time(value: str) -> str | None:
    parsed = parse_clock_time(value)
    return f'{parsed[0]:02d}:{parsed[1]:02d}:{parsed[2]:02d}' if parsed else None


def parse_clip_time(value: str) -> int | None:
    """ЧЧ:ММ:СС или прежнее ММ:СС; минуты в двухчастном виде не ограничены."""
    parts = value.strip().split(':')
    if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
        return None
    numbers = list(map(int, parts))
    if len(numbers) == 2:
        minute, second = numbers
        return minute * 60 + second if second < 60 else None
    hour, minute, second = numbers
    return hour * 3600 + minute * 60 + second if minute < 60 and second < 60 else None


def format_clip_time(total_seconds: float) -> str:
    hours, remainder = divmod(round(total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'
