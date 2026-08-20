# 改動總結 — Phase 3：速率限制策略化

日期：2026-08-20
分支：dev_ai
相關 commit：

| Task | commit | 訊息 |
|------|--------|------|
| 3.1 | `07aeaa6` | feat: 新增 LimitPolicy 與 Null 限制器，修正預扣超量的無限等待 |
| 3.1 / 3.3 | `564baf7` | feat: 全域 RPM 接入等待鏈，with_global_limits 標記棄用 |
| 3.2 | `5fc3154` | feat: agent YAML 可宣告 model_params.limits，優先序高於 MODEL_LIMITS |

---

## 變更清單

### Task 3.1：LimitPolicy 與 Null 限制器

1. `src/agent_factory/rate_limiter/token_bucket.py`：修改。
   - 新增 `LimitPolicy`（`enforced` / `concurrency_only` / `unlimited`）。
   - 新增 `NullTokenBucket`、`NullLimiter`：介面與真實限制器等價的 no-op 實作。
   - `AsyncTokenBucket.acquire()` 在 `amount > capacity` 時拋 `ValueError`，取代原本的無限等待。
   - `LimitRegistry` 依 policy 建立對應限制器；`bucket()` / `rpm()` 對未登錄模型改回傳 Null 實作（不再 `KeyError`），並以集合記錄已警告模型，每個模型只警告一次。三個公開方法簽名未變更。
   - `AdaptiveUmbrella.acquire()` 加入保護：預扣量超過當前 capacity 時取用全部可得額度並 warning，不拋 `ValueError`。
2. `src/agent_factory/rate_limiter/limits_parameters.py`：修改。
   - 新增 `DEFAULT_POLICY`，可由環境變數 `LIMIT_DEFAULT_POLICY` 覆寫，預設 `concurrency_only`。
   - 本地模型設定改為 `{"policy": "concurrency_only"}`，移除 5000 萬 TPM 魔數與整段相關註解。
   - 修正註解狀態的 `AdaptiveUmbrella(init_tpm=...)`：改為只加總含 `TPM` 鍵的 entry。
3. `tests/rate_limiter/test_limit_policy.py`：新增，21 個測試。

### Task 3.3（依使用者答覆調整範圍）

4. `src/agent_factory/rate_limiter/wrappers.py`：修改。
   - `with_global_limits` 保留但加上 `DeprecationWarning` 與 `.. deprecated::` docstring（使用者選項 Q1:B）。
   - 全域 RPM 接入 `limits_guard_multi` 的等待鏈，位置比照原 `with_global_limits`：模型層限制之後、併發名額之前（使用者選項 Q2:B）。等待順序更新為
     `umbrella TPM → model TPM → model RPM → model RPD → 全域 RPM → semaphore`。
5. `src/agent_factory/rate_limiter/limits_parameters.py`：修改。新增 `get_global_rpm_limiter()`，依 event loop 以 `WeakKeyDictionary` 分別建立 `AsyncLimiter`。`global_rpm_limiter` 模組層實例保留供已棄用的 `with_global_limits` 使用。
6. `tests/rate_limiter/test_wrappers_estimate.py`：修改。`test_wait_order_events_unchanged` 更名為 `test_wait_order` 並更新預期階段序列，docstring 註明此為依使用者指示的刻意變更。

### Task 3.2：限制設定移入 YAML

7. `src/agent_factory/config_schema.py`：修改。新增 `ModelLimitsConfig`（`policy` / `TPM` / `RPM` / `RPD`，`enforced` 時 TPM 與 RPM 必填）與 `to_registry_config()`；`ModelParamsConfig` 新增選填的 `limits` 欄位。
8. `src/agent_factory/rate_limiter/token_bucket.py`：修改。`register()` 新增 `source` 參數與衝突／冪等處理；新增 `_normalize_cfg()`、`SOURCE_MODEL_LIMITS`、`SOURCE_YAML`、`_SOURCE_PRIORITY`。
9. `src/agent_factory/core.py`：修改。`AgentFactory.__init__` 於建立 agent 前呼叫 `_register_model_limits()`，把 YAML 宣告的 limits 註冊進全域 registry。
10. `example_file/agents_setup_example.yaml`：修改。新增雲端模型的完整 `enforced` 宣告範例（含選填性與優先序註解）與本地模型的 `concurrency_only` 範例。
11. `tests/test_config_schema.py`：新增，22 個測試。

---

## 測試結果

- pytest 執行結果：**176 passed / 0 failed / 2 deselected**
- 新增測試皆不需 API key 與網路。

### 需人工執行的項目

| 項目 | 說明 |
|------|------|
| `tests/test_core.py::test_run_agent_against_real_api` | 需付費 API 金鑰，本次未執行 |
| `tests/test_multimodal.py::test_run_ocr_against_local_ollama` | 需本地 Ollama，本次未透過 pytest 執行；改以獨立腳本完成驗收流程第 3 項（見下） |

---

## 備註

### 驗收流程結果

| # | 項目 | 結果 |
|---|------|------|
| 1 | `uv run pytest` 全綠 | 176 passed |
| 2 | `wrappers.py` 無 policy 分支 | 以測試 `test_wrappers_contains_no_policy_branch` 自動守住，取代一次性的人工 diff 複查 |
| 3 | 本地模型連續 5 次呼叫，各階段 `wait_s` ≈ 0 | 真實 Ollama 實測，四個階段（`umbrella_tpm`、`model_tpm`、`model_rpm_chain`、`global_rpm`）各 5 筆全為 0.0 |
| 4 | 未登錄模型正常執行並只警告一次 | 連續三次取用，warning 數維持 1 |
| 5 | 極小 TPM + 大請求立即拋錯 | TPM=100 送出 8345 的請求，立即拋 `ValueError`，`timeout 20` 下 exit code 0（非 124） |

### 文件要求之間的矛盾與解法

Task 3.2 的要求 2 為「優先序 YAML > MODEL_LIMITS」，要求 3、4 為「以第一次註冊為準」。但 `MODEL_LIMITS` 於模組載入時即註冊，YAML 永遠是「第二次」——照字面實作 YAML 將永遠無法生效。

解法：`register()` 引入 `source` 參數與優先序（`model_limits` < `yaml`）。

- 設定相同 → 冪等返回，**不重建限制器**（重建會使桶內既有餘額歸零，等同靜默清空配額）
- 設定不同、來源優先序較高 → 覆寫並 log info
- 設定不同、來源相同或較低 → 保留先註冊者並 log warning，不合併

兩條規則因而同時成立。設定比較經 `_normalize_cfg()` 正規化，忽略鍵順序、`policy` 寫明與省略、`RPD` 為 None 或缺席的差異。

### Codex 複查重點的處理

| 複查點 | 結果 |
|--------|------|
| Null 與真實限制器的介面等價性 | 以 `inspect.signature` 逐一比對 `acquire`／`refund` 參數列；`NullLimiter` 驗證 `async with` 用法 |
| `registry.rpd()` 回傳 `None` 的既有分支是否仍成立 | 三種情境（enforced 無 RPD、concurrency_only、未登錄）皆回 `None`；有 RPD 時回真實 limiter |
| `AdaptiveUmbrella` 縮減後是否撞上新 `ValueError` | **確認為真**，已處理，詳見下節 |
| 退款走到 `NullTokenBucket` 是否正常 | 有測試覆蓋，不出錯 |
| 多工廠實例、重複初始化的註冊行為 | 三個測試涵蓋；其中冪等測試斷言桶物件為同一實例且餘額未被歸零 |
| 向後相容是否真的覆蓋「完全不含 limits」 | `test_yaml_without_limits_does_not_touch_registry` 斷言 registry 內桶物件仍為同一實例，非僅驗證「能通過驗證」 |

### AdaptiveUmbrella 與新 ValueError 的交互作用

`on_global_rl_error()` 每次將 capacity 乘以 `dec_mult=0.75`，下限 `min_tpm=5000`。連續觸發 5 次後 capacity 降至 5,000，而 Phase 2 實測的單筆預扣量為 8,345 —— 加上新的 `ValueError` 後將使請求直接失敗。

umbrella 是跨模型的自適應總量上限，不是單一模型的硬性配額；在此拋錯會把「全域節流」變成「單筆請求被拒」。故 `AdaptiveUmbrella.acquire()` 改為：超量時取用當前全部可得額度並 log warning，不拋錯。有測試覆蓋。

### 自行造成並修正的問題

將 `global_rpm_limiter` 接入熱路徑後，測試出現 16 條 `RuntimeWarning: This AsyncLimiter instance is being re-used across loops`。模組層 `AsyncLimiter` 單例跨 event loop 重用是 aiolimiter 明文警告的未定義行為；本模組為被下游引用的通用模組，無法假設呼叫端只有一個 loop。已改為 `get_global_rpm_limiter()` 依 loop 分別建立，警告清空。

### 已知問題（未處理，需另行決定）

同類的跨 event loop 共用問題還存在於兩處，皆為既有設計而非本次引入：

1. 模組層 `registry` 內的 `AsyncLimiter`（`model_rpms` / `model_rpds`）
2. `global_sem`（`asyncio.Semaphore`）

修正需重構 `LimitRegistry` 的生命週期，超出本階段範圍。

### 本階段改變的既有行為（Phase 4 文件任務必須反映）

1. **未登錄模型不再拋 `KeyError`**，改依 `DEFAULT_POLICY`（預設 `concurrency_only`）執行並警告一次。
2. **本地模型不再有 TPM 管制**，改為只受全域併發約束。
3. **全域 RPM 現在會實際生效**（環境變數 `RPM`）。先前它只存在於未被使用的 `with_global_limits` 內，對實際請求毫無作用；`.env.example` 中 `TPM`／`GLOBAL_TPM` 的「待 Phase 3 決定」註記仍待處理。
4. **`with_global_limits` 已棄用**，呼叫時發出 `DeprecationWarning`，計畫於下一個主要版本移除。

Phase 3 文件〈給 Phase 4 的預留〉已列出三份需更新的文件，另請併入上述第 3、4 點。

### 驗收過程中的兩個絆腳點

1. `tiny_images` fixture 是「僅 header」的檔案，Ollama 回 400 無法解碼。驗收腳本改以 zlib 產生完整可解碼的 64×64 PNG。
2. `limit_runner.trace` 在 `limits_guard_multi` 套用裝飾器時即被綁定，事後替換模組屬性無效；驗收腳本改以 logging handler 擷取 trace 事件。

---

**Phase 3 完成，等待使用者驗收後才進入 Phase 4。**
