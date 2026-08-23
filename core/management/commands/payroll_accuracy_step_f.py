from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from core.models import (
    AttendanceRecord,
    Branch,
    EmployeeContribution,
    HolidaySuspension,
    LeaveRequest,
    OvertimeRequest,
    PayrollPeriod,
    PayrollRule,
    TravelOrder,
    UserProfile,
)

from core.payroll_calculations import money
from core.views import _compute_payroll


class Command(BaseCommand):
    help = (
        "Create controlled JO, COS, and Permanent employees "
        "and verify IntelliHRTrack payroll accuracy end-to-end."
    )

    START_DATE = date(2026, 10, 1)
    END_DATE = date(2026, 10, 15)

    PERIOD_NAME = (
        "Payroll Accuracy Verification - October 1-15 2026"
    )

    MANILA = ZoneInfo("Asia/Manila")

    # Realistic fictional employee identities.
    EMPLOYEES = {
        "JO": {
            "username": "miguel.andres.navarro",
            "first_name": "Miguel Andres",
            "last_name": "Navarro",
            "biometric_id": "900101",
            "department": "Administrative Services",
            "position": "Data Encoder",
        },
        "COS": {
            "username": "camille.rose.mendoza",
            "first_name": "Camille Rose",
            "last_name": "Mendoza",
            "biometric_id": "900102",
            "department": "Planning and Monitoring",
            "position": "Project Support Staff",
        },
        "PERMANENT": {
            "username": "adrian.luis.villanueva",
            "first_name": "Adrian Luis",
            "last_name": "Villanueva",
            "biometric_id": "900103",
            "department": "Administrative Services",
            "position": "Administrative Officer II",
        },
    }

    def handle(self, *args, **options):
        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "INTELLIHRTRACK PAYROLL ACCURACY — STEP F"
            )
        )
        self.stdout.write(
            "=" * 72
        )

        # =====================================================
        # 1. BRANCH
        # =====================================================
        try:
            branch = Branch.objects.get(
                name__iexact="Occidental Mindoro"
            )
        except Branch.DoesNotExist:
            raise CommandError(
                "Occidental Mindoro branch was not found."
            )

        try:
            rules = PayrollRule.objects.get(
                branch=branch
            )
        except PayrollRule.DoesNotExist:
            raise CommandError(
                "Occidental Mindoro has no PayrollRule."
            )

        self.stdout.write(
            f"Branch: {branch.name}"
        )

        # =====================================================
        # 2. VERIFY CRITICAL CONFIGURATION
        # =====================================================
        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_LABEL(
                "Checking payroll configuration..."
            )
        )

        expected_rules = [
            (
                "Salary divisor",
                money(rules.salary_divisor),
                Decimal("22.00"),
            ),
            (
                "Daily required hours",
                money(rules.daily_hours_required),
                Decimal("8.00"),
            ),
            (
                "Tax rate",
                money(rules.tax_rate_percent),
                Decimal("5.00"),
            ),
            (
                "SSS monthly minimum",
                money(rules.sss_minimum),
                Decimal("760.00"),
            ),
            (
                "Pag-IBIG monthly minimum",
                money(rules.pagibig_minimum),
                Decimal("400.00"),
            ),
            (
                "PhilHealth default",
                money(rules.philhealth_default_value),
                Decimal("5.00"),
            ),
        ]

        configuration_failed = False

        for label, actual, expected in expected_rules:
            if actual == expected:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"PASS  {label}: {actual}"
                    )
                )
            else:
                configuration_failed = True

                self.stdout.write(
                    self.style.ERROR(
                        f"FAIL  {label}: "
                        f"actual={actual}, expected={expected}"
                    )
                )

        time_rules = [
            (
                "Earliest creditable time",
                rules.earliest_creditable_time,
                time(7, 0),
            ),
            (
                "Lunch start",
                rules.lunch_start_time,
                time(12, 0),
            ),
            (
                "Lunch end",
                rules.lunch_end_time,
                time(13, 0),
            ),
            (
                "Normal work start",
                rules.work_start_time,
                time(8, 0),
            ),
            (
                "Flag ceremony cutoff",
                rules.flag_ceremony_cutoff_time,
                time(8, 0),
            ),
        ]

        for label, actual, expected in time_rules:
            if actual == expected:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"PASS  {label}: {actual}"
                    )
                )
            else:
                configuration_failed = True

                self.stdout.write(
                    self.style.ERROR(
                        f"FAIL  {label}: "
                        f"actual={actual}, expected={expected}"
                    )
                )

        if not rules.lunch_break_required:
            configuration_failed = True
            self.stdout.write(
                self.style.ERROR(
                    "FAIL  Lunch break must be required."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "PASS  Lunch break required: True"
                )
            )

        if rules.grace_minutes_normal != 15:
            configuration_failed = True
            self.stdout.write(
                self.style.ERROR(
                    "FAIL  Normal grace must be 15 minutes."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "PASS  Normal grace: 15 minutes"
                )
            )

        if configuration_failed:
            raise CommandError(
                "Critical payroll configuration does not "
                "match the Step F answer key."
            )

        # =====================================================
        # 3. MAKE SURE TEST PERIOD HAS NO VERIFIED HOLIDAY
        # =====================================================
        verified_holidays = (
            HolidaySuspension.objects
            .filter(
                date__gte=self.START_DATE,
                date__lte=self.END_DATE,
                is_payroll_verified=True,
            )
            .filter(
                Q(
                    scope=
                    HolidaySuspension.SCOPE_NATIONWIDE
                )
                |
                Q(
                    scope=
                    HolidaySuspension.SCOPE_REGION
                )
                |
                Q(
                    scope=
                    HolidaySuspension.SCOPE_BRANCH,
                    branch=branch,
                )
            )
        )

        if verified_holidays.exists():
            names = ", ".join(
                f"{h.date}: {h.name}"
                for h in verified_holidays
            )

            raise CommandError(
                "The verification period contains verified "
                f"holiday/suspension records: {names}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                "PASS  No verified holiday/suspension "
                "affects the verification period."
            )
        )

        # =====================================================
        # 4. PAYROLL PERIOD
        # =====================================================
        period, _ = PayrollPeriod.objects.get_or_create(
            name=self.PERIOD_NAME,
            start_date=self.START_DATE,
            end_date=self.END_DATE,
            pay_mode=PayrollPeriod.PAY_FIRST_HALF,
        )

        self.stdout.write(
            f"Period: {period.name}"
        )

        # =====================================================
        # 5. CREATE EMPLOYEES
        # =====================================================
        jo = self.create_employee(
            branch=branch,
            employment_type=UserProfile.EMP_JO,
            data=self.EMPLOYEES["JO"],
            daily_rate=Decimal("1000.00"),
            monthly_salary=Decimal("0.00"),
            pera=Decimal("0.00"),
        )

        cos = self.create_employee(
            branch=branch,
            employment_type=UserProfile.EMP_COS,
            data=self.EMPLOYEES["COS"],
            daily_rate=Decimal("0.00"),
            monthly_salary=Decimal("22000.00"),
            pera=Decimal("0.00"),
        )

        permanent = self.create_employee(
            branch=branch,
            employment_type=UserProfile.EMP_PERMANENT,
            data=self.EMPLOYEES["PERMANENT"],
            daily_rate=Decimal("0.00"),
            monthly_salary=Decimal("30000.00"),
            pera=Decimal("2000.00"),
        )

        profiles = [
            jo,
            cos,
            permanent,
        ]

        # =====================================================
        # 6. CLEAN ONLY OUR CONTROL DATA FOR THIS PERIOD
        # =====================================================
        biometric_ids = [
            p.biometric_employee_id
            for p in profiles
        ]

        AttendanceRecord.objects.filter(
            employee_id__in=biometric_ids,
            timestamp__date__gte=self.START_DATE,
            timestamp__date__lte=self.END_DATE,
        ).delete()

        OvertimeRequest.objects.filter(
            profile__in=profiles,
            date__gte=self.START_DATE,
            date__lte=self.END_DATE,
        ).delete()

        TravelOrder.objects.filter(
            employee__in=profiles,
            start_date__lte=self.END_DATE,
            end_date__gte=self.START_DATE,
        ).delete()

        LeaveRequest.objects.filter(
            employee__in=[
                p.user
                for p in profiles
            ],
            start_date__lte=self.END_DATE,
            end_date__gte=self.START_DATE,
        ).delete()

        # =====================================================
        # 7. CONTRIBUTION CONFIGURATION
        # =====================================================

        # JO
        EmployeeContribution.objects.update_or_create(
            profile=jo,
            defaults={
                "sss_amount": Decimal("760.00"),
                "pagibig_amount": Decimal("400.00"),
                "philhealth_mode":
                    EmployeeContribution.PHILHEALTH_PERCENT,
                "philhealth_value": Decimal("5.00"),

                "wtax_amount": Decimal("0.00"),
                "gsis_employee_share": Decimal("0.00"),
                "gsis_employer_share": Decimal("0.00"),
                "loan_deduction_amount": Decimal("0.00"),
                "other_deduction_amount": Decimal("0.00"),
                "other_employer_contribution": Decimal("0.00"),
            },
        )

        # COS
        EmployeeContribution.objects.update_or_create(
            profile=cos,
            defaults={
                "sss_amount": Decimal("760.00"),
                "pagibig_amount": Decimal("400.00"),
                "philhealth_mode":
                    EmployeeContribution.PHILHEALTH_PERCENT,
                "philhealth_value": Decimal("5.00"),

                "wtax_amount": Decimal("0.00"),
                "gsis_employee_share": Decimal("0.00"),
                "gsis_employer_share": Decimal("0.00"),
                "loan_deduction_amount": Decimal("0.00"),
                "other_deduction_amount": Decimal("0.00"),
                "other_employer_contribution": Decimal("0.00"),
            },
        )

        # Permanent:
        # all statutory override values deliberately zero.
        EmployeeContribution.objects.update_or_create(
            profile=permanent,
            defaults={
                "sss_amount": Decimal("0.00"),
                "pagibig_amount": Decimal("0.00"),

                "philhealth_mode":
                    EmployeeContribution.PHILHEALTH_PERCENT,
                "philhealth_value": Decimal("0.00"),

                "wtax_amount": Decimal("0.00"),
                "gsis_employee_share": Decimal("0.00"),
                "gsis_employer_share": Decimal("0.00"),

                "loan_deduction_amount": Decimal("0.00"),
                "other_deduction_amount": Decimal("0.00"),
                "other_employer_contribution": Decimal("0.00"),
            },
        )

        # =====================================================
        # 8. BUILD WEEKDAY LIST
        # =====================================================
        workdays = []

        current = self.START_DATE

        while current <= self.END_DATE:
            if current.weekday() < 5:
                workdays.append(current)

            current += timedelta(days=1)

        self.stdout.write(
            f"Weekdays in test period: {len(workdays)}"
        )

        # Oct 1-15, 2026 has 11 weekdays.
        if len(workdays) != 11:
            raise CommandError(
                "Unexpected weekday count. "
                f"Expected 11, got {len(workdays)}."
            )

        # =====================================================
        # 9. ATTENDANCE SCENARIOS
        # =====================================================

        # Tuesday, October 6:
        # AM IN  8:25
        # AM OUT 12:00
        # PM IN  1:00
        # PM OUT 5:05
        #
        # Result:
        # 460 credited minutes
        # 10 minutes late
        # 20 minutes undertime
        irregular_day = date(2026, 10, 6)

        # JO:
        # 11 workdays - 2 absences = 9 paid days
        jo_absences = {
            date(2026, 10, 8),
            date(2026, 10, 9),
        }

        self.seed_attendance(
            jo,
            branch,
            workdays,
            absences=jo_absences,
            irregular_day=irregular_day,
        )

        # COS:
        # 11 workdays - 1 absence
        cos_absences = {
            date(2026, 10, 9),
        }

        self.seed_attendance(
            cos,
            branch,
            workdays,
            absences=cos_absences,
            irregular_day=irregular_day,
        )

        # Permanent:
        # perfect attendance
        self.seed_attendance(
            permanent,
            branch,
            workdays,
            absences=set(),
            irregular_day=None,
        )

        # =====================================================
        # 10. RUN REAL PAYROLL ENGINE
        # =====================================================
        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "RUNNING _compute_payroll()"
            )
        )

        results = {
            "JO": _compute_payroll(
                jo,
                branch,
                period,
                rules,
            ),
            "COS": _compute_payroll(
                cos,
                branch,
                period,
                rules,
            ),
            "PERMANENT": _compute_payroll(
                permanent,
                branch,
                period,
                rules,
            ),
        }

        failures = []

        # =====================================================
        # 11. JO EXPECTED ANSWER
        # =====================================================
        self.check_employee(
            label="JO — Miguel Andres Navarro",
            result=results["JO"],
            expected={
                "present_days": 9,
                "absences": 2,
                "late_minutes": 10,
                "undertime_minutes": 20,

                "base": Decimal("9000.00"),
                "attendance_deduction":
                    Decimal("62.40"),
                "earned_after_attendance":
                    Decimal("8937.60"),

                "sss": Decimal("380.00"),
                "pagibig": Decimal("200.00"),
                "philhealth": Decimal("446.88"),
                "tax": Decimal("446.88"),

                "deductions": Decimal("1536.16"),
                "net": Decimal("7463.84"),
            },
            failures=failures,
        )

        # =====================================================
        # 12. COS EXPECTED ANSWER
        # =====================================================
        self.check_employee(
            label="COS — Camille Rose Mendoza",
            result=results["COS"],
            expected={
                "present_days": 10,
                "absences": 1,
                "late_minutes": 10,
                "undertime_minutes": 20,

                "base": Decimal("11000.00"),
                "attendance_deduction":
                    Decimal("1062.40"),
                "earned_after_attendance":
                    Decimal("9937.60"),

                "sss": Decimal("380.00"),
                "pagibig": Decimal("200.00"),
                "philhealth": Decimal("496.88"),
                "tax": Decimal("496.88"),

                "deductions": Decimal("2636.16"),
                "net": Decimal("8363.84"),
            },
            failures=failures,
        )

        # =====================================================
        # 13. PERMANENT EXPECTED ANSWER
        # =====================================================
        self.check_employee(
            label="PERMANENT — Adrian Luis Villanueva",
            result=results["PERMANENT"],
            expected={
                "present_days": 11,
                "absences": 0,
                "late_minutes": 0,
                "undertime_minutes": 0,

                "base": Decimal("15000.00"),
                "pera": Decimal("1000.00"),
                "gross": Decimal("16000.00"),

                "philhealth": Decimal("375.00"),
                "pagibig": Decimal("100.00"),
                "gsis_employee": Decimal("1350.00"),
                "gsis_employer": Decimal("1800.00"),

                "taxable_compensation":
                    Decimal("13175.00"),
                "tax": Decimal("413.70"),

                "deductions": Decimal("2238.70"),
                "net": Decimal("13761.30"),

                "employer_contributions_total":
                    Decimal("2275.00"),
            },
            failures=failures,
        )

        # =====================================================
        # FINAL RESULT
        # =====================================================
        self.stdout.write("")
        self.stdout.write(
            "=" * 72
        )

        if failures:
            self.stdout.write(
                self.style.ERROR(
                    f"STEP F FAILED — {len(failures)} "
                    "difference(s) found."
                )
            )

            for failure in failures:
                self.stdout.write(
                    self.style.ERROR(
                        f"  - {failure}"
                    )
                )

            raise CommandError(
                "Manual answer and IntelliHRTrack "
                "do not match."
            )

        self.stdout.write(
            self.style.SUCCESS(
                "STEP F PASSED"
            )
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Manual calculator answers match "
                "the real IntelliHRTrack payroll engine."
            )
        )

        self.stdout.write("")
        self.stdout.write(
            "Primary verification employees:"
        )
        self.stdout.write(
            "  JO        Miguel Andres Navarro"
        )
        self.stdout.write(
            "  COS       Camille Rose Mendoza"
        )
        self.stdout.write(
            "  PERMANENT Adrian Luis Villanueva"
        )

    # =========================================================
    # EMPLOYEE CREATOR
    # =========================================================
    def create_employee(
        self,
        *,
        branch,
        employment_type,
        data,
        daily_rate,
        monthly_salary,
        pera,
    ):
        user, _ = User.objects.update_or_create(
            username=data["username"],
            defaults={
                "first_name": data["first_name"],
                "last_name": data["last_name"],
                "email": "",
                "is_active": True,
                "is_staff": False,
                "is_superuser": False,
            },
        )

        # These verification accounts are not intended
        # for login at this stage.
        user.set_unusable_password()
        user.save(
            update_fields=["password"]
        )

        profile, _ = UserProfile.objects.update_or_create(
            user=user,
            defaults={
                "branch": branch,
                "employment_type": employment_type,
                "department": data["department"],
                "position": data["position"],
                "biometric_employee_id":
                    data["biometric_id"],

                "daily_rate": daily_rate,
                "monthly_salary": monthly_salary,

                "employment_start_date":
                    date(2026, 1, 1),
                "employment_end_date": None,

                "pera_allowance": pera,
                "other_earnings_amount":
                    Decimal("0.00"),
                "manual_deduction_amount":
                    Decimal("0.00"),

                "has_premium": False,
                "is_approved": True,
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Prepared employee: "
                f"{user.get_full_name()} "
                f"({employment_type})"
            )
        )

        return profile

    # =========================================================
    # ATTENDANCE SEEDER
    # =========================================================
    def seed_attendance(
        self,
        profile,
        branch,
        workdays,
        *,
        absences,
        irregular_day,
    ):
        for workday in workdays:
            if workday in absences:
                continue

            if (
                irregular_day
                and workday == irregular_day
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

            else:
                # Perfect flex-time workday:
                #
                # 7:00-12:00 = 5 hours
                # 1:00-4:00  = 3 hours
                # Total      = 8 hours
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
                db_status,
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

                AttendanceRecord.objects.update_or_create(
                    employee_id=
                        profile.biometric_employee_id,
                    timestamp=timestamp,
                    attendance_status=db_status,
                    branch=branch,
                    defaults={
                        "full_name":
                            profile.user.get_full_name(),
                        "department":
                            profile.department,
                        "raw_row": {
                            "time":
                                timestamp.isoformat(),
                            "attendanceStatus":
                                raw_status,
                            "label":
                                label,
                        },
                    },
                )

    # =========================================================
    # RESULT CHECKER
    # =========================================================
    def check_employee(
        self,
        *,
        label,
        result,
        expected,
        failures,
    ):
        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_LABEL(
                label
            )
        )

        summary = (
            result.get(
                "attendance_summary",
                {},
            )
            or {}
        )

        payroll = (
            result.get(
                "computed_payroll",
                {},
            )
            or {}
        )

        gov = (
            result.get(
                "gov",
                {},
            )
            or {}
        )

        sources = {
            **summary,
            **payroll,
            **gov,
        }

        money_fields = {
            "base",
            "pera",
            "gross",
            "attendance_deduction",
            "earned_after_attendance",
            "sss",
            "pagibig",
            "philhealth",
            "tax",
            "taxable_compensation",
            "gsis_employee",
            "gsis_employer",
            "deductions",
            "net",
            "employer_contributions_total",
        }

        for field, expected_value in expected.items():
            actual = sources.get(field)

            if field in money_fields:
                actual = money(
                    actual or Decimal("0.00")
                )

                expected_value = money(
                    expected_value
                )

            if actual == expected_value:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"PASS  {field}: {actual}"
                    )
                )

            else:
                failure = (
                    f"{label} / {field}: "
                    f"actual={actual}, "
                    f"expected={expected_value}"
                )

                failures.append(
                    failure
                )

                self.stdout.write(
                    self.style.ERROR(
                        f"FAIL  {field}: "
                        f"actual={actual}, "
                        f"expected={expected_value}"
                    )
                )

        issues = result.get(
            "issues",
            "",
        )

        self.stdout.write(
            f"Issues: {issues or 'None'}"
        )