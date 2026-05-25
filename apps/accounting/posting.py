from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Account, AccountingEvent, AccountingSettings, InventoryMovement, JournalEntry, JournalLine, ZERO
from .services import (
    record_accounting_event,
    register_accounting_event_handler,
    process_accounting_event,
    seed_default_accounts_for_tenant,
)


ORDER_PAID_EVENT_TYPE = "order.paid"
PAYMENT_PAID_EVENT_TYPE = "payment.paid"
REFUND_COMPLETED_EVENT_TYPE = "refund.completed"
ORDER_SOURCE_MODEL = "orders.Order"
PAYMENT_SOURCE_MODEL = "payments.Payment"
REFUND_SOURCE_MODEL = "accounting.Refund"
ORDER_ACCOUNTING_STATUSES = {"PAID", "SHIPPED", "DELIVERED"}
REVENUE_RECOGNITION_PAYMENT_SUCCESS = "payment_success"
REVENUE_RECOGNITION_DELIVERY = "delivery"

ACCOUNT_CASH = "1000"
ACCOUNT_BANK = "1010"
ACCOUNT_MOBILE_MONEY = "1020"
ACCOUNT_RECEIVABLE = "1100"
ACCOUNT_VAT_PAYABLE = "2100"
ACCOUNT_SALES_REVENUE = "4000"
ACCOUNT_DELIVERY_INCOME = "4010"
ACCOUNT_DISCOUNTS = "4090"
ACCOUNT_SALES_RETURNS = "4100"
ACCOUNT_INVENTORY = "1200"
ACCOUNT_COGS = "5000"


def _money(value) -> Decimal:
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"Invalid money value: {value}") from exc


def _account_for_code(*, tenant, code: str) -> Account:
    return Account.objects.get(tenant=tenant, code=code, is_active=True)


def _latest_payment_for_order(order):
    from apps.payments.models import Payment

    paid_payment = (
        Payment.objects.filter(order=order, tenant=order.tenant, status=Payment.Status.PAID)
        .order_by("-paid_at", "-created_at", "-id")
        .first()
    )
    if paid_payment is not None:
        return paid_payment
    return (
        Payment.objects.filter(order=order, tenant=order.tenant)
        .order_by("-created_at", "-id")
        .first()
    )


def _order_has_paid_payment(order) -> bool:
    from apps.payments.models import Payment

    return Payment.objects.filter(
        order=order,
        tenant=order.tenant,
        status=Payment.Status.PAID,
    ).exists()


def _settlement_account_code(payment) -> str:
    from apps.payments.models import Payment

    if payment is None:
        return ACCOUNT_RECEIVABLE

    if payment.provider == Payment.Provider.CASH:
        return ACCOUNT_CASH

    if payment.status != Payment.Status.PAID:
        return ACCOUNT_RECEIVABLE

    if payment.provider == Payment.Provider.MTN:
        return ACCOUNT_MOBILE_MONEY

    if payment.provider in {
        Payment.Provider.CARD,
        Payment.Provider.STRIPE,
        Payment.Provider.PAYSTACK,
        Payment.Provider.FLUTTERWAVE,
    }:
        return ACCOUNT_BANK

    return ACCOUNT_RECEIVABLE


def _payment_account_code(payment) -> str:
    from apps.payments.models import Payment

    if payment.provider == Payment.Provider.CASH:
        return ACCOUNT_CASH

    if payment.provider == Payment.Provider.MTN:
        return ACCOUNT_MOBILE_MONEY

    if payment.provider in {
        Payment.Provider.CARD,
        Payment.Provider.STRIPE,
        Payment.Provider.PAYSTACK,
        Payment.Provider.FLUTTERWAVE,
    }:
        return ACCOUNT_BANK

    return ACCOUNT_BANK


def _missing_cost_policy(*, tenant) -> str:
    settings, _created = AccountingSettings.objects.get_or_create(
        tenant=tenant,
        defaults={"base_currency": getattr(tenant, "currency", "UGX") or "UGX"},
    )
    policy = (settings.metadata or {}).get("missing_cost_policy", "attention")
    return str(policy).strip().lower()


def revenue_recognition_policy(*, tenant) -> str:
    settings, _created = AccountingSettings.objects.get_or_create(
        tenant=tenant,
        defaults={"base_currency": getattr(tenant, "currency", "UGX") or "UGX"},
    )
    policy = (settings.metadata or {}).get(
        "revenue_recognition_policy",
        REVENUE_RECOGNITION_PAYMENT_SUCCESS,
    )
    normalized = str(policy or "").strip().lower().replace("-", "_")
    aliases = {
        "payment": REVENUE_RECOGNITION_PAYMENT_SUCCESS,
        "paid": REVENUE_RECOGNITION_PAYMENT_SUCCESS,
        "payment_success": REVENUE_RECOGNITION_PAYMENT_SUCCESS,
        "delivery": REVENUE_RECOGNITION_DELIVERY,
        "delivered": REVENUE_RECOGNITION_DELIVERY,
    }
    return aliases.get(normalized, REVENUE_RECOGNITION_PAYMENT_SUCCESS)


def should_post_order_revenue_on_payment(order) -> bool:
    return revenue_recognition_policy(tenant=order.tenant) == REVENUE_RECOGNITION_PAYMENT_SUCCESS


def should_post_order_revenue_on_delivery(order) -> bool:
    return revenue_recognition_policy(tenant=order.tenant) == REVENUE_RECOGNITION_DELIVERY


def order_has_paid_payment(order) -> bool:
    from apps.payments.models import Payment

    return order.payments.filter(status=Payment.Status.PAID).exists()


def _order_item_unit_cost(item) -> Decimal:
    snapshot = _money(getattr(item, "cost_price_snapshot", ZERO))
    if snapshot > ZERO:
        return snapshot
    variant_cost = _money(getattr(item.variant, "unit_cost", ZERO))
    if variant_cost > ZERO:
        return variant_cost
    return _money(getattr(item.product, "cost_price", ZERO))


def _order_item_cost_details(order) -> tuple[list[dict], list[dict]]:
    details = []
    missing = []
    for item in order.items.select_related("variant", "product").order_by("id"):
        unit_cost = _order_item_unit_cost(item)
        detail = {
            "order_item_id": item.pk,
            "product_id": item.product_id,
            "product_title": item.product_title,
            "variant_id": item.variant_id,
            "variant_sku": item.variant_sku,
            "quantity": item.quantity,
            "unit_cost": str(unit_cost),
            "total_cost": str(unit_cost * Decimal(item.quantity)),
            "cost_source": (
                "order_item_snapshot"
                if _money(getattr(item, "cost_price_snapshot", ZERO)) > ZERO
                else "variant_current"
                if _money(getattr(item.variant, "unit_cost", ZERO)) > ZERO
                else "product_current"
            ),
        }
        if unit_cost <= ZERO:
            missing.append(detail)
        else:
            details.append(detail)
    return details, missing


def _order_paid_payload(order) -> dict:
    payment = _latest_payment_for_order(order)
    return {
        "order_id": order.pk,
        "order_slug": order.slug,
        "items_subtotal": str(order.items_subtotal or ZERO),
        "discount_amount": str(order.discount_amount or ZERO),
        "shipping_fee": str(order.shipping_fee or ZERO),
        "tax_amount": str(getattr(order, "tax_amount", ZERO) or ZERO),
        "total_price": str(order.total_price or ZERO),
        "payment_id": getattr(payment, "pk", None),
        "payment_reference": getattr(payment, "reference", ""),
        "payment_provider": getattr(payment, "provider", ""),
        "payment_status": getattr(payment, "status", ""),
    }


def queue_order_paid_accounting_event(order) -> None:
    """Record and process the order-paid accounting event after commit."""

    payload = _order_paid_payload(order)
    idempotency_key = f"order-paid-{order.pk}"

    def _record_and_process() -> None:
        event, _created = record_accounting_event(
            tenant=order.tenant,
            event_type=ORDER_PAID_EVENT_TYPE,
            source_model=ORDER_SOURCE_MODEL,
            source_id=order.pk,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        process_accounting_event(event=event)

    transaction.on_commit(_record_and_process)


def _payment_paid_payload(payment) -> dict:
    return {
        "payment_id": payment.pk,
        "payment_reference": payment.reference,
        "payment_provider": payment.provider,
        "payment_status": payment.status,
        "amount": str(payment.amount or ZERO),
        "currency": payment.currency,
        "order_id": payment.order_id,
        "order_slug": getattr(payment.order, "slug", ""),
    }


def queue_payment_paid_accounting_event(payment) -> None:
    """Record and process the payment-paid accounting event after commit."""

    tenant = payment.tenant or getattr(payment.order, "tenant", None)
    if tenant is None:
        return

    payload = _payment_paid_payload(payment)
    idempotency_key = f"payment-paid-{payment.pk}"

    def _record_and_process() -> None:
        event, _created = record_accounting_event(
            tenant=tenant,
            event_type=PAYMENT_PAID_EVENT_TYPE,
            source_model=PAYMENT_SOURCE_MODEL,
            source_id=payment.pk,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        process_accounting_event(event=event)

    transaction.on_commit(_record_and_process)


def _refund_completed_payload(refund) -> dict:
    return {
        "refund_id": refund.pk,
        "order_id": refund.order_id,
        "order_slug": getattr(refund.order, "slug", ""),
        "payment_id": refund.payment_id,
        "payment_reference": getattr(refund.payment, "reference", ""),
        "amount": str(refund.amount or ZERO),
        "tax_amount": str(refund.tax_amount or ZERO),
        "status": refund.status,
    }


def queue_refund_completed_accounting_event(refund) -> None:
    payload = _refund_completed_payload(refund)
    idempotency_key = f"refund-completed-{refund.pk}"

    def _record_and_process() -> None:
        event, _created = record_accounting_event(
            tenant=refund.tenant,
            event_type=REFUND_COMPLETED_EVENT_TYPE,
            source_model=REFUND_SOURCE_MODEL,
            source_id=refund.pk,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        process_accounting_event(event=event)

    transaction.on_commit(_record_and_process)


def _prepare_order_cogs(*, order, event: AccountingEvent, allow_missing_cost: bool = False) -> tuple[Account, Account, list[dict]]:
    cogs_details, missing_cost_details = _order_item_cost_details(order)
    if missing_cost_details:
        if _missing_cost_policy(tenant=event.tenant) == "attention" or allow_missing_cost or (event.payload or {}).get("allow_missing_cost"):
            event.payload = {
                **(event.payload or {}),
                "requires_attention": True,
                "attention_reason": "Missing product cost for one or more order items.",
                "missing_cost_items": missing_cost_details,
            }
            event.save(update_fields=["payload", "updated_at"])
        else:
            raise ValidationError(
                {
                    "cost": "Missing product cost for one or more order items.",
                    "items": missing_cost_details,
                }
            )

    inventory = _account_for_code(tenant=event.tenant, code=ACCOUNT_INVENTORY)
    cogs = _account_for_code(tenant=event.tenant, code=ACCOUNT_COGS)
    return cogs, inventory, cogs_details


@transaction.atomic
def ensure_order_cogs_posted(*, order, idempotency_key: str | None = None, allow_missing_cost: bool = False) -> JournalEntry | None:
    seed_default_accounts_for_tenant(tenant=order.tenant)
    event, _created = record_accounting_event(
        tenant=order.tenant,
        event_type=ORDER_PAID_EVENT_TYPE,
        source_model=ORDER_SOURCE_MODEL,
        source_id=order.pk,
        idempotency_key=idempotency_key or f"order-paid-{order.pk}",
        payload={**_order_paid_payload(order), "cogs_repair": True, "allow_missing_cost": allow_missing_cost},
    )
    cogs, inventory, cogs_details = _prepare_order_cogs(
        order=order,
        event=event,
        allow_missing_cost=allow_missing_cost,
    )
    return _post_cogs_entry(
        event=event,
        order=order,
        cogs_account=cogs,
        inventory_account=inventory,
        cogs_details=cogs_details,
    )


@transaction.atomic
def post_order_paid_event(event: AccountingEvent) -> JournalEntry:
    from apps.orders.models import Order

    order = (
        Order.objects.select_for_update()
        .prefetch_related("payments")
        .get(pk=event.source_id, tenant=event.tenant)
    )

    if order.status not in ORDER_ACCOUNTING_STATUSES and not _order_has_paid_payment(order):
        raise ValidationError("Order must have a paid payment, or be shipped/delivered, before sales accounting can be posted.")

    seed_default_accounts_for_tenant(tenant=event.tenant)
    cogs, inventory, cogs_details = _prepare_order_cogs(order=order, event=event)

    existing = JournalEntry.objects.filter(
        tenant=event.tenant,
        source_model=ORDER_SOURCE_MODEL,
        source_id=str(order.pk),
        idempotency_key=event.idempotency_key,
    ).first()
    if existing is not None:
        _post_cogs_entry(
            event=event,
            order=order,
            cogs_account=cogs,
            inventory_account=inventory,
            cogs_details=cogs_details,
        )
        return existing

    payment = _latest_payment_for_order(order)
    settlement_account = _account_for_code(
        tenant=event.tenant,
        code=_settlement_account_code(payment),
    )
    sales_revenue = _account_for_code(tenant=event.tenant, code=ACCOUNT_SALES_REVENUE)
    delivery_income = _account_for_code(tenant=event.tenant, code=ACCOUNT_DELIVERY_INCOME)
    discounts = _account_for_code(tenant=event.tenant, code=ACCOUNT_DISCOUNTS)
    vat_payable = _account_for_code(tenant=event.tenant, code=ACCOUNT_VAT_PAYABLE)
    items_subtotal = _money(order.items_subtotal)
    discount_amount = _money(order.discount_amount)
    shipping_fee = _money(order.shipping_fee)
    tax_amount = _money(getattr(order, "tax_amount", ZERO))
    total_price = _money(order.total_price)

    expected_total = items_subtotal + shipping_fee + tax_amount - discount_amount
    if expected_total != total_price:
        raise ValidationError("Order accounting amounts do not reconcile to order total.")

    entry = JournalEntry.objects.create(
        tenant=event.tenant,
        memo=f"Sales posting for order {order.slug}",
        source_model=ORDER_SOURCE_MODEL,
        source_id=str(order.pk),
        idempotency_key=event.idempotency_key,
        metadata={
            "event_id": event.pk,
            "order_slug": order.slug,
            "payment_reference": getattr(payment, "reference", ""),
            "payment_provider": getattr(payment, "provider", ""),
        },
    )

    JournalLine.objects.create(
        tenant=event.tenant,
        journal_entry=entry,
        account=settlement_account,
        description=f"Order {order.slug} settlement",
        debit=total_price,
    )
    if items_subtotal > ZERO:
        JournalLine.objects.create(
            tenant=event.tenant,
            journal_entry=entry,
            account=sales_revenue,
            description=f"Order {order.slug} product sales",
            credit=items_subtotal,
        )
    if shipping_fee > ZERO:
        JournalLine.objects.create(
            tenant=event.tenant,
            journal_entry=entry,
            account=delivery_income,
            description=f"Order {order.slug} delivery income",
            credit=shipping_fee,
        )
    if tax_amount > ZERO:
        JournalLine.objects.create(
            tenant=event.tenant,
            journal_entry=entry,
            account=vat_payable,
            description=f"Order {order.slug} VAT payable",
            credit=tax_amount,
        )
    if discount_amount > ZERO:
        JournalLine.objects.create(
            tenant=event.tenant,
            journal_entry=entry,
            account=discounts,
            description=f"Order {order.slug} discount",
            debit=discount_amount,
        )

    posted_sales_entry = entry.post()

    _post_cogs_entry(
        event=event,
        order=order,
        cogs_account=cogs,
        inventory_account=inventory,
        cogs_details=cogs_details,
    )

    return posted_sales_entry


def _post_cogs_entry(
    *,
    event: AccountingEvent,
    order,
    cogs_account: Account,
    inventory_account: Account,
    cogs_details: list[dict],
) -> JournalEntry | None:
    if not cogs_details:
        return None

    idempotency_key = f"{event.idempotency_key}-cogs"
    existing = JournalEntry.objects.filter(
        tenant=event.tenant,
        source_model=ORDER_SOURCE_MODEL,
        source_id=str(order.pk),
        idempotency_key=idempotency_key,
    ).first()
    if existing is not None:
        return existing

    total_cost = sum((_money(item["total_cost"]) for item in cogs_details), ZERO)
    if total_cost <= ZERO:
        return None

    entry = JournalEntry.objects.create(
        tenant=event.tenant,
        memo=f"COGS posting for order {order.slug}",
        source_model=ORDER_SOURCE_MODEL,
        source_id=str(order.pk),
        idempotency_key=idempotency_key,
        metadata={
            "event_id": event.pk,
            "order_slug": order.slug,
            "posting_type": "COGS",
            "items": cogs_details,
        },
    )
    JournalLine.objects.create(
        tenant=event.tenant,
        journal_entry=entry,
        account=cogs_account,
        description=f"Order {order.slug} cost of goods sold",
        debit=total_cost,
        metadata={"posting_type": "COGS", "items": cogs_details},
    )
    JournalLine.objects.create(
        tenant=event.tenant,
        journal_entry=entry,
        account=inventory_account,
        description=f"Order {order.slug} inventory relief",
        credit=total_cost,
        metadata={"posting_type": "INVENTORY_RELIEF", "items": cogs_details},
    )
    for item in cogs_details:
        if not InventoryMovement.objects.filter(
            tenant=event.tenant,
            source_model="orders.OrderItem",
            source_id=str(item["order_item_id"]),
            movement_type=InventoryMovement.MovementType.SALE,
        ).exists():
            InventoryMovement.objects.create(
                tenant=event.tenant,
                variant_id=item["variant_id"],
                movement_type=InventoryMovement.MovementType.SALE,
                quantity=item["quantity"],
                unit_cost=_money(item["unit_cost"]),
                total_cost=_money(item["total_cost"]),
                source_model="orders.OrderItem",
                source_id=str(item["order_item_id"]),
                note=f"Sold on order {order.slug}",
                metadata={
                    "order_id": order.pk,
                    "order_slug": order.slug,
                    "journal_entry_id": entry.pk,
                    "cost_source": item.get("cost_source", ""),
                },
            )
    return entry.post()


@transaction.atomic
def post_payment_paid_event(event: AccountingEvent) -> JournalEntry | None:
    from apps.payments.models import Payment

    payment = (
        Payment.objects.select_for_update()
        .select_related("order", "tenant")
        .get(pk=event.source_id, tenant=event.tenant)
    )
    if payment.status != Payment.Status.PAID:
        raise ValidationError("Payment must be PAID before payment accounting can be posted.")

    existing = JournalEntry.objects.filter(
        tenant=event.tenant,
        source_model=PAYMENT_SOURCE_MODEL,
        source_id=str(payment.pk),
        idempotency_key=event.idempotency_key,
    ).first()
    if existing is not None:
        return existing

    seed_default_accounts_for_tenant(tenant=event.tenant)

    receivable = _account_for_code(tenant=event.tenant, code=ACCOUNT_RECEIVABLE)
    receipt_account = _account_for_code(
        tenant=event.tenant,
        code=_payment_account_code(payment),
    )

    if not payment.order_id:
        event.payload = {
            **(event.payload or {}),
            "ignored": True,
            "ignored_reason": "Payment has no linked order to clear from accounts receivable.",
        }
        event.save(update_fields=["payload", "updated_at"])
        return None

    ar_sales_line = JournalLine.objects.filter(
        tenant=event.tenant,
        journal_entry__tenant=event.tenant,
        journal_entry__source_model=ORDER_SOURCE_MODEL,
        journal_entry__source_id=str(payment.order_id),
        journal_entry__idempotency_key=f"order-paid-{payment.order_id}",
        journal_entry__status=JournalEntry.Status.POSTED,
        account=receivable,
        debit__gt=ZERO,
    ).select_related("journal_entry").first()

    if ar_sales_line is None:
        event.payload = {
            **(event.payload or {}),
            "ignored": True,
            "ignored_reason": "Linked order sale did not post to accounts receivable.",
        }
        event.save(update_fields=["payload", "updated_at"])
        return None

    amount = _money(payment.amount)
    if amount != _money(ar_sales_line.debit):
        raise ValidationError("Payment amount does not match the linked accounts receivable balance.")

    entry = JournalEntry.objects.create(
        tenant=event.tenant,
        memo=f"Payment receipt for {payment.reference}",
        source_model=PAYMENT_SOURCE_MODEL,
        source_id=str(payment.pk),
        idempotency_key=event.idempotency_key,
        metadata={
            "event_id": event.pk,
            "payment_reference": payment.reference,
            "payment_provider": payment.provider,
            "order_id": payment.order_id,
            "order_slug": getattr(payment.order, "slug", ""),
            "clears_journal_entry_id": ar_sales_line.journal_entry_id,
        },
    )
    JournalLine.objects.create(
        tenant=event.tenant,
        journal_entry=entry,
        account=receipt_account,
        description=f"Payment {payment.reference} receipt",
        debit=amount,
        metadata={
            "payment_id": payment.pk,
            "payment_reference": payment.reference,
            "payment_provider": payment.provider,
            "order_id": payment.order_id,
        },
    )
    JournalLine.objects.create(
        tenant=event.tenant,
        journal_entry=entry,
        account=receivable,
        description=f"Payment {payment.reference} clears AR",
        credit=amount,
        metadata={
            "payment_id": payment.pk,
            "payment_reference": payment.reference,
            "payment_provider": payment.provider,
            "order_id": payment.order_id,
            "clears_journal_line_id": ar_sales_line.pk,
        },
    )
    return entry.post()


def _refund_cost_details(refund) -> list[dict]:
    details = []
    for line in refund.lines.select_related("order_item", "order_item__variant").filter(return_to_stock=True).order_by("id"):
        unit_cost = _order_item_unit_cost(line.order_item)
        if unit_cost <= ZERO:
            continue
        details.append(
            {
                "refund_line_id": line.pk,
                "order_item_id": line.order_item_id,
                "product_title": line.order_item.product_title,
                "variant_id": line.order_item.variant_id,
                "variant_sku": line.order_item.variant_sku,
                "quantity": line.quantity,
                "unit_cost": str(unit_cost),
                "total_cost": str(unit_cost * Decimal(line.quantity)),
                "cost_source": (
                    "order_item_snapshot"
                    if _money(getattr(line.order_item, "cost_price_snapshot", ZERO)) > ZERO
                    else "variant_current"
                    if _money(getattr(line.order_item.variant, "unit_cost", ZERO)) > ZERO
                    else "product_current"
                ),
            }
        )
    return details


def _refund_settlement_account(refund) -> Account:
    if refund.payment_id:
        return _account_for_code(
            tenant=refund.tenant,
            code=_payment_account_code(refund.payment),
        )

    original_settlement = (
        JournalLine.objects.filter(
            tenant=refund.tenant,
            journal_entry__source_model=ORDER_SOURCE_MODEL,
            journal_entry__source_id=str(refund.order_id),
            journal_entry__idempotency_key=f"order-paid-{refund.order_id}",
            journal_entry__status=JournalEntry.Status.POSTED,
            debit__gt=ZERO,
            account__code__in=[ACCOUNT_CASH, ACCOUNT_BANK, ACCOUNT_MOBILE_MONEY, ACCOUNT_RECEIVABLE],
        )
        .select_related("account")
        .first()
    )
    if original_settlement is not None:
        return original_settlement.account
    return _account_for_code(tenant=refund.tenant, code=ACCOUNT_RECEIVABLE)


@transaction.atomic
def post_refund_completed_event(event: AccountingEvent) -> JournalEntry:
    from .models import Refund

    refund = (
        Refund.objects.select_for_update()
        .select_related("tenant", "order", "payment")
        .prefetch_related("lines", "lines__order_item", "lines__order_item__variant")
        .get(pk=event.source_id, tenant=event.tenant)
    )
    if refund.status != Refund.Status.COMPLETED:
        raise ValidationError("Refund must be completed before refund accounting can be posted.")

    existing = JournalEntry.objects.filter(
        tenant=event.tenant,
        source_model=REFUND_SOURCE_MODEL,
        source_id=str(refund.pk),
        idempotency_key=event.idempotency_key,
    ).first()
    if existing is not None:
        return existing

    seed_default_accounts_for_tenant(tenant=event.tenant)

    sales_returns = _account_for_code(tenant=event.tenant, code=ACCOUNT_SALES_RETURNS)
    vat_payable = _account_for_code(tenant=event.tenant, code=ACCOUNT_VAT_PAYABLE)
    inventory = _account_for_code(tenant=event.tenant, code=ACCOUNT_INVENTORY)
    cogs = _account_for_code(tenant=event.tenant, code=ACCOUNT_COGS)
    settlement_account = _refund_settlement_account(refund)

    refund_amount = _money(refund.amount)
    tax_amount = _money(refund.tax_amount)
    net_refund_amount = refund_amount - tax_amount
    if net_refund_amount < ZERO:
        raise ValidationError("Refund tax amount cannot exceed refund amount.")

    cost_details = _refund_cost_details(refund)
    returned_cost = sum((_money(item["total_cost"]) for item in cost_details), ZERO)

    entry = JournalEntry.objects.create(
        tenant=event.tenant,
        memo=f"Refund posting for order {refund.order.slug}",
        source_model=REFUND_SOURCE_MODEL,
        source_id=str(refund.pk),
        idempotency_key=event.idempotency_key,
        metadata={
            "event_id": event.pk,
            "refund_id": refund.pk,
            "order_id": refund.order_id,
            "order_slug": refund.order.slug,
            "payment_reference": getattr(refund.payment, "reference", ""),
            "cost_items": cost_details,
        },
    )

    if net_refund_amount > ZERO:
        JournalLine.objects.create(
            tenant=event.tenant,
            journal_entry=entry,
            account=sales_returns,
            description=f"Refund {refund.pk} sales return",
            debit=net_refund_amount,
            metadata={"refund_id": refund.pk, "order_id": refund.order_id},
        )
    if tax_amount > ZERO:
        JournalLine.objects.create(
            tenant=event.tenant,
            journal_entry=entry,
            account=vat_payable,
            description=f"Refund {refund.pk} VAT reversal",
            debit=tax_amount,
            metadata={"refund_id": refund.pk, "order_id": refund.order_id},
        )
    if returned_cost > ZERO:
        JournalLine.objects.create(
            tenant=event.tenant,
            journal_entry=entry,
            account=inventory,
            description=f"Refund {refund.pk} inventory return",
            debit=returned_cost,
            metadata={"refund_id": refund.pk, "items": cost_details},
        )
        JournalLine.objects.create(
            tenant=event.tenant,
            journal_entry=entry,
            account=cogs,
            description=f"Refund {refund.pk} COGS reversal",
            credit=returned_cost,
            metadata={"refund_id": refund.pk, "items": cost_details},
        )

    JournalLine.objects.create(
        tenant=event.tenant,
        journal_entry=entry,
        account=settlement_account,
        description=f"Refund {refund.pk} cash out",
        credit=refund_amount,
        metadata={
            "refund_id": refund.pk,
            "order_id": refund.order_id,
            "payment_id": refund.payment_id,
        },
    )

    return entry.post()


def register_posting_handlers() -> None:
    register_accounting_event_handler(ORDER_PAID_EVENT_TYPE, post_order_paid_event)
    register_accounting_event_handler(PAYMENT_PAID_EVENT_TYPE, post_payment_paid_event)
    register_accounting_event_handler(REFUND_COMPLETED_EVENT_TYPE, post_refund_completed_event)
