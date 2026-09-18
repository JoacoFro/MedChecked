from django.contrib import admin
from .models import (
    Insumo,
    Pedido,
    Salida,
    HistorialMovimiento,
    Envio,
    Pastillero,
    TomaPastillero,
    MemoriaAstrana,
    AprendizajeAstrana,
)

# Registro simple para modelos básicos
admin.site.register(Pedido)
admin.site.register(Salida)
admin.site.register(HistorialMovimiento)

# Registro con columnas personalizadas para Insumos
@admin.register(Insumo)
class InsumoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'stock_actual_cajas', 'backup_unidades', 'consumo_diario')
    search_fields = ('nombre',)

# Registro con columnas personalizadas para Envíos (EL QUE TE FALTA)
@admin.register(Envio)
class EnvioAdmin(admin.ModelAdmin):
    # Esto hace que veas los datos en columnas en el admin
    list_display = ('tipo', 'estado', 'fecha_solicitud', 'fecha_recepcion')
    # Esto te agrega filtros a la derecha
    list_filter = ('estado', 'tipo', 'fecha_solicitud')
    # Permite buscar por las notas
    search_fields = ('notas',)
    list_display = ('fecha_solicitud', 'estado', 'tipo', 'fecha_cierre')

@admin.register(Pastillero)
class PastilleroAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'cantidad_total', 'estado_diario', 'estado_diario_fecha')
    list_filter = ('estado_diario', 'estado_diario_fecha')
    search_fields = ('nombre',)


@admin.register(TomaPastillero)
class TomaPastilleroAdmin(admin.ModelAdmin):
    list_display = ('fecha_hora', 'medicamento', 'cantidad')
    list_filter = ('fecha_hora',)
    search_fields = ('medicamento__nombre',)
    ordering = ('-fecha_hora',)


@admin.register(MemoriaAstrana)
class MemoriaAstranaAdmin(admin.ModelAdmin):
    list_display = ('chat_id', 'categoria', 'clave', 'valor', 'confirmada', 'activa', 'fecha_actualizacion')
    list_filter = ('categoria', 'confirmada', 'activa')
    search_fields = ('chat_id', 'clave', 'valor')


@admin.register(AprendizajeAstrana)
class AprendizajeAstranaAdmin(admin.ModelAdmin):
    list_display = ('chat_id', 'frase', 'intencion', 'confirmado', 'fecha')
    list_filter = ('intencion', 'confirmado')
    search_fields = ('chat_id', 'frase', 'respuesta')