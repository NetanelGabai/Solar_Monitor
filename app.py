import streamlit as st
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import re
import random
from requests.auth import HTTPBasicAuth
from concurrent.futures import ThreadPoolExecutor, as_completed 

# --- הגדרות האפליקציה ---
st.set_page_config(page_title="Solar Monitor - AI Hit List", page_icon="☀️", layout="wide")

MAX_WORKERS = 5
VCOM_MICRO_DELAY = 0.25 

# --- הגדרות SolarEdge ---
API_KEY = "K0X7PD9WAJ11B33DM7BUWNY6VCJ9YVFS"
BASE_URL = "https://monitoringapi.solaredge.com"
default_yesterday = (datetime.now() - timedelta(1)).strftime('%Y-%m-%d')

# --- הגדרות VCOM (Meteocontrol) ---
VCOM_BASE_URL = "https://api.meteocontrol.de/v2"
VCOM_CREDENTIALS = {
    "electraservice": {
        "PASSWORD": "Elec74New!++",
        "API_KEY": "d8860d256e03c97ca24c8548c2979be6489add6c2e4a46e1c1964fb5c65bf01a"
    },
    "ELECTRA - PV": {
        "PASSWORD": "Elec74New!++",
        "API_KEY": "d8860d256e03c97ca24c8548c2979be6489add6c2e4a46e1c1964fb5c65bf01a"
    }
}

# --- מנוע רשת וטורבו ---
global_http_session = requests.Session() 
vcom_inverters_cache = {} 

def request_with_retry(url, auth=None, headers=None, params=None):
    for attempt in range(5): 
        try:
            res = global_http_session.get(url, auth=auth, headers=headers, params=params, timeout=15)
            if res.status_code == 200:
                if "meteocontrol" in url:
                    time.sleep(VCOM_MICRO_DELAY) 
                return res
            elif res.status_code == 429: 
                time.sleep((2 * (attempt + 1)) + random.uniform(0.1, 1.0)) 
                continue
        except requests.exceptions.RequestException:
            pass
        time.sleep(1) 
    return None

def get_vcom_auth(account_name):
    if account_name in VCOM_CREDENTIALS:
        creds = VCOM_CREDENTIALS[account_name]
        return HTTPBasicAuth(account_name, creds['PASSWORD']), {"X-API-KEY": creds['API_KEY'], "Accept": "application/json"}
    return None, None

def get_vcom_inverters(site_id, auth, headers):
    if site_id in vcom_inverters_cache:
        return vcom_inverters_cache[site_id]
        
    res = request_with_retry(f"{VCOM_BASE_URL}/systems/{site_id}/inverters", auth=auth, headers=headers)
    if res and res.status_code == 200:
        inverters = {str(inv['id']): inv.get('name', str(inv['id'])) for inv in res.json().get('data', [])}
        vcom_inverters_cache[site_id] = inverters
        return inverters
    return {}

# -------------------------------------------------------------------
# פונקציית נתיב הזהב (The Golden Path) החדשה ששילבנו!
# -------------------------------------------------------------------
def fetch_vcom_data(site_id, inv_id, auth, headers, start_dt, end_dt, is_system=True):
    """פונקציה חכמה שמשלבת את נתיבי החישוב החדשים ברמת האתר ומונעת ירידה מיותרת לגיבוי ממירים"""
    if is_system:
        # רשימת העדיפויות שלנו ברמת האתר: מונה ראשי -> ממירים מחושבים -> תפוקה גולמית ישנה
        system_endpoints = [
            f"{VCOM_BASE_URL}/systems/{site_id}/calculations/abbreviations/E_ZAEHLER/measurements",
            f"{VCOM_BASE_URL}/systems/{site_id}/calculations/abbreviations/E_MESS/measurements",
            f"{VCOM_BASE_URL}/systems/{site_id}/abbreviations/E_DAY/measurements"
        ]
        
        for url in system_endpoints:
            res = request_with_retry(url, auth=auth, headers=headers, params={"from": start_dt, "to": end_dt, "resolution": "day"})
            if res and res.status_code == 200:
                data = res.json().get('data', {})
                # שולף את הנתונים, לא משנה תחת איזה מפתח הם חזרו
                site_data = data.get(site_id, {}) if site_id in data else data
                for key, val_list in site_data.items():
                    if isinstance(val_list, list) and any(d.get('value') is not None for d in val_list):
                        return val_list # בינגו! מצאנו נתון תקין וחסכנו סריקת ממירים
        return [] # רק במקרה קיצון שהכל נכשל, נרד לגיבוי ממירים
    else:
        # שליפה רגילה עבור ממיר בודד (לשלב הדיאגנוסטיקה או גיבוי עמוק)
        url = f"{VCOM_BASE_URL}/systems/{site_id}/inverters/{inv_id}/abbreviations/E_DAY/measurements"
        res = request_with_retry(url, auth=auth, headers=headers, params={"from": start_dt, "to": end_dt, "resolution": "day"})
        if res and res.status_code == 200:
            return res.json().get('data', {}).get(inv_id, {}).get('E_DAY', [])
        return []

# --- פונקציות מנוע החוקים ---
@st.cache_data(ttl=3600)
def load_metadata():
    try:
        df = pd.read_csv('sites_metadata.csv', encoding='utf-8-sig') 
        df = df[~df['Name'].str.contains(r'DELETED|DISABLED|\*|^Z\s*-', na=False, case=False)]
        df = df.dropna(subset=['Latitude', 'Longitude'])
        df['Capacity_kWp'] = pd.to_numeric(df['Capacity_kWp'], errors='coerce') 
        df = df[df['Capacity_kWp'] > 0]
        if 'Portal' not in df.columns: df['Portal'] = 'SolarEdge'
        if 'Account_Name' not in df.columns: df['Account_Name'] = ''
        return df
    except Exception as e:
        st.error(f"Error loading sites_metadata.csv: {e}")
        return pd.DataFrame()

def get_capacity_from_model(model_name):
    if not model_name: return 1.0 
    match = re.search(r'SE(\d+(?:\.\d+)?)', model_name, re.IGNORECASE)
    if match: return float(match.group(1))
    return 1.0 

def get_inverter_diagnosis(row, target_date):
    site_id = str(row['Site_ID'])
    portal = row.get('Portal', 'SolarEdge')
    
    if portal == 'VCOM':
        auth, headers = get_vcom_auth(row['Account_Name'])
        if not auth: return "VCOM Auth Error"
        
        inverters = get_vcom_inverters(site_id, auth, headers)
        if not inverters: return "No inverters found"
        
        faults = []
        inv_energies = {}
        day_start = f"{target_date}T00:00:00+03:00"
        day_end = f"{target_date}T23:59:59+03:00"
        
        # דיאגנוסטיקת AC מנורמלת ע"י שליפת E_INT_N
        for inv_id, inv_name in inverters.items():
            abbr_str = "E_INT_N,E_DAY"
            meas_url = f"{VCOM_BASE_URL}/systems/{site_id}/inverters/{inv_id}/abbreviations/{abbr_str}/measurements"
            meas_res = request_with_retry(meas_url, auth=auth, headers=headers, params={"from": day_start, "to": day_end, "resolution": "day"})
            
            norm_val, abs_val = 0.0, 0.0
            if meas_res and meas_res.status_code == 200:
                data = meas_res.json().get('data', {}).get(inv_id, {})
                norm_data = data.get('E_INT_N', [])
                valid_norm = [d['value'] for d in norm_data if d.get('value') is not None and d.get('value') > 0]
                if valid_norm: norm_val = max(valid_norm)
                
                abs_data = data.get('E_DAY', [])
                valid_abs = [d['value'] for d in abs_data if d.get('value') is not None and d.get('value') > 0]
                if valid_abs: abs_val = max(valid_abs)
                
            inv_energies[inv_name] = {'norm': norm_val, 'abs': abs_val}
            
        use_normalized = all(v['norm'] > 0 for v in inv_energies.values()) if inv_energies else False
        comparison_dict = {name: (v['norm'] if use_normalized else v['abs']) for name, v in inv_energies.items()}
        max_energy = max(comparison_dict.values()) if comparison_dict else 0
        unit = "kWh/kWp" if use_normalized else "kWh"
        
        for name, energy in comparison_dict.items():
            if energy == 0:
                faults.append(f"{name}: 0 {unit}")
            elif max_energy > 0 and energy < (max_energy * 0.75):
                faults.append(f"{name}: Low Output ({energy:.2f} vs max {max_energy:.2f} {unit})")

        # דיאגנוסטיקת DC לסטרינגים (שעות צהריים)
        start_time = f"{target_date}T11:00:00+03:00"
        end_time = f"{target_date}T13:00:00+03:00"
        
        for inv_id, inv_name in inverters.items():
            abbr_url = f"{VCOM_BASE_URL}/systems/{site_id}/inverters/{inv_id}/abbreviations"
            abbr_res = request_with_retry(abbr_url, auth=auth, headers=headers)
            if not abbr_res: continue
            
            available_abbrs = abbr_res.json().get('data', [])
            dc_abbrs = [abbr for abbr in available_abbrs if abbr.startswith('I_DC')]
            if not dc_abbrs: continue
            
            abbr_str = ",".join(dc_abbrs)
            meas_url = f"{VCOM_BASE_URL}/systems/{site_id}/inverters/{inv_id}/abbreviations/{abbr_str}/measurements"
            meas_res = request_with_retry(meas_url, auth=auth, headers=headers, params={"from": start_time, "to": end_time, "resolution": "15min"})
            
            if not meas_res: continue
            
            data = meas_res.json().get('data', {}).get(inv_id, {})
            string_medians = {}
            for abbr in dc_abbrs:
                measurements = data.get(abbr, [])
                vals = [m['value'] for m in measurements if m.get('value') is not None]
                string_medians[abbr] = np.median(vals) if vals else 0.0
                
            if not string_medians: continue
            
            inv_median_current = np.median(list(string_medians.values()))
            for abbr, current in string_medians.items():
                if current < 0.5: 
                    faults.append(f"{inv_name} ({abbr}): 0A (Suspected Open String)")
                elif inv_median_current > 2.0 and current < (inv_median_current * 0.6): 
                    faults.append(f"{inv_name} ({abbr}): Low Current ({current:.1f}A vs avg {inv_median_current:.1f}A)")
                    
        return "Faults: " + " | ".join(set(faults)) if faults else "Inverters balanced. Check Shading/Soiling."
        
    elif portal == 'SolarEdge':
        equip_url = f"{BASE_URL}/equipment/{site_id}/list?api_key={API_KEY}"
        try:
            res = request_with_retry(equip_url)
            if not res or res.status_code != 200: return "API Error"
            
            inverters = [eq for eq in res.json().get('reporters', {}).get('list', []) if 'inverter' in eq.get('name', '').lower() or eq.get('type', '') == 'Inverter']
            if not inverters: return "No inverters found"
                
            inverter_data = {}
            for inv in inverters:
                sn = inv.get('serialNumber')
                name = inv.get('name', sn)
                capacity_kw = get_capacity_from_model(inv.get('model', ''))
                
                data_url = f"{BASE_URL}/equipment/{site_id}/{sn}/data?startTime={target_date} 00:00:00&endTime={target_date} 23:59:59&api_key={API_KEY}"
                d_res = request_with_retry(data_url)
                inv_energy = 0
                if d_res and d_res.status_code == 200:
                    energies = [t.get('totalEnergy') for t in d_res.json().get('data', {}).get('telemetries', []) if t.get('totalEnergy') is not None]
                    if energies: inv_energy = (max(energies) - min(energies)) / 1000 
                
                inverter_data[name] = {
                    'energy': inv_energy,
                    'capacity': capacity_kw,
                    'specific_yield': inv_energy / capacity_kw if capacity_kw > 0 else 0
                }
                
            if not any(d['energy'] > 0 for d in inverter_data.values()): 
                return "Site Offline: All inverters at 0 kWh"
                
            max_specific_yield = max([d['specific_yield'] for d in inverter_data.values()])
            faults = []
            for name, data in inverter_data.items():
                cap_label = f"[{data['capacity']:g}kW]" if data['capacity'] > 0 else ""
                if data['energy'] == 0:
                    faults.append(f"{name} {cap_label}: 0 kWh")
                elif data['specific_yield'] < (max_specific_yield * 0.75): 
                    faults.append(f"{name} {cap_label}: Low Output ({data['energy']:.1f} kWh)")
                    
            return "Faults: " + " | ".join(faults) if faults else "Inverters balanced. Check Shading/Soiling."
        except Exception as e:
            return f"Diagnosis Failed: {str(e)[:30]}"

# --- ממשק המשתמש (UI) ---
st.title("☀️ Solar Monitor - AI Hit List")
st.markdown("Automated anomaly detection using Geo-Clustering, YoY trends, and normalized Inverter-level diagnosis.")

df_sites = load_metadata()

with st.sidebar:
    st.header("⚙️ Scan Settings")
    target_date = st.date_input("Select Date to Monitor", datetime.strptime(default_yesterday, '%Y-%m-%d'))
    target_date_str = target_date.strftime('%Y-%m-%d')
    
    st.divider()
    st.subheader("🎯 Site Selection")
    
    if not df_sites.empty:
        site_options = sorted(df_sites['Name'].tolist())
        selected_sites = st.multiselect("Select specific sites to scan (optional):", options=site_options)
        
        st.divider()
        scan_mode = None
        scan_limit = 0
        
        if not selected_sites:
            st.subheader("🗂️ Bulk Scan Options")
            total_sites = len(df_sites)
            scan_mode = st.radio(
                "Select portfolio to scan:",
                options=[
                    "🧪 Test Mode (Custom Sample)",
                    "☀️ All SolarEdge Sites",
                    "⚡ VCOM: electraservice",
                    "⚡ VCOM: ELECTRA - PV",
                    f"⚠️ Scan ALL ({total_sites} sites - High Timeout Risk)"
                ]
            )
            if scan_mode == "🧪 Test Mode (Custom Sample)":
                scan_limit = st.number_input(f"Number of Sites to Scan (Max: {total_sites})", min_value=1, max_value=total_sites if total_sites > 0 else 1, value=min(20, total_sites), step=10)
    
    run_button = st.button("🚀 Run Anomaly Detection", use_container_width=True, type="primary")

if run_button:
    if df_sites.empty: st.stop()
        
    if selected_sites:
        sites_to_scan = df_sites[df_sites['Name'].isin(selected_sites)].copy()
    else:
        if scan_mode == "🧪 Test Mode (Custom Sample)":
            sites_to_scan = df_sites.head(int(scan_limit)).copy()
        elif scan_mode == "☀️ All SolarEdge Sites":
            sites_to_scan = df_sites[df_sites['Portal'] == 'SolarEdge'].copy()
        elif scan_mode == "⚡ VCOM: electraservice":
            sites_to_scan = df_sites[(df_sites['Portal'] == 'VCOM') & (df_sites['Account_Name'] == 'electraservice')].copy()
        elif scan_mode == "⚡ VCOM: ELECTRA - PV":
            sites_to_scan = df_sites[(df_sites['Portal'] == 'VCOM') & (df_sites['Account_Name'] == 'ELECTRA - PV')].copy()
        elif scan_mode and scan_mode.startswith("⚠️ Scan ALL"):
            sites_to_scan = df_sites.copy()
        else:
            sites_to_scan = df_sites.head(20).copy()
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    total_sites_count = len(sites_to_scan)
    
    # === איחוד שלבים (Data Fusion): משיכת יומי, 7 ימים ו-YoY במקשה אחת ===
    status_text.text(f"1/3: Fetching All Baselines... (0/{total_sites_count})")
    
    def fetch_site_baselines(row, target_date_str):
        site_id = str(row['Site_ID'])
        portal = row.get('Portal', 'SolarEdge')
        
        target_dt = datetime.strptime(target_date_str, '%Y-%m-%d')
        start_7d_str = (target_dt - timedelta(days=6)).strftime('%Y-%m-%d') 
        ly_end = target_dt.replace(year=target_dt.year - 1)
        ly_start_str = (ly_end - timedelta(days=13)).strftime('%Y-%m-%d')
        ly_end_str = ly_end.strftime('%Y-%m-%d')
        
        vcom_start_7d = f"{start_7d_str}T00:00:00+03:00"
        vcom_end_7d = f"{target_date_str}T23:59:59+03:00"
        vcom_start_yoy = f"{ly_start_str}T00:00:00+03:00"
        vcom_end_yoy = f"{ly_end_str}T23:59:59+03:00"
        
        result = {'Site_ID': site_id, 'Energy_kWh': np.nan, '7D_Avg_Energy_kWh': np.nan, 'LY_Avg_Energy_kWh': np.nan}
        
        if portal == 'SolarEdge':
            url_7d = f"{BASE_URL}/site/{site_id}/energy?timeUnit=DAY&startDate={start_7d_str}&endDate={target_date_str}&api_key={API_KEY}"
            res_7d = request_with_retry(url_7d)
            if res_7d and res_7d.status_code == 200:
                vals = res_7d.json().get('energy', {}).get('values', [])
                valid_vals = [v['value'] for v in vals if v.get('value') is not None]
                if valid_vals: result['7D_Avg_Energy_kWh'] = sum(valid_vals) / len(valid_vals) / 1000.0
                daily_obj = next((v for v in vals if target_date_str in v.get('date', '')), None)
                if daily_obj and daily_obj.get('value') is not None: result['Energy_kWh'] = daily_obj['value'] / 1000.0
            
            url_yoy = f"{BASE_URL}/site/{site_id}/energy?timeUnit=DAY&startDate={ly_start_str}&endDate={ly_end_str}&api_key={API_KEY}"
            res_yoy = request_with_retry(url_yoy)
            if res_yoy and res_yoy.status_code == 200:
                vals = res_yoy.json().get('energy', {}).get('values', [])
                valid_vals = [v['value'] for v in vals if v.get('value') is not None]
                if valid_vals: result['LY_Avg_Energy_kWh'] = sum(valid_vals) / len(valid_vals) / 1000.0
                    
        elif portal == 'VCOM':
            auth, headers = get_vcom_auth(row['Account_Name'])
            if auth:
                # משתמש בפונקציית נתיב הזהב החדשה שלנו!
                sys_7d_data = fetch_vcom_data(site_id, None, auth, headers, vcom_start_7d, vcom_end_7d, is_system=True)
                sys_yoy_data = fetch_vcom_data(site_id, None, auth, headers, vcom_start_yoy, vcom_end_yoy, is_system=True)
                
                if sys_7d_data or sys_yoy_data:
                    if sys_7d_data:
                        daily_val = next((d['value'] for d in sys_7d_data if target_date_str in d.get('timestamp', '') and d.get('value') is not None), np.nan)
                        result['Energy_kWh'] = daily_val
                        valid_7d = [d['value'] for d in sys_7d_data if d.get('value') is not None and d.get('value') > 0]
                        if valid_7d: result['7D_Avg_Energy_kWh'] = sum(valid_7d) / len(valid_7d)
                    
                    if sys_yoy_data:
                        valid_yoy = [d['value'] for d in sys_yoy_data if d.get('value') is not None and d.get('value') > 0]
                        if valid_yoy: result['LY_Avg_Energy_kWh'] = sum(valid_yoy) / len(valid_yoy)
                else:
                    # גיבוי: מעבר על ממירים פעם אחת בלבד לשני השלבים!
                    inverters = get_vcom_inverters(site_id, auth, headers)
                    if inverters:
                        tot_daily, tot_7d_avg, tot_yoy_avg = 0.0, 0.0, 0.0
                        has_daily, has_7d, has_yoy = False, False, False
                        
                        for inv_id in inverters:
                            inv_7d = fetch_vcom_data(site_id, inv_id, auth, headers, vcom_start_7d, vcom_end_7d, is_system=False)
                            if inv_7d:
                                d_val = next((d['value'] for d in inv_7d if target_date_str in d.get('timestamp', '') and d.get('value') is not None), 0)
                                tot_daily += d_val
                                if d_val > 0: has_daily = True
                                
                                valid_7d = [d['value'] for d in inv_7d if d.get('value') is not None and d.get('value') > 0]
                                if valid_7d:
                                    tot_7d_avg += sum(valid_7d) / len(valid_7d)
                                    has_7d = True
                            
                            inv_yoy = fetch_vcom_data(site_id, inv_id, auth, headers, vcom_start_yoy, vcom_end_yoy, is_system=False)
                            if inv_yoy:
                                valid_yoy = [d['value'] for d in inv_yoy if d.get('value') is not None and d.get('value') > 0]
                                if valid_yoy:
                                    tot_yoy_avg += sum(valid_yoy) / len(valid_yoy)
                                    has_yoy = True
                                    
                        if has_daily: result['Energy_kWh'] = tot_daily
                        if has_7d: result['7D_Avg_Energy_kWh'] = tot_7d_avg
                        if has_yoy: result['LY_Avg_Energy_kWh'] = tot_yoy_avg

        return result

    baseline_data = []
    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_site_baselines, row, target_date_str) for _, row in sites_to_scan.iterrows()]
        for future in as_completed(futures):
            baseline_data.append(future.result())
            completed += 1
            progress_bar.progress(int((completed / total_sites_count) * 60)) 
            status_text.text(f"1/3: Fetching All Baselines... ({completed}/{total_sites_count})")
            
    df_baselines = pd.DataFrame(baseline_data)
    
    # --- שלב 2: עיבוד נתונים (Geo-Clustering) ---
    status_text.text("2/3: Running Geo-Clustering...")
    df_master = pd.merge(df_baselines, df_sites, on='Site_ID', how='inner')
    
    df_master['Specific_Yield'] = df_master['Energy_kWh'] / df_master['Capacity_kWp']
    df_master['LY_Avg_Yield'] = df_master['LY_Avg_Energy_kWh'] / df_master['Capacity_kWp']
    df_master['7D_Avg_Yield'] = df_master['7D_Avg_Energy_kWh'] / df_master['Capacity_kWp']
    
    df_master['YoY_Change_%'] = np.where(df_master['LY_Avg_Yield'] > 0, ((df_master['Specific_Yield'] / df_master['LY_Avg_Yield']) - 1) * 100, np.nan)
    df_master['7D_Change_%'] = np.where(df_master['7D_Avg_Yield'] > 0, ((df_master['Specific_Yield'] / df_master['7D_Avg_Yield']) - 1) * 100, np.nan)
    
    df_master['Lat_Grid'] = df_master['Latitude'].astype(float).round(1)
    df_master['Lon_Grid'] = df_master['Longitude'].astype(float).round(1)
    
    cluster_medians = df_master.groupby(['Lat_Grid', 'Lon_Grid'])['Specific_Yield'].median().reset_index()
    cluster_medians.rename(columns={'Specific_Yield': 'Cluster_Median_Yield'}, inplace=True)
    df_master = pd.merge(df_master, cluster_medians, on=['Lat_Grid', 'Lon_Grid'], how='left')
    
    df_master['Performance_vs_Cluster'] = np.where(df_master['Cluster_Median_Yield'] > 0, df_master['Specific_Yield'] / df_master['Cluster_Median_Yield'], np.nan)
    
    df_master['Needs_Deep_Scan'] = (
        (df_master['Performance_vs_Cluster'] < 0.97) | 
        (df_master['7D_Change_%'] < -3.0) | 
        (df_master['YoY_Change_%'] < -10.0)
    )
    
    if selected_sites:
        sites_to_deep_scan = df_master.copy()
    else:
        sites_to_deep_scan = df_master[df_master['Needs_Deep_Scan']].copy()
    
    progress_bar.progress(70)
    
    # --- שלב 3: דיאגנוסטיקה עמוקה לממירים חשודים ---
    suspect_count = len(sites_to_deep_scan)
    status_text.text(f"3/3: Deep Scanning Inverters... (0/{suspect_count})")
    
    diagnoses_dict = {}
    completed_scans = 0
    
    def fetch_diagnosis(row):
        return row['Site_ID'], get_inverter_diagnosis(row, target_date_str)
        
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_diagnosis, row) for _, row in sites_to_deep_scan.iterrows()]
        for future in as_completed(futures):
            site_id, diagnosis_result = future.result()
            diagnoses_dict[site_id] = diagnosis_result
            completed_scans += 1
            if suspect_count > 0:
                progress_bar.progress(70 + int((completed_scans / suspect_count) * 30))
                status_text.text(f"3/3: Deep Scanning Inverters... ({completed_scans}/{suspect_count})")
        
    df_master['System_Diagnosis'] = df_master['Site_ID'].map(diagnoses_dict).fillna("Skipped (Site Optimal - No Drops Detected)")
    
    df_master['Alert_Status'] = np.where(
        (df_master['Performance_vs_Cluster'] < 0.80) | 
        (df_master['7D_Change_%'] < -10.0) | 
        (df_master['YoY_Change_%'] < -20.0) |
        (df_master['System_Diagnosis'].str.contains('0 kWh|0A|Low Current|Low Output|Offline|Faults', na=False, regex=True)), 
        '🔴 Fault Suspected', 
        '🟢 OK'
    )
    
    progress_bar.progress(100)
    status_text.text("Scan Complete!")
    time.sleep(1)
    status_text.empty()
    progress_bar.empty()
    
    if selected_sites:
        anomalies = df_master.copy()
    else:
        anomalies = df_master[df_master['Alert_Status'] == '🔴 Fault Suspected'].copy()
    
    st.divider()
    col1, col2, col3 = st.columns(3)
    col1.metric("Sites Scanned", len(df_master))
    
    if selected_sites:
        col2.metric("Sites Displayed", len(anomalies))
        st.subheader("🔍 Selected Sites Analysis")
    else:
        col2.metric("Anomalies Detected", len(anomalies), delta_color="inverse")
        col3.metric("Clean Sites", len(df_master) - len(anomalies))
        st.subheader("🚨 Priority Hit List")
    
    if anomalies.empty and not selected_sites:
        st.success("All monitored sites are performing well! No anomalies detected.")
    elif not anomalies.empty:
        display_df = anomalies[['Name', 'City', 'Specific_Yield', 'Cluster_Median_Yield', '7D_Change_%', 'YoY_Change_%', 'System_Diagnosis', 'Alert_Status']].copy()
        
        display_df['7D_Change_%'] = display_df['7D_Change_%'].apply(lambda x: f"{x:.1f}%" if pd.notnull(x) else "-")
        display_df['YoY_Change_%'] = display_df['YoY_Change_%'].apply(lambda x: f"{x:.1f}%" if pd.notnull(x) else "-")
        
        display_df = display_df.astype(object)
        display_df.fillna("-", inplace=True)
        
        display_df.rename(columns={
            'Specific_Yield': 'Yield (kWh/kWp)',
            'Cluster_Median_Yield': 'Area Median (kWh/kWp)',
            '7D_Change_%': '7-Day Trend',
            'YoY_Change_%': 'YoY Trend',
            'System_Diagnosis': 'AI Diagnosis',
            'Alert_Status': 'Status'
        }, inplace=True)
        
        def highlight_offline(s):
            if 'Offline' in str(s.get('AI Diagnosis', '')) or '0A' in str(s.get('AI Diagnosis', '')): 
                return ['background-color: #4a1c1c; color: #ffcccc'] * len(s)
            elif 'Fault Suspected' in str(s.get('Status', '')):
                return ['background-color: #331a00'] * len(s) 
            return [''] * len(s)
            
        st.dataframe(
            display_df.style
            .format({
                'Yield (kWh/kWp)': lambda x: f"{x:.2f}" if isinstance(x, (int, float)) else x,
                'Area Median (kWh/kWp)': lambda x: f"{x:.2f}" if isinstance(x, (int, float)) else x
            })
            .apply(highlight_offline, axis=1),
            use_container_width=True,
            hide_index=True,
            height=400
        )
