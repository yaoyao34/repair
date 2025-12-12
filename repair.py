import streamlit as st
import pandas as pd
import requests
from datetime import datetime
import re

import gspread
from google.oauth2.service_account import Credentials


# =========================
# Utils
# =========================
def norm_pwd(x) -> str:
    if x is None:
        return ""
    s = str(x)
    s = s.replace("\u3000", " ")
    s = re.sub(r"[\u200b-\u200d\ufeff]", "", s)
    return s.strip()


def to_ymd(ts) -> str:
    d = pd.to_datetime(ts, errors="coerce")
    if pd.isna(d):
        return ""
    return d.strftime("%Y-%m-%d")


# =========================
# Global settings
# =========================
LINE_ACCESS_TOKEN = st.secrets.get("LINE_ACCESS_TOKEN", "")
GROUP_ID = st.secrets.get("GROUP_ID", "")

SHEET_URL = st.secrets.get("SHEET_URL")
if not SHEET_URL:
    st.error("找不到 SHEET_URL：請確認 Streamlit secrets 內有設定 SHEET_URL。")
    st.stop()

REPORT_SHEET = "報修資料"
REPAIR_SHEET = "維修紀錄"
PASSWORD_SHEET = "密碼設定"


def line_notify(message: str) -> bool:
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


@st.cache_data(ttl=600)
def load_data():
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
        # 注意：工作表仍可能有「維修照片及影片」欄位，但此版不使用它
        repair_sheet = spreadsheet.worksheet(REPAIR_SHEET)
        repair_expected = ["時間戳記", "案件編號", "處理進度", "維修說明", "維修照片及影片"]
        repair_data = pd.DataFrame(repair_sheet.get_all_records(expected_headers=repair_expected))

        # --- 密碼 ---
        password_sheet = spreadsheet.worksheet(PASSWORD_SHEET)
        raw_pwd = password_sheet.acell("A1").value
        correct_password = norm_pwd(raw_pwd)

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
    r = report_df.copy()
    w = repair_df.copy()

    if "案件編號" in r.columns:
        r["案件編號"] = r["案件編號"].astype(str).str.strip()
    if "案件編號" in w.columns:
        w["案件編號"] = w["案件編號"].astype(str).str.strip()

    # 報修日期：只取 YYYY-MM-DD
    r["報修日期"] = r["時間戳記"].apply(to_ymd)
    r = r[["案件編號", "報修日期", "班級地點", "損壞設備", "損壞情形描述", "照片或影片"]]

    # 維修：同案件多筆取最新
    w["_ts"] = pd.to_datetime(w["時間戳記"], errors="coerce")
    w = w.sort_values("_ts").groupby("案件編號", as_index=False).tail(1)

    # 只保留你要編修的欄位（不含維修照片）
    if "處理進度" not in w.columns:
        w["處理進度"] = ""
    if "維修說明" not in w.columns:
        w["維修說明"] = ""
    w = w[["案件編號", "處理進度", "維修說明"]]

    merged = r.merge(w, on="案件編號", how="left")
    merged["處理進度"] = merged["處理進度"].fillna("")
    merged["維修說明"] = merged["維修說明"].fillna("")
    return merged


def update_latest_repair(case_id: str, progress: str, note: str) -> bool:
    """
    更新「維修紀錄」中該案件的最新一筆（以最後出現的列為準）。
    若該案件不存在任何維修紀錄，則新增一筆。
    """
    try:
        spreadsheet = gspread_client.open_by_url(SHEET_URL)
        ws = spreadsheet.worksheet(REPAIR_SHEET)

        # 取整張表（第1列是表頭）
        values = ws.get_all_values()
        if not values or len(values) < 1:
            st.error("維修紀錄工作表是空的或讀取失敗。")
            return False

        header = values[0]
        # 欄位索引
        def col_idx(name: str):
            return header.index(name)

        required = ["時間戳記", "案件編號", "處理進度", "維修說明"]
        for k in required:
            if k not in header:
                st.error(f"維修紀錄缺少欄位：{k}")
                return False

        idx_ts = col_idx("時間戳記")
        idx_case = col_idx("案件編號")
        idx_prog = col_idx("處理進度")
        idx_note = col_idx("維修說明")

        # 找該案件最後一筆（資料列從第2列開始）
        last_row_number = None
        for i in range(1, len(values)):
            row = values[i]
            if len(row) <= idx_case:
                continue
            if str(row[idx_case]).strip() == case_id:
                last_row_number = i + 1  # 轉成 worksheet row number（1-based）

        today = datetime.now().strftime("%Y-%m-%d")

        if last_row_number is None:
            # 新增一筆（維修照片欄位若存在就留空）
            out = [""] * len(header)
            out[idx_ts] = today
            out[idx_case] = case_id
            out[idx_prog] = progress
            out[idx_note] = note
            ws.append_row(out, value_input_option="USER_ENTERED")
        else:
            # 更新最後一筆
            ws.update_cell(last_row_number, idx_ts + 1, today)
            ws.update_cell(last_row_number, idx_prog + 1, progress)
            ws.update_cell(last_row_number, idx_note + 1, note)

        return True

    except Exception as e:
        st.error(f"寫回維修紀錄失敗：{e}")
        return False


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

    # 顯示/編修用：把案件編號放在 index（並隱藏 index），UI 不顯示案件編號
    editor_df = merged.copy()
    editor_df = editor_df.set_index("案件編號")
    st.subheader("案件總覽（可直接編修：處理進度 / 維修說明）")

    edited = st.data_editor(
        editor_df[["報修日期", "班級地點", "損壞設備", "損壞情形描述", "照片或影片", "處理進度", "維修說明"]],
        hide_index=True,
        use_container_width=True,
        disabled=["報修日期", "班級地點", "損壞設備", "損壞情形描述", "照片或影片"] if not authed else
                 ["報修日期", "班級地點", "損壞設備", "損壞情形描述", "照片或影片"],
        column_config={
            "處理進度": st.column_config.SelectboxColumn(
                "處理進度",
                options=["", "已接單", "處理中", "待料", "已完成", "退回/無法處理"],
            ),
            "維修說明": st.column_config.TextColumn("維修說明"),
        },
        key="editor",
    )

    if not authed:
        st.warning("密碼錯誤：目前只能查看，無法儲存編修。")
        return

    st.divider()

    # 儲存：比對原始 merged 與 edited，找出變更
    if st.button("儲存變更", type="primary"):
        original = editor_df[["處理進度", "維修說明"]].copy()
        current = edited[["處理進度", "維修說明"]].copy()

        # 以 index（案件編號）對齊
        original = original.fillna("")
        current = current.fillna("")

        changed_cases = []
        for case_id in current.index:
            if case_id not in original.index:
                continue
            if (str(current.loc[case_id, "處理進度"]) != str(original.loc[case_id, "處理進度"])) or \
               (str(current.loc[case_id, "維修說明"]) != str(original.loc[case_id, "維修說明"])):
                changed_cases.append(case_id)

        if not changed_cases:
            st.info("沒有任何變更。")
            return

        ok_cnt = 0
        for case_id in changed_cases:
            p = str(current.loc[case_id, "處理進度"]).strip()
            n = str(current.loc[case_id, "維修說明"]).strip()
            if update_latest_repair(case_id=case_id, progress=p, note=n):
                ok_cnt += 1

        st.success(f"已儲存 {ok_cnt} 筆變更。")
        st.cache_data.clear()

        # 可選：LINE 通知（只通知有變更的筆數，不含案件編號）
        line_notify(f"維修更新：已儲存 {ok_cnt} 筆（{datetime.now().strftime('%Y-%m-%d')}）")

        st.rerun()


if __name__ == "__main__":
    main()
