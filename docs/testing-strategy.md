# 測試策略

## 核心原則

根據程式是否呼叫 **付費第三方服務**（包含但不限於 LLM API、雲端資料庫、付費 SaaS API 等），採取不同的測試策略：

| 類別 | 測試方式 | 執行者 |
|------|----------|--------|
| **不涉及付費 API** | 撰寫完整單元測試，使用 `pytest` 執行並根據錯誤結果修正，直到所有測試通過 | AI 自動完成 |
| **涉及付費 API** | 撰寫含 mock 的測試骨架，不實際執行 | AI 撰寫，**人工執行與驗證** |

## 非付費 API 程式：完整自動測試

### 流程

1. 完成功能程式碼後，立即撰寫對應的單元測試。
2. 執行 `pytest` 並檢視結果。
3. 若有失敗，根據錯誤訊息修正程式碼或測試，重複直到全部通過。
4. 測試通過後方可進行 commit。

### 規範

- 測試檔案放在 `tests/` 目錄，結構鏡射 `src/` 的模組層級。
- 檔案命名：`test_<module_name>.py`。
- 使用 `pytest` 作為唯一測試框架。
- 善用 `conftest.py` 集中管理 fixtures。
- 異步測試使用 `pytest-asyncio`，標記 `@pytest.mark.asyncio`。

### 範例

```python
# tests/test_config_loader.py
import pytest
from src.my_package.config_loader import load_config


@pytest.mark.asyncio
async def test_load_config_returns_valid_structure():
    config = await load_config("tests/fixtures/sample.yaml")
    assert config is not None
    assert "agents" in config
```

## 付費 API 程式：Mock 測試骨架

### 流程

1. 完成功能程式碼後，撰寫測試骨架，其中所有付費 API 呼叫皆以 `unittest.mock.AsyncMock` 或 `pytest-mock` 模擬。
2. **不執行測試**，而是回報人工進行驗證。
3. 在改動日誌中註明哪些測試需要人工執行。

### 規範

- Mock 應模擬真實的 API 回傳格式，包含正常回應與常見錯誤（如 rate limit、timeout）。
- 測試骨架應涵蓋：正常流程、錯誤處理、邊界條件。
- 使用 `@pytest.fixture` 將 mock 物件集中於 `conftest.py`。

### 範例

```python
# tests/test_summary_agent.py
import pytest
from unittest.mock import AsyncMock, patch


@pytest.fixture
def mock_llm_response():
    """模擬 LLM API 正常回應"""
    return {
        "choices": [{"message": {"content": "這是摘要結果"}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 30},
    }


@pytest.mark.asyncio
async def test_summary_agent_normal_flow(mock_llm_response):
    with patch("src.my_package.agent.call_llm_api", new_callable=AsyncMock) as mock_api:
        mock_api.return_value = mock_llm_response
        result = await run_summary_agent("測試輸入文字")
        assert result is not None
        mock_api.assert_called_once()


@pytest.mark.asyncio
async def test_summary_agent_rate_limit_retry():
    """測試遇到 rate limit 時的重試邏輯（需人工驗證實際 API 行為）"""
    with patch("src.my_package.agent.call_llm_api", new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = [
            Exception("Rate limit exceeded"),
            {"choices": [{"message": {"content": "重試後成功"}}]},
        ]
        result = await run_summary_agent("測試輸入文字")
        assert result is not None
        assert mock_api.call_count == 2
```

## 判斷原則

若不確定某個第三方服務是否屬於「付費 API」，依以下標準判斷：

- **會產生費用的呼叫**（按量計費、訂閱制 API）→ 視為付費 API，撰寫 mock 骨架。
- **免費且無呼叫限制疑慮的服務**（本地資料庫、免費公開 API）→ 可直接測試。
- **有免費額度但超過會計費的服務** → 視為付費 API，避免意外產生費用。
