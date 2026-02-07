
from django.conf import settings
from django.db import models
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

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

    # Required fields you want to store
    employee_id = models.CharField(max_length=64, db_index=True)  # Person ID
    full_name = models.CharField(max_length=255, blank=True)
    department = models.CharField(max_length=255, blank=True)

    # ✅ FK to Branch (creates branch_id)
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
        b = self.branch.name if self.branch else "—"
        return f"{self.employee_id} @ {self.timestamp} ({self.attendance_status}) [{b}]"


# =========================
# User Profile (Branch + Approval)
# =========================
class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )

    # ✅ FK to Branch (creates branch_id)
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="profiles",
        null=True,
        blank=True,
        db_index=True,
    )

    is_approved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_userprofile"
        indexes = [
            models.Index(fields=["branch", "is_approved"]),
        ]

    def __str__(self):
        status = "APPROVED" if self.is_approved else "PENDING"
        b = self.branch.name if self.branch else "—"
        return f"{self.user.username} ({b}) - {status}"


class LeaveRequest(models.Model):
    # Keep values stable (for filtering + UI)
    STATUS_DRAFT = "DRAFT"
    STATUS_PENDING = "PENDING"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    DURATION_FULL = "FULL"
    DURATION_HALF_AM = "HALF_AM"
    DURATION_HALF_PM = "HALF_PM"
    DURATION_CHOICES = [
        (DURATION_FULL, "Full Day"),
        (DURATION_HALF_AM, "Half Day (AM)"),
        (DURATION_HALF_PM, "Half Day (PM)"),
    ]

    TYPE_VACATION = "VACATION"
    TYPE_SICK = "SICK"
    TYPE_EMERGENCY = "EMERGENCY"
    TYPE_OFFICIAL = "OFFICIAL"
    TYPE_MATERNITY = "MATERNITY"
    TYPE_PATERNITY = "PATERNITY"

    LEAVE_TYPE_CHOICES = [
        (TYPE_VACATION, "Vacation Leave"),
        (TYPE_SICK, "Sick Leave"),
        (TYPE_EMERGENCY, "Emergency Leave"),
        (TYPE_OFFICIAL, "Official Business"),
        (TYPE_MATERNITY, "Maternity Leave"),
        (TYPE_PATERNITY, "Paternity Leave"),
    ]

    employee = models.ForeignKey(User, on_delete=models.CASCADE, related_name="leave_requests")
    branch = models.ForeignKey("Branch", on_delete=models.PROTECT, related_name="leave_requests")

    leave_type = models.CharField(max_length=20, choices=LEAVE_TYPE_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    duration = models.CharField(max_length=20, choices=DURATION_CHOICES, default=DURATION_FULL)

    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)

    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="leave_reviews")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    admin_note = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.employee.username} {self.leave_type} {self.start_date} - {self.end_date} ({self.status})"


class LeaveAttachment(models.Model):
    leave_request = models.ForeignKey(LeaveRequest, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="leave_attachments/")
    uploaded_at = models.DateTimeField(auto_now_add=True)
