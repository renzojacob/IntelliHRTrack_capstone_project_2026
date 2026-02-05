from django.db import models


class AttendanceRecord(models.Model):
    EVENT_IN = "IN"
    EVENT_OUT = "OUT"
    EVENT_UNKNOWN = "UNKNOWN"

    EVENT_TYPE_CHOICES = [
        (EVENT_IN, "IN"),
        (EVENT_OUT, "OUT"),
        (EVENT_UNKNOWN, "UNKNOWN"),
    ]

    employee_id = models.CharField(max_length=64, db_index=True)
    full_name = models.CharField(max_length=255, blank=True)
    timestamp = models.DateTimeField(db_index=True)
    event_type = models.CharField(
        max_length=16, choices=EVENT_TYPE_CHOICES, default=EVENT_UNKNOWN
    )
    device_name = models.CharField(max_length=128, blank=True)
    verification_mode = models.CharField(max_length=64, blank=True)
    event_code = models.CharField(max_length=64, blank=True)
    raw_row = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        db_table = "core_attendancerecord"
        indexes = [
            models.Index(fields=["employee_id", "timestamp"]),
            models.Index(fields=["-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee_id", "timestamp", "event_type"],
                name="unique_emp_ts_event"
            )
        ]

    def __str__(self):
        return f"{self.employee_id} @ {self.timestamp} ({self.event_type})"
