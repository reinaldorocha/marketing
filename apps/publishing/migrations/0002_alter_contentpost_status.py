from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('publishing', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='contentpost',
            name='status',
            field=models.CharField(
                choices=[
                    ('draft', 'Rascunho'),
                    ('scheduled', 'Agendado'),
                    ('publishing', 'Processando na Meta'),
                    ('published', 'Publicado'),
                    ('failed', 'Falha'),
                ],
                db_index=True,
                default='draft',
                max_length=32,
            ),
        ),
    ]
