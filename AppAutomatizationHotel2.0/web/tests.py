import io
from datetime import date
from decimal import Decimal

import openpyxl
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import (
    BankStatementMovement,
    CardOperatorTransaction,
    CrossMatchResult,
    ErpTransaction,
    ReconciliationProcess,
    SystemConfiguration,
    TransbankTransaction,
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

    def test_only_superuser_can_access(self):
        self.client.force_login(self.auditor)
        self.assertRedirects(self.client.get(reverse("settings")), reverse("dashboard"))
        self._post()
        self.assertEqual(SystemConfiguration.get_solo().clp_rounding_tolerance, Decimal("1000"))

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
