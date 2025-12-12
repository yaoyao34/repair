import streamlit as st
import pandas as pd
import requests
from datetime import datetime
import re

# --- gspread / google auth ---
import gspread
from google.oauth2.service_account import Credentials


# =========================
# Utils
# =========================
def norm_pwd(x) -> str:
    """密碼正規化：去除全/半形空白、換行、零寬字元"""
    if x is None:
        return ""
    s = str(x)
    s = s.replace("\u3000", " ")                 # 全形空白
    s = re.sub(r"[\u200b-\u200d\ufeff]", "", s)  # 零寬字元
    return s.strip()


def to_ymd(ts) -> str:
    """把各種 timestamp 轉成 YYYY-MM-DD（失敗回空字串）"""
    d = pd.to_datetime(ts, errors="coerce")
    if pd.isna(d):
        return ""
    return d.strftime("%Y-%m-%d")


def split_links(cell: str) -> list[str]:
    """把逗號分隔的連結切成清單"""
    if not cell:
        return []
    return [p.strip() for p in str(cell).split(",") if p.strip()]


# =========================
# Global settings
# =========================
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


# =========================
# LINE notify (optional)
# =========================
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


# =========================
# Google Sheets client
# =========================
@st.cache_resource(ttl=None)
def get_gspread_client():
    """使用 secrets.toml 的 [google_service_account] 連接 Google Sheets API"""
    try:
        credentials_dict = dict(st.secrets["google_service_account"])
        creds = Credentials.from_service_account_info(
            credentials_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return gspread.authorize(creds)
    except Exception as e:
        st.error(f"Gspread 連線失敗：{e}")
        st.stop()


gspread_client = get_gspread_client()


# =========================
# Data load / merge / write
# =========================
@st.cache_data(ttl=600)
def load_data():
    """只抓指定欄位，避免空白表頭造成 duplicates"""
    try:
        spreadsheet = gspread_client.open_by_url(SHEET_URL)

        # --- 報修資料 ---
        report_sheet = spreadsheet.worksheet(REPORT_SHEET)
        report_expected = [
            "時間戳記",
            "電子郵件地址",
            "稱謂",
            "報修者姓名",
            "班級地點",
            "損壞設備",
            "損壞情形描述",
            "照片或影片",
            "案件編號",
        ]
        report_data = pd.DataFrame(report_sheet.get_all_records(expected_headers=report_expected))

        # --- 維修紀錄 ---
        repair_sheet = spreadsheet.worksheet(REPAIR_SHEET)
        repair_expected = ["時間戳記", "案件編號", "處理進度", "維修說明", "維修照片及影片"]
        repair_data = pd.DataFrame(repair_sheet.get_all_records(expected_headers=repair_expected))

        # --- 密碼 ---
        password_sheet = spreadsheet.worksheet(PASSWORD_SHEET)
        raw_pwd = password_sheet.acell("A1").value
        correct_password = norm_pwd(raw_pwd)

        # 空表保護
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
    """用 案件編號 合併：同案件多筆維修取最新一筆；報修日期只取 YYYY-MM-DD"""
    r = report_df.copy()
    w = repair_df.copy()

    if "案件編號" in r.columns:
        r["案件編號"] = r["案件編號"].astype(str).str.strip()
    if "案件編號" in w.columns:
        w["案件編號"] = w["案件編號"].astype(str).str.strip()

    r["報修日期"] = r["時間戳記"].apply(to_ymd)
    r = r[["案件編號", "報修日期", "班級地點", "損壞設備", "損壞情形描述", "照片或影片"]]

    w["_ts"] = pd.to_datetime(w["時間戳記"], errors="coerce")
    w = w.sort_values("_ts").groupby("案件編號", as_index=False).tail(1)
    w = w[["案件編號", "處理進度", "維修說明", "維修照片及影片"]]

    merged = r.merge(w, on="案件編號", how="left")
    return merged


def append_repair_record(record: dict) -> bool:
    """依維修紀錄表欄位順序寫入"""
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


# =========================
# App
# =========================
def main():
    st.title("報修 / 維修系統")

    report_data, repair_data, correct_password = load_data()

    # --- 登入 ---
    with st.sidebar:
        st.subheader("管理登入")
        pwd_in = norm_pwd(st.text_input("密碼", type="password"))

        if correct_password == "":
            authed = True
            st.info("密碼設定!A1 為空，目前不需要登入。")
        else:
            authed = (pwd_in == correct_password)

        st.caption(f"密碼長度：A1={len(correct_password)}、輸入={len(pwd_in)}")

    merged = build_merged_view(report_data, repair_data)

    # 顯示用：不顯示案件編號
    show_cols = [
        "報修日期",
        "班級地點",
        "損壞設備",
        "損壞情形描述",
        "照片或影片",
        "處理進度",
        "維修說明",
        "維修照片及影片",
    ]
    st.subheader("案件總覽（報修 + 維修）")
    st.dataframe(merged[show_cols], use_container_width=True)

    # 連結預覽（避免直接顯示圖片/影片造成權限與效能問題；需要再加 st.image/st.video）
    with st.expander("連結預覽（報修/維修照片影片）", expanded=False):
        for _, row in merged.iterrows():
            title = f"{row.get('報修日期','')}｜{row.get('班級地點','')}｜{row.get('損壞設備','')}"
            with st.expander(title, expanded=False):
                st.write("報修照片/影片：")
                for link in split_links(row.get("照片或影片", "")):
                    st.write(link)
                st.write("維修照片/影片：")
                for link in split_links(row.get("維修照片及影片", "")):
                    st.write(link)

    if not authed:
        st.warning("密碼錯誤，無法新增維修紀錄。")
        return

    st.divider()
    st.subheader("新增維修紀錄")

    # 下拉：顯示「報修日期｜班級地點｜損壞設備」，不露案件編號；但實際 value 用案件編號
    r = report_data.copy()
    if r.empty or "案件編號" not in r.columns:
        st.error("報修資料沒有可用的案件（缺少 案件編號）。")
        return

    r["案件編號"] = r["案件編號"].astype(str).str.strip()
    r["報修日期"] = r["時間戳記"].apply(to_ymd)
    r["顯示"] = r["報修日期"].astype(str) + "｜" + r["班級地點"].astype(str) + "｜" + r["損壞設備"].astype(str)

    options = r[["顯示", "案件編號"]].dropna().drop_duplicates()
    if options.empty:
        st.error("報修資料沒有可用的案件（顯示欄位不足）。")
        return

    display_to_id = dict(zip(options["顯示"].tolist(), options["案件編號"].tolist()))

    col1, col2 = st.columns(2)
    with col1:
        choice = st.selectbox("選擇報修案件", options=list(display_to_id.keys()))
        case_id = display_to_id[choice]  # 內部使用
    with col2:
        progress = st.selectbox("處理進度", options=["已接單", "處理中", "待料", "已完成", "退回/無法處理"])

    note = st.text_area("維修說明", height=120)

    repair_links = st.text_input(
        "維修照片/影片連結（可多個，用逗號分隔）",
        placeholder="https://drive.google.com/..., https://..."
    )

    if st.button("送出維修紀錄", type="primary"):
        if not case_id.strip():
            st.error("案件選擇異常（案件編號空白）。")
            return

        record = {
            "時間戳記": datetime.now().strftime("%Y-%m-%d"),  # 只存年月日
            "案件編號": case_id.strip(),
            "處理進度": progress.strip(),
            "維修說明": note.strip(),
            "維修照片及影片": repair_links.strip(),
        }

        ok = append_repair_record(record)
        if ok:
            st.success("已寫入維修紀錄。")
            st.cache_data.clear()

            # 可選：LINE 通知
            line_notify(f"維修更新\n{choice}\n進度：{record['處理進度']}\n日期：{record['時間戳記']}")

            st.rerun()


if __name__ == "__main__":
    main()
