from django.apps import AppConfig


class AccountingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounting"

    def ready(self):
        from .posting import register_posting_handlers

        register_posting_handlers()
