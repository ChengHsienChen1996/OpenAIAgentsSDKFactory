# LLM API 整合說明

> **此文檔僅適用於有呼叫 LLM API 服務的專案。** 無 LLM 呼叫的專案可忽略此文件。

## 適用時機

當專案中使用了以下任一類服務時，本文檔的規範即生效：

- OpenAI API（GPT 系列）
- Google Gemini API
- Anthropic Claude API
- Azure OpenAI Service
- 其他 OpenAI-compatible endpoint（自架模型、第三方供應商等）
- 本地模型（Ollama、vLLM 等）——無雲端費用，但仍建議 mock 掉模型載入與推理呼叫

## 架構概覽

LLM 整合專案建議採用以下模組化架構（參考 `openai-agents` SDK 模式）：

```
src/<package_name>/
├── agent_factory/
│   ├── core.py                # AgentFactory：從設定檔建立並管理 Agent
│   ├── config_loader.py       # 設定檔載入與驗證
│   ├── agent_builder.py       # 從設定建立 Agent 實例
│   ├── config_schema.py       # Pydantic schema 定義
│   ├── limit_runner.py        # 帶速率限制的 Runner
│   ├── agent_utils/           # 輔助工具（動態載入、prompt 工廠等）
│   └── rate_limiter/          # 速率限制模組
├── prompts/                   # Prompt 檔案（Markdown / Jinja2 模板）
└── schemas/                   # 輸入輸出的 Pydantic 型別定義
```

> 以上為建議結構，實際可依專案複雜度調整。核心原則是 **將 LLM 呼叫邏輯與業務邏輯分離**。

## 設定管理

### YAML 驅動的 Agent 設定

使用 YAML 檔案宣告 agent，支援：

- **靜態 prompt**：直接讀取 Markdown 檔案作為 system prompt。
- **動態 prompt**：每次呼叫時透過 Jinja2 模板 + PayloadBuilder 函式動態產生。
- **Structured output**：指定 Pydantic BaseModel 自動解析回傳值。

### 環境變數

LLM 相關的敏感資訊（API key、endpoint）一律透過 `.env` 管理：

```env
OPENAI_API_KEY=sk-...
YAML_SETTINGS_FILE=src/<package_name>/agents.yaml
GLOBAL_CONCURRENCY=6
RPM=200
TPM=30000
```

## 速率限制

### 三維度管控

| 維度 | 說明 |
|------|------|
| **TPM** | 每分鐘 token 上限，預扣 + 退款機制 |
| **RPM** | 每分鐘請求次數上限 |
| **RPD** | 每日請求次數上限（選填，適用於免費額度供應商） |

### 模型配額設定

在 `rate_limiter/limits_parameters.py` 的 `MODEL_LIMITS` 中設定各模型的配額：

```python
MODEL_LIMITS = {
    "gpt-4.1": {"TPM": 30000, "RPM": 500, "TPD": 90000},
    "gemma-4-31b-it": {"TPM": 30000, "RPM": 15, "RPD": 1500},
}
```

每個模型可宣告 `policy` 決定管制方式：

| 策略 | 說明 | 適用 |
|------|------|------|
| `enforced` | 實際管制 TPM/RPM/RPD（未寫 `policy` 時的預設） | 雲端供應商 |
| `concurrency_only` | 只受全域併發約束，不需填配額數值 | 本地／自架模型 |
| `unlimited` | 完全不管制 | 特殊情境 |

```python
MODEL_LIMITS = {
    "gpt-4.1": {"TPM": 30000, "RPM": 500, "TPD": 90000},
    "my-local-model": {"policy": "concurrency_only"},
}
```

也可在 agent YAML 的 `model_params.limits` 宣告，優先序為 **agent YAML > `MODEL_LIMITS` > `DEFAULT_POLICY`**。

未登錄於任何一處的模型會套用 `DEFAULT_POLICY`（預設 `concurrency_only`），並發出一次警告，不會中斷執行。

> **版本差異**：上述策略機制需模組具備 `LimitPolicy`。較早的版本沒有此機制，使用未定義於 `MODEL_LIMITS` 的模型會直接拋出 `KeyError`，必須先新增設定。引用舊版的專案請以後者為準。

> **本地模型（如 Ollama）特例**：本地推理無雲端費用，速率限制的意義在於保護本機資源而非避免帳單。TPM／RPM 不對應任何真實約束，真正的瓶頸是 GPU 序列化執行，唯一有意義的管制是併發數，因此建議宣告 `{"policy": "concurrency_only"}`——不必為它編造配額數值。
>
> 舊版無 policy 機制時的作法是把 `MODEL_LIMITS` 的配額設得足夠寬鬆；但要注意單次預扣量若超過所設的 TPM，舊版會無限等待（表現為靜默卡死），因此該數值必須明顯大於任何單次請求的預扣量。

## Prompt 管理

| 類型 | 適用場景 | 檔案位置 |
|------|----------|----------|
| 靜態 prompt | 內容固定的 agent | `prompts/<agent_name>.md` |
| 動態 prompt | 需依 context 客製化的 agent | `prompts/<template>.md` + Python PayloadBuilder |

## 測試注意事項

LLM API（含本地模型）的測試策略依循 [testing-strategy.md](testing-strategy.md)：

1. **雲端付費 API**：撰寫 mock 測試骨架，**不由 AI 執行**，回報人工驗證。
2. **本地模型**：一律 mock 掉模型載入與推理呼叫，不在自動測試中真正載入模型（載入成本高、佔用資源）。
3. 在改動日誌中標注需人工測試的項目。

## 常見問題

- **Gemini API Key 格式問題**：Google AI Studio 產生的 `AQ.` 開頭 key 可能導致 `400 Multiple authentication credentials received`，需改用 Google Cloud Console 產生的 `AIza` 開頭格式。
- **模型未在 MODEL_LIMITS 中定義**：套用 `DEFAULT_POLICY`（預設 `concurrency_only`）並發出一次警告，不會中斷執行。若該模型需要實際管制，請在 `limits_parameters.py` 或 agent YAML 的 `model_params.limits` 補上設定。（舊版行為為拋出 `KeyError`。）
