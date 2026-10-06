from datetime import date
from io import BytesIO

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from .conciliacion_pesos import (
    conciliar_tarjetas, leer_bordero, leer_cartola, leer_tbk, normalizar_codigo, verificar_abonos,
)


def _venta(codigo, monto, fecha=date(2026, 9, 5), cuota="S/C", tipo="Venta", medio="Crédito"):
    return {
        "medio": medio, "fecha_venta": fecha, "tipo_movimiento": tipo, "comercio": "47195679",
        "local": "RESTAURANT BRISTOL", "monto_original": monto, "tipo_venta": "Venta $",
        "n_cuota": cuota, "monto_abono": monto, "comision": 0, "total_abono": monto, "codigo": codigo,
    }


def _pago(documento, valor, fecha=date(2026, 9, 5), cod="V$"):
    return {
        "hab": "", "reserva": "", "cod_deb": cod, "descripcion": "Visa $", "valor": valor,
        "documento": documento, "fecha": fecha, "usuario": "CDAZA", "designacion": "",
    }


def _tbk(*ventas):
    return [{"medio": "Crédito", "detalle": list(ventas)}]


def _xlsx(filas):
    wb = Workbook()
    ws = wb.active
    for fila in filas:
        ws.append(fila)
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


class NormalizarCodigoTests(TestCase):
    def test_numerico_recupera_ceros(self):
        self.assertEqual(normalizar_codigo(27284), "027284")
        self.assertEqual(normalizar_codigo(5431.0), "005431")

    def test_vacios(self):
        self.assertEqual(normalizar_codigo(float("nan")), "")
        self.assertEqual(normalizar_codigo(None), "")
        self.assertEqual(normalizar_codigo("-"), "")

    def test_alfanumerico_en_mayusculas(self):
        self.assertEqual(normalizar_codigo(" 05330i "), "05330I")


class ConciliarTarjetasTests(TestCase):
    def tipos(self, excepciones):
        return [e["Tipo de error"] for e in excepciones]

    def test_cuadra_dentro_de_tolerancia(self):
        ok, exc = conciliar_tarjetas(_tbk(_venta("706537", 15000)), [_pago("706537", 15500)])
        self.assertEqual(len(ok), 1)
        self.assertEqual(exc, [])

    def test_cero_antepuesto_por_el_erp_no_es_error(self):
        ok, exc = conciliar_tarjetas(_tbk(_venta("05330I", 30000)), [_pago("005330I", 30000)])
        self.assertEqual(len(ok), 1)

    def test_diferencia_de_monto(self):
        _, exc = conciliar_tarjetas(_tbk(_venta("007626", 115204)), [_pago("007626", 113700)])
        self.assertEqual(self.tipos(exc), ["Diferencia de monto"])
        self.assertEqual(exc[0]["Monto discrepante"], 1504)

    def test_letra_o_por_cero(self):
        _, exc = conciliar_tarjetas(_tbk(_venta("HCOVGZ", 33191)), [_pago("HC0VGZ", 33191)])
        self.assertEqual(self.tipos(exc), ["Posible error de tipeo"])

    def test_no_registrada_en_erp(self):
        _, exc = conciliar_tarjetas(_tbk(_venta("839049", 161916)), [_pago("111111", 5000)])
        self.assertIn("No registrada en ERP", self.tipos(exc))

    def test_cuota_posterior(self):
        _, exc = conciliar_tarjetas(_tbk(_venta("626135", 1354387, date(2026, 7, 4), "3/3")), [_pago("111111", 5000)])
        self.assertEqual(self.tipos(exc), ["Cuota de venta anterior"])

    def test_fuera_del_periodo_del_bordero(self):
        _, exc = conciliar_tarjetas(_tbk(_venta("070981", 8690, date(2026, 9, 3))), [_pago("111111", 5000, date(2026, 9, 4))])
        self.assertEqual(self.tipos(exc), ["Fuera del período del Borderó"])

    def test_pago_erp_sin_venta_tbk(self):
        _, exc = conciliar_tarjetas(_tbk(_venta("706537", 15000)), [_pago("706537", 15000), _pago("999999", 7000)])
        self.assertEqual(self.tipos(exc), ["Sin abono Transbank en este período"])


class LectoresTests(TestCase):
    def test_leer_tbk_con_encabezado_en_dos_filas(self):
        vacio = [None] * 26
        archivo = _xlsx([
            [None, "Total ventas (+)", None, 15000],
            [None, "Comisión Transbank (-)", None, 300],
            [None, "IVA de comisión Transbank (-)", None, 57],
            [None, "Total abono", None, 14643],
            [None, "Fecha de venta", "Tipo de movimiento", "Código de comercio", "Nombre local", "Medio de pago", "Ventas"],
            [None, None, None, None, None, None, "Monto original de venta (+)", "Monto vuelto", "Tipo de venta/cuota",
             "N° de cuota", "Monto venta válido para abono (+)", "Comisión Transbank (-)", "IVA Comisión Transbank (-)",
             "Total abono (=)", None, None, None, None, None, None, None, None, None, None, "N° de tarjeta",
             "Código de autorización de venta"],
            [None, "04 septiembre 2026", "Venta", "47195679", "RESTAURANT BRISTOL, AVENIDA", "Crédito", 15000, 0,
             "Venta $", "S/C", 15000, 300, 57, 14643] + vacio[:10] + ["4974 02** **** 1114", "706537"],
        ])
        tbk = leer_tbk(archivo, "Crédito")
        self.assertEqual(tbk["total_abono"], 14643)
        self.assertEqual(len(tbk["detalle"]), 1)
        venta = tbk["detalle"][0]
        self.assertEqual(venta["codigo"], "706537")
        self.assertEqual(venta["fecha_venta"], date(2026, 9, 4))
        self.assertEqual(venta["local"], "RESTAURANT BRISTOL")

    def test_leer_bordero_filtra_pagos_con_tarjeta(self):
        archivo = _xlsx([
            [None, None, None, None, None, None, "HOTELERA SAN FRANCISCO S.A"],
            ["Hab", "Tipo Hab", "Reserva", "Cuenta", "Cod. Deb.", "Descripción", None, "Factura", "Valor", "Documento", "Fecha", "Hora", "Usuario", "Designación"],
            [1002, "S1K", 4622294, 1785942, "CNBQ", "1/2 Pension Alimentos", None, None, 20900, "UH: 1002", "07/09/2026", "00:57:38", "CDAZA", "CATALINA LAGOS"],
            [None, None, None, 1786633, "V$", "Visa $", None, None, -15000, 706537, "04/09/2026", None, "JFIGUEROA", None],
            [None, None, None, None, None, "Total:", None, 125400],
        ])
        pagos = leer_bordero(archivo)
        self.assertEqual(len(pagos), 1)
        self.assertEqual(pagos[0]["valor"], 15000)
        self.assertEqual(pagos[0]["documento"], "706537")

    def test_verificar_abono_en_cartola(self):
        archivo = _xlsx([
            [None, "Fecha", None, "Descripción", None, "Canal o Sucursal", "Nro. Docto.", "Cargos (CLP)", "Abonos (CLP)", "Saldo (CLP)"],
            [None, "08/09/2026", None, "Pago: Abono Comer. Tc Tbk 0966893109", None, "Oficina Central", None, None, 2597371, 3628585],
        ])
        movimientos = leer_cartola(archivo)
        tbk = [{"medio": "Crédito", "total_ventas": 2663840, "comision": 55856, "iva_comision": 10613, "total_abono": 2597371}]
        resultado = verificar_abonos(tbk, movimientos)
        self.assertTrue(resultado[0]["encontrado"])
        self.assertEqual(resultado[0]["fecha"], date(2026, 9, 8))

    def test_archivo_equivocado_da_error_claro(self):
        with self.assertRaisesMessage(ValueError, "Cod. Deb."):
            leer_bordero(_xlsx([["cualquier", "cosa"]]))


class VistaPesosTests(TestCase):
    def setUp(self):
        User.objects.create_user(username="contador@hotel.cl", password="clave-segura-123")
        self.client.login(username="contador@hotel.cl", password="clave-segura-123")

    def test_faltan_archivos(self):
        archivo = SimpleUploadedFile("credito.xlsx", b"x")
        resp = self.client.post(reverse("PR_CLP"), {"clp_credito": archivo})
        self.assertContains(resp, "Faltan archivos")
