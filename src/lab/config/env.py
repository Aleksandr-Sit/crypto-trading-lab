"""Секреты и площадки: имена переменных из .env.example, отчёт «подключена / только данные».

Значения секретов никогда не логируются — только факт «заполнено / пусто».
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lab.contracts import KeyRights

# Площадка → переменные, которые все должны быть заполнены для торговли.
VENUE_ENV: dict[str, tuple[str, ...]] = {
    "bybit": ("BYBIT_API_KEY", "BYBIT_API_SECRET"),
    "okx": ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"),
    "binance": ("BINANCE_API_KEY", "BINANCE_API_SECRET"),
    "hyperliquid": ("HYPERLIQUID_PRIVATE_KEY",),
    "robinhood": ("ROBINHOOD_API_KEY", "ROBINHOOD_PRIVATE_KEY"),
    "polymarket": ("POLYMARKET_PRIVATE_KEY",),
    "solana": ("SOLANA_HOT_WALLET_KEY",),
    "evm": ("EVM_HOT_WALLET_KEY",),
    "ton": ("TON_HOT_WALLET_MNEMONIC",),
}

# Источники данных (ключ не обязателен для старта, но без него источник спит).
DATA_ENV: tuple[str, ...] = (
    "HELIUS_API_KEY",
    "ALCHEMY_API_KEY",
    "ETHERSCAN_API_KEY",
    "THEGRAPH_API_KEY",
    "JUPITER_API_KEY",
    "MAGICEDEN_API_KEY",
    "OPENSEA_API_KEY",
    "TENSOR_API_KEY",
    "TONAPI_KEY",
    "DUNE_API_KEY",
    "CIELO_API_KEY",
    "ALPHAVANTAGE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ADMIN_ID",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
)

LABEL_CONNECTED = "подключена"
LABEL_DATA_ONLY = "только данные (ключ пуст)"
LABEL_WITHDRAW = "ОТКЛОНЕНА: ключ с правом вывода — не используется"


@dataclass(frozen=True)
class VenueStatus:
    venue: str
    connected: bool
    label: str
    missing: tuple[str, ...]


def load_dotenv(path: Path | str = ".env") -> dict[str, str]:
    """Читает KEY=VALUE из .env без сторонних библиотек; не пишет в os.environ."""
    path = Path(path)
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def environment(dotenv: Path | str = ".env") -> dict[str, str]:
    """os.environ поверх .env: реальные переменные окружения имеют приоритет."""
    merged = load_dotenv(dotenv)
    merged.update({k: v for k, v in os.environ.items() if k in _known_names()})
    return merged


def _known_names() -> set[str]:
    names = set(DATA_ENV)
    for keys in VENUE_ENV.values():
        names.update(keys)
    return names


def venues_report(
    env: Mapping[str, str] | None = None,
    rights: Mapping[str, KeyRights] | None = None,
) -> list[VenueStatus]:
    """Список площадок: «подключена» только при полном наборе ключей и без права вывода."""
    env = environment() if env is None else env
    rights = rights or {}
    rows: list[VenueStatus] = []
    for venue, keys in VENUE_ENV.items():
        missing = tuple(k for k in keys if not env.get(k, "").strip())
        if missing:
            rows.append(VenueStatus(venue, False, LABEL_DATA_ONLY, missing))
            continue
        venue_rights = rights.get(venue)
        if venue_rights is not None and venue_rights.withdraw:
            rows.append(VenueStatus(venue, False, LABEL_WITHDRAW, ()))
            continue
        rows.append(VenueStatus(venue, True, LABEL_CONNECTED, ()))
    return rows


def data_keys_report(env: Mapping[str, str] | None = None) -> list[tuple[str, bool]]:
    env = environment() if env is None else env
    return [(name, bool(env.get(name, "").strip())) for name in DATA_ENV]


def format_venues_table(rows: list[VenueStatus]) -> str:
    width = max(len(r.venue) for r in rows)
    lines = ["Площадки:"]
    for r in rows:
        extra = f"  [нет: {', '.join(r.missing)}]" if r.missing else ""
        lines.append(f"  {r.venue.ljust(width)}  {r.label}{extra}")
    return "\n".join(lines)
