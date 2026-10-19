# AGENTS.md

## Contexto del proyecto

Este repositorio es una aplicación Django para control de stock de medicamentos, con una capa web y un bot de Telegram. La lógica principal del dominio está en `medicine_control`, mientras que `Astrana/` contiene un motor de asistencia/NLP y `bot_interactivo.py` es el punto de entrada del bot Telegram.

## Estructura importante

- `config/settings.py`: configuración global de Django, DB, seguridad y estáticos.
- `config/urls.py`: rutas principales del sitio.
- `medicine_control/models.py`: modelos del dominio: `Insumo`, `Pedido`, `Salida`, `Envio`, `Pastillero`, etc.
- `medicine_control/views.py`: lógica de negocio, cálculos de stock y endpoints.
- `medicine_control/templates/medicine_control/`: templates HTML de la UI.
- `Astrana/main.py`: flujo principal de la asistente Astrana.
- `Astrana/nlp_engine.py`: interpretador/engine de lenguaje natural.
- `bot_interactivo.py`: bot de Telegram interactivo.
- `manage.py`: comando principal de Django.

## Comandos frecuentes

- Instalar dependencias: `pip install -r requirements.txt`
- Migraciones: `python manage.py makemigrations` y `python manage.py migrate`
- Ejecutar servidor local: `python manage.py runserver`
- Ejecutar tests: `python manage.py test`
- Entrar en el bot Telegram: `python bot_interactivo.py`
- Crear superusuario: `python create_admin.py`

## Convenciones del códigobase

- El proyecto está en español y gran parte de la lógica de negocio usa nombres y mensajes en español.
- Cuando se toque lógica de stock o autonomía, revisar tanto el modelo como las vistas/templates relacionadas; los cálculos suelen estar duplicados entre ellos.
- Los scripts que importan modelos Django deben ejecutar `django.setup()` antes de importar modelos.
- El comportamiento de producción usa variables de entorno y `config/settings.py` resuelve SQLite local o Postgres según `DATABASE_URL`.
- No hardcodear tokens, secretos o datos sensibles; preferir `.env` y variables de entorno.
- Si se modifica el modelo, revisar si hace falta crear o ajustar migraciones.
- Mantener la convención de app `medicine_control` para lo empresarial y evitar introducir nueva lógica en `config` salvo configuración.

## Reglas para tareas de código

- Priorizar cambios pequeños y específicos sobre refactors grandes.
- Si se agrega funcionalidad de bot o Astrana, mantener la compatibilidad con los modelos y la base de datos existente.
- Para cambios de UI, revisar primero los templates y el contexto que les pasa `views.py`.
- Para cambios de negocio, validar el flujo en modelos/vistas y no solo en el frontend.
- Mantener SQL y queries simples; este proyecto usa Django ORM y modelos concretos.

## Puntos de atención

- Hay varios scripts de bot y asistentes en el repo; no asumir que todo se controla por un único sistema.
- La app tiene varios patrones heredados y nombres en español/inglés mezclados; conservar coherencia con el estilo actual del archivo que se está modificando.
- Hay una base SQLite por defecto local, así que los cambios deben ser compatibles con el entorno de desarrollo y con despliegue en Render.

## Objetivo general

Ayudar a que un agente de IA entienda rápidamente la estructura del proyecto, las normas del dominio y los puntos clave para iterar sin romper la lógica del control de stock ni la integración con Telegram/Astrana.
