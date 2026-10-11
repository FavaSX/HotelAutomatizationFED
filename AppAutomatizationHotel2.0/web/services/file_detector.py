"""
Automatic File Role Detector for US Dollars (USD) Reconciliation.
Inspects the internal cell structure (first 45 rows) of uploaded Excel files
to automatically classify each file into one of the 7 required USD reconciliation slots.
"""

import io
from typing import Any, Dict, List, Optional
import pandas as pd

from .column_detector import normalize_text


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
    ("erp", ("erp", "terjetas", "tarjetas erp", "bordero")),
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

        dataframe = pd.read_excel(io.BytesIO(raw_bytes), header=None)
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

    # Fallback: filename keywords
    for role_key, keywords in FILENAME_FALLBACKS:
        if any(kw in normalized_filename for kw in keywords):
            return _build_result(role_key, "MEDIUM", f"Identificado por nombre de archivo ('{file_name}')")

    return _build_result(None, "LOW", "No se encontró una huella contable reconocida en las primeras filas")


def detect_and_align_usd_files(uploaded_files: List[Any]) -> Dict[str, Any]:
    """
    Inspects a batch of uploaded Excel files and aligns them against the 7 required USD roles.
    """
    inspected_files = [inspect_single_excel_file(f, idx) for idx, f in enumerate(uploaded_files)]
    used_file_indices = set()
    aligned_roles: List[Dict[str, Any]] = []

    for role_meta in USD_REQUIRED_ROLES:
        role_key = role_meta["role_key"]
        candidates = [
            c for c in inspected_files
            if c["file_index"] not in used_file_indices and c["detected_role"] == role_key
        ]
        # Prefer HIGH confidence over MEDIUM
        candidates.sort(key=lambda c: 0 if c["confidence"] == "HIGH" else 1)
        matched_info = candidates[0] if candidates else None

        if matched_info:
            used_file_indices.add(matched_info["file_index"])
            aligned_roles.append({
                **role_meta,
                "matched": True,
                "file_index": matched_info["file_index"],
                "file_name": matched_info["file_name"],
                "confidence": matched_info["confidence"],
                "row_count": matched_info["row_count"],
                "evidence": matched_info["evidence"],
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
            })

    return {
        "aligned_roles": aligned_roles,
        "inspected_files": inspected_files,
        "unassigned_files": [f for f in inspected_files if f["file_index"] not in used_file_indices],
        "all_matched": all(r["matched"] for r in aligned_roles),
        "matched_count": sum(1 for r in aligned_roles if r["matched"]),
        "total_required": len(USD_REQUIRED_ROLES),
    }
