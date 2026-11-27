"""
SOAR Study - Appointment Keeping & Tracing Dashboard
====================================================
Streamlit dashboard for the SOAR REDCap project (password protected).

Page 1 - Summary:  appointment keeping, upcoming visits, tracing, booking.
Page 2 - Register: participant-level longitudinal register
                   (1 Booking | 2 Tracing register | 3 Missed appointments).

Setup
-----
    pip install -r requirements.txt
    streamlit run app.py

Secrets (.streamlit/secrets.toml, or environment variables of the same name):
    REDCAP_API_URL     = "https://your-server.redcap/api/"
    REDCAP_API_TOKEN   = "..."
    DASHBOARD_PASSWORD = "..."
    # optional: REDCAP_CA_BUNDLE = "path/to/bundle.pem"
"""

import hmac
import os
import re
import time

import pandas as pd
import plotly.express as px
import streamlit as st
from redcap import Project

st.set_page_config(
    page_title="SOAR Appointments Dashboard",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ───────────────────────────────────────────────────────────────
# STYLE (same look as the SOAR enrollment tracker)
# ───────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
    @import url('https://fonts.googleapis.com/css2?family=EB+Garamond:wght@400;500;600;700;800&display=swap');
    .stApp, .stApp div, .stApp p, .stApp label, .stApp li, .stApp a, .stApp small,
    .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
    .stApp button, .stApp input, .stApp textarea, .stApp select,
    .stApp table, .stApp th, .stApp td,
    .stApp span:not([data-testid="stIconMaterial"]) {
        font-family: 'EB Garamond', Garamond, 'Cormorant Garamond', 'Times New Roman', serif !important;
    }
    [data-testid="stIconMaterial"] { font-family: "Material Symbols Rounded", "Material Icons" !important; }
    .main-header { font-size: 2.2rem; font-weight: 800; color: #4a90d9; letter-spacing: 0.01em; }
    .sub-header { font-size: 1.1rem; font-weight: 500; opacity: 0.85; margin-bottom: 1rem; }
    .stDeployButton, [data-testid="stToolbarActionButton"], #MainMenu { display: none !important; }
    footer { visibility: hidden !important; height: 0 !important; }
</style>
""",
    unsafe_allow_html=True,
)

PLOT_FONT = "EB Garamond, Garamond, Cormorant Garamond, Times New Roman, serif"
STATUS_COLORS = {
    "Kept": "#27ae60",
    "Missed": "#c0392b",
    "Due now": "#e67e22",
    "Upcoming": "#2980b9",
    "Early termination": "#8e44ad",
    "Closed (withdrawn/died)": "#6c757d",
}

# ───────────────────────────────────────────────────────────────
# PASSWORD PROTECTION
# ───────────────────────────────────────────────────────────────
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 60


def _secret(name, default=""):
    value = os.getenv(name, "")
    if value:
        return value
    try:
        return str(st.secrets.get(name, default))
    except Exception:
        return default


def require_login():
    """Block the app until the correct password is entered (fails closed)."""
    expected = _secret("DASHBOARD_PASSWORD")
    if not expected:
        st.error(
            "No dashboard password is configured. Set DASHBOARD_PASSWORD in "
            "`.streamlit/secrets.toml` or as an environment variable."
        )
        st.stop()

    if st.session_state.get("auth_ok"):
        return

    locked_until = st.session_state.get("locked_until", 0)
    st.markdown('<div class="main-header">SOAR Appointments Dashboard</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-header">Authorised study staff only</div>', unsafe_allow_html=True)

    if time.time() < locked_until:
        st.error(f"Too many failed attempts. Try again in {int(locked_until - time.time())} seconds.")
        st.stop()

    with st.form("login_form"):
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        if hmac.compare_digest(pw.encode("utf-8"), expected.encode("utf-8")):
            st.session_state["auth_ok"] = True
            st.session_state["failed"] = 0
            st.rerun()
        else:
            st.session_state["failed"] = st.session_state.get("failed", 0) + 1
            if st.session_state["failed"] >= MAX_ATTEMPTS:
                st.session_state["locked_until"] = time.time() + LOCKOUT_SECONDS
                st.session_state["failed"] = 0
            time.sleep(1)
            st.error("Incorrect password.")
    st.stop()


# ───────────────────────────────────────────────────────────────
# CODE BOOKS (from the SOAR data dictionary)
# ───────────────────────────────────────────────────────────────
WINDOW_DAYS = 3  # +/- days around the target date (appt_log: window opens/closes)

STUDY_FOLLOWUPS = {  # appt code -> (label, days from Day 0)
    "1": ("Week 1 visit", 7),
    "2": ("Week 2 visit", 14),
    "3": ("Week 3 visit", 21),
    "4": ("Week 12 visit", 84),
    "5": ("Week 36 visit", 252),
}
PSF_SESSIONS = {
    "11": ("PSF Session 1 (Day 0)", 0),
    "12": ("PSF Session 2 (Day 7)", 7),
    "13": ("PSF Session 3 (Day 14)", 14),
    "14": ("PSF Session 4 (Day 21)", 21),
    "15": ("PSF Session 5 (Week 4)", 28),
    "16": ("PSF Session 6 (Week 5)", 35),
    "17": ("PSF Session 7 (Week 7)", 49),
    "18": ("PSF Session 8 (Week 8)", 56),
}
APPT_LABELS = {
    "0": "Day 0 visit",
    **{k: v[0] for k, v in STUDY_FOLLOWUPS.items()},
    **{k: v[0] for k, v in PSF_SESSIONS.items()},
    "20": "Unscheduled",
}
APPT_ORDER = list(APPT_LABELS.values())
OFFSETS = {k: v[1] for k, v in {**STUDY_FOLLOWUPS, **PSF_SESSIONS}.items()}

ARM_MAP = {"1": "PSF", "2": "BA"}
REM_OUTCOME = {"1": "Reached and confirmed attendance", "2": "Not reached", "3": "Reached and rescheduled"}
CONTACTED = {
    "1": "Participant", "2": "Contact person 1", "3": "Contact person 2",
    "4": "Person living with participant", "5": "Other",
}
TA_TYPE = {
    "1": "Phone call", "2": "SMS", "3": "Next of kin contact", "4": "Physical tracing (home visit)",
    "5": "Contact with treatment provider or facility", "6": "Other",
}
TA_PHONE = {"1": "Phone 1", "2": "Phone 2", "3": "Work phone", "4": "Next of kin phone", "5": "Other"}
TA_OUTCOME = {
    "1": "Participant reached", "2": "No answer", "3": "Phone switched off",
    "4": "Wrong or unavailable number", "5": "Someone else answered; participant not available",
    "6": "Next of kin reached; participant not located", "7": "Participant found (physical tracing)",
    "8": "Participant not found (physical tracing)", "9": "Participant has moved",
    "10": "Participant declined to continue", "11": "Participant reported deceased", "12": "Other",
}
TA_REACHED = {"1", "7"}
TA_REASON = {
    "1": "Illness", "2": "Travel or away from home", "3": "Transport or cost",
    "4": "Work or family commitments", "5": "Forgot", "6": "Relocated", "7": "Hospitalised",
    "8": "Does not wish to continue", "9": "Unknown", "10": "Other",
}
TA_NEXT = {
    "1": "Repeat call", "2": "Contact next of kin", "3": "Plan physical tracing",
    "4": "Contact treatment provider", "5": "Escalate for lost-to-follow-up review",
    "6": "None: participant back on schedule",
}
TC_OUTCOME = {
    "1": "Returned and appointment completed", "2": "Withdrawn: participant-initiated",
    "3": "Withdrawn: investigator-initiated", "4": "Lost to follow-up", "5": "Died",
    "6": "Relocated but still attending study visits",
}
TC_EXIT = {"2", "3", "5"}  # withdrawn / died: no further appointments expected
VISIT_STATUS = {"1": "Completed", "2": "Missed", "3": "Rescheduled", "4": "Early Termination"}

FIELDS = (
    ["study_id", "medication_date", "bt_intervention_type", "bt_day0_date",
     "visit_window", "visit_status", "visit_date"]
    + [f"bt_psf_s{i}_{x}" for i in range(1, 9) for x in ("actual_date", "status")]
    + ["ap_appt", "ap_psf_appt", "ap_target_date", "ap_window_open", "ap_window_close",
       "ap_booked_date", "ap_rem_call_date", "ap_rem_outcome", "ap_resched_new_date",
       "ap_contacted", "ap_contacted_oth", "ap_notes", "ap_staff"]
    + ["ta_visit", "ta_date", "ta_time", "ta_type", "ta_phone_used", "ta_by", "ta_cadre",
       "ta_outcome", "ta_reason", "ta_new_locator", "ta_returned", "ta_return_date",
       "ta_next_step", "ta_notes"]
    + ["tc_visit", "tc_closed_date", "tc_outcome", "tc_withdraw_date", "tc_death_date",
       "tc_pi_informed", "tc_status_form", "tc_by"]
)


# ───────────────────────────────────────────────────────────────
# REDCAP CONNECTION & EXPORT
# ───────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False, ttl=900)
def connect_redcap():
    api_url, token = _secret("REDCAP_API_URL"), _secret("REDCAP_API_TOKEN")
    if not api_url or not token:
        return None
    ca_bundle = _secret("REDCAP_CA_BUNDLE")
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "redcap_ca_bundle.pem")
    if not ca_bundle and os.path.isfile(local):
        ca_bundle = local
    try:
        return Project(api_url, token, verify_ssl=ca_bundle) if ca_bundle else Project(api_url, token)
    except Exception as e:
        st.error(f"Failed to connect to REDCap: {e}")
        return None


def _invalid_fields(err):
    msg = str(err)
    if "not valid" not in msg:
        return []
    return re.findall(r"'([A-Za-z0-9_]+)'", msg.split("not valid:", 1)[-1])


@st.cache_data(ttl=900, show_spinner="Pulling data from REDCap...")
def load_data(_proj):
    """Export every record (all events and repeating instances) as raw codes.

    Fields that do not exist in the project are dropped and the call retried,
    so the dashboard still loads while the data dictionary is being updated.
    """
    rid = getattr(_proj, "def_field", "record_id")
    fields = [rid] + FIELDS
    for _ in range(5):
        try:
            recs = _proj.export_records(fields=fields, raw_or_label="raw")
            break
        except Exception as e:
            bad = set(_invalid_fields(e))
            if not bad:
                raise
            fields = [f for f in fields if f not in bad]
    df = recs.reset_index() if isinstance(recs, pd.DataFrame) else pd.DataFrame(recs)
    df = df.fillna("").astype(str).apply(lambda c: c.str.strip())
    return df, rid


# ───────────────────────────────────────────────────────────────
# DATA MODEL
# ───────────────────────────────────────────────────────────────
def to_dt(series):
    return pd.to_datetime(series, errors="coerce", format="ISO8601")


def lab(series, mapping):
    return series.map(lambda v: mapping.get(v, v))


def get(df, name):
    return df[name] if name in df.columns else pd.Series([""] * len(df), index=df.index)


def first_nonblank(df, rid, name):
    if name not in df.columns:
        return pd.Series(dtype=str)
    d = df[df[name] != ""].drop_duplicates(rid)
    return d.set_index(rid)[name]


def fmt_dates(df, cols):
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_datetime(out[c]).dt.strftime("%d-%m-%Y").fillna("")
    return out


def build_model(df, rid, today):
    df = df.copy()
    df["_rid"] = df[rid]
    df["_inst"] = pd.to_numeric(get(df, "redcap_repeat_instance"), errors="coerce").fillna(0)

    # Participants: enrolled = has an assigned Study ID.
    ids = pd.Index(df["_rid"].unique())
    P = pd.DataFrame(index=ids)
    P["study_id"] = first_nonblank(df, "_rid", "study_id")
    P["day0"] = to_dt(first_nonblank(df, "_rid", "medication_date"))
    if "bt_day0_date" in df.columns:
        P["day0"] = P["day0"].fillna(to_dt(first_nonblank(df, "_rid", "bt_day0_date")))
    P["arm"] = first_nonblank(df, "_rid", "bt_intervention_type").map(ARM_MAP)
    P["study_id"] = P["study_id"].fillna("")
    P["arm"] = P["arm"].fillna("")
    P = P[P["study_id"] != ""]
    sid = P["study_id"].to_dict()

    # Booking log (appt_log) --------------------------------------------------
    A = df[(get(df, "ap_appt") != "") | (get(df, "ap_psf_appt") != "")].copy()
    A["code"] = get(A, "ap_appt").where(get(A, "ap_appt") != "", get(A, "ap_psf_appt"))
    for c in ("ap_target_date", "ap_window_open", "ap_window_close", "ap_booked_date",
              "ap_rem_call_date", "ap_resched_new_date"):
        A[c] = to_dt(get(A, c))
    A["eff_date"] = A["ap_resched_new_date"].where(
        (get(A, "ap_rem_outcome") == "3") & A["ap_resched_new_date"].notna(), A["ap_booked_date"]
    )
    A = A.sort_values(["_rid", "_inst"])
    A_latest = A.drop_duplicates(["_rid", "code"], keep="last").set_index(["_rid", "code"])

    # Attendance: study visits (visit_log) ------------------------------------
    V = df[get(df, "visit_window") != ""].copy()
    V["_rank"] = (get(V, "visit_status") != "1").astype(int)
    V = V.sort_values("_rank").drop_duplicates(["_rid", "visit_window"])
    V = V.set_index(["_rid", "visit_window"])

    # Attendance: PSF sessions (behavioral_tracking) --------------------------
    psf_att = {}
    for i in range(1, 9):
        code = str(10 + i)
        stt = first_nonblank(df, "_rid", f"bt_psf_s{i}_status")
        act = to_dt(first_nonblank(df, "_rid", f"bt_psf_s{i}_actual_date"))
        for r in set(stt.index) | set(act.index):
            psf_att[(r, code)] = (stt.get(r, ""), act.get(r, pd.NaT))

    # Tracing -----------------------------------------------------------------
    T = df[(get(df, "ta_visit") != "") | (get(df, "ta_date") != "")].copy()
    T["ta_date"] = to_dt(get(T, "ta_date"))
    T = T.sort_values(["_rid", "ta_date", "_inst"])
    C = df[(get(df, "tc_visit") != "") | (get(df, "tc_outcome") != "")].copy()
    C["tc_closed_date"] = to_dt(get(C, "tc_closed_date"))
    C = C.sort_values(["_rid", "tc_closed_date", "_inst"])
    exit_ids = set(C.loc[get(C, "tc_outcome").isin(TC_EXIT), "_rid"])
    t_last = T.drop_duplicates(["_rid", "ta_visit"], keep="last").set_index(["_rid", "ta_visit"])
    t_count = T.groupby(["_rid", "ta_visit"]).size()
    c_last = C.drop_duplicates(["_rid", "tc_visit"], keep="last").set_index(["_rid", "tc_visit"])

    # Expected schedule -------------------------------------------------------
    rows = []
    for r, p in P[P["day0"].notna()].iterrows():
        codes = list(STUDY_FOLLOWUPS) + (list(PSF_SESSIONS) if p["arm"] == "PSF" else [])
        for code in codes:
            target = p["day0"] + pd.Timedelta(days=OFFSETS[code])
            opens, closes = target - pd.Timedelta(days=WINDOW_DAYS), target + pd.Timedelta(days=WINDOW_DAYS)
            b = A_latest.loc[(r, code)] if (r, code) in A_latest.index else None
            booked = call_date = eff = pd.NaT
            call_outcome = contacted = booking_notes = ""
            if b is not None:
                booked, call_date, eff = b["ap_booked_date"], b["ap_rem_call_date"], b["eff_date"]
                call_outcome = REM_OUTCOME.get(b.get("ap_rem_outcome", ""), b.get("ap_rem_outcome", ""))
                contacted = CONTACTED.get(b.get("ap_contacted", ""), b.get("ap_contacted", ""))
                booking_notes = b.get("ap_notes", "")
            # attendance
            attended, actual, explicit = False, pd.NaT, ""
            if code in STUDY_FOLLOWUPS and (r, code) in V.index:
                v = V.loc[(r, code)]
                vs, vd = v.get("visit_status", ""), pd.to_datetime(v.get("visit_date", ""), errors="coerce")
                if vs == "1" or (vs == "" and pd.notna(vd)):
                    attended, actual = True, vd
                elif vs == "2":
                    explicit = "Missed"
                elif vs == "4":
                    explicit = "Early termination"
            elif code in PSF_SESSIONS and (r, code) in psf_att:
                pst, pd_ = psf_att[(r, code)]
                if pst == "1" or (pst == "" and pd.notna(pd_)):
                    attended, actual = True, pd_
                elif pst == "0":
                    explicit = "Missed"
            deadline = max([d for d in (closes, eff) if pd.notna(d)])
            if attended:
                status = "Kept"
            elif explicit == "Early termination":
                status = "Early termination"
            elif r in exit_ids:
                status = "Closed (withdrawn/died)"
            elif explicit == "Missed" or today > deadline:
                status = "Missed"
            elif today >= opens:
                status = "Due now"
            else:
                status = "Upcoming"
            timing = ""
            if status == "Kept" and pd.notna(actual):
                timing = "In window" if opens <= actual <= closes else ("Early" if actual < opens else "Late")
            n_att = int(t_count.get((r, code), 0))
            tl = t_last.loc[(r, code)] if (r, code) in t_last.index else None
            cl = c_last.loc[(r, code)] if (r, code) in c_last.index else None
            rows.append({
                "Record ID": r, "Study ID": p["study_id"], "Arm": p["arm"] or "Not assigned",
                "code": code, "Appointment": APPT_LABELS[code], "Day 0": p["day0"],
                "Target date": target, "Window opens": opens, "Window closes": closes,
                "Booked": "Yes" if pd.notna(booked) else "No",
                "Booked date": booked, "Effective date": eff if pd.notna(eff) else booked,
                "Call attempt date": call_date, "Call outcome": call_outcome,
                "Who was contacted": contacted, "Booking notes": booking_notes,
                "Status": status, "Actual date": actual, "Timing": timing,
                "Days past window": (today - closes).days if status == "Missed" else None,
                "Tracing attempts": n_att,
                "Last attempt": tl["ta_date"] if tl is not None else pd.NaT,
                "Last attempt outcome": TA_OUTCOME.get(tl["ta_outcome"], tl["ta_outcome"]) if tl is not None else "",
                "Reason for missing": TA_REASON.get(tl["ta_reason"], tl["ta_reason"]) if tl is not None else "",
                "Next step": TA_NEXT.get(tl["ta_next_step"], tl["ta_next_step"]) if tl is not None else "",
                "Closure outcome": TC_OUTCOME.get(cl["tc_outcome"], cl["tc_outcome"]) if cl is not None else "",
                "Closure date": cl["tc_closed_date"] if cl is not None else pd.NaT,
            })
    S = pd.DataFrame(rows)
    if S.empty:
        S = pd.DataFrame(columns=["Record ID", "Study ID", "Arm", "code", "Appointment", "Status"])
    else:
        S["Appointment"] = pd.Categorical(S["Appointment"], APPT_ORDER, ordered=True)
        S = S.sort_values(["Record ID", "Target date", "Appointment"]).reset_index(drop=True)
        S["Appointment"] = S["Appointment"].astype(str)
        S["Days past window"] = pd.to_numeric(S["Days past window"], errors="coerce").astype("Int64")

    # Booking entries not in the schedule (e.g. Unscheduled visits) -----------
    if not S.empty:
        sched_keys = set(zip(S["Record ID"], S["code"]))
    else:
        sched_keys = set()
    off = A[[(r, c) not in sched_keys and r in sid for r, c in zip(A["_rid"], A["code"])]]
    return {"P": P, "S": S, "A": A, "off": off, "T": T, "C": C, "sid": sid, "exit_ids": exit_ids}


# ───────────────────────────────────────────────────────────────
# SMALL UI HELPERS
# ───────────────────────────────────────────────────────────────
def show_df(df, **kw):
    try:
        st.dataframe(df, hide_index=True, width="stretch", **kw)
    except TypeError:
        st.dataframe(df, hide_index=True, use_container_width=True, **kw)


def show_plot(fig):
    fig.update_layout(font=dict(family=PLOT_FONT, size=14), margin=dict(l=10, r=10, t=40, b=10))
    try:
        st.plotly_chart(fig, width="stretch")
    except TypeError:
        st.plotly_chart(fig, use_container_width=True)


def pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "n/a"


def download(df, name):
    st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8"), file_name=name, mime="text/csv")


def bar_counts(series, title, color="#2980b9", horizontal=True):
    s = series[series != ""].value_counts().rename_axis("label").reset_index(name="count")
    if s.empty:
        st.caption(f"{title}: no data yet.")
        return
    fig = (px.bar(s, y="label", x="count", orientation="h", title=title, text="count")
           if horizontal else px.bar(s, x="label", y="count", title=title, text="count"))
    fig.update_traces(marker_color=color)
    fig.update_layout(yaxis=dict(autorange="reversed", title=""), xaxis_title="", showlegend=False)
    show_plot(fig)


# ───────────────────────────────────────────────────────────────
# PAGE 1 - SUMMARY
# ───────────────────────────────────────────────────────────────
def page_summary(m, today):
    S, T, C = m["S"], m["T"], m["C"]
    st.markdown('<div class="main-header">Appointment Summary</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="sub-header">Appointment keeping, upcoming visits, tracing and booking · as of '
        f'{today:%d %b %Y}</div>', unsafe_allow_html=True)
    if S.empty:
        st.info("No participants with a Day 0 (medication start) date yet.")
        return

    tab1, tab2, tab3, tab4 = st.tabs(
        ["Appointment keeping", "Upcoming visits", "Tracing summary", "Booking summary"])

    # 1 ── Appointment keeping ────────────────────────────────────
    with tab1:
        due = S[S["Status"].isin(["Kept", "Missed"])]
        kept, missed = int((due["Status"] == "Kept").sum()), int((due["Status"] == "Missed").sum())
        in_win = int((S["Timing"] == "In window").sum())
        c = st.columns(5)
        c[0].metric("Participants on schedule", S["Record ID"].nunique())
        c[1].metric("Appointments due to date", kept + missed)
        c[2].metric("Kept", kept)
        c[3].metric("Missed", missed)
        c[4].metric("Keeping rate", pct(kept, kept + missed))
        st.caption(f"Kept within window: {pct(in_win, kept)} of kept appointments "
                   f"(window = target date ± {WINDOW_DAYS} days). Withdrawn/died and early-termination "
                   "participants are excluded from the rate.")

        left, right = st.columns([3, 2])
        with left:
            g = S.groupby(["Appointment", "Status"]).size().reset_index(name="n")
            fig = px.bar(g, x="Appointment", y="n", color="Status", color_discrete_map=STATUS_COLORS,
                         category_orders={"Appointment": [a for a in APPT_ORDER if a in set(S["Appointment"])]},
                         title="Status by appointment")
            fig.update_layout(xaxis_title="", yaxis_title="Appointments", legend_title="")
            show_plot(fig)
        with right:
            g = S["Status"].value_counts().reset_index()
            g.columns = ["Status", "n"]
            fig = px.pie(g, names="Status", values="n", hole=0.55, color="Status",
                         color_discrete_map=STATUS_COLORS, title="All scheduled appointments")
            show_plot(fig)

        by_arm = due.groupby("Arm")["Status"].agg(
            Due="size", Kept=lambda s: (s == "Kept").sum(), Missed=lambda s: (s == "Missed").sum()
        ).reset_index()
        by_arm["Keeping rate"] = [pct(k, d) for k, d in zip(by_arm["Kept"], by_arm["Due"])]
        st.markdown("**Keeping by intervention arm**")
        show_df(by_arm)

    # 2 ── Upcoming visits ────────────────────────────────────────
    with tab2:
        horizon = st.select_slider("Look ahead (days)", options=[7, 14, 21, 30, 60], value=14)
        U = S[S["Status"].isin(["Due now", "Upcoming"])].copy()
        U["Expected on"] = U["Effective date"].fillna(U["Target date"])
        U["Days until"] = (U["Expected on"] - today).dt.days
        U = U[U["Days until"] <= horizon].sort_values("Expected on")
        c = st.columns(4)
        c[0].metric("Due now (window open)", int((U["Status"] == "Due now").sum()))
        c[1].metric("Today", int((U["Days until"] == 0).sum()))
        c[2].metric(f"Next {horizon} days", len(U))
        c[3].metric("Not yet booked", int((U["Booked"] == "No").sum()))
        if U.empty:
            st.success("No upcoming appointments in this period.")
        else:
            g = U.groupby("Expected on").size().reset_index(name="Appointments")
            fig = px.bar(g, x="Expected on", y="Appointments", title="Appointments expected per day")
            fig.update_traces(marker_color="#2980b9")
            fig.update_layout(xaxis_title="")
            show_plot(fig)
            out = fmt_dates(U, ["Expected on", "Target date", "Window opens", "Window closes", "Booked date"])
            show_df(out[["Study ID", "Record ID", "Arm", "Appointment", "Status", "Expected on",
                         "Days until", "Window opens", "Window closes", "Booked", "Call outcome"]])
            download(out, "upcoming_appointments.csv")

    # 3 ── Tracing summary ────────────────────────────────────────
    with tab3:
        M = S[S["Status"] == "Missed"]
        traced = int((M["Tracing attempts"] > 0).sum())
        n_att = len(T)
        reached = int(get(T, "ta_outcome").isin(TA_REACHED).sum())
        c = st.columns(5)
        c[0].metric("Missed appointments", len(M))
        c[1].metric("With tracing started", traced)
        c[2].metric("No tracing yet", len(M) - traced)
        c[3].metric("Tracing attempts", n_att)
        c[4].metric("Attempts that reached participant", pct(reached, n_att))
        a, b = st.columns(2)
        with a:
            bar_counts(lab(get(T, "ta_type"), TA_TYPE), "Attempts by type")
            bar_counts(lab(get(T, "ta_reason"), TA_REASON), "Reasons for missing the appointment", "#e67e22")
        with b:
            bar_counts(lab(get(T, "ta_outcome"), TA_OUTCOME), "Outcome of attempts", "#8e44ad")
            bar_counts(lab(get(C, "tc_outcome"), TC_OUTCOME), "Tracing closure outcomes", "#c0392b")
        if len(M) - traced:
            st.markdown("**Missed appointments with no tracing attempt**")
            x = fmt_dates(M[M["Tracing attempts"] == 0], ["Window closes"])
            show_df(x[["Study ID", "Record ID", "Appointment", "Window closes", "Days past window"]])

    # 4 ── Booking summary ────────────────────────────────────────
    with tab4:
        open_ = S[S["Status"].isin(["Due now", "Upcoming", "Missed"])]
        soon = S[S["Status"].isin(["Due now", "Upcoming"]) &
                 (S["Target date"] - today <= pd.Timedelta(days=14))]
        booked_all = S[S["Booked"] == "Yes"]
        called = booked_all[booked_all["Call attempt date"].notna()]
        c = st.columns(5)
        c[0].metric("Bookings recorded", len(booked_all))
        c[1].metric("Due in next 14 days", len(soon))
        c[2].metric("…of which booked", pct(int((soon["Booked"] == "Yes").sum()), len(soon)))
        c[3].metric("Reminder call made", pct(len(called), len(booked_all)))
        c[4].metric("Rescheduled", int((S["Call outcome"] == REM_OUTCOME["3"]).sum()))
        a, b = st.columns(2)
        with a:
            bar_counts(S["Call outcome"], "Reminder call outcome", "#27ae60")
        with b:
            bar_counts(S["Who was contacted"], "Who was contacted", "#2980b9")
        need = soon[soon["Booked"] == "No"].sort_values("Target date")
        st.markdown("**Due in the next 14 days with no booking**")
        if need.empty:
            st.success("Every appointment due in the next 14 days is booked.")
        else:
            need = fmt_dates(need, ["Target date", "Window opens", "Window closes"])
            show_df(need[["Study ID", "Record ID", "Appointment", "Target date", "Window opens", "Window closes"]])


# ───────────────────────────────────────────────────────────────
# PAGE 2 - LONGITUDINAL REGISTER
# ───────────────────────────────────────────────────────────────
def page_register(m, today):
    S, T, C, off = m["S"], m["T"], m["C"], m["off"]
    sid = m["sid"]
    st.markdown('<div class="main-header">Participant Register</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-header">Longitudinal register: one row per participant per appointment</div>',
                unsafe_allow_html=True)

    f1, f2, f3 = st.columns([2, 1, 2])
    q = f1.text_input("Search Study ID or Record ID").strip().lower()
    arm = f2.selectbox("Arm", ["All", "PSF", "BA", "Not assigned"])
    appts = f3.multiselect("Appointment", APPT_ORDER)

    def keep(df, rid_col="Record ID", sid_col="Study ID", appt_col="Appointment", arm_col="Arm"):
        if df.empty:
            return df
        d = df
        if q:
            d = d[d[rid_col].astype(str).str.lower().str.contains(q, regex=False)
                  | d[sid_col].astype(str).str.lower().str.contains(q, regex=False)]
        if arm != "All" and arm_col in d.columns:
            d = d[d[arm_col] == arm]
        if appts and appt_col in d.columns:
            d = d[d[appt_col].isin(appts)]
        return d

    t1, t2, t3 = st.tabs(["1 · Booking", "2 · Tracing register", "3 · Missed appointments"])

    # 1 ── Booking ──────────────────────────────────────────────
    with t1:
        B = keep(S)
        only_booked = st.checkbox("Show booked appointments only", value=False)
        if only_booked:
            B = B[B["Booked"] == "Yes"]
        cols = ["Record ID", "Study ID", "Arm", "Appointment", "Target date", "Window opens", "Window closes",
                "Booked", "Booked date", "Call attempt date", "Call outcome", "Who was contacted",
                "Status", "Actual date", "Booking notes"]
        out = fmt_dates(B, ["Target date", "Window opens", "Window closes", "Booked date",
                            "Call attempt date", "Actual date"])
        st.caption(f"{len(out)} appointments · {out['Record ID'].nunique() if len(out) else 0} participants")
        show_df(out[[c for c in cols if c in out.columns]])
        download(out[[c for c in cols if c in out.columns]], "booking_register.csv")
        if not off.empty:
            st.markdown("**Bookings outside the expected schedule** (for example Unscheduled visits)")
            o = pd.DataFrame({
                "Record ID": off["_rid"], "Study ID": off["_rid"].map(sid),
                "Appointment": lab(off["code"], APPT_LABELS), "Booked date": off["ap_booked_date"],
                "Call attempt date": off["ap_rem_call_date"],
                "Call outcome": lab(get(off, "ap_rem_outcome"), REM_OUTCOME),
                "Notes": get(off, "ap_notes"),
            })
            o["Arm"] = ""
            o = fmt_dates(keep(o), ["Booked date", "Call attempt date"])
            show_df(o.drop(columns="Arm"))

    # 2 ── Tracing register ─────────────────────────────────────
    with t2:
        if T.empty:
            st.info("No tracing attempts recorded yet.")
        else:
            R = pd.DataFrame({
                "Record ID": T["_rid"], "Study ID": T["_rid"].map(sid),
                "Appointment": lab(get(T, "ta_visit"), APPT_LABELS),
                "Attempt date": T["ta_date"], "Time": get(T, "ta_time"),
                "Type": lab(get(T, "ta_type"), TA_TYPE), "Number used": lab(get(T, "ta_phone_used"), TA_PHONE),
                "By": get(T, "ta_by"), "Outcome": lab(get(T, "ta_outcome"), TA_OUTCOME),
                "Reason for missing": lab(get(T, "ta_reason"), TA_REASON),
                "New locator info": lab(get(T, "ta_new_locator"), {"1": "Yes", "0": "No"}),
                "Agreed to return": lab(get(T, "ta_returned"), {"1": "Yes", "0": "No"}),
                "Agreed return date": to_dt(get(T, "ta_return_date")),
                "Next step": lab(get(T, "ta_next_step"), TA_NEXT), "Notes": get(T, "ta_notes"),
            })
            R["Arm"] = R["Record ID"].map(m["P"]["arm"]).replace("", "Not assigned").fillna("Not assigned")
            R["Attempt #"] = R.groupby(["Record ID", "Appointment"]).cumcount() + 1
            R = fmt_dates(keep(R), ["Attempt date", "Agreed return date"])
            cols = ["Record ID", "Study ID", "Arm", "Appointment", "Attempt #", "Attempt date", "Time", "Type",
                    "Number used", "By", "Outcome", "Reason for missing", "New locator info",
                    "Agreed to return", "Agreed return date", "Next step", "Notes"]
            st.caption(f"{len(R)} attempts · {R['Record ID'].nunique() if len(R) else 0} participants")
            show_df(R[cols])
            download(R[cols], "tracing_register.csv")
        if not C.empty:
            st.markdown("**Tracing closure**")
            K = pd.DataFrame({
                "Record ID": C["_rid"], "Study ID": C["_rid"].map(sid),
                "Appointment": lab(get(C, "tc_visit"), APPT_LABELS),
                "Date closed": C["tc_closed_date"], "Final outcome": lab(get(C, "tc_outcome"), TC_OUTCOME),
                "PI informed": to_dt(get(C, "tc_pi_informed")),
                "Status form completed": lab(get(C, "tc_status_form"), {"1": "Yes", "0": "No"}),
                "Closed by": get(C, "tc_by"),
            })
            K["Arm"] = ""
            K = fmt_dates(keep(K), ["Date closed", "PI informed"])
            show_df(K.drop(columns="Arm"))

    # 3 ── Missed appointments ──────────────────────────────────
    with t3:
        M = keep(S[S["Status"] == "Missed"])
        if M.empty:
            st.success("No missed appointments match these filters.")
        else:
            def tracing_state(r):
                if r["Closure outcome"]:
                    return f"Closed: {r['Closure outcome']}"
                if r["Tracing attempts"] > 0:
                    return f"In progress ({int(r['Tracing attempts'])} attempts)"
                return "Not started"
            M = M.copy()
            M["Tracing status"] = M.apply(tracing_state, axis=1)
            M = M.sort_values("Days past window", ascending=False)
            c = st.columns(3)
            c[0].metric("Missed appointments", len(M))
            c[1].metric("Participants affected", M["Record ID"].nunique())
            c[2].metric("Tracing not started", int((M["Tracing status"] == "Not started").sum()))
            out = fmt_dates(M, ["Target date", "Window closes", "Booked date", "Last attempt"])
            cols = ["Record ID", "Study ID", "Arm", "Appointment", "Target date", "Window closes",
                    "Days past window", "Booked date", "Call outcome", "Tracing status",
                    "Tracing attempts", "Last attempt", "Last attempt outcome", "Reason for missing", "Next step"]
            show_df(out[cols])
            download(out[cols], "missed_appointments_register.csv")


# ───────────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────────
def main():
    require_login()

    with st.sidebar:
        st.markdown("### SOAR Appointments")
        page = st.radio("Page", ["Summary", "Participant register"], index=0)
        st.markdown("---")
        st.markdown("**Data refresh:** every 15 min")
        if st.button("Refresh data now"):
            st.cache_data.clear()
            st.cache_resource.clear()
            st.rerun()
        if st.button("Sign out"):
            st.session_state.clear()
            st.rerun()

    proj = connect_redcap()
    if proj is None:
        st.warning("REDCap credentials not found. Set REDCAP_API_URL and REDCAP_API_TOKEN.")
        st.stop()
    try:
        df, rid = load_data(proj)
    except Exception as e:
        st.error(f"Data export failed: {e}")
        st.stop()
    if df.empty:
        st.warning("No data returned from REDCap. Check the API token's export rights.")
        st.stop()

    today = pd.Timestamp.now(tz="Africa/Nairobi").tz_localize(None).normalize()
    model = build_model(df, rid, today)
    if page == "Summary":
        page_summary(model, today)
    else:
        page_register(model, today)


if __name__ == "__main__":
    main()
