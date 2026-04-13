from django.db import models

class Insumo(models.Model):
    nombre = models.CharField(max_length=100)
    stock_actual_cajas = models.IntegerField() 
    unidades_por_caja = models.IntegerField(default=30)
    consumo_diario = models.IntegerField(default=0)
    backup_unidades = models.IntegerField(default=0)

    def obtener_ultimo_lugar(self):
        # Buscamos el último pedido vinculado a este insumo
        ultimo_pedido = self.pedido_set.order_by('-fecha', '-id').first()
        if ultimo_pedido:
            return ultimo_pedido.lugar_compra
        return "Sin registros"

class Pedido(models.Model):
    TIPO_CHOICES = [
        ('normal', 'Obra Social'),
        ('propio', 'Cuenta Propia'),
    ]
    
    # IMPORTANTE: on_delete (sin el 'run') y el Insumo tiene que estar arriba
    insumo = models.ForeignKey(Insumo, on_delete=models.CASCADE)
    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES)
    cantidad = models.IntegerField()
    fecha = models.DateField()
    lugar_compra = models.CharField(max_length=100, blank=True, null=True)

    def __str__(self):
        return f"{self.tipo} - {self.fecha}"