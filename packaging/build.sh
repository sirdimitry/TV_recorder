#!/bin/bash
# packaging/build.sh — собирает TV Recorder.app целиком: PyInstaller,
# вшивание статических ffmpeg/ffplay/ffprobe под обе архитектуры (Homebrew-
# сборка динамически линкована и не переносится на чужой Mac — здесь нужны
# самодостаточные бинарники), ad-hoc подпись. Результат: packaging/dist/TV
# Recorder.app — готов для packaging/make_dmg.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGING_DIR="$REPO_ROOT/packaging"
DIST_DIR="$PACKAGING_DIR/dist"
BUILD_DIR="$PACKAGING_DIR/build"
FFMPEG_CACHE="$PACKAGING_DIR/ffmpeg"
APP_PATH="$DIST_DIR/TV Recorder.app"

cd "$REPO_ROOT"

echo "==> Иконка"
python3 packaging/generate_icon.py

echo "==> PyInstaller (--distpath/--workpath внутрь packaging/, не трогаем корень репозитория)"
python3 -m PyInstaller packaging/TVRecorder.spec --noconfirm \
    --distpath "$DIST_DIR" --workpath "$BUILD_DIR"

echo "==> Статические ffmpeg/ffplay/ffprobe (обе архитектуры — не спрашиваем, какой Mac у друга)"
# Простой список вместо ассоциативного массива — /bin/bash на macOS это
# древний bash 3.2 (лицензионные причины Apple), declare -A там не работает.
mkdir -p "$FFMPEG_CACHE"
# Именно 7.1, а не более новая версия — на 9.x (arm64)/8.0 (intel), которые
# тут были раньше, поймали реальный баг: HLS-плейлисты с байт-рейнджами одного
# файла (#EXT-X-BYTERANGE без явного @offset у второго и следующих сегментов —
# так отдаёт, например, ntv.ru через Shaka Packager) у них читаются только на
# первый сегмент, дальше запись видео молча обрывается (звук при этом
# докачивается полностью — он идёт отдельным, обычным mp4, а не байт-рейндж
# плейлистом). Причём именно на 720p — ниже потому что вариант ниже разрешения
# может быть не байт-рейндж плейлистом, там бага не будет. На 7.1 (и на
# системном Homebrew ffmpeg 7.1.1, с которым сверялись) этот же плейлист
# скачивается целиком без проблем — проверено вживую до фиксации версии здесь.
SOURCES="
arm64 ffmpeg https://www.osxexperts.net/ffmpeg71arm.zip
arm64 ffprobe https://www.osxexperts.net/ffprobe71arm.zip
arm64 ffplay https://www.osxexperts.net/ffplay71arm.zip
x86_64 ffmpeg https://www.osxexperts.net/ffmpeg71intel.zip
x86_64 ffprobe https://www.osxexperts.net/ffprobe71intel.zip
x86_64 ffplay https://www.osxexperts.net/ffplay71intel.zip
"

echo "$SOURCES" | while read -r arch bin_name url; do
    [ -z "$arch" ] && continue
    dest_dir="$FFMPEG_CACHE/$arch"
    dest_bin="$dest_dir/$bin_name"
    mkdir -p "$dest_dir"
    if [ ! -x "$dest_bin" ]; then
        echo "    качаю $arch/$bin_name..."
        zip_path=$(mktemp).zip
        curl -sL --fail -o "$zip_path" "$url"
        extract_dir=$(mktemp -d)
        unzip -o -q "$zip_path" -d "$extract_dir"
        found=$(find "$extract_dir" -type f -name "$bin_name" -perm -u+x | head -1)
        if [ -z "$found" ]; then
            found=$(find "$extract_dir" -type f -name "$bin_name" | head -1)
        fi
        cp "$found" "$dest_bin"
        chmod +x "$dest_bin"
        rm -rf "$zip_path" "$extract_dir"
    fi
    file_out=$(file -b "$dest_bin")
    echo "    $arch/$bin_name: $file_out"
    case "$arch" in
        arm64) echo "$file_out" | grep -q "arm64" || { echo "ОШИБКА: $dest_bin не arm64" >&2; exit 1; } ;;
        x86_64) echo "$file_out" | grep -q "x86_64" || { echo "ОШИБКА: $dest_bin не x86_64" >&2; exit 1; } ;;
    esac
done

echo "==> Копирую бинарники в бандл"
RES_BIN="$APP_PATH/Contents/Resources/bin"
mkdir -p "$RES_BIN/arm64" "$RES_BIN/x86_64"
cp "$FFMPEG_CACHE/arm64/"* "$RES_BIN/arm64/"
cp "$FFMPEG_CACHE/x86_64/"* "$RES_BIN/x86_64/"
chmod +x "$RES_BIN"/arm64/* "$RES_BIN"/x86_64/*

echo "==> ad-hoc подпись (без Apple Developer сертификата — обязательна для запуска на Apple Silicon,"
echo "    но НЕ заменяет нотариацию Apple: при первом запуске у друга будет предупреждение Gatekeeper,"
echo "    один клик правой кнопкой -> Открыть)"
find "$RES_BIN" -type f -exec codesign --force --sign - {} \;
codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH" && echo "    подпись валидна"

echo "==> Готово: $APP_PATH"
