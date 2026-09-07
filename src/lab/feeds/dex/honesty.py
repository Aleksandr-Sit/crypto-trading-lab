"""Чек-лист честности токена (История 67, R17.2).

Проверяются: mint/freeze authority, доля топ-держателей, ликвидность, возраст, блок-лист.
Провал любого пункта — `passed=False`, и покупка запрещена: стратегия не выдаёт сигнал,
исполнитель отклоняет ордер. Отсутствие данных — тоже провал: «не знаем» на ранней
стадии стоит дороже, чем пропущенный вход.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from lab.feeds.dex.config import HonestyConfig, MemeConfig, load_meme
from lab.feeds.dex.types import TokenInfo


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    value: Decimal | str | None = None
    limit: Decimal | str | None = None
    detail: str = ""


@dataclass(frozen=True)
class Checklist:
    token: str
    chain: str
    passed: bool
    checks: list[Check] = field(default_factory=list)
    checked_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)

    def failed_names(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]

    def reason(self) -> str:
        failed = [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]
        return "; ".join(failed)


def _authority(name: str, value: str | None, required: bool) -> Check:
    if not required:
        return Check(name, True, detail="проверка выключена конфигом")
    if value is None:
        return Check(name, True, value="revoked", detail="права отозваны")
    return Check(name, False, value=value, limit="revoked", detail=f"право у {value}")


def honesty_check(
    token: TokenInfo,
    *,
    config: HonestyConfig | MemeConfig | None = None,
    blocklist: list[str] | tuple[str, ...] = (),
    now: datetime | None = None,
) -> Checklist:
    """`Checklist.passed=False` → покупка запрещена (Приёмка Истории 67)."""
    if isinstance(config, MemeConfig):
        config = config.honesty
    cfg = config or load_meme().honesty
    now = now or datetime.now(UTC)
    denied = {str(x).lower() for x in (*cfg.blocklist, *blocklist)}

    checks: list[Check] = [
        _authority("mint_authority", token.mint_authority, cfg.require_mint_revoked),
        _authority("freeze_authority", token.freeze_authority, cfg.require_freeze_revoked),
    ]

    top1, top10 = token.top1_pct(), token.top10_pct()
    if top1 is None:
        checks.append(
            Check("top_holders", False, detail="нет данных о держателях — покупка запрещена")
        )
    elif top1 > cfg.max_top_holder_pct:
        checks.append(
            Check(
                "top_holders",
                False,
                value=top1,
                limit=cfg.max_top_holder_pct,
                detail=f"крупнейший держатель {top1}% > {cfg.max_top_holder_pct}%",
            )
        )
    elif top10 is not None and top10 > cfg.max_top10_pct:
        checks.append(
            Check(
                "top_holders",
                False,
                value=top10,
                limit=cfg.max_top10_pct,
                detail=f"первая десятка держит {top10}% > {cfg.max_top10_pct}%",
            )
        )
    else:
        checks.append(Check("top_holders", True, value=top1, limit=cfg.max_top_holder_pct))

    liquidity = token.liquidity_usd
    if liquidity is None:
        checks.append(Check("liquidity", False, detail="ликвидность неизвестна"))
    else:
        ok = liquidity >= cfg.min_liquidity_usd
        checks.append(
            Check(
                "liquidity",
                ok,
                value=liquidity,
                limit=cfg.min_liquidity_usd,
                detail="" if ok else f"ликвидность ${liquidity} < ${cfg.min_liquidity_usd}",
            )
        )

    age = token.age_s(now)
    if age is None:
        checks.append(Check("age", False, detail="возраст токена неизвестен"))
    else:
        ok = age >= cfg.min_age_s
        checks.append(
            Check(
                "age",
                ok,
                value=age,
                limit=Decimal(cfg.min_age_s),
                detail="" if ok else f"возраст {age} с < {cfg.min_age_s} с",
            )
        )

    hits = [
        value
        for value in (token.address, token.creator, token.pair)
        if value and str(value).lower() in denied
    ]
    checks.append(
        Check(
            "blocklist",
            not hits,
            value=hits[0] if hits else None,
            detail="" if not hits else f"в блок-листе: {hits[0]}",
        )
    )

    return Checklist(
        token=token.address,
        chain=token.chain,
        passed=all(c.passed for c in checks),
        checks=checks,
        checked_at=now,
    )


__all__ = ["Check", "Checklist", "honesty_check"]
