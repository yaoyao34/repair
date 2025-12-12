import streamlit as st
import pandas as pd
from streamlit_gsheets import GSheetsConnection
import requests
from datetime import datetime
import time

# --- 1. 全域變數與設定 ---

# 從 secrets.toml 讀取設定
LINE_ACCESS_TOKEN = st.secrets["LINE_ACCESS_TOKEN"]
GROUP_ID = st.secrets["GROUP_ID"]
SHEET_URL = st.secrets["SHEET_URL"]

# 初始化 Sheets 連線
conn = st.connection("gsheets", type=GSheetsConnection)

# 工作表名稱
REPORT_SHEET = "報修資料"
REPAIR_SHEET = "維修紀錄"
PASSWORD_SHEET = "密碼設定"
    
# --- 2. 核心函式 ---

@st.cache_data(ttl=600) # 快取資料，避免每次刷新都重複讀取
def load_data():
    """從 Google Sheets 讀取所有必要資料"""
    try:
        # 讀取密碼 (假設密碼設定表只有一個密碼在 A1)
        password_df = conn.read(spreadsheet=SHEET_URL, worksheet=PASSWORD_SHEET, usecols=[0], header=None)
        correct_password = str(password_df.iloc[0, 0]).strip() # 取 A1 格並去除空白

        # 讀取報修資料
        report_data = conn.read(spreadsheet=SHEET_URL, worksheet=REPORT_SHEET, ttl=5)
        
        # 讀取維修紀錄
        repair_data = conn.read(spreadsheet=SHEET_URL, worksheet=REPAIR_SHEET, ttl=5)
        
        # 確保資料框非空
        if report_data.empty:
             report_data = pd.DataFrame(columns=['案件編號', '地點', '損壞設備'])
        if repair_data.empty:
            repair_data = pd.DataFrame(columns=['案件編號', '處理進度', '維修說明', '更新時間'])

        return report_data, repair_data, correct_password

    except Exception as e:
        st.error(f"讀取資料庫錯誤，請檢查 Sheets 權限或設定: {e}")
        return pd.DataFrame(), pd.DataFrame(), "DEFAULT_PASSWORD_ERROR"

def send_line_notification(text):
    """發送 LINE 訊息到群組"""
    url = "https://api.line.me/v2/bot/message/push"
    
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Authorization": "Bearer " + LINE_ACCESS_TOKEN,
    }
    payload = {
        "to": GROUP_ID,
        "messages": [{"type": "text", "text": text}]
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status() # 對於 4xx 或 5xx 錯誤拋出異常
    except requests.exceptions.RequestException as e:
        st.error(f"LINE 發送失敗: {e}")
        st.code(f"LINE Response Status: {response.status_code}\nResponse Text: {response.text}")
        return False
    return True


def append_repair_record(record):
    """將維修紀錄寫入 Google Sheets"""
    try:
        conn.append(
            spreadsheet=SHEET_URL,
            worksheet=REPAIR_SHEET,
            data=pd.DataFrame([record]) # GSheetsConnection 寫入需要 DataFrame
        )
        return True
    except Exception as e:
        st.error(f"寫入維修紀錄失敗: {e}")
        return False

# --- 3. 頁面函式 ---

def show_login(correct_password):
    """顯示登入介面"""
    with st.sidebar:
        st.header("維修人員登入")
        password_input = st.text_input("輸入維修密碼", type="password", key="login_pass")
        
        if st.button("登入", use_container_width=True):
            if password_input == correct_password:
                st.session_state["logged_in"] = True
                st.success("登入成功！")
                st.rerun()
            else:
                st.error("密碼錯誤，請重新輸入。")

def show_repair_form(report_df, repair_df):
    """顯示維修回報表單 (僅登入後可見)"""
    st.divider()
    st.header("📝 維修進度回報")

    # 找出所有已報修但未標註完成的案件編號，用於 Selectbox
    # 取得已完成的案件編號
    completed_cases = repair_df[repair_df['處理進度'] == '✅ 已完成']['案件編號'].unique()
    
    # 篩選出未完成的報修案件
    pending_reports = report_df[~report_df['案件編號'].isin(completed_cases)]
    
    # 組合下拉選單選項
    if pending_reports.empty:
        st.info("目前沒有未完成的報修案件。")
        return

    # 組合顯示名稱：案件編號 (地點 - 設備)
    case_options = pending_reports.apply(
        lambda row: f"{row['案件編號']} ({row['地點']} - {row['損壞設備']})", axis=1
    ).tolist()

    with st.form("repair_update_form"):
        selected_option = st.selectbox("請選擇要回報的案件", case_options, help="案件編號後方顯示地點與設備")
        
        # 從選項中解析出純案件編號
        ticket_id = selected_option.split(' ')[0] 
        
        status_options = [
            "🔧 處理中", 
            "🚚 送修/待料中", 
            "✅ 已完成"
        ]
        new_status = st.radio("處理進度", status_options, index=0, horizontal=True)
        
        note = st.text_area("維修說明 (請簡述處理內容與結果)")
        
        # 維修照片及影片欄位，由於 Streamlit 上傳檔案需處理檔案連結，這裡僅示範文字輸入連結
        photo_link = st.text_input("維修照片/影片連結 (可選)")
        
        submitted = st.form_submit_button("提交回報並通知 LINE 群組", type="primary")
        
        if submitted:
            if not note:
                st.error("維修說明不可空白。")
                return

            timestamp = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            
            # 組織要寫入 Sheets 的資料
            new_record = {
                '時間戳記': timestamp,
                '案件編號': ticket_id,
                '處理進度': new_status,
                '維修說明': note,
                '維修照片及影片': photo_link if photo_link else "無照片/連結"
            }
            
            # 寫入 Sheets
            if append_repair_record(new_record):
                # 組合 LINE 訊息
                line_message = f"{new_status}【維修進度更新】\n" + \
                               f"案件編號：{ticket_id}\n" + \
                               f"目前狀態：{new_status.split(' ')[1]}\n" + \
                               f"處理說明：{note}\n" + \
                               f"更新時間：{timestamp}"
                
                # 發送 LINE 通知
                send_line_notification(line_message)
                
                st.success(f"案件 {ticket_id} 回報成功！已廣播至 LINE 群組。")
                
                # 清除快取並重新載入，以更新顯示的表格
                st.cache_data.clear()
                time.sleep(1) # 等待資料庫寫入完成
                st.rerun()

# --- 4. Streamlit 主程式 ---

def main():
    st.set_page_config(layout="wide", page_title="維修管理系統", initial_sidebar_state="expanded")
    st.title("🔧 設備報修/維修管理系統")

    # 初始化登入狀態
    if "logged_in" not in st.session_state:
        st.session_state["logged_in"] = False

    # 載入資料 (並取得正確密碼)
    report_data, repair_data, correct_password = load_data()

    # 處理登入
    if not st.session_state.logged_in:
        show_login(correct_password)
    else:
        # 顯示登出按鈕
        with st.sidebar:
            st.success("已登入為維修人員")
            if st.button("登出", use_container_width=True):
                st.session_state.logged_in = False
                st.rerun()
        
        # 顯示維修回報表單
        show_repair_form(report_data, repair_data)

    # --- 顯示所有維修紀錄 ---
    st.header("📂 最新維修紀錄")
    
    # 確保 Timestamp 格式正確並排序
    try:
        repair_data['時間戳記'] = pd.to_datetime(repair_data['時間戳記'], errors='coerce')
        display_data = repair_data.sort_values(by='時間戳記', ascending=False)
    except:
         display_data = repair_data
         st.warning("時間戳記轉換錯誤，無法排序。")

    # 資料篩選器
    all_statuses = display_data['處理進度'].unique().tolist()
    status_filter = st.multiselect(
        "依處理進度篩選", 
        options=["全部"] + all_statuses, 
        default=["全部"]
    )

    if "全部" not in status_filter:
        display_data = display_data[display_data['處理進度'].isin(status_filter)]
    
    # 欄位重新命名以利顯示
    display_data = display_data.rename(columns={
        '時間戳記': '更新時間',
        '案件編號': '案件編號',
        '處理進度': '狀態',
        '維修說明': '說明',
        '維修照片及影片': '照片/影片連結'
    })

    st.dataframe(display_data[['案件編號', '狀態', '說明', '更新時間', '照片/影片連結']], use_container_width=True)


if __name__ == "__main__":
    main()