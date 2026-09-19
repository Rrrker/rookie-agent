"""模块边界契约 —— 全系统唯一的接口与数据 Schema 来源。

三条硬规则（配合 ``app/__init__.py`` 的依赖方向一起生效）：

1. **跨模块传递的数据只能是本文件里的 dataclass / Enum**，不允许传裸 dict；
2. **模块之间只依赖本文件的 Protocol**，实现类之间互不 import
   —— 所以任何一个模块都能被替换成 Mock，单测不需要起真实服务；
3. **接口签名一旦变化，本文件先改**，再改实现 —— 契约先行。

模块与端口对应关系：

===================  ==================  ====================================
模块                 实现类               对外暴露的端口（Port）
===================  ==================  ====================================
消息渠道              TelegramChannel      ChannelPort
记忆层                MemoryGateway        MemoryPort
情绪系统              PadEmotionEngine     EmotionPort
多模态路由            MediaRouter          MediaPort
工具挂载              McpHub               ToolPort
主脑                  CodexBrain           BrainPort
分身调度              CloneSystem          SchedulerPort
===================  ==================  ====================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol, Sequence, runtime_checkable

__all__ = [
    # 工具函数
    "utcnow",
    # 信道层
    "MediaKind", "MediaRef", "InboundMessage", "OutboundMessage",
    # 情绪层
    "EmotionState", "EmotionEvent",
    # 记忆层
    "MemoryRecord", "MemoryWrite", "Entity", "Triple", "ExtractionResult",
    # 工具层
    "ToolSpec", "ToolResult",
    # 执行层
    "ExecResult", "CommandTrace", "ProcessInfo",
    # 分身层
    "TaskState", "TaskKind", "CloneSpec", "CloneTask", "CloneResult",
    # 端口
    "ChannelPort", "MemoryPort", "EmotionPort", "MediaPort", "ToolPort",
    "BrainPort", "SchedulerPort", "IsolatedRunnerPort",
    "ExecPort", "ProcessPort",
]


def utcnow() -> datetime:
    """统一时间源，便于测试时冻结时间。"""
    return datetime.now(timezone.utc)


# =========================================================================== #
# 1. 信道层 —— 渠道差异在这里被抹平，上层只看到 InboundMessage / OutboundMessage
# =========================================================================== #


class MediaKind(str, Enum):
    """附件类型。多模态路由完全按这个枚举做分发。"""

    IMAGE = "image"      # -> 多模态模型（vision）
    VOICE = "voice"      # -> STT
    AUDIO = "audio"      # -> STT
    DOCUMENT = "document"  # -> 暂存落盘，交给主脑按需读取


@dataclass(slots=True)
class MediaRef:
    """一条附件的引用。约定：``path`` 一定指向本地已落盘文件。

    渠道层负责把远端文件下载到本地（Telegram 的图片/语音都是 file_id），
    上层模块只操作本地路径 —— 这样 vision/stt 工具不需要知道 Telegram 的存在。
    """

    kind: MediaKind
    path: Path
    mime: str | None = None
    duration_sec: float | None = None
    caption: str | None = None
    source_id: str | None = None  # 渠道侧原始标识（如 telegram file_id）


@dataclass(slots=True)
class InboundMessage:
    """入站消息的规范化表示。"""

    channel: str
    chat_id: str
    user_id: str
    text: str = ""
    media: list[MediaRef] = field(default_factory=list)
    message_id: str | None = None
    ts: datetime = field(default_factory=utcnow)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutboundMessage:
    """出站消息。``voice_path`` 非空时渠道应优先发语音。"""

    chat_id: str
    text: str = ""
    voice_path: Path | None = None
    reply_to: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


# =========================================================================== #
# 2. 情绪层 —— PAD（Pleasure-Arousal-Dominance）
# =========================================================================== #


@dataclass(slots=True)
class EmotionState:
    """PAD 三维情绪状态。

    每一维取值 ``[-1.0, 1.0]``：

    * ``pleasure``  愉悦度：负=不快，正=愉快
    * ``arousal``   唤醒度：负=平静/困倦，正=激动/紧张
    * ``dominance`` 支配感：负=被支配/顺从，正=掌控/自信
    """

    pleasure: float = 0.0
    arousal: float = 0.0
    dominance: float = 0.0
    label: str = "neutral"
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def magnitude(self) -> float:
        """情绪强度（欧氏距离），用于判断是否需要注入语气指令。"""
        return (self.pleasure**2 + self.arousal**2 + self.dominance**2) ** 0.5

    def as_dict(self) -> dict[str, float | str]:
        return {
            "pleasure": round(self.pleasure, 4),
            "arousal": round(self.arousal, 4),
            "dominance": round(self.dominance, 4),
            "label": self.label,
        }


@dataclass(slots=True)
class EmotionEvent:
    """一次情绪刺激事件。

    ``name`` 由评估器（appraiser）产出，例如 ``"praise"`` / ``"insult"`` /
    ``"task_success"`` / ``"error"`` / ``"urgency"``；
    ``intensity`` 为 ``[0, 1]`` 的刺激强度。
    """

    name: str
    intensity: float = 1.0
    evidence: str | None = None


# =========================================================================== #
# 3. 记忆层 —— 对应 MemPalace 的 drawer（向量库）与 triple（知识图谱）
# =========================================================================== #


@dataclass(slots=True)
class MemoryRecord:
    """一次检索命中。字段与 MemPalace ``search_memories`` 的返回结构对齐。"""

    text: str
    wing: str | None = None
    room: str | None = None
    source_file: str | None = None
    similarity: float = 0.0
    drawer_id: str | None = None


@dataclass(slots=True)
class MemoryWrite:
    """一条待写入的逐字记忆（MemPalace 术语叫 drawer）。

    对齐 MemPalace ``mempalace_add_drawer`` 的工具入参：
    ``wing`` / ``room`` / ``content`` / ``source_file`` / ``added_by``。
    """

    content: str
    wing: str
    room: str
    source_file: str | None = None
    added_by: str = "agent"


@dataclass(slots=True)
class Entity:
    """知识图谱节点。对齐 MemPalace ``entities`` 表：
    ``id``（小写规范化名）/ ``name`` / ``type`` / ``properties``。"""

    name: str
    type: str = "concept"  # person | project | tool | concept
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.name.strip().lower()


@dataclass(slots=True)
class Triple:
    """时序三元组。对齐 MemPalace ``triples`` 表：

    ``subject -> predicate -> object [valid_from, valid_to]``

    * ``valid_to is None`` 表示"当前仍然成立"；
    * 事实变更用 ``supersede``（一个事务内关旧开新），
      事实结束用 ``invalidate``。
    """

    subject: str
    predicate: str
    object: str
    valid_from: str | None = None  # YYYY-MM-DD
    valid_to: str | None = None    # None = 至今有效
    confidence: float = 1.0
    source_closet: str | None = None

    def render(self) -> str:
        tail = f"[{self.valid_from or '?'} → {self.valid_to or 'now'}]"
        return f"{self.subject} --{self.predicate}--> {self.object} {tail}"


@dataclass(slots=True)
class ExtractionResult:
    """主脑用结构化输出抽取出的记忆增量。"""

    entities: list[Entity] = field(default_factory=list)
    triples: list[Triple] = field(default_factory=list)
    summary: str = ""


# =========================================================================== #
# 4. 工具层 —— MCP 工具的统一描述
# =========================================================================== #


@dataclass(slots=True)
class ToolSpec:
    """一个 MCP 工具的规范化描述。

    ``qualified_name`` 遵循 Codex 的命名空间规则：
    ``mcp__<server_key>__<tool_name>``，server key 参与命名所以不会冲突。
    """

    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        return f"mcp__{self.server}__{self.name}"

    def to_openai_tool(self) -> dict[str, Any]:
        """转成 OpenAI function-calling 的 tool 描述，交给主脑调用。"""
        return {
            "type": "function",
            "function": {
                "name": self.qualified_name,
                "description": self.description,
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


@dataclass(slots=True)
class ToolResult:
    """工具调用结果。统一 ``ok`` 字段，让上层不必区分异常与业务失败。"""

    ok: bool
    content: Any = None
    error: str | None = None
    elapsed_ms: int = 0

    @staticmethod
    def success(content: Any, elapsed_ms: int = 0) -> "ToolResult":
        return ToolResult(ok=True, content=content, elapsed_ms=elapsed_ms)

    @staticmethod
    def failure(error: str, elapsed_ms: int = 0) -> "ToolResult":
        return ToolResult(ok=False, error=error, elapsed_ms=elapsed_ms)


# =========================================================================== #
# 5. 分身层 —— 任务与结果
# =========================================================================== #


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


TaskKind = Literal["chat", "background", "cron"]


@dataclass(slots=True)
class CloneSpec:
    """一个分身的静态定义（角色、权限、隔离边界）。

    隔离体现在四个维度：

    * **上下文隔离** —— 每个分身持有自己的 Codex thread，互不污染历史；
    * **文件系统隔离** —— 每个分身有独立 ``workspace_dir``；
    * **工具面隔离** —— ``tools_allowlist`` 做最小权限，空列表=不给工具；
    * **记忆隔离** —— 每个分身写自己的 ``wing``，检索时可跨翼也可只在本翼。
    """

    clone_id: str
    name: str
    system_prompt: str
    tools_allowlist: tuple[str, ...] = ()   # 空 = 不挂任何 MCP 工具
    sandbox: str = "read_only"
    max_concurrency: int = 1
    timeout_sec: float = 600.0
    wing: str | None = None
    workspace_dir: Path | None = None


@dataclass(slots=True)
class CloneTask:
    """一个待执行的任务。任务本身不持有任何分身状态 —— 这是水平扩展的前提。"""

    task_id: str
    prompt: str
    kind: TaskKind = "background"
    clone_id: str | None = None          # None = 交给调度器自动选
    priority: int = 5                    # 数字越小越优先
    timeout_sec: float | None = None
    context: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    # 任务完成后把结果投递到这个 chat（用于定时播报）
    notify_chat_id: str | None = None


@dataclass(slots=True)
class CloneResult:
    task_id: str
    clone_id: str
    state: TaskState
    output: str = ""
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempts: int = 1

    @property
    def elapsed_sec(self) -> float:
        if not self.started_at or not self.finished_at:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()


# =========================================================================== #
# 6. 端口定义（Protocol）—— 模块之间的唯一耦合点
# =========================================================================== #


InboundHandler = Callable[[InboundMessage], Awaitable[None]]


@runtime_checkable
class ChannelPort(Protocol):
    """消息渠道。实现者：TelegramChannel。"""

    name: str

    async def start(self, handler: InboundHandler) -> None: ...
    async def send(self, msg: OutboundMessage) -> None: ...
    async def stop(self) -> None: ...


@runtime_checkable
class MemoryPort(Protocol):
    """记忆层读写接口。实现者：MemoryGateway（MemPalace 适配）。"""

    async def remember(self, write: MemoryWrite) -> str: ...
    async def recall(
        self, query: str, *, wing: str | None = None, room: str | None = None, k: int = 5
    ) -> list[MemoryRecord]: ...
    async def wake_up(self, wing: str | None = None) -> str: ...
    async def upsert_entity(self, entity: Entity) -> None: ...
    async def assert_fact(self, triple: Triple) -> None: ...
    async def invalidate_fact(self, subject: str, predicate: str, obj: str, ended: str) -> None: ...
    async def supersede_fact(
        self, subject: str, predicate: str, *, old_obj: str, new_obj: str, at: str
    ) -> None: ...
    async def facts_about(self, entity: str, *, as_of: str | None = None) -> list[Triple]: ...
    async def timeline(self, entity: str | None = None) -> list[Triple]: ...
    async def stats(self) -> dict[str, Any]: ...


@runtime_checkable
class EmotionPort(Protocol):
    """情绪系统。实现者：PadEmotionEngine。"""

    def current(self) -> EmotionState: ...
    def appraise(self, event: EmotionEvent) -> EmotionState: ...
    def appraise_text(self, text: str) -> EmotionState: ...
    def decay(self) -> EmotionState: ...
    def style_directive(self) -> str: ...
    def voice_params(self) -> dict[str, float]: ...
    def trace(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class MediaPort(Protocol):
    """多模态能力。实现者：MediaRouter（底层通过 ToolPort 调 MCP 工具）。"""

    async def describe_image(self, ref: MediaRef, *, prompt: str | None = None) -> str: ...
    async def transcribe(self, ref: MediaRef) -> str: ...
    async def synthesize(self, text: str, *, voice: str | None = None) -> Path | None: ...
    def route_of(self, kind: MediaKind) -> str: ...


@runtime_checkable
class ToolPort(Protocol):
    """工具挂载层。实现者：McpHub。"""

    async def list_tools(self) -> list[ToolSpec]: ...
    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> ToolResult: ...
    async def call_qualified(self, qualified_name: str, arguments: dict[str, Any]) -> ToolResult: ...
    def openai_tools(self, allowed: Sequence[str] | None = None) -> list[dict[str, Any]]: ...


@runtime_checkable
class IsolatedRunnerPort(Protocol):
    """分身任务的执行器。

    由 brain 提供实现（``CodexBrain.run_isolated``）、由 clones 消费 ——
    依赖方向是 clones → 端口 ← brain，于是分身调度完全不需要知道
    底层是 Codex、还是别的 agent runtime。

    实现的硬性要求：**每次调用都必须开一条全新的隔离上下文**
    （新 thread / 新沙箱 / 新工作目录），不能复用主对话的状态。
    """

    async def run_isolated(self, prompt: str, *, clone: "CloneSpec") -> str: ...


@runtime_checkable
class BrainPort(Protocol):
    """主脑。实现者：CodexBrain。"""

    async def reply(
        self,
        *,
        session_id: str,
        text: str,
        memory_context: str = "",
        style_directive: str = "",
        media_notes: Sequence[str] = (),
        trace: list["CommandTrace"] | None = None,
        changed_files: list[str] | None = None,
    ) -> str:
        """跑一轮并返回文本。

        ``trace`` / ``changed_files`` 是**由调用方持有的列表**：实现方把本轮
        Codex 执行的命令（``commandExecution`` 条目）与文件改动（``fileChange`` 条目）
        追加进去。

        用出参而不是改返回类型，有两个理由：

        1. 不破坏 :class:`BrainPort` 的既有签名，调用方可以完全忽略它们；
        2. 避免"实现方保存上一轮状态"那种跨会话竞态 —— 列表归调用方所有，天然无竞态。

        这也让**命令审计变成零新增能力**：Codex 本来就把这些条目放在
        ``TurnResult.items`` 里，我们只是把它们筛出来。
        """
        ...

    async def extract(self, transcript: str) -> ExtractionResult: ...
    async def close(self) -> None: ...


@runtime_checkable
class SchedulerPort(Protocol):
    """分身调度。实现者：CloneSystem。"""

    async def submit(self, task: CloneTask) -> str: ...
    async def result(self, task_id: str) -> CloneResult | None: ...
    async def cancel(self, task_id: str) -> bool: ...
    def add_cron(self, name: str, expr: str, factory: Callable[[], CloneTask]) -> str: ...
    def remove_cron(self, job_id: str) -> bool: ...
    def stats(self) -> dict[str, Any]: ...


# =========================================================================== #
# 7. 执行层 —— 一次性命令、命令审计、长驻进程
# =========================================================================== #
#
# 注意这层的定位：**它不是"给 agent 加执行能力"**。
# Codex 已经自带 shell 执行（TurnResult.items 里的 commandExecution 条目），
# 且受 OS 级沙箱约束。这一层服务的是**另外两类需求**：
#
#   * 我们自己的代码要跑命令（orchestrator / cron）—— 不该绕道模型；
#   * 长驻进程（start/logs/kill）—— Codex 没有这个形态。
#
# 并且必须清楚：**这一层不受 Codex 沙箱约束**，权限等于当前进程。
# =========================================================================== #


@dataclass(slots=True)
class ExecResult:
    """一次性命令的执行结果。

    四类错误刻意分开归因，因为调用方对它们的反应完全不同：

    * ``exit_code != 0`` 且 ``ok=True``  —— 命令跑了但失败，去读 stderr
    * ``timeout``                        —— 换更快的方式
    * ``not_found`` / ``spawn_failed``   —— 别重试同一串
    * ``output_overflow``                —— 输出太大，已被强杀
    """

    argv: tuple[str, ...]
    ok: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    duration_ms: int = 0
    error_code: str | None = None
    error: str | None = None


@dataclass(slots=True)
class CommandTrace:
    """Codex 自己执行过的一条命令（从 ``commandExecution`` 条目抽出）。

    字段名对齐 ``CommandExecutionThreadItem`` 的已核实定义：
    ``command`` / ``cwd`` / ``status`` / ``aggregated_output`` / ``exit_code`` / ``duration_ms``。
    ``status`` 取值：``in_progress`` / ``completed`` / ``failed`` / **``declined``**。
    """

    command: str
    cwd: str | None = None
    status: str = "unknown"
    exit_code: int | None = None
    output: str = ""
    duration_ms: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and self.status != "declined"

    @property
    def declined(self) -> bool:
        """被审批/沙箱拒绝 —— 这是"agent 尝试越界"的强信号，值得单独统计。"""
        return self.status == "declined"


@dataclass(slots=True)
class ProcessInfo:
    """一个长驻进程的快照。``error`` 非空表示这次操作没成功（不抛异常）。"""

    name: str
    argv: tuple[str, ...] = ()
    cwd: str = ""
    pid: int | None = None
    running: bool = False
    exit_code: int | None = None
    started_at: datetime | None = None
    uptime_sec: float = 0.0
    log_lines: int = 0
    log_bytes: int = 0
    error: str | None = None


@runtime_checkable
class ExecPort(Protocol):
    """内部命令执行。实现者：LocalExecutor。

    只暴露 ``run_named`` —— 命令必须先登记进白名单。
    这不是安全边界，而是防止内部代码在演进中悄悄长成任意执行入口。
    """

    def names(self) -> list[str]: ...
    def describe(self) -> dict[str, str]: ...
    async def run_named(self, name: str, *, cwd: Path | None = None) -> ExecResult: ...


@runtime_checkable
class ProcessPort(Protocol):
    """长驻进程管理。实现者：ProcessManager。

    ⚠️ 不受任何沙箱约束，权限等于当前进程；默认关闭。
    """

    async def start(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> ProcessInfo: ...
    async def stop(self, name: str, *, grace: float | None = None) -> ProcessInfo | None: ...
    async def stop_all(self) -> int: ...
    async def reap(self) -> list[ProcessInfo]: ...
    async def logs(self, name: str, *, tail_lines: int = 60) -> str: ...
    def list(self) -> list[ProcessInfo]: ...
    def get(self, name: str) -> ProcessInfo | None: ...
    def stats(self) -> dict[str, Any]: ...
