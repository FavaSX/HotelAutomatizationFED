"""
URL configuration for BankReconciliation project.
Covers US Dollars (Phase 1, Preview & Commit) and Chilean Pesos (Phase 2) reconciliation.
"""

from django.contrib import admin
from django.urls import path
from web import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.dashboard_view, name='dashboard'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('historial/', views.history_view, name='history'),
    path('configuracion/', views.settings_view, name='settings'),
    path('reconciliation/usd/', views.usd_upload_view, name='usd_upload'),
    path('reconciliation/usd/preview/', views.usd_preview_view, name='usd_preview'),
    path('reconciliation/usd/commit/', views.usd_commit_view, name='usd_commit'),
    path('reconciliation/usd/discard/', views.usd_discard_view, name='usd_discard'),
    path('reconciliation/usd/export/<int:process_id>/', views.usd_export_committed_view, name='usd_export_committed'),
    path('reconciliation/usd/detect-files/', views.usd_detect_files_view, name='usd_detect_files'),
    path('reconciliation/clear-test-db/', views.clear_test_database_view, name='clear_test_db'),
    path('reconciliation/clp/', views.clp_upload_view, name='clp_upload'),
    path('reconciliation/clp/result/<int:process_id>/', views.clp_result_view, name='clp_result'),
    path('reconciliation/clp/export/<int:process_id>/', views.clp_export_view, name='clp_export'),
]
