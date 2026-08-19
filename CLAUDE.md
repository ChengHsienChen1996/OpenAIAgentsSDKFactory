# CLAUDE.md — OpenAIAgentsSDKFactory

以 `openai-agents` SDK 建立的通用 Agent 模組，支援任何 OpenAI-compatible 供應商。詳細目的、技術棧與資源預算見 [docs/project-overview.md](docs/project-overview.md)。

---

## 開工前必做（最高優先）

1. **依 [docs/git-workflow.md](docs/git-workflow.md) 完成兩項準備**：確認在 `dev_ai` 分支、快照 `.claude/settings.local.json`（後者無版控歷史，誤刪永久遺失）。
2. **本專案是被下游專案引用的通用模組**。任何對 `LimitAgentRunner.run()` 簽名、`limits_guard_multi` 參數、`registry` 公開介面的改動都是破壞性變更 —— 動到這些之前先停下來問。

---

## 專案文檔索引

| 文檔 | 說明 |
|------|------|
| [docs/project-overview.md](docs/project-overview.md) | 專案目的、目標與非目標、技術棧、環境與工具鏈 |
| [docs/architecture.md](docs/architecture.md) | 本專案的架構約束、模組職責、設定參數化與測試補充規則 |
| [.agent/plans/phase-1-foundation.md](.agent/plans/phase-1-foundation.md) | Phase 1：版控隔離機制與測試地基 |
| [.agent/plans/phase-2-token-estimation.md](.agent/plans/phase-2-token-estimation.md) | Phase 2：輸入 token 估算重構（多模態） |
| [.agent/plans/phase-3-limit-policy.md](.agent/plans/phase-3-limit-policy.md) | Phase 3：速率限制策略化 |
| [.agent/plans/phase-4-validation-and-docs.md](.agent/plans/phase-4-validation-and-docs.md) | Phase 4：估算驗證與文件同步 |

---

## 通用規範文檔索引

> 以下為跨專案共用規範，**內容不因本專案而改**，一律以其為準。本專案文件只寫獨有決策與差異。

| 文檔 | 說明 |
|------|------|
| [docs/project-structure.md](docs/project-structure.md) | 目錄結構規範（src layout、Zone 1／Zone 2 版控分區） |
| [docs/coding-style.md](docs/coding-style.md) | 編碼風格（async 優先、命名、型別標註、錯誤處理） |
| [docs/testing-strategy.md](docs/testing-strategy.md) | 測試策略（非付費 API 自動測試 vs 付費 API mock 骨架＋人工執行） |
| [docs/git-workflow.md](docs/git-workflow.md) | 版控流程（dev_ai、不上 main 的檔案隔離、設定備份、commit 格式） |
| [docs/change-log-guide.md](docs/change-log-guide.md) | `/logs` 改動總結規範與格式 |
| [docs/llm-integration.md](docs/llm-integration.md) | LLM API 整合說明（本專案適用） |

---

## 協作準則精要

1. **一次只做一個 phase**：開工前先讀該 phase 文件，列出計畫給使用者確認才動手。
2. **一次只做一個 task**：完成後停下來交付，不連續推進。
3. **重大技術選擇有疑義就停下來問**，不擅自決定；尤其是會改變公開介面或既有行為的選項。
4. **跑不起來先報告**：附錯誤訊息、已嘗試方法、卡點，不無限 debug。
5. **不過度工程**：抽象只加在 phase 文件明確標示的擴充邊界（估算器註冊表、限制策略）。
6. **commit 與改動日誌**依 git-workflow.md 與 change-log-guide.md 格式，不自創。

---

## 首次開工檢查清單

1. `git branch --show-current` 確認在 `dev_ai`；不在則依 git-workflow.md 建立。
2. 快照 `.claude/settings.local.json` 至 `~/.claude/settings-backups/OpenAIAgentsSDKFactory/`。
3. 讀 [docs/project-overview.md](docs/project-overview.md) 與 [docs/architecture.md](docs/architecture.md)。
4. 讀 `README.md` 的〈影像輸入與 TPM 預扣〉一節 —— 那是 Phase 2 要解決的問題現場。
5. 讀 [.agent/plans/phase-1-foundation.md](.agent/plans/phase-1-foundation.md)，列出執行計畫給使用者確認。
6. 確認後才開始第一個 task。
