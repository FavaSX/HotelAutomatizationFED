"""
Views for the US Dollars (USD) and Chilean Pesos (CLP) Financial Reconciliation Workflows.
  - Login / logout (`auth.py`)
  - Home dashboard with the Dólares / Pesos panels (`dashboard.py`)
  - USD workflow: upload, preview, commit, discard, export, file detection (`usd.py`)
  - CLP workflow, same lifecycle (`clp.py`)
  - Pendientes inbox: review, resolve, reopen and export exceptions (`exceptions.py`)
  - System tools: tolerance settings and test database cleanup (`admin_tools.py`)
  - Superuser user lifecycle management (`users.py`)
"""

from .auth import login_view, logout_view
from .dashboard import dashboard_view
from .usd import (
    usd_upload_view,
    usd_preview_view,
    usd_discard_view,
    usd_commit_view,
    usd_export_committed_view,
    usd_detect_files_view,
    usd_preview_correction_view,
)
from .clp import (
    clp_upload_view,
    clp_detect_files_view,
    clp_preview_view,
    clp_discard_view,
    clp_commit_view,
    clp_export_committed_view,
    clp_preview_correction_view,
)
from .exceptions import (
    exception_erp_add_view,
    exception_erp_delete_view,
    exception_erp_edit_view,
    exception_suggestion_accept_view,
    exception_suggestion_reject_view,
    exceptions_view,
    exceptions_export_view,
    exception_resolve_view,
    exception_reopen_view,
)
from .admin_tools import clear_test_database_view, settings_view
from .users import (
    user_edit_view,
    user_restore_view,
    user_soft_delete_view,
    users_manage_view,
)
from .tables_viewer import (
    audit_trail_view,
    closing_detail_view,
    closings_list_view,
    consolidated_bank_view,
    consolidated_erp_view,
    consolidated_matches_view,
    consolidated_operator_view,
    consolidated_transbank_view,
)

__all__ = [
    "login_view",
    "logout_view",
    "dashboard_view",
    "usd_upload_view",
    "usd_preview_view",
    "usd_discard_view",
    "usd_commit_view",
    "usd_export_committed_view",
    "usd_detect_files_view",
    "clp_upload_view",
    "clp_detect_files_view",
    "clp_preview_view",
    "clp_discard_view",
    "clp_commit_view",
    "clp_export_committed_view",
    "exceptions_view",
    "exceptions_export_view",
    "exception_resolve_view",
    "exception_reopen_view",
    "exception_erp_edit_view",
    "exception_erp_add_view",
    "exception_erp_delete_view",
    "exception_suggestion_accept_view",
    "clp_preview_correction_view",
    "usd_preview_correction_view",
    "exception_suggestion_reject_view",
    "clear_test_database_view",
    "settings_view",
    "closings_list_view",
    "closing_detail_view",
    "consolidated_erp_view",
    "consolidated_transbank_view",
    "consolidated_bank_view",
    "consolidated_operator_view",
    "consolidated_matches_view",
    "audit_trail_view",
    "users_manage_view",
    "user_edit_view",
    "user_soft_delete_view",
    "user_restore_view",
]
