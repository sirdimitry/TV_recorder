"""Read-only presentation of durable recording history."""
from datetime import datetime

STATUS_LABELS = {
    'completed': 'Завершено', 'partial': 'Сохранено частично',
    'processing_error': 'Ошибка обработки', 'interrupted': 'Прервано закрытием приложения',
    'recording': 'Запись', 'paused': 'Пауза', 'connecting': 'Подключение',
    'reconnecting': 'Переподключение', 'retrying': 'Восстановление',
    'finalizing': 'Завершение файла', 'history_error': 'Ошибка истории',
}
EVENT_LABELS = {
    'started': 'Начало записи', 'connection_state': 'Состояние потока',
    'stalled': 'Поток перестал продвигаться', 'part_started': 'Начало новой части',
    'part_finished': 'Часть сохранена и проверена', 'retry_scheduled': 'Повтор запланирован',
    'paused': 'Пауза: эфир пропускается', 'resumed': 'Продолжение после паузы',
    'stop_requested': 'Запрошена остановка', 'shutdown': 'Выход из приложения',
    'completed': 'Запись завершена', 'interrupted': 'Аварийное завершение',
    'parts_merge_started': 'Автоматическая сборка итогового файла',
    'parts_merge_failed': 'Не удалось автоматически объединить части',
    'parts_merged': 'Части объединены', 'file_recovered': 'Файл восстановлен после аварии',
}


def seconds(value):
    return f'{value:.1f} с' if value is not None else 'неизвестно'


def date_label(value):
    try:
        return datetime.fromisoformat(value).strftime('%d.%m.%Y %H:%M:%S')
    except (ValueError, TypeError):
        return '—'


def format_record(record):
    session = record['session']
    lines = [session.get('name', 'Запись'),
             'Статус: '+STATUS_LABELS.get(session.get('state'), session.get('state', '—')),
             'Начало: '+date_label(session.get('started_at')),
             'Завершение: '+date_label(session.get('finished_at')),
             'Ожидалось: '+seconds(session.get('expected_seconds')),
             'Сохранено: '+seconds(session.get('actual_seconds')),
             'Недобор (оценка): '+seconds(session.get('missing_seconds')),
             f"Повторы: {session.get('retries', 0)}; паузы: {session.get('pauses', 0)}"]
    if session.get('error'):
        lines.extend(['', 'Причина: '+session['error']])
    for i, part in enumerate(session.get('parts', []), 1):
        lines.extend(['', f'Часть {i}: '+part.get('path', '—'),
                      'Статус: '+STATUS_LABELS.get(part.get('status'), part.get('status', '—')),
                      'Длительность: '+seconds(part.get('duration')),
                      'Ожидалось: '+seconds(part.get('expected_duration')),
                      'Недобор: '+seconds(part.get('missing_seconds')),
                      'Видео: '+('есть' if part.get('has_video') else 'нет')+
                      '; звук: '+('есть' if part.get('has_audio') else 'нет')])
        if part.get('error'):
            lines.append('Причина: '+part['error'])
    if session.get('merged_path'):
        lines.extend(['', 'Объединённый файл: '+session['merged_path']])
    lines.extend(['', 'События:'])
    for event in record.get('events', []):
        detail = event.get('detail', {})
        label = EVENT_LABELS.get(event['type'], event['type'])
        if event['type'] == 'connection_state':
            label += ': '+STATUS_LABELS.get(detail.get('state'), detail.get('state', '—'))
        if event['type'] == 'retry_scheduled':
            label += f" (попытка {detail.get('attempt')}, через {detail.get('delay')} с)"
        lines.append(date_label(event.get('at'))+' — '+label)
    return '\n'.join(lines)
