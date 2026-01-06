I# Chatbot WhatsApp — Roles/Toastmasters (PROTOTIPO)

Version: 1.0
Author: Data & Flow Consulting <fcisnerosr@outlook.es>
License: Proprietary

---
## ⚠️ NOTAS IMPORTANTES (LEER ANTES DE USAR)

* Este repositorio es un **PROTOTIPO** en evolución. Úsalo para pruebas; no es aún un sistema “production-ready”.
* **NUNCA** subas el archivo `.env` al repositorio ni compartas ahí credenciales. Mantén `.env` fuera de Git (está en `.gitignore`). Provee `.env.example` como guía.
* Quien ejecute el bot debe agregarse como `ADMIN` en el `.env` para poder usar comandos administrativos (`INICIAR`/`ESTADO`/`RESET`/`CANCELAR`).
* En modo Sandbox de Gupshup, cada usuario debe escribir “hola” al número sandbox al menos una vez cada 24h para poder recibir mensajes del bot.

---
## Descripción

Bot en **Python + Flask** que administra rondas y asigna roles de una reunión (Toastmasters). Envía propuestas de rol por WhatsApp usando **Gupshup API**.
Para pruebas locales expone el servidor con **ngrok**.

**Nueva implementación (v1.2 - Multi-Club):**
- El sistema ahora soporta **múltiples clubes** de forma simultánea y aislada.
- La estructura de datos se organiza en subdirectorios, uno por cada club (`data/<club_slug>`).
- El webhook de Gupshup se vuelve dinámico para dirigir los mensajes al club correcto.
- Se introduce **POO** con `src/models.py` para una gestión robusta de roles, miembros y clubes.
- Se utiliza `src/setup_club.py` para **sembrar** el estado inicial de uno o más clubes.

---
## Estructura principal

```text
chatbot_whatsapp/
├─ src/
│  ├─ app.py          # Servidor Flask (endpoint /webhook; club auto-resuelto por waid)
│  ├─ models.py       # POO: Role, Member, Club + persistencia JSON
│  └─ setup_club.py   # Script de semilla inicial para los clubes
├─ data/
│  ├─ club-demo/      # Slug del primer club
│  │  ├─ club.json
│  │  └─ state.json
│  └─ club-toastmasters/ # Slug del segundo club
│     ├─ club.json
│     └─ state.json
├─ environment.yml    # Entorno conda/mamba
└─ README.md
```

---
## Comandos del Bot

El bot responde a comandos de administradores y de usuarios estándar.

#### Comandos de Administrador
(Solo para números en `ADMIN_NUMBERS`)

| Comando | Descripción |
|---|---|
| `INICIAR` | Comienza una nueva ronda de asignación de roles. |
| `ESTADO` | Muestra un resumen de la ronda actual. |
| `CANCELAR` | Cancela la ronda actual. |
| `RESET` | **(Peligroso)** Reinicia el estado del bot a cero. |
| `MIEMBROS` | Lista todos los miembros del club con su waid y nivel. |
| `AGREGAR Nombre, 521...` | Añade un nuevo miembro al club. |
| `ELIMINAR 521...` o `Nombre` | Elimina un miembro del club. |

#### Comandos de Usuario
(Disponibles para todos)

| Comando | Descripción |
|---|---|
| `ACEPTO` | Confirma el rol propuesto. |
| `RECHAZO` | Rechaza el rol propuesto. |
| `MI ROL` | Recuerda el rol asignado o pendiente. |
| `HOLA` | Saluda al bot (útil para el sandbox de Gupshup). |

---
## Requisitos

* Python 3.12+
* Mamba/Conda (o pip)
* Cuenta de Gupshup (App WhatsApp con modo Sandbox)
* Cuenta de ngrok (gratuita) para exponer `http://localhost:5000`

---
## Instalación del entorno

1.  **Crear y activar entorno con mamba/conda:**
    ```
    mamba env create -f environment.yml
    mamba activate chatbot-whatsapp
    ```
    (Alternativa pip: `pip install -r requirements.txt`)

2.  **Crear `.env` en la raíz del proyecto (NO subir a Git). Ejemplo:**
    ```
    GUPSHUP_API_KEY=tu_api_key
    GUPSHUP_APP_NAME=RolesClubBotToastmasters
    GUPSHUP_SOURCE=917834811114           # remitente sandbox (sin +)
    VERIFY_TOKEN=rolesclub-verify
    ADMIN_NUMBERS=521XXXXXXXXXX          # tu número admin (E.164 sin +)
    PORT=5000
    ```
    **Importante:**
    * Si no estás en `ADMIN_NUMBERS`, no podrás iniciar ronda ni usar comandos admin.
    * Provee un archivo `.env.example` (sin secretos) para el equipo.

---
## 🧩 Inicialización del Catálogo (Semilla)

> **Esta sección es obligatoria la primera vez.**

Genera la estructura de directorios y los archivos `data/<club_slug>/club.json` para los clubes definidos en el script.
Esto debe hacerse **antes de la primera vez que corras `app.py`**:

```bash
python src/setup_club.py
```
Verás un mensaje de confirmación por cada club creado.

---

## Ejecución local + ngrok + Webhook

1.  **Levantar Flask (en una terminal):**
    ```bash
    python src/app.py
    ```

2.  **Exponer el puerto con ngrok (en otra terminal):**
    ```bash
    ngrok http 5000
    ```
    Copia la URL pública que aparece (`https://xxxxx.ngrok-free.app`).

3.  **Configurar Webhook en Gupshup:**
    *   **Callback URL**: `https://<tu-url-ngrok>/webhook`
        *   Nota: El club se determina automáticamente por el número (waid) del remitente según `data/clubs/registry.json` y `data/clubs/<club_id>/club.json`.
    *   Asegúrate de que los eventos de mensajes estén activados.

4.  **Prueba rápida (desde un número admin):**
    *   Envía `HOLA` al número de WhatsApp del bot.
    *   Envía `INICIAR` para comenzar una ronda en el club correspondiente.
    *   Los miembros responden `ACEPTO` / `RECHAZO`.

**Consejo:** Abre `http://127.0.0.1:4040` para inspeccionar requests/responses de ngrok.

---
## Buenas prácticas de configuración/secrets

* `.env`: nunca en Git. Usa `.env.example` como plantilla.
* **Producción:** define variables de entorno en el servicio (Render/Railway/Heroku, Docker/compose, systemd) o usa un Secret Manager.
* Si alguna clave se subió por error: **ROTAR la clave y limpiar el historial.**

---
## Despliegue (alto nivel)

1.  Subir el repo a GitHub.
2.  PaaS (Render/Railway/Heroku) o un VPS detrás de Nginx+Gunicorn.
3.  Variables de entorno = valores de `.env`.
4.  Comando de inicio (ejemplo simple): `python src/app.py`
5.  Configurar Webhook de Gupshup a la URL pública de tu servicio:
    `https://<tu-app>.onrender.com/webhook`

---
## Solución de problemas comunes

* **No recibo mensajes en pruebas:**
    * Asegúrate de que enviaste “hola” al sandbox (ventana 24h).
    * Verifica que el webhook apunta a `/webhook` con protocolo https y Meta v3.
    * Revisa que `ADMIN_NUMBERS` tenga tu número en formato E.164 sin “+”.
* **ngrok no muestra tráfico:**
    * Abre `http://127.0.0.1:4040` para ver cada request.
* **Cambió la URL de ngrok:**
    * Actualiza el Webhook en Gupshup con la nueva URL.
    * Confirmar EditWebhook

---
## Contribución

* Crea una rama: `git checkout -b feature/nueva-funcionalidad`
* Commit con mensajes claros: `feat|fix|chore|docs: ...`
* Abre un Pull Request.

---
## Licencia

Proprietary. Consulta el archivo `LICENSE` para detalles.
