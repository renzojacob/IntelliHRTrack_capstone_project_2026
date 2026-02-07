from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # auth
    path("auth/login/", views.login_ui, name="login_ui"),
    path("auth/signup/", views.signup_ui, name="signup_ui"),
    path("auth/logout/", auth_views.LogoutView.as_view(next_page="login_ui"), name="logout"),

    # -------------------------
    # Admin UI
    # -------------------------
    path("admin-ui/dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("admin-ui/leave/", views.admin_leave_approval, name="admin_leave"),
    path("admin-ui/leave/<int:leave_id>/approve/", views.admin_leave_approve, name="admin_leave_approve"),
    path("admin-ui/leave/<int:leave_id>/reject/", views.admin_leave_reject, name="admin_leave_reject"),

    path("admin-ui/biometrics/", views.admin_biometrics_attendance, name="admin_biometrics"),
    path("admin-ui/biometrics/import/", views.admin_biometrics_import, name="admin_biometrics_import"),
    path("admin-ui/biometrics/template/", views.admin_biometrics_template, name="admin_biometrics_template"),
    path("admin-ui/biometrics/export/", views.admin_biometrics_export, name="admin_biometrics_export"),

    # ✅ Employee Management (approve/reject + CRUD in same page)
    path("admin-ui/employees/", views.admin_employee_management, name="admin_employees"),

    # ✅ approve
    path(
        "admin-ui/employees/approve/<int:profile_id>/",
        views.approve_user,
        name="approve_user"
    ),

    # ✅ reject
    path(
        "admin-ui/employees/reject/<int:profile_id>/",
        views.reject_user,
        name="reject_user"
    ),

    path("admin-ui/scheduling/", views.admin_shift_scheduling, name="admin_scheduling"),
    path("admin-ui/payroll/", views.admin_payroll, name="admin_payroll"),
    path("admin-ui/reports/", views.admin_reports, name="admin_reports"),
    path("admin-ui/analytics/", views.admin_analytics, name="admin_analytics"),
    path("admin-ui/system/", views.admin_system_administration, name="admin_system"),

    # Attendance CRUD
    path("admin-ui/biometrics/records/new/", views.attendance_create, name="admin_attendance_create"),
    path("admin-ui/biometrics/records/<int:pk>/", views.attendance_detail, name="admin_attendance_detail"),
    path("admin-ui/biometrics/records/<int:pk>/edit/", views.attendance_update, name="admin_attendance_update"),
    path("admin-ui/biometrics/records/<int:pk>/delete/", views.attendance_delete, name="admin_attendance_delete"),

    # -------------------------
    # Employee UI
    # -------------------------
    path("employee/dashboard/", views.employee_dashboard, name="employee_dashboard"),
    path("employee/attendance/", views.employee_attendance, name="employee_attendance"),
    path("employee/schedule/", views.employee_schedule, name="employee_schedule"),
    path("employee/leave/", views.employee_leave, name="employee_leave"),
    path("employee/leave/<int:leave_id>/cancel/", views.employee_leave_cancel, name="employee_leave_cancel"),
    path("employee/payroll/", views.employee_payroll, name="employee_payroll"),
    path("employee/analytics/", views.employee_analytics, name="employee_analytics"),
    path("employee/notifications/", views.employee_notifications, name="employee_notifications"),
    path("employee/profile/", views.employee_profile, name="employee_profile"),
]
