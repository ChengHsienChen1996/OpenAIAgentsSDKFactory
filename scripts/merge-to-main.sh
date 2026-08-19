#!/usr/bin/env bash
# 將來源分支合併進 main，並剝離 .no-merge 中列出的路徑，
# 使那些檔案在 dev_ai 保有本地版控、但不進入 main。
#
# 用法：在乾淨的工作目錄執行
#   scripts/merge-to-main.sh [來源分支]   # 來源分支預設 dev_ai
set -euo pipefail

SRC="${1:-dev_ai}"
TARGET="main"
ROOT="$(git rev-parse --show-toplevel)"
NOMERGE="$ROOT/.no-merge"

[ -f "$NOMERGE" ] || { echo "❌ 找不到 $NOMERGE"; exit 1; }

# 讀取路徑清單（去掉註解、空行、尾端空白）
mapfile -t PATHS < <(grep -vE '^[[:space:]]*(#|$)' "$NOMERGE" | sed 's/[[:space:]]*$//')

# 前置檢查：工作目錄必須乾淨
if [ -n "$(git status --porcelain)" ]; then
  echo "❌ 工作目錄不乾淨，請先 commit 或 stash 後再合併"; exit 1
fi

git checkout "$TARGET"

# --no-commit：先不產生合併 commit，讓我們有機會剝離
#
# git merge 的非零離開碼有兩種意義，必須分開處理：
#   1. 合併已開始、有檔案衝突   → MERGE_HEAD 存在 → 繼續剝離，衝突留給使用者
#   2. 合併根本沒開始（分支名打錯、index.lock、submodule 未 init…）
#                               → MERGE_HEAD 不存在 → 必須中止
# 若不區分而一律放行，第 2 種情況會在「什麼都沒合併」的狀態下往下跑剝離迴圈，
# 把檔案刪掉之後還回報成功。
merge_rc=0
git merge --no-ff --no-commit "$SRC" || merge_rc=$?
if [ "$merge_rc" -ne 0 ] && [ ! -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]; then
  echo "❌ 合併未能開始（git merge 離開碼 $merge_rc），未做任何剝離"
  echo "   目前分支已切到 $TARGET，請確認來源分支名稱與 repo 狀態後重試。"
  exit "$merge_rc"
fi

# 逐一把隔離路徑還原成 main 既有狀態
for p in "${PATHS[@]}"; do
  if git cat-file -e "HEAD:$p" 2>/dev/null; then
    # main 原本就有此路徑 → 還原成 main 的版本（等於忽略 dev_ai 的變更）
    git checkout HEAD -- "$p"
  else
    # main 原本沒有（dev_ai 新增）→ 從索引與工作目錄移除
    #
    # 用 git rm（不加 --cached）而非 `--cached` + `rm -rf`：git rm 只動被追蹤的
    # 檔案，未追蹤的原封不動。`rm -rf` 不認得追蹤狀態，會一併刪掉像
    # .claude/settings.local.json 這種被全域 gitignore、無任何版控可還原的檔案。
    # -f 是必要的：合併剛把這些檔案加進索引，不加會被當成「有暫存變更」而拒絕。
    git rm -rf --quiet --ignore-unmatch "$p" || true
  fi
done

echo
echo "✅ 已剝離下列路徑（不會進入 $TARGET）："
printf '   - %s\n' "${PATHS[@]}"
echo
if git diff --name-only --diff-filter=U | grep -q .; then
  echo "⚠️  仍有其他檔案衝突，請解決後執行：git commit"
else
  echo "→ 完成剝離，執行以下指令產生合併 commit：git commit --no-edit"
fi
