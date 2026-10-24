"""桌宠对话与碎碎念模块单测。

覆盖：记忆读写与裁剪、提示词拼装、回复清洗、响应解析、错误路径。
不发起真实网络请求（``call_chat`` 只测参数校验与错误分支）。
"""

import json
import sys
from pathlib import Path

import pytest

_PET_DIR = Path(__file__).resolve().parents[1] / "plugins" / "dafeiyu" / "pet"
if str(_PET_DIR) not in sys.path:
    sys.path.insert(0, str(_PET_DIR))

import companion  # noqa: E402


# ── 记忆读写 ───────────────────────────────────────────────

def test_memory_roundtrip(tmp_path):
    """存进去再读出来，内容一致。"""
    path = tmp_path / "memory.json"
    mem = companion.Memory()
    mem.chat.append(companion.ChatTurn(user="你好", assistant="你好呀", at=1.0))
    mem.whispers.append({"text": "今天好热", "at": 2.0})
    assert companion.save_memory(mem, path)

    loaded = companion.load_memory(path)
    assert len(loaded.chat) == 1
    assert loaded.chat[0].user == "你好"
    assert loaded.chat[0].assistant == "你好呀"
    assert len(loaded.whispers) == 1
    assert loaded.whispers[0]["text"] == "今天好热"


def test_load_memory_missing_file(tmp_path):
    """文件不存在时返回空记忆，不抛异常。"""
    mem = companion.load_memory(tmp_path / "nope.json")
    assert mem.chat == []
    assert mem.whispers == []


def test_load_memory_corrupt_file(tmp_path):
    """文件损坏时返回空记忆（不能因为坏文件让桌宠起不来）。"""
    path = tmp_path / "bad.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    mem = companion.load_memory(path)
    assert mem.chat == []


def test_memory_from_dict_skips_invalid_entries():
    """字段缺失或类型不符的条目被跳过，不抛异常。"""
    mem = companion.Memory.from_dict({
        "chat": [
            {"user": "ok", "assistant": "fine"},
            {"user": "", "assistant": "空用户"},      # 缺 user
            {"assistant": "缺用户"},                    # 缺 user
            "not a dict",                               # 类型不对
            {"user": "x", "assistant": "y", "at": "bad"},  # at 非法但有内容
        ],
        "whispers": [
            {"text": "有效"},
            {"no_text": 1},
            "bad",
        ],
    })
    assert len(mem.chat) == 2
    assert mem.chat[0].user == "ok"
    assert mem.chat[1].at == 0.0   # 非法 at 兜底成 0
    assert len(mem.whispers) == 1


def test_trim_memory_chat_limit():
    """对话记忆裁到上限，保留最近的。"""
    mem = companion.Memory()
    for i in range(companion.MAX_CHAT_TURNS + 10):
        mem.chat.append(companion.ChatTurn(user=f"u{i}", assistant=f"a{i}"))
    companion.trim_memory(mem)
    assert len(mem.chat) == companion.MAX_CHAT_TURNS
    # 保留的是最后 N 条
    assert mem.chat[-1].user == f"u{companion.MAX_CHAT_TURNS + 9}"


def test_trim_memory_whisper_limit():
    mem = companion.Memory()
    for i in range(companion.MAX_WHISPERS + 5):
        mem.whispers.append({"text": f"w{i}"})
    companion.trim_memory(mem)
    assert len(mem.whispers) == companion.MAX_WHISPERS


def test_save_memory_trims(tmp_path):
    """保存时自动裁剪，磁盘上不会无限增长。"""
    path = tmp_path / "memory.json"
    mem = companion.Memory()
    for i in range(companion.MAX_CHAT_TURNS + 20):
        mem.chat.append(companion.ChatTurn(user=f"u{i}", assistant=f"a{i}"))
    companion.save_memory(mem, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["chat"]) == companion.MAX_CHAT_TURNS


# ── 提示词拼装 ─────────────────────────────────────────────

def test_build_chat_messages_structure():
    """messages 结构：system 打头，历史交替，本轮问题在最后。"""
    mem = companion.Memory()
    mem.chat.append(companion.ChatTurn(user="第一轮", assistant="第一答"))
    msgs = companion.build_chat_messages(mem, "第二个问题")
    assert msgs[0]["role"] == "system"
    assert msgs[1] == {"role": "user", "content": "第一轮"}
    assert msgs[2] == {"role": "assistant", "content": "第一答"}
    assert msgs[-1] == {"role": "user", "content": "第二个问题"}


def test_build_chat_messages_empty_history():
    """没有历史时只有 system + 本轮问题。"""
    msgs = companion.build_chat_messages(companion.Memory(), "你好")
    assert len(msgs) == 2
    assert msgs[-1]["content"] == "你好"


def test_build_chat_messages_caps_history():
    """历史超长时只取最近 N 轮，不把全量塞给模型。"""
    mem = companion.Memory()
    for i in range(companion.MAX_CHAT_TURNS + 10):
        mem.chat.append(companion.ChatTurn(user=f"u{i}", assistant=f"a{i}"))
    msgs = companion.build_chat_messages(mem, "新问题")
    # system + 2*N + 本轮
    assert len(msgs) == 1 + companion.MAX_CHAT_TURNS * 2 + 1


def test_build_whisper_messages_includes_recent():
    """碎碎念把最近说过的带上，避免重复。"""
    recent = [{"text": "今天天气不错"}, {"text": "想喝奶茶"}]
    msgs = companion.build_whisper_messages(recent)
    assert msgs[0]["role"] == "system"
    assert "今天天气不错" in msgs[0]["content"]
    assert "换一句" in msgs[0]["content"]


def test_build_whisper_messages_no_history():
    msgs = companion.build_whisper_messages([])
    assert "最近已经说过" not in msgs[0]["content"]


# ── 回复清洗 ───────────────────────────────────────────────

def test_clean_reply_strips_whitespace_and_newlines():
    """换行折成空格，气泡只显示一行。"""
    assert companion.clean_reply("  你好\n世界  ") == "你好 世界"


def test_clean_reply_strips_chinese_quotes():
    assert companion.clean_reply("“你好呀”") == "你好呀"


def test_clean_reply_strips_corner_brackets():
    assert companion.clean_reply("「你好呀」") == "你好呀"


def test_clean_reply_keeps_inner_quotes():
    """只去一层外层引号，内部标点保留。"""
    assert companion.clean_reply("他说“好”") == "他说“好”"


def test_clean_reply_empty():
    assert companion.clean_reply("") == ""
    assert companion.clean_reply(None) == ""


# ── 响应解析 ───────────────────────────────────────────────

def test_parse_chat_response_ok():
    body = {"choices": [{"message": {"content": "你好呀"}}]}
    text, error = companion.parse_chat_response(body)
    assert text == "你好呀"
    assert error == ""


def test_parse_chat_response_multimodal_content():
    """content 是分段数组时拼出 text 片段。"""
    body = {"choices": [{"message": {"content": [
        {"type": "text", "text": "前半"},
        {"type": "image", "url": "x"},
        {"type": "text", "text": "后半"},
    ]}}]}
    text, error = companion.parse_chat_response(body)
    assert text == "前半后半"
    assert error == ""


def test_parse_chat_response_empty_choices():
    text, error = companion.parse_chat_response({"choices": []})
    assert text == ""
    assert error


def test_parse_chat_response_error_field():
    """上游把错误放在 error 字段时，把消息透出来。"""
    text, error = companion.parse_chat_response(
        {"error": {"message": "余额不足", "type": "insufficient_quota"}}
    )
    assert text == ""
    assert "余额不足" in error


def test_parse_chat_response_not_dict():
    text, error = companion.parse_chat_response("nope")
    assert text == ""
    assert error


def test_parse_chat_response_empty_content():
    """空回复要报错，不能当成成功。"""
    text, error = companion.parse_chat_response(
        {"choices": [{"message": {"content": "   "}}]}
    )
    assert text == ""
    assert error


# ── call_chat 参数校验 ─────────────────────────────────────

def test_call_chat_requires_model():
    """没配置模型时直接返回错误，不发请求。"""
    result = companion.call_chat("http://127.0.0.1:1", "", [])
    assert result.ok is False
    assert "模型" in result.error


def test_call_chat_requires_base_url():
    result = companion.call_chat("", "gpt-4o", [])
    assert result.ok is False


def test_call_chat_connection_error_is_reported():
    """连不上时返回错误而不是抛异常（端口 1 必然连不上）。"""
    result = companion.call_chat(
        "http://127.0.0.1:1", "test-model",
        [{"role": "user", "content": "hi"}],
        timeout=1.0,
    )
    assert result.ok is False
    assert result.error
