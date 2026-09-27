import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory

from core.models import (
    AttendanceRecord,
    Branch,
    EmployeeContribution,
    FinalizedDTR,
    OvertimeRequest,
    PayrollBatch,
    PayrollItem,
    PayrollPeriod,
    TravelOrder,
    UserProfile,
)
from core.views import (
    admin_finalize_payroll_batch,
    admin_finalize_payroll_item_dtr,
    admin_payroll_process_batch,
)


class Command(BaseCommand):
    help = (
        "Populate ALL existing approved employees in the original Occidental Mindoro "
        "branch with a clean, presentable payroll demonstration period. This command "
        "does not modify the finalized October Step-H payroll period and does not "
        "modify the separate Payroll Verification branch."
    )

    BRANCH_NAME = "Occidental Mindoro"
    PERIOD_NAME = "Payroll System Demonstration - August 1-15 2026"
    START_DATE = date(2026, 8, 1)
    END_DATE = date(2026, 8, 15)

    MANILA = ZoneInfo("Asia/Manila")

    DEMO_MARKER = "INTELLIHRTRACK_ALL_EXISTING_USERS_DEMO_V1"
    TRAVEL_REASON = "Payroll system demonstration - official travel"
    OT_REASON = "Payroll system demonstration - approved overtime"

    def handle(self, *args, **options):
        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "INTELLIHRTRACK - ALL EXISTING USERS PAYROLL DEMO DATA"
            )
        )
        self.stdout.write("=" * 78)

        branch = Branch.objects.filter(
            name__iexact=self.BRANCH_NAME
        ).first()

        if not branch:
            raise CommandError(
                f'Branch "{self.BRANCH_NAME}" was not found.'
            )

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
                "No approved non-admin employees were found in Occidental Mindoro."
            )

        invalid_types = [
            p for p in profiles
            if p.employment_type not in {
                UserProfile.EMP_JO,
                UserProfile.EMP_COS,
                UserProfile.EMP_PERMANENT,
            }
        ]

        if invalid_types:
            names = ", ".join(
                p.user.get_full_name() or p.user.username
                for p in invalid_types
            )
            raise CommandError(
                "These employees have an unsupported employment type: " + names
            )

        self.stdout.write(
            f"Branch: {branch.name}"
        )
        self.stdout.write(
            f"Employees found: {len(profiles)}"
        )

        # ------------------------------------------------------------
        # 1. Make sure every existing employee can participate in DTR
        #    and payroll calculation.
        # ------------------------------------------------------------
        for profile in profiles:
            changed_fields = []

            if not (profile.biometric_employee_id or "").strip():
                profile.biometric_employee_id = f"DEMO{profile.id:06d}"
                changed_fields.append("biometric_employee_id")

            if profile.employment_type == UserProfile.EMP_JO:
                daily = Decimal(str(profile.daily_rate or "0.00"))
                monthly = Decimal(str(profile.monthly_salary or "0.00"))

                if daily <= 0 and monthly <= 0:
                    profile.daily_rate = Decimal("900.00")
                    changed_fields.append("daily_rate")

            elif profile.employment_type == UserProfile.EMP_COS:
                monthly = Decimal(str(profile.monthly_salary or "0.00"))

                if monthly <= 0:
                    profile.monthly_salary = Decimal("22000.00")
                    changed_fields.append("monthly_salary")

            elif profile.employment_type == UserProfile.EMP_PERMANENT:
                monthly = Decimal(str(profile.monthly_salary or "0.00"))

                if monthly <= 0:
                    profile.monthly_salary = Decimal("30000.00")
                    changed_fields.append("monthly_salary")

                pera = Decimal(str(profile.pera_allowance or "0.00"))

                if pera <= 0:
                    profile.pera_allowance = Decimal("2000.00")
                    changed_fields.append("pera_allowance")

            # Keep the demo focused on confirmed payroll rules.
            if profile.has_premium:
                profile.has_premium = False
                changed_fields.append("has_premium")

            if Decimal(str(profile.manual_deduction_amount or "0.00")) != 0:
                profile.manual_deduction_amount = Decimal("0.00")
                changed_fields.append("manual_deduction_amount")

            if Decimal(str(profile.other_earnings_amount or "0.00")) != 0:
                profile.other_earnings_amount = Decimal("0.00")
                changed_fields.append("other_earnings_amount")

            if changed_fields:
                profile.save(update_fields=list(dict.fromkeys(changed_fields)))

            self._configure_contributions(profile)

        # ------------------------------------------------------------
        # 2. Create a NEW period for the original branch.
        #    This is intentionally separate from Step H.
        # ------------------------------------------------------------
        period, _ = PayrollPeriod.objects.update_or_create(
            name=self.PERIOD_NAME,
            defaults={
                "start_date": self.START_DATE,
                "end_date": self.END_DATE,
                "pay_mode": PayrollPeriod.PAY_FIRST_HALF,
            },
        )

        self.stdout.write(
            f"Demo period: {period.name}"
        )

        admin_user = self._get_admin_user()
        self.stdout.write(
            f"Admin used for processing: {admin_user.username}"
        )

        # ------------------------------------------------------------
        # 3. Clean only THIS command's previous demo run.
        #    Do not touch attendance belonging to another source.
        # ------------------------------------------------------------
        start_dt = datetime(
            self.START_DATE.year,
            self.START_DATE.month,
            self.START_DATE.day,
            0,
            0,
            tzinfo=self.MANILA,
        )

        after_end = self.END_DATE + timedelta(days=1)

        end_dt = datetime(
            after_end.year,
            after_end.month,
            after_end.day,
            0,
            0,
            tzinfo=self.MANILA,
        )

        biometric_ids = [
            p.biometric_employee_id
            for p in profiles
            if p.biometric_employee_id
        ]

        existing_attendance = list(
            AttendanceRecord.objects.filter(
                branch=branch,
                employee_id__in=biometric_ids,
                timestamp__gte=start_dt,
                timestamp__lt=end_dt,
            )
        )

        foreign_records = []

        for record in existing_attendance:
            raw = record.raw_row if isinstance(record.raw_row, dict) else {}

            if raw.get("demo_marker") != self.DEMO_MARKER:
                foreign_records.append(record)

        if foreign_records:
            examples = ", ".join(
                f"{r.employee_id}@{r.timestamp}"
                for r in foreign_records[:5]
            )

            raise CommandError(
                "The August 1-15 demo period already contains attendance that was "
                "NOT created by this command. Nothing was deleted. "
                f"Examples: {examples}. Use another clean period before rerunning."
            )

        # Safe cleanup of a previous run of this exact demo.
        FinalizedDTR.objects.filter(
            profile__in=profiles,
            period=period,
        ).delete()

        PayrollItem.objects.filter(
            batch__branch=branch,
            batch__period=period,
        ).delete()

        PayrollBatch.objects.filter(
            branch=branch,
            period=period,
        ).delete()

        for record in existing_attendance:
            raw = record.raw_row if isinstance(record.raw_row, dict) else {}

            if raw.get("demo_marker") == self.DEMO_MARKER:
                record.delete()

        OvertimeRequest.objects.filter(
            profile__in=profiles,
            date__gte=self.START_DATE,
            date__lte=self.END_DATE,
            reason=self.OT_REASON,
        ).delete()

        TravelOrder.objects.filter(
            employee__in=profiles,
            start_date__lte=self.END_DATE,
            end_date__gte=self.START_DATE,
            reason=self.TRAVEL_REASON,
        ).delete()

        # ------------------------------------------------------------
        # 4. Seed realistic attendance for ALL existing employees.
        # ------------------------------------------------------------
        workdays = self._workdays()

        if len(workdays) != 10:
            raise CommandError(
                f"Expected 10 weekdays in this demo period; found {len(workdays)}."
            )

        contractual_index = 0
        scenario_by_profile = {}

        for profile in profiles:
            if profile.employment_type == UserProfile.EMP_PERMANENT:
                scenario = "perfect"
            else:
                scenario_cycle = [
                    "perfect",
                    "late_under",
                    "absence",
                    "travel",
                    "overtime",
                ]

                scenario = scenario_cycle[
                    contractual_index % len(scenario_cycle)
                ]
                contractual_index += 1

            scenario_by_profile[profile.id] = scenario

            self._seed_employee_attendance(
                profile=profile,
                branch=branch,
                workdays=workdays,
                scenario=scenario,
            )

            if scenario == "travel":
                TravelOrder.objects.create(
                    employee=profile,
                    start_date=date(2026, 8, 6),
                    end_date=date(2026, 8, 6),
                    reason=self.TRAVEL_REASON,
                )

            if scenario == "overtime":
                OvertimeRequest.objects.create(
                    profile=profile,
                    date=date(2026, 8, 5),
                    hours=Decimal("1.50"),
                    approved=True,
                    approved_by=admin_user,
                    reason=self.OT_REASON,
                )

        # ------------------------------------------------------------
        # 5. Process ONE ALL-employees payroll batch.
        # ------------------------------------------------------------
        factory = RequestFactory()

        process_request = factory.post(
            "/admin-ui/payroll/process/",
            {
                "period": str(period.id),
                "branch": str(branch.id),
                "type": PayrollBatch.SCOPE_ALL,
                "search": "",
            },
        )

        process_request.user = admin_user

        response = admin_payroll_process_batch(
            process_request
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

        expected_count = len(profiles)
        actual_count = int(
            payload.get("total_items", 0) or 0
        )

        if actual_count != expected_count:
            raise CommandError(
                f"Expected {expected_count} employees in the ALL batch, "
                f"but the payroll engine processed {actual_count}."
            )

        batch = PayrollBatch.objects.get(
            id=payload["batch_id"]
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"PASS  ALL payroll batch processed with {actual_count} employees"
            )
        )

        # ------------------------------------------------------------
        # 6. Verify each employee has meaningful data and finalize DTR.
        # ------------------------------------------------------------
        failures = []

        for profile in profiles:
            item = PayrollItem.objects.filter(
                batch=batch,
                profile=profile,
            ).first()

            name = (
                profile.user.get_full_name()
                or profile.user.username
            )

            if not item:
                failures.append(
                    f"{name}: missing PayrollItem"
                )
                continue

            meta = (
                item.meta
                if isinstance(item.meta, dict)
                else {}
            )

            summary = (
                meta.get("attendance_summary", {})
                if isinstance(
                    meta.get("attendance_summary"),
                    dict,
                )
                else {}
            )

            present = int(
                summary.get("present_days", 0) or 0
            )

            travel = int(
                summary.get("travel_days", 0) or 0
            )

            absences = int(
                summary.get("absences", item.absences) or 0
            )

            missing_logs = int(
                summary.get("missing_logs", 0) or 0
            )

            net = Decimal(
                str(item.net_pay or "0.00")
            )

            base = Decimal(
                str(item.base_pay or "0.00")
            )

            if present <= 0:
                failures.append(
                    f"{name}: present days is still zero"
                )

            if missing_logs != 0:
                failures.append(
                    f"{name}: missing logs = {missing_logs}"
                )

            if base <= 0:
                failures.append(
                    f"{name}: base pay is not positive ({base})"
                )

            if net < 0:
                failures.append(
                    f"{name}: net pay is negative ({net})"
                )

            if item.issues:
                failures.append(
                    f"{name}: payroll issues = {item.issues}"
                )

            finalize_request = factory.post(
                f"/admin-ui/payroll/item/{item.id}/dtr/finalize/"
            )

            self._prepare_message_request(
                finalize_request,
                admin_user,
            )

            admin_finalize_payroll_item_dtr(
                finalize_request,
                item.id,
            )

            locked = FinalizedDTR.objects.filter(
                profile=profile,
                period=period,
                is_locked=True,
            ).exists()

            if not locked:
                failures.append(
                    f"{name}: DTR did not finalize/lock"
                )

            scenario_label = {
                "perfect": "Perfect attendance",
                "late_under": "Late + undertime",
                "absence": "One absence",
                "travel": "Official Travel",
                "overtime": "Approved overtime",
            }.get(
                scenario_by_profile.get(profile.id),
                "Demo attendance",
            )

            self.stdout.write(
                self.style.SUCCESS(
                    "PASS  "
                    f"{name:<28} "
                    f"{profile.employment_type:<10} "
                    f"Present={present:<2} "
                    f"Travel={travel:<2} "
                    f"Absent={absences:<2} "
                    f"Net={net} "
                    f"[{scenario_label}]"
                )
            )

        # ------------------------------------------------------------
        # 7. Finalize the ALL batch.
        # ------------------------------------------------------------
        if not failures:
            finalize_batch_request = factory.post(
                f"/admin-ui/payroll/batch/{batch.id}/finalize/"
            )

            self._prepare_message_request(
                finalize_batch_request,
                admin_user,
            )

            admin_finalize_payroll_batch(
                finalize_batch_request,
                batch.id,
            )

            batch.refresh_from_db()

            if (
                batch.status
                != PayrollBatch.STATUS_FINALIZED
            ):
                failures.append(
                    "ALL payroll batch did not finalize"
                )

        if failures:
            self.stdout.write("")
            self.stdout.write(
                self.style.ERROR(
                    f"DEMO DATA FAILED - {len(failures)} problem(s)"
                )
            )

            for failure in failures:
                self.stdout.write(
                    self.style.ERROR(
                        f"  - {failure}"
                    )
                )

            raise CommandError(
                "Do not use this demo period in the professor demonstration yet."
            )

        self.stdout.write("")
        self.stdout.write("=" * 78)
        self.stdout.write(
            self.style.SUCCESS(
                "ALL EXISTING OCCIDENTAL MINDORO EMPLOYEES NOW HAVE DEMO PAYROLL DATA"
            )
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Employees populated: {len(profiles)}"
            )
        )

        self.stdout.write(
            self.style.SUCCESS(
                "DTR status: LOCKED for every employee"
            )
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Payroll batch status: FINALIZED"
            )
        )

        self.stdout.write("")
        self.stdout.write(
            "In IntelliHRTrack select:"
        )

        self.stdout.write(
            f"  Branch : {branch.name}"
        )

        self.stdout.write(
            f"  Period : {period.name}"
        )

        self.stdout.write(
            "  Type   : ALL"
        )

        self.stdout.write("")
        self.stdout.write(
            "IMPORTANT: This is a presentation/demo period for the existing "
            "Occidental Mindoro users. The separate Payroll Verification branch "
            "and its 9-person PDF evidence remain unchanged."
        )

    def _configure_contributions(
        self,
        profile,
    ):
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
            # Permanent: zero overrides means use the automatic
            # Permanent computation instead of old test override values.
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

    def _workdays(self):
        days = []
        current = self.START_DATE

        while current <= self.END_DATE:
            if current.weekday() < 5:
                days.append(current)

            current += timedelta(days=1)

        return days

    def _seed_employee_attendance(
        self,
        profile,
        branch,
        workdays,
        scenario,
    ):
        absence_date = date(2026, 8, 7)
        irregular_date = date(2026, 8, 4)
        travel_date = date(2026, 8, 6)
        overtime_date = date(2026, 8, 5)

        for workday in workdays:
            if (
                scenario == "absence"
                and workday == absence_date
            ):
                continue

            if (
                scenario == "travel"
                and workday == travel_date
            ):
                continue

            if (
                scenario == "late_under"
                and workday == irregular_date
            ):
                punches = [
                    (
                        8,
                        25,
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
                        17,
                        5,
                        AttendanceRecord.STATUS_CHECKOUT,
                        "Check Out",
                        "checkOut",
                    ),
                ]

            elif (
                scenario == "overtime"
                and workday == overtime_date
            ):
                punches = [
                    (
                        7,
                        0,
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
                        17,
                        30,
                        AttendanceRecord.STATUS_CHECKOUT,
                        "Check Out",
                        "checkOut",
                    ),
                ]

            else:
                punches = [
                    (
                        7,
                        0,
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
                        16,
                        0,
                        AttendanceRecord.STATUS_CHECKOUT,
                        "Check Out",
                        "checkOut",
                    ),
                ]

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

    def _prepare_message_request(
        self,
        request,
        user,
    ):
        request.user = user
        request.session = {}
        request._messages = FallbackStorage(
            request
        )

        return request
