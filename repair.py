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
    if "已接單" in s:
        return "🧾"
    return "🔧"

def read_sheet_as_df(ws, expected_headers):
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame(columns=expected_headers)

    header = values[0]
    rows = values[1:]

    idx_map = {}
    for i, h in enumerate(header):
        h2 = norm(h)
        if h2 and h2 not in idx_map:
            idx_map[h2] = i

    data = {h: [] for h in expected_headers}
    for row in rows:
        for h in expected_headers:
            i = idx_map.get(h, None)
            data[h].append(row[i] if (i is not None and i < len(row)) else "")

    return pd.DataFrame(data)

def safe_key(s: str) -> str:
    """把 key 轉成 streamlit 安全字元，避免奇怪符號造成碰撞"""
    s = norm(s)
    s = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff_-]+", "_", s)
    return s[:80] if len(s) > 80 else s


# ================= Secrets / GSpread =================
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
@st.cache_data(ttl=120)
def load_data():
    sh = gc.open_by_url(SHEET_URL)

    report_ws = sh.worksheet("報修資料")
    repair_ws = sh.worksheet("維修紀錄")
    pwd_ws = sh.worksheet("密碼設定")

    report_headers = ["時間戳記","班級地點","損壞設備","損壞情形描述","照片或影片","案件編號"]
    repair_headers  = ["時間戳記","案件編號","處理進度","維修說明"]

    report = read_sheet_as_df(report_ws, report_headers)
    repair  = read_sheet_as_df(repair_ws, repair_headers)

    correct_pwd = norm(pwd_ws.acell("A1").value)
    return report, repair, correct_pwd


# ================= 寫回 =================
def save_repair(case_id, status, note):
    sh = gc.open_by_url(SHEET_URL)
    ws = sh.worksheet("維修紀錄")

    values = ws.get_all_values()
    if not values:
        raise RuntimeError("維修紀錄工作表為空或讀取失敗")

    header = values[0]

    def find_col(name):
        for i, h in enumerate(header):
            if norm(h) == name:
                return i
        return None

    c_ts   = find_col("時間戳記")
    c_case = find_col("案件編號")
    c_stat = find_col("處理進度")
    c_note = find_col("維修說明")

    if None in (c_ts, c_case, c_stat, c_note):
        raise RuntimeError("維修紀錄表頭缺少必要欄位：時間戳記/案件編號/處理進度/維修說明")

    last_row = None
    for r in range(1, len(values)):
        row = values[r]
        if c_case < len(row) and norm(row[c_case]) == case_id:
            last_row = r + 1

    today = datetime.now().strftime("%Y-%m-%d")

    if last_row:
        ws.update_cell(last_row, c_ts + 1, today)
        ws.update_cell(last_row, c_stat + 1, status)
        ws.update_cell(last_row, c_note + 1, note)
    else:
        new_row = [""] * len(header)
        new_row[c_ts] = today
        new_row[c_case] = case_id
        new_row[c_stat] = status
        new_row[c_note] = note
        ws.append_row(new_row, value_input_option="USER_ENTERED")


# ================= 主程式 =================
def main():
    st.title("報修 / 維修整合系統（自製表單版）")

    report, repair, correct_pwd = load_data()

    # ---- Sidebar ----
    with st.sidebar:
        st.subheader("管理登入")
        pwd_in = st.text_input("密碼", type="password")
        authed = (correct_pwd == "") or (pwd_in == correct_pwd)

        st.divider()
        kw = st.text_input("搜尋關鍵字（地點/設備/描述/維修）", value="").strip()

        status_list = sorted(set(repair["處理進度"].fillna("").astype(str).tolist()))
        status_filter = st.multiselect("篩選處理進度", options=status_list, default=[])

    # ---- 報修資料去重：同案件編號只留最後一筆（避免 form key 重複） ----
    r = report.copy()
    r["案件編號"] = r["案件編號"].astype(str).str.strip()
    r["_ts"] = pd.to_datetime(r["時間戳記"], errors="coerce")
    r = r.sort_values("_ts").groupby("案件編號", as_index=False).tail(1).drop(columns=["_ts"])
    r["報修日期"] = r["時間戳記"].apply(to_ymd)

    # ---- 維修資料：同案件編號只取最後一筆 ----
    w = repair.copy()
    w["案件編號"] = w["案件編號"].astype(str).str.strip()
    w["_ts"] = pd.to_datetime(w["時間戳記"], errors="coerce")
    w = w.sort_values("_ts").groupby("案件編號", as_index=False).tail(1).drop(columns=["_ts"])

    df = r.merge(w[["案件編號","處理進度","維修說明"]], on="案件編號", how="left").fillna("")
    df["_sort_date"] = pd.to_datetime(df["報修日期"], errors="coerce")
    df = df.sort_values("_sort_date", ascending=False, na_position="last").drop(columns=["_sort_date"])

    # ---- 搜尋 ----
    if kw:
        k = kw.lower()
        def hit(row):
            text = " ".join([
                str(row.get("班級地點","")),
                str(row.get("損壞設備","")),
                str(row.get("損壞情形描述","")),
                str(row.get("維修說明","")),
            ]).lower()
            return k in text
        df = df[df.apply(hit, axis=1)]

    # ---- 篩選 ----
    if status_filter:
        df = df[df["處理進度"].astype(str).isin(status_filter)]

    if df.empty:
        st.info("目前沒有符合條件的案件。")
        return

    # ---- 顯示 ----
    for i, row in enumerate(df.to_dict("records")):
        icon = status_icon(row.get("處理進度",""))
        title = f'{row.get("報修日期","")}｜{row.get("班級地點","")}｜{row.get("損壞設備","")}｜{icon} {row.get("處理進度","")}'.strip()

        case_id = norm(row.get("案件編號",""))
        case_key = safe_key(case_id)

        with st.expander(title, expanded=False):
            st.markdown(f"**損壞情形**：{row.get('損壞情形描述','')}")

            links = split_links(row.get("照片或影片",""))
            if links:
                st.markdown("**照片 / 影片（點連結查看）**")
                for j, url in enumerate(links, start=1):
                    st.markdown(f"- [{media_label(url,j)}]({url})")
            else:
                st.caption("（無照片/影片）")

            st.divider()

            if not authed:
                st.warning("未登入：僅可查看維修內容。")
                st.markdown(f"**處理進度**：{row.get('處理進度','')}")
                st.markdown(f"**維修說明**：{row.get('維修說明','')}")
                continue

            # 關鍵：form key 一定唯一（案件編號 + 迴圈序號 + hash）
            form_key = f"repair_{case_key}_{i}_{abs(hash(case_id)) % 100000}"

            with st.form(key=form_key):
                status_options = ["", "已接單", "處理中", "待料", "送修", "已完成", "退回/無法處理"]
                cur = norm(row.get("處理進度",""))
                idx = status_options.index(cur) if cur in status_options else 0

                new_status = st.selectbox("處理進度", status_options, index=idx, key=f"st_{form_key}")
                new_note = st.text_area("維修說明", value=norm(row.get("維修說明","")), key=f"nt_{form_key}")

                if st.form_submit_button("儲存"):
                    save_repair(case_id, new_status, new_note)
                    st.success("已儲存")
                    st.cache_data.clear()
                    st.rerun()


if __name__ == "__main__":
    main()
