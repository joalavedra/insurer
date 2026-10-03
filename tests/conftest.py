from typing import Any

import pytest

from insurer.products import Product, load_product


@pytest.fixture
def product() -> Product:
    return load_product()


@pytest.fixture
def profile() -> dict[str, Any]:
    return {
        "approval_threshold": "eur_200",
        "merchant_allowlist": True,
        "rail": "card",
        "tenure_months": 6,
        "monthly_spend_cap_cents": 30000,
        "kill_switch": True,
    }
