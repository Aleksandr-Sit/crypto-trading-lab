"""Реестр классов стратегий (код, не база): id `<ветка>-<источник>-<слаг>` → (класс, манифест).

Отличие от `core.registry`: там — записи реестра со ступенями и статусами (таблица strategies),
здесь — какие правила вообще умеет собирать код. Worker/замер берут стратегию через `build(id)`.
Карточка каталога (`docs/research/strategies/<id>.md`) читается `manifest_from_card` —
параметры карточки становятся `params` манифеста.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from lab.contracts import Branch, StopSpec, StrategyManifest
from lab.strategies.base import Strategy, strategy_id_of

_REGISTRY: dict[str, tuple[type[Strategy], StrategyManifest]] = {}


class UnknownStrategy(KeyError):
    pass


def register(
    cls: type[Strategy], manifest: StrategyManifest, *, replace: bool = False
) -> type[Strategy]:
    sid = strategy_id_of(manifest)
    if sid in _REGISTRY and not replace:
        raise ValueError(f"стратегия {sid} уже зарегистрирована")
    _REGISTRY[sid] = (cls, manifest)
    return cls


def preset(manifest: StrategyManifest) -> Callable[[type[Strategy]], type[Strategy]]:
    """Декоратор: `@preset(manifest) class X(Strategy)`."""

    def deco(cls: type[Strategy]) -> type[Strategy]:
        return register(cls, manifest, replace=True)

    return deco


def ids() -> list[str]:
    _load_builtin()
    return sorted(_REGISTRY)


def manifest(strategy_id: str) -> StrategyManifest:
    _load_builtin()
    try:
        return _REGISTRY[strategy_id][1]
    except KeyError as err:
        raise UnknownStrategy(strategy_id) from err


def klass(strategy_id: str) -> type[Strategy]:
    _load_builtin()
    try:
        return _REGISTRY[strategy_id][0]
    except KeyError as err:
        raise UnknownStrategy(strategy_id) from err


def build(
    strategy_id: str,
    *,
    params: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> Strategy:
    """Экземпляр стратегии; `params` накладываются поверх параметров манифеста.

    `overrides` — поля манифеста из записи реестра (площадка, инструменты, таймфрейм, стоп).
    Правила берутся из кода, а ГДЕ и НА ЧЁМ они работают — из записи: иначе копия стратегии
    на другом ряду (`--slug-suffix`) молча читала бы инструмент исходной площадки и получала
    «нет свечей в окне».
    """
    _load_builtin()
    try:
        cls, base = _REGISTRY[strategy_id]
    except KeyError as err:
        raise UnknownStrategy(strategy_id) from err
    update: dict[str, Any] = dict(overrides or {})
    if params:
        update["params"] = {**base.params, **params}
    m = base if not update else base.model_copy(update=update)
    return cls(m)


def factory(strategy_id: str, *, params: dict[str, Any] | None = None) -> Callable[[], Strategy]:
    """Фабрика для `core.measure.run(strategy=...)` с walk-forward (свежий экземпляр на окно)."""
    return lambda: build(strategy_id, params=params)


# -- карточки каталога ----------------------------------------------------------------------

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def read_card(path: Path | str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    m = _FRONT.match(text)
    if not m:
        raise ValueError(f"{path}: нет YAML-фронтматтера")
    data = yaml.safe_load(m.group(1)) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: фронтматтер должен быть словарём")
    return data


def manifest_from_card(
    path: Path | str,
    *,
    slug: str | None = None,
    source_kind: str | None = None,
    stop: StopSpec | None = None,
    overrides: dict[str, Any] | None = None,
) -> StrategyManifest:
    """Манифест из карточки. `source_kind` карточек (`bot_preset`, `author_indicator`) не проходит
    паттерн манифеста — заменяется на `preset`/`indicator`; slug — хвост id карточки после ветки."""
    card = read_card(path)
    # Вселенная в карточке может быть описана ПРОЗОЙ («топ-50 USDT-пар по обороту»), когда
    # состав пересчитывается на каждом ребалансе. `list()` от строки дал бы список отдельных
    # БУКВ, и стратегия поехала бы торговать инструментом «т». Такие вселенные задаются
    # списком при регистрации (`scripts/register_coded_strategy.py --instrument`).
    raw_instruments = card.get("instruments") or []
    if not isinstance(raw_instruments, list):
        raw_instruments = []
    branch = Branch(card["branch"])
    card_id = str(card["id"])
    kind = source_kind or _SOURCE_KINDS.get(str(card.get("source_kind", "")), "preset")
    params = dict(card.get("params") or {})
    params["card"] = card_id
    if overrides:
        params.update(overrides)
    stop_pct = params.get("stop_loss_pct")
    try:
        return StrategyManifest(
            slug=slug or card_id.removeprefix(f"{branch.value}-"),
            branch=branch,
            venue=str(card.get("venue", "")),
            source_kind=kind,
            source_ref=str(card.get("source_ref")) if card.get("source_ref") else None,
            instruments=raw_instruments,
            timeframe=_TF.get(str(card.get("timeframe")), str(card.get("timeframe"))),
            params=params,
            can_backtest=bool(card.get("can_backtest", True)),
            stop=stop or StopSpec(max_dd_pct=stop_pct if stop_pct else 25),
            description=str(card.get("regime", "")),
        )
    except ValidationError as err:
        raise ValueError(f"{path}: карточка не даёт валидный манифест: {err}") from err


_SOURCE_KINDS = {"bot_preset": "preset", "author_indicator": "indicator", "public": "public"}
# 1w/1M в карточках: движок шагает по дневным свечам, недели/месяцы стратегия строит сама
_TF = {"1w": "1d", "1M": "1d"}

_loaded = False


def _load_builtin() -> None:
    """Пресеты, индикаторные и книжные стратегии регистрируются при импорте своих модулей."""
    global _loaded
    if _loaded:
        return
    import lab.strategies.classics  # noqa: F401
    import lab.strategies.indicators.strategies  # noqa: F401
    import lab.strategies.listing  # noqa: F401
    import lab.strategies.neutral  # noqa: F401
    import lab.strategies.pairs  # noqa: F401
    import lab.strategies.positioning  # noqa: F401
    import lab.strategies.presets  # noqa: F401
    import lab.strategies.xsmom  # noqa: F401

    # Флаг ставится ПОСЛЕ импортов, а не до. Иначе сломанный модуль виден только один раз:
    # первый вызов падает с настоящей ошибкой, а все следующие считают загрузку сделанной
    # и врут «стратегия неизвестна» — на реестре без половины стратегий.
    _loaded = True


__all__ = [
    "UnknownStrategy",
    "build",
    "factory",
    "ids",
    "klass",
    "manifest",
    "manifest_from_card",
    "preset",
    "read_card",
    "register",
]
