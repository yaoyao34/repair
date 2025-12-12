import streamlit as st
import pandas as pd
import requests
from datetime import datetime
import time
import base64

# --- gspread / google auth ---
import gspread
from google.oauth2.service_account import Credentials

# --- 1. 全域變數與設定 ---
LINE_ACCESS_TOKEN = st.secrets.get("LINE_ACCESS_TOKEN", "")
GROUP_ID = st.secrets.get("GROUP_ID", "")
SHEET_URL = st.secrets["SHEET_URL"]

# 工作表名稱
REPORT_SHEET = "報修資料"
REPAIR_SHEET = "維修紀錄"
PASSWORD_SHEET = "密碼設定"


def line_notify(message: str) -> bool:
    """LINE Notify 推播（若未設定 token 則略過）"""
    if not LINE_ACCESS_TOKEN:
        return False
    try:
        headers = {"Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
        payload = {"message": message}
        r = requests.post("https://notify-api.line.me/api/notify", headers=headers, data=payload, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


@st.cache_resource(ttl=None)
def get_gspread_client():
    """使用 secrets.toml 的 [google_service_account] 連接 Google Sheets API（不使用 Base64）"""
    try:
        credentials_dict = dict(st.secrets["google_service_account"])
        creds = Credentials.from_service_account_info(
            credentials_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        return gspread.authorize(creds)
    except Exception as e:
        st.error(f"Gspread 連線失敗：{e}")
        st.stop()


gspread_client = get_gspread_client()


@st.cache_data(ttl=600)
def load_data():
    """使用 gspread 讀取資料"""
    try:
        spreadsheet = gspread_client.open_by_url(SHEET_URL)

        # 報修資料
        report_sheet = spreadsheet.worksheet(REPORT_SHEET)
        report_data = pd.DataFrame(report_sheet.get_all_records())

        # 維修紀錄
        repair_sheet = spreadsheet.worksheet(REPAIR_SHEET)
        repair_data = pd.DataFrame(repair_sheet.get_all_records())

        # 密碼
        password_sheet = spreadsheet.worksheet(PASSWORD_SHEET)
        correct_password = (password_sheet.acell("A1").value or "").strip()

        # 空表保護
        if report_data.empty:
            report_data = pd.DataFrame(columns=["案件編號", "地點", "損壞設備"])
        if repair_data.empty:
            repair_data = pd.DataFrame(columns=["案件編號", "處理進度", "維修說明", "更新時間"])

        return report_data, repair_data, correct_password

    except gspread.exceptions.WorksheetNotFound:
        st.error(
            f"工作表找不到：請檢查工作表名稱是否正確："
            f"'{REPORT_SHEET}', '{REPAIR_SHEET}', '{PASSWORD_SHEET}'"
        )
        st.stop()
    except Exception as e:
        st.error(f"資料讀取失敗 (load_data)：{e}")
        st.stop()


def append_repair_record(record: dict) -> bool:
    """將維修紀錄寫入 Google Sheets（依欄位順序寫入，避免 dict 順序錯位）"""
    try:
        spreadsheet = gspread_client.open_by_url(SHEET_URL)
        sheet = spreadsheet.worksheet(REPAIR_SHEET)

        # 建議固定欄位順序（你表格欄位若不同，請照你的表頭調整）
        fields = ["案件編號", "處理進度", "維修說明", "更新時間"]
        row = [record.get(k, "") for k in fields]

        sheet.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        st.error(f"寫入維修紀錄失敗 (Gspread)：{e}")
        return False


def main():
    st.title("報修 / 維修系統")

    report_data, repair_data, correct_password = load_data()

    # --- 簡單登入 ---
    with st.sidebar:
        st.subheader("管理登入")
        pwd = st.text_input("密碼", type="password")
        authed = (pwd == correct_password) if correct_password else True
        if not correct_password:
            st.info("未設定密碼（密碼設定!A1 為空），目前不需要登入。")

    st.subheader("報修資料")
    st.dataframe(report_data, use_container_width=True)

    st.subheader("維修紀錄")
    st.dataframe(repair_data, use_container_width=True)

    if not authed:
        st.warning("密碼錯誤，無法新增維修紀錄。")
        return

    st.divider()
    st.subheader("新增維修紀錄")

    # 若你報修資料有案件編號可選，優先用下拉
    # 若你報修資料有案件編號可選，優先用下拉
    case_list = []
    if ("案件編號" in report_data.columns) and (not report_data.empty):
        s = report_data["案件編號"].dropna()
        if not s.empty:
            case_list = s.astype(str).unique().tolist()

    col1, col2 = st.columns(2)
    with col1:
        if case_list:
            case_id = st.selectbox("案件編號", options=case_list)
        else:
            case_id = st.text_input("案件編號")
    with col2:
        progress = st.selectbox("處理進度", options=["已接單", "處理中", "待料", "已完成", "退回/無法處理"])

