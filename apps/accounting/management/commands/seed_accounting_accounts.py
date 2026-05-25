from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.accounting.services import seed_default_accounts_for_tenants
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Seed the default Chart of Accounts for existing tenants."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            action="append",
            dest="tenant_slugs",
            help="Tenant slug to seed. Can be passed multiple times. Defaults to all active tenants.",
        )
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Include inactive tenants when seeding all tenants.",
        )

    def handle(self, *args, **options):
        tenant_slugs = options.get("tenant_slugs") or []
        include_inactive = options.get("include_inactive", False)

        tenants = Tenant.objects.order_by("slug")

        if tenant_slugs:
            normalized_slugs = [slug.strip().lower() for slug in tenant_slugs if slug.strip()]
            tenants = tenants.filter(slug__in=normalized_slugs)
            found_slugs = set(tenants.values_list("slug", flat=True))
            missing_slugs = sorted(set(normalized_slugs) - found_slugs)
            if missing_slugs:
                raise CommandError(
                    f"Tenant slug(s) not found: {', '.join(missing_slugs)}"
                )
        elif not include_inactive:
            tenants = tenants.filter(is_active=True)

        tenants = list(tenants)
        if not tenants:
            self.stdout.write(self.style.WARNING("No tenants found to seed."))
            return

        results = seed_default_accounts_for_tenants(tenants=tenants)

        for result in results:
            tenant = result["tenant"]
            self.stdout.write(
                f"{tenant.slug}: created {result['created_count']} default account(s), "
                f"kept {result['existing_count']} existing account(s)."
            )

        created_total = sum(result["created_count"] for result in results)
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded default accounting accounts for {len(results)} tenant(s); "
                f"{created_total} account(s) created."
            )
        )
