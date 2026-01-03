# DevOpsTool.py
import customtkinter as ctk
import os
import subprocess
import shutil
import threading
import json
import sys
import time
import requests  # 需 pip install requests
from packaging import version  # 需 pip install packaging
from tkinter import filedialog, messagebox
import signal
import tempfile
import traceback

# ================= 設定區 (開發者請修改這裡) =================
APP_NAME = "DevOpsMaster"
CURRENT_VERSION = "1.0.1"  # 更新版本號

# GitHub 更新資訊
GITHUB_USER = "Riridesu"     # 你的 GitHub 帳號
GITHUB_REPO = "DevOpsTool"     # 你的儲存庫名稱

# 1. 版本檢查網址
VERSION_URL = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/version.txt"
# 2. 新版執行檔下載點
EXE_DOWNLOAD_URL = f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}/releases/latest/download/DevOpsTool.exe"
# ==========================================================

# 設定外觀
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")

# --- 設定檔路徑遷移至 AppData ---
if os.name == 'nt':
    appdata_env = os.getenv('APPDATA') or os.path.expanduser('~')
    APP_DATA_DIR = os.path.join(appdata_env, APP_NAME)
else:
    APP_DATA_DIR = os.path.join(os.path.expanduser('~'), ".config", APP_NAME)

if not os.path.exists(APP_DATA_DIR):
    os.makedirs(APP_DATA_DIR, exist_ok=True)

GLOBAL_CONFIG_FILE = os.path.join(APP_DATA_DIR, "tool_settings.json")

# 自動遷移舊設定
local_config = "tool_settings.json"
if os.path.exists(local_config) and not os.path.exists(GLOBAL_CONFIG_FILE):
    try:
        shutil.copy(local_config, GLOBAL_CONFIG_FILE)
    except Exception:
        pass


class UpdateManager:
    """處理線上更新的核心邏輯（UI 互動皆會派回主執行緒）"""
    def __init__(self, app_instance, log_callback):
        self.app = app_instance
        self.log = log_callback
        self.session = requests.Session()

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def check_for_updates(self):
        """在背景執行緒下載遠端版本號，後續的 UI 互動使用 app.after 跑到主執行緒"""
        try:
            self.log(f"正在檢查更新... (目前版本 v{CURRENT_VERSION})")
            response = self.session.get(VERSION_URL, timeout=8)
            if response.status_code != 200:
                self.log(f"檢查失敗: 無法連接伺服器 (Code {response.status_code})")
                # show message on main thread
                self.app.after(0, lambda: messagebox.showwarning("檢查失敗", f"無法連接更新伺服器 (Code {response.status_code})"))
                return

            remote_ver_str = response.text.strip()
            self.log(f"遠端版本: v{remote_ver_str}")

            try:
                if version.parse(remote_ver_str) > version.parse(CURRENT_VERSION):
                    # prompt on main thread, then if yes start download in background
                    def prompt_and_update():
                        ans = messagebox.askyesno("發現新版本", f"發現新版本 v{remote_ver_str}！\n\n點擊「是」將自動下載並重啟更新。")
                        if ans:
                            # run perform_update in a background daemon thread to avoid blocking UI
                            th = threading.Thread(target=self.perform_update, daemon=True)
                            th.start()

                    self.app.after(0, prompt_and_update)
                else:
                    self.log("目前已是最新版本。")
                    self.app.after(0, lambda: messagebox.showinfo("檢查結果", "目前已是最新版本。"))
            except Exception as e:
                self.log(f"版本比對失敗: {e}\n{traceback.format_exc()}")

        except Exception as e:
            self.log(f"更新檢查發生錯誤: {e}\n{traceback.format_exc()}")
            self.app.after(0, lambda: messagebox.showerror("檢查錯誤", str(e)))

    def perform_update(self):
        """下載新 exe 並啟動更新流程（下載於 APP_DATA_DIR，UI 訊息回到主執行緒）"""
        self.log("開始下載更新檔...")
        # 下載到 AppData 的暫存檔
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".exe", prefix="update_", dir=APP_DATA_DIR)
            os.close(tmp_fd)
            self.log(f"暫存檔: {tmp_path}")

            with self.session.get(EXE_DOWNLOAD_URL, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(tmp_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=16384):
                        if chunk:
                            f.write(chunk)

            self.log("下載完成，準備重新啟動...")

            current_exe = sys.executable
            basename = os.path.basename(current_exe).lower()

            # 如果是在 python 解譯器中執行（非封裝 exe），不做覆蓋，但通知使用者
            if not current_exe.lower().endswith(".exe") or "python" in basename:
                self.log("偵測到非 exe 執行環境，無法自動更新執行檔。")
                def notify():
                    messagebox.showwarning("無法更新", "您正在使用 Python 直譯器執行腳本，\n無法進行 EXE 自我覆蓋。請以已封裝的 .exe 執行更新。")
                self.app.after(0, notify)
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                return

            # 建立 updater.bat 放在 APP_DATA_DIR，使用絕對路徑並嘗試替換 exe（會等待原程序關閉）
            bat_path = os.path.join(APP_DATA_DIR, "updater.bat")
            # 使用延遲/重試以處理 windows 鎖定問題
            bat_script = f"""@echo off
title Updating {APP_NAME}...
REM 等待原程序完全關閉並嘗試替換 (最多 30 次)
setlocal enabledelayedexpansion
set RETRIES=0
:LOOP
if %RETRIES% GEQ 30 goto FAIL
timeout /t 1 /nobreak > NUL
2>nul attrib -r "{current_exe}"
move /Y "{tmp_path}" "{current_exe}" >nul 2>&1
if exist "{current_exe}" (
    goto STARTAPP
) else (
    set /a RETRIES+=1
    goto LOOP
)
:STARTAPP
start "" "{current_exe}"
del "%~f0"
exit /b 0
:FAIL
echo 更新失敗，請手動替換 {current_exe}
pause
del "%~f0"
"""
            try:
                with open(bat_path, "w", encoding='utf-8') as bat:
                    bat.write(bat_script)
            except Exception as e:
                self.log(f"寫入 updater.bat 失敗: {e}\n{traceback.format_exc()}")
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                self.app.after(0, lambda: messagebox.showerror("更新錯誤", f"無法建立 updater.bat：{e}"))
                return

            # 啟動 updater.bat
            try:
                # 啟動後主程序需結束，讓批次檔能替換檔案
                subprocess.Popen(f'"{bat_path}"', shell=True, cwd=APP_DATA_DIR)
                # 關閉應用程式（在主執行緒呼叫 on_closing）
                try:
                    self.app.after(0, lambda: self.app.on_closing(force=True))
                except Exception:
                    pass
                # 強制退出背景執行緒/進程
                try:
                    time.sleep(1)
                except Exception:
                    pass
                os._exit(0)
            except Exception as e:
                self.log(f"啟動 updater.bat 失敗: {e}\n{traceback.format_exc()}")
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                self.app.after(0, lambda: messagebox.showerror("更新錯誤", str(e)))

        except Exception as e:
            self.log(f"更新失敗: {e}\n{traceback.format_exc()}")
            try:
                if 'tmp_path' in locals() and os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            self.app.after(0, lambda: messagebox.showerror("更新錯誤", str(e)))


class TaskHandler:
    """負責執行具體任務"""
    def __init__(self, log_callback):
        self.log = log_callback
        self.current_process = None
        self.process_lock = threading.Lock()
        self.stop_event = threading.Event()

    def _terminate_process_group(self, process):
        try:
            if process.poll() is not None:
                return
        except Exception:
            pass

        try:
            if os.name == 'nt':
                try:
                    # 需要 process 是 new process group 才能生效
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                    self.log("已向 Windows process group 發送 CTRL_BREAK_EVENT。")
                except Exception as e:
                    self.log(f"發送 CTRL_BREAK_EVENT 失敗: {e}")
                    try:
                        process.terminate()
                    except Exception:
                        pass
            else:
                try:
                    pgid = os.getpgid(process.pid)
                    os.killpg(pgid, signal.SIGTERM)
                    self.log("已對 process group 發送 SIGTERM。")
                except Exception as e:
                    self.log(f"對 process group 發送 SIGTERM 失敗: {e}")
        except Exception:
            pass

    def run_cmd(self, command, cwd=None, env=None, shell=True):
        try:
            self.log(f"[{cwd}] > {command}")
            env_copy = os.environ.copy()
            env_copy["PYTHONIOENCODING"] = "utf-8"
            if env:
                env_copy.update(env)

            popen_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "shell": shell,
                "cwd": cwd,
                "env": env_copy,
                "text": True,
                "encoding": 'utf-8',
                "errors": 'replace',
                "bufsize": 1
            }

            if os.name == 'nt':
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["preexec_fn"] = os.setsid

            process = subprocess.Popen(command, **popen_kwargs)

            with self.process_lock:
                self.current_process = process
                self.stop_event.clear()

            try:
                # 防護：process.stdout 可能為 None
                if process.stdout is not None:
                    for line in iter(process.stdout.readline, ''):
                        if line:
                            self.log(line.rstrip())
                        # 檢查中斷事件
                        if self.stop_event.is_set():
                            try:
                                self._terminate_process_group(process)
                            except Exception:
                                pass
                            # 繼續讀取輸出直到 process 結束或被 kill
                    # close stdout if still open
                    try:
                        process.stdout.close()
                    except Exception:
                        pass
                else:
                    self.log("process.stdout is None")
            except Exception as e:
                self.log(f"讀取 subprocess 輸出錯誤: {e}\n{traceback.format_exc()}")
                try:
                    if process.poll() is None:
                        process.terminate()
                except Exception:
                    pass

            try:
                rc = process.wait(timeout=10)
            except Exception:
                try:
                    try:
                        if os.name == 'nt':
                            process.kill()
                        else:
                            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except Exception:
                        pass
                except Exception:
                    pass
                try:
                    rc = process.wait()
                except Exception:
                    rc = -1

            return rc
        except Exception as e:
            self.log(f"指令錯誤: {e}\n{traceback.format_exc()}")
            return 1
        finally:
            with self.process_lock:
                self.current_process = None
                self.stop_event.clear()

    def stop_all(self):
        self.stop_event.set()
        with self.process_lock:
            p = self.current_process
            if not p:
                return
            try:
                try:
                    self._terminate_process_group(p)
                except Exception:
                    pass
                try:
                    p.wait(timeout=3)
                    return
                except Exception:
                    pass
                try:
                    if p.poll() is None:
                        p.terminate()
                except Exception:
                    pass
                try:
                    p.wait(timeout=2)
                except Exception:
                    try:
                        if os.name == 'nt':
                            p.kill()
                        else:
                            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    except Exception:
                        pass
            except Exception:
                pass

    def action_clean(self, project_path, venv_name):
        self.log("--- 清理暫存檔案 ---")
        targets = ["build", "dist", "__pycache__", venv_name]
        for t in targets:
            full_path = os.path.join(project_path, t)
            if os.path.exists(full_path):
                try:
                    shutil.rmtree(full_path)
                    self.log(f"已刪除: {t}")
                except Exception as e:
                    self.log(f"刪除失敗 {t}: {e}")
        for f in os.listdir(project_path):
            if f.endswith(".spec"):
                try:
                    os.remove(os.path.join(project_path, f))
                except Exception:
                    pass
        self.log("清理完成。")

    def action_run(self, project_path, entry_point):
        self.log(f"--- 執行測試: {entry_point} ---")
        ep = os.path.join(project_path, entry_point) if not os.path.isabs(entry_point) else entry_point
        if not os.path.exists(ep):
            self.log(f"入口檔案不存在: {ep}")
            self.app_log_local(f"入口檔案不存在: {ep}")
            return
        self.run_cmd(f'python "{ep}"', cwd=project_path)

    def action_build(self, project_path, venv_name, entry_point, output_name):
        self.log("--- 開始建置流程 ---")
        venv_path = os.path.join(project_path, venv_name)
        if not os.path.exists(venv_path):
            self.log("建立虛擬環境...")
            # 建立於 project_path
            self.run_cmd(f'python -m venv "{venv_name}"', cwd=project_path)

        pip_cmd = os.path.join(venv_path, "Scripts", "pip.exe") if os.name == 'nt' else os.path.join(venv_path, "bin", "pip")
        py_cmd = os.path.join(venv_path, "Scripts", "python.exe") if os.name == 'nt' else os.path.join(venv_path, "bin", "python")

        req_file = os.path.join(project_path, "requirements.txt")
        pkgs = ["pyinstaller"]
        if os.path.exists(req_file):
            self.log("讀取 requirements.txt...")
            try:
                with open(req_file, 'r', encoding='utf-8') as f:
                    pkgs += [l.strip() for l in f if l.strip() and not l.startswith('#')]
            except Exception as e:
                self.log(f"讀取 requirements.txt 發生錯誤: {e}")

        # 如果 pip 不存在，改用 python -m pip 安裝
        if not os.path.exists(pip_cmd):
            self.log("venv pip 未找到，使用 python -m pip 安裝套件。")
            install_cmd = f'"{py_cmd}" -m pip install {" ".join(pkgs)}'
        else:
            install_cmd = f'"{pip_cmd}" install {" ".join(pkgs)}'

        self.run_cmd(install_cmd, cwd=project_path)

        ep = os.path.join(project_path, entry_point) if not os.path.isabs(entry_point) else entry_point
        if not os.path.exists(ep):
            self.log(f"入口檔案不存在: {ep}，建置取消。")
            return

        cmd = f'"{py_cmd}" -m PyInstaller -F --clean --name "{output_name}" "{ep}" --distpath ./dist'
        self.run_cmd(cmd, cwd=project_path)
        self.log(f"打包完成: dist/{output_name}.exe")

    def action_publish(self, project_path, user, repo):
        self.log(f"--- 發布至 GitHub ({user}/{repo}) ---")
        if not os.path.exists(os.path.join(project_path, ".git")):
            self.run_cmd("git init", cwd=project_path)

        try:
            self.run_cmd(f"git remote remove origin", cwd=project_path)
        except Exception:
            pass

        self.run_cmd(f"git remote add origin https://github.com/{user}/{repo}.git", cwd=project_path)
        self.run_cmd("git add .", cwd=project_path)
        rc = self.run_cmd('git commit -m "Update via DevOps Tool"', cwd=project_path)
        if rc != 0:
            # commit 失敗可能是沒有變更，記錄並嘗試 push（如果 remote 已有 commit）
            self.log("git commit 可能失敗（例如沒有變更或尚未設定 git user），跳過 commit。")
        # 嘗試推送 main，若失敗再嘗試 master
        if self.run_cmd("git push -u origin main", cwd=project_path) != 0:
            self.run_cmd("git push -u origin master", cwd=project_path)


class App(ctk.CTk):
    # 語言字典
    TRANSLATIONS = {
        "zh": {
            "global_settings": "⚙️ GitHub User",
            "btn_save": "儲存",
            "btn_update": "⟳ 檢查更新",
            "recent_files": "最近開啟：",
            "btn_browse": "📂 瀏覽新資料夾",
            "entry_point": "入口檔案",
            "output_name": "輸出檔名 (.exe)",
            "repo_name": "Repo 名稱",
            "btn_save_project": "💾 儲存專案設定",
            "panel_title": "操作面板",
            "btn_run": "▶ 執行測試",
            "btn_clean": "🗑 清理環境",
            "btn_build": "🔨 一鍵打包",
            "btn_publish": "☁ 發布",
            "lang_label": "語言 / Language"
        },
        "en": {
            "global_settings": "⚙️ GitHub User",
            "btn_save": "Save",
            "btn_update": "⟳ Check Update",
            "recent_files": "Recent:",
            "btn_browse": "📂 Browse Folder",
            "entry_point": "Entry Point",
            "output_name": "Output Name (.exe)",
            "repo_name": "Repo Name",
            "btn_save_project": "💾 Save Project Config",
            "panel_title": "Control Panel",
            "btn_run": "▶ Run Test",
            "btn_clean": "🗑 Clean Env",
            "btn_build": "🔨 Build EXE",
            "btn_publish": "☁ Publish",
            "lang_label": "Language"
        }
    }

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{CURRENT_VERSION}")
        self.geometry("900x750") # 稍微加高以容納語言選單

        self.lang = "zh" # 預設語言

        # handler 需要能記錄 UI log（handler 內有些方法會呼叫 app 屬性）
        self.handler = TaskHandler(log_callback=self.ui_log)
        # 把 app 參考注入 handler（部分 handler 方法想要呼叫 app 的函式）
        self.handler.app_log_local = self.ui_log
        self.updater = UpdateManager(app_instance=self, log_callback=self.ui_log)

        self.project_path = None
        self.recent_projects = []

        self._threads = []
        self._threads_lock = threading.Lock()
        self._closing = False

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # === 1. 全域設定與更新 ===
        self.global_frame = ctk.CTkFrame(self)
        self.global_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=20, pady=(15, 5))

        self.lbl_global_title = ctk.CTkLabel(self.global_frame, text=self.t("global_settings"), font=("Arial", 12, "bold"))
        self.lbl_global_title.pack(side="left", padx=10)
        
        self.entry_git_user = ctk.CTkEntry(self.global_frame, width=150)
        self.entry_git_user.pack(side="left", padx=5)

        self.btn_save_global = ctk.CTkButton(self.global_frame, text=self.t("btn_save"), width=60, fg_color="#444", command=self.save_global_settings)
        self.btn_save_global.pack(side="left", padx=5)

        self.btn_update = ctk.CTkButton(self.global_frame, text=self.t("btn_update"), width=100, fg_color="#E67E22", hover_color="#D35400", command=self.thread_check_update)
        self.btn_update.pack(side="right", padx=10)

        self.lbl_ver = ctk.CTkLabel(self.global_frame, text=f"v{CURRENT_VERSION}", text_color="gray")
        self.lbl_ver.pack(side="right", padx=5)

        # === 2. 專案選擇 ===
        self.select_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.select_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=20, pady=5)

        self.lbl_recent = ctk.CTkLabel(self.select_frame, text=self.t("recent_files"))
        self.lbl_recent.pack(side="left", padx=(0, 5))
        
        self.history_menu = ctk.CTkOptionMenu(self.select_frame, values=["無紀錄"], command=self.load_from_history, width=300)
        self.history_menu.pack(side="left", padx=5)

        self.btn_select = ctk.CTkButton(self.select_frame, text=self.t("btn_browse"), command=self.select_folder)
        self.btn_select.pack(side="left", padx=10)

        self.lbl_path = ctk.CTkLabel(self.select_frame, text="", text_color="gray")
        self.lbl_path.pack(side="left", padx=10)

        # === 3. 專案設定 ===
        self.project_config_frame = ctk.CTkFrame(self)
        self.project_config_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=5)

        # 儲存 entry 與 label 引用以便切換語言
        self.lbl_entry_point = None
        self.lbl_output_name = None
        self.lbl_repo_name = None

        def create_entry(parent, key, default, col):
            lbl = ctk.CTkLabel(parent, text=self.t(key), font=("Arial", 12, "bold"))
            lbl.grid(row=0, column=col, padx=10, pady=5, sticky="w")
            entry = ctk.CTkEntry(parent, width=180)
            entry.grid(row=1, column=col, padx=10, pady=5)
            entry.insert(0, default)
            return entry, lbl

        self.entry_entrypoint, self.lbl_entry_point = create_entry(self.project_config_frame, "entry_point", "src/main.py", 0)
        self.entry_output, self.lbl_output_name = create_entry(self.project_config_frame, "output_name", "MyTool", 1)
        self.entry_git_repo, self.lbl_repo_name = create_entry(self.project_config_frame, "repo_name", "MyRepo", 2)

        self.btn_save_project = ctk.CTkButton(self.project_config_frame, text=self.t("btn_save_project"), width=120, fg_color="#555", command=self.save_project_settings)
        self.btn_save_project.grid(row=1, column=3, padx=20)

        # === 4. 操作面板 (Sidebar) ===
        self.sidebar = ctk.CTkFrame(self, width=180, corner_radius=0)
        self.sidebar.grid(row=3, column=0, sticky="nsew", pady=10)
        
        self.lbl_panel = ctk.CTkLabel(self.sidebar, text=self.t("panel_title"), font=("Arial", 16, "bold"))
        self.lbl_panel.pack(pady=20)

        # 按鈕變數化並調整順序: Run -> Publish -> Build -> Clean
        # 1. Run (執行) - 綠色
        self.btn_run = self.create_btn(self.sidebar, "btn_run", self.thread_run, "#2CC985", "#229A66")
        
        # 2. Publish (發布) - 紫色 (移到第二順位)
        self.btn_publish = self.create_btn(self.sidebar, "btn_publish", self.thread_publish, "#9B59B6", "#8E44AD")

        # 3. Build (打包) - 藍色 (移到第三順位)
        self.btn_build = self.create_btn(self.sidebar, "btn_build", self.thread_build, "#3498DB", "#2980B9")

        # 4. Clean (清理) - 紅色 (移到最後)
        self.btn_clean = self.create_btn(self.sidebar, "btn_clean", self.thread_clean, "#E74C3C", "#C0392B")

        # === 5. 語言切換區 (新增於左下角) ===
        self.lbl_lang = ctk.CTkLabel(self.sidebar, text=self.t("lang_label"))
        self.lbl_lang.pack(side="bottom", pady=(0, 10))
        
        self.lang_menu = ctk.CTkOptionMenu(
            self.sidebar, 
            values=["繁體中文", "English"], 
            command=self.change_language,
            width=140
        )
        self.lang_menu.set("繁體中文") # Default
        self.lang_menu.pack(side="bottom", pady=(10, 5), padx=20)

        # === 6. 日誌區 ===
        self.textbox = ctk.CTkTextbox(self, font=("Consolas", 12))
        self.textbox.grid(row=3, column=1, padx=20, pady=20, sticky="nsew")

        self.load_global_settings()
        self.ui_log(f"系統就緒。設定檔路徑: {GLOBAL_CONFIG_FILE}")

        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    # --- 語言處理邏輯 ---
    def t(self, key):
        """取得當前語言的文字"""
        return self.TRANSLATIONS[self.lang].get(key, key)

    def change_language(self, choice):
        """切換語言並更新介面"""
        self.lang = "zh" if choice == "繁體中文" else "en"
        
        # 更新所有靜態文字
        self.lbl_global_title.configure(text=self.t("global_settings"))
        self.btn_save_global.configure(text=self.t("btn_save"))
        self.btn_update.configure(text=self.t("btn_update"))
        self.lbl_recent.configure(text=self.t("recent_files"))
        self.btn_select.configure(text=self.t("btn_browse"))
        
        self.lbl_entry_point.configure(text=self.t("entry_point"))
        self.lbl_output_name.configure(text=self.t("output_name"))
        self.lbl_repo_name.configure(text=self.t("repo_name"))
        self.btn_save_project.configure(text=self.t("btn_save_project"))
        
        self.lbl_panel.configure(text=self.t("panel_title"))
        self.btn_run.configure(text=self.t("btn_run"))
        self.btn_clean.configure(text=self.t("btn_clean"))
        self.btn_build.configure(text=self.t("btn_build"))
        self.btn_publish.configure(text=self.t("btn_publish"))
        self.lbl_lang.configure(text=self.t("lang_label"))

    def create_btn(self, parent, text_key, cmd, fg, hover):
        """建立按鈕並回傳物件 (方便後續修改文字)"""
        btn = ctk.CTkButton(parent, text=self.t(text_key), command=cmd, fg_color=fg, hover_color=hover, height=45)
        btn.pack(pady=10, padx=20, fill="x")
        return btn

    def ui_log(self, msg):
        try:
            # 使用 after 保證在主執行緒操作 UI
            self.after(0, lambda: (self.textbox.insert("end", str(msg) + "\n"), self.textbox.see("end")))
        except Exception:
            pass

    def set_entry(self, entry, text):
        entry.delete(0, "end")
        entry.insert(0, text)

    # --- 歷史與設定讀寫 ---
    def load_global_settings(self):
        if os.path.exists(GLOBAL_CONFIG_FILE):
            try:
                with open(GLOBAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.set_entry(self.entry_git_user, data.get("git_user", ""))
                    self.recent_projects = data.get("recent_projects", [])
                    self.update_history_menu()
                    # 嘗試讀取上次的語言設定 (選用)
                    saved_lang = data.get("language", "zh")
                    if saved_lang in ["zh", "en"]:
                        self.lang = saved_lang
                        self.lang_menu.set("繁體中文" if saved_lang == "zh" else "English")
                        self.change_language("繁體中文" if saved_lang == "zh" else "English")
            except Exception:
                pass

    def save_global_settings(self):
        data = {
            "git_user": self.entry_git_user.get(), 
            "recent_projects": self.recent_projects,
            "language": self.lang  # 儲存語言設定
        }
        try:
            # 確保目錄存在
            os.makedirs(os.path.dirname(GLOBAL_CONFIG_FILE), exist_ok=True)
            with open(GLOBAL_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            self.ui_log(f"全域設定已儲存 ({GLOBAL_CONFIG_FILE})")
        except Exception as e:
            self.ui_log(f"儲存失敗: {e}\n{traceback.format_exc()}")

    def update_history_menu(self):
        val = self.recent_projects[:10] if self.recent_projects else ["無紀錄"]
        try:
            self.history_menu.configure(values=val)
        except Exception:
            pass

    def add_to_history(self, path):
        if path in self.recent_projects:
            try:
                self.recent_projects.remove(path)
            except Exception:
                pass
        self.recent_projects.insert(0, path)
        self.update_history_menu()
        try:
            self.history_menu.set(path)
        except Exception:
            pass
        self.save_global_settings()

    def load_from_history(self, value):
        if value == "無紀錄" or not os.path.exists(value):
            return
        self.project_path = value
        self.lbl_path.configure(text=value)
        self.load_project_settings(value)
        self.add_to_history(value)
        self.ui_log(f"已從歷史載入: {value}")

    def select_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.project_path = folder
            self.lbl_path.configure(text=folder)
            self.load_project_settings(folder)
            self.add_to_history(folder)

    def load_project_settings(self, folder):
        cfg = os.path.join(folder, "devops_config.json")
        if os.path.exists(cfg):
            try:
                with open(cfg, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.set_entry(self.entry_entrypoint, data.get("entry_point", "src/main.py"))
                    self.set_entry(self.entry_output, data.get("output_name", "MyTool"))
                    self.set_entry(self.entry_git_repo, data.get("git_repo", ""))
            except Exception:
                pass
        else:
            self.set_entry(self.entry_git_repo, os.path.basename(folder))

    def save_project_settings(self):
        if not self.project_path:
            return
        data = {
            "entry_point": self.entry_entrypoint.get(),
            "output_name": self.entry_output.get(),
            "git_repo": self.entry_git_repo.get()
        }
        try:
            with open(os.path.join(self.project_path, "devops_config.json"), 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            self.ui_log("專案設定已儲存。")
        except Exception as e:
            self.ui_log(f"儲存專案設定失敗: {e}\n{traceback.format_exc()}")

    # --- 執行緒 ---
    def _run(self, func, *args):
        if self._closing:
            self.ui_log("系統正在關閉，無法啟動新工作。")
            return

        def wrapper():
            try:
                func(*args)
            except Exception as e:
                self.ui_log(f"工作執行失敗: {e}\n{traceback.format_exc()}")
            finally:
                with self._threads_lock:
                    try:
                        self._threads.remove(threading.current_thread())
                    except Exception:
                        pass

        th = threading.Thread(target=wrapper, daemon=True)
        with self._threads_lock:
            self._threads.append(th)
        th.start()

    def check_ready(self):
        if not self.project_path:
            messagebox.showerror("錯誤", "請先選擇專案！")
            return False
        return True

    def thread_run(self):
        if self.check_ready():
            self.save_project_settings()
            self._run(self.handler.action_run, self.project_path, self.entry_entrypoint.get())

    def thread_clean(self):
        if self.check_ready():
            self._run(self.handler.action_clean, self.project_path, "venv_build")

    def thread_build(self):
        if self.check_ready():
            self.save_project_settings()
            self._run(self.handler.action_build, self.project_path, "venv_build", self.entry_entrypoint.get(), self.entry_output.get())

    def thread_publish(self):
        if self.check_ready():
            self.save_project_settings()
            self.save_global_settings()
            self._run(self.handler.action_publish, self.project_path, self.entry_git_user.get(), self.entry_git_repo.get())

    def thread_check_update(self):
        self._run(self.updater.check_for_updates)

    def on_closing(self, force: bool = False):
        if self._closing and not force:
            return
        self._closing = True
        self.ui_log("應用程序正在關閉，停止背景工作...")

        try:
            self.handler.stop_all()
        except Exception:
            pass

        try:
            self.updater.close()
        except Exception:
            pass

        wait_start = time.time()
        timeout = 5 if not force else 1
        with self._threads_lock:
            threads_copy = list(self._threads)
        for t in threads_copy:
            remaining = timeout - (time.time() - wait_start)
            if remaining <= 0:
                break
            try:
                t.join(timeout=remaining)
            except Exception:
                pass

        with self._threads_lock:
            alive = [t for t in self._threads if t.is_alive()]
        if alive:
            try:
                self.handler.stop_all()
            except Exception:
                pass
            for t in alive:
                try:
                    t.join(timeout=1)
                except Exception:
                    pass

        try:
            self.destroy()
        except Exception:
            pass
        try:
            sys.exit(0)
        except Exception:
            pass


if __name__ == "__main__":
    app = App()
    app.mainloop()