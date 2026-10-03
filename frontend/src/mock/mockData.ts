/**
 * 原型用假数据：不调用任何模型 API。
 * 回答内容刻意包含：大量行内公式、块级公式、列表、引用、代码块（内含 $ 与行内代码）、
 * 以及纯文本美元符号样例，用来验证公式稳定性和"不胡乱推断"。
 */

export const mockQuestion = '请讲解圆的面积公式，举例计算半径为 3 的圆面积，并给出一段 Python 代码。'

export const mockAnswer: string = [
  '好的，我们来系统讲解圆的面积。先看一个重要结论：圆的面积公式是 $S = \\pi r^2$，其中 $r$ 是半径，$\\pi \\approx 3.14159$。',
  '',
  '## 一、公式从哪里来',
  '',
  '把圆平均分成若干份，交错拼成一个近似的长方形。长方形的长约为圆周长的一半，即 $\\frac{C}{2} = \\pi r$；宽约为半径 $r$。因此面积为',
  '',
  '$$S = \\pi r \\times r = \\pi r^2$$',
  '',
  '这就是圆面积的来源。注意这里的推导用到了极限思想：当份数 $n$ 越大，拼出的图形越接近长方形，误差 $\\varepsilon \\to 0$。',
  '',
  '## 二、计算半径为 3 的圆',
  '',
  '代入公式 $r = 3$：',
  '',
  '$$S = \\pi \\times 3^2 = 9\\pi \\approx 28.274$$',
  '',
  '也就是说，半径为 3 的圆面积约是 $28.27$（保留两位小数）。如果题目要求精确值，就写 $9\\pi$。',
  '',
  '## 三、几个常见变形',
  '',
  '- 已知直径 $d$：先用 $r = \\frac{d}{2}$，再代入 $S = \\pi r^2$。',
  '- 已知面积求半径：由 $S = \\pi r^2$ 得 $r = \\sqrt{\\frac{S}{\\pi}}$。',
  '- 半圆的面积：$S_{半} = \\frac{1}{2}\\pi r^2$，别忘了除以 $2$。',
  '- 环形面积：大圆减小圆，$S_{环} = \\pi(R^2 - r^2)$。',
  '',
  '> 记忆口诀：**先算半径的平方，再乘圆周率 $\\pi$**。',
  '',
  '## 四、Python 代码',
  '',
  '下面用代码验证一下（注意代码里的美元符号只是普通字符，例如价格 $5）:',
  '',
  '```python',
  'import math',
  '',
  'def circle_area(r: float) -> float:',
  '    """计算圆的面积，r 是半径"""',
  '    return math.pi * r ** 2',
  '',
  'print(f"半径为 3 的圆面积约为 {circle_area(3):.3f}")  # 约 28.274',
  'print("半价优惠后只要 $5，原价 $10")  # 代码里的 $ 不是公式',
  '```',
  '',
  '运行后输出 `28.274`，与手算结果一致（这里 `$5` 只是字符串内容）。',
  '顺带一提：行内代码 `$x$` 里的美元符号也不会变成公式，即使分片正好切在反引号中间。',
  '',
  '## 五、小结',
  '',
  '1. 圆面积公式 $S = \\pi r^2$；',
  '2. 半径为 3 时，$S = 9\\pi \\approx 28.274$；',
  '3. 常见变形包括直径求面积、面积求半径等，核心都是 $r^2$ 与 $\\pi$ 的组合。',
  '',
  '如果需要，我还可以出一道练习题让你巩固 \\(S = \\pi r^2\\) 的使用。',
].join('\n')

export const mockThinking: string = [
  '用户想了解圆面积公式的来源与计算。我应该先给出推导思路：把圆切分成扇形拼成近似长方形，得到 $S = \\pi r^2$。',
  '然后代入 $r = 3$ 计算 $9\\pi \\approx 28.274$。',
  '需要注意用户可能混淆直径与半径，要在变形部分补充 $r = \\frac{d}{2}$。',
  '最后给一段可运行的 Python 示例，并提醒代码中的美元符号不是公式分隔符。',
].join('\n')

/** 含非法 LaTeX 与行尾未闭合公式，验证渲染失败降级 */
export const mockBadAnswer: string = [
  '这一段演示渲染失败的降级行为。下面是一个合法公式 $a^2 + b^2 = c^2$，应该正常排版。',
  '',
  '接着是一个内容非法的公式：$$\\frac{1}{2 + \\unknowncmd{3}}$$，KaTeX 会以红色错误样式显示原始 LaTeX，而不是丢掉它。',
  '',
  '还有一类是行内代码没有闭合：`$x$ 与 \\frac{1}{2}，到段落结束都按原文保留，其中的内容不会被误判成公式。',
  '',
  '最后是一个直到回答结束都没有闭合的公式：$x + y =，它在结束时按普通文本原样显示，不会卡住后续输出。',
].join('\n')

/** 纯文本美元符号，验证不胡乱推断成公式 */
export const mockPriceText: string = [
  '这本书价格 $5，另一本 $10。',
  '两本合计 $15，注意这里没有任何公式，所有美元符号都应原样显示。',
].join('\n')

/** 仅含一个公式：验证"只有一个公式被错误删除"时观测器必须报警而不是当成清空 */
export const mockSingleFormula: string = [
  '圆的面积公式如下：',
  '',
  '$$S = \\pi r^2$$',
  '',
  '这是本页唯一的公式。',
  '接下来的文字继续流式输出，片刻后上面的公式会被故意从页面中删除，',
  '用于验证观测器能把这次删除报告为异常，而不是误判成正常的清空操作。',
  '流式输出仍在继续，公式此刻应当已经完成排版并且保持稳定。',
].join('\n')

/** 简单可复现的伪随机数 */
function makeRng(seed: number): () => number {
  let s = seed >>> 0
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0
    return s / 0x100000000
  }
}

/**
 * 把全文切成"累计快照"序列（模拟核心服务 yield 累计 reply 的形态）。
 * 切分点刻意落在公式分隔符与 LaTeX 命令中间：每个 $ 之后、\frac、\sqrt、
 * \pi、\times、\varepsilon 命令的第 2 个字符后，其余用小步随机切分加密。
 */
export function buildChunks(text: string, seed = 42): string[] {
  const forced: number[] = []
  for (const [needle, cutAfter] of [['$', 1], ['\\frac', 2], ['\\sqrt', 2], ['\\pi', 2], ['\\times', 2], ['\\varepsilon', 2]] as const) {
    let from = 0
    for (;;) {
      const idx = text.indexOf(needle, from)
      if (idx === -1) break
      forced.push(idx + cutAfter)
      from = idx + needle.length
    }
  }
  const rng = makeRng(seed)
  let pos = 0
  const points: number[] = []
  const sorted = [...new Set(forced)].sort((a, b) => a - b)
  for (const p of sorted) {
    while (pos < p) {
      pos = Math.min(pos + 6 + Math.floor(rng() * 20), p)
      if (pos > 0 && pos < text.length) points.push(pos)
    }
    if (p > 0 && p < text.length) points.push(p)
  }
  while (pos < text.length) {
    pos = Math.min(pos + 6 + Math.floor(rng() * 20), text.length)
    if (pos < text.length) points.push(pos)
  }
  const unique = [...new Set(points)].sort((a, b) => a - b)
  return [...unique.map((p) => text.slice(0, p)), text]
}
