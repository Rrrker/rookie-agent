"""非模型消费方的一次性命令执行 + 共享的输出捕获工具。

==============================================================================
这一层和 Codex 内置 shell 的分工（别搞混）
==============================================================================
Codex **已经自带**命令执行：模型想跑命令时用它的内置 shell 工具，并受沙箱约束。
本模块服务的是**另一类消费方** —— 我们自己的代码：

* orchestrator 做状态汇总 / 健康检查
* 定时巡检
* 分身提交结果前做一次轻量校验

它们不该绕道模型 —— 多一次 API 调用、多一次不确定性、还可能被模型"自由发挥"。

==============================================================================
两个刻意加上的约束
==============================================================================
1. **不接受自由命令。** 只跑 :data:`NAMED_COMMANDS` 里登记过的白名单。
   这不是"安全边界"（真正的边界是沙箱与最小权限），而是**阻止内部代码在演进中
   悄悄长成一个任意执行入口**。想加命令必须显式登记，这个动作本身会让人停一下想清楚。

2. **输出捕获有纪律。** 双阈值、并发读双流、剥离 ANSI、编码兜底。
   每一条都是踩出来的，见各函数注释。**只读 stdout 不读 stderr 会因管道缓冲区写满而死锁**，
   这是 subprocess 最经典的坑。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from app.config import ExecConfig
from app.contracts import ExecResult

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 输出归一化
# --------------------------------------------------------------------------- #

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# 子进程只保留这些环境变量。默认继承整个 os.environ 会把 API Key 送进命令作用域，
# 一条 `env` 就能把密钥打出来回传到聊天里。
_ENV_KEEP = (
    "PATH", "HOME", "USERPROFILE", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "COMSPEC", "WINDIR", "PATHEXT", "SYSTEMDRIVE", "APPDATA", "LOCALAPPDATA",
)


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造最小环境，替代默认的全量继承。"""
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    if extra:
        env.update(extra)
    return env


def strip_ansi(text: str) -> str:
    """剥掉 ANSI 转义序列 —— 否则日志不可读，还白烧 token。"""
    return _ANSI_RE.sub("", text)


def looks_binary(raw: bytes) -> bool:
    return b"\x00" in raw[:4096]


def decode_output(raw: bytes) -> str:
    """编码兜底链：UTF-16（看 BOM）→ UTF-8 → GBK → latin-1。

    Windows 上 PowerShell 重定向常产出 UTF-16，中文控制台常产出 GBK，
    所以这里不能只写 ``errors="replace"`` 的 utf-8 —— 那样中文会变问号。
    """
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        with contextlib.suppress(UnicodeError):
            return raw.decode("utf-16")
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def clip(text: str, limit: int) -> tuple[str, bool]:
    """按字符数截断，返回 (文本, 是否截断)。"""
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit] + f"\n…[已截断，原始长度 {len(text)} 字符]", True


def spawn_kwargs() -> dict[str, object]:
    """让子进程成为**独立进程组**。

    否则超时只能杀掉直接子进程，``bash -c "sleep 1000 &"`` 留下的孙进程会活成僵尸。
    POSIX 用 ``start_new_session``，Windows 用 ``CREATE_NEW_PROCESS_GROUP``。
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def read_stream(stream: asyncio.StreamReader | None, max_bytes: int) -> tuple[bytes, bool]:
    """带**进程级字节上限**地读一个流。

    必须与另一个流并发调用（``asyncio.gather``），否则管道缓冲区写满会死锁。
    超过上限就停止读取并返回 ``truncated=True`` —— 调用方据此杀掉进程。
    只做"返回时截断"是不够的：``yes`` / ``cat /dev/urandom`` 会在截断之前先把内存吃光。
    """
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if total + len(chunk) > max_bytes:
            chunks.append(chunk[: max(0, max_bytes - total)])
            return b"".join(chunks), True
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), False


async def terminate_tree(proc: asyncio.subprocess.Process, *, grace: float = 5.0) -> None:
    """终止**整个进程组**，先礼后兵。

    Windows 没有进程组信号，用 ``taskkill /T`` 才是真的杀树。
    """
    if proc.returncode is not None:
        return

    if os.name == "nt":
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=2.0)


# --------------------------------------------------------------------------- #
# 白名单命令登记表
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class NamedCommand:
    name: str
    argv: tuple[str, ...]
    description: str
    timeout_sec: float = 30.0


def default_named_commands() -> dict[str, NamedCommand]:
    """内部代码被允许执行的**全部**命令。

    刻意做得很小 —— 每一项都要说得出"谁在什么时候需要它"。
    """
    py = sys.executable
    items = (
        NamedCommand("git_status", ("git", "status", "--short", "--branch"), "仓库状态摘要"),
        NamedCommand("git_last_commit", ("git", "log", "-1", "--stat", "--oneline"), "最近一次提交"),
        NamedCommand("git_diff_stat", ("git", "diff", "--stat"), "未提交改动概览"),
        NamedCommand("python_version", (py, "--version"), "解释器版本"),
        NamedCommand(
            "platform_probe",
            (py, "-c", "import sys;print(sys.platform, sys.version.split()[0])"),
            "平台探针",
        ),
    )
    return {item.name: item for item in items}


# --------------------------------------------------------------------------- #
# 执行器
# --------------------------------------------------------------------------- #


class LocalExecutor:
    """实现 :class:`app.contracts.ExecPort`。

    对外**只暴露** ``run_named`` —— 想跑新命令就先登记。
    """

    def __init__(self, cfg: ExecConfig, commands: dict[str, NamedCommand] | None = None) -> None:
        self.cfg = cfg
        self._commands = commands or default_named_commands()

    # ------------------------------------------------------------------ #
    # 登记表
    # ------------------------------------------------------------------ #

    def names(self) -> list[str]:
        return sorted(self._commands)

    def describe(self) -> dict[str, str]:
        return {name: cmd.description for name, cmd in sorted(self._commands.items())}

    def argv_of(self, name: str) -> tuple[str, ...] | None:
        cmd = self._commands.get(name)
        return cmd.argv if cmd else None

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #

    async def run_named(self, name: str, *, cwd: Path | None = None) -> ExecResult:
        started = time.perf_counter()
        command = self._commands.get(name)
        if command is None:
            return ExecResult(
                argv=(name,),
                ok=False,
                error_code="unknown_command",
                error=f"未登记的命令：{name}（可用：{', '.join(self.names())}）",
            )
        if not self.cfg.enabled:
            return ExecResult(
                argv=command.argv, ok=False, error_code="disabled", error="内部执行入口已关闭"
            )
        return await self._spawn_and_capture(
            command.argv,
            cwd=cwd or self.cfg.workdir,
            timeout=min(command.timeout_sec, self.cfg.timeout_sec),
            max_capture=self.cfg.max_capture_bytes,
            max_output=self.cfg.max_output_chars,
            env_extra=self.cfg.env_extra,
        )

    async def _spawn_and_capture(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        max_capture: int,
        max_output: int,
        env_extra: dict[str, str] | None = None,
        stdin_data: bytes | None = None,
    ) -> ExecResult:
        """共享的底层执行原语（process_manager 也用它）。

        返回**永不抛异常**的 :class:`ExecResult`，并按四类错误分开归因：
        ``timeout`` / ``not_found`` / ``output_overflow`` / ``spawn_failed``。
        归因分清楚很重要 —— 模型与调用方对它们的反应完全不同。
        """
        started = time.perf_counter()
        argv_list = [str(a) for a in argv]
        cwd = Path(cwd)
        cwd.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv_list,
                cwd=str(cwd),
                env=minimal_env(env_extra),
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **spawn_kwargs(),
            )
        except FileNotFoundError:
            return ExecResult(
                argv=tuple(argv_list), ok=False, error_code="not_found",
                error=f"命令不存在：{argv_list[0]}",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except OSError as exc:
            return ExecResult(
                argv=tuple(argv_list), ok=False, error_code="spawn_failed", error=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        if stdin_data is not None and proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
            proc.stdin.close()

        timed_out = False
        overflow = False
        try:
            (out_raw, out_trunc), (err_raw, err_trunc) = await asyncio.wait_for(
                asyncio.gather(
                    read_stream(proc.stdout, max_capture),
                    read_stream(proc.stderr, max_capture),
                ),
                timeout=timeout,
            )
            overflow = out_trunc or err_trunc
        except asyncio.TimeoutError:
            timed_out = True
            out_raw = err_raw = b""
        except asyncio.CancelledError:
            await terminate_tree(proc)
            raise

        if timed_out or overflow:
            await terminate_tree(proc, grace=3.0)

        # 无论走哪条路径都必须 wait()：
        # 读取端在管道 EOF 时就会返回，而 returncode 是由 transport 的
        # process_exited 回调设置的 —— 两者有时序差。不 wait 就会拿到 None，
        # 把"命令成功"误判成"退出码未知"。
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5.0)

        code = proc.returncode
        stdout = strip_ansi(decode_output(out_raw))
        stderr = strip_ansi(decode_output(err_raw))
        if looks_binary(out_raw):
            stdout = f"<二进制输出 {len(out_raw)} 字节，已省略>"
        stdout, cut_out = clip(stdout, max_output)
        stderr, cut_err = clip(stderr, max_output)

        result = ExecResult(
            argv=tuple(argv_list),
            ok=not timed_out and not overflow,
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            truncated=cut_out or cut_err or overflow,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        if timed_out:
            result.error_code = "timeout"
            result.error = f"执行超时（{timeout:g}s），已终止整个进程组"
        elif overflow:
            result.error_code = "output_overflow"
            result.error = f"输出超过 {max_capture} 字节上限，已终止进程"
        return result


def summarize(result: ExecResult, *, head_lines: int = 20) -> str:
    """把 :class:`ExecResult` 压成一段可读文本（给 /status、日志播报用）。"""
    if not result.ok and result.error_code in {"not_found", "unknown_command", "disabled", "spawn_failed"}:
        return f"✗ {' '.join(result.argv)}\n  {result.error}"
    lines: list[str] = []
    head = f"$ {' '.join(result.argv)}"
    lines.append(head)
    body = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if body:
        picked = body.splitlines()
        lines.extend(picked[:head_lines])
        if len(picked) > head_lines:
            lines.append(f"…（共 {len(picked)} 行，此处 {head_lines} 行）")
    elif err:
        picked = err.splitlines()
        lines.extend(f"! {ln}" for ln in picked[:head_lines])
    else:
        lines.append("（无输出）")
    if result.error_code:
        lines.append(f"! {result.error}")
    lines.append(f"→ exit={result.exit_code}  {result.duration_ms}ms")
    return "\n".join(lines)


def iter_iterable(values: Iterable[str] | None) -> list[str]:
    return list(values or [])
