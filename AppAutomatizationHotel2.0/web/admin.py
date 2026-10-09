from django.contrib import admin
from .models import (
    CardType,
    ReconciliationStatus,
    SystemConfiguration,
    ReconciliationProcess,
    BankStatementMovement,
    BankDepositValidation,
    ReconciliationSummary,
    ErpTransaction,
    TransbankTransaction,
    CardOperatorTransaction,
    CrossMatchResult,
    AuditResolutionLog,
)


@admin.register(CardType)
class CardTypeAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'bin_prefixes')
    search_fields = ('code', 'name')


@admin.register(ReconciliationStatus)
class ReconciliationStatusAdmin(admin.ModelAdmin):
    list_display = ('code', 'display_name')


@admin.register(SystemConfiguration)
class SystemConfigurationAdmin(admin.ModelAdmin):
    list_display = ('usd_rounding_tolerance', 'clp_rounding_tolerance', 'updated_at', 'updated_by')


@admin.register(ReconciliationProcess)
class ReconciliationProcessAdmin(admin.ModelAdmin):
    list_display = ('id', 'process_currency', 'accounting_period', 'user', 'execution_date', 'daily_dollar_rate')
    list_filter = ('process_currency', 'execution_date')
    search_fields = ('accounting_period', 'user__username')


@admin.register(BankStatementMovement)
class BankStatementMovementAdmin(admin.ModelAdmin):
    list_display = ('process', 'movement_date', 'description', 'deposit_amount', 'charge_amount', 'is_transbank_deposit')
    list_filter = ('is_transbank_deposit', 'movement_date')
    search_fields = ('description', 'document_number')


@admin.register(BankDepositValidation)
class BankDepositValidationAdmin(admin.ModelAdmin):
    list_display = ('process', 'payment_modality', 'transbank_calculated_deposit', 'bank_statement_deposit', 'is_matched')
    list_filter = ('payment_modality', 'is_matched')


@admin.register(ReconciliationSummary)
class ReconciliationSummaryAdmin(admin.ModelAdmin):
    list_display = ('process', 'card_type', 'currency_table', 'erp_sales_total', 'transbank_sales_total', 'total_commissions', 'difference')
    list_filter = ('currency_table', 'card_type')


@admin.register(ErpTransaction)
class ErpTransactionAdmin(admin.ModelAdmin):
    list_display = ('process', 'card_type', 'document_number', 'authorization_code', 'amount', 'room_number', 'guest_name', 'cashier_username', 'reconciled')
    list_filter = ('reconciled', 'card_type', 'cashier_username')
    search_fields = ('authorization_code', 'document_number', 'guest_name', 'room_number', 'cashier_username')


@admin.register(TransbankTransaction)
class TransbankTransactionAdmin(admin.ModelAdmin):
    list_display = ('process', 'card_type', 'payment_modality', 'authorization_code', 'original_sale_amount', 'transbank_commission', 'net_deposit_amount', 'merchant_location_name', 'reconciled')
    list_filter = ('reconciled', 'payment_modality', 'card_type', 'merchant_location_name')
    search_fields = ('authorization_code', 'masked_card_number', 'transaction_identifier', 'receipt_number')


@admin.register(CardOperatorTransaction)
class CardOperatorTransactionAdmin(admin.ModelAdmin):
    list_display = ('process', 'card_type', 'document_number', 'foreign_currency_amount', 'balance_amount', 'corrected_balance_amount')
    list_filter = ('card_type',)
    search_fields = ('document_number', 'authorization_code')


@admin.register(CrossMatchResult)
class CrossMatchResultAdmin(admin.ModelAdmin):
    list_display = ('process', 'card_type', 'authorization_code', 'system_result', 'difference_usd', 'difference_clp', 'room_number', 'guest_name', 'cashier_username', 'management_status')
    list_filter = ('system_result', 'management_status', 'card_type', 'merchant_location_name')
    search_fields = ('authorization_code', 'document_number', 'guest_name', 'room_number', 'cashier_username')


@admin.register(AuditResolutionLog)
class AuditResolutionLogAdmin(admin.ModelAdmin):
    list_display = ('cross_match_result', 'user', 'previous_status', 'new_status', 'created_at')
    list_filter = ('new_status', 'created_at')
