# 改動總結 — Phase 4 Task 4.2：估算準確度實測驗證

日期：2026-08-20
分支：dev_ai
相關 commit：`fdecd84`（Task 4.1 量測工具）、本次新增 `scripts/validate_image_estimation.py`

---

## 變更清單

1. `scripts/validate_image_estimation.py`：新增。以真實供應商回報的 token 數驗證影像估算公式的實測腳本。
2. `tests/cloud_vision_agents_setup.yaml`：Task 4.1 已新增。雲端 vision 實測用設定，只收錄 mini 級模型。
3. `tests/prompt_files/vision_probe_instruction.md`：Task 4.1 已新增。

---

## 實測方法

### 為什麼不用 `imgs/` 的相片

公式驗證需要精確控制寬高，因此改用合成的已知尺寸 PNG（完整可解碼，非僅 header）。

### 如何隔離影像成本

供應商只回報 `input_tokens` 總數，不會拆分影像與文字。作法是對每個模型先送一次**訊息結構完全相同**的基準呼叫（把影像 item 換成單字元文字 item），再送影像呼叫，兩者相減即為影像的實際成本。

> **量測口徑的重要修正**：第一版基準呼叫只用一則純文字 message，與影像呼叫的兩則結構不同，
> 導致每筆結果被多算 3~4 tokens 的 message overhead。這讓原本精準的估算全部呈現「低估 0.97~1.00x」，
> 差點得出錯誤結論。改為結構對齊後，patch-based 的四筆結果從「低估」變為 1.00~1.01x。
> 口徑錯誤會讓「估算很準」與「估算很爛」兩種結論都不可信。

### 成本

2 個模型 × (1 次基準 + 4 張影像) = 10 次呼叫，皆為 mini 級模型、輸出上限 64 tokens，總花費在 0.05 美元以內。

---

## 實測結果

### 雲端：`gpt-4.1-mini`（patch-based，multiplier 1.62，patch budget 1536）

結構對齊基準：實際輸入 27 tokens

| 尺寸 | 估算影像 | 實際影像 | 倍率 | 判定 |
|------|--------:|--------:|-----:|------|
| 256×256 | 104 | 103 | 1.01x | 通過 |
| 512×512 | 415 | 414 | 1.00x | 通過 |
| 1024×1024 | 1,659 | 1,658 | 1.00x | 通過 |
| 1024×1536 | 2,489 | 2,488 | 1.00x | 通過 |

**patch-based 公式四筆全部精確吻合**（±1 token 為基準呼叫的取整殘差）。

### 雲端：`gpt-4o-mini`（tile-based，base 2833 / per-tile 5667）

| 尺寸 | 估算影像 | 實際影像 | 倍率 | 判定 |
|------|--------:|--------:|-----:|------|
| 256×256 | 25,501 | 8,500 | 3.00x | **高估超標** |
| 512×512 | 25,501 | 8,500 | 3.00x | **高估超標** |
| 1024×1024 | 25,501 | 25,501 | 1.00x | 通過 |
| 1024×1536 | 36,835 | 36,835 | 1.00x | 通過 |

大尺寸精確吻合，小尺寸高估 3 倍。成因見下節。

### 本地：Ollama `glm-ocr-optimized:latest`

| 項目 | 實測值 |
|------|--------|
| `reserved` | 8,345 |
| 分項 | `text_tok=3`、`image_tok=3,000`、`other_tok=8`、`image_count=1` |
| `umbrella_tpm` 等待 | 0.0（連續 5 次） |
| `model_tpm` 等待 | 0.0（連續 5 次） |
| `model_rpm_chain` 等待 | 0.0（連續 5 次） |
| `global_rpm` 等待 | 0.0（連續 5 次） |
| 估算誤差 | **無法量測** |

**本地無法驗證估算準確度**：Ollama 的 OpenAI-compatible endpoint 完全不回報 usage
（`actual_total_tokens: 0`），只能確認預扣量的量級與等待時間。量測工具會明確標示
「供應商未回報 usage，無法計算估算誤差」，不會輸出假的倍率。

`image_tok=3,000` 為 `FALLBACK_IMAGE_TOKENS`——本地模型沒有對應估算器，走保守 fallback，屬預期行為。

---

## 待決事項：tile-based 小尺寸影像高估 3 倍

### 現象

`gpt-4o-mini` 對 256×256 與 512×512 的實際計費皆為 **8,500 tokens**，恰為
`base(2833) + 1 × per_tile(5667)`，即**只計 1 塊**。本實作估為 4 塊。

### 成因

官方文件描述的縮放步驟為「scale so that the image's shortest side is 768px long」。
本實作照字面實作為無條件縮放（短邊小於 768 時會**放大**），但實測顯示供應商**不放大**小圖。
此為文件敘述的兩種可能讀法，實測資料可據以判定。

### 驗證

將短邊縮放改為只縮小不放大（`min(1.0, 768 / short_side)`）後，四筆實測**全部精確吻合**：

| 尺寸 | 實測 | 目前實作 | 提議修正 |
|------|-----:|--------:|--------:|
| 256×256 | 8,500 | 25,501 ✗ | 8,500 ✓ |
| 512×512 | 8,500 | 25,501 ✗ | 8,500 ✓ |
| 1024×1024 | 25,501 | 25,501 ✓ | 25,501 ✓ |
| 1024×1536 | 36,835 | 36,835 ✓ | 36,835 ✓ |

### 狀態

**未修改，等待使用者決定。** Task 4.2 要求 4 明訂「若雲端實測發現估算誤差超出可接受範圍，
停下來報告，不要自行調整公式湊數字」。3.00x 已略微超出「高估不超過 3 倍」的建議基準。

現行行為為**高估**，符合 architecture.md 原則 3（寧可高估不可低估），不會造成 429，
代價僅為小尺寸影像佔用了不必要的 TPM 配額。因此**無立即風險**。

---

## 人工複驗清單

以下步驟可完整重現本次實測。需要 `OPENAI_API_KEY` 與本地 Ollama。

### 1. 雲端公式驗證

```bash
uv run python scripts/validate_image_estimation.py
```

**預期輸出與判讀**：

| 項目 | 預期值 | 不符時代表 |
|------|--------|-----------|
| `gpt-4.1-mini` 四個尺寸的倍率 | 1.00–1.01x | patch-based 公式或 patch budget 有誤 |
| `gpt-4o-mini` 1024×1024 倍率 | 1.00x | tile-based 的 base/per-tile 常數有誤 |
| `gpt-4o-mini` 1024×1536 倍率 | 1.00x | 縮放步驟或 tile 計數有誤 |
| `gpt-4o-mini` 256×256、512×512 倍率 | 3.00x（已知問題，見上節） | 若已套用修正則應為 1.00x |
| 是否出現低估 | 否 | 出現低估即為嚴重問題，會撞 429 |
| 是否有 TPM 卡死 | 否，全部在數秒內完成 | 卡死代表預扣量超過桶容量 |
| 是否出現 429 | 否 | 出現代表估算低估或配額設定有誤 |

單獨測某個模型或尺寸：

```bash
uv run python scripts/validate_image_estimation.py --agent PatchBasedAgent
uv run python scripts/validate_image_estimation.py --sizes 1024x1024 1024x1536
```

### 2. 本地模型驗證

```bash
uv run python tests/test_multimodal.py --measure --limit 1
```

**預期輸出與判讀**：

| 項目 | 預期值 | 不符時代表 |
|------|--------|-----------|
| `reserved` | 8,000–9,000（4000×3000 相片、`max_tokens=4096`） | 若為百萬級代表估算退回了文字計數 |
| `image_tok` | 3,000（即 `FALLBACK_IMAGE_TOKENS`） | 本地模型無估算器，此為正確的 fallback |
| `text_tok` | 個位數 | 若為千級以上代表影像混入了文字計數 |
| 誤差統計 | 顯示「供應商未回報 usage，無法計算估算誤差」 | 若印出倍率數字，代表 usage 判定有誤 |

### 3. 等待階段驗證

```bash
uv run pytest -m integration tests/test_multimodal.py -s 2>&1 | grep acquired
```

**預期**：`umbrella_tpm`、`model_tpm:*`、`model_rpm_chain`、`global_rpm` 四階段的
`wait_s` 皆為 0.0。任一階段非 0 代表本地模型的 `concurrency_only` 策略未生效。

### 4. 退款驗證

```bash
uv run pytest -m integration tests/test_multimodal.py -s 2>&1 | grep -E "refund|usage_unavailable"
```

**預期**：出現 `usage_unavailable_full_refund` 的 ERROR 與 `refund_tpm_full`。
這是 Ollama 不回報 usage 的已知行為，非錯誤。若改用雲端模型，應改為出現 `refund_tpm`
（依差額退款）而非全額退款。

---

## 測試結果

- pytest 執行結果：**202 passed / 0 failed / 2 deselected**
- 本次實測由 AI 執行（使用者提供 `OPENAI_API_KEY`，並告知測試後將移除）

### 需人工執行的項目

| 項目 | 狀態 |
|------|------|
| 雲端 vision 模型實測 | 已執行（本文件記錄實際數值） |
| 本地 Ollama 實測 | 已執行 |
| `tests/test_core.py::test_run_agent_against_real_api` | 未執行，需使用者自有的 `YAML_SETTINGS_FILE` 設定 |

---

## 備註

### 完成標準對照

| 完成標準 | 狀態 |
|---------|------|
| 估算誤差量測工具可輸出「預扣 vs 實際」統計 | 完成，兩種口徑分開輸出 |
| 本地模型實測：含影像請求無 TPM 等待 | 完成，四階段 `wait_s` 皆為 0 |
| 本地模型估算誤差在可接受範圍 | **無法驗證**——Ollama 不回報 usage |
| 雲端 vision 模型實測完成，無 429、無 TPM 卡死 | 完成，8 次影像呼叫皆正常，無 429、無卡死 |

### 本次實測的主要價值

Phase 2 的公式正確性原本只有兩項依據：官方文件的已算好範例（patch-based 兩組、Gemini 一組），
以及依文件步驟推導的 tile-based 期望值。本次實測提供了第三項、也是最強的依據——
**供應商實際計費數字**。結果證實 patch-based 公式完全正確，並揭露了 tile-based
在小尺寸影像上的一項讀法錯誤，而該錯誤是任何「對照文件」的複查方式都抓不到的。
