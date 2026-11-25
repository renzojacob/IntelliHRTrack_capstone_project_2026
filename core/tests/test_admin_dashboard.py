import datetime as dt
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from core.models import LeaveRequest
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

    @patch("core.views.timezone.localdate")
    def test_on_leave_today_counts_distinct_employees_with_active_approved_leave(self, localdate):
        today = dt.date(2026, 11, 25)
        yesterday = today - dt.timedelta(days=1)
        tomorrow = today + dt.timedelta(days=1)
        localdate.return_value = today
        approved = LeaveRequest.STATUS_APPROVED

        def add_leave(profile, start_date, end_date, status=approved):
            LeaveRequest.objects.create(
                employee=profile.user,
                branch=profile.branch,
                leave_type=LeaveRequest.TYPE_VACATION,
                start_date=start_date,
                end_date=end_date,
                status=status,
            )

        add_leave(self.employee, yesterday, yesterday)
        add_leave(self.employee, tomorrow, tomorrow)
        add_leave(self.employee, today, today, LeaveRequest.STATUS_PENDING)
        other_employee = employee("other-employee", branch("Other branch"), bio_id="1002")
        add_leave(other_employee, today, today)

        response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(response.context["dashboard_data"]["leave"], 0)
        self.assertEqual(response.context["dashboard_data"]["pending_leaves"], 1)

        add_leave(self.employee, yesterday, today)
        add_leave(self.employee, today, tomorrow)

        response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(response.context["dashboard_data"]["leave"], 1)
