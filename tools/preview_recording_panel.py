"""Isolated native UI preview with synthetic tasks; never starts recordings."""
from pathlib import Path
import json
import tempfile
import sys
import time
from unittest.mock import Mock, patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import customtkinter as ctk
from core.recorder import Recorder, RecordingTask
from core.recording_history import RecordingHistory
from gui.recording_panel import RecordingPanel
from utils.icons import get_icon

ctk.set_appearance_mode('dark')
temporary = tempfile.TemporaryDirectory(prefix='tv-ui-preview-')
history = RecordingHistory(Path(temporary.name)/'history')
recorder = Recorder(history=history)
root = ctk.CTk()
root.title('TV Recorder — UI preview (synthetic data)')
root.geometry('600x850')
root.minsize(420, 600)
for index, state in enumerate(('recording', 'reconnecting', 'joining', 'partial')):
    task = RecordingTask(str(index), ('ТНТ · утренний эфир', 'Очень длинное название телеканала для проверки узкой панели',
                        'Россия 24 · сборка записи', 'Первый канал · сохранённая запись')[index], 'local-test', '/tmp/test-recording.mp4')
    task.start_time = time.time()-120
    task.phase = state
    task.connection_state = state
    task.is_recording = state in ('recording', 'reconnecting')
    task.recovering = state in ('reconnecting', 'joining')
    task.merging = state == 'joining'
    task.result_status = 'partial' if state == 'partial' else None
    task.success = False if state == 'partial' else None
    task.file_size_bytes = (index+1)*450*1024**2
    task.disk_free_bytes = (1.8 if index == 1 else 120)*1024**3
    task.retry_count = 1 if index in (1, 3) else 0
    task.part_number = 2 if index in (1, 3) else 1
    task.parts = [dict(path='/tmp/part1.mp4', status='partial', duration=600, has_video=True, has_audio=True)] if index in (1, 3) else []
    task.error_message = 'Не хватает примерно 68 секунд: поток оборвался и был восстановлен. Сохранён единый MP4.' if state == 'partial' else ''
    task.output_available = state == 'partial'
    task.merged_path = '/tmp/joined.mp4' if state == 'partial' else ''
    recorder.tasks[task.task_id] = task
    recorder._history_event(task, 'started', state=state)
with patch('gui.recording_panel.Storage'), patch.object(RecordingPanel, '_load_task_logo',
        lambda self, name, label: label.configure(image=get_icon('tv', self.colors['text_muted'], 20))):
    panel = RecordingPanel(root, recorder)
    panel.pack(fill='both', expand=True)

def close():
    recorder._closing = True
    history.close()
    root.destroy()
    temporary.cleanup()
root.protocol('WM_DELETE_WINDOW', close)

def layout_check():
    findings=[]
    for widgets in panel.task_widgets.values():
        row=widgets['row']
        for name in ('actions','title','name_lbl','timer','metrics','message'):
            widget=widgets[name]
            if widget.winfo_ismapped():
                findings.append(dict(name=name,left=widget.winfo_rootx()-row.winfo_rootx(),
                    right=widget.winfo_rootx()-row.winfo_rootx()+widget.winfo_width(), row_width=row.winfo_width()))
    Path('/tmp/tv-stage9-ui-layout.json').write_text(json.dumps(findings,indent=2))
root.after(1000, layout_check)
root.mainloop()
