# programa_generator.py
# Generador de programa de sesión en HTML/PDF para Toastmasters

from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
from weasyprint import HTML
import base64
import qrcode
from io import BytesIO


def generate_qr_code(url: str) -> str:
    """Genera un QR code y lo retorna como base64 data URI."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    img_base64 = base64.b64encode(buffer.read()).decode()
    return f"data:image/png;base64,{img_base64}"


def get_next_tuesday() -> datetime:
    """Retorna el próximo martes a las 19:00."""
    today = datetime.now()
    days_ahead = 1 - today.weekday()  # 1 = Martes
    if days_ahead <= 0 or (days_ahead == 0 and today.hour >= 19):
        days_ahead += 7
    next_tuesday = today + timedelta(days=days_ahead)
    return next_tuesday.replace(hour=19, minute=0, second=0, microsecond=0)


def calculate_agenda_times(st: dict, start_time: datetime) -> List[Dict]:
    """
    Calcula los horarios de toda la agenda basándose en duraciones estándar.
    Retorna lista de items con: {time, duration_min, duration_avg, duration_max, title, member, css_class}
    """
    agenda = []
    current_time = start_time
    
    def add_item(duration_min, duration_avg, duration_max, title, member="", css_class=""):
        nonlocal current_time
        agenda.append({
            "time": current_time.strftime("%H:%M"),
            "duration_min": duration_min,
            "duration_avg": duration_avg,
            "duration_max": duration_max,
            "title": title,
            "member": member,
            "css_class": css_class
        })
        current_time += timedelta(minutes=duration_avg)
    
    # 1. Bienvenida del Oficial de Asambleas
    add_item(1, 1.5, 2, "🎉 Bienvenida del Oficial de Asambleas", "", "red-bg")
    
    # 2. Apertura de la Sesión
    add_item(0.5, 0.5, 1, "🎭 Apertura de la Sesión", "", "yellow-bg")
    
    # 3. Entrada del Toastmaster
    toastmaster = None
    for role, info in st.get("accepted", {}).items():
        if "toastmaster" in role.lower():
            toastmaster = info.get("name", "")
            break
    add_item(8, 9, 10, "🎤 Entrada del Toastmasters y Auto presentación", toastmaster, "yellow-bg")
    add_item(1, 1, 1, "Intervención del Toastmasters", toastmaster, "")
    
    # 4. Sección Educativa (si existe)
    educational_sections = st.get("educational_sections", [])
    if educational_sections:
        for section in educational_sections:
            add_item(5, 10, 20, f"📚 Sección Educativa: {section.get('nombre_seccion', 'N/A')}", 
                    section.get('nombre', ''), "")
            add_item(1, 1, 1, "Intervención del Toastmasters", toastmaster, "")
    
    # 5. Sección de Table Topics
    topic_master = None
    for role, info in st.get("accepted", {}).items():
        if "topic master" in role.lower() or "temas improvisados" in role.lower():
            topic_master = info.get("name", "")
            break
    add_item(12, 13, 15, "💬 Sección de Table Topics", topic_master, "")
    add_item(1, 1, 1, "Intervención del Toastmasters", toastmaster, "")
    
    # 6. Receso
    add_item(8, 9, 10, "☕ Receso, Toma de fotografía y Tiempo de reportarla", toastmaster, "orange-bg")
    add_item(1, 1, 1, "Intervención del Toastmasters", toastmaster, "")
    
    # 7. Discursos Preparados
    prepared_speeches = st.get("prepared_speeches", [])
    for idx, speech in enumerate(prepared_speeches, 1):
        dur_min = speech.get("duracion_min", 5)
        dur_max = speech.get("duracion_max", 7)
        dur_avg = (dur_min + dur_max) / 2
        
        speech_title = f"{speech.get('pathway', 'Pathway')}: Nivel {speech.get('nivel', '?')}"
        add_item(dur_min, dur_avg, dur_max, 
                f"🎤 Proyecto No. {idx} {speech_title}", 
                speech.get('nombre', ''), 
                "")
        
        # Título del discurso
        agenda.append({
            "time": "",
            "duration_min": "",
            "duration_avg": "",
            "duration_max": "",
            "title": f'Título del discurso: "{speech.get("titulo", "N/A")}"',
            "member": "",
            "css_class": "sub-item"
        })
        
        add_item(1, 1, 1, "Intervención del Toastmasters", toastmaster, "")
        
        # Evaluación del discurso
        evaluator = speech.get("evaluador", "Por asignar")
        add_item(2, 2.5, 3, f"📊 Evaluación Discurso Preparado #{idx}", evaluator, "")
        add_item(1, 1, 1, "Intervención del Toastmasters", toastmaster, "")
    
    # 8. Evaluador General
    eval_general = None
    for role, info in st.get("accepted", {}).items():
        if "evaluador general" in role.lower():
            eval_general = info.get("name", "")
            break
    add_item(2, 2, 3, "📈 Evaluación General", eval_general, "red-bg")
    add_item(1, 1, 1, "Intervención del Toastmasters", toastmaster, "")
    
    # 9. Evaluación Gramatical
    eval_gramatical = None
    for role, info in st.get("accepted", {}).items():
        if "evaluador gramatical" in role.lower():
            eval_gramatical = info.get("name", "")
            break
    add_item(2, 2, 2, "📝 Evaluación Gramatical:", eval_gramatical, "")
    
    # 10. Contador de Vacilaciones
    eval_muletillas = None
    for role, info in st.get("accepted", {}).items():
        if "muletillas" in role.lower():
            eval_muletillas = info.get("name", "")
            break
    add_item(1, 1, 2, "🔊 Contador de Vacilaciones:", eval_muletillas, "")
    
    # 11. Evaluación de Tiempo
    eval_tiempo = None
    for role, info in st.get("accepted", {}).items():
        if "evaluador del tiempo" in role.lower():
            eval_tiempo = info.get("name", "")
            break
    add_item(1, 1, 2, "⏱️ Evaluación de Tiempo:", eval_tiempo, "")
    
    add_item(1, 1, 1, "Intervención del Toastmasters", toastmaster, "")
    
    # 12. Resultados de Table Topics
    add_item(1, 1, 2, "🏆 Resultados de Table Topics", toastmaster, "")
    add_item(1, 1, 1, "Intervención del Toastmasters", toastmaster, "")
    
    # 13. Evaluación general
    add_item(13, 14, 15, "📊 Evaluación general:", eval_general, "")
    add_item(1, 1, 1, "Intervención del Toastmasters", toastmaster, "")
    
    # 14. Clausura
    add_item(4, 5, 6, "🎬 Clausura y Tiempo de Presidencia", toastmaster, "green-bg")
    
    return agenda


def generate_program_html(ctx, st: dict, session_number: int) -> str:
    """Genera el HTML del programa de la sesión."""
    template_path = Path(__file__).parent.parent / "assets" / "programa_template.html"
    logo_path = Path(__file__).parent.parent / "assets" / "toastmasters_logo.png"
    
    if not template_path.exists():
        raise FileNotFoundError(f"Template no encontrado: {template_path}")
    
    template = template_path.read_text(encoding="utf-8")
    
    # Obtener datos
    next_tuesday = get_next_tuesday()
    date_str = next_tuesday.strftime("%d/%m/%Y")
    
    theme = st.get("session_theme", {}).get("topic", "Por definir")
    word_data = st.get("word_of_the_day", {})
    word = word_data.get("palabra", "Elocuente")
    word_def = word_data.get("significado", "Persona o expresión que comunica con claridad, fuerza y habilidad.")
    
    # Calcular agenda
    agenda_items = calculate_agenda_times(st, next_tuesday)
    
    # Generar filas de la tabla
    agenda_rows_html = ""
    for item in agenda_items:
        time_cell = f'<td class="time-col">{item["time"]}</td>' if item["time"] else '<td></td>'
        
        if item["duration_avg"]:
            # Tres columnas de tiempo con colores semáforo
            green_cell = f'<td class="mins-col green-time">{item["duration_min"]}</td>'
            yellow_cell = f'<td class="mins-col yellow-time">{item["duration_avg"]}</td>'
            red_cell = f'<td class="mins-col red-time">{item["duration_max"]}</td>'
        else:
            green_cell = '<td></td>'
            yellow_cell = '<td></td>'
            red_cell = '<td></td>'
        
        css_class = f' class="{item["css_class"]}"' if item["css_class"] else ''
        title_cell = f'<td class="agenda-col"{css_class}>{item["title"]}</td>'
        member_cell = f'<td class="member-col">{item["member"]}</td>'
        
        agenda_rows_html += f'<tr>{time_cell}{green_cell}{yellow_cell}{red_cell}{title_cell}{member_cell}</tr>\n'
    
    # Generar QR code
    qr_code_data = generate_qr_code("https://linktr.ee/toastmastersfiuv")
    
    # Reemplazar placeholders
    html_content = template.replace("{{logo_path}}", str(logo_path))
    html_content = html_content.replace("{{qr_code}}", qr_code_data)
    html_content = html_content.replace("{{session_number}}", str(session_number))
    html_content = html_content.replace("{{theme}}", theme)
    html_content = html_content.replace("{{word}}", word)
    html_content = html_content.replace("{{word_definition}}", word_def)
    html_content = html_content.replace("{{date}}", date_str)
    html_content = html_content.replace("{{agenda_rows}}", agenda_rows_html)
    
    return html_content


def generate_program_pdf(ctx, st: dict, session_number: int = 627) -> Path:
    """Genera el PDF del programa y retorna la ruta del archivo."""
    # Por ahora usar número fijo 627
    session_number = 627
    html_content = generate_program_html(ctx, st, session_number)
    
    # Crear directorio de salida
    output_dir = Path(__file__).parent.parent / "generated_pdfs"
    output_dir.mkdir(exist_ok=True)
    
    # Nombre del archivo
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_path = output_dir / f"programa_sesion_{session_number}_{timestamp}.pdf"
    
    # Generar PDF
    HTML(string=html_content, base_url=str(Path(__file__).parent.parent)).write_pdf(pdf_path)
    
    return pdf_path
