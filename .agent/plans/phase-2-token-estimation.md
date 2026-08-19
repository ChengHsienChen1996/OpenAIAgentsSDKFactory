# Phase 2：輸入 token 估算重構（多模態）

## 目標

讓 TPM 預扣估算不再把 base64 影像當文字整串計數。做完這個階段後，含影像的請求預扣量從百萬級降到數千級，雲端 vision 模型（`gpt-4o`、`gpt-4.1`、Gemini）可以正常走完速率管制流程而不卡死在 TPM 等待，本地模型也不必再靠灌大 TPM 繞過。

## 完成標準

- [ ] `estimate_input_tokens()` 以走訪輸入結構取代 `count_tokens(str(input_))`
- [ ] `input_image` item 完全不參與文字計數，改由供應商估算器換算
- [ ] 每個估算器的 docstring 都有官方文件連結與查閱日期
- [ ] 影像尺寸以純 Python 解析 header 取得，未引入 Pillow 依賴
- [ ] 含 3 MB 影像的輸入，`total` 估算值 < 10,000（現行為約 2,550,000）
- [ ] 預扣退款在「設定模型名 ≠ 回應模型名」情境下正確發生
- [ ] trace 事件含 `text_tok` / `image_tok` / `other_tok` / `image_count` 分項
- [ ] `uv run pytest` 全綠，且新增測試不需 API key

---

## 子任務拆分

### Task 2.1：影像 token 估算註冊表

> 先做估算器再做走訪邏輯：Task 2.2 需要呼叫本任務定義的介面，先定介面可避免返工。

**產出**
- `src/agent_factory/rate_limiter/image_tokens.py`（新增）
- `tests/rate_limiter/test_image_tokens.py`（新增）

**要求**

1. 實作影像尺寸解析：

```python
def read_image_size(image_url: str) -> Optional[Tuple[int, int]]:
    """從 data URL 解析影像寬高，不解碼整張影像。

    僅 base64 解碼前 512 bytes，以純 Python 解析各格式的 header：
    JPEG（SOF marker）、PNG（IHDR）、WebP（VP8/VP8L/VP8X）、GIF（logical screen descriptor）。

    Returns:
        (width, height)；遠端 http(s) URL、格式不支援或 header 截斷時回傳 None。
    """
```

   - **遠端 URL 一律回傳 `None`，不發任何網路請求。**
   - 截斷或畸形的 header 回傳 `None`，不拋例外。

2. 實作估算器註冊表，以模型名前綴比對：

```python
IMAGE_TOKEN_ESTIMATORS: Dict[str, Callable[[int, int, str], int]] = {
    # key 為模型名前綴，value 簽名為 (width, height, detail) -> tokens
}
```

3. 至少涵蓋 OpenAI 的 tile-based 系列（`gpt-4o` 家族）、patch-based 系列（`gpt-4.1` 家族）與 Gemini。
   **待確認：各家的換算公式必須在實作時查閱該供應商當前官方文件，不得依賴既有註解或記憶。** 每個估算器的 docstring 必須包含官方文件 URL 與查閱日期。
4. 每個估算器都要有上限 clamp，對應各家的最大 token 數，避免超大影像產生失控估值。
5. 對外入口：

```python
def estimate_image_tokens(image_url: str, model_name: str, detail: str = "auto") -> int:
    """估算單張影像的 token 成本。

    尺寸不可得 → 該模型家族的保守預設值（取 high-detail 上限）並 log warning。
    模型無對應估算器 → FALLBACK_IMAGE_TOKENS 並 log warning。
    """
```

6. 所有常數（`FALLBACK_IMAGE_TOKENS`、各家族保守預設值）定義為模組層 UPPER_SNAKE_CASE，不內嵌在運算式中。

**Codex 複查重點**
- **換算公式必須逐一對照供應商當前官方文件驗證**。這是整個 phase 唯一真正的風險點：公式抄錯會讓修復失去意義，且測試會一起通過（期望值與實作可能同源抄錯）。docstring 的來源連結與日期是否確實存在且可解析。
- 是否真的只讀必要 bytes —— 若為了取尺寸而 base64 解碼整張 3 MB 影像，只是把效能問題從 token 計數搬到解碼。
- fallback 方向是否一律「保守 = 高估」（見 architecture.md 原則 3）。

---

### Task 2.2：結構化輸入 token 估算器

**產出**
- `src/agent_factory/rate_limiter/token_counter.py`（修改）
- `tests/rate_limiter/test_token_counter.py`（新增）

**要求**

1. 新增 `estimate_input_tokens(input_, model_name) -> InputTokenEstimate`，dataclass 定義見 [architecture.md](../../docs/architecture.md)〈輸入 token 估算的契約〉。
2. 走訪規則：
   1. `input_` 為 `str` → 直接 `count_tokens`
   2. `input_` 為 `list` → 逐則 message：
      - `content` 為 `str` → `count_tokens`
      - `content` 為 `list` → 逐 item 依 `type` 分派：
        - `input_text` → `count_tokens(item["text"])`
        - `input_image` → `estimate_image_tokens(...)`，**絕不對 `image_url` 做文字計數**
        - 其他 → `UNKNOWN_ITEM_TOKENS`（預設 1000）+ log warning + 設 `has_unknown_items`
   3. 每則 message 加 `PER_MESSAGE_OVERHEAD`（預設 4）
3. `input_image` 的判定不可只看 `type` 欄位：若 item 含 `image_url` 但 `type` 缺失或非預期，歸入未知 item（走保守常數），**不得**退回文字計數。
4. 保留現有 `count_tokens(text, model)` 簽名不變（system prompt 計數與外部使用仍依賴它）。
5. 畸形結構（message 缺 `content`、item 非 dict）跳過並 log warning，不拋例外 —— 估算失敗不該讓請求掛掉。

**Codex 複查重點**
- 走訪邏輯是否涵蓋 `openai-agents` SDK 實際會流經 `input_` 的所有 item 型別（除了 role/content message，是否還有 tool result 等形態）。
- 未知 item 的處理是否真的「保守高估」而非靜默略過（略過等於低估）。

---

### Task 2.3：wrappers 接線與 trace 分項

**產出**
- `src/agent_factory/rate_limiter/wrappers.py`（修改）
- `tests/rate_limiter/test_wrappers_estimate.py`（新增）

**要求**

1. `user_tok = count_tokens(str(user_input), model_name)` 改為 `estimate = estimate_input_tokens(user_input, model_name)`，`reserved` 計算改用 `estimate.total`。
2. `trace("estimate_ready", ...)` 增加 `text_tok`、`image_tok`、`other_tok`、`image_count` 四個欄位。**這些欄位是 Phase 4 估算誤差量測的唯一資料來源。**
3. `trace("enter", ...)` 的 `user_tok` key 保留（避免破壞既有 log 解析），值改為 `estimate.total`。
4. `estimate.image_count > 0` 但模型無對應估算器時，在 `enter` 階段就 log warning —— 讓使用者在開始等待前就知道估算不可靠。
5. `reserved` 的組成（user + sys + safety_pad + output_buffer × max_out + per_round_pad）維持原設計，只換 user 那一項的來源。

**Codex 複查重點**
- `reserved` 各組成項是否有任何一項在改動中被意外遺漏。
- trace 欄位名與 Phase 4 的量測腳本需要對齊 —— 命名一旦定下，Phase 4 會直接依賴。

---

### Task 2.4：預扣退款的模型名匹配修復

> 這是與影像問題同源的隱性缺陷：影像場景下單筆預扣量大，退款失效的後果被放大到「一次呼叫抽乾整個桶」。

**產出**
- `src/agent_factory/rate_limiter/wrappers.py`（修改）
- `src/agent_factory/rate_limiter/token_bucket.py`（修改）
- `tests/rate_limiter/test_refund.py`（新增）

**要求**

1. **問題現況**：校正階段用設定檔模型名（`gpt-4.1`）查 `used_by_model`，但 `raw_responses[*].model` 回傳的是供應商完整版本名（`gpt-4.1-2025-04-14`）。查不到 → `actual = 0` → 退款條件 `reserved > actual > 0` 不成立 → **整筆預扣永遠不退**。
2. 模型名匹配改為三段式：精確相符 → 前綴相符 → 單一模型時直接採用總量。
3. 退款條件修正：
   - 取得到用量 → 依差額補扣或退款
   - **完全取不到用量 → 全額退款 `reserved` 並 log warning**（寧可短暫超額，不可讓桶被靜默抽乾）
4. `AsyncTokenBucket.refund` 的 `int(self.tokens) + amount` 改為 `self.tokens + amount`，保留小數精度。

**Codex 複查重點**
- 前綴比對不可誤配：`gpt-4.1` 不得配到 `gpt-4.1-mini` 的用量。比對順序與嚴格度是關鍵。
- 全額退款的 fallback 在 tool 多回合情境下是否會造成系統性低估；若會，改為退款但同時 log error 標記需人工檢視。

---

## 驗收流程

1. `uv run pytest tests/rate_limiter/` → 全綠，無需 API key。
2. 人工檢視 `image_tokens.py` 每個估算器的 docstring，逐一點開文件連結確認公式與該頁一致。
3. 執行含影像的多模態測試（需本地 Ollama），檢視 trace 輸出：
   - `estimate_ready` 的 `image_tok` 為數千級、`text_tok` 為兩位數
   - `reserved` 總量 < 10,000
   - `model_tpm` 階段的 `wait_s` ≈ 0
4. 人工比對：同一張影像在改動前後的 `reserved` 值，應相差三個數量級。
5. 撰寫改動日誌至 `logs/`，註明哪些測試需人工執行。

## 已知風險與注意

- **不要憑記憶寫供應商的換算公式**。你的知識可能過時，且這類規則隨模型改版變動。一律查當前官方文件，並把來源寫進 docstring。
- **不要引入 Pillow**。尺寸解析用純 Python 讀 header，這是已確認的設計決定。
- **不要為了取尺寸而完整解碼 base64**。只解碼前 512 bytes。
- **不要修改 `limits_guard_multi` 的等待順序**（umbrella → model TPM → RPM → RPD → semaphore）。本階段只換估算來源，流程結構不動。
- **不要在本階段動速率限制策略**。`MODEL_LIMITS` 的內容、`LimitRegistry` 的行為、本地模型的假配額都留給 Phase 3。看到 `glm-ocr-optimized:latest` 那個 5000 萬的 TPM 魔數請忍住，它是 Phase 3 Task 3.1 的事。
- **不要改 `LimitAgentRunner.run()` 的簽名**（見 architecture.md 原則 1，跨專案契約）。

## 給 Phase 3 的預留

- Task 2.3 建立的 trace 分項欄位，Phase 4 的估算誤差量測會直接依賴，欄位名不要在後續階段隨意更動。
- Task 2.4 修好退款後，Phase 3 把本地模型改為 `concurrency_only` 時，退款路徑會走到 `NullTokenBucket` 的 no-op 實作 —— Phase 3 需確認這條路徑不會因為「退款到一個不存在的桶」而出錯。

---

**完成後暫停，等使用者驗收，才進入 Phase 3。**
