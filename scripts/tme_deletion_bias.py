"""Выборочно ли автор удаляет проигрыши: доля удалённых среди отчётов-поражений против отчётов-побед.

Вселенная — посты, которые видел архив Wayback (удаление видно только у них). Текст — САМАЯ
РАННЯЯ версия из снимков, одинаково для удалённых и живых: иначе живой пост получал бы
вторую попытку совпасть через нынешний (дописанный) текст. Совпадения печатаются целиком —
правило ловит и планы («тейк на 62000»), поэтому список читать глазами.

До 01.10.2026 правило подбиралось руками и не сохранилось (Pifagor 30.09: «2 из 8 против 10
из 30»); теперь оно здесь, и старые и новые данные считаются одним и тем же.

    python scripts/tme_deletion_bias.py --pages <снимки> --posts <posts.json> [--show]
"""  # noqa: E501 — docstring это текст --help, переносить нельзя

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tme_archive import parse  # noqa: E402

LOSS = re.compile(
    # не «в убыт»: ловит «надеюсь, вы не в убытках» чаще, чем отчёт
    r"стоп\w*\s+(задет|задело|сработ|выбил)|задел\w*\s+стоп|(закрыт|закрыл)\w*\s+по\s+стопу"
    r"|выбил\w*\s+по\s+стоп|сорри\s+ребят|закрыл\w*[^.\n]{0,25}в\s*(минус|-\s*\d)",
    re.I,
)
WIN = re.compile(
    r"поздравляю\s+с\s+профит|цел\w*\s+(была\s+)?отработ|тейк\w*\s+(взят|забрал|отработ|сработ|задет)"
    r"|(забрал|взял)\w*\s+тейк|по\s+тейку|закрыл\w*[^.\n]{0,25}в\s*(плюс|\+\s*\d)|пришли\s+к\s+цел",
    re.I,
)


def fisher_two_sided(a: int, b: int, c: int, d: int) -> float:
    """Точный тест Фишера для таблицы [[a, b], [c, d]] (двусторонний, по вероятностям)."""
    n1, n2, k, n = a + b, c + d, a + c, a + b + c + d

    def p(x: int) -> float:
        return comb(n1, x) * comb(n2, k - x) / comb(n, k)

    p0 = p(a)
    lo, hi = max(0, k - n2), min(k, n1)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= p0 * (1 + 1e-9)))


def archived(pages: Path) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for f in sorted(pages.glob("*.html*")):
        ts = f.name.split("_")[0]
        raw = gzip.open(f).read() if f.suffix == ".gz" else f.read_bytes()
        for p in parse(raw.decode("utf-8", "replace")):
            if p["id"] not in out or ts < out[p["id"]]["snapshot"]:
                out[p["id"]] = p | {"snapshot": ts}
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--pages", required=True)
    ap.add_argument("--posts", required=True)
    ap.add_argument("--show", action="store_true", help="печатать каждое совпадение")
    args = ap.parse_args()

    cur = json.load(open(args.posts, encoding="utf-8"))
    alive = {p["id"] for p in cur} | {ph["id"] for p in cur for ph in p["photos"]}
    arch = archived(Path(args.pages))
    res = {}
    for name, rx in (("поражение", LOSS), ("победа", WIN)):
        hits = [(pid, a) for pid, a in sorted(arch.items()) if rx.search(a["text"])]
        gone = [pid for pid, _ in hits if pid not in alive]
        res[name] = (len(gone), len(hits))
        print(f"{name}: удалено {len(gone)} из {len(hits)}")
        if args.show:
            for pid, a in hits:
                mark = "УДАЛЁН" if pid not in alive else "жив   "
                print(f"  {mark} #{pid} {a['date'][:10]} reply_to={a.get('reply_to')}: "
                      f"{a['text'][:160]!r}")
    (dl, nl), (dw, nw) = res["поражение"], res["победа"]
    if nl and nw:
        p = fisher_two_sided(dl, nl - dl, dw, nw - dw)
        print(f"доля удалённых: поражения {dl / nl:.0%}, победы {dw / nw:.0%}; Фишер p = {p:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
