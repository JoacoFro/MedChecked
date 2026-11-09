from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('medicine_control', '0025_memoria_astrana_categorias_personales'),
    ]

    operations = [
        migrations.AlterField(
            model_name='memoriaastrana',
            name='categoria',
            field=models.CharField(
                choices=[
                    ('preferencia', 'Preferencia'),
                    ('contexto', 'Contexto'),
                    ('alias', 'Alias'),
                    ('personal', 'Recuerdo personal'),
                    ('identidad', 'Identidad'),
                    ('preferencia_usuario', 'Preferencia personal'),
                    ('interes', 'Interés'),
                    ('proyecto', 'Proyecto'),
                    ('conversacion', 'Conversación importante'),
                    ('decision', 'Decisión'),
                ],
                max_length=20,
            ),
        ),
    ]
