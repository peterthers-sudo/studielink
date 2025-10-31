from flask import Flask, render_template, request, redirect, url_for, session, jsonify, make_response
import mysql.connector
import io, csv
from datetime import datetime
import random

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = "skift_mig_til_noget_unikt_og_hemmeligt"

# ----- DB -----
def get_db_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password="studielink",       # <- skift hvis nødvendigt
        database="studielink",
        auth_plugin="mysql_native_password"
    )

# Hvilke kolonner må opdateres via admin-API (i hovedtal_2025)
ALLOWED_COLUMNS = {
    "optomrnr", "optaget_ialt", "standby_ialt",
    "ansogninger_ialt", "1_priotitet_ans", "adgangskvotient", "standby_kvotient"
}

# Hjælpere til parsing/visning
def parse_kvot_val(v):
    """Returnér float for kvotient. 'Alle optaget' / 'Ledige pladser' → 2.0.
       Tillad komma som decimalseparator. Returnér None hvis ikke parsebar."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    low = s.lower()
    if "alle optaget" in low or "ledige pladser" in low:
        return 2.0
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None

def normalize_decimal_for_db(value):
    """Normalisér en decimalkolonne til DB:
       - tomt → None
       - 'Alle optaget'/'Ledige pladser' → None (vi gemmer tomt i DB)
       - '9,5' → '9.5'
       - ugyldigt → None
    """
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    low = s.lower()
    if "alle optaget" in low or "ledige pladser" in low:
        return None
    s = s.replace(",", ".")
    try:
        float(s)
        return s  # lad MySQL konvertere '9.5' til DECIMAL
    except ValueError:
        return None

def skaler_absolut(v):
    """Skaler tal v på skalaen 2.0→12.7 til 0–100%."""
    if v is None:
        return 0
    return max(0, min(100, (v - 2.0) / (12.7 - 2.0) * 100))


# =========================
#        Forside
# =========================
@app.route('/', methods=['GET', 'POST'])
def index():
    resultater = []
    fejl = None
    gennemsnit = None
    medtag_alle = False

    # bevar valg i dropdowns
    valgt_institution = request.form.get('institution', '')
    valgt_by = request.form.get('by', '')

    if request.method == 'POST':
        raw = request.form.get('gennemsnit', '').replace(',', '.').strip()
        medtag_alle = 'medtag_alle' in request.form

        # valider gennemsnit
        try:
            gennemsnit = float(raw)
        except ValueError:
            fejl = "Indtast et gyldigt tal mellem 2 og 12,7."
        if fejl is None and not (2.0 <= gennemsnit <= 12.7):
            fejl = "Karakter skal være mellem 2 og 12,7."

        if fejl is None:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            # JOIN: hovedtal_2025 (h) + udbud_2025 (u) + uddannelser_2025 (ud)
            base = """
                SELECT 
                    h.id,
                    u.`Uddannelse` AS navn,
                    u.`Ejerinstitution` AS institution,
                    u.`Foregar_pa_by` AS by_navn,
                    u.`Studiestart` AS studiestart,
                    u.`Link_til_info_om_udbud` AS info_link,
                    h.`adgangskvotient`
                FROM hovedtal_2025 h
                JOIN udbud_2025 u ON u.`KOT-nummer` = h.`optomrnr`
                LEFT JOIN uddannelser_2025 ud ON ud.`Uddannelse` = u.`Uddannelse`
                WHERE 1=1
            """
            params = []
            if not medtag_alle:
                base += " AND (h.`adgangskvotient` IS NULL OR h.`adgangskvotient` <= %s + 0.5)"
                params.append(gennemsnit)

            if valgt_institution.strip():
                base += " AND u.`Ejerinstitution` = %s"
                params.append(valgt_institution.strip())

            if valgt_by.strip():
                base += " AND u.`Foregar_pa_by` LIKE %s"
                params.append("%" + valgt_by.strip() + "%")

            # sortér på kvotient (numerisk) og navn
            base += " ORDER BY h.`adgangskvotient` DESC, navn ASC"

            cursor.execute(base, params)
            rows = cursor.fetchall()
            cursor.close()
            conn.close()

            # Berig rækkerne
            for r in rows:
                kvot = parse_kvot_val(r.get("adgangskvotient"))
                # Formater til visning
                if kvot is not None:
                    r["adgangskvotient"] = f"{kvot:.1f}".replace('.', ',')

                # Hvis vi ikke medtager alle: filtrér væk rækker der ligger >0,5 over, når kvot er tal
                if not medtag_alle and kvot is not None and kvot > (gennemsnit + 0.5):
                    continue

                diff = None if kvot is None else round(gennemsnit - kvot, 1)
                r["diff"] = diff
                if kvot is None:
                    r["adgangskvotient"] = "Åbent optag"

                if diff is None:
                    r["tekst"] = "Åbent optag (ingen grænse)"
                    r["kategori"] = "grøn"
                else:
                    if diff <= -0.6:
                        r["tekst"] = f"Langt fra – mangler {abs(diff)} point"
                        r["kategori"] = "rød"
                    elif -0.5 <= diff < -0.1:
                        r["tekst"] = f"Tæt på – mangler {abs(diff)} point"
                        r["kategori"] = "orange"
                    elif -0.1 <= diff <= 0.0:
                        r["tekst"] = "Spot on!"
                        r["kategori"] = "gul"
                    elif 0.1 <= diff <= 0.5:
                        r["tekst"] = f"Lidt over grænsen med {abs(diff)} point"
                        r["kategori"] = "lysegrøn"
                    elif diff >= 0.6:
                        r["tekst"] = f"Sikkert optaget – {abs(diff)} point over grænsen"
                        r["kategori"] = "grøn"

                r["bredde"] = skaler_absolut(gennemsnit)
                r["markering"] = skaler_absolut(kvot if kvot is not None else 2.0)

                resultater.append(r)

    # dropdown-data til formularen (fra udbud_2025)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT `Ejerinstitution` FROM udbud_2025 WHERE `Ejerinstitution` IS NOT NULL AND `Ejerinstitution`<>'' ORDER BY `Ejerinstitution`;")
    institutioner = [x[0] for x in cur.fetchall()]
    cur.execute("SELECT DISTINCT `Foregar_pa_by` FROM udbud_2025 WHERE `Foregar_pa_by` IS NOT NULL AND `Foregar_pa_by`<>'' ORDER BY `Foregar_pa_by`;")
    byer = [x[0] for x in cur.fetchall()]
    cur.close()
    conn.close()

    return render_template(
        'index.html',
        resultater=resultater,
        fejl=fejl,
        gennemsnit=gennemsnit,
        medtag_alle=medtag_alle,
        institutioner=institutioner,
        byer=byer,
        valgt_institution=valgt_institution,
        valgt_by=valgt_by,
        mode="normal"
    )


# =========================
#        LOGIN
# =========================
@app.route('/login', methods=['GET', 'POST'])
def login():
    fejl = None
    if request.method == 'POST':
        u = request.form.get('brugernavn', '')
        p = request.form.get('adgangskode', '')
        if u == "admin" and p == "studielink":
            session['logged_in'] = True
            return redirect(url_for('admin'))
        fejl = "Forkert brugernavn eller adgangskode."
    return render_template('login.html', fejl=fejl)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


# =========================
#        ADMIN
# =========================
@app.route('/admin')
def admin():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    # Vis en "joined" liste som før – men redigerbare felter er fra hovedtal_2025
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT
            h.id,
            h.`optomrnr`,
            u.`Uddannelse` AS navn,
            u.`Ejerinstitution` AS institution,
            u.`Foregar_pa_by` AS by_navn,
            u.`Studiestart` AS studiestart,
            h.`optaget_ialt`,
            h.`standby_ialt`,
            h.`ansogninger_ialt`,
            h.`1_priotitet_ans`,
            h.`adgangskvotient`,
            h.`standby_kvotient`
        FROM hovedtal_2025 h
        JOIN udbud_2025 u ON u.`KOT-nummer` = h.`optomrnr`
        LEFT JOIN uddannelser_2025 ud ON ud.`Uddannelse` = u.`Uddannelse`
        ORDER BY h.id;
    """)
    data = cur.fetchall()
    cur.close()
    conn.close()

    # dropdown-data til filtrering (fra udbud_2025)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT `Foregar_pa_by` FROM udbud_2025 WHERE `Foregar_pa_by` IS NOT NULL AND `Foregar_pa_by`<>'' ORDER BY `Foregar_pa_by`;")
    byer = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT `Ejerinstitution` FROM udbud_2025 WHERE `Ejerinstitution` IS NOT NULL AND `Ejerinstitution`<>'' ORDER BY `Ejerinstitution`;")
    institutioner = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    return render_template('admin.html', data=data, byer=byer, institutioner=institutioner)


# --- Gem én celle (blur) ---
@app.route('/update_udbud', methods=['POST'])
def update_udbud():
    if not session.get('logged_in'):
        return "Ikke autoriseret", 403

    d = request.get_json()
    col = d.get('column')
    if col not in ALLOWED_COLUMNS:
        return "Kolonne ikke tilladt", 400

    val = d.get('value')
    id_val = d.get('id')

    # normalisér decimalfelter
    if col in ('adgangskvotient', 'standby_kvotient'):
        val = normalize_decimal_for_db(val)

    conn = get_db_connection()
    cur = conn.cursor()
    sql = f"UPDATE hovedtal_2025 SET `{col}`=%s WHERE id=%s"
    cur.execute(sql, (val, id_val))
    conn.commit()
    cur.close()
    conn.close()
    return "OK"


# --- Gem alt (batch) ---
@app.route('/update_batch', methods=['POST'])
def update_batch():
    if not session.get('logged_in'):
        return "Ikke autoriseret", 403

    rows = request.get_json() or []
    conn = get_db_connection()
    cur = conn.cursor()

    for r in rows:
        col = r.get('column')
        if col not in ALLOWED_COLUMNS:
            continue
        val = r.get('value')
        id_val = r.get('id')

        if col in ('adgangskvotient', 'standby_kvotient'):
            val = normalize_decimal_for_db(val)

        sql = f"UPDATE hovedtal_2025 SET `{col}`=%s WHERE id=%s"
        cur.execute(sql, (val, id_val))

    conn.commit()
    cur.close()
    conn.close()
    return "OK"


# --- Eksportér CSV (fra hovedtal_2025 + lidt kontekst) ---
@app.route('/export_csv')
def export_csv():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT
            h.id,
            h.`optomrnr`,
            u.`Uddannelse` AS navn,
            u.`Ejerinstitution` AS institution,
            u.`Foregar_pa_by` AS by_navn,
            u.`Studiestart` AS studiestart,
            h.`optaget_ialt`,
            h.`standby_ialt`,
            h.`ansogninger_ialt`,
            h.`1_priotitet_ans`,
            h.`adgangskvotient`,
            h.`standby_kvotient`
        FROM hovedtal_2025 h
        JOIN udbud_2025 u ON u.`KOT-nummer` = h.`optomrnr`
        ORDER BY h.id;
    """)
    data = cur.fetchall()
    cur.close()
    conn.close()

    if not data:
        return "Ingen data at eksportere", 400

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(data[0].keys()), delimiter=';')
    writer.writeheader()
    writer.writerows(data)
    csv_data = output.getvalue().encode('utf-8-sig')

    resp = make_response(csv_data)
    resp.headers["Content-Disposition"] = "attachment; filename=hovedtal_2025_export.csv"
    resp.headers["Content-Type"] = "text/csv"
    return resp


# --- Importér CSV (til hovedtal_2025) ---
@app.route('/import_csv', methods=['POST'])
def import_csv():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    if 'file' not in request.files:
        return "Ingen fil modtaget", 400
    f = request.files['file']
    if f.filename == '':
        return "Ingen fil valgt", 400

    content = f.read().decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(content), delimiter=';')
    rows = list(reader)

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    rapport = []
    opdateret = 0
    indsat = 0
    sprunget = 0

    for row in rows:
        # trim alle felter
        row = {k: (v.strip() if v is not None else "") for k, v in row.items()}

        id_val = row.get("id", "").strip()
        optomrnr = row.get("optomrnr", "").strip()

        # Byg sæt af kolonner fra filen, begrænset til ALLOWED_COLUMNS
        incoming_cols = [c for c in row.keys() if c in ALLOWED_COLUMNS]

        # Normalisér decimalkolonner
        normd = {}
        for c in incoming_cols:
            v = row.get(c, None)
            if c in ('adgangskvotient', 'standby_kvotient'):
                v = normalize_decimal_for_db(v)
            if isinstance(v, str) and v == "":
                v = None
            normd[c] = v

        # Find eksisterende række: først via id, ellers via optomrnr (hvis unikt)
        exists = False
        existing_id = None
        if id_val:
            cur.execute("SELECT id FROM hovedtal_2025 WHERE id=%s", (id_val,))
            r = cur.fetchone()
            if r:
                exists = True
                existing_id = r["id"]
        elif optomrnr:
            cur.execute("SELECT id FROM hovedtal_2025 WHERE `optomrnr`=%s", (optomrnr,))
            r = cur.fetchone()
            if r:
                exists = True
                existing_id = r["id"]

        if exists:
            if normd:
                set_bits = ", ".join(f"`{k}`=%s" for k in normd.keys())
                sql = f"UPDATE hovedtal_2025 SET {set_bits} WHERE id=%s"
                cur.execute(sql, (*normd.values(), existing_id))
            opdateret += 1
            rapport.append(f"✅ Opdateret id={existing_id} (optomrnr={optomrnr or 'ukendt'})")
        else:
            # Indsæt – kræver som minimum optomrnr (ellers spring)
            if not optomrnr:
                rapport.append("❌ Mangler optomrnr – sprunget (ny række kræver optomrnr)")
                sprunget += 1
                continue
            cols = ["optomrnr"] + sorted([c for c in normd.keys() if c != "optomrnr"])
            vals = [optomrnr] + [normd[c] for c in cols if c != "optomrnr"]
            placeholders = ", ".join(["%s"] * len(cols))
            sql = f"INSERT INTO hovedtal_2025 ({', '.join('`'+c+'`' for c in cols)}) VALUES ({placeholders})"
            cur.execute(sql, vals)
            indsat += 1
            rapport.append(f"✅ Oprettet NY (optomrnr={optomrnr})")

    conn.commit()
    cur.close()
    conn.close()

    # skriv rapport til fil og vis i browser
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = "\n".join(rapport)

    html = f"""
    <h2>Import fuldført</h2>
    <p><strong>{opdateret}</strong> opdateret, <strong>{indsat}</strong> nyoprettet, <strong>{sprunget}</strong> sprunget over.</p>
    <pre style="background:#f4f4f4;padding:1rem;border-radius:8px;white-space:pre-wrap;">{body}</pre>
    <p><a href="/admin">← Tilbage til admin</a></p>
    """
    return html


@app.route('/vaelg_for_mig', methods=['POST'])
def vaelg_for_mig():
    # Hent brugerens input
    gennemsnit_raw = request.form.get('gennemsnit', '').replace(',', '.').strip()
    medtag_alle = 'medtag_alle' in request.form
    valgt_institution = request.form.get('institution', '')
    valgt_by = request.form.get('by', '')

    # Valider gennemsnit
    try:
        gennemsnit = float(gennemsnit_raw)
    except ValueError:
        return redirect(url_for('index'))

    if not (2.0 <= gennemsnit <= 12.7):
        return redirect(url_for('index'))

    # Hent én tilfældig uddannelse ud fra filtrene
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    base = """
        SELECT 
            h.id,
            u.`Uddannelse` AS navn,
            u.`Ejerinstitution` AS institution,
            u.`Foregar_pa_by` AS by_navn,
            u.`Studiestart` AS studiestart,
            u.`Link_til_info_om_udbud` AS info_link,
            h.`adgangskvotient`
        FROM hovedtal_2025 h
        JOIN udbud_2025 u ON u.`KOT-nummer` = h.`optomrnr`
        LEFT JOIN uddannelser_2025 ud ON ud.`Uddannelse` = u.`Uddannelse`
        WHERE 1=1
    """
    params = []

    if not medtag_alle:
        base += " AND (h.`adgangskvotient` IS NULL OR h.`adgangskvotient` <= %s + 0.5)"
        params.append(gennemsnit)

    if valgt_institution.strip():
        base += " AND u.`Ejerinstitution` = %s"
        params.append(valgt_institution.strip())

    if valgt_by.strip():
        base += " AND u.`Foregar_pa_by` LIKE %s"
        params.append("%" + valgt_by.strip() + "%")

    base += " ORDER BY RAND() LIMIT 1"
    cursor.execute(base, params)
    valgt = cursor.fetchone()
    cursor.close()
    conn.close()

    if not valgt:
        return redirect(url_for('index'))

    # Formater kvotient
    kvot = parse_kvot_val(valgt.get("adgangskvotient"))
    if kvot is None:
        valgt["adgangskvotient"] = "Åbent optag"
    else:
        valgt["adgangskvotient"] = f"{kvot:.1f}".replace('.', ',')

    # === Berig præcis som i index() ===
    diff = None if kvot is None else round(gennemsnit - kvot, 1)
    valgt["diff"] = diff

    if kvot is None:
        valgt["tekst"] = "Åbent optag (ingen grænse)"
        valgt["kategori"] = "grøn"
    else:
        if diff <= -0.6:
            valgt["tekst"] = f"Langt fra – mangler {abs(diff)} point"
            valgt["kategori"] = "rød"
        elif -0.5 <= diff < -0.1:
            valgt["tekst"] = f"Tæt på – mangler {abs(diff)} point"
            valgt["kategori"] = "orange"
        elif -0.1 <= diff <= 0.0:
            valgt["tekst"] = "Spot on!"
            valgt["kategori"] = "gul"
        elif 0.1 <= diff <= 0.5:
            valgt["tekst"] = f"Lidt over grænsen med {abs(diff)} point"
            valgt["kategori"] = "lysegrøn"
        elif diff >= 0.6:
            valgt["tekst"] = f"Sikkert optaget – {abs(diff)} point over grænsen"
            valgt["kategori"] = "grøn"

    valgt["bredde"] = skaler_absolut(gennemsnit)
    valgt["markering"] = skaler_absolut(kvot if kvot is not None else 2.0)

    # Hent dropdowns (så siden stadig har formularen intakt)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT `Ejerinstitution` FROM udbud_2025 WHERE `Ejerinstitution` IS NOT NULL AND `Ejerinstitution`<>'' ORDER BY `Ejerinstitution`;")
    institutioner = [x[0] for x in cur.fetchall()]
    cur.execute("SELECT DISTINCT `Foregar_pa_by` FROM udbud_2025 WHERE `Foregar_pa_by` IS NOT NULL AND `Foregar_pa_by`<>'' ORDER BY `Foregar_pa_by`;")
    byer = [x[0] for x in cur.fetchall()]
    cur.close()
    conn.close()

    # Returnér index-siden med KUN dette ene resultat
    return render_template(
        'index.html',
        resultater=[valgt],
        fejl=None,
        gennemsnit=gennemsnit,
        medtag_alle=medtag_alle,
        institutioner=institutioner,
        byer=byer,
        valgt_institution=valgt_institution,
        valgt_by=valgt_by,
        mode="random"
    )


# =========================
#        Kvote 2
# =========================
@app.route("/kvote2", methods=["GET", "POST"])
def kvote2():
    resultater = None
    score_total = None
    max_points = None
    chance_score = None

    if request.method == "POST":

        # --- 1) Kvotient → 0-5 point ---
        snit_raw = request.form.get("snit")
        if snit_raw:
            try:
                kvotient = float(snit_raw.replace(",", "."))
                kvotient_score = max(0, min(5, (kvotient - 2) / (12.7 - 2) * 5))
            except:
                kvotient_score = 0
        else:
            kvotient_score = 0

        # --- 2) Øvrige kriterier (0-5) ---
        score_total = (
            kvotient_score +
            int(request.form.get("score_erhverv") or 0) +
            int(request.form.get("score_udland") or 0) +
            int(request.form.get("score_hojskole") or 0) +
            int(request.form.get("score_ansogning") or 0) +
            int(request.form.get("score_projekter") or 0)
        )

        max_points = 5 * 6  # 6 kriterier á 5 point
        chance_score = score_total / max_points  # 0.00 - 1.00

        # --- 3) Hent uddannelser (navn) via JOIN, så vi har konsistente felter ---
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT 
                h.id,
                u.`Uddannelse` AS navn,
                u.`Ejerinstitution` AS institution,
                u.`Foregar_pa_by` AS by_navn
            FROM hovedtal_2025 h
            JOIN udbud_2025 u ON u.`KOT-nummer` = h.`optomrnr`
        """)
        rows = cursor.fetchall()
        conn.close()

        # --- 4) Kategori ud fra samlet score ---
        def kategori_fra_score(c):
            if c >= 0.80:
                return "grøn"
            elif c >= 0.55:
                return "gul"
            elif c >= 0.35:
                return "orange"
            return "rød"

        for r in rows:
            r["kategori"] = kategori_fra_score(chance_score)

        # --- 5) Sortér bedste chance først ---
        sort_order = {"grøn": 1, "gul": 2, "orange": 3, "rød": 4}
        rows.sort(key=lambda r: (sort_order.get(r["kategori"], 99), r.get("navn", "")))

        resultater = rows

    # ✅ ALWAYS RETURN — both GET & POST -->
    return render_template("kvote2.html",
                           resultater=resultater,
                           score_total=score_total,
                           max_points=max_points,
                           chance_score=chance_score)



# =========================
#     APP START
# =========================
if __name__ == '__main__':
    app.run(debug=True)
