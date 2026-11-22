# IntelliHRTrack Production Advisory ML

## 1. Production ML System Overview

Production ML is an additive, read-only Workforce Analytics layer trained only
from eligible IntelliHRTrack employee-period history. It predicts
`NEXT_PERIOD_QUALIFYING_ABSENCE`; it never uses UCI observations or research
metrics. Deterministic DTR, attendance, leave, payroll, statutory deductions,
and finalization remain authoritative.

## 2. ML Lifecycle

The lifecycle is explicit: build and assess a private dataset, train four
candidates only when readiness passes, verify stored evaluation evidence, and
activate one validated registry entry through a management command. Training
and activation never occur in a web request.

## 3. Data Collection

The builder reads approved non-staff profiles, configured payroll periods,
existing payroll rules, and deterministic DTR output. It does not create or
change operational records. Private exports use pseudonymous group tokens and
live under the ignored server-side `private_ml_artifacts/` root.

## 4. Target Definition

The target is 1 when an employee has at least one qualifying, unprotected exact
`Absent` DTR day in the immediately following eligible payroll period, else 0.
Approved Leave, Official Business, Official Travel, verified
Holiday/Suspension, Weekend, Outside Employment Period, and other statuses
already protected by deterministic DTR classification do not become positive
labels. The label is not a misconduct or disciplinary determination.

## 5. Feature Engineering

Numerical predictors are prior-period attendance rate, absences, late days and
minutes, undertime days and minutes, incomplete DTR count, consecutive absence
count, Monday/Friday absence count, average worked hours, approved leave days,
travel days, holiday/suspension days, and the rule-based risk score.
Employment type and department are categorical. Names, usernames, email,
salary, payroll values, deductions, biometric IDs, employee IDs, protected
attributes, and target-period data are excluded. Employee IDs exist only as
in-memory grouping metadata and are pseudonymized in private exports.

## 6. Readiness Gate

Readiness requires eligible source-covered observations, both target classes,
multiple employees, at least three completed target periods, strict period
chronology, unique employee-period observations, the exact feature allowlist,
both classes in earlier training and later evaluation data, and at least one
valid expanding temporal validation fold. Failures return stable reason codes;
there is no row-count-only override and no random-split fallback.

## 7. Training

`train_absenteeism_models` constructs Logistic Regression, Decision Tree,
Random Forest, and Gradient Boosting scikit-learn pipelines. Numerical values
are median-imputed and Logistic Regression is scaled. Categorical values are
imputed and one-hot encoded with unknown-category handling. Class weights are
tuned where supported. SMOTE is not applied.

## 8. Temporal Validation

The latest completed target period is held out for final evaluation. Earlier
periods supply expanding temporal validation folds used for tuning and
threshold selection. The final holdout is never used for either purpose.
Ordinary random row splitting is prohibited.

## 9. Model Evaluation

Accuracy, Precision, Recall, F1, Average Precision/PR-AUC, ROC-AUC when defined,
and confusion matrices are recorded. Validation-fold means and population
standard deviations are retained separately from final holdout metrics.

| Model | Accuracy | Precision | Recall | F1 | PR-AUC | ROC-AUC | CV Mean | CV SD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Logistic Regression | Not measured — insufficient eligible IntelliHRTrack historical data. | — | — | — | — | — | — | — |
| Decision Tree | Not measured — insufficient eligible IntelliHRTrack historical data. | — | — | — | — | — | — | — |
| Random Forest | Not measured — insufficient eligible IntelliHRTrack historical data. | — | — | — | — | — | — | — |
| Gradient Boosting | Not measured — insufficient eligible IntelliHRTrack historical data. | — | — | — | — | — | — | — |

This table must be replaced only with genuine measured IntelliHRTrack results.
UCI metrics must never be substituted.

### Current production readiness assessment

The latest real IntelliHRTrack assessment is `NOT_READY`:

- Candidate observations: 202
- Eligible observations: 4
- Excluded observations: 198
- Eligible employees: 4
- Completed target periods: 2
- Positive targets: 2
- Negative targets: 2
- Temporal validation folds: 0
- Final holdout available: No
- Active production algorithm: `NONE`

Readiness failures are `INSUFFICIENT_TARGET_PERIODS`,
`INSUFFICIENT_TEMPORAL_VALIDATION_DATA`, and
`INSUFFICIENT_EMPLOYEE_GROUPS`. Source-quality exclusions include
`NO_FEATURE_ATTENDANCE_COVERAGE`: 186 and
`NO_TARGET_ATTENDANCE_COVERAGE`: 184. Exclusion reason counts overlap and must
not be summed to derive the 198 excluded observations.

Known limitation: `CURRENT_PROFILE_CATEGORICAL_VALUES_NOT_HISTORICAL`.

Current production metrics: **Not measured — insufficient eligible
IntelliHRTrack historical data.** No algorithm is a production winner.

The latest private dataset reports `source_code_version = 42eba17` because the
production ML implementation was uncommitted when that dataset was built. The
builder obtains this value dynamically with `git rev-parse --short HEAD`; the
recorded value must not be manually rewritten. A later post-commit dataset
build may naturally record the newer commit SHA.

## 10. Model Selection

The policy prioritizes mean Recall, then F1, then PR-AUC and stability. Models
within 0.02 Recall and 0.01 F1 remain candidates. A model is a robust winner
only when it is unique and its Recall margin exceeds its fold variability;
otherwise selection returns `NO_ROBUST_WINNER`. Accuracy alone never selects a
model. Thresholds are chosen only from earlier validation data.

## 11. Artifact Storage

Trusted locally generated joblib bundles are saved outside static and media
roots. Each bundle includes its fitted pipeline, schema, target, version,
threshold, risk bands, explanation metadata, and training metadata. The
registry stores a SHA-256 checksum. Loading rejects missing, corrupt,
wrong-purpose, wrong-version, wrong-schema, or path-traversing artifacts.
User-uploaded serialized models are not accepted.

## 12. Registry and Activation

`MLModelRegistry` uses CANDIDATE, VALIDATED, ACTIVE, RETIRED, and FAILED states.
Activation accepts only a checksum-verified VALIDATED entry and transactionally
retires any previous ACTIVE entry. `MLLifecycleEvent` stores aggregate-only
dataset, readiness, training, evaluation, activation, failure, and retirement
events. Management commands record system provenance rather than inventing a
Django user.

## 13. Prediction Service

`core.analytics.production_ml.prediction.predict_absenteeism_risk` locates or
accepts the single ACTIVE registry entry, verifies its artifact, builds the
latest eligible prior-period feature snapshot, returns probability, binary
decision, risk band, model/version, periods, relevant indicators, and quality
warnings. It performs no writes and never trains.

## 14. Workforce Analytics Integration

`/admin-ui/analytics/` retains all rule-based cards and tables. An additional
advisory card shows either active-model health and aggregate predictions or the
latest real readiness failure. Employee rows show ML probability only when an
active artifact and eligible snapshot exist. UCI metrics never appear here.

## 15. Monitoring / Staleness

The registry exposes training time, dataset time, sample size, validation
metrics, checksum health, and model status. Operators should treat old models,
missing artifacts, schema mismatches, or new quality failures as unavailable.
No silent fallback to an unverified artifact is allowed.

## 16. Retraining

Retraining is an explicit management command after a fresh readiness and
dataset assessment. A newly validated candidate does not automatically replace
the active model. Evaluation and activation remain separate auditable steps.

## 17. Privacy

Exports are private and ignored by Git. Direct employee identifiers are not
predictors and are replaced with one-way keyed group tokens in exported data.
Lifecycle events contain aggregate counts and reason codes, not feature vectors
or employee identities. Department aggregates require at least three available
predictions.

## 18. Ethical Limitations

The probability is not a diagnosis, causal explanation, misconduct finding, or
disciplinary recommendation. Historical patterns may encode operational bias,
missing records, or changing policy. Current employment type and department are
not historical dimension snapshots and remain a documented limitation.

## 19. Human Oversight

Authorized HR users must review data quality and context. Predictions are
decision-support indicators only and cannot automatically change attendance,
payroll, deductions, leave, discipline, employment status, or finalization.

## 20. Production vs UCI Research Separation

Production artifacts are trained exclusively from eligible IntelliHRTrack
employee-period observations and registered with the production purpose/schema.
The UCI experiment remains in `core.analytics.research`, uses separate results,
and cannot satisfy production readiness, populate production metrics, enter the
production registry, or score IntelliHRTrack employees.

## Commands

```text
python manage.py build_ml_dataset [--as-of YYYY-MM-DD] [--output relative/path.json]
python manage.py train_absenteeism_models
python manage.py evaluate_absenteeism_models [registry-id-or-version]
python manage.py activate_ml_model <registry-id-or-version> [--actor-label LABEL]
```

Install direct production artifact support with:

```text
pip install -r requirements-production-ml.txt
```
