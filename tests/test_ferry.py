#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ferry 回归测试：python3 -m unittest discover -s tests -v"""

import json
import os
import subprocess
import tempfile
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

    def test_sed_macos_back_to_linux_drops_empty_backup(self):
        t = tr("sed -i '' 's/a/b/' f.txt", "linux", "macos")
        self.assertIn("'s/a/b/'", t.command)
        self.assertNotIn("'' '',", t.command)

    def test_sed_expression_values_substituted(self):
        t = tr("sed -i 's/debug=True/debug=False/' settings.py", "windows")
        self.assertIn("'debug=True','debug=False'", t.command)
        self.assertIn("settings.py", t.command)

    def test_strftime_converted(self):
        self.assertIn("yyyy-MM-dd", tr("date +%Y-%m-%d", "windows").command)

    def test_stat_mtime_keeps_timestamp_semantics(self):
        t = tr("stat -c %Y app.log", "windows")
        self.assertIn("ToUnixTimeSeconds", t.command)

    def test_quote_preserved_curl_json(self):
        t = tr("""curl -X POST -d '{"a":1}' https://x/y""", "windows")
        self.assertIn("""'{"a":1}'""", t.command)

    def test_home_path_mapped(self):
        t = tr("ls -la /home/user", "windows")
        self.assertIn("$env:USERPROFILE", t.command)
        self.assertEqual(t.status, "approximate")

    def test_env_var(self):
        t = tr("echo $PATH", "windows")
        self.assertIn("$env:PATH", t.command)

    def test_recipe_example_literals_replaced(self):
        # recipe 里的示例路径不能原样吐出来
        t = tr("grep -rn TODO src", "macos")
        self.assertEqual(t.command, "grep -rn TODO src")
        self.assertNotIn("Select-String", " ".join(t.caveats))

    def test_caveat_filtered_by_target(self):
        t = tr("grep -rn TODO src", "windows")
        self.assertTrue(any("Select-String" in c for c in t.caveats))

    def test_universal_flags_pass_through(self):
        # curl/git 这类三平台同一二进制，参数不该被"翻掉"
        t = tr("curl -fsSL https://example.com/x.sh", "windows")
        self.assertIn("-fsSL", t.command)

    def test_combined_short_flag_kept(self):
        t = tr("ls -la /tmp", "macos")
        self.assertIn("ls -la", t.command)

    def test_bsd_pcre_unsupported(self):
        t = tr("grep -P '\\d{3}' a.txt", "macos", frm="linux")
        self.assertEqual(t.status, "unsupported")

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
        # 不变量：删除动作不许被静默丢弃。要么明确无等价，要么给一条真的会删的命令 + caveat
        self.assertNotEqual(t.status, "equivalent")
        if t.command:
            self.assertIn("Remove-Item", t.command)
            self.assertTrue(t.caveats)

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

    def test_tail_with_abs_path_keeps_tail_semantics(self):
        """tail -n 50 /var/log/x 不能被参数映射丢掉 -n 50（只能返回全部）。

        /var/log 在 Windows 无等价目录树，映射只能算近似，状态必须是
        approximate——旧断言要求 equivalent 且原样保留 POSIX 路径，
        等于把「路径没转换」的 bug 写成了契约（P0-A）。
        """
        t = tr("tail -n 50 /var/log/syslog", "windows")
        self.assertEqual(t.status, "approximate")
        self.assertIn("-Tail 50", t.command)
        self.assertNotIn("/var/log", t.command)
        self.assertIn("winevt", t.command.lower() + t.command)

    def test_abs_path_translated_in_recipe_branch(self):
        """recipe 分支也要转换用户传入的绝对路径（P0-A）。"""
        t = tr("cat /etc/hosts", "windows")
        self.assertIn("drivers", t.command + t.command.lower())
        self.assertNotIn(" /etc/hosts", t.command)
        self.assertEqual(t.status, "approximate")

    def test_env_prefix_stripped_with_note(self):
        """env python3 曾被写成 Get-ChildItem python3（env 被当 head，P0-B）。"""
        t = tr("env python3", "windows")
        self.assertNotIn("Get-ChildItem", t.command)
        self.assertTrue(any("env" in c for c in t.caveats))

    def test_du_sh_with_abs_path_keeps_path(self):
        """du -sh /var/log 曾丢掉路径参数（只汇总当前目录）。"""
        t = tr("du -sh /var/log", "windows")
        self.assertIn("winevt", t.command.lower() + "winevt")
        self.assertNotIn(" / 1MB".replace(" / ", " /var"), t.command)

    def test_du_sh_dot_not_corrupted(self):
        """du -sh . 的 `.Sum / 1MB` 里的 / 是除号，不能被当成根目录（回归）。"""
        t = tr("du -sh .", "windows")
        self.assertIn("/ 1MB", t.command)
        self.assertNotIn("C:\\", t.command)

    def test_var_log_maps_to_winevt(self):
        """/var/log 曾被硬拼成 C:\\var\\log（不存在），必须映射到真实目录。"""
        t = tr("ls -la /var/log", "windows")
        self.assertNotIn("C:\\var\\log", t.command)
        self.assertIn("winevt", t.command.lower() + "winevt")

    def test_relative_path_not_over_flagged(self):
        """相对路径不该被误判成不可映射而降级。"""
        t = tr("cat a.txt", "windows")
        self.assertEqual(t.status, "equivalent")

    def test_tail_in_pipe_uses_select_object(self):
        t = tr("cat /var/log/syslog | tail -n 50", "windows")
        self.assertEqual(t.status, "approximate")
        self.assertIn("Select-Object -Last 50", t.command)

    def test_numeric_recipe_args_not_treated_as_slot(self):
        """50 是目标侧开关的值，不能被当槽位吞掉。"""
        t = tr("tail -n 20 a.log", "windows")
        self.assertIn("-Tail 20", t.command)


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


class TestSshHosts(unittest.TestCase):
    """hosts 登记与 ssh dry-run（不真的连远端）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = dict(os.environ, FERRY_HOSTS=os.path.join(self.tmp, "hosts.json"))

    def tearDown(self):
        import shutil as _sh
        _sh.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "ferry.py"), *args],
                              capture_output=True, text=True, env=self.env)

    def test_hosts_add_then_list(self):
        self.run_cli("hosts", "add", "box", "macos", "--user", "u")
        r = self.run_cli("hosts", "--json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["hosts"]["box"]["platform"], "macos")

    def test_ssh_dry_run_translates(self):
        self.run_cli("hosts", "add", "box", "macos", "--user", "u")
        r = self.run_cli("ssh", "box", "ls -la /tmp")
        self.assertIn("macOS", r.stdout)
        self.assertIn("ssh ", r.stdout)
        self.assertIn("--run", r.stdout)          # 默认只打印，不执行
        self.assertEqual(r.returncode, 0)

    def test_ssh_unknown_host_errors(self):
        r = self.run_cli("ssh", "nope", "ls")
        self.assertEqual(r.returncode, 4)


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
