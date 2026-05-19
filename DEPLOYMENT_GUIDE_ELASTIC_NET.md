# Deployment Guide: Elastic Net 50-Feature Model

This guide explains how to properly deploy the Elastic Net 50-feature model (`elastic_net_3s_50_features`) to your production GUI/inference environment. 

## The Problem with Direct Deployment
Initially, the model was saved with standard Scikit-Learn artifacts:
- `elastic_net_model_pipeline.pkl` (The model)
- `elastic_net_model_label_encoder.pkl` (The label encoder)
- `elastic_net_model_selected_features.json` (The feature list)

**CRITICAL ISSUE**: If you try to directly load the `.pkl` into the legacy GUI, the GUI will not know how to read the `.json` file to reorder the incoming extracted features. If the features from your new `.parquet` are passed into the `StandardScaler` in the wrong order, it causes extreme mathematical anomalies (e.g., subtracting a Time parameter mean from a Frequency parameter value), creating massive Z-scores and outputting `99.8% Class 2` by accident.

## How to Standardize the Deployment

To deploy this model seamlessly into the legacy architecture (matching the old `standalone_ensemble_noisy` script expectations), the deployment artifacts must be converted or exported as:
1. `weighted_ensemble_model.joblib`
2. `required_features.txt`

### Option 1: Using the Pre-Converted Artifacts (Recommended)
We have already generated a native GUI-compatible version of this exact 50-feature model without the `LabelEncoder` dependency, and packaged it identically to legacy models.

1. Navigate to `/root/Krak_Repo/KRAK-analyses-Chips/KRAK-analyses-Chips/model_deployment/`
2. Copy the two generated files directly into your target deployment machine/GUI folder:
   - `weighted_ensemble_model.joblib`
   - `required_features.txt`
3. When the GUI loads `required_features.txt`, it will guarantee that your Pandas DataFrame columns are re-indexed to match the exact 1-to-50 order the Elastic Net was trained on.

### Option 2: Deploying the .pkl format manually
If you MUST use the original `.pkl` and `.json` files from the `elastic_net_3s_50_features` folder, your deployment `inference.py` or GUI **MUST** explicitly sort the dataframe columns before calling `predict_proba`:

```python
import pandas as pd
import joblib
import json

# 1. Load the artifacts
pipeline = joblib.load("elastic_net_model_pipeline.pkl")
label_encoder = joblib.load("elastic_net_model_label_encoder.pkl")

with open("elastic_net_model_selected_features.json", "r") as f:
    required_features = json.load(f)

# 2. Extract your features into a DataFrame
# X_test_raw = pd.DataFrame(extracted_features)

# 3. CRITICAL: Reorder the columns to match training exactly!
# If a feature is missing, this will throw an error rather than silently failing.
X_test_aligned = X_test_raw[required_features]

# 4. Predict
probabilities = pipeline.predict_proba(X_test_aligned)
predictions = pipeline.predict(X_test_aligned)

# 5. Decode labels
final_classes = label_encoder.inverse_transform(predictions)
```

## Summary
The wildly incorrect `0.998` confidence score previously seen on the deployment machine was due to feature misalignment. Always rely on `required_features.txt` (or parsing the `.json`) to enforce exact pandas DataFrame column ordering before running inference.