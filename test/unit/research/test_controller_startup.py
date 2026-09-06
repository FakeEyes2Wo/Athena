from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from athena.research.prepare.authority import BaselineAuthorityError
from athena.research.runtime import control


@pytest.mark.asyncio
async def test_preflight_failure_precedes_git_agents_and_survey(monkeypatch):
    runtime = SimpleNamespace(
        provider=object(),
        session=SimpleNamespace(lifecycle=SimpleNamespace(task=None)),
        config=SimpleNamespace(
            dependencies=SimpleNamespace(environment_repair_actions={})
        ),
        git=SimpleNamespace(init=AsyncMock()),
        agents=SimpleNamespace(start=Mock()),
    )
    check = AsyncMock(side_effect=BaselineAuthorityError("not configured"))
    survey = Mock()
    monkeypatch.setattr(control, "preflight", check)
    monkeypatch.setattr(control, "start_survey", survey)
    with pytest.raises(BaselineAuthorityError):
        await control.start(runtime)
    check.assert_awaited_once_with(runtime, {})
    runtime.git.init.assert_not_awaited()
    runtime.agents.start.assert_not_called()
    survey.assert_not_called()
    assert runtime.session.lifecycle.task is None
