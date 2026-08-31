"""Compatibility shim: the serving implementation lives in ``model`` / ``http_api``.

Keeps the old import path and ``python -m athena.serving.predictions_api`` entrypoint.
"""

from athena.serving.http_api import Handler, build_server, main
from athena.serving.model import (
    DEFAULT_NULLABLE_FIELDS,
    DEFAULT_REQUIRED_FIELDS,
    EXAMPLE_RECORDS,
    EXAMPLE_TRUTH,
    ID_COL,
    MAX_RECORDS,
    Booster,
    ModelBundle,
    PredictionRequest,
    ValidationError,
    create_features,
)

__all__ = [
    "ID_COL",
    "MAX_RECORDS",
    "DEFAULT_REQUIRED_FIELDS",
    "DEFAULT_NULLABLE_FIELDS",
    "EXAMPLE_RECORDS",
    "EXAMPLE_TRUTH",
    "Booster",
    "ValidationError",
    "PredictionRequest",
    "create_features",
    "ModelBundle",
    "Handler",
    "build_server",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
