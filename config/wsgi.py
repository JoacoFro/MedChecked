import os
import subprocess
import sys
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

if os.environ.get('RUN_MAIN') != 'true':
    try:
        # Obtenemos la ruta absoluta al archivo main.py
        current_path = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_path)
        bot_path = os.path.join(project_root, "Astrana", "main.py")

        print(f"DEBUG: Intentando lanzar bot en: {bot_path}")

        # Lanzamos con -u para ver logs y capturamos errores
        subprocess.Popen(
            [sys.executable, "-u", bot_path],
            cwd=project_root, # Ejecutamos desde la raíz del proyecto
            stdout=None, 
            stderr=None
        )
        print("✅ [WSGI] Astrana lanzada con ruta absoluta.")
    except Exception as e:
        print(f"❌ [WSGI] Error crítico al lanzar: {e}")

application = get_wsgi_application()