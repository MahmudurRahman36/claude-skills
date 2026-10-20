"""Build the throwaway `inv` project used by tests/scenarios.md.

Usage: python make_fixture.py <empty-target-dir>
Stdlib only. The target becomes a git repo with one commit, so diffs show what an agent changed.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

HANDLERS = 17

FILES = {
    "inv/__init__.py": "",
    "inv/config.py": 'SETTINGS = {\n    "data_file": "data/inventory.json",\n}\n',
    "inv/store.py": '''import json
import os
import tempfile


def load(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save(path, items):
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=2)
    os.replace(tmp, path)


def add(path, name, qty, price):
    items = load(path)
    items.append({"name": name, "qty": qty, "price": price})
    save(path, items)
    return items
''',
    "inv/fmt.py": '''def price(value):
    return f"{value}"


def render_table(items):
    rows = [f"{i['name']:<12}{i['qty']:>5}  {price(i['price'])}" for i in items]
    return "\\n".join(rows)
''',
    "inv/cli.py": '''import argparse

from inv import fmt, store
from inv.config import SETTINGS


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inventroy manager")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("qty", type=int)
    add.add_argument("price", type=float)
    args = parser.parse_args(argv)
    path = SETTINGS["data_file"]
    if args.cmd == "list":
        print(fmt.render_table(store.load(path)))
    else:
        store.add(path, args.name, args.qty, args.price)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
    "inv/legacy/__init__.py": "",
    "inv/legacy/money.py": '''def fmt_price(value):
    """Old helper from the v1 report module."""
    return f"{int(value * 100) / 100:.2f}"
''',
    "inv/handlers/__init__.py": "",
    "tests/__init__.py": "",
    "tests/test_store.py": '''import os
import tempfile
import unittest

from inv import store


class StoreTest(unittest.TestCase):
    def test_add_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "inv.json")
            store.add(path, "pen", 3, 1.5)
            self.assertEqual(store.load(path)[0]["name"], "pen")
''',
    "tests/test_fmt.py": '''import unittest

from inv import fmt


class FmtTest(unittest.TestCase):
    def test_render_table(self):
        out = fmt.render_table([{"name": "pen", "qty": 3, "price": 1.5}])
        self.assertIn("pen", out)
        self.assertIn("1.5", out)
''',
    "tests/test_cli.py": '''import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from inv import cli


class CliTest(unittest.TestCase):
    def test_list(self):
        items = [{"name": "pen", "qty": 3, "price": 1.5}]
        with mock.patch("inv.store.load", return_value=items), redirect_stdout(io.StringIO()) as out:
            cli.main(["list"])
        self.assertIn("pen", out.getvalue())
''',
    "tests/test_handlers.py": f'''import importlib
import unittest

ITEMS = [{{"name": "pen", "qty": 3, "price": 1.5}}, {{"name": "ink", "qty": 0, "price": 4.0}}]


class HandlerTest(unittest.TestCase):
    def test_all_handlers_render(self):
        for n in range(1, {HANDLERS + 1}):
            mod = importlib.import_module(f"inv.handlers.h{{n:02d}}")
            self.assertIsInstance(mod.run(ITEMS), str)
''',
}

for n in range(1, HANDLERS + 1):
    FILES[f"inv/handlers/h{n:02d}.py"] = f'''from inv import fmt


def run(items):
    picked = [i for i in items if i["qty"] >= {n % 3}]
    return fmt.render_table(picked)
'''


def build(target):
    root = Path(target)
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise SystemExit(f"target not empty: {root}")
    for rel, body in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    data = root / "data"
    data.mkdir()
    items = [{"name": "pen", "qty": 3, "price": 1.5}, {"name": "ink", "qty": 0, "price": 19.999}]
    (data / "inventory.json").write_text(json.dumps(items, indent=2), encoding="utf-8")
    con = sqlite3.connect(data / "app.db")
    con.execute("create table audit(id integer primary key, event text)")
    con.execute("insert into audit(event) values ('seed')")
    con.commit()
    con.close()
    for cmd in (["init", "-q"], ["add", "-A"],
                ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "fixture"]):
        subprocess.run(["git", *cmd], cwd=root, check=True)
    return root


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(build(sys.argv[1]))
