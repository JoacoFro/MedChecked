from django.shortcuts import render, redirect
from .forms import PedidoForm
from .models import Insumo, Pedido
from django.db.models import Sum
from datetime import datetime, timedelta
from django.utils.dateparse import parse_date

def home(request):
    insumos = Insumo.objects.all()
    
    # 1. Calculamos el total de unidades físicas (Cajas * Unidades por caja)
    total_unidades = sum(i.stock_actual_cajas * i.unidades_por_caja for i in insumos)
    
    # 2. Consumo diario total
    consumo_total = insumos.aggregate(Sum('consumo_diario'))['consumo_diario__sum'] or 0
    
    # 3. Autonomía Real
    autonomia = total_unidades // consumo_total if consumo_total > 0 else 0
    
    # 4. Porcentaje para la barra (basado en el pack de 300)
    porcentaje = min((total_unidades / 300) * 100, 100)
    
    # 5. Fecha sugerida de próximo pedido (Hoy + Autonomía)
    proximo_pedido = datetime.now() + timedelta(days=autonomia)

    context = {
        'autonomia': autonomia,
        'porcentaje': porcentaje,
        'proximo_pedido': proximo_pedido,
    }
    return render(request, 'medicine_control/home.html', context)

def cargar_insumo(request):
    if request.method == 'POST':
        insumo_default, created = Insumo.objects.get_or_create(
            nombre="Sondas", 
            defaults={'stock_actual_cajas': 0}
        )
        
        tipo = request.POST.get('tipo_pedido')
        fecha_str = request.POST.get('fecha')
        fecha = parse_date(fecha_str)
        
        if tipo == 'normal':
            cantidad = 300
            insumo_default.stock_actual_cajas += 10
            lugar = "Obra Social" 
        else:
            cantidad = int(request.POST.get('cantidad', 0))
            insumo_default.backup_unidades += cantidad
            lugar = request.POST.get('lugar_compra')

        insumo_default.save()

        # Creamos el registro del historial
        Pedido.objects.create(
            insumo=insumo_default,
            tipo=tipo,
            cantidad=cantidad,
            fecha=fecha,
            lugar_compra=lugar
        )
        
        # 1. REDIRECT: Usamos 'lista' porque así está en tu urls.py (name='lista')
        return redirect('lista') 
    
    return render(request, 'medicine_control/cargar_insumo.html')

def lista_insumos(request):
    insumos = Insumo.objects.all()
    
    # 2. HISTORIAL: Traemos los pedidos para que la tabla muestre lo que agregás
    ingresos = Pedido.objects.all().order_by('-fecha') 

    # --- Tus cálculos de stock ---
    total_unidades = sum((i.stock_actual_cajas * i.unidades_por_caja) + i.backup_unidades for i in insumos)
    total_cajas = insumos.aggregate(Sum('stock_actual_cajas'))['stock_actual_cajas__sum'] or 0
    total_backup = insumos.aggregate(Sum('backup_unidades'))['backup_unidades__sum'] or 0
    consumo_diario_total = insumos.aggregate(Sum('consumo_diario'))['consumo_diario__sum'] or 0
    autonomia = total_unidades // consumo_diario_total if consumo_diario_total > 0 else 0
    unidades_os = sum(i.stock_actual_cajas * i.unidades_por_caja for i in insumos)
    porcentaje_general = min((unidades_os / 300) * 100, 100)

    context = {
        'insumos': insumos,
        'ingresos': ingresos, 
        'total_unidades': total_unidades,
        'total_cajas': total_cajas,
        'total_backup': total_backup,
        'autonomia': autonomia,
        'consumo_diario': consumo_diario_total,
        'porcentaje_general': porcentaje_general,
    }
    
    # 3. RENDER: Usamos el nombre en plural como me confirmaste recién
    return render(request, 'medicine_control/lista_insumos.html', context)