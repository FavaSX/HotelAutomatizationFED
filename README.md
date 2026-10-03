# Automatización Financiera - Hotel San Francisco

> **Nota:** Este documento se encuentra en construcción. Posteriormente se agregará la descripción general del proyecto, los objetivos y la documentación de la Fase 2 (Conciliación en Pesos).

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
