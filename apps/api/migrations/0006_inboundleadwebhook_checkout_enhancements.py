import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('flows', '0001_initial'),
        ('api', '0005_alter_inboundleadwebhook_managers'),
    ]

    operations = [
        migrations.AlterField(
            model_name='inboundleadwebhook',
            name='target_flow',
            field=models.ForeignKey(
                blank=True,
                help_text='O fluxo iniciado ao receber um lead ou evento (opcional).',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='inbound_lead_webhooks',
                to='flows.flow',
            ),
        ),
        migrations.AddField(
            model_name='inboundleadwebhook',
            name='remove_tags',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Lista de etiquetas para remover do contato ao receber este webhook (ex: 'Venda pendente').",
            ),
        ),
        migrations.AddField(
            model_name='inboundleadwebhook',
            name='event_rules',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='Regras de múltiplos eventos por tipo/status (ex: pix_generated, order_approved).',
            ),
        ),
        migrations.AddField(
            model_name='inboundleadwebhook',
            name='recent_test_payload',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Último payload de teste capturado para facilitar o mapeamento visual.',
            ),
        ),
    ]
