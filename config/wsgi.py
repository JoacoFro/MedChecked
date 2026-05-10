import os
import subprocess
import sys
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# --- EL LANZADOR DE ASTRANA ---
# Solo lanzamos el bot si no es un proceso de "reloader" y si estamos en el proceso principal
if os.environ.get('RUN_MAIN') != 'true': # Evita que se dispare dos veces en local
    try:
        # Lanzamos el bot como un proceso hijo totalmente independiente
        subprocess.Popen([sys.executable, "Astrana/main.py"])
        print("✅ [WSGI] Astrana lanzada como proceso independiente.")
    except Exception as e:
        print(f"❌ [WSGI] No se pudo lanzar Astrana: {e}")

application = get_wsgi_application()