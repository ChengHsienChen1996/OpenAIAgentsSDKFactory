# 改動日誌規範

## 目的

每次 AI 完成所有作業後，在 `/logs` 目錄下產出一份改動總結檔，方便人工快速複查本次的所有程式變更。

> `logs/` 屬版控隔離區（Zone 2）：在 `dev_ai` 上照常 commit 以保留歷史，但透過 `.no-merge` 機制確保不會合併進 `main`。機制細節見 [git-workflow.md](git-workflow.md)〈不上 main 的檔案隔離〉。

## 檔案命名

```
logs/<YYYY-MM-DD>_<tag>_<簡要描述>.md
```

- 日期為作業當天。
- `<tag>` 對應本次主要改動的 commit 標籤（feat、fix、refactor 等）。
- `<簡要描述>` 使用英文 kebab-case。

### 範例

```
logs/2026-05-07_feat_add-summary-agent.md
logs/2026-05-07_fix_rate-limiter-token-refund.md
logs/2026-05-07_refactor_extract-prompt-loader.md
```

若同一天有多次獨立作業，各自產出獨立的日誌檔。

## 檔案內容格式

```markdown
# 改動總結 — <簡要標題>

日期：YYYY-MM-DD
分支：dev_ai
相關 commit：<commit hash 或簡要列表>

---

## 變更清單

1. `src/xxx/aaa.py`：此次更新修正某某函式的某某問題
2. `src/xxx/bbb.py`：新增程式檔，用於放置某某用途模組，內含 `func_a`、`func_b` 函式
3. `tests/test_aaa.py`：新增對應單元測試，涵蓋正常流程與邊界條件
4. `docs/some-doc.md`：更新文件說明以反映本次改動

## 測試結果

- pytest 執行結果：X passed / Y failed（或「全數通過」）
- 需人工測試項目：`test_xxx.py` 中的付費 API 相關測試（已撰寫 mock 骨架）

## 備註

（選填）補充說明、已知限制、後續待辦等。
```

## 撰寫原則

1. **每個變更的檔案都要列出**，包含新增、修改、刪除。
2. 描述應簡明扼要，讓人工一眼理解改了什麼、為什麼改。
3. 若有付費 API 相關的測試需要人工執行，務必在「測試結果」區塊明確標注。
4. 改動日誌是本次作業的 **最後一步**，在所有程式碼完成、測試通過、commit 完畢後才撰寫。
