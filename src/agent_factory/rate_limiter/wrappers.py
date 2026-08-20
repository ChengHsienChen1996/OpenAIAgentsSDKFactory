import time, asyncio, logging, uuid

from functools import wraps
from collections import defaultdict
from typing import Any, Dict, Callable, Optional


from agents import Agent, OpenAIChatCompletionsModel
from agents.run_context import RunContextWrapper

from .image_tokens import has_image_estimator
from .limits_parameters import global_sem, global_rpm_limiter
from .token_counter import count_tokens, estimate_input_tokens
from .token_bucket import LimitRegistry

logger = logging.getLogger("rate.guard")


def is_version_variant(response_model: str, configured_model: str) -> bool:
    """判斷供應商回報的模型名是否為設定檔模型的版本變體。

    供應商常回傳帶日期或版本的完整名稱（``gpt-4.1`` → ``gpt-4.1-2025-04-14``），
    設定檔寫的則是家族名。單純用 ``startswith`` 會誤配：``gpt-4.1-mini`` 也以
    ``gpt-4.1-`` 開頭，但它是另一個模型、另一套配額，用量絕不能算到 ``gpt-4.1`` 頭上。

    判準：去掉家族名與連字號後，剩餘部分必須以數字開頭（日期或版本號），
    ``mini`` / ``nano`` 這類變體名以字母開頭因而被排除。
    """
    if not response_model or not configured_model:
        return False
    if response_model == configured_model:
        return True

    prefix = configured_model + "-"
    if not response_model.startswith(prefix):
        return False

    return response_model[len(prefix):][:1].isdigit()


def resolve_actual_usage(
    configured_model: str,
    used_by_model: Dict[str, int],
    total_used: int,
    model_count: int,
) -> Optional[int]:
    """取得該模型的實際 token 用量，三段式比對。

    1. 精確相符：``used_by_model`` 直接有該模型名。
    2. 版本變體相符：唯一符合的變體才採用，多個符合時視為無法判定。
    3. 本次呼叫只涉及單一模型：直接採用總量。

    第 3 段是 openai-agents 0.3.3 的實際生效路徑 —— ``ModelResponse`` 只有
    ``output`` / ``usage`` / ``response_id`` 三個欄位，**沒有** ``model``，
    因此 ``used_by_model`` 恆為空。前兩段是為了相容未來 SDK 補上該欄位的情況。

    Returns:
        實際用量；完全無法判定時回傳 None（呼叫端應全額退款）。
    """
    if configured_model in used_by_model:
        return used_by_model[configured_model]

    variants = [
        name for name in used_by_model if is_version_variant(name, configured_model)
    ]
    if len(variants) == 1:
        return used_by_model[variants[0]]

    if model_count == 1 and total_used > 0:
        return total_used

    return None


def with_global_limits(fn):
    @wraps(fn)
    async def wrapped(state, *args, **kwargs):
        async with global_rpm_limiter:
            async with global_sem:
                return await fn(state, *args, **kwargs)
    return wrapped

async def _wait_with_trace(coro: Callable[[], "asyncio.Future"],
                           trace: Optional[Callable[[str, Dict[str, Any]], None]],
                           run_id: str,
                           stage: str,
                           warn_after_s: float,
                           heartbeat_every_s: float):
    """
    執行一個等待動作（例如 acquire/rpm/semaphore），
    若超過 warn_after_s 秒未完成，開始每 heartbeat_every_s 秒打一次 still_waiting 訊息。
    """
    start = time.monotonic()
    warned = False
    # 啟一個監視器，一旦主動作完成就會被取消
    done_evt = asyncio.Event()

    async def _heartbeat():
        nonlocal warned
        try:
            while not done_evt.is_set():
                await asyncio.sleep(heartbeat_every_s)
                elapsed = time.monotonic() - start
                if elapsed >= warn_after_s:
                    warned = True
                    if trace:
                        trace("still_waiting", {"run_id": run_id, "stage": stage, "elapsed_s": round(elapsed, 2)})
        except asyncio.CancelledError:
            pass

    hb_task = asyncio.create_task(_heartbeat()) if heartbeat_every_s > 0 else None
    try:
        result = await coro()
        return result, time.monotonic() - start, warned
    finally:
        done_evt.set()
        if hb_task:
            hb_task.cancel()


def limits_guard_multi(
    registry: LimitRegistry,
    umbrella: Any,                         # NoopUmbrella 或 AdaptiveUmbrella
    *,
    agent_arg: str = "agent",
    input_arg: str = "input_",
    context_arg: str = "context",
    # —— 預扣參數 —— #
    max_output_tokens: int = 1024,
    output_buffer_mult: float = 1.2,
    safety_pad_tokens: int = 64,
    per_round_pad: int = 300,              # function/tool-heavy 可拉高（500~800）
    # ---- 追蹤選項 ----
    trace: Optional[Callable[[str, Dict[str, Any]], None]] = None,  # trace(event, fields)
    warn_after_s: float = 15.0,  # 某階段等待超過這個秒數開始心跳
    heartbeat_every_s: float = 15.0,  # 心跳間隔；設 0 可關閉心跳
):
    """
    只掛在「實際發 API」的方法上（如呼叫 Runner.run 的 gateway）。
    等待順序：TPM(umbrella→model) → RPM(model) → Semaphore。
    結束後：按 raw_responses[*].model 逐模型合計 actual，做「低估補扣 / 高估退款」；umbrella 同步校正。
    """
    def deco(fn):
        @wraps(fn)
        async def wrapped(self, *args, **kwargs):
            run_id = uuid.uuid4().hex[:8]
            agent: Agent = getattr(self, agent_arg)
            model = agent.model
            if isinstance(model, str):
                model_name = model
            elif isinstance(model, OpenAIChatCompletionsModel):
                model_name = model.model
            else:
                model_name = model.__name__
            user_input = kwargs.get(input_arg, args[0] if args else "")
            ctx_obj = kwargs.get(context_arg, None)
            wrapper = RunContextWrapper(context=ctx_obj)

            # 先算輸入估算（不會阻塞）。影像由 image_tokens 換算，不做文字計數。
            estimate = estimate_input_tokens(user_input, model_name)
            user_tok = estimate.total

            # 有影像卻沒有對應估算器時，估值只能取保守 fallback。在開始等配額之前
            # 就先示警，讓使用者不必等到請求結束才發現估算不可靠。
            if estimate.image_count > 0 and not has_image_estimator(model_name):
                logger.warning(
                    "image_estimate_unreliable run_id=%s model=%s image_count=%d",
                    run_id, model_name, estimate.image_count,
                )

            t0 = time.monotonic()
            if trace:
                trace("enter", {
                    "run_id": run_id,
                    "models": [model_name],
                    "user_tok": user_tok,
                    "note": "about to build dynamic system"
                })

            # ★ 用心跳包住「動態 system prompt」這一步
            async def _build_sys():
                return await agent.get_system_prompt(wrapper)

            sys_text, sys_wait, _ = await _wait_with_trace(
                _build_sys, trace, run_id, "build_system", warn_after_s, heartbeat_every_s
            )
            if trace:
                trace("acquired", {"run_id": run_id, "stage": "build_system", "wait_s": round(sys_wait, 2)})

            sys_tok = count_tokens(sys_text, model_name)

            _max_out = getattr(agent.model_settings, "max_tokens", None) or max_output_tokens
            reserved = max(1,
                           user_tok + sys_tok
                           + safety_pad_tokens
                           + int(output_buffer_mult * _max_out)
                           + per_round_pad
                           )
            models = [model_name]

            # 可選：把目前估值也打一下。
            # text_tok / image_tok / other_tok / image_count 是 Phase 4 量測估算誤差的
            # 唯一資料來源，欄位名不可隨意更動。
            if trace:
                trace("estimate_ready", {
                    "run_id": run_id, "reserved_tokens": reserved,
                    "user_tok": user_tok, "sys_tok": sys_tok,
                    "text_tok": estimate.text_tokens,
                    "image_tok": estimate.image_tokens,
                    "other_tok": estimate.other_tokens,
                    "image_count": estimate.image_count,
                    "has_unknown_items": estimate.has_unknown_items,
                })

            # ---- 4) 送出前等待：先 umbrella.TPM，再每模型 TPM，再每模型 RPM，最後併發 ----
            try:
                # TPM（umbrella）
                async def _umb_acq(): await umbrella.acquire(reserved)
                _, wait_umb, umb_warned = await _wait_with_trace(_umb_acq, trace, run_id, "umbrella_tpm", warn_after_s,
                                                                 heartbeat_every_s)
                if trace: trace("acquired", {"run_id": run_id, "stage": "umbrella_tpm", "wait_s": round(wait_umb, 2)})
                # TPM（每模型）
                for m in models:
                    async def _tpm_acq(m=m):
                        await registry.bucket(m).acquire(reserved)

                    _, w, warned = await _wait_with_trace(_tpm_acq, trace, run_id, f"model_tpm:{m}", warn_after_s,
                                                          heartbeat_every_s)
                    if trace: trace("acquired", {"run_id": run_id, "stage": f"model_tpm:{m}", "wait_s": round(w, 2)})

                # RPM（每模型；固定順序避免鎖序競爭）
                async def _rpm_chain():
                    for _m in sorted(models):
                        async with registry.rpm(_m):
                            pass
                _, wait_rpm, rpm_warned = await _wait_with_trace(_rpm_chain, trace, run_id, "model_rpm_chain", warn_after_s, heartbeat_every_s)
                if trace: trace("acquired", {"run_id": run_id, "stage": "model_rpm_chain", "wait_s": round(wait_rpm, 2)})

                # RPD（若存在）
                rpd_limiter = registry.rpd(model_name)
                if rpd_limiter:
                    async def _rpd_acq():
                        async with rpd_limiter:
                            pass
                    _, wait_rpd, _ = await _wait_with_trace(_rpd_acq, trace, run_id, "model_rpd", warn_after_s,
                                                            heartbeat_every_s)
                    if trace: trace("acquired", {"run_id": run_id, "stage": "model_rpd", "wait_s": round(wait_rpd, 2)})

                # 併發名額（最後拿，拿到就立刻送）
                async def _sem_and_call():
                    async with global_sem:
                        return await fn(self, *args, **kwargs)

                resp, call_time, _ = await _wait_with_trace(lambda: _sem_and_call(), trace, run_id, "inflight_and_call",
                                                            warn_after_s, heartbeat_every_s)
                if trace:
                    trace("sent", {"run_id": run_id, "call_elapsed_s": round(call_time, 2)})

            except Exception:
                # 出錯：退回已預扣的 TPM
                for m in models:
                    try:
                        await registry.bucket(m).refund(reserved)
                    except Exception:
                        pass
                try:
                    await umbrella.refund(reserved)
                except Exception:
                    pass
                raise

            # ---- 5) 以 raw_responses 逐模型統計實際 token（涵蓋 function/tool 多回合）----
            used_by_model: Dict[str, int] = defaultdict(int)
            total_used = 0
            raw_list = getattr(resp, "raw_responses", None) or []
            for r in raw_list:
                u = getattr(r, "usage", None)
                tot = 0
                if u and hasattr(u, "total_tokens"):
                    tot = int(u.total_tokens)
                elif isinstance(u, dict) and "total_tokens" in u:
                    tot = int(u["total_tokens"])

                model_used = getattr(r, "model", None) or getattr(r, "model_name", None)
                if model_used and tot:
                    used_by_model[model_used] += tot
                total_used += tot

            # ---- 6) 校正：低估 → 補扣；高估 → 退款（先模型、後 umbrella）----
            for m in models:
                actual = resolve_actual_usage(m, used_by_model, total_used, len(models))

                if actual is None:
                    # 取不到任何用量：全額退回預扣，寧可短暫超額也不讓桶被靜默抽乾。
                    # 用 error 而非 warning —— 這代表 TPM 管制對該供應商實質失效，需人工檢視。
                    logger.error(
                        "usage_unavailable_full_refund run_id=%s model=%s reserved=%d "
                        "raw_responses=%d：無法取得實際用量，本次預扣全額退回，TPM 管制未生效",
                        run_id, m, reserved, len(raw_list),
                    )
                    await registry.bucket(m).refund(reserved)
                    if trace:
                        trace("refund_tpm_full", {"run_id": run_id, "model": m, "refund_tokens": reserved})
                elif actual > reserved:
                    # 低估：補扣差額（必要時會等待）
                    await registry.bucket(m).acquire(actual - reserved)
                    if trace: trace("topup_tpm", {"run_id": run_id, "model": m, "extra_tokens": actual - reserved})
                elif reserved > actual:
                    # 高估：退款差額
                    await registry.bucket(m).refund(reserved - actual)
                    if trace: trace("refund_tpm", {"run_id": run_id, "model": m, "refund_tokens": reserved - actual})

            if total_used > reserved:
                await umbrella.acquire(total_used - reserved)
                if trace: trace("topup_tpm",
                                {"run_id": run_id, "model": "umbrella", "extra_tokens": total_used - reserved})
            elif total_used <= 0:
                # 與模型桶一致：完全取不到用量時全額退回，否則 umbrella 也會被慢慢抽乾。
                await umbrella.refund(reserved)
                if trace: trace("refund_tpm_full",
                                {"run_id": run_id, "model": "umbrella", "refund_tokens": reserved})
            elif reserved > total_used:
                await umbrella.refund(reserved - total_used)
                if trace: trace("refund_tpm",
                                {"run_id": run_id, "model": "umbrella", "refund_tokens": reserved - total_used})

            if trace:
                trace("done", {
                    "run_id": run_id,
                    "total_elapsed_s": round(time.monotonic() - t0, 2),
                    "reserved_tokens": reserved,
                    "actual_total_tokens": total_used,
                    "used_by_model": dict(used_by_model),
                })

            return resp
        return wrapped
    return deco