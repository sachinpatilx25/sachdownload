import os
import re
import uuid
import glob
import shutil
import asyncio
import urllib.parse
import tempfile
import requests
import yt_dlp
from yt_dlp.utils import ExtractorError

# Patch known yt-dlp site extractor bugs (e.g. KeyError: 'videoModel' or 'No video formats found!' on XHamster)
try:
    from yt_dlp.extractor.xhamster import XHamsterIE
    _orig_xh_real_extract = XHamsterIE._real_extract

    def _safe_xh_real_extract(self, url):
        orig_parse_json = self._parse_json

        def patched_parse_json(*args, **kwargs):
            res = orig_parse_json(*args, **kwargs)
            if isinstance(res, dict):
                if "videoModel" not in res or not isinstance(res.get("videoModel"), dict):
                    res["videoModel"] = res.get("videoEntity") or res.get("xplayerSettings") or {}
                if "title" not in res["videoModel"]:
                    res["videoModel"]["title"] = res.get("videoTitle") or res.get("title") or "video"
            return res

        self._parse_json = patched_parse_json

        info = None
        try:
            info = _orig_xh_real_extract(self, url)
        except Exception:
            pass

        if info and info.get("formats"):
            return info

        # If the site extractor returned empty formats or failed, scrape HTML directly
        desktop_url = re.sub(r'^(https?://(?:.+?\.)?)m\.', r'\1', url)
        mobj = self._match_valid_url(url)
        video_id = (mobj.group('id') or mobj.group('id_2')) if mobj else "video"

        webpage = self._download_webpage(desktop_url, video_id, fatal=False)
        if not webpage:
            if info:
                return info
            raise ExtractorError('No video formats found!')

        # Log webpage preview for debugging
        self.to_screen(f"[DEBUG_HTML_LEN] {len(webpage)} preview: {webpage[:200]}")

        title = self._html_search_regex(
            [r'<h1[^>]*>([^<]+)</h1>', r'<title[^>]*>([^<]+)</title>'],
            webpage, 'title', default='video'
        )

        unescaped = webpage.replace(r'\/', '/')
        formats = []
        seen = set()

        matches = re.findall(r'https?://[^\s"\'<>]+\.(?:mp4|m3u8)(?:\?[^\s"\'<>]*)?', unescaped)
        self.to_screen(f"[DEBUG_MATCHES] count: {len(matches)} items: {matches[:10]}")
        for m in matches:
            if re.search(r'(\.t\.mp4|thumb-|/preview/)', m):
                continue
            if m in seen:
                continue
            seen.add(m)
            if ".m3u8" in m:
                m3u8_formats = self._extract_m3u8_formats(m, video_id, 'mp4', entry_protocol='m3u8_native', fatal=False)
                if m3u8_formats:
                    formats.extend(m3u8_formats)
            else:
                formats.append({
                    'url': m,
                    'format_id': 'http-mp4',
                    'ext': 'mp4',
                    'http_headers': {'Referer': url}
                })

        if not formats and info:
            return info

        return {
            'id': video_id,
            'title': title,
            'formats': formats
        }

    XHamsterIE._real_extract = _safe_xh_real_extract
except Exception:
    pass

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_VERCEL = bool(os.environ.get("VERCEL")) or "LAMBDA_TASK_ROOT" in os.environ or "AWS_LAMBDA_FUNCTION_NAME" in os.environ

if IS_VERCEL:
    DOWNLOADS_DIR = os.path.join(tempfile.gettempdir(), "downloads_cache")
else:
    DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads_cache")

try:
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
except Exception:
    pass

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

def get_ffmpeg():
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")

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

def download_video_sync(url: str, task_id: str | None = None) -> tuple[str, str, str, str | None]:
    if not task_id:
        task_id = str(uuid.uuid4())[:8]

    file_id = task_id
    ffmpeg_exe = get_ffmpeg()
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
        "direct_url": None,
        "error": None
    }

    # 1. Direct video URL check
    try:
        head = requests.head(url, headers=headers_with_ref, allow_redirects=True, timeout=7)
        content_type = head.headers.get("content-type", "").lower()
        if "video/" in content_type:
            parsed_path = urllib.parse.urlparse(head.url).path
            base_name = os.path.basename(parsed_path) or f"video_{file_id}.mp4"
            base_name = sanitize_filename(urllib.parse.unquote(base_name))
            if not os.path.splitext(base_name)[1]:
                base_name += ".mp4"

            if IS_VERCEL:
                tasks_progress[task_id].update({
                    "status": "finished",
                    "percent": 100.0,
                    "percent_str": "100%",
                    "filename": base_name,
                    "file_id": file_id,
                    "direct_url": url
                })
                return file_id, "", base_name, url

            total_size = int(head.headers.get("content-length", 0))
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

            try:
                shutil.copy2(out_path, os.path.join(BASE_DIR, base_name))
            except Exception:
                pass

            downloaded_files[file_id] = {"path": out_path, "filename": base_name}
            tasks_progress[task_id].update({
                "status": "finished",
                "percent": 100.0,
                "percent_str": "100%",
                "filename": base_name,
                "file_id": file_id,
                "direct_url": None
            })
            return file_id, out_path, base_name, None
    except Exception:
        pass

    # 2. Main yt-dlp extraction
    out_template = os.path.join(DOWNLOADS_DIR, f"{file_id}_%(title).100s.%(ext)s")
    ydl_opts = {
        "outtmpl": out_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        "playlist_items": "1",
        "merge_output_format": "mp4",
        "http_headers": headers_with_ref,
        "progress_hooks": [progress_hook],
    }
    if ffmpeg_exe:
        ydl_opts["ffmpeg_location"] = ffmpeg_exe

    last_error = ""
    try:
        print(f"\n[INFO] Starting video extraction for: {url}")
        if IS_VERCEL:
            # On Vercel, extract video URL directly to prevent 15s serverless timeout and 4.5MB payload limits
            with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "http_headers": headers_with_ref}) as ydl:
                info = ydl.extract_info(url, download=False)
                if "entries" in info:
                    entries = [e for e in info.get("entries", []) if e]
                    if entries:
                        info = entries[0]

                formats = info.get("formats", [])
                best_direct_url = None
                # Prefer direct MP4 format
                for f in reversed(formats):
                    f_url = f.get("url")
                    if f_url and f.get("ext") == "mp4" and "m3u8" not in f_url:
                        best_direct_url = f_url
                        break
                if not best_direct_url and formats:
                    best_direct_url = formats[-1].get("url")
                if not best_direct_url:
                    best_direct_url = info.get("url")

                if best_direct_url:
                    title = sanitize_filename(info.get("title") or "video")
                    filename = f"{title}.mp4"
                    tasks_progress[task_id].update({
                        "status": "finished",
                        "percent": 100.0,
                        "percent_str": "100%",
                        "filename": filename,
                        "file_id": file_id,
                        "direct_url": best_direct_url
                    })
                    return file_id, "", filename, best_direct_url

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        pattern = os.path.join(DOWNLOADS_DIR, f"{file_id}_*")
        matches = glob.glob(pattern)
        if matches:
            saved_file = matches[0]
            display_name = os.path.basename(saved_file).replace(f"{file_id}_", "", 1)
            
            if not IS_VERCEL:
                try:
                    shutil.copy2(saved_file, os.path.join(BASE_DIR, display_name))
                    print(f"[SUCCESS] Video saved to Downloads: {display_name}")
                except Exception as e:
                    print(f"[INFO] Could not copy to Downloads root: {e}")

            downloaded_files[file_id] = {"path": saved_file, "filename": display_name}
            tasks_progress[task_id].update({
                "status": "finished",
                "percent": 100.0,
                "percent_str": "100%",
                "filename": display_name,
                "file_id": file_id,
                "direct_url": None
            })
            return file_id, saved_file, display_name, None
    except Exception as ydl_err:
        last_error = str(ydl_err)
        print(f"[WARN] yt-dlp primary extraction failed: {last_error}")

    # 3. Fallback: Parse HTML & Embedded Media Streams
    try:
        print("[INFO] Attempting enhanced fallback scraping...")
        resp = requests.get(url, headers=headers_with_ref, timeout=12)
        resp.raise_for_status()
        html = resp.text
        html_unescaped = html.replace(r"\/", "/")

        candidates = []
        for m in re.finditer(r'<(?:video|source)\b[^>]*\bsrc=["\']([^"\']+)["\']', html, re.I):
            candidates.append(m.group(1))
        for m in re.finditer(r'<iframe\b[^>]*\bsrc=["\']([^"\']+)["\']', html, re.I):
            candidates.append(m.group(1))
        for m in re.finditer(r'<meta\b[^>]*\bproperty=["\']og:video(?::url)?["\'][^>]*\bcontent=["\']([^"\']+)["\']', html, re.I):
            candidates.append(m.group(1))
        for m in re.finditer(r'<meta\b[^>]*\bcontent=["\']([^"\']+)["\'][^>]*\bproperty=["\']og:video(?::url)?["\']', html, re.I):
            candidates.append(m.group(1))
        for m in re.finditer(r'https?://[^\s"\'<>]+\.(?:m3u8|mp4|webm)(?:\?[^\s"\'<>]*)?', html_unescaped, re.I):
            candidate_url = m.group(0)
            if not re.search(r'(?:\.t\.mp4|thumb-|/preview/)', candidate_url, re.I):
                candidates.append(candidate_url)

        title_m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.I)
        page_title = sanitize_filename(title_m.group(1) if title_m else "video")

        for cand in candidates:
            resolved = urllib.parse.urljoin(url, cand.strip().replace(r"\/", "/"))
            if resolved != url:
                try:
                    print(f"[INFO] Trying candidate stream: {resolved}")
                    if IS_VERCEL:
                        filename = f"{page_title}.mp4"
                        tasks_progress[task_id].update({
                            "status": "finished",
                            "percent": 100.0,
                            "percent_str": "100%",
                            "filename": filename,
                            "file_id": file_id,
                            "direct_url": resolved
                        })
                        return file_id, "", filename, resolved

                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([resolved])
                    pattern = os.path.join(DOWNLOADS_DIR, f"{file_id}_*")
                    matches = glob.glob(pattern)
                    if matches:
                        saved_file = matches[0]
                        display_name = os.path.basename(saved_file).replace(f"{file_id}_", "", 1)
                        if not IS_VERCEL:
                            try:
                                shutil.copy2(saved_file, os.path.join(BASE_DIR, display_name))
                            except Exception:
                                pass

                        downloaded_files[file_id] = {"path": saved_file, "filename": display_name}
                        tasks_progress[task_id].update({
                            "status": "finished",
                            "percent": 100.0,
                            "percent_str": "100%",
                            "filename": display_name,
                            "file_id": file_id,
                            "direct_url": None
                        })
                        return file_id, saved_file, display_name, None
                except Exception:
                    continue
    except Exception as scrape_err:
        print(f"[WARN] Fallback scraping failed: {scrape_err}")

    # 4. Error reporting with clear instructions
    clean_err = last_error.split("\n")[0] if last_error else "No downloadable video stream detected."
    if "No video formats found" in clean_err or "403" in clean_err:
        if IS_VERCEL:
            clean_err = "This website blocks cloud/serverless IPs (Vercel). Please run this download on your computer via http://localhost:8000."
        else:
            clean_err = "No video stream found for this URL. Please verify the URL."

    tasks_progress[task_id].update({
        "status": "error",
        "error": clean_err
    })
    raise RuntimeError(clean_err)

class DownloadRequest(BaseModel):
    url: str

@app.get("/api/debug_inspect")
async def debug_inspect(url: str):
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://google.com/",
        "Cookie": "cookie_accept_v2=%7B%22e%22%3A1%2C%22f%22%3A1%2C%22t%22%3A1%2C%22a%22%3A1%7D; age_verified=1; is_age_verified=1; confirmed_age=1; shouldConfirmSession=false"
    }
    r = requests.get(url, headers=headers)
    has_age_wall = "age-verification-wall" in r.text
    has_player = "xplayerSettings" in r.text
    has_video_model = "videoModel" in r.text
    title_matches = [line.strip() for line in r.text.splitlines() if '<title' in line]
    return {
        "status_code": r.status_code,
        "has_age_wall": has_age_wall,
        "has_player": has_player,
        "has_video_model": has_video_model,
        "title": title_matches[0] if title_matches else None
    }

@app.get("/")
async def serve_root():
    index_path = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return HTMLResponse("<h1>SachDownload is running</h1>")

@app.get("/index.html")
async def serve_index():
    index_path = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return HTMLResponse("<h1>SachDownload is running</h1>")

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
@app.post("/download/start")
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
        "direct_url": None,
        "error": None
    }

    if IS_VERCEL:
        # On Vercel, run directly so serverless function doesn't freeze background tasks
        try:
            file_id, filepath, filename, direct_url = await asyncio.to_thread(download_video_sync, url, task_id)
            return {"success": True, "task_id": task_id, "direct_url": direct_url, "filename": filename}
        except Exception as e:
            return JSONResponse({"success": False, "task_id": task_id, "error": str(e)}, status_code=500)

    asyncio.create_task(asyncio.to_thread(download_video_sync, url, task_id))
    return {"success": True, "task_id": task_id}

@app.get("/api/download/progress/{task_id}")
@app.get("/download/progress/{task_id}")
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
        file_id, filepath, filename, direct_url = await asyncio.to_thread(download_video_sync, url)
        return {"success": True, "file_id": file_id, "filename": filename, "direct_url": direct_url}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/api/file/{file_id}")
@app.get("/file/{file_id}")
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
        file_id, filepath, filename, direct_url = await asyncio.to_thread(download_video_sync, url)
        if direct_url:
            return RedirectResponse(direct_url)
        return FileResponse(
            filepath,
            filename=filename,
            media_type="application/octet-stream"
        )
    except Exception as e:
        return HTMLResponse(f"<p>Error downloading video: {e}</p><p><a href='/'>Go back</a></p>", status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
