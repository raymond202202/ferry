#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ferry — 摆渡：Linux / macOS / Windows 命令互译。

设计原则
--------
1. 离线优先：核心是一个可审计、可测试的确定性映射库（data/*.jsonc）。
   查不到就明确报 unsupported，绝不静默编造。
2. 绝不静默降级：语义不等价必须标 approximate / unsupported 并给出 caveat，
   让调用方（AI agent）能靠 status + 退出码做分支。
3. Agent 优先：--json 稳定契约，退出码 0/2/3/4 表达等价级别。
4. 无第三方依赖：仅标准库，Linux/macOS 开箱即用。

用法速查
--------
    ferry translate "ls -la" --to windows
    ferry translate "dir /w" --to linux --from windows
    ferry translate "sed -i 's/a/b/' f" --to macos --json
    ferry lookup rm
    ferry prompt-export --to windows --max-chars 3000
    ferry syntax --to windows
    ferry pitfalls --to macos
    ferry doctor --live
    ferry stats
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import base64
import shlex
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = 1
VERSION = "0.2.0"

PLATFORMS = ("linux", "macos", "windows")
PLATFORM_LABEL = {"linux": "Linux", "macos": "macOS", "windows": "Windows"}
DEFAULT_SHELL = {"linux": "bash", "macos": "zsh", "windows": "powershell"}
SHELL_FAMILY = {
    "bash": "posix", "zsh": "posix", "sh": "posix", "dash": "posix",
    "powershell": "powershell", "pwsh": "powershell", "cmd": "cmd",
}
STATUS_ORDER = {"equivalent": 0, "approximate": 1, "unsupported": 2, "error": 3}
EXIT_CODE = {"equivalent": 0, "approximate": 2, "unsupported": 3, "error": 4}

CATEGORY_LABELS = {
    "files": "文件与目录",
    "text": "文本处理",
    "process": "进程与作业",
    "service": "服务与计划任务",
    "net": "网络",
    "pkg": "包管理",
    "archive": "压缩归档",
    "system": "系统信息与电源",
    "dev": "开发工具",
    "misc": "其他",
}

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

ANSI = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m", "cyan": "\033[36m",
}


def color(s: str, name: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return s
    return ANSI.get(name, "") + s + ANSI["reset"]


def normalize_platform(p: str | None) -> str | None:
    if not p:
        return None
    p = p.strip().lower()
    if p in ("linux", "gnu", "ubuntu", "debian", "fedora", "arch", "centos", "rhel"):
        return "linux"
    if p in ("macos", "mac", "osx", "darwin", "apple", "bsd"):
        return "macos"
    if p in ("windows", "win", "win32", "win64", "nt", "cmd", "powershell", "pwsh"):
        return "windows"
    return None


def current_platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def worst(status_a: str, status_b: str) -> str:
    return status_a if STATUS_ORDER.get(status_a, 3) >= STATUS_ORDER.get(status_b, 3) else status_b


def data_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("FERRY_DATA")
    if env:
        dirs.append(Path(env).expanduser())
    here = Path(__file__).resolve().parent
    dirs.append(here / "data")
    dirs.append(Path("~/.ferry/install/data").expanduser())
    dirs.append(Path("~/.local/share/ferry/data").expanduser())
    seen, out = set(), []
    for d in dirs:
        if d.is_dir() and d not in seen:
            seen.add(d)
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# JSONC 载入（允许 // 与 /* */ 注释和尾逗号，方便手工维护）
# ---------------------------------------------------------------------------

def load_jsonc(path: Path):
    txt = path.read_text(encoding="utf-8")
    out: list[str] = []
    i, n = 0, len(txt)
    in_str = False
    while i < n:
        c = txt[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(txt[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if txt.startswith("//", i):
            j = txt.find("\n", i)
            i = n if j < 0 else j
            continue
        if txt.startswith("/*", i):
            j = txt.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    s = "".join(out)
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return json.loads(s)


# ---------------------------------------------------------------------------
# 词法：拆复合命令 / 拆 token
# ---------------------------------------------------------------------------

OPS = ("2>&1", "1>&2", "&>>", ">>", "<<<", "<<", "&&", "||", "|&", "|", ";", "&")


def split_chain(s: str) -> list[tuple[str, str]]:
    """按顶层操作符拆分，返回 [(op_before, segment_text), ...]。"""
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    pending_op = ""
    i, n = 0, len(s)
    quote: str | None = None
    depth = 0
    while i < n:
        c = s[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"`":
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(s[i:i + 2])
            i += 2
            continue
        if c == "$" and i + 1 < n and s[i + 1] == "(":
            depth += 1
            buf.append("$(")
            i += 2
            continue
        if c == "(":
            depth += 1
            buf.append(c)
            i += 1
            continue
        if c == ")" and depth > 0:
            depth -= 1
            buf.append(c)
            i += 1
            continue
        if depth == 0:
            matched = next((op for op in OPS if s.startswith(op, i)), None)
            if matched:
                text = "".join(buf).strip()
                if text:
                    out.append((pending_op, text))
                buf = []
                pending_op = matched
                i += len(matched)
                continue
        buf.append(c)
        i += 1
    text = "".join(buf).strip()
    if text:
        out.append((pending_op, text))
    return out


def tokenize(seg: str) -> list[str]:
    """拆参数，去引号；Windows 路径中的反斜杠保持字面。"""
    toks: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(seg[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
                i += 1
                continue
            buf.append(c)
            i += 1
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c.isspace():
            if buf:
                toks.append("".join(buf))
                buf = []
            i += 1
            continue
        if c == "\\" and i + 1 < n and seg[i + 1] in " \"'\\$`":
            buf.append(seg[i + 1])
            i += 2
            continue
        buf.append(c)
        i += 1
    if buf:
        toks.append("".join(buf))
    return toks


def tokenize_q(seg: str) -> list[tuple[str, bool]]:
    """同 tokenize，但返回 (值, 是否被引号包裹)。"""
    toks: list[tuple[str, bool]] = []
    buf: list[str] = []
    quote: str | None = None
    had_quote = False
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(seg[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
                i += 1
                continue
            buf.append(c)
            i += 1
            continue
        if c in "'\"":
            quote = c
            had_quote = True
            i += 1
            continue
        if c.isspace():
            if buf or had_quote:
                toks.append(("".join(buf), had_quote))
                buf, had_quote = [], False
            i += 1
            continue
        if c == "\\" and i + 1 < n and seg[i + 1] in " \"'\\$`":
            buf.append(seg[i + 1])
            i += 2
            continue
        buf.append(c)
        i += 1
    if buf or had_quote:
        toks.append(("".join(buf), had_quote))
    return toks


def quote_for(tok: str, target: str) -> str:
    if not tok:
        return tok
    needs = any(ch.isspace() for ch in tok) or any(ch in tok for ch in "'\"`$*?[](){}|&;<>")
    if not needs:
        return tok
    return force_quote(tok, target)


def force_quote(tok: str, target: str) -> str:
    """无条件加引号（用于输入本来就是带引号的 token）。"""
    if target == "windows":
        if "'" not in tok:
            return "'" + tok + "'"
        return '"' + tok.replace('"', '`"') + '"'
    return "'" + tok.replace("'", "'\\''") + "'"


def path_style_of(tgt: str, style: str | None) -> str:
    if tgt == "windows":
        return "windows"
    return style or "wsl"


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    raw: dict
    id: str
    category: str
    summary: str
    names: dict
    aliases: dict
    flags: list
    direct_flags: set
    recipes: list

    @property
    def hint(self) -> str:
        return self.raw.get("hint", "") or ""

    @property
    def universal(self) -> bool:
        """同一二进制三平台通用（git/curl/docker/ssh）：参数语义一致，不该硬翻。"""
        return bool(self.raw.get("universal"))

    def name_for(self, platform: str, shell: str | None) -> str | None:
        if platform == "windows":
            if shell == "cmd":
                return self.names.get("windows_cmd") or self.names.get("windows")
            return self.names.get("windows") or self.names.get("windows_cmd")
        return self.names.get(platform)

    def exact_name_for(self, platform: str, shell: str | None) -> str | None:
        """严格按 shell 取名字：cmd 模式下没有 cmd 映射就返回 None。"""
        if platform == "windows":
            if shell == "cmd":
                return self.names.get("windows_cmd")
            return self.names.get("windows")
        return self.names.get(platform)


def flag_key(platform: str, shell: str | None) -> str:
    if platform == "windows":
        return "windows_cmd" if shell == "cmd" else "windows"
    return platform


class KB:
    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.by_id: dict[str, Entry] = {}
        self.index: dict[tuple[str, str], dict[str, Entry]] = {}
        self.syntax: list[dict] = []
        self.pitfalls: list[dict] = []
        self.patterns: list[dict] = []
        self.drills: list[dict] = []
        self.loaded_from: list[str] = []
        self.errors: list[str] = []
        self.collisions: list[str] = []

    # -- 载入 ------------------------------------------------------------
    def load(self) -> None:
        dirs = data_dirs()
        if not dirs:
            self.errors.append("找不到数据目录（data/*.jsonc）")
            return
        base = dirs[0]
        self.loaded_from.append(str(base))
        for path in sorted(base.glob("*.jsonc")):
            try:
                doc = load_jsonc(path)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"{path.name}: JSON 解析失败 — {exc}")
                continue
            for item in doc.get("commands", []):
                self._add(Entry(
                    raw=item,
                    id=item.get("id", ""),
                    category=item.get("category", "misc"),
                    summary=item.get("summary", ""),
                    names=item.get("names", {}) or {},
                    aliases=item.get("aliases", {}) or {},
                    flags=item.get("flags", []) or [],
                    direct_flags=set(item.get("direct_flags", []) or []),
                    recipes=item.get("recipes", []) or [],
                ))
            self.syntax.extend(doc.get("syntax", []) or [])
            self.pitfalls.extend(doc.get("pitfalls", []) or [])
            self.patterns.extend(doc.get("patterns", []) or [])
            self.drills.extend(doc.get("drills", []) or [])

    def _add(self, e: Entry) -> None:
        if not e.id:
            self.errors.append(f"{e.names}: 缺少 id")
            return
        if e.id in self.by_id:
            self.errors.append(f"重复 id: {e.id}")
            return
        self.by_id[e.id] = e
        self.entries.append(e)
        for platform in PLATFORMS:
            shells = ["powershell", "cmd"] if platform == "windows" else [None]
            for sh in shells:
                nm = e.names.get("windows_cmd" if (platform == "windows" and sh == "cmd") else platform)
                if not nm:
                    continue
                key = (platform, sh or "posix")
                bucket = self.index.setdefault(key, {})
                for name in [nm.lower()] + [a.lower() for a in (
                        e.aliases.get("windows_cmd" if (platform == "windows" and sh == "cmd") else platform) or [])]:
                    score = 2 if name.replace("_", "") == e.id.replace("_", "").lower() else 1
                    prev = bucket.get(name)
                    if prev is None or score > prev[0]:
                        if prev is not None and score == prev[0] and prev[1] is not e:
                            self.collisions.append(
                                f"{key[0]}/{key[1]}: `{name}` 同时属于 {prev[1].id} 与 {e.id}（保留前者）")
                        bucket[name] = (score, e)
                    elif prev[1] is not e:
                        self.collisions.append(
                            f"{key[0]}/{key[1]}: `{name}` 同时属于 {prev[1].id} 与 {e.id}（保留前者）")

    # -- 查询 ------------------------------------------------------------
    def find2(self, token: str, platform: str, shell: str | None) -> tuple[Entry | None, str | None]:
        """返回 (条目, 命中的 shell)。windows 下会依次试 powershell / cmd。"""
        names = [token.lower()]
        if "/" in token:
            names.append(token.split("/")[-1].lower())
        if platform == "windows":
            order = ["cmd", "powershell"] if shell == "cmd" else ["powershell", "cmd"]
            for sh in order:
                table = self.index.get(("windows", sh), {})
                for nm in names:
                    if nm in table:
                        return table[nm][1], sh
            return None, None
        table = self.index.get((platform, "posix"), {})
        for nm in names:
            if nm in table:
                return table[nm][1], "posix"
        return None, None

    def find(self, token: str, platform: str, shell: str | None) -> Entry | None:
        return self.find2(token, platform, shell)[0]

    def find_any(self, token: str) -> list[tuple[str, Entry]]:
        hits = []
        for (platform, sh), table in self.index.items():
            rec = table.get(token.lower())
            if rec:
                hits.append((platform, rec[1]))
        return hits

    def stats(self) -> dict:
        cats: dict[str, int] = {}
        recipes = 0
        for e in self.entries:
            cats[e.category] = cats.get(e.category, 0) + 1
            recipes += len(e.recipes)
        return {
            "entries": len(self.entries),
            "recipes": recipes,
            "syntax": len(self.syntax),
            "pitfalls": len(self.pitfalls),
            "patterns": len(self.patterns),
            "drills": len(self.drills),
            "categories": cats,
            "data_dir": self.loaded_from[0] if self.loaded_from else None,
        }


# ---------------------------------------------------------------------------
# 路径 / 变量 / 特殊 token 转换
# ---------------------------------------------------------------------------

POSIX_TO_WIN_ROOTS = {
    "/home": None,
    "/tmp": "$env:TEMP",
    "/var/tmp": "$env:TEMP",
    "/dev/null": "$null",
    "/proc": None,
    "/sys": None,
    "/etc": r"C:\Windows\System32\drivers\etc",
    "/usr/bin": None,
    "/usr/local/bin": None,
    "/opt": r"C:\ProgramData",
    "/mnt": None,
    "/dev": None,
}


def is_posix_path(tok: str) -> bool:
    return bool(re.match(r"^(~|\.{1,2})?/", tok)) or bool(re.match(r"^~[A-Za-z]", tok))


def is_cmd_switch(tok: str) -> bool:
    """cmd.exe 开关：/a /s /w /q /f /i /t... 与 POSIX 绝对路径 /tmp 区分开。"""
    return bool(re.fullmatch(r"/[A-Za-z]{1,2}(:[^\s]*)?", tok)) or tok in ("/?",) or bool(
        re.fullmatch(r"/[A-Za-z]{1,2}\s", tok))


def is_win_path(tok: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", tok)) or tok.startswith("\\\\")


def path_to_posix(p: str, style: str = "wsl") -> tuple[str, list[str]]:
    notes: list[str] = []
    q = p.replace("\\", "/")
    if q.startswith("//"):
        return q, ["UNC 网络路径在 POSIX 下需先挂载"]
    m = re.match(r"^([A-Za-z]):(.*)$", q)
    if m:
        drive, rest = m.group(1).lower(), m.group(2)
        root = f"/{drive}" if style == "msys" else f"/mnt/{drive}"
        notes.append(f"盘符 {m.group(1)}: 映射为 {root}（--path-style={'msys' if style=='msys' else 'wsl'}）")
        return root + rest, notes
    return q, notes


def path_to_windows(p: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    if p == "/dev/null":
        return "$null", ["/dev/null → $null"]
    # /mnt/c/... 或 /c/... → C:\...
    m = re.match(r"^/(?:mnt/)?([a-z])(/.*)?$", p)
    if m and (p.startswith("/mnt/") or re.match(r"^/[a-z]/", p)):
        drive, rest = m.group(1).upper(), (m.group(2) or "/")
        return drive + ":" + rest.replace("/", "\\"), notes
    if p.startswith("~") or p.startswith("/home/") or p.startswith("/Users/"):
        sub = re.sub(r"^(~|/home/[^/]+|/Users/[^/]+)", "", p)
        notes.append("家目录映射为 $env:USERPROFILE")
        return "$env:USERPROFILE" + (sub.replace("/", "\\") if sub else ""), notes
    for root, mapped in POSIX_TO_WIN_ROOTS.items():
        if p == root or p.startswith(root + "/"):
            if mapped is None:
                notes.append(f"{root} 在 Windows 无直接等价路径，原样保留")
                return p, notes
            sub = p[len(root):].lstrip("/")
            if "$env" in mapped:
                notes.append(f"{root} 在 Windows 下映射为 {mapped}（近似）")
                return (mapped + (("\\" + sub.replace("/", "\\")) if sub else "")), notes
            notes.append(f"{root} → {mapped}（近似）")
            return (mapped + ("\\" + sub.replace("/", "\\") if sub else "")), notes
    notes.append("绝对路径按 C: 根处理（请核对）")
    return "C:" + p.replace("/", "\\"), notes


def convert_token(tok: str, src: str, tgt: str, style: str, caveats: list[str]) -> str:
    if tok in ("/dev/null", "$null", "NUL"):
        if tgt == "windows":
            out = "$null" if style != "cmd" else "NUL"
            if out != tok:
                caveats.append(f"{tok} → {out}")
            return out
        if src == "windows":
            return "/dev/null"
        return tok
    if src == tgt:
        return tok
    if tgt == "windows" and is_posix_path(tok):
        out, notes = path_to_windows(tok)
        if tok.endswith("/") and out and not out.endswith(("\\", "/")):
            out += "\\"
        caveats.extend(notes)
        return quote_for(out, "windows") if " " in out else out
    if tgt != "windows" and is_win_path(tok):
        out, notes = path_to_posix(tok, style)
        caveats.extend(notes)
        return quote_for(out, "posix") if " " in out else out
    # 环境变量
    m = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", tok)
    if m and tgt == "windows":
        caveats.append(f"环境变量 ${m.group(1)} → $env:{m.group(1)}")
        return f"$env:{m.group(1)}"
    m = re.fullmatch(r"%([A-Za-z_][A-Za-z0-9_]*)%", tok)
    if m and tgt != "windows":
        return f"${m.group(1)}"
    if tgt == "windows" and m:
        return f"$env:{m.group(1)}"
    return tok


def translate_env_assign(tok: str, tgt: str) -> str | None:
    m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", tok)
    if not m:
        return None
    if tgt == "windows":
        return f"$env:{m.group(1)}='{m.group(2)}'"
    return tok


# ---------------------------------------------------------------------------
# 单段翻译
# ---------------------------------------------------------------------------

@dataclass
class SegResult:
    input: str
    operator: str
    command: str | None
    status: str
    kind: str  # recipe | flags | passthrough | unsupported
    caveats: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


PREFIXES = {"sudo", "doas", "env", "time", "command", "builtin", "exec", "nohup"}
WRAPPERS = {"xargs", "find", "watch", "timeout"}  # 内嵌子命令，不递归翻译


def _norm_tokens(tokens: list[str]) -> list[str]:
    """归一化：忽略参数尾部斜杠差异（build/ 与 build 视为同一目录）。"""
    return [(t.rstrip("/") or "/") if t not in ("", "/") else t for t in tokens]


def _slot_match(recipe_tok: str, input_tok: str) -> bool:
    """recipe 里写的是示例值（TODO、app.tar.gz、src/），位置对齐后应当作槽位。

    参数 token 必须精确相等（-P 与 -rn 不能混），非参数 token 允许不同值。
    """
    if recipe_tok == input_tok:
        return True
    if recipe_tok.startswith("-") or input_tok.startswith("-"):
        return False
    # cmd 风格开关 /w /s /b 也算参数，必须精确匹配（路径 /home/x 不算）
    switch = re.compile(r"/[A-Za-z?]{1,3}$")
    if switch.match(recipe_tok) or switch.match(input_tok):
        return False
    return True


def recipe_match(entry: Entry, src_key: str, tokens: list[str]) -> dict | None:
    """精确匹配 recipe（按 token 归一化，忽略引号与尾部斜杠差异）。"""
    want = _norm_tokens(tokens)
    for r in entry.recipes:
        s = r.get(src_key)
        if not s:
            continue
        rt = _norm_tokens(tokenize(s))
        if len(rt) != len(want):
            continue
        # 先试精确匹配，再试"示例值当槽位"的匹配——同一位置只能命中一条 recipe
        if rt == want:
            return r
    for r in entry.recipes:
        s = r.get(src_key)
        if not s:
            continue
        rt = _norm_tokens(tokenize(s))
        if len(rt) != len(want):
            continue
        if all(_slot_match(a, b) for a, b in zip(rt, want)):
            return r
    return None


def translate_segment(seg: str, src: str, tgt: str, shell_tgt: str, kb: KB,
                      style: str) -> SegResult:
    """把一段（无操作符）命令翻译到目标平台。"""
    tq = tokenize_q(seg)
    tokens = [v for v, _ in tq]
    if not tokens:
        return SegResult(seg, "", None, "equivalent", "passthrough")

    caveats: list[str] = []
    notes: list[str] = []

    # 前缀处理
    prefix: list[str] = []
    idx = 0
    while idx < len(tokens) and (tokens[idx] in PREFIXES or translate_env_assign(tokens[idx], tgt)):
        prefix.append(tokens[idx])
        idx += 1
    if idx >= len(tokens):
        return SegResult(seg, "", None, "unsupported", "unsupported",
                         ["仅有前缀，无实际命令"])

    head = tokens[idx]
    args = tokens[idx + 1:]
    quoted_vals = {v for v, q in tq[idx + 1:] if q and v}
    entry, src_shell = kb.find2(head, src, None)

    src_key = flag_key(src, src_shell)
    tgt_key = flag_key(tgt, shell_tgt)

    # 1) recipe 命中
    if entry:
        r = recipe_match(entry, src_key, tokens[idx:])
        if r:
            val = r.get(tgt_key)
            status = r.get("status", "equivalent")
            caveats = list(r.get("caveats", []) or [])
            if val is None:
                alt = entry.exact_name_for(tgt, shell_tgt)
                return SegResult(seg, "", None, "unsupported", "unsupported",
                                 caveats + [f"{PLATFORM_LABEL[tgt]} / {shell_tgt} 无等价命令"])
            val = substitute_recipe_literals(val, r, src_key, tokens[idx:])
            if tgt != "windows":  # Windows 路径不带尾斜杠；POSIX 目标要保留目录语义
                val = preserve_trailing_slash(val, tokens[idx:])
            return SegResult(seg, "", val, status, "recipe", caveats, notes)

    # 2) 无条目：判断是否跨平台同名（git/curl/docker...）
    if not entry:
        hits = kb.find_any(head)
        if not hits:
            if shutil.which(head) and tgt == current_platform():
                return SegResult(seg, "", seg, "equivalent", "passthrough",
                                 ["本机已存在该命令，原样执行"])
            return SegResult(seg, "", None, "unsupported", "unsupported",
                             [f"映射库未收录 `{head}`，无法确定性翻译"])
        # 目标平台也有同名 → 直通
        tgt_names = {e.name_for(tgt, shell_tgt) for _, e in hits}
        if head in tgt_names:
            return SegResult(seg, "", seg, "equivalent", "passthrough",
                             [f"`{head}` 在目标平台同名可用"])
        entry = hits[0][1]

    if entry is None:
        return SegResult(seg, "", None, "unsupported", "unsupported",
                         [f"映射库未收录 `{head}`"])
    tgt_name = entry.exact_name_for(tgt, shell_tgt)
    if not tgt_name:
        fb = entry.name_for(tgt, None)
        msg = f"{PLATFORM_LABEL[tgt]}/{shell_tgt} 无直接等价命令"
        if entry.hint:
            msg += f"；替代：{entry.hint}"
        elif fb:
            msg += f"；PowerShell 可用：{fb}"
        caveats.append(msg)
        return SegResult(seg, "", None, "unsupported", "unsupported", caveats)

    # 3) flag / 参数级翻译
    if entry.universal:
        # 同一个二进制在三平台都跑（git/curl/docker/ssh）：参数语义一致，
        # 硬翻参数只会把 -fsSL 这种正确写法丢掉
        out_args, fcaveats, unknown, dropped = list(args), [], False, set()
    else:
        out_args, fcaveats, unknown, dropped = translate_flags(
            args, entry, src_key, tgt_key, tgt, shell_tgt)
    caveats.extend(fcaveats)
    if dropped & DANGEROUS_FLAGS:
        caveats.append(f"丢弃的参数 {', '.join(sorted(dropped & DANGEROUS_FLAGS))} 会影响语义，"
                       f"不能只删参数了事")
        return SegResult(seg, "", None, "unsupported", "unsupported", caveats)

    converted: list[str] = []
    changed_path = False
    flag_tokens: set[str] = set()
    for f in entry.flags:
        v = f.get(tgt_key)
        if v:
            flag_tokens.update(v.split())
    for a in out_args:
        if a in flag_tokens:
            converted.append(a)
            continue
        if a == "":
            converted.append("''" if tgt != "windows" else '""')
            continue
        val = convert_token(a, src, tgt, style, caveats)
        if a in quoted_vals:
            val = force_quote(val, tgt)
        if val != a:
            changed_path = True
        converted.append(val)

    target_tokens = [tgt_name] + converted

    # 前缀回填
    if prefix:
        if tgt == "windows":
            for p in prefix:
                if p == "sudo":
                    caveats.append("Windows 无 sudo：需以管理员身份运行（Start-Process -Verb RunAs）")
                elif p in ("nohup",):
                    caveats.append("nohup 在 Windows 无等价：用 Start-Process 或计划任务")
                elif p in ("time", "command", "builtin", "env", "exec", "doas"):
                    pass
        else:
            target_tokens = prefix + target_tokens

    status = "approximate" if (unknown or caveats or changed_path) else "equivalent"
    # 直接可用 flag 且无转换 → 视为等价
    if not unknown and entry.direct_flags and all(
            a in entry.direct_flags or a.startswith("--") and a in entry.direct_flags for a in args):
        status = "equivalent"

    kind = "flags"
    if entry.raw.get("universal") and tgt_name == head:
        kind = "passthrough"
        status = "equivalent"
        caveats.append("三平台同名，仅需注意 shell 差异")

    return SegResult(seg, "", " ".join(target_tokens), status, kind, caveats, notes)


def split_flag_value(tok: str) -> tuple[str, str | None]:
    if tok.startswith("--") and "=" in tok:
        k, v = tok.split("=", 1)
        return k, v
    return tok, None


DANGEROUS_FLAGS = {"-delete", "-exec", "-execdir", "-rf", "-fr", "-9", "--delete", "-f"}


def translate_flags(args: list[str], entry: Entry, src_key: str, tgt_key: str,
                    tgt: str, shell_tgt: str) -> tuple[list[str], list[str], bool, set[str]]:
    """返回 (目标参数列表, caveats, 是否有未识别参数, 被丢弃的参数)。"""
    caveats: list[str] = []
    out: list[str] = []
    unknown = False
    dropped: set[str] = set()

    table: dict[str, dict] = {}
    for f in entry.flags:
        s = f.get(src_key)
        if not s:
            continue
        table[s] = f

    def target_tokens(f: dict) -> list[str]:
        t = f.get(tgt_key)
        if t is None:
            return []
        return t.split() if t else []

    for a in args:
        if a in table:
            tts = target_tokens(table[a])
            if not tts:
                caveats.append(f"参数 {a} 在目标平台无直接对应，已丢弃（{table[a].get('semantic','')}）")
            out.extend(tts)
            continue
        key, val = split_flag_value(a)
        if key != a and key in table:
            tts = target_tokens(table[key])
            if tts:
                out.extend(tts)
                if val is not None:
                    out.append(val)
            continue
        # POSIX 组合短参数 -la
        if src_key in ("linux", "macos") and re.fullmatch(r"-[A-Za-z]{2,}", a):
            expanded, ok, lost = [], True, []
            for ch in a[1:]:
                single = "-" + ch
                if single in table:
                    tts = target_tokens(table[single])
                    if tts:
                        expanded.extend(tts)
                    else:
                        lost.append(single)
                else:
                    ok = False
                    break
            if ok and lost:
                caveats.append(f"参数 {', '.join(lost)} 在目标平台无对应，已丢弃")
            if ok:
                # 全等映射时保留原组合写法（ls -la 不该被拆成 ls -l -a）
                if not lost and expanded == ["-" + ch for ch in a[1:]]:
                    out.append(a)
                else:
                    out.extend(expanded)
                continue
        # 未识别：-h/--help 这类通用参数直通
        if a in ("--help", "-h", "-?", "/?", "--version", "-V"):
            out.append(a)
            continue
        if a.startswith("-") and tgt == "windows" and shell_tgt == "cmd":
            caveats.append(f"参数 {a} 是 POSIX 风格，cmd.exe 不识别")
            unknown = True
            out.append(a)
            continue
        if (re.fullmatch(r"--?[A-Za-z][A-Za-z0-9_-]*(=.*)?", a) and tgt == "windows"
                and src_key in ("linux", "macos")):
            dropped.add(a)
            caveats.append(f"参数 {a} 在 {shell_tgt} 无对应，已丢弃 —— 请核对结果是否符合原意")
            continue
        if is_cmd_switch(a) and tgt != "windows" and not is_posix_path(a):
            caveats.append(f"参数 {a} 疑似 cmd 开关，POSIX 不识别")
            unknown = True
            out.append(a)
            continue
        out.append(a)
    return out, caveats, unknown, dropped


# ---------------------------------------------------------------------------
# recipe 示例字面量回流 & caveat 平台过滤
# ---------------------------------------------------------------------------

PLATFORM_WORDS = {
    "windows": re.compile(r"PowerShell|PS ?5\.1|\bcmd\.exe\b|Select-String|Get-[A-Z]\w+|"
                          r"Remove-Item|Copy-Item|Start-Process|注册表|回收站|Windows|Win32|"
                          r"icacls|winget|\bNUL\b|tasklist|findstr"),
    "macos": re.compile(r"macOS|OS X|BSD|launchctl|Homebrew|brew |plutil|shasum|diskutil|"
                        r"-j -f|ditto|\bpbpaste\b"),
    "linux": re.compile(r"Linux|GNU|systemd|systemctl|\bdnf\b|\bapt\b|/proc|/etc/|\byum\b|"
                        r"netstat|\biproute\b"),
}


def filter_caveats(caveats: list[str], target: str) -> list[str]:
    """只保留适用于 target 的 caveat。

    数据的 caveat 挂在 recipe 上（一条 recipe 服务三个目标平台），所以
    "Select-String 不区分大小写" 这种 Windows 专属提示不该出现在 macOS 结果里。
    只在一条 caveat 明确点名了别的平台时才丢，宁可多留也不误删。
    """
    out = []
    for c in caveats:
        mentioned = {p for p, rx in PLATFORM_WORDS.items() if rx.search(c)}
        if mentioned and target not in mentioned:
            continue
        out.append(c)
    return out


def same_command(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    ta = [t.strip("'\"") for t in tokenize(a)]
    tb = [t.strip("'\"") for t in tokenize(b)]
    return ta == tb


def preserve_trailing_slash(val: str, input_toks: list[str]) -> str:
    """输入是 app/，recipe 的目标平台写法是 app —— 目录语义要保留。"""
    for t in input_toks:
        if not t.endswith("/") or len(t) < 2:
            continue
        base = t.rstrip("/")
        if not base:
            continue
        pat = re.compile(r"(?<![\w./\\-])" + re.escape(base) + r"(?![\w./\\-])")
        val = pat.sub(base + "/", val)
    return val


def substitute_recipe_literals(val: str, recipe: dict, src_key: str,
                               input_toks: list[str]) -> str:
    """把 recipe 里的示例字面量换回用户实际输入。

    recipe 写的是 `grep -rn TODO src/`，用户输入的是 `grep -rn TODO src`，
    输出不该凭空多出一个 `src/`；`tar -xzf app.tar.gz` 也一样。
    仅按位置对齐替换非参数 token，且在单词边界处替换，避免误伤。
    """
    rt = tokenize(recipe.get(src_key) or "")
    if not rt or len(rt) != len(input_toks):
        return val
    for a, b in zip(rt, input_toks):
        if a == b or a.startswith("-") or b.startswith("-") or len(a) < 2:
            continue
        pattern = re.compile(r"(?<![\w./\\-])" + re.escape(a) + r"(?![\w/\\-])")
        val = pattern.sub(b.replace("\\", "\\\\"), val, count=1)
    return val


# ---------------------------------------------------------------------------
# 顶层翻译
# ---------------------------------------------------------------------------

@dataclass
class Translation:
    input: str
    source: str
    target: str
    shell: str
    status: str
    command: str | None
    segments: list[SegResult]
    caveats: list[str]
    inferred: bool = False
    notes: list[str] = field(default_factory=list)
    llm_used: bool = False

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "input": self.input,
            "source": self.source,
            "target": self.target,
            "shell": self.shell,
            "status": self.status,
            "command": self.command,
            "confidence": {"equivalent": "high", "approximate": "medium",
                           "unsupported": "none", "error": "none"}.get(self.status, "medium"),
            "inferred": self.inferred,
            "llm_used": self.llm_used,
            "caveats": self.caveats,
            "notes": self.notes,
            "segments": [
                {"input": s.input, "operator": s.operator, "command": s.command,
                 "status": s.status, "kind": s.kind, "caveats": s.caveats}
                for s in self.segments
            ],
        }


WILD = re.compile(r"\{[a-z_][a-z0-9_]*\}")


def match_pattern(template: str, tokens: list[str]) -> dict | None:
    """模板里 {name} 匹配恰好一个 token。返回捕获字典或 None。"""
    tl = tokenize(template)
    if len(tl) != len(tokens):
        return None
    caps: dict[str, str] = {}
    for t, v in zip(tl, tokens):
        if WILD.fullmatch(t):
            caps[t[1:-1]] = v
        elif t != v:
            return None
    return caps


def fill(template: str, caps: dict[str, str]) -> str:
    def rep(m):
        return caps.get(m.group(0)[1:-1], m.group(0))
    return WILD.sub(rep, template)


def try_patterns(cmd: str, src: str, tgt: str, shell: str, kb: KB) -> SegResult | None:
    tokens = tokenize(cmd)
    keys = [flag_key(src, None)]
    tkey = flag_key(tgt, shell)
    if src == "windows":
        keys = ["windows", "windows_cmd"]
    for pat in kb.patterns:
        for k in keys:
            tpl = pat.get(k)
            if not tpl:
                continue
            caps = match_pattern(tpl, tokens)
            if caps is None:
                continue
            out = pat.get(tkey)
            if out is None:
                return SegResult(cmd, "", None, "unsupported", "unsupported",
                                 list(pat.get("caveats", []) or [])
                                 + [f"{PLATFORM_LABEL[tgt]}/{shell} 下没有这条命令的等价写法"])
            return SegResult(cmd, "", fill(out, caps), pat.get("status", "equivalent"),
                             "pattern", list(pat.get("caveats", []) or []))
    return None


def detect_source(tokens_first: str, kb: KB, target: str) -> tuple[str, bool]:
    """猜源平台。返回 (platform, inferred)。"""
    cur = current_platform()
    if kb.find(tokens_first, cur, None):
        return cur, False
    hits = {p for p, _ in kb.find_any(tokens_first)}
    hits.discard(cur)
    if len(hits) == 1:
        return hits.pop(), True
    if hits:
        others = sorted(hits)
        return others[0], True
    return cur, True


def op_for(target_shell: str, op: str) -> tuple[str, list[str]]:
    caveats: list[str] = []
    if not op:
        return "", caveats
    fam = SHELL_FAMILY.get(target_shell, "posix")
    if fam == "powershell":
        if op in ("&&", "||"):
            caveats.append(f"PowerShell 5.1 不支持 `{op}`，需 PS7+ 或改写为 ; if ($?) {{ ... }}")
        if op in ("|&",):
            caveats.append("PowerShell 无 |&，用 2>&1 |")
            return "2>&1 |", caveats
    if fam == "cmd" and op in ("&&", "||"):
        if op == "||":
            caveats.append("cmd.exe 无 ||")
    return op, caveats


def translate(cmd: str, target: str, source: str | None, shell: str | None,
              kb: KB, style: str | None = None) -> Translation:
    target = normalize_platform(target) or target
    shell = (shell or DEFAULT_SHELL.get(target, "bash")).lower()
    if shell == "pwsh":
        shell = "powershell"
    style = path_style_of(target, style)

    parts = split_chain(cmd)
    if not parts:
        return Translation(cmd, source or current_platform(), target, shell,
                           "error", None, [], ["空命令"])

    inferred = False
    if not source:
        first_tok = tokenize(parts[0][1])[0] if tokenize(parts[0][1]) else ""
        source, inferred = detect_source(first_tok, kb, target)
    source = normalize_platform(source) or source
    if source == target and shell == DEFAULT_SHELL.get(target):
        # 同平台同 shell：仍走一遍，交给 recipe/flags 决定是否等价
        pass

    segs: list[SegResult] = []
    all_caveats: list[str] = []
    status = "equivalent"

    # 0) 整体管道惯用法优先（ps aux | grep x 这类）
    pat = try_patterns(cmd, source, target, shell, kb)
    if pat is not None:
        t = Translation(cmd, source, target, shell, pat.status, pat.command, [pat], pat.caveats, inferred)
        if inferred:
            t.notes.append(f"源平台未指定，按命令名推断为 {PLATFORM_LABEL[source]}")
        return t

    for op, text in parts:
        if not text:
            continue
        seg = translate_segment(text, source, target, shell, kb, style)
        seg.operator = op
        tgt_op, op_caveats = op_for(shell, op)
        seg.operator = tgt_op
        if op_caveats:
            seg.caveats.extend(op_caveats)
            if seg.status == "equivalent":
                seg.status = "approximate"
        segs.append(seg)
        all_caveats.extend(seg.caveats)
        status = worst(status, seg.status)

    # caveat 按目标平台过滤；若近似状态的唯一理由被过滤掉、且命令与原命令一致，
    # 说明这个方向其实是等价的（recipe 的 status 是三个平台共用的，会误伤）
    status = "equivalent"
    for s in segs:
        s.caveats = filter_caveats(s.caveats, target)
        if s.status == "approximate" and not s.caveats and same_command(s.command, s.input):
            s.status = "equivalent"
        status = worst(status, s.status)

    if any(s.status == "unsupported" for s in segs):
        status = "unsupported"

    if status == "unsupported":
        command = None
    else:
        chunks = []
        for s in segs:
            if s.command is None:
                continue
            if chunks and s.operator:
                chunks.append(s.operator)
            chunks.append(s.command)
        command = " ".join(chunks) if chunks else None

    # 过滤后重建（all_caveats 是过滤前的快照，不能直接用）
    all_caveats = [c for s in segs for c in s.caveats]

    # 去重 caveat，保序
    seen, uniq = set(), []
    for c in all_caveats:
        if c not in seen:
            seen.add(c)
            uniq.append(c)

    t = Translation(cmd, source, target, shell, status, command, segs, uniq, inferred)
    if inferred:
        t.notes.append(f"源平台未指定，按命令名推断为 {PLATFORM_LABEL[source]}")
    return t


# ---------------------------------------------------------------------------
# 可选 LLM 兜底
# ---------------------------------------------------------------------------

def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def llm_config() -> dict | None:
    load_env_file(Path("~/.ferry/.env").expanduser())
    key = (os.environ.get("FERRY_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
           or os.environ.get("OPENAI_API_KEY"))
    if not key:
        return None
    return {
        "key": key,
        "base": os.environ.get("FERRY_LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        "model": os.environ.get("FERRY_LLM_MODEL", "deepseek-chat"),
    }


LLM_SYSTEM = """You translate shell commands between Linux, macOS and Windows.
Reply with STRICT JSON only:
{"command": "<translated command or null>", "status": "equivalent|approximate|unsupported",
 "caveats": ["short Chinese note", ...]}
Rules: never invent flags; if unsure use "approximate" and explain; if impossible use
"unsupported" with command null and suggest an alternative in caveats.
Threads: Windows target means PowerShell unless told otherwise."""


def llm_translate(cmd: str, target: str, shell: str) -> dict | None:
    cfg = llm_config()
    if not cfg:
        return None
    payload = {
        "model": cfg["model"],
        "temperature": 0,
        "messages": [
            {"role": "system", "content": LLM_SYSTEM},
            {"role": "user", "content": f"From {current_platform()} to {target} ({shell}): {cmd}"},
        ],
    }
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['key']}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = json.loads(resp.read().decode())
        text = body["choices"][0]["message"]["content"].strip()
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        data = json.loads(text)
        return data
    except Exception as exc:  # noqa: BLE001
        print(f"[ferry] LLM 兜底失败: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

STATUS_BADGE = {
    "equivalent": ("等价", "green"),
    "approximate": ("近似等价", "yellow"),
    "unsupported": ("无等价", "red"),
    "error": ("错误", "red"),
}


def print_translation(t: Translation, explain: bool) -> None:
    label, col = STATUS_BADGE.get(t.status, ("?", "dim"))
    print(f"{color('输入', 'dim')}   {t.input}   {color('[' + PLATFORM_LABEL[t.source] + ']', 'dim')}")
    print(f"{color('目标', 'dim')}   {PLATFORM_LABEL[t.target]} / {t.shell}")
    print(f"{color('状态', 'dim')}   {color(label, col)} ({t.status})")
    print()
    if t.command:
        print("  " + color(t.command, "bold"))
    else:
        print("  " + color("—— 无法确定性翻译 ——", "red"))
    if t.segments and len([s for s in t.segments if s.input]) > 1:
        print()
        print(color("  分段：", "dim"))
        for s in t.segments:
            print(f"    {s.operator + ' ' if s.operator else ''}{s.input}")
            print(f"      → {s.command if s.command else '（无）'}  [{s.status}]")
    if t.caveats:
        print()
        print(color("注意事项", "yellow"))
        for c in t.caveats:
            print(f"  • {c}")
    if t.notes:
        print()
        for n in t.notes:
            print(color(f"  · {n}", "dim"))
    if explain:
        print()
        print(color("判定过程", "cyan"))
        for s in t.segments:
            print(f"  {s.input!r}: kind={s.kind} status={s.status}")
        print(color("  规则：recipe 精确命中 > 参数映射 > 同名直通 > 无等价", "dim"))


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def cmd_translate(args, kb: KB) -> int:
    cmd = " ".join(args.command) if isinstance(args.command, list) else (args.command or "")
    if args.stdin:
        lines = [ln.rstrip("\n") for ln in sys.stdin if ln.strip()]
        out = []
        rc = 0
        for ln in lines:
            t = translate(ln, args.to, args.from_os, args.shell, kb, args.path_style)
            if t.status == "unsupported" and args.llm:
                t = llm_merge(t, kb)
            rc = max(rc, EXIT_CODE.get(t.status, 4))
            out.append(t)
        if args.json:
            print(json.dumps([t.to_dict() for t in out], ensure_ascii=False, indent=2))
        else:
            for t in out:
                print_translation(t, args.explain)
                print()
        return rc

    if not cmd:
        print("缺少命令：ferry translate 'ls -la' --to windows（或用 --stdin 逐行读）",
              file=sys.stderr)
        return 4
    t = translate(cmd, args.to, args.from_os, args.shell, kb, args.path_style)
    if t.status == "unsupported" and args.llm:
        t = llm_merge(t, kb)
    if args.json:
        print(json.dumps(t.to_dict(), ensure_ascii=False, indent=2))
    else:
        print_translation(t, args.explain)
    return EXIT_CODE.get(t.status, 4)


def llm_merge(t: Translation, kb: KB) -> Translation:
    data = llm_translate(t.input, t.target, t.shell)
    if not data:
        return t
    status = data.get("status", "approximate")
    if status not in STATUS_ORDER:
        status = "approximate"
    t.command = data.get("command")
    t.status = status if t.command else "unsupported"
    t.inferred = True
    t.llm_used = True
    t.caveats = ["（LLM 推断，未写入映射库，请人工核对）"] + list(data.get("caveats") or [])
    for s in t.segments:
        s.command = t.command if len(t.segments) == 1 else s.command
        s.kind = "llm" if len(t.segments) == 1 else s.kind
    return t


def cmd_lookup(args, kb: KB) -> int:
    entry = kb.find(args.name, current_platform(), None)
    if not entry:
        hits = kb.find_any(args.name)
        entry = hits[0][1] if hits else None
    if not entry:
        print(f"未收录：{args.name}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(entry.raw, ensure_ascii=False, indent=2))
        return 0
    print(color(f"{entry.id}", "bold"), "—", entry.summary, color(f"[{entry.category}]", "dim"))
    print()
    print(f"  {'Linux':<10} {entry.names.get('linux') or '—'}")
    print(f"  {'macOS':<10} {entry.names.get('macos') or '—'}")
    print(f"  {'Windows':<10} {entry.names.get('windows') or '—'} (PowerShell)")
    if entry.names.get("windows_cmd"):
        print(f"  {'':<10} {entry.names['windows_cmd']} (cmd.exe)")
    if entry.recipes:
        print()
        print(color("  常用写法", "cyan"))
        for r in entry.recipes:
            st = r.get("status", "equivalent")
            label, col = STATUS_BADGE.get(st, ("?", "dim"))
            print(f"    {color(r.get('desc',''), 'bold')}  {color('[' + label + ']', col)}")
            for key, lab in (("linux", "Linux"), ("macos", "macOS"),
                             ("windows", "Win/PS"), ("windows_cmd", "Win/cmd")):
                if r.get(key):
                    print(f"      {lab:<8} {r[key]}")
            for c in r.get("caveats", []) or []:
                print(color(f"      · {c}", "dim"))
    return 0


def cmd_prompt_export(args, kb: KB) -> int:
    target = normalize_platform(args.to) or args.to
    shell = (args.shell or DEFAULT_SHELL.get(target)).lower()
    key = flag_key(target, shell)
    lines: list[str] = []
    lines.append(f"# {PLATFORM_LABEL[target]} 命令速查（由 ferry 生成，shell={shell}）")
    lines.append("")
    cats: dict[str, list[str]] = {}
    for e in kb.entries:
        if args.category and e.category != args.category:
            continue
        rows = []
        used = 0
        for r in e.recipes:
            v = r.get(key)
            if not v:
                continue
            st = r.get("status", "equivalent")
            mark = "" if st == "equivalent" else ("≈" if st == "approximate" else "✗")
            if args.compact:
                if used >= args.per_command:
                    break
                used += 1
                label = f"{r.get('desc', e.summary)}: " if used == 1 else "  "
                rows.append(f"- {label}`{v}`{(' ' + mark) if mark else ''}")
            else:
                rows.append(f"- {r.get('desc', e.summary)}: `{v}`{(' ' + mark) if mark else ''}")
        if rows:
            cats.setdefault(e.category, []).extend(rows)
    for cat, rows in cats.items():
        lines.append(f"## {CATEGORY_LABELS.get(cat, cat)}")
        lines.extend(rows)
        lines.append("")
    if kb.syntax and not args.category:
        lines.append("## Shell 语法差异")
        for s in kb.syntax:
            v = s.get(key)
            if not v:
                continue
            lines.append(f"- {s.get('summary','')}: `{v}`")
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    if args.max_chars and len(text) > args.max_chars:
        cut = text[:args.max_chars]
        cut = cut[:cut.rfind("\n")]
        text = cut + "\n…（已截断，用 ferry prompt-export 取全文）\n"
    if args.json:
        print(json.dumps({"target": target, "shell": shell, "text": text}, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(text)
    return 0


def cmd_syntax(args, kb: KB) -> int:
    target = normalize_platform(args.to) or args.to
    shell = (args.shell or DEFAULT_SHELL.get(target)).lower()
    key = flag_key(target, shell)
    if args.json:
        print(json.dumps(kb.syntax, ensure_ascii=False, indent=2))
        return 0
    print(color(f"POSIX ↔ {PLATFORM_LABEL[target]} 语法差异", "bold"))
    print()
    for s in kb.syntax:
        status = s.get("status", "approximate")
        label, col = STATUS_BADGE.get(status, ("?", "dim"))
        print(f"{color(s.get('summary',''), 'bold')}  {color('[' + label + ']', col)}")
        print(f"  posix  {s.get('linux') or s.get('macos') or '—'}")
        print(f"  target {s.get(key) or '—'}")
        if s.get("note"):
            print(color(f"  · {s['note']}", "dim"))
        print()
    return 0


def cmd_pitfalls(args, kb: KB) -> int:
    target = normalize_platform(args.to) or args.to
    rows = [p for p in kb.pitfalls if target in (p.get("platforms") or [])] or kb.pitfalls
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(color(f"跨平台已知坑（{PLATFORM_LABEL[target]}）", "bold"))
    print()
    for p in rows:
        print(f"{color('• ' + p.get('title',''), 'yellow')}")
        if p.get("detail"):
            print(textwrap.indent(p["detail"], "    "))
        if p.get("fix"):
            print(color(textwrap.indent("修正: " + p["fix"], "    "), "dim"))
        print()
    return 0


def cmd_doctor(args, kb: KB) -> int:
    problems = list(kb.errors)
    warns: list[str] = []
    valid_status = set(STATUS_ORDER) - {"error"}
    for e in kb.entries:
        if not e.summary:
            warns.append(f"{e.id}: 缺少 summary")
        if not any(e.names.get(p) for p in ("linux", "macos", "windows", "windows_cmd")):
            problems.append(f"{e.id}: 没有任何平台命令名")
        for r in e.recipes:
            st = r.get("status", "equivalent")
            if st not in valid_status:
                problems.append(f"{e.id}: recipe 状态非法 {st}")
            if not r.get("desc"):
                warns.append(f"{e.id}: recipe 缺少 desc")
            if not any(r.get(k) for k in ("linux", "macos", "windows", "windows_cmd")):
                problems.append(f"{e.id}: 空 recipe")
        for f in e.flags:
            if not f.get("semantic"):
                warns.append(f"{e.id}: flag 缺少 semantic")
    # 覆盖统计
    cov = {p: sum(1 for e in kb.entries if e.names.get(p) or
                  (p == "windows" and e.names.get("windows_cmd"))) for p in PLATFORMS}

    if args.live:
        cur = current_platform()
        missing = []
        for e in kb.entries:
            nm = e.exact_name_for(cur, None)
            if not nm:
                continue
            head = nm.split()[0]
            if not shutil.which(head):
                missing.append(f"{e.id} → {head}")
        if missing:
            warns.append(f"本机未找到 {len(missing)} 个命令：{', '.join(missing[:15])}"
                         + (" …" if len(missing) > 15 else ""))

    report = {
        "ok": not problems,
        "entries": len(kb.entries),
        "coverage": cov,
        "problems": problems,
        "warnings": warns,
        "index_aliases": len(kb.collisions),
        "data_dir": kb.loaded_from[0] if kb.loaded_from else None,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(color("ferry doctor", "bold"))
        print(f"  数据目录  {report['data_dir']}")
        print(f"  条目      {len(kb.entries)}")
        print(f"  覆盖      " + "  ".join(f"{PLATFORM_LABEL[p]} {cov[p]}" for p in PLATFORMS))
        if kb.drills:
            n_cur = sum(1 for d in kb.drills if d.get("platform") == current_platform())
            print(f"  验证用例  {len(kb.drills)} 条（当前平台 {n_cur} 条，ferry drills --to "
                  f"{current_platform()} --run）")
        if problems:
            print(color(f"\n  错误 {len(problems)} 项", "red"))
            for p in problems:
                print("   ✗ " + p)
        if warns:
            print(color(f"\n  提示 {len(warns)} 项", "yellow"))
            for w in warns:
                print("   · " + w)
        if kb.collisions:
            shown = kb.collisions if args.verbose else []
            print(color(f"\n  索引同名 {len(kb.collisions)} 处"
                        f"{'（同平台同一命令名对应多个条目，已按优先级取一个）' if not shown else ''}",
                        "dim"))
            for w in shown[:60]:
                print(color("   · " + w, "dim"))
        if not problems:
            print(color("\n  ✓ 数据完整性检查通过", "green"))
    return 0 if not problems else 1


def _drill_script_posix(drills: list[dict], target: str) -> str:
    L = [
        "#!/bin/sh",
        f"# ferry drills — 在 {PLATFORM_LABEL[target]} 上验证 ferry 的跨平台断言",
        "# 每条用例都在独立临时目录内执行，不碰你的文件。用法：sh this.sh",
        "pass=0; fail=0",
        r'ok() { pass=$((pass+1)); printf "OK   %s\n" "$1"; }',
        r'ng() { fail=$((fail+1)); printf "FAIL %s\n" "$1"; }',
        "",
    ]
    for d in drills:
        expect = d.get("expect", "success")
        did = d["id"]
        cmd = d["cmd"]
        L.append(f'# --- {did}: {d.get("summary", "")}')
        L.append('d=$(mktemp -d); cd "$d" || exit 1')
        if d.get("setup"):
            L.append(f"{{ {d['setup']} ; }} >/dev/null 2>&1")
        L.append(f"if sh -c {json.dumps(cmd, ensure_ascii=False)} >/dev/null 2>&1; "
                 f"then got=success; else got=fail; fi")
        if expect == "any":
            L.append(f'echo "INFO {did} got=$got"')
        else:
            L.append(f'if [ "$got" = "{expect}" ]; then ok {json.dumps(did, ensure_ascii=False)}; '
                     f'else ng {json.dumps(did + " (expect " + expect + ", got " + "$got" + ")", ensure_ascii=False)}; fi')
        L.append('cd /; rm -rf "$d"')
        L.append("")
    L += ['echo', 'echo "通过 $pass，失败 $fail"',
          '[ "$fail" -eq 0 ] && exit 0 || exit 2']
    return "\n".join(L)


def _drill_script_pwsh(drills: list[dict], target: str) -> str:
    L = [
        "# ferry drills — 在 Windows 上验证 ferry 的跨平台断言",
        "# 用法：powershell -ExecutionPolicy Bypass -File this.ps1",
        "$pass = 0; $fail = 0",
        "",
    ]
    for d in drills:
        expect = d.get("expect", "success")
        did = d["id"]
        L.append(f'# --- {did}: {d.get("summary", "")}')
        L.append('$d = Join-Path $env:TEMP ("ferry-" + [guid]::NewGuid())')
        L.append("New-Item -ItemType Directory $d -Force | Out-Null")
        L.append("Push-Location $d")
        if d.get("setup"):
            L.append(f'try {{ {d["setup"]} }} catch {{}}')
        L.append("try {")
        L.append(f"  Invoke-Expression {json.dumps(d['cmd'], ensure_ascii=False)} | Out-Null")
        L.append('  $got = "success"')
        L.append('} catch { $got = "fail" }')
        L.append("Pop-Location; Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue")
        if expect == "any":
            L.append(f'Write-Host "INFO {did} got=$got"')
        else:
            L.append(f'if ($got -eq "{expect}") {{ $pass++; Write-Host "OK   {did}" }}'
                     f' else {{ $fail++; Write-Host "FAIL {did} (expect {expect}, got $got)" }}')
        L.append("")
    L += ['Write-Host ""', 'Write-Host "通过 $pass，失败 $fail"',
          'exit $(if ($fail -eq 0) { 0 } else { 2 })']
    return "\n".join(L)


def cmd_drills(args, kb: KB) -> int:
    target = normalize_platform(args.to) or args.to
    rows = [d for d in kb.drills if d.get("platform") == target]
    if not rows:
        print(f"{PLATFORM_LABEL[target]} 暂无验证用例", file=sys.stderr)
        return 3

    if args.emit:
        script = (_drill_script_pwsh(rows, target) if args.emit == "ps1"
                  else _drill_script_posix(rows, target))
        sys.stdout.write(script + "\n")
        return 0

    if args.run:
        if target != current_platform():
            print(f"只支持在当前平台运行；当前 {PLATFORM_LABEL[current_platform()]}，"
                  f"目标 {PLATFORM_LABEL[target]}。请用 --emit 在目标机上跑。", file=sys.stderr)
            return 4
        import subprocess, tempfile
        fails = 0
        for d in rows:
            with tempfile.TemporaryDirectory() as td:
                cmd = d["cmd"]
                if d.get("setup"):
                    subprocess.run(d["setup"], shell=True, cwd=td,
                                   capture_output=True, text=True)
                proc = subprocess.run(cmd, shell=True, cwd=td, capture_output=True, text=True)
            got = "success" if proc.returncode == 0 else "fail"
            want = d.get("expect", "success")
            if want == "any":
                # 环境相关项（装了 GNU coreutils 就会变）：只记录，不影响通过与否
                first = (proc.stdout or "").strip().splitlines()[:1]
                extra = f" [{first[0][:60]}]" if first else ""
                print(f"{color('INFO', 'dim')} {d['id']:<28} 实际 {got:<7} {d.get('summary','')}{extra}")
                continue
            mark = color("OK  ", "green") if got == want else color("FAIL", "red")
            print(f"{mark} {d['id']:<28} 期望 {want:<7} 实际 {got:<7} {d.get('summary','')}")
            if got != want:
                fails += 1
                if proc.stderr.strip():
                    print(color("      " + proc.stderr.strip().splitlines()[0], "dim"))
        print()
        print(f"通过 {len(rows) - fails} / {len(rows)}" if not fails
              else color(f"失败 {fails} / {len(rows)}", "yellow"))
        return 0 if fails == 0 else 2

    print(color(f"{PLATFORM_LABEL[target]} 验证用例（{len(rows)} 条）", "bold"))
    print()
    for d in rows:
        print(f"  {d['id']:<28} 期望 {d.get('expect','success'):<7} {d.get('summary','')}")
        print(color(f"      $ {d['cmd']}", "dim"))
    print()
    print("在本机跑：ferry drills --to " + target + " --run")
    print("生成脚本给目标机：ferry drills --to " + target +
          (" --emit ps1" if target == "windows" else " --emit sh"))
    return 0


def cmd_stats(args, kb: KB) -> int:
    s = kb.stats()
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    print(color("ferry 映射库", "bold"))
    print(f"  命令条目  {s['entries']}")
    print(f"  写法样例  {s['recipes']}")
    print(f"  语法条目  {s['syntax']}")
    print(f"  管道惯用  {s.get('patterns', 0)}")
    print(f"  已知坑    {s['pitfalls']}")
    print(f"  真机断言  {s.get('drills', 0)}")
    print()
    for cat, n in sorted(s["categories"].items(), key=lambda kv: -kv[1]):
        print(f"  {CATEGORY_LABELS.get(cat, cat):<12} {n}")
    return 0


def cmd_platforms(args, kb: KB) -> int:
    cur = current_platform()
    print(f"当前平台：{PLATFORM_LABEL[cur]} ({cur})")
    print(f"自动检测 shell：{DEFAULT_SHELL[cur]}")
    print()
    for p in PLATFORMS:
        print(f"  {PLATFORM_LABEL[p]:<8} shells: {', '.join(DEFAULT_SHELL[p] for _ in [0])}"
              f"{' + cmd' if p == 'windows' else ''}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ferry",
        description="ferry — 摆渡：Linux / macOS / Windows 命令互译（离线优先，agent 友好）",
        epilog="退出码：0 等价 / 2 近似需注意 / 3 无等价 / 4 错误",
    )
    p.add_argument("--version", action="version", version=f"ferry {VERSION}")
    sub = p.add_subparsers(dest="sub")

    t = sub.add_parser("translate", aliases=["t"], help="翻译命令")
    t.add_argument("command", nargs="*", help="要翻译的命令（可带管道/&&/;）")
    t.add_argument("--to", required=True, help="目标平台：linux|macos|windows")
    t.add_argument("--from", dest="from_os", help="源平台（缺省自动推断）")
    t.add_argument("--shell", help="目标 shell：bash|zsh|powershell|cmd")
    t.add_argument("--json", action="store_true", help="JSON 输出（agent 用）")
    t.add_argument("--explain", action="store_true", help="显示判定过程")
    t.add_argument("--stdin", action="store_true", help="从 stdin 逐行读取命令")
    t.add_argument("--llm", action="store_true", help="查不到时用 LLM 兜底（需 API key）")
    t.add_argument("--path-style", default=None, choices=["wsl", "msys"],
                   help="Windows 盘符在 POSIX 下的写法（默认 wsl: /mnt/c）")
    t.set_defaults(func=cmd_translate)

    lk = sub.add_parser("lookup", help="查命令在三平台的对应写法")
    lk.add_argument("name")
    lk.add_argument("--json", action="store_true")
    lk.set_defaults(func=cmd_lookup)

    pe = sub.add_parser("prompt-export", help="导出目标平台速查摘要（注入 agent system prompt）")
    pe.add_argument("--to", required=True)
    pe.add_argument("--shell")
    pe.add_argument("--category")
    pe.add_argument("--max-chars", type=int, default=0)
    pe.add_argument("--compact", action="store_true", help="只列命令，省略说明（更省 token）")
    pe.add_argument("--per-command", type=int, default=2, help="compact 模式下每条命令最多几条写法")
    pe.add_argument("--json", action="store_true")
    pe.set_defaults(func=cmd_prompt_export)

    sy = sub.add_parser("syntax", help="shell 语法差异对照")
    sy.add_argument("--to", required=True)
    sy.add_argument("--shell")
    sy.add_argument("--json", action="store_true")
    sy.set_defaults(func=cmd_syntax)

    pf = sub.add_parser("pitfalls", help="跨平台已知坑")
    pf.add_argument("--to", required=True)
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(func=cmd_pitfalls)

    dr = sub.add_parser("doctor", help="数据完整性检查")
    dr.add_argument("--live", action="store_true", help="检查本机命令是否真实存在")
    dr.add_argument("--verbose", action="store_true", help="列出索引同名详情")
    dr.add_argument("--json", action="store_true")
    dr.set_defaults(func=cmd_doctor)

    dl = sub.add_parser("drills", help="跨平台断言验证用例（真机自证）")
    dl.add_argument("--to", required=True)
    dl.add_argument("--run", action="store_true", help="在当前平台执行")
    dl.add_argument("--emit", choices=["sh", "ps1"], help="生成可在目标机运行的脚本")
    dl.set_defaults(func=cmd_drills)

    hs = sub.add_parser("hosts", help="远程主机登记（ferry ssh 用）")
    hs.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove"])
    hs.add_argument("alias", nargs="?")
    hs.add_argument("platform", nargs="?")
    hs.add_argument("--user")
    hs.add_argument("--port", type=int)
    hs.add_argument("--shell")
    hs.add_argument("--note")
    hs.add_argument("--json", action="store_true")
    hs.set_defaults(func=cmd_hosts)

    sh = sub.add_parser("ssh", help="翻译后通过 ssh 执行（默认 dry-run）")
    sh.add_argument("host")
    sh.add_argument("command", nargs=argparse.REMAINDER)
    sh.add_argument("--to", help="目标平台（默认读 hosts 配置）")
    sh.add_argument("--from", dest="from_os")
    sh.add_argument("--shell")
    sh.add_argument("--path-style", default="wsl")
    sh.add_argument("--run", action="store_true", help="真的执行（默认只打印）")
    sh.add_argument("--json", action="store_true")
    sh.add_argument("--explain", action="store_true")
    sh.add_argument("--timeout", type=int, default=60)
    sh.set_defaults(func=cmd_ssh)

    st = sub.add_parser("stats", help="映射库统计")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_stats)

    pl = sub.add_parser("platforms", help="平台与 shell 信息")
    pl.set_defaults(func=cmd_platforms)
    return p


# ---------------------------------------------------------------- 远程主机（ssh）

HOSTS_PATH = os.path.expanduser("~/.ferry/hosts.json")


def load_hosts() -> dict:
    """主机别名 → {platform, shell, user, note}。查找顺序：$FERRY_HOSTS → ~/.ferry/hosts.json。"""
    for path in (os.environ.get("FERRY_HOSTS"), HOSTS_PATH):
        if not path:
            continue
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as f:
                return (json.load(f) or {}).get("hosts", {}) or {}
        except FileNotFoundError:
            continue
        except Exception as e:  # 配置坏了要说，不要静默
            print(f"[ferry] 读取主机配置失败 {path}: {e}", file=sys.stderr)
    return {}


def save_hosts(hosts: dict) -> None:
    os.makedirs(os.path.dirname(HOSTS_PATH), exist_ok=True)
    with open(HOSTS_PATH, "w", encoding="utf-8") as f:
        json.dump({"hosts": hosts}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def windows_encoded_command(cmd: str) -> str:
    """PowerShell -EncodedCommand 用 UTF-16LE base64，绕开 ssh → cmd → powershell 三层引号地狱。"""
    return base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")


def wrap_for_remote(cmd: str, platform: str, shell: str | None) -> str:
    """把目标平台的命令包成"远端 shell 能正确解析"的形式。"""
    if platform == "windows":
        if (shell or "powershell") == "cmd":
            return f'cmd /c "{cmd}"'
        return f"powershell -NoProfile -EncodedCommand {windows_encoded_command(cmd)}"
    return cmd


def ssh_argv(host: str, remote_cmd: str, user: str | None, port: int | None,
             extra: list[str] | None = None) -> list[str]:
    """argv 形式调用 ssh —— 命令作为单个参数传入，本地不过 shell，天然免引号问题。"""
    argv = ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes"]
    if port:
        argv += ["-p", str(port)]
    if extra:
        argv += extra
    argv.append(f"{user}@{host}" if user and "@" not in host else host)
    argv.append(remote_cmd)
    return argv


def cmd_hosts(args, kb: KB) -> int:
    hosts = load_hosts()
    if args.action == "add":
        if not args.alias or not args.platform:
            print("用法：ferry hosts add <别名> <linux|macos|windows> [--user U] [--port 22] [--note 备注]",
                  file=sys.stderr)
            return 4
        hosts[args.alias] = {
            "platform": normalize_platform(args.platform),
            "shell": args.shell,
            "user": args.user,
            "port": args.port,
            "note": args.note,
        }
        hosts[args.alias] = {k: v for k, v in hosts[args.alias].items() if v is not None}
        save_hosts(hosts)
        print(f"已登记 {args.alias} → {args.platform}（{HOSTS_PATH}）")
        return 0
    if args.action == "remove":
        if args.alias in hosts:
            hosts.pop(args.alias)
            save_hosts(hosts)
            print(f"已移除 {args.alias}")
            return 0
        print(f"没有这个别名：{args.alias}", file=sys.stderr)
        return 4

    if args.json:
        print(json.dumps({"schema": SCHEMA, "config": HOSTS_PATH, "hosts": hosts},
                         ensure_ascii=False, indent=2))
        return 0
    if not hosts:
        print(f"还没有登记远程主机。配置文件：{HOSTS_PATH}")
        print("加一台：ferry hosts add macbook macos --user dev")
        return 0
    print(f"远程主机（{HOSTS_PATH}）")
    print()
    for alias, info in sorted(hosts.items()):
        plat = PLATFORM_LABEL.get(info.get("platform"), info.get("platform"))
        extra = " ".join(f"{k}={v}" for k, v in info.items() if k != "platform" and v)
        print(f"  {alias:<12} {plat:<8} {extra}")
    print()
    print('dry-run：ferry ssh macbook "ls -la"        真执行：加 --run')
    return 0


def cmd_ssh(args, kb: KB) -> int:
    hosts = load_hosts()
    info = hosts.get(args.host) or {}
    target = normalize_platform(args.to or info.get("platform"))
    cmd_tokens = list(args.command) if isinstance(args.command, list) else [args.command or ""]
    # command 用 REMAINDER 吞掉后面所有 token，所以尾部的 --run/--json 要在这里认出来
    for flag in ("--run", "--json", "--explain", "--dry-run"):
        while flag in cmd_tokens:
            cmd_tokens.remove(flag)
            if flag == "--dry-run":
                args.run = False
            else:
                setattr(args, flag.lstrip("-"), True)
    cmd = " ".join(t for t in cmd_tokens if t)
    if not cmd:
        print('缺少命令：ferry ssh macbook "ls -la"', file=sys.stderr)
        return 4
    if not target:
        print(f"不知道 {args.host} 是什么系统。登记一下，或加 --to：\n"
              f"  ferry hosts add {args.host} macos --user dev\n"
              f"  ferry ssh {args.host} \"{cmd}\" --to windows", file=sys.stderr)
        return 4

    shell = args.shell or info.get("shell")
    t = translate(cmd, target, args.from_os or current_platform(), shell, kb, args.path_style)
    remote = wrap_for_remote(t.command, target, shell) if t.command else None
    argv = ssh_argv(args.host, remote, info.get("user"), info.get("port")) if remote else None

    rc = EXIT_CODE.get(t.status, 3)
    if args.json:
        print(json.dumps({
            "schema": SCHEMA, "host": args.host, "target": target, "shell": shell or DEFAULT_SHELL.get(target),
            "status": t.status, "command": t.command, "caveats": t.caveats,
            "ssh_argv": argv, "executed": False if not args.run else True,
        }, ensure_ascii=False, indent=2))

    if not args.run:
        if not args.json:
            print_translation(t, args.explain)
            if remote:
                shown = " ".join(shlex.quote(a) for a in argv) if argv else ""
                print()
                print(color("远端执行（dry-run，未执行）", "bold"))
                print("  " + shown)
                if target == "windows":
                    print(color("  PowerShell 走 -EncodedCommand，避免三层引号被吃掉", "dim"))
                print(color("  确认无误后加 --run", "dim"))
        return rc

    if t.status == "unsupported" or not remote:
        print("目标平台无等价命令，拒绝执行（--run 也不会执行 unsupported）", file=sys.stderr)
        for c in t.caveats:
            print("  • " + c, file=sys.stderr)
        return 3

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        print(f"ssh {args.host} 超时（{args.timeout}s）", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("找不到 ssh 客户端", file=sys.stderr)
        return 4
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        print(f"[ferry] ssh 退出码 {proc.returncode}", file=sys.stderr)
        return 2
    if t.status == "approximate":
        print(color("\n[提示] 本次为近似等价翻译，差异见上", "yellow"), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "sub", None):
        parser.print_help()
        return 0
    kb = KB()
    kb.load()
    if kb.errors:
        for e in kb.errors:
            print(f"[ferry] 数据错误: {e}", file=sys.stderr)
    try:
        return args.func(args, kb)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
