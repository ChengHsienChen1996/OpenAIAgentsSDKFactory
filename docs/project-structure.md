# 專案目錄結構規範

## 標準 Layout

所有專案統一使用 `src` layout：

```
project-root/
├── src/
│   └── <package_name>/
│       ├── __init__.py
│       └── ...
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   └── ...
├── docs/
├── prompts/              # （選用）僅 LLM 專案使用，存放 prompt 檔案
├── scripts/              # 專案工具腳本（含 merge-to-main.sh、init-project.sh）
├── .githooks/            # 版控 hook（pre-push 防呆）
│                         # ─── 以下為「本地版控、不上 main」隔離區 ───
├── .agent/
│   ├── plans/            # 開發進度計畫書
│   └── notes/            # 開發筆記
├── logs/                 # AI 每次作業的改動總結紀錄
├── .claude/              # Claude Code agent 設定
├── CLAUDE.md
│                         # ─────────────────────────────────────────
├── .no-merge             # 隔離路徑清單（merge 腳本與 pre-push hook 共用）
├── pyproject.toml
├── .env
└── .gitignore
```

## 版控分區

專案檔案分為兩區，差別只在「是否進入 `main`」：

- **Zone 1（會上 main）**：`src/`、`tests/`、`docs/`、`prompts/`、`pyproject.toml`、`scripts/`、`.githooks/` 等正式產出。照常合併。
- **Zone 2（本地版控、不上 main）**：`CLAUDE.md`、`.claude/`、`.agent/`、`logs/`。這些在 `dev_ai` 照常 commit（保有本地歷史），但透過 `.no-merge` + `scripts/merge-to-main.sh` + `.githooks/pre-push` 確保永不進入 `main`。機制細節見 [git-workflow.md](git-workflow.md)〈不上 main 的檔案隔離〉。

Zone 2 的成員由 `.no-merge` 定義；要增減隔離對象，改該檔案即可。

> **特例**：`.claude/settings.local.json` 雖位於隔離區的 `.claude/` 下，但它被**全域** gitignore 擋住，連 `dev_ai` 都沒有版控，因此隔離機制對它無效。此檔改以 repo 外快照備份保護，詳見 [git-workflow.md](git-workflow.md)〈Claude Code 權限設定備份〉。

## 目錄用途

| 目錄 / 檔案 | 用途 | 上 main？ |
|------|------|:---:|
| `src/<package_name>/` | 所有業務邏輯程式碼 | ✅ |
| `tests/` | 測試程式碼，結構應鏡射 `src/` 內的模組層級 | ✅ |
| `docs/` | 專案文檔與規範說明 | ✅ |
| `prompts/` | LLM 專案存放 system prompt / Jinja2 模板（詳見 [llm-integration.md](llm-integration.md)） | ✅ |
| `scripts/` | 專案工具腳本（合併、初始化等） | ✅ |
| `.githooks/` | 版控 hook（`pre-push` 阻擋隔離路徑進 main） | ✅ |
| `.agent/plans/` | 開發進度計畫書 | ❌ |
| `.agent/notes/` | 開發筆記 | ❌ |
| `logs/` | AI 每次作業完成後的改動總結紀錄（詳見 [change-log-guide.md](change-log-guide.md)） | ❌ |
| `.claude/` | Claude Code agent 設定 / hook / settings | ❌ |
| `CLAUDE.md` | Agent 主規範文件 | ❌ |
| `.no-merge` | 隔離路徑清單，供 merge 腳本與 pre-push hook 讀取 | ✅ |

## 規則

1. **不要在專案根目錄直接放置業務程式碼**，所有 Python 原始碼一律放在 `src/<package_name>/` 下。
2. 測試檔案命名統一為 `test_<module_name>.py`，放在 `tests/` 對應子目錄中。
3. 環境設定使用 `.env` 搭配 `pyproject.toml`，不將 `.env` 納入版本控制。
4. 如有額外設定檔（如 YAML），建議放在 `src/<package_name>/` 下或專案根目錄，視複雜度決定。
5. 開發計畫書、進度、筆記一律放 `.agent/` 下；不要散落在根目錄或 `docs/`，以免誤入 `main`。
6. `CLAUDE.md`、`.claude/` 因 Claude Code 固定從根目錄讀取，無法移入 `.agent/`，其隔離改由 `.no-merge` 機制處理，不需搬移。
