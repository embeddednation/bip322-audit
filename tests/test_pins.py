"""The sibling packages are named once: the lower bound in pyproject.toml is the tag setup.sh and ci.yml check out."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _bounds(requirements: list[str]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for req in requirements:
        m = re.fullmatch(r"(bip322-[a-z]+)(?:\[[a-z,]+\])?>=([0-9][0-9.]*),<[0-9][0-9.]*", req)
        if m:
            found.setdefault(m.group(1), set()).add("v" + m.group(2))
    return found


def test_sibling_versions_agree():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirements = data["project"]["dependencies"] + [r for reqs in data["project"].get("optional-dependencies", {}).values() for r in reqs]
    bounds = _bounds(requirements)
    assert bounds, "no sibling package among the dependencies"
    for name, tags in bounds.items():
        assert len(tags) == 1, f"{name} has several lower bounds in pyproject.toml: {sorted(tags)}"
    setup = (ROOT / "setup.sh").read_text()
    for name, tags in bounds.items():
        var = name.split("-")[1].upper() + "_REF"
        m = re.search(var + r"=\$\{" + var + r":-(v[^}]+)\}", setup)
        assert m and m.group(1) == next(iter(tags)), f"setup.sh {var} does not match the pyproject.toml bound {sorted(tags)}"
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for name, tags in bounds.items():
        for m in re.finditer(r"repository: embeddednation/" + name + r"\n\s+ref: (v[^\s]+)", ci):
            assert m.group(1) == next(iter(tags)), f"ci.yml checks out {name} at {m.group(1)}, pyproject.toml wants {sorted(tags)}"
    assert data["project"]["version"] == (ROOT / "bip322audit" / "_version.py").read_text().split('"')[1]
