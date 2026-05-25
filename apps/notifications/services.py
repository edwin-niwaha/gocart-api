from django.utils import timezone

from apps.tenants.models import TenantMembership
from .models import Notification


def create_notification(
    *,
    user,
    tenant,
    notification_type: str,
    title: str,
    message: str,
    data: dict | None = None,
) -> Notification:
    return Notification.objects.create(
        tenant=tenant,
        user=user,
        notification_type=notification_type,
        title=title,
        message=message,
        data=data or {},
    )


def mark_notification_read(*, notification: Notification) -> Notification:
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at", "updated_at"])
    return notification


def mark_all_notifications_read(*, user, tenant) -> int:
    now = timezone.now()
    updated = Notification.objects.filter(user=user, tenant=tenant, is_read=False).update(
        is_read=True,
        read_at=now,
        updated_at=now,
    )
    return updated


def send_order_notification(*, user, order, title: str, message: str) -> Notification:
    return create_notification(
        user=user,
        tenant=order.tenant,
        notification_type=Notification.NotificationType.ORDER,
        title=title,
        message=message,
        data={"order_slug": order.slug},
    )


def tenant_order_admin_users(*, tenant):
    admin_roles = {
        TenantMembership.Role.SUPER_ADMIN,
        TenantMembership.Role.TENANT_OWNER,
        TenantMembership.Role.TENANT_ADMIN,
        TenantMembership.Role.MANAGER,
    }
    seen = set()
    for membership in (
        TenantMembership.objects.filter(
            tenant=tenant,
            role__in=admin_roles,
            is_active=True,
            user__is_active=True,
        )
        .select_related("user")
        .order_by("user_id")
    ):
        if membership.user_id in seen:
            continue
        seen.add(membership.user_id)
        yield membership.user


def send_pending_order_admin_notifications(*, order) -> int:
    created = 0
    title = "Pending order received"
    message = f"Order {order.slug} is pending confirmation."
    for user in tenant_order_admin_users(tenant=order.tenant):
        create_notification(
            user=user,
            tenant=order.tenant,
            notification_type=Notification.NotificationType.ORDER,
            title=title,
            message=message,
            data={
                "type": "pending_order",
                "order_id": order.id,
                "order_slug": order.slug,
                "order_status": order.status,
            },
        )
        created += 1
    return created


def send_payment_notification(*, user, payment, title: str, message: str) -> Notification:
    return create_notification(
        user=user,
        tenant=payment.order.tenant,
        notification_type=Notification.NotificationType.PAYMENT,
        title=title,
        message=message,
        data={
            "payment_id": payment.id,
            "reference": payment.reference,
            "order_slug": payment.order.slug,
        },
    )


def send_promotion_notification(*, user, coupon, title: str, message: str) -> Notification:
    return create_notification(
        user=user,
        tenant=coupon.tenant,
        notification_type=Notification.NotificationType.PROMOTION,
        title=title,
        message=message,
        data={
            "coupon_code": coupon.code,
        },
    )
