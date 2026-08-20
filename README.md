# TV Recorder for macOS

A desktop application for watching and recording Russian federal TV channels. It is built with Python and Tkinter and runs on macOS.

## Current features

- Downloads and updates a channel list from an open IPTV playlist, with a local fallback list.
- Opens a stream preview through `ffplay`.
- Records several channels at the same time through `ffmpeg`.
- Supports scheduled recordings by channel, time range, and weekdays.
- Stops scheduled recordings automatically at the selected end time.
- Shows active recordings with elapsed time, pause, stop, remove, and Reveal in Finder controls.
- Uses the system date and time as the default schedule values.
- Supports scrolling channels with a mouse wheel or a two-finger trackpad gesture.
- Monitors VPN and actual internet availability in the status bar.
- Uses macOS-native notifications without extra macOS bindings.

## Requirements

- macOS 12 or newer
- Python 3.12 or newer
- FFmpeg, including `ffplay`

## Installation

```bash
git clone https://github.com/sirdimitry/TV_recorder.git
cd TV_recorder
brew install ffmpeg
python3 -m pip install -r requirements.txt
python3 main.py
```

If Terminal is already open in the project directory, only run:

```bash
python3 main.py
```

## Usage

1. Wait for the splash screen to synchronise the channel list.
2. Click the record button next to a channel to start a manual recording.
3. In **Schedule**, choose a channel, enter a start and end time, and select weekdays.
4. Completed recording files appear in `recordings/`. The folder button reveals the selected file in Finder.

The schedule repeats on the selected weekdays. Start and end times must be different; equal values would mean a 24-hour recording.

## Project layout

```text
core/       Recording, scheduling, playlist parsing, and data storage
gui/        Tkinter user interface
utils/      Configuration, logging, network, VPN, and logo helpers
data/       Bundled fallback channel list
recordings/ Saved videos (created locally and excluded from Git)
logs/       Application logs (created locally and excluded from Git)
```

## Versioning

The project uses patch-version auto-incrementing. Every local commit updates `VERSION` and creates a dated entry in `CHANGELOG.md`; use an English commit message for the short change description. The shared hook is stored in `.githooks/`; the repository is configured to use it automatically.

## License

MIT License.
