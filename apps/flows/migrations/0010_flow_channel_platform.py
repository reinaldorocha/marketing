from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('flows', '0009_alter_flow_status'),
        ('channels', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='flow',
            name='channel_platform',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Canal principal deste fluxo: whatsapp, instagram, ou vazio para multicanal.',
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name='flow',
            name='default_connection',
            field=models.ForeignKey(
                blank=True,
                help_text='Conexão padrão associada a este fluxo.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='flows',
                to='channels.channelconnection',
            ),
        ),
    ]
