"""Загрузка YAML в pydantic-модель с понятной ошибкой (имя файла + поле)."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError


class ConfigError(ValueError):
    pass


def load_config[M: BaseModel](path: Path | str, model: type[M]) -> M:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"конфиг {path} не найден")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise ConfigError(f"{path.name}: не разобран YAML: {err}") from err
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: ожидался словарь на верхнем уровне")
    try:
        return model.model_validate(raw)
    except ValidationError as err:
        lines = [f"{path.name}: невалидный конфиг ({err.error_count()} ошибок):"]
        for e in err.errors():
            loc = ".".join(str(p) for p in e["loc"]) or "<корень>"
            lines.append(f"  - {loc}: {e['msg']}")
        raise ConfigError("\n".join(lines)) from err
