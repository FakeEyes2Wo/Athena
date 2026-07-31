```python
import pandas as pd

# Path to the predictions file
predictions_path = r'C:\Users\YeBai\Athena\demo_work/predictions.csv'

# Read the predictions CSV
pred_df = pd.read_csv(predictions_path)

# Locate the PassengerId column
passenger_col = None
for col in pred_df.columns:
    if col.strip().lower() == 'passengerid':
        passenger_col = col
        break
if passenger_col is None:
    raise ValueError("No 'PassengerId' column found in predictions.csv")

# Locate the Survived / probability column
survived_col = None
for col in pred_df.columns:
    if col.strip().lower() == 'survived':
        survived_col = col
        break
if survived_col is None:
    # Try to find a single numeric column whose values are probabilities or binary
    numeric_candidates = []
    for col in pred_df.columns:
        if col == passenger_col:
            continue
        numeric = pd.to_numeric(pred_df[col], errors='coerce')
        if numeric.dropna().between(0, 1).all() and numeric.notna().sum() > 0:
            numeric_candidates.append(col)
    if len(numeric_candidates) == 1:
        survived_col = numeric_candidates[0]
    elif len(numeric_candidates) > 1:
        # Prefer a column with exactly binary 0/1 values
        binary = [c for c in numeric_candidates if pd.to_numeric(pred_df[c], errors='coerce').dropna().isin([0, 1]).all()]
        survived_col = (binary or numeric_candidates)[0]
    else:
        raise ValueError("No suitable 'Survived' column found in predictions.csv")

# Extract passenger IDs and convert survival predictions to numeric
passenger_ids = pred_df[passenger_col]
survived = pd.to_numeric(pred_df[survived_col], errors='coerce')

# Convert to binary 0/1
if survived.dropna().isin([0, 1]).all():
    survived = survived.fillna(0).astype(int)
else:
    # Assume the column contains probabilities; threshold at 0.5
    survived = (survived >= 0.5).astype(int)

# Build submission DataFrame
submission = pd.DataFrame({
    'PassengerId': passenger_ids,
    'Survived': survived
})
submission['Survived'] = submission['Survived'].astype(int)

# Write submission.csv
submission.to_csv('submission.csv', index=False)
```