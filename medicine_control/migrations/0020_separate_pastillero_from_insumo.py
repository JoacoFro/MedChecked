from django.db import migrations, models


def copy_insumo_names_to_pastillero(apps, schema_editor):
    Pastillero = apps.get_model('medicine_control', 'Pastillero')
    Insumo = apps.get_model('medicine_control', 'Insumo')
    insumo_names = dict(Insumo.objects.values_list('id', 'nombre'))

    for registro in Pastillero.objects.all():
        registro.nombre = insumo_names.get(registro.insumo_id, 'Medicamento')
        registro.save(update_fields=['nombre'])


class Migration(migrations.Migration):
    dependencies = [
        ('medicine_control', '0019_pastillero_cantidad_total_alter_pastillero_cantidad'),
    ]

    operations = [
        migrations.AddField(
            model_name='pastillero',
            name='nombre',
            field=models.CharField(max_length=100, null=True),
        ),
        migrations.RunPython(copy_insumo_names_to_pastillero, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='pastillero',
            name='nombre',
            field=models.CharField(max_length=100),
        ),
        migrations.RemoveField(
            model_name='pastillero',
            name='insumo',
        ),
    ]
