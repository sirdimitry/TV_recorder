#!/bin/bash
# packaging/make_dmg.sh — упаковывает уже собранный packaging/dist/TV
# Recorder.app (см. packaging/build.sh) в обычный macOS drag-to-Applications
# .dmg, только встроенными утилитами (hdiutil) — без установки create-dmg.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_PATH="$REPO_ROOT/packaging/dist/TV Recorder.app"
DMG_NAME="TV Recorder"
OUT_DMG="$REPO_ROOT/packaging/TV Recorder.dmg"
VOLUME_NAME="TV Recorder"

if [ ! -d "$APP_PATH" ]; then
    echo "Не найден $APP_PATH — сначала запустите packaging/build.sh" >&2
    exit 1
fi

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

# Без -nobrowse и со стандартной точкой монтирования /Volumes/<имя> — Finder
# должен реально "видеть" том, иначе AppleScript ниже не найдёт `disk
# "$VOLUME_NAME"` (проверено: с -nobrowse Finder том не индексирует вообще).
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
MOUNT_NAME=$(basename "$MOUNT_POINT")
sleep 2

osascript <<OSA
tell application "Finder"
    tell disk "$MOUNT_NAME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {200, 120, 720, 420}
        set viewOptions to the icon view options of container window
        set arrangement of viewOptions to not arranged
        set icon size of viewOptions to 96
        set position of item "TV Recorder.app" of container window to {130, 150}
        set position of item "Applications" of container window to {390, 150}
        close
        open
        update without registering applications
    end tell
end tell
OSA

sync
hdiutil detach "$MOUNT_DEVICE" -quiet
MOUNT_DEVICE=""

rm -f "$OUT_DMG"
hdiutil convert "$RW_DMG" -format UDZO -o "$OUT_DMG" >/dev/null
rm -f "$RW_DMG"
RW_DMG=""

echo "Готово: $OUT_DMG"
