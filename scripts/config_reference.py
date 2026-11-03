"""Generate the README's configuration reference from config/settings.yaml.

Every key, its default and its inline comment (with the [ASSUMPTION] /
[OPTIONAL] / [VERIFY] tags) is written between the markers

    <!-- config-reference:start -->  ...  <!-- config-reference:end -->

Run after editing settings.yaml:   python scripts/config_reference.py
The test suite fails if the README is out of date.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nseswing.config import leaf_keys, load_raw  # noqa: E402

START, END = "<!-- config-reference:start -->", "<!-- config-reference:end -->"
_KEY = re.compile(r"^(\s*)([A-Za-z0-9_]+):(.*)$")


def comments(path: Path) -> dict[str, str]:
    """Dotted key → inline comment, from a block-style YAML file."""
    out: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    pending: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            pending.append(stripped.lstrip("# ").strip())
            continue
        m = _KEY.match(raw)
        if not m:
            pending = [] if not stripped else pending
            continue
        indent, key, rest = len(m.group(1)), m.group(2), m.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        dotted = ".".join([k for _, k in stack] + [key])
        comment = ""
        r = rest.strip()
        if r[:1] in ("'", '"'):  # skip the quoted value, then look for a comment
            close = r.find(r[0], 1)
            r = r[close + 1 :] if close > 0 else ""
        if "#" in r:
            comment = r.split("#", 1)[1].strip()
        elif pending and dotted.count(".") >= 1:
            comment = " ".join(pending)
        out[dotted] = comment
        pending = []
        stack.append((indent, key))
    return out


def value_of(data: dict, dotted: str):
    cur = data
    for part in dotted.split("."):
        cur = cur[part]
    return cur


def fmt(v, top: bool = True) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, dict):
        if not v:
            return "{}"
        sep = "<br>" if top else ", "
        inner = sep.join(f"{k}: {fmt(x, False)}" for k, x in v.items())
        return inner if top else "{" + inner + "}"
    if isinstance(v, list):
        if v and isinstance(v[0], dict):
            return "<br>".join(fmt(x, False) for x in v)
        return "[" + ", ".join(fmt(x, False) for x in v) + "]"
    return str(v)


def render(settings_path: Path) -> str:
    data = load_raw(settings_path)
    notes = comments(settings_path)
    lines = [START, "", "_Generated from `config/settings.yaml` by `python scripts/config_reference.py`. Do not edit by hand._", ""]
    section = None
    for key in leaf_keys(data):
        top = key.split(".")[0]
        if top != section:
            section = top
            lines += ["", f"#### `{top}`", "", "| Key | Default | Meaning / assumption |", "|---|---|---|"]
        note = notes.get(key) or notes.get(key.rsplit(".", 1)[0], "")
        val = fmt(value_of(data, key)).replace("|", "\\|")
        cell = "<br>".join(f"`{part}`" for part in val.split("<br>"))
        lines.append(f"| `{key}` | {cell} | {note.replace('|', '/')} |")
    lines += ["", END]
    return "\n".join(lines)


def update_readme(readme: Path, settings_path: Path) -> bool:
    text = readme.read_text(encoding="utf-8")
    block = render(settings_path)
    new = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: block, text, flags=re.S)
    changed = new != text
    readme.write_text(new, encoding="utf-8")
    return changed


if __name__ == "__main__":
    changed = update_readme(ROOT / "README.md", ROOT / "config" / "settings.yaml")
    print("README configuration reference", "updated" if changed else "already up to date")
