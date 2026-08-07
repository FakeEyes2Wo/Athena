"""KaggleClient using kagglehub."""

import asyncio
import os

from athena.core.contracts import ArtifactRef
from athena.integrations.kaggle.types import CompetitionInfo

try:
    import kagglehub  # noqa: F811
except ImportError:
    kagglehub = None  # kagglehub 为可选依赖，首次调用时按需安装


class KaggleClient:
    """Kaggle API adapter. Global singleton with competition-level state."""

    def __init__(self):
        self._competition: str | None = None
        self._check_auth()

    def _check_auth(self) -> None:
        kaggle_json = os.path.expanduser("~/.kaggle/kaggle.json")
        has_env = bool(
            os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY")
        )
        if not os.path.exists(kaggle_json) and not has_env:
            print(
                "Kaggle credentials not found. "
                "Place kaggle.json at ~/.kaggle/kaggle.json "
                "or set KAGGLE_USERNAME/KAGGLE_KEY env vars."
            )

    def set_competition(self, competition_id: str) -> None:
        self._competition = competition_id

    def get_competition_info(self) -> CompetitionInfo:
        if not self._competition:
            raise ValueError("No competition set. Call set_competition() first.")
        if kagglehub is None:
            raise RuntimeError("kagglehub not installed. Run: pip install kagglehub")
        try:
            info = kagglehub.competition_info(self._competition)
            return CompetitionInfo(
                name=getattr(info, "title", self._competition),
                type=getattr(info, "type", ""),
                metric=getattr(info, "evaluation_metric", ""),
                deadline=str(getattr(info, "deadline", "")),
                description=getattr(info, "description", "")[:500],
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to get competition info: {exc}")

    async def download_dataset(self, target_dir: str | None = None) -> ArtifactRef:
        if not self._competition:
            raise ValueError("No competition set.")
        target = target_dir or f"./data_analyze/{self._competition}"
        os.makedirs(target, exist_ok=True)
        if kagglehub is None:
            raise RuntimeError("kagglehub not installed. Run: pip install kagglehub")
        try:
            loop = asyncio.get_event_loop()
            path = await loop.run_in_executor(
                None,
                lambda: kagglehub.competition_download(self._competition, path=target),
            )
            return f"artifact://{path}"
        except Exception as exc:
            raise RuntimeError(f"Failed to download dataset: {exc}")
