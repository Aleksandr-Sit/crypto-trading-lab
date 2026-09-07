"""Pifagor Trade (Дима, Дмитрий Енин) — порты индикаторов.

[ИНДИКАТОР v0 — по публичным описаниям; v1 — по скрипту пользователя]

Источник описаний: https://www.pifagor.trade/indicators.html (19 индикаторов, дословные описания
разобраны в `docs/research/indicators/pifagor.md`). Pine-скриптов публично нет (профиль TradingView
`Pifagor_trade` — 0 скриптов), поэтому кодируются только два с явной базой (MFI, SMA); остальные —
заглушки `[ИНДИКАТОР — нужен скрипт]` с тем, что известно.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np

from lab.strategies.indicators.base import LABEL_V0, BaseIndicator, StubIndicator
from lab.strategies.indicators.frame import Frame
from lab.strategies.indicators.ta import atr, cross_down, cross_up, mfi, sma

SITE = "https://www.pifagor.trade/indicators.html"


class PifagorMfi(BaseIndicator):
    """«MFI Pifagor — версия MFI с авторскими доработками» (сайт). Доработки не раскрыты —
    v0 = стандартный Money Flow Index с порогами. Колонки: `mfi`, `signal_buy` (выход из зоны
    перепроданности снизу вверх), `signal_sell` (MFI выше overbought)."""

    name: ClassVar[str] = "mfi"
    source: ClassVar[str] = SITE
    label: ClassVar[str] = LABEL_V0

    def __init__(self, period: int = 14, oversold: float = 20, overbought: float = 80) -> None:
        self.period, self.oversold, self.overbought = period, oversold, overbought

    def compute(self, frame: Frame) -> Frame:
        m = mfi(frame["high"], frame["low"], frame["close"], frame["volume"], self.period)
        return frame.with_columns(
            mfi=m,
            signal_buy=cross_up(m, self.oversold),
            signal_sell=(np.nan_to_num(m) > self.overbought).astype(float),
        )


class ForeverSma(BaseIndicator):
    """«Forever SMA — глобальный индикатор для торговли биткоином, долгосрочный взгляд на тренд»
    (сайт). Период неизвестен — v0: цена относительно SMA(period) с буфером против пилы.
    Колонки: `sma`, `above` (1/0), `signal_buy`/`signal_sell` — пересечения с буфером."""

    name: ClassVar[str] = "forever-sma"
    source: ClassVar[str] = SITE
    label: ClassVar[str] = LABEL_V0

    def __init__(
        self, period: int = 200, entry_buffer_pct: float = 1.0, exit_buffer_pct: float = 1.0
    ) -> None:
        self.period = period
        self.entry_buffer, self.exit_buffer = entry_buffer_pct / 100, exit_buffer_pct / 100

    def compute(self, frame: Frame) -> Frame:
        s = sma(frame["close"], self.period)
        close = frame["close"]
        above = np.where(np.isnan(s), np.nan, (close > s * (1 + self.entry_buffer)).astype(float))
        below = np.where(np.isnan(s), np.nan, (close < s * (1 - self.exit_buffer)).astype(float))
        return frame.with_columns(
            sma=s,
            above=above,
            signal_buy=cross_up(np.nan_to_num(above), 0.5),
            signal_sell=cross_up(np.nan_to_num(below), 0.5),
        )


class AtrBottom(BaseIndicator):
    """«Дно по ATR — поиск потенциального дна в средне- и долгосрочной перспективе» (сайт).
    v0: зона дна = `close < SMA(n) − k·ATR(n)`. n, k — параметры замера, не знание о формуле."""

    name: ClassVar[str] = "atr-bottom"
    source: ClassVar[str] = SITE
    label: ClassVar[str] = LABEL_V0

    def __init__(self, period: int = 50, k: float = 2.0) -> None:
        self.period, self.k = period, k

    def compute(self, frame: Frame) -> Frame:
        s = sma(frame["close"], self.period)
        a = atr(frame["high"], frame["low"], frame["close"], self.period)
        zone = frame["close"] < s - self.k * a
        return frame.with_columns(
            atr_bottom_line=s - self.k * a,
            signal_bottom=zone.astype(float),
            signal_exit=cross_down(zone.astype(float), 0.5),
        )


# -- заглушки: название и описание есть, формулы нет -----------------------------------------


def _stub(cls_name: str, name: str, known: str) -> type[StubIndicator]:
    return type(cls_name, (StubIndicator,), {"name": name, "source": SITE, "what_is_known": known})


Trader01 = _stub(
    "Trader01", "trader-01", "«зелёные волны — зона покупки, красные — выхода»; формулы нет"
)
DcaPifagor = _stub(
    "DcaPifagor", "dca-pifagor-3.1", "композит 15 on-chain метрик, список не раскрыт"
)
BtcMood = _stub("BtcMood", "btc-mood", "настроение толпы, применять к BLX 1d; формулы нет")
MoneyWaterfall = _stub(
    "MoneyWaterfall", "money-waterfall", "покупки/продажи крупных игроков (on-chain)"
)
Radar4h = _stub("Radar4h", "radar-4h", "импульс альтов на 4h; формулы нет")
AltsStrategy = _stub(
    "AltsStrategy", "alts-strategy-3.6", "готовая стратегия с входами/выходами, правила не раскрыты"
)
Opportunity = _stub("Opportunity", "opportunity", "справедливая стоимость и зоны крупного игрока")
BottomLine = _stub("BottomLine", "bottom-line", "конец медвежьего рынка BTC; формулы нет")
BigGuy = _stub("BigGuy", "big-guy", "активность крупных игроков; формулы нет")
AltsMood = _stub("AltsMood", "altsmood", "настроение альтов; формулы нет")
BuyMore = _stub("BuyMore", "buy-more", "зоны докупки в тренде; формулы нет")
WarmBuy = _stub("WarmBuy", "warm-buy", "снижение давления продавцов; формулы нет")
HeroEthBtc = _stub("HeroEthBtc", "hero-eth-btc", "сила альт-сезона ETH/BTC, 12h/1d; формулы нет")
BestEthBtc = _stub("BestEthBtc", "best-eth-btc", "ротация BTC/ETH, 12h/4h; формулы нет")
DivBtcUsdt = _stub("DivBtcUsdt", "div-btc-usdt", "дивергенции; какой осциллятор — неизвестно")
PifagorHistogram = _stub("PifagorHistogram", "pifagor-histogram", "авторская гистограмма импульса")

INDICATORS: dict[str, type[BaseIndicator]] = {
    c.name: c
    for c in (
        PifagorMfi,
        ForeverSma,
        AtrBottom,
        Trader01,
        DcaPifagor,
        BtcMood,
        MoneyWaterfall,
        Radar4h,
        AltsStrategy,
        Opportunity,
        BottomLine,
        BigGuy,
        AltsMood,
        BuyMore,
        WarmBuy,
        HeroEthBtc,
        BestEthBtc,
        DivBtcUsdt,
        PifagorHistogram,
    )
}


def by_name(name: str, **kwargs) -> BaseIndicator:
    return INDICATORS[name](**kwargs)


__all__ = ["INDICATORS", "AtrBottom", "ForeverSma", "PifagorMfi", "Trader01", "by_name"]
