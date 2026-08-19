# 版控流程

## 開工前準備

每次開工前，先完成以下兩項準備，再開始寫任何程式碼：

1. **確認分支**：確保在 `dev_ai` 分支上（見下方「分支規範」）。
2. **備份權限設定**：快照 `.claude/settings.local.json`（見下方「Claude Code 權限設定備份」）。

## 分支規範

### AI 作業分支：`dev_ai`

所有 AI 執行的程式碼改動 **必須** 在 `dev_ai` 分支上進行。

#### 每次作業前的檢查流程

```bash
# 1. 確認當前分支
git branch --show-current

# 2. 若不在 dev_ai，則從 main 建立並切換
git checkout main
git pull origin main          # 確保 main 為最新
git checkout -b dev_ai        # 首次建立

# 3. 若 dev_ai 已存在，直接切換
git checkout dev_ai
```

#### 重點

- `dev_ai` 一律從 `main` 分支建立。
- 是否將 `dev_ai` 合併回 `main` **由人工決定**，AI 不主動執行 merge；且合併一律使用 `scripts/merge-to-main.sh`（詳見〈不上 main 的檔案隔離〉）。
- 如果人工已將 `dev_ai` 合併並刪除，下次作業時重新從 `main` 建立即可。

## 不上 main 的檔案隔離

部分檔案需要在 `dev_ai` 保留本地版控（可回溯歷史），但 **不應合併進 `main`**：

| 類型 | 路徑 |
|------|------|
| Agent 設定 | `CLAUDE.md`、`.claude/` |
| 開發計畫書 / 筆記 | `.agent/plans/`、`.agent/notes/` |
| 開發紀錄 | `logs/` |

這些路徑照常在 `dev_ai` 上 commit，因此保有完整本地歷史；只在「進入 `main`」這個邊界被攔下。

### 運作原理

以單一清單 `.no-merge`（專案根目錄，一行一條）為準，透過兩道機制確保隔離。新增或調整隔離路徑時，只需修改這個檔案，合併與防呆會同步套用：

1. **合併時剝離** —— 以 `scripts/merge-to-main.sh` 取代手動 `git merge`。它以 `--no-commit` 合併來源分支後，把 `.no-merge` 中每條路徑還原成 `main` 既有狀態（`main` 原本沒有的則從索引移除），再產生合併 commit。結果：程式碼變更進入 `main`，隔離檔案不進入。
2. **推送防呆** —— `.githooks/pre-push` 在推上 `main` 前檢查即將推送的差異，一旦碰到 `.no-merge` 中的路徑即中止推送。這是即使忘了用合併腳本、直接 `git merge` 也擋得住的最後一道牆。

> 註：`.gitignore` 不負責此隔離 —— 被 ignore 的檔案無法在 `dev_ai` 保留版控。隔離改由上述兩道機制在合併/推送邊界處理。

### 人工合併流程

```bash
# 在乾淨的工作目錄，於任意分支執行
scripts/merge-to-main.sh dev_ai

# 若腳本回報無其他衝突：
git commit --no-edit

# 若有其他檔案衝突，解決後再：
git commit
```

### 新專案啟用

```bash
# 先將 .no-merge、.gitignore、.githooks/、scripts/ 複製進專案根目錄，然後：
scripts/init-project.sh
```

`init-project.sh` 會建立 `.agent/`、`logs/` 資料夾，確保 `.no-merge` 存在，並以 `git config core.hooksPath .githooks` 啟用 pre-push hook。

> pre-push hook 只保護 `main`。`dev_ai` 若推上遠端，隔離檔案會一併帶上遠端 `dev_ai`（這是「本地/私有分支版控」的預期行為）；真正的紅線是它們永遠不進 `main`。

## Claude Code 權限設定備份

### 為什麼是獨立於「檔案隔離」的另一層保護

上一節的隔離機制，保護的是「在 `dev_ai` 有版控、只是不進 `main`」的檔案。但 `.claude/settings.local.json` 屬於不同情況，隔離機制救不了它：

- 它被使用者的**全域** gitignore（`~/.config/git/ignore` 中的 `**/.claude/settings.local.json`）擋住，**連 `dev_ai` 都沒有版控**——誤刪即永久遺失，無法還原。
- 它儲存 Claude Code 的權限核准設定（哪些 Bash 指令已被允許執行），每次核准新權限就會增長，重建成本高。

換句話說：`.claude/` 目錄本身屬隔離區（Zone 2，在 `dev_ai` 有歷史）；但 `settings.local.json` 這個檔案被全域 gitignore 單獨排除在版控之外，所以需要另一層 **repo 外** 的快照備份。

### 備份時機與指令

**每次開工時** 存一份當日快照：

```bash
mkdir -p ~/.claude/settings-backups/<project_name>
cp -p .claude/settings.local.json \
   ~/.claude/settings-backups/<project_name>/settings.local.json.$(date +%Y-%m-%d)
```

- 將 `<project_name>` 換成專案專屬名稱，避免不同專案的備份互相覆蓋。
- 檔名帶日期，同日重跑會覆蓋當日快照，不影響先前日期的備份。
- 還原就是反向 `cp`（把備份檔複製回 `.claude/settings.local.json`）。

### 為什麼刻意存在 repo 之外

- 主要威脅來自 **會清掉未追蹤檔案** 的操作——`git clean`（尤其 `-x`）、誤刪、砍掉重新 clone 等。放在 repo 內的備份擋不住這些操作。
  （註：`scripts/merge-to-main.sh` 只還原 `.no-merge` 中**被追蹤**的路徑，不會動到未追蹤的 `settings.local.json`，因此合併腳本本身不是此檔的威脅來源。）
- **不要** 存進 `~/.claude/backups/`，那是 Claude Code 自己輪替 `.claude.json.backup.*` 用的目錄，兩者會互相干擾。

## Commit 訊息格式

### 格式

```
<tag>: <簡要描述> [commit by ai]
```

### 常用標籤

| 標籤 | 用途 |
|------|------|
| `feat` | 新增功能 |
| `fix` | 修正 Bug |
| `refactor` | 重構（不改變外部行為） |
| `test` | 新增或修改測試 |
| `docs` | 文件變更 |
| `chore` | 雜項（依賴更新、設定調整等） |
| `style` | 程式碼格式調整（不影響邏輯） |
| `perf` | 效能優化 |

### 範例

```bash
git commit -m "feat: 新增使用者摘要 agent [commit by ai]"
git commit -m "fix: 修正 rate limiter token 退款計算錯誤 [commit by ai]"
git commit -m "test: 新增 config_loader 單元測試 [commit by ai]"
git commit -m "refactor: 將 prompt 載入邏輯抽取至獨立模組 [commit by ai]"
git commit -m "docs: 更新 CLAUDE.md 測試策略說明 [commit by ai]"
```

### 規則

1. 描述使用中文或英文皆可，專案內保持一致。
2. 標籤一律使用小寫英文。
3. 結尾的 `[commit by ai]` 標記 **不可省略**，用於區分人工與 AI 的提交。
4. 一次 commit 應對應一個邏輯上的改動單位，避免將不相關的變更混在同一次 commit。

## 完整作業流程

```
1. 開工前準備：確認 dev_ai 分支、備份 .claude/settings.local.json
2. 進行程式碼開發
3. 執行測試（依測試策略決定）
4. git add 相關檔案
5. 以規範格式 commit
6. 撰寫改動日誌至 /logs（詳見 change-log-guide.md）
   —— logs/ 屬隔離區，會留在 dev_ai 但不進 main
7. 合併回 main 由人工執行：scripts/merge-to-main.sh dev_ai
```
