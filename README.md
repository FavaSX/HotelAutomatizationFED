# Sistema de Conciliación Financiera ERP — Hotel Plaza San Francisco (v2.0)

El **Sistema de Conciliación Financiera ERP** es una plataforma web corporativa desarrollada en **Django** y diseñada para automatizar, auditar y persistir el cruce masivo de datos financieros entre el sistema central del hotel (ERP), los abonos de **Transbank**, las **cartolas bancarias** y los reportes de liquidación de los **operadores de tarjetas de crédito internacionales** (*Visa, MasterCard, American Express y Diners Club*).

La plataforma cubre hoy dos procesos de cierre:
* **Fase 1 — Dólares (USD):** tarjetas internacionales cobradas en moneda extranjera y su liquidación en pesos.
* **Fase 2 — Pesos Chilenos (CLP):** ventas nacionales con tarjetas de **Crédito, Débito y Prepago** abonadas por Transbank.

---

## Fase 1: Conciliación de Transacciones Internacionales en Dólares (USD)

La primera fase del proyecto automatiza el cierre y cuadratura de transacciones internacionales en **moneda extranjera (Dólares - USD)** y su correspondiente liquidación en **Pesos Chilenos (CLP)**. Su propósito es eliminar el cruce manual de planillas, detectar cobros omitidos o con diferencias de tipo de cambio/redondeo y garantizar que cada dólar procesado por Transbank esté respaldado en el ERP del hotel y depositado íntegramente en la cuenta corriente bancaria.

### 1. Archivos de Entrada (Los 7 Insumos Diarios)
En cada proceso de conciliación, el departamento de finanzas carga **7 archivos Excel** (`.xlsx` / `.xls`) exportados desde distintas plataformas externas e internas. El motor de lectura limpia automáticamente encabezados decorativos, celdas combinadas y filas de subtotales para extraer las columnas contables y operativas clave:

* **A. Consolidado ERP del Hotel (`TERJETAS ERP.xlsx`)**
  * **Origen:** Sistema interno de gestión hotelera / recepción (Borderó de tarjetas).
  * **Datos extraídos:** Código de Autorización, N° de Cuenta/Documento (Folio de traspaso a operadores), N° de Factura, Monto cobrado, Fecha y Hora de emisión, Tipo de Tarjeta, **Nombre del Huésped**, **N° de Habitación** y **Usuario Cajero/Recepcionista** (estos últimos vitales para que contabilidad identifique al responsable ante cualquier descuadre).

* **B. Abono General Transbank (`ABONO TBK.xlsx`)**
  * **Origen:** Portal Privado de Transbank (detalle de ventas en dólares).
  * **Datos extraídos:** **Código de Autorización** (fuente de verdad de la máquina POS), Marca de Tarjeta (`Visa`, `MasterCard`, `American Express`, `Diners Club`), Monto Bruto en USD, Fecha de Venta, Número de Tarjeta Enmascarado (BIN) y Local/Comercio.

* **C. Reportes de Operadoras Internacionales (`VISA`, `MASTERCARD`, `AMEX`, `DINERS`)**
  * **Origen:** Portales de liquidación de cada franquicia de tarjeta de crédito.
  * **Datos extraídos:** N° de Documento (enlace directo con la columna *Cuenta* del ERP), N° de Secuencia, Monto original en Moneda Extranjera (USD), **Saldo en Pesos (`SALDO`)** y **Saldo Corregido en Pesos (`SALDO CORREGIDO`)**.

* **D. Cartola Bancaria (`cartola.xls`)**
  * **Origen:** Portal corporativo del banco del hotel.
  * **Datos extraídos:** Fecha de movimiento, Descripción, N° de Documento, Cargos, **Abonos** y Saldo contable, identificando de forma automática las transferencias correspondientes a liquidaciones de Transbank.

---

### 2. Flujo de Cruce Triple y Reglas de Negocio
El motor financiero ejecuta la conciliación en **tres niveles de validación simultáneos**:

1. **Cruce Transbank vs. ERP (con soporte de Facturas Divididas):**
   * Tomando a **Transbank como fuente de verdad**, el algoritmo busca cada código de autorización dentro del ERP del hotel.
   * Si en el ERP existen registros preliminares sin facturar y registros definitivos con factura para una misma operación, el sistema prioriza automáticamente las líneas facturadas.
   * Si una reserva divide un único cobro de tarjeta en **múltiples facturas parciales** bajo el mismo código de autorización (*Split Invoices*), el motor agrupa y suma las facturas del ERP para cuadrar el monto exacto del voucher y enlazar todos los números de documento involucrados.
2. **Cruce en Cascada contra Operadoras (USD y CLP):**
   * Utilizando el N° de Cuenta/Documento obtenido del ERP, el sistema localiza la transacción en el archivo de su operadora correspondiente (*Visa, MasterCard, Amex o Diners*).
   * Valida primero el monto en dólares (**Monto TBK USD vs. Monto Operador USD**) aplicando la tolerancia de redondeo configurada en el sistema.
   * En paralelo, valida la conversión a pesos chilenos en cascada: compara el monto CLP del ERP primero contra la columna **`SALDO`** de la operadora y, si hubo ajuste cambiario, contra la columna **`SALDO CORREGIDO`**.
3. **Confirmación de Abono en Cartola Bancaria:**
   * Suma la totalidad de las liquidaciones netas del cierre y escanea la cartola bancaria buscando el abono exacto en cuenta corriente, emitiendo un dictamen inmediato de **Abono Bancario Confirmado** o **Alerta de Diferencia Bancaria**.

---

### 3. Flujo de Trabajo del Contador: Previsualización, Excel y Persistencia
A diferencia del esquema tradicional a ciegas, el sistema opera bajo una arquitectura de **Dos Fases (Preview & Commit)**:

1. **Dashboard Interactivo de Previsualización (En Memoria):**
   Al subir los 7 archivos, el contador accede inmediatamente a una vista preliminar con KPIs ejecutivos, tablas de cuadratura por franquicia en **USD** y **CLP**, estado del abono bancario y el desglose línea por línea clasificado en **3 pestañas de auditoría**:
   * **Ubicadas Exactas:** Transacciones que cuadran tanto en ERP como en la operadora (incluyendo el detalle de Huésped, Habitación, Cajero, Tarjeta, Monto TBK, Monto Operador, Diferencia USD, Monto ERP CLP y Diagnóstico).
   * **Con Diferencia:** Vouchers ubicados en el ERP pero que presentan diferencias monetarias en dólares o pesos fuera del rango de tolerancia, o que no figuran aún en la liquidación del operador.
   * **No Ubicadas en ERP:** Cobros reales procesados en la máquina de Transbank cuyo código de autorización no fue ingresado al sistema del hotel por recepción.
2. **Exportación de Borrador Excel Multi-Hoja:**
   Desde la previsualización, el contador puede descargar en un clic un reporte Excel estilizado con 3 hojas (*Resumen Dólares*, *Vouchers Ubicados* y *Discrepancias y No Ubicados*) para compartir observaciones con recepción antes de cerrar el periodo.
3. **Confirmación y Guardado Definitivo en Base de Datos:**
   Una vez revisados los datos (o tras corregir y volver a subir un archivo con errores), el contador presiona **"Confirmar y Guardar en Base de Datos"**, persistiendo toda la operación de forma atómica con trazabilidad histórica completa.

---

## Fase 2: Conciliación Nacional de Tarjetas en Pesos Chilenos (CLP)

La segunda fase automatiza la cuadratura diaria de las ventas con tarjeta en **moneda nacional (CLP)**. Toma como fuente de verdad los tres abonos de Transbank (**Crédito**, **Débito** y **Prepago**), ubica cada voucher en el **Borderó** del hotel y confirma que el abono neto de cada modalidad llegó a la cuenta corriente. Reutiliza la arquitectura de la Fase 1: auto-detección de archivos, previsualización en memoria, confirmación atómica, deduplicación y bitácora de auditoría.

### 1. Archivos de Entrada (Los 5 Insumos)
El contador sube **5 archivos Excel** en cualquier orden y con cualquier nombre. `file_detector.py` (`CLP_REQUIRED_ROLES`) los reconoce por su contenido interno:

* **A. Borderó ERP del Hotel**
  * **Origen:** Reporte *"Cargos de Débitos y Créditos"* del sistema hotelero. Su encabezado trae el período: *"Periodo de: 04/09/2026 hasta 07/09/2026"*.
  * **Datos extraídos:** Solo pagos con tarjeta en pesos (*Red Compra*, *Visa $*, *Master Card $*, *Amex $*, *Diners $*), incluidas sus reversas. Por cada pago: Código de Autorización, Medio de Pago, Monto, Fecha y Hora, N° de Factura, Cuenta, **Huésped**, **Habitación** y **Cajero**. Las filas en US$ se dejan para la Fase 1.

* **B, C y D. Abonos por Día de Transbank (Crédito, Débito y Prepago)**
  * **Origen:** Portal Privado de Transbank, un archivo por medio de pago.
  * **Cabecera:** Período de consulta (fecha del abono), Cuenta Corriente, Total Ventas, Comisión e IVA, Ventas Anuladas, Cobros por Servicio y **Total Abono**.
  * **Detalle de vouchers:** Fecha de Venta, Código de Autorización, Monto Bruto, Monto Válido, Tipo y N° de Cuota (`1/3`, `S/C`), Comisión, IVA de Comisión, **Abono Neto**, Tarjeta Enmascarada y Local. La marca (*Visa, MasterCard, Amex, Diners*) se deduce del BIN de la tarjeta. Las anulaciones y otros movimientos que no son venta se omiten.

* **E. Cartola Bancaria en Pesos**
  * **Datos extraídos:** Movimientos de la cuenta corriente, incluidos los abonos identificados como depósitos de Transbank.

---

### 2. Reglas de Negocio del Cruce (`clp_reconciliation.py`)
1. **Transbank como fuente de verdad:** Cada voucher se busca en el Borderó por **código de autorización normalizado**.
2. **Medio de pago compatible:** Débito y Prepago deben estar registrados en el Borderó como **Red Compra**, y Crédito como **Visa $ / Master Card $ / Amex $ / Diners $**. Si el monto coincide pero el medio no, el voucher queda como *Medio de pago distinto*.
3. **Tolerancia CLP parametrizable:** La diferencia aceptada se configura en `SystemConfiguration.clp_rounding_tolerance` y siempre queda entre **±$500 y ±$1.000**. El valor por defecto es $1.000.
4. **Reversas y códigos repetidos:** Cuando un código tiene varias filas en el Borderó, primero se compara contra el **neto de todas** (así se anulan las reversas) y, si no cuadra, contra la fila más cercana. Los vouchers que tienen monto exacto se procesan antes, para que un código compartido no lo tome la venta equivocada.
5. **Cuotas y período:** Las cuotas posteriores (`2/3`, `3/3`) y las ventas fuera del período del Borderó cargado se separan como **Fuera de Período**, con la indicación de buscarlas en el Borderó de la venta original.
6. **Abono bancario por modalidad:** El *Total Abono* de cada archivo Transbank se busca en la cartola dentro de la tolerancia, dando preferencia a la glosa esperada (`TC TBK` para Crédito, `ABONOS REALIZADOS POR TRANSBANK` para Débito y Prepago) y a la misma fecha. Un movimiento bancario nunca se asigna a dos abonos.

| Resultado | Significado | Acción sugerida |
| --- | --- | --- |
| `MATCHED` | Código y monto coinciden en el Borderó | — |
| `AMOUNT_MISMATCH` | El código existe, pero el monto difiere más allá de la tolerancia | Revisar el monto con recepción |
| `CARD_TYPE_MISMATCH` | El monto coincide, pero está registrado con otro medio de pago | Corregir el medio de pago en el ERP |
| `NOT_FOUND_IN_ERP` | El voucher de Transbank no está en el Borderó | Buscar la venta con el cajero y registrarla en el ERP |
| `OUT_OF_PERIOD` | Cuota posterior o venta de otra fecha | Buscar en el Borderó del período de la venta |

---

### 3. Motor de Sugerencias Asistidas Nivel 2 (`clp_suggestion_engine.py`)
El motor solo ayuda al auditor y **nunca concilia de forma automática**:
* **Vouchers no ubicados:** Propone filas del Borderó que ningún voucher reclamó, con el mismo monto y medio de pago, registradas el mismo día o el siguiente, y con un código parecido (error de digitación u OCR) o sin código. Cada propuesta muestra sus razones (*Monto Exacto*, *Misma Fecha*, *Mismo Medio*, *Código digitado como…*).
* **Diferencias de monto:** Detecta cuando la diferencia se registró como un cargo separado el mismo día.
* **Borderó sin voucher:** Lista los pagos con tarjeta del Borderó que no corresponden a ningún voucher. Por ejemplo, ventas a crédito que Transbank abonará en un depósito posterior.

---

### 4. Flujo de Trabajo del Contador (Preview & Commit)
1. **Carga masiva (`/reconciliation/clp/`):** El contador arrastra los 5 archivos, revisa la tabla de alineación propuesta por el detector y la corrige si hace falta.
2. **Previsualización en memoria:** Muestra KPIs, cuadratura por **modalidad** (Crédito, Débito, Prepago) y por **marca**, la validación de los 3 abonos contra la cartola y 6 pestañas de auditoría: *Cuadrados*, *Con Diferencia*, *En TBK pero NO en Borderó*, *Fuera de Período*, *En Borderó sin Voucher* y *Reporte de Excepciones*.
3. **Confirmación y guardado (`clp_persistence.py`):** `commit_clp_preview_to_database` guarda todo en una sola transacción atómica y aplica las mismas protecciones de la Fase 1:
   * Si al menos el 80% de los vouchers y los abonos coinciden con un proceso ya guardado, actualiza ese proceso en lugar de crear uno nuevo.
   * Filtra en SQL por llaves naturales para no duplicar movimientos, filas del Borderó ni vouchers.
   * Registra en `AuditResolutionLog` los vouchers que cambian de resultado al volver a subir los archivos.
4. **Excel oficial (`clp_report_exporter.py`):** Desde un proceso guardado se descarga un libro de 3 hojas: **Resumen Pesos**, **Vouchers Cuadrados** y **Reporte de Excepciones**. Esta última hoja trae las columnas Tipo de error, Fecha de origen, Documento / Código, Monto discrepante y Acción sugerida.

---

## Hito 1: Auditoría del Sistema Legacy y Reingeniería Total (Plataforma v2.0)

El punto de partida del proyecto fue la recepción de un prototipo heredado (**Sistema Legacy v1.0**). Antes de avanzar hacia nuevas fases, el equipo realizó una auditoría forense de código, arquitectura, seguridad y precisión matemática sobre dicho proyecto original.

Debido a la magnitud de la deuda técnica y a que el proyecto antiguo carecía por completo de base de datos para almacenar el historial contable, se tomó la decisión de ingeniería de **reconstruir la plataforma desde cero (`AppAutomatizationHotel2.0`)**, corrigiendo todos los defectos de raíz e incorporando capacidades empresariales de auditoría.

### 1. Diagnóstico del Proyecto Antiguo (Por qué se reconstruyó desde cero)
Durante la auditoría del código original se detectaron fallas críticas que impedían su uso confiable en producción:
* **Pérdida Silenciosa del ~35% de las Transacciones (Bug de Punteros `seek(0)`):** El código antiguo leía los archivos `InMemoryUploadedFile` dentro de bucles sin reiniciar el puntero de lectura. Tras la primera pasada, los archivos se leían como vacíos, enviando decenas de transacciones válidas a la lista de *"No Encontradas"*.
* **Corrupción de Totales por Valores `NaN`:** Cuando una celda de moneda extranjera venía vacía en los reportes de operadoras (frecuente en *MasterCard*), Pandas la interpretaba como `NaN`. Como en Python `float(NaN)` no lanza excepción `ValueError`, el valor `NaN` ingresaba a las sumatorias contaminando el total general (`Total = NaN`).
* **Falso Negativo en Facturas Divididas (*Split Invoices*):** El sistema antiguo tomaba únicamente la primera fila coincidente del ERP. Cuando recepción dividía el pago de una habitación en dos o más facturas con el mismo código de autorización, el sistema reportaba una diferencia falsa o ignoraba las facturas secundarias.
* **Fragilidad ante Cambios de Columnas:** Toda la lectura dependía de índices numéricos fijos (`row[7]`, `row[24]`), por lo que cualquier columna nueva en un reporte de Transbank o del ERP rompía el cruce.
* **Ausencia Total de Persistencia y Auditoría:** El sistema antiguo funcionaba únicamente como un convertidor efímero de archivos que sobrescribía 2 Excels locales en disco, sin guardar procesos, transacciones ni historial de quién resolvió una discrepancia.
* **Vulnerabilidades de Seguridad y Código Basura:** Existían claves de API de Google Maps expuestas en el código fuente, peticiones síncronas a APIs externas que bloqueaban la carga de páginas y más de 30 plantillas HTML residuales sin relación con el sistema.

---

### 2. Nueva Arquitectura de Base de Datos Relacional y Motor Anti-Duplicados
Se diseñó e implementó desde cero un modelo relacional normalizado en Django compuesto por **11 modelos (3 catálogos base y 8 tablas operativas de conciliación)**:
* **Catálogos y Configuración:** `CardType` (franquicias y prefijos BIN), `ReconciliationStatus` (estados de gestión contable) y `SystemConfiguration` (tolerancias de redondeo en USD y CLP editables por administración). La migración `0003_clp_tolerance_and_catalogs` carga los catálogos base (Visa, MasterCard, American Express, Diners Club; estados *Pendiente* y *Aceptado*) y limita la tolerancia CLP al rango de $500 a $1.000.
* **Cabecera y Resúmenes del Cierre:** `ReconciliationProcess` (registro maestro de ejecución), `BankDepositValidation` (validación del abono bancario por modalidad) y `ReconciliationSummary` (cuadraturas consolidadas por tarjeta en tablas USD y CLP).
* ** Almacén de Transacciones Limpias:** `BankStatementMovement`, `ErpTransaction`, `TransbankTransaction` y `CardOperatorTransaction`.
* **Auditoría y Gestión de Discrepancias:** `CrossMatchResult` (resultado individual por cada voucher con diagnóstico del sistema y estado de resolución) y `AuditResolutionLog` (bitácora inmutable de cambios de estado).

#### Blindaje contra Duplicidad (Idempotencia en las 8 Tablas Operativas)
Para evitar que la base de datos se infle si un contador guarda dos veces el mismo proceso o si vuelve a subir los archivos tras corregir una factura en el ERP, se construyó un **motor de deduplicación inteligente**:
1. **Llaves Naturales Únicas por Tabla:** Cada movimiento bancario, cobro ERP, voucher Transbank y liquidación de operadora se identifica por su huella contable real (ej. *Tarjeta + Código de Autorización + Monto + Fecha + BIN* en Transbank, o *Cuenta + Factura + Autorización + Monto + Fecha + Hora* en ERP).
2. **Detección Inteligente del Mismo Cierre Contable:** El sistema evalúa si la mayoría ($\ge 80\%$) de los vouchers del archivo `ABONO TBK` entrante y el monto total de liquidación coinciden con un proceso ya registrado. Si es el mismo cierre, reutiliza y actualiza el proceso existente en lugar de duplicar resúmenes; si es un cierre de otro día que solo comparte 1 o 2 filas por solapamiento de fechas de descarga, crea un proceso nuevo sin sobrescribir el historial del día anterior.
3. **Consultas SQL Focalizadas (`.filter(...__in=...)`):** En vez de cargar tablas históricas completas en memoria, el motor filtra en SQL únicamente los lotes de fechas, números de documento y códigos de autorización presentes en los archivos subidos, garantizando tiempos de respuesta constantes aunque la base de datos acumule años de operación.
4. **Resolución y Trazabilidad Automática (`AuditResolutionLog`):** La tabla `CrossMatchResult` mantiene una relación 1 a 1 con cada voucher de Transbank. Si un voucher había quedado como `NOT_FOUND_IN_ERP` o `AMOUNT_MISMATCH` y, al re-subir el archivo ERP corregido, pasa a `MATCHED`, el sistema actualiza el registro existente a conciliado, asigna automáticamente el usuario y fecha de resolución, e inserta una entrada en `AuditResolutionLog` dejando constancia de la corrección.

---

### 3. Motor de Cálculo en Memoria, Auto-Detección y Arquitectura Modular
* **Auto-Detección de Archivos por Huella Interna (`file_detector.py`):** En versiones anteriores el usuario debía subir manualmente cada uno de los 7 archivos en 7 casilleros separados, con el riesgo de equivocarse de casilla entre las 4 operadoras. En la nueva versión se reemplazó ese esquema por una **Zona Única de Carga Masiva en 2 Pasos**:
  1. **Paso 1 (Selección Masiva):** El contador arrastra o selecciona los 7 archivos juntos en cualquier orden e incluso con nombres distintos. El sistema abre las primeras 45 filas de cada Excel en memoria e identifica su **huella contable interna** (por ejemplo, leyendo `Cliente: TRANSBANK - TARJETA MASTERCARD US$`, `Código Local: 27941117` o los códigos `AME$ / VIS$`).
  2. **Paso 2 (Tabla de Alineación y Confirmación):** El sistema presenta los 7 roles alineados junto con la evidencia interna encontrada, el conteo de filas, el nivel de certeza (`100% Seguro`) y un selector desplegable en cada fila que permite cambiar o reasignar manualmente cualquier archivo antes de ejecutar la conciliación, haciendo innecesario mantener el antiguo formulario manual de 7 casilleros.
* **Previsualización Instantánea (`Zero-Write`):** Toda la lectura y cruce matemático (`calculate_usd_preview`) se ejecuta 100% en memoria RAM en fracciones de segundo y se almacena temporalmente en la sesión del usuario, permitiendo auditar y exportar borradores sin ensuciar la base de datos hasta la confirmación explícita (`commit_usd_preview_to_database`) protegida con `transaction.atomic()`.
* **Detector Híbrido de Columnas (`column_detector.py`) y Precisión `Decimal`:** Escanea las primeras filas de cada Excel buscando coincidencias por nombres de encabezado normalizados (sin tildes ni ruido), utiliza índices posicionales como respaldo secundario y reemplaza los flotantes inexactos por aritmética `Decimal` libre de errores `NaN`.
* **Arquitectura Modular por Responsabilidad Única (`web/services/`):** El núcleo financiero se estructuró en módulos independientes y desacoplados:
  * `usd_reconciliation.py`: Motor matemático de cruce triple en memoria para Dólares.
  * `reconciliation_persistence.py`: Persistencia transaccional, deduplicación en las 8 tablas y registro automático en `AuditResolutionLog`.
  * `report_exporter.py`: Generación de reportes Excel multi-hoja estilizados con `openpyxl`.
  * `file_detector.py`: Clasificación y alineación automática por estructura interna de los 7 archivos USD y los 5 archivos CLP.
  * `excel_parsers.py` y `column_detector.py`: Lectura compartida y sanitización de Cartola, ERP/Borderó, Operadoras y abonos Transbank en pesos.
  * `clp_reconciliation.py`: Motor de cruce en memoria para Pesos (Transbank Crédito, Débito y Prepago vs. Borderó y Cartola).
  * `clp_suggestion_engine.py`: Sugerencias asistidas de Nivel 2 para vouchers en pesos no ubicados o con diferencia.
  * `clp_persistence.py`: Persistencia transaccional y deduplicación del cierre en pesos.
  * `clp_report_exporter.py`: Excel oficial del cierre en pesos con el Reporte de Excepciones.

---

### 4. Rediseño de Interfaz (UI/UX) y Estandarización Contable Chilena
* **Identidad Visual Corporativa:** Interfaz completamente nueva inspirada en la línea gráfica del **Hotel Plaza San Francisco** (azul marino profundo `#0C0C42` y dorado `#996909`), con barra de navegación unificada, alertas dinámicas y pantalla de bloqueo de carga (*Loading Overlay*) para impedir envíos dobles.
* **Flujo Guiado de Carga y Confirmación:** La pantalla de carga guía al contador desde el arrastre masivo de los 7 reportes hasta la tabla interactiva de verificación de alineación, validando en vivo que no falten archivos ni existan asignaciones duplicadas antes de habilitar el procesamiento.
* **Formato Numérico y Diagnósticos Claros:** Se creó una librería de filtros personalizados (`currency_filters.py`) que formatea todos los montos bajo el estándar chileno (**`.`** para separador de miles y **`,`** para decimales, tanto en USD como en CLP), muestra los nombres completos de las franquicias (*MasterCard, Visa, American Express, Diners Club*) y presenta diagnósticos directos como `Coincide con SALDO $107.984` o `Coincide con SALDO CORREGIDO $47.430`.

---

## Puesta en Marcha
```bash
cd AppAutomatizationHotel2.0
python manage.py migrate      # obligatorio: carga catálogos y tolerancias (sin ellos falla el guardado)
python manage.py test web     # suite de pruebas USD y CLP
python manage.py runserver
```
* Conciliación en Dólares: `/` (página de inicio)
* Conciliación en Pesos: `/reconciliation/clp/`

---

## Estado del Proyecto
* **Fase 1 (USD):** Implementada.
* **Fase 2 (CLP):** Versión 1 implementada (cruce, sugerencias asistidas, persistencia y Excel de excepciones).

