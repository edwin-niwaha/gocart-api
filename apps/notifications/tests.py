from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from unittest.mock import patch

from apps.addresses.models import CustomerAddress
from apps.notifications.models import DeviceToken
from apps.notifications.push import send_push_to_user
from apps.notifications.tasks import _send_order_push_notification, send_order_push_notification_task
from apps.orders.models import Order
from apps.tenants.models import Tenant

User = get_user_model()


@override_settings(ENABLE_FIREBASE=False)
class DeviceTokenApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="test@example.com", username="tester", password="secret123")
        self.tenant = Tenant.objects.create(name="GoCart", slug="gocart", is_active=True, is_default=True)
        self.client.force_authenticate(user=self.user)

    def test_register_device_token(self):
        response = self.client.post(
            "/api/v1/device-tokens/",
            {
                "token": "sample-device-token",
                "platform": "android",
                "device_id": "device-1",
                "app_version": "1.0.0",
            },
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(DeviceToken.objects.filter(token="sample-device-token", user=self.user, tenant=self.tenant, is_active=True).exists())

    def test_unregister_device_token(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token="sample-device-token", platform="android")
        response = self.client.post(
            "/api/v1/device-tokens/unregister/",
            {"token": "sample-device-token"},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DeviceToken.objects.get(token="sample-device-token").is_active)


class HealthChecksTests(TestCase):
    def test_live_endpoint(self):
        response = self.client.get("/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

from apps.notifications.models import Notification
from apps.notifications.services import send_pending_order_admin_notifications
from apps.tenants.models import TenantMembership


class NotificationBroadcastTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.manager = User.objects.create_user(email="manager4@example.com", username="manager4", password="pass123456")
        self.staff = User.objects.create_user(email="staff4@example.com", username="staff4", password="pass123456")
        self.tenant = Tenant.objects.create(name="Notify", slug="notify", is_active=True, is_default=True)
        TenantMembership.objects.create(tenant=self.tenant, user=self.manager, role=TenantMembership.Role.MANAGER)
        TenantMembership.objects.create(tenant=self.tenant, user=self.staff, role=TenantMembership.Role.STAFF)

    def test_manager_can_broadcast_notification(self):
        self.client.force_authenticate(user=self.manager)
        response = self.client.post(
            "/api/v1/notifications/broadcast/",
            {"title": "Update", "message": "Hello team"},
            format="json",
            HTTP_X_TENANT_SLUG=self.tenant.slug,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Notification.objects.filter(tenant=self.tenant).count(), 2)


class PendingOrderAdminNotificationTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(email="buyer@example.com", username="buyer", password="pass123456")
        self.owner = User.objects.create_user(email="owner@example.com", username="owner", password="pass123456")
        self.admin = User.objects.create_user(email="admin@example.com", username="admin", password="pass123456")
        self.manager = User.objects.create_user(email="manager@example.com", username="manager", password="pass123456")
        self.staff = User.objects.create_user(email="staff@example.com", username="staff", password="pass123456")
        self.tenant = Tenant.objects.create(name="Orders Notify", slug="orders-notify", is_active=True, is_default=True)
        TenantMembership.objects.create(tenant=self.tenant, user=self.owner, role=TenantMembership.Role.TENANT_OWNER)
        TenantMembership.objects.create(tenant=self.tenant, user=self.admin, role=TenantMembership.Role.TENANT_ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.manager, role=TenantMembership.Role.MANAGER)
        TenantMembership.objects.create(tenant=self.tenant, user=self.staff, role=TenantMembership.Role.STAFF)
        self.address = CustomerAddress.objects.create(
            user=self.customer,
            street_name="Pending Street",
            city="Kampala",
            region=CustomerAddress.Region.KAMPALA_AREA,
        )

    def test_pending_order_notification_targets_order_admins_only(self):
        order = Order.objects.create(
            user=self.customer,
            tenant=self.tenant,
            address=self.address,
            status=Order.Status.PENDING,
            slug="pending-admin-alert",
        )

        created = send_pending_order_admin_notifications(order=order)

        self.assertEqual(created, 3)
        recipients = set(Notification.objects.values_list("user__email", flat=True))
        self.assertEqual(
            recipients,
            {"owner@example.com", "admin@example.com", "manager@example.com"},
        )
        notification = Notification.objects.get(user=self.admin)
        self.assertEqual(notification.notification_type, Notification.NotificationType.ORDER)
        self.assertEqual(notification.title, "Pending order received")
        self.assertEqual(notification.data["type"], "pending_order")
        self.assertEqual(notification.data["order_slug"], order.slug)


class NotificationTaskReliabilityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="push-buyer@example.com", username="pushbuyer", password="secret123")
        self.tenant = Tenant.objects.create(name="Push Tenant", slug="push-tenant", is_active=True, is_default=True)
        self.address = CustomerAddress.objects.create(
            user=self.user,
            street_name="Plot 10",
            city="Kampala",
            region=CustomerAddress.Region.KAMPALA_AREA,
        )
        self.order = Order.objects.create(
            user=self.user,
            tenant=self.tenant,
            address=self.address,
            status=Order.Status.PROCESSING,
            slug="push-order",
        )

    def test_push_task_has_retry_policy(self):
        self.assertEqual(send_order_push_notification_task.autoretry_for, (Exception,))
        self.assertTrue(send_order_push_notification_task.retry_backoff)
        self.assertTrue(send_order_push_notification_task.retry_jitter)
        self.assertEqual(send_order_push_notification_task.retry_backoff_max, 300)
        self.assertEqual(send_order_push_notification_task.max_retries, 5)

    @patch("apps.notifications.tasks.send_push_to_user")
    def test_missing_order_push_task_skips_without_provider_call(self, send_push):
        result = _send_order_push_notification(999999)

        self.assertEqual(result, {"success": False, "reason": "order missing"})
        send_push.assert_not_called()

    @patch("apps.notifications.tasks.send_push_to_user", side_effect=RuntimeError("fcm unavailable"))
    def test_push_task_reraises_provider_failure_for_celery_retry(self, send_push):
        with self.assertRaises(RuntimeError):
            _send_order_push_notification(self.order.id)

        send_push.assert_called_once()

    @patch("apps.notifications.push.messaging.send_each_for_multicast")
    def test_push_delivery_uses_only_active_tenant_tokens(self, send_multicast):
        other_tenant = Tenant.objects.create(name="Other Push Tenant", slug="other-push-tenant", is_active=True)
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token="tenant-a-token", platform="android")
        DeviceToken.objects.create(user=self.user, tenant=other_tenant, token="tenant-b-token", platform="android")

        send_multicast.return_value.responses = [type("Result", (), {"success": True})()]

        send_push_to_user(
            user=self.user,
            tenant=self.tenant,
            title="Order updated",
            body="Your order changed.",
        )

        send_multicast.assert_called_once()
        message = send_multicast.call_args.args[0]
        self.assertEqual(message.tokens, ["tenant-a-token"])
