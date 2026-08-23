import json

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management.base import (
    BaseCommand,
    CommandError,
)
from django.http import HttpResponse
from django.test import RequestFactory

from core.models import (
    Branch,
    EmployeeContribution,
    PayrollBatch,
    PayrollItem,
    PayrollPeriod,
    PayrollRule,
    UserProfile,
)

from core.payroll_calculations import money

from core.views import (
    _build_payslip_data,
    admin_payroll_batch_detail,
    admin_payroll_process_batch,
)


class Command(BaseCommand):
    help = (
        "Verify that processed PayrollItem, payslip, "
        "and payroll register preserve identical "
        "official payroll amounts."
    )

    PERIOD_NAME = (
        "Payroll Accuracy Verification - October 1-15 2026"
    )

    EMPLOYEES = {
        "JO": {
            "username": "miguel.andres.navarro",
            "name": "Miguel Andres Navarro",
            "net": Decimal("7463.84"),
            "base": Decimal("9000.00"),
            "gross": Decimal("9000.00"),
            "deductions": Decimal("1536.16"),
            "gov": Decimal("1026.88"),
            "tax": Decimal("446.88"),
            "attendance": Decimal("62.40"),
            "daily_rate": Decimal("1000.00"),
            "sss": Decimal("380.00"),
            "philhealth": Decimal("446.88"),
            "pagibig": Decimal("200.00"),
            "present_days": 9,
            "absences": 2,
            "late": 10,
            "undertime": 20,
            "earned": Decimal("8937.60"),
            "employer_cost": Decimal("0.00"),
        },

        "COS": {
            "username": "camille.rose.mendoza",
            "name": "Camille Rose Mendoza",
            "net": Decimal("8363.84"),
            "base": Decimal("11000.00"),
            "gross": Decimal("11000.00"),
            "deductions": Decimal("2636.16"),
            "gov": Decimal("1076.88"),
            "tax": Decimal("496.88"),
            "attendance": Decimal("1062.40"),
            "daily_rate": Decimal("1000.00"),
            "sss": Decimal("380.00"),
            "philhealth": Decimal("496.88"),
            "pagibig": Decimal("200.00"),
            "present_days": 10,
            "absences": 1,
            "late": 10,
            "undertime": 20,
            "earned": Decimal("9937.60"),
            "employer_cost": Decimal("0.00"),
        },

        "PERMANENT": {
            "username": "adrian.luis.villanueva",
            "name": "Adrian Luis Villanueva",
            "net": Decimal("13761.30"),
            "base": Decimal("15000.00"),
            "gross": Decimal("16000.00"),
            "deductions": Decimal("2238.70"),
            "gov": Decimal("1825.00"),
            "tax": Decimal("413.70"),
            "attendance": Decimal("0.00"),
            "present_days": 11,
            "absences": 0,
            "late": 0,
            "undertime": 0,
            "philhealth": Decimal("375.00"),
            "pagibig": Decimal("100.00"),
            "gsis_employee": Decimal("1350.00"),
            "gsis_employer": Decimal("1800.00"),
            "employer_cost": Decimal("2275.00"),
        },
    }

    def handle(self, *args, **options):
        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "INTELLIHRTRACK PAYROLL ACCURACY — STEP G"
            )
        )
        self.stdout.write(
            "=" * 72
        )

        branch = Branch.objects.filter(
            name__iexact="Occidental Mindoro"
        ).first()

        if not branch:
            raise CommandError(
                "Occidental Mindoro branch not found."
            )

        period = PayrollPeriod.objects.filter(
            name=self.PERIOD_NAME
        ).first()

        if not period:
            raise CommandError(
                "Step F payroll period was not found. "
                "Run payroll_accuracy_step_f first."
            )

        rules = PayrollRule.objects.filter(
            branch=branch
        ).first()

        if not rules:
            raise CommandError(
                "PayrollRule for Occidental Mindoro "
                "was not found."
            )

        admin_user = (
            User.objects
            .filter(
                is_superuser=True,
                is_active=True,
            )
            .order_by("id")
            .first()
        )

        if not admin_user:
            admin_user = (
                User.objects
                .filter(
                    is_staff=True,
                    is_active=True,
                )
                .order_by("id")
                .first()
            )

        if not admin_user:
            raise CommandError(
                "No active staff/superuser account "
                "exists for the real admin processing test."
            )

        self.stdout.write(
            f"Using admin account: {admin_user.username}"
        )

        factory = RequestFactory()

        failures = []

        for scope in (
            PayrollBatch.SCOPE_JO,
            PayrollBatch.SCOPE_COS,
            PayrollBatch.SCOPE_PERMANENT,
        ):
            config = self.EMPLOYEES[scope]

            profile = (
                UserProfile.objects
                .select_related(
                    "user",
                    "branch",
                )
                .filter(
                    user__username=config["username"]
                )
                .first()
            )

            if not profile:
                raise CommandError(
                    f'{config["name"]} was not found. '
                    "Run Step F first."
                )

            self.stdout.write("")
            self.stdout.write(
                self.style.MIGRATE_LABEL(
                    f"{scope} — {config['name']}"
                )
            )

            # =================================================
            # 1. CALL THE REAL ADMIN PAYROLL PROCESSING VIEW
            # =================================================
            request = factory.post(
                "/admin-ui/payroll/process/",
                {
                    "period": str(period.id),
                    "branch": str(branch.id),
                    "type": scope,
                    "search": config["username"],
                },
            )

            request.user = admin_user

            response = admin_payroll_process_batch(
                request
            )

            if response.status_code != 200:
                failures.append(
                    f"{scope}: process response "
                    f"HTTP {response.status_code}"
                )

                self.stdout.write(
                    self.style.ERROR(
                        "FAIL  Real payroll processing "
                        f"returned HTTP {response.status_code}"
                    )
                )

                try:
                    self.stdout.write(
                        response.content.decode()
                    )
                except Exception:
                    pass

                continue

            payload = json.loads(
                response.content.decode()
            )

            if not payload.get("ok"):
                failures.append(
                    f"{scope}: process returned ok=False"
                )

                self.stdout.write(
                    self.style.ERROR(
                        f"FAIL  Processing response: {payload}"
                    )
                )

                continue

            self.stdout.write(
                self.style.SUCCESS(
                    "PASS  Real admin payroll processing"
                )
            )

            # =================================================
            # 2. FIND SAVED BATCH + PAYROLL ITEM
            # =================================================
            batch = (
                PayrollBatch.objects
                .filter(
                    branch=branch,
                    period=period,
                    employee_type_scope=scope,
                )
                .first()
            )

            if not batch:
                failures.append(
                    f"{scope}: no PayrollBatch saved"
                )
                continue

            items = PayrollItem.objects.filter(
                batch=batch
            )

            if items.count() != 1:
                failures.append(
                    f"{scope}: expected exactly 1 "
                    f"PayrollItem, found {items.count()}"
                )

                self.stdout.write(
                    self.style.ERROR(
                        "FAIL  Expected exactly one "
                        f"PayrollItem, got {items.count()}"
                    )
                )

                continue

            item = items.select_related(
                "profile",
                "profile__user",
            ).first()

            self.stdout.write(
                self.style.SUCCESS(
                    "PASS  Exactly one saved PayrollItem"
                )
            )

            self.check_money(
                failures,
                scope,
                "PayrollItem base",
                item.base_pay,
                config["base"],
            )

            self.check_money(
                failures,
                scope,
                "PayrollItem deductions",
                item.deductions_total,
                config["deductions"],
            )

            self.check_money(
                failures,
                scope,
                "PayrollItem government total",
                item.gov_contributions_total,
                config["gov"],
            )

            self.check_money(
                failures,
                scope,
                "PayrollItem tax",
                item.tax_total,
                config["tax"],
            )

            self.check_money(
                failures,
                scope,
                "PayrollItem net",
                item.net_pay,
                config["net"],
            )

            self.check_value(
                failures,
                scope,
                "PayrollItem late minutes",
                int(item.late_minutes or 0),
                config["late"],
            )

            self.check_value(
                failures,
                scope,
                "PayrollItem undertime minutes",
                int(item.undertime_minutes or 0),
                config["undertime"],
            )

            self.check_value(
                failures,
                scope,
                "PayrollItem absences",
                int(item.absences or 0),
                config["absences"],
            )

            # =================================================
            # 3. VERIFY SAVED SNAPSHOT
            # =================================================
            meta = (
                item.meta
                if isinstance(item.meta, dict)
                else {}
            )

            computed_meta = (
                meta.get("computed_payroll")
                if isinstance(
                    meta.get("computed_payroll"),
                    dict,
                )
                else {}
            )

            attendance_meta = (
                meta.get("attendance_summary")
                if isinstance(
                    meta.get("attendance_summary"),
                    dict,
                )
                else {}
            )

            gov_meta = (
                meta.get("gov")
                if isinstance(
                    meta.get("gov"),
                    dict,
                )
                else {}
            )

            self.check_value(
                failures,
                scope,
                "Snapshot present days",
                int(
                    attendance_meta.get(
                        "present_days",
                        0,
                    )
                    or 0
                ),
                config["present_days"],
            )

            if scope in {
                PayrollBatch.SCOPE_JO,
                PayrollBatch.SCOPE_COS,
            }:
                self.check_money(
                    failures,
                    scope,
                    "Snapshot earned after attendance",
                    computed_meta.get(
                        "earned_after_attendance",
                        "0.00",
                    ),
                    config["earned"],
                )

            if scope == PayrollBatch.SCOPE_PERMANENT:
                permanent_meta = (
                    meta.get("permanent_breakdown")
                    if isinstance(
                        meta.get(
                            "permanent_breakdown"
                        ),
                        dict,
                    )
                    else {}
                )

                self.check_money(
                    failures,
                    scope,
                    "Snapshot GSIS employee",
                    permanent_meta.get(
                        "gsis_employee",
                        "0.00",
                    ),
                    config["gsis_employee"],
                )

                self.check_money(
                    failures,
                    scope,
                    "Snapshot GSIS employer",
                    permanent_meta.get(
                        "gsis_employer",
                        "0.00",
                    ),
                    config["gsis_employer"],
                )

                self.check_money(
                    failures,
                    scope,
                    "Snapshot employer cost",
                    permanent_meta.get(
                        "employer_contributions_total",
                        "0.00",
                    ),
                    config["employer_cost"],
                )

            # =================================================
            # 4. VERIFY SAVED PAYSLIP
            # =================================================
            payslip_data = _build_payslip_data(
                profile,
                period,
                branch,
                rules,
                admin_user,
                saved_item=item,
            )

            payslip = payslip_data["payslip"]

            self.check_money(
                failures,
                scope,
                "Payslip gross",
                payslip["gross_pay"],
                config["gross"],
            )

            self.check_money(
                failures,
                scope,
                "Payslip deductions",
                payslip["total_deductions"],
                config["deductions"],
            )

            self.check_money(
                failures,
                scope,
                "Payslip net",
                payslip["net_pay"],
                config["net"],
            )

            self.check_value(
                failures,
                scope,
                "Payslip present days",
                int(
                    payslip.get(
                        "days_present",
                        0,
                    )
                    or 0
                ),
                config["present_days"],
            )

            self.check_value(
                failures,
                scope,
                "Payslip employee name",
                payslip.get(
                    "employee_name"
                ),
                config["name"],
            )

            if scope in {
                PayrollBatch.SCOPE_JO,
                PayrollBatch.SCOPE_COS,
            }:
                self.check_money(
                    failures,
                    scope,
                    "Payslip SSS",
                    payslip["sss"],
                    config["sss"],
                )

                self.check_money(
                    failures,
                    scope,
                    "Payslip PhilHealth",
                    payslip["philhealth"],
                    config["philhealth"],
                )

                self.check_money(
                    failures,
                    scope,
                    "Payslip Pag-IBIG",
                    payslip["pagibig"],
                    config["pagibig"],
                )

                self.check_money(
                    failures,
                    scope,
                    "Payslip daily rate",
                    payslip["daily_rate"],
                    config["daily_rate"],
                )

            if scope == PayrollBatch.SCOPE_PERMANENT:
                self.check_money(
                    failures,
                    scope,
                    "Payslip PhilHealth",
                    payslip["philhealth"],
                    config["philhealth"],
                )

                self.check_money(
                    failures,
                    scope,
                    "Payslip Pag-IBIG",
                    payslip["pagibig"],
                    config["pagibig"],
                )

                self.check_money(
                    failures,
                    scope,
                    "Payslip GSIS employee",
                    payslip["gsis_employee"],
                    config["gsis_employee"],
                )

                self.check_money(
                    failures,
                    scope,
                    "Payslip GSIS employer",
                    payslip["gsis_employer"],
                    config["gsis_employer"],
                )

                self.check_money(
                    failures,
                    scope,
                    "Payslip employer contributions",
                    payslip[
                        "employer_contributions_total"
                    ],
                    config["employer_cost"],
                )

            # =================================================
            # 5. PROVE PAYSLIP IS SNAPSHOT-STABLE
            # =================================================
            if scope == PayrollBatch.SCOPE_JO:
                original_daily = profile.daily_rate

                contrib = (
                    EmployeeContribution.objects
                    .get(
                        profile=profile
                    )
                )

                original_sss = contrib.sss_amount

                try:
                    # Deliberately corrupt LIVE values.
                    profile.daily_rate = Decimal(
                        "9999.00"
                    )
                    profile.save(
                        update_fields=[
                            "daily_rate"
                        ]
                    )

                    contrib.sss_amount = Decimal(
                        "9999.00"
                    )
                    contrib.save(
                        update_fields=[
                            "sss_amount"
                        ]
                    )

                    profile.refresh_from_db()

                    stable_data = _build_payslip_data(
                        profile,
                        period,
                        branch,
                        rules,
                        admin_user,
                        saved_item=item,
                    )

                    stable = stable_data[
                        "payslip"
                    ]

                    self.check_money(
                        failures,
                        scope,
                        "Saved payslip daily rate "
                        "after LIVE rate modification",
                        stable["daily_rate"],
                        Decimal("1000.00"),
                    )

                    self.check_money(
                        failures,
                        scope,
                        "Saved payslip SSS "
                        "after LIVE contribution modification",
                        stable["sss"],
                        Decimal("380.00"),
                    )

                    self.check_money(
                        failures,
                        scope,
                        "Saved payslip NET "
                        "after LIVE modification",
                        stable["net_pay"],
                        Decimal("7463.84"),
                    )

                finally:
                    profile.daily_rate = (
                        original_daily
                    )

                    profile.save(
                        update_fields=[
                            "daily_rate"
                        ]
                    )

                    contrib.sss_amount = (
                        original_sss
                    )

                    contrib.save(
                        update_fields=[
                            "sss_amount"
                        ]
                    )

            # =================================================
            # 6. VERIFY REAL PAYROLL REGISTER VIEW
            # =================================================
            register_request = factory.get(
                f"/admin-ui/payroll/batch/{batch.id}/"
            )

            register_request.user = admin_user

            captured = {}

            def fake_render(
                request,
                template_name,
                context,
            ):
                captured["template"] = (
                    template_name
                )
                captured["context"] = context

                return HttpResponse(
                    "REGISTER TEST OK"
                )

            with patch(
                "core.views.render",
                side_effect=fake_render,
            ):
                register_response = (
                    admin_payroll_batch_detail(
                        register_request,
                        batch.id,
                    )
                )

            if register_response.status_code != 200:
                failures.append(
                    f"{scope}: register view "
                    f"HTTP {register_response.status_code}"
                )

                continue

            context = captured.get(
                "context",
                {},
            )

            register_rows = context.get(
                "register_rows",
                [],
            )

            if len(register_rows) != 1:
                failures.append(
                    f"{scope}: expected one register "
                    f"row, found {len(register_rows)}"
                )

                self.stdout.write(
                    self.style.ERROR(
                        "FAIL  Payroll register row count"
                    )
                )

                continue

            row = register_rows[0]

            self.check_money(
                failures,
                scope,
                "Register gross",
                row["gross_pay"],
                config["gross"],
            )

            self.check_money(
                failures,
                scope,
                "Register deductions",
                row["total_deductions"],
                config["deductions"],
            )

            self.check_money(
                failures,
                scope,
                "Register net",
                row["net_pay"],
                config["net"],
            )

            self.check_money(
                failures,
                scope,
                "Register calculated total net",
                context[
                    "calculated_total_net"
                ],
                config["net"],
            )

            self.check_money(
                failures,
                scope,
                "Register calculated total deductions",
                context[
                    "calculated_total_deductions"
                ],
                config["deductions"],
            )

            self.check_money(
                failures,
                scope,
                "Register calculated total gross",
                context[
                    "calculated_total_gross"
                ],
                config["gross"],
            )

            self.check_money(
                failures,
                scope,
                "Register employer cost",
                context[
                    "calculated_total_employer_cost"
                ],
                config["employer_cost"],
            )

        self.stdout.write("")
        self.stdout.write(
            "=" * 72
        )

        if failures:
            self.stdout.write(
                self.style.ERROR(
                    f"STEP G FAILED — "
                    f"{len(failures)} difference(s)."
                )
            )

            for failure in failures:
                self.stdout.write(
                    self.style.ERROR(
                        f"  - {failure}"
                    )
                )

            raise CommandError(
                "Saved payroll records are not "
                "fully consistent."
            )

        self.stdout.write(
            self.style.SUCCESS(
                "STEP G PASSED"
            )
        )

        self.stdout.write(
            self.style.SUCCESS(
                "PayrollItem, saved payslip, "
                "and payroll register all preserve "
                "the same official payroll amounts."
            )
        )

    def check_money(
        self,
        failures,
        scope,
        label,
        actual,
        expected,
    ):
        actual_money = money(
            actual or Decimal("0.00")
        )

        expected_money = money(
            expected
        )

        if actual_money == expected_money:
            self.stdout.write(
                self.style.SUCCESS(
                    f"PASS  {label}: "
                    f"{actual_money}"
                )
            )

        else:
            failure = (
                f"{scope} / {label}: "
                f"actual={actual_money}, "
                f"expected={expected_money}"
            )

            failures.append(
                failure
            )

            self.stdout.write(
                self.style.ERROR(
                    f"FAIL  {label}: "
                    f"actual={actual_money}, "
                    f"expected={expected_money}"
                )
            )

    def check_value(
        self,
        failures,
        scope,
        label,
        actual,
        expected,
    ):
        if actual == expected:
            self.stdout.write(
                self.style.SUCCESS(
                    f"PASS  {label}: {actual}"
                )
            )

        else:
            failure = (
                f"{scope} / {label}: "
                f"actual={actual}, "
                f"expected={expected}"
            )

            failures.append(
                failure
            )

            self.stdout.write(
                self.style.ERROR(
                    f"FAIL  {label}: "
                    f"actual={actual}, "
                    f"expected={expected}"
                )
            )