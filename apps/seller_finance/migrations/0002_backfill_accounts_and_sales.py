from decimal import Decimal

from django.db import migrations


def backfill(apps, schema_editor):
    """Open an account per seller and credit purchases confirmed before this module."""
    Seller = apps.get_model("sellers", "Seller")
    Purchase = apps.get_model("purchases", "Purchase")
    SellerAccount = apps.get_model("seller_finance", "SellerAccount")
    SellerTransaction = apps.get_model("seller_finance", "SellerTransaction")

    for seller in Seller.objects.all():
        account, _ = SellerAccount.objects.get_or_create(seller=seller)
        booked = set(
            SellerTransaction.objects.filter(account=account, kind="SALE").values_list(
                "reference", flat=True
            )
        )
        balance = account.balance
        purchases = Purchase.objects.filter(seller=seller, status="CONFIRMED").order_by(
            "confirmed_at", "id"
        )
        for purchase in purchases:
            reference = f"purchase:{purchase.pk}"
            if reference in booked:
                continue
            balance += purchase.total
            SellerTransaction.objects.create(
                account=account,
                kind="SALE",
                amount=purchase.total,
                balance_after=balance,
                description="Sale (backfilled)",
                reference=reference,
                created_at=purchase.confirmed_at,
            )
        if balance != account.balance:
            account.balance = balance.quantize(Decimal("0.01"))
            account.save(update_fields=["balance"])


class Migration(migrations.Migration):
    dependencies = [
        ("seller_finance", "0001_initial"),
        ("sellers", "0001_initial"),
        ("purchases", "0001_initial"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
