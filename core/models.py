from django.conf import settings
from django.db import models


class Branch(models.Model):
    name = models.CharField(max_length=100, unique=True, db_index=True)

    class Meta:
        db_table = "core_branch"
        ordering = ["name"]

    def __str__(self):
        return self.name


class AttendanceRecord(models.Model):
    STATUS_CHECKIN = "CHECK_IN"
    STATUS_CHECKOUT = "CHECK_OUT"
    STATUS_UNKNOWN = "UNKNOWN"

    ATTENDANCE_STATUS_CHOICES = [
        (STATUS_CHECKIN, "Check-in"),
        (STATUS_CHECKOUT, "Check-out"),
        (STATUS_UNKNOWN, "Unknown"),
    ]

    employee_id = models.CharField(max_length=64, db_index=True)  # Person ID
    full_name = models.CharField(max_length=255, blank=True)
    department = models.CharField(max_length=255, blank=True)

    # FK to Branch
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="attendance_records",
        null=True,
        blank=True,
        db_index=True,
    )

    timestamp = models.DateTimeField(db_index=True)  # Time
    attendance_status = models.CharField(
        max_length=16,
        choices=ATTENDANCE_STATUS_CHOICES,
        default=STATUS_UNKNOWN,
        db_index=True,
    )

    # store raw imported row (optional but helpful)
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
        b = self.branch.name if self.branch else "—"
        return f"{self.employee_id} @ {self.timestamp} ({self.attendance_status}) [{b}]"


# =========================
# User Profile
# =========================
class UserProfile(models.Model):
    EMPLOYMENT_COS = "COS"
    EMPLOYMENT_JO = "JO"

    EMPLOYMENT_TYPE_CHOICES = [
        (EMPLOYMENT_COS, "Contract of Service"),
        (EMPLOYMENT_JO, "Job Order"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )

    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="profiles",
        null=True,
        blank=True,
        db_index=True,
    )

    # NEW FIELDS
    department = models.CharField(max_length=120, blank=True, db_index=True)

    # IMPORTANT:
    # if you want this required during signup, keep blank=False (default).
    # if you want it optional, add blank=True.
    employment_type = models.CharField(
        max_length=3,
        choices=EMPLOYMENT_TYPE_CHOICES,
        db_index=True,
    )

    is_approved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_userprofile"
        indexes = [
            models.Index(fields=["branch", "is_approved"]),
            models.Index(fields=["department"]),
            models.Index(fields=["employment_type"]),
        ]

    def __str__(self):
        status = "APPROVED" if self.is_approved else "PENDING"
        b = self.branch.name if self.branch else "—"
        dept = self.department or "—"
        return f"{self.user.username} | {b} | {dept} | {self.employment_type} | {status}"
