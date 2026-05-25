from __future__ import annotations

import inspect
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from apps.addresses.models import CustomerAddress
from apps.common.models import TimeStampedModel
from apps.products.models import Product, ProductVariant
from apps.tenants.models import Tenant


_CHECK_CONSTRAINT_USES_CONDITION = (
    "condition" in inspect.signature(models.CheckConstraint).parameters
)


def build_check_constraint(*, predicate, name: str) -> models.CheckConstraint:
    kwargs = {"name": name}
    kwargs["condition" if _CHECK_CONSTRAINT_USES_CONDITION else "check"] = predicate
    return models.CheckConstraint(**kwargs)


class Order(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        AWAITING_PAYMENT = "AWAITING_PAYMENT", "Awaiting payment"
        CONFIRMED = "CONFIRMED", "Confirmed"
        PROCESSING = "PROCESSING", "Processing"
        # Legacy compatibility only. New payment events update Payment.status.
        PAID = "PAID", "Paid"
        SHIPPED = "SHIPPED", "Shipped"
        DELIVERED = "DELIVERED", "Delivered"
        CANCELLED = "CANCELLED", "Cancelled"
        REFUNDED = "REFUNDED", "Refunded"

    class DeliveryOption(models.TextChoices):
        HOME_DELIVERY = "HOME_DELIVERY", "Home delivery"
        PICKUP_STATION = "PICKUP_STATION", "Pickup station"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="orders",
        null=True,
        blank=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="orders",
        null=True,
        blank=True,
    )
    address = models.ForeignKey(
        CustomerAddress,
        on_delete=models.PROTECT,
        related_name="orders",
        null=True,
        blank=True,
    )
    pickup_station = models.ForeignKey(
        "shipping.PickupStation",
        on_delete=models.PROTECT,
        related_name="orders",
        null=True,
        blank=True,
    )
    pickup_station_name = models.CharField(max_length=255, blank=True)
    pickup_station_city = models.CharField(max_length=100, blank=True)
    pickup_station_area = models.CharField(max_length=100, blank=True)
    pickup_station_address = models.TextField(blank=True)
    pickup_station_phone = models.CharField(max_length=50, blank=True)
    pickup_station_opening_hours = models.CharField(max_length=255, blank=True)
    guest_session_key = models.CharField(
        max_length=40,
        null=True,
        blank=True,
        db_index=True,
    )
    customer_name = models.CharField(max_length=255, blank=True)
    customer_email = models.EmailField(blank=True)
    customer_phone = models.CharField(max_length=20, blank=True)
    address_street_name = models.CharField(max_length=255, blank=True)
    address_city = models.CharField(max_length=100, blank=True)
    address_area = models.CharField(max_length=100, blank=True)
    address_region = models.CharField(
        max_length=30,
        choices=CustomerAddress.Region.choices,
        blank=True,
    )
    address_additional_information = models.TextField(blank=True)
    slug = models.SlugField(max_length=255, unique=True, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    delivery_option = models.CharField(
        max_length=20,
        choices=DeliveryOption.choices,
        default=DeliveryOption.HOME_DELIVERY,
        db_index=True,
    )
    items_subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    shipping_fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    description = models.TextField(blank=True)
    total_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "slug"]),
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["guest_session_key", "status"]),
            models.Index(fields=["customer_email"]),
        ]
        constraints = [
            build_check_constraint(
                predicate=models.Q(user__isnull=False)
                | models.Q(guest_session_key__isnull=False),
                name="order_requires_user_or_guest_session",
            ),
            build_check_constraint(
                predicate=~(
                    models.Q(user__isnull=False)
                    & models.Q(guest_session_key__isnull=False)
                ),
                name="order_owner_is_exclusive",
            ),
        ]

    def __str__(self) -> str:
        return self.slug

    def recalculate_total_price(self) -> Decimal:
        subtotal = sum((item.line_total for item in self.items.all()), Decimal("0.00"))
        self.items_subtotal = subtotal
        self.total_price = (
            max(subtotal - (self.discount_amount or Decimal("0.00")), Decimal("0.00"))
            + (self.shipping_fee or Decimal("0.00"))
        )
        self.save(update_fields=["items_subtotal", "total_price", "updated_at"])
        return subtotal

    @property
    def total_items(self) -> int:
        return sum(item.quantity for item in self.items.all())

    @property
    def contact_email(self) -> str:
        return self.customer_email or getattr(self.user, "email", "")

    @property
    def contact_name(self) -> str:
        if self.customer_name:
            return self.customer_name
        if self.user_id:
            full_name = self.user.get_full_name().strip()
            return full_name or self.user.email or "Customer"
        return self.contact_email or "Customer"

    @property
    def delivery_street_name(self) -> str:
        return self.address_street_name or getattr(self.address, "street_name", "")

    @property
    def delivery_city(self) -> str:
        return self.address_city or getattr(self.address, "city", "")

    @property
    def delivery_area(self) -> str:
        return self.address_area or getattr(self.address, "area", "")

    @property
    def delivery_region(self) -> str:
        return self.address_region or getattr(self.address, "region", "")

    @property
    def delivery_additional_information(self) -> str:
        return self.address_additional_information or getattr(
            self.address,
            "additional_information",
            "",
        )

    @property
    def delivery_pickup_station_name(self) -> str:
        return self.pickup_station_name or getattr(self.pickup_station, "name", "")

    @property
    def delivery_pickup_station_city(self) -> str:
        return self.pickup_station_city or getattr(self.pickup_station, "city", "")

    @property
    def delivery_pickup_station_area(self) -> str:
        return self.pickup_station_area or getattr(self.pickup_station, "area", "")

    @property
    def delivery_pickup_station_address(self) -> str:
        return self.pickup_station_address or getattr(
            self.pickup_station,
            "address",
            "",
        )

    @property
    def delivery_pickup_station_phone(self) -> str:
        return self.pickup_station_phone or getattr(self.pickup_station, "phone", "")

    @property
    def delivery_pickup_station_opening_hours(self) -> str:
        return self.pickup_station_opening_hours or getattr(
            self.pickup_station,
            "opening_hours",
            "",
        )


class OrderItem(TimeStampedModel):
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="order_items",
        null=True,
        blank=True,
    )
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="order_items",
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        related_name="order_items",
    )

    product_title = models.CharField(max_length=255, editable=False)
    product_image = models.URLField(blank=True, null=True, editable=False)
    variant_name = models.CharField(max_length=100, editable=False)
    variant_sku = models.CharField(max_length=100, editable=False)

    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    cost_price_snapshot = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "variant"],
                name="unique_variant_per_order",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "order"]),
            models.Index(fields=["tenant", "product"]),
            models.Index(fields=["tenant", "variant"]),
            models.Index(fields=["variant_sku"]),
        ]

    def __str__(self) -> str:
        return f"{self.order.slug} - {self.product_title} ({self.variant_name})"

    @property
    def line_total(self) -> Decimal:
        return (self.unit_price or Decimal("0.00")) * (self.quantity or 0)

    @property
    def line_cost_total(self) -> Decimal:
        return (self.cost_price_snapshot or Decimal("0.00")) * (self.quantity or 0)

    @property
    def gross_profit(self) -> Decimal:
        return self.line_total - self.line_cost_total

    def _snapshot_unit_cost(self) -> Decimal:
        variant_cost = getattr(self.variant, "unit_cost", Decimal("0.00")) or Decimal("0.00")
        if variant_cost > Decimal("0.00"):
            return variant_cost
        return getattr(self.variant.product, "cost_price", Decimal("0.00")) or Decimal("0.00")

    def clean(self) -> None:
        if self.variant_id and self.product_id and self.variant.product_id != self.product_id:
            raise ValidationError(
                {"variant": "Selected variant does not belong to the selected product."}
            )
        if self.order_id and self.tenant_id and self.order.tenant_id != self.tenant_id:
            raise ValidationError({"tenant": "Order item tenant must match order tenant."})
        if self.variant_id and self.tenant_id and self.variant.tenant_id != self.tenant_id:
            raise ValidationError({"variant": "Variant must belong to the same tenant."})

    def _get_snapshot_product_image(self) -> str | None:
        if not self.variant_id:
            return None

        product = self.variant.product

        if product.hero_image:
            try:
                url = product.hero_image.url
            except Exception:
                return None

            if not url:
                return None

            if url.startswith("http://") or url.startswith("https://"):
                return url

            return f"{settings.BACKEND_URL.rstrip('/')}{url}"

        if isinstance(product.image_urls, list) and product.image_urls:
            image = product.image_urls[0]

            if image.startswith("http://") or image.startswith("https://"):
                return image

            return f"{settings.BACKEND_URL.rstrip('/')}{image}"

        return None

    def save(self, *args, **kwargs):
        if self.variant_id:
            variant = self.variant
            product = variant.product
            self.tenant = variant.tenant
            self.product = product
            self.product_title = product.title
            self.product_image = self._get_snapshot_product_image()
            self.variant_name = variant.name
            self.variant_sku = variant.sku
            if self.unit_price is None:
                self.unit_price = variant.price
            if not self.cost_price_snapshot:
                self.cost_price_snapshot = self._snapshot_unit_cost()
        self.full_clean()
        super().save(*args, **kwargs)


class OrderStatusEvent(TimeStampedModel):
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="order_status_events",
        null=True,
        blank=True,
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="status_events",
    )
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="order_status_events",
        null=True,
        blank=True,
    )
    from_status = models.CharField(max_length=20, choices=Order.Status.choices, blank=True)
    to_status = models.CharField(max_length=20, choices=Order.Status.choices)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["tenant", "order"]),
            models.Index(fields=["tenant", "to_status"]),
        ]

    def __str__(self) -> str:
        return f"{self.order.slug}: {self.from_status} -> {self.to_status}"
