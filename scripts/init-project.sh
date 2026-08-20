#!/usr/bin/env bash
# 為新專案套用「不上 main」隔離機制。
# 前置：先把 .no-merge、.githooks/、scripts/ 複製進專案根目錄。
# 用法：在 git 專案根目錄執行  scripts/init-project.sh
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

# 1. 建立隔離區資料夾（.gitkeep 讓空資料夾也能進版控）
mkdir -p .agent/plans .agent/notes logs
touch .agent/plans/.gitkeep .agent/notes/.gitkeep logs/.gitkeep

# 2. 確保 .no-merge 存在
if [ ! -f .no-merge ]; then
  cat > .no-merge <<'EOF'
# 不會合併進 main 的路徑（在 dev_ai 保留本地版控，合併時剝離）。
CLAUDE.md
.claude/
.agent/
logs/
EOF
fi

# 3. 啟用 pre-push hook 並補上執行權限
git config core.hooksPath .githooks
chmod +x .githooks/pre-push scripts/*.sh 2>/dev/null || true

echo "✅ 隔離機制已套用："
echo "   - 隔離路徑清單：.no-merge"
echo "   - 合併請用：    scripts/merge-to-main.sh dev_ai"
echo "   - pre-push hook 已啟用（core.hooksPath=.githooks）"
