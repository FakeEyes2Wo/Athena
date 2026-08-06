from athena.code.backends.base import (
    ExecutionOutput as BackendExecutionOutput,
    GenerationResult as BackendGenerationResult,
)
from athena.code.types import EngineResult, ExecutionOutput, GenerationResult


def test_backend_reexports_canonical_execution_output():
    assert BackendExecutionOutput is ExecutionOutput


def test_backend_reexports_canonical_generation_result():
    assert BackendGenerationResult is GenerationResult


def test_engine_result_defaults_to_unsuccessful():
    assert EngineResult().success is False


def test_execution_output_default_files_are_isolated():
    first = ExecutionOutput(stdout="", stderr="", returncode=0)
    second = ExecutionOutput(stdout="", stderr="", returncode=0)

    first.files.append("output.txt")

    assert second.files == []


def test_engine_result_default_files_are_isolated():
    first = EngineResult()
    second = EngineResult()

    first.files.append("result.txt")

    assert second.files == []


def test_generation_result_default_lists_are_isolated():
    first = GenerationResult()
    second = GenerationResult()

    first.files_created.append("created.py")
    first.files_modified.append("modified.py")

    assert second.files_created == []
    assert second.files_modified == []
