from django.db import connection, transaction
from django.utils import timezone

from .models import Pastillero, TomaPastillero


def medicamentos_pendientes_ids():
    connection.close_if_unusable_or_obsolete()
    hoy = timezone.localdate()
    return list(
        Pastillero.objects.filter(cantidad_total__gt=0)
        .exclude(
            estado_diario_fecha=hoy,
            estado_diario__in=['tomado', 'omitido'],
        )
        .values_list('id', flat=True)
    )


def registrar_toma_recordatorio(medicamento_id):
    hoy = timezone.localdate()
    with transaction.atomic():
        medicamento = Pastillero.objects.select_for_update().get(id=medicamento_id)
        if medicamento.estado_diario_fecha == hoy and medicamento.estado_diario in ['tomado', 'omitido']:
            return medicamento, False, 'ya_procesado'
        if medicamento.cantidad_total <= 0:
            medicamento.estado_diario = 'omitido'
            medicamento.estado_diario_fecha = hoy
            medicamento.save(update_fields=['estado_diario', 'estado_diario_fecha'])
            return medicamento, False, 'sin_stock'

        medicamento.cantidad_total -= 1
        medicamento.cantidad = 1
        medicamento.fecha_hora = timezone.now()
        medicamento.estado_diario = 'tomado'
        medicamento.estado_diario_fecha = hoy
        medicamento.save(update_fields=[
            'cantidad_total', 'cantidad', 'fecha_hora',
            'estado_diario', 'estado_diario_fecha',
        ])
        TomaPastillero.objects.create(medicamento=medicamento, cantidad=1)
        return medicamento, True, 'tomado'
