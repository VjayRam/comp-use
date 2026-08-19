import pytest

from comp_use.config import load_settings
from comp_use.guardrail import AllowlistViolation, Guardrail
from comp_use.schemas import Locator, LocatorStrategy, RiskTier, Step


def make_guardrail() -> Guardrail:
    return Guardrail(load_settings())


def test_allowed_url_and_action_pass():
    g = make_guardrail()
    g.check_allowlist("http://localhost:5000/member/12345", "navigate")


def test_disallowed_domain_raises():
    g = make_guardrail()
    with pytest.raises(AllowlistViolation):
        g.check_allowlist("http://evil.example.com/x", "navigate")


def test_disallowed_action_type_raises():
    g = make_guardrail()
    with pytest.raises(AllowlistViolation):
        g.check_allowlist("http://localhost:5000/x", "delete_everything")


def test_risky_step_requires_confirmation_by_default():
    g = make_guardrail()
    step = Step(
        action="click",
        locator=Locator(strategy=LocatorStrategy.TEXT, value={"text": "Transfer"}),
        risk_tier=RiskTier.RISKY,
    )
    assert g.requires_confirmation(step, confirm_risky=False) is True
    assert g.requires_confirmation(step, confirm_risky=True) is False


def test_safe_step_never_requires_confirmation():
    g = make_guardrail()
    step = Step(
        action="click",
        locator=Locator(strategy=LocatorStrategy.TEXT, value={"text": "Search"}),
        risk_tier=RiskTier.SAFE,
    )
    assert g.requires_confirmation(step, confirm_risky=False) is False


def test_redact_masks_account_numbers_and_amounts():
    g = make_guardrail()
    redacted = g.redact("Member 123456789 has balance $1,500.00 today")
    assert "123456789" not in redacted
    assert "$1,500.00" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_masks_mock_bank_account_and_transaction_ids():
    g = make_guardrail()
    redacted = g.redact(
        "Transferred from ACC-001 to ACC-002, txn TXN-000123, "
        "sub-account SUB-0042, confirmation CONF-000007"
    )
    for value in ("ACC-001", "ACC-002", "TXN-000123", "SUB-0042", "CONF-000007"):
        assert value not in redacted
    assert redacted.count("[REDACTED]") == 5
