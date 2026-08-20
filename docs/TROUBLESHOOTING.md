# 故障排除文件

本文件收錄使用本框架時可能遇到的**外部供應商相容性問題**與**設定排查步驟**，非框架本身的 bug。每則條目採固定結構：問題描述 → 原因說明 → 解決方式 → 驗證方式。

---

## 目錄

- [Gemini OpenAI-compatible Endpoint 與新版 API Key 相容性問題](#gemini-openai-compatible-endpoint-與新版-api-key-相容性問題)
- [預扣估算異常的排查步驟](#預扣估算異常的排查步驟)

---

## Gemini OpenAI-compatible Endpoint 與新版 API Key 相容性問題

### 問題描述

使用 Google AI Studio 新建立的 API key（`AQ.` 前綴），透過 Gemini 的 OpenAI-compatible endpoint 呼叫時，會出現以下錯誤：

```
400 Multiple authentication credentials received
```

即使 key 本身有效、環境變數設定正確，請求仍會被拒絕。

### 原因說明

這是 Google 端的已知行為，非本框架問題。`AQ.` 前綴的 key 為 Google AI Studio 新格式，在透過 OpenAI-compatible endpoint（`https://generativelanguage.googleapis.com/v1beta/openai/`）呼叫時，Google 的後端會同時偵測到 header 中的 Bearer token 與其他認證資訊，導致「多重認證衝突」錯誤。

舊格式 `AIza` 開頭的 key（由 Google Cloud Console 建立）可正常運作。

### 解決方式：從 Google Cloud Console 建立舊格式 API Key

1. 前往 [https://console.cloud.google.com](https://console.cloud.google.com)

2. 選擇或新建一個 GCP 專案

3. 啟用 **Generative Language API**：
   - 左側選單 → `APIs & Services` → `Enabled APIs & Services`
   - 點擊 `+ ENABLE APIS AND SERVICES`
   - 搜尋「Generative Language API」→ 點選後按 **Enable**

4. 建立 API key：
   - `APIs & Services` → `Credentials`
   - 點擊 `+ CREATE CREDENTIALS` → 選擇 `API key`

5. 產生的 key 會是 `AIza` 開頭的舊格式，複製並填入 `.env`：
   ```env
   GEMINI_API_KEY="AIza..."
   ```

6. （建議）限制 key 的存取範圍，避免濫用：
   - 點擊剛建立的 key 進入編輯頁面
   - `API restrictions` → 選擇 `Restrict key`
   - 從清單中勾選 **Generative Language API** → 儲存

### 驗證方式

以下程式碼可用於直接驗證 key 是否可透過 OpenAI-compatible endpoint 正常運作，不依賴本框架：

```python
import asyncio, os
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

async def main():
    client = AsyncOpenAI(
        api_key=os.getenv("GEMINI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    resp = await client.chat.completions.create(
        model="gemma-4-31b-it",
        messages=[{"role": "user", "content": "hi"}],
    )
    print(resp.choices[0].message.content)

asyncio.run(main())
```

- 若正常印出回覆內容，表示 key 格式正確，可填入 YAML 設定的 `client.api_key`
- 若仍出現 `400 Multiple authentication credentials received`，請確認 key 開頭為 `AIza`，而非 `AQ.`

---

## 預扣估算異常的排查步驟

### 問題描述

含影像的請求出現以下任一症狀：

- 卡在 `model_tpm` 階段很久，或直接拋出 `ValueError: 單次請求預扣量 ... 超過該模型 TPM 上限`
- 預扣量明顯偏離預期（例如單張影像就到數十萬、數百萬 token）
- 撞到供應商 429

### 原因說明

預扣量 `reserved` 的組成為：

```
reserved = 輸入估算 + system prompt + safety_pad + output_buffer × max_tokens + per_round_pad
```

影像相關的問題幾乎都出在「輸入估算」這一項。依下列順序逐步縮小範圍。

### 排查步驟

#### 步驟 1：看 trace 的分項，判斷問題出在文字還是影像

把 `rate.guard` logger 設為 `INFO`，找 `estimate_ready` 事件：

```
estimate_ready {"reserved_tokens": 8345, "user_tok": 3011, "sys_tok": 55,
                "text_tok": 3, "image_tok": 3000, "other_tok": 8,
                "image_count": 1, "has_unknown_items": false}
```

| 觀察 | 代表 | 往下看 |
|------|------|--------|
| `text_tok` 為千級以上而輸入只有短短一句 | 影像被當成文字計數了 | 步驟 2 |
| `image_count` 為 0 但確實傳了影像 | 影像 item 未被辨識 | 步驟 2 |
| `has_unknown_items` 為 `true` | 有 item 走了保守常數（每個 1,000） | 步驟 2 |
| `image_tok` 恰為 3,000 | 走了 `FALLBACK_IMAGE_TOKENS`，模型沒有對應估算器 | 步驟 3 |
| `image_tok` 為該家族的 high-detail 上限 | 尺寸解析失敗 | 步驟 4 |
| 分項都正常但 `reserved` 仍偏高 | 問題不在估算，而在 `max_tokens` 設定 | 步驟 5 |

#### 步驟 2：確認影像 item 的結構

`input_image` item 必須同時具備 `type` 與 `image_url` 兩個欄位：

```python
{"type": "input_image", "detail": "auto", "image_url": "data:image/png;base64,..."}
```

`type` 缺失或非 `"input_image"` 時，該 item 會被歸為未知 item（計入 1,000 的保守常數），**不會**退回文字計數。若 `text_tok` 異常高，檢查是否把 base64 字串放進了 `input_text` 的 `text` 欄位。

#### 步驟 3：確認估算器是否命中

```python
from agent_factory.rate_limiter.image_tokens import has_image_estimator, _match_prefix

has_image_estimator("gpt-4.1-mini")   # True
_match_prefix("gpt-4.1-mini")         # 'gpt-4.1-mini'
```

比對取**最長符合的前綴**。常見狀況：

- 模型名帶了供應商前綴（如 `openai/gpt-4o`）→ 前綴比對失敗，需調整模型名或新增 entry
- 本地模型（Ollama 等）→ 本來就沒有估算器，走 fallback 屬正常。宣告 `limits: {policy: concurrency_only}` 後 TPM 不受管制，fallback 值不影響行為

#### 步驟 4：確認尺寸是否解析成功

```python
from agent_factory.rate_limiter.image_tokens import read_image_size

read_image_size(data_url)   # (4000, 3000) 或 None
```

回傳 `None` 的可能原因：

| 原因 | 說明 |
|------|------|
| 遠端 `http(s)` URL | 刻意不發網路請求，一律回傳 `None`。改以 data URL 傳入 |
| 格式不支援 | 僅支援 JPEG／PNG／WebP／GIF |
| header 被截斷 | data URL 不完整 |
| JPEG 的 SOF 超出讀取上限 | 先讀 512 bytes，找不到才擴讀至 128 KB。EXIF 極大的檔案可能仍不足 |

#### 步驟 5：確認 `max_tokens` 設定

`reserved` 含 `output_buffer_mult(1.2) × max_tokens`。若 agent YAML 設了 `max_tokens: 4096`，這一項就佔 4,915 token——與影像估算無關。把 `max_tokens` 調整為實際需要的輸出長度即可。

### 驗證方式

確認公式本身是否正確，可對照供應商實際計費：

```bash
uv run python scripts/validate_image_estimation.py
```

該腳本對每個模型送出結構對齊的基準呼叫與各尺寸影像呼叫，兩者相減即為供應商實際計算的影像 token，與本框架的估算值逐筆比對。**倍率應為 1.00x 上下，且不得低於 1.00x**（低估會撞 429）。

需要 `OPENAI_API_KEY`；預設 10 次 mini 級模型呼叫，花費在 0.05 美元以內。
