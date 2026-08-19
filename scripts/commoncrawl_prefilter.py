from __future__ import annotations

import json
import os

from app.commoncrawl_prefilter import run_commoncrawl_prefilter
from app.database import SessionLocal


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
