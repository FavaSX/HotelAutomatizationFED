# Registro de cambios — AppAutomatizationHotel2.0

Bitácora de cambios y funcionalidades nuevas: **qué** se hizo, **por qué**, **de dónde sale cada dato** (base de datos, sesión o Excel) y **qué pruebas lo cubren**.
Las entradas más recientes van primero.

**Cómo levantar la app:** desde `AppAutomatizationHotel2.0/`, ejecutar `venv\Scripts\python.exe manage.py runserver` y abrir <http://127.0.0.1:8000/>. Para usar otro puerto: `manage.py runserver 8765`.

**Cómo correr las pruebas:** `venv\Scripts\python.exe manage.py test web`. Django crea una base de datos temporal y la borra al terminar, así que no toca `db.sqlite3`.

---

## Lo que queda por hacer

Lista viva: se actualiza en cada cambio. Al terminar un punto, se borra de aquí y se agrega como entrada en el historial de más abajo.

**Cómo leer las etiquetas** (revisión del 2026-10-14 contra el código y el cierre real del 03/12/2025, 110 vouchers):
- 🔴 **Ocurre:** se comprobó en los datos reales o en el código; se indica a qué afecta y cuánto.
- 🟡 **Preventivo:** el código permite el problema, pero **no ocurre** en los datos revisados. Evita errores en otros meses.
- ⚪ **Decisión / orden:** no cambia ningún número; es una decisión con finanzas, una mejora de presentación o limpieza.

### 1. Motor de conciliación USD (`web/services/usd_reconciliation.py`)
- [ ] 🟡 **Búsqueda parcial del código de autorización (solo USD).** `usd_reconciliation.py` (línea ~242) todavía busca `auth_code in erp_key` cuando no hay coincidencia exacta; un código corto podría calzar dentro de otro más largo que lo contenga (por ejemplo `1234` dentro de `512340`). Verificado el 2026-10-14: en el cierre real no produjo cruces equivocados, y 23 de los 110 vouchers dependen de ella porque el ERP rellena los códigos con ceros a la izquierda. **`main` ya trae `normalize_auth_code` en `column_detector.py`** (quita los ceros a la izquierda) y la Fase 2 (CLP) ya la usa; para USD bastaría con buscar por esa clave en vez de por coincidencia parcial.
- [ ] 🟡 **La diferencia en pesos no afecta el estado.** Un voucher queda `MATCHED` solo si cuadra en US$. Verificado: en el cierre real los **85** conciliados también cuadran en pesos (0 casos). Protege contra errores de digitación en pesos en el ERP.
- [ ] 🟡 **Tolerancia en pesos.** El cruce en pesos exige igualdad exacta; en el levantamiento se habló de ±500 a 1.000 CLP. Verificado: ningún voucher queda a menos de $ 1.000 sin cuadrar exacto (0 casos). Confirmar el valor con finanzas antes de implementarlo.
- [ ] 🟡 **Una línea del ERP o de la operadora podría asignarse a dos vouchers.** El código no marca las líneas ya usadas. Verificado: 0 líneas repetidas en el cierre real.
- [ ] 🟡 **Cargos anulados en el ERP.** El ERP registra reversas como filas positivas; por ejemplo, un mismo código con un cargo y su reversa por el mismo monto, sin factura. El motor ignora las filas positivas, así que si un voucher solo tuviera un cargo anulado, lo cruzaría con ese cargo. Verificado: el ERP del cierre tiene **10 filas positivas**. La única que toca un voucher de este cierre es la de un voucher con pago dividido, y el motor lo resuelve bien porque da prioridad a las líneas con factura. Las otras 9 son de días anteriores y no corresponden a vouchers de este abono.
- [ ] 🟡 **Validación del abono bancario** (`excel_parsers.parse_and_validate_bank_deposits`): acepta cualquier abono de la cartola que esté dentro de la tolerancia, sin exigir que sea de Transbank. Verificado: el abono encontrado ("Compra/venta Divisas", el día del abono) **sí** es de Transbank. La cartola tiene 134 movimientos, 24 marcados como Transbank. *(Corrección: la versión anterior de esta lista decía que se usaba una tolerancia en US$ sobre un monto en pesos. Es incorrecto: el abono está en dólares.)*
- [ ] ⚪ **Nombres internos cruzados en el resumen USD.** En `usd_table` (y en `ReconciliationSummary` para las filas USD), `erp_sales` contiene el monto **Transbank** y `tbk_sales` el de la **operadora**. Verificado: las pantallas y el Excel exportado los rotulan bien ("Total TBK" / "Total Operador"), así que el usuario no ve nada erróneo. Solo confunde a quien lee el código.

### 2. Interfaz
- [ ] ⚪ **Rediseñar la pantalla de carga de archivos** (`/reconciliation/usd/`, `usd_upload.html`). Es la única que conserva el estilo anterior.
- [ ] ⚪ **Mover "Vaciar BD Pruebas"** fuera del flujo normal (al admin o a una página solo para staff), o eliminarlo antes de producción.
- [ ] ⚪ **Historial de cierres:** una página con todos los cierres USD, filtros y descarga del Excel. Hoy el Inicio muestra los últimos 6. Hay una versión en la rama `eva` (`historial.html`).
- [ ] ⚪ **Panel de información general en el Inicio** (dólares y pesos juntos). Ideas propuestas el 2026-10-18; pendiente elegir cuáles.
- [ ] ⚪ **Gestión de excepciones:** marcar una excepción como resuelta y comentarla. El modelo existe (`CrossMatchResult.management_status`, `auditor_comment`, `AuditResolutionLog`), pero no hay pantalla.

### 3. Pruebas
- [ ] **Prueba de regresión del motor con cierres reales.** Correr `calculate_usd_preview` con los 7 Excel de un cierre y comparar voucher por voucher con un resultado revisado. Los Excel tienen nombres de huéspedes: leerlos desde una carpeta fuera del repositorio (por ejemplo, con una variable de entorno) y saltar la prueba si no están. Resultado ya revisado del cierre 03/12/2025 en la entrada del 2026-10-18. Sumar otro período con vouchers de Diners, Amex o Visa con diferencias y cuotas, que en ese cierre no aparecen. *(Se implementó el 2026-10-14 y se revirtió el 2026-10-18.)*
- [ ] Pruebas unitarias de `file_detector` (que identifique bien los 7 archivos) y `reconciliation_persistence` (que no duplique al guardar dos veces el mismo cierre).
- [ ] Prueba del Inicio con datos: crear un cierre de ejemplo y verificar los números de cada tarjeta. Hoy solo se prueba el estado vacío.

### 4. Seguridad y configuración (antes de producción)
Verificado en `BankReconciliation/settings.py` y `web/views.py`:
- [ ] `SECRET_KEY` está escrita en el código; leerla de una variable de entorno o `.env`.
- [ ] `DEBUG = True` y `ALLOWED_HOSTS = ['*']`; ajustar para producción.
- [ ] `clear_test_db` solo exige sesión iniciada: cualquier usuario puede borrar todos los cierres. Restringir a staff o eliminar.
- [ ] 🟡 Tamaño de la sesión: la previsualización de este cierre (5 días de ventas) ocupa **~525 KB** en `django_session`. Con un mes completo puede llegar a varios MB.
- [ ] El sistema antiguo `AppAutomatizationHotel/` sigue en el repositorio, con su `db.sqlite3`. Archivarlo o eliminarlo.

### 5. Fase 2 (Diego) y alcance del levantamiento (Drive: "Automatización de Conciliaciones Plaza San Francisco")
- [ ] **Fase 2 — Conciliación en pesos (CLP), a cargo de Diego:** débito, crédito y prepago de Transbank vs. borderó vs. cartola. Insumos en el Drive, "Analisis consolidado en pesos". Está en la rama `eva`; entra a `main` cuando el equipo lo apruebe.
- [ ] **Conciliación bancaria general** (cartola vs. control financiero vs. contabilidad), con `08_2026_CONCILIACIÓN BANCARIA HPSF.xlsx` como referencia.
- [ ] **Casos especiales del levantamiento.** Estado en el cierre USD del 03/12/2025:
  - pagos en cuotas: no aparecen (los 110 vouchers son "Venta US$");
  - un pago grande depositado en varias transferencias: no evaluado en USD;
  - notas de crédito y anulaciones: el ERP tiene filas positivas (ver punto 1, "Cargos anulados");
  - errores de tipeo en el código (por ejemplo "O" por "0"): no evaluado;
  - registros borrados del ERP: no evaluado.
- [ ] **Reporte de excepciones con acción sugerida:** tipo de error, fecha, documento o código, monto de la diferencia y recomendación.
- [ ] **Prueba piloto con los datos de septiembre** junto a finanzas, para ajustar falsos positivos.

---

## 2026-10-18 — Rama actualizada sobre el nuevo `main` para rehacer el pull request

### Qué pasó
El PR #10 (esta rama) se mezcló a `main` y se revirtió minutos después (PR #11). Luego `main` quedó en `49f21aa` "Refine reconciliation preview summaries" (FavaSX), sin rastro del merge ni de la reversión. Para volver a proponer los mismos cambios, la rama `feat/inicio-dashboard-y-diseno` se rebasó sobre ese `main`.

### Conflictos y cómo se resolvieron
- **`web/views.py`:** ambos lados agregaron imports de modelos; quedaron todos (`CrossMatchResult`, `ReconciliationProcess`, `ReconciliationSummary`, `SystemConfiguration`). La vista de previsualización conserva lo de `main` (tolerancia desde `SystemConfiguration`) y lo de esta rama (`franchise_rows`, `match_rate`).
- **`web/tests.py`:** se mantiene la prueba nueva de `main` (chip "Tolerancia ±US$ 0,50 USD") y se agregan las clases de esta rama después.
- **`web/templates/usd_preview.html`:** se mantiene el diseño de esta rama y se le suman los aportes de `49f21aa`:
  - chip **"Tolerancia ±US$ … USD"** en el encabezado, con el valor configurado;
  - la **cuenta bancaria** en la tarjeta del abono, que usa `preview.bank_account_number` si el abono no la trae;
  - botones de sugerencias con la cantidad de casos **con sugerencia** (se mezcló sin conflicto).

### Respaldo
La versión anterior de la rama quedó en la rama local `respaldo/inicio-dashboard-028a644`.

### Verificación
- 20 pruebas de Django, todas pasan, incluida la prueba de tolerancia de `main`.
- `manage.py check` sin problemas.
- Previsualización revisada con un cierre real en modo claro: chip de tolerancia, cuenta y tarjetas correctas.

---

## 2026-10-18 — Botón de tema y selector Dólares/Pesos en azul (modo claro)

### Qué cambió
En **modo claro**, con el mismo estilo que "Dólar Hoy" y la fecha (vidrio azul `rgba(35, 48, 140, .88)` con letras claras):
- **Botón de cambiar tema** (barra superior): fondo azul e ícono blanco.
- **Selector Dólares / Pesos del Inicio:**
  - fondo azul;
  - pestañas inactivas en blanco translúcido;
  - pestaña activa resaltada con un azul más claro y texto blanco;
  - contador de pendientes en blanco sobre azul.

En **modo oscuro** no cambia nada.

### Por qué
Pedido de Eva, para que todos los controles de la parte superior compartan el estilo azul con letras claras del modo claro.

### Cómo
Reglas al final de `theme.css` para `.btn-theme-toggle` y `.currency-switch` bajo `[data-bs-theme="light"]`. El selector de pestañas de la previsualización usa el mismo componente (`.segmented`), pero **no se modificó**, porque las reglas solo aplican a `.currency-switch`. Cache del CSS en `?v=29`.

### Verificación
- Captura de la parte superior del Inicio en modo claro.
- Modo oscuro comparado píxel a píxel con el original: sin cambios visibles.
- 20 pruebas de Django, todas pasan.

---

## 2026-10-18 — "Dólar Hoy" y fecha en azul (modo claro)

### Qué cambió
En **modo claro**, los chips de la barra superior "Dólar Hoy" y la fecha de hoy tienen **fondo azul con letras blancas**, y el ícono en azul claro (`#C9CEEE`). Mantienen el efecto de vidrio (azul `rgba(35, 48, 140, .88)` con desenfoque) y un brillo superior sutil. En **modo oscuro** no cambian.

### Por qué
Pedido de Eva, para que los datos de la barra superior destaquen en azul, el color principal del modo claro.

### Cómo
Regla al final de `theme.css` para `.dollar-pill` y `.date-chip` bajo `[data-bs-theme="light"]`. Cache del CSS en `?v=28`.

### Verificación
Capturas de la barra superior en modo claro (ambos chips azules con texto blanco) y en modo oscuro (sin cambios). 20 pruebas de Django, todas pasan.

---

## 2026-10-18 — Vuelve el "liquid glass" en modo claro

### Qué cambió
En modo claro vuelve el efecto de vidrio translúcido en el **menú lateral, la barra superior, los modales y los chips** (fecha, Dólar Hoy y botón de tema), igual que en el diseño original. Las tarjetas de contenido siguen blancas y planas.

### Por qué
El ajuste anterior del modo claro (más abajo, "Modo claro en azul y más descansado") había hecho el menú blanco sólido, la barra superior casi opaca y el fondo prácticamente sin color. El vidrio solo se nota si hay color detrás que difuminar, así que el efecto había desaparecido.

### Cómo
- **Vidrio en modo claro** (`theme.css`, tokens):
  - `--glass-bg` rgba(255,255,255,.55) y `--glass-bg-strong` .70;
  - borde blanco `--glass-border` .70 y brillo `--glass-highlight` .95;
  - sombra con un borde sutil de 1 px para que el panel se distinga del fondo.
- **Fondo de color para el vidrio:** tres manchas suaves en azul, celeste e índigo (opacidad .10–.16), en vez de las doradas originales.
- Se quitaron las reglas que hacían opacos el menú y la barra superior en modo claro. A la barra solo se le dejó una línea inferior sutil.
- Cache del CSS en `?v=26`.

### Verificación
- Modo claro revisado arriba y con la página bajada: el menú deja ver el tono azul del fondo detrás.
- Modo oscuro: comparación píxel a píxel con la versión original, sin cambios visibles (175 píxeles de suavizado en la barra superior).
- 20 pruebas de Django, todas pasan.

---

## 2026-10-18 — Modo claro en azul y más descansado (modo oscuro sin cambios)

### Qué cambió
- **Color de acento por modo:** en **modo oscuro** sigue el **dorado**; en **modo claro** predomina el **azul marino** de la marca. Afecta:
  - menú activo y botones principales ("Nueva conciliación", "Guardar cierre", "Seleccionar archivos");
  - porcentajes destacados y barras de las tarjetas;
  - chips y filtros activos, ícono de carga, foco de los campos, "Dólar Hoy" e íconos de marca.
- **Modo claro más descansado para la vista:**
  - **Fondo y texto:** fondo gris azulado suave (`#F2F4F8`) en vez de gris claro con manchas doradas, y texto un poco más suave (`#1B1F3B` en vez del azul marino puro `#0C0C42`; secundario `#5A6075`).
  - **Tarjetas:** blancas y planas, sin el degradado que las oscurecía hacia abajo, con sombras más suaves y un borde superior azul en la tarjeta destacada.
  - **Menú lateral:** blanco con borde, en vez de vidrio translúcido sobre blanco. *(Revertido: ver la entrada "Vuelve el liquid glass en modo claro".)*
  - **Barra superior y tablas:** barra superior casi opaca, y fondo de barras de progreso y encabezados de tabla con más contraste.
- **Login en modo claro:** el ícono del encabezado azul usa un tono claro para que se vea.

### Por qué
Pedido de Eva. En modo claro el dorado predominaba sobre fondo blanco, y la combinación de manchas de color, degradados sobre tarjetas y texto azul marino puro cansaba la vista.

### Cómo (para el equipo)
- Nuevos tokens de acento en `theme.css`: `--accent`, `--accent-soft`, `--accent-gradient`, `--accent-glow`, `--accent-rgb` y `--on-accent` (color del texto sobre el acento).
  - **Oscuro:** valen exactamente el dorado de antes (`#D4AF4F`, el mismo degradado, texto `#0A0A0B`).
  - **Claro:** azul (`#23308C`, degradado `#3442A8 → #23308C → #141A5C`, texto blanco).
  - `--brand*` y `--bs-primary*` siguen al acento en cada modo.
- En todas las reglas se reemplazó el dorado fijo por esos tokens: `var(--gold-*)` (31 usos), `rgba(212, 175, 79, …)` (11) y el texto negro `#0A0A0B` sobre fondos dorados (11).
- Nueva sección **"11. Modo claro: lectura descansada"** al final de `theme.css`. Cache del CSS en `?v=25`.
- **Para estilos nuevos:** usar `var(--accent…)`, no `var(--gold…)`, para que respeten el modo.

### Verificación
- **Modo oscuro sin cambios:** comparación píxel a píxel del Inicio antes y después. Difieren 175 de 2.450.880 píxeles, solo por suavizado en el botón translúcido de la barra superior; no se ven a simple vista.
- **Modo claro:** revisado en Inicio, previsualización USD, carga CLP y login.
- 20 pruebas de Django, todas pasan.

### Fuera de alcance
El interior de la tabla de vouchers de la previsualización (comparación TBK = ERP con verdes y turquesas, montos en verde) es diseño del equipo de `main` y no se tocó.

---

## 2026-10-18 — "Dólar Hoy" se anima al abrir la app

### Qué cambió
El valor "Dólar Hoy" de la barra superior cuenta desde 0 hasta la cotización:
- **la primera vez que se muestra en la pestaña del navegador**, aunque la sesión ya estuviera iniciada;
- y también **justo después de iniciar sesión**, como antes.

Al navegar entre páginas en la misma pestaña aparece directo, sin animación. Si el sistema operativo pide reducir animaciones, se muestra directo siempre.

### Por qué
Antes solo se animaba justo después del login, así que al abrir la app con la sesión ya iniciada nunca se veía la animación. Ahora es consistente con las tarjetas del Inicio.

### Cómo
`web/static/js/app.js` (`showValue`): se anima si existe la marca `justLoggedIn` (la pone el formulario de login) **o** si aún no existe `usdRateAnimated` en `sessionStorage`. Después de mostrarlo se guarda `usdRateAnimated`. `sessionStorage` es por pestaña, así que una pestaña nueva vuelve a animarse. Cache del JS en `?v=4` (`base.html`).

### Datos
Sin cambios: la cotización viene de `cl.dolarapi.com` (respaldo: `open.er-api.com`) y se guarda en `sessionStorage` para no volver a pedirla en cada página.

### Pruebas
Revisado en Chrome sin interfaz:
- **Primera carga del Inicio:** 20 valores distintos ($25,37 → $177,74 → … → $984,38).
- **Siguiente página en la misma pestaña** (Conciliación Pesos): directo a $984,38.

Las 20 pruebas de Django pasan.

---

## 2026-10-18 — Animación de conteo en "Monto en revisión"

### Qué cambió
La tarjeta "Monto en revisión" del Inicio ahora también cuenta desde 0 hasta el monto al abrir el panel, igual que "Conciliación automática" y "Excepciones pendientes", en dólares (US$) y en pesos ($).

### Por qué
Era la única tarjeta con número que no se animaba. La animación solo admitía un texto después del número (como el "%"), y los montos llevan el símbolo de moneda antes.

### Cómo
- La animación del Inicio (`dashboard.html`) admite `data-countup-prefix` (por ejemplo `$`).
- Al terminar, deja **exactamente el texto que formateó el servidor** (`num_usd` / `num_clp`), así el valor final no depende de cómo el navegador agrupa los miles.
- `dashboard_usd.html`: el monto en US$ usa 2 decimales. `dashboard_clp.html`: el monto en $ usa el prefijo y 0 decimales.

### Datos
Sin cambios: es la misma suma de `transbank_amount` de las excepciones pendientes.

### Pruebas
Revisado en Chrome sin interfaz con la base real:
- **US$:** el valor leído cada 50 ms va subiendo hasta el monto final, con el mismo formato del servidor.
- **$:** al cambiar a Pesos, a los 300 ms iba a mitad de camino y terminó en el monto exacto.

---

## 2026-10-18 — Se borraron los 2 cierres de pesos de prueba de la base local

### Qué cambió
A pedido de Eva, se eliminaron de la base local los procesos **#1 y #2**: cierres de pesos de prueba creados el 12/10/2026 con la versión anterior de la rama `eva`, con 6 vouchers inventados cada uno (códigos `123456`, `1O0000`, `555555`, `777777`, `654321`, `999999`).

### Por qué
Distorsionaban el panel de pesos del Inicio:
- La tarjeta de conciliación mostraba **+35,5 pts**, porque comparaba el cierre real #4 (68,8%) con el de prueba #2 (33,3%).
- Las pendientes sumaban 52 en vez de 44.
- Aparecían tipos que el motor actual no genera ("Void Or Credit Note", "Typo Suspected", "Ghost In Erp").

### Cómo se hizo
1. Se verificó que ningún registro de otros cierres (#3 USD y #4 CLP) apuntara a transacciones, líneas ERP o movimientos bancarios de #1 o #2: 0 referencias. Tampoco había registros de auditoría.
2. Respaldo previo de la base: `db_real_respaldo_antes_borrar_prueba.sqlite3`, en la carpeta temporal de la sesión.
3. Borrado dentro de una transacción, que comprueba antes que cada proceso sea CLP y tenga exactamente esos 6 códigos de prueba. Django eliminó en cascada 44 registros: 2 procesos, 12 cruces, 8 transacciones Transbank, 8 transacciones ERP, 4 movimientos de cartola, 6 validaciones bancarias y 4 resúmenes.

### Resultado
Quedan 2 cierres, ambos reales: **#3 (USD, abono 03/12/2025)** y **#4 (CLP, abono 08/09/2026)**. El panel de pesos muestra 44 excepciones pendientes con los tipos del motor de Diego: Fuera de período 31, Falta en ERP 6, Error de tipeo 5, No cuadra monto 1 y Falta código en ERP 1. La variación en puntos desaparece hasta que exista un segundo cierre real de pesos con el cual comparar.

---

## 2026-10-18 — Tarjeta "Conciliación automática" igual en Dólares y Pesos

### Qué cambió
La tarjeta "Conciliación automática" del Inicio era distinta en cada panel:
- **Dólares:** barra de 2 segmentos (conciliados / excepciones) y el texto "85 de 110 vouchers".
- **Pesos:** barra por estado con leyenda.

Ahora ambos paneles usan **el mismo componente** (`partials/dashboard_match_card.html`):
- porcentaje y variación respecto del cierre anterior;
- barra por estado: conciliados (verde), con diferencia (naranja), no ubicados (rojo) y fuera de período (gris);
- leyenda con la cantidad de cada estado;
- total de vouchers del último cierre.

La única diferencia es la que existe en los datos: en pesos el estado rojo se llama "No en Borderó" y se agrega "Fuera de período", que la conciliación en dólares no tiene. En dólares se lee "No ubicadas en ERP".

### Por qué
La tarjeta de dólares se hizo primero y no se actualizó al crear la de pesos. Con el desglose por estado, en dólares se ve que de las 25 excepciones del último cierre, 22 son vouchers no ubicados en el ERP y 3 tienen diferencia de monto.

### De dónde salen los datos
`cross_match_result.system_result` del último cierre de cada moneda, contado por estado con la nueva función `_result_counts()` de `web/views.py`, que usan ambos paneles. `CARD_TYPE_MISMATCH` (solo pesos) se suma a "con diferencia".

### Archivos
- `web/views.py`: `_result_counts()`; `_usd_dashboard()` ahora entrega `last_counts`, igual que `_clp_dashboard()`.
- `web/templates/partials/dashboard_match_card.html` (nuevo), incluido desde `dashboard_usd.html` y `dashboard_clp.html`.

### Pruebas
20 pruebas, todas pasan. Revisión visual con la base real: dólares 77,3% (85 / 3 / 22, 110 vouchers) y pesos 68,8% (97 / 1 / 12 / 31, 141 vouchers).

---

## 2026-10-18 — Cierre real de pesos guardado en la base local

### Qué cambió
A pedido de Eva, se guardó en la base local (`AppAutomatizationHotel2.0/db.sqlite3`) el **cierre real de pesos del abono 08/09/2026** (Borderó 04/09 al 07/09/2026), para que el panel de pesos del Inicio muestre datos reales. Es el proceso **#4**.

### Cómo se hizo
- **Archivos:** `Descargas\Datos Conciliacion pesos` → `BORDERO 04-09 A 07-09.XLS`, `cartola.xlsx`, `CREDITOS TBK 08-09.xlsx`, `DEBITOS TBK 08-09.xls` y `PREPAGO TBK 08-09.xls`.
- **Pasos:** los mismos que hace la app al guardar desde "Conciliación Pesos": `calculate_clp_preview`, luego `enrich_clp_preview_with_suggestions` y luego `commit_clp_preview_to_database`, con el usuario `admin`. No se modificó código del motor.
- **Respaldo previo:** se copió la base antes de guardar, en la carpeta temporal de la sesión (`db_real_respaldo_2026-10-18.sqlite3`).

### Resultado
141 vouchers: 97 conciliados, 1 con diferencia, 12 no están en el Borderó y 31 fuera de período. Conciliación automática 68,8%. Los 3 abonos (crédito, débito y prepago) se encontraron en la cartola del 08/09/2026.

### Ojo
La base local sigue teniendo los 2 cierres de pesos de prueba antiguos (#1 y #2), que suman 8 excepciones con tipos que el motor actual ya no usa. Por eso el panel muestra 52 pendientes; el cierre real aporta 44.

### Verificación
El Inicio (`/?moneda=clp`) responde 200 y muestra el proceso #4 como último cierre: 68,8%, abonos 3/3, y desglose por modalidad y por tipo de error.

---

## 2026-10-18 — Inicio con dos paneles: Dólares y Pesos

### Qué cambió
El Inicio ahora tiene un selector **Dólares | Pesos**. Cada botón muestra el número de excepciones pendientes de esa moneda.
- **Panel de dólares:** el mismo de antes (conciliación automática, excepciones pendientes, monto en revisión, abono bancario, franquicias, excepciones por cajero, excepciones de mayor monto y cierres recientes).
- **Panel de pesos (nuevo):**
  - **Conciliación automática** del último cierre, con barra de 4 estados: conciliados, con diferencia, no están en el Borderó y fuera de período.
  - **Excepciones pendientes** de todos los cierres en pesos, por tipo.
  - **Monto en revisión** en pesos, indicando cuánto corresponde a ventas fuera de período (no son errores del hotel: se buscan en el Borderó de su fecha).
  - **Abonos bancarios** del último cierre: los 3 abonos (crédito, débito y prepago) con su estado en la cartola y el monto.
  - **Modalidades del último cierre:** crédito, débito y prepago con monto cobrado, vouchers conciliados, comisión Transbank y abono neto, más la comisión total.
  - **Excepciones por tipo de error** con la **acción sugerida** que calcula el motor de Diego (por ejemplo "Falta en ERP → Buscar la venta con el cajero y registrarla en el ERP").
  - **Excepciones de mayor monto** (modalidad, tarjeta, fecha y tipo) y **cierres recientes** (porcentaje conciliado, abonos encontrados "3/3" y descarga del Excel).
- El panel abierto se elige así:
  1. con `?moneda=usd` o `?moneda=clp` en la URL;
  2. si no viene en la URL, el último que miró el usuario (guardado en el navegador);
  3. si es la primera vez, dólares, salvo que solo existan cierres en pesos.
- Cada panel tiene sus propios botones "Reporte del último cierre" y "Nueva conciliación".

### Por qué
El Inicio solo mostraba dólares. El panel de pesos no copia el de dólares: se diseñó a partir de lo que realmente guarda la conciliación CLP, verificado con el cierre real del 08/09/2026.
- Hay 3 modalidades, cada una con su abono.
- Existe el estado "fuera de período" (31 de 141 vouchers en ese cierre).
- Las excepciones casi nunca tienen cajero (43 de 44 sin cajero), así que un panel por cajero no aportaría. En su lugar va el panel por tipo de error y acción sugerida, que es el "reporte de excepciones con acción sugerida" que pidió el hotel en el levantamiento.

### De dónde salen los datos (base de datos)
| Tarjeta (pesos) | Tabla(s) | Cálculo |
|---|---|---|
| Conciliación automática | `reconciliation_process`, `cross_match_result` | `system_result` del último cierre CLP: `MATCHED`, `AMOUNT_MISMATCH` + `CARD_TYPE_MISMATCH`, `NOT_FOUND_IN_ERP`, `OUT_OF_PERIOD`. |
| Excepciones pendientes / Monto en revisión | `cross_match_result` | Estado `PENDING` y resultado distinto de `MATCHED`, en todos los cierres CLP; suma de `transbank_amount`. |
| Abonos bancarios | `bank_deposit_validation` | `CLP_CREDIT`, `CLP_DEBIT` y `CLP_PREPAID` del último cierre. |
| Modalidades | `cross_match_result` → `transbank_transaction.payment_modality`, `reconciliation_summary` | Vouchers y monto por modalidad; comisión (`total_commissions`) y abono neto (`net_deposit_total`) sumados por modalidad. |
| Excepciones por tipo de error | `cross_match_result.system_status_detail` | El motor CLP guarda "Modalidad · Tipo de error · Acción sugerida"; se agrupa por tipo y se muestra la acción más frecuente. |
| Cierres recientes | `reconciliation_process`, `bank_deposit_validation` | Últimos 6 cierres CLP; abonos encontrados / total. |

**Ojo con la base local:** tiene 2 cierres de pesos de prueba antiguos (#1 y #2, creados con la versión anterior de la rama `eva`). Usan resultados que el motor actual ya no genera ("Void Or Credit Note", "Typo Suspected", "Ghost In Erp"), y por eso aparecen en el panel e inflan las pendientes. Con cierres reales solo aparecen los tipos del motor de Diego.

### Archivos
- `web/views.py`: `dashboard_view` dividido en `_usd_dashboard()` y `_clp_dashboard()`, con funciones comunes (`_closings`, `_recent_closings`, `_pending_exceptions`, `_pending_by_type`, `_split_status_detail`).
- `web/templates/dashboard.html`: encabezado, selector y JS (se recuerda el panel y la animación de números corre al mostrarlo).
- `web/templates/partials/dashboard_usd.html` y `partials/dashboard_clp.html` (nuevos).
- `web/static/css/theme.css`: estilos del selector, segmento gris "fuera de período", leyenda y chip neutro. Cache del CSS en `?v=24`.

### Pruebas
20 pruebas, todas pasan (`manage.py test web`). En `DashboardViewTests`:

| Prueba | Qué verifica |
|---|---|
| `test_empty_state_without_processes` | Sin cierres, ambos paneles muestran su estado vacío. |
| `test_clp_panel_figures_come_from_saved_closing` | Con un cierre CLP de ejemplo (4 vouchers, 3 abonos y comisión): tasa 50%, pendientes, monto fuera de período, abonos 2/3, agrupación por tipo de error con su acción y desglose por modalidad. |
| `test_defaults_to_clp_panel_when_only_pesos_exist` | Si solo hay cierres en pesos, abre el panel de pesos. |

Revisión visual con una **copia** de la base local en la que se guardó el cierre real de pesos del 08/09/2026 (141 vouchers): escritorio oscuro y claro, y móvil. **La base de datos real no se modificó.**

---

## 2026-10-18 — Actualización con `origin/main` manteniendo el diseño local

### Qué cambió
Se trajeron los 14 commits nuevos de `origin/main` (conciliación en pesos de Diego, motor de sugerencias "Nivel 2", nueva previsualización USD con paginación, filtro por cajero, pantalla completa y JavaScript propio, migración `0003_clp_tolerance_and_catalogs`). El `main` local se avanzó a `1d806ac` y los cambios visuales locales se volvieron a aplicar encima, **sin quitar ninguna función nueva**:

- **Inicio (dashboard):** `dashboard.html`, `dashboard_view` y la ruta `/`. La carga USD sigue en `/reconciliation/usd/`. El menú suma "Inicio" sobre "Conciliación Dólares" y "Conciliación Pesos". En el Inicio, el subtítulo del menú dice "Suite de Conciliación Financiera"; en cada módulo, el de ese módulo.
- **Menú y barra superior** (`base.html`): fecha de hoy en vez del texto fijo, menú lateral fijo al hacer scroll, enlace al admin solo para staff y cache del CSS subido a `?v=23`.
- **Previsualización USD** (`usd_preview.html`):
  - Se aplicó el diseño local: encabezado, indicadores (conciliación con barra de 3 colores, diferencia total, abono), cuadratura USD + CLP en una sola tabla, pestañas segmentadas, barra de búsqueda y filtros con chips, y barra de acciones.
  - **Las tablas de vouchers son las de `main`, sin cambios por dentro:** comparación TBK = ERP y ERP = OP, fecha, cajero, insignias de diagnóstico, sugerencias de Nivel 2, paginación y pantalla completa. Se mantuvo la estructura que necesita el CSS de pantalla completa (`#voucherSectionCard` con `.card-header`, `#voucherToolbar` y `.card-body`).
- **Modales de guardar y descartar** (`partials/usd_preview_modals.html`): diseño local, con todos los IDs y el `data-commit-url` que usa `usd_preview.js`. El modal de sugerencias es el de `main`, sin cambios. El botón final del modal de guardado dice "Finalizar": `usd_preview.js` lo envía a la carga de archivos.
- **Estilos** (`theme.css`): nueva sección "10. Inicio y previsualización (diseño local)" al final del archivo. Incluye las clases base que `main` eliminó y que el Inicio usa (`card-hero-gold`/`navy`, `icon-square`, `stat-value`, `stat-delta`). **No se definió `.sort-icon`**, porque `usd_preview.js` la controla en las previsualizaciones de dólares y de pesos.
- Se eliminó `partials/voucher_cells.html`, que quedó sin uso.

### Cómo se hizo (para poder revisarlo o deshacerlo)
1. Respaldo de los cambios locales en `git stash` ("visual-local antes de actualizar con origin/main (2026-10-18)") y en una copia de archivos.
2. `git merge --ff-only origin/main` (sin commits locales, así que no hubo merge con conflictos).
3. Reaplicación manual archivo por archivo sobre las versiones nuevas.
4. `manage.py migrate` en la base local para aplicar `0003_clp_tolerance_and_catalogs` (crea los catálogos que falten con `get_or_create`; no borra datos).

### Datos
- **Inicio:** sin cambios, sigue leyendo los cierres USD guardados.
- **Previsualización:** la cuadratura por franquicia ahora muestra los totales que calcula el motor de `main`, que **incluye los vouchers no ubicados en el ERP** en el total Transbank. Por eso el total Transbank de la cuadratura aumenta, y la "Diferencia total" (operadoras vs. Transbank) ahora incluye los montos de los vouchers no ubicados. El cambio viene del motor de `main`, no del diseño.

### Pruebas
- 18 pruebas, todas pasan: las 14 de `main` (`ClpHelpersTests`, `ClpPreviewTests`, `ClpPersistenceTests`, `ClpViewsTests` y otras) más las 4 locales (`DashboardViewTests`, `UsdPreviewViewTests`), agregadas al final de `web/tests.py`.
- Revisión visual con Chrome sin interfaz en el Inicio, la previsualización con los 110 vouchers reales y las cargas USD y CLP. La pantalla de pesos se ve igual que en `main`, salvo el nuevo enlace "Inicio" del menú.

### Pendientes resueltos por `main` (verificados en el código actualizado)
- Los vouchers no ubicados ya suman en la cuadratura por franquicia.
- El filtro `clear_diagnosis` ya no existe.
- Los catálogos se crean con la migración `0003_clp_tolerance_and_catalogs`.
- La conciliación CLP ya usa `normalize_auth_code` en vez de la búsqueda parcial.

---

## 2026-10-18 — Se quitaron los cambios de backend; se mantiene solo lo visual

### Qué cambió
A pedido del equipo, se revirtieron los cambios de backend hechos el 2026-10-13 y 2026-10-14, y se mantuvieron los cambios visuales.

**Revertido (vuelve a como está en `main`):**
- `web/services/column_detector.py` y `web/services/usd_reconciliation.py`: el motor vuelve a la búsqueda parcial original del código de autorización. Se eliminaron `normalize_auth_code` e `index_by_normalized_auth_code`.
- `web/test_usd_regression.py`: eliminada la prueba de regresión con el cierre real.
- `web/tests.py`: eliminadas `AuthCodeMatchingTests` y `test_login_ignores_external_next`.
- `web/views.py` (`login_view`): se quitó la validación del parámetro `?next=`. Después del login siempre se va al Inicio.

**Se mantiene (visual, y el código que alimenta esas pantallas):**
- Inicio con dashboard (`dashboard.html`, `dashboard_view`, ruta `/`; la carga de archivos sigue en `/reconciliation/usd/`).
- Rediseño de la previsualización USD (`usd_preview.html`, `partials/`, y en `usd_preview_view` el armado de `franchise_rows` y `match_rate`).
- Menú lateral fijo, barra superior con la fecha, "US$" en la tarjeta de abono y estilos en `theme.css`.

### Por qué
Decisión del equipo: por ahora esta rama local solo incluye mejoras visuales; los cambios al motor y a las pruebas del motor se verán aparte.

### Hallazgos que conviene conservar (verificados el 2026-10-14 con el cierre real del 03/12/2025)
Se registran aquí para no tener que repetir el análisis cuando se retome el motor:
- **El ERP rellena los códigos de autorización con ceros a la izquierda** hasta 7 caracteres (Transbank `A1234` → ERP `0A1234`). **23 de los 110 vouchers** cruzan gracias a la búsqueda parcial. Con el ERP de ese cierre, la búsqueda parcial no produjo cruces equivocados.
- **El motor actual coincide con la salida del sistema antiguo** (Drive: "archivos de salida de proceso") en los 110 vouchers: los mismos 88 ubicados y 22 no ubicados, con igual documento ERP y monto CLP. Hay 4 diferencias revisadas, y en todas el motor nuevo es el correcto:
  - **Un voucher con pago dividido:** el sistema antiguo lo cruzó con una sola factura; el motor nuevo detecta las dos facturas que suman exactamente el monto de Transbank y coinciden con las dos liquidaciones de la operadora. El cargo provisorio sin factura fue reversado en el ERP; el motor prioriza las líneas con factura.
  - **Un voucher** con US$ 1,20 de diferencia entre Transbank y la operadora, sobre la tolerancia de 0,50.
  - **Dos vouchers** están en el ERP pero no en el archivo de MasterCard.
- **Resultado del cierre 03/12/2025:** 85 ubicadas, 3 con diferencia, 22 no en ERP. El abono calculado se encontró en la cartola, con US$ 0,07 de diferencia.

### Datos
Sin cambios en la base de datos. Los Excel reales no se copiaron al repositorio.

### Pruebas
Quedan 4 pruebas (`web/tests.py`), todas pasan:

| Prueba | Qué verifica |
|---|---|
| `test_requires_login` | Sin sesión, `/` redirige al login. |
| `test_empty_state_without_processes` | Con la base vacía, el Inicio muestra el estado vacío. |
| `test_redirects_to_upload_without_preview` | Sin borrador en sesión, la previsualización redirige a la carga de archivos. |
| `test_merges_usd_and_clp_rows_per_franchise` | La previsualización une las filas USD y CLP por franquicia. |

`manage.py check` sin problemas.

---

## 2026-10-14 — "US$" en la tarjeta de abono del Inicio

### Qué cambió
En la tarjeta "Abono bancario · último cierre" del Inicio, los montos "Calculado Transbank" y "En cartola" ahora se muestran con "US$".

### Por qué
Antes aparecían sin moneda. Se verificó que el abono está en dólares: el Excel de Transbank (`ABONO TBK`) indica "Moneda: Dólar", y el abono calculado cuadra en dólares (ventas − comisión − IVA de la comisión = abono calculado). La previsualización ya lo mostraba en US$.

### De dónde salen los datos
Tabla `bank_deposit_validation` del último cierre USD: `transbank_calculated_deposit` y `bank_statement_deposit`. El Inicio solo considera cierres con `process_currency = "USD"`, así que siempre son montos en dólares.

### Archivos
- `web/templates/dashboard.html`.

### Pruebas
Sin prueba nueva (es solo texto). Se comprobó renderizando el Inicio con el cierre guardado en la base local: muestra ambos montos con "US$". Las 13 pruebas existentes pasan.

---

## 2026-10-13 — Menú lateral fijo al hacer scroll

### Qué cambió
El menú lateral (Inicio, Conciliación Dólares, Admin y Cerrar Sesión) ahora queda fijo a la izquierda mientras se baja por la página. Si el menú creciera más que la ventana, se desplaza por dentro.

### Por qué
Al bajar en pantallas largas (Inicio, previsualización) el menú se iba hacia arriba y había que volver al inicio de la página para cambiar de sección. El panel tenía `position: sticky`, pero como el contenedor flex lo estiraba a la altura de toda la página, nunca llegaba a quedar fijo.

### Cómo se resolvió
En `web/static/css/theme.css` (`.sidebar-panel`): `height: 100dvh` (con `100vh` de respaldo), `align-self: flex-start` para que no se estire, `overflow-y: auto` y `z-index: 1030`. En móvil no cambia nada: el menú sigue siendo un panel deslizable con `position: fixed`.

### Datos
No usa base de datos ni Excel; es solo estilo.

### Pruebas
Es un cambio de CSS, sin prueba automática. Se verificó con Chrome sin interfaz (headless) a 1440×900: después de bajar 766 px en el Inicio, el borde superior del menú queda en 0 px, su altura es igual a la de la ventana (900 px) y todos sus botones siguen visibles.

---

## 2026-10-13 — Rediseño de la previsualización USD

**Pantalla:** `/reconciliation/usd/preview/` (`usd_preview_view`, plantilla `web/templates/usd_preview.html`)

### Qué cambió
- **Encabezado** con el paso del flujo ("Paso 2 de 3"), un chip "Borrador sin guardar" y el período contable.
- **Tres indicadores** al inicio:
  - **Conciliación automática:** porcentaje de vouchers ubicados, con una barra de tres colores (ubicadas / con diferencia / no están en ERP). Al hacer clic en la leyenda se abre la pestaña correspondiente.
  - **Diferencia total:** en US$ (operadoras vs. Transbank) y en CLP (ERP vs. operadoras).
  - **Abono en cartola:** confirmado o no, con el monto calculado por Transbank, el monto en cartola, la fecha y la cuenta.
- **Cuadratura por franquicia:** las dos tablas anteriores (USD y CLP) se unieron en una sola, con columnas agrupadas.
  - Las diferencias en cero se ven en gris con ✓ y las distintas de cero en rojo.
  - Los negativos en pesos se muestran como `-$1.234`, no `$-1.234`.
  - Las franquicias sin movimientos se marcan como tales.
- **Detalle de vouchers:**
  - Pestañas como control segmentado, con el contador de cada una.
  - Buscador y filtro por tarjeta en una sola fila.
  - La tabla cabe sin barra horizontal en escritorio.
  - El diagnóstico se corta en dos líneas y muestra el texto completo al pasar el mouse.
  - En "No están en ERP" se agregó el número de tarjeta enmascarado y la acción sugerida.
- **Barra inferior fija** con "Descartar" y "Guardar cierre".
- **Modales de guardar y descartar** rehechos. Al guardar, el botón final lleva al **Inicio** (antes volvía a la carga de archivos).

### Por qué
Antes los montos se partían en dos líneas, las tablas USD y CLP repetían las mismas franquicias por separado, había demasiados colores compitiendo y el diagnóstico ocupaba hasta cuatro líneas por fila. El objetivo es que el contador vea primero el resultado y las excepciones, y después el detalle.

### De dónde salen los datos
- **Nada se lee de la base de datos en esta pantalla.** Todo viene de `request.session["usd_reconciliation_preview"]`, el borrador que calcula `calculate_usd_preview()` en `web/services/usd_reconciliation.py` a partir de los **7 Excel** subidos:

  | Archivo | Rol |
  |---|---|
  | `TERJETAS ERP.xlsx` | ERP del hotel |
  | `ABONO TBK.xlsx` | Transbank |
  | `cartola.xls` | Cartola bancaria |
  | `AMEX DOLAR.XLS` | Operadora |
  | `DINERS DOLAR.XLS` | Operadora |
  | `VISA DOLAR.XLS` | Operadora |
  | `MASTERCARD DOLAR.XLS` | Operadora |

- La vista agrega dos datos derivados, sin recalcular la conciliación:
  - `franchise_rows`: une `usd_table` y `clp_table` por `card_code` (AX, DI, MC, VI) y marca `has_activity` cuando la franquicia tiene montos.
  - `match_rate`: `counts.matched / counts.total × 100`.
- **Nombres de columna en la tabla de cuadratura.** En `usd_table` del motor, el campo `erp_sales` contiene en realidad el **monto Transbank** y `tbk_sales` el **monto de la operadora**. Por eso la pantalla los rotula "Transbank" y "Operadora". En `clp_table` sí corresponden a ERP y operadora. Pendiente: corregir esos nombres en el motor.

### Archivos
- `web/views.py`: `usd_preview_view` arma `franchise_rows` y `match_rate`.
- `web/templates/usd_preview.html`: nueva estructura. Se mantienen los IDs y funciones de JS (búsqueda, filtros, ordenamiento y guardado por AJAX).
- `web/templates/partials/diff_cell.html` (nuevo): celda de diferencia (✓ si es cero, rojo si no) para USD y CLP.
- `web/templates/partials/voucher_cells.html` (nuevo): celdas de voucher (código, tarjeta, documento, huésped, habitación y cajero).
- `web/static/css/theme.css`: secciones "14. Preview (USD)" y "Modals".

### Pruebas (`web/tests.py` → `UsdPreviewViewTests`)
| Prueba | Qué verifica |
|---|---|
| `test_redirects_to_upload_without_preview` | Sin borrador en sesión, redirige a la carga de archivos. |
| `test_merges_usd_and_clp_rows_per_franchise` | Con un borrador de ejemplo (sin leer ningún Excel): une USD y CLP por franquicia, quita el sufijo " US$" del nombre, marca Diners sin movimientos, calcula 100% de conciliación y muestra el voucher. |

Revisión visual adicional: capturas en escritorio (claro y oscuro) y en móvil (390 px), con los archivos reales de prueba del 4/12/2025 (110 vouchers: 85 ubicadas, 3 con diferencia, 22 no en ERP).

---

## 2026-10-13 — Inicio con dashboard

**Pantalla:** `/` (`dashboard_view`, plantilla `web/templates/dashboard.html`)

### Qué cambió
- La raíz `/` ahora es el **Inicio**. La carga de archivos USD pasó a `/reconciliation/usd/`.
- Después del login se entra al Inicio.
- **Menú lateral:** "Inicio" y "Conciliación Dólares", con la pantalla activa resaltada. "Base de Datos (Admin)" solo se muestra a usuarios staff.
- **Barra superior:** el texto fijo del hotel se reemplazó por la fecha de hoy.

### Por qué
El Inicio debía mostrar indicadores con sentido para el contador, **sin valores escritos a mano**: cada número sale de los cierres guardados en la base de datos.

### De dónde salen los datos (base de datos `db.sqlite3`)
Solo se consideran procesos con `process_currency = "USD"`. Los procesos CLP de prueba no se mezclan, porque la Fase 2 aún no está en `main`.

| Tarjeta | Tabla(s) | Cálculo |
|---|---|---|
| Conciliación automática | `reconciliation_process`, `cross_match_result` | Vouchers `MATCHED` / total del **último** cierre. Muestra la variación en puntos respecto del cierre anterior. |
| Excepciones pendientes | `cross_match_result`, `reconciliation_status` | Resultados con `management_status = PENDING` y `system_result ≠ MATCHED`, de **todos** los cierres USD, agrupados por tipo. |
| Monto en revisión | `cross_match_result` | Suma de `transbank_amount` de esas excepciones pendientes. |
| Abono bancario | `bank_deposit_validation` | Validación del último cierre: monto calculado por Transbank vs. monto en cartola. |
| Franquicias del último cierre | `cross_match_result`, `reconciliation_summary` | Monto Transbank y vouchers conciliados por tarjeta. La diferencia viene de `reconciliation_summary` (tabla USD). |
| Excepciones por cajero | `cross_match_result.cashier_username` | Conteo de pendientes por cajero. Los vouchers que no están en el ERP no tienen cajero y se agrupan como "Sin cajero". |
| Excepciones de mayor monto | `cross_match_result` | Las 6 pendientes con mayor `transbank_amount`. |
| Cierres recientes | `reconciliation_process`, `bank_deposit_validation` | Los últimos 6 cierres USD, con % conciliado, estado del abono y enlace al Excel oficial. |

Si no hay ningún cierre USD guardado, se muestra un estado vacío con el botón para cargar los archivos.

### Archivos
- `web/views.py`: `dashboard_view`, etiquetas `EXCEPTION_LABELS`, y `login_view` redirige al Inicio.
- `BankReconciliation/urls.py`: rutas `dashboard` (`/`) y `usd_upload` (`/reconciliation/usd/`).
- `web/templates/dashboard.html` (nuevo).
- `web/templates/base.html`: menú lateral y barra superior.
- `web/static/css/theme.css`: sección "12. Dashboard (Inicio)".

### Pruebas (`web/tests.py` → `DashboardViewTests`)
| Prueba | Qué verifica |
|---|---|
| `test_requires_login` | Sin sesión, `/` redirige al login. |
| `test_empty_state_without_processes` | Con la base vacía, el Inicio responde 200 y muestra el estado vacío. |
