#!/usr/bin/env bash
# ferry 安装脚本：把 CLI 与映射数据装到 ~/.ferry/install，并链到 ~/.local/bin
# 用法：./install.sh            （安装/升级）
#       ./install.sh --uninstall （卸载，保留数据目录）
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.ferry/install"
BIN_DIR="$HOME/.local/bin"
LINK="$BIN_DIR/ferry"

ADD_PATH=0
for arg in "$@"; do
  case "$arg" in
    --uninstall)
      rm -f "$LINK"; rm -rf "$DEST"
      echo "已卸载 ferry（$DEST 与 $LINK 已删除）"; exit 0 ;;
    --add-path) ADD_PATH=1 ;;
    *) echo "未知参数：$arg（可用：--add-path / --uninstall）"; exit 1 ;;
  esac
done

echo "==> 安装到 $DEST"
mkdir -p "$DEST/data" "$BIN_DIR"
install -m 0755 "$SRC/ferry.py" "$DEST/ferry.py"
# 只同步映射数据，避免残留旧文件
rm -f "$DEST"/data/*.jsonc
cp -f "$SRC"/data/*.jsonc "$DEST/data/"
[[ -f "$SRC/README.md" ]] && cp -f "$SRC/README.md" "$DEST/README.md"
[[ -d "$SRC/docs" ]] && mkdir -p "$DEST/docs" && cp -f "$SRC"/docs/*.md "$DEST/docs/" 2>/dev/null || true

ln -sf "$DEST/ferry.py" "$LINK"
chmod +x "$DEST/ferry.py"

# PATH：默认不动用户的 rc 文件；要自动写入需显式 --add-path
if ! grep -qs 'ferry-install-path' "$HOME/.bashrc"; then
  if [[ "${ADD_PATH:-0}" == "1" ]]; then
    {
      echo ''
      echo '# ferry-install-path'
      echo 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac'
    } >> "$HOME/.bashrc"
    echo "==> 已在 ~/.bashrc 加入 ~/.local/bin 到 PATH（--add-path）"
  else
    echo "==> 提示：~/.bashrc 未包含 ~/.local/bin。"
    echo "    想让 ferry 直接可用，二选一："
    echo "      ./install.sh --add-path        # 由脚本写入（幂等，可手动删掉那块）"
    echo "      echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
    echo "    也可以直接用绝对路径：$LINK"
  fi
fi

# 配置文件模板（不覆盖已有）
if [[ ! -f "$DEST/config.example.env" && -f "$SRC/.env.example" ]]; then
  cp -f "$SRC/.env.example" "$DEST/config.example.env"
fi

echo "==> 自检"
"$DEST/ferry.py" doctor | sed 's/^/    /'

echo
echo "完成。新终端里可以直接用：ferry translate 'ls -la' --to windows"
echo "当前会话可先执行：export PATH=\"\$HOME/.local/bin:\$PATH\""
