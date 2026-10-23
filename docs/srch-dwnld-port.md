# srch-dwnld integration, 16 October 2026

Source: `sirdimitry/srch-dwnld`, commit `7660027a7b8124cec61f911656e8ca027a39d942`.
The local srch-dwnld checkout was older (`0ada7c1`) and was left unchanged.

| Source | TV Recorder | Adaptation |
| --- | --- | --- |
| `searchnf/core/tv_archive.py` | `core/tv_archive.py` | Same catalog, Moscow clock, session cookies, validated playlists/TS boundaries, bounded segment batches, concat and exact trim. Desktop destinations and cancellation replace Telegram/cloud delivery. |
| `searchnf/core/link_prepare.py` | `core/archive_media.py` | Exact H.264/AAC trim with VideoToolbox and software fallback; progress and duration validation. |
| `searchnf/core/resolvers/` | `core/source_resolvers/` | TV1, Smotrim, REN, Nextcloud, TV Zvezda, Webcaster, Vipler, TikTok and direct-media parsers. Desktop HTTP/logging adapters replace bot configuration. |
| `searchnf/yt_dlp_compat.py` | `core/yt_dlp_compat.py` | Public VK embed prefetch and object/string OK metadata fixes, registered with yt-dlp. |
| Clip and stalled-download behavior | `core/downloader.py`, `gui/download_dialog.py` | Optional exact time range for a single download; no-progress watchdog. |

Existing site handlers, the legacy Yandex path, and browser fallbacks remain in place. New handlers run after the existing direct/yt-dlp/HTML paths fail. Telegram bot administration, search, and cloud delivery belong to the other application and were not transferred.

Archive channel checks run off the UI thread and validate real video boundaries. A successful calendar HTTP response alone never marks a channel playable. Source 404 responses can affect individual channels/dates; the UI reports them without claiming a successful download. Cached channel status is dated and refreshed at application startup.

The destination receives only a checked, finalized MP4. Work segments remain in a temporary directory on the same volume and are removed on cancellation or failure.

## Zabava / Wink catch-up

The complete bundled catalog (`data/zabava-full.m3u`) comes from
[CrocoUser/zabava-project](https://github.com/CrocoUser/zabava-project),
commit `189abd2cacc1f4e0dc0fd63c1d43b20db4c4bbae`, fetched 16 October 2026:
2540 Ngenix streams across 79 catalog groups, without the non-Ngenix info entry.
Existing local Ngenix streams are merged by URL. Regional and time-shift variants
are retained. Startup refreshes the cached catalog and checks channels incrementally
with six workers. Bounded TS prefixes verify catalog availability without downloading
thousands of full video segments; export/preview preflight checks full boundaries.
Cached results show their check time and are not a guarantee for every date.

Ngenix `utcstart` alone can return live television. `core/stream_archive.py` always
sets both `utcstart` and `utcend`, including child variant URLs, requires a completed
VOD playlist, cross-checks HLS program dates against timeline filenames, rejects gaps
and verifies coverage. This path reuses the exact trim and atomic export above.
Archive saving reads `Config.get_recordings_dir()` at click time; the old archive-only
destination setting is ignored.
