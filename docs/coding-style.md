# 編碼風格

## 異步優先原則

- **預設使用 `async/await`** 撰寫函式，僅在有明確理由時才使用同步寫法（例如：純 CPU 計算且無 I/O、第三方函式庫不支援異步）。
- 程式入口點統一使用 `asyncio.run()` 啟動。
- 選擇異步框架時以 **易讀性、可維護性、擴展性** 為優先，不強制綁定特定框架。

## 命名慣例

| 對象 | 風格 | 範例 |
|------|------|------|
| 模組 / 檔案 | snake_case | `config_loader.py` |
| 函式 / 方法 | snake_case | `async def load_config()` |
| 類別 | PascalCase | `AgentFactory` |
| 常數 | UPPER_SNAKE_CASE | `MAX_RETRY_COUNT` |
| 私有成員 | 單底線前綴 | `_internal_cache` |

## 型別標註

- 所有公開函式的參數與回傳值 **必須** 加上 type hints。
- 內部輔助函式建議加上，但不強制。
- 複雜型別優先使用 `TypeAlias` 或 `TypeVar` 提升可讀性。

## 程式碼組織

1. **單一職責**：每個模組 / 類別只負責一件事，過於龐大時應拆分。
2. **依賴注入**：避免在模組層級硬編碼依賴，優先透過參數或設定傳入。
3. **錯誤處理**：使用明確的例外類別，避免裸露的 `except Exception`；需要時定義專案自訂例外。
4. **文件字串**：公開類別與函式需撰寫 docstring，格式採用 Google style 或 NumPy style（專案內統一即可）。

## 格式化與檢查

- 建議搭配 `ruff` 進行 linting 與格式化。
- `pyproject.toml` 中統一設定規則，保持團隊一致。
