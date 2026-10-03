import re

def clean_views():
    with open('web/views.py', 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Clean inicio
    inicio_old = """def inicio(request):
    request.session["datos"]=""
    x={}

    # x["valor"]=request.session["datos"]
    # x["habitacion"]=habi(0)
    comentarios=Comentario.objects.all()
    mensaje={'comentarios':comentarios}
    return render(request,"index.html",mensaje)"""
    
    inicio_new = """def inicio(request):
    return render(request, "index.html")"""
    content = content.replace(inicio_old, inicio_new)

    # 2. Clean cerrar_sesion
    cerrar_old = """def cerrar_sesion(request):
    contex = {}
    logout(request)
    return render(request,"index.html",contex)"""
    
    cerrar_new = """def cerrar_sesion(request):
    logout(request)
    return redirect('INICIO')"""
    content = content.replace(cerrar_old, cerrar_new)

    # 3. Remove descargar_excel_ant and descargar_excel (since it is unused, the URL uses descargar_excel_dif)
    content = re.sub(r'data = \{\}\ndef descargar_excel_ant\(request\):.*?return response\n*', '', content, flags=re.DOTALL)
    content = re.sub(r'# descargar excel con titulo en la primera fila\n@login_required\(login_url=\'LO\'\)\ndef descargar_excel\(request\):.*?return response\n*', '', content, flags=re.DOTALL)

    # 4. Remove QR functions (generar_qr2, enviar_codigo_qr)
    content = re.sub(r'def generar_qr2\(req\):.*?return HttpResponse\(img_bytes, content_type=\'image/png\'\)\n*', '', content, flags=re.DOTALL)
    content = re.sub(r'def enviar_codigo_qr\(request,clave,correo\):.*?return 1\n*', '', content, flags=re.DOTALL)

    # 5. Remove unused imports
    imports_to_remove = [
        "import qrcode\n",
        "from PIL import Image,ImageDraw\n",
        "from django.core.mail import EmailMessage\n",
        "from django.core.files.base import ContentFile\n"
    ]
    for imp in imports_to_remove:
        content = content.replace(imp, "")

    with open('web/views.py', 'w', encoding='utf-8') as f:
        f.write(content)

clean_views()
print("Cleaning complete")
