from django.shortcuts import render
from .servicios_financieros import procesar_archivos, informacion_proceso
from .i18n import *

from .models import *
# importar el modelo de tabla User 
from django.contrib.auth.models import User,Group
# importar librerias que permitan la validacion del login
from django.contrib.auth import authenticate,logout,login as login_aut
# importar libreria decoradora que permite evitar el ingreso de usuarios a las paginas web
from django.contrib.auth.decorators import login_required, permission_required

from django.shortcuts import redirect
from datetime import datetime, timedelta
import uuid
import math

from openpyxl import Workbook,load_workbook
from django.http import HttpResponse
from io import BytesIO

import os
import time

from io import BytesIO
from django.core.files import File
from django.http import HttpResponse
import pandas as pd
# Create your views here.
import httpx
import asyncio
from datetime import datetime




async def obtener_datos():
    async with httpx.AsyncClient() as client:
        response = await client.get("https://mindicador.cl/api/dolar")  # Método GET
        response.raise_for_status()  # Lanza error si hay problema (4xx o 5xx)
        datos = response.json()      # Convierte respuesta JSON en dict de Python
        for item in datos['serie']:
            fecha_original = item["fecha"]
            fecha_obj = datetime.fromisoformat(fecha_original.replace("Z", "+00:00"))
            fecha_formateada = fecha_obj.strftime("%d-%m-%Y")
            #print(f"Fecha: {fecha_formateada}, Valor: {item['valor']}")
            fila={"fecha":fecha_formateada,"valor":item['valor']}
            data_dolar.append(fila)
        print(data_dolar)
        


def inicio(request):
    return render(request, "index.html")

@login_required(login_url='LO')
def procesamiento(request):
    contexto={}
    # asyncio.run(obtener_datos())  # Desactivado temporalmente para no ralentizar la carga
    # definicion de variables que capturan las sumas por tipo de tarjeta
    if request.method == 'POST':

    
        archivo1 = request.FILES.get('archivo1') # ERP
        archivo2 = request.FILES.get('archivo2') # amex
        archivo3 = request.FILES.get('archivo3') # dinners
        archivo4 = request.FILES.get('archivo4') # visa dolar
        archivo5 = request.FILES.get('archivo5') # mastercard
        archivo6 = request.FILES.get('archivo6') # banco
        archivo7 = request.FILES.get('archivo7') # transbank 
    
        # Validacion del backend: todos los archivos son obligatorios
        if not all([archivo1, archivo2, archivo3, archivo4, archivo5, archivo6, archivo7]):
            contexto["error_validacion"] = "Faltan archivos. El sistema requiere obligatoriamente los 7 archivos (ERP, Transbank, Banco, Amex, Diners, Visa, Mastercard) para realizar el cruce."
            return render(request, "procesamiento.html", contexto)
    

        diccionario_temp, diccionario_dolar, ws, abonado = procesar_archivos(archivo1, archivo2, archivo3, archivo4, archivo5, archivo6, archivo7)
        contexto["data"] = diccionario_temp
        contexto["data_dolar"] = diccionario_dolar
        contexto["ws"] = ws
        contexto["abonado"] = abonado
