# Generated by Django 4.2.16 on 2025-05-13 14:34

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Contract',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('client_name', models.CharField(max_length=255)),
                ('start_date', models.DateField()),
                ('end_date', models.DateField()),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('signed', 'Signed'), ('active', 'Active'), ('expired', 'Expired'), ('terminated', 'Terminated')], default='draft', max_length=20)),
                ('total_value', models.DecimalField(decimal_places=2, max_digits=10)),
                ('signed_date', models.DateField(blank=True, null=True)),
                ('document', models.FileField(blank=True, null=True, upload_to='contracts/')),
            ],
        ),
    ]
