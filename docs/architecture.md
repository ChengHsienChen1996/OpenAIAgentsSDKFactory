# 架構說明 — OpenAIAgentsSDKFactory

> 通用的目錄結構、編碼風格、測試、版控規範分別以 [project-structure.md](project-structure.md)、[coding-style.md](coding-style.md)、[testing-strategy.md](testing-strategy.md)、[git-workflow.md](git-workflow.md) 為準。本文只寫本專案獨有的決策、通用規則在本專案的落實方式，以及與通用規則的差異。

---

## 架構約束（最高優先，違反須先討論）

### 原則 1：公開介面是跨專案契約

**做什麼**：`LimitAgentRunner.run()` 的簽名、`limits_guard_multi` 的參數、`AgentFactory.get_agent_by_name()`、`LimitRegistry` 的 `bucket()`／`rpm()`／`rpd()`，這四組是使用端直接依賴的公開介面。變更前必須停下來與使用者討論。

**為什麼**：本模組定位為可重用的通用模組，會被任意數量的下游專案引用，且引用方式（submodule、pip 安裝、直接複製）與更新時機都不由本專案控制。介面變更會在使用端以執行期錯誤的形式爆開，而且不會立刻被發現 —— 本專案這邊看不到誰在用、也無法一起改。

### 原則 2：速率限制的策略差異由型別吸收，不由分支吸收

**做什麼**：不同模型的限制策略（強制管制／僅併發／完全不限）差異，一律透過「回傳不同實作但介面相同的限制器物件」來表達；`wrappers.py` 內**不得**出現 `if policy == ...` 之類的分支。

**為什麼**：`limits_guard_multi` 是全模組最複雜、最難測試的一段（等待順序、心跳、預扣校正交織）。每加一條策略分支就讓它的狀態組合翻倍。把差異推到 registry 回傳的物件上，核心流程永遠只有一條路徑。

### 原則 3：估算寧可高估，不可低估

**做什麼**：任何 token 估算的 fallback（尺寸解析失敗、未知供應商、未知 content type）一律取保守的高值。

**為什麼**：高估的代價是請求稍慢（多預扣、事後退款）；低估的代價是撞供應商 429，而本模組明列不做自動重試（見 project-overview 非目標），錯誤會直接往上拋給使用端。兩者不對等。

### 原則 4：新增供應商支援只准加，不准改

**做什麼**：新增一個供應商的影像 token 換算規則，應該只需要在估算器註冊表新增一個 entry；新增一種限制策略，應該只需要新增一個限制器實作。**不得**為了支援新供應商而修改 `wrappers.py` 或 `token_counter.py` 的走訪邏輯。

**為什麼**：供應商的換算規則會隨模型改版變動，是本模組預期最高頻的變更點。把它隔離在註冊表後面，變更半徑就固定在一個檔案內。

---

## 目錄結構

通用結構以 [project-structure.md](project-structure.md) 為準。本專案獨有的部分：

```
src/agent_factory/
├── core.py                  # AgentFactory：從 YAML 建立並管理 Agent
├── limit_runner.py          # LimitAgentRunner：帶速率限制的 Runner 包裝
├── config_loader.py         # AgentConfigLoader：YAML 載入與 Pydantic 驗證
├── agent_builder.py         # AgentBuilder：從 AgentConfig 建立 Agent 實例
├── config_schema.py         # Pydantic schema
├── agent_utils/             # 動態載入、prompt 工廠、檔案載入
└── rate_limiter/
    ├── token_bucket.py      # AsyncTokenBucket / LimitRegistry / Umbrella
    ├── token_counter.py     # token 計數與輸入估算入口
    ├── image_tokens.py      # （Phase 2 新增）影像 token 估算註冊表
    ├── wrappers.py          # limits_guard_multi 裝飾器（核心速率管制邏輯）
    └── limits_parameters.py # MODEL_LIMITS 與全域 limiter 初始化
```

> 文檔清單不在此重列，見 [CLAUDE.md](../CLAUDE.md) 的索引表。

### 與通用規則的差異

| 項目 | 通用規則 | 本專案現況 | 處理 |
|------|----------|-----------|------|
| 測試目錄 | `tests/`，結構鏡射 `src/` | `test/`（單數），未鏡射 | Phase 1 改正，以通用規則為準 |
| 套件目錄 | `src/<package_name>/` | `src/agent_factory/` | 已符合，無差異 |
| 任務計畫文件 | 一律放 `.agent/` 下 | — | 本文件組的 phase 文件放 `.agent/plans/`，不放 `docs/` |

---

## 核心機制規格

### 速率管制的執行順序

`limits_guard_multi` 對每次呼叫的處理是固定的線性流程，順序不可調換：

1. 估算輸入 token（不阻塞）
2. 建立 system prompt（動態 prompt 會在此觸發 PayloadBuilder，可能耗時，故有心跳）
3. 計算預扣量 `reserved = 輸入 + system + safety_pad + output_buffer × max_tokens + per_round_pad`
4. 依序取得配額：umbrella TPM → 模型 TPM → 模型 RPM → 模型 RPD（若有）→ 全域併發 semaphore
5. 發出請求
6. 依 `raw_responses[*].usage` 校正：實際 > 預扣則補扣，實際 < 預扣則退款

**為什麼併發名額最後取**：拿到就要立刻送出，避免佔著並發額度卻還在等 TPM。

**任何階段拋出例外**，都必須退回已預扣的 TPM（模型與 umbrella 兩層）。

### 輸入 token 估算的契約（Phase 2 建立）

`estimate_input_tokens(input_, model_name)` 回傳分項結構而非單一數字：

```python
@dataclass
class InputTokenEstimate:
    text_tokens: int
    image_tokens: int
    other_tokens: int
    total: int
    image_count: int
    has_unknown_items: bool
```

**為什麼分項**：影像估算出錯時，單一總數看不出問題出在文字還是影像。分項值會進 trace，是唯一的線上除錯依據。

### 限制策略（Phase 3 建立）

| 策略 | TPM | RPM/RPD | 全域併發 | 適用 |
|------|-----|---------|---------|------|
| `enforced` | 實際管制 | 實際管制 | 是 | 雲端供應商 |
| `concurrency_only` | 不管制 | 不管制 | 是 | 本地／自架模型 |
| `unlimited` | 不管制 | 不管制 | 否 | 特殊情境 |

**本地模型為何是 `concurrency_only`**：本地推理無帳單，TPM/RPM 不對應任何真實約束；真正的瓶頸是 GPU 序列化執行，唯一有意義的管制是併發數。

---

## 設定參數化原則

1. **供應商配額、策略、模型參數一律從設定讀，不硬編在程式邏輯中**。優先序：agent YAML 的 `model_params.limits` > `MODEL_LIMITS` > `DEFAULT_POLICY`。
2. **啟動時驗證**：所有 agent 設定經 Pydantic 驗證，錯誤訊息必須包含 agent name 與欄位路徑（現行 `AgentConfigLoader.load_validated` 已如此，維持）。
3. **魔數必須有名字**：估算相關的常數（保守預設 token 數、每則 message overhead、未知 item 成本）一律定義為模組層 UPPER_SNAKE_CASE 常數，不得內嵌在運算式中。
4. **供應商換算規則必須標註來源**：每個影像 token 估算器的 docstring 必須寫明官方文件連結與查閱日期。這些規則會隨模型改版失效，沒有來源就無法判斷是否過期。

---

## 測試要點

> 測試規範以 [testing-strategy.md](testing-strategy.md) 為準。本專案補充：

1. **本地 Ollama 視同付費 API 處理**：依 llm-integration.md，本地模型的推理呼叫一律 mock，不在自動測試中真正載入模型。
2. **速率限制測試必須設 timeout**：`AsyncTokenBucket` 的失敗模式是「無限等待」而非拋錯。任何測試取得配額的案例都要用 `pytest.mark.timeout` 或 `asyncio.wait_for` 包住，否則測試失敗會表現為 CI 掛住。
3. **估算器測試必須標註公式來源**：驗證影像 token 換算的測試，斷言的期望值要在註解中寫明出自哪份供應商文件。期望值與實作若一起抄錯，測試會通過但功能是錯的 —— 註解是唯一的人工複查依據。
4. **退款正確性用回歸測試守住**：預扣／退款的錯誤不會立即可見（表現為配額緩慢流失），必須有「連續 N 次呼叫後桶內餘額不單調下降」這類測試。

---

## Codex 複查機制（通用規則的專案延伸）

本專案的 task 文件在通用格式外多一個 **Codex 複查重點** 欄位。這不是通用規範的一部分，而是本專案工作流的延伸：Claude Code 完成 task 後由 Codex 做第二輪複查，該欄位明確指出複查者應該重點檢查什麼。撰寫 task 時必須填寫此欄位，內容應指向「最容易改壞、且不易被測試捕捉」的地方，而非重複驗收標準。
