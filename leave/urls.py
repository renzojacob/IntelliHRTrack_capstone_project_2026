from django.urls import path
from . import views

urlpatterns = [
    # employee
    path("request/", views.employee_request_leave, name="employee_request_leave"),
    path("my/", views.employee_my_leaves, name="employee_my_leaves"),

    # admin
    path("admin/pending/", views.admin_pending_leaves, name="admin_pending_leaves"),
    path("admin/<int:leave_id>/<str:action>/", views.admin_decide_leave, name="admin_decide_leave"),
]
