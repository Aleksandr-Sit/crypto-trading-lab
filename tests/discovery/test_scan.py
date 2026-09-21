"""Шов R15/R15.2: discovery.scan() — общий интерфейс источников, отпечаток, дедупликация."""

from __future__ import annotations

from lab.contracts import CandidateDecision
from lab.core.registry import Registry
from lab.discovery import CandidateSpec, Discovery, default_sources, fingerprint
from lab.discovery.config import DiscoveryConfig


class FakeSource:
    """Источник за общим интерфейсом CandidateSource — без сети."""

    def __init__(self, id: str, specs: list[CandidateSpec], *, fail: str = "") -> None:
        self.id = id
        self._specs = specs
        self._fail = fail
        self.calls = 0

    def fetch(self) -> list[CandidateSpec]:
        self.calls += 1
        if self._fail:
            raise RuntimeError(self._fail)
        return list(self._specs)


class FakeBot:
    def __init__(self) -> None:
        self.cards: list[tuple[str, dict]] = []

    def send_card_sync(self, kind: str, payload: dict) -> int:
        self.cards.append((kind, dict(payload)))
        return len(self.cards)


def spec(ref: str, *, facts: dict | None = None, venue: str = "okx") -> CandidateSpec:
    return CandidateSpec(
        kind="trader",
        ref=ref,
        venue=venue,
        branch="copy",
        source="okx_lead",
        source_url=f"https://example/{ref}",
        facts=facts or {"win_rate_pct": 60},
    )


def test_scan_enqueues_candidates_and_sends_cards(session):
    bot = FakeBot()
    d = Discovery(session, sources=[FakeSource("okx_lead", [spec("AAA"), spec("BBB")])], bot=bot)

    result = d.scan()

    assert [c.ref for c in result.new] == ["AAA", "BBB"]
    assert result.by_source == {"okx_lead": 2}
    assert [k for k, _ in bot.cards] == ["candidate", "candidate"]
    assert bot.cards[0][1]["ref"] == "AAA"
    assert bot.cards[0][1]["kind"] == "trader"
    queued = Registry(session).candidates(CandidateDecision.PENDING)
    assert {c.ref for c in queued} == {"AAA", "BBB"}


def test_repeat_scan_does_not_duplicate(session):
    source = FakeSource("okx_lead", [spec("AAA"), spec("AAA"), spec("BBB")])
    d = Discovery(session, sources=[source])

    first = d.scan()
    second = d.scan()

    assert len(first.new) == 2  # дубликат внутри одного прогона тоже схлопнут
    assert second.new == []
    assert len(Registry(session).candidates()) == 2


def test_rejected_returns_only_when_fingerprint_changes(session):
    source = FakeSource("okx_lead", [spec("AAA", facts={"win_rate_pct": 60})])
    d = Discovery(session, sources=[source])
    candidate = d.scan().new[0]
    d.reject(candidate.id, reason="не нравится")

    unchanged = d.scan()
    assert unchanged.new == [] and unchanged.updated == []
    assert Registry(session).candidates()[0].decision == CandidateDecision.REJECTED

    source._specs = [spec("AAA", facts={"win_rate_pct": 90})]
    changed = d.scan()

    assert [c.ref for c in changed.updated] == ["AAA"]
    assert Registry(session).candidates()[0].decision == CandidateDecision.PENDING


def test_source_failure_does_not_stop_scan(session):
    d = Discovery(
        session,
        sources=[
            FakeSource("broken", [], fail="503 от площадки"),
            FakeSource("okx_lead", [spec("AAA")]),
        ],
    )

    result = d.scan()

    assert [c.ref for c in result.new] == ["AAA"]
    assert "503 от площадки" in result.errors["broken"]


def test_fingerprint_reacts_to_facts_only():
    base = spec("AAA", facts={"win_rate_pct": 60})
    same_note = CandidateSpec(**{**base.__dict__, "note": "другая заметка"})
    other = spec("AAA", facts={"win_rate_pct": 90})

    assert fingerprint(base) == fingerprint(same_note)
    assert fingerprint(base) != fingerprint(other)


def test_default_sources_cover_six_kinds():
    # Конфиг по умолчанию, а не боевой: в `config/discovery.yaml` часть лент бывает
    # намеренно выключена (copy/prediction приглушены 21.09.2026), а здесь проверяется,
    # что код умеет собрать все шесть, — это разные утверждения.
    sources = default_sources(config=DiscoveryConfig())

    ids = {s.id for s in sources}
    assert len(ids) >= 6, ids
    expected = {"okx_lead", "hyperliquid", "polymarket", "smart_money", "github", "nft_launchpad"}
    assert expected <= ids
    for s in sources:
        assert callable(s.fetch)
