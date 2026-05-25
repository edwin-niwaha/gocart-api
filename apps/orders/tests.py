from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

from apps.addresses.models import CustomerAddress
from apps.cart.models import Cart, CartItem
from apps.orders.models import Order
from apps.orders.tasks import (
    _run_email_task,
    send_admin_order_status_email_task,
    send_customer_order_status_email_task,
    send_new_order_admin_email_task,
    send_order_confirmation_email_task,
    send_order_push_notification_task,
)
from apps.payments.models import Payment
from apps.products.models import Category, Product, ProductVariant
from apps.promotions.models import Coupon
from apps.shipping.models import DeliveryRate, PickupStation, ShippingMethod
from apps.tenants.models import Tenant, TenantMembership

User = get_user_model()


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True, ENABLE_EMAIL=False)
class OrderTenantCheckoutTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="buyer@example.com", username="buyer", password="secret123")
        self.client.force_authenticate(user=self.user)
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a", is_active=True, is_default=True)
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b", is_active=True)
        self.category_a = Category.objects.create(tenant=self.tenant_a, name="Fruits", slug="fruits")
        self.category_b = Category.objects.create(tenant=self.tenant_b, name="Veg", slug="veg")
        self.product_a = Product.objects.create(tenant=self.tenant_a, category=self.category_a, title="Apple", slug="apple")
        self.product_b = Product.objects.create(tenant=self.tenant_b, category=self.category_b, title="Carrot", slug="carrot")
        self.variant_a = ProductVariant.objects.create(tenant=self.tenant_a, product=self.product_a, name="1kg", sku="A1", price="1000.00", stock_quantity=10)
        self.variant_b = ProductVariant.objects.create(tenant=self.tenant_b, product=self.product_b, name="1kg", sku="B1", price="2000.00", stock_quantity=10)
        self.address = CustomerAddress.objects.create(user=self.user, street_name="Plot 1", city="Kampala", region=CustomerAddress.Region.KAMPALA_AREA)
        self.cart = Cart.objects.create(user=self.user)
        self.create_delivery_rate(fee="0.00")

    def create_delivery_rate(self, *, fee: str, city: str = "", area: str = "", estimated_days: int = 1):
        return DeliveryRate.objects.create(
            tenant=self.tenant_a,
            region=CustomerAddress.Region.KAMPALA_AREA,
            city=city,
            area=area,
            fee=fee,
            estimated_days=estimated_days,
            is_active=True,
        )

    def test_checkout_uses_active_tenant_items_only(self):
        CartItem.objects.create(cart=self.cart, variant=self.variant_a, quantity=2, unit_price="1000.00")
        CartItem.objects.create(cart=self.cart, variant=self.variant_b, quantity=1, unit_price="2000.00")

        response = self.client.post(
            "/api/v1/orders/checkout/",
            {"address_id": self.address.id, "description": "checkout"},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )
        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(slug=response.data["order"]["slug"])
        self.assertEqual(order.tenant, self.tenant_a)
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.items.first().variant, self.variant_a)

    def test_checkout_snapshots_variant_cost_for_profit_reporting(self):
        self.variant_a.unit_cost = Decimal("600.00")
        self.variant_a.save(update_fields=["unit_cost"])
        CartItem.objects.create(cart=self.cart, variant=self.variant_a, quantity=2, unit_price="1000.00")

        response = self.client.post(
            "/api/v1/orders/checkout/",
            {"address_id": self.address.id, "payment_method": Payment.Provider.CASH},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )

        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(slug=response.data["order"]["slug"])
        item = order.items.get()
        self.assertEqual(item.cost_price_snapshot, Decimal("600.00"))
        self.assertEqual(item.line_cost_total, Decimal("1200.00"))
        self.assertEqual(item.gross_profit, Decimal("800.00"))

    def test_checkout_snapshots_product_cost_when_variant_cost_is_missing(self):
        self.product_a.cost_price = Decimal("700.00")
        self.product_a.save(update_fields=["cost_price"])
        self.variant_a.unit_cost = Decimal("0.00")
        self.variant_a.save(update_fields=["unit_cost"])
        CartItem.objects.create(cart=self.cart, variant=self.variant_a, quantity=2, unit_price="1000.00")

        response = self.client.post(
            "/api/v1/orders/checkout/",
            {"address_id": self.address.id, "payment_method": Payment.Provider.CASH},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )

        self.assertEqual(response.status_code, 201, response.data)
        order = Order.objects.get(slug=response.data["order"]["slug"])
        item = order.items.get()
        self.assertEqual(item.cost_price_snapshot, Decimal("700.00"))
        self.assertEqual(item.line_cost_total, Decimal("1400.00"))
        self.assertEqual(item.gross_profit, Decimal("600.00"))

    def test_checkout_applies_coupon_and_shipping_to_order_payment_total(self):
        CartItem.objects.create(cart=self.cart, variant=self.variant_a, quantity=2, unit_price="1000.00")
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
            tenant=self.tenant_a,
            code="SAVE300",
            discount_type=Coupon.DiscountType.FIXED,
            value="300.00",
            starts_at=timezone.now() - timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1),
            is_active=True,
        )

        response = self.client.post(
            "/api/v1/orders/checkout/",
            {
                "address_id": self.address.id,
                "coupon_code": "save300",
                "payment_method": Payment.Provider.CASH,
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )

        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(slug=response.data["order"]["slug"])
        payment = Payment.objects.get(order=order)
        coupon.refresh_from_db()

        self.assertEqual(order.items_subtotal, Decimal("2000.00"))
        self.assertEqual(order.discount_amount, Decimal("300.00"))
        self.assertEqual(order.shipping_fee, Decimal("500.00"))
        self.assertEqual(order.total_price, Decimal("2200.00"))
        self.assertEqual(payment.amount, order.total_price)
        self.assertEqual(coupon.used_count, 1)
        self.assertEqual(payment.provider_response["checkout_summary"]["items_subtotal"], "2000.00")
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

    def test_checkout_pickup_station_uses_zero_checkout_fee_and_sets_delivery_option(self):
        CartItem.objects.create(cart=self.cart, variant=self.variant_a, quantity=2, unit_price="1000.00")
        pickup_station = PickupStation.objects.create(
            tenant=self.tenant_a,
            name="Wandegeya Pickup",
            city="Kampala",
            area="Wandegeya",
            address="Bombo Road Stage",
            is_active=True,
        )

        response = self.client.post(
            "/api/v1/orders/checkout/",
            {
                "address_id": self.address.id,
                "delivery_option": Order.DeliveryOption.PICKUP_STATION,
                "pickup_station_id": pickup_station.id,
                "payment_method": Payment.Provider.CASH,
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )

        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(slug=response.data["order"]["slug"])
        payment = Payment.objects.get(order=order)

        self.assertEqual(order.delivery_option, Order.DeliveryOption.PICKUP_STATION)
        self.assertEqual(order.pickup_station, pickup_station)
        self.assertEqual(order.items_subtotal, Decimal("2000.00"))
        self.assertEqual(order.discount_amount, Decimal("0.00"))
        self.assertEqual(order.shipping_fee, Decimal("0.00"))
        self.assertEqual(order.total_price, Decimal("2000.00"))
        self.assertEqual(order.pickup_station_name, pickup_station.name)
        self.assertEqual(order.pickup_station_city, pickup_station.city)
        self.assertEqual(order.pickup_station_area, pickup_station.area)
        self.assertEqual(order.pickup_station_address, pickup_station.address)
        self.assertEqual(payment.amount, order.total_price)
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

    def test_checkout_replay_after_cart_consumed_does_not_create_duplicate_order(self):
        CartItem.objects.create(cart=self.cart, variant=self.variant_a, quantity=2, unit_price="1000.00")

        first_response = self.client.post(
            "/api/v1/orders/checkout/",
            {"address_id": self.address.id, "description": "first checkout"},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )
        second_response = self.client.post(
            "/api/v1/orders/checkout/",
            {"address_id": self.address.id, "description": "second checkout"},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second_response.status_code, 400)
        self.assertEqual(Order.objects.count(), 1)
        self.variant_a.refresh_from_db()
        self.assertEqual(self.variant_a.stock_quantity, 8)

    def test_checkout_idempotency_replays_order_and_charges_shipping_once(self):
        CartItem.objects.create(cart=self.cart, variant=self.variant_a, quantity=2, unit_price="1000.00")
        variant_c = ProductVariant.objects.create(
            tenant=self.tenant_a,
            product=self.product_a,
            name="500g",
            sku="A2",
            price="1000.00",
            stock_quantity=10,
        )
        CartItem.objects.create(cart=self.cart, variant=variant_c, quantity=1, unit_price="1000.00")
        shipping_method = ShippingMethod.objects.create(
            name="Kampala express",
            fee="5000.00",
            estimated_days=1,
            is_active=True,
        )
        delivery_rate = self.create_delivery_rate(
            city="Kampala",
            fee="5000.00",
            estimated_days=1,
        )
        payload = {
            "address_id": self.address.id,
            "payment_method": Payment.Provider.CASH,
        }
        headers = {
            "HTTP_X_TENANT_SLUG": self.tenant_a.slug,
            "HTTP_IDEMPOTENCY_KEY": "checkout-test-key-001",
        }

        first_response = self.client.post(
            "/api/v1/orders/checkout/",
            payload,
            format="json",
            **headers,
        )
        second_response = self.client.post(
            "/api/v1/orders/checkout/",
            payload,
            format="json",
            **headers,
        )

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(first_response.data["order"]["slug"], second_response.data["order"]["slug"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)

        order = Order.objects.get(slug=first_response.data["order"]["slug"])
        payment = Payment.objects.get(order=order)

        self.assertEqual(order.items.count(), 2)
        self.assertEqual(order.items_subtotal, Decimal("3000.00"))
        self.assertEqual(order.discount_amount, Decimal("0.00"))
        self.assertEqual(order.shipping_fee, Decimal("5000.00"))
        self.assertEqual(order.total_price, Decimal("8000.00"))
        self.assertEqual(payment.amount, order.total_price)
        self.assertEqual(payment.provider_response["checkout_summary"]["items_subtotal"], "3000.00")
        self.assertEqual(payment.provider_response["checkout_summary"]["shipping"], "5000.00")
        self.assertEqual(payment.provider_response["checkout_summary"]["total"], "8000.00")
        self.assertEqual(payment.provider_response["idempotency_key"], "checkout-test-key-001")
        self.assertEqual(
            payment.provider_response["checkout_summary"]["delivery_rate_id"],
            delivery_rate.id,
        )
        self.assertEqual(
            payment.provider_response["checkout_summary"]["shipping_method_id"],
            shipping_method.id,
        )

    def test_checkout_insufficient_stock_does_not_create_order_or_payment(self):
        CartItem.objects.create(cart=self.cart, variant=self.variant_a, quantity=2, unit_price="1000.00")
        ProductVariant.objects.filter(pk=self.variant_a.pk).update(stock_quantity=1)

        response = self.client.post(
            "/api/v1/orders/checkout/",
            {"address_id": self.address.id, "description": "stock check"},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Insufficient stock for Apple (1kg)")
        self.assertEqual(response.data["errors"], {})
        self.assertEqual(response.data["code"], "validation_error")
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(CartItem.objects.count(), 1)
        self.variant_a.refresh_from_db()
        self.assertEqual(self.variant_a.stock_quantity, 1)

    @override_settings(ENABLED_CHECKOUT_PAYMENT_METHODS=["CASH", "MTN"])
    def test_checkout_rejects_online_payment_methods(self):
        CartItem.objects.create(cart=self.cart, variant=self.variant_a, quantity=2, unit_price="1000.00")

        response = self.client.post(
            "/api/v1/orders/checkout/",
            {
                "address_id": self.address.id,
                "description": "mtn checkout",
                "payment_method": Payment.Provider.MTN,
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("dedicated payment initiation endpoint", str(response.data))
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_checkout_summary_uses_address_based_delivery_rate(self):
        CartItem.objects.create(cart=self.cart, variant=self.variant_a, quantity=2, unit_price="1000.00")
        delivery_rate = self.create_delivery_rate(
            city="Kampala",
            fee="1500.00",
            estimated_days=2,
        )

        response = self.client.get(
            "/api/v1/checkout/summary/",
            {
                "address_id": self.address.id,
                "delivery_option": Order.DeliveryOption.HOME_DELIVERY,
            },
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["items_subtotal"], "2000.00")
        self.assertEqual(response.data["shipping"], "1500.00")
        self.assertEqual(response.data["total"], "3500.00")
        self.assertEqual(response.data["delivery_rate_id"], delivery_rate.id)
        self.assertEqual(response.data["estimated_days"], 2)

    def test_checkout_returns_validation_error_when_no_delivery_rate_matches(self):
        CartItem.objects.create(cart=self.cart, variant=self.variant_a, quantity=1, unit_price="1000.00")
        DeliveryRate.objects.filter(tenant=self.tenant_a).delete()

        response = self.client.post(
            "/api/v1/orders/checkout/",
            {"address_id": self.address.id, "payment_method": Payment.Provider.CASH},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["detail"],
            "Delivery is not available for this location.",
        )
        self.assertEqual(response.data["code"], "validation_error")
        self.assertFalse(Order.objects.exists())

    def test_tenant_staff_can_checkout_own_cart(self):
        staff = User.objects.create_user(email="checkout-staff@example.com", username="checkoutstaff", password="secret123")
        TenantMembership.objects.create(tenant=self.tenant_a, user=staff, role=TenantMembership.Role.STAFF)
        staff_address = CustomerAddress.objects.create(
            user=staff,
            street_name="Plot 88",
            city="Kampala",
            region=CustomerAddress.Region.KAMPALA_AREA,
        )
        staff_cart = Cart.objects.create(user=staff)
        CartItem.objects.create(cart=staff_cart, variant=self.variant_a, quantity=1, unit_price="1000.00")

        self.client.force_authenticate(user=staff)
        response = self.client.post(
            "/api/v1/orders/checkout/",
            {"address_id": staff_address.id, "description": "seller buying for self"},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )

        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(slug=response.data["order"]["slug"])
        self.assertEqual(order.user, staff)
        self.assertEqual(order.items.count(), 1)
        self.assertFalse(CartItem.objects.filter(cart=staff_cart).exists())

    def test_tenant_staff_cannot_checkout_with_customer_address(self):
        staff = User.objects.create_user(email="checkout-staff-2@example.com", username="checkoutstaff2", password="secret123")
        other_user = User.objects.create_user(email="other-buyer@example.com", username="otherbuyer", password="secret123")
        TenantMembership.objects.create(tenant=self.tenant_a, user=staff, role=TenantMembership.Role.STAFF)
        other_address = CustomerAddress.objects.create(
            user=other_user,
            street_name="Plot 99",
            city="Kampala",
            region=CustomerAddress.Region.KAMPALA_AREA,
        )
        staff_cart = Cart.objects.create(user=staff)
        CartItem.objects.create(cart=staff_cart, variant=self.variant_a, quantity=1, unit_price="1000.00")

        self.client.force_authenticate(user=staff)
        response = self.client.post(
            "/api/v1/orders/checkout/",
            {"address_id": other_address.id, "description": "invalid address"},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant_a.slug,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Address not found.")
        self.assertEqual(response.data["code"], "validation_error")
        self.assertFalse(Order.objects.filter(user=staff).exists())


class OrderStaffVisibilityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.customer = User.objects.create_user(email="buyer2@example.com", username="buyer2", password="secret123")
        self.staff = User.objects.create_user(email="staff@example.com", username="staff", password="secret123")
        self.tenant = Tenant.objects.create(name="Tenant Staff", slug="tenant-staff", is_active=True, is_default=True)
        TenantMembership.objects.create(tenant=self.tenant, user=self.staff, role=TenantMembership.Role.STAFF)
        address = CustomerAddress.objects.create(user=self.customer, street_name="Plot 2", city="Kampala", region=CustomerAddress.Region.KAMPALA_AREA)
        self.order = Order.objects.create(user=self.customer, tenant=self.tenant, address=address, status=Order.Status.PENDING, slug="ord-staff")

    def test_staff_can_list_tenant_orders(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get("/api/v1/orders/", HTTP_X_TENANT_SLUG=self.tenant.slug)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)


class OrderStatusTransitionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.customer = User.objects.create_user(email="buyer3@example.com", username="buyer3", password="secret123")
        self.staff = User.objects.create_user(email="staff3@example.com", username="staff3", password="secret123")
        self.tenant = Tenant.objects.create(name="Tenant Ops", slug="tenant-ops", is_active=True, is_default=True)
        TenantMembership.objects.create(tenant=self.tenant, user=self.staff, role=TenantMembership.Role.STAFF)
        address = CustomerAddress.objects.create(user=self.customer, street_name="Plot 3", city="Kampala", region=CustomerAddress.Region.KAMPALA_AREA)
        self.order = Order.objects.create(user=self.customer, tenant=self.tenant, address=address, status=Order.Status.PENDING, slug="ord-transition")

    def test_staff_can_transition_order_status(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            f"/api/v1/orders/{self.order.slug}/transition-status/",
            {"status": Order.Status.PROCESSING, "note": "Picked"},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PROCESSING)
        self.assertEqual(self.order.status_events.count(), 1)

    def test_staff_can_confirm_ship_and_deliver_order(self):
        self.client.force_authenticate(user=self.staff)

        for next_status in (
            Order.Status.CONFIRMED,
            Order.Status.PROCESSING,
            Order.Status.SHIPPED,
            Order.Status.DELIVERED,
        ):
            response = self.client.post(
                f"/api/v1/orders/{self.order.slug}/transition-status/",
                {"status": next_status},
                format="json",
                HTTP_X_TENANT_SLUG=self.tenant.slug,
            )
            self.assertEqual(response.status_code, 200, response.data)
            self.order.refresh_from_db()
            self.assertEqual(self.order.status, next_status)

    def test_staff_can_mark_order_delivered_before_payment_is_recognized(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            f"/api/v1/orders/{self.order.slug}/transition-status/",
            {"status": Order.Status.DELIVERED, "note": "Delivered before payment collection"},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.DELIVERED)
        self.assertEqual(response.data["payment_status"], Payment.Status.UNPAID)

    def test_staff_can_deliver_legacy_paid_order_even_without_paid_payment(self):
        self.order.status = Order.Status.PAID
        self.order.save(update_fields=["status", "updated_at"])
        self.client.force_authenticate(user=self.staff)

        response = self.client.post(
            f"/api/v1/orders/{self.order.slug}/transition-status/",
            {"status": Order.Status.DELIVERED, "note": "Delivered legacy paid order"},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.DELIVERED)
        self.assertEqual(response.data["payment_status"], Payment.Status.UNPAID)

    def test_paid_transition_is_kept_for_legacy_clients(self):
        self.order.status = Order.Status.PROCESSING
        self.order.save(update_fields=["status", "updated_at"])
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            f"/api/v1/orders/{self.order.slug}/transition-status/",
            {"status": Order.Status.PAID},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(response.data["order_status"], Order.Status.PROCESSING)

    def test_cancel_unpaid_order(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            f"/api/v1/orders/{self.order.slug}/transition-status/",
            {"status": Order.Status.CANCELLED},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CANCELLED)

    def test_cancel_paid_order_keeps_payment_paid_for_refund_handling(self):
        Payment.objects.create(
            tenant=self.tenant,
            user=self.customer,
            order=self.order,
            provider=Payment.Provider.MTN,
            status=Payment.Status.PAID,
            amount="0.00",
            currency=Payment.Currency.UGX,
        )
        self.client.force_authenticate(user=self.staff)

        response = self.client.post(
            f"/api/v1/orders/{self.order.slug}/transition-status/",
            {"status": Order.Status.CANCELLED},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        payment = self.order.payments.get()
        self.assertEqual(self.order.status, Order.Status.CANCELLED)
        self.assertEqual(payment.status, Payment.Status.PAID)

    def test_customer_cannot_transition_order_status(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.post(
            f"/api/v1/orders/{self.order.slug}/transition-status/",
            {"status": Order.Status.PROCESSING},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        self.assertEqual(response.status_code, 403)

    def test_invalid_transition_returns_specific_message(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            f"/api/v1/orders/{self.order.slug}/transition-status/",
            {"status": Order.Status.REFUNDED},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["detail"],
            "Cannot transition order from PENDING to REFUNDED.",
        )
        self.assertEqual(response.data["code"], "validation_error")


class OrderTaskReliabilityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="task-buyer@example.com", username="taskbuyer", password="secret123")
        self.tenant = Tenant.objects.create(name="Task Tenant", slug="order-task-tenant", is_active=True, is_default=True)
        self.address = CustomerAddress.objects.create(
            user=self.user,
            street_name="Plot 5",
            city="Kampala",
            region=CustomerAddress.Region.KAMPALA_AREA,
        )
        self.order = Order.objects.create(
            user=self.user,
            tenant=self.tenant,
            address=self.address,
            status=Order.Status.PENDING,
            slug="order-task-test",
        )

    def test_order_tasks_have_retry_policy(self):
        for task in (
            send_order_confirmation_email_task,
            send_new_order_admin_email_task,
            send_customer_order_status_email_task,
            send_admin_order_status_email_task,
            send_order_push_notification_task,
        ):
            self.assertEqual(task.autoretry_for, (Exception,))
            self.assertTrue(task.retry_backoff)
            self.assertTrue(task.retry_jitter)
            self.assertEqual(task.retry_backoff_max, 300)
            self.assertEqual(task.max_retries, 5)

    def test_missing_order_email_task_is_skipped_without_sender_call(self):
        sender = Mock()

        _run_email_task(
            order_id=999999,
            action_label="test order email",
            sender=sender,
        )

        sender.assert_not_called()

    def test_order_email_task_reraises_sender_failure_for_celery_retry(self):
        sender = Mock(side_effect=RuntimeError("smtp unavailable"))

        with self.assertRaises(RuntimeError):
            _run_email_task(
                order_id=self.order.id,
                action_label="test order email",
                sender=sender,
                recipient_getter=lambda order: order.user.email,
            )

        sender.assert_called_once_with(self.order)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True, ENABLE_EMAIL=False)
class GuestOrderCheckoutTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = Tenant.objects.create(
            name="Guest Checkout Tenant",
            slug="guest-checkout-tenant",
            is_active=True,
            is_default=True,
        )
        self.category = Category.objects.create(
            tenant=self.tenant,
            name="Bakery",
            slug="bakery",
        )
        self.product = Product.objects.create(
            tenant=self.tenant,
            category=self.category,
            title="Cake",
            slug="cake",
        )
        self.variant = ProductVariant.objects.create(
            tenant=self.tenant,
            product=self.product,
            name="Whole",
            sku="cake-whole",
            price="12000.00",
            stock_quantity=10,
        )
        DeliveryRate.objects.create(
            tenant=self.tenant,
            region=CustomerAddress.Region.KAMPALA_AREA,
            city="",
            area="",
            fee="0.00",
            estimated_days=1,
            is_active=True,
        )

    def test_guest_checkout_creates_guest_order_and_cash_payment(self):
        add_response = self.client.post(
            "/api/v1/cart-items/",
            {"variant_id": self.variant.id, "quantity": 2},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        checkout_response = self.client.post(
            "/api/v1/orders/checkout/",
            {
                "customer_name": "Guest Customer",
                "customer_email": "guest.customer@example.com",
                "customer_phone": "0772123456",
                "street_name": "Plot 44",
                "city": "Kampala",
                "region": "kampala_area",
                "description": "guest checkout",
                "payment_method": Payment.Provider.CASH,
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )

        self.assertEqual(add_response.status_code, 201)
        self.assertEqual(checkout_response.status_code, 201)

        order = Order.objects.get(slug=checkout_response.data["order"]["slug"])
        payment = Payment.objects.get(order=order)

        self.assertIsNone(order.user)
        self.assertTrue(order.guest_session_key)
        self.assertEqual(order.customer_email, "guest.customer@example.com")
        self.assertEqual(order.address_street_name, "Plot 44")
        self.assertEqual(order.items_subtotal, Decimal("24000.00"))
        self.assertEqual(order.discount_amount, Decimal("0.00"))
        self.assertEqual(order.shipping_fee, Decimal("0.00"))
        self.assertEqual(order.total_price, Decimal("24000.00"))
        self.assertIsNone(payment.user)
        self.assertEqual(payment.amount, order.total_price)
        self.assertFalse(CartItem.objects.exists())
