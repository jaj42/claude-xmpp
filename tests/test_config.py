from pathlib import Path

import pytest

from claude_xmpp_agent.config import ConfigError, load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"

MINIMAL = """
[xmpp]
jid = "bot@example.org/agent"
password = "secret"
room = "Room@Conference.Example.org"

[access]
allowed_jids = ["Alice@Example.org/phone", "bob@example.org"]
"""


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_load_minimal_with_defaults(tmp_path):
    cfg = load_config(write(tmp_path, MINIMAL), env={})
    assert cfg.xmpp.jid == "bot@example.org/agent"
    assert cfg.xmpp.password == "secret"
    assert cfg.xmpp.room == "room@conference.example.org"
    assert cfg.xmpp.nick == "claude"
    assert cfg.xmpp.room_password is None
    assert cfg.access.allowed_jids == {"alice@example.org", "bob@example.org"}
    assert cfg.claude.cwd == tmp_path.resolve()
    assert cfg.claude.model is None
    assert cfg.claude.permission_mode == "default"
    assert cfg.claude.allowed_tools == ()
    assert cfg.claude.setting_sources == ("user", "project", "local")
    assert cfg.claude.allow_bypass is False
    assert cfg.ui.verbosity == "normal"
    assert cfg.ui.max_chunk == 3000
    assert cfg.ui.approval_timeout is None


def test_load_full(tmp_path):
    (tmp_path / "proj").mkdir()
    text = MINIMAL + """
[claude]
cwd = "proj"
model = "sonnet"
permission_mode = "bypassPermissions"
allow_bypass = true
allowed_tools = ["Read", "Grep"]
setting_sources = ["project"]

[ui]
verbosity = "verbose"
max_chunk = 1000
approval_timeout = 60
"""
    cfg = load_config(write(tmp_path, text), env={})
    assert cfg.claude.cwd == (tmp_path / "proj").resolve()
    assert cfg.claude.model == "sonnet"
    assert cfg.claude.permission_mode == "bypassPermissions"
    assert cfg.claude.allowed_tools == ("Read", "Grep")
    assert cfg.claude.setting_sources == ("project",)
    assert cfg.ui.verbosity == "verbose"
    assert cfg.ui.max_chunk == 1000
    assert cfg.ui.approval_timeout == 60.0


def test_example_config_loads(tmp_path):
    cfg = load_config(EXAMPLE, env={"XMPP_PASSWORD": "pw"})
    assert cfg.xmpp.password == "pw"
    assert cfg.access.allowed_jids == {"alice@example.org"}


def test_password_env_fallback(tmp_path):
    text = MINIMAL.replace('password = "secret"\n', "")
    cfg = load_config(write(tmp_path, text), env={"XMPP_PASSWORD": "from-env"})
    assert cfg.xmpp.password == "from-env"


def test_config_password_wins_over_env(tmp_path):
    cfg = load_config(write(tmp_path, MINIMAL), env={"XMPP_PASSWORD": "from-env"})
    assert cfg.xmpp.password == "secret"


def test_missing_password(tmp_path):
    text = MINIMAL.replace('password = "secret"\n', "")
    with pytest.raises(ConfigError, match="password"):
        load_config(write(tmp_path, text), env={})


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml", env={})


def test_bad_toml(tmp_path):
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(write(tmp_path, "[xmpp\n"), env={})


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ('room = "Room@Conference.Example.org"', 'room = ""', "room must not be empty"),
        ('room = "Room@Conference.Example.org"', 'room = "conference.example.org"', "room JID"),
        ('room = "Room@Conference.Example.org"', 'room = "a@b@c"', "invalid JID"),
        ('jid = "bot@example.org/agent"', 'jid = ""', "jid is required"),
        ('["Alice@Example.org/phone", "bob@example.org"]', "[]", "allowed_jids must not be empty"),
        ('["Alice@Example.org/phone", "bob@example.org"]', '["@bad"]', "invalid JID"),
        ('["Alice@Example.org/phone", "bob@example.org"]', '"alice@example.org"', "must be a list"),
        ('password = "secret"', 'password = "secret"\nnickname = "x"', "unknown key"),
    ],
)
def test_validation_errors(tmp_path, old, new, match):
    assert old in MINIMAL
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, MINIMAL.replace(old, new, 1)), env={})


def test_missing_access_section(tmp_path):
    text = MINIMAL.split("[access]")[0]
    with pytest.raises(ConfigError, match="allowed_jids must not be empty"):
        load_config(write(tmp_path, text), env={})


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ('[claude]\npermission_mode = "yolo"', "permission_mode"),
        ('[claude]\npermission_mode = "bypassPermissions"', "allow_bypass"),
        ('[claude]\nsetting_sources = ["global"]', "setting_sources"),
        ('[claude]\ncwd = "does-not-exist"', "not a directory"),
        ('[claude]\nallowed_tools = "Read"', "list of strings"),
        ('[ui]\nverbosity = "loud"', "verbosity"),
        ("[ui]\nmax_chunk = 10", "max_chunk"),
        ("[ui]\napproval_timeout = 0", "approval_timeout"),
    ],
)
def test_claude_and_ui_validation(tmp_path, extra, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, MINIMAL + "\n" + extra + "\n"), env={})


# -- [mcp_servers] ---------------------------------------------------------------

MCP = """
[mcp_servers.sfar]
url = "http://host.example.org:1234/mcp"
headers = { Authorization = "Bearer ${SFAR_TOKEN}" }

[mcp_servers.files]
command = "npx"
args = ["-y", "server-files", "${HOME}/docs"]
env = { LOG = "quiet" }
"""


def test_mcp_servers_default_empty(tmp_path):
    assert load_config(write(tmp_path, MINIMAL), env={}).mcp_servers == {}


def test_mcp_servers_parsed_and_expanded(tmp_path):
    cfg = load_config(write(tmp_path, MINIMAL + MCP), env={"SFAR_TOKEN": "t0k", "HOME": "/h"})
    assert cfg.mcp_servers == {
        "sfar": {
            "type": "http",
            "url": "http://host.example.org:1234/mcp",
            "headers": {"Authorization": "Bearer t0k"},
        },
        "files": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "server-files", "/h/docs"],
            "env": {"LOG": "quiet"},
        },
    }


def test_mcp_sse_type(tmp_path):
    text = MINIMAL + '[mcp_servers.s]\ntype = "sse"\nurl = "http://x/sse"\n'
    assert load_config(write(tmp_path, text), env={}).mcp_servers["s"] == {"type": "sse", "url": "http://x/sse"}


def test_mcp_unset_env_var(tmp_path):
    with pytest.raises(ConfigError, match=r"headers.Authorization: \$\{SFAR_TOKEN\} is not set"):
        load_config(write(tmp_path, MINIMAL + MCP), env={"HOME": "/h"})


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ('[mcp_servers.s]\ntype = "ws"\nurl = "x"', "type must be one of"),
        ("[mcp_servers.s]\nargs = []", "command is required"),
        ('[mcp_servers.s]\nurl = "x"\ncommand = "y"', "unknown key"),
        ('[mcp_servers.s]\nurl = "x"\nheaders = { A = 1 }', "table of strings"),
        ('[mcp_servers."a b"]\nurl = "x"', "name may only contain"),
        ('[mcp_servers]\ns = "x"', "must be a table"),
    ],
)
def test_mcp_validation(tmp_path, extra, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, MINIMAL + "\n" + extra + "\n"), env={})
