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
    """
    最穩讀法：
    - 用 get_all_values() 讀原始表格
    - 第一列可能有空白/重複表頭，不管它
    - 只依 expected_headers 建 DataFrame（缺欄補空）
    """
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame(columns=expected_headers)

    header = values[0]
    rows = values[1:]

    # 建立：欄名 -> 第一次出現的 index（忽略重複/空白）
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


# ================= 讀資料（最穩） =================
@st.cache_data(ttl=120)
def load_data():
    sh = gc.open_by_url(SHEET_URL)

    report_ws = sh.worksheet("報修資料")
    repair_ws = sh.worksheet("維修紀錄")
    pwd_ws = sh.worksheet("密碼設定")

    report_headers = ["時間戳記","班級地點","損壞設備","損壞情形描述","照片或影片","案件編號"]
    repair_headers  = ["時間戳記","案件編號","處理進度","維修說明"]  # 不用維修照片

    report = read_sheet_as_df(report_ws, report_headers)
    repair  = read_sheet_as_df(repair_ws, repair_headers)

    correct_pwd = norm(pwd_ws.acell("A1").value)

    return report, repair, correct_pwd


# ================= 寫回（更新最後一筆，沒有就新增） =================
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

    # 找最後一筆（以列順序）
    last_row = None
    for r in range(1, len(values)):
        row = values[r]
        if c_case < len(row) and norm(row[c_case]) == case_id:
            last_row = r + 1  # sheet row number

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


# ================= 主程式：整合顯示表單 =================
def main():
    st.title("報修 / 維修整合系統（自製表單版）")

    report, repair, correct_pwd = load_data()

    # ---- Sidebar：登入 + 搜尋 + 篩選 ----
    with st.sidebar:
        st.subheader("管理登入")
        pwd_in = st.text_input("密碼", type="password")
        authed = (correct_pwd == "") or (pwd_in == correct_pwd)

        st.divider()
        kw = st.text_input("搜尋關鍵字（地點/設備/描述/維修）", value="").strip()

        # 進度篩選
        status_list = sorted(set(repair["處理進度"].fillna("").astype(str).tolist()))
        status_filter = st.multiselect("篩選處理進度", options=status_list, default=[])

    # ---- 合併：維修取同案件最後一筆 ----
    r = report.copy()
    r["案件編號"] = r["案件編號"].astype(str).str.strip()
    r["報修日期"] = r["時間戳記"].apply(to_ymd)

    w = repair.copy()
    w["案件編號"] = w["案件編號"].astype(str).str.strip()
    # 以列順序最後一筆為最新（tail(1)）
    w = w.groupby("案件編號", as_index=False).tail(1)

    df = r.merge(w[["案件編號","處理進度","維修說明"]], on="案件編號", how="left")
    df = df.fillna("")
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

    # ---- 篩選處理進度 ----
    if status_filter:
        df = df[df["處理進度"].astype(str).isin(status_filter)]

    # ---- 顯示（每案一個 expander + 表單）----
    if df.empty:
        st.info("目前沒有符合條件的案件。")
        return

    for _, row in df.iterrows():
        icon = status_icon(row["處理進度"])
        title = f'{row["報修日期"]}｜{row["班級地點"]}｜{row["損壞設備"]}｜{icon} {row["處理進度"]}'.strip()

        with st.expander(title, expanded=False):
            # 報修內容（唯讀）
            st.markdown(f"**損壞情形**：{row['損壞情形描述']}")

            # 連結（照片/影片）
            links = split_links(row["照片或影片"])
            if links:
                st.markdown("**照片 / 影片（點連結查看）**")
                for i, url in enumerate(links, start=1):
                    st.markdown(f"- [{media_label(url,i)}]({url})")
            else:
                st.caption("（無照片/影片）")

            st.divider()

            # 維修區（登入者可編輯）
            if not authed:
                st.warning("未登入：僅可查看維修內容。")
                st.markdown(f"**處理進度**：{row['處理進度']}")
                st.markdown(f"**維修說明**：{row['維修說明']}")
                continue

            with st.form(f"repair_{row['案件編號']}"):
                status_options = ["", "已接單", "處理中", "待料", "送修", "已完成", "退回/無法處理"]
                cur = str(row["處理進度"]).strip()
                idx = status_options.index(cur) if cur in status_options else 0

                new_status = st.selectbox("處理進度", status_options, index=idx)
                new_note = st.text_area("維修說明", value=str(row["維修說明"]))

                if st.form_submit_button("儲存"):
                    save_repair(str(row["案件編號"]).strip(), new_status, new_note)
                    st.success("已儲存")
                    st.cache_data.clear()
                    st.rerun()


if __name__ == "__main__":
    main()
