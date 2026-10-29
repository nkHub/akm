"""桌宠的对话与碎碎念：提示词、记忆读写、LLM 调用。

与 ``physics.py`` 一样尽量做成可单测的纯逻辑：真正的 HTTP 调用集中在一个
函数里，其余（记忆裁剪、提示词拼装、回复清洗）都是纯函数。

设计要点：
- **记忆持久化**：对话历史与碎碎念历史落在 ``~/.akm/dafeiyu/memory.json``，
  与桌宠窗口位置（layout.json）同目录。进程重启后能接上上次的聊天。
- **记忆有上限**：只保留最近 N 轮，避免文件无限增长、也避免把超长历史塞给模型。
- **失败不伪造**：模型不可用时明确返回原因，气泡显示「对话不可用」，
  不编造一句假回复。
- **不改动 AKM 转发链路**：这里是桌宠主动发起的普通 HTTP 请求，走 AKM 自己的
  ``/v1/chat/completions``，因此计费、审计、限流全部照常生效。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# 记忆文件：与 layout.json 同目录（由插件注入 AKM_DAFEIYU_DATA_DIR）
DATA_DIR = Path(os.environ.get("AKM_DAFEIYU_DATA_DIR", str(Path.home() / ".akm" / "dafeiyu")))
MEMORY_FILE = DATA_DIR / "memory.json"

# 保留最近多少轮对话 / 多少条碎碎念
MAX_CHAT_TURNS = 20
MAX_WHISPERS = 30

# 碎碎念默认提示词：让模型说一句短话，符合桌宠口吻
DEFAULT_WHISPER_PROMPT = (
    "你是一只住在用户电脑桌面上的大肥鱼桌宠。"
    "请说一句很短的中文碎碎念（不超过 20 个字），"
    "语气轻松可爱，可以是关于工作、天气、吃饭、摸鱼之类的小感想。"
    "只输出这句话本身，不要引号、不要解释。"
)

# 碎碎念可用的动画动作（与素材 manifest 的 clip 名一致）。
# 模型只能从这些里挑，避免它编出不存在的动作导致桌宠静默不播。
WHISPER_CLIPS = (
    "head_pat", "poke", "tail", "eat_token",
    "idle_stretch", "idle_yawn",
)

# 碎碎念要求模型**同时**给「一句话 + 一个动作」。
# 用 JSON 输出比「先说动作再说内容」的纯文本更稳：模型很难只输出动作名，
# 也避免把动作名混进气泡文本里（用户会看到「tail 今天天气不错」）。
WHISPER_JSON_PROMPT = (
    "你是一只住在用户电脑桌面上的大肥鱼桌宠。"
    "请输出一个 JSON 对象，包含两个字段：\n"
    '  "text": 一句很短的中文碎碎念（不超过 20 个字），语气轻松可爱，'
    "可以是关于工作、天气、吃饭、摸鱼之类的小感想；\n"
    '  "clip": 配合这句话最适合的动画动作，只能从这些里选：'
    + "、".join(WHISPER_CLIPS) + "。\n"
    "只输出 JSON，不要 markdown 代码块、不要额外解释。"
)

# 对话系统提示词
DEFAULT_CHAT_PROMPT = (
    "你是用户桌面上的一只大肥鱼桌宠，名字叫大肥鱼。"
    "用简短、轻松、可爱的中文回答，一般不超过两句话。"
    "如果用户问你是什么，就说自己是他桌面上的桌宠。"
)


@dataclass
class ChatTurn:
    """一轮对话（用户一句 + 桌宠一句）。"""

    user: str
    assistant: str
    at: float = field(default_factory=time.time)


@dataclass
class Memory:
    """桌宠的持久化记忆。"""

    chat: list[ChatTurn] = field(default_factory=list)
    whispers: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "chat": [
                {"user": t.user, "assistant": t.assistant, "at": t.at}
                for t in self.chat
            ],
            "whispers": list(self.whispers),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Memory":
        """从磁盘结构还原；字段缺失或类型不符时逐项兜底，不抛异常。"""
        mem = cls()
        if not isinstance(data, dict):
            return mem
        for item in data.get("chat") or []:
            if not isinstance(item, dict):
                continue
            user = str(item.get("user") or "")
            assistant = str(item.get("assistant") or "")
            if not user or not assistant:
                continue
            try:
                at = float(item.get("at") or 0)
            except (TypeError, ValueError):
                at = 0.0
            mem.chat.append(ChatTurn(user=user, assistant=assistant, at=at))
        for item in data.get("whispers") or []:
            if isinstance(item, dict) and item.get("text"):
                mem.whispers.append(item)
        return mem


def load_memory(path: Path = MEMORY_FILE) -> Memory:
    """读记忆文件。文件不存在/损坏都返回空记忆，不阻塞桌宠启动。"""
    try:
        raw = path.read_text(encoding="utf-8")
        return Memory.from_dict(json.loads(raw))
    except FileNotFoundError:
        return Memory()
    except Exception:
        # 损坏的记忆不该让桌宠起不来：当作空记忆继续
        return Memory()


def save_memory(memory: Memory, path: Path = MEMORY_FILE) -> bool:
    """写记忆文件（自动裁剪到上限）。返回是否写成功。"""
    trim_memory(memory)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(memory.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


def trim_memory(memory: Memory) -> None:
    """把记忆裁到上限：只留最近若干轮 / 若干条。"""
    if len(memory.chat) > MAX_CHAT_TURNS:
        del memory.chat[:-MAX_CHAT_TURNS]
    if len(memory.whispers) > MAX_WHISPERS:
        del memory.whispers[:-MAX_WHISPERS]


def build_chat_messages(memory: Memory, user_text: str,
                        system_prompt: str = DEFAULT_CHAT_PROMPT) -> list[dict]:
    """把历史 + 本轮问题拼成 OpenAI Chat 格式的 messages。

    历史按「用户 / 助手」交替展开，只取最近 ``MAX_CHAT_TURNS`` 轮，
    避免上下文过长把模型的注意力冲淡。
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for turn in memory.chat[-MAX_CHAT_TURNS:]:
        messages.append({"role": "user", "content": turn.user})
        messages.append({"role": "assistant", "content": turn.assistant})
    messages.append({"role": "user", "content": user_text})
    return messages


def clean_reply(text: str) -> str:
    """清洗模型回复：去首尾空白、去包裹引号、压成单行。

    桌宠气泡只能显示一行，所以把换行折成空格，并去掉模型爱加的引号。
    """
    if not text:
        return ""
    out = " ".join(str(text).split())
    # 去掉成对的中英文引号（只去一层，避免把正常标点删掉）
    for left, right in (("“", "”"), ("「", "」"), ('"', '"'), ("'", "'")):
        if out.startswith(left) and out.endswith(right) and len(out) > 2:
            out = out[1:-1].strip()
    return out


def parse_chat_response(body: dict) -> tuple[str, str]:
    """从 /v1/chat/completions 响应里取回复文本。

    返回 ``(text, error)``：成功时 error 为空；失败时 text 为空、error 说明原因。
    """
    if not isinstance(body, dict):
        return "", "响应格式无法识别"
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        # 有些上游把错误放在 error 字段里
        err = body.get("error")
        if isinstance(err, dict):
            return "", str(err.get("message") or err.get("type") or "上游返回错误")
        if isinstance(err, str) and err:
            return "", err
        return "", "上游没有返回内容"
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, list):
        # 多模态/分段 content：拼出所有 text 片段
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        content = "".join(parts)
    text = clean_reply(str(content or ""))
    if not text:
        return "", "模型返回了空回复"
    return text, ""


@dataclass
class LlmResult:
    """一次 LLM 调用的结果。"""

    ok: bool
    text: str = ""
    error: str = ""
    status_code: int = 0


def call_chat(
    base_url: str,
    model: str,
    messages: list[dict],
    api_key: str = "",
    timeout: float = 30.0,
) -> LlmResult:
    """调 AKM 自己的 ``/v1/chat/completions`` 取一次回复。

    刻意用同步 urllib 而不是 aiohttp/httpx：桌宠跑在 AppKit 主线程的定时器里，
    这里由**后台线程**调用（见 pet_app 的 _ChatWorker），同步实现最简单，
    也不会和 AppKit 的事件循环抢线程。
    """
    if not base_url or not model:
        return LlmResult(ok=False, error="未配置模型")
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = json.dumps(
        {"model": model, "messages": messages, "stream": False, "max_tokens": 200},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # 显式把 User-Agent 置为空串，阻止 urllib 自动补 `Python-urllib/<版本>`。
    #
    # 为什么必须显式置空：urllib 在请求头里找不到 User-Agent 时，会自己填上
    # `Python-urllib/3.12`。而 AKM 开启 `use_native_user_agent` 后会把客户端
    # 自带的 UA 原样透传给上游（见 akm/agent.py 的 `_resolve_user_agent`）。
    # 这个默认 UA 是 Cloudflare 等 WAF 的典型爬虫特征，会被
    # Error 1010（banned your access based on your browser's signature）直接拒绝。
    #
    # 置空后服务端收到的 UA 是空串，AKM 侧 `original_user_agent` 为空，
    # 按既有逻辑回退成 `akm/<version>`——与「客户端没带 UA」的语义完全一致。
    # 这样做不用在插件里硬编码版本号，避免 AKM 升级后版本号漂移。
    headers["User-Agent"] = ""
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也带 body，尽量把上游错误文本取出来给用户看
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        return LlmResult(ok=False, error=f"HTTP {exc.code}", status_code=exc.code)
    except Exception as exc:
        return LlmResult(ok=False, error=str(exc))

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return LlmResult(ok=False, error="响应不是合法 JSON", status_code=status)
    text, error = parse_chat_response(body)
    if error:
        return LlmResult(ok=False, error=error, status_code=status)
    return LlmResult(ok=True, text=text, status_code=status)


def build_whisper_messages(recent_whispers: list[dict],
                           prompt: str = DEFAULT_WHISPER_PROMPT) -> list[dict]:
    """碎碎念的 messages：把最近说过的几句一起给它，避免重复。"""
    system = prompt
    if recent_whispers:
        said = "、".join(str(w.get("text") or "") for w in recent_whispers[-5:] if w.get("text"))
        if said:
            system += f"\n你最近已经说过：{said}。请换一句不同的。"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "说一句碎碎念"},
    ]


def parse_whisper_reply(text: str) -> tuple[str, str]:
    """解析碎碎念回复，返回 ``(气泡文本, 动画 clip)``。

    **为什么要容错**：提示词要求模型输出 JSON，但实际有几种偏差：
      ① 包了 markdown 代码块 ```json ... ```；
      ② 输出纯文本（老提示词 / 模型不听话）；
      ③ clip 不在允许列表里，或是编造的动作名。

    处理策略：
      - 能从回复里抠出 JSON 且含 ``text`` → 用它的 text/clip；
      - 抠不出或 text 为空 → **整段回复当气泡文本**，clip 返回空（由调用方兜底）；
      - clip 不在 ``WHISPER_CLIPS`` 里 → clip 返回空，文本照常保留。

    返回的 clip 为空表示「让调用方决定播什么」，绝不编造动作名——
    不存在的 clip 会让桌宠静默不播（cache.has() 返回 False）。
    """
    raw = str(text or "").strip()
    if not raw:
        return "", ""

    # 去掉 markdown 代码块围栏（```json / ``` 都剥掉）
    body = raw
    if body.startswith("```"):
        # 去掉首行围栏与末尾围栏
        body = body.split("\n", 1)[-1] if "\n" in body else body[3:]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        body = body.strip()

    # 尝试从任意位置抠出第一个 JSON 对象（模型可能在前后加解释）
    start = body.find("{")
    end = body.rfind("}")
    if start >= 0 and end > start:
        candidate = body[start:end + 1]
        try:
            data = json.loads(candidate)
        except Exception:
            data = None
        if isinstance(data, dict):
            say = clean_reply(str(data.get("text") or ""))
            clip = str(data.get("clip") or "").strip()
            if clip not in WHISPER_CLIPS:
                clip = ""
            if say:
                return say, clip
            # 是合法 JSON 但 text 为空：不要把原始 JSON 当气泡文本（用户会看到
            # `{"clip": "poke"}` 这种东西）。clip 仍可保留，文本交给调用方兜底。
            return "", clip

    # 不是 JSON（纯文本回复）：整段当气泡文本，clip 留空
    return clean_reply(raw), ""
