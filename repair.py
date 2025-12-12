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
    """
    強韌轉換成 YYYY-MM-DD
    - pandas 可解析就轉
    - 解析不到：嘗試從字串抓 2025-12-12 / 2025/12/12 這類格式
    """
    if ts is None:
        return ""
    s = str(ts).strip()
    if not s:
        return ""

    d = pd.to_datetime(s, errors="coerce")
    if not pd.isna(d):
        return d.strftime("%Y-%m-%d")

    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        y, mo, da = m.group(1), int(m.group(2)), int(m.group(3))
        return f"{y}-{mo:02d}-{da:02d}"

    return ""


def split_links(cell: str) -> list[str]:
    """Google 表單多檔案用逗號分隔"""
    if not cell:
        return []
    return [p.strip() for p in str(cell).split(",") if p.strip()]


def status_emoji(status: str) -> str:
    """
    狀態圖示（你提的邏輯 + 常用補強）
    """
    s = (status or "").strip()
    e = "🔧"
    if "已完成" in s:
        e = "✅"
    elif "送修" in s or "送" in s and "修" in s:
        e = "🚚"
    elif "待料" in s:
        e = "📦"
    elif "處理中" in s:
        e = "🛠️"
    elif "退回" in s or "無法" in s:
        e = "⛔"
    elif "已接單" in s:
        e = "🧾"
    return e


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
    """只抓指定欄位，避免空白表頭 duplicates"""
    try:
        spreadsheet = gspread_client.open_by_url(SHEET_URL)

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

        repair_sheet = spreadsheet.worksheet(REPAIR_SHEET)
        repair_expected = ["時間戳記", "案件編號", "處理進度", "維修說明", "維修照片及影片"]
        repair_data = pd.DataFrame(repair_sheet.get_all_records(expected_headers=repair_expected))

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
    """
    以 案件編號 合併：
    - 報修日期：YYYY-MM-DD
    - 維修：抓「該案件在維修紀錄工作表中最後出現的一列」（不靠時間戳記，避免空白/格式亂）
    """
    r = report_df.copy()
    w = repair_df.copy()

    r["案件編號"] = r["案件編號"].astype(str).str.strip()
    w["案件編號"] = w["案件編號"].astype(str).str.strip()

    r["報修日期"] = r["時間戳記"].apply(to_ymd)
    r = r[["案件編號", "報修日期", "班級地點", "損壞設備", "損壞情形描述", "照片或影片"]]

    # 維修取最新：以原資料列順序為準（最後出現者為最新）
    w = w.reset_index(drop=True)
    w["_row_order"] = w.index
    w = w.sort_values("_row_order").groupby("案件編號", as_index=False).tail(1)

    if "處理進度" not in w.columns:
        w["處理進度"] = ""
    if "維修說明" not in w.columns:
        w["維修說明"] = ""
    w = w[["案件編號", "處理進度", "維修說明"]]

    merged = r.merge(w, on="案件編號", how="left")
    merged["處理進度"] = merged["處理進度"].fillna("")
    merged["維修說明"] = merged["維修說明"].fillna("")

    # 狀態圖示欄
    merged["狀態"] = merged["處理進度"].apply(lambda x: f"{status_emoji(x)} {x}".strip())

    # 報修日期預設遞減排
    merged["_sort_date"] = pd.to_datetime(merged["報修日期"], errors="coerce")
    merged = merged.sort_values(["_sort_date"], ascending=False, na_position="last").drop(columns=["_sort_date"])

    return merged


def update_latest_repair(case_id: str, progress: str, note: str) -> bool:
    """
    更新「維修紀錄」該案件最後一筆；若不存在則新增。
    僅更新：時間戳記(YYYY-MM-DD)、處理進度、維修說明。
    """
    try:
        spreadsheet = gspread_client.open_by_url(SHEET_URL)
        ws = spreadsheet.worksheet(REPAIR_SHEET)

        values = ws.get_all_values()
        if not values:
            st.error("維修紀錄工作表讀取失敗或空表。")
            return False

        header = values[0]

        def idx(name: str) -> int:
            return header.index(name)

        required = ["時間戳記", "案件編號", "處理進度", "維修說明"]
        for k in required:
            if k not in header:
                st.error(f"維修紀錄缺少欄位：{k}")
                return False

        idx_ts = idx("時間戳記")
        idx_case = idx("案件編號")
        idx_prog = idx("處理進度")
        idx_note = idx("維修說明")

        last_row_number = None
        for i in range(1, len(values)):
            row = values[i]
            if len(row) <= idx_case:
                continue
            if str(row[idx_case]).strip() == case_id:
                last_row_number = i + 1  # sheet row number (1-based)

        today = datetime.now().strftime("%Y-%m-%d")

        if last_row_number is None:
            out = [""] * len(header)
            out[idx_ts] = today
            out[idx_case] = case_id
            out[idx_prog] = progress
            out[idx_note] = note
            ws.append_row(out, value_input_option="USER_ENTERED")
        else:
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
    merged = build_merged_view(report_data, repair_data)

    # ---- Sidebar: 登入 + 搜尋/篩選 ----
    with st.sidebar:
        st.subheader("管理登入")
        pwd_in = norm_pwd(st.text_input("密碼", type="password"))
        if correct_password == "":
            authed = True
            st.info("密碼設定!A1 為空，目前不需要登入。")
        else:
            authed = (pwd_in == correct_password)
        st.caption(f"密碼長度：A1={len(correct_password)}、輸入={len(pwd_in)}")

        st.divider()
        st.subheader("搜尋 / 篩選")

        keyword = st.text_input("關鍵字（可搜：地點/設備/描述/維修說明）", value="").strip()

        all_status = sorted([s for s in merged["處理進度"].fillna("").unique().tolist()])
        # 讓使用者好選：把空白放最後
        all_status = [s for s in all_status if s != ""] + ([""] if "" in all_status else [])
        status_filter = st.multiselect("篩選處理進度", options=all_status, default=[])

    # ---- Apply filters ----
    filtered = merged.copy()

    if keyword:
        k = keyword.lower()
        def hit(row) -> bool:
            fields = [
                row.get("班級地點", ""),
                row.get("損壞設備", ""),
                row.get("損壞情形描述", ""),
                row.get("維修說明", ""),
            ]
            text = " ".join([str(x) for x in fields]).lower()
            return k in text

        filtered = filtered[filtered.apply(hit, axis=1)]

    if status_filter:
        filtered = filtered[filtered["處理進度"].fillna("").isin(status_filter)]

    # ---- Table (editable) ----
    st.subheader("案件總覽（上方可直接編修：處理進度 / 維修說明）")

    editor_df = filtered.copy()
    # UI 不顯示案件編號，但內部要用它更新
    editor_df = editor_df.set_index("案件編號")

    # 顯示欄位（照片欄保留原文字/連結）
    view_cols = ["報修日期", "班級地點", "損壞設備", "損壞情形描述", "照片或影片", "狀態", "維修說明"]

    # 未登入：全部唯讀；登入：只允許改「處理進度」「維修說明」
    disabled_cols = ["報修日期", "班級地點", "損壞設備", "損壞情形描述", "照片或影片", "狀態", "維修說明", "處理進度"]
    if authed:
        disabled_cols = ["報修日期", "班級地點", "損壞設備", "損壞情形描述", "照片或影片", "狀態"]

    # data_editor 內要包含「處理進度」才可編輯，但你也要顯示「狀態」
    # 所以這裡同時帶入「處理進度」並在 column_config 做美化
    show_in_editor = editor_df[["報修日期", "班級地點", "損壞設備", "損壞情形描述", "照片或影片", "處理進度", "狀態", "維修說明"]]

    edited = st.data_editor(
        show_in_editor,
        hide_index=True,
        use_container_width=True,
        disabled=disabled_cols,
        column_config={
            "處理進度": st.column_config.SelectboxColumn(
                "處理進度",
                options=["", "已接單", "處理中", "待料", "送修", "已完成", "退回/無法處理"],
            ),
            "狀態": st.column_config.TextColumn("狀態"),
            "維修說明": st.column_config.TextColumn("維修說明"),
        },
        key="editor",
    )

    # ---- Report photo hyperlinks ----
    st.divider()
    st.subheader("報修照片 / 影片（可點連結）")

    if filtered.empty:
        st.info("目前沒有符合條件的案件。")
    else:
        # 用 expander 顯示每案的可點連結（逗號分隔）
        for _, row in filtered.iterrows():
            title = f"{row.get('報修日期','')}｜{row.get('班級地點','')}｜{row.get('損壞設備','')}"
            with st.expander(title, expanded=False):
                links = split_links(row.get("照片或影片", ""))
                if not links:
                    st.write("（無）")
                else:
                    # 逐一產生超連結
                    for i, url in enumerate(links, start=1):
                        st.markdown(f"- [報修檔案 {i}]({url})")

    if not authed:
        st.warning("密碼錯誤：目前只能查看，無法儲存編修。")
        return

    # ---- Save changes ----
    st.divider()
    if st.button("儲存變更", type="primary"):
        # 對齊原始（filtered 的 editor_df）與 edited（data_editor 回傳）
        original = editor_df[["處理進度", "維修說明"]].fillna("")
        current = edited[["處理進度", "維修說明"]].fillna("")

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

        # 可選：LINE 通知（不含案件編號）
        line_notify(f"維修更新：已儲存 {ok_cnt} 筆（{datetime.now().strftime('%Y-%m-%d')}）")

        st.rerun()


if __name__ == "__main__":
    main()
