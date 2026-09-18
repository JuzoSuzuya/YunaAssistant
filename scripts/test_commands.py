#!/usr/bin/env python3
"""Smoke tests for command_registry.py (plan task T4).

Run: python3 scripts/test_commands.py   (exit 0 = all PASS)
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "yuna-desktop"))

import command_registry as cr  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL  {name}  {detail}")


def main() -> int:
    # (a) count >= 1000
    n = cr.count_commands()
    check("count>=1000", n >= 1000, f"got {n}")
    print(f"      builtin+custom count = {n}")

    # (b) per-family: patterns[0] matches sample AND dispatch(sample) resolves
    for fam in cr.BUILTIN_FAMILIES:
        pat = fam["patterns"][0]
        sample = fam["sample"]
        m = re.search(pat, sample, re.I)
        check(
            f"family[{fam['id']}].pattern0 matches sample",
            m is not None,
            f"pattern={pat!r} sample={sample!r}",
        )
        d = cr.dispatch(sample)
        check(
            f"family[{fam['id']}].dispatch(sample)",
            d is not None and d["command"] == fam["id"],
            f"got {d}",
        )

    # (c) custom command round-trip (add → dispatch → remove)
    name = f"test_custom_{int(time.time())}"
    cmd = {
        "name": name,
        "patterns": [r"тестовая команда (?P<query>.+)"],
        "actions": [{"fn": "now_playing"}],
        "enabled": True,
        "stop_on_error": True,
    }
    r = cr.add_custom_command(cmd)
    check("custom.add", r.get("ok") is True, str(r))
    d = cr.dispatch("тестовая команда проверка")
    check(
        "custom.dispatch",
        d is not None
        and d["command"] == name
        and d["source"] == "custom"
        and d["params"].get("query") == "проверка",
        str(d),
    )
    check("custom.remove", cr.remove_custom_command(name) is True)
    check("custom.gone", cr.dispatch("тестовая команда проверка") is None)

    # (d) multi-action ordering + all ok
    multi = {
        "command": "test_multi",
        "actions": [
            {"fn": "now_playing"},
            {"fn": "stop_speaking"},
            {"fn": "now_playing"},
        ],
        "params": {},
        "stop_on_error": True,
    }
    res = cr.execute(multi)
    check(
        "multi-action order+ok",
        len(res) == 3
        and all(x["ok"] for x in res)
        and [x["action"]["fn"] for x in res]
        == ["now_playing", "stop_speaking", "now_playing"],
        str(res),
    )

    # (e) stop_on_error semantics
    bad = {
        "command": "test_bad",
        "actions": [
            {"fn": "now_playing"},
            {"fn": "definitely_missing_fn"},
            {"fn": "now_playing"},
        ],
        "params": {},
        "stop_on_error": True,
    }
    res = cr.execute(bad)
    check("stop_on_error=True stops", len(res) == 2 and not res[1]["ok"], str(res))
    bad["stop_on_error"] = False
    res = cr.execute(bad)
    check(
        "stop_on_error=False continues",
        len(res) == 3 and res[2]["ok"],
        str(res),
    )

    # (f) malformed input → None, no exception
    check("dispatch nonsense is None", cr.dispatch("asdfghjkl qwerty nonsense") is None)
    check("dispatch non-str is None", cr.dispatch(12345) is None)
    check("dispatch empty is None", cr.dispatch("   ") is None)

    # (g) missing/corrupt custom.json → []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "custom.json"
        orig = cr.CUSTOM_PATH
        cr.CUSTOM_PATH = tmp
        try:
            check("custom missing file -> []", cr.load_custom_commands() == [])
            tmp.write_text("{corrupt json!!", encoding="utf-8")
            check("custom corrupt file -> []", cr.load_custom_commands() == [])
        finally:
            cr.CUSTOM_PATH = orig
            cr._custom_cache = None

    # (h) custom.json schema sanity: saved file is valid JSON list
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "custom.json"
        orig = cr.CUSTOM_PATH
        cr.CUSTOM_PATH = tmp
        try:
            cr.add_custom_command(cmd)
            data = json.loads(tmp.read_text(encoding="utf-8"))
            check(
                "custom saved as JSON list", isinstance(data, list) and len(data) == 1
            )
            cr.remove_custom_command(name)
        finally:
            cr.CUSTOM_PATH = orig
            cr._custom_cache = None

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
