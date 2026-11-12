"""
URL configuration for BankReconciliation project.
US Dollars (Phase 1) and Chilean Pesos (Phase 2) reconciliation with Preview & Commit.
"""

import os

from django.contrib import admin
from django.urls import path
from web import views

urlpatterns = [
    # Non-default admin path set per deployment (DJANGO_ADMIN_URL) so bots probing /admin/ get a 404
    path(os.environ.get('DJANGO_ADMIN_URL', 'admin/'), admin.site.urls),
    path('', views.dashboard_view, name='dashboard'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('reconciliation/usd/', views.usd_upload_view, name='usd_upload'),
    path('reconciliation/usd/preview/', views.usd_preview_view, name='usd_preview'),
    path('reconciliation/usd/commit/', views.usd_commit_view, name='usd_commit'),
    path('reconciliation/usd/preview/correccion/', views.usd_preview_correction_view, name='usd_preview_correction'),
    path('reconciliation/usd/discard/', views.usd_discard_view, name='usd_discard'),
    path('reconciliation/usd/export/<int:process_id>/', views.usd_export_committed_view, name='usd_export_committed'),
    path('reconciliation/usd/detect-files/', views.usd_detect_files_view, name='usd_detect_files'),
    path('reconciliation/clp/', views.clp_upload_view, name='clp_upload'),
    path('reconciliation/clp/preview/', views.clp_preview_view, name='clp_preview'),
    path('reconciliation/clp/commit/', views.clp_commit_view, name='clp_commit'),
    path('reconciliation/clp/preview/correccion/', views.clp_preview_correction_view, name='clp_preview_correction'),
    path('reconciliation/clp/discard/', views.clp_discard_view, name='clp_discard'),
    path('reconciliation/clp/export/<int:process_id>/', views.clp_export_committed_view, name='clp_export_committed'),
    path('reconciliation/clp/detect-files/', views.clp_detect_files_view, name='clp_detect_files'),
    path('excepciones/', views.exceptions_view, name='exceptions'),
    path('excepciones/exportar/', views.exceptions_export_view, name='exceptions_export'),
    path('excepciones/<int:match_id>/resolver/', views.exception_resolve_view, name='exception_resolve'),
    path('excepciones/<int:match_id>/reabrir/', views.exception_reopen_view, name='exception_reopen'),
    path('excepciones/<int:match_id>/erp/<int:row_id>/corregir/', views.exception_erp_edit_view, name='exception_erp_edit'),
    path('excepciones/<int:match_id>/erp/agregar/', views.exception_erp_add_view, name='exception_erp_add'),
    path('excepciones/<int:match_id>/sugerencia/aceptar/', views.exception_suggestion_accept_view, name='exception_suggestion_accept'),
    path('excepciones/<int:match_id>/sugerencia/rechazar/', views.exception_suggestion_reject_view, name='exception_suggestion_reject'),
    path('excepciones/<int:match_id>/erp/<int:row_id>/eliminar/', views.exception_erp_delete_view, name='exception_erp_delete'),
    path('settings/', views.settings_view, name='settings'),
    path('reconciliation/clear-test-db/', views.clear_test_database_view, name='clear_test_db'),
    path('tablas/', views.closings_list_view, name='closings_list'),
    path('tablas/erp/', views.consolidated_erp_view, name='consolidated_erp'),
    path('tablas/transbank/', views.consolidated_transbank_view, name='consolidated_transbank'),
    path('tablas/cartola/', views.consolidated_bank_view, name='consolidated_bank'),
    path('tablas/operadoras/', views.consolidated_operator_view, name='consolidated_operator'),
    path('tablas/cruces/', views.consolidated_matches_view, name='consolidated_matches'),
    path('tablas/cierre/<int:process_id>/', views.closing_detail_view, name='closing_detail'),
    path('auditoria/', views.audit_trail_view, name='audit_trail'),
    path('usuarios/', views.users_manage_view, name='users_manage'),
    path('usuarios/<int:user_id>/editar/', views.user_edit_view, name='user_edit'),
    path('usuarios/<int:user_id>/eliminar/', views.user_soft_delete_view, name='user_soft_delete'),
    path('usuarios/<int:user_id>/reactivar/', views.user_restore_view, name='user_restore'),
]
