"""本地符号计算与答案校验：sympy + latex2sympy2。"""

from __future__ import annotations

import re
import sys
import threading
import types
import io as _io


def _install_typing_io_shim() -> None:
    """Python 3.13 移除了 typing.io；antlr4 4.7.2 仍引用它，先补一个兼容模块。"""
    if "typing.io" not in sys.modules:
        shim = types.ModuleType("typing.io")
        shim.TextIO = _io.TextIOBase
        sys.modules["typing.io"] = shim


_install_typing_io_shim()

try:
    import sympy as sp
    from latex2sympy2 import latex2sympy

    CALCULATOR_AVAILABLE = True
except Exception:  # 依赖未安装时功能降级，界面给出提示
    sp = None
    latex2sympy = None
    CALCULATOR_AVAILABLE = False

_MATH_BLOCK = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.S)
_BOXED = re.compile(r"\\boxed\s*\{([^}]*)\}")
_IMPLICIT_MUL = re.compile(r"(?<!\\)(\d)([a-zA-Z])")
_TRAILING = re.compile(r"[。．,，;；\s]+$")

MAX_FORMULA_LEN = 500
MAX_FORMULAS_PER_CALL = 20
VERIFY_TIMEOUT_SECONDS = 10.0


def extract_math_blocks(text: str) -> list[str]:
    """从回答文本中提取所有 $...$ / $$...$$ 公式块。"""
    blocks: list[str] = []
    for match in _MATH_BLOCK.finditer(text):
        raw = (match.group(1) or match.group(2) or "").strip()
        if raw:
            blocks.append(raw)
    return blocks


def _clean(latex: str) -> str:
    # math_format 会把 < > 转义为 \lt / \gt / \textless / \textgreater，解析前转回
    latex = latex.replace(r"\textless", "<").replace(r"\textgreater", ">")
    latex = latex.replace(r"\lt", "<").replace(r"\gt", ">")
    latex = _BOXED.sub(r"\1", latex)
    latex = _IMPLICIT_MUL.sub(r"\1\\cdot\2", latex)
    latex = latex.replace("\\left(", "(").replace("\\right)", ")")
    return _TRAILING.sub("", latex).strip()


def _parse_with_timeout(latex: str, timeout: float = VERIFY_TIMEOUT_SECONDS):
    """在守护线程中解析公式；超时返回 None，避免阻塞事件线程。"""
    result: dict = {}

    def worker() -> None:
        try:
            result["expr"] = latex2sympy(latex)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None
    if "error" in result:
        raise result["error"]
    return result.get("expr")


def verify_math(text: str) -> list[dict]:
    """逐条验算文本中的公式；返回 [{source, status, detail}]。"""
    if not CALCULATOR_AVAILABLE:
        return [
            {
                "source": text[:80],
                "status": "不可用",
                "detail": "sympy / latex2sympy2 未安装",
            }
        ]
    results: list[dict] = []
    for raw in extract_math_blocks(text)[:MAX_FORMULAS_PER_CALL]:
        if len(raw) > MAX_FORMULA_LEN:
            results.append(
                {
                    "source": raw[:60],
                    "status": "解析失败",
                    "detail": "公式过长（>500 字符），已跳过验算",
                }
            )
            continue
        latex = _clean(raw)
        try:
            expr = _parse_with_timeout(latex)
        except Exception as exc:
            results.append(
                {"source": raw, "status": "解析失败", "detail": str(exc)[:80]}
            )
            continue
        if expr is None:
            results.append(
                {"source": raw, "status": "解析失败", "detail": "验算超时"}
            )
            continue
        if expr is None or isinstance(expr, bool):
            results.append(
                {"source": raw, "status": "解析失败", "detail": "无法解析"}
            )
            continue
        if isinstance(expr, list):
            if not expr or not all(isinstance(item, sp.Equality) for item in expr):
                results.append(
                    {"source": raw, "status": "解析失败", "detail": "无有效解或解析为空"}
                )
                continue
            detail = " 或 ".join(sp.sstr(item) for item in expr[:3])
            results.append({"source": raw, "status": "解为", "detail": detail})
            continue
        if isinstance(expr, sp.Equality):
            try:
                diff = sp.simplify(expr.lhs - expr.rhs)
                if diff == 0:
                    results.append({"source": raw, "status": "成立", "detail": ""})
                elif not diff.free_symbols:
                    results.append(
                        {"source": raw, "status": "不成立", "detail": ""}
                    )
                else:
                    results.append(
                        {
                            "source": raw,
                            "status": "方程",
                            "detail": sp.sstr(expr)[:80],
                        }
                    )
            except Exception as exc:
                results.append(
                    {"source": raw, "status": "解析失败", "detail": str(exc)[:80]}
                )
        else:
            if "=" in raw:
                # 解析器把 x=2 这类式子丢掉了未知数，如实按"含未知数方程"处理
                results.append({"source": raw, "status": "方程", "detail": raw})
                continue
            try:
                detail = sp.sstr(expr)[:80]
            except Exception:
                detail = ""
            results.append({"source": raw, "status": "表达式", "detail": detail})
    return results
