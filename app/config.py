"""集中配置层。

设计约束：
1. 所有可调参数只在这里读环境变量，其它模块禁止直接 ``os.environ``；
2. 全部是 frozen dataclass —— 配置在进程启动后不可变，避免运行期被意外改写；
3. 自带极简 .env 解析，不引入 python-dotenv，减少依赖面。
"""

from __future__ import annotations

import os
import shutil
import sys
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
# 路径锚点 —— 为什么不能让路径跟着 CWD 走
# --------------------------------------------------------------------------- #


def project_root() -> Path:
    """项目根目录：以本文件位置为锚点，而不是当前工作目录。

    这一条是**部署刚需**，不是洁癖：

    * systemd 默认把服务的工作目录设为 ``/``。此时 ``./var`` 会被解析成 ``/var``，
      非 root 服务账号直接 ``PermissionError: [Errno 13]``，**启动即失败**；
    * 若以 root 运行，则把运行数据（记忆 outbox、会话库、附件）写进系统 ``/var``，
      比启动失败更糟；
    * ``.env`` 同理：读不到时会**静默回落到全部默认配置**，比报错更难排查。

    用 ``__file__`` 锚定后，无论从哪个目录、以什么方式启动，行为都一致。
    """
    return Path(__file__).resolve().parent.parent


def resolve_from_root(path: Path | str, *, root: Path | None = None) -> Path:
    """把路径统一解析成绝对路径；相对值以**项目根**为基准（不是 CWD）。"""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (root or project_root()) / p
    return p.resolve()


def _default_data_dir() -> Path:
    """运行数据根目录的兜底默认值：``<项目根>/var``。"""
    return project_root() / "var"


# --------------------------------------------------------------------------- #
# 分模块配置
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CodexConfig:
    """主脑运行时配置。"""

    model: str = "gpt-5.6-terra"
    sandbox: str = "read_only"  # read_only | workspace_write | full_access
    api_key: str | None = None
    # 主脑的工作目录（agent 的"工作区"）。默认锚定项目根而非 CWD —— 见 project_root()。
    working_dir: Path = field(default_factory=project_root)
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
    media_dir: Path = field(default_factory=lambda: _default_data_dir() / "media")
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
    # MCP 写入失败时的补投队列。归到配置里而不是硬编码在 memory 模块 ——
    # 否则 AGENT_DATA_DIR 改了它却不动，outbox 会散落在 CWD 下（部署时的经典坑）。
    outbox_path: Path = field(
        default_factory=lambda: _default_data_dir() / "memory_outbox.jsonl"
    )
    # MCP server 启动命令。
    #
    # ⚠️ 默认值必须是**当前解释器的绝对路径**，不能是裸 ``"python"``：
    # Debian / Ubuntu 遵循 PEP 394 只提供 ``python3``，裸 ``python`` 常常不存在，
    # 结果是两个 MCP server 全部起不来 —— 而 hub 把子进程启动失败降级成一条 warning，
    # 于是系统"看起来启动成功、工具数却是 0"，识图/STT/TTS/记忆集体失能。
    # 用 sys.executable 还顺带保证子进程跑在同一个 venv 里。
    mcp_command: str = field(default_factory=lambda: sys.executable)
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
    media_dir: Path = field(default_factory=lambda: _default_data_dir() / "media")

    # MCP server 启动命令（自建 media-tools server）。理由同 MemoryConfig.mcp_command。
    mcp_command: str = field(default_factory=lambda: sys.executable)
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
    clone_root: Path = field(default_factory=lambda: _default_data_dir() / "clones")
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
    workdir: Path = field(default_factory=lambda: _default_data_dir() / "exec")
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
    workdir: Path = field(default_factory=lambda: _default_data_dir() / "procs")
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
    """从 .env / 环境变量组装 AppConfig。

    路径解析约定（部署相关，别改回去）：

    * ``env_file`` 是相对路径时，以**项目根**为基准解析，而不是 CWD；
    * ``AGENT_DATA_DIR`` 同理，默认 ``<项目根>/var``；
    * 其余所有 ``*_WORKDIR`` / ``*_PATH`` 相对值也一律以项目根为基准。

    这样 systemd（CWD=``/``）与手动前台运行（CWD=项目根）得到完全一致的配置。
    """
    root = project_root()
    load_dotenv(resolve_from_root(env_file, root=root))

    data_dir = resolve_from_root(env("AGENT_DATA_DIR") or "var", root=root)

    codex = CodexConfig(
        model=env("AGENT_CODEX_MODEL", "gpt-5.6-terra") or "gpt-5.6-terra",
        sandbox=env("AGENT_CODEX_SANDBOX", "read_only") or "read_only",
        api_key=env("OPENAI_API_KEY"),
        working_dir=resolve_from_root(env("AGENT_CODEX_CWD") or ".", root=root),
        approval_mode=env("AGENT_CODEX_APPROVAL_MODE", "deny_all") or "deny_all",
        turn_timeout_sec=env_float("AGENT_TURN_TIMEOUT_SEC", 180.0),
        dry_run=env_bool("AGENT_DRY_RUN", False),
    )

    telegram = TelegramConfig(
        token=env("TELEGRAM_BOT_TOKEN"),
        allowed_chat_ids=frozenset(env_list("TELEGRAM_ALLOWED_CHAT_IDS")),
        media_dir=data_dir / "media",
    )

    # MCP 解释器：默认当前解释器（见 MemoryConfig.mcp_command 的说明），
    # 允许用 AGENT_MCP_COMMAND 覆盖成虚拟环境的绝对路径。
    mcp_command = env("AGENT_MCP_COMMAND") or sys.executable

    memory = MemoryConfig(
        palace_path=resolve_from_root(
            env("MEMPALACE_PALACE_PATH") or _home_mempalace("palace"), root=root
        ),
        kg_path=resolve_from_root(
            env("MEMPALACE_KG_PATH") or _home_mempalace("knowledge_graph.sqlite3"), root=root
        ),
        collection_name=env("MEMPALACE_COLLECTION", "mempalace_drawers") or "mempalace_drawers",
        default_wing=env("AGENT_WING", "wing_agent") or "wing_agent",
        default_room=env("AGENT_ROOM_DEFAULT", "conversation") or "conversation",
        recall_topk=env_int("AGENT_MEMORY_TOPK", 5),
        persist_turns=env_bool("AGENT_PERSIST_TURNS", True),
        extract_facts=env_bool("AGENT_EXTRACT_FACTS", False),
        outbox_path=data_dir / "memory_outbox.jsonl",
        mcp_command=mcp_command,
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
        mcp_command=mcp_command,
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
        workdir=resolve_from_root(
            env("AGENT_EXEC_WORKDIR") or (data_dir / "exec"), root=root
        ),
        timeout_sec=env_float("AGENT_EXEC_TIMEOUT_SEC", 60.0),
        max_output_chars=env_int("AGENT_EXEC_MAX_OUTPUT_CHARS", 4000),
        max_capture_bytes=env_int("AGENT_EXEC_MAX_CAPTURE_BYTES", 131072),
        audit_to_memory=env_bool("AGENT_EXEC_AUDIT_TO_MEMORY", True),
        audit_room=env("AGENT_EXEC_AUDIT_ROOM", "shell-audit") or "shell-audit",
        audit_output_chars=env_int("AGENT_EXEC_AUDIT_OUTPUT_CHARS", 300),
    )

    process_cfg = ProcessConfig(
        enabled=env_bool("AGENT_PROCESS_ENABLED", False),
        workdir=resolve_from_root(
            env("AGENT_PROCESS_WORKDIR") or (data_dir / "procs"), root=root
        ),
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


def _executable_exists(cmd: str) -> bool:
    """判断 MCP 启动命令是否可用：绝对/带分隔符的查文件，裸名字查 PATH。"""
    if not cmd:
        return False
    if Path(cmd).is_absolute() or "/" in cmd or "\\" in cmd:
        return Path(cmd).exists()
    return shutil.which(cmd) is not None


def validate_environment(cfg: AppConfig) -> tuple[list[str], list[str]]:
    """部署环境自检，返回 ``(阻塞性问题, 仅提醒的注意项)``。

    与 :func:`validate_security` 的分工：那个管"配置是否安全"，这个管"环境是否能跑"。
    两者都是 fail-fast 同一目的：把"启动到一半才炸"的问题提前到启动前 ——
    尤其是那类**不会报错、只是静默失能**的失败（MCP 工具数为 0、沙箱 fail-closed），
    它们在日志里往往只有一行 warning，排查成本极高。
    """
    problems: list[str] = []
    notices: list[str] = []

    # 1) 数据目录必须可写 —— systemd 下最常见的失败点（路径相对 CWD）
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        problems.append(
            f"数据目录不可写：{cfg.data_dir}（{exc}）。"
            "请确认目录存在、属主是运行服务的用户；或把 AGENT_DATA_DIR 指到别处。"
        )

    # 2) MCP 解释器必须存在 —— 裸 "python" 在 Debian/Ubuntu 上不存在，会导致"启动成功但零工具"
    for label, cmd in (
        ("记忆 MCP (mempalace)", cfg.memory.mcp_command),
        ("多模态 MCP (media_tools)", cfg.media.mcp_command),
    ):
        if not _executable_exists(cmd):
            problems.append(
                f"{label} 的解释器不存在：{cmd!r}。"
                "请把 AGENT_MCP_COMMAND 设为虚拟环境里 python 的绝对路径。"
            )

    # 3) 记忆库路径的父目录要能创建
    for label, path in (("记忆宫殿 palace", cfg.memory.palace_path), ("知识图谱", cfg.memory.kg_path)):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"{label} 的父目录不可创建：{path.parent}（{exc}）")

    # 4) Linux 沙箱依赖：bwrap 缺失时 Codex 会 fail-closed（agent 活着但不干活）
    if sys.platform.startswith("linux"):
        if shutil.which("bwrap"):
            notices.append("bubblewrap(bwrap) 就绪：Codex 的 Linux 沙箱可正常工作。")
        else:
            notices.append(
                "PATH 上未找到 bwrap。Codex 会回退到自带 bwrap；"
                "但若运行环境禁止 unprivileged user namespace（典型：未加权限的容器），"
                "沙箱会 fail-closed —— agent 将无法执行任何工具调用。建议 apt install bubblewrap。"
            )

    # 5) ffmpeg 可选：缺失时 TTS 降级为发送音频文件（不是语音气泡）
    if not shutil.which(cfg.media.ffmpeg_bin):
        notices.append(
            f"未找到 {cfg.media.ffmpeg_bin}：语音回复会降级为 mp3 音频文件而不是语音气泡（功能不中断）。"
        )

    # 6) 长驻进程只写了 systemd unit 的假设，这里只提醒，不阻塞
    if cfg.process.enabled and not sys.platform.startswith("linux"):
        notices.append(
            "AGENT_PROCESS_ENABLED=true 但当前不是 Linux："
            "长驻进程不受任何沙箱约束，请确保运行在专用账号下。"
        )

    return problems, notices
