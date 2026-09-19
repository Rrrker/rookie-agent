"""集中配置层。

设计约束：
1. 所有可调参数只在这里读环境变量，其它模块禁止直接 ``os.environ``；
2. 全部是 frozen dataclass —— 配置在进程启动后不可变，避免运行期被意外改写；
3. 自带极简 .env 解析，不引入 python-dotenv，减少依赖面。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# 环境变量读取工具
# --------------------------------------------------------------------------- #


def load_dotenv(path: Path | str = ".env") -> None:
    """极简 .env 加载器：已存在的真实环境变量优先（setdefault 语义）。"""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(key: str, default: str | None = None) -> str | None:
    v = os.environ.get(key)
    return default if v is None or v.strip() == "" else v.strip()


def env_bool(key: str, default: bool = False) -> bool:
    v = env(key)
    return default if v is None else v.lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: int) -> int:
    v = env(key)
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    v = env(key)
    try:
        return float(v) if v is not None else default
    except ValueError:
        return default


def env_list(key: str) -> list[str]:
    v = env(key)
    if not v:
        return []
    return [item.strip() for item in v.split(",") if item.strip()]


def _home_mempalace(*parts: str) -> Path:
    return Path.home().joinpath(".mempalace", *parts)


# --------------------------------------------------------------------------- #
# 分模块配置
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CodexConfig:
    """主脑运行时配置。"""

    model: str = "gpt-5.6-terra"
    sandbox: str = "read_only"  # read_only | workspace_write | full_access
    api_key: str | None = None
    working_dir: Path = field(default_factory=Path.cwd)
    # 审批模式。**已用内省实测**：SDK 的 ApprovalMode 只有两个成员 ——
    # `auto_review`（默认，交给自动审查器裁决）与 `deny_all`（一律拒绝）。
    # 不存在 CLI 侧的 never / untrusted / on_request 这些名字，不要照搬文档。
    #
    # 无人值守场景（Telegram bot）默认选 `deny_all`：行为确定、fail-closed，
    # 让沙箱成为唯一的边界，而不是指望一个没人监督的"自动审查器"替你做决定。
    approval_mode: str = "deny_all"
    max_turns: int = 12
    # 每轮最长等待时间（秒）；Codex 卡死时由 orchestrator 取消
    turn_timeout_sec: float = 180.0
    # 开发模式：不真正调用 Codex，用确定性桩函数回包（smoke_test --dry-run 用）
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    token: str | None = None
    allowed_chat_ids: frozenset[str] = frozenset()
    media_dir: Path = field(default_factory=lambda: Path("./var/media"))
    # Telegram 单条消息上限 4096 字符，留一点余量做分片
    max_message_chars: int = 3800

    def is_allowed(self, chat_id: str) -> bool:
        return not self.allowed_chat_ids or str(chat_id) in self.allowed_chat_ids


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """MemPalace 记忆层配置。

    ``palace_path`` 与 ``kg_path`` 是 MemPalace 自己的磁盘布局，
    无论走进程内 Python API 还是走 MCP server，读写都是同一份数据。
    """

    palace_path: Path = field(default_factory=lambda: _home_mempalace("palace"))
    kg_path: Path = field(default_factory=lambda: _home_mempalace("knowledge_graph.sqlite3"))
    collection_name: str = "mempalace_drawers"
    default_wing: str = "wing_agent"
    default_room: str = "conversation"
    recall_topk: int = 5
    # 是否把每轮对话落成 drawer（写入成本低，默认开）
    persist_turns: bool = True
    # 是否用 Codex 抽取实体/关系三元组写进知识图谱（多一次模型调用，默认关）
    extract_facts: bool = False
    # MCP server 启动命令；留空则不启用 MCP 写入通道
    mcp_command: str = "python"
    mcp_args: tuple[str, ...] = ("-m", "mempalace.mcp_server")


@dataclass(frozen=True, slots=True)
class MediaConfig:
    """多模态模型端点。三个模态各自独立配置，便于分别路由到不同厂商。"""

    vision_base_url: str | None = None
    vision_api_key: str | None = None
    vision_model: str = "gpt-4o-mini"

    stt_base_url: str | None = None
    stt_api_key: str | None = None
    stt_model: str = "whisper-1"

    tts_base_url: str | None = None
    tts_api_key: str | None = None
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "alloy"

    ffmpeg_bin: str = "ffmpeg"
    media_dir: Path = field(default_factory=lambda: Path("./var/media"))

    # MCP server 启动命令（自建 media-tools server）
    mcp_command: str = "python"
    mcp_args: tuple[str, ...] = ("-m", "mcp_servers.media_tools_server")


@dataclass(frozen=True, slots=True)
class EmotionConfig:
    """PAD 情绪系统参数。

    这些数字是**标定值**，不是随手取的：

    * ``gain`` 0.55 —— 让一次强刺激（3 个情绪词）产生约 ±0.3 的位移，
      足以跨过 ``label_epsilon`` 改变象限，但不足以一开口就顶到 clamp；
    * ``negativity_bias`` 1.6 —— 负性偏差（Baumeister et al., 2001）：
      同等强度的负面事件心理冲击显著大于正面事件，不设偏差的话
      情绪会长期卡在正向，失去区分度；
    * ``label_epsilon`` 0.12 —— 相对**基线**的偏移小于它就判中性。
      用"相对基线的偏移"而不是"绝对值"，是因为中立不等于原点。
    """

    # 基线情绪（略偏正向、低唤醒、中性支配感）
    baseline: tuple[float, float, float] = (0.10, 0.00, 0.00)
    # 向基线衰减的半衰期（秒）；情绪不该永久残留
    decay_half_life_sec: float = 900.0
    # 单次事件的幅度增益
    gain: float = 0.55
    clamp: float = 0.80          # 单维绝对值上限，留出继续变化的空间
    negativity_bias: float = 1.6  # 负向事件的增益放大倍数
    label_epsilon: float = 0.12   # 偏离基线多少才算"有情绪"
    # 情绪强度低于该阈值时不注入语气指令，避免不必要的 prompt 抖动
    directive_threshold: float = 0.18


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """分身调度配置。"""

    max_parallel_tasks: int = 4
    clone_timeout_sec: float = 600.0
    max_queue_size: int = 64
    # 分身工作目录根：每个分身会拿到独立子目录，实现沙箱隔离
    clone_root: Path = field(default_factory=lambda: Path("./var/clones"))
    # 失败任务重试次数
    max_retries: int = 1


@dataclass(frozen=True, slots=True)
class ExecConfig:
    """内部命令执行（非模型消费方）配置 + 命令审计配置。

    定位先说清楚：Codex **自带** shell 执行且受沙箱约束，本配置管的是
    **"我们自己的代码"要跑命令**的场景（状态汇总、周期巡检、分身自检）。
    这条路径**不受沙箱约束**，所以只允许白名单里登记过的命令。
    """

    enabled: bool = True
    # 白名单命令的默认工作目录
    workdir: Path = field(default_factory=lambda: Path("./var/exec"))
    timeout_sec: float = 60.0
    # 返回级截断（给人/日志看）
    max_output_chars: int = 4000
    # 进程级上限：读到这么多字节就杀掉进程
    # 只做返回级截断是不够的 —— `yes` / `cat /dev/urandom` 会在截断前先吃穿内存
    max_capture_bytes: int = 131072
    env_extra: dict[str, str] = field(default_factory=dict)

    # ---- 命令审计：把 Codex 自己跑过的命令写进长期记忆 ----
    audit_to_memory: bool = True
    audit_room: str = "shell-audit"
    audit_output_chars: int = 300


@dataclass(frozen=True, slots=True)
class ProcessConfig:
    """长驻进程配置。

    ⚠️ **默认关闭**。这条路径不受任何沙箱约束，权限等于当前进程，
    启用必须是一个显式动作。
    """

    enabled: bool = False
    workdir: Path = field(default_factory=lambda: Path("./var/procs"))
    max_processes: int = 3
    grace_sec: float = 5.0
    # 每个进程日志环形缓冲的字节上限，防止跑一周把内存吃穿
    max_log_bytes: int = 262144


@dataclass(frozen=True, slots=True)
class AppConfig:
    """顶层配置聚合对象。"""

    codex: CodexConfig
    telegram: TelegramConfig
    memory: MemoryConfig
    media: MediaConfig
    emotion: EmotionConfig
    scheduler: SchedulerConfig
    exec: ExecConfig
    process: ProcessConfig
    data_dir: Path
    log_level: str = "INFO"


def load_config(env_file: Path | str = ".env") -> AppConfig:
    """从 .env / 环境变量组装 AppConfig。"""
    load_dotenv(env_file)

    data_dir = Path(env("AGENT_DATA_DIR", "./var") or "./var")

    codex = CodexConfig(
        model=env("AGENT_CODEX_MODEL", "gpt-5.6-terra") or "gpt-5.6-terra",
        sandbox=env("AGENT_CODEX_SANDBOX", "read_only") or "read_only",
        api_key=env("OPENAI_API_KEY"),
        working_dir=Path(env("AGENT_CODEX_CWD", str(Path.cwd())) or str(Path.cwd())),
        approval_mode=env("AGENT_CODEX_APPROVAL_MODE", "deny_all") or "deny_all",
        turn_timeout_sec=env_float("AGENT_TURN_TIMEOUT_SEC", 180.0),
        dry_run=env_bool("AGENT_DRY_RUN", False),
    )

    telegram = TelegramConfig(
        token=env("TELEGRAM_BOT_TOKEN"),
        allowed_chat_ids=frozenset(env_list("TELEGRAM_ALLOWED_CHAT_IDS")),
        media_dir=data_dir / "media",
    )

    memory = MemoryConfig(
        palace_path=Path(env("MEMPALACE_PALACE_PATH", str(_home_mempalace("palace")))),
        kg_path=Path(env("MEMPALACE_KG_PATH", str(_home_mempalace("knowledge_graph.sqlite3")))),
        collection_name=env("MEMPALACE_COLLECTION", "mempalace_drawers") or "mempalace_drawers",
        default_wing=env("AGENT_WING", "wing_agent") or "wing_agent",
        default_room=env("AGENT_ROOM_DEFAULT", "conversation") or "conversation",
        recall_topk=env_int("AGENT_MEMORY_TOPK", 5),
        persist_turns=env_bool("AGENT_PERSIST_TURNS", True),
        extract_facts=env_bool("AGENT_EXTRACT_FACTS", False),
    )

    media = MediaConfig(
        vision_base_url=env("VISION_BASE_URL"),
        vision_api_key=env("VISION_API_KEY") or env("OPENAI_API_KEY"),
        vision_model=env("VISION_MODEL", "gpt-4o-mini") or "gpt-4o-mini",
        stt_base_url=env("STT_BASE_URL"),
        stt_api_key=env("STT_API_KEY") or env("OPENAI_API_KEY"),
        stt_model=env("STT_MODEL", "whisper-1") or "whisper-1",
        tts_base_url=env("TTS_BASE_URL"),
        tts_api_key=env("TTS_API_KEY") or env("OPENAI_API_KEY"),
        tts_model=env("TTS_MODEL", "gpt-4o-mini-tts") or "gpt-4o-mini-tts",
        tts_voice=env("TTS_VOICE", "alloy") or "alloy",
        ffmpeg_bin=env("FFMPEG_BIN", "ffmpeg") or "ffmpeg",
        media_dir=data_dir / "media",
    )

    emotion = EmotionConfig(
        baseline=(
            env_float("AGENT_PAD_BASE_P", 0.10),
            env_float("AGENT_PAD_BASE_A", 0.00),
            env_float("AGENT_PAD_BASE_D", 0.00),
        ),
        decay_half_life_sec=env_float("AGENT_PAD_HALFLIFE_SEC", 900.0),
        gain=env_float("AGENT_PAD_GAIN", 0.55),
        negativity_bias=env_float("AGENT_PAD_NEGATIVITY_BIAS", 1.6),
        label_epsilon=env_float("AGENT_PAD_LABEL_EPS", 0.12),
    )

    scheduler = SchedulerConfig(
        max_parallel_tasks=env_int("AGENT_MAX_PARALLEL_TASKS", 4),
        clone_timeout_sec=env_float("AGENT_CLONE_TIMEOUT_SEC", 600.0),
        clone_root=data_dir / "clones",
    )

    exec_cfg = ExecConfig(
        enabled=env_bool("AGENT_EXEC_ENABLED", True),
        workdir=Path(env("AGENT_EXEC_WORKDIR", str(data_dir / "exec"))),
        timeout_sec=env_float("AGENT_EXEC_TIMEOUT_SEC", 60.0),
        max_output_chars=env_int("AGENT_EXEC_MAX_OUTPUT_CHARS", 4000),
        max_capture_bytes=env_int("AGENT_EXEC_MAX_CAPTURE_BYTES", 131072),
        audit_to_memory=env_bool("AGENT_EXEC_AUDIT_TO_MEMORY", True),
        audit_room=env("AGENT_EXEC_AUDIT_ROOM", "shell-audit") or "shell-audit",
        audit_output_chars=env_int("AGENT_EXEC_AUDIT_OUTPUT_CHARS", 300),
    )

    process_cfg = ProcessConfig(
        enabled=env_bool("AGENT_PROCESS_ENABLED", False),
        workdir=Path(env("AGENT_PROCESS_WORKDIR", str(data_dir / "procs"))),
        max_processes=env_int("AGENT_PROCESS_MAX", 3),
        grace_sec=env_float("AGENT_PROCESS_GRACE_SEC", 5.0),
        max_log_bytes=env_int("AGENT_PROCESS_MAX_LOG_BYTES", 262144),
    )

    return AppConfig(
        codex=codex,
        telegram=telegram,
        memory=memory,
        media=media,
        emotion=emotion,
        scheduler=scheduler,
        exec=exec_cfg,
        process=process_cfg,
        data_dir=data_dir,
        log_level=env("AGENT_LOG_LEVEL", "INFO") or "INFO",
    )


def validate_security(cfg: AppConfig) -> tuple[list[str], list[str]]:
    """启动前的 fail-closed 校验，返回 ``(阻塞性问题, 仅提醒的注意项)``。

    为什么需要它：``TelegramConfig.is_allowed()`` 的语义是 **空白名单 = 不限制**。
    而我们现在开启了执行类能力 —— 两者叠加就等于"任何人都能让这台机器跑命令"。
    所以只要执行能力开着，就强制要求 Telegram 白名单非空（fail-closed），
    而不是打一条 warning 然后照样启动。
    """
    problems: list[str] = []
    notices: list[str] = []

    if (cfg.exec.enabled or cfg.process.enabled) and cfg.telegram.token:
        if not cfg.telegram.allowed_chat_ids:
            problems.append(
                "已启用执行能力（AGENT_EXEC_ENABLED / AGENT_PROCESS_ENABLED），"
                "但 TELEGRAM_ALLOWED_CHAT_IDS 为空。空白名单 = 不限制会话，"
                "等于对所有人开放命令执行入口。请显式配置允许的 chat id。"
            )

    if cfg.process.enabled:
        notices.append(
            "AGENT_PROCESS_ENABLED=true：长驻进程不受 Codex 沙箱约束，"
            "权限等于当前进程。建议运行在容器或专用账号下。"
        )

    if cfg.codex.sandbox == "full_access":
        notices.append(
            "AGENT_CODEX_SANDBOX=full_access：Codex 无文件系统限制，"
            "仅应在一次性环境（容器 / 全新虚拟机）中使用。"
        )

    return problems, notices
