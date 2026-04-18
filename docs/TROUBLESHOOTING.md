# 故障排除文件

本文件收錄使用本框架時可能遇到的**外部供應商相容性問題**，非框架本身的 bug。每則條目採固定結構：問題描述 → 原因說明 → 解決方式 → 驗證方式。

---

## 目錄

- [Gemini OpenAI-compatible Endpoint 與新版 API Key 相容性問題](#gemini-openai-compatible-endpoint-與新版-api-key-相容性問題)

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
