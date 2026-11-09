from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('medicine_control', '0024_envio_un_envio_abierto_por_tipo'),
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
                    ('proyecto', 'Proyecto'),
                ],
                max_length=20,
            ),
        ),
    ]
