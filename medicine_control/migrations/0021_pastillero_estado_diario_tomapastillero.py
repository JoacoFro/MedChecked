import django.db.models.deletion
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ('medicine_control', '0020_separate_pastillero_from_insumo'),
    ]

    operations = [
        migrations.AddField(
            model_name='pastillero',
            name='estado_diario',
            field=models.CharField(
                choices=[
                    ('pendiente', 'Pendiente'),
                    ('tomado', 'Tomado'),
                    ('omitido', 'Omitido'),
                ],
                default='pendiente',
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='pastillero',
            name='estado_diario_fecha',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='TomaPastillero',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('cantidad', models.PositiveIntegerField()),
                ('fecha_hora', models.DateTimeField(default=django.utils.timezone.now)),
                ('medicamento', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='historial_tomas',
                    to='medicine_control.pastillero',
                )),
            ],
            options={
                'verbose_name': 'Toma de pastillero',
                'verbose_name_plural': 'Tomas de pastillero',
                'ordering': ['-fecha_hora'],
            },
        ),
    ]
