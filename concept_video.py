# -*- coding: utf-8 -*-
"""
concept_video.py —— AI 概念动画解释视频生成工具

输入一个 AI/数学概念（如"梯度下降"），自动生成 1~3 分钟带旁白和字幕的动画讲解视频。

Pipeline:
    概念 → ① LLM 生成分段脚本+分镜(JSON) → ② Edge-TTS 旁白(免费)
         → ③ LLM 生成 Manim 代码并沙箱渲染(失败自动重试/兜底)
         → ④ 字幕对齐(SRT) → ⑤ ffmpeg 合成 MP4

用法:
    .venv/bin/python concept_video.py "梯度下降"
    .venv/bin/python concept_video.py "神经网络" --audience 中学生 --minutes 2 --quality l

依赖: anthropic, edge-tts, manim (均已装在 .venv)；系统 ffmpeg (brew)。
输出: ~/Downloads/concept_video/<概念>_<时间戳>/<概念>.mp4
"""

import argparse
import asyncio
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ============================================================
# 1. 配置常量
# ============================================================

AI_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding"
AI_API_KEY  = ""
AI_MODEL    = "doubao-seed-2.0-pro"

try:  # 密钥统一放 config_local.py（不进 git）
    from config_local import AI_BASE_URL, AI_API_KEY, AI_MODEL  # noqa: F811
except ImportError:
    pass

OUTPUT_ROOT = Path.home() / "Downloads" / "concept_video"

TTS_VOICE = "zh-CN-YunxiNeural"   # 男声讲解风；女声可换 zh-CN-XiaoxiaoNeural
TTS_RATE  = "+10%"                # 略快的语速，信息密度更高（短视频节奏）
CHARS_PER_SEC = 5.7               # 中文 TTS 实测语速(字/秒，+10% 后)，用于估算脚本字数预算

# 小红书草稿上传（复用 xhs-auto 的登录态，见 xhs_auto.py）
XHS_PROFILE_DIR = Path.home() / "Downloads" / "xhs_auto" / "browser_profile"
XHS_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
XHS_LOGIN_TIMEOUT_S = 180
XHS_UPLOAD_TIMEOUT_S = 300        # 视频上传+转码等待上限

VIDEO_W, VIDEO_H = 1280, 720
SCENE_GAP = 0.6                   # 每个场景旁白结束后的留白秒数
MAX_CODE_RETRY = 3                # Manim 代码渲染失败时的 LLM 修复重试次数

BG_COLOR = "#0e1520"              # 深蓝背景，3Blue1Brown 风
CN_FONT  = "PingFang SC"

# manim 质量档位: 命令行参数 → (flag, 输出子目录)
QUALITY = {
    "l": ("-ql", "480p15"),
    "m": ("-qm", "720p30"),
    "h": ("-qh", "1080p60"),
}

# ============================================================
# 2. Prompt 模板
# ============================================================

SCRIPT_PROMPT = """你是一位极擅长科普的视频编导（风格类似 3Blue1Brown / 李永乐老师）。
请为概念「{concept}」设计一个总时长约 {minutes} 分钟的动画讲解视频脚本。

目标受众：{audience}（零基础也要能听懂）。

硬性要求：
1. 全片拆成 {n_scenes} 个场景，每个场景旁白 70~85 个汉字（约 13~15 秒语音），少于 70 字不合格。
2. 旁白全部用打比方、讲故事的方式，口语化，禁止堆砌公式和术语；必须出现术语时立刻用大白话解释。
3. 结构：钩子开场 → 生活化比喻 → 核心机制拆解 → 一个关键细节/常见误区 → 一句话总结+互动引导。
   - 场景1是「钩子」：第一句话必须在 3 秒内抛出反常识/悬念/利益点（如"你每天都在被这个算法安排"），
     严禁"大家好""今天我们来讲"这类开场白；
   - 最后一个场景结尾必须带一句互动引导：向观众抛一个能在评论区一句话回答的小问题。
4. 全片旁白总字数控制在 {total_chars} 字左右（按 4.2 字/秒折算）。
5. 每个场景给出 visual_plan：用中文具体描述这一幕的画面该画什么、怎么动
   （元素限定为：坐标轴上的函数曲线、移动的小球/点、箭头、几何图形、简笔小人、文字标注、分屏对比等
    2D 扁平元素——之后会用 Manim 渲染，所以描述要具体可画）。

只输出 JSON，不要任何其他文字，格式：
{{
  "title": "视频标题（10字内）",
  "hook": "片头钩子大字（8~14字，悬念式/反常识式，如：手机偷偷上的一门课）",
  "concept": "{concept}",
  "scenes": [
    {{
      "id": 1,
      "name": "场景小标题",
      "narration": "这一段的旁白文字……",
      "visual_plan": "画面描述：先画…然后…最后…"
    }}
  ]
}}"""

MANIM_PROMPT = """你是 Manim Community Edition v0.20 专家。请为下面这个讲解视频场景写一段 Manim 代码。

视频主题：{title}（概念：{concept}）
场景 {sid}/{total}：{name}
旁白（动画要配合它的节奏）：{narration}
画面设计：{visual_plan}

【硬性规则，违反任何一条代码都会运行失败】
1. 只输出一个 python 代码块，内容为：`from manim import *` + 可选 `import numpy as np` / `import math` / `import random`，
   然后定义恰好一个类 `class Scene{sid:02d}(Scene):`，全部逻辑写在 construct 里。不要 if __main__。
2. 严禁 LaTeX：不准用 Tex / MathTex / Title / DecimalNumber / Integer / Variable，
   不准调用 axes.add_coordinates() / get_axis_labels() / get_graph_label() / NumberLine(include_numbers=True)。
   所有文字（含数字、公式）一律用 Text("...", font="{font}")；公式写成纯文本如 Text("y = x²", font="{font}")。
3. construct 第一行：`self.camera.background_color = "{bg}"`。
4. 画函数曲线用 `axes.plot(lambda x: ..., x_range=[a, b])`（不存在 get_graph）。动画用 Create / FadeIn /
   FadeOut / Transform / MoveAlongPath / ValueTracker+always_redraw 等 v0.20 现行 API。
5. 画面是 16:9，坐标范围约 x∈[-7,7], y∈[-4,4]。屏幕底部 y < -2.5 的横条是字幕专用区，
   任何元素（包括图形组的最低端、图形下方的文字标签）都严禁进入，构图重心放在画面中上部；
   中文正文 font_size 26~34，标题 40~52。
6. 动画总时长（所有 run_time 与 self.wait 之和）必须等于 {duration:.1f} 秒，最后用 self.wait 补足；
   每个 run_time 和 self.wait 的值必须 ≥ 0.1（0 或负数会直接报错），不要用变量运算出时长。
7. 不准读写文件、不准联网、不准用图片/SVG/声音素材。图形只准用这些真实存在的类：
   Circle / Square / Rectangle / RoundedRectangle / Triangle / Polygon / Line / DashedLine /
   Arrow / DoubleArrow / CurvedArrow / Dot / Arc / Ellipse / Axes / NumberPlane / VGroup /
   SurroundingRectangle / Brace / Angle。不存在 Checkmark、Cross、StickFigure 之类的类，
   对勾/叉号/小人请用 Line/Circle/Text("✓") 自己拼。
   方位常量只有 UP / DOWN / LEFT / RIGHT / UL / UR / DL / DR / ORIGIN（不存在 UP_LEFT 这种写法）。
8. 配色（深色背景下要亮）：白 WHITE、黄 "#FFD166"、蓝 "#4EA8DE"、绿 "#80ED99"、红 "#FF6B6B"。
9. 严禁任何元素重叠：文字绝不能压在图形上，两个图形绝不能交叠。布局纪律：
   - 标题用 .to_edge(UP, buff=0.5)；主图形放画面中部；
   - 给图形配的文字标签一律用 .next_to(目标, UP/RIGHT/LEFT, buff=0.4) 贴在旁边，不准手填坐标盖上去；
   - 「带框的文字」必须先建 Text，再用 SurroundingRectangle(text, corner_radius=0.15, buff=0.3)
     自动生成贴合的框；严禁先画固定大小的 Rectangle/RoundedRectangle 再把文字塞进去（文字会出框）；
     文字要放进已有图形内部时，必须先 text.scale_to_fit_width(图形.width * 0.8)；
   - 多个并列元素用 VGroup(...).arrange(RIGHT, buff=1) 排开；
   - 讲到新内容时，先 self.play(FadeOut(旧元素们)) 清场，再画新的。
10. 动画要轻量：场上同时存在的对象 ≤ 25 个，always_redraw/updater 累计运行 ≤ 10 秒，
    禁止循环创建上百个对象，否则渲染会超时。

只输出代码块，不要解释。"""

MANIM_FIX_PROMPT = """你刚才写的 Manim 场景代码运行失败了。请修复后重新输出完整代码。

报错信息（截取关键部分）：
{error}

原代码：
```python
{code}
```

记住之前的所有硬性规则（禁 LaTeX、只用 Text(font="{font}")、类名必须是 Scene{sid:02d}、
总时长 {duration:.1f} 秒、不准用 v0.20 已删除的 API）。只输出修复后的完整 python 代码块。"""

# 兜底场景：LLM 代码反复失败时使用的确定性模板（纯文字卡片，保证能渲染）
FALLBACK_SCENE_TPL = '''from manim import *

class Scene{sid:02d}(Scene):
    def construct(self):
        self.camera.background_color = "{bg}"
        title = Text({name!r}, font="{font}", font_size=44, color="#FFD166")
        title.to_edge(UP, buff=0.8)
        body = Text({wrapped!r}, font="{font}", font_size=30, line_spacing=1.2)
        body.scale_to_fit_width(min(body.width, 11))
        body.next_to(title, DOWN, buff=0.9)
        self.play(FadeIn(title), run_time=0.8)
        self.play(FadeIn(body, shift=UP * 0.3), run_time=1.2)
        self.wait({wait:.2f})
'''

QUIZ_PROMPT = """你是一位出题专家。根据以下概念视频的旁白内容，为学习者出 3 道测试题（混合题型），帮助巩固理解。

概念：{concept}
视频标题：{title}
旁白内容：
{narration}

要求：
1. 恰好 3 道题：第1题选择题（4选1）、第2题填空题、第3题问答题。
2. 题目考查核心概念，语言简洁，中文出题。
3. 选择题标注正确答案字母和一句解析；填空题答案 ≤10 字；问答题答案 50~100 字。
4. 严格按以下 Markdown 格式输出，不要添加其他内容：

## 第1题（选择题）
题目正文……？
A. …  B. …  C. …  D. …
**答案：X**
**解析：一句话说明原因。**

## 第2题（填空题）
题目正文，_____ 。
**答案：……**

## 第3题（问答题）
题目正文……？
**答案：……（50~100字）**"""

XHS_COPY_PROMPT = """你是小红书爆款文案专家。我做了一条 AI 概念科普动画视频，请写发布文案。

概念：{concept}
视频钩子：{hook}
视频内容（旁白）：{narration}

要求：
1. title：≤19 个字，带 1 个 emoji，悬念式/利益点式（可基于钩子改写）。
2. body：80~150 字，口语化，前两句必须抓人；结尾带一个引导评论的问题。
3. topics：4~5 个话题词（不带#号），如 AI科普、机器学习、涨知识。

只输出 JSON：{{"title": "...", "body": "...", "topics": ["...", "..."]}}"""

# 片头钩子卡：开场 1.6 秒悬念大字点题（确定性模板，不走 LLM）
INTRO_SCENE_TPL = '''from manim import *

class Scene00(Scene):
    def construct(self):
        self.camera.background_color = "{bg}"
        hook = Text({hook!r}, font="{font}", font_size=72, color="#FFD166", weight=BOLD)
        hook.scale_to_fit_width(min(hook.width, 12))
        sub = Text({sub!r}, font="{font}", font_size=32, color=WHITE)
        sub.next_to(hook, DOWN, buff=0.6)
        VGroup(hook, sub).move_to(ORIGIN)
        self.play(FadeIn(hook, scale=1.25), run_time=0.35)
        self.play(FadeIn(sub, shift=UP * 0.2), run_time=0.3)
        self.wait(0.7)
        self.play(FadeOut(hook), FadeOut(sub), run_time=0.25)
'''

# ============================================================
# 3. 工具函数
# ============================================================

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ai_call(prompt: str, max_tokens: int = 8192) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=AI_API_KEY, base_url=AI_BASE_URL)
    msg = client.messages.create(
        model=AI_MODEL, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def extract_json(text: str) -> dict:
    """从 LLM 回复中抠出 JSON（容忍 ```json 围栏和前后废话）。"""
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"回复中找不到 JSON: {text[:200]}")
    return json.loads(text[start:end + 1])


def extract_code(text: str) -> str:
    """从 LLM 回复中抠出 python 代码块。"""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    code = m.group(1) if m else text
    return code.strip()


def check_code_safety(code: str):
    """简单静态检查：生成的代码只许 import manim/numpy/math/random，禁危险调用。"""
    for line in code.splitlines():
        m = re.match(r"\s*(?:import|from)\s+([\w.]+)", line)
        if m and m.group(1).split(".")[0] not in ("manim", "numpy", "math", "random"):
            raise ValueError(f"不允许的 import: {line.strip()}")
    banned = ["subprocess", "os.system", "os.popen", "shutil", "eval(", "exec(",
              "open(", "__import__", "urllib", "requests", "socket"]
    for word in banned:
        if word in code:
            raise ValueError(f"代码包含被禁止的调用: {word}")


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def run_ffmpeg(args: list, desc: str):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + args
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg {desc} 失败:\n{res.stderr[-2000:]}")


def srt_ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def wrap_cn(text: str, width: int = 18) -> str:
    """中文按宽度硬换行（兜底场景用）。"""
    lines, line, w = [], "", 0
    for ch in text:
        line += ch
        w += 2 if ord(ch) > 0x2E80 else 1
        if w >= width * 2:
            lines.append(line)
            line, w = "", 0
    if line:
        lines.append(line)
    return "\n".join(lines)

# ============================================================
# 4. Step 函数
# ============================================================

def step1_generate_script(concept: str, audience: str, minutes: float) -> dict:
    """① 脚本 + 分镜：一次 LLM 调用产出结构化 JSON。"""
    log(f"① 生成脚本与分镜（概念：{concept}，受众：{audience}，约 {minutes} 分钟）…")
    budget = int(minutes * 60 * CHARS_PER_SEC)
    n_scenes = max(6, min(10, round(budget / 75)))
    prompt = SCRIPT_PROMPT.format(
        concept=concept, audience=audience, minutes=minutes,
        total_chars=budget, n_scenes=n_scenes)
    feedback = ""
    for attempt in range(3):
        try:
            data = extract_json(ai_call(prompt + feedback))
            assert data.get("scenes"), "scenes 为空"
            for i, sc in enumerate(data["scenes"], 1):
                sc["id"] = i
                assert sc.get("narration"), f"场景{i}缺 narration"
                sc.setdefault("name", f"场景{i}")
                sc.setdefault("visual_plan", sc["narration"])
            total = sum(len(s["narration"]) for s in data["scenes"])
            # 旁白太短会导致成片远短于目标时长，不达标就带反馈重试
            if total < budget * 0.7 and attempt < 2:
                feedback = (f"\n\n上一版旁白总共只有 {total} 字，远低于 {budget} 字的要求，"
                            f"视频会太短。请把每个场景的旁白扩充到 60~85 字再输出。")
                log(f"   旁白仅 {total} 字（目标 {budget}），要求 LLM 扩写重试…")
                continue
            log(f"   脚本完成：《{data.get('title', concept)}》，共 {len(data['scenes'])} 个场景，"
                f"旁白 {total} 字")
            return data
        except Exception as e:
            log(f"   脚本 JSON 解析失败（第 {attempt + 1} 次）：{e}")
    raise RuntimeError("脚本生成连续失败，请检查网络/API")


async def _tts_async(text: str, mp3_path: Path) -> list:
    """调 Edge-TTS，返回词级时间戳 [(start_sec, end_sec, word), ...]"""
    import edge_tts
    comm = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE,
                                boundary="WordBoundary")  # 7.x 默认句边界，必须显式开词边界
    boundaries = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                boundaries.append((start, start + chunk["duration"] / 1e7, chunk["text"]))
    return boundaries


def step2_tts(scenes: list, audio_dir: Path):
    """② 每个场景合成旁白 mp3，记录精确时长和词级时间戳。"""
    log("② Edge-TTS 合成旁白…")
    for sc in scenes:
        mp3 = audio_dir / f"scene{sc['id']:02d}.mp3"
        for attempt in range(3):
            try:
                sc["words"] = asyncio.run(_tts_async(sc["narration"], mp3))
                break
            except Exception as e:
                log(f"   场景{sc['id']} TTS 失败（第 {attempt + 1} 次）：{e}")
                time.sleep(2)
        else:
            raise RuntimeError(f"场景{sc['id']} TTS 连续失败")
        sc["audio"] = str(mp3)
        sc["audio_dur"] = ffprobe_duration(mp3)
        sc["video_dur"] = sc["audio_dur"] + SCENE_GAP
        log(f"   场景{sc['id']}《{sc['name']}》旁白 {sc['audio_dur']:.1f}s")


def _render_manim(code: str, sid: int, work_dir: Path, q_flag: str, q_dir: str) -> Path:
    """把代码写盘并调 manim CLI 渲染，返回 mp4 路径；失败抛异常（带 stderr）。"""
    py_file = work_dir / f"scene{sid:02d}.py"
    py_file.write_text(code, encoding="utf-8")
    media_dir = work_dir / "media"
    cmd = [sys.executable, "-m", "manim", "render", q_flag, "--disable_caching",
           "--media_dir", str(media_dir), "-o", f"scene{sid:02d}.mp4",
           str(py_file), f"Scene{sid:02d}"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=420)
    except subprocess.TimeoutExpired:
        raise RuntimeError("渲染超时(420s)：动画太重了。请减少元素数量、缩短 updater/"
                           "ValueTracker 动画时长，避免每帧重建大量对象。")
    pattern = str(media_dir / "videos" / f"scene{sid:02d}" / q_dir / f"scene{sid:02d}.mp4")
    hits = glob.glob(pattern)
    if res.returncode != 0 or not hits:
        raise RuntimeError((res.stderr or res.stdout)[-3000:])
    return Path(hits[0])


def step3_render_scene(sc: dict, meta: dict, total: int, work_dir: Path,
                       quality: str) -> Path:
    """③ 单场景：LLM 生成 Manim 代码 → 渲染；报错回喂 LLM 修复；多次失败用兜底模板。"""
    sid, dur = sc["id"], sc["video_dur"]
    q_flag, q_dir = QUALITY[quality]
    log(f"③ 场景{sid}/{total}《{sc['name']}》生成 Manim 代码并渲染（目标 {dur:.1f}s）…")

    code, last_err = "", ""
    for attempt in range(1, MAX_CODE_RETRY + 1):
        try:
            if attempt == 1:
                reply = ai_call(MANIM_PROMPT.format(
                    title=meta["title"], concept=meta["concept"], sid=sid, total=total,
                    name=sc["name"], narration=sc["narration"],
                    visual_plan=sc["visual_plan"], duration=dur,
                    font=CN_FONT, bg=BG_COLOR))
            else:
                reply = ai_call(MANIM_FIX_PROMPT.format(
                    error=last_err[-1500:], code=code, sid=sid,
                    duration=dur, font=CN_FONT))
            code = extract_code(reply)
            check_code_safety(code)
            mp4 = _render_manim(code, sid, work_dir, q_flag, q_dir)
            log(f"   场景{sid} 渲染成功（第 {attempt} 次尝试）")
            return mp4
        except Exception as e:
            last_err = str(e)
            log(f"   场景{sid} 第 {attempt} 次失败：{last_err.splitlines()[-1][:150]}")

    log(f"   场景{sid} 改用兜底文字卡片")
    fallback = FALLBACK_SCENE_TPL.format(
        sid=sid, bg=BG_COLOR, font=CN_FONT, name=sc["name"],
        wrapped=wrap_cn(sc["narration"]), wait=max(dur - 2.0, 0.5))
    return _render_manim(fallback, sid, work_dir, q_flag, q_dir)


def step4_build_srt(scenes: list, srt_path: Path):
    """④ 用 TTS 词级时间戳拼字幕，一句一条：句号必断、逗号看长度断、15 字硬断。

    edge-tts 的词时间戳里不含标点，所以要把每个词对齐回原旁白文本，
    根据词后面跟着的标点决定断句位置。"""
    log("④ 生成字幕…")
    HARD_PUNCT = "。！？；…"     # 必断
    SOFT_PUNCT = "，、：—"       # 行长 ≥9 字才断
    MAX_LINE = 15                 # 硬性上限（手机端一行能放下）

    entries, offset = [], 0.0
    for sc in scenes:
        if not sc["narration"]:          # 片头等无旁白场景只占时间轴
            offset += sc["video_dur"]
            continue
        narration = sc["narration"]
        words = sc.get("words") or [(0.0, sc["audio_dur"], narration)]
        pos = 0                          # 在原文中的对齐位置
        line, line_start, line_end = "", None, None

        def flush():
            nonlocal line, line_start
            text = line.strip("，、；：—")
            if text:
                entries.append((offset + line_start, offset + line_end, text))
            line, line_start = "", None

        for start, end, word in words:
            if line_start is None:
                line_start = start
            line += word
            line_end = end
            # 对齐原文，看这个词后面跟的是什么标点
            idx = narration.find(word, pos)
            if idx != -1:
                pos = idx + len(word)
            trailing = ""
            while pos < len(narration) and not narration[pos].isalnum():
                trailing += narration[pos]
                pos += 1
            if any(ch in HARD_PUNCT for ch in trailing):
                flush()
            elif any(ch in SOFT_PUNCT for ch in trailing):
                if len(line) >= 9:
                    flush()
                else:  # 不断句时保留逗号，可读性更好
                    line += "".join(c for c in trailing if c in SOFT_PUNCT)
            elif len(line) >= MAX_LINE:
                flush()
        flush()
        offset += sc["video_dur"]

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(entries, 1):
            end += 0.15
            if i < len(entries):  # 不与下一条重叠（重叠会闪双行字幕）
                end = min(end, max(entries[i][0] - 0.01, start + 0.3))
            f.write(f"{i}\n{srt_ts(start)} --> {srt_ts(end)}\n{text}\n\n")
    log(f"   共 {len(entries)} 条字幕 → {srt_path.name}")


def step5_compose(scenes: list, srt_path: Path, build_dir: Path, out_mp4: Path,
                  bgm: str = None):
    """⑤ 每场景对齐音画并统一编码 → concat →（可选混BGM）→ 三种画幅烧字幕。"""
    log("⑤ 合成视频…")
    parts = []
    for sc in scenes:
        part = build_dir / f"part{sc['id']:02d}.mp4"
        dur = sc["video_dur"]
        # 视频末帧冻结补齐到 dur，音频 apad 补静音到 dur，统一编码参数以便无缝 concat
        if sc["audio"]:
            audio_in = ["-i", sc["audio"]]
        else:  # 无旁白场景（片头）配静音轨
            audio_in = ["-f", "lavfi", "-t", f"{dur:.3f}",
                        "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        run_ffmpeg([
            "-i", sc["scene_mp4"], *audio_in,
            "-vf", (f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
                    f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color={BG_COLOR},"
                    f"tpad=stop_mode=clone:stop_duration=10,fps=30"),
            "-af", "apad", "-t", f"{dur:.3f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            str(part)], f"场景{sc['id']}音画对齐")
        parts.append(part)
        log(f"   场景{sc['id']} 音画对齐完成（{dur:.1f}s）")

    concat_list = build_dir / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    merged = build_dir / "merged.mp4"
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-c", "copy", str(merged)], "拼接")

    if bgm and Path(bgm).exists():  # 低音量循环混入背景音乐
        merged_bgm = build_dir / "merged_bgm.mp4"
        run_ffmpeg(["-i", str(merged), "-stream_loop", "-1", "-i", bgm,
                    "-filter_complex",
                    "[1:a]volume=0.12[b];[0:a][b]amix=inputs=2:duration=first:dropout_transition=0[a]",
                    "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                    "-c:a", "aac", "-ar", "44100", str(merged_bgm)], "混入BGM")
        merged = merged_bgm
        log("   已混入背景音乐")

    srt_escaped = str(srt_path.resolve()).replace("'", r"\'").replace(":", r"\:")
    base_style = ("PrimaryColour=&HFFFFFF,OutlineColour=&H80000000,"
                  "BorderStyle=1,Outline=1,Shadow=0")

    # 版本一 16:9（1280x720）：画面缩到上方 1088x612，底部 108px 字幕专属带，永不压画面
    style = f"FontName={CN_FONT},FontSize=13,MarginV=10,{base_style}"
    run_ffmpeg(["-i", str(merged),
                "-vf", (f"scale=1088:612,pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:0:color={BG_COLOR},"
                        f"subtitles='{srt_escaped}':force_style='{style}'"),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "copy", str(out_mp4)], "烧录字幕(16:9)")
    log(f"   16:9 横版 → {out_mp4}")

    # 版本二 3:4 竖版（1080x1440，小红书）：画面置于上部，字幕在画面正下方的留白区
    out_34 = out_mp4.with_name(out_mp4.stem + "_3x4.mp4")
    style34 = f"FontName={CN_FONT},FontSize=11,MarginV=55,{base_style}"
    run_ffmpeg(["-i", str(merged),
                "-vf", (f"scale=1080:-2,pad=1080:1440:0:300:color={BG_COLOR},"
                        f"subtitles='{srt_escaped}':force_style='{style34}'"),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "copy", str(out_34)], "烧录字幕(3:4)")
    log(f"   3:4 竖版 → {out_34}")

    # 版本三 9:16 竖版（1080x1920，抖音）：画面中上部，手机端大字幕
    out_916 = out_mp4.with_name(out_mp4.stem + "_9x16.mp4")
    style916 = f"FontName={CN_FONT},FontSize=10,MarginV=42,{base_style}"
    run_ffmpeg(["-i", str(merged),
                "-vf", (f"scale=1080:-2,pad=1080:1920:0:420:color={BG_COLOR},"
                        f"subtitles='{srt_escaped}':force_style='{style916}'"),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "copy", str(out_916)], "烧录字幕(9:16)")
    log(f"   9:16 竖版 → {out_916}")

def _xhs_first_visible(page, selectors, timeout_each=3000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_each)
            return loc
        except Exception:
            continue
    return None


def _xhs_launch(p):
    """复用 xhs-auto 的持久化登录目录启动 Chromium。"""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock = XHS_PROFILE_DIR / name
        if lock.exists() or lock.is_symlink():
            try:
                lock.unlink()
            except OSError:
                pass
    return p.chromium.launch_persistent_context(
        str(XHS_PROFILE_DIR), headless=False,
        viewport={"width": 1440, "height": 900},
        args=["--disable-blink-features=AutomationControlled"])


def _xhs_wait_login(page):
    deadline = time.time() + XHS_LOGIN_TIMEOUT_S
    warned = False
    while time.time() < deadline:
        if "login" in page.url or page.locator("text=扫码登录").count() > 0:
            if not warned:
                log(">>> 请在弹出的浏览器里扫码登录小红书（登录态会记住）")
                warned = True
            time.sleep(2)
            continue
        if _xhs_first_visible(page, ['div:has-text("上传视频")',
                                     'span:has-text("上传视频")'], 4000):
            return
        time.sleep(2)
    raise RuntimeError("等待小红书登录超时")


def ai_xhs_copy(meta: dict) -> dict:
    """LLM 生成小红书笔记文案（标题/正文/话题），失败时用兜底文案。"""
    narration = "".join(s["narration"] for s in meta["scenes"] if s.get("narration"))
    try:
        data = extract_json(ai_call(XHS_COPY_PROMPT.format(
            concept=meta["concept"], hook=meta.get("hook", ""),
            narration=narration[:600])))
        assert data.get("title") and data.get("body")
        data["title"] = data["title"][:20]
        data.setdefault("topics", [])
        return data
    except Exception as e:
        log(f"   小红书文案生成失败，用兜底文案：{e}")
        return {"title": (meta.get("hook") or meta["concept"])[:20],
                "body": f"60秒看懂「{meta['concept']}」，零基础也能明白。"
                        f"你身边有类似的例子吗？评论区聊聊👇",
                "topics": ["AI科普", "涨知识", meta["concept"][:10]]}


def make_xhs_cover(meta: dict, out_path: Path) -> Path:
    """生成 3:4 小红书封面图：深底 + 钩子大字 + 概念名，PIL 直接画。"""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1080, 1440
    img = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    def load_font(size, bold=False):
        for path, idx in [("/System/Library/Fonts/PingFang.ttc", 1 if bold else 2),
                          ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0)]:
            try:
                return ImageFont.truetype(path, size, index=idx)
            except Exception:
                continue
        return ImageFont.load_default(size)

    hook = meta.get("hook") or meta["concept"]
    # 钩子按 5~6 字一行拆行，行数多则缩小字号
    per_line = 5 if len(hook) <= 10 else 6
    lines = [hook[i:i + per_line] for i in range(0, len(hook), per_line)]
    font_size = min(190, int(W * 0.92 / per_line))
    f_hook = load_font(font_size, bold=True)
    line_h = int(font_size * 1.25)
    block_h = line_h * len(lines)
    y = (H - block_h) // 2 - 120
    for ln in lines:
        w = draw.textlength(ln, font=f_hook)
        draw.text(((W - w) / 2, y), ln, font=f_hook, fill="#FFD166")
        y += line_h

    f_sub = load_font(58)
    sub = f"60秒看懂「{meta['concept']}」"
    w = draw.textlength(sub, font=f_sub)
    draw.text(((W - w) / 2, y + 50), sub, font=f_sub, fill="#FFFFFF")
    draw.rounded_rectangle([(W - 360) / 2, y + 170, (W + 360) / 2, y + 182],
                           radius=6, fill="#4EA8DE")
    img.save(out_path, "JPEG", quality=92)
    return out_path


def _xhs_js_click_text(page, text: str, nth_from_end: int = 0) -> bool:
    """JS 点击文本(去空白后)等于 text 的最深元素。

    按 textContent 整体匹配而非叶子节点，兼容「暂存离开」这类被拆成多个
    span 的按钮；文档序最后一个匹配即最深层。nth_from_end=1 取倒数第二个。"""
    return page.evaluate("""(args) => {
        const want = args.text.replace(/\\s+/g, '');
        const hits = [];
        const walk = (root) => {
            for (const el of root.querySelectorAll('*')) {
                if (el.shadowRoot) walk(el.shadowRoot);   // 穿透 open shadow DOM
                const txt = (el.textContent || '').replace(/[\\s\\u200b\\u200c\\ufeff]+/g, '');
                if (txt === want) hits.push(el);
            }
        };
        walk(document);
        const i = hits.length - 1 - args.nth;
        if (i >= 0) { hits[i].click(); return true; }
        return false;
    }""", {"text": text, "nth": nth_from_end})


def _xhs_upload_video(page, video_path: Path, shot_dir: Path):
    """上传视频并等转码完成；「上传失败」自动重传，最多 3 次。"""
    for attempt in range(1, 4):
        if attempt > 1:
            page.goto(XHS_PUBLISH_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        # 默认就在"上传视频"标签；视频上传框 = accept 含视频格式的 input[type=file]
        file_input = None
        deadline = time.time() + 30
        while time.time() < deadline and file_input is None:
            for el in page.locator('input[type="file"]').all():
                acc = (el.get_attribute("accept") or "").lower()
                if any(k in acc for k in ("video", "mp4", "mov", "quicktime")):
                    file_input = el
                    break
            if file_input is None:
                page.wait_for_timeout(1500)
        if file_input is None:
            page.screenshot(path=str(shot_dir / "xhs_无上传入口.png"))
            raise RuntimeError("没找到视频上传入口（已截图）")
        file_input.set_input_files(str(video_path))
        log(f"   视频上传中（第 {attempt} 次）：{video_path.name}")

        # 轮询：明确区分「上传失败」和真正成功（成功标志=可见"上传成功"且无失败提示）
        failed = False
        deadline = time.time() + XHS_UPLOAD_TIMEOUT_S
        while time.time() < deadline:
            body_text = page.evaluate("() => document.body.innerText")
            if "上传失败" in body_text:
                failed = True
                break
            if "上传成功" in body_text or "重新上传" in body_text:
                log("   视频上传完成")
                return
            page.wait_for_timeout(2000)
        if failed:
            log(f"   第 {attempt} 次上传失败（网络异常），重试…")
            continue
        page.screenshot(path=str(shot_dir / "xhs_上传超时.png"))
        raise RuntimeError("视频上传/转码超时（已截图）")
    page.screenshot(path=str(shot_dir / "xhs_上传失败.png"))
    raise RuntimeError("视频连续 3 次上传失败（已截图），请检查网络后重跑")


def _xhs_set_cover(page, cover_path: Path, shot_dir: Path):
    """上传自定义封面（失败不致命：默认封面也能存草稿）。"""
    def find_image_input():
        for el in page.locator('input[type="file"]').all():
            acc = (el.get_attribute("accept") or "").lower()
            if any(k in acc for k in ("image", "png", "jpg", "jpeg")):
                return el
        return None

    try:
        # 封面入口是 div.cover 里带第一帧背景图的 .default 盒子（来自 DOM dump）
        entry = _xhs_first_visible(page, [
            "div.cover .default", "div.cover", ".cover-plugin-preview .cover"], 5000)
        if entry is None:
            log("   !! 没找到「设置封面」入口，用默认封面")
            return
        entry.click()
        page.wait_for_timeout(2500)
        cover_input = None
        for _ in range(4):
            cover_input = find_image_input()
            if cover_input:
                break
            _xhs_js_click_text(page, "上传封面")       # 弹窗里可能要先切标签
            page.wait_for_timeout(1500)
        if cover_input is None:
            page.screenshot(path=str(shot_dir / "xhs_封面弹窗.png"))
            (shot_dir / "xhs_封面_dom.html").write_text(page.content(), encoding="utf-8")
            log("   !! 封面弹窗里没找到上传入口（已截图并 dump DOM），用默认封面")
            page.keyboard.press("Escape")
            return
        cover_input.set_input_files(str(cover_path))
        page.wait_for_timeout(4000)          # 等封面上传/裁剪预览
        for name in ("确定", "确认", "完成", "保存"):
            if _xhs_js_click_text(page, name):
                break
        page.wait_for_timeout(2000)
        log("   已上传自定义封面")
    except Exception as e:
        log(f"   !! 封面上传出错（用默认封面继续）：{e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass


def step6_xhs_draft(page, video_path: Path, copy: dict, cover_path: Path,
                    shot_dir: Path):
    """⑥ 上传一条视频到小红书创作者中心：视频+封面+标题/正文/话题 → 存草稿。"""
    page.goto(XHS_PUBLISH_URL, wait_until="domcontentloaded")
    _xhs_upload_video(page, video_path, shot_dir)
    if cover_path and cover_path.exists():
        _xhs_set_cover(page, cover_path, shot_dir)

    title_box = _xhs_first_visible(page, [
        'input[placeholder*="标题"]', 'div.d-input input', 'input.d-text'])
    if title_box:
        title_box.click()
        title_box.fill(copy["title"])
        log(f"   已填标题：{copy['title']}")
    editor = _xhs_first_visible(page, [
        'div.ql-editor', '#post-textarea', 'div[contenteditable="true"]'])
    if editor:
        editor.click()
        page.keyboard.insert_text(copy["body"])
        page.wait_for_timeout(600)
        for t in copy.get("topics", [])[:5]:   # 话题：输#词后点联想下拉
            for combo in ("Meta+ArrowDown", "Control+End"):
                try:
                    page.keyboard.press(combo)
                except Exception:
                    pass
            page.keyboard.type("#" + t, delay=80)
            page.wait_for_timeout(2500)
            sug = _xhs_first_visible(page, [
                "#creator-editor-topic-container .item", ".publish-topic-item",
                'div[class*="topic"] .item', 'ul[class*="topic"] li'], 2500)
            if sug:
                sug.click()
                page.wait_for_timeout(400)
            else:
                page.keyboard.type(" ")
        log("   已填正文和话题")

    # 等可能的「加载中」弹层消失，再存草稿
    for _ in range(15):
        if "加载中" not in page.evaluate("() => document.body.innerText"):
            break
        page.wait_for_timeout(1000)
    page.screenshot(path=str(shot_dir / "xhs_存草稿前.png"))

    # 「暂存离开」在 <xhs-publish-btn> 的闭合 shadow DOM 里（DOM dump 确认），
    # 任何选择器都进不去，只能对宿主元素做坐标点击：save 按钮在左半、发布在右半，
    # 点 25% 宽度处稳落在「暂存离开」上
    clicked = False
    try:
        host = page.locator("xhs-publish-btn").first
        host.wait_for(state="visible", timeout=8000)
        box = host.bounding_box()
        if box and box["width"] > 50:
            page.mouse.click(box["x"] + box["width"] * 0.25,
                             box["y"] + box["height"] / 2)
            clicked = True
    except Exception:
        pass
    if not clicked:                                    # 页面改版兜底：按文本点
        for name in ("暂存离开", "存草稿", "保存草稿"):
            if _xhs_js_click_text(page, name):
                clicked = True
                break
    if not clicked:
        page.screenshot(path=str(shot_dir / "xhs_无存草稿按钮.png"))
        (shot_dir / "xhs_dom.html").write_text(page.content(), encoding="utf-8")
        raise RuntimeError("没找到存草稿按钮（已截图并 dump DOM）")
    page.wait_for_timeout(3500)
    page.screenshot(path=str(shot_dir / "xhs_存草稿后.png"))
    log("   ✅ 已存入小红书草稿箱")


def upload_drafts_to_xhs(video_dirs: list):
    """批量把生成结果目录里的 3:4 视频依次存入小红书草稿箱（一个浏览器会话）。"""
    from playwright.sync_api import sync_playwright
    log(f"⑥ 上传 {len(video_dirs)} 条视频到小红书草稿箱…")
    XHS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as p:
        ctx = _xhs_launch(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(XHS_PUBLISH_URL, wait_until="domcontentloaded")
        _xhs_wait_login(page)
        for d in video_dirs:
            d = Path(d)
            try:
                meta = json.loads((d / "script.json").read_text(encoding="utf-8"))
                videos = sorted(d.glob("*_3x4.mp4"))
                if not videos:
                    raise RuntimeError("目录里没有 3:4 视频")
                copy = ai_xhs_copy(meta)
                cover = make_xhs_cover(meta, d / "xhs_cover.jpg")
                step6_xhs_draft(page, videos[-1], copy, cover, d)
                results.append((d.name, "✅ 草稿已保存"))
            except Exception as e:
                log(f"   ❌ {d.name} 上传失败：{e}")
                results.append((d.name, f"❌ {e}"))
        ctx.close()
    return results


def step_generate_quiz(meta: dict, out_dir: Path) -> Path | None:
    """生成 3 道测试题（选择/填空/问答）并保存为 题目.md。"""
    log("⑦ 生成概念测试题…")
    narration = "\n".join(
        f"【{sc['name']}】{sc['narration']}"
        for sc in meta.get("scenes", [])
        if sc.get("narration")
    )
    try:
        md = ai_call(QUIZ_PROMPT.format(
            concept=meta["concept"],
            title=meta.get("title", meta["concept"]),
            narration=narration[:1500]))
        md = re.sub(r"^```(?:markdown)?\s*\n?", "", md.strip())
        md = re.sub(r"\n?```\s*$", "", md.strip())
        quiz_path = out_dir / "题目.md"
        quiz_path.write_text(
            f"# 「{meta['concept']}」概念测试题\n\n{md.strip()}\n",
            encoding="utf-8")
        log(f"   测试题已保存 → {quiz_path.name}")
        return quiz_path
    except Exception as e:
        log(f"   !! 测试题生成失败：{e}")
        return None


# ============================================================
# 5. 主流程
# ============================================================

def run(concept: str, audience: str = "成人零基础", minutes: float = 2.0,
        quality: str = "m", bgm: str = None) -> Path:
    safe_name = re.sub(r"[^\w一-鿿-]+", "_", concept)[:30]
    out_dir = OUTPUT_ROOT / f"{safe_name}_{time.strftime('%m%d_%H%M%S')}"
    audio_dir, work_dir, build_dir = out_dir / "audio", out_dir / "scenes", out_dir / "build"
    for d in (audio_dir, work_dir, build_dir):
        d.mkdir(parents=True, exist_ok=True)
    log(f"输出目录：{out_dir}")

    meta = step1_generate_script(concept, audience, minutes)
    scenes = meta["scenes"]
    (out_dir / "script.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    step2_tts(scenes, audio_dir)
    (out_dir / "script.json").write_text(  # 带上时长信息再存一遍，方便排查
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    total = len(scenes)
    for sc in scenes:
        sc["scene_mp4"] = str(step3_render_scene(sc, meta, total, work_dir, quality))

    # 片头钩子卡（1.6s，悬念大字，无旁白无字幕），插到最前面
    log("③ 渲染片头钩子卡…")
    q_flag, q_dir = QUALITY[quality]
    intro_code = INTRO_SCENE_TPL.format(
        bg=BG_COLOR, font=CN_FONT,
        hook=meta.get("hook") or concept,
        sub=f"60秒看懂「{concept}」")
    intro_mp4 = _render_manim(intro_code, 0, work_dir, q_flag, q_dir)
    scenes.insert(0, {"id": 0, "name": "片头", "narration": "", "words": [],
                      "audio": None, "audio_dur": 0.0, "video_dur": 1.6,
                      "scene_mp4": str(intro_mp4)})

    srt_path = out_dir / f"{safe_name}.srt"
    step4_build_srt(scenes, srt_path)

    out_mp4 = out_dir / f"{safe_name}.mp4"
    step5_compose(scenes, srt_path, build_dir, out_mp4, bgm=bgm)

    step_generate_quiz(meta, out_dir)

    total_dur = sum(s["video_dur"] for s in scenes)
    log(f"✅ 完成！《{meta.get('title', concept)}》 时长 {int(total_dur // 60)}:{int(total_dur % 60):02d}")
    return out_mp4


def run_batch(concepts: list, audience: str = "成人零基础", minutes: float = 2.0,
              quality: str = "m", bgm: str = None, xhs: bool = False):
    """批量生成多个概念的视频；可选逐条存入小红书草稿箱。单条失败不影响其余。"""
    results, ok_dirs = [], []
    for i, concept in enumerate(concepts, 1):
        log(f"━━━━━ 批量 {i}/{len(concepts)}：{concept} ━━━━━")
        try:
            out = run(concept, audience, minutes, quality, bgm)
            results.append((concept, f"✅ {out}"))
            ok_dirs.append(out.parent)
        except Exception as e:
            log(f"❌ 「{concept}」生成失败：{e}")
            results.append((concept, f"❌ {e}"))

    if xhs and ok_dirs:
        for name, status in upload_drafts_to_xhs(ok_dirs):
            results.append((name, status))

    log("━━━━━ 批量汇总 ━━━━━")
    for name, status in results:
        log(f"  {name}: {status}")
    return results


def ask(prompt: str, default: str, choices: dict = None) -> str:
    """交互式问一项：回车用默认值；choices 为 {序号: 值} 时按序号选。"""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return default
        if choices is None:
            return raw
        if raw in choices:
            return choices[raw]
        print(f"   请输入 {'/'.join(choices)} 或直接回车")


def interactive():
    print("=" * 46)
    print("  AI 概念动画讲解视频生成器")
    print("  （直接回车 = 使用默认值）")
    print("=" * 46)
    while True:
        concepts = []
        while not concepts:
            raw = input("\n要讲解的概念（多个用逗号/空格分隔，如：梯度下降，过拟合）：").strip()
            concepts = [c for c in re.split(r"[，,、;；\s]+", raw) if c]
        audience = ask("目标受众 1=小学生 2=中学生 3=成人零基础 [默认3]：", "成人零基础",
                       {"1": "小学生", "2": "中学生", "3": "成人零基础"})
        minutes = ask("目标时长（分钟 1~3）[默认2]：", "2")
        try:
            minutes = min(max(float(minutes), 1.0), 3.0)
        except ValueError:
            minutes = 2.0
        quality = ask("渲染质量 l=480p15(快) m=720p30 h=1080p60(慢) [默认m]：", "m",
                      {q: q for q in QUALITY})
        bgm = input("背景音乐 mp3 路径（可选，回车跳过）：").strip().strip("'\"") or None
        xhs = input("生成后存入小红书草稿箱？(y/N)：").strip().lower() in ("y", "yes")
        print(f"\n→ {len(concepts)} 个概念：{'、'.join(concepts)} | {audience} | "
              f"{minutes:g} 分钟 | {QUALITY[quality][1]}"
              f"{' | 传小红书草稿' if xhs else ''}，开始生成…\n")
        try:
            run_batch(concepts, audience, minutes, quality, bgm, xhs)
        except Exception as e:
            print(f"\n❌ 批量执行出错：{e}")
        if input("\n再来一批？(y/N)：").strip().lower() not in ("y", "yes"):
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI 概念动画解释视频生成工具（不带参数=交互模式）")
    parser.add_argument("concepts", nargs="*",
                        help="要讲解的概念，可多个（批量），如：梯度下降 过拟合；省略则进入交互模式")
    parser.add_argument("--audience", default="成人零基础",
                        choices=["小学生", "中学生", "成人零基础"], help="目标受众")
    parser.add_argument("--minutes", type=float, default=2.0, help="目标时长（分钟，1~3）")
    parser.add_argument("--quality", default="m", choices=list(QUALITY),
                        help="渲染质量 l=480p15 m=720p30 h=1080p60")
    parser.add_argument("--bgm", default=None, help="背景音乐文件路径（可选，低音量循环混入）")
    parser.add_argument("--xhs", action="store_true", help="生成后逐条存入小红书草稿箱")
    parser.add_argument("--xhs-only", nargs="*", metavar="输出目录",
                        help="不生成视频，直接把已有输出目录上传小红书草稿箱")
    parser.add_argument("--quiz-only", nargs="*", metavar="输出目录",
                        help="不生成视频，直接给已有输出目录生成/补写 题目.md")
    args = parser.parse_args()
    if args.quiz_only is not None:
        dirs = args.quiz_only or sorted(
            str(p) for p in OUTPUT_ROOT.iterdir() if (p / "script.json").exists())
        for d in dirs:
            d = Path(d)
            try:
                meta = json.loads((d / "script.json").read_text(encoding="utf-8"))
                step_generate_quiz(meta, d)
            except Exception as e:
                log(f"❌ {d.name} 出题失败：{e}")
    elif args.xhs_only is not None:
        dirs = args.xhs_only or sorted(
            str(p) for p in OUTPUT_ROOT.iterdir() if (p / "script.json").exists())
        upload_drafts_to_xhs(dirs)
    elif args.concepts:
        run_batch(args.concepts, args.audience, min(max(args.minutes, 1.0), 3.0),
                  args.quality, args.bgm, args.xhs)
    else:
        try:
            interactive()
        except (KeyboardInterrupt, EOFError):
            print("\n已退出")
