# 🔄 Sistema de Gestión Dinámica de Roles

## ⚠️ NOTA IMPORTANTE

**Las funcionalidades descritas en este documento son PROPUESTAS para desarrollo futuro.**

El código actual del bot NO implementa estas características. Este documento sirve como:
- Guía de diseño para futuras implementaciones
- Documentación de casos de uso deseados
- Referencia para el equipo de desarrollo

---

## Caso 1: Cancelar un Rol Ya Aceptado

### Escenario
Juan aceptó ser Toastmaster, pero surgió un imprevisto y necesita cancelar.

### Flujo Propuesto

**Juan escribe:** `CANCELAR MI ROL`

**Bot responde:**
```
⚠️ Actualmente eres: TOASTMASTER

¿Estás seguro de cancelar este rol?

Responde:
• SÍ CANCELAR - Liberarás el cargo
• NO - Mantener el rol
```

**Si Juan responde:** `SÍ CANCELAR`

**Bot responde:**
```
✅ Has liberado el rol de TOASTMASTER

El bot buscará un reemplazo automáticamente.

Opciones adicionales:
• SUGERIR REEMPLAZO - Proponer a alguien específico
• DEJAR AL BOT - El sistema elegirá automáticamente
```

---

## Caso 2: Transferir Rol a Alguien Específico

### Escenario
María es Toastmaster pero prefiere dárselo a Pedro porque él tiene más experiencia con el tema de esa sesión.

### Flujo Propuesto

**María escribe:** `TRANSFERIR MI ROL`

**Bot responde:**
```
📋 Tu rol actual: TOASTMASTER

Puedes transferirlo a otro socio elegible.

Escribe el nombre o número del socio:
• PEDRO
• 5215551234567
• VER DISPONIBLES - Lista quiénes pueden tomar este rol
```

**María escribe:** `PEDRO`

**Bot verifica:**
- ¿Pedro está registrado?
- ¿Pedro cumple el nivel mínimo? (Nivel 4 para Toastmaster)
- ¿Pedro no tiene roles incompatibles?

**Si todo está bien, bot responde:**
```
✅ Solicitud enviada a Pedro

Esperando su respuesta...
```

**Bot envía a Pedro:**
```
👋 Hola Pedro

María te propone transferir el rol de TOASTMASTER

¿Aceptas?
• ACEPTO
• RECHAZO
```

**Si Pedro acepta:**
```
[A María] ✅ Pedro aceptó el rol de TOASTMASTER. Quedas liberada.
[A Pedro] ✅ Ahora eres TOASTMASTER para la próxima reunión.
```

**Si Pedro rechaza:**
```
[A María] ❌ Pedro rechazó el rol.

Opciones:
• SUGERIR OTRO - Proponer a alguien más
• DEJAR AL BOT - El sistema elegirá automáticamente
• MANTENER ROL - Conservar tu cargo original
```

---

## Caso 3: Solicitar Rol Adicional (con Conflicto)

### Escenario
Carlos es Toastmaster y quiere dar un discurso preparado (roles incompatibles).

### Flujo Propuesto

**Carlos escribe:** `QUIERO SER ORADOR`

**Bot detecta conflicto y responde:**
```
⚠️ CONFLICTO DETECTADO

Actualmente eres: TOASTMASTER
Solicitas: ORADOR

❌ Estos roles NO son compatibles
(El Toastmaster presenta a los oradores)

Opciones:
1️⃣ CANCELAR TOASTMASTER - Liberar ese rol y tomar ORADOR
2️⃣ MANTENER TOASTMASTER - Conservar tu rol actual
3️⃣ TRANSFERIR TOASTMASTER - Dar tu rol a alguien específico y tomar ORADOR
```

**Si Carlos responde:** `1️⃣`

**Bot pregunta:**
```
¿Cómo asignar el rol de TOASTMASTER?

• AUTOMÁTICO - El bot elegirá al mejor candidato
• MANUAL - Tú propones a alguien
```

**Si Carlos responde:** `MANUAL`

**Bot responde:**
```
📋 Candidatos elegibles para TOASTMASTER:

1. Ana López (Nivel 4, 12 participaciones)
2. Luis García (Nivel 4, 15 participaciones)
3. Sofia Torres (Nivel 4, 10 participaciones)

Escribe el número o nombre del candidato:
```

**Carlos escribe:** `2`

**Bot envía a Luis:**
```
👋 Hola Luis

Carlos dejó el rol de TOASTMASTER y te propone como reemplazo.

¿Aceptas ser TOASTMASTER?
• ACEPTO
• RECHAZO
```

---

## Caso 4: Cambiar de Rol por Uno Menos Exigente

### Escenario
Laura aceptó ser Evaluador General pero se siente abrumada y prefiere algo más sencillo.

### Flujo Propuesto

**Laura escribe:** `CAMBIAR MI ROL`

**Bot responde:**
```
📊 Tu rol actual: EVALUADOR GENERAL (Nivel 4 requerido)

Puedes cambiarlo por uno menos exigente:

Roles disponibles:
1. CONTADOR DE TIEMPO (Nivel 1)
2. CONTADOR DE MULETILLAS (Nivel 1)
3. GRAMÁTICO (Nivel 2)

Escribe el número del rol deseado:
```

**Laura escribe:** `3`

**Bot verifica disponibilidad y responde:**
```
✅ Cambio autorizado

Nuevo rol: GRAMÁTICO

¿Cómo asignar EVALUADOR GENERAL?
• AUTOMÁTICO
• MANUAL (sugerir reemplazo)
```

---

## Caso 5: Múltiples Cambios en Cascada

### Escenario
Diego es Orador pero cancela. El bot debe reorganizar todo porque había un Evaluador Individual asignado específicamente para Diego.

### Flujo Propuesto

**Diego escribe:** `CANCELAR MI ROL`

**Bot detecta dependencias:**
```
⚠️ ATENCIÓN: Dependencias Detectadas

Si cancelas ORADOR, afectas a:
• Evaluador Individual: Fernanda (asignada para evaluar tu discurso)

Opciones:
1️⃣ CANCELAR TODO - Liberas tu rol y Fernanda también
2️⃣ BUSCAR ORADOR - Transferir tu rol a otro y Fernanda se queda
3️⃣ MANTENER - No hacer cambios
```

**Si Diego responde:** `2️⃣`

**Bot busca automáticamente:**
```
🔍 Buscando oradores disponibles...

Candidato encontrado: Miguel (Nivel 3, sin conflictos)

Enviando propuesta...
```

**Bot envía a Miguel:**
```
👋 Hola Miguel

Diego necesita transferir el rol de ORADOR.

¿Puedes reemplazarlo?
• ACEPTO
• RECHAZO
```

---

## Caso 6: Ver Roles Disponibles para Intercambio

### Escenario
Patricia tiene curiosidad por saber qué otros roles podría tomar sin conflictos.

### Flujo Propuesto

**Patricia escribe:** `VER OPCIONES`

**Bot responde:**
```
📋 Tu situación actual:

Rol actual: CONTADOR DE MULETILLAS
Tu nivel: 2 (Intermedio)

Roles compatibles que puedes solicitar:
✅ CONTADOR DE TIEMPO
✅ GRAMÁTICO
✅ TEMA DEL DÍA

❌ No disponibles (requieren nivel mayor):
🔒 TOASTMASTER (Nivel 4)
🔒 EVALUADOR GENERAL (Nivel 4)
🔒 EVALUADOR INDIVIDUAL (Nivel 3)
🔒 ORADOR (Nivel 3)

Para solicitar un cambio: CAMBIAR MI ROL
```

---

## 📋 Comandos Nuevos Propuestos

### Para Usuarios

| Comando | Descripción |
|---------|-------------|
| `CANCELAR MI ROL` | Libera tu rol actual |
| `TRANSFERIR MI ROL` | Ofrece tu rol a otro socio |
| `CAMBIAR MI ROL` | Solicita un rol diferente |
| `QUIERO SER [ROL]` | Solicita un rol específico adicional |
| `VER OPCIONES` | Muestra qué roles puedes tomar |
| `VER DISPONIBLES` | Lista socios elegibles para un rol |

### Para Administradores (adicionales)

| Comando | Descripción |
|---------|-------------|
| `FORZAR CAMBIO [nombre] [nuevo_rol]` | Reasigna un rol manualmente |
| `VER CONFLICTOS` | Muestra incompatibilidades actuales |
| `REORGANIZAR` | El bot redistribuye todos los roles desde cero |

---

## ⚙️ Reglas Adicionales para Transferencias

1. **Ventana de Cambio:** Solo hasta 48 horas antes de la reunión
2. **Límite de Cambios:** Máximo 2 cambios por socio por ronda
3. **Prioridad:** Las transferencias manuales tienen prioridad sobre las automáticas
4. **Notificaciones:** Todos los afectados reciben notificación de cambios
5. **Historial:** El bot registra quién transfirió a quién (transparencia)
6. **Penalización:** Cancelar más de 3 veces seguidas = suspensión temporal de asignaciones automáticas

---

## 🚨 Casos Especiales

### Emergencia Médica
```
Comando: EMERGENCIA
Bot: Libera automáticamente todos tus roles sin penalización
```

### Último Momento (menos de 24h antes)
```
Bot: "⚠️ Cambio de último momento. Se notificará al VP de Educación."
```

### Nadie Acepta la Transferencia
```
Bot: "❌ No se encontró reemplazo. Opciones:
1. Mantener tu rol original
2. Dejar el rol vacante (se notificará al admin)"
```

---

## 🔧 Implementación Técnica Requerida

Para desarrollar estas funcionalidades, se necesita:

### Cambios en `src/models.py`

**Nuevos estados de rol:**
- `PENDIENTE_TRANSFERENCIA` - Rol en proceso de transferencia
- `PENDIENTE_CANCELACION` - Rol esperando confirmación de cancelación
- `PENDIENTE_CAMBIO` - Rol esperando cambio a otro rol

**Nuevos métodos en clase `Member`:**
- `cancel_role()` - Cancela el rol actual
- `transfer_role_to(member)` - Transfiere rol a otro miembro
- `request_role_change(new_role)` - Solicita cambio de rol
- `get_compatible_roles()` - Lista roles compatibles con el actual

**Nuevos métodos en clase `Club`:**
- `find_eligible_replacements(role_name)` - Busca candidatos para un rol
- `detect_role_dependencies(member)` - Detecta dependencias de roles
- `resolve_cascade_changes(member)` - Resuelve cambios en cascada
- `log_transfer(from_member, to_member, role)` - Registra transferencias

### Cambios en `src/app.py`

**Nuevos handlers de comandos:**
- `handle_cancel_role(waid, club)` - Procesa `CANCELAR MI ROL`
- `handle_transfer_role(waid, club)` - Procesa `TRANSFERIR MI ROL`
- `handle_change_role(waid, club)` - Procesa `CAMBIAR MI ROL`
- `handle_request_specific_role(waid, club, role_name)` - Procesa `QUIERO SER [ROL]`
- `handle_view_options(waid, club)` - Procesa `VER OPCIONES`

**Nueva lógica de estados conversacionales:**
```python
CONVERSATION_STATES = {
    'AWAITING_CANCEL_CONFIRMATION': 'Esperando confirmación de cancelación',
    'AWAITING_TRANSFER_TARGET': 'Esperando nombre de destinatario',
    'AWAITING_TRANSFER_ACCEPTANCE': 'Esperando aceptación de transferencia',
    'AWAITING_CHANGE_SELECTION': 'Esperando selección de nuevo rol',
    'AWAITING_CONFLICT_RESOLUTION': 'Esperando resolución de conflicto'
}
```

### Nuevas estructuras de datos en JSON

**En `state.json` agregar:**
```json
{
  "pending_transfers": [
    {
      "from_waid": "521...",
      "to_waid": "521...",
      "role": "Toastmaster",
      "timestamp": "2026-01-03T10:30:00",
      "status": "pending"
    }
  ],
  "transfer_history": [
    {
      "from_member": "María",
      "to_member": "Pedro",
      "role": "Toastmaster",
      "date": "2026-01-03",
      "reason": "manual_transfer"
    }
  ],
  "conversation_state": {
    "521...": {
      "state": "AWAITING_TRANSFER_TARGET",
      "data": {"current_role": "Toastmaster"},
      "timestamp": "2026-01-03T10:30:00"
    }
  }
}
```

### Sistema de Notificaciones Broadcast

**Nueva función:**
```python
def notify_affected_members(changes_list, club):
    """
    Envía notificaciones a todos los miembros afectados por cambios
    
    Args:
        changes_list: Lista de cambios realizados
        club: Instancia del club
    """
    for change in changes_list:
        # Notificar a quien cedió el rol
        # Notificar a quien recibió el rol
        # Notificar al admin
        # Notificar a dependencias (ej: evaluadores)
```

---

## 📊 Diagrama de Flujo: Transferencia de Rol

```
[Usuario: TRANSFERIR MI ROL]
         ↓
[Bot: ¿Tiene rol asignado?] → NO → [Error: No tienes rol]
         ↓ SÍ
[Bot: Solicita nombre/número]
         ↓
[Usuario: proporciona destinatario]
         ↓
[Bot: Valida destinatario] → INVÁLIDO → [Error: Socio no encontrado]
         ↓ VÁLIDO
[Bot: Verifica elegibilidad] → NO ELEGIBLE → [Error: No cumple requisitos]
         ↓ ELEGIBLE
[Bot: Envía propuesta al destinatario]
         ↓
[Destinatario: ACEPTO/RECHAZO]
         ↓
    ┌────┴────┐
 ACEPTO    RECHAZO
    ↓          ↓
[Transferir] [Volver a solicitar nombre]
    ↓
[Actualizar state.json]
    ↓
[Notificar a ambos]
    ↓
[FIN]
```

---

## 🧪 Casos de Prueba

### Test 1: Cancelación Simple
1. Usuario tiene rol "Contador de Tiempo"
2. Escribe: `CANCELAR MI ROL`
3. Confirma: `SÍ CANCELAR`
4. **Esperado:** Rol liberado, bot busca reemplazo automático

### Test 2: Transferencia Exitosa
1. María tiene "Toastmaster" (Nivel 4)
2. Escribe: `TRANSFERIR MI ROL`
3. Propone: `PEDRO`
4. Pedro acepta
5. **Esperado:** Pedro = Toastmaster, María = sin rol

### Test 3: Conflicto de Roles
1. Carlos tiene "Toastmaster"
2. Escribe: `QUIERO SER ORADOR`
3. **Esperado:** Bot detecta conflicto, ofrece opciones

### Test 4: Cambio a Rol Menor
1. Laura tiene "Evaluador General"
2. Escribe: `CAMBIAR MI ROL`
3. Selecciona "Gramático"
4. **Esperado:** Cambio autorizado, bot busca reemplazo para EG

### Test 5: Dependencias en Cascada
1. Diego tiene "Orador", Fernanda tiene "Evaluador Individual" (para Diego)
2. Diego escribe: `CANCELAR MI ROL`
3. **Esperado:** Bot advierte sobre Fernanda, ofrece opciones

---

## 📅 Roadmap de Implementación

### Fase 1: Cancelaciones Básicas (Sprint 1-2)
- [ ] Comando `CANCELAR MI ROL`
- [ ] Confirmación doble
- [ ] Búsqueda automática de reemplazo
- [ ] Tests unitarios

### Fase 2: Transferencias Manuales (Sprint 3-4)
- [ ] Comando `TRANSFERIR MI ROL`
- [ ] Validación de destinatario
- [ ] Sistema de propuestas
- [ ] Notificaciones bidireccionales
- [ ] Tests de integración

### Fase 3: Cambios de Rol (Sprint 5-6)
- [ ] Comando `CAMBIAR MI ROL`
- [ ] Detección de conflictos
- [ ] Sistema de opciones compatibles
- [ ] Tests de casos edge

### Fase 4: Gestión Avanzada (Sprint 7-8)
- [ ] Comando `QUIERO SER [ROL]`
- [ ] Detección de dependencias
- [ ] Cambios en cascada
- [ ] Sistema de penalizaciones
- [ ] Tests de regresión completos

### Fase 5: Comandos Admin Avanzados (Sprint 9-10)
- [ ] `FORZAR CAMBIO`
- [ ] `VER CONFLICTOS`
- [ ] `REORGANIZAR`
- [ ] Dashboard de historial
- [ ] Documentación completa

---

## 💡 Consideraciones de UX

1. **Mensajes claros:** Usar emojis y formato estructurado
2. **Confirmaciones dobles:** Para acciones destructivas
3. **Opciones numeradas:** Facilita selección en móvil
4. **Timeouts:** Si no responde en 5 min, cancelar operación
5. **Feedback inmediato:** Siempre confirmar recepción de comando
6. **Historial visible:** Usuario puede ver sus cambios recientes

---

## 🔐 Consideraciones de Seguridad

1. **Validar identidad:** Solo el dueño del rol puede cancelarlo/transferirlo
2. **Rate limiting:** Máximo 3 cambios por hora por usuario
3. **Audit log:** Registrar todos los cambios con timestamp
4. **Permisos admin:** Algunos comandos solo para administradores
5. **Rollback:** Capacidad de deshacer cambios en caso de error

---

**Documento creado:** Enero 3, 2026  
**Versión:** 1.0  
**Estado:** Diseño propuesto - No implementado  
**Autor:** Data & Flow Consulting
