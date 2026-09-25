"""The plugin's version, stated three times, must agree.

- plugin.json's `version` is what Claude Code compares to decide whether an
  installed copy is out of date. A new commit without a bump never reaches
  anyone already installed.
- marketplace.json lists the same version for the catalog.
- .mcp.json sends it to the Gateway as X-Whales-Plugin-Version, which is how
  the Gateway tells a designer on an old plugin to update (whales-mcp-gateway
  plugin_nudge.py). A bump that forgot this header would report the old
  version forever — and get its own users told to update.
"""
import json
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def test_manifest_marketplace_and_header_agree():
    version = _load(PLUGIN / ".claude-plugin" / "plugin.json")["version"]
    listed = [p["version"] for p in _load(REPO / ".claude-plugin" / "marketplace.json")["plugins"]
              if p["name"] == "whales"]
    header = _load(PLUGIN / ".mcp.json")["mcpServers"]["whales"]["headers"]["X-Whales-Plugin-Version"]
    assert listed == [version]
    assert header == version


def test_the_token_header_is_still_there():
    headers = _load(PLUGIN / ".mcp.json")["mcpServers"]["whales"]["headers"]
    assert headers["Authorization"] == "Bearer ${user_config.whales_token}"
