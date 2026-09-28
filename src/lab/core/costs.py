"""core.costs — модели издержек по площадкам (решение §4).

Выставляет: `estimate(venue, intent, book|pool) -> Costs`, `actual(fill, ...) -> Costs`,
`fee_in_quote`, `base_moved`, `is_spot`, `UnknownFeeAsset`.
Прячет: тарифы (config/costs.yaml) и формулы проскальзывания.

Все суммы — Decimal в валюте котировки (USD/USDT/USDC считаются равными валюте учёта).
Версия модели (`CostModel.version`) = `costs-v<version>@<sha256 конфига>` — идёт в снимок замера.

Комиссия филла приходит в той монете, в которой её взяла биржа. На споте Binance и Bybit
берут комиссию покупки в ПОЛУЧАЕМОЙ монете (купил 0.01 BTC — на счёте 0.00999), продажи —
в котировке; Binance со включённой оплатой BNB — в BNB. Отсюда два правила (пробел 4
`docs/research/allocator-2026-09-27.md`): `fee_in_quote` — комиссия в валюте котировки,
`base_moved` — сколько монеты на самом деле пришло на счёт или ушло с него.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from lab.config import CONFIG_DIR, load_config
from lab.contracts import Book, Costs, Fill, OrderIntent, Side

_BPS = Decimal(10_000)
_QUOTE_ASSETS = {"USD", "USDT", "USDC", "BUSD", "FDUSD", "DAI"}


class UnknownFeeAsset(ValueError):
    """Комиссия в третьей монете (не котировка и не база инструмента): её цены на момент
    филла нет, а угадывать число нельзя — издержки передаёт тот, кто знает цену."""


def _pair(instrument: str) -> tuple[str, str] | None:
    """База и котировка по имени ccxt: `BTC/USDT`, `BTC/USDT:USDT`, `BTC/USDT:USDT-260925`.
    Имя не по ccxt (NFT `коллекция:токен`, адрес токена, id рынка) — None."""
    if "/" not in instrument:
        return None
    base, rest = instrument.split("/", 1)
    return base.upper(), rest.split(":", 1)[0].upper()


def is_spot(instrument: str) -> bool:
    """Спотовая пара по имени ccxt: есть `/`, нет расчётной валюты через `:`. Продажа на ней
    не открывает шорт — продать можно только то, что лежит на счёте. Имена не по ccxt сюда
    не попадают: NFT называются `коллекция:токен`, и двоеточие там — не перп."""
    return "/" in instrument and ":" not in instrument


def is_perpetual(instrument: str) -> bool:
    """Бессрочный контракт по имени ccxt: `BASE/QUOTE:SETTLE` без даты экспирации. У срочного
    (`BTC/USDT:USDT-260925`) и опциона после расчётной валюты стоит дата, фандинга у них нет —
    запрос ставок по ним давал бы ошибку каждую ночь. NFT `коллекция:токен` без `/` — не перп."""
    if "/" not in instrument or ":" not in instrument:
        return False
    return "-" not in instrument.split(":", 1)[1]


def fee_in_quote(fill: Fill, instrument: str) -> Decimal | None:
    """Комиссия филла в валюте котировки — той, в которой журнал считает P&L.

    Котировка и доллары — как есть; базовая монета — по цене филла; третья монета — None.
    У имени не по ccxt базу не определить: такие исполнители (DEX, NFT, Polymarket,
    Robinhood) отдают комиссию в своей котировке, она берётся как есть."""
    pair = _pair(instrument)
    if pair is None:
        return fill.fee
    base, quote = pair
    asset = fill.fee_asset.upper()
    if asset == quote or asset in _QUOTE_ASSETS:
        return fill.fee
    if asset == base:
        return fill.fee * fill.price
    return None


def base_moved(fill: Fill, instrument: str, side: Side) -> Decimal:
    """Сколько базовой монеты пришло на счёт (покупка) или ушло с него (продажа).

    На споте комиссия в базовой монете уменьшает купленное и добавляется к проданному.
    Лот журнала обязан быть тем, что лежит на счёте: иначе продажа «всего лота» получит
    отказ «недостаточно средств». На деривативе объём — контракты, комиссия берётся
    из маржи и объёма не касается."""
    if not is_spot(instrument):
        return fill.qty
    base, _ = _pair(instrument)  # type: ignore[misc]  # спот — всегда имя ccxt
    if fill.fee_asset.upper() != base:
        return fill.qty
    return fill.qty - fill.fee if side == "buy" else fill.qty + fill.fee


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CexFees(_Cfg):
    taker_bps: Decimal = Field(ge=0)
    maker_bps: Decimal = Field(ge=0)


class CexTariff(CexFees):
    """Строка площадки — тариф СПОТА. Бессрочные контракты у бирж тарифицируются
    отдельно (у Bybit 5.5/2 против 10/10 на споте), поэтому у них свой блок `perp`.
    Блока нет — перпы считаются по строке спота, как было до `version: 2`."""

    funding_interval_h: int = Field(ge=1, default=8)
    perp: CexFees | None = None

    def fees(self, *, perp: bool) -> CexFees:
        return self.perp if perp and self.perp is not None else self


class DexTariff(_Cfg):
    pool_fee_bps: Decimal = Field(ge=0)
    priority_fee_usd: Decimal = Field(ge=0)
    gas_usd: Decimal = Field(ge=0)
    chain: str


class NftTariff(_Cfg):
    marketplace_fee_pct: Decimal = Field(ge=0)
    default_royalty_pct: Decimal = Field(ge=0)
    gas_usd: Decimal = Field(ge=0)


class OtherTariff(_Cfg):
    taker_bps: Decimal = Field(ge=0, default=Decimal(0))
    maker_bps: Decimal = Field(ge=0, default=Decimal(0))
    gas_usd: Decimal = Field(ge=0, default=Decimal(0))
    spread_bps: Decimal | None = None


class SlippageDefaults(_Cfg):
    default_spread_bps: Decimal = Field(ge=0)
    impact_bps_per_depth_share: Decimal = Field(ge=0)
    default_depth_usd: Decimal = Field(gt=0)


class CostsConfig(_Cfg):
    version: int = Field(ge=1)
    cex: dict[str, CexTariff]
    dex: dict[str, DexTariff]
    nft: dict[str, NftTariff]
    other: dict[str, OtherTariff] = Field(default_factory=dict)
    slippage: SlippageDefaults

    def kind_of(self, venue: str) -> str:
        for kind in ("cex", "dex", "nft", "other"):
            if venue in getattr(self, kind):
                return kind
        raise KeyError(f"площадка {venue!r} не описана в costs.yaml")


def load_costs(path: Path | None = None) -> CostsConfig:
    return load_config(path or CONFIG_DIR / "costs.yaml", CostsConfig)


@dataclass(frozen=True)
class Pool:
    """Ликвидность пула константного продукта: резерв котировки и спот-цена."""

    liquidity_quote: Decimal
    price: Decimal
    fee_bps: Decimal | None = None  # переопределяет тариф площадки


@dataclass(frozen=True)
class Depth:
    """Глубина без стакана: спред и глубина в USD (для оценки по формуле impact)."""

    depth_usd: Decimal
    spread_bps: Decimal | None = None


def _ref_price(intent: OrderIntent, book: Book | None, pool: Pool | None) -> Decimal:
    if book is not None and book.bids and book.asks:
        return (book.bids[0].price + book.asks[0].price) / 2
    if pool is not None:
        return pool.price
    if intent.price is not None:
        return intent.price
    raise ValueError("нет референсной цены: передай book, pool или intent.price")


def _walk_book(book: Book, side: Side, qty: Decimal) -> tuple[Decimal, Decimal]:
    """VWAP исполнения по стакану и исполненный объём (остаток — за пределами глубины)."""
    levels = book.asks if side == "buy" else book.bids
    remaining = qty
    notional = Decimal(0)
    filled = Decimal(0)
    for level in levels:
        take = min(remaining, level.qty)
        notional += take * level.price
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    if filled == 0:
        raise ValueError("пустой стакан")
    return notional / filled, filled


def _crosses(intent: OrderIntent, book: Book) -> bool:
    if intent.order_type == "market" or intent.price is None:
        return True
    if intent.side == "buy":
        return bool(book.asks) and intent.price >= book.asks[0].price
    return bool(book.bids) and intent.price <= book.bids[0].price


class CostModel:
    def __init__(self, config: CostsConfig | None = None) -> None:
        self.config = config or load_costs()
        payload = json.dumps(self.config.model_dump(mode="json"), sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
        self.version = f"costs-v{self.config.version}@{digest}"

    # -- оценка до сделки ------------------------------------------------------

    def estimate(
        self,
        venue: str,
        intent: OrderIntent,
        book: Book | None = None,
        pool: Pool | None = None,
        depth: Depth | None = None,
        *,
        royalty_pct: Decimal | None = None,
        perp: bool = False,
    ) -> Costs:
        """`perp` — бессрочный контракт, а не спот. Говорит тот, кто знает рынок
        инструмента (движок замера, исполнитель); по имени модель его не угадывает."""
        kind = self.config.kind_of(venue)
        if kind == "cex":
            fees = self.config.cex[venue].fees(perp=perp)
            return self._estimate_cex(fees, intent, book, depth)
        if kind == "dex":
            return self._estimate_dex(self.config.dex[venue], intent, pool, book, depth)
        if kind == "nft":
            return self._estimate_nft(self.config.nft[venue], intent, royalty_pct)
        return self._estimate_other(self.config.other[venue], intent, book, depth)

    def _slip_and_vwap(
        self, intent: OrderIntent, book: Book | None, depth: Depth | None
    ) -> tuple[Decimal, Decimal, bool]:
        """(проскальзывание, цена исполнения, тейкер?)"""
        ref = _ref_price(intent, book, None)
        if book is not None and book.bids and book.asks:
            if not _crosses(intent, book):
                return Decimal(0), intent.price or ref, False
            vwap, filled = _walk_book(book, intent.side, intent.qty)
            unfilled = intent.qty - filled
            # непокрытый стаканом остаток — по формуле impact от последней цены
            extra = Decimal(0)
            if unfilled > 0:
                extra = self._impact(unfilled * vwap, depth) * unfilled * vwap / _BPS
            slip = abs(vwap - ref) * filled + extra
            return slip, vwap, True
        if intent.order_type == "limit":
            return Decimal(0), intent.price or ref, False
        notional = intent.qty * ref
        s = self.config.slippage
        spread = depth.spread_bps if depth and depth.spread_bps is not None else None
        spread = s.default_spread_bps if spread is None else spread
        bps = spread / 2 + self._impact(notional, depth)
        slip = notional * bps / _BPS
        sign = 1 if intent.side == "buy" else -1
        return slip, ref * (1 + sign * bps / _BPS), True

    def _impact(self, notional: Decimal, depth: Depth | None) -> Decimal:
        s = self.config.slippage
        d = depth.depth_usd if depth is not None else s.default_depth_usd
        return s.impact_bps_per_depth_share * notional / d

    def _estimate_cex(
        self, tariff: CexFees, intent: OrderIntent, book: Book | None, depth: Depth | None
    ) -> Costs:
        slip, px, taker = self._slip_and_vwap(intent, book, depth)
        rate = tariff.taker_bps if taker else tariff.maker_bps
        return Costs(fee=intent.qty * px * rate / _BPS, slippage=slip)

    def _estimate_other(
        self, tariff: OtherTariff, intent: OrderIntent, book: Book | None, depth: Depth | None
    ) -> Costs:
        if tariff.spread_bps is not None and depth is None:
            depth = Depth(self.config.slippage.default_depth_usd, tariff.spread_bps)
        slip, px, taker = self._slip_and_vwap(intent, book, depth)
        rate = tariff.taker_bps if taker else tariff.maker_bps
        return Costs(fee=intent.qty * px * rate / _BPS, slippage=slip, gas=tariff.gas_usd)

    def _estimate_dex(
        self,
        tariff: DexTariff,
        intent: OrderIntent,
        pool: Pool | None,
        book: Book | None,
        depth: Depth | None,
    ) -> Costs:
        fee_bps = pool.fee_bps if pool and pool.fee_bps is not None else tariff.pool_fee_bps
        if pool is not None:
            notional = intent.qty * pool.price
            # x*y=k: покупка на dx котировки даёт y*dx/(x+dx) базы; потеря = dx * dx/(x+dx)
            slip = notional * notional / (pool.liquidity_quote + notional)
        else:
            slip, px, _ = self._slip_and_vwap(intent, book, depth)
            notional = intent.qty * px
        return Costs(
            fee=notional * fee_bps / _BPS,
            slippage=slip,
            priority_fee=tariff.priority_fee_usd,
            gas=tariff.gas_usd,
        )

    def _estimate_nft(
        self, tariff: NftTariff, intent: OrderIntent, royalty_pct: Decimal | None
    ) -> Costs:
        if intent.price is None:
            raise ValueError("для NFT нужна цена (intent.price)")
        notional = intent.qty * intent.price
        royalty = tariff.default_royalty_pct if royalty_pct is None else royalty_pct
        return Costs(
            fee=notional * tariff.marketplace_fee_pct / 100,
            royalty=notional * royalty / 100,
            gas=tariff.gas_usd,
        )

    # -- факт по филлу ---------------------------------------------------------

    def actual(
        self,
        fill: Fill,
        *,
        side: Side,
        instrument: str,
        ref_price: Decimal | None = None,
        funding: Decimal = Decimal(0),
        gas: Decimal = Decimal(0),
        priority_fee: Decimal = Decimal(0),
        royalty: Decimal = Decimal(0),
    ) -> Costs:
        """Издержки по факту: комиссия из филла в котировке (`fee_in_quote`; третья монета —
        `UnknownFeeAsset`), проскальзывание относительно референсной цены на момент решения
        (мид/котировка), остальное — как передано."""
        fee = fee_in_quote(fill, instrument)
        if fee is None:
            raise UnknownFeeAsset(
                f"комиссия {fill.fee} {fill.fee_asset} по {instrument}: цены монеты нет"
            )
        slippage = Decimal(0)
        if ref_price is not None:
            diff = fill.price - ref_price if side == "buy" else ref_price - fill.price
            slippage = diff * fill.qty
        return Costs(
            fee=fee,
            slippage=slippage,
            funding=funding,
            gas=gas,
            priority_fee=priority_fee,
            royalty=royalty,
        )

    def funding_interval_h(self, venue: str) -> int | None:
        tariff = self.config.cex.get(venue)
        return tariff.funding_interval_h if tariff else None


_default: CostModel | None = None


def default_model() -> CostModel:
    global _default
    if _default is None:
        _default = CostModel()
    return _default


def estimate(
    venue: str,
    intent: OrderIntent,
    book: Book | None = None,
    pool: Pool | None = None,
    **kw: object,
) -> Costs:
    return default_model().estimate(venue, intent, book, pool, **kw)  # type: ignore[arg-type]


def actual(fill: Fill, *, side: Side, ref_price: Decimal | None = None, **kw: Decimal) -> Costs:
    return default_model().actual(fill, side=side, ref_price=ref_price, **kw)


__all__ = [
    "CostModel",
    "CostsConfig",
    "Depth",
    "Pool",
    "UnknownFeeAsset",
    "actual",
    "base_moved",
    "default_model",
    "estimate",
    "fee_in_quote",
    "is_perpetual",
    "is_spot",
    "load_costs",
]
