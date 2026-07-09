from django.contrib import admin
from .models import (
    Branch,
    UserProfile,
    AttendanceRecord,
    PayrollPeriod,
    PayrollRule,
    EmployeeContribution,
    HolidaySuspension,
    PayrollBatch,
    PayrollItem,
    FinalizedDTR,
    BiometricDevice,
    TravelOrder,
    OvertimeRequest,
)


@admin.register(FinalizedDTR)
class FinalizedDTRAdmin(admin.ModelAdmin):
    list_display = (
        "profile",
        "branch",
        "period",
        "is_locked",
        "finalized_by",
        "finalized_at",
        "unlocked_by",
        "unlocked_at",
    )
    list_filter = ("is_locked", "branch", "period")
    search_fields = (
        "profile__user__username",
        "profile__user__first_name",
        "profile__user__last_name",
        "profile__biometric_employee_id",
        "unlock_reason",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "finalized_at",
        "unlocked_at",
    )