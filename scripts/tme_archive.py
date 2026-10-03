"""Архив публичного Telegram-канала через веб-превью t.me/s/<канал>: тексты, даты, фото.

Нужен для разбора автора целиком (индикаторы, сделки, продукты), а не только свежих постов,
как делает `feeds.social.tme.TmePreviewReader`. Ключей не требует.

    python scripts/tme_archive.py pifagortrade --out ../lab-data/channels [--since 2023-01-01] [--no-photos]

Выход: <out>/<канал>/posts.json и <out>/<канал>/img/<id>.jpg. Повторный запуск докачивает
только недостающие фото; ленту перечитывает целиком (она короткая).

Про нумерацию: у каждой фотографии альбома свой номер сообщения, поэтому пропуски
в номерах постов — обычно альбомы, а не удалённые посты. Фото альбома пишутся в `photos`
поста-подписи с их собственными номерами.

`edited` — пост правился после публикации (дата правки превью не отдаёт);
`reply_to` — номер поста, на который этот отвечает (цитата в текст НЕ попадает; до 01.10.2026
попадала вместо текста ответа — архивы, снятые раньше, перекачать);
`unsupported` — вложение (видео, файл, опрос) превью не показывает, только плашка «Please open
Telegram»; текст поста при этом может быть на месте. Вложение читать только через Telegram API.
"""  # noqa: E501 — docstring это текст --help, переносить нельзя

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0"}
WRAP = re.compile(
    r'<div class="tgme_widget_message_wrap.*?(?=<div class="tgme_widget_message_wrap|\Z)', re.S
)
ID = re.compile(r'data-post="[^/]+/(\d+)"')
TIME = re.compile(r'<time datetime="([^"]+)"')
# не цитата из поста-ответа (`js-message_reply_text`): иначе ответ получает чужой текст
TEXT = re.compile(
    r'<div class="tgme_widget_message_text(?![^"]*reply_text)[^"]*"[^>]*>(.*?)</div>', re.S
)
REPLY = re.compile(r'class="tgme_widget_message_reply[^"]*" href="https?://t\.me/[^/]+/(\d+)')
PHOTO = re.compile(
    r"tgme_widget_message_photo_wrap[^>]*?background-image:url\('([^']+)'\)"
    r"[^>]*?href=\"[^\"]*/(\d+)",
    re.S,
)
VIDEO = re.compile(r"tgme_widget_message_video")
VIEWS = re.compile(r'tgme_widget_message_views">([^<]+)<')
LINK = re.compile(r'<a href="(https?://[^"]+)"')
EDITED = re.compile(r'tgme_widget_message_meta">edited')


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=30).read()


def clean(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse(page: str) -> list[dict]:
    out = []
    for block in WRAP.findall(page):
        m, t = ID.search(block), TIME.search(block)
        if not m or not t:
            continue
        tx = TEXT.search(block)
        text_html = tx.group(1) if tx else ""
        out.append({
            "id": int(m.group(1)),
            "date": t.group(1),
            "text": clean(text_html),
            "links": sorted(set(LINK.findall(text_html))),
            "photos": [{"id": int(pid), "url": url} for url, pid in PHOTO.findall(block)],
            "video": bool(VIDEO.search(block)),
            "views": (VIEWS.search(block).group(1) if VIEWS.search(block) else ""),
            "edited": bool(EDITED.search(block)),
            "reply_to": int(REPLY.search(block).group(1)) if REPLY.search(block) else None,
            # не «text_not_supported»: теперь это класс обёртки КАЖДОГО поста
            "unsupported": "message_media_not_supported" in block,
        })
    return out


def crawl(channel: str, since: str) -> list[dict]:
    posts: dict[int, dict] = {}
    before = None
    while True:
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        got = parse(get(url).decode("utf-8"))
        if before is not None:
            got = [p for p in got if p["id"] < before]
        if not got:
            break
        for p in got:
            posts[p["id"]] = p
        oldest = min(p["id"] for p in got)
        print(f"  до #{oldest} {posts[oldest]['date'][:10]}, всего {len(posts)}", flush=True)
        if posts[oldest]["date"][:10] < since:
            break
        before = oldest
        time.sleep(0.6)
    return sorted((p for p in posts.values() if p["date"][:10] >= since), key=lambda p: p["id"])


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("channel")
    ap.add_argument("--out", default="../lab-data/channels")
    ap.add_argument("--since", default="2000-01-01")
    ap.add_argument("--no-photos", action="store_true")
    args = ap.parse_args()

    root = Path(args.out) / args.channel
    (root / "img").mkdir(parents=True, exist_ok=True)
    print(f"{args.channel}: читаю ленту")
    posts = crawl(args.channel, args.since)
    (root / "posts.json").write_text(
        json.dumps(posts, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    photos = [ph for p in posts for ph in p["photos"]]
    print(f"{len(posts)} постов, {len(photos)} фото, "
          f"{posts[0]['date'][:10]} .. {posts[-1]['date'][:10]}")
    if args.no_photos:
        return 0
    fresh = 0
    for ph in photos:
        path = root / "img" / f"{ph['id']}.jpg"
        if path.exists():
            continue
        try:
            path.write_bytes(get(ph["url"]))
            fresh += 1
        except Exception as err:  # noqa: BLE001 — ссылка CDN могла протухнуть, остальное качаем
            print(f"  фото #{ph['id']}: {err}")
        time.sleep(0.15)
    print(f"фото докачано: {fresh}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
