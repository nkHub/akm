import asyncio
import tempfile
import json
import io
import logging
from pathlib import Path
from unittest.mock import AsyncMock
from collections import OrderedDict

import httpx
import pytest
import shutil
from pathlib import Path
from httpx import ASGITransport, AsyncClient

from akm.audit import AuditLogQueue, write_log, list_logs
from akm.db import get_connection, init_db, get_keys_log_path, get_db_path
from akm.health import HealthMonitor
from akm.key_pool import get_key
from akm.server import app, lifespan, _build_runtime_debug_payload, _default_image_generation_model, _image_supported_models_from_config


@pytest.mark.asyncio
async def test_markdown_kb_on_request_injects_hits_for_chat_request(monkeypatch):
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    plugin.config = {
        "embedding_model": "text-embedding-3-small",
        "reranker_model": "",
        "top_k": 2,
    }

    async def fake_retrieve(question, top_k, embedding_model, reranker_model, project_context=None):
        assert question == "请根据知识库回答"
        assert top_k == 2
        assert embedding_model == "text-embedding-3-small"
        assert reranker_model == ""
        assert project_context in (None, {})
        return [{
            "file_name": "guide.md",
            "title": "Guide",
            "chunk_index": 0,
            "chunk_text": "Knowledge base content",
        }]

    monkeypatch.setattr(plugin, "_retrieve", fake_retrieve)

    request = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "user", "content": "请根据知识库回答"}
        ],
    }
    out = await plugin.on_request(request)
    assert out is not None
    assert out["model"] == "gpt-4o-mini"
    assert out["messages"][0]["role"] == "system"
    assert "Knowledge base content" in out["messages"][0]["content"]


@pytest.mark.asyncio
async def test_markdown_kb_on_request_skips_when_no_hits(monkeypatch):
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    plugin.config = {
        "embedding_model": "text-embedding-3-small",
        "reranker_model": "",
        "top_k": 2,
    }

    async def fake_retrieve(question, top_k, embedding_model, reranker_model, project_context=None):
        return []

    monkeypatch.setattr(plugin, "_retrieve", fake_retrieve)

    request = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "user", "content": "请根据知识库回答"}
        ],
    }
    out = await plugin.on_request(request)
    assert out is request
    assert out["model"] == "gpt-4o-mini"
    assert out["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_markdown_kb_on_request_handles_responses_instructions(monkeypatch):
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    plugin.config = {
        "embedding_model": "text-embedding-3-small",
        "reranker_model": "",
        "top_k": 1,
    }

    async def fake_retrieve(question, top_k, embedding_model, reranker_model, project_context=None):
        return [{
            "file_name": "guide.md",
            "title": "Guide",
            "chunk_index": 0,
            "chunk_text": "Knowledge base content",
        }]

    monkeypatch.setattr(plugin, "_retrieve", fake_retrieve)

    request = {
        "model": "gpt-4.1",
        "input": "给我答案",
        "instructions": "你是一个助手。",
    }
    out = await plugin.on_request(request)
    assert out is not None
    assert out["model"] == "gpt-4.1"
    assert "Knowledge base content" in out["instructions"]
    assert "原始系统要求" in out["instructions"]


@pytest.mark.asyncio
async def test_markdown_kb_on_request_handles_messages_system_injection(monkeypatch):
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    plugin.config = {
        "embedding_model": "text-embedding-3-small",
        "reranker_model": "",
        "top_k": 1,
    }

    async def fake_retrieve(question, top_k, embedding_model, reranker_model, project_context=None):
        assert question == "请结合知识库回答"
        return [{
            "file_name": "guide.md",
            "title": "Guide",
            "chunk_index": 0,
            "chunk_text": "Knowledge base content",
        }]

    monkeypatch.setattr(plugin, "_retrieve", fake_retrieve)

    request = {
        "model": "claude-sonnet-4",
        "max_tokens": 1024,
        "system": "你是一个助手。",
        "messages": [
            {"role": "user", "content": "请结合知识库回答"}
        ],
    }
    out = await plugin.on_request(request)
    assert out is not None
    assert out["model"] == "claude-sonnet-4"
    assert "Knowledge base content" in out["system"]
    assert "原始系统要求" in out["system"]


def test_markdown_kb_extracts_project_context_from_opencode_message_text():
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    request = {
        "model": "gpt-5.4",
        "messages": [{
            "role": "system",
            "content": "Here is some useful information about the environment you are running in:\n<env>\n  Working directory: /Users/nk/Desktop/ccs\n  Workspace root folder: /Users/nk/Desktop/ccs\n  Is directory a git repo: yes\n</env>",
        }],
    }

    context = plugin._extract_project_context(request)
    assert context["workspace_root"] == "/Users/nk/Desktop/ccs"
    assert context["working_directory"] == "/Users/nk/Desktop/ccs"
    assert context["project_name"] == "ccs"
    assert context["project_id"] == "/Users/nk/Desktop/ccs"
    assert context["source"] == "opencode.messages.content.env"


def test_markdown_kb_extracts_project_context_from_codex_environment_context_text():
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    request = {
        "model": "gpt-5.4",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": "<environment_context>\n  <cwd>/Users/nk/Desktop/project/bonnie-clyde</cwd>\n  <shell>zsh</shell>\n  <current_date>2026-06-18</current_date>\n  <workspace_root>/Users/nk/Desktop/project/bonnie-clyde</workspace_root>\n</environment_context>",
            }],
        }],
    }

    context = plugin._extract_project_context(request)
    assert context["workspace_root"] == "/Users/nk/Desktop/project/bonnie-clyde"
    assert context["working_directory"] == "/Users/nk/Desktop/project/bonnie-clyde"
    assert context["project_name"] == "bonnie-clyde"
    assert context["project_id"] == "/Users/nk/Desktop/project/bonnie-clyde"
    assert context["source"] == "codex.input.content.text.environment_context"


def test_markdown_kb_extracts_project_context_from_claude_system_reminder_text():
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    request = {
        "model": "kimi-k2.5-free",
        "messages": [{
            "role": "user",
            "content": [{
                "type": "text",
                "text": "# Environment\nYou have been invoked in the following environment: \n - Primary working directory: /Users/nk/Desktop/publish/erp2_table\n - Is a git repository: true\n - Platform: darwin\n",
            }],
        }],
    }

    context = plugin._extract_project_context(request)
    assert context["workspace_root"] == ""
    assert context["working_directory"] == "/Users/nk/Desktop/publish/erp2_table"
    assert context["project_name"] == "erp2_table"
    assert context["project_id"] == "/Users/nk/Desktop/publish/erp2_table"
    assert context["source"] == "claude.messages.content.text"


def test_markdown_kb_prefer_project_hits_keeps_matching_project_documents():
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    hits = [
        {"file_name": "bonnie-clyde.md", "title": "Bonnie Clyde", "score": 0.91},
        {"file_name": "AI Key Manager.md", "title": "AI Key Manager", "score": 0.95},
        {"file_name": "notes.md", "title": "bonnie-clyde rollout", "score": 0.87},
    ]
    project_context = {
        "project_name": "bonnie-clyde",
        "project_id": "/Users/nk/Desktop/project/bonnie-clyde",
    }

    filtered = plugin._prefer_project_hits(hits, project_context)
    assert len(filtered) == 2
    assert filtered[0]["file_name"] == "bonnie-clyde.md"
    assert filtered[1]["file_name"] == "notes.md"


def test_markdown_kb_prefer_project_hits_falls_back_when_no_match():
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    hits = [
        {"file_name": "AI Key Manager.md", "title": "AI Key Manager", "score": 0.95},
        {"file_name": "notes.md", "title": "General Notes", "score": 0.87},
    ]
    project_context = {
        "project_name": "bonnie-clyde",
        "project_id": "/Users/nk/Desktop/project/bonnie-clyde",
    }

    filtered = plugin._prefer_project_hits(hits, project_context)
    assert filtered == hits


def test_markdown_kb_chunking_builtin_tree(monkeypatch, tmp_path):
    """验证内置标题树切片器：按标题拆分为独立 chunk。"""
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    path = tmp_path / "guide.md"
    path.write_text("# Title\n\n第一段内容\n\n## Details\n\n第二段内容\n\n### Sub\n\n第三段内容", "utf-8")

    chunks = plugin._chunk_markdown_file(path, {
        "chunk_size": 800,
        "chunk_overlap": 120,
        "document_workspace_root": "",
    })

    assert len(chunks) >= 2
    # 验证每个 chunk 都有 heading_path
    for c in chunks:
        assert "heading_path" in c
        assert "title" in c
        assert "chunk_text" in c
        assert c["chunk_text"].strip()
    assert chunks[0]["title"] == "Title"
    assert chunks[0]["heading_level"] == 1
    # 验证 heading_path 存储为 JSON
    import json
    hp0 = json.loads(chunks[0]["heading_path"])
    assert "Title" in hp0


@pytest.mark.asyncio
async def test_markdown_kb_bind_file_workspace_marks_file_for_rebuild(monkeypatch):
    from fastapi import FastAPI
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-bind-workspace")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    async def fake_post(self, url, json=None, **kwargs):
        if url.endswith("/v1/embeddings"):
            inputs = json.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]
            return httpx.Response(200, json={"data": [{"embedding": [1.0, float(idx + 1)], "index": idx} for idx, _ in enumerate(inputs)]})
        return httpx.Response(404, json={"detail": "unexpected url"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    plugin = pm.plugins["markdown_kb"]
    plugin.enabled = True
    plugin.config = pm.get_config("markdown_kb") or {}
    await plugin.on_load()

    docs_dir = test_home / ".akm" / "markdown_kb" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "guide.md").write_text("# Guide\n\nBody", "utf-8")

    await plugin.rebuild_index()
    bind_result = plugin.bind_file_workspace("guide.md", "/Users/nk/Desktop/ccs/")
    assert bind_result["ok"] is True
    assert bind_result["workspace_root"] == "/Users/nk/Desktop/ccs"
    assert bind_result["needs_rebuild"] is True

    files = plugin.list_files()
    assert files["files"][0]["workspace_root"] == "/Users/nk/Desktop/ccs"

    preview = plugin.preview_sync()
    assert preview["summary"]["changed"] == 1


@pytest.mark.asyncio
async def test_markdown_kb_rebuild_file_persists_bound_workspace(monkeypatch):
    from fastapi import FastAPI
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-bind-rebuild")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    async def fake_post(self, url, json=None, **kwargs):
        if url.endswith("/v1/embeddings"):
            inputs = json.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]
            return httpx.Response(200, json={"data": [{"embedding": [1.0, float(idx + 1)], "index": idx} for idx, _ in enumerate(inputs)]})
        return httpx.Response(404, json={"detail": "unexpected url"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    plugin = pm.plugins["markdown_kb"]
    plugin.enabled = True
    plugin.config = pm.get_config("markdown_kb") or {}
    await plugin.on_load()

    docs_dir = test_home / ".akm" / "markdown_kb" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "guide.md").write_text("# Guide\n\nBody", "utf-8")

    plugin.bind_file_workspace("guide.md", "/Users/nk/Desktop/project/bonnie-clyde")
    rebuild = await plugin.rebuild_file("guide.md")
    assert rebuild["ok"] is True

    documents = plugin._store.list_documents()
    assert documents
    assert all(item["workspace_root"] == "/Users/nk/Desktop/project/bonnie-clyde" for item in documents)


def test_markdown_kb_with_workspace_searches_public_and_current_workspace_documents():
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    documents = [
        {"file_name": "public.md", "workspace_root": ""},
        {"file_name": "AI Key Manager.md", "workspace_root": "/Users/nk/Desktop/ccs"},
        {"file_name": "bonnie-clyde.md", "workspace_root": "/Users/nk/Desktop/project/bonnie-clyde"},
        {"file_name": "erp2_table.md", "workspace_root": "/Users/nk/Desktop/publish/erp2_table"},
    ]
    project_context = {
        "workspace_root": "/Users/nk/Desktop/project/bonnie-clyde",
        "working_directory": "/Users/nk/Desktop/project/bonnie-clyde",
    }

    filtered = plugin._filter_documents_by_workspace(documents, project_context)
    assert len(filtered) == 2
    assert filtered[0]["file_name"] == "public.md"
    assert filtered[1]["file_name"] == "bonnie-clyde.md"


def test_markdown_kb_without_workspace_only_searches_unbound_documents():
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    documents = [
        {"file_name": "AI Key Manager.md", "workspace_root": ""},
        {"file_name": "bonnie-clyde.md", "workspace_root": "/Users/nk/Desktop/project/bonnie-clyde"},
    ]
    project_context = {
        "workspace_root": "",
        "working_directory": "",
    }

    filtered = plugin._filter_documents_by_workspace(documents, project_context)
    assert len(filtered) == 1
    assert filtered[0]["file_name"] == "AI Key Manager.md"


@pytest.mark.asyncio
async def test_markdown_kb_on_request_injects_realistic_project_context(monkeypatch):
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    plugin.config = {
        "embedding_model": "text-embedding-3-small",
        "reranker_model": "",
        "top_k": 1,
    }

    async def fake_retrieve(question, top_k, embedding_model, reranker_model, project_context=None):
        return [{
            "file_name": "guide.md",
            "title": "Guide",
            "chunk_index": 0,
            "chunk_text": "Knowledge base content",
        }]

    monkeypatch.setattr(plugin, "_retrieve", fake_retrieve)

    request = {
        "model": "gpt-5.4",
        "messages": [
            {
                "role": "system",
                "content": "<env>\n  Working directory: /Users/nk/Desktop/ccs\n  Workspace root folder: /Users/nk/Desktop/ccs\n</env>",
            },
            {
                "role": "user",
                "content": "请根据知识库回答",
            },
        ],
    }

    out = await plugin.on_request(request)
    assert out is not None
    assert out["messages"][0]["role"] == "system"
    assert "workspace_root: /Users/nk/Desktop/ccs" in out["messages"][0]["content"]
    assert "working_directory: /Users/nk/Desktop/ccs" in out["messages"][0]["content"]
    assert "project_name: ccs" in out["messages"][0]["content"]
    assert "Knowledge base content" in out["messages"][0]["content"]


@pytest.mark.asyncio
async def test_markdown_kb_on_request_with_workspace_uses_public_and_current_workspace_hits(monkeypatch):
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    plugin.config = {
        "embedding_model": "text-embedding-3-small",
        "reranker_model": "",
        "top_k": 1,
    }

    async def fake_retrieve(question, top_k, embedding_model, reranker_model, project_context=None):
        documents = [
            {"file_name": "public.md", "workspace_root": "", "embedding": [0.1]},
            {"file_name": "bonnie-clyde.md", "workspace_root": "/Users/nk/Desktop/project/bonnie-clyde", "embedding": [0.1]},
            {"file_name": "AI Key Manager.md", "workspace_root": "/Users/nk/Desktop/ccs", "embedding": [0.1]},
        ]
        filtered = plugin._filter_documents_by_workspace(documents, project_context)
        return [{
            "file_name": item["file_name"],
            "title": item["file_name"],
            "chunk_index": 0,
            "chunk_text": item["file_name"],
        } for item in filtered]

    monkeypatch.setattr(plugin, "_retrieve", fake_retrieve)

    request = {
        "model": "gpt-5.4",
        "messages": [
            {
                "role": "system",
                "content": "<env>\n  Working directory: /Users/nk/Desktop/ccs\n  Workspace root folder: /Users/nk/Desktop/ccs\n</env>",
            },
            {
                "role": "user",
                "content": "请根据知识库回答",
            },
        ],
    }

    out = await plugin.on_request(request)
    assert out is not None
    assert out["messages"][0]["role"] == "system"
    assert "public.md" in out["messages"][0]["content"]
    assert "AI Key Manager.md" in out["messages"][0]["content"]
    assert "bonnie-clyde.md" not in out["messages"][0]["content"]


@pytest.mark.asyncio
async def test_markdown_kb_on_request_without_workspace_only_uses_unbound_documents(monkeypatch):
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    plugin.config = {
        "embedding_model": "text-embedding-3-small",
        "reranker_model": "",
        "top_k": 1,
    }

    async def fake_retrieve(question, top_k, embedding_model, reranker_model, project_context=None):
        documents = [
            {"file_name": "public.md", "workspace_root": "", "embedding": [0.1]},
            {"file_name": "bonnie-clyde.md", "workspace_root": "/Users/nk/Desktop/project/bonnie-clyde", "embedding": [0.1]},
        ]
        filtered = plugin._filter_documents_by_workspace(documents, project_context)
        return [{
            "file_name": item["file_name"],
            "title": item["file_name"],
            "chunk_index": 0,
            "chunk_text": item["file_name"],
        } for item in filtered]

    monkeypatch.setattr(plugin, "_retrieve", fake_retrieve)

    request = {
        "model": "gpt-5.4",
        "messages": [
            {
                "role": "user",
                "content": "请根据知识库回答",
            },
        ],
    }

    out = await plugin.on_request(request)
    assert out is not None
    assert out["messages"][0]["role"] == "system"
    assert "public.md" in out["messages"][0]["content"]
    assert "bonnie-clyde.md" not in out["messages"][0]["content"]


@pytest.mark.asyncio
async def test_markdown_kb_runs_after_model_matcher_for_plain_aliases(monkeypatch):
    from fastapi import FastAPI
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-priority")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    cfg_path = test_home / ".akm" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "plugin_states": {
            "model_matcher": True,
            "markdown_kb": True,
        },
        "plugin_configs": {
            "model_matcher": {"aliases": "default-chat=gpt-4o-mini"},
            "markdown_kb": {
                "embedding_model": "text-embedding-3-small",
                "reranker_model": "",
                "chat_model": "gpt-4o-mini",
                "chunk_size": 800,
                "chunk_overlap": 120,
                "top_k": 2,
            },
        },
    }, ensure_ascii=False), "utf-8")

    pm = PluginManager()
    await pm.load_all(FastAPI())
    markdown_plugin = pm.plugins["markdown_kb"]

    async def fake_retrieve(question, top_k, embedding_model, reranker_model, project_context=None):
        return [{
            "file_name": "guide.md",
            "title": "Guide",
            "chunk_index": 0,
            "chunk_text": "Injected docs",
        }]

    monkeypatch.setattr(markdown_plugin, "_retrieve", fake_retrieve)

    body = {
        "model": "default-chat",
        "messages": [{"role": "user", "content": "根据知识库回答"}],
    }
    out = await pm.run_hook("on_request", request=body)
    request_out = out["request"]
    assert request_out["model"] == "gpt-4o-mini"
    assert request_out["messages"][0]["role"] == "system"
    assert "Injected docs" in request_out["messages"][0]["content"]


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    conn = get_connection()
    init_db(conn)
    conn.close()
    monkeypatch.setattr("akm.server._stats_cache", {})
    # 为 lifespan 未生效的测试环境提供模拟 http_client 和 plugin_manager
    app.state.http_client = AsyncMock()
    app.state.plugin_manager = None
    app.state.health_monitor = HealthMonitor()
    yield


@pytest.mark.asyncio
async def test_chat_completions_success(monkeypatch):
    """正常请求返回上游响应"""
    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None, request_timeout=None, original_user_agent=""):
        return {
            "status_code": 200,
            "body": '{"choices":[{"message":{"content":"hello"}}]}',
            "key_alias": "test-key",
            "provider": "openai",
            "model": "gpt-4",
            "error": "",
            "latency_ms": 100,
        }
    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "hello"


@pytest.mark.asyncio
async def test_chat_completions_no_keys(monkeypatch):
    """没有可用 key 时返回 503"""
    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None, request_timeout=None, original_user_agent=""):
        return {
            "status_code": 503,
            "body": "",
            "key_alias": "",
            "provider": "",
            "model": "gpt-4",
            "error": "没有可用的 API key",
            "latency_ms": 0,
        }
    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 503
    data = resp.json()
    assert "没有可用" in data["detail"]


@pytest.mark.asyncio
async def test_embeddings_forward_success(monkeypatch):
    """/v1/embeddings 应复用通用转发链路返回普通 JSON。"""

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None, request_timeout=None, original_user_agent=""):
        assert api_path == "embeddings"
        assert original_user_agent == "python-httpx/0.28.1"
        return {
            "status_code": 200,
            "body": '{"object":"list","data":[{"object":"embedding","embedding":[0.1,0.2],"index":0}],"model":"text-embedding-3-small","usage":{"prompt_tokens":8,"total_tokens":8}}',
            "key_alias": "embed-key",
            "provider": "openai",
            "model": "text-embedding-3-small",
            "error": "",
            "latency_ms": 80,
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "text-embedding-3-small", "input": "hello"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["object"] == "embedding"
    assert data["model"] == "text-embedding-3-small"


@pytest.mark.asyncio
async def test_rerank_forward_success(monkeypatch):
    """/v1/rerank 应参考 embeddings 走普通 JSON 透传链路。"""

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None, request_timeout=None, original_user_agent=""):
        assert api_path == "rerank"
        assert original_user_agent == "python-httpx/0.28.1"
        return {
            "status_code": 200,
            "body": '{"results":[{"index":1,"relevance_score":0.98},{"index":0,"relevance_score":0.42}],"model":"rerank-v1","usage":{"total_tokens":12}}',
            "key_alias": "rerank-key",
            "provider": "openai",
            "model": "rerank-v1",
            "error": "",
            "latency_ms": 70,
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/rerank",
            json={"model": "rerank-v1", "query": "hello", "documents": ["a", "b"]},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["index"] == 1
    assert data["model"] == "rerank-v1"


@pytest.mark.asyncio
async def test_image_generations_forward_success(monkeypatch):
    """/v1/images/generations 应复用通用转发链路返回普通 JSON。"""

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None, request_timeout=None, original_user_agent=""):
        assert api_path == "images/generations"
        assert request_timeout == 300
        assert original_user_agent == "python-httpx/0.28.1"
        return {
            "status_code": 200,
            "body": '{"created":123,"data":[{"url":"https://example.com/image.png"}]}',
            "key_alias": "image-key",
            "provider": "openai",
            "model": "gpt-image-1",
            "error": "",
            "latency_ms": 90,
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/images/generations",
            json={"model": "gpt-image-1", "prompt": "a cat"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["url"] == "https://example.com/image.png"


@pytest.mark.asyncio
async def test_image_generations_uses_default_model_when_omitted(monkeypatch):
    """图片生成接口未显式传 model 时，应自动回填 gpt-image-2。"""

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None, request_timeout=None, original_user_agent=""):
        assert api_path == "images/generations"
        assert body["model"] == "gpt-image-2"
        assert request_timeout == 300
        return {
            "status_code": 200,
            "body": '{"created":123,"data":[{"url":"https://example.com/default-image.png"}]}',
            "key_alias": "image-key",
            "provider": "openai",
            "model": body["model"],
            "error": "",
            "latency_ms": 90,
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "log_request_body": False,
            "log_response_body": False,
            "stream_capture_max_bytes": 262144,
            "image_supported_models": "gpt-image-2,gpt-image-3",
        },
    )
    monkeypatch.setattr(
        "akm.server.list_keys",
        lambda: [
            {"alias": "image-key", "provider": "openai", "status": "active", "models": "gpt-image-2"},
        ],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/images/generations",
            json={"prompt": "a cat"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["url"] == "https://example.com/default-image.png"


@pytest.mark.asyncio
async def test_image_generations_returns_clear_error_when_default_model_unavailable(monkeypatch):
    """未传 model 且当前没有 key 支持 gpt-image-2 时，应直接返回可读错误。"""

    monkeypatch.setattr(
        "akm.server.list_keys",
        lambda: [
            {"alias": "k1", "provider": "openai", "status": "active", "models": "gpt-4.1"},
        ],
    )
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "log_request_body": False,
            "log_response_body": False,
            "stream_capture_max_bytes": 262144,
            "image_supported_models": "gpt-image-2,gpt-image-3",
        },
    )
    monkeypatch.setattr("akm.server.forward_request", AsyncMock())
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/images/generations",
            json={"prompt": "a cat"},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert "gpt-image-2" in body["detail"]


@pytest.mark.asyncio
async def test_image_edits_forward_success(monkeypatch):
    """/v1/images/edits 应接收 multipart/form-data 并复用通用转发链路。"""

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None, request_timeout=None, original_user_agent=""):
        assert api_path == "images/edits"
        assert body["model"] == "gpt-image-2"
        assert body["__akm_multipart__"] is True
        assert body["__akm_form_fields__"]["prompt"] == "edit cat"
        assert body["__akm_form_files__"]["image"][0] == "cat.png"
        assert request_timeout == 300
        assert original_user_agent == "python-httpx/0.28.1"
        return {
            "status_code": 200,
            "body": '{"created":123,"data":[{"url":"https://example.com/edited.png"}]}',
            "key_alias": "image-key",
            "provider": "openai",
            "model": body["model"],
            "error": "",
            "latency_ms": 90,
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/images/edits",
            data={"model": "gpt-image-2", "prompt": "edit cat"},
            files={"image": ("cat.png", io.BytesIO(b"fake-bytes"), "image/png")},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["url"] == "https://example.com/edited.png"


@pytest.mark.asyncio
async def test_special_routes_forward_original_user_agent_when_present(monkeypatch):
    """特殊接口应继续把入口原始 User-Agent 传给公共转发链路，以便请求头开关统一生效。"""

    captured = []

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None, request_timeout=None, original_user_agent=""):
        captured.append((api_path, original_user_agent))
        return {
            "status_code": 200,
            "body": '{"ok":true}',
            "key_alias": "test-key",
            "provider": "openai",
            "model": body.get("model", ""),
            "error": "",
            "latency_ms": 1,
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/embeddings",
            json={"model": "text-embedding-3-small", "input": "hello"},
            headers={"User-Agent": "OpenCode/1.0.0"},
        )
        await client.post(
            "/v1/rerank",
            json={"model": "rerank-v1", "query": "hello", "documents": ["a", "b"]},
            headers={"User-Agent": "OpenCode/1.0.0"},
        )
        await client.post(
            "/v1/images/generations",
            json={"model": "gpt-image-1", "prompt": "a cat"},
            headers={"User-Agent": "OpenCode/1.0.0"},
        )
        await client.post(
            "/v1/images/edits",
            data={"model": "gpt-image-2", "prompt": "edit cat"},
            files={"image": ("cat.png", io.BytesIO(b"fake-bytes"), "image/png")},
            headers={"User-Agent": "OpenCode/1.0.0"},
        )

    assert captured == [
        ("embeddings", "OpenCode/1.0.0"),
        ("rerank", "OpenCode/1.0.0"),
        ("images/generations", "OpenCode/1.0.0"),
        ("images/edits", "OpenCode/1.0.0"),
    ]


@pytest.mark.asyncio
async def test_image_edits_uses_default_model_when_omitted(monkeypatch):
    """图片编辑接口未显式传 model 时，应在存在可用 key 时自动回填 gpt-image-2。"""

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None, request_timeout=None, original_user_agent=""):
        assert api_path == "images/edits"
        assert body["model"] == "gpt-image-2"
        assert body["__akm_form_fields__"]["model"] == "gpt-image-2"
        assert request_timeout == 300
        return {
            "status_code": 200,
            "body": '{"created":123,"data":[{"url":"https://example.com/default-edit.png"}]}',
            "key_alias": "image-key",
            "provider": "openai",
            "model": body["model"],
            "error": "",
            "latency_ms": 90,
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "log_request_body": False,
            "log_response_body": False,
            "stream_capture_max_bytes": 262144,
            "image_supported_models": "gpt-image-2,gpt-image-3",
        },
    )
    monkeypatch.setattr(
        "akm.server.list_keys",
        lambda: [
            {"alias": "image-key", "provider": "openai", "status": "active", "models": "gpt-image-2"},
        ],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/images/edits",
            data={"prompt": "edit cat"},
            files={"image": ("cat.png", io.BytesIO(b"fake-bytes"), "image/png")},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["url"] == "https://example.com/default-edit.png"


@pytest.mark.asyncio
async def test_image_edits_returns_clear_error_when_default_model_unavailable(monkeypatch):
    """图片编辑未传 model 且当前没有 key 支持 gpt-image-2 时，应直接返回可读错误。"""

    monkeypatch.setattr(
        "akm.server.list_keys",
        lambda: [
            {"alias": "k1", "provider": "openai", "status": "active", "models": "gpt-4.1"},
        ],
    )
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "log_request_body": False,
            "log_response_body": False,
            "stream_capture_max_bytes": 262144,
            "image_supported_models": "gpt-image-2,gpt-image-3",
        },
    )
    monkeypatch.setattr("akm.server.forward_request", AsyncMock())
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/images/edits",
            data={"prompt": "edit cat"},
            files={"image": ("cat.png", io.BytesIO(b"fake-bytes"), "image/png")},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert "gpt-image-2" in body["detail"]


def test_image_supported_models_from_config_supports_multiple_values():
    models = _image_supported_models_from_config({"image_supported_models": "gpt-image-2, gpt-image-3 , gpt-image-fast"})
    assert models == ["gpt-image-2", "gpt-image-3", "gpt-image-fast"]


def test_default_image_generation_model_uses_first_configured_value():
    model = _default_image_generation_model({"image_supported_models": "gpt-image-2,gpt-image-3"})
    assert model == "gpt-image-2"


@pytest.mark.asyncio
async def test_non_stream_audit_log_prefers_forwarded_request_body(monkeypatch):
    """审计日志应优先记录 proxy 返回的实际转发请求体，而不是入口原始 body。"""

    captured = {}

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None, request_timeout=None, original_user_agent=""):
        return {
            "status_code": 200,
            "body": '{"choices":[{"message":{"content":"ok"}}]}',
            "request_body_for_log": '{"messages":[{"content":"__AKM_EMAIL_deadbeefcafe__"}]}',
            "key_alias": "test-key",
            "provider": "openai",
            "model": "gpt-4",
            "error": "",
            "latency_ms": 50,
        }

    async def fake_submit(app_obj, data):
        captured.update(data)

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server._submit_audit_log", fake_submit)
    monkeypatch.setattr("akm.server.load_config", lambda: {"log_request_body": True, "log_response_body": False, "stream_capture_max_bytes": 262144})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "a@test.com"}]},
        )

    assert resp.status_code == 200
    assert captured["request_body"] == '{"messages":[{"content":"__AKM_EMAIL_deadbeefcafe__"}]}'
    assert "a@test.com" not in captured["request_body"]


@pytest.mark.asyncio
async def test_stream_audit_log_prefers_forwarded_request_body(monkeypatch):
    """流式审计日志同样应优先记录实际转发请求体。"""

    captured = {}

    class DummyResp:
        async def aiter_bytes(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            return None

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None, request_timeout=None, original_user_agent=""):
        return {
            "stream": True,
            "status_code": 200,
            "response": DummyResp(),
            "adapter": None,
            "request_body_for_log": '{"messages":[{"content":"__AKM_PHONE_deadbeefcafe__"}],"stream":true}',
            "key_alias": "stream-key",
            "provider": "openai",
            "model": "gpt-4",
        }

    async def fake_submit(app_obj, data):
        captured.update(data)

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server._submit_audit_log", fake_submit)
    monkeypatch.setattr("akm.server.load_config", lambda: {"log_request_body": True, "log_response_body": False, "stream_capture_max_bytes": 262144})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "13800138000"}], "stream": True},
        ) as resp:
            assert resp.status_code == 200
            async for _ in resp.aiter_text():
                pass

    assert captured["request_body"] == '{"messages":[{"content":"__AKM_PHONE_deadbeefcafe__"}],"stream":true}'
    assert "13800138000" not in captured["request_body"]


@pytest.mark.asyncio
async def test_streaming_response_emits_on_response_only_after_stream_finishes(monkeypatch):
    """流式请求结束后应由 server 侧统一触发一次 on_response。"""

    class DummyResp:
        def __init__(self):
            self.closed = False

        async def aiter_bytes(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            self.closed = True

    class DummyPM:
        def __init__(self):
            self.events = []
            self.plugins = {}

        async def run_hook(self, hook, **kwargs):
            if hook == "on_response":
                self.events.append(kwargs)
            return kwargs

    pm = DummyPM()
    app.state.plugin_manager = pm

    upstream_resp = DummyResp()

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None, request_timeout=None, original_user_agent=""):
        return {
            "stream": True,
            "status_code": 200,
            "response": upstream_resp,
            "adapter": None,
            "key_alias": "stream-key",
            "provider": "openai",
            "model": "gpt-4",
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        ) as resp:
            assert resp.status_code == 200
            chunks = []
            async for chunk in resp.aiter_text():
                chunks.append(chunk)

    assert any("data: [DONE]" in chunk for chunk in chunks)
    assert upstream_resp.closed is True
    assert len(pm.events) == 1
    meta = pm.events[0]["response"]
    assert meta["ok"] is True
    assert meta["stream"] is True
    assert meta["key_alias"] == "stream-key"


@pytest.mark.asyncio
async def test_health_endpoint():
    """健康检查端点"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_debug_runtime_exposes_core_runtime_fields():
    """运行时诊断端点应返回进程、健康和队列等核心观测字段。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/debug/runtime")

    assert resp.status_code == 200
    data = resp.json()
    assert "process" in data
    assert "health" in data
    assert "audit_queue" in data
    assert "http_client" in data
    assert data["process"]["pid"] > 0
    assert "rss_bytes" in data["process"]
    assert "thread_count" in data["process"]
    assert "stopped" in data["audit_queue"]
    assert "worker_alive" in data["audit_queue"]


@pytest.mark.asyncio
async def test_debug_runtime_history_returns_recent_monitor_events():
    """运行时事件历史端点应返回最近的自愈与退化事件。"""
    monitor = HealthMonitor()
    monitor.record_http_client_recreated("test recreate")
    monitor.set_audit_backlog(pending=12, dropped=2, failures=1)
    monitor.db_consecutive_failures = 3
    monitor.db_last_error = "db locked"
    monitor._append_event(
        "db.probe.failed",
        {"consecutive_failures": monitor.db_consecutive_failures, "error": monitor.db_last_error},
    )
    monitor.pending_audit_tasks = 350
    monitor.ready_payload()
    app.state.health_monitor = monitor

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/debug/runtime/history?limit=10")

    assert resp.status_code == 200
    payload = resp.json()
    events = payload["events"]
    event_names = [item["event"] for item in events]
    assert "http_client.recreated" in event_names
    assert "audit.queue.dropped" in event_names
    assert "db.probe.failed" in event_names
    assert "health.status.changed" in event_names


@pytest.mark.asyncio
async def test_health_ready_and_detail_endpoints_reflect_monitor_state():
    """监护端点应返回 ready/detail 状态与关键指标。"""
    monitor = HealthMonitor()
    monitor.pending_audit_tasks = 350
    monitor.consecutive_upstream_failures = 12
    app.state.health_monitor = monitor

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ready_resp = await client.get("/health/ready")
        detail_resp = await client.get("/health/detail")

    assert ready_resp.status_code == 200
    ready = ready_resp.json()
    assert ready["status"] == "degraded"
    assert "audit_backlog_high" in ready["reasons"]

    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["status"] == "degraded"
    assert detail["metrics"]["pending_audit_tasks"] == 350
    assert detail["metrics"]["consecutive_upstream_failures"] == 12


@pytest.mark.asyncio
async def test_health_detail_exposes_audit_queue_drop_signal():
    """审计队列发生丢弃时，detail 端点应暴露该降级信号。"""
    monitor = HealthMonitor()
    monitor.set_audit_backlog(pending=0, dropped=3, failures=1)
    app.state.health_monitor = monitor

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail_resp = await client.get("/health/detail")

    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["status"] == "degraded"
    assert "audit_queue_dropped" in detail["reasons"]
    assert detail["metrics"]["audit_queue_dropped"] == 3


@pytest.mark.asyncio
async def test_health_detail_exposes_http_client_recreate_metrics():
    """detail 端点应暴露共享客户端的软重建状态。"""
    monitor = HealthMonitor()
    monitor.record_http_client_recreated("too many upstream timeouts")
    app.state.health_monitor = monitor

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail_resp = await client.get("/health/detail")

    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["metrics"]["http_client_recreate_count"] == 1
    assert detail["metrics"]["http_client_last_recreate_reason"] == "too many upstream timeouts"
    assert detail["metrics"]["http_client_last_recreated_at"] != ""


@pytest.mark.asyncio
async def test_runtime_debug_exposes_stopped_audit_queue_state():
    """运行时诊断应明确暴露审计队列是否已停止，便于排查“服务还活着但日志拒收”。"""
    queue = AuditLogQueue(maxsize=2)
    await queue.start()
    await queue.stop()
    app.state.audit_log_queue = queue

    payload = _build_runtime_debug_payload(app)

    assert payload["audit_queue"]["enabled"] is True
    assert payload["audit_queue"]["stopped"] is True
    assert payload["audit_queue"]["worker_alive"] is False


@pytest.mark.asyncio
async def test_lifespan_shutdown_only_stops_its_own_audit_queue(monkeypatch):
    """旧 lifespan 退出时不应误停新 lifespan 已挂到 app.state 的审计队列。"""
    from fastapi import FastAPI

    async def fake_load_all(self, fastapi_app, db=None):
        return None

    class FakeHttpClientPoolManager:
        """最小假实现：避免测试触发真实网络客户端创建。"""

        is_route_pool = False

        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

        def stats(self):
            return {}

    monkeypatch.setattr("akm.server.load_custom_agents", lambda: None)
    monkeypatch.setattr("akm.server.PluginManager.load_all", fake_load_all)
    monkeypatch.setattr("akm.server.HttpClientPoolManager", FakeHttpClientPoolManager)

    fastapi_app = FastAPI()

    old_cm = lifespan(fastapi_app)
    await old_cm.__aenter__()
    old_queue = fastapi_app.state.audit_log_queue
    old_task = fastapi_app.state.health_task

    new_cm = lifespan(fastapi_app)
    await new_cm.__aenter__()
    new_queue = fastapi_app.state.audit_log_queue
    new_task = fastapi_app.state.health_task

    assert new_queue is not old_queue
    assert new_task is not old_task
    assert old_queue.is_stopped() is False
    assert new_queue.is_stopped() is False

    await old_cm.__aexit__(None, None, None)

    assert old_queue.is_stopped() is True
    assert new_queue.is_stopped() is False
    assert new_queue.worker_alive() is True

    await new_cm.__aexit__(None, None, None)
    assert new_queue.is_stopped() is True


@pytest.mark.asyncio
async def test_health_ready_returns_503_when_db_probe_is_critical():
    """当 DB 探针连续失败过多时，就绪探针应返回 503。"""
    monitor = HealthMonitor()
    monitor.db_consecutive_failures = 10
    app.state.health_monitor = monitor

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ready_resp = await client.get("/health/ready")

    assert ready_resp.status_code == 503
    body = ready_resp.json()
    assert body["status"] == "unhealthy"
    assert body["ready"] is False


@pytest.mark.asyncio
async def test_recreate_shared_http_client_closes_old_client_and_resets_monitor(monkeypatch):
    """连续失败触发软重建时，应替换共享客户端并关闭旧连接池。"""

    class DummyClient:
        def __init__(self, name):
            self.name = name
            self.closed = False

        async def aclose(self):
            self.closed = True

    old_client = DummyClient("old")
    new_client = DummyClient("new")

    app.state.http_client = old_client
    app.state.http_client_lock = asyncio.Lock()
    monitor = HealthMonitor()
    monitor.consecutive_upstream_failures = monitor.UPSTREAM_FAILS_RECREATE
    app.state.health_monitor = monitor

    monkeypatch.setattr("akm.server._build_http_client_pool_manager", lambda: new_client)

    from akm.server import _recreate_http_client_pool

    changed = await _recreate_http_client_pool(app, "too many upstream failures")

    assert changed is True
    assert app.state.http_client is new_client
    assert old_client.closed is True
    assert monitor.http_client_recreate_count == 1
    assert monitor.http_client_last_recreate_reason == "too many upstream failures"
    assert monitor.consecutive_upstream_failures == 0


@pytest.mark.asyncio
async def test_list_models(monkeypatch):
    """/v1/models 返回 active key 的模型列表"""
    monkeypatch.setattr("akm.server.list_keys", lambda: [
        {"alias": "k1", "provider": "openai", "status": "active", "models": "gpt-4,gpt-3.5-turbo"},
        {"alias": "k2", "provider": "deepseek", "status": "active", "models": "deepseek-chat"},
        {"alias": "k3", "provider": "openai", "status": "disabled", "models": "gpt-4"},
        {"alias": "k4", "provider": "openai", "status": "active", "models": "*", "provider_models": ["gpt-4.1", "gpt-4.1-mini"]},
    ])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    model_ids = {m["id"] for m in data["data"]}
    assert "gpt-4" in model_ids
    assert "gpt-3.5-turbo" in model_ids
    assert "deepseek-chat" in model_ids
    assert "gpt-4.1" in model_ids
    assert "gpt-4.1-mini" in model_ids
    # disabled key 的模型不出现
    assert len(data["data"]) == 5


@pytest.mark.asyncio
async def test_api_add_key_syncs_provider_models_from_remote(monkeypatch):
    """新增 key 时应同步拉取提供商模型列表并落库。"""

    class DummyResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-mini"}]}

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            return DummyResponse()

    monkeypatch.setattr("akm.server.httpx.AsyncClient", DummyAsyncClient)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/keys",
            json={
                "alias": "sync-key",
                "provider": "openai",
                "api_key": "sk-sync",
                "base_url": "https://example.com/v1",
                "models": "*",
            },
        )

    assert resp.status_code == 200
    key = get_connection().execute("SELECT provider_models FROM keys WHERE alias = ?", ("sync-key",)).fetchone()
    assert key is not None
    assert "gpt-4.1" in key["provider_models"]
    assert "gpt-4.1-mini" in key["provider_models"]


@pytest.mark.asyncio
async def test_api_add_key_rejects_wildcard_with_custom_models(monkeypatch):
    """保存 key 时，星号和自定义模型不能混用。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/keys",
            json={
                "alias": "bad-key",
                "provider": "openai",
                "api_key": "sk-bad",
                "models": "*,gpt-4.1",
            },
        )

    assert resp.status_code == 400
    assert "星号不能和自定义模型同时使用" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_api_add_key_with_explicit_models_skips_provider_model_sync(monkeypatch):
    """显式自定义模型保存时，不应自动请求提供商 /models。"""

    async def fail_fetch(*args, **kwargs):
        raise AssertionError("explicit models should not fetch provider models")

    monkeypatch.setattr("akm.server._fetch_provider_models", fail_fetch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/keys",
            json={
                "alias": "explicit-key",
                "provider": "openai",
                "api_key": "sk-explicit",
                "base_url": "https://example.com/v1",
                "models": "gpt-4.1,gpt-4.1-mini",
            },
        )

    assert resp.status_code == 200
    key = get_key("explicit-key")
    assert key is not None
    assert key["models"] == "gpt-4.1,gpt-4.1-mini"
    assert key["provider_models"] == []


@pytest.mark.asyncio
async def test_api_update_key_from_wildcard_to_explicit_models_clears_provider_models(monkeypatch):
    """从 wildcard 改成显式模型时，应清空旧的 provider_models，避免残留误导。"""

    async def fake_fetch(provider, api_key, base_url, auth_header):
        return ["gpt-4.1", "gpt-4.1-mini"]

    monkeypatch.setattr("akm.server._fetch_provider_models", fake_fetch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/keys",
            json={
                "alias": "switch-key",
                "provider": "openai",
                "api_key": "sk-switch",
                "base_url": "https://example.com/v1",
                "models": "*",
            },
        )
        assert create_resp.status_code == 200

        async def fail_fetch(*args, **kwargs):
            raise AssertionError("explicit models update should not fetch provider models")

        monkeypatch.setattr("akm.server._fetch_provider_models", fail_fetch)

        update_resp = await client.put(
            "/api/keys/switch-key",
            json={
                "alias": "switch-key",
                "provider": "openai",
                "models": "gpt-4.1",
            },
        )

    assert update_resp.status_code == 200
    key = get_key("switch-key")
    assert key is not None
    assert key["models"] == "gpt-4.1"
    assert key["provider_models"] == []


@pytest.mark.asyncio
async def test_api_refresh_key_provider_models(monkeypatch):
    """批量刷新 provider 模型列表接口应返回成功/失败统计。"""

    add_key = get_connection().execute
    conn = get_connection()
    conn.execute(
        "INSERT INTO keys (alias, provider, api_key, base_url, models, auth_header, priority, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("k1", "openai", "enc1", "https://example.com/v1", "*", "Bearer {api_key}", 0, "active"),
    )
    conn.execute(
        "INSERT INTO keys (alias, provider, api_key, base_url, models, auth_header, priority, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("k2", "openai", "enc2", "https://bad.example.com/v1", "*", "Bearer {api_key}", 1, "active"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("akm.key_pool._decrypt", lambda value: "sk-test")

    async def fake_fetch(provider, api_key, base_url, auth_header):
        if "bad" in str(base_url):
            raise ValueError("同步提供商模型列表失败: HTTP 500")
        return ["gpt-4.1", "gpt-4.1-mini"]

    monkeypatch.setattr("akm.server._fetch_provider_models", fake_fetch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/keys/refresh-models")

    assert resp.status_code == 200
    data = resp.json()
    assert data["refreshed"] == 1
    assert len(data["failed"]) == 1
    assert data["failed"][0]["alias"] == "k2"


@pytest.mark.asyncio
async def test_key_change_log_written_without_api_key(monkeypatch):
    """Key 变更应写入 keys.log，且不能包含 api_key 明文。"""

    class DummyResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-mini"}]}

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            return DummyResponse()

    monkeypatch.setattr("akm.server.httpx.AsyncClient", DummyAsyncClient)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/keys",
            json={
                "alias": "log-key",
                "provider": "openai",
                "api_key": "sk-secret-create",
                "base_url": "https://example.com/v1",
                "models": "*",
            },
        )
        assert create_resp.status_code == 200

        status_resp = await client.patch(
            "/api/keys/log-key/status",
            json={"status": "disabled"},
        )
        assert status_resp.status_code == 200

        delete_resp = await client.delete("/api/keys/log-key")
        assert delete_resp.status_code == 200

    log_path = Path(get_keys_log_path())
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "sk-secret-create" not in content

    rows = [json.loads(line) for line in content.splitlines() if line.strip()]
    events = [row["event"] for row in rows]
    assert events == ["key.config.created", "key.status.changed", "key.config.deleted"]
    assert all(row["category"] == "key_audit" for row in rows)
    assert all(row["scope"] == "configuration" for row in rows)
    assert rows[0]["details"]["api_key_updated"] is True
    assert rows[0]["details"]["after"]["alias"] == "log-key"
    assert rows[1]["details"]["before_status"] == "active"
    assert rows[1]["details"]["after_status"] == "disabled"
    assert rows[2]["details"]["before"]["alias"] == "log-key"


@pytest.mark.asyncio
async def test_api_delete_key_supports_alias_with_slash(monkeypatch):
    """包含斜杠的别名应可通过 URL 编码后正常删除。"""

    class DummyResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "gpt-4.1"}]}

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            return DummyResponse()

    monkeypatch.setattr("akm.server.httpx.AsyncClient", DummyAsyncClient)

    alias = "https://vww.bytego.team/pricing"
    encoded_alias = "https%3A%2F%2Fvww.bytego.team%2Fpricing"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/keys",
            json={
                "alias": alias,
                "provider": "openai",
                "api_key": "sk-delete-me",
                "base_url": "https://example.com/v1",
                "models": "*",
            },
        )
        assert create_resp.status_code == 200

        delete_resp = await client.delete(f"/api/keys/{encoded_alias}")

    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"ok": True, "alias": alias}
    assert get_key(alias) is None


@pytest.mark.asyncio
async def test_api_export_keys_omits_model_list(monkeypatch):
    """导出备份时不应包含 model_list 这类派生字段。"""

    class DummyResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-mini"}]}

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            return DummyResponse()

    monkeypatch.setattr("akm.server.httpx.AsyncClient", DummyAsyncClient)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/keys",
            json={
                "alias": "export-key",
                "provider": "openai",
                "api_key": "sk-export",
                "base_url": "https://example.com/v1",
                "models": "*",
            },
        )
        assert create_resp.status_code == 200

        resp = await client.get("/api/keys/export")

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) == 1
    assert "model_list" not in rows[0]
    assert rows[0]["provider_models"] == ["gpt-4.1", "gpt-4.1-mini"]


@pytest.mark.asyncio
async def test_api_logs_size_includes_db_and_log_files():
    db_path = Path(get_db_path())
    db_path.write_bytes(b"db")
    wal_path = db_path.parent / "akm.db-wal"
    shm_path = db_path.parent / "akm.db-shm"
    keys_log_path = db_path.parent / "keys.log"
    extra_log_path = db_path.parent / "extra.log"
    wal_path.write_bytes(b"wal")
    shm_path.write_bytes(b"shm")
    keys_log_path.write_text("hello", encoding="utf-8")
    extra_log_path.write_text("world!!", encoding="utf-8")

    expected_db_size = db_path.stat().st_size + wal_path.stat().st_size + shm_path.stat().st_size
    expected_log_size = keys_log_path.stat().st_size + extra_log_path.stat().st_size

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/logs/size")

    assert resp.status_code == 200
    data = resp.json()
    assert data["db_size"] == expected_db_size
    assert data["log_size"] == expected_log_size
    assert data["cache_size"] == expected_db_size + expected_log_size
    assert data["size"] == expected_db_size + expected_log_size


@pytest.mark.asyncio
async def test_api_logs_adds_conv_warning_labels(monkeypatch):
    """/api/logs 返回转换告警派生字段（codes + labels）"""
    async def fake_list_logs_async(**kwargs):
        return [{
        "request_headers": '{"x-akm-conv-warnings":"responses_store_not_mapped,responses_include_not_fully_mapped"}',
        "response_body": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }]

    async def fake_count_logs_async(**kwargs):
        return 1

    monkeypatch.setattr("akm.server.list_logs_async", fake_list_logs_async)
    monkeypatch.setattr("akm.server.count_logs_async", fake_count_logs_async)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/logs")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    row = data["data"][0]
    assert "responses_store_not_mapped" in row["conv_warning_codes"]
    assert "responses_include_not_fully_mapped" in row["conv_warning_codes"]
    assert "store 未映射" in row["conv_warning_labels"]
    assert "include 未完整映射" in row["conv_warning_labels"]


@pytest.mark.asyncio
async def test_api_logs_adds_estimated_cost_when_enabled(monkeypatch):
    """开启费用估算后，审计接口为每条日志附加固定美元费用。"""
    async def fake_list_logs_async(**kwargs):
        return [{
            "model": "gpt-4",
            "request_headers": "{}",
            "response_body": "",
            "prompt_tokens": 1_000_000,
            "completion_tokens": 1_000_000,
            "total_tokens": 2_000_000,
            "cached_tokens": 400_000,
            "cache_creation_tokens": 0,
        }]

    async def fake_count_logs_async(**kwargs):
        return 1

    monkeypatch.setattr("akm.server.list_logs_async", fake_list_logs_async)
    monkeypatch.setattr("akm.server.count_logs_async", fake_count_logs_async)
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {"cost_stats_enabled": True, "cost_pricing_table": "gpt-4=1/0.1/2"},
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/logs")

    assert resp.status_code == 200
    data = resp.json()
    assert data["cost_stats_enabled"] is True
    assert data["data"][0]["estimated_cost"] == 2.64
    assert data["data"][0]["cost_currency"] == "$"


@pytest.mark.asyncio
async def test_api_logs_omits_estimated_cost_when_disabled(monkeypatch):
    """费用估算关闭时，审计接口不计算也不返回每条费用。"""
    async def fake_list_logs_async(**kwargs):
        return [{
            "model": "gpt-4",
            "request_headers": "{}",
            "response_body": "",
            "prompt_tokens": 1_000_000,
            "completion_tokens": 1_000_000,
            "total_tokens": 2_000_000,
            "cached_tokens": 0,
            "cache_creation_tokens": 0,
        }]

    async def fake_count_logs_async(**kwargs):
        return 1

    monkeypatch.setattr("akm.server.list_logs_async", fake_list_logs_async)
    monkeypatch.setattr("akm.server.count_logs_async", fake_count_logs_async)
    monkeypatch.setattr("akm.server.load_config", lambda: {"cost_stats_enabled": False})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/logs")

    assert resp.status_code == 200
    data = resp.json()
    assert data["cost_stats_enabled"] is False
    assert "estimated_cost" not in data["data"][0]
    assert "cost_currency" not in data["data"][0]


def test_extract_tokens_from_messages_sse_fallback():
    from akm.server import _extract_tokens
    sse = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"x","content":[],"usage":{"input_tokens":123}}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":7,"cached_tokens":100}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
        'data: [DONE]\n\n'
    )
    out = _extract_tokens(sse)
    assert out is not None
    assert out["prompt_tokens"] == 223
    assert out["completion_tokens"] == 7
    assert out["total_tokens"] == 230
    assert out["cached_tokens"] == 100


def test_extract_tokens_prefers_anthropic_cache_read_input_tokens():
    from akm.server import _extract_tokens
    body = '{"usage":{"input_tokens":1200,"output_tokens":80,"cache_read_input_tokens":900,"cache_creation_input_tokens":300}}'
    out = _extract_tokens(body)
    assert out is not None
    assert out["prompt_tokens"] == 2100
    assert out["completion_tokens"] == 80
    assert out["cached_tokens"] == 900
    assert out["cache_creation_tokens"] == 300
    assert out["total_tokens"] == 2180


def test_extract_tokens_keeps_responses_input_tokens_without_cache_read_addback():
    from akm.server import _extract_tokens
    body = '{"usage":{"input_tokens":1200,"output_tokens":80,"cached_tokens":900,"total_tokens":1280}}'
    out = _extract_tokens(body)
    assert out is not None
    assert out["prompt_tokens"] == 1200
    assert out["completion_tokens"] == 80
    assert out["cached_tokens"] == 900
    assert out["total_tokens"] == 1280


def test_extract_tokens_keeps_explicit_zero_usage_metrics():
    from akm.server import _extract_tokens
    body = '{"results":[{"index":0,"relevance_score":0.88}],"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}'
    out = _extract_tokens(body)
    assert out is not None
    assert out["prompt_tokens"] == 0
    assert out["completion_tokens"] == 0
    assert out["total_tokens"] == 0


def test_estimate_tokens_light_when_usage_missing():
    from akm.server import _estimate_tokens_light
    req = {"model": "x", "messages": [{"role": "user", "content": "你好，帮我总结一下这段文本"}]}
    out = _estimate_tokens_light(req, "")
    assert out["prompt_tokens"] > 0
    assert out["completion_tokens"] == 0
    assert out["total_tokens"] == out["prompt_tokens"]


def test_bounded_stream_capture_keeps_head_and_tail_with_marker():
    from akm.server import _BoundedStreamCapture

    cap = _BoundedStreamCapture(1024)
    cap.append(b"abcdefghij" * 80)
    cap.append(b"klmnopqrst" * 80)
    cap.append(b"uvwxyz1234567890" * 80)

    text = cap.build_text()
    assert text.startswith("abcdefghij")
    assert "stream truncated by akm" in text
    assert text.endswith("uvwxyz1234567890")
    assert cap.truncated is True


def test_incremental_sse_usage_tracker_keeps_real_usage_after_truncation():
    """流式响应即使审计正文被截断，也应保留实时提取到的真实 usage。"""
    from akm.server import _BoundedStreamCapture, _IncrementalSSEUsageTracker

    tracker = _IncrementalSSEUsageTracker()
    capture = _BoundedStreamCapture(1024)

    prefix = 'data: {"type":"response.output_text.delta","delta":"' + ('x' * 5000) + '"}\n\n'
    suffix = (
        'data: {"type":"response.completed","response":{"usage":'
        '{"prompt_tokens":1234,"completion_tokens":56,"total_tokens":1290}}}\n\n'
        + 'data: {"type":"response.output_text.delta","delta":"'
        + ('y' * 5000)
        + '"}\n\n'
    )

    for part in (prefix.encode("utf-8"), suffix.encode("utf-8")):
        capture.append(part)
        tracker.append(part)

    truncated_text = capture.build_text()
    assert capture.truncated is True
    assert "response.completed" not in truncated_text

    tokens = tracker.build_tokens()
    assert tokens is not None
    assert tokens["prompt_tokens"] == 1234
    assert tokens["completion_tokens"] == 56
    assert tokens["total_tokens"] == 1290


@pytest.mark.asyncio
async def test_build_usage_metrics_does_not_estimate_when_upstream_explicitly_returns_zero_usage(monkeypatch):
    from starlette.requests import Request
    from akm.server import _build_usage_metrics, FLAG_USAGE_ESTIMATED_LIGHT

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/rerank",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "scheme": "http",
        "app": app,
    }
    request = Request(scope, receive)

    body = '{"results":[{"index":1,"relevance_score":0.98}],"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}'
    tokens, flags = await _build_usage_metrics(
        request=request,
        request_body={"model": "bge-reranker-v2-m3-free", "query": "hi", "documents": ["a"]},
        response_body=body,
        api_path="rerank",
        key_alias="rerank-key",
        provider="openai",
        adapter=None,
    )

    assert tokens["prompt_tokens"] == 0
    assert tokens["completion_tokens"] == 0
    assert tokens["total_tokens"] == 0
    assert FLAG_USAGE_ESTIMATED_LIGHT not in flags


@pytest.mark.asyncio
async def test_api_clean_logs_all_flag_clears_everything():
    write_log({"provider": "o", "key_alias": "k1", "model": "m", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    write_log({"provider": "o", "key_alias": "k2", "model": "m", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    assert len(list_logs(limit=10)) == 2

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/logs/clean", json={"all": True})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["deleted"] == 2
    assert len(list_logs(limit=10)) == 0


@pytest.mark.asyncio
async def test_api_list_agents_returns_messages_anthropic_switch():
    """/api/agents 返回 messages 的 /anthropic 开关状态。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/agents")

    assert resp.status_code == 200
    agents = {item["name"]: item for item in resp.json()["data"]}
    assert agents["deepseek"]["messages_use_anthropic_path"] is True
    assert agents["openai"]["messages_use_anthropic_path"] is False


@pytest.mark.asyncio
async def test_api_add_agent_persists_protocol_capability_fields(tmp_path, monkeypatch):
    """/api/agents 应允许保存下沉到 Agent 层的协议兼容能力字段。"""
    monkeypatch.setattr("akm.agent._get_config_path", lambda: str(tmp_path / "config.json"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/agents", json={
            "name": "vendorx",
            "default_base_url": "https://vendor.example.com/v1",
            "default_auth_header": "Bearer {api_key}",
            "supports_responses": True,
            "supports_chat": True,
            "supports_messages": False,
            "messages_use_anthropic_path": False,
            "inject_max_completion_tokens": True,
            "inject_reasoning_effort": True,
            "map_metadata_user_id_to_user": False,
            "responses_force_thinking_enabled": True,
            "responses_default_reasoning_effort": "high",
        })
        list_resp = await client.get("/api/agents")

    assert create_resp.status_code == 200
    agents = {item["name"]: item for item in list_resp.json()["data"]}
    assert agents["vendorx"]["inject_max_completion_tokens"] is True
    assert agents["vendorx"]["inject_reasoning_effort"] is True
    assert agents["vendorx"]["map_metadata_user_id_to_user"] is False
    assert agents["vendorx"]["responses_force_thinking_enabled"] is True
    assert agents["vendorx"]["responses_default_reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_api_stats_includes_cost_when_enabled(monkeypatch):
    """开启费用统计后 /api/stats 返回总费用与每日费用。"""
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "stats_include_estimated_usage": False,
            "cost_stats_enabled": True,
            "cost_pricing_table": "gpt-4=1/0.1/2\n*=0.5/0.05/1",
            "log_request_body": False,
            "log_response_body": False,
        },
    )
    # 清缓存，避免被同进程其他 stats 测试污染
    from akm import server as server_mod

    server_mod._stats_cache.clear()
    write_log({
        "provider": "openai",
        "key_alias": "k1",
        "model": "gpt-4",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 10,
        "error": "",
        "request_headers": "{}",
        "prompt_tokens": 1_000_000,
        "completion_tokens": 1_000_000,
        "total_tokens": 2_000_000,
        "cached_tokens": 400_000,
        "cache_creation_tokens": 0,
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stats?days=1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["cost_stats_enabled"] is True
    assert data["total_cost"] == 2.64
    assert data["cost_currency"] == "$"
    assert data["costs_by_currency"]["$"] == 2.64
    day_vals = list((data.get("daily") or {}).values())
    assert day_vals
    assert day_vals[0]["cost"] == 2.64


@pytest.mark.asyncio
async def test_api_stats_ignores_estimated_usage_tokens_by_default(monkeypatch):
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "stats_include_estimated_usage": False,
            "log_request_body": False,
            "log_response_body": False,
        },
    )
    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 10,
        "error": "",
        "request_headers": '{"x-akm-flags":"usage_estimated_light"}',
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 10,
        "cache_creation_tokens": 0,
    })
    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 12,
        "error": "",
        "request_headers": '{}',
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
        "cached_tokens": 30,
        "cache_creation_tokens": 0,
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stats?days=1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_requests"] == 1
    assert data["total_prompt_tokens"] == 50
    assert data["total_completion_tokens"] == 20
    assert data["total_tokens"] == 100
    assert data["total_cached_tokens"] == 30


@pytest.mark.asyncio
async def test_api_stats_can_include_estimated_usage_when_enabled(monkeypatch):
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "stats_include_estimated_usage": True,
            "log_request_body": False,
            "log_response_body": False,
        },
    )

    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 10,
        "error": "",
        "request_headers": '{"x-akm-flags":"usage_estimated_light"}',
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 10,
        "cache_creation_tokens": 0,
    })
    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 12,
        "error": "",
        "request_headers": '{}',
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
        "cached_tokens": 30,
        "cache_creation_tokens": 0,
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stats?days=1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_requests"] == 2
    assert data["total_prompt_tokens"] == 140
    assert data["total_completion_tokens"] == 70
    assert data["total_tokens"] == 250
    assert data["total_cached_tokens"] == 40


@pytest.mark.asyncio
async def test_api_stats_ignores_rows_without_key_alias():
    write_log({
        "provider": "openai",
        "key_alias": "",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 503,
        "latency_ms": 5,
        "error": "没有可用的 API key",
        "request_headers": '{}',
        "prompt_tokens": 999,
        "completion_tokens": 888,
        "total_tokens": 1887,
        "cached_tokens": 0,
        "cache_creation_tokens": 0,
    })
    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 12,
        "error": "",
        "request_headers": '{}',
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
        "cached_tokens": 30,
        "cache_creation_tokens": 0,
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stats?days=1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_requests"] == 1
    assert data["total_prompt_tokens"] == 50
    assert data["total_completion_tokens"] == 20
    assert data["total_tokens"] == 100
    assert "real-key" in data["by_key"]
    assert "" not in data["by_key"]


@pytest.mark.asyncio
async def test_api_stats_ignores_failed_rows_even_with_key_alias():
    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 502,
        "latency_ms": 10,
        "error": "upstream failed",
        "request_headers": '{}',
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
        "cached_tokens": 30,
        "cache_creation_tokens": 0,
    })
    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 12,
        "error": "",
        "request_headers": '{}',
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
        "cached_tokens": 30,
        "cache_creation_tokens": 0,
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stats?days=1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_requests"] == 1
    assert data["by_key"]["real-key"]["requests"] == 1
    assert data["by_provider"]["openai"]["requests"] == 1


@pytest.mark.asyncio
async def test_api_stats_normalizes_messages_usage_before_aggregation(monkeypatch):
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "stats_include_estimated_usage": True,
            "log_request_body": False,
            "log_response_body": False,
        },
    )

    write_log({
        "provider": "anthropic",
        "key_alias": "claude-key",
        "model": "claude-sonnet-4",
        "request_body": "",
        "response_body": '{"usage":{"input_tokens":12121,"cache_read_input_tokens":21632,"output_tokens":159,"service_tier":"standard"}}',
        "status_code": 200,
        "latency_ms": 10,
        "error": "",
        "request_headers": '{}',
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "cache_creation_tokens": 0,
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stats?days=1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_requests"] == 1
    assert data["total_prompt_tokens"] == 12121
    assert data["total_completion_tokens"] == 159
    assert data["total_cached_tokens"] == 21632
    assert data["total_tokens"] == 33912


@pytest.mark.asyncio
async def test_plugin_config_api_roundtrip():
    class DummyPM:
        def __init__(self):
            self.saved = None

        def get_config(self, name):
            return {"enabled": True} if name == "protocol_converter" else None

        def set_config(self, name, data):
            self.saved = (name, data)
            return {"ok": True}

    app.state.plugin_manager = DummyPM()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        get_resp = await client.get("/api/plugin-config/protocol_converter")
        post_resp = await client.post("/api/plugin-config/protocol_converter", json={"enabled": False})

    assert get_resp.status_code == 200
    assert get_resp.json()["enabled"] is True
    assert post_resp.status_code == 200
    assert app.state.plugin_manager.saved == ("protocol_converter", {"enabled": False})


@pytest.mark.asyncio
async def test_markdown_kb_plugin_loads_disabled_by_default(monkeypatch):
    """markdown_kb 插件应能被加载，且首次默认关闭。"""
    from fastapi import FastAPI
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-load")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    pm = PluginManager()
    fastapi_app = FastAPI()
    await pm.load_all(fastapi_app)

    plugin = pm.plugins.get("markdown_kb")
    assert plugin is not None
    assert plugin.builtin is False
    assert plugin.enabled is False
    assert plugin.meta.category == "app"

    menu = pm.get_menu()
    kb_menu = next((item for item in menu if item["name"] == "markdown_kb"), None)
    assert kb_menu is None


@pytest.mark.asyncio
async def test_markdown_kb_status_and_upload_api(monkeypatch):
    """markdown_kb 插件最小 API 应支持状态查询与批量 .md 上传保存。"""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-api")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    pm.plugins["markdown_kb"].enabled = True
    pm.plugins["markdown_kb"].config = pm.get_config("markdown_kb") or {}
    await pm.plugins["markdown_kb"].on_load()

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        status_before = await client.get("/api/markdown-kb/status")
        upload_resp = await client.post(
            "/api/markdown-kb/files/upload",
            files=[
                ("files", ("notes.md", b"# Title\n\nhello plugin\n", "text/markdown")),
                ("files", ("guide.md", b"# Guide\n\nbatch upload\n", "text/markdown")),
            ],
        )
        status_after = await client.get("/api/markdown-kb/status")
        bad_upload = await client.post(
            "/api/markdown-kb/files/upload",
            files=[("files", ("notes.txt", b"not markdown\n", "text/plain"))],
        )

    assert status_before.status_code == 200
    assert status_before.json()["doc_count"] == 0

    assert upload_resp.status_code == 200
    upload_data = upload_resp.json()
    assert upload_data["ok"] is True
    assert upload_data["count"] == 2
    assert {item["file_name"] for item in upload_data["files"]} == {"notes.md", "guide.md"}
    assert all(item["size_bytes"] > 0 for item in upload_data["files"])

    assert status_after.status_code == 200
    status_after_data = status_after.json()
    assert status_after_data["doc_count"] == 2
    assert status_after_data["docs_dir"].endswith(".akm/markdown_kb/docs")

    saved_file = test_home / ".akm" / "markdown_kb" / "docs" / "notes.md"
    assert saved_file.exists()
    assert saved_file.read_text("utf-8") == "# Title\n\nhello plugin\n"

    saved_file_2 = test_home / ".akm" / "markdown_kb" / "docs" / "guide.md"
    assert saved_file_2.exists()
    assert saved_file_2.read_text("utf-8") == "# Guide\n\nbatch upload\n"

    assert bad_upload.status_code == 400
    assert bad_upload.json()["detail"] == "仅支持 .md 文件"


@pytest.mark.asyncio
async def test_markdown_kb_learn_api_creates_workspace_bound_doc_and_dedupes(monkeypatch):
    """`/learn` 应能写入新知识文档、绑定 workspace，并在重复触发时只入库一次。"""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-learn-api")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    async def fake_post(self, url, json=None, **kwargs):
        if url.endswith("/v1/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {
                            "content": json_module.dumps({
                                "should_learn": True,
                                "title": "退款页重复提交排查",
                                "summary_markdown": "### 结论\n重复提交来自前端重复绑定提交事件。\n\n### 修复方式\n在重复挂载前先解除旧绑定，并收敛为单一提交入口。",
                                "quotes": [
                                    "已经定位到重复绑定 submit 事件。",
                                    "旧页面恢复时又注册了一次提交处理器。",
                                ],
                            }, ensure_ascii=False),
                        }
                    }]
                },
            )
        if url.endswith("/v1/embeddings"):
            inputs = json.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": idx, "embedding": [float(len(str(item or ""))), 1.0, 0.0]}
                        for idx, item in enumerate(inputs)
                    ],
                    "model": json.get("model", "text-embedding-3-small"),
                },
            )
        return httpx.Response(404, json={"detail": "unexpected url"})

    json_module = json
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    plugin = pm.plugins["markdown_kb"]
    plugin.enabled = True
    plugin.config = pm.get_config("markdown_kb") or {}
    await plugin.on_load()
    fastapi_app.state.plugin_manager = pm

    transport = ASGITransport(app=fastapi_app)
    learn_payload = {
        "source": "codex",
        "trigger_phase": "stop",
        "session_id": "sess_123",
        "turn_id": "turn_456",
        "workspace_root": "/Users/nk/Desktop/ccs",
        "title_hint": "退款页重复提交排查",
        "user_prompt": "请帮我排查退款页为什么会重复提交",
        "assistant_excerpt": "已经定位到重复绑定 submit 事件。",
        "conversation_excerpt": [
            {"role": "user", "text": "请帮我排查退款页为什么会重复提交"},
            {"role": "assistant", "text": "已经定位到重复绑定 submit 事件。"},
        ],
        "learn_keyword": "AKM入库",
        "dedupe_key": "codex:sess_123:turn_456",
    }

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first_resp = await client.request("POST", "/api/markdown-kb/learn", json=learn_payload)
        second_resp = await client.request("POST", "/api/markdown-kb/learn", json={
            **learn_payload,
            "trigger_phase": "pre_compact",
        })

    assert first_resp.status_code == 200
    first_data = first_resp.json()
    assert first_data["ok"] is True
    assert first_data["status"] == "completed"
    assert first_data["ignored"] is False
    assert first_data["deduped"] is False
    assert first_data["workspace_root"] == "/Users/nk/Desktop/ccs"
    assert first_data["file_name"].endswith(".learn.md")
    assert first_data["chunk_count"] >= 1

    assert second_resp.status_code == 200
    second_data = second_resp.json()
    assert second_data["ok"] is True
    assert second_data["deduped"] is True
    assert second_data["file_name"] == first_data["file_name"]
    assert second_data["doc_id"] == first_data["doc_id"]

    learned_entry = plugin._find_doc_entry(doc_id=first_data["doc_id"])
    learned_path = plugin._doc_storage_path(learned_entry)
    assert learned_path.exists()
    learned_text = learned_path.read_text("utf-8")
    assert "# 退款页重复提交排查" in learned_text
    assert "**知识摘要**" in learned_text
    assert "重复绑定提交事件" in learned_text
    assert "AKM入库" not in learned_text

    listed_files = plugin.list_files()
    learned_item = next(item for item in listed_files["files"] if item["file_name"] == first_data["file_name"])
    assert learned_item["indexed"] is True
    assert learned_item["workspace_root"] == "/Users/nk/Desktop/ccs"

    scoped_query = await plugin.query({
        "question": "退款页为什么会重复提交",
        "top_k": 5,
        "workspace_root": "/Users/nk/Desktop/ccs",
        "working_directory": "/Users/nk/Desktop/ccs",
    })
    assert any(item["file_name"] == first_data["file_name"] for item in scoped_query["hits"])


@pytest.mark.asyncio
async def test_markdown_kb_learn_ignored_when_model_says_no_stable_knowledge(monkeypatch):
    """当模型明确判断无稳定知识可沉淀时，`/learn` 应返回 ignored 且不落盘。"""
    from fastapi import FastAPI
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-learn-ignored")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    async def fake_post(self, url, json=None, **kwargs):
        if url.endswith("/v1/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {
                            "content": json_module.dumps({
                                "should_learn": False,
                                "title": "",
                                "summary_markdown": "",
                                "quotes": [],
                            }, ensure_ascii=False),
                        }
                    }]
                },
            )
        return httpx.Response(404, json={"detail": "unexpected url"})

    json_module = json
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    plugin = pm.plugins["markdown_kb"]
    plugin.enabled = True
    plugin.config = pm.get_config("markdown_kb") or {}
    await plugin.on_load()

    result = await plugin.learn({
        "source": "claude_code",
        "trigger_phase": "stop",
        "session_id": "sess_ignore",
        "workspace_root": "/Users/nk/Desktop/ccs",
        "title_hint": "一次普通寒暄",
        "user_prompt": "谢谢，今天先这样",
        "assistant_excerpt": "好的，随时找我。",
        "conversation_excerpt": [],
        "learn_keyword": "AKM入库",
        "dedupe_key": "claude_code:sess_ignore",
    })

    assert result["ok"] is True
    assert result["ignored"] is True
    assert result["status"] == "ignored"

    docs_dir = test_home / ".akm" / "markdown_kb" / "docs"
    assert list(docs_dir.glob("*.md")) == []

    learn_records = plugin._load_learn_records()
    assert learn_records["claude_code:sess_ignore"]["status"] == "ignored"


@pytest.mark.asyncio
async def test_plugin_host_page_keeps_admin_layout(monkeypatch):
    """插件页面应通过后台宿主页加载，保留左侧菜单而不是直接返回裸 HTML。"""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-plugin-host-layout")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    pm.plugins["markdown_kb"].enabled = True
    pm.plugins["markdown_kb"].config = pm.get_config("markdown_kb") or {}
    await pm.plugins["markdown_kb"].on_load()
    fastapi_app.state.plugin_manager = pm

    transport = ASGITransport(app=app)
    app.state.plugin_manager = pm
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        host_resp = await client.get("/plugins/markdown_kb")
        raw_resp = await client.get("/plugins/markdown_kb/raw")

    assert host_resp.status_code == 200
    assert "AKM 后台" in host_resp.text
    assert 'iframe' in host_resp.text
    assert '/plugins/markdown_kb/raw' in host_resp.text

    assert raw_resp.status_code == 200
    assert "Markdown 知识库" in raw_resp.text
    assert "Workspace 范围" in raw_resp.text
    assert "AKM 后台" not in raw_resp.text


@pytest.mark.asyncio
async def test_markdown_kb_rebuild_query_ask_and_delete(monkeypatch):
    """markdown_kb 应支持重建、检索、问答和删除后同步清理索引。"""
    from fastapi import FastAPI
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-flow")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    async def fake_post(self, url, json=None, **kwargs):
        if url.endswith("/v1/embeddings"):
            inputs = json.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]

            def vectorize(text):
                raw = str(text or "")
                length = float(len(raw))
                markdown_score = 10.0 if "Markdown" in raw else 0.0
                release_score = 8.0 if "Release" in raw else 0.0
                plugin_score = 6.0 if "plugin" in raw.lower() else 0.0
                return [length, markdown_score, release_score, plugin_score]

            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": idx, "embedding": vectorize(item)}
                        for idx, item in enumerate(inputs)
                    ],
                    "model": json.get("model", "text-embedding-3-small"),
                },
            )

        if url.endswith("/v1/rerank"):
            docs = json.get("documents", [])
            results = []
            for idx, text in enumerate(docs):
                raw = str(text or "")
                score = 0.2
                if "Rebuild" in raw or "rebuild" in raw:
                    score = 0.99
                elif "Markdown" in raw:
                    score = 0.75
                results.append({"index": idx, "relevance_score": score})
            results.sort(key=lambda item: item["relevance_score"], reverse=True)
            return httpx.Response(
                200,
                json={
                    "results": results,
                    "model": json.get("model", "rerank-v1"),
                },
            )

        if url.endswith("/v1/chat/completions"):
            messages = json.get("messages", [])
            prompt = messages[-1]["content"] if messages else ""
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": f"基于资料的回答: {prompt[:48]}"}}
                    ]
                },
            )

        return httpx.Response(404, json={"detail": "unexpected url"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    plugin = pm.plugins["markdown_kb"]
    plugin.enabled = True
    plugin.config = pm.get_config("markdown_kb") or {}
    await plugin.on_load()
    plugin.config["reranker_model"] = "rerank-v1"

    docs_dir = test_home / ".akm" / "markdown_kb" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "guide.md").write_text(
        "# Markdown Guide\n\nMarkdown plugin keeps docs local.\n\n## Query\n\nUse query to inspect chunks.\n\n## Ask\n\nUse ask to answer with citations.\n",
        "utf-8",
    )
    (docs_dir / "release.md").write_text(
        "# Release Notes\n\nRelease plugin updates carefully.\n\n## Rebuild\n\nRun rebuild after changing docs.\n",
        "utf-8",
    )

    bind_result = plugin.bind_file_workspace("guide.md", "/Users/nk/Desktop/ccs")
    assert bind_result["workspace_root"] == "/Users/nk/Desktop/ccs"

    rebuild = await plugin.rebuild_index()
    assert rebuild["ok"] is True
    assert rebuild["doc_count"] == 2
    assert rebuild["chunk_count"] >= 2

    preview_before = plugin.preview_sync()
    assert preview_before["summary"]["unchanged"] == 2
    assert preview_before["summary"]["added"] == 0

    files = plugin.list_files()
    assert files["count"] == 2
    assert all(item["indexed"] for item in files["files"])

    status = plugin.get_status()
    assert status["chunk_count"] == rebuild["chunk_count"]
    assert status["last_rebuilt_at"] is not None
    assert status["health"]["in_sync"] is True

    query_result = await plugin.query({"question": "How does Markdown plugin query work?", "top_k": 2})
    assert query_result["ok"] is True
    assert len(query_result["hits"]) == 1
    assert query_result["reranker_model"] == "rerank-v1"
    assert query_result["hits"][0]["file_name"] == "release.md"
    assert all("chunk_text" in item for item in query_result["hits"])
    assert query_result["hits"][0]["rerank_score"] is not None

    scoped_query_result = await plugin.query({
        "question": "When should I run rebuild?",
        "top_k": 5,
        "workspace_root": "",
    })
    assert all(item["file_name"] == "release.md" for item in scoped_query_result["hits"])

    workspace_scoped_query_result = await plugin.query({
        "question": "How does Markdown plugin query work?",
        "top_k": 5,
        "workspace_root": "/Users/nk/Desktop/ccs",
        "working_directory": "/Users/nk/Desktop/ccs",
    })
    assert workspace_scoped_query_result["workspace_root"] == "/Users/nk/Desktop/ccs"
    assert any(item["file_name"] == "guide.md" for item in workspace_scoped_query_result["hits"])
    assert all(item["file_name"] in {"guide.md", "release.md"} for item in workspace_scoped_query_result["hits"])

    ask_result = await plugin.ask({"question": "When should I run rebuild?", "top_k": 2})
    assert ask_result["ok"] is True
    assert ask_result["answer"].startswith("基于资料的回答:")
    assert len(ask_result["citations"]) == 1
    assert ask_result["reranker_model"] == "rerank-v1"

    workspace_scoped_ask_result = await plugin.ask({
        "question": "How does Markdown plugin query work?",
        "top_k": 5,
        "workspace_root": "/Users/nk/Desktop/ccs",
        "working_directory": "/Users/nk/Desktop/ccs",
    })
    assert workspace_scoped_ask_result["workspace_root"] == "/Users/nk/Desktop/ccs"
    assert any(item["file_name"] == "guide.md" for item in workspace_scoped_ask_result["citations"])
    assert all(item["file_name"] in {"guide.md", "release.md"} for item in workspace_scoped_ask_result["citations"])

    (docs_dir / "guide.md").write_text(
        "# Markdown Guide\n\nMarkdown plugin keeps docs local.\n\n## Query\n\nUse query to inspect chunks.\n\n## Ask\n\nUse ask to answer with citations.\n\n## Sync\n\nSync only changed files.\n",
        "utf-8",
    )

    single_rebuild = await plugin.rebuild_file("guide.md")
    assert single_rebuild["ok"] is True
    assert single_rebuild["file_name"] == "guide.md"

    preview_after_change = plugin.preview_sync()
    assert preview_after_change["summary"]["unchanged"] == 2
    assert preview_after_change["summary"]["changed"] == 0

    (docs_dir / "new.md").write_text("# New File\n\nFresh notes.\n", "utf-8")
    preview_added = plugin.preview_sync()
    assert preview_added["summary"]["added"] == 1
    health_after_add = plugin.health_check()
    assert health_after_add["in_sync"] is False
    assert health_after_add["summary"]["added"] == 1

    sync_result = await plugin.sync_index(apply_changes=True)
    assert sync_result["applied"] is True
    assert "new.md" in (sync_result["applied_changes"]["added"] or [])

    files_after_sync = plugin.list_files()
    file_names = {item["file_name"] for item in files_after_sync["files"]}
    assert "new.md" in file_names

    assert plugin.health_check()["in_sync"] is True

    delete_result = plugin.delete_file("release.md")
    assert delete_result["ok"] is True
    assert delete_result["removed_chunks"] >= 1

    files_after_delete = plugin.list_files()
    assert files_after_delete["count"] == 2
    assert {item["file_name"] for item in files_after_delete["files"]} == {"guide.md", "new.md"}

    query_after_delete = await plugin.query({"question": "Markdown plugin", "top_k": 5})
    assert all(item["file_name"] in {"guide.md", "new.md"} for item in query_after_delete["hits"])

    clear_result = plugin.clear_index(delete_docs=False)
    assert clear_result["ok"] is True
    assert clear_result["removed_docs"] == 0

    status_after_clear = plugin.get_status()
    assert status_after_clear["chunk_count"] == 0
    assert status_after_clear["doc_count"] == 2

    clear_with_docs = plugin.clear_index(delete_docs=True)
    assert clear_with_docs["ok"] is True
    assert clear_with_docs["removed_docs"] == 2

    final_status = plugin.get_status()
    assert final_status["doc_count"] == 0
    assert final_status["chunk_count"] == 0
    assert "new.md" in file_names


@pytest.mark.asyncio
async def test_markdown_kb_keyword_weight_only_affects_non_rerank_order(monkeypatch):
    """未启用 rerank 时，语义/关键词权重应参与最终排序。"""
    from fastapi import FastAPI
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-keyword-weight")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    async def fake_post(self, url, json=None, **kwargs):
        if url.endswith("/v1/embeddings"):
            inputs = json.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]

            def vectorize(text):
                raw = str(text or "")
                if raw.strip().lower() == "exactterm":
                    return [1.0, 0.0]
                if "semantically close" in raw.lower():
                    return [1.0, 0.0]
                return [0.0, 1.0]

            return httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": vectorize(item), "index": idx}
                        for idx, item in enumerate(inputs)
                    ]
                },
            )
        return httpx.Response(404, json={"detail": "unexpected url"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    plugin = pm.plugins["markdown_kb"]
    plugin.enabled = True
    plugin.config = pm.get_config("markdown_kb") or {}
    await plugin.on_load()

    docs_dir = test_home / ".akm" / "markdown_kb" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "semantic.md").write_text("# Broad Topic\n\nThis document is semantically close but misses the exact token.", "utf-8")
    (docs_dir / "keyword.md").write_text("# exactterm\n\nThis chunk contains exactterm for literal matching.", "utf-8")

    await plugin.rebuild_index()

    plugin.config["reranker_model"] = ""
    plugin.config["semantic_weight"] = 1
    plugin.config["keyword_weight"] = 0
    plugin.config["score_threshold"] = 0
    semantic_only = await plugin.query({"question": "exactterm", "top_k": 2})
    assert semantic_only["hits"][0]["file_name"] == "semantic.md"

    plugin.config["semantic_weight"] = 0
    plugin.config["keyword_weight"] = 1
    keyword_first = await plugin.query({"question": "exactterm", "top_k": 2})
    assert keyword_first["hits"][0]["file_name"] == "keyword.md"
    assert keyword_first["hits"][0]["keyword_score"] > keyword_first["hits"][1]["keyword_score"]


@pytest.mark.asyncio
async def test_markdown_kb_rerank_keeps_top_k_and_threshold_but_ignores_keyword_weight(monkeypatch):
    """启用 rerank 后，最终排序看 rerank，但第一阶段仍保留关键词粗召回。"""
    from fastapi import FastAPI
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-rerank-threshold")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    async def fake_post(self, url, json=None, **kwargs):
        if url.endswith("/v1/embeddings"):
            inputs = json.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]
            def vectorize(text):
                raw = str(text or "").lower()
                if raw.strip() == "exactterm":
                    return [1.0, 0.0]
                if "unrelated semantic background" in raw:
                    return [1.0, 0.0]
                return [0.0, 1.0]

            return httpx.Response(200, json={"data": [{"embedding": vectorize(item), "index": idx} for idx, item in enumerate(inputs)]})

        if url.endswith("/v1/rerank"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 1, "relevance_score": 0.95},
                        {"index": 0, "relevance_score": 0.72},
                    ]
                },
            )

        return httpx.Response(404, json={"detail": "unexpected url"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    plugin = pm.plugins["markdown_kb"]
    plugin.enabled = True
    plugin.config = pm.get_config("markdown_kb") or {}
    await plugin.on_load()

    docs_dir = test_home / ".akm" / "markdown_kb" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "a.md").write_text("# Alpha\n\nexactterm alpha", "utf-8")
    (docs_dir / "b.md").write_text("# Beta\n\nexactterm beta exactterm", "utf-8")
    (docs_dir / "c.md").write_text("# Gamma\n\nunrelated semantic background without keyword hit", "utf-8")

    await plugin.rebuild_index()

    plugin.config["reranker_model"] = "rerank-v1"
    plugin.config["semantic_weight"] = 0
    plugin.config["keyword_weight"] = 1
    plugin.config["score_threshold"] = 0.7

    result = await plugin.query({"question": "exactterm", "top_k": 2})
    assert len(result["hits"]) == 2
    assert [item["file_name"] for item in result["hits"]] == ["a.md", "b.md"]
    assert all(item["rerank_score"] is not None for item in result["hits"])
    assert all(item["score"] >= 0.7 for item in result["hits"])
    assert all(item["file_name"] != "c.md" for item in result["hits"])
    assert result["hits"][1]["hybrid_score"] > result["hits"][0]["hybrid_score"]

    result_top1 = await plugin.query({"question": "exactterm", "top_k": 1})
    assert len(result_top1["hits"]) == 1
    assert result_top1["hits"][0]["file_name"] == "a.md"


@pytest.mark.asyncio
async def test_markdown_kb_chinese_query_can_hit_partial_phrases_without_full_sentence_match(monkeypatch):
    """中文长句查询应能命中局部短语，而不要求整句逐字出现。"""
    from fastapi import FastAPI
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-chinese-query")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    async def fake_post(self, url, json=None, **kwargs):
        if url.endswith("/v1/embeddings"):
            inputs = json.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]

            def vectorize(text):
                raw = str(text or "")
                if raw == "参考考试大纲生成复习计划":
                    return [1.0, 0.0]
                if "考试大纲" in raw or "复习计划" in raw:
                    return [0.92, 0.08]
                return [0.0, 1.0]

            return httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": vectorize(item), "index": idx}
                        for idx, item in enumerate(inputs)
                    ]
                },
            )
        return httpx.Response(404, json={"detail": "unexpected url"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    plugin = pm.plugins["markdown_kb"]
    plugin.enabled = True
    plugin.config = pm.get_config("markdown_kb") or {}
    await plugin.on_load()

    docs_dir = test_home / ".akm" / "markdown_kb" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "plan.md").write_text("# 复习计划\n\n可以参考考试大纲制定阶段性复习计划。", "utf-8")
    (docs_dir / "other.md").write_text("# 其他主题\n\n这是一个不相关的说明。", "utf-8")

    await plugin.rebuild_index()

    plugin.config["reranker_model"] = ""
    plugin.config["semantic_weight"] = 0.3
    plugin.config["keyword_weight"] = 0.7
    plugin.config["score_threshold"] = 0

    result = await plugin.query({"question": "参考考试大纲生成复习计划", "top_k": 2})
    assert result["hits"]
    assert result["hits"][0]["file_name"] == "plan.md"
    assert result["hits"][0]["keyword_score"] > result["hits"][1]["keyword_score"]


@pytest.mark.asyncio
async def test_markdown_kb_english_query_does_not_depend_on_full_sentence_phrase_match(monkeypatch):
    """英文长句查询也应主要依赖 token 覆盖，而不是整句原样出现。"""
    from fastapi import FastAPI
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-english-query")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    async def fake_post(self, url, json=None, **kwargs):
        if url.endswith("/v1/embeddings"):
            inputs = json.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]

            def vectorize(text):
                raw = str(text or "").lower()
                if raw == "generate a study plan from the exam outline":
                    return [1.0, 0.0]
                if "exam outline" in raw or "study plan" in raw:
                    return [0.92, 0.08]
                return [0.0, 1.0]

            return httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": vectorize(item), "index": idx}
                        for idx, item in enumerate(inputs)
                    ]
                },
            )
        return httpx.Response(404, json={"detail": "unexpected url"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    plugin = pm.plugins["markdown_kb"]
    plugin.enabled = True
    plugin.config = pm.get_config("markdown_kb") or {}
    await plugin.on_load()

    docs_dir = test_home / ".akm" / "markdown_kb" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "plan.md").write_text("# Study Plan\n\nUse the exam outline to prepare a phased study plan.", "utf-8")
    (docs_dir / "other.md").write_text("# Other Topic\n\nThis document is unrelated.", "utf-8")

    await plugin.rebuild_index()

    plugin.config["reranker_model"] = ""
    plugin.config["semantic_weight"] = 0.3
    plugin.config["keyword_weight"] = 0.7
    plugin.config["score_threshold"] = 0

    result = await plugin.query({"question": "generate a study plan from the exam outline", "top_k": 2})
    assert result["hits"]
    assert result["hits"][0]["file_name"] == "plan.md"
    assert result["hits"][0]["keyword_score"] > result["hits"][1]["keyword_score"]


@pytest.mark.asyncio
async def test_markdown_kb_query_falls_back_when_index_contains_mixed_embedding_dimensions(monkeypatch):
    """索引里混有不同维度 embedding 时，query 不应直接 500。"""
    from fastapi import FastAPI
    from akm.plugins.plugin_manager import PluginManager

    test_home = Path("/var/folders/ks/s1958s1x2cqfypj2y2_808rm0000gn/T/opencode/test-home-markdown-kb-mixed-embeddings")
    if test_home.exists():
        shutil.rmtree(test_home)
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: test_home))

    async def fake_post(self, url, json=None, **kwargs):
        if url.endswith("/v1/embeddings"):
            inputs = json.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": [1.0, 0.0], "index": idx}
                        for idx, item in enumerate(inputs)
                    ]
                },
            )
        return httpx.Response(404, json={"detail": "unexpected url"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    fastapi_app = FastAPI()
    pm = PluginManager()
    await pm.load_all(fastapi_app)
    plugin = pm.plugins["markdown_kb"]
    plugin.enabled = True
    plugin.config = pm.get_config("markdown_kb") or {}
    await plugin.on_load()

    docs_dir = test_home / ".akm" / "markdown_kb" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "a.md").write_text("# Alpha\n\nalpha topic", "utf-8")
    (docs_dir / "b.md").write_text("# Beta\n\nbeta topic", "utf-8")

    await plugin.rebuild_index()

    snapshot = plugin._load_index_data()
    snapshot["documents"][0]["embedding"] = [1.0, 0.0, 0.0]
    plugin._save_index_data(snapshot)
    plugin._invalidate_embedding_cache()

    plugin.config["reranker_model"] = ""
    plugin.config["score_threshold"] = 0
    result = await plugin.query({"question": "alpha", "top_k": 2})
    assert result["ok"] is True
    assert len(result["hits"]) >= 1


def test_markdown_kb_tokenize_keywords_prefers_jieba3_small_for_cjk_segments(monkeypatch):
    """中文分词在 jieba3 small 可用时应优先使用自然词粒度。"""
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    plugin.logger = logging.getLogger("test.markdown_kb.jieba3")
    plugin._jieba3_warned_unavailable = False
    plugin._jieba3_small_tokenizer = None

    class DummyJieba3Tokenizer:
        def __init__(self, model="base"):
            assert model == "small"

        def cut_text(self, text):
            assert text == "参考考试大纲生成复习计划"
            return ["参考", "考试大纲", "生成", "复习计划"]

    monkeypatch.setattr("plugins.markdown_kb.index.Jieba3Tokenizer", DummyJieba3Tokenizer)

    tokens = plugin._tokenize_keywords("参考考试大纲生成复习计划")
    assert "参考考试大纲生成复习计划" in tokens
    assert "考试大纲" in tokens
    assert "复习计划" in tokens
    assert "生成" in tokens
    assert "考试" not in tokens


def test_markdown_kb_tokenize_keywords_falls_back_to_sliding_windows_when_jieba3_unavailable(monkeypatch):
    """当 jieba3 不可用时，应继续使用原有 2~4 字滑窗分词。"""
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    plugin.logger = logging.getLogger("test.markdown_kb.jieba3.fallback")
    plugin._jieba3_warned_unavailable = False
    plugin._jieba3_small_tokenizer = None

    monkeypatch.setattr("plugins.markdown_kb.index.Jieba3Tokenizer", None)

    tokens = plugin._tokenize_keywords("参考考试大纲生成复习计划")
    assert "参考考试大纲生成复习计划" in tokens
    assert "考试大纲" in tokens
    assert "复习计划" in tokens
    assert "考试" in tokens
    assert "复习" in tokens


@pytest.mark.asyncio
async def test_markdown_kb_query_prefers_sqlite_vec_candidates_when_available(monkeypatch):
    """当 sqlite-vec 可用时，第一阶段候选应优先来自 store 侧向量召回。"""
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    plugin.name = "markdown_kb"
    plugin.logger = logging.getLogger("test.markdown_kb.sqlite_vec")
    plugin.config = {
        "embedding_model": "text-embedding-3-small",
        "reranker_model": "",
        "top_k": 2,
        "semantic_weight": 1,
        "keyword_weight": 0,
        "score_threshold": 0,
    }
    plugin._ensure_runtime_ready = lambda: None
    plugin._query_embedding_cache = OrderedDict()
    plugin._query_result_cache = OrderedDict()
    plugin._embedding_cache_revision = None
    plugin._embedding_cache_documents = []
    plugin._bm25_cache_revision = None
    plugin._bm25_cache_documents = []
    plugin._bm25_cache_stats = None

    class FakeStore:
        def stats(self):
            return {
                "document_count": 3,
                "last_rebuilt_at": "2026-06-22T00:00:00+00:00",
                "vec_enabled": True,
            }

        def list_documents_by_scope(self, workspace_root="", selected_doc=None):
            return [
                {"id": "a", "doc_id": "d1", "file_name": "a.md", "workspace_root": "", "title": "A", "chunk_index": 0, "chunk_text": "A", "content_hash": "a", "embedding": [0.0, 1.0]},
                {"id": "b", "doc_id": "d2", "file_name": "b.md", "workspace_root": "", "title": "B", "chunk_index": 0, "chunk_text": "B", "content_hash": "b", "embedding": [1.0, 0.0]},
                {"id": "c", "doc_id": "d3", "file_name": "c.md", "workspace_root": "", "title": "C", "chunk_index": 0, "chunk_text": "C", "content_hash": "c", "embedding": [0.0, 1.0]},
            ]

        def search_by_vector(self, query_vector, limit, workspace_root="", selected_doc=None):
            assert query_vector == [1.0, 0.0]
            assert limit >= 2
            return [
                {"id": "b", "distance": 0.0},
                {"id": "a", "distance": 0.8},
            ]

        def get_documents_by_chunk_ids(self, chunk_ids):
            assert chunk_ids == ["b", "a"]
            mapping = {
                "a": {"id": "a", "doc_id": "d1", "file_name": "a.md", "workspace_root": "", "title": "A", "chunk_index": 0, "chunk_text": "A", "content_hash": "a", "embedding": [0.0, 1.0]},
                "b": {"id": "b", "doc_id": "d2", "file_name": "b.md", "workspace_root": "", "title": "B", "chunk_index": 0, "chunk_text": "B", "content_hash": "b", "embedding": [1.0, 0.0]},
            }
            return [mapping[item] for item in chunk_ids]

        def read_memory_map(self, chunk_ids):
            return {}

        def update_memory(self, upserts, cap=1.0):
            pass

        def cleanup_expired_memory(self):
            return 0

    plugin._store = FakeStore()

    async def fake_get_query_embedding(question, embedding_model):
        assert question == "query"
        assert embedding_model == "text-embedding-3-small"
        return [1.0, 0.0]

    monkeypatch.setattr(plugin, "_get_query_embedding", fake_get_query_embedding)
    monkeypatch.setattr(plugin, "_load_documents_for_retrieval", lambda: pytest.fail("sqlite-vec 可用时不应回退全量向量加载"))

    result = await plugin._retrieve("query", 2, "text-embedding-3-small", "", {}, {})
    assert [item["file_name"] for item in result] == ["b.md", "a.md"]
    assert result[0]["vector_score"] > result[1]["vector_score"]


@pytest.mark.asyncio
async def test_markdown_kb_query_passes_workspace_scope_into_sqlite_vec_search(monkeypatch):
    """sqlite-vec 召回应在 SQL 层拿到 workspace 过滤条件。"""
    from plugins.markdown_kb.index import Plugin

    plugin = Plugin()
    plugin.name = "markdown_kb"
    plugin.logger = logging.getLogger("test.markdown_kb.sqlite_vec.workspace")
    plugin.config = {
        "embedding_model": "text-embedding-3-small",
        "reranker_model": "",
        "top_k": 2,
        "semantic_weight": 1,
        "keyword_weight": 0,
        "score_threshold": 0,
    }
    plugin._ensure_runtime_ready = lambda: None
    plugin._query_embedding_cache = OrderedDict()
    plugin._query_result_cache = OrderedDict()
    plugin._embedding_cache_revision = None
    plugin._embedding_cache_documents = []
    plugin._bm25_cache_revision = None
    plugin._bm25_cache_documents = []
    plugin._bm25_cache_stats = None

    captured = {}

    class FakeStore:
        def stats(self):
            return {
                "document_count": 2,
                "last_rebuilt_at": "2026-06-22T00:00:00+00:00",
                "vec_enabled": True,
            }

        def list_documents_by_scope(self, workspace_root="", selected_doc=None):
            captured["scope_workspace"] = workspace_root
            captured["scope_selected_doc"] = dict(selected_doc or {})
            return [
                {"id": "public", "doc_id": "d1", "file_name": "public.md", "workspace_root": "", "title": "Public", "chunk_index": 0, "chunk_text": "public", "content_hash": "p", "embedding": [1.0, 0.0]},
                {"id": "ws", "doc_id": "d2", "file_name": "workspace.md", "workspace_root": "/Users/nk/Desktop/ccs", "title": "Workspace", "chunk_index": 0, "chunk_text": "workspace", "content_hash": "w", "embedding": [1.0, 0.0]},
            ]

        def search_by_vector(self, query_vector, limit, workspace_root="", selected_doc=None):
            captured["search_workspace"] = workspace_root
            captured["search_selected_doc"] = dict(selected_doc or {})
            return [
                {"id": "ws", "distance": 0.0},
                {"id": "public", "distance": 0.2},
            ]

        def get_documents_by_chunk_ids(self, chunk_ids):
            mapping = {
                "public": {"id": "public", "doc_id": "d1", "file_name": "public.md", "workspace_root": "", "title": "Public", "chunk_index": 0, "chunk_text": "public", "content_hash": "p", "embedding": [1.0, 0.0]},
                "ws": {"id": "ws", "doc_id": "d2", "file_name": "workspace.md", "workspace_root": "/Users/nk/Desktop/ccs", "title": "Workspace", "chunk_index": 0, "chunk_text": "workspace", "content_hash": "w", "embedding": [1.0, 0.0]},
            }
            return [mapping[item] for item in chunk_ids]

        def read_memory_map(self, chunk_ids):
            return {}

        def update_memory(self, upserts, cap=1.0):
            pass

        def cleanup_expired_memory(self):
            return 0

    plugin._store = FakeStore()

    async def fake_get_query_embedding(question, embedding_model):
        return [1.0, 0.0]

    monkeypatch.setattr(plugin, "_get_query_embedding", fake_get_query_embedding)
    monkeypatch.setattr(plugin, "_load_documents_for_retrieval", lambda: pytest.fail("sqlite-vec scope 路径不应回退全量加载"))

    result = await plugin._retrieve(
        "query",
        2,
        "text-embedding-3-small",
        "",
        {
            "workspace_root": "/Users/nk/Desktop/ccs",
            "working_directory": "/Users/nk/Desktop/ccs",
            "project_name": "ccs",
            "project_id": "/Users/nk/Desktop/ccs",
        },
        {},
    )
    assert [item["file_name"] for item in result] == ["workspace.md", "public.md"]
    assert captured["scope_workspace"] == "/Users/nk/Desktop/ccs"
    assert captured["search_workspace"] == "/Users/nk/Desktop/ccs"


def test_sqlite_kb_index_store_marks_vec_enabled_when_runtime_and_dimensions_are_ready(monkeypatch, tmp_path):
    """store 在运行时可用且 embedding 维度一致时，应把 vec 状态写入元数据。"""
    from plugins.markdown_kb.index import SqliteKbIndexStore

    class DummySqliteVec:
        @staticmethod
        def load(conn):
            conn.execute("SELECT 1")

    monkeypatch.setattr(SqliteKbIndexStore, "_import_sqlite_vec_optional", lambda self: DummySqliteVec)

    def fake_ensure(self, conn):
        self._vec_runtime_available = True
        self._vec_version = "test-vec"
        return True

    def fake_create(self, conn, embedding_dim):
        conn.execute(f"CREATE TABLE {self._vec_table_name}(chunk_id TEXT PRIMARY KEY, embedding TEXT NOT NULL)")

    monkeypatch.setattr(SqliteKbIndexStore, "_ensure_vec_runtime", fake_ensure)
    monkeypatch.setattr(SqliteKbIndexStore, "_create_vec_table", fake_create)

    store = SqliteKbIndexStore(tmp_path / "kb.db", logging.getLogger("test.sqlite_store.vec"))
    snapshot = store.replace_all(
        [
            {
                "id": "a",
                "doc_id": "d1",
                "file_name": "a.md",
                "file_path": "/tmp/a.md",
                "workspace_root": "",
                "title": "A",
                "heading_level": 1,
                "chunk_index": 0,
                "chunk_text": "alpha",
                "content_hash": "h1",
                "created_at": "2026-06-22T00:00:00+00:00",
                "updated_at": "2026-06-22T00:00:00+00:00",
                "indexed_at": "2026-06-22T00:00:00+00:00",
                "embedding": [1.0, 0.0, 0.0],
            }
        ],
        {
            "last_rebuilt_at": "2026-06-22T00:00:00+00:00",
            "embedding_model": "text-embedding-small",
        },
    )
    assert snapshot["documents"][0]["file_name"] == "a.md"
    stats = store.stats()
    assert stats["vec_available"] is True
    assert stats["vec_ready"] is True
    assert stats["vec_enabled"] is True
    assert stats["vec_version"] == "test-vec"
    assert stats["embedding_dim"] == 3


@pytest.mark.asyncio
async def test_api_logs_keeps_security_headers_for_frontend():
    monkeypatch = pytest.MonkeyPatch()
    async def fake_list_logs_async(**kwargs):
        return [{
        "request_headers": '{"x-akm-security":"warn:(?i)curl.*bash","x-akm-flags":"security_response_warned"}',
        "response_body": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }]

    async def fake_count_logs_async(**kwargs):
        return 1

    monkeypatch.setattr("akm.server.list_logs_async", fake_list_logs_async)
    monkeypatch.setattr("akm.server.count_logs_async", fake_count_logs_async)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/logs")

    monkeypatch.undo()
    assert resp.status_code == 200
    data = resp.json()
    row = data["data"][0]
    assert "x-akm-security" in row["request_headers"]
    assert "security_response_warned" in row["request_headers"]


@pytest.mark.asyncio
async def test_api_logs_normalizes_messages_usage_before_render(monkeypatch):
    async def fake_list_logs_async(**kwargs):
        return [{
            "provider": "anthropic",
            "request_headers": '{}',
            "response_body": '{"usage":{"input_tokens":12121,"cache_read_input_tokens":21632,"output_tokens":159,"service_tier":"standard"}}',
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "cache_creation_tokens": 0,
        }]

    async def fake_count_logs_async(**kwargs):
        return 1

    monkeypatch.setattr("akm.server.list_logs_async", fake_list_logs_async)
    monkeypatch.setattr("akm.server.count_logs_async", fake_count_logs_async)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/logs")

    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["prompt_tokens"] == 33753
    assert row["completion_tokens"] == 159
    assert row["cached_tokens"] == 21632
    assert row["net_prompt_tokens"] == 12121
    assert row["total_tokens"] == 33912
