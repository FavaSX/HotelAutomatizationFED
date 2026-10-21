from django import forms

from .models import SystemConfiguration


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
