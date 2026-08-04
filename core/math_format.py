"""数学输出净化：把模型输出的不规范 LaTeX 统一成 KaTeX 可识别的 $...$ / $$...$$。"""

from __future__ import annotations

import re

_MATH_SPAN = re.compile(r"\$\$.*?\$\$|\$.*?\$", re.S)
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")
_ESCAPED_PAREN = re.compile(r"\\\(([\s\S]*?)\\\)")
_ESCAPED_BRACKET = re.compile(r"\\\[([\s\S]*?)\\\]")
_BRACKET_BLOCK = re.compile(r"(?m)^[ \t]*\[([\s\S]*?)\][ \t]*$")
# 公式被 ** 加粗包裹：完整公式去掉加粗；闭合 $$ 被 ** 顶替时补回闭合 $$
_BOLD_MATH = re.compile(r"\*\*(\$\$.*?\$\$|\$.*?\$)\*\*", re.S)
_BOLD_MATH_UNCLOSED = re.compile(r"\*\*(\$\$.*?)\*\*", re.S)
# 尾部未闭合的公式（自动补闭合符）：$$... 或 $... 到行尾
_UNCLOSED_DISPLAY = re.compile(r"\$\$([^$\n]*)$")
_UNCLOSED_INLINE = re.compile(r"\$([^$\n]*)$")
# 句子中间的未闭合公式：$$... 或 $... 后紧跟中文时，在中文前补上闭合符
_UNCLOSED_DISPLAY_MID = re.compile(r"\$\$([^$\n]*?)(?<!\{)(?=\s*[\u4e00-\u9fff])")
_UNCLOSED_INLINE_MID = re.compile(r"\$([^$\n]*?)(?<!\{)(?=\s*[\u4e00-\u9fff])")
_TEXT_CMD = re.compile(r"\\text\{[^{}]*\}")


def _protect(text: str) -> tuple[str, list[str]]:
    """把已有的 $...$ / $$...$$ 内容替换为占位符，避免被后续规则误伤。"""
    parts: list[str] = []

    def _repl(match: re.Match) -> str:
        parts.append(match.group(0))
        return f"\x00{len(parts) - 1}\x00"

    return _MATH_SPAN.sub(_repl, text), parts


def _restore(text: str, parts: list[str]) -> str:
    for index, part in enumerate(parts):
        text = text.replace(f"\x00{index}\x00", part)
    return text


def _convert_escaped_delimiters(text: str) -> str:
    r"""\(...\) -> $...$；\[...\] -> $$...$$；内容里已有公式占位符时跳过。"""
    def _paren(match: re.Match) -> str:
        content = match.group(1)
        if "\x00" in content:
            return match.group(0)
        return f"${content.strip()}$"

    def _bracket(match: re.Match) -> str:
        content = match.group(1)
        if "\x00" in content:
            return match.group(0)
        return f"$${content.strip()}$$"

    text = _ESCAPED_PAREN.sub(_paren, text)
    text = _ESCAPED_BRACKET.sub(_bracket, text)
    return text


def _unwrap_parentheses(text: str) -> str:
    """去掉直接包在行内公式外面的圆括号：( $x$ ) -> $x$。"""
    return re.sub(r"\(\s*(\x00\d+\x00)\s*\)", r"\1", text)


def _convert_bracket_blocks(text: str) -> str:
    """把单独成行且含反斜杠/换行的 [ ... ] 显示块转成 $$...$$，普通方括号不动。"""
    def _repl(match: re.Match) -> str:
        content = match.group(1)
        if "\x00" not in content and ("\\" in content or "\n" in content):
            return f"$${content}$$\n"
        return match.group(0)

    return _BRACKET_BLOCK.sub(_repl, text)


def _strip_bold_around_math(text: str) -> str:
    """去掉直接包裹公式的 ** 加粗标记；普通加粗文字不受影响。"""
    text = _BOLD_MATH.sub(r"\1", text)
    text = _BOLD_MATH_UNCLOSED.sub(
        lambda m: "$$" + m.group(1)[2:] + "$$", text
    )
    return text


def _fix_mid_text_math(text: str) -> str:
    """句子中间的未闭合公式自动补闭合；已闭合公式与普通文本不受影响。"""
    def _disp(match: re.Match) -> str:
        content = match.group(1)
        if content.count("{") != content.count("}"):
            return match.group(0)
        if re.search(r"[\\{}^_0-9a-zA-Z]", content):
            return "$$" + content + "$$"
        return match.group(0)

    def _inline(match: re.Match) -> str:
        content = match.group(1)
        if content.count("{") != content.count("}"):
            return match.group(0)
        if re.search(r"[\\{}^_]", content):
            return "$" + content + "$"
        return match.group(0)

    text = _UNCLOSED_DISPLAY_MID.sub(_disp, text)
    text = _UNCLOSED_INLINE_MID.sub(_inline, text)
    return text


def _escape_angles_in_math(text: str) -> str:
    r"""把公式内的 < > 替换为 \lt / \gt（\text{...} 文本模式内改用 \textless / \textgreater）。

    Gradio 聊天框前端不会转义公式里的 <，浏览器会把 <a... 当成未闭合的
    HTML 标签并吞掉该消息后面的全部内容（分类讨论因此“消失”）。
    KaTeX 中这些命令渲染效果与 < > 相同，可安全替换。
    """
    def _repl(match: re.Match) -> str:
        span = match.group(0)
        display = span.startswith("$$") and span.endswith("$$")
        core = span[2:-2] if display else span[1:-1]
        texts: list[str] = []

        def _save(m: re.Match) -> str:
            texts.append(m.group(0))
            return f"\x01{len(texts) - 1}\x01"

        core = _TEXT_CMD.sub(_save, core)
        core = core.replace("<", r"\lt ").replace(">", r"\gt ")
        for index, part in enumerate(texts):
            part = part.replace("<", r"\textless ").replace(">", r"\textgreater ")
            core = core.replace(f"\x01{index}\x01", part)
        return ("$$" if display else "$") + core + ("$$" if display else "$")

    return _MATH_SPAN.sub(_repl, text)


def _fix_trailing_math(text: str) -> str:
    """给尾部未闭合的公式自动补上闭合符；仅当内容是 LaTeX 风格（含反斜杠、花括号、上下标）时才补。"""
    def _looks_like_math(content: str) -> bool:
        return bool(re.search(r"[\\{}^_]", content))

    match = _UNCLOSED_DISPLAY.search(text)
    if match and _looks_like_math(match.group(1)):
        return text + "$$"
    match = _UNCLOSED_INLINE.search(text)
    if match and _looks_like_math(match.group(1)):
        return text + "$"
    return text


def _normalize_once(text: str) -> str:
    text = _strip_bold_around_math(text)
    # 先保护已闭合的公式，避免 _fix_mid_text_math 误伤含中文的公式（如 \text{在}）
    protected, parts = _protect(text)
    protected = _fix_mid_text_math(protected)
    protected = _convert_escaped_delimiters(protected)
    protected = _unwrap_parentheses(protected)
    protected = _convert_bracket_blocks(protected)
    text = _restore(protected, parts)
    # 最后把公式内的 < > 转义，防止前端把 < 当作 HTML 标签吞掉后续内容
    return _escape_angles_in_math(text)


def normalize_math_text(text: str, auto_close: bool = False) -> str:
    """把常见的不规范 LaTeX 写法改写成 KaTeX 可渲染的格式；幂等。

    auto_close=True 时，额外给尾部未闭合的公式补上闭合符（用于用户输入与历史恢复）。
    """
    result = text
    for _ in range(2):
        result = _normalize_once(result)
    if auto_close:
        result = _fix_trailing_math(result)
    return result
