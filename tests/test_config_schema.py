"""ModelLimitsConfig 與 YAML limits 註冊的測試。

registry 是模組層全域單例，多工廠實例／重複初始化最容易在此出事，
因此註冊行為的測試一律使用獨立的 LimitRegistry 實例，避免測試之間互相汙染。
"""
import logging
import textwrap

import pytest
from pydantic import ValidationError

from agent_factory.config_loader import AgentConfigLoader
from agent_factory.config_schema import AgentConfig, ModelLimitsConfig
from agent_factory.core import AgentFactory
from agent_factory.rate_limiter.token_bucket import (
    SOURCE_MODEL_LIMITS,
    SOURCE_YAML,
    AsyncTokenBucket,
    LimitPolicy,
    LimitRegistry,
    NullTokenBucket,
)

MODEL = "gpt-4o"


# --------------------------------------------------------------------------- #
# ModelLimitsConfig 驗證
# --------------------------------------------------------------------------- #

def test_enforced_requires_tpm_and_rpm():
    """policy=enforced 缺 TPM/RPM 時應驗證失敗，訊息指出缺哪些欄位。"""
    with pytest.raises(ValidationError, match="TPM, RPM"):
        ModelLimitsConfig(policy="enforced")


def test_enforced_with_quota_is_valid():
    limits = ModelLimitsConfig(policy="enforced", TPM=30000, RPM=500)

    assert limits.TPM == 30000
    assert limits.RPD is None


@pytest.mark.parametrize("policy", ["concurrency_only", "unlimited"])
def test_non_enforced_needs_no_quota(policy):
    """本地／自架模型不需要填任何配額數值。"""
    limits = ModelLimitsConfig(policy=policy)

    assert limits.policy == policy
    assert limits.TPM is None


def test_policy_defaults_to_enforced():
    """未寫 policy 時視為 enforced，維持既有 MODEL_LIMITS 的語意。"""
    limits = ModelLimitsConfig(TPM=100, RPM=10)

    assert limits.policy == "enforced"


def test_invalid_policy_rejected():
    with pytest.raises(ValidationError):
        ModelLimitsConfig(policy="whatever")


def test_to_registry_config_omits_unset_quotas():
    """轉換為 registry 設定時，未設定的欄位不應出現（避免 None 混入）。"""
    assert ModelLimitsConfig(policy="concurrency_only").to_registry_config() == {
        "policy": "concurrency_only"
    }
    assert ModelLimitsConfig(policy="enforced", TPM=1, RPM=2).to_registry_config() == {
        "policy": "enforced", "TPM": 1, "RPM": 2
    }


# --------------------------------------------------------------------------- #
# 註冊：冪等、衝突、優先序
# --------------------------------------------------------------------------- #

def test_repeated_identical_registration_is_idempotent():
    """重複註冊相同設定不應報錯，也不應重建限制器。

    重建會把桶內既有餘額歸零，等同於靜默清空配額。
    """
    reg = LimitRegistry({MODEL: {"TPM": 30000, "RPM": 500}})
    bucket_before = reg.bucket(MODEL)
    bucket_before.tokens = 123.0

    reg.register(MODEL, {"TPM": 30000, "RPM": 500})

    assert reg.bucket(MODEL) is bucket_before
    assert bucket_before.tokens == 123.0


def test_identical_registration_ignores_key_order_and_defaults():
    """policy 寫明與省略、鍵順序不同、RPD 為 None，都應視為同一組設定。"""
    reg = LimitRegistry({MODEL: {"TPM": 30000, "RPM": 500}})
    bucket_before = reg.bucket(MODEL)

    reg.register(MODEL, {"RPM": 500, "policy": "enforced", "TPM": 30000, "RPD": None})

    assert reg.bucket(MODEL) is bucket_before


def test_conflicting_same_source_keeps_first_and_warns(caplog):
    """同來源的衝突設定：以先註冊的為準並 warning，不做合併。"""
    reg = LimitRegistry({MODEL: {"TPM": 30000, "RPM": 500}})

    with caplog.at_level(logging.WARNING, logger="rate.guard"):
        reg.register(MODEL, {"TPM": 999, "RPM": 1})

    assert "limits_conflict" in caplog.text
    assert reg.bucket(MODEL).capacity == 30000


def test_yaml_source_overrides_model_limits():
    """優先序 YAML > MODEL_LIMITS。"""
    reg = LimitRegistry({MODEL: {"TPM": 30000, "RPM": 500}})

    reg.register(MODEL, {"policy": "concurrency_only"}, source=SOURCE_YAML)

    assert isinstance(reg.bucket(MODEL), NullTokenBucket)
    assert reg.policy(MODEL) is LimitPolicy.CONCURRENCY_ONLY


def test_model_limits_does_not_override_yaml(caplog):
    """反向不成立：MODEL_LIMITS 不得覆寫已由 YAML 宣告的設定。"""
    reg = LimitRegistry({})
    reg.register(MODEL, {"policy": "concurrency_only"}, source=SOURCE_YAML)

    with caplog.at_level(logging.WARNING, logger="rate.guard"):
        reg.register(MODEL, {"TPM": 30000, "RPM": 500}, source=SOURCE_MODEL_LIMITS)

    assert "limits_conflict" in caplog.text
    assert isinstance(reg.bucket(MODEL), NullTokenBucket)


def test_two_yaml_declarations_conflict_keeps_first(caplog):
    """同一模型被兩個 agent 以不同 limits 宣告：以第一次為準並 warning。"""
    reg = LimitRegistry({})
    reg.register(MODEL, {"TPM": 100, "RPM": 10}, source=SOURCE_YAML)

    with caplog.at_level(logging.WARNING, logger="rate.guard"):
        reg.register(MODEL, {"TPM": 200, "RPM": 20}, source=SOURCE_YAML)

    assert "limits_conflict" in caplog.text
    assert reg.bucket(MODEL).capacity == 100


def test_repeated_yaml_registration_is_idempotent():
    """多工廠實例／重複初始化時，相同的 YAML 設定重複註冊不應報錯。"""
    reg = LimitRegistry({})
    cfg = {"policy": "enforced", "TPM": 100, "RPM": 10}

    for _ in range(5):
        reg.register(MODEL, cfg, source=SOURCE_YAML)

    assert isinstance(reg.bucket(MODEL), AsyncTokenBucket)
    assert reg.bucket(MODEL).capacity == 100


# --------------------------------------------------------------------------- #
# 從 YAML 走完整條路徑
# --------------------------------------------------------------------------- #

def _write_yaml(tmp_path, body: str):
    prompt = tmp_path / "p.md"
    prompt.write_text("測試用 prompt", encoding="utf-8")
    yaml_file = tmp_path / "agents.yaml"
    yaml_file.write_text(textwrap.dedent(body).replace("PROMPT", str(prompt)), encoding="utf-8")
    return yaml_file


LOCAL_AGENT_YAML = """
    agents:
      local:
        - ocr_agent:
            name: LocalAgent
            model_instruction:
              dynamic_prompt: false
              instruction_file_path: PROMPT
            model_params:
              model: my-local-model
              client:
                api_key: k
                base_url: https://example.invalid/v1
              limits:
                policy: concurrency_only
    """

NO_LIMITS_AGENT_YAML = """
    agents:
      plain:
        - plain_agent:
            name: PlainAgent
            model_instruction:
              dynamic_prompt: false
              instruction_file_path: PROMPT
            model_params:
              model: gpt-4o
              client:
                api_key: k
                base_url: https://example.invalid/v1
    """


def test_yaml_limits_are_parsed(tmp_path):
    """YAML 的 limits 欄位應被解析進 AgentConfig。"""
    config = AgentConfigLoader.load_validated(_write_yaml(tmp_path, LOCAL_AGENT_YAML))[0]

    assert config.model_params.limits is not None
    assert config.model_params.limits.policy == "concurrency_only"


def test_yaml_enforced_without_quota_reports_agent_name(tmp_path):
    """驗證失敗的錯誤訊息必須帶 agent 的 YAML key 與缺少的欄位。"""
    yaml_file = _write_yaml(
        tmp_path,
        """
        agents:
          bad:
            - bad_agent:
                name: BadAgent
                model_instruction:
                  dynamic_prompt: false
                  instruction_file_path: PROMPT
                model_params:
                  model: gpt-4o
                  client:
                    api_key: k
                    base_url: https://example.invalid/v1
                  limits:
                    policy: enforced
        """,
    )

    with pytest.raises(ValueError, match="bad_agent"):
        AgentConfigLoader.load_validated(yaml_file)


def test_factory_registers_yaml_limits(tmp_path, monkeypatch):
    """工廠初始化時應把 YAML 宣告的 limits 註冊進 registry。"""
    from agent_factory import core

    isolated = LimitRegistry({})
    monkeypatch.setattr(core, "limit_registry", isolated)

    AgentFactory.create_factory_from_yaml(_write_yaml(tmp_path, LOCAL_AGENT_YAML))

    assert isolated.policy("my-local-model") is LimitPolicy.CONCURRENCY_ONLY
    assert isinstance(isolated.bucket("my-local-model"), NullTokenBucket)


def test_multiple_factory_instances_do_not_error(tmp_path, monkeypatch):
    """同一份 YAML 建立多個工廠實例時，重複註冊必須是冪等的。"""
    from agent_factory import core

    isolated = LimitRegistry({})
    monkeypatch.setattr(core, "limit_registry", isolated)
    yaml_file = _write_yaml(tmp_path, LOCAL_AGENT_YAML)

    for _ in range(3):
        AgentFactory.create_factory_from_yaml(yaml_file)

    assert isolated.policy("my-local-model") is LimitPolicy.CONCURRENCY_ONLY


# --------------------------------------------------------------------------- #
# 向後相容：完全不含 limits 欄位
# --------------------------------------------------------------------------- #

def test_yaml_without_limits_field_still_validates(tmp_path):
    """完全不含 limits 欄位的既有 YAML 仍能通過驗證，limits 為 None。"""
    config = AgentConfigLoader.load_validated(_write_yaml(tmp_path, NO_LIMITS_AGENT_YAML))[0]

    assert config.model_params.limits is None


def test_yaml_without_limits_does_not_touch_registry(tmp_path, monkeypatch):
    """未宣告 limits 的 agent 完全不觸碰 registry —— 既有設定的行為保持不變。"""
    from agent_factory import core

    isolated = LimitRegistry({MODEL: {"TPM": 30000, "RPM": 500}})
    original_bucket = isolated.bucket(MODEL)
    monkeypatch.setattr(core, "limit_registry", isolated)

    AgentFactory.create_factory_from_yaml(_write_yaml(tmp_path, NO_LIMITS_AGENT_YAML))

    # 桶物件必須是同一個實例：既沒有被重建，也沒有被改成 Null 版
    assert isolated.bucket(MODEL) is original_bucket
    assert isolated.policy(MODEL) is LimitPolicy.ENFORCED


def test_existing_fixture_yaml_unaffected(sample_yaml_path):
    """Phase 1 建立的既有 fixture（不含 limits）行為不變。"""
    config = AgentConfigLoader.load_validated(sample_yaml_path)[0]

    assert config.model_params.limits is None
    assert config.model_params.model == "gpt-4.1"


def test_agent_config_accepts_limits_field():
    """AgentConfig 直接建構時也接受 limits。"""
    config = AgentConfig(
        name="A",
        model_instruction={"dynamic_prompt": False, "instruction_file_path": "x.md"},
        model_params={
            "model": "m",
            "client": {"base_url": "https://example.invalid", "api_key": "k"},
            "limits": {"policy": "unlimited"},
        },
    )

    assert config.model_params.limits.policy == "unlimited"
