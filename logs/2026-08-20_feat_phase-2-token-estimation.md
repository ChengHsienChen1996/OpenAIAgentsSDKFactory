# 改動總結 — Phase 2：輸入 token 估算重構（多模態）

日期：2026-08-20
分支：dev_ai
相關 commit：

| Task | commit | 訊息 |
|------|--------|------|
| 2.1 | `f651cb1` | feat: 新增影像 token 估算註冊表 |
| 2.2 | `15766e0` | feat: 新增結構化輸入 token 估算，影像不再進文字計數 |
| 2.3 | `d43184c` | feat: wrappers 改用結構化輸入估算並輸出 trace 分項 |
| 2.4 | `5ff0fc9` | fix: 修復預扣退款失效與退款精度流失 |

---

## 變更清單

### Task 2.1：影像 token 估算註冊表

1. `src/agent_factory/rate_limiter/image_tokens.py`：新增。內含
   - `read_image_size()`：純 Python 解析 JPEG(SOF)／PNG(IHDR)／WebP(VP8·VP8L·VP8X)／GIF(LSD) 的 header 取寬高，未引入 Pillow。遠端 URL 一律回 `None` 且不發網路請求。
   - `IMAGE_TOKEN_ESTIMATORS`：前綴比對註冊表，涵蓋 OpenAI tile-based（gpt-4o／gpt-4.1／gpt-4.5／o1／o3）、OpenAI patch-based（gpt-4.1-mini／nano、gpt-5 家族、o4-mini）、Google Gemini。每個估算器的 docstring 附官方文件 URL 與查閱日期（2026-08-20）。
   - `estimate_image_tokens()` 對外入口，及 `UNKNOWN_SIZE_TOKENS`／`FALLBACK_IMAGE_TOKENS` 等模組層常數。
   - `has_image_estimator()`（Task 2.3 加入）：供呼叫端在等待配額前判斷估算是否可靠。
2. `tests/rate_limiter/test_image_tokens.py`：新增，55 個測試。

### Task 2.2：結構化輸入 token 估算器

3. `src/agent_factory/rate_limiter/token_counter.py`：修改。新增 `InputTokenEstimate` dataclass 與 `estimate_input_tokens()`，以明確白名單走訪 `input_`。`count_tokens()` 簽名與行為未動。
4. `tests/rate_limiter/test_token_counter.py`：新增，29 個測試。

### Task 2.3：wrappers 接線與 trace 分項

5. `src/agent_factory/rate_limiter/wrappers.py`：修改。`count_tokens(str(user_input))` 改為 `estimate_input_tokens()`；`estimate_ready` 增加 `text_tok`／`image_tok`／`other_tok`／`image_count`／`has_unknown_items`；有影像但模型無估算器時於 `enter` 階段 log warning。等待順序與 `reserved` 五項組成未動。
6. `tests/rate_limiter/test_wrappers_estimate.py`：新增，10 個測試。

### Task 2.4：預扣退款修復

7. `src/agent_factory/rate_limiter/wrappers.py`：修改。新增模組層的 `is_version_variant()` 與 `resolve_actual_usage()`（三段式比對）；校正階段改用其結果，取不到用量時全額退款並 log error；umbrella 同步採相同規則。
8. `src/agent_factory/rate_limiter/token_bucket.py`：修改。`AsyncTokenBucket.refund` 的 `int(self.tokens) + amount` 改為 `self.tokens + amount`。
9. `tests/conftest.py`：修改。`mock_run_result` 預設不再帶 `model` 欄位（貼合真實 `ModelResponse`），新增 `include_model_name` 參數供比對路徑測試使用。
10. `tests/rate_limiter/test_refund.py`：新增，26 個測試。

---

## 測試結果

- pytest 執行結果：**133 passed / 0 failed / 2 deselected**（`tests/rate_limiter/` 單獨執行為 87 passed）
- 新增測試皆不需 API key 與網路。

### 需人工執行的項目

| 項目 | 說明 |
|------|------|
| 逐一點開 `image_tokens.py` 各估算器 docstring 的官方文件連結，確認公式與該頁一致 | **這是本 phase 唯一無法由自動測試擔保的部分**。已盡量以官方文件的「已算好範例」當測試期望值（見下），但 tile-based 缺官方範例，仍需人工複查 |
| `tests/test_multimodal.py::test_run_ocr_against_local_ollama` | 需本地 Ollama 與自備 `imgs/` 影像。已執行過一次，結果見下 |
| `tests/test_core.py::test_run_agent_against_real_api` | 需付費 API 金鑰，本次未執行 |

---

## 備註

### 公式正確性的驗證方式

Codex 複查重點指出「期望值與實作可能同源抄錯」。因此測試期望值優先取自供應商官方文件中**已算好的範例**，而非由本實作推導：

| 對象 | 期望值來源 | 結果 |
|------|-----------|------|
| patch-based 1024×1024 → 1024 patches | OpenAI 文件範例 | 相符 |
| patch-based 1800×2400 → 縮至 1056×1408 → 1452 patches | OpenAI 文件範例 | 相符 |
| Gemini 960×540 → 6 塊 → 1548 tokens | Gemini 文件範例 | 相符 |
| tile-based | 官方無已算好範例，依文件三步驟推導，各測試註解寫明推導過程 | 待人工複查 |

第二組同時驗證了 shrink factor 公式（含 adjusted factor 的取整損失比），實作算出的縮放後尺寸與文件完全一致。

### 與 phase 文件不符、已依實況調整的三處

1. **模型家族歸類**：phase 文件寫「patch-based 系列（gpt-4.1 家族）」。查當前官方文件，`gpt-4.1` 與 `gpt-4o` 同為 **tile-based**，patch-based 適用的是 `gpt-4.1-mini`／`nano` 與 GPT-5 家族。已以官方文件為準。因 `MODEL_LIMITS` 同時含 `gpt-4.1` 與 `gpt-4.1-mini`，註冊表採**最長前綴優先**比對，並有專門測試守住。

2. **header 讀取量**：phase 文件寫「只解碼前 512 bytes」。實測 `imgs/` 四張相機直出相片的 SOF marker 位於 offset 62,560–66,425（EXIF 內嵌縮圖所致），512 bytes 一律解析失敗、全部退回保守值。改為分段讀取：先解 512 bytes，僅 JPEG 在找不到 SOF 時擴讀至 128 KB 上限。對 2.7 MB 相片仍只解碼約 5%，「不完整解碼」的設計意圖維持。

3. **退款失效的成因**：phase 文件描述為「設定檔模型名與回應版本名對不上」。實際查證 `openai-agents 0.3.3`，`ModelResponse` 只有 `output`／`usage`／`response_id`，**沒有 `model` 欄位**，因此 `used_by_model` 恆為空、模型層預扣**從未退款過**。三段式比對中的前兩段在此版本永遠不會命中，實際生效的是第三段（單一模型取總量）；前兩段保留以相容未來 SDK。

### 實測效果（單張 2.69 MB／4000×3000 相片）

| 模型 | 改動前 | 改動後 | 降幅 |
|------|-------:|-------:|-----:|
| `gpt-4o` | 2,439,832 | 776 | 3,144× |
| `gpt-4.1-mini` | 2,439,832 | 2,417 | 1,009× |
| `gemini-2.5-flash` | 2,439,832 | 1,043 | 2,339× |
| `glm-ocr-optimized:latest`（無估算器） | 2,439,832 | 3,011 | 810× |

本地 Ollama 實跑的 trace：

```
estimate_ready {"reserved_tokens": 8345, "user_tok": 3011, "sys_tok": 55,
                "text_tok": 3, "image_tok": 3000, "other_tok": 8, "image_count": 1}
```

`reserved` 由改動前的約 2,560,796 降至 **8,345**（約 307 倍），組成核對無誤：
`3011 + 55 + 64 + int(1.2×4096) + 300 = 8345`。

### 走訪邏輯避開的兩個陷阱

SDK 的 `TResponseInputItem` 是 21 種型別的 union。其中兩種夾帶影像資料，若以「掃描所有字串欄位」的通用寫法處理，會以另一個入口重現同一個 bug：

- `computer_call_output.output`：是截圖 dict，內含 base64 `image_url`
- `image_generation_call.result`：是 base64 影像字串

因此走訪採明確白名單（只有確定為純字串的欄位才計數），兩者各有一個專門測試守住。

### 已知問題（Phase 3 處理）

實跑確認 **Ollama 完全不回報 usage**（`actual_total_tokens: 0`、`used_by_model: {}`），因此每次呼叫都走全額退款路徑，TPM 管制對其實質失效。依 Codex 複查重點的建議，此情況以 `logger.error` 標記需人工檢視，而非 warning。

副作用是本地 Ollama 每次呼叫都會產生一條 ERROR。此噪音預期在 Phase 3 消失 —— 本地模型改為 `concurrency_only` 後，退款路徑會走 no-op 實作而不再經過此處。在此之前無法區分「本地模型、不在乎」與「雲端模型、出事了」，故刻意保留。

### 前次日誌的數字更正

`2026-08-20_chore_phase-1-foundation.md` 之後的口頭報告中，曾稱舊做法「光是估算就花 40.8 秒」。該次量測含 tiktoken 首次載入 encoder 的冷啟動時間。暖機後重測三次均為 **1.05 秒**。舊做法每次請求多付的是約 1 秒，非 40 秒。

---

**Phase 2 完成，等待使用者驗收後才進入 Phase 3。**
