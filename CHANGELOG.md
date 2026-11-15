## Unreleased — stage 10 candidate

- Keep monitor, forms, settings, about and confirmations in the main window.
- Use embedded link preview; cancel delayed form results on navigation.
- Extract RecordingTask from recording orchestration without changing imports.
- Separate runtime/build dependencies and record tested direct versions.
- Add macOS reliability CI, isolated QA profiles and local build cache.
- Fix PyInstaller webview collection and generate ICNS without iconutil.
- Add simple test-DMG mode and SHA-256 output for image builds.

# Changelog

All notable changes to this project are documented in this file.

## [1.1.49] - 2026-10-23

- See the accompanying Git commit message for details.

## [1.1.48] - 2026-10-23

- Rebuild Archive as a compact channel browser, dated player and persistent fragment export panel; keep save, cancel, sound and transport controls visible in small windows.
- Group time shifts, regional feeds and duplicate sources under one channel row. Expose national/time-shift versions in the channel menu and regional versions through the region filter; preserve all sources and the clip draft when switching versions.
- Scale archive video to the available player area at 16:9 without a fixed maximum size, including the currently displayed frame; retain one decoder while resizing.
- Show fragment duration and validation inline, separate watching from saving, and debounce catalog filtering while retaining selection and scroll position.

## [1.1.47] - 2026-10-23

- Recheck recording windows every five seconds, including after sleep and on app startup; recover the remaining portion, retry failed starts, and enforce the original stop time across sleep.
- Share occurrence guards between cron and recovery to prevent duplicate recordings, including after schedule reloads; log APScheduler warnings and errors to the application log.
- Reset archive clip boundaries to the actual channel selection time in Moscow time. Open preview near the current broadcast with a thirty-second archive delay instead of stale startup values.

## [1.1.46] - 2026-10-16

- Load the complete public Zabava/Wink catalog, including regional channels and time shifts; add background archive/live checks, search, region/status filters, cached catalog updates and custom Ngenix stream connections.
- Start muted archive preview on channel selection. Clicking the preview toggles sound; remove separate watch, stop, sound and destination controls. Archive exports use the global recordings folder.
- Add dated archive seeking, a daily timeline, skip controls and clip boundary marks from the displayed frame. Advance the broadcast clock only when decoded frames arrive.
- Request Ngenix archives with both absolute time bounds and verify program dates, segment timestamps, coverage and video boundaries to prevent a live fallback being shown as an archive.

## [1.1.45] - 2026-10-16

- Add a dedicated OnTVTime Archive workspace with automatic per-channel playback checks, available dates, Moscow time ranges, preview, explicit sound control, destination folder, progress, and cancellation.
- Port OnTVTime session, playlist, segment validation, and exact trim logic from srch-dwnld 7660027; unavailable recordings and network failures remain distinct.
- Port VK/OK extractor compatibility fixes and additional TikTok, Webcaster, embedded Smotrim, Vipler, and direct-media fallbacks. Prefer full-quality HLS in First Channel programme playlists.
- Support downloading a precise video time range and stop stalled downloads without blocking the interface.

## [1.1.44] - 2026-10-16

- Support recordings restricted to a specific calendar date and display that date in the schedule, preserving it when the entry is edited.

## [1.1.43] - 2026-10-16

- Restore the existing main window on macOS Dock/Finder reopen and add an explicit Show TV Recorder menu command.
- Deliver preview frames through a single latest-frame slot polled by the UI, avoiding Tk calls from the decoder thread and queued video frames.
- Detect stalled preview decoders, reconnect them automatically, reap stopped processes, and retain decoder errors in the log. Recording processes are independent.

## [1.1.42] - 2026-10-07

- See the accompanying Git commit message for details.

## [1.1.41] - 2026-10-07

- See the accompanying Git commit message for details.

## [1.1.40] - 2026-10-07

- See the accompanying Git commit message for details.

## [1.1.39] - 2026-10-07

- Replace the Russia 1 and Russia 24 sources with verified Ngenix/Zabava streams containing video and audio together. Russia 24 now includes the usual on-air logo and ticker.
- Validate both streams with five-minute recordings and archive playback at 1, 24, and 72 hours; bundled FFmpeg also records both channels with audio.
- Add polite invitations for feedback, feature suggestions, and GitHub stars to the English and Russian READMEs.
- Provide a ready-to-install Apple Silicon DMG in [the GitHub release](https://github.com/sirdimitry/TV_recorder/releases/tag/v1.1.39), with a SHA-256 checksum. The app is ad-hoc signed and is not Apple-notarized.

## [1.1.38] - 2026-09-28

- See the accompanying Git commit message for details.

## [1.1.37] - 2026-09-28

- See the accompanying Git commit message for details.

## [1.1.35] - 2026-09-24

- See the accompanying Git commit message for details.

## [1.1.34] - 2026-09-24

- See the accompanying Git commit message for details.

## [1.1.33] - 2026-09-24

- See the accompanying Git commit message for details.

## [1.1.32] - 2026-09-24

- See the accompanying Git commit message for details.

## [1.1.31] - 2026-09-24

- See the accompanying Git commit message for details.

## [1.1.30] - 2026-09-24

- Show the main window and start the scheduler immediately while channel synchronization runs in the background.
- Keep all Tk interface updates on the main thread during startup, avoiding an intermittent Dock-only launch.

## [1.1.29] - 2026-09-24

- Keep user-edited channel streams through playlist sync; use the playlist temporarily when the preferred stream is unavailable.
- Ignore delayed link and logo updates for list rows that have already been replaced.

## [1.1.28] - 2026-09-24

- “Сейчас” records the channel or link selected in the form, without requiring a schedule row selection.
- Application shutdown now waits for recording, download, and file finalization in the background so the window remains responsive.

## [1.1.27] - 2026-09-23

- The schedule “Clear” action now removes schedule and completed-recording rows while preserving form values.
- Selecting a channel now chooses today and a valid 30-minute time window.
- Added “Clear all” actions for saved links and download history.
- Corrected completion status for recordings with a duration limit.

## [1.1.26] - 2026-09-22

- See the accompanying Git commit message for details.

## [1.1.25] - 2026-09-22

- See the accompanying Git commit message for details.

## [1.1.24] - 2026-09-22

- See the accompanying Git commit message for details.

## [1.1.23] - 2026-09-22

- See the accompanying Git commit message for details.

## [1.1.22] - 2026-09-01

- See the accompanying Git commit message for details.

## [1.1.21] - 2026-09-01

- See the accompanying Git commit message for details.

## [1.1.20] - 2026-09-01

- See the accompanying Git commit message for details.

## [1.1.19] - 2026-09-01

- See the accompanying Git commit message for details.

## [1.1.18] - 2026-09-01

- See the accompanying Git commit message for details.

## [1.1.17] - 2026-09-01

- See the accompanying Git commit message for details.

## [1.1.16] - 2026-09-01

- See the accompanying Git commit message for details.

## [1.1.15] - 2026-09-01

- See the accompanying Git commit message for details.

## [1.1.14] - 2026-09-01

- See the accompanying Git commit message for details.

## [1.1.13] - 2026-09-01

- See the accompanying Git commit message for details.

## [1.1.12] - 2026-08-31

- See the accompanying Git commit message for details.

## [1.1.11] - 2026-08-31

- See the accompanying Git commit message for details.

## [1.1.10] - 2026-08-31

- See the accompanying Git commit message for details.

## [1.1.9] - 2026-08-31

- See the accompanying Git commit message for details.

## [1.1.8] - 2026-08-31

- See the accompanying Git commit message for details.

## [1.1.7] - 2026-08-27

- See the accompanying Git commit message for details.

## [1.1.6] - 2026-08-27

- See the accompanying Git commit message for details.

## [1.1.5] - 2026-08-27

- See the accompanying Git commit message for details.

## [1.1.4] - 2026-08-26

- See the accompanying Git commit message for details.

## [1.1.3] - 2026-08-26

- See the accompanying Git commit message for details.

## [1.1.2] - 2026-08-26

- See the accompanying Git commit message for details.

## [1.1.1] - 2026-08-26

- See the accompanying Git commit message for details.

## [1.1.0] - 2026-08-26

- See the accompanying Git commit message for details.

## [1.0.57] - 2026-08-25

- See the accompanying Git commit message for details.

## [1.0.56] - 2026-08-25

- See the accompanying Git commit message for details.

## [1.0.55] - 2026-08-25

- See the accompanying Git commit message for details.

## [1.0.54] - 2026-08-25

- See the accompanying Git commit message for details.

## [1.0.53] - 2026-08-25

- See the accompanying Git commit message for details.

## [1.0.52] - 2026-08-25

- See the accompanying Git commit message for details.

## [1.0.51] - 2026-08-25

- See the accompanying Git commit message for details.

## [1.0.50] - 2026-08-25

- See the accompanying Git commit message for details.

## [1.0.49] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.48] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.47] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.46] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.45] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.44] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.43] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.42] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.41] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.40] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.39] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.38] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.37] - 2026-08-24

- See the accompanying Git commit message for details.

## [1.0.36] - 2026-08-23

- See the accompanying Git commit message for details.

## [1.0.35] - 2026-08-23

- See the accompanying Git commit message for details.

## [1.0.34] - 2026-08-23

- See the accompanying Git commit message for details.

## [1.0.33] - 2026-08-23

- See the accompanying Git commit message for details.

## [1.0.32] - 2026-08-23

- See the accompanying Git commit message for details.

## [1.0.31] - 2026-08-23

- See the accompanying Git commit message for details.

## [1.0.30] - 2026-08-23

- See the accompanying Git commit message for details.

## [1.0.29] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.28] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.27] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.26] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.25] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.24] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.23] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.22] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.21] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.20] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.19] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.18] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.17] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.16] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.15] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.14] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.13] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.12] - 2026-08-22

- See the accompanying Git commit message for details.

## [1.0.11] - 2026-08-21

- See the accompanying Git commit message for details.

## [1.0.10] - 2026-08-21

- See the accompanying Git commit message for details.

## [1.0.9] - 2026-08-21

- See the accompanying Git commit message for details.

## [1.0.8] - 2026-08-21

- See the accompanying Git commit message for details.

## [1.0.7] - 2026-08-21

- See the accompanying Git commit message for details.

## [1.0.6] - 2026-08-21

- See the accompanying Git commit message for details.

## [1.0.5] - 2026-08-21

- See the accompanying Git commit message for details.

## [1.0.4] - 2026-08-21

- See the accompanying Git commit message for details.

## [1.0.3] - 2026-08-21

- See the accompanying Git commit message for details.

## [1.0.2] - 2026-08-21

- See the accompanying Git commit message for details.

## [1.0.1] - 2026-08-20

- feat: restore scheduling and improve macOS experience

## [1.0.0] - Initial beta

- Added IPTV channel discovery, preview, recording, schedules, and a dark macOS interface.
