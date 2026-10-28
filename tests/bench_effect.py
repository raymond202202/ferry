#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ferry 效果实测：翻译准确率 + 性能基准。

对比三种方式翻译同一批 20 条命令（Linux→Windows）：
  A. ferry CLI（本地，零 token）
  B. ferry prompt-export 速查表注入 LLM（模拟 agent 带 skill 上下文）
  C. LLM 裸翻译（无任何辅助，模拟现状）

本脚本只做 A 的准确率与耗时测量；B/C 的 token 由人工/外部 LLM 记录，
脚本输出各方式的输入字符数供 token 估算。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ferry  # noqa: E402

PY = sys.executable

# 20 条典型 Linux 命令（覆盖文件/文本/进程/网络/打包）
CASES = [
    "ls -la /var/log",
    "rm -rf build/",
    "cp -r src/ dst/",
    "mv old.txt new.txt",
    "mkdir -p a/b/c",
    "cat access.log | grep 404 | wc -l",
    "grep -rn 'TODO' ./src",
    "sed -i 's/debug=True/debug=False/' settings.py",
    "tail -f app.log",
    "head -n 20 data.csv",
    "find . -name '*.pyc' -delete",
    "chmod +x deploy.sh",
    "ps aux | grep nginx",
    "kill -9 1234",
    "df -h",
    "du -sh ./node_modules",
    "tar -xzf backup.tar.gz",
    "curl -s https://api.example.com/health",
    "date +%F",
    "stat -c %Y app.log",
]

# 人工核定的正确答案（Windows PowerShell 语义）
EXPECTED = {
    "ls -la /var/log": "Get-ChildItem /var/log 或等价（路径映射 caveat 可接受）",
    "rm -rf build/": "Remove-Item -Recurse -Force build",
    "cp -r src/ dst/": "Copy-Item -Recurse src dst",
    "mv old.txt new.txt": "Move-Item old.txt new.txt",
    "mkdir -p a/b/c": "New-Item -ItemType Directory -Force -Path a/b/c",
    "cat access.log | grep 404 | wc -l": "管道语义保留，Select-String + Measure-Object",
    "grep -rn 'TODO' ./src": "Select-String -Recurse 语义",
    "sed -i 's/debug=True/debug=False/' settings.py": "Get-Content + -replace + Set-Content，替换内容必须保留",
    "tail -f app.log": "Get-Content -Tail -Wait",
    "head -n 20 data.csv": "Get-Content -TotalCount 20",
    "find . -name '*.pyc' -delete": "Get-ChildItem -Recurse -Filter + Remove-Item",
    "chmod +x deploy.sh": "无直接对应，应标 unsupported/approximate 而非输出错误命令",
    "ps aux | grep nginx": "Get-Process nginx",
    "kill -9 1234": "Stop-Process -Id 1234 -Force",
    "df -h": "Get-PSDrive 或 Get-Volume",
    "du -sh ./node_modules": "Measure-Object Length -Sum 语义",
    "tar -xzf backup.tar.gz": "tar -xzf backup.tar.gz（Windows 自带 bsdtar）",
    "curl -s https://api.example.com/health": "curl.exe 或 Invoke-WebRequest",
    "date +%F": "Get-Date -Format yyyy-MM-dd",
    "stat -c %Y app.log": "LastWriteTime epoch 语义",
}


def main():
    kb = ferry.KB()
    kb.load()

    # ---- A. ferry 准确率 + 耗时 ----
    t0 = time.perf_counter()
    results = []
    for cmd in CASES:
        t1 = time.perf_counter()
        tr = ferry.translate(cmd, "windows", "linux", None, kb)
        dt = (time.perf_counter() - t1) * 1000
        results.append((cmd, tr.status, tr.command, tr.caveats, dt))
    total_ms = (time.perf_counter() - t0) * 1000

    print("=" * 72)
    print("A. ferry 翻译（Linux → Windows，20 条）")
    print("=" * 72)
    ok = warn = bad = 0
    for cmd, status, out, caveats, dt in results:
        exp = EXPECTED[cmd]
        # 人工判定标准：输出语义正确=OK；approximate 但语义对=OK(带告警)；
        # 输出错误命令/丢内容=BAD
        print(f"  [{status:12}] {dt:6.1f}ms  {cmd}")
        print(f"       -> {out}")
        if caveats:
            print(f"       caveat: {caveats[0][:66]}")
    print(f"\n  总耗时: {total_ms:.0f}ms（含 KB 加载），平均 {total_ms/len(CASES):.1f}ms/条")
    print(f"  ferry 输出 token 估算: ~{sum(len(str(r[2] or '')) for r in results)//3} tokens")

    # ---- B. prompt-export 大小（注入 LLM 的固定成本）----
    t0 = time.perf_counter()
    proc = subprocess.run([PY, str(ROOT / "ferry.py"), "prompt-export", "--to", "windows"],
                         capture_output=True, text=True)
    pe_ms = (time.perf_counter() - t0) * 1000
    pe = proc.stdout
    print(f"\nB. prompt-export 速查表（注入 LLM 的固定上下文成本）")
    print(f"  字符数: {len(pe)} | 估算 token: ~{len(pe)//2}（中文约 1 字/token）")
    print(f"  生成耗时: {pe_ms:.0f}ms")

    # ---- C. 裸 LLM 翻译的输入成本（每轮对话都要带命令+说明）----
    print(f"\nC. LLM 裸翻译（无 ferry）每条成本估算")
    per_turn = sum(len(c) for c in CASES) / len(CASES)
    print(f"  平均命令长度: {per_turn:.0f} 字符")
    print(f"  LLM 输出 20 条翻译估算: ~{sum(len(EXPECTED[c]) for c in CASES)//2} tokens（含解释文字会更多）")
    print(f"  LLM 单条响应耗时（经验值）: 2-8s/条，20 条约 40-160s")


if __name__ == "__main__":
    main()
