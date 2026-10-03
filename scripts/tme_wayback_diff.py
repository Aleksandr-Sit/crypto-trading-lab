"""Что автор канала удалил и переписал: снимки Wayback Machine против нынешней ленты.

Снимки t.me/s/<канал>[/<id>] берутся из web.archive.org в «сыром» виде (`<ts>id_/<url>`)
и лежат как <dir>/<ts>_<что угодно>.html(.gz). Нынешняя лента — posts.json от tme_archive.py.

Результат:
  * deleted — пост был в архиве, а сейчас его номера нет ни среди постов, ни среди фото альбомов;
  * changed — пост есть, но текст отличается (сравнение без пробелов и регистра), с обеими версиями.
Дата снимка — верхняя граница «когда пост существовал в таком виде».

    python scripts/tme_wayback_diff.py --pages <dir> --posts <posts.json> --out <prefix>
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tme_archive import parse  # noqa: E402


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--pages", required=True)
    ap.add_argument("--posts", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cur = json.load(open(args.posts, encoding="utf-8"))
    by_id = {p["id"]: p for p in cur}
    album = {ph["id"] for p in cur for ph in p["photos"]}

    archived: dict[int, dict] = {}
    files = sorted(Path(args.pages).glob("*.html*"))
    for f in files:
        ts = f.name.split("_")[0]
        raw = gzip.open(f).read() if f.suffix == ".gz" else f.read_bytes()
        for p in parse(raw.decode("utf-8", "replace")):
            old = archived.get(p["id"])
            # храним САМУЮ РАННЮЮ версию: она ближе к исходному тексту
            if old is None or ts < old["snapshot"]:
                archived[p["id"]] = p | {"snapshot": ts}

    deleted, changed = [], []
    for pid, a in sorted(archived.items()):
        if pid in by_id:
            now = by_id[pid]
            if a["text"] and norm(a["text"]) != norm(now["text"]):
                changed.append({"id": pid, "date": a["date"], "snapshot": a["snapshot"],
                                "was": a["text"], "now": now["text"]})
        elif pid not in album:
            deleted.append({"id": pid, "date": a["date"], "snapshot": a["snapshot"],
                            "text": a["text"], "photos": len(a["photos"]), "video": a["video"]})

    Path(args.out + "_deleted.json").write_text(
        json.dumps(deleted, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    Path(args.out + "_changed.json").write_text(
        json.dumps(changed, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"снимков {len(files)}, постов в архиве {len(archived)} "
          f"(номера {min(archived)}..{max(archived)}), сейчас в ленте {len(cur)}")
    print(f"удалено с тех пор: {len(deleted)}; переписано: {len(changed)}")
    by_year: dict[str, list[int]] = {}
    for d in deleted:
        by_year.setdefault(d["date"][:4], []).append(d["id"])
    print("удалённые по году публикации:", {y: len(v) for y, v in sorted(by_year.items())})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
