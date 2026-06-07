import json
from pathlib import Path

from scripts import mcp_image_server


def test_dispatch_tools_list_returns_generate_and_edit_tools():
    result = mcp_image_server.asyncio.run(mcp_image_server._dispatch("tools/list", {}))

    tool_names = [item["name"] for item in result["tools"]]
    assert tool_names == ["generate_image", "edit_image"]


def test_dispatch_generate_image_returns_text_content(monkeypatch):
    async def fake_generate(payload, timeout=120.0):
        assert payload == {
            "prompt": "a cat astronaut",
            "model": "gpt-image-2",
            "n": 2,
        }
        assert timeout == 30.0
        return {"data": [{"url": "https://example.com/cat.png"}]}

    monkeypatch.setattr(mcp_image_server, "_generate_image_via_local_service", fake_generate)

    result = mcp_image_server.asyncio.run(
        mcp_image_server._dispatch(
            "tools/call",
            {
                "name": "generate_image",
                "arguments": {
                    "prompt": "a cat astronaut",
                    "model": "gpt-image-2",
                    "n": 2,
                    "timeout": 30,
                },
            },
        )
    )

    assert json.loads(result["content"][0]["text"])["data"][0]["url"] == "https://example.com/cat.png"


def test_dispatch_edit_image_reads_local_files(monkeypatch, tmp_path):
    image_path = tmp_path / "cat.png"
    mask_path = tmp_path / "mask.png"
    image_path.write_bytes(b"cat")
    mask_path.write_bytes(b"mask")

    async def fake_edit(form_data, file_specs, timeout=120.0):
        assert form_data == {
            "prompt": "remove background",
            "n": "1",
        }
        assert file_specs[0][0] == "image"
        assert file_specs[0][1][0] == "cat.png"
        assert file_specs[0][1][1] == b"cat"
        assert file_specs[1][0] == "mask"
        assert file_specs[1][1][0] == "mask.png"
        assert file_specs[1][1][1] == b"mask"
        assert timeout == 20.0
        return {"data": [{"url": "https://example.com/edited.png"}]}

    monkeypatch.setattr(mcp_image_server, "_edit_image_via_local_service", fake_edit)

    result = mcp_image_server.asyncio.run(
        mcp_image_server._dispatch(
            "tools/call",
            {
                "name": "edit_image",
                "arguments": {
                    "image_path": str(image_path),
                    "mask": str(mask_path),
                    "prompt": "remove background",
                    "n": 1,
                    "timeout": 20,
                },
            },
        )
    )

    assert json.loads(result["content"][0]["text"])["data"][0]["url"] == "https://example.com/edited.png"
