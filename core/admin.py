from django.contrib import admin

from .models import (
    AttendanceRecord,
    Branch,
    EmployeeContribution,
    FinalizedDTR,
    PayrollBatch,
    PayrollItem,
    PayrollPeriod,
    PayrollRule,
    UserProfile,
)


class ReadOnlyAuditAdmin(admin.ModelAdmin):
    """
    Base admin for generated payroll and audit records.

    These records can be inspected through Django admin, but new records
    cannot be manually added or deleted here.

    Editing payroll snapshots directly in Django admin is intentionally
    prevented to protect the audit trail.
    """

    def get_readonly_fields(self, request, obj=None):
        return tuple(
            field.name
            for field in self.model._meta.fields
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# =========================================================
# Branch
# =========================================================
@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
    )

    search_fields = (
        "name",
    )

    ordering = (
        "name",
    )


# =========================================================
# Employee / Payroll Profile
# =========================================================
@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "branch",
        "employment_type",
        "biometric_employee_id",
        "department",
        "position",
        "monthly_salary",
        "daily_rate",
        "pera_allowance",
        "is_approved",
    )

    list_filter = (
        "employment_type",
        "is_approved",
        "branch",
        "has_premium",
    )

    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
        "user__email",
        "biometric_employee_id",
        "department",
        "position",
    )

    list_select_related = (
        "user",
        "branch",
    )

    ordering = (
        "user__username",
    )

    readonly_fields = (
        "created_at",
    )


# =========================================================
# Raw Attendance Records
# =========================================================
@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(ReadOnlyAuditAdmin):
    list_display = (
        "id",
        "employee_id",
        "full_name",
        "department",
        "branch",
        "timestamp",
        "attendance_status",
    )

    list_filter = (
        "attendance_status",
        "branch",
    )

    search_fields = (
        "employee_id",
        "full_name",
        "department",
    )

    ordering = (
        "-timestamp",
    )

    #date_hierarchy = "timestamp"


# =========================================================
# Payroll Period
# =========================================================
@admin.register(PayrollPeriod)
class PayrollPeriodAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "start_date",
        "end_date",
        "pay_mode",
    )

    list_filter = (
        "pay_mode",
    )

    search_fields = (
        "name",
    )

    ordering = (
        "-start_date",
        "-end_date",
    )

    date_hierarchy = "start_date"


# =========================================================
# Configurable Payroll Rules
# =========================================================
@admin.register(PayrollRule)
class PayrollRuleAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "branch",
        "salary_divisor",
        "daily_hours_required",
        "premium_rate_percent",
        "tax_rate_percent",
        "philhealth_default_value",
        "sss_minimum",
        "pagibig_minimum",
        "ot_multiplier",
    )

    list_filter = (
        "branch",
    )

    search_fields = (
        "branch__name",
    )


# =========================================================
# Employee Contributions and Deductions
# =========================================================
@admin.register(EmployeeContribution)
class EmployeeContributionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "profile",
        "sss_amount",
        "pagibig_amount",
        "philhealth_mode",
        "philhealth_value",
        "wtax_amount",
        "gsis_employee_share",
        "gsis_employer_share",
        "loan_deduction_amount",
        "other_deduction_amount",
        "other_employer_contribution",
    )

    list_filter = (
        "philhealth_mode",
        "profile__employment_type",
        "profile__branch",
    )

    search_fields = (
        "profile__user__username",
        "profile__user__first_name",
        "profile__user__last_name",
        "profile__biometric_employee_id",
    )

    list_select_related = (
        "profile",
        "profile__user",
        "profile__branch",
    )


# =========================================================
# Payroll Batch Audit
# =========================================================
# =========================================================
# Payroll Batch Audit
# =========================================================
@admin.register(PayrollBatch)
class PayrollBatchAdmin(ReadOnlyAuditAdmin):
    list_display = (
        "id",
        "name",
        "branch",
        "period",
        "employee_type_scope",
        "status",
        "processed_by",
        "processed_at",
        "finalized_by",
        "finalized_at",
        "reopened_by",
        "reopened_at",
        "short_reopen_reason",
        "totals_net",
        "totals_deductions",
    )

    list_filter = (
        "status",
        "employee_type_scope",
        "branch",
        "period",
    )

    search_fields = (
        "name",
        "branch__name",
        "period__name",
        "processed_by__username",
        "finalized_by__username",
        "reopened_by__username",
        "reopen_reason",
    )

    list_select_related = (
        "branch",
        "period",
        "processed_by",
        "finalized_by",
        "reopened_by",
    )

    ordering = (
        "-processed_at",
        "-created_at",
    )

    @admin.display(description="Reopen Reason")
    def short_reopen_reason(self, obj):
        text = str(obj.reopen_reason or "").strip()

        if not text:
            return "—"

        if len(text) <= 60:
            return text

        return f"{text[:60]}..."


# =========================================================
# Payroll Item Audit
# =========================================================
@admin.register(PayrollItem)
class PayrollItemAdmin(ReadOnlyAuditAdmin):
    list_display = (
        "id",
        "employee_name",
        "employee_type",
        "batch",
        "base_pay",
        "gov_contributions_total",
        "tax_total",
        "deductions_total",
        "net_pay",
        "late_minutes",
        "undertime_minutes",
        "absences",
        "short_issues",
    )

    search_fields = (
        "profile__user__username",
        "profile__user__first_name",
        "profile__user__last_name",
        "profile__biometric_employee_id",
        "batch__name",
        "issues",
    )

    list_select_related = (
        "batch",
        "batch__branch",
        "batch__period",
        "profile",
        "profile__user",
    )

    ordering = (
        "-id",
    )

    @admin.display(description="Employee")
    def employee_name(self, obj):
        return (
            obj.profile.user.get_full_name()
            or obj.profile.user.username
        )

    @admin.display(description="Employee Type")
    def employee_type(self, obj):
        return obj.profile.employment_type

    @admin.display(description="Issues")
    def short_issues(self, obj):
        text = str(obj.issues or "").strip()

        if not text:
            return "—"

        if len(text) <= 60:
            return text

        return f"{text[:60]}..."


# =========================================================
# Finalized DTR Audit
# =========================================================
@admin.register(FinalizedDTR)
class FinalizedDTRAdmin(ReadOnlyAuditAdmin):
    list_display = (
        "id",
        "employee_name",
        "branch",
        "period",
        "is_locked",
        "payroll_item",
        "finalized_by",
        "finalized_at",
        "unlocked_by",
        "unlocked_at",
        "short_unlock_reason",
    )

    list_filter = (
        "is_locked",
        "branch",
        "period",
    )

    search_fields = (
        "profile__user__username",
        "profile__user__first_name",
        "profile__user__last_name",
        "profile__biometric_employee_id",
        "unlock_reason",
    )

    list_select_related = (
        "profile",
        "profile__user",
        "branch",
        "period",
        "payroll_item",
        "finalized_by",
        "unlocked_by",
    )

    ordering = (
        "-finalized_at",
        "-created_at",
    )

    date_hierarchy = "finalized_at"

    @admin.display(description="Employee")
    def employee_name(self, obj):
        return (
            obj.profile.user.get_full_name()
            or obj.profile.user.username
        )

    @admin.display(description="Unlock Reason")
    def short_unlock_reason(self, obj):
        text = str(obj.unlock_reason or "").strip()

        if not text:
            return "—"

        if len(text) <= 60:
            return text

        return f"{text[:60]}..."