# Changelog

版本按"映射库能力"走，不追求语义化版本的严格性：加数据是小版本，改契约是大版本。

## 0.2.0 — 2026-10-18

**跨设备 SSH**

- 新增 `ferry hosts add|list|remove`，主机表存在 `~/.ferry/hosts.json`（别名 → 平台 / shell / user / port）。
- 新增 `ferry ssh <host> "<命令>"`：翻译后经 ssh 执行。**默认 dry-run**，只打印翻译结果和现成 ssh 命令；`--run` 才真执行。
- 远端命令以 argv 形式传给 `ssh`（单参数），本地 shell 不参与，引号不会被吃掉；Windows 目标用 `powershell -EncodedCommand`（UTF-16LE base64），绕开 ssh → cmd → powershell 三层引号。
- `unsupported` 时 `--run` 拒绝执行。

**引擎修复（真实使用暴露）**

- recipe 里的示例字面量不再原样输出：`grep -rn TODO src` 不会变成 `src/`，`tar -xzf a.tgz` 不会变成 `app.tar.gz`。
- caveat 按目标平台过滤：Linux → macOS 不再显示 `Select-String` 的 Windows 专属注意事项。
- `universal` 命令（git / curl / docker）参数原样透传，不再丢弃 `-fsSL`。
- 近似状态回收：命令与原命令一致、且适用 caveat 为空时判为等价（此前 recipe 的 status 被 Windows 侧的坑污染）。
- 组合短参数全等映射时保留原写法：`ls -la` 不再拆成 `ls -l -a`。
- 保留 POSIX 目标的目录尾斜杠（`cp -r src/ dst/`）。

**验证**

- 45 项单元测试；新增 `tests/test_ferry.py` 中 ssh / hosts / recipe 字面量 / caveat 过滤用例。
- 新增 GitHub Actions：Ubuntu + macOS × Python 3.9/3.12，macOS runner 直接执行 BSD 断言。
- `drills` 支持 `expect: "any"`——环境相关项（是否装了 GNU coreutils）只记录不判失败。
- 真机结果修正数据：macOS 15+ 的 `sort -V` 与 `/sbin/sha256sum` 实际可用，`sort -V` 从 `unsupported` 改为 `approximate`。

## 0.1.0 — 2026-10-18

- 首个版本：127 条命令 / 245 条写法 / 21 条管道惯用法 / 24 条语法差异 / 22 个已知坑。
- 三态契约（`equivalent` / `approximate` / `unsupported`）+ 退出码 `0/2/3/4`。
- `translate` / `lookup` / `prompt-export` / `syntax` / `pitfalls` / `drills` / `doctor` / `stats`。
- Python 3 stdlib 单文件，零依赖；`install.sh` 装到 `~/.ferry/install` 并软链到 `~/.local/bin/ferry`。
