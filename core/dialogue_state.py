"""苏格拉底教学模式的阶段状态与转移规则。"""

from __future__ import annotations

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


def looks_like_new_problem(text: str) -> bool:
    """粗略判断用户消息是否是一道新题（含数学定界符/运算符，或较长的问句）。"""
    t = (text or "").strip()
    if len(t) >= 15 and (
        t.endswith(("？", "?"))
        or any(k in t for k in ("求", "解", "证明", "已知", "设", "怎么", "如何", "为什么", "计算"))
    ):
        return True
    return any(ch in t for ch in "=<>≤≥×÷√∫∑±∞²³") or "$" in t or "\\" in t


def advance_stage(
    mode: str, message: str, current: dict | None, has_history: bool = False
) -> dict:
    """根据用户新消息计算本轮回复应遵循的阶段（纯函数，供测试）。

    优先级：解答模式 → 逃生词 → 练习词 → 批改词 → 新题判定 → 常规推进。
    """
    current = dict(current or {})
    stage = current.get("stage")
    hints = int(current.get("hints") or 0)
    last_mode = current.get("last_mode") or MODE_INTERACTIVE
    mode_changed = mode != last_mode
    if mode != MODE_INTERACTIVE:
        return {
            "stage": None,
            "hints": 0,
            "last_mode": mode,
            "mode_changed": mode_changed,
        }

    text = (message or "").strip()
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
    if stage in ("summary", "full_solution", "grading") and looks_like_new_problem(
        text
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
    if stage == "targeted_hint":
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
