"""Замер стратегии на НЕСКОЛЬКИХ инструментах сразу (08.09.2026).

До этого движок вёл один инструмент, и три стратегии каталога были неизмеримы в принципе:
фандинг-арбитраж и базис держат две ноги одновременно (перп и спот), кроссмоментум —
корзину. Здесь проверяется, что ряды сливаются по времени, сигнал уходит движку своего
инструмента, а капитал и стоп остаются общими на всю стратегию.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import Branch, Candle, Costs, Signal, StopSpec, StrategyManifest
from lab.core.measure import PaperEngine
from lab.core.measure.simulator import simulate
from lab.core.measure.types import ClosedTrade, IncompleteData

HOUR = timedelta(hours=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
A, B = "BTC/USDT:USDT", "ETH/USDT:USDT"


def _bar(instrument: str, i: int, price: Decimal) -> Candle:
    return Candle(
        instrument=instrument,
        tf="1h",
        ts=T0 + HOUR * i,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(1000),
    )


def _merged(n: int, price_a=Decimal(100), price_b=Decimal(50)) -> list[Candle]:
    """Два ряда, слитые по времени — так их отдаёт `_merged` в runner."""
    out: list[Candle] = []
    for i in range(n):
        out.append(_bar(A, i, price_a))
        out.append(_bar(B, i, price_b))
    return out


def _engines() -> dict[str, PaperEngine]:
    return {
        name: PaperEngine(venue="binance", instrument=name, tf="1h", branch=Branch.CEX_PERP)
        for name in (A, B)
    }


class TwoLegs:
    """Открывает лонг по первой ноге и шорт по второй — как связка спот+перп."""

    def __init__(self) -> None:
        self.manifest = StrategyManifest(
            slug="two-legs",
            branch=Branch.CEX_PERP,
            venue="binance",
            source_kind="test",
            instruments=[A, B],
            timeframe="1h",
            stop=StopSpec(max_dd_pct=Decimal(50)),
        )
        self.opened = False

    def on_bar(self, bar: Candle) -> list[Signal]:
        if self.opened or bar.instrument != B:
            return []
        self.opened = True
        # Обе ноги решаются на одном баре: это и есть смысл связки.
        return [
            Signal(
                strategy_id="x",
                decided_at=bar.ts + HOUR,
                instrument=instrument,
                side=side,
                size=Decimal(1),
                price_ref=bar.close,
                inputs_hash=f"h-{instrument}",
                ttl_s=7200,
            )
            for instrument, side in ((A, "buy"), (B, "sell"))
        ]


def test_signals_go_to_the_engine_of_their_instrument():
    strategy = TwoLegs()
    engines = _engines()

    result = simulate(strategy, _merged(5), engines=engines, capital=Decimal(10_000))

    assert engines[A].position > 0, "первая нога должна быть в лонге"
    assert engines[B].position < 0, "вторая — в шорте"
    assert result.positions[A] > 0 and result.positions[B] < 0


def test_single_instrument_call_still_works():
    """Старый вызов с одним движком — прежнее поведение, ничего не сломано."""
    engine = PaperEngine(venue="binance", instrument=A, tf="1h", branch=Branch.CEX_PERP)

    class Idle:
        manifest = None

        def on_bar(self, bar: Candle) -> list[Signal]:
            return []

    result = simulate(Idle(), [_bar(A, i, Decimal(100)) for i in range(3)], engine=engine)
    assert result.trades == []


def test_unknown_instrument_in_a_portfolio_is_an_error_not_silence():
    """У портфеля имя решает, кому уйдёт сделка: чужой ряд — дефект данных, а не мелочь."""
    engines = _engines()
    stray = [_bar("SOL/USDT:USDT", 0, Decimal(10))]

    with pytest.raises(IncompleteData, match="нет в манифесте"):
        simulate(TwoLegs(), stray, engines=engines)


def test_single_engine_does_not_check_the_name():
    """С одним движком поток и ЕСТЬ его инструмент, как бы ряд ни назывался.

    Так приходят синтетические ряды в тестах и переименованные пары; проверять там имя
    значило бы ломать замер на ровном месте — распределять сделки всё равно некуда.
    """
    engine = PaperEngine(venue="binance", instrument=A, tf="1h", branch=Branch.CEX_PERP)

    class Idle:
        manifest = None

        def on_bar(self, bar: Candle) -> list[Signal]:
            return []

    result = simulate(Idle(), [_bar("SYN/USD", i, Decimal(100)) for i in range(3)], engine=engine)
    assert result.trades == []


def test_gap_is_checked_per_instrument():
    """Разрыв в одном ряду не должен маскироваться чередованием инструментов."""
    bars = [_bar(A, 0, Decimal(100)), _bar(B, 0, Decimal(50)), _bar(A, 5, Decimal(100))]

    with pytest.raises(IncompleteData, match="разрыв данных"):
        simulate(TwoLegs(), bars, engines=_engines())


def test_hedged_legs_do_not_create_a_phantom_drawdown():
    """Ноги, закрытые одним моментом, — один шаг кривой, а не два.

    У хеджа одна нога всегда в минусе, другая в плюсе. Считая их по очереди, кривая
    проваливается на величину убыточной ноги и тут же восстанавливается — просадки такой
    не было ни секунды. У кэш-энд-керри так набегало 20% при итоге связки в пару сотен
    долларов, и на этой выдуманной просадке срабатывал стоп, обрывая замер на середине.
    """
    from lab.core.measure.metrics import _drawdown

    opened = T0
    closed = T0 + HOUR
    legs = [
        ClosedTrade(
            instrument=name,
            side=side,
            qty=Decimal(1),
            entry_price=Decimal(100),
            exit_price=Decimal(100),
            opened_at=opened,
            closed_at=closed,
            pnl_gross=pnl,
            costs=Costs(),
        )
        for name, side, pnl in ((A, "long", Decimal(-1400)), (B, "short", Decimal(1500)))
    ]

    max_dd, _ = _drawdown(legs, Decimal(10_000))

    assert max_dd == 0, "связка закрылась в плюс — просадки нет"


def test_stop_sees_legs_of_one_moment_together():
    """Стопу ноги связки приходят разными БАРАМИ одного часа — считать их надо вместе.

    Метрика просадки группирует по времени закрытия и провала не видит, а стоп получал
    сделки по мере обработки баров: сначала убыточную ногу, потом прибыльную. На этом
    расхождении замер кэш-энд-керри печатал «просадка 0%» и «стоп сработал по просадке»
    в одном и том же снимке.
    """
    strategy = TwoLegs()
    # Стоп сработал бы на любой ноге по отдельности (порог 1%), но связка в плюсе.
    strategy.manifest = strategy.manifest.model_copy(
        update={"stop": StopSpec(max_dd_pct=Decimal(1))}
    )
    engines = _engines()
    bars: list[Candle] = []
    for i in range(6):
        bars.append(_bar(A, i, Decimal(100) + Decimal(i) * 10))  # лонг растёт
        bars.append(_bar(B, i, Decimal(50) + Decimal(i) * 5))  # шорт против нас

    result = simulate(
        strategy, bars, engines=engines, stop=strategy.manifest.stop, capital=Decimal(10_000)
    )

    assert result.stopped_at is None, "связка в плюсе — останавливать нечего"


def test_allowed_gap_is_counted_not_hidden():
    """На портфеле дыра в одном ряду не роняет замер, но обязана попасть в результат.

    Вселенная кросс-моментума — сотни пар, и остановка торгов хотя бы одной (COCOS/USDT,
    январь 2021) есть всегда: падать из-за неё замером всего среза неправильно. Но и
    молчать нельзя — иначе неполные данные так и не всплывут.
    """
    bars = [_bar(A, 0, Decimal(100)), _bar(B, 0, Decimal(50)), _bar(A, 5, Decimal(100))]

    result = simulate(TwoLegs(), bars, engines=_engines(), allow_gaps=True)

    assert result.gaps == {A: 4}, "четыре пропущенных бара между 0 и 5"


def test_chunk_shrinks_with_the_number_of_instruments():
    """Бюджет памяти общий на замер, а не на инструмент.

    `heapq.merge` заводит все потоки сразу, и каждый тянет первый кусок: 647 рядов
    вселенной кросс-моментума по 2 000 баров — это 1.3 млн свечей и SIGKILL по `mem_limit`
    (код 137, вывода нет вовсе). Проверяется именно суммарный размер, а не формула.
    """
    from lab.core.measure.runner import CHUNK_BARS, chunk_for

    assert chunk_for(1) == CHUNK_BARS
    assert chunk_for(50) * 50 <= CHUNK_BARS, "полсотни рядов должны укладываться в бюджет"
    assert chunk_for(647) * 647 <= 200_000, "вселенная альтов не должна съедать гигабайт"
    assert chunk_for(10_000) >= 100, "но и по одной свече за запрос ходить незачем"


def test_reasons_are_counted_by_executed_signals():
    """Почему стратегия торговала — должно доезжать до результата, а не теряться в хеше.

    `inputs` уходит в `inputs_hash`, а хеш обратно не прочитаешь: именно поэтому нельзя было
    объяснить ни ранний стоп черепах, ни частые выходы фандинг-арбитража. Причина считается
    по ИСПОЛНЕННЫМ сигналам: намерение и сделка — разные вещи.
    """
    from lab.strategies.base import Strategy

    class Tagged(Strategy):
        def reset(self) -> None:
            self.sent = False

        def on_bar(self, bar: Candle) -> list[Signal]:
            if self.sent or bar.instrument != A:
                return []
            self.sent = True
            return [self.signal(bar, "buy", Decimal(1), inputs={"kind": "breakout"})]

    manifest = StrategyManifest(
        slug="tagged",
        branch=Branch.CEX_PERP,
        venue="binance",
        source_kind="test",
        instruments=[A],
        timeframe="1h",
        stop=StopSpec(max_dd_pct=Decimal(50)),
    )
    engine = PaperEngine(venue="binance", instrument=A, tf="1h", branch=Branch.CEX_PERP)

    result = simulate(Tagged(manifest), [_bar(A, i, Decimal(100)) for i in range(4)], engine=engine)

    assert result.reasons == {"breakout": 1}


def test_expired_signals_are_counted_not_stored():
    """Протухшие сигналы считаются, а не копятся объектами.

    У сеточной стратегии на минутках лимитка протухает почти на каждом баре: за 400 суток
    это 576 тысяч объектов Signal, под гигабайт памяти. Читалась от них только длина,
    а замер получал SIGKILL от cgroup — молча, без единой строки вывода, что снаружи
    неотличимо от «замер ничего не сказал».
    """
    engine = PaperEngine(venue="binance", instrument=A, tf="1h", branch=Branch.CEX_PERP)
    for i in range(5):
        engine.submit(
            Signal(
                strategy_id="x",
                decided_at=T0 + HOUR * i,
                instrument=A,
                side="buy",
                size=Decimal(1),
                price_ref=Decimal(1),  # лимитка глубоко под рынком — не исполнится
                inputs_hash=f"h{i}",
                ttl_s=1,
                meta={"order_type": "limit", "limit_price": "1"},
            )
        )

    for i in range(6):
        engine.on_bar(_bar(A, i, Decimal(100)))

    assert engine.expired == 5
    assert isinstance(engine.expired, int), "список сигналов хранить незачем — нужен счётчик"


def test_fills_are_not_stored_when_the_engine_is_told_not_to():
    """Замер хранит только счётчик исполнений — сами объекты ему не нужны.

    Сеточная стратегия на трёх инструментах и минутных барах исполняется почти непрерывно,
    лимитки набираются частями: за 400 суток набегает под гигабайт объектов `Fill`,
    которых потом никто не читает. Замер получал SIGKILL от cgroup молча, без единой
    строки вывода — снаружи это неотличимо от «замер ничего не сказал».
    """
    kept = PaperEngine(venue="binance", instrument=A, tf="1h", branch=Branch.CEX_PERP)
    lean = PaperEngine(
        venue="binance", instrument=A, tf="1h", branch=Branch.CEX_PERP, keep_fills=False
    )

    for engine in (kept, lean):
        engine.submit(
            Signal(
                strategy_id="x",
                decided_at=T0,
                instrument=A,
                side="buy",
                size=Decimal(1),
                price_ref=Decimal(100),
                inputs_hash="h",
                ttl_s=7200,
            )
        )
        engine.on_bar(_bar(A, 0, Decimal(100)))

    assert kept.fills_count == lean.fills_count == 1, "считаются одинаково"
    assert len(kept.fills) == 1, "по умолчанию исполнения хранятся — так удобно в бумаге"
    assert lean.fills == [], "в замере — только счётчик"
    assert kept.position == lean.position, "на позицию режим не влияет"
