"""Типы замера: сделка, метрики, порог, снимок. Деньги — Decimal, USD."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lab.contracts import Costs, MeasureMode


class MeasureError(Exception):
    pass


class LookaheadError(MeasureError):
    """Сигнал «решён» раньше, чем данные, которые он видел, — заглядывание в будущее."""


class IncompleteData(MeasureError):
    """Данных не хватило или источник упал посреди замера (R11.6)."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class ClosedTrade(_Model):
    """Закрытая сделка — вход и выход по FIFO, издержки по компонентам."""

    instrument: str
    side: Literal["long", "short"]
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    pnl_gross: Decimal
    costs: Costs

    @property
    def pnl_net(self) -> Decimal:
        return self.pnl_gross - self.costs.total

    @property
    def turnover(self) -> Decimal:
        return self.qty * (self.entry_price + self.exit_price)


class NotApplicable(_Model):
    """Ветко-специфичная метрика не считается здесь: `None` с причиной (R11)."""

    reason: str


class SampleStatus(_Model):
    status: Literal["ok", "insufficient"]
    n: int
    required: int
    detail: str


class CostsBreakdown(_Model):
    fee: Decimal = Decimal(0)
    slippage: Decimal = Decimal(0)
    funding: Decimal = Decimal(0)
    gas: Decimal = Decimal(0)
    priority_fee: Decimal = Decimal(0)
    royalty: Decimal = Decimal(0)
    total: Decimal = Decimal(0)
    turnover: Decimal = Decimal(0)


class PhaseStat(_Model):
    """Как стратегия отработала в одной фазе рынка: рост, падение или боковик."""

    windows: int = 0
    profitable: int = 0
    ahead: int = 0


class Stability(_Model):
    """Как стратегия ведёт себя на МНОГИХ окнах, а не на одном (В12, замечание 08.09.2026).

    Одиночное окно — плохая опора: у `pifagor-forever-sma` вердикт менялся с «преимущество»
    на «разгром» от сдвига границы. Здесь считается, в скольких окнах стратегия зарабатывала
    после издержек и в скольких обходила бенчмарк своей ветки.

    `compared` может быть меньше `windows`: в части окон бенчмарка в хранилище нет,
    и сравнивать не с чем — такие окна в долю «впереди» не входят, а не считаются провалом.
    """

    windows: int = 0
    profitable: int = 0
    ahead: int = 0
    compared: int = 0
    window_days: int = 0
    step_days: int = 0
    # Разбивка по фазам рынка: система строится ради работы на РАЗНЫХ фазах, и вопрос
    # «работает ли она в своей фазе» важнее, чем «обогнала ли биткоин вообще».
    phases: dict[str, PhaseStat] = Field(default_factory=dict)

    @property
    def profitable_pct(self) -> Decimal:
        return Decimal(self.profitable) * 100 / self.windows if self.windows else Decimal(0)

    @property
    def ahead_pct(self) -> Decimal | None:
        """None — сравнивать было не с чем ни в одном окне."""
        return Decimal(self.ahead) * 100 / self.compared if self.compared else None


class Metrics(_Model):
    """Единый словарь метрик (R34i). Ветко-специфичные — значение или NotApplicable."""

    n_trades: int
    sample: SampleStatus
    net_pnl: Decimal
    net_pnl_pct: Decimal
    ev_per_trade: Decimal | None
    ev_ci95: tuple[Decimal, Decimal] | None
    win_rate: Decimal | None
    profit_factor: Decimal | None
    payoff: Decimal | None
    max_dd_pct: Decimal
    dd_duration_days: Decimal
    sharpe: Decimal | None
    sortino: Decimal | None
    exposure_pct: Decimal
    costs_pct: Decimal | None
    costs: CostsBreakdown
    benchmark_pct: Decimal | None
    vs_benchmark: Decimal | None
    # Годовые: разница процентов за окно несопоставима между окнами разной длины и
    # ломается на длинной истории (BTC с 2011 дал +2 855 000%, и вычитание съедает всё).
    # Сравнение ведётся по этим полям; `vs_benchmark` остаётся как справка «сколько за окно».
    cagr_pct: Decimal | None = None
    benchmark_cagr_pct: Decimal | None = None
    vs_benchmark_cagr: Decimal | None = None
    # С чем сравнивали: btc_bh | btc_dca | cash | none. Без этого поля «обошла бенчмарк»
    # нечитаемо — у разных веток бенчмарк разный.
    benchmark_kind: str = ""
    paper_vs_live_gap: Decimal | NotApplicable
    copy_lag_cost: dict[str, Decimal] | NotApplicable
    brier: Decimal | NotApplicable
    resolution_return: Decimal | NotApplicable
    mint_success_rate: Decimal | NotApplicable
    mint_cost_failed: Decimal | NotApplicable
    capital: Decimal
    window_from: datetime
    window_to: datetime
    # Оценка по многим окнам, если её считали: одиночное окно — плохая опора, вердикт
    # получается свойством нарезки. Считается только когда остальные критерии пройдены,
    # поэтому у большинства снимков здесь пусто.
    stability: Stability | None = None
    # Стоп стратегии в симуляции (G04): без этих полей обрыв цифр на пробое читался бы
    # как «стратегия сама перестала торговать», а не «её остановили по правилу».
    stopped_at: datetime | None = None
    stop_rule: str = ""
    blocked_signals: int = 0
    # Сколько сделок пришлось на каждую причину: «стоп», «выход по каналу», «базис».
    # Без этого поведение стратегии объяснить нечем — только гадать по итоговым цифрам,
    # а именно на этом дважды и застряли: со стопом черепах и с выходами фандинг-арбитража.
    reasons: dict[str, int] = Field(default_factory=dict)
    # Дыры в данных, когда замер их допускает (портфель): сколько рядов и сколько баров.
    # Замер прошёл — но снимок обязан признавать, что данные были неполные.
    data_gaps: dict[str, int] = Field(default_factory=dict)


class Criterion(_Model):
    name: str
    value: Decimal | int | None
    limit: Decimal | int | None
    op: str
    passed: bool | None
    detail: str = ""


class ThresholdResult(_Model):
    status: Literal["passed", "failed", "insufficient"]
    criteria: list[Criterion]
    sample: SampleStatus

    def failed_names(self) -> list[str]:
        return [c.name for c in self.criteria if c.passed is False]


class FoldResult(_Model):
    is_from: datetime
    is_to: datetime
    oos_from: datetime
    oos_to: datetime
    in_sample: Metrics | None = None
    out_of_sample: Metrics | None = None


class Measurement(_Model):
    """Неизменяемый снимок замера (R11.5): параметры, хеш данных, версия кода, результат."""

    id: int | None
    strategy_id: str
    mode: MeasureMode
    window_from: datetime
    window_to: datetime
    data_hash: str
    code_version: str
    costs_version: str
    params: dict[str, Any] = Field(default_factory=dict)
    status: Literal["ok", "incomplete"]
    reason: str = ""
    metrics: Metrics | None = None
    threshold: ThresholdResult | None = None
    folds: list[FoldResult] = Field(default_factory=list)
    trades: list[ClosedTrade] = Field(default_factory=list)
    cached: bool = False
    created_at: datetime | None = None
