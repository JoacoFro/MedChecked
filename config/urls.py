from django.contrib import admin
from django.urls import path
from medicine_control.views import home, cargar_insumo, lista_insumos # Agregá lista_insumos aquí

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', home, name='home'),
    path('cargar/', cargar_insumo, name='cargar'),
    path('lista/', lista_insumos, name='lista'), 
]