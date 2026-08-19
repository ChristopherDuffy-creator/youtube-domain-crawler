from __future__ import annotations

import json
import os

# `railway run --service Postgres` injects the database service variables into
# this GitHub runner. The normal DATABASE_URL points at Railway's private network,
# which is unreachable from GitHub Actions, so prefer the public TCP URL before
# importing app.database (where the SQLAlchemy engine is created).
if os.getenv("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.environ["DATABASE_PUBLIC_URL"]

from app.commoncrawl_prefilter import run_commoncrawl_prefilter  # noqa: E402
from app.database import SessionLocal  # noqa: E402


def main() -> None:
    batch_size = int(os.getenv("COMMONCRAWL_PREFILTER_BATCH_SIZE", "10"))
    index_count = int(os.getenv("COMMONCRAWL_PREFILTER_INDEX_COUNT", "2"))
    with SessionLocal() as db:
        counters = run_commoncrawl_prefilter(
            db,
            batch_size=batch_size,
            index_count=index_count,
        )
    print(json.dumps(counters, sort_keys=True))


if __name__ == "__main__":
    main()
