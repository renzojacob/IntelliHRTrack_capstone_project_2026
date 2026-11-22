from django.test import SimpleTestCase

from core.analytics.labels import build_unprotected_absence_target


class UnprotectedAbsenceTargetTests(SimpleTestCase):
    def target(self, status, **extra):
        row = {"date": "2026-02-02", "status": status, **extra}
        return build_unprotected_absence_target({"rows": [row]})

    def test_protected_statuses_do_not_create_positive_target(self):
        for status in (
            "Approved Leave",
            "Official Business",
            "Official Travel",
            "Holiday/Suspension",
            "Weekend",
            "Outside Employment Period",
        ):
            with self.subTest(status=status):
                self.assertEqual(self.target(status), 0)

    def test_unprotected_absent_row_creates_positive_target(self):
        self.assertEqual(self.target("Absent"), 1)

    def test_unverified_holiday_metadata_cannot_protect_an_absent_row(self):
        # The deterministic DTR builder emits Holiday/Suspension only for a
        # verified record.  If it emits Absent, analytics must not override it
        # based on unverified holiday metadata or create a second rule engine.
        self.assertEqual(self.target("Absent", holiday_verified=False), 1)

    def test_empty_or_malformed_history_is_safe(self):
        self.assertEqual(build_unprotected_absence_target({"rows": []}), 0)
        self.assertEqual(build_unprotected_absence_target(None), 0)
        self.assertEqual(build_unprotected_absence_target({"rows": [None, {}]}), 0)
