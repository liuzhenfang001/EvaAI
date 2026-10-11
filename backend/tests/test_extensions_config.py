"""`extensions_config.json` 的契约：MCP server 声明怎么解析。

这份文件是**第一次给 extensions 层立契约**——`load_extensions()` 从 T4 起就在读它，但一直没有
消费者，ADR-0012 当年还把「不碰它」写成了决定。这两条断言因此值得单独钉住：一个坏 server 只被
跳过（不拖垮整份文件），一个拼错的键则**必须炸**（不能被静默忽略）。
"""

from __future__ import annotations

import pytest

from evaharness.config.extensions_config import (
    ExtensionsConfig,
    McpServerConfig,
    parse_mcp_servers,
    resolve_stdio_command,
)


class TestTransportInference:
    def test_stdio_is_inferred_from_command(self):
        assert McpServerConfig(command="npx.cmd", args=["-y", "pkg"]).transport == "stdio"

    def test_http_is_inferred_from_url(self):
        assert McpServerConfig(url="https://example.com/mcp").transport == "http"

    def test_an_explicit_transport_wins_over_inference(self):
        config = McpServerConfig(transport="http", url="https://example.com/mcp")
        assert config.transport == "http"


class TestValidation:
    def test_stdio_without_command_is_rejected(self):
        with pytest.raises(ValueError, match="command"):
            McpServerConfig(transport="stdio")

    def test_http_without_url_is_rejected(self):
        with pytest.raises(ValueError, match="url"):
            McpServerConfig(transport="http")

    def test_a_server_with_neither_command_nor_url_is_rejected(self):
        with pytest.raises(ValueError, match="command|url"):
            McpServerConfig(args=["x"])

    def test_an_unsupported_transport_is_rejected(self):
        """`sse` 是 langchain.mcp 支持的 transport，但我们的范围里没有它（决策 #6）。"""
        with pytest.raises(ValueError, match="sse"):
            McpServerConfig(transport="sse", url="https://example.com/mcp")

    def test_a_misspelled_key_is_rejected_not_swallowed(self):
        """`extra="forbid"` 的理由：这里没有下游构造器接住未知键。

        `comand` 若被静默忽略，用户看到的是「配了 server 却一个工具都没有」。
        """
        with pytest.raises(ValueError, match="comand"):
            McpServerConfig(comand="npx.cmd")

    def test_timeout_is_positive_by_default(self):
        assert McpServerConfig(command="npx.cmd").timeout > 0

    def test_a_server_is_enabled_by_default(self):
        """缺省开——「加一段配置」的默认意图就是要用它。"""
        assert McpServerConfig(command="npx.cmd").enabled is True

    def test_the_switch_is_a_declared_field_not_a_typo(self):
        """`shared` 这种捏造出来的键照样被拒——开关是**声明出来的**字段，不是放开了未知键。"""
        assert McpServerConfig(command="npx.cmd", enabled=False).enabled is False
        with pytest.raises(ValueError, match="enable"):
            McpServerConfig(command="npx.cmd", enable=False)


class TestExtensionsFile:
    def test_servers_come_from_the_camel_case_key(self):
        config = ExtensionsConfig.model_validate({"mcpServers": {"fs": {"command": "npx.cmd"}}})
        assert set(config.mcp_servers) == {"fs"}
        assert config.mcp_servers["fs"].command == "npx.cmd"

    def test_the_other_two_sections_are_declared_but_untouched(self):
        """`skills` / `middlewares` 是占位——本片只落地 `mcpServers`。"""
        config = ExtensionsConfig.model_validate(
            {"mcpServers": {}, "skills": {"a": {}}, "middlewares": []}
        )
        assert config.mcp_servers == {}

    def test_an_unknown_top_level_key_is_rejected(self):
        """`mcpServer`（少个 s）若被吞掉，症状同样是「配了却没有任何工具」。"""
        with pytest.raises(ValueError, match="mcpServer"):
            ExtensionsConfig.model_validate({"mcpServer": {}})

    def test_an_empty_file_is_valid(self):
        assert ExtensionsConfig.model_validate({}).mcp_servers == {}


class TestParseServers:
    def test_a_broken_server_is_skipped_and_the_others_survive(self, caplog):
        servers = parse_mcp_servers(
            {"good": {"command": "npx.cmd"}, "bad": {"transport": "http"}, "also-good": {"url": "https://x/mcp"}}
        )
        assert set(servers) == {"good", "also-good"}
        assert "bad" in caplog.text

    def test_a_non_mapping_entry_is_skipped(self, caplog):
        servers = parse_mcp_servers({"nope": "just a string"})
        assert servers == {}
        assert "nope" in caplog.text

    def test_the_skip_survives_a_full_model_validation(self, caplog):
        config = ExtensionsConfig.model_validate({"mcpServers": {"good": {"command": "x"}, "bad": {}}})
        assert set(config.mcp_servers) == {"good"}
        assert "bad" in caplog.text


class TestResolveStdioCommand:
    def test_an_absolute_executable_is_left_alone(self, tmp_path):
        exe = tmp_path / "tool.exe"
        exe.write_text("", encoding="utf-8")
        assert resolve_stdio_command(str(exe)) == str(exe)

    def test_a_relative_path_is_left_alone(self):
        assert resolve_stdio_command("./scripts/server.py") == "./scripts/server.py"

    def test_a_bare_name_that_is_not_on_path_is_left_alone(self):
        """`which` 找不到就原样返回——让 spawn 自己失败，而不是我们编一个路径。"""
        assert resolve_stdio_command("definitely-not-a-real-command-xyz") == (
            "definitely-not-a-real-command-xyz"
        )

    @pytest.mark.skipif(__import__("os").name != "nt", reason="Windows 专有的 193 陷阱")
    def test_the_windows_extensionless_script_trap_is_resolved(self, monkeypatch):
        """实测：`shutil.which("npx")` 返回的是无扩展名的 sh 脚本，CreateProcess 报 WinError 193。

        同一个目录里的 `npx.cmd` 才是能执行的——所以扩展名候选表要按 .cmd/.bat/.exe 试。
        """
        import shutil as _shutil

        monkeypatch.setattr(_shutil, "which", lambda name: r"C:\fake\dir\npx")
        monkeypatch.setattr(
            "evaharness.config.extensions_config.os.path.isfile",
            lambda path: path.lower() == r"c:\fake\dir\npx.cmd",
        )
        assert resolve_stdio_command("npx") == r"C:\fake\dir\npx.cmd"
