# Phase 1：版控隔離機制與測試地基

## 目標

讓後續每一個 task 都能被「版控隔離、自動測試、留下改動日誌」三件事支撐起來。做完這個階段後，repo 具備 Zone 1／Zone 2 分區、`tests/` 目錄可用 `uv run pytest` 執行、`.env.example` 與依賴宣告完整 —— 也就是後續三個 phase 的驗收流程真的跑得起來。

## 完成標準

- [ ] `.no-merge`、`scripts/merge-to-main.sh`、`.githooks/pre-push`、`init-project.sh` 已就位，`git config core.hooksPath` 已指向 `.githooks`
- [ ] `CLAUDE.md`、`docs/`、`.agent/plans/` 內的文件組已進版控（`CLAUDE.md` 與 `.agent/` 屬 Zone 2）
- [ ] `test/` 已改名為 `tests/` 並鏡射 `src/agent_factory/` 的模組層級
- [ ] `uv run pytest` 可在無 API key 環境下執行且全數通過
- [ ] `pyproject.toml` 已宣告 `pytest`、`pytest-asyncio`、`pytest-timeout`
- [ ] `example_file/.env.example` 涵蓋 project-overview 環境變數表的所有項目
- [ ] repo 缺件問題（`multimodal_agents_setup.yaml` 等）已向使用者確認並處理

---

## 子任務拆分

### Task 1.1：建立版控隔離機制

**產出**
- `.no-merge`
- `scripts/merge-to-main.sh`
- `scripts/init-project.sh`
- `.githooks/pre-push`
- `.gitignore`（更新）

**要求**

1. 從通用規範來源複製上述四個檔案至專案根目錄，內容**原樣不改**。
2. 執行 `scripts/init-project.sh`，它會建立 `.agent/`、`logs/`，確保 `.no-merge` 存在，並設定 `git config core.hooksPath .githooks`。
3. 將本文件組（`CLAUDE.md`、`docs/project-overview.md`、`docs/architecture.md`、`.agent/plans/*.md`）與通用規範文檔（`docs/` 下六份）放進對應位置並 commit。
4. 現有 `.gitignore` 保留，不刪除既有的 `.idea/` 相關條目。

**Codex 複查重點**
- 隔離清單內容與通用規範一致，未因本專案自行增刪。
- `pre-push` hook 確實生效（`git config core.hooksPath` 有值）。

---

### Task 1.2：補齊依賴與環境變數範例

**產出**
- `pyproject.toml`
- `example_file/.env.example`

**要求**

1. `pyproject.toml` 新增測試依賴群組：`pytest`、`pytest-asyncio`、`pytest-timeout`。**待確認：具體版本下限由 `uv add` 解析當前可用版本決定，不要憑記憶寫死版本號。**
2. 新增 `[tool.pytest.ini_options]`：設定 `asyncio_mode`、`testpaths = ["tests"]`，並註冊 `integration` marker（供需要真實服務的測試標記用）。
3. `example_file/.env.example` 補齊為 project-overview 環境變數表的完整內容：`OPENAI_API_KEY`、`YAML_SETTINGS_FILE`、`GLOBAL_CONCURRENCY`、`RPM`、`TPM`，另加多模態測試用的 `OLLAMA_BASE_URL`、`OLLAMA_API_KEY`。每個變數上方加一行註解說明用途。

**Codex 複查重點**
- `.env.example` 的每個變數都能在程式碼中找到對應讀取點（`grep` 驗證）；找不到的應在註解標明用途或提報移除。

---

### Task 1.3：測試目錄重整

**產出**
- `tests/__init__.py`
- `tests/conftest.py`
- `tests/rate_limiter/`（目錄）
- `tests/test_core.py`、`tests/test_config_loader.py`（自 `test/test_basic.py` 拆分）
- 刪除 `test/test_basic.py`

**要求**

1. 以 `git mv` 進行改名與搬移（保留檔案歷史），不要刪除重建。
2. `test/test_basic.py` 目前是「一連串 `print` + `assert` 的手動腳本」，改寫為標準 pytest 形式：
   - 每個 `test_` 函式獨立、不互相傳遞回傳值
   - 共用的 `AgentFactory` 實例改為 `conftest.py` 的 fixture
   - 移除 `print("✅ ...")`，改以 assert 表達
3. 依模組層級拆分：`AgentFactory` 相關 → `tests/test_core.py`；YAML 載入與 Pydantic 驗證 → `tests/test_config_loader.py`。
4. `conftest.py` 建立以下 fixtures，**本階段實作但部分尚未使用，為 Phase 2／3 預留**：
   - `sample_yaml_path`：指向測試用最小 YAML 設定
   - `mock_run_result`：模擬 `RunResult`，含可設定的 `raw_responses[*].usage.total_tokens` 與 `.model`（Phase 2 Task 2.4 的退款測試會用）
   - `tiny_images`：以 bytes 常數提供最小的合法 JPEG／PNG／WebP／GIF header（Phase 2 Task 2.2 的尺寸解析測試會用）
5. 原本需要真實 API key 的測試標記 `@pytest.mark.integration`，預設不執行。

**Codex 複查重點**
- 改寫後的測試是否仍覆蓋原本 `test_basic.py` 驗證的四件事（工廠初始化、取得 agent、取不到時拋 KeyError、設定驗證失敗拋 ValueError）—— 重構過程最容易靜默掉測試覆蓋。
- fixture 是否真的用 `git mv` 保留了歷史。

---

### Task 1.4：處理 repo 缺件

**產出**
- `tests/fixtures/`（測試用最小 YAML 與 prompt）
- 視使用者回覆而定的其他檔案

**要求**

1. 先向使用者確認以下三項是刻意排除還是遺漏（**不要自行猜測後補檔**）：
   - `test/multimodal_agents_setup.yaml`
   - `test/prompt_files/ocr_instruction.md`
   - `imgs/`
2. 無論上述結論如何，`tests/fixtures/` 下建立**不依賴外部服務**的最小測試素材：一份只宣告單一 agent 的 YAML、一份兩行的 prompt md。這是 `sample_yaml_path` fixture 的目標，讓單元測試不依賴使用者的私人設定檔。
3. `imgs/` 若確認為刻意排除，在 `.gitignore` 明確列出並於 README 說明測試前需自備影像。

**Codex 複查重點**
- `tests/fixtures/` 的 YAML 是否真的不需要任何環境變數或網路即可通過 `AgentConfigLoader.load_validated`。

---

## 驗收流程

1. `git branch --show-current` → 應為 `dev_ai`。
2. `git config core.hooksPath` → 應輸出 `.githooks`。
3. 在乾淨環境（無 `.env`、無 API key）執行 `uv run pytest` → 應全數通過，且 integration 測試被跳過。
4. 檢查 `git status`：`CLAUDE.md`、`.agent/`、`logs/` 已被追蹤。
5. 人工檢視 `tests/` 結構是否鏡射 `src/agent_factory/`。

## 已知風險與注意

- **不要修改任何通用規範文檔的內容**（`docs/` 下的六份）。它們是跨專案共用的唯一真實來源，本專案只引用不改寫。
- **不要在本階段動 `src/` 下的任何業務邏輯**。本階段只做結構、依賴、測試地基；`wrappers.py`、`token_counter.py` 等一行都不改。
- **不要為了讓測試通過而放寬斷言**。如果重構後某個測試不再成立，停下來報告，不要改斷言或加 `skip`。
- **不要自行補上缺件**（Task 1.4 第 1 點）。憑猜測產生的 YAML 或 prompt 會與使用者實際的設定不一致，比缺件更難發現。
- `pytest-asyncio` 的 `asyncio_mode` 設定值在不同版本間語意有變動，**待確認：安裝後以實際版本文件為準**，不要憑記憶填。

## 給 Phase 2 的預留

本階段在 `conftest.py` 建立的 `mock_run_result` 與 `tiny_images` 兩個 fixture，本階段不使用，是 Phase 2 的 Task 2.2（影像尺寸解析）與 Task 2.4（退款校正）的測試基礎。若這兩個 fixture 的形狀在 Phase 2 發現不合用，屬預期內的調整，直接修改即可。

---

**完成後暫停，等使用者驗收，才進入 Phase 2。**
