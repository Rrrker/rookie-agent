"""多模态路由 —— 把"附件类型"映射到"模型能力"。

--------------------------------------------------------------------------
为什么需要一个独立的路由模块
--------------------------------------------------------------------------
如果不做路由，多模态逻辑会渗透到 orchestrator 里变成一串 ``if 图片 / elif 语音``，
于是"加一种模态"就要改主流程。

这个模块把决策收敛成一张**声明式路由表**，并对外只暴露三个动词
（``describe_image`` / ``transcribe`` / ``synthesize``），
主流程完全不需要知道背后是哪个厂商的哪个模型。

--------------------------------------------------------------------------
路由表
--------------------------------------------------------------------------
附件类型        路由目标      工具（MCP）                    模型端点
==============  =============  ============================  ==============
IMAGE           视觉理解       media_tools/vision_describe   VISION_*
VOICE / AUDIO   语音转文本     media_tools/stt_transcribe    STT_*
（输出侧）      文本转语音     media_tools/tts_synthesize    TTS_*
DOCUMENT        不路由         落盘交给主脑按需读取            —
==============  =============  ============================  ==============

--------------------------------------------------------------------------
Telegram 的一个硬约束
--------------------------------------------------------------------------
``sendVoice`` 只接受 **OGG/Opus**；而绝大多数 TTS 直接产出 mp3。
所以 ``synthesize()`` 内部做一次 ffmpeg 转码；如果机器上没有 ffmpeg，
就降级返回 mp3 让渠道走 ``sendAudio`` —— 功能不中断，只是气泡样式不同。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.config import MediaConfig
from app.contracts import MediaKind, MediaRef, ToolPort

logger = logging.getLogger(__name__)

# 附件类型 → (路由目标, MCP 限定工具名)
_ROUTE_TABLE: dict[MediaKind, tuple[str, str]] = {
    MediaKind.IMAGE: ("vision", "media_tools__vision_describe"),
    MediaKind.VOICE: ("stt", "media_tools__stt_transcribe"),
    MediaKind.AUDIO: ("stt", "media_tools__stt_transcribe"),
    MediaKind.DOCUMENT: ("local", ""),
}

_MD_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_MD_INLINE_RE = re.compile(r"[*_`~>#]+")
_URL_RE = re.compile(r"https?://\S+")


@dataclass(slots=True)
class RouteDecision:
    """一次路由决策，打印出来就能定位问题。"""

    kind: MediaKind
    target: str          # vision | stt | local
    tool: str
    reason: str


class MediaRouter:
    """实现 :class:`app.contracts.MediaPort`。

    依赖 :class:`app.contracts.ToolPort` 而不是直接依赖 McpHub ——
    单测时塞一个假 ToolPort 就能验证路由逻辑。
    """

    def __init__(self, cfg: MediaConfig, tools: ToolPort) -> None:
        self.cfg = cfg
        self._tools = tools
        self._ffmpeg = shutil.which(cfg.ffmpeg_bin) or shutil.which("ffmpeg")
        cfg.media_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 路由决策
    # ------------------------------------------------------------------ #

    def route_of(self, kind: MediaKind) -> str:
        return _ROUTE_TABLE.get(kind, ("local", ""))[0]

    def decide(self, ref: MediaRef) -> RouteDecision:
        target, tool = _ROUTE_TABLE.get(ref.kind, ("local", ""))
        reason = {
            "vision": "图片 → 多模态理解模型",
            "stt": "音频 → 语音转文本模型",
            "local": "文档不做模型路由，落盘后由主脑按需读取",
        }.get(target, "未知类型")
        return RouteDecision(kind=ref.kind, target=target, tool=tool, reason=reason)

    # ------------------------------------------------------------------ #
    # 入站：附件 → 文本
    # ------------------------------------------------------------------ #

    async def describe_image(self, ref: MediaRef, *, prompt: str | None = None) -> str:
        args: dict[str, Any] = {"image_path": str(ref.path)}
        if prompt:
            args["prompt"] = prompt
        if ref.caption:
            # 把用户配文作为问题的一部分，识图更有针对性
            args["prompt"] = f"{prompt or ''}\n用户配文：{ref.caption}".strip()
        result = await self._tools.call_qualified("media_tools__vision_describe", args)
        if not result.ok:
            return f"[识图失败] {result.error}"
        return str((result.content or {}).get("text") or "").strip()

    async def transcribe(self, ref: MediaRef) -> str:
        result = await self._tools.call_qualified(
            "media_tools__stt_transcribe", {"audio_path": str(ref.path), "language": "zh"}
        )
        if not result.ok:
            return f"[语音转写失败] {result.error}"
        return str((result.content or {}).get("text") or "").strip()

    async def ingest(self, refs: Iterable[MediaRef]) -> tuple[list[str], str]:
        """处理一组附件。

        返回 ``(notes, merged_text)``：

        * ``notes``    —— 逐条说明（"这张图是…"、"语音内容是…"），注入主脑
        * ``merged_text`` —— 语音转写结果拼成的"用户其实说了什么"，
                              用它替代空文本，让主流程像处理纯文本一样处理语音
        """
        notes: list[str] = []
        spoken: list[str] = []

        for ref in refs:
            decision = self.decide(ref)
            logger.info(
                "多模态路由：%s → %s（%s）", ref.kind.value, decision.target, decision.reason
            )
            if decision.target == "vision":
                desc = await self.describe_image(ref)
                notes.append(f"【图片内容】{desc}" if desc else "【图片内容】识别为空")
            elif decision.target == "stt":
                text = await self.transcribe(ref)
                if text:
                    spoken.append(text)
                    notes.append(f"【语音转写】{text}")
                else:
                    notes.append("【语音转写】未识别到有效内容")
            else:
                notes.append(f"【文档】用户上传了文件：{ref.path.name}（已落盘，可按需读取）")

        return notes, "\n".join(spoken).strip()

    # ------------------------------------------------------------------ #
    # 出站：文本 → 语音
    # ------------------------------------------------------------------ #

    async def synthesize(self, text: str, *, voice: str | None = None) -> Path | None:
        """合成语音并返回**适合 Telegram 发送**的文件路径。

        会做两件事：
        1. 剥掉 Markdown / URL —— 读出来才不刺耳；
        2. 用 ffmpeg 转 OGG/Opus 以适配 ``sendVoice``；没有 ffmpeg 就返回原 mp3。
        """
        spoken = self.plain_text(text)
        if not spoken:
            return None

        stamp = int(time.time() * 1000)
        raw_path = self.cfg.media_dir / f"tts_{stamp}.mp3"
        result = await self._tools.call_qualified(
            "media_tools__tts_synthesize",
            {
                "text": spoken[:2000],
                "out_path": str(raw_path),
                "voice": voice or self.cfg.tts_voice,
                "speed": 1.0,
                "fmt": "mp3",
            },
        )
        if not result.ok:
            logger.warning("TTS 合成失败: %s", result.error)
            return None

        produced = Path(str((result.content or {}).get("path") or raw_path))
        if not produced.is_file():
            return None

        ogg = await self.to_ogg_opus(produced)
        return ogg or produced

    async def to_ogg_opus(self, source: Path) -> Path | None:
        """转成 Telegram ``sendVoice`` 要求的 OGG/Opus。"""
        if self._ffmpeg is None:
            logger.info("未找到 ffmpeg，降级发送音频文件（非语音气泡）")
            return None
        target = source.with_suffix(".ogg")
        cmd = [
            self._ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1",
            str(target),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode == 0 and target.is_file():
                return target
            logger.warning("ffmpeg 转码失败: %s", (stderr or b"").decode("utf-8", "ignore")[:200])
        except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
            logger.warning("ffmpeg 调用异常: %s", exc)
        return None

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def plain_text(markdown: str) -> str:
        """把 Markdown 压成适合朗读的纯文本。"""
        text = _MD_FENCE_RE.sub("", markdown)
        text = _URL_RE.sub("链接", text)
        text = _MD_INLINE_RE.sub("", text)
        text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.MULTILINE)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def should_reply_with_voice(self, refs: Iterable[MediaRef]) -> bool:
        """若用户用语音提问，就用语音回答 —— 同模态回复降低认知切换成本。"""
        return any(ref.kind in (MediaKind.VOICE, MediaKind.AUDIO) for ref in refs)
