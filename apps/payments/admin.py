from django.contrib import admin, messages
from django.utils.html import format_html

from .models import Payment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "user",
        "order",
        "order_status_display",
        "provider",
        "status_badge",
        "amount",
        "currency",
        "phone_number",
        "created_at",
        "paid_at",
    )

    list_filter = (
        "provider",
        "status",
        "currency",
        "created_at",
        "paid_at",
    )

    search_fields = (
        "reference",
        "transaction_id",
        "external_id",
        "phone_number",
        "order__slug",
        "user__email",
        "user__username",
    )

    readonly_fields = (
        "reference",
        "transaction_id",
        "external_id",
        "provider_response",
        "created_at",
        "updated_at",
        "paid_at",
        "address_id_display",
        "order_status_display",
    )

    fieldsets = (
        ("Basic Info", {
            "fields": (
                "reference",
                "user",
                "tenant",
                "order",
                "order_status_display",
                "provider",
                "status",
            )
        }),
        ("Payment Details", {
            "fields": (
                "amount",
                "currency",
                "phone_number",
                "external_id",
                "transaction_id",
            )
        }),
        ("Address Info", {
            "fields": (
                "address_id_display",
            )
        }),
        ("Timestamps", {
            "fields": (
                "created_at",
                "updated_at",
                "paid_at",
            )
        }),
        ("Provider Response", {
            "fields": ("provider_response",),
        }),
    )

    ordering = ("-created_at",)

    def save_model(self, request, obj, form, change):
        previous_status = None
        if change and obj.pk:
            previous_status = (
                Payment.objects.filter(pk=obj.pk)
                .values_list("status", flat=True)
                .first()
            )

        super().save_model(request, obj, form, change)

        if previous_status != Payment.Status.PAID and obj.status == Payment.Status.PAID:
            self.message_user(
                request,
                f"Payment {obj.reference} marked as PAID.",
                level=messages.SUCCESS,
            )

    def status_badge(self, obj):
        colors = {
            "PENDING": "#f59e0b",
            "PROCESSING": "#3b82f6",
            "PAID": "#10b981",
            "FAILED": "#ef4444",
            "REFUNDED": "#8b5cf6",
            "CANCELLED": "#6b7280",
        }
        color = colors.get(obj.status, "#6b7280")

        return format_html(
            '<span style="color: white; background: {}; padding: 4px 8px; border-radius: 6px;">{}</span>',
            color,
            obj.status,
        )

    status_badge.short_description = "Payment Status"

    def order_status_display(self, obj):
        if not obj.order_id:
            return "-"
        return obj.order.status

    order_status_display.short_description = "Order Status"

    def address_id_display(self, obj):
        return obj.provider_response.get("address_id") if obj.provider_response else None

    address_id_display.short_description = "Address ID"
