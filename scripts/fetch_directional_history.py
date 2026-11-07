"""Archive resumable position pages for #39; keep API failures and raw responses."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from binance_lead_history import BASE, HEADERS

PID = "5095694990932973056"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--page-size", type=int, choices=(50, 200), default=50)
    args = parser.parse_args()
    pages = args.root / (
        "directional_pages_2026-11-07"
        if args.page_size == 50
        else "directional_pages200_2026-11-07"
    )
    pages.mkdir(exist_ok=True)
    failures, rows, totals = [], {}, []
    with httpx.Client(headers=HEADERS, timeout=30) as client:
        detail_path = pages / "detail.json"
        if not detail_path.exists():
            response = client.get(f"{BASE}/detail", params={"portfolioId": PID})
            response.raise_for_status()
            save(detail_path, response.json())
        detail = json.loads(detail_path.read_text(encoding="utf-8"))
        for page in range(1, 201):
            path = pages / f"page_{page:03d}.json"
            if path.exists():
                record = json.loads(path.read_text(encoding="utf-8"))
            else:
                body = dict(portfolioId=PID, pageNumber=page, pageSize=args.page_size)
                for attempt in range(6):
                    record = dict(body=body, fetched_at=datetime.now(UTC).isoformat())
                    try:
                        response = client.post(f"{BASE}/position-history", json=body)
                        response.raise_for_status()
                        record["response"] = response.json()
                        data = record["response"].get("data")
                        if not record["response"].get("success") or not isinstance(data, dict):
                            raise ValueError("API unsuccessful or null data")
                        if not isinstance(data.get("list"), list) or "total" not in data:
                            raise ValueError("Invalid position page")
                        break
                    except (httpx.HTTPError, ValueError) as exc:
                        record["error"] = str(exc)
                        save(pages / f"error_{page:03d}_{time.time_ns()}.json", record)
                        time.sleep(5 * (attempt + 1))
                else:
                    failures.append(dict(page=page, error=record.get("error")))
                    break
                save(path, record)
                time.sleep(1)
            data = record["response"]["data"]
            totals.append(data["total"])
            for row in data["list"]:
                rows[row["positionId"]] = row
            print(f"page={page} unique={len(rows)} total={data['total']}", flush=True)
            if not data["list"] or len(rows) >= data["total"]:
                break
    complete = bool(totals) and len(set(totals)) == 1 and len(rows) == totals[0] and not failures
    result = dict(
        portfolio_id=PID,
        fetched_at=datetime.now(UTC).isoformat(),
        detail=detail.get("data"),
        total=totals[-1] if totals else None,
        observed_totals=totals,
        complete=complete,
        failures=failures,
        positions=sorted(rows.values(), key=lambda r: r["opened"]),
    )
    save(args.root / f"directional_{PID}_2026-11-07.json", result)
    print(f"complete={complete} positions={len(rows)} failures={failures}", flush=True)


if __name__ == "__main__":
    main()
