from django import forms
from .models import AttendanceRecord


# ✅ Option B: Branch chosen in UI dropdown
BRANCH_CHOICES = [
    ("", "Select branch"),
    ("Main", "Main"),
    ("Branch A", "Branch A"),
    ("Branch B", "Branch B"),
]


class AttendanceRecordForm(forms.ModelForm):
    class Meta:
        model = AttendanceRecord
        fields = [
            "employee_id",
            "full_name",
            "department",
            "branch",
            "timestamp",
            "attendance_status",
        ]
        widgets = {
            "employee_id": forms.TextInput(attrs={"class": "w-full rounded-lg border border-gray-300 px-3 py-2"}),
            "full_name": forms.TextInput(attrs={"class": "w-full rounded-lg border border-gray-300 px-3 py-2"}),
            "department": forms.TextInput(attrs={"class": "w-full rounded-lg border border-gray-300 px-3 py-2"}),
            "branch": forms.Select(choices=BRANCH_CHOICES, attrs={"class": "w-full rounded-lg border border-gray-300 px-3 py-2"}),
            "timestamp": forms.DateTimeInput(attrs={"type": "datetime-local", "class": "w-full rounded-lg border border-gray-300 px-3 py-2"}),
            "attendance_status": forms.Select(attrs={"class": "w-full rounded-lg border border-gray-300 px-3 py-2"}),
        }


class AttendanceImportForm(forms.Form):
    file = forms.FileField(required=True)
    skip_duplicates = forms.BooleanField(required=False, initial=True)
    branch = forms.ChoiceField(choices=BRANCH_CHOICES, required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["file"].widget.attrs.update({
            "class": "sr-only",
            "accept": ".csv,.xls,.xlsx",
        })

        self.fields["branch"].widget.attrs.update({
            "class": "mt-1 block w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
        })

        self.fields["skip_duplicates"].widget.attrs.update({
            "class": "h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
        })

    def clean_branch(self):
        b = (self.cleaned_data.get("branch") or "").strip()
        if not b:
            raise forms.ValidationError("Please select a branch.")
        return b
