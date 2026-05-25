from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from .models import (
    Account,
    AccountingEvent,
    AccountingSettings,
    InventoryMovement,
    JournalEntry,
    JournalLine,
    Refund,
    RefundLine,
)
from .services import record_accounting_event


class LowercaseChoiceCharField(serializers.CharField):
    def __init__(self, *, choices, **kwargs):
        super().__init__(**kwargs)
        self._valid_values = {choice.value for choice in choices}
        self._lowercase_to_value = {
            choice.value.lower(): choice.value
            for choice in choices
        }
        self._lowercase_to_value.update(
            {
                choice.label.lower(): choice.value
                for choice in choices
            }
        )

    def to_internal_value(self, data):
        value = str(data or "").strip()
        if not value and self.allow_blank:
            return ""
        normalized = self._lowercase_to_value.get(value.lower(), value.upper())
        if normalized not in self._valid_values:
            raise serializers.ValidationError("Invalid choice.")
        return normalized

    def to_representation(self, value):
        return str(value or "").strip().lower()


class ReportDateRangeSerializer(serializers.Serializer):
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate(self, attrs):
        date_from = attrs.get("date_from")
        date_to = attrs.get("date_to")
        if date_from and date_to and date_from > date_to:
            raise serializers.ValidationError({"date_to": "date_to must be on or after date_from."})
        return attrs


class ReportAsOfDateSerializer(serializers.Serializer):
    date_to = serializers.DateField(required=False)


class AccountSerializer(serializers.ModelSerializer):
    parent_code = serializers.CharField(source="parent.code", read_only=True)
    account_type = LowercaseChoiceCharField(choices=Account.Type)
    normal_balance = LowercaseChoiceCharField(
        choices=Account.NormalBalance,
        required=False,
        allow_blank=True,
    )

    class Meta:
        model = Account
        fields = (
            "id",
            "tenant",
            "code",
            "name",
            "account_type",
            "parent",
            "parent_code",
            "normal_balance",
            "currency",
            "description",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "tenant", "parent_code", "created_at", "updated_at")

    def validate_account_type(self, value: str) -> str:
        normalized = (value or "").strip().upper()
        valid_values = {choice.value for choice in Account.Type}
        if normalized not in valid_values:
            raise serializers.ValidationError("Invalid account type.")
        return normalized

    def validate_parent(self, value: Account | None) -> Account | None:
        tenant = getattr(self.context.get("request"), "tenant", None)
        if value is not None and tenant is not None and value.tenant_id != tenant.id:
            raise serializers.ValidationError("Parent account does not belong to this tenant.")
        if value is not None and self.instance is not None and value.pk == self.instance.pk:
            raise serializers.ValidationError("Account cannot be its own parent.")
        return value

    def validate(self, attrs):
        if "code" in attrs and attrs["code"]:
            attrs["code"] = attrs["code"].strip().upper()
        tenant = getattr(self.context.get("request"), "tenant", None)
        code = attrs.get("code")
        if tenant is not None and code:
            queryset = Account.objects.filter(tenant=tenant, code=code)
            if self.instance is not None:
                queryset = queryset.exclude(pk=self.instance.pk)
            if queryset.exists():
                raise serializers.ValidationError({"code": "Account code already exists for this tenant."})
        if "normal_balance" not in attrs or not attrs.get("normal_balance"):
            account_type = attrs.get("account_type") or getattr(self.instance, "account_type", "")
            attrs["normal_balance"] = (
                Account.NormalBalance.DEBIT
                if account_type in {Account.Type.ASSET, Account.Type.EXPENSE}
                else Account.NormalBalance.CREDIT
            )
        return attrs

    def create(self, validated_data):
        validated_data.pop("tenant", None)
        try:
            return Account.objects.create(tenant=self.context["request"].tenant, **validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        try:
            instance.save()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)
        return instance


class JournalLineSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code", read_only=True)
    account_name = serializers.CharField(source="account.name", read_only=True)

    class Meta:
        model = JournalLine
        fields = (
            "id",
            "tenant",
            "journal_entry",
            "account",
            "account_code",
            "account_name",
            "description",
            "debit",
            "credit",
            "metadata",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "tenant",
            "journal_entry",
            "account_code",
            "account_name",
            "created_at",
            "updated_at",
        )

    def validate_account(self, value: Account) -> Account:
        tenant = getattr(self.context.get("request"), "tenant", None)
        if tenant is not None and value.tenant_id != tenant.id:
            raise serializers.ValidationError("Account does not belong to this tenant.")
        if not value.is_active:
            raise serializers.ValidationError("Account is inactive.")
        return value


class JournalEntrySerializer(serializers.ModelSerializer):
    lines = JournalLineSerializer(many=True, required=False)
    total_debits = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    total_credits = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = JournalEntry
        fields = (
            "id",
            "tenant",
            "entry_number",
            "entry_date",
            "memo",
            "status",
            "source_model",
            "source_id",
            "idempotency_key",
            "metadata",
            "posted_at",
            "posted_by",
            "reversed_at",
            "reversed_by",
            "reversed_entry",
            "total_debits",
            "total_credits",
            "lines",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "tenant",
            "entry_number",
            "status",
            "posted_at",
            "posted_by",
            "reversed_at",
            "reversed_by",
            "reversed_entry",
            "total_debits",
            "total_credits",
            "created_at",
            "updated_at",
        )
        extra_kwargs = {
            "memo": {"required": False, "allow_blank": True},
            "source_model": {"required": False, "allow_blank": True},
            "source_id": {"required": False, "allow_blank": True},
            "idempotency_key": {"required": False, "allow_blank": True},
        }

    def create(self, validated_data):
        lines_data = validated_data.pop("lines", [])
        request = self.context["request"]
        try:
            with transaction.atomic():
                journal_entry = JournalEntry.objects.create(
                    tenant=request.tenant,
                    **validated_data,
                )
                for line_data in lines_data:
                    JournalLine.objects.create(
                        tenant=request.tenant,
                        journal_entry=journal_entry,
                        **line_data,
                    )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            ) from exc
        return journal_entry

    def update(self, instance: JournalEntry, validated_data):
        lines_data = validated_data.pop("lines", None)
        try:
            with transaction.atomic():
                for attr, value in validated_data.items():
                    setattr(instance, attr, value)
                instance.save()

                if lines_data is not None:
                    instance.lines.all().delete()
                    for line_data in lines_data:
                        JournalLine.objects.create(
                            tenant=instance.tenant,
                            journal_entry=instance,
                            **line_data,
                        )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            ) from exc
        return instance


class JournalEntryReverseSerializer(serializers.Serializer):
    memo = serializers.CharField(max_length=255, required=False, allow_blank=True)


class AccountingSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccountingSettings
        fields = (
            "id",
            "tenant",
            "base_currency",
            "fiscal_year_start_month",
            "lock_posted_entries",
            "require_balanced_entries",
            "metadata",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "tenant", "created_at", "updated_at")

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        try:
            instance.save()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)
        return instance


class AccountingEventSerializer(serializers.ModelSerializer):
    journal_entry_number = serializers.CharField(source="journal_entry.entry_number", read_only=True)

    class Meta:
        model = AccountingEvent
        fields = (
            "id",
            "tenant",
            "event_type",
            "source_model",
            "source_id",
            "idempotency_key",
            "status",
            "payload",
            "error_message",
            "processed_at",
            "journal_entry",
            "journal_entry_number",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "tenant",
            "status",
            "error_message",
            "processed_at",
            "journal_entry",
            "journal_entry_number",
            "created_at",
            "updated_at",
        )

    def create(self, validated_data):
        event, _created = record_accounting_event(
            tenant=self.context["request"].tenant,
            **validated_data,
        )
        return event


class InventoryMovementSerializer(serializers.ModelSerializer):
    variant_sku = serializers.CharField(source="variant.sku", read_only=True)
    product_title = serializers.CharField(source="variant.product.title", read_only=True)

    class Meta:
        model = InventoryMovement
        fields = (
            "id",
            "tenant",
            "variant",
            "variant_sku",
            "product_title",
            "movement_type",
            "quantity",
            "unit_cost",
            "total_cost",
            "source_model",
            "source_id",
            "note",
            "metadata",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "tenant",
            "variant_sku",
            "product_title",
            "total_cost",
            "created_at",
            "updated_at",
        )

    def validate_variant(self, value):
        tenant = getattr(self.context.get("request"), "tenant", None)
        if tenant is not None and value.tenant_id != tenant.id:
            raise serializers.ValidationError("Variant does not belong to this tenant.")
        return value

    def create(self, validated_data):
        return InventoryMovement.objects.create(
            tenant=self.context["request"].tenant,
            **validated_data,
        )


class RefundLineSerializer(serializers.ModelSerializer):
    product_title = serializers.CharField(source="order_item.product_title", read_only=True)
    variant_sku = serializers.CharField(source="order_item.variant_sku", read_only=True)

    class Meta:
        model = RefundLine
        fields = (
            "id",
            "tenant",
            "refund",
            "order_item",
            "product_title",
            "variant_sku",
            "quantity",
            "amount",
            "tax_amount",
            "return_to_stock",
            "metadata",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "tenant",
            "refund",
            "product_title",
            "variant_sku",
            "created_at",
            "updated_at",
        )

    def validate_order_item(self, value):
        tenant = getattr(self.context.get("request"), "tenant", None)
        if tenant is not None and value.tenant_id != tenant.id:
            raise serializers.ValidationError("Order item does not belong to this tenant.")
        return value


class RefundSerializer(serializers.ModelSerializer):
    lines = RefundLineSerializer(many=True, required=False)
    order_slug = serializers.CharField(source="order.slug", read_only=True)
    payment_reference = serializers.CharField(source="payment.reference", read_only=True)

    class Meta:
        model = Refund
        fields = (
            "id",
            "tenant",
            "order",
            "order_slug",
            "payment",
            "payment_reference",
            "status",
            "amount",
            "tax_amount",
            "reason",
            "idempotency_key",
            "metadata",
            "approved_at",
            "approved_by",
            "completed_at",
            "completed_by",
            "lines",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "tenant",
            "status",
            "order_slug",
            "payment_reference",
            "approved_at",
            "approved_by",
            "completed_at",
            "completed_by",
            "created_at",
            "updated_at",
        )

    def validate_order(self, value):
        tenant = getattr(self.context.get("request"), "tenant", None)
        if tenant is not None and value.tenant_id != tenant.id:
            raise serializers.ValidationError("Order does not belong to this tenant.")
        return value

    def validate_payment(self, value):
        tenant = getattr(self.context.get("request"), "tenant", None)
        if value is not None and tenant is not None and value.tenant_id != tenant.id:
            raise serializers.ValidationError("Payment does not belong to this tenant.")
        return value

    def validate(self, attrs):
        order = attrs.get("order") or getattr(self.instance, "order", None)
        payment = attrs.get("payment") or getattr(self.instance, "payment", None)
        amount = attrs.get("amount") or getattr(self.instance, "amount", None)
        tax_amount = attrs.get("tax_amount") or getattr(self.instance, "tax_amount", 0)
        lines = attrs.get("lines", [])

        if payment is not None and order is not None and payment.order_id != order.id:
            raise serializers.ValidationError({"payment": "Payment must belong to the selected order."})
        if amount is not None and order is not None:
            existing_total = Refund.objects.filter(
                tenant=order.tenant,
                order=order,
            ).exclude(status=Refund.Status.VOID)
            if self.instance is not None:
                existing_total = existing_total.exclude(pk=self.instance.pk)
            existing_amount = sum((refund.amount for refund in existing_total), 0)
            if existing_amount + amount > order.total_price:
                raise serializers.ValidationError({"amount": "Refunds cannot exceed the order total."})
        if tax_amount and amount and tax_amount > amount:
            raise serializers.ValidationError({"tax_amount": "Tax amount cannot exceed refund amount."})
        if lines:
            line_total = sum(line["amount"] for line in lines)
            if amount is not None and line_total > amount:
                raise serializers.ValidationError({"lines": "Refund line totals cannot exceed refund amount."})
        return attrs

    def create(self, validated_data):
        lines_data = validated_data.pop("lines", [])
        request = self.context["request"]
        try:
            with transaction.atomic():
                refund = Refund.objects.create(tenant=request.tenant, **validated_data)
                for line_data in lines_data:
                    RefundLine.objects.create(
                        tenant=request.tenant,
                        refund=refund,
                        **line_data,
                    )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            ) from exc
        return refund

    def update(self, instance, validated_data):
        lines_data = validated_data.pop("lines", None)
        try:
            with transaction.atomic():
                for attr, value in validated_data.items():
                    setattr(instance, attr, value)
                instance.save()
                if lines_data is not None:
                    instance.lines.all().delete()
                    for line_data in lines_data:
                        RefundLine.objects.create(
                            tenant=instance.tenant,
                            refund=instance,
                            **line_data,
                        )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            ) from exc
        return instance
