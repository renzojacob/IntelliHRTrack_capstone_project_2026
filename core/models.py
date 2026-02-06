from django.conf import settings
from django.db import models


class AttendanceRecord(models.Model):
    STATUS_CHECKIN = "CHECK_IN"
    STATUS_CHECKOUT = "CHECK_OUT"
    STATUS_UNKNOWN = "UNKNOWN"

    ATTENDANCE_STATUS_CHOICES = [
        (STATUS_CHECKIN, "Check-in"),
        (STATUS_CHECKOUT, "Check-out"),
        (STATUS_UNKNOWN, "Unknown"),
    ]

    # Required fields you want to store
    employee_id = models.CharField(max_length=64, db_index=True)  # Person ID
    full_name = models.CharField(max_length=255, blank=True)
    department = models.CharField(max_length=255, blank=True)
    branch = models.CharField(max_length=100, blank=True, db_index=True)

    timestamp = models.DateTimeField(db_index=True)  # Time
    attendance_status = models.CharField(
        max_length=16,
        choices=ATTENDANCE_STATUS_CHOICES,
        default=STATUS_UNKNOWN,
        db_index=True,
    )

    # (Optional) keep raw row for debugging; remove if you truly don't want it
    raw_row = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        db_table = "core_attendancerecord"
        indexes = [
            models.Index(fields=["employee_id", "timestamp"]),
            models.Index(fields=["branch", "timestamp"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee_id", "timestamp", "attendance_status", "branch"],
                name="unique_emp_ts_status_branch",
            )
        ]

    def __str__(self):
        return f"{self.employee_id} @ {self.timestamp} ({self.attendance_status}) [{self.branch}]"


# =========================
# NEW: User Profile (Branch + Approval)
# =========================
class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    branch = models.CharField(max_length=100, db_index=True)
    is_approved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_userprofile"
        indexes = [
            models.Index(fields=["branch", "is_approved"]),
        ]

    def __str__(self):
        status = "APPROVED" if self.is_approved else "PENDING"
        return f"{self.user.username} ({self.branch}) - {status}"
