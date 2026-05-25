from __future__ import annotations

import inspect
from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.common.models import TimeStampedModel
from apps.tenants.models import Tenant


ZERO = Decimal("0.00")
MONEY_MAX_DIGITS = 14
MONEY_DECIMAL_PLACES = 2
_CHECK_CONSTRAINT_USES_CONDITION = (
    "condition" in inspect.signature(models.CheckConstraint).parameters
)


def build_check_constraint(*, predicate, name: str) -> models.CheckConstraint:
    kwargs = {"name": name}
    kwargs["condition" if _CHECK_CONSTRAINT_USES_CONDITION else "check"] = predicate
    return models.CheckConstraint(**kwargs)


class Account(TimeStampedModel):
    class Type(models.TextChoices):
        ASSET = "ASSET", "Asset"
        LIABILITY = "LIABILITY", "Liability"
        EQUITY = "EQUITY", "Equity"
        INCOME = "INCOME", "Income"
        EXPENSE = "EXPENSE", "Expense"

    class NormalBalance(models.TextChoices):
        DEBIT = "DEBIT", "Debit"
        CREDIT = "CREDIT", "Credit"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="accounts")
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    account_type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="children",
        null=True,
        blank=True,
    )
    normal_balance = models.CharField(max_length=10, choices=NormalBalance.choices)
    currency = models.CharField(max_length=10, default="UGX")
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["code", "name"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="unique_account_code_per_tenant"),
        ]
        indexes = [
            models.Index(fields=["tenant", "account_type"], name="acct_acc_tenant_type_idx"),
            models.Index(fields=["tenant", "is_active"], name="acct_acc_tenant_active_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"

    def clean(self) -> None:
        if self.code:
            self.code = self.code.strip().upper()
        if self.currency:
            self.currency = self.currency.strip().upper()
        if self.parent_id and self.tenant_id and self.parent.tenant_id != self.tenant_id:
            raise ValidationError({"parent": "Parent account must belong to the same tenant."})
        if self.parent_id and self.pk and self.parent_id == self.pk:
            raise ValidationError({"parent": "Account cannot be its own parent."})


class JournalEntry(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"
        REVERSED = "REVERSED", "Reversed"
        VOID = "VOID", "Void"

    FINAL_STATUSES = {Status.POSTED, Status.REVERSED, Status.VOID}

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="journal_entries")
    entry_number = models.CharField(max_length=50, db_index=True, blank=True)
    entry_date = models.DateField(default=timezone.localdate, db_index=True)
    memo = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    source_model = models.CharField(max_length=120, blank=True, db_index=True)
    source_id = models.CharField(max_length=120, blank=True, db_index=True)
    idempotency_key = models.CharField(max_length=200, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="posted_journal_entries",
        null=True,
        blank=True,
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reversed_journal_entries",
        null=True,
        blank=True,
    )
    reversed_entry = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="reversal_entries",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-entry_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "entry_number"], name="unique_journal_entry_number_per_tenant"),
            models.UniqueConstraint(
                fields=["tenant", "source_model", "source_id", "idempotency_key"],
                name="unique_journal_source_idempotency_per_tenant",
                condition=(
                    ~models.Q(source_model="")
                    & ~models.Q(source_id="")
                    & ~models.Q(idempotency_key="")
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"], name="acct_je_tenant_status_idx"),
            models.Index(fields=["tenant", "entry_date"], name="acct_je_tenant_date_idx"),
            models.Index(fields=["tenant", "source_model", "source_id"], name="acct_je_source_idx"),
        ]

    def __str__(self) -> str:
        return self.entry_number or f"JournalEntry {self.pk}"

    @property
    def is_finalized(self) -> bool:
        return self.status in self.FINAL_STATUSES

    @property
    def total_debits(self) -> Decimal:
        return self.lines.aggregate(
            total=Coalesce(
                Sum("debit"),
                ZERO,
                output_field=models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES),
            )
        )["total"]

    @property
    def total_credits(self) -> Decimal:
        return self.lines.aggregate(
            total=Coalesce(
                Sum("credit"),
                ZERO,
                output_field=models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES),
            )
        )["total"]

    def clean(self) -> None:
        if self.entry_number:
            self.entry_number = self.entry_number.strip().upper()
        if self.idempotency_key:
            self.idempotency_key = self.idempotency_key.strip()

        if not self.pk:
            return

        previous = JournalEntry.objects.filter(pk=self.pk).first()
        if not previous or previous.status not in self.FINAL_STATUSES:
            return

        if getattr(self, "_allow_finalized_update", False):
            return

        protected_fields = {
            "tenant_id",
            "entry_number",
            "entry_date",
            "memo",
            "status",
            "source_model",
            "source_id",
            "idempotency_key",
            "metadata",
            "posted_at",
            "posted_by_id",
            "reversed_at",
            "reversed_by_id",
            "reversed_entry_id",
        }
        changed = [
            field
            for field in protected_fields
            if getattr(previous, field) != getattr(self, field)
        ]
        if changed:
            raise ValidationError("Finalized journal entries cannot be edited directly. Create a reversal or adjustment entry instead.")

    def save(self, *args, **kwargs):
        if not self.entry_number:
            self.entry_number = f"JE-{uuid4().hex[:12].upper()}"
        self.full_clean()
        super().save(*args, **kwargs)
        if getattr(self, "_allow_finalized_update", False):
            delattr(self, "_allow_finalized_update")

    def delete(self, *args, **kwargs):
        if self.is_finalized:
            raise ValidationError("Finalized journal entries cannot be deleted directly.")
        return super().delete(*args, **kwargs)

    def validate_balanced(self) -> None:
        if not self.pk:
            raise ValidationError("Journal entry must be saved before posting.")
        if not self.lines.exists():
            raise ValidationError("Journal entry must have at least one line.")

        total_debits = self.total_debits
        total_credits = self.total_credits
        if total_debits <= ZERO:
            raise ValidationError("Journal entry total must be greater than zero.")
        if total_debits != total_credits:
            raise ValidationError("Journal entry must balance before posting: total debits must equal total credits.")

    @transaction.atomic
    def post(self, *, user=None) -> "JournalEntry":
        locked = JournalEntry.objects.select_for_update().get(pk=self.pk)
        if locked.status != self.Status.DRAFT:
            raise ValidationError("Only draft journal entries can be posted.")
        locked.validate_balanced()
        locked.status = self.Status.POSTED
        locked.posted_at = timezone.now()
        locked.posted_by = user if getattr(user, "is_authenticated", False) else None
        locked._allow_finalized_update = True
        locked.save(update_fields=["status", "posted_at", "posted_by", "updated_at"])
        return locked

    @transaction.atomic
    def reverse(self, *, user=None, memo: str = "") -> "JournalEntry":
        original = JournalEntry.objects.select_for_update().prefetch_related("lines").get(pk=self.pk)
        if original.status != self.Status.POSTED:
            raise ValidationError("Only posted journal entries can be reversed.")

        reversal = JournalEntry.objects.create(
            tenant=original.tenant,
            entry_date=timezone.localdate(),
            memo=memo or f"Reversal of {original.entry_number}",
            source_model=original.source_model or original.__class__.__name__,
            source_id=original.source_id or str(original.pk),
            idempotency_key=f"reversal-{original.pk}-{uuid4().hex[:8]}",
            reversed_entry=original,
            metadata={"reverses_entry_id": original.pk, "reverses_entry_number": original.entry_number},
        )

        for line in original.lines.all():
            JournalLine.objects.create(
                tenant=original.tenant,
                journal_entry=reversal,
                account=line.account,
                description=f"Reversal: {line.description}"[:255],
                debit=line.credit,
                credit=line.debit,
                metadata={"reverses_line_id": line.pk, **(line.metadata or {})},
            )

        posted_reversal = reversal.post(user=user)
        original.status = self.Status.REVERSED
        original.reversed_at = timezone.now()
        original.reversed_by = user if getattr(user, "is_authenticated", False) else None
        original._allow_finalized_update = True
        original.save(update_fields=["status", "reversed_at", "reversed_by", "updated_at"])
        return posted_reversal


class JournalLine(TimeStampedModel):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="journal_lines")
    journal_entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="journal_lines")
    description = models.CharField(max_length=255, blank=True)
    debit = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    credit = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["tenant", "journal_entry"], name="acct_jl_entry_idx"),
            models.Index(fields=["tenant", "account"], name="acct_jl_account_idx"),
        ]
        constraints = [
            build_check_constraint(
                predicate=models.Q(debit__gte=ZERO),
                name="journal_line_debit_nonnegative",
            ),
            build_check_constraint(
                predicate=models.Q(credit__gte=ZERO),
                name="journal_line_credit_nonnegative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.journal_entry.entry_number}: {self.account.code}"

    def clean(self) -> None:
        debit = self.debit or ZERO
        credit = self.credit or ZERO
        if debit > ZERO and credit > ZERO:
            raise ValidationError("A journal line cannot have both debit and credit amounts.")
        if debit == ZERO and credit == ZERO:
            raise ValidationError("A journal line must have either a debit or a credit amount.")
        if self.journal_entry_id and self.tenant_id and self.journal_entry.tenant_id != self.tenant_id:
            raise ValidationError("Journal line tenant must match journal entry tenant.")
        if self.account_id and self.tenant_id and self.account.tenant_id != self.tenant_id:
            raise ValidationError("Journal line account tenant must match journal line tenant.")
        if self.journal_entry_id and self.journal_entry.is_finalized:
            raise ValidationError("Finalized journal entry lines cannot be edited directly.")

    def save(self, *args, **kwargs):
        if self.journal_entry_id and not self.tenant_id:
            self.tenant = self.journal_entry.tenant
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.journal_entry_id and self.journal_entry.is_finalized:
            raise ValidationError("Finalized journal entry lines cannot be deleted directly.")
        return super().delete(*args, **kwargs)


class AccountingSettings(TimeStampedModel):
    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="accounting_settings")
    base_currency = models.CharField(max_length=10, default="UGX")
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=1)
    lock_posted_entries = models.BooleanField(default=True)
    require_balanced_entries = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Accounting settings"
        verbose_name_plural = "Accounting settings"

    def __str__(self) -> str:
        return f"Accounting settings: {self.tenant.slug}"

    def clean(self) -> None:
        if self.base_currency:
            self.base_currency = self.base_currency.strip().upper()
        if not 1 <= self.fiscal_year_start_month <= 12:
            raise ValidationError({"fiscal_year_start_month": "Month must be between 1 and 12."})


class AccountingEvent(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        PROCESSED = "PROCESSED", "Processed"
        FAILED = "FAILED", "Failed"
        IGNORED = "IGNORED", "Ignored"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="accounting_events")
    event_type = models.CharField(max_length=120, db_index=True)
    source_model = models.CharField(max_length=120, db_index=True)
    source_id = models.CharField(max_length=120, db_index=True)
    idempotency_key = models.CharField(max_length=200, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    journal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.SET_NULL,
        related_name="accounting_events",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "event_type", "source_model", "source_id", "idempotency_key"],
                name="unique_accounting_event_source_per_tenant",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "event_type"], name="acct_event_tenant_type_idx"),
            models.Index(fields=["tenant", "status"], name="acct_event_status_idx"),
            models.Index(fields=["tenant", "source_model", "source_id"], name="acct_event_source_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.event_type}: {self.source_model}:{self.source_id}"

    def clean(self) -> None:
        self.event_type = (self.event_type or "").strip()
        self.source_model = (self.source_model or "").strip()
        self.source_id = (self.source_id or "").strip()
        self.idempotency_key = (self.idempotency_key or "").strip()
        if not self.event_type:
            raise ValidationError({"event_type": "Event type is required."})
        if not self.source_model:
            raise ValidationError({"source_model": "Source model is required."})
        if not self.source_id:
            raise ValidationError({"source_id": "Source id is required."})
        if not self.idempotency_key:
            raise ValidationError({"idempotency_key": "Idempotency key is required."})
        if self.journal_entry_id and self.journal_entry.tenant_id != self.tenant_id:
            raise ValidationError({"journal_entry": "Journal entry must belong to the same tenant."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class InventoryMovement(TimeStampedModel):
    class MovementType(models.TextChoices):
        PURCHASE = "PURCHASE", "Purchase"
        SALE = "SALE", "Sale"
        ADJUSTMENT_IN = "ADJUSTMENT_IN", "Adjustment in"
        ADJUSTMENT_OUT = "ADJUSTMENT_OUT", "Adjustment out"
        RETURN = "RETURN", "Return"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="inventory_movements",
    )
    variant = models.ForeignKey(
        "products.ProductVariant",
        on_delete=models.PROTECT,
        related_name="inventory_movements",
    )
    movement_type = models.CharField(
        max_length=30,
        choices=MovementType.choices,
        db_index=True,
    )
    quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        validators=[MinValueValidator(ZERO)],
    )
    total_cost = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        validators=[MinValueValidator(ZERO)],
    )
    source_model = models.CharField(max_length=120, blank=True, db_index=True)
    source_id = models.CharField(max_length=120, blank=True, db_index=True)
    note = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["tenant", "variant"], name="acct_inv_variant_idx"),
            models.Index(fields=["tenant", "movement_type"], name="acct_inv_type_idx"),
            models.Index(fields=["tenant", "source_model", "source_id"], name="acct_inv_source_idx"),
        ]
        constraints = [
            build_check_constraint(
                predicate=models.Q(quantity__gt=0),
                name="inventory_movement_quantity_positive",
            ),
            build_check_constraint(
                predicate=models.Q(unit_cost__gte=ZERO),
                name="inventory_movement_unit_cost_nonnegative",
            ),
            build_check_constraint(
                predicate=models.Q(total_cost__gte=ZERO),
                name="inventory_movement_total_cost_nonnegative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.movement_type} {self.variant_id} x {self.quantity}"

    def clean(self) -> None:
        if self.variant_id and self.tenant_id and self.variant.tenant_id != self.tenant_id:
            raise ValidationError({"variant": "Variant must belong to the same tenant."})
        if self.quantity is not None and self.quantity <= 0:
            raise ValidationError({"quantity": "Quantity must be greater than zero."})
        if self.unit_cost is None:
            self.unit_cost = ZERO
        expected_total = (self.unit_cost or ZERO) * Decimal(self.quantity or 0)
        if self.total_cost in (None, ""):
            self.total_cost = expected_total
        if self.total_cost != expected_total:
            raise ValidationError({"total_cost": "Total cost must equal quantity multiplied by unit cost."})
        self.source_model = (self.source_model or "").strip()
        self.source_id = (self.source_id or "").strip()
        self.note = (self.note or "").strip()

    def save(self, *args, **kwargs):
        if self.variant_id and not self.tenant_id:
            self.tenant = self.variant.tenant
        if self.unit_cost is None:
            self.unit_cost = ZERO
        if self.quantity is not None:
            self.total_cost = (self.unit_cost or ZERO) * Decimal(self.quantity)
        self.full_clean()
        super().save(*args, **kwargs)


class Refund(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"
        COMPLETED = "COMPLETED", "Completed"
        VOID = "VOID", "Void"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="refunds")
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="accounting_refunds")
    payment = models.ForeignKey(
        "payments.Payment",
        on_delete=models.PROTECT,
        related_name="accounting_refunds",
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    amount = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    tax_amount = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    reason = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=200, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="approved_accounting_refunds",
        null=True,
        blank=True,
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="completed_accounting_refunds",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "idempotency_key"], name="unique_refund_idempotency_per_tenant"),
            build_check_constraint(predicate=models.Q(amount__gt=ZERO), name="refund_amount_positive"),
            build_check_constraint(predicate=models.Q(tax_amount__gte=ZERO), name="refund_tax_nonnegative"),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"], name="acct_refund_status_idx"),
            models.Index(fields=["tenant", "order"], name="acct_refund_order_idx"),
            models.Index(fields=["tenant", "payment"], name="acct_refund_payment_idx"),
        ]

    def __str__(self) -> str:
        return f"Refund {self.pk or 'new'} for {self.order_id}"

    @property
    def is_finalized(self) -> bool:
        return self.status in {self.Status.COMPLETED, self.Status.VOID}

    def clean(self) -> None:
        if self.order_id and self.tenant_id and self.order.tenant_id != self.tenant_id:
            raise ValidationError({"order": "Order must belong to the same tenant."})
        if self.payment_id and self.tenant_id and self.payment.tenant_id != self.tenant_id:
            raise ValidationError({"payment": "Payment must belong to the same tenant."})
        if self.payment_id and self.order_id and self.payment.order_id != self.order_id:
            raise ValidationError({"payment": "Payment must belong to the selected order."})
        if self.amount is not None and self.tax_amount is not None and self.tax_amount > self.amount:
            raise ValidationError({"tax_amount": "Tax amount cannot exceed refund amount."})
        if self.idempotency_key:
            self.idempotency_key = self.idempotency_key.strip()
        if not self.idempotency_key:
            self.idempotency_key = f"refund-{uuid4().hex[:16]}"

        if not self.pk:
            return
        previous = Refund.objects.filter(pk=self.pk).first()
        if not previous or not previous.is_finalized or getattr(self, "_allow_finalized_update", False):
            return
        protected_fields = {
            "tenant_id",
            "order_id",
            "payment_id",
            "status",
            "amount",
            "tax_amount",
            "reason",
            "idempotency_key",
            "metadata",
            "approved_at",
            "approved_by_id",
            "completed_at",
            "completed_by_id",
        }
        changed = [
            field
            for field in protected_fields
            if getattr(previous, field) != getattr(self, field)
        ]
        if changed:
            raise ValidationError("Finalized refunds cannot be edited directly.")

    def save(self, *args, **kwargs):
        if self.order_id and not self.tenant_id:
            self.tenant = self.order.tenant
        self.full_clean()
        super().save(*args, **kwargs)
        if getattr(self, "_allow_finalized_update", False):
            delattr(self, "_allow_finalized_update")


class RefundLine(TimeStampedModel):
    refund = models.ForeignKey(Refund, on_delete=models.CASCADE, related_name="lines")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="refund_lines")
    order_item = models.ForeignKey("orders.OrderItem", on_delete=models.PROTECT, related_name="refund_lines")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    amount = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    tax_amount = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    return_to_stock = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            build_check_constraint(predicate=models.Q(quantity__gt=0), name="refund_line_quantity_positive"),
            build_check_constraint(predicate=models.Q(amount__gt=ZERO), name="refund_line_amount_positive"),
            build_check_constraint(predicate=models.Q(tax_amount__gte=ZERO), name="refund_line_tax_nonnegative"),
        ]
        indexes = [
            models.Index(fields=["tenant", "refund"], name="acct_refline_refund_idx"),
            models.Index(fields=["tenant", "order_item"], name="acct_refline_item_idx"),
        ]

    def clean(self) -> None:
        if self.refund_id and self.tenant_id and self.refund.tenant_id != self.tenant_id:
            raise ValidationError({"tenant": "Refund line tenant must match refund tenant."})
        if self.order_item_id and self.tenant_id and self.order_item.tenant_id != self.tenant_id:
            raise ValidationError({"order_item": "Order item must belong to the same tenant."})
        if self.refund_id and self.order_item_id and self.order_item.order_id != self.refund.order_id:
            raise ValidationError({"order_item": "Order item must belong to the refund order."})
        if self.quantity and self.order_item_id and self.quantity > self.order_item.quantity:
            raise ValidationError({"quantity": "Refund quantity cannot exceed ordered quantity."})
        if self.tax_amount is not None and self.amount is not None and self.tax_amount > self.amount:
            raise ValidationError({"tax_amount": "Tax amount cannot exceed line amount."})
        if self.refund_id and self.refund.is_finalized:
            raise ValidationError("Finalized refund lines cannot be edited directly.")

    def save(self, *args, **kwargs):
        if self.refund_id and not self.tenant_id:
            self.tenant = self.refund.tenant
        self.full_clean()
        super().save(*args, **kwargs)
