# 改動總結 — Phase 1：版控隔離機制與測試地基

日期：2026-08-20
分支：dev_ai
相關 commit：

| Task | commit | 訊息 |
|------|--------|------|
| 1.1 | `56e796d` | chore: 建立不上 main 的版控隔離機制與隔離區目錄 |
| 1.1 | `7d2b873` | docs: 新增專案文檔組與跨專案通用規範文檔 |
| 1.2 | `3e99405` | chore: 新增 pytest 測試依賴與設定，補齊 .env.example |
| 1.3 | `4af6c89` | refactor: 測試目錄 test/ 改名為 tests/ |
| 1.3 | `fcf7467` | refactor: test_basic.py 更名為 test_core.py |
| 1.3 | `a675934` | test: 將手動測試腳本改寫為 pytest 形式並依模組拆分 |
| 1.4 | `f56737b` | chore: imgs/ 排除版控，並同步 README 與 project-overview 的測試路徑 |

---

## 變更清單

### Task 1.1：版控隔離機制

1. `.no-merge`：新增。隔離路徑清單（`CLAUDE.md`、`.claude/`、`.agent/`、`logs/`），供合併腳本與 pre-push hook 共用。
2. `.githooks/pre-push`：新增。推上 `main` 前檢查差異，命中隔離路徑即中止推送。
3. `scripts/init-project.sh`：新增。建立隔離區資料夾、設定 `core.hooksPath`、補腳本執行權限。
4. `scripts/merge-to-main.sh`：新增。合併時依 `.no-merge` 剝離隔離路徑。
5. `.gitignore`：修改。新增 Python／環境設定／編輯器三組忽略規則，並註明隔離檔案刻意不列於此（保留 `.idea/` 既有條目）。
6. `.agent/plans/.gitkeep`、`.agent/notes/.gitkeep`、`logs/.gitkeep`：新增，讓空的隔離區資料夾能進版控。
7. `CLAUDE.md`：新增（Zone 2）。專案主規範與文檔索引。
8. `docs/project-overview.md`、`docs/architecture.md`：新增。本專案的目的、技術棧、架構約束。
9. `docs/project-structure.md`、`docs/coding-style.md`、`docs/testing-strategy.md`、`docs/git-workflow.md`、`docs/change-log-guide.md`、`docs/llm-integration.md`：新增。跨專案通用規範，僅納入版控，內容未做任何修改。
10. `.agent/plans/phase-1-foundation.md`、`phase-2-token-estimation.md`、`phase-3-limit-policy.md`、`phase-4-validation-and-docs.md`：新增（Zone 2）。四階段計畫書。

### Task 1.2：依賴與環境變數範例

11. `pyproject.toml`：修改。新增 `[dependency-groups].dev`（`pytest>=9.1.1`、`pytest-asyncio>=1.4.0`、`pytest-timeout>=2.4.0`，版本由 `uv add` 解析）；新增 `[tool.pytest.ini_options]`：`testpaths`、`asyncio_mode="strict"`、`asyncio_default_fixture_loop_scope="function"`、`integration` marker 註冊、`addopts = ["-m", "not integration"]`。
12. `uv.lock`：修改。鎖定新增的測試依賴。
13. `example_file/.env.example`：修改。自 2 個變數補齊為 7 個（`OPENAI_API_KEY`、`YAML_SETTINGS_FILE`、`GLOBAL_CONCURRENCY`、`RPM`、`TPM`、`OLLAMA_BASE_URL`、`OLLAMA_API_KEY`），每個上方加註用途。

### Task 1.3：測試目錄重整

14. `test/` → `tests/`：以 `git mv` 改名，先單獨 commit 純改名以保留檔案歷史。
15. `tests/test_basic.py` → `tests/test_core.py`：改名後改寫為 AgentFactory 的 pytest 測試（工廠初始化、取得 agent、靜態 instruction 載入、未知名稱拋 `KeyError`），並新增 integration 標記的真實 API 呼叫測試。改名與改寫分成兩個 commit，確保 `git log --follow` 在預設相似度門檻下仍能追溯至原始檔。
16. `tests/test_config_loader.py`：新增。自 `test_basic.py` 拆出 YAML 載入與 Pydantic 驗證的部分，涵蓋 `__dir__` 注入、`load_validated` 正常路徑、缺少動態 prompt 欄位／缺少 `base_url` 拋 `ValueError`、工廠層錯誤傳遞。
17. `tests/test_multimodal.py`：修改。改寫為 pytest 形式；修正改名後失效的 `test/` 路徑；實際呼叫 Ollama 的案例標記 `@pytest.mark.integration` 並加 `@pytest.mark.timeout(600)`；保留可直接執行的 CLI 入口。
18. `tests/conftest.py`：新增。四個 fixture —— `sample_yaml_path`、`sample_factory`（本階段使用）、`mock_run_result`、`tiny_images`（Phase 2 預留）。
19. `tests/__init__.py`、`tests/rate_limiter/__init__.py`：新增，使 `tests/` 鏡射 `src/agent_factory/` 的模組層級。
20. `tests/fixtures/sample_agents.yaml`、`tests/fixtures/prompt_files/sample_instruction.md`：新增。單元測試用最小素材（原列於 Task 1.4，因 `sample_yaml_path` fixture 相依而提前建立）。
21. `tests/multimodal_agents_setup.yaml`、`tests/prompt_files/ocr_instruction.md`：納入版控，作為多模態測試紀錄保留。

### Task 1.4：repo 缺件處理

22. `.gitignore`：修改。新增 `imgs/`（本機測試影像，體積大且內容因人而異，確認不納入版控）。
23. `README.md`：修改。修正 6 處因改名而失效的 `test/`（單數）路徑；補上 `-m integration` 的執行方式；新增「執行前需自備影像」說明。
24. `docs/project-overview.md`：修改。「已知的 repo 缺件」改為已確認的版控歸屬表；更新常用指令路徑；測試依賴列改為實際安裝版本（原標記為待確認）。

---

## 測試結果

- pytest 執行結果：**13 passed / 0 failed / 2 deselected**
- 驗證條件：自 repo 外的工作目錄執行，且 `OPENAI_API_KEY`、`YAML_SETTINGS_FILE`、`OLLAMA_BASE_URL`、`OLLAMA_API_KEY` 全部 unset，確認 `.env` 未被載入、測試不依賴任何環境變數。
- `tests/fixtures/sample_agents.yaml` 另以 `env -i`（環境變數全清空）實測 `load_validated` 與 `AgentBuilder.build` 皆通過。

### 需人工執行的項目

以下兩項標記 `integration`，`uv run pytest` 預設排除，需人工執行與驗證：

| 測試 | 前置條件 | 執行方式 |
|------|---------|---------|
| `tests/test_core.py::test_run_agent_against_real_api` | 有效的 `YAML_SETTINGS_FILE` 與 API 金鑰（付費 API） | `uv run pytest -m integration tests/test_core.py` |
| `tests/test_multimodal.py::test_run_ocr_against_local_ollama` | 本地 Ollama 執行中、自備 `imgs/` 影像 | `uv run pytest -m integration tests/test_multimodal.py -s` |

---

## 備註

### 驗收流程結果

| 項目 | 結果 |
|------|------|
| `git branch --show-current` | `dev_ai` |
| `git config core.hooksPath` | `.githooks` |
| 乾淨環境 `uv run pytest` | 13 passed，integration 已排除 |
| `CLAUDE.md`／`.agent/`／`logs/` 已追蹤 | 是，工作目錄乾淨 |
| `tests/` 鏡射 `src/agent_factory/` | `core.py`↔`test_core.py`、`config_loader.py`↔`test_config_loader.py`、`rate_limiter/`↔`rate_limiter/` |

pre-push hook 另以模擬的 push refs 實際執行過一次（模擬將 `dev_ai` 推上 `main`），確認以退出碼 1 攔截並列出 8 個違規路徑，非僅檢查設定值有無。

### 過程中修正的自身錯誤

- **Task 1.3 的 rename 歷史一度斷裂**：雖使用 `git mv`，但因改寫幅度過大，commit 時 git 的相似度偵測將 rename 判給了 `test_config_loader.py`，`test_core.py` 的歷史因而中斷。已重做為「純改名 commit → 改寫 commit」兩段，`git log --follow tests/test_core.py` 現可於預設門檻追溯至 `131ecb2`。

### 既有問題的發現與處置

- **`GLOBAL_TPM` 無使用點**：`limits_parameters.py:33` 讀入環境變數 `TPM` 後，全 repo 無任何消費點（唯一會用到的 `AdaptiveUmbrella` 為註解狀態）。已在 `.env.example` 註明「保留或移除待 Phase 3 決定」，未逕自移除——本階段不得更動 `src/`。
- **`tests/test_basic.py` 原有兩個測試無實際斷言**：`test_get_agent_not_found` 與 `test_config_validation_fail` 原以 try/except 印訊息，未拋出預期例外時仍會通過。改寫時一律改用 `pytest.raises`。

### 與架構規範的落差（待後續處理）

`docs/architecture.md` 測試要點第 1 條要求「本地 Ollama 視同付費 API，一律 mock」。目前 `test_run_ocr_against_local_ollama` 為真實呼叫，靠 `integration` marker 排除於自動測試之外——符合「不在自動測試中真正載入模型」，但尚無對應的 mock 骨架。建議於 Phase 2／3 撰寫 `wrappers.py` 測試時一併補上，屆時 `conftest.py` 的 `mock_run_result` fixture 可直接使用。

### 範圍調整

`tests/fixtures/` 原列於 Task 1.4 產出，因 Task 1.3 的 `sample_yaml_path` fixture 必須指向實際檔案才能成立（否則該階段測試無法通過、依測試策略即不可 commit），故提前於 Task 1.3 建立。
