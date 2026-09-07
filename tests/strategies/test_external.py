"""Адаптер внешних проектов `external-signal` (R01.3, История 106): webhook/файл/Telegram-канал,
формат — `config/external_signals.yaml`; сигналы попадают в журнал как стратегия `ext-<имя>`."""

import json
from datetime import UTC, datetime
from decimal import Decimal

from lab.core.journal import Journal
from lab.core.registry import Registry
from lab.strategies.external import (
    ExternalSignalStrategy,
    file_events,
    load_external_signals,
    telegram_event,
    webhook_event,
)


def test_config_has_example_and_three_transports():
    cfg = load_external_signals()
    assert {s.transport for s in cfg.sources} >= {"webhook", "file", "telegram"}
    assert cfg.example["instrument"] and cfg.example["side"] in ("buy", "sell")


def test_webhook_example_becomes_signal_of_ext_strategy(session):
    cfg = load_external_signals()
    source = cfg.source("crypto-trader")
    strategy = ExternalSignalStrategy(source)
    assert strategy.strategy_id == "cex-spot-ext-crypto-trader"
    event = webhook_event(source, json.dumps(cfg.example).encode(), secret=None)
    signals = strategy.on_event(event)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.strategy_id == "cex-spot-ext-crypto-trader"
    assert sig.instrument == cfg.example["instrument"] and sig.side == cfg.example["side"]
    assert sig.decided_at == datetime.fromisoformat(cfg.example["ts"]).astimezone(UTC)
    assert sig.size == Decimal(str(cfg.example["size"])) and len(sig.inputs_hash) == 64
    assert sig.meta["source"] == "crypto-trader"
    added = Registry(session).add(strategy.manifest)  # источник заводится в реестре как стратегия
    assert added.id == "cex-spot-ext-crypto-trader" and not strategy.manifest.can_backtest
    rec = Journal(session).record_signal(sig)
    assert rec.strategy_id == "cex-spot-ext-crypto-trader"


def test_webhook_rejects_bad_signature_and_unknown_payload():
    cfg = load_external_signals()
    source = cfg.source("crypto-trader")
    body = json.dumps(cfg.example).encode()
    assert webhook_event(source, body, secret="s3cret", signature="deadbeef") is None
    assert webhook_event(source, b'{"hello": "world"}', secret=None) is None


def test_file_and_telegram_transports(tmp_path):
    cfg = load_external_signals()
    source = cfg.source("solana-smart-money")
    path = tmp_path / "signals.jsonl"
    path.write_text(
        json.dumps(cfg.example)
        + "\nnot json\n"
        + json.dumps({**cfg.example, "source_id": "b"})
        + "\n"
    )
    events = file_events(source, path)
    assert [e.payload["source_id"] for e in events] == [cfg.example["source_id"], "b"]
    strategy = ExternalSignalStrategy(source)
    assert strategy.strategy_id == "meme-ext-solana-smart-money"
    assert len(strategy.on_event(events[0])) == 1
    assert strategy.on_event(events[0]) == []  # повтор того же source_id не даёт второго сигнала
    tg = telegram_event(
        source,
        "BTC/USDT LONG вход 100 цель 110 стоп 95",
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        message_ref="7",
    )
    assert tg is not None and strategy.on_event(tg)[0].side == "buy"
    assert (
        telegram_event(
            source, "просто пост", published_at=datetime(2026, 9, 1, tzinfo=UTC), message_ref="8"
        )
        is None
    )
