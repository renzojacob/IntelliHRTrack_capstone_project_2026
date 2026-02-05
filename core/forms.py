from django import forms
from .models import AttendanceRecord


class AttendanceRecordForm(forms.ModelForm):
    class Meta:
        model = AttendanceRecord
        fields = [
            "employee_id",
            "full_name",
            "timestamp",
            "event_type",
            "device_name",
            "verification_mode",
            "event_code",
        ]
        widgets = {
            "timestamp": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }


class AttendanceImportForm(forms.Form):
    csv_file = forms.FileField()
    skip_duplicates = forms.BooleanField(required=False, initial=True)
    device_source = forms.CharField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['csv_file'].widget.attrs.update({'class': 'rounded-lg border border-gray-300 px-3 py-2'})
        self.fields['device_source'].widget.attrs.update({'class': 'rounded-lg border border-gray-300 px-3 py-2', 'placeholder': 'Device name'})
