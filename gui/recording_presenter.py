"""Pure presentation model for recording cards and filters; no filesystem or Tk IO."""
from core.disk_space import GIB, MIN_FREE, WARN_FREE

STATE_ACCENT = {state: color for color, states in (
    ('red', ('recording', 'processing_error', 'failed')),
    ('green', ('completed',)),
    ('yellow', ('joining', 'connecting', 'reconnecting', 'retrying', 'paused',
                'stopping', 'finalizing', 'checking', 'partial', 'ended_early')))
    for state in states}
LABELS = dict(recording='Запись', joining='Сборка единого MP4', connecting='Подключение',
              reconnecting='Переподключение', retrying='Повтор подключения', paused='Пауза',
              stopping='Завершение записи', finalizing='Сохранение MP4', checking='Проверка файла',
              partial='Сохранено частично', completed='Готово', ended_early='Источник завершился раньше',
              processing_error='Ошибка обработки', failed='Ошибка записи')


def task_active(task):
    return (task.is_recording or task.recovering or task.merging
            or task.phase in ('stopping', 'finalizing', 'checking'))


def task_state(task):
    if task.merging:
        return 'joining'
    if task.is_paused:
        return 'paused'
    if task_active(task) and task.phase in ('stopping', 'finalizing', 'checking'):
        return task.phase
    if task.connection_state == 'retrying' and task.recovering:
        return 'retrying'
    if task.is_recording:
        return task.connection_state if task.connection_state in ('connecting', 'reconnecting') else 'recording'
    if task.phase in ('completed', 'partial', 'processing_error') and not task.is_recording:
        return 'ended_early' if task.phase == 'completed' and task.ended_early else task.phase
    if task.recovering:
        return 'retrying'
    if task.result_status in ('partial', 'processing_error'):
        return task.result_status
    if task.success is True:
        return 'ended_early' if task.ended_early else 'completed'
    return 'failed' if task.success is False else 'finalizing'


def size_label(value):
    if value is None:
        return '—'
    value = max(0, value)
    for unit in ('Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'):
        if value < 1024 or unit == 'ТиБ':
            return f'{value:.1f} {unit}' if unit != 'Б' else f'{int(value)} Б'
        value /= 1024


def visible_tasks(tasks, selection):
    if selection == 'Активные':
        return [task for task in tasks if task_active(task)]
    if selection == 'Требуют внимания':
        return [task for task in tasks if not task_active(task) and task_state(task) in
                ('partial', 'processing_error', 'failed', 'ended_early')]
    return list(tasks)


def summary_label(tasks):
    paused = sum(task.is_paused for task in tasks)
    active = sum(task_active(task) and not task.is_paused for task in tasks)
    attention = len(visible_tasks(tasks, 'Требуют внимания'))
    ready = sum(not task_active(task) and task_state(task) == 'completed' for task in tasks)
    return f'В работе: {active} · Пауза: {paused} · Готово: {ready} · Внимание: {attention}'


def card_model(task):
    state = task_state(task)
    label = LABELS[state]
    if state == 'partial' and task.pause_count and not task.retry_count:
        label = 'Сохранено с паузами'
    if state == 'retrying':
        label += f' · попытка {task.retry_count}'
    active = task_active(task)
    counts = []
    if active:
        counts.append(f'Часть {task.part_number}')
    elif task.merged_path:
        counts.append(f'Единый MP4 · частей {len(task.parts)}')
    else:
        counts.append(f'Частей: {len(task.parts)}')
    if task.retry_count:
        counts.append(f'Повторов: {task.retry_count}')
    if task.pause_count:
        counts.append(f'Пауз: {task.pause_count}')
    counts.append(('Файл: ' if task.merged_path or not active else 'Записано: ')+size_label(task.file_size_bytes))
    disk = ''
    severity = 'muted'
    if task.storage_error:
        disk, severity = 'Диск: запись остановлена', 'red'
    elif task.disk_error:
        disk, severity = 'Диск недоступен', 'red'
    elif task.disk_free_bytes is not None and active:
        disk = 'Свободно: '+size_label(task.disk_free_bytes)
        if task.disk_free_bytes < MIN_FREE+task.disk_reserved_bytes:
            severity = 'red'
        elif task.disk_free_bytes < WARN_FREE+task.disk_reserved_bytes:
            severity = 'yellow'
    reason = task.storage_error or task.disk_error or task.storage_warning or '\n'.join(
        value for value in (task.error_message, task.merge_error) if value)
    if not reason and task.missing_seconds is not None and task.missing_seconds > 1:
        reason = f'Не хватает примерно {task.missing_seconds:.1f} с. Подробности — по кнопке «i».'
    if task.is_paused and task.is_live and not reason:
        reason = 'Эфир на паузе пропускается. Продолжение начнёт новую часть.'
    return dict(state=state, label=label, color=STATE_ACCENT[state], metrics=' · '.join(counts),
                disk=disk, disk_color=severity, reason=reason,
                can_pause=(state in ('recording', 'paused') and not task.stop_requested and not task.merging and (
                    task.is_paused and (task.is_recording or task.recovering) or
                    task.is_recording and task.connection_state == 'recording')),
                can_stop=(task.is_recording or task.recovering) and not task.merging
                         and not task.stop_requested and state not in ('finalizing', 'checking'),
                can_remove=not active, can_open=not active and task.output_available,
                can_merge=not active and len(task.parts)>1 and not task.merged_path)
