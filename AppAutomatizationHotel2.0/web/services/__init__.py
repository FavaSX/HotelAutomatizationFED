"""
Financial Reconciliation Services Package.
Exposes the main entry points for:
  - USD in-memory calculation (`usd_reconciliation.py`)
  - CLP in-memory calculation (`clp_reconciliation.py`)
  - Level 2 assisted secondary matching (`suggestion_engine.py`, `clp_suggestion_engine.py`)
  - Atomic database persistence & deduplication (`reconciliation_persistence.py`, `clp_persistence.py`)
  - Styled Excel exporting (`report_exporter.py`, `clp_report_exporter.py`)
  - Dynamic column & file detection (`column_detector.py`, `file_detector.py`)
"""

from .usd_reconciliation import calculate_usd_preview
from .reconciliation_persistence import commit_usd_preview_to_database
from .report_exporter import generate_committed_usd_excel_bytes
from .column_detector import resolve_dataframe_columns
from .file_detector import detect_and_align_usd_files, detect_and_align_clp_files
from .suggestion_engine import enrich_preview_with_suggestions
from .clp_reconciliation import calculate_clp_preview
from .clp_persistence import commit_clp_preview_to_database
from .clp_report_exporter import generate_committed_clp_excel_bytes

__all__ = [
    "calculate_usd_preview",
    "commit_usd_preview_to_database",
    "generate_committed_usd_excel_bytes",
    "resolve_dataframe_columns",
    "detect_and_align_usd_files",
    "enrich_preview_with_suggestions",
    "calculate_clp_preview",
    "commit_clp_preview_to_database",
    "generate_committed_clp_excel_bytes",
    "detect_and_align_clp_files",
]
