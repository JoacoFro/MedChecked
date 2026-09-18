from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('medicine_control', '0021_pastillero_estado_diario_tomapastillero'),
    ]

    operations = [
        migrations.CreateModel(
            name='AprendizajeAstrana',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('chat_id', models.CharField(default='principal', max_length=100)),
                ('frase', models.TextField()),
                ('intencion', models.CharField(max_length=100)),
                ('respuesta', models.TextField(blank=True)),
                ('confirmado', models.BooleanField(default=False)),
                ('fecha', models.DateTimeField(auto_now_add=True)),
            ],
            options={'ordering': ['-fecha']},
        ),
        migrations.CreateModel(
            name='MemoriaAstrana',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('chat_id', models.CharField(default='principal', max_length=100)),
                ('categoria', models.CharField(choices=[('preferencia', 'Preferencia'), ('contexto', 'Contexto'), ('alias', 'Alias')], max_length=20)),
                ('clave', models.CharField(max_length=100)),
                ('valor', models.TextField()),
                ('confirmada', models.BooleanField(default=False)),
                ('activa', models.BooleanField(default=True)),
                ('fecha_actualizacion', models.DateTimeField(auto_now=True)),
            ],
            options={'ordering': ['-fecha_actualizacion']},
        ),
        migrations.AddConstraint(
            model_name='memoriaastrana',
            constraint=models.UniqueConstraint(fields=('chat_id', 'categoria', 'clave'), name='memoria_astrana_chat_categoria_clave_unica'),
        ),
    ]
