# Schema only (D-073): the portal settings get the name, the website, the colours and the two
# logos, and the rights to view and to change them; the security log gets the event of a rejected
# upload. No data is written here: an empty row means "use the server configuration".

import leg_platform.accounts.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0024_mailjob_greeting_name'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='portalsettings',
            options={'default_permissions': ('view', 'change'), 'verbose_name': 'portal settings', 'verbose_name_plural': 'portal settings'},
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='accent_dark',
            field=models.CharField(blank=True, max_length=7, validators=[leg_platform.accounts.models.validate_hex_colour], verbose_name='accent colour, dark mode'),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='accent_light',
            field=models.CharField(blank=True, max_length=7, validators=[leg_platform.accounts.models.validate_hex_colour], verbose_name='accent colour, light mode'),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='highlight',
            field=models.CharField(blank=True, help_text='Used only for the small dots in the lists of the login page, never for text.', max_length=7, validators=[leg_platform.accounts.models.validate_hex_colour], verbose_name='highlight colour'),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='logo_dark_data',
            field=models.BinaryField(null=True),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='logo_dark_sha256',
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='logo_dark_type',
            field=models.CharField(blank=True, choices=[('png', 'PNG'), ('svg', 'SVG')], editable=False, max_length=3),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='logo_light_data',
            field=models.BinaryField(null=True),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='logo_light_sha256',
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='logo_light_type',
            field=models.CharField(blank=True, choices=[('png', 'PNG'), ('svg', 'SVG')], editable=False, max_length=3),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='name',
            field=models.CharField(blank=True, help_text='Shown in the header, in page titles, in the subject of emails and in the administration. At most 100 characters. Leave empty to use the name from the server configuration.', max_length=100, validators=[leg_platform.accounts.models.validate_instance_name], verbose_name='name of the portal'),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='text_dark',
            field=models.CharField(blank=True, help_text='Leave empty to choose white or dark text automatically.', max_length=7, validators=[leg_platform.accounts.models.validate_hex_colour], verbose_name='text colour on the accent colour, dark mode'),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='text_light',
            field=models.CharField(blank=True, help_text='Leave empty to choose white or dark text automatically.', max_length=7, validators=[leg_platform.accounts.models.validate_hex_colour], verbose_name='text colour on the accent colour, light mode'),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='website_url',
            field=models.URLField(blank=True, help_text='The website of your community with the contact information. It is linked in the footer and used as the contact address in emails. It must start with https://. Leave empty to use the server configuration.', max_length=500, validators=[leg_platform.accounts.models.validate_https_address], verbose_name='address of the website'),
        ),
        migrations.AlterField(
            model_name='securityevent',
            name='event',
            field=models.CharField(choices=[('password_changed', 'Password changed'), ('password_reset', 'Password reset by link'), ('email_change_requested', 'Email address change requested'), ('email_changed', 'Email address changed'), ('account_blocked', 'Account blocked'), ('account_unblocked', 'Account unblocked'), ('account_deactivated', 'Account deleted (retention obligation)'), ('account_reactivated', 'Account restored'), ('account_deleted', 'Account deleted'), ('two_factor_enabled', 'Two-factor authentication set up'), ('two_factor_disabled', 'Two-factor authentication removed by the account itself'), ('two_factor_reset', 'Two-factor authentication reset by link or by a system administrator'), ('two_factor_reset_requested', 'Link for removing two-factor authentication requested'), ('recovery_codes_renewed', 'Recovery codes renewed'), ('recovery_code_used', 'Recovery code used'), ('code_attempts_warning', 'Many wrong codes entered'), ('code_attempts_paused', 'Code entry paused'), ('login_lockout', 'Account locked after failed logins'), ('privileges_changed', 'Access to the administration changed'), ('access_relations_changed', 'Roles or permissions changed'), ('initial_administrator_created', 'Initial system administrator created'), ('emergency_two_factor_reset', 'Two-factor authentication reset by the emergency command'), ('emergency_account_unblocked', 'Account unblocked by the emergency command'), ('emergency_keys_reencrypted', 'Second factor keys encrypted again by the emergency command'), ('graph_configuration_changed', 'Microsoft Graph configuration changed'), ('graph_certificate_uploaded', 'Microsoft Graph certificate uploaded'), ('graph_test_email_requested', 'Microsoft Graph test email requested'), ('portal_settings_changed', 'Portal settings changed'), ('portal_upload_rejected', 'Portal upload rejected'), ('email_change_cancelled', 'Requested email address change cancelled by the account'), ('email_change_stopped', 'Requested email address change stopped with the link of the notice'), ('secondary_email_changed', 'Second email address changed'), ('registration_completed', 'Registration completed'), ('session_ended_wrong_codes', 'Session ended after wrong codes'), ('all_sessions_ended', 'All sessions ended after wrong entries'), ('two_factor_removed_phone_lost', 'Two-factor authentication removed with a recovery code (phone lost)')], editable=False, max_length=40, verbose_name='event'),
        ),
    ]
