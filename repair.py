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
    s = norm(ts)
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

def split_links(cell):
    if not cell:
        return []
    return [x.strip() for x in str(cell).split(",") if x.strip()]

def media_label(url, i):
    u = (url or "").lower()
    if any(u.endswith(e) for e in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
        return f"照片 {i}"
    if any(u.endswith(e) for e in [".mp4", ".mov", ".webm", ".mkv"]):
        return f"影片 {i}"
    return f"檔案 {i}"

def status_icon(s):
    s = s or ""
    if "已完成" in s:
        return "✅"
    if "送修" in s:
        return "🚚"
    if "待料" in s:
        return "📦"
    if "處理中" in s:
        return "🛠️"
    if "退回" in s or "無法" in s:
        return "⛔"
    if "待觀查" in s:
        return "👀"
    return "🔧"

def safe_key(s):
    s = norm(s)
    s = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff_-]+", "_", s)
    return s[:80]


# ================= GSpread =================
SHEET_URL = st.secrets["SHEET_URL"]

@st.cache_resource
def gs_client():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["google_service_account"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)

gc = gs_client()


# ================= 讀資料（穩定版） =================
def read_sheet_as_df(ws, headers):
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame(columns=headers)

    header = values[0]
    rows = values[1:]

    idx_map = {}
    for i, h in enumerate(header):
        h = norm(h)
        if h and h not in idx_map:
            idx_map[h] = i

    data = {h: [] for h in headers}
    for r in rows:
        for h in headers:
            i = idx_map.get(h)
            data[h].append(r[i] if i is not None and i < len(r) else "")

    return pd.DataFrame(data)


@st.cache_data(ttl=120)
def load_data():
    sh = gc.open_by_url(SHEET_URL)

    report = read_sheet_as_df(
        sh.worksheet("報修資料"),
        ["時間戳記","班級地點","損壞設備","損壞情形描述","照片或影片","案件編號"]
    )

    repair = read_sheet_as_df(
        sh.worksheet("維修紀錄"),
        ["時間戳記","案件編號","處理進度","維修說明"]
    )

    pwd = norm(sh.worksheet("密碼設定").acell("A1").value)
    return report, repair, pwd


# ================= 寫回 =================
def save_repair(case_id, status, note):
    ws = gc.open_by_url(SHEET_URL).worksheet("維修紀錄")
    values = ws.get_all_values()
    header = values[0]

    def col(name):
        for i, h in enumerate(header):
            if norm(h) == name:
                return i
        return None

    c_ts, c_case, c_stat, c_note = map(col, ["時間戳記","案件編號","處理進度","維修說明"])

    today = datetime.now().strftime("%Y-%m-%d")
    last = None
    for i in range(1, len(values)):
        if c_case < len(values[i]) and norm(values[i][c_case]) == case_id:
            last = i + 1

    if last:
        ws.update_cell(last, c_ts+1, today)
        ws.update_cell(last, c_stat+1, status)
        ws.update_cell(last, c_note+1, note)
    else:
        row = [""] * len(header)
        row[c_ts] = today
        row[c_case] = case_id
        row[c_stat] = status
        row[c_note] = note
        ws.append_row(row, value_input_option="USER_ENTERED")


# ================= 主程式 =================
def main():
    st.title("報修 / 維修整合系統")

    report, repair, correct_pwd = load_data()

    # ---- Sidebar ----
    with st.sidebar:
        st.subheader("管理登入")
        pwd = st.text_input("密碼", type="password")
        authed = (correct_pwd == "") or (pwd == correct_pwd)

        st.divider()
        kw = st.text_input("搜尋關鍵字")

        status_list = sorted(set(repair["處理進度"].fillna("").tolist()))
        status_filter = st.multiselect("處理進度", status_list)

    # ---- 合併 ----
    r = report.copy()
    r["案件編號"] = r["案件編號"].astype(str).str.strip()
    r["_ts"] = pd.to_datetime(r["時間戳記"], errors="coerce")
    r = r.sort_values("_ts").groupby("案件編號", as_index=False).tail(1)
    r["報修日期"] = r["時間戳記"].apply(to_ymd)

    w = repair.copy()
    w["案件編號"] = w["案件編號"].astype(str).str.strip()
    w["_ts"] = pd.to_datetime(w["時間戳記"], errors="coerce")
    w = w.sort_values("_ts").groupby("案件編號", as_index=False).tail(1)

    df = r.merge(w[["案件編號","處理進度","維修說明"]], on="案件編號", how="left").fillna("")
    df = df.sort_values("報修日期", ascending=False)

    if kw:
        df = df[df.apply(lambda x: kw.lower() in " ".join(x.astype(str)).lower(), axis=1)]
    if status_filter:
        df = df[df["處理進度"].isin(status_filter)]

    # ---- 分頁（固定 10 筆）----
    PAGE_SIZE = 10
    total = len(df)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    page = st.number_input("頁碼", 1, pages, 1)
    start, end = (page-1)*PAGE_SIZE, page*PAGE_SIZE
    page_df = df.iloc[start:end]

    st.caption(f"共 {total} 筆，顯示第 {start+1}–{min(end, total)} 筆（第 {page}/{pages} 頁）")

    # ---- 顯示 ----
    for i, row in enumerate(page_df.to_dict("records")):
        icon = status_icon(row["處理進度"])
        title = f'{row["報修日期"]}｜{row["班級地點"]}｜{row["損壞設備"]}｜{icon} {row["處理進度"]}'

        case_id = norm(row["案件編號"])
        form_key = f"f_{safe_key(case_id)}_{page}_{i}"

        with st.expander(title):
            st.markdown(f"**損壞情形**：{row['損壞情形描述']}")

            for j, url in enumerate(split_links(row["照片或影片"]), 1):
                st.markdown(f"- [{media_label(url,j)}]({url})")

            st.divider()

            if not authed:
                st.markdown(f"**處理進度**：{row['處理進度']}")
                st.markdown(f"**維修說明**：{row['維修說明']}")
                continue

            with st.form(form_key):
                options = ["","待觀查","處理中","待料","送修","已完成","退回/無法處理"]
                cur = row["處理進度"] if row["處理進度"] in options else ""
                status = st.selectbox("處理進度", options, index=options.index(cur))
                note = st.text_area("維修說明", row["維修說明"])
                if st.form_submit_button("儲存"):
                    save_repair(case_id, status, note)
                    st.success("已儲存")
                    st.cache_data.clear()
                    st.rerun()


if __name__ == "__main__":
    main()
