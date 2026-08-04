# 🧮 数学学习 AI 助手（Math_AI_Assistant）

基于 **Python + Gradio + DeepSeek 大模型**的数学学习助手，采用苏格拉底式引导教学，支持 LaTeX 公式渲染与流式输出。

## 目录结构

```text
Math_AI_Assistant/
├── app.py                  # 入口：Gradio 界面 + 对话逻辑
├── requirements.txt        # 依赖清单
├── .env.example            # 环境变量示例（复制为 .env 后填写密钥）
├── .gitignore
├── README.md
├── PLAN.md                 # 分步实施计划
└── core/
    ├── __init__.py
    ├── config.py           # 配置读取与校验
    ├── prompts.py          # 系统提示词（教学人设）
    └── deepseek_client.py  # DeepSeek API 封装（流式）
```

## 运行步骤

```bash
# 1. 安装依赖（建议在项目内创建虚拟环境）
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt

# 2. 配置 API Key
# 复制 .env.example 为 .env，填入 DEEPSEEK_API_KEY

# 3. 启动
python app.py
# 浏览器打开 http://127.0.0.1:7860
```

## 关键设计说明

- **模型**：默认 `deepseek-v4-flash`（2026-07 起的新模型名，旧名 `deepseek-chat`/`deepseek-reasoner` 已弃用）。`deepseek-v4-pro` 更强但更贵，可在 `.env` 中切换。
- **思考模式**：在 `.env` 中把 `DEEPSEEK_THINKING=true` 可开启思维链，数学推理更严谨；界面会隐藏思维链内容，只展示最终回答。
- **教学方式**：系统提示词要求「不直接给答案、先引导、分步讲、用 LaTeX」；支持学段与教学模式选择、拍照识题、本地验算等功能。

## 拍照识题

- 在输入框旁上传或粘贴题目图片，助手会用百炼视觉模型（`qwen3.7-flash`）把题目识别成文字并填入输入框，确认/修改后点击发送即可让 DeepSeek 讲解。
- 这是可选功能：需要在 `.env` 中配置 `DASHSCOPE_API_KEY`；不配置也能正常使用纯文本聊天。

## 手机访问（同一 Wi-Fi）

- 启动助手后，把电脑和手机连接到同一个 Wi-Fi。
- 手机浏览器打开启动窗口里显示的地址：`http://<电脑IP>:7860`（电脑的局域网 IP 可在启动窗口或 `ipconfig` 中查看）。
- 上传题目图片时，手机系统会弹出「拍照或从相册选择」，可直接选择相册里的照片。
- 注意：同一网络下的其他设备也能访问该地址；介意的话可以在后续版本加访问密码。

详细规划见 [PLAN.md](PLAN.md)。
