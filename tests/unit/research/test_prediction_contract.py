import pytest

from athena.research.prediction_contract import assert_predictions_cover


def _csv(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_real_csv_rows_require_exact_prediction_identity_coverage(tmp_path):
    expected = _csv(
        tmp_path / "search_features.csv",
        "__athena_row_id,flux\nrow-a,1.2\nrow-b,3.4\nrow-c,5.6\n",
    )
    predictions = tmp_path / "predictions"
    _csv(predictions / "predictions__task.csv", "id,label\nrow-c,1\nrow-a,0\nrow-b,1\n")

    assert_predictions_cover(
        predictions,
        expected,
        prediction_file="predictions__task.csv",
        prediction_id_column="id",
    )


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("id,label\na,0\n", "no prediction"),
        ("id,label\na,0\na,1\nb,0\n", "duplicate identity"),
        ("id,label\na,0\nb,1\nextra,0\n", "absent from"),
        ("id,label\n", "no identity rows"),
    ],
)
def test_rejects_incomplete_duplicate_extra_or_empty_predictions(
    tmp_path, output, message
):
    expected = _csv(tmp_path / "features.csv", "sample_id,x\na,1\nb,2\n")
    predictions = tmp_path / "predictions"
    _csv(predictions / "out.csv", output)

    with pytest.raises(ValueError, match=message):
        assert_predictions_cover(predictions, expected, prediction_id_column="id")


def test_rejects_empty_expected_features(tmp_path):
    expected = _csv(tmp_path / "features.csv", "sample_id,x\n")
    predictions = tmp_path / "predictions"
    _csv(predictions / "out.csv", "id,label\na,0\n")

    with pytest.raises(ValueError, match="expected feature file.*no rows"):
        assert_predictions_cover(predictions, expected, prediction_id_column="id")


def test_declared_prediction_file_ignores_unrelated_csv(tmp_path):
    expected = _csv(tmp_path / "features.csv", "sample_id,x\na,1\n")
    predictions = tmp_path / "predictions"
    _csv(predictions / "predictions__task.csv", "id,label\na,1\n")
    _csv(predictions / "debug.csv", "id,debug\nwrong,1\n")

    assert_predictions_cover(
        predictions,
        expected,
        prediction_file="predictions__task.csv",
        prediction_id_column="id",
    )


def test_missing_declared_identity_column_fails_with_actionable_message(tmp_path):
    expected = _csv(tmp_path / "features.csv", "sample_id,x\na,1\n")
    predictions = tmp_path / "predictions"
    _csv(predictions / "out.csv", "wrong_id,label\na,1\n")

    with pytest.raises(ValueError, match="missing declared identity column 'id'"):
        assert_predictions_cover(predictions, expected, prediction_id_column="id")


def test_non_tabular_format_is_transparently_skipped(tmp_path):
    expected = tmp_path / "opaque.bin"
    predictions = tmp_path / "predictions"
    expected.write_bytes(b"opaque")
    predictions.mkdir()

    # The caller sees a documented no-op for formats this CSV contract cannot inspect.
    assert_predictions_cover(predictions, expected, prediction_format="image_directory")


def test_legacy_contract_infers_first_identity_column(tmp_path):
    expected = _csv(tmp_path / "features.csv", "sample_key,x\na,1\nb,2\n")
    predictions = tmp_path / "predictions"
    _csv(predictions / "out.csv", "image_id,label\nb,0\na,1\n")

    assert_predictions_cover(predictions, expected)
