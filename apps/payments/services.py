import logging
import random
import uuid
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from apps.cart.models import CartItem
from apps.orders.models import Order
from apps.promotions.services import calculate_coupon_discount, get_valid_coupon
from apps.shipping.services import (
    get_checkout_shipping_fee,
    resolve_checkout_delivery_rate,
)
from apps.shipping.models import ShippingMethod
from .models import Payment

logger = logging.getLogger(__name__)

REQUIRED_MOMO_SETTINGS = (
    "SUBSCRIPTION_KEY",
    "MOMO_API_USER",
    "MOMO_API_KEY",
    "MOMO_BASE_URL",
)


class PaymentProviderUnavailable(APIException):
    status_code = 503
    default_detail = "Payment provider is temporarily unavailable."
    default_code = "payment_provider_unavailable"


def generate_uuid() -> str:
    return str(uuid.uuid4())


def validate_momo_configuration() -> None:
    if not getattr(settings, "ENABLE_MOMO", False):
        raise ValidationError({"detail": "Mobile money payments are not enabled."})

    missing = [name for name in REQUIRED_MOMO_SETTINGS if not getattr(settings, name, "")]
    if missing:
        raise ValidationError(
            {
                "detail": "Mobile money payments are not configured.",
                "missing_settings": missing,
            }
        )


def validate_card_payment_configuration() -> None:
    if not getattr(settings, "ENABLE_CARD_PAYMENTS", False):
        raise ValidationError({"detail": "Card payments are not enabled."})

    gateway = str(getattr(settings, "CARD_PAYMENT_GATEWAY", "") or "").strip().lower()
    if not getattr(settings, "DEBUG", False) and gateway in {"", "placeholder", "test"}:
        raise ValidationError(
            {
                "detail": "Card payments require a production payment gateway.",
                "missing_settings": ["CARD_PAYMENT_GATEWAY"],
            }
        )

    if not getattr(settings, "CARD_PAYMENT_GATEWAY_CHECKOUT_URL", ""):
        raise ValidationError(
            {
                "detail": "Card payments are not configured.",
                "missing_settings": ["CARD_PAYMENT_GATEWAY_CHECKOUT_URL"],
            }
        )


def momo_headers(
    subscription_key: str,
    token: str | None = None,
    ref_id: str | None = None,
):
    headers = {
        "Ocp-Apim-Subscription-Key": subscription_key,
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Target-Environment"] = settings.MOMO_TARGET_ENVIRONMENT

    if ref_id:
        headers["X-Reference-Id"] = ref_id

    return headers


def create_access_token() -> str:
    validate_momo_configuration()
    url = f"{settings.MOMO_BASE_URL}/collection/token/"
    auth = requests.auth.HTTPBasicAuth(
        settings.MOMO_API_USER,
        settings.MOMO_API_KEY,
    )
    headers = {
        "Ocp-Apim-Subscription-Key": settings.SUBSCRIPTION_KEY,
    }

    try:
        res = requests.post(url, headers=headers, auth=auth, timeout=30)
        res.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("MTN token request failed error=%s", exc)
        raise PaymentProviderUnavailable() from exc

    token = res.json().get("access_token")
    if not token:
        raise ValueError("MTN access token missing in response.")

    return token


def request_to_pay(
    *,
    phone: str,
    amount: Decimal,
    external_id: str | None = None,
) -> dict:
    access_token = create_access_token()
    transaction_id = generate_uuid()

    url = f"{settings.MOMO_BASE_URL}/collection/v1_0/requesttopay"
    headers = momo_headers(settings.SUBSCRIPTION_KEY, access_token, transaction_id)

    clean_phone = phone.replace("+", "").replace(" ", "")
    numeric_external_id = external_id or str(random.randint(10000000, 99999999))

    body = {
        "amount": str(int(amount)),
        "currency": settings.MOMO_CURRENCY,
        "externalId": numeric_external_id,
        "payer": {
            "partyIdType": "MSISDN",
            "partyId": clean_phone,
        },
        "payerMessage": "GoCart order payment",
        "payeeNote": "Thank you for using GoCart",
    }

    try:
        res = requests.post(url, json=body, headers=headers, timeout=30)
    except requests.RequestException as exc:
        logger.warning("MTN request_to_pay failed reference_id=%s error=%s", transaction_id, exc)
        raise PaymentProviderUnavailable() from exc

    data = {}
    try:
        data = res.json() if res.content else {}
    except Exception:
        data = {"raw": res.text}

    log_func = logger.info if res.status_code == 202 else logger.warning
    log_func(
        "MTN request_to_pay status=%s reference_id=%s provider_status=%s",
        res.status_code,
        transaction_id,
        data.get("status") if isinstance(data, dict) else "",
    )

    return {
        "reference_id": transaction_id,
        "status_code": res.status_code,
        "data": data,
    }


def get_user_cart_total(user, tenant=None) -> Decimal:
    return get_cart_total_from_items(get_user_cart_items(user=user, tenant=tenant))


def get_user_cart_items(*, user, tenant=None) -> list[CartItem]:
    cart_items = CartItem.objects.select_related("variant", "variant__product").filter(cart__user=user)

    if tenant is not None:
        cart_items = cart_items.filter(variant__tenant=tenant)

    return list(cart_items)


def get_cart_total_from_items(cart_items: list[CartItem]) -> Decimal:
    return sum((item.line_total for item in cart_items), Decimal("0.00"))


def validate_coupon_for_cart_items(*, coupon, cart_items: list[CartItem]) -> None:
    if not coupon.products.exists() and not coupon.categories.exists():
        return

    cart_product_ids = {item.variant.product_id for item in cart_items}
    cart_category_ids = {item.variant.product.category_id for item in cart_items}
    coupon_product_ids = set(coupon.products.values_list("id", flat=True))
    coupon_category_ids = set(coupon.categories.values_list("id", flat=True))

    if not (cart_product_ids & coupon_product_ids) and not (cart_category_ids & coupon_category_ids):
        raise ValidationError("Coupon does not apply to this cart.")


def build_checkout_summary(
    *,
    cart_items: list[CartItem],
    tenant=None,
    delivery_option: str = Order.DeliveryOption.HOME_DELIVERY,
    pickup_station=None,
    address=None,
    address_city: str = "",
    address_region: str = "",
    address_area: str = "",
    coupon_code: str = "",
) -> dict:
    items_subtotal = get_cart_total_from_items(cart_items)
    discount = Decimal("0.00")
    coupon = None
    delivery_rate = resolve_checkout_delivery_rate(
        tenant=tenant,
        delivery_option=delivery_option,
        address=address,
        address_city=address_city,
        address_region=address_region,
        address_area=address_area,
    )

    if coupon_code:
        coupon = get_valid_coupon(code=coupon_code, tenant=tenant)
        validate_coupon_for_cart_items(coupon=coupon, cart_items=cart_items)
        discount = calculate_coupon_discount(coupon=coupon, amount=items_subtotal)

    shipping = get_checkout_shipping_fee(
        tenant=tenant,
        delivery_option=delivery_option,
        address=address,
        address_city=address_city,
        address_region=address_region,
        address_area=address_area,
    )
    shipping_method = None
    if delivery_option == Order.DeliveryOption.HOME_DELIVERY:
        shipping_method = (
            ShippingMethod.objects.filter(is_active=True, fee=shipping)
            .order_by("estimated_days", "id")
            .first()
        )
    total = max(items_subtotal - discount, Decimal("0.00")) + shipping

    return {
        "items_subtotal": items_subtotal,
        "discount": discount,
        "shipping": shipping,
        "total": total,
        "delivery_option": delivery_option,
        "coupon": coupon,
        "coupon_id": coupon.id if coupon is not None else None,
        "coupon_code": coupon.code if coupon is not None else "",
        "delivery_rate_id": delivery_rate.id if delivery_rate is not None else None,
        "estimated_days": delivery_rate.estimated_days if delivery_rate is not None else None,
        "shipping_method_id": shipping_method.id if shipping_method is not None else None,
        "pickup_station_id": pickup_station.id if pickup_station is not None else None,
    }


def serialize_checkout_summary(summary: dict) -> dict:
    return {
        "items_subtotal": str(summary["items_subtotal"]),
        "discount": str(summary["discount"]),
        "shipping": str(summary["shipping"]),
        "total": str(summary["total"]),
        "delivery_option": summary["delivery_option"],
        "coupon_id": summary["coupon_id"],
        "coupon_code": summary["coupon_code"],
        "delivery_rate_id": summary["delivery_rate_id"],
        "estimated_days": summary["estimated_days"],
        "shipping_method_id": summary["shipping_method_id"],
        "pickup_station_id": summary["pickup_station_id"],
    }


def get_expected_total_from_payment_summary(*, cart_items: list[CartItem], payment: Payment) -> Decimal:
    checkout_summary = payment.provider_response.get("checkout_summary") or {}
    if not checkout_summary:
        return get_cart_total_from_items(cart_items)

    items_subtotal = Decimal(str(checkout_summary.get("items_subtotal", "0.00")))
    if get_cart_total_from_items(cart_items) != items_subtotal:
        return Decimal("-1.00")

    discount = Decimal(str(checkout_summary.get("discount", "0.00")))
    shipping = Decimal(str(checkout_summary.get("shipping", "0.00")))
    return max(items_subtotal - discount, Decimal("0.00")) + shipping


def build_cart_snapshot(cart_items: list[CartItem]) -> list[dict]:
    return [
        {
            "variant_id": item.variant_id,
            "quantity": item.quantity,
            "unit_price": str(item.unit_price),
        }
        for item in sorted(cart_items, key=lambda cart_item: cart_item.variant_id)
    ]


def user_has_cart_items(user, tenant=None) -> bool:
    queryset = CartItem.objects.filter(cart__user=user)

    if tenant is not None:
        queryset = queryset.filter(variant__tenant=tenant)

    return queryset.exists()


def initiate_mtn_payment(
    *,
    user,
    order,
    phone_number: str,
    address,
    tenant=None,
    delivery_option: str = Order.DeliveryOption.HOME_DELIVERY,
    pickup_station=None,
    coupon_code: str = "",
    idempotency_key: str = "",
) -> Payment:
    validate_momo_configuration()
    cart_items = get_user_cart_items(user=user, tenant=tenant)
    if get_cart_total_from_items(cart_items) <= Decimal("0.00"):
        raise ValidationError({"detail": "Your cart is empty."})
    checkout_summary = build_checkout_summary(
        cart_items=cart_items,
        tenant=tenant,
        delivery_option=delivery_option,
        pickup_station=pickup_station,
        address=address,
        coupon_code=coupon_code,
    )
    amount = checkout_summary["total"]
    cart_snapshot = build_cart_snapshot(cart_items)
    serialized_summary = serialize_checkout_summary(checkout_summary)

    payment = Payment.objects.create(
        tenant=tenant,
        user=user,
        order=None,
        provider=Payment.Provider.MTN,
        amount=amount,
        currency=settings.MOMO_CURRENCY,
        phone_number=phone_number,
        status=Payment.Status.PENDING,
        provider_response={
            "address_id": address.id if address is not None else None,
            "idempotency_key": idempotency_key,
            "cart_snapshot": cart_snapshot,
            "cart_total": str(checkout_summary["items_subtotal"]),
            "checkout_summary": serialized_summary,
        },
    )

    result = request_to_pay(
        phone=phone_number,
        amount=amount,
        external_id=str(random.randint(10000000, 99999999)),
    )

    payment.external_id = result["reference_id"]
    payment.transaction_id = result["reference_id"]
    payment.provider_response = {
        **payment.provider_response,
        "initiate": result["data"],
        "initiate_status_code": result["status_code"],
        "address_id": address.id if address is not None else None,
        "idempotency_key": idempotency_key,
        "cart_snapshot": cart_snapshot,
        "cart_total": str(checkout_summary["items_subtotal"]),
        "checkout_summary": serialized_summary,
    }

    if result["status_code"] == 202:
        payment.status = Payment.Status.PROCESSING
        payment.save(
            update_fields=[
                "external_id",
                "transaction_id",
                "provider_response",
                "status",
                "updated_at",
            ]
        )
        return payment

    payment.status = Payment.Status.FAILED
    payment.save(
        update_fields=[
            "external_id",
            "transaction_id",
            "provider_response",
            "status",
            "updated_at",
        ]
    )

    raise ValidationError(
        {
            "detail": "MTN payment request was not accepted.",
            "provider_response": result["data"],
            "provider_status_code": result["status_code"],
        }
    )


def initiate_card_payment(
    *,
    user,
    address,
    tenant=None,
    delivery_option: str = Order.DeliveryOption.HOME_DELIVERY,
    pickup_station=None,
    coupon_code: str = "",
    gateway: str = "",
    cardholder_name: str = "",
    card_last4: str = "",
    expiry_month: int | None = None,
    expiry_year: int | None = None,
    billing_email: str = "",
    billing_phone: str = "",
    idempotency_key: str = "",
) -> Payment:
    validate_card_payment_configuration()

    cart_items = get_user_cart_items(user=user, tenant=tenant)
    if get_cart_total_from_items(cart_items) <= Decimal("0.00"):
        raise ValidationError({"detail": "Your cart is empty."})

    checkout_summary = build_checkout_summary(
        cart_items=cart_items,
        tenant=tenant,
        delivery_option=delivery_option,
        pickup_station=pickup_station,
        address=address,
        coupon_code=coupon_code,
    )
    amount = checkout_summary["total"]
    cart_snapshot = build_cart_snapshot(cart_items)
    serialized_summary = serialize_checkout_summary(checkout_summary)
    gateway_name = gateway or getattr(settings, "CARD_PAYMENT_GATEWAY", "placeholder")

    # TODO: Replace this placeholder with a PCI-compliant gateway integration
    # (for example Flutterwave, Stripe, Pesapal, or DPO). The backend must
    # accept a provider token/hosted checkout callback, never raw PAN or CVV.
    payment = Payment.objects.create(
        tenant=tenant,
        user=user,
        order=None,
        provider=Payment.Provider.CARD,
        amount=amount,
        currency=Payment.Currency.UGX,
        status=Payment.Status.PROCESSING,
        checkout_url=getattr(settings, "CARD_PAYMENT_GATEWAY_CHECKOUT_URL", ""),
        provider_response={
            "address_id": address.id if address is not None else None,
            "idempotency_key": idempotency_key,
            "cart_snapshot": cart_snapshot,
            "cart_total": str(checkout_summary["items_subtotal"]),
            "checkout_summary": serialized_summary,
            "gateway": gateway_name,
            "card": {
                "cardholder_name": cardholder_name,
                "last4": card_last4,
                "expiry_month": expiry_month,
                "expiry_year": expiry_year,
            },
            "billing": {
                "email": billing_email,
                "phone": billing_phone,
            },
            "integration_status": "placeholder_checkout_created",
        },
    )

    return payment


def check_status(reference_id: str) -> dict:
    access_token = create_access_token()
    url = f"{settings.MOMO_BASE_URL}/collection/v1_0/requesttopay/{reference_id}"
    headers = momo_headers(settings.SUBSCRIPTION_KEY, token=access_token)

    try:
        res = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        logger.warning("MTN check_status failed reference_id=%s error=%s", reference_id, exc)
        raise PaymentProviderUnavailable() from exc

    try:
        data = res.json() if res.content else {}
    except Exception:
        data = {"raw": res.text}

    log_func = logger.info if res.status_code < 400 else logger.warning
    log_func(
        "MTN check_status http_status=%s reference_id=%s provider_status=%s",
        res.status_code,
        reference_id,
        data.get("status") if isinstance(data, dict) else "",
    )

    return {
        "http_status": res.status_code,
        "data": data,
    }


def refresh_mtn_payment_status(payment: Payment) -> Payment:
    if payment.provider != Payment.Provider.MTN or not payment.external_id:
        return payment

    result = check_status(payment.external_id)
    http_status = result.get("http_status")
    data = result.get("data", {})

    if http_status == 404:
        payment.status = Payment.Status.FAILED
        payment.provider_response = {
            **payment.provider_response,
            "status_check": data,
            "status_check_http_status": http_status,
        }
        payment.save(
            update_fields=[
                "status",
                "provider_response",
                "updated_at",
            ]
        )
        return payment

    if http_status >= 400:
        payment.provider_response = {
            **payment.provider_response,
            "status_check": data,
            "status_check_http_status": http_status,
        }
        payment.save(
            update_fields=[
                "provider_response",
                "updated_at",
            ]
        )
        return payment

    provider_status = str(data.get("status", "")).upper()

    financial_transaction_id = data.get("financialTransactionId")
    if financial_transaction_id:
        payment.transaction_id = str(financial_transaction_id)

    if provider_status == "SUCCESSFUL":
        payment.status = Payment.Status.PAID
        if not payment.paid_at:
            payment.paid_at = timezone.now()
    elif provider_status in {"FAILED", "REJECTED", "EXPIRED"}:
        payment.status = Payment.Status.FAILED
    else:
        payment.status = Payment.Status.PROCESSING

    payment.provider_response = {
        **payment.provider_response,
        "status_check": data,
        "status_check_http_status": http_status,
    }
    payment.save(
        update_fields=[
            "status",
            "provider_response",
            "transaction_id",
            "paid_at",
            "updated_at",
        ]
    )
    return payment
