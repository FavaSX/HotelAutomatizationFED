"""
Financial Reconciliation Services Package.
Exposes the main entry points for:
  - USD in-memory calculation (`usd_reconciliation.py`)
  - Atomic database persistence & deduplication (`reconciliation_persistence.py`)
  - Styled Excel exporting (`report_exporter.py`)
  - CLP reconciliation (`clp_reconciliation.py`)
  - Dynamic column & file detection (`column_detector.py`, `file_detector.py`)
"""

from .usd_reconciliation import (
    execute_usd_reconciliation,
    calculate_usd_preview,
)
from .reconciliation_persistence import commit_usd_preview_to_database
from .report_exporter import (
    generate_usd_preview_excel_bytes,
    generate_reconciliation_excel_bytes,
)
from .clp_reconciliation import execute_clp_reconciliation
from .column_detector import identify_card_brand_from_bin, resolve_dataframe_columns
from .file_detector import detect_and_align_usd_files

__all__ = [
    "execute_usd_reconciliation",
    "calculate_usd_preview",
    "commit_usd_preview_to_database",
    "generate_usd_preview_excel_bytes",
    "generate_reconciliation_excel_bytes",
    "execute_clp_reconciliation",
    "identify_card_brand_from_bin",
    "resolve_dataframe_columns",
    "detect_and_align_usd_files",
]
