from datetime import timedelta

from django.db.models import Sum
from django.utils import timezone

from .models import Salida


def consumo_diario_estimado():
    hace_dos_semanas = timezone.now() - timedelta(days=14)
    unidades_consumidas = (
        Salida.objects.filter(fecha__gte=hace_dos_semanas)
        .aggregate(total=Sum('cantidad'))['total']
        or 0
    )
    return max(unidades_consumidas / 14, 8)
