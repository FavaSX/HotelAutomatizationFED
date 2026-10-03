import pandas as pd
import math

informacion_proceso = []
documento_inf = []
monto_buscar_inf = []
diferencia_inf = []
fecha_inf = []
tc_inf = []
data_dolar = []


def check_card(df_tarjeta, codigo_erp, monto_original, valor_erp, c0, c2, fecha_venta, tipo_tarjeta, nombre_tarjeta):
    find_dinners = df_tarjeta[df_tarjeta['Unnamed: 2'] == codigo_erp]
    c1 = codigo_erp
    c3, c4, c7, c8 = '', '', '', ''
    
    if len(find_dinners) > 0:
        encontro = 0
        mo, om = 0, 0
        for fila_dinners in find_dinners.index:
            otra_moneda = find_dinners.at[fila_dinners, 'Unnamed: 12']
            saldo = find_dinners.at[fila_dinners, 'Unnamed: 13']
            saldo_corregido = find_dinners.at[fila_dinners, 'Unnamed: 14']
            
            if monto_original == otra_moneda:
                c7 = f"El monto original {monto_original} coincide con OTRA MONEDA {otra_moneda}"
                c3 = otra_moneda
                    
            if saldo == valor_erp:
                c8 = f"El valor ERP {valor_erp} coincide con SALDO {saldo}"
                mo, om = monto_original, otra_moneda                                   
                encontro = 1
                break
            elif valor_erp == saldo_corregido:
                c8 = f"El valor ERP {valor_erp} coincide con SALDO CORREGIDO {saldo_corregido}"
                mo, om = monto_original, otra_moneda
                encontro = 1
                break
                
        if encontro == 0:
            c7 = f"NO ENCONTRO EL MONTO ORIGINAL {monto_original} EN NINGUNA COLUMNA DE {nombre_tarjeta}"       
        else:
            diferencia = mo - om 
            c4 = diferencia 
            c3 = om  
            
    return {
        "codigo_autorizacion": c0,
        "documento": c1,
        "monto_original": c2,
        "monto_transbank": c3,
        "diferencia": c4,
        "fecha_venta": fecha_venta,
        "tipo_tarjeta": tipo_tarjeta,
        "status": c7,
        "observacion": c8,
        "extra": "OK"
    }

def procesar_archivos(archivo1, archivo2, archivo3, archivo4, archivo5, archivo6, archivo7):
    global informacion_proceso, data_dolar, documento_inf, monto_buscar_inf, diferencia_inf, fecha_inf, tc_inf
    informacion_proceso.clear()
    data_dolar.clear()
    documento_inf.clear()
    monto_buscar_inf.clear()
    diferencia_inf.clear()
    fecha_inf.clear()
    tc_inf.clear()

    print("Cargando archivos a memoria RAM...")
    
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

    abonado = 0
    
    if archivo6:
        print("ES VALIDO ARCHIVO 6")
        ws=0
        abono_dolar=df_transbank   # transbank         
        for fila in abono_dolar.index:
            cuenta = abono_dolar.at[fila, 'Unnamed: 1']   
            if cuenta == "Abono Calculado (=):":
                monto = abono_dolar.at[fila, 'Unnamed: 2']
                pass # contexto removed
                banco=df_banco   # transbank                 
                find_banco = banco[banco['Unnamed: 8'] == monto]
                if len(find_banco)>0:
                    abonado = monto
                    pass # contexto removed
                    ws=1
                    break
                
        if abonado==0:
            pass # contexto removed
        pass # contexto removed
    if archivo7:
        print("ES VALIDO ARCHIVO 7")
        
        abono_dolar=df_transbank   # transbank         
        erp=df_erp  # erp
         
        for fila in abono_dolar.index:
            c0='' # codigo autorizacion
            c1='' # documento
            c2='' # monto abono
            c3='' # monto transbank
            c4='' # diferencia
            c5='' # fecha
            c6='' # tipo tarjeta
            c7='' # status
            c8='' # observacion 
            c9='' # extra
                           
            i+= 1                
            texto = f"{i} - {abono_dolar.at[fila, 'Unnamed: 3']} - {abono_dolar.at[fila, 'Unnamed: 6']} - {abono_dolar.at[fila,'Unnamed: 7']}"
            tipo_tarjeta = abono_dolar.at[fila, 'Unnamed: 3']                
            monto_original = abono_dolar.at[fila, 'Unnamed: 6']
            codigo_autorizacion = abono_dolar.at[fila, 'Unnamed: 7']
            fecha_venta = abono_dolar.at[fila, 'Unnamed: 2']
            
            if pd.isna(codigo_autorizacion) or not tipo_tarjeta in ["AX","DI","VI","MC"]: 
                continue
            
            erp['Unnamed: 9'] = erp['Unnamed: 9'].fillna(0)
            erp['Unnamed: 9'] = erp['Unnamed: 9'].astype(str).str.strip()
            find_erp = erp[erp['Unnamed: 9'].str.contains(str(codigo_autorizacion), na=False,case=False)]
            
            cantidad_filas = len(find_erp)
            c0=codigo_autorizacion
            
            if cantidad_filas>0:
                c2=monto_original
                
                codigo_erp = find_erp.iloc[0]['Unnamed: 3']
                valor_erp = abs(int(find_erp.iloc[0]['Unnamed: 8']))                                                        
                
                if "DI" in tipo_tarjeta:
                    reg = check_card(df_diners, codigo_erp, monto_original, valor_erp, c0, c2, fecha_venta, tipo_tarjeta, "DINNERS")
                    informacion_proceso.append(reg)
                elif "VI" in tipo_tarjeta:
                    reg = check_card(df_visa, codigo_erp, monto_original, valor_erp, c0, c2, fecha_venta, tipo_tarjeta, "VISA")
                    informacion_proceso.append(reg)
                elif "MC" in tipo_tarjeta:
                    reg = check_card(df_mc, codigo_erp, monto_original, valor_erp, c0, c2, fecha_venta, tipo_tarjeta, "MASTER CARD")
                    informacion_proceso.append(reg)
                elif "AX" in tipo_tarjeta:
                    reg = check_card(df_amex, codigo_erp, monto_original, valor_erp, c0, c2, fecha_venta, tipo_tarjeta, "AMERICAN EXPRESS")
                    informacion_proceso.append(reg)
                
            else:
                c7=f"NO ENCONTRO EL CODIGO : {codigo_autorizacion} CUYO VALOR ES DE {monto_original} "
                c6=tipo_tarjeta
                c0=codigo_autorizacion
                c2=monto_original
                c5=fecha_venta
                c9="NO"
                reg={"codigo_autorizacion":c0,"documento":c1,"monto_original":c2,"monto_transbank":c3,"diferencia":c4,"fecha_venta":fecha_venta,"tipo_tarjeta":tipo_tarjeta,"status":c7,"observacion":c8,"extra":c9}   
                informacion_proceso.append(reg)
                
    print("fin proceso ERP con Transbank")
    print("-----------------------------------") 
       
    if archivo1:
        print("ES VALIDO")
        x=df_erp
        
        print("Valores Archivo ERP -----------------------------------")            

        
        diccionario = {}
        i=0
        for fila in x.index:
            
            i+= 1
            texto = f"{i} - {x.at[fila, 'Unnamed: 3']} - {x.at[fila, 'Unnamed: 5']} - {x.at[fila,'Unnamed: 8']}"

            if "US$" in texto:                    
                codigo_buscar = x.at[fila, 'Unnamed: 3']
                valor_buscado = abs(int(x.at[fila, 'Unnamed: 8']))
                
                fecha_erp = x.at[fila, 'Unnamed: 10']
                if (x.at[fila, 'Unnamed: 8'])<0:
                    if "Amex US$" in texto:
                        dic_amex.append({"codigo":codigo_buscar,"valor":valor_buscado,"ERP":x.at[fila, 'Unnamed: 8']})
                        suma_amex = suma_amex + abs(int(x.at[fila, 'Unnamed: 8']))
                        
                    if "Dinners US$" in texto:
                        suma_dinners = suma_dinners + abs(int(x.at[fila, 'Unnamed: 8']))
                        
                    if "Master Card US$" in texto:
                        cant_master_card = cant_master_card +1
                        suma_master_card = suma_master_card + abs(int(x.at[fila, 'Unnamed: 8']))
                        
                    if "Visa US$" in texto:
                        suma_visa = suma_visa + abs(int(x.at[fila, 'Unnamed: 8']))
                    
                if "Amex US$" in texto:
                    cantidad=0
                    suma = 0
                    if archivo2:
                        amex=df_amex
                        i_amex=0
                        sw=0
                        for fila_amex in amex.index:
                            codigo = amex.at[fila_amex, 'Unnamed: 2']
                            i_amex = i_amex +1
                            texto_amex = f"{i_amex} - {amex.at[fila_amex, 'Unnamed: 2']} - {amex.at[fila_amex, 'Unnamed: 12']} - {amex.at[fila_amex,'Unnamed: 13']} - {amex.at[fila_amex,'Unnamed: 14']}"
                            if codigo_buscar==codigo:
                                cantidad = cantidad +1
                                sw_e = 0
                                if not math.isnan(amex.at[fila_amex, 'Unnamed: 12']): 
                                    if valor_buscado==abs(int(amex.at[fila_amex, 'Unnamed: 12'])):
                                        sw_e = 1
                                        suma = suma + abs(int(amex.at[fila_amex, 'Unnamed: 12']))
                                        
                                if not math.isnan(amex.at[fila_amex, 'Unnamed: 13']):                                     
                                    if valor_buscado==abs(int(amex.at[fila_amex, 'Unnamed: 13'])):
                                        sw_e = 1
                                        suma = suma + abs(int(amex.at[fila_amex, 'Unnamed: 13']))
                                        
                                if not math.isnan(amex.at[fila_amex, 'Unnamed: 14']): 
                                    if valor_buscado==abs(int(amex.at[fila_amex, 'Unnamed: 14'])):
                                        sw_e = 1
                                        suma = suma + abs(int(amex.at[fila_amex, 'Unnamed: 14'])) 
                                if sw_e == 0:
                                    print(f"\033[31m NO ESTA PRESENTE EL VALOR EN NINGUNA COLUMNA (amex)...CODIGO {codigo_buscar}..Valor:{valor_buscado}.. \033[0m")                                                                               
                                sw=1
                                diccionario[f"{i}"]={"tarjeta":"Amex US$","codigo":abs(int(codigo_buscar)),"Valor":valor_buscado,"Status":"encontrado"}
                                continue
                        if(sw==0):
                            tc_inf.append("Amex US$")
                            documento_inf.append(abs(int(codigo_buscar)))
                            monto_buscar_inf.append(valor_buscado)
                            fecha_inf.append(fecha_erp)
                            diccionario[f"{i}"]={"tarjeta":"Amex US$","codigo":abs(int(codigo_buscar)),"Valor":valor_buscado,"Status":"no encontrado"}                                               
                
                                        
                                        
                
                
                if "Master Card US$" in texto:
                    cantidad=0
                    if archivo5:
                        mc=df_mc
                        i_mc=0
                        sw=0
                        sw_e = 0
                        om = 0
                        sal = 0
                        sal_corr = 0    
                                                        
                        filtro = mc['Unnamed: 2'] == codigo_buscar
                        
                        if not filtro.any():
                            tc_inf.append("Master Card US$")
                            documento_inf.append(abs(int(codigo_buscar)))
                            monto_buscar_inf.append(valor_buscado)
                            fecha_inf.append(fecha_erp)
                            diccionario[f"{i}"]={"tarjeta":"Master Card US$","codigo":abs(int(codigo_buscar)),"Valor":valor_buscado,"Status":"no encontrado"}                                             
                            continue
                        
                        cantidad_filas = len(mc.index[filtro])
                        
                        paso = 0
                        
                        for fila_mc in mc.index[filtro]:
                            paso = paso + 1
                            codigo = mc.at[fila_mc, 'Unnamed: 2'] # Numeros de Documentos recuperados desde archivo "Master Card"
                            if pd.isna(codigo):
                                continue
                            i_mc = i_mc +1
                            texto_mc = f"{i_mc} -Documento: {mc.at[fila_mc, 'Unnamed: 2']} - Otra Moneda: {mc.at[fila_mc, 'Unnamed: 12']} - Saldo: {mc.at[fila_mc,'Unnamed: 13']} - Saldo Corregido:{mc.at[fila_mc,'Unnamed: 14']}"
                            if codigo_buscar==codigo:
                                cantidad = cantidad +1
                                if not math.isnan(mc.at[fila_mc, 'Unnamed: 12']): 
                                    if valor_buscado==abs(int(mc.at[fila_mc, 'Unnamed: 12'])):
                                        sw_e = 1
                                        om = abs(int(mc.at[fila_mc, 'Unnamed: 12']))
                                        suma_mc = suma_mc + abs(int(mc.at[fila_mc, 'Unnamed: 12']))                                            
                                        
                                if not math.isnan(mc.at[fila_mc, 'Unnamed: 13']):                                     
                                    if valor_buscado==abs(int(mc.at[fila_mc, 'Unnamed: 13'])):
                                        sw_e = 1
                                        sal = abs(int(mc.at[fila_mc, 'Unnamed: 13']))
                                        suma_mc = suma_mc + abs(int(mc.at[fila_mc, 'Unnamed: 13']))
                                        
                                if not math.isnan(mc.at[fila_mc, 'Unnamed: 14']): 
                                    if valor_buscado==abs(int(mc.at[fila_mc, 'Unnamed: 14'])):
                                        sw_e = 1
                                        sal_corr = abs(int(mc.at[fila_mc, 'Unnamed: 14']))
                                        suma_mc = suma_mc + abs(int(mc.at[fila_mc, 'Unnamed: 14']))
                                sw=1
                                diccionario[f"{i}"]={"tarjeta":"Master Card US$","codigo":abs(int(codigo_buscar)),"Valor":valor_buscado,"Status":"encontrado"}
                        if(paso==cantidad_filas) and (sw_e==0):
                            tc_inf.append("Master Card US$")
                            documento_inf.append(abs(int(codigo_buscar)))
                            monto_buscar_inf.append(valor_buscado)
                            fecha_inf.append(fecha_erp)
                            diccionario[f"{i}"]={"tarjeta":"Master Card US$","codigo":abs(int(codigo_buscar)),"Valor":valor_buscado,"Status":"no encontrado"}                                             

                                        
                                        

            continue
            clave = x.at[fila, 'Unnamed: 3']
            valor = x.at[fila, 'Unnamed: 6']
            if pd.isna(clave):
                continue
            if x.at[fila, 'Unnamed: 3'] in diccionario:
                diccionario[x.at[fila, 'Unnamed: 3']] = diccionario[x.at[fila, 'Unnamed: 3']] + [x.at[fila, 'Unnamed: 6']]        
            else:
                if str(valor).isnumeric():
                    diccionario[str(clave)] = [valor]      
            
            print(texto)
            i += 1
    
    print("TERMINO PROCESO ERP")
    print("-----------------------------------")
    print(f"Total Master Card US$ Encontrado en ERP: {suma_mc}")
    
    if archivo7:
            TRANS   =df_transbank #Archivo Transbank
            diccionario_2 = {}
            i2=0
            for fila in TRANS.index:
                i2+= 1
                fecha = TRANS.at[fila, 'Unnamed: 2']
                tipo_tarjeta = TRANS.at[fila, 'Unnamed: 3']
                monto_original = TRANS.at[fila, 'Unnamed: 6']
                codigo_aut = TRANS.at[fila, 'Unnamed: 7']
                texto = f"{i2} - {fecha} - {tipo_tarjeta} - {monto_original} - {codigo_aut}"
                ERP=df_erp #Archivo ERP
                sw=0
                for fila_erp in ERP.index:
                    monto =  ERP.at[fila_erp, 'Unnamed: 8']
                    codigo_erp = ERP.at[fila_erp, 'Unnamed: 9']
                    if not pd.isna(tipo_tarjeta):                            
                        if str(codigo_aut) in str(codigo_erp):
                            sw=1
                            diccionario_2[f"{i2}"]={"tarjeta":tipo_tarjeta,"codigo":str(codigo_aut),"Valor":abs(int(monto)),"Status":"encontrado"}    
                if sw==0:
                    diccionario_2[f"{i2}"]={"tarjeta":tipo_tarjeta,"codigo":(codigo_aut),"Valor":monto_original,"Status":"no encontrado"}
            
    # --- CONSOLIDACIÓN DE TABLAS DE RESUMEN ---
    
    # 1. Tabla Pesos (Manteniendo la lógica original de diccionario y diccionario_2)
    diccionario_temp = [
        {'Amex US$': 'Amex US$', 'Valor US': 0, 'Valor TBK': 0, 'Diferencia': 0},
        {'Dinners US$': 'Dinners US$', 'Valor US': 0, 'Valor TBK': 0, 'Diferencia': 0},
        {'Master Card US$': 'Master Card US$', 'Valor US': 0, 'Valor TBK': 0, 'Diferencia': 0},
        {'Visa US$': 'Visa US$', 'Valor US': 0, 'Valor TBK': 0, 'Diferencia': 0},
        {'Total': 'Total', 'Valor US': 0, 'Valor TBK': 0, 'Diferencia': 0}
    ]
    
    # Sumar diccionario (Valor US)
    totales_us = {"Amex US$": 0, "Dinners US$": 0, "Master Card US$": 0, "Visa US$": 0}
    for valor in diccionario.values():
        val_list = list(valor.values())
        if val_list[3] == "encontrado" and val_list[0] in totales_us:
            totales_us[val_list[0]] += val_list[2]
            
    # Asignar a diccionario_temp
    nombres_temp = ["Amex US$", "Dinners US$", "Master Card US$", "Visa US$"]
    for i, nombre in enumerate(nombres_temp):
        diccionario_temp[i]["Valor US"] = totales_us[nombre]
    diccionario_temp[4]["Valor US"] = sum(totales_us.values())

    # Sumar diccionario_2 (Acumula sobre lo anterior para TBK según lógica original)
    totales_tbk = {"AX": totales_us["Amex US$"], "DI": totales_us["Dinners US$"], "MC": totales_us["Master Card US$"], "VI": totales_us["Visa US$"]}
    for valor in diccionario_2.values():
        val_list = list(valor.values())
        if val_list[3] == "encontrado" and val_list[0] in totales_tbk:
            totales_tbk[val_list[0]] += val_list[2]

    # Asignar a diccionario_temp y calcular diferencias
    claves_tbk = ["AX", "DI", "MC", "VI"]
    for i, (nombre, sigla) in enumerate(zip(nombres_temp, claves_tbk)):
        diccionario_temp[i]["Valor TBK"] = totales_tbk[sigla]
        diccionario_temp[i]["Diferencia"] = diccionario_temp[i]["Valor TBK"] - diccionario_temp[i]["Valor US"]
    
    diccionario_temp[4]["Valor TBK"] = sum(totales_tbk.values())
    diccionario_temp[4]["Diferencia"] = diccionario_temp[4]["Valor TBK"] - diccionario_temp[4]["Valor US"]

    # 2. Tabla Dólares (Calculada directamente de informacion_proceso)
    diccionario_dolar = [
        {'Amex US$': 'Amex US$', 'Valor US': 0, 'Valor TBK': 0, 'Diferencia': 0},
        {'Dinners US$': 'Dinners US$', 'Valor US': 0, 'Valor TBK': 0, 'Diferencia': 0},
        {'Master Card US$': 'Master Card US$', 'Valor US': 0, 'Valor TBK': 0, 'Diferencia': 0},
        {'Visa US$': 'Visa US$', 'Valor US': 0, 'Valor TBK': 0, 'Diferencia': 0},
        {'Total': 'Total', 'Valor US': 0, 'Valor TBK': 0, 'Diferencia': 0}
    ]
    
    totales_dolar = {'AX': {'idx': 0, 'us': 0, 'tbk': 0}, 'DI': {'idx': 1, 'us': 0, 'tbk': 0}, 
                     'MC': {'idx': 2, 'us': 0, 'tbk': 0}, 'VI': {'idx': 3, 'us': 0, 'tbk': 0}}
                     
    for item in informacion_proceso:
        if item['extra'] == 'OK' and item['tipo_tarjeta'] in totales_dolar:
            tarj = item['tipo_tarjeta']
            totales_dolar[tarj]['us'] += item['monto_original']
            
            # Limpiar y convertir monto_transbank
            val_tbk = item['monto_transbank']
            if isinstance(val_tbk, str):
                val_tbk = val_tbk.replace(',', '.')
            try:
                val_tbk = float(val_tbk)
                if math.isnan(val_tbk):
                    val_tbk = 0.0
            except ValueError:
                val_tbk = 0.0
            item['monto_transbank'] = val_tbk
            
            totales_dolar[tarj]['tbk'] += val_tbk

    tot_us_dolar, tot_tbk_dolar = 0, 0
    for tarj, data in totales_dolar.items():
        idx = data['idx']
        diccionario_dolar[idx]['Valor US'] = round(data['us'], 2)
        diccionario_dolar[idx]['Valor TBK'] = round(data['tbk'], 2)
        diccionario_dolar[idx]['Diferencia'] = round(data['tbk'] - data['us'], 2)
        
        tot_us_dolar += data['us']
        tot_tbk_dolar += data['tbk']

    diccionario_dolar[4]['Valor US'] = round(tot_us_dolar, 2)
    diccionario_dolar[4]['Valor TBK'] = round(tot_tbk_dolar, 2)
    diccionario_dolar[4]['Diferencia'] = round(tot_tbk_dolar - tot_us_dolar, 2)

    print("-----------------------------------")
    print("Fin Proceso de Archivos")
    print("-----------------------------------")

    return diccionario_temp, diccionario_dolar, ws, abonado
