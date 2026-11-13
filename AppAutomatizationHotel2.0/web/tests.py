import io
from datetime import date
from decimal import Decimal

import openpyxl
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import (
    AuditResolutionLog,
    BankStatementMovement,
    CardOperatorTransaction,
    CardType,
    CrossMatchResult,
    ReconciliationStatus,
    ErpTransaction,
    ErpTransactionChange,
    ReconciliationProcess,
    SystemConfiguration,
    TransbankTransaction,
    UserAccountStatus,
    UserAuditLog,
)
from .services import (
    calculate_clp_preview,
    calculate_usd_preview,
    commit_clp_preview_to_database,
    commit_usd_preview_to_database,
    detect_and_align_clp_files,
)
from .services.clp_reconciliation import get_clp_tolerance
from .services.column_detector import normalize_auth_code, safe_date
from .services.clp_report_exporter import generate_committed_clp_excel_bytes
from .services.erp_corrections import erp_source_key
from .services.erp_editing import CorrectionError, add_row, edit_row, soft_delete_row
from .services.preview_corrections import PreviewCorrectionError, apply_preview_correction
from .services.suggestion_actions import accept_suggestion, load_suggestion, open_proposal, reject_suggestion
from .services.exception_resolution import resolve_exception
from .services.report_exporter import generate_committed_usd_excel_bytes


# ------------------------------------------------------------------------------
# Synthetic Excel files with the same layout as the real hotel reports
# ------------------------------------------------------------------------------

def _xlsx(name: str, rows: dict) -> io.BytesIO:
    """rows = {excel_row: {column_letter: value}}"""
    wb = openpyxl.Workbook()
    ws = wb.active
    for row_number, cells in rows.items():
        for column, value in cells.items():
            ws[f"{column}{row_number}"] = value
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    stream.name = name
    return stream


def build_tbk_settlement(name: str, medium: str, sales: list) -> io.BytesIO:
    """
    Transbank 'Abonos por día'. sales = [(fecha, local, monto, tipo, cuota, valido, comision, iva, tarjeta, codigo)].
    """
    total_sales = sum(s[5] for s in sales)
    commission = sum(s[6] for s in sales)
    vat = sum(s[7] for s in sales)
    rows = {
        5: {"B": "Reporte:", "C": "Abonos por día", "E": "Periodo de consulta:", "F": "08 septiembre 2026"},
        9: {"B": "Empresa:", "C": "RUT:", "D": "Cuenta corriente:", "E": "Moneda:", "F": "Medio de pago:"},
        10: {"B": "HOTELERA SAN FRANCISCO S.A.", "C": "99.511.100-4", "D": "BANCO DE CHILE 6211002", "E": "Pesos", "F": medium},
        17: {"B": "Total ventas (+)", "C": total_sales},
        21: {"B": "Comisión Transbank (-)", "C": commission},
        22: {"B": "IVA de comisión Transbank (-)", "C": vat},
        26: {"B": "Total abono", "C": total_sales - commission - vat},
        29: {"B": "Fecha de venta", "C": "Tipo de movimiento", "D": "Código de comercio", "E": "Nombre local", "F": "Medio de pago", "G": "Ventas", "Y": "Identificador"},
        30: {
            "G": "Monto original de venta (+)", "I": "Tipo de venta/cuota", "J": "N° de cuota",
            "K": "Monto venta válido para abono (+)", "L": "Comisión Transbank (-)", "M": "IVA Comisión Transbank (-)",
            "N": "Total abono (=)", "Y": "N° de tarjeta", "Z": "Código de autorización de venta",
        },
    }
    for offset, (fecha, local, monto, tipo, cuota, valido, comision, iva, tarjeta, codigo) in enumerate(sales):
        rows[31 + offset] = {
            "B": fecha, "C": "Venta", "D": "27941095", "E": f"{local}, AVENIDA LIBERTADOR BERNARDO O'HIGGINS 816",
            "F": medium, "G": monto, "I": tipo, "J": cuota, "K": valido, "L": comision, "M": iva,
            "N": valido - comision - iva, "Y": tarjeta, "Z": codigo,
        }
    return _xlsx(name, rows)


def build_bordero(payments: list) -> io.BytesIO:
    """Bordero rows: (hab, cuenta, cod, descripcion, factura, valor, documento, fecha, hora, usuario, huesped)."""
    header = {"A": "Hab", "B": "Tipo Hab", "C": "Reserva", "D": "Cuenta", "E": "Cod. Deb.", "F": "Descripción",
              "H": "Factura", "I": "Valor", "J": "Documento", "K": "Fecha", "L": "Hora", "M": "Usuario", "N": "Designación"}
    rows = {
        1: {"A": "HOTELERA SAN FRANCISCO S.A"},
        2: {"A": "Cargos de Débitos y Créditos"},
        4: {"A": "Periodo de:", "B": "04/09/2026", "C": "hasta", "D": "07/09/2026"},
        5: header,
        6: {"A": 1002, "D": 1785942, "E": "CNBQ", "F": "1/2 Pension Alimentos", "I": 20900, "J": "UH: 1002", "K": "07/09/2026"},
        7: {"A": "Total VHF:", "B": 20900},
    }
    for offset, (hab, cuenta, cod, desc, factura, valor, documento, fecha, hora, usuario, huesped) in enumerate(payments):
        rows[8 + offset] = {"A": hab, "D": cuenta, "E": cod, "F": desc, "G": factura, "I": valor,
                            "J": documento, "K": fecha, "L": hora, "M": usuario, "N": huesped}
    return _xlsx("BORDERO 04-09 A 07-09.xlsx", rows)


def build_cartola(deposits: list) -> io.BytesIO:
    rows = {
        11: {"B": "Cuenta N°:", "C": "00-000-62110-02"},
        12: {"B": "Moneda:", "C": "Pesos Chilenos (CLP)"},
        15: {"B": "Saldo Disponible", "C": "Saldo Contable"},
        22: {"B": "Fecha", "D": "Descripción", "F": "Canal o Sucursal", "G": "Nro. Docto.", "H": "Cargos (CLP)", "I": "Abonos (CLP)", "J": "Saldo (CLP)"},
    }
    for offset, (fecha, descripcion, abono) in enumerate(deposits):
        rows[23 + offset] = {"B": fecha, "D": descripcion, "F": "Oficina Central", "I": abono, "J": 1000000}
    return _xlsx("cartola.xlsx", rows)


CREDIT_SALES = [
    # MATCHED: Mastercard credit 405544 (the study guide example)
    ("04 septiembre 2026", "HOTEL PLAZA SAN FRANCISCO", 187172, "Venta $", "S/C", 187172, 4997, 949, "5525 61** **** 5410", "405544"),
    # OUT_OF_PERIOD: sale before the Bordero period
    ("03 septiembre 2026", "RESTAURANT BRISTOL", 8690, "Venta $", "S/C", 8690, 174, 33, "5532 53** **** 4291", "070981"),
    # OUT_OF_PERIOD: installment 3/3 of a July sale (compared with the original amount)
    ("04 julio 2026", "HOTEL PLAZA SAN FRANCISCO", 1354387, "C3C", "3/3", 451463, 11377, 2162, "4260 40** **** 7444", "626135"),
]
DEBIT_SALES = [
    # MATCHED with Excel-dropped leading zeros (TBK 003098 -> Bordero 3098) and a 1 peso difference
    ("07 septiembre 2026", "RESTAURANT BRISTOL", 57420, "Normal", "S/C", 57420, 568, 108, "4547 93** **** 1111", "003098"),
    # AMOUNT_MISMATCH: 1.504 above the ±1.000 maximum
    ("05 septiembre 2026", "RESTAURANT BRISTOL", 115204, "Normal", "S/C", 115204, 1140, 217, "4547 93** **** 2222", "007626"),
    # NOT_FOUND_IN_ERP with typo suggestion: cashier typed 0FOFUAZ
    ("06 septiembre 2026", "HOTEL PLAZA SAN FRANCISCO", 106999, "Normal", "S/C", 106999, 856, 163, "4547 93** **** 8430", "F0FUAZ"),
    # MATCHED within tolerance (704 pesos)
    ("05 septiembre 2026", "HOTEL PLAZA SAN FRANCISCO", 115204, "Normal", "S/C", 115204, 1140, 217, "4547 93** **** 3333", "112233"),
]
PREPAID_SALES = [
    # MATCHED by netting a reversal: -11.000, +11.000, -9.800
    ("05 septiembre 2026", "RESTAURANT BRISTOL", 9800, "-", "S/C", 9800, 97, 18, "5303 12** **** 4444", "823431"),
]
BORDERO_PAYMENTS = [
    (822, 1786484, "MC$", "Master Card $", 53895, -187172, 405544, "04/09/2026", "03:11:09", "CDAZA", "RODRIGO ISLA"),
    (None, 1786798, "RCO", "Red Compra", 259900, -57421, 3098, "07/09/2026", "13:00:00", "BFREDERIC", "PARC"),
    (None, 1786853, "RCO", "Red Compra", 259924, -113700, 7626, "05/09/2026", "15:38:37", "SCORREA", "PARC"),
    (825, 1787015, "RCO", "Red Compra", 259977, -106999, "0FOFUAZ", "06/09/2026", "19:36:48", "MSALAS", "KATHARINA WINCO"),
    (None, 1786900, "RCO", "Red Compra", 259930, -114500, 112233, "05/09/2026", "16:00:00", "SCORREA", "PARC"),
    (None, 1786794, "RCO", "Red Compra", None, 11000, 823431, "05/09/2026", "09:26:26", "SCORREA", "PARC"),
    (None, 1786794, "RCO", "Red Compra", None, -11000, 823431, "05/09/2026", "09:25:33", "SCORREA", "PARC"),
    (None, 1786793, "RCO", "Red Compra", 259889, -9800, 823431, "05/09/2026", "09:23:22", "SCORREA", "PARC"),
    # A Phase 1 dollar payment and cash must be ignored
    (602, 1715625, "VUS$", "Visa US$", 99965, -107984, 112194, "05/09/2026", "19:28:02", "TMENDOZA", "MARK WILLIS"),
    (None, 1786000, "E$", "Efectivo $", None, -50000, None, "05/09/2026", "10:00:00", "SCORREA", "PARC"),
]


def _deposit(sales):
    return sum(s[5] - s[6] - s[7] for s in sales)


def build_clp_files() -> dict:
    return {
        "erp_file": build_bordero(BORDERO_PAYMENTS),
        "bank_statement_file": build_cartola([
            ("08/09/2026", "Pago: Abonos Realizados Por Transbank Al Comercio Afilia 0966893109", _deposit(PREPAID_SALES)),
            ("08/09/2026", "Pago: Abonos Realizados Por Transbank Al Comercio Afilia 0966893109", _deposit(DEBIT_SALES)),
            ("08/09/2026", "Pago: Proveedores 0856339009", 580075),
            ("08/09/2026", "Pago: Abono Comer. Tc Tbk 0966893109", _deposit(CREDIT_SALES)),
        ]),
        "transbank_credit_file": build_tbk_settlement("CREDITOS TBK 08-09.xlsx", "Crédito", CREDIT_SALES),
        "transbank_debit_file": build_tbk_settlement("DEBITOS TBK 08-09.xlsx", "Débito", DEBIT_SALES),
        "transbank_prepaid_file": build_tbk_settlement("PREPAGO TBK 08-09.xlsx", "Prepago", PREPAID_SALES),
    }


def _by_code(preview, bucket):
    return {v["authorization_code"]: v for v in preview[bucket]}


# ------------------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------------------

class ClpHelpersTests(TestCase):
    def test_normalize_auth_code_drops_leading_zeros(self):
        self.assertEqual(normalize_auth_code("003098"), normalize_auth_code(3098))
        self.assertEqual(normalize_auth_code("0WBNIJW"), "WBNIJW")
        self.assertEqual(normalize_auth_code(405544.0), "405544")

    def test_safe_date_reads_spanish_transbank_dates(self):
        self.assertEqual(safe_date("04 septiembre 2026"), date(2026, 9, 4))
        self.assertEqual(safe_date("04/09/2026"), date(2026, 9, 4))

    def test_tolerance_comes_from_system_configuration(self):
        config = SystemConfiguration.objects.first()
        self.assertEqual(get_clp_tolerance(), Decimal("1000"))
        config.clp_rounding_tolerance = Decimal("700")
        config.save()
        self.assertEqual(get_clp_tolerance(), Decimal("700"))


class ClpPreviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.preview = calculate_clp_preview(**build_clp_files())

    def test_bank_deposits_match_each_settlement(self):
        validations = {v["payment_modality"]: v for v in self.preview["bank_validations"]}
        self.assertTrue(all(v["is_matched"] for v in validations.values()))
        self.assertIn("TC TBK", validations["CREDIT"]["bank_description"].upper())
        self.assertEqual(validations["DEBIT"]["bank_deposit_amount"], _deposit(DEBIT_SALES))
        self.assertEqual(validations["PREPAID"]["bank_deposit_amount"], _deposit(PREPAID_SALES))

    def test_traced_voucher_405544_matches_bordero_row(self):
        voucher = _by_code(self.preview, "matched_vouchers")["405544"]
        self.assertEqual(voucher["erp_payment_code"], "MC$")
        self.assertEqual(voucher["difference"], 0)
        self.assertEqual(voucher["account_code"], "1786484")
        self.assertEqual(voucher["erp_authorization_code"], "405544")
        self.assertNotIn("guest_name", voucher)
        self.assertNotIn("room_number", voucher)
        self.assertEqual(voucher["card_code"], "MC")

    def test_leading_zeros_reversals_and_tolerance(self):
        matched = _by_code(self.preview, "matched_vouchers")
        self.assertEqual(matched["003098"]["difference"], -1)
        self.assertEqual(len(matched["823431"]["erp_indices"]), 1)
        self.assertEqual(matched["823431"]["erp_amount"], 9800)
        self.assertEqual(matched["112233"]["difference"], 704)

    def test_amount_above_tolerance_is_a_discrepancy(self):
        voucher = _by_code(self.preview, "discrepancy_vouchers")["007626"]
        self.assertEqual(voucher["system_result"], "AMOUNT_MISMATCH")
        self.assertEqual(voucher["difference"], 1504)

    def test_out_of_period_and_later_installments(self):
        out_of_period = _by_code(self.preview, "out_of_period_vouchers")
        self.assertEqual(set(out_of_period), {"070981", "626135"})
        self.assertIn("Cuota 3/3", out_of_period["626135"]["observation"])

    def test_orphan_typo_gets_high_confidence_suggestion(self):
        voucher = _by_code(self.preview, "orphan_vouchers")["F0FUAZ"]
        self.assertEqual(voucher["suggestion"]["confidence"], "ALTA")
        self.assertEqual(voucher["suggestion"]["erp_row"]["authorization_code"], "0FOFUAZ")
        self.assertEqual(voucher["error_type"], "Error de tipeo")

    def test_only_peso_card_payments_are_read_from_bordero(self):
        codes = {row["payment_code"] for row in self.preview["erp_transactions"]}
        self.assertEqual(codes, {"MC$", "RCO"})


class ClpPersistenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("auditor", password="x")

    def test_commit_export_and_reupload_without_duplicates(self):
        process = commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))
        self.assertEqual(process.process_currency, "CLP")
        self.assertEqual(process.tolerance_applied, Decimal("1000"))
        self.assertEqual(process.bank_validations.filter(is_matched=True).count(), 3)
        self.assertEqual(process.cross_match_results.count(), len(CREDIT_SALES + DEBIT_SALES + PREPAID_SALES))
        counts = (TransbankTransaction.objects.count(), ErpTransaction.objects.count(),
                  CrossMatchResult.objects.count(), BankStatementMovement.objects.count())

        orphan = process.cross_match_results.get(authorization_code="F0FUAZ")
        self.assertEqual(orphan.account_code, "1787015")
        self.assertEqual(orphan.document_number, "0FOFUAZ")
        self.assertEqual(orphan.display_account_code, "1787015")
        self.assertEqual(orphan.display_erp_document, "0FOFUAZ")

        matched_405544 = process.cross_match_results.get(authorization_code="405544")
        self.assertEqual(matched_405544.account_code, "1786484")
        self.assertEqual(matched_405544.document_number, "405544")

        workbook = openpyxl.load_workbook(generate_committed_clp_excel_bytes(process.id))
        self.assertEqual(workbook.sheetnames, ["Resumen Pesos", "Vouchers Cuadrados", "Reporte de Excepciones"])
        matched_headers = [c.value for c in workbook["Vouchers Cuadrados"][1]]
        self.assertIn("Cuenta", matched_headers)
        self.assertIn("Documento Borderó", matched_headers)
        self.assertNotIn("Huésped", matched_headers)
        self.assertNotIn("Hab.", matched_headers)
        exc_headers = [c.value for c in workbook["Reporte de Excepciones"][1]]
        self.assertIn("Cuenta", exc_headers)
        self.assertIn("Documento Borderó", exc_headers)
        self.assertIn("Cajero", exc_headers)
        self.assertNotIn("Huésped / Hab. / Cajero", exc_headers)
        first_exception = [c.value for c in workbook["Reporte de Excepciones"][2]]
        self.assertEqual(first_exception[0], "Error de tipeo")

        again = commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))
        self.assertEqual(again.id, process.id)
        self.assertEqual(ReconciliationProcess.objects.count(), 1)
        self.assertEqual(
            counts,
            (TransbankTransaction.objects.count(), ErpTransaction.objects.count(),
             CrossMatchResult.objects.count(), BankStatementMovement.objects.count()),
        )


class ToleranceSettingsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", password="x")
        self.auditor = User.objects.create_user("auditor", password="x")

    def _post(self, **overrides):
        data = {"usd_rounding_tolerance": "0.50", "clp_rounding_tolerance": "700", **overrides}
        return self.client.post(reverse("settings"), data)

    def test_standard_user_can_access_and_update_tolerances(self):
        self.client.force_login(self.auditor)
        self.assertEqual(self.client.get(reverse("settings")).status_code, 200)
        self.assertRedirects(self._post(clp_rounding_tolerance="850"), reverse("settings"))
        config = SystemConfiguration.get_solo()
        self.assertEqual(config.clp_rounding_tolerance, Decimal("850"))
        self.assertEqual(config.updated_by, self.auditor)

    def test_superuser_updates_tolerances(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("settings")).status_code, 200)
        self.assertRedirects(self._post(usd_rounding_tolerance="1.25"), reverse("settings"))
        config = SystemConfiguration.get_solo()
        self.assertEqual(config.usd_rounding_tolerance, Decimal("1.25"))
        self.assertEqual(config.clp_rounding_tolerance, Decimal("700"))
        self.assertEqual(config.updated_by, self.admin)
        self.assertEqual(SystemConfiguration.objects.count(), 1)

    def test_rejects_out_of_range_values(self):
        self.client.force_login(self.admin)
        for overrides in (
            {"usd_rounding_tolerance": "5.01"},
            {"clp_rounding_tolerance": "5001"},
            {"clp_rounding_tolerance": "-1"},
        ):
            self.assertEqual(self._post(**overrides).status_code, 200)
        config = SystemConfiguration.get_solo()
        self.assertEqual(config.usd_rounding_tolerance, Decimal("0.50"))
        self.assertEqual(config.clp_rounding_tolerance, Decimal("1000"))


class ClpViewsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("auditor", password="x")
        self.client.force_login(self.user)

    def test_detect_files_aligns_the_five_roles(self):
        files = build_clp_files()
        report = detect_and_align_clp_files(list(files.values()))
        self.assertTrue(report["all_matched"])
        aligned = {r["role_key"]: r for r in report["aligned_roles"]}
        self.assertEqual(aligned["erp"]["file_name"], "BORDERO 04-09 A 07-09.xlsx")
        self.assertEqual(aligned["bank"]["file_name"], "cartola.xlsx")
        tbk_names = {aligned["tbk"]["file_name"], *(f["file_name"] for f in aligned["tbk"]["extra_files"])}
        self.assertEqual(
            tbk_names,
            {"CREDITOS TBK 08-09.xlsx", "DEBITOS TBK 08-09.xlsx", "PREPAGO TBK 08-09.xlsx"},
        )

    def test_upload_preview_commit_and_export(self):
        files = build_clp_files()
        payload = {
            "clp_erp_file": files["erp_file"],
            "clp_bank_file": files["bank_statement_file"],
            "clp_tbk_credit_file": files["transbank_credit_file"],
            "clp_tbk_debit_file": files["transbank_debit_file"],
            "clp_tbk_prepaid_file": files["transbank_prepaid_file"],
        }
        response = self.client.post(reverse("clp_upload"), payload)
        self.assertRedirects(response, reverse("clp_preview"))

        response = self.client.get(reverse("clp_preview"))
        self.assertContains(response, "F0FUAZ")
        self.assertContains(response, "Ubicadas Exactas")
        self.assertContains(response, "Tolerancia ±$1.000 CLP")
        self.assertContains(response, "Cuenta / Documento")
        self.assertContains(response, "Cta: 1786484")
        self.assertContains(response, "Doc: 405544")
        self.assertNotContains(response, "RODRIGO ISLA")
        self.assertNotContains(response, "Huésped")
        self.assertNotContains(response, "Habitación")
        self.assertNotContains(response, "Borderó histórico (")

        response = self.client.post(reverse("clp_commit"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(self.client.get(data["export_url"]).status_code, 200)

    def test_usd_upload_and_preview_render(self):
        self.assertEqual(self.client.get(reverse("usd_upload")).status_code, 200)

        # Verify USD preview renders the USD tolerance badge when session is populated
        session = self.client.session
        session["usd_reconciliation_preview"] = {
            "accounting_period": "Cierre Dólares — 08/09/2026",
            "tolerance": 0.50,
            "bank_validation": {},
            "counts": {"matched": 0, "discrepancies": 0, "orphans": 0, "total": 0},
            "suggestion_counts": {},
        }
        session.save()
        response = self.client.get(reverse("usd_preview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tolerancia ±US$ 0,50 USD")


class DashboardViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="contador", password="clave-de-prueba-123")

    def test_requires_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, f"{reverse('login')}?next=/")

    def test_empty_state_without_processes(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["usd"]["has_data"])
        self.assertFalse(response.context["clp"]["has_data"])
        self.assertContains(response, "Todavía no hay cierres en dólares")
        self.assertContains(response, "Todavía no hay cierres en pesos")

    def _clp_closing(self):
        """A saved CLP closing shaped like the ones commit_clp_preview_to_database stores."""
        from .models import BankDepositValidation, CardType, ReconciliationStatus, ReconciliationSummary

        visa, _ = CardType.objects.get_or_create(code="VI", defaults={"name": "Visa"})
        pending, _ = ReconciliationStatus.objects.get_or_create(code="PENDING", defaults={"display_name": "Pendiente"})
        accepted, _ = ReconciliationStatus.objects.get_or_create(code="ACCEPTED_ROUNDING", defaults={"display_name": "Aceptado"})
        process = ReconciliationProcess.objects.create(
            user=self.user, process_currency="CLP", accounting_period="Cierre Pesos — Abono 08/09/2026",
        )
        rows = [
            # (modality, result, amount, status detail)
            ("CREDIT", "MATCHED", 10000, "Crédito · Cuadrado · Sin acción"),
            ("DEBIT", "MATCHED", 20000, "Débito · Cuadrado · Sin acción"),
            ("DEBIT", "NOT_FOUND_IN_ERP", 250000, "Débito · Falta en ERP · Buscar la venta con el cajero y registrarla en el ERP"),
            ("CREDIT", "OUT_OF_PERIOD", 5000, "Crédito · Fuera de período · Buscar en el Borderó del período de la venta"),
        ]
        for i, (modality, result, amount, detail) in enumerate(rows):
            tbk = TransbankTransaction.objects.create(
                process=process, card_type=visa, payment_modality=modality,
                authorization_code=f"A{i}", original_sale_amount=amount,
            )
            CrossMatchResult.objects.create(
                process=process, card_type=visa, transbank_transaction=tbk, authorization_code=f"A{i}",
                transbank_amount=amount, system_result=result, system_status_detail=detail,
                management_status=accepted if result == "MATCHED" else pending,
            )
        for modality, matched in (("CLP_CREDIT", True), ("CLP_DEBIT", True), ("CLP_PREPAID", False)):
            BankDepositValidation.objects.create(
                process=process, payment_modality=modality, transbank_calculated_deposit=1000, is_matched=matched,
            )
        ReconciliationSummary.objects.create(
            process=process, card_type=visa, payment_modality="CLP_DEBIT", currency_table="CLP", total_commissions=350,
        )
        return process

    def test_clp_panel_figures_come_from_saved_closing(self):
        self._clp_closing()
        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard"), {"moneda": "clp"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_currency"], "clp")
        clp = response.context["clp"]
        self.assertTrue(clp["has_data"])
        self.assertEqual(clp["last_process"].match_rate, 50.0)
        self.assertEqual(clp["last_counts"]["orphans"], 1)
        self.assertEqual(clp["last_counts"]["out_of_period"], 1)
        self.assertEqual(clp["pending_count"], 2)
        self.assertEqual(clp["pending_amount"], 255000)
        self.assertEqual(clp["out_of_period_amount"], 5000)
        self.assertEqual(clp["last_process"].banks_matched, 2)
        errors = {g["label"]: g for g in clp["pending_by_error"]}
        self.assertEqual(errors["Falta en ERP"]["count"], 1)
        self.assertEqual(errors["Falta en ERP"]["action"], "Buscar la venta con el cajero y registrarla en el ERP")
        modalities = {m["code"]: m for m in clp["modalities"]}
        self.assertEqual((modalities["DEBIT"]["vouchers"], modalities["DEBIT"]["matched"]), (2, 1))
        self.assertEqual(modalities["DEBIT"]["commissions"], 350)
        self.assertEqual(modalities["PREPAID"]["vouchers"], 0)
        self.assertFalse(response.context["usd"]["has_data"])

    def test_defaults_to_clp_panel_when_only_pesos_exist(self):
        self._clp_closing()
        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["active_currency"], "clp")


class UsdPreviewViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="contador", password="clave-de-prueba-123")
        self.client.force_login(self.user)

    def _store_preview(self, preview):
        session = self.client.session
        session["usd_reconciliation_preview"] = preview
        session.save()

    def test_redirects_to_upload_without_preview(self):
        response = self.client.get(reverse("usd_preview"))
        self.assertRedirects(response, reverse("usd_upload"))

    def test_merges_usd_and_clp_rows_per_franchise(self):
        voucher = {
            "authorization_code": "A10001", "erp_authorization_code": "A10001",
            "account_code": "9000001", "document_number": "9000001",
            "operator_document_number": "9000001", "card_name": "Visa",
            "cashier_username": "CAJERO1",
            "gross_amount_usd": 100.0, "operator_amount_usd": 100.11, "difference_usd": -0.11,
            "erp_amount_clp": 95000.0, "observation": "Coincide con SALDO 95000",
        }
        self._store_preview({
            "accounting_period": "Cierre Dólares — Abono de prueba",
            "bank_validation": {"calculated_deposit": 100.0, "bank_deposit_amount": 100.0, "is_matched": True},
            "usd_table": [
                {"card_code": "VI", "card_name": "Visa US$", "erp_sales": 100.0, "tbk_sales": 100.11, "difference": 0.11},
                {"card_code": "DI", "card_name": "Diners Club US$", "erp_sales": 0.0, "tbk_sales": 0.0, "difference": 0.0},
            ],
            "clp_table": [
                {"card_code": "VI", "card_name": "Visa (CLP)", "erp_sales": 95000.0, "tbk_sales": 95000.0, "difference": 0.0},
                {"card_code": "DI", "card_name": "Diners Club (CLP)", "erp_sales": 0.0, "tbk_sales": 0.0, "difference": 0.0},
            ],
            "usd_totals": {"erp": 100.0, "tbk": 100.11, "diff": 0.11},
            "clp_totals": {"erp": 95000.0, "tbk": 95000.0, "diff": 0.0},
            "matched_vouchers": [voucher],
            "discrepancy_vouchers": [],
            "orphan_vouchers": [],
            "counts": {"matched": 1, "discrepancies": 0, "orphans": 0, "total": 1},
        })

        response = self.client.get(reverse("usd_preview"))

        self.assertEqual(response.status_code, 200)
        rows = {row["card_code"]: row for row in response.context["franchise_rows"]}
        self.assertEqual(rows["VI"]["card_name"], "Visa")
        self.assertEqual(rows["VI"]["clp"]["erp_sales"], 95000.0)
        self.assertTrue(rows["VI"]["has_activity"])
        self.assertFalse(rows["DI"]["has_activity"])
        self.assertEqual(response.context["match_rate"], 100.0)
        self.assertContains(response, "A10001")



class BruteForceProtectionTests(TestCase):
    """django-axes locks the username+IP pair after 5 failed logins (both /login/ and the admin)."""

    def setUp(self):
        self.user = User.objects.create_superuser("contadora", password="clave-correcta-123")

    def _login(self, url, password, ip="10.0.0.1"):
        return self.client.post(url, {"username": "contadora", "password": password}, HTTP_X_REAL_IP=ip)

    def test_correct_password_logs_in(self):
        self.assertRedirects(self._login(reverse("login"), "clave-correcta-123"), reverse("dashboard"))

    def test_lockout_after_failed_attempts_blocks_even_the_correct_password(self):
        for _ in range(4):
            self.assertEqual(self._login(reverse("login"), "mala").status_code, 200)
        # The 5th failure reaches AXES_FAILURE_LIMIT and is already answered with the lockout page
        self.assertEqual(self._login(reverse("login"), "mala").status_code, 429)

        response = self._login(reverse("login"), "clave-correcta-123")

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Acceso bloqueado", status_code=429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_failed_login_shows_remaining_attempts(self):
        self.assertContains(self._login(reverse("login"), "mala"), "quedan <strong>4 intentos</strong>", html=False)
        self._login(reverse("login"), "mala")
        self._login(reverse("login"), "mala")
        self.assertContains(self._login(reverse("login"), "mala"), "último intento")

    def test_lockout_is_per_ip_so_attacker_cannot_block_real_user(self):
        for _ in range(5):
            self._login(reverse("login"), "mala", ip="203.0.113.9")

        response = self._login(reverse("login"), "clave-correcta-123", ip="10.0.0.1")

        self.assertRedirects(response, reverse("dashboard"))

    def test_admin_login_is_also_protected(self):
        admin_login = reverse("admin:login")
        for _ in range(5):
            self._login(admin_login, "mala")

        self.assertEqual(self._login(admin_login, "clave-correcta-123").status_code, 429)


class ExceptionInboxTests(TestCase):
    """The "Pendientes" inbox: the accountant resolves, reopens, filters and exports exceptions."""

    def setUp(self):
        self.user = User.objects.create_user(username="contador", password="clave-de-prueba-123")
        self.process = DashboardViewTests._clp_closing(self)
        self.orphan = self.process.cross_match_results.get(system_result="NOT_FOUND_IN_ERP")
        self.out_of_period = self.process.cross_match_results.get(system_result="OUT_OF_PERIOD")
        self.out_of_period.cashier_username = "jperez"
        self.out_of_period.save()
        self.client.force_login(self.user)

    def _resolve(self, match, status, comment=""):
        return self.client.post(
            reverse("exception_resolve", args=[match.id]),
            {"status": status, "comment": comment, "return_query": "estado=pendientes"},
        )

    def test_inbox_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("exceptions"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_inbox_lists_pending_exceptions_only(self):
        response = self.client.get(reverse("exceptions"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual({r.id for r in response.context["rows"]}, {self.orphan.id, self.out_of_period.id})
        self.assertEqual(response.context["sidebar_pending_count"], 2)

    def test_inbox_shows_one_currency_per_tab(self):
        # Only pesos have pending cases, so the inbox opens on the Pesos tab
        response = self.client.get(reverse("exceptions"))
        self.assertEqual(response.context["currency"], "clp")
        self.assertEqual(response.context["pending_counts"], {"usd": 0, "clp": 2})
        self.assertEqual(response.context["totals"]["amount"], 255000)

        usd = self.client.get(reverse("exceptions"), {"moneda": "usd"})
        self.assertEqual(usd.context["currency"], "usd")
        self.assertEqual(list(usd.context["rows"]), [])
        self.assertNotIn("OUT_OF_PERIOD", dict(usd.context["type_choices"]))

    def test_resolving_logs_it_and_lowers_the_dashboard_counter(self):
        response = self._resolve(self.orphan, "RESOLVED_ERP_FIXED", "Registrada en ERP con folio 8841")
        self.assertRedirects(response, f"{reverse('exceptions')}?estado=pendientes")

        self.orphan.refresh_from_db()
        self.assertEqual(self.orphan.management_status.code, "RESOLVED_ERP_FIXED")
        self.assertEqual(self.orphan.resolved_by, self.user)
        self.assertIsNotNone(self.orphan.resolution_date)
        log = self.orphan.audit_logs.get()
        self.assertEqual((log.previous_status.code, log.new_status.code), ("PENDING", "RESOLVED_ERP_FIXED"))
        self.assertEqual(log.comment, "Registrada en ERP con folio 8841")

        dashboard = self.client.get(reverse("dashboard"), {"moneda": "clp"})
        self.assertEqual(dashboard.context["clp"]["pending_count"], 1)

    def test_accepting_a_difference_needs_a_comment(self):
        self._resolve(self.orphan, "RESOLVED_ACCEPTED")
        self.orphan.refresh_from_db()
        self.assertEqual(self.orphan.management_status.code, "PENDING")
        self.assertFalse(self.orphan.audit_logs.exists())

    def test_reopen_needs_a_comment_and_returns_to_pending(self):
        self._resolve(self.orphan, "RESOLVED_CASHIER")
        url = reverse("exception_reopen", args=[self.orphan.id])

        self.client.post(url, {"comment": ""})
        self.orphan.refresh_from_db()
        self.assertEqual(self.orphan.management_status.code, "RESOLVED_CASHIER")

        self.client.post(url, {"comment": "Recepción no lo corrigió"})
        self.orphan.refresh_from_db()
        self.assertEqual(self.orphan.management_status.code, "PENDING")
        self.assertIsNone(self.orphan.resolved_by)
        self.assertEqual(self.orphan.audit_logs.count(), 2)

    def test_filters_by_type_cashier_and_state(self):
        def ids(**params):
            return {r.id for r in self.client.get(reverse("exceptions"), params).context["rows"]}

        self.assertEqual(ids(tipo="OUT_OF_PERIOD"), {self.out_of_period.id})
        self.assertEqual(ids(cajero="jperez"), {self.out_of_period.id})
        self.assertEqual(ids(cajero="__none__"), {self.orphan.id})
        self.assertEqual(ids(moneda="usd"), set())
        self._resolve(self.orphan, "RESOLVED_ERP_FIXED")
        self.assertEqual(ids(), {self.out_of_period.id})
        self.assertEqual(ids(estado="resueltas"), {self.orphan.id})
        self.assertEqual(ids(estado="todas"), {self.orphan.id, self.out_of_period.id})

    def test_export_contains_the_filtered_rows(self):
        response = self.client.get(reverse("exceptions_export"), {"cajero": "jperez"})
        self.assertEqual(response.status_code, 200)
        sheet = openpyxl.load_workbook(io.BytesIO(response.content))["Excepciones"]
        self.assertEqual(sheet.max_row, 2)
        headers = [c.value for c in sheet[1]]
        self.assertIn("Cuenta", headers)
        self.assertIn("Documento", headers)
        self.assertNotIn("Huésped", headers)
        self.assertNotIn("Habitación", headers)
        self.assertEqual(sheet["C2"].value, self.out_of_period.authorization_code)
        self.assertEqual(sheet["I2"].value, "jperez")


class ExceptionResolutionSurvivesReuploadTests(TestCase):
    def test_reupload_keeps_a_manual_resolution(self):
        user = User.objects.create_user("auditor", password="x")
        process = commit_clp_preview_to_database(user, calculate_clp_preview(**build_clp_files()))
        exception = process.cross_match_results.exclude(system_result="MATCHED").first()
        resolve_exception(exception, "RESOLVED_ADJUSTMENT", "Ajuste 2026-114", user)

        commit_clp_preview_to_database(user, calculate_clp_preview(**build_clp_files()))
        exception.refresh_from_db()
        self.assertEqual(exception.management_status.code, "RESOLVED_ADJUSTMENT")


def _build_usd_tbk_file(sales: list) -> io.BytesIO:
    """sales = [(fecha, card_type, masked, monto_usd, codigo)]"""
    total_usd = sum(Decimal(str(s[3])) for s in sales)
    rows = {
        2: {"A": "Abono calculado", "B": float(total_usd), "C": "Banco de Chile", "D": "Fecha abono", "E": "08/09/2026"},
        3: {"A": "Numero cuenta", "B": "00-000-62110-07"},
        5: {
            "A": "Fecha de venta", "B": "Código de comercio", "C": "Tipo de tarjeta",
            "D": "Identificador", "E": "Tipo de cuota", "F": "Monto original de venta",
            "G": "Código de autorización de venta",
        },
    }
    for idx, (fecha, card_type, masked, monto_usd, codigo) in enumerate(sales, start=6):
        rows[idx] = {
            "A": fecha, "B": "27941095", "C": card_type, "D": masked,
            "E": "Venta Normal", "F": monto_usd, "G": codigo,
        }
    return _xlsx("ABONO TBK USD.xlsx", rows)


def _build_usd_bordero_file(payments: list) -> io.BytesIO:
    """payments = [(hab, cuenta, cod, descripcion, factura, valor_clp, documento, fecha, hora, usuario, huesped)]"""
    rows = {
        1: {"A": "HOTELERA SAN FRANCISCO S.A"},
        2: {"A": "Cargos de Débitos y Créditos"},
        4: {"A": "Periodo de:", "B": "04/09/2026", "C": "hasta", "D": "07/09/2026"},
        5: {
            "A": "Hab", "B": "Tipo Hab", "C": "Reserva", "D": "Cuenta", "E": "Cod. Deb.",
            "F": "Descripción", "H": "Factura", "I": "Valor", "J": "Documento",
            "K": "Fecha", "L": "Hora", "M": "Usuario", "N": "Designación",
        },
    }
    for offset, (hab, cuenta, cod, desc, factura, valor, documento, fecha, hora, usuario, huesped) in enumerate(payments):
        rows[6 + offset] = {
            "A": hab, "D": cuenta, "E": cod, "F": desc, "G": factura,
            "I": valor, "J": documento, "K": fecha, "L": hora, "M": usuario, "N": huesped,
        }
    return _xlsx("TARJETAS ERP USD.xlsx", rows)


def _build_operator_file(name: str, items: list) -> io.BytesIO:
    """items = [(documento, parcela, usd, saldo, saldo_corregido)]"""
    rows = {
        1: {"C": "Documento", "D": "Parcela", "M": "Otra Moneda", "N": "Saldo", "O": "Saldo Corregido"},
    }
    for idx, (doc, parcela, usd, saldo, saldo_corr) in enumerate(items, start=2):
        rows[idx] = {"C": doc, "D": parcela, "M": usd, "N": saldo, "O": saldo_corr}
    return _xlsx(name, rows)


def _build_usd_files() -> dict:
    sales = [
        ("05/09/2026", "VI", "4260 40** **** 1111", 120.00, "112194"),
        ("05/09/2026", "MC", "5525 61** **** 2222", 85.00, "334455"),
        ("06/09/2026", "VI", "4260 40** **** 3333", 50.00, "556677"),
        ("06/09/2026", "VI", "4260 40** **** 4444", 40.00, "888999"),
    ]
    bordero = [
        (602, 1715625, "VUS$", "Visa US$", 99965, -114000, 112194, "05/09/2026", "19:28:02", "TMENDOZA", "JOHN DOE"),
        (710, 1715800, "MUS$", "Master Card US$", 99970, -80750, 334455, "05/09/2026", "20:00:00", "CDAZA", "JANE SMITH"),
        (815, 1715900, "VUS$", "Visa US$", 99980, -47500, 556677, "06/09/2026", "11:15:00", "SCORREA", "MARIO RUIZ"),
    ]
    return {
        "erp_file": _build_usd_bordero_file(bordero),
        "transbank_file": _build_usd_tbk_file(sales),
        "bank_statement_file": build_cartola([("08/09/2026", "Pago: Abono Divisas Transbank", 295.00)]),
        "amex_file": _build_operator_file("AX.xlsx", []),
        "diners_file": _build_operator_file("DI.xlsx", []),
        "visa_file": _build_operator_file("VI.xlsx", [
            (1715625, 1, 120.00, 114000, 114000),
            (1715900, 1, 0.00, 0, 47500),
        ]),
        "mastercard_file": _build_operator_file("MC.xlsx", [
            (999888, 1, 85.00, 80750, 80750),
        ]),
    }


class UsdAccountDocumentAndReportsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("contador_usd", password="clave-de-prueba-123")
        self.client.force_login(self.user)

    def test_usd_preview_commit_and_excel_show_account_and_document_without_guest_or_room(self):
        preview = calculate_usd_preview(**_build_usd_files())

        matched = {v["authorization_code"]: v for v in preview["matched_vouchers"]}
        discrepancies = {v["authorization_code"]: v for v in preview["discrepancy_vouchers"]}
        orphans = {v["authorization_code"]: v for v in preview["orphan_vouchers"]}

        self.assertEqual(matched["112194"]["account_code"], "1715625")
        self.assertEqual(matched["112194"]["erp_authorization_code"], "112194")
        self.assertEqual(matched["112194"]["operator_document_number"], "1715625")

        # Case 1A: Pos. Clientes folio mismatch -> empty operator_document_number & Level 2 suggestion
        self.assertEqual(discrepancies["334455"]["account_code"], "1715800")
        self.assertEqual(discrepancies["334455"]["erp_authorization_code"], "334455")
        self.assertEqual(discrepancies["334455"]["operator_document_number"], "")
        self.assertIn("999888", discrepancies["334455"]["suggestion"]["headline"])

        # Case 1B: Linked by Cuenta 1715900 and Saldo Corregido CLP with USD == 0.00
        self.assertEqual(discrepancies["556677"]["account_code"], "1715900")
        self.assertEqual(discrepancies["556677"]["erp_authorization_code"], "556677")
        self.assertEqual(discrepancies["556677"]["operator_document_number"], "1715900")
        self.assertIn("Cta. ERP 1715900", discrepancies["556677"]["suggestion"]["headline"])

        self.assertEqual(orphans["888999"]["account_code"], "")
        self.assertEqual(orphans["888999"]["erp_authorization_code"], "")

        for bucket in ("matched_vouchers", "discrepancy_vouchers", "orphan_vouchers"):
            for item in preview[bucket]:
                self.assertNotIn("guest_name", item)
                self.assertNotIn("room_number", item)
                if item.get("suggestion"):
                    blob = str(item["suggestion"])
                    self.assertNotIn("JOHN DOE", blob)
                    self.assertNotIn("JANE SMITH", blob)
                    self.assertNotIn("Hab.", blob)
                    self.assertNotIn("Huésped", blob)

        session = self.client.session
        session["usd_reconciliation_preview"] = preview
        session.save()
        html_resp = self.client.get(reverse("usd_preview"))
        self.assertEqual(html_resp.status_code, 200)
        self.assertContains(html_resp, "Cruce Vouchers (TBK ⇄ Doc. ERP)")
        self.assertContains(html_resp, "Cruce Cuenta ERP ⇄ Doc. Pos. Clientes")
        self.assertContains(html_resp, "Cta ERP:")
        self.assertContains(html_resp, "Doc ERP:")
        self.assertContains(html_resp, "Doc PC:")
        self.assertContains(html_resp, "1715625")
        self.assertContains(html_resp, "1715900")
        self.assertContains(html_resp, "Cta ERP: 1715800")
        self.assertContains(html_resp, "Sin Pos. Cliente")
        self.assertNotContains(html_resp, "JOHN DOE")
        self.assertNotContains(html_resp, "JANE SMITH")
        self.assertNotContains(html_resp, "MARIO RUIZ")
        self.assertNotContains(html_resp, "Huésped")
        self.assertNotContains(html_resp, "Habitación")

        process = commit_usd_preview_to_database(self.user, preview)
        cm_112194 = process.cross_match_results.get(authorization_code="112194")
        cm_334455 = process.cross_match_results.get(authorization_code="334455")
        cm_556677 = process.cross_match_results.get(authorization_code="556677")
        self.assertEqual((cm_112194.account_code, cm_112194.document_number), ("1715625", "112194"))
        self.assertEqual((cm_334455.account_code, cm_334455.document_number), ("1715800", "334455"))
        self.assertEqual((cm_556677.account_code, cm_556677.document_number), ("1715900", "556677"))
        self.assertEqual(cm_112194.guest_name, "—")
        self.assertEqual(cm_112194.room_number, "—")

        wb = openpyxl.load_workbook(generate_committed_usd_excel_bytes(process.id))
        self.assertEqual(
            wb.sheetnames,
            ["Resumen Dólares", "Vouchers Ubicados", "Discrepancias y No Ubicados", "Sugerencias Asistidas"],
        )

        matched_row2 = [c.value for c in wb["Vouchers Ubicados"][2]]
        self.assertIn("TBK: 112194 = Doc. ERP: 112194", matched_row2)
        self.assertIn("Cta. ERP: 1715625 = Doc. PC: 1715625", matched_row2)

        disc_ws = wb["Discrepancias y No Ubicados"]
        disc_headers = [c.value for c in disc_ws[1]]
        self.assertEqual(disc_headers[:4], ["Autorización TBK", "Cuenta", "Documento Borderó", "Doc. Pos. Clientes"])
        disc_by_auth = {disc_ws.cell(r, 1).value: [disc_ws.cell(r, c).value or "" for c in range(1, 5)] for r in range(2, disc_ws.max_row + 1)}
        self.assertEqual(disc_by_auth["334455"], ["334455", "1715800", "334455", ""])
        self.assertEqual(disc_by_auth["556677"], ["556677", "1715900", "556677", "1715900"])
        self.assertEqual(disc_by_auth["888999"], ["888999", "", "", ""])

        sug_headers = [c.value for c in wb["Sugerencias Asistidas"][1]]
        self.assertEqual(len(sug_headers), 17)
        self.assertIn("Documento ERP", sug_headers)
        self.assertIn("Cuenta ERP", sug_headers)
        self.assertIn("Doc. Pos. Clientes", sug_headers)
        self.assertNotIn("Huésped / Habitación", sug_headers)

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            for row_vals in ws.iter_rows(values_only=True):
                for val in row_vals:
                    if val is None:
                        continue
                    s_val = str(val)
                    self.assertNotIn("JOHN DOE", s_val)
                    self.assertNotIn("JANE SMITH", s_val)
                    self.assertNotIn("MARIO RUIZ", s_val)
                    self.assertNotIn("Huésped", s_val)
                    self.assertNotIn("Habitación", s_val)


class DashboardExceptionsAndLegacyCompatTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("contador_mix", password="clave-de-prueba-123")
        self.client.force_login(self.user)
        self.usd_process = commit_usd_preview_to_database(self.user, calculate_usd_preview(**_build_usd_files()))
        self.clp_process = commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))

    def test_dashboard_and_exceptions_display_and_search_by_account_and_document(self):
        dash_usd = self.client.get(reverse("dashboard"), {"moneda": "usd"})
        self.assertContains(dash_usd, "Cuenta / Documento")
        self.assertContains(dash_usd, "Cta:</span> 1715800")
        self.assertContains(dash_usd, "Doc: 334455")
        self.assertNotContains(dash_usd, "JANE SMITH")

        dash_clp = self.client.get(reverse("dashboard"), {"moneda": "clp"})
        self.assertContains(dash_clp, "Cuenta / Documento")
        self.assertContains(dash_clp, "Cta:</span> 1786853")
        self.assertContains(dash_clp, "Doc: 7626")

        exc_usd = self.client.get(reverse("exceptions"), {"moneda": "usd"})
        self.assertContains(exc_usd, "Cuenta / Doc. / Cajero")
        self.assertContains(exc_usd, "Cuenta (Col. D)")
        self.assertContains(exc_usd, "Cta: 1715800")
        self.assertContains(exc_usd, "Doc: 334455")
        self.assertNotContains(exc_usd, "Habitación")
        self.assertNotContains(exc_usd, "Huésped")
        self.assertNotContains(exc_usd, "JANE SMITH")

        exc_by_account = self.client.get(reverse("exceptions"), {"moneda": "usd", "q": "1715900"})
        self.assertEqual([r.authorization_code for r in exc_by_account.context["rows"]], ["556677"])

        exc_by_doc = self.client.get(reverse("exceptions"), {"moneda": "clp", "q": "7626"})
        self.assertEqual([r.authorization_code for r in exc_by_doc.context["rows"]], ["007626"])

        export_usd = self.client.get(reverse("exceptions_export"), {"moneda": "usd"})
        sheet = openpyxl.load_workbook(io.BytesIO(export_usd.content))["Excepciones"]
        headers = [c.value for c in sheet[1]]
        self.assertIn("Cuenta", headers)
        self.assertIn("Documento", headers)
        self.assertNotIn("Huésped", headers)
        self.assertNotIn("Habitación", headers)
        exported_by_auth = {sheet.cell(r, 3).value: (sheet.cell(r, 4).value, sheet.cell(r, 5).value) for r in range(2, sheet.max_row + 1)}
        self.assertEqual(exported_by_auth["334455"], ("1715800", "334455"))
        self.assertEqual(exported_by_auth["556677"], ("1715900", "ERP: 556677 / PC: 1715900"))

    def test_legacy_cross_match_without_account_code_uses_fallbacks(self):
        legacy_usd = self.usd_process.cross_match_results.get(authorization_code="334455")
        legacy_usd.account_code = None
        legacy_usd.document_number = "1715800"
        legacy_usd.save(update_fields=["account_code", "document_number"])
        legacy_usd.refresh_from_db()
        self.assertEqual(legacy_usd.display_account_code, "1715800")
        self.assertEqual(legacy_usd.display_erp_document, "334455")

        legacy_clp = self.clp_process.cross_match_results.get(authorization_code="007626")
        legacy_clp.account_code = None
        legacy_clp.save(update_fields=["account_code"])
        legacy_clp.refresh_from_db()
        self.assertEqual(legacy_clp.display_account_code, "1786853")
        self.assertEqual(legacy_clp.display_erp_document, "7626")


class ErpCorrectionsSurviveReuploadTests(TestCase):
    """Momento 3 of the corrections flow: a new upload of the same Bordero must not undo a correction."""

    def setUp(self):
        self.user = User.objects.create_user("contador", password="x")
        self.process = commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))

    def _correct(self, row, field_name, new_value):
        # Stand-in for the PR 2 service: change the row and record it in the history
        ErpTransactionChange.objects.create(
            erp_transaction=row, field_name=field_name, old_value=str(getattr(row, field_name)),
            new_value=str(new_value), user=self.user, source=ErpTransactionChange.SOURCE_MANUAL,
        )
        setattr(row, field_name, new_value)
        row.save()

    def test_every_excel_row_gets_the_fingerprint_of_its_original_values(self):
        rows = ErpTransaction.objects.all()
        self.assertTrue(rows.exists())
        for row in rows:
            self.assertEqual(row.origin, ErpTransaction.ORIGIN_EXCEL)
            self.assertEqual(row.source_key, erp_source_key(
                row.source_currency or "CLP", row.document_number, row.invoice_number, row.authorization_code,
                row.amount, row.transaction_date, row.transaction_time,
            ))

    def test_corrected_amount_is_kept_and_the_voucher_stays_matched(self):
        self.assertEqual(self.process.cross_match_results.get(authorization_code="007626").system_result, "AMOUNT_MISMATCH")
        row = ErpTransaction.objects.get(authorization_code="7626")
        original_key = row.source_key
        self._correct(row, "amount", Decimal("115204.00"))
        erp_count = ErpTransaction.all_objects.count()

        preview = calculate_clp_preview(**build_clp_files())
        self.assertEqual((preview["erp_corrections_applied"]["corrected"], preview["erp_corrections_applied"]["deleted"]), (1, 0))
        self.assertEqual(_by_code(preview, "matched_vouchers")["007626"]["difference"], 0)
        commit_clp_preview_to_database(self.user, preview)

        row.refresh_from_db()
        self.assertEqual(row.amount, Decimal("115204.00"))
        self.assertEqual(row.source_key, original_key)
        self.assertEqual(ErpTransaction.all_objects.count(), erp_count)  # the typo did not come back as a new row
        self.assertEqual(self.process.cross_match_results.get(authorization_code="007626").system_result, "MATCHED")

    def test_soft_deleted_row_stays_deleted_and_out_of_the_cross_match(self):
        row = ErpTransaction.objects.get(authorization_code="112233")
        ErpTransactionChange.objects.create(
            erp_transaction=row, field_name=ErpTransactionChange.FIELD_DELETED, user=self.user,
            source=ErpTransactionChange.SOURCE_MANUAL, reason="Fila duplicada",
        )
        row.is_deleted = True
        row.save()
        erp_count = ErpTransaction.all_objects.count()

        preview = calculate_clp_preview(**build_clp_files())
        self.assertEqual((preview["erp_corrections_applied"]["corrected"], preview["erp_corrections_applied"]["deleted"]), (0, 1))
        self.assertIn("112233", _by_code(preview, "orphan_vouchers"))
        commit_clp_preview_to_database(self.user, preview)

        self.assertEqual(ErpTransaction.all_objects.count(), erp_count)
        self.assertFalse(ErpTransaction.objects.filter(authorization_code="112233").exists())
        self.assertTrue(ErpTransaction.all_objects.get(pk=row.pk).is_deleted)

    def test_a_row_without_history_is_left_as_the_excel_says(self):
        preview = calculate_clp_preview(**build_clp_files())
        self.assertEqual(preview["erp_corrections_applied"], {"corrected": 0, "deleted": 0, "manual": 0, "possible_duplicates": []})
        self.assertTrue(all(r["source_key"] for r in preview["erp_transactions"]))



class ErpManualCorrectionTests(TestCase):
    """Opción B: the accountant edits, adds or deletes an ERP row and the voucher is crossed again."""

    def setUp(self):
        self.user = User.objects.create_user("contador", password="x")
        self.process = commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))

    def _case(self, code):
        return self.process.cross_match_results.select_related(
            "process", "transbank_transaction", "card_type", "management_status"
        ).get(authorization_code=code)

    def test_fixing_the_amount_matches_and_survives_a_reupload(self):
        case = self._case("007626")
        row = case.erp_transaction
        outcome = edit_row(row, {"amount": "115204"}, self.user, match=case, reason="Voucher físico")

        self.assertTrue(outcome["matched"])
        case.refresh_from_db()
        self.assertEqual((case.system_result, case.management_status.code), ("MATCHED", "RESOLVED_ERP_FIXED"))
        self.assertEqual(case.difference_clp, 0)
        change = ErpTransactionChange.objects.get(erp_transaction=row)
        self.assertEqual((change.field_name, change.old_value, change.new_value), ("amount", "113700.00", "115204.00"))
        self.assertEqual(change.cross_match_result, case)
        self.assertIn("Monto $113.700 → $115.204", AuditResolutionLog.objects.filter(cross_match_result=case).first().comment)

        commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))
        case.refresh_from_db()
        self.assertEqual((case.system_result, case.management_status.code), ("MATCHED", "RESOLVED_ERP_FIXED"))

    def test_a_partial_fix_stays_pending_with_the_remaining_difference(self):
        case = self._case("007626")
        outcome = edit_row(case.erp_transaction, {"amount": "112000"}, self.user, match=case)
        self.assertFalse(outcome["matched"])
        case.refresh_from_db()
        self.assertEqual((case.system_result, case.management_status.code), ("AMOUNT_MISMATCH", "PENDING"))
        self.assertIn("Aún hay diferencia de $3.204", AuditResolutionLog.objects.filter(cross_match_result=case).first().comment)

    def test_any_erp_field_can_be_corrected_cleared_and_survives_a_reupload(self):
        case = self._case("007626")
        row = case.erp_transaction
        with self.assertRaises(CorrectionError):
            edit_row(row, {"guest_name": "No permitido"}, self.user, match=case)
        with self.assertRaises(CorrectionError):
            edit_row(row, {"room_number": "1204"}, self.user, match=case)
        edit_row(row, {
            "reservation_number": "RES-901", "room_type": "SUIT", "invoice_number": "F-778",
            "transaction_time": "14:35:10", "cashier_username": "", "amount": "",  # blank optional field clears; blank amount is kept
        }, self.user, match=case)
        row.refresh_from_db()
        self.assertEqual(
            (row.reservation_number, row.room_type, row.invoice_number, row.transaction_time.isoformat()),
            ("RES-901", "SUIT", "F-778", "14:35:10"),
        )
        self.assertEqual(row.amount, Decimal("113700.00"))
        self.assertIsNone(row.cashier_username)

        commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))
        row = ErpTransaction.objects.filter(source_key=row.source_key).order_by("-pk").first()
        self.assertEqual((row.reservation_number, row.room_type, row.invoice_number), ("RES-901", "SUIT", "F-778"))

    def test_unknown_or_unchanged_values_are_rejected(self):
        case = self._case("007626")
        row = case.erp_transaction
        with self.assertRaises(CorrectionError):
            edit_row(row, {"payment_code": "XX$"}, self.user, match=case)
        with self.assertRaises(CorrectionError):
            edit_row(row, {"amount": "113700"}, self.user, match=case)  # nothing changed

    def test_adding_a_missing_row_by_hand_matches_and_survives_a_reupload(self):
        case = self._case("F0FUAZ")
        self.assertEqual(case.system_result, "NOT_FOUND_IN_ERP")
        outcome = add_row(case, {
            "amount": "106999", "authorization_code": "F0FUAZ", "transaction_date": "2026-09-06",
            "payment_code": "RCO", "document_number": "1787015",
        }, self.user, reason="Cajero no registró el código")
        self.assertTrue(outcome["matched"])
        manual = ErpTransaction.objects.get(origin=ErpTransaction.ORIGIN_MANUAL)
        self.assertIsNone(manual.source_key)
        self.assertEqual(ErpTransactionChange.objects.get(erp_transaction=manual).field_name, ErpTransactionChange.FIELD_CREATED)
        erp_count = ErpTransaction.all_objects.count()

        preview = calculate_clp_preview(**build_clp_files())
        self.assertEqual(preview["erp_corrections_applied"]["manual"], 1)
        self.assertIn("F0FUAZ", _by_code(preview, "matched_vouchers"))
        commit_clp_preview_to_database(self.user, preview)
        case.refresh_from_db()
        self.assertEqual((case.system_result, case.management_status.code), ("MATCHED", "RESOLVED_ERP_FIXED"))
        self.assertEqual(ErpTransaction.all_objects.count(), erp_count)

    def test_adding_a_row_keeps_every_bordero_column(self):
        case = self._case("F0FUAZ")
        add_row(case, {
            "account_code": "C-555", "payment_code": "RCO", "invoice_number": "F-9", "amount": "106999",
            "transaction_date": "2026-09-06", "transaction_time": "13:45:20", "cashier_username": "mrojas",
            "reservation_number": "RES-77", "room_type": "KING", "authorization_code": "F0FUAZ",
        }, self.user)
        manual = ErpTransaction.objects.get(origin=ErpTransaction.ORIGIN_MANUAL)
        self.assertEqual(
            (manual.account_code, manual.invoice_number, manual.reservation_number, manual.room_type,
             manual.transaction_time.isoformat(), manual.cashier_username, manual.guest_name, manual.room_number),
            ("C-555", "F-9", "RES-77", "KING", "13:45:20", "MROJAS", None, None),
        )

    def test_adding_a_row_as_written_in_the_bordero(self):
        """Cuenta is the row's document, Documento the voucher code, Valor negative like the Excel."""
        case = self._case("F0FUAZ")
        outcome = add_row(case, {
            "account_code": "1787015", "payment_code": "RCO", "amount": "-106999", "authorization_code": "F0FUAZ",
            "transaction_date": "2026-09-06", "cashier_username": "  c  daza ",
        }, self.user)
        self.assertTrue(outcome["matched"])
        manual = ErpTransaction.objects.get(origin=ErpTransaction.ORIGIN_MANUAL)
        self.assertEqual((manual.document_number, manual.account_code, manual.amount), ("1787015", "1787015", Decimal("106999.00")))
        self.assertEqual((manual.cashier_username, manual.guest_name, manual.room_number), ("C DAZA", None, None))

    def test_adding_a_row_without_cuenta(self):
        case = self._case("F0FUAZ")
        outcome = add_row(case, {
            "account_code": "  ", "payment_code": "RCO", "amount": "-106999", "authorization_code": "F0FUAZ",
            "transaction_date": "2026-09-06",
        }, self.user)
        self.assertTrue(outcome["matched"])
        manual = ErpTransaction.objects.get(origin=ErpTransaction.ORIGIN_MANUAL)
        self.assertEqual((manual.document_number, manual.account_code), ("", None))

    def test_deleting_needs_a_reason_and_crosses_the_linked_voucher_again(self):
        case = self._case("112233")
        row = case.erp_transaction
        with self.assertRaises(CorrectionError):
            soft_delete_row(row, self.user, "  ", match=case)
        soft_delete_row(row, self.user, "Fila duplicada", match=case)
        row.refresh_from_db()
        self.assertTrue(row.is_deleted)
        self.assertEqual(row.deleted_by, self.user)
        case.refresh_from_db()
        self.assertEqual((case.system_result, case.management_status.code), ("NOT_FOUND_IN_ERP", "PENDING"))

    def test_possible_duplicate_of_a_hand_added_row_is_flagged_on_upload(self):
        ErpTransaction.objects.create(
            process=self.process, document_number="9999", account_code="9999", authorization_code="555444",
            amount=Decimal("41000"), transaction_date=date(2026, 9, 5), payment_code="RCO",
            source_currency="CLP", origin=ErpTransaction.ORIGIN_MANUAL,
        )
        payments = BORDERO_PAYMENTS + [
            (None, 1799999, "RCO", "Red Compra", 260001, -41000, 555444, "05/09/2026", "11:00:00", "SCORREA", "PARC"),
        ]
        files = build_clp_files()
        files["erp_file"] = build_bordero(payments)
        duplicates = calculate_clp_preview(**files)["erp_corrections_applied"]["possible_duplicates"]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["authorization_code"], "555444")


class ErpCorrectionInboxViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("contador", password="x")
        self.process = commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))
        self.client.force_login(self.user)
        self.case = self.process.cross_match_results.get(authorization_code="007626")

    def test_inbox_shows_the_correction_panel(self):
        response = self.client.get(reverse("exceptions") + "?moneda=clp")
        self.assertContains(response, "Corregir fila del ERP")
        self.assertContains(response, reverse("exception_erp_edit", args=[self.case.id, self.case.erp_transaction_id]))

    def test_posting_a_fix_matches_and_the_case_moves_to_resolved(self):
        response = self.client.post(
            reverse("exception_erp_edit", args=[self.case.id, self.case.erp_transaction_id]),
            {"amount": "115204", "authorization_code": "7626", "transaction_date": "2026-09-05",
             "payment_code": "RCO", "document_number": "1786853", "return_query": "moneda=clp"},
            follow=True,
        )
        self.assertContains(response, "Corregido en ERP")
        self.case.refresh_from_db()
        self.assertEqual(self.case.management_status.code, "RESOLVED_ERP_FIXED")
        resolved = self.client.get(reverse("exceptions") + "?moneda=clp&estado=resueltas")
        self.assertContains(resolved, "007626")
        self.assertContains(resolved, "Corrección en ERP: Monto")

    def test_a_bad_value_shows_an_error_and_changes_nothing(self):
        response = self.client.post(
            reverse("exception_erp_edit", args=[self.case.id, self.case.erp_transaction_id]),
            {"amount": "abc", "return_query": "moneda=clp"}, follow=True,
        )
        self.assertContains(response, "no es un número válido")
        self.assertFalse(ErpTransactionChange.objects.exists())


class UsdVoucherRecrossTests(TestCase):
    """A dollar voucher crosses again through ERP -> card operator -> Transbank after a code fix."""

    def test_fixing_a_typo_in_the_code_matches_the_voucher(self):
        user = User.objects.create_user("contador", password="x")
        visa, _ = CardType.objects.get_or_create(code="VI", defaults={"name": "Visa"})
        pending, _ = ReconciliationStatus.objects.get_or_create(code="PENDING", defaults={"display_name": "Pendiente"})
        process = ReconciliationProcess.objects.create(user=user, process_currency="USD", accounting_period="Cierre Dólares")
        tbk = TransbankTransaction.objects.create(
            process=process, card_type=visa, payment_modality="CREDIT", authorization_code="ABC123",
            sale_date=date(2025, 11, 26), original_sale_amount=Decimal("100.00"),
        )
        row = ErpTransaction.objects.create(
            process=process, card_type=visa, document_number="D1", invoice_number="F1", authorization_code="ABC12",
            amount=Decimal("95000"), transaction_date=date(2025, 11, 26), payment_code="VUS$", source_currency="USD",
        )
        CardOperatorTransaction.objects.create(
            process=process, card_type=visa, document_number="D1", foreign_currency_amount=Decimal("100.00"),
            balance_amount=Decimal("95000"),
        )
        case = CrossMatchResult.objects.create(
            process=process, card_type=visa, transbank_transaction=tbk, authorization_code="ABC123",
            transbank_amount=Decimal("100.00"), system_result="NOT_FOUND_IN_ERP", management_status=pending,
        )
        outcome = edit_row(row, {"authorization_code": "ABC123"}, user, match=case)
        self.assertTrue(outcome["matched"])
        case.refresh_from_db()
        self.assertEqual((case.system_result, case.erp_transaction, case.difference_usd), ("MATCHED", row, Decimal("0.00")))



class SuggestionAcceptRejectTests(TestCase):
    """Opción A: a suggestion carries one concrete change that the accountant accepts or rejects."""

    def setUp(self):
        self.user = User.objects.create_user("contador", password="x")
        self.process = commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))

    def _case(self, code):
        return self.process.cross_match_results.select_related(
            "process", "transbank_transaction", "card_type", "management_status"
        ).get(authorization_code=code)

    def test_typo_suggestion_names_the_row_field_and_value(self):
        proposal = open_proposal(self._case("F0FUAZ"))
        row = ErpTransaction.objects.get(pk=proposal["erp_id"])
        self.assertEqual(row.authorization_code, "0FOFUAZ")
        self.assertEqual((proposal["field"], proposal["old"], proposal["new"]), ("authorization_code", "0FOFUAZ", "F0FUAZ"))

    def test_accepting_fixes_the_erp_matches_and_survives_a_reupload(self):
        case = self._case("F0FUAZ")
        outcome = accept_suggestion(case, self.user)
        self.assertTrue(outcome["matched"])
        case.refresh_from_db()
        self.assertEqual((case.system_result, case.management_status.code), ("MATCHED", "RESOLVED_ERP_FIXED"))
        change = ErpTransactionChange.objects.get(cross_match_result=case)
        self.assertEqual(change.source, ErpTransactionChange.SOURCE_SUGGESTION)
        self.assertEqual(load_suggestion(case)["proposed_change"]["status"], "accepted")
        with self.assertRaises(CorrectionError):
            accept_suggestion(case, self.user)  # already used

        commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))
        case.refresh_from_db()
        self.assertEqual((case.system_result, case.management_status.code), ("MATCHED", "RESOLVED_ERP_FIXED"))

    def test_amount_suggestion_and_rejecting_leaves_the_erp_alone(self):
        case = self._case("007626")
        proposal = open_proposal(case)
        self.assertEqual((proposal["field"], proposal["old"], proposal["new"]), ("amount", "113700.00", "115204.00"))

        reject_suggestion(case, self.user, "El voucher físico dice 113.700")
        case.refresh_from_db()
        self.assertEqual(case.management_status.code, "REJECTED")
        self.assertEqual(case.erp_transaction.amount, Decimal("113700.00"))
        self.assertFalse(ErpTransactionChange.objects.exists())
        self.assertIsNone(open_proposal(case))
        self.assertIn("Sugerencia rechazada", AuditResolutionLog.objects.filter(cross_match_result=case).first().comment)

        # Rejected stays open: it can still be fixed by hand (Opción B)
        outcome = edit_row(case.erp_transaction, {"amount": "115204"}, self.user, match=case)
        self.assertTrue(outcome["matched"])

    def test_accepting_after_the_row_changed_is_refused(self):
        case = self._case("007626")
        row = case.erp_transaction
        row.amount = Decimal("114000")
        row.save()
        with self.assertRaises(CorrectionError):
            accept_suggestion(case, self.user)

    def test_inbox_buttons_and_counters(self):
        self.client.force_login(self.user)
        case = self._case("F0FUAZ")
        page = self.client.get(reverse("exceptions") + "?moneda=clp")
        self.assertContains(page, "Correcciones sugeridas en el ERP")
        self.assertContains(page, f'data-suggestion-case="{case.id}"')
        self.assertContains(page, reverse("exception_suggestion_accept", args=[case.id]))

        other = self._case("007626")
        response = self.client.post(reverse("exception_suggestion_reject", args=[other.id]), {"return_query": "moneda=clp"}, follow=True)
        self.assertContains(response, "sugerencia rechazada")
        self.assertContains(response, "Rechazada")  # still listed with the open cases

        response = self.client.post(reverse("exception_suggestion_accept", args=[case.id]), {"return_query": "moneda=clp"}, follow=True)
        self.assertContains(response, "Corregido en ERP")



class PreviewCorrectionTests(TestCase):
    """Step 2: a suggested correction is accepted (and may be adjusted) before the closing is saved."""

    def setUp(self):
        self.user = User.objects.create_user("contador", password="x")
        self.preview = calculate_clp_preview(**build_clp_files())

    def _tbk_index(self, bucket, code):
        return _by_code(self.preview, bucket)[code]["tbk_index"]

    def test_the_preview_already_names_the_row_field_and_value(self):
        proposal = _by_code(self.preview, "orphan_vouchers")["F0FUAZ"]["suggestion"]["proposed_change"]
        self.assertEqual((proposal["field"], proposal["old"], proposal["new"]), ("authorization_code", "0FOFUAZ", "F0FUAZ"))
        self.assertIsNotNone(proposal["erp_index"])

    def test_applying_moves_the_voucher_to_matched_and_saving_writes_the_erp(self):
        outcome = apply_preview_correction(self.preview, "CLP", self._tbk_index("orphan_vouchers", "F0FUAZ"), "")
        self.assertTrue(outcome["matched"])
        self.assertIn("F0FUAZ", _by_code(self.preview, "matched_vouchers"))
        self.assertNotIn("F0FUAZ", _by_code(self.preview, "orphan_vouchers"))
        self.assertEqual(self.preview["counts"]["matched"], 5)
        with self.assertRaises(PreviewCorrectionError):
            apply_preview_correction(self.preview, "CLP", self._tbk_index("matched_vouchers", "F0FUAZ"), "")

        process = commit_clp_preview_to_database(self.user, self.preview)
        case = process.cross_match_results.get(authorization_code="F0FUAZ")
        self.assertEqual((case.system_result, case.management_status.code), ("MATCHED", "RESOLVED_ERP_FIXED"))
        row = case.erp_transaction
        self.assertEqual(row.authorization_code, "F0FUAZ")
        self.assertEqual(row.source_key, erp_source_key(  # fingerprint of the ORIGINAL Excel value
            "CLP", row.document_number, row.invoice_number, "0FOFUAZ", row.amount, row.transaction_date, row.transaction_time))
        change = ErpTransactionChange.objects.get(erp_transaction=row)
        self.assertEqual((change.source, change.old_value, change.new_value), ("SUGGESTION", "0FOFUAZ", "F0FUAZ"))
        self.assertIn("No está en el ERP → Cuadrado", AuditResolutionLog.objects.filter(cross_match_result=case).first().comment)
        erp_count = ErpTransaction.all_objects.count()

        commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))
        case.refresh_from_db()
        self.assertEqual((case.system_result, case.management_status.code), ("MATCHED", "RESOLVED_ERP_FIXED"))
        self.assertEqual(ErpTransaction.all_objects.count(), erp_count)

    def test_the_accountant_can_adjust_the_suggested_value(self):
        idx = self._tbk_index("discrepancy_vouchers", "007626")
        with self.assertRaises(PreviewCorrectionError):
            apply_preview_correction(self.preview, "CLP", idx, "abc")
        outcome = apply_preview_correction(self.preview, "CLP", idx, "115000")  # 204 within the ±1.000 tolerance
        self.assertTrue(outcome["matched"])
        process = commit_clp_preview_to_database(self.user, self.preview)
        self.assertEqual(process.cross_match_results.get(authorization_code="007626").erp_transaction.amount, Decimal("115000.00"))

    def test_preview_endpoint(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["clp_reconciliation_preview"] = self.preview
        session.save()
        url = reverse("clp_preview_correction")
        idx = self._tbk_index("orphan_vouchers", "F0FUAZ")
        response = self.client.post(url, data={"tbk_index": idx, "value": "F0FUAZ"}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["matched"])
        again = self.client.post(url, data={"tbk_index": idx, "value": "F0FUAZ"}, content_type="application/json")
        self.assertEqual(again.status_code, 400)
        page = self.client.get(reverse("clp_preview"))
        self.assertContains(page, "aceptada en este paso")


class SuggestionPanelTests(TestCase):
    """The inbox lists the suggested ERP corrections above the table, with an editable value."""

    def setUp(self):
        self.user = User.objects.create_user("contador", password="x")
        self.process = commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))
        self.client.force_login(self.user)

    def test_panel_lists_open_suggestions_and_accepts_an_edited_value(self):
        page = self.client.get(reverse("exceptions") + "?moneda=clp")
        self.assertContains(page, "Correcciones sugeridas en el ERP")
        case = self.process.cross_match_results.get(authorization_code="007626")
        self.assertContains(page, f'data-suggestion-case="{case.id}" data-suffix="p"')
        cards = page.context["suggestion_cards"]["cases"]
        self.assertEqual(cards[str(case.id)]["suggestion"]["proposed_change"]["new"], "115204.00")
        self.assertEqual(cards[str(case.id)]["tbk_amount"], 115204.0)

        self.client.post(reverse("exception_suggestion_accept", args=[case.id]), {"value": "115000", "return_query": "moneda=clp"})
        case.refresh_from_db()
        self.assertEqual(case.management_status.code, "RESOLVED_ERP_FIXED")
        self.assertEqual(case.erp_transaction.amount, Decimal("115000.00"))
        self.assertEqual(ErpTransactionChange.objects.get().reason, "Sugerencia aceptada con el valor ajustado")
        self.assertNotContains(self.client.get(reverse("exceptions") + "?moneda=clp"), f'data-suggestion-case="{case.id}" data-suffix="p"')


class SuggestionModalAjaxTests(TestCase):
    """Pendientes: the suggestions window accepts and rejects without leaving it (JSON answers)."""

    def setUp(self):
        self.user = User.objects.create_user("contador", password="x")
        self.process = commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))
        self.client.force_login(self.user)
        self.ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

    def test_button_opens_the_window_and_decisions_answer_json(self):
        page = self.client.get(reverse("exceptions") + "?moneda=clp")
        self.assertContains(page, 'data-bs-target="#inboxSuggestionModal"')
        self.assertContains(page, "Ver Sugerencias Asistidas (2 casos)")

        accept_case = self.process.cross_match_results.get(authorization_code="F0FUAZ")
        response = self.client.post(reverse("exception_suggestion_accept", args=[accept_case.id]), {"value": "F0FUAZ"}, **self.ajax)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["status"], response.json()["matched"]), ("accepted", True))

        reject_case = self.process.cross_match_results.get(authorization_code="007626")
        response = self.client.post(reverse("exception_suggestion_reject", args=[reject_case.id]), {}, **self.ajax)
        self.assertEqual(response.json()["status"], "rejected")

        again = self.client.post(reverse("exception_suggestion_reject", args=[reject_case.id]), {}, **self.ajax)
        self.assertEqual(again.status_code, 400)
        self.assertIn("error", again.json())


class ReadOnlyTableVisualizationPanelsTests(TestCase):
    """Covers all 6 read-only table panels in preview, saved closing detail, and audit trail."""

    def setUp(self):
        self.user = User.objects.create_user("auditor_tablas", password="clave-de-prueba-123")
        self.client.force_login(self.user)

        usd_files = _build_usd_files()
        self.usd_process = commit_usd_preview_to_database(self.user, calculate_usd_preview(**usd_files))
        self.clp_process = commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))
        # Add an unlinked ERP row so "Solo en ERP (sin TBK)" has a concrete example to filter and display
        ErpTransaction.objects.create(
            process=self.clp_process,
            account_code="1799999",
            document_number="999001",
            reservation_number="RES4297830",
            room_number="1001",
            room_type="SE1K",
            guest_name="ANA PEREZ",
            authorization_code="999001",
            amount=Decimal("42500.00"),
            source_currency="CLP",
            reconciled=False,
            origin=ErpTransaction.ORIGIN_EXCEL,
        )

    def test_all_stored_erp_and_transbank_columns_rendered(self):
        from .services.tables_viewer import build_preview_source_tables_context

        # Consultas -> Borderó ERP consolidado y por cierre: muestran TODAS las columnas (incluidas Habitación y Huésped)
        erp_cons = self.client.get(reverse("consolidated_erp"))
        self.assertEqual(erp_cons.status_code, 200)
        for col in (
            "Cuenta", "Documento", "N° Reserva", "Habitación", "Tipo", "Huésped",
            "Cód. Autorización", "Factura", "Cód. Pago", "Tarjeta", "Fecha",
            "Hora", "Cajero", "Monto (CLP)", "Moneda", "Cierre", "Origen",
            "Cruce con Transbank",
        ):
            self.assertContains(erp_cons, col)
        self.assertContains(erp_cons, "RES4297830")
        self.assertContains(erp_cons, "1001")
        self.assertContains(erp_cons, "SE1K")
        self.assertContains(erp_cons, "ANA PEREZ")

        by_res = self.client.get(reverse("consolidated_erp"), {"q": "RES4297830"})
        self.assertEqual(by_res.context["kpis"]["total"], 1)
        by_room = self.client.get(reverse("consolidated_erp"), {"q": "1001"})
        self.assertEqual(by_room.context["kpis"]["total"], 1)
        by_room_type = self.client.get(reverse("consolidated_erp"), {"q": "SE1K"})
        self.assertEqual(by_room_type.context["kpis"]["total"], 1)
        by_guest = self.client.get(reverse("consolidated_erp"), {"q": "ANA PEREZ"})
        self.assertEqual(by_guest.context["kpis"]["total"], 1)

        erp_closing = self.client.get(reverse("closing_detail", args=[self.clp_process.id]), {"vista": "erp"})
        self.assertEqual(erp_closing.status_code, 200)
        for col in ("N° Reserva", "Habitación", "Tipo", "Huésped", "Cód. Pago", "Tarjeta", "Hora", "Moneda"):
            self.assertContains(erp_closing, col)
        self.assertContains(erp_closing, "RES4297830")
        self.assertContains(erp_closing, "1001")
        self.assertContains(erp_closing, "SE1K")
        self.assertContains(erp_closing, "ANA PEREZ")

        tbk_cons = self.client.get(reverse("consolidated_transbank"))
        self.assertEqual(tbk_cons.status_code, 200)
        for col in (
            "Cód. Autorización", "Modalidad", "Tarjeta", "Fecha Venta",
            "Tarjeta Enmascarada", "Cód. Comercio", "Comercio / Local",
            "Boleta", "TID", "Tipo Cuota", "N° Cuota", "Moneda", "Cierre",
            "Venta", "Comisión TBK", "IVA Com.", "Abono Neto", "Estado",
        ):
            self.assertContains(tbk_cons, col)

        tbk_closing = self.client.get(reverse("closing_detail", args=[self.clp_process.id]), {"vista": "transbank"})
        self.assertEqual(tbk_closing.status_code, 200)
        for col in (
            "Modalidad", "Tarjeta", "Cód. Comercio", "Comercio / Local",
            "Boleta", "TID", "Tipo Cuota", "N° Cuota", "Moneda",
        ):
            self.assertContains(tbk_closing, col)

        # Conciliación (preview + Resultados del Cruce): mantienen Cuenta/Documento y NO exponen Habitación ni Huésped
        cruce_closing = self.client.get(reverse("closing_detail", args=[self.clp_process.id]), {"vista": "cruce"})
        self.assertNotContains(cruce_closing, "Habitación")
        self.assertNotContains(cruce_closing, "Huésped")

        usd_src = build_preview_source_tables_context(calculate_usd_preview(**_build_usd_files()), "USD")
        erp_sample = usd_src["payload"]["erp"][0]
        for key in ("reservation_number", "room_type", "payment_code", "card_code", "transaction_time", "source_currency"):
            self.assertIn(key, erp_sample)
        self.assertNotIn("room_number", erp_sample)
        self.assertNotIn("guest_name", erp_sample)
        self.assertEqual(erp_sample["source_currency"], "USD")
        tbk_sample = usd_src["payload"]["tbk"][0]
        for key in (
            "modality_label", "card_code", "merchant_code", "merchant_location_name",
            "receipt_number", "transaction_identifier", "installment_type",
            "installment_number", "currency",
        ):
            self.assertIn(key, tbk_sample)
        self.assertEqual(tbk_sample["currency"], "USD")

    def test_requires_login_and_blocks_mutations(self):
        erp_url = reverse("consolidated_erp")
        protected_urls = (
            erp_url,
            reverse("consolidated_transbank"),
            reverse("consolidated_bank"),
            reverse("consolidated_operator"),
            reverse("consolidated_matches"),
            reverse("closings_list"),
            reverse("closing_detail", args=[self.usd_process.id]),
            reverse("audit_trail"),
        )
        self.client.logout()
        for url in protected_urls:
            self.assertEqual(self.client.get(url).status_code, 302)

        self.client.force_login(self.user)
        for url in protected_urls:
            self.assertEqual(self.client.post(url, {}).status_code, 405)
            self.assertEqual(self.client.delete(url).status_code, 405)
            html = self.client.get(url)
            self.assertEqual(html.status_code, 200)
            self.assertContains(html, "Consultas")
            self.assertNotContains(html, "Solo lectura")
            self.assertNotContains(html, "solo lectura")
            self.assertNotContains(html, "Solo visualización")
            self.assertNotContains(html, "solo visualización")
            main_section = html.content.decode("utf-8").split("<main", 1)[-1]
            self.assertNotIn('method="post"', main_section.lower())
            if url == erp_url:
                self.assertContains(html, "Huésped")
                self.assertContains(html, "Habitación")
            else:
                self.assertNotContains(html, "Huésped")
                self.assertNotContains(html, "Habitación")

    def test_saved_closing_six_tabs_and_filters(self):
        # Panel 1: Directory & Dashboard link
        directory = self.client.get(reverse("closings_list"), {"moneda": "USD"})
        self.assertContains(directory, f"#{self.usd_process.id}")
        self.assertNotContains(directory, self.clp_process.accounting_period)

        # Panel 1 (resumen & cruce)
        resumen = self.client.get(reverse("closing_detail", args=[self.usd_process.id]), {"vista": "resumen"})
        self.assertContains(resumen, "Cuadratura contable guardada")
        self.assertContains(resumen, "TARJETAS ERP USD.xlsx")

        cruce = self.client.get(
            reverse("closing_detail", args=[self.usd_process.id]),
            {"vista": "cruce", "q": "1715800"},
        )
        self.assertContains(cruce, "1715800")
        self.assertContains(cruce, "334455")
        self.assertNotContains(cruce, "JANE SMITH")

        # Panel 2: Borderó ERP including rows absent from Transbank ("Solo en ERP (sin TBK)")
        erp_solo = self.client.get(
            reverse("closing_detail", args=[self.clp_process.id]),
            {"vista": "erp", "cruce": "solo_erp"},
        )
        self.assertContains(erp_solo, "1799999")
        self.assertContains(erp_solo, "Solo en ERP (sin TBK)")

        # Panel 3: Cartola Bancaria
        cartola = self.client.get(
            reverse("closing_detail", args=[self.usd_process.id]),
            {"vista": "cartola", "tipo": "cuadrados"},
        )
        self.assertContains(cartola, "Abono cuadrado")
        self.assertContains(cartola, "Abono Divisas Transbank")

        # Panel 4: Posición por Clientes (USD only)
        operadoras_all = self.client.get(
            reverse("closing_detail", args=[self.usd_process.id]),
            {"vista": "operadoras"},
        )
        self.assertContains(operadoras_all, "1715625")
        self.assertContains(operadoras_all, "999888")
        operadoras_unused = self.client.get(
            reverse("closing_detail", args=[self.usd_process.id]),
            {"vista": "operadoras", "cruce": "sin_usar"},
        )
        self.assertContains(operadoras_unused, "999888")
        self.assertNotContains(operadoras_unused, "1715625")

        # Panel 6: Transbank Detallado
        tbk_tab = self.client.get(
            reverse("closing_detail", args=[self.clp_process.id]),
            {"vista": "transbank"},
        )
        self.assertEqual(tbk_tab.status_code, 200)
        self.assertContains(tbk_tab, "007626")

    def test_audit_trail_shows_erp_changes_and_resolutions_read_only(self):
        fix_case = self.clp_process.cross_match_results.get(authorization_code="007626")
        self.client.post(
            reverse("exception_suggestion_accept", args=[fix_case.id]),
            {"value": "115204", "return_query": "moneda=clp"},
        )
        orphan_case = self.usd_process.cross_match_results.get(authorization_code="888999")
        self.client.post(
            reverse("exception_resolve", args=[orphan_case.id]),
            {"status": "RESOLVED_ADJUSTMENT", "comment": "Validado con tesorería", "return_query": "moneda=usd"},
        )

        erp_audit = self.client.get(reverse("audit_trail"), {"vista": "erp", "moneda": "CLP"})
        self.assertEqual(erp_audit.status_code, 200)
        self.assertContains(erp_audit, "115.204")
        self.assertContains(erp_audit, "1786853")
        self.assertNotContains(erp_audit, "Huésped")
        self.assertNotContains(erp_audit, "Habitación")

        res_audit = self.client.get(reverse("audit_trail"), {"vista": "resoluciones", "moneda": "USD"})
        self.assertEqual(res_audit.status_code, 200)
        self.assertContains(res_audit, "888999")
        self.assertContains(res_audit, "Ajuste contable registrado")
        self.assertContains(res_audit, "Validado con tesorería")

    def test_preview_source_tables_render_sanitized_in_usd_and_clp(self):
        usd_preview = calculate_usd_preview(**_build_usd_files())
        session = self.client.session
        session["usd_reconciliation_preview"] = usd_preview
        session["clp_reconciliation_preview"] = calculate_clp_preview(**build_clp_files())
        session.save()

        usd_resp = self.client.get(reverse("usd_preview"))
        self.assertContains(usd_resp, 'id="sourceTablesCard"')
        self.assertContains(usd_resp, 'id="jsonSourceTablesData"')
        self.assertContains(usd_resp, "Posición por Clientes")
        self.assertContains(usd_resp, "1715625")
        self.assertNotContains(usd_resp, "JOHN DOE")
        self.assertNotContains(usd_resp, "JANE SMITH")
        self.assertNotContains(usd_resp, "MARIO RUIZ")
        self.assertNotContains(usd_resp, "Huésped")
        self.assertNotContains(usd_resp, "Habitación")
        self.assertNotContains(usd_resp, "Solo visualización")

        clp_resp = self.client.get(reverse("clp_preview"))
        self.assertContains(clp_resp, 'id="sourceTablesCard"')
        self.assertContains(clp_resp, "Borderó ERP")
        self.assertContains(clp_resp, "Cartola Bancaria")
        self.assertContains(clp_resp, "Transbank Detallado")
        self.assertNotContains(clp_resp, "Huésped")
        self.assertNotContains(clp_resp, "Habitación")
        self.assertNotContains(clp_resp, "Solo visualización")

    def test_consolidated_tables_combine_all_closings_and_filter_cleanly(self):
        # Consolidated Borderó ERP shows rows from BOTH USD and CLP closings together
        erp_all = self.client.get(reverse("consolidated_erp"))
        self.assertEqual(erp_all.status_code, 200)
        self.assertContains(erp_all, "1715625")  # USD ERP row
        self.assertContains(erp_all, "1786853")  # CLP ERP row
        self.assertContains(erp_all, "1799999")  # CLP unlinked ERP row
        self.assertContains(erp_all, "JOHN DOE")
        self.assertContains(erp_all, "602")

        erp_solo = self.client.get(reverse("consolidated_erp"), {"cruce": "solo_erp", "moneda": "CLP"})
        self.assertContains(erp_solo, "1799999")
        self.assertNotContains(erp_solo, "1715625")

        # Consolidated Transbank shows BOTH USD and CLP TBK vouchers together
        tbk_all = self.client.get(reverse("consolidated_transbank"))
        self.assertEqual(tbk_all.status_code, 200)
        self.assertContains(tbk_all, "888999")  # USD Transbank row
        self.assertContains(tbk_all, "007626")  # CLP Transbank row
        self.assertContains(tbk_all, f"#{self.usd_process.id}")
        self.assertContains(tbk_all, f"#{self.clp_process.id}")

        tbk_usd = self.client.get(reverse("consolidated_transbank"), {"moneda": "USD"})
        self.assertContains(tbk_usd, "888999")
        self.assertNotContains(tbk_usd, "007626")

        # Consolidated Cartola, Posición por Clientes & Resultados del Cruce
        bank_all = self.client.get(reverse("consolidated_bank"))
        self.assertContains(bank_all, "Abono Divisas Transbank")

        op_all = self.client.get(reverse("consolidated_operator"), {"cruce": "sin_usar"})
        self.assertContains(op_all, "999888")
        self.assertNotContains(op_all, "1715625")

        cruce_all = self.client.get(reverse("consolidated_matches"))
        self.assertContains(cruce_all, "334455")
        self.assertContains(cruce_all, "007626")
        self.assertNotContains(cruce_all, "JOHN DOE")
        self.assertNotContains(cruce_all, "JANE SMITH")

    def test_accepting_suggestion_or_resolving_exception_updates_transbank_status(self):
        fix_case = self.clp_process.cross_match_results.get(authorization_code="007626")
        orphan_case = self.usd_process.cross_match_results.get(authorization_code="888999")
        self.assertFalse(fix_case.transbank_transaction.reconciled)
        self.assertFalse(orphan_case.transbank_transaction.reconciled)

        before_pending = self.client.get(reverse("consolidated_transbank"), {"estado": "pendiente"})
        self.assertContains(before_pending, "007626")
        self.assertContains(before_pending, "888999")

        # 1) Accepting a suggestion syncs TransbankTransaction.reconciled=True and shows Conciliado
        self.client.post(
            reverse("exception_suggestion_accept", args=[fix_case.id]),
            {"value": "115204", "return_query": "moneda=clp"},
        )
        fix_case.transbank_transaction.refresh_from_db()
        self.assertTrue(fix_case.transbank_transaction.reconciled)

        conciliado_resp = self.client.get(reverse("consolidated_transbank"), {"estado": "conciliado"})
        self.assertContains(conciliado_resp, "007626")
        self.assertContains(conciliado_resp, "Conciliado")
        pending_after_accept = self.client.get(reverse("consolidated_transbank"), {"estado": "pendiente"})
        self.assertNotContains(pending_after_accept, "007626")

        closing_conciliado = self.client.get(
            reverse("closing_detail", args=[self.clp_process.id]),
            {"vista": "transbank", "estado": "conciliado"},
        )
        self.assertContains(closing_conciliado, "007626")

        # Legacy pre-fix rows (CrossMatchResult=MATCHED while tbk.reconciled=False) also show as Conciliado
        tbk_clp = fix_case.transbank_transaction
        tbk_clp.reconciled = False
        tbk_clp.save(update_fields=["reconciled"])
        legacy_conciliado = self.client.get(reverse("consolidated_transbank"), {"estado": "conciliado"})
        self.assertContains(legacy_conciliado, "007626")
        legacy_pending = self.client.get(reverse("consolidated_transbank"), {"estado": "pendiente"})
        self.assertNotContains(legacy_pending, "007626")

        # 2) Manually resolving an exception moves Transbank status from Pendiente -> Resuelto
        self.client.post(
            reverse("exception_resolve", args=[orphan_case.id]),
            {"status": "RESOLVED_ADJUSTMENT", "comment": "Ajuste aprobado", "return_query": "moneda=usd"},
        )
        resuelto_resp = self.client.get(reverse("consolidated_transbank"), {"estado": "resuelto"})
        self.assertContains(resuelto_resp, "888999")
        self.assertContains(resuelto_resp, "Resuelto")
        self.assertContains(resuelto_resp, "Ajuste contable registrado")
        pending_after_resolve = self.client.get(reverse("consolidated_transbank"), {"estado": "pendiente"})
        self.assertNotContains(pending_after_resolve, "888999")

        closing_resuelto = self.client.get(
            reverse("closing_detail", args=[self.usd_process.id]),
            {"vista": "transbank", "estado": "resuelto"},
        )
        self.assertContains(closing_resuelto, "888999")

        # 3) Reopening a resolved exception restores Pendiente
        self.client.post(
            reverse("exception_reopen", args=[orphan_case.id]),
            {"comment": "Reapertura de prueba", "return_query": "moneda=usd"},
        )
        reopened_pending = self.client.get(reverse("consolidated_transbank"), {"estado": "pendiente"})
        self.assertContains(reopened_pending, "888999")
        reopened_resuelto = self.client.get(reverse("consolidated_transbank"), {"estado": "resuelto"})
        self.assertNotContains(reopened_resuelto, "888999")

    def test_modified_erp_rows_show_badge_and_before_history_in_tables_and_preview(self):
        import json as _json
        import re as _re

        fix_case = self.clp_process.cross_match_results.get(authorization_code="007626")
        erp_row = fix_case.erp_transaction
        orig_doc = erp_row.document_number

        # Before any modification, no history trigger renders on consolidated ERP or cruce
        before_erp = self.client.get(reverse("consolidated_erp"))
        self.assertNotContains(before_erp, "js-erp-history-trigger")
        before_mod_filter = self.client.get(reverse("consolidated_erp"), {"origen": "MODIFIED"})
        self.assertNotContains(before_mod_filter, orig_doc)

        # Modify twice: accept suggestion on amount, then manually adjust document_number
        self.client.post(
            reverse("exception_suggestion_accept", args=[fix_case.id]),
            {"value": "115204", "return_query": "moneda=clp"},
        )
        self.client.post(
            reverse("exception_erp_edit", args=[fix_case.id, erp_row.id]),
            {
                "amount": "115204",
                "authorization_code": "007626",
                "document_number": "1786900",
                "reason": "Ajuste documento contable",
                "return_query": "moneda=clp",
            },
        )

        # Consolidated Borderó ERP & filter origen=MODIFIED
        after_erp = self.client.get(reverse("consolidated_erp"))
        self.assertContains(after_erp, "js-erp-history-trigger")
        self.assertContains(after_erp, "Modificada (3)")
        self.assertContains(after_erp, 'id="erpHistoryModal"')
        self.assertContains(after_erp, 'id="jsonErpHistoryData"')

        m = _re.search(
            r'<script id="jsonErpHistoryData" type="application/json">(.*?)</script>',
            after_erp.content.decode("utf-8"),
            _re.DOTALL,
        )
        self.assertIsNotNone(m)
        history_payload = _json.loads(m.group(1))
        row_hist = history_payload[str(erp_row.id)]
        self.assertIn("room_number", row_hist["current_summary"])
        self.assertIn("guest_name", row_hist["current_summary"])
        self.assertEqual(row_hist["changes_count"], 3)
        self.assertEqual(row_hist["before_summary"]["account"], orig_doc)
        self.assertEqual(row_hist["before_summary"]["document"], orig_doc)
        self.assertEqual(row_hist["before_summary"]["auth_code"], "7626")
        self.assertEqual(row_hist["before_summary"]["amount"], "$113.700")
        self.assertEqual(row_hist["current_summary"]["account"], "1786900")
        self.assertEqual(row_hist["current_summary"]["document"], "1786900")
        self.assertEqual(row_hist["current_summary"]["auth_code"], "007626")
        self.assertEqual(row_hist["current_summary"]["amount"], "$115.204")
        self.assertEqual(len(row_hist["diff_fields"]), 3)

        only_mod = self.client.get(reverse("consolidated_erp"), {"origen": "MODIFIED"})
        self.assertContains(only_mod, "1786900")
        self.assertNotContains(only_mod, "1799999")

        # Per-closing Borderó ERP tab
        closing_erp = self.client.get(reverse("closing_detail", args=[self.clp_process.id]), {"vista": "erp"})
        self.assertContains(closing_erp, "Modificada (3)")
        self.assertContains(closing_erp, 'id="erpHistoryModal"')

        # Resultados del Cruce (consolidated & per-closing) also surface the modified ERP indicator
        cruce_cons = self.client.get(reverse("consolidated_matches"))
        self.assertContains(cruce_cons, "ERP modificado (3)")
        self.assertContains(cruce_cons, 'id="jsonErpHistoryData"')
        closing_cruce = self.client.get(reverse("closing_detail", args=[self.clp_process.id]), {"vista": "cruce"})
        self.assertContains(closing_cruce, "ERP modificado (3)")

        # Step 2 preview: in-memory accepted suggestion shows is_modified + before value immediately
        clp_preview = calculate_clp_preview(**build_clp_files())
        f0_tbk_idx = next(v["tbk_index"] for v in clp_preview["orphan_vouchers"] if v["authorization_code"] == "F0FUAZ")
        apply_preview_correction(clp_preview, "CLP", f0_tbk_idx, "")
        session = self.client.session
        session["clp_reconciliation_preview"] = clp_preview
        session.save()

        preview_resp = self.client.get(reverse("clp_preview"))
        self.assertContains(preview_resp, 'id="erpHistoryModal"')
        pm = _re.search(
            r'<script type="application/json" id="jsonSourceTablesData">(.*?)</script>',
            preview_resp.content.decode("utf-8"),
            _re.DOTALL,
        )
        self.assertIsNotNone(pm)
        src_payload = _json.loads(pm.group(1))
        mod_preview_rows = [r for r in src_payload["erp"] if r.get("is_modified")]
        self.assertGreaterEqual(len(mod_preview_rows), 2)
        db_inherited = next(r for r in mod_preview_rows if r["authorization_code"] == "007626")
        self.assertNotIn("room_number", db_inherited["history"]["current_summary"])
        self.assertNotIn("guest_name", db_inherited["history"]["current_summary"])
        self.assertEqual(db_inherited["history"]["before_summary"]["amount"], "$113.700")
        self.assertEqual(db_inherited["history"]["current_summary"]["amount"], "$115.204")
        in_memory_row = next(r for r in mod_preview_rows if r["authorization_code"] == "F0FUAZ")
        self.assertEqual(in_memory_row["history"]["diff_fields"][0]["before"], "0FOFUAZ")
        self.assertEqual(in_memory_row["history"]["diff_fields"][0]["after"], "F0FUAZ")


class UserManagementTests(TestCase):
    """Covers superadmin-only user lifecycle management and parity of non-user-management permissions."""

    def setUp(self):
        self.superadmin = User.objects.create_superuser("super_hotel", password="ClaveFuerte!2026")
        self.standard = User.objects.create_user(
            "auditor_std",
            first_name="Ana",
            last_name="Contadora",
            email="ana@hotel.cl",
            password="ClaveFuerte!2026",
        )

    def test_non_superadmin_is_blocked_from_user_management_only(self):
        # Anonymous users redirect to login
        self.assertRedirects(
            self.client.get(reverse("users_manage")),
            f"{reverse('login')}?next={reverse('users_manage')}",
        )

        # Standard user has access to general app + Configuración + Vaciar BD Pruebas,
        # and only Gestión de Usuarios (+ Admin DB) is restricted to superadmin
        self.client.force_login(self.standard)
        dash = self.client.get(reverse("dashboard"))
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, reverse("settings"))
        self.assertNotContains(dash, reverse("users_manage"))
        self.assertNotContains(dash, "Base de Datos (Admin)")
        self.assertEqual(self.client.get(reverse("settings")).status_code, 200)

        upload_clp = self.client.get(reverse("clp_upload"))
        self.assertContains(upload_clp, "Vaciar BD Pruebas")
        self.assertRedirects(self.client.post(reverse("clear_test_db"), {"next": "clp_upload"}), reverse("clp_upload"))

        self.assertRedirects(self.client.get(reverse("users_manage")), reverse("dashboard"))
        self.assertRedirects(
            self.client.post(
                reverse("users_manage"),
                {
                    "username": "no_debe_crearse",
                    "role": "STANDARD",
                    "password": "OtraClave!2026",
                    "confirm_password": "OtraClave!2026",
                },
            ),
            reverse("dashboard"),
        )
        self.assertRedirects(
            self.client.post(reverse("user_edit", args=[self.superadmin.id]), {"role": "STANDARD"}),
            reverse("dashboard"),
        )
        self.assertRedirects(
            self.client.post(reverse("user_soft_delete", args=[self.superadmin.id]), {"reason": "x"}),
            reverse("dashboard"),
        )
        self.assertRedirects(
            self.client.post(reverse("user_restore", args=[self.superadmin.id])),
            reverse("dashboard"),
        )
        self.assertFalse(User.objects.filter(username="no_debe_crearse").exists())
        self.superadmin.refresh_from_db()
        self.assertTrue(self.superadmin.is_active)

    def test_superadmin_creates_edits_soft_deletes_and_restores_users(self):
        self.client.force_login(self.superadmin)
        page = self.client.get(reverse("users_manage"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, reverse("users_manage"))
        self.assertContains(page, "Base de Datos (Admin)")
        self.assertContains(page, "auditor_std")

        # 1) Create a new standard user and another superadmin
        resp = self.client.post(
            reverse("users_manage"),
            {
                "username": "jperez",
                "first_name": "Juan",
                "last_name": "Pérez",
                "email": "jperez@hotel.cl",
                "role": "STANDARD",
                "password": "ClaveSegura!2026",
                "confirm_password": "ClaveSegura!2026",
            },
        )
        self.assertRedirects(resp, reverse("users_manage"))
        created = User.objects.get(username="jperez")
        self.assertTrue(created.is_active)
        self.assertFalse(created.is_superuser)
        self.assertFalse(created.is_staff)
        self.assertTrue(created.account_status)
        self.assertFalse(created.account_status.is_soft_deleted)
        self.assertEqual(created.account_status.created_by, self.superadmin)
        self.assertTrue(
            UserAuditLog.objects.filter(target_user=created, action=UserAuditLog.ACTION_CREATED, actor=self.superadmin).exists()
        )

        # 2) Edit profile + reset password -> user logs in with new password
        edit_resp = self.client.post(
            reverse("user_edit", args=[created.id]),
            {
                "first_name": "Juan Carlos",
                "last_name": "Pérez",
                "email": "jcperez@hotel.cl",
                "role": "STANDARD",
                "new_password": "NuevaClave!9876",
                "confirm_new_password": "NuevaClave!9876",
            },
        )
        self.assertRedirects(edit_resp, reverse("users_manage"))
        created.refresh_from_db()
        self.assertEqual(created.first_name, "Juan Carlos")
        self.assertEqual(created.email, "jcperez@hotel.cl")
        self.assertTrue(created.check_password("NuevaClave!9876"))
        self.assertTrue(UserAuditLog.objects.filter(target_user=created, action=UserAuditLog.ACTION_UPDATED).exists())
        self.assertTrue(UserAuditLog.objects.filter(target_user=created, action=UserAuditLog.ACTION_PASSWORD_RESET).exists())

        # Validation failure re-renders with modal flag instead of creating user
        bad_resp = self.client.post(
            reverse("users_manage"),
            {
                "username": "malclave",
                "first_name": "Mal",
                "last_name": "Clave",
                "email": "mal@hotel.cl",
                "role": "STANDARD",
                "password": "ClaveSegura!2026",
                "confirm_password": "OtraClave!2026",
            },
        )
        self.assertEqual(bad_resp.status_code, 200)
        self.assertContains(bad_resp, 'data-open-create="1"')
        self.assertFalse(User.objects.filter(username="malclave").exists())

        # Create a ReconciliationProcess owned by `created` to prove soft delete preserves FK history
        proc = ReconciliationProcess.objects.create(user=created, process_currency="CLP")

        # 3) Active session exists for `created`; soft delete blocks login and invalidates session
        std_client = self.client.__class__()
        std_client.force_login(created)
        self.assertEqual(std_client.get(reverse("dashboard")).status_code, 200)

        del_resp = self.client.post(
            reverse("user_soft_delete", args=[created.id]),
            {"reason": "Término de contrato"},
        )
        self.assertRedirects(del_resp, reverse("users_manage"))
        created.refresh_from_db()
        status_obj = UserAccountStatus.objects.get(user=created)
        self.assertFalse(created.is_active)
        self.assertTrue(status_obj.is_soft_deleted)
        self.assertEqual(status_obj.deleted_by, self.superadmin)
        self.assertIsNotNone(status_obj.deleted_at)
        self.assertEqual(status_obj.delete_reason, "Término de contrato")
        self.assertTrue(ReconciliationProcess.objects.filter(pk=proc.pk, user=created).exists())
        self.assertTrue(UserAuditLog.objects.filter(target_user=created, action=UserAuditLog.ACTION_SOFT_DELETED).exists())
        # Existing session of the soft-deleted user was invalidated immediately
        self.assertEqual(std_client.get(reverse("dashboard")).status_code, 302)

        # Login attempt fails while soft-deleted
        guest_client = self.client.__class__()
        guest_client.post(reverse("login"), {"username": "jperez", "password": "NuevaClave!9876"})
        self.assertEqual(guest_client.get(reverse("dashboard")).status_code, 302)

        # Filters work cleanly in superadmin UI
        deleted_only = self.client.get(reverse("users_manage"), {"estado": "DELETED", "q": "jperez"})
        self.assertIn(created, list(deleted_only.context["user_rows"]))
        self.assertContains(deleted_only, "Eliminado (Soft delete)")
        self.assertContains(deleted_only, "Término de contrato")
        self.assertContains(deleted_only, reverse("user_restore", args=[created.id]))
        active_only = self.client.get(reverse("users_manage"), {"estado": "ACTIVE"})
        self.assertNotIn(created, list(active_only.context["user_rows"]))
        self.assertNotContains(active_only, reverse("user_restore", args=[created.id]))

        # 4) Guardrails: cannot delete self or demote last active superadmin
        self.client.post(reverse("user_soft_delete", args=[self.superadmin.id]), {"reason": "auto"})
        self.superadmin.refresh_from_db()
        self.assertTrue(self.superadmin.is_active)
        self.client.post(reverse("user_edit", args=[self.superadmin.id]), {"role": "STANDARD"})
        self.superadmin.refresh_from_db()
        self.assertTrue(self.superadmin.is_superuser)

        # 5) Restore soft-deleted user and promote to superadmin -> login & /usuarios/ access work
        res_resp = self.client.post(
            reverse("user_restore", args=[created.id]),
            {"reason": "Reingreso aprobado"},
        )
        self.assertRedirects(res_resp, reverse("users_manage"))
        created.refresh_from_db()
        status_obj.refresh_from_db()
        self.assertTrue(created.is_active)
        self.assertFalse(status_obj.is_soft_deleted)
        self.assertEqual(status_obj.restored_by, self.superadmin)
        self.assertIsNotNone(status_obj.restored_at)
        self.assertTrue(UserAuditLog.objects.filter(target_user=created, action=UserAuditLog.ACTION_RESTORED).exists())

        self.client.post(
            reverse("user_edit", args=[created.id]),
            {
                "first_name": "Juan Carlos",
                "last_name": "Pérez",
                "email": "jcperez@hotel.cl",
                "role": "SUPERADMIN",
            },
        )
        created.refresh_from_db()
        self.assertTrue(created.is_superuser)
        self.assertTrue(created.is_staff)

        guest_client.post(reverse("login"), {"username": "jperez", "password": "NuevaClave!9876"})
        self.assertEqual(guest_client.get(reverse("dashboard")).status_code, 200)
        self.assertEqual(guest_client.get(reverse("users_manage")).status_code, 200)


class FullErpIngestionAndCumulativeUploadTests(TestCase):
    """
    Verifies that ALL valid movement rows from a Borderó ERP file (USD cards, CLP cards,
    cash, room charges HOA, taxes IVA including 1-digit shifted Valor rows, restaurant/bar
    charges, and same-second duplicate room charges) are persisted in ErpTransaction, and
    that cumulative multi-month ERP uploads deduplicate cleanly while preserving historical
    closings, manual edits, and soft-deletes.
    """

    def setUp(self):
        self.user = User.objects.create_user("auditor_full_erp", password="ClaveFuerte!2026")
        self.client.force_login(self.user)

    def test_all_bordero_movements_stored_including_shifted_and_same_second_rows(self):
        # build_clp_files() contains:
        # - 1 CNBQ food charge (row 6: 20900)
        # - 8 CLP card payments (MC$ + RCO)
        # - 1 USD card payment (VUS$: -107984)
        # - 1 Cash payment (E$: -50000)
        # Plus we add a 1-digit shifted IVA row (col I=None, col J=0, col K="UH: 822", col L="05/09/2026")
        # and two identical same-second CNBQ charges in the same room.
        header = {
            "A": "Hab", "B": "Tipo Hab", "C": "Reserva", "D": "Cuenta", "E": "Cod. Deb.", "F": "Descripción",
            "H": "Factura", "I": "Valor", "J": "Documento", "K": "Fecha", "L": "Hora", "M": "Usuario", "N": "Designación",
        }
        rows = {
            1: {"A": "HOTELERA SAN FRANCISCO S.A"},
            2: {"A": "Cargos de Débitos y Créditos"},
            4: {"A": "Periodo de:", "B": "04/09/2026", "C": "hasta", "D": "07/09/2026"},
            5: header,
            # Standard non-card charge
            6: {"A": 1002, "D": 1785942, "E": "HOA", "F": "Hospedaje Automático", "I": 117985, "J": "UH: 1002", "K": "05/09/2026", "L": "02:30:10", "M": "NCAMPOS", "N": "CARLOS SOTO"},
            # Shifted 1-digit Valor=0 row (I is empty, J=0, K=Doc, L=Fecha, M=Hora, N=Usuario, O=Designación)
            7: {"A": 1002, "D": 1785942, "E": "IVA", "F": "IVA Débito Fiscal", "G": 259901, "J": 0, "K": "UH: 1002", "L": "05/09/2026", "M": "02:30:11", "N": "NCAMPOS", "O": "CARLOS SOTO"},
            # Two identical same-second CNBQ charges in the same room (2 guests sharing room 1103)
            8: {"A": 1103, "D": 1785999, "E": "CNBQ", "F": "1/2 Pension Alimentos", "I": 22900, "J": "UH: 1103", "K": "05/09/2026", "L": "01:12:11", "M": "NCAMPOS", "N": "HUESPED UNO"},
            9: {"A": 1103, "D": 1785999, "E": "CNBQ", "F": "1/2 Pension Alimentos", "I": 22900, "J": "UH: 1103", "K": "05/09/2026", "L": "01:12:11", "M": "NCAMPOS", "N": "HUESPED DOS"},
            10: {"A": "Total VHF:", "B": 163785},
        }
        for offset, (hab, cuenta, cod, desc, factura, valor, documento, fecha, hora, usuario, huesped) in enumerate(BORDERO_PAYMENTS):
            rows[11 + offset] = {
                "A": hab, "D": cuenta, "E": cod, "F": desc, "G": factura, "I": valor,
                "J": documento, "K": fecha, "L": hora, "M": usuario, "N": huesped,
            }

        files = build_clp_files()
        files["erp_file"] = _xlsx("BORDERO COMPLETO.xlsx", rows)
        preview = calculate_clp_preview(**files)
        # In-memory CLP preview only cross-matches the 8 CLP card rows
        self.assertEqual(len(preview["erp_transactions"]), 8)

        process = commit_clp_preview_to_database(self.user, preview)
        # Database stores ALL 14 valid movements (8 CLP cards + 1 USD card + 1 E$ cash + 1 HOA + 1 shifted IVA + 2 same-second CNBQ)
        self.assertEqual(ErpTransaction.objects.count(), 14)
        self.assertTrue(ErpTransaction.objects.filter(payment_code="HOA", payment_description="Hospedaje Automático").exists())
        self.assertTrue(ErpTransaction.objects.filter(payment_code="IVA", payment_description="IVA Débito Fiscal", amount=Decimal("0.00")).exists())
        self.assertTrue(ErpTransaction.objects.filter(payment_code="E$", payment_description="Efectivo $", amount=Decimal("-50000.00")).exists())
        self.assertTrue(ErpTransaction.objects.filter(payment_code="VUS$", source_currency="USD", amount=Decimal("107984.00")).exists())
        self.assertEqual(ErpTransaction.objects.filter(payment_code="CNBQ", account_code="1785999").count(), 2)

        # Consolidated ERP table renders payment_description and supports ALL_CARDS / RCO / NON_CARD filters
        resp_all = self.client.get(reverse("consolidated_erp"))
        self.assertContains(resp_all, "Hospedaje Automático")
        self.assertContains(resp_all, "IVA Débito Fiscal")
        self.assertContains(resp_all, "Red Compra")

        resp_cards = self.client.get(reverse("consolidated_erp"), {"tarjeta": "ALL_CARDS"})
        self.assertEqual(resp_cards.context["kpis"]["total"], 9)  # 8 CLP cards + 1 USD card

        resp_rco = self.client.get(reverse("consolidated_erp"), {"tarjeta": "RCO"})
        self.assertEqual(resp_rco.context["kpis"]["total"], 7)

        resp_non_card = self.client.get(reverse("consolidated_erp"), {"tarjeta": "NON_CARD"})
        self.assertEqual(resp_non_card.context["kpis"]["total"], 5)  # HOA + IVA + 2 CNBQ + E$

    def test_cumulative_multi_month_upload_preserves_closings_edits_and_reconciles_pending(self):
        # Month 1 (September): commit initial CLP closing
        sep_files = build_clp_files()
        sep_process = commit_clp_preview_to_database(self.user, calculate_clp_preview(**sep_files))
        initial_total = ErpTransaction.all_objects.count()
        self.assertEqual(initial_total, 11)  # 8 CLP cards + 1 CNBQ + 1 VUS$ + 1 E$

        # Reconciled row in Month 1 (405544) belongs to sep_process with reconciled=True
        rec_row = ErpTransaction.objects.get(authorization_code="405544")
        self.assertTrue(rec_row.reconciled)
        self.assertEqual(rec_row.process_id, sep_process.id)

        # Manually edit 007626 in Month 1 so it becomes reconciled & corrected
        case_7626 = sep_process.cross_match_results.get(authorization_code="007626")
        edit_row(case_7626.erp_transaction, {"amount": "115204"}, self.user, match=case_7626, reason="Ajuste mes 1")

        # Soft-delete the E$ cash row in Month 1
        cash_row = ErpTransaction.objects.get(payment_code="E$")
        cash_row.is_deleted = True
        cash_row.save()

        # Also add an unreconciled September card sale (auth 909090) that wasn't in September's Transbank settlement
        sep_plus_pending = BORDERO_PAYMENTS + [
            (501, 1789001, "MC$", "Master Card $", 54001, -75000, 909090, "07/09/2026", "21:00:00", "CDAZA", "PEDRO PENDIENTE"),
        ]
        sep_files_with_pending = build_clp_files()
        sep_files_with_pending["erp_file"] = build_bordero(sep_plus_pending)
        commit_clp_preview_to_database(self.user, calculate_clp_preview(**sep_files_with_pending))
        pending_row = ErpTransaction.objects.get(authorization_code="909090")
        self.assertFalse(pending_row.reconciled)
        self.assertEqual(pending_row.process_id, sep_process.id)

        # Month 2 (October): User uploads a CUMULATIVE Borderó containing all September rows + new October rows,
        # and a new October Transbank settlement that settles both 909090 (from Sep) and 808080 (from Oct).
        cumulative_payments = sep_plus_pending + [
            (701, 1795001, "MC$", "Master Card $", 61001, -92000, 808080, "15/10/2026", "14:20:00", "SCORREA", "CLIENTE OCTUBRE"),
            (701, 1795001, "HOA", "Hospedaje Automático", None, 92000, "UH: 701", "15/10/2026", "02:10:00", "NCAMPOS", "CLIENTE OCTUBRE"),
        ]
        oct_credit_sales = [
            ("07 septiembre 2026", "HOTEL PLAZA SAN FRANCISCO", 75000, "Venta $", "S/C", 75000, 1500, 285, "5525 61** **** 5410", "909090"),
            ("15 octubre 2026", "HOTEL PLAZA SAN FRANCISCO", 92000, "Venta $", "S/C", 92000, 1840, 350, "5525 61** **** 5410", "808080"),
        ]
        oct_files = {
            "erp_file": build_bordero(cumulative_payments),
            "bank_statement_file": build_cartola([
                ("16/10/2026", "Pago: Abono Comer. Tc Tbk 0966893109", _deposit(oct_credit_sales)),
            ]),
            "transbank_credit_file": build_tbk_settlement("CREDITOS OCT.xlsx", "Crédito", oct_credit_sales),
            "transbank_debit_file": build_tbk_settlement("DEBITOS OCT.xlsx", "Débito", []),
            "transbank_prepaid_file": build_tbk_settlement("PREPAGO OCT.xlsx", "Prepago", []),
        }
        oct_process = commit_clp_preview_to_database(self.user, calculate_clp_preview(**oct_files))
        self.assertNotEqual(oct_process.id, sep_process.id)

        # 1) Total rows in DB increased by ONLY the 2 new October rows (12 -> 14), zero duplicates!
        self.assertEqual(ErpTransaction.all_objects.count(), 14)

        # 2) Historical reconciled row 405544 kept reconciled=True AND stayed in sep_process
        rec_row.refresh_from_db()
        self.assertTrue(rec_row.reconciled)
        self.assertEqual(rec_row.process_id, sep_process.id)

        # 3) Edited row 7626 kept its corrected amount 115204.00 and stayed in sep_process
        edited_row = ErpTransaction.objects.get(authorization_code="7626")
        self.assertEqual(edited_row.amount, Decimal("115204.00"))
        self.assertTrue(edited_row.reconciled)
        self.assertEqual(edited_row.process_id, sep_process.id)

        # 4) Soft-deleted E$ row stayed deleted
        cash_row.refresh_from_db()
        self.assertTrue(cash_row.is_deleted)

        # 5) Previously pending row 909090 is now reconciled in oct_process!
        pending_row.refresh_from_db()
        self.assertTrue(pending_row.reconciled)
        self.assertEqual(pending_row.process_id, oct_process.id)

        # 6) New October rows (808080 and HOA) belong to oct_process
        oct_card = ErpTransaction.objects.get(authorization_code="808080")
        self.assertTrue(oct_card.reconciled)
        self.assertEqual(oct_card.process_id, oct_process.id)
        oct_hoa = ErpTransaction.objects.get(payment_code="HOA", account_code="1795001")
        self.assertEqual(oct_hoa.process_id, oct_process.id)


class UnifiedPaginationTests(TestCase):
    """
    Verifies that Consultas tables (consolidated views, closing detail, closings list, audit trail)
    and Step 2 loaded source tables share the same page-size selector + windowed numbered pagination UI.
    """

    def setUp(self):
        self.user = User.objects.create_user("auditor_pag", password="ClaveFuerte!2026")
        self.client.force_login(self.user)
        self.process = commit_clp_preview_to_database(self.user, calculate_clp_preview(**build_clp_files()))
        # Ensure we have > 30 ERP rows so 10/page produces 4+ pages and 30/page produces 2 pages
        extra = [
            ErpTransaction(
                process=self.process,
                account_code=f"9900{idx:03d}",
                document_number=f"9900{idx:03d}",
                authorization_code=f"AUTH{idx:03d}",
                payment_code="HOA",
                payment_description="Hospedaje Automático",
                amount=Decimal("10000.00"),
                source_currency="CLP",
                reconciled=False,
                origin=ErpTransaction.ORIGIN_EXCEL,
                source_key=f"pag-test-key-{idx}",
            )
            for idx in range(25)
        ]
        ErpTransaction.objects.bulk_create(extra)

    def test_build_page_items_window_and_ellipses(self):
        from .services.tables_viewer import build_page_items

        self.assertEqual(build_page_items(1, 1), [])
        two_pages = build_page_items(1, 2)
        self.assertEqual([i["number"] for i in two_pages], [1, 2])
        self.assertTrue(two_pages[0]["active"])

        mid = build_page_items(5, 10)
        # Expect: 1, ..., 4, 5(active), 6, ..., 10
        self.assertEqual(
            [(i["kind"], i.get("number"), i.get("active", False)) for i in mid],
            [
                ("page", 1, False),
                ("ellipsis", None, False),
                ("page", 4, False),
                ("page", 5, True),
                ("page", 6, False),
                ("ellipsis", None, False),
                ("page", 10, False),
            ],
        )

    def test_consultas_pagination_bar_supports_page_size_and_numbered_links(self):
        # Total rows = 11 (from build_clp_files) + 25 = 36 rows
        resp = self.client.get(reverse("consolidated_erp"), {"por_pagina": "10", "page": "2"})
        self.assertEqual(resp.status_code, 200)
        page = resp.context["page"]
        self.assertEqual(page.paginator.per_page, 10)
        self.assertEqual(page.paginator.num_pages, 4)
        self.assertEqual(len(resp.context["erp_rows"]), 10)
        self.assertContains(resp, "voucher-pagination-bar")
        self.assertContains(resp, "voucher-page-size-select")
        self.assertContains(resp, '<option value="10" selected>Página 10</option>', html=True)
        self.assertContains(resp, "por_pagina=10&page=1")
        self.assertContains(resp, "por_pagina=10&page=3")
        self.assertContains(resp, "por_pagina=10&page=4")

        # Invalid por_pagina falls back safely to default 25
        resp_invalid = self.client.get(reverse("consolidated_erp"), {"por_pagina": "9999"})
        self.assertEqual(resp_invalid.context["page"].paginator.per_page, 25)
        self.assertContains(resp_invalid, '<option value="25" selected>Página 25</option>', html=True)

        # Single-page result with por_pagina=50 STILL shows the page-size dropdown
        resp_single = self.client.get(reverse("consolidated_erp"), {"por_pagina": "50"})
        self.assertEqual(resp_single.context["page"].paginator.num_pages, 1)
        self.assertContains(resp_single, '<option value="50" selected>Página 50</option>', html=True)

        # Same behavior on closing_detail (?vista=erp), closings_list, and audit_trail
        detail_resp = self.client.get(
            reverse("closing_detail", args=[self.process.id]),
            {"vista": "erp", "cruce": "solo_erp", "por_pagina": "10"},
        )
        self.assertEqual(detail_resp.status_code, 200)
        self.assertEqual(detail_resp.context["page"].paginator.per_page, 10)
        self.assertContains(detail_resp, "voucher-page-size-select")
        self.assertContains(detail_resp, "js-db-page-size-form")
        self.assertContains(detail_resp, '<input type="hidden" name="vista" value="erp">', html=True)
        self.assertContains(detail_resp, '<input type="hidden" name="cruce" value="solo_erp">', html=True)
        self.assertContains(detail_resp, "app.js?v=6")

        closings_resp = self.client.get(reverse("closings_list"))
        self.assertEqual(closings_resp.status_code, 200)
        self.assertEqual(closings_resp.context["page"].paginator.per_page, 25)
        self.assertContains(closings_resp, '<option value="25" selected>Página 25</option>', html=True)

    def test_preview_source_tables_render_page_size_selector(self):
        clp_preview = calculate_clp_preview(**build_clp_files())
        session = self.client.session
        session["clp_reconciliation_preview"] = clp_preview
        session["usd_reconciliation_preview"] = calculate_usd_preview(**_build_usd_files())
        session.save()

        for route_name in ("clp_preview", "usd_preview"):
            resp = self.client.get(reverse(route_name))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, 'id="srcPageSizeSelect"')
            self.assertContains(resp, '<option value="25" selected>Página 25</option>', html=True)
            self.assertContains(resp, '<option value="ALL">Todas</option>', html=True)
            self.assertContains(resp, "source_tables_viewer.js?v=5")

    def test_db_pagination_bar_unlocalizes_page_numbers_over_1000(self):
        from django.template.loader import render_to_string
        from .services.tables_viewer import _paginate

        page = _paginate(list(range(20454)), 999, per_page=10, base_default=25)
        html = render_to_string(
            "partials/db_pagination_bar.html",
            {"page": page, "query_string": "por_pagina=10", "aria_label": "Paginación de movimientos ERP"},
        )
        self.assertIn('href="?por_pagina=10&page=1000"', html)
        self.assertIn('href="?por_pagina=10&page=2046"', html)
        self.assertNotIn("\xa0", html.split("<nav")[1])


