"""工具挂载层 —— MCP 客户端枢纽。

--------------------------------------------------------------------------
为什么这一层非有不可
--------------------------------------------------------------------------
Codex Python SDK **本身没有工具注册接口**。你能给 Codex 挂工具的唯一方式，
是写进它的配置文件 ``~/.codex/config.toml``：

.. code-block:: toml

    [mcp_servers.media_tools]
    command = "python"
    args = ["-m", "mcp_servers.media_tools_server"]

于是架构上就出现了**两个消费方**，它们必须看到同一份工具定义：

1. **Codex 主脑** —— 通过 Codex 自己启动 MCP server（我们只负责写配置），
   模型看到的工具名会被命名空间化成 ``mcp__media_tools__vision_describe``；
2. **本应用自己** —— orchestrator 需要主动调用这些工具
   （比如收到语音后先转文本，这一步不能让模型决定要不要做）。

所以 :class:`McpHub` 扮演的是**第 2 个消费方**，同时负责把工具 schema
导出成 OpenAI function-calling 格式；而第 1 个消费方由
``scripts/setup_codex_mcp.py`` 写配置文件来满足。

--------------------------------------------------------------------------
并发正确性：为什么每个 server 要有独立 worker task
--------------------------------------------------------------------------
MCP 官方 SDK 的 ``ClientSession`` 建在 anyio 的 task group 上。
如果在任务 A 里创建 session、在任务 B 里发起调用，anyio 会报
``Attempted to exit cancel scope in a different task``。

常见做法是"一个全局 session + 一把锁"，但那只是把并发退化成了串行，
并且仍然会在超时取消时炸掉。

本实现用的是**每 server 一个专属 worker task**：

* worker task 在自己的上下文里 ``async with`` 整个 stdio 生命周期；
* 外部调用者只往 ``asyncio.Queue`` 里投递请求并 ``await`` 一个 Future；
* 所有 MCP IO 都发生在创建它们的那个 task 里 —— anyio 的约束天然满足；
* 想断开某个 server，往队列投一个哨兵即可，不需要碰 session 对象。

副作用是：**请求排队是有序的**，而不同 server 之间完全并行。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from app.contracts import ToolResult, ToolSpec

logger = logging.getLogger(__name__)

try:  # MCP 是硬依赖，但缺了也应该给出可读的报错而不是 ImportError 栈
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _MCP_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    _MCP_AVAILABLE = False
    _MCP_IMPORT_ERROR = str(_exc)


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """一个 MCP server 的启动描述。字段名对齐 Codex ``config.toml`` 的写法。"""

    key: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    enabled: bool = True
    startup_timeout_sec: float = 25.0
    tool_timeout_sec: float = 180.0

    def to_codex_toml(self) -> str:
        """生成 Codex ``config.toml`` 片段 —— 保证两个消费方配置同源。"""
        args = ", ".join(f'"{a}"' for a in self.args)
        lines = [
            f"[mcp_servers.{self.key}]",
            f'command = "{self.command}"',
            f"args = [{args}]",
        ]
        if self.env:
            pairs = ", ".join(f'"{k}" = "{v}"' for k, v in self.env.items())
            lines.append(f"env = {{ {pairs} }}")
        if self.cwd:
            lines.append(f'cwd = "{self.cwd.as_posix()}"')
        lines.append(f"enabled = {str(self.enabled).lower()}")
        lines.append(f"startup_timeout_sec = {self.startup_timeout_sec:g}")
        lines.append(f"tool_timeout_sec = {self.tool_timeout_sec:g}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 单 server worker
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Request:
    """投递给 worker 的一次 MCP 操作。"""

    op: str  # "list_tools" | "call"
    payload: dict[str, Any]
    future: asyncio.Future[Any]


class _ServerWorker:
    """一个 MCP server 的专属执行上下文（独占一个 task）。"""

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self._queue: asyncio.Queue[_Request | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._error: BaseException | None = None
        self._tools: list[ToolSpec] = []

    # -------------------------------------------------------------- #
    # 生命周期
    # -------------------------------------------------------------- #

    async def start(self) -> None:
        if not _MCP_AVAILABLE:
            self._error = RuntimeError(f"mcp 包不可用: {_MCP_IMPORT_ERROR}")
            self._ready.set()
            return
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.spec.key}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.spec.startup_timeout_sec)
        except asyncio.TimeoutError:
            self._error = TimeoutError(f"启动超时（{self.spec.startup_timeout_sec}s）")
            await self.stop()

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)  # 哨兵：让 worker 优雅收尾
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            self._task.cancel()
        self._task = None

    @property
    def healthy(self) -> bool:
        return self._error is None and self._task is not None and not self._task.done()

    @property
    def error(self) -> BaseException | None:
        return self._error

    @property
    def tools(self) -> list[ToolSpec]:
        return list(self._tools)

    # -------------------------------------------------------------- #
    # worker 主体：所有 MCP IO 都在这个 task 里发生
    # -------------------------------------------------------------- #

    async def _run(self) -> None:
        params = StdioServerParameters(
            command=self.spec.command,
            args=list(self.spec.args),
            env={**os.environ, **self.spec.env} if self.spec.env else None,
            cwd=str(self.spec.cwd) if self.spec.cwd else None,
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    raw = await session.list_tools()
                    self._tools = [self._to_spec(t) for t in (raw.tools or [])]
                    logger.info(
                        "[mcp:%s] 已连接，注册 %d 个工具", self.spec.key, len(self._tools)
                    )
                    self._ready.set()
                    await self._serve(session)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
            logger.warning("[mcp:%s] 连接异常: %s", self.spec.key, exc)
            self._ready.set()
        finally:
            self._drain_pending()

    async def _serve(self, session: "ClientSession") -> None:
        while True:
            request = await self._queue.get()
            if request is None:
                break
            if request.future.cancelled():
                continue
            try:
                if request.op == "call":
                    result = await asyncio.wait_for(
                        session.call_tool(
                            request.payload["tool"], request.payload["arguments"]
                        ),
                        timeout=self.spec.tool_timeout_sec,
                    )
                    request.future.set_result(self._unwrap(result))
                elif request.op == "list_tools":
                    request.future.set_result(list(self._tools))
            except asyncio.CancelledError:
                if not request.future.done():
                    request.future.set_exception(RuntimeError("已取消"))
                raise
            except BaseException as exc:  # noqa: BLE001
                if not request.future.done():
                    request.future.set_exception(exc)

    def _drain_pending(self) -> None:
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not None and not item.future.done():
                item.future.set_exception(RuntimeError(f"MCP server '{self.spec.key}' 已断开"))

    # -------------------------------------------------------------- #
    # 对外请求
    # -------------------------------------------------------------- #

    async def request(self, op: str, **payload: Any) -> Any:
        if self._error is not None:
            raise RuntimeError(f"MCP server '{self.spec.key}' 不可用: {self._error}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await self._queue.put(_Request(op=op, payload=payload, future=future))
        return await future

    # -------------------------------------------------------------- #
    # 结果归一化
    # -------------------------------------------------------------- #

    def _to_spec(self, tool: Any) -> ToolSpec:
        return ToolSpec(
            server=self.spec.key,
            name=getattr(tool, "name", "unknown"),
            description=getattr(tool, "description", "") or "",
            input_schema=getattr(tool, "inputSchema", None) or {},
        )

    @staticmethod
    def _unwrap(result: Any) -> Any:
        """把 CallToolResult 压成 Python 原生结构。

        MCP 的返回是 ContentBlock 列表。约定：凡是我们自己写的 server
        都返回单个 JSON 文本块，所以这里优先尝试 ``json.loads``；
        解析失败就退回纯文本拼接。
        """
        import json

        blocks = getattr(result, "content", None) or []
        texts: list[str] = []
        for block in blocks:
            text = getattr(block, "text", None)
            if text is not None:
                texts.append(text)
        if not texts:
            return None
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except (json.JSONDecodeError, TypeError):
            return joined


# --------------------------------------------------------------------------- #
# 枢纽
# --------------------------------------------------------------------------- #


class McpHub:
    """实现 :class:`app.contracts.ToolPort`。

    职责边界：

    * 管理 N 个 MCP server 的生命周期；
    * 把分散在多个 server 的工具聚合成一张**扁平工具表**（按限定名索引）；
    * 导出 OpenAI function-calling schema 供模型侧使用；
    * 统一把失败转成 :class:`ToolResult`，上层不必 try/except。
    """

    def __init__(self, specs: Sequence[McpServerSpec] = ()) -> None:
        self._workers: dict[str, _ServerWorker] = {
            spec.key: _ServerWorker(spec) for spec in specs if spec.enabled
        }
        self._index: dict[str, ToolSpec] = {}
        self._started = False

    # -------------------------------------------------------------- #
    # 构造辅助
    # -------------------------------------------------------------- #

    @classmethod
    def from_app_config(
        cls,
        memory_cfg: Any,
        media_cfg: Any,
        *,
        enable_memory: bool = True,
        enable_media: bool = True,
    ) -> "McpHub":
        """从 AppConfig 拼出标准的两个 server。

        ``mempalace`` 既给主脑用（模型可自主记忆），也给应用自己用（记忆层写入）。
        """
        specs: list[McpServerSpec] = []

        if enable_memory:
            specs.append(
                McpServerSpec(
                    key="mempalace",
                    command=memory_cfg.mcp_command,
                    args=tuple(memory_cfg.mcp_args),
                    env={
                        "MEMPALACE_PALACE_PATH": str(memory_cfg.palace_path),
                    },
                    tool_timeout_sec=60.0,
                )
            )
        if enable_media:
            specs.append(
                McpServerSpec(
                    key="media_tools",
                    command=media_cfg.mcp_command,
                    args=tuple(media_cfg.mcp_args),
                    env={
                        "VISION_BASE_URL": media_cfg.vision_base_url or "",
                        "VISION_API_KEY": media_cfg.vision_api_key or "",
                        "VISION_MODEL": media_cfg.vision_model,
                        "STT_BASE_URL": media_cfg.stt_base_url or "",
                        "STT_API_KEY": media_cfg.stt_api_key or "",
                        "STT_MODEL": media_cfg.stt_model,
                        "TTS_BASE_URL": media_cfg.tts_base_url or "",
                        "TTS_API_KEY": media_cfg.tts_api_key or "",
                        "TTS_MODEL": media_cfg.tts_model,
                        "TTS_VOICE": media_cfg.tts_voice,
                    },
                    tool_timeout_sec=180.0,
                )
            )
        return cls(specs)

    # -------------------------------------------------------------- #
    # 生命周期
    # -------------------------------------------------------------- #

    async def start(self, *, only: Sequence[str] | None = None) -> None:
        if self._started:
            return
        targets = [
            w for key, w in self._workers.items() if only is None or key in only
        ]
        if not targets:
            self._started = True
            return
        await asyncio.gather(*(w.start() for w in targets), return_exceptions=True)
        self._refresh_index()
        self._started = True
        logger.info(
            "MCP 枢纽就绪：%d/%d 个 server，共 %d 个工具",
            sum(1 for w in self._workers.values() if w.healthy),
            len(self._workers),
            len(self._index),
        )

    async def stop(self) -> None:
        await asyncio.gather(*(w.stop() for w in self._workers.values()), return_exceptions=True)
        self._started = False

    def _refresh_index(self) -> None:
        self._index = {
            spec.qualified_name: spec for w in self._workers.values() for spec in w.tools
        }

    def bind_memory(self, memory: Any) -> None:
        """保留钩子：某些 server 需要把 hub 反向注入（当前无使用方）。"""

    # -------------------------------------------------------------- #
    # ToolPort 实现
    # -------------------------------------------------------------- #

    async def list_tools(self) -> list[ToolSpec]:
        if not self._started:
            await self.start()
        self._refresh_index()
        return list(self._index.values())

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> ToolResult:
        worker = self._workers.get(server)
        if worker is None:
            return ToolResult.failure(f"未配置 MCP server: {server}")
        if tool not in {spec.name for spec in worker.tools}:
            # 允许 server 尚未列完工具时的乐观调用（例如启动竞态）
            logger.debug("[mcp:%s] 工具 %s 未在清单中，仍尝试调用", server, tool)

        started = time.perf_counter()
        try:
            content = await worker.request("call", tool=tool, arguments=arguments)
            elapsed = int((time.perf_counter() - started) * 1000)
            return ToolResult.success(content, elapsed)
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.warning("[mcp:%s] 调用 %s 失败: %s", server, tool, exc)
            return ToolResult.failure(str(exc), elapsed)

    async def call_qualified(
        self, qualified_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        """按限定名调用工具。以下三种写法都接受：

        * ``mcp__media_tools__vision_describe`` —— 模型侧看到的完整形式
        * ``media_tools__vision_describe``       —— 简写
        * ``vision_describe``                    —— 裸工具名（须唯一）

        宽容解析是刻意的：工具名有三个来源（模型输出、本应用的路由代码、
        Codex 配置文件），让它们共用同一个入口，比要求所有调用方记住
        同一种拼法更不容易出错。
        """
        # 1) 完整限定名精确命中（最可靠）
        spec = self._index.get(qualified_name)
        if spec is not None:
            return await self.call(spec.server, spec.name, arguments)

        # 2) mcp__<server>__<tool>
        if qualified_name.startswith("mcp__"):
            _, server, tool = qualified_name.split("__", 2)
            return await self.call(server, tool, arguments)

        # 3) <server>__<tool>
        if "__" in qualified_name:
            server, tool = qualified_name.split("__", 1)
            return await self.call(server, tool, arguments)

        # 4) 裸工具名：要求全表唯一，否则宁可报错也不要猜错
        matches = [s for s in self._index.values() if s.name == qualified_name]
        if len(matches) == 1:
            return await self.call(matches[0].server, matches[0].name, arguments)

        return ToolResult.failure(f"无法解析工具名: {qualified_name}")

    def openai_tools(self, allowed: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """导出 function-calling schema。

        ``allowed`` 支持三种写法：完整限定名、``server__tool``、裸工具名。
        传空序列 = 明确不给任何工具（用于最小权限分身）。
        """
        if allowed is not None and len(allowed) == 0:
            return []

        out: list[dict[str, Any]] = []
        for qualified, spec in self._index.items():
            if allowed:
                short = f"{spec.server}__{spec.name}"
                if not (qualified in allowed or short in allowed or spec.name in allowed):
                    continue
            out.append(spec.to_openai_tool())
        return out

    # -------------------------------------------------------------- #
    # 可观测性
    # -------------------------------------------------------------- #

    def health(self) -> dict[str, Any]:
        self._refresh_index()
        return {
            "servers": {
                key: {
                    "healthy": w.healthy,
                    "tools": [s.name for s in w.tools],
                    "error": str(w.error) if w.error else None,
                }
                for key, w in self._workers.items()
            },
            "tool_count": len(self._index),
        }

    @property
    def ready(self) -> bool:
        return self._started and any(w.healthy for w in self._workers.values())
