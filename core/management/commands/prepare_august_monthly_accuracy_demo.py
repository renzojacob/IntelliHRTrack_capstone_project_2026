import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory

from core.models import (
    AttendanceRecord,
    Branch,
    EmployeeContribution,
    HolidaySuspension,
    OvertimeRequest,
    PayrollBatch,
    PayrollItem,
    PayrollPeriod,
    TravelOrder,
    UserProfile,
)
from core.views import admin_payroll_process_batch


class Command(BaseCommand):
    help = (
        "Prepare an August 1-31, 2026 MONTHLY payroll accuracy live-demo period "
        "for the existing Occidental Mindoro employees. It preserves the locked "
        "August 1-15 DTR evidence, adds varied second-half attendance, verified "
        "August holidays, approved OT, absences, late/undertime and travel, then "
        "processes but DOES NOT FINALIZE the monthly payroll so it can be "
        "reprocessed during the professor's live employment-type test."
    )

    BRANCH_NAME = "Occidental Mindoro"
    PERIOD_NAME = "Payroll Accuracy Live Demo - August 1-31 2026"
    START_DATE = date(2026, 8, 1)
    END_DATE = date(2026, 8, 31)

    SECOND_HALF_START = date(2026, 8, 17)
    SECOND_HALF_END = date(2026, 8, 31)

    MANILA = ZoneInfo("Asia/Manila")

    DEMO_MARKER = "INTELLIHRTRACK_AUGUST_MONTHLY_LIVE_DEMO_V1"
    TRAVEL_REASON = "August 2026 payroll accuracy live demo - official travel"
    OT_REASON = "August 2026 payroll accuracy live demo - approved overtime"

    HOLIDAY_SOURCE = (
        "Presidential Proclamation No. 1006, s. 2025 - "
        "Regular Holidays and Special (Non-Working) Days for 2026"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--restore-employee-oc",
            action="store_true",
            help=(
                "Restore employee_oc to JO after a live JO/COS reclassification "
                "demo and reprocess the monthly payroll."
            ),
        )

    def handle(self, *args, **options):
        branch = Branch.objects.filter(
            name__iexact=self.BRANCH_NAME
        ).first()

        if not branch:
            raise CommandError(
                f'Branch "{self.BRANCH_NAME}" was not found.'
            )

        period, _ = PayrollPeriod.objects.update_or_create(
            name=self.PERIOD_NAME,
            defaults={
                "start_date": self.START_DATE,
                "end_date": self.END_DATE,
                "pay_mode": PayrollPeriod.PAY_MONTHLY,
            },
        )

        admin_user = self._get_admin_user()

        if options["restore_employee_oc"]:
            self._restore_employee_oc(branch)
            batch = self._process_all(
                branch=branch,
                period=period,
                admin_user=admin_user,
            )

            self.stdout.write(
                self.style.SUCCESS(
                    "employee_oc restored to JO and August monthly payroll "
                    "was reprocessed."
                )
            )

            self._print_batch_summary(batch)
            return

        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "INTELLIHRTRACK - AUGUST 2026 MONTHLY PAYROLL LIVE DEMO"
            )
        )
        self.stdout.write("=" * 82)

        profiles = list(
            UserProfile.objects
            .select_related("user", "branch")
            .filter(
                branch=branch,
                is_approved=True,
                user__is_staff=False,
                user__is_superuser=False,
            )
            .order_by("user__username")
        )

        if not profiles:
            raise CommandError(
                "No approved non-admin employees were found."
            )

        self.stdout.write(f"Branch: {branch.name}")
        self.stdout.write(f"Period: {period.name}")
        self.stdout.write(f"Pay mode: {period.pay_mode}")
        self.stdout.write(f"Employees: {len(profiles)}")

        # ---------------------------------------------------------
        # 1. Real official August 2026 holidays.
        # ---------------------------------------------------------
        HolidaySuspension.objects.update_or_create(
            date=date(2026, 8, 21),
            name="Ninoy Aquino Day",
            defaults={
                "type": HolidaySuspension.TYPE_SPECIAL,
                "scope": HolidaySuspension.SCOPE_NATIONWIDE,
                "branch": None,
                "notes": (
                    "Special non-working day used in the August 2026 "
                    "controlled payroll accuracy demonstration."
                ),
                "is_payroll_verified": True,
                "source_reference": self.HOLIDAY_SOURCE,
            },
        )

        HolidaySuspension.objects.update_or_create(
            date=date(2026, 8, 31),
            name="National Heroes Day",
            defaults={
                "type": HolidaySuspension.TYPE_HOLIDAY,
                "scope": HolidaySuspension.SCOPE_NATIONWIDE,
                "branch": None,
                "notes": (
                    "Regular holiday used in the August 2026 "
                    "controlled payroll accuracy demonstration."
                ),
                "is_payroll_verified": True,
                "source_reference": self.HOLIDAY_SOURCE,
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                "PASS  Verified holidays added: Aug 21 and Aug 31"
            )
        )

        # ---------------------------------------------------------
        # 2. Configure JO/COS live comparison values.
        #
        # employee_oc is deliberately given BOTH:
        #   JO daily rate = 1000
        #   COS monthly salary = 22000
        #
        # Current payroll logic uses the explicit daily rate when JO,
        # and the monthly salary when COS. Therefore the professor can
        # change ONLY the employment type JO <-> COS and reprocess.
        # ---------------------------------------------------------
        for profile in profiles:
            changed = []

            if not (profile.biometric_employee_id or "").strip():
                profile.biometric_employee_id = f"DEMO{profile.id:06d}"
                changed.append("biometric_employee_id")

            username = profile.user.username

            if username == "employee_oc":
                if Decimal(str(profile.daily_rate or "0")) != Decimal("1000.00"):
                    profile.daily_rate = Decimal("1000.00")
                    changed.append("daily_rate")

                if Decimal(str(profile.monthly_salary or "0")) != Decimal("22000.00"):
                    profile.monthly_salary = Decimal("22000.00")
                    changed.append("monthly_salary")

                profile.employment_type = UserProfile.EMP_JO
                changed.append("employment_type")

            if profile.employment_type == UserProfile.EMP_JO:
                if Decimal(str(profile.daily_rate or "0")) <= 0:
                    profile.daily_rate = Decimal("1000.00")
                    changed.append("daily_rate")

            elif profile.employment_type == UserProfile.EMP_COS:
                if Decimal(str(profile.monthly_salary or "0")) <= 0:
                    profile.monthly_salary = Decimal("22000.00")
                    changed.append("monthly_salary")

            elif profile.employment_type == UserProfile.EMP_PERMANENT:
                if Decimal(str(profile.monthly_salary or "0")) <= 0:
                    profile.monthly_salary = Decimal("30000.00")
                    changed.append("monthly_salary")

                if Decimal(str(profile.pera_allowance or "0")) <= 0:
                    profile.pera_allowance = Decimal("2000.00")
                    changed.append("pera_allowance")

            if profile.has_premium:
                profile.has_premium = False
                changed.append("has_premium")

            if Decimal(str(profile.manual_deduction_amount or "0")) != 0:
                profile.manual_deduction_amount = Decimal("0.00")
                changed.append("manual_deduction_amount")

            if changed:
                profile.save(
                    update_fields=list(dict.fromkeys(changed))
                )

            self._configure_contributions(profile)

        # ---------------------------------------------------------
        # 3. Preserve Aug 1-15 locked evidence.
        #    Clean/recreate ONLY Aug 17-31 records created by THIS command.
        # ---------------------------------------------------------
        start_dt = datetime(
            2026, 8, 17, 0, 0,
            tzinfo=self.MANILA,
        )
        end_dt = datetime(
            2026, 9, 1, 0, 0,
            tzinfo=self.MANILA,
        )

        profile_by_bio = {
            str(p.biometric_employee_id): p
            for p in profiles
            if p.biometric_employee_id
        }

        existing_second_half = list(
            AttendanceRecord.objects.filter(
                branch=branch,
                employee_id__in=list(profile_by_bio.keys()),
                timestamp__gte=start_dt,
                timestamp__lt=end_dt,
            )
        )

        foreign = []

        for record in existing_second_half:
            raw = (
                record.raw_row
                if isinstance(record.raw_row, dict)
                else {}
            )

            if raw.get("demo_marker") == self.DEMO_MARKER:
                record.delete()
            else:
                foreign.append(record)

        if foreign:
            sample = ", ".join(
                f"{r.employee_id}@{r.timestamp}"
                for r in foreign[:5]
            )

            raise CommandError(
                "Aug 17-31 already contains attendance that was not created "
                "by this command. Nothing else was changed. "
                f"Examples: {sample}"
            )

        OvertimeRequest.objects.filter(
            profile__in=profiles,
            date__gte=self.SECOND_HALF_START,
            date__lte=self.SECOND_HALF_END,
            reason=self.OT_REASON,
        ).delete()

        TravelOrder.objects.filter(
            employee__in=profiles,
            start_date__lte=self.SECOND_HALF_END,
            end_date__gte=self.SECOND_HALF_START,
            reason=self.TRAVEL_REASON,
        ).delete()

        # ---------------------------------------------------------
        # 4. Distinct scenarios for the existing six employees.
        # ---------------------------------------------------------
        scenario_map = {
            "adrian.luis.villanueva": {
                "travel": {date(2026, 8, 18)},
            },
            "camille.rose.mendoza": {
                "late_under": {date(2026, 8, 19)},
                "absent": {date(2026, 8, 25)},
            },
            "employee_oc": {
                "absent": {date(2026, 8, 17)},
                "overtime": {date(2026, 8, 20)},
            },
            "ivy": {
                "travel": {date(2026, 8, 18)},
                "late_under": {date(2026, 8, 26)},
            },
            "miguel.andres.navarro": {
                "overtime": {date(2026, 8, 19)},
                "absent": {date(2026, 8, 27)},
            },
            "renzo": {
                "travel": {date(2026, 8, 25)},
            },
        }

        ordinary_second_half_days = [
            date(2026, 8, 17),
            date(2026, 8, 18),
            date(2026, 8, 19),
            date(2026, 8, 20),
            # Aug 21 = holiday
            date(2026, 8, 24),
            date(2026, 8, 25),
            date(2026, 8, 26),
            date(2026, 8, 27),
            date(2026, 8, 28),
            # Aug 31 = holiday
        ]

        for index, profile in enumerate(profiles):
            username = profile.user.username
            scenario = scenario_map.get(username, {})

            travel_dates = set(scenario.get("travel", set()))
            absent_dates = set(scenario.get("absent", set()))
            late_under_dates = set(
                scenario.get("late_under", set())
            )
            overtime_dates = set(
                scenario.get("overtime", set())
            )

            for travel_date in travel_dates:
                TravelOrder.objects.create(
                    employee=profile,
                    start_date=travel_date,
                    end_date=travel_date,
                    reason=self.TRAVEL_REASON,
                )

            for ot_date in overtime_dates:
                existing_ot = OvertimeRequest.objects.filter(
                    profile=profile,
                    date=ot_date,
                ).first()

                if existing_ot and existing_ot.reason != self.OT_REASON:
                    raise CommandError(
                        f"{username} already has a different OT request "
                        f"on {ot_date}; refusing to overwrite it."
                    )

                OvertimeRequest.objects.update_or_create(
                    profile=profile,
                    date=ot_date,
                    defaults={
                        "hours": Decimal("1.50"),
                        "approved": True,
                        "approved_by": admin_user,
                        "reason": self.OT_REASON,
                    },
                )

            # Different normal shift appearance for each employee,
            # while still totaling exactly 8 credited hours.
            minute_offset = (index % 4) * 5
            normal_in_hour = 7
            normal_in_minute = minute_offset
            normal_out_hour = 16
            normal_out_minute = minute_offset

            for workday in ordinary_second_half_days:
                if workday in travel_dates:
                    continue

                if workday in absent_dates:
                    continue

                if workday in late_under_dates:
                    punches = [
                        (8, 25, AttendanceRecord.STATUS_CHECKIN, "Check In", "checkIn"),
                        (12, 0, AttendanceRecord.STATUS_CHECKOUT, "Check Out", "checkOut"),
                        (13, 0, AttendanceRecord.STATUS_CHECKIN, "Check In", "checkIn"),
                        (17, 5, AttendanceRecord.STATUS_CHECKOUT, "Check Out", "checkOut"),
                    ]

                elif workday in overtime_dates:
                    punches = [
                        (7, 0, AttendanceRecord.STATUS_CHECKIN, "Check In", "checkIn"),
                        (12, 0, AttendanceRecord.STATUS_CHECKOUT, "Check Out", "checkOut"),
                        (13, 0, AttendanceRecord.STATUS_CHECKIN, "Check In", "checkIn"),
                        (17, 30, AttendanceRecord.STATUS_CHECKOUT, "Check Out", "checkOut"),
                    ]

                else:
                    punches = [
                        (
                            normal_in_hour,
                            normal_in_minute,
                            AttendanceRecord.STATUS_CHECKIN,
                            "Check In",
                            "checkIn",
                        ),
                        (
                            12,
                            0,
                            AttendanceRecord.STATUS_CHECKOUT,
                            "Check Out",
                            "checkOut",
                        ),
                        (
                            13,
                            0,
                            AttendanceRecord.STATUS_CHECKIN,
                            "Check In",
                            "checkIn",
                        ),
                        (
                            normal_out_hour,
                            normal_out_minute,
                            AttendanceRecord.STATUS_CHECKOUT,
                            "Check Out",
                            "checkOut",
                        ),
                    ]

                self._create_punches(
                    profile=profile,
                    branch=branch,
                    workday=workday,
                    punches=punches,
                )

        # ---------------------------------------------------------
        # 5. Process but DO NOT finalize.
        #    This is essential for live JO <-> COS reclassification.
        # ---------------------------------------------------------
        batch = self._process_all(
            branch=branch,
            period=period,
            admin_user=admin_user,
        )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                "PASS  August monthly payroll processed successfully."
            )
        )

        self._print_batch_summary(batch)

        self.stdout.write("")
        self.stdout.write("=" * 82)
        self.stdout.write(
            self.style.SUCCESS(
                "AUGUST MONTHLY LIVE DEMO IS READY"
            )
        )
        self.stdout.write("")
        self.stdout.write("Use in IntelliHRTrack:")
        self.stdout.write(f"  Branch : {branch.name}")
        self.stdout.write(f"  Period : {period.name}")
        self.stdout.write("  Type   : ALL")
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                "IMPORTANT: DO NOT FINALIZE this August 1-31 live-demo "
                "batch before the professor's employment-type test."
            )
        )
        self.stdout.write(
            self.style.WARNING(
                "The future-dated Aug 25-28 rows are controlled TEST DATA "
                "because today's date is Aug 24, 2026. Do not present this "
                "as real employee attendance."
            )
        )
        self.stdout.write("")
        self.stdout.write(
            "Live JO -> COS test employee: employee_oc"
        )
        self.stdout.write(
            "  JO daily rate already configured: PHP 1,000.00"
        )
        self.stdout.write(
            "  COS monthly salary already configured: PHP 22,000.00"
        )
        self.stdout.write(
            "  Therefore you may change ONLY Employment Type JO <-> COS, "
            "save, then click Process Payroll again."
        )
        self.stdout.write("")
        self.stdout.write(
            "After the demonstration restore employee_oc to JO, then either "
            "click Process Payroll again or run:"
        )
        self.stdout.write(
            "  python manage.py prepare_august_monthly_accuracy_demo "
            "--restore-employee-oc"
        )

    def _configure_contributions(self, profile):
        if profile.employment_type in {
            UserProfile.EMP_JO,
            UserProfile.EMP_COS,
        }:
            defaults = {
                "sss_amount": Decimal("760.00"),
                "pagibig_amount": Decimal("400.00"),
                "philhealth_mode": EmployeeContribution.PHILHEALTH_PERCENT,
                "philhealth_value": Decimal("5.00"),
                "wtax_amount": Decimal("0.00"),
                "gsis_employee_share": Decimal("0.00"),
                "gsis_employer_share": Decimal("0.00"),
                "loan_deduction_amount": Decimal("0.00"),
                "other_deduction_amount": Decimal("0.00"),
                "other_employer_contribution": Decimal("0.00"),
            }
        else:
            defaults = {
                "sss_amount": Decimal("0.00"),
                "pagibig_amount": Decimal("0.00"),
                "philhealth_mode": EmployeeContribution.PHILHEALTH_PERCENT,
                "philhealth_value": Decimal("0.00"),
                "wtax_amount": Decimal("0.00"),
                "gsis_employee_share": Decimal("0.00"),
                "gsis_employer_share": Decimal("0.00"),
                "loan_deduction_amount": Decimal("0.00"),
                "other_deduction_amount": Decimal("0.00"),
                "other_employer_contribution": Decimal("0.00"),
            }

        EmployeeContribution.objects.update_or_create(
            profile=profile,
            defaults=defaults,
        )

    def _restore_employee_oc(self, branch):
        profile = UserProfile.objects.get(
            branch=branch,
            user__username="employee_oc",
        )

        profile.employment_type = UserProfile.EMP_JO
        profile.daily_rate = Decimal("1000.00")
        profile.monthly_salary = Decimal("22000.00")
        profile.has_premium = False
        profile.manual_deduction_amount = Decimal("0.00")

        profile.save(
            update_fields=[
                "employment_type",
                "daily_rate",
                "monthly_salary",
                "has_premium",
                "manual_deduction_amount",
            ]
        )

        self._configure_contributions(profile)

    def _create_punches(
        self,
        profile,
        branch,
        workday,
        punches,
    ):
        for (
            hour,
            minute,
            attendance_status,
            label,
            raw_status,
        ) in punches:
            timestamp = datetime(
                workday.year,
                workday.month,
                workday.day,
                hour,
                minute,
                tzinfo=self.MANILA,
            )

            AttendanceRecord.objects.create(
                employee_id=profile.biometric_employee_id,
                full_name=(
                    profile.user.get_full_name()
                    or profile.user.username
                ),
                department=profile.department,
                branch=branch,
                timestamp=timestamp,
                attendance_status=attendance_status,
                raw_row={
                    "time": timestamp.isoformat(),
                    "attendanceStatus": raw_status,
                    "label": label,
                    "demo_marker": self.DEMO_MARKER,
                },
            )

    def _process_all(
        self,
        branch,
        period,
        admin_user,
    ):
        factory = RequestFactory()

        request = factory.post(
            "/admin-ui/payroll/process/",
            {
                "period": str(period.id),
                "branch": str(branch.id),
                "type": PayrollBatch.SCOPE_ALL,
                "search": "",
            },
        )

        request.user = admin_user

        response = admin_payroll_process_batch(
            request
        )

        try:
            payload = json.loads(
                response.content.decode()
            )
        except Exception:
            payload = {}

        if (
            response.status_code != 200
            or not payload.get("ok")
        ):
            raise CommandError(
                "Payroll processing failed: "
                f"HTTP {response.status_code} {payload}"
            )

        return PayrollBatch.objects.get(
            id=payload["batch_id"]
        )

    def _print_batch_summary(self, batch):
        items = (
            PayrollItem.objects
            .filter(batch=batch)
            .select_related("profile", "profile__user")
            .order_by("profile__user__username")
        )

        self.stdout.write("")
        self.stdout.write(
            f"Batch status: {batch.status.upper()} "
            "(intentionally NOT finalized)"
        )

        for item in items:
            meta = (
                item.meta
                if isinstance(item.meta, dict)
                else {}
            )

            summary = meta.get(
                "attendance_summary",
                {},
            )

            computed = meta.get(
                "computed_payroll",
                {},
            )

            name = (
                item.profile.user.get_full_name()
                or item.profile.user.username
            )

            self.stdout.write(
                "  "
                f"{name:<28} "
                f"{item.profile.employment_type:<10} "
                f"Present={summary.get('present_days', 0):<2} "
                f"Travel={summary.get('travel_days', 0):<2} "
                f"Holiday={summary.get('holiday_days', 0):<2} "
                f"Absent={summary.get('absences', 0):<2} "
                f"Late={summary.get('late_minutes', 0):<3} "
                f"Under={summary.get('undertime_minutes', 0):<3} "
                f"OT={computed.get('overtime_hours', '0.00'):<5} "
                f"Net={item.net_pay}"
            )

    def _get_admin_user(self):
        user = User.objects.filter(
            is_superuser=True,
            is_active=True,
        ).order_by("id").first()

        if user:
            return user

        user = User.objects.filter(
            is_staff=True,
            is_active=True,
        ).order_by("id").first()

        if user:
            return user

        raise CommandError(
            "No active staff/superuser account exists."
        )
