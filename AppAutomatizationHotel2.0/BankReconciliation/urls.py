"""
URL configuration for BankReconciliation project.
US Dollars (Phase 1) and Chilean Pesos (Phase 2) reconciliation with Preview & Commit.
"""

from django.contrib import admin
from django.urls import path
from web import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.dashboard_view, name='dashboard'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('reconciliation/usd/', views.usd_upload_view, name='usd_upload'),
    path('reconciliation/usd/preview/', views.usd_preview_view, name='usd_preview'),
    path('reconciliation/usd/commit/', views.usd_commit_view, name='usd_commit'),
    path('reconciliation/usd/discard/', views.usd_discard_view, name='usd_discard'),
    path('reconciliation/usd/export/<int:process_id>/', views.usd_export_committed_view, name='usd_export_committed'),
    path('reconciliation/usd/detect-files/', views.usd_detect_files_view, name='usd_detect_files'),
    path('reconciliation/clp/', views.clp_upload_view, name='clp_upload'),
    path('reconciliation/clp/preview/', views.clp_preview_view, name='clp_preview'),
    path('reconciliation/clp/commit/', views.clp_commit_view, name='clp_commit'),
    path('reconciliation/clp/discard/', views.clp_discard_view, name='clp_discard'),
    path('reconciliation/clp/export/<int:process_id>/', views.clp_export_committed_view, name='clp_export_committed'),
    path('reconciliation/clp/detect-files/', views.clp_detect_files_view, name='clp_detect_files'),
    path('settings/', views.settings_view, name='settings'),
    path('reconciliation/clear-test-db/', views.clear_test_database_view, name='clear_test_db'),
]
