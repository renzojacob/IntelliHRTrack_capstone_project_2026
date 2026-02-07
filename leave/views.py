from django.shortcuts import render

# Create your views here.
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import LeaveRequestForm
from .models import Employee, LeaveRequest


def _emp(request) -> Employee:
    # requires that every user has Employee profile
    return request.user.employee


def _require_admin(emp: Employee):
    if emp.role != Employee.ROLE_ADMIN:
        raise PermissionDenied("Admins only.")


@login_required
def employee_request_leave(request):
    emp = _emp(request)

    if request.method == "POST":
        form = LeaveRequestForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.employee = emp
            obj.branch = emp.branch  # ✅ AUTO assign (multi-branch safe)
            obj.status = LeaveRequest.STATUS_PENDING
            obj.save()

            messages.success(request, "Leave request submitted!")
            return redirect("employee_my_leaves")
    else:
        form = LeaveRequestForm()

    return render(request, "employee/leave_request_form.html", {"form": form, "current": "leave"})


@login_required
def employee_my_leaves(request):
    emp = _emp(request)
    leaves = LeaveRequest.objects.filter(employee=emp).order_by("-created_at")
    return render(request, "employee/my_leaves.html", {"leaves": leaves, "current": "leave"})


@login_required
def admin_pending_leaves(request):
    admin_emp = _emp(request)
    _require_admin(admin_emp)

    leaves = (
        LeaveRequest.objects.filter(branch=admin_emp.branch, status=LeaveRequest.STATUS_PENDING)
        .select_related("employee", "employee__user", "employee__branch")
        .order_by("-created_at")
    )

    return render(request, "admin/leave_approval.html", {
        "current": "leave",
        "leaves": leaves,
        "pending_count": leaves.count(),
    })


@require_POST
@login_required
def admin_decide_leave(request, leave_id, action):
    admin_emp = _emp(request)
    _require_admin(admin_emp)

    leave = get_object_or_404(LeaveRequest, id=leave_id)

    # ✅ CRITICAL SECURITY CHECK
    if leave.branch_id != admin_emp.branch_id:
        raise PermissionDenied("Not your branch")

    if leave.status != LeaveRequest.STATUS_PENDING:
        messages.info(request, "This request was already reviewed.")
        return redirect("admin_pending_leaves")

    if action == "approve":
        leave.status = LeaveRequest.STATUS_APPROVED
    elif action == "reject":
        leave.status = LeaveRequest.STATUS_REJECTED
    else:
        messages.error(request, "Invalid action.")
        return redirect("admin_pending_leaves")

    leave.reviewed_by = request.user
    leave.reviewed_at = timezone.now()
    leave.save()

    messages.success(request, f"Leave {action}d successfully.")
    return redirect("admin_pending_leaves")
