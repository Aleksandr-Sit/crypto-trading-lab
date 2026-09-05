"""Перечисления словаря спецификации: ветки, ступени, статусы, режимы."""

from enum import StrEnum


class Branch(StrEnum):
    CEX_SPOT = "cex-spot"
    CEX_PERP = "cex-perp"
    DEX_PERP = "dex-perp"
    COPY = "copy"
    MEME = "meme"
    NFT = "nft"
    PREDICTION = "prediction"
    RH = "rh"


class Rung(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    MICRO = "micro"
    SIGNAL = "signal"
    SEMI = "semi"
    AUTO = "auto"


RUNG_ORDER: tuple[Rung, ...] = (
    Rung.BACKTEST,
    Rung.PAPER,
    Rung.MICRO,
    Rung.SIGNAL,
    Rung.SEMI,
    Rung.AUTO,
)


class Status(StrEnum):
    CANDIDATE = "candidate"
    MEASURING = "measuring"
    PASSED = "passed"
    FAILED = "failed"
    DEGRADED = "degraded"
    RETIRED = "retired"


class Mode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class MeasureMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    MICRO = "micro"
    FORWARD = "forward"


class SignalOutcome(StrEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    SKIPPED = "skipped"
    EXPIRED = "expired"


class CandidateDecision(StrEnum):
    PENDING = "pending"
    DRAFT = "draft"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class OrderState(StrEnum):
    NEW = "new"
    OPEN = "open"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
