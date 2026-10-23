"""Present one channel per network while retaining every playable version."""
from collections import Counter
from dataclasses import dataclass
import re


def normalized(value):
    return ' '.join(value.casefold().replace('ё', 'е').split())


def channel_identity(channel):
    name = channel.name.strip()
    shift = 0
    match = re.search(r'\s*\(\s*\+(\d+)\s*\)\s*$', name)
    if match:
        shift = int(match[1])
        name = name[:match.start()].strip()
    group = getattr(channel, 'group', '')
    region = ''
    if group not in ('', 'Основные', 'Мои потоки'):
        match = re.search(r'\s*\(([^()]+)\)\s*$', name)
        if match and normalized(match[1]) not in ('hd', 'sd', 'uhd', '4k'):
            region = match[1].strip()
            name = name[:match.start()].strip()
        else:
            region = group
    aliases = {'первый': 'Первый канал', 'первый канал': 'Первый канал',
               'пятница!': 'Пятница', 'пятница': 'Пятница', 'мир': 'Мир'}
    name = aliases.get(normalized(name), name)
    label = region or ('Основной' if shift == 0 else f'+{shift} ч')
    if region and shift:
        label += f' · +{shift} ч'
    return name, label, shift, region


@dataclass
class ChannelFamily:
    key: str
    name: str
    versions: tuple

    def choices(self, region='Все регионы'):
        if region != 'Все регионы':
            return tuple(c for c in self.versions if getattr(c, 'group', '') == region)
        national = tuple(c for c in self.versions if not channel_identity(c)[3])
        return national or self.versions

    def version_labels(self, results):
        names = [channel_identity(c)[1] for c in self.versions]
        totals, counts = Counter(names), Counter()
        labels = {}
        states = {'available': 'архив', 'live_only': 'эфир', 'unavailable': 'недоступен',
                  'unknown': 'не проверен'}
        for channel, name in zip(self.versions, names):
            counts[name] += 1
            if totals[name] > 1:
                name += f' · {counts[name]}'
            state = states.get(results.get(channel.slug, {}).get('state'), 'не проверен')
            labels[f'{name} — {state}'] = channel
        return labels

    def preferred(self, results):
        def rank(channel):
            _, _, shift, region = channel_identity(channel)
            state = results.get(channel.slug, {}).get('state')
            return ({'available': 0, 'live_only': 2, 'unavailable': 3}.get(state, 1),
                    bool(region), shift, getattr(channel, 'group', '') != 'Мои потоки')
        return min(self.versions, key=rank)

    def state(self, results):
        states = {results.get(c.slug, {}).get('state', 'unknown') for c in self.versions}
        return next((s for s in ('available', 'live_only', 'unknown', 'unavailable') if s in states), 'unknown')

    def matches(self, query='', region='Все регионы', state=None, results=None):
        query = normalized(query)
        for channel in self.versions:
            if region != 'Все регионы' and getattr(channel, 'group', '') != region:
                continue
            if state and (results or {}).get(channel.slug, {}).get('state') != state:
                continue
            if query in normalized(channel.name + ' ' + getattr(channel, 'group', '') + ' ' + self.name):
                return True
        return False


def group_channels(channels):
    groups = {}
    for channel in channels:
        name, _, _, _ = channel_identity(channel)
        key = normalized(name)
        if key not in groups:
            groups[key] = ChannelFamily(key, name, ())
        groups[key].versions += (channel,)
    return tuple(groups.values())
