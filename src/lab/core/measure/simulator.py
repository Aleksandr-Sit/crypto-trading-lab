"""Событийный симулятор и бумажный движок (решения §3, §6, §10; истории 17–18).

Один движок исполнения на бэктест и бумагу — `PaperEngine`:
  submit(signal)  — принять сигнал; decided_at раньше уже известных данных → LookaheadError;
  on_bar(bar)     — исполнить ожидающие сигналы по этому бару, начислить фандинг, вернуть филлы.

Правила исполнения:
  market — по открытию бара ± проскальзывание из модели издержек (цена хуже мида);
  limit  — если бар коснулся цены; частично: не больше max_participation × объёма бара за бар;
  фандинг — для перпов раз в funding_interval_h по открытой позиции (лонг платит при rate > 0).
P&L сделки считается по референсным ценам (мид), проскальзывание — отдельная компонента издержек,
чтобы не считать его дважды.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

from lab.contracts import Branch, Candle, Costs, Event, Fill, OrderIntent, Signal, StopSpec
from lab.contracts.timeframes import parse_tf
from lab.core.costs import CostModel, Depth, default_model
from lab.core.measure.types import ClosedTrade, IncompleteData, LookaheadError

PERP_BRANCHES = {Branch.CEX_PERP, Branch.DEX_PERP}
ZERO_D = Decimal(0)


def check_continuity(candles: Sequence[Candle], tf: timedelta) -> None:
    """Свечи должны идти подряд с шагом tf — иначе замер `incomplete` (R11.6)."""
    if not candles:
        raise IncompleteData("нет свечей в окне")
    for prev, cur in zip(candles, candles[1:], strict=False):
        if cur.ts - prev.ts != tf:
            raise IncompleteData(
                f"разрыв данных между {prev.ts.isoformat()} и {cur.ts.isoformat()} "
                f"(ожидался шаг {tf})"
            )


@dataclass
class _Lot:
    side: str  # long | short
    qty: Decimal
    ref_price: Decimal
    opened_at: datetime
    costs: Costs


@dataclass
class _Pending:
    signal: Signal
    remaining: Decimal
    expires_at: datetime
    order_type: str
    limit_price: Decimal | None
    order_id: str = ""


@dataclass
class PaperEngine:
    venue: str
    instrument: str
    tf: str
    branch: Branch | str = Branch.CEX_SPOT
    costs: CostModel = field(default_factory=default_model)
    funding_rate: Decimal = Decimal("0.0001")
    max_participation: Decimal = Decimal("0.1")
    depth: Depth | None = None
    quote_asset: str = "USD"
    on_fill: Callable[[Fill, Signal, Costs, Decimal], None] | None = None
    # История ставок «время расчёта → ставка». Нет — начисляем по константе funding_rate,
    # и это честно видно в счётчиках ниже.
    funding_rates: Mapping[datetime, Decimal] | None = None
    # Платит ли этот инструмент фандинг. None — решает ветка (так и было раньше).
    # Явное значение нужно связкам «спот + перп»: обе ноги в перп-ветке, но платит одна,
    # и угадывать это по имени движок не должен — пусть говорит тот, кто его создаёт.
    is_perp: bool | None = None
    # Метрики позиционирования «момент → показатели». Отдаются стратегии событием
    # `positioning`: открытый интерес и потоки — данные другой природы, чем цена,
    # и единственные, что не дублируют премию за плечо (связь со ставкой −0.03).
    positioning: Mapping[datetime, Mapping[str, Decimal]] | None = None
    # Плечо этой ноги. None — залога нет вовсе (спот), ликвидация невозможна.
    # Залог считается ПО НОГЕ, а не по стратегии: на бирже спот и фьючерс — разные счета,
    # и шорт фьючерса ликвидируют, даже когда спот-нога того же хеджа в прибыли.
    leverage: Decimal | None = None
    # Поддерживающая маржа, % от номинала (Binance для BTC на малом плече — около 0.4–0.5%).
    maintenance_margin_pct: Decimal = Decimal("0.5")
    # Хранить ли КАЖДОЕ исполнение объектом. В бумаге и в тестах это удобно, а в замере
    # разорительно: сеточная стратегия на трёх инструментах и минутных барах исполняется
    # почти непрерывно, лимитки набираются частями, и за 400 суток набегает под гигабайт
    # объектов `Fill`, которых потом никто не читает. Замер получал SIGKILL от cgroup
    # молча, без единой строки вывода. Счётчик `fills_count` остаётся всегда.
    keep_fills: bool = True

    def __post_init__(self) -> None:
        self.step = parse_tf(self.tf)
        self.clock: datetime | None = None
        self.pending: list[_Pending] = []
        self.lots: list[_Lot] = []
        self.fills: list[Fill] = []
        self.fills_count = 0
        self.closed: list[ClosedTrade] = []
        # СЧЁТЧИК, а не список: у сеточных стратегий на минутках протухает по сигналу
        # почти на каждом баре — 576 тысяч объектов Signal за 400 суток, под гигабайт
        # памяти. Читалась от них всё равно только длина, и замер получал SIGKILL от
        # cgroup молча, без единой строки вывода.
        self.expired = 0
        self._ids = count(1)
        # Интервал расчёта фандинга принадлежит ИНСТРУМЕНТУ, а не площадке: у BTC и ETH
        # он восьмичасовой, у альтов Binance сплошь четырёхчасовой. Конфиг знает только
        # площадку, и альтам начислялась ровно половина выплат. Поэтому интервал берётся
        # из самой истории ставок, а конфиг остаётся запасным путём.
        self._funding_h = _interval_from(self.funding_rates) or (
            self.costs.funding_interval_h(self.venue) or 8
        )
        self.funding_from_history = 0  # сколько выплат взято из истории
        self.funding_missed = 0  # сколько посчитано по константе, потому что истории нет
        self._is_perp = (
            Branch(self.branch) in PERP_BRANCHES if self.is_perp is None else self.is_perp
        )

    # -- вход --------------------------------------------------------------------

    @property
    def position(self) -> Decimal:
        return sum((lot.qty if lot.side == "long" else -lot.qty for lot in self.lots), Decimal(0))

    def submit(self, signal: Signal) -> None:
        if self.clock is not None and signal.decided_at < self.clock:
            raise LookaheadError(
                f"сигнал решён {signal.decided_at.isoformat()}, а движок уже видел данные до "
                f"{self.clock.isoformat()} — заглядывание в будущее"
            )
        if signal.side not in ("buy", "sell"):
            raise ValueError(f"side {signal.side!r}: ожидается buy|sell")
        if signal.size <= 0:
            raise ValueError("size должен быть > 0")
        order_type = str(signal.meta.get("order_type", "market"))
        limit_price = signal.meta.get(
            "limit_price", signal.price_ref if order_type == "limit" else None
        )
        self.pending.append(
            _Pending(
                signal=signal,
                remaining=signal.size,
                expires_at=signal.decided_at + timedelta(seconds=signal.ttl_s),
                order_type=order_type,
                limit_price=None if limit_price is None else Decimal(str(limit_price)),
                order_id=f"paper-{next(self._ids)}",
            )
        )

    def on_bar(self, bar: Candle) -> list[Fill]:
        if self.clock is not None and bar.ts < self.clock:
            raise LookaheadError(f"бар {bar.ts.isoformat()} раньше часов движка {self.clock}")
        fills: list[Fill] = []
        still: list[_Pending] = []
        for p in self.pending:
            if p.signal.decided_at > bar.ts:
                still.append(p)
                continue
            if bar.ts >= p.expires_at and p.expires_at > p.signal.decided_at:
                self.expired += 1
                continue
            fill = self._execute(p, bar)
            if fill is not None:
                fills.append(fill)
            if p.remaining > 0:
                still.append(p)
        self.pending = still
        if self._is_perp:
            self._accrue_funding(bar)
        self.clock = bar.ts + self.step
        return fills

    # -- исполнение -----------------------------------------------------------------

    def _intent(self, p: _Pending, qty: Decimal, price: Decimal | None) -> OrderIntent:
        return OrderIntent(
            strategy_id=p.signal.strategy_id,
            venue=self.venue,
            instrument=self.instrument,
            side=p.signal.side,  # type: ignore[arg-type]
            qty=qty,
            price=price,
            order_type="limit" if p.order_type == "limit" else "market",
            mode="paper",
            signal_id=p.signal.inputs_hash,
            client_order_id=p.order_id,
        )

    def _execute(self, p: _Pending, bar: Candle) -> Fill | None:
        side = p.signal.side
        if p.order_type == "limit":
            price = p.limit_price
            if price is None:
                raise ValueError("лимитному сигналу нужна limit_price или price_ref")
            touched = bar.low <= price if side == "buy" else bar.high >= price
            if not touched:
                return None
            cap = bar.volume * self.max_participation
            qty = min(p.remaining, cap) if cap > 0 else p.remaining
            if qty <= 0:
                return None
            ref = price
            costs = self.costs.estimate(self.venue, self._intent(p, qty, price), depth=self.depth)
            fill_price = price
        else:
            qty = p.remaining
            ref = bar.open
            intent = self._intent(p, qty, ref)
            costs = self.costs.estimate(self.venue, intent, depth=self.depth)
            sign = 1 if side == "buy" else -1
            fill_price = ref + sign * costs.slippage / qty
        p.remaining -= qty
        fill = Fill(
            id=f"pf-{next(self._ids)}",
            order_id=p.order_id,
            price=fill_price,
            qty=qty,
            fee=costs.fee,
            fee_asset=self.quote_asset,
            ts=bar.ts,
        )
        self.fills_count += 1
        if self.keep_fills:
            self.fills.append(fill)
        self._apply(side, qty, ref, bar.ts, costs)
        if self.on_fill is not None:
            self.on_fill(fill, p.signal, costs, ref)
        return fill

    def _apply(self, side: str, qty: Decimal, ref: Decimal, ts: datetime, costs: Costs) -> None:
        opening = "long" if side == "buy" else "short"
        closing = "short" if side == "buy" else "long"
        remaining = qty
        total_qty = qty
        while remaining > 0 and self.lots and self.lots[0].side == closing:
            lot = self.lots[0]
            take = min(remaining, lot.qty)
            share_open = take / lot.qty  # доля от ОСТАТКА лота: издержки не теряются и не двоятся
            share_close = take / total_qty
            lot_costs = _scale(lot.costs, share_open)
            close_costs = _scale(costs, share_close)
            direction = 1 if lot.side == "long" else -1
            self.closed.append(
                ClosedTrade(
                    instrument=self.instrument,
                    side=lot.side,  # type: ignore[arg-type]
                    qty=take,
                    entry_price=lot.ref_price,
                    exit_price=ref,
                    opened_at=lot.opened_at,
                    closed_at=ts,
                    pnl_gross=(ref - lot.ref_price) * take * direction,
                    costs=_add(lot_costs, close_costs),
                )
            )
            lot.costs = _scale(lot.costs, 1 - share_open)
            lot.qty -= take
            remaining -= take
            if lot.qty <= 0:
                self.lots.pop(0)
        if remaining > 0:
            self.lots.append(
                _Lot(
                    side=opening,
                    qty=remaining,
                    ref_price=ref,
                    opened_at=ts,
                    costs=_scale(costs, remaining / total_qty),
                )
            )

    # -- маржа ------------------------------------------------------------------------

    def worst_price(self, bar: Candle) -> Decimal:
        """Цена бара, худшая для текущей позиции: шорту максимум, лонгу минимум.

        Биржа смотрит на цену непрерывно, и позиция, пережившая час «в среднем», могла
        не пережить его экстремум.
        """
        return bar.high if self.position < 0 else bar.low

    def margin_state(self, price: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        """Залог, нереализованный итог и требование поддержания при данной цене.

        Разложено на три части, потому что складывать их приходится по-разному: при
        изолированной марже каждая нога отвечает за себя, при кросс-марже все ноги
        счёта складываются в одну сумму — и тогда прибыль спота держит убыток фьючерса.
        """
        margin = ZERO_D
        unrealized = ZERO_D
        for lot in self.lots:
            direction = 1 if lot.side == "long" else -1
            unrealized += (price - lot.ref_price) * lot.qty * direction
            if self.leverage is not None and self.leverage > 0:
                margin += lot.qty * lot.ref_price / self.leverage
        keep = ZERO_D
        if self.leverage is not None and self.leverage > 0:
            keep = abs(self.position) * price * self.maintenance_margin_pct / 100
        return margin, unrealized, keep

    def margin_breach(self, bar: Candle) -> Decimal | None:
        """Цена, на которой ИЗОЛИРОВАННАЯ позиция была бы ликвидирована внутри бара.

        Модель простая и намеренно грубая — маржа по ноге, без страхового фонда,
        частичных ликвидаций и ступеней плеча: она отвечает на вопрос «дожил ли счёт»,
        а не «сколько именно списала бы биржа».
        """
        if self.leverage is None or self.leverage <= 0 or not self.lots:
            return None
        worst = self.worst_price(bar)
        margin, unrealized, keep = self.margin_state(worst)
        return worst if margin + unrealized <= keep else None

    def liquidate(self, price: Decimal, ts: datetime) -> Decimal:
        """Принудительно закрыть всё по цене ликвидации. Возврат — закрытый объём.

        Издержки считаются как у обычного рыночного закрытия. В реальности ликвидация
        дороже (штраф биржи и проскальзывание в неликвидный момент), так что оценка
        оптимистична — но она и нужна для ответа «дожил или нет», а не для копейки.
        """
        position = self.position
        if position == 0:
            return ZERO_D
        side = "buy" if position < 0 else "sell"
        qty = abs(position)
        intent = OrderIntent(
            strategy_id="liquidation",
            venue=self.venue,
            instrument=self.instrument,
            side=side,  # type: ignore[arg-type]
            qty=qty,
            price=price,
            order_type="market",
            mode="paper",
            # Ликвидацию инициирует БИРЖА, сигнала стратегии за ней нет — но поля
            # обязательные, и пустыми их оставлять нельзя: пусть в журнале будет видно,
            # что это принудительное закрытие, а не решение правил.
            signal_id="liquidation",
            client_order_id=f"liq-{next(self._ids)}",
        )
        costs = self.costs.estimate(self.venue, intent, depth=self.depth)
        self._apply(side, qty, price, ts, costs)
        return qty

    def positioning_at(self, bar: Candle) -> Mapping[str, Decimal] | None:
        """Метрики позиционирования на момент бара, если они собраны.

        Ключ — время бара: метрики сведены к суткам, а стратегия на дневных свечах,
        поэтому совпадение точное. Нет данных — None, и стратегия просто не получит
        события: молчание честнее выдуманного числа.
        """
        if self.positioning is None:
            return None
        return self.positioning.get(bar.ts)

    def funding_events(self, bar: Candle) -> list[tuple[datetime, Decimal]]:
        """Выплаты фандинга внутри бара: момент и ставка. Для спота — пусто.

        Отдаётся симулятору, чтобы он передал их стратегии: фандинг-арбитраж принимает
        решение именно по ставке, а из свечей её не видно.
        """
        if not self._is_perp:
            return []
        return [(moment, self._peek_rate(moment)) for moment in self._moments(bar)]

    def _moments(self, bar: Candle) -> list[datetime]:
        """Моменты расчёта фандинга внутри бара.

        Границы генерируются по интервалу, а не берутся из истории напрямую: так дыра
        в истории остаётся ВИДНОЙ — ставка на пропущенный момент считается по константе
        и попадает в счётчик `funding_missed`. Если брать только те моменты, что есть
        в истории, пропуски исчезают молча, а молчание здесь хуже неточности.
        """
        return _funding_moments(bar.ts, bar.ts + self.step, self._funding_h)

    def _peek_rate(self, moment: datetime) -> Decimal:
        """Ставка на момент без учёта в счётчиках: счётчики про НАЧИСЛЕНИЕ, а не про показ."""
        if self.funding_rates is None:
            return self.funding_rate
        rate = self.funding_rates.get(moment)
        return self.funding_rate if rate is None else rate

    def _accrue_funding(self, bar: Candle) -> None:
        """Начисление за каждую границу фандинга внутри бара [ts, ts+step).

        На дневных барах при интервале 8 ч границ три, на часовых — ноль или одна. Ставка
        берётся из истории (`funding_rates`), если она есть: у нейтральных стратегий весь
        доход именно в ней, и подставлять константу значит мерить выдуманное число. Нет
        истории на этот момент — остаётся `funding_rate` из параметров, и это видно
        по `funding_from_history`.
        """
        if not self.lots:
            return
        for moment in self._moments(bar):
            rate = self._rate_at(moment)
            if not rate:
                continue
            for lot in self.lots:
                sign = 1 if lot.side == "long" else -1
                pay = rate * lot.qty * bar.open * sign
                lot.costs = lot.costs.model_copy(update={"funding": lot.costs.funding + pay})

    def _rate_at(self, moment: datetime) -> Decimal:
        """Ставка на момент расчёта: из истории, иначе — константа из параметров."""
        if self.funding_rates is None:
            return self.funding_rate
        rate = self.funding_rates.get(moment)
        if rate is None:
            self.funding_missed += 1
            return self.funding_rate
        self.funding_from_history += 1
        return rate


def _interval_from(rates: Mapping[datetime, Decimal] | None) -> int | None:
    """Интервал расчёта фандинга по самой истории ставок, в часах.

    Берётся ТИПИЧНЫЙ промежуток между соседними выплатами, а не первый попавшийся:
    в истории бывают дыры (месяц не выгрузился), и один разрыв не должен решать за всех.
    Меньше трёх выплат — сказать нечего, отвечаем None и уходим на конфиг площадки.
    """
    if not rates or len(rates) < 3:
        return None
    moments = sorted(rates)
    gaps = sorted(
        int((b - a).total_seconds() // 3600)
        for a, b in zip(moments, moments[1:], strict=False)
    )
    typical = gaps[len(gaps) // 2]
    return typical if typical >= 1 else None


def _funding_moments(start: datetime, end: datetime, interval_h: int) -> list[datetime]:
    """Границы фандинга (часы, кратные интервалу от полуночи UTC) внутри [start, end).

    Раньше считалось только их КОЛИЧЕСТВО — при константной ставке этого хватало. С историей
    нужны сами моменты: ставка у каждой выплаты своя, и в этом весь смысл нейтральных стратегий.

    Сам интервал приходит из `_interval_from` — он свойство инструмента, а не площадки.
    """
    step = interval_h * 3600
    a, b = int(start.timestamp()), int(end.timestamp())
    first = ((a - 1) // step + 1) * step
    return [datetime.fromtimestamp(t, tz=UTC) for t in range(first, b, step) if t >= a]


def _scale(c: Costs, k: Decimal) -> Costs:
    return Costs(**{name: getattr(c, name) * k for name in Costs.model_fields})


def _add(a: Costs, b: Costs) -> Costs:
    return Costs(**{name: getattr(a, name) + getattr(b, name) for name in Costs.model_fields})


@dataclass(frozen=True)
class SimResult:
    trades: list[ClosedTrade]
    fills: list[Fill]
    open_position: Decimal
    expired_signals: int
    stopped_at: datetime | None = None
    stop_rule: str = ""
    blocked_signals: int = 0
    # Позиции по инструментам: у портфельной стратегии «одна открытая позиция» не значит
    # ничего — важно, что осталось на каждой ноге.
    positions: dict[str, Decimal] = field(default_factory=dict)
    # Сколько исполненных сигналов пришлось на каждую причину: «стоп», «выход по каналу»,
    # «базис». Без этого поведение стратегии объяснить нечем — только гадать по цифрам.
    reasons: dict[str, int] = field(default_factory=dict)
    # Пропущенные бары по инструментам, когда разрывы разрешены (портфель): сколько шагов
    # ряда не хватило. Пусто — данные сплошные.
    gaps: dict[str, int] = field(default_factory=dict)
    # Ликвидации: инструмент и момент. Пусто — счёт дожил до конца окна.
    liquidations: list[tuple[str, datetime]] = field(default_factory=list)
    # Переоценка по рынку (ревизия 12.09.2026). До неё просадка и итог считались ТОЛЬКО
    # по закрытым сделкам: позиция, просевшая на 40% и закрытая в +1%, просадки не давала
    # вовсе, а всё, что оставалось открытым на конец окна, из результата выпадало. Здесь
    # худшая просадка кривой «капитал + закрытое + незакрытое по цене бара» и незакрытый
    # итог на последнем баре.
    mtm_max_dd_pct: Decimal = ZERO_D
    unrealized_end: Decimal = ZERO_D
    # Средний занятый залог за окно. Ноль — стратегия без залога (спот) или ничего
    # не держала: тогда поправки на простаивающий капитал не будет вовсе.
    avg_margin: Decimal = ZERO_D


def _isolated_breach(bar: Candle, eng: PaperEngine) -> list[tuple[str, Decimal]]:
    """Изолированная маржа: нога отвечает за себя, и только за себя."""
    hit = eng.margin_breach(bar)
    return [(bar.instrument, hit)] if hit is not None else []


def _cross_breach(
    book: Mapping[str, PaperEngine],
    bar: Candle,
    eng: PaperEngine,
    last_price: Mapping[str, Decimal],
) -> list[tuple[str, Decimal]]:
    """Кросс-маржа: залог общий на счёт, и прибыль одной ноги держит убыток другой.

    Для кэш-энд-керри это и есть разница между «стратегия умерла» и «стратегия дожила»:
    08.05.2021 шорт квартального ETH потерял 103% своего залога, а спот-нога в тот же
    момент стоила на 114% дороже входа. При изолированной марже биржа не видит спот
    вовсе — счета разные; при кросс-марже видит, и ликвидации не происходит.

    Цены остальных ног берутся ПОСЛЕДНИЕ известные, а не «этого же часа»: бары приходят
    по одному, и цены всех инструментов одновременно у нас нет. Для часовых баров
    расхождение мало, но оно есть, и это допущение, а не точный расчёт.
    """
    prices = dict(last_price)
    prices[bar.instrument] = eng.worst_price(bar)
    equity = ZERO_D
    keep = ZERO_D
    leveraged = False
    for name, engine in book.items():
        price = prices.get(name)
        if price is None or not engine.lots:
            continue
        margin, unrealized, need = engine.margin_state(price)
        equity += margin + unrealized
        keep += need
        leveraged = leveraged or need > 0
    if not leveraged or equity > keep:
        return []
    # Счёт кончился: биржа закрывает ВСЁ, что на нём есть, а не одну ногу.
    return [(name, prices[name]) for name, engine in book.items() if engine.lots and name in prices]


class _StopTracker:
    """Следит за стопом стратегии по мере закрытия сделок — теми же формулами, что риск-ядро.

    Просадка: кривая по ЗАКРЫТЫМ сделкам, глубина от пика в % от капитала
    (`ops.portfolio._drawdown_pct`). Дневной: сумма `pnl_net` за последние сутки в % от него же
    (`LivePortfolio.strategy_stats`). Совпадение формул тут важнее краткости: разойдутся —
    и бэктест снова начнёт обещать не то, что сделает живая система.

    Считает НАКОПИТЕЛЬНО, а не пересчётом всей кривой на каждом баре: у сеточных стратегий
    тысячи сделок и сотни тысяч баров, и полный пересчёт превращал замер в часы работы
    процессора при простаивающей сети.
    """

    def __init__(self, stop: StopSpec | None, capital: Decimal) -> None:
        self.stop = stop
        self.capital = capital
        self.active = stop is not None and capital > 0
        self._equity = Decimal(0)
        self._peak = Decimal(0)
        self._worst = Decimal(0)
        self._day: deque[tuple[datetime, Decimal]] = deque()
        self._day_sum = Decimal(0)

    def breach(self, closed: Sequence[ClosedTrade], now: datetime) -> str:
        """`closed` — только НОВЫЕ сделки с прошлого бара.

        Раньше сюда приходил весь список, и счётчик сам отслеживал позицию в нём. С портфелем
        списков несколько (по движку на инструмент), общего порядка у них нет — поэтому кто
        именно новый, знает вызывающий, а счётчик просто складывает.
        """
        if not self.active:
            return ""
        if closed:
            # Пик и дно обновляются ОДИН раз на пачку: все эти сделки закрылись на одном
            # баре, то есть одновременно. Считая по одной, мы ловили провал между ногами
            # хеджа — у кэш-энд-керри так набегала «просадка» 20% при итоге связки в пару
            # сотен долларов, и на ней срабатывал стоп, обрывая замер на середине.
            self._equity += sum((t.pnl_net for t in closed), Decimal(0))
            self._peak = max(self._peak, self._equity)
            self._worst = min(self._worst, self._equity - self._peak)
        for trade in closed:
            if trade.closed_at is not None:
                self._day.append((trade.closed_at, trade.pnl_net))
                self._day_sum += trade.pnl_net

        stop = self.stop
        assert stop is not None  # active == stop is not None
        if stop.daily_pct is not None:
            edge = now - timedelta(days=1)
            while self._day and self._day[0][0] < edge:
                self._day_sum -= self._day.popleft()[1]
            if -(self._day_sum * 100 / self.capital) >= stop.daily_pct:
                return "strategy_stop_daily"
        if stop.max_dd_pct is not None and (
            (-self._worst * 100 / self.capital) >= stop.max_dd_pct
        ):
            return "strategy_stop_dd"
        return ""


def _opens_exposure(position: Decimal, signal: Signal) -> bool:
    """Увеличивает ли сигнал позицию: после пробоя стопа закрывать можно, открывать нет."""
    delta = signal.size if signal.side == "buy" else -signal.size
    return abs(position + delta) > abs(position)


def simulate(
    strategy,
    candles: Iterable[Candle],
    *,
    engine: PaperEngine | None = None,
    engines: Mapping[str, PaperEngine] | None = None,
    stop: StopSpec | None = None,
    capital: Decimal = Decimal(10_000),
    allow_gaps: bool = False,
    cross_margin: bool = False,
) -> SimResult:
    """Прогон стратегии по свечам: сначала исполняются ожидающие сигналы по бару,
    потом стратегия видит бар и решает — её сигналы исполнятся не раньше следующего бара.

    Инструментов может быть несколько (`engines` — движок на инструмент): кроссмоментум,
    базис и фандинг-арбитраж иначе не измерить, им нужны несколько рядов ОДНОВРЕМЕННО.
    Поток свечей приходит слитым по времени, сигнал уходит движку своего инструмента.
    Капитал общий: стоп стратегии считается по всем закрытым сделкам вместе, а не по каждой
    ноге отдельно — иначе связка «лонг спот + шорт перп» выглядела бы как две стратегии.

    Принимает поток, а не список: год минутных свечей — полмиллиона объектов и больше
    гигабайта памяти, а нужен всегда только текущий бар. Непрерывность проверяется по ходу
    и ОТДЕЛЬНО по каждому инструменту (то же правило, что в `check_continuity`).
    """
    book = dict(engines) if engines is not None else {}
    if engine is not None:
        book.setdefault(engine.instrument, engine)
    if not book:
        raise ValueError("нужен engine или engines")

    prev: dict[str, Candle] = {}
    seen = 0
    stopped_at: datetime | None = None
    stop_rule = ""
    blocked = 0
    tracker = _StopTracker(stop, capital)
    batch: list[ClosedTrade] = []
    liquidations: list[tuple[str, datetime]] = []
    last_price: dict[str, Decimal] = {}
    batch_ts: datetime | None = None
    consumed = dict.fromkeys(book, 0)

    # Переоценка по рынку. Пересчитывается только у движка, чей бар пришёл (и у тех, кого
    # ликвидировали): обход всех движков на каждом баре при вселенной в 647 рядов — это
    # миллиард операций на замер.
    realized = ZERO_D
    realized_idx = dict.fromkeys(book, 0)
    key_of = {id(e): name for name, e in book.items()}
    unreal: dict[str, Decimal] = dict.fromkeys(book, ZERO_D)
    unreal_total = ZERO_D
    mtm_peak = capital
    mtm_max_dd = ZERO_D
    # Занятость капитала: сколько залога держится в среднем по времени. Нужна, чтобы
    # сравнение с бенчмарком было честным в ОБЕ стороны. Бенчмарк «кэш» уже считается
    # по безрисковой ставке, а не по нулю; но простаивающие деньги самой стратегии
    # до сих пор приносили ноль, и стратегия, занимающая десятую часть счёта,
    # сравнивалась с депозитом на весь счёт.
    margin: dict[str, Decimal] = dict.fromkeys(book, ZERO_D)
    margin_total = ZERO_D
    margin_sum = ZERO_D
    margin_obs = 0

    def _refresh(name: str, price: Decimal) -> None:
        nonlocal realized, unreal_total, margin_total
        e = book[name]
        fresh_closed = e.closed[realized_idx[name] :]
        realized_idx[name] = len(e.closed)
        realized += sum((t.pnl_net for t in fresh_closed), ZERO_D)
        now_margin, now_u, _ = e.margin_state(price)
        unreal_total += now_u - unreal[name]
        unreal[name] = now_u
        margin_total += now_margin - margin[name]
        margin[name] = now_margin

    # Причины считаем по ИСПОЛНЕННЫМ сигналам: намерение и сделка — разные вещи, лимитка
    # могла не сработать, а сигнал протухнуть по ttl.
    gaps: dict[str, int] = {}
    reasons: dict[str, int] = {}

    def _count(_fill: Fill, signal: Signal, _costs: Costs, _ref: Decimal) -> None:
        label = str(signal.meta.get("reason") or signal.meta.get("kind") or "без причины")
        reasons[label] = reasons.get(label, 0) + 1

    for engine_ in book.values():
        if engine_.on_fill is None:
            engine_.on_fill = _count

    single = next(iter(book.values())) if len(book) == 1 else None
    for bar in candles:  # noqa: PLR1702
        # С одним движком имя в баре не проверяем: поток и есть его инструмент, а звать его
        # ряд может иначе (синтетика в тестах, переименованная пара). С несколькими движками
        # так нельзя — там имя решает, кому уйдёт сделка, и расхождение с манифестом это
        # дефект данных, а не мелочь.
        eng = single if single is not None else book.get(bar.instrument)
        if eng is None:
            raise IncompleteData(
                f"свеча инструмента {bar.instrument}, которого нет в манифесте стратегии"
            )
        before = prev.get(bar.instrument)
        if before is not None and bar.ts - before.ts != eng.step:
            if not allow_gaps:
                raise IncompleteData(
                    f"разрыв данных {bar.instrument} между {before.ts.isoformat()} и "
                    f"{bar.ts.isoformat()} (ожидался шаг {eng.step})"
                )
            # Портфель из сотен рядов: остановка торгов одним альтом — обычное дело
            # (COCOS/USDT, январь 2021), и ронять из-за неё замер всего среза неправильно.
            # Пропуск ЧЕСТЕН для кросс-секционных правил: в эти дни пара действительно не
            # торговалась, и правило обязано её не выбирать. Но молча это делать нельзя —
            # пропуски считаются и уезжают в снимок, иначе плохие данные так и не всплывут.
            missing = int((bar.ts - before.ts) / eng.step) - 1
            gaps[bar.instrument] = gaps.get(bar.instrument, 0) + max(1, missing)
        prev[bar.instrument] = bar
        seen += 1
        eng.on_bar(bar)

        # Ликвидация: биржа закрывает позицию раньше, чем стратегия успевает что-то решить.
        # Считается ДО стопа и до решения стратегии — так это и происходит в бою.
        last_price[bar.instrument] = bar.close
        killed = (
            _cross_breach(book, bar, eng, last_price)
            if cross_margin
            else _isolated_breach(bar, eng)
        )
        for name, price in killed:
            book[name].liquidate(price, bar.ts)
            liquidations.append((name, bar.ts))
        # Ключ — движка, а не бара: при одном движке ряд может зваться иначе (синтетика
        # в тестах, переименованная пара), и по имени бара движка в книге нет.
        own = key_of[id(eng)]
        _refresh(own, bar.close)
        for name, price in killed:
            if name != own:
                _refresh(name, last_price.get(name, price))
        margin_sum += margin_total
        margin_obs += 1
        equity = capital + realized + unreal_total
        if equity >= mtm_peak:
            mtm_peak = equity
        elif mtm_peak > 0:
            mtm_max_dd = max(mtm_max_dd, (mtm_peak - equity) / mtm_peak * 100)
        if killed and stopped_at is None:
            # Хедж после ликвидации сломан, и продолжать по правилам нельзя: ведём себя
            # как при пробое стопа — закрытия проходят, открытия нет.
            stopped_at, stop_rule = bar.ts, "ликвидация"

        if stopped_at is None:
            # Стоп смотрит на сделки, закрытые ОДНИМ МОМЕНТОМ, а не одним баром: ноги
            # связки приходят разными барами того же часа, и, отдавая их стопу по одной,
            # мы показывали ему провал между ногами хеджа. Поэтому пачка копится, пока
            # время не сдвинулось, и уходит целиком на границе часа (и в конце потока).
            fresh: list[ClosedTrade] = []
            for name, e in book.items():
                fresh.extend(e.closed[consumed[name] :])
                consumed[name] = len(e.closed)
            if batch_ts is not None and bar.ts != batch_ts:
                stop_rule = tracker.breach(batch, batch_ts)
                batch = []
                if stop_rule:
                    stopped_at = batch_ts
            batch_ts = bar.ts
            batch.extend(fresh)

        # Ставку фандинга из свечей не видно, а фандинг-арбитраж решает именно по ней.
        # Отдаём её СОБЫТИЕМ — тем же контрактом, которым стратегия слушает живые фиды.
        decisions = list(strategy.on_bar(bar))
        on_event = getattr(strategy, "on_event", None)
        if on_event is not None:
            for moment, rate in eng.funding_events(bar):
                decisions.extend(
                    on_event(
                        Event(
                            kind="funding",
                            ts=moment,
                            payload={"instrument": bar.instrument, "rate": rate},
                        )
                    )
                )
            # Метрики позиционирования — тем же контрактом, что и ставки: стратегия
            # не ходит в хранилище сама, ей приносят. Нет данных за этот момент —
            # события нет вовсе, и правило честно молчит.
            metrics = eng.positioning_at(bar)
            if metrics:
                decisions.extend(
                    on_event(
                        Event(
                            kind="positioning",
                            ts=bar.ts,
                            payload={"instrument": bar.instrument, **dict(metrics)},
                        )
                    )
                )

        for signal in decisions:
            # Сигнал уходит движку СВОЕГО инструмента; при одном движке — ему же.
            target = single or book.get(signal.instrument, eng)
            # После пробоя стратегия ведёт себя как `degraded` у живого риск-ядра:
            # закрывающие сигналы проходят, открывающие — нет. Сама стратегия об этом
            # не знает и продолжает считать: так же, как в бою.
            if stopped_at is not None and _opens_exposure(target.position, signal):
                blocked += 1
                continue
            target.submit(signal)

    if seen == 0:
        raise IncompleteData("нет свечей в окне")
    # Последний момент потока: его пачка иначе осталась бы непроверенной, и пробой стопа
    # на самых последних сделках замер бы не заметил.
    if stopped_at is None and batch and batch_ts is not None:
        stop_rule = tracker.breach(batch, batch_ts)
        if stop_rule:
            stopped_at = batch_ts
    trades: list[ClosedTrade] = []
    fills: list[Fill] = []
    expired = 0
    position = Decimal(0)
    for e in book.values():
        trades.extend(e.closed)
        fills.extend(e.fills)
        expired += e.expired
        position += e.position
    trades.sort(key=lambda t: t.closed_at)
    return SimResult(
        trades=trades,
        fills=fills,
        open_position=position,
        expired_signals=expired,
        stopped_at=stopped_at,
        stop_rule=stop_rule,
        blocked_signals=blocked,
        positions={name: e.position for name, e in book.items()},
        reasons=reasons,
        liquidations=liquidations,
        gaps=gaps,
        mtm_max_dd_pct=mtm_max_dd,
        unrealized_end=unreal_total,
        avg_margin=(margin_sum / margin_obs) if margin_obs else ZERO_D,
    )
