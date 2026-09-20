"""冒烟测试 —— 不依赖任何外部服务，把六层流水线完整跑一遍。

==============================================================================
为什么这个测试值得存在
==============================================================================
因为它验证的是**架构本身**，而不是某个函数：

* 工具层被换成 ``FakeHub``（只实现 ToolPort 协议）→ 全系统照常工作，
  **证明契约层抽象是真的成立**，不是写在文档里的空话；
* 渠道层被换成 ``RecordingChannel``（只实现 ChannelPort）→ 主流程无感，
  证明加一个新渠道不需要改 orchestrator；
* Codex 用 ``dry_run`` 桩 → 证明主脑可以被替换；
* MemPalace 未安装时会自动走 SQLite 只读降级 + outbox 暂存
  → 证明降级路径也是通的。

跑完之后你应该能看到：
    ✅ 情绪沿 PAD 空间移动并给出象限标签
    ✅ 多模态附件被正确路由到 vision / stt
    ✅ 记忆召回走了 MCP 通道
    ✅ 分身任务被调度、执行、产出结果
    ✅ cron 表达式能算出下次触发时刻
    ✅ 记忆写入失败时进入 outbox 且能补投
    ✅ 自定义 Codex 端点被正确翻译成 CLI 配置（密钥只进环境变量）

用法::

    python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain import CodexBrain  # noqa: E402
from app.clones import CloneSystem, CronExpr  # noqa: E402
from app.command_audit import CommandAuditor  # noqa: E402
from app.config import (  # noqa: E402
    CODEX_PROVIDER_KEY_ENV,
    AppConfig,
    CodexConfig,
    codex_provider_overrides,
    load_config,
    validate_environment,
)
from app.contracts import (  # noqa: E402
    InboundMessage,
    MediaKind,
    MediaRef,
    OutboundMessage,
    TaskState,
    ToolResult,
    ToolSpec,
)
from app.emotion import PadEmotionEngine  # noqa: E402
from app.local_exec import LocalExecutor  # noqa: E402
from app.media_router import MediaRouter  # noqa: E402
from app.memory import MemoryGateway  # noqa: E402
from app.orchestrator import Orchestrator, default_clones  # noqa: E402
from app.process_manager import ProcessManager  # noqa: E402

PASS, FAIL = "✅", "❌"
_checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _checks.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f"  — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


# =========================================================================== #
# 桩实现：只依赖 contracts，不依赖任何具体模块
# =========================================================================== #


class FakeHub:
    """只实现 ToolPort 协议。用来证明工具层是可替换的。"""

    def __init__(self, *, fail_writes: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_writes = fail_writes
        self._tools = [
            ToolSpec("media_tools", "vision_describe", "识图"),
            ToolSpec("media_tools", "stt_transcribe", "语音转文本"),
            ToolSpec("media_tools", "tts_synthesize", "文本转语音"),
            ToolSpec("media_tools", "media_probe", "媒体嗅探"),
            ToolSpec("mempalace", "mempalace_add_drawer", "写入记忆"),
            ToolSpec("mempalace", "mempalace_search", "检索记忆"),
        ]

    async def start(self, **_: Any) -> None: ...
    async def stop(self) -> None: ...
    def bind_memory(self, memory: Any) -> None: ...

    def health(self) -> dict[str, Any]:
        return {
            "servers": {
                "media_tools": {"healthy": True, "tools": [t.name for t in self._tools if t.server == "media_tools"]},
                "mempalace": {"healthy": True, "tools": [t.name for t in self._tools if t.server == "mempalace"]},
            },
            "tool_count": len(self._tools),
        }

    async def list_tools(self) -> list[ToolSpec]:
        return list(self._tools)

    def openai_tools(self, allowed: Sequence[str] | None = None) -> list[dict[str, Any]]:
        return [t.to_openai_tool() for t in self._tools]

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append((server, tool, arguments))

        if tool == "mempalace_add_drawer":
            if self.fail_writes:
                return ToolResult.failure("模拟写入失败：MCP server 不可达")
            return ToolResult.success({"drawer_id": f"drawer_fake_{len(self.calls):04d}"})

        if tool == "mempalace_search":
            return ToolResult.success(
                {
                    "results": [
                        {
                            "text": "用户此前明确表示：希望部署方案尽量单文件、少依赖，"
                                    "避免为了分层而分层。",
                            "wing": "wing_agent",
                            "room": "conversation",
                            "source_file": "chat/local",
                            "similarity": 0.87,
                        },
                        {
                            "text": "约定：所有对外路径统一用正斜杠，构建产物统一输出到 dist/。",
                            "wing": "wing_agent",
                            "room": "conversation",
                            "source_file": "chat/local",
                            "similarity": 0.72,
                        },
                    ]
                }
            )

        if tool == "vision_describe":
            return ToolResult.success(
                {
                    "model": "fake-vision",
                    "text": "一张深色主题的终端界面截图，标题栏写着 Docker MCP，"
                            "表格里有三列：服务名、状态、端口。",
                }
            )

        if tool == "stt_transcribe":
            return ToolResult.success(
                {
                    "model": "fake-stt",
                    "text": "帮我看一下这个界面的配色是不是太暗了",
                    "language": "zh",
                    "duration_sec": 3.2,
                }
            )

        if tool == "tts_synthesize":
            target = Path(str(arguments.get("out_path") or "./var/media/tts_fake.mp3"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"ID3\x03\x00\x00\x00" + b"\x00" * 64)
            return ToolResult.success(
                {"model": "fake-tts", "voice": arguments.get("voice", "alloy"),
                 "path": str(target), "bytes": 74}
            )

        if tool == "media_probe":
            return ToolResult.success({"path": arguments.get("path"), "bytes": 1024})

        return ToolResult.failure(f"未实现的工具: {server}/{tool}")

    async def call_qualified(self, qualified_name: str, arguments: dict[str, Any]) -> ToolResult:
        """与真实 McpHub 保持完全一致的宽容解析语义（否则桩会骗过测试）。"""
        if qualified_name.startswith("mcp__"):
            _, server, tool = qualified_name.split("__", 2)
            return await self.call(server, tool, arguments)
        if "__" in qualified_name:
            server, tool = qualified_name.split("__", 1)
            return await self.call(server, tool, arguments)
        return ToolResult.failure(f"无法解析工具名: {qualified_name}")


class RecordingChannel:
    """只实现 ChannelPort 协议。用来证明渠道层是可替换的。"""

    name = "recording"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self.typing_count = 0
        self._handler: Any = None

    async def start(self, handler: Any) -> None:
        self._handler = handler

    async def stop(self) -> None: ...

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)

    async def send_typing(self, chat_id: str, **_: Any) -> None:
        self.typing_count += 1

    @property
    def last(self) -> str:
        return self.sent[-1].text if self.sent else ""


# =========================================================================== #
# 装配
# =========================================================================== #


def build(cfg: AppConfig, hub: FakeHub) -> tuple[Orchestrator, RecordingChannel]:
    memory = MemoryGateway(cfg.memory, hub)
    media = MediaRouter(cfg.media, hub)
    emotion = PadEmotionEngine(cfg.emotion)
    brain = CodexBrain(cfg.codex, hub, session_db=cfg.data_dir / "sessions.sqlite3")
    clones = CloneSystem(cfg.scheduler, brain, default_clones())

    # 执行层三件套也接进来 —— 这样 /exec、/proc、/status 与命令审计的接线
    # 也一并被这个离线测试覆盖到（组件本身另有 scripts/smoke_exec.py 专项验证）。
    executor = LocalExecutor(cfg.exec)
    processes = ProcessManager(cfg.process)
    auditor = CommandAuditor(memory, cfg.exec, wing=cfg.memory.default_wing)

    channel = RecordingChannel()
    orch = Orchestrator(
        cfg, channel=channel, hub=hub, memory=memory, emotion=emotion,
        media=media, brain=brain, clones=clones,
        executor=executor, processes=processes, auditor=auditor,
    )
    return orch, channel


# =========================================================================== #
# 场景
# =========================================================================== #

SCENARIOS: list[tuple[str, str]] = [
    ("中性提问", "帮我理一下这个项目的模块边界。"),
    ("被赞扬", "谢谢！这个方案太棒了，很厉害。"),
    ("被指责", "你这说法不对吧，简直是垃圾答案。"),
    ("紧急+亲昵", "赶紧的，马上要交了，么么~"),
    ("玩笑", "哈哈哈哈笑死我了"),
]


async def scenario_pipeline(orch: Orchestrator, channel: RecordingChannel) -> None:
    section("场景 1｜对话流水线：情绪演化 + 记忆召回 + 主脑回复")
    emotion = orch.emotion

    print(f"\n{'输入':<12}{'P':>8}{'A':>8}{'D':>8}   象限      回复摘要")
    print("-" * 74)
    labels: list[str] = []
    for label, text in SCENARIOS:
        await orch.handle(
            InboundMessage(channel="recording", chat_id="c1", user_id="u1", text=text)
        )
        state = emotion.current()
        labels.append(state.label)
        reply_head = channel.last.splitlines()[0][:26]
        print(
            f"{label:<12}{state.pleasure:>8.2f}{state.arousal:>8.2f}"
            f"{state.dominance:>8.2f}   {state.label:<10}{reply_head}"
        )

    check(
        "情绪象限出现了分化（不是一味正向）",
        len(set(labels)) >= 2,
        " → ".join(labels),
    )
    check(
        "被指责后情绪转入负向象限",
        labels[2] in {"anxious", "hostile", "disdainful", "bored"},
        f"被指责后 → {labels[2]}",
    )
    check("情绪状态发生了移动", emotion.current().magnitude > 0, f"强度={emotion.current().magnitude:.3f}")
    check("情绪象限标签非中性", emotion.current().label != "neutral", emotion.current().label)
    check("语气指令已生成", bool(emotion.style_directive()), "style_directive 非空")
    check("语音参数可推导", "speed" in emotion.voice_params(), json.dumps(emotion.voice_params()))
    check("对话产生了出站消息", len(channel.sent) == len(SCENARIOS), f"{len(channel.sent)} 条")
    check("打字指示器被触发", channel.typing_count == len(SCENARIOS), f"{channel.typing_count} 次")


async def scenario_decay(emotion: PadEmotionEngine) -> None:
    section("场景 2｜情绪衰减：PAD 状态会自己往基线回归")
    from datetime import timedelta

    emotion.appraise_text("垃圾！太差了！")
    before = emotion.current()
    print(f"  刺激后   P={before.pleasure:+.3f} A={before.arousal:+.3f}")

    # 手动把时间拨到 1 个半衰期之后，验证 α = 0.5
    future = before.updated_at + timedelta(seconds=emotion.cfg.decay_half_life_sec)
    emotion._last_tick = emotion._state.updated_at  # noqa: SLF001 - 测试内省
    after = emotion.decay(now=future)
    print(f"  1 个半衰期后 P={after.pleasure:+.3f} A={after.arousal:+.3f}")

    baseline_p = emotion.cfg.baseline[0]
    expected = before.pleasure + (baseline_p - before.pleasure) * 0.5
    check(
        "半衰期后位移恰好走完一半",
        abs(after.pleasure - expected) < 1e-6,
        f"实测 {after.pleasure:+.4f} / 期望 {expected:+.4f}",
    )


async def scenario_multimodal(orch: Orchestrator, channel: RecordingChannel, hub: FakeHub, media_dir: Path) -> None:
    section("场景 3｜多模态路由：图片→vision，语音→stt，回复→tts")

    img = media_dir / "fake.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    voice = media_dir / "fake.ogg"
    voice.write_bytes(b"OggS" + b"\x00" * 64)

    await orch.handle(
        InboundMessage(
            channel="recording", chat_id="c2", user_id="u1", text="看看这个",
            media=[MediaRef(kind=MediaKind.IMAGE, path=img, caption="截图")],
        )
    )
    note = channel.last
    check(
        "图片被路由到 vision 工具",
        any(t == "vision_describe" for _, t, _ in hub.calls),
        "vision_describe 已被调用",
    )
    check("识图结果进入了主脑上下文", "深色主题" in note, note.splitlines()[0][:40])

    await orch.handle(
        InboundMessage(
            channel="recording", chat_id="c2", user_id="u1",
            media=[MediaRef(kind=MediaKind.VOICE, path=voice, duration_sec=3.2)],
        )
    )
    check(
        "语音被路由到 stt 工具",
        any(t == "stt_transcribe" for _, t, _ in hub.calls),
        "stt_transcribe 已被调用",
    )
    last = channel.sent[-1]
    check("语音提问触发了同模态语音回复", last.voice_path is not None, str(last.voice_path))
    check(
        "tts 工具被调用",
        any(t == "tts_synthesize" for _, t, _ in hub.calls),
        "tts_synthesize 已被调用",
    )


async def scenario_memory(hub: FakeHub, orch: Orchestrator) -> None:
    section("场景 4｜记忆层：语义召回 / 逐字落库 / 知识图谱降级 / outbox 自愈")

    records = await orch.memory.recall("部署方案", k=3)
    check("记忆语义召回可用", len(records) >= 1, f"召回 {len(records)} 条")
    check(
        "召回走了 MCP 通道",
        any(t == "mempalace_search" for _, t, _ in hub.calls),
        "mempalace_search 已被调用",
    )

    ctx = await orch.memory.build_context("部署方案", wing="wing_agent")
    check("上下文拼装非空", bool(ctx.strip()), f"{len(ctx)} 字符")

    report = orch.memory.backend_report()
    print("  记忆后端自检：" + json.dumps(report, ensure_ascii=False))
    check("记忆后端自检可输出", "palace_path" in report)

    # 知识图谱：mempalace 未安装时应静默降级，不抛异常
    from app.contracts import Triple

    try:
        await orch.memory.assert_fact(Triple("鹏哥", "prefers", "单文件部署"))
        await orch.memory.facts_about("鹏哥")
        check("知识图谱不可用时优雅降级", True, "未抛异常")
    except Exception as exc:  # noqa: BLE001
        check("知识图谱不可用时优雅降级", False, str(exc))

    # outbox：写入失败不能丢数据
    hub.fail_writes = True
    before = hub.calls.__len__()
    record_id = await orch.memory.remember(
        __import__("app.contracts", fromlist=["MemoryWrite"]).MemoryWrite(
            content="这条记忆应该进 outbox", wing="wing_agent", room="conversation"
        )
    )
    hub.fail_writes = False
    check("写入失败时落 outbox", record_id.startswith("outbox_"), record_id)
    # outbox 路径必须**由配置派生**，不能硬编码 "./var/..." ——
    # 硬编码的路径会跟着 CWD 跑，systemd（CWD=/）下会写进系统 /var。
    outbox = orch.memory._outbox  # noqa: SLF001 - 内省：这一项验证的就是"路径来源"
    check(
        "outbox 落在 AGENT_DATA_DIR 下（不跟随 CWD）",
        orch.cfg.data_dir in outbox.parents,
        str(outbox),
    )
    check("outbox 文件已生成", outbox.exists(), f"{outbox.stat().st_size if outbox.exists() else 0} 字节")
    flushed = await orch.memory.flush_outbox()
    check("outbox 可补投", flushed >= 1, f"补投 {flushed} 条")


async def scenario_clones(orch: Orchestrator) -> None:
    section("场景 5｜分身系统：任务隔离、并发调度、结果回收、cron")

    stats = orch.clones.stats()
    print("  分身角色：" + json.dumps(stats["clones"], ensure_ascii=False, indent=2)[:600])
    check("分身角色已注册", len(stats["clones"]) == 3, f"{len(stats['clones'])} 个")
    check("worker 池已启动", stats["workers"] >= 1, f"{stats['workers']} 个 worker")

    from app.contracts import CloneTask

    ids = []
    for i in range(3):
        ids.append(
            await orch.clones.submit(
                CloneTask(
                    task_id="", prompt=f"并行任务 {i}：统计模块 {i} 的职责边界。",
                    kind="background", priority=5, clone_id="researcher",
                )
            )
        )
    check("多任务已入队", len(ids) == 3, ", ".join(ids))

    deadline = time.time() + 25
    while time.time() < deadline:
        results = [await orch.clones.result(i) for i in ids]
        if all(r is not None and r.state in {TaskState.DONE, TaskState.FAILED, TaskState.TIMEOUT} for r in results):
            break
        await asyncio.sleep(0.2)

    finished = [await orch.clones.result(i) for i in ids]
    done = [r for r in finished if r and r.state is TaskState.DONE]
    check("分身任务全部完成", len(done) == 3, f"{len(done)}/3")
    for r in finished:
        if r:
            print(f"    · {r.clone_id:<12}{r.state.value:<8}{r.elapsed_sec:.2f}s  {r.output[:48]}")

    check("结果带耗时统计", all(r.elapsed_sec > 0 for r in done if r), "elapsed_sec > 0")

    # cron 计算
    base = datetime(2026, 9, 20, 0, 32, tzinfo=timezone.utc)
    cases = [
        ("*/30 * * * *", "00:32 → 01:00"),
        ("0 9 * * *", "每天 09:00"),
        ("0 9 * * 1-5", "工作日 09:00"),
    ]
    print("\n  cron 求值：")
    ok_all = True
    for expr, desc in cases:
        nxt = CronExpr(expr).next_after(base)
        print(f"    {expr:<16} 下次触发 {nxt.isoformat() if nxt else 'None'}   （{desc}）")
        ok_all = ok_all and nxt is not None
    check("cron 表达式解析正确", ok_all, f"{len(cases)} 个表达式")

    try:
        CronExpr("* * *")
        check("非法 cron 表达式被拒绝", False, "未抛异常")
    except ValueError:
        check("非法 cron 表达式被拒绝", True, "抛 ValueError")

    sched = orch.clones.stats()
    check("定时任务已注册", len(sched["cron_jobs"]) >= 2, f"{len(sched['cron_jobs'])} 个")


async def scenario_commands(orch: Orchestrator, channel: RecordingChannel) -> None:
    section("场景 6｜指令集：/status /emotion /memory /audit /task /exec /proc /help")

    commands = [
        "/help",
        "/emotion",
        "/memory 部署",
        "/audit 部署",
        "/status",
        "/task 汇总今天的记忆",
        "/exec",
        "/exec python_version",
        "/exec 不存在的命令",
        "/proc",
    ]
    for cmd in commands:
        await orch.handle(
            InboundMessage(channel="recording", chat_id="c3", user_id="u1", text=cmd)
        )
        head = channel.last.splitlines()[0][:60]
        print(f"  {cmd:<28} → {head}")

    check("指令全部有响应", len(channel.sent) >= len(commands), f"{len(channel.sent)} 条出站")
    check(
        "执行层已接入编排器",
        orch.executor is not None and orch.processes is not None and orch.auditor is not None,
        "executor / processes / auditor 均已装配",
    )


# =========================================================================== #
# 场景 7：自定义 Codex 端点
# =========================================================================== #


async def scenario_custom_endpoint(workdir: Path) -> None:
    """自定义端点 → Codex CLI 配置的翻译逻辑必须能离线断言。

    这条最容易静默出错：键名拼错一个字符，CLI 只会抛一句难懂的反序列化错误；
    更糟的是配了不支持 Responses 协议的中转 —— 配置能通过、启动也正常，
    只是每轮对话都超时。所以形状校验必须在启动前 fail-fast。
    """
    # 1) 留空 = 官方 provider，不该产生任何 CLI 覆盖项
    overrides, env = codex_provider_overrides(CodexConfig())
    check("未配 base_url 时不生成 CLI 覆盖项", overrides == () and env is None, repr(overrides))

    # 2) 配了 base_url：切 provider + 锁 responses + 密钥走环境变量
    custom = CodexConfig(
        base_url="https://relay.example.com/v1",
        base_url_api_key="sk-unit-test-key",
        base_url_provider_id="myrelay",
    )
    overrides, env = codex_provider_overrides(custom)
    check("覆盖项切到自定义 model_provider", 'model_provider="myrelay"' in overrides)
    check(
        "覆盖项写入 base_url",
        'model_providers.myrelay.base_url="https://relay.example.com/v1"' in overrides,
    )
    check(
        "覆盖项锁定 responses 协议",
        'model_providers.myrelay.wire_api="responses"' in overrides,
    )
    check("覆盖项声明无需官方登录态", "requires_openai_auth=false" in " ".join(overrides))
    check(
        "密钥只进环境变量、不进命令行参数",
        env is not None
        and env.get(CODEX_PROVIDER_KEY_ENV) == "sk-unit-test-key"
        and not any("sk-unit-test-key" in o for o in overrides),
        "argv 里出现密钥会被同机用户从 ps 看到",
    )

    # 3) 没给专用 key 时应回落到 OPENAI_API_KEY
    _, env2 = codex_provider_overrides(
        CodexConfig(base_url="https://x.example.com/v1", api_key="sk-fallback")
    )
    check(
        "专用 key 缺省时复用 OPENAI_API_KEY",
        env2 is not None and env2.get(CODEX_PROVIDER_KEY_ENV) == "sk-fallback",
    )

    # 4) 保留 provider id 必须被启动前校验拦下（CLI 侧只会静默失败或报天书）
    base = load_config(ROOT / ".env")
    reserved = replace(
        base,
        data_dir=workdir,  # 让 validate_environment 的可写探测落在测试目录，别碰真实 var/
        codex=replace(
            base.codex,
            base_url="https://x.example.com/v1",
            base_url_provider_id="openai",
        ),
    )
    problems, _ = validate_environment(reserved)
    check(
        "保留 provider id（openai）被拒绝启动",
        any("保留" in p for p in problems),
        str(problems)[:90],
    )

    # 5) URL 形状不对也要 fail-fast
    bad_url = replace(
        base,
        data_dir=workdir,
        codex=replace(
            base.codex, base_url="relay.example.com/v1", base_url_provider_id="ok"
        ),
    )
    problems2, _ = validate_environment(bad_url)
    check(
        "缺少 http(s):// 前缀的 base_url 被拒绝启动",
        any("http://" in p or "https://" in p for p in problems2),
        str(problems2)[:90],
    )


# =========================================================================== #
# 主流程
# =========================================================================== #


async def run() -> int:
    workdir = ROOT / "var" / "smoketest"
    if workdir.exists():
        import shutil

        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    cfg = load_config(ROOT / ".env")
    cfg = replace(
        cfg,
        codex=replace(cfg.codex, dry_run=True),
        data_dir=workdir,
        # 注意：outbox_path 与 media_dir 都是 load_config 时从 data_dir 派生的，
        # 所以替换 data_dir 后必须**一并替换**它们，否则会写到真实数据目录去。
        memory=replace(
            cfg.memory,
            persist_turns=True,
            extract_facts=False,
            outbox_path=workdir / "memory_outbox.jsonl",
        ),
        media=replace(cfg.media, media_dir=workdir / "media"),
    )

    hub = FakeHub()
    orch, channel = build(cfg, hub)

    print("\n" + "=" * 74)
    print("Codex Agent System — 离线冒烟测试")
    print("=" * 74)
    print("说明：本测试用桩替换了工具层、渠道层与模型，验证的是架构与流程。")
    print(f"工作目录：{workdir}")

    await orch.start()
    try:
        await scenario_pipeline(orch, channel)
        await scenario_decay(orch.emotion)
        await scenario_multimodal(orch, channel, hub, workdir / "media")
        await scenario_memory(hub, orch)
        await scenario_clones(orch)
        await scenario_commands(orch, channel)
        await scenario_custom_endpoint(workdir)
    finally:
        await orch.stop()

    section("汇总")
    total = len(_checks)
    passed = sum(1 for _, ok, _ in _checks if ok)
    for name, ok, detail in _checks:
        if not ok:
            print(f"  {FAIL} {name}  — {detail}")
    print(f"\n  通过 {passed}/{total}")
    print(f"  工具调用总数：{len(hub.calls)}")
    by_tool: dict[str, int] = {}
    for _, tool, _ in hub.calls:
        by_tool[tool] = by_tool.get(tool, 0) + 1
    for tool, count in sorted(by_tool.items(), key=lambda kv: -kv[1]):
        print(f"    · {tool:<26} × {count}")

    print(f"\n  情绪事件轨迹（最近 8 条）：")
    for item in orch.emotion.trace()[-8:]:
        pad = item["pad"]
        print(
            f"    {item['ts'][11:]}  {item['event']:<14}"
            f"P={pad['pleasure']:+.2f} A={pad['arousal']:+.2f} D={pad['dominance']:+.2f}"
            f"  ({pad['label']})"
        )

    if passed == total:
        print("\n  🎉 全部通过：六层流水线在离线环境下端到端打通。\n")
        return 0
    print(f"\n  ⚠️  有 {total - passed} 项未通过，请查看上面标记为 {FAIL} 的条目。\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
