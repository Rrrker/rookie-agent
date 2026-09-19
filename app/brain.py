"""主脑 —— Codex Agent SDK 封装。

--------------------------------------------------------------------------
Codex SDK 的真实形态（决定了封装怎么写）
--------------------------------------------------------------------------
``openai-codex`` 这个包**不是**一个进程内的 LLM 客户端，它做的是：

    用 JSON-RPC 控制一个本地的 Codex app-server 进程

因此它的对象全都自带长生命周期和状态：

* ``AsyncCodex``           —— 控制通道，整个进程一个就够
* ``codex.thread_start()`` —— 开一条**有状态的线程**，服务端保存全部历史
* ``thread.run(prompt)``   —— 在线程上跑一轮，返回 ``TurnResult``

这带来两个重要的设计后果：

1. **线程就是会话记忆**。所以本模块把 ``session_id → thread_id`` 持久化，
   重启后用 ``thread_resume()`` 接回原线程，而不是把历史塞进 prompt。
   这是 Codex 相对"裸 LLM"最大的优势，不用就浪费了。
2. **线程不能被并发写**。同一会话连发两条消息会打乱上下文，
   所以每个 session 一把 ``asyncio.Lock``。

--------------------------------------------------------------------------
三段式 prompt 装配（这是本模块的核心设计）
--------------------------------------------------------------------------
把提示词按**变化频率**拆成三层，各走各的通道：

========================  ====================================  ==================
内容                       走哪条通道                             变化频率
========================  ====================================  ==================
人格与能力边界             ``base_instructions``（建线程时）      几乎不变
工具使用规则               ``developer_instructions``            极少变
记忆上下文 / 情绪指令      run 输入里的 ``<context>`` 块            每轮都变
========================  ====================================  ==================

这样做的好处是：高频变化的内容不会污染低频的指令层，
而 Codex 服务端也能更好地复用线程状态。

--------------------------------------------------------------------------
一条重要事实：线程 ≠ 记忆
--------------------------------------------------------------------------
Codex 的 thread 是**短期记忆**（这一段对话的上下文），
MemPalace 是**长期记忆**（跨会话跨项目的事实）。
两者是互补的，不能互相替代：单靠 thread 换台设备就断片，
单靠长期记忆则丢掉"刚才我们聊到哪了"。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from app.command_audit import extract_commands, extract_file_changes
from app.config import CodexConfig
from app.contracts import CloneSpec, CommandTrace, Entity, ExtractionResult, Triple, ToolPort

logger = logging.getLogger(__name__)

try:
    from openai_codex import AsyncCodex, Sandbox

    _SDK_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    _SDK_AVAILABLE = False
    _SDK_IMPORT_ERROR = str(_exc)
    Sandbox = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 固定提示词层
# --------------------------------------------------------------------------- #

BASE_INSTRUCTIONS = """你是「小黄毛」，一个长期陪伴型 Agent。你通过 Telegram 与用户对话。

核心行为准则：
- 直接给结论，再补必要的说明。不要用「好问题」「我很乐意帮您」这类填充语开篇。
- 不知道就说不知道。不要编造事实、数字、链接或 API 签名。
- 涉及代码时给出可直接运行的版本，并说明依赖与运行方式。
- 用户可能有非技术背景：先说清「做什么、为什么」，再展开实现细节。
- 回复默认用中文，简洁明了。除非用户要求，不要长篇大论。
"""

TOOL_INSTRUCTIONS = """你可以调用外部工具来扩展能力，这些工具通过 MCP 协议挂载：

- `mcp__media_tools__vision_describe` — 识图：分析本地图片内容并提取文字
- `mcp__media_tools__stt_transcribe`   — 语音转文本
- `mcp__media_tools__tts_synthesize`   — 文本转语音，产出音频文件
- `mcp__media_tools__media_probe`      — 探测文件类型与时长
- `mcp__mempalace__*`                  — 长期记忆的读写（检索历史、写入事实、维护实体关系）

使用规则：
1. 需要图片内容时调用识图工具，不要凭文件名猜测图片内容。
2. 记忆写入要克制：只记「稳定的偏好、明确的事实、达成的结论」，
   不要记录寒暄、临时状态或你自己的推理过程。
3. 检索记忆时优先用语义描述而不是关键词，例如
   「之前关于部署方案的讨论」比「部署」召回效果好得多。
4. 工具失败时要如实告知用户，不要用编造的内容掩盖失败。

关于 shell 命令（你自带 shell 工具，它受沙箱约束 —— 沙箱是边界，不是你的判断）：
5. **能用专用工具就别用 shell。** 读文件用读文件能力、搜内容用搜索能力、
   改文件用编辑能力。shell 是最后手段：`cat` / `grep` / `sed -i` 这类用法
   既慢又容易出错，还会把输出污染成难以阅读的一坨。
6. **先探再动。** 不确定命令是否存在、路径对不对时，先用只读方式确认
   （`--help` / `--version` / `ls`），不要凭猜测直接执行会改变状态的命令。
7. **破坏性操作先说后做。** 删除、覆盖、移动、force push 这类操作，
   先用一句话说明你打算做什么、影响范围是什么，再执行。
8. 命令失败时如实报告退出码与 stderr，不要换个说法掩盖。
   退出码 127 是"命令不存在"（别重试同一串），128+N 是被信号 N 终止。
"""


# --------------------------------------------------------------------------- #
# 会话 → 线程 的持久化映射
# --------------------------------------------------------------------------- #


class _SessionStore:
    """``session_id → thread_id`` 映射表。

    用 SQLite 而不是内存字典，是因为进程重启后要能 ``thread_resume()``
    接回原线程 —— 否则"长期记忆 Agent"这个承诺就只兑现了一半。
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "  session_id TEXT PRIMARY KEY,"
                "  thread_id  TEXT NOT NULL,"
                "  updated_at TEXT NOT NULL"
                ")"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, session_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thread_id FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return str(row["thread_id"]) if row else None

    def put(self, session_id: str, thread_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, thread_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET thread_id = excluded.thread_id, "
                "updated_at = excluded.updated_at",
                (session_id, thread_id, datetime.now(timezone.utc).isoformat()),
            )

    def forget(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


# --------------------------------------------------------------------------- #
# 事实抽取的 JSON Schema（Codex 结构化输出用）
# --------------------------------------------------------------------------- #

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["person", "project", "tool", "concept"]},
                },
                "required": ["name", "type"],
                "additionalProperties": False,
            },
        },
        "triples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "valid_from": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["subject", "predicate", "object"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "entities", "triples"],
    "additionalProperties": False,
}

_EXTRACT_PROMPT = """从下面这段对话里抽取**稳定事实**，用于写入长期知识图谱。

要求：
- 只抽「人物、项目、工具、概念」之间的持久关系，例如
  用户偏好、技术选型、项目归属、发布时间。
- 不要抽临时状态（"今天很累"）、寒暄、以及你自己说过的客套话。
- predicate 用简短的英文下划线式，例如 prefers / works_on / uses_model / located_at。
- valid_from 用 YYYY-MM-DD；无法判断就省略。
- 没有可抽取的内容就返回空数组。

对话内容：
---
{transcript}
---
"""


class CodexBrain:
    """实现 :class:`app.contracts.BrainPort` 与 ``IsolatedRunnerPort``。

    单例使用：整个进程一个 ``AsyncCodex`` 控制通道，
    多会话通过多条 thread 并行。
    """

    def __init__(
        self,
        cfg: CodexConfig,
        tools: ToolPort | None = None,
        *,
        session_db: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self._tools = tools
        self._stack: contextlib.AsyncExitStack | None = None
        self._codex: Any | None = None
        self._threads: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._store = _SessionStore(session_db or Path("./var/sessions.sqlite3"))

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self.cfg.dry_run:
            logger.warning("主脑处于 dry_run 模式：不会调用真实 Codex，仅回显")
            return
        if not _SDK_AVAILABLE:
            raise RuntimeError(f"openai-codex 不可用: {_SDK_IMPORT_ERROR}")

        self._stack = contextlib.AsyncExitStack()
        self._codex = await self._stack.enter_async_context(AsyncCodex())
        if self.cfg.api_key:
            try:
                await self._codex.login_api_key(self.cfg.api_key)
                logger.info("已使用 API Key 登录 Codex")
            except Exception as exc:  # noqa: BLE001
                logger.warning("API Key 登录失败，将尝试复用已有登录态: %s", exc)
        account = await self._codex.account()
        logger.info("Codex 主脑就绪，账号=%s", getattr(account, "account", "unknown"))

    async def close(self) -> None:
        self._threads.clear()
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._codex = None

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    def _sandbox(self) -> Any:
        """把配置里的字符串安全地映射到 SDK 的 Sandbox 预设。"""
        if Sandbox is None:
            return None
        return getattr(Sandbox, self.cfg.sandbox, Sandbox.read_only)

    def _approval(self) -> Any:
        """把配置里的字符串映射到 SDK 的 ApprovalMode。

        **已用内省实测**：``ApprovalMode`` 只有两个成员 —— ``auto_review``（默认）
        与 ``deny_all``。**没有** CLI 侧的 never / untrusted / on_request，
        照搬官方文档写 ``never`` 会踩空。

        名字对不上时**不静默降级**：明确告警并改用最保守的 ``deny_all``。
        因为"审批模式配错了却悄悄用了宽松默认值"是最危险的失败方式。

        （这里用函数内 import 是刻意的：保持 ``brain`` 模块在没装 SDK 时仍可导入，
        与模块顶部 ``try/except`` 的意图一致。调用频率是"每建一条线程一次"，无性能问题。）
        """
        try:
            from openai_codex import ApprovalMode
        except Exception:  # noqa: BLE001
            return None

        wanted = self.cfg.approval_mode
        member = getattr(ApprovalMode, wanted, None)
        # 必须用 isinstance 判断而不是 hasattr：ApprovalMode 是 str 的子类，
        # `hasattr(ApprovalMode, "capitalize")` 同样是 True —— 一个拼错的配置项
        # 会静默拿到 str 的方法对象传进 SDK，然后报一个完全看不懂的 RPC 错误。
        if isinstance(member, ApprovalMode):
            return member
        logger.warning(
            "未知的 approval_mode=%r（SDK 只支持 auto_review / deny_all），已回退 deny_all",
            wanted,
        )
        return getattr(ApprovalMode, "deny_all", None)

    # ------------------------------------------------------------------ #
    # 线程管理
    # ------------------------------------------------------------------ #

    async def _thread_for(self, session_id: str) -> Any:
        """取（或建/接回）本会话的 Codex thread。

        优先级：内存里的活线程 → 存储里记录的线程（resume）→ 新建。
        """
        if session_id in self._threads:
            return self._threads[session_id]

        assert self._codex is not None
        thread = None
        stored = self._store.get(session_id)
        if stored:
            try:
                thread = await self._codex.thread_resume(
                    stored,
                    sandbox=self._sandbox(),
                    approval_mode=self._approval(),
                    cwd=str(self.cfg.working_dir),
                )
                logger.info("接回历史线程 session=%s thread=%s", session_id, stored)
            except Exception as exc:  # noqa: BLE001
                logger.warning("线程 %s 接回失败，将新建: %s", stored, exc)

        if thread is None:
            thread = await self._codex.thread_start(
                sandbox=self._sandbox(),
                approval_mode=self._approval(),
                cwd=str(self.cfg.working_dir),
                base_instructions=BASE_INSTRUCTIONS,
                developer_instructions=TOOL_INSTRUCTIONS,
                model=self.cfg.model,
            )
            logger.info(
                "新建线程 session=%s thread=%s sandbox=%s approval=%s",
                session_id, thread.id, self.cfg.sandbox, self.cfg.approval_mode,
            )

        self._threads[session_id] = thread
        self._store.put(session_id, thread.id)
        return thread

    async def reset_session(self, session_id: str) -> None:
        """开一条全新线程（用户说"忘掉之前的"时用）。"""
        self._threads.pop(session_id, None)
        self._store.forget(session_id)

    # ------------------------------------------------------------------ #
    # 组装 prompt
    # ------------------------------------------------------------------ #

    @staticmethod
    def _compose_input(
        text: str,
        memory_context: str,
        style_directive: str,
        media_notes: Sequence[str],
    ) -> str:
        """把每轮变化的内容包进 ``<context>`` 块。

        显式标注"不要复述"很重要 —— 否则模型经常会把记忆当成用户说的话去回应。
        """
        parts: list[str] = []
        if style_directive.strip():
            parts.append(style_directive.strip())
        if media_notes:
            parts.append("本轮附件解析结果：\n" + "\n".join(f"- {n}" for n in media_notes))
        if memory_context.strip():
            parts.append(memory_context.strip())

        body = text.strip() or "（用户只发了附件，没有文字）"
        if not parts:
            return body

        context_block = "\n\n".join(parts)
        return (
            "<context>\n"
            "以下是本轮可用上下文，供你参考。不要复述、不要向用户展示这段内容。\n\n"
            f"{context_block}\n"
            "</context>\n\n"
            f"{body}"
        )

    # ------------------------------------------------------------------ #
    # 主流程：回复
    # ------------------------------------------------------------------ #

    async def reply(
        self,
        *,
        session_id: str,
        text: str,
        memory_context: str = "",
        style_directive: str = "",
        media_notes: Sequence[str] = (),
        trace: list[CommandTrace] | None = None,
        changed_files: list[str] | None = None,
    ) -> str:
        prompt = self._compose_input(text, memory_context, style_directive, media_notes)

        if self.cfg.dry_run:
            return self._dry_reply(text, memory_context, style_directive, media_notes)

        async with self._lock_for(session_id):
            thread = await self._thread_for(session_id)
            try:
                result = await asyncio.wait_for(
                    thread.run(prompt, output_schema=None),
                    timeout=self.cfg.turn_timeout_sec,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Codex 轮次超时（%ss）session=%s", self.cfg.turn_timeout_sec, session_id
                )
                return "这一轮处理超时了。可以换个说法再试一次，或者把需求拆小一点。"
            except Exception as exc:  # noqa: BLE001
                logger.exception("Codex 轮次失败")
                return f"调用模型时出错了：{exc}"

        items = list(getattr(result, "items", []) or [])

        # 命令与文件改动都躺在 items 里（commandExecution / fileChange 条目），
        # 直接筛出来交给调用方 —— 所以"命令审计"是零新增能力，不需要任何拦截层。
        if trace is not None:
            trace.extend(extract_commands(items))
        if changed_files is not None:
            changed_files.extend(extract_file_changes(items))

        answer = (result.final_response or "").strip()
        if not answer:
            # Codex 只做了工具调用没产出文本时 final_response 为 None
            answer = "（这一轮没有生成文字回复，可能只执行了工具调用。）"
        logger.info(
            "session=%s 轮次完成，items=%d（命令 %d 条），usage=%s",
            session_id,
            len(items),
            len(trace) if trace is not None else 0,
            getattr(result, "usage", None),
        )
        return answer

    # ------------------------------------------------------------------ #
    # 主流程：事实抽取（结构化输出）
    # ------------------------------------------------------------------ #

    async def extract(self, transcript: str) -> ExtractionResult:
        """用 Codex 的结构化输出抽取实体与三元组。

        单独起一条**一次性线程**（``ephemeral``），不污染主对话线程。
        """
        if self.cfg.dry_run or not transcript.strip():
            return ExtractionResult(summary="dry-run，未抽取")

        if self._codex is None:
            return ExtractionResult()

        prompt = _EXTRACT_PROMPT.format(transcript=transcript[:6000])
        try:
            thread = await self._codex.thread_start(
                sandbox=self._sandbox(),
                cwd=str(self.cfg.working_dir),
                base_instructions="你是一个严谨的信息抽取器，只输出符合 schema 的 JSON。",
                ephemeral=True,
            )
            result = await asyncio.wait_for(
                thread.run(prompt, output_schema=EXTRACTION_SCHEMA),
                timeout=self.cfg.turn_timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("事实抽取失败: %s", exc)
            return ExtractionResult()

        raw = (result.final_response or "").strip()
        if not raw:
            return ExtractionResult()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("抽取结果不是合法 JSON：%s", raw[:200])
            return ExtractionResult()

        return ExtractionResult(
            summary=str(data.get("summary") or ""),
            entities=[
                Entity(name=e["name"], type=e.get("type", "concept"))
                for e in data.get("entities", [])
                if e.get("name")
            ],
            triples=[
                Triple(
                    subject=t["subject"],
                    predicate=t["predicate"],
                    object=t["object"],
                    valid_from=t.get("valid_from"),
                    confidence=float(t.get("confidence", 1.0)),
                )
                for t in data.get("triples", [])
                if t.get("subject") and t.get("object")
            ],
        )

    # ------------------------------------------------------------------ #
    # 分身执行器（实现 IsolatedRunnerPort）
    # ------------------------------------------------------------------ #

    async def run_isolated(self, prompt: str, *, clone: CloneSpec) -> str:
        """以分身身份跑一个任务。

        **隔离三件套**（这是分身系统的物理基础）：

        * 新开一条 thread —— 不共享主对话历史；
        * ``cwd = clone.workspace_dir`` —— 文件写入被限制在分身自己的目录；
        * ``base_instructions = clone.system_prompt`` —— 角色人格独立。

        线程用完即弃（``ephemeral``），避免长期占用服务端会话资源。
        """
        if self.cfg.dry_run:
            return f"【dry-run 分身「{clone.name}」】已完成：{prompt[:160]}"

        if self._codex is None:
            raise RuntimeError("主脑未启动，无法执行分身任务")

        sandbox = getattr(Sandbox, clone.sandbox, self._sandbox()) if Sandbox else None
        thread = await self._codex.thread_start(
            sandbox=sandbox,
            approval_mode=self._approval(),
            cwd=str(clone.workspace_dir or self.cfg.working_dir),
            base_instructions=clone.system_prompt,
            developer_instructions=TOOL_INSTRUCTIONS if clone.tools_allowlist else "",
            model=self.cfg.model,
            ephemeral=True,
        )
        result = await asyncio.wait_for(
            thread.run(prompt, output_schema=None), timeout=clone.timeout_sec
        )
        return (result.final_response or "").strip()

    # ------------------------------------------------------------------ #
    # dry_run 桩：让整条流水线在无 Codex 环境下也能被验证
    # ------------------------------------------------------------------ #

    @staticmethod
    def _dry_reply(
        text: str,
        memory_context: str,
        style_directive: str,
        media_notes: Sequence[str],
    ) -> str:
        lines = ["【dry-run 回复】已收到你的消息。"]
        if media_notes:
            lines.append("附件解析：" + "；".join(media_notes))
        if style_directive:
            first = style_directive.splitlines()
            lines.append("情绪注入：" + (first[0] if first else ""))
        if memory_context:
            lines.append(f"记忆上下文长度：{len(memory_context)} 字符")
        lines.append(f"原始输入：{text[:200] or '（空）'}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 可观测性
    # ------------------------------------------------------------------ #

    def health(self) -> dict[str, Any]:
        return {
            "mode": "dry_run" if self.cfg.dry_run else "live",
            "model": self.cfg.model,
            "sandbox": self.cfg.sandbox,
            "active_threads": len(self._threads),
            "known_sessions": len(self._locks),
        }
