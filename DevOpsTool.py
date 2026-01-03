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

# ================= 設定區 (開發者請修改這裡) =================
APP_NAME = "DevOpsMaster"
CURRENT_VERSION = "1.0.0"  # 每次發布新版前，請手動更新這裡的版本號

# GitHub 更新資訊
GITHUB_USER = "Riridesu"     # 你的 GitHub 帳號
GITHUB_REPO = "DevOpsTool"     # 你的儲存庫名稱

# 1. 版本檢查網址 (請在 Repo 放一個 version.txt，內容純文字如 1.0.1)
VERSION_URL = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/version.txt"
# 2. 新版執行檔下載點 (通常是 Releases 的直接下載連結)
EXE_DOWNLOAD_URL = f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}/releases/latest/download/DevOpsTool.exe"
# ==========================================================

# 設定外觀
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")

# --- 關鍵修改：設定檔路徑遷移至 AppData ---
# 這樣做可以確保 .exe 被覆蓋更新時，設定檔依然保留在系統目錄中
if os.name == 'nt':
    APP_DATA_DIR = os.path.join(os.getenv('APPDATA'), APP_NAME)
else:
    APP_DATA_DIR = os.path.join(os.path.expanduser('~'), ".config", APP_NAME)

if not os.path.exists(APP_DATA_DIR):
    os.makedirs(APP_DATA_DIR, exist_ok=True)

GLOBAL_CONFIG_FILE = os.path.join(APP_DATA_DIR, "tool_settings.json")

# (可選) 自動遷移舊設定：如果舊版設定檔在旁邊，自動搬進 AppData
local_config = "tool_settings.json"
if os.path.exists(local_config) and not os.path.exists(GLOBAL_CONFIG_FILE):
    try:
        shutil.copy(local_config, GLOBAL_CONFIG_FILE)
    except Exception:
        pass


class UpdateManager:
    """處理線上更新的核心邏輯"""
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
        self.log(f"正在檢查更新... (目前版本 v{CURRENT_VERSION})")
        try:
            # 1. 抓取遠端版本號
            response = self.session.get(VERSION_URL, timeout=5)
            if response.status_code != 200:
                self.log(f"檢查失敗: 無法連接伺服器 (Code {response.status_code})")
                return

            remote_ver_str = response.text.strip()
            self.log(f"遠端版本: v{remote_ver_str}")

            # 2. 比對版本
            if version.parse(remote_ver_str) > version.parse(CURRENT_VERSION):
                ans = messagebox.askyesno("發現新版本", f"發現新版本 v{remote_ver_str}！\n\n點擊「是」將自動下載並重啟更新。")
                if ans:
                    self.perform_update()
            else:
                self.log("目前已是最新版本。")
                messagebox.showinfo("檢查結果", "目前已是最新版本。")

        except Exception as e:
            self.log(f"更新檢查發生錯誤: {e}")

    def perform_update(self):
        """下載 -> 建立 Bat -> 關閉自己 -> Bat 替換檔案 -> 重啟"""
        self.log("開始下載更新檔...")
        temp_exe = "update_temp.exe"
        try:
            # 1. 下載新版 EXE
            with self.session.get(EXE_DOWNLOAD_URL, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(temp_exe, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

            self.log("下載完成，準備重新啟動...")

            # 2. 獲取當前執行檔名稱
            current_exe = sys.executable

            # 防呆：如果是用 Python 腳本執行的，不能刪除 python.exe
            if not current_exe.endswith(".exe") or "python" in os.path.basename(current_exe).lower():
                messagebox.showwarning("無法更新", "您正在使用 Python 直譯器執行腳本，\n無法進行 EXE 自我覆蓋測試。")
                try:
                    os.remove(temp_exe)
                except Exception:
                    pass
                return

            # 3. 建立更新用的批次檔 (Magic Script)
            # 邏輯：等待 2 秒 -> 刪除舊檔 -> 改名新檔 -> 啟動新檔 -> 刪除自己
            bat_script = f"""
@echo off
title Updating {APP_NAME}...
timeout /t 2 /nobreak > NUL
del "{current_exe}"
move "{temp_exe}" "{current_exe}"
start "" "{current_exe}"
del "%~f0"
"""
            with open("updater.bat", "w", encoding='utf-8') as bat:
                bat.write(bat_script)

            # 4. 啟動 Bat 並關閉主程式
            subprocess.Popen("updater.bat", shell=True)
            try:
                # 確保 app 關閉時不會留下任何子程序
                self.app.on_closing(force=True)
            except Exception:
                try:
                    self.app.destroy()
                except Exception:
                    pass
            # 正常結束程式
            sys.exit(0)

        except Exception as e:
            self.log(f"更新失敗: {e}")
            try:
                if os.path.exists(temp_exe):
                    os.remove(temp_exe)
            except Exception:
                pass
            messagebox.showerror("更新錯誤", str(e))


class TaskHandler:
    """負責執行具體任務，並支援 process-group 的可靠終止"""
    def __init__(self, log_callback):
        self.log = log_callback
        self.current_process = None
        self.process_lock = threading.Lock()
        self.stop_event = threading.Event()

    def _terminate_process_group(self, process):
        """嘗試以 process-group 方式終止 process 以及其子孫：
           - Windows: 發送 CTRL_BREAK_EVENT (需要 CREATE_NEW_PROCESS_GROUP)
           - Unix: 使用 os.killpg + SIGTERM / SIGKILL (需要 preexec_fn=os.setsid)
        """
        try:
            if process.poll() is not None:
                return
        except Exception:
            pass

        try:
            if os.name == 'nt':
                # 發送 CTRL_BREAK_EVENT 到 process group（需在 Popen 使用 CREATE_NEW_PROCESS_GROUP）
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                    self.log("已向 Windows process group 發送 CTRL_BREAK_EVENT。")
                except Exception as e:
                    self.log(f"發送 CTRL_BREAK_EVENT 失敗: {e}")
            else:
                # Unix: 以 process group 進行 SIGTERM
                try:
                    pgid = os.getpgid(process.pid)
                    os.killpg(pgid, signal.SIGTERM)
                    self.log("已對 process group 發送 SIGTERM。")
                except Exception as e:
                    self.log(f"對 process group 發送 SIGTERM 失敗: {e}")
        except Exception:
            pass

    def run_cmd(self, command, cwd=None, env=None, shell=True):
        """
        執行命令並回傳 exit code。
        - 啟動時建立新的 process group（Windows / Unix）。
        - 可透過 stop_all() 請求中止（會嘗試對整個 group 發送中止訊號）。
        """
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

            # 建立新的 process group，以便能以 group 方式殺掉所有衍生子程序
            if os.name == 'nt':
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["preexec_fn"] = os.setsid

            process = subprocess.Popen(command, **popen_kwargs)

            with self.process_lock:
                self.current_process = process
                self.stop_event.clear()

            # 逐行讀取輸出
            try:
                for line in iter(process.stdout.readline, ''):
                    if line:
                        self.log(line.rstrip())
                    if self.stop_event.is_set():
                        # 嘗試以 process-group 方式先做溫和結束
                        try:
                            self._terminate_process_group(process)
                        except Exception:
                            pass
                # 確保讀取完畢
            except Exception as e:
                self.log(f"讀取 subprocess 輸出錯誤: {e}")
                try:
                    if process.poll() is None:
                        process.terminate()
                except Exception:
                    pass

            # 等待結束，若超時則採取更強力的手段
            try:
                rc = process.wait(timeout=10)
            except Exception:
                try:
                    # 如果還在運行，先嘗試以 process-group kill（Unix），或 kill (Windows)
                    try:
                        if os.name == 'nt':
                            # Windows fallback: 對 process 嘗試 kill
                            process.kill()
                        else:
                            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except Exception:
                        pass
                except Exception:
                    pass
                # 最後等待不設 timeout
                try:
                    rc = process.wait()
                except Exception:
                    rc = -1

            return rc
        except Exception as e:
            self.log(f"指令錯誤: {e}")
            return 1
        finally:
            with self.process_lock:
                self.current_process = None
                self.stop_event.clear()

    def stop_all(self):
        """
        請求停止目前正在執行的 process（若有）。
        - 會設定 stop_event（讓正在讀 stdout 的 loop 看到），然後嘗試以 process-group 的方式結束。
        - 若不回應，會 fallback 到 terminate / kill。
        """
        self.stop_event.set()
        with self.process_lock:
            p = self.current_process
            if not p:
                return

            try:
                # 優先嘗試 process-group 溫和結束
                try:
                    self._terminate_process_group(p)
                except Exception:
                    pass

                # 等一小段時間看是否結束
                try:
                    p.wait(timeout=3)
                    return
                except Exception:
                    pass

                # fallback: try terminate / kill
                try:
                    if p.poll() is None:
                        p.terminate()
                except Exception:
                    pass

                try:
                    p.wait(timeout=2)
                except Exception:
                    # 最後強制 kill / group kill
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
        self.run_cmd(f'python "{entry_point}"', cwd=project_path)

    def action_build(self, project_path, venv_name, entry_point, output_name):
        self.log("--- 開始建置流程 ---")
        venv_path = os.path.join(project_path, venv_name)
        if not os.path.exists(venv_path):
            self.log("建立虛擬環境...")
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

        self.run_cmd(f'"{pip_cmd}" install {" ".join(pkgs)}', cwd=project_path)
        cmd = f'"{py_cmd}" -m PyInstaller -F --clean --name "{output_name}" "{entry_point}" --distpath ./dist'
        self.run_cmd(cmd, cwd=project_path)
        self.log(f"打包完成: dist/{output_name}.exe")

    def action_publish(self, project_path, user, repo):
        self.log(f"--- 發布至 GitHub ({user}/{repo}) ---")
        if not os.path.exists(os.path.join(project_path, ".git")):
            self.run_cmd("git init", cwd=project_path)

        # 先嘗試移除已存在的 remote（避免重複）
        try:
            self.run_cmd(f"git remote remove origin", cwd=project_path)
        except Exception:
            pass

        self.run_cmd(f"git remote add origin https://github.com/{user}/{repo}.git", cwd=project_path)
        self.run_cmd("git add .", cwd=project_path)
        self.run_cmd('git commit -m "Update via DevOps Tool"', cwd=project_path)
        if self.run_cmd("git push -u origin main", cwd=project_path) != 0:
            self.run_cmd("git push -u origin master", cwd=project_path)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{CURRENT_VERSION}")
        self.geometry("900x700")

        self.handler = TaskHandler(log_callback=self.ui_log)
        self.updater = UpdateManager(app_instance=self, log_callback=self.ui_log)

        self.project_path = None
        self.recent_projects = []

        # 用來追蹤 worker threads（非 daemon），以便在關閉時等待或中斷
        self._threads = []
        self._threads_lock = threading.Lock()
        self._closing = False  # 關閉旗標

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # === 1. 全域設定與更新 ===
        self.global_frame = ctk.CTkFrame(self)
        self.global_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=20, pady=(15, 5))

        ctk.CTkLabel(self.global_frame, text="⚙️ GitHub User", font=("Arial", 12, "bold")).pack(side="left", padx=10)
        self.entry_git_user = ctk.CTkEntry(self.global_frame, width=150)
        self.entry_git_user.pack(side="left", padx=5)

        self.btn_save_global = ctk.CTkButton(self.global_frame, text="儲存", width=60, fg_color="#444", command=self.save_global_settings)
        self.btn_save_global.pack(side="left", padx=5)

        # 新增：檢查更新按鈕
        self.btn_update = ctk.CTkButton(self.global_frame, text="⟳ 檢查更新", width=100, fg_color="#E67E22", hover_color="#D35400", command=self.thread_check_update)
        self.btn_update.pack(side="right", padx=10)

        self.lbl_ver = ctk.CTkLabel(self.global_frame, text=f"v{CURRENT_VERSION}", text_color="gray")
        self.lbl_ver.pack(side="right", padx=5)

        # === 2. 專案選擇 ===
        self.select_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.select_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=20, pady=5)

        ctk.CTkLabel(self.select_frame, text="最近開啟：").pack(side="left", padx=(0, 5))
        self.history_menu = ctk.CTkOptionMenu(self.select_frame, values=["無紀錄"], command=self.load_from_history, width=300)
        self.history_menu.pack(side="left", padx=5)

        self.btn_select = ctk.CTkButton(self.select_frame, text="📂 瀏覽新資料夾", command=self.select_folder)
        self.btn_select.pack(side="left", padx=10)

        self.lbl_path = ctk.CTkLabel(self.select_frame, text="", text_color="gray")
        self.lbl_path.pack(side="left", padx=10)

        # === 3. 專案設定 ===
        self.project_config_frame = ctk.CTkFrame(self)
        self.project_config_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=5)

        def create_entry(parent, label, default, col):
            lbl = ctk.CTkLabel(parent, text=label, font=("Arial", 12, "bold"))
            lbl.grid(row=0, column=col, padx=10, pady=5, sticky="w")
            entry = ctk.CTkEntry(parent, width=180)
            entry.grid(row=1, column=col, padx=10, pady=5)
            entry.insert(0, default)
            return entry

        self.entry_entrypoint = create_entry(self.project_config_frame, "入口檔案", "src/main.py", 0)
        self.entry_output = create_entry(self.project_config_frame, "輸出檔名 (.exe)", "MyTool", 1)
        self.entry_git_repo = create_entry(self.project_config_frame, "Repo 名稱", "MyRepo", 2)

        ctk.CTkButton(self.project_config_frame, text="💾 儲存專案設定", width=120, fg_color="#555", command=self.save_project_settings).grid(row=1, column=3, padx=20)

        # === 4. 操作面板 ===
        self.sidebar = ctk.CTkFrame(self, width=180, corner_radius=0)
        self.sidebar.grid(row=3, column=0, sticky="nsew", pady=10)
        ctk.CTkLabel(self.sidebar, text="操作面板", font=("Arial", 16, "bold")).pack(pady=20)

        self.create_btn("▶ 執行測試", self.thread_run, "#2CC985", "#229A66")
        self.create_btn("🗑 清理環境", self.thread_clean, "#E74C3C", "#C0392B")
        self.create_btn("🔨 一鍵打包", self.thread_build, "#3498DB", "#2980B9")
        self.create_btn("☁ 發布", self.thread_publish, "#9B59B6", "#8E44AD")

        self.textbox = ctk.CTkTextbox(self, font=("Consolas", 12))
        self.textbox.grid(row=3, column=1, padx=20, pady=20, sticky="nsew")

        self.load_global_settings()
        self.ui_log(f"系統就緒。設定檔路徑: {GLOBAL_CONFIG_FILE}")

        # 綁定關閉事件
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    # --- 輔助函式 ---
    def create_btn(self, text, cmd, fg, hover):
        ctk.CTkButton(self.sidebar, text=text, command=cmd, fg_color=fg, hover_color=hover, height=45).pack(pady=10, padx=20, fill="x")

    def ui_log(self, msg):
        try:
            # 使用 after 在主線程更新 UI
            self.after(0, lambda: (self.textbox.insert("end", str(msg) + "\n"), self.textbox.see("end")))
        except Exception:
            pass

    def set_entry(self, entry, text):
        entry.delete(0, "end")
        entry.insert(0, text)

    # --- 歷史與設定讀寫 (從 AppData) ---
    def load_global_settings(self):
        if os.path.exists(GLOBAL_CONFIG_FILE):
            try:
                with open(GLOBAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.set_entry(self.entry_git_user, data.get("git_user", ""))
                    self.recent_projects = data.get("recent_projects", [])
                    self.update_history_menu()
            except Exception:
                pass

    def save_global_settings(self):
        data = {"git_user": self.entry_git_user.get(), "recent_projects": self.recent_projects}
        try:
            with open(GLOBAL_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            self.ui_log(f"全域設定已儲存 ({GLOBAL_CONFIG_FILE})")
        except Exception as e:
            self.ui_log(f"儲存失敗: {e}")

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
            self.ui_log(f"儲存專案設定失敗: {e}")

    # --- 執行緒 ---
    def _run(self, func, *args):
        """
        啟動一個非 daemon 的 thread 並追蹤，關閉時可以 join。
        """
        if self._closing:
            self.ui_log("系統正在關閉，無法啟動新工作。")
            return

        def wrapper():
            try:
                func(*args)
            except Exception as e:
                self.ui_log(f"工作執行失敗: {e}")
            finally:
                # 執行完畢後將自己從列表移除
                with self._threads_lock:
                    try:
                        self._threads.remove(threading.current_thread())
                    except Exception:
                        pass

        th = threading.Thread(target=wrapper, daemon=False)
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

    # 新增更新執行緒
    def thread_check_update(self):
        self._run(self.updater.check_for_updates)

    def on_closing(self, force: bool = False):
        """
        關閉應用程式的清理流程：
        - 將 _closing 設為 True，阻止新工作啟動
        - 要求 handler 停止目前子程序（使用 process-group 殺死策略）
        - 等待工作 thread 結束（最多幾秒），若 force=True 則快速結束
        """
        if self._closing and not force:
            return
        self._closing = True
        self.ui_log("應用程序正在關閉，停止背景工作...")

        # 1) 停所有正在執行的子程序
        try:
            self.handler.stop_all()
        except Exception:
            pass

        # 2) 關閉 updater session
        try:
            self.updater.close()
        except Exception:
            pass

        # 3) 等待 worker threads 結束（短暫等待）
        wait_start = time.time()
        timeout = 5 if not force else 1
        # 複製一份 list 避免 race condition
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

        # 若仍有尚未結束的 thread，記錄並嘗試強制 stop
        with self._threads_lock:
            alive = [t for t in self._threads if t.is_alive()]
        if alive:
            self.ui_log(f"有 {len(alive)} 個工作尚未完成，將強制終止 (若有子程序會被 kill)。")
            try:
                self.handler.stop_all()
            except Exception:
                pass
            # 最後嘗試再等一小段時間
            for t in alive:
                try:
                    t.join(timeout=1)
                except Exception:
                    pass

        # 最後關閉 UI
        try:
            self.destroy()
        except Exception:
            pass
        # 確保整個進程結束
        try:
            sys.exit(0)
        except Exception:
            pass


if __name__ == "__main__":
    app = App()
    app.mainloop()