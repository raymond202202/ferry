# ferry（摆渡）

[![test](https://github.com/raymond202202/ferry/actions/workflows/test.yml/badge.svg)](https://github.com/raymond202202/ferry/actions/workflows/test.yml)

> 把命令从 Linux 摆渡到 macOS / Windows，再摆渡回来。离线、确定性、给 AI agent 用。

ferry 不是"自然语言转命令"，也不是简单的命令名词典。它解决的是**同一条命令在不同操作系统上到底能不能跑、跑起来语义一样不一样**：

```
$ ferry translate "rm -rf build" --to windows
输入   rm -rf build   [Linux]
目标   Windows / powershell
状态   等价 (equivalent)

  Remove-Item -Recurse -Force build

注意事项
  • Windows 直接永久删除，不进回收站；不可恢复
```

```
$ ferry translate "sed -i 's/a/b/' f.txt" --to macos
状态   近似等价 (approximate)
  sed -i '' 's/a/b/' f.txt
注意事项
  • BSD sed 的 -i 必须带一个备份后缀参数，空串 '' 表示不备份；这是 Linux→macOS 最高频的坑
```

## 为什么值得单独做个工具

跨平台命令转换的难度是**三层**的，市面上的工具基本只做了第一层：

| 转换方向 | 真实难点 | ferry 的处理 |
|---|---|---|
| Linux ↔ macOS | 命令名 95% 相同，坑全在 **BSD/GNU 参数差异**（`sed -i`、`stat -c/-f`、`date -d`、`sort -V`、`grep -P`、缺 `timeout/nproc/free`） | 参数级映射 + recipe + 坑位表 |
| Linux/macOS ↔ Windows | 不只是换命令，而是换**数据模型**：文本流管道 → 对象管道，POSIX 权限位 → ACL，systemd → SCM | 显式降级为 `approximate` 并给 caveat；能证明无解的直接 `unsupported` |
| 任意方向 | **绝不静默降级**：错误的"看起来对"的命令比报错危险得多（`rm -rf` / `chmod` / `find -delete`） | 三态 status + 退出码，让调用方能分支 |

关键设计取舍：**宁可报 unsupported，也不猜**。查不到就明说，需要时可选用 LLM 兜底，但结果标记 `inferred` 且绝不写进映射库。

## 安装

```bash
cd ~/.projects/ferry && ./install.sh
# 装到 ~/.ferry/install，链接到 ~/.local/bin/ferry
# 若 ~/.local/bin 不在 PATH：./install.sh --add-path（幂等，往 ~/.bashrc 写一个可删的块）
```

零依赖：只要 `python3`（3.9+）。Linux / macOS 开箱即用；Windows 用 `py` 跑同一份代码。

## 用法

```bash
# 翻译（源平台可省略，按命令名自动推断）
ferry translate "ls -la" --to windows
ferry translate "dir /w /s" --to linux            # 自动识别源是 Windows
ferry translate "cp -r src/ dst/" --to macos
ferry translate "rm -rf x" --to windows --shell cmd

# 逐行批量（agent 友好）
cat cmds.txt | ferry translate --to windows --json --stdin

# 查一条命令的三平台对应写法 + 常用 recipe
ferry lookup grep

# 导出目标平台速查摘要，直接塞进 agent 的 system prompt
ferry prompt-export --to windows --max-chars 4000
ferry prompt-export --to windows --compact --per-command 1   # 更省 token（~9KB 全文）

# shell 语法差异 / 已知坑
ferry syntax --to windows
ferry pitfalls --to macos

# 自检与统计
ferry doctor --live
ferry stats

# 跨平台断言验证（在目标机上自证，而不是信我）
ferry drills --to linux --run
ferry drills --to macos --emit sh > verify-mac.sh     # 拷到 Mac 上跑
ferry drills --to windows --emit ps1 > verify-win.ps1 # 拷到 Windows 上跑
```

### 跨设备 SSH（这台机器最常用的姿势）

```bash
ferry hosts add macbook macos --user dev      # 登记一次，之后按别名走
ferry hosts add winbox windows --user dev

ferry ssh macbook "ls -la /tmp"                  # 默认 dry-run：翻译 + 打印现成 ssh 命令
ferry ssh macbook "sed -i 's/a/b/' f.txt" --run  # 确认翻译没问题再真跑
ferry ssh winbox "ps aux | grep node" --to windows  # 平台没登记时显式指定
```

- **默认 dry-run**：ssh 出去的命令错了，是远端静默做错事，先看再跑。
- 命令以 argv 形式传给 `ssh`（`ssh host <cmd>` 单参数），本地不过 shell — 引号不会被吃掉；Windows 目标走 `powershell -EncodedCommand`（UTF-16LE base64），绕开 ssh→cmd→powershell 三层引号地狱。
- `unsupported` 时 `--run` 也拒绝执行。
- 目标机不需要装 ferry：本地翻译、远端执行。

### 退出码（给脚本/agent 用）

| 码 | 含义 | 建议动作 |
|---|---|---|
| 0 | 等价，可直接跑 | 执行 |
| 2 | 近似等价，看 caveats | 执行前核对，或向用户说明差异 |
| 3 | 目标平台无等价 | 不要执行，换方案 |
| 4 | 输入错误（空命令等） | 修调用 |

### JSON 契约

```json
{
  "schema": 1,
  "input": "ps aux | grep nginx",
  "source": "linux", "target": "windows", "shell": "powershell",
  "status": "equivalent",
  "command": "Get-Process -Name nginx",
  "confidence": "high",
  "inferred": false, "llm_used": false,
  "caveats": [],
  "segments": [{"input": "...", "operator": "", "command": "...", "status": "...", "kind": "pattern", "caveats": []}]
}
```

`status` 取值：`equivalent` / `approximate` / `unsupported`。`kind` 说明命中路径：`recipe`（精确命中）> `pattern`（管道惯用法）> `flags`（参数级推导）> `passthrough`（同名直通）> `llm`（兜底推断）> `unsupported`。

## 接入 AI agent

三种方式，按侵入性从低到高：

**1. CLI 子进程（推荐，零集成成本）**

```bash
ferry translate "$CMD" --to windows --json && echo $?
```

**2. 提示词注入（省 token，让 agent 自己写对）**

```bash
ferry prompt-export --to windows --max-chars 3000 > /tmp/win-cheatsheet.md
```
把这段塞进 system prompt，agent 不必每次查表。适合长会话、频繁跨平台操作的 agent。

**3. 库调用（flare 等 Node 项目）**

见 `examples/flare-tool.ts`：把 ferry 包成 function-calling 工具，schema 见文件内注释。

## 数据格式

`data/*.jsonc`（允许 `//` 注释和尾逗号），一条命令长这样：

```jsonc
{
  "id": "cp", "category": "files", "summary": "复制文件或目录",
  "names": { "linux": "cp", "macos": "cp", "windows": "Copy-Item", "windows_cmd": "copy" },
  "flags": [
    { "semantic": "递归", "linux": "-r", "macos": "-R", "windows": "-Recurse", "windows_cmd": "/e" }
  ],
  "recipes": [
    { "desc": "递归复制目录", "status": "equivalent",
      "linux": "cp -r src/ dst/", "macos": "cp -R src/ dst/",
      "windows": "Copy-Item -Recurse src dst", "windows_cmd": "xcopy src dst /e /i" }
  ]
}
```

- `names`：各平台命令名；`windows_cmd` 是 cmd.exe 专用（PowerShell 用 `windows`）。
- `flags`：参数语义映射 —— 这是 Linux↔macOS 的主要战场。
- `recipes`：高频完整写法，优先级最高，`status` 必须诚实。
- 目标平台确实没有等价：写成 `null`，并在 `caveats` 说明替代方案。
- `hint`：该命令在目标平台的整体替代思路（用于 unsupported 时的提示）。

`data/95-patterns.jsonc` 放管道惯用法（`{name}` 匹配单个 token），`data/97-drills.jsonc` 放真机断言。

## 覆盖范围

127 条命令 / 245 条高频写法 / 21 条管道惯用法 / 24 条语法差异 / 22 个已知坑 / 33 条真机断言。

| 类别 | 条数 | 类别 | 条数 |
|---|---|---|---|
| 进程与作业 | 28 | 网络 | 20 |
| 文件与目录 | 27 | 包管理 | 9 |
| 文本处理 | 22 | 开发工具 | 8 |
| 服务与调度 | 6 | 压缩归档 | 7 |

## 诚实的验证状态

- ✅ **Linux 侧**：`ferry drills --to linux --run` 5/5；命令名本机可解析（`doctor --live`）。
- ✅ **macOS 侧**：GitHub Actions 的 macOS runner 上逐条跑过（`drills --to macos --run`）——16 条严格断言全绿（`sed -i` 备份后缀、`stat -f`/`-c`、`date -j -f`/`-d`、`timeout`/`nproc` 缺失、`grep -P`、`cp -a`、`base64 -D`、`sort -h`、`du -h`、`find -exec +`、`launchctl` 等），3 条环境相关项（`sha256sum`/`sort -V`/coreutils 遮蔽）以 INFO 记录实际结果。
- ✅ **引擎**：45 项单元测试（词法、recipe、参数映射、路径/引号/变量、退出码、JSON 契约、ssh 主机表）。
- ⏳ **Windows 侧**：33 条断言里 14 条是 Windows 的，已写成可执行脚本，但**没有真机跑过**。CI 里没有 Windows job —— `drills --run` 用 `cmd.exe` 执行，而 Windows 断言是 PowerShell 写的，直接在 CI 跑会假失败，所以留给人机验证：
  ```bash
  ferry drills --to windows --emit ps1 > /tmp/win.ps1   # 在 Windows 上 powershell -File win.ps1
  ```
  跑出与预期不符的项，就说明对应数据要改——这正是 drills 存在的意义。macOS 那轮就是这么发现 `sort -V` 和 `sha256sum` 的判断过时的（新版 macOS 已支持，见 Changelog）。

## 开发

```bash
python3 -m unittest discover -s tests -v   # 45 项
python3 ferry.py doctor                    # 数据完整性 + 覆盖统计
python3 ferry.py drills --to linux --run   # 当前平台行为断言
```

CI 在 Ubuntu 和 macOS 上跑同一套测试（macOS runner 会真的执行 BSD 相关断言，`data/97-drills.jsonc` 里标错的项会直接把 CI 打红——这是有意的，断言就该用结果说话）。

加命令、加惯用法、加断言的步骤见 `docs/DESIGN.md` §9。

## 不做的事

- 不执行命令（只翻译；执行是调用方的事，安全边界清楚）。
- 不声称语义完全等价（`approximate` 就是 `approximate`）。
- 不做自然语言 → 命令（那是另一类工具）。
- LLM 兜底的结果不落库（避免污染确定性资产）。

## 路线图

1. macOS / Windows 真机 drills 转绿，按结果修数据。
2. `--mcp` 模式（stdio MCP server），给支持 MCP 的 agent 直接当工具用。
3. 反向覆盖率检查：`ferry gaps --to windows` 列出"Linux 有但 Windows 没有映射"的命令，指导补数据。
4. WSL / MSYS2 / Git-Bash 三种路径语义的完整支持。
