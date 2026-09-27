from __future__ import annotations

import re
from collections import OrderedDict

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import AttendanceRecord, Branch, UserProfile


class Command(BaseCommand):
    help = (
        "Create/link IntelliHRTrack employee profiles for all employees already "
        "imported into the DA MIMAROPA Client Validation branch. All created/"
        "updated profiles are set to COS. No salary is invented."
    )

    DEFAULT_BRANCH = "DA MIMAROPA Client Validation"

    def add_arguments(self, parser):
        parser.add_argument(
            "--branch",
            default=self.DEFAULT_BRANCH,
            help="Branch containing the imported client attendance.",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Actually create/update users and profiles. Without this flag, dry-run only.",
        )

    def handle(self, *args, **options):
        branch_name = str(options["branch"]).strip()
        commit = bool(options["commit"])

        branch = Branch.objects.filter(name=branch_name).first()
        if not branch:
            raise CommandError(f'Branch not found: "{branch_name}"')

        records = (
            AttendanceRecord.objects
            .filter(branch=branch)
            .order_by("employee_id", "timestamp")
        )

        if not records.exists():
            raise CommandError(
                f'No attendance records found in branch "{branch_name}".'
            )

        # One canonical row per biometric employee ID.
        employees = OrderedDict()

        for rec in records.iterator():
            emp_id = str(rec.employee_id or "").strip()
            if not emp_id:
                continue

            if emp_id not in employees:
                employees[emp_id] = {
                    "employee_id": emp_id,
                    "full_name": (rec.full_name or "").strip() or f"Employee {emp_id}",
                    "department": (rec.department or "").strip(),
                }

        if not employees:
            raise CommandError("No usable employee IDs found.")

        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING("CLIENT COS EMPLOYEE PROFILE LINKER")
        )
        self.stdout.write("=" * 78)
        self.stdout.write(f"Branch      : {branch.name}")
        self.stdout.write(f"Employees   : {len(employees)}")
        self.stdout.write(
            f"Mode        : {'COMMIT' if commit else 'DRY-RUN (nothing written)'}"
        )
        self.stdout.write(
            "Employment  : COS for all imported client employees"
        )
        self.stdout.write(
            "Salary      : NOT CHANGED / NOT INVENTED"
        )
        self.stdout.write("")

        preview_count = 0
        for emp in employees.values():
            username = self._username_for(emp["employee_id"])
            self.stdout.write(
                f'  ID {emp["employee_id"]:<8} '
                f'{emp["full_name"][:34]:<34} '
                f'-> username {username}'
            )
            preview_count += 1
            if preview_count >= 20:
                break

        if len(employees) > 20:
            self.stdout.write(
                f"  ... plus {len(employees) - 20} more employee(s)"
            )

        if not commit:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "DRY-RUN ONLY. No users/profiles were created."
                )
            )
            self.stdout.write(
                "If the preview is correct, rerun with --commit."
            )
            return

        created_users = 0
        updated_users = 0
        created_profiles = 0
        updated_profiles = 0
        conflicts = []

        with transaction.atomic():
            for emp in employees.values():
                employee_id = emp["employee_id"]
                username = self._username_for(employee_id)

                # Never hijack a profile already using this username with a
                # different biometric ID.
                user = User.objects.filter(username=username).first()

                if user:
                    existing_profile = getattr(user, "profile", None)
                    if (
                        existing_profile
                        and existing_profile.biometric_employee_id
                        and str(existing_profile.biometric_employee_id).strip()
                        != employee_id
                    ):
                        conflicts.append(
                            f"{username}: existing biometric ID "
                            f"{existing_profile.biometric_employee_id} != {employee_id}"
                        )
                        continue

                    updated_users += 1
                else:
                    user = User(username=username)
                    user.set_unusable_password()
                    created_users += 1

                # Preserve the client's full name in Django's display name.
                user.first_name = emp["full_name"][:150]
                user.last_name = ""
                user.email = ""
                user.is_active = True
                user.is_staff = False
                user.is_superuser = False
                user.save()

                profile, created = UserProfile.objects.get_or_create(
                    user=user,
                    defaults={
                        "branch": branch,
                        "employment_type": UserProfile.EMP_COS,
                        "department": emp["department"],
                        "position": "",
                        "biometric_employee_id": employee_id,
                        "is_approved": True,
                    },
                )

                if created:
                    created_profiles += 1
                else:
                    # Intentionally do NOT touch daily_rate/monthly_salary/PERA.
                    profile.branch = branch
                    profile.employment_type = UserProfile.EMP_COS
                    profile.department = emp["department"]
                    profile.biometric_employee_id = employee_id
                    profile.is_approved = True
                    profile.save(
                        update_fields=[
                            "branch",
                            "employment_type",
                            "department",
                            "biometric_employee_id",
                            "is_approved",
                        ]
                    )
                    updated_profiles += 1

        self.stdout.write("")
        self.stdout.write("=" * 78)
        self.stdout.write(
            self.style.SUCCESS("CLIENT COS EMPLOYEE LINKING COMPLETE")
        )
        self.stdout.write(f"Users created   : {created_users}")
        self.stdout.write(f"Users updated   : {updated_users}")
        self.stdout.write(f"Profiles created: {created_profiles}")
        self.stdout.write(f"Profiles updated: {updated_profiles}")
        self.stdout.write(f"Conflicts skipped: {len(conflicts)}")

        if conflicts:
            for item in conflicts[:10]:
                self.stdout.write(self.style.WARNING(f"  - {item}"))

        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                "IMPORTANT: These employees are now linked to their REAL imported "
                "attendance and marked COS, but accurate payroll still requires the "
                "client's actual monthly salary and deduction/contribution settings."
            )
        )

    def _username_for(self, employee_id: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9]+", "_", employee_id).strip("_").lower()
        if not safe:
            safe = "unknown"
        return f"client_cos_{safe}"[:150]
