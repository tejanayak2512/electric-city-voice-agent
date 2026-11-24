from django.db import migrations, models


def populate_default_substations(apps, schema_editor):
    Transformer = apps.get_model("admins", "Transformer")
    mapping = {
        "T-01": "North 33/11 kV Primary Substation",
        "T-02": "Central 33/11 kV Grid Substation",
        "T-03": "South 33/11 kV Industrial Substation",
    }
    for code, sub_name in mapping.items():
        Transformer.objects.filter(code=code).update(substation_name=sub_name)


class Migration(migrations.Migration):

    dependencies = [
        ("admins", "0002_departmentemployee_transformer_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="transformer",
            name="substation_name",
            field=models.CharField(default="Main 33/11 kV Substation", max_length=120),
        ),
        migrations.RunPython(populate_default_substations, migrations.RunPython.noop),
    ]
