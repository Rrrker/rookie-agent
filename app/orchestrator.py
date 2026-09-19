"""编排层 —— 把各模块串成一条流水线。

==============================================================================
为什么这一层要独立存在
==============================================================================
各模块只认 :mod:`app.contracts`，彼此不知道对方存在。"先后顺序"这个知识必然存在于某处 ——
与其散落在各模块里互相调用，不如集中到这一个文件。好处是**流程可以整条替换而不动任何模块**。

==============================================================================
一轮对话的完整流水线
==============================================================================
::

    Telegram 入站
        │
        ├─1─ 情绪评估   emotion.appraise_text(用户原话)
        ├─2─ 多模态路由  media.ingest(附件)
        ├─3─ 记忆召回   memory.build_context(用户意图)
        ├─4─ 情绪指令   emotion.style_directive()
        ├─5─ 主脑推理   brain.reply(..., trace=...)   ← 顺带回收它执行的命令
        ├─6─ 结果回灌   emotion（仅失败/超时，成功不回灌）
        ├─7─ 语音合成   media.synthesize()            ← 仅当用户用语音提问
        ├─8─ 出站发送   channel.send()
        ├─9─ 异步落库   asyncio.create_task(_persist) ← 逐字记忆 + 命令审计
        └─10 内部心跳   _internal_tick()              ← 进程回收 + 白名单探针

**第 9 步刻意异步化**：写记忆的耗时不该计入用户等待。

==============================================================================
关于"执行能力"的三条路径（别搞混）
==============================================================================
====================  ================================  ==========================
谁在跑                用什么                            约束
====================  ================================  ==========================
模型                  Codex 内置 shell 工具              Codex 的 OS 级沙箱
我们自己的代码         LocalExecutor（白名单命令）        不受沙箱约束→只给白名单
我们自己的代码         ProcessManager（长驻进程）         不受沙箱约束→默认关闭
====================  ================================  ==========================

模型跑过的命令由第 9 步从 ``TurnResult.items`` 里筛出来写进记忆 —— 不新增任何工具。

指令集：``/help`` ``/status`` ``/emotion`` ``/memory`` ``/audit`` ``/task``
``/exec`` ``/proc`` ``/voice`` ``/reset``
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from pathlib import Path
from typing import Any, Sequence

from app.brain import CodexBrain
from app.clones import CloneSystem
from app.command_audit import CommandAuditor
from app.config import AppConfig
from app.contracts import (
    CloneResult,
    CloneSpec,
    CloneTask,
    CommandTrace,
    InboundMessage,
    OutboundMessage,
    TaskState,
)
from app.emotion import PadEmotionEngine
from app.local_exec import LocalExecutor, summarize
from app.mcp_hub import McpHub
from app.media_router import MediaRouter
from app.memory import MemoryGateway
from app.process_manager import ProcessManager

logger = logging.getLogger(__name__)

# 内部心跳：每 _PROBE_EVERY_TICKS 个 tick 跑一次状态探针
_TICK_INTERVAL = 60.0
_PROBE_EVERY_TICKS = 5


# --------------------------------------------------------------------------- #
# 默认分身角色
# --------------------------------------------------------------------------- #


def default_clones() -> list[CloneSpec]:
    """三个分身，权限逐级收紧 —— 最小权限的落地样例。"""
    return [
        CloneSpec(
            clone_id="researcher",
            name="调研员",
            system_prompt=(
                "你是调研分身。任务是把一个问题查清楚并给出可核查的结论。\n"
                "输出格式：结论（3 句以内）→ 依据 → 不确定的部分。\n"
                "不要编造来源。没有工具可用时，明确说明结论来自你的既有知识。"
            ),
            tools_allowlist=(),
            sandbox="read_only",
            max_concurrency=2,
            timeout_sec=300.0,
            wing="wing_agent",
        ),
        CloneSpec(
            clone_id="coder",
            name="工程分身",
            system_prompt=(
                "你是工程分身。任务是给出可直接运行的代码改动方案。\n"
                "必须说明：改哪个文件、改成什么、如何验证。\n"
                "不要顺手重构无关代码。"
            ),
            tools_allowlist=("media_tools__media_probe",),
            sandbox="workspace_write",
            max_concurrency=1,
            timeout_sec=600.0,
            wing="wing_agent",
        ),
        CloneSpec(
            clone_id="librarian",
            name="记忆管家",
            system_prompt=(
                "你是记忆管理分身。任务是整理与归纳长期记忆。\n"
                "只做归纳与去重建议，不产生新事实，不修改原始记录。"
            ),
            tools_allowlist=("mempalace__mempalace_search", "mempalace__mempalace_status"),
            sandbox="read_only",
            max_concurrency=1,
            timeout_sec=240.0,
            wing="wing_agent",
        ),
    ]


# --------------------------------------------------------------------------- #
# 编排器
# --------------------------------------------------------------------------- #


class Orchestrator:
    """把渠道 / 记忆 / 情绪 / 多模态 / 工具 / 主脑 / 分身 / 执行 装配成可运行系统。

    三个执行相关依赖是**可选**的：传 ``None`` 时对应能力优雅关闭。
    这不是图省事 —— 它让 ``Orchestrator`` 在测试里可以只装配需要的部分，
    同时避免它自己去 new 具体实现（那会破坏"只有 main.py 做依赖注入"的约定）。
    """

    def __init__(
        self,
        cfg: AppConfig,
        *,
        channel: Any,
        hub: McpHub,
        memory: MemoryGateway,
        emotion: PadEmotionEngine,
        media: MediaRouter,
        brain: CodexBrain,
        clones: CloneSystem,
        executor: LocalExecutor | None = None,
        processes: ProcessManager | None = None,
        auditor: CommandAuditor | None = None,
    ) -> None:
        self.cfg = cfg
        self.channel = channel
        self.hub = hub
        self.memory = memory
        self.emotion = emotion
        self.media = media
        self.brain = brain
        self.clones = clones
        self.executor = executor
        self.processes = processes
        self.auditor = auditor

        self._voice_reply: dict[str, bool] = {}
        self._pending: set[asyncio.Task[Any]] = set()
        self._tick_task: asyncio.Task[None] | None = None
        self._last_probe: Any = None
        self._audited_commands = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        await self.hub.start()
        await self.memory.flush_outbox()
        await self.brain.start()
        await self.clones.start()
        self.clones.set_finish_hook(self._on_clone_finished)
        self._register_cron()

        logger.info("记忆后端：%s", json.dumps(self.memory.backend_report(), ensure_ascii=False))
        logger.info("工具枢纽：%s", json.dumps(self.hub.health(), ensure_ascii=False))
        logger.info(
            "执行层：内部命令=%s / 长驻进程=%s / 命令审计=%s",
            "开启" if (self.executor and self.cfg.exec.enabled) else "关闭",
            "开启" if (self.processes and self.cfg.process.enabled) else "关闭（默认）",
            "开启" if (self.auditor and self.auditor.enabled) else "关闭",
        )

        self._tick_task = asyncio.create_task(self._internal_tick(), name="internal-tick")
        await self.channel.start(self.handle)

    async def stop(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            self._tick_task = None
        for task in list(self._pending):
            task.cancel()
        await asyncio.gather(*self._pending, return_exceptions=True)
        await self.channel.stop()
        await self.clones.stop()
        if self.processes is not None:
            stopped = await self.processes.stop_all()
            if stopped:
                logger.info("已停止 %d 个长驻进程", stopped)
        await self.brain.close()
        await self.hub.stop()

    def _spawn(self, coro: Any) -> None:
        """后台任务（记忆落库等），登记以便优雅关闭时取消。"""
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    # ------------------------------------------------------------------ #
    # 主流水线
    # ------------------------------------------------------------------ #

    async def handle(self, msg: InboundMessage) -> None:
        text = msg.text.strip()

        if text.startswith("/") and await self._handle_command(msg, text):
            return

        # ── 1. 情绪评估（用用户原话；附件还没转成文字时先跳过）
        if text:
            self.emotion.appraise_text(text)
            logger.info("情绪状态 %s", self.emotion.current().as_dict())

        # ── 2. 多模态路由
        notes, spoken = await self.media.ingest(msg.media) if msg.media else ([], "")
        user_text = text or spoken or ""

        # ── 3. 记忆召回
        memory_context = ""
        if user_text:
            try:
                memory_context = await self.memory.build_context(
                    user_text, wing=self.cfg.memory.default_wing
                )
            except Exception:  # noqa: BLE001
                logger.exception("记忆召回失败，继续无记忆回复")

        # ── 4. 情绪指令
        directive = self.emotion.style_directive()

        # ── 5. 主脑推理（顺带回收它执行的命令）
        await self.channel.send_typing(msg.chat_id)
        trace: list[CommandTrace] = []
        changed: list[str] = []
        answer = await self.brain.reply(
            session_id=msg.chat_id,
            text=user_text,
            memory_context=memory_context,
            style_directive=directive,
            media_notes=notes,
            trace=trace,
            changed_files=changed,
        )

        # ── 6. 结果回灌情绪
        # 注意：**正常回复不产生正向情绪事件**。顺利应答是默认预期，不是值得高兴的事；
        # 每轮都加一次 task_success 会让情绪瞬间顶到上限并永久卡在正向象限，语气调节随之失效。
        if answer.startswith("调用模型时出错了") or answer.startswith("这一轮处理超时了"):
            self.emotion.on_task_outcome(success=False, detail=answer[:60])

        # ── 7. 语音合成（同模态回复）
        voice_path: Path | None = None
        want_voice = self._voice_reply.get(msg.chat_id, False) or self.media.should_reply_with_voice(
            msg.media
        )
        if want_voice and answer:
            voice_path = await self.media.synthesize(answer, voice=self.cfg.media.tts_voice)

        # ── 8. 出站
        await self.channel.send(
            OutboundMessage(
                chat_id=msg.chat_id,
                text=answer,
                voice_path=voice_path,
                reply_to=msg.message_id,
                meta={"emotion": self.emotion.current().as_dict(), "commands": len(trace)},
            )
        )
        if trace:
            logger.info("本轮 Codex 执行了 %d 条命令，改动 %d 个文件", len(trace), len(changed))

        # ── 9. 异步落库（不阻塞用户感知）
        if (self.cfg.memory.persist_turns and user_text) or trace or changed:
            self._spawn(
                self._persist(
                    chat_id=msg.chat_id,
                    user_text=user_text,
                    assistant_text=answer,
                    trace=trace,
                    changed=changed,
                )
            )

    # ------------------------------------------------------------------ #
    # 记忆写入（后台）
    # ------------------------------------------------------------------ #

    async def _persist(
        self,
        *,
        chat_id: str,
        user_text: str,
        assistant_text: str,
        trace: Sequence[CommandTrace] = (),
        changed: Sequence[str] = (),
    ) -> None:
        if self.cfg.memory.persist_turns and user_text:
            try:
                await self.memory.remember_turn(
                    chat_id=chat_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    wing=self.cfg.memory.default_wing,
                    emotion=self.emotion.current().as_dict(),
                )
            except Exception:  # noqa: BLE001
                logger.exception("落库逐字记忆失败")

        # 命令审计：不是"新能力"，只是把 Codex 本来就产出的条目落库
        if self.auditor is not None and (trace or changed):
            try:
                written = await self.auditor.record(
                    session_id=chat_id,
                    traces=trace,
                    file_changes=changed,
                    note=user_text[:200],
                )
                if written:
                    self._audited_commands += len(trace)
            except Exception:  # noqa: BLE001
                logger.exception("命令审计失败")

        if not (self.cfg.memory.extract_facts and user_text):
            return
        try:
            extraction = await self.brain.extract(f"USER: {user_text}\nAGENT: {assistant_text}")
            for entity in extraction.entities:
                await self.memory.upsert_entity(entity)
            for triple in extraction.triples:
                await self.memory.assert_fact(triple)
            if extraction.triples:
                logger.info("事实抽取写入 %d 条三元组", len(extraction.triples))
        except Exception:  # noqa: BLE001
            logger.exception("事实抽取入库失败")

    # ------------------------------------------------------------------ #
    # 内部心跳：非模型消费方的执行入口
    # ------------------------------------------------------------------ #

    async def _internal_tick(self) -> None:
        """周期维护：回收长驻进程 + 跑一次**白名单**状态探针。

        这一步体现"我们自己的代码也要能执行命令"：
        用 :class:`LocalExecutor` 跑已登记的命令，**不经过模型**、不产生对话。
        之所以只允许白名单，是因为这条路径不受 Codex 沙箱约束。
        """
        rounds = 0
        while True:
            try:
                await asyncio.sleep(_TICK_INTERVAL)
                rounds += 1

                if self.processes is not None:
                    for info in await self.processes.reap():
                        logger.info("进程自然退出：%s", ProcessManager.render(info))

                if (
                    self.executor is not None
                    and self.cfg.exec.enabled
                    and rounds % _PROBE_EVERY_TICKS == 0
                ):
                    self._last_probe = await self.executor.run_named("git_status")
                    logger.debug("周期探针完成：%s", self._last_probe.error_code or "ok")
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("内部心跳异常")

    # ------------------------------------------------------------------ #
    # 分身
    # ------------------------------------------------------------------ #

    def _register_cron(self) -> None:
        """注册示例定时任务（演示"定时器只投递、分身才执行"）。"""
        self.clones.add_cron(
            "morning_brief",
            "0 9 * * *",
            lambda: CloneTask(
                task_id="",
                prompt=(
                    "生成今天的简报：用一句话说明这是自动生成的定时任务，"
                    "然后用三条要点总结你被赋予的职责和当前可用的能力边界。"
                ),
                kind="cron",
                clone_id="researcher",
                priority=8,
            ),
        )
        self.clones.add_cron(
            "memory_flush",
            "*/30 * * * *",
            lambda: CloneTask(
                task_id="", prompt="__flush_memory__", kind="cron", clone_id="librarian", priority=9
            ),
        )

    async def _on_clone_finished(self, task: CloneTask, record: CloneResult) -> None:
        success = record.state is TaskState.DONE
        self.emotion.on_task_outcome(success=success, detail=task.prompt[:60])

        if not task.notify_chat_id:
            logger.info("分身任务完成 %s（%s），无播报目标", task.task_id, record.state.value)
            return

        if success:
            body = record.output or "（无输出）"
        else:
            body = f"任务未完成：{record.error or record.state.value}"

        await self.channel.send(
            OutboundMessage(
                chat_id=task.notify_chat_id,
                text=f"⏱ 后台任务完成（{record.elapsed_sec:.0f}s）\n{body[:3000]}",
                meta={"task_id": task.task_id, "state": record.state.value},
            )
        )

    # ------------------------------------------------------------------ #
    # 指令处理
    # ------------------------------------------------------------------ #

    async def _handle_command(self, msg: InboundMessage, text: str) -> bool:
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()

        if cmd == "/help":
            await self._reply(msg, _HELP)
        elif cmd == "/status":
            await self._reply(msg, await self._render_status())
        elif cmd == "/emotion":
            await self._reply(
                msg, json.dumps(self.emotion.snapshot(), ensure_ascii=False, indent=2)
            )
        elif cmd == "/memory":
            await self._reply(msg, await self._search_memory(arg))
        elif cmd == "/audit":
            await self._reply(msg, await self._search_audit(arg))
        elif cmd == "/task":
            if not arg:
                await self._reply(msg, "用法：/task 交给分身处理的任务描述")
            else:
                task_id = await self.clones.submit(
                    CloneTask(
                        task_id="", prompt=arg, kind="background", priority=5,
                        notify_chat_id=msg.chat_id,
                    )
                )
                await self._reply(msg, f"已派发给分身，任务号 `{task_id}`。完成后我会主动告诉你。")
        elif cmd == "/exec":
            await self._reply(msg, await self._cmd_exec(arg))
        elif cmd == "/proc":
            await self._reply(msg, await self._cmd_proc(arg))
        elif cmd == "/voice":
            current = self._voice_reply.get(msg.chat_id, False)
            self._voice_reply[msg.chat_id] = (
                not current if arg in ("", "toggle") else arg == "on"
            )
            state = "开启" if self._voice_reply[msg.chat_id] else "关闭"
            await self._reply(msg, f"语音回复已{state}。")
        elif cmd == "/reset":
            await self.brain.reset_session(msg.chat_id)
            await self._reply(msg, "已开启新线程。长期记忆不受影响。")
        else:
            return False
        return True

    async def _search_memory(self, arg: str) -> str:
        if not arg:
            return "用法：/memory 检索内容"
        records = await self.memory.recall(arg, k=self.cfg.memory.recall_topk)
        if not records:
            return "没有召回到相关记忆。"
        return "\n\n".join(
            f"[{i+1}] ({r.similarity:.2f}) {r.text[:300]}" for i, r in enumerate(records)
        )

    async def _search_audit(self, arg: str) -> str:
        if not arg:
            return "用法：/audit 检索内容（例如 /audit 部署 或 /audit rm）"
        if self.auditor is None:
            return "命令审计未启用。"
        records = await self.auditor.search(arg, k=self.cfg.memory.recall_topk)
        if not records:
            return "没有找到相关的命令记录。"
        return "\n\n".join(
            f"[{i+1}] ({r.similarity:.2f})\n{r.text[:600]}" for i, r in enumerate(records)
        )

    async def _cmd_exec(self, arg: str) -> str:
        """内部执行入口 —— 只跑白名单命令，不经过模型。"""
        if self.executor is None:
            return "内部执行入口未装配。"
        if not self.cfg.exec.enabled:
            return "内部执行入口已在配置中关闭（AGENT_EXEC_ENABLED）。"
        if not arg:
            lines = ["已登记的内部命令（白名单）："]
            lines += [f"  · {name} — {desc}" for name, desc in self.executor.describe().items()]
            lines.append("\n用法：/exec <命令名>")
            lines.append("注意：这条路径不受 Codex 沙箱约束，所以只允许白名单命令。")
            return "\n".join(lines)
        return summarize(await self.executor.run_named(arg.split()[0]))

    async def _cmd_proc(self, arg: str) -> str:
        """长驻进程管理（默认关闭，因为这条路径完全没有沙箱）。"""
        if self.processes is None:
            return "长驻进程管理未装配。"
        if not self.cfg.process.enabled:
            return (
                "长驻进程功能未启用（默认关闭）。\n"
                "原因：这条路径**不受 Codex 沙箱约束**，权限等于当前进程。\n"
                "确认需要时设 `AGENT_PROCESS_ENABLED=true` 再重启。"
            )

        head, _, rest = arg.partition(" ")
        head, rest = head.lower(), rest.strip()

        if head in ("", "list", "ls"):
            infos = self.processes.list()
            if not infos:
                return ("当前没有受管的进程。\n"
                        "用法：/proc start [--name 名] <命令…> / /proc logs <名> / /proc stop <名>")
            return "\n\n".join(ProcessManager.render(i) for i in infos)

        if head == "start":
            if not rest:
                return "用法：/proc start [--name 名] <命令…>"
            name = f"proc{len(self.processes.list()) + 1}"
            if rest.startswith("--name "):
                name, _, rest = rest[len("--name "):].partition(" ")
                rest = rest.strip()
            try:
                argv = shlex.split(rest, posix=(os.name != "nt"))
            except ValueError as exc:
                return f"命令解析失败：{exc}"
            if not argv:
                return "命令为空。"
            return ProcessManager.render(await self.processes.start(name, argv))

        if head == "logs":
            if not rest:
                return "用法：/proc logs <名> [行数]"
            parts = rest.split()
            tail = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 60
            return f"--- {parts[0]} 最近 {tail} 行 ---\n" + await self.processes.logs(
                parts[0], tail_lines=tail
            )

        if head == "stop":
            if not rest:
                return "用法：/proc stop <名>"
            info = await self.processes.stop(rest)
            return ProcessManager.render(info) if info else f"没有名为 {rest} 的进程。"

        return "未知子命令。可用：list / start / logs / stop"

    async def _render_status(self) -> str:
        payload: dict[str, Any] = {
            "codex": {
                "sandbox": self.cfg.codex.sandbox,
                "approval_mode": self.cfg.codex.approval_mode,
            },
            "brain": self.brain.health(),
            "tools": self.hub.health(),
            "memory": self.memory.backend_report(),
            "memory_stats": await self.memory.stats(),
            "scheduler": {k: v for k, v in self.clones.stats().items() if k != "clones"},
            "exec": {
                "enabled": bool(self.executor and self.cfg.exec.enabled),
                "commands": self.executor.names() if self.executor else [],
                "audited_commands": self._audited_commands,
            },
            "process": self.processes.stats() if self.processes else {"enabled": False},
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2)

        if self._last_probe is not None:
            body += "\n\n【周期探针 git_status】\n" + summarize(self._last_probe, head_lines=12)
        else:
            body += "\n\n（周期探针尚未运行：每 5 分钟一次）"
        return "```json\n" + body + "\n```"

    async def _reply(self, msg: InboundMessage, text: str) -> None:
        await self.channel.send(
            OutboundMessage(chat_id=msg.chat_id, text=text, reply_to=msg.message_id)
        )


_HELP = """可用指令：
/status   各模块健康状态 + 沙箱/审批模式 + 周期探针
/emotion  当前 PAD 情绪值与语音参数
/memory <内容>  语义检索长期记忆
/audit <内容>   检索历史命令记录（Codex 跑过什么）
/task <描述>     派发给分身后台执行，完成后主动通知
/exec           列出内部白名单命令；/exec <名> 执行
/proc           长驻进程：list / start / logs / stop（默认关闭）
/voice on|off    切换语音回复
/reset    开启新对话线程（长期记忆不变）
/help     显示本帮助

直接发消息即可对话；发图片会走识图，发语音会自动转文字并语音回复。

关于执行能力：
  · 模型自己跑命令用的是 Codex 内置 shell（受其沙箱约束），
    跑过的命令会自动进审计，可用 /audit 查；
  · /exec 与 /proc 是**我们自己的代码**的入口，不受 Codex 沙箱约束，
    所以分别只开放白名单命令、且默认关闭。"""
