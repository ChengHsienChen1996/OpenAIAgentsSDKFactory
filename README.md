# OpenAIAgentsSDKFactory

透過 `openai-agents` SDK 建立通用 Agent 模組，適用於支援 OpenAI Compatible API 的模型供應商（OpenAI、Azure、Google Gemini、自架模型等）。

## 核心功能

- **YAML 驅動的 AgentFactory**：以設定檔宣告 agent，支援靜態與動態 prompt 兩種模式
- **多維度速率限制**：TPM / RPM / RPD 三層限制，搭配預扣、退款、心跳追蹤
- **Pydantic 設定驗證**：啟動時驗證所有 agent 設定，錯誤訊息包含欄位路徑
- **可插拔供應商**：任何 OpenAI-compatible endpoint 皆可使用，不鎖定單一雲端

---

## 安裝與環境設定

### 安裝依賴

```bash
uv sync
```

### 設定 `.env`

複製範例並填入值：

```bash
cp example_file/.env.example .env
```

| 變數 | 必填 | 說明 |
|------|------|------|
| `OPENAI_API_KEY` | 是 | API 金鑰（或相容供應商的 key） |
| `YAML_SETTINGS_FILE` | 是 | agents 設定 YAML 的路徑 |
| `GLOBAL_CONCURRENCY` | 否 | 最大並發請求數，預設 `6` |
| `RPM` | 否 | 全域每分鐘請求上限，預設 `200` |
| `TPM` | 否 | 全域每分鐘 token 上限，預設 `30000` |

> 請勿將 `.env` 加入版本控制。

---

## 快速開始

### 1. 建立 prompt 檔案

```markdown
<!-- prompts/my_agent.md -->
你是一個專業的摘要助理，請將使用者提供的文字精簡為三句話以內。
```

### 2. 建立 YAML 設定

```yaml
# agents.yaml
openai: &openai
  client:
    api_key: ${oc.env:OPENAI_API_KEY, ''}
    base_url: https://api.openai.com/v1
  params:
    temperature: 0.5

models:
  gpt-41: &model_gpt41
    <<: *openai
    model: gpt-4.1

agents:
  default:
    - SummaryAgent:
        name: SummaryAgent
        model_instruction:
          dynamic_prompt: false
          instruction_file_path: ${__dir__}/prompts/my_agent.md
        model_params:
          <<: *model_gpt41
          params:
            temperature: 0.3
```

### 3. 執行

```python
import asyncio
from agent_factory.core import create_agent_factory
from agent_factory.limit_runner import LimitAgentRunner


async def main():
    factory = create_agent_factory()          # 讀取 YAML_SETTINGS_FILE 環境變數
    agent = factory.get_agent_by_name("SummaryAgent")

    runner = LimitAgentRunner(agent=agent)
    result = await runner.run(input_="請幫我摘要：人工智慧正在改變各行各業……")
    print(result.final_output)

asyncio.run(main())
```

---

## YAML 設定說明

### 靜態 prompt

直接讀取 Markdown 檔案作為 system prompt，適合內容固定的 agent：

```yaml
model_instruction:
  dynamic_prompt: false
  instruction_file_path: ${__dir__}/prompts/my_agent.md
```

`${__dir__}` 會自動解析為 YAML 檔所在目錄。

### 動態 prompt

每次呼叫時動態產生 system prompt，適合需要依 context 客製化指令的 agent：

```yaml
model_instruction:
  dynamic_prompt: true
  instruction_file_path: ${__dir__}/prompts/template.md   # Jinja2 模板
  dynamic_module_path: mypackage.prompts.build_payload     # PayloadBuilder 函式
  model_context_path: mypackage.schemas.MyContext          # context 型別（Pydantic / dataclass）
```

`PayloadBuilder` 函式簽名：

```python
async def build_payload(context: MyContext, agent) -> dict:
    return {"user_name": context.user_name, ...}
```

### Structured output

指定 Pydantic `BaseModel` 的 dotted path，agent 回傳值會自動解析為該型別：

```yaml
output_schema: mypackage.schemas.SummaryOutput
```

```python
from pydantic import BaseModel

class SummaryOutput(BaseModel):
    summary: str
    key_points: list[str]
```

---

## 目錄結構

```
src/agent_factory/
├── core.py                          # AgentFactory：從 YAML 建立並管理 Agent
├── limit_runner.py                  # LimitAgentRunner：帶速率限制的 Runner 包裝
├── config_loader.py                 # AgentConfigLoader：YAML 載入與 Pydantic 驗證
├── agent_builder.py                 # AgentBuilder：從 AgentConfig 建立 Agent 實例
├── config_schema.py                 # Pydantic schema（InstructionConfig、ModelParamsConfig、AgentConfig）
├── agent_utils/
│   ├── module_loader.py             # import_by_path：依 dotted path 動態載入模組
│   ├── dynamic_prompt_creator.py    # make_dynamic_prompt：動態 instruction 工廠
│   └── file_loader.py              # load_yaml / load_and_format_with_escape
└── rate_limiter/
    ├── token_bucket.py              # AsyncTokenBucket / LimitRegistry / NoopUmbrella / AdaptiveUmbrella
    ├── token_counter.py             # tiktoken token 計數
    ├── wrappers.py                  # limits_guard_multi 裝飾器（核心速率管制邏輯）
    └── limits_parameters.py         # MODEL_LIMITS 配額常數、全域 limiter 初始化
```

---

## 速率限制說明

### 三種維度

| 維度 | 說明 | 實作 |
|------|------|------|
| **TPM** | 每分鐘 token 上限，以預扣 + 退款機制管控 | `AsyncTokenBucket` |
| **RPM** | 每分鐘請求次數上限 | `aiolimiter.AsyncLimiter` |
| **RPD** | 每日請求次數上限（選填，用於 Free Tier 供應商） | `aiolimiter.AsyncLimiter` |

每次呼叫的等待順序：`Umbrella TPM → 模型 TPM → 模型 RPM → 模型 RPD（若存在）→ 全域 Semaphore`

呼叫完成後，框架會依 `raw_responses` 的實際 token 用量自動校正：低估補扣、高估退款。

### 新增或調整模型配額

編輯 `src/agent_factory/rate_limiter/limits_parameters.py` 的 `MODEL_LIMITS`：

```python
MODEL_LIMITS = {
    # OpenAI Tier 1（2025/09）
    "gpt-4.1": {"TPM": 30000, "RPM": 500, "TPD": 90000},

    # Gemini Free Tier（2026/04）
    "gemma-4-31b-it": {"TPM": 30000, "RPM": 15, "RPD": 1500},

    # 自訂供應商（RPD 選填，不設定則無每日限制）
    "my-custom-model": {"TPM": 100000, "RPM": 60},
}
```

> `TPD` 欄位為記錄用途，速率管制實際使用 `TPM`。`RPD` 存在時才建立每日限制器。

### 全域 Umbrella

預設使用 `NoopUmbrella`（不做跨模型共享限制）。若需統一管控全域 TPM，可在 `limits_parameters.py` 切換：

```python
# umbrella = NoopUmbrella()
umbrella = AdaptiveUmbrella(init_tpm=sum(v["TPM"] for v in MODEL_LIMITS.values()))
```

`AdaptiveUmbrella` 遇到速率錯誤時會自動縮減全域 TPM 配額（乘以 `dec_mult=0.75`），並每分鐘逐步恢復（`inc_per_min=1000`）。

---

## API 參考

### `AgentFactory` / `create_agent_factory`

```python
from agent_factory.core import AgentFactory, create_agent_factory
```

| 方法 | 說明 |
|------|------|
| `create_agent_factory() -> AgentFactory` | 從 `YAML_SETTINGS_FILE` 環境變數建立工廠 |
| `AgentFactory.create_factory_from_yaml(path) -> AgentFactory` | 從指定路徑建立工廠 |
| `factory.get_agent_by_name(name: str) -> Agent` | 依 YAML `name` 欄位取得 Agent，找不到時拋出 `KeyError` |
| `AgentFactory.register(name: str)` | Classmethod decorator，手動將 callable 註冊至 registry |

#### `register()` 範例

```python
from agents import Agent
from agent_factory.core import AgentFactory

@AgentFactory.register("special_agent")
def build_special_agent(settings: dict) -> Agent:
    # 自行建立並回傳 Agent
    ...
```

---

### `LimitAgentRunner`

```python
from agent_factory.limit_runner import LimitAgentRunner
```

| 方法 | 簽名 | 說明 |
|------|------|------|
| `__init__` | `(agent: Agent)` | 接受 `Agent` 實例 |
| `run` | `(input_: str \| list, context=None, **kwargs) -> RunResult` | 帶速率限制地執行 agent |
| `extract_tool_usage` | `(run_result: RunResult) -> ToolTrace` | 從結果中提取 tool call 相關 items |

```python
runner = LimitAgentRunner(agent=agent)
result = await runner.run(input_="你好", context=my_context)
tool_trace = await runner.extract_tool_usage(result)
```

`run()` 內部使用 `limits_guard_multi` 裝飾器，呼叫端無需額外設定速率參數。

---

### `AgentConfigLoader`

```python
from agent_factory.config_loader import AgentConfigLoader
```

| 方法 | 說明 |
|------|------|
| `AgentConfigLoader.load_raw(yaml_path) -> DictConfig` | 載入 YAML 並注入 `__dir__`，回傳 OmegaConf `DictConfig` |
| `AgentConfigLoader.load_validated(yaml_path) -> List[AgentConfig]` | 載入並對每個 agent 執行 Pydantic 驗證，失敗時拋出含 agent name 的 `ValueError` |

---

### `AgentBuilder`

```python
from agent_factory.agent_builder import AgentBuilder
```

| 方法 | 說明 |
|------|------|
| `AgentBuilder.build(agent_config: AgentConfig) -> Agent` | 從驗證後的 `AgentConfig` 建立 Agent 實例，靜態／動態 prompt 在此分支處理 |

---

## 已知限制與故障排除

- **Gemini `AQ.` 前綴 API key**：使用 Google AI Studio 新建的 API key 透過 Gemini OpenAI-compatible endpoint 呼叫時，會出現 `400 Multiple authentication credentials received` 錯誤。需改用從 Google Cloud Console 建立的 `AIza` 開頭格式。詳細步驟與驗證程式碼請參見 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

- **`MODEL_LIMITS` 未涵蓋的模型**：若使用的模型名稱不在 `MODEL_LIMITS` 字典中，速率限制模組在取得 bucket 時會拋出 `KeyError`。請在 `limits_parameters.py` 的 `MODEL_LIMITS` 新增對應設定後再使用。
