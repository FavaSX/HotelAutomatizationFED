import os

views_path = r"c:\Users\sxfav\Desktop\DuocUC\Capstone\HotelAutomatizationFED\AppAutomatizationHotel\web\views.py"
services_path = r"c:\Users\sxfav\Desktop\DuocUC\Capstone\HotelAutomatizationFED\AppAutomatizationHotel\web\servicios_financieros.py"

with open(views_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

start_idx = -1
end_idx = -1
for i, line in enumerate(lines):
    if "abonado = 0" in line and start_idx == -1:
        start_idx = i
    if "return render(request,\"procesamiento.html\",contexto)" in line and start_idx != -1:
        end_idx = i
        break

extracted_lines = lines[start_idx:end_idx]

# Clean up context assignments in extracted lines
cleaned_extracted_lines = []
for line in extracted_lines:
    if 'contexto["data"]=diccionario_temp' in line:
        pass # omit
    elif 'contexto["data_dolar"]=diccionario_dolar' in line:
        pass # omit
    else:
        # Dedent by 4 spaces since it will go inside `def procesar_archivos:`
        # Actually, let's keep the indentation mostly as is, or dedent if it starts with 8 spaces.
        if line.startswith("        "):
            cleaned_extracted_lines.append(line[4:])
        elif line.startswith("    "):
            # already 4 spaces, keep as is
            cleaned_extracted_lines.append(line)
        else:
            cleaned_extracted_lines.append(line)

services_content = """import pandas as pd
import math

informacion_proceso = []
documento_inf = []
monto_buscar_inf = []
diferencia_inf = []
fecha_inf = []
tc_inf = []
data_dolar = []

def procesar_archivos(archivo1, archivo2, archivo3, archivo4, archivo5, archivo6, archivo7):
    global informacion_proceso, data_dolar, documento_inf, monto_buscar_inf, diferencia_inf, fecha_inf, tc_inf
    informacion_proceso.clear()
    data_dolar.clear()
    documento_inf.clear()
    monto_buscar_inf.clear()
    diferencia_inf.clear()
    fecha_inf.clear()
    tc_inf.clear()

    diccionario = {}
    suma_amex=0
    suma_dinners=0
    suma_mc=0
    suma_master_card=0
    cant_master_card=0
    suma_visa=0
    i=0
    dic_amex=[]
    ws = 0
    diccionario_temp = []
    diccionario_dolar = []

""" + "".join(cleaned_extracted_lines) + """
    return diccionario_temp, diccionario_dolar, ws, abonado
"""

with open(services_path, "w", encoding="utf-8") as f:
    f.write(services_content)

# Now rebuild views.py
new_views = []
# Insert the import at the top
inserted_import = False

skip = False
for i, line in enumerate(lines):
    # Skip the old globals
    if line.startswith("documento_inf=[]") or line.startswith("monto_buscar_inf=[]") or \
       line.startswith("diferencia_inf=[]") or line.startswith("fecha_inf=[]") or \
       line.startswith("tc_inf=[]") or line.startswith("informacion_proceso=[]") or \
       line.startswith("data_dolar=[]"):
        continue

    if not inserted_import and line.startswith("from django.shortcuts import render"):
        new_views.append(line)
        new_views.append("from .servicios_financieros import procesar_archivos, informacion_proceso\n")
        inserted_import = True
        continue

    if line.startswith("def procesamiento(request):"):
        new_views.append(line)
        new_views.append("    contexto={}\n")
        continue
    
    # Skip everything we moved
    if "diccionario = {}" in line: skip = True
    if "if request.method == 'POST':" in line: skip = False
    
    if skip: continue

    # Clean the `global` declaration in views.py
    if "global informacion_proceso, data_dolar" in line or \
       "informacion_proceso = []" in line or "data_dolar = []" in line or \
       "documento_inf = []" in line or "monto_buscar_inf = []" in line or \
       "diferencia_inf = []" in line or "fecha_inf = []" in line or "tc_inf = []" in line:
        continue

    if i == start_idx:
        # Insert the call
        call_code = """
        diccionario_temp, diccionario_dolar, ws, abonado = procesar_archivos(archivo1, archivo2, archivo3, archivo4, archivo5, archivo6, archivo7)
        contexto["data"] = diccionario_temp
        contexto["data_dolar"] = diccionario_dolar
        contexto["ws"] = ws
        contexto["abonado"] = abonado
"""
        new_views.append(call_code)
        continue

    if start_idx < i < end_idx:
        continue

    new_views.append(line)

with open(views_path, "w", encoding="utf-8") as f:
    f.writelines(new_views)

print("Refactor complete.")
