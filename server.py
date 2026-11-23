import os
import re
import uuid
import glob
import shutil
import asyncio
import urllib.parse
import requests
import imageio_ffmpeg
import yt_dlp
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads_cache")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

app = FastAPI(title="Website Video Downloader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

downloaded_files = {}
tasks_progress = {}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return cleaned or "video"

def make_progress_hook(task_id: str):
    def hook(d):
        if task_id not in tasks_progress:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            if total > 0:
                percent = round((downloaded / total) * 100, 1)
            else:
                raw_pct = re.sub(r"[^\d.]", "", str(d.get("_percent_str", "0") or "0"))
                try:
                    percent = float(raw_pct)
                except ValueError:
                    percent = 0.0

            raw_speed = d.get("_speed_str", "") or ""
            raw_eta = d.get("_eta_str", "") or ""
            speed = re.sub(r"\x1b\[[0-9;]*m", "", str(raw_speed)).strip()
            eta = re.sub(r"\x1b\[[0-9;]*m", "", str(raw_eta)).strip()

            tasks_progress[task_id].update({
                "status": "downloading",
                "percent": percent,
                "percent_str": f"{percent}%",
                "speed": speed,
                "eta": eta
            })
        elif status == "finished":
            tasks_progress[task_id].update({
                "status": "merging",
                "percent": 100.0,
                "percent_str": "100%",
                "speed": "",
                "eta": "Processing/Merging"
            })
    return hook

def download_video_sync(url: str, task_id: str | None = None) -> tuple[str, str, str]:
    if not task_id:
        task_id = str(uuid.uuid4())[:8]

    file_id = task_id
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    headers_with_ref = {**HEADERS, "Referer": url}
    progress_hook = make_progress_hook(task_id)

    tasks_progress[task_id] = {
        "status": "starting",
        "percent": 0.0,
        "percent_str": "0%",
        "speed": "",
        "eta": "",
        "filename": "",
        "file_id": file_id,
        "error": None
    }

    # 1. Direct video URL check
    try:
        head = requests.head(url, headers=headers_with_ref, allow_redirects=True, timeout=7)
        content_type = head.headers.get("content-type", "").lower()
        if "video/" in content_type:
            total_size = int(head.headers.get("content-length", 0))
            parsed_path = urllib.parse.urlparse(head.url).path
            base_name = os.path.basename(parsed_path) or f"video_{file_id}.mp4"
            base_name = sanitize_filename(urllib.parse.unquote(base_name))
            if not os.path.splitext(base_name)[1]:
                base_name += ".mp4"

            out_filename = f"{file_id}_{base_name}"
            out_path = os.path.join(DOWNLOADS_DIR, out_filename)

            downloaded_bytes = 0
            with requests.get(url, headers=headers_with_ref, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 64):
                        if chunk:
                            f.write(chunk)
                            downloaded_bytes += len(chunk)
                            if total_size > 0:
                                pct = round((downloaded_bytes / total_size) * 100, 1)
                                tasks_progress[task_id].update({
                                    "status": "downloading",
                                    "percent": pct,
                                    "percent_str": f"{pct}%"
                                })

            direct_dest = os.path.join(BASE_DIR, base_name)
            try:
                shutil.copy2(out_path, direct_dest)
            except Exception:
                pass

            downloaded_files[file_id] = {"path": out_path, "filename": base_name}
            tasks_progress[task_id].update({
                "status": "finished",
                "percent": 100.0,
                "percent_str": "100%",
                "filename": base_name,
                "file_id": file_id
            })
            return file_id, out_path, base_name
    except Exception:
        pass

    # 2. Main yt-dlp extraction
    out_template = os.path.join(DOWNLOADS_DIR, f"{file_id}_%(title).100s.%(ext)s")
    ydl_opts = {
        "outtmpl": out_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "ffmpeg_location": ffmpeg_exe,
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        "playlist_items": "1",
        "merge_output_format": "mp4",
        "http_headers": headers_with_ref,
        "progress_hooks": [progress_hook],
    }

    last_error = ""
    try:
        print(f"\n[INFO] Starting video extraction for: {url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        pattern = os.path.join(DOWNLOADS_DIR, f"{file_id}_*")
        matches = glob.glob(pattern)
        if matches:
            saved_file = matches[0]
            display_name = os.path.basename(saved_file).replace(f"{file_id}_", "", 1)
            direct_dest = os.path.join(BASE_DIR, display_name)
            try:
                shutil.copy2(saved_file, direct_dest)
                print(f"[SUCCESS] Video saved to Downloads: {direct_dest}")
            except Exception as e:
                print(f"[INFO] Could not copy to Downloads root: {e}")

            downloaded_files[file_id] = {"path": saved_file, "filename": display_name}
            tasks_progress[task_id].update({
                "status": "finished",
                "percent": 100.0,
                "percent_str": "100%",
                "filename": display_name,
                "file_id": file_id
            })
            return file_id, saved_file, display_name
    except Exception as ydl_err:
        last_error = str(ydl_err)
        print(f"[WARN] yt-dlp primary extraction failed: {last_error}")

    # 3. Enhanced Fallback: Parse HTML & Embedded Scripts
    try:
        print("[INFO] Attempting enhanced fallback scraping...")
        resp = requests.get(url, headers=headers_with_ref, timeout=12)
        resp.raise_for_status()
        html = resp.text

        candidates = []
        # Media tags
        for m in re.finditer(r'<(?:video|source)\b[^>]*\bsrc=["\']([^"\']+)["\']', html, re.I):
            candidates.append(m.group(1))
        # Iframes
        for m in re.finditer(r'<iframe\b[^>]*\bsrc=["\']([^"\']+)["\']', html, re.I):
            candidates.append(m.group(1))
        # OpenGraph video
        for m in re.finditer(r'<meta\b[^>]*\bproperty=["\']og:video(?::url)?["\'][^>]*\bcontent=["\']([^"\']+)["\']', html, re.I):
            candidates.append(m.group(1))
        for m in re.finditer(r'<meta\b[^>]*\bcontent=["\']([^"\']+)["\'][^>]*\bproperty=["\']og:video(?::url)?["\']', html, re.I):
            candidates.append(m.group(1))
        # Script media stream links (.m3u8, .mp4, .webm)
        for m in re.finditer(r'https?://[^\s"\'<>]+\.(?:m3u8|mp4|webm)(?:\?[^\s"\'<>]*)?', html, re.I):
            candidates.append(m.group(0))

        for cand in candidates:
            resolved = urllib.parse.urljoin(url, cand.strip().replace(r"\/", "/"))
            if resolved != url:
                try:
                    print(f"[INFO] Trying candidate stream: {resolved}")
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([resolved])
                    pattern = os.path.join(DOWNLOADS_DIR, f"{file_id}_*")
                    matches = glob.glob(pattern)
                    if matches:
                        saved_file = matches[0]
                        display_name = os.path.basename(saved_file).replace(f"{file_id}_", "", 1)
                        direct_dest = os.path.join(BASE_DIR, display_name)
                        try:
                            shutil.copy2(saved_file, direct_dest)
                        except Exception:
                            pass

                        downloaded_files[file_id] = {"path": saved_file, "filename": display_name}
                        tasks_progress[task_id].update({
                            "status": "finished",
                            "percent": 100.0,
                            "percent_str": "100%",
                            "filename": display_name,
                            "file_id": file_id
                        })
                        return file_id, saved_file, display_name
                except Exception:
                    continue
    except Exception as scrape_err:
        print(f"[WARN] Fallback scraping failed: {scrape_err}")

    clean_err = last_error.split("\n")[0] if last_error else "No downloadable video stream detected."
    tasks_progress[task_id].update({
        "status": "error",
        "error": clean_err
    })
    raise RuntimeError(clean_err)

class DownloadRequest(BaseModel):
    url: str

@app.get("/")
async def serve_root():
    return FileResponse(os.path.join(BASE_DIR, "index.html"), media_type="text/html")

@app.get("/index.html")
async def serve_index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"), media_type="text/html")

@app.get("/icon.svg")
async def serve_icon():
    path = os.path.join(BASE_DIR, "icon.svg")
    if os.path.exists(path):
        return FileResponse(path, media_type="image/svg+xml")
    raise HTTPException(status_code=404)

@app.get("/manifest.json")
async def serve_manifest():
    path = os.path.join(BASE_DIR, "manifest.json")
    if os.path.exists(path):
        return FileResponse(path, media_type="application/manifest+json")
    raise HTTPException(status_code=404)

@app.post("/api/download/start")
async def api_download_start(payload: DownloadRequest):
    url = payload.url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return JSONResponse({"success": False, "error": "Please provide a valid URL starting with http:// or https://"}, status_code=400)

    task_id = str(uuid.uuid4())[:8]
    tasks_progress[task_id] = {
        "status": "starting",
        "percent": 0.0,
        "percent_str": "0%",
        "speed": "",
        "eta": "",
        "filename": "",
        "file_id": task_id,
        "error": None
    }

    # Run in background thread so progress endpoint can immediately respond
    asyncio.create_task(asyncio.to_thread(download_video_sync, url, task_id))
    return {"success": True, "task_id": task_id}

@app.get("/api/download/progress/{task_id}")
async def api_download_progress(task_id: str):
    prog = tasks_progress.get(task_id)
    if not prog:
        return JSONResponse({"status": "error", "error": "Task not found"}, status_code=404)
    return prog

@app.post("/api/download")
async def api_download(payload: DownloadRequest):
    url = payload.url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return JSONResponse({"success": False, "error": "Please provide a valid URL starting with http:// or https://"}, status_code=400)

    try:
        file_id, filepath, filename = await asyncio.to_thread(download_video_sync, url)
        return {"success": True, "file_id": file_id, "filename": filename}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/file/{file_id}")
async def get_downloaded_file(file_id: str):
    info = downloaded_files.get(file_id)
    if not info:
        pattern = os.path.join(DOWNLOADS_DIR, f"{file_id}_*")
        matches = glob.glob(pattern)
        if matches:
            filepath = matches[0]
            filename = os.path.basename(filepath).replace(f"{file_id}_", "", 1)
            info = {"path": filepath, "filename": filename}
        else:
            raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        info["path"],
        filename=info["filename"],
        media_type="application/octet-stream"
    )

@app.api_route("/download", methods=["GET", "POST"])
async def direct_download(request: Request):
    if request.method == "POST":
        form_data = await request.form()
        url = form_data.get("url", "").strip()
    else:
        url = request.query_params.get("url", "").strip()

    if not url:
        return HTMLResponse("<p>Error: No URL provided. <a href='/'>Go back</a></p>", status_code=400)

    try:
        file_id, filepath, filename = await asyncio.to_thread(download_video_sync, url)
        return FileResponse(
            filepath,
            filename=filename,
            media_type="application/octet-stream"
        )
    except Exception as e:
        return HTMLResponse(f"<p>Error downloading video: {e}</p><p><a href='/'>Go back</a></p>", status_code=500)

if __name__ == "__main__":
    print("=" * 60)
    print("Website Video Downloader Server running on http://localhost:8000")
    print("Open http://localhost:8000 in your browser.")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8000)
