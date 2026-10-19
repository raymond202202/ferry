# Changelog

版本按"映射库能力"走，不追求语义化版本的严格性：加数据是小版本，改契约是大版本。

## 0.3.0 — 2026-10-19

**FreeBSD 平台**

- 平台键新增 `freebsd`（`names` / `flags` / `recipes` / `syntax` / `patterns` / `drills` 全部落地），`normalize_platform` 识别 `freebsd` / `fbsd` / `bsd` / `freebsd13~15`。
- `current_platform()` 用 `sys.platform.startswith("freebsd")` 返回 `freebsd`，默认 shell 取 `sh`。
- 与 macOS 同源但**不照抄**：FreeBSD 15 的 `sort -V`、`readlink -f`、`ls --color`、`nproc`、`timeout` 可用；`sed -i` 仍需备份后缀、`stat` 用 `-f`、`date` 用 `-j/-r/-v`；base 无 `sudo`/`bash`/`curl`/`git`/`python3`/`lsof`/`rsync`/`zip`。
- 包管理 / 服务 / 网络 / 磁盘映射：`brew`→`pkg`、`brew install`→`pkg install -y`、`systemctl`→`service`+`sysrc`、`ip addr`→`ifconfig`、`ss`→`sockstat`、`lsblk`→`gpart show`、`free`→`vmstat -s`、`lsof -i`→`sockstat -4l`。
- `journalctl` 在 FreeBSD **无等价物**（`tail` 不是等价物：缺日志源，执行后会等 stdin 挂死），`names` 置空标 `unsupported`，条目加 `hint` 指向 `grep <关键词> /var/log/messages | tail -n 50` 与 `tail -f /var/log/messages`。
- `90-syntax.jsonc` 新增 5 条 FreeBSD 专属坑：`freebsd-minimal-base`、`freebsd-sh-not-bash`、`freebsd-pkg-bootstrap`、`freebsd-no-sudo-su-c`、`freebsd-third-party-layout`；`bsd-sed-inplace` 等 8 条多平台坑扩到 `["macos","freebsd"]`。
- `97-drills.jsonc` 新增 54 条 `platform: "freebsd"` 断言，全部在 FreeBSD 15.0-RELEASE VM 上实测：`通过 53，失败 0`（`fbsd-pkg-clean-system` 为 `expect: any`）。

**引擎修复**

- `recipe_match` 先剥 `sudo`/`doas`/`env`/`VAR=value` 前缀再匹配，模糊轮跳过"动词位冲突"的 recipe。修掉 `apt install nginx` → `pkg nginx`（丢动词）的老 bug。
- 参数（flag）翻译改为**按首 token 建索引 + 记录示例值占几个 token**：输入与示例完全一致才整段替换，否则只换开关、保留用户真实值；目标端该开关不接值而用户带了值，整段原样保留并给 caveat。修掉 `-n 50` 被改成 `-n 100`、`stat -c %s` 的 `%s` 覆盖用户路径、`date -d @<epoch>` 三段全错。`ferry.py` 与 flare 的 `engine.ts`（`FlagHit`）实现对齐，两端由 `tests/fixtures/ferry-cases.json` 千余条用例逐条比对。
- `date` 新增 recipe：epoch 秒转日期 `date -d @<epoch> +%F` → `date -r <epoch> +%F`（`approximate`，BSD 无 `-d`）。
- `install.sh` 改为纯 POSIX sh（FreeBSD base 无 bash）；`--add-path` 在 FreeBSD 写 `~/.profile`，其他平台仍写 `~/.bashrc`。

**FreeBSD 真机验证（15.0-RELEASE，QEMU/KVM）**

- `ferry drills --to freebsd`：53 通过 / 0 失败（1 条 `expect: any` 只记录）。
- 10 条双向 `translate` 抽样 9 条可执行（`journalctl` 按设计判 `unsupported`）。
- 翻译结果在 VM 上实跑通过：`date -r 1700000000 +%F` → `2023-11-14`、`stat -f %z f.txt` → `4`、`sed -i '' 's/aaa/bbb/' f.txt` 就地生效、`service -l` 正常输出。
- flare 本体在同一 VM 上 `npm run build` + `sh scripts/install.sh` 成功：`flare --version` → 0.6.153，`currentPlatform()` → `freebsd`。
- 复跑步骤见 `docs/freebsd-vm-verification.md`。

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
