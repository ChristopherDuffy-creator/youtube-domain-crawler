"""Process-wide logging safety for third-party HTTP clients.

httpx logs full request URLs at INFO. Some of our provider APIs authenticate in
query parameters, so those INFO lines can expose credentials in Railway logs.
Keep request-level client logging at WARNING while preserving application/job
INFO logs.
"""

import logging

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
