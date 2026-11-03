"""The README must list every setting in config/settings.yaml, and be regenerated after edits."""

import importlib.util
import re
from pathlib import Path

from nseswing.config import leaf_keys, load_raw

ROOT = Path(__file__).resolve().parents[1]


def _generator():
    spec = importlib.util.spec_from_file_location("config_reference", ROOT / "scripts" / "config_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_setting_is_documented_in_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    missing = [k for k in leaf_keys(load_raw()) if f"`{k}`" not in readme]
    assert not missing, f"undocumented settings: {missing}"


def test_readme_reference_is_up_to_date():
    gen = _generator()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    current = re.search(re.escape(gen.START) + r".*?" + re.escape(gen.END), readme, re.S).group(0)
    assert current == gen.render(ROOT / "config" / "settings.yaml"), "run: python scripts/config_reference.py"


def test_every_setting_has_a_comment():
    gen = _generator()
    notes = gen.comments(ROOT / "config" / "settings.yaml")
    blank = [k for k in leaf_keys(load_raw()) if not (notes.get(k) or notes.get(k.rsplit(".", 1)[0]))]
    assert not blank, f"settings without an explanatory comment: {blank}"


def test_every_setting_is_read_by_the_code():
    """A setting nobody reads would be misleading: you would tune it and nothing would change."""
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src" / "nseswing").rglob("*.py"))
    src += "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "app").glob("*.py"))
    unused = []
    for key in leaf_keys(load_raw()):
        last = key.split(".")[-1]
        if not re.search(r"\." + re.escape(last) + r"\b", src) and f'"{last}"' not in src:
            unused.append(key)
    assert not unused, f"settings never read by the code: {unused}"
