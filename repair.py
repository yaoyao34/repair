# repair.py (修改第 3 行之後的內容)

import streamlit as st
import pandas as pd
import requests
from datetime import datetime
import time

# --- 新增 gspread 相關的匯入 ---
import gspread
import json
# 確保您有這行： from google.oauth2.service_account import Credentials
from google.oauth2.service_account import Credentials 
# --- 新增 gspread 相關的匯入 ---

# --- 1. 全域變數與設定 ---
# ... (LINE_ACCESS_TOKEN, GROUP_ID, SHEET_URL 等保留) ...

# 工作表名稱
REPORT_SHEET = "報修資料"
REPAIR_SHEET = "維修紀錄"
PASSWORD_SHEET = "密碼設定"

# --- 2. 核心函式 ---

@st.cache_resource(ttl=None) 
def get_gspread_client():
    """使用服務帳號憑證連接 Google Sheets API"""
    try:
        # 從 secrets.toml 讀取 [gsheets] 區段
        credentials_dict = dict(st.secrets["gsheets"])
        
        # 建立 gspread 憑證物件
        creds = Credentials.from_service_account_info(
            credentials_dict,
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        # 建立 gspread client
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        st.error(f"Gspread 連線失敗，請檢查 secrets.toml 中的 [gsheets] 憑證格式和內容是否正確。錯誤: {e}")
        st.stop()

# 初始化客戶端
gspread_client = get_gspread_client()

@st.cache_data(ttl=600)
def load_data():
    """使用 gspread 讀取資料"""
    try:
        spreadsheet = gspread_client.open_by_url(SHEET_URL)
        
        # 讀取報修資料
        report_sheet = spreadsheet.worksheet(REPORT_SHEET)
        report_data = pd.DataFrame(report_sheet.get_all_records())

        # 讀取維修紀錄
        repair_sheet = spreadsheet.worksheet(REPAIR_SHEET)
        repair_data = pd.DataFrame(repair_sheet.get_all_records())
        
        # 讀取密碼 (假設密碼設定表只有一個密碼在 A1)
        password_sheet = spreadsheet.worksheet(PASSWORD_SHEET)
        correct_password = password_sheet.acell('A1').value.strip()
        
        # 確保資料框非空 (若為空，建立空DF，避免後續程式碼錯誤)
        if report_data.empty: report_data = pd.DataFrame(columns=['案件編號', '地點', '損壞設備'])
        if repair_data.empty: repair_data = pd.DataFrame(columns=['案件編號', '處理進度', '維修說明', '更新時間'])

        return report_data, repair_data, correct_password
        
    except gspread.exceptions.WorksheetNotFound as wnf_e:
        st.error(f"工作表找不到錯誤：請檢查您的工作表名稱是否為 '{REPORT_SHEET}', '{REPAIR_SHEET}', '{PASSWORD_SHEET}'。")
        st.stop()
    except Exception as e:
        st.error(f"資料讀取失敗 (load_data)：{e}")
        st.stop()

# 修改 append_repair_record 函式以使用 gspread
def append_repair_record(record):
    """將維修紀錄寫入 Google Sheets"""
    try:
        spreadsheet = gspread_client.open_by_url(SHEET_URL)
        repair_sheet = spreadsheet.worksheet(REPAIR_SHEET)
        
        # 將字典的值作為列表追加到 Sheets 中，確保順序與 Sheets 欄位一致
        repair_sheet.append_row(
            list(record.values()), 
            value_input_option='USER_ENTERED'
        )
        return True
    except Exception as e:
        st.error(f"寫入維修紀錄失敗 (Gspread): {e}")
        return False

# ... (主程式 main() 保持不變) ...
