# OpenAIAgentsSDKFactory

透過 `openai-agents` SDK 建立通用 Agent 模組，適用於支援 OpenAI Compatible API 的模型供應商（OpenAI、Azure、Google Gemini、自架模型等）。

## 核心功能

- **YAML 驅動的 AgentFactory**：以設定檔宣告 agent，支援靜態與動態 prompt 兩種模式
- **多維度速率限制**：TPM / RPM / RPD 三層限制，搭配預扣、退款、心跳追蹤
- **可切換的限制策略**：雲端模型完整管制、本地模型只受併發約束，差異由設定宣告而非改程式
- **Pydantic 設定驗證**：啟動時驗證所有 agent 設定，錯誤訊息包含欄位路徑
- **可插拔供應商**：任何 OpenAI-compatible endpoint 皆可使用，不鎖定單一雲端
- **多模態輸入**：以 `input_image` message 傳入影像，影像 token 依各供應商公式估算，不做文字計數

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

## 多模態（影像）輸入

### input 格式

`LimitAgentRunner.run()` 的 `input_` 除了字串，也接受 `list[TResponseInputItem]`（即 openai-agents SDK 的 message list）。影像以 `input_image` content item 傳入，值為 base64 data URL：

```python
import base64
from pathlib import Path


def image_to_data_url(image_path: Path, mime: str = "image/jpeg") -> str:
    b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


model_input = [
    {
        "role": "user",
        "content": [
            {
                "type": "input_image",
                "detail": "auto",
                "image_url": image_to_data_url(Path("imgs/table.jpg")),
            }
        ],
    },
    {
        "role": "user",
        "content": "Table Recognition:",
    },
]

result = await runner.run(input_=model_input)
print(result.final_output)
```

影像與文字可拆成兩則 message（如上），也可合併在同一則 message 的 `content` 陣列中。`detail` 可用 `"auto"` / `"low"` / `"high"`，自架模型通常會忽略此欄位。

### YAML 設定

多模態 agent 的設定與一般 agent 完全相同——只要 `model` 指向具備 vision 能力的模型即可：

```yaml
# 本地 Ollama（Docker）：docker run -d -p 11434:11434 --name ollama ollama/ollama
ollama: &ollama
  client:
    api_key: ${oc.env:OLLAMA_API_KEY, 'ollama'}   # Ollama 不驗證，但 AsyncOpenAI 要求非空字串
    base_url: ${oc.env:OLLAMA_BASE_URL, 'http://localhost:11434/v1'}
  params:
    temperature: 0.0

agents:
  multimodal:
    - ocr_agent:
        name: OllamaOCRAgent
        model_instruction:
          dynamic_prompt: false
          instruction_file_path: ${__dir__}/prompt_files/ocr_instruction.md
        model_params:
          <<: *ollama
          model: glm-ocr-optimized:latest
          params:
            temperature: 0.0
            max_tokens: 4096
          limits:
            policy: concurrency_only   # 本地模型：不必填任何 TPM／RPM 數值
```

本地／自架模型建議宣告 `limits: {policy: concurrency_only}`（如上）——本地推理無帳單，TPM／RPM 不對應任何真實約束，唯一有意義的管制是併發數。未宣告時會沿用 `MODEL_LIMITS`，兩處都沒有則依 `DEFAULT_POLICY` 執行並發出一次警告。詳見〈[速率限制說明](#速率限制說明)〉。

### 參考實作

`tests/test_multimodal.py` 是完整範例，以 Docker 版 Ollama 的 `glm-ocr-optimized:latest` 辨識 `./imgs` 內的圖片。

實際呼叫 Ollama 的案例標記為 `integration`，`uv run pytest` 預設不執行；要跑需明確指定：

```bash
uv run pytest -m integration tests/test_multimodal.py -s
```

也可直接執行該檔，用 CLI 參數逐張看辨識結果：

```bash
uv run python tests/test_multimodal.py                    # 測試 ./imgs 內所有圖片
uv run python tests/test_multimodal.py --limit 1          # 只測第一張
uv run python tests/test_multimodal.py --image imgs/table.jpg --prompt "OCR:"
uv run python tests/test_multimodal.py --max-side 1600    # 先等比縮圖（需另行安裝 Pillow）
```

搭配的設定檔為 `tests/multimodal_agents_setup.yaml` 與 `tests/prompt_files/ocr_instruction.md`。

> **執行前需自備影像**：`imgs/` 是本機測試資料夾，內容因人而異且體積大，**不納入版控**（已列於 `.gitignore`）。請自行建立 `imgs/` 並放入待辨識的圖片，否則 integration 測試會被跳過。

### 影像輸入的 token 估算

輸入 token 由 `estimate_input_tokens()` 走訪 `input_` 結構後分項估算，**base64 影像字串絕不參與文字計數**——影像改由 `image_tokens.py` 依供應商公式從寬高換算。

估算結果為分項結構，會完整寫進 trace 的 `estimate_ready` 事件：

| 分項 | 內容 |
|------|------|
| `text_tok` | `input_text` / `output_text` / `refusal` 與字串型 content |
| `image_tok` | `input_image`，依模型套用對應的換算公式 |
| `other_tok` | 每則 message 的固定 overhead，以及無法識別的 item |
| `image_count` | 影像數量 |
| `has_unknown_items` | 是否含無法識別的 item（該項以保守常數計入） |

#### 影像尺寸如何取得

以純 Python 解析影像 header 取得寬高，**不引入 Pillow、也不完整解碼 base64**。支援 JPEG（SOF marker）、PNG（IHDR）、WebP（VP8／VP8L／VP8X）、GIF（logical screen descriptor）。

先解碼開頭 512 bytes；JPEG 的 SOF 位置浮動（相機直出相片的 EXIF 常內嵌縮圖，實測 SOF 落在 60–70 KB 處），找不到時擴讀至 128 KB 上限。**遠端 `http(s)` URL 一律回傳 `None`，不會發出任何網路請求。**

#### 已涵蓋的估算器

| 供應商 | 模型前綴 | 公式 |
|--------|---------|------|
| OpenAI | `gpt-4o`、`gpt-4o-mini`、`gpt-4.1`、`gpt-4.5`、`o1`、`o3` | tile-based |
| OpenAI | `gpt-4.1-mini`、`gpt-4.1-nano`、`gpt-5-mini`、`gpt-5-nano`、`gpt-5.4-mini`、`gpt-5.4-nano`、`o4-mini` | patch-based |
| Google | `gemini` | 768px tile |

比對取**最長符合的前綴**——`gpt-4.1-mini` 走 patch-based，不會被較短的 `gpt-4.1`（tile-based）攔截。

`gpt-4o-mini` 與 `gpt-4.1-mini` 已用真實 API 回報的計費數字驗證，四種尺寸的估算誤差皆在 1.00–1.01x（見 `logs/2026-08-20_test_multimodal-estimation-validation.md`）。其餘模型的公式依官方文件實作，尚未逐一實測。

#### fallback 行為

一律取保守高值（高估只是請求稍慢，低估會撞供應商 429 而本模組不做自動重試）：

| 情況 | 行為 |
|------|------|
| 尺寸無法解析（遠端 URL、格式不支援、header 截斷） | 取該模型家族的 high-detail 上限，log warning |
| 模型無對應估算器 | 取 `FALLBACK_IMAGE_TOKENS`（3,000），log warning |
| 有影像但模型無估算器 | 額外在開始等待配額前先 log warning |

#### 為新供應商新增估算器

只需在 `image_tokens.py` 的 `IMAGE_TOKEN_ESTIMATORS` 新增一個 entry，**不需修改任何走訪邏輯**：

```python
IMAGE_TOKEN_ESTIMATORS = {
    "my-provider-vision": lambda width, height, detail: ...,
}

UNKNOWN_SIZE_TOKENS = {
    "my-provider-vision": 4000,   # 尺寸不可得時的保守值，須一併新增
}
```

兩個字典必須一一對應（有測試守住）。每個估算器的 docstring 必須寫明官方文件連結與查閱日期——這類規則會隨模型改版失效，沒有來源就無法判斷是否過期。

#### 效果

同一張 2.69 MB／4000×3000 相片，改為分項估算前後：

| 模型 | 改動前 | 改動後 |
|------|-------:|-------:|
| `gpt-4o` | 2,439,832 | 776 |
| `gpt-4.1-mini` | 2,439,832 | 2,417 |
| `gemini-2.5-flash` | 2,439,832 | 1,043 |

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

每次呼叫的等待順序：`Umbrella TPM → 模型 TPM → 模型 RPM → 模型 RPD（若存在）→ 全域 RPM → 全域 Semaphore`

呼叫完成後，框架會依 `raw_responses` 的實際 token 用量自動校正：低估補扣、高估退款。供應商完全未回報用量時，整筆預扣會全額退回並記錄一則 error——寧可短暫超額，也不讓桶被靜默抽乾。

### 限制策略

每個模型套用一種策略，差異由限制器的型別吸收：

| 策略 | TPM | RPM／RPD | 全域併發 | 適用 |
|------|-----|---------|---------|------|
| `enforced` | 實際管制 | 實際管制 | 是 | 雲端供應商 |
| `concurrency_only` | 不管制 | 不管制 | 是 | 本地／自架模型 |
| `unlimited` | 不管制 | 不管制 | 否 | 特殊情境 |

**選用建議**：雲端供應商用 `enforced` 並填入該帳號的實際配額；本地／自架模型用 `concurrency_only`——本地推理無帳單，TPM／RPM 不對應任何真實約束，真正的瓶頸是 GPU 序列化執行，唯一有意義的管制是併發數。

`enforced` 必須填 `TPM` 與 `RPM`（缺少時啟動即驗證失敗）；另外兩種不需要任何配額數值。

### 設定來源與優先序

**`agent YAML 的 model_params.limits` > `MODEL_LIMITS` > `DEFAULT_POLICY`**

**1. agent YAML**（優先序最高）：

```yaml
model_params:
  model: glm-ocr-optimized:latest
  limits:
    policy: concurrency_only
```

**2. `MODEL_LIMITS`**（`limits_parameters.py`）：

```python
MODEL_LIMITS = {
    # OpenAI Tier 1（2025/09）；未寫 policy 時視為 enforced
    "gpt-4.1": {"TPM": 30000, "RPM": 500, "TPD": 90000},

    # Gemini Free Tier（2026/04）
    "gemma-4-31b-it": {"TPM": 30000, "RPM": 15, "RPD": 1500},

    # 本地模型：不必填配額數值
    "glm-ocr-optimized:latest": {"policy": "concurrency_only"},
}
```

**3. `DEFAULT_POLICY`**：兩處都沒有登錄的模型套用此策略，預設 `concurrency_only`，可由環境變數 `LIMIT_DEFAULT_POLICY` 覆寫。此時會發出一次警告（同一模型只警告一次），**不會拋出例外**。

> `TPD` 欄位為記錄用途，速率管制實際使用 `TPM`。`RPD` 存在時才建立每日限制器。
>
> 同一模型被兩個 agent 以**不同** limits 宣告時，以先註冊者為準並記錄 warning，不做合併——合併兩組配額會產生沒有人宣告過的隱性行為。設定相同的重複註冊則為冪等操作。

### 全域 Umbrella

預設使用 `NoopUmbrella`（不做跨模型共享限制）。若需統一管控全域 TPM，可在 `limits_parameters.py` 切換：

```python
# umbrella = NoopUmbrella()
umbrella = AdaptiveUmbrella(
    init_tpm=sum(v["TPM"] for v in MODEL_LIMITS.values() if "TPM" in v)
)
```

> `if "TPM" in v` 不可省略——`concurrency_only` 與 `unlimited` 的 entry 沒有 `TPM` 鍵。

`AdaptiveUmbrella` 遇到速率錯誤時會自動縮減全域 TPM 配額（乘以 `dec_mult=0.75`），並每分鐘逐步恢復（`inc_per_min=1000`）。縮減後若單筆預扣量超過當前 capacity，會取用全部可得額度並記錄 warning，而非讓該次請求失敗。

### 單次預扣量超過桶容量

`enforced` 模型的單次預扣量若超過該模型的 `TPM`，`AsyncTokenBucket.acquire()` 會立即拋出 `ValueError`（而非無限等待——後者會表現為程式靜默卡死）。遇到時請檢查 trace 的 `estimate_ready` 分項是否異常，或調高該模型的 `TPM`。

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

- **遠端 URL 影像無法取得尺寸**：`read_image_size()` 只解析 base64 data URL 的 header，遇到 `http(s)` URL 一律回傳 `None`（刻意不發網路請求）。此時影像會以該模型家族的 high-detail 上限計入，屬保守高估。若需精確估算，請改以 data URL 傳入影像。

- **未涵蓋的供應商走 fallback 估算**：模型名不符合任何估算器前綴時，影像以 `FALLBACK_IMAGE_TOKENS`（3,000）計入並 log warning。對雲端供應商建議自行新增估算器（見〈[為新供應商新增估算器](#為新供應商新增估算器)〉），只需在註冊表加一個 entry；本地模型則因為宣告 `concurrency_only` 後不受 TPM 管制，fallback 值不影響實際行為。

- **僅部分模型經過實測驗證**：`gpt-4o-mini`（tile-based）與 `gpt-4.1-mini`（patch-based）已用真實 API 計費數字驗證。其餘模型的公式依官方文件實作，未逐一實測。供應商的換算規則會隨模型改版變動，若發現預扣量明顯偏離，請依 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) 的排查步驟確認。

- **本地模型無法量測估算誤差**：Ollama 的 OpenAI-compatible endpoint 不回報 usage，框架會走全額退款路徑並記錄一則 error。這是已知行為，非錯誤；宣告 `concurrency_only` 後 TPM 管制本就不適用於本地模型。
