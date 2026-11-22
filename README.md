# IntelliHRTrack_capstone_project_2026

## Application architecture

### Production IntelliHRTrack

The operational application covers HR administration, employees, biometrics,
attendance, DTR, leave, payroll, and Workforce Analytics using
organization-specific IntelliHRTrack data. Workforce absenteeism analytics are
rule-based and explainable. The production application does not run or display
external-dataset ML predictions, and ML research never changes payroll, DTR,
attendance status, leave approval, employee risk scoring, or finalization.

### Capstone ML research

The separate research layer uses the external public UCI Absenteeism at Work
dataset for supervised model comparison, grouped/nested cross-validation,
evaluation metrics, duplicate-sensitivity analysis, and limitations reporting.
Its experiment code and reproducible JSON/Markdown evidence support the
capstone methodology, results chapter, paper, and defense only. No research
model is deployed for IntelliHRTrack employee inference.

The read-only research presentation is available only by its restricted URL to
superusers. It is intentionally absent from ordinary operational navigation.

The separately gated production advisory pipeline is documented in
[`docs/production_ml.md`](docs/production_ml.md). It remains inactive until
eligible IntelliHRTrack history passes the temporal readiness gate and a
validated artifact is explicitly activated.

Current production status: no ML model is active. The latest real assessment
found insufficient eligible historical target periods and temporal/group
validation coverage, so training and activation remain blocked. Production ML
uses only IntelliHRTrack-compatible employee-period data, predicts a qualifying
unprotected exact `Absent` occurrence in the next eligible period, and is
advisory only. It never modifies payroll, DTR, attendance, leave, discipline,
or employment outcomes. Measured UCI research metrics remain research-only and
are never presented as production performance.

## Analytics data layers

- **Operational DTR classification** is the deterministic source of truth for
  HR and payroll. Its attendance statuses and payroll consequences are not
  changed by analytics or ML eligibility decisions.
- **Rule-based analytics** is a current-period, explainable risk indicator. It
  is not machine learning and does not determine payroll or misconduct.
- **ML raw supervised observation** is a technically constructible historical
  feature-period to next-period target pair.
- **ML-eligible observation** is a raw observation that separately passes
  attendance-source coverage, supported-cadence, strict chronology, completed
  target-period, and deterministic-label gates in `core.analytics.quality`.

The descriptive analytics `Good`/`Review`/`Poor` score remains separate from
ML eligibility. Employment type and department currently use the present
`UserProfile` values rather than historical dimension snapshots; eligibility
metadata reports this limitation for future scientific evaluation.

## Phase 2C public-dataset research evidence

Superusers can review the completed UCI absenteeism evaluation at
`/admin-ui/analytics/ml-research/`. The page is read-only and loads the
committed `research_results/phase2c_uci_absenteeism/uci_absenteeism_phase2c_results.json`
artifact; it does not train models, run inference, or query IntelliHRTrack
employee, attendance, DTR, or payroll records.

The UCI results are external research evidence only. No production ML model is
deployed, and the findings must not be used for payroll, discipline, employee
scoring, or automated HR decisions. The raw UCI CSV remains local-only under
the ignored `local_data/` directory.
