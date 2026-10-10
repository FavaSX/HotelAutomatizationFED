"""
URL configuration for BankReconciliation project.
Focused on US Dollars (Phase 1) reconciliation with Preview & Commit.
"""

from django.contrib import admin
from django.urls import path
from web import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.usd_upload_view, name='usd_upload'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('reconciliation/usd/preview/', views.usd_preview_view, name='usd_preview'),
    path('reconciliation/usd/commit/', views.usd_commit_view, name='usd_commit'),
    path('reconciliation/usd/discard/', views.usd_discard_view, name='usd_discard'),
    path('reconciliation/usd/export-draft/', views.usd_export_draft_view, name='usd_export_draft'),
    path('reconciliation/clear-test-db/', views.clear_test_database_view, name='clear_test_db'),
]
