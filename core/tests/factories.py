"""Small helpers that create real model rows for the DTR tests."""
import datetime as dt
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User

from core.models import AttendanceRecord, Branch, HolidaySuspension, PayrollPeriod, PayrollRule, UserProfile

MNL = ZoneInfo("Asia/Manila")


def branch(name="Calapan"):
    b, _ = Branch.objects.get_or_create(name=name)
    PayrollRule.objects.get_or_create(branch=b)
    return b


def employee(username, br, emp_type="COS", bio_id="22", monthly="20000.00"):
    u = User.objects.create_user(username, password="x")
    return UserProfile.objects.create(user=u, branch=br, employment_type=emp_type, biometric_employee_id=bio_id,
                                      is_approved=True, monthly_salary=monthly)


def admin(username, br=None, superuser=False):
    u = User.objects.create_user(username, password="x", is_staff=True, is_superuser=superuser)
    UserProfile.objects.create(user=u, branch=br, is_approved=True)
    return u


def punch(profile, day, hhmm, kind):
    """kind: 'in' or 'out'. Stored exactly like a Hikvision event with +08:00 time."""
    h, m = map(int, hhmm.split(":"))
    ts = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=MNL)
    status = AttendanceRecord.STATUS_CHECKIN if kind == "in" else AttendanceRecord.STATUS_CHECKOUT
    return AttendanceRecord.objects.create(employee_id=profile.biometric_employee_id, full_name=profile.user.username,
                                           branch=profile.branch, timestamp=ts, attendance_status=status,
                                           raw_row={"time": ts.isoformat(), "label": "Check In" if kind == "in" else "Check Out"})


def day_punches(profile, day, am_in=None, am_out=None, pm_in=None, pm_out=None):
    for value, kind in ((am_in, "in"), (am_out, "out"), (pm_in, "in"), (pm_out, "out")):
        if value:
            punch(profile, day, value, kind)


def non_working(day, br=None, kind="holiday", verified=True, name="Holiday"):
    scope = HolidaySuspension.SCOPE_BRANCH if br else HolidaySuspension.SCOPE_REGION
    return HolidaySuspension.objects.create(date=day, name=name, type=kind, scope=scope, branch=br, is_payroll_verified=verified)


def period(start, end, mode="FIRST_HALF"):
    return PayrollPeriod.objects.create(name=f"{start} - {end}", start_date=start, end_date=end, pay_mode=mode)
