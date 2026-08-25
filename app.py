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
st.set_page_config(page_title="Solar Monitor - Daily Hit List", page_icon="☀️", layout="wide")

# --- הגדרות MIN MAX (נקודת האיזון למהירות ויציבות) ---
MAX_WORKERS = 4
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

# --- מנוע טורבו והגנה מ-Rate Limits גנרי לכולם ---
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

def fetch_vcom_system_energy(site_id, auth, headers, start_dt, end_dt):
    meas_url = f"{VCOM_BASE_URL}/systems/{site_id}/abbreviations/E_DAY/measurements"
    res = request_with_retry(meas_url, auth=auth, headers=headers, params={"from": start_dt, "to": end_dt, "resolution": "day"})
    if res and res.status_code == 200:
        data_block = res.json().get('data', {})
        e_day_data = data_block.get(site_id, {}).get('E_DAY', []) 
        if not e_day_data: 
            e_day_data = data_block.get('E_DAY', [])
        
        vals = [d['value'] for d in e_day_data if d.get('value') is not None and d.get('value') > 0]
        if vals:
            return vals
    return None

def fetch_vcom_inverter_e_day(site_id, inv_id, auth, headers, start_dt, end_dt):
    url = f"{VCOM_BASE_URL}/systems/{site_id}/inverters/{inv_id}/abbreviations/E_DAY/measurements"
    res = request_with_retry(url, auth=auth, headers=headers, params={"from": start_dt, "to": end_dt, "resolution": "day"})
    if res:
        data = res.json().get('data', {}).get(inv_id, {}).get('E_DAY', [])
        return [d['value'] for d in data if d.get('value') is not None and d.get('value') > 0]
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
        
        # 1. שליפה חכמה של נתון מנורמל ונתון אבסולוטי בו זמנית
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
            
        # 2. החלטה האם להשוות באופן מנורמל או אבסולוטי (גיבוי)
        use_normalized = all(v['norm'] > 0 for v in inv_energies.values()) if inv_energies else False
        
        comparison_dict = {name: (v['norm'] if use_normalized else v['abs']) for name, v in inv_energies.items()}
        max_energy = max(comparison_dict.values()) if comparison_dict else 0
        
        unit = "kWh/kWp" if use_normalized else "kWh"
        
        for name, energy in comparison_dict.items():
            if energy == 0:
                faults.append(f"{name}: 0 {unit}")
            elif max_energy > 0 and energy < (max_energy * 0.75):
                faults.append(f"{name}: Low Output ({energy:.2f} vs max {max_energy:.2f} {unit})")

        # 3. בדיקת זרמי ה-DC לסטרינגים (שעות צהריים)
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
                    faults.append(f"{inv_name} ({abbr}): 0A (Suspected Open String/Blown Fuse)")
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
    
    # --- שלב 1: תפוקה יומית ---
    status_text.text(f"1/5: Fetching Daily Energy... (0/{total_sites_count})")
    def fetch_daily(row):
        site_id = str(row['Site_ID'])
        portal = row.get('Portal', 'SolarEdge')
        vcom_start = f"{target_date_str}T00:00:00+03:00"
        vcom_end = f"{target_date_str}T23:59:59+03:00"
        
        if portal == 'SolarEdge':
            url = f"{BASE_URL}/site/{site_id}/energy?timeUnit=DAY&startDate={target_date_str}&endDate={target_date_str}&api_key={API_KEY}"
            try:
                res = request_with_retry(url)
                if res and res.status_code == 200:
                    val = res.json()['energy']['values'][0]['value']
                    return {'Site_ID': site_id, 'Energy_kWh': val / 1000 if val is not None else 0} 
            except: pass
            return {'Site_ID': site_id, 'Energy_kWh': np.nan}
            
        elif portal == 'VCOM':
            auth, headers = get_vcom_auth(row['Account_Name'])
            if auth:
                system_vals = fetch_vcom_system_energy(site_id, auth, headers, vcom_start, vcom_end)
                if system_vals:
                    return {'Site_ID': site_id, 'Energy_kWh': max(system_vals)} 
                
                site_energy_total = 0.0
                has_data = False
                inverters = get_vcom_inverters(site_id, auth, headers)
                for inv_id in inverters: 
                    vals = fetch_vcom_inverter_e_day(site_id, inv_id, auth, headers, vcom_start, vcom_end)
                    if vals:
                        site_energy_total += max(vals) 
                        has_data = True
                return {'Site_ID': site_id, 'Energy_kWh': site_energy_total if has_data else np.nan}
        return {'Site_ID': site_id, 'Energy_kWh': np.nan}

    energy_data = []
    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_daily, row) for _, row in sites_to_scan.iterrows()]
        for future in as_completed(futures):
            energy_data.append(future.result())
            completed += 1
            progress_bar.progress(int((completed / total_sites_count) * 20))
            status_text.text(f"1/5: Fetching Daily Energy... ({completed}/{total_sites_count})")
    df_daily = pd.DataFrame(energy_data)

    # --- שלב 2: תפוקת 7 ימים ---
    status_text.text(f"2/5: Fetching 7-Day Trend... (0/{total_sites_count})")
    def fetch_7d(row):
        site_id = str(row['Site_ID'])
        portal = row.get('Portal', 'SolarEdge')
        target_dt = datetime.strptime(target_date_str, '%Y-%m-%d')
        start_date_str = (target_dt - timedelta(days=7)).strftime('%Y-%m-%d')
        vcom_start = f"{start_date_str}T00:00:00+03:00"
        vcom_end = f"{target_date_str}T23:59:59+03:00"
        
        if portal == 'SolarEdge':
            url = f"{BASE_URL}/site/{site_id}/energy?timeUnit=DAY&startDate={start_date_str}&endDate={target_date_str}&api_key={API_KEY}"
            try:
                res = request_with_retry(url)
                if res and res.status_code == 200:
                    vals = [v['value'] for v in res.json().get('energy', {}).get('values', []) if v.get('value') is not None]
                    avg = sum(vals)/len(vals) if vals else np.nan
                    return {'Site_ID': site_id, '7D_Avg_Energy_kWh': (avg / 1000) if not np.isnan(avg) else np.nan}
            except: pass
            return {'Site_ID': site_id, '7D_Avg_Energy_kWh': np.nan}
            
        elif portal == 'VCOM':
            auth, headers = get_vcom_auth(row['Account_Name'])
            if auth:
                system_vals = fetch_vcom_system_energy(site_id, auth, headers, vcom_start, vcom_end)
                if system_vals:
                    return {'Site_ID': site_id, '7D_Avg_Energy_kWh': sum(system_vals)/len(system_vals)}
                
                site_avg_total = 0.0
                has_data = False
                inverters = get_vcom_inverters(site_id, auth, headers)
                for inv_id in inverters:
                    vals = fetch_vcom_inverter_e_day(site_id, inv_id, auth, headers, vcom_start, vcom_end)
                    if vals:
                        site_avg_total += sum(vals)/len(vals)
                        has_data = True
                return {'Site_ID': site_id, '7D_Avg_Energy_kWh': site_avg_total if has_data else np.nan}
        return {'Site_ID': site_id, '7D_Avg_Energy_kWh': np.nan}

    data_7d = []
    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_7d, row) for _, row in sites_to_scan.iterrows()]
        for future in as_completed(futures):
            data_7d.append(future.result())
            completed += 1
            progress_bar.progress(20 + int((completed / total_sites_count) * 20))
            status_text.text(f"2/5: Fetching 7-Day Trend... ({completed}/{total_sites_count})")
    df_7d = pd.DataFrame(data_7d)

    # --- שלב 3: תפוקה YoY ---
    status_text.text(f"3/5: Fetching YoY Baseline... (0/{total_sites_count})")
    def fetch_yoy(row):
        site_id = str(row['Site_ID'])
        portal = row.get('Portal', 'SolarEdge')
        curr_dt = datetime.strptime(target_date_str, '%Y-%m-%d')
        ly_end = curr_dt.replace(year=curr_dt.year - 1)
        ly_start_str = (ly_end - timedelta(days=14)).strftime('%Y-%m-%d')
        ly_end_str = ly_end.strftime('%Y-%m-%d')
        vcom_start = f"{ly_start_str}T00:00:00+03:00"
        vcom_end = f"{ly_end_str}T23:59:59+03:00"
        
        if portal == 'SolarEdge':
            url = f"{BASE_URL}/site/{site_id}/energy?timeUnit=DAY&startDate={ly_start_str}&endDate={ly_end_str}&api_key={API_KEY}"
            try:
                res = request_with_retry(url)
                if res and res.status_code == 200:
                    vals = [v['value'] for v in res.json().get('energy', {}).get('values', []) if v.get('value') is not None]
                    avg = sum(vals)/len(vals) if vals else np.nan
                    return {'Site_ID': site_id, 'LY_Avg_Energy_kWh': (avg / 1000) if not np.isnan(avg) else np.nan}
            except: pass
            return {'Site_ID': site_id, 'LY_Avg_Energy_kWh': np.nan}
            
        elif portal == 'VCOM':
            auth, headers = get_vcom_auth(row['Account_Name'])
            if auth:
                system_vals = fetch_vcom_system_energy(site_id, auth, headers, vcom_start, vcom_end)
                if system_vals:
                    return {'Site_ID': site_id, 'LY_Avg_Energy_kWh': sum(system_vals)/len(system_vals)}
                
                site_avg_total = 0.0
                has_data = False
                inverters = get_vcom_inverters(site_id, auth, headers)
                for inv_id in inverters:
                    vals = fetch_vcom_inverter_e_day(site_id, inv_id, auth, headers, vcom_start, vcom_end)
                    if vals:
                        site_avg_total += sum(vals)/len(vals)
                        has_data = True
                return {'Site_ID': site_id, 'LY_Avg_Energy_kWh': site_avg_total if has_data else np.nan}
        return {'Site_ID': site_id, 'LY_Avg_Energy_kWh': np.nan}

    data_yoy = []
    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_yoy, row) for _, row in sites_to_scan.iterrows()]
        for future in as_completed(futures):
            data_yoy.append(future.result())
            completed += 1
            progress_bar.progress(40 + int((completed / total_sites_count) * 20))
            status_text.text(f"3/5: Fetching YoY Baseline... ({completed}/{total_sites_count})")
    df_yoy = pd.DataFrame(data_yoy)
    
    # --- שלב 4: עיבוד נתונים (Geo-Clustering) ---
    status_text.text("4/5: Running Geo-Clustering...")
    df_master = pd.merge(df_daily, df_sites, on='Site_ID', how='inner')
    df_master = pd.merge(df_master, df_yoy, on='Site_ID', how='left')
    df_master = pd.merge(df_master, df_7d, on='Site_ID', how='left')
    
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
    
    # --- שלב 5: דיאגנוסטיקה עמוקה לממירים ---
    suspect_count = len(sites_to_deep_scan)
    status_text.text(f"5/5: Deep Scanning Inverters... (0/{suspect_count})")
    
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
                progress_bar.progress(60 + int((completed_scans / suspect_count) * 40))
                status_text.text(f"5/5: Deep Scanning Inverters... ({completed_scans}/{suspect_count})")
        
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
