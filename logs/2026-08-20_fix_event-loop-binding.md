# 改動總結 — 全域併發 semaphore 的 event loop 綁定修正

日期：2026-08-20
分支：dev_ai
相關 commit：

| commit | 訊息 |
|--------|------|
| `a397694` | docs: 記錄 event loop 綁定問題的實測結果與修復選項 |
| `e5fc6b4` | fix: 全域併發 semaphore 改為依 event loop 建立 |

> 本次為 Phase 3 日誌列為「已知問題、未處理」項目的獨立修正，不屬於任何 phase。
> 問題分析、實測數據與方案取捨見 [`.agent/notes/event-loop-binding.md`](../.agent/notes/event-loop-binding.md)。

---

## 變更清單

1. `.agent/notes/event-loop-binding.md`：新增。問題描述、三類物件的實測結果、影響範圍、
   三種修法的取捨，以及事後補上的〈修復記錄〉。
2. `src/agent_factory/rate_limiter/limits_parameters.py`：修改。
   - 新增 `get_global_sem()`：依當前 event loop 建立／取得 semaphore。
   - 新增 `_drop_closed_loops()`：清理已關閉 loop 的對照表項目。
   - `global_sem` 與 `global_rpm_limiter` 改由 module `__getattr__`（PEP 562）提供，
     取用時發出 `DeprecationWarning`；實際物件更名為 `_legacy_global_sem` 與
     `_legacy_global_rpm_limiter`。
   - `get_global_rpm_limiter()` 一併加入 `_drop_closed_loops()` 呼叫。
3. `src/agent_factory/rate_limiter/wrappers.py`：修改。`limits_guard_multi` 的併發階段與
   已棄用的 `with_global_limits` 都改用 `get_global_sem()` / `get_global_rpm_limiter()`，
   不再於模組層引用舊符號。
4. `tests/rate_limiter/test_event_loop_binding.py`：新增，10 個測試。

---

## 測試結果

- pytest 執行結果：**217 passed / 0 failed / 2 deselected**
- 新增測試皆不需 API key 與網路。

### 需人工執行的項目

無。本次改動不涉及任何外部服務。

---

## 備註

### 修正的問題

`limits_parameters.global_sem` 為模組層 `asyncio.Semaphore` 單例。asyncio 同步原語會在
**首次發生競爭時**綁定當時的 event loop，之後於其他 loop 再次發生競爭即拋出：

```
RuntimeError: <asyncio.locks.Semaphore object ... [locked]> is bound to a different event loop
```

**觸發條件需兩者同時成立**：loop A 曾發生競爭（同時請求數超過 `GLOBAL_CONCURRENCY`，預設 6），
且 loop B 再次發生競爭。實測對照：

| 情境 | 結果 |
|------|------|
| 兩個 loop，併發皆 ≤ 6 | 正常 |
| loop A 併發 8，loop B 僅 1 個請求 | 正常 |
| loop A 併發 8，loop B 併發 8 | **RuntimeError** |

這是間歇性、負載相關的故障：低負載完全正常，且錯誤訊息無法指向本模組，除錯成本高。
最可能先踩到的是下游專案的測試套件（pytest-asyncio 預設每個測試一個新 loop）。

### 修正範圍比 Phase 3 日誌所述更小

Phase 3 日誌記為「需重構 `LimitRegistry` 的生命週期」。逐一實測三類物件後，
真正需要處理的只有 semaphore：

| 物件 | 跨 loop 行為 | 處置 |
|------|-------------|------|
| `global_sem`（`asyncio.Semaphore`） | 拋 `RuntimeError` | **改為依 loop 建立** |
| `registry.model_rpms` / `model_rpds`（`AsyncLimiter`） | 警告後自行重綁，節流仍正確生效 | **維持共用** |
| `AsyncTokenBucket._lock`（`asyncio.Lock`） | 從未綁定（持鎖期間無 `await`，不可能競爭） | 不需處理 |

`registry` 的限制器**刻意維持共用**：那些是供應商配額，依 loop 分割會讓每個 loop 各拿一份，
實際請求量變成 N 倍而撞 429——對配額型限制器而言，依 loop 分割比警告噪音更糟。
併發上限則是本機資源保護而非供應商配額，且真正的配額仍由共用的 token bucket 與
RPM 限制器把關，因此依 loop 分割可接受。

`AsyncTokenBucket` 同樣維持共用，且**不應**依 loop 重建——重建會使 TPM 餘額歸零，
等同靜默清空配額。

### 過程中發現並修正的次生問題：WeakKeyDictionary 洩漏

撰寫「已關閉 loop 不得累積」的迴歸測試時當場失敗，追查後發現單靠
`WeakKeyDictionary` 並不足夠：

`asyncio.Semaphore` 發生競爭後會把 `_loop` 指回該 loop，`AsyncLimiter` 首次使用後也會
保存 `_event_loop`——**value 強參照了 key**，弱參照因而永遠不會被觸發。

實測（修正前）：

| 情境 | `sem._loop` | 連續 5 次 `asyncio.run` 後 gc×3 的殘留項目 |
|------|------------|----------------------------------|
| 無競爭 | `None` | 0 |
| **有競爭** | 指回該 loop | **5（全部殘留）** |

洩漏正好發生在本機制要處理的情境上。**Phase 3 引入的 `_rpm_limiter_by_loop` 有相同缺陷**，
當時未發現，本次一併修正。

修法為 `_drop_closed_loops()`：每次取用時清掉已關閉 loop 的項目。修正後連續 20 次
帶競爭的 `asyncio.run()`，對照表項目數恆為 1。

> 教訓：`WeakKeyDictionary` 只在「value 不會強參照 key」時才成立。
> 以 asyncio 同步原語為 value 時此前提不成立，必須另有主動清理機制。

### 行為變更

1. **多 loop 情境下的併發上限語意**：由「全程 `GLOBAL_CONCURRENCY` 個」變為
   「每個 loop 各 `GLOBAL_CONCURRENCY` 個」。單一 loop（絕大多數正式環境）行為完全不變；
   多 loop 情境在修正前是直接拋 `RuntimeError`，本就沒有可保障的語意，故不算削弱既有保證。

2. **`global_sem` 與 `global_rpm_limiter` 已棄用**：兩者為模組公開符號，下游可能直接引用，
   直接移除屬破壞性變更（architecture.md 原則 1）。改以 module `__getattr__` 攔截，
   仍可取用但發出 `DeprecationWarning`，計畫於下一個主要版本移除。
   本 repo 內部已全數改用新函式，以 `-W error::DeprecationWarning` 驗證匯入時不會觸發警告。

### 測試涵蓋

| 測試 | 驗證內容 |
|------|---------|
| `test_contended_semaphore_survives_loop_change` | 核心迴歸：兩個 loop 各跑併發 `GLOBAL_CONCURRENCY + 2` |
| `test_semaphore_still_caps_concurrency` | 修正後併發上限仍生效（同時進行數不超過上限） |
| `test_semaphore_is_per_loop_instance` / `test_same_loop_reuses_one_semaphore` | 實例的取得語意 |
| `test_closed_loops_are_not_retained`（及 RPM 版本） | 洩漏已修 |
| `test_rpm_limiter_survives_loop_change` | 不再出現 `re-used across loops` 警告 |
| `test_deprecated_globals_still_accessible_with_warning` | 舊符號相容性與 `DeprecationWarning` |
| `test_unknown_attribute_still_raises_attribute_error` | module `__getattr__` 未吞掉一般的 `AttributeError` |

### 文件

本次未更動 `README.md` 與 `docs/`：`global_sem` 與 `global_rpm_limiter` 未出現於
README 的 API 參考，併發上限的對外描述（`GLOBAL_CONCURRENCY` 環境變數）也未改變。
