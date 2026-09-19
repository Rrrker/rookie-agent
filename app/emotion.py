"""情绪系统 —— PAD（Pleasure-Arousal-Dominance）三维情感模型。

--------------------------------------------------------------------------
为什么要 PAD，而不是"开心/生气"这种离散标签？
--------------------------------------------------------------------------
离散标签有三个致命缺陷：无法表达强度、无法插值、无法自然衰减。
PAD 把情绪放进连续三维空间，于是三件事都变成了算术：

* **刺激**   一次事件 = 空间里的一个位移向量：``state += gain * intensity * effect``
* **衰减**   没有新刺激时，状态按指数向基线回归：``x ← x + (base - x) * α``
* **语气**   状态 → 语言指令/语音参数，是两个纯函数的映射

--------------------------------------------------------------------------
模型定义（Mehrabian 1974）
--------------------------------------------------------------------------
每一维取值 ``[-1, 1]``：

===============  ============  ====================================
维度              负向含义       正向含义
===============  ============  ====================================
Pleasure 愉悦度    不快、烦躁      愉快、满意
Arousal 唤醒度     平静、低能量    激动、紧张、警觉
Dominance 支配感   顺从、被支配    掌控、自信、主导
===============  ============  ====================================

三维符号组合出 8 个象限（Mehrabian 八种气质）：:

    (+P,+A,+D) 热情   (+P,+A,-D) 亲和   (+P,-A,+D) 松弛   (+P,-A,-D) 温和
    (-P,+A,+D) 冷峻   (-P,+A,-D) 紧绷   (-P,-A,+D) 冷淡   (-P,-A,-D) 倦怠

--------------------------------------------------------------------------
衰减的数学
--------------------------------------------------------------------------
用半衰期而不是"每秒减固定值"——因为固定值在接近基线时会过冲，
而指数衰减天然渐近、永不越界：

    α = 1 - 0.5 ** (Δt / half_life)
    x_new = x + (baseline - x) * α

Δt = half_life 时 α = 0.5，正好走完一半距离。
"""

from __future__ import annotations

import logging
import re
from collections import deque
from datetime import datetime
from typing import Any, Iterable

from app.config import EmotionConfig
from app.contracts import EmotionEvent, EmotionState, utcnow

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 事件 → PAD 位移向量
# 数值是 (ΔP, ΔA, ΔD)，表示单位强度刺激的方向。数值大小是设计参数，
# 可以用真实对话日志做回归校准，这里给的是经过推敲的初值。
# --------------------------------------------------------------------------- #
_EVENT_EFFECTS: dict[str, tuple[float, float, float]] = {
    "praise":       (0.90, 0.30, 0.15),   # 被肯定 → 愉悦↑，略兴奋，自信↑
    "gratitude":    (0.85, 0.10, 0.05),   # 被感谢 → 主要是愉悦
    "insult":       (-1.00, 0.55, -0.25), # 被辱骂 → 愉悦↓↓，激动（气），支配感小降
    "blame":        (-0.80, 0.45, -0.45), # 被指责 → 愉悦↓，支配感明显↓
    "criticism":    (-0.35, 0.20, -0.10), # 建设性批评 → 轻微负向
    "urgency":      (-0.10, 0.90, 0.25),  # 紧急 → 唤醒度飙升，进入主导状态
    "question":     (0.05, 0.20, 0.30),   # 提问 → 支配感与注意力上升
    "task_success": (0.75, 0.40, 0.70),   # 任务成功 → 愉悦+掌控感双升
    "task_failure": (-0.70, 0.35, -0.55), # 任务失败 → 支配感挫伤
    "error":        (-0.50, 0.60, -0.60), # 系统报错 → 紧张且失控
    "long_silence": (-0.25, -0.35, 0.00), # 长时间无互动 → 愉悦↓且低能量（倦怠）
    "intimacy":     (0.80, 0.25, 0.05),   # 亲昵表达 → 愉悦↑
    "humor":        (0.80, 0.55, 0.20),   # 玩笑 → 愉悦+唤醒双升
    "neutral":      (0.00, 0.00, 0.00),
}

# 关键词词表：中文为主，附常用英文。命中数用于估算刺激强度。
_LEXICON: dict[str, tuple[str, ...]] = {
    "praise": (
        "谢谢", "多谢", "感谢", "太棒", "真棒", "厉害", "牛", "优秀", "不错",
        "完美", "点赞", "好样", "爱你", "靠谱", "给力", "辛苦了",
        "thank", "thanks", "awesome", "great job", "well done", "perfect", "brilliant",
    ),
    "gratitude": ("麻烦你", "有劳", "感恩", "appreciate"),
    "insult": (
        "垃圾", "废物", "蠢", "笨", "闭嘴", "滚", "傻", "差劲", "没用", "弱智",
        "傻逼", "智障", "胡说", "瞎说",
        "stupid", "useless", "terrible", "worst", "garbage", "idiot", "nonsense",
    ),
    "blame": ("都怪你", "你的错", "怪你", "搞砸", "又错", "怎么又", "你害", "your fault"),
    "criticism": ("不对", "错了", "有问题", "不对吧", "但是", "然而", "不满意", "wrong", "not right"),
    "urgency": (
        "急", "马上", "立刻", "赶紧", "尽快", "紧急", "火速", "现在就要", "来不及",
        "asap", "urgent", "right now", "immediately", "hurry",
    ),
    "intimacy": ("抱抱", "晚安", "想你", "亲亲", "爱你哟", "么么", "love you", "good night"),
    "humor": ("哈哈", "笑死", "嘻嘻", "233", "搞笑", "lol", "haha", "lmao", "😂", "🤣"),
}

_QUESTION_RE = re.compile(r"[?？]|吗[？?]?$|怎么|为什么|如何|是不是|能不能|什么", re.IGNORECASE)

# 八象限 → 中文标签
_OCTANTS: dict[tuple[int, int, int], str] = {
    (1, 1, 1): "exuberant",
    (1, 1, -1): "dependent",
    (1, -1, 1): "relaxed",
    (1, -1, -1): "docile",
    (-1, 1, 1): "disdainful",
    (-1, 1, -1): "anxious",
    (-1, -1, 1): "hostile",
    (-1, -1, -1): "bored",
}

_LABEL_CN: dict[str, str] = {
    "exuberant": "热情",
    "dependent": "亲和",
    "relaxed": "松弛",
    "docile": "温和",
    "disdainful": "冷峻",
    "anxious": "紧绷",
    "hostile": "冷淡",
    "bored": "倦怠",
    "neutral": "中性",
}

# 象限 → 语气指令（这是情绪系统真正影响输出的地方）
_STYLE_TABLE: dict[str, str] = {
    "exuberant": "语气轻快、有感染力。可以用感叹和肯定词，但结论仍要放在第一句。",
    "dependent": "语气亲和、放低姿态。多用「我们一起」「你看这样行不行」，少用断言。",
    "relaxed": "语气平稳松弛，允许一点从容的口气词。节奏可以慢，不必急着给方案。",
    "docile": "语气温和、简短。少用专业术语，多确认对方感受。",
    "disdainful": "语气干脆、专业克制。直接给判断和依据，不寒暄。",
    "anxious": "语气收紧、聚焦。句子要短，先安抚情绪，再给确定的下一步。",
    "hostile": "语气冷静、就事论事。不展开闲聊，只回答被问到的部分，不加多余铺垫。",
    "bored": "语气简洁、低能量。能一句话说完就不说第二句。",
    "neutral": "语气中性自然。",
}


def _sign(value: float, dead_zone: float = 0.05) -> int:
    """带死区的取符号：接近 0 的分量不算方向，避免基线附近的标签抖动。"""
    if value > dead_zone:
        return 1
    if value < -dead_zone:
        return -1
    return 0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class PadEmotionEngine:
    """实现 :class:`app.contracts.EmotionPort`。

    纯内存、无 IO，所以同步方法即可（不需要 async）。
    状态是**全局单一**的 —— 情绪属于"这一个助手人格"，
    而不是每个会话一份；分身任务不参与情绪演化。
    """

    def __init__(self, cfg: EmotionConfig | None = None) -> None:
        self.cfg = cfg or EmotionConfig()
        self._baseline: tuple[float, float, float] = self.cfg.baseline
        self._state = EmotionState(
            pleasure=self._baseline[0],
            arousal=self._baseline[1],
            dominance=self._baseline[2],
            label=self._label_for(self._baseline),
        )
        self._last_tick: datetime = utcnow()
        self._trace: deque[dict[str, Any]] = deque(maxlen=200)

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    def current(self) -> EmotionState:
        """取当前状态。取之前先按真实时间衰减 —— 情绪随时间自己消退。"""
        return self.decay()

    def trace(self) -> list[dict[str, Any]]:
        return list(self._trace)

    # ------------------------------------------------------------------ #
    # 衰减
    # ------------------------------------------------------------------ #

    def decay(self, *, now: datetime | None = None) -> EmotionState:
        """按经过时间向基线做指数回归。"""
        now = now or utcnow()
        dt = (now - self._last_tick).total_seconds()
        if dt <= 0:
            return self._state
        self._last_tick = now

        half_life = max(1.0, self.cfg.decay_half_life_sec)
        alpha = 1.0 - 0.5 ** (dt / half_life)

        p, a, d = (
            self._state.pleasure + (self._baseline[0] - self._state.pleasure) * alpha,
            self._state.arousal + (self._baseline[1] - self._state.arousal) * alpha,
            self._state.dominance + (self._baseline[2] - self._state.dominance) * alpha,
        )
        self._state = EmotionState(
            pleasure=p, arousal=a, dominance=d,
            label=self._label_for((p, a, d)), updated_at=now,
        )
        return self._state

    # ------------------------------------------------------------------ #
    # 刺激
    # ------------------------------------------------------------------ #

    def appraise(self, event: EmotionEvent) -> EmotionState:
        """施加一次情绪刺激。"""
        effects = _EVENT_EFFECTS.get(event.name)
        if effects is None:
            logger.debug("未知情绪事件：%s（按 neutral 处理）", event.name)
            return self._state

        self.decay()
        gain = self.cfg.gain * _clamp(event.intensity, 0.0, 1.0)

        # 负性偏差：同等强度下，负面事件的冲击大于正面事件
        # （Baumeister et al., 2001）。不设这个偏差，正向刺激会持续占优，
        # 情绪长期卡在正向象限 —— 那样"动态调整语气"就没意义了。
        if effects[0] < 0:
            gain *= self.cfg.negativity_bias

        limit = self.cfg.clamp
        p = _clamp(self._state.pleasure + gain * effects[0], -limit, limit)
        a = _clamp(self._state.arousal + gain * effects[1], -limit, limit)
        d = _clamp(self._state.dominance + gain * effects[2], -limit, limit)

        self._state = EmotionState(
            pleasure=p, arousal=a, dominance=d,
            label=self._label_for((p, a, d)),
        )
        self._trace.append(
            {
                "ts": self._state.updated_at.isoformat(timespec="seconds"),
                "event": event.name,
                "intensity": round(event.intensity, 3),
                "evidence": event.evidence,
                "pad": self._state.as_dict(),
            }
        )
        return self._state

    def appraise_text(self, text: str, *, sender_is_user: bool = True) -> EmotionState:
        """从自然语言里抽取情绪刺激。

        实现方式是**加权关键词 + 提问句式**，而不是再调一次模型 ——
        情绪评估必须零延迟、零成本、可离线，否则它就会成为流水线上的瓶颈。
        命中多个事件时逐个施加，让复合情绪自然叠加（例如"紧急 + 抱怨"）。

        想升级精度时，只需把这里替换成一个返回 :class:`EmotionEvent` 列表的
        小模型调用，契约不变。
        """
        if not text or not text.strip():
            return self._state

        lowered = text.lower()
        fired: list[EmotionEvent] = []

        for name, patterns in _LEXICON.items():
            hits = [p for p in patterns if p in lowered]
            if not hits:
                continue
            # 命中越多刺激越强，但起点要低、增长要缓：
            # 1 个命中 = 0.35（轻微），之后每多一个 +0.22，封顶 1.0。
            # 起点定高会让一条消息里的重复词把情绪直接顶到 clamp，
            # 之后就再也看不出变化 —— 那正是"情绪装饰化"的根源。
            intensity = _clamp(0.35 + 0.22 * (len(hits) - 1), 0.0, 1.0)
            fired.append(
                EmotionEvent(name=name, intensity=intensity, evidence=",".join(hits[:3]))
            )

        if not fired and _QUESTION_RE.search(text):
            fired.append(EmotionEvent(name="question", intensity=0.4, evidence="question-form"))

        if not fired:
            return self._state

        state = self._state
        for event in fired:
            state = self.appraise(event)
        return state

    def on_task_outcome(
        self, *, success: bool, intensity: float = 0.55, detail: str = ""
    ) -> EmotionState:
        """后台分身任务的结果回灌。

        只有**稀缺且显著**的事件才值得影响情绪：一次跑了几分钟的后台任务
        成功或失败是有信息量的；而"这一轮正常回了句话"不是 ——
        后者由 orchestrator 刻意排除在外。

        强度默认 0.55 而不是 1.0，是为了让日常事件的累积速度慢于
        半衰期，情绪才有机会回到基线。
        """
        return self.appraise(
            EmotionEvent(
                name="task_success" if success else "task_failure",
                intensity=_clamp(intensity, 0.0, 1.0),
                evidence=detail[:120],
            )
        )

    def on_idle(self, idle_sec: float) -> EmotionState:
        """长时间无互动 → 轻微倦怠。由调度器的 tick 周期调用。"""
        if idle_sec >= 1800:
            return self.appraise(
                EmotionEvent(name="long_silence", intensity=min(1.0, idle_sec / 7200))
            )
        return self._state

    # ------------------------------------------------------------------ #
    # 输出侧：语气指令与语音参数
    # ------------------------------------------------------------------ #

    def style_directive(self) -> str:
        """把情绪状态翻译成一段可注入主脑的语气指令。

        强度低于阈值时返回空串 —— 情绪不该在每一轮都扰动 prompt，
        否则语气指令会变成噪声，反而让输出不稳定。
        """
        state = self.current()
        if state.magnitude < self.cfg.directive_threshold:
            return ""

        label = state.label
        base = _STYLE_TABLE.get(label, _STYLE_TABLE["neutral"])

        clauses: list[str] = []
        # 唤醒度 → 节奏与句子长度
        if state.arousal > 0.35:
            clauses.append("节奏稍快，句子偏短；先给结论再补充说明。")
        elif state.arousal < -0.25:
            clauses.append("节奏放缓，允许把背景交代完整再落到结论。")
        # 支配感 → 确定性强度
        if state.dominance > 0.35:
            clauses.append("语气笃定，直接给判断，不要用「可能」「也许」反复铺垫。")
        elif state.dominance < -0.25:
            clauses.append("多用协商口吻，把最终选择权交回给用户。")

        body = " ".join([base, *clauses])
        return (
            f"[实时情绪状态] P={state.pleasure:+.2f} A={state.arousal:+.2f} "
            f"D={state.dominance:+.2f}（{_LABEL_CN.get(label, label)}）\n"
            f"[语气要求] {body}\n"
            "情绪只影响措辞与节奏，不得改变事实、结论或安全边界。"
        )

    def voice_params(self) -> dict[str, float]:
        """由情绪推导 TTS 参数。

        注：OpenAI 兼容 TTS 只接受 ``speed``；``warmth`` / ``assertiveness``
        是给支持更丰富参数的 TTS（或未来的 style 字段）预留的语义量，
        也可以用来挑选不同音色。
        """
        state = self.current()
        return {
            "speed": round(_clamp(1.0 + 0.22 * state.arousal, 0.75, 1.35), 3),
            "warmth": round((state.pleasure + 1.0) / 2.0, 3),
            "assertiveness": round((state.dominance + 1.0) / 2.0, 3),
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _label_for(self, pad: Iterable[float]) -> str:
        """把 PAD 坐标映射到八象限标签。

        判据是**相对基线的偏移**而不是绝对值 —— 因为"中立"不等于原点，
        而是"没有偏离我的常态"。这一点很关键：如果按绝对值判，
        基线本来就偏正向（P=+0.10），会让情绪系统永远偏向正向象限。

        偏移量小于 ``label_epsilon`` 判中性；否则按三维符号定象限。
        符号用"非负即正"，不再叠加死区 —— 偏移量已经足够大，
        此时的 0 只是数值巧合，必须落到某个象限而不能悬空。
        """
        p, a, d = pad
        dp = p - self._baseline[0]
        da = a - self._baseline[1]
        dd = d - self._baseline[2]
        if (dp * dp + da * da + dd * dd) ** 0.5 < self.cfg.label_epsilon:
            return "neutral"
        key = tuple(1 if _sign(delta, 0.0) >= 0 else -1 for delta in (dp, da, dd))
        return _OCTANTS[key]  # type: ignore[index]

    def snapshot(self) -> dict[str, Any]:
        """给调度器/监控用的快照。"""
        state = self.current()
        return {
            "state": state.as_dict(),
            "magnitude": round(state.magnitude, 4),
            "voice": self.voice_params(),
            "recent_events": [t["event"] for t in list(self._trace)[-5:]],
        }
