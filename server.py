import asyncio
import glob
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except Exception:
    curl_requests = None
    HAS_CURL_CFFI = False

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
except Exception:
    ImpersonateTarget = None

try:
    from yt_dlp.extractor.xhamster import XHamsterIE
    _orig_xh_real_extract = XHamsterIE._real_extract
except Exception:
    XHamsterIE = None
    _orig_xh_real_extract = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_VERCEL = (
    bool(os.environ.get("VERCEL"))
    or "LAMBDA_TASK_ROOT" in os.environ
    or "AWS_LAMBDA_FUNCTION_NAME" in os.environ
)

if IS_VERCEL:
    DOWNLOADS_DIR = os.path.join(tempfile.gettempdir(), "downloads_cache")
else:
    DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads_cache")

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

AGE_COOKIES = (
    "cookie_accept_v2=%7B%22e%22%3A1%2C%22f%22%3A1%2C%22t%22%3A1%2C%22a%22%3A1%7D; "
    "age_verified=1; is_age_verified=1; confirmed_age=1; shouldConfirmSession=false"
)

ALLOWED_PORTS = {80, 443, 8080, 8443}
VIDEO_EXTS = {"mp4", "m4v", "webm", "mov", "mkv", "ogv", "3gp", "ts"}
AUDIO_EXTS = {"m4a", "mp3", "aac", "ogg", "opus", "wav", "flac"}
IGNORED_EMBED_HOSTS = (
    "accounts.google.com",
    "googletagmanager.com",
    "google-analytics.com",
    "googlesyndication.com",
    "doubleclick.net",
    "facebook.com/plugins",
    "recaptcha.net",
    "google.com/recaptcha",
    "hcaptcha.com",
    "challenges.cloudflare.com",
)
PREVIEW_URL_RE = re.compile(r"(\.t\.mp4|thumb-|/preview/|/thumbs/|storyboard)", re.I)

downloaded_files: dict[str, dict[str, str]] = {}
tasks_progress: dict[str, dict[str, Any]] = {}
_FFMPEG_CACHE: Optional[str] = None
_FFMPEG_CHECKED = False


def _is_executable_ffmpeg(path: Optional[str]) -> bool:
    if not path or not os.path.exists(path):
        return False
    try:
        kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": 5}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        res = subprocess.run([path, "-version"], **kwargs)
        return res.returncode == 0
    except Exception:
        return False


def get_ffmpeg() -> Optional[str]:
    """Return a verified executable ffmpeg path, avoiding OS-blocked unsigned binaries."""
    global _FFMPEG_CACHE, _FFMPEG_CHECKED
    if _FFMPEG_CHECKED:
        return _FFMPEG_CACHE
    candidates = [shutil.which("ffmpeg")]
    try:
        import imageio_ffmpeg
        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    for cand in candidates:
        if _is_executable_ffmpeg(cand):
            _FFMPEG_CACHE = cand
            break
    _FFMPEG_CHECKED = True
    return _FFMPEG_CACHE


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|\r\n\t]+', " ", str(name or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(". ")
    return (cleaned[:90].strip() or "video")


NAT64_NET = ipaddress.ip_network("64:ff9b::/96")


def _is_blocked_ip_obj(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in NAT64_NET:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_blocked_ip_str(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return _is_blocked_ip_obj(ip)


def validate_public_url(url: str, require_public: bool = True) -> urllib.parse.ParseResult:
    """Ensure URL is http(s), free of embedded credentials, and does not target private IPs."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Please provide a valid http:// or https:// URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not supported.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if require_public and port not in ALLOWED_PORTS:
        raise ValueError("URL port is not allowed.")

    host = parsed.hostname.strip("[]")
    if require_public:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise ValueError(f"Could not resolve host {host}: {e}") from e
        if not infos:
            raise ValueError(f"Could not resolve host {host}.")
        for info in infos:
            addr = info[4][0].split("%")[0]
            if _is_blocked_ip_str(addr):
                raise ValueError("Target address is not allowed.")
    return parsed


def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    timeout: int = 15,
    stream: bool = False,
    impersonate: bool = True,
    max_redirects: int = 5,
    require_public: bool = IS_VERCEL,
):
    """Make an HTTP request with redirect SSRF validation and optional Chrome TLS impersonation."""
    req_headers = {**DEFAULT_HEADERS, **(headers or {})}
    current = url
    for _ in range(max_redirects + 1):
        validate_public_url(current, require_public=require_public)
        if impersonate and HAS_CURL_CFFI:
            resp = curl_requests.request(
                method,
                current,
                headers=req_headers,
                timeout=timeout,
                stream=stream,
                allow_redirects=False,
                impersonate="chrome",
            )
        else:
            resp = requests.request(
                method,
                current,
                headers=req_headers,
                timeout=timeout,
                stream=stream,
                allow_redirects=False,
            )

        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
            nxt = urllib.parse.urljoin(current, resp.headers["location"])
            resp.close()
            current = nxt
            continue
        return resp
    raise ValueError("Too many redirects.")


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

    desktop_url = re.sub(r"^(https?://(?:.+?\.)?)m\.", r"\1", url)
    mobj = self._match_valid_url(url)
    video_id = (mobj.group("id") or mobj.group("id_2")) if mobj else "video"

    webpage = None
    try:
        resp = http_request(
            desktop_url,
            headers={"Referer": "https://www.google.com/", "Cookie": AGE_COOKIES},
            timeout=15,
            require_public=False,
        )
        if resp.status_code < 400:
            webpage = resp.text
        resp.close()
    except Exception:
        pass

    if not webpage:
        webpage = self._download_webpage(desktop_url, video_id, fatal=False)
    if not webpage:
        if info:
            return info
        raise ExtractorError("No video formats found!")

    title = self._html_search_regex(
        [r"<h1[^>]*>([^<]+)</h1>", r"<title[^>]*>([^<]+)</title>"],
        webpage,
        "title",
        default="video",
    )
    unescaped = webpage.replace(r"\/", "/").replace(r"\u002f", "/").replace(r"\u0026", "&")
    formats = []
    seen = set()

    for m in re.findall(r'https?://[^\s"\'<>\\]+\.(?:mp4|m3u8)(?:\?[^\s"\'<>\\]*)?', unescaped):
        if PREVIEW_URL_RE.search(m) or m in seen:
            continue
        seen.add(m)
        if ".m3u8" in m:
            m3u8_formats = self._extract_m3u8_formats(
                m, video_id, "mp4", entry_protocol="m3u8_native", fatal=False
            )
            if m3u8_formats:
                formats.extend(m3u8_formats)
        else:
            q_match = re.search(r"(\d{3,4})p", m)
            height = int(q_match.group(1)) if q_match else None
            formats.append({
                "url": m,
                "format_id": f"http-{height or 'mp4'}",
                "ext": "mp4",
                "height": height,
                "http_headers": {"Referer": url},
            })

    if not formats and info:
        return info
    return {"id": video_id, "title": title, "formats": formats}


if XHamsterIE is not None and _orig_xh_real_extract is not None:
    XHamsterIE._real_extract = _safe_xh_real_extract


def _parse_content_range_size(headers: Any) -> Optional[int]:
    cr = headers.get("content-range") or headers.get("Content-Range") or ""
    m = re.search(r"/(\d+)$", cr.strip())
    if m:
        return int(m.group(1))
    cl = headers.get("content-length") or headers.get("Content-Length")
    if cl and str(cl).isdigit():
        return int(cl)
    return None


def _is_hls_playlist(content_type: str, url: str) -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if "mpegurl" in ctype:
        return True
    low = url.lower()
    path = urllib.parse.urlparse(low).path
    return path.endswith(".m3u8") or "/hls_playlist/" in low


def _read_stream_text(resp: Any, max_bytes: int = 2_000_000) -> str:
    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=16384):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break
    if not chunks and getattr(resp, "content", None):
        return resp.content[:max_bytes].decode("utf-8", "replace")
    return b"".join(chunks).decode("utf-8", "replace")


def parse_m3u8(text: str, base_url: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("Invalid HLS playlist.")

    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        variants = []
        audio_groups: dict[str, str] = {}
        for line in lines:
            if line.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line.upper():
                gid_m = re.search(r'GROUP-ID="([^"]+)"', line)
                uri_m = re.search(r'URI="([^"]+)"', line)
                if gid_m and uri_m:
                    audio_groups[gid_m.group(1)] = urllib.parse.urljoin(base_url, uri_m.group(1))
        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue
            bw_m = re.search(r"BANDWIDTH=(\d+)", line)
            res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            aud_m = re.search(r'AUDIO="([^"]+)"', line)
            next_uri = next((l for l in lines[i + 1 :] if not l.startswith("#")), None)
            if next_uri:
                audio_group = aud_m.group(1) if aud_m else None
                variants.append({
                    "url": urllib.parse.urljoin(base_url, next_uri),
                    "bandwidth": int(bw_m.group(1)) if bw_m else 0,
                    "width": int(res_m.group(1)) if res_m else None,
                    "height": int(res_m.group(2)) if res_m else None,
                    "audio_url": audio_groups.get(audio_group) if audio_group else None,
                })
        variants.sort(key=lambda v: (v.get("height") or 0, v.get("bandwidth") or 0), reverse=True)
        return {"kind": "master", "variants": variants}

    segments = []
    init_map = None
    encrypted_method = None
    endlist = False
    pending_range = None
    last_end = 0

    for line in lines:
        if line.startswith("#EXT-X-KEY"):
            m = re.search(r"METHOD=([A-Z0-9-]+)", line, re.I)
            method = (m.group(1).upper() if m else "NONE")
            if method != "NONE":
                encrypted_method = method
        elif line.startswith("#EXT-X-MAP"):
            uri_m = re.search(r'URI="([^"]+)"', line)
            br_m = re.search(r'BYTERANGE="(\d+)(?:@(\d+))?"', line)
            if uri_m:
                br = None
                if br_m:
                    length = int(br_m.group(1))
                    offset = int(br_m.group(2)) if br_m.group(2) is not None else 0
                    br = (offset, offset + length - 1)
                init_map = {"url": urllib.parse.urljoin(base_url, uri_m.group(1)), "range": br}
        elif line.startswith("#EXT-X-BYTERANGE:"):
            br_m = re.search(r"(\d+)(?:@(\d+))?", line.split(":", 1)[1])
            if br_m:
                length = int(br_m.group(1))
                offset = int(br_m.group(2)) if br_m.group(2) is not None else last_end
                pending_range = (offset, offset + length - 1)
                last_end = offset + length
        elif line.startswith("#EXT-X-ENDLIST"):
            endlist = True
        elif not line.startswith("#"):
            segments.append({
                "url": urllib.parse.urljoin(base_url, line),
                "range": pending_range,
            })
            pending_range = None

    return {
        "kind": "media",
        "segments": segments,
        "init": init_map,
        "encrypted_method": encrypted_method,
        "endlist": endlist,
        "ext": "mp4" if init_map else "ts",
    }


def _classify_format(fmt: dict[str, Any], webpage_url: str) -> Optional[dict[str, Any]]:
    f_url = fmt.get("url")
    if not f_url or not str(f_url).startswith(("http://", "https://")):
        return None
    ext = (fmt.get("ext") or "").lower().strip(".")
    if ext == "mhtml" or fmt.get("vcodec") == "none" and fmt.get("acodec") == "none":
        return None

    proto = (fmt.get("protocol") or "").lower()
    is_hls = "m3u8" in proto or _is_hls_playlist("", f_url)
    vcodec = fmt.get("vcodec")
    acodec = fmt.get("acodec")

    if vcodec is None and acodec is None:
        has_video = ext in VIDEO_EXTS or not ext or ext not in AUDIO_EXTS
        has_audio = ext in VIDEO_EXTS or ext in AUDIO_EXTS or not ext
    else:
        has_video = vcodec != "none"
        has_audio = acodec != "none"

    if not has_video and not has_audio:
        return None

    height = fmt.get("height")
    if not height and fmt.get("resolution"):
        m = re.search(r"x(\d+)$", str(fmt["resolution"]))
        if m:
            height = int(m.group(1))

    if not ext:
        ext = "ts" if is_hls else ("m4a" if not has_video else "mp4")

    parts = []
    if has_video and height:
        parts.append(f"{height}p")
    elif has_video:
        parts.append("Video")
    else:
        parts.append("Audio")
    parts.append(ext.upper())
    if is_hls:
        parts.append("HLS")
    elif has_video and has_audio:
        parts.append("Video+Audio")
    elif has_video:
        parts.append("Video Only")
    else:
        parts.append("Audio Only")

    filesize = fmt.get("filesize") or fmt.get("filesize_approx")
    headers = dict(fmt.get("http_headers") or {})
    headers.setdefault("Referer", webpage_url)

    return {
        "format_id": str(fmt.get("format_id") or ("hls" if is_hls else ext)),
        "label": " · ".join(parts),
        "ext": "mp4" if is_hls and ext == "mp4" else ext,
        "height": int(height) if height else 0,
        "tbr": float(fmt.get("tbr") or 0),
        "filesize": int(filesize) if filesize else None,
        "has_video": bool(has_video),
        "has_audio": bool(has_audio),
        "muxed": bool(has_video and has_audio),
        "is_hls": is_hls,
        "needs_merge": bool(has_video and not has_audio and not is_hls),
        "url": f_url,
        "http_headers": headers,
        "ip_bound": _looks_ip_bound(f_url),
    }


def _looks_ip_bound(url: str) -> bool:
    return "googlevideo.com" in url.lower() and "ip=" in url.lower()


def _sort_and_dedupe_formats(raw_formats: list[dict[str, Any]], webpage_url: str) -> list[dict[str, Any]]:
    normalized = []
    seen_urls = set()
    for f in raw_formats:
        item = _classify_format(f, webpage_url)
        if not item or item["url"] in seen_urls:
            continue
        seen_urls.add(item["url"])
        normalized.append(item)

    def sort_key(item: dict[str, Any]):
        # Prefer muxed MP4/non-HLS with both video & audio, then HLS, then video-only, then audio-only.
        tier = 0
        if item["muxed"] and not item["is_hls"] and item["ext"] == "mp4":
            tier = 5
        elif item["muxed"] and not item["is_hls"]:
            tier = 4
        elif item["is_hls"] and item["has_video"]:
            tier = 3
        elif item["has_video"]:
            tier = 2
        else:
            tier = 1
        return (tier, item["height"], item["tbr"], item["filesize"] or 0)

    normalized.sort(key=sort_key, reverse=True)
    return normalized


def _build_format_urls(item: dict[str, Any], title: str, referer: str) -> dict[str, Any]:
    ext = "ts" if item["is_hls"] and item["ext"] != "mp4" else item["ext"]
    fname = f"{sanitize_filename(title)}.{ext}"
    params = urllib.parse.urlencode({
        "url": item["url"],
        "ref": item["http_headers"].get("Referer") or referer,
        "name": fname,
    })
    out = dict(item)
    out["filename"] = fname
    out["proxy_url"] = f"/api/proxy?{params}"
    out["direct_url"] = None if (item["is_hls"] or item["ip_bound"]) else item["url"]
    return out


def _try_direct_media(url: str, referer: Optional[str] = None) -> Optional[dict[str, Any]]:
    headers = {} if _is_hls_playlist("", url) else {"Range": "bytes=0-2047"}
    if referer:
        headers["Referer"] = referer
    try:
        resp = http_request(url, method="GET", headers=headers, timeout=10, stream=True)
    except Exception:
        return None

    try:
        if resp.status_code >= 400:
            return None
        final_url = str(resp.url)
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        path = urllib.parse.urlparse(final_url).path
        base = sanitize_filename(urllib.parse.unquote(os.path.basename(path)) or "video")
        ext = os.path.splitext(path)[1].lstrip(".").lower()

        if _is_hls_playlist(ctype, final_url):
            if resp.status_code == 206:
                resp.close()
                resp = http_request(
                    final_url,
                    method="GET",
                    headers={"Referer": referer} if referer else {},
                    timeout=10,
                    stream=True,
                )
            body = _read_stream_text(resp)
            if "#EXTM3U" not in body:
                return None
            parsed = parse_m3u8(body, final_url)
            formats = []
            if parsed["kind"] == "master":
                for idx, v in enumerate(parsed["variants"]):
                    h = v.get("height")
                    formats.append({
                        "format_id": f"hls-{h or idx}",
                        "ext": "ts",
                        "height": h,
                        "tbr": round((v.get("bandwidth") or 0) / 1000, 1),
                        "protocol": "m3u8_native",
                        "vcodec": "h264",
                        "acodec": "none" if v.get("audio_url") else "aac",
                        "url": v["url"],
                        "http_headers": {"Referer": referer or final_url},
                    })
                    if v.get("audio_url"):
                        formats.append({
                            "format_id": f"hls-audio-{idx}",
                            "ext": "m4a",
                            "vcodec": "none",
                            "acodec": "aac",
                            "protocol": "m3u8_native",
                            "url": v["audio_url"],
                            "http_headers": {"Referer": referer or final_url},
                        })
            else:
                formats.append({
                    "format_id": "hls-0",
                    "ext": parsed["ext"],
                    "protocol": "m3u8_native",
                    "vcodec": "h264",
                    "acodec": "aac",
                    "url": final_url,
                    "http_headers": {"Referer": referer or final_url},
                })
            return {
                "title": base,
                "webpage_url": final_url,
                "extractor": "direct-hls",
                "formats": _sort_and_dedupe_formats(formats, final_url),
            }

        is_media_ctype = ctype.startswith(("video/", "audio/")) or ctype in ("application/mp4", "application/ogg") or (
            ctype in ("application/octet-stream", "binary/octet-stream") and ext in (VIDEO_EXTS | AUDIO_EXTS)
        )
        if not is_media_ctype:
            return None

        if not ext:
            ext = "mp4" if ctype.startswith("video/") else "m4a"
        size = _parse_content_range_size(resp.headers)
        fmt = {
            "format_id": f"direct-{ext}",
            "ext": ext,
            "filesize": size,
            "vcodec": "none" if ctype.startswith("audio/") else "unknown",
            "acodec": "unknown",
            "url": final_url,
            "http_headers": {"Referer": referer or final_url},
        }
        return {
            "title": os.path.splitext(base)[0] or "video",
            "webpage_url": final_url,
            "extractor": "direct",
            "formats": _sort_and_dedupe_formats([fmt], final_url),
        }
    finally:
        resp.close()


def _try_ytdlp(url: str, use_impersonate: bool = False) -> tuple[Optional[dict[str, Any]], str]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": 15,
        "retries": 2,
    }
    node_path = shutil.which("node")
    if node_path:
        opts["js_runtimes"] = {"node": {"path": node_path}}
    if use_impersonate and ImpersonateTarget is not None:
        opts["impersonate"] = ImpersonateTarget("chrome")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None, "No video information returned."
            if "entries" in info:
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    return None, "No entries found."
                info = entries[0]

            raw_formats = list(info.get("formats") or [])
            if not raw_formats and info.get("url"):
                raw_formats.append({
                    "format_id": info.get("format_id") or "direct",
                    "ext": info.get("ext") or "mp4",
                    "height": info.get("height"),
                    "url": info["url"],
                    "http_headers": info.get("http_headers") or {"Referer": url},
                })
            formats = _sort_and_dedupe_formats(raw_formats, info.get("webpage_url") or url)
            if not formats:
                return None, "No playable video formats found."
            return {
                "title": sanitize_filename(info.get("title") or "video"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "webpage_url": info.get("webpage_url") or url,
                "extractor": info.get("extractor_key") or info.get("extractor") or "yt-dlp",
                "formats": formats,
            }, ""
    except Exception as e:
        return None, str(e)


def _extract_youtube_id(url: str) -> Optional[str]:
    m = re.search(
        r"(?:youtube\.com/(?:watch\?.*?v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})",
        url,
    )
    return m.group(1) if m else None


def _try_youtube_innertube(url: str, muxed_only: bool = False) -> tuple[Optional[dict[str, Any]], str]:
    vid = _extract_youtube_id(url)
    if not vid:
        return None, ""

    clients = [
        (
            "ANDROID_VR",
            "1.60.19",
            {"androidSdkVersion": 32},
            "com.google.android.apps.youtube.vr.oculus/1.60.19 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip",
        ),
        (
            "VISIONOS",
            "0.1",
            {},
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        ),
    ]

    last_reason = ""
    for cname, cver, cextra, ua in clients:
        payload = {
            "videoId": vid,
            "context": {
                "client": {
                    "clientName": cname,
                    "clientVersion": cver,
                    "hl": "en",
                    "gl": "US",
                    **cextra,
                }
            },
        }
        try:
            r = requests.post(
                "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                json=payload,
                headers={"User-Agent": ua},
                timeout=12,
            )
            if not r.ok:
                last_reason = f"YouTube returned HTTP {r.status_code}"
                continue
            data = r.json()
            status = (data.get("playabilityStatus") or {}).get("status", "")
            if status != "OK":
                last_reason = (data.get("playabilityStatus") or {}).get("reason") or status
                continue
            sd = data.get("streamingData") or {}
            vd = data.get("videoDetails") or {}
            raw_fmts = []
            source_list = sd.get("formats", []) if muxed_only else (sd.get("formats", []) + sd.get("adaptiveFormats", []))
            for f in source_list:
                f_url = f.get("url")
                if not f_url:
                    continue
                is_muxed = f in sd.get("formats", [])
                if is_muxed:
                    try:
                        pr = requests.get(f_url, headers={"User-Agent": ua, "Range": "bytes=0-63"}, timeout=6)
                        if pr.status_code not in (200, 206):
                            continue
                    except Exception:
                        continue
                mime = f.get("mimeType", "")
                is_audio_only = mime.startswith("audio/")
                ext = "mp4" if "mp4" in mime else ("webm" if "webm" in mime else "m4a" if is_audio_only else "mp4")
                raw_fmts.append({
                    "format_id": str(f.get("itag") or "yt"),
                    "ext": "m4a" if (is_audio_only and ext == "mp4") else ext,
                    "height": f.get("height"),
                    "tbr": round((f.get("bitrate") or 0) / 1000, 1),
                    "filesize": int(f["contentLength"]) if str(f.get("contentLength", "")).isdigit() else None,
                    "vcodec": "none" if is_audio_only else "avc1",
                    "acodec": "mp4a" if (is_muxed or is_audio_only) else "none",
                    "url": f_url,
                    "http_headers": {"Referer": "https://www.youtube.com/", "User-Agent": ua},
                })
            formats = _sort_and_dedupe_formats(raw_fmts, url)
            if formats:
                return {
                    "title": sanitize_filename(vd.get("title") or "youtube_video"),
                    "duration": int(vd["lengthSeconds"]) if str(vd.get("lengthSeconds", "")).isdigit() else None,
                    "webpage_url": url,
                    "extractor": f"youtube-{cname.lower()}",
                    "formats": formats,
                }, ""
        except Exception as e:
            last_reason = str(e)
    return None, last_reason or "No direct YouTube stream URLs available."


def _try_platform_apis(url: str) -> Optional[dict[str, Any]]:
    # Vimeo
    vm = re.search(r"(?:vimeo\.com/(?:channels/[^/]+/|groups/[^/]+/videos/|video/)?|player\.vimeo\.com/video/)(\d+)", url)
    if vm:
        vid = vm.group(1)
        h_match = re.search(r"[?&]h=([a-zA-Z0-9]+)", url)
        cfg_url = f"https://player.vimeo.com/video/{vid}/config" + (f"?h={h_match.group(1)}" if h_match else "")
        try:
            r = http_request(cfg_url, headers={"Referer": "https://vimeo.com/"}, timeout=12)
            if r.ok:
                data = r.json()
                files = (data.get("request") or {}).get("files") or {}
                raw_fmts = []
                for p in files.get("progressive") or []:
                    if p.get("url"):
                        raw_fmts.append({
                            "format_id": f"vimeo-{p.get('quality') or p.get('height')}",
                            "ext": "mp4",
                            "height": p.get("height"),
                            "vcodec": "h264",
                            "acodec": "aac",
                            "url": p["url"],
                            "http_headers": {"Referer": "https://vimeo.com/"},
                        })
                hls = files.get("hls") or {}
                default_cdn = hls.get("default_cdn")
                cdns = hls.get("cdns") or {}
                hls_url = (cdns.get(default_cdn) or {}).get("url") if default_cdn else None
                if hls_url and "/drm/" not in hls_url.lower():
                    raw_fmts.append({
                        "format_id": "vimeo-hls",
                        "ext": "mp4",
                        "protocol": "m3u8_native",
                        "vcodec": "h264",
                        "acodec": "aac",
                        "url": hls_url,
                        "http_headers": {"Referer": "https://vimeo.com/"},
                    })
                fmts = _sort_and_dedupe_formats(raw_fmts, url)
                if fmts:
                    video_meta = data.get("video") or {}
                    return {
                        "title": sanitize_filename(video_meta.get("title") or f"vimeo_{vid}"),
                        "duration": video_meta.get("duration"),
                        "webpage_url": url,
                        "extractor": "vimeo-config",
                        "formats": fmts,
                    }
                if hls_url and "/drm/" in hls_url.lower():
                    raise RuntimeError("This Vimeo stream uses DRM (CBCS) protection and cannot be downloaded.")
        except RuntimeError:
            raise
        except Exception:
            pass

    # Dailymotion
    dm = re.search(r"(?:dailymotion\.com/(?:video|embed/video)/|dai\.ly/)([A-Za-z0-9]+)", url)
    if dm:
        vid = dm.group(1)
        try:
            r = http_request(
                f"https://www.dailymotion.com/player/metadata/video/{vid}",
                headers={"Referer": "https://www.dailymotion.com/"},
                timeout=12,
            )
            if r.ok:
                data = r.json()
                if not data.get("error"):
                    raw_fmts = []
                    for q_label, entries in (data.get("qualities") or {}).items():
                        for e in entries or []:
                            e_url = e.get("url")
                            if not e_url:
                                continue
                            h = int(q_label) if q_label.isdigit() else None
                            mime = (e.get("type") or "").lower()
                            is_hls = "mpegurl" in mime or ".m3u8" in e_url
                            raw_fmts.append({
                                "format_id": f"dm-{q_label}",
                                "ext": "mp4",
                                "height": h,
                                "protocol": "m3u8_native" if is_hls else "https",
                                "vcodec": "h264",
                                "acodec": "aac",
                                "url": e_url,
                                "http_headers": {"Referer": "https://www.dailymotion.com/"},
                            })
                    fmts = _sort_and_dedupe_formats(raw_fmts, url)
                    if fmts:
                        return {
                            "title": sanitize_filename(data.get("title") or f"dailymotion_{vid}"),
                            "duration": data.get("duration"),
                            "webpage_url": url,
                            "extractor": "dailymotion-meta",
                            "formats": fmts,
                        }
        except RuntimeError:
            raise
        except Exception:
            pass

    # X / Twitter
    xm = re.search(r"(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)", url)
    if xm:
        user, tid = xm.group(1), xm.group(2)
        try:
            r = http_request(f"https://api.fxtwitter.com/{user}/status/{tid}", timeout=12)
            if r.ok:
                tweet = (r.json() or {}).get("tweet") or {}
                media = tweet.get("media") or {}
                raw_fmts = []
                for idx, v in enumerate(media.get("videos") or []):
                    for var in v.get("variants") or []:
                        if var.get("url") and "mp4" in (var.get("content_type") or "mp4"):
                            m_res = re.search(r"/(\d+)x(\d+)/", var["url"])
                            raw_fmts.append({
                                "format_id": f"x-{idx}-{var.get('bitrate', 0)}",
                                "ext": "mp4",
                                "height": int(m_res.group(2)) if m_res else v.get("height"),
                                "tbr": round((var.get("bitrate") or 0) / 1000, 1),
                                "vcodec": "h264",
                                "acodec": "aac",
                                "url": var["url"],
                                "http_headers": {"Referer": "https://x.com/"},
                            })
                    if v.get("url") and not raw_fmts:
                        raw_fmts.append({
                            "format_id": f"x-{idx}",
                            "ext": "mp4",
                            "height": v.get("height"),
                            "vcodec": "h264",
                            "acodec": "aac",
                            "url": v["url"],
                            "http_headers": {"Referer": "https://x.com/"},
                        })
                fmts = _sort_and_dedupe_formats(raw_fmts, url)
                if fmts:
                    txt = (tweet.get("text") or f"tweet_{tid}")[:70]
                    return {
                        "title": sanitize_filename(txt),
                        "webpage_url": url,
                        "extractor": "x-fxtwitter",
                        "formats": fmts,
                    }
        except RuntimeError:
            raise
        except Exception:
            pass

    # Reddit
    rm = re.search(r"reddit\.com/(r/[^/]+/comments/[a-z0-9]+)", url, re.I)
    if rm:
        try:
            r = http_request(
                f"https://old.reddit.com/{rm.group(1)}.json",
                headers={"User-Agent": "SachDownload/2.0 (web video tool)"},
                timeout=12,
                impersonate=False,
            )
            if r.ok:
                listing = r.json()
                post = listing[0]["data"]["children"][0]["data"]
                rv = ((post.get("secure_media") or post.get("media") or {}).get("reddit_video")) or {}
                raw_fmts = []
                if rv.get("hls_url"):
                    raw_fmts.append({
                        "format_id": "reddit-hls",
                        "ext": "mp4",
                        "height": rv.get("height"),
                        "protocol": "m3u8_native",
                        "vcodec": "h264",
                        "acodec": "aac",
                        "url": rv["hls_url"],
                        "http_headers": {"Referer": "https://www.reddit.com/"},
                    })
                if rv.get("fallback_url"):
                    raw_fmts.append({
                        "format_id": "reddit-mp4",
                        "ext": "mp4",
                        "height": rv.get("height"),
                        "vcodec": "h264",
                        "acodec": "none",
                        "url": rv["fallback_url"],
                        "http_headers": {"Referer": "https://www.reddit.com/"},
                    })
                fmts = _sort_and_dedupe_formats(raw_fmts, url)
                if fmts:
                    return {
                        "title": sanitize_filename(post.get("title") or "reddit_video"),
                        "webpage_url": url,
                        "extractor": "reddit-json",
                        "formats": fmts,
                    }
        except RuntimeError:
            raise
        except Exception:
            pass

    return None


def _is_login_or_block_url(url: str) -> bool:
    low = url.lower()
    return any(tok in low for tok in ("accounts.google.com/servicelogin", "/challenge", "captcha"))


def _try_html_fallback(url: str, depth: int = 0) -> Optional[dict[str, Any]]:
    if depth > 2 or _is_login_or_block_url(url):
        return None
    try:
        resp = http_request(
            url,
            headers={"Referer": url, "Cookie": AGE_COOKIES},
            timeout=12,
        )
        if resp.status_code >= 400:
            resp.close()
            return None
        final_url = str(resp.url)
        if _is_login_or_block_url(final_url):
            resp.close()
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        if "html" not in ctype and "xml" not in ctype and "text/" not in ctype:
            resp.close()
            return _try_direct_media(final_url, referer=url)
        html = resp.text[:2_500_000]
        resp.close()
    except Exception:
        return None

    title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
    page_title = sanitize_filename(title_m.group(1) if title_m else "video")

    unescaped = (
        html.replace(r"\/", "/")
        .replace(r"\u002f", "/")
        .replace(r"\u002F", "/")
        .replace(r"\u0026", "&")
        .replace(r"\u003d", "=")
        .replace(r"\x2f", "/")
        .replace("&amp;", "&")
    )

    media_candidates: list[str] = []
    seen: set[str] = set()

    def add_cand(raw: str):
        val = raw.strip()
        if not val or val.startswith(("blob:", "data:", "javascript:")):
            return
        resolved = urllib.parse.urljoin(final_url, val)
        if resolved == final_url or resolved in seen or PREVIEW_URL_RE.search(resolved):
            return
        if _is_login_or_block_url(resolved):
            return
        seen.add(resolved)
        media_candidates.append(resolved)

    for m in re.finditer(
        r'<(?:video|source)\b[^>]*\b(?:src|data-src|data-video-src|data-hls|data-mp4)=["\']([^"\']+)["\']',
        html,
        re.I,
    ):
        add_cand(m.group(1))
    for m in re.finditer(
        r'<meta\b[^>]*\b(?:property|name)=["\'](?:og:video(?::url|:secure_url)?|twitter:player:stream)["\'][^>]*\bcontent=["\']([^"\']+)["\']',
        html,
        re.I,
    ):
        add_cand(m.group(1))
    for m in re.finditer(
        r'"(?:contentUrl|videoUrl|video_url|playbackUrl|hlsUrl|mp4Url|streamUrl|file)"\s*:\s*"([^"]+)"',
        unescaped,
        re.I,
    ):
        add_cand(m.group(1))
    for m in re.finditer(
        r'https?://[^\s"\'<>\\)]+\.(?:m3u8|mp4|webm|m4v|mov)(?:\?[^\s"\'<>\\)]*)?',
        unescaped,
        re.I,
    ):
        add_cand(m.group(0))

    raw_fmts = []
    for cand in media_candidates[:15]:
        verified = _try_direct_media(cand, referer=final_url)
        if verified and verified.get("formats"):
            for f in verified["formats"]:
                raw_fmts.append({
                    "format_id": f["format_id"],
                    "ext": f["ext"],
                    "height": f["height"],
                    "filesize": f["filesize"],
                    "protocol": "m3u8_native" if f["is_hls"] else "https",
                    "vcodec": "h264" if f["has_video"] else "none",
                    "acodec": "aac" if f["has_audio"] else "none",
                    "url": f["url"],
                    "http_headers": {"Referer": final_url},
                })
            if len(raw_fmts) >= 6:
                break

    if raw_fmts:
        return {
            "title": page_title,
            "webpage_url": final_url,
            "extractor": "html-scraper",
            "formats": _sort_and_dedupe_formats(raw_fmts, final_url),
        }

    # Recursively inspect non-ad video iframe embeds
    if depth < 2:
        iframe_urls = []
        for m in re.finditer(r'<iframe\b[^>]*\b(?:src|data-src)=["\']([^"\']+)["\']', html, re.I):
            raw_src = m.group(1).strip().replace("&amp;", "&")
            if not raw_src or raw_src.startswith(("about:", "javascript:", "data:")):
                continue
            resolved = urllib.parse.urljoin(final_url, raw_src)
            low = resolved.lower()
            if any(bad in low for bad in IGNORED_EMBED_HOSTS):
                continue
            if resolved not in iframe_urls:
                iframe_urls.append(resolved)

        for emb in iframe_urls[:3]:
            res = extract_video_info(emb, depth=depth + 1)
            if res and res.get("formats"):
                if res.get("title") in ("video", "") and page_title != "video":
                    res["title"] = page_title
                return res

    return None


def extract_video_info(url: str, depth: int = 0) -> dict[str, Any]:
    url = url.strip()
    validate_public_url(url, require_public=IS_VERCEL)

    direct = _try_direct_media(url)
    if direct and direct.get("formats"):
        return direct

    ytdlp_info, ytdlp_err = _try_ytdlp(url, use_impersonate=False)
    if ytdlp_info and ytdlp_info.get("formats"):
        if _extract_youtube_id(url) and not any(f["muxed"] for f in ytdlp_info["formats"]):
            muxed_yt, _ = _try_youtube_innertube(url, muxed_only=True)
            if muxed_yt and muxed_yt.get("formats"):
                ytdlp_info["formats"] = muxed_yt["formats"] + ytdlp_info["formats"]
        return ytdlp_info

    # Retry yt-dlp with Chrome TLS impersonation for 403 / Cloudflare blocks
    if any(k in ytdlp_err.lower() for k in ("403", "forbidden", "cloudflare")):
        ytdlp_imp, imp_err = _try_ytdlp(url, use_impersonate=True)
        if ytdlp_imp and ytdlp_imp.get("formats"):
            return ytdlp_imp
        if imp_err:
            ytdlp_err = imp_err

    # Platform-specific API fallbacks (Vimeo config, Dailymotion metadata, FXTwitter, Reddit JSON, YouTube InnerTube)
    plat = _try_platform_apis(url)
    if plat and plat.get("formats"):
        return plat

    yt_fallback, yt_fb_err = _try_youtube_innertube(url)
    if yt_fallback and yt_fallback.get("formats"):
        return yt_fallback
    if yt_fb_err:
        ytdlp_err = yt_fb_err

    # Never scrape login pages on explicit auth/bot rejections from YouTube
    low_err = ytdlp_err.lower()
    if _extract_youtube_id(url) and ("bot" in low_err or "sign in" in low_err or "login" in low_err):
        if IS_VERCEL:
            raise RuntimeError(
                "YouTube blocked Vercel's cloud IP with bot verification. "
                "Run SachDownload locally (python server.py) to download with your home IP."
            )
        raise RuntimeError("YouTube requires sign-in / anti-bot confirmation for this video.")

    html_res = _try_html_fallback(url, depth=depth)
    if html_res and html_res.get("formats"):
        return html_res

    if "private video" in low_err:
        raise RuntimeError("This video is private and cannot be downloaded.")
    if "video unavailable" in low_err:
        raise RuntimeError("This video is unavailable or has been removed.")
    if "403" in low_err or "forbidden" in low_err:
        if IS_VERCEL:
            raise RuntimeError(
                "This site blocks cloud server requests (403). Run locally via http://localhost:8000 for full access."
            )
        raise RuntimeError("The site refused access (403 Forbidden).")

    clean = ytdlp_err.split("\n")[0].replace("ERROR:", "").strip() if ytdlp_err else ""
    raise RuntimeError(clean or "No downloadable video stream found on this URL.")


def _pick_best_format(formats: list[dict[str, Any]], requested_id: Optional[str] = None) -> dict[str, Any]:
    if requested_id and requested_id != "best":
        for f in formats:
            if f["format_id"] == requested_id:
                return f
    for f in formats:
        if f["muxed"] and not f["is_hls"]:
            return f
    for f in formats:
        if f["has_video"]:
            return f
    return formats[0]


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
            speed = re.sub(r"\x1b\[[0-9;]*m", "", str(d.get("_speed_str", "") or "")).strip()
            eta = re.sub(r"\x1b\[[0-9;]*m", "", str(d.get("_eta_str", "") or "")).strip()
            tasks_progress[task_id].update({
                "status": "downloading",
                "percent": min(percent, 99.5),
                "percent_str": f"{min(percent, 99.5)}%",
                "speed": speed,
                "eta": eta,
            })
        elif status == "finished":
            tasks_progress[task_id].update({
                "status": "merging",
                "percent": 100.0,
                "percent_str": "100%",
                "speed": "",
                "eta": "Finalizing...",
            })
    return hook


def download_video_sync(
    url: str,
    task_id: Optional[str] = None,
    format_id: Optional[str] = None,
) -> tuple[str, str, str, Optional[str], Optional[str]]:
    if not task_id:
        task_id = str(uuid.uuid4())[:8]
    file_id = task_id

    tasks_progress[task_id] = {
        "status": "starting",
        "percent": 0.0,
        "percent_str": "0%",
        "speed": "",
        "eta": "",
        "filename": "",
        "file_id": file_id,
        "direct_url": None,
        "proxy_url": None,
        "error": None,
    }

    try:
        info = extract_video_info(url)
        formats = [_build_format_urls(f, info["title"], info["webpage_url"]) for f in info["formats"]]
        chosen = _pick_best_format(formats, format_id)

        if IS_VERCEL:
            tasks_progress[task_id].update({
                "status": "finished",
                "percent": 100.0,
                "percent_str": "100%",
                "filename": chosen["filename"],
                "file_id": file_id,
                "direct_url": chosen["direct_url"],
                "proxy_url": chosen["proxy_url"],
            })
            return file_id, "", chosen["filename"], chosen["direct_url"], chosen["proxy_url"]

        ffmpeg_exe = get_ffmpeg()
        out_template = os.path.join(DOWNLOADS_DIR, f"{file_id}_%(title).90s.%(ext)s")
        if format_id and format_id != "best":
            fmt_spec = f"{format_id}+bestaudio/{format_id}" if (chosen["needs_merge"] and ffmpeg_exe) else format_id
        elif ffmpeg_exe:
            fmt_spec = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
        else:
            fmt_spec = "best[ext=mp4]/best"

        ydl_opts: dict[str, Any] = {
            "outtmpl": out_template,
            "format": fmt_spec,
            "windowsfilenames": True,
            "noplaylist": True,
            "playlist_items": "1",
            "retries": 4,
            "fragment_retries": 4,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [make_progress_hook(task_id)],
        }
        node_path = shutil.which("node")
        if node_path:
            ydl_opts["js_runtimes"] = {"node": {"path": node_path}}
        if ffmpeg_exe:
            ydl_opts["ffmpeg_location"] = ffmpeg_exe
            ydl_opts["merge_output_format"] = "mp4"

        downloaded_via_ydl = False
        if info["extractor"] not in ("html-scraper", "vimeo-config", "dailymotion-meta", "x-fxtwitter"):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                downloaded_via_ydl = bool(glob.glob(os.path.join(DOWNLOADS_DIR, f"{file_id}_*")))
            except Exception:
                downloaded_via_ydl = False

        if not downloaded_via_ydl:
            # Download chosen stream URL directly (supports fallback/platform extractors)
            if chosen["is_hls"]:
                hls_opts = {
                    "outtmpl": os.path.join(DOWNLOADS_DIR, f"{file_id}_{sanitize_filename(info['title'])}.%(ext)s"),
                    "quiet": True,
                    "no_warnings": True,
                    "http_headers": chosen["http_headers"],
                    "progress_hooks": [make_progress_hook(task_id)],
                }
                if ffmpeg_exe:
                    hls_opts["ffmpeg_location"] = ffmpeg_exe
                with yt_dlp.YoutubeDL(hls_opts) as ydl:
                    ydl.download([chosen["url"]])
            else:
                out_path = os.path.join(DOWNLOADS_DIR, f"{file_id}_{chosen['filename']}")
                resp = http_request(
                    chosen["url"],
                    headers=chosen["http_headers"],
                    timeout=60,
                    stream=True,
                    require_public=False,
                )
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0) or 0)
                done = 0
                with open(out_path, "wb") as fp:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            fp.write(chunk)
                            done += len(chunk)
                            if total > 0:
                                pct = round((done / total) * 100, 1)
                                tasks_progress[task_id].update({
                                    "status": "downloading",
                                    "percent": min(pct, 99.5),
                                    "percent_str": f"{min(pct, 99.5)}%",
                                })
                resp.close()

        matches = glob.glob(os.path.join(DOWNLOADS_DIR, f"{file_id}_*"))
        if not matches:
            raise RuntimeError("Download completed but output file was not found.")
        saved_file = matches[0]
        display_name = os.path.basename(saved_file).replace(f"{file_id}_", "", 1)

        downloaded_files[file_id] = {"path": saved_file, "filename": display_name}
        tasks_progress[task_id].update({
            "status": "finished",
            "percent": 100.0,
            "percent_str": "100%",
            "filename": display_name,
            "file_id": file_id,
            "direct_url": None,
            "proxy_url": None,
        })
        return file_id, saved_file, display_name, None, None
    except Exception as e:
        msg = str(e)
        tasks_progress[task_id].update({"status": "error", "error": msg})
        raise


def _content_disposition(filename: str) -> str:
    ascii_name = re.sub(r'[^\x20-\x7e]', "_", filename).replace('"', "_").replace("\\", "_")
    encoded = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def _stream_hls_segments(segments_plan: dict[str, Any], headers: dict[str, str]):
    parts = ([segments_plan["init"]] if segments_plan["init"] else []) + segments_plan["segments"]

    def fetch_part(part: dict[str, Any]) -> bytes:
        h = dict(headers)
        if part.get("range"):
            s, e = part["range"]
            h["Range"] = f"bytes={s}-{e}"
        r = http_request(part["url"], headers=h, timeout=25, stream=False, require_public=True)
        if r.status_code >= 400:
            r.close()
            raise RuntimeError(f"Segment fetch failed ({r.status_code})")
        content = r.content
        r.close()
        return content

    pool = ThreadPoolExecutor(max_workers=4)
    try:
        futures = [pool.submit(fetch_part, part) for part in parts[:4]]
        idx = 4
        pos = 0
        while pos < len(parts):
            data = futures[pos].result()
            yield data
            if idx < len(parts):
                futures.append(pool.submit(fetch_part, parts[idx]))
                idx += 1
            pos += 1
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = None


app = FastAPI(title="SachDownload - Video Downloader")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def serve_root():
    index_path = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return HTMLResponse("<h1>SachDownload is running</h1>")


@app.get("/index.html")
async def serve_index():
    return await serve_root()


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


@app.post("/api/inspect")
@app.post("/inspect")
async def api_inspect(payload: DownloadRequest):
    url = payload.url.strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse(
            {"success": False, "error": "Please provide a valid URL starting with http:// or https://"},
            status_code=400,
        )
    try:
        info = await asyncio.to_thread(extract_video_info, url)
        formats = [_build_format_urls(f, info["title"], info["webpage_url"]) for f in info["formats"]]
        return {
            "success": True,
            "title": info["title"],
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "extractor": info["extractor"],
            "is_vercel": IS_VERCEL,
            "formats": formats[:15],
        }
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@app.get("/api/proxy")
@app.get("/proxy")
async def api_proxy(
    request: Request,
    url: str = Query(...),
    ref: str = Query(""),
    name: str = Query("video.mp4"),
):
    try:
        validate_public_url(url, require_public=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    headers: dict[str, str] = {}
    if ref and ref.startswith(("http://", "https://")):
        headers["Referer"] = ref
    else:
        parsed = urllib.parse.urlparse(url)
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"

    req_range = request.headers.get("range")
    if req_range and not _is_hls_playlist("", url):
        headers["Range"] = req_range

    try:
        up = await asyncio.to_thread(
            http_request, url, "GET", headers, 20, True, True, 5, True
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    if up.status_code >= 400:
        up.close()
        raise HTTPException(status_code=502, detail=f"Upstream site refused download ({up.status_code}).")

    ctype = (up.headers.get("content-type") or "").split(";")[0].strip().lower()
    final_url = str(up.url)
    clean_name = sanitize_filename(name)

    if _is_hls_playlist(ctype, final_url):
        playlist_text = _read_stream_text(up)
        up.close()
        try:
            parsed_hls = parse_m3u8(playlist_text, final_url)
            if parsed_hls["kind"] == "master":
                if not parsed_hls["variants"]:
                    raise HTTPException(status_code=422, detail="No playable streams in playlist.")
                best_var = parsed_hls["variants"][0]
                media_r = await asyncio.to_thread(
                    http_request, best_var["url"], "GET", headers, 20, False, True, 5, True
                )
                if media_r.status_code >= 400:
                    media_r.close()
                    raise HTTPException(status_code=502, detail="Failed to load HLS stream variant.")
                parsed_hls = parse_m3u8(media_r.text, str(media_r.url))
                media_r.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=422, detail=str(e))

        if parsed_hls.get("encrypted_method"):
            raise HTTPException(
                status_code=422,
                detail=f"Encrypted HLS ({parsed_hls['encrypted_method']}) requires running locally with ffmpeg.",
            )
        if not parsed_hls.get("endlist"):
            raise HTTPException(status_code=422, detail="Live streams cannot be saved as a single file.")
        if not parsed_hls.get("segments") or len(parsed_hls["segments"]) > 3500:
            raise HTTPException(status_code=422, detail="Stream has no segments or exceeds length limit.")

        ext = parsed_hls["ext"]
        if not clean_name.lower().endswith(f".{ext}"):
            clean_name = os.path.splitext(clean_name)[0] + f".{ext}"

        return StreamingResponse(
            _stream_hls_segments(parsed_hls, headers),
            media_type="video/mp4" if ext == "mp4" else "video/mp2t",
            headers={
                "Content-Disposition": _content_disposition(clean_name),
                "Cache-Control": "no-store",
            },
        )

    if ctype.startswith("text/html"):
        up.close()
        raise HTTPException(status_code=415, detail="Target URL returned an HTML webpage instead of video media.")

    if "." not in clean_name:
        clean_name += ".mp4"

    resp_headers = {
        "Content-Disposition": _content_disposition(clean_name),
        "Cache-Control": "no-store",
    }
    for h in ("content-length", "content-range", "accept-ranges"):
        val = up.headers.get(h)
        if val:
            resp_headers[h.title()] = val

    def iter_upstream():
        try:
            for chunk in up.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            up.close()

    return StreamingResponse(
        iter_upstream(),
        status_code=206 if up.status_code == 206 else 200,
        media_type=ctype or "application/octet-stream",
        headers=resp_headers,
    )


@app.post("/api/download/start")
@app.post("/download/start")
async def api_download_start(payload: DownloadRequest):
    url = payload.url.strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse(
            {"success": False, "error": "Please provide a valid URL starting with http:// or https://"},
            status_code=400,
        )

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
        "proxy_url": None,
        "error": None,
    }

    if IS_VERCEL:
        try:
            file_id, _, filename, direct_url, proxy_url = await asyncio.to_thread(
                download_video_sync, url, task_id, payload.format_id
            )
            return {
                "success": True,
                "task_id": task_id,
                "direct_url": direct_url,
                "proxy_url": proxy_url,
                "filename": filename,
            }
        except Exception as e:
            return JSONResponse({"success": False, "task_id": task_id, "error": str(e)}, status_code=500)

    asyncio.create_task(asyncio.to_thread(download_video_sync, url, task_id, payload.format_id))
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
    if not url.startswith(("http://", "https://")):
        return JSONResponse(
            {"success": False, "error": "Please provide a valid URL starting with http:// or https://"},
            status_code=400,
        )
    try:
        file_id, _, filename, direct_url, proxy_url = await asyncio.to_thread(
            download_video_sync, url, None, payload.format_id
        )
        return {
            "success": True,
            "file_id": file_id,
            "filename": filename,
            "direct_url": direct_url,
            "proxy_url": proxy_url,
        }
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/file/{file_id}")
@app.get("/file/{file_id}")
async def get_downloaded_file(file_id: str):
    info = downloaded_files.get(file_id)
    if not info:
        matches = glob.glob(os.path.join(DOWNLOADS_DIR, f"{file_id}_*"))
        if matches:
            filepath = matches[0]
            filename = os.path.basename(filepath).replace(f"{file_id}_", "", 1)
            info = {"path": filepath, "filename": filename}
        else:
            raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        info["path"],
        filename=info["filename"],
        media_type="application/octet-stream",
    )


@app.api_route("/download", methods=["GET", "POST"])
async def direct_download(request: Request):
    if request.method == "POST":
        form_data = await request.form()
        url = str(form_data.get("url", "")).strip()
    else:
        url = str(request.query_params.get("url", "")).strip()

    if not url:
        return HTMLResponse("<p>Error: No URL provided. <a href='/'>Go back</a></p>", status_code=400)

    try:
        file_id, filepath, filename, direct_url, proxy_url = await asyncio.to_thread(download_video_sync, url)
        if proxy_url:
            return RedirectResponse(proxy_url)
        if direct_url:
            return RedirectResponse(direct_url)
        return FileResponse(filepath, filename=filename, media_type="application/octet-stream")
    except Exception as e:
        return HTMLResponse(f"<p>Error downloading video: {e}</p><p><a href='/'>Go back</a></p>", status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
