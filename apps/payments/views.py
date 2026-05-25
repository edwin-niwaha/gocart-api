import logging

from django.db.models import Q
from django.db import transaction
from decimal import Decimal
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.addresses.models import CustomerAddress
from apps.cart.models import Cart, CartItem
from apps.accounting.posting import (
    queue_order_paid_accounting_event,
    should_post_order_revenue_on_payment,
)
from apps.orders.models import Order
from apps.orders.serializers import OrderReadSerializer
from apps.orders.services import add_order_item, create_order
from apps.promotions.models import Coupon
from apps.promotions.services import increment_coupon_usage
from rest_framework import generics

from .models import Payment
from .serializers import (
    CardInitiatePaymentSerializer,
    MTNInitiatePaymentSerializer,
    PaymentListSerializer,
    PaymentStatusSerializer,
)
from .services import (
    build_cart_snapshot,
    get_expected_total_from_payment_summary,
    get_cart_total_from_items,
    initiate_card_payment,
    initiate_mtn_payment,
    refresh_mtn_payment_status,
    user_has_cart_items,
)
from apps.orders.views import get_idempotency_key

logger = logging.getLogger(__name__)


def _get_cart_items_for_user(user, tenant):
    cart = Cart.objects.select_for_update().filter(user=user).first()
    if cart is None:
        return []

    return list(
        CartItem.objects.select_for_update()
        .select_related(
            "cart",
            "variant",
            "variant__product",
        )
        .filter(
            cart=cart,
            variant__tenant=tenant,
        )
        .order_by("id")
    )


class MTNInitiatePaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        idempotency_key = get_idempotency_key(request)
        if idempotency_key:
            existing_payment = (
                Payment.objects.filter(
                    tenant=request.tenant,
                    user=request.user,
                    provider=Payment.Provider.MTN,
                    provider_response__idempotency_key=idempotency_key,
                )
                .order_by("-created_at")
                .first()
            )
            if existing_payment is not None:
                return Response(
                    {
                        "reference": existing_payment.reference,
                        "external_id": existing_payment.external_id,
                        "status": existing_payment.status,
                        "amount": existing_payment.amount,
                        "currency": existing_payment.currency,
                    },
                    status=status.HTTP_200_OK,
                )

        serializer = MTNInitiatePaymentSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        if not user_has_cart_items(request.user, request.tenant):
            raise ValidationError({"detail": "Your cart is empty."})

        address = serializer.context["address_instance"]
        phone_number = serializer.validated_data["phone_number"]
        delivery_option = serializer.validated_data.get(
            "delivery_option",
            Order.DeliveryOption.HOME_DELIVERY,
        )
        pickup_station = serializer.validated_data.get("pickup_station")
        coupon_code = serializer.validated_data.get("coupon_code", "")
        order = None  # DO NOT create order yet

        payment = initiate_mtn_payment(
            user=request.user,
            order=order,
            phone_number=phone_number,
            address=address,
            tenant=request.tenant,
            delivery_option=delivery_option,
            pickup_station=pickup_station,
            coupon_code=coupon_code,
            idempotency_key=idempotency_key,
        )
        logger.info(
            "MTN payment initiated payment_id=%s user_id=%s tenant_id=%s amount=%s request_id=%s",
            payment.id,
            request.user.id,
            getattr(request.tenant, "id", None),
            payment.amount,
            getattr(request, "id", ""),
        )

        return Response(
            {
                "reference": payment.reference,
                "external_id": payment.external_id,
                "status": payment.status,
                "amount": payment.amount,
                "currency": payment.currency,
            },
            status=status.HTTP_201_CREATED,
        )


class CardInitiatePaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        idempotency_key = get_idempotency_key(request)
        if idempotency_key:
            existing_payment = (
                Payment.objects.filter(
                    tenant=request.tenant,
                    user=request.user,
                    provider=Payment.Provider.CARD,
                    provider_response__idempotency_key=idempotency_key,
                )
                .order_by("-created_at")
                .first()
            )
            if existing_payment is not None:
                return Response(
                    {
                        "reference": existing_payment.reference,
                        "checkout_url": existing_payment.checkout_url or None,
                        "status": existing_payment.status,
                        "amount": existing_payment.amount,
                        "currency": existing_payment.currency,
                    },
                    status=status.HTTP_200_OK,
                )

        serializer = CardInitiatePaymentSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        if not user_has_cart_items(request.user, request.tenant):
            raise ValidationError({"detail": "Your cart is empty."})

        address = serializer.context["address_instance"]
        delivery_option = serializer.validated_data.get(
            "delivery_option",
            Order.DeliveryOption.HOME_DELIVERY,
        )
        pickup_station = serializer.validated_data.get("pickup_station")
        coupon_code = serializer.validated_data.get("coupon_code", "")

        payment = initiate_card_payment(
            user=request.user,
            address=address,
            tenant=request.tenant,
            delivery_option=delivery_option,
            pickup_station=pickup_station,
            coupon_code=coupon_code,
            gateway=serializer.validated_data.get("gateway", ""),
            cardholder_name=serializer.validated_data["cardholder_name"],
            card_last4=serializer.validated_data["card_last4"],
            expiry_month=serializer.validated_data["expiry_month"],
            expiry_year=serializer.validated_data["expiry_year"],
            billing_email=serializer.validated_data.get("billing_email", ""),
            billing_phone=serializer.validated_data.get("billing_phone", ""),
            idempotency_key=idempotency_key,
        )
        logger.info(
            "Card payment initialized payment_id=%s user_id=%s tenant_id=%s amount=%s request_id=%s",
            payment.id,
            request.user.id,
            getattr(request.tenant, "id", None),
            payment.amount,
            getattr(request, "id", ""),
        )

        return Response(
            {
                "reference": payment.reference,
                "checkout_url": payment.checkout_url or None,
                "status": payment.status,
                "amount": payment.amount,
                "currency": payment.currency,
            },
            status=status.HTTP_201_CREATED,
        )


class PaymentStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, reference):
        try:
            payment = Payment.objects.get(
                reference=reference,
                user=request.user,
                tenant=request.tenant,
            )
        except Payment.DoesNotExist:
            raise NotFound("Payment not found.")

        if payment.provider == Payment.Provider.MTN:
            payment = refresh_mtn_payment_status(payment)

        return Response(
            PaymentStatusSerializer(payment).data,
            status=status.HTTP_200_OK,
        )


class FinalizePaidOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, reference):
        try:
            payment_for_refresh = Payment.objects.get(
                reference=reference,
                user=request.user,
                tenant=request.tenant,
            )
        except Payment.DoesNotExist:
            raise NotFound("Payment not found.")

        if payment_for_refresh.provider == Payment.Provider.MTN:
            refresh_mtn_payment_status(payment_for_refresh)

        with transaction.atomic():
            try:
                payment = (
                    Payment.objects.select_for_update()
                    .select_related("order")
                    .get(
                        reference=reference,
                        user=request.user,
                        tenant=request.tenant,
                    )
                )
            except Payment.DoesNotExist:
                raise NotFound("Payment not found.")

            if payment.status == Payment.Status.PROCESSING:
                raise ValidationError(
                    {
                        "detail": "Payment is still being confirmed. Please try again in a moment."
                    }
                )

            if payment.status != Payment.Status.PAID:
                raise ValidationError(
                    {
                        "detail": f"Payment is not successful. Current status: {payment.status}."
                    }
                )

            if payment.order is not None:
                order = payment.order
                if payment.amount != order.total_price:
                    raise ValidationError(
                        {
                            "detail": "Paid payment amount does not match the linked order total."
                        }
                    )
                if should_post_order_revenue_on_payment(order):
                    queue_order_paid_accounting_event(order)

                output = OrderReadSerializer(order, context={"request": request})
                return Response(
                    {
                        "order": output.data,
                        "payment_reference": payment.reference,
                        "payment_status": payment.status,
                        "payment_provider": payment.provider,
                    },
                    status=status.HTTP_200_OK,
                )

            checkout_summary = payment.provider_response.get("checkout_summary") or {}
            delivery_option = checkout_summary.get("delivery_option") or Order.DeliveryOption.HOME_DELIVERY
            address_id = payment.provider_response.get("address_id")
            if delivery_option != Order.DeliveryOption.PICKUP_STATION and not address_id:
                raise ValidationError({"detail": "Address information missing from payment."})

            address = None
            if address_id:
                try:
                    address = CustomerAddress.objects.get(
                        id=address_id,
                        user=request.user,
                    )
                except CustomerAddress.DoesNotExist:
                    raise ValidationError({"detail": "Address not found."})

            cart_items = _get_cart_items_for_user(request.user, request.tenant)
            if not cart_items:
                raise ValidationError({"detail": "Cart is empty."})

            expected_payment_total = get_expected_total_from_payment_summary(
                cart_items=cart_items,
                payment=payment,
            )
            if expected_payment_total != payment.amount:
                logger.warning(
                    "Payment finalization blocked due to amount mismatch payment_id=%s user_id=%s paid_amount=%s expected_total=%s request_id=%s",
                    payment.id,
                    request.user.id,
                    payment.amount,
                    expected_payment_total,
                    getattr(request, "id", ""),
                )
                raise ValidationError(
                    {
                        "detail": "Your cart changed after payment was initiated. Please start a new payment."
                    }
                )

            expected_snapshot = payment.provider_response.get("cart_snapshot")
            if expected_snapshot and expected_snapshot != build_cart_snapshot(cart_items):
                logger.warning(
                    "Payment finalization blocked due to cart snapshot mismatch payment_id=%s user_id=%s request_id=%s",
                    payment.id,
                    request.user.id,
                    getattr(request, "id", ""),
                )
                raise ValidationError(
                    {
                        "detail": "Your cart changed after payment was initiated. Please start a new payment."
                    }
                )

            locked_variants = {}
            for cart_item in cart_items:
                variant = (
                    cart_item.variant.__class__.objects.select_related("product")
                    .select_for_update()
                    .get(id=cart_item.variant.id, tenant=request.tenant)
                )
                quantity = cart_item.quantity

                if variant.stock_quantity < quantity:
                    raise ValidationError(
                        {
                            "detail": f"Insufficient stock for {variant.product.title} ({variant.name})"
                        }
                    )

                locked_variants[cart_item.id] = variant

            # ALWAYS create order here after successful payment and cart verification.
            pickup_station = None
            pickup_station_id = checkout_summary.get("pickup_station_id")
            if delivery_option == Order.DeliveryOption.PICKUP_STATION:
                if not pickup_station_id:
                    raise ValidationError({"detail": "Pickup station information missing from payment."})

                from apps.shipping.models import PickupStation

                pickup_station = PickupStation.objects.filter(
                    Q(tenant=request.tenant) | Q(tenant__isnull=True),
                    id=pickup_station_id,
                    is_active=True,
                ).first()
                if pickup_station is None:
                    raise ValidationError({"detail": "Pickup station not found."})

            order = create_order(
                user=request.user,
                tenant=request.tenant,
                address=address,
                description="Placed after successful online payment",
                status=Order.Status.PENDING,
                delivery_option=delivery_option,
                pickup_station=pickup_station,
            )

            payment.order = order

            for cart_item in cart_items:
                variant = locked_variants[cart_item.id]
                quantity = cart_item.quantity

                variant.stock_quantity -= quantity
                variant.save(update_fields=["stock_quantity"])
                add_order_item(
                    order=order,
                    variant=variant,
                    quantity=quantity,
                    unit_price=cart_item.unit_price,
                )

            order.discount_amount = Decimal(
                str(checkout_summary.get("discount", "0.00"))
            )
            order.shipping_fee = Decimal(
                str(checkout_summary.get("shipping", "0.00"))
            )
            order.recalculate_total_price()
            order.save(
                update_fields=[
                    "items_subtotal",
                    "discount_amount",
                    "shipping_fee",
                    "updated_at",
                ]
            )

            CartItem.objects.filter(id__in=[item.id for item in cart_items]).delete()

            coupon_id = checkout_summary.get("coupon_id")
            if coupon_id:
                coupon = Coupon.objects.filter(id=coupon_id, tenant=request.tenant).first()
                if coupon is not None:
                    increment_coupon_usage(coupon=coupon)

            payment.tenant = request.tenant
            payment.amount = order.total_price
            payment.save(update_fields=["order", "amount", "tenant", "updated_at"])
            if should_post_order_revenue_on_payment(order):
                queue_order_paid_accounting_event(order)
            logger.info(
                "Payment finalized order_id=%s payment_id=%s user_id=%s tenant_id=%s request_id=%s",
                order.id,
                payment.id,
                request.user.id,
                getattr(request.tenant, "id", None),
                getattr(request, "id", ""),
            )

            output = OrderReadSerializer(order, context={"request": request})
            return Response(
                {
                    "order": output.data,
                    "payment_reference": payment.reference,
                    "payment_status": payment.status,
                    "payment_provider": payment.provider,
                },
                status=status.HTTP_201_CREATED,
            )

class UserPaymentListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PaymentListSerializer

    def get_queryset(self):
        queryset = Payment.objects.filter(user=self.request.user)

        tenant = getattr(self.request, "tenant", None)
        if tenant is not None:
            queryset = queryset.filter(tenant=tenant)

        return queryset.select_related("user", "order").order_by("-created_at")
