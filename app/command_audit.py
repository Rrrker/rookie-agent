"""命令审计 —— 把 Codex **自己跑过的命令**抽出来写进长期记忆。

==============================================================================
为什么这件事是"零成本"的
==============================================================================
我们**不需要**给 Codex 加 shell 工具 —— 它本来就有。
而且它跑过的每条命令都躺在 ``TurnResult.items`` 里，类型是
``CommandExecutionThreadItem``（已实测的字段：``command`` / ``cwd`` / ``status`` /
``aggregated_output`` / ``exit_code`` / ``duration_ms``）。

所以审计只是"筛出来 + 脱敏 + 写库"，**不需要新增任何工具或拦截层**。
这正是"先核实"的价值：原先计划里那个 shell MCP server 可以直接删掉。

==============================================================================
两个必须做对的地方
==============================================================================
1. **脱敏。** 命令里经常带凭据（``curl -H "Authorization: Bearer sk-..."``）。
   原样写进记忆等于把密钥永久留在磁盘上。
2. **写库不能阻塞回复。** 由调用方 ``asyncio.create_task`` 后台跑。

==============================================================================
一个有用的实现细节
==============================================================================
``ThreadItem`` 是 pydantic ``RootModel``（20 个变体的 tagged union），
所以真正的条目在 ``item.root`` 里，判别符是 ``type`` 字段的字面量值。
字段是 snake_case 名字 + camelCase alias，两种都取一遍最稳。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Sequence

from app.config import ExecConfig
from app.contracts import CommandTrace, MemoryPort, MemoryWrite

logger = logging.getLogger(__name__)

_COMMAND_TYPE = "commandExecution"
_FILE_CHANGE_TYPE = "fileChange"


# --------------------------------------------------------------------------- #
# 抽取
# --------------------------------------------------------------------------- #


def _unwrap(item: Any) -> Any:
    """pydantic RootModel 的外层剥掉，露出真正的变体对象。"""
    return getattr(item, "root", item)


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    """按候选名字取值，兼容 snake_case 与 camelCase，也兼容 dict。"""
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return default


def _enum_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    inner = getattr(value, "value", None)
    if isinstance(inner, str):
        return inner
    return str(value)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    inner = getattr(value, "root", value)
    return str(inner)


def item_kind(item: Any) -> str:
    """取条目的判别符（``commandExecution`` / ``fileChange`` / ``agentMessage`` …）。"""
    return _enum_str(_field(_unwrap(item), "type", default="")) or ""


def extract_commands(items: Iterable[Any] | None) -> list[CommandTrace]:
    """从一轮的 items 里筛出所有命令执行记录。"""
    traces: list[CommandTrace] = []
    for raw in items or ():
        item = _unwrap(raw)
        if item_kind(item) != _COMMAND_TYPE:
            continue
        traces.append(
            CommandTrace(
                command=str(_field(item, "command", default="") or ""),
                cwd=_as_text(_field(item, "cwd")),
                status=_enum_str(_field(item, "status", default="unknown")) or "unknown",
                exit_code=_as_int(_field(item, "exit_code", "exitCode")),
                output=str(_field(item, "aggregated_output", "aggregatedOutput", default="") or ""),
                duration_ms=_as_int(_field(item, "duration_ms", "durationMs")),
            )
        )
    return traces


def extract_file_changes(items: Iterable[Any] | None) -> list[str]:
    """顺手把文件改动也抽出来（同一个 items 里就有，免费）。"""
    changes: list[str] = []
    for raw in items or ():
        item = _unwrap(raw)
        if item_kind(item) != _FILE_CHANGE_TYPE:
            continue
        for change in _field(item, "changes", default=[]) or []:
            path = _as_text(_field(change, "path"))
            kind = _enum_str(_field(change, "kind", default="change")) or "change"
            if path:
                changes.append(f"{kind}:{path}")
    return changes


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"\bghp_[A-Za-z0-9]{8,}"), "ghp_***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}"), "github_pat_***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***"),
    (
        re.compile(
            r"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret)"
            r"\s*[=:]\s*)([^\s'\"]{4,})"
        ),
        r"\1***",
    ),
    (re.compile(r"(?i)(authorization:\s*)[^\s'\"]+"), r"\1***"),
)


def redact(text: str) -> str:
    """掩掉命令与输出里的凭据形态。宁可误伤也不要漏。"""
    if not text:
        return ""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def render_traces(traces: Sequence[CommandTrace], *, output_chars: int = 300) -> str:
    """把命令列表压成一段便于检索的文本。"""
    lines: list[str] = []
    for index, trace in enumerate(traces, 1):
        bits = [f"{index}. $ {trace.command}"]
        mark = "✓" if trace.exit_code == 0 else ("✗" if trace.exit_code is not None else "·")
        bits.append(f"   {mark} status={trace.status}")
        if trace.exit_code is not None:
            bits.append(f"exit={trace.exit_code}")
        if trace.duration_ms is not None:
            bits.append(f"{trace.duration_ms}ms")
        if trace.cwd:
            bits.append(f"cwd={trace.cwd}")
        head = "  ".join(bits[:1]) + "  [" + " ".join(bits[1:]) + "]"
        lines.append(head)
        excerpt = (trace.output or "").strip()
        if excerpt:
            snippet = excerpt[:output_chars]
            lines.append(f"   ↳ {snippet}" + ("…" if len(excerpt) > output_chars else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 审计器
# --------------------------------------------------------------------------- #


class CommandAuditor:
    """实现"命令 → 长期记忆"的落库。

    一次写入**一条** drawer（整轮的命令合在一起），而不是每条命令一条 ——
    保留"同一轮里都干了什么"这层语义，检索时更有用，写入次数也更少。
    """

    def __init__(
        self,
        memory: MemoryPort,
        cfg: ExecConfig,
        *,
        wing: str,
        room: str | None = None,
    ) -> None:
        self._memory = memory
        self.cfg = cfg
        self.wing = wing
        self.room = room or cfg.audit_room

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.audit_to_memory)

    async def record(
        self,
        *,
        session_id: str,
        traces: Sequence[CommandTrace],
        file_changes: Sequence[str] = (),
        note: str = "",
    ) -> int:
        """把一轮的命令写入记忆。返回写入条数（0 或 1）。"""
        if not self.enabled or (not traces and not file_changes):
            return 0

        blocks = [f"[session={session_id}] 本轮执行了 {len(traces)} 条命令"]
        if note:
            blocks.append(f"用户意图：{note}")
        if traces:
            blocks.append("命令记录：\n" + render_traces(traces, output_chars=self.cfg.audit_output_chars))
        if file_changes:
            blocks.append("文件改动：" + "，".join(file_changes[:20]))

        content = redact("\n".join(blocks))

        try:
            await self._memory.remember(
                MemoryWrite(
                    content=content,
                    wing=self.wing,
                    room=self.room,
                    source_file=f"shell/{session_id}",
                    added_by="command-auditor",
                )
            )
            logger.info("命令审计已写入记忆（%d 条命令）", len(traces))
            return 1
        except Exception:  # noqa: BLE001
            # 审计失败绝不能影响主流程
            logger.exception("命令审计写入失败")
            return 0

    async def search(self, query: str, *, k: int = 5) -> list[Any]:
        """检索历史命令记录（给 /audit 指令用）。"""
        return await self._memory.recall(query, wing=self.wing, room=self.room, k=k)
