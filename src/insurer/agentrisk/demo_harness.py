from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


def harness_host() -> str:
    return os.environ.get("HARNESS_HOST") or "127.0.0.1"


def run_harness(app: Any, run_server: Callable[..., Any]) -> None:
    run_server(app, host=harness_host(), port=8000)
