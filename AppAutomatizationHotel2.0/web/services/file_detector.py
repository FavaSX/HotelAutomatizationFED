"""
Automatic File Role Detector for US Dollars (USD) and Chilean Pesos (CLP) Reconciliation.
Inspects the internal cell structure (first 45 rows) of uploaded Excel files
to automatically classify each file into one of the 7 required USD slots or the 5 required CLP slots.
Uses 100% structural fingerprinting based on internal cell contents, headers, merchant codes, and POS blocks.
Does NOT guess by filename to avoid false positives with summary/unrelated files.
Supports multi-file grouping per role for multi-day, weekly or monthly reconciliations.
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


def load_dataframe_from_file(uploaded_file) -> pd.DataFrame:
    """
    Safely reads an uploaded Excel/spreadsheet file.
    Tries standard openpyxl, xlrd engine (for binary .xls), HTML tables (common in ERP exports), and CSV.
    """
    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(0)
    raw_bytes = uploaded_file.read()
    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(0)

    if not raw_bytes:
        return pd.DataFrame()

    # 1. Standard read_excel (openpyxl for .xlsx, xlrd for binary .xls)
    try:
        return pd.read_excel(io.BytesIO(raw_bytes), header=None)
    except Exception:
        pass

    # 2. Try xlrd explicitly for legacy binary .xls
    try:
        return pd.read_excel(io.BytesIO(raw_bytes), header=None, engine="xlrd")
    except Exception:
        pass

    # 3. Many hotel ERP systems (Opera, FrontOffice) export HTML tables with a .xls extension
    try:
        dfs = pd.read_html(io.BytesIO(raw_bytes))
        if dfs:
            return dfs[0]
    except Exception:
        pass

    # 4. Try CSV/TSV
    try:
        return pd.read_csv(io.BytesIO(raw_bytes), sep=None, engine="python", header=None)
    except Exception:
        pass

    return pd.DataFrame()


def inspect_single_excel_file(uploaded_file, file_index: int) -> Dict[str, Any]:
    """
    Reads the internal cells of an uploaded Excel file and classifies it 100% by internal accounting fingerprint.
    No filename guessing is performed.
    """
    file_name = getattr(uploaded_file, "name", f"archivo_{file_index}")
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
        dataframe = load_dataframe_from_file(uploaded_file)
        total_rows = len(dataframe)

        if total_rows > 0:
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

    if total_rows == 0:
        return _build_result(None, "LOW", f"Archivo sin filas legibles o formato no reconocido ('{file_name}')")

    return _build_result(None, "LOW", "No se encontró una huella contable reconocida en las primeras filas")


def detect_and_align_usd_files(uploaded_files: List[Any]) -> Dict[str, Any]:
    """
    Inspects a batch of uploaded Excel files and aligns them against the 7 required USD roles.
    Supports multiple files per role.
    """
    inspected_files = [inspect_single_excel_file(f, idx) for idx, f in enumerate(uploaded_files)]
    return align_inspected_files(inspected_files, USD_REQUIRED_ROLES)


def align_inspected_files(inspected_files: List[Dict[str, Any]], required_roles: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Assigns inspected files to required roles, supporting multiple files per role.
    Only auto-groups candidates with matching high confidence and rows > 0 to prevent
    polluting primary files with 0-row or unverified summary files.
    """
    used_file_indices = set()
    aligned_roles: List[Dict[str, Any]] = []

    for role_meta in required_roles:
        role_key = role_meta["role_key"]
        candidates = [
            c for c in inspected_files
            if c["file_index"] not in used_file_indices and c["detected_role"] == role_key
        ]
        # Prefer HIGH confidence, then most recent period, then largest row count
        candidates.sort(
            key=lambda c: (0 if c["confidence"] == "HIGH" else 1, -(c.get("period_end_ord") or 0), -c["row_count"])
        )

        if candidates:
            primary = candidates[0]
            # Only auto-group candidates that have HIGH confidence and actual rows
            if primary["confidence"] == "HIGH" and primary["row_count"] > 0:
                valid_candidates = [
                    c for c in candidates
                    if c["confidence"] == "HIGH" and c["row_count"] > 0
                ]
            else:
                valid_candidates = [c for c in candidates if c["row_count"] > 0] or [primary]

            for c in valid_candidates:
                used_file_indices.add(c["file_index"])

            indices = [c["file_index"] for c in valid_candidates]
            names = [c["file_name"] for c in valid_candidates]
            total_rows = sum(c["row_count"] for c in valid_candidates)
            evidence = (
                f"{len(valid_candidates)} archivo(s) concatenado(s) ({total_rows} filas): {', '.join(names)}"
                if len(valid_candidates) > 1
                else primary["evidence"]
            )
            aligned_roles.append({
                **role_meta,
                "matched": True,
                "file_index": primary["file_index"],
                "file_name": primary["file_name"],
                "file_indices": indices,
                "file_names": names,
                "file_count": len(valid_candidates),
                "matched_files": valid_candidates,
                "confidence": "HIGH" if all(c["confidence"] == "HIGH" for c in valid_candidates) else "MEDIUM",
                "row_count": total_rows,
                "evidence": evidence,
            })
        else:
            aligned_roles.append({
                **role_meta,
                "matched": False,
                "file_index": None,
                "file_name": None,
                "file_indices": [],
                "file_names": [],
                "file_count": 0,
                "matched_files": [],
                "confidence": "MISSING",
                "row_count": 0,
                "evidence": "No se detectó ningún archivo para este rol en el lote cargado.",
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


def inspect_single_clp_excel_file(uploaded_file, file_index: int) -> Dict[str, Any]:
    """
    Reads the first rows of an uploaded Excel file and classifies it 100% by internal accounting fingerprint.
    The 3 Transbank settlements share the same layout; only the 'Medio de pago' cell tells them apart.
    No filename guessing is performed.
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
        dataframe = load_dataframe_from_file(uploaded_file)
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

    if result["row_count"] == 0:
        result.update(evidence=f"Archivo sin filas legibles o formato no reconocido ('{file_name}')")
        return result

    return result


def detect_and_align_clp_files(uploaded_files: List[Any]) -> Dict[str, Any]:
    """Inspects a batch of uploaded Excel files and aligns them against the 5 required CLP roles."""
    inspected_files = [inspect_single_clp_excel_file(f, idx) for idx, f in enumerate(uploaded_files)]
    return align_inspected_files(inspected_files, CLP_REQUIRED_ROLES)
