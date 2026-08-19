# Phase 3：速率限制策略化

## 目標

讓速率限制對每種模型都有意義：雲端模型維持完整管制，本地／自架模型只受併發數約束、不再需要填假的 TPM/RPM 魔數，未登錄的模型也不再直接 `KeyError`。同時修掉「單次預扣量超過桶容量時無限等待」這個會讓程式靜默卡死的根因。

## 完成標準

- [ ] `LimitPolicy` 三種策略（`enforced` / `concurrency_only` / `unlimited`）可用
- [ ] `wrappers.py` 內**沒有任何** policy 相關的 if/else 分支（以 diff 驗證）
- [ ] 本地模型改為 `{"policy": "concurrency_only"}`，5000 萬 TPM 魔數已移除
- [ ] `AsyncTokenBucket.acquire(amount > capacity)` 拋出 `ValueError` 而非無限等待
- [ ] 未登錄模型依 `DEFAULT_POLICY` 運作並 log warning（每個模型只警告一次）
- [ ] agent YAML 可透過 `model_params.limits` 宣告限制，優先序 YAML > `MODEL_LIMITS` > `DEFAULT_POLICY`
- [ ] `with_global_limits` 死碼已移除
- [ ] 既有不含 `limits` 欄位的 YAML 完全不受影響

---

## 子任務拆分

### Task 3.1：LimitPolicy 與 Null 限制器

**產出**
- `src/agent_factory/rate_limiter/token_bucket.py`（修改）
- `src/agent_factory/rate_limiter/limits_parameters.py`（修改）
- `tests/rate_limiter/test_limit_policy.py`（新增）

**要求**

1. 新增介面相容的 no-op 實作：

```python
class NullTokenBucket:
    """介面與 AsyncTokenBucket 相同，所有操作立即返回。"""
    async def acquire(self, amount: int) -> None: ...
    async def refund(self, amount: int) -> None: ...


class NullLimiter:
    """介面與 aiolimiter.AsyncLimiter 相同的 async context manager，立即通過。"""
    async def __aenter__(self): ...
    async def __aexit__(self, *exc_info): ...
```

   **關鍵**：`wrappers.py` 不得因此新增任何分支。策略差異由 registry 回傳的物件型別吸收（見 [architecture.md](../../docs/architecture.md) 原則 2）。

2. `MODEL_LIMITS` 的 entry 支援 `policy` 欄位，三種值見 architecture.md〈限制策略〉表。`concurrency_only` 與 `unlimited` 不需再填 TPM/RPM。
3. `LimitRegistry` 依 policy 建立對應限制器；`bucket()` / `rpm()` / `rpd()` 對未登錄模型依 `DEFAULT_POLICY`（模組常數，可由環境變數 `LIMIT_DEFAULT_POLICY` 覆寫，預設 `concurrency_only`）回傳對應實作，並在首次遇到該模型時 log warning 一次（用集合記錄已警告過的模型名，避免洗版）。
4. **修正無限等待**：`AsyncTokenBucket.acquire` 在 `amount > self.capacity` 時直接拋 `ValueError`，訊息明確指出「單次請求預扣量超過該模型 TPM 上限，請檢查估算或調整設定」。即使 Phase 2 已讓估算正確，此防護仍須保留 —— 它是「靜默卡死」與「明確報錯」的差別。
5. `limits_parameters.py` 的本地模型設定改為：

```python
"glm-ocr-optimized:latest": {"policy": "concurrency_only"},
```

   並移除原本關於 5000 萬 TPM 的整段註解。

**Codex 複查重點**
- Null 實作與真實限制器的介面等價性，特別是 `AsyncLimiter` 作為 async context manager 的用法，以及 `registry.rpd()` 回傳 `None` 的既有分支是否仍成立。
- `DEFAULT_POLICY` 改變了「未登錄模型即 KeyError」的既有行為 —— 確認 Phase 4 的文件任務有涵蓋此變更。
- `AdaptiveUmbrella` 縮減 capacity 後，既有的 in-flight 預扣是否會突然超過 capacity 而拋出新的 `ValueError`。若會，該路徑需另行處理並在交付說明中指出。
- Phase 2 修好的退款路徑走到 `NullTokenBucket` 時是否正常（退款到 no-op 桶不應出錯）。

---

### Task 3.2：限制設定移入 YAML

**產出**
- `src/agent_factory/config_schema.py`（修改）
- `src/agent_factory/rate_limiter/limits_parameters.py`（修改）
- `src/agent_factory/core.py`（修改）
- `example_file/agents_setup_example.yaml`（修改）
- `tests/test_config_schema.py`（新增或擴充）

**要求**

1. `ModelParamsConfig` 新增選填欄位：

```python
class ModelLimitsConfig(BaseModel):
    policy: Literal["enforced", "concurrency_only", "unlimited"] = "enforced"
    TPM: Optional[int] = None
    RPM: Optional[int] = None
    RPD: Optional[int] = None
```

   驗證規則：`policy="enforced"` 時 `TPM` 與 `RPM` 必填，缺少時拋出含欄位路徑的錯誤（沿用現行 `AgentConfigLoader` 包裝 agent name 的作法）。

2. 工廠初始化時將各 agent 宣告的 limits 註冊進 `registry`（以模型名為 key）。優先序：**YAML > `MODEL_LIMITS` > `DEFAULT_POLICY`**。
3. 同一模型被多個 agent 以不同 limits 宣告時：以第一次註冊為準並 log warning，**不做合併**（合併會產生隱性行為）。
4. `registry` 為模組層全域單例，動態註冊需冪等：重複註冊相同設定不應報錯，註冊衝突時 warning 而非靜默覆蓋。
5. `example_file/agents_setup_example.yaml` 補上本地模型宣告 `limits: {policy: concurrency_only}` 的範例。
6. **向後相容**：既有不含 `limits` 欄位的 YAML 行為完全不變。

**Codex 複查重點**
- 多工廠實例、重複初始化情境下的註冊行為（這是全域單例最容易出事的地方）。
- 向後相容測試是否真的覆蓋「完全不含 limits 欄位」的既有設定路徑。

---

### Task 3.3：死碼清理

**產出**
- `src/agent_factory/rate_limiter/wrappers.py`（修改）
- `src/agent_factory/rate_limiter/__init__.py`（修改）

**要求**

1. 移除 `with_global_limits` 函式定義，並自 `__init__.py` 的匯出清單移除。
2. 移除前先確認無外部使用：本模組會被下游專案引用，而本 repo 內看不到使用端。**向使用者確認目前有哪些專案引用本模組、該符號是否被使用，取得確認後才移除**，不要僅憑本 repo 內 `grep` 無結果就判定為死碼。
3. `global_sem` 與 `global_rpm_limiter` 保留 —— 前者仍被 `limits_guard_multi` 使用；後者**待確認：目前無使用點，請向使用者確認保留或移除**，不要自行判定為死碼。

**Codex 複查重點**
- `grep -rn "with_global_limits"` 全 repo 無結果。
- 是否誤刪了仍被使用的 `global_sem`。

---

## 驗收流程

1. `uv run pytest` → 全綠。
2. `git diff` 檢視 `wrappers.py`：確認**沒有**新增任何 policy 分支。
3. 本地模型連續呼叫 5 次（需 Ollama），檢視 trace：`umbrella_tpm`、`model_tpm`、`model_rpm_chain` 三階段的 `wait_s` 皆應 ≈ 0。
4. 以一個未在任何設定中出現的模型名建立 agent 並呼叫 → 應正常執行並出現一次 warning，第二次呼叫不再重複 warning。
5. 手動把某模型的 TPM 設得極小後送出大請求 → 應立即拋 `ValueError`，不是掛住（用 timeout 驗證）。
6. 撰寫改動日誌至 `logs/`。

## 已知風險與注意

- **不要在 `wrappers.py` 加 policy 分支**。這是本階段的核心設計約束，違反即失敗。
- **不要修改 `LimitRegistry` 的三個公開方法簽名**（`bucket()` / `rpm()` / `rpd()`）—— 跨專案契約，見 architecture.md 原則 1。
- **不要順手「簡化」`AdaptiveUmbrella`**。它目前預設未啟用（`umbrella = NoopUmbrella()`），看起來像死碼但是刻意保留的可選實作。
- **不要把 `DEFAULT_POLICY` 設成 `enforced`**。那會讓未登錄模型走進「用預設配額管制」的隱性行為，比 `KeyError` 更難除錯。
- **不要動 Phase 2 完成的估算邏輯**。本階段只碰限制策略。
- 本階段改變了兩處既有行為（未登錄模型不再 KeyError、本地模型不再有 TPM 管制），**兩者都必須在 Phase 4 的文件任務中反映**。

## 給 Phase 4 的預留

本階段完成後，以下三份文件的敘述會與實作不符，Phase 4 必須逐一處理：

1. 本專案 `README.md`〈已知限制與故障排除〉的「`MODEL_LIMITS` 未涵蓋的模型會拋 `KeyError`」
2. 本專案 `README.md`〈影像輸入與 TPM 預扣〉整節（Phase 2 已使其失效）
3. **通用文檔 `docs/llm-integration.md`** 的兩處：「使用未定義在 `MODEL_LIMITS` 中的模型會導致 `KeyError`」與「本地模型的 `MODEL_LIMITS` 可依本機能力調整」。此文件為跨專案共用，更新方式見 Phase 4 Task 4.4。

---

**完成後暫停，等使用者驗收，才進入 Phase 4。**
