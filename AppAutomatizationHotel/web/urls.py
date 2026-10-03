
from django.contrib import admin
from django.urls import path
from .views import *
from django.conf.urls import handler404
from django.shortcuts import render

def custom_404(request, exception):
    return render(request, '404.html', status=404)

urlpatterns = [
    path('', inicio,name='INICIO'),
    path('procesamiento',procesamiento,name='PR'),
    
    path('login',login,name='LO'),  
    path('cerrar',cerrar_sesion,name='CC'),   
    path('envio_qr',enviar_codigo_qr,name="EQR"),
    path('qr', generar_qr2, name='qr'),
    path('descargar-excel/', descargar_excel_dif, name='descargar_excel'),
    path('descargar-excel-nopresente/', descargar_excel_nopresente, name='descargar_excel_nopresente'),
    
]
