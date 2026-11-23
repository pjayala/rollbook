# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""The dashboard's scripts as a whole: index.html loads every one of them in an
order that works. They are classic scripts sharing one global scope, so a name
declared twice is a load-time SyntaxError, and a top-level initialiser that uses
a later script's name is a ReferenceError: both only show in a real page load."""

import json
import os
import re
import shutil
import subprocess
import unittest

from helpers import STATIC, script_order, tmpdir

NODE = shutil.which("node")
TOP = re.compile(r"^(?:async\s+)?(?:function\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)", re.M)


def _read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


class Scripts(unittest.TestCase):
    def test_every_script_loaded_once_and_main_last(self):
        order = script_order()
        on_disk = sorted(n for n in os.listdir(STATIC) if n.endswith(".js"))
        self.assertEqual(sorted(order), on_disk, "a script missing from index.html, or a tag without a file")
        self.assertEqual(len(order), len(set(order)))
        self.assertEqual(order[-1], "main.js", "main.js boots the app: every other script must be loaded first")

    def test_headers(self):
        for name in script_order():
            head = _read(name).split("\n")[:8]
            self.assertTrue(head[0].startswith("// SPDX-License-Identifier: GPL-3.0-or-later"), name)
            self.assertIn('"use strict";', head, name)

    def test_no_top_level_name_twice(self):
        seen = {}
        for name in script_order():
            for m in TOP.finditer(_read(name)):
                self.assertNotIn(m.group(1), seen, "%s declared in %s and %s" % (m.group(1), seen.get(m.group(1)), name))
                seen[m.group(1)] = name

    def test_top_level_declarations_are_plain(self):
        """TOP sees one name per declaration: `const {a, b} = …` or `const a = 1, b = 2`
        at top level would hide names from the duplicate check."""
        for name in script_order():
            for ln in _read(name).split("\n"):
                self.assertNotRegex(ln, r"^(?:const|let|var)\s*[{\[]", "%s: destructuring at top level" % name)
                m = re.match(r"^(?:const|let|var)\s+\w+\s*=\s*([^;]*);\s*(?://.*)?$", ln)
                if m and "(" not in m.group(1) and "[" not in m.group(1) and "{" not in m.group(1) and "`" not in m.group(1) and '"' not in m.group(1):
                    self.assertNotRegex(m.group(1), r",\s*[A-Za-z_$][\w$]*\s*=", "%s: two declarators on one line: %s" % (name, ln))

    def test_files_stay_small(self):
        """The point of the split: no new 3000-line file. compare.js is a page of its own."""
        for name in script_order():
            n = _read(name).count("\n")
            self.assertLess(n, 750, "%s has %d lines: split it" % (name, n))


# Enough of a browser for every script's top level and boot() to run: anything
# not listed is a Proxy that answers every property and call with itself.
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [dir, names] = [process.argv[2], JSON.parse(process.argv[3])];
const any = new Proxy(function () {}, { get: (t, k) => k === Symbol.toPrimitive ? () => "" : k === "then" ? undefined : any,
  apply: () => any, construct: () => any, set: () => true });
const store = {};
const ctx = {
  console, URLSearchParams, Map, Set, Promise, JSON, Math, Date, setTimeout: () => 0, clearTimeout() {},
  document: any, navigator: any, history: any, innerWidth: 1440, devicePixelRatio: 1,
  localStorage: store, sessionStorage: {}, location: { hash: "", search: "", pathname: "/" },
  addEventListener() {}, fetch: () => new Promise(() => {}), requestAnimationFrame: () => 0, cancelAnimationFrame() {},
  matchMedia: () => any, getComputedStyle: () => any, ResizeObserver: function () { return any; }, Image: function () { return any; },
};
ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
vm.createContext(ctx);
for (const n of names) vm.runInContext(fs.readFileSync(dir + "/" + n, "utf8"), ctx, { filename: n });
const want = ["viewHome", "viewProgress", "viewSessions", "viewSession", "viewMap", "viewSegments", "viewSegment",
  "viewProfile", "viewGear", "viewCompare", "route", "boot", "chart", "sessionMap"];
process.stdout.write(JSON.stringify(want.filter(n => vm.runInContext("typeof " + n, ctx) !== "function")));
"""


@unittest.skipUnless(NODE, "node not installed")
class LoadOrder(unittest.TestCase):
    def load(self, order):
        path = os.path.join(tmpdir(self), "load.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(HARNESS)
        return subprocess.run([NODE, path, STATIC, json.dumps(order)], capture_output=True, text=True, timeout=60)

    def test_all_scripts_load_and_boot_starts(self):
        p = self.load(script_order())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), [], "views missing after load")

    def test_harness_catches_a_bad_order(self):
        """circuits.js reads PAL (core.js) at its top level: loaded first, it must fail."""
        order = script_order()
        order.remove("circuits.js")
        order.insert(0, "circuits.js")
        p = self.load(order)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("PAL", p.stderr)

    def test_harness_catches_a_name_declared_twice(self):
        order = script_order()
        p = self.load(order[:-1] + ["core.js", "main.js"])  # core.js again: const PAL twice
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("SyntaxError", p.stderr)


if __name__ == "__main__":
    unittest.main()
