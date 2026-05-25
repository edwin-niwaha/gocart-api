from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from django.db import transaction
from django.utils.text import slugify
from rest_framework.exceptions import ValidationError

from apps.common.views import create_audit_log
from apps.products.models import ProductVariant
from apps.tenants.models import Tenant
from .models import Order, OrderItem, OrderStatusEvent

ALLOWED_STATUS_TRANSITIONS = {
    Order.Status.PENDING: {Order.Status.AWAITING_PAYMENT, Order.Status.CONFIRMED, Order.Status.PROCESSING, Order.Status.DELIVERED, Order.Status.CANCELLED},
    Order.Status.AWAITING_PAYMENT: {Order.Status.PENDING, Order.Status.CONFIRMED, Order.Status.PAID, Order.Status.DELIVERED, Order.Status.CANCELLED},
    Order.Status.CONFIRMED: {Order.Status.PROCESSING, Order.Status.SHIPPED, Order.Status.DELIVERED, Order.Status.CANCELLED},
    Order.Status.PROCESSING: {Order.Status.PAID, Order.Status.SHIPPED, Order.Status.DELIVERED, Order.Status.CANCELLED},
    # PAID is a legacy order status. Keep it accepted for older API clients; new UI should use Payment.status.
    Order.Status.PAID: {Order.Status.CONFIRMED, Order.Status.PROCESSING, Order.Status.SHIPPED, Order.Status.DELIVERED, Order.Status.REFUNDED},
    Order.Status.SHIPPED: {Order.Status.DELIVERED, Order.Status.REFUNDED},
    Order.Status.DELIVERED: {Order.Status.REFUNDED},
    Order.Status.CANCELLED: set(),
    Order.Status.REFUNDED: set(),
}


def generate_order_slug(*, user=None, prefix: str = "order") -> str:
    owner_token = getattr(user, "id", None) or "guest"
    return f"{slugify(prefix)}-{owner_token}-{uuid4().hex[:8]}"


def _populate_order_snapshot(*, user=None, validated_data: dict) -> None:
    address = validated_data.get("address")
    pickup_station = validated_data.get("pickup_station")
    customer_name = (validated_data.get("customer_name") or "").strip()
    customer_email = (validated_data.get("customer_email") or "").strip().lower()
    customer_phone = (validated_data.get("customer_phone") or "").strip()

    if user is not None:
        default_name = user.get_full_name().strip() or user.email or user.username
        if not customer_name:
            customer_name = default_name
        if not customer_email:
            customer_email = user.email

    validated_data["customer_name"] = customer_name
    validated_data["customer_email"] = customer_email
    validated_data["customer_phone"] = customer_phone or getattr(address, "phone_number", "")
    validated_data["address_street_name"] = (
        validated_data.get("address_street_name")
        or getattr(address, "street_name", "")
    )
    validated_data["address_city"] = (
        validated_data.get("address_city")
        or getattr(address, "city", "")
    )
    validated_data["address_area"] = (
        validated_data.get("address_area")
        or getattr(address, "area", "")
    )
    validated_data["address_region"] = (
        validated_data.get("address_region")
        or getattr(address, "region", "")
    )
    validated_data["address_additional_information"] = (
        validated_data.get("address_additional_information")
        or getattr(address, "additional_information", "")
    )
    validated_data["pickup_station_name"] = (
        validated_data.get("pickup_station_name")
        or getattr(pickup_station, "name", "")
    )
    validated_data["pickup_station_city"] = (
        validated_data.get("pickup_station_city")
        or getattr(pickup_station, "city", "")
    )
    validated_data["pickup_station_area"] = (
        validated_data.get("pickup_station_area")
        or getattr(pickup_station, "area", "")
    )
    validated_data["pickup_station_address"] = (
        validated_data.get("pickup_station_address")
        or getattr(pickup_station, "address", "")
    )
    validated_data["pickup_station_phone"] = (
        validated_data.get("pickup_station_phone")
        or getattr(pickup_station, "phone", "")
    )
    validated_data["pickup_station_opening_hours"] = (
        validated_data.get("pickup_station_opening_hours")
        or getattr(pickup_station, "opening_hours", "")
    )


@transaction.atomic
def create_order(*, user=None, tenant: Tenant, **validated_data) -> Order:
    if not validated_data.get("slug"):
        validated_data["slug"] = generate_order_slug(user=user)
    _populate_order_snapshot(user=user, validated_data=validated_data)
    order = Order.objects.create(user=user, tenant=tenant, **validated_data)
    actor = user if getattr(user, "is_authenticated", False) else None
    OrderStatusEvent.objects.create(
        tenant=tenant,
        order=order,
        changed_by=actor,
        from_status="",
        to_status=order.status,
        note="Order created",
    )
    create_audit_log(
        tenant=tenant,
        actor=actor,
        action="order.created",
        summary=f"Order {order.slug} created",
        target=order,
    )
    return order


@transaction.atomic
def add_order_item(
    *,
    order: Order,
    variant: ProductVariant,
    quantity: int,
    unit_price: Decimal | None = None,
) -> OrderItem:
    return OrderItem.objects.create(
        tenant=order.tenant,
        order=order,
        product=variant.product,
        variant=variant,
        quantity=quantity,
        unit_price=unit_price if unit_price is not None else variant.price,
    )


@transaction.atomic
def update_order_item(*, item: OrderItem, **validated_data) -> OrderItem:
    variant = validated_data.get("variant")
    for attr, value in validated_data.items():
        setattr(item, attr, value)
    if variant is not None:
        item.product = variant.product
        item.tenant = variant.tenant
        if "unit_price" not in validated_data:
            item.unit_price = variant.price
    item.save()
    item.order.recalculate_total_price()
    return item


@transaction.atomic
def remove_order_item(*, item: OrderItem) -> None:
    order = item.order
    item.delete()
    order.recalculate_total_price()


@transaction.atomic
def transition_order_status(*, order: Order, new_status: str, changed_by=None, note: str = "") -> Order:
    current_status = order.status
    if new_status == current_status:
        return order
    allowed = ALLOWED_STATUS_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise ValidationError({"status": f"Cannot transition order from {current_status} to {new_status}."})
    order.status = new_status
    order.save(update_fields=["status", "updated_at"])
    OrderStatusEvent.objects.create(
        tenant=order.tenant,
        order=order,
        changed_by=changed_by,
        from_status=current_status,
        to_status=new_status,
        note=note,
    )
    create_audit_log(
        tenant=order.tenant,
        actor=changed_by,
        action="order.status_changed",
        summary=f"Order {order.slug} moved to {new_status}",
        target=order,
        metadata={"from": current_status, "to": new_status, "note": note},
    )
    if new_status == Order.Status.PAID:
        from apps.accounting.posting import (
            queue_order_paid_accounting_event,
            should_post_order_revenue_on_payment,
        )

        if should_post_order_revenue_on_payment(order):
            queue_order_paid_accounting_event(order)
    elif new_status == Order.Status.DELIVERED:
        from apps.accounting.posting import (
            queue_order_paid_accounting_event,
            order_has_paid_payment,
            should_post_order_revenue_on_delivery,
        )

        if should_post_order_revenue_on_delivery(order) and order_has_paid_payment(order):
            queue_order_paid_accounting_event(order)
    return order
