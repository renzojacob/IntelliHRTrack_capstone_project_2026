from django.urls import path
from . import views

urlpatterns = [
    # auth
    path("auth/login/", views.login_ui, name="login_ui"),
    path("auth/signup/", views.signup_ui, name="signup_ui"),

    # admin UI (MATCH base_admin.html URL NAMES)
    path("admin-ui/dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("admin-ui/biometrics/", views.admin_biometrics_attendance, name="admin_biometrics"),
    path("admin-ui/biometrics/import/", views.admin_biometrics_import, name="admin_biometrics_import"),
    path("admin-ui/employees/", views.admin_employee_management, name="admin_employees"),
    path("admin-ui/scheduling/", views.admin_shift_scheduling, name="admin_scheduling"),
    path("admin-ui/leave/", views.admin_leave_approval, name="admin_leave"),
    path("admin-ui/payroll/", views.admin_payroll, name="admin_payroll"),
    path("admin-ui/reports/", views.admin_reports, name="admin_reports"),
    path("admin-ui/analytics/", views.admin_analytics, name="admin_analytics"),
    path("admin-ui/system/", views.admin_system_administration, name="admin_system"),

     # admin UI
    path("admin-ui/dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("admin-ui/biometrics/", views.admin_biometrics_attendance, name="admin_biometrics"),
    path("admin-ui/biometrics/import/", views.admin_biometrics_import, name="admin_biometrics_import"),
    path("admin-ui/biometrics/template/", views.admin_biometrics_template, name="admin_biometrics_template"),
    path("admin-ui/biometrics/export/", views.admin_biometrics_export, name="admin_biometrics_export"),

    # CRUD
    path("admin-ui/biometrics/records/new/", views.attendance_create, name="admin_attendance_create"),
    path("admin-ui/biometrics/records/<int:pk>/", views.attendance_detail, name="admin_attendance_detail"),
    path("admin-ui/biometrics/records/<int:pk>/edit/", views.attendance_update, name="admin_attendance_update"),
    path("admin-ui/biometrics/records/<int:pk>/delete/", views.attendance_delete, name="admin_attendance_delete"),
]
