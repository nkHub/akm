#!/usr/bin/env python3
"""markdown_kb 检索质量基准脚本

用法（在仓库根目录执行）：
    python scripts/benchmark_markdown_kb.py
    python scripts/benchmark_markdown_kb.py --top-k 5 --semantic-weight 0.6 --keyword-weight 0.4

功能：
    1. 在临时 home 目录下用 PluginManager 真实加载 markdown_kb 插件；
    2. 用本地 mock embedding（词级 hashing 向量）重建索引，不依赖外部服务；
    3. 对一组带标准答案的测试用例执行 query，计算 Accuracy@k / MRR / NDCG@k；
    4. 支持传多个权重组合做对比（用于评估 semantic_weight / keyword_weight 调参效果）。

注意：
    mock embedding 是字面词级相似度，无法覆盖同义词/近义词改写场景；
    因此本脚本衡量的是「检索机制与排序是否按预期工作」，而非真实语义模型能力。
    真实场景请把 mock embedding 替换为 AKM /v1/embeddings 后再复跑。
"""
import argparse
import asyncio
import hashlib
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

# 仓库根加入 sys.path，便于 import akm 与 plugins
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx

# ── 测试文档集：多个主题，query 通过字面词命中对应文档 ──
# 注意：真实 embedding 模式下本机 AKM 网关批量上限为 10，文档需控制在 10 个 chunk 内。
# 每篇精简为一到两个段落，确保总 chunk 数不超过上限，避免 rebuild 时 502。
DOCS = {
    "refund.md": """# 退款流程

用户在订单完成后的七天内可以申请退款。退款申请提交后，由客服审核，审核通过后原路退回支付账户，通常三个工作日到账。

## 退费条件
商品未拆封、无使用痕迹、在七天无理由退货期内。
""",
    "deploy.md": """# 部署指南

服务使用 Docker 镜像部署到生产环境，推荐使用 docker compose 编排，将容器映射到宿主机的 8080 端口。

## 部署步骤
拉取最新镜像、编写 docker-compose.yml、启动容器并检查健康检查接口。
""",
    "api.md": """# API 接口规范

所有接口统一使用 REST 风格，返回 JSON 格式。请求需要在 Header 中携带 API Key 完成鉴权，未携带或非法 Key 返回 401。
""",
    "database.md": """# 数据库优化

慢查询通常由缺少索引导致。排查时先开启慢查询日志，再针对高频查询创建合适索引，避免全表扫描。
""",
    "auth.md": """# 登录鉴权

系统使用 JWT 令牌完成会话管理。用户登录成功后签发 access_token，有效期 2 小时，过期后使用 refresh_token 续期。
""",
}

# ── 测试用例：每条包含查询与期望命中的文档名 ──
# expected 允许多个文档名（命中任意一个即视为正确），一般填一个主题文档
# 近义改写用例（query 用与文档不同的表达）仅在真实 embedding 下才能命中，
# mock 字面匹配会 miss，用于对比两种 embedding 的语义覆盖能力。
TEST_CASES = [
    {"query": "如何申请退款，退款流程是怎样的", "expected": ["refund.md"]},
    {"query": "退款多久到账，退费需要什么条件", "expected": ["refund.md"]},
    {"query": "怎么用 docker 部署到生产环境", "expected": ["deploy.md"]},
    {"query": "docker compose 上线配置怎么写", "expected": ["deploy.md"]},
    {"query": "接口鉴权需要带什么请求头", "expected": ["api.md"]},
    {"query": "API 返回 401 是什么原因", "expected": ["api.md"]},
    {"query": "数据库慢查询如何优化", "expected": ["database.md"]},
    {"query": "数据库索引怎么建", "expected": ["database.md"]},
    {"query": "登录后 token 过期怎么办", "expected": ["auth.md"]},
    {"query": "JWT 会话管理如何保障安全", "expected": ["auth.md"]},
    # 近义改写：与文档用词不同，字面无重叠
    {"query": "买的东西不满意能退货吗", "expected": ["refund.md"]},
    {"query": "怎么把服务发布到线上服务器", "expected": ["deploy.md"]},
    {"query": "调用接口时身份校验失败怎么办", "expected": ["api.md"]},
    {"query": "查询很慢是为什么，怎么提速", "expected": ["database.md"]},
    {"query": "用户登录态失效了会怎么样", "expected": ["auth.md"]},
]


# ── mock embedding：字面词级 hashing 向量 ──
DIM = 128
_EMBED_MODEL = "text-embedding-3-small"

def _tokenize(text: str) -> list[str]:
    """极简中英文 tokenization：中文按 1-2 gram，英文按空白词。

    不引入 jieba 依赖，保持脚本自包含；语义靠字面重叠模拟。
    """
    tokens: list[str] = []
    lower = str(text or "").lower()
    # 英文/数字词
    for word in __import__("re").findall(r"[a-z0-9_]+", lower):
        if len(word) >= 2:
            tokens.append(word)
    # 中文：连续汉字串按 1-gram 与 2-gram 切分
    for run in __import__("re").findall(r"[\u4e00-\u9fff]+", lower):
        for i in range(len(run)):
            tokens.append(run[i])
            if i + 1 < len(run):
                tokens.append(run[i : i + 2])
    return tokens


def _embed_text(text: str) -> list[float]:
    """把文本映射为固定维度向量（词级 hashing 累加 + L2 归一化）。"""
    vec = [0.0] * DIM
    for token in _tokenize(text):
        digest = hashlib.md5(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % DIM
        vec[index] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 1e-12:
        return vec
    return [v / norm for v in vec]


# ── 指标计算 ──
def _ndcg_at_k(ranks: list[int | None], k: int) -> float:
    """逐用例计算 NDCG@k 后取平均，结果归一化到 [0, 1]。

    每个用例只含一个期望文档，因此单个用例的 IDCG 为 1/log2(2)=1；
    未命中（rank 为 None）贡献 0，命中但排名 > k 也贡献 0。
    """
    if not ranks:
        return 0.0
    total = 0.0
    for rank in ranks:
        if rank is not None and rank <= k:
            total += 1.0 / math.log2(rank + 1)
    return total / len(ranks)


def _rank_of_first_expected(hits: list[dict], expected: list[str]) -> int | None:
    """返回首个期望文档在 hits 中的 1-based 排名；未命中返回 None。"""
    for index, hit in enumerate(hits, start=1):
        if hit.get("file_name") in expected:
            return index
    return None


# ── 插件环境搭建（与 tests/test_server.py 同构） ──
async def _setup_plugin(work_root: Path, use_real: bool, embedding_model: str):
    """在临时 home 下加载 markdown_kb 插件并重建索引，返回插件实例。

    use_real=True 时使用本机 AKM /v1/embeddings（需先启动服务）；
    use_real=False 时使用本地 mock embedding（字面词级 hashing），不依赖外部服务。
    """
    from fastapi import FastAPI
    from akm.plugins.plugin_manager import PluginManager

    if not use_real:
        # 模拟 HTTP embedding 服务：拦截 AsyncClient.post
        async def fake_post(self, url, json=None, **kwargs):
            if url.endswith("/v1/embeddings"):
                inputs = json.get("input")
                if isinstance(inputs, str):
                    inputs = [inputs]
                return httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "data": [
                            {"object": "embedding", "index": idx, "embedding": _embed_text(item)}
                            for idx, item in enumerate(inputs)
                        ],
                        "model": json.get("model", _EMBED_MODEL),
                    },
                )
            return httpx.Response(404, json={"detail": f"unexpected url: {url}"})

        import plugins.markdown_kb.index as mkb_module

        mkb_module.httpx.AsyncClient.post = fake_post

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    plugin = pm.plugins["markdown_kb"]
    plugin.enabled = True
    plugin.config = dict(pm.get_config("markdown_kb") or {})
    plugin.config["embedding_model"] = embedding_model
    plugin.config["reranker_model"] = ""
    plugin.config["score_threshold"] = 0.0  # 基准聚焦排序质量，关闭阈值过滤

    plugin.runtime_ready = await plugin.on_load() is not False

    # 写入测试文档
    docs_dir = work_root / ".akm" / "markdown_kb" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for name, content in DOCS.items():
        (docs_dir / name).write_text(content, "utf-8")

    rebuild = await plugin.rebuild_index()
    print(f"[setup] 文档 {rebuild.get('doc_count')} 篇，chunk {rebuild.get('chunk_count')} 个")
    return plugin


async def _run_benchmark(plugin, top_k: int, semantic_weight: float, keyword_weight: float) -> dict:
    """用指定权重跑完全部用例，返回指标汇总。"""
    plugin.config["semantic_weight"] = semantic_weight
    plugin.config["keyword_weight"] = keyword_weight

    hits_count = []
    ranks = []
    for case in TEST_CASES:
        result = await plugin.query(
            {"question": case["query"], "top_k": top_k}
        )
        hits = result.get("hits") or []
        hits_count.append(len(hits))
        rank = _rank_of_first_expected(hits, case["expected"])
        if rank is not None:
            ranks.append(rank)

    total = len(TEST_CASES)
    accuracy_at_k = sum(1 for r in ranks if r <= top_k) / total
    mrr = sum(1.0 / r for r in ranks) / total
    ndcg = _ndcg_at_k(ranks, top_k)
    return {
        "accuracy_at_k": accuracy_at_k,
        "mrr": mrr,
        "ndcg_at_k": ndcg,
        "avg_hits": sum(hits_count) / total,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="markdown_kb 检索质量基准")
    parser.add_argument("--top-k", type=int, default=3, help="召回条数，默认 3")
    parser.add_argument("--semantic-weight", type=float, default=1.0)
    parser.add_argument("--keyword-weight", type=float, default=0.0)
    parser.add_argument("--weights", type=str, default="",
                        help='逗号分隔的多组权重对比，如 "1:0,0.6:0.4,0.3:0.7"；'
                             "传入后忽略 --semantic-weight/--keyword-weight")
    parser.add_argument("--real", action="store_true",
                        help="使用本机 AKM /v1/embeddings 真实向量（需先启动服务，模型见 --embedding-model）")
    parser.add_argument("--embedding-model", type=str, default=_EMBED_MODEL,
                        help="真实 embedding 模型名，默认 text-embedding-3-small")
    args = parser.parse_args()

    work_root = Path(tempfile.mkdtemp(prefix="mkb-bench-"))
    try:
        # 把 home 重定向到临时目录，隔离插件数据
        original_home = Path.home
        Path.home = classmethod(lambda cls: work_root)  # type: ignore[assignment]
        plugin = await _setup_plugin(work_root, args.real, args.embedding_model)
        Path.home = original_home  # type: ignore[assignment]

        weight_groups = []
        if args.weights:
            for pair in args.weights.split(","):
                sem, kw = pair.split(":")
                weight_groups.append((float(sem), float(kw)))
        else:
            weight_groups.append((args.semantic_weight, args.keyword_weight))

        print(f"[bench] top_k={args.top_k}，用例 {len(TEST_CASES)} 条，权重组 {len(weight_groups)} 组"
              f"（embedding={'real:' + args.embedding_model if args.real else 'mock'}）")
        print(f"{'sem':>5} {'kw':>5} {'Acc@k':>7} {'MRR':>6} {'NDCG@k':>8} {'avg_hits':>8}")
        for sem, kw in weight_groups:
            metrics = await _run_benchmark(plugin, args.top_k, sem, kw)
            print(
                f"{sem:>5.2f} {kw:>5.2f} {metrics['accuracy_at_k']:>7.2%} "
                f"{metrics['mrr']:>6.3f} {metrics['ndcg_at_k']:>8.3f} {metrics['avg_hits']:>8.1f}"
            )
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
