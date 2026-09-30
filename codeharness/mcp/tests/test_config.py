"""Unit tests for codeharness.mcp.config — loading, expansion, validation."""

import json

import pytest

from codeharness.mcp.config import MCPConfigError, load_config, validate_config


def _write(tmp_path, data) -> str:
    p = tmp_path / "mcp_servers.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_stdio_config_defaults_and_env_passthrough(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    path = _write(tmp_path, {
        "github": {
            "transport": "stdio",
            "command": "docker",
            "args": ["run", "-i", "--rm"],
            "env": {"GITHUB_PAT": "${MY_TOKEN}"},
        }
    })
    configs = load_config(path)
    cfg = configs["github"]
    assert cfg.name == "github"
    assert cfg.command == "docker"
    assert cfg.args == ("run", "-i", "--rm")
    # expanded from host env at load time; only explicit vars are carried
    assert cfg.env == {"GITHUB_PAT": "s3cret"}
    assert cfg.connect_timeout == 30.0 and cfg.call_timeout == 60.0
    assert cfg.enabled is True


def test_unresolved_var_fails_fast(tmp_path, monkeypatch):
    monkeypatch.delenv("DEFINITELY_NOT_SET_XYZ", raising=False)
    path = _write(tmp_path, {
        "s": {
            "transport": "stdio",
            "command": "x",
            "env": {"K": "${DEFINITELY_NOT_SET_XYZ}"},
        }
    })
    with pytest.raises(MCPConfigError, match="DEFINITELY_NOT_SET_XYZ"):
        load_config(path)


def test_workspace_expansion_in_args(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", "/tmp/ws")
    path = _write(tmp_path, {
        "filesystem": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "${WORKSPACE}"],
        }
    })
    cfg = load_config(path)["filesystem"]
    assert cfg.args[-1] == "/tmp/ws"


def test_streamable_http_loads_but_url_required(tmp_path):
    path = _write(tmp_path, {
        "remote": {"transport": "streamable_http", "url": "https://x.invalid/mcp"},
        "broken": {"transport": "streamable_http"},
    })
    with pytest.raises(MCPConfigError, match="broken"):
        load_config(path)

    path = _write(tmp_path, {
        "remote": {"transport": "streamable_http", "url": "https://x.invalid/mcp"},
    })
    cfg = load_config(path)["remote"]
    assert cfg.transport == "streamable_http" and cfg.url == "https://x.invalid/mcp"


@pytest.mark.parametrize("raw,match", [
    ({"transport": "carrier-pigeon"}, "transport"),
    ({"transport": "stdio"}, "command"),                    # stdio without command
    ({"transport": "stdio", "command": "x", "args": "oops"}, "args"),
    ({"transport": "stdio", "command": "x", "env": {"A": 1}}, "env"),
    ({"transport": "stdio", "command": "x", "call_timeout": -2}, "call_timeout"),
    ({"transport": "stdio", "command": "x", "connect_timeout": True}, "connect_timeout"),
    ("not-an-object", "JSON object"),
])
def test_invalid_entries_raise_with_server_name(raw, match):
    with pytest.raises(MCPConfigError) as ei:
        validate_config("srv", raw)
    assert "srv" in str(ei.value)
    assert match in str(ei.value)


def test_disabled_entry_still_validated(tmp_path):
    path = _write(tmp_path, {
        "off": {"transport": "stdio", "command": "x", "enabled": False},
    })
    cfg = load_config(path)["off"]
    assert cfg.enabled is False


def test_disabled_entry_skips_var_expansion(tmp_path, monkeypatch):
    # A disabled server may reference secrets that are not configured yet;
    # that must not break loading of the enabled servers.
    monkeypatch.delenv("NOT_CONFIGURED_PAT", raising=False)
    monkeypatch.setenv("WORKSPACE", "/tmp/ws")
    path = _write(tmp_path, {
        "github": {
            "transport": "stdio",
            "command": "docker",
            "env": {"PAT": "${NOT_CONFIGURED_PAT}"},
            "enabled": False,
        },
        "filesystem": {
            "transport": "stdio",
            "command": "npx",
            "args": ["${WORKSPACE}"],
        },
    })
    configs = load_config(path)
    assert configs["github"].env == {"PAT": "${NOT_CONFIGURED_PAT}"}  # unexpanded
    assert configs["filesystem"].args == ("/tmp/ws",)


def test_missing_file_and_bad_json(tmp_path):
    with pytest.raises(MCPConfigError, match="not found"):
        load_config(tmp_path / "nope.json")
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(MCPConfigError, match="invalid JSON"):
        load_config(p)
