# Sistema de Conciliación ERP - Hotel Plaza San Francisco

El **Sistema de Conciliación ERP** es una plataforma web interna (desarrollada en Django) diseñada para automatizar y auditar el cruce masivo de datos financieros entre el sistema central del hotel, los abonos de Transbank, las cartolas bancarias y los reportes de los operadores de tarjetas de crédito internacionales.

---

## Fase 1: Consolidación en Dólares (v1.0)

La primera fase del proyecto automatiza la conciliación de pagos y transacciones internacionales en **moneda extranjera (Dólares - USD)**. Su objetivo principal es erradicar el cruce manual de planillas y asegurar que los montos registrados en el ERP del hotel coincidan de manera exacta con lo liquidado por las tarjetas y lo depositado finalmente en el banco.

### 1. Archivos de Entrada (El Insumo Diario)
Cada mañana, el departamento de finanzas debe descargar **7 reportes Excel** (formato Legacy) desde distintas plataformas web. Estos archivos suelen venir llenos de "ruido visual" (logos, celdas combinadas, etc.). El sistema limpia esta basura y extrae con pinzas solo las columnas vitales:

* **A. El Archivo ERP (Sistema del Hotel)** 
  * **Origen:** Exportado desde el sistema de reservas/ERP interno del hotel.
  * **Qué le importa al código:** Extrae el Monto Cobrado, la Fecha y el **Código de Autorización** (la llave maestra digitada por el recepcionista). Ignora datos irrelevantes como el nombre del empleado o el folio de la habitación.
  
* **B. El Archivo Transbank (La Máquina)** 
  * **Origen:** Descargado desde el Portal Privado de Transbank por el contador.
  * **Qué le importa al código:** Extrae el Monto Bruto físico, el Tipo de Tarjeta (VI, MC, AX) y el **Código de Autorización** (el voucher de la máquina). Ignora desgloses de IVA, propinas o números de serie físicos de las máquinas POS.
  
* **C. Los Archivos de Operadoras (Visa, MasterCard, Amex, Diners)** 
  * **Origen:** Descargados directamente de los portales de liquidación de cada marca corporativa.
  * **Qué le importa al código:** Busca el mismo **Código de Autorización** para rescatar el Monto Bruto original y, lo más importante, el **Saldo Neto en Pesos** (el valor final real que la tarjeta transferirá al hotel luego de cobrar el "peaje" de comisión). Ignora asientos contables o días de mora.

* **D. La Cartola del Banco** 
  * **Origen:** Exportada desde el portal corporativo del banco donde el hotel tiene su cuenta corriente.
  * **Qué le importa al código:** Solo escanea la columna de **Abonos** para confirmar que la suma masiva de todos los saldos netos calculados en el paso anterior ingresó efectivamente al banco.

### 2. Flujo de Cruce y Lógica de Negocio
El algoritmo cruza estos 7 archivos mediante los siguientes pasos lógicos:
1. **Validación Transbank vs ERP:** El algoritmo contrasta cada transacción del archivo de Transbank con el reporte del ERP del hotel, usando el Código de Autorización para detectar cobros ausentes o discrepancias numéricas.
2. **Cruce al Detalle por Operadora:** Se compara la salida de Transbank contra el detalle individual de cada tarjeta de crédito. Esto permite auditar las comisiones retenidas y validar los valores líquidos finales.
3. **Confirmación Bancaria:** Se rastrea la cartola del banco en busca del abono exacto, confirmando que la sumatoria total del día ingresó efectivamente a la cuenta corriente del hotel.

### 3. Archivos de Salida y Flujo de Trabajo del Contador (Outputs)
Al finalizar el procesamiento matemático, el sistema reduce el caos de los 7 archivos a solo **2 reportes Excel accionables**, cada uno con un propósito específico para el flujo de trabajo humano:

#### A. Transacciones Ubicadas (`informacion_proceso.xlsx`)
Este archivo contiene todas las transacciones donde el sistema logró enlazar con éxito el **Código de Autorización** en todos los archivos. Sin embargo, "ubicada" no significa "perfecta". El contador debe usar este archivo de la siguiente manera:
* **Filtro de Diferencias:** El contador revisa la columna **Diferencia**. El 95% de los casos será `$0` (cuadre perfecto). Si la diferencia es de, por ejemplo, `-$15`, significa que la transacción existe, pero los montos no coinciden (ej. un descuento aplicado en la máquina pero no registrado en el ERP). El contador debe investigar y resolver manualmente estas discrepancias.
* **Tolerancia al Redondeo:** El algoritmo está programado para perdonar diferencias minúsculas de centavos causadas por el tipo de cambio. Internamente, el código corta los decimales (`int()`) y fuerza un redondeo de dos cifras (`round(..., 2)`) para evitar que el equipo humano pierda tiempo revisando diferencias matemáticas de fracciones de dólar.

#### B. Transacciones No Ubicadas (`transacciones_no_ubicadas.xlsx`)
Este es el archivo de "errores graves" o "ventas huérfanas". 
* **Transbank como Fuente de Verdad:** El algoritmo está diseñado asumiendo que **la máquina de Transbank manda**. El código lee Transbank de principio a fin y busca cada cobro en el ERP.
* **El Diagnóstico:** Si el sistema encuentra una transacción en Transbank pero el código de autorización no existe en el ERP, la envía a este reporte con columnas en blanco y un diagnóstico claro en la columna `Status` (Ej: *"NO ENCONTRÓ EL CÓDIGO X"*).
* **Acción Humana:** El contador toma este archivo, va a la recepción del hotel y exige explicaciones sobre por qué hay ingresos de dinero físico en la máquina que nunca fueron ingresados al sistema interno del hotel.

---

## Hito 1: Auditoría, Refactorización y Optimización del Sistema Legacy (Consolidación Dólares)

Antes de comenzar con el desarrollo de la Fase 2 (Conciliación en Pesos), el equipo de desarrollo realizó una auditoría profunda al código heredado (Legacy) correspondiente a la **Consolidación en Dólares**. 

Se identificó una deuda técnica importante, vulnerabilidades de seguridad, problemas de rendimiento y bugs críticos de pérdida de datos. A continuación se detallan las mejoras implementadas para estabilizar y modernizar el núcleo del sistema:

### 1. Seguridad y Limpieza Profunda del Proyecto
* **Protección de API Keys:** Se detectó una clave de la API de Google Maps expuesta (hardcodeada) en plantillas HTML residuales. Al confirmarse que el proyecto no utiliza mapas, la clave fue eliminada de raíz junto con los archivos obsoletos, erradicando la vulnerabilidad por completo.
* **Eliminación de Código Basura:** Se realizó una purga masiva de carpetas inútiles (`admin/` y `basura/`), eliminando más de 30 archivos HTML de plantillas prefabricadas que no pertenecían al proyecto.
* **Buenas Prácticas de Entorno:** Se auditó el entorno virtual global y se actualizó el archivo `.gitignore` para excluir carpetas pesadas (`.venv/`), la base de datos de desarrollo (`db.sqlite3`) y las carpetas temporales de análisis (`scratch/`).

### 2. Corrección de Bugs Matemáticos y Pérdida de Datos
* **Bug Crítico de Lectura (Punteros de Archivo):** El sistema original procesaba los Excels (`InMemoryUploadedFile`) dentro de un bucle sin reiniciar el puntero (`seek(0)`). Esto causaba que, tras la primera iteración, los archivos se leyeran como "vacíos", ignorando ~35% de transacciones válidas y enviándolas a "No Encontradas".
* **Bug de Ruptura de Totales (El Virus NaN):** Se corrigió un error matemático grave que colapsaba la tabla final (específicamente visible en los reportes de MasterCard). En los Excels de operadoras, cuando una transacción tiene la columna "Otra Moneda" en blanco, Pandas lee esa celda como un objeto `NaN` (Not a Number). El código Legacy intentaba protegerse de letras o celdas vacías con un bloque `try: float(valor) except ValueError:`, pero ignoraba una trampa técnica de Python: `float(NaN)` es una operación matemática válida que no arroja error. Al no detenerse en el `except`, el `NaN` pasaba directo a la sumatoria, infectando el Total (`14000 + NaN = NaN`). Se solucionó inyectando una validación estricta con `math.isnan()` para sanitizar los flotantes.

### 3. Optimización Extrema de Rendimiento (Caché en RAM)
* **Caché en Memoria:** El código anterior leía los archivos desde disco repetidas veces dentro de bucles anidados. Se implementó una arquitectura donde los 7 archivos se cargan una única vez en DataFrames de Pandas al inicio de la función. El cruce financiero ahora se realiza puramente en RAM, reduciendo el tiempo de procesamiento a milisegundos.
* **Desbloqueo de Red:** Se desactivó una petición síncrona innecesaria a una API externa (`mindicador.cl`) que detenía el servidor web cada vez que se recargaba cualquier vista del sistema.

### 4. Clean Code y Mantenibilidad (Refactorización)
* **Reducción de Código Base:** Se redujo el motor financiero (`servicios_financieros.py`) de casi 800 líneas a ~440 líneas (reducción del 45%), eliminando comentarios antiguos e importaciones muertas.
* **Aplicación del Principio DRY (Don't Repeat Yourself):** La lógica de validación por tarjeta estaba copiada 4 veces (Amex, Visa, Diners, MasterCard). Se extrajo en una función centralizada (`check_card`), facilitando la escalabilidad futura.
* **Consolidación Dinámica:** Se reemplazaron más de 120 líneas de sumatorias manuales (`total_amex += ...`) por bucles dinámicos en Python puro para generar las tablas de resumen de forma mantenible.

### 5. Mejoras de Interfaz (UI) y Experiencia de Usuario (UX)
* **Rediseño con Pestañas y Asistente (Wizard):** Se dividió la pantalla en pestañas (Dólares / Pesos) y se agregó un *Toggle* que permite cambiar entre el "Modo Clásico" (cartas con íconos para cada archivo) y un "Modo Asistente" guiado de 3 pasos lógicos.
* **Bloqueo de Múltiples Peticiones:** Se implementó un *Loading Overlay* (pantalla de carga interactiva) que bloquea la pantalla al enviar el formulario.
* **Validaciones Frontend:** Se añadió validación en JavaScript para obligar al usuario a subir estrictamente los 7 archivos antes del envío, evitando caídas 500 en el backend.

---

## Próximos Pasos: Fase 2 (Conciliación en Pesos)
*(Esta sección será documentada próximamente por el equipo de desarrollo a medida que se construya el módulo en CLP).*
