import json

from datetime import datetime, time, timedelta
from decimal import Decimal
from django.db import transaction
from django.conf import settings
from django.utils import timezone

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import (
    FallbackStorage,
)
from django.core.management.base import (
    BaseCommand,
    CommandError,
)
from django.test import RequestFactory

from core.models import (
    AttendanceRecord,
    Branch,
    FinalizedDTR,
    PayrollBatch,
    PayrollItem,
    PayrollPeriod,
    UserProfile,
)

from core.payroll_calculations import money

from core.views import (
    _build_payroll_batch_validation,
    _locked_dtr_for_attendance_record,
    admin_finalize_payroll_batch,
    admin_finalize_payroll_item_dtr,
    admin_payroll_process_batch,
    attendance_delete,
    attendance_update,
)


class Command(BaseCommand):
    help = (
        "Verify DTR finalization, payroll finalization, "
        "attendance locking, and payroll immutability."
    )

    PERIOD_NAME = (
        "Payroll Accuracy Verification - October 1-15 2026"
    )

    EMPLOYEES = {
        PayrollBatch.SCOPE_JO: {
            "username": "miguel.andres.navarro",
            "name": "Miguel Andres Navarro",
            "net": Decimal("7463.84"),
        },

        PayrollBatch.SCOPE_COS: {
            "username": "camille.rose.mendoza",
            "name": "Camille Rose Mendoza",
            "net": Decimal("8363.84"),
        },

        PayrollBatch.SCOPE_PERMANENT: {
            "username": "adrian.luis.villanueva",
            "name": "Adrian Luis Villanueva",
            "net": Decimal("13761.30"),
        },
    }

    def handle(self, *args, **options):
        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "INTELLIHRTRACK PAYROLL ACCURACY — STEP H"
            )
        )
        self.stdout.write("=" * 72)

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
                "Step F/G verification period not found."
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
                "No active staff/superuser account found."
            )

        self.stdout.write(
            f"Using admin account: {admin_user.username}"
        )

        factory = RequestFactory()
        failures = []

        for scope, config in self.EMPLOYEES.items():
            self.stdout.write("")
            self.stdout.write(
                self.style.MIGRATE_LABEL(
                    f"{scope} — {config['name']}"
                )
            )

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
                    f"{config['name']} not found."
                )

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
                raise CommandError(
                    f"{scope} Step G batch not found."
                )

            item = (
                PayrollItem.objects
                .filter(
                    batch=batch,
                    profile=profile,
                )
                .first()
            )

            if not item:
                raise CommandError(
                    f"{scope} PayrollItem not found."
                )

            original_item_id = item.id
            original_net = money(item.net_pay)
            original_deductions = money(
                item.deductions_total
            )

            original_dtr_rows = json.dumps(
                (
                    item.meta
                    if isinstance(item.meta, dict)
                    else {}
                ).get(
                    "dtr_rows",
                    [],
                ),
                sort_keys=True,
                default=str,
            )

            # =============================================
            # 1. FINALIZE THE REAL SAVED DTR
            # =============================================
            finalized_dtr = (
                FinalizedDTR.objects
                .filter(
                    profile=profile,
                    period=period,
                    is_locked=True,
                )
                .first()
            )

            if not finalized_dtr:
                request = factory.post(
                    f"/admin-ui/payroll/item/"
                    f"{item.id}/dtr/finalize/"
                )

                self.prepare_request(
                    request,
                    admin_user,
                )

                response = (
                    admin_finalize_payroll_item_dtr(
                        request,
                        item.id,
                    )
                )

                if response.status_code not in {
                    301,
                    302,
                }:
                    failures.append(
                        f"{scope}: DTR finalization "
                        f"HTTP {response.status_code}"
                    )

            finalized_dtr = (
                FinalizedDTR.objects
                .filter(
                    profile=profile,
                    period=period,
                )
                .first()
            )

            if (
                finalized_dtr
                and finalized_dtr.is_locked
            ):
                self.pass_line(
                    "DTR finalized and locked"
                )
            else:
                failures.append(
                    f"{scope}: DTR was not locked"
                )
                self.fail_line(
                    "DTR finalized and locked"
                )
                continue

            if finalized_dtr.payroll_item_id == item.id:
                self.pass_line(
                    "Locked DTR points to saved PayrollItem"
                )
            else:
                failures.append(
                    f"{scope}: finalized DTR PayrollItem mismatch"
                )

            locked_rows = json.dumps(
                finalized_dtr.rows or [],
                sort_keys=True,
                default=str,
            )

            if locked_rows == original_dtr_rows:
                self.pass_line(
                    "Locked DTR rows equal processed snapshot"
                )
            else:
                failures.append(
                    f"{scope}: finalized DTR rows mismatch"
                )
                self.fail_line(
                    "Locked DTR rows equal processed snapshot"
                )

            # =============================================
            # 2. FINALIZATION VALIDATION
            # =============================================
            validation = (
                _build_payroll_batch_validation(
                    batch
                )
            )

            if validation["is_ready"]:
                self.pass_line(
                    "Payroll validation is READY"
                )
            else:
                failures.append(
                    f"{scope}: validation errors: "
                    f"{validation['errors']}"
                )

                self.fail_line(
                    "Payroll validation is READY"
                )

                continue

            # =============================================
            # 3. FINALIZE THE REAL PAYROLL BATCH
            # =============================================
            if (
                batch.status
                != PayrollBatch.STATUS_FINALIZED
            ):
                request = factory.post(
                    f"/admin-ui/payroll/batch/"
                    f"{batch.id}/finalize/"
                )

                self.prepare_request(
                    request,
                    admin_user,
                )

                response = admin_finalize_payroll_batch(
                    request,
                    batch.id,
                )

                if response.status_code not in {
                    301,
                    302,
                }:
                    failures.append(
                        f"{scope}: batch finalization "
                        f"HTTP {response.status_code}"
                    )

            batch.refresh_from_db()

            if (
                batch.status
                == PayrollBatch.STATUS_FINALIZED
            ):
                self.pass_line(
                    "Payroll batch finalized"
                )
            else:
                failures.append(
                    f"{scope}: payroll batch not finalized"
                )
                self.fail_line(
                    "Payroll batch finalized"
                )

            # =============================================
            # 4. RAW ATTENDANCE MUST NOW BE LOCKED
            # =============================================
            # Use the exact same timezone-safe datetime
            # range approach as the real DTR/payroll engine.
            attendance_start = datetime.combine(
                period.start_date,
                time.min,
            )

            attendance_end = datetime.combine(
                period.end_date + timedelta(days=1),
                time.min,
            )

            if settings.USE_TZ:
                attendance_start = timezone.make_aware(
                    attendance_start,
                    timezone.get_current_timezone(),
                )

                attendance_end = timezone.make_aware(
                    attendance_end,
                    timezone.get_current_timezone(),
                )

            attendance = (
                AttendanceRecord.objects
                .filter(
                    employee_id=
                        profile.biometric_employee_id,
                    branch=branch,
                    timestamp__gte=attendance_start,
                    timestamp__lt=attendance_end,
                )
                .order_by("timestamp")
                .first()
            )


            if not attendance:
                failures.append(
                    f"{scope}: no attendance record "
                    "available for lock test"
                )

            else:
                detected_lock = (
                    _locked_dtr_for_attendance_record(
                        attendance
                    )
                )

                if detected_lock:
                    self.pass_line(
                        "Raw attendance recognizes DTR lock"
                    )
                else:
                    failures.append(
                        f"{scope}: attendance lock "
                        "helper returned None"
                    )

                attendance_id = attendance.id
                original_timestamp = attendance.timestamp

                # Real edit page must refuse access.
                edit_request = factory.get(
                    f"/admin-ui/biometrics/records/"
                    f"{attendance.id}/edit/"
                )

                self.prepare_request(
                    edit_request,
                    admin_user,
                )

                edit_response = attendance_update(
                    edit_request,
                    attendance.id,
                )

                if edit_response.status_code in {
                    301,
                    302,
                }:
                    self.pass_line(
                        "Locked attendance edit blocked"
                    )
                else:
                    failures.append(
                        f"{scope}: attendance edit not blocked"
                    )

                # Real delete action must not delete it.
                                # -----------------------------------------
                # Test deletion inside a rollback block.
                # Even if the delete protection is broken,
                # the test must never permanently destroy
                # attendance evidence.
                # -----------------------------------------
                with transaction.atomic():
                    delete_request = factory.post(
                        f"/admin-ui/biometrics/records/"
                        f"{attendance.id}/delete/"
                    )

                    self.prepare_request(
                        delete_request,
                        admin_user,
                    )

                    delete_response = attendance_delete(
                        delete_request,
                        attendance.id,
                    )

                    still_exists_during_test = (
                        AttendanceRecord.objects
                        .filter(
                            id=attendance_id
                        )
                        .exists()
                    )

                    if (
                        delete_response.status_code
                        in {301, 302}
                        and still_exists_during_test
                    ):
                        self.pass_line(
                            "Locked attendance delete blocked"
                        )

                    else:
                        failures.append(
                            f"{scope}: locked attendance "
                            "could be deleted"
                        )

                        self.fail_line(
                            "Locked attendance delete blocked"
                        )

                    # Always roll back this destructive test.
                    transaction.set_rollback(True)

                # Verify the database record is still present
                # after the rollback-protected delete test.
                attendance_exists_after_test = (
                    AttendanceRecord.objects
                    .filter(
                        id=attendance_id
                    )
                    .exists()
                )

                if attendance_exists_after_test:
                    attendance.refresh_from_db()

                    if (
                        attendance.timestamp
                        == original_timestamp
                    ):
                        self.pass_line(
                            "Attendance source remained unchanged"
                        )
                    else:
                        failures.append(
                            f"{scope}: attendance timestamp changed"
                        )

                        self.fail_line(
                            "Attendance source remained unchanged"
                        )

                else:
                    failures.append(
                        f"{scope}: attendance record disappeared "
                        "after rollback-protected delete test"
                    )

                    self.fail_line(
                        "Attendance source remained unchanged"
                    )

            # =============================================
            # 5. ATTEMPT REAL PAYROLL REPROCESSING
            # =============================================
            process_request = factory.post(
                "/admin-ui/payroll/process/",
                {
                    "period": str(period.id),
                    "branch": str(branch.id),
                    "type": scope,
                    "search": config["username"],
                },
            )

            self.prepare_request(
                process_request,
                admin_user,
            )

            process_response = (
                admin_payroll_process_batch(
                    process_request
                )
            )

            if process_response.status_code == 400:
                try:
                    payload = json.loads(
                        process_response.content.decode()
                    )
                except Exception:
                    payload = {}

                if not payload.get("ok", True):
                    self.pass_line(
                        "Reprocessing finalized/locked payroll blocked"
                    )
                else:
                    failures.append(
                        f"{scope}: reprocessing returned "
                        "unexpected payload"
                    )
            else:
                failures.append(
                    f"{scope}: reprocessing was not blocked; "
                    f"HTTP {process_response.status_code}"
                )

                self.fail_line(
                    "Reprocessing finalized/locked payroll blocked"
                )

            # =============================================
            # 6. VERIFY OFFICIAL VALUES DID NOT CHANGE
            # =============================================
            batch.refresh_from_db()

            saved_item = (
                PayrollItem.objects
                .filter(
                    batch=batch,
                    profile=profile,
                )
                .first()
            )

            if not saved_item:
                failures.append(
                    f"{scope}: PayrollItem disappeared"
                )
                continue

            if saved_item.id == original_item_id:
                self.pass_line(
                    "Original PayrollItem preserved"
                )
            else:
                failures.append(
                    f"{scope}: PayrollItem was replaced"
                )

            if money(saved_item.net_pay) == original_net:
                self.pass_line(
                    f"Official net preserved: {original_net}"
                )
            else:
                failures.append(
                    f"{scope}: net changed from "
                    f"{original_net} to {saved_item.net_pay}"
                )

            if (
                money(saved_item.deductions_total)
                == original_deductions
            ):
                self.pass_line(
                    "Official deductions preserved"
                )
            else:
                failures.append(
                    f"{scope}: deductions changed"
                )

            finalized_dtr.refresh_from_db()

            after_rows = json.dumps(
                finalized_dtr.rows or [],
                sort_keys=True,
                default=str,
            )

            if after_rows == locked_rows:
                self.pass_line(
                    "Finalized DTR snapshot preserved"
                )
            else:
                failures.append(
                    f"{scope}: locked DTR snapshot changed"
                )

        self.stdout.write("")
        self.stdout.write("=" * 72)

        if failures:
            self.stdout.write(
                self.style.ERROR(
                    f"STEP H FAILED — "
                    f"{len(failures)} problem(s)."
                )
            )

            for failure in failures:
                self.stdout.write(
                    self.style.ERROR(
                        f"  - {failure}"
                    )
                )

            raise CommandError(
                "Finalization/immutability verification failed."
            )

        self.stdout.write(
            self.style.SUCCESS(
                "STEP H PASSED"
            )
        )

        self.stdout.write(
            self.style.SUCCESS(
                "DTRs and payroll batches are finalized, "
                "raw attendance changes are blocked, "
                "reprocessing is blocked, and official "
                "payroll values remain unchanged."
            )
        )

    def prepare_request(
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

    def pass_line(self, text):
        self.stdout.write(
            self.style.SUCCESS(
                f"PASS  {text}"
            )
        )

    def fail_line(self, text):
        self.stdout.write(
            self.style.ERROR(
                f"FAIL  {text}"
            )
        )