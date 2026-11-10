from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('flows', '0007_flow_offline'),
    ]

    operations = [
        migrations.AddField(
            model_name='flow',
            name='require_follower',
            field=models.BooleanField(
                blank=True,
                default=None,
                help_text='Require Instagram follower before running flow. None inherits connection default.',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='flow',
            name='non_follower_message',
            field=models.TextField(
                blank=True,
                default='',
                help_text='Custom message sent to non-followers. Empty falls back to connection default.',
            ),
        ),
        migrations.AddField(
            model_name='flow',
            name='follow_button_label',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Custom label for the Instagram follow button. Empty falls back to connection default.',
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name='flow',
            name='confirm_button_label',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Custom label for the follow confirmation button. Empty falls back to connection default.',
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name='flow',
            name='follower_confirmation_text',
            field=models.TextField(
                blank=True,
                default='',
                help_text='Custom message sent once follower status is verified. Empty falls back to connection default.',
            ),
        ),
    ]
