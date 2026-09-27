from django import forms

from .models import AttendanceRecord, Branch, BiometricDevice

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
            "employee_id": forms.TextInput(
                attrs={"class": "w-full rounded-lg border border-gray-300 px-3 py-2"}
            ),
            "full_name": forms.TextInput(
                attrs={"class": "w-full rounded-lg border border-gray-300 px-3 py-2"}
            ),
            "department": forms.TextInput(
                attrs={"class": "w-full rounded-lg border border-gray-300 px-3 py-2"}
            ),
            # ✅ IMPORTANT: DO NOT put static choices here.
            # branch is a FK, Django will render the Branch queryset automatically.
            "branch": forms.Select(
                attrs={"class": "w-full rounded-lg border border-gray-300 bg-white px-3 py-2"}
            ),
            "timestamp": forms.DateTimeInput(
                attrs={"type": "datetime-local", "class": "w-full rounded-lg border border-gray-300 px-3 py-2"}
            ),
            "attendance_status": forms.Select(
                attrs={"class": "w-full rounded-lg border border-gray-300 px-3 py-2"}
            ),
        }


class AttendanceImportForm(forms.Form):
    file = forms.FileField(required=True)
    skip_duplicates = forms.BooleanField(required=False, initial=True)

    # ✅ IMPORTANT FIX:
    # Use ModelChoiceField so cleaned_data["branch"] becomes a Branch instance
    # and the posted value is an ID that must exist in the queryset.
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.none(),  # set in views.py per-admin scope before is_valid()
        required=True,
        empty_label="Select branch",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["file"].widget.attrs.update({
            "class": "sr-only",
            "accept": ".csv,.xls,.xlsx",
        })

        self.fields["branch"].widget.attrs.update({
            "class": "mt-1 block w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm shadow-sm "
                     "focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
        })

        self.fields["skip_duplicates"].widget.attrs.update({
            "class": "h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
        })

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if not f:
            raise forms.ValidationError("Please upload a file.")
        name = (f.name or "").lower()
        if not (name.endswith(".csv") or name.endswith(".xls") or name.endswith(".xlsx")):
            raise forms.ValidationError("Unsupported file type. Upload .csv, .xls, or .xlsx")
        return f

class BiometricDeviceForm(forms.ModelForm):
    """
    Used by branch admins and superusers to register or update
    Hikvision devices through the website.
    """

    password = forms.CharField(
        label="Hikvision Password",
        required=False,
        strip=False,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={
                "class": (
                    "mt-2 block w-full rounded-2xl border border-gray-200 "
                    "bg-white px-4 py-3 text-sm text-gray-900 shadow-sm "
                    "outline-none transition focus:border-cyan-500 "
                    "focus:ring-2 focus:ring-cyan-500/20 "
                    "dark:border-white/10 dark:bg-slate-950/50 "
                    "dark:text-white"
                ),
                "placeholder": "Enter Hikvision password",
                "autocomplete": "new-password",
            },
        ),
    )

    class Meta:
        model = BiometricDevice

        fields = [
            "name",
            "ip_address",
            "port",
            "username",
            "password",
            "branch",
            "is_active",
        ]

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": (
                        "mt-2 block w-full rounded-2xl border border-gray-200 "
                        "bg-white px-4 py-3 text-sm text-gray-900 shadow-sm "
                        "outline-none transition focus:border-cyan-500 "
                        "focus:ring-2 focus:ring-cyan-500/20 "
                        "dark:border-white/10 dark:bg-slate-950/50 "
                        "dark:text-white"
                    ),
                    "placeholder": "Example: Calapan Main Entrance",
                }
            ),

            "ip_address": forms.TextInput(
                attrs={
                    "class": (
                        "mt-2 block w-full rounded-2xl border border-gray-200 "
                        "bg-white px-4 py-3 text-sm text-gray-900 shadow-sm "
                        "outline-none transition focus:border-cyan-500 "
                        "focus:ring-2 focus:ring-cyan-500/20 "
                        "dark:border-white/10 dark:bg-slate-950/50 "
                        "dark:text-white"
                    ),
                    "placeholder": "Example: 192.168.1.64",
                }
            ),

            "port": forms.NumberInput(
                attrs={
                    "class": (
                        "mt-2 block w-full rounded-2xl border border-gray-200 "
                        "bg-white px-4 py-3 text-sm text-gray-900 shadow-sm "
                        "outline-none transition focus:border-cyan-500 "
                        "focus:ring-2 focus:ring-cyan-500/20 "
                        "dark:border-white/10 dark:bg-slate-950/50 "
                        "dark:text-white"
                    ),
                    "min": 1,
                    "max": 65535,
                }
            ),

            "username": forms.TextInput(
                attrs={
                    "class": (
                        "mt-2 block w-full rounded-2xl border border-gray-200 "
                        "bg-white px-4 py-3 text-sm text-gray-900 shadow-sm "
                        "outline-none transition focus:border-cyan-500 "
                        "focus:ring-2 focus:ring-cyan-500/20 "
                        "dark:border-white/10 dark:bg-slate-950/50 "
                        "dark:text-white"
                    ),
                    "placeholder": "Example: admin",
                    "autocomplete": "off",
                }
            ),

            "branch": forms.Select(
                attrs={
                    "class": (
                        "mt-2 block w-full rounded-2xl border border-gray-200 "
                        "bg-white px-4 py-3 text-sm text-gray-900 shadow-sm "
                        "outline-none transition focus:border-cyan-500 "
                        "focus:ring-2 focus:ring-cyan-500/20 "
                        "dark:border-white/10 dark:bg-slate-950/50 "
                        "dark:text-white"
                    ),
                }
            ),

            "is_active": forms.CheckboxInput(
                attrs={
                    "class": (
                        "h-5 w-5 rounded border-gray-300 text-cyan-600 "
                        "focus:ring-cyan-500"
                    ),
                }
            ),
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user

        instance = kwargs.get("instance")
        self._existing_password = (
            instance.password
            if instance and instance.pk
            else ""
        )

        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            self.fields["password"].required = False
            self.fields["password"].help_text = (
                "Leave blank to keep the current Hikvision password."
            )
        else:
            self.fields["password"].required = True
            self.fields["password"].help_text = (
                "Enter the administrator password configured on the device."
            )

        if user and user.is_superuser:
            self.fields["branch"].queryset = (
                Branch.objects.all().order_by("name")
            )

        elif user and user.is_staff:
            try:
                admin_branch = user.profile.branch
            except Exception:
                admin_branch = None

            if admin_branch:
                self.fields["branch"].queryset = Branch.objects.filter(
                    pk=admin_branch.pk
                )

                self.fields["branch"].initial = admin_branch
                self.fields["branch"].disabled = True

            else:
                self.fields["branch"].queryset = Branch.objects.none()

        else:
            self.fields["branch"].queryset = Branch.objects.none()

    def clean_port(self):
        port = self.cleaned_data.get("port")

        if port is None or not 1 <= port <= 65535:
            raise forms.ValidationError(
                "Port must be between 1 and 65535."
            )

        return port

    def clean(self):
        cleaned_data = super().clean()

        branch = cleaned_data.get("branch")
        ip_address = cleaned_data.get("ip_address")
        port = cleaned_data.get("port")

        if self.user and self.user.is_staff and not self.user.is_superuser:
            try:
                admin_branch = self.user.profile.branch
            except Exception:
                admin_branch = None

            if not admin_branch:
                self.add_error(
                    "branch",
                    "Your admin account is not assigned to a branch.",
                )

            elif branch and branch.pk != admin_branch.pk:
                self.add_error(
                    "branch",
                    "You can register a device only for your assigned branch.",
                )

        if branch and ip_address and port:
            duplicate = BiometricDevice.objects.filter(
                branch=branch,
                ip_address=ip_address,
                port=port,
            )

            if self.instance and self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)

            if duplicate.exists():
                self.add_error(
                    "ip_address",
                    (
                        "This IP address and port are already registered "
                        "for this branch."
                    ),
                )

        return cleaned_data

    def save(self, commit=True):
        device = super().save(commit=False)

        entered_password = self.cleaned_data.get("password")

        if entered_password:
            device.password = entered_password
        elif self.instance and self.instance.pk:
            device.password = self._existing_password

        if (
            self.user
            and self.user.is_staff
            and not self.user.is_superuser
        ):
            device.branch = self.user.profile.branch

        if commit:
            device.save()

        return device
        