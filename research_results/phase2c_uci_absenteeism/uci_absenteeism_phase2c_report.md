# Phase 2C: UCI Absenteeism Public Research Experiment

## Objective

Compare a prior-probability baseline with Logistic Regression, Decision Tree, Random Forest, and Gradient Boosting using employee-group-aware nested cross-validation.

## Dataset

- Source SHA-256: `41930631AA5B14F91FDE29AE595CEFAD2BEAC464DDF837BF8150487E40038320`
- Raw rows: 740
- Invalid-month exclusions: 3
- Primary usable rows: 737
- Primary employee groups: 34
- Positive class: 271 (0.367707)
- Negative class: 466
- Exact duplicates: retained in the primary analysis

## Target and predictors

Target: `UCI_EIGHT_PLUS_ABSENCE_HOURS` — 1 if Absenteeism time in hours >= 8; else 0.

Predictors:

- `Month of absence`
- `Day of the week`
- `Transportation expense`
- `Distance from Residence to Work`
- `Service time`

`ID` was used only for grouping. Raw absenteeism hours, reason, disciplinary failure, and every non-allowlisted field were excluded from the feature matrix.

## Evaluation methodology

- Outer CV: 5-fold StratifiedGroupKFold(shuffle=True)
- Inner CV: 4-fold StratifiedGroupKFold(shuffle=True)
- Inner selection metric: average_precision
- Random seed: 20261122
- Every fold was checked for zero employee-ID overlap.
- All preprocessing and hyperparameter selection occurred inside training folds.

## Fold verification

| Fold | Train rows | Test rows | Train employees | Test employees | Train prevalence | Test prevalence | Group overlap |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 594 | 143 | 29 | 5 | 0.365320 | 0.377622 | 0 |
| 2 | 595 | 142 | 25 | 9 | 0.363025 | 0.387324 | 0 |
| 3 | 582 | 155 | 27 | 7 | 0.376289 | 0.335484 | 0 |
| 4 | 593 | 144 | 25 | 9 | 0.352445 | 0.430556 | 0 |
| 5 | 584 | 153 | 30 | 4 | 0.381849 | 0.313725 | 0 |

## Model comparison

| Model | Recall mean±std | F1 mean±std | AP mean±std | Precision mean±std | Accuracy mean±std | ROC-AUC mean±std |
|---|---:|---:|---:|---:|---:|---:|
| Dummy prior baseline | 0.000000 ± 0.000000 | 0.000000 ± 0.000000 | 0.368942 ± 0.040934 | 0.000000 ± 0.000000 | 0.631058 ± 0.040934 | 0.500000 ± 0.000000 |
| Logistic Regression | 0.541420 ± 0.205095 | 0.459423 ± 0.099974 | 0.533053 ± 0.053405 | 0.480501 ± 0.108379 | 0.575354 ± 0.049535 | 0.642113 ± 0.060824 |
| Decision Tree | 0.323370 ± 0.096566 | 0.373113 ± 0.060644 | 0.425899 ± 0.042220 | 0.486715 ± 0.115396 | 0.603583 ± 0.093027 | 0.536997 ± 0.049776 |
| Random Forest | 0.566920 ± 0.248907 | 0.449987 ± 0.123264 | 0.444415 ± 0.074131 | 0.410184 ± 0.098655 | 0.533606 ± 0.095346 | 0.547277 ± 0.069734 |
| Gradient Boosting | 0.355030 ± 0.230781 | 0.353344 ± 0.177012 | 0.468219 ± 0.036510 | 0.597888 ± 0.230936 | 0.606372 ± 0.081347 | 0.599201 ± 0.064054 |

## Scientific interpretation

- Original preimplemented rule selection: **Random Forest**
- Primary recall leader: **Random Forest**
- Strongest overall discrimination: **Logistic Regression**
- Exact-duplicate sensitivity winner: **Decision Tree**
- Robust single winner: **None**

The primary grouped analysis identified Random Forest as the recall leader, while Logistic Regression produced the strongest overall discrimination and precision-recall performance. The recall difference was small relative to fold-level variability. Because model ranking changed under exact-duplicate sensitivity analysis and fold-level variability was substantial, the experiment did not establish a robust single superior classifier.

The original Random Forest selection is retained as the output of the preimplemented rule; it is not presented as proof of universal scientific superiority.

## Pooled original-rule-selected-model performance

Original rule selected model: **Random Forest**.

Selection rule: Retain models within 0.02 absolute mean recall of the best; then within 0.01 mean F1 of the best retained model; select by mean Average Precision, then lower AP variance and lower complexity.

- Recall: 0.568266
- Precision: 0.403141
- F1: 0.471669
- Average Precision: 0.406277
- Accuracy: 0.531886
- ROC-AUC: 0.545730
- Confusion matrix `[[TN, FP], [FN, TP]]`: `[[238, 228], [117, 154]]`

## Duplicate sensitivity

- Deduplicated usable rows: 703
- Deduplicated selected model: Decision Tree
- Material conclusion change: true
- Materiality rule: Material if selected model changes or absolute mean Recall, F1, or AP changes by more than 0.05 for the primary selected model.
- Differences for the primary selected model (deduplicated minus primary): mean_average_precision=+0.022381, mean_f1=-0.035958, mean_recall=-0.146367

## Limitations

- Genuine chronology cannot be reconstructed from the source CSV.
- Employee group sizes are uneven.
- Exact duplicate rows cannot be distinguished from genuine repeated events because exact dates are absent.
- This is a small external research dataset; performance uncertainty across employee groups must be retained.

## External-validity disclaimer

This external UCI research dataset does not represent IntelliHRTrack employees, branches, payroll periods, attendance devices, or organizational policies. Results are not validated for IntelliHRTrack production use.

This experiment is not connected to IntelliHRTrack payroll, DTR, attendance ingestion, production labels, or production predictions.
