import streamlit as st
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import re

# --- הגדרות האפליקציה ---
st.set_page_config(page_title="Solar Monitor - Daily Hit List", page_icon="☀️", layout="wide")

API_KEY = "K0X7PD9WAJ11B33DM7BUWNY6VCJ9YVFS"
BASE_URL = "https://monitoringapi.solaredge.com"
default_yesterday = (datetime.now() - timedelta(1)).strftime('%Y-%m-%d')

# --- פונקציות מנוע החוקים ---
@st.cache_data(ttl=3600)
def load_metadata():
    try:
        df = pd.read_csv('sites_metadata.csv')
        df = df[~df['Name'].str.contains('DELETED|DISABLED|\*', na=False, case=False)]
        df = df.dropna(subset=['Latitude', 'Longitude'])
        df = df[df['Capacity_kWp'] > 0]
        return df
    except Exception:
        st.error("Error loading sites_metadata.csv. Make sure the file exists in the directory.")
        return pd.DataFrame()

def get_daily_site_energy(site_ids, target_date):
    energy_data = []
    for site_id in site_ids:
        url = f"{BASE_URL}/site/{site_id}/energy?timeUnit=DAY&startDate={target_date}&endDate={target_date}&api_key={API_KEY}"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                val = res.json()['energy']['values'][0]['value']
                energy_data.append({'Site_ID': site_id, 'Energy_kWh': 0 if val is None else val})
        except:
            pass
    return pd.DataFrame(energy_data)

def get_7_day_baseline(site_ids, target_date_str):
    target_date = datetime.strptime(target_date_str, '%Y-%m-%d')
    start_date_str = (target_date - timedelta(days=7)).strftime('%Y-%m-%d')
    data_list = []
    for site_id in site_ids:
        url = f"{BASE_URL}/site/{site_id}/energy?timeUnit=DAY&startDate={start_date_str}&endDate={target_date_str}&api_key={API_KEY}"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                vals = [v['value'] for v in res.json().get('energy', {}).get('values', []) if v.get('value') is not None]
                avg = sum(vals)/len(vals) if vals else np.nan
                data_list.append({'Site_ID': site_id, '7D_Avg_Energy_kWh': avg / 1000 if not np.isnan(avg) else np.nan})
        except:
            data_list.append({'Site_ID': site_id, '7D_Avg_Energy_kWh': np.nan})
    return pd.DataFrame(data_list)

def get_yoy_baseline(site_ids, current_date_str):
    current_date = datetime.strptime(current_date_str, '%Y-%m-%d')
    ly_end = current_date.replace(year=current_date.year - 1)
    ly_start_str = (ly_end - timedelta(days=14)).strftime('%Y-%m-%d')
    ly_end_str = ly_end.strftime('%Y-%m-%d')
    data_list = []
    for site_id in site_ids:
        url = f"{BASE_URL}/site/{site_id}/energy?timeUnit=DAY&startDate={ly_start_str}&endDate={ly_end_str}&api_key={API_KEY}"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                vals = [v['value'] for v in res.json().get('energy', {}).get('values', []) if v.get('value') is not None]
                avg = sum(vals)/len(vals) if vals else np.nan
                data_list.append({'Site_ID': site_id, 'LY_Avg_Energy_kWh': avg / 1000 if not np.isnan(avg) else np.nan})
        except:
            data_list.append({'Site_ID': site_id, 'LY_Avg_Energy_kWh': np.nan})
    return pd.DataFrame(data_list)

def get_capacity_from_model(model_name):
    """ פונקציית עזר לחילוץ גודל הממיר מתוך שם המודל """
    if not model_name:
        return 1.0 
    match = re.search(r'SE(\d+(?:\.\d+)?)', model_name, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return 1.0 

def get_inverter_diagnosis(site_id, target_date):
    equip_url = f"{BASE_URL}/equipment/{site_id}/list?api_key={API_KEY}"
    try:
        res = requests.get(equip_url, timeout=10)
        if res.status_code != 200: return "API Error"
        
        inverters = [eq for eq in res.json().get('reporters', {}).get('list', []) if 'inverter' in eq.get('name', '').lower() or eq.get('type', '') == 'Inverter']
        if not inverters: return "No inverters found"
            
        inverter_data = {}
        for inv in inverters:
            sn = inv.get('serialNumber')
            name = inv.get('name', sn)
            model = inv.get('model', '')
            capacity_kw = get_capacity_from_model(model)
            
            data_url = f"{BASE_URL}/equipment/{site_id}/{sn}/data?startTime={target_date} 00:00:00&endTime={target_date} 23:59:59&api_key={API_KEY}"
            d_res = requests.get(data_url, timeout=10)
            inv_energy = 0
            if d_res.status_code == 200:
                energies = [t.get('totalEnergy') for t in d_res.json().get('data', {}).get('telemetries', []) if t.get('totalEnergy') is not None]
                if energies: inv_energy = (max(energies) - min(energies)) / 1000
            
            specific_yield = inv_energy / capacity_kw if capacity_kw > 0 else 0
            
            inverter_data[name] = {
                'energy': inv_energy,
                'capacity': capacity_kw,
                'specific_yield': specific_yield
            }
            time.sleep(0.1) 
            
        if not any(d['energy'] > 0 for d in inverter_data.values()): 
            return "Site Offline: All inverters at 0 kWh"
            
        max_specific_yield = max([d['specific_yield'] for d in inverter_data.values()])
        
        faults = []
        for name, data in inverter_data.items():
            if data['energy'] == 0:
                faults.append(f"{name}: 0 kWh")
            elif data['specific_yield'] < (max_specific_yield * 0.75): 
                faults.append(f"{name}: Low Output ({data['energy']:.1f} kWh)")
                
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
    
    total_sites = len(df_sites)
    
    # הוספת אפשרות לסירוק מלא
    scan_all = st.checkbox(f"Scan All Sites (Production Mode: {total_sites} sites)")
    
    if scan_all:
        scan_limit = total_sites
    else:
        scan_limit = st.number_input(f"Number of Sites to Scan (Max: {total_sites})", min_value=1, max_value=total_sites, value=20, step=10)
        
    run_button = st.button("🚀 Run Anomaly Detection", use_container_width=True, type="primary")

if run_button:
    if df_sites.empty:
        st.stop()
        
    test_site_ids = df_sites['Site_ID'].head(int(scan_limit)).tolist()
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    status_text.text("1/5: Fetching Daily Energy...")
    df_daily = get_daily_site_energy(test_site_ids, target_date_str)
    progress_bar.progress(20)
    
    status_text.text("2/5: Fetching 7-Day Trend...")
    df_7d = get_7_day_baseline(test_site_ids, target_date_str)
    progress_bar.progress(40)
    
    status_text.text("3/5: Fetching YoY Baseline...")
    df_yoy = get_yoy_baseline(test_site_ids, target_date_str)
    progress_bar.progress(60)
    
    status_text.text("4/5: Running Geo-Clustering Rule Engine...")
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
    cluster_stats = df_master.groupby(['Lat_Grid', 'Lon_Grid'])['Specific_Yield'].median().reset_index()
    cluster_stats.rename(columns={'Specific_Yield': 'Cluster_Median_Yield'}, inplace=True)
    df_master = pd.merge(df_master, cluster_stats, on=['Lat_Grid', 'Lon_Grid'], how='left')
    
    df_master['Performance_vs_Cluster'] = df_master['Specific_Yield'] / df_master['Cluster_Median_Yield']
    
    df_master['Alert_Status'] = np.where(
        (df_master['Performance_vs_Cluster'] < 0.80) | (df_master['7D_Change_%'] < -20.0), 
        '🔴 Fault Suspected', 
        '🟢 OK'
    )
    progress_bar.progress(80)
    
    anomalies = df_master[df_master['Alert_Status'] == '🔴 Fault Suspected'].copy()
    
    status_text.text(f"5/5: Drilling down into Inverter data for {len(anomalies)} suspected sites...")
    diagnoses = []
    for index, row in anomalies.iterrows():
        diagnoses.append(get_inverter_diagnosis(row['Site_ID'], target_date_str))
    if not anomalies.empty:
        anomalies['System_Diagnosis'] = diagnoses
    progress_bar.progress(100)
    status_text.text("Scan Complete!")
    time.sleep(1)
    status_text.empty()
    progress_bar.empty()
    
    # --- הצגת התוצאות ---
    st.divider()
    col1, col2, col3 = st.columns(3)
    col1.metric("Sites Scanned", len(df_master))
    col2.metric("Anomalies Detected", len(anomalies), delta_color="inverse")
    col3.metric("Clean Sites", len(df_master) - len(anomalies))
    
    if anomalies.empty:
        st.success("All monitored sites are performing well! No anomalies detected.")
    else:
        st.subheader("🚨 Priority Hit List")
        
        display_df = anomalies[['Name', 'City', 'Specific_Yield', 'Cluster_Median_Yield', '7D_Change_%', 'YoY_Change_%', 'System_Diagnosis']].copy()
        
        display_df['7D_Change_%'] = display_df['7D_Change_%'].apply(lambda x: f"{x:.1f}%" if pd.notnull(x) else "-")
        display_df['YoY_Change_%'] = display_df['YoY_Change_%'].apply(lambda x: f"{x:.1f}%" if pd.notnull(x) else "-")
        display_df.fillna("-", inplace=True)
        
        display_df.rename(columns={
            'Specific_Yield': 'Yield (kWh/kWp)',
            'Cluster_Median_Yield': 'Area Median (kWh/kWp)',
            '7D_Change_%': '7-Day Trend',
            'YoY_Change_%': 'YoY Trend',
            'System_Diagnosis': 'AI Diagnosis'
        }, inplace=True)
        
        def highlight_offline(s):
            if 'Offline' in str(s): 
                return ['background-color: #4a1c1c; color: #ffcccc'] * len(s)
            return [''] * len(s)
            
        st.dataframe(
            display_df.style
            .format({
                'Yield (kWh/kWp)': '{:.2f}',
                'Area Median (kWh/kWp)': '{:.2f}'
            })
            .apply(highlight_offline, axis=1, subset=['AI Diagnosis']),
            use_container_width=True,
            hide_index=True,
            height=400
        )
