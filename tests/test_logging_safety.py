import importlib
import logging


def test_http_client_request_logs_are_not_info_level():
    module = importlib.import_module("sitecustomize")
    importlib.reload(module)

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
