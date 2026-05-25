import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from rest_framework.test import APIClient

from apps.addresses.models import CustomerAddress
from apps.cart.models import Cart, CartItem
from apps.orders.models import Order
from apps.payments.models import Payment
from apps.products.models import Category, Product, ProductVariant
from apps.promotions.models import Coupon
from apps.shipping.models import DeliveryRate, PickupStation, ShippingMethod
from apps.tenants.models import Tenant, TenantMembership

User = get_user_model()


class AdminPaymentTenantScopeTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_superuser(
            email="payments-admin@example.com",
            username="payments-admin",
            password="pass123456",
        )
        self.tenant = Tenant.objects.create(name="Tenant A", slug="tenant-a", is_active=True)
        self.other_tenant = Tenant.objects.create(name="Tenant B", slug="tenant-b", is_active=True)
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.admin,
            role=TenantMembership.Role.SUPER_ADMIN,
        )
        self.client.force_authenticate(user=self.admin)
        self.payment = Payment.objects.create(
            tenant=self.tenant,
            provider=Payment.Provider.CASH,
            status=Payment.Status.PENDING,
            amount="1000.00",
            currency=Payment.Currency.UGX,
        )
        self.other_payment = Payment.objects.create(
            tenant=self.other_tenant,
            provider=Payment.Provider.CASH,
            status=Payment.Status.PENDING,
            amount="2000.00",
            currency=Payment.Currency.UGX,
        )

    def test_admin_payment_list_defaults_to_active_tenant(self):
        response = self.client.get(
            "/api/v1/admin/payments/",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([item["id"] for item in response.data], [self.payment.id])
        self.assertEqual(response.data[0]["tenant_slug"], self.tenant.slug)

    def test_global_admin_can_opt_into_all_tenants(self):
        response = self.client.get(
            "/api/v1/admin/payments/",
            {"all_tenants": "true"},
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertCountEqual(
            [item["id"] for item in response.data],
            [self.payment.id, self.other_payment.id],
        )

    def test_admin_payment_detail_defaults_to_active_tenant(self):
        response = self.client.patch(
            f"/api/v1/admin/payments/{self.other_payment.id}/",
            {"status": Payment.Status.FAILED},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 404)


class PaymentFinalizeSafetyTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="pay@example.com", username="payer", password="pass123456")
        self.client.force_authenticate(user=self.user)
        self.tenant = Tenant.objects.create(name="Payments Tenant", slug="payments-tenant", is_active=True)
        self.category = Category.objects.create(tenant=self.tenant, name="Food", slug="food")
        self.product = Product.objects.create(tenant=self.tenant, category=self.category, title="Beans", slug="beans")
        self.variant = ProductVariant.objects.create(
            tenant=self.tenant,
            product=self.product,
            name="1kg",
            sku="beans-1kg",
            price="1000.00",
            stock_quantity=10,
        )
        self.address = CustomerAddress.objects.create(
            user=self.user,
            street_name="Plot 9",
            city="Kampala",
            region=CustomerAddress.Region.KAMPALA_AREA,
        )
        self.cart = Cart.objects.create(user=self.user)
        self.create_delivery_rate(fee="0.00")

    def create_delivery_rate(self, *, fee: str, city: str = "", area: str = "", estimated_days: int = 1):
        return DeliveryRate.objects.create(
            tenant=self.tenant,
            region=CustomerAddress.Region.KAMPALA_AREA,
            city=city,
            area=area,
            fee=fee,
            estimated_days=estimated_days,
            is_active=True,
        )

    def create_discount_and_shipping(self):
        shipping_method = ShippingMethod.objects.create(
            name="Kampala delivery",
            fee="500.00",
            estimated_days=1,
            is_active=True,
        )
        delivery_rate = self.create_delivery_rate(
            city="Kampala",
            fee="500.00",
            estimated_days=1,
        )
        coupon = Coupon.objects.create(
            tenant=self.tenant,
            code="SAVE300",
            discount_type=Coupon.DiscountType.FIXED,
            value="300.00",
            starts_at=timezone.now() - timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1),
            is_active=True,
        )
        return coupon, shipping_method, delivery_rate

    @override_settings(
        ENABLE_MOMO=True,
        SUBSCRIPTION_KEY="test-subscription",
        MOMO_API_USER="test-user",
        MOMO_API_KEY="test-key",
        MOMO_BASE_URL="https://momo.example.test",
        MOMO_CURRENCY=Payment.Currency.UGX,
    )
    @patch("apps.payments.services.request_to_pay")
    def test_mtn_initiation_uses_coupon_and_shipping_total(self, request_to_pay):
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
            unit_price="1000.00",
        )
        coupon, shipping_method, delivery_rate = self.create_discount_and_shipping()
        request_to_pay.return_value = {
            "reference_id": "mtn-reference",
            "status_code": 202,
            "data": {},
        }

        response = self.client.post(
            "/api/v1/payments/mtn/initiate/",
            {
                "address_id": self.address.id,
                "phone_number": "0772123456",
                "coupon_code": "save300",
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 201, response.data)
        payment = Payment.objects.get(reference=response.data["reference"])
        coupon.refresh_from_db()

        self.assertEqual(payment.amount, Decimal("2200.00"))
        self.assertEqual(coupon.used_count, 0)
        self.assertEqual(payment.provider_response["cart_total"], "2000.00")
        self.assertEqual(payment.provider_response["checkout_summary"]["discount"], "300.00")
        self.assertEqual(payment.provider_response["checkout_summary"]["shipping"], "500.00")
        self.assertEqual(payment.provider_response["checkout_summary"]["total"], "2200.00")
        self.assertEqual(payment.provider_response["checkout_summary"]["coupon_code"], coupon.code)
        self.assertEqual(
            payment.provider_response["checkout_summary"]["delivery_rate_id"],
            delivery_rate.id,
        )
        self.assertEqual(
            payment.provider_response["checkout_summary"]["shipping_method_id"],
            shipping_method.id,
        )
        request_to_pay.assert_called_once()
        self.assertEqual(request_to_pay.call_args.kwargs["amount"], Decimal("2200.00"))

    @override_settings(
        ENABLE_MOMO=True,
        SUBSCRIPTION_KEY="test-subscription",
        MOMO_API_USER="test-user",
        MOMO_API_KEY="test-key",
        MOMO_BASE_URL="https://momo.example.test",
        MOMO_CURRENCY=Payment.Currency.UGX,
    )
    @patch("apps.payments.services.request_to_pay")
    def test_mtn_initiation_idempotency_replays_existing_payment(self, request_to_pay):
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
            unit_price="1000.00",
        )
        request_to_pay.return_value = {
            "reference_id": "mtn-reference",
            "status_code": 202,
            "data": {},
        }
        payload = {
            "address_id": self.address.id,
            "phone_number": "0772123456",
        }
        headers = {
            "HTTP_X_TENANT_SLUG": self.tenant.slug,
            "HTTP_IDEMPOTENCY_KEY": "payment-initiate-key-001",
        }

        first_response = self.client.post(
            "/api/v1/payments/mtn/initiate/",
            payload,
            format="json",
            **headers,
        )
        second_response = self.client.post(
            "/api/v1/payments/mtn/initiate/",
            payload,
            format="json",
            **headers,
        )

        self.assertEqual(first_response.status_code, 201, first_response.data)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(first_response.data["reference"], second_response.data["reference"])
        self.assertEqual(Payment.objects.count(), 1)
        request_to_pay.assert_called_once()

        payment = Payment.objects.get(reference=first_response.data["reference"])
        self.assertEqual(payment.provider_response["idempotency_key"], "payment-initiate-key-001")

    @override_settings(
        ENABLE_MOMO=True,
        SUBSCRIPTION_KEY="test-subscription",
        MOMO_API_USER="test-user",
        MOMO_API_KEY="test-key",
        MOMO_BASE_URL="https://momo.example.test",
        MOMO_CURRENCY=Payment.Currency.UGX,
    )
    @patch("apps.payments.services.request_to_pay")
    def test_mtn_initiation_with_pickup_station_uses_zero_checkout_fee(self, request_to_pay):
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
            unit_price="1000.00",
        )
        pickup_station = PickupStation.objects.create(
            tenant=self.tenant,
            name="Ntinda Pickup",
            city="Kampala",
            area="Ntinda",
            address="Ntinda Complex",
            is_active=True,
        )
        request_to_pay.return_value = {
            "reference_id": "mtn-pickup-reference",
            "status_code": 202,
            "data": {},
        }

        response = self.client.post(
            "/api/v1/payments/mtn/initiate/",
            {
                "address_id": self.address.id,
                "phone_number": "0772123456",
                "delivery_option": Order.DeliveryOption.PICKUP_STATION,
                "pickup_station_id": pickup_station.id,
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 201, response.data)
        payment = Payment.objects.get(reference=response.data["reference"])
        self.assertEqual(payment.amount, Decimal("2000.00"))
        self.assertEqual(
            payment.provider_response["checkout_summary"]["delivery_option"],
            Order.DeliveryOption.PICKUP_STATION,
        )
        self.assertEqual(
            payment.provider_response["checkout_summary"]["pickup_station_id"],
            pickup_station.id,
        )
        self.assertIsNone(
            payment.provider_response["checkout_summary"]["shipping_method_id"]
        )
        self.assertIsNone(
            payment.provider_response["checkout_summary"]["delivery_rate_id"]
        )
        request_to_pay.assert_called_once()
        self.assertEqual(request_to_pay.call_args.kwargs["amount"], Decimal("2000.00"))

    @override_settings(
        ENABLE_CARD_PAYMENTS=True,
        CARD_PAYMENT_GATEWAY="placeholder",
        CARD_PAYMENT_GATEWAY_CHECKOUT_URL="https://payments.example.test/checkout",
    )
    def test_card_initiation_creates_placeholder_without_sensitive_card_data(self):
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
            unit_price="1000.00",
        )

        response = self.client.post(
            "/api/v1/payments/card/initiate/",
            {
                "address_id": self.address.id,
                "cardholder_name": "Jane Buyer",
                "card_last4": "4242",
                "expiry_month": 12,
                "expiry_year": timezone.now().year + 1,
                "billing_email": "jane@example.com",
                "billing_phone": "0772123456",
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["checkout_url"], "https://payments.example.test/checkout")
        payment = Payment.objects.get(reference=response.data["reference"])
        provider_response = json.dumps(payment.provider_response)

        self.assertEqual(payment.provider, Payment.Provider.CARD)
        self.assertEqual(payment.status, Payment.Status.PROCESSING)
        self.assertEqual(payment.amount, Decimal("2000.00"))
        self.assertEqual(payment.provider_response["card"]["last4"], "4242")
        self.assertNotIn("card_number", provider_response)
        self.assertNotIn("cvv", provider_response)
        self.assertNotIn("4242424242424242", provider_response)

    @override_settings(
        ENABLE_CARD_PAYMENTS=True,
        CARD_PAYMENT_GATEWAY="placeholder",
        CARD_PAYMENT_GATEWAY_CHECKOUT_URL="https://payments.example.test/checkout",
    )
    def test_card_initiation_rejects_raw_card_number_and_cvv(self):
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
            unit_price="1000.00",
        )

        response = self.client.post(
            "/api/v1/payments/card/initiate/",
            {
                "address_id": self.address.id,
                "cardholder_name": "Jane Buyer",
                "card_last4": "4242",
                "expiry_month": 12,
                "expiry_year": timezone.now().year + 1,
                "card_number": "4242424242424242",
                "cvv": "123",
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("PCI-compliant gateway", str(response.data))
        self.assertFalse(Payment.objects.exists())

    @override_settings(
        ENABLE_CARD_PAYMENTS=True,
        CARD_PAYMENT_GATEWAY="placeholder",
        CARD_PAYMENT_GATEWAY_CHECKOUT_URL="https://payments.example.test/checkout",
    )
    def test_card_initiation_idempotency_replays_existing_payment(self):
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
            unit_price="1000.00",
        )
        payload = {
            "address_id": self.address.id,
            "cardholder_name": "Jane Buyer",
            "card_last4": "4242",
            "expiry_month": 12,
            "expiry_year": timezone.now().year + 1,
        }
        headers = {
            "HTTP_X_TENANT_SLUG": self.tenant.slug,
            "HTTP_IDEMPOTENCY_KEY": "card-initiate-key-001",
        }

        first_response = self.client.post(
            "/api/v1/payments/card/initiate/",
            payload,
            format="json",
            **headers,
        )
        second_response = self.client.post(
            "/api/v1/payments/card/initiate/",
            payload,
            format="json",
            **headers,
        )

        self.assertEqual(first_response.status_code, 201, first_response.data)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(first_response.data["reference"], second_response.data["reference"])
        self.assertEqual(Payment.objects.count(), 1)

    @override_settings(
        ENABLE_CARD_PAYMENTS=True,
        CARD_PAYMENT_GATEWAY="placeholder",
        CARD_PAYMENT_GATEWAY_CHECKOUT_URL="",
    )
    def test_card_initiation_requires_gateway_configuration(self):
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
            unit_price="1000.00",
        )

        response = self.client.post(
            "/api/v1/payments/card/initiate/",
            {
                "address_id": self.address.id,
                "cardholder_name": "Jane Buyer",
                "card_last4": "4242",
                "expiry_month": 12,
                "expiry_year": timezone.now().year + 1,
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Card payments are not configured", str(response.data))
        self.assertFalse(Payment.objects.exists())

    def test_finalize_paid_payment_applies_stored_coupon_and_shipping_total(self):
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
            unit_price="1000.00",
        )
        coupon, shipping_method, delivery_rate = self.create_discount_and_shipping()
        payment = Payment.objects.create(
            tenant=self.tenant,
            user=self.user,
            provider=Payment.Provider.MTN,
            status=Payment.Status.PAID,
            amount="2200.00",
            currency=Payment.Currency.UGX,
            provider_response={
                "address_id": self.address.id,
                "cart_snapshot": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 2,
                        "unit_price": "1000.00",
                    }
                ],
                "cart_total": "2000.00",
                "checkout_summary": {
                    "items_subtotal": "2000.00",
                    "discount": "300.00",
                    "shipping": "500.00",
                    "total": "2200.00",
                    "coupon_id": coupon.id,
                    "coupon_code": coupon.code,
                    "delivery_rate_id": delivery_rate.id,
                    "estimated_days": delivery_rate.estimated_days,
                    "shipping_method_id": shipping_method.id,
                },
            },
        )

        response = self.client.post(
            f"/api/v1/payments/{payment.reference}/finalize-order/",
            {},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(slug=response.data["order"]["slug"])
        payment.refresh_from_db()
        coupon.refresh_from_db()
        self.variant.refresh_from_db()

        self.assertEqual(order.items_subtotal, Decimal("2000.00"))
        self.assertEqual(order.discount_amount, Decimal("300.00"))
        self.assertEqual(order.shipping_fee, Decimal("500.00"))
        self.assertEqual(order.total_price, Decimal("2200.00"))
        self.assertEqual(payment.amount, order.total_price)
        self.assertEqual(payment.order, order)
        self.assertEqual(coupon.used_count, 1)
        self.assertEqual(self.variant.stock_quantity, 8)

    def test_finalize_paid_payment_reuses_stored_pickup_station(self):
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
            unit_price="1000.00",
        )
        pickup_station = PickupStation.objects.create(
            tenant=self.tenant,
            name="Naalya Pickup",
            city="Kampala",
            area="Naalya",
            address="Metroplex",
            is_active=True,
        )
        payment = Payment.objects.create(
            tenant=self.tenant,
            user=self.user,
            provider=Payment.Provider.MTN,
            status=Payment.Status.PAID,
            amount="2000.00",
            currency=Payment.Currency.UGX,
            provider_response={
                "address_id": self.address.id,
                "cart_snapshot": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 2,
                        "unit_price": "1000.00",
                    }
                ],
                "cart_total": "2000.00",
                "checkout_summary": {
                    "items_subtotal": "2000.00",
                    "discount": "0.00",
                    "shipping": "0.00",
                    "total": "2000.00",
                    "delivery_option": Order.DeliveryOption.PICKUP_STATION,
                    "coupon_id": None,
                    "coupon_code": "",
                    "delivery_rate_id": None,
                    "estimated_days": None,
                    "shipping_method_id": None,
                    "pickup_station_id": pickup_station.id,
                },
            },
        )

        response = self.client.post(
            f"/api/v1/payments/{payment.reference}/finalize-order/",
            {},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(slug=response.data["order"]["slug"])
        payment.refresh_from_db()
        self.variant.refresh_from_db()

        self.assertEqual(order.delivery_option, Order.DeliveryOption.PICKUP_STATION)
        self.assertEqual(order.pickup_station, pickup_station)
        self.assertEqual(order.items_subtotal, Decimal("2000.00"))
        self.assertEqual(order.discount_amount, Decimal("0.00"))
        self.assertEqual(order.shipping_fee, Decimal("0.00"))
        self.assertEqual(order.total_price, Decimal("2000.00"))
        self.assertEqual(payment.amount, order.total_price)
        self.assertEqual(payment.order, order)
        self.assertEqual(self.variant.stock_quantity, 8)

    def test_finalize_paid_order_rejects_changed_cart_amount(self):
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
            unit_price="1000.00",
        )
        payment = Payment.objects.create(
            tenant=self.tenant,
            user=self.user,
            provider=Payment.Provider.MTN,
            status=Payment.Status.PAID,
            amount="1000.00",
            currency=Payment.Currency.UGX,
            provider_response={
                "address_id": self.address.id,
                "cart_snapshot": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 1,
                        "unit_price": "1000.00",
                    }
                ],
            },
        )

        response = self.client.post(
            f"/api/v1/payments/{payment.reference}/finalize-order/",
            {},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_finalize_paid_order_rejects_wrong_tenant(self):
        other_tenant = Tenant.objects.create(name="Other Payments Tenant", slug="other-payments-tenant", is_active=True)
        payment = Payment.objects.create(
            tenant=other_tenant,
            user=self.user,
            provider=Payment.Provider.MTN,
            status=Payment.Status.PAID,
            amount="1000.00",
            currency=Payment.Currency.UGX,
            provider_response={"address_id": self.address.id},
        )

        response = self.client.post(
            f"/api/v1/payments/{payment.reference}/finalize-order/",
            {},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["detail"], "Payment not found.")
        self.assertEqual(response.data["errors"], {})
        self.assertEqual(response.data["code"], "not_found")
        self.assertEqual(Order.objects.count(), 0)

    def test_finalize_paid_order_with_insufficient_stock_does_not_create_order(self):
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
            unit_price="1000.00",
        )
        payment = Payment.objects.create(
            tenant=self.tenant,
            user=self.user,
            provider=Payment.Provider.MTN,
            status=Payment.Status.PAID,
            amount="2000.00",
            currency=Payment.Currency.UGX,
            provider_response={
                "address_id": self.address.id,
                "cart_snapshot": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 2,
                        "unit_price": "1000.00",
                    }
                ],
            },
        )
        ProductVariant.objects.filter(pk=self.variant.pk).update(stock_quantity=1)

        response = self.client.post(
            f"/api/v1/payments/{payment.reference}/finalize-order/",
            {},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)
        payment.refresh_from_db()
        self.assertIsNone(payment.order)
        self.assertEqual(CartItem.objects.count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock_quantity, 1)

    def test_finalize_paid_payment_returns_existing_advanced_order(self):
        order = Order.objects.create(
            tenant=self.tenant,
            user=self.user,
            address=self.address,
            status=Order.Status.SHIPPED,
            slug="paid-existing-order",
            total_price="1000.00",
        )
        payment = Payment.objects.create(
            tenant=self.tenant,
            user=self.user,
            order=order,
            provider=Payment.Provider.MTN,
            status=Payment.Status.PAID,
            amount="1000.00",
            currency=Payment.Currency.UGX,
            provider_response={"address_id": self.address.id},
        )

        response = self.client.post(
            f"/api/v1/payments/{payment.reference}/finalize-order/",
            {},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(response.data["order"]["slug"], order.slug)

    def test_paid_linked_payment_requires_matching_order_total(self):
        order = Order.objects.create(
            tenant=self.tenant,
            user=self.user,
            address=self.address,
            status=Order.Status.PENDING,
            slug="pending-mismatch-order",
            total_price="2000.00",
        )
        payment = Payment.objects.create(
            tenant=self.tenant,
            user=self.user,
            order=order,
            provider=Payment.Provider.MTN,
            status=Payment.Status.PROCESSING,
            amount="1000.00",
            currency=Payment.Currency.UGX,
            provider_response={"address_id": self.address.id},
        )

        payment.status = Payment.Status.PAID
        with self.assertRaises(DjangoValidationError):
            payment.save()

        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(payment.status, Payment.Status.PROCESSING)

    def test_successful_linked_payment_updates_payment_status_not_order_status(self):
        order = Order.objects.create(
            tenant=self.tenant,
            user=self.user,
            address=self.address,
            status=Order.Status.PENDING,
            slug="pending-paid-order",
            total_price="1000.00",
        )
        payment = Payment.objects.create(
            tenant=self.tenant,
            user=self.user,
            order=order,
            provider=Payment.Provider.MTN,
            status=Payment.Status.PROCESSING,
            amount="1000.00",
            currency=Payment.Currency.UGX,
            provider_response={"address_id": self.address.id},
        )

        payment.status = Payment.Status.PAID
        payment.save()

        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PAID)
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertIsNotNone(payment.paid_at)

    def test_failed_payment_does_not_change_order_status(self):
        order = Order.objects.create(
            tenant=self.tenant,
            user=self.user,
            address=self.address,
            status=Order.Status.PENDING,
            slug="pending-failed-order",
            total_price="1000.00",
        )
        payment = Payment.objects.create(
            tenant=self.tenant,
            user=self.user,
            order=order,
            provider=Payment.Provider.MTN,
            status=Payment.Status.PROCESSING,
            amount="1000.00",
            currency=Payment.Currency.UGX,
            provider_response={"address_id": self.address.id},
        )

        payment.status = Payment.Status.FAILED
        payment.save()

        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.FAILED)
        self.assertEqual(order.status, Order.Status.PENDING)

    def test_finalize_existing_paid_payment_rejects_order_amount_mismatch(self):
        order = Order.objects.create(
            tenant=self.tenant,
            user=self.user,
            address=self.address,
            status=Order.Status.PENDING,
            slug="paid-mismatch-order",
            total_price="2000.00",
        )
        payment = Payment.objects.create(
            tenant=self.tenant,
            user=self.user,
            order=order,
            provider=Payment.Provider.CARD,
            status=Payment.Status.PROCESSING,
            amount="1000.00",
            currency=Payment.Currency.UGX,
            provider_response={"address_id": self.address.id},
        )
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.PAID)

        response = self.client.post(
            f"/api/v1/payments/{payment.reference}/finalize-order/",
            {},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("amount does not match", str(response.data))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)

    def test_finalize_paid_payment_is_replay_safe_after_order_created(self):
        CartItem.objects.create(
            cart=self.cart,
            variant=self.variant,
            quantity=2,
            unit_price="1000.00",
        )
        payment = Payment.objects.create(
            tenant=self.tenant,
            user=self.user,
            provider=Payment.Provider.MTN,
            status=Payment.Status.PAID,
            amount="2000.00",
            currency=Payment.Currency.UGX,
            provider_response={
                "address_id": self.address.id,
                "cart_snapshot": [
                    {
                        "variant_id": self.variant.id,
                        "quantity": 2,
                        "unit_price": "1000.00",
                    }
                ],
            },
        )

        first_response = self.client.post(
            f"/api/v1/payments/{payment.reference}/finalize-order/",
            {},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        second_response = self.client.post(
            f"/api/v1/payments/{payment.reference}/finalize-order/",
            {},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(first_response.data["order"]["slug"], second_response.data["order"]["slug"])
        self.assertEqual(CartItem.objects.count(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock_quantity, 8)
