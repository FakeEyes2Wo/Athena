"""The SEARCH candidate prompt must warn about inherited absolute paths.

A candidate starts from the parent experiment's working solution, copied into a
workspace with a *different* name. Every absolute path in that solution still
points at the parent. In TESS run 20260830_205138 three of eight SEARCH
experiments reported a score they did not produce, because their
``PREDICTIONS_DIR`` still named
``...\\workspaces\\athena-350b5a2fa62b4b24b507e3d53308e772\\predictions``:

    exp_hyp_bff76ba44780  0.8176555348319928  <- the baseline's number
    exp_hyp_68b274213fb5  0.8176555348319928  <- the baseline's number
    exp_hyp_6c0b25528435  0.8361448797506438  <- the SOTA's number, to the digit

The evaluator cannot tell: it scores whatever sits in ``predictions/``, and what
sat there was the inherited file. The freshness guard now fails such an
experiment rather than scoring the stale file, but the hour of compute is still
gone, so the prompt has to say it.
"""

from athena.agents.prompt_agent import load_prompt


def test_plan_prompt_says_inherited_absolute_paths_belong_to_the_parent() -> None:
    prompt = load_prompt("plan")

    assert "Every absolute path you inherit is the parent's" in prompt
    assert "relative path" in prompt
    # 不能只说"别硬编码"而不说后果：候选要知道沉默的失败是"父实验的分数被当成你的"。
    assert "parent's number is then reported as your hypothesis's result" in prompt


def test_plan_prompt_still_rejects_rerunning_the_inherited_solution() -> None:
    """The new paragraph sits beside the no_change rule; neither may displace it."""
    prompt = load_prompt("plan")

    assert "You inherit the parent experiment's working solution." in prompt
    assert "no_change" in prompt
