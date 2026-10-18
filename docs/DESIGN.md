# ferry 设计说明

## 1. 定位与边界

**输入**：一条（可含管道/`&&`/`;`/重定向的）命令 + 目标平台（+ 目标 shell）。
**输出**：目标平台等价命令 + 等价等级 + 差异说明。

**不做**：执行命令、自然语言生成命令、证明两段文本语义严格相等。ferry 输出的是"可核对的高质量建议"，不是"保证正确的执行计划"。调用方（agent）必须读 `status` 与 `caveats`。

## 2. 为什么状态是三态而不是布尔

跨平台翻译里，误判的代价不对称：

- `ls -la` → `Get-ChildItem -Force`：能跑，但输出列不同 → `approximate`
- `chmod +x` → 无 → `unsupported`
- `rm -rf` → `Remove-Item -Recurse -Force` → `equivalent`（语义一致，但删了不可恢复 → 用 caveat 提示，不用降级）

布尔值"能/不能"会把这些混为一谈。三态 + 退出码让调用方能写：
```bash
ferry translate "$c" --to windows --json >/tmp/r.json
case $? in
  0) run "$(jq -r .command /tmp/r.json)" ;;
  2) run_after_review "$(jq -r .command /tmp/r.json)" ;;
  3) fallback_plan ;;
esac
```

`approximate` 不一定意味着"危险"，只意味着"存在需要人/agent 知道的差异"。caveats 里写清差异是什么。

## 3. 匹配优先级（引擎核心）

```
0. patterns   整条命令匹配管道惯用法（ps aux | grep x）
1. recipe     该命令的精确写法命中（参数归一化后比对）
2. flags      命令名映射 + 参数级语义映射 + 路径/变量转换
3. passthrough 三平台同名（git/curl/docker）或本机已存在
4. unsupported 明确报无解（附 hint / 替代思路）
```

为什么 0 在最前：`ps aux | grep nginx` 逐段翻译会得到 `Get-Process | Select-String nginx` —— 语法合法、语义完全错。管道惯用法必须整条识别。

为什么 recipe 优先于 flags：flags 推导本质是"猜"，recipe 是"已知正确写法"。凡是高频的都补成 recipe。

### 参数归一化

recipe 比对时对 token 序列归一化（忽略引号差异、忽略目录尾斜杠）。`rm -rf build` 与 `rm -rf build/` 命中同一条 recipe。

### 参数级映射的诚实度

- 映射表里有 → 用映射；映射为空串 → 丢弃并说明（例如 `ls -l` 在 PowerShell 无对应，输出格式本身就是详情列表）。
- **危险参数不允许静默丢弃**：`-delete`/`-exec`/`-9`/`--delete` 这类一旦丢弃就改变语义，直接判 `unsupported`，而不是产出"少了一半动作"的命令。
- 未识别的 POSIX 参数投到 PowerShell：丢弃 + 说明；`-h/--help` 这类通用参数直通。

## 4. 路径、引号、变量

| 对象 | 策略 |
|---|---|
| POSIX → Windows | `/home/<u>` → `$env:USERPROFILE`；`/tmp` → `$env:TEMP`；`/mnt/c/...`、`/c/...` → `C:\...`；`/etc` → `%SystemRoot%\System32\drivers\etc`；`/proc`、`/usr/bin` 等无对应 → 原样保留 + caveat |
| Windows → POSIX | `C:\x` → `/mnt/c/x`（`--path-style wsl`）或 `/c/x`（`msys`）；UNC → 提示需挂载 |
| 引号 | 输入 token 若被引号包裹，输出保留引号（POSIX 单引号，PowerShell 单引号/必要时反引号转义） |
| 变量 | `$VAR` ⇄ `$env:VAR` ⇄ `%VAR%` |
| `/dev/null` | `$null`（PowerShell）/ `NUL`（cmd） |

任何路径改写都会把该段降级为 `approximate` 并列 caveat —— 因为路径一定需要人确认。

## 5. 数据模型

```
data/10-files.jsonc    … 80-dev.jsonc     命令条目（按类别分文件）
data/90-syntax.jsonc   shell 语法差异 + 已知坑
data/95-patterns.jsonc 管道惯用法（{name} 占位）
data/97-drills.jsonc   真机断言（expect success|fail）
```

`names` 里 `windows` = PowerShell，`windows_cmd` = cmd.exe。没有 `windows_cmd` 就表示 cmd 下无等价（引擎会报 unsupported 并提示 PowerShell 写法）。

索引冲突处理：同平台同名命令可能属于多个条目（`dir` 既是 `ls` 也是 `find`/`stat` 的 cmd 形态）。索引按优先级取一个 —— **条目 id 与命令名一致者优先**（所以 `brew` 解析到 `brew`，而不是把 `brew` 当成 `apt` 的 macOS 名）。

## 6. 为什么不把数据写成代码

- 数据要能被非程序员审阅、被 diff 审阅。
- 同一份数据可以被 CLI、prompt-export、drills、未来的 MCP server 复用。
- 引擎改错不该连带数据；数据改错不该连带引擎。

JSONC（而不是 YAML）是因为**零依赖**：YAML 需要第三方库，而 JSON 手写太痛苦，所以自带 30 行 JSONC 解析（去注释 + 去尾逗号）。

## 7. LLM 兜底的边界

- 默认关闭，`--llm` 才启用，且需要 key。
- 返回结果强制标记 `inferred: true` + `llm_used: true`，caveat 第一行写明"LLM 推断，未写入映射库，请人工核对"。
- **永不落库**。要沉淀就人工审核后写进 `data/`，并配一条 drill。

## 8. 已知限制

1. 管道语义差异只做到"提示"级别：PowerShell 是对象流，`grep`/`awk` 式的文本处理换成了 `Select-String` / `ForEach-Object`，但复杂表达式仍需改写。
2. `find -exec`、`xargs` 接复杂子命令不做递归翻译（会显式提示）。
3. 不支持 shell 函数、alias 展开、heredoc（`unsupported`）。
4. 服务名映射只能给"形态"（systemctl → Get-Service/launchctl），具体名字得人确认。
5. 只有 127 条命令；覆盖日常运维 90%，长尾需要增量补。
6. macOS/Windows 侧断言尚未真机跑过（见 README「诚实的验证状态」）。

## 9. 扩容方式

加一条命令 = 在对应类别文件加一个对象 → `ferry doctor` 自查 → 补 1-3 条 recipe → 最好补 1 条 drill。不需要改代码。

加一种管道惯用法 = 往 `95-patterns.jsonc` 加 `{name}` 模板。

新平台（如 BSD/Fedora 之外）需要改 `PLATFORMS` 与 `flag_key()`，工作量中等 —— 当前不为"可能的需求"预留抽象。
