from __future__ import annotations

import logging
import re
from decimal import Decimal

from django.db import transaction
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.cart.models import Cart, CartItem
from apps.common.guest_sessions import get_request_guest_session_key
from apps.payments.models import Payment
from apps.payments.services import build_checkout_summary, serialize_checkout_summary
from apps.promotions.services import apply_coupon_to_order, increment_coupon_usage
from apps.tenants.permissions import IsTenantStaff
from apps.tenants.utils import user_is_tenant_staff
from .models import Order, OrderItem
from .notifications import queue_order_created_notifications
from .serializers import (
    OrderCheckoutSerializer,
    OrderItemReadSerializer,
    OrderReadSerializer,
    OrderStatusTransitionSerializer,
    OrderWriteSerializer,
)
from .services import add_order_item, create_order, transition_order_status

logger = logging.getLogger(__name__)
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")


def get_idempotency_key(request) -> str:
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        return ""
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise ValidationError({"idempotency_key": "Invalid idempotency key."})
    return key


def build_checkout_response(*, order: Order, payment: Payment, request, status_code):
    output = OrderReadSerializer(order, context={"request": request})
    return Response(
        {
            "order": output.data,
            "payment_reference": payment.reference,
            "payment_status": payment.status,
            "payment_provider": payment.provider,
        },
        status=status_code,
    )


def get_existing_checkout_payment(
    *,
    request,
    tenant,
    idempotency_key: str,
    guest_session_key: str | None = None,
):
    if not idempotency_key:
        return None

    queryset = (
        Payment.objects.select_related("order")
        .filter(
            tenant=tenant,
            provider_response__idempotency_key=idempotency_key,
            order__isnull=False,
        )
    )
    if getattr(request.user, "is_authenticated", False):
        queryset = queryset.filter(user=request.user)
    elif guest_session_key:
        queryset = queryset.filter(
            user__isnull=True,
            guest_session_key=guest_session_key,
        )
    else:
        return None

    return queryset.first()


def get_checkout_cart_items(*, request, tenant, lock: bool = False):
    is_authenticated = getattr(request.user, "is_authenticated", False)
    guest_session_key = get_request_guest_session_key(
        request,
        create=not is_authenticated,
    )
    cart_filters = (
        {"user": request.user}
        if is_authenticated
        else {
            "user__isnull": True,
            "guest_session_key": guest_session_key,
        }
    )
    cart_queryset = Cart.objects.filter(**cart_filters)
    if lock:
        cart_queryset = cart_queryset.select_for_update()
    cart = cart_queryset.first()
    if cart is None:
        return [], guest_session_key

    cart_items_queryset = (
        CartItem.objects.select_related("cart", "variant", "variant__product")
        .filter(
            cart=cart,
            variant__tenant=tenant,
        )
        .order_by("id")
    )
    if lock:
        cart_items_queryset = cart_items_queryset.select_for_update()

    return list(cart_items_queryset), guest_session_key


class CheckoutSummaryView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, *args, **kwargs):
        return self._build_summary_response(request, request.query_params)

    def post(self, request, *args, **kwargs):
        return self._build_summary_response(request, request.data)

    def _build_summary_response(self, request, data):
        serializer = OrderCheckoutSerializer(
            data=data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        tenant = getattr(request, "tenant", None)
        cart_items, _guest_session_key = get_checkout_cart_items(
            request=request,
            tenant=tenant,
            lock=False,
        )
        delivery_option = serializer.validated_data.get(
            "delivery_option",
            Order.DeliveryOption.HOME_DELIVERY,
        )
        pickup_station = serializer.validated_data.get("pickup_station")

        if not cart_items:
            return Response(
                {
                    "items_subtotal": "0.00",
                    "discount": "0.00",
                    "shipping": "0.00",
                    "total": "0.00",
                    "delivery_option": delivery_option,
                    "coupon_id": None,
                    "coupon_code": "",
                    "delivery_rate_id": None,
                    "estimated_days": None,
                    "shipping_method_id": None,
                    "pickup_station_id": pickup_station.id if pickup_station is not None else None,
                },
                status=status.HTTP_200_OK,
            )

        summary = build_checkout_summary(
            cart_items=cart_items,
            tenant=tenant,
            delivery_option=delivery_option,
            pickup_station=pickup_station,
            address=serializer.validated_data.get("address"),
            address_city=serializer.validated_data.get("address_city", ""),
            address_region=serializer.validated_data.get("address_region", ""),
            address_area=serializer.validated_data.get("address_area", ""),
            coupon_code=serializer.validated_data.get("coupon_code", ""),
        )
        return Response(serialize_checkout_summary(summary), status=status.HTTP_200_OK)


class IsAdminOrOwner(permissions.BasePermission):
    def has_permission(self, request, view):
        return True

    def has_object_permission(self, request, view, obj):
        if user_is_tenant_staff(request.user, getattr(request, "tenant", None)):
            return True

        if getattr(request.user, "is_authenticated", False):
            owner = getattr(obj, "user", None)
            if owner is not None:
                return owner == request.user

            order = getattr(obj, "order", None)
            if order is not None:
                return order.user == request.user

            return False

        guest_session_key = get_request_guest_session_key(request)
        if not guest_session_key:
            return False

        guest_owner = getattr(obj, "guest_session_key", None)
        if guest_owner is not None:
            return guest_owner == guest_session_key

        order = getattr(obj, "order", None)
        if order is not None:
            return order.guest_session_key == guest_session_key

        owner = getattr(obj, "user", None)
        if owner is not None:
            return owner == request.user

        return False


class OrderViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.AllowAny, IsAdminOrOwner]
    lookup_field = "slug"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status"]
    search_fields = ["slug", "description", "customer_email", "customer_name"]
    ordering_fields = ["created_at", "total_price", "status"]

    def get_permissions(self):
        if self.action == "transition_status":
            permission_classes = [permissions.IsAuthenticated, IsTenantStaff]
        elif self.action in {"create", "update", "partial_update", "destroy"}:
            permission_classes = [permissions.IsAuthenticated, IsAdminOrOwner]
        else:
            permission_classes = [permissions.AllowAny, IsAdminOrOwner]
        return [permission() for permission in permission_classes]

    def get_queryset(self):
        tenant = self.request.tenant  # type: ignore
        queryset = (
            Order.objects.select_related("tenant", "user", "address")
            .prefetch_related(
                "items",
                "items__product",
                "items__variant",
                "status_events",
                "status_events__changed_by",
            )
            .filter(tenant=tenant)
        )

        if user_is_tenant_staff(self.request.user, tenant):
            return queryset
        if getattr(self.request.user, "is_authenticated", False):
            return queryset.filter(user=self.request.user)

        guest_session_key = get_request_guest_session_key(self.request)
        if not guest_session_key:
            return queryset.none()

        return queryset.filter(
            user__isnull=True,
            guest_session_key=guest_session_key,
        )

    def get_serializer_class(self):
        if self.action == "checkout":
            return OrderCheckoutSerializer
        if self.action == "transition_status":
            return OrderStatusTransitionSerializer
        if self.action in {"create", "update", "partial_update"}:
            return OrderWriteSerializer
        return OrderReadSerializer

    def perform_create(self, serializer):
        address = serializer.validated_data.get("address")
        serializer.save(
            user=self.request.user,
            tenant=self.request.tenant,
            customer_name=(
                self.request.user.get_full_name().strip()
                or self.request.user.email
                or self.request.user.username
            ),
            customer_email=self.request.user.email,
            customer_phone=address.phone_number,
            address_street_name=address.street_name,
            address_city=address.city,
            address_area=address.area,
            address_region=address.region,
            address_additional_information=address.additional_information,
        )

    @action(detail=False, methods=["post"], url_path="checkout")
    def checkout(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        address = serializer.validated_data.get("address")
        description = serializer.validated_data.get("description", "")
        payment_method = serializer.validated_data.get(
            "payment_method",
            Payment.Provider.CASH,
        )
        delivery_option = serializer.validated_data.get(
            "delivery_option",
            Order.DeliveryOption.HOME_DELIVERY,
        )
        pickup_station = serializer.validated_data.get("pickup_station")
        coupon_code = serializer.validated_data.get("coupon_code", "")
        tenant = request.tenant
        idempotency_key = get_idempotency_key(request)
        is_authenticated = getattr(request.user, "is_authenticated", False)
        guest_session_key = get_request_guest_session_key(
            request,
            create=not is_authenticated,
        )

        existing_payment = get_existing_checkout_payment(
            request=request,
            tenant=tenant,
            idempotency_key=idempotency_key,
            guest_session_key=guest_session_key,
        )
        if existing_payment is not None:
            return build_checkout_response(
                order=existing_payment.order,
                payment=existing_payment,
                request=request,
                status_code=status.HTTP_200_OK,
            )

        with transaction.atomic():
            cart_filters = (
                {"user": request.user}
                if is_authenticated
                else {
                    "user__isnull": True,
                    "guest_session_key": guest_session_key,
                }
            )
            cart = Cart.objects.select_for_update().filter(**cart_filters).first()
            if cart is None:
                raise ValidationError({"detail": "Your cart is empty for this tenant."})

            existing_payment = get_existing_checkout_payment(
                request=request,
                tenant=tenant,
                idempotency_key=idempotency_key,
                guest_session_key=guest_session_key,
            )
            if existing_payment is not None:
                return build_checkout_response(
                    order=existing_payment.order,
                    payment=existing_payment,
                    request=request,
                    status_code=status.HTTP_200_OK,
                )

            cart_items, _guest_session_key = get_checkout_cart_items(
                request=request,
                tenant=tenant,
                lock=True,
            )

            if not cart_items:
                raise ValidationError({"detail": "Your cart is empty for this tenant."})

            locked_variants = {}
            for cart_item in cart_items:
                variant = (
                    cart_item.variant.__class__.objects.select_related("product")
                    .select_for_update()
                    .get(id=cart_item.variant.id, tenant=tenant)
                )
                quantity = cart_item.quantity

                if variant.stock_quantity < quantity:
                    raise ValidationError(
                        {
                            "detail": f"Insufficient stock for {variant.product.title} ({variant.name})"
                        }
                    )

                locked_variants[cart_item.id] = variant

            order = create_order(
                user=request.user if is_authenticated else None,
                tenant=tenant,
                address=address,
                guest_session_key=guest_session_key,
                customer_name=serializer.validated_data.get("customer_name", ""),
                customer_email=serializer.validated_data.get("customer_email", ""),
                customer_phone=serializer.validated_data.get("customer_phone", ""),
                address_street_name=serializer.validated_data.get("address_street_name", ""),
                address_city=serializer.validated_data.get("address_city", ""),
                address_area=serializer.validated_data.get("address_area", ""),
                address_region=serializer.validated_data.get("address_region", ""),
                address_additional_information=serializer.validated_data.get(
                    "address_additional_information",
                    "",
                ),
                description=description or "Placed from checkout",
                status=Order.Status.PENDING,
                delivery_option=delivery_option,
                pickup_station=pickup_station,
            )

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

            checkout_summary = build_checkout_summary(
                cart_items=cart_items,
                tenant=tenant,
                delivery_option=delivery_option,
                pickup_station=pickup_station,
                address=address,
                address_city=serializer.validated_data.get("address_city", ""),
                address_region=serializer.validated_data.get("address_region", ""),
                address_area=serializer.validated_data.get("address_area", ""),
                coupon_code=coupon_code,
            )
            items_subtotal = order.recalculate_total_price()
            discount_amount = checkout_summary["discount"]
            applied_coupon = checkout_summary["coupon"]

            if applied_coupon is not None:
                apply_coupon_to_order(order=order, code=coupon_code)
                increment_coupon_usage(coupon=applied_coupon)

            shipping_fee = checkout_summary["shipping"]
            final_total = checkout_summary["total"]
            order.items_subtotal = items_subtotal
            order.discount_amount = discount_amount
            order.shipping_fee = shipping_fee
            order.total_price = final_total
            order.save(
                update_fields=[
                    "items_subtotal",
                    "discount_amount",
                    "shipping_fee",
                    "total_price",
                    "updated_at",
                ]
            )

            serialized_checkout_summary = serialize_checkout_summary(checkout_summary)

            payment = Payment.objects.create(
                tenant=tenant,
                user=request.user if is_authenticated else None,
                guest_session_key=guest_session_key,
                order=order,
                provider=payment_method,
                amount=order.total_price,
                currency=Payment.Currency.UGX,
                status=Payment.Status.PENDING,
                provider_response={
                    "payment_method": payment_method,
                    "payment_note": "Pay on delivery selected at checkout",
                    "idempotency_key": idempotency_key,
                    "checkout_summary": serialized_checkout_summary,
                },
            )

            CartItem.objects.filter(id__in=[item.id for item in cart_items]).delete()
            queue_order_created_notifications(order.id)
            logger.info(
                "Checkout created order_id=%s user_id=%s tenant_id=%s payment_reference=%s request_id=%s",
                order.id,
                getattr(request.user, "id", None),
                getattr(tenant, "id", None),
                payment.reference,
                getattr(request, "id", ""),
            )

        return build_checkout_response(
            order=order,
            payment=payment,
            request=request,
            status_code=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="transition-status",
        permission_classes=[permissions.IsAuthenticated, IsTenantStaff],
    )
    def transition_status(self, request, slug=None, *args, **kwargs):
        order = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        updated = transition_order_status(
            order=order,
            new_status=serializer.validated_data["status"],
            changed_by=request.user,
            note=serializer.validated_data.get("note", ""),
        )

        return Response(
            OrderReadSerializer(updated, context=self.get_serializer_context()).data,
            status=status.HTTP_200_OK,
        )


class OrderItemViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [permissions.AllowAny, IsAdminOrOwner]
    serializer_class = OrderItemReadSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["order", "product", "variant"]
    search_fields = ["product_title", "variant_name", "variant_sku", "order__slug"]
    ordering_fields = ["created_at", "quantity", "unit_price"]

    def get_queryset(self):
        tenant = self.request.tenant  # type: ignore
        queryset = (
            OrderItem.objects.select_related("tenant", "order", "product", "variant")
            .filter(tenant=tenant)
        )

        if user_is_tenant_staff(self.request.user, tenant):
            return queryset
        if getattr(self.request.user, "is_authenticated", False):
            return queryset.filter(order__user=self.request.user)

        guest_session_key = get_request_guest_session_key(self.request)
        if not guest_session_key:
            return queryset.none()

        return queryset.filter(
            order__user__isnull=True,
            order__guest_session_key=guest_session_key,
        )

