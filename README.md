# SachDownload - Website Video Downloader

A minimal, lightweight web video downloader with real-time download progress tracking.

## Features
- Minimalist, pure HTML interface (no CSS bloat).
- Live download percentage (%), speed, and ETA updates.
- Extracts and downloads videos from any website (direct video links, HTML5 `<video>` tags, HLS/m3u8 streaming, and embedded web players).
- Direct local download to your Downloads folder.

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

## Note on Deployment (Vercel vs. Persistent Servers)
Vercel is a serverless platform with a 15-second execution limit and a read-only filesystem. Since video extraction, downloading, and ffmpeg remuxing often take several minutes, deploying on persistent server environments such as **Render**, **Railway**, **Fly.io**, or running **locally** is recommended for full functionality.
