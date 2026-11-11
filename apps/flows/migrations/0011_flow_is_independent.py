from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('flows', '0010_flow_channel_platform'),
    ]

    operations = [
        migrations.AddField(
            model_name='flow',
            name='is_independent',
            field=models.BooleanField(
                default=False,
                help_text='Automação independente (ex: recuperação, pós-venda). Não é cancelada por conversas no chat.',
            ),
        ),
        migrations.AddField(
            model_name='flowexecution',
            name='is_independent',
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text='Indica se esta execução é de uma automação independente.',
            ),
        ),
    ]
