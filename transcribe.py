import argparse
import base64
import hashlib
import json
import os
import re
import sys
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from replay import APP_DATA, PROGRESS_INTERVAL_SECONDS, ReplayError, validate_replay, with_browser


OUTPUT_DIR = Path(__file__).resolve().parent / "文字稿"


def timestamp(seconds):
    minutes, sec = divmod(max(0, int(seconds)), 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}:{sec:02d}"


def replay_name(url):
    token = parse_qs(urlsplit(url).query).get("token", [""])[0]
    identifier = hashlib.sha256(url.encode()).hexdigest()[:8]
    # The URL can include a display date. Use it only in the output filename.
    try:
        payload = token.split(".")[1]
        details = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        when = datetime.fromisoformat(details["recordTime"])
    except (IndexError, KeyError, ValueError, TypeError):
        return f"课程回放_{identifier}"
    return f"课程回放_{when:%Y-%m-%d_%H%M}_{identifier}"


@contextmanager
def cuda_runtime():
    nvidia = APP_DATA / "gpu-runtime" / "nvidia"
    folders = [nvidia / name / "bin" for name in ("cublas", "cudnn")]
    folders = [folder for folder in folders if folder.is_dir()]
    if not folders:
        yield
        return

    original_path = os.environ.get("PATH")
    with ExitStack() as libraries:
        for folder in folders:
            libraries.enter_context(os.add_dll_directory(str(folder)))
        # NVIDIA's libraries also look for their dependencies through PATH.
        path_entries = [str(folder) for folder in folders]
        if original_path is not None:
            path_entries.append(original_path)
        os.environ["PATH"] = os.pathsep.join(path_entries)
        try:
            yield
        finally:
            if original_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = original_path


def run_whisper(audio, model_size, language, device):
    from faster_whisper import WhisperModel
    from opencc import OpenCC

    models = APP_DATA / "models"
    installed = models / f"whisper-{model_size}"
    model_source = str(installed) if installed.is_dir() else model_size
    hardware = "NVIDIA GPU（CUDA）" if device == "cuda" else "CPU"
    print(f"正在使用 {hardware} 加载 Whisper {model_size} 模型……", flush=True)
    model = WhisperModel(
        model_source, device=device, compute_type="float16" if device == "cuda" else "int8",
        download_root=str(models),
        cpu_threads=min(os.cpu_count() or 4, 8),
    )

    convert = OpenCC("t2s").convert
    result = []
    print(f"正在使用 {hardware} 本地转写……", flush=True)
    segments, info = model.transcribe(
        str(audio), language=None if language == "auto" else language, vad_filter=True,
    )
    next_report = PROGRESS_INTERVAL_SECONDS
    for segment in segments:
        text = segment.text.strip()
        if text:
            if info.language == "zh":
                text = convert(text)
            result.append(f"[{timestamp(segment.start)}] {text}")
        if segment.end >= next_report:
            print(f"已转写到 {timestamp(segment.end)} / {timestamp(info.duration)}", flush=True)
            next_report = (int(segment.end) // PROGRESS_INTERVAL_SECONDS + 1) * PROGRESS_INTERVAL_SECONDS
    if not result:
        raise ReplayError("没有识别到可转写的语音。")
    return "\n\n".join(result) + "\n"


def transcribe(audio, model_size, language, device):
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
    from av.error import FFmpegError

    devices = (device,)
    if device == "auto":
        from ctranslate2 import get_cuda_device_count
        try:
            devices = ("cuda", "cpu") if get_cuda_device_count() else ("cpu",)
        except (OSError, RuntimeError):
            devices = ("cpu",)

    for selected in devices:
        try:
            with cuda_runtime() if selected == "cuda" else nullcontext():
                return run_whisper(audio, model_size, language, selected)
        except FFmpegError:
            raise ReplayError("转写失败。请确认输入是可播放的音视频。") from None
        except ValueError:
            raise ReplayError("无法加载指定的模型或识别语言，请检查转写设置。") from None
        except (OSError, RuntimeError):
            if selected == "cuda":
                if device == "auto":
                    print("GPU 转写无法继续，正在用 CPU 重试……", flush=True)
                    continue
                raise ReplayError("GPU 转写失败，请检查 CUDA 运行库、驱动和可用显存。") from None
            raise ReplayError("CPU 转写失败，请检查模型文件和可用内存。") from None


def write_transcript(text, name, output):
    if output:
        path = output
        if path.suffix.lower() != ".txt":
            raise ReplayError("输出文件请使用 .txt 扩展名。")
    else:
        path = OUTPUT_DIR / f"{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    for number in range(1, 10000):
        target = path if number == 1 else path.with_name(f"{path.stem}_{number}{path.suffix}")
        try:
            with target.open("x", encoding="utf-8") as file:
                file.write(text)
            return target.resolve()
        except FileExistsError:
            if output:
                raise ReplayError("指定的输出文件已存在，请换一个文件名。") from None
    raise ReplayError("同名文字稿过多，请指定一个输出文件。")


def main():
    parser = argparse.ArgumentParser(description="把北大教学网课程回放或本地音视频转成带时间戳的文字稿。")
    parser.add_argument("source", nargs="?", help="教学网回放网址或本地音视频路径。省略时提示粘贴。")
    parser.add_argument("--login", action="store_true", help="只在专用 Edge 窗口登录教学网。")
    parser.add_argument("--model", choices=["tiny", "base", "small", "medium", "large-v3"], default="small")
    parser.add_argument("--language", default="zh", help="语音语言，默认 zh（中文）。可用 en 或 auto 等。")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto",
                        help="默认优先使用 NVIDIA GPU，必要时转为 CPU。")
    parser.add_argument("--output", type=Path, help="指定输出 .txt 文件。")
    options = parser.parse_args()
    if options.login:
        if options.source:
            parser.error("--login 无需传入网址。")
        with_browser()
        print("教学网登录会话已保存到本机。")
        return

    if options.output:
        if options.output.suffix.lower() != ".txt":
            raise ReplayError("输出文件请使用 .txt 扩展名。")
        if options.output.exists():
            raise ReplayError("指定的输出文件已存在，请换一个文件名。")
    interactive = options.source is None and sys.stdin.isatty()
    source = options.source
    if source is None:
        if interactive:
            print("请粘贴教学网回放链接，或拖入已下载的音视频文件，再按回车。", flush=True)
        source = sys.stdin.readline()
    source = source.strip().strip('"')
    if not source:
        raise ReplayError("没有收到回放网址或本地文件。")
    markdown_link = re.fullmatch(r"\[[^\]]+\]\((https://[^)]+)\)", source)
    if markdown_link:
        source = markdown_link[1]
    if source.startswith(("https://", "http://")):
        validate_replay(source)
        with TemporaryDirectory(prefix="pku-replay-") as folder:
            audio = with_browser(source, Path(folder))
            result = transcribe(audio, options.model, options.language, options.device)
        name = replay_name(source)
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise ReplayError("找不到这个音视频文件。")
        name = path.stem
        result = transcribe(path, options.model, options.language, options.device)

    target = write_transcript(result, name, options.output)
    print(f"文字稿已保存到 {target}", flush=True)
    if interactive:
        try:
            os.startfile(target)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except ReplayError as error:
        print(f"处理失败。{error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        sys.exit(130)
    except OSError:
        print("处理失败。请检查磁盘空间、运行环境和文件访问权限。", file=sys.stderr)
        sys.exit(1)
