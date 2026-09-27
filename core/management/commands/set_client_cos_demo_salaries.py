from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Branch, EmployeeContribution, UserProfile


class Command(BaseCommand):
    help = (
        "Assign controlled DEMO monthly salaries and standard configured "
        "COS deductions to all client-validation COS employees. "
        "These are test/default values, NOT client-provided salary data."
    )

    BRANCH_NAME = "DA MIMAROPA Client Validation"

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Actually save the demo salary values. Without this flag, dry-run only.",
        )

    def handle(self, *args, **options):
        commit = bool(options["commit"])

        branch = Branch.objects.filter(
            name=self.BRANCH_NAME
        ).first()

        if not branch:
            raise CommandError(
                f'Branch "{self.BRANCH_NAME}" was not found.'
            )

        profiles = list(
            UserProfile.objects
            .select_related("user")
            .filter(
                branch=branch,
                employment_type=UserProfile.EMP_COS,
                is_approved=True,
            )
            .order_by("biometric_employee_id", "id")
        )

        if not profiles:
            raise CommandError(
                "No approved COS profiles found in the client validation branch."
            )

        # Controlled DEMO salary pattern.
        # Most employees use 22,000; a small number use 24,000 or 26,000
        # so the payroll screen is not unrealistically identical.
        salary_pattern = [
            Decimal("22000.00"),
            Decimal("22000.00"),
            Decimal("22000.00"),
            Decimal("22000.00"),
            Decimal("24000.00"),
            Decimal("22000.00"),
            Decimal("22000.00"),
            Decimal("26000.00"),
        ]

        assignments = []

        for index, profile in enumerate(profiles):
            salary = salary_pattern[index % len(salary_pattern)]

            assignments.append(
                (
                    profile,
                    salary,
                )
            )

        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "CLIENT COS DEMO SALARY ASSIGNMENT"
            )
        )
        self.stdout.write("=" * 78)
        self.stdout.write(f"Branch     : {branch.name}")
        self.stdout.write(f"Employees  : {len(assignments)}")
        self.stdout.write(
            f"Mode       : {'COMMIT' if commit else 'DRY-RUN (nothing written)'}"
        )
        self.stdout.write(
            "Salary note: CONTROLLED TEST VALUES - NOT PROVIDED BY CLIENT"
        )
        self.stdout.write("")

        for profile, salary in assignments[:20]:
            name = (
                profile.user.get_full_name()
                or profile.user.username
            )

            self.stdout.write(
                f"  ID {profile.biometric_employee_id:<8} "
                f"{name[:34]:<34} "
                f"Monthly Salary = PHP {salary:,.2f}"
            )

        if len(assignments) > 20:
            self.stdout.write(
                f"  ... plus {len(assignments) - 20} more employee(s)"
            )

        if not commit:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "DRY-RUN ONLY. Review the salary pattern above. "
                    "Rerun with --commit to save."
                )
            )
            return

        with transaction.atomic():
            for profile, salary in assignments:
                profile.monthly_salary = salary
                profile.daily_rate = Decimal("0.00")
                profile.has_premium = False
                profile.manual_deduction_amount = Decimal("0.00")
                profile.other_earnings_amount = Decimal("0.00")

                profile.save(
                    update_fields=[
                        "monthly_salary",
                        "daily_rate",
                        "has_premium",
                        "manual_deduction_amount",
                        "other_earnings_amount",
                    ]
                )

                EmployeeContribution.objects.update_or_create(
                    profile=profile,
                    defaults={
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
                    },
                )

        self.stdout.write("")
        self.stdout.write("=" * 78)
        self.stdout.write(
            self.style.SUCCESS(
                "CLIENT COS DEMO SALARIES SAVED SUCCESSFULLY"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(assignments)} COS employees now have demo monthly salaries."
            )
        )
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                "IMPORTANT: These salary amounts are controlled test/default values, "
                "not client-verified salary data. You may edit any employee's "
                "monthly salary later in Employee Management."
            )
        )
