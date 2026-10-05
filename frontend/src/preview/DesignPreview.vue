<script setup lang="ts">
/**
 * 设计预览（开发专用入口 #design，独立分包）：
 * 全部为假数据，不调用任何模型 API，也不复用生产页面的状态逻辑。
 * 目的：在用户确认设计方案前，隔离展示「以内容为中心」的
 * 布局、颜色、字体与交互细节（桌面聊天 / 登录 / 手机聊天三个视图）。
 * 方向：深色背景浅色文字 + 无卡片回答 + 克制浅蓝主色，
 * 高级感来自排版、留白、比例和细节，不靠玻璃效果、渐变或重阴影。
 * 公式渲染复用生产 StreamMessage 组件，保证预览即真实效果。
 */
import { ref } from 'vue'
import StreamMessage from '../components/StreamMessage.vue'
import { mockAnswer, mockQuestion, mockThinking } from '../mock/mockData'

type View = 'desktop' | 'login' | 'mobile'
const view = ref<View>('desktop')

// 第二轮问答的短回答（假数据，含行内公式、列表与引用）
const shortAnswer = [
  '第 (2) 问用配方法。先把常数项移到右边，再两边加上一次项系数一半的平方：',
  '',
  '$$x^2 - 4x + 3 = (x - 2)^2 - 1 = 0$$',
  '',
  '于是 $(x-2)^2 = 1$，开方得 $x_1 = 1$，$x_2 = 3$。',
  '',
  '- 检验：代入原方程，两个根都成立；',
  '- 提示：二次项系数不是 1 时，先提出系数再配方。',
  '',
  '> 配方的关键口诀：**加上一次项系数一半的平方**。',
].join('\n')

// 长公式示例：验证窄屏下块级公式局部横向滚动、不撑破页面
const longFormulaText = [
  '长公式在窄屏下应局部横向滚动，而不是把页面撑宽：',
  '',
  '$$\\int_0^{+\\infty} \\frac{x^3 e^{-x}}{(1-e^{-x})(1+x^2)^2}\\,dx = \\frac{\\pi^2}{30} \\approx 0.32298$$',
].join('\n')

const usageText =
  '累计用量 12,345 tokens · 今日已用 1,234 / 50,000（2.5%）· 登录 3 次 · 提问 6 次'

// 预览用“题目照片”占位图（内联 SVG，代表一张拍下来的几何题）
const photoSvg = [
  '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="220" viewBox="0 0 320 220">',
  '<rect width="320" height="220" fill="#fafbfc"/>',
  '<rect x="10" y="10" width="300" height="200" rx="6" fill="#fff" stroke="#dfe3e9"/>',
  '<text x="26" y="42" font-family="serif" font-size="15" fill="#3a3f47">2. 已知二次函数 y = x² - 4x + 3，</text>',
  '<text x="26" y="66" font-family="serif" font-size="15" fill="#3a3f47">(1) 求顶点坐标；(2) 求它的零点。</text>',
  '<g transform="translate(70,120)">',
  '<path d="M0 60 Q 45 -35 180 30" fill="none" stroke="#8fa6d9" stroke-width="2.5"/>',
  '<line x1="0" y1="60" x2="200" y2="60" stroke="#c6ccd4" stroke-width="1.5"/>',
  '<line x1="0" y1="60" x2="0" y2="-40" stroke="#c6ccd4" stroke-width="1.5"/>',
  '</g>',
  '<text x="278" y="200" font-family="sans-serif" font-size="10" fill="#b3bac4">photo</text>',
  '</svg>',
].join('')

const photoUrl = `data:image/svg+xml;utf8,${encodeURIComponent(photoSvg)}`
</script>

<!-- 非 scoped 样式已随深色方案上移到共享 style.css（body 深色、滚动条、焦点环），
     预览与真实页面共用同一份全局样式，这里不再有任何覆盖。 -->

<template>
  <div class="preview-root">
    <!-- 预览控制条：与被预览的界面本身明确区分 -->
    <div class="preview-bar">
      <strong>设计预览</strong>
      <span>假数据 · 独立入口 #design · 不连接模型、不写真实数据</span>
      <nav>
        <button :class="{ active: view === 'desktop' }" @click="view = 'desktop'">桌面聊天</button>
        <button :class="{ active: view === 'login' }" @click="view = 'login'">登录页</button>
        <button :class="{ active: view === 'mobile' }" @click="view = 'mobile'">手机聊天</button>
      </nav>
    </div>

    <!-- ============ 桌面聊天页 ============ -->
    <div v-if="view === 'desktop'" class="stage desktop-stage">
      <div class="page">
        <header class="header">
          <div class="brand">
            <span class="mark">∑</span>
            <h1>数学学习 AI 助手</h1>
          </div>
          <div class="user-bar">
            <span class="user-chip">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <circle cx="10" cy="7" r="3" />
                <path d="M4 16.5c1.2-2.8 3.4-4 6-4s4.8 1.2 6 4" />
              </svg>
              杨同学
            </span>
            <span class="divider" aria-hidden="true"></span>
            <select class="mode-select" aria-label="模式">
              <option>互动模式</option>
              <option>解答模式</option>
            </select>
            <button class="btn ghost" type="button">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <circle cx="10" cy="10" r="7.2" />
                <path d="M10 6.6v6.8M6.6 10h6.8" />
              </svg>
              新对话
            </button>
            <button class="btn ghost" type="button">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M8 4H5.5A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8M12.5 6.8 15.7 10l-3.2 3.2M15.5 10H7.8" />
              </svg>
              退出
            </button>
          </div>
        </header>

        <p class="notice">
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <circle cx="10" cy="10" r="7.2" />
            <path d="M10 9v4.4M10 6.4v.2" />
          </svg>
          已恢复上次对话历史（2 条）。
        </p>

        <details class="panel">
          <summary>
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <circle cx="10" cy="10" r="2.7" />
              <path
                d="M10 3v2M10 15v2M3 10h2M15 10h2M5 5l1.4 1.4M13.6 13.6 15 15M15 5l-1.4 1.4M6.4 13.6 5 15"
              />
            </svg>
            设置
            <span class="summary-hint">服务商 · 模型 · 学段 · 思考</span>
          </summary>
          <div class="panel-grid">
            <label>
              服务商
              <select><option>DeepSeek</option></select>
            </label>
            <label>
              模型
              <select><option>deepseek-chat</option></select>
            </label>
            <label>
              学段
              <select><option>高中</option></select>
            </label>
            <label class="checkbox">
              <input type="checkbox" checked />
              深度思考模式（更严谨，稍慢）
            </label>
            <label>
              思考强度
              <select><option>high</option></select>
            </label>
          </div>
        </details>

        <details class="panel">
          <summary>
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path d="M10 2.6l5.8 2.1v4.4c0 3.9-2.5 6.6-5.8 8.1-3.3-1.5-5.8-4.2-5.8-8.1V4.7z" />
              <path d="M7.4 10l1.9 1.9 3.4-3.6" />
            </svg>
            管理（仅本机可用）
            <span class="summary-hint">用量统计 · 密码重置</span>
          </summary>
          <div class="panel-body">
            <div class="stats-bar">
              <span>
                今日（10-05）全局已用 <strong>28,410</strong> / 100,000 tokens · 登录 12 次 · 提问 36 次
              </span>
              <button class="btn ghost small" type="button">
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path d="M15.5 10a5.5 5.5 0 1 1-1.6-3.9M15.5 3.5v3h-3" />
                </svg>
                刷新统计
              </button>
            </div>
            <table class="accounts">
              <thead>
                <tr>
                  <th>账号</th>
                  <th>状态</th>
                  <th class="num">今日用量</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>yang（杨同学）</td>
                  <td><span class="pill ok">正常</span></td>
                  <td class="num">1,234</td>
                </tr>
                <tr>
                  <td>guest01（访客）</td>
                  <td><span class="pill ok">正常</span></td>
                  <td class="num">860</td>
                </tr>
                <tr>
                  <td>old02</td>
                  <td><span class="pill off">停用</span></td>
                  <td class="num">0</td>
                </tr>
              </tbody>
            </table>
            <form class="reset">
              <label>
                重置密码账号
                <select><option>请选择账号</option></select>
              </label>
              <label>
                新密码
                <input type="password" placeholder="输入新密码" />
              </label>
              <button class="btn primary" type="button" disabled title="请先选择账号">
                确认重置
              </button>
            </form>
          </div>
        </details>

        <main class="chat">
          <div class="msg user">
            <div class="bubble">{{ mockQuestion }}</div>
          </div>

          <div class="msg assistant">
            <details class="thinking" open>
              <summary>
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path d="M10 3.2 12.2 8l5.1.5-3.8 3.4 1.1 5-4.6-2.7L5.4 16.9l1.1-5L2.7 8.5 7.8 8z" />
                </svg>
                思考过程
              </summary>
              <StreamMessage scope="pvd-think-1" :text="mockThinking" :done="true" />
            </details>
            <div class="answer">
              <StreamMessage scope="pvd-ans-1" :text="mockAnswer" :done="true" />
            </div>
          </div>

          <div class="msg user">
            <div class="bubble">
              <img :src="photoUrl" class="sent-image" alt="发送的题目图片" />
              <span>这道题的第 (2) 问怎么解？请用配方法。</span>
            </div>
          </div>

          <div class="msg assistant">
            <div class="answer">
              <StreamMessage scope="pvd-ans-2" :text="shortAnswer" :done="true" />
            </div>
          </div>

          <!-- 生成中的状态示例（假数据，静态展示） -->
          <div class="msg assistant">
            <details class="thinking streaming" open>
              <summary>
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path d="M10 3.2 12.2 8l5.1.5-3.8 3.4 1.1 5-4.6-2.7L5.4 16.9l1.1-5L2.7 8.5 7.8 8z" />
                </svg>
                思考过程（进行中）
                <span class="pulse" aria-hidden="true"></span>
              </summary>
              <div class="waiting">正在分析题目条件…</div>
            </details>
            <div class="answer pending">
              <span class="waiting">正在思考… 已用时 6 秒</span>
            </div>
          </div>

          <!-- 失败提示示例（假数据，文案与真实 App 的断连提示一致） -->
          <div class="msg assistant">
            <div class="fail">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <circle cx="10" cy="10" r="7.2" />
                <path d="M7.2 7.2l5.6 5.6M12.8 7.2l-5.6 5.6" />
              </svg>
              <span>连接中断，请稍后重试。</span>
              <button class="btn ghost small" type="button">重试</button>
            </div>
          </div>
        </main>

        <section class="meta">
          <p class="status success">
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <circle cx="10" cy="10" r="7.2" />
              <path d="M6.6 10.2l2.3 2.3 4.5-4.8" />
            </svg>
            回答已完成，已写入历史。
          </p>
          <p class="usage">{{ usageText }}</p>
        </section>

        <footer class="composer">
          <button class="icon-btn" type="button" title="添加图片（支持裁剪、旋转）">
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <rect x="2.5" y="3.5" width="15" height="13" rx="2" />
              <circle cx="7" cy="8" r="1.4" />
              <path d="M2.5 14l4.5-4 3 3 3.5-3.5 4 4" />
            </svg>
          </button>
          <input placeholder="输入数学问题，可附图片" />
          <button class="btn primary send" type="button">
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path d="M3.2 10.2 17 3.6l-3 12.6-4.2-4z" />
              <path d="M9.8 12.2 17 3.6" />
            </svg>
            发送
          </button>
        </footer>
      </div>
    </div>

    <!-- ============ 登录页 ============ -->
    <div v-else-if="view === 'login'" class="stage login-stage">
      <form class="login-card" @submit.prevent>
        <span class="mark large">∑</span>
        <h1>数学学习 AI 助手</h1>
        <p class="hint">请输入访问用户名与密码</p>
        <label>
          用户名
          <input autocomplete="username" placeholder="用户名" />
        </label>
        <label>
          密码
          <input type="password" autocomplete="current-password" placeholder="密码" />
        </label>
        <button class="btn primary" type="submit">登 录</button>
        <p class="foot">仅限授权用户访问 · 登录即记录用量</p>
      </form>
    </div>

    <!-- ============ 手机聊天页（390px 视口模拟） ============ -->
    <div v-else class="stage mobile-stage">
      <div class="phone">
        <div class="phone-page">
          <header class="m-header">
            <span class="mark small">∑</span>
            <h1>数学助手</h1>
            <button class="btn ghost small" type="button">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <circle cx="10" cy="10" r="7.2" />
                <path d="M10 6.6v6.8M6.6 10h6.8" />
              </svg>
              新对话
            </button>
          </header>

          <main class="m-chat">
            <div class="msg user">
              <div class="bubble">
                <img :src="photoUrl" class="sent-image" alt="发送的题目图片" />
                <span>这道题的第 (2) 问怎么解？</span>
              </div>
            </div>
            <div class="msg assistant">
              <div class="answer">
                <StreamMessage scope="pvd-m-1" :text="shortAnswer" :done="true" />
                <StreamMessage scope="pvd-m-2" :text="longFormulaText" :done="true" />
              </div>
            </div>
          </main>

          <p class="m-usage">{{ usageText }}</p>

          <footer class="m-composer">
            <div class="m-composer-row">
              <button class="icon-btn" type="button" aria-label="添加图片">
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <rect x="2.5" y="3.5" width="15" height="13" rx="2" />
                  <circle cx="7" cy="8" r="1.4" />
                  <path d="M2.5 14l4.5-4 3 3 3.5-3.5 4 4" />
                </svg>
              </button>
              <input placeholder="输入数学问题，可附图片" />
              <button class="btn primary send square" type="button" aria-label="发送">
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path d="M3.2 10.2 17 3.6l-3 12.6-4.2-4z" />
                  <path d="M9.8 12.2 17 3.6" />
                </svg>
              </button>
            </div>
          </footer>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ===== 设计令牌已上移到共享 style.css（:root），此处仅保留预览布局结构 ===== */
.preview-root {
  min-height: 100vh;
  background: var(--bg);
  color: var(--ink);
  color-scheme: dark;
  font-family: 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif;
}

/* ===== 预览控制条（非设计的一部分） ===== */
.preview-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 8px 16px;
  background: #23272e;
  color: #d6dae0;
  font-size: 0.82rem;
  position: static; /* 整页截图与阅读时不重复吸附 */
  z-index: 40;
}
.preview-bar strong {
  color: #fff;
  letter-spacing: 0.05em;
}
.preview-bar nav {
  margin-left: auto;
  display: flex;
  gap: 6px;
}
.preview-bar nav button {
  border: 1px solid #4a505a;
  background: transparent;
  color: #d6dae0;
  border-radius: 8px;
  padding: 4px 12px;
  font-size: 0.82rem;
  cursor: pointer;
}
.preview-bar nav button.active {
  background: var(--accent);
  border-color: var(--accent);
  color: #10131c;
}

.stage {
  padding: 24px 16px 48px;
}
.desktop-stage {
  display: flex;
  justify-content: center;
}

/* ===== 通用控件 ===== */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--line);
  border-radius: var(--radius-ctl);
  font-size: 0.88rem;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s, box-shadow 0.15s;
}
.btn svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  flex-shrink: 0;
}
.btn.ghost {
  background: transparent;
  color: var(--ink-2);
  padding: 6px 12px;
}
.btn.ghost:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-weak);
}
.btn.primary {
  border: none;
  background: var(--accent);
  color: #14151a;
  padding: 8px 18px;
  font-weight: 550;
}
.btn.primary:hover {
  background: var(--accent-deep);
}
.btn.primary:disabled {
  background: #2b3148;
  color: #7d8492;
  cursor: default;
}
.btn.ghost:disabled,
.btn.ghost:disabled:hover {
  border-color: var(--line-soft);
  color: var(--ink-3);
  background: transparent;
  cursor: default;
}
.icon-btn:disabled,
.icon-btn:disabled:hover {
  color: var(--ink-3);
  background: var(--surface);
  border-color: var(--line-soft);
  cursor: default;
}
.btn:focus-visible,
.icon-btn:focus-visible,
input:focus-visible,
select:focus-visible,
summary:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}
.icon-btn {
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink-2);
  width: 40px;
  height: 40px;
  border-radius: var(--radius-ctl);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
}
.icon-btn svg {
  width: 19px;
  height: 19px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.icon-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-weak);
}
select,
input {
  font: inherit;
  color: var(--ink);
}
/* 文本输入基础深色样式（输入区/聊天内输入各自覆盖） */
input:not([type='checkbox']) {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}
select {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 5px 8px;
  background: var(--surface);
  font-size: 0.88rem;
}

/* ===== 品牌标志 ===== */
.mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  background: var(--accent);
  color: #10131c;
  font-size: 1.05rem;
  font-family: Georgia, 'Times New Roman', serif;
  flex-shrink: 0;
}
.mark.large {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  font-size: 1.5rem;
}
.mark.small {
  width: 26px;
  height: 26px;
  border-radius: 8px;
  font-size: 0.9rem;
}

/* ===== 桌面页框架：单栏内容流，页头不加卡片 ===== */
.page {
  width: min(780px, 100%);
  min-height: calc(100vh - 96px);
  background: var(--bg);
  display: flex;
  flex-direction: column;
}
.header {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 2px;
  background: var(--bg);
  border-bottom: 1px solid var(--line-soft);
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
}
.brand h1 {
  font-size: 1.08rem;
  margin: 0;
  font-weight: 650;
  letter-spacing: 0.01em;
}
.user-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.88rem;
  color: var(--ink-2);
}
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 11px;
  border-radius: 999px;
  border: 1px solid var(--line);
  color: var(--ink-2);
  font-size: 0.84rem;
  background: transparent;
}
.user-chip svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.divider {
  width: 1px;
  height: 18px;
  background: var(--line);
}
.mode-select {
  border-radius: 999px;
  padding: 5px 10px;
  color: var(--ink);
}

.notice {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 12px 0 0;
  padding: 7px 12px;
  font-size: 0.84rem;
  color: var(--warn);
  background: var(--warn-bg);
  border: 1px solid #4a3d1e;
  border-radius: var(--radius-ctl);
}
.notice svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  flex-shrink: 0;
}

/* ===== 折叠面板（设置 / 管理）：默认折叠的白色表面，细边框少量阴影 ===== */
.panel {
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  margin-top: 12px;
  font-size: 0.88rem;
  background: var(--surface);
  box-shadow: var(--shadow-card);
}
.panel summary {
  cursor: pointer;
  user-select: none;
  padding: 9px 14px;
  color: var(--ink-2);
  display: flex;
  align-items: center;
  gap: 8px;
  list-style: none;
  border-radius: var(--radius-card);
}
.panel summary::-webkit-details-marker {
  display: none;
}
.panel summary::after {
  content: '';
  margin-left: auto;
  width: 8px;
  height: 8px;
  border-right: 1.6px solid var(--ink-3);
  border-bottom: 1.6px solid var(--ink-3);
  transform: rotate(45deg);
  transition: transform 0.15s;
}
.panel[open] summary::after {
  transform: rotate(225deg);
}
.panel summary svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.panel summary:hover {
  color: var(--ink);
  background: var(--surface-2);
}
.summary-hint {
  color: var(--ink-3);
  font-size: 0.78rem;
}
.panel-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 18px;
  padding: 6px 14px 14px;
}
.panel-grid label,
.reset label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: var(--ink-2);
  font-size: 0.84rem;
}
.panel-grid label.checkbox {
  flex-direction: row;
  align-items: center;
  gap: 7px;
  color: var(--ink);
  padding-top: 18px;
}
.panel-grid input[type='checkbox'] {
  width: 16px;
  height: 16px;
  accent-color: var(--accent);
}
.panel-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 4px 14px 14px;
}
.stats-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  color: var(--ink);
  font-size: 0.86rem;
}
.stats-bar strong {
  font-variant-numeric: tabular-nums;
  color: var(--ink);
}
.btn.small {
  padding: 4px 10px;
  font-size: 0.82rem;
  background: var(--surface);
}
.accounts {
  width: 100%;
  border-collapse: collapse;
  background: var(--surface);
  border: 1px solid var(--line-soft);
  border-radius: 10px;
  overflow: hidden;
  font-size: 0.85rem;
}
.accounts th,
.accounts td {
  text-align: left;
  padding: 7px 12px;
  border-bottom: 1px solid var(--line-soft);
}
.accounts th {
  color: var(--ink-2);
  font-weight: 550;
  background: var(--surface-2);
  font-size: 0.8rem;
}
.accounts tr:last-child td {
  border-bottom: none;
}
.accounts .num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.pill {
  display: inline-block;
  font-size: 0.74rem;
  border-radius: 999px;
  padding: 1px 9px;
}
.pill.ok {
  color: var(--ok);
  background: #1c2b22;
  border: 1px solid #2e4a38;
}
.pill.off {
  color: var(--ink-3);
  background: var(--surface-2);
  border: 1px solid var(--line-soft);
}
.reset {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  flex-wrap: wrap;
}
.reset select,
.reset input {
  min-width: 170px;
  padding: 7px 10px;
}

/* ===== 聊天区：回答直接呈现在页面背景上，无卡片 ===== */
.chat {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 22px;
  padding: 20px 2px;
}
.msg {
  display: flex;
}
.msg.user {
  justify-content: flex-end;
}
.msg.user .bubble {
  background: var(--user-bubble);
  color: var(--ink);
  border-radius: 16px 16px 4px 16px;
  padding: 9px 14px;
  max-width: 78%;
  white-space: pre-wrap;
  overflow-wrap: break-word;
  line-height: 1.65;
  font-size: 0.95rem;
}
.msg.assistant {
  flex-direction: column;
}
.msg.assistant .answer {
  max-width: 100%;
  font-size: 0.95rem;
  line-height: 1.8;
  color: var(--ink);
}
.sent-image {
  display: block;
  max-width: 240px;
  max-height: 170px;
  border-radius: 10px;
  border: 1px solid var(--line);
  margin-bottom: 8px;
  background: #fff;
}

/* 回答内部排版（标题/段落/列表/引用/公式/代码）已统一到 StreamMessage 共享样式 */

/* 思考区：紧凑状态行 + 弱化内容，不再像第二张大卡片 */
.thinking {
  border: none;
  border-left: 2px solid var(--line);
  border-radius: 0;
  padding: 1px 0 1px 12px;
  margin-bottom: 10px;
  background: transparent;
  font-size: 0.9em;
  color: var(--ink-2);
  max-width: 100%;
}
.thinking[open] {
  padding-bottom: 2px;
}
.thinking summary {
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 6px;
  list-style: none;
  color: var(--ink-2);
  font-size: 0.82rem;
}
.thinking summary::after {
  content: '';
  margin-left: auto;
  width: 7px;
  height: 7px;
  border-right: 1.5px solid var(--ink-3);
  border-bottom: 1.5px solid var(--ink-3);
  transform: rotate(45deg);
  transition: transform 0.15s;
}
.thinking[open] summary::after {
  transform: rotate(225deg);
}
.thinking summary::-webkit-details-marker {
  display: none;
}
.thinking summary svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linejoin: round;
}
.thinking summary:hover {
  color: var(--ink);
}
.thinking :deep(.stream-body) {
  color: var(--ink-2);
  font-size: 0.92em;
}
.thinking.streaming {
  border-left-color: var(--accent);
}
.pulse {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  margin-left: 2px;
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%,
  100% {
    opacity: 0.25;
  }
  50% {
    opacity: 1;
  }
}
.waiting {
  color: var(--ink-3);
  font-size: 0.9em;
}

/* 失败提示：图标 + 文字 + 操作，不单靠颜色 */
.fail {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 2px 0;
  font-size: 0.88rem;
  color: var(--ink-2);
}
.fail svg {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: var(--danger);
  stroke-width: 1.6;
  stroke-linecap: round;
  flex-shrink: 0;
}
.fail .btn.small {
  margin-left: 2px;
}

/* 状态与用量：弱化的紧凑文字行 */
.meta {
  padding: 0 2px;
  border-top: 1px solid var(--line-soft);
  padding-top: 12px;
}
.status {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  padding: 0;
  font-size: 0.82rem;
  color: var(--ink-2);
  background: none;
  border: none;
}
.status svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: var(--ok);
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  flex-shrink: 0;
}
.usage {
  margin: 6px 0 0;
  padding: 0;
  font-size: 0.78rem;
  color: var(--ink-3);
  white-space: pre-wrap;
  font-variant-numeric: tabular-nums;
}

/* ===== 输入区：白色实心表面 + 细边框 + 少量阴影，专注但不厚重 ===== */
.composer {
  position: sticky;
  bottom: 0;
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 8px 10px;
  margin: 18px 0 14px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-pop);
}
.composer input {
  flex: 1;
  border: none;
  background: transparent;
  padding: 8px 4px;
  font-size: 0.95rem;
  outline: none;
}
.composer input::placeholder {
  color: var(--ink-3);
}
.composer input:focus-visible {
  box-shadow: none;
}
.composer:focus-within {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
.send {
  padding: 0 18px;
  height: 38px;
  white-space: nowrap;
}
.send svg {
  width: 15px;
  height: 15px;
}

/* ===== 登录页：同配色同控件，简洁无装饰插画 ===== */
.login-stage {
  display: flex;
  justify-content: center;
  padding-top: 9vh;
}
.login-card {
  width: min(360px, 92%);
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 16px;
  box-shadow: var(--shadow-pop);
  padding: 34px 30px 26px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  text-align: center;
}
.login-card .mark.large {
  align-self: center;
  margin-bottom: 2px;
}
.login-card h1 {
  font-size: 1.25rem;
  margin: 0;
  font-weight: 650;
}
.login-card .hint {
  margin: 0 0 10px;
  color: var(--ink-3);
  font-size: 0.86rem;
}
.login-card label {
  display: flex;
  flex-direction: column;
  gap: 5px;
  text-align: left;
  font-size: 0.84rem;
  color: var(--ink-2);
}
.login-card input {
  border: 1px solid var(--line);
  border-radius: var(--radius-ctl);
  padding: 10px 12px;
  font-size: 0.95rem;
  background: var(--surface);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.login-card input:focus-visible {
  border-color: var(--accent);
}
.login-card .btn.primary {
  margin-top: 6px;
  padding: 11px 0;
  font-size: 0.95rem;
  letter-spacing: 0.3em;
  text-indent: 0.3em;
}
.login-card .foot {
  margin: 12px 0 0;
  font-size: 0.76rem;
  color: var(--ink-3);
}

/* ===== 手机视图 ===== */
.mobile-stage {
  display: flex;
  justify-content: center;
}
.phone {
  width: 390px;
  max-width: 100%;
  height: 780px;
  border: 10px solid #0b0c10;
  border-radius: 40px;
  background: var(--bg);
  overflow: hidden;
  box-shadow: var(--shadow-pop);
}
.phone-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 12px 12px 10px;
}
.m-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line-soft);
}
.m-header h1 {
  font-size: 0.98rem;
  margin: 0;
  font-weight: 650;
  flex: 1;
}
.m-header .btn.small {
  padding: 5px 9px;
  font-size: 0.78rem;
}
.m-chat {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 14px 2px;
}
.m-chat .msg.user .bubble {
  max-width: 92%;
}
.m-chat .msg.assistant .answer {
  font-size: 0.92rem;
}
.m-chat .sent-image {
  max-width: 180px;
  max-height: 130px;
}
.m-usage {
  margin: 4px 0 6px;
  padding: 0 4px;
  font-size: 0.72rem;
  color: var(--ink-3);
  white-space: pre-wrap;
}
.m-composer {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-pop);
  padding: 8px;
}
.m-composer:focus-within {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
.m-composer-row {
  display: flex;
  gap: 6px;
  align-items: center;
}
.m-composer input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  padding: 8px 4px;
  font-size: 0.92rem;
  outline: none;
}
.m-composer input::placeholder {
  color: var(--ink-3);
}
.m-composer input:focus-visible {
  box-shadow: none;
}
.m-composer .send.square {
  width: 38px;
  padding: 0;
  justify-content: center;
}
</style>
