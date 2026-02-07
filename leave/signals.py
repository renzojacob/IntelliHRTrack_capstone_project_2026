from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Employee, Branch

User = get_user_model()

@receiver(post_save, sender=User)
def create_employee_profile(sender, instance, created, **kwargs):
    if not created:
        return

    # default branch (pwede mo palitan later)
    branch, _ = Branch.objects.get_or_create(name="DA MIMAROPA – Default")

    # create Employee profile automatically
    Employee.objects.create(user=instance, branch=branch, role=Employee.ROLE_EMPLOYEE)
