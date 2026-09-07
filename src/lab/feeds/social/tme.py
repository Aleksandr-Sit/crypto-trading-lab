"""Читалка каналов авторов через веб-превью `https://t.me/s/<канал>` — без ключей Telethon.

Публичный канал отдаёт свои посты обычной HTML-страницей: блок `tgme_widget_message`
с атрибутом `data-post="канал/НОМЕР"`, текст в `.tgme_widget_message_text`, дата в
`<time datetime=...>`. Авторизация не нужна, `TELEGRAM_API_ID/HASH` — тоже.

Интерфейс тот же, что у `TelegramReader`: `available()`, `health()`, `read()`; тип
сообщения — общий `ChannelMessage`. Сеть спрятана за протоколом `TmeTransport` ровно
ради тестов (`FakeTmeTransport`). Любой отказ — деградация: `health()` = down с
причиной, `read()` — пустой список, наружу ничего не летит.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode

from lab.contracts import Health
from lab.feeds.quota import NullQuota, QuotaSink
from lab.feeds.social.telegram import ChannelMessage, TelegramReader, channels_from_authors

TME_BASE = "https://t.me/s"
FEED_ID = "telegram_web"
DEFAULT_TIMEOUT_S = 15.0
PAGE_SIZE = 20  # столько постов отдаёт одна страница превью
MAX_PAGES = 10  # потолок пагинации: дальше не ходим, сколько бы ни просили

NO_PREVIEW = "недоступен: канал закрыт или веб-превью t.me/s выключено"
UNREACHABLE = "недоступен: нет связи с t.me"

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "wbr"}


class TmeError(RuntimeError):
    """Страница превью не отдалась: нет связи, отказ t.me, не тот ответ."""


@runtime_checkable
class TmeTransport(Protocol):
    def get_text(self, url: str, *, params: dict | None = None) -> str: ...


class HttpxTmeTransport:
    """Живой транспорт. `httpx` импортируется лениво — среда без него живёт."""

    def __init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S, client: Any = None) -> None:
        self.timeout_s = timeout_s
        self._client = client

    def _c(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                timeout=self.timeout_s,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; crypto-trading-lab)"},
            )
        return self._client

    def get_text(self, url: str, *, params: dict | None = None) -> str:
        import httpx

        try:
            response = self._c().get(url, params=params)
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError as err:
            raise TmeError(f"{url}: {err.response.status_code}") from err
        except httpx.HTTPError as err:
            raise TmeError(f"{url}: нет связи ({err})") from err
        except ImportError as err:  # pragma: no cover — httpx в зависимостях
            raise TmeError(f"{url}: httpx не установлен") from err

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


class FakeTmeTransport:
    """Подставной транспорт для тестов: маршруты по подстроке URL с параметрами.

    Первый подходящий маршрут выигрывает — более узкий (`before=…`) регистрируй раньше.
    """

    def __init__(self, *, offline: bool = False) -> None:
        self.routes: list[tuple[str, Any]] = []
        self.calls: list[str] = []
        self.offline = offline

    def route(self, match: str, value: Any) -> FakeTmeTransport:
        """`value` — готовый HTML или `f(url, params) -> html`."""
        self.routes.append((match, value))
        return self

    def get_text(self, url: str, *, params: dict | None = None) -> str:
        key = url if not params else f"{url}?{urlencode(params)}"
        self.calls.append(key)
        if self.offline:
            raise TmeError(f"{key}: нет связи (офлайн-транспорт)")
        for match, value in self.routes:
            if match in key:
                return value(url, params) if isinstance(value, Callable) else str(value)
        raise TmeError(f"GET {key}: маршрут не задан в FakeTmeTransport")


@dataclass(frozen=True)
class _RawPost:
    message_id: int
    published_at: datetime | None
    text: str


class _PreviewParser(HTMLParser):
    """Собирает посты со страницы `t.me/s/<канал>`: id из `data-post`, дату из `<time>`,
    текст из `.tgme_widget_message_text` — теги вон, сущности развёрнуты, `<br>` — перевод
    строки."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[_RawPost] = []
        self._depth = 0
        self._data_post: str | None = None
        self._time: str | None = None
        self._text_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            if tag == "br" and self._text_depth:
                self._chunks.append("\n")
            return
        attr = {k: (v or "") for k, v in attrs}
        classes = attr.get("class", "").split()
        if self._data_post is None:
            if "tgme_widget_message" in classes and attr.get("data-post"):
                self._data_post, self._time, self._depth = attr["data-post"], None, 1
                self._text_depth, self._chunks = 0, []
            return
        self._depth += 1
        if tag == "time" and attr.get("datetime"):
            self._time = attr["datetime"]
        if "tgme_widget_message_text" in classes and self._text_depth == 0:
            self._text_depth = self._depth

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID or self._data_post is None:
            return
        if self._text_depth and self._depth == self._text_depth:
            self._text_depth = 0
        self._depth -= 1
        if self._depth <= 0:
            self._close_post()

    def handle_data(self, data: str) -> None:
        if self._text_depth:
            self._chunks.append(data)

    def close(self) -> None:  # незакрытая разметка не должна съедать последний пост
        super().close()
        if self._data_post is not None:
            self._close_post()

    def _close_post(self) -> None:
        data_post, raw_time = self._data_post, self._time
        text = _clean("".join(self._chunks))
        self._data_post, self._time, self._chunks, self._text_depth = None, None, [], 0
        self._depth = 0
        message_id = _message_id(data_post or "")
        if message_id is None:
            return
        self.posts.append(_RawPost(message_id, _parse_time(raw_time), text))


def _message_id(data_post: str) -> int | None:
    tail = data_post.rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)


def _clean(text: str) -> str:
    lines = [line.strip() for line in text.replace("\r", "").split("\n")]
    return "\n".join(lines).strip()


def _fresh(post: _RawPost, since: datetime | None) -> bool:
    return since is None or post.published_at is None or post.published_at >= since


def parse_page(html: str) -> list[_RawPost]:
    """Посты страницы превью в порядке разметки (от старых к свежим)."""
    parser = _PreviewParser()
    parser.feed(html)
    parser.close()
    return parser.posts


class TmePreviewReader:
    """Тот же интерфейс, что у `TelegramReader`, но через веб-превью и без ключей."""

    feed_id = FEED_ID

    def __init__(
        self,
        transport: TmeTransport | None = None,
        *,
        quota: QuotaSink | None = None,
        base: str = TME_BASE,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self.transport = transport or HttpxTmeTransport()
        self.quota = quota or NullQuota()
        self.base = base.rstrip("/")
        self.max_pages = max(1, max_pages)
        self.id = self.feed_id

    def available(self) -> bool:
        return True  # ключей не требует; отказ виден в health()

    def health(self, channel: str | None = None) -> Health:
        """Проба: тянем страницу канала. Нет связи, канал закрыт или превью выключено —
        `down` с причиной; исключений наружу нет."""
        now = datetime.now(UTC)
        target = channel or self._probe_channel()
        if target is None:
            return Health(
                status="down",
                detail="недоступен: в config/authors.yaml нет telegram-каналов",
                checked_at=now,
            )
        try:
            posts = self._page(target, before=None)
        except Exception as err:  # noqa: BLE001 — любой отказ транспорта суть недоступность
            return Health(status="down", detail=f"{UNREACHABLE}: {err}", checked_at=now)
        if not posts:
            return Health(status="down", detail=f"{NO_PREVIEW} ({target})", checked_at=now)
        return Health(status="ok", detail=f"веб-превью t.me/s/{target} читается", checked_at=now)

    @staticmethod
    def _probe_channel() -> str | None:
        try:
            channels = channels_from_authors()
        except Exception:  # noqa: BLE001 — конфига может не быть
            return None
        return channels[0][1] if channels else None

    def read(
        self, channel: str, *, since: datetime | None = None, limit: int = 200
    ) -> list[ChannelMessage]:
        try:
            raw = self._collect(channel, since=since, limit=limit)
        except TmeError:
            return []
        except Exception:  # noqa: BLE001 — читалка не роняет вызывающего ничем
            return []
        seen: dict[int, ChannelMessage] = {}
        for post in raw:
            if post.text and post.published_at is not None and post.message_id not in seen:
                seen[post.message_id] = ChannelMessage(
                    channel, post.message_id, post.published_at, post.text
                )
        out = sorted(seen.values(), key=lambda m: m.message_id, reverse=True)
        return out[:limit]

    def _collect(self, channel: str, *, since: datetime | None, limit: int) -> list[_RawPost]:
        """Страница отдаёт ~20 постов; за более старыми ходим с `?before=<id>`.

        Останавливаемся, когда посты кончились, ушли за `since`, набрали `limit`
        или упёрлись в потолок `max_pages`. Отказ на первой странице — отказ чтения,
        на следующих — просто конец обхода: собранное не выбрасываем.
        """
        collected: list[_RawPost] = []
        before: int | None = None
        for page_no in range(self.max_pages):
            try:
                posts = self._page(channel, before=before)
            except TmeError:
                if page_no == 0:
                    raise
                break
            if before is not None:  # `before` отдаёт строго более старые — свежие отбрасываем
                posts = [p for p in posts if p.message_id < before]
            if not posts:
                break
            fresh = [p for p in posts if _fresh(p, since)]
            collected.extend(fresh)
            oldest = min(p.message_id for p in posts)
            crossed = len(fresh) < len(posts)  # на странице пошли посты старше `since`
            if crossed or len(collected) >= limit:
                break
            before = oldest
        return collected

    def _page(self, channel: str, *, before: int | None) -> list[_RawPost]:
        self.quota.use(self.feed_id, 1)
        params = {"before": before} if before is not None else None
        return parse_page(self.transport.get_text(f"{self.base}/{channel}", params=params))


def make_reader(
    env: Mapping[str, str] | None = None,
    *,
    transport: TmeTransport | None = None,
    quota: QuotaSink | None = None,
) -> TelegramReader | TmePreviewReader:
    """Читалка каналов: Telethon, если заданы обе `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`,
    иначе веб-превью `t.me/s/` — оно ключей не требует и работает всегда."""
    env = os.environ if env is None else env
    if env.get("TELEGRAM_API_ID") and env.get("TELEGRAM_API_HASH"):
        return TelegramReader.from_env(env)
    return TmePreviewReader(transport, quota=quota)


__all__ = [
    "FEED_ID",
    "MAX_PAGES",
    "NO_PREVIEW",
    "PAGE_SIZE",
    "TME_BASE",
    "UNREACHABLE",
    "ChannelMessage",
    "FakeTmeTransport",
    "HttpxTmeTransport",
    "TmeError",
    "TmePreviewReader",
    "TmeTransport",
    "make_reader",
    "parse_page",
]
