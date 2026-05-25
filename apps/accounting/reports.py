from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db.models import QuerySet

from .models import Account, JournalEntry, JournalLine, ZERO


CASH_ACCOUNT_CODES = ("1000", "1010", "1020")
ACCOUNT_SALES_REVENUE = "4000"
ACCOUNT_DELIVERY_INCOME = "4010"
ACCOUNT_DISCOUNTS = "4090"
ACCOUNT_SALES_RETURNS = "4100"
ACCOUNT_VAT_PAYABLE = "2100"
ACCOUNT_COGS = "5000"
ORDER_SOURCE_MODEL = "orders.Order"


def _posted_lines(*, tenant, date_from=None, date_to=None) -> QuerySet[JournalLine]:
    queryset = (
        JournalLine.objects.filter(
            tenant=tenant,
            journal_entry__tenant=tenant,
            journal_entry__status=JournalEntry.Status.POSTED,
        )
        .select_related("account", "journal_entry")
        .order_by("journal_entry__entry_date", "journal_entry__entry_number", "id")
    )
    if date_from is not None:
        queryset = queryset.filter(journal_entry__entry_date__gte=date_from)
    if date_to is not None:
        queryset = queryset.filter(journal_entry__entry_date__lte=date_to)
    return queryset


def _opening_lines(*, tenant, date_from=None) -> QuerySet[JournalLine]:
    queryset = (
        JournalLine.objects.filter(
            tenant=tenant,
            journal_entry__tenant=tenant,
            journal_entry__status=JournalEntry.Status.POSTED,
        )
        .select_related("account", "journal_entry")
        .order_by("journal_entry__entry_date", "journal_entry__entry_number", "id")
    )
    if date_from is not None:
        queryset = queryset.filter(journal_entry__entry_date__lt=date_from)
    else:
        queryset = queryset.none()
    return queryset


def _closing_lines(*, tenant, date_to=None) -> QuerySet[JournalLine]:
    queryset = (
        JournalLine.objects.filter(
            tenant=tenant,
            journal_entry__tenant=tenant,
            journal_entry__status=JournalEntry.Status.POSTED,
        )
        .select_related("account", "journal_entry")
        .order_by("journal_entry__entry_date", "journal_entry__entry_number", "id")
    )
    if date_to is not None:
        queryset = queryset.filter(journal_entry__entry_date__lte=date_to)
    return queryset


def _signed_balance(*, account: Account, debit: Decimal, credit: Decimal) -> Decimal:
    if account.normal_balance == Account.NormalBalance.CREDIT:
        return credit - debit
    return debit - credit


def _debit_credit(balance: Decimal) -> tuple[Decimal, Decimal]:
    if balance >= ZERO:
        return balance, ZERO
    return ZERO, abs(balance)


def _raw_balance(*, debit: Decimal, credit: Decimal) -> Decimal:
    return debit - credit


def _sum_by_account(lines) -> dict[int, dict]:
    buckets = {}
    for line in lines:
        account = line.account
        bucket = buckets.setdefault(
            account.id,
            {
                "account": account,
                "debit": ZERO,
                "credit": ZERO,
            },
        )
        bucket["debit"] += line.debit or ZERO
        bucket["credit"] += line.credit or ZERO
    return buckets


def general_ledger(*, tenant, date_from=None, date_to=None) -> dict:
    opening = _sum_by_account(_opening_lines(tenant=tenant, date_from=date_from))
    accounts = {}

    for line in _posted_lines(tenant=tenant, date_from=date_from, date_to=date_to):
        account = line.account
        opening_bucket = opening.get(account.id, {"debit": ZERO, "credit": ZERO})
        account_bucket = accounts.setdefault(
            account.id,
            {
                "account_id": account.id,
                "account_code": account.code,
                "account_name": account.name,
                "account_type": account.account_type,
                "opening_balance": _signed_balance(
                    account=account,
                    debit=opening_bucket["debit"],
                    credit=opening_bucket["credit"],
                ),
                "period_debits": ZERO,
                "period_credits": ZERO,
                "closing_balance": _signed_balance(
                    account=account,
                    debit=opening_bucket["debit"],
                    credit=opening_bucket["credit"],
                ),
                "lines": [],
            },
        )
        account_bucket["period_debits"] += line.debit or ZERO
        account_bucket["period_credits"] += line.credit or ZERO
        account_bucket["closing_balance"] = (
            account_bucket["opening_balance"]
            + _signed_balance(account=account, debit=account_bucket["period_debits"], credit=account_bucket["period_credits"])
        )
        account_bucket["lines"].append(
            {
                "journal_entry_id": line.journal_entry_id,
                "entry_number": line.journal_entry.entry_number,
                "entry_date": line.journal_entry.entry_date,
                "description": line.description,
                "debit": line.debit,
                "credit": line.credit,
                "source_model": line.journal_entry.source_model,
                "source_id": line.journal_entry.source_id,
                "metadata": line.metadata,
            }
        )

    return {
        "date_from": date_from,
        "date_to": date_to,
        "accounts": sorted(accounts.values(), key=lambda item: item["account_code"]),
    }


def trial_balance(*, tenant, date_from=None, date_to=None) -> dict:
    opening = _sum_by_account(_opening_lines(tenant=tenant, date_from=date_from))
    period = _sum_by_account(_posted_lines(tenant=tenant, date_from=date_from, date_to=date_to))
    account_ids = set(opening) | set(period)
    rows = []
    totals = defaultdict(lambda: ZERO)

    for account in Account.objects.filter(tenant=tenant, id__in=account_ids).order_by("code", "name"):
        opening_bucket = opening.get(account.id, {"debit": ZERO, "credit": ZERO})
        period_bucket = period.get(account.id, {"debit": ZERO, "credit": ZERO})
        opening_balance = _raw_balance(
            debit=opening_bucket["debit"],
            credit=opening_bucket["credit"],
        )
        period_balance = _raw_balance(
            debit=period_bucket["debit"],
            credit=period_bucket["credit"],
        )
        closing_balance = opening_balance + period_balance
        opening_debit, opening_credit = _debit_credit(opening_balance)
        closing_debit, closing_credit = _debit_credit(closing_balance)

        row = {
            "account_id": account.id,
            "account_code": account.code,
            "account_name": account.name,
            "account_type": account.account_type,
            "opening_debit": opening_debit,
            "opening_credit": opening_credit,
            "period_debit": period_bucket["debit"],
            "period_credit": period_bucket["credit"],
            "closing_debit": closing_debit,
            "closing_credit": closing_credit,
        }
        for key in (
            "opening_debit",
            "opening_credit",
            "period_debit",
            "period_credit",
            "closing_debit",
            "closing_credit",
        ):
            totals[key] += row[key]
        rows.append(row)

    return {
        "date_from": date_from,
        "date_to": date_to,
        "rows": rows,
        "totals": dict(totals),
        "is_balanced": totals["closing_debit"] == totals["closing_credit"],
    }


def profit_and_loss(*, tenant, date_from=None, date_to=None) -> dict:
    period = _sum_by_account(
        _posted_lines(tenant=tenant, date_from=date_from, date_to=date_to).filter(
            account__account_type__in=[Account.Type.INCOME, Account.Type.EXPENSE]
        )
    )
    sections = {
        "income": [],
        "expenses": [],
    }
    total_income = ZERO
    total_expenses = ZERO
    cost_of_goods_sold = ZERO
    cogs_rows = []

    for account in Account.objects.filter(tenant=tenant, id__in=period.keys()).order_by("code", "name"):
        bucket = period[account.id]
        amount = _signed_balance(account=account, debit=bucket["debit"], credit=bucket["credit"])
        row = {
            "account_id": account.id,
            "account_code": account.code,
            "account_name": account.name,
            "amount": amount,
        }
        if account.account_type == Account.Type.INCOME:
            sections["income"].append(row)
            total_income += amount
        elif account.account_type == Account.Type.EXPENSE:
            sections["expenses"].append(row)
            total_expenses += amount
            if account.code == ACCOUNT_COGS:
                cost_of_goods_sold += amount
                cogs_rows.append(row)

    if not cogs_rows:
        cogs_account = Account.objects.filter(tenant=tenant, code=ACCOUNT_COGS).first()
        cogs_rows.append(
            {
                "account_id": getattr(cogs_account, "id", None),
                "account_code": ACCOUNT_COGS,
                "account_name": getattr(cogs_account, "name", "Cost of Goods Sold"),
                "amount": ZERO,
            }
        )

    gross_profit = total_income - cost_of_goods_sold
    operating_expenses = total_expenses - cost_of_goods_sold

    return {
        "date_from": date_from,
        "date_to": date_to,
        "revenue": sections["income"],
        "cost_of_goods_sold_rows": cogs_rows,
        "cost_of_goods_sold": cost_of_goods_sold,
        "gross_profit": gross_profit,
        "operating_expenses": operating_expenses,
        "income": sections["income"],
        "expenses": sections["expenses"],
        "total_income": total_income,
        "total_expenses": total_expenses,
        "net_income": gross_profit - operating_expenses,
    }


def balance_sheet(*, tenant, date_to=None) -> dict:
    closing = _sum_by_account(
        _closing_lines(tenant=tenant, date_to=date_to).filter(
            account__account_type__in=[Account.Type.ASSET, Account.Type.LIABILITY, Account.Type.EQUITY, Account.Type.INCOME, Account.Type.EXPENSE]
        )
    )
    assets = []
    liabilities = []
    equity = []
    total_assets = ZERO
    total_liabilities = ZERO
    total_equity = ZERO
    income_total = ZERO
    expense_total = ZERO

    for account in Account.objects.filter(tenant=tenant, id__in=closing.keys()).order_by("code", "name"):
        bucket = closing[account.id]
        amount = _signed_balance(account=account, debit=bucket["debit"], credit=bucket["credit"])
        row = {
            "account_id": account.id,
            "account_code": account.code,
            "account_name": account.name,
            "amount": amount,
        }
        if account.account_type == Account.Type.ASSET:
            assets.append(row)
            total_assets += amount
        elif account.account_type == Account.Type.LIABILITY:
            liabilities.append(row)
            total_liabilities += amount
        elif account.account_type == Account.Type.EQUITY:
            equity.append(row)
            total_equity += amount
        elif account.account_type == Account.Type.INCOME:
            income_total += amount
        elif account.account_type == Account.Type.EXPENSE:
            expense_total += amount

    current_earnings = income_total - expense_total
    if current_earnings != ZERO:
        equity.append(
            {
                "account_id": None,
                "account_code": "CURRENT_EARNINGS",
                "account_name": "Current Earnings",
                "amount": current_earnings,
            }
        )
        total_equity += current_earnings

    return {
        "date_to": date_to,
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "total_equity": total_equity,
        "total_liabilities_and_equity": total_liabilities + total_equity,
        "is_balanced": total_assets == total_liabilities + total_equity,
    }


def cash_flow(*, tenant, date_from=None, date_to=None) -> dict:
    cash_accounts = Account.objects.filter(tenant=tenant, code__in=CASH_ACCOUNT_CODES).order_by("code")
    opening = _sum_by_account(_opening_lines(tenant=tenant, date_from=date_from).filter(account__in=cash_accounts))
    period = _sum_by_account(_posted_lines(tenant=tenant, date_from=date_from, date_to=date_to).filter(account__in=cash_accounts))
    rows = []
    opening_cash = ZERO
    net_change = ZERO

    for account in cash_accounts:
        opening_bucket = opening.get(account.id, {"debit": ZERO, "credit": ZERO})
        period_bucket = period.get(account.id, {"debit": ZERO, "credit": ZERO})
        opening_balance = _signed_balance(
            account=account,
            debit=opening_bucket["debit"],
            credit=opening_bucket["credit"],
        )
        period_change = _signed_balance(
            account=account,
            debit=period_bucket["debit"],
            credit=period_bucket["credit"],
        )
        rows.append(
            {
                "account_id": account.id,
                "account_code": account.code,
                "account_name": account.name,
                "opening_balance": opening_balance,
                "cash_inflows": period_bucket["debit"],
                "cash_outflows": period_bucket["credit"],
                "net_change": period_change,
                "closing_balance": opening_balance + period_change,
            }
        )
        opening_cash += opening_balance
        net_change += period_change

    return {
        "date_from": date_from,
        "date_to": date_to,
        "cash_accounts": rows,
        "opening_cash": opening_cash,
        "net_cash_change": net_change,
        "closing_cash": opening_cash + net_change,
    }


def _account_net(*, tenant, code: str, date_from=None, date_to=None) -> Decimal:
    total = ZERO
    for line in _posted_lines(tenant=tenant, date_from=date_from, date_to=date_to).filter(account__code=code):
        account = line.account
        total += _signed_balance(
            account=account,
            debit=line.debit or ZERO,
            credit=line.credit or ZERO,
        )
    return total


def _posted_sales_order_ids(*, tenant, date_from=None, date_to=None) -> set[int]:
    ids: set[int] = set()
    entries = (
        JournalEntry.objects.filter(
            tenant=tenant,
            status=JournalEntry.Status.POSTED,
            source_model=ORDER_SOURCE_MODEL,
            idempotency_key__startswith="order-paid-",
        )
        .exclude(idempotency_key__endswith="-cogs")
        .values_list("source_id", flat=True)
    )
    if date_from is not None:
        entries = entries.filter(entry_date__gte=date_from)
    if date_to is not None:
        entries = entries.filter(entry_date__lte=date_to)

    for source_id in entries:
        try:
            ids.add(int(source_id))
        except (TypeError, ValueError):
            continue
    return ids


def sales_report(*, tenant, date_from=None, date_to=None) -> dict:
    from apps.orders.models import Order

    gross_sales = _account_net(
        tenant=tenant,
        code=ACCOUNT_SALES_REVENUE,
        date_from=date_from,
        date_to=date_to,
    )
    shipping_income = _account_net(
        tenant=tenant,
        code=ACCOUNT_DELIVERY_INCOME,
        date_from=date_from,
        date_to=date_to,
    )
    discounts = _account_net(
        tenant=tenant,
        code=ACCOUNT_DISCOUNTS,
        date_from=date_from,
        date_to=date_to,
    )
    sales_returns = _account_net(
        tenant=tenant,
        code=ACCOUNT_SALES_RETURNS,
        date_from=date_from,
        date_to=date_to,
    )
    tax_collected = _account_net(
        tenant=tenant,
        code=ACCOUNT_VAT_PAYABLE,
        date_from=date_from,
        date_to=date_to,
    )
    cogs = _account_net(
        tenant=tenant,
        code=ACCOUNT_COGS,
        date_from=date_from,
        date_to=date_to,
    )
    net_sales = gross_sales - discounts - sales_returns
    total_revenue = net_sales + shipping_income
    product_gross_profit = net_sales - cogs
    gross_profit = total_revenue - cogs
    gross_margin = (gross_profit / total_revenue * Decimal("100.00")).quantize(Decimal("0.01")) if total_revenue else ZERO

    order_ids = _posted_sales_order_ids(tenant=tenant, date_from=date_from, date_to=date_to)
    orders = (
        Order.objects.filter(tenant=tenant, id__in=order_ids)
        .prefetch_related("items", "items__product", "payments")
        .order_by("created_at", "id")
    )
    order_rows = []
    product_buckets = {}
    category_buckets = {}
    payment_method_buckets = defaultdict(lambda: {"orders": 0, "amount": ZERO})

    for order in orders:
        paid_payments = [payment for payment in order.payments.all() if payment.status == "PAID"]
        payment_methods = sorted({payment.provider for payment in paid_payments})
        payment_amount = sum((payment.amount or ZERO for payment in paid_payments), ZERO)
        for provider in payment_methods or ["UNPAID"]:
            payment_method_buckets[provider]["orders"] += 1
            payment_method_buckets[provider]["amount"] += payment_amount

        order_rows.append(
            {
                "order_id": order.id,
                "order_slug": order.slug,
                "order_status": order.status,
                "items_subtotal": order.items_subtotal,
                "discount_amount": order.discount_amount,
                "shipping_fee": order.shipping_fee,
                "total_price": order.total_price,
                "payment_methods": payment_methods,
            }
        )

        for item in order.items.all():
            line_sales = item.line_total
            line_cost = item.line_cost_total
            product_key = item.product_id or 0
            product_bucket = product_buckets.setdefault(
                product_key,
                {
                    "product_id": item.product_id,
                    "product_title": item.product_title,
                    "quantity": 0,
                    "gross_sales": ZERO,
                    "cost_of_goods_sold": ZERO,
                    "gross_profit": ZERO,
                },
            )
            product_bucket["quantity"] += item.quantity
            product_bucket["gross_sales"] += line_sales
            product_bucket["cost_of_goods_sold"] += line_cost
            product_bucket["gross_profit"] += line_sales - line_cost

            category = getattr(item.product, "category", None)
            category_key = getattr(category, "id", None) or 0
            category_bucket = category_buckets.setdefault(
                category_key,
                {
                    "category_id": getattr(category, "id", None),
                    "category_name": getattr(category, "name", "Uncategorized"),
                    "quantity": 0,
                    "gross_sales": ZERO,
                    "cost_of_goods_sold": ZERO,
                    "gross_profit": ZERO,
                },
            )
            category_bucket["quantity"] += item.quantity
            category_bucket["gross_sales"] += line_sales
            category_bucket["cost_of_goods_sold"] += line_cost
            category_bucket["gross_profit"] += line_sales - line_cost

    return {
        "date_from": date_from,
        "date_to": date_to,
        "totals": {
            "gross_sales": gross_sales,
            "discounts": discounts,
            "sales_returns": sales_returns,
            "net_sales": net_sales,
            "shipping_income": shipping_income,
            "tax_collected": tax_collected,
            "total_revenue": total_revenue,
            "cost_of_goods_sold": cogs,
            "product_gross_profit": product_gross_profit,
            "gross_profit": gross_profit,
            "gross_margin_percent": gross_margin,
            "posted_orders": len(order_ids),
            "average_order_value": (total_revenue / Decimal(len(order_ids))).quantize(Decimal("0.01")) if order_ids else ZERO,
        },
        "orders": order_rows,
        "by_product": sorted(product_buckets.values(), key=lambda item: item["gross_sales"], reverse=True),
        "by_category": sorted(category_buckets.values(), key=lambda item: item["gross_sales"], reverse=True),
        "by_payment_method": [
            {
                "payment_method": provider,
                "orders": bucket["orders"],
                "amount": bucket["amount"],
            }
            for provider, bucket in sorted(payment_method_buckets.items())
        ],
    }
