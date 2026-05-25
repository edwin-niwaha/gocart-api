from __future__ import annotations

import logging

from django.db import transaction

from .models import Order
from .tasks import (
    send_admin_order_status_email_task,
    send_customer_order_status_email_task,
    send_new_order_admin_email_task,
    send_order_confirmation_email_task,
    send_order_push_notification_task,
)

logger = logging.getLogger(__name__)

CUSTOMER_NOTIFY_STATUSES = {
    Order.Status.PENDING,
    Order.Status.CONFIRMED,
    Order.Status.PROCESSING,
    Order.Status.PAID,
    Order.Status.SHIPPED,
    Order.Status.DELIVERED,
    Order.Status.CANCELLED,
}

ADMIN_NOTIFY_STATUSES = {
    Order.Status.PENDING,
    Order.Status.CONFIRMED,
    Order.Status.PAID,
    Order.Status.CANCELLED,
    Order.Status.SHIPPED,
}


def queue_order_created_notifications(order_id: int) -> None:
    logger.info("Registering order-created notifications for order_id=%s", order_id)

    def _enqueue() -> None:
        order = Order.objects.filter(id=order_id).select_related("tenant").first()
        logger.info("Enqueuing order-created notifications for order_id=%s", order_id)
        send_order_confirmation_email_task.delay(order_id)
        send_new_order_admin_email_task.delay(order_id)
        if order is not None and order.status == Order.Status.PENDING:
            from apps.notifications.services import send_pending_order_admin_notifications

            created = send_pending_order_admin_notifications(order=order)
            logger.info(
                "Created pending-order admin notifications for order_id=%s count=%s",
                order_id,
                created,
            )

    transaction.on_commit(_enqueue)


def queue_order_status_notifications(order_id: int, old_status: str, new_status: str) -> None:
    if old_status == new_status:
        return

    logger.info(
        "Registering status notifications for order_id=%s old_status=%s new_status=%s",
        order_id,
        old_status,
        new_status,
    )

    def _enqueue() -> None:
        if new_status in CUSTOMER_NOTIFY_STATUSES:
            send_customer_order_status_email_task.delay(order_id)
            send_order_push_notification_task.delay(order_id)

        if new_status in ADMIN_NOTIFY_STATUSES:
            send_admin_order_status_email_task.delay(order_id)
            if new_status == Order.Status.PENDING:
                order = Order.objects.filter(id=order_id).select_related("tenant").first()
                if order is not None:
                    from apps.notifications.services import send_pending_order_admin_notifications

                    created = send_pending_order_admin_notifications(order=order)
                    logger.info(
                        "Created pending-order admin notifications for order_id=%s count=%s",
                        order_id,
                        created,
                    )

    transaction.on_commit(_enqueue)
