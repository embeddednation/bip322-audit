"""Every pin of a git dependency names the same tag: the main dependency, the extras, and setup.sh."""

import re
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent


def _pins(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for name, tag in re.findall(r"(bip322-[a-z]+)(?:\[[a-z,]+\])? @ git\+ssh://[^@]+@[^@]+@(v[0-9][^\"\s]*)", text):
        found.setdefault(name, set()).add(tag)
    return found


def test_git_pins_agree():
    pyproject = (ROOT / "pyproject.toml").read_text()
    data = tomllib.loads(pyproject)
    requirements = data["project"]["dependencies"] + [r for reqs in data["project"].get("optional-dependencies", {}).values() for r in reqs]
    pins = _pins("\n".join(requirements))
    assert pins, "no git pins in pyproject.toml (moved to PyPI? then drop this test)"
    for name, tags in pins.items():
        assert len(tags) == 1, f"{name} is pinned at several tags in pyproject.toml: {sorted(tags)}"
    setup = (ROOT / "setup.sh").read_text()
    for name, tags in pins.items():
        var = name.split("-")[1].upper() + "_REF"
        m = re.search(var + r"=\$\{" + var + r":-(v[^}]+)\}", setup)
        assert m and m.group(1) == next(iter(tags)), f"setup.sh {var} does not match the pyproject.toml pin {sorted(tags)}"
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for name, tags in pins.items():
        for m in re.finditer(r"repository: embeddednation/" + name + r"\n\s+ref: (v[^\s]+)", ci):
            assert m.group(1) == next(iter(tags)), f"ci.yml checks out {name} at {m.group(1)}, pyproject.toml pins {sorted(tags)}"
    assert data["project"]["version"] == (ROOT / "bip322audit" / "_version.py").read_text().split('"')[1]
