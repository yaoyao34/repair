
import streamlit as st
import pandas as pd
import requests
from datetime import datetime
import time
import base64
import re

# --- gspread / google auth ---
import gspread
from google.oauth2.service_account import Credentials

def norm_pwd(x) -> str:
    """密碼正規化：去除全/半形空白、換行、零寬字元"""
    if x is None:
        return ""
    s = str(x)
    s = s.replace("\u3000", " ")          # 全形空白
    s = re.sub(r"[\u200b-\u200d\ufeff]", "", s)  # 零寬字元
    s = s.strip()
    return s

# --- 1. 全域變數與設定 ---
LINE_ACCESS_TOKEN = st.secrets.get("LINE_ACCESS_TOKEN", "")
GROUP_ID = st.secrets.get("GROUP_ID", "")
SHEET_URL = st.secrets.get("SHEET_URL")
if not SHEET_URL:
    st.error("找不到 SHEET_URL：請確認 Streamlit secrets 內有設定 SHEET_URL。")
    st.stop()


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
    """使用 gspread 讀取資料（只抓指定欄位，避免空白表頭造成 duplicates）"""
    try:
        spreadsheet = gspread_client.open_by_url(SHEET_URL)

        # --- 報修資料 ---
        report_sheet = spreadsheet.worksheet(REPORT_SHEET)
        report_expected = [
            "時間戳記", "電子郵件地址", "稱謂", "報修者姓名", "班級地點",
            "損壞設備", "損壞情形描述", "照片或影片", "案件編號"
        ]
        report_data = pd.DataFrame(
            report_sheet.get_all_records(expected_headers=report_expected)
        )

        # --- 維修紀錄 ---
        repair_sheet = spreadsheet.worksheet(REPAIR_SHEET)
        repair_expected = ["時間戳記", "案件編號", "處理進度", "維修說明", "維修照片及影片"]
        repair_data = pd.DataFrame(
            repair_sheet.get_all_records(expected_headers=repair_expected)
        )

        # --- 密碼 ---
        password_sheet = spreadsheet.worksheet(PASSWORD_SHEET)
        raw_pwd = password_sheet.acell("A1").value
        correct_password = norm_pwd(raw_pwd)


        # 空表保護（欄位用你實際的表頭）
        if report_data.empty:
            report_data = pd.DataFrame(columns=report_expected)
        if repair_data.empty:
            repair_data = pd.DataFrame(columns=repair_expected)

        return report_data, repair_data, correct_password

    except gspread.exceptions.WorksheetNotFound:
        st.error(f"工作表找不到：請檢查分頁名稱是否為 '{REPORT_SHEET}', '{REPAIR_SHEET}', '{PASSWORD_SHEET}'")
        st.stop()
    except Exception as e:
        st.error(f"資料讀取失敗 (load_data)：{e}")
        st.stop()

def build_merged_view(report_df: pd.DataFrame, repair_df: pd.DataFrame) -> pd.DataFrame:
    # 欄位轉字串，避免 merge 失敗
    for df in (report_df, repair_df):
        if "案件編號" in df.columns:
            df["案件編號"] = df["案件編號"].astype(str).str.strip()

    # 報修：只留你要的欄位
    r = report_df.copy()
    r["報修日期"] = pd.to_datetime(r["時間戳記"], errors="coerce").dt.strftime("%Y-%m-%d")
    r = r[["案件編號", "報修日期", "班級地點", "損壞設備", "損壞情形描述", "照片或影片"]]

    # 維修：同一案件可能多筆 -> 取最新一筆
    w = repair_df.copy()
    w["_ts"] = pd.to_datetime(w["時間戳記"], errors="coerce")
    w = w.sort_values("_ts").groupby("案件編號", as_index=False).tail(1)
    w = w[["案件編號", "處理進度", "維修說明", "維修照片及影片"]]

    # 合併
    merged = r.merge(w, on="案件編號", how="left")
    return merged



def append_repair_record(record: dict) -> bool:
    """將維修紀錄寫入 Google Sheets（依維修紀錄表的實際欄位順序）"""
    try:
        spreadsheet = gspread_client.open_by_url(SHEET_URL)
        sheet = spreadsheet.worksheet(REPAIR_SHEET)

        fields = ["時間戳記", "案件編號", "處理進度", "維修說明", "維修照片及影片"]
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
        pwd_in = st.text_input("密碼", type="password")
        pwd_in = norm_pwd(pwd_in)
    
        # A1 空白 -> 不需要登入
        if correct_password == "":
            authed = True
            st.info("密碼設定!A1 為空，目前不需要登入。")
        else:
            authed = (pwd_in == correct_password)
    
        # 診斷（不顯示密碼本身）
        st.caption(f"密碼長度：A1={len(correct_password)}、輸入={len(pwd_in)}")


    merged = build_merged_view(report_data, repair_data)
    st.subheader("案件總覽（報修 + 維修）")
    st.dataframe(merged, use_container_width=True)

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

if __name__ == "__main__":
    main()







