from __future__ import annotations

from django.conf import settings
from django.db.models import Q
from rest_framework import serializers

from apps.addresses.models import CustomerAddress
from apps.payments.models import Payment
from apps.shipping.models import PickupStation
from apps.tenants.utils import user_is_tenant_staff
from .models import Order, OrderItem, OrderStatusEvent


def normalize_order_status(status: str) -> str:
    if status == Order.Status.PAID:
        return Order.Status.PROCESSING
    return status


def get_order_payment_status(order: Order) -> str:
    latest_payment = (
        order.payments.order_by("-paid_at", "-created_at", "-id").first()
        if getattr(order, "pk", None)
        else None
    )
    if latest_payment is not None:
        return latest_payment.status
    if order.status == Order.Status.PAID:
        return Payment.Status.PAID
    return Payment.Status.UNPAID


def _validate_order_address_owner(serializer, value: CustomerAddress) -> CustomerAddress:
    request = serializer.context["request"]
    instance = getattr(serializer, "instance", None)
    expected_user = getattr(instance, "user", request.user)

    if value.user_id != expected_user.id:
        raise serializers.ValidationError("Address not found.")

    return value


class OrderItemReadSerializer(serializers.ModelSerializer):
    product_image = serializers.SerializerMethodField()
    line_total = serializers.ReadOnlyField()
    line_cost_total = serializers.ReadOnlyField()
    gross_profit = serializers.ReadOnlyField()

    class Meta:
        model = OrderItem
        fields = (
            "id",
            "tenant",
            "product",
            "product_title",
            "product_image",
            "variant",
            "variant_name",
            "variant_sku",
            "quantity",
            "unit_price",
            "cost_price_snapshot",
            "line_total",
            "line_cost_total",
            "gross_profit",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_product_image(self, obj):
        product = getattr(obj, "product", None)
        if not product:
            return None
        return product.primary_image


class OrderStatusEventSerializer(serializers.ModelSerializer):
    changed_by_email = serializers.CharField(source="changed_by.email", read_only=True)

    class Meta:
        model = OrderStatusEvent
        fields = (
            "id",
            "from_status",
            "to_status",
            "note",
            "changed_by",
            "changed_by_email",
            "created_at",
        )
        read_only_fields = fields


class OrderReadSerializer(serializers.ModelSerializer):
    items = OrderItemReadSerializer(many=True, read_only=True)
    status_events = OrderStatusEventSerializer(many=True, read_only=True)
    user_email = serializers.SerializerMethodField()
    address_id = serializers.SerializerMethodField()
    pickup_station_id = serializers.SerializerMethodField()
    pickup_station_name = serializers.SerializerMethodField()
    pickup_station_city = serializers.SerializerMethodField()
    pickup_station_area = serializers.SerializerMethodField()
    pickup_station_address = serializers.SerializerMethodField()
    pickup_station_phone = serializers.SerializerMethodField()
    pickup_station_opening_hours = serializers.SerializerMethodField()
    address_street_name = serializers.SerializerMethodField()
    address_city = serializers.SerializerMethodField()
    address_area = serializers.SerializerMethodField()
    address_region = serializers.SerializerMethodField()
    address_additional_information = serializers.SerializerMethodField()
    is_guest = serializers.SerializerMethodField()
    order_status = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            "id",
            "tenant",
            "slug",
            "user",
            "user_email",
            "customer_name",
            "customer_email",
            "customer_phone",
            "is_guest",
            "status",
            "order_status",
            "payment_status",
            "delivery_option",
            "description",
            "items_subtotal",
            "discount_amount",
            "shipping_fee",
            "total_price",
            "address_id",
            "pickup_station_id",
            "pickup_station_name",
            "pickup_station_city",
            "pickup_station_area",
            "pickup_station_address",
            "pickup_station_phone",
            "pickup_station_opening_hours",
            "address_street_name",
            "address_city",
            "address_area",
            "address_region",
            "address_additional_information",
            "items",
            "status_events",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_user_email(self, obj: Order) -> str | None:
        return obj.contact_email or None

    def get_address_id(self, obj: Order) -> int | None:
        return obj.address_id

    def get_pickup_station_id(self, obj: Order) -> int | None:
        return obj.pickup_station_id

    def get_pickup_station_name(self, obj: Order) -> str:
        return obj.delivery_pickup_station_name

    def get_pickup_station_city(self, obj: Order) -> str:
        return obj.delivery_pickup_station_city

    def get_pickup_station_area(self, obj: Order) -> str:
        return obj.delivery_pickup_station_area

    def get_pickup_station_address(self, obj: Order) -> str:
        return obj.delivery_pickup_station_address

    def get_pickup_station_phone(self, obj: Order) -> str:
        return obj.delivery_pickup_station_phone

    def get_pickup_station_opening_hours(self, obj: Order) -> str:
        return obj.delivery_pickup_station_opening_hours

    def get_address_street_name(self, obj: Order) -> str:
        return obj.delivery_street_name

    def get_address_city(self, obj: Order) -> str:
        return obj.delivery_city

    def get_address_area(self, obj: Order) -> str:
        return obj.delivery_area

    def get_address_region(self, obj: Order) -> str:
        return obj.delivery_region

    def get_address_additional_information(self, obj: Order) -> str:
        return obj.delivery_additional_information

    def get_is_guest(self, obj: Order) -> bool:
        return obj.user_id is None

    def get_order_status(self, obj: Order) -> str:
        return normalize_order_status(obj.status)

    def get_payment_status(self, obj: Order) -> str:
        return get_order_payment_status(obj)


class OrderCheckoutSerializer(serializers.Serializer):
    address_id = serializers.PrimaryKeyRelatedField(
        queryset=CustomerAddress.objects.all(),
        source="address",
        required=False,
        allow_null=True,
    )
    delivery_option = serializers.ChoiceField(
        choices=Order.DeliveryOption.choices,
        required=False,
        default=Order.DeliveryOption.HOME_DELIVERY,
    )
    pickup_station_id = serializers.PrimaryKeyRelatedField(
        queryset=PickupStation.objects.filter(is_active=True),
        source="pickup_station",
        required=False,
        allow_null=True,
    )
    customer_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    customer_email = serializers.EmailField(required=False, allow_blank=True)
    customer_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    street_name = serializers.CharField(
        source="address_street_name",
        max_length=255,
        required=False,
        allow_blank=True,
    )
    city = serializers.CharField(
        source="address_city",
        max_length=100,
        required=False,
        allow_blank=True,
    )
    area = serializers.CharField(
        source="address_area",
        max_length=100,
        required=False,
        allow_blank=True,
    )
    region = serializers.ChoiceField(
        source="address_region",
        choices=CustomerAddress.Region.choices,
        required=False,
        allow_blank=True,
    )
    additional_information = serializers.CharField(
        source="address_additional_information",
        required=False,
        allow_blank=True,
    )
    description = serializers.CharField(required=False, allow_blank=True)
    coupon_code = serializers.CharField(
        max_length=50,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    payment_method = serializers.ChoiceField(
        choices=Payment.Provider.choices,
        required=False,
        default=Payment.Provider.CASH,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = getattr(self.context.get("request"), "tenant", None)
        queryset = PickupStation.objects.filter(is_active=True)
        if tenant is not None:
            queryset = queryset.filter(Q(tenant=tenant) | Q(tenant__isnull=True))
        self.fields["pickup_station_id"].queryset = queryset

    def validate_customer_name(self, value: str) -> str:
        return value.strip()

    def validate_customer_email(self, value: str) -> str:
        return value.strip().lower()

    def validate_customer_phone(self, value: str) -> str:
        return value.strip()

    def validate_street_name(self, value: str) -> str:
        return value.strip()

    def validate_city(self, value: str) -> str:
        return value.strip()

    def validate_area(self, value: str) -> str:
        return value.strip()

    def validate_payment_method(self, value: str) -> str:
        enabled_methods = set(
            getattr(settings, "ENABLED_CHECKOUT_PAYMENT_METHODS", ["CASH"])
        )
        if value != Payment.Provider.CASH or value not in enabled_methods:
            raise serializers.ValidationError(
                "Use the dedicated payment initiation endpoint for online payments."
            )
        return value

    def validate_coupon_code(self, value: str) -> str:
        return value.strip().upper()

    def validate(self, attrs):
        request = self.context["request"]
        address = attrs.get("address")
        delivery_option = attrs.get(
            "delivery_option",
            Order.DeliveryOption.HOME_DELIVERY,
        )
        pickup_station = attrs.get("pickup_station")
        errors = {}

        if delivery_option == Order.DeliveryOption.PICKUP_STATION:
            if pickup_station is None:
                errors["pickup_station_id"] = "Pickup station is required."
            attrs["address"] = None
        else:
            attrs["pickup_station"] = None

        if getattr(request.user, "is_authenticated", False):
            if delivery_option != Order.DeliveryOption.PICKUP_STATION and address is None:
                errors["address_id"] = "Address is required."
            if errors:
                raise serializers.ValidationError(errors)

            if address is not None:
                address = _validate_order_address_owner(self, address)
            attrs["customer_name"] = (
                request.user.get_full_name().strip()
                or request.user.email
                or request.user.username
            )
            attrs["customer_email"] = request.user.email
            attrs["customer_phone"] = getattr(address, "phone_number", "") or attrs.get("customer_phone", "")
            attrs["address_street_name"] = getattr(address, "street_name", "")
            attrs["address_city"] = getattr(address, "city", "")
            attrs["address_area"] = getattr(address, "area", "")
            attrs["address_region"] = getattr(address, "region", "")
            attrs["address_additional_information"] = getattr(address, "additional_information", "")
            return attrs

        if address is not None:
            errors["address_id"] = "Guest checkout does not support saved addresses."

        required_fields = {
            "customer_name": "customer_name",
            "customer_email": "customer_email",
            "customer_phone": "customer_phone",
        }
        if delivery_option != Order.DeliveryOption.PICKUP_STATION:
            required_fields.update(
                {
                    "address_street_name": "street_name",
                    "address_city": "city",
                    "address_region": "region",
                }
            )
        for attr_name, field_name in required_fields.items():
            if not attrs.get(attr_name):
                errors[field_name] = "This field is required."

        if errors:
            raise serializers.ValidationError(errors)

        return attrs


class OrderStatusTransitionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Order.Status.choices)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)


class OrderWriteSerializer(serializers.ModelSerializer):
    address_id = serializers.PrimaryKeyRelatedField(
        queryset=CustomerAddress.objects.all(),
        source="address",
    )

    class Meta:
        model = Order
        fields = (
            "slug",
            "status",
            "description",
            "address_id",
        )

    def validate_address_id(self, value: CustomerAddress) -> CustomerAddress:
        return _validate_order_address_owner(self, value)

    def validate(self, attrs):
        address = attrs.get("address") or getattr(self.instance, "address", None)
        if address is None:
            raise serializers.ValidationError({"address_id": "Address is required."})
        return attrs

    def validate_status(self, value: str) -> str:
        request = self.context["request"]
        if not user_is_tenant_staff(request.user, getattr(request, "tenant", None)):
            raise serializers.ValidationError(
                "Only tenant staff can set order status directly."
            )
        return value
