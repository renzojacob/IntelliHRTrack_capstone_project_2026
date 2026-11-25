import datetime as dt
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from core.tests.factories import admin, branch, employee, punch


class AdminDashboardAttendanceTests(TestCase):
    def setUp(self):
        self.branch = branch()
        self.admin = admin("branch-admin", self.branch)
        self.employee = employee("employee", self.branch, bio_id="1001")
        self.client.force_login(self.admin)

    @patch("core.views.timezone.localdate")
    def test_old_checkin_is_not_counted_as_present_today(self, localdate):
        today = dt.date(2026, 11, 25)
        localdate.return_value = today
        punch(self.employee, today - dt.timedelta(days=1), "08:00", "in")

        response = self.client.get(reverse("admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["dashboard_data"]["present"], 0)

    @patch("core.views.timezone.localdate")
    def test_today_checkin_is_counted_as_present(self, localdate):
        today = dt.date(2026, 11, 25)
        localdate.return_value = today
        punch(self.employee, today, "08:00", "in")

        response = self.client.get(reverse("admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["dashboard_data"]["present"], 1)
