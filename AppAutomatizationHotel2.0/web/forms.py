from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError as DjangoValidationError

from .models import SystemConfiguration
from .services.exception_resolution import resolution_statuses
from .services.user_management import (
    ROLE_CHOICES,
    ROLE_STANDARD,
    ROLE_SUPERADMIN,
    active_superadmin_count,
)


class ToleranceSettingsForm(forms.ModelForm):
    """Form for reconciliation tolerances (accessible to all authenticated users)."""

    class Meta:
        model = SystemConfiguration
        fields = ["usd_rounding_tolerance", "clp_rounding_tolerance"]
        labels = {
            "usd_rounding_tolerance": "Tolerancia USD (±US$)",
            "clp_rounding_tolerance": "Tolerancia CLP (±$)",
        }
        widgets = {
            "usd_rounding_tolerance": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0", "max": "5"}),
            "clp_rounding_tolerance": forms.NumberInput(attrs={"class": "form-control", "step": "1", "min": "0", "max": "5000"}),
        }


class ExceptionResolutionForm(forms.Form):
    """Closes or reopens one exception from the "Pendientes" inbox; business rules live in exception_resolution."""

    status = forms.ChoiceField(label="Motivo", required=False, widget=forms.Select(attrs={"class": "form-select"}))
    comment = forms.CharField(
        label="Comentario", required=False, max_length=1000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "Qué se hizo, N° de documento del ajuste, quién lo informó…"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [("", "Selecciona un motivo")] + [
            (s.code, s.display_name) for s in resolution_statuses()
        ]


def _validate_password_pair(form, password_field: str, confirm_field: str, user_candidate: User) -> None:
    """Shared validator ensuring confirmation matches and Django password policy holds."""
    pw1 = form.cleaned_data.get(password_field) or ""
    pw2 = form.cleaned_data.get(confirm_field) or ""
    if not pw1 and not pw2:
        return
    if pw1 != pw2:
        form.add_error(confirm_field, "Las contraseñas no coinciden.")
        return
    try:
        validate_password(pw1, user=user_candidate)
    except DjangoValidationError as exc:
        for msg in exc.messages:
            form.add_error(password_field, msg)


class ManagedUserCreateForm(forms.Form):
    """Superadmin form to create an active standard user or superadmin account."""

    username = forms.CharField(
        label="Nombre de usuario",
        max_length=150,
        validators=[UnicodeUsernameValidator()],
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "ej. jperez", "autocomplete": "off"}),
    )
    first_name = forms.CharField(
        label="Nombre",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Nombre"}),
    )
    last_name = forms.CharField(
        label="Apellido",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Apellido"}),
    )
    email = forms.EmailField(
        label="Correo electrónico",
        required=False,
        widget=forms.EmailInput(attrs={"class": "form-control", "placeholder": "auditor@hotel.cl"}),
    )
    role = forms.ChoiceField(
        label="Rol de acceso",
        choices=ROLE_CHOICES,
        initial=ROLE_STANDARD,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    password = forms.CharField(
        label="Contraseña",
        widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "new-password"}),
    )
    confirm_password = forms.CharField(
        label="Confirmar contraseña",
        widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "new-password"}),
    )

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Ya existe una cuenta registrada con ese nombre de usuario.")
        return username

    def clean(self):
        cleaned = super().clean()
        candidate = User(
            username=cleaned.get("username") or "",
            first_name=cleaned.get("first_name") or "",
            last_name=cleaned.get("last_name") or "",
            email=cleaned.get("email") or "",
        )
        _validate_password_pair(self, "password", "confirm_password", candidate)
        return cleaned


class ManagedUserEditForm(forms.Form):
    """Superadmin form to edit profile/role and optionally reset a user's password."""

    first_name = forms.CharField(
        label="Nombre",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    last_name = forms.CharField(
        label="Apellido",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    email = forms.EmailField(
        label="Correo electrónico",
        required=False,
        widget=forms.EmailInput(attrs={"class": "form-control"}),
    )
    role = forms.ChoiceField(
        label="Rol de acceso",
        choices=ROLE_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    new_password = forms.CharField(
        label="Nueva contraseña (opcional)",
        required=False,
        widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "new-password", "placeholder": "Dejar en blanco para mantener la actual"}),
    )
    confirm_new_password = forms.CharField(
        label="Confirmar nueva contraseña",
        required=False,
        widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "new-password"}),
    )

    def __init__(self, *args, actor: User = None, target_user: User = None, **kwargs):
        self.actor = actor
        self.target_user = target_user
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        new_pw = cleaned.get("new_password") or ""
        confirm_pw = cleaned.get("confirm_new_password") or ""
        if confirm_pw and not new_pw:
            self.add_error("new_password", "Ingresa la nueva contraseña antes de confirmarla.")

        candidate = self.target_user or User()
        _validate_password_pair(self, "new_password", "confirm_new_password", candidate)

        role = cleaned.get("role")
        if self.target_user is not None and role:
            want_super = role == ROLE_SUPERADMIN
            if self.actor is not None and self.target_user.pk == self.actor.pk and self.target_user.is_superuser and not want_super:
                self.add_error("role", "No puedes quitarte el rol de superadmin a ti mismo.")
            elif (
                self.target_user.is_superuser
                and self.target_user.is_active
                and not want_super
                and active_superadmin_count(exclude_user_id=self.target_user.pk) == 0
            ):
                self.add_error("role", "Debe quedar al menos un superadmin activo en el sistema.")

        return cleaned
