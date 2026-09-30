from django.apps import AppConfig
from django.db.models.signals import post_migrate


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    label = "accounts"

    def ready(self):
        from .rbac import sync_rbac_after_migrate

        post_migrate.connect(sync_rbac_after_migrate, sender=self)
