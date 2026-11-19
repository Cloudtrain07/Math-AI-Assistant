"""苏格拉底教学模式的阶段状态与转移规则。"""

from __future__ import annotations

import re

from core.prompts import MODE_INTERACTIVE


# 互动模式阶段关键词（启发式，用户可用「给答案」等口令纠正）
ESCAPE_KEYWORDS = (
    "给答案",
    "直接讲",
    "没时间",
    "别问了",
    "直接解",
    "说答案",
    "给我答案",
    "直接给答案",
    "讲一下",
    "讲讲",
)

_REPLY_DIRECTIVE = re.compile(
    r"(?:(?P<negation>不要|不用|不必|不想|不需要|无需|别)\s*)?"
    r"(?:(?:再|先|直接)\s*)*(?:只\s*)?"
    r"(?:(?P<solution>(?:给(?:出|我)?|说出?|告诉我|讲出?|想要|需要|要)(?:最终|正确)?答案"
    r"|把(?:最终|正确)?答案(?:直接)?告诉我"
    r"|(?:看|讲|说|给出|给我|展示|写出)(?:一下)?完整(?:解法|解答|步骤|过程|推导)"
    r"|直接(?:讲|解))"
    r"|(?P<hint>(?:给我\s*)?提示(?:我(?:一下|一步|下一步)?|一下|一步|下一步)"
    r"|(?:给(?:我)?|来)(?:一个|一点)提示|(?:要|想要|需要)(?:一个|一点)?提示)"
    r"|(?P<answer>(?:最终|正确)?答案)|提示)"
)
_PROBLEM_SWITCH = re.compile(
    r"(?:^|[，,。.!！?？；;：:、\n])\s*(?:(?:我们|我想|请|现在|接下来|先)\s*)*"
    r"(?:换(?:一?(?:道|个))?(?:新)?题|再(?:来|做|解)一道新题"
    r"|(?:下一题|新题)(?=$|[\s，,。.!！?？；;：:、]))"
)
_FOLLOWUP_REFERENCE = re.compile(
    r"刚才|前面|之前|(?:这|那|上)(?:一|个|道|张)?(?:步|题|图|等式|式子)|图[中里上]"
)
_PROBLEM_START = re.compile(
    r"^(?:请\s*)?(?:求|解(?:方程|不等式)|计算|证明|已知|设(?=\s|\$|[a-zA-Z]))"
)
_ARITHMETIC_QUESTION = re.compile(r"[\d\s.+＋\-−*/×÷()（）]+[=＝]\s*[?？]")
PRACTICE_KEYWORDS = (
    "出题",
    "练练",
    "再来一道",
    "巩固",
    "出道题",
    "练习题",
    "再来一题",
    "换个题",
)
GRADING_KEYWORDS = (
    "我的做法",
    "我的过程",
    "我的解答",
    "我的思路",
    "我这样写",
    "我这样算",
    "我算得",
    "我解出来",
    "我算出来",
    "帮我看看",
    "帮我看",
    "检查一下",
    "请批改",
    "帮我批改",
    "改一下",
    "对吗",
    "对不对",
)


def new_stage_state() -> dict:
    """互动模式的初始阶段状态。"""
    return {"stage": "initial_hint", "hints": 0, "last_mode": MODE_INTERACTIVE}


def _explicit_reply_stage(text: str) -> str | None:
    """按顺序处理分句里的提示或解答指令。"""
    stage = None
    for clause in re.split(r"[，,。！？!?；;\n]", text):
        for directive in _REPLY_DIRECTIVE.finditer(clause):
            if directive.group("negation"):
                if directive.group("solution") or directive.group("answer"):
                    stage = "single_hint"
            elif directive.group("solution"):
                stage = "full_solution"
            elif directive.group("hint"):
                stage = "single_hint"
    return stage


def explicitly_switches_problem(text: str) -> bool:
    return bool(_PROBLEM_SWITCH.search(text))


def looks_like_new_problem(text: str) -> bool:
    """从换题指令或独立题干识别新题，优先保留带上下文指代的追问。"""
    t = (text or "").strip()
    return explicitly_switches_problem(t) or (
        not _FOLLOWUP_REFERENCE.search(t)
        and bool(_PROBLEM_START.match(t) or _ARITHMETIC_QUESTION.fullmatch(t))
    )


def advance_stage(
    mode: str, message: str, current: dict | None, has_history: bool = False , force_continue: bool = False
) -> dict:
    """根据用户新消息计算本轮回复应遵循的阶段（纯函数，供测试）。

    优先处理继续生成、解答模式和本轮明确指令，再走原有阶段流转。
    """
    current = dict(current or {})
    stage = current.get("stage")
    hints = int(current.get("hints") or 0)
    last_mode = current.get("last_mode") or MODE_INTERACTIVE
    mode_changed = mode != last_mode
    if force_continue:
        current["mode_changed"] = False
        if mode != MODE_INTERACTIVE:
            current["stage"] = None
            current["last_mode"] = mode
        return current
    if mode != MODE_INTERACTIVE:
        return {
            "stage": None,
            "hints": 0,
            "last_mode": mode,
            "mode_changed": mode_changed,
        }

    text = (message or "").strip()
    explicit_stage = _explicit_reply_stage(text)
    if explicit_stage is not None:
        return {
            "stage": explicit_stage,
            "hints": min(hints + 1, 2) if explicit_stage == "single_hint" else hints,
            "last_mode": mode,
            "mode_changed": mode_changed,
        }
    if mode_changed:
        new_stage = "targeted_hint" if has_history else "initial_hint"
        return {
            "stage": new_stage,
            "hints": 0,
            "last_mode": mode,
            "mode_changed": True,
        }
    if any(k in text for k in ESCAPE_KEYWORDS):
        return {
            "stage": "full_solution",
            "hints": hints,
            "last_mode": mode,
            "mode_changed": False,
        }
    if any(k in text for k in PRACTICE_KEYWORDS):
        return {
            "stage": "practice_wait",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    if any(k in text for k in GRADING_KEYWORDS):
        return {
            "stage": "grading",
            "hints": hints,
            "last_mode": mode,
            "mode_changed": False,
        }
    if explicitly_switches_problem(text) or (
        stage in ("summary", "full_solution", "grading") and looks_like_new_problem(text)
    ):
        return {
            "stage": "initial_hint",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    if not has_history:
        return {
            "stage": "initial_hint",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage in (None, "initial_hint"):
        return {
            "stage": "targeted_hint",
            "hints": hints + 1,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage in ("targeted_hint", "single_hint"):
        if hints + 1 >= 3:
            return {
                "stage": "full_solution",
                "hints": 0,
                "last_mode": mode,
                "mode_changed": False,
            }
        return {
            "stage": "targeted_hint",
            "hints": hints + 1,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage == "full_solution":
        return {
            "stage": "targeted_hint",
            "hints": 1,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage == "practice_wait":
        return {
            "stage": "practice_grade",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage == "practice_grade":
        return {
            "stage": "practice_grade",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage == "grading":
        return {
            "stage": "summary",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    if stage == "summary":
        return {
            "stage": "summary",
            "hints": 0,
            "last_mode": mode,
            "mode_changed": False,
        }
    return {
        "stage": "initial_hint",
        "hints": 0,
        "last_mode": mode,
        "mode_changed": False,
    }


def prepare_stage_state(mode: str, current: dict | None) -> dict:
    """补齐阶段状态，并修正互动模式下不合法的阶段值。"""
    result = dict(current or new_stage_state())
    result.setdefault("stage", None)
    result.setdefault("hints", 0)
    result.setdefault("last_mode", MODE_INTERACTIVE)

    if mode == MODE_INTERACTIVE and result.get("stage") not in (
        "initial_hint",
        "targeted_hint",
        "single_hint",
        "full_solution",
        "summary",
        "grading",
        "practice_wait",
        "practice_grade",
    ):
        result["stage"] = "initial_hint"

    return result
