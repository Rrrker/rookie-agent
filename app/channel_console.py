"""控制台渠道 —— 第二个 ChannelPort 实现。

存在意义有两个：

1. **开发时不用摸手机验证**：没有 Telegram token 也能把整条流水线跑通；
2. **证明抽象真的成立**：渠道层换实现，上面五层代码一行都不用改。
   如果加一个渠道需要改动 orchestrator，说明端口设计失败了。

它有意识地被做得很薄 —— 渠道就该这么薄。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from app.contracts import InboundHandler, InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)


class ConsoleChannel:
    """实现 :class:`app.contracts.ChannelPort`，在终端里收发。"""

    name = "console"

    def __init__(self, chat_id: str = "local", prompt: str = "你 > ") -> None:
        self.chat_id = chat_id
        self.prompt = prompt
        self._handler: InboundHandler | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()

    async def start(self, handler: InboundHandler) -> None:
        self._handler = handler
        self._task = asyncio.create_task(self._loop(), name="console-channel")
        print("\n控制台渠道已就绪。直接输入消息；`exit` 退出。\n")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._closed.is_set():
            try:
                # input() 是阻塞的，挪到线程里，不能堵住事件循环
                line = await loop.run_in_executor(None, lambda: sys.stdin.readline())
            except (asyncio.CancelledError, RuntimeError):
                return
            if not line:
                return
            text = line.strip()
            if text in {"exit", "quit", ":q"}:
                print("再见。")
                self._closed.set()
                return
            if not text or self._handler is None:
                continue
            await self._handler(
                InboundMessage(
                    channel=self.name,
                    chat_id=self.chat_id,
                    user_id="local",
                    text=text,
                )
            )

    async def send(self, out: OutboundMessage) -> None:
        marker = "🤖 " if not out.voice_path else "🔊 "
        sys.stdout.write(f"\n{marker}{out.text}\n")
        if out.voice_path:
            sys.stdout.write(f"   [语音文件] {out.voice_path}\n")
        sys.stdout.write(self.prompt)
        sys.stdout.flush()

    async def send_typing(self, chat_id: str, *, action: str = "typing") -> None:
        sys.stdout.write("…")
        sys.stdout.flush()


class NullChannel:
    """什么都不做的渠道 —— 第三个 ChannelPort 实现。

    用途单一但明确：**让系统在没有渠道的情况下也能被完整装配起来**。
    目前最大的消费方是 ``python main.py --health-check``：运维/CI 要验证的是
    "记忆层、工具枢纽、主脑、分身调度能不能起来"，而不是"Telegram 能不能收消息"。
    用 NullChannel 就不需要 Telegram token，也不会有 stdin 读取任务挂在那里。

    顺带再证一次端口抽象成立：加第三个渠道，上面五层一行都没改。
    """

    name = "null"

    async def start(self, handler: InboundHandler) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, out: OutboundMessage) -> None: ...

