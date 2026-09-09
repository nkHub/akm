"""markdown-kb 插件层单元测试。

当前覆盖 `_embed_texts` 的批量分组逻辑：AKM embedding 网关对单次请求
超过 10 条会返回 502，插件需按每批 10 条拆分并顺序合并结果。
"""
import asyncio
import pathlib
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, "plugins/markdown_kb")
import index as _mk_index  # noqa: E402


class _FakeEmbeddingResponse:
    """模拟 /v1/embeddings 响应，每条返回一个可区分的向量。"""

    def __init__(self, rows: list[list[float]]):
        self.status_code = 200
        self._rows = rows

    def json(self) -> dict:
        return {"data": [{"embedding": row} for row in self._rows]}


def _build_plugin() -> "_mk_index.Plugin":
    """用 __new__ 轻量构造插件实例，只挂 _embed_texts 需要的依赖。"""
    tmp = tempfile.mkdtemp()
    pathlib.Path.home = lambda: pathlib.Path(tmp)
    plugin = _mk_index.Plugin.__new__(_mk_index.Plugin)
    plugin._akm_base_url = lambda: "http://127.0.0.1:9"
    plugin.logger = type("_L", (), {"info": lambda *a, **k: None})()
    return plugin


def _stub_post(batch_sizes: list[int], seed: list[int]):
    """注入 httpx.AsyncClient.post：校验每批不超过 10 条、记录批次大小并
    按全局序号返回向量，便于断言结果顺序。"""

    async def fake_post(self_or_none, url: str, json: dict):
        n = len(json["input"])
        assert n <= 10, f"单批 embedding 请求超过 10 条: {n}"
        batch_sizes.append(n)
        rows = []
        for _ in range(n):
            rows.append([float(seed[0])])
            seed[0] += 1
        return _FakeEmbeddingResponse(rows)

    return fake_post


@pytest.mark.asyncio
async def test_embed_texts_splits_large_batches_by_ten():
    """25 条文本应拆为 10/10/5 三批，且结果按输入顺序合并。"""
    batch_sizes: list[int] = []
    seed = [0]
    fake_post = _stub_post(batch_sizes, seed)
    plugin = _build_plugin()
    with patch.object(_mk_index.httpx.AsyncClient, "post", new=fake_post):
        vectors = await plugin._embed_texts([str(i) for i in range(25)], "text-embedding-3-small")
    assert batch_sizes == [10, 10, 5]
    assert len(vectors) == 25
    # 结果顺序与输入一致：第 i 条向量的值应为浮点 i。
    assert vectors[5] == [5.0]
    assert vectors[24] == [24.0]


@pytest.mark.asyncio
async def test_embed_texts_single_batch_when_below_limit():
    """8 条文本应单批发送且不拆。"""
    batch_sizes: list[int] = []
    fake_post = _stub_post(batch_sizes, [0])
    plugin = _build_plugin()
    with patch.object(_mk_index.httpx.AsyncClient, "post", new=fake_post):
        vectors = await plugin._embed_texts([str(i) for i in range(8)], "text-embedding-3-small")
    assert batch_sizes == [8]
    assert len(vectors) == 8


@pytest.mark.asyncio
async def test_embed_texts_empty_input_returns_empty():
    """空输入应直接返回空列表，不发起任何请求。"""
    batch_sizes: list[int] = []
    fake_post = _stub_post(batch_sizes, [0])
    plugin = _build_plugin()
    with patch.object(_mk_index.httpx.AsyncClient, "post", new=fake_post):
        vectors = await plugin._embed_texts([], "text-embedding-3-small")
    assert vectors == []
    assert batch_sizes == []


def test_dashboard_card_returns_zero_metrics_without_store():
    """记忆存储未就绪（__new__ 轻量实例）时，卡片仍应返回、四项指标为 0。"""
    plugin = _build_plugin()
    card = plugin.dashboard_card()
    assert isinstance(card, dict)
    assert card["id"] == "markdown_kb"
    assert card["icon"] == "book"
    assert card["title"] == "知识库记忆"
    labels = [m["label"] for m in card["metrics"]]
    assert labels == ["记忆条目", "平均记忆值", "累计命中", "高值(>0.5)"]
    assert all(m["value"] == 0 or m["value"] == 0.0 for m in card["metrics"])
    assert card["actions"] == [{"label": "查看 ›", "href": "/plugins/markdown_kb"}]


def test_dashboard_card_maps_memory_stats():
    """有记忆统计时，卡片应把 summary 四项映射为对应指标值。"""
    plugin = _build_plugin()
    plugin.get_memory_stats = lambda: {
        "summary": {
            "memory_chunk_count": 12,
            "memory_avg_value": 0.66,
            "memory_total_hits": 99,
            "memory_high_value_count": 5,
        }
    }
    card = plugin.dashboard_card()
    values = {m["label"]: m["value"] for m in card["metrics"]}
    assert values == {"记忆条目": 12, "平均记忆值": 0.66, "累计命中": 99, "高值(>0.5)": 5}