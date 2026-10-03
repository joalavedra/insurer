"""Product wording and configuration loading."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PRODUCT_PATH = (
    Path(__file__).resolve().parents[2] / "products" / "agent_spend_cover.yaml"
)
PACKAGED_PRODUCT_PATH = Path(__file__).with_name("agent_spend_cover.yaml")


@dataclass(frozen=True)
class Product:
    raw: dict[str, Any]

    @property
    def product_id(self) -> str:
        return str(self.raw["id"])

    @property
    def version(self) -> int:
        return int(self.raw["version"])

    @property
    def coverage(self) -> dict[str, Any]:
        return self.raw["coverage"]

    @property
    def rating(self) -> dict[str, Any]:
        return self.raw["rating"]

    @property
    def simulation(self) -> dict[str, Any]:
        return self.raw["simulation"]

    @property
    def product_key(self) -> str:
        return f"{self.product_id}@{self.version}"


def load_product(path: str | Path | None = None) -> Product:
    product_path = (
        Path(path)
        if path is not None
        else PRODUCT_PATH
        if PRODUCT_PATH.exists()
        else PACKAGED_PRODUCT_PATH
    )
    with product_path.open(encoding="utf-8") as source:
        raw = yaml.safe_load(source)
    if not isinstance(raw, dict):
        raise ValueError("product YAML must contain a mapping")
    return Product(raw=raw)
