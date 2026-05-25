from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from apps.addresses.models import CustomerAddress
from apps.orders.models import Order
from apps.shipping.models import PickupStation

from .models import Payment


def normalize_order_status(status: str) -> str:
    if status == Order.Status.PAID:
        return Order.Status.PROCESSING
    return status


class PaymentCreateSerializer(serializers.ModelSerializer):
    order = serializers.PrimaryKeyRelatedField(
        queryset=Order.objects.all()
    )

    class Meta:
        model = Payment
        fields = [
            "order",
            "provider",
            "amount",
            "currency",
            "phone_number",
        ]

    def validate_order(self, value):
        request = self.context["request"]

        if value.user != request.user or value.tenant_id != getattr(request.tenant, "id", None):
            raise serializers.ValidationError("Order not found.")

        return value

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Payment amount must be greater than zero.")
        return value

    def validate(self, attrs):
        order = attrs.get("order")
        amount = attrs.get("amount")
        if order is not None and amount is not None and amount != order.total_price:
            raise serializers.ValidationError(
                {"amount": "Payment amount must match the order total."}
            )
        return attrs

    def create(self, validated_data):
        request = self.context["request"]

        return Payment.objects.create(
            tenant=getattr(request, "tenant", None),
            user=request.user,
            status=Payment.Status.PENDING,
            **validated_data,
        )


class MTNInitiatePaymentSerializer(serializers.Serializer):
    address_id = serializers.IntegerField(min_value=1, required=False)
    phone_number = serializers.CharField(max_length=20)
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
    coupon_code = serializers.CharField(
        max_length=50,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = getattr(self.context.get("request"), "tenant", None)
        queryset = PickupStation.objects.filter(is_active=True)
        if tenant is not None:
            queryset = queryset.filter(Q(tenant=tenant) | Q(tenant__isnull=True))
        self.fields["pickup_station_id"].queryset = queryset

    def validate_phone_number(self, value: str) -> str:
        raw = value.strip().replace(" ", "")

        if raw.startswith("+256"):
            normalized = raw
        elif raw.startswith("256"):
            normalized = f"+{raw}"
        elif raw.startswith("0"):
            normalized = f"+256{raw[1:]}"
        else:
            normalized = raw

        if not normalized.startswith("+2567") or len(normalized) != 13:
            raise serializers.ValidationError(
                "Enter a valid Uganda number like 078XXXXXXX or +25678XXXXXXX."
            )

        return normalized

    def validate(self, attrs):
        request = self.context["request"]
        address_id = attrs.get("address_id")
        delivery_option = attrs.get(
            "delivery_option",
            Order.DeliveryOption.HOME_DELIVERY,
        )
        pickup_station = attrs.get("pickup_station")
        address = None

        if delivery_option == Order.DeliveryOption.PICKUP_STATION:
            if pickup_station is None:
                raise serializers.ValidationError({"pickup_station_id": "Pickup station is required."})
        else:
            if not address_id:
                raise serializers.ValidationError({"address_id": "Address is required."})
            attrs["pickup_station"] = None
            try:
                address = CustomerAddress.objects.get(
                    id=address_id,
                    user=request.user,
                )
            except CustomerAddress.DoesNotExist:
                raise serializers.ValidationError(
                    {"address_id": "Address not found."}
                )

        self.context["address_instance"] = address
        return attrs

    def validate_coupon_code(self, value: str) -> str:
        return value.strip().upper()


class CardInitiatePaymentSerializer(serializers.Serializer):
    address_id = serializers.IntegerField(min_value=1, required=False)
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
    coupon_code = serializers.CharField(
        max_length=50,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    gateway = serializers.CharField(max_length=40, required=False, allow_blank=True)
    cardholder_name = serializers.CharField(max_length=120)
    card_last4 = serializers.RegexField(regex=r"^\d{4}$")
    expiry_month = serializers.IntegerField(min_value=1, max_value=12)
    expiry_year = serializers.IntegerField(min_value=2024, max_value=2100)
    billing_email = serializers.EmailField(required=False, allow_blank=True)
    billing_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = getattr(self.context.get("request"), "tenant", None)
        queryset = PickupStation.objects.filter(is_active=True)
        if tenant is not None:
            queryset = queryset.filter(Q(tenant=tenant) | Q(tenant__isnull=True))
        self.fields["pickup_station_id"].queryset = queryset

    def validate_coupon_code(self, value: str) -> str:
        return value.strip().upper()

    def validate_gateway(self, value: str) -> str:
        return value.strip().lower()

    def validate(self, attrs):
        request = self.context["request"]
        address_id = attrs.get("address_id")
        delivery_option = attrs.get(
            "delivery_option",
            Order.DeliveryOption.HOME_DELIVERY,
        )
        pickup_station = attrs.get("pickup_station")
        address = None
        now = timezone.now()
        expiry_year = attrs.get("expiry_year")
        expiry_month = attrs.get("expiry_month")

        if expiry_year < now.year or (
            expiry_year == now.year and expiry_month < now.month
        ):
            raise serializers.ValidationError(
                {"expiry_year": "Card expiry date is in the past."}
            )

        blocked_fields = {"card_number", "number", "pan", "cvv", "cvc", "security_code"}
        leaked_fields = blocked_fields.intersection(set(self.initial_data.keys()))
        if leaked_fields:
            raise serializers.ValidationError(
                {
                    "detail": "Raw card number and CVV must be tokenized by a PCI-compliant gateway, not sent to GoCart.",
                    "blocked_fields": sorted(leaked_fields),
                }
            )

        if delivery_option == Order.DeliveryOption.PICKUP_STATION:
            if pickup_station is None:
                raise serializers.ValidationError({"pickup_station_id": "Pickup station is required."})
        else:
            if not address_id:
                raise serializers.ValidationError({"address_id": "Address is required."})
            attrs["pickup_station"] = None
            try:
                address = CustomerAddress.objects.get(
                    id=address_id,
                    user=request.user,
                )
            except CustomerAddress.DoesNotExist:
                raise serializers.ValidationError(
                    {"address_id": "Address not found."}
                )

        self.context["address_instance"] = address
        return attrs


class PaymentStatusSerializer(serializers.ModelSerializer):
    payment_status = serializers.CharField(source="status", read_only=True)
    order_status = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "reference",
            "provider",
            "status",
            "payment_status",
            "order_status",
            "amount",
            "currency",
            "phone_number",
            "external_id",
            "transaction_id",
            "provider_response",
            "paid_at",
            "created_at",
            "updated_at",
        ]

    def get_order_status(self, obj):
        if not obj.order_id:
            return None
        return normalize_order_status(obj.order.status)


class PaymentListSerializer(serializers.ModelSerializer):
    user_email = serializers.SerializerMethodField()
    order_slug = serializers.CharField(source="order.slug", read_only=True)
    payment_status = serializers.CharField(source="status", read_only=True)

    class Meta:
        model = Payment
        fields = [
            "id",
            "user",
            "user_email",
            "order",
            "order_slug",
            "provider",
            "status",
            "payment_status",
            "currency",
            "amount",
            "phone_number",
            "reference",
            "external_id",
            "transaction_id",
            "checkout_url",
            "provider_response",
            "paid_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_user_email(self, obj):
        return getattr(obj.user, "email", None) or getattr(obj.order, "customer_email", None)



class AdminPaymentSerializer(serializers.ModelSerializer):
    user_email = serializers.SerializerMethodField()
    username = serializers.SerializerMethodField()
    order_slug = serializers.CharField(source="order.slug", read_only=True)
    order_status = serializers.SerializerMethodField()
    payment_status = serializers.CharField(source="status", read_only=True)
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    tenant_slug = serializers.CharField(source="tenant.slug", read_only=True)
    address_id = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id",
            "reference",
            "user",
            "user_email",
            "username",
            "tenant",
            "tenant_name",
            "tenant_slug",
            "order",
            "order_slug",
            "order_status",
            "provider",
            "status",
            "payment_status",
            "amount",
            "currency",
            "phone_number",
            "external_id",
            "transaction_id",
            "provider_response",
            "address_id",
            "created_at",
            "updated_at",
            "paid_at",
        ]
        read_only_fields = [
            "reference",
            "user",
            "user_email",
            "username",
            "tenant",
            "tenant_name",
            "tenant_slug",
            "order",
            "order_slug",
            "order_status",
            "amount",
            "currency",
            "phone_number",
            "external_id",
            "address_id",
            "created_at",
            "updated_at",
            "paid_at",
        ]

    def get_address_id(self, obj):
        return obj.provider_response.get("address_id") if obj.provider_response else None

    def get_order_status(self, obj):
        if not obj.order_id:
            return None
        return normalize_order_status(obj.order.status)

    def get_user_email(self, obj):
        return getattr(obj.user, "email", None) or getattr(obj.order, "customer_email", None)

    def get_username(self, obj):
        return getattr(obj.user, "username", None) or getattr(obj.order, "customer_name", None)

    def validate_provider(self, value):
        allowed = {choice[0] for choice in Payment.Provider.choices}
        if value not in allowed:
            raise serializers.ValidationError("Invalid payment provider.")
        return value

    def validate_status(self, value):
        allowed = {choice[0] for choice in Payment.Status.choices}
        if value not in allowed:
            raise serializers.ValidationError("Invalid payment status.")
        return value

    def validate(self, attrs):
        status = attrs.get("status", getattr(self.instance, "status", None))
        if status == Payment.Status.PAID and self.instance and self.instance.order_id:
            amount = attrs.get("amount", self.instance.amount)
            order_total = self.instance.order.total_price
            if amount != order_total:
                raise serializers.ValidationError(
                    {
                        "amount": "Payment amount must match the linked order total before it can be marked paid."
                    }
                )
        return attrs
