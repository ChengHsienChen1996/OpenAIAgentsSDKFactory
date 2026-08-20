# 改動總結 — Phase 4：估算驗證與文件同步

日期：2026-08-20
分支：dev_ai
相關 commit：

| Task | commit | 訊息 |
|------|--------|------|
| 4.1 | `fdecd84` | test: 新增估算誤差量測模式與分項歸屬單元測試 |
| 4.2 | `942ffdb` | test: 新增影像估算公式的實測驗證腳本與實測記錄 |
| 4.2 | `96b5fc2` | fix: tile-based 短邊縮放改為只縮小不放大，依實測計費校正 |
| 4.3 | `fd121de` | docs: 同步 README 與 TROUBLESHOOTING 至 Phase 2-3 的實際行為 |
| 4.4 | `96e8aed` | docs: 更新通用文檔中因限制策略而失效的敘述 |

> Task 4.2 的完整實測方法、數值與人工複驗清單另見
> [`2026-08-20_test_multimodal-estimation-validation.md`](2026-08-20_test_multimodal-estimation-validation.md)。

---

## 變更清單

### Task 4.1：估算準確度量測

1. `tests/rate_limiter/test_estimation_accuracy.py`：新增，26 個測試。驗證分項加總等於 `total`、各分項歸屬正確、估值量級合理。
2. `tests/test_multimodal.py`：修改。新增 `--measure` 誤差量測模式與 `TraceCapture`／`MeasurementRow`／`print_measurement_report()`；另加 `--yaml`／`--agent` 參數，使同一支檔案也能驅動雲端量測。
3. `tests/cloud_vision_agents_setup.yaml`：新增。雲端實測用設定，只收錄 mini 級模型，涵蓋 tile-based 與 patch-based 兩種公式家族。
4. `tests/prompt_files/vision_probe_instruction.md`：新增。

> Task 4.1 要求 1（以 `git mv` 將 `test/test_multimodal.py` 搬至 `tests/`）在 Phase 1 Task 1.3 已完成，
> `git log --follow` 目前仍可追溯至 `fd88f47`。本階段為原地擴充。

### Task 4.2：實測驗證

5. `scripts/validate_image_estimation.py`：新增。以供應商實際計費驗證影像估算公式的實測腳本。
6. `src/agent_factory/rate_limiter/image_tokens.py`：修改。`_tile_based_tokens()` 的短邊縮放改為 `min(1.0, 768 / short_side)`（只縮小、不放大），docstring 記錄實測依據。
7. `tests/rate_limiter/test_image_tokens.py`：修改。新增 `test_tile_based_matches_measured_billing`（期望值取自實際計費）與 `test_tile_based_does_not_upscale_small_images`。
8. `logs/2026-08-20_test_multimodal-estimation-validation.md`：新增。實測記錄與人工複驗清單。

### Task 4.3：專案文件更新

9. `README.md`：修改。
   - 〈影像輸入與 TPM 預扣〉整節重寫為〈影像輸入的 token 估算〉：分項機制、尺寸解析方式、估算器涵蓋範圍、fallback 行為、新增估算器的作法、前後效果對照。
   - 〈已知限制與故障排除〉：移除已修復的兩條（`MODEL_LIMITS` KeyError、雲端 vision 不支援），改列四條實際限制。
   - 〈速率限制說明〉：新增〈限制策略〉表、〈設定來源與優先序〉、〈單次預扣量超過桶容量〉；更新等待順序與 `AdaptiveUmbrella` 範例。
   - 多模態範例段：`KeyError` 提醒改為建議宣告 `concurrency_only`，範例 YAML 補上該欄位。
   - 〈核心功能〉：新增「可切換的限制策略」，多模態條目移除「雲端模型限制見下文」。
10. `docs/TROUBLESHOOTING.md`：修改。新增〈預扣估算異常的排查步驟〉，含五個步驟與 trace 分項判讀對照表。

### Task 4.4：通用文檔更新

11. `docs/llm-integration.md`：修改。**改動前已列出段落與措辭並取得使用者確認**。三處：
    - 〈模型配額設定〉的 `KeyError` 敘述 → 改為策略機制與優先序說明。
    - 〈本地模型特例〉→ 建議宣告 `concurrency_only`。
    - 〈常見問題〉的 `KeyError` 條目 → 改為 `DEFAULT_POLICY` 行為。

    三處皆**保留舊版行為的說明並標為版本差異**，未直接覆蓋。

---

## 測試結果

- pytest 執行結果：**207 passed / 0 failed / 2 deselected**
- 單元測試皆不需 API key 與網路。

### 人工執行項目

| 項目 | 執行者 | 結果 |
|------|--------|------|
| 雲端 vision 模型實測（`gpt-4o-mini`、`gpt-4.1-mini`） | AI（使用者提供金鑰） | 8 筆全部通過，倍率 1.00–1.01x |
| 本地 Ollama 實測 | AI | `reserved=8,345`，四個等待階段 `wait_s` 皆為 0 |
| `tests/test_core.py::test_run_agent_against_real_api` | — | **未執行**，需使用者自有的 `YAML_SETTINGS_FILE` 設定 |

> 使用者於本階段提供 `OPENAI_API_KEY` 供實測，並告知測試後將移除。實測已全部完成，金鑰可撤除。
> 重跑實測時需重新提供。

---

## 備註

### 實測發現並修正的公式錯誤

本階段最重要的產出。`gpt-4o-mini` 對 256×256 與 512×512 的實際計費皆為 8,500 tokens
（= `base + 1 塊`），而原實作估為 4 塊、高估 3 倍。

成因：官方文件的「scale so that the image's shortest side is 768px long」對「是否放大小圖」
有歧義。原實作採無條件縮放（會放大），實測顯示供應商不放大。改為只縮小後，四筆實測全部精確吻合。

**這個錯誤是任何「對照官方文件」的複查方式都抓不到的** —— 文件本身就有歧義，
只有供應商的實際計費能判定。修正流程依 Task 4.2 要求 4 先停下來報告、取得同意後才動手，
非為湊數字而調整公式。

修正後的實測數字已寫入 `test_tile_based_matches_measured_billing` 作為迴歸測試，
`_tile_based_tokens()` 的 docstring 也記錄了來龍去脈，避免日後有人「照文件改回去」。

### 量測口徑的兩次修正

Codex 複查重點指出「口徑錯誤會讓兩種結論都不可信」，本階段實際踩到兩次：

1. **報表口徑**：`reserved` 含 `output_buffer × max_tokens` 與各項 pad，與實際用量直接相比會系統性偏高。
   最終輸出改為兩種口徑分開列出：估算口徑（輸入估算 + system prompt vs `input_tokens`）
   與預扣口徑（`reserved` vs `total_tokens`），並在後者標明「不代表估算誤差」。
2. **基準呼叫口徑**：第一版基準只用一則純文字 message，與影像呼叫的兩則結構不同，
   每筆多算 3~4 tokens 的 message overhead，導致 patch-based 四筆全部顯示「低估 0.97~1.00x」，
   差點得出「估算會撞 429」的錯誤結論。改為結構對齊後才看到真實數字（1.00~1.01x）。

### 通用文檔的版本相容處理

`docs/llm-integration.md` 為跨專案共用，其現行敘述對**尚未升級本模組的專案仍然正確**
（舊版確實會拋 `KeyError`）。因此三處改動皆採「新行為為主 + 標註版本差異保留舊行為說明」，
而非直接覆蓋。改動經 `git diff` 確認限縮在三行，未擴散至其他章節。

另在〈本地模型特例〉補了一段舊版的注意事項：舊版「把配額設得寬鬆」的說法未講清楚
寬鬆到多少才夠，而單次預扣量超過所設 TPM 時舊版會無限等待（靜默卡死）。
本專案正是踩過此坑（多模態影像使單次預扣達 250 萬，因而填了 5000 萬 TPM）。

### 未寫入文件的未實測宣稱

依 Phase 4 已知風險「不要在文件中寫入未實測的宣稱」，README 明確區分：
`gpt-4o-mini`（tile-based）與 `gpt-4.1-mini`（patch-based）已用真實計費驗證；
其餘 12 個估算器前綴依官方文件實作、**未逐一實測**。未將兩個模型的結果擴大宣稱為全面支援。

### 本地模型無法驗證估算誤差

Ollama 的 OpenAI-compatible endpoint 完全不回報 usage，因此完成標準的
「本地模型估算誤差在可接受範圍」**無法直接驗證**，改由雲端實測背書。
量測工具會明確輸出「供應商未回報 usage，無法計算估算誤差」，不產生假的倍率數字。

### 承接自 Phase 3 的未處理事項

以下為 Phase 3 日誌已記錄、本階段未處理的既有問題，需另行決定：

1. 模組層 `registry` 內的 `AsyncLimiter`（`model_rpms` / `model_rpds`）跨 event loop 共用。
2. `global_sem`（`asyncio.Semaphore`）同上。

修正需重構 `LimitRegistry` 的生命週期。

---

## 驗收流程結果

| # | 項目 | 結果 |
|---|------|------|
| 1 | `uv run pytest` 全綠 | 207 passed / 2 deselected |
| 2 | 人工執行 Task 4.2 實測清單 | 已執行，數值見實測記錄 |
| 3 | 通讀 README 確認無過期敘述 | 已掃描 `KeyError`／`尚未支援`／`50000000`／`count_tokens(str`；僅存的 `KeyError` 為 `get_agent_by_name` 的正確敘述。內部錨點連結亦已驗證 |
| 4 | `docs/llm-integration.md` 改動已取得確認 | 已於改動前列出段落與措辭並取得同意 |
| 5 | 撰寫改動日誌 | 本文件 |
| 6 | 是否合併回 main | **由人工決定**，指令為 `scripts/merge-to-main.sh dev_ai` |

---

**Phase 4 完成。本輪四階段優化至此結束；是否合併回 `main` 由人工決定。**
