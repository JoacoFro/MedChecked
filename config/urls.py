from django.contrib import admin
from django.urls import path
from medicine_control import views  # Importamos el módulo completo para ser más ordenados
from medicine_control.views import cron_monitoreo_sistema, pastillero_view
from medicine_control.views import astrana_chat_api, astrana_chat_view

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

    # Recursos PWA servidos en raíz para habilitar el alcance del service worker.
    path('manifest.json', views.astrana_manifest, name='astrana_manifest'),
    path('sw.js', views.astrana_service_worker, name='astrana_service_worker'),

    # Vista principal donde vivirá la interfaz de la PWA de Astrana
    path('astrana/', astrana_chat_view, name='astrana_pwa'),  # Reemplazaremos views.home por la vista del chat de Astrana más adelante
    path('astrana/icon.jpg', views.astrana_icon, name='astrana_icon'),
    path('astrana/icon-<int:size>.png', views.astrana_pwa_icon, name='astrana_pwa_icon'),
    path('api/astrana/chat/', astrana_chat_api, name='astrana_chat_api'),
    path('api/astrana/pwa/vapid-key/', views.astrana_vapid_public_key, name='astrana_vapid_public_key'),
    path('api/astrana/pwa/subscribe/', views.astrana_guardar_suscripcion_push, name='astrana_pwa_subscribe'),
    path('api/astrana/pwa/probar-push/', views.astrana_probar_push_api, name='astrana_pwa_probar_push'),
    path('api/astrana/pastillero/confirmar/', views.astrana_confirmar_toma_api, name='astrana_confirmar_toma_api'),
    path('api/astrana/pastillero/estado-hoy/', views.astrana_estado_pastillero_hoy, name='astrana_estado_pastillero_hoy'),
]