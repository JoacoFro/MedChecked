#!/bin/bash

# 1. Iniciar Gunicorn (la web) en segundo plano
echo "🌐 Iniciando Gunicorn..."
gunicorn config.wsgi:application --bind 0.0.0.0:$PORT &

# 2. Esperar un poco para que la web se asiente
sleep 5

# 3. Iniciar Astrana usando un "flock" a nivel sistema operativo
# Esto es infalible: si el script intenta correr dos veces, el segundo se bloquea
echo "🤖 Intentando levantar Astrana..."
exec flock -n /tmp/astrana.lock python -u Astrana/main.py