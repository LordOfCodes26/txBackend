from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("cash", "0003_simple")]

    operations = [migrations.DeleteModel(name="CashEntry")]
