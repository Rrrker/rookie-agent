"""长驻进程管理（Hermes 的 ``process`` 工具对应物）。

==============================================================================
为什么必须新建这一个
==============================================================================
核对下来，Codex 已经提供了 Hermes ``terminal`` 的全部能力（一次性命令 + 输出 + 退出码），
但**没有** ``process``：没有"起一个长驻进程 / 拉日志 / 杀掉"的三件套。

后果很具体：``npm run dev``、``python -m http.server``、``pytest --watch`` 这类命令，
用一次性执行会一直等到超时，然后被强行终止 —— 你既拿不到日志，也没法在它跑着的时候做别的事。
**这是四个缺口里唯一值得新建模块的一个。**

==============================================================================
⚠️ 安全定位：这条路没有沙箱
==============================================================================
必须说清楚：**Codex 内置 shell 受它的 OS 级沙箱约束，而本模块不受任何沙箱约束** ——
它就是普通的 ``asyncio.create_subprocess_exec``，权限等于当前进程。

所以：

* :attr:`ProcessConfig.enabled` **默认 False**，启用是一个显式动作；
* 只做了一层很薄的"防误操作"过滤（见 :data:`_HARDLINE_PROGRAMS`），
  **它不是安全边界**，绕过去的方法多得很；
* 真正的隔离要靠容器/独立用户，而不是靠这里的字符串判断。

这三句话是刻意写进代码里的，因为"以为有人兜底"比"知道没人兜底"危险得多。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from app.config import ProcessConfig
from app.contracts import ProcessInfo
from app.local_exec import decode_output, minimal_env, spawn_kwargs, strip_ansi, terminate_tree

logger = logging.getLogger(__name__)

# 极薄的防误操作过滤。**不是安全边界**，只拦最明显的灾难性命令。
_HARDLINE_PROGRAMS = frozenset(
    {"mkfs", "mkfs.ext4", "mkfs.ext3", "fdisk", "parted", "wipefs", "shred", "diskpart", "format"}
)
_HARDLINE_FRAGMENTS = (":(){", "rm -rf /", "rm -rf /*", "> /dev/sd", "of=/dev/sd")


@dataclass(slots=True)
class _Managed:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    proc: asyncio.subprocess.Process
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    logs: deque[str] = field(default_factory=deque)
    log_bytes: int = 0
    readers: list[asyncio.Task[None]] = field(default_factory=list)
    exit_code: int | None = None

    def append(self, line: str, *, max_bytes: int) -> None:
        self.logs.append(line)
        self.log_bytes += len(line) + 1
        # 日志按字节预算从旧到新裁掉 —— 长驻进程跑一周也不会把内存吃穿
        while self.log_bytes > max_bytes and self.logs:
            dropped = self.logs.popleft()
            self.log_bytes -= len(dropped) + 1

    @property
    def running(self) -> bool:
        return self.proc.returncode is None


class ProcessManager:
    """实现 :class:`app.contracts.ProcessPort`。

    刻意不继承任何"策略引擎" —— 职责单一：生命周期 + 日志环形缓冲 + 干净收尸。
    """

    def __init__(self, cfg: ProcessConfig) -> None:
        self.cfg = cfg
        self._procs: dict[str, _Managed] = {}
        cfg.workdir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _screen(argv: Sequence[str]) -> str | None:
        """极薄的防误操作过滤，返回拒绝原因或 None。"""
        if not argv:
            return "空命令"
        program = Path(str(argv[0])).stem.lower()
        if program in _HARDLINE_PROGRAMS:
            return f"拒绝启动 {program}（磁盘级操作）"
        joined = " ".join(str(a) for a in argv).lower()
        for fragment in _HARDLINE_FRAGMENTS:
            if fragment in joined:
                return f"拒绝启动：命令含高危片段 {fragment!r}"
        return None

    def _live(self) -> list[_Managed]:
        return [m for m in self._procs.values() if m.running]

    def _info(self, managed: _Managed, error: str | None = None) -> ProcessInfo:
        now = datetime.now(timezone.utc)
        return ProcessInfo(
            name=managed.name,
            argv=managed.argv,
            cwd=str(managed.cwd),
            pid=managed.proc.pid,
            running=managed.running,
            exit_code=managed.exit_code if managed.exit_code is not None else managed.proc.returncode,
            started_at=managed.started_at,
            uptime_sec=round((now - managed.started_at).total_seconds(), 1),
            log_lines=len(managed.logs),
            log_bytes=managed.log_bytes,
            error=error,
        )

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> ProcessInfo:
        argv_tuple = tuple(str(a) for a in argv)
        workdir = Path(cwd or self.cfg.workdir)

        if not self.cfg.enabled:
            return ProcessInfo(
                name=name, argv=argv_tuple, cwd=str(workdir),
                error="长驻进程功能未启用（设置 AGENT_PROCESS_ENABLED=true）",
            )
        if reason := self._screen(argv_tuple):
            logger.warning("拒绝启动进程 %s：%s", name, reason)
            return ProcessInfo(name=name, argv=argv_tuple, cwd=str(workdir), error=reason)
        if (existing := self._procs.get(name)) and existing.running:
            return ProcessInfo(
                name=name, argv=argv_tuple, cwd=str(workdir),
                error=f"同名进程 {name} 已在运行（pid={existing.proc.pid}）",
            )
        # 先回收已退出的进程再数并发。否则刚退出的进程会被算成"运行中"，
        # 让 start 被"已达上限"误拒 —— 这是实测踩出来的 bug。
        await self.reap()

        if len(self._live()) >= self.cfg.max_processes:
            return ProcessInfo(
                name=name, argv=argv_tuple, cwd=str(workdir),
                error=f"并发上限 {self.cfg.max_processes}，请先停止其它进程",
            )

        workdir.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv_tuple,
                cwd=str(workdir),
                env=minimal_env(env_extra),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **spawn_kwargs(),
            )
        except FileNotFoundError:
            return ProcessInfo(
                name=name, argv=argv_tuple, cwd=str(workdir),
                error=f"命令不存在：{argv_tuple[0]}",
            )
        except OSError as exc:
            return ProcessInfo(name=name, argv=argv_tuple, cwd=str(workdir), error=str(exc))

        managed = _Managed(name=name, argv=argv_tuple, cwd=workdir, proc=proc)
        managed.readers = [
            asyncio.create_task(self._pump(managed, proc.stdout, "out"), name=f"proc-{name}-out"),
            asyncio.create_task(self._pump(managed, proc.stderr, "err"), name=f"proc-{name}-err"),
        ]
        self._procs[name] = managed
        logger.info("进程已启动 name=%s pid=%s argv=%s", name, proc.pid, argv_tuple)
        return self._info(managed)

    async def stop(self, name: str, *, grace: float | None = None) -> ProcessInfo | None:
        managed = self._procs.get(name)
        if managed is None:
            return None
        if managed.running:
            await terminate_tree(managed.proc, grace=grace if grace is not None else self.cfg.grace_sec)
        await self._settle(managed)
        logger.info("进程已停止 name=%s exit=%s", name, managed.exit_code)
        return self._info(managed)

    async def stop_all(self) -> int:
        names = [name for name, m in self._procs.items() if m.running]
        for name in names:
            await self.stop(name)
        return len(names)

    @staticmethod
    def _readers_done(managed: _Managed) -> bool:
        """读取任务全部结束 ⇒ 管道 EOF ⇒ 进程已退出（或正在退出）。"""
        return bool(managed.readers) and all(t.done() for t in managed.readers)

    async def reap(self) -> list[ProcessInfo]:
        """回收已退出的进程（由 orchestrator 的周期任务调用）。

        判据必须**同时**看两个信号：

        * ``proc.returncode`` —— 由 transport 的 process_exited 回调设置，
          可能比读取端晚一拍；
        * 读取任务是否全部结束 —— 管道 EOF 是"进程已关闭输出"的直接证据。

        只看 ``returncode`` 会把刚退出的进程一直当成"运行中"，
        导致并发计数虚高、后续 start 被"已达上限"误拒（这是实测踩出来的 bug）。
        """
        finished: list[ProcessInfo] = []
        for managed in self._procs.values():
            if managed.exit_code is not None:
                continue
            if managed.proc.returncode is None and not self._readers_done(managed):
                continue
            await self._settle(managed)
            finished.append(self._info(managed))
        return finished

    async def _settle(self, managed: _Managed) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(managed.proc.wait(), timeout=2.0)
        managed.exit_code = managed.proc.returncode
        for task in managed.readers:
            if not task.done():
                task.cancel()
        if managed.readers:
            await asyncio.gather(*managed.readers, return_exceptions=True)

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #

    async def _pump(
        self, managed: _Managed, stream: asyncio.StreamReader | None, tag: str
    ) -> None:
        """后台把子进程输出搬进环形缓冲。

        用分块 read + 手动切行，而不是 ``readline()`` ——
        后者遇到超长行（没有换行符的进度条之类）会抛 ``LimitOverrunError``。
        """
        if stream is None:
            return
        remainder = ""
        try:
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    break
                text = remainder + decode_output(chunk)
                lines = text.split("\n")
                remainder = lines.pop()
                for line in lines:
                    cleaned = strip_ansi(line).rstrip("\r")
                    if cleaned:
                        managed.append(f"[{tag}] {cleaned}", max_bytes=self.cfg.max_log_bytes)
            if remainder.strip():
                managed.append(
                    f"[{tag}] {strip_ansi(remainder).rstrip()}", max_bytes=self.cfg.max_log_bytes
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("进程 %s 日志读取结束：%s", managed.name, exc)

    async def logs(self, name: str, *, tail_lines: int = 60) -> str:
        managed = self._procs.get(name)
        if managed is None:
            available = ", ".join(sorted(self._procs)) or "无"
            return f"没有名为 {name} 的进程（现有：{available}）"
        picked = list(managed.logs)[-max(1, tail_lines):]
        return "\n".join(picked) if picked else "（暂无输出）"

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def list(self) -> list[ProcessInfo]:
        return [self._info(m) for m in sorted(self._procs.values(), key=lambda x: x.started_at)]

    def get(self, name: str) -> ProcessInfo | None:
        managed = self._procs.get(name)
        return self._info(managed) if managed else None

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.enabled,
            "max_processes": self.cfg.max_processes,
            "tracked": len(self._procs),
            "running": len(self._live()),
            "processes": [
                {
                    "name": i.name,
                    "pid": i.pid,
                    "running": i.running,
                    "exit_code": i.exit_code,
                    "uptime_sec": i.uptime_sec,
                    "log_lines": i.log_lines,
                }
                for i in self.list()
            ],
        }

    @staticmethod
    def render(info: ProcessInfo) -> str:
        if info.error:
            return f"✗ {info.name}: {info.error}"
        state = "运行中" if info.running else f"已退出(exit={info.exit_code})"
        return (
            f"{info.name}  {state}  pid={info.pid}  {info.uptime_sec:.0f}s  "
            f"日志 {info.log_lines} 行/{info.log_bytes} 字节\n  $ {' '.join(info.argv)}"
        )
