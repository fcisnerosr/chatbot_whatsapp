# Sistema de Confirmaciones y Generación Automática de PDF

## 📋 Descripción General

Este sistema automatiza el proceso de confirmación de asistencia de los socios y la generación del programa de la sesión en formato PDF profesional.

## 🔄 Flujo Completo

### 1. Asignación de Roles (Martes 7 PM)
- El sistema inicia la ronda automáticamente cada martes a las 7 PM
- Se envían invitaciones a los socios según disponibilidad y rotación
- Los socios tienen 4 horas (Evaluador gramatical/Toastmaster) o 24 horas (otros roles) para aceptar

### 2. Confirmación de Roles Aceptados (Lunes 3 PM)
**Cuando todos los roles están aceptados:**
- El sistema programa automáticamente el envío de confirmaciones para el lunes 3 PM
- Se marca el estado `confirmations_scheduled: true` para evitar duplicados

**Envío de confirmaciones:**
- Cada socio con rol asignado recibe un mensaje:
  ```
  ⚠️ *Confirmación de asistencia*
  
  Hola [Nombre]!
  
  Tienes asignado el rol de [Rol] para la sesión de mañana martes.
  
  Por favor confirma tu asistencia respondiendo:
  *CONFIRMO* o *SI*
  
  ⏰ Plazo: Hoy antes de las 7:00 PM
  
  Si no confirmas, buscaremos un reemplazo.
  ```

### 3. Procesamiento de Confirmaciones

**El socio responde:**
- Puede escribir: `CONFIRMO`, `CONFIRM`, `SI`, `ASISTIRE`, `ASISTO`
- El sistema marca su confirmación como recibida
- Notifica a los admins del progreso

**Si no confirma antes de las 7 PM:**
- El sistema busca un reemplazo automáticamente
- Se notifica al socio original que su rol fue reasignado
- Se invita al nuevo socio a confirmar
- Si no hay reemplazo, se mantiene al original con advertencia

### 4. Generación del PDF (Lunes 7 PM o cuando todos confirmen)

**Cuando todos confirman:**
- Se genera automáticamente el programa en HTML
- Se convierte a PDF usando WeasyPrint
- Se guarda en `/home/paco/chatbot_whatsapp/generated_pdfs/`

**El PDF incluye:**
- Banner con degradado azul Toastmasters
- Logo oficial de Toastmasters International
- Número de sesión y fecha (próximo martes 7 PM)
- Tema de la sesión
- Palabra del día
- Agenda completa con horarios calculados automáticamente
- Nombres de todos los participantes
- Duración de cada segmento

**Envío del PDF:**
- Se notifica al Toastmaster de la noche
- Los admins reciben la ruta completa del archivo
- Para envío automático por WhatsApp, se requiere configurar URL pública

## 🎨 Estructura del PDF

```
┌─────────────────────────────────────────┐
│  [Banner degradado azul + Logo TM]      │
├─────────────────────────────────────────┤
│  Sesión #123                             │
│  Próximo Martes 7:00 PM                  │
├─────────────────────────────────────────┤
│  Tema: [Tema de la sesión]              │
│  Palabra del día: [Palabra]             │
├─────────────────────────────────────────┤
│  AGENDA                                  │
│  ┌────────┬────────┬──────────┬───────┐│
│  │ Hora   │ Dur.   │ Agenda   │ Quien ││
│  ├────────┼────────┼──────────┼───────┤│
│  │ 19:00  │ 8-10m  │ TM abre  │ Juan  ││
│  │ 19:10  │ 2m     │ Pdte abre│ María ││
│  │ ...    │ ...    │ ...      │ ...   ││
│  └────────┴────────┴──────────┴───────┘│
└─────────────────────────────────────────┘
```

## ⚙️ Configuración Técnica

### Archivos Principales

1. **`src/app.py`**
   - `_schedule_monday_confirmations()`: Programa confirmaciones para lunes 3 PM
   - `_send_monday_confirmations()`: Envía mensajes de confirmación
   - `_schedule_confirmation_deadline()`: Programa verificación para 7 PM
   - `_process_confirmation_deadline()`: Procesa no confirmados
   - `_generate_and_send_pdf()`: Genera y notifica PDF

2. **`src/programa_generator.py`**
   - `generate_program_pdf()`: Función principal de generación
   - `generate_program_html()`: Crea HTML desde template
   - `calculate_agenda_times()`: Calcula horarios automáticamente
   - `get_next_tuesday()`: Obtiene fecha de la sesión

3. **`assets/programa_template.html`**
   - Template HTML con estilos CSS integrados
   - Variables reemplazadas por Jinja2-style: `{{variable}}`

### Dependencias

```bash
pip install weasyprint pillow
```

### Estructura de Datos

**State Store (`state.json`):**
```json
{
  "round": 123,
  "accepted": {
    "Toastmaster de la noche": {
      "waid": "5212293655442",
      "name": "Paco"
    }
  },
  "confirmations": {
    "5212293655442": {
      "role": "Toastmaster de la noche",
      "confirmed": true,
      "timestamp": 1234567890.123
    }
  },
  "confirmations_scheduled": true
}
```

## 🧪 Comandos de Prueba (Solo Admins)

### 1. Enviar Confirmaciones Manualmente
```
test confirmaciones
enviar confirmaciones
```
Envía inmediatamente las confirmaciones sin esperar al lunes 3 PM.

### 2. Generar PDF Manualmente
```
test pdf
generar pdf
```
Genera el PDF inmediatamente con los roles actuales aceptados.

### 3. Reset Completo
```
reset
```
Reinicia la ronda, limpia todas las sesiones y confirmaciones.

## 📱 Comandos para Socios

### Confirmar Asistencia
```
CONFIRMO
SI
CONFIRM
ASISTIRE
ASISTO
```
Cualquiera de estos comandos confirma la asistencia del socio.

## 🔍 Monitoreo

### Logs del Sistema

El sistema registra en logs:
- Programación de confirmaciones
- Envío de mensajes
- Confirmaciones recibidas
- Generación de PDF
- Errores y excepciones

### Notificaciones a Admins

Los admins reciben notificaciones sobre:
- ✅ Confirmaciones recibidas (con contador de pendientes)
- 🔄 Reasignaciones por no confirmación
- ⚠️ Roles sin reemplazo disponible
- 📄 PDF generado (con ruta completa del archivo)
- ❌ Errores en generación de PDF

## 🎯 Próximos Pasos

### Envío Automático de PDF por WhatsApp

Para enviar el PDF automáticamente, necesitas configurar una URL pública:

**Opción 1: Servidor Web/Cloud**
```python
# En _generate_and_send_pdf():
pdf_url = upload_to_s3(pdf_path)  # O tu servicio preferido
send_document(toastmaster_waid, pdf_url, 
              caption=f"Programa Sesión #{round_no}",
              filename=pdf_path.name)
```

**Opción 2: ngrok (desarrollo)**
```bash
# Terminal 1: Servir archivos
cd /home/paco/chatbot_whatsapp/generated_pdfs
python -m http.server 8080

# Terminal 2: Exponer públicamente
ngrok http 8080

# Usar URL: https://xxxxx.ngrok.io/programa_123.pdf
```

**Opción 3: Telegram Bot (alternativa)**
- Telegram permite enviar archivos locales sin URL pública
- Crear bot de Telegram para compartir PDFs
- Integrar con el sistema existente

## 🐛 Troubleshooting

### El PDF no se genera
- Verificar que WeasyPrint esté instalado: `pip show weasyprint`
- Verificar que exista `assets/toastmasters_logo.png`
- Revisar logs para errores específicos

### Las confirmaciones no se envían
- Verificar que `confirmations_scheduled: true` en state.json
- Revisar logs para tiempo programado
- Para pruebas, usar `test confirmaciones`

### Los horarios están incorrectos
- Verificar duraciones en `club.json`
- La agenda siempre inicia a las 19:00 (7 PM)
- Duraciones se toman de `default_duration` en roles

## 📊 Estadísticas

El sistema rastrea automáticamente:
- Tiempo de confirmación de cada socio
- Reasignaciones por no confirmación
- PDFs generados por sesión
- Tasa de confirmación por socio

## 🔐 Seguridad

- Solo admins pueden ejecutar comandos de prueba
- Las confirmaciones solo pueden ser enviadas por el sistema
- Los PDFs se guardan localmente (no expuestos públicamente)
- Verificación de permisos en cada comando

## 📞 Soporte

Para problemas o sugerencias:
1. Revisar logs en la consola del bot
2. Verificar estado en `data/clubs/[club]/state.json`
3. Contactar al administrador del sistema
