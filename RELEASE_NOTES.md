# QuickRec v1.0.0

The first release of QuickRec, a tiny, fast screen recorder for Windows 11, made by **Anant Rastogi**.

## Download
**QuickRec-v1.0.0-win64.zip**: unzip it, keep `QuickRec.exe` and `ffmpeg.exe` together, and run `QuickRec.exe`. No installation is needed.

## Highlights
- **One floating bar:** record, pause, screenshot, settings. A right-click menu holds the quick options.
- **Colour-exact capture** with Windows Graphics Capture and correct BT.709 colour handling. This also works with calibration tools and "advanced colour" display modes.
- **Up to 240 fps, constant frame rate.** It's tuned for steady 120 fps on Intel Arc.
- **GPU pipeline** with Intel Quick Sync, NVIDIA NVENC or AMD AMF, and automatic CPU fallback.
- **Codecs:** H.264, HEVC and AV1, plus an optional full-colour 4:4:4 mode.
- **Resolution:** native or any custom size.
- **Capture area:** full screen, a region, or a window.
- **Audio:** system audio and microphone, with noise reduction and optional separate tracks.
- **Workflow:** pause and resume, crash-safe recording, auto-recovery, auto-stop, global shortcuts and a countdown.
- **Invisible UI:** the bar never shows up in your own recordings.
- **Modern settings window** where changes save automatically.

## Requirements
- Windows 10 (version 2004) or Windows 11, 64-bit
- A DirectX 11 GPU. Intel, NVIDIA or AMD hardware encoding is recommended.

## Known notes
- The app isn't code-signed, so SmartScreen may show a warning the first time (*More info → Run anyway*).
- On battery in *Best power efficiency* mode, Windows slows the GPU down. Plug in for the steadiest 120 fps.
- NVIDIA and AMD encoding paths are included but were only tested on Intel hardware.
