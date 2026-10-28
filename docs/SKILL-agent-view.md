---
name: cross-platform-command-translate
display_name: 跨平台命令防错（Agent 自检）
version: "1.0.2"
description: Agent 跨平台命令防错工具（面向 AI Agent，不是面向用户的命令问答）。模型原生训练环境（通常是 Linux）与 Agent 客户端实际运行平台（Windows/macOS/FreeBSD）不一致时，Agent 在生成/执行 shell 命令前必须先用本技能核对，避免凭 Linux 记忆写出平台不存在或语义不同的命令（如 sed -i、stat -c/-f、date -d、ps aux、find -delete、rm -rf 的差异）。离线确定性查表，不猜、不静默降级。
---

# 跨平台命令行翻译（Agent 防错工具）

**本技能是给 Agent 自己用的**，不是替用户翻译命令的问答工具。离线确定性查表，覆盖 127 条命令 / 87 条真机验证用例。不猜、不静默降级。

## 为什么需要它

模型的原生训练环境通常是 Linux，但 Agent 客户端可能跑在 Windows / macOS / FreeBSD 上。凭 Linux 记忆直接写命令，最常见的错误有三类：

1. **命令在目标平台根本不存在**（如 Windows 没有 `chmod +x`）
2. **命令存在但参数语义完全不同**（如 GNU `stat -c %Y` vs BSD `stat -f %m`，格式符含义完全对不上）
3. **命令存在但必须改写法**（如 `sed -i` 在 macOS 必须带 `''` 备份后缀参数，照抄 Linux 写法会直接把文件当参数名吞掉）

Agent 自己很难察觉这类错——命令报错还好，**最怕静默做错事**（如 `rm -rf build` 语义漂移成删错东西）。所以**执行前先核对**，不要凭记忆。

## 包内结构

```
scripts/ferry.py       主程序（单文件，仅需 Python 3.8+，无第三方依赖）
assets/data/*.jsonc    映射数据库（11 个文件）
assets/install.ps1     Windows 安装脚本（-AddPath 可写入用户 PATH，-Uninstall 卸载）
assets/install.sh      Linux / macOS 安装脚本
assets/README-ferry.md CLI 完整文档
```

## 首次使用：先安装

```bash
# Windows（PowerShell）
powershell -NoProfile -ExecutionPolicy Bypass -File "$SKILL_DIR/assets/install.ps1"

# Linux / macOS
bash "$SKILL_DIR/assets/install.sh"
```

装到 `~/.ferry/`：`install/`（程序+数据）、`bin/ferry.cmd` 与 `bin/ferry`（双包装器）。
**不改系统 PATH、不需管理员权限**；想让 `ferry` 直接可用再加 `-AddPath` / `--add-path`。

未安装时也可直接跑，不依赖任何安装步骤：

```bash
python "$SKILL_DIR/scripts/ferry.py" translate "ls -la" --to windows
```

## 何时用（Agent 自检时机）

**触发条件：你（Agent）准备生成或执行 shell 命令，且命令来自你的 Linux/训练平台记忆，而当前运行平台不是 Linux。**

具体时机：

- 准备执行任何非平凡 shell 命令前（哪怕你觉得很有把握）
- 命令包含 `sed`、`stat`、`date`、`find`、`ps`、`grep`、`ls`、`rm`、`chmod`、`du`、`df`、`head`、`tail`、`wc`、`touch`、`which`、`readlink`、`basename`、`dirname`、`xargs` 中的任何一个
- 用户平台是 Windows/macOS/FreeBSD（不确定当前平台就先探测，如 `uname -s` 或看提示词里的环境信息）
- 执行结果和预期不符，怀疑是平台差异时（回头复核）

**不需要用**：
- 纯 Linux 环境执行 Linux 命令
- 用户明确要求翻译给**他/她**看（那是给人看的问答，不经过你的执行）
- 自然语言描述 → 命令（需要 LLM 理解意图）
- 同一平台内的命令优化

## 怎么核对（Agent 操作步骤）

1. 确定目标平台（从环境信息/`uname -s` 得知，别猜）
2. 对每条准备执行的命令跑 `ferry translate "<cmd>" --to <platform> --json`
3. 按 `status` 分三态处理（见下表）
4. `approximate` 时，先改写再执行，不要拿着 Linux 原句去硬跑
5. `unsupported` 时，放弃该命令，换思路或告知用户，不要硬编一个像模像样的命令

## 核心原则：三态 + 不静默

每次翻译都返回三种状态之一，**必须**区分对待：

| 状态 | 退出码 | 含义 | 该怎么做 |
|---|---|---|---|
| `equivalent` | 0 | 语义等价 | 可直接执行 |
| `approximate` | 2 | 不完全等价 | **必须**把 caveats 一起告诉用户，不能只给命令 |
| `unsupported` | 3 | 目标平台无对应 | 不要执行，换方案或说明原因 |

**永远不要**把 `approximate` 的结果当成正确答案直接给出——caveat 里往往写着"参数已丢弃"这类会让人执行错内容的信息。

## 调用方式

装好后三种调用法（任选）：

```bash
# 1) 包装器（装过且已在 PATH）
ferry translate "ls -la" --to windows

# 2) 绝对路径（不依赖 PATH，最稳）
FERRY="$HOME/.ferry/bin/ferry.cmd"

# 3) 直接跑脚本（完全免安装）
python "$SKILL_DIR/scripts/ferry.py" translate "ls -la" --to windows
```

### 单条翻译（最常用）

```bash
"$FERRY" translate "rm -rf build" --to windows
```

源平台省略时按命令名自动推断；明确知道时显式指定更稳：

```bash
"$FERRY" translate "sed -i 's/a/b/' f.txt" --to macos --from linux
```

### JSON 输出（程序化处理时用）

```bash
"$FERRY" translate "ps aux | grep nginx" --to windows --json
```

关键字段：`status`、`command`、`caveats[]`、`segments[]`（管道拆成多段，每段独立状态）、`kind`（命中路径 recipe > pattern > flags > passthrough > llm > unsupported）、`inferred`。

### 其他子命令

```bash
"$FERRY" lookup grep              # 一条命令在各平台的对应写法
"$FERRY" syntax --to windows      # shell 语法差异（引号/变量/重定向/退出码）
"$FERRY" pitfalls --to macos      # 已知坑清单
"$FERRY" doctor                   # 数据完整性自检
```

## Agent 执行后的输出规矩

1. `equivalent`：直接执行，无需向用户解释
2. `approximate`：按 caveat 改写后执行；如果命令来自用户，要把改写点和 caveat 告诉用户
3. `unsupported`：放弃该命令，换方案或告知用户原因
4. **高危命令（删除/权限/覆盖）**：`rm`、`-delete`、`chmod`、`Set-Content`、`dd`、`mkfs`、`shutdown` 等，无论什么状态，执行前向用户说明后果——这条与平台无关，是通用规矩

## 高频的 "Agent 静默出错" 案例（必读）

这些是 Agent 最容易凭 Linux 记忆犯的错，每次跨平台执行前过一遍：

- **`sed -i 's/a/b/' f`** 在 macOS 会报错（缺备份后缀）。正确：`sed -i '' 's/a/b/' f`。反过来 Linux 上 `''` 会报错。**双向都要核对**。
- **`stat -c %Y f`** 在 macOS/FreeBSD 报错（-c 不存在）。BSD 用 `-f %m`，且 `%m` 含义不同。
- **`date -d '2026-01-31'`** 在 macOS 报错。BSD 用 `-j -f '%Y-%m-%d'`，Windows 用 `Get-Date`。
- **`ps aux | grep x`** Windows 用 `Get-Process -Name x`，不需要管道。
- **`rm -rf`** → `Remove-Item -Recurse -Force`，永久删除不进回收站。Agent 执行前必须确认路径是预期目标。
- **`find -delete`** → `Get-ChildItem ... | Remove-Item -Force`，建议先去掉 `| Remove-Item` 跑一遍看命中集合。
- **`chmod +x`** Windows 无可执行位概念，`unsupported`，不要伪造替代品。
- **`ls -la`** `-l` 在 Windows 无对应会被丢弃并告警，换 `Get-ChildItem -Force`。
- **`readlink -f`**、`realpath` 在 macOS 老版本不全，Windows 无原生对应。
- **同名直通不是漏改**：`git`、`curl`、`docker`、`ssh`、`tar` 三平台同名且参数一致，ferry 直通不改——这是对的，别自作聪明加戏。

## 维护

改完 `scripts/ferry.py` 或 `assets/data/` 后：

```bash
python "$SKILL_DIR/scripts/ferry.py" doctor                    # 数据完整性自检
python "$SKILL_DIR/scripts/ferry.py" drills --to windows --run  # 真机断言 9/9
```

- 某条翻译错了：先 `translate <cmd> --explain` 看命中路径（recipe > pattern > flags > passthrough > llm），再判断是数据缺项还是代码逻辑
- 卸载：`install.ps1 -Uninstall` / `install.sh --uninstall`

**不要把 API key 写进任何数据文件、配置或本 skill**。`--llm` 兜底只从环境变量读（`FERRY_LLM_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`），结果标记 `inferred`，不写回映射库。
