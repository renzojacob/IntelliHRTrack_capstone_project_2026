# core/models.py

from django.conf import settings
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

    employee_id = models.CharField(max_length=64, db_index=True)  # Person ID
    full_name = models.CharField(max_length=255, blank=True)
    department = models.CharField(max_length=255, blank=True)

    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="attendance_records",
        null=True,
        blank=True,
        db_index=True,
    )

    timestamp = models.DateTimeField(db_index=True)
    attendance_status = models.CharField(
        max_length=16,
        choices=ATTENDANCE_STATUS_CHOICES,
        default=STATUS_UNKNOWN,
        db_index=True,
    )

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
# User Profile (Branch + Approval + Payroll fields)
# =========================
class UserProfile(models.Model):
    EMP_COS = "COS"
    EMP_JO = "JO"
    EMPLOYMENT_TYPE_CHOICES = [
        (EMP_COS, "Contract of Service (COS)"),
        (EMP_JO, "Job Order (JO)"),
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

    # ✅ used by your UI
    employment_type = models.CharField(
        max_length=10, choices=EMPLOYMENT_TYPE_CHOICES, default=EMP_COS, db_index=True
    )
    department = models.CharField(max_length=255, blank=True, default="")
    position = models.CharField(max_length=255, blank=True, default="")

    # ✅ JO / COS base compensation
    daily_rate = models.DecimalField(max_digits=12, decimal_places=2, default=0)   # JO
    monthly_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # COS

    # premium flag (20% default handled by PayrollRule)
    has_premium = models.BooleanField(default=False)

    is_approved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_userprofile"
        indexes = [
            models.Index(fields=["branch", "is_approved"]),
            models.Index(fields=["branch", "employment_type"]),
        ]

    def __str__(self):
        status = "APPROVED" if self.is_approved else "PENDING"
        b = self.branch.name if self.branch else "—"
        return f"{self.user.username} ({b}) - {status}"


# =========================
# Leave
# =========================
class LeaveRequest(models.Model):
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


# =========================
# Payroll models
# =========================
class PayrollPeriod(models.Model):
    """
    Used by your filter: payroll_periods
    """
    PAY_MONTHLY = "MONTHLY"
    PAY_FIRST_HALF = "FIRST_HALF"
    PAY_SECOND_HALF = "SECOND_HALF"
    PAY_MODE_CHOICES = [
        (PAY_MONTHLY, "Monthly"),
        (PAY_FIRST_HALF, "Kinsenas (1st Half)"),
        (PAY_SECOND_HALF, "Katapusan (2nd Half)"),
    ]

    name = models.CharField(max_length=120)
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    pay_mode = models.CharField(max_length=20, choices=PAY_MODE_CHOICES, default=PAY_MONTHLY)

    class Meta:
        ordering = ["-start_date"]
        db_table = "core_payrollperiod"

    def __str__(self):
        return f"{self.name} ({self.start_date} - {self.end_date})"


class PayrollRule(models.Model):
    """
    Matches variables used by Payroll Setup form:
    payroll_rules.tax_rate_percent, premium_rate_percent, etc.
    """
    branch = models.OneToOneField(Branch, on_delete=models.CASCADE, related_name="payroll_rules")

    tax_rate_percent = models.DecimalField(max_digits=6, decimal_places=2, default=5)
    premium_rate_percent = models.DecimalField(max_digits=6, decimal_places=2, default=20)

    PHILHEALTH_PERCENT = "percent"
    PHILHEALTH_FIXED = "fixed"
    philhealth_default_mode = models.CharField(max_length=10, default=PHILHEALTH_PERCENT)
    philhealth_default_value = models.DecimalField(max_digits=10, decimal_places=2, default=5)

    late_penalty_per_minute = models.DecimalField(max_digits=10, decimal_places=2, default=1.50)
    undertime_penalty_per_minute = models.DecimalField(max_digits=10, decimal_places=2, default=2.00)

    grace_minutes_normal = models.PositiveIntegerField(default=15)

    # “Flag ceremony cutoff” time (UI expects this)
    flag_ceremony_cutoff_time = models.TimeField(default="08:00")

    lunch_break_required = models.BooleanField(default=False)
    daily_hours_required = models.DecimalField(max_digits=5, decimal_places=2, default=8)

    # Needed for undertime calculation
    work_start_time = models.TimeField(default="08:00")
    work_end_time = models.TimeField(default="17:00")

    class Meta:
        db_table = "core_payrollrule"

    def __str__(self):
        return f"PayrollRule({self.branch.name})"


class EmployeeContribution(models.Model):
    """
    Employee-specific Gov contributions table in UI.
    """
    profile = models.OneToOneField(UserProfile, on_delete=models.CASCADE, related_name="contrib")

    sss_amount = models.DecimalField(max_digits=10, decimal_places=2, default=760)
    pagibig_amount = models.DecimalField(max_digits=10, decimal_places=2, default=400)

    PHILHEALTH_PERCENT = "percent"
    PHILHEALTH_FIXED = "fixed"
    philhealth_mode = models.CharField(max_length=10, default=PHILHEALTH_PERCENT)
    philhealth_value = models.DecimalField(max_digits=10, decimal_places=2, default=5)

    class Meta:
        db_table = "core_employeecontribution"

    def __str__(self):
        return f"Contrib({self.profile.user.username})"


class HolidaySuspension(models.Model):
    """
    For Holiday & Suspension Manager list.
    """
    TYPE_HOLIDAY = "holiday"
    TYPE_SUSPENSION = "suspension"
    TYPE_SPECIAL = "special"

    TYPE_CHOICES = [
        (TYPE_HOLIDAY, "Regular Holiday"),
        (TYPE_SUSPENSION, "Work Suspension"),
        (TYPE_SPECIAL, "Special Non-Working Day"),
    ]

    SCOPE_NATIONWIDE = "nationwide"
    SCOPE_REGION = "region"
    SCOPE_BRANCH = "branch"

    SCOPE_CHOICES = [
        (SCOPE_NATIONWIDE, "Nationwide"),
        (SCOPE_REGION, "MIMAROPA Region"),
        (SCOPE_BRANCH, "Specific Branch"),
    ]

    date = models.DateField(db_index=True)
    name = models.CharField(max_length=200)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_HOLIDAY)
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES, default=SCOPE_REGION)
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.CASCADE, related_name="holidays")

    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date"]
        db_table = "core_holidaysuspension"
        indexes = [
            models.Index(fields=["date", "scope"]),
        ]

    def __str__(self):
        return f"{self.date} {self.name} ({self.type}/{self.scope})"


class PayrollBatch(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_COMPLETED = "completed"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_COMPLETED, "Completed"),
    ]

    name = models.CharField(max_length=200)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="payroll_batches")
    period = models.ForeignKey(PayrollPeriod, on_delete=models.PROTECT, related_name="payroll_batches")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)

    processed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="processed_payroll_batches")
    processed_at = models.DateTimeField(null=True, blank=True)

    totals_net = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    totals_deductions = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        db_table = "core_payrollbatch"
        unique_together = [("branch", "period")]

    def __str__(self):
        return f"{self.name} ({self.branch.name})"


class PayrollItem(models.Model):
    batch = models.ForeignKey(PayrollBatch, on_delete=models.CASCADE, related_name="items")
    profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, related_name="payroll_items")

    base_pay = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    premium_pay = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    overtime_pay = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    late_minutes = models.PositiveIntegerField(default=0)
    undertime_minutes = models.PositiveIntegerField(default=0)
    absences = models.PositiveIntegerField(default=0)

    deductions_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gov_contributions_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    net_pay = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    issues = models.TextField(blank=True, default="")

    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "core_payrollitem"
        ordering = ["profile__user__username"]

    def __str__(self):
        return f"{self.profile.user.username} - {self.net_pay}"
