"""Telegram 消息渠道 —— 渠道差异的唯一吸收层。

--------------------------------------------------------------------------
职责边界（写死在这里，越界就是设计错误）
--------------------------------------------------------------------------
只做三件事：

1. **收**  把 Telegram 的各种 message 形态（文本/图片/语音/音频/文档）
   规范化成 :class:`app.contracts.InboundMessage`；
2. **落盘** 把远端 ``file_id`` 下载成本地文件 —— 上层只认本地路径，
   于是 vision/stt 工具完全不需要知道 Telegram 的存在；
3. **发**  把 :class:`app.contracts.OutboundMessage` 渲染成 Telegram 消息
   （文本分片 / 语音气泡 / 打字中指示器）。

**不做的事**：不做任何语义判断、不调模型、不碰记忆。
收到消息就交给 ``handler``，发消息就照单执行。

--------------------------------------------------------------------------
两个容易踩的坑
--------------------------------------------------------------------------
* **4096 字符上限** —— 超长回复会被 Telegram 拒绝，所以在这里分片，
  而不是让上层的回复长度受制于渠道限制。
* **发送前先发 typing 指示** —— 主脑一轮可能要十几秒，
  没有反馈用户会以为机器人死了。打字指示器是体验的关键，不是可选项。
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from app.config import TelegramConfig
from app.contracts import (
    InboundMessage,
    InboundHandler,
    MediaKind,
    MediaRef,
    OutboundMessage,
)

logger = logging.getLogger(__name__)

try:
    from telegram import Bot, Message, Update
    from telegram.constants import ChatAction
    from telegram.error import TelegramError
    from telegram.ext import Application, ContextTypes, MessageHandler, filters

    _PTB_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    _PTB_AVAILABLE = False
    _PTB_IMPORT_ERROR = str(_exc)

# mime 前缀 → 附件类型
_MIME_KIND: tuple[tuple[str, MediaKind], ...] = (
    ("image/", MediaKind.IMAGE),
    ("audio/", MediaKind.AUDIO),
    ("video/", MediaKind.DOCUMENT),
    ("application/", MediaKind.DOCUMENT),
)


class TelegramChannel:
    """实现 :class:`app.contracts.ChannelPort`。"""

    name = "telegram"

    def __init__(self, cfg: TelegramConfig) -> None:
        if not _PTB_AVAILABLE:
            raise RuntimeError(f"python-telegram-bot 不可用: {_PTB_IMPORT_ERROR}")
        if not cfg.token:
            raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN，无法启动 Telegram 渠道")
        self.cfg = cfg
        cfg.media_dir.mkdir(parents=True, exist_ok=True)
        self._app: "Application | None" = None
        self._handler: InboundHandler | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self, handler: InboundHandler) -> None:
        self._handler = handler
        self._app = (
            Application.builder()
            .token(self.cfg.token)
            .concurrent_updates(True)  # 允许并发处理，避免一个慢请求堵住所有人
            .build()
        )
        self._app.add_handler(
            MessageHandler(
                filters.TEXT
                | filters.PHOTO
                | filters.VOICE
                | filters.AUDIO
                | filters.Document.ALL,
                self._on_message,
            )
        )
        # 手动管理生命周期，而不是 run_polling()：我们要和 asyncio 主循环共存
        await self._app.initialize()
        await self._app.start()
        assert self._app.updater is not None
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram 渠道已启动（@%s）", (await self._app.bot.get_me()).username)

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            if self._app.updater is not None:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        except TelegramError as exc:
            logger.warning("Telegram 关闭异常: %s", exc)
        finally:
            self._app = None

    # ------------------------------------------------------------------ #
    # 收
    # ------------------------------------------------------------------ #

    async def _on_message(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        msg: Message | None = update.effective_message
        if msg is None or msg.chat is None:
            return

        chat_id = str(msg.chat.id)
        if not self.cfg.is_allowed(chat_id):
            logger.warning("拒绝未授权会话 %s", chat_id)
            try:
                await msg.reply_text("这个机器人未对当前会话开放。")
            except TelegramError:
                pass
            return

        try:
            inbound = await self._normalize(msg, chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("消息规范化失败")
            await self._safe_reply(msg, f"消息解析失败：{exc}")
            return

        if inbound is None:
            return  # 空消息（例如只有无法处理的附件），静默忽略

        if self._handler is None:
            logger.warning("渠道已启动但未注册 handler，丢弃消息")
            return

        # 立刻给反馈，再进主流程
        await self.send_typing(chat_id)
        try:
            await self._handler(inbound)
        except Exception as exc:  # noqa: BLE001
            logger.exception("处理入站消息时异常")
            await self._safe_reply(msg, f"处理这条消息时出错了：{exc}")

    async def _normalize(self, msg: "Message", chat_id: str) -> InboundMessage | None:
        """Telegram Message → InboundMessage（含附件下载）。"""
        media: list[MediaRef] = []
        text = (msg.text or msg.caption or "").strip()

        if msg.photo:
            # photo 是一组不同尺寸，取分辨率最大的那个
            largest = msg.photo[-1]
            ref = await self._download(
                bot=msg.get_bot(),
                file_id=largest.file_id,
                chat_id=chat_id,
                kind=MediaKind.IMAGE,
                suffix=".jpg",
                caption=msg.caption,
            )
            if ref:
                media.append(ref)

        if msg.voice:
            ref = await self._download(
                bot=msg.get_bot(),
                file_id=msg.voice.file_id,
                chat_id=chat_id,
                kind=MediaKind.VOICE,
                suffix=".ogg",
                mime=msg.voice.mime_type,
                duration=float(msg.voice.duration or 0),
                caption=msg.caption,
            )
            if ref:
                media.append(ref)

        if msg.audio:
            ref = await self._download(
                bot=msg.get_bot(),
                file_id=msg.audio.file_id,
                chat_id=chat_id,
                kind=MediaKind.AUDIO,
                suffix=Path(msg.audio.file_name or ".mp3").suffix or ".mp3",
                mime=msg.audio.mime_type,
                duration=float(msg.audio.duration or 0),
                caption=msg.caption,
            )
            if ref:
                media.append(ref)

        if msg.document:
            doc = msg.document
            mime = doc.mime_type or ""
            kind = next(
                (k for prefix, k in _MIME_KIND if mime.startswith(prefix)), MediaKind.DOCUMENT
            )
            suffix = Path(doc.file_name or "").suffix or (mimetypes.guess_extension(mime) or ".bin")
            ref = await self._download(
                bot=msg.get_bot(),
                file_id=doc.file_id,
                chat_id=chat_id,
                kind=kind,
                suffix=suffix,
                mime=mime,
                caption=msg.caption,
            )
            if ref:
                media.append(ref)

        if not text and not media:
            return None

        user = msg.from_user
        return InboundMessage(
            channel=self.name,
            chat_id=chat_id,
            user_id=str(user.id) if user else "unknown",
            text=text,
            media=media,
            message_id=str(msg.message_id),
            raw={"chat_type": msg.chat.type, "username": getattr(user, "username", None)},
        )

    async def _download(
        self,
        *,
        bot: "Bot",
        file_id: str,
        chat_id: str,
        kind: MediaKind,
        suffix: str,
        mime: str | None = None,
        duration: float | None = None,
        caption: str | None = None,
    ) -> MediaRef | None:
        """把远端文件落到本地 ``media_dir/<chat_id>/``。"""
        try:
            tg_file = await bot.get_file(file_id)
            target_dir = self.cfg.media_dir / chat_id
            target_dir.mkdir(parents=True, exist_ok=True)
            stamp = tg_file.file_unique_id or file_id[-12:]
            target = target_dir / f"{kind.value}_{stamp}{suffix}"
            if not target.exists():
                await tg_file.download_to_drive(custom_path=str(target))
            return MediaRef(
                kind=kind,
                path=target,
                mime=mime,
                duration_sec=duration,
                caption=caption,
                source_id=file_id,
            )
        except TelegramError as exc:
            logger.warning("下载附件失败 (%s): %s", kind.value, exc)
            return None

    # ------------------------------------------------------------------ #
    # 发
    # ------------------------------------------------------------------ #

    async def send(self, out: OutboundMessage) -> None:
        if self._app is None:
            logger.warning("渠道未启动，丢弃出站消息")
            return
        bot = self._app.bot

        # 有语音就发语音气泡；同时把文本也发出去，方便回溯与复制
        if out.voice_path is not None and out.voice_path.is_file():
            if await self._send_voice(bot, out):
                pass
            else:
                logger.info("语音发送失败，回退为纯文本")

        if out.text.strip():
            for chunk in self._split(out.text):
                try:
                    await bot.send_message(
                        chat_id=out.chat_id,
                        text=chunk,
                        reply_to_message_id=(
                            int(out.reply_to) if out.reply_to and out.reply_to.isdigit() else None
                        ),
                    )
                except TelegramError as exc:
                    logger.error("发送文本失败: %s", exc)
                    break

    async def _send_voice(self, bot: "Bot", out: OutboundMessage) -> bool:
        """OGG/Opus 走 send_voice（语音气泡），其它格式走 send_audio。"""
        assert out.voice_path is not None
        path = out.voice_path
        try:
            with path.open("rb") as fh:
                if path.suffix.lower() in {".ogg", ".oga", ".opus"}:
                    await bot.send_voice(
                        chat_id=out.chat_id,
                        voice=fh,
                        caption=(out.text[:900] if out.text else None),
                    )
                else:
                    await bot.send_audio(chat_id=out.chat_id, audio=fh, title="回复")
            return True
        except TelegramError as exc:
            logger.error("发送语音失败: %s", exc)
            return False

    async def send_typing(self, chat_id: str, *, action: str = ChatAction.TYPING) -> None:
        """发送"正在输入"指示。主脑耗时较长时这是必要的体验补偿。"""
        if self._app is None:
            return
        try:
            await self._app.bot.send_chat_action(chat_id=chat_id, action=action)
        except TelegramError:
            pass

    async def _safe_reply(self, msg: "Message", text: str) -> None:
        try:
            await msg.reply_text(text[:3800])
        except TelegramError:
            pass

    def _split(self, text: str) -> list[str]:
        """按 Telegram 上限分片，尽量在换行处断开。"""
        limit = self.cfg.max_message_chars
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text
        while len(remaining) > limit:
            window = remaining[:limit]
            cut = window.rfind("\n")
            if cut < limit // 2:
                cut = limit
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        if remaining:
            chunks.append(remaining)
        return chunks

    @property
    def bot_username(self) -> str | None:
        return getattr(self._app.bot, "username", None) if self._app else None
