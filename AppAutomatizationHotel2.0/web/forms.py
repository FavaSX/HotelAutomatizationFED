from django import forms

from .models import SystemConfiguration
from .services.exception_resolution import resolution_statuses


class ToleranceSettingsForm(forms.ModelForm):
    """Superuser form for the reconciliation tolerances; ranges come from the model validators."""

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
