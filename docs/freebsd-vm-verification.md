# FreeBSD 真机验证流程（可复跑）

ferry 的 `freebsd` 平台数据不接受"按记忆填"。这里的步骤是在 Linux 宿主上用 QEMU 起一台
FreeBSD 15.0 VM，把所有要写进 `data/*.jsonc` 的断言先在真机上跑出来，再落库。

> 本文件只记录步骤与命令，**不含任何凭据**。VM 密码、SSH 私钥请自行生成；下文用
> `<VM_PASSWORD>`、`~/.ssh/id_ed25519` 占位。

## 0. 前置

- Linux 宿主，`/dev/kvm` 可读写（`test -r /dev/kvm -a -w /dev/kvm`）
- `qemu-system-x86-core`、`aria2c`、`xz`
- 约 10 GB 空闲磁盘（镜像 + qcow2 + 包缓存）

## 1. 取镜像并扩容

```sh
mkdir -p ~/.cache/freebsd-vm && cd ~/.cache/freebsd-vm
# Cloud-Init 版没有预置用户，靠 seed ISO 注入
aria2c -x16 -s16 -o fbsd.qcow2.xz \
  https://download.freebsd.org/releases/VM-IMAGES/15.0-RELEASE/amd64/Latest/FreeBSD-15.0-RELEASE-amd64-BASIC-CLOUDINIT-ufs.qcow2.xz
xz -dk fbsd.qcow2.xz
cp fbsd.qcow2 fresh.qcow2
qemu-img resize fresh.qcow2 8G
```

`xz` 报"压缩数据已损坏"时先核对 sha256 再重下，别硬解。

## 2. Cloud-Init seed（不含明文密码的做法）

`seed/user-data` 里用密码哈希而不是明文；哈希用宿主侧生成：

```sh
openssl passwd -6 '<VM_PASSWORD>'          # 得到 $6$... 哈希
```

```yaml
# seed/user-data
#cloud-config
hostname: fbsd
users:
  - name: fantastic
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    shell: /bin/sh
    lock_passwd: false
    passwd: "$6$<HASH>"
    ssh_authorized_keys:
      - <你的公钥内容>
ssh_pwauth: false
```

```sh
# seed/meta-data
instance-id: fbsd-01
local-hostname: fbsd

# 打成 ISO（FreeBSD 的 cloud-init 兼容实现 nuageinit 认这个卷标）
genisoimage -output seed.iso -volid cidata -joliet -rock seed/user-data seed/meta-data
```

FreeBSD 15 用 **nuageinit**（不是 cloud-init）。它支持 `users`、`ssh_authorized_keys`、
`write_files`、`runcmd`，但**不保证**每条都执行——验证时以 `ssh` 能否登录为准，
不要假设 `runcmd` 生效。

## 3. 启动

```sh
cd ~/.cache/freebsd-vm
qemu-system-x86_64 \
  -machine q35 -accel kvm -cpu host -m 3072 -smp 2 \
  -drive file=fresh.qcow2,if=virtio,cache=unsafe \
  -drive file=seed.iso,media=cdrom,readonly=on \
  -netdev user,id=n0,hostfwd=tcp:127.0.0.1:2222-:22 \
  -device virtio-net-pci,netdev=n0 \
  -serial tcp:127.0.0.1:4445,server,nowait \
  -monitor unix:mon.sock,server,nowait \
  -display none -daemonize -pidfile qemu.pid
```

- 关机：`printf 'system_powerdown\n' | socat - UNIX-CONNECT:mon.sock`
- 串口单用户修复（根文件系统异常时）：`nc 127.0.0.1 4445`，进 loader 后 `mount -u -w /`、
  `fsck -y /`、`mount -a`；`autoboot_delay=0` 的镜像需要在 QEMU 加 `-S` 后连发空格抢 loader 菜单。

登录：

```sh
ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -i ~/.ssh/id_ed25519 fantastic@127.0.0.1
```

## 4. 在 FreeBSD 上装 node/npm（base 没有，且可能无 root）

有 root 时直接 `pkg install -y node22 npm-node22 python3 gmake`。
只有普通用户时，用**用户级 pkgroot**：宿主侧解析 `packagesite.yaml` 拿全部依赖，`aria2c`
下载后 `tar xf` 解包到 `~/pkgroot`。

```sh
# 宿主侧：pkg 没有 pkg-static 时的旁路
aria2c -x16 -o packagesite.pkg https://pkg.FreeBSD.org/FreeBSD:15:amd64/quarterly/packagesite.pkg
tar xf packagesite.pkg --to-stdout packagesite.yaml > packagesite.yaml   # 索引
# 按名字 BFS 解析依赖 → 生成 url 列表 → aria2c -i urls.txt
scp -P 2222 pkgs.tar fantastic@127.0.0.1:/tmp/
# VM 内
mkdir -p ~/pkgroot && cd ~/pkgroot && tar xf /tmp/pkgs.tar
```

之后每条命令都带上环境（**`NODE_ICU_DATA` 必须设**，否则 npm 报 `Internal error. Icu error.`
——官方 node22 把 ICU 路径编死到 `/usr/local/share/icu/76.1`）：

```sh
export PATH=$HOME/pkgroot/usr/local/bin:$PATH
export LD_LIBRARY_PATH=$HOME/pkgroot/usr/local/lib:/usr/local/lib
export NODE_ICU_DATA=$HOME/pkgroot/usr/local/share/icu/76.1
export ICU_DATA=$NODE_ICU_DATA
export MAKE=gmake
```

`npm` 在独立包 `npm-node22` 里，不在 `node22` 包内。

## 5. 装 ferry 并跑断言

```sh
git clone https://github.com/raymond202202/ferry.git ~/ferry   # 或用 scp 传源码
cd ~/ferry
sh install.sh --add-path            # 纯 POSIX sh，FreeBSD 写 ~/.profile
~/.local/bin/ferry doctor
~/.local/bin/ferry drills --to freebsd --run     # 期望：通过 N，失败 0
```

`drills --run` 是**在本机执行断言命令**，所以 `--to freebsd` 必须在 FreeBSD 上跑。
没有 root 时 `pkg` 相关断言会失败——这类条目用 `expect: any` 只记录，不要硬编 `success`。

翻译结果必须实跑一遍，不能只看字符串：

```sh
~/.local/bin/ferry translate "date -d @1700000000 +%F" --to freebsd --json
date -r 1700000000 +%F          # → 2023-11-14
printf 'aaa\n' > f.txt
stat -f %z f.txt                # → 4
sed -i '' 's/aaa/bbb/' f.txt    # 就地生效，f.txt 变 bbb
service -l                      # 列出 rc.d 脚本
```

## 6. 顺带验证 flare（内嵌 ferry 引擎）

```sh
cd ~/flare && npm install --no-audit --no-fund
npx tsc -p tsconfig.json --noEmit && npm run build
sh scripts/install.sh
flare --version                  # 期望打印版本号
node -e 'import("./dist/tools/ferry-core/engine.js").then(m=>console.log(m.currentPlatform()))'
```

npm 12 默认不跑依赖的安装脚本，原生模块（`better-sqlite3`）需要：

```sh
npm install-scripts approve --all
npm rebuild better-sqlite3
```

## 7. 坑清单（都是这里踩出来的）

| 现象 | 原因 / 处理 |
|---|---|
| `sed -i 's/a/b/' f` 失败 | BSD sed 必须给备份后缀：`sed -i '' 's/a/b/' f` |
| `stat -c %s` 不支持 | 用 `stat -f %z` |
| `date -d` 不支持 | 用 `date -j -f <fmt> <str>` 或 `date -r <epoch>` |
| 没有 `sudo` / `doas` / `bash` | base 系统；`su -` 的 `-c` 是 login class 不是执行命令 |
| `journalctl` 不存在 | 无等价物。看 `/var/log/messages`，`grep` + `tail -n` |
| `pkg -v` 提示未安装 | 需 `pkg bootstrap`（要 root）或用户级 pkgroot 旁路 |
| npm `Internal error. Icu error.` | 设 `NODE_ICU_DATA` |
| 原生模块没编译 | `npm install-scripts approve --all` + `npm rebuild` |
| `tail -u x` 挂住 | 缺日志源，会等 stdin。不要用它替代 `journalctl` |
