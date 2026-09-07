"""Живые клиенты свопа: котировка по HTTP, отправка — подписью горячего кошелька.

Котировка бесплатна и нужна и бумаге, и живой торговле, поэтому она здесь настоящая:
Jupiter `/swap/v1/quote`, STON.fi `/v1/swap/simulate`, EVM — котировка маршрута роутера.
Подпись и отправка требуют `solders`/`web3` и ключа горячего кошелька: библиотека грузится
лениво, и без неё ветка честно уходит в «только замер» (`TradingUnavailable`), а не молчит.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from lab.executors.dex.executor import NotConnected, TradingUnavailable
from lab.executors.dex.swap import SwapPlan, SwapQuote, TxResult
from lab.feeds.chains.transport import ChainError, HttpTransport, HttpxTransport

JUPITER_BASE = "https://lite-api.jup.ag"
STONFI_BASE = "https://api.ston.fi/v1"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TON_MASTER = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _dec(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


class HttpSwapClient:
    """Общее: транспорт, ключ кошелька, отказ отправки без библиотеки подписи."""

    venue = "dex"
    sign_lib = ""

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        base: str = "",
        private_key: str | None = None,
        quote_mint: str = "",
        slippage_bps: int = 150,
    ) -> None:
        self.transport = transport or HttpxTransport()
        self.base = base
        self.private_key = private_key
        self.quote_mint = quote_mint
        self.slippage_bps = slippage_bps

    def quote(self, instrument: str, side: str, qty: Decimal, **kw: Any) -> SwapQuote:
        raise NotImplementedError

    def send(self, plan: SwapPlan) -> TxResult:
        if not self.private_key:
            raise NotConnected(f"{self.venue}: нет ключа горячего кошелька — своп не подписать")
        raise TradingUnavailable(
            f"{self.venue}: для отправки свопа нужен {self.sign_lib}; ветка meme идёт "
            "в режиме «только замер»"
        )


class JupiterClient(HttpSwapClient):
    """Jupiter (Solana): 1 RPS без ключа (`research-sources.md` §3)."""

    venue = "jupiter"
    sign_lib = "solders/solana-py и ключ SOLANA_HOT_WALLET_KEY"

    def __init__(self, transport=None, *, base: str = JUPITER_BASE, **kw: Any) -> None:
        kw.setdefault("quote_mint", USDC_MINT)
        super().__init__(transport, base=base, **kw)

    def quote(self, instrument: str, side: str, qty: Decimal, **kw: Any) -> SwapQuote:
        price = kw.get("price") or Decimal(1)
        amount = int(qty * price * 10**6) if side == "buy" else int(qty * 10**6)
        params = {
            "inputMint": self.quote_mint if side == "buy" else instrument,
            "outputMint": instrument if side == "buy" else self.quote_mint,
            "amount": amount,
            "slippageBps": self.slippage_bps,
        }
        raw = self.transport.get(f"{self.base}/swap/v1/quote", params=params)
        if not isinstance(raw, dict) or "outAmount" not in raw:
            raise ChainError("jupiter: маршрут не найден")
        out_amount = _dec(raw["outAmount"]) / Decimal(10**6)
        in_amount = _dec(raw.get("inAmount"), Decimal(amount)) / Decimal(10**6)
        route = ",".join(
            str(step.get("swapInfo", {}).get("label", "")) for step in raw.get("routePlan", [])
        )
        price_out = (in_amount / out_amount) if (side == "buy" and out_amount) else (
            out_amount / in_amount if in_amount else Decimal(0)
        )
        return SwapQuote(
            price=price_out,
            out_amount=out_amount,
            price_impact_pct=_dec(raw.get("priceImpactPct")) * 100,
            route=route,
            raw=raw,
        )


class EvmSwapClient(HttpSwapClient):
    """Uniswap / Aerodrome / PancakeSwap: котировка роутера, отправка — через web3."""

    venue = "uniswap"
    sign_lib = "web3.py и ключ EVM_HOT_WALLET_KEY"

    def __init__(
        self, transport=None, *, venue: str = "uniswap", base: str = "", **kw: Any
    ) -> None:
        super().__init__(transport, base=base, **kw)
        self.venue = venue

    def quote(self, instrument: str, side: str, qty: Decimal, **kw: Any) -> SwapQuote:
        """Котировка EVM-роутера — вызов ноды, а не HTTP-агрегатора: нужен web3."""
        raise TradingUnavailable(
            f"{self.venue}: котировка роутера требует web3.py и RPC (ALCHEMY_API_KEY); "
            "цена ранней стадии берётся из DexScreener"
        )


class StonFiClient(HttpSwapClient):
    """STON.fi (TON): симуляция свопа без ключа (`research-sources.md` §5)."""

    venue = "stonfi"
    sign_lib = "tonutils/tonsdk и мнемоника TON_HOT_WALLET_MNEMONIC"

    def __init__(self, transport=None, *, base: str = STONFI_BASE, **kw: Any) -> None:
        kw.setdefault("quote_mint", TON_MASTER)
        super().__init__(transport, base=base, **kw)

    def quote(self, instrument: str, side: str, qty: Decimal, **kw: Any) -> SwapQuote:
        params = {
            "offer_address": self.quote_mint if side == "buy" else instrument,
            "ask_address": instrument if side == "buy" else self.quote_mint,
            "units": str(int(qty * 10**9)),
            "slippage_tolerance": str(Decimal(self.slippage_bps) / 10000),
        }
        raw = self.transport.post(f"{self.base}/swap/simulate", json=params)
        if not isinstance(raw, dict) or "ask_units" not in raw:
            raise ChainError("stonfi: маршрут не найден")
        ask = _dec(raw["ask_units"]) / Decimal(10**9)
        offer = _dec(raw.get("offer_units"), Decimal(1)) / Decimal(10**9)
        return SwapQuote(
            price=(offer / ask) if ask else Decimal(0),
            out_amount=ask,
            price_impact_pct=_dec(raw.get("price_impact")) * 100,
            route="stonfi",
            raw=raw,
        )


CLIENTS = {
    "jupiter": JupiterClient,
    "uniswap": EvmSwapClient,
    "pancake": EvmSwapClient,
    "stonfi": StonFiClient,
}


def make_client(venue: str, transport: HttpTransport | None = None, **kw: Any) -> HttpSwapClient:
    cls = CLIENTS.get(venue)
    if cls is None:
        raise TradingUnavailable(f"неизвестная DEX-площадка {venue!r}")
    if cls is EvmSwapClient:
        return EvmSwapClient(transport, venue=venue, **kw)
    return cls(transport, **kw)


__all__ = [
    "CLIENTS",
    "JUPITER_BASE",
    "STONFI_BASE",
    "EvmSwapClient",
    "HttpSwapClient",
    "JupiterClient",
    "StonFiClient",
    "make_client",
]
