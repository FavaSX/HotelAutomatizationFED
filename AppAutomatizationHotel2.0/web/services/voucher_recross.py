"""
Cross ONE saved voucher again against the Bordero rows stored in the database.

The upload cross-match runs over the Excel files in memory. After the accountant corrects, adds or deletes
an ERP row from the Pendientes inbox there is no Excel to re-read, so this module rebuilds the same inputs
from the database and reuses the upload rules:
  - CLP: `_cross_match_sale` of clp_reconciliation (same tolerance, medium and date rules).
  - USD: the ERP -> card operator -> Transbank chain of usd_reconciliation, for one voucher.
"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db.models import Max, Min, Q

from ..models import (
    CardOperatorTransaction,
    CrossMatchResult,
    ErpTransaction,
    ReconciliationProcess,
    SystemConfiguration,
)
from ..templatetags.currency_filters import fmt_clp, fmt_usd
from .clp_reconciliation import _cross_match_sale, get_clp_tolerance
from .column_detector import normalize_auth_code

NOT_LINKED_RESULTS = ("NOT_FOUND_IN_ERP", "OUT_OF_PERIOD")
_PERIOD_RE = re.compile(r"Borderó (\d{2}/\d{2}/\d{4}) al (\d{2}/\d{2}/\d{4})")
_SINGLE_PERIOD_RE = re.compile(r"Borderó (\d{2}/\d{2}/\d{4})")


class RecrossError(ValueError):
    pass


def _rows_with_code(currency: str, code: str) -> List[ErpTransaction]:
    """Active card ERP rows of the currency whose normalized authorization code is `code`."""
    if not code:
        return []
    card_filter = (
        Q(card_type__isnull=False) | Q(payment_code__in=("VUS$", "MCUS$", "MUS$", "AUS$", "DUS$"))
        if currency == "USD"
        else Q(card_type__isnull=False) | Q(payment_code__in=("RCO", "RC", "REDCOMPRA", "V$", "MC$", "AME$", "DIN$"))
    )
    rows = (
        ErpTransaction.objects.filter(source_currency=currency, authorization_code__icontains=code)
        .filter(card_filter)
        .select_related("card_type")
        .order_by("id")
    )
    return [r for r in rows if normalize_auth_code(r.authorization_code or "") == code]


def _claimed_by_other_vouchers(match: CrossMatchResult, row_ids: List[int]) -> set:
    return set(
        CrossMatchResult.objects.filter(erp_transaction_id__in=row_ids)
        .exclude(pk=match.pk)
        .exclude(system_result__in=NOT_LINKED_RESULTS)
        .values_list("erp_transaction_id", flat=True)
    )


# ------------------------------------------------------------------------------
# CLP
# ------------------------------------------------------------------------------

def _clp_period(match: CrossMatchResult):
    period_text = match.process.accounting_period or ""
    found = _PERIOD_RE.search(period_text)
    if found:
        return tuple(datetime.strptime(d, "%d/%m/%Y").date() for d in found.groups())
    single = _SINGLE_PERIOD_RE.search(period_text)
    if single:
        d = datetime.strptime(single.group(1), "%d/%m/%Y").date()
        return d, d
    bounds = (
        match.process.erp_transactions.filter(origin=ErpTransaction.ORIGIN_EXCEL, transaction_date__isnull=False)
        .aggregate(start=Min("transaction_date"), end=Max("transaction_date"))
    )
    return bounds.get("start"), bounds.get("end")


def _clp_row_view(row: ErpTransaction) -> Dict[str, Any]:
    is_redcompra = row.payment_code == "RCO" or not row.card_type_id
    return {
        "amount": float(row.amount),
        "transaction_date": row.transaction_date.strftime("%d/%m/%Y") if row.transaction_date else None,
        "account_code": row.account_code or row.document_number,
        "payment_group": "REDCOMPRA" if is_redcompra else "CREDIT",
        "payment_code": row.payment_code or "",
        "payment_description": "Red Compra" if is_redcompra else (row.card_type.name if row.card_type else row.payment_code),
        "is_reversal": row.amount < 0,
        "invoice_number": row.invoice_number or "",
    }


def _recross_clp(match: CrossMatchResult) -> Dict[str, Any]:
    tbk = match.transbank_transaction
    gross = tbk.original_sale_amount
    sale = {
        "payment_modality": tbk.payment_modality,
        "gross_amount": gross,
        "valid_amount": gross,
        "authorization_code": tbk.authorization_code,
        "sale_date": tbk.sale_date,
        "installment_number": tbk.installment_number or "",
    }
    code = normalize_auth_code(tbk.authorization_code)
    candidates = _rows_with_code("CLP", code)
    claimed_ids = _claimed_by_other_vouchers(match, [r.id for r in candidates])
    rows = [r for r in candidates if r.id not in claimed_ids]
    views = [_clp_row_view(r) for r in rows]

    result = _cross_match_sale(
        sale, views, {code: list(range(len(views)))}, set(), get_clp_tolerance(), _clp_period(match)
    )
    linked = result["linked"]
    primary = None
    if linked:
        primary_idx = min(
            linked,
            key=lambda i: (
                views[i]["is_reversal"] or views[i]["amount"] <= 0,
                abs(gross - Decimal(str(views[i]["amount"]))),
                not bool(views[i]["invoice_number"]),
            ),
        )
        primary = rows[primary_idx]
    return {
        "system_result": result["result"],
        "linked_rows": [rows[i] for i in linked],
        "fields": {
            "erp_transaction": primary,
            "erp_amount_clp": Decimal(str(result["erp_amount"])).quantize(Decimal("0.01")),
            "difference_clp": Decimal(str(result["difference"])).quantize(Decimal("0.01")),
            "account_code": ((primary.account_code or primary.document_number) if primary else (match.account_code or "")) or "",
            "document_number": (primary.authorization_code if primary else match.document_number) or "",
            "system_observation": result["observation"][:255],
        },
        "remaining": fmt_clp(abs(result["difference"])),
    }


# ------------------------------------------------------------------------------
# USD
# ------------------------------------------------------------------------------

def _operator_for(row: ErpTransaction, card_code: str) -> Tuple[Optional[CardOperatorTransaction], str]:
    """Same rule as `_resolve_operator_for_erp`: document number, then the CLP balance."""
    candidates = list(
        CardOperatorTransaction.objects.filter(card_type__code=card_code, document_number=row.document_number).order_by("id")
    )
    for cand in candidates:
        if cand.balance_amount == row.amount:
            return cand, f"Coincide con SALDO {cand.balance_amount}"
        if cand.corrected_balance_amount == row.amount:
            return cand, f"Coincide con SALDO CORREGIDO {cand.corrected_balance_amount}"
    return (candidates[0] if candidates else None), "No encontrado en archivo Operadora"


def _op_clp(op: Optional[CardOperatorTransaction]) -> Decimal:
    if op is None:
        return Decimal("0")
    return op.balance_amount if op.balance_amount > 0 else op.corrected_balance_amount


def _recross_usd(match: CrossMatchResult) -> Dict[str, Any]:
    tbk = match.transbank_transaction
    gross_usd = tbk.original_sale_amount
    card_code = match.card_type.code
    tolerance = SystemConfiguration.get_solo().usd_rounding_tolerance

    code = normalize_auth_code(tbk.authorization_code)
    candidates = _rows_with_code("USD", code)
    claimed_ids = _claimed_by_other_vouchers(match, [r.id for r in candidates])
    rows = [r for r in candidates if r.id not in claimed_ids]
    rows = [r for r in rows if r.invoice_number] or rows

    if not rows:
        return {
            "system_result": "NOT_FOUND_IN_ERP",
            "linked_rows": [],
            "fields": {
                "erp_transaction": None, "card_operator_transaction": None,
                "operator_amount_usd": Decimal("0"), "erp_amount_clp": Decimal("0"),
                "difference_usd": gross_usd, "difference_clp": Decimal("0"),
                "account_code": "", "document_number": "",
                "system_observation": f"Voucher {tbk.authorization_code} no encontrado en ERP Hotel",
            },
            "remaining": fmt_usd(gross_usd),
        }

    resolved = [(row, *_operator_for(row, card_code)) for row in rows]
    single = next(
        (
            c for c in resolved
            if c[0].amount > 0 and c[1] is not None and abs(gross_usd - c[1].foreign_currency_amount) <= tolerance
        ),
        None,
    )
    if single is not None or len(resolved) == 1:
        row, op, observation = single if single is not None else resolved[0]
        linked = [row]
        operator_usd = op.foreign_currency_amount if op else Decimal("0")
        operator_clp = _op_clp(op)
        erp_clp = row.amount
        account_code = row.account_code or row.document_number or ""
        document_number = row.authorization_code or row.document_number or ""
    else:
        primary_cand = next((c for c in resolved if c[0].amount > 0), resolved[0])
        row, op, _ = primary_cand
        linked = [c[0] for c in resolved]
        operator_usd = sum((c[1].foreign_currency_amount for c in resolved if c[1]), Decimal("0"))
        operator_clp = sum((_op_clp(c[1]) for c in resolved), Decimal("0"))
        erp_clp = sum((c[0].amount for c in resolved), Decimal("0"))
        accounts = list(dict.fromkeys((c[0].account_code or c[0].document_number) for c in resolved if (c[0].account_code or c[0].document_number)))
        account_code = " / ".join(accounts)[:50]
        document_number = row.authorization_code or account_code
        observation = (
            f"Voucher dividido en {len(resolved)} facturas (Cta. {account_code}) | Suma SALDO {operator_clp}"
            if all(c[1] for c in resolved) else "No encontrado en archivo Operadora"
        )

    difference_usd = round(gross_usd - operator_usd, 2)
    matched = abs(difference_usd) <= tolerance and op is not None
    return {
        "system_result": "MATCHED" if matched else "AMOUNT_MISMATCH",
        "linked_rows": linked,
        "fields": {
            "erp_transaction": row,
            "card_operator_transaction": op,
            "operator_amount_usd": operator_usd,
            "erp_amount_clp": erp_clp,
            "difference_usd": difference_usd,
            "difference_clp": round(operator_clp - erp_clp, 2),
            "account_code": account_code,
            "document_number": document_number,
            "system_observation": observation[:255],
        },
        "remaining": fmt_usd(abs(difference_usd)),
    }


def recalculate_process_summaries(process: ReconciliationProcess) -> None:
    """
    Recomputes and updates ReconciliationSummary rows of `process` from its current CrossMatchResult rows
    so that summary tabs, Excel exports, and dashboard cards reflect post-upload ERP edits and accepted suggestions.
    """
    if process is None or not process.pk:
        return
    if process.process_currency == "CLP":
        matches = list(process.cross_match_results.select_related("transbank_transaction"))
        for summary in process.summaries.filter(currency_table="CLP"):
            modality = (summary.payment_modality or "").removeprefix("CLP_")
            group = [
                m for m in matches
                if m.card_type_id == summary.card_type_id
                and (m.transbank_transaction.payment_modality if m.transbank_transaction_id else "CREDIT") == modality
            ]
            erp_total = sum((m.erp_amount_clp for m in group), Decimal("0")).quantize(Decimal("0.01"))
            diff = (summary.transbank_sales_total - erp_total).quantize(Decimal("0.01"))
            if summary.erp_sales_total != erp_total or summary.difference != diff:
                summary.erp_sales_total = erp_total
                summary.difference = diff
                summary.save(update_fields=["erp_sales_total", "difference"])
    else:
        matches = list(process.cross_match_results.all())
        for summary in process.summaries.all():
            group = [m for m in matches if m.card_type_id == summary.card_type_id]
            if summary.currency_table == "USD":
                tbk_usd = round(sum((m.transbank_amount for m in group), Decimal("0")), 2)
                op_usd = round(sum((m.operator_amount_usd for m in group), Decimal("0")), 2)
                diff_usd = round(tbk_usd - op_usd, 2)
                if (
                    summary.erp_sales_total != tbk_usd
                    or summary.transbank_sales_total != op_usd
                    or summary.difference != diff_usd
                ):
                    summary.erp_sales_total = tbk_usd
                    summary.transbank_sales_total = op_usd
                    summary.difference = diff_usd
                    summary.save(update_fields=["erp_sales_total", "transbank_sales_total", "difference"])
            elif summary.currency_table == "CLP":
                erp_clp = round(sum((m.erp_amount_clp for m in group), Decimal("0")), 0)
                op_clp = round(sum((m.erp_amount_clp + m.difference_clp for m in group), Decimal("0")), 0)
                diff_clp = round(op_clp - erp_clp, 0)
                if (
                    summary.erp_sales_total != erp_clp
                    or summary.transbank_sales_total != op_clp
                    or summary.difference != diff_clp
                ):
                    summary.erp_sales_total = erp_clp
                    summary.transbank_sales_total = op_clp
                    summary.difference = diff_clp
                    summary.save(update_fields=["erp_sales_total", "transbank_sales_total", "difference"])


def recross_voucher(match: CrossMatchResult) -> Dict[str, Any]:
    """
    Recomputes the cross-match of one voucher from the database and writes the new result on `match`
    (not saved: the caller saves it with the status change, inside its transaction).
    Returns {"system_result", "previous_result", "remaining", "linked_rows"}.
    """
    if match.transbank_transaction_id is None:
        raise RecrossError("Este caso no tiene un voucher de Transbank asociado; no se puede volver a cruzar.")
    outcome = _recross_clp(match) if match.process.process_currency == "CLP" else _recross_usd(match)
    previous = match.system_result
    for field_name, value in outcome["fields"].items():
        setattr(match, field_name, value)
    match.system_result = outcome["system_result"]
    tbk = match.transbank_transaction
    if outcome["system_result"] == "MATCHED":
        for row in outcome["linked_rows"]:
            if not row.reconciled:
                row.reconciled = True
                row.save(update_fields=["reconciled"])
        if not tbk.reconciled:
            tbk.reconciled = True
            tbk.save(update_fields=["reconciled"])
    elif tbk.reconciled:
        other_matched = (
            CrossMatchResult.objects.filter(transbank_transaction=tbk, system_result="MATCHED")
            .exclude(pk=match.pk)
            .exists()
        )
        if not other_matched:
            tbk.reconciled = False
            tbk.save(update_fields=["reconciled"])
    return {**outcome, "previous_result": previous}
