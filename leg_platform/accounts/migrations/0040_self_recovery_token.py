from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0039_sign_in_method_return'),
    ]

    operations = [
        migrations.AlterField(
            model_name='verificationtoken',
            name='purpose',
            field=models.CharField(choices=[('registration', 'Registration'), ('password_reset', 'Password reset'), ('two_factor_reset', 'Two-factor authentication reset'), ('self_recovery', 'Sign-in recovery requested by the account'), ('method_return', 'Sign-in method return'), ('email_change', 'Email address change'), ('secondary_email', 'Second email address'), ('email_change_stop', 'Stop an email address change')], editable=False, max_length=20),
        ),
    ]
