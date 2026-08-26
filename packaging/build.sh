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
SOURCES="
arm64 ffmpeg https://www.osxexperts.net/ffmpeg9arm.zip
arm64 ffprobe https://www.osxexperts.net/ffprobe9arm.zip
arm64 ffplay https://www.osxexperts.net/ffplay9arm.zip
x86_64 ffmpeg https://www.osxexperts.net/ffmpeg80intel.zip
x86_64 ffprobe https://www.osxexperts.net/ffprobe80intel.zip
x86_64 ffplay https://www.osxexperts.net/ffplay80intel.zip
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
