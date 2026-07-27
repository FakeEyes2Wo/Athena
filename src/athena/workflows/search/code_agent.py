"""Execute a hypothesis by generating code in an isolated Git worktree."""

from pydantic import BaseModel
from athena.core.schemas import ArtifactRef, Hypothesis, EvalSpec, EvalResult
from athena.core.gitutils.workspace import GitWorkBranch


class CodegenResult(BaseModel):
    experiment_id: str
    commit: str
    diff: ArtifactRef
    eval: EvalResult
    logs: ArtifactRef
    wall_time_s: float = 0.0


class CodeRouter:
    """Route to Codex or Qoder based on hypothesis type."""

    def route(self, hypothesis: Hypothesis) -> str:
        normalized = hypothesis.intervention.lower().replace(" ", "_")
        if "from_scratch" in normalized:
            return "codex"
        if len(hypothesis.intervention) > 200:
            return "codex"  # large changes -> Codex
        return "qoder"


class CodeAgent:
    """Execute a hypothesis by generating code in an isolated worktree."""

    def __init__(self, backend: str = "auto"):
        self.backend = backend  # "auto" | "codex" | "qoder"
        self._router = CodeRouter()

    async def execute(
        self,
        hypothesis: Hypothesis,
        parent_commit: str,
        eval_spec: EvalSpec,
        worktree: GitWorkBranch,
    ) -> CodegenResult:
        """Generate code, run in sandbox, return results.

        MVP: creates a stub model.py and eval.py, runs them.
        Full implementation dispatches to Qoder/Codex SDK.
        """
        import os, json, time, subprocess
        from uuid import uuid4

        experiment_id = f"exp_{uuid4().hex[:12]}"
        wt_path = worktree.path
        start = time.time()

        # Write eval.py based on EvalSpec
        eval_code = f"""
import json, sys
import pandas as pd
from sklearn.metrics import f1_score, accuracy_score

# Load predictions and labels
try:
    preds = pd.read_csv("predictions.csv")
    labels = pd.read_csv("labels.csv")
    y_pred = preds.iloc[:, 0]
    y_true = labels.iloc[:, 0]
    primary = float(f1_score(y_true, y_pred, average="macro"))
    secondary = {{"accuracy": float(accuracy_score(y_true, y_pred))}}
except Exception as e:
    primary = 0.0
    secondary = {{"error": str(e)}}

result = {{
    "experiment_id": "{experiment_id}",
    "primary": primary,
    "secondary": secondary,
}}
with open("eval_result.json", "w") as f:
    json.dump(result, f, indent=2)
print(f"Eval complete: primary={{primary:.4f}}")
"""
        os.makedirs(wt_path, exist_ok=True)
        with open(os.path.join(wt_path, "eval.py"), "w") as f:
            f.write(eval_code)

        # Write stub model training script
        model_code = f"""
import pandas as pd
import numpy as np
import json, os

print("Training baseline model...")
# Load data
train = pd.read_csv("train.csv")
target_col = "{eval_spec.primary.name}"

# Simple baseline: predict mean/mode
np.random.seed(42)
n = len(train)
preds = np.random.rand(n)
pd.DataFrame({{"prediction": preds}}).to_csv("predictions.csv", index=False)
pd.DataFrame({{"target": np.random.rand(n)}}).to_csv("labels.csv", index=False)
print("Training complete.")
"""
        with open(os.path.join(wt_path, "model.py"), "w") as f:
            f.write(model_code)

        # Run model training
        logs = []
        try:
            result = subprocess.run(
                ["python", "model.py"],
                cwd=wt_path,
                capture_output=True,
                text=True,
                timeout=300,
            )
            logs.append(result.stdout)
            logs.append(result.stderr)
        except subprocess.TimeoutExpired:
            logs.append("TIMEOUT: model.py exceeded 300s")

        # Run evaluation
        try:
            result = subprocess.run(
                ["python", "eval.py"],
                cwd=wt_path,
                capture_output=True,
                text=True,
                timeout=60,
            )
            logs.append(result.stdout)
        except subprocess.TimeoutExpired:
            logs.append("TIMEOUT: eval.py exceeded 60s")

        # Read eval result
        eval_result_path = os.path.join(wt_path, "eval_result.json")
        if os.path.exists(eval_result_path):
            with open(eval_result_path) as f:
                eval_data = json.load(f)
        else:
            eval_data = {
                "experiment_id": experiment_id,
                "primary": 0.0,
                "secondary": {},
            }

        elapsed = time.time() - start
        return CodegenResult(
            experiment_id=experiment_id,
            commit=parent_commit,  # updated after git commit
            diff=f"artifact://diffs/{experiment_id}",
            eval=EvalResult(
                experiment_id=experiment_id,
                primary=eval_data["primary"],
                secondary=eval_data.get("secondary", {}),
                per_sample=f"artifact://samples/{experiment_id}",
            ),
            logs=f"artifact://logs/{experiment_id}",
            wall_time_s=elapsed,
        )
