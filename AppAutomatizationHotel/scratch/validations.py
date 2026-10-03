import re
import os

views_path = r"c:\Users\sxfav\Desktop\DuocUC\Capstone\HotelAutomatizationFED\AppAutomatizationHotel\web\views.py"
with open(views_path, "r", encoding="utf-8") as f:
    views_content = f.read()

target_files = "archivo7 = request.FILES.get('archivo7') # transbank \n"
replacement_files = target_files + """    
        # Validacion del backend: todos los archivos son obligatorios
        if not all([archivo1, archivo2, archivo3, archivo4, archivo5, archivo6, archivo7]):
            contexto["error_validacion"] = "Faltan archivos. El sistema requiere obligatoriamente los 7 archivos (ERP, Transbank, Banco, Amex, Diners, Visa, Mastercard) para realizar el cruce."
            return render(request, "procesamiento.html", contexto)
"""
if "Validacion del backend" not in views_content:
    views_content = views_content.replace(target_files, replacement_files)
    with open(views_path, "w", encoding="utf-8") as f:
        f.write(views_content)


html_path = r"c:\Users\sxfav\Desktop\DuocUC\Capstone\HotelAutomatizationFED\AppAutomatizationHotel\web\templates\procesamiento.html"
with open(html_path, "r", encoding="utf-8") as f:
    html_content = f.read()

# Replace the form onsubmit
target_onsubmit = """onsubmit="showLoading()">"""
replacement_onsubmit = """onsubmit="return validateAndShowLoading()">"""
if "validateAndShowLoading" not in html_content:
    html_content = html_content.replace(target_onsubmit, replacement_onsubmit)

# Add the JS logic
target_js = """function showLoading() {"""
replacement_js = """function validateAndShowLoading() {
    const requiredFiles = ['archivo1', 'archivo2', 'archivo3', 'archivo4', 'archivo5', 'archivo6', 'archivo7'];
    const fileNames = ['ERP', 'Amex', 'Diners', 'Visa', 'MasterCard', 'Banco', 'Transbank'];
    
    for (let i = 0; i < requiredFiles.length; i++) {
        const input = document.getElementById(requiredFiles[i]);
        if (!input.files || input.files.length === 0) {
            alert("A\u00fan no has seleccionado el archivo para: " + fileNames[i] + ".\\nPor favor, aseg\u00farate de cargar los 7 archivos antes de procesar para evitar errores en la conciliaci\u00f3n.");
            return false; // Prevent form from submitting
        }
    }
    
    document.getElementById('loadingOverlay').classList.remove('d-none');
    return true;
  }

  function showLoading() {"""
if "validateAndShowLoading() {" not in html_content:
    html_content = html_content.replace(target_js, replacement_js)

# Insert HTML alert for backend validation (just below the toggle switch or inside the form)
target_alert = """<form action="{% url 'PR' %}" """
replacement_alert = """
          {% if error_validacion %}
            <div class="alert alert-danger shadow-sm border-0 d-flex align-items-center mb-4" role="alert">
              <i class="bi bi-shield-fill-exclamation fs-3 me-3"></i>
              <div>
                <strong>\u00a1Error de Validaci\u00f3n!</strong><br>
                {{ error_validacion }}
              </div>
            </div>
          {% endif %}
          
          <form action="{% url 'PR' %}" """
if "error_validacion" not in html_content:
    html_content = html_content.replace(target_alert, replacement_alert)

with open(html_path, "w", encoding="utf-8") as f:
    f.write(html_content)

print("Validations implemented.")
