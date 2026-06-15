# concept-video

输入任意概念词，自动生成带旁白、字幕、动画的讲解视频，并可一键批量上传小红书草稿箱。

## 效果

- 风格：3Blue1Brown / 李永乐老师式动画讲解
- 时长：1 ~ 3 分钟
- 输出：16:9 横版 + 3:4 小红书 + 9:16 抖音，三版 MP4 同时生成
- 附赠：SRT 字幕文件 + 3 道概念测试题（`题目.md`）

适用范围不限 AI，心理学、经济学、物理、历史等任意概念均可。

## 流程

```
概念词
  → ① LLM 生成分段脚本 + 分镜（钩子/比喻/核心机制/误区/互动结尾）
  → ② Edge-TTS 合成旁白（免费，词级时间戳对齐字幕）
  → ③ LLM 生成 Manim 动画代码，沙箱渲染，报错自动修复重试
  → ④ 生成 SRT 字幕（一句一条，永不压画面）
  → ⑤ ffmpeg 音画对齐 + concat + 三画幅烧字幕
  → ⑥（可选）自动存入小红书草稿箱（含封面 + 标题 + 正文 + 话题）
  → ⑦ 生成 3 道概念测试题（选择 + 填空 + 问答）
```

## 依赖

```bash
# 系统依赖（macOS）
brew install ffmpeg

# Python 依赖（建议用 .venv）
pip install anthropic edge-tts manim pillow playwright
playwright install chromium
```

> Manim 需要 Python ≥ 3.9；字幕用 PingFang SC（macOS 内置）

## 配置

在同目录创建 `config_local.py`（不进 git）：

```python
AI_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding"
AI_API_KEY  = "你的豆包 API Key"
AI_MODEL    = "doubao-seed-2.0-pro"
```

其他大模型（OpenAI / Claude 等）改 `AI_BASE_URL` 和 `AI_MODEL` 即可，调用方式兼容 Anthropic SDK。

## 用法

**交互模式**（推荐新手）：

```bash
.venv/bin/python concept_video.py
```

**命令行模式**：

```bash
# 单个概念
.venv/bin/python concept_video.py "梯度下降"

# 指定参数
.venv/bin/python concept_video.py "条件反射" --audience 中学生 --minutes 2 --quality m

# 批量生成
.venv/bin/python concept_video.py "供需曲线" "边际效用" "纳什均衡"

# 批量生成 + 自动存小红书草稿
.venv/bin/python concept_video.py "认知失调" "沉没成本" --xhs

# 只给已有文件夹补传小红书
.venv/bin/python concept_video.py --xhs-only

# 只给已有文件夹补生成测试题
.venv/bin/python concept_video.py --quiz-only
```

**参数说明**：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--audience` | 成人零基础 | 小学生 / 中学生 / 成人零基础 |
| `--minutes` | 2.0 | 目标时长（1 ~ 3 分钟） |
| `--quality` | m | l=480p15（快） m=720p30 h=1080p60（慢） |
| `--bgm` | 无 | 背景音乐 mp3 路径，低音量循环混入 |
| `--xhs` | 否 | 生成后自动存入小红书草稿箱 |

## 输出目录

```
~/Downloads/concept_video/
└── 梯度下降_0615_092832/
    ├── 梯度下降.mp4        # 16:9 横版
    ├── 梯度下降_3x4.mp4   # 3:4 小红书
    ├── 梯度下降_9x16.mp4  # 9:16 抖音
    ├── 梯度下降.srt        # 字幕文件
    ├── script.json         # 分镜脚本（含旁白、画面描述）
    ├── 题目.md             # 3 道概念测试题 + 答案
    └── xhs_cover.jpg       # 小红书封面图（上传时生成）
```

## 技术细节

- **LLM**：豆包 `doubao-seed-2.0-pro`，via Anthropic SDK + 自定义 base_url
- **TTS**：Edge-TTS（免费），`zh-CN-YunxiNeural`，`+10%` 语速，词级时间戳对齐字幕
- **动画**：Manim Community Edition 0.20.1，严禁 LaTeX，全中文 `Text(font="PingFang SC")`
- **字幕区隔离**：ffmpeg 将动画缩至上方区域，底部保留纯背景色字幕带，字幕永不压画面
- **小红书上传**：Playwright 自动化，「暂存离开」按钮在闭合 Shadow DOM 里，通过坐标点击实现

## License

MIT
