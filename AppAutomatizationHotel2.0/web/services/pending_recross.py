"""
Automatic re-crossing and assisted-suggestion refreshing for open PENDING cases
whenever new ERP or card-operator rows are saved (closing commits or manual/assisted edits).
"""

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from django.db.models import Q
from django.utils import timezone

from ..models import (
    AuditResolutionLog,
    CardOperatorTransaction,
    CrossMatchResult,
    ErpTransaction,
    ReconciliationProcess,
    ReconciliationStatus,
)
from ..templatetags.currency_filters import fmt_clp
from .clp_reconciliation import MODALITY_LABELS, _installment_is_later, get_clp_tolerance
from .clp_suggestion_engine import _find_db_suggestion_for_clp_voucher
from .column_detector import normalize_auth_code
from .erp_coverage import expand_date_window
from .suggestion_actions import _as_text, load_suggestion, with_proposed_change
from .suggestion_engine import _find_db_suggestion_for_orphan
from .voucher_recross import NOT_LINKED_RESULTS, recalculate_process_summaries, recross_voucher

_CHUNK_SIZE = 400

_CARD_FILTERS = {
    "CLP": Q(card_type__isnull=False) | Q(payment_code__in=("RCO", "RC", "REDCOMPRA", "V$", "MC$", "AME$", "DIN$")),
    "USD": Q(card_type__isnull=False) | Q(payment_code__in=("VUS$", "MCUS$", "MUS$", "AUS$", "DUS$")),
}

_RESULT_LABELS = {
    "MATCHED": "Cuadrado",
    "AMOUNT_MISMATCH": "Diferencia de monto",
    "CARD_TYPE_MISMATCH": "Medio de pago distinto",
    "NOT_FOUND_IN_ERP": "No está en Borderó",
    "OUT_OF_PERIOD": "Fuera de período",
    "UNMATCHED_BANK_DEPOSIT": "Depósito sin cruce",
}


def _chunked(values: Sequence[int], size: int = _CHUNK_SIZE) -> Iterable[Sequence[int]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def _load_erp_rows_by_id(erp_ids: Iterable[int]) -> Dict[int, ErpTransaction]:
    unique_ids = sorted({int(eid) for eid in erp_ids if eid})
    rows: Dict[int, ErpTransaction] = {}
    for chunk in _chunked(unique_ids):
        for row in ErpTransaction.objects.filter(pk__in=chunk).select_related("process", "card_type"):
            rows[row.pk] = row
    return rows


def _claimed_map_for_erp_ids(erp_ids: Iterable[int]) -> Dict[int, Set[int]]:
    unique_ids = sorted({int(eid) for eid in erp_ids if eid})
    claimed: Dict[int, Set[int]] = {}
    for chunk in _chunked(unique_ids):
        rows = (
            CrossMatchResult.objects.filter(erp_transaction_id__in=chunk)
            .exclude(system_result__in=NOT_LINKED_RESULTS)
            .values_list("erp_transaction_id", "pk")
        )
        for erp_id, match_id in rows:
            claimed.setdefault(erp_id, set()).add(match_id)
    return claimed


def _has_open_valid_proposal(
    case: CrossMatchResult,
    suggestion: Optional[Dict[str, Any]],
    erp_by_id: Dict[int, ErpTransaction],
    claimed_map: Dict[int, Set[int]],
) -> bool:
    if not isinstance(suggestion, dict):
        return False
    prop = suggestion.get("proposed_change")
    if not isinstance(prop, dict):
        return False
    if prop.get("status"):
        return True
    erp_id = prop.get("erp_id")
    field_name = prop.get("field")
    row = erp_by_id.get(erp_id) if erp_id else None
    if row is None or not field_name or not hasattr(row, field_name):
        return False
    other_claimers = claimed_map.get(row.pk, set()) - {case.pk}
    if other_claimers:
        return False
    if case.system_result in NOT_LINKED_RESULTS and row.reconciled:
        return False
    if case.system_result not in NOT_LINKED_RESULTS and row.reconciled and row.pk != case.erp_transaction_id:
        return False
    return _as_text(getattr(row, field_name)) == prop.get("old")


def _enrich_case_hotel_fields(case: CrossMatchResult, row: Optional[ErpTransaction]) -> None:
    if row is None:
        return
    if not case.room_number and row.room_number:
        case.room_number = row.room_number
    if not case.guest_name and row.guest_name:
        case.guest_name = row.guest_name
    if not case.cashier_username and row.cashier_username:
        case.cashier_username = row.cashier_username


def _voucher_payload_for_actions(
    case: CrossMatchResult,
    linked_rows: Optional[Sequence[ErpTransaction]] = None,
) -> Dict[str, Any]:
    tbk = case.transbank_transaction
    card_code = case.card_type.code if case.card_type_id else (tbk.card_type.code if tbk.card_type_id else "")
    card_name = case.card_type.name if case.card_type_id else (tbk.card_type.name if tbk.card_type_id else card_code)
    modality = (tbk.payment_modality or "CREDIT") if tbk else "CREDIT"
    rows_list = [r for r in (linked_rows or ()) if r and r.pk]
    if not rows_list and case.erp_transaction_id:
        rows_list = [case.erp_transaction]
    return {
        "system_result": case.system_result,
        "authorization_code": (tbk.authorization_code if tbk else case.authorization_code) or "",
        "tbk_amount": float(tbk.original_sale_amount if tbk else case.transbank_amount),
        "gross_amount_usd": float(tbk.original_sale_amount if tbk else case.transbank_amount),
        "payment_modality": modality,
        "modality_label": MODALITY_LABELS.get(modality, modality),
        "card_code": card_code,
        "card_name": card_name,
        "sale_date": tbk.sale_date.strftime("%d/%m/%Y") if tbk and tbk.sale_date else "",
        "masked_card": (tbk.masked_card_number if tbk else "") or "—",
        "erp_index": None,
        "erp_indices": [r.pk for r in rows_list],
    }


def _process_tbk_file_name(case: CrossMatchResult, default: str = "Transbank") -> str:
    proc = case.process
    if proc is None:
        return default
    modality = (case.transbank_transaction.payment_modality if case.transbank_transaction else "") or ""
    modality_attr = {
        "CREDIT": proc.transbank_credit_file_name,
        "DEBIT": proc.transbank_debit_file_name,
        "PREPAID": proc.transbank_prepaid_file_name,
    }.get(modality)
    return modality_attr or proc.transbank_general_file_name or default


def _process_op_file_name(proc: Optional[ReconciliationProcess], card_code: str) -> str:
    if proc is None:
        return "Posición por Clientes"
    return (
        {
            "AX": proc.american_express_file_name,
            "DI": proc.diners_file_name,
            "VI": proc.visa_file_name,
            "MC": proc.mastercard_file_name,
        }.get(card_code)
        or "Posición por Clientes"
    )


def _chip_dict(label: str, icon: str, tone: str) -> Dict[str, str]:
    return {"label": label, "icon": icon, "tone": tone}


def _build_clp_discrepancy_suggestion(
    case: CrossMatchResult,
    row: ErpTransaction,
    linked_rows: Optional[Sequence[ErpTransaction]] = None,
) -> Optional[Dict[str, Any]]:
    voucher = _voucher_payload_for_actions(case, linked_rows=linked_rows)
    db_proc_id = row.process_id
    db_proc_period = row.process.accounting_period if row.process else f"Proceso #{db_proc_id}"
    db_auth = (row.authorization_code or "").strip() or "— (sin código)"
    db_acc = row.account_code or row.document_number or "—"
    db_inv = row.invoice_number or "—"
    db_cashier = row.cashier_username or "—"
    db_date = row.transaction_date.strftime("%d/%m/%Y") if row.transaction_date else "—"
    db_pay = row.payment_code or "—"
    tbk_file = _process_tbk_file_name(case)
    erp_file = (row.process.erp_file_name if row.process else "") or f"Base de Datos (Proceso #{db_proc_id})"
    is_rc = (row.payment_code in ("RCO", "RC", "REDCOMPRA")) or (row.card_type_id and row.card_type.code in ("RCO", "RC"))
    erp_row_view = {
        "excel_row": f"ID #{row.pk}",
        "source_file": erp_file,
        "authorization_code": db_auth,
        "payment_code": db_pay,
        "card_code": row.card_type.code if row.card_type_id else "",
        "card_name": row.card_type.name if row.card_type_id else ("Redcompra" if is_rc else "Tarjeta"),
        "payment_group": "REDCOMPRA" if is_rc else "CREDIT",
        "amount": float(row.amount),
        "transaction_date": db_date,
        "transaction_time": row.transaction_time.strftime("%H:%M:%S") if row.transaction_time else "",
        "cashier_username": db_cashier,
        "account_code": db_acc,
        "invoice_number": db_inv,
    }

    if case.system_result == "AMOUNT_MISMATCH":
        gap = float(case.difference_clp)
        sug = {
            "confidence": "ALTA",
            "score": 90,
            "erp_index": None,
            "headline": f"Cruce en BD Proceso #{db_proc_id} (Cta. {db_acc} · Fact. {db_inv}) con dif. de {fmt_clp(abs(gap))}",
            "cause": f"Diferencia de {fmt_clp(gap)} entre POS Transbank y monto digitado en Borderó",
            "missing_code": False,
            "action": f"Revisar propina/cargo de {fmt_clp(abs(gap))} en Cta. {db_acc} con cajero {db_cashier}",
            "candidate_summary": f"Cta. {db_acc} · Doc. {db_auth} · Fact. {db_inv} · {fmt_clp(row.amount)} ({db_cashier})",
            "manual_check_note": (
                f"El voucher '{voucher['authorization_code']}' se cobró en Transbank por {fmt_clp(voucher['tbk_amount'])}, "
                f"pero en la BD (Proceso #{db_proc_id}, {db_proc_period}, Cta. {db_acc}, Factura {db_inv}) el cajero '{db_cashier}' "
                f"registró {fmt_clp(row.amount)} con Documento '{db_auth}'."
            ),
            "reasons": [
                _chip_dict(f"Mismo Código ({voucher['authorization_code']} ⇄ {db_auth})", "bi-upc-scan", "emerald"),
                _chip_dict(f"Fecha Venta Borderó: {db_date}", "bi-calendar-check", "cyan"),
                _chip_dict(f"Mismo Medio ({db_pay})", "bi-credit-card-2-front", "purple"),
                _chip_dict(f"Diferencia POS vs Borderó: {fmt_clp(gap)}", "bi-exclamation-triangle", "gold"),
            ],
            "files_involved": {"tbk": tbk_file, "erp": erp_file},
            "erp_row": erp_row_view,
            "db_match": {"transaction_id": row.pk, "process_id": db_proc_id, "accounting_period": db_proc_period},
        }
    elif case.system_result == "CARD_TYPE_MISMATCH":
        sug = {
            "confidence": "ALTA",
            "score": 90,
            "erp_index": None,
            "headline": f"Monto y código exactos en BD Proceso #{db_proc_id}, digitado como {db_pay}",
            "cause": f"Medio de pago registrado como {db_pay} en lugar de {voucher['modality_label']}",
            "missing_code": False,
            "action": f"Cambiar medio de pago de {db_pay} a {voucher['modality_label']} en el Borderó",
            "candidate_summary": f"Cta. {db_acc} · Doc. {db_auth} · Fact. {db_inv} · {fmt_clp(row.amount)} ({db_cashier})",
            "manual_check_note": (
                f"El voucher '{voucher['authorization_code']}' ({fmt_clp(voucher['tbk_amount'])}) coincide en Documento y monto "
                f"con el registro del Proceso #{db_proc_id} (Cta. {db_acc}, Doc. {db_auth}), pero fue ingresado como '{db_pay}'."
            ),
            "reasons": [
                _chip_dict(f"Monto Exacto ({fmt_clp(row.amount)})", "bi-cash-stack", "emerald"),
                _chip_dict(f"Mismo Código ({db_auth})", "bi-upc-scan", "emerald"),
                _chip_dict(f"Medio distinto ({db_pay} ≠ {voucher['modality_label']})", "bi-credit-card-2-front", "gold"),
            ],
            "files_involved": {"tbk": tbk_file, "erp": erp_file},
            "erp_row": erp_row_view,
            "db_match": {"transaction_id": row.pk, "process_id": db_proc_id, "accounting_period": db_proc_period},
        }
    else:
        return None

    return with_proposed_change("CLP", voucher, sug, [])


def _clp_status_detail(case: CrossMatchResult, sug: Optional[Dict[str, Any]]) -> str:
    modality = (case.transbank_transaction.payment_modality if case.transbank_transaction else "") or "CREDIT"
    mod_label = MODALITY_LABELS.get(modality, modality)
    if case.system_result == "AMOUNT_MISMATCH":
        err = "Diferencia de monto"
    elif case.system_result == "CARD_TYPE_MISMATCH":
        err = "Medio de pago errado"
    else:
        err = _RESULT_LABELS.get(case.system_result, case.system_result)
    action = (sug or {}).get("action") or "Revisar diferencia"
    return f"{mod_label} · {err} · {action}"[:255]


def _refresh_unlinked_suggestions(
    unlinked_cases: List[CrossMatchResult],
) -> Tuple[int, int]:
    """
    Recomputes assisted DB suggestions for unlinked PENDING cases (`NOT_FOUND_IN_ERP` / `OUT_OF_PERIOD`)
    that do not currently hold a still-valid open `proposed_change`.
    Returns `(suggestions_updated, suggestions_cleared)`.
    """
    if not unlinked_cases:
        return 0, 0

    parsed_by_pk: Dict[int, Optional[Dict[str, Any]]] = {}
    referenced_erp_ids: List[int] = []
    for case in unlinked_cases:
        sug = load_suggestion(case)
        parsed_by_pk[case.pk] = sug
        prop = (sug or {}).get("proposed_change")
        if isinstance(prop, dict) and prop.get("erp_id"):
            referenced_erp_ids.append(prop["erp_id"])

    ref_rows = _load_erp_rows_by_id(referenced_erp_ids)
    ref_claimed = _claimed_map_for_erp_ids(referenced_erp_ids)

    claimed_db_ids: Dict[str, Set[int]] = {"CLP": set(), "USD": set()}
    clp_to_refresh: List[CrossMatchResult] = []
    usd_to_refresh: List[CrossMatchResult] = []

    for case in unlinked_cases:
        currency = case.process.process_currency
        sug = parsed_by_pk[case.pk]
        if _has_open_valid_proposal(case, sug, ref_rows, ref_claimed):
            prop_erp_id = (sug or {}).get("proposed_change", {}).get("erp_id")
            if prop_erp_id and currency in claimed_db_ids:
                claimed_db_ids[currency].add(prop_erp_id)
            continue
        # Leave plain human auditor text untouched.
        if case.auditor_comment and case.auditor_comment.strip() and sug is None:
            continue
        if currency == "CLP":
            clp_to_refresh.append(case)
        elif currency == "USD":
            usd_to_refresh.append(case)

    updated = 0
    cleared = 0

    if clp_to_refresh:
        clp_tolerance = float(get_clp_tolerance())
        clp_dates = expand_date_window(
            [c.transbank_transaction.sale_date for c in clp_to_refresh if c.transbank_transaction.sale_date],
            back_days=2,
            forward_days=5,
        )
        clp_qs = (
            ErpTransaction.objects.filter(reconciled=False, source_currency="CLP")
            .filter(_CARD_FILTERS["CLP"])
            .select_related("process", "card_type")
        )
        if clp_dates:
            clp_candidates = list(clp_qs.filter(Q(transaction_date__in=clp_dates) | Q(transaction_date__isnull=True)))
        else:
            clp_candidates = list(clp_qs.order_by("-transaction_date")[:500])

        cand_claimed = _claimed_map_for_erp_ids([r.pk for r in clp_candidates])
        clp_candidates = [r for r in clp_candidates if not cand_claimed.get(r.pk)]

        for case in clp_to_refresh:
            voucher = _voucher_payload_for_actions(case)
            tbk_file = _process_tbk_file_name(case)
            default_erp_file = case.process.erp_file_name or "Borderó"
            new_sug = _find_db_suggestion_for_clp_voucher(
                voucher,
                clp_tolerance,
                tbk_file,
                default_erp_file,
                db_candidates=clp_candidates,
                claimed_db_ids=claimed_db_ids["CLP"],
            )
            if new_sug:
                new_sug = with_proposed_change("CLP", voucher, new_sug, [])
                new_comment = json.dumps(new_sug, ensure_ascii=False)
                if new_comment != case.auditor_comment:
                    case.auditor_comment = new_comment
                    case.save(update_fields=["auditor_comment"])
                    updated += 1
            elif isinstance((parsed_by_pk[case.pk] or {}).get("proposed_change"), dict):
                case.auditor_comment = None
                case.save(update_fields=["auditor_comment"])
                cleared += 1

    if usd_to_refresh:
        usd_dates = expand_date_window(
            [c.transbank_transaction.sale_date for c in usd_to_refresh if c.transbank_transaction.sale_date],
            back_days=2,
            forward_days=5,
        )
        usd_qs = (
            ErpTransaction.objects.filter(reconciled=False, source_currency="USD")
            .filter(_CARD_FILTERS["USD"])
            .select_related("process", "card_type")
        )
        if usd_dates:
            usd_candidates = list(usd_qs.filter(Q(transaction_date__in=usd_dates) | Q(transaction_date__isnull=True)))
        else:
            usd_candidates = list(usd_qs.order_by("-transaction_date")[:300])

        cand_claimed = _claimed_map_for_erp_ids([r.pk for r in usd_candidates])
        usd_candidates = [r for r in usd_candidates if not cand_claimed.get(r.pk)]

        usd_proc_ids = sorted({c.process_id for c in usd_to_refresh if c.process_id})
        usd_card_codes = sorted({
            (c.card_type.code if c.card_type_id else c.transbank_transaction.card_type.code)
            for c in usd_to_refresh
            if (c.card_type_id or (c.transbank_transaction_id and c.transbank_transaction.card_type_id))
        })
        op_rows = list(
            CardOperatorTransaction.objects.filter(
                Q(process_id__in=usd_proc_ids) | Q(card_type__code__in=usd_card_codes)
            )
            .select_related("card_type")
            .order_by("-id")[:1000]
        )
        all_ops = [
            {
                "card_code": op.card_type.code,
                "document_number": op.document_number,
                "authorization_code": op.authorization_code or "",
                "foreign_currency_amount": float(op.foreign_currency_amount),
                "balance_amount": float(op.balance_amount),
                "corrected_balance_amount": float(op.corrected_balance_amount),
                "_process_id": op.process_id,
            }
            for op in op_rows
        ]

        for case in usd_to_refresh:
            orph = _voucher_payload_for_actions(case)
            proc_erp = case.process.erp_file_name or "Borderó"
            proc_tbk = _process_tbk_file_name(case, "ABONO TBK")
            proc_ops = [o for o in all_ops if o["_process_id"] == case.process_id] + [
                o for o in all_ops if o["_process_id"] != case.process_id
            ]

            def _files_for_card(card_code: str, _proc=case.process, _tbk=proc_tbk, _erp=proc_erp) -> Dict[str, str]:
                return {"tbk": _tbk, "erp": _erp, "op": _process_op_file_name(_proc, card_code)}

            new_sug = _find_db_suggestion_for_orphan(
                orph,
                proc_ops,
                set(),
                _files_for_card,
                db_candidates=usd_candidates,
                claimed_db_ids=claimed_db_ids["USD"],
            )
            if new_sug:
                new_sug = with_proposed_change("USD", orph, new_sug, [])
                new_comment = json.dumps(new_sug, ensure_ascii=False)
                if new_comment != case.auditor_comment:
                    case.auditor_comment = new_comment
                    case.save(update_fields=["auditor_comment"])
                    updated += 1
            elif isinstance((parsed_by_pk[case.pk] or {}).get("proposed_change"), dict):
                case.auditor_comment = None
                case.save(update_fields=["auditor_comment"])
                cleared += 1

    return updated, cleared


def recross_pending_cases(
    user,
    *,
    currencies: Optional[Tuple[str, ...]] = None,
    exclude_process_id: Optional[int] = None,
    exclude_match_ids: Optional[Set[int]] = None,
    resolved_status_code: str = "ACCEPTED_ROUNDING",
    audit_prefix: str = "Actualización automática con nuevos datos del Borderó",
    clear_suggestion_on_match: bool = True,
) -> Dict[str, int]:
    """
    Re-evaluates all open PENDING `CrossMatchResult` cases against the current ERP & Operator DB:
      1. Cases whose code now matches (or already-linked discrepancies resolved by new data)
         are updated, linked, transitioned to `resolved_status_code`, and logged.
      2. Unlinked cases (`NOT_FOUND_IN_ERP` / `OUT_OF_PERIOD`) that remain unlinked keep
         `erp_transaction=None` (faithful cross-match) and refresh their `proposed_change`
         assisted suggestion when they have no valid open proposal yet.
    """
    qs = (
        CrossMatchResult.objects.filter(
            management_status__code="PENDING",
            transbank_transaction__isnull=False,
        )
        .exclude(system_result="MATCHED")
        .select_related(
            "process",
            "management_status",
            "transbank_transaction",
            "transbank_transaction__card_type",
            "card_type",
            "erp_transaction",
            "card_operator_transaction",
        )
        .order_by("process_id", "id")
    )
    if currencies:
        qs = qs.filter(process__process_currency__in=list(currencies))
    if exclude_process_id is not None:
        qs = qs.exclude(process_id=exclude_process_id)

    exclude_ids = set(exclude_match_ids or ())
    pending_cases = [c for c in qs if c.pk not in exclude_ids]
    if not pending_cases:
        return {"matched": 0, "updated": 0, "suggestions_updated": 0, "suggestions_cleared": 0}

    needed_currencies = {c.process.process_currency for c in pending_cases if c.process_id}
    available_codes: Dict[str, Set[str]] = {}
    later_clp_codes: Set[str] = set()
    for curr in needed_currencies:
        card_filter = _CARD_FILTERS.get(curr)
        if card_filter is None:
            available_codes[curr] = set()
            continue
        raw_rows = (
            ErpTransaction.objects.filter(source_currency=curr)
            .filter(card_filter)
            .values_list("authorization_code", "reconciled")
        )
        unrec_codes: Set[str] = set()
        for code, is_rec in raw_rows:
            norm = normalize_auth_code(code) if code else ""
            if not norm:
                continue
            if not is_rec:
                unrec_codes.add(norm)
            if curr == "CLP":
                later_clp_codes.add(norm)
        available_codes[curr] = unrec_codes

    resolved_status: Optional[ReconciliationStatus] = None
    dirty_processes: Dict[int, ReconciliationProcess] = {}
    matched_count = 0
    updated_count = 0
    remaining_unlinked: List[CrossMatchResult] = []
    now = timezone.now()

    for case in pending_cases:
        currency = case.process.process_currency
        norm_code = normalize_auth_code(case.transbank_transaction.authorization_code)
        is_later_clp = (
            currency == "CLP"
            and _installment_is_later(case.transbank_transaction.installment_number or "")
        )
        code_pool = later_clp_codes if is_later_clp else available_codes.get(currency, set())
        has_candidate = bool(case.erp_transaction_id or (norm_code and norm_code in code_pool))
        if not has_candidate:
            if case.system_result in NOT_LINKED_RESULTS:
                remaining_unlinked.append(case)
            continue

        prev_result = case.system_result
        prev_obs = case.system_observation
        prev_status = case.management_status
        prev_erp_id = case.erp_transaction_id
        prev_op_id = case.card_operator_transaction_id
        prev_diff_clp = case.difference_clp
        prev_diff_usd = case.difference_usd

        outcome = recross_voucher(case, restrict_to_date_window=True)
        new_result = outcome["system_result"]

        if new_result in NOT_LINKED_RESULTS and prev_result in NOT_LINKED_RESULTS:
            # Nothing actually linked; restore original unlinked diagnosis and refresh suggestions in Phase 2.
            case.system_result = prev_result
            case.system_observation = prev_obs
            remaining_unlinked.append(case)
            continue

        if new_result == "MATCHED":
            if resolved_status is None:
                resolved_status = ReconciliationStatus.objects.get(code=resolved_status_code)
            _enrich_case_hotel_fields(case, outcome["fields"].get("erp_transaction"))
            case.management_status = resolved_status
            case.resolved_by = user
            case.resolution_date = now
            case.system_status_detail = ""
            if clear_suggestion_on_match and case.auditor_comment:
                existing_sug = load_suggestion(case)
                prop = (existing_sug or {}).get("proposed_change") if isinstance(existing_sug, dict) else None
                if not prop or prop.get("status") != "accepted":
                    case.auditor_comment = None
            case.save()

            prev_label = _RESULT_LABELS.get(prev_result, prev_result)
            AuditResolutionLog.objects.create(
                cross_match_result=case,
                user=user,
                previous_status=prev_status,
                new_status=resolved_status,
                comment=f"{audit_prefix}. Cruce: {prev_label} → Cuadrado.",
            )
            if case.process_id:
                dirty_processes[case.process_id] = case.process
            matched_count += 1
            continue

        if new_result in NOT_LINKED_RESULTS:
            # Previously linked case lost its ERP link cleanly (rare fallback).
            existing_sug = load_suggestion(case)
            if isinstance(existing_sug, dict) and isinstance(existing_sug.get("proposed_change"), dict):
                case.auditor_comment = None
            case.system_status_detail = ""
            case.save()
            AuditResolutionLog.objects.create(
                cross_match_result=case,
                user=user,
                previous_status=prev_status,
                new_status=prev_status,
                comment=(
                    f"{audit_prefix}. Cruce: {_RESULT_LABELS.get(prev_result, prev_result)} → "
                    f"{_RESULT_LABELS.get(new_result, new_result)}."
                ),
            )
            if case.process_id:
                dirty_processes[case.process_id] = case.process
            remaining_unlinked.append(case)
            updated_count += 1
            continue

        # Linked mismatch (`AMOUNT_MISMATCH` / `CARD_TYPE_MISMATCH`), keep PENDING.
        changed = (
            new_result != prev_result
            or case.erp_transaction_id != prev_erp_id
            or case.card_operator_transaction_id != prev_op_id
            or case.difference_clp != prev_diff_clp
            or case.difference_usd != prev_diff_usd
        )
        if not changed:
            continue

        linked_row = outcome["fields"].get("erp_transaction")
        _enrich_case_hotel_fields(case, linked_row)
        if currency == "CLP" and linked_row is not None:
            new_sug = _build_clp_discrepancy_suggestion(
                case, linked_row, linked_rows=outcome.get("linked_rows")
            )
            if new_sug:
                case.auditor_comment = json.dumps(new_sug, ensure_ascii=False)
            case.system_status_detail = _clp_status_detail(case, new_sug)
        else:
            existing_sug = load_suggestion(case)
            if isinstance(existing_sug, dict) and isinstance(existing_sug.get("proposed_change"), dict):
                case.auditor_comment = None
            if prev_result == "NOT_FOUND_IN_ERP" or not case.system_status_detail:
                case.system_status_detail = f"Diferencia de USD {case.difference_usd}"[:255]

        case.save()
        AuditResolutionLog.objects.create(
            cross_match_result=case,
            user=user,
            previous_status=prev_status,
            new_status=prev_status,
            comment=(
                f"{audit_prefix}. Cruce: {_RESULT_LABELS.get(prev_result, prev_result)} → "
                f"{_RESULT_LABELS.get(new_result, new_result)} ({outcome['remaining']})."
            ),
        )
        if case.process_id:
            dirty_processes[case.process_id] = case.process
        updated_count += 1

    sug_updated, sug_cleared = _refresh_unlinked_suggestions(remaining_unlinked)

    for proc in dirty_processes.values():
        recalculate_process_summaries(proc)

    return {
        "matched": matched_count,
        "updated": updated_count,
        "suggestions_updated": sug_updated,
        "suggestions_cleared": sug_cleared,
    }
