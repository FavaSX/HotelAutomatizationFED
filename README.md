# Sistema de Conciliación ERP - Hotel Plaza San Francisco

El **Sistema de Conciliación ERP** es una plataforma web interna (desarrollada en Django) diseñada para automatizar y auditar el cruce masivo de datos financieros entre el sistema central del hotel, los abonos de Transbank, las cartolas bancarias y los reportes de los operadores de tarjetas de crédito internacionales.

---

## Fase 1: Consolidación en Dólares (v1.0)

La primera fase del proyecto automatiza la conciliación de pagos y transacciones internacionales en **moneda extranjera (Dólares - USD)**. Su objetivo principal es erradicar el trabajo manual y asegurar que los montos registrados en el ERP del hotel coincidan de manera exacta con lo liquidado por las tarjetas y lo depositado finalmente en el banco.

### ¿Cómo funciona?
Para ejecutar el ciclo de conciliación, el operador contable debe proveer **7 archivos Excel** clave al sistema:
1. **Archivo ERP:** Reporte de ventas en dólares extraído del sistema de reservas/ERP del hotel.
2. **Archivo Transbank:** Detalle de todas las transacciones procesadas a través de TBK.
3. **Cartola del Banco:** Estado de cuenta bancario donde se deben reflejar los depósitos reales.
4. **Archivos de Operadoras (4):** Reportes individuales detallados de **Visa, MasterCard, American Express y Diners Club**.

### Flujo de Cruce y Lógica de Negocio
1. **Validación Transbank vs ERP:** El algoritmo contrasta cada transacción en el archivo de Transbank con el reporte del ERP del hotel, usando cruces lógicos para detectar cobros ausentes, duplicados o discrepancias numéricas.
2. **Cruce al Detalle por Operadora:** Se compara la salida de Transbank contra el detalle individual de cada tarjeta de crédito. Esto permite auditar las comisiones retenidas y validar los valores netos de cada marca.
3. **Confirmación Bancaria:** Se rastrea la cartola del banco en busca del abono exacto, confirmando que la sumatoria total del día ingresó efectivamente a la cuenta corriente del hotel.
4. **Generación de Reportes:** 
   - La pantalla renderiza dos **Tablas de Resumen** dinámicas que exponen las diferencias en rojo/verde por cada cuenta contable.
   - Genera reportes en formato Excel (descargables) segmentados en **Transacciones Ubicadas** (cuadradas con éxito) y **No Ubicadas** (inconsistencias que requieren atención contable humana).

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
* **Bug de Ruptura de Totales (NaN):** Se corrigió un error matemático en los cruces de MasterCard, donde las transacciones sin coincidencia asignaban un string vacío (`''`) al monto. Al convertirse a flotante, generaban un valor `NaN` que rompía toda la tabla de consolidación final. Ambos bugs fueron solucionados al 100%.

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
