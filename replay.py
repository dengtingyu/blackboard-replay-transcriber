import base64
import ctypes
import io
import json
import os
import re
import time
import wave
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin, urlsplit

import av
import certifi
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import sync_playwright


PKU_HOME = "https://course.pku.edu.cn/"
REPLAY_PATH = "/webapps/bb-streammedia-hqy-BBLEARN/playVideo.action"
APP_DATA = Path(os.environ["LOCALAPPDATA"]) / "PKUReplayTranscriber"
SESSION_FILE = APP_DATA / "session.dpapi"
SAMPLE_RATE = 16000
PROGRESS_INTERVAL_SECONDS = 10 * 60


class ReplayError(Exception):
    pass


class DataBlob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.c_void_p)]


def dpapi(data, protect):
    # Windows ties the encrypted cookie backup to the current user account.
    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.addressof(buffer))
    result = DataBlob()
    crypt = ctypes.windll.crypt32
    operation = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.data, result.length)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(result.data))


def is_pku(host):
    return host == "pku.edu.cn" or host.endswith(".pku.edu.cn")


def restore_session(context):
    try:
        cookies = json.loads(dpapi(SESSION_FILE.read_bytes(), protect=False))
        # Do not replace a fresh cookie already stored by Edge.
        existing = {(c["name"], c["domain"], c["path"]) for c in context.cookies()}
        context.add_cookies([
            c for c in cookies
            if is_pku(c["domain"].lstrip("."))
            and (c["expires"] < 0 or c["expires"] > time.time())
            and (c["name"], c["domain"], c["path"]) not in existing
        ])
    except FileNotFoundError:
        pass
    except (OSError, ValueError, BrowserError):
        print("之前的登录备份无法读取，必要时请在教学网重新登录。", flush=True)


def save_session(context):
    cookies = [c for c in context.cookies() if is_pku(c["domain"].lstrip("."))]
    if cookies:
        encoded = dpapi(json.dumps(cookies).encode(), protect=True)
        with NamedTemporaryFile(dir=APP_DATA, delete=False) as temp:
            temp_path = Path(temp.name)
            temp.write(encoded)
        try:
            temp_path.replace(SESSION_FILE)
        finally:
            temp_path.unlink(missing_ok=True)


def validate_replay(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ReplayError("请粘贴北大教学网里打开的完整回放网址。") from None
    if (parts.scheme != "https" or parts.netloc.lower() != "course.pku.edu.cn"
            or parts.path != REPLAY_PATH or not parts.query):
        raise ReplayError("请粘贴北大教学网里打开的完整回放网址。")


def login_screen(page):
    url = urlsplit(page.url)
    host = url.hostname or ""
    if host.startswith(("iaaa.", "sso.", "id.")) and is_pku(host):
        return True
    if host != "course.pku.edu.cn":
        return False
    path = url.path.lower()
    return (
        path.startswith(("/webapps/login", "/webapps/bb-sso-", "/webapps/blackboard/password"))
        or page.locator("#login input[type=password], #loginForm input[type=password], #user_id").count() > 0
    )


def navigate(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=35000)


def open_with_login(page, url):
    navigate(page, url)
    if not login_screen(page):
        return
    print("请在弹出的 Edge 窗口完成教学网登录，登录后工具会自动继续。", flush=True)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        if urlsplit(page.url).hostname != "course.pku.edu.cn" or login_screen(page):
            continue
        if url != PKU_HOME and page.url != url:
            navigate(page, url)
            if login_screen(page):
                print("教学网仍要求登录，请检查登录状态后重试。", flush=True)
                raise ReplayError("登录会话无法访问这条回放。")
        return
    raise ReplayError("等待登录超时，请重新运行工具。")


@dataclass
class Media:
    url: str
    priority: int
    request: object = None
    response: object = None


def media_priority(url, content_type="", resource_type=""):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return 0
    path = parts.path.lower()
    mime = content_type.partition(";")[0].strip().lower()
    if path.endswith((".m3u8", ".mpd")) or mime in {
        "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml",
        "audio/mpegurl", "audio/x-mpegurl",
    }:
        return 30
    if path.endswith((".m4s", ".ts", ".cmfv", ".cmfa")) or mime == "video/mp2t":
        return 0
    if path.endswith((".m4a", ".aac", ".mp3", ".wav", ".opus", ".ogg")) or mime.startswith("audio/"):
        return 20
    if path.endswith((".mp4", ".webm", ".flv", ".mov")) or mime.startswith("video/"):
        return 10
    return 5 if resource_type == "media" and mime == "application/octet-stream" else 0


PLAYER_STATUS = r"""() => [...document.querySelectorAll('video, audio')].map(el => {
    if (el.paused && !el.ended && !el.mediaKeys && (el.src || el.querySelector('source'))) {
        el.muted = true;
        el.play().catch(() => {});
    }
    return {
        src: el.currentSrc || el.src,
        duration: Number.isFinite(el.duration) ? el.duration : null,
        protected: Boolean(el.mediaKeys || el.dataset.replayEncrypted)
    };
})"""


def read_players(page):
    players = []
    for frame in page.frames:
        try:
            players.extend(frame.evaluate(PLAYER_STATUS))
        except BrowserError:
            # A player iframe may navigate while it initializes.
            continue
    return players


def pause_players(pages):
    for page in pages:
        for frame in page.frames:
            try:
                frame.evaluate("() => document.querySelectorAll('video, audio').forEach(el => el.pause())")
            except BrowserError:
                # A frame may close or navigate while the media URL is being picked up.
                continue


def configure_media(context, media):
    url = urlsplit(media.url)
    options = {
        "protocol_whitelist": "http,https,tcp,tls",
        "tls_verify": "1",
        "ca_file": certifi.where(),
    }
    sent = set()
    if media.request:
        for name in ("user-agent", "referer"):
            value = media.request.header_value(name)
            if value and "\r" not in value and "\n" not in value:
                if name == "referer":
                    value = urlsplit(value)._replace(query="", fragment="").geturl()
                options[name.replace("-", "_")] = value
        origin = media.request.header_value("origin")
        if origin and "\r" not in origin and "\n" not in origin:
            options["headers"] = f"Origin: {origin}\r\n"
        sent = {pair.strip() for pair in (media.request.header_value("cookie") or "").split(";")}
    if sent and url.scheme == "https":
        cookies = [
            f'{c["name"]}={c["value"]}; path={c["path"]}; domain={url.hostname};'
            for c in context.cookies([media.url])
            if f'{c["name"]}={c["value"]}' in sent and not c.get("partitionKey")
            and not any(ch in c["value"] + c["path"] for ch in ";\r\n")
        ]
        if cookies:
            options["cookies"] = "\n".join(cookies)
    return options


HLS_URI = re.compile(r'(?<=[,:])URI="([^"]+)"')


def absolute_media_url(base, uri):
    address = urljoin(base, uri)
    parts = urlsplit(address)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ReplayError("回放包含播放器专用的地址，无法直接提取。")
    return address


def media_input(media, responses):
    """Use keys returned to the authenticated player for its AES-128 HLS playlist."""
    if media.priority != 30 or media.response is None:
        return media.url, {}
    text = media.response.text().lstrip("\ufeff")
    if not text.startswith("#EXTM3U") or "#EXT-X-KEY:" not in text:
        return media.url, {}

    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY:"):
            method = re.search(r"[:,]METHOD=([^,]+)", line)
            key_format = re.search(r'[:,]KEYFORMAT="([^"]+)"', line)
            if method and method[1] == "NONE":
                lines.append(line)
                continue
            uri = HLS_URI.search(line)
            if (not method or method[1] != "AES-128" or not uri
                    or (key_format and key_format[1] != "identity")):
                raise ReplayError("播放器使用的媒体保护格式无法直接提取。")
            response = responses.get(absolute_media_url(media.url, uri[1]))
            if response is None:
                # Keep the player running until its normal key request completes.
                return None
            key = response.body()
            if len(key) != 16:
                raise ReplayError("播放器未收到有效的 HLS 播放密钥。")
            encoded = base64.b64encode(key).decode("ascii")
            line = f'{line[:uri.start(1)]}data:application/octet-stream;base64,{encoded}{line[uri.end(1):]}'
        elif line.startswith("#"):
            line = HLS_URI.sub(
                lambda match: f'URI="{absolute_media_url(media.url, match[1])}"', line,
            )
        elif line:
            line = absolute_media_url(media.url, line)
        lines.append(line)

    # Keep the authorized playback data in memory. Only the reconstructed
    # playlist can use a data URI, and only for the exact key above.
    playlist = io.BytesIO(("\n".join(lines) + "\n").encode("utf-8"))
    return playlist, {
        "protocol_whitelist": "http,https,tcp,tls,crypto,data",
        "allowed_extensions": "ALL",
        "seg_max_retry": "3",
    }


def extract_audio(media_source, output, options, expected_duration):
    # Decode one audio track and discard any video data as it streams in.
    hls_format = "hls" if isinstance(media_source, io.BytesIO) else None
    with av.open(media_source, format=hls_format, options=options, timeout=(18, 18)) as source:
        streams = source.streams.audio
        if not streams:
            raise ReplayError("这个视频流没有音轨。")
        stream = streams[0]
        if stream.duration and stream.time_base:
            expected_duration = max(expected_duration or 0, float(stream.duration * stream.time_base))
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        frames_written = 0
        def write_frames(output_file, frames):
            for frame in frames:
                output_file.writeframesraw(frame.to_ndarray().tobytes())
            return sum(frame.samples for frame in frames)

        with wave.open(str(output), "wb") as audio:
            audio.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            next_report = PROGRESS_INTERVAL_SECONDS
            for frame in source.decode(stream):
                frames_written += write_frames(audio, resampler.resample(frame))
                if frames_written >= (next_report * SAMPLE_RATE):
                    print(f"已读取时长约 {next_report // 60} 分钟的音频。", flush=True)
                    next_report += PROGRESS_INTERVAL_SECONDS
            frames_written += write_frames(audio, resampler.resample(None))
    seconds = frames_written / SAMPLE_RATE
    if seconds < 1 or (expected_duration and seconds < expected_duration * 0.98 - 2):
        raise ReplayError("读到的音频不完整。")


def find_audio(context, page, url, temp):
    candidates = {}
    responses = {}
    downloads = []
    pages = [page]

    def observe(response):
        if not response.ok:
            return
        responses[response.url] = response
        mime = response.headers.get("content-type", "")
        priority = media_priority(response.url, mime, response.request.resource_type)
        if priority:
            candidates[response.url] = Media(response.url, priority, response.request, response)

    def on_download(download):
        downloads.append(download)

    def on_page(new_page):
        pages.append(new_page)
        new_page.on("download", on_download)

    page.on("download", on_download)
    context.on("page", on_page)
    context.on("response", observe)
    context.add_init_script(
        "document.addEventListener('encrypted', e => { e.target.dataset.replayEncrypted = '1'; }, true);"
    )
    open_with_login(page, url)
    save_session(context)
    print("正在查找回放音轨。若播放器要求手动开始，点一下播放键即可。", flush=True)
    deadline = time.monotonic() + 55
    first_media_at = None
    attempted = set()
    waiting_for_key = False
    last_failure = None
    output = temp / "audio.wav"
    while time.monotonic() < deadline:
        page.wait_for_timeout(800)
        if downloads:
            destination = temp / "official-download"
            downloads[0].save_as(destination)
            print("使用教学网页面下载的文件。", flush=True)
            return destination
        if len(attempted) >= 6:
            continue
        players = [player for browser_page in pages if not browser_page.is_closed()
                   for player in read_players(browser_page)]
        if any(player["protected"] for player in players):
            raise ReplayError("播放器报告了媒体保护。若网站提供官方下载，可下载后使用本地文件转写。")
        active_sources = {player["src"] for player in players if player["src"]}
        durations = {player["src"]: player["duration"] for player in players if player["duration"]}
        for src in active_sources:
            priority = media_priority(src)
            if priority:
                candidates.setdefault(src, Media(src, priority))
        if not candidates:
            continue
        if first_media_at is None:
            first_media_at = time.monotonic()
        if time.monotonic() - first_media_at < 4:
            continue
        ordered = sorted((media for media in candidates.values() if media.url not in attempted),
                         key=lambda media: media.priority + 25 * (media.url in active_sources),
                         reverse=True)
        if not ordered:
            continue
        for media in ordered:
            try:
                prepared = media_input(media, responses)
                if prepared is None:
                    waiting_for_key = True
                    continue
                source, input_options = prepared
                options = configure_media(context, media)
                options.update(input_options)
                attempted.add(media.url)
                pause_players([browser_page for browser_page in pages if not browser_page.is_closed()])
                print("正在从回放地址直接读取完整音轨，网页可保持暂停。", flush=True)
                expected = durations.get(media.url) or max(durations.values(), default=None)
                extract_audio(source, output, options, expected)
                return output
            except ReplayError as error:
                last_failure = str(error)
                attempted.add(media.url)
                output.unlink(missing_ok=True)
            except (av.error.FFmpegError, BrowserError):
                # Decoder and browser errors can contain signed URLs.
                last_failure = "播放器的媒体地址读取失败。"
                attempted.add(media.url)
                output.unlink(missing_ok=True)
            if len(attempted) >= 6:
                break
    if last_failure:
        raise ReplayError(last_failure)
    if waiting_for_key:
        raise ReplayError("播放器还未完成音轨授权。请确认网页能正常开始播放后重试。")
    raise ReplayError("没有读到完整的回放音频。请确认链接在教学网可播放；若能下载视频，可用本地文件转写。")


def with_browser(url=None, temp=None):
    with sync_playwright() as playwright:
        APP_DATA.mkdir(parents=True, exist_ok=True)
        try:
            context = playwright.chromium.launch_persistent_context(
                str(APP_DATA / "edge-profile"), channel="msedge", headless=False,
                args=["--disable-save-password-bubble", "--disable-features=PasswordManagerOnboarding"],
                accept_downloads=True,
            )
        except BrowserError:
            raise ReplayError("无法打开专用 Edge 窗口。请确认已安装 Microsoft Edge，且没有另一份转写工具正在使用。") from None
        try:
            restore_session(context)
            page = context.pages[0] if context.pages else context.new_page()
            if url is None:
                open_with_login(page, PKU_HOME)
                save_session(context)
                return None
            return find_audio(context, page, url, temp)
        except BrowserError:
            raise ReplayError("回放页面没能打开或浏览器已关闭。请检查教学网、网络和链接后重试。") from None
        finally:
            context.close()
