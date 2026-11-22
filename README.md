# IntelliHRTrack_capstone_project_2026

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
