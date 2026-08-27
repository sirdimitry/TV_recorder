# packaging/TVRecorder.spec
"""PyInstaller spec для сборки TV Recorder в самостоятельный macOS .app —
собирается через `pyinstaller packaging/TVRecorder.spec` (обычно из
packaging/build.sh, который следом докладывает ffmpeg/ffplay/ffprobe и
подписывает бандл, см. этот файл)."""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

REPO_ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

pywebview_datas, pywebview_binaries, pywebview_hidden = collect_all('pywebview')
customtkinter_datas = collect_data_files('customtkinter')
# playwright (core/tass_provider.py) — collect_all нужен, чтобы подхватить
# его собственный Node-бинарник и JS-драйвер (playwright/driver/**), они не
# .py/.so и PyInstaller их без явного collect_all не найдёт. Сам браузер
# (Chrome/Chromium) НЕ бандлится — provider сам находит системный Chrome
# или при необходимости скачивает Chromium при первом использовании этой
# функции (см. TassProvider._ensure_bundled_chromium), а не при сборке.
playwright_datas, playwright_binaries, playwright_hidden = collect_all('playwright')

a = Analysis(
    [str(REPO_ROOT / 'main.py')],
    pathex=[str(REPO_ROOT)],
    binaries=[*pywebview_binaries, *playwright_binaries],
    datas=[
        (str(REPO_ROOT / 'VERSION'), '.'),
        (str(REPO_ROOT / 'data' / 'default_channels.json'), 'data'),
        *pywebview_datas,
        *customtkinter_datas,
        *playwright_datas,
    ],
    hiddenimports=[
        *pywebview_hidden,
        *playwright_hidden,
        'AppKit', 'Quartz', 'Security', 'UniformTypeIdentifiers', 'WebKit', 'objc',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TV Recorder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='TV Recorder',
)

app = BUNDLE(
    coll,
    name='TV Recorder.app',
    icon=str(REPO_ROOT / 'packaging' / 'icon.icns'),
    bundle_identifier='com.sirdimitry.tvrecorder',
    info_plist={
        'CFBundleShortVersionString': (REPO_ROOT / 'VERSION').read_text(encoding='utf-8').strip(),
        'NSHighResolutionCapable': True,
        'NSHumanReadableCopyright': 'MIT License',
    },
)
