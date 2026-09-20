"""记忆层 —— MemPalace 适配器（ChromaDB 向量库 + SQLite 时序知识图谱）。

--------------------------------------------------------------------------
MemPalace 的两套访问面（这是本模块最关键的设计认知）
--------------------------------------------------------------------------
MemPalace 对外提供两条入口，**它们读写的是同一份磁盘数据**：

1. 进程内 Python API（直接、快、无需子进程）
   ``mempalace.searcher.search_memories``  语义检索
   ``mempalace.knowledge_graph.KnowledgeGraph``  知识图谱读写
   ``mempalace.layers.MemoryStack``  L0/L1 唤醒
2. MCP Server（``python -m mempalace.mcp_server``，29 个工具）
   给"别的 agent"用 —— 比如让 Codex 主脑自己动手记忆

所以本模块的策略是**按能力选路径**，而不是二选一：

========================================  ==================================
能力                                        走哪条路 / 为什么
========================================  ==================================
抽屉写入（drawer）                          MCP ``mempalace_add_drawer``
                                              —— 官方 Python API 未公开
                                              写入函数，MCP 工具签名是稳定的
抽屉检索（semantic search）                 进程内 ``search_memories``
                                              —— 高频调用，省掉子进程往返
知识图谱读写（entity / triple）              进程内 ``KnowledgeGraph``
                                              —— 官方公开且完整
L0/L1 唤醒（wake-up 上下文）                进程内 ``MemoryStack``
========================================  ==================================

--------------------------------------------------------------------------
数据 Schema（与 MemPalace 磁盘布局一一对应）
--------------------------------------------------------------------------
drawer（向量库，collection = ``mempalace_drawers``）
    text          逐字原文，不摘要 —— MemPalace 的核心哲学
    metadata      wing / room / source_file / added_by
    存储路径       ``{palace_path}/``（ChromaDB persist dir）

entity（``knowledge_graph.sqlite3`` → 表 ``entities``）
    id            小写规范化名
    name          展示名
    type          person | project | tool | concept
    properties    JSON blob

triple（``knowledge_graph.sqlite3`` → 表 ``triples``）
    subject / predicate / object
    valid_from    该事实开始为真的日期
    valid_to      NULL = 至今有效
    confidence    0.0 ~ 1.0
    source_closet 回指对应 drawer 的 ID（事实与原文可追溯）

--------------------------------------------------------------------------
降级策略（保证"装不全也能跑"）
--------------------------------------------------------------------------
* 未装 ``mempalace``  → 知识图谱走只读 SQLite 直读（schema 固定），
                        写入进 outbox 待补；
* MCP 写入通道不可用  → drawer 写入落到 ``var/memory_outbox.jsonl``，
                        之后 ``flush_outbox()`` 可补投，不丢数据。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.config import MemoryConfig
from app.contracts import (
    Entity,
    MemoryRecord,
    MemoryWrite,
    ToolPort,
    Triple,
    utcnow,
)

logger = logging.getLogger(__name__)


def _today() -> str:
    return date.today().isoformat()


def _maybe_import(module_name: str) -> Any | None:
    """软导入：装了就返回模块，没装返回 None 而不抛异常。"""
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - 缺依赖是预期情况
        logger.debug("可选依赖 %s 不可用: %s", module_name, exc)
        return None


# --------------------------------------------------------------------------- #
# 知识图谱：进程内官方 API，缺失时降级为只读 SQLite
# --------------------------------------------------------------------------- #


class _SqliteKgReader:
    """只读兜底：直接读 MemPalace 的 SQLite schema。

    仅用于未安装 ``mempalace`` 包时的查询降级，**不做任何写入**，
    避免绕过官方实现破坏数据一致性。
    """

    def __init__(self, kg_path: Path) -> None:
        self.kg_path = kg_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.kg_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def available(self) -> bool:
        return self.kg_path.exists()

    def query_entity(self, entity: str, as_of: str | None = None) -> list[Triple]:
        if not self.available():
            return []
        key = entity.strip().lower()
        sql = (
            "SELECT subject, predicate, object, valid_from, valid_to, confidence, source_closet "
            "FROM triples WHERE lower(subject) = ? OR lower(object) = ?"
        )
        params: list[Any] = [key, key]
        if as_of:
            sql += " AND (valid_from IS NULL OR valid_from <= ?) AND (valid_to IS NULL OR valid_to >= ?)"
            params += [as_of, as_of]
        sql += " ORDER BY valid_from ASC"
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            logger.warning("直读知识图谱失败: %s", exc)
            return []
        return [Triple(**dict(row)) for row in rows]

    def timeline(self, entity: str | None = None) -> list[Triple]:
        if not self.available():
            return []
        sql = (
            "SELECT subject, predicate, object, valid_from, valid_to, confidence, source_closet "
            "FROM triples"
        )
        params: list[Any] = []
        if entity:
            sql += " WHERE lower(subject) = ? OR lower(object) = ?"
            params = [entity.strip().lower(), entity.strip().lower()]
        sql += " ORDER BY valid_from ASC"
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            logger.warning("直读知识图谱失败: %s", exc)
            return []
        return [Triple(**dict(row)) for row in rows]

    def stats(self) -> dict[str, Any]:
        if not self.available():
            return {"backend": "sqlite-readonly", "available": False}
        try:
            with self._connect() as conn:
                total = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
                current = conn.execute(
                    "SELECT COUNT(*) FROM triples WHERE valid_to IS NULL"
                ).fetchone()[0]
                entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        except sqlite3.Error as exc:
            return {"backend": "sqlite-readonly", "available": False, "error": str(exc)}
        return {
            "backend": "sqlite-readonly",
            "available": True,
            "entities": entities,
            "triples": total,
            "current_facts": current,
            "expired_facts": total - current,
        }


# --------------------------------------------------------------------------- #
# 记忆网关
# --------------------------------------------------------------------------- #


class MemoryGateway:
    """实现 :class:`app.contracts.MemoryPort`。

    所有对 MemPalace 的读写都必须经过这里 —— 上层模块拿到的是
    :class:`MemoryRecord` / :class:`Triple` 这类契约对象，而不是 ChromaDB 的
    原始返回结构。这样将来换掉记忆后端（比如换 pgvector）只改这一个文件。
    """

    WING_AGENT = "wing_agent"

    def __init__(self, cfg: MemoryConfig, tools: ToolPort | None = None) -> None:
        self.cfg = cfg
        self._tools = tools
        self._kg_module = _maybe_import("mempalace.knowledge_graph")
        self._searcher = _maybe_import("mempalace.searcher")
        self._layers = _maybe_import("mempalace.layers")
        self._fallback = _SqliteKgReader(cfg.kg_path)
        # outbox 路径取自配置，不硬编码 —— 硬编码的 "./var/..." 会跟着 CWD 跑，
        # systemd（CWD=/）下会写进系统 /var，且 AGENT_DATA_DIR 改了它也不动。
        self._outbox = cfg.outbox_path
        self._kg_lock = asyncio.Lock()  # SQLite 写串行化，避免 database is locked
        self._kg_handle: Any | None = None
        self._stack_handle: Any | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def bind_tools(self, tools: ToolPort) -> None:
        """延迟注入工具层，打破 memory <-> mcp_hub 的构造环。"""
        self._tools = tools

    def backend_report(self) -> dict[str, Any]:
        """自检：当前每条能力实际走的是哪条路。"""
        return {
            "palace_path": str(self.cfg.palace_path),
            "kg_path": str(self.cfg.kg_path),
            "drawer_write": "mcp:mempalace_add_drawer" if self._tools else "outbox-only",
            "drawer_search": "lib:search_memories" if self._searcher else "unavailable",
            "knowledge_graph": "lib:KnowledgeGraph" if self._kg_module else "sqlite-readonly",
            "wake_up": "lib:MemoryStack" if self._layers else "unavailable",
            "kg_readonly_fallback": self._fallback.available(),
        }

    def _kg(self) -> Any | None:
        if self._kg_module is None:
            return None
        if self._kg_handle is None:
            try:
                self._kg_handle = self._kg_module.KnowledgeGraph()
            except Exception as exc:  # noqa: BLE001
                logger.warning("KnowledgeGraph 初始化失败，降级直读: %s", exc)
                self._kg_module = None
                return None
        return self._kg_handle

    # ------------------------------------------------------------------ #
    # 抽屉（drawer）：逐字记忆的写入与检索
    # ------------------------------------------------------------------ #

    async def remember(self, write: MemoryWrite) -> str:
        """写入一条逐字记忆，返回 drawer_id（失败时返回 outbox 记录 id）。"""
        if self._tools is not None:
            result = await self._tools.call(
                "mempalace",
                "mempalace_add_drawer",
                {
                    "wing": write.wing,
                    "room": write.room,
                    "content": write.content,
                    "source_file": write.source_file or "",
                    "added_by": write.added_by,
                },
            )
            if result.ok:
                return self._extract_id(result.content)
            logger.warning("MCP 写入抽屉失败，转 outbox: %s", result.error)

        return self._to_outbox(write)

    def _to_outbox(self, write: MemoryWrite) -> str:
        """写失败不丢数据：落到本地 JSONL 待补投。"""
        self._outbox.parent.mkdir(parents=True, exist_ok=True)
        record_id = f"outbox_{int(utcnow().timestamp() * 1000)}"
        row = {
            "id": record_id,
            "wing": write.wing,
            "room": write.room,
            "content": write.content,
            "source_file": write.source_file,
            "added_by": write.added_by,
            "ts": utcnow().isoformat(),
        }
        with self._outbox.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        logger.info("记忆写入暂存 outbox: %s", record_id)
        return record_id

    async def flush_outbox(self) -> int:
        """把 outbox 里的待写记忆补投到 MemPalace，返回成功条数。"""
        if not self._outbox.exists() or self._tools is None:
            return 0
        rows = [
            json.loads(line)
            for line in self._outbox.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        pending: list[dict[str, Any]] = []
        flushed = 0
        for row in rows:
            ok = await self.remember(
                MemoryWrite(
                    content=row["content"],
                    wing=row["wing"],
                    room=row["room"],
                    source_file=row.get("source_file"),
                    added_by=row.get("added_by", "agent"),
                )
            )
            if ok.startswith("outbox_"):
                pending.append(row)
            else:
                flushed += 1
        self._outbox.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in pending), encoding="utf-8"
        )
        logger.info("outbox 补投完成：成功 %d 条，剩余 %d 条", flushed, len(pending))
        return flushed

    async def recall(
        self, query: str, *, wing: str | None = None, room: str | None = None, k: int = 5
    ) -> list[MemoryRecord]:
        """语义检索。优先进程内 API；否则回落到 MCP ``mempalace_search``。"""
        if not query.strip():
            return []

        if self._searcher is not None:
            try:
                raw = await asyncio.to_thread(
                    self._searcher.search_memories,
                    query=query,
                    wing=wing,
                    room=room,
                    n_results=k,
                )
                return [self._to_record(item) for item in (raw.get("results") or [])]
            except Exception as exc:  # noqa: BLE001
                logger.warning("进程内检索失败，回落 MCP: %s", exc)

        if self._tools is not None:
            args: dict[str, Any] = {"query": query, "n_results": k}
            if wing:
                args["wing"] = wing
            if room:
                args["room"] = room
            result = await self._tools.call("mempalace", "mempalace_search", args)
            if result.ok:
                return [self._to_record(item) for item in self._as_items(result.content)]
        return []

    @staticmethod
    def _as_items(content: Any) -> list[dict[str, Any]]:
        """MCP 返回的 content 可能是 list[TextContent] 或已解析的 dict。"""
        if isinstance(content, dict):
            return list(content.get("results") or [])
        if isinstance(content, list):
            out: list[dict[str, Any]] = []
            for item in content:
                if isinstance(item, dict):
                    out.extend(item.get("results") or [item])
                elif isinstance(item, str):
                    try:
                        out.extend(json.loads(item).get("results") or [])
                    except json.JSONDecodeError:
                        continue
            return out
        return []

    @staticmethod
    def _to_record(item: dict[str, Any]) -> MemoryRecord:
        return MemoryRecord(
            text=str(item.get("text") or item.get("content") or ""),
            wing=item.get("wing"),
            room=item.get("room"),
            source_file=item.get("source_file"),
            similarity=float(item.get("similarity") or item.get("score") or 0.0),
            drawer_id=item.get("drawer_id") or item.get("id"),
        )

    async def wake_up(self, wing: str | None = None) -> str:
        """加载 L0（身份）+ L1（核心事实），约 600~900 tokens。

        这是让 agent "醒来就知道自己在哪" 的关键 —— 比全量塞历史便宜两个数量级。
        """
        if self._layers is None:
            return ""
        try:
            if self._stack_handle is None:
                self._stack_handle = self._layers.MemoryStack()
            return str(await asyncio.to_thread(self._stack_handle.wake_up, wing=wing) or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("wake_up 失败: %s", exc)
            return ""

    # ------------------------------------------------------------------ #
    # 知识图谱：实体与关系
    # ------------------------------------------------------------------ #

    async def upsert_entity(self, entity: Entity) -> None:
        kg = self._kg()
        if kg is None:
            logger.debug("知识图谱不可写，跳过实体 %s", entity.name)
            return
        async with self._kg_lock:
            await asyncio.to_thread(
                kg.add_entity, entity.name, entity_type=entity.type
            )

    async def assert_fact(self, triple: Triple) -> None:
        """新增一条事实（可共存的事实用这个，例如"参与过 X 项目"）。"""
        kg = self._kg()
        if kg is None:
            logger.debug("知识图谱不可写，跳过三元组 %s", triple.render())
            return
        async with self._kg_lock:
            await asyncio.to_thread(
                kg.add_triple,
                triple.subject,
                triple.predicate,
                triple.object,
                valid_from=triple.valid_from or _today(),
                confidence=triple.confidence,
                source_closet=triple.source_closet,
            )

    async def invalidate_fact(
        self, subject: str, predicate: str, obj: str, ended: str
    ) -> None:
        """事实结束（不再是真），但历史查询仍可查到。"""
        kg = self._kg()
        if kg is None:
            return
        async with self._kg_lock:
            await asyncio.to_thread(kg.invalidate, subject, predicate, obj, ended=ended)

    async def supersede_fact(
        self, subject: str, predicate: str, *, old_obj: str, new_obj: str, at: str
    ) -> None:
        """单值事实变更：一个事务内关旧开新，交接点不重叠。

        适用于 ``uses_model`` / ``works_at`` 这类"只应有一个当前值"的关系。
        """
        kg = self._kg()
        if kg is None:
            return
        async with self._kg_lock:
            await asyncio.to_thread(
                kg.supersede, subject, predicate, old_obj=old_obj, new_obj=new_obj, at=at
            )

    async def facts_about(self, entity: str, *, as_of: str | None = None) -> list[Triple]:
        kg = self._kg()
        if kg is not None:
            try:
                rows = await asyncio.to_thread(kg.query_entity, entity, as_of=as_of)
                return [self._as_triple(row) for row in rows or []]
            except Exception as exc:  # noqa: BLE001
                logger.warning("知识图谱查询失败，降级直读: %s", exc)
        return await asyncio.to_thread(self._fallback.query_entity, entity, as_of)

    async def timeline(self, entity: str | None = None) -> list[Triple]:
        kg = self._kg()
        if kg is not None:
            try:
                rows = await asyncio.to_thread(kg.timeline, entity)
                return [self._as_triple(row) for row in rows or []]
            except Exception as exc:  # noqa: BLE001
                logger.warning("时间线查询失败，降级直读: %s", exc)
        return await asyncio.to_thread(self._fallback.timeline, entity)

    async def stats(self) -> dict[str, Any]:
        kg = self._kg()
        if kg is not None:
            try:
                raw = await asyncio.to_thread(kg.stats)
                if isinstance(raw, dict):
                    return {"backend": "lib:KnowledgeGraph", **raw}
            except Exception as exc:  # noqa: BLE001
                logger.debug("知识图谱 stats 失败: %s", exc)
        return await asyncio.to_thread(self._fallback.stats)

    @staticmethod
    def _as_triple(row: Any) -> Triple:
        """兼容对象行（官方 API）与 dict/sqlite3.Row（直读）两种返回形态。"""
        if isinstance(row, Triple):
            return row
        if isinstance(row, dict):
            data = row
        elif hasattr(row, "keys"):
            data = {k: row[k] for k in row.keys()}
        else:  # 未知形态：退化成字符串描述
            return Triple(subject=str(row), predicate="related_to", object="?")
        return Triple(
            subject=str(data.get("subject") or data.get("source") or ""),
            predicate=str(data.get("predicate") or data.get("relation") or "related_to"),
            object=str(data.get("object") or data.get("target") or ""),
            valid_from=data.get("valid_from"),
            valid_to=data.get("valid_to"),
            confidence=float(data.get("confidence") or 1.0),
            source_closet=data.get("source_closet"),
        )

    # ------------------------------------------------------------------ #
    # 面向 orchestrator 的组合能力
    # ------------------------------------------------------------------ #

    async def remember_turn(
        self,
        *,
        chat_id: str,
        user_text: str,
        assistant_text: str,
        wing: str | None = None,
        room: str | None = None,
        emotion: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        """把一轮对话落成两条逐字 drawer（用户侧 + 助手侧）。

        刻意不做摘要 —— MemPalace 的 96.6% R@5 正是靠"原文直存"拿到的，
        一旦摘要就引入信息损失。
        """
        wing = wing or self.cfg.default_wing
        room = room or self.cfg.default_room
        stamp = utcnow().isoformat(timespec="seconds")
        suffix = f"emotion={json.dumps(emotion, ensure_ascii=False)}" if emotion else ""
        user_id = await self.remember(
            MemoryWrite(
                content=f"[{stamp}] USER({chat_id}): {user_text}\n{suffix}".strip(),
                wing=wing,
                room=room,
                source_file=f"chat/{chat_id}",
                added_by="telegram",
            )
        )
        agent_id = await self.remember(
            MemoryWrite(
                content=f"[{stamp}] AGENT: {assistant_text}",
                wing=wing,
                room=room,
                source_file=f"chat/{chat_id}",
                added_by="agent",
            )
        )
        return user_id, agent_id

    async def build_context(
        self,
        query: str,
        *,
        wing: str | None = None,
        room: str | None = None,
        k: int | None = None,
        entity: str | None = None,
    ) -> str:
        """把"唤醒层 + 语义召回 + 知识图谱事实"拼成一段可注入主脑的上下文。

        这是记忆层对外的**唯一**读接口 —— 主脑不需要知道 drawer 和 triple 的区别。
        """
        k = k or self.cfg.recall_topk
        wing = wing or self.cfg.default_wing
        blocks: list[str] = []

        wake = await self.wake_up(wing)
        if wake.strip():
            blocks.append(f"## 身份与核心事实（L0/L1）\n{wake.strip()}")

        records = await self.recall(query, wing=wing, room=room, k=k)
        if records:
            lines = [
                f"- ({r.similarity:.2f}) {r.text.strip()[:400]}"
                for r in records
            ]
            blocks.append("## 相关历史记忆（语义召回）\n" + "\n".join(lines))

        if entity:
            triples = await self.facts_about(entity)
            if triples:
                blocks.append(
                    "## 已知实体关系（时序知识图谱）\n"
                    + "\n".join(f"- {t.render()}" for t in triples)
                )

        if not blocks:
            return ""
        return "\n\n".join(blocks)

    @staticmethod
    def _extract_id(content: Any) -> str:
        if isinstance(content, dict):
            return str(content.get("drawer_id") or content.get("id") or "")
        if isinstance(content, list) and content:
            return MemoryGateway._extract_id(content[0])
        text = str(content)
        for token in text.split():
            if token.startswith("drawer_") or token.startswith("outbox_"):
                return token.strip('",')
        return text[:64]
