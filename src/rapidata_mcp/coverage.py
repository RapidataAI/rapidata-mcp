"""Compare the installed rapidata SDK with ``sdk_coverage.toml``.

The file classifies every public SDK function as exposed by an MCP tool or left out
for a stated reason. ``python -m rapidata_mcp.coverage [path]`` prints the GitHub
issue body for each function the file misses or names but the SDK no longer has,
and exits 1 when there is any.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re
import sys
import textwrap
from collections import deque
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # pyright: ignore[reportMissingImports]

_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")


def sdk_functions() -> set[str]:
    """Every public method and property reachable from ``RapidataClient``, as
    ``"Class.member"``: the client's managers, and every class a reachable member
    is annotated to return."""
    from rapidata import RapidataClient

    classes = _sdk_classes()
    queue: deque[type] = deque([RapidataClient, *_managers(RapidataClient, classes)])
    seen: set[type] = set()
    functions: set[str] = set()
    while queue:
        cls = queue.popleft()
        if cls in seen:
            continue
        seen.add(cls)
        for name, member in _public_members(cls):
            functions.add(f"{cls.__name__}.{name}")
            for returned in _returned_classes(member, classes):
                queue.append(returned)
    return functions


def classified(coverage: dict[str, Any]) -> set[str]:
    sections = [*coverage.get("exposed", {}).values()]
    sections += coverage.get("left_out", {}).values()
    return {function for section in sections for function in section["functions"]}


def differences(listed: set[str], actual: set[str]) -> str | None:
    """The issue body for the functions that differ, or None when none do."""
    new, gone = sorted(actual - listed), sorted(listed - actual)
    if not new and not gone:
        return None
    lines = []
    if new:
        lines += [
            "These SDK functions are not in `sdk_coverage.toml`. Add each under the "
            "tool that exposes it, or under `left_out` with the reason:",
            "",
            *[f"- `{function}`" for function in new],
            "",
        ]
    if gone:
        lines += [
            "`sdk_coverage.toml` lists these, but the SDK no longer has them; remove "
            "them (and check the tool that used them still works):",
            "",
            *[f"- `{function}`" for function in gone],
            "",
        ]
    return "\n".join(lines)


def _sdk_classes() -> dict[str, type]:
    import rapidata.rapidata_client as package

    classes: dict[str, type] = {}
    for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
        module = importlib.import_module(info.name)
        for name, value in vars(module).items():
            if inspect.isclass(value) and value.__module__ == module.__name__:
                classes.setdefault(name, value)
    return classes


def _managers(client: type, classes: dict[str, type]) -> list[type]:
    """The classes ``RapidataClient.__init__`` assigns to its public attributes."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(client.__init__)))
    managers = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        target, call = node.targets[0], node.value.func
        if (
            isinstance(target, ast.Attribute)
            and not target.attr.startswith("_")
            and isinstance(call, ast.Name)
            and call.id in classes
        ):
            managers.append(classes[call.id])
    return managers


def _public_members(cls: type) -> list[tuple[str, Any]]:
    members = []
    for owner in cls.__mro__:
        if not owner.__module__.startswith("rapidata."):
            continue
        for name, member in vars(owner).items():
            if name.startswith("_") or name in dict(members):
                continue
            if isinstance(member, (property, classmethod, staticmethod)):
                members.append((name, member))
            elif inspect.isfunction(member):
                members.append((name, member))
    return members


def _returned_classes(member: Any, classes: dict[str, type]) -> list[type]:
    function = member.fget if isinstance(member, property) else member
    function = getattr(function, "__func__", function)
    annotation = getattr(function, "__annotations__", {}).get("return")
    if annotation is None:
        return []
    text = annotation if isinstance(annotation, str) else repr(annotation)
    return [classes[name] for name in _IDENTIFIER.findall(text) if name in classes]


def main(argv: list[str]) -> int:
    path = Path(argv[0] if argv else "sdk_coverage.toml")
    listed = classified(tomllib.loads(path.read_text("utf-8")))
    body = differences(listed, sdk_functions())
    if body is None:
        return 0
    print(body)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
