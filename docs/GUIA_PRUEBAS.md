# 🧪 Guía Rápida de Pruebas del Sistema

## Requisitos Previos

✅ WeasyPrint instalado
✅ Logo descargado en `assets/toastmasters_logo.png`
✅ Template HTML en `assets/programa_template.html`
✅ Bot corriendo con ngrok

## 🚀 Flujo de Prueba Completo

### 1. Iniciar Ronda (Como Admin)
```
# Opción A: Esperar al martes 7 PM automático
# Opción B: Ir al menú de admin y seleccionar "Nueva ronda"
```

### 2. Aceptar Roles (Como Socios)
- Cada socio invitado responde: `ACEPTO` o `SI`
- El admin recibe notificación de cada aceptación

### 3. Verificar Estado
```
# Como Admin:
Menú Admin → 📊 Estado de la ronda

# Como Socio:
Menú Miembro → 📊 Estado de la ronda
```

### 4. Enviar Confirmaciones (Prueba Manual)
```
# Como Admin (sin esperar al lunes):
test confirmaciones
```

**Resultado esperado:**
- Cada socio con rol recibe mensaje de confirmación
- Admins reciben: "📨 Enviadas X solicitudes de confirmación"

### 5. Confirmar Asistencia (Como Socios)
```
# Cada socio responde:
CONFIRMO
```

**Resultado esperado:**
- Socio recibe: "✅ Confirmación recibida para [rol]. ¡Gracias!"
- Admin recibe: "[Club] ✅ [Nombre] confirmó. Pendientes: X"

### 6. Generar PDF (Automático o Manual)

**Automático:**
- Cuando el último socio confirma, se genera automáticamente

**Manual:**
```
# Como Admin:
generar pdf
```

**Resultado esperado:**
- Toastmaster recibe notificación
- Admin recibe ruta del archivo: `/home/paco/chatbot_whatsapp/generated_pdfs/programa_123.pdf`

### 7. Verificar PDF Generado
```bash
cd /home/paco/chatbot_whatsapp/generated_pdfs
ls -lh
# Ver el archivo más reciente
```

## 🎯 Casos de Prueba Específicos

### Caso 1: Socio No Confirma a Tiempo

**Pasos:**
1. Enviar confirmaciones: `test confirmaciones`
2. Que solo algunos socios confirmen
3. Esperar 1 minuto (o modificar deadline para pruebas)
4. Verificar que se busca reemplazo

**Resultado esperado:**
- Socio original: "❌ No confirmaste a tiempo..."
- Nuevo socio: "🔄 Se te asignó el rol..."
- Admin: "Rol reasignado de X a Y"

### Caso 2: No Hay Reemplazo Disponible

**Pasos:**
1. Asignar todos los roles disponibles
2. Que un socio no confirme
3. Verificar comportamiento

**Resultado esperado:**
- Socio: "⚠️ No confirmaste, pero no hay reemplazo..."
- Admin: "⚠️ [Nombre] no confirmó [rol], pero no hay reemplazo. Se mantiene asignado."

### Caso 3: Todos Confirman Antes de 7 PM

**Pasos:**
1. Enviar confirmaciones
2. Todos los socios confirman inmediatamente

**Resultado esperado:**
- PDF se genera automáticamente sin esperar a las 7 PM
- Admin: "✅ Todos los socios confirmaron. Generando programa..."

## 🔧 Comandos de Depuración

### Ver Estado Actual
```bash
cat /home/paco/chatbot_whatsapp/data/clubs/Toastmasters_Facultad_de_Ingenieria/state.json | jq .
```

### Ver Confirmaciones
```bash
cat /home/paco/chatbot_whatsapp/data/clubs/Toastmasters_Facultad_de_Ingenieria/state.json | jq .confirmations
```

### Ver Roles Aceptados
```bash
cat /home/paco/chatbot_whatsapp/data/clubs/Toastmasters_Facultad_de_Ingenieria/state.json | jq .accepted
```

### Ver Logs en Tiempo Real
```bash
# Si estás corriendo el bot en terminal
# Los logs aparecen automáticamente

# Para ver solo confirmaciones:
# En el código, busca: log.info que contengan "confirmación"
```

## 📊 Checklist de Validación

- [ ] Confirmaciones se envían correctamente
- [ ] Socios pueden confirmar con diferentes comandos (CONFIRMO, SI, etc.)
- [ ] Sistema detecta cuando todos confirmaron
- [ ] PDF se genera automáticamente
- [ ] PDF contiene todos los datos correctos:
  - [ ] Número de sesión correcto
  - [ ] Fecha = próximo martes
  - [ ] Tema y palabra del día
  - [ ] Todos los roles con nombres
  - [ ] Horarios calculados correctamente
  - [ ] Logo aparece
  - [ ] Estilos CSS se aplican
- [ ] Toastmaster recibe notificación
- [ ] Admin recibe ruta del archivo
- [ ] Sistema maneja no confirmaciones
- [ ] Sistema busca reemplazos
- [ ] Sistema maneja caso sin reemplazos

## 🐛 Errores Comunes

### Error: "Import weasyprint could not be resolved"
```bash
pip install weasyprint
```

### Error: "Logo not found"
```bash
curl -o /home/paco/chatbot_whatsapp/assets/toastmasters_logo.png \
  https://www.toastmasters.org/resources/toastmasters-international-logo
```

### Error: "No such file or directory: generated_pdfs"
```bash
mkdir -p /home/paco/chatbot_whatsapp/generated_pdfs
```

### Error: PDF vacío o mal formateado
- Verificar que `programa_template.html` existe
- Verificar que todas las variables `{{var}}` tienen valores
- Revisar logs para mensajes de error de WeasyPrint

## 📈 Métricas a Monitorear

1. **Tiempo de confirmación**: ¿Cuánto tarda cada socio en confirmar?
2. **Tasa de no confirmación**: ¿Cuántos socios no confirman habitualmente?
3. **Reasignaciones**: ¿Cuántas veces se necesita buscar reemplazo?
4. **Tiempo de generación PDF**: ¿Cuánto tarda en generarse?
5. **Errores**: ¿Hay errores recurrentes?

## 🎓 Notas Adicionales

- **Timezone**: El sistema usa la hora local del servidor
- **Durations**: Las duraciones están en el formato "X-Ymin" o "Xmin"
- **Round number**: Se incrementa automáticamente en cada nueva ronda
- **Persistence**: Todo se guarda en `state.json` automáticamente

## ✅ Prueba Completa Exitosa

Si puedes completar estos pasos sin errores, el sistema está funcionando correctamente:

1. ✅ Iniciar nueva ronda
2. ✅ Todos aceptan roles
3. ✅ Enviar confirmaciones (manual)
4. ✅ Todos confirman
5. ✅ PDF se genera automáticamente
6. ✅ PDF se puede abrir y leer correctamente
7. ✅ Todos los datos son correctos en el PDF
8. ✅ Reset funciona y limpia todo

## 🆘 ¿Necesitas Ayuda?

Revisa:
1. Terminal donde corre el bot (logs en tiempo real)
2. `state.json` (estado actual del sistema)
3. `generated_pdfs/` (archivos generados)
4. Este documento (referencia completa)
