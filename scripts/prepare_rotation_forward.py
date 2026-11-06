"""Default: roll back a preview. --apply: repair audited rung, with services stopped."""

import argparse
import json
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from lab.ops.rotation_forward import prepare_rotation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        with Session(engine) as session:
            result = prepare_rotation(session, apply=args.apply)
            if args.apply:
                session.commit()
            else:
                session.rollback()
        print(json.dumps(result, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
