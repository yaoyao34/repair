import streamlit as st
import pandas as pd
from datetime import datetime
import re
import gspread
from google.oauth2.service_account import Credentials

# ================= 工具 =================
def norm(x):
    if x is None:
        return ""
    return str(x).strip()

def to_ymd(ts):
    d = pd.to_datetime(ts, errors="coerce")
    if pd.isna(d):
        return ""
    return d.strftime("%Y-%m-%d")

def split_links(cell):
    if not cell:
        return []
    return [x.strip() for x in str(cell).split(",") if x.strip()]

def media_label(url, i):
    u = url.lower()
    if any(u.endswith(e) for e in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
        return f"照片 {i}"
    if any(u.endswith(e) for e in [".mp4", ".mov", ".webm", ".mkv"]):
        return f"影片 {i}"
    return f"檔案 {i}"

def status_icon(s):
    if "已完成" in s:
        return "✅"
    if "送修" in s:
        return "🚚"
    if "待料" in s:
        return "📦"
    if "處理中" in s:
        return "🛠️"
    if "退回" in s:
        return "⛔"
    if "已接單" in s:
        return "🧾"
    return "🔧"

# ================= Secrets =================
SHEET_URL = st.secrets["SHEET_URL"]

@st.cache_resource
def gs_client():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["google_service_account"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)

gc = gs_client()

# ================= 讀資料 =================
@st.cache_data(ttl=300)
def load_data():
    sh = gc.open_by_url(SHEET_URL)

    report = pd.DataFrame(sh.worksheet("報修資料").get_all_records())
    repair = pd.DataFrame(sh.worksheet("維修紀錄").get_all_records())
    pwd = norm(sh.worksheet("密碼設定").acell("A1").value)

    return report, repair, pwd

# ================= 寫回 =================
def save_repair(case_id, status, note):
    ws = gc.open_by_url(SHEET_URL).worksheet("維修紀錄")
    rows = ws.get_all_values()
    header = rows[0]

    def idx(c): return header.index(c)

    today = datetime.now().strftime("%Y-%m-%d")

    last = None
    for i in range(1, len(rows)):
        if rows[i][idx("案件編號")] == case_id:
            last = i + 1

    if last:
        ws.update_cell(last, idx("時間戳記")+1, today)
        ws.update_cell(last, idx("處理進度")+1, status)
        ws.update_cell(last, idx("維修說明")+1, note)
    else:
        row = [""] * len(header)
        row[idx("時間戳記")] = today
        row[idx("案件編號")] = case_id
        row[idx("處理進度")] = status
        row[idx("維修說明")] = note
        ws.append_row(row, value_input_option="USER_ENTERED")

# ================= 主程式 =================
def main():
    st.title("報修 / 維修整合系統")

    report, repair, correct_pwd = load_data()

    # ---- 登入 ----
    with st.sidebar:
        pwd = st.text_input("管理密碼", type="password")
        authed = (correct_pwd == "") or (pwd == correct_pwd)

        st.divider()
        kw = st.text_input("搜尋")
        status_filter = st.multiselect(
            "處理進度",
            sorted(set(repair.get("處理進度", [])))
        )

    # ---- 合併 ----
    r = report.copy()
    r["案件編號"] = r["案件編號"].astype(str)
    r["報修日期"] = r["時間戳記"].apply(to_ymd)

    w = repair.copy()
    w["案件編號"] = w["案件編號"].astype(str)
    w = w.groupby("案件編號").tail(1)

    df = r.merge(w[["案件編號","處理進度","維修說明"]], on="案件編號", how="left")
    df = df.fillna("")
    df = df.sort_values("報修日期", ascending=False)

    # ---- 篩選 ----
    if kw:
        df = df[df.apply(lambda x: kw in " ".join(x.astype(str)), axis=1)]
    if status_filter:
        df = df[df["處理進度"].isin(status_filter)]

    # ---- 顯示 ----
    for _, row in df.iterrows():
        title = f'{row["報修日期"]}｜{row["班級地點"]}｜{row["損壞設備"]}'
        with st.expander(title):
            st.markdown(f"**損壞情形**：{row['損壞情形描述']}")

            links = split_links(row["照片或影片"])
            if links:
                st.markdown("**照片 / 影片**")
                for i,u in enumerate(links,1):
                    st.markdown(f"- [{media_label(u,i)}]({u})")

            st.divider()

            icon = status_icon(row["處理進度"])
            st.markdown(f"**狀態**：{icon} {row['處理進度']}")

            if authed:
                with st.form(f"f_{row['案件編號']}"):
                    status = st.selectbox(
                        "處理進度",
                        ["","已接單","處理中","待料","送修","已完成","退回"],
                        index=["","已接單","處理中","待料","送修","已完成","退回"].index(row["處理進度"]) if row["處理進度"] in ["","已接單","處理中","待料","送修","已完成","退回"] else 0
                    )
                    note = st.text_area("維修說明", row["維修說明"])
                    if st.form_submit_button("儲存"):
                        save_repair(row["案件編號"], status, note)
                        st.success("已儲存")
                        st.cache_data.clear()
                        st.rerun()
            else:
                st.markdown(f"**維修說明**：{row['維修說明']}")

if __name__ == "__main__":
    main()
