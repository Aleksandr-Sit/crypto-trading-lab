"""Реестр исполнителей: имя площадки → фабрика.

Контрактный тест `tests/contracts/test_executor_contract.py` параметризуется по `all()`,
так что новый исполнитель регистрируется здесь и получает проверку автоматически.
"""

from collections.abc import Callable

from lab.contracts import Executor

ExecutorFactory = Callable[[], Executor]

_registry: dict[str, ExecutorFactory] = {}


class DuplicateExecutor(ValueError):
    pass


def register(name: str, factory: ExecutorFactory, *, replace: bool = False) -> None:
    if not replace and name in _registry:
        raise DuplicateExecutor(f"исполнитель '{name}' уже зарегистрирован")
    _registry[name] = factory


def unregister(name: str) -> None:
    _registry.pop(name, None)


def get(name: str) -> ExecutorFactory:
    try:
        return _registry[name]
    except KeyError as err:
        raise KeyError(f"исполнитель '{name}' не зарегистрирован") from err


def all() -> dict[str, ExecutorFactory]:  # noqa: A001 — имя из тикета
    return dict(_registry)
