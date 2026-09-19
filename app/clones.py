"""分身系统 —— 多子 Agent 并发调度。

==============================================================================
一、什么是"分身"
==============================================================================
分身不是"再起一个模型进程"，而是**同一套 runtime 上的一份隔离执行上下文**。
一个分身由 :class:`app.contracts.CloneSpec` 描述，隔离体现在四个正交维度：

======================  ==========================  ==============================
维度                     隔离手段                     解决什么问题
======================  ==========================  ==============================
上下文隔离               每个任务一条独立 Codex thread   长任务的历史不污染主对话
文件系统隔离             每个分身一个独立 workspace 目录   写文件不会越界到别人的工作区
工具面隔离               ``tools_allowlist`` 白名单      按最小权限原则收紧能力面
记忆隔离                 每个分身写自己的 ``wing``        领域知识不串味
======================  ==========================  ==============================

四者合起来的效果是：**分身之间没有共享可变状态**。这个性质不是锦上添花，
它是下面"水平扩展"成立的前提。

==============================================================================
二、任务隔离带来的三个工程优势
==============================================================================
1. **故障不外溢**。一个分身卡死/超时/报错，只影响它自己那条任务；
   主对话线程毫发无伤（因为压根不在同一条 thread 上）。
2. **可重投递**。任务对象 :class:`CloneTask` 不持有任何"属于某个分身"的字段，
   所以一个失败的任务可以原样丢回队列，交给另一个分身重跑。
   如果任务持有执行者状态，这一步就做不到。
3. **权限可分级**。定时任务多半不需要写权限，就给它 ``read_only``；
   只有明确要做代码改动的分身才给 ``workspace_write``。
   权限跟着分身走，而不是跟着进程走。

==============================================================================
三、水平扩展机制
==============================================================================
扩展的基本单位是 **worker**（一个消费队列的协程），而不是进程。
新增一个 worker 就多一份并发能力，因为：

* 任务无状态 → 任意 worker 都能处理任意任务，不存在"任务亲和性"约束；
* 分身的隔离边界由 ``cwd`` / ``tools_allowlist`` 决定 → 扩容时只需复制
  ``CloneSpec``，不需要为新 worker 做任何特殊配置；
* 真正的外部瓶颈（模型 API 速率）由两个信号量统一控制：
  ``max_parallel_tasks``（全局）与 ``CloneSpec.max_concurrency``（单分身），
  所以"多开 worker"不会把上游打爆。

==============================================================================
四、调度机制
==============================================================================
三层结构::

    优先级队列  →  worker 池  →  每分身信号量  →  带超时/重试的执行
    (priority)     (全局并发)     (单角色并发)      (timeout + retry)

* **优先级队列**：``CloneTask.priority`` 数字越小越先被执行
  （用户交互类=0，常规后台=5，定时巡检=8），保证交互不被后台任务饿死。
* **超时**：任务级 ``timeout_sec`` 与全局默认值取 min，超时记 ``TIMEOUT`` 状态。
* **重试**：仅对 ``FAILED`` 生效（超时不重试 —— 超时说明负载已满，
  重试只会加剧雪崩），最多 ``max_retries`` 次。
* **cron**：内置极简 5 字段解析器，不引入 APScheduler。
  ``分 时 日 月 周``，支持 ``*`` / ``*/n`` / ``a,b`` / ``a-b``。
  每 15 秒 tick 一次，到点就 ``submit()`` 一个任务 —— 定时器只负责**投递**，
  不负责执行，所以长任务不会堵住调度心跳。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from app.config import SchedulerConfig
from app.contracts import (
    CloneResult,
    CloneSpec,
    CloneTask,
    IsolatedRunnerPort,
    TaskState,
    utcnow,
)

logger = logging.getLogger(__name__)


# =========================================================================== #
# Cron 表达式
# =========================================================================== #


class CronExpr:
    """极简 5 字段 cron 解析器：``分 时 日 月 周``。

    支持语法：``*``、``*/n``、``a,b,c``、``a-b``、``a-b/n``。
    周字段 0 与 7 都表示周日（遵循 cron 惯例）。
    """

    __slots__ = ("minute", "hour", "dom", "month", "dow", "raw")

    def __init__(self, expr: str) -> None:
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"cron 表达式必须是 5 个字段，收到 {len(fields)}: {expr!r}")
        self.raw = expr
        self.minute = self._parse(fields[0], 0, 59)
        self.hour = self._parse(fields[1], 0, 23)
        self.dom = self._parse(fields[2], 1, 31)
        self.month = self._parse(fields[3], 1, 12)
        self.dow = self._parse(fields[4], 0, 7)

    @staticmethod
    def _parse(field: str, lo: int, hi: int) -> set[int]:
        out: set[int] = set()
        for part in field.split(","):
            part = part.strip()
            if not part:
                continue
            step = 1
            if "/" in part:
                part, _, raw_step = part.partition("/")
                step = max(1, int(raw_step))
            if part in ("*", "?"):
                start, end = lo, hi
            elif "-" in part:
                a, _, b = part.partition("-")
                start, end = int(a), int(b)
            else:
                start = end = int(part)
            out.update(range(start, end + 1, step))
        return out

    def matches(self, moment: datetime) -> bool:
        # cron 的周日=0；Python 的 weekday() 是周一=0，所以平移一位
        dow = (moment.weekday() + 1) % 7
        dow_ok = dow in self.dow or (dow == 0 and 7 in self.dow)
        return (
            moment.minute in self.minute
            and moment.hour in self.hour
            and moment.day in self.dom
            and moment.month in self.month
            and dow_ok
        )

    def next_after(self, after: datetime) -> datetime | None:
        """求下一次触发时刻。逐分钟推进，最多找 366 天。"""
        cursor = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = cursor + timedelta(days=366)
        while cursor < limit:
            if self.matches(cursor):
                return cursor
            cursor += timedelta(minutes=1)
        return None


@dataclass(slots=True)
class _CronJob:
    job_id: str
    name: str
    expr: CronExpr
    factory: Callable[[], CloneTask]
    next_run: datetime | None = None
    fired_count: int = 0


# =========================================================================== #
# 分身系统
# =========================================================================== #


class CloneSystem:
    """实现 :class:`app.contracts.SchedulerPort`。

    构造时注入 :class:`app.contracts.IsolatedRunnerPort`（通常就是主脑），
    于是"谁来跑这个 prompt"和"什么时候跑、跑几个"被彻底解耦。
    """

    def __init__(
        self,
        cfg: SchedulerConfig,
        runner: IsolatedRunnerPort,
        clones: Sequence[CloneSpec],
    ) -> None:
        if not clones:
            raise ValueError("至少需要一个 CloneSpec，否则没有可用的分身")
        self.cfg = cfg
        self._runner = runner
        self._clones: dict[str, CloneSpec] = {}
        for spec in clones:
            spec.workspace_dir = spec.workspace_dir or (cfg.clone_root / spec.clone_id)
            spec.workspace_dir.mkdir(parents=True, exist_ok=True)
            self._clones[spec.clone_id] = spec

        # 优先队列：(priority, 序号, 任务) —— 序号保证同级 FIFO 且元素可比较
        self._queue: asyncio.PriorityQueue[tuple[int, int, CloneTask]] = asyncio.PriorityQueue(
            maxsize=cfg.max_queue_size
        )
        self._counter = itertools.count()
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, asyncio.Task[None]] = {}
        self._results: dict[str, CloneResult] = {}
        self._completed: list[str] = []
        self._clone_semaphores: dict[str, asyncio.Semaphore] = {
            cid: asyncio.Semaphore(max(1, spec.max_concurrency))
            for cid, spec in self._clones.items()
        }
        self._cron_jobs: dict[str, _CronJob] = {}
        self._tick_task: asyncio.Task[None] | None = None
        self._finish_hook: Callable[[CloneTask, CloneResult], Any] | None = None
        self._stopping = False
        self._rr_index = 0  # 轮询选分身用

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        for index in range(max(1, self.cfg.max_parallel_tasks)):
            task = asyncio.create_task(self._worker(index), name=f"clone-worker-{index}")
            self._workers.append(task)
        self._tick_task = asyncio.create_task(self._tick_loop(), name="clone-cron")
        logger.info(
            "分身系统启动：%d 个 worker，%d 个分身角色",
            len(self._workers), len(self._clones),
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._tick_task:
            self._tick_task.cancel()
        for _ in self._workers:
            await self._queue.put((-1, next(self._counter), _STOP_TASK))
        await asyncio.gather(*(t for t in self._running.values()), return_exceptions=True)
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    # ------------------------------------------------------------------ #
    # 提交与查询
    # ------------------------------------------------------------------ #

    async def submit(self, task: CloneTask) -> str:
        """投递任务。返回 task_id，立即返回不阻塞。"""
        if self._stopping:
            raise RuntimeError("分身系统正在关闭，拒绝新任务")
        if task.clone_id is not None and task.clone_id not in self._clones:
            raise ValueError(f"未知分身: {task.clone_id}")

        task_id = task.task_id or f"task_{uuid.uuid4().hex[:12]}"
        task.task_id = task_id
        self._results[task_id] = CloneResult(
            task_id=task_id,
            clone_id=task.clone_id or "-",
            state=TaskState.QUEUED,
        )
        try:
            self._queue.put_nowait((task.priority, next(self._counter), task))
        except asyncio.QueueFull:
            self._results[task_id].state = TaskState.FAILED
            self._results[task_id].error = "任务队列已满"
            raise
        logger.info(
            "任务入队 id=%s kind=%s priority=%d clone=%s",
            task_id, task.kind, task.priority, task.clone_id or "auto",
        )
        return task_id

    async def result(self, task_id: str) -> CloneResult | None:
        return self._results.get(task_id)

    async def cancel(self, task_id: str) -> bool:
        running = self._running.get(task_id)
        if running is not None:
            running.cancel()
            return True
        record = self._results.get(task_id)
        if record is not None and record.state is TaskState.QUEUED:
            record.state = TaskState.CANCELLED
            record.finished_at = utcnow()
            return True
        return False

    def recent_results(self, limit: int = 20) -> list[CloneResult]:
        ids = self._completed[-limit:][::-1]
        return [self._results[i] for i in ids if i in self._results]

    # ------------------------------------------------------------------ #
    # worker 主体
    # ------------------------------------------------------------------ #

    async def _worker(self, index: int) -> None:
        while True:
            _, _, task = await self._queue.get()
            if task is _STOP_TASK:
                return
            try:
                await self._execute(task)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("worker#%d 执行任务 %s 时未捕获异常", index, task.task_id)

    async def _execute(self, task: CloneTask) -> None:
        spec = self._pick_clone(task)
        record = self._results[task.task_id]
        record.clone_id = spec.clone_id
        record.state = TaskState.RUNNING
        record.started_at = utcnow()

        timeout = min(spec.timeout_sec, task.timeout_sec or self.cfg.clone_timeout_sec)
        semaphore = self._clone_semaphores[spec.clone_id]

        async with semaphore:
            attempts = 0
            while True:
                attempts += 1
                record.attempts = attempts
                try:
                    current = asyncio.current_task()
                    if current is not None:
                        self._running[task.task_id] = current
                    output = await asyncio.wait_for(
                        self._runner.run_isolated(task.prompt, clone=spec), timeout=timeout
                    )
                    record.state = TaskState.DONE
                    record.output = output
                    record.error = None
                    break
                except asyncio.TimeoutError:
                    # 超时说明负载已满，重试只会加剧雪崩 → 直接判失败
                    record.state = TaskState.TIMEOUT
                    record.error = f"超时（{timeout:.0f}s）"
                    logger.warning("任务 %s 超时，分身=%s", task.task_id, spec.clone_id)
                    break
                except asyncio.CancelledError:
                    record.state = TaskState.CANCELLED
                    record.error = "已取消"
                    raise
                except Exception as exc:  # noqa: BLE001
                    record.error = str(exc)
                    if attempts > self.cfg.max_retries:
                        record.state = TaskState.FAILED
                        logger.error("任务 %s 失败（尝试 %d 次）: %s", task.task_id, attempts, exc)
                        break
                    logger.warning("任务 %s 第 %d 次失败，重试: %s", task.task_id, attempts, exc)
                    await asyncio.sleep(min(2.0 * attempts, 10.0))
                finally:
                    self._running.pop(task.task_id, None)

        record.finished_at = utcnow()
        self._completed.append(task.task_id)
        logger.info(
            "任务完成 id=%s state=%s 耗时=%.1fs",
            task.task_id, record.state.value, record.elapsed_sec,
        )
        await self._on_finished(task, record)

    def _pick_clone(self, task: CloneTask) -> CloneSpec:
        """选分身：显式指定优先；否则按角色轮询，负载均衡交给信号量。"""
        if task.clone_id:
            return self._clones[task.clone_id]
        ids = list(self._clones)
        # 定时巡检类任务优先给并发额度最充裕的角色
        spec = self._clones[ids[self._rr_index % len(ids)]]
        self._rr_index += 1
        return spec

    async def _on_finished(self, task: CloneTask, record: CloneResult) -> None:
        """收尾钩子：留一个扩展点给 orchestrator（比如把结果播报回聊天）。"""
        self._finish_hook: Callable[[CloneTask, CloneResult], Any] | None = getattr(
            self, "_finish_hook", None
        )
        if callable(self._finish_hook):
            try:
                outcome = self._finish_hook(task, record)
                if asyncio.iscoroutine(outcome):
                    await outcome
            except Exception:  # noqa: BLE001
                logger.exception("收尾钩子执行失败")

    def set_finish_hook(
        self, hook: Callable[[CloneTask, CloneResult], Any] | None
    ) -> None:
        """注册任务完成回调（orchestrator 用它把结果推回 Telegram）。"""
        self._finish_hook = hook

    # ------------------------------------------------------------------ #
    # cron 调度
    # ------------------------------------------------------------------ #

    def add_cron(self, name: str, expr: str, factory: Callable[[], CloneTask]) -> str:
        """注册一个定时任务。``factory`` 每次触发时生成新任务（避免复用同一对象）。"""
        job_id = f"cron_{uuid.uuid4().hex[:8]}"
        cron = CronExpr(expr)
        job = _CronJob(
            job_id=job_id,
            name=name,
            expr=cron,
            factory=factory,
            next_run=cron.next_after(datetime.now(timezone.utc)),
        )
        self._cron_jobs[job_id] = job
        logger.info("注册定时任务 %s（%s），下次触发 %s", name, expr, job.next_run)
        return job_id

    def remove_cron(self, job_id: str) -> bool:
        return self._cron_jobs.pop(job_id, None) is not None

    async def _tick_loop(self) -> None:
        """调度心跳：只负责"到点投递"，绝不在此处执行任务。"""
        interval = 15.0
        while True:
            try:
                await asyncio.sleep(interval)
                now = datetime.now(timezone.utc)
                for job in list(self._cron_jobs.values()):
                    if job.next_run is None:
                        job.next_run = job.expr.next_after(now)
                        continue
                    if job.next_run <= now:
                        task = job.factory()
                        await self.submit(task)
                        job.fired_count += 1
                        job.next_run = job.expr.next_after(now)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("cron tick 异常")

    # ------------------------------------------------------------------ #
    # 可观测性
    # ------------------------------------------------------------------ #

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for record in self._results.values():
            counts[record.state.value] = counts.get(record.state.value, 0) + 1
        return {
            "clones": {
                cid: {
                    "name": spec.name,
                    "sandbox": spec.sandbox,
                    "max_concurrency": spec.max_concurrency,
                    "tools": list(spec.tools_allowlist),
                    "workspace": str(spec.workspace_dir),
                }
                for cid, spec in self._clones.items()
            },
            "workers": len(self._workers),
            "queue_size": self._queue.qsize(),
            "running": len(self._running),
            "task_states": counts,
            "cron_jobs": {
                j.job_id: {
                    "name": j.name,
                    "expr": j.expr.raw,
                    "next_run": j.next_run.isoformat() if j.next_run else None,
                    "fired": j.fired_count,
                }
                for j in self._cron_jobs.values()
            },
        }


# 用于优雅停止的哨兵任务
_STOP_TASK = CloneTask(task_id="__stop__", prompt="", clone_id=None)
