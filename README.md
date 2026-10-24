# Sistema de Conciliación Financiera ERP — Hotel Plaza San Francisco (v2.0)

El **Sistema de Conciliación Financiera ERP** es una plataforma web corporativa desarrollada en **Django** y diseñada para automatizar, auditar y persistir el cruce masivo de datos financieros entre el sistema central del hotel (ERP), los abonos de **Transbank**, las **cartolas bancarias** y los reportes de liquidación de los **operadores de tarjetas de crédito internacionales** (*Visa, MasterCard, American Express y Diners Club*).

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
4. **Motor de Sugerencia Asistida Nivel 2 (`suggestion_engine.py`):**
   * Para aquellos vouchers que no logran un cruce automático en el Nivel 1 (pestañas *Con Diferencia* y *En TBK pero NO en ERP*), el sistema ejecuta un segundo motor analítico de carácter **consultivo** (nunca fuerza ni altera la conciliación automática).
   * Cruza los vouchers pendientes contra la totalidad de filas disponibles del ERP y de los operadores aplicando algoritmos de similitud de texto (`difflib`), normalización de confusiones visuales de lectura POS/OCR (`G↔6`, `O↔0`, `B↔8`, `T/Z↔7`, `S↔5`, `I↔1`), detección de errores humanos de digitación de 1 o 2 caracteres, cercanía de fechas y correlación de montos en USD y CLP.

---

### 3. Flujo de Trabajo del Contador: Previsualización, Asistente Nivel 2 y Persistencia Oficial
A diferencia del esquema tradicional a ciegas, el sistema opera bajo una arquitectura de **Dos Fases (Preview & Commit)**:

1. **Dashboard Interactivo de Previsualización (En Memoria):**
   Al subir los 7 archivos, el contador accede inmediatamente a una vista preliminar con KPIs ejecutivos (porcentaje de conciliación automática con barra de progreso interactiva, diferencia total y tarjeta de confirmación del abono en cartola bancaria con número de cuenta detectado), las **dos tablas paralelas de cuadratura** (**2A. Cuadratura por Operador en Dólares US$** y **2B. Cuadratura en Moneda Nacional Pesos CLP**) y el desglose línea por línea clasificado en **3 pestañas de auditoría**:
   * **Ubicadas Exactas:** Transacciones que cuadran tanto en ERP como en la operadora (incluyendo el detalle de Cruce de Vouchers `TBK ⇄ ERP`, Cruce de Documentos `ERP ⇄ OP`, Fecha, Cajero, Tarjeta, Monto TBK, Monto Operador, Diferencia USD, Monto CLP y Diagnóstico).
   * **Con Diferencia:** Vouchers ubicados en el ERP pero que presentan diferencias monetarias en dólares o pesos fuera del rango de tolerancia (`±US$ 0,50 USD`), o cuyo documento aún no figura en la liquidación del operador.
   * **En TBK pero NO en ERP:** Cobros reales procesados en la máquina de Transbank cuyo código de autorización no fue ingresado con coincidencia exacta al sistema del hotel por recepción.
2. **Asistente de Cruce Secundario (Modal Nivel 2):**
   En las pestañas *Con Diferencia* y *En TBK pero NO en ERP*, un banner ejecutivo informa cuántos casos cuentan con **sugerencia asistida** y permite abrir un modal interactivo con tablas comparativas lado a lado (`Transbank ⇄ ERP Hotel ⇄ Operadora`), nivel de confianza (`Alta / Media`) y la acción contable sugerida para corregir el dato en recepción.
3. **Confirmación en Base de Datos y Descarga de Excel Oficial en 2 Pasos:**
   Una vez revisados los datos, el contador presiona **"Guardar cierre"**. Mediante una petición asíncrona (AJAX), el sistema persiste atómicamente toda la operación en la base de datos y transforma el modal al **Paso 2**, habilitando el botón de descarga del **Reporte Excel Oficial Multi-Hoja** (*Resumen Dólares*, *Vouchers Ubicados* y *Discrepancias y No Ubicados*) respaldado por el registro histórico recién creado.

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
* **Catálogos y Configuración:** `CardType` (franquicias y prefijos BIN), `ReconciliationStatus` (estados de gestión contable) y `SystemConfiguration` (tolerancias de redondeo en USD y CLP editables por administración).
* **Cabecera y Resúmenes del Cierre:** `ReconciliationProcess` (registro maestro de ejecución), `BankDepositValidation` (validación del abono bancario por modalidad: `USD_GENERAL`, `CLP_CREDIT`, `CLP_DEBIT`, `CLP_PREPAID`) y `ReconciliationSummary` (cuadraturas consolidadas por tarjeta y modalidad en tablas USD y CLP).
* **Almacén de Transacciones Limpias:** `BankStatementMovement`, `ErpTransaction`, `TransbankTransaction` y `CardOperatorTransaction`.
* **Auditoría y Gestión de Discrepancias:** `CrossMatchResult` (resultado individual por cada voucher con diagnóstico del sistema y estado de resolución) y `AuditResolutionLog` (bitácora inmutable de cambios de estado).

#### Blindaje contra Duplicidad (Idempotencia en las 8 Tablas Operativas)
Para evitar que la base de datos se infle si un contador guarda dos veces el mismo proceso o si vuelve a subir los archivos tras corregir una factura en el ERP, se construyó un **motor de deduplicación inteligente**:
1. **Llaves Naturales Únicas por Tabla:** Cada movimiento bancario, cobro ERP, voucher Transbank y liquidación de operadora se identifica por su huella contable real (ej. *Tarjeta + Código de Autorización + Monto + Fecha + BIN* en Transbank, o *Cuenta + Factura + Autorización + Monto + Fecha + Hora* en ERP).
2. **Detección Inteligente del Mismo Cierre Contable:** El sistema evalúa si la mayoría ($\ge 80\%$) de los vouchers del archivo de abono Transbank entrante y el monto total de liquidación coinciden con un proceso ya registrado de la misma moneda. Si es el mismo cierre, reutiliza y actualiza el proceso existente en lugar de duplicar resúmenes; si es un cierre de otro día que solo comparte 1 o 2 filas por solapamiento de fechas de descarga, crea un proceso nuevo sin sobrescribir el historial del día anterior.
3. **Consultas SQL Focalizadas (`.filter(...__in=...)`):** En vez de cargar tablas históricas completas en memoria, el motor filtra en SQL únicamente los lotes de fechas, números de documento y códigos de autorización presentes en los archivos subidos, garantizando tiempos de respuesta constantes aunque la base de datos acumule años de operación.
4. **Resolución y Trazabilidad Automática (`AuditResolutionLog`):** La tabla `CrossMatchResult` mantiene una relación 1 a 1 con cada voucher de Transbank. Si un voucher había quedado como `NOT_FOUND_IN_ERP`, `AMOUNT_MISMATCH`, `CARD_TYPE_MISMATCH` o `OUT_OF_PERIOD` y, al re-subir el archivo ERP corregido, pasa a `MATCHED`, el sistema actualiza el registro existente a conciliado, asigna automáticamente el usuario y fecha de resolución, e inserta una entrada en `AuditResolutionLog` dejando constancia de la corrección.

---

### 3. Motor de Cálculo en Memoria, Auto-Detección y Arquitectura Modular
* **Auto-Detección de Archivos por Huella Interna (`file_detector.py`):** En versiones anteriores el usuario debía subir manualmente cada archivo en casilleros separados, con el riesgo de equivocarse de casilla entre operadoras o modalidades de pago. En la nueva versión se reemplazó ese esquema por una **Zona Única de Carga Masiva en 2 Pasos** tanto para Dólares (7 archivos) como para Pesos (5 archivos + Borderós históricos opcionales):
  1. **Paso 1 (Selección Masiva):** El contador arrastra o selecciona todos los archivos juntos en cualquier orden e incluso con nombres distintos. El sistema abre las primeras 45 filas de cada Excel en memoria e identifica su **huella contable interna** (por ejemplo, leyendo `Cliente: TRANSBANK - TARJETA MASTERCARD US$`, el campo `Medio de pago: Crédito / Débito / Prepago` en liquidaciones CLP, o los encabezados `Cargos de Débitos y Créditos` del Borderó FrontOffice).
  2. **Paso 2 (Tabla de Alineación y Confirmación):** El sistema presenta los roles requeridos alineados junto con la evidencia interna encontrada, el período de fechas detectado, el conteo de filas, el nivel de certeza (`100% Seguro`) y un selector desplegable en cada fila que permite cambiar o reasignar manualmente cualquier archivo antes de ejecutar la conciliación.
* **Previsualización Instantánea (`Zero-Write`):** Toda la lectura y cruce matemático (`calculate_usd_preview` y `calculate_clp_preview`) se ejecuta 100% en memoria RAM en fracciones de segundo y se almacena temporalmente en la sesión del usuario, permitiendo auditar sin tocar la base de datos hasta la confirmación explícita (`commit_usd_preview_to_database` / `commit_clp_preview_to_database`) protegida con `transaction.atomic()`.
* **Detector Híbrido de Columnas (`column_detector.py`) y Precisión `Decimal`:** Escanea las primeras filas de cada Excel buscando coincidencias por nombres de encabezado normalizados (sin tildes ni ruido), utiliza índices posicionales como respaldo secundario y reemplaza los flotantes inexactos por aritmética `Decimal` libre de errores `NaN`.
* **Arquitectura Modular por Responsabilidad Única (`web/services/`):** El núcleo financiero se estructuró en 11 módulos independientes y desacoplados:
  * `usd_reconciliation.py` y `clp_reconciliation.py`: Motores matemáticos de conciliación en memoria para Dólares (USD) y Pesos (CLP).
  * `suggestion_engine.py` y `clp_suggestion_engine.py`: Motores de sugerencia asistida Nivel 2 para análisis de discrepancias, errores tipográficos/OCR y búsqueda en Borderós históricos.
  * `reconciliation_persistence.py` y `clp_persistence.py`: Persistencia transaccional, deduplicación en las 8 tablas operativas y registro automático en `AuditResolutionLog`.
  * `report_exporter.py` y `clp_report_exporter.py`: Generación de reportes Excel oficiales multi-hoja estilizados con `openpyxl`.
  * `file_detector.py`: Clasificación y alineación automática de lotes de archivos USD y CLP por huella estructural interna.
  * `excel_parsers.py` y `column_detector.py`: Lectura compartida, inferencia de marcas por BIN y sanitización de Cartola, Borderó ERP, Transbank y Operadoras.

---

### 4. Rediseño de Interfaz (UI/UX) y Estandarización Contable Chilena
* **Sistema de Diseño Corporativo con Modo Claro y Oscuro (`theme.css`):** Interfaz ejecutiva inspirada en la identidad del **Hotel Plaza San Francisco** (azul marino profundo `#0C0C42` y dorado `#996909`), equipada con barra lateral colapsable, selector de **Modo Claro / Modo Oscuro** persistente, alertas dinámicas y pantalla de bloqueo de carga (*Loading Overlay*) para impedir envíos dobles.
* **Herramientas Avanzadas de Productividad en Tablas (`usd_preview.js`):**
  * **Filtros Instantáneos Combinables:** Buscador en tiempo real por código de voucher, documento, cuenta, factura o huésped; filtro rápido por franquicia de tarjeta (*Visa, MasterCard, Amex, Diners, Redcompra*); filtro por modalidad (*Crédito, Débito, Prepago*) y selector específico por **Usuario Cajero**.
  * **Ordenamiento Dinámico y Modo Pantalla Completa:** Ordenamiento ascendente/descendente (`Mayor a Menor / Menor a Mayor`) en columnas de montos y diferencias, junto con un botón de **Pantalla Completa** (`Esc`) que maximiza el área de trabajo para auditorías extensas.
  * **Paginación Estandarizada (Default 20):** Todas las tablas de vouchers (tanto en Dólares como en Pesos) y todos los listados dentro de los modales de **Sugerencias Asistidas Nivel 2** cuentan con paginación cliente predeterminada en **20 registros por página**, con selector adaptable (`20`, `50`, `100` o `Todos`).
* **Formato Numérico y Diagnósticos Claros (`currency_filters.py`):** Librería única de formateo bajo el estándar chileno (**`.`** para separador de miles y **`,`** para decimales, tanto en USD como en CLP), píldoras visuales duales de comparación (`TBK = ERP` / `TBK ≠ ERP`) y diagnósticos legibles sin jerga técnica.

---

## Fase 2: Conciliación de Transacciones Nacionales en Pesos Chilenos (CLP)

La segunda fase del proyecto automatiza el cierre y cuadratura de las transacciones en **moneda nacional (Pesos Chilenos - CLP)** procesadas a través de las tres modalidades de pago de Transbank (**Crédito**, **Débito** y **Prepago**) frente al **Borderó de Cargos de Débitos y Créditos** del ERP del hotel y los depósitos efectivos en la **Cartola Bancaria en Pesos**.

Su objetivo es validar que los **3 abonos diarios independientes** de Transbank hayan ingresado exactamente a la cuenta corriente del hotel (descontando comisiones e IVA), y verificar que cada voucher cobrado en los terminales POS del hotel esté debidamente registrado por recepción en el Borderó con su código de autorización, monto y medio de pago correctos.

### 1. Archivos de Entrada (Los 5 Insumos Diarios + Borderós Históricos Opcionales)
Para ejecutar un cierre en pesos, el contador arrastra en la zona única de carga un lote base de **5 archivos Excel** (`.xlsx` / `.xls`), con la posibilidad de incluir **archivos Borderó adicionales de períodos anteriores** (6 o más archivos) en la misma operación:

* **A. Borderó ERP del Hotel (`BORDERO.XLS` / `.xlsx`)**
  * **Origen:** Sistema FrontOffice del hotel (*Reporte de Cargos de Débitos y Créditos*).
  * **Datos extraídos:** Rango de fechas del período consultado, Código de Medio de Pago (`VI` Visa, `MC` MasterCard, `AX` Amex, `DI` Diners, `RC` Redcompra Débito), Código de Autorización normalizado, Monto en CLP, Fecha y Hora de transacción, N° de Cuenta, N° de Factura, Reserva, Habitación, **Nombre del Huésped**, **Usuario Cajero** y detección automática de **reversas o anulaciones** (montos negativos). El parser filtra y excluye automáticamente los cargos en moneda extranjera (`AME$`, `VIS$`, `MAS$`, `DIN$`) para aislar exclusivamente la operación en pesos.

* **B. Tres Liquidaciones Diarias de Transbank (`CREDITOS TBK.xlsx`, `DEBITOS TBK.xlsx`, `PREPAGO TBK.xlsx`)**
  * **Origen:** Portal Privado de Transbank (*Reporte de Abonos por día*).
  * **Datos extraídos:**
    * **Bloque de Cabecera Financiera:** *Fecha de Liquidación (Período de Consulta)*, *Cuenta Corriente de Destino*, *Medio de Pago* (`Crédito`, `Débito` o `Prepago`), *Total Ventas (+)*, *Comisión Transbank (−)*, *IVA de Comisión (−)*, *Ventas Anuladas (−)*, *Cobros por Servicio (−)* y **Total Abono Neto (=)**.
    * **Tabla de Detalle de Vouchers:** *Código de Autorización*, *Monto Original de Venta* (en ventas en cuotas, extrae el total original de la compra para cruzar 1:1 con la factura del hotel), *Monto Válido / Cuota del Día*, *Comisión*, *IVA*, *Abono Neto*, *Fecha de Venta*, *Tipo y N° de Cuota* (`1/3`, `2/6`, etc.), *Identificador de Transacción* y *Número de Tarjeta Enmascarado (BIN)*, a partir del cual el motor infiere automáticamente la franquicia (`4→Visa`, `2/5→MasterCard`, `34/37→Amex`, `30/36/38→Diners`).

* **C. Cartola Bancaria en Pesos (`cartola.xlsx` / `.xls`)**
  * **Origen:** Portal bancario corporativo (Cuenta Corriente en Pesos Chilenos).
  * **Datos extraídos:** Número de cuenta corriente en el encabezado, Fecha de movimiento, Descripción (`TC TBK` para tarjetas de crédito y `ABONOS REALIZADOS POR TRANSBANK` para débito y prepago), Cargos, **Abonos** y Saldo contable.

* **D. Soporte Automático para Borderós Históricos Adicionales (6° Archivo Opcional, ej. `BORDERO 04-07.XLS`)**
  * Debido a que una liquidación de Transbank de un día específico (ej. agosto) suele incluir **cuotas posteriores o ventas rezagadas** realizadas en meses previos (ej. julio), el sistema permite arrastrar un 6° archivo correspondiente al Borderó del período anterior junto con los 5 archivos del día.
  * El detector (`file_detector.py`) compara automáticamente las fechas de cobertura de los Borderós subidos, asigna el más reciente como **Borderó Principal** del cierre e ingiere el Borderó anterior como **Fuente Histórica Auxiliar**, poniéndolo a disposición del motor de sugerencias Nivel 2 para localizar al instante las ventas fuera de período.

---

### 2. Flujo de Cruce en Dos Pasadas, Validación Triple de Cartola y Reglas de Negocio
El motor financiero de Pesos (`clp_reconciliation.py`) aplica las reglas contables del hotel en **cuatro etapas sucesivas**:

1. **Validación Independiente de los 3 Abonos en Cartola Bancaria:**
   * Para cada modalidad (**Crédito**, **Débito** y **Prepago**), el motor comprueba primero la integridad interna del archivo de Transbank verificando que la suma del detalle de vouchers cuadre con la cabecera (`Total Ventas − Comisión − IVA = Total Abono`).
   * A continuación, busca en la cartola bancaria los **3 depósitos independientes** correspondientes a cada medio de pago, aplicando la tolerancia parametrizada en pesos (configurable por administración, por defecto $\pm \$1.000\text{ CLP}$) y verificando que un mismo movimiento bancario no sea reclamado dos veces.
2. **Algoritmo de Cruce Transbank vs. Borderó en Dos Pasadas:**
   * **Pasada 1 (Prioridad de Monto Exacto):** El motor procesa primero todos los vouchers de Transbank cuyo código de autorización normalizado tiene una fila en el Borderó con **monto exacto** dentro de la tolerancia. Esto impide que códigos cortos o repetidos consuman erróneamente una fila que pertenecía a otra venta exacta.
   * **Pasada 2 (Neto de Reversas y Discrepancias):** Cuando un mismo código de autorización aparece en múltiples líneas del Borderó (por ejemplo, cuando recepción realiza un cobro, luego ingresa una reversa negativa y vuelve a cargar el monto corregido), el algoritmo calcula primero la **suma neta de todas las filas del código** (cancelando reversas automáticamente) antes de evaluar si existe diferencia monetaria.
3. **Tipificación Contable de Excepciones (`CARD_TYPE_MISMATCH` y `OUT_OF_PERIOD`):**
   * **Discrepancia de Medio de Pago (`CARD_TYPE_MISMATCH`):** Si el código de autorización y el monto cuadran exactamente, pero recepción registró la venta bajo un medio de pago incompatible en el ERP (por ejemplo, una tarjeta de *Crédito* digitada como `RC` *Redcompra Débito*), el sistema la separa en la pestaña *Con Diferencia* indicando el código erróneo para su corrección contable.
   * **Separación de Ventas Fuera de Período (`OUT_OF_PERIOD`):** Si un voucher no se encuentra en el Borderó principal, el motor evalúa su **Fecha de Venta original** y su **Número de Cuota**. Si la fecha de venta está fuera del rango de fechas del Borderó principal o corresponde a una cuota posterior (`2/3`, `3/3`, etc.), no la contamina como extravío del día, sino que la clasifica en una cuarta categoría auditable: **Fuera de Período**.
4. **Motor de Sugerencia Asistida Nivel 2 para Pesos (`clp_suggestion_engine.py`):**
   * Inspecciona todos los vouchers clasificados en *Con Diferencia*, *En TBK pero NO en Borderó* y *Fuera de Período* contra las filas no reclamadas del **Borderó principal** y la totalidad del **Borderó histórico adicional**.
   * Calcula un puntaje ponderado evaluando coincidencia exacta o cercana de monto (comparando tanto el *Monto Original de Compra* como el *Monto Válido de la Cuota*), proximidad de fechas, compatibilidad de tarjeta y similitud tipográfica/OCR del código de autorización (detectando de inmediato casos reales donde recepción digitó un dígito distinto, como `826135` en lugar de `626135`, e indicando la fila exacta del Excel donde se encuentra).

---

### 3. Flujo de Trabajo del Contador en CLP: Previsualización, Asistente Nivel 2 y Cierre Oficial

1. **Dashboard Interactivo de Previsualización CLP (`clp_preview.html`):**
   * **1. Validación de Abonos en Cartola Bancaria:** Muestra el número de cuenta corriente detectado y **3 tarjetas ejecutivas lado a lado** (*Crédito*, *Débito* y *Prepago*) con la fecha de liquidación, el desglose de `Ventas − Comisión − IVA`, el abono neto esperado de Transbank frente al monto real hallado en la cartola bancaria y su estado individual (`Cuadrado` / `Pendiente`).
   * **2. Cuadratura por Medio de Pago (Transbank vs. Borderó):** Tabla resumen por modalidad con el conteo total de vouchers, ubicadas exactas, Ventas TBK, Ventas Borderó, Diferencia, Comisión + IVA, Abono Neto y totales generales.
   * **3. Cuatro Pestañas de Auditoría de Vouchers:**
     * **Ubicadas Exactas:** Vouchers conciliados 1:1 o por neto de reversas dentro de la tolerancia en pesos.
     * **Con Diferencia:** Vouchers cuyo código existe en el Borderó pero presentan diferencias de monto (`AMOUNT_MISMATCH`) o error de medio de pago (`CARD_TYPE_MISMATCH`).
     * **En TBK pero NO en Borderó:** Vouchers del período actual cuyo código de autorización no figura en el Borderó.
     * **Fuera de Período:** Vouchers correspondientes a fechas de venta anteriores al Borderó principal o cobros de cuotas posteriores.
2. **Asistente Interactivo Nivel 2 en las 3 Pestañas de Excepción:**
   * Cada una de las pestañas de excepción (*Con Diferencia*, *En TBK pero NO en Borderó* y *Fuera de Período*) incorpora su propio botón de **Listado de Sugerencias Asistidas Nivel 2**, mostrando el número exacto de sugerencias detectadas y permitiendo auditar la comparación detallada entre el voucher de Transbank y la fila candidata del Borderó (incluyendo el nombre del archivo Borderó de origen y el número de fila en Excel).
3. **Confirmación Atómica y Descarga de Excel Oficial en 2 Pasos (`clp_persistence.py` y `clp_report_exporter.py`):**
   * Al hacer clic en **"Confirmar y Guardar en BD"**, el sistema guarda atómicamente el cierre en pesos reutilizando la arquitectura relacional y el motor anti-duplicados de 8 tablas, registra cualquier resolución histórica en `AuditResolutionLog` y habilita en el mismo modal la descarga del **Reporte Excel Oficial de Pesos** estructurado en 3 hojas (*Resumen Pesos*, *Vouchers Cuadrados* y *Excepciones*).


---

## Seguridad del Despliegue (PythonAnywhere)

* **Protección contra fuerza bruta (`django-axes`):** tras **5 intentos fallidos** se bloquea la combinación *usuario + IP* durante **15 minutos**, tanto en `/login/` como en el login del admin. El bloqueo es por par usuario/IP, por lo que un atacante no puede dejar sin acceso a un usuario legítimo desde otra IP. La IP real del visitante se obtiene de la cabecera `X-Real-IP` del proxy de PythonAnywhere (`web/security.py`). Los intentos quedan registrados en el admin (*Access attempts* / *Access failures*).
  * Desbloquear a un usuario: `python manage.py axes_reset_username <usuario>` (o `axes_reset` para limpiar todos).
* **Configuración segura por defecto:** `DEBUG` está desactivado salvo `DJANGO_DEBUG=True`, y la aplicación no arranca en producción sin `DJANGO_SECRET_KEY`. `ALLOWED_HOSTS` ya no acepta cualquier host.
* **Ruta del admin no predecible:** se define con `DJANGO_ADMIN_URL`, así los bots que prueban `/admin/` reciben un 404.
* **HTTPS y sesiones:** HSTS, cookies seguras, redirección a HTTPS y sesiones que expiran al cerrar el navegador o tras 8 horas.
* No existe registro público: solo un superusuario puede crear cuentas desde el admin.

Variables obligatorias en el archivo WSGI de PythonAnywhere: ver `AppAutomatizationHotel2.0/.env.example`.
