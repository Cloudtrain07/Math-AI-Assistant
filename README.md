# 🧮 数学学习 AI 助手（Math_AI_Assistant）

基于 **Python + Gradio + DeepSeek 大模型**的数学学习助手，采用苏格拉底式引导教学，支持 LaTeX 公式渲染、流式输出、拍照识题、本地验算与公网访问。

## 功能特性

- **流式对话**：按学段（小学/初中/高中/大学）与教学模式（互动/解答）组装提示词，互动模式内置阶段引导、批改与练习子流程
- **思考进度与超时保护**：深度思考期间状态栏每秒刷新已用时与估算思考 token；超过 `MAX_WAIT_SECONDS`（默认 600 秒）自动停止，避免无限等待
- **思考过程展示**：深度思考的思维链以可折叠面板展示（公式按 LaTeX 渲染），仅当轮可见、不写入历史、不回传 API
- **模型/思考强度切换**：界面可直接切换 `deepseek-v4-flash` / `deepseek-v4-pro` 与思考强度 `low / high / max`，无需改配置重启
- **继续生成**：回答因长度限制被截断时，可一键带上题目原文继续生成
- **LaTeX 渲染**：自动把模型输出的不规范公式归一化为 KaTeX 可渲染格式
- **拍照识题**：上传/粘贴题目图片，用百炼视觉模型自动转写为文字
- **本地验算**：用 sympy 验算回答中的公式（含不等式的 `\lt` 写法自动还原）
- **会话用量统计**：状态栏显示本次用量与本会话累计 token、轮数
- **每日成本护栏**：每访客（按 IP）每天最多 `DAILY_TOKEN_LIMIT`（默认 100 万）、全局每天最多 `GLOBAL_DAILY_TOKEN_LIMIT`（默认 300 万），超限自动拒绝
- **输入限制**：单条消息超过 `MAX_MESSAGE_LEN`（默认 2000 字符）会被直接拒绝，不占用额度
- **访问日志**：记录登录、提问、回复与超额事件，`data/access.log` 只保留最近 1000 条
- **错误诊断日志**：API 异常与"空回复"的完整信息（错误类型、token、配置）自动写入 `data/error.log`
- **主人用量面板**：本机（localhost）点击“刷新用量”可查看今日全局/各访客用量、登录与提问次数
- **公网访问**：启动时自动建立 Cloudflare 快速隧道，生成随机密码，任何设备凭用户名 `math` + 密码访问
- **消息复制**：每条消息自带复制按钮

## 公网访问（cloudflared）

1. 下载 `cloudflared-windows-amd64.exe`（GitHub Releases: `cloudflare/cloudflared`），重命名为 `cloudflared.exe` 放到项目根目录（已加入 `.gitignore`，不会入库）
2. 运行 `python app.py`（或 `启动数学助手.bat`），启动窗口会显示：
   - 公网链接：`https://xxx.trycloudflare.com`（每次重启都会变化）
   - 用户名 `math` 与随机 8 位密码（完整信息同时写入 `data/当前访问密码.txt`）
3. 把公网链接和密码分享给信任的人；关闭程序后链接失效

说明：
- 隧道运行中若 cloudflared 进程退出，程序会自动重连并更新链接（`data/cloudflared.pid` 用于启动时清理残留进程）
- 电脑需保持开机运行程序，公网才能访问
- 局域网内仍可直接访问 `http://<电脑IP>:7860`

## 运行步骤

```bash
# 1. 安装依赖（建议在项目内创建虚拟环境）
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt

# 2. 配置 API Key
# 复制 .env.example 为 .env，填入 DEEPSEEK_API_KEY（可选 DASHSCOPE_API_KEY 用于拍照识题）

# 3. 启动
python app.py
# 浏览器打开 http://127.0.0.1:7860
```

## 配置项（.env）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | - | DeepSeek API 密钥（必填） |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 默认模型（可在界面下拉切换 `flash` / `pro`） |
| `DEEPSEEK_THINKING` | `false` | 是否开启思考模式 |
| `DEEPSEEK_REASONING_EFFORT` | `high` | 默认思考强度：`low/high/max`（可在界面下拉切换） |
| `MAX_HISTORY_MESSAGES` | `20` | 保留的最大历史消息条数 |
| `MAX_OUTPUT_TOKENS` | `30000` | 单次回答最大输出 token |
| `MAX_WAIT_SECONDS` | `600` | 单次请求最长等待秒数（含思考时间），超时自动停止 |
| `MAX_MESSAGE_LEN` | `2000` | 单条消息最大长度（字符），超长拒绝 |
| `DAILY_TOKEN_LIMIT` | `1000000` | 每访客每日 token 上限 |
| `GLOBAL_DAILY_TOKEN_LIMIT` | `3000000` | 全局每日 token 总上限 |
| `DASHSCOPE_API_KEY` | - | 百炼视觉密钥（拍照识题，可选） |
| `VISION_MODEL` | `qwen3.7-flash-2026-07-15` | 视觉模型名 |

## 目录结构

```text
Math_AI_Assistant/
├── app.py                  # 入口：Gradio 界面 + 对话逻辑 + 启动隧道
├── requirements.txt        # 运行依赖
├── requirements-dev.txt    # 测试依赖（pytest）
├── .env.example            # 环境变量示例
├── .gitignore
├── README.md
├── PLAN.md                 # 分步实施计划
├── core/
│   ├── access.py           # 公网密码 / 访问信息文件 / 访问日志 / 主人判定
│   ├── calculator.py       # sympy 本地验算（含长度/条数/超时限制）
│   ├── config.py           # 配置读取与校验
│   ├── deepseek_client.py  # DeepSeek API 封装（流式）
│   ├── math_format.py      # LaTeX 归一化（< > 转义等）
│   ├── prompts.py          # 系统提示词（双模式教学 + 防注入）
│   ├── storage.py          # 会话历史持久化（原子写入）
│   ├── tunnel.py           # cloudflared 快速隧道（自愈 + 残留清理）
│   ├── usage.py            # 会话/每日用量统计与限额
│   └── vision_client.py    # 百炼视觉识图
└── tests/                  # pytest 回归测试（75 用例）
```

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

覆盖：LaTeX 归一化、验算、用量/限额逻辑、访问日志裁剪、隧道解析与残留清理、配置校验、respond 主流程、思考面板展示、继续生成。

## 其他说明

- **错误处理与诊断**：API 请求失败会给出中文友好提示并保留已生成的部分内容；失败详情（错误类型、消息、token、模型与思考配置）自动写入 `data/error.log`，便于排查
- **数据目录**：`data/`（聊天记录、用量、访问信息、日志）已加入 `.gitignore`，不会入库
- **隐私提示**：`data/access.log` 与用量统计中包含访客 IP，仅用于成本统计与安全防护

详细规划见 [PLAN.md](PLAN.md)。
