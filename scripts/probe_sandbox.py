"""阶段 0：探测 Codex 沙箱的真实边界（不需要模型、不需要登录）。

`codex sandbox` 只是沙箱包装器，不触发模型调用 —— 所以这一步零成本、零风险、不需要账号。
产出 `docs/sandbox-probe-report.md`：一张「什么能做 / 什么不能做」的实测矩阵。

判定口径（四档，必须分开，否则会误判边界）::

    PROBE_OK      → 允许                       操作真的成功
    PROBE_DENIED  → 拒绝（OS 层拦截）           命令跑了但被系统拒绝
    PROBE_ERROR   → 命令已启动，操作失败        失败原因与沙箱无关
    三哨兵皆无    → 拒绝（命令未启动）          **沙箱根本没让命令跑起来**

==============================================================================
三个工程细节（都是踩过才加的，请勿删）
==============================================================================
1. **先自标定调用形式，不判断版本。**
   官方文档说用 `codex sandbox macos|linux|windows <cmd>`，但实测 0.154.0 的
   `sandbox --help` 只有 `Usage: codex sandbox [OPTIONS] [COMMAND]...` ——
   **没有** platform 子命令。把 `windows` 传进去会被当成要执行的命令名，
   报 `CreateProcessAsUserW failed: 2 系统找不到指定的文件`，一个完全误导人的错误。
   不同版本 flag 位置还会变，所以这里逐个候选形式实跑一条无害标定命令，
   第一个成功的即答案。

2. **探针写成 .py 文件而不是 `python -c "<code>"`。**
   `codex sandbox [OPTIONS] [COMMAND]...` 的参数解析边界不清晰，
   `-c` 有被它当成自身 flag 吞掉的风险。

3. **结果完整性闸门。** 若 ≥80% 的运行是"命令未启动"，那几乎必然是**工具或环境**出错，
   而不是沙箱在拦 —— 报告会被标记为**无效**并附原始 stderr。
   **一份错的安全报告比没有报告更危险。**

==============================================================================
覆盖的探针
==============================================================================
读        读工作区 / 读用户目录 / 读凭据（.env、~/.codex/auth.json）
写        写工作区 / 写项目根（**自我改写能力**）/ 写用户目录 / 写临时目录
网络      出站 TCP / DNS 解析
执行      起子进程 / 普通 shell / 登录 shell
环境      枚举含 KEY/TOKEN/SECRET 的环境变量
权限      列 ~/.ssh
共 14 个 × 2 种沙箱模式

用法::

    python scripts/probe_sandbox.py                    # 完整探测 + 生成报告
    python scripts/probe_sandbox.py --list             # 只看探针清单
    python scripts/probe_sandbox.py --calibrate-only   # 只做调用形式自标定
    python scripts/probe_sandbox.py --mode read-only   # 只测一种模式
    python scripts/probe_sandbox.py --only 读           # 只测某组
    python scripts/probe_sandbox.py --skip-outside-writes
    python scripts/probe_sandbox.py --print-report

本脚本会在工作区、用户目录专用子目录、临时目录各创建一个探针文件，结束时清理。
不做任何破坏性操作。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = PROJECT_ROOT / "var" / "sandbox-probe"
REPORT_PATH = PROJECT_ROOT / "docs" / "sandbox-probe-report.md"
OUTSIDE_DIR = Path.home() / ".rookie-agent-probe"

SENTINEL_OK = "PROBE_OK"
SENTINEL_DENIED = "PROBE_DENIED"
SENTINEL_ERROR = "PROBE_ERROR"
CALIB_SENTINEL = "CALIB_OK"

V_ALLOW = "允许"
V_DENIED_OS = "拒绝（OS 层拦截）"
V_FAILED = "命令已启动，操作失败"
V_NOT_STARTED = "拒绝（命令未启动）"
V_TIMEOUT = "超时"
V_NO_SPAWN = "无法启动"
V_UNKNOWN = "未知"

MODES = ("read-only", "workspace-write")
EXACT_NAMES = ("codex.exe", "codex", "codex.cmd", "codex.bat")

# 沙箱机制本身起不来时的特征串 —— 用来区分"环境不支持"与"工具写错了"
SANDBOX_MECHANISM_MARKERS = (
    "sandbox failed",
    "CreateProcessAsUser",
    "restricted token",
    "landlock",
    "seatbelt",
    "bwrap",
    "sandbox-exec",
)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Probe:
    id: str
    group: str
    goal: str
    code: str
    note: str = ""

    def argv(self, workspace: Path) -> list[str]:
        script = workspace / f"probe_{self.id}.py"
        script.write_text(self.code, encoding="utf-8")
        return [sys.executable, str(script)]


@dataclass(slots=True)
class ProbeRun:
    mode: str
    verdict: str
    exit_code: int
    stdout: str
    stderr: str
    elapsed_sec: float
    command: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class ProbeResult:
    probe: Probe
    runs: dict[str, ProbeRun] = field(default_factory=dict)


@dataclass(slots=True)
class Calibration:
    shape_name: str | None = None
    shape: tuple[str, ...] | None = None
    evidence: list[tuple[str, str, str]] = field(default_factory=list)
    environment_unsupported: bool = False
    environment_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.shape is not None


@dataclass(slots=True)
class Capabilities:
    usable: bool = False
    reason: str = ""
    version: str = ""
    help_text: str = ""
    sandbox_help: str = ""
    raw: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 定位 codex
# --------------------------------------------------------------------------- #


def find_codex() -> tuple[str | None, list[str]]:
    """按 环境变量 → PATH → 解释器同级 → site-packages 找**精确名**可执行文件。

    精确名优先是必须的：site-packages 里还有 `codex-code-mode-host.exe` 这种同前缀
    兄弟二进制，按字母序（`-` < `.`）会排在 `codex.exe` 前面被误选。
    """
    tried: list[str] = []

    override = os.environ.get("CODEX_BIN")
    if override:
        tried.append(f"CODEX_BIN={override}")
        if Path(override).is_file():
            return override, tried

    found = shutil.which("codex")
    tried.append("shutil.which('codex')")
    if found:
        return found, tried

    roots: list[Path] = [Path(sys.executable).resolve().parent]
    for key in ("scripts", "purelib", "platlib"):
        base = sysconfig.get_paths().get(key)
        if base:
            roots.append(Path(base))

    for name in EXACT_NAMES:
        candidate = roots[0] / name
        tried.append(str(candidate))
        if candidate.is_file():
            return str(candidate), tried

    for root in roots[1:]:
        if not root.exists():
            continue
        tried.append(f"rglob {EXACT_NAMES} under {root}")
        for name in EXACT_NAMES:
            for candidate in root.rglob(name):
                if candidate.is_file():
                    return str(candidate), tried

    for root in roots:
        if not root.exists():
            continue
        tried.append(f"glob codex* under {root}")
        for candidate in sorted(root.glob("**/codex*")):
            if candidate.is_file() and candidate.suffix.lower() in {".exe", ".cmd", ".bat", ""}:
                return str(candidate), tried

    return None, tried


PLATFORM_TOKEN = {"darwin": "macos", "win32": "windows", "linux": "linux"}


def platform_token() -> str:
    key = "win32" if os.name == "nt" else sys.platform
    return PLATFORM_TOKEN.get(key, "linux")


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #


def _run_quiet(argv: Sequence[str], *, timeout: float = 30.0, cwd: Path | None = None):
    try:
        return subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd) if cwd else None, errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None


def preflight(codex: str) -> Capabilities:
    """确认这是真正的 codex CLI，并把 help 文本留档（用于校准参数语义）。"""
    caps = Capabilities()

    ver = _run_quiet([codex, "--version"])
    if ver is None or ver.returncode != 0:
        caps.reason = f"`--version` 执行失败：{getattr(ver, 'stderr', '无法启动')}"
        return caps
    caps.version = (ver.stdout or ver.stderr or "").strip()[:200]

    for label, args in (("help", ["--help"]), ("sandbox_help", ["sandbox", "--help"])):
        proc = _run_quiet([codex, *args])
        caps.raw[label] = ((proc.stdout or "") + (proc.stderr or "")).strip()[:3000] if proc else ""

    caps.help_text = caps.raw.get("help", "")
    caps.sandbox_help = caps.raw.get("sandbox_help", "")

    if "sandbox" not in caps.help_text and "sandbox" not in caps.sandbox_help:
        caps.reason = "help 中找不到 sandbox 子命令，疑似选错了可执行文件"
        return caps

    caps.usable = True
    return caps


# --------------------------------------------------------------------------- #
# 调用形式自标定
# --------------------------------------------------------------------------- #


def shape_variants() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """候选调用形式。`@cfg` / `@cmd` 是占位符，其余 token 原样输出。

    顺序按"最可能正确"排列：0.154.0 的 help 是 `codex sandbox [OPTIONS] [COMMAND]...`，
    对应第一种。
    """
    plat = platform_token()
    return (
        ("sandbox [cfg] cmd", ("sandbox", "@cfg", "@cmd")),
        (f"sandbox {plat} [cfg] cmd", ("sandbox", plat, "@cfg", "@cmd")),
        ("[cfg] sandbox cmd", ("@cfg", "sandbox", "@cmd")),
        ("debug [cfg] cmd", ("debug", "@cfg", "@cmd")),
        ("sandbox cmd（不带 cfg）", ("sandbox", "@cmd")),
    )


def materialize(
    shape: Sequence[str], codex: str, cfg: Sequence[str], command: Sequence[str]
) -> list[str]:
    argv = [codex]
    for token in shape:
        if token == "@cfg":
            argv.extend(cfg)
        elif token == "@cmd":
            argv.extend(command)
        else:
            argv.append(token)
    return argv


def config_args(mode: str) -> list[str]:
    return ["-c", f'sandbox_mode="{mode}"', "-c", 'approval_policy="never"']


def calibrate(codex: str, *, timeout: float) -> Calibration:
    """逐个候选形式实跑一条无害标定命令，第一个能跑通的就是答案。"""
    calib = Calibration()
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    script = WORKSPACE / "_calibrate.py"
    script.write_text(f"print({CALIB_SENTINEL!r})\n", encoding="utf-8")
    command = [sys.executable, str(script)]
    cfg = config_args("read-only")

    # 受限令牌沙箱在"沙箱套沙箱"的嵌套环境下会偶发失败
    # （实测同一命令一次成功一次失败），必须重试，否则会把偶发故障误判成"环境不支持"。
    attempts = 3
    failures: list[str] = []
    for name, shape in shape_variants():
        argv = materialize(shape, codex, cfg, command)
        snippet = ""
        for attempt in range(1, attempts + 1):
            proc = _run_quiet(argv, timeout=timeout, cwd=WORKSPACE)
            stdout = (proc.stdout or "") if proc else ""
            if proc is not None and CALIB_SENTINEL in stdout:
                calib.shape_name, calib.shape = name, shape
                note = "" if attempt == 1 else f"第 {attempt} 次尝试才成功（沙箱启动存在偶发性）"
                calib.evidence.append((name, "成功", note))
                return calib
            snippet = ((proc.stderr or proc.stdout or "") if proc else "无法启动").strip()[:220]
        failures.append(snippet)
        calib.evidence.append((name, "失败", snippet))

    joined = " ".join(failures).lower()

    # 两种完全不同的失败必须分开诊断，否则会把"跑错地方"误判成"平台不支持"：
    #   ① 上层沙箱拦住子进程创建 —— 说明脚本本身跑在另一个沙箱里，换地方跑就行
    #   ② 平台沙箱机制起不来 —— 这才是环境限制，且**只对当前平台成立**
    if "unable to create process using" in joined:
        calib.environment_reason = (
            "子进程创建被上层沙箱拦截（stderr: `Unable to create process using ...`）。"
            "典型场景是**本脚本自身就跑在另一个沙箱里** —— 受限令牌叠加嵌套执行过不去。"
            "请在普通终端（最好是部署目标机）上直接运行本脚本。"
        )
        return calib

    if any(marker.lower() in joined for marker in SANDBOX_MECHANISM_MARKERS):
        calib.environment_unsupported = True
        calib.environment_reason = (
            f"沙箱机制在 `{platform_token()}` 上无法启动（stderr 命中 sandbox / restricted-token 特征）。"
            "这不是策略拒绝。**注意本结论只对当前平台成立** —— Codex 沙箱是平台相关实现："
            "Linux 用 bwrap + seccomp，macOS 用 Seatbelt，原生 Windows 用 restricted token"
            "（WSL2 下反而走 Linux 实现）。请以**部署目标平台**的探测结果为准。"
        )
    return calib


# --------------------------------------------------------------------------- #
# 探针定义
# --------------------------------------------------------------------------- #


def _wrap(action: str, extra_stderr: str = "") -> str:
    """把单行动作包成带哨兵的脚本。``action`` 必须是一行语句（可用 ``;`` 串联）。"""
    lines = ["import sys, os", "try:", f"    {action}", f"    print({SENTINEL_OK!r})"]
    if extra_stderr:
        lines.append(f"    sys.stderr.write({extra_stderr} + chr(10))")
    lines += [
        "except PermissionError as e:",
        f"    print({SENTINEL_DENIED!r}, e)",
        "except Exception as e:",
        f"    print({SENTINEL_ERROR!r}, type(e).__name__, e)",
    ]
    return "\n".join(lines) + "\n"


def build_probes(*, include_outside_writes: bool) -> list[Probe]:
    prj, ws = PROJECT_ROOT.as_posix(), WORKSPACE.as_posix()
    home, outside = Path.home().as_posix(), OUTSIDE_DIR.as_posix()
    tmp, py = Path(tempfile.gettempdir()).as_posix(), sys.executable

    probes: list[Probe] = [
        Probe("read_workspace", "读", "读取工作区内的文件",
              _wrap(f"open(r'{ws}/sample.txt').read()"), "文档称 read-only 允许读"),
        Probe("read_home_dir", "读", "列出用户目录（工作区之外）",
              _wrap(f"os.listdir(r'{home}')"), "读权限是否被限制在 cwd"),
        Probe("read_secrets", "读", "尝试读取 .env 与 ~/.codex/auth.json（凭据）",
              "import sys, os\n"
              f"targets = [r'{prj}/.env', r'{home}/.codex/auth.json']\n"
              "try:\n"
              "    hits = [p for p in targets if os.path.exists(p)]\n"
              "    for p in hits:\n"
              "        open(p).read(1)\n"
              f"    print({SENTINEL_OK!r}, hits)\n"
              "except PermissionError as e:\n"
              f"    print({SENTINEL_DENIED!r}, e)\n"
              "except Exception as e:\n"
              f"    print({SENTINEL_ERROR!r}, type(e).__name__, e)\n",
              "**最关键**：能读到就等于 API Key 暴露给 agent"),
        Probe("read_ssh_dir", "权限", "列出 ~/.ssh",
              _wrap(f"os.listdir(r'{home}/.ssh')"),
              "目录不存在时会落到 PROBE_ERROR，属正常"),
        Probe("write_workspace", "写", "写 var/sandbox-probe/（工作区内）",
              _wrap(f"open(r'{ws}/write_test.txt','w').write('x')")),
    ]

    if include_outside_writes:
        probes += [
            Probe("write_project_root", "写", "写项目根目录（**改 agent 自己的源码**）",
                  _wrap(f"open(r'{prj}/.agent_probe_write.txt','w').write('x')"),
                  "workspace-write 下 cwd 就是项目根 → 直接测自我改写能力"),
            Probe("write_home", "写", "写用户目录下的专用探针子目录",
                  _wrap(f"os.makedirs(r'{outside}',exist_ok=True); "
                        f"open(r'{outside}/w.txt','w').write('x')")),
            Probe("write_temp", "写", "写系统临时目录",
                  _wrap(f"open(r'{tmp}/agent_probe_write.txt','w').write('x')"),
                  "workspace-write 常对 temp 有例外"),
        ]

    probes += [
        Probe("net_outbound", "网络", "TCP 出站到 1.1.1.1:80",
              _wrap("__import__('socket').create_connection(('1.1.1.1',80),3)"),
              "网络总开关的实际效果"),
        Probe("net_dns", "网络", "DNS 解析 example.com",
              _wrap("__import__('socket').getaddrinfo('example.com',80)")),
        Probe("spawn_child", "执行", "启动一个子进程",
              _wrap(f"__import__('subprocess').run([r'{py}','-c','pass'],check=True)"),
              "命令执行的基础能力"),
        Probe("shell_cmd", "执行", "普通 shell 调用（cmd /c 或 sh -c）",
              _wrap("(__import__('subprocess').run(['cmd','/c','echo S'],check=True) "
                    "if os.name=='nt' else "
                    "__import__('subprocess').run(['sh','-c','echo S'],check=True))")),
        Probe("shell_login", "执行", "登录 shell（bash -lc）",
              _wrap("__import__('subprocess').run(['bash','-lc','echo S'],check=True)"),
              "allow_login_shell 的实际含义；Windows 上预计直接 ERROR"),
        Probe("env_secrets", "环境", "枚举含 KEY/TOKEN/SECRET 的环境变量名",
              _wrap("pass",
                    "str([k for k in os.environ if any(t in k.upper() "
                    "for t in ('KEY','TOKEN','SECRET','PASSWORD'))])"),
              "**子进程是否继承凭据** —— 一条 env 就能把 Key 带出去"),
    ]
    return probes


# --------------------------------------------------------------------------- #
# 执行与判定
# --------------------------------------------------------------------------- #


def classify(exit_code: int, stdout: str, stderr: str) -> str:
    text = f"{stdout}\n{stderr}"
    if SENTINEL_DENIED in text:
        return V_DENIED_OS
    if SENTINEL_OK in text:
        return V_ALLOW
    if SENTINEL_ERROR in text:
        return V_FAILED
    if exit_code != 0:
        return V_NOT_STARTED
    return V_UNKNOWN


def run_probe(
    codex: str, mode: str, probe: Probe, *, calib: Calibration, timeout: float
) -> ProbeRun:
    assert calib.shape is not None
    argv = materialize(calib.shape, codex, config_args(mode), probe.argv(WORKSPACE))
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            cwd=str(WORKSPACE), errors="replace",
        )
    except subprocess.TimeoutExpired:
        return ProbeRun(mode, V_TIMEOUT, -1, "", "", time.perf_counter() - started, argv,
                        error=f"超过 {timeout:g}s")
    except OSError as exc:
        return ProbeRun(mode, V_NO_SPAWN, -1, "", "", time.perf_counter() - started, argv,
                        error=str(exc))
    elapsed = time.perf_counter() - started
    out, err = (proc.stdout or "")[-1500:], (proc.stderr or "")[-1500:]
    return ProbeRun(mode, classify(proc.returncode, out, err), proc.returncode, out, err, elapsed, argv)


# --------------------------------------------------------------------------- #
# 准备与清理
# --------------------------------------------------------------------------- #


def prepare() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "sample.txt").write_text("probe sample\n", encoding="utf-8")


def cleanup() -> None:
    for path in (
        WORKSPACE / "write_test.txt",
        PROJECT_ROOT / ".agent_probe_write.txt",
        Path(tempfile.gettempdir()) / "agent_probe_write.txt",
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    shutil.rmtree(OUTSIDE_DIR, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #


def recommended_config() -> str:
    return "\n".join([
        "# 建议写进 .env（app 通过 SDK 按 thread 传 sandbox，不依赖 CLI profile）",
        "AGENT_CODEX_SANDBOX=workspace-write   # 需要写文件能力时才提上来",
        "",
        "# 若要让 Codex CLI 也用同一套边界，新建 profile 而不是改全局默认：",
        "#   ~/.codex/rookie-agent.config.toml",
        'approval_policy = "never"             # 无人值守：没有真人可答审批',
        'sandbox_mode = "workspace-write"',
        "allow_login_shell = false             # 避开 rc 文件陷阱",
        "[sandbox_workspace_write]",
        "network_access = false                # 默认关，显式写出来",
        "",
        "# 然后： codex --profile rookie-agent",
    ])


def render_report(
    *,
    codex: str,
    caps: Capabilities,
    calib: Calibration,
    results: list[ProbeResult],
    modes: tuple[str, ...],
    elapsed: float,
    invalid_reason: str | None,
) -> str:
    out: list[str] = []
    add = out.append

    add("# Codex 沙箱边界实测报告")
    add("")
    if invalid_reason:
        add("> ## ⚠️ 本次探测无效")
        add("> ")
        add(f"> {invalid_reason}")
        add("> ")
        add("> 不要把下表当作沙箱能力的证据 —— 它记录的是工具或环境故障，不是安全策略。")
        add("")

    add(f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    add(f"- 操作系统：{sys.platform}（{os.name}）")
    add(f"- codex 可执行文件：`{codex}`")
    add(f"- codex 版本：`{caps.version or '未知'}`")
    add(f"- **自标定调用形式**：`{calib.shape_name or '无可用形式'}`"
        + (f" → `{' '.join(calib.shape)}`" if calib.shape else ""))
    add(f"- Python：`{sys.executable}`")
    add(f"- 工作目录（cwd）：`{WORKSPACE.as_posix()}`")
    add(f"- 探测耗时：{elapsed:.1f}s")
    add("")
    add("> 由 `scripts/probe_sandbox.py` 自动生成，重新探测会覆盖。")
    add("")

    add("## 1. 自标定过程")
    add("")
    add("候选调用形式逐个实跑一条无害命令，取第一个成功的：")
    add("")
    add("| 候选形式 | 结果 | 输出摘要 |")
    add("|---|---|---|")
    for name, verdict, snippet in calib.evidence:
        add(f"| `{name}` | {verdict} | {snippet.replace('|', '\\|')[:160] or '—'} |")
    add("")

    if results:
        add("## 2. 实测矩阵")
        add("")
        add("| 组 | 探针 | 目标 | " + " | ".join(f"`{m}`" for m in modes) + " |")
        add("|---|---|---|" + "---|" * len(modes))
        for result in results:
            cells = " | ".join(result.runs[m].verdict if m in result.runs else "—" for m in modes)
            add(f"| {result.probe.group} | `{result.probe.id}` | {result.probe.goal} | {cells} |")
        add("")

        add("## 3. 判定口径")
        add("")
        add("| 判定 | 含义 |")
        add("|---|---|")
        add(f"| {V_ALLOW} | 打印了 `PROBE_OK`，操作真的成功 |")
        add(f"| {V_DENIED_OS} | 打印了 `PROBE_DENIED`，命令跑了但被系统拒绝 |")
        add(f"| {V_FAILED} | 打印了 `PROBE_ERROR`，失败原因与沙箱无关 |")
        add(f"| {V_NOT_STARTED} | 三哨兵皆无且退出码非零，**沙箱根本没让命令启动** |")
        add("")
        add("把「OS 层拦截」和「命令未启动」混为一谈会误判边界 —— 前者说明沙箱在拦，"
            "后者可能是参数写错。")
        add("")

        add("## 4. 关键结论")
        add("")
        key = {
            "read_secrets": "**凭据可读性** —— 能读到就等于 `.env` / Codex 登录态完全暴露",
            "write_project_root": "**自我改写能力** —— 能写项目根，agent 就能改自己的源码",
            "write_home": "工作区外的写能力",
            "net_outbound": "网络总开关",
            "env_secrets": "子进程继承的环境变量里是否有凭据类变量",
        }
        for probe_id, meaning in key.items():
            found = next((r for r in results if r.probe.id == probe_id), None)
            if not found:
                continue
            cells = "，".join(f"{m}={found.runs[m].verdict}" for m in modes if m in found.runs)
            add(f"- **`{probe_id}`**：{cells} — {meaning}")
        add("")

        add("## 5. 原始输出")
        add("")
        for result in results:
            add(f"### `{result.probe.id}` — {result.probe.goal}")
            if result.probe.note:
                add(f"> {result.probe.note}")
            add("")
            for mode in modes:
                run = result.runs.get(mode)
                if run is None:
                    continue
                add(f"**{mode}** → {run.verdict}（exit={run.exit_code}，{run.elapsed_sec:.2f}s）")
                add("```")
                add(" ".join(run.command))
                add("```")
                if run.error:
                    add(f"```\n{run.error}\n```")
                snippet = (run.stdout or "").strip() or (run.stderr or "").strip()
                if snippet:
                    add("```")
                    add(snippet[:900])
                    add("```")
                add("")

    add("## 6. help 文本留档（参数语义校准）")
    add("")
    for label, text in caps.raw.items():
        add(f"### {label}")
        add("```")
        add(text or "<空>")
        add("```")
        add("")

    add("## 7. 建议的配置")
    add("")
    add("```")
    add(recommended_config())
    add("```")
    add("")

    add("## 8. 下一步判断")
    add("")
    if calib.environment_unsupported:
        add("**沙箱机制在本机不可用**，这本身就是一个关键结论：")
        add("")
        add("- 走 WSL2 跑 Codex（官方推荐路径），或")
        add("- 改用 Docker backend 让容器承担隔离职责，或")
        add("- 接受 `local` backend 且**不要**给 agent 写权限。")
        add("")
        add("在这之前，阶段 2（真实执行能力）不应开工 —— 没有沙箱兜底的执行能力是裸奔。")
    else:
        add("- `read-only` 下已能满足「跑测试 / 读日志 / 查 git 状态」→ 阶段 1-4 大部分可以不做。")
        add("- `write_project_root` 为「允许」→ agent 能改自己源码，需 Docker/独立目录隔离"
            "（`docs/shell-tool-design.md` 风险 3）。")
        add("- `read_secrets` 为「允许」→ 密钥必须靠子进程环境隔离，不能指望沙箱（风险 6）。")
        add("- `env_secrets` 打出非空列表 → 一条 `env` 就能把 Key 带出去。")
    add("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="探测 Codex 沙箱的真实边界")
    parser.add_argument("--mode", choices=MODES, action="append", help="只测指定模式（可重复）")
    parser.add_argument("--only", action="append", help="只测指定分组或探针 id（可重复）")
    parser.add_argument("--skip-outside-writes", action="store_true", help="不测工作区外的写")
    parser.add_argument("--timeout", type=float, default=45.0, help="单探针超时秒数")
    parser.add_argument("--list", action="store_true", help="只列出探针清单")
    parser.add_argument("--calibrate-only", action="store_true", help="只做调用形式自标定")
    parser.add_argument("--print-report", action="store_true", help="报告打到 stdout")
    args = parser.parse_args(argv)

    probes = build_probes(include_outside_writes=not args.skip_outside_writes)
    modes = tuple(args.mode) if args.mode else MODES
    if args.only:
        wanted = set(args.only)
        probes = [p for p in probes if p.group in wanted or p.id in wanted]

    if args.list:
        print(f"{'组':<6}{'探针 id':<22}目标")
        print("-" * 78)
        for probe in probes:
            print(f"{probe.group:<6}{probe.id:<22}{probe.goal}")
        print(f"\n共 {len(probes)} 个探针 × {len(modes)} 种模式 = {len(probes) * len(modes)} 次运行")
        return 0

    print("=" * 78)
    print("阶段 0：Codex 沙箱边界探测")
    print("=" * 78)
    print("codex sandbox 只调用沙箱包装器，不触发模型调用，因此不需要登录。\n")

    codex, tried = find_codex()
    if codex is None:
        print("未找到 codex 可执行文件。已尝试：")
        for item in tried:
            print(f"  · {item}")
        print("\n请先安装：pip install openai-codex\n或设置 CODEX_BIN 指向已有二进制。")
        return 2
    print(f"定位到：{codex}")

    caps = preflight(codex)
    if not caps.usable:
        print(f"\n❌ 自检失败：{caps.reason}")
        for item in tried:
            print(f"  · {item}")
        print("\n请用 CODEX_BIN 显式指定，例如：")
        print(r"  set CODEX_BIN=...\Lib\site-packages\codex_cli_bin\bin\codex.exe")
        return 3
    print(f"自检通过：{caps.version}")

    prepare()
    print("\n自标定调用形式（逐个候选实跑）…")
    calib = calibrate(codex, timeout=args.timeout)
    for name, verdict, snippet in calib.evidence:
        print(f"  [{'✓' if verdict == '成功' else '×'}] {name:<28}{snippet[:70]}")

    if not calib.ok:
        reason = calib.environment_reason or (
            f"所有 {len(calib.evidence)} 种调用形式都无法执行标定命令，"
            "且 stderr 未命中沙箱机制特征 —— 疑似参数语义变更或二进制不完整。"
        )
        print(f"\n❌ 自标定失败。\n{reason}")
        report = render_report(
            codex=codex, caps=caps, calib=calib, results=[], modes=modes,
            elapsed=0.0, invalid_reason=reason,
        )
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(report, encoding="utf-8")
        print(f"\n诊断报告已写入：{REPORT_PATH}")
        return 5 if calib.environment_unsupported else 4

    print(f"\n可用形式：{calib.shape_name}")

    if args.calibrate_only:
        return 0

    cleanup()
    prepare()
    print(f"\n探针工作区：{WORKSPACE}")
    print(f"模式：{', '.join(modes)}    探针数：{len(probes)}\n")

    started = time.perf_counter()
    results: list[ProbeResult] = []
    for index, probe in enumerate(probes, 1):
        result = ProbeResult(probe=probe)
        for mode in modes:
            result.runs[mode] = run_probe(codex, mode, probe, calib=calib, timeout=args.timeout)
        results.append(result)
        verdicts = "  ".join(f"{m}={result.runs[m].verdict}" for m in modes)
        print(f"[{index:>2}/{len(probes)}] {probe.id:<20} {verdicts}")

    elapsed = time.perf_counter() - started
    cleanup()

    all_runs = [run for r in results for run in r.runs.values()]
    blocked = sum(1 for run in all_runs if run.verdict in {V_NOT_STARTED, V_NO_SPAWN})
    invalid_reason: str | None = None
    if all_runs and blocked >= len(all_runs) * 0.8:
        invalid_reason = (
            f"{blocked}/{len(all_runs)} 次运行都是「命令未启动」。"
            "自标定已通过说明调用形式正确，因此这更可能是沙箱机制在本机受限，"
            "而非策略拒绝。"
        )

    report = render_report(
        codex=codex, caps=caps, calib=calib, results=results, modes=modes,
        elapsed=elapsed, invalid_reason=invalid_reason,
    )
    if args.print_report:
        print("\n" + report)
    else:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(report, encoding="utf-8")
        print(f"\n报告已写入：{REPORT_PATH}")

    if invalid_reason:
        print(f"\n⚠️  {invalid_reason}")
        for run in all_runs[:2]:
            print(f"  $ {' '.join(run.command)}")
            print(f"    exit={run.exit_code}")
            print("    " + ((run.stderr or run.stdout or "<空>").strip().replace("\n", "\n    "))[:700])
        return 6

    print("\n关键探针速览：")
    for probe_id in ("read_secrets", "write_project_root", "net_outbound", "env_secrets"):
        found = next((r for r in results if r.probe.id == probe_id), None)
        if found:
            print(f"  {probe_id:<20} " + "  ".join(f"{m}={found.runs[m].verdict}" for m in modes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
