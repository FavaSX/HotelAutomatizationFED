import re
file_path = r"c:\Users\sxfav\Desktop\DuocUC\Capstone\HotelAutomatizationFED\AppAutomatizationHotel\web\views.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

content = content.replace("    asyncio.run(obtener_datos())", "    # asyncio.run(obtener_datos())  # Desactivado temporalmente para no ralentizar la carga")

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)
print("Speed fix applied.")
