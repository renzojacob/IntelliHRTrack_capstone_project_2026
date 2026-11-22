# Phase 2C public research experiment

This package is intentionally separate from IntelliHRTrack's production
analytics pipeline. It does not import `core.analytics.features` or
`core.analytics.labels`, access Django models, write attendance, calculate DTRs,
or alter payroll.

After installing `requirements.txt`, run:

```text
python manage.py test core.tests.test_uci_research_experiment --settings=config.test_settings
python manage.py run_uci_absenteeism_experiment
```

To update interpretation/reporting from an existing measured JSON without
training any model, run:

```text
python manage.py regenerate_uci_absenteeism_reports
```

The command verifies the Phase 2B source SHA-256, retains exact duplicates for
the primary nested employee-group-aware experiment, repeats the evaluation with
exact duplicates removed, and writes only these report artifacts:

```text
research_results/phase2c_uci_absenteeism/uci_absenteeism_phase2c_results.json
research_results/phase2c_uci_absenteeism/uci_absenteeism_phase2c_report.md
```

It does not serialize a trained model. The public UCI results are not validated
for IntelliHRTrack employees and must not be integrated into production payroll,
DTR, attendance, or prediction behavior.

## Production and research separation

Production IntelliHRTrack provides HR administration, organization-specific
attendance and biometrics, DTR, leave, payroll, and operational rule-based
Workforce Analytics. It does not use the external UCI classifiers for employee
predictions or operational decisions.

The capstone research layer retains the dummy baseline, Logistic Regression,
Decision Tree, Random Forest, and Gradient Boosting comparison; grouped nested
cross-validation; Accuracy, Precision, Recall, F1, Average Precision/PR-AUC,
and ROC-AUC; sensitivity analysis; and research limitations. These results are
evidence for the capstone paper, methodology, results chapter, and defense.

The artifact-driven presentation remains at
`/admin-ui/analytics/ml-research/`. It is superuser-only, read-only, absent from
normal application navigation, and performs neither training nor inference
during web requests.
