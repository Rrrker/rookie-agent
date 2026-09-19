"""执行层专项测试 —— 覆盖"四个缺口"的实现。

不依赖 Telegram / Codex / MemPalace：
* 用**自定义命令注册表**替代白名单（这样能安全地测超时、输出溢出、二进制、编码）；
* 用**假 MemoryPort** 验证命令审计落库与脱敏；
* 用真实的 ``ProcessManager`` 起一个真进程，验证 start/logs/stop 全链路。

跑完你会看到四个缺口各自的验证结果：

1. ``approval_mode`` 真的传给了 SDK（枚举解析 + 未知值回退）
2. Codex 跑过的命令能被抽出来、脱敏、写进记忆
3. 长驻进程能 start / logs / stop，且默认关闭、高危命令被拦
4. 内部白名单命令能跑（非模型消费方的执行入口）

用法::

    python scripts/smoke_exec.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain import CodexBrain  # noqa: E402
from app.command_audit import (  # noqa: E402
    CommandAuditor,
    extract_commands,
    extract_file_changes,
    redact,
    render_traces,
)
from app.config import (  # noqa: E402
    CodexConfig,
    ExecConfig,
    ProcessConfig,
    TelegramConfig,
    load_config,
    validate_security,
)
from app.contracts import CommandTrace, MemoryWrite  # noqa: E402
from app.local_exec import (  # noqa: E402
    LocalExecutor,
    NamedCommand,
    clip,
    decode_output,
    strip_ansi,
    summarize,
)
from app.process_manager import ProcessManager  # noqa: E402

PASS, FAIL = "✅", "❌"
_checks: list[tuple[str, bool, str]] = []

WORKDIR = ROOT / "var" / "exec-smoketest"


def check(name: str, ok: bool, detail: str = "") -> None:
    _checks.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f"  — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def value_of(x: Any) -> Any:
    return getattr(x, "value", x)


# =========================================================================== #
# 桩
# =========================================================================== #


class FakeMemory:
    """最小 MemoryPort 实现，只用于验证审计写入与脱敏。"""

    def __init__(self) -> None:
        self.writes: list[MemoryWrite] = []

    async def remember(self, write: MemoryWrite) -> str:
        self.writes.append(write)
        return f"drawer_fake_{len(self.writes):03d}"

    async def recall(self, query: str, **_: Any) -> list[Any]:
        return []


class _Status:
    """模拟 str-Enum（有 .value，本身也是 str 子类）。"""

    def __init__(self, v: str) -> None:
        self.value = v

    def __str__(self) -> str:  # pragma: no cover - 仅便于打印
        return self.value


class _Item:
    """模拟 pydantic 变体对象 / RootModel 外壳。"""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def cmd_item(
    command: str,
    *,
    status: str = "completed",
    exit_code: int | None = 0,
    output: str = "hello",
    cwd: str = "/workspace",
    duration_ms: int | None = 42,
    wrapped: bool = True,
) -> _Item:
    item = _Item(
        type="commandExecution",
        id="item_cmd",
        command=command,
        cwd=_Item(root=cwd),
        status=_Status(status),
        exit_code=exit_code,
        aggregated_output=output,
        duration_ms=duration_ms,
    )
    return _Item(root=item) if wrapped else item


def msg_item(text: str) -> _Item:
    return _Item(root=_Item(type="agentMessage", id="item_msg", text=text))


def file_item(path: str, kind: str = "update") -> _Item:
    return _Item(
        root=_Item(type="fileChange", id="item_file", status=_Status("completed"),
                   changes=[_Item(path=_Item(root=path), kind=_Status(kind))])
    )


def make_exec_cfg(**over: Any) -> ExecConfig:
    base = ExecConfig(
        enabled=True,
        workdir=WORKDIR / "exec",
        timeout_sec=20.0,
        max_output_chars=2000,
        max_capture_bytes=65536,
    )
    return replace(base, **over) if over else base


def test_commands() -> dict[str, NamedCommand]:
    """测试专用的命令注册表。

    刻意包含"输出巨大""会挂住""输出二进制""输出 ANSI"这几类 ——
    它们只能通过自定义注册表安全地构造出来。
    """
    py = sys.executable
    items = (
        NamedCommand("echo_ok", (py, "-c", "print('hello-exec')"), "正常输出"),
        NamedCommand("flood", (py, "-c", "print('x' * 200000)"), "输出巨大"),
        NamedCommand("hang", (py, "-c", "import time;time.sleep(30)"), "会挂住", timeout_sec=1.5),
        NamedCommand(
            "ansi", (py, "-c", "import sys;sys.stdout.write(chr(27)+'[31mred'+chr(27)+'[0m')"),
            "输出 ANSI",
        ),
        NamedCommand(
            "binary", (py, "-c", "import sys;sys.stdout.buffer.write(bytes(64));sys.stdout.flush()"),
            "输出二进制",
        ),
        NamedCommand(
            "gbk", (py, "-c", "import sys;sys.stdout.buffer.write(bytes([0xd6,0xd0,0xce,0xc4]))"),
            "GBK 中文输出",
        ),
        NamedCommand("missing", ("definitely-not-a-real-binary-xyz",), "程序不存在"),
    )
    return {i.name: i for i in items}


# =========================================================================== #
# 场景
# =========================================================================== #


def s1_capture() -> None:
    section("场景 1｜输出捕获：ANSI / 编码 / 截断 / 文本化")
    check("剥离 ANSI", strip_ansi("\x1b[31mred\x1b[0m ok") == "red ok")
    check("剥离 OSC 序列", strip_ansi("\x1b]0;title\x07done") == "done")

    check("UTF-8 解码", decode_output("中文".encode("utf-8")) == "中文")
    check("GBK 兜底解码", decode_output(bytes([0xd6, 0xd0, 0xce, 0xc4])) == "中文")
    check("UTF-16 BOM 转码", decode_output("中".encode("utf-16")) == "中")
    check("空输入", decode_output(b"") == "")

    text, cut = clip("a" * 100, 10)
    check("按字符截断并标注", cut and "已截断" in text and len(text) < 100)
    text2, cut2 = clip("short", 10)
    check("未超限不截断", (not cut2) and text2 == "short")


async def s2_executor() -> None:
    section("场景 2｜内部白名单执行：正常 / 未登记 / 关闭 / 不存在 / 超时 / 溢出")
    ex = LocalExecutor(make_exec_cfg(), test_commands())

    r = await ex.run_named("echo_ok")
    check("正常命令成功", r.ok and r.exit_code == 0, f"exit={r.exit_code}")
    check("stdout 正确捕获", "hello-exec" in r.stdout, r.stdout.strip()[:40])
    check("记录了耗时", r.duration_ms >= 0, f"{r.duration_ms}ms")

    r = await ex.run_named("nope")
    check("未登记命令被拒", (not r.ok) and r.error_code == "unknown_command", str(r.error_code))

    off = LocalExecutor(replace(make_exec_cfg(), enabled=False), test_commands())
    r = await off.run_named("echo_ok")
    check("关闭时不执行", (not r.ok) and r.error_code == "disabled")

    r = await ex.run_named("missing")
    check("程序不存在单独归因", (not r.ok) and r.error_code == "not_found", str(r.error_code))

    r = await ex.run_named("hang")
    check("超时被识别", (not r.ok) and r.error_code == "timeout", f"{r.duration_ms}ms")
    check("超时耗时接近上限", r.duration_ms < 15000, f"{r.duration_ms}ms")

    r = await LocalExecutor(make_exec_cfg(max_capture_bytes=4096), test_commands()).run_named("flood")
    check("输出溢出被识别", (not r.ok) and r.error_code == "output_overflow", str(r.error_code))

    r = await ex.run_named("ansi")
    check("stdout 中的 ANSI 被剥离", "red" in r.stdout and "\x1b" not in r.stdout, repr(r.stdout[:30]))

    r = await ex.run_named("binary")
    check("二进制输出被摘要化", "二进制输出" in r.stdout, r.stdout.strip()[:40])

    r = await ex.run_named("gbk")
    check("GBK 中文正确解码", "中文" in r.stdout, r.stdout.strip()[:20])

    summary = summarize(await ex.run_named("echo_ok"), head_lines=3)
    check("summarize 可读", "$" in summary and "exit=" in summary, summary.splitlines()[0][:50])


async def s3_processes() -> None:
    section("场景 3｜长驻进程：默认关闭 / start / logs / stop / 上限 / 高危拦截")

    cfg_off = ProcessConfig(enabled=False, workdir=WORKDIR / "proc-off")
    mgr_off = ProcessManager(cfg_off)
    info = await mgr_off.start("x", [sys.executable, "-c", "print(1)"])
    check("默认关闭时拒绝启动", info.error is not None and "未启用" in info.error, str(info.error))

    cfg = ProcessConfig(
        enabled=True, workdir=WORKDIR / "proc", max_processes=2, grace_sec=3.0, max_log_bytes=65536
    )
    mgr = ProcessManager(cfg)

    prog = "import time,sys\nfor i in range(200):\n    print('tick', i, flush=True)\n    time.sleep(0.2)\n"
    info = await mgr.start("ticker", [sys.executable, "-c", prog])
    check("进程启动成功", info.running and info.pid, f"pid={info.pid}")

    await asyncio.sleep(1.5)
    logs = await mgr.logs("ticker", tail_lines=20)
    check("日志被后台读取", "tick" in logs, logs.splitlines()[-1][:40] if logs else "空")

    info2 = await mgr.start("ticker", [sys.executable, "-c", "print(1)"])
    check("同名重复启动被拒", info2.error is not None and "已在运行" in info2.error, str(info2.error))

    info_bad = await mgr.start("bad", ["mkfs"])
    check("高危程序被拦", info_bad.error is not None and "拒绝" in info_bad.error, str(info_bad.error))

    # 必须用**第二个长驻进程**才能验证上限：短命进程可能在检查前就已退出
    blocker = await mgr.start("blocker", [sys.executable, "-c", "import time;time.sleep(60)"])
    check("第二个受管进程可启动", blocker.running, f"pid={blocker.pid}")

    info3 = await mgr.start("third", [sys.executable, "-c", "print(1)"])
    check("并发上限生效", info3.error is not None and "上限" in info3.error, str(info3.error))

    stopped = await mgr.stop("ticker")
    check("stop 后不再运行", stopped is not None and not stopped.running, str(stopped.exit_code))
    check("stop 回填退出码", stopped is not None and stopped.exit_code is not None)

    listed = mgr.list()
    check("list 只含成功启动的进程", len(listed) == 2, f"{len(listed)} 个")
    check("logs 对不存在的进程有提示", "没有名为" in await mgr.logs("ghost"))

    n = await mgr.stop_all()
    check("stop_all 清空运行中的进程", n == 1, f"停掉 {n} 个")
    check("stats 结构完整", {"enabled", "running", "processes"} <= set(mgr.stats()))
    check(
        "render 可读",
        "pid=" in ProcessManager.render(listed[0]) or "已退出" in ProcessManager.render(listed[0]),
    )


def s4_audit() -> None:
    section("场景 4｜命令审计：抽取 / 脱敏 / 落库")

    items = [
        msg_item("我先看看"),
        cmd_item("pytest -q"),
        cmd_item("git status --short", exit_code=0),
        cmd_item("rm -rf build", status="declined", exit_code=None),
        file_item("app/brain.py"),
        file_item("README.md", kind="add"),
    ]
    traces = extract_commands(items)
    check("从 items 抽出命令条目", len(traces) == 3, f"{len(traces)} 条")
    check("非命令条目被忽略", all("我先看看" not in t.command for t in traces))
    check("命令字段正确", traces[0].command == "pytest -q", traces[0].command)
    check("cwd 解包正确（RootModel 已剥壳）", traces[0].cwd == "/workspace", str(traces[0].cwd))
    check("exit_code 正确", traces[1].exit_code == 0, str(traces[1].exit_code))
    check("declined 状态被识别", traces[2].declined and not traces[2].succeeded, traces[2].status)
    check("duration_ms 正确", traces[0].duration_ms == 42, str(traces[0].duration_ms))

    changes = extract_file_changes(items)
    check("文件改动被抽出", changes == ["update:app/brain.py", "add:README.md"], str(changes))

    check("脱敏 sk-", "sk-***" in redact("curl -H 'x: sk-abcdefgh12345'"))
    check("脱敏 Bearer", "***" in redact("Authorization: Bearer abcdefgh12345"))
    check("脱敏 token=", "***" in redact("--token=supersecretvalue"), redact("--token=supersecretvalue"))
    check("普通文本不被误伤", redact("git status --short") == "git status --short")

    rendered = render_traces(traces)
    check("渲染含命令与退出码", "pytest -q" in rendered and "exit=" in rendered)


def _sample_audit_payload() -> tuple[list[CommandTrace], list[str]]:
    """构造带凭据的命令，用来验证"落库前一定脱敏"。"""
    items = [
        cmd_item("curl -H 'Authorization: Bearer abcdefgh1234567' https://api.example.com"),
        cmd_item("export API_KEY=sk-abcdefgh12345678 && python train.py", exit_code=1),
    ]
    return extract_commands(items), extract_file_changes(items)


async def s4b_persist() -> None:
    section("场景 4b｜审计落库：写入记忆 / 落库前脱敏 / 可关闭")

    traces, changes = _sample_audit_payload()
    check("带凭据的命令被抽出", len(traces) == 2, f"{len(traces)} 条")

    memory = FakeMemory()
    auditor = CommandAuditor(memory, make_exec_cfg(), wing="wing_test")
    written = await auditor.record(
        session_id="c1", traces=traces, file_changes=changes, note="用户要求跑训练"
    )
    check("写入一条审计 drawer", written == 1 and len(memory.writes) == 1, f"{len(memory.writes)} 条")

    first = memory.writes[0] if memory.writes else None
    check(
        "wing / room 正确",
        bool(first) and first.wing == "wing_test" and first.room == "shell-audit",
    )
    content = first.content if first else ""
    check("落库前已脱敏（Bearer）", "abcdefgh1234567" not in content, content[:60])
    check("落库前已脱敏（sk-）", "sk-abcdefgh12345678" not in content)
    check("存在脱敏占位符", "***" in content)
    check("仍保留命令轮廓便于检索", "curl" in content and "train.py" in content)

    silent = FakeMemory()
    off = CommandAuditor(silent, make_exec_cfg(audit_to_memory=False), wing="wing_test")
    n = await off.record(session_id="c1", traces=traces)
    check("可关闭审计", n == 0 and not silent.writes)
    check("enabled 跟随配置", off.enabled is False and auditor.enabled is True)

    empty = FakeMemory()
    n2 = await CommandAuditor(empty, make_exec_cfg(), wing="w").record(session_id="c1", traces=[])
    check("空 trace 不写库", n2 == 0 and not empty.writes)


def s5_approval_and_security() -> None:
    section("场景 5｜approval_mode 接线 + 启动前安全校验")

    tmp = WORKDIR / "sessions.sqlite3"
    tmp.parent.mkdir(parents=True, exist_ok=True)

    brain = CodexBrain(replace(CodexConfig(), approval_mode="deny_all"), None, session_db=tmp)
    check("deny_all 解析成功", value_of(brain._approval()) == "deny_all", str(value_of(brain._approval())))

    brain2 = CodexBrain(replace(CodexConfig(), approval_mode="auto_review"), None, session_db=tmp)
    check("auto_review 解析成功", value_of(brain2._approval()) == "auto_review")

    brain3 = CodexBrain(replace(CodexConfig(), approval_mode="never"), None, session_db=tmp)
    got = value_of(brain3._approval())
    check("未知值回退到最保守的 deny_all", got in {"deny_all", None}, f"得到 {got!r}")

    base = load_config(ROOT / ".env")

    cfg_risky = replace(
        base,
        telegram=TelegramConfig(token="123:abc", allowed_chat_ids=frozenset()),
        exec=make_exec_cfg(),
        process=ProcessConfig(enabled=False, workdir=WORKDIR / "p"),
    )
    problems, _ = validate_security(cfg_risky)
    check("空 Telegram 白名单 + 执行能力 → 阻塞", len(problems) == 1, problems[0][:50] if problems else "无")

    cfg_ok = replace(
        cfg_risky, telegram=TelegramConfig(token="123:abc", allowed_chat_ids=frozenset({"42"}))
    )
    problems2, notices2 = validate_security(cfg_ok)
    check("有白名单则通过", not problems2, f"{len(problems2)} 个问题")

    cfg_proc = replace(cfg_ok, process=ProcessConfig(enabled=True, workdir=WORKDIR / "p2"))
    _, notices3 = validate_security(cfg_proc)
    check("启用长驻进程给出提醒", any("沙箱" in n for n in notices3), "；".join(notices3)[:60] if notices3 else "无")

    cfg_full = replace(cfg_ok, codex=replace(cfg_ok.codex, sandbox="full_access"))
    _, notices4 = validate_security(cfg_full)
    check("full_access 给出提醒", any("full_access" in n for n in notices4))


# =========================================================================== #
# 主流程
# =========================================================================== #


async def run() -> int:
    WORKDIR.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 74)
    print("执行层专项测试 —— 覆盖四个缺口的实现")
    print("=" * 74)
    print(f"工作目录：{WORKDIR}")

    s1_capture()
    await s2_executor()
    await s3_processes()
    s4_audit()
    await s4b_persist()
    s5_approval_and_security()

    section("汇总")
    for name, ok, detail in _checks:
        if not ok:
            print(f"  {FAIL} {name}  — {detail}")
    passed = sum(1 for _, ok, _ in _checks if ok)
    print(f"\n  通过 {passed}/{len(_checks)}")
    if passed == len(_checks):
        print("\n  🎉 全部通过：四个缺口都已落地。\n")
        return 0
    print(f"\n  ⚠️  有 {len(_checks) - passed} 项未通过。\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
