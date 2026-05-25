from __future__ import annotations

from dataclasses import dataclass, field

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from apps.accounting.models import AccountingEvent, JournalEntry
from apps.accounting.posting import (
    ORDER_ACCOUNTING_STATUSES,
    ORDER_PAID_EVENT_TYPE,
    ORDER_SOURCE_MODEL,
    PAYMENT_PAID_EVENT_TYPE,
    PAYMENT_SOURCE_MODEL,
    ensure_order_cogs_posted,
    _order_item_cost_details,
    _order_paid_payload,
    _payment_paid_payload,
)
from apps.accounting.services import process_accounting_event, record_accounting_event
from apps.orders.models import Order
from apps.payments.models import Payment
from apps.tenants.models import Tenant


@dataclass
class BackfillStats:
    seen: int = 0
    processed: int = 0
    dry_run: int = 0
    skipped: int = 0
    failed: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


class Command(BaseCommand):
    help = "Backfill accounting events and journals for historical paid orders and payments."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be backfilled without creating events or journal entries.",
        )
        parser.add_argument(
            "--tenant",
            action="append",
            dest="tenant_slugs",
            help="Tenant slug to backfill. Can be passed multiple times. Defaults to all active tenants.",
        )
        parser.add_argument(
            "--date-from",
            dest="date_from",
            help="Only include records created on or after this YYYY-MM-DD date.",
        )
        parser.add_argument(
            "--date-to",
            dest="date_to",
            help="Only include records created on or before this YYYY-MM-DD date.",
        )
        parser.add_argument(
            "--allow-missing-cost",
            action="store_true",
            help="Post sales for orders with missing item costs and mark them as requiring attention.",
        )
        parser.add_argument(
            "--include-inactive-tenants",
            action="store_true",
            help="Include inactive tenants when no --tenant filter is supplied.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        allow_missing_cost = bool(options["allow_missing_cost"])
        date_from = self._parse_date_option(options.get("date_from"), "date-from")
        date_to = self._parse_date_option(options.get("date_to"), "date-to")
        if date_from and date_to and date_from > date_to:
            raise CommandError("--date-from cannot be after --date-to.")

        tenants = self._get_tenants(
            tenant_slugs=options.get("tenant_slugs") or [],
            include_inactive=bool(options["include_inactive_tenants"]),
        )
        if not tenants:
            self.stdout.write(self.style.WARNING("No tenants found for accounting backfill."))
            return

        mode = "DRY-RUN" if dry_run else "APPLY"
        self.stdout.write(f"{mode}: backfilling accounting for {len(tenants)} tenant(s).")

        order_stats = BackfillStats()
        payment_stats = BackfillStats()

        for tenant in tenants:
            self.stdout.write(f"Tenant {tenant.slug}: scanning orders and payments.")
            self._backfill_orders(
                tenant=tenant,
                date_from=date_from,
                date_to=date_to,
                dry_run=dry_run,
                allow_missing_cost=allow_missing_cost,
                stats=order_stats,
            )
            self._backfill_payments(
                tenant=tenant,
                date_from=date_from,
                date_to=date_to,
                dry_run=dry_run,
                stats=payment_stats,
            )

        self._write_summary("orders", order_stats)
        self._write_summary("payments", payment_stats)

    def _parse_date_option(self, value, option_name: str):
        if not value:
            return None
        parsed = parse_date(value)
        if parsed is None:
            raise CommandError(f"--{option_name} must be a valid YYYY-MM-DD date.")
        return parsed

    def _get_tenants(self, *, tenant_slugs: list[str], include_inactive: bool) -> list[Tenant]:
        tenants = Tenant.objects.order_by("slug")
        if tenant_slugs:
            normalized = [slug.strip().lower() for slug in tenant_slugs if slug.strip()]
            tenants = tenants.filter(slug__in=normalized)
            found = set(tenants.values_list("slug", flat=True))
            missing = sorted(set(normalized) - found)
            if missing:
                raise CommandError(f"Tenant slug(s) not found: {', '.join(missing)}")
        elif not include_inactive:
            tenants = tenants.filter(is_active=True)
        return list(tenants)

    def _apply_date_filters(self, queryset, *, date_from, date_to):
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)
        return queryset

    def _backfill_orders(
        self,
        *,
        tenant: Tenant,
        date_from,
        date_to,
        dry_run: bool,
        allow_missing_cost: bool,
        stats: BackfillStats,
    ) -> None:
        orders = (
            Order.objects.filter(tenant=tenant, status__in=ORDER_ACCOUNTING_STATUSES)
            .prefetch_related("items", "items__variant", "items__product", "payments")
            .order_by("created_at", "id")
        )
        orders = self._apply_date_filters(orders, date_from=date_from, date_to=date_to)

        for order in orders.iterator():
            stats.seen += 1
            idempotency_key = f"order-paid-{order.pk}"
            if self._order_has_sales_without_cogs(order=order, idempotency_key=idempotency_key):
                if dry_run:
                    stats.dry_run += 1
                    self.stdout.write(f"  DRY-RUN order {order.slug}: would repair missing COGS")
                else:
                    cogs_entry = ensure_order_cogs_posted(
                        order=order,
                        idempotency_key=idempotency_key,
                        allow_missing_cost=allow_missing_cost,
                    )
                    if cogs_entry is not None:
                        stats.processed += 1
                        self.stdout.write(f"  OK order {order.slug}: repaired COGS")
                    else:
                        stats.skip("no COGS to post")
                        self.stdout.write(f"  SKIP order {order.slug}: no COGS to post")
                continue

            skip_reason = self._order_skip_reason(
                order=order,
                idempotency_key=idempotency_key,
                allow_missing_cost=allow_missing_cost,
            )
            if skip_reason:
                stats.skip(skip_reason)
                self.stdout.write(f"  SKIP order {order.slug}: {skip_reason}")
                continue

            if dry_run:
                stats.dry_run += 1
                self.stdout.write(f"  DRY-RUN order {order.slug}: would backfill order.paid")
                continue

            payload = {
                **_order_paid_payload(order),
                "backfill": True,
                "allow_missing_cost": allow_missing_cost,
            }
            event, created = record_accounting_event(
                tenant=tenant,
                event_type=ORDER_PAID_EVENT_TYPE,
                source_model=ORDER_SOURCE_MODEL,
                source_id=order.pk,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if not created:
                stats.skip("accounting event already exists")
                self.stdout.write(f"  SKIP order {order.slug}: accounting event already exists")
                continue

            processed = process_accounting_event(event=event)
            if processed.status == AccountingEvent.Status.PROCESSED:
                stats.processed += 1
                self.stdout.write(f"  OK order {order.slug}: posted")
            else:
                stats.failed += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"  FAILED order {order.slug}: {processed.error_message or processed.status}"
                    )
                )

    def _order_skip_reason(self, *, order: Order, idempotency_key: str, allow_missing_cost: bool) -> str:
        if AccountingEvent.objects.filter(
            tenant=order.tenant,
            event_type=ORDER_PAID_EVENT_TYPE,
            source_model=ORDER_SOURCE_MODEL,
            source_id=str(order.pk),
            idempotency_key=idempotency_key,
        ).exists():
            return "accounting event already exists"
        if JournalEntry.objects.filter(
            tenant=order.tenant,
            source_model=ORDER_SOURCE_MODEL,
            source_id=str(order.pk),
            idempotency_key=idempotency_key,
        ).exists():
            return "sales journal already exists"

        _cost_details, missing_cost_details = _order_item_cost_details(order)
        if missing_cost_details and not allow_missing_cost:
            return "missing product cost"
        return ""

    def _order_has_sales_without_cogs(self, *, order: Order, idempotency_key: str) -> bool:
        has_sales = JournalEntry.objects.filter(
            tenant=order.tenant,
            source_model=ORDER_SOURCE_MODEL,
            source_id=str(order.pk),
            idempotency_key=idempotency_key,
        ).exists()
        if not has_sales:
            return False
        return not JournalEntry.objects.filter(
            tenant=order.tenant,
            source_model=ORDER_SOURCE_MODEL,
            source_id=str(order.pk),
            idempotency_key=f"{idempotency_key}-cogs",
        ).exists()

    def _backfill_payments(
        self,
        *,
        tenant: Tenant,
        date_from,
        date_to,
        dry_run: bool,
        stats: BackfillStats,
    ) -> None:
        payments = (
            Payment.objects.filter(tenant=tenant, status=Payment.Status.PAID)
            .select_related("tenant", "order")
            .order_by("created_at", "id")
        )
        payments = self._apply_date_filters(payments, date_from=date_from, date_to=date_to)

        for payment in payments.iterator():
            stats.seen += 1
            idempotency_key = f"payment-paid-{payment.pk}"
            skip_reason = self._payment_skip_reason(payment=payment, idempotency_key=idempotency_key)
            if skip_reason:
                stats.skip(skip_reason)
                self.stdout.write(f"  SKIP payment {payment.reference}: {skip_reason}")
                continue

            if dry_run:
                stats.dry_run += 1
                self.stdout.write(f"  DRY-RUN payment {payment.reference}: would backfill payment.paid")
                continue

            event, created = record_accounting_event(
                tenant=tenant,
                event_type=PAYMENT_PAID_EVENT_TYPE,
                source_model=PAYMENT_SOURCE_MODEL,
                source_id=payment.pk,
                idempotency_key=idempotency_key,
                payload={**_payment_paid_payload(payment), "backfill": True},
            )
            if not created:
                stats.skip("accounting event already exists")
                self.stdout.write(f"  SKIP payment {payment.reference}: accounting event already exists")
                continue

            processed = process_accounting_event(event=event)
            if processed.status == AccountingEvent.Status.PROCESSED:
                stats.processed += 1
                self.stdout.write(f"  OK payment {payment.reference}: processed")
            else:
                stats.failed += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"  FAILED payment {payment.reference}: {processed.error_message or processed.status}"
                    )
                )

    def _payment_skip_reason(self, *, payment: Payment, idempotency_key: str) -> str:
        if not payment.order_id:
            return "payment has no linked order"
        if AccountingEvent.objects.filter(
            tenant=payment.tenant,
            event_type=PAYMENT_PAID_EVENT_TYPE,
            source_model=PAYMENT_SOURCE_MODEL,
            source_id=str(payment.pk),
            idempotency_key=idempotency_key,
        ).exists():
            return "accounting event already exists"
        if JournalEntry.objects.filter(
            tenant=payment.tenant,
            source_model=PAYMENT_SOURCE_MODEL,
            source_id=str(payment.pk),
            idempotency_key=idempotency_key,
        ).exists():
            return "payment journal already exists"
        return ""

    def _write_summary(self, label: str, stats: BackfillStats) -> None:
        self.stdout.write(
            self.style.SUCCESS(
                f"{label}: seen={stats.seen}, processed={stats.processed}, "
                f"dry_run={stats.dry_run}, skipped={stats.skipped}, failed={stats.failed}"
            )
        )
        for reason, count in sorted(stats.skip_reasons.items()):
            self.stdout.write(f"{label} skipped: {reason} ({count})")
