"""Сериализация типов контрактов: JSON туда и обратно без потери Decimal и времени."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from lab.contracts import (
    Costs,
    Health,
    KeyRights,
    OrderIntent,
    Signal,
    StrategyManifest,
)


def test_order_intent_roundtrip_keeps_decimal_exact():
    intent = OrderIntent(
        strategy_id="cex-spot-preset-ema-cross-v1",
        venue="bybit",
        instrument="BTC/USDT",
        side="buy",
        qty=Decimal("0.00123456"),
        price=Decimal("64123.45"),
        order_type="limit",
        mode="paper",
        signal_id="sig-1",
        client_order_id="lab-1",
    )
    raw = intent.model_dump_json()
    back = OrderIntent.model_validate_json(raw)
    assert back == intent
    # Decimal не превращается во float ни на одном шаге
    assert json.loads(raw)["qty"] == "0.00123456"
    assert back.leverage == Decimal(1)
    assert back.reduce_only is False


def test_order_intent_rejects_unknown_side():
    with pytest.raises(ValidationError):
        OrderIntent(
            strategy_id="s",
            venue="v",
            instrument="i",
            side="hold",
            qty=Decimal(1),
            price=None,
            order_type="market",
            mode="paper",
            signal_id="sig",
            client_order_id="c",
        )


def test_signal_roundtrip_keeps_utc_datetime():
    decided = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)
    sig = Signal(
        strategy_id="meme-sol-pumpfun-early-v1",
        decided_at=decided,
        instrument="SOL/USDC",
        side="buy",
        size=Decimal("12.5"),
        price_ref=None,
        inputs_hash="abc123",
        ttl_s=60,
        meta={"reason": "early"},
    )
    back = Signal.model_validate_json(sig.model_dump_json())
    assert back.decided_at == decided
    assert back.decided_at.tzinfo is not None
    assert back.meta == {"reason": "early"}


def test_costs_total_is_sum_of_components():
    costs = Costs(
        fee=Decimal("0.10"),
        slippage=Decimal("0.05"),
        funding=Decimal("0.01"),
        gas=Decimal("0.02"),
        royalty=Decimal("0.00"),
    )
    assert costs.total == Decimal("0.18")
    assert Costs.model_validate_json(costs.model_dump_json()).total == Decimal("0.18")


def test_health_roundtrip():
    h = Health(status="degraded", detail="квота 90%", checked_at=datetime(2026, 9, 5, tzinfo=UTC))
    assert Health.model_validate_json(h.model_dump_json()) == h


def test_key_rights_withdraw_is_explicit():
    rights = KeyRights(trade=True, withdraw=True)
    assert KeyRights.model_validate_json(rights.model_dump_json()).withdraw is True


def test_manifest_roundtrip_and_id_parts():
    manifest = StrategyManifest(
        slug="ema-cross-v1",
        branch="cex-spot",
        venue="bybit",
        source_kind="preset",
        instruments=["BTC/USDT"],
        params={"fast": 12, "slow": 26},
        can_backtest=True,
        stop={"max_dd_pct": "10"},
    )
    back = StrategyManifest.model_validate_json(manifest.model_dump_json())
    assert back == manifest
    assert back.stop.max_dd_pct == Decimal("10")


def test_manifest_missing_required_field_names_it():
    with pytest.raises(ValidationError) as err:
        StrategyManifest(slug="x", branch="cex-spot", venue="bybit", source_kind="preset")
    missing = {e["loc"][0] for e in err.value.errors()}
    assert "instruments" in missing
    assert "stop" in missing


def test_manifest_rejects_unknown_branch():
    with pytest.raises(ValidationError):
        StrategyManifest(
            slug="x",
            branch="forex",
            venue="bybit",
            source_kind="preset",
            instruments=["EUR/USD"],
            stop={"max_dd_pct": "5"},
        )


def test_stop_requires_daily_or_max_dd():
    from lab.contracts import StopSpec

    assert StopSpec(daily_pct=Decimal(3)).max_dd_pct is None
    assert StopSpec(max_dd_pct=Decimal(10)).daily_pct is None
    with pytest.raises(ValidationError, match="daily_pct"):
        StopSpec()


def test_costs_total_includes_priority_fee():
    costs = Costs(fee=Decimal("0.1"), priority_fee=Decimal("0.02"))
    assert costs.total == Decimal("0.12")
