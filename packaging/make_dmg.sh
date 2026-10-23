#!/bin/bash
# packaging/make_dmg.sh — упаковывает уже собранный packaging/dist/TV
# Recorder.app (см. packaging/build.sh) в обычный macOS drag-to-Applications
# .dmg, только встроенными утилитами (hdiutil) — без установки create-dmg.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_PATH="$REPO_ROOT/packaging/dist/TV Recorder.app"
DMG_NAME="TV Recorder"

VOLUME_NAME="TV Recorder"

if [ ! -d "$APP_PATH" ]; then
    echo "Не найден $APP_PATH — сначала запустите packaging/build.sh" >&2
    exit 1
fi

APP_VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP_PATH/Contents/Info.plist")
OUT_DMG="$REPO_ROOT/packaging/TV_Recorder_v${APP_VERSION}_Apple_Silicon.dmg"

STAGE_DIR=$(mktemp -d)
RW_DMG=""
MOUNT_DEVICE=""
cleanup() {
    if [ -n "$MOUNT_DEVICE" ]; then
        hdiutil detach "$MOUNT_DEVICE" -quiet 2>/dev/null || true
    fi
    rm -rf "$STAGE_DIR"
    [ -z "$RW_DMG" ] || rm -f "$RW_DMG"
}
trap cleanup EXIT

cp -R "$APP_PATH" "$STAGE_DIR/"
ln -s /Applications "$STAGE_DIR/Applications"

RW_DMG=$(mktemp -u).dmg
hdiutil create -volname "$VOLUME_NAME" -srcfolder "$STAGE_DIR" -ov -format UDRW "$RW_DMG" >/dev/null

# Finder appearance is written as metadata without driving the user's Finder.
# Build-only dependencies: python3 -m pip install -r packaging/requirements.txt
ATTACH_PLIST=$(mktemp)
hdiutil attach "$RW_DMG" -readwrite -noautoopen -plist > "$ATTACH_PLIST"
MOUNT_DEVICE=$(python3 - "$ATTACH_PLIST" <<'PY'
import plistlib, sys
with open(sys.argv[1], 'rb') as source:
    entities = plistlib.load(source).get('system-entities', [])
print(next(entity['dev-entry'] for entity in entities if entity.get('mount-point')))
PY
)
MOUNT_POINT=$(python3 - "$ATTACH_PLIST" <<'PY'
import plistlib, sys
with open(sys.argv[1], 'rb') as source:
    entities = plistlib.load(source).get('system-entities', [])
print(next(entity['mount-point'] for entity in entities if entity.get('mount-point')))
PY
)
rm -f "$ATTACH_PLIST"
python3 - "$MOUNT_POINT" <<'PYMETADATA'
from pathlib import Path
import sys
from ds_store import DSStore
from mac_alias import Alias

volume = Path(sys.argv[1])
background = volume / 'TV Recorder.app/Contents/Resources/dmg_background.png'
with DSStore.open(str(volume / '.DS_Store'), 'w+') as store:
    store['.']['bwsp'] = {
        'ShowStatusBar': False, 'ShowToolbar': False, 'ShowPathbar': False,
        'ShowSidebar': False, 'SidebarWidth': 0,
        'WindowBounds': '{{200, 120}, {600, 485}}',
    }
    store['.']['icvp'] = {
        'viewOptionsVersion': 1, 'backgroundType': 2,
        'backgroundImageAlias': Alias.for_file(str(background)).to_bytes(),
        'iconSize': 112.0, 'textSize': 12.0, 'labelOnBottom': True,
        'arrangeBy': 'none', 'gridSpacing': 100.0, 'gridOffsetX': 0.0,
        'gridOffsetY': 0.0, 'showItemInfo': False, 'showIconPreview': False,
    }
    store['.']['vSrn'] = ('long', 1)
    store['.']['vstl'] = ('type', b'icnv')
    store['TV Recorder.app']['Iloc'] = (150, 145)
    store['Applications']['Iloc'] = (450, 145)
PYMETADATA

sync
if [ -d "$MOUNT_POINT/.fseventsd" ]; then
    find "$MOUNT_POINT/.fseventsd" -depth -delete
fi
hdiutil detach "$MOUNT_DEVICE" -quiet
MOUNT_DEVICE=""

rm -f "$OUT_DMG"
hdiutil convert "$RW_DMG" -format UDZO -o "$OUT_DMG" >/dev/null
rm -f "$RW_DMG"
RW_DMG=""

echo "Готово: $OUT_DMG"
