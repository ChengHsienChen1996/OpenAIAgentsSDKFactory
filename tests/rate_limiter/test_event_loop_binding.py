"""模組層同步原語跨 event loop 的迴歸測試。

背景見 .agent/notes/event-loop-binding.md。

`asyncio.Semaphore` 會在首次發生競爭時綁定當時的 event loop；若另一個 loop 再次
發生競爭就會拋出 `RuntimeError: ... is bound to a different event loop`。
這是間歇性、負載相關的故障——低負載時完全正常，因此必須用「兩個 loop 都製造競爭」
的方式才測得出來。

本檔的測試刻意各自建立 event loop（不使用 pytest-asyncio 的 fixture），
因為要驗證的正是跨 loop 行為本身。
"""
import asyncio
import warnings

import pytest

from agent_factory.rate_limiter import limits_parameters
from agent_factory.rate_limiter.limits_parameters import (
    GLOBAL_CONCURRENCY,
    get_global_rpm_limiter,
    get_global_sem,
)

# 必須超過併發上限才會產生 waiter，Semaphore 也才會綁定 loop
CONTENDED = GLOBAL_CONCURRENCY + 2


async def _burst(n: int) -> int:
    """同時發出 n 個請求，回傳實際完成數。"""
    completed = 0

    async def hold():
        nonlocal completed
        async with get_global_sem():
            await asyncio.sleep(0.01)
            completed += 1

    await asyncio.gather(*[hold() for _ in range(n)])
    return completed


@pytest.mark.timeout(60)
def test_contended_semaphore_survives_loop_change():
    """兩個 event loop 各自跑到超過併發上限時都不得拋錯。

    這是本次修復的核心迴歸：修復前第二個 loop 會拋
    `RuntimeError: ... is bound to a different event loop`。
    """
    assert asyncio.run(_burst(CONTENDED)) == CONTENDED
    assert asyncio.run(_burst(CONTENDED)) == CONTENDED


@pytest.mark.timeout(60)
def test_semaphore_is_per_loop_instance():
    """每個 event loop 取得的是各自的 semaphore 實例。"""
    first = asyncio.run(_get_sem_id())
    second = asyncio.run(_get_sem_id())

    assert first != second


async def _get_sem_id() -> int:
    return id(get_global_sem())


@pytest.mark.timeout(60)
def test_same_loop_reuses_one_semaphore():
    """同一個 loop 內重複取得應為同一個實例，否則併發上限形同虛設。"""

    async def _twice():
        return get_global_sem() is get_global_sem()

    assert asyncio.run(_twice()) is True


@pytest.mark.timeout(60)
def test_semaphore_still_caps_concurrency():
    """依 loop 建立後，併發上限在單一 loop 內仍然生效。"""
    peak = 0
    current = 0

    async def _run():
        nonlocal peak, current

        async def hold():
            nonlocal peak, current
            async with get_global_sem():
                current += 1
                peak = max(peak, current)
                await asyncio.sleep(0.01)
                current -= 1

        await asyncio.gather(*[hold() for _ in range(CONTENDED)])

    asyncio.run(_run())

    assert peak <= GLOBAL_CONCURRENCY


@pytest.mark.timeout(60)
def test_rpm_limiter_survives_loop_change():
    """全域 RPM 限制器同樣不得因跨 loop 而出錯或發出警告。"""

    async def _use():
        async with get_global_rpm_limiter():
            return True

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert asyncio.run(_use()) is True
        assert asyncio.run(_use()) is True

    reuse_warnings = [w for w in caught if "re-used across loops" in str(w.message)]
    assert not reuse_warnings


@pytest.mark.timeout(60)
def test_closed_loops_are_not_retained():
    """已關閉的 loop 不得在對照表中累積。

    僅靠 WeakKeyDictionary 不足夠：發生競爭後 `Semaphore._loop` 會指回該 loop，
    形成「value 強參照 key」，弱參照永遠不會觸發。實測不加主動清理時，
    連續 5 次帶競爭的 asyncio.run 會留下 5 個項目且 gc 無法回收。
    """
    for _ in range(5):
        asyncio.run(_burst(CONTENDED))

    # 最後一次 run 的 loop 已關閉，下一次取用時會被清掉
    asyncio.run(_get_sem_id())

    live = [loop for loop in limits_parameters._sem_by_loop if not loop.is_closed()]
    assert len(limits_parameters._sem_by_loop) <= 1
    assert len(live) <= 1


@pytest.mark.timeout(60)
def test_closed_loops_are_not_retained_for_rpm_limiter():
    """RPM 限制器的對照表同樣不得累積已關閉的 loop。"""

    async def _use():
        async with get_global_rpm_limiter():
            return True

    for _ in range(5):
        asyncio.run(_use())

    assert len(limits_parameters._rpm_limiter_by_loop) <= 1


# --------------------------------------------------------------------------- #
# 舊符號的相容性
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "name,replacement",
    [
        ("global_sem", "get_global_sem"),
        ("global_rpm_limiter", "get_global_rpm_limiter"),
    ],
)
def test_deprecated_globals_still_accessible_with_warning(name, replacement):
    """舊的模組層符號仍可取用（下游可能直接引用），但會發出 DeprecationWarning。"""
    with pytest.warns(DeprecationWarning, match=replacement):
        value = getattr(limits_parameters, name)

    assert value is not None


def test_unknown_attribute_still_raises_attribute_error():
    """module __getattr__ 不得吞掉一般的 AttributeError。"""
    with pytest.raises(AttributeError, match="no attribute"):
        limits_parameters.definitely_not_a_real_symbol
