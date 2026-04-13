from django import forms
from .models import Insumo
from datetime import date

class PedidoForm(forms.ModelForm):
    CANTIDADES_SUGERIDAS = [(300, '300 unidades'), (100, '100 unidades')]
    
    cantidad_sugerida = forms.ChoiceField(
        choices=CANTIDADES_SUGERIDAS,
        label="Cantidad",
        widget=forms.Select(attrs={'class': 'form-control bg-dark text-white border-secondary', 'id': 'id_sugerida'})
    )
    
    es_manual = forms.BooleanField(
        required=False, 
        label="Ingresar manualmente", 
        widget=forms.CheckboxInput(attrs={'id': 'id_es_manual', 'class': 'form-check-input'})
    )
    
    cantidad_manual = forms.IntegerField(
        required=False, 
        widget=forms.NumberInput(attrs={'class': 'form-control bg-dark text-white border-secondary d-none', 'id': 'id_manual', 'placeholder': 'Ej: 150'})
    )

    fecha_pedido = forms.DateField(
        initial=date.today,
        widget=forms.DateInput(attrs={'class': 'form-control bg-dark text-white border-secondary', 'type': 'date'})
    )

    class Meta:
        model = Insumo
        fields = ['nombre']
        widgets = {
            'nombre': forms.TextInput(attrs={'class': 'form-control bg-dark text-white border-secondary'})
        }