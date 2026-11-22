# Third-party notices

SmoothRec's own code is © 2026 Anant Rastogi and MIT-licensed (see `LICENSE`). The release download bundles or embeds the following:

| Component | Use | License |
|---|---|---|
| **FFmpeg** (`ffmpeg.exe`) | Capture, scaling, encoding | GPL v3 (this build includes GPL components) |
| Python | Runtime embedded in `SmoothRec.exe` | PSF License |
| Tcl/Tk | User interface | Tcl/Tk License (BSD-style) |
| NumPy | Audio buffers | BSD 3-Clause |
| SoundCard | WASAPI audio capture | BSD 3-Clause |
| Pillow | Drawing the interface | MIT-CMU (HPND) |
| PyInstaller bootloader | Single-file executable | GPL v2 with bootloader exception (permits any license for the bundled app) |

## FFmpeg

`ffmpeg.exe` is a separate program that SmoothRec runs as an external process; it isn't linked into SmoothRec.
The bundled binary is an unmodified FFmpeg build (`N-127043-g5a54fcf75e`, from the
[yt-dlp FFmpeg-Builds](https://github.com/yt-dlp/FFmpeg-Builds) project, distributed through `winget`).

- FFmpeg license: <https://ffmpeg.org/legal.html>
- FFmpeg source code: <https://ffmpeg.org/download.html> / <https://git.ffmpeg.org/ffmpeg.git>
- Build scripts for this binary: <https://github.com/yt-dlp/FFmpeg-Builds>

FFmpeg is a trademark of Fabrice Bellard, originator of the FFmpeg project.
