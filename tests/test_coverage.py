from pathlib import Path

import httpx
import tomllib
from mcp.server.fastmcp import FastMCP

from rapidata_mcp.coverage import classified, differences, sdk_functions
from rapidata_mcp.tools import register_tools

COVERAGE = tomllib.loads(
    (Path(__file__).parent.parent / "sdk_coverage.toml").read_text("utf-8")
)


def test_every_sdk_function_is_classified():
    assert differences(classified(COVERAGE), sdk_functions()) is None


def test_a_new_or_removed_sdk_function_produces_the_issue_text():
    listed = classified(COVERAGE)
    actual = (sdk_functions() | {"RapidataJob.teleport"}) - {"RapidataJob.pause"}

    body = differences(listed, actual)

    assert body is not None
    new, gone = body.split("`sdk_coverage.toml` lists these")
    assert "not in `sdk_coverage.toml`" in new
    assert "- `RapidataJob.teleport`" in new
    assert "- `RapidataJob.pause`" in gone


def test_exposed_functions_name_real_tools():
    mcp = FastMCP("test")
    register_tools(mcp, lambda: None, http=httpx.Client())  # type: ignore[arg-type]
    tools = {tool.name for tool in mcp._tool_manager.list_tools()}

    assert set(COVERAGE["exposed"]) <= tools
    assert all(section["why"] for section in COVERAGE["left_out"].values())
