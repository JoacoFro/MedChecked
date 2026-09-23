from django.contrib import admin
from django.urls import path
from django.views.generic.base import RedirectView
from django.templatetags.static import static
from medicine_control import views  # Importamos el módulo completo para ser más ordenados
from medicine_control.views import cron_monitoreo_sistema, pastillero_view

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.home, name='home'),
    path('cargar/', views.cargar_insumo, name='cargar'),
    path('lista/', views.lista_insumos, name='lista'), 
    path('salida/', views.registrar_salida, name='salida_stock'), 
    path('envios/', views.lista_envios, name='envios'),
    path('iniciar-pedido/', views.iniciar_pedido, name='iniciar_pedido'),
    path('marcar-recibido/', views.marcar_recibido_home, name='marcar_recibido_home'),
    path('api/v1/sistema-monitoreo/', cron_monitoreo_sistema, name='monitoreo_sistema'),
    path('pastillero/', pastillero_view, name='pastillero'),

    # === RUTAS PWA ASTRANA (Archivos en static/) ===
    # Redirigen las peticiones de la raíz /manifest.json y /sw.js a la carpeta static/
    path('manifest.json', RedirectView.as_view(url=static('manifest.json'), permanent=True)),
    path('sw.js', RedirectView.as_view(url=static('sw.js'), permanent=True)),

    # Vista principal donde vivirá la interfaz de la PWA de Astrana
    path('astrana/', views.home, name='astrana_pwa'),  # Reemplazaremos views.home por la vista del chat de Astrana más adelante
]