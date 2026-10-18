#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ferry 回归测试：python3 -m unittest discover -s tests -v"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ferry  # noqa: E402

KB = ferry.KB()
KB.load()


def tr(cmd, to, frm=None, shell=None):
    return ferry.translate(cmd, to, frm, shell, KB)


class TestLexer(unittest.TestCase):
    def test_split_operators(self):
        parts = ferry.split_chain("ls -la | grep x && rm -f a ; echo done")
        self.assertEqual([op for op, _ in parts], ["", "|", "&&", ";"])

    def test_quoted_pipe_not_split(self):
        parts = ferry.split_chain("grep 'a|b' f.txt")
        self.assertEqual(len(parts), 1)

    def test_subshell_kept_intact(self):
        parts = ferry.split_chain("echo $(date | cut -d' ' -f1)")
        self.assertEqual(len(parts), 1)

    def test_tokenize_windows_path(self):
        self.assertEqual(ferry.tokenize("copy C:\\Users\\me\\a.txt D:\\"),
                         ["copy", "C:\\Users\\me\\a.txt", "D:\\"])

    def test_tokenize_quoted(self):
        tq = ferry.tokenize_q("sed -i 's/a b/c/' f.txt")
        self.assertIn(("s/a b/c/", True), tq)


class TestTranslateCore(unittest.TestCase):
    def test_ls_to_windows(self):
        t = tr("ls -la", "windows")
        self.assertEqual(t.status, "approximate")
        self.assertIn("Get-ChildItem", t.command)

    def test_rm_rf_recipe(self):
        t = tr("rm -rf build/", "windows")
        self.assertEqual(t.status, "equivalent")
        self.assertEqual(t.command, "Remove-Item -Recurse -Force build")

    def test_reverse_from_windows(self):
        t = tr("copy /y a.txt b.txt", "linux")
        self.assertEqual(t.source, "windows")
        self.assertEqual(t.command, "cp -f a.txt b.txt")

    def test_cmd_dir_flags(self):
        t = tr("dir /w /s", "linux")
        self.assertEqual(t.command, "ls -C -R")

    def test_sed_bsd_inplace(self):
        t = tr("sed -i 's/a/b/' f.txt", "macos")
        self.assertEqual(t.status, "approximate")
        self.assertIn("-i ''", t.command)
        self.assertIn("'s/a/b/'", t.command)

    def test_quote_preserved_curl_json(self):
        t = tr("""curl -X POST -d '{"a":1}' https://x/y""", "windows")
        self.assertIn("""'{"a":1}'""", t.command)

    def test_home_path_mapped(self):
        t = tr("ls -la /home/fantastic", "windows")
        self.assertIn("$env:USERPROFILE", t.command)
        self.assertEqual(t.status, "approximate")

    def test_env_var(self):
        t = tr("echo $PATH", "windows")
        self.assertIn("$env:PATH", t.command)

    def test_pipeline_pattern(self):
        t = tr("ps aux | grep nginx", "windows")
        self.assertEqual(t.status, "equivalent")
        self.assertEqual(t.command, "Get-Process -Name nginx")

    def test_chmod_unsupported_on_windows(self):
        t = tr("chmod +x run.sh", "windows")
        self.assertEqual(t.status, "unsupported")
        self.assertIsNone(t.command)

    def test_dangerous_flag_not_silently_dropped(self):
        t = tr("find . -name '*.log' -delete", "windows")
        self.assertEqual(t.status, "unsupported")

    def test_systemctl_pattern(self):
        t = tr("systemctl restart nginx", "macos")
        self.assertIn("brew services restart", t.command)
        self.assertEqual(t.status, "approximate")

    def test_cmd_shell_target(self):
        t = tr("rm -rf build", "windows", shell="cmd")
        self.assertIn("rmdir /s /q", t.command)

    def test_compound_ops(self):
        t = tr("rm -rf build && ls -la", "windows")
        self.assertIn("&&", t.command)
        self.assertEqual(t.status, "approximate")  # PS5.1 不支持 &&

    def test_unknown_command(self):
        t = tr("frobnicate --now", "windows")
        self.assertEqual(t.status, "unsupported")

    def test_same_platform_identity(self):
        t = tr("git status -sb", "linux")
        self.assertEqual(t.status, "equivalent")
        self.assertIn("git status", t.command)


class TestJsonContract(unittest.TestCase):
    def test_schema_keys(self):
        d = tr("ls -la", "windows").to_dict()
        for k in ("schema", "input", "source", "target", "shell", "status",
                  "command", "confidence", "inferred", "caveats", "segments"):
            self.assertIn(k, d)
        self.assertEqual(d["schema"], ferry.SCHEMA)

    def test_exit_codes(self):
        self.assertEqual(ferry.EXIT_CODE["equivalent"], 0)
        self.assertEqual(ferry.EXIT_CODE["approximate"], 2)
        self.assertEqual(ferry.EXIT_CODE["unsupported"], 3)


class TestData(unittest.TestCase):
    def test_no_hard_errors(self):
        self.assertEqual(KB.errors, [])

    def test_volume(self):
        self.assertGreaterEqual(len(KB.entries), 110)
        self.assertGreaterEqual(len(KB.patterns), 12)
        self.assertGreaterEqual(len(KB.pitfalls), 15)
        self.assertGreaterEqual(len(KB.syntax), 20)
        self.assertGreaterEqual(len(KB.drills), 20)

    def test_every_entry_has_summary_and_names(self):
        for e in KB.entries:
            self.assertTrue(e.summary, e.id)
            self.assertTrue(any(e.names.get(k) for k in
                                ("linux", "macos", "windows", "windows_cmd")), e.id)

    def test_recipes_have_targets(self):
        for e in KB.entries:
            for r in e.recipes:
                self.assertTrue(any(r.get(k) for k in
                                    ("linux", "macos", "windows", "windows_cmd")),
                                f"{e.id}: 空 recipe")

    def test_statuses_valid(self):
        for e in KB.entries:
            for r in e.recipes:
                self.assertIn(r.get("status", "equivalent"),
                              ("equivalent", "approximate", "unsupported"), e.id)


class TestCli(unittest.TestCase):
    def run_cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(ROOT / "ferry.py"), *args],
                              capture_output=True, text=True, input=stdin,
                              cwd=str(ROOT))

    def test_json_output_parses(self):
        r = self.run_cli("translate", "ls -la", "--to", "windows", "--json")
        self.assertEqual(r.returncode, 2)
        json.loads(r.stdout)

    def test_unsupported_exit_3(self):
        r = self.run_cli("translate", "chmod +x a.sh", "--to", "windows", "--json")
        self.assertEqual(r.returncode, 3)

    def test_doctor_ok(self):
        r = self.run_cli("doctor")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_prompt_export(self):
        r = self.run_cli("prompt-export", "--to", "windows", "--max-chars", "4000")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Get-ChildItem", r.stdout)
        self.assertLessEqual(len(r.stdout), 4100)

    def test_stdin_batch(self):
        r = self.run_cli("translate", "--to", "windows", "--json", "--stdin",
                         stdin="ls -la\nrm -rf x\n")
        data = json.loads(r.stdout)
        self.assertEqual(len(data), 2)

    def test_lookup(self):
        r = self.run_cli("lookup", "grep", "--json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["id"], "grep")

    def test_drills_run_on_current_platform(self):
        r = self.run_cli("drills", "--to", ferry.current_platform(), "--run")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_drills_emit_script(self):
        r = self.run_cli("drills", "--to", "macos", "--emit", "sh")
        self.assertEqual(r.returncode, 0)
        self.assertIn("#!/bin/sh", r.stdout)
        self.assertIn("sed-inplace-bsd", r.stdout)

    def test_syntax_listing(self):
        r = self.run_cli("syntax", "--to", "windows")
        self.assertEqual(r.returncode, 0)
        self.assertIn("$env:", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
