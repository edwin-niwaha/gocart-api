from decimal import Decimal

from rest_framework.exceptions import ValidationError

from apps.orders.models import Order
from .models import DeliveryRate


def _clean_location_part(value) -> str:
    return str(value or "").strip()


def resolve_checkout_delivery_rate(
    *,
    tenant,
    delivery_option: str,
    address=None,
    address_city: str = "",
    address_region: str = "",
    address_area: str = "",
) -> DeliveryRate | None:
    if tenant is None or delivery_option != Order.DeliveryOption.HOME_DELIVERY:
        return None

    region = _clean_location_part(address_region or getattr(address, "region", ""))
    city = _clean_location_part(address_city or getattr(address, "city", ""))
    area = _clean_location_part(address_area or getattr(address, "area", ""))

    if not region:
        return None

    rates = DeliveryRate.objects.filter(
        tenant=tenant,
        is_active=True,
        region=region,
    ).order_by("fee", "estimated_days", "id")

    if area and city:
        exact_rate = rates.filter(city__iexact=city, area__iexact=area).first()
        if exact_rate is not None:
            return exact_rate

    if city:
        city_rate = rates.filter(city__iexact=city, area="").first()
        if city_rate is not None:
            return city_rate

    region_rate = rates.filter(city="", area="").first()
    if region_rate is not None:
        return region_rate

    # Jumia-style coverage fallback: if the exact customer location is not
    # priced yet, use the broadest active tenant rate instead of blocking
    # checkout. Operators can still add exact city/area rates to override it.
    broad_rate = (
        DeliveryRate.objects.filter(
            tenant=tenant,
            is_active=True,
            city="",
            area="",
        )
        .order_by("fee", "estimated_days", "id")
        .first()
    )
    if broad_rate is not None:
        return broad_rate

    return (
        DeliveryRate.objects.filter(tenant=tenant, is_active=True)
        .order_by("fee", "estimated_days", "id")
        .first()
    )


def get_checkout_shipping_fee(
    *,
    tenant=None,
    delivery_option: str = Order.DeliveryOption.HOME_DELIVERY,
    address=None,
    address_city: str = "",
    address_region: str = "",
    address_area: str = "",
) -> Decimal:
    if delivery_option == Order.DeliveryOption.PICKUP_STATION:
        return Decimal("0.00")

    delivery_rate = resolve_checkout_delivery_rate(
        tenant=tenant,
        delivery_option=delivery_option,
        address=address,
        address_city=address_city,
        address_region=address_region,
        address_area=address_area,
    )
    if delivery_rate is not None:
        return delivery_rate.fee

    raise ValidationError("Delivery is not available for this location.")


