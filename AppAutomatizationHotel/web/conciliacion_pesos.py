"""Conciliación en Pesos (CLP) - Proceso 1.

Cruza las ventas de Transbank (Crédito, Débito, Prepago) contra los pagos con
tarjeta del Borderó del ERP y verifica que el Total abono de cada archivo TBK
aparezca en la cartola del banco.

Los Excel se leen tal cual vienen: los encabezados se ubican por su texto y no
por posición fija.
"""
import math
from datetime import date

import pandas as pd

TOLERANCIA_CLP = 1000

# Códigos de pago con tarjeta en pesos del Borderó (Cod. Deb.)
CODIGOS_TARJETA = {"V$", "MC$", "AME$", "RCO"}

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# Resultado del último proceso, leído por las vistas de descarga
# (mismo patrón que informacion_proceso en servicios_financieros.py).
resultado_pesos = {"conciliadas": [], "excepciones": []}


# ---------------------------------------------------------------- utilidades

def normalizar_codigo(valor):
    """Deja un código de autorización comparable: texto, mayúsculas y 6 dígitos si es numérico."""
    if valor is None or (isinstance(valor, float) and math.isnan(valor)) or pd.isna(valor):
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    texto = str(valor).strip().upper()
    if texto.endswith(".0") and texto[:-2].isdigit():
        texto = texto[:-2]
    if texto in ("", "-", "NAN"):
        return ""
    if texto.isdigit():
        texto = texto.zfill(6)
    return texto


def _clave(codigo):
    """Llave de cruce: el ERP antepone ceros a los códigos alfanuméricos (05330I -> 005330I)."""
    return codigo.lstrip("0")


def _clave_tipeo(codigo):
    """Variante tolerante a errores de tipeo: letra O digitada en vez de cero."""
    return _clave(codigo.replace("O", "0"))


def _numero(valor):
    """Convierte a float; devuelve 0.0 si está vacío o no es un número."""
    num = pd.to_numeric(valor, errors="coerce")
    if pd.isna(num):
        return 0.0
    return float(num)


def _fecha(valor):
    """Acepta '04 septiembre 2026', '04/09/2026' o un datetime. Devuelve date o None."""
    if valor is None or pd.isna(valor):
        return None
    if hasattr(valor, "date"):
        return valor.date()
    texto = str(valor).strip().lower()
    partes = texto.split()
    if len(partes) == 3 and partes[1] in MESES:
        try:
            return date(int(partes[2]), MESES[partes[1]], int(partes[0]))
        except ValueError:
            return None
    fecha = pd.to_datetime(texto, dayfirst=True, errors="coerce")
    return None if pd.isna(fecha) else fecha.date()


def _leer(archivo):
    if hasattr(archivo, "seek"):
        archivo.seek(0)
    return pd.read_excel(archivo, header=None)


def _buscar_fila(df, texto, columna=None):
    """Índice de la primera fila que contiene una celda igual a `texto`."""
    columnas = [columna] if columna is not None else df.columns
    for col in columnas:
        coincide = df[col].astype(str).str.strip() == texto
        if coincide.any():
            return coincide.idxmax()
    raise ValueError(f"No se encontró el encabezado '{texto}'.")


def _mapa_columnas(df, filas):
    """Etiqueta -> índice de columna, leyendo una o más filas de encabezado."""
    mapa = {}
    for fila in filas:
        for col, valor in df.loc[fila].items():
            if pd.notna(valor):
                mapa.setdefault(str(valor).strip(), col)
    return mapa


def _columna(mapa, prefijo, archivo):
    for etiqueta, col in mapa.items():
        if etiqueta.startswith(prefijo):
            return col
    raise ValueError(f"No se encontró la columna '{prefijo}' en {archivo}.")


# ------------------------------------------------------------------ lectores

def leer_tbk(archivo, medio):
    """Lee un reporte 'Abonos por día' de Transbank (Crédito, Débito o Prepago)."""
    df = _leer(archivo)
    nombre = f"TBK {medio}"

    def resumen(etiqueta):
        fila = _buscar_fila(df, etiqueta)
        valores = [v for v in df.loc[fila].tolist() if pd.notna(v) and str(v).strip() != etiqueta]
        return _numero(valores[0]) if valores else 0.0

    fila_enc = _buscar_fila(df, "Fecha de venta")
    mapa = _mapa_columnas(df, [fila_enc, fila_enc + 1])
    cols = {
        "fecha_venta": _columna(mapa, "Fecha de venta", nombre),
        "tipo_movimiento": _columna(mapa, "Tipo de movimiento", nombre),
        "comercio": _columna(mapa, "Código de comercio", nombre),
        "local": _columna(mapa, "Nombre local", nombre),
        "monto_original": _columna(mapa, "Monto original de venta", nombre),
        "tipo_venta": _columna(mapa, "Tipo de venta", nombre),
        "n_cuota": _columna(mapa, "N° de cuota", nombre),
        "monto_abono": _columna(mapa, "Monto venta válido para abono", nombre),
        "comision": _columna(mapa, "Comisión Transbank", nombre),
        "iva": _columna(mapa, "IVA Comisión Transbank", nombre),
        "total_abono": _columna(mapa, "Total abono (=)", nombre),
        "codigo": _columna(mapa, "Código de autorización de venta", nombre),
    }

    filas = []
    for _, fila in df.loc[fila_enc + 2:].iterrows():
        tipo = fila[cols["tipo_movimiento"]]
        if pd.isna(tipo) or str(tipo).strip() == "":
            continue
        filas.append({
            "medio": medio,
            "fecha_venta": _fecha(fila[cols["fecha_venta"]]),
            "tipo_movimiento": str(tipo).strip(),
            "comercio": str(fila[cols["comercio"]]).strip(),
            "local": str(fila[cols["local"]]).split(",")[0].strip(),
            "monto_original": _numero(fila[cols["monto_original"]]),
            "tipo_venta": str(fila[cols["tipo_venta"]]).strip(),
            "n_cuota": str(fila[cols["n_cuota"]]).strip(),
            "monto_abono": _numero(fila[cols["monto_abono"]]),
            "comision": _numero(fila[cols["comision"]]) + _numero(fila[cols["iva"]]),
            "total_abono": _numero(fila[cols["total_abono"]]),
            "codigo": normalizar_codigo(fila[cols["codigo"]]),
        })

    return {
        "medio": medio,
        "total_ventas": resumen("Total ventas (+)"),
        "comision": resumen("Comisión Transbank (-)"),
        "iva_comision": resumen("IVA de comisión Transbank (-)"),
        "total_abono": resumen("Total abono"),
        "detalle": filas,
    }


def leer_bordero(archivo):
    """Lee el Borderó y devuelve solo los pagos con tarjeta en pesos."""
    df = _leer(archivo)
    fila_enc = _buscar_fila(df, "Cod. Deb.")
    mapa = _mapa_columnas(df, [fila_enc])
    cols = {k: _columna(mapa, v, "el Borderó") for k, v in {
        "hab": "Hab", "reserva": "Reserva", "cod_deb": "Cod. Deb.", "descripcion": "Descripción",
        "valor": "Valor", "documento": "Documento", "fecha": "Fecha", "usuario": "Usuario",
        "designacion": "Designación",
    }.items()}

    pagos = []
    for _, fila in df.loc[fila_enc + 1:].iterrows():
        cod = str(fila[cols["cod_deb"]]).strip()
        if cod not in CODIGOS_TARJETA:
            continue
        pagos.append({
            "hab": "" if pd.isna(fila[cols["hab"]]) else str(fila[cols["hab"]]),
            "reserva": "" if pd.isna(fila[cols["reserva"]]) else str(fila[cols["reserva"]]),
            "cod_deb": cod,
            "descripcion": str(fila[cols["descripcion"]]).strip(),
            "valor": abs(_numero(fila[cols["valor"]])),
            "documento": normalizar_codigo(fila[cols["documento"]]),
            "fecha": _fecha(fila[cols["fecha"]]),
            "usuario": "" if pd.isna(fila[cols["usuario"]]) else str(fila[cols["usuario"]]),
            "designacion": "" if pd.isna(fila[cols["designacion"]]) else str(fila[cols["designacion"]]),
        })
    return pagos


def leer_cartola(archivo):
    """Lee los movimientos de la cartola del Banco de Chile."""
    df = _leer(archivo)
    fila_enc = _buscar_fila(df, "Abonos (CLP)")
    mapa = _mapa_columnas(df, [fila_enc])
    c_fecha = _columna(mapa, "Fecha", "la cartola")
    c_desc = _columna(mapa, "Descripción", "la cartola")
    c_abono = _columna(mapa, "Abonos", "la cartola")

    movimientos = []
    for _, fila in df.loc[fila_enc + 1:].iterrows():
        fecha = _fecha(fila[c_fecha])
        if fecha is None:
            continue
        movimientos.append({
            "fecha": fecha,
            "descripcion": "" if pd.isna(fila[c_desc]) else str(fila[c_desc]).strip(),
            "abono": _numero(fila[c_abono]),
        })
    return movimientos


# ------------------------------------------------------------------- cruces

def _excepcion(tipo, venta, monto_erp, accion, pago=None):
    monto_tbk = venta["monto_original"] if venta else 0
    return {
        "Tipo de error": tipo,
        "Fecha de origen": (venta or {}).get("fecha_venta") or (pago or {}).get("fecha"),
        "Documento/Código": (venta or {}).get("codigo") or (pago or {}).get("documento"),
        "Monto discrepante": round(monto_tbk - monto_erp),
        "Acción sugerida": accion,
        "Medio": (venta or {}).get("medio", (pago or {}).get("descripcion", "")),
        "Local": (venta or {}).get("local", ""),
        "Cuota": (venta or {}).get("n_cuota", ""),
        "Monto TBK": round(monto_tbk),
        "Monto ERP": round(monto_erp),
        "Doc. ERP": (pago or {}).get("documento", ""),
        "Usuario ERP": (pago or {}).get("usuario", ""),
    }


def _es_cuota_posterior(n_cuota):
    partes = n_cuota.split("/")
    return len(partes) == 2 and partes[0].isdigit() and int(partes[0]) > 1


def conciliar_tarjetas(tbk_list, pagos, tolerancia=TOLERANCIA_CLP):
    """Cruza cada venta TBK contra los pagos con tarjeta del Borderó."""
    por_codigo, por_tipeo = {}, {}
    for i, pago in enumerate(pagos):
        if pago["documento"]:
            por_codigo.setdefault(_clave(pago["documento"]), []).append(i)
            por_tipeo.setdefault(_clave_tipeo(pago["documento"]), []).append(i)
    usados = set()

    fechas_bordero = [p["fecha"] for p in pagos if p["fecha"]]
    inicio_bordero = min(fechas_bordero) if fechas_bordero else None

    conciliadas, excepciones = [], []
    ventas = [v for tbk in tbk_list for v in tbk["detalle"]]

    def elegir(indices, monto):
        libres = [i for i in indices if i not in usados] or indices
        return min(libres, key=lambda i: abs(pagos[i]["valor"] - monto))

    for venta in ventas:
        if venta["tipo_movimiento"].lower() != "venta":
            excepciones.append(_excepcion(
                "Anulación / Nota de crédito", venta, 0,
                "Verificar que la anulación esté reversada en el ERP"))
            continue

        codigo, monto = venta["codigo"], venta["monto_original"]
        if codigo and _clave(codigo) in por_codigo:
            i = elegir(por_codigo[_clave(codigo)], monto)
            usados.add(i)
            pago = pagos[i]
            if abs(monto - pago["valor"]) <= tolerancia:
                conciliadas.append({
                    "Fecha venta": venta["fecha_venta"], "Código": codigo, "Medio": venta["medio"],
                    "Local": venta["local"], "Cuota": venta["n_cuota"], "Monto TBK": round(monto),
                    "Monto ERP": round(pago["valor"]), "Diferencia": round(monto - pago["valor"]),
                    "Cod. Deb.": pago["cod_deb"], "Hab": pago["hab"], "Usuario ERP": pago["usuario"],
                })
            else:
                excepciones.append(_excepcion(
                    "Diferencia de monto", venta, pago["valor"],
                    "Revisar el monto digitado en el ERP o descuentos aplicados", pago))
            continue

        clave = _clave_tipeo(codigo)
        if clave and clave in por_tipeo:
            i = elegir(por_tipeo[clave], monto)
            usados.add(i)
            excepciones.append(_excepcion(
                "Posible error de tipeo", venta, pagos[i]["valor"],
                f"Corregir el documento '{pagos[i]['documento']}' por '{codigo}' en el ERP", pagos[i]))
            continue

        fecha = venta["fecha_venta"]
        if _es_cuota_posterior(venta["n_cuota"]):
            excepciones.append(_excepcion(
                "Cuota de venta anterior", venta, 0,
                "Informativo: cuota diferida; la venta original se registró en un período anterior"))
        elif inicio_bordero and fecha and fecha < inicio_bordero:
            excepciones.append(_excepcion(
                "Fuera del período del Borderó", venta, 0,
                "Informativo: cargar el Borderó que incluya la fecha de esta venta"))
        else:
            excepciones.append(_excepcion(
                "No registrada en ERP", venta, 0,
                "Venta en Transbank sin registro en el Borderó: consultar a recepción"))

    fechas_tbk = [v["fecha_venta"] for v in ventas if v["fecha_venta"]]
    if fechas_tbk:
        desde, hasta = min(fechas_tbk), max(fechas_tbk)
        for i, pago in enumerate(pagos):
            if i in usados or not pago["fecha"] or not (desde <= pago["fecha"] <= hasta):
                continue
            excepciones.append(_excepcion(
                "Sin abono Transbank en este período", None, pago["valor"],
                "Puede abonarse otro día: verificar en el próximo abono de Transbank", pago))

    return conciliadas, excepciones


def verificar_abonos(tbk_list, movimientos):
    """Busca el Total abono de cada archivo TBK entre los abonos de la cartola."""
    resultado = []
    for tbk in tbk_list:
        encontrado = next((m for m in movimientos
                           if round(m["abono"]) == round(tbk["total_abono"])
                           and ("tbk" in m["descripcion"].lower() or "transbank" in m["descripcion"].lower())),
                          None)
        resultado.append({
            "medio": tbk["medio"],
            "total_ventas": round(tbk["total_ventas"]),
            "comision": round(tbk["comision"] + tbk["iva_comision"]),
            "total_abono": round(tbk["total_abono"]),
            "encontrado": encontrado is not None,
            "fecha": encontrado["fecha"] if encontrado else None,
            "glosa": encontrado["descripcion"] if encontrado else "",
        })
    return resultado


def procesar_pesos(credito, debito, prepago, bordero, cartola):
    tbk_list = [leer_tbk(credito, "Crédito"), leer_tbk(debito, "Débito"), leer_tbk(prepago, "Prepago")]
    pagos = leer_bordero(bordero)
    movimientos = leer_cartola(cartola)

    conciliadas, excepciones = conciliar_tarjetas(tbk_list, pagos)
    abonos = verificar_abonos(tbk_list, movimientos)

    conteos = {"Conciliadas": len(conciliadas)}
    for exc in excepciones:
        conteos[exc["Tipo de error"]] = conteos.get(exc["Tipo de error"], 0) + 1

    resultado_pesos["conciliadas"] = conciliadas
    resultado_pesos["excepciones"] = excepciones

    # Vista para la plantilla (las claves con espacios no se pueden usar en Django templates)
    tabla = [{
        "tipo": e["Tipo de error"], "fecha": e["Fecha de origen"], "codigo": e["Documento/Código"],
        "medio": e["Medio"], "monto_tbk": e["Monto TBK"], "monto_erp": e["Monto ERP"],
        "accion": e["Acción sugerida"],
    } for e in excepciones[:200]]

    return {"abonos": abonos, "conteos": conteos, "excepciones": tabla, "total_excepciones": len(excepciones)}
