from django.db import models
from django.contrib.auth.models import User


# ==========================================
# 1. BASE CATALOGS AND CONFIGURATION
# ==========================================

class CardType(models.Model):
    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=50)
    bin_prefixes = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        db_table = 'card_type'
        verbose_name = 'Card Type'
        verbose_name_plural = 'Card Types'

    def __str__(self):
        return f"{self.code} - {self.name}"


class ReconciliationStatus(models.Model):
    code = models.CharField(max_length=30, unique=True)
    display_name = models.CharField(max_length=50)

    class Meta:
        db_table = 'reconciliation_status'
        verbose_name = 'Reconciliation Status'
        verbose_name_plural = 'Reconciliation Statuses'

    def __str__(self):
        return self.display_name


class SystemConfiguration(models.Model):
    usd_rounding_tolerance = models.DecimalField(max_digits=6, decimal_places=2, default=0.50)
    clp_rounding_tolerance = models.DecimalField(max_digits=8, decimal_places=2, default=5.00)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        db_table = 'system_configuration'
        verbose_name = 'System Configuration'
        verbose_name_plural = 'System Configurations'

    def __str__(self):
        return f"Tolerance: ±${self.usd_rounding_tolerance} USD / ±${self.clp_rounding_tolerance} CLP"


# ==========================================
# 2. RECONCILIATION HEADER, BANK & SUMMARIES
# ==========================================

class ReconciliationProcess(models.Model):
    CURRENCY_CHOICES = [
        ('USD', 'US Dollars (Phase 1)'),
        ('CLP', 'Chilean Pesos (Phase 2)'),
    ]

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='reconciliation_processes')
    execution_date = models.DateTimeField(auto_now_add=True)
    process_currency = models.CharField(max_length=5, choices=CURRENCY_CHOICES)
    accounting_period = models.CharField(max_length=50, blank=True, null=True)
    daily_dollar_rate = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)

    # Uploaded source Excel file names
    erp_file_name = models.CharField(max_length=255, blank=True, null=True)
    bank_statement_file_name = models.CharField(max_length=255, blank=True, null=True)
    transbank_general_file_name = models.CharField(max_length=255, blank=True, null=True)
    transbank_credit_file_name = models.CharField(max_length=255, blank=True, null=True)
    transbank_debit_file_name = models.CharField(max_length=255, blank=True, null=True)
    transbank_prepaid_file_name = models.CharField(max_length=255, blank=True, null=True)
    american_express_file_name = models.CharField(max_length=255, blank=True, null=True)
    diners_file_name = models.CharField(max_length=255, blank=True, null=True)
    visa_file_name = models.CharField(max_length=255, blank=True, null=True)
    mastercard_file_name = models.CharField(max_length=255, blank=True, null=True)

    # Generated output Excel paths
    matched_transactions_file_path = models.CharField(max_length=255, blank=True, null=True)
    unmatched_transactions_file_path = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'reconciliation_process'
        ordering = ['-execution_date']
        verbose_name = 'Reconciliation Process'
        verbose_name_plural = 'Reconciliation Processes'

    def __str__(self):
        return f"Process #{self.id} ({self.process_currency}) - {self.execution_date:%Y-%m-%d %H:%M}"


class BankStatementMovement(models.Model):
    process = models.ForeignKey(ReconciliationProcess, on_delete=models.CASCADE, related_name='bank_movements')
    bank_account_number = models.CharField(max_length=50, blank=True, null=True)
    movement_date = models.DateField(blank=True, null=True)
    description = models.CharField(max_length=255, blank=True, null=True)
    branch_or_channel = models.CharField(max_length=100, blank=True, null=True)
    document_number = models.CharField(max_length=50, blank=True, null=True)
    charge_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    deposit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    balance_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    is_transbank_deposit = models.BooleanField(default=False)

    class Meta:
        db_table = 'bank_statement_movement'
        verbose_name = 'Bank Statement Movement'
        verbose_name_plural = 'Bank Statement Movements'

    def __str__(self):
        return f"{self.movement_date} - {self.description} (${self.deposit_amount})"


class BankDepositValidation(models.Model):
    process = models.ForeignKey(ReconciliationProcess, on_delete=models.CASCADE, related_name='bank_validations')
    bank_movement = models.ForeignKey(BankStatementMovement, on_delete=models.SET_NULL, null=True, blank=True)
    payment_modality = models.CharField(max_length=30)  # USD_GENERAL, CLP_CREDIT, CLP_DEBIT, CLP_PREPAID
    transbank_calculated_deposit = models.DecimalField(max_digits=14, decimal_places=2)
    bank_statement_deposit = models.DecimalField(max_digits=14, decimal_places=2, blank=True, null=True)
    deposit_date = models.DateField(blank=True, null=True)
    is_matched = models.BooleanField(default=False)

    class Meta:
        db_table = 'bank_deposit_validation'
        verbose_name = 'Bank Deposit Validation'
        verbose_name_plural = 'Bank Deposit Validations'

    def __str__(self):
        return f"{self.payment_modality}: {self.transbank_calculated_deposit} ({'Matched' if self.is_matched else 'Unmatched'})"


class ReconciliationSummary(models.Model):
    process = models.ForeignKey(ReconciliationProcess, on_delete=models.CASCADE, related_name='summaries')
    card_type = models.ForeignKey(CardType, on_delete=models.PROTECT)
    payment_modality = models.CharField(max_length=30, blank=True, null=True)
    currency_table = models.CharField(max_length=5)  # USD or CLP

    erp_sales_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    transbank_sales_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_commissions = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_deposit_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    difference = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        db_table = 'reconciliation_summary'
        verbose_name = 'Reconciliation Summary'
        verbose_name_plural = 'Reconciliation Summaries'

    def __str__(self):
        return f"{self.card_type.code} ({self.currency_table}) Diff: {self.difference}"


# ==========================================
# 3. CLEAN DATA EXTRACTED FROM EXCEL FILES
# ==========================================

class ErpTransaction(models.Model):
    process = models.ForeignKey(ReconciliationProcess, on_delete=models.CASCADE, related_name='erp_transactions')
    card_type = models.ForeignKey(CardType, on_delete=models.SET_NULL, null=True, blank=True)

    # Hotel Guest, Room & Receptionist details (from ERP / Bordero columns 0, 1, 2, 3, 6, 11, 12, 13)
    room_number = models.CharField(max_length=20, blank=True, null=True)
    room_type = models.CharField(max_length=20, blank=True, null=True)
    reservation_number = models.CharField(max_length=50, blank=True, null=True)
    account_code = models.CharField(max_length=50, blank=True, null=True)
    payment_code = models.CharField(max_length=20, blank=True, null=True)
    invoice_number = models.CharField(max_length=50, blank=True, null=True)
    document_number = models.CharField(max_length=50)
    authorization_code = models.CharField(max_length=50, blank=True, null=True)
    transaction_date = models.DateField(blank=True, null=True)
    transaction_time = models.TimeField(blank=True, null=True)
    cashier_username = models.CharField(max_length=50, blank=True, null=True)
    guest_name = models.CharField(max_length=150, blank=True, null=True)

    amount = models.DecimalField(max_digits=14, decimal_places=2)
    source_currency = models.CharField(max_length=10, blank=True, null=True)
    reconciled = models.BooleanField(default=False)

    class Meta:
        db_table = 'erp_transaction'
        verbose_name = 'ERP Transaction'
        verbose_name_plural = 'ERP Transactions'

    def __str__(self):
        return f"ERP Doc {self.document_number} | Auth {self.authorization_code} | ${self.amount}"


class TransbankTransaction(models.Model):
    process = models.ForeignKey(ReconciliationProcess, on_delete=models.CASCADE, related_name='transbank_transactions')
    card_type = models.ForeignKey(CardType, on_delete=models.PROTECT)

    payment_modality = models.CharField(max_length=20)  # CREDIT, DEBIT, PREPAID
    merchant_code = models.CharField(max_length=30, blank=True, null=True)
    merchant_location_name = models.CharField(max_length=150, blank=True, null=True)  # Hotel vs Restaurant Bristol
    masked_card_number = models.CharField(max_length=30, blank=True, null=True)  # Column 24 BIN
    transaction_identifier = models.CharField(max_length=50, blank=True, null=True)  # Column 30 TID
    receipt_number = models.CharField(max_length=50, blank=True, null=True)  # Column 31 Boleta
    installment_type = models.CharField(max_length=50, blank=True, null=True)  # Column 8 Tipo cuota
    installment_number = models.CharField(max_length=20, blank=True, null=True)  # Column 9 N cuota
    authorization_code = models.CharField(max_length=50)
    sale_date = models.DateField(blank=True, null=True)

    original_sale_amount = models.DecimalField(max_digits=14, decimal_places=2)
    transbank_commission = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    commission_vat_tax = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_deposit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    reconciled = models.BooleanField(default=False)

    class Meta:
        db_table = 'transbank_transaction'
        verbose_name = 'Transbank Transaction'
        verbose_name_plural = 'Transbank Transactions'

    def __str__(self):
        return f"TBK Auth {self.authorization_code} | {self.card_type.code} | ${self.original_sale_amount}"


class CardOperatorTransaction(models.Model):
    process = models.ForeignKey(ReconciliationProcess, on_delete=models.CASCADE, related_name='operator_transactions')
    card_type = models.ForeignKey(CardType, on_delete=models.PROTECT)

    document_number = models.CharField(max_length=50)
    sequence_number = models.CharField(max_length=20, blank=True, default='')
    authorization_code = models.CharField(max_length=50, blank=True, null=True)
    foreign_currency_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    balance_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    corrected_balance_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        db_table = 'card_operator_transaction'
        verbose_name = 'Card Operator Transaction'
        verbose_name_plural = 'Card Operator Transactions'

    def __str__(self):
        return f"{self.card_type.code} Doc {self.document_number}-{self.sequence_number} | USD {self.foreign_currency_amount}"


# ==========================================
# 4. CROSS-MATCHING AND DISCREPANCY MANAGEMENT
# ==========================================

class CrossMatchResult(models.Model):
    process = models.ForeignKey(ReconciliationProcess, on_delete=models.CASCADE, related_name='cross_match_results')
    card_type = models.ForeignKey(CardType, on_delete=models.PROTECT)

    transbank_transaction = models.ForeignKey(TransbankTransaction, on_delete=models.SET_NULL, null=True, blank=True)
    erp_transaction = models.ForeignKey(ErpTransaction, on_delete=models.SET_NULL, null=True, blank=True)
    card_operator_transaction = models.ForeignKey(CardOperatorTransaction, on_delete=models.SET_NULL, null=True, blank=True)

    authorization_code = models.CharField(max_length=50, blank=True, null=True)
    document_number = models.CharField(max_length=50, blank=True, null=True)
    sale_date = models.DateField(blank=True, null=True)

    # Contextual hotel fields for quick UI display
    merchant_location_name = models.CharField(max_length=150, blank=True, null=True)
    room_number = models.CharField(max_length=20, blank=True, null=True)
    guest_name = models.CharField(max_length=150, blank=True, null=True)
    cashier_username = models.CharField(max_length=50, blank=True, null=True)

    transbank_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    operator_amount_usd = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    erp_amount_clp = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    difference_usd = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    difference_clp = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    # System diagnosis: MATCHED, AMOUNT_MISMATCH, NOT_FOUND_IN_ERP, GHOST_IN_ERP, CARD_TYPE_MISMATCH
    system_result = models.CharField(max_length=40)
    system_status_detail = models.CharField(max_length=255, blank=True, null=True)
    system_observation = models.CharField(max_length=255, blank=True, null=True)

    # Accountant workflow
    management_status = models.ForeignKey(ReconciliationStatus, on_delete=models.PROTECT, null=True, blank=True)
    auditor_comment = models.TextField(blank=True, null=True)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='resolved_matches')
    resolution_date = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'cross_match_result'
        verbose_name = 'Cross Match Result'
        verbose_name_plural = 'Cross Match Results'

    def __str__(self):
        return f"Match {self.authorization_code} - {self.system_result}"


class AuditResolutionLog(models.Model):
    cross_match_result = models.ForeignKey(CrossMatchResult, on_delete=models.CASCADE, related_name='audit_logs')
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    previous_status = models.ForeignKey(ReconciliationStatus, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    new_status = models.ForeignKey(ReconciliationStatus, on_delete=models.PROTECT, related_name='+')
    comment = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'audit_resolution_log'
        ordering = ['-created_at']
        verbose_name = 'Audit Resolution Log'
        verbose_name_plural = 'Audit Resolution Logs'

    def __str__(self):
        return f"Log #{self.id} on Match #{self.cross_match_result_id} by {self.user.username}"
