import os
import re
import uuid
import time
import pdfplumber
import pandas as pd
import threading
from flask import Flask, render_template, request, send_file, session, redirect, url_for
from ics import Calendar, Event
from datetime import datetime, timedelta
import pytz
from calendar import monthrange

# Configurazione applicazione
app = Flask("analisi_griglia_pdf")
app.secret_key = "supersecretkey_prod_123!@#"

# Cartelle di lavoro
UPLOAD_FOLDER = "uploads"
ICS_FOLDER = "calendars"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ICS_FOLDER, exist_ok=True)

# Timezone e storage temporaneo
TZ = pytz.timezone("Europe/Rome")
TEMPORARY_STORAGE = {}
CLEANUP_INTERVAL = 3600  # 1 ora in secondi

# Pulizia automatica dello storage
def storage_cleanup():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()
        expired = [k for k, v in TEMPORARY_STORAGE.items() if now - v['timestamp'] > CLEANUP_INTERVAL * 2]
        for key in expired:
            del TEMPORARY_STORAGE[key]
        print(f"Pulizia storage: rimossi {len(expired)} elementi")

cleanup_thread = threading.Thread(target=storage_cleanup)
cleanup_thread.daemon = True
cleanup_thread.start()

# Funzioni di elaborazione PDF (invariate)
def extract_month_year_from_table(df):
    for cell in df.iloc[0]:
        if isinstance(cell, str):
            match = re.search(
                r"(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)[a-zA-Z]*[-_\s]?(\d{2,4})",
                cell, 
                re.IGNORECASE
            )
            if match:
                mese_abbr = match.group(1).lower()
                anno = match.group(2)
                if len(anno) == 2:
                    anno = "20" + anno
                months_map = {
                    "gen": "January", "feb": "February", "mar": "March",
                    "apr": "April", "mag": "May", "giu": "June",
                    "lug": "July", "ago": "August", "set": "September",
                    "ott": "October", "nov": "November", "dic": "December"
                }
                if mese_abbr in months_map:
                    return months_map[mese_abbr], int(anno)
    return None, None

def extract_table_from_pdf(pdf_path):
    tables = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if table:
                df = pd.DataFrame(table)
                df = df.dropna(axis=0, how="all")
                df = df.dropna(axis=1, how="all")
                tables.append(df)
    return pd.concat(tables, ignore_index=True) if tables else None

def translate_shifts(df, month, year):
    work_hours = {"d": 4, "e": 5, "f": 6, "g": 7, "h": 8}
    special_events = {"R1", "R2", "FER", "R0", "OFF", "FEST"}
    
    month_number = datetime.strptime(month, "%B").month
    _, num_days = monthrange(year, month_number)
    days = list(range(1, num_days + 1))
    
    pivot_data = {}
    
    for idx, row in df.iterrows():
        if idx == 0:
            continue
        
        nome_cell = row.iloc[0]
        if pd.isna(nome_cell) or not isinstance(nome_cell, str):
            continue
        
        nome = nome_cell.split(",")[0].strip()
        if nome not in pivot_data:
            pivot_data[nome] = {str(day): "" for day in days}
        
        for day in days:
            day_str = str(day)
            if day >= len(row):
                continue
                
            # Pulizia del valore: rimuovi spazi e caratteri non alfanumerici
            raw_value = str(row.iloc[day]).strip().lower() if (day < len(row) and not pd.isna(row.iloc[day])) else ""
            clean_value = re.sub(r'[^a-z0-9]', '', raw_value)  # Solo lettere e numeri
            
            if not clean_value:
                continue
                
            if clean_value in special_events:
                pivot_data[nome][day_str] = clean_value.upper()
            
            # Gestione valori tipo d20v -> d20
            elif len(clean_value) >= 2 and clean_value[0] in work_hours:
                prefix = clean_value[0]
                number_part = re.sub(r'[^0-9]', '', clean_value[1:])  # Estrai solo numeri
                
                if number_part:
                    try:
                        start_code = int(number_part)
                        duration = work_hours[prefix]
                        start_h = start_code / 2
                        start_time = f"{int(start_h):02d}:{'00' if start_h % 1 == 0 else '30'}"
                        pivot_data[nome][day_str] = f"{start_time} ({duration}h)"
                    except:
                        pivot_data[nome][day_str] = "Formato non valido"
                else:
                    pivot_data[nome][day_str] = clean_value.upper()
            
            else:
                pivot_data[nome][day_str] = clean_value.upper()
    
    translated_df = pd.DataFrame.from_dict(pivot_data, orient="index")
    translated_df.columns.name = "Giorno"
    translated_df.index.name = "Nome"
    translated_df.reset_index(inplace=True)
    
    cols = ["Nome"] + [str(day) for day in days]
    return translated_df[cols].fillna("")
    

def generate_ics_files(translated_df, month, year):
    ics_files = {}
    month_number = datetime.strptime(month, "%B").month
    special_events = {"R1", "R2", "FER", "R0", "OFF", "FEST"}
    
    for nome in translated_df["Nome"].unique():
        cal = Calendar()
        cal.creator = "Analisi Griglia PDF"
        cal.timezone = TZ.zone
        
        person_shifts = translated_df[translated_df["Nome"] == nome]
        
        for _, row in person_shifts.iterrows():
            for day_str in translated_df.columns[1:]:
                if day_str == "Nome":
                    continue
                    
                day = int(day_str)
                value = row[day_str]
                
                if not value or pd.isna(value):
                    continue
                
                try:
                    naive_date = datetime(year, month_number, day)
                    aware_date = TZ.localize(naive_date)
                    
                    if value in special_events:
                        event = Event()
                        event.name = value
                        event.begin = aware_date.replace(hour=0, minute=1)
                        event.end = aware_date.replace(hour=23, minute=59)
                    elif "(" in value and "h)" in value:
                        start_time, duration = value.split(" (")
                        duration = int(duration.replace("h)", ""))
                        start_h, start_m = map(int, start_time.split(":"))
                        
                        event = Event()
                        event.begin = aware_date.replace(hour=start_h, minute=start_m)
                        event.end = event.begin + timedelta(hours=duration)
                        event.name = f"Turno: {start_time} ({duration}h)"
                    
                    cal.events.add(event)
                except Exception as e:
                    print(f"Errore creazione evento: {str(e)}")
        
        file_path = os.path.join(ICS_FOLDER, f"{nome.replace(' ', '_')}.ics")
        with open(file_path, "w") as f:
            f.writelines(cal.serialize_iter())
        
        ics_files[nome] = file_path
    
    return ics_files
	
# Route principale
@app.route("/", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        file = request.files["file"]
        if not file or file.filename == '':
            return "Nessun file selezionato"

        # Salva il PDF
        pdf_path = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(pdf_path)
        
        # Elabora il PDF
        try:
            df = extract_table_from_pdf(pdf_path)
            if df is None or df.empty:
                return "Nessuna tabella trovata nel PDF"
            
            df = df.dropna(axis=0, how="all")
            mese, anno = extract_month_year_from_table(df)
            
            if not mese or not anno:
                return "Impossibile determinare mese/anno"
            
            translated_df = translate_shifts(df, mese, anno)
            ics_files = generate_ics_files(translated_df, mese, anno)

            # Salva nello storage temporaneo
            session_id = str(uuid.uuid4())
            TEMPORARY_STORAGE[session_id] = {
                'translated_df': translated_df.to_dict(),
                'mese': mese,
                'anno': anno,
                'ics_files': ics_files,
                'original_table': df.to_html(
                 classes='table table-borderless table-sm',
                 index=False,
                 border=0
                ),  
                'translated_table': translated_df.to_html(
                classes='table table-bordered table-sm',
                index=False,
                border=0,
                justify='center'
                ),
                'timestamp': time.time()
            }

            # Aggiorna la sessione
            session['current_session'] = session_id

            return render_template("result.html",
                                original_table=TEMPORARY_STORAGE[session_id]['original_table'],
                                translated_table=TEMPORARY_STORAGE[session_id]['translated_table'],
                                ics_files=ics_files,
                                mese=mese,
                                anno=anno)

        except Exception as e:
            return f"Errore durante l'elaborazione: {str(e)}"

    return render_template("upload.html")
    

# Route cambio turno
@app.route("/cambio-turno", methods=["GET", "POST"])
@app.route("/cambio-turno", methods=["GET", "POST"])
def cambio_turno():
    session_id = session.get('current_session')
    
    if not session_id or session_id not in TEMPORARY_STORAGE:
        return redirect(url_for("upload"))
    
    storage_data = TEMPORARY_STORAGE[session_id]
    translated_df = pd.DataFrame(storage_data['translated_df'])
    nomi = translated_df['Nome'].str.strip().unique().tolist()
    mese = storage_data['mese']
    anno = storage_data['anno']

    if request.method == "POST":
        nome_richiedente = request.form.get("nome", "").strip()
        giorno = request.form.get("giorno", "").strip()
        ora = request.form.get("ora", "").strip()
        durata = request.form.get("durata", "").strip()  # Riceve "4H", "6H" ecc.

        # Costruzione diretta del turno (formato DB: "08:30 (6H)")
        turno_richiesto = f"{ora} ({durata})"  # Diventa "08:30 (6H)"
        
        # Normalizzazione per confronto
        turno_richiesto_normalized = turno_richiesto.replace(" ", "").upper()
        
        matches = []
        try:
            giorno_int = int(giorno)
            giorno_str = str(giorno_int)
            
            for _, row in translated_df.iterrows():
                nome = str(row['Nome']).strip()
                turno_db = str(row[giorno_str]).strip().replace(" ", "").upper()
                
                if nome.lower() == nome_richiedente.lower():
                    continue
                    
                if turno_db == turno_richiesto_normalized:
                    matches.append(nome)
                    
        except Exception as e:
            errori = [f"Errore durante la ricerca: {str(e)}"]
            return render_template("cambio_turno.html",
                                nomi=nomi,
                                mese=mese,
                                anno=anno,
                                errors=errori)

        return render_template("cambio_turno.html",
                            nomi=nomi,
                            mese=mese,
                            anno=anno,
                            matches=matches,
                            giorno=giorno,
                            turno_richiesto=turno_richiesto)

    return render_template("cambio_turno.html",
                         nomi=nomi,
                         mese=mese,
                         anno=anno)
                         
# Download ICS
@app.route("/download/<nome>")
def download_ics(nome):
    session_id = session.get('current_session')
    if not session_id or session_id not in TEMPORARY_STORAGE:
        return redirect(url_for("upload"))
    
    ics_files = TEMPORARY_STORAGE[session_id]['ics_files']
    if nome not in ics_files:
        return "File non trovato"
    
    return send_file(ics_files[nome], as_attachment=True)

@app.route("/result")
def result():
    session_id = session.get('current_session')
    if not session_id or session_id not in TEMPORARY_STORAGE:
        return redirect(url_for("upload"))
    
    data = TEMPORARY_STORAGE[session_id]
    return render_template("result.html",
                        original_table=data['original_table'],
                        translated_table=data['translated_table'],
                        ics_files=data['ics_files'],
                        mese=data['mese'],
                        anno=data['anno'])
                        
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=False)
