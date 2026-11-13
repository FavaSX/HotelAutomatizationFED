"""
ERP Bordero DB Coverage Tracking, Ideal Export Range Calculation, Upload Warnings,
and Historical Card Candidate Lookup for USD & CLP Cross-Matching.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
import math
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
import pandas as pd

from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from ..models import CrossMatchResult, ErpTransaction, ReconciliationProcess
from .column_detector import (
    ERP_COLUMN_RULES,
    normalize_auth_code,
    resolve_dataframe_columns,
    safe_date,
    safe_decimal,
)
from .excel_parsers import classify_bordero_clp_payment, parse_bordero_period

_FAST_NOT_NA = (str, int, Decimal, bool)
_NOT_LINKED_RESULTS = ("NOT_FOUND_IN_ERP", "OUT_OF_PERIOD")


def _cell(row_tuple: tuple, col_idx: int) -> Any:
    if col_idx < 0 or col_idx >= len(row_tuple):
        return None
    val = row_tuple[col_idx]
    if val is None or val is pd.NaT or val is pd.NA:
        return None
    if isinstance(val, _FAST_NOT_NA):
        return val
    if isinstance(val, float):
        return None if math.isnan(val) else val
    return None if pd.isna(val) else val


def _fmt_dmy(value: Optional[date]) -> str:
    return value.strftime("%d/%m/%Y") if value else ""


def get_erp_coverage_summary(today: Optional[date] = None) -> Dict[str, Any]:
    """
    Inspects all active ErpTransaction rows and prior ReconciliationProcess metadata to build:
      - Actual DB date span (min_date .. max_date, total_rows, date gaps)
      - Whether the last uploaded closing left an in-progress day incomplete
      - The ideal Bordero export window [recommended_from .. recommended_to] up to yesterday
    """
    today = today or timezone.localdate()
    yesterday = today - timedelta(days=1)

    agg = ErpTransaction.objects.aggregate(
        min_date=Min("transaction_date"),
        max_date=Max("transaction_date"),
        total_rows=Count("id"),
    )
    min_date: Optional[date] = agg.get("min_date")
    max_date: Optional[date] = agg.get("max_date")
    total_rows: int = int(agg.get("total_rows") or 0)

    if total_rows == 0 or min_date is None or max_date is None:
        rec_from_year = date(today.year, 1, 1)
        rec_from_prev = date(today.year - 1, 1, 1)
        return {
            "has_data": False,
            "total_rows": 0,
            "min_date": None,
            "max_date": None,
            "min_date_str": "",
            "max_date_str": "",
            "min_date_iso": "",
            "max_date_iso": "",
            "today": today,
            "today_str": _fmt_dmy(today),
            "yesterday": yesterday,
            "yesterday_str": _fmt_dmy(yesterday),
            "has_incomplete_day": False,
            "incomplete_date": None,
            "incomplete_date_str": "",
            "incomplete_date_iso": "",
            "incomplete_process_id": None,
            "recommended_from": rec_from_year,
            "recommended_from_str": _fmt_dmy(rec_from_year),
            "recommended_from_extended_str": _fmt_dmy(rec_from_prev),
            "recommended_to": yesterday,
            "recommended_to_str": _fmt_dmy(yesterday),
            "ideal_range_label": f"01/01/{today.year} al {_fmt_dmy(yesterday)} (o desde 01/01/{today.year - 1} / {today.year - 2})",
            "gap_count": 0,
            "largest_gap": None,
        }

    # Check if an earlier closing left its final Bordero day incomplete
    incomplete_proc = (
        ReconciliationProcess.objects.filter(
            erp_last_day_incomplete=True,
            erp_max_date__isnull=False,
        )
        .order_by("-erp_max_date", "-execution_date")
        .first()
    )
    incomplete_date: Optional[date] = None
    incomplete_process_id: Optional[int] = None
    if incomplete_proc is not None:
        incomplete_date = incomplete_proc.erp_max_date
        incomplete_process_id = incomplete_proc.id
    elif max_date >= today:
        # Today's date is present in DB (records can still be posted after export time)
        incomplete_date = max_date

    # Recommended export start: incomplete day (if earlier or equal to max_date), otherwise DB max_date inclusive
    rec_from = max_date
    if incomplete_date is not None and incomplete_date < rec_from:
        rec_from = incomplete_date
    rec_to = yesterday if yesterday >= rec_from else max(today, rec_from)

    # Detect internal gaps (> 3 consecutive calendar days without any ERP row between min_date and max_date)
    distinct_dates = sorted(
        d
        for d in ErpTransaction.objects.exclude(transaction_date__isnull=True)
        .values_list("transaction_date", flat=True)
        .distinct()
    )
    gaps: List[Dict[str, Any]] = []
    for d1, d2 in zip(distinct_dates, distinct_dates[1:]):
        missing_days = (d2 - d1).days - 1
        if missing_days > 3:
            gaps.append({
                "from_date": d1 + timedelta(days=1),
                "to_date": d2 - timedelta(days=1),
                "from_str": _fmt_dmy(d1 + timedelta(days=1)),
                "to_str": _fmt_dmy(d2 - timedelta(days=1)),
                "days": missing_days,
            })
    largest_gap = max(gaps, key=lambda g: g["days"]) if gaps else None

    return {
        "has_data": True,
        "total_rows": total_rows,
        "min_date": min_date,
        "max_date": max_date,
        "min_date_str": _fmt_dmy(min_date),
        "max_date_str": _fmt_dmy(max_date),
        "min_date_iso": min_date.isoformat(),
        "max_date_iso": max_date.isoformat(),
        "today": today,
        "today_str": _fmt_dmy(today),
        "yesterday": yesterday,
        "yesterday_str": _fmt_dmy(yesterday),
        "has_incomplete_day": incomplete_date is not None,
        "incomplete_date": incomplete_date,
        "incomplete_date_str": _fmt_dmy(incomplete_date),
        "incomplete_date_iso": incomplete_date.isoformat() if incomplete_date else "",
        "incomplete_process_id": incomplete_process_id,
        "recommended_from": rec_from,
        "recommended_from_str": _fmt_dmy(rec_from),
        "recommended_from_extended_str": "",
        "recommended_to": rec_to,
        "recommended_to_str": _fmt_dmy(rec_to),
        "ideal_range_label": f"{_fmt_dmy(rec_from)} al {_fmt_dmy(rec_to)}",
        "gap_count": len(gaps),
        "largest_gap": largest_gap,
    }


def extract_bordero_df_stats(dataframe: pd.DataFrame) -> Dict[str, Any]:
    """
    Lightweight tuple scan of a Bordero DataFrame to extract min/max dates, header period,
    and card vs non-card movement counts without constructing Django model instances.
    """
    stats = {
        "period_header": (None, None),
        "min_date": None,
        "max_date": None,
        "total_movements": 0,
        "card_movements": 0,
        "non_card_movements": 0,
        "usd_card_movements": 0,
        "clp_card_movements": 0,
    }
    if dataframe is None or len(dataframe) == 0:
        return stats

    cols = resolve_dataframe_columns(dataframe, ERP_COLUMN_RULES)
    c_pcode = cols["payment_code"]
    c_pdesc = cols["payment_description"]
    c_amt = cols["amount"]
    c_date = cols["transaction_date"]

    min_date: Optional[date] = None
    max_date: Optional[date] = None
    total = card = non_card = usd_card = clp_card = 0

    for row in dataframe.itertuples(index=False, name=None):
        payment_code = str(_cell(row, c_pcode) or "").strip().upper()
        if not payment_code or ("COD" in payment_code and "DEB" in payment_code):
            continue
        tx_date = safe_date(_cell(row, c_date))
        amt_cell = _cell(row, c_amt)
        if tx_date is None and amt_cell is None:
            shifted_date = safe_date(_cell(row, c_date + 1))
            if shifted_date is not None:
                tx_date = shifted_date
                amt_cell = _cell(row, c_amt + 1)
        if tx_date is None:
            continue

        total += 1
        if min_date is None or tx_date < min_date:
            min_date = tx_date
        if max_date is None or tx_date > max_date:
            max_date = tx_date

        description = " ".join(str(_cell(row, c_pdesc) or "").split())
        upper_desc = description.upper()
        raw_amount = safe_decimal(amt_cell)

        is_usd_card = (
            raw_amount != 0
            and "US$" in upper_desc
            and (
                "MASTER" in upper_desc or payment_code in ("MCUS$", "MUS$")
                or "VISA" in upper_desc or payment_code == "VUS$"
                or "AMEX" in upper_desc or "AMERICAN" in upper_desc or payment_code == "AUS$"
                or "DINER" in upper_desc or payment_code == "DUS$"
            )
        )
        is_clp_card, _, _ = classify_bordero_clp_payment(description)
        is_clp_card = bool(is_clp_card and raw_amount != 0)

        if is_usd_card:
            card += 1
            usd_card += 1
        elif is_clp_card:
            card += 1
            clp_card += 1
        else:
            non_card += 1

    stats["period_header"] = parse_bordero_period(dataframe)
    stats["min_date"] = min_date
    stats["max_date"] = max_date
    stats["total_movements"] = total
    stats["card_movements"] = card
    stats["non_card_movements"] = non_card
    stats["usd_card_movements"] = usd_card
    stats["clp_card_movements"] = clp_card
    return stats


def merge_bordero_stats(stats_list: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Combines stats dictionaries from one or more uploaded Bordero files into a unified span."""
    file_mins: List[date] = []
    file_maxs: List[date] = []
    header_mins: List[date] = []
    header_maxs: List[date] = []
    total = card = non_card = usd_card = clp_card = 0

    for st in stats_list:
        if not st:
            continue
        if st.get("min_date"):
            file_mins.append(st["min_date"])
        if st.get("max_date"):
            file_maxs.append(st["max_date"])
        p_min, p_max = st.get("period_header") or (None, None)
        if p_min:
            header_mins.append(p_min)
        if p_max:
            header_maxs.append(p_max)
        total += int(st.get("total_movements") or 0)
        card += int(st.get("card_movements") or 0)
        non_card += int(st.get("non_card_movements") or 0)
        usd_card += int(st.get("usd_card_movements") or 0)
        clp_card += int(st.get("clp_card_movements") or 0)

    file_min = min(file_mins) if file_mins else None
    file_max = max(file_maxs) if file_maxs else None
    header_min = min(header_mins) if header_mins else None
    header_max = max(header_maxs) if header_maxs else None
    effective_min = min(d for d in (file_min, header_min) if d) if (file_min or header_min) else None
    effective_max = max(d for d in (file_max, header_max) if d) if (file_max or header_max) else None

    return {
        "file_min": file_min,
        "file_max": file_max,
        "header_min": header_min,
        "header_max": header_max,
        "effective_min": effective_min,
        "effective_max": effective_max,
        "total_movements": total,
        "card_movements": card,
        "non_card_movements": non_card,
        "usd_card_movements": usd_card,
        "clp_card_movements": clp_card,
    }


def evaluate_bordero_upload_warnings(
    stats_list: List[Dict[str, Any]],
    coverage: Optional[Dict[str, Any]] = None,
    today: Optional[date] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Validates uploaded Bordero coverage against current DB history and emits actionable warnings:
      - SKIPPED_INCOMPLETE_DAY (danger): DB had an incomplete day D, and upload starts after D.
      - GAP_AFTER_DB_MAX (warning): upload starts strictly after DB max_date (leaves uncovered days).
      - INCLUDES_TODAY_INCOMPLETE (warning): upload includes today's date (still generating movements).
      - CARD_ONLY_FILTERED (warning): upload contains >= 20 rows and zero non-card movements.
    Returns `(coverage_meta_dict, warnings_list)` ready for JSON storage in preview payload & upload UI.
    """
    today = today or timezone.localdate()
    coverage = coverage or get_erp_coverage_summary(today=today)
    merged = merge_bordero_stats(stats_list)

    eff_min: Optional[date] = merged["effective_min"]
    eff_max: Optional[date] = merged["effective_max"]
    file_min: Optional[date] = merged["file_min"]
    file_max: Optional[date] = merged["file_max"]
    includes_today = bool(eff_max is not None and eff_max >= today)

    warnings: List[Dict[str, Any]] = []

    skipped_incomplete = False
    incomplete_date: Optional[date] = coverage.get("incomplete_date")
    if coverage.get("has_incomplete_day") and incomplete_date is not None:
        check_min = eff_min or file_min
        if check_min is not None and check_min > incomplete_date:
            skipped_incomplete = True
            warnings.append({
                "level": "danger",
                "code": "SKIPPED_INCOMPLETE_DAY",
                "title": f"El día incompleto {_fmt_dmy(incomplete_date)} no quedó incluido en este Borderó",
                "message": (
                    f"La carga anterior incluyó movimientos del día en curso {_fmt_dmy(incomplete_date)}, "
                    f"por lo que ese cierre pudo quedar incompleto tras la hora de exportación. "
                    f"Este Borderó comienza el {_fmt_dmy(check_min)}: si continúas sin incluir desde el "
                    f"{_fmt_dmy(incomplete_date)}, podrían quedar registros sueltos sin importar."
                ),
            })

    db_max: Optional[date] = coverage.get("max_date")
    if not skipped_incomplete and coverage.get("has_data") and db_max is not None:
        check_min = eff_min or file_min
        if check_min is not None and check_min > db_max:
            warnings.append({
                "level": "warning",
                "code": "GAP_AFTER_DB_MAX",
                "title": f"Posible salto de fechas respecto a la base de datos (última fecha: {_fmt_dmy(db_max)})",
                "message": (
                    f"La base de datos llega hasta el {_fmt_dmy(db_max)} y el Borderó subido comienza "
                    f"el {_fmt_dmy(check_min)}. Para no dejar días sin respaldar, se recomienda exportar "
                    f"desde el {_fmt_dmy(db_max)} hasta el {_fmt_dmy(coverage.get('recommended_to'))}."
                ),
            })

    if includes_today and eff_max is not None:
        warnings.append({
            "level": "warning",
            "code": "INCLUDES_TODAY_INCOMPLETE",
            "title": f"Borderó incluye movimientos del día de hoy ({_fmt_dmy(eff_max)})",
            "message": (
                f"Como la jornada de hoy ({_fmt_dmy(eff_max)}) sigue activa, podrían registrarse nuevos cargos o pagos "
                f"después de esta exportación. Al confirmar el cierre se marcará como incompleto y en la próxima sesión "
                f"te pediremos iniciar nuevamente desde el {_fmt_dmy(eff_max)}."
            ),
        })

    if merged["total_movements"] >= 20 and merged["card_movements"] > 0 and merged["non_card_movements"] == 0:
        warnings.append({
            "level": "warning",
            "code": "CARD_ONLY_FILTERED",
            "title": "El Borderó parece estar filtrado solo por tarjetas",
            "message": (
                f"Se detectaron {merged['total_movements']} movimientos y todos corresponden a pagos con tarjeta "
                f"(0 movimientos de cargos/abonos como HOA, IVA, BAR o PROP). Recuerda exportar el Borderó completo "
                f"sin filtros de forma de pago para que ambas conciliaciones (USD y CLP) puedan aprovechar la base."
            ),
        })

    coverage_meta = {
        "erp_min_date": eff_min.isoformat() if eff_min else None,
        "erp_max_date": eff_max.isoformat() if eff_max else None,
        "erp_min_date_str": _fmt_dmy(eff_min),
        "erp_max_date_str": _fmt_dmy(eff_max),
        "file_min_date_str": _fmt_dmy(file_min),
        "file_max_date_str": _fmt_dmy(file_max),
        "erp_last_day_incomplete": includes_today,
        "total_movements": merged["total_movements"],
        "card_movements": merged["card_movements"],
        "non_card_movements": merged["non_card_movements"],
        "usd_card_movements": merged["usd_card_movements"],
        "clp_card_movements": merged["clp_card_movements"],
    }
    return coverage_meta, warnings


def resolve_prior_incomplete_processes(
    uploaded_min: Optional[date],
    uploaded_max: Optional[date],
    today: Optional[date] = None,
) -> int:
    """
    When a new closing is saved on a later day covering a previously flagged incomplete day D
    (i.e. D < today and uploaded_min <= D <= uploaded_max), clears `erp_last_day_incomplete` on
    prior processes whose incomplete day was D.
    """
    if uploaded_min is None or uploaded_max is None:
        return 0
    today = today or timezone.localdate()
    cutoff_max = min(uploaded_max, today - timedelta(days=1))
    if cutoff_max < uploaded_min:
        return 0
    return ReconciliationProcess.objects.filter(
        erp_last_day_incomplete=True,
        erp_max_date__gte=uploaded_min,
        erp_max_date__lte=cutoff_max,
    ).update(erp_last_day_incomplete=False)


def expand_date_window(sale_dates: Iterable[Any], back_days: int = 2, forward_days: int = 5) -> Set[date]:
    """Expands voucher sale dates into a discrete set of nearby ERP posting dates."""
    target: Set[date] = set()
    for raw in sale_dates:
        parsed = raw if isinstance(raw, date) and not isinstance(raw, datetime) else safe_date(raw)
        if parsed is None:
            continue
        for offset in range(-back_days, forward_days + 1):
            target.add(parsed + timedelta(days=offset))
    return target


def db_card_candidates_for_codes(
    source_currency: str,
    voucher_codes: Set[str],
    exclude_source_keys: Optional[Set[str]] = None,
    exclude_ids: Optional[Set[int]] = None,
    later_installment_codes: Optional[Set[str]] = None,
    voucher_dates_by_code: Optional[Dict[str, Set[date]]] = None,
) -> List[ErpTransaction]:
    """
    Fetches historical active card/RedCompra ErpTransaction rows from DB matching uploaded
    Transbank voucher authorization codes within a coherent sale-date window (excluding rows
    already loaded from the Excel).

    Priority & safety rules:
      - Only DB rows within [-7, +14] days of a matching voucher's `sale_date` (when both dates
        are known) are eligible, preventing false cross-month collisions on short 4-digit codes.
      - Unreconciled (`reconciled=False`) DB rows are always eligible (sorted first).
      - Already-reconciled (`reconciled=True`) DB rows are ONLY returned when:
          1. the normalized auth code belongs to a later CLP installment (`2/3`, `3/3`, etc.), OR
          2. the row has no active CrossMatchResult claim, OR
          3. its existing CrossMatchResult links a voucher on the same sale_date (idempotent re-upload).
    """
    clean_codes = {normalize_auth_code(c) for c in (voucher_codes or set()) if normalize_auth_code(c)}
    if not clean_codes:
        return []

    card_filter = (
        Q(card_type__isnull=False) | Q(payment_code__in=("VUS$", "MCUS$", "MUS$", "AUS$", "DUS$"))
        if source_currency == "USD"
        else Q(card_type__isnull=False) | Q(payment_code__in=("RCO", "RC", "REDCOMPRA", "V$", "MC$", "AME$", "DIN$"))
    )

    excl_keys = {k for k in (exclude_source_keys or set()) if k}
    excl_ids = {i for i in (exclude_ids or set()) if i}
    v_dates_map = voucher_dates_by_code or {}
    allowed_dates = expand_date_window(
        (sd for dates in v_dates_map.values() for sd in dates),
        back_days=7,
        forward_days=14,
    )

    # Chunked __icontains queries keep SQLite expression tree well within limits even on large settlements
    code_list = sorted(clean_codes)
    matched_rows: Dict[int, ErpTransaction] = {}
    for start in range(0, len(code_list), 100):
        chunk = code_list[start : start + 100]
        q = Q()
        for code in chunk:
            q |= Q(authorization_code__icontains=code)
        qs = (
            ErpTransaction.objects.filter(source_currency=source_currency)
            .filter(card_filter)
            .filter(q)
        )
        if allowed_dates:
            qs = qs.filter(Q(transaction_date__in=allowed_dates) | Q(transaction_date__isnull=True))
        qs = qs.select_related("card_type", "process").order_by("reconciled", "id")
        for row in qs:
            if row.id in excl_ids:
                continue
            if row.source_key and row.source_key in excl_keys:
                continue
            norm = normalize_auth_code(row.authorization_code or "")
            if norm not in clean_codes:
                continue
            incoming_dates = v_dates_map.get(norm)
            if row.transaction_date is not None and incoming_dates:
                if not any(-7 <= (row.transaction_date - sd).days <= 14 for sd in incoming_dates):
                    continue
            matched_rows[row.id] = row

    if not matched_rows:
        return []

    reconciled_ids = [r.id for r in matched_rows.values() if r.reconciled]
    claimed_dates_by_erp: Dict[int, Set[Optional[date]]] = {}
    if reconciled_ids:
        for cm_erp_id, cm_sale_date in (
            CrossMatchResult.objects.filter(erp_transaction_id__in=reconciled_ids)
            .exclude(system_result__in=_NOT_LINKED_RESULTS)
            .values_list("erp_transaction_id", "transbank_transaction__sale_date")
        ):
            claimed_dates_by_erp.setdefault(cm_erp_id, set()).add(cm_sale_date)

    later_set = later_installment_codes or set()

    eligible: List[ErpTransaction] = []
    for row in matched_rows.values():
        if not row.reconciled:
            eligible.append(row)
            continue
        norm = normalize_auth_code(row.authorization_code or "")
        if norm in later_set:
            eligible.append(row)
            continue
        claimed_dates = claimed_dates_by_erp.get(row.id)
        if not claimed_dates:
            eligible.append(row)
            continue
        incoming_dates = v_dates_map.get(norm)
        if incoming_dates and (claimed_dates & incoming_dates):
            eligible.append(row)

    eligible.sort(key=lambda r: (0 if not r.reconciled else 1, r.id))
    return eligible
