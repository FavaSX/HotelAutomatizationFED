import io
from datetime import date
from decimal import Decimal

import openpyxl
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import (
    BankStatementMovement,
    CrossMatchResult,
    ErpTransaction,
    ReconciliationProcess,
    SystemConfiguration,
    TransbankTransaction,
)
from .services import calculate_clp_preview, commit_clp_preview_to_database, detect_and_align_clp_files
from .services.clp_reconciliation import get_clp_tolerance
from .services.column_detector import normalize_auth_code, safe_date
from .services.clp_report_exporter import generate_committed_clp_excel_bytes


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

    def test_tolerance_is_kept_between_500_and_1000(self):
        config = SystemConfiguration.objects.first()
        self.assertEqual(get_clp_tolerance(), Decimal("1000"))
        config.clp_rounding_tolerance = Decimal("5")
        config.save()
        self.assertEqual(get_clp_tolerance(), Decimal("500"))
        config.clp_rounding_tolerance = Decimal("5000")
        config.save()
        self.assertEqual(get_clp_tolerance(), Decimal("1000"))


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
        self.assertEqual(voucher["guest_name"], "RODRIGO ISLA")
        self.assertEqual(voucher["card_code"], "MC")

    def test_leading_zeros_reversals_and_tolerance(self):
        matched = _by_code(self.preview, "matched_vouchers")
        self.assertEqual(matched["003098"]["difference"], -1)
        self.assertEqual(len(matched["823431"]["erp_indices"]), 3)
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
        self.assertEqual(process.bank_validations.filter(is_matched=True).count(), 3)
        self.assertEqual(process.cross_match_results.count(), len(CREDIT_SALES + DEBIT_SALES + PREPAID_SALES))
        counts = (TransbankTransaction.objects.count(), ErpTransaction.objects.count(),
                  CrossMatchResult.objects.count(), BankStatementMovement.objects.count())

        orphan = process.cross_match_results.get(authorization_code="F0FUAZ")
        self.assertEqual(orphan.document_number, "0FOFUAZ")

        workbook = openpyxl.load_workbook(generate_committed_clp_excel_bytes(process.id))
        self.assertEqual(workbook.sheetnames, ["Resumen Pesos", "Vouchers Cuadrados", "Reporte de Excepciones"])
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


class ClpViewsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("auditor", password="x")
        self.client.force_login(self.user)

    def test_detect_files_aligns_the_five_roles(self):
        files = build_clp_files()
        report = detect_and_align_clp_files(list(files.values()))
        self.assertTrue(report["all_matched"])
        roles = {r["role_key"]: r["file_name"] for r in report["aligned_roles"]}
        self.assertEqual(roles["tbk_debit"], "DEBITOS TBK 08-09.xlsx")
        self.assertEqual(roles["erp"], "BORDERO 04-09 A 07-09.xlsx")

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
        self.assertContains(response, "Reporte de Excepciones")

        response = self.client.post(reverse("clp_commit"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(self.client.get(data["export_url"]).status_code, 200)

    def test_usd_upload_page_still_renders(self):
        self.assertEqual(self.client.get(reverse("usd_upload")).status_code, 200)
