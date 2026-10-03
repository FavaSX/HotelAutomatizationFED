# This is an auto-generated Django model module.
# You'll have to do the following manually to clean this up:
#   * Rearrange models' order
#   * Make sure each model has one field with primary_key=True
#   * Make sure each ForeignKey and OneToOneField has `on_delete` set to the desired behavior
#   * Remove `managed = False` lines if you wish to allow Django to create, modify, and delete the table
# Feel free to rename the models, but don't rename db_table values or field names.
from django.db import models
import qrcode
from io import BytesIO
from django.core.files import File
from PIL import Image,ImageDraw

class CheckInOut(models.Model):
    id_cio = models.AutoField(primary_key=True)
    check_in_fh = models.DateTimeField()
    check_out_fh = models.DateTimeField()
    id_reserva = models.OneToOneField('Reserva', models.DO_NOTHING, db_column='id_reserva' )
    id_p = models.ForeignKey('Personal', models.DO_NOTHING, db_column='id_p' )
    id_est_ch = models.ForeignKey('EstadoCheckInOut', models.DO_NOTHING, db_column='id_est_ch' )
  

    class Meta:
        db_table = 'check_in_out'
         


class Cliente(models.Model):
    id_reg = models.AutoField(primary_key=True )
    identificacion = models.CharField(max_length=45 )
    nombre = models.CharField(max_length=60 )
    ape_paterno = models.CharField(max_length=60 )
    ape_materno = models.CharField(max_length=60, blank=True, null=True )
    es_nacional = models.CharField(max_length=1 )
    pais = models.CharField(max_length=45 )
    habla_espanol = models.CharField(max_length=1 )
    idioma_natural = models.CharField(max_length=45 )
    email=models.CharField(max_length=100,default='h@h.com' )
    id_user = models.OneToOneField('Usuarios', models.DO_NOTHING, db_column='id_user' )

    class Meta:
        db_table = 'cliente'
         


class Encuesta(models.Model):
    id = models.AutoField(primary_key=True )
    preg1 = models.CharField(max_length=60 )
    resp1 = models.CharField(max_length=60 )
    preg2 = models.CharField(max_length=60 )
    resp2 = models.CharField(max_length=60 )
    preg3 = models.CharField(max_length=60 )
    resp3 = models.CharField(max_length=60 )
    preg4 = models.CharField(max_length=60 )
    resp4 = models.CharField(max_length=60 )
    comentarios = models.CharField(max_length=60 )
    id_reserva = models.OneToOneField('Reserva', models.DO_NOTHING, db_column='id_reserva' )

    class Meta:
        db_table = 'encuesta'
         


class EstadoCheckInOut(models.Model):
    id_est_ch = models.AutoField(primary_key=True)
    descripcion = models.CharField(max_length=45, blank=True, null=True )

    class Meta:
        db_table = 'estado_check_in_out'
         


class EstadoReserva(models.Model):
    id_estado = models.AutoField(primary_key=True)
    descripcion = models.CharField(max_length=45 )
    

    class Meta:
        db_table = 'estado_reserva'
         
        
    def __str__(self):
        return self.descripcion

class Galeria(models.Model):
    id_g = models.AutoField(primary_key=True)
    descripcion = models.CharField(max_length=70 )
    foto = models.ImageField(upload_to='galeria')
    id_h = models.ForeignKey('Habitacion', models.DO_NOTHING, db_column='id_h' )

    class Meta:
        db_table = 'galeria'
         


class Habitacion(models.Model):
    id_h = models.AutoField(primary_key=True)
    piso = models.DecimalField(max_digits=3, decimal_places=0 )
    numero = models.DecimalField(max_digits=3, decimal_places=0 )
    cant_personas = models.DecimalField(max_digits=3, decimal_places=0 )
    habitaciones = models.DecimalField(max_digits=2, decimal_places=0 )
    banos = models.DecimalField(max_digits=2, decimal_places=0 )
    metros = models.DecimalField(max_digits=3, decimal_places=0 )
    wifi = models.CharField(max_length=1 )
    tv_cable = models.CharField(max_length=1 )
    desayuno = models.CharField(max_length=1 )
    precio_noche = models.DecimalField(max_digits=6, decimal_places=0 )
    descripcion = models.TextField(max_length=300 )
    activa = models.CharField(max_length=1 )
    num_star = models.DecimalField(max_digits=1,default=2, decimal_places=0 )
    imagen = models.ImageField(upload_to='fotos',default='fotos/no_disponible.jpg')
    id_th = models.ForeignKey('TipoHabitacion', models.DO_NOTHING, db_column='id_th' )

    class Meta:
        db_table = 'habitacion'
         


class Pago(models.Model):
    id_pago = models.AutoField(primary_key=True)
    anticipo_30 = models.DecimalField(max_digits=6, decimal_places=0 )
    es_pagado = models.CharField(max_length=1 )
    id_reserva = models.ForeignKey('Reserva', models.DO_NOTHING, db_column='id_reserva' )

    class Meta:
        db_table = 'pago'
         


class Personal(models.Model):
    id_p = models.AutoField(primary_key=True)
    run = models.DecimalField(max_digits=8, decimal_places=0 )
    dv = models.CharField(max_length=1, blank=True, null=True )
    nombre = models.CharField(max_length=60 )
    ape_paterno = models.CharField(max_length=60 )
    ape_materno = models.CharField(max_length=60, blank=True, null=True )
    direccion = models.CharField(max_length=100 )
    fono = models.CharField(max_length=45 )
    correo = models.CharField(max_length=100 )
    clave = models.CharField(max_length=45 )
    activo = models.CharField(max_length=1 )
    id_tp = models.ForeignKey('TipoPersonal', models.DO_NOTHING, db_column='id_tp' )

    class Meta:
        db_table = 'personal'
         


class Reserva(models.Model):
    id_reserva = models.AutoField(primary_key=True)
    fecha_inicio = models.DateTimeField()
    fecha_termino = models.DateTimeField()
    dias = models.DecimalField(max_digits=6, decimal_places=0 )
    valor = models.DecimalField(max_digits=7, decimal_places=0 )
    cant_personas= models.IntegerField(default=1)
    obs = models.CharField(max_length=100, blank=True, null=True )
    qr = models.ImageField(upload_to='qr',default='fotos/no_disponible.jpg')
    id_reg = models.ForeignKey(Cliente, models.DO_NOTHING, db_column='id_reg' )
    id_h = models.ForeignKey(Habitacion, models.DO_NOTHING, db_column='id_h' )
    id_estado = models.ForeignKey(EstadoReserva, models.DO_NOTHING, db_column='id_estado' )

    class Meta:
        db_table = 'reserva'
         


class TipoHabitacion(models.Model):
    id_th = models.AutoField(primary_key=True)
    descripcion = models.CharField(max_length=45 )
    precio = models.DecimalField(max_digits=6, decimal_places=0 )

    class Meta:
        db_table = 'tipo_habitacion'
         
    def __str__(self):
        return self.descripcion

class TipoPersonal(models.Model):
    id_tp = models.AutoField(primary_key=True)
    descripcion = models.CharField(max_length=45 )

    class Meta:
        db_table = 'tipo_personal'
         

    def __str__(self):
        return self.descripcion
    
class Usuarios(models.Model):
    id_user = models.AutoField(primary_key=True)
    correo = models.CharField(max_length=120 )
    password = models.CharField(max_length=45 )
    fecha_creacion = models.DateTimeField()
    activo = models.CharField(max_length=1 )

    class Meta:
        db_table = 'usuarios'
        
class Comentario(models.Model):
    id_comen = models.AutoField(primary_key=True)
    id_h = models.ForeignKey(Habitacion, models.DO_NOTHING, db_column='id_h' )
    fecha_creacion = models.DateTimeField()
    nombre=models.CharField(max_length=45 )
    correo = models.CharField(max_length=120 )
    comentario = models.CharField(max_length=220 )

    class Meta:
        db_table = 'comentario'
 