import re

file_path = r"c:\Users\sxfav\Desktop\DuocUC\Capstone\HotelAutomatizationFED\AppAutomatizationHotel\web\views.py"

with open(file_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

new_lines = []
in_runaway_block = False

for line in lines:
    if line.startswith("    diccionario_dolar=["):
        in_runaway_block = True
    
    if in_runaway_block:
        if line.startswith("    return render(request,\"procesamiento.html\",contexto)"):
            in_runaway_block = False
            new_lines.append(line)
        else:
            if line.strip() == "":
                new_lines.append("\n")
            elif line.startswith("    "):
                # Indent by 4 more spaces
                new_lines.append("    " + line)
            else:
                # Should not happen unless there's a dedent, but just in case
                new_lines.append("    " + line)
    else:
        new_lines.append(line)

with open(file_path, "w", encoding="utf-8") as f:
    f.writelines(new_lines)

print("Indentation fixed.")
