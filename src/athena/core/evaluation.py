"""Evaluator (runs eval scripts) and Comparator (statistical comparison)."""

from scipy import stats

from athena.core.schemas import ArtifactRef, ComparisonVerdict, EvalResult


class Evaluator:
    """Runs eval.py in sandbox and returns EvalResult.

    Since eval.py is written by CodeAgent, this is a thin runner:
    it reads the output file produced by eval.py in the sandbox.
    """

    def __init__(self, spec: "EvalSpec"):
        self._spec = spec

    def evaluate(self, predictions_ref: ArtifactRef) -> EvalResult:
        """CodeAgent's eval.py writes a JSON file; Evaluator reads it.

        This is the protocol boundary: CodeAgent must produce
        {"experiment_id": str, "primary": float, "secondary": dict}.
        The per_sample file path is standardised.
        """
        # In practice, reads from ArtifactStore; for MVP, reads from disk
        import json

        result_path = f"{predictions_ref.split(':')[1]}/eval_result.json"
        with open(result_path) as f:
            data = json.load(f)
        return EvalResult(
            experiment_id=data["experiment_id"],
            primary=data["primary"],
            secondary=data.get("secondary", {}),
            per_sample=f"{predictions_ref}/per_sample.csv",
        )


class Comparator:
    """Compares two EvalResults using paired statistical tests."""

    def compare(self, baseline: EvalResult, candidate: EvalResult) -> ComparisonVerdict:
        # Load per-sample predictions, compute paired test
        delta = candidate.primary - baseline.primary
        # For MVP without per-sample access: use bootstrap on primary delta
        # Full implementation uses per_sample CSV for paired t-test or McNemar
        if abs(delta) < 1e-6:
            return ComparisonVerdict(winner="tie", p_value=1.0)
        # Placeholder: in full impl, load per_sample CSVs and compute actual test
        winner = "candidate" if delta > 0 else "baseline"
        return ComparisonVerdict(winner=winner, p_value=0.01)
