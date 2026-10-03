import re
import os

# 1. FIX VIEWS.PY
views_path = r"c:\Users\sxfav\Desktop\DuocUC\Capstone\HotelAutomatizationFED\AppAutomatizationHotel\web\views.py"
with open(views_path, "r", encoding="utf-8") as f:
    views_content = f.read()

# Remove the bad else
views_content = views_content.replace('    else:\n        print("es invalido")\n\n', '')
views_content = views_content.replace('    else:\n        print("es invalido")\n', '')

# Insert global variable clear at start of POST
if "informacion_proceso = []\n" not in views_content:
    target_post = "    if request.method == 'POST':\n"
    replacement_post = target_post + """
        global informacion_proceso, data_dolar, documento_inf, monto_buscar_inf, diferencia_inf, fecha_inf, tc_inf
        informacion_proceso = []
        data_dolar = []
        documento_inf = []
        monto_buscar_inf = []
        diferencia_inf = []
        fecha_inf = []
        tc_inf = []
"""
    views_content = views_content.replace(target_post, replacement_post)

with open(views_path, "w", encoding="utf-8") as f:
    f.write(views_content)


# 2. UPDATE PROCESAMIENTO.HTML (2-Step UI)
html_path = r"c:\Users\sxfav\Desktop\DuocUC\Capstone\HotelAutomatizationFED\AppAutomatizationHotel\web\templates\procesamiento.html"
with open(html_path, "r", encoding="utf-8") as f:
    html_content = f.read()

# Insert {% if not data_dolar and not data %}
target_tab = """      <div class="tab-pane fade show active" id="pills-usd" role="tabpanel">"""
if "{% if not data_dolar and not data %}" not in html_content:
    replacement_tab = target_tab + """\n\n        {% if not data_dolar and not data %}"""
    html_content = html_content.replace(target_tab, replacement_tab)

# Insert Loading overlay and script
target_overlay = """<!-- CONTENIDO DE LAS PESTAÑAS -->"""
target_overlay_alt = """<!-- CONTENIDO DE LAS PESTAA`AS -->"""
target_overlay_alt2 = """<!-- CONTENIDO DE LAS PESTAAS -->"""
overlay_html = """
<!-- Pantalla de Carga (Loading Overlay) -->
<div id="loadingOverlay" class="d-none position-fixed top-0 start-0 w-100 h-100 bg-white" style="z-index: 9999; opacity: 0.95;">
    <div class="d-flex flex-column justify-content-center align-items-center h-100">
        <div class="spinner-border text-primary mb-4" style="width: 5rem; height: 5rem; border-width: 0.5rem;" role="status"></div>
        <h2 class="text-primary fw-bold">Procesando Archivos...</h2>
        <p class="text-muted fs-5 mt-2">Cruzando transacciones de Transbank con el ERP.<br>Esto puede tomar unos segundos, por favor espera.</p>
    </div>
</div>

<!-- CONTENIDO DE LAS PESTAÑAS -->"""
if "loadingOverlay" not in html_content:
    if target_overlay in html_content:
        html_content = html_content.replace(target_overlay, overlay_html)
    elif target_overlay_alt in html_content:
        html_content = html_content.replace(target_overlay_alt, overlay_html)
    elif target_overlay_alt2 in html_content:
        html_content = html_content.replace(target_overlay_alt2, overlay_html)

# Add showLoading to form
target_form = """<form action="{% url 'PR' %}" method="post" enctype="multipart/form-data">"""
target_form2 = """<form action="{% url 'PR' %}" method="post" enctype="multipart/form-data" id="mainForm">"""
if "showLoading()" not in html_content:
    if target_form2 in html_content:
        html_content = html_content.replace(target_form2, """<form action="{% url 'PR' %}" method="post" enctype="multipart/form-data" id="mainForm" onsubmit="showLoading()">""")
    else:
        html_content = html_content.replace(target_form, """<form action="{% url 'PR' %}" method="post" enctype="multipart/form-data" onsubmit="showLoading()">""")

# Replace {% if data_dolar or data %} with {% else %}
target_results = """        <!-- SECCIA"N DE RESULTADOS (SOLO APARECE SI HAY DATOS) -->
        {% if data_dolar or data %}"""
replacement_results = """        {% else %}
        <!-- SECCIÓN DE RESULTADOS (PASO 2) -->"""
html_content = html_content.replace(target_results, replacement_results)
target_results_alt = """        <!-- SECCI\u00d3N DE RESULTADOS (SOLO APARECE SI HAY DATOS) -->
        {% if data_dolar or data %}"""
html_content = html_content.replace(target_results_alt, replacement_results)
target_results_alt2 = """        <!-- SECCIN DE RESULTADOS (SOLO APARECE SI HAY DATOS) -->
        {% if data_dolar or data %}"""
html_content = html_content.replace(target_results_alt2, replacement_results)

# Insert Volver a empezar
target_end = """        </div>
        {% endif %}

      </div>"""
replacement_end = """        </div>
        
        <div class="text-center mt-5 mb-3">
            <a href="{% url 'PR' %}" class="btn btn-primary px-5 py-3 shadow fs-5 rounded-pill"><i class="bi bi-arrow-repeat me-2"></i> Volver a Empezar (Nueva Conciliación)</a>
        </div>

        {% endif %}

      </div>"""
if "Volver a Empezar" not in html_content:
    html_content = html_content.replace(target_end, replacement_end)

# Add JS script logic
target_js = """// Función para cambiar entre modos (Toggle)"""
target_js_alt = """// FunciA3n para cambiar entre modos (Toggle)"""
target_js_alt2 = """// Funcin para cambiar entre modos (Toggle)"""
js_func = """function showLoading() {
    document.getElementById('loadingOverlay').classList.remove('d-none');
  }

  // Función para cambiar entre modos (Toggle)"""
if "showLoading()" not in html_content:
    if target_js in html_content:
        html_content = html_content.replace(target_js, js_func)
    elif target_js_alt in html_content:
        html_content = html_content.replace(target_js_alt, js_func)
    elif target_js_alt2 in html_content:
        html_content = html_content.replace(target_js_alt2, js_func)

# Table coloring logic
js_color = """
  // Formateo y Coloreado de Tablas
  document.addEventListener("DOMContentLoaded", function() {
    const tables = document.querySelectorAll("table");
    tables.forEach(table => {
        const rows = table.querySelectorAll("tbody tr");
        rows.forEach(row => {
            const cells = row.querySelectorAll("td");
            if (cells.length === 4) {
                const diffCell = cells[3];
                const valStr = diffCell.innerText.trim().replace(/[^0-9.-]+/g,"");
                const val = parseFloat(valStr);
                if (!isNaN(val)) {
                    if (val === 0) {
                        diffCell.classList.add("text-success", "fw-bold");
                        diffCell.innerHTML = "<i class='bi bi-check-circle-fill'></i> 0";
                    } else {
                        diffCell.classList.add("text-danger", "fw-bold");
                    }
                }
            }
        });
    });
  });
</script>"""
target_script_end = "</script>"
if "Formateo y Coloreado de Tablas" not in html_content:
    # replace only the LAST occurrence of </script>
    parts = html_content.rsplit("</script>", 1)
    html_content = js_color.join(parts)

with open(html_path, "w", encoding="utf-8") as f:
    f.write(html_content)

print("Both files repaired.")
