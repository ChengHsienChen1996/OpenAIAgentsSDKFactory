# 專案概觀 — OpenAIAgentsSDKFactory

## 目的

以 `openai-agents` SDK 為底，提供一個**可重用的通用 Agent 模組**：agent 以 YAML 宣告而非程式碼硬編，並內建多維度速率限制，讓上層專案不必各自重寫「建立 agent」與「避免撞供應商配額」這兩件事。適用於任何支援 OpenAI Compatible API 的供應商（OpenAI、Azure、Google Gemini、自架模型）。

本模組為獨立 repo，設計上不綁定任何特定使用端；任何需要「以設定檔宣告 agent」與「帶速率限制地執行」的專案都可引用。

## 目標

- YAML 驅動的 AgentFactory：以設定檔宣告 agent，支援靜態與動態 prompt 兩種模式
- 多維度速率限制：TPM / RPM / RPD 三層，搭配預扣、退款、心跳追蹤
- Pydantic 設定驗證：啟動時驗證所有 agent 設定，錯誤訊息包含欄位路徑
- 可插拔供應商：任何 OpenAI-compatible endpoint 皆可使用，不鎖定單一雲端
- 多模態輸入：以 `input_image` message 傳入影像

## 非目標

> 明列於此是為了防止開發過程自行擴大範圍。要新增以下能力，一律先與使用者討論。

- **不做多 agent 編排**：本模組只負責「建立單一 agent」與「帶速率限制地執行它」。handoff、workflow、graph 等編排邏輯屬下游專案的責任。
- **不做 streaming 支援**：`LimitAgentRunner.run()` 只包裝 `Runner.run()`。串流的 token 計費與預扣校正模型完全不同，目前不納入。
- **不做 prompt 內容管理**：prompt 檔案的版本、A/B、模板繼承等屬使用端職責，本模組只負責載入與格式化。
- **不做供應商 SDK 封裝**：不為各家供應商寫專屬 client，一律走 OpenAI-compatible endpoint。
- **不做持久化**：速率限制狀態全部在記憶體，程序重啟即歸零。跨程序／分散式的配額共享不在範圍內。
- **不自動重試**：遇到 429 或其他錯誤時退回預扣並向上拋出，重試策略由呼叫端決定。

## 技術棧

| 組件 | 選型 | 理由 |
|------|------|------|
| 執行環境 | Python `>=3.12` | 依 `pyproject.toml` 的 `requires-python` |
| 套件管理 | `uv` | 專案統一使用，指令見下方工具鏈 |
| Agent SDK | `openai-agents >= 0.3.3` | 本模組的封裝對象 |
| 設定格式 | `omegaconf >= 2.3.0` | 支援 YAML anchor、`${oc.env:}` 環境變數插值、`${__dir__}` 路徑解析 |
| 設定驗證 | Pydantic v2 | 啟動時驗證，錯誤訊息帶欄位路徑 |
| 速率限制 | `aiolimiter >= 1.2.1` + 自製 `AsyncTokenBucket` | RPM/RPD 用現成 limiter；TPM 需要預扣／退款語意，現成套件不支援故自製 |
| Token 計數 | `tiktoken >= 0.12.0` | 文字 token 估算 |
| 環境變數 | `python-dotenv >= 1.1.1` | `.env` 載入 |
| 測試 | `pytest` + `pytest-asyncio` | 依 testing-strategy.md；**待確認：目前 `pyproject.toml` 尚未宣告這兩個依賴，Phase 1 需補上** |

## 資源預算

本模組本身無 GPU／記憶體需求（純 I/O 封裝層），資源約束來自兩端：

| 項目 | 說明 |
|------|------|
| 雲端 API 配額 | 由 `MODEL_LIMITS` 宣告，目前收錄 OpenAI Tier 1（2025/09）與 Gemini Free Tier（2026/04）數值。**待確認：這些配額值可能已過時，開工時應核對供應商當前文件** |
| 全域併發 | `GLOBAL_CONCURRENCY`，預設 6 |
| 本地模型 | 以 Docker 版 Ollama 執行 vision 模型。VRAM 需求視模型而定，本模組不管控；真正有意義的約束是併發數（GPU 序列化執行） |

## 環境與工具鏈

### 安裝

```bash
uv sync
cp example_file/.env.example .env
```

### 環境變數

| 變數 | 必填 | 說明 |
|------|------|------|
| `OPENAI_API_KEY` | 是 | API 金鑰（或相容供應商的 key） |
| `YAML_SETTINGS_FILE` | 是 | agents 設定 YAML 的路徑 |
| `GLOBAL_CONCURRENCY` | 否 | 最大並發請求數，預設 `6` |
| `RPM` | 否 | 全域每分鐘請求上限，預設 `200` |
| `TPM` | 否 | 全域每分鐘 token 上限，預設 `30000`。**待確認：此變數讀入 `GLOBAL_TPM` 後目前無任何使用點（`umbrella` 預設為 `NoopUmbrella`），Phase 3 需確認保留或移除** |

> `.env` 不納入版本控制。`example_file/.env.example` 目前只有前兩項，Phase 1 應補齊。

### 本地模型（多模態測試用）

```bash
docker run -d -p 11434:11434 --name ollama ollama/ollama
```

> **待確認**：README 中的模型 tag `glm-ocr-optimized:latest` 為自訂本地 tag，非 Ollama 官方 registry 名稱；`OLLAMA_BASE_URL` 預設 `http://localhost:11434/v1`。開工前請向使用者確認實際可用的模型名稱。

### 已知的 repo 缺件

> **待確認**：README 與 `test/test_multimodal.py` 引用了以下路徑，但目前不在版控中 —— 是刻意 gitignore 還是遺漏，開工前需確認：
> - `test/multimodal_agents_setup.yaml`
> - `test/prompt_files/ocr_instruction.md`
> - `imgs/`（測試影像資料夾）

### 常用指令

```bash
uv sync                                    # 安裝依賴
uv run pytest                              # 執行測試（Phase 1 建立 tests/ 後可用）
uv run python test/test_multimodal.py      # 多模態手動驗證（需本地 Ollama）
```
