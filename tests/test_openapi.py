"""OpenAPI contract validation tests."""

from __future__ import annotations

from pathlib import Path

import yaml
from openapi_spec_validator import validate


def test_openapi_spec_is_valid():
    spec_path = Path(__file__).resolve().parent.parent / "openapi.yml"
    with spec_path.open("r", encoding="utf-8") as handle:
        spec = yaml.safe_load(handle)

    validate(spec)
    assert "/v1/ocr" in spec["paths"]
    assert "/v1/ocr/drive" in spec["paths"]
    assert "/v1/jobs" in spec["paths"]
    assert "/healthz" in spec["paths"]
