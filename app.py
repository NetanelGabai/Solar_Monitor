import streamlit as st
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import re
from requests.auth import HTTPBasicAuth
from concurrent.futures import ThreadPoolExecutor, as_completed # התוספת לעיבוד מקבילי

# --- הגדרות האפליקציה ---
st.set_page_config(page_title="Solar Monitor - Daily Hit List", page_icon="☀️", layout="wide")

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

# --- מנוע טורבו לשליפות ---
global_http_session = requests.Session() # שומר על חיבור פתוח
vcom_inverters_cache = {} # זוכר ממירים כדי לחסוך קריאות API

def get_vcom_auth(account_name):
    if account_name in VCOM_CREDENTIALS:
        creds = VCOM_CREDENTIALS[account_name]
        return HTTPBasicAuth(account_name, creds['PASSWORD']), {"X-API-KEY": creds['API_KEY'], "Accept": "application/json"}
    return None, None

def get_vcom_inverters(site_id, auth, headers):
    if site_id in vcom_inverters_cache:
        return vcom_inverters_cache[site_id]
        
    try:
        inv_res = global_http_session.get(f"{VCOM_BASE_URL}/systems/{site_id}/inverters", auth=auth, headers=headers, timeout=10)
        if inv_res.status_code == 200:
            inverters = [str(inv['id']) for inv in inv_res.json().get('data', [])]
            vcom_inverters_cache[site_id] = inverters
            return inverters
    except:
        pass
    return []

# --- פונקציות מנוע החוקים ---
@st.cache_data(ttl=3600)
def load_metadata():
    try:
        df = pd.read_csv('sites_metadata.csv')
        df = df[~df['Name'].str.contains(r'DELETED|DISABLED|\*|^Z\s*-', na=False, case=False)]
        df = df.dropna(subset=['Latitude', 'Longitude'])
        df = df[df['Capacity_kWp'] > 0]
        if 'Portal' not in df.columns: df['Portal'] = 'SolarEdge'
        if 'Account_Name' not in df.columns: df['Account_Name'] = ''
        return df
    except Exception:
        st.error("Error loading sites_metadata.csv. Make sure the file exists in the directory.")
        return pd.DataFrame()

def get_daily_site_energy(df_sites_to_scan, target_date_str):
    vcom_start = f"{target_date_str}T00:00:00Z"
    vcom_end = f"{target_date_str}T23:59:59Z"
    
    def fetch_single(row):
        site_id = str(row['Site_ID'])
        portal = row.get('Portal', 'SolarEdge')
        
        if portal == 'SolarEdge':
            url = f"{BASE_URL}/site/{site_id}/energy?timeUnit=DAY&startDate={target_date_str}&endDate={target_date_str}&api_key={API_KEY}"
            try:
                res = global_http_session.get(url, timeout=10)
                if res.status_code == 200:
                    val = res.json()['energy']['values'][0]['value']
                    return {'Site_ID': site_id, 'Energy_kWh': 0 if val is None else val}
            except:
                pass
            return {'Site_ID': site_id, 'Energy_kWh': np.nan}
                
        elif portal == 'VCOM':
            auth, headers = get_vcom_auth(row['Account_Name'])
            site_energy_total = 0.0
            has_data = False
            if auth:
                inverters = get_vcom_inverters(site_id, auth, headers)
                for inv_id in inverters:
                    meas_url = f"{VCOM_BASE_URL}/systems/{site_id}/inverters/{inv_id}/abbreviations/E_DAY/measurements"
                    try:
                        meas_res = global_http_session.get(meas_url, auth=auth, headers=headers, params={"from": vcom_start, "to": vcom_end, "resolution": "day"}, timeout=10)
                        if meas_res.status_code == 200:
                            data = meas_res.json().get('data', {}).get(inv_id, {}).get('E_DAY', [])
                            valid_vals = [d['value'] for d in data if d.get('value') is not None]
                            if valid_vals:
                                site_energy_total += max(valid_vals) * 1000 
                                has_data = True
                    except:
                        pass
            return {'Site_ID': site_id, 'Energy_kWh': site_energy_total if has_data else np.nan}
        return {'Site_ID': site_id, 'Energy_kWh': np.nan}

    energy_data = []
    # מריץ 10 אתרים במקביל!
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_single, row) for _, row in df_sites_to_scan.iterrows()]
        for future in as_completed(futures):
            energy_data.append(future.result())
            
    return pd.DataFrame(energy_data)

def get_7_day_baseline(df_sites_to_scan, target_date_str):
    target_date = datetime.strptime(target_date_str, '%Y-%m-%d')
    start_date_str = (target_date - timedelta(days=7)).strftime('%Y-%m-%d')
    vcom_start = f"{start_date_str}T00:00:00Z"
    vcom_end = f"{target_date_str}T23:59:59Z"
    
    def fetch_single(row):
        site_id = str(row['Site_ID'])
        portal = row.get('Portal', 'SolarEdge')
        
        if portal == 'SolarEdge':
            url = f"{BASE_URL}/site/{site_id}/energy?timeUnit=DAY&startDate={start_date_str}&endDate={target_date_str}&api_key={API_KEY}"
            try:
                res = global_http_session.get(url, timeout=10)
                if res.status_code == 200:
                    vals = [v['value'] for v in res.json().get('energy', {}).get('values', []) if v.get('value') is not None]
                    avg = sum(vals)/len(vals) if vals else np.nan
                    return {'Site_ID': site_id, '7D_Avg_Energy_kWh': avg / 1000 if not np.isnan(avg) else np.nan}
            except:
                pass
            return {'Site_ID': site_id, '7D_Avg_Energy_kWh': np.nan}
                
        elif portal == 'VCOM':
            auth, headers = get_vcom_auth(row['Account_Name'])
            site_avg_total = 0.0
            has_data = False
            if auth:
                inverters = get_vcom_inverters(site_id, auth, headers)
                for inv_id in inverters:
                    meas_url = f"{VCOM_BASE_URL}/systems/{site_id}/inverters/{inv_id}/abbreviations/E_DAY/measurements"
                    try:
                        meas_res = global_http_session.get(meas_url, auth=auth, headers=headers, params={"from": vcom_start, "to": vcom_end, "resolution": "day"}, timeout=10)
                        if meas_res.status_code == 200:
                            data = meas_res.json().get('data', {}).get(inv_id, {}).get('E_DAY', [])
                            vals = [d['value'] for d in data if d.get('value') is not None]
                            if vals:
                                inv_avg = sum(vals)/len(vals)
                                site_avg_total += inv_avg 
                                has_data = True
                    except:
                        pass
            return {'Site_ID': site_id, '7D_Avg_Energy_kWh': site_avg_total if has_data else np.nan}
        return {'Site_ID': site_id, '7D_Avg_Energy_kWh': np.nan}

    data_list = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_single, row) for _, row in df_sites_to_scan.iterrows()]
        for future in as_completed(futures):
            data_list.append(future.result())
            
    return pd.DataFrame(data_list)

def get_yoy_baseline(df_sites_to_scan, current_date_str):
    current_date = datetime.strptime(current_date_str, '%Y-%m-%d')
    ly_end = current_date.replace(year=current_date.year - 1)
    ly_start_str = (ly_end - timedelta(days=14)).strftime('%Y-%m-%d')
    ly_end_str = ly_end.strftime('%Y-%m-%d')
    vcom_start = f"{ly_start_str}T00:00:00Z"
    vcom_end = f"{ly_end_str}T23:59:59Z"
    
    def fetch_single(row):
        site_id = str(row['Site_ID'])
        portal = row.get('Portal', 'SolarEdge')
        
        if portal == 'SolarEdge':
            url = f"{BASE_URL}/site/{site_id}/energy?timeUnit=DAY&startDate={ly_start_str}&endDate={ly_end_str}&api_key={API_KEY}"
            try:
                res = global_http_session.get(url, timeout=10)
                if res.status_code == 200:
                    vals = [v['value'] for v in res.json().get('energy', {}).get('values', []) if v.get('value') is not None]
                    avg = sum(vals)/len(vals) if vals else np.nan
                    return {'Site_ID': site_id, 'LY_Avg_Energy_kWh': avg / 1000 if not np.isnan(avg) else np.nan}
            except:
                pass
            return {'Site_ID': site_id, 'LY_Avg_Energy_kWh': np.nan}
                
        elif portal == 'VCOM':
            auth, headers = get_vcom_auth(row['Account_Name'])
            site_avg_total = 0.0
            has_data = False
            if auth:
                inverters = get_vcom_inverters(site_id, auth, headers)
                for inv_id in inverters:
                    meas_url = f"{VCOM_BASE_URL}/systems/{site_id}/inverters/{inv_id}/abbreviations/E_DAY/measurements"
                    try:
                        meas_res = global_http_session.get(meas_url, auth=auth, headers=headers, params={"from": vcom_start, "to": vcom_end, "resolution": "day"}, timeout=10)
                        if meas_res.status_code == 200:
                            data = meas_res.json().get('data', {}).get(inv_id, {}).get('E_DAY', [])
                            vals = [d['value'] for d in data if d.get('value') is not None]
                            if vals:
                                inv_avg = sum(vals)/len(vals)
                                site_avg_total += inv_avg 
                                has_data = True
                    except:
                        pass
            return {'Site_ID': site_id, 'LY_Avg_Energy_kWh': site_avg_total if has_data else np.nan}
        return {'Site_ID': site_id, 'LY_Avg_Energy_kWh': np.nan}

    data_list = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_single, row) for _, row in df_sites_to_scan.iterrows()]
        for future in as_completed(futures):
            data_list.append(future.result())
            
    return pd.DataFrame(data_list)

def get_capacity_from_model(model_name):
    if not model_name: return 1.0 
    match = re.search(r'SE(\d+(?:\.\d+)?)', model_name, re.IGNORECASE)
    if match: return float(match.group(1))
    return 1.0 

def get_inverter_diagnosis(row, target_date):
    site_id = str(row['Site_ID'])
    portal = row.get('Portal', 'SolarEdge')
    
    if portal == 'VCOM':
        return "VCOM: AI Diagnosis in development"
        
    equip_url = f"{BASE_URL}/equipment/{site_id}/list?api_key={API_KEY}"
    try:
        res = global_http_session.get(equip_url, timeout=10)
        if res.status_code != 200: return "API Error"
        
        inverters = [eq for eq in res.json().get('reporters', {}).get('list', []) if 'inverter' in eq.get('name', '').lower() or eq.get('type', '') == 'Inverter']
        if not inverters: return "No inverters found"
            
        inverter_data = {}
        for inv in inverters:
            sn = inv.get('serialNumber')
            name = inv.get('name', sn)
            capacity_kw = get_capacity_from_model(inv.get('model', ''))
            
            data_url = f"{BASE_URL}/equipment/{site_id}/{sn}/data?startTime={target_date} 00:00:00&endTime={target_date} 23:59:59&api_key={API_KEY}"
            d_res = global_http_session.get(data_url, timeout=10)
            inv_energy = 0
            if d_res.status_code == 200:
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
    except:
        return "Diagnosis Failed"

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
    
    status_text.text(f"1/5: Fetching Daily Energy for {len(sites_to_scan)} sites (Turbo Mode)...")
    df_daily = get_daily_site_energy(sites_to_scan, target_date_str)
    progress_bar.progress(20)
    
    status_text.text("2/5: Fetching 7-Day Trend (Turbo Mode)...")
    df_7d = get_7_day_baseline(sites_to_scan, target_date_str)
    progress_bar.progress(40)
    
    status_text.text("3/5: Fetching YoY Baseline (Turbo Mode)...")
    df_yoy = get_yoy_baseline(sites_to_scan, target_date_str)
    progress_bar.progress(60)
    
    status_text.text("4/5: Running Geo-Clustering...")
    df_master = pd.merge(df_daily, df_sites, on='Site_ID', how='inner')
    df_master = pd.merge(df_master, df_yoy, on='Site_ID', how='left')
    df_master = pd.merge(df_master, df_7d, on='Site_ID', how='left')
    
    df_master['Energy_kWh'] = df_master['Energy_kWh'] / 1000
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
    
    status_text.text(f"5/5: Deep Scanning Inverters for {len(sites_to_deep_scan)} suspicious sites...")
    
    diagnoses_dict = {}
    total_sites_count = len(sites_to_deep_scan)
    
    for index, (i, row) in enumerate(sites_to_deep_scan.iterrows()):
        if total_sites_count > 0:
            progress_bar.progress(60 + int((index / total_sites_count) * 35))
        diagnoses_dict[row['Site_ID']] = get_inverter_diagnosis(row, target_date_str)
        
    df_master['System_Diagnosis'] = df_master['Site_ID'].map(diagnoses_dict).fillna("Skipped (Site Optimal - No Drops Detected)")
    
    df_master['Alert_Status'] = np.where(
        (df_master['Performance_vs_Cluster'] < 0.80) | 
        (df_master['7D_Change_%'] < -10.0) | 
        (df_master['YoY_Change_%'] < -20.0) |
        (df_master['System_Diagnosis'].str.contains('0 kWh|Low Output|Offline|Faults', na=False, regex=True)), 
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
            if 'Offline' in str(s.get('AI Diagnosis', '')): 
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
