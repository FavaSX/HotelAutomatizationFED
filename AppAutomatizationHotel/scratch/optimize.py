import re

file_path = r"c:\Users\sxfav\Desktop\DuocUC\Capstone\HotelAutomatizationFED\AppAutomatizationHotel\web\servicios_financieros.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# Define the loading block
loading_block = """    # Optimizaci\u00f3n: Cargar los 7 archivos a memoria una sola vez
    print("Cargando archivos a memoria RAM...")
    
    # Manejar las posiciones del archivo (volver a 0 si ya se ley\u00f3)
    for f in [archivo1, archivo2, archivo3, archivo4, archivo5, archivo6, archivo7]:
        if hasattr(f, 'seek'):
            f.seek(0)
            
    df_erp = pd.read_excel(archivo1, usecols=[1,2,3,4,5,6,7,8,9,10], nrows=1500)
    df_amex = pd.read_excel(archivo2, usecols=[1,2,3,4,5,6,7,8,9,10,11,12,13,14], nrows=2500, engine='xlrd')
    df_diners = pd.read_excel(archivo3, usecols=[1,2,3,4,5,6,7,8,9,10,11,12,13,14], nrows=2500, engine='xlrd')
    df_visa = pd.read_excel(archivo4, usecols=[1,2,3,4,5,6,7,8,9,10,11,12,13,14], nrows=2500, engine='xlrd')
    df_mc = pd.read_excel(archivo5, usecols=[1,2,3,4,5,6,7,8,9,10,11,12,13,14], nrows=2500, engine='xlrd')
    df_banco = pd.read_excel(archivo6, usecols=[1,2,3,4,5,6,7,8], nrows=1500)
    df_transbank = pd.read_excel(archivo7, usecols=[1,2,3,4,5,6,7,8,9,10], nrows=2500)
    
    print("Archivos cargados. Iniciando procesamiento en memoria...")

    diccionario = {}"""

# Insert the loading block where `diccionario = {}` is
content = content.replace("    diccionario = {}", loading_block)

# Replace all the individual pd.read_excel calls!
replacements = {
    r"pd\.read_excel\(archivo7,usecols=\[1,2,3,4,5,6,7,8,9,10\], nrows=2500\)": "df_transbank",
    r"pd\.read_excel\(archivo7,usecols=\[1,2,3,4,5,6,7,8,9,10\], nrows=1500\)": "df_transbank",
    
    r"pd\.read_excel\(archivo6,usecols=\[1,2,3,4,5,6,7,8\], nrows=1500\)": "df_banco",
    
    r"pd\.read_excel\(archivo1,usecols=\[1,2,3,4,5,6,7,8,9,10\], nrows=1500\)": "df_erp",
    
    r"pd\.read_excel\(archivo2,usecols=\[1,2,3,4,5,6,7,8,9,10,11,12,13,14\], nrows=2500,engine='xlrd'\)": "df_amex",
    r"pd\.read_excel\(archivo2,usecols=\[1,2,3,4,5,6,7,8,9,10,11,12,13,14\], nrows=2500\)": "df_amex",
    
    r"pd\.read_excel\(archivo3,usecols=\[1,2,3,4,5,6,7,8,9,10,11,12,13,14\], nrows=2500,engine='xlrd'\)": "df_diners",
    r"pd\.read_excel\(archivo3,usecols=\[1,2,3,4,5,6,7,8,9,10,11,12,13,14\], nrows=2500\)": "df_diners",
    
    r"pd\.read_excel\(archivo4,usecols=\[1,2,3,4,5,6,7,8,9,10,11,12,13,14\], nrows=2500,engine='xlrd'\)": "df_visa",
    r"pd\.read_excel\(archivo4,usecols=\[1,2,3,4,5,6,7,8,9,10,11,12,13,14\], nrows=2500\)": "df_visa",
    
    r"pd\.read_excel\(archivo5,usecols=\[1,2,3,4,5,6,7,8,9,10,11,12,13,14\], nrows=2500,engine='xlrd'\)": "df_mc",
    r"pd\.read_excel\(archivo5,usecols=\[1,2,3,4,5,6,7,8,9,10,11,12,13,14\], nrows=2500\)": "df_mc",
}

for pattern, replacement in replacements.items():
    content = re.sub(pattern, replacement, content)

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("Optimized!")

