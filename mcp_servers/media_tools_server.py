"""media-tools MCP Server —— 识图 / STT / TTS 三个多模态工具。

--------------------------------------------------------------------------
Tool 规范（本 server 的对外契约）
--------------------------------------------------------------------------
工具名            用途              输入                                    输出
================  ================  ======================================  ==================================
``vision_describe``  图片理解        image_path, prompt?, max_chars?          {ok, model, text, elapsed_ms}
``stt_transcribe``   语音转文本      audio_path, language?, prompt?           {ok, model, text, language, ...}
``tts_synthesize``   文本转语音      text, out_path?, voice?, speed?, ...     {ok, model, voice, path, bytes}
``media_probe``      媒体嗅探        path                                     {ok, suffix, bytes, mime, ffprobe}
================  ================  ======================================  ==================================

**错误约定**：工具内部永不抛异常，一律返回 ``{"ok": false, "error": "..."}``。
原因是 MCP 的 ``isError`` 会中断模型的工具调用循环，而"这个图片读不出来"
属于**业务结果**而不是协议故障 —— 让模型看到错误并自行决定如何回复，
比让整轮对话崩掉更合理。

--------------------------------------------------------------------------
配置（全部走环境变量，符合 MCP server 的进程模型）
--------------------------------------------------------------------------
VISION_BASE_URL / VISION_API_KEY / VISION_MODEL
STT_BASE_URL    / STT_API_KEY    / STT_MODEL
TTS_BASE_URL    / TTS_API_KEY    / TTS_MODEL / TTS_VOICE
FFMPEG_BIN

三个模态**各自独立配置端点**，所以你可以识图用 A 家、语音用 B 家。
这就是"多模态路由"落地的地方。

启动：``python -m mcp_servers.media_tools_server``
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

SERVER_NAME = "media-tools"
mcp = FastMCP(SERVER_NAME)


# --------------------------------------------------------------------------- #
# 配置读取
# --------------------------------------------------------------------------- #


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _default_prompt() -> str:
    return (
        "请客观描述这张图片的内容，然后提取其中所有可见文字（OCR）。"
        "如果图片里有图表，说明它表达的数据结论。不要臆测图片之外的信息。"
    )


def _client(base_url: str, api_key: str) -> Any:
    """按需构造 OpenAI 兼容异步客户端。

    ``base_url`` 为空时用官方默认端点 —— 这样只填 API Key 也能跑。
    """
    from openai import AsyncOpenAI

    kwargs: dict[str, Any] = {"api_key": api_key or "not-needed"}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _fail(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _check_file(path: str, kind: str) -> Path | None:
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    if p.stat().st_size == 0:
        return None
    return p


def _guess_mime(path: Path, fallback: str) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or fallback


def _ffprobe_duration(path: Path) -> float | None:
    """有 ffprobe 就顺便取时长，没有就算了 —— 不是关键路径。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return float(out.stdout.strip()) if out.stdout.strip() else None
    except (subprocess.SubprocessError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Tool 1：识图
# --------------------------------------------------------------------------- #


@mcp.tool()
async def vision_describe(
    image_path: str,
    prompt: str = "",
    max_chars: int = 1500,
) -> str:
    """【识图】用多模态模型描述本地图片内容并提取其中的文字。

    Args:
        image_path: 本地图片绝对路径（png/jpg/webp/gif）。
        prompt: 自定义提问；留空则使用"客观描述 + OCR + 图表结论"的默认指令。
        max_chars: 返回文本的最大长度，超出截断，避免把上下文撑爆。

    Returns:
        JSON 字符串：{ok, model, text, elapsed_ms}
    """
    started = time.perf_counter()
    path = _check_file(image_path, "image")
    if path is None:
        return _fail(f"图片不存在或为空: {image_path}")

    model = _env("VISION_MODEL", "gpt-4o-mini")
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        mime = _guess_mime(path, "image/jpeg")
        client = _client(_env("VISION_BASE_URL"), _env("VISION_API_KEY"))

        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt.strip() or _default_prompt()},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{data}"},
                        },
                    ],
                }
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "…"
        return _ok(
            {
                "model": model,
                "text": text,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(f"识图失败（{model}）: {exc}")


# --------------------------------------------------------------------------- #
# Tool 2：语音转文本
# --------------------------------------------------------------------------- #


@mcp.tool()
async def stt_transcribe(
    audio_path: str,
    language: str = "zh",
    prompt: str = "",
) -> str:
    """【语音转文本】把本地音频文件转写成文字。

    Args:
        audio_path: 本地音频路径（ogg/mp3/m4a/wav/webm，Telegram 语音是 ogg/opus）。
        language: ISO-639-1 语言代码，默认 zh；传空串让模型自动判断。
        prompt: 领域提示词，可显著提升专有名词识别率。

    Returns:
        JSON 字符串：{ok, model, text, language, duration_sec, elapsed_ms}
    """
    started = time.perf_counter()
    path = _check_file(audio_path, "audio")
    if path is None:
        return _fail(f"音频不存在或为空: {audio_path}")

    model = _env("STT_MODEL", "whisper-1")
    try:
        client = _client(_env("STT_BASE_URL"), _env("STT_API_KEY"))
        kwargs: dict[str, Any] = {"model": model, "file": (path.name, path.read_bytes())}
        if language:
            kwargs["language"] = language
        if prompt:
            kwargs["prompt"] = prompt

        response = await client.audio.transcriptions.create(**kwargs)
        text = (getattr(response, "text", "") or "").strip()
        return _ok(
            {
                "model": model,
                "text": text,
                "language": language or "auto",
                "duration_sec": _ffprobe_duration(path),
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(f"语音转文本失败（{model}）: {exc}")


# --------------------------------------------------------------------------- #
# Tool 3：文本转语音
# --------------------------------------------------------------------------- #


@mcp.tool()
async def tts_synthesize(
    text: str,
    out_path: str = "",
    voice: str = "",
    speed: float = 1.0,
    instructions: str = "",
    fmt: str = "mp3",
) -> str:
    """【文本转语音】把文字合成为语音文件，返回落盘路径。

    Args:
        text: 要朗读的文本（建议先剥掉 Markdown 标记）。
        out_path: 输出文件路径；留空则自动生成到临时目录。
        voice: 音色名，留空则用 TTS_VOICE 环境变量。
        speed: 语速倍率 0.25~4.0，由情绪系统推导（唤醒度越高越快）。
        instructions: 风格指令（仅部分模型支持，如 gpt-4o-mini-tts）。
        fmt: 输出格式 mp3 | opus | aac | wav。

    Returns:
        JSON 字符串：{ok, model, voice, path, bytes, elapsed_ms}
    """
    started = time.perf_counter()
    if not text.strip():
        return _fail("文本为空，无法合成")

    model = _env("TTS_MODEL", "gpt-4o-mini-tts")
    voice = voice or _env("TTS_VOICE", "alloy")
    try:
        if out_path:
            target = Path(out_path).expanduser()
        else:
            stamp = int(time.time() * 1000)
            target = Path(os.environ.get("TEMP", "/tmp")) / f"tts_{stamp}.{fmt}"
        target.parent.mkdir(parents=True, exist_ok=True)

        client = _client(_env("TTS_BASE_URL"), _env("TTS_API_KEY"))
        kwargs: dict[str, Any] = {
            "model": model,
            "voice": voice,
            "input": text,
            "speed": max(0.25, min(4.0, float(speed))),
            "response_format": fmt,
        }
        if instructions:
            kwargs["instructions"] = instructions

        response = await client.audio.speech.create(**kwargs)
        audio = getattr(response, "content", None)
        if audio is None:
            audio = await response.read()  # 某些 SDK 版本是流式对象
        target.write_bytes(audio)

        return _ok(
            {
                "model": model,
                "voice": voice,
                "path": str(target),
                "bytes": len(audio),
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(f"语音合成失败（{model}）: {exc}")


# --------------------------------------------------------------------------- #
# Tool 4：媒体嗅探（辅助工具）
# --------------------------------------------------------------------------- #


@mcp.tool()
async def media_probe(path: str) -> str:
    """【媒体嗅探】返回本地文件的类型、大小与时长，用于路由前的预判。

    Args:
        path: 本地文件路径。

    Returns:
        JSON 字符串：{ok, path, suffix, bytes, mime, duration_sec}
    """
    p = _check_file(path, "media")
    if p is None:
        return _fail(f"文件不存在或为空: {path}")
    return _ok(
        {
            "path": str(p),
            "suffix": p.suffix.lower(),
            "bytes": p.stat().st_size,
            "mime": _guess_mime(p, "application/octet-stream"),
            "duration_sec": _ffprobe_duration(p),
        }
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main() -> None:
    # stdio 传输：被宿主进程拉起，不要往 stdout 打任何非协议内容
    mcp.run()


if __name__ == "__main__":
    main()
