# 模組層物件與 event loop 綁定問題

日期：2026-08-20
狀態：**已依方案 A 修復**（2026-08-20）。修復內容與過程中發現的次生問題見文末〈修復記錄〉。

---

## 背景

`limits_parameters.py` 在模組載入時建立多個同步原語，整個程序共用：

```python
global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)          # 全域併發
registry = LimitRegistry(MODEL_LIMITS, ...)                 # 內含 AsyncLimiter / AsyncTokenBucket
```

asyncio 的同步原語會在**首次需要等待時**綁定當前 event loop，之後於其他 loop 使用即為未定義行為。
Phase 3 把 `global_rpm_limiter` 接進熱路徑時，測試立刻冒出 16 條
`RuntimeWarning: This AsyncLimiter instance is being re-used across loops`，
該項已改為依 loop 建立（`get_global_rpm_limiter()`），但其餘物件未處理。

---

## 實測結果

逐一實測三類物件，結論與 Phase 3 日誌所記的「需重構 `LimitRegistry` 生命週期」**範圍不同** ——
真正有硬性故障的只有一個。

| 物件 | 跨 loop 行為 | 嚴重度 |
|------|-------------|--------|
| `global_sem`（`asyncio.Semaphore`） | **拋 `RuntimeError`**，請求直接失敗 | **高** |
| `registry.model_rpms` / `model_rpds`（`AsyncLimiter`） | 發出 warning，丟棄舊 loop 的 waiter，自行復原 | 低 |
| `AsyncTokenBucket._lock`（`asyncio.Lock`） | 不受影響 | 無 |

### 1. `global_sem` —— 唯一的硬性故障

```
RuntimeError: <asyncio.locks.Semaphore object ... [locked]> is bound to a different event loop
```

**精確的觸發條件（兩個條件必須同時成立）**：

1. loop A 中曾發生**競爭**——即同時進行的請求數 **超過** `GLOBAL_CONCURRENCY`（預設 6）。
   此時 `Semaphore.acquire()` 才會建立 waiter future 並呼叫 `_get_loop()` 綁定 loop。
2. loop B 中**再次**發生競爭。

實測驗證：

| 情境 | 結果 |
|------|------|
| 兩個 loop，併發數皆 ≤ 6（無競爭） | 正常 |
| loop A 併發 8（有競爭），loop B 僅 1 個請求（無競爭） | 正常 |
| loop A 併發 8，loop B 併發 8（皆有競爭） | **RuntimeError** |

換言之這是一個**間歇性、負載相關**的故障：低負載時完全正常，只有在兩個 loop 都跑到超過併發上限時才爆。
這正是最難除錯的失效模式——測試環境重現不了，正式環境偶發。

補充：`_loop` 一旦綁定就不會重設，即使該 loop 已關閉。實測顯示綁定的是一個
`closed=True` 的 loop 物件。

**未發現狀態洩漏**：loop 中途因例外中止時，semaphore 的可用名額會正確歸還，不會永久減少。

### 2. `registry` 內的 `AsyncLimiter` —— 低風險

aiolimiter 自身有防護（`leakybucket.py` L96-102）：偵測到 loop 變更時會重新綁定，
並**過濾掉屬於舊 loop 的 waiter**，然後發出 warning。

實測：跨 4 個 loop 使用 `AsyncLimiter(2, time_period=60)`，第 3、4 次各正確等待約 30 秒——
**節流仍然生效，時間基準未錯亂**（`loop.time()` 在各 loop 皆為 `time.monotonic()`）。

代價僅為：warning 噪音，以及舊 loop 的 pending waiter 被靜默丟棄
（但那些 coroutine 本來就隨舊 loop 一起消失，實務上無影響）。

### 3. `AsyncTokenBucket._lock` —— 無風險

實測 1,000 次高頻取用後 `_lock._loop` 仍為 `None`——**從未綁定**。

原因：`acquire()` 與 `refund()` 持鎖期間沒有任何 `await`，協程不會被搶佔，
因此鎖永遠不可能發生競爭，`_get_loop()` 也就永不被呼叫。

這也代表 `AsyncTokenBucket` 本身是 loop-agnostic 的，**不需要**依 loop 重建。

---

## 誰會踩到

| 使用方式 | 是否受影響 |
|---------|-----------|
| 單一 `asyncio.run(main())`，全程一個 loop | 否（絕大多數正式環境） |
| 下游專案的測試套件（pytest-asyncio 預設每個測試一個新 loop） | **是**，若測試涵蓋併發 > 6 的情境 |
| 重複呼叫 `asyncio.run()`（如 CLI 工具連續處理多批任務） | **是**，若每批併發 > 6 |
| 在多個 thread 各跑一個 loop | **是**，且此時「全域併發上限」的語意本來就不成立 |

最可能先踩到的是**下游專案的測試套件**——本 repo 自己的測試就是因為這個機制才在 Phase 3 冒出警告。

---

## 修復範圍（比 Phase 3 日誌所述更小）

Phase 3 日誌寫「需重構 `LimitRegistry` 的生命週期」。依實測，實際需要處理的是：

- **必要**：`global_sem` 改為依 loop 取得。
- **選用**：`registry.model_rpms` / `model_rpds` 改為依 loop 取得，僅為消除 warning 噪音。
- **不需要**：`AsyncTokenBucket` 維持共用即可（且**不應**依 loop 重建——重建會讓 TPM 餘額歸零，
  等同靜默清空配額）。

因此不是「重構整個 registry 生命週期」，而是「把兩類 limiter 的取得方式改為依 loop」。

---

## 可能的作法

### 方案 A：依 loop 建立（與 `get_global_rpm_limiter()` 一致）

以 `WeakKeyDictionary` 依 loop 保存實例，與 Phase 3 已採用的作法一致。

- 優點：與既有修法一致；單一 loop 的正式環境行為完全不變。
- 缺點：**語意變更**——多 loop 情境下併發上限變成「每個 loop 各 6 個」而非全程 6 個。
  但現況在該情境下是直接拋錯，談不上語意保障。
- 影響面：`global_sem` 是模組層公開符號，下游可能直接引用，需比照
  `with_global_limits` 的作法保留舊符號並加 `DeprecationWarning`。

### 方案 B：偵測 loop 變更後重建（比照 aiolimiter 自身作法）

保留單一實例，發現 loop 變更時重建並 warning。

- 優點：語意上仍是「單一全域上限」；改動更小。
- 缺點：重建瞬間若舊 loop 仍有 in-flight 請求，併發計數會短暫失準。

### 方案 C：只寫進文件，不改程式

明確宣告「本模組假設單一 event loop」，並在 README／`llm-integration.md` 標注。

- 優點：零風險。
- 缺點：下游測試套件仍會踩到，且錯誤訊息（`is bound to a different event loop`）
  完全看不出與本模組有關，除錯成本高。

---

## 建議

方案 A 或 B 擇一，**並補一個能重現該故障的迴歸測試**（兩個 loop 各跑併發 > `GLOBAL_CONCURRENCY`）。
現況最糟的不是行為錯誤，而是**故障條件隱晦且錯誤訊息無法指向根因**。

若暫不修復，至少應執行方案 C 的文件標注，讓踩到的人能快速定位。

---

## 重現腳本

```python
import asyncio
from agent_factory.rate_limiter.limits_parameters import GLOBAL_CONCURRENCY, global_sem

async def burst(n):
    async def hold():
        async with global_sem:
            await asyncio.sleep(0.05)
    await asyncio.gather(*[hold() for _ in range(n)])

asyncio.run(burst(GLOBAL_CONCURRENCY + 2))   # loop A：產生競爭並綁定 loop
asyncio.run(burst(GLOBAL_CONCURRENCY + 2))   # loop B：RuntimeError
```

---

## 修復記錄（2026-08-20，方案 A）

### 改動

| 檔案 | 內容 |
|------|------|
| `limits_parameters.py` | 新增 `get_global_sem()`（依 loop 建立）、`_drop_closed_loops()`；`global_sem` 與 `global_rpm_limiter` 改由 module `__getattr__` 提供並發出 `DeprecationWarning` |
| `wrappers.py` | `limits_guard_multi` 與已棄用的 `with_global_limits` 都改用 per-loop 取得函式 |
| `tests/rate_limiter/test_event_loop_binding.py` | 新增 10 個迴歸測試 |

### 刻意不改的部分

`registry` 內的 `model_rpms` / `model_rpds`（`AsyncLimiter`）**維持共用**。

理由：那些是**供應商配額**。依 loop 分割會讓每個 loop 各拿一份配額，實際請求量變成 N 倍
而撞 429 —— 對配額型限制器而言，依 loop 分割是比警告噪音更糟的錯誤。
併發上限則不同，它是本機資源保護而非供應商配額，且真正的配額仍由共用的 token bucket
與 RPM 限制器把關，因此依 loop 分割是可接受的。

`AsyncTokenBucket` 同樣維持共用（實測從不綁定 loop，且依 loop 重建會使 TPM 餘額歸零）。

### 過程中發現的次生問題：WeakKeyDictionary 洩漏

**單靠 `WeakKeyDictionary` 並不足夠。** `asyncio.Semaphore` 發生競爭後會把 `_loop`
指回該 loop，`AsyncLimiter` 首次使用後也會保存 `_event_loop`——**value 強參照了 key**，
弱參照因而永遠不會被觸發。

實測（修正前）：

| 情境 | `sem._loop` | 連續 5 次 `asyncio.run` 後 gc×3 的殘留項目 |
|------|------------|----------------------------------|
| 無競爭 | `None` | 0 |
| **有競爭** | 指回該 loop | **5（全部殘留）** |

也就是說，洩漏正好發生在本機制要處理的情境上。Phase 3 加入的 `_rpm_limiter_by_loop`
有同樣的問題。

**修法**：`_drop_closed_loops()` 在每次取用時清掉已關閉 loop 的項目。
修正後連續 20 次帶競爭的 `asyncio.run()`，對照表項目數恆為 1。

> 教訓：`WeakKeyDictionary` 只在「value 不會強參照 key」時才成立。
> 以 asyncio 同步原語為 value 時這個前提不成立，必須另有主動清理機制。

### 語意變更

多 loop 情境下，全域併發上限變成「每個 loop 各 `GLOBAL_CONCURRENCY` 個」。

單一 loop（絕大多數正式環境）行為完全不變。多 loop 情境在修復前是直接拋 `RuntimeError`，
本就沒有可保障的語意，因此不算削弱既有保證。

### 舊符號的相容處理

`global_sem` 與 `global_rpm_limiter` 是模組的公開符號，下游可能直接引用，
直接移除屬破壞性變更（architecture.md 原則 1）。改以 PEP 562 的 module `__getattr__`
攔截：仍可取用（回傳原本的模組層單例），但會發出 `DeprecationWarning` 提示改用
對應的 per-loop 函式。本 repo 內部已全數改用新函式，匯入時不會觸發警告。

### 驗證

- 全套測試：**217 passed / 2 deselected**
- 核心迴歸 `test_contended_semaphore_survives_loop_change`：兩個 loop 各跑併發
  `GLOBAL_CONCURRENCY + 2`，修復前第二個會拋 `RuntimeError`
- 併發上限仍生效：`test_semaphore_still_caps_concurrency` 驗證同時進行數不超過上限
- 洩漏已修：`test_closed_loops_are_not_retained` 與 RPM 版本各一
