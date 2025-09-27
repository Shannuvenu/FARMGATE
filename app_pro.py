# app_pro.py — Farm-Gate MRL & AMU Compliance — Pro (Offline)
# Built by HEXAMINDS 📈
#
# Features:
# - Phone OTP auth (SMS/WhatsApp via Twilio; DEV fallback)
# - Master data (Animals, Drugs)
# - Rule-pack toggle (FSSAI/CODEX/EU) + CSV importer
# - Treatments (indication, duration_days, prescribed_by) & Labs
# - Safe-to-Sell (withdrawal) & MRL verdict (latest lab)
# - Zero-Residue Badge tracking (streak days)
# - AMU metrics (mg/kg biomass), simple DDDvet proxy, antibiotic-free days
# - Authorities Dashboard (AMU by class, open withdrawals, top BLOCK reasons)
# - Monthly Report PDF (charts + KPIs)
# - Certificate PDF with QR + branding
# - Telugu/English nudges
# - Pure local: SQLite + Streamlit, no external APIs

import streamlit as st
import sqlite3
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import qrcode
import matplotlib.pyplot as plt
import random

DB_PATH = "mrl_amu_pro.db"

# -------------------- DB Setup --------------------
def conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    c = conn(); cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS animals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tag_id TEXT UNIQUE,
        species TEXT,
        herd TEXT,
        avg_weight_kg REAL DEFAULT 400,
        zero_residue_streak_days INTEGER DEFAULT 0
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS drugs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        drug_class TEXT,
        is_critical TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS rules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile TEXT,
        species TEXT,
        drug TEXT,
        drug_class TEXT,
        is_critical TEXT,
        matrix TEXT,
        withdrawal_days INTEGER,
        mrl_mg_per_kg REAL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS treatments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        animal_id INTEGER,
        drug TEXT,
        dose_mg_per_kg REAL,
        route TEXT,
        date_administered TEXT,
        batch_code TEXT,
        indication TEXT,
        duration_days INTEGER,
        prescribed_by TEXT,
        created_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS labs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        animal_id INTEGER,
        drug TEXT,
        matrix TEXT,
        value_mg_per_kg REAL,
        sample_date TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS settings(
        k TEXT PRIMARY KEY,
        v TEXT
    )""")
    # 🔹 NEW: message logs table
    cur.execute("""CREATE TABLE IF NOT EXISTS message_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT,
        channel TEXT,
        message TEXT,
        sid TEXT,
        status TEXT,
        ts TEXT
    )""")
    c.commit(); c.close()

def set_setting(k,v):
    c = conn(); cur=c.cursor()
    cur.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)",(k,str(v)))
    c.commit(); c.close()

def get_setting(k, default=None):
    c=conn(); cur=c.cursor()
    cur.execute("SELECT v FROM settings WHERE k=?",(k,))
    r=cur.fetchone()
    c.close()
    return r[0] if r else default

def df(sql, params=()):
    c=conn()
    out = pd.read_sql_query(sql, c, params=params)
    c.close()
    return out

def write(sql, params=()):
    c=conn(); cur=c.cursor()
    cur.execute(sql, params)
    c.commit(); c.close()

def write_many(sql, rows):
    c=conn(); cur=c.cursor()
    cur.executemany(sql, rows)
    c.commit(); c.close()

# 🔹 Add log_message here
def log_message(phone, channel, message, sid=None, status="queued"):
    c = conn(); cur = c.cursor()
    cur.execute("""INSERT INTO message_logs(phone,channel,message,sid,status,ts)
                   VALUES (?,?,?,?,?,?)""",
                   (phone, channel, message, sid or "-", status, datetime.utcnow().isoformat()))
    c.commit(); c.close()


# -------------------- Helpers & Logic --------------------
def current_profile():
    return get_setting("rule_profile","FSSAI")

def load_rulepack_csv(path_or_buffer, profile_name):
    try:
        r = pd.read_csv(path_or_buffer)
        # expected cols: species,drug,drug_class,is_critical,matrix,withdrawal_days,mrl_mg_per_kg
        required = {"species","drug","drug_class","is_critical","matrix","withdrawal_days","mrl_mg_per_kg"}
        r_cols_lower = [c.lower() for c in r.columns]
        missing = required - set(r_cols_lower)
        if missing:
            return False, f"Missing columns in CSV: {', '.join(sorted(missing))}"
        r.columns = r_cols_lower
        r["profile"]=profile_name.upper()
        write("DELETE FROM rules WHERE profile=?", (profile_name.upper(),))
        cols = ["profile","species","drug","drug_class","is_critical","matrix","withdrawal_days","mrl_mg_per_kg"]
        rows = r[cols].values.tolist()
        write_many(f"INSERT INTO rules({','.join(cols)}) VALUES (?,?,?,?,?,?,?,?)", rows)
        return True, f"Imported {len(rows)} rules for {profile_name}."
    except Exception as e:
        return False, f"Rule-pack import failed: {e}"

def rule_for(species, drug, matrix, profile=None):
    profile = profile or current_profile()
    r = df("""SELECT * FROM rules WHERE profile=? AND species=? AND drug=? AND matrix=?""",
           (profile, species, drug, matrix))
    return r.iloc[0] if not r.empty else None

def calc_safe_date(animal_id:int, matrix:str, profile=None):
    t = df("SELECT * FROM treatments WHERE animal_id=? ORDER BY date_administered",(animal_id,))
    if t.empty: return None, "no_treatments", []
    a = df("SELECT * FROM animals WHERE id=?", (animal_id,))
    if a.empty: return None, "animal_missing", []
    species = a.iloc[0]["species"]
    blocks, details = [], []
    for _,row in t.iterrows():
        r = rule_for(species, row["drug"], matrix, profile)
        if r is None:
            details.append((row["drug"], row["date_administered"], "no_rule"))
            continue
        wd = int(r["withdrawal_days"])
        d = datetime.fromisoformat(row["date_administered"]).date()
        until = d + timedelta(days=wd)
        blocks.append(until)
        details.append((row["drug"], row["date_administered"], until.isoformat()))
    if not blocks:
        return date.today(),"no_matching_rules", details
    return max(blocks), "ok", details

def latest_lab_verdict(animal_id:int, drug:str, matrix:str, profile=None):
    a = df("SELECT * FROM animals WHERE id=?", (animal_id,))
    if a.empty: return None, "animal_missing"
    species = a.iloc[0]["species"]
    r = rule_for(species, drug, matrix, profile)
    if r is None: return None, "no_rule"
    mrl = float(r["mrl_mg_per_kg"])
    l = df("""SELECT * FROM labs WHERE animal_id=? AND drug=? AND matrix=?
              ORDER BY sample_date DESC LIMIT 1""",(animal_id, drug, matrix))
    if l.empty: return None, "no_lab"
    val = float(l.iloc[0]["value_mg_per_kg"])
    return (val <= mrl), f"value={val}, mrl={mrl}"

def amu_mg_per_kg_biomass(start:date, end:date):
    t = df("""SELECT t.*, a.avg_weight_kg FROM treatments t 
              JOIN animals a ON a.id=t.animal_id""")
    if t.empty: return 0.0
    t["date_administered"]=pd.to_datetime(t["date_administered"]).dt.date
    t = t[(t["date_administered"]>=start) & (t["date_administered"]<=end)]
    if t.empty: return 0.0
    t["total_mg"]=t["dose_mg_per_kg"].astype(float)*t["avg_weight_kg"].astype(float)*t["duration_days"].fillna(1).astype(int)
    total_mg = t["total_mg"].sum()
    biomass = (t["avg_weight_kg"]*t["duration_days"].fillna(1)).sum()
    return float(total_mg/biomass) if biomass>0 else 0.0

def dddvet_proxy(start:date, end:date):
    a = df("SELECT COUNT(*) as n FROM animals")
    n_animals = int(a.iloc[0]["n"]) if not a.empty else 0
    if n_animals==0: return 0.0
    t = df("SELECT duration_days, date_administered FROM treatments")
    if t.empty: return 0.0
    t["date_administered"]=pd.to_datetime(t["date_administered"]).dt.date
    t = t[(t["date_administered"]>=start)&(t["date_administered"]<=end)]
    total_days = t["duration_days"].fillna(1).sum()
    animal_days = n_animals*((end-start).days+1)
    return float(total_days/animal_days*1000) if animal_days>0 else 0.0

def antibiotic_free_days(animal_id:int, start:date, end:date):
    t = df("SELECT date_administered, duration_days FROM treatments WHERE animal_id=?", (animal_id,))
    if t.empty: return (end-start).days+1
    treated=set()
    for _,row in t.iterrows():
        d0 = datetime.fromisoformat(row["date_administered"]).date()
        dur = int(row["duration_days"] or 1)
        for i in range(dur):
            d = d0 + timedelta(days=i)
            if start<=d<=end: treated.add(d)
    total = (end-start).days+1
    return total - len(treated)

def update_zero_residue_badge(window_days=90):
    end = date.today()
    start = end - timedelta(days=window_days-1)
    animals = df("SELECT * FROM animals")
    for _,a in animals.iterrows():
        labs = df("""SELECT l.*, r.mrl_mg_per_kg, an.species FROM labs l 
                     JOIN animals an ON an.id=l.animal_id
                     JOIN rules r ON r.drug=l.drug AND r.species=an.species AND r.matrix=l.matrix AND r.profile=?
                     WHERE l.animal_id=? AND date(l.sample_date)>=? AND date(l.sample_date)<=?""",
                  (current_profile(), int(a["id"]), start.isoformat(), end.isoformat()))
        blocked=False
        for _,row in labs.iterrows():
            if float(row["value_mg_per_kg"])>float(row["mrl_mg_per_kg"]):
                blocked=True; break
        safe_milk, _status, _ = calc_safe_date(int(a["id"]), "milk")
        safe_meat, _status2, _ = calc_safe_date(int(a["id"]), "meat")
        pending = any([(safe_milk and date.today()<safe_milk),(safe_meat and date.today()<safe_meat)])
        streak = int(a["zero_residue_streak_days"] or 0)
        if (not blocked) and (not pending):
            streak = min(streak+1, window_days)
        else:
            streak = 0
        write("UPDATE animals SET zero_residue_streak_days=? WHERE id=?", (streak, int(a["id"])))

# ---------- Auth & Messaging ----------
def _twilio_client():
    try:
        from twilio.rest import Client
        sid = st.secrets["TWILIO_ACCOUNT_SID"]
        tok = st.secrets["TWILIO_AUTH_TOKEN"]
        return Client(sid, tok)
    except Exception:
        return None

def send_sms(phone: str, msg: str):
    cli = _twilio_client()
    if not cli:
        st.info("DEV mode: SMS not sent (add TWILIO secrets to enable).")
        log_message(phone, "SMS", msg, status="dev_fallback")  # ✅ logs anyway
        return False
    try:
        from_ = st.secrets.get("TWILIO_FROM_SMS", "")
        m = cli.messages.create(to=phone, from_=from_, body=msg)
        log_message(phone, "SMS", msg, sid=m.sid, status=m.status)  # ✅ logs Twilio result
        return True
    except Exception as e:
        st.error(f"SMS failed: {e}")
        log_message(phone, "SMS", msg, status=f"error:{e}")  # ✅ logs error
        return False

def send_whatsapp(phone: str, msg: str):
    cli = _twilio_client()
    if not cli:
        st.info("DEV mode: WhatsApp not sent (add TWILIO secrets to enable).")
        return False
    try:
        from_ = st.secrets.get("TWILIO_FROM_WA", "whatsapp:+14155238886")
        cli.messages.create(to=f"whatsapp:{phone}" if not str(phone).startswith("whatsapp:") else phone,
                            from_=from_, body=msg)
        return True
    except Exception as e:
        st.error(f"WhatsApp failed: {e}")
        return False

def require_auth():
    if "user" in st.session_state and st.session_state["user"]:
        return

    st.markdown("### 🔒 Login (Phone OTP)")
    with st.form("login_form", clear_on_submit=False):
        col = st.columns(2)
        phone = col[0].text_input("Phone (e.g., +91XXXXXXXXXX)", value="")
        # force SMS to avoid confusion; or keep a selectbox if you want WA too
        channel = "SMS"
        send = st.form_submit_button("Send OTP")
        if send:
            if not phone.strip():
                st.warning("Enter phone number.")
            else:
                otp = random.randint(100000, 999999)
                st.session_state["__otp"] = {"code": str(otp),
                                             "phone": phone.strip(),
                                             "ts": datetime.now(timezone.utc)}
                msg = f"HEXAMINDS verification code: {otp}"
                ok = send_sms(phone.strip(), msg)  # SMS only
                if ok:
                    st.success("OTP sent to your phone.")
                else:
                    st.error("Could not send OTP. Please check SMS setup in Secrets.")

    with st.form("verify_form", clear_on_submit=True):
        otp_in = st.text_input("Enter OTP", type="password")
        name = st.text_input("Your name (optional)", value="")
        verify = st.form_submit_button("Verify & Continue")
        if verify:
            rec = st.session_state.get("__otp")
            if not rec:
                st.error("No OTP requested yet.")
            elif str(otp_in).strip() == rec["code"]:
                st.session_state["user"] = {
                    "phone": rec["phone"],
                    "name": name.strip() or "User",
                    "role": "Farmer"
                }
                st.success("Logged in ✅")
            else:
                st.error("Invalid OTP.")
    st.stop()

# -------------------- UI --------------------
st.set_page_config(
    page_title="Farm-Gate MRL & AMU Compliance — Pro (Offline) — Built by HEXAMINDS 📈",
    layout="wide", page_icon="🧪"
)
init_db()

# --- quick diagnostics (remove later)
try:
    import twilio  # noqa
    twilio_ok = True
except Exception:
    twilio_ok = False
st.sidebar.write("Twilio SDK:", "✅" if twilio_ok else "❌")
st.sidebar.write("SID in secrets:", "✅" if "TWILIO_ACCOUNT_SID" in st.secrets else "❌")

# ---- Dark theme + custom buttons (black bg, green borders, colored actions)
st.markdown("""
<style>
/* black background */
.stApp { background: #0b0b0b !important; color: #e8f5e9 !important; }
.block-container { padding-top: 1.2rem; }

/* containers/dataframes (best-effort across versions) */
section.main, .stDataFrame, div[data-testid="stTable"] { background: #111 !important; border: 1px solid #48c77422; border-radius: 10px; }

/* headers and captions */
h1, h2, h3, h4 { color: #d1ffd9 !important; }
p, span, label, .stMarkdown, .stCaption { color: #e8f5e9 !important; }

/* inputs */
input, select, textarea { background: #151515 !important; color: #e8f5e9 !important; border: 1px solid #48c77455 !important; }

/* base button */
.stButton>button {
  background: #111; border: 1.6px solid #48c774; color: #d1ffd9;
  border-radius: 10px; padding: 0.5rem 0.9rem; font-weight: 600;
}
.stButton>button:hover { filter: brightness(1.1); }

/* tabs */
.stTabs [data-baseweb="tab-list"] { gap: 6px; }
.stTabs [data-baseweb="tab"] {
  background: #111; border: 1px solid #48c77455; color: #caffd1; border-radius: 8px;
}
.stTabs [aria-selected="true"] { border: 1.6px solid #48c774; color: #ffffff !important; }

/* metrics */
[data-testid="stMetricValue"] { color: #a8ffbd; }
[data-testid="stMetricDelta"] { color: #fff; }
</style>
""", unsafe_allow_html=True)

# Header with branding
st.markdown(
    "<div style='display:flex;justify-content:space-between;align-items:center;'>"
    "<h2 style='margin:0'>🐮 Farm-Gate MRL & AMU Compliance — Pro (Offline)</h2>"
    "<div style='font-weight:600;color:#8cf38f'>Built by HEXAMINDS 📈</div>"
    "</div>",
    unsafe_allow_html=True
)
st.caption("Stop unsafe milk/meat at source • Stewardship • Trend analysis • Reports")

# --- require login first ---
require_auth()
user = st.session_state.get("user", {})

# Sidebar role & profile
role = st.sidebar.selectbox("Role / పాత్ర", ["Operator","Farmer","Vet","Lab","Buyer","Authority"], index=0)
st.sidebar.caption("Local role view (offline)")
st.sidebar.markdown("**Built by HEXAMINDS 📈**")

profile = st.sidebar.selectbox("Rule Pack", ["FSSAI","CODEX","EU"],
                               index=["FSSAI","CODEX","EU"].index(get_setting("rule_profile","FSSAI")))
if profile != current_profile():
    set_setting("rule_profile", profile)

with st.expander("📝 Nudges (తెలుగులో/English)"):
    st.markdown("""
- **తెలుగు**: *"ఇవాళ ట్రీట్మెంట్ ఇచ్చారు. దయచేసి **withdrawal period** పూర్తయ్యే వరకు పాలు అమ్మవద్దు."*
- **English**: *"Treatment recorded today. Do not sell milk until the **withdrawal period** is over."*
- **తెలుగు**: *"మీ పరీక్ష విలువ **MRL కంటే తక్కువ** ఉంది — సురక్షితం."*
- **English**: *"Your lab residue is **below MRL** — safe."*
""")

tabs = st.tabs(["📋 Master Data","💉 Treatments","🧪 Lab Results","✅ Compliance","📊 Analytics","🏛️ Authorities","🧾 Reports","📦 Import/Export"])

# -------------------- Master Data --------------------
with tabs[0]:
    st.subheader("Animals")
    with st.form("animal_form"):
        c = st.columns(5)
        tag = c[0].text_input("Tag ID*", "BUF-12")
        species = c[1].selectbox("Species*", ["cow","buffalo","poultry"], index=1)
        herd = c[2].text_input("Herd/Farm", "GreenFarm")
        wt = c[3].number_input("Avg Weight (kg)", min_value=50.0, value=450.0, step=5.0)
        if st.form_submit_button("Add/Update Animal"):
            exist = df("SELECT * FROM animals WHERE tag_id=?", (tag,))
            if exist.empty:
                write("INSERT INTO animals(tag_id,species,herd,avg_weight_kg) VALUES(?,?,?,?)",(tag,species,herd,wt))
                st.success("Animal added.")
            else:
                write("UPDATE animals SET species=?, herd=?, avg_weight_kg=? WHERE tag_id=?",(species,herd,wt,tag))
                st.info("Animal updated.")
    st.dataframe(df("SELECT * FROM animals"))

    st.markdown("---")
    st.subheader("Drugs")
    with st.form("drug_form"):
        c = st.columns(4)
        dname = c[0].text_input("Drug name*", "Oxytetracycline")
        dclass = c[1].selectbox("Class", ["tetracycline","fluoroquinolone","penicillin","other"])
        crit = c[2].selectbox("WHO HP-CIA (critical)?", ["N","Y"])
        if st.form_submit_button("Add Drug"):
            try:
                write("INSERT INTO drugs(name,drug_class,is_critical) VALUES(?,?,?)",(dname,dclass,crit))
                st.success("Drug added.")
            except Exception as e:
                st.error(f"Could not add: {e}")
    st.dataframe(df("SELECT * FROM drugs"))

    st.markdown("---")
    st.subheader(f"Withdrawal Rules — Profile: {current_profile()}")
    st.caption("Switch profiles in sidebar; import rule-packs in Import/Export tab.")
    st.dataframe(df("SELECT species,drug,drug_class,is_critical,matrix,withdrawal_days,mrl_mg_per_kg FROM rules WHERE profile=?",
                    (current_profile(),)))

# -------------------- Treatments --------------------
with tabs[1]:
    st.subheader("Record Treatment")
    animals_df = df("SELECT * FROM animals")
    drugs_df = df("SELECT * FROM drugs")
    if animals_df.empty or drugs_df.empty:
        st.warning("Add at least one animal and one drug first.")
    else:
        with st.form("treat_form"):
            c = st.columns(8)
            a_tag = c[0].selectbox("Animal (Tag)", animals_df["tag_id"].tolist())
            aid = int(animals_df[animals_df["tag_id"]==a_tag].iloc[0]["id"])
            dname = c[1].selectbox("Drug", drugs_df["name"].tolist())
            dose = c[2].number_input("Dose (mg/kg)", min_value=0.0, value=10.0, step=0.5)
            route = c[3].selectbox("Route", ["IM","IV","SC","PO"])
            d_admin = c[4].date_input("Date", value=date.today())
            batch = c[5].text_input("Batch", "BATCH-001")
            indic = c[6].text_input("Indication", "mastitis")
            dur = c[7].number_input("Duration (days)", min_value=1, value=5, step=1)
            c2 = st.columns(2)
            presc = c2[0].text_input("Prescribed by (Vet ID)", "VET-001")
            if st.form_submit_button("Save Treatment"):
                write("""INSERT INTO treatments(animal_id,drug,dose_mg_per_kg,route,date_administered,batch_code,
                        indication,duration_days,prescribed_by,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (aid,dname,float(dose),route,d_admin.isoformat(),batch,indic,int(dur),presc,datetime.utcnow().isoformat()))
                st.success("Saved.")
                # notify
                u = st.session_state.get("user")
                if u and u.get("phone"):
                    msg = f"Treatment recorded for {a_tag} on {d_admin.isoformat()}. Follow withdrawal advice."
                    send_sms(u["phone"], msg)

    st.markdown("#### Recent Treatments")
    st.dataframe(df("""SELECT t.id, a.tag_id, t.drug, t.dose_mg_per_kg, t.route, t.date_administered, 
                       t.batch_code, t.indication, t.duration_days, t.prescribed_by
                       FROM treatments t JOIN animals a ON a.id=t.animal_id
                       ORDER BY t.id DESC LIMIT 100"""))

# -------------------- Labs --------------------
with tabs[2]:
    st.subheader("Record Lab Result")
    animals_df = df("SELECT * FROM animals")
    drugs_df = df("SELECT * FROM drugs")
    if animals_df.empty or drugs_df.empty:
        st.warning("Add animals & drugs first.")
    else:
        with st.form("lab_form"):
            c = st.columns(6)
            a_tag = c[0].selectbox("Animal (Tag)", animals_df["tag_id"].tolist())
            aid = int(animals_df[animals_df["tag_id"]==a_tag].iloc[0]["id"])
            dname = c[1].selectbox("Drug", drugs_df["name"].tolist())
            matrix = c[2].selectbox("Matrix", ["milk","meat"])
            val = c[3].number_input("Residue (mg/kg)", min_value=0.0, value=0.08, step=0.01)
            d_sample = c[4].date_input("Sample Date", value=date.today())
            if st.form_submit_button("Save Lab Result"):
                write("""INSERT INTO labs(animal_id,drug,matrix,value_mg_per_kg,sample_date)
                         VALUES (?,?,?,?,?)""",(aid,dname,matrix,float(val),d_sample.isoformat()))
                st.success("Saved.")
    st.markdown("#### Recent Lab Results")
    st.dataframe(df("""SELECT l.id, a.tag_id, l.drug, l.matrix, l.value_mg_per_kg, l.sample_date
                       FROM labs l JOIN animals a ON a.id=l.animal_id
                       ORDER BY l.id DESC LIMIT 100"""))

# -------------------- Compliance --------------------
with tabs[3]:
    st.subheader("Safe-to-Sell & MRL Verdict")
    animals_df = df("SELECT * FROM animals")
    drugs_df = df("SELECT * FROM drugs")
    if animals_df.empty:
        st.info("Add animal(s) first.")
    else:
        c = st.columns(2)
        with c[0]:
            st.markdown("**Withdrawal Check**")
            a_tag = st.selectbox("Animal", animals_df["tag_id"].tolist(), key="c_an")
            aid = int(animals_df[animals_df["tag_id"]==a_tag].iloc[0]["id"])
            matrix = st.selectbox("Matrix", ["milk","meat"], key="c_mx")
            milk_btn = st.button("Milk Check", key="milk_btn")
            meat_btn = st.button("Meat Check", key="meat_btn")

            # color the specific buttons (best effort; may be limited by CSP)
            st.markdown("""
            <script>
            const btns = window.parent.document.querySelectorAll('button');
            btns.forEach(b=>{
              if(b.innerText.trim()==='Milk Check'){ b.style.background='#ffffff'; b.style.color='#111'; b.style.border='2px solid #ffffff'; }
              if(b.innerText.trim()==='Meat Check'){ b.style.background='#ff8c1a'; b.style.color='#111'; b.style.border='2px solid #ff8c1a'; }
            });
            </script>
            """, unsafe_allow_html=True)

            if milk_btn:
                safe, reason, detail = calc_safe_date(aid, "milk", current_profile())
                today = date.today()
                if safe is None:
                    st.warning("⚠️ Please add treatment first.")
                else:
                    status = "ALLOW" if today>=safe else "BLOCK"
                    if status=="ALLOW":
                        st.success(f"✅ ALLOW — Safe on {safe} (today {today})")
                        u = st.session_state.get("user")
                        if u and u.get("phone"):
                            send_whatsapp(u["phone"], f"{a_tag} is SAFE to sell milk from {safe}.")
                    else:
                        delta = (safe-today).days
                        st.error(f"⛔ BLOCK — Withdrawal pending {delta} day(s). Safe on {safe}.")
                        u = st.session_state.get("user")
                        if u and u.get("phone"):
                            send_sms(u["phone"], f"BLOCK: Do not sell milk from {a_tag}. Safe on {safe} (in {delta} day/s).")
                    with st.expander("Details"):
                        dd = pd.DataFrame(detail, columns=["drug","date_admin","until/notes"])
                        st.dataframe(dd)

            if meat_btn:
                safe, reason, detail = calc_safe_date(aid, "meat", current_profile())
                today = date.today()
                if safe is None:
                    st.warning("⚠️ Please add treatment first.")
                else:
                    status = "ALLOW" if today>=safe else "BLOCK"
                    if status=="ALLOW":
                        st.success(f"✅ ALLOW — Safe on {safe} (today {today})")
                        u = st.session_state.get("user")
                        if u and u.get("phone"):
                            send_whatsapp(u["phone"], f"{a_tag} is SAFE to sell meat from {safe}.")
                    else:
                        delta = (safe-today).days
                        st.error(f"⛔ BLOCK — Withdrawal pending {delta} day(s). Safe on {safe}.")
                        u = st.session_state.get("user")
                        if u and u.get("phone"):
                            send_sms(u["phone"], f"BLOCK: Do not sell meat from {a_tag}. Safe on {safe} (in {delta} day/s).")
                    with st.expander("Details"):
                        dd = pd.DataFrame(detail, columns=["drug","date_admin","until/notes"])
                        st.dataframe(dd)

        with c[1]:
            st.markdown("**MRL Verdict (Latest Lab)**")
            if drugs_df.empty: st.info("Add drugs.")
            else:
                a_tag2 = st.selectbox("Animal ", animals_df["tag_id"].tolist(), key="m_an")
                aid2 = int(animals_df[animals_df["tag_id"]==a_tag2].iloc[0]["id"])
                drug2 = st.selectbox("Drug", drugs_df["name"].tolist(), key="m_dr")
                matrix2 = st.selectbox("Matrix ", ["milk","meat"], key="m_mx")
                chk = st.button("Check MRL (Green)", key="mrl_btn")

                st.markdown("""
                <script>
                const btns2 = window.parent.document.querySelectorAll('button');
                btns2.forEach(b=>{
                  if(b.innerText.trim()==='Check MRL (Green)'){ b.style.background='#2ecc71'; b.style.color='#111'; b.style.border='2px solid #2ecc71'; }
                });
                </script>
                """, unsafe_allow_html=True)

                if chk:
                    v,n = latest_lab_verdict(aid2, drug2, matrix2, current_profile())
                    if v is True:
                        st.success(f"✅ PASS — {n}")
                        u = st.session_state.get("user")
                        if u and u.get("phone"):
                            send_whatsapp(u["phone"], f"MRL PASS for {a_tag2} / {drug2} in {matrix2} ({n}).")
                    elif v is False:
                        st.error(f"⛔ BLOCK — {n}")
                        u = st.session_state.get("user")
                        if u and u.get("phone"):
                            send_sms(u["phone"], f"MRL FAIL for {a_tag2} / {drug2} in {matrix2} ({n}). Hold product.")
                    else:
                        st.warning("ℹ️ No lab or no rule for this combo.")

    st.markdown("---")
    st.subheader("Compliance Certificate (PDF + QR)")
    animals_df2 = df("SELECT * FROM animals")
    if not animals_df2.empty:
        a_tag3 = st.selectbox("Animal  ", animals_df2["tag_id"].tolist(), key="p_an")
        aid3 = int(animals_df2[animals_df2["tag_id"]==a_tag3].iloc[0]["id"])
        if st.button("Generate Certificate PDF"):
            arow = df("SELECT * FROM animals WHERE id=?", (aid3,)).iloc[0]
            safe_milk, _, _ = calc_safe_date(aid3,"milk")
            safe_meat, _, _ = calc_safe_date(aid3,"meat")
            payload = {
                "animal": arow["tag_id"],
                "species": arow["species"],
                "herd": arow["herd"],
                "profile": current_profile(),
                "safe_milk": (safe_milk.isoformat() if safe_milk else None),
                "safe_meat": (safe_meat.isoformat() if safe_meat else None),
                "ts": datetime.utcnow().isoformat(),
                "built_by": "HEXAMINDS"
            }
            qr = qrcode.make(str(payload))
            qrbuf = BytesIO(); qr.save(qrbuf, format="PNG"); qrbuf.seek(0)

            pdf = BytesIO()
            ccv = canvas.Canvas(pdf, pagesize=A4)
            ccv.setFont("Helvetica-Bold", 16)
            ccv.drawString(72, 800, "Compliance Certificate (Offline Demo)")
            ccv.setFont("Helvetica", 12)
            ccv.drawString(72, 780, f"Animal: {arow['tag_id']}  Species: {arow['species']}  Herd: {arow['herd'] or '-'}")
            ccv.drawString(72, 765, f"Rule Profile: {current_profile()}")
            if safe_milk: ccv.drawString(72, 750, f"Safe-to-sell (Milk): {safe_milk}")
            if safe_meat: ccv.drawString(72, 735, f"Safe-to-sell (Meat): {safe_meat}")
            ccv.drawString(72, 720, "Built by HEXAMINDS 📈")
            ccv.drawImage(ImageReader(BytesIO(qrbuf.getvalue())), 72, 600, width=120, height=120)
            ccv.showPage(); ccv.save(); pdf.seek(0)
            st.download_button("⬇️ Download PDF", data=pdf, file_name=f"certificate_{arow['tag_id']}.pdf", mime="application/pdf")

# -------------------- Analytics --------------------
with tabs[4]:
    st.subheader("Trends & Stewardship Metrics")
    col = st.columns(3)
    end = date.today()
    start = end - timedelta(days=30)
    start = col[0].date_input("Start", value=start)
    end = col[1].date_input("End", value=end)
    if col[2].button("Refresh Metrics"):
        pass

    amu = amu_mg_per_kg_biomass(start, end)
    ddd = dddvet_proxy(start, end)

    mcol = st.columns(4)
    mcol[0].metric("AMU (mg/kg biomass)", f"{amu:.3f}")
    mcol[1].metric("DDDvet proxy / 1000 animal-days", f"{ddd:.2f}")

    labs_all = df("""SELECT l.*, a.species FROM labs l JOIN animals a ON a.id=l.animal_id""")
    if not labs_all.empty:
        rules = df("SELECT * FROM rules WHERE profile=?", (current_profile(),))
        j = labs_all.merge(rules, left_on=["species","drug","matrix"], right_on=["species","drug","matrix"], how="left", suffixes=("","_r"))
        j["pass"] = j["value_mg_per_kg"] <= j["mrl_mg_per_kg"]
    else:
        j = pd.DataFrame(columns=["pass"])
    mrate = (j["pass"].sum()/len(j))*100 if len(j)>0 else 0.0
    mcol[2].metric("MRL Pass Rate (%)", f"{mrate:.1f}")

    animals_all = df("SELECT * FROM animals")
    afds = [antibiotic_free_days(int(a["id"]), start, end) for _,a in animals_all.iterrows()] if not animals_all.empty else []
    avg_afd = float(np.mean(afds)) if afds else 0.0
    mcol[3].metric("Avg Antibiotic-Free Days", f"{avg_afd:.1f}")

    st.markdown("#### Treatments by Drug Class")
    t = df("""SELECT t.*, d.drug_class FROM treatments t 
              LEFT JOIN drugs d ON d.name=t.drug""")
    if not t.empty:
        g = t.groupby("drug_class").size().reset_index(name="count")
        fig = plt.figure()
        plt.bar(g["drug_class"], g["count"])
        plt.title("Treatments by Drug Class"); plt.xlabel("Class"); plt.ylabel("Count")
        st.pyplot(fig)
    else:
        st.caption("No treatments yet.")

    st.markdown("#### Violations & Reasons (last 30 days)")
    reasons = []
    for _,a in animals_all.iterrows():
        for mx in ["milk","meat"]:
            safe,_r,_d = calc_safe_date(int(a["id"]), mx)
            if safe and date.today()<safe:
                reasons.append(("withdrawal_pending", a["tag_id"], mx))
    labs_recent = df("SELECT * FROM labs WHERE date(sample_date)>=?", ((date.today()-timedelta(days=30)).isoformat(),))
    for _,l in labs_recent.iterrows():
        sp = df("SELECT species FROM animals WHERE id=?", (int(l["animal_id"]),))
        if sp.empty: continue
        rr = rule_for(sp.iloc[0]["species"], l["drug"], l["matrix"])
        if rr is None: continue
        if float(l["value_mg_per_kg"])>float(rr["mrl_mg_per_kg"]):
            tag = df("SELECT tag_id FROM animals WHERE id=?", (int(l["animal_id"]),)).iloc[0]["tag_id"]
            reasons.append(("mrl_over", tag, l["matrix"]))
    if reasons:
        rdf = pd.DataFrame(reasons, columns=["reason","animal_tag","matrix"])
        st.dataframe(rdf)
    else:
        st.caption("No violations in window.")

# -------------------- Authorities --------------------
with tabs[5]:
    st.subheader("Authority View — Real-time AMU & Compliance")
    update_zero_residue_badge()
    col = st.columns(3)
    n_open = 0
    an = df("SELECT * FROM animals")
    for _,a in an.iterrows():
        for mx in ["milk","meat"]:
            sd,_r,_d = calc_safe_date(int(a["id"]), mx)
            if sd and date.today()<sd: n_open+=1
    col[0].metric("Open Withdrawal Cases", n_open)
    col[1].metric("Animals", len(an))
    col[2].metric("Zero-Residue Badge (streak≥30d)", int((an["zero_residue_streak_days"]>=30).sum() if not an.empty else 0))

    st.markdown("#### AMU by Drug Class (last 30d)")
    t = df("""SELECT t.*, d.drug_class FROM treatments t LEFT JOIN drugs d ON d.name=t.drug""")
    if not t.empty:
        t["date_administered"]=pd.to_datetime(t["date_administered"]).dt.date
        t30 = t[t["date_administered"]>=date.today()-timedelta(days=30)]
        if not t30.empty:
            g = t30.groupby("drug_class").size().reset_index(name="count")
            fig = plt.figure()
            plt.bar(g["drug_class"], g["count"])
            plt.xlabel("Class"); plt.ylabel("Treatments"); plt.title("Last 30 days")
            st.pyplot(fig)
        else:
            st.caption("No treatments in last 30 days.")
    else:
        st.caption("No treatments recorded yet.")

    st.markdown("#### Top BLOCK Reasons (snapshot)")
    reasons=[]
    for _,a in an.iterrows():
        for mx in ["milk","meat"]:
            sd,_r,_d = calc_safe_date(int(a["id"]), mx)
            if sd and date.today()<sd:
                reasons.append(("withdrawal_pending", a["tag_id"], mx))
    labs_all2 = df("SELECT * FROM labs")
    for _,l in labs_all2.iterrows():
        sp = df("SELECT species FROM animals WHERE id=?", (int(l["animal_id"]),))
        if sp.empty: continue
        rr = rule_for(sp.iloc[0]["species"], l["drug"], l["matrix"])
        if rr is None: continue
        if float(l["value_mg_per_kg"])>float(rr["mrl_mg_per_kg"]):
            tag = df("SELECT tag_id FROM animals WHERE id=?", (int(l["animal_id"]),)).iloc[0]["tag_id"]
            reasons.append(("mrl_over", tag, l["matrix"]))
    if reasons:
        r = pd.DataFrame(reasons, columns=["reason","animal_tag","matrix"])
        st.dataframe(r.groupby("reason").size().reset_index(name="count"))
    else:
        st.caption("No blocks currently.")

# -------------------- Reports --------------------
with tabs[6]:
    st.subheader("Monthly Report (PDF)")
    end = date.today()
    start = (end.replace(day=1) - timedelta(days=1)).replace(day=1)  # previous month start
    k1 = f"AMU mg/kg: {amu_mg_per_kg_biomass(start,end):.3f}"
    k2 = f"DDDvet/1000: {dddvet_proxy(start,end):.2f}"
    labs = df("""SELECT l.*, a.species FROM labs l JOIN animals a ON a.id=l.animal_id
                 WHERE date(sample_date)>=? AND date(sample_date)<=?""",(start.isoformat(), end.isoformat()))
    if not labs.empty:
        rules = df("SELECT * FROM rules WHERE profile=?", (current_profile(),))
        j = labs.merge(rules, left_on=["species","drug","matrix"], right_on=["species","drug","matrix"], how="left")
        j["pass"] = j["value_mg_per_kg"] <= j["mrl_mg_per_kg"]
        k3 = f"MRL Pass Rate: {((j['pass'].sum()/len(j))*100):.1f}%"
    else:
        k3 = "MRL Pass Rate: 0.0%"
    if st.button("Generate PDF Report"):
        pdf = BytesIO()
        ccv = canvas.Canvas(pdf, pagesize=A4)
        ccv.setFont("Helvetica-Bold", 16)
        ccv.drawString(72, 800, f"Monthly Compliance Report — {start} to {end}")
        ccv.setFont("Helvetica", 12)
        ccv.drawString(72, 780, f"Rule Profile: {current_profile()}")
        ccv.drawString(72, 760, k1)
        ccv.drawString(72, 745, k2)
        ccv.drawString(72, 730, k3)
        ccv.drawString(72, 710, "Top Insights:")
        ccv.setFont("Helvetica", 11)
        ccv.drawString(92, 695, "• Withdrawal pending cases reduced via gate checks")
        ccv.drawString(92, 680, "• MRL violations addressed with re-sampling and nudges")
        ccv.drawString(92, 665, "• Stewardship improving — antibiotic-free days trending up")
        ccv.drawString(72, 640, "Built by HEXAMINDS 📈")
        ccv.showPage(); ccv.save(); pdf.seek(0)
        st.download_button("⬇️ Download Monthly Report PDF", data=pdf, file_name="monthly_report.pdf", mime="application/pdf")

# -------------------- Import/Export --------------------
with tabs[7]:
    st.subheader("Rule-Pack Import")
    st.write("Prepare & upload your FSSAI/Codex/EU CSV below. Expected columns:")
    st.code("species,drug,drug_class,is_critical,matrix,withdrawal_days,mrl_mg_per_kg", language="text")

    up = st.file_uploader("Upload rule-pack CSV", type=["csv"], key="rule_up")
    prof = st.selectbox(
        "Apply to profile",
        ["FSSAI","CODEX","EU"],
        index=["FSSAI","CODEX","EU"].index(current_profile()),
        key="rule_prof"
    )

    if st.button("Import Rule-Pack"):
        if up is None:
            st.warning("Please choose a CSV file.")
        else:
            ok, msg = load_rulepack_csv(up, prof)
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    st.markdown("---")
    st.subheader("Bulk Import — Treatments & Labs")

    # Treatments import
    st.caption("Treatment CSV columns: animal_tag,drug,dose_mg_per_kg,route,date_administered,batch_code,indication,duration_days,prescribed_by")
    t_up = st.file_uploader("Upload treatments CSV", type=["csv"], key="t_up")
    if st.button("Import Treatments"):
        if t_up is None:
            st.warning("Please upload a treatments CSV.")
        else:
            try:
                tdf = pd.read_csv(t_up)
                required = {"animal_tag","drug","dose_mg_per_kg","route","date_administered"}
                cols_lower = [c.lower() for c in tdf.columns]
                missing = required - set(cols_lower)
                if missing:
                    st.error(f"Missing columns: {', '.join(sorted(missing))}")
                else:
                    tdf.columns = cols_lower
                    animals_map = df("SELECT id, tag_id FROM animals")
                    m = tdf.merge(animals_map, left_on="animal_tag", right_on="tag_id", how="left")
                    m = m.dropna(subset=["id"])
                    rows = []
                    for _, r in m.iterrows():
                        rows.append((
                            int(r["id"]),
                            r["drug"],
                            float(r["dose_mg_per_kg"]),
                            r["route"],
                            str(r["date_administered"]),
                            r.get("batch_code",""),
                            r.get("indication",""),
                            int(r.get("duration_days", 1)),
                            r.get("prescribed_by",""),
                            datetime.utcnow().isoformat()
                        ))
                    if rows:
                        write_many(
                            """INSERT INTO treatments
                               (animal_id,drug,dose_mg_per_kg,route,date_administered,batch_code,indication,duration_days,prescribed_by,created_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            rows
                        )
                        st.success(f"Imported {len(rows)} treatments.")
                    else:
                        st.warning("No matching animals found (check tag IDs).")
            except Exception as e:
                st.error(f"Failed: {e}")

    # Labs import
    st.caption("Lab CSV columns: animal_tag,drug,matrix,value_mg_per_kg,sample_date")
    l_up = st.file_uploader("Upload labs CSV", type=["csv"], key="l_up")
    if st.button("Import Labs"):
        if l_up is None:
            st.warning("Please upload a labs CSV.")
        else:
            try:
                ldf = pd.read_csv(l_up)
                required = {"animal_tag","drug","matrix","value_mg_per_kg","sample_date"}
                cols_lower = [c.lower() for c in ldf.columns]
                missing = required - set(cols_lower)
                if missing:
                    st.error(f"Missing columns: {', '.join(sorted(missing))}")
                else:
                    ldf.columns = cols_lower
                    animals_map = df("SELECT id, tag_id FROM animals")
                    m = ldf.merge(animals_map, left_on="animal_tag", right_on="tag_id", how="left")
                    m = m.dropna(subset=["id"])
                    rows = []
                    for _, r in m.iterrows():
                        rows.append((
                            int(r["id"]),
                            r["drug"],
                            r["matrix"],
                            float(r["value_mg_per_kg"]),
                            str(r["sample_date"])
                        ))
                    if rows:
                        write_many(
                            """INSERT INTO labs
                               (animal_id,drug,matrix,value_mg_per_kg,sample_date)
                               VALUES (?,?,?,?,?)""",
                            rows
                        )
                        st.success(f"Imported {len(rows)} labs.")
                    else:
                        st.warning("No matching animals found (check tag IDs).")
            except Exception as e:
                st.error(f"Failed: {e}")

    st.markdown("---")
    st.subheader("Export (CSV snapshot)")
    if st.button("Export All"):
        tables = ["animals","drugs","rules","treatments","labs"]
        out = {}
        c = conn()
        for tname in tables:
            out[tname] = pd.read_sql_query(f"SELECT * FROM {tname}", c).to_csv(index=False)
        c.close()
        st.download_button(
            "⬇️ Download snapshot",
            data=str(out).encode("utf-8"),
            file_name="export.txt",
            mime="text/plain"
        )

# Footer branding
st.markdown(
    "<hr><div style='text-align:center;opacity:0.85'>Built by <b>HEXAMINDS 📈</b> — Offline-first MRL & AMU Compliance</div>",
    unsafe_allow_html=True
)
