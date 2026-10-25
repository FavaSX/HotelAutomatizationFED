"""
Automatic File Role Detector for US Dollars (USD) and Chilean Pesos (CLP) Reconciliation.
Inspects the internal cell structure (first 45 rows) of uploaded Excel files
to automatically classify each file into one of the 7 required USD slots or the 5 required CLP slots.
Only the ERP / Borderó role supports grouping multiple files (e.g. shifts, weekend days, auxiliary files).
All other roles (Cartola, Transbank, Card Operators) strictly assign 1 file per role.
"""

import io
from typing import Any, Dict, List, Optional
import pandas as pd

from .column_detector import normalize_text, safe_date


USD_REQUIRED_ROLES: List[Dict[str, str]] = [
    {"role_key": "erp", "input_name": "usd_erp_file", "role_label": "1. Consolidado ERP Hotel", "badge": "ERP", "badge_class": "bg-primary"},
    {"role_key": "transbank", "input_name": "usd_transbank_file", "role_label": "2. Liquidación Abonos TBK", "badge": "TBK", "badge_class": "bg-info text-dark"},
    {"role_key": "bank", "input_name": "usd_bank_file", "role_label": "3. Cartola Bancaria", "badge": "BANCO", "badge_class": "bg-success"},
    {"role_key": "amex", "input_name": "usd_amex_file", "role_label": "4. American Express (USD)", "badge": "AX", "badge_class": "bg-dark"},
    {"role_key": "diners", "input_name": "usd_diners_file", "role_label": "5. Diners Club (USD)", "badge": "DI", "badge_class": "bg-secondary"},
    {"role_key": "visa", "input_name": "usd_visa_file", "role_label": "6. Visa Internacional (USD)", "badge": "VI", "badge_class": "bg-primary"},
    {"role_key": "mastercard", "input_name": "usd_mastercard_file", "role_label": "7. MasterCard Internacional (USD)", "badge": "MC", "badge_class": "bg-warning text-dark"},
]

OPERATOR_KEYWORDS = [
    ("amex", ("amex", "american express")),
    ("diners", ("dinners", "diners")),
    ("mastercard", ("mastercard", "master card")),
    ("visa", ("visa",)),
]

FILENAME_FALLBACKS = [
    ("transbank", ("abono", "tbk", "transbank")),
    ("bank", ("cartola", "banco", "chile")),
    ("amex", ("amex", "american")),
    ("diners", ("diners", "dinners")),
    ("visa", ("visa",)),
    ("mastercard", ("master", "mc")),
]


def inspect_single_excel_file(uploaded_file, file_index: int) -> Dict[str, Any]:
    """
    Reads the internal cells of an uploaded Excel file and returns its detected role_key,
    confidence level, row count, and the internal evidence found.
    Empty files (0 rows) are strictly marked as unassigned without guessing.
    """
    file_name = getattr(uploaded_file, "name", f"archivo_{file_index}")
    normalized_filename = normalize_text(file_name)
    total_rows = 0

    def _build_result(role: Optional[str], confidence: str, evidence: str) -> Dict[str, Any]:
        return {
            "file_index": file_index,
            "file_name": file_name,
            "detected_role": role,
            "confidence": confidence,
            "row_count": total_rows,
            "evidence": evidence,
        }

    try:
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)
        raw_bytes = uploaded_file.read()
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)

        dataframe = None
        try:
            dataframe = pd.read_excel(io.BytesIO(raw_bytes), header=None)
        except Exception:
            try:
                tables = pd.read_html(io.BytesIO(raw_bytes))
                if tables:
                    dataframe = tables[0]
            except Exception:
                pass

        if dataframe is None or len(dataframe) == 0:
            return _build_result(None, "LOW", "Archivo vacío sin registros (0 filas)")

        total_rows = len(dataframe)

        raw_lines = [
            " | ".join(str(cell).strip() for cell in row if pd.notna(cell) and str(cell).strip())
            for _, row in dataframe.head(45).iterrows()
        ]
        raw_lines = [line for line in raw_lines if line]
        combined_raw_text = "\n".join(raw_lines)
        normalized_content = normalize_text(combined_raw_text)

        # 1. Card Operator statements ("Posicion Por Clientes" / "Otra Moneda" + "Saldo Corregido")
        if "posicion por clientes" in normalized_content or (
            "otra moneda" in normalized_content and "saldo corregido" in normalized_content
        ):
            client_line = next(
                (line for line in raw_lines if "TARJETA" in line.upper() or "TRANSBANK" in line.upper()),
                "Reporte de Posición por Clientes (Otra Moneda / Saldo)",
            )
            client_line = client_line.split("| Tel")[0].replace("|", "").strip()[:85]
            for role_key, keywords in OPERATOR_KEYWORDS:
                if any(kw in normalized_content for kw in keywords):
                    return _build_result(role_key, "HIGH", f"Huella interna: {client_line}")

        # 2. Transbank Settlement ("Abono Calculado" / "Codigo Local" / "Identificador de Transaccion")
        if (
            "abono calculado" in normalized_content
            or ("codigo local" in normalized_content and "datos empresa" in normalized_content)
            or "identificador de transaccion" in normalized_content
        ):
            return _build_result(
                "transbank",
                "HIGH",
                "Huella interna: Bloque 'Datos Empresa / Abono Calculado' y columnas POS Transbank",
            )

        # 3. Bank Statement ("Saldo Disponible" + "Saldo Contable" or "Cargos" + "Abonos")
        if (
            ("saldo disponible" in normalized_content and "saldo contable" in normalized_content)
            or ("cargos" in normalized_content and "abonos" in normalized_content and "sucursal" in normalized_content)
            or "retenciones 24 hrs" in normalized_content
        ):
            return _build_result(
                "bank",
                "HIGH",
                "Huella interna: Cartola Cuenta Corriente USD ('Saldo Disponible', 'Cargos', 'Abonos')",
            )

        # 4. Hotel ERP ("AME$", "VIS$", "MAS$", "DIN$" or ERP specific headers)
        if any(tok in combined_raw_text.upper() for tok in ("AME$", "VIS$", "MAS$", "DIN$", "AMEX $", "VISA $")) or (
            "reserva" in normalized_content and "factura" in normalized_content
        ):
            return _build_result(
                "erp",
                "HIGH",
                "Huella interna: 'HOTELERA SAN FRANCISCO S.A' + Códigos de tarjeta ERP (AME$, VIS$, MAS$, DIN$)",
            )

    except Exception:
        pass

    # Fallback por nombre solo para operadores y banco (nunca para ERP, para evitar falsos positivos con 0 filas)
    for role_key, keywords in FILENAME_FALLBACKS:
        if any(kw in normalized_filename for kw in keywords):
            return _build_result(role_key, "MEDIUM", f"Identificado por nombre de archivo ('{file_name}')")

    return _build_result(None, "LOW", "No se encontró una huella contable reconocida en las primeras filas")


def detect_and_align_usd_files(uploaded_files: List[Any]) -> Dict[str, Any]:
    """Inspects a batch of uploaded Excel files and aligns them against the 7 required USD roles."""
    inspected_files = [inspect_single_excel_file(f, idx) for idx, f in enumerate(uploaded_files)]
    return align_inspected_files(inspected_files, USD_REQUIRED_ROLES)


def align_inspected_files(inspected_files: List[Dict[str, Any]], required_roles: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Assigns inspected files to required roles.
    ONLY the ERP role permits grouping multiple files (e.g. shifts, multi-day, auxiliary borderos).
    All other roles strictly assign exactly 1 file per role.
    """
    used_file_indices = set()
    aligned_roles: List[Dict[str, Any]] = []

    for role_meta in required_roles:
        role_key = role_meta["role_key"]
        candidates = [
            c for c in inspected_files
            if c["file_index"] not in used_file_indices and c["detected_role"] == role_key and c.get("row_count", 0) > 0
        ]
        # Prefer HIGH confidence, then most recent period, then largest row count
        candidates.sort(
            key=lambda c: (0 if c["confidence"] == "HIGH" else 1, -(c.get("period_end_ord") or 0), -c["row_count"])
        )
        matched_info = candidates[0] if candidates else None

        if matched_info:
            used_file_indices.add(matched_info["file_index"])
            extra_files_info = []

            # ONLY the ERP role allows grouping multiple files:
            if role_key == "erp":
                for extra in candidates[1:]:
                    if extra.get("confidence") == "HIGH" and extra.get("row_count", 0) > 0:
                        used_file_indices.add(extra["file_index"])
                        extra_files_info.append({
                            "file_index": extra["file_index"],
                            "file_name": extra["file_name"],
                            "row_count": extra["row_count"],
                            "evidence": extra["evidence"],
                        })

            aligned_roles.append({
                **role_meta,
                "matched": True,
                "file_index": matched_info["file_index"],
                "file_name": matched_info["file_name"],
                "confidence": matched_info["confidence"],
                "row_count": matched_info["row_count"],
                "evidence": matched_info["evidence"],
                "extra_files": extra_files_info,
            })
        else:
            aligned_roles.append({
                **role_meta,
                "matched": False,
                "file_index": None,
                "file_name": None,
                "confidence": "MISSING",
                "row_count": 0,
                "evidence": "No se detectó ningún archivo para este rol en el lote cargado.",
                "extra_files": [],
            })

    return {
        "aligned_roles": aligned_roles,
        "inspected_files": inspected_files,
        "unassigned_files": [f for f in inspected_files if f["file_index"] not in used_file_indices],
        "all_matched": all(r["matched"] for r in aligned_roles),
        "matched_count": sum(1 for r in aligned_roles if r["matched"]),
        "total_required": len(required_roles),
    }


# ==============================================================================
# CLP (PESOS) FILE DETECTION
# ==============================================================================

CLP_REQUIRED_ROLES: List[Dict[str, str]] = [
    {"role_key": "erp", "input_name": "clp_erp_file", "role_label": "1. Borderó ERP Hotel", "badge": "ERP", "badge_class": "bg-primary"},
    {"role_key": "bank", "input_name": "clp_bank_file", "role_label": "2. Cartola Bancaria (Pesos)", "badge": "BANCO", "badge_class": "bg-success"},
    {"role_key": "tbk_credit", "input_name": "clp_tbk_credit_file", "role_label": "3. Abono Transbank Crédito", "badge": "TBK", "badge_class": "bg-info text-dark"},
    {"role_key": "tbk_debit", "input_name": "clp_tbk_debit_file", "role_label": "4. Abono Transbank Débito", "badge": "TBK", "badge_class": "bg-info text-dark"},
    {"role_key": "tbk_prepaid", "input_name": "clp_tbk_prepaid_file", "role_label": "5. Abono Transbank Prepago", "badge": "TBK", "badge_class": "bg-info text-dark"},
]

CLP_TBK_MEDIUM_ROLES = {"credito": "tbk_credit", "debito": "tbk_debit", "prepago": "tbk_prepaid"}

CLP_FILENAME_FALLBACKS = [
    ("tbk_credit", ("credito",)),
    ("tbk_debit", ("debito",)),
    ("tbk_prepaid", ("prepago",)),
    ("bank", ("cartola", "banco")),
]


def inspect_single_clp_excel_file(uploaded_file, file_index: int) -> Dict[str, Any]:
    """
    Reads the first rows of an uploaded Excel file and classifies it as one of the 5 CLP roles.
    The 3 Transbank settlements share the same layout; only the 'Medio de pago' cell tells them apart.
    """
    file_name = getattr(uploaded_file, "name", f"archivo_{file_index}")
    result: Dict[str, Any] = {
        "file_index": file_index,
        "file_name": file_name,
        "detected_role": None,
        "confidence": "LOW",
        "row_count": 0,
        "period_end_ord": 0,
        "evidence": "No se encontró una huella contable reconocida en las primeras filas",
    }

    try:
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)
        raw_bytes = uploaded_file.read()
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)

        dataframe = None
        try:
            dataframe = pd.read_excel(io.BytesIO(raw_bytes), header=None)
        except Exception:
            try:
                tables = pd.read_html(io.BytesIO(raw_bytes))
                if tables:
                    dataframe = tables[0]
            except Exception:
                pass

        if dataframe is None or len(dataframe) == 0:
            result.update(detected_role=None, confidence="LOW", row_count=0, evidence="Archivo vacío sin registros (0 filas)")
            return result

        result["row_count"] = len(dataframe)
        head = dataframe.head(45)
        normalized_content = normalize_text(" | ".join(
            str(cell) for _, row in head.iterrows() for cell in row if pd.notna(cell)
        ))

        # 1. Transbank 'Abonos por día': read the value below the 'Medio de pago:' label
        if "abonos por dia" in normalized_content and "medio de pago" in normalized_content:
            settlement_date = None
            for _, r in head.iterrows():
                cells = [val for val in r.values if pd.notna(val) and str(val).strip()]
                for pos, val in enumerate(cells):
                    if normalize_text(val).startswith("periodo de consulta") and pos + 1 < len(cells):
                        settlement_date = safe_date(cells[pos + 1])
            for row_index in range(len(head) - 1):
                for col_index, cell in enumerate(head.iloc[row_index].values):
                    if normalize_text(cell).startswith("medio de pago"):
                        medium_raw = str(head.iloc[row_index + 1].values[col_index]).strip()
                        role = CLP_TBK_MEDIUM_ROLES.get(normalize_text(medium_raw))
                        if role:
                            date_suffix = f" ({settlement_date.strftime('%d/%m/%Y')})" if settlement_date else ""
                            result.update(
                                detected_role=role,
                                confidence="HIGH",
                                period_end_ord=settlement_date.toordinal() if settlement_date else 0,
                                evidence=f"Huella interna: Transbank 'Abonos por día' · Medio de pago: {medium_raw}{date_suffix}",
                            )
                            return result

        # 2. Bordero 'Cargos de Débitos y Créditos'
        if "cargos de debitos y creditos" in normalized_content or (
            "cod. deb" in normalized_content and "reserva" in normalized_content and "factura" in normalized_content
        ):
            period_dates = []
            for _, r in head.iloc[:15].iterrows():
                cells = [val for val in r.values if pd.notna(val) and str(val).strip()]
                if cells and normalize_text(cells[0]).startswith("periodo de"):
                    period_dates = [d for d in (safe_date(v) for v in cells[1:]) if d]
                    break
            period_label = ""
            if period_dates:
                d_start, d_end = min(period_dates), max(period_dates)
                period_label = f" ({d_start.strftime('%d/%m/%Y')} al {d_end.strftime('%d/%m/%Y')})"
                result["period_end_ord"] = d_end.toordinal()
            result.update(
                detected_role="erp",
                confidence="HIGH",
                evidence=f"Huella interna: Borderó FrontOffice 'Cargos de Débitos y Créditos'{period_label}",
            )
            return result

        # 3. Bank statement in pesos
        if ("saldo disponible" in normalized_content and "saldo contable" in normalized_content) or (
            "cargos" in normalized_content and "abonos" in normalized_content and "sucursal" in normalized_content
        ):
            currency = "Pesos Chilenos (CLP)" if "pesos" in normalized_content else "moneda no indicada"
            result.update(detected_role="bank", confidence="HIGH", evidence=f"Huella interna: Cartola Cuenta Corriente ({currency})")
            return result
    except Exception:
        pass

    normalized_filename = normalize_text(file_name)
    for role_key, keywords in CLP_FILENAME_FALLBACKS:
        if any(kw in normalized_filename for kw in keywords):
            result.update(detected_role=role_key, confidence="MEDIUM", evidence=f"Identificado por nombre de archivo ('{file_name}')")
            break
    return result


def detect_and_align_clp_files(uploaded_files: List[Any]) -> Dict[str, Any]:
    """Inspects a batch of uploaded Excel files and aligns them against the 5 required CLP roles."""
    inspected_files = [inspect_single_clp_excel_file(f, idx) for idx, f in enumerate(uploaded_files)]
    return align_inspected_files(inspected_files, CLP_REQUIRED_ROLES)
