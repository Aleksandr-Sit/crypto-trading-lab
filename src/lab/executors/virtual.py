"""Persistent paper execution from a freshly fetched order book; never sends orders."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from lab.contracts import Costs, Fill, OrderIntent
from lab.core.costs import CostModel, is_spot
from lab.core.journal import Journal
from lab.db.base import utcnow
from lab.db.models import ForwardObservationRow, OrderRow, SignalRow, StrategyRow


class VirtualExecutor:
    def __init__(self, session, feed, *, clock=utcnow):
        self.session = session
        self.feed = feed
        self.clock = clock
        self.journal = Journal(session)

    def execute(self, signal_id: str) -> str:
        from lab.core.forward import current_observation

        with self.session.begin_nested():
            signal = self.session.get_one(SignalRow, signal_id)
            observation = current_observation(self.session, signal.strategy_id)
            if observation is None or signal.observation_id != observation.id:
                raise ValueError("signal observation is no longer current")
            strategy = self.session.get_one(StrategyRow, signal.strategy_id)
            if strategy.rung != "paper" or strategy.status not in ("measuring", "passed"):
                raise ValueError("strategy is not active on paper")
            return self._execute(signal_id)

    def _execute(self, signal_id: str) -> str:
        signal = self.session.get_one(SignalRow, signal_id)
        # The observation's strategy lock serializes this with the collector.
        existing = self.session.scalar(select(OrderRow).where(
            OrderRow.signal_id == signal_id, OrderRow.mode == "paper",
        ))
        if existing is not None:
            return existing.id
        if signal.side not in ("buy", "sell") or signal.size <= 0:
            raise ValueError("unsupported signal side or size")
        if signal.meta.get("order_type", "market") != "market":
            raise ValueError("virtual limit execution is not supported")
        if not is_spot(signal.instrument):
            raise ValueError("virtual derivatives require funding accounting")
        qty = signal.size
        if signal.side == "sell":
            held = self.journal.open_qty(
                signal.strategy_id, signal.instrument, "long", mode="paper",
            )
            qty = min(qty, held)
        if qty <= 0:
            raise ValueError("no paper position to sell")
        book = self.feed.book(signal.instrument)
        now = self.clock()
        if not (signal.decided_at <= now <= signal.decided_at + timedelta(seconds=signal.ttl)):
            raise ValueError("signal expired or future dated")
        if now - signal.decided_at > timedelta(seconds=60):
            raise ValueError("signal arrived late")
        if not timedelta(0) <= now - book.ts <= timedelta(seconds=10):
            raise ValueError("stale or future order book")
        if (book.instrument != signal.instrument or not book.bids or not book.asks
                or book.bids[0].price <= 0
                or book.asks[0].price < book.bids[0].price):
            raise ValueError("invalid order book")
        levels = book.asks if signal.side == "buy" else book.bids
        remaining, total = qty, Decimal(0)
        for level in levels:
            if level.price <= 0 or level.qty < 0:
                raise ValueError("invalid order book level")
            take = min(remaining, level.qty)
            total += take * level.price
            remaining -= take
            if remaining == 0:
                break
        if remaining:
            raise ValueError("insufficient visible order book depth")
        price = total / qty
        intent = OrderIntent(
            strategy_id=signal.strategy_id, venue=self.feed.venue,
            instrument=signal.instrument, side=signal.side, qty=qty, price=price,
            order_type="market", mode="paper", signal_id=signal_id,
            client_order_id=f"virtual:{signal_id}",
        )
        # Price already includes spread and depth. Charging slippage again would double it.
        fee = CostModel().estimate(intent.venue, intent, book=book).fee
        quote = signal.instrument.split("/")[1]
        if quote not in ("USD", "USDT", "USDC"):
            raise ValueError("paper capital requires a USD quoted instrument")
        if signal.side == "buy":
            observation = self.session.get_one(ForwardObservationRow, signal.observation_id)
            capital = Decimal(observation.spec["measurement_capital"])
            closed = [t for t in self.journal.closed_trades(signal.strategy_id)
                      if t.mode == "paper"]
            lots = [t for t in self.journal.open_trades(signal.strategy_id) if t.mode == "paper"]
            cash = (capital + sum((t.pnl_net for t in closed), Decimal(0))
                    - sum((t.entry_price * t.qty + t.costs.total for t in lots), Decimal(0)))
            if total + fee > cash:
                raise ValueError("insufficient paper cash")
        signal.meta = {**signal.meta, "virtual_book": book.model_dump(mode="json")}
        order_id = f"virtual:{signal_id}"
        self.journal.record_order(intent, order_id=order_id,
                                  risk_verdict={"executor": "virtual-book-v1"})
        # Actual creation time, after the book request and the journal order insert.
        filled_at = self.clock()
        if filled_at > signal.decided_at + timedelta(seconds=signal.ttl):
            raise ValueError("signal expired during execution")
        self.journal.record_fill(Fill(
            id=f"{order_id}:fill", order_id=order_id, price=price, qty=qty,
            fee=fee, fee_asset=signal.instrument.split("/")[1], ts=filled_at,
        ), costs=Costs(fee=fee), ref_price=signal.price_ref)
        return order_id
