# TV Recorder

A native-feeling macOS desktop app for watching and recording Russian federal
TV channels — and, more generally, for pulling video off almost any link.
Built with Python, Tkinter and CustomTkinter.

## What it does

**Каналы (Channels)** — auto-updated IPTV playlist of Russian federal
channels (with a bundled local fallback), live preview via `ffplay`, and
simultaneous multi-channel recording via `ffmpeg`.

**Мои ссылки (My Links)** — paste a link to almost anything (YouTube, VK,
RuTube, Twitch, or a plain news-site article with an embedded player) and
record it. Resolution runs through a fallback chain: `yt-dlp` first, then a
direct HTML/JS scrape for sites `yt-dlp` doesn't know, then — as a last
resort — a real embedded browser that watches its own network traffic for
the stream. When video and audio arrive as separate tracks (common on VK),
they're muxed into one file with `ffmpeg -c copy` — no re-encoding.

**Браузер (Browser)** — for sites where no direct stream can be found at
all: records the page itself via screen-capture of an embedded browser
window.

**Загрузки (Downloads)** — a one-shot universal downloader. Paste any link,
pick a target quality (360p–1080p, when the source actually offers more than
one), pick a save folder, and get a single finished MP4 with a live
progress bar, download speed and ETA — reusing the same resolver chain as
"Мои ссылки".

**Расписание (Schedule)** — schedule recordings by time range and weekdays
for any of Каналы / Мои ссылки / Браузер, with automatic stop at the
selected end time and a live panel of active recordings (pause, stop,
remove, reveal in Finder, grid monitor view).

Also: VPN and real internet-connectivity monitoring in the status bar,
native macOS notifications, and a compact icon-led dark UI.

## Requirements

- macOS 12 or newer
- Python 3.12 or newer
- FFmpeg (including `ffplay` and `ffprobe`)

## Installation

```bash
git clone https://github.com/sirdimitry/TV_recorder.git
cd TV_recorder
brew install ffmpeg
python3 -m pip install -r requirements.txt
python3 main.py
```

If Terminal is already open in the project directory, just run:

```bash
python3 main.py
```

> YouTube's site changes frequently and can break older `yt-dlp` releases
> ("Sign in to confirm you're not a bot", "Requested format is not
> available"). If links stop resolving, try
> `python3 -m pip install --upgrade yt-dlp` first.

## Usage

1. Wait for the splash screen to sync the channel list.
2. **Каналы**: click record next to a channel for a manual recording.
3. **Мои ссылки** / **Браузер**: add a link, then record it manually or
   schedule it.
4. **Загрузки**: add a link, pick quality and folder, and it downloads in
   the background — no scheduling involved, it's a one-shot job.
5. **Расписание**: pick a source, a start/end time, and weekdays; it repeats
   on the selected days and stops itself at the end time (start and end
   must differ — equal values would mean a 24-hour recording).
6. Finished files land in `recordings/` (scheduled/manual channel and link
   recordings) or `downloads/` (the Загрузки tab); the folder button reveals
   the selected file in Finder.

## Project layout

```text
core/       Recording, link resolution, scheduling, playlist parsing, storage
gui/        CustomTkinter/Tkinter interface
utils/      Configuration, icons, logging, network, VPN, logo cache helpers
data/       Bundled fallback channel list and runtime data (gitignored)
recordings/ Saved channel/link recordings (created locally, gitignored)
downloads/  Saved one-shot downloads (created locally, gitignored)
logs/       Application logs (created locally, gitignored)
```

## Versioning

Every commit auto-bumps `VERSION` and adds a dated entry to
`CHANGELOG.md` via a Git hook in `.githooks/` (the repo is configured to
use it automatically) — write an English one-line summary in the commit
message. By default this bumps the patch number; for a deliberately larger
change, bump minor or major explicitly:

```bash
BUMP=minor git commit -m "..."   # or BUMP=major
```

## License

MIT — see [LICENSE](LICENSE).
