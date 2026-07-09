# core/models.py

from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
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

    employee_id = models.CharField(max_length=64, db_index=True)
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


# =========================================================
# User Profile (Branch + Approval + Payroll fields)
# =========================================================
# =========================================================
# User Profile (Branch + Approval + Payroll fields)
# =========================================================
class UserProfile(models.Model):
    EMP_COS = "COS"
    EMP_JO = "JO"
    EMP_PERMANENT = "PERMANENT"

    EMPLOYMENT_TYPE_CHOICES = [
        (EMP_COS, "Contract of Service (COS)"),
        (EMP_JO, "Job Order (JO)"),
        (EMP_PERMANENT, "Permanent"),
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

    employment_type = models.CharField(
        max_length=10,
        choices=EMPLOYMENT_TYPE_CHOICES,
        default=EMP_COS,
        db_index=True,
    )

    department = models.CharField(max_length=255, blank=True, default="")
    position = models.CharField(max_length=255, blank=True, default="")

    biometric_employee_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Employee ID used by the Hikvision biometric device.",
    )

    # Base Compensation
    # JO: usually daily_rate
    # COS: usually monthly_salary or fixed cut-off salary
    # PERMANENT: monthly_salary is treated as basic salary for now
    daily_rate = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    monthly_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Permanent employee earnings
    # PERA is commonly treated as a standard allowance.
    # Exact handling per cut-off must still be confirmed by the client.
    pera_allowance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="PERA or standard allowance for permanent employees.",
    )

    other_earnings_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Other regular earnings/allowances if approved by client policy.",
    )

    # UI SUPPORT: manual per employee deduction
    manual_deduction_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )

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

    @property
    def is_job_order(self):
        return self.employment_type == self.EMP_JO

    @property
    def is_cos(self):
        return self.employment_type == self.EMP_COS

    @property
    def is_permanent(self):
        return self.employment_type == self.EMP_PERMANENT

    @property
    def basic_salary(self):
        """
        For permanent employees, monthly_salary is treated as basic salary.
        This avoids adding a separate basic_salary field too early.
        """
        return self.monthly_salary or Decimal("0.00")


# =========================================================
# Leave (UNCHANGED)
# =========================================================
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

    employee = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="leave_requests"
    )
    branch = models.ForeignKey(
        "Branch", on_delete=models.PROTECT, related_name="leave_requests"
    )

    leave_type = models.CharField(max_length=20, choices=LEAVE_TYPE_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    duration = models.CharField(
        max_length=20, choices=DURATION_CHOICES, default=DURATION_FULL
    )

    reason = models.TextField()
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )

    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="leave_reviews"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    admin_note = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.employee.username} {self.leave_type} {self.start_date} - {self.end_date} ({self.status})"


class LeaveAttachment(models.Model):
    leave_request = models.ForeignKey(
        LeaveRequest, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(upload_to="leave_attachments/")
    uploaded_at = models.DateTimeField(auto_now_add=True)


# =========================================================
# Payroll Models
# =========================================================
class PayrollPeriod(models.Model):
    PAY_MONTHLY = "MONTHLY"
    PAY_FIRST_HALF = "FIRST_HALF"
    PAY_SECOND_HALF = "SECOND_HALF"

    PAY_MODE_CHOICES = [
        (PAY_MONTHLY, "Monthly"),
        (PAY_FIRST_HALF, "Semi-monthly 1st Half"),
        (PAY_SECOND_HALF, "Semi-monthly 2nd Half"),
    ]

    name = models.CharField(max_length=120)
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    pay_mode = models.CharField(
        max_length=20,
        choices=PAY_MODE_CHOICES,
        default=PAY_MONTHLY,
    )

    class Meta:
        ordering = ["-start_date"]
        db_table = "core_payrollperiod"

    def __str__(self):
        return f"{self.name} ({self.start_date} - {self.end_date})"

class PayrollRule(models.Model):
    branch = models.OneToOneField(
        Branch,
        on_delete=models.CASCADE,
        related_name="payroll_rules",
    )

    tax_rate_percent = models.DecimalField(max_digits=6, decimal_places=2, default=5)
    premium_rate_percent = models.DecimalField(max_digits=6, decimal_places=2, default=20)

    PHILHEALTH_PERCENT = "percent"
    PHILHEALTH_FIXED = "fixed"

    philhealth_default_mode = models.CharField(
        max_length=10,
        default=PHILHEALTH_PERCENT,
    )
    philhealth_default_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=5,
    )

    salary_divisor = models.DecimalField(max_digits=6, decimal_places=2, default=22)

    sss_minimum = models.DecimalField(max_digits=10, decimal_places=2, default=760)
    pagibig_minimum = models.DecimalField(max_digits=10, decimal_places=2, default=400)

    ot_multiplier = models.DecimalField(max_digits=5, decimal_places=2, default=1.25)

    grace_minutes_normal = models.PositiveIntegerField(default=15)
    flag_ceremony_cutoff_time = models.TimeField(default="08:00")

    lunch_break_required = models.BooleanField(default=True)
    daily_hours_required = models.DecimalField(max_digits=5, decimal_places=2, default=8)

    work_start_time = models.TimeField(default="08:00")
    work_end_time = models.TimeField(default="17:00")

    class Meta:
        db_table = "core_payrollrule"

    def __str__(self):
        return f"PayrollRule({self.branch.name})"


class EmployeeContribution(models.Model):
    profile = models.OneToOneField(
        UserProfile,
        on_delete=models.CASCADE,
        related_name="contrib",
    )

    # JO/COS common contribution fields
    sss_amount = models.DecimalField(max_digits=10, decimal_places=2, default=760)
    pagibig_amount = models.DecimalField(max_digits=10, decimal_places=2, default=400)

    PHILHEALTH_PERCENT = "percent"
    PHILHEALTH_FIXED = "fixed"

    PHILHEALTH_MODE_CHOICES = [
        (PHILHEALTH_PERCENT, "Percentage"),
        (PHILHEALTH_FIXED, "Fixed Amount"),
    ]

    philhealth_mode = models.CharField(
        max_length=10,
        choices=PHILHEALTH_MODE_CHOICES,
        default=PHILHEALTH_PERCENT,
    )

    philhealth_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=5,
    )

    # Permanent employee deductions
    # These are manual/configurable first because the uploaded Word file
    # does not provide exact official formulas.
    wtax_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Withholding tax amount for permanent employees.",
    )

    gsis_employee_share = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="GSIS employee share deducted from permanent employee pay.",
    )

    gsis_employer_share = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="GSIS employer share for records/costing only. Not deducted from employee pay.",
    )

    loan_deduction_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total loan deductions such as calamity loan or multi-purpose loan.",
    )

    other_deduction_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Other authorized deductions.",
    )

    other_employer_contribution = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Other employer contribution for records/costing only. Not deducted from employee pay.",
    )

    class Meta:
        db_table = "core_employeecontribution"

    def __str__(self):
        return f"Contrib({self.profile.user.username})"


class HolidaySuspension(models.Model):
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

    branch = models.ForeignKey(
        Branch, null=True, blank=True, on_delete=models.CASCADE, related_name="holidays"
    )

    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date"]
        db_table = "core_holidaysuspension"
        indexes = [models.Index(fields=["date", "scope"])]

    def __str__(self):
        return f"{self.date} {self.name} ({self.type}/{self.scope})"


class PayrollBatch(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_COMPLETED = "completed"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_COMPLETED, "Completed"),
    ]

    SCOPE_ALL = "ALL"
    SCOPE_JO = "JO"
    SCOPE_COS = "COS"
    SCOPE_PERMANENT = "PERMANENT"

    EMPLOYEE_TYPE_SCOPE_CHOICES = [
        (SCOPE_ALL, "All Employee Types"),
        (SCOPE_JO, "Job Order Only"),
        (SCOPE_COS, "Contract of Service Only"),
        (SCOPE_PERMANENT, "Permanent Only"),
    ]

    name = models.CharField(max_length=200)

    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="payroll_batches",
    )

    period = models.ForeignKey(
        PayrollPeriod,
        on_delete=models.PROTECT,
        related_name="payroll_batches",
    )

    employee_type_scope = models.CharField(
        max_length=20,
        choices=EMPLOYEE_TYPE_SCOPE_CHOICES,
        default=SCOPE_ALL,
        db_index=True,
        help_text="Scope of employees included in this payroll batch.",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
    )

    processed_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="processed_payroll_batches",
    )
    

    processed_at = models.DateTimeField(null=True, blank=True)

    totals_net = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    totals_deductions = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        db_table = "core_payrollbatch"
        unique_together = [("branch", "period", "employee_type_scope")]

    def __str__(self):
        return f"{self.name} ({self.branch.name}) - {self.employee_type_scope}"

class PayrollItem(models.Model):
    batch = models.ForeignKey(PayrollBatch, on_delete=models.CASCADE, related_name="items")
    profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, related_name="payroll_items")

    base_pay = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    premium_pay = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    overtime_hours = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    overtime_pay = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    late_minutes = models.PositiveIntegerField(default=0)
    undertime_minutes = models.PositiveIntegerField(default=0)
    absences = models.PositiveIntegerField(default=0)

    manual_deduction = models.DecimalField(max_digits=14, decimal_places=2, default=0)

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
class FinalizedDTR(models.Model):
    """
    Stores a locked/finalized DTR snapshot for one employee and one payroll period.

    Purpose:
    - Preserve the DTR used for payroll
    - Prevent accidental payroll/DTR overwrite
    - Keep audit trail for finalization and unlocking
    """

    profile = models.ForeignKey(
        UserProfile,
        on_delete=models.PROTECT,
        related_name="finalized_dtrs",
    )

    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="finalized_dtrs",
    )

    period = models.ForeignKey(
        PayrollPeriod,
        on_delete=models.PROTECT,
        related_name="finalized_dtrs",
    )

    payroll_item = models.ForeignKey(
        PayrollItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finalized_dtrs",
        help_text="PayrollItem used as source when this DTR was finalized.",
    )

    rows = models.JSONField(
        default=list,
        blank=True,
        help_text="Locked DTR rows snapshot.",
    )

    summary = models.JSONField(
        default=dict,
        blank=True,
        help_text="Locked DTR summary snapshot.",
    )

    source_meta = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extra source information such as batch ID, processor, and scope.",
    )

    is_locked = models.BooleanField(default=True, db_index=True)

    finalized_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finalized_dtrs",
    )

    finalized_at = models.DateTimeField(null=True, blank=True)

    unlocked_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="unlocked_dtrs",
    )

    unlocked_at = models.DateTimeField(null=True, blank=True)

    unlock_reason = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_finalizeddtr"
        ordering = ["-finalized_at", "-created_at"]
        unique_together = [("profile", "period")]
        indexes = [
            models.Index(fields=["branch", "period"]),
            models.Index(fields=["profile", "period", "is_locked"]),
        ]

    def __str__(self):
        status = "LOCKED" if self.is_locked else "UNLOCKED"
        return f"{self.profile.user.username} - {self.period.name} ({status})"

        
class BiometricDevice(models.Model):
    name = models.CharField(max_length=100)
    ip_address = models.GenericIPAddressField()
    port = models.IntegerField(default=80)
    username = models.CharField(max_length=100)
    password = models.CharField(max_length=100)
    branch = models.ForeignKey("Branch", on_delete=models.CASCADE)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.ip_address})"


#employees on travel feature
class TravelOrder(models.Model):
    employee = models.ForeignKey(UserProfile, on_delete=models.CASCADE)
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_travelorder"

    def __str__(self):
        return f"{self.employee.user.username} ({self.start_date} - {self.end_date})"

    #
class OvertimeRequest(models.Model):
    profile = models.ForeignKey(
        UserProfile,
        on_delete=models.CASCADE,
        related_name="overtime_requests",
    )
    date = models.DateField(db_index=True)
    hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    approved = models.BooleanField(default=False)
    approved_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_overtime_requests",
    )

    reason = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_overtimerequest"
        ordering = ["-date", "-created_at"]
        unique_together = [("profile", "date")]

    def __str__(self):
        return f"{self.profile.user.username} OT {self.date} ({self.hours} hrs)"
    