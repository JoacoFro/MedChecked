from django.contrib import admin
from .models import Insumo, Pedido, Salida, HistorialMovimiento, Envio

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