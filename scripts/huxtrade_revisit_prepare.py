"""Freeze the order-covered HuxTrade regime before requesting entry market data."""

import argparse
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import automata_revisit_probe as probe

PID = "5192646545574438913"
PREFIX = "huxtrade_revisit"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    source = args.root / f"huxtrade_{PID}_2026-11-07.json"
    order_source = args.root / f"huxtrade_{PID}_orders_rawpass_2026-11-07.json"
    destination = args.root / f"{PREFIX}_{PID}_2026-11-08.json"
    assert not destination.exists(), "Cohort already frozen"
    data, orders = probe.read(source), probe.read(order_source)["orders"]
    cohort, excluded = [], []
    for position in data["positions"]:
        buys = [
            order
            for order in orders
            if order["symbol"] == position["symbol"]
            and order["side"] == "BUY"
            and abs(order["orderUpdateTime"] - position["opened"]) <= 25
        ]
        if not buys:
            excluded.append(position["positionId"])
            continue
        assert len(buys) == 1
        order = buys[0]
        assert position["side"] == "Long" and position["leverage"] == "2"
        assert order["type"] == "MARKET" and order["positionSide"] == "LONG"
        assert Decimal(str(order["executedQty"])) == Decimal(str(position["closedVolume"]))
        assert order["orderTime"] <= order["orderUpdateTime"]
        assert datetime.fromtimestamp(order["orderTime"] / 1000, UTC).hour == 16
        cohort.append(
            dict(
                position,
                position_opened=position["opened"],
                opened=order["orderTime"],
                first_entry_order=order,
            )
        )
    assert len(cohort) == 236 and len(excluded) == 72
    probe.save(
        destination,
        dict(
            positions=cohort,
            total=len(cohort),
            complete=False,
            scope="Order-covered Long x2, 16 UTC regime only; not full history",
            excluded_position_ids=excluded,
            sources={source.name: probe.sha(source), order_source.name: probe.sha(order_source)},
            script_sha256=probe.sha(Path(__file__)),
        ),
    )
    probe.PREFIX, probe.PID = PREFIX, PID
    probe.prepare(args.root)
    split_path = args.root / f"{PREFIX}_entry_split_2026-11-08.json"
    split = probe.read(split_path)
    split["holdout_scope"] = (
        "Unseen pre-order market features only; position/order timing and execution "
        "already inspected. Same-day baskets are dependent. Symbol selection, "
        "size and exit remain unknown. No profitability inference."
    )
    probe.save(split_path, split)
    for phase, part in split["phases"].items():
        print(phase, part["days"][0], part["days"][-1])
        print("sample day counts", dict(Counter(probe.day(p["opened"]) for p in part["sample"])))


if __name__ == "__main__":
    main()
