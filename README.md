# SachDownload - Video Downloader

A lightweight FastAPI + `yt-dlp` video downloader with format inspection, streaming proxy, HLS segment joining, and local progress tracking.

## Features
- Inspects video qualities (`/api/inspect`) across direct media URLs, `yt-dlp` extractors, platform fallbacks (YouTube InnerTube, Vimeo, Dailymotion, X/Twitter, Reddit), and recursive `<iframe>` embeds.
- Streaming proxy (`/api/proxy`) with SSRF protection, `Range` forwarding, and live HLS `.m3u8` segment joining (`.ts` and fMP4 `#EXT-X-MAP` / `#EXT-X-BYTERANGE`) so downloads work cleanly from Vercel without exposing IP-locked or hotlink-blocked upstream URLs.
- Local unrestricted mode (`python server.py` or `start.bat`) using verified system `ffmpeg` + `yt-dlp` for full 1080p/4K video+audio merging and live download progress.

## Running Locally

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Start the server:
```bash
python server.py
# or double-click start.bat on Windows
```

3. Open in your browser:
```
http://localhost:8000
```
