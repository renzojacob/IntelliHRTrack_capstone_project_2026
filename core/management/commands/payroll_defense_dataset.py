import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
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
    PayrollRule,
    TravelOrder,
    UserProfile,
)
from core.payroll_calculations import money
from core.views import (
    admin_finalize_payroll_batch,
    admin_finalize_payroll_item_dtr,
    admin_payroll_process_batch,
)


class Command(BaseCommand):
    help = (
        "Create a clean payroll-accuracy verification branch with nine realistic "
        "fictional employees, process/finalize payroll, validate the answer keys, "
        "and export the actual saved system data for the defense PDF."
    )

    SOURCE_BRANCH_NAME = "Occidental Mindoro"
    DEMO_BRANCH_NAME = "Occidental Mindoro - Payroll Verification"
    PERIOD_NAME = "Payroll Accuracy Defense - August 3-14 2026"
    START_DATE = date(2026, 8, 3)
    END_DATE = date(2026, 8, 14)
    MANILA = ZoneInfo("Asia/Manila")
    USERNAME_MARKER = "pv."

    EMPLOYEES = [
        {
            "key": "miguel",
            "username": "pv.miguel.navarro",
            "first_name": "Miguel Andres",
            "last_name": "Navarro",
            "employment_type": UserProfile.EMP_JO,
            "biometric_id": "910001",
            "department": "Administrative Services",
            "position": "Data Encoder",
            "daily_rate": Decimal("1000.00"),
            "monthly_salary": Decimal("0.00"),
            "pera": Decimal("0.00"),
            "scenario": "One absence plus 10 minutes late and 20 minutes undertime",
            "absence_dates": {date(2026, 8, 7)},
            "irregular_date": date(2026, 8, 4),
            "ot_date": None,
            "ot_hours": Decimal("0.00"),
            "travel_date": None,
            "expected": {
                "present_days": 9,
                "travel_days": 0,
                "absences": 1,
                "late_minutes": 10,
                "undertime_minutes": 20,
                "base": "9000.00",
                "gross": "9000.00",
                "attendance_deduction": "62.40",
                "earned_after_attendance": "8937.60",
                "sss": "380.00",
                "philhealth": "446.88",
                "pagibig": "200.00",
                "tax": "446.88",
                "deductions": "1536.16",
                "net": "7463.84",
                "overtime_hours": "0.00",
                "overtime_pay": "0.00",
            },
            "formula": [
                "Base pay = P1,000.00 x 9 paid days = P9,000.00",
                "Hourly rate = P1,000.00 / 8 = P125.00",
                "Per-minute rate = P125.00 / 60 = P2.08",
                "Late = 10 x P2.08 = P20.80",
                "Undertime = 20 x P2.08 = P41.60",
                "Attendance deduction = P20.80 + P41.60 = P62.40",
                "Earned compensation = P9,000.00 - P62.40 = P8,937.60",
                "SSS = P760.00 x 0.50 = P380.00",
                "Pag-IBIG = P400.00 x 0.50 = P200.00",
                "PhilHealth = P8,937.60 x 5% = P446.88",
                "Tax = P8,937.60 x 5% = P446.88",
                "Net = P9,000.00 - P1,536.16 = P7,463.84",
            ],
        },
        {
            "key": "sofia",
            "username": "pv.sofia.reyes",
            "first_name": "Sofia Mae",
            "last_name": "Reyes",
            "employment_type": UserProfile.EMP_JO,
            "biometric_id": "910002",
            "department": "Records Management",
            "position": "Administrative Aide",
            "daily_rate": Decimal("900.00"),
            "monthly_salary": Decimal("0.00"),
            "pera": Decimal("0.00"),
            "scenario": "Perfect attendance",
            "absence_dates": set(),
            "irregular_date": None,
            "ot_date": None,
            "ot_hours": Decimal("0.00"),
            "travel_date": None,
            "expected": {
                "present_days": 10,
                "travel_days": 0,
                "absences": 0,
                "late_minutes": 0,
                "undertime_minutes": 0,
                "base": "9000.00",
                "gross": "9000.00",
                "attendance_deduction": "0.00",
                "earned_after_attendance": "9000.00",
                "sss": "380.00",
                "philhealth": "450.00",
                "pagibig": "200.00",
                "tax": "450.00",
                "deductions": "1480.00",
                "net": "7520.00",
                "overtime_hours": "0.00",
                "overtime_pay": "0.00",
            },
            "formula": [
                "Base pay = P900.00 x 10 paid days = P9,000.00",
                "Attendance deduction = P0.00",
                "Earned compensation = P9,000.00",
                "SSS = P760.00 x 0.50 = P380.00",
                "Pag-IBIG = P400.00 x 0.50 = P200.00",
                "PhilHealth = P9,000.00 x 5% = P450.00",
                "Tax = P9,000.00 x 5% = P450.00",
                "Net = P9,000.00 - P1,480.00 = P7,520.00",
            ],
        },
        {
            "key": "carlo",
            "username": "pv.carlo.bautista",
            "first_name": "Carlo Joaquin",
            "last_name": "Bautista",
            "employment_type": UserProfile.EMP_JO,
            "biometric_id": "910003",
            "department": "Field Operations",
            "position": "Field Support Staff",
            "daily_rate": Decimal("1200.00"),
            "monthly_salary": Decimal("0.00"),
            "pera": Decimal("0.00"),
            "scenario": "Perfect attendance with 1.50 hours approved and DTR-proven overtime",
            "absence_dates": set(),
            "irregular_date": None,
            "ot_date": date(2026, 8, 5),
            "ot_hours": Decimal("1.50"),
            "travel_date": None,
            "expected": {
                "present_days": 10,
                "travel_days": 0,
                "absences": 0,
                "late_minutes": 0,
                "undertime_minutes": 0,
                "base": "12000.00",
                "gross": "12281.25",
                "attendance_deduction": "0.00",
                "earned_after_attendance": "12281.25",
                "sss": "380.00",
                "philhealth": "614.06",
                "pagibig": "200.00",
                "tax": "614.06",
                "deductions": "1808.12",
                "net": "10473.13",
                "overtime_hours": "1.50",
                "overtime_pay": "281.25",
            },
            "formula": [
                "Base pay = P1,200.00 x 10 paid days = P12,000.00",
                "Hourly rate = P1,200.00 / 8 = P150.00",
                "Actual work on OT day = 570 minutes; required = 480; actual OT = 90 minutes = 1.50 hours",
                "OT pay = 1.50 x P150.00 x 1.25 = P281.25",
                "Gross = P12,000.00 + P281.25 = P12,281.25",
                "PhilHealth = P12,281.25 x 5% = P614.06",
                "Tax = P12,281.25 x 5% = P614.06",
                "SSS = P380.00; Pag-IBIG = P200.00",
                "Net = P12,281.25 - P1,808.12 = P10,473.13",
            ],
        },
        {
            "key": "camille",
            "username": "pv.camille.mendoza",
            "first_name": "Camille Rose",
            "last_name": "Mendoza",
            "employment_type": UserProfile.EMP_COS,
            "biometric_id": "910004",
            "department": "Planning and Monitoring",
            "position": "Project Support Staff",
            "daily_rate": Decimal("0.00"),
            "monthly_salary": Decimal("22000.00"),
            "pera": Decimal("0.00"),
            "scenario": "One absence plus 10 minutes late and 20 minutes undertime",
            "absence_dates": {date(2026, 8, 7)},
            "irregular_date": date(2026, 8, 4),
            "ot_date": None,
            "ot_hours": Decimal("0.00"),
            "travel_date": None,
            "expected": {
                "present_days": 9,
                "travel_days": 0,
                "absences": 1,
                "late_minutes": 10,
                "undertime_minutes": 20,
                "base": "11000.00",
                "gross": "11000.00",
                "attendance_deduction": "1062.40",
                "earned_after_attendance": "9937.60",
                "sss": "380.00",
                "philhealth": "496.88",
                "pagibig": "200.00",
                "tax": "496.88",
                "deductions": "2636.16",
                "net": "8363.84",
                "overtime_hours": "0.00",
                "overtime_pay": "0.00",
            },
            "formula": [
                "Semi-monthly base = P22,000.00 x 0.50 = P11,000.00",
                "Daily rate = P22,000.00 / 22 = P1,000.00",
                "Absence = 1 x P1,000.00 = P1,000.00",
                "Late = 10 x P2.08 = P20.80; undertime = 20 x P2.08 = P41.60",
                "Attendance deduction = P1,062.40",
                "Earned compensation = P11,000.00 - P1,062.40 = P9,937.60",
                "SSS = P380.00; Pag-IBIG = P200.00",
                "PhilHealth = P9,937.60 x 5% = P496.88",
                "Tax = P9,937.60 x 5% = P496.88",
                "Net = P11,000.00 - P2,636.16 = P8,363.84",
            ],
        },
        {
            "key": "bianca",
            "username": "pv.bianca.ramos",
            "first_name": "Bianca Nicole",
            "last_name": "Ramos",
            "employment_type": UserProfile.EMP_COS,
            "biometric_id": "910005",
            "department": "Information Management",
            "position": "Technical Support Staff",
            "daily_rate": Decimal("0.00"),
            "monthly_salary": Decimal("26400.00"),
            "pera": Decimal("0.00"),
            "scenario": "Perfect attendance",
            "absence_dates": set(),
            "irregular_date": None,
            "ot_date": None,
            "ot_hours": Decimal("0.00"),
            "travel_date": None,
            "expected": {
                "present_days": 10,
                "travel_days": 0,
                "absences": 0,
                "late_minutes": 0,
                "undertime_minutes": 0,
                "base": "13200.00",
                "gross": "13200.00",
                "attendance_deduction": "0.00",
                "earned_after_attendance": "13200.00",
                "sss": "380.00",
                "philhealth": "660.00",
                "pagibig": "200.00",
                "tax": "660.00",
                "deductions": "1900.00",
                "net": "11300.00",
                "overtime_hours": "0.00",
                "overtime_pay": "0.00",
            },
            "formula": [
                "Semi-monthly base = P26,400.00 x 0.50 = P13,200.00",
                "Daily rate = P26,400.00 / 22 = P1,200.00",
                "Attendance deduction = P0.00",
                "SSS = P380.00; Pag-IBIG = P200.00",
                "PhilHealth = P13,200.00 x 5% = P660.00",
                "Tax = P13,200.00 x 5% = P660.00",
                "Net = P13,200.00 - P1,900.00 = P11,300.00",
            ],
        },
        {
            "key": "daniel",
            "username": "pv.daniel.santos",
            "first_name": "Daniel Paolo",
            "last_name": "Santos",
            "employment_type": UserProfile.EMP_COS,
            "biometric_id": "910006",
            "department": "Field Operations",
            "position": "Program Assistant",
            "daily_rate": Decimal("0.00"),
            "monthly_salary": Decimal("19800.00"),
            "pera": Decimal("0.00"),
            "scenario": "One approved Official Travel day; no attendance deduction",
            "absence_dates": set(),
            "irregular_date": None,
            "ot_date": None,
            "ot_hours": Decimal("0.00"),
            "travel_date": date(2026, 8, 6),
            "expected": {
                "present_days": 10,
                "travel_days": 1,
                "absences": 0,
                "late_minutes": 0,
                "undertime_minutes": 0,
                "base": "9900.00",
                "gross": "9900.00",
                "attendance_deduction": "0.00",
                "earned_after_attendance": "9900.00",
                "sss": "380.00",
                "philhealth": "495.00",
                "pagibig": "200.00",
                "tax": "495.00",
                "deductions": "1570.00",
                "net": "8330.00",
                "overtime_hours": "0.00",
                "overtime_pay": "0.00",
            },
            "formula": [
                "Semi-monthly base = P19,800.00 x 0.50 = P9,900.00",
                "Daily rate = P19,800.00 / 22 = P900.00",
                "Approved Official Travel is treated as present, so attendance deduction = P0.00",
                "SSS = P380.00; Pag-IBIG = P200.00",
                "PhilHealth = P9,900.00 x 5% = P495.00",
                "Tax = P9,900.00 x 5% = P495.00",
                "Net = P9,900.00 - P1,570.00 = P8,330.00",
            ],
        },
        {
            "key": "adrian",
            "username": "pv.adrian.villanueva",
            "first_name": "Adrian Luis",
            "last_name": "Villanueva",
            "employment_type": UserProfile.EMP_PERMANENT,
            "biometric_id": "910007",
            "department": "Administrative Services",
            "position": "Administrative Officer II",
            "daily_rate": Decimal("0.00"),
            "monthly_salary": Decimal("30000.00"),
            "pera": Decimal("2000.00"),
            "scenario": "Perfect attendance; automatic Permanent statutory computation",
            "absence_dates": set(),
            "irregular_date": None,
            "ot_date": None,
            "ot_hours": Decimal("0.00"),
            "travel_date": None,
            "expected": {
                "present_days": 10,
                "travel_days": 0,
                "absences": 0,
                "late_minutes": 0,
                "undertime_minutes": 0,
                "base": "15000.00",
                "gross": "16000.00",
                "pera": "1000.00",
                "philhealth": "375.00",
                "pagibig": "100.00",
                "gsis_employee": "1350.00",
                "gsis_employer": "1800.00",
                "taxable_compensation": "13175.00",
                "tax": "413.70",
                "deductions": "2238.70",
                "net": "13761.30",
                "employer_contributions_total": "2275.00",
            },
            "formula": [
                "Basic for cutoff = P30,000.00 x 0.50 = P15,000.00",
                "PERA for cutoff = P2,000.00 x 0.50 = P1,000.00",
                "Gross = P16,000.00",
                "GSIS employee = P30,000.00 x 9% x 0.50 = P1,350.00",
                "PhilHealth employee = P30,000.00 x 5% / 2 x 0.50 = P375.00",
                "Pag-IBIG employee = P10,000.00 x 2% x 0.50 = P100.00",
                "Taxable = P16,000.00 - P1,000.00 PERA - P1,825.00 mandatory = P13,175.00",
                "WTAX = (P13,175.00 - P10,417.00) x 15% = P413.70",
                "Net = P16,000.00 - P2,238.70 = P13,761.30",
                "Employer contributions = P1,800.00 GSIS + P375.00 PhilHealth + P100.00 Pag-IBIG = P2,275.00",
            ],
        },
        {
            "key": "patricia",
            "username": "pv.patricia.garcia",
            "first_name": "Patricia Anne",
            "last_name": "Garcia",
            "employment_type": UserProfile.EMP_PERMANENT,
            "biometric_id": "910008",
            "department": "Finance and Management",
            "position": "Accountant II",
            "daily_rate": Decimal("0.00"),
            "monthly_salary": Decimal("45000.00"),
            "pera": Decimal("2000.00"),
            "scenario": "Perfect attendance; higher BIR semi-monthly bracket",
            "absence_dates": set(),
            "irregular_date": None,
            "ot_date": None,
            "ot_hours": Decimal("0.00"),
            "travel_date": None,
            "expected": {
                "present_days": 10,
                "travel_days": 0,
                "absences": 0,
                "late_minutes": 0,
                "undertime_minutes": 0,
                "base": "22500.00",
                "gross": "23500.00",
                "pera": "1000.00",
                "philhealth": "562.50",
                "pagibig": "100.00",
                "gsis_employee": "2025.00",
                "gsis_employer": "2700.00",
                "taxable_compensation": "19812.50",
                "tax": "1566.60",
                "deductions": "4254.10",
                "net": "19245.90",
                "employer_contributions_total": "3362.50",
            },
            "formula": [
                "Basic for cutoff = P45,000.00 x 0.50 = P22,500.00",
                "PERA for cutoff = P1,000.00; gross = P23,500.00",
                "GSIS employee = P45,000.00 x 9% x 0.50 = P2,025.00",
                "PhilHealth employee = P45,000.00 x 5% / 2 x 0.50 = P562.50",
                "Pag-IBIG employee = P100.00",
                "Taxable = P23,500.00 - P1,000.00 - P2,687.50 = P19,812.50",
                "WTAX = P937.50 + (P19,812.50 - P16,667.00) x 20% = P1,566.60",
                "Net = P23,500.00 - P4,254.10 = P19,245.90",
                "Employer contributions = P3,362.50",
            ],
        },
        {
            "key": "joshua",
            "username": "pv.joshua.fernandez",
            "first_name": "Joshua Miguel",
            "last_name": "Fernandez",
            "employment_type": UserProfile.EMP_PERMANENT,
            "biometric_id": "910009",
            "department": "Human Resource Management",
            "position": "Administrative Officer I",
            "daily_rate": Decimal("0.00"),
            "monthly_salary": Decimal("25000.00"),
            "pera": Decimal("2000.00"),
            "scenario": "Perfect attendance; lower taxable compensation",
            "absence_dates": set(),
            "irregular_date": None,
            "ot_date": None,
            "ot_hours": Decimal("0.00"),
            "travel_date": None,
            "expected": {
                "present_days": 10,
                "travel_days": 0,
                "absences": 0,
                "late_minutes": 0,
                "undertime_minutes": 0,
                "base": "12500.00",
                "gross": "13500.00",
                "pera": "1000.00",
                "philhealth": "312.50",
                "pagibig": "100.00",
                "gsis_employee": "1125.00",
                "gsis_employer": "1500.00",
                "taxable_compensation": "10962.50",
                "tax": "81.83",
                "deductions": "1619.33",
                "net": "11880.67",
                "employer_contributions_total": "1912.50",
            },
            "formula": [
                "Basic for cutoff = P25,000.00 x 0.50 = P12,500.00",
                "PERA for cutoff = P1,000.00; gross = P13,500.00",
                "GSIS employee = P25,000.00 x 9% x 0.50 = P1,125.00",
                "PhilHealth employee = P25,000.00 x 5% / 2 x 0.50 = P312.50",
                "Pag-IBIG employee = P100.00",
                "Taxable = P13,500.00 - P1,000.00 - P1,537.50 = P10,962.50",
                "WTAX = (P10,962.50 - P10,417.00) x 15% = P81.83",
                "Net = P13,500.00 - P1,619.33 = P11,880.67",
                "Employer contributions = P1,912.50",
            ],
        },
    ]

    def handle(self, *args, **options):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "INTELLIHRTRACK - PAYROLL ACCURACY DEFENSE DATASET"
        ))
        self.stdout.write("=" * 78)

        source_branch = Branch.objects.filter(
            name__iexact=self.SOURCE_BRANCH_NAME
        ).first()
        if not source_branch:
            raise CommandError(
                f'{self.SOURCE_BRANCH_NAME} branch was not found.'
            )

        source_rules = PayrollRule.objects.filter(branch=source_branch).first()
        if not source_rules:
            raise CommandError("Source Occidental Mindoro PayrollRule was not found.")

        self._assert_source_rules(source_rules)

        branch, _ = Branch.objects.get_or_create(name=self.DEMO_BRANCH_NAME)
        rules, _ = PayrollRule.objects.update_or_create(
            branch=branch,
            defaults={
                "tax_rate_percent": source_rules.tax_rate_percent,
                "premium_rate_percent": source_rules.premium_rate_percent,
                "philhealth_default_mode": source_rules.philhealth_default_mode,
                "philhealth_default_value": source_rules.philhealth_default_value,
                "salary_divisor": source_rules.salary_divisor,
                "sss_minimum": source_rules.sss_minimum,
                "pagibig_minimum": source_rules.pagibig_minimum,
                "ot_multiplier": source_rules.ot_multiplier,
                "grace_minutes_normal": source_rules.grace_minutes_normal,
                "flag_ceremony_cutoff_time": source_rules.flag_ceremony_cutoff_time,
                "earliest_creditable_time": source_rules.earliest_creditable_time,
                "lunch_break_required": source_rules.lunch_break_required,
                "lunch_start_time": source_rules.lunch_start_time,
                "lunch_end_time": source_rules.lunch_end_time,
                "daily_hours_required": source_rules.daily_hours_required,
                "work_start_time": source_rules.work_start_time,
                "work_end_time": source_rules.work_end_time,
            },
        )

        period, _ = PayrollPeriod.objects.update_or_create(
            name=self.PERIOD_NAME,
            defaults={
                "start_date": self.START_DATE,
                "end_date": self.END_DATE,
                "pay_mode": PayrollPeriod.PAY_FIRST_HALF,
            },
        )

        admin_user = self._get_admin_user()
        self.stdout.write(f"Admin used for processing: {admin_user.username}")
        self.stdout.write(f"Verification branch: {branch.name}")
        self.stdout.write(f"Verification period: {period.name}")

        profiles = {}
        for cfg in self.EMPLOYEES:
            profiles[cfg["key"]] = self._upsert_employee(branch, cfg)

        self._clean_controlled_period(branch, period, list(profiles.values()))

        for cfg in self.EMPLOYEES:
            profile = profiles[cfg["key"]]
            self._configure_contributions(profile, cfg)

        workdays = self._workdays()
        if len(workdays) != 10:
            raise CommandError(
                f"Unexpected workday count: {len(workdays)}; expected 10."
            )

        for cfg in self.EMPLOYEES:
            profile = profiles[cfg["key"]]
            self._seed_employee_attendance(profile, branch, workdays, cfg)

            if cfg.get("travel_date"):
                TravelOrder.objects.update_or_create(
                    employee=profile,
                    start_date=cfg["travel_date"],
                    end_date=cfg["travel_date"],
                    defaults={
                        "reason": "Official field assignment - payroll verification scenario"
                    },
                )

            if cfg.get("ot_date"):
                OvertimeRequest.objects.update_or_create(
                    profile=profile,
                    date=cfg["ot_date"],
                    defaults={
                        "hours": cfg["ot_hours"],
                        "approved": True,
                        "approved_by": admin_user,
                        "reason": "Approved overtime - payroll verification scenario",
                    },
                )

        factory = RequestFactory()
        batches = {}

        for scope in (
            PayrollBatch.SCOPE_JO,
            PayrollBatch.SCOPE_COS,
            PayrollBatch.SCOPE_PERMANENT,
        ):
            request = factory.post(
                "/admin-ui/payroll/process/",
                {
                    "period": str(period.id),
                    "branch": str(branch.id),
                    "type": scope,
                    "search": self.USERNAME_MARKER,
                },
            )
            request.user = admin_user

            response = admin_payroll_process_batch(request)
            try:
                payload = json.loads(response.content.decode())
            except Exception:
                payload = {}

            if response.status_code != 200 or not payload.get("ok"):
                raise CommandError(
                    f"{scope} payroll processing failed: HTTP {response.status_code} {payload}"
                )

            if int(payload.get("total_items", 0)) != 3:
                raise CommandError(
                    f"{scope} should contain exactly 3 verification employees; "
                    f"found {payload.get('total_items')}."
                )

            batch = PayrollBatch.objects.get(id=payload["batch_id"])
            batches[scope] = batch
            self.stdout.write(self.style.SUCCESS(
                f"PASS  {scope} batch processed with 3 employees"
            ))

        failures = []
        export_employees = []

        for cfg in self.EMPLOYEES:
            profile = profiles[cfg["key"]]
            batch = batches[self._scope_for_profile(profile)]
            item = PayrollItem.objects.get(batch=batch, profile=profile)

            self._validate_item(cfg, item, failures)

            # Finalize DTR using the actual system view.
            finalize_dtr_request = factory.post(
                f"/admin-ui/payroll/item/{item.id}/dtr/finalize/"
            )
            self._prepare_message_request(finalize_dtr_request, admin_user)
            admin_finalize_payroll_item_dtr(finalize_dtr_request, item.id)

            dtr = FinalizedDTR.objects.filter(
                profile=profile,
                period=period,
                is_locked=True,
            ).first()
            if not dtr:
                failures.append(f"{profile.user.get_full_name()}: DTR did not finalize")

            meta = item.meta if isinstance(item.meta, dict) else {}
            export_employees.append({
                "name": profile.user.get_full_name() or profile.user.username,
                "username": profile.user.username,
                "biometric_employee_id": profile.biometric_employee_id,
                "employment_type": profile.employment_type,
                "department": profile.department,
                "position": profile.position,
                "scenario": cfg["scenario"],
                "formula": cfg["formula"],
                "salary_inputs": {
                    "daily_rate": str(profile.daily_rate or Decimal("0.00")),
                    "monthly_salary": str(profile.monthly_salary or Decimal("0.00")),
                    "pera_allowance": str(profile.pera_allowance or Decimal("0.00")),
                },
                "expected": cfg["expected"],
                "actual": {
                    "payroll_item": {
                        "base_pay": str(item.base_pay),
                        "premium_pay": str(item.premium_pay),
                        "overtime_hours": str(item.overtime_hours),
                        "overtime_pay": str(item.overtime_pay),
                        "late_minutes": item.late_minutes,
                        "undertime_minutes": item.undertime_minutes,
                        "absences": item.absences,
                        "government_contributions_total": str(item.gov_contributions_total),
                        "tax_total": str(item.tax_total),
                        "deductions_total": str(item.deductions_total),
                        "net_pay": str(item.net_pay),
                        "issues": item.issues or "",
                    },
                    "rates": meta.get("rates", {}),
                    "attendance_summary": meta.get("attendance_summary", {}),
                    "computed_payroll": meta.get("computed_payroll", {}),
                    "gov": meta.get("gov", {}),
                    "permanent_breakdown": meta.get("permanent_breakdown", {}),
                    "dtr_rows": meta.get("dtr_rows", []),
                    "dtr_locked": bool(dtr and dtr.is_locked),
                },
            })

        # Finalize each batch after every included DTR is locked.
        for scope, batch in batches.items():
            finalize_batch_request = factory.post(
                f"/admin-ui/payroll/batch/{batch.id}/finalize/"
            )
            self._prepare_message_request(finalize_batch_request, admin_user)
            admin_finalize_payroll_batch(finalize_batch_request, batch.id)
            batch.refresh_from_db()

            if batch.status != PayrollBatch.STATUS_FINALIZED:
                failures.append(f"{scope} batch did not finalize")
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"PASS  {scope} batch finalized"
                ))

        if failures:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR(
                f"PAYROLL DEFENSE DATASET FAILED - {len(failures)} difference(s)"
            ))
            for failure in failures:
                self.stdout.write(self.style.ERROR(f"  - {failure}"))
            raise CommandError(
                "Do not use the dataset for the defense until all differences are corrected."
            )

        export_payload = {
            "title": "IntelliHRTrack Payroll Accuracy Defense Dataset",
            "purpose": (
                "Controlled fictional employee dataset for face-to-face demonstration "
                "of the panel recommendation: Accurate payroll computation."
            ),
            "branch": branch.name,
            "source_rule_branch": source_branch.name,
            "period": {
                "name": period.name,
                "start_date": str(period.start_date),
                "end_date": str(period.end_date),
                "pay_mode": period.pay_mode,
                "workdays": [str(d) for d in workdays],
            },
            "rules": {
                "salary_divisor": str(rules.salary_divisor),
                "daily_hours_required": str(rules.daily_hours_required),
                "earliest_creditable_time": str(rules.earliest_creditable_time),
                "work_start_time": str(rules.work_start_time),
                "grace_minutes_normal": rules.grace_minutes_normal,
                "flag_ceremony_cutoff_time": str(rules.flag_ceremony_cutoff_time),
                "lunch_break_required": rules.lunch_break_required,
                "lunch_start_time": str(rules.lunch_start_time),
                "lunch_end_time": str(rules.lunch_end_time),
                "tax_rate_percent": str(rules.tax_rate_percent),
                "philhealth_default_value": str(rules.philhealth_default_value),
                "sss_minimum": str(rules.sss_minimum),
                "pagibig_minimum": str(rules.pagibig_minimum),
                "ot_multiplier": str(rules.ot_multiplier),
            },
            "employees": export_employees,
            "batches": {
                scope: {
                    "id": batch.id,
                    "name": batch.name,
                    "status": batch.status,
                    "totals_net": str(batch.totals_net),
                    "totals_deductions": str(batch.totals_deductions),
                }
                for scope, batch in batches.items()
            },
            "result": "PASS",
        }

        export_path = Path.cwd() / "payroll_defense_export.json"
        export_path.write_text(
            json.dumps(export_payload, indent=2, default=str),
            encoding="utf-8",
        )

        self.stdout.write("")
        self.stdout.write("=" * 78)
        self.stdout.write(self.style.SUCCESS(
            "PAYROLL DEFENSE DATASET PASSED"
        ))
        self.stdout.write(self.style.SUCCESS(
            "All nine saved payroll records match their manual answer keys and are finalized."
        ))
        self.stdout.write("")
        self.stdout.write("In IntelliHRTrack select:")
        self.stdout.write(f"  Branch : {branch.name}")
        self.stdout.write(f"  Period : {period.name}")
        self.stdout.write("  Type   : ALL")
        self.stdout.write("")
        self.stdout.write(f"PDF source export created at: {export_path}")
        self.stdout.write(
            "Upload payroll_defense_export.json to ChatGPT so the final defense PDF "
            "can be generated from the ACTUAL saved system data."
        )

    def _assert_source_rules(self, rules):
        checks = {
            "salary_divisor": (money(rules.salary_divisor), Decimal("22.00")),
            "daily_hours_required": (money(rules.daily_hours_required), Decimal("8.00")),
            "tax_rate_percent": (money(rules.tax_rate_percent), Decimal("5.00")),
            "philhealth_default_value": (money(rules.philhealth_default_value), Decimal("5.00")),
            "sss_minimum": (money(rules.sss_minimum), Decimal("760.00")),
            "pagibig_minimum": (money(rules.pagibig_minimum), Decimal("400.00")),
            "ot_multiplier": (money(rules.ot_multiplier), Decimal("1.25")),
        }
        for name, (actual, expected) in checks.items():
            if actual != expected:
                raise CommandError(
                    f"Source rule {name} is {actual}; defense answer key expects {expected}."
                )

        time_checks = {
            "earliest_creditable_time": (rules.earliest_creditable_time, time(7, 0)),
            "work_start_time": (rules.work_start_time, time(8, 0)),
            "flag_ceremony_cutoff_time": (rules.flag_ceremony_cutoff_time, time(8, 0)),
            "lunch_start_time": (rules.lunch_start_time, time(12, 0)),
            "lunch_end_time": (rules.lunch_end_time, time(13, 0)),
        }
        for name, (actual, expected) in time_checks.items():
            if actual != expected:
                raise CommandError(
                    f"Source rule {name} is {actual}; defense answer key expects {expected}."
                )

        if not rules.lunch_break_required:
            raise CommandError("Source rule lunch_break_required must be True.")
        if rules.grace_minutes_normal != 15:
            raise CommandError("Source rule grace_minutes_normal must be 15.")

    def _get_admin_user(self):
        admin = User.objects.filter(
            is_superuser=True,
            is_active=True,
        ).order_by("id").first()
        if not admin:
            admin = User.objects.filter(
                is_staff=True,
                is_active=True,
            ).order_by("id").first()
        if not admin:
            raise CommandError("No active staff/superuser account exists.")
        return admin

    def _upsert_employee(self, branch, cfg):
        user, _ = User.objects.update_or_create(
            username=cfg["username"],
            defaults={
                "first_name": cfg["first_name"],
                "last_name": cfg["last_name"],
                "email": "",
                "is_active": True,
                "is_staff": False,
                "is_superuser": False,
            },
        )
        user.set_unusable_password()
        user.save(update_fields=["password"])

        profile, _ = UserProfile.objects.update_or_create(
            user=user,
            defaults={
                "branch": branch,
                "employment_type": cfg["employment_type"],
                "department": cfg["department"],
                "position": cfg["position"],
                "biometric_employee_id": cfg["biometric_id"],
                "daily_rate": cfg["daily_rate"],
                "monthly_salary": cfg["monthly_salary"],
                "employment_start_date": date(2026, 1, 1),
                "employment_end_date": None,
                "pera_allowance": cfg["pera"],
                "other_earnings_amount": Decimal("0.00"),
                "manual_deduction_amount": Decimal("0.00"),
                "has_premium": False,
                "is_approved": True,
            },
        )
        return profile

    def _clean_controlled_period(self, branch, period, profiles):
        biometric_ids = [p.biometric_employee_id for p in profiles]
        start_dt = datetime(
            self.START_DATE.year,
            self.START_DATE.month,
            self.START_DATE.day,
            0,
            0,
            tzinfo=self.MANILA,
        )
        end_day = self.END_DATE + timedelta(days=1)
        end_dt = datetime(
            end_day.year,
            end_day.month,
            end_day.day,
            0,
            0,
            tzinfo=self.MANILA,
        )

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

        AttendanceRecord.objects.filter(
            branch=branch,
            employee_id__in=biometric_ids,
            timestamp__gte=start_dt,
            timestamp__lt=end_dt,
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

    def _configure_contributions(self, profile, cfg):
        if cfg["employment_type"] in {
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

    def _workdays(self):
        days = []
        current = self.START_DATE
        while current <= self.END_DATE:
            if current.weekday() < 5:
                days.append(current)
            current += timedelta(days=1)
        return days

    def _seed_employee_attendance(self, profile, branch, workdays, cfg):
        for workday in workdays:
            if workday in cfg["absence_dates"]:
                continue
            if cfg.get("travel_date") == workday:
                continue

            if cfg.get("irregular_date") == workday:
                punches = [
                    (8, 25, AttendanceRecord.STATUS_CHECKIN, "Check In", "checkIn"),
                    (12, 0, AttendanceRecord.STATUS_CHECKOUT, "Check Out", "checkOut"),
                    (13, 0, AttendanceRecord.STATUS_CHECKIN, "Check In", "checkIn"),
                    (17, 5, AttendanceRecord.STATUS_CHECKOUT, "Check Out", "checkOut"),
                ]
            elif cfg.get("ot_date") == workday:
                punches = [
                    (7, 0, AttendanceRecord.STATUS_CHECKIN, "Check In", "checkIn"),
                    (12, 0, AttendanceRecord.STATUS_CHECKOUT, "Check Out", "checkOut"),
                    (13, 0, AttendanceRecord.STATUS_CHECKIN, "Check In", "checkIn"),
                    (17, 30, AttendanceRecord.STATUS_CHECKOUT, "Check Out", "checkOut"),
                ]
            else:
                punches = [
                    (7, 0, AttendanceRecord.STATUS_CHECKIN, "Check In", "checkIn"),
                    (12, 0, AttendanceRecord.STATUS_CHECKOUT, "Check Out", "checkOut"),
                    (13, 0, AttendanceRecord.STATUS_CHECKIN, "Check In", "checkIn"),
                    (16, 0, AttendanceRecord.STATUS_CHECKOUT, "Check Out", "checkOut"),
                ]

            for hour, minute, status, label, raw_status in punches:
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
                    full_name=profile.user.get_full_name(),
                    department=profile.department,
                    branch=branch,
                    timestamp=timestamp,
                    attendance_status=status,
                    raw_row={
                        "time": timestamp.isoformat(),
                        "attendanceStatus": raw_status,
                        "label": label,
                    },
                )

    def _scope_for_profile(self, profile):
        if profile.employment_type == UserProfile.EMP_JO:
            return PayrollBatch.SCOPE_JO
        if profile.employment_type == UserProfile.EMP_COS:
            return PayrollBatch.SCOPE_COS
        return PayrollBatch.SCOPE_PERMANENT

    def _validate_item(self, cfg, item, failures):
        meta = item.meta if isinstance(item.meta, dict) else {}
        summary = meta.get("attendance_summary", {}) if isinstance(meta.get("attendance_summary"), dict) else {}
        computed = meta.get("computed_payroll", {}) if isinstance(meta.get("computed_payroll"), dict) else {}
        gov = meta.get("gov", {}) if isinstance(meta.get("gov"), dict) else {}
        permanent = meta.get("permanent_breakdown", {}) if isinstance(meta.get("permanent_breakdown"), dict) else {}
        expected = cfg["expected"]
        name = item.profile.user.get_full_name() or item.profile.user.username

        def compare_value(label, actual, exp):
            if actual != exp:
                failures.append(f"{name} / {label}: actual={actual}, expected={exp}")
            else:
                self.stdout.write(self.style.SUCCESS(f"PASS  {name} / {label}: {actual}"))

        def compare_money(label, actual, exp):
            actual_m = money(actual or Decimal("0.00"))
            exp_m = money(exp)
            compare_value(label, actual_m, exp_m)

        compare_value("present days", int(summary.get("present_days", 0) or 0), expected["present_days"])
        compare_value("travel days", int(summary.get("travel_days", 0) or 0), expected["travel_days"])
        compare_value("absences", int(item.absences or 0), expected["absences"])
        compare_value("late minutes", int(item.late_minutes or 0), expected["late_minutes"])
        compare_value("undertime minutes", int(item.undertime_minutes or 0), expected["undertime_minutes"])
        compare_money("base", item.base_pay, expected["base"])
        compare_money("gross", computed.get("gross", item.base_pay), expected["gross"])
        compare_money("deductions", item.deductions_total, expected["deductions"])
        compare_money("net", item.net_pay, expected["net"])

        if cfg["employment_type"] in {UserProfile.EMP_JO, UserProfile.EMP_COS}:
            compare_money("attendance deduction", computed.get("attendance_deduction", "0.00"), expected["attendance_deduction"])
            compare_money("earned after attendance", computed.get("earned_after_attendance", "0.00"), expected["earned_after_attendance"])
            compare_money("SSS", gov.get("sss", "0.00"), expected["sss"])
            compare_money("PhilHealth", gov.get("philhealth", "0.00"), expected["philhealth"])
            compare_money("Pag-IBIG", gov.get("pagibig", "0.00"), expected["pagibig"])
            compare_money("tax", gov.get("tax", "0.00"), expected["tax"])
            compare_money("OT hours", item.overtime_hours, expected["overtime_hours"])
            compare_money("OT pay", item.overtime_pay, expected["overtime_pay"])
        else:
            compare_money("PERA", permanent.get("pera", "0.00"), expected["pera"])
            compare_money("PhilHealth", gov.get("philhealth", "0.00"), expected["philhealth"])
            compare_money("Pag-IBIG", gov.get("pagibig", "0.00"), expected["pagibig"])
            compare_money("GSIS employee", permanent.get("gsis_employee", "0.00"), expected["gsis_employee"])
            compare_money("GSIS employer", permanent.get("gsis_employer", "0.00"), expected["gsis_employer"])
            compare_money("taxable compensation", gov.get("taxable_compensation", "0.00"), expected["taxable_compensation"])
            compare_money("WTAX", gov.get("tax", "0.00"), expected["tax"])
            compare_money("employer contributions", permanent.get("employer_contributions_total", "0.00"), expected["employer_contributions_total"])

        if item.issues:
            failures.append(f"{name} has payroll issues: {item.issues}")

    def _prepare_message_request(self, request, user):
        request.user = user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request
