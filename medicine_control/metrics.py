from datetime import timedelta

from django.db.models import Sum
from django.utils import timezone

from .models import Envio, Salida


def consumo_diario_estimado():
    hace_dos_semanas = timezone.now() - timedelta(days=14)
    unidades_consumidas = (
        Salida.objects.filter(fecha__gte=hace_dos_semanas)
        .aggregate(total=Sum('cantidad'))['total']
        or 0
    )
    return max(unidades_consumidas / 14, 8)


def demora_promedio_pedidos() -> float | None:
    hoy = timezone.localdate()
    demoras = Envio.objects.filter(estado='recibido').values_list(
        'fecha_solicitud',
        'fecha_cierre',
    )
    demoras_dias = [
        ((fecha_cierre or hoy) - fecha_solicitud).days
        for fecha_solicitud, fecha_cierre in demoras
    ]
    return sum(demoras_dias) / len(demoras_dias) if demoras_dias else None


def ultimo_pedido_recibido():
    return Envio.objects.filter(
        estado='recibido',
        fecha_cierre__isnull=False,
    ).order_by('-fecha_cierre', '-fecha_solicitud', '-id').first()
