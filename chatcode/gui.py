"""图形界面：选工作区 → 点启动 → 复制地址 → 去网页对话里改造代码。

沿用之前隧道启动器的交互习惯（端口、启停、日志、复制到剪贴板），
把 MCP 服务和穿透合成一个窗口。
"""

from __future__ import annotations

import argparse
import gc
import json
import queue
import threading
import uuid
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import VERSION, Settings, app_data_dir, build_arg_parser, settings_from_args, parse_language_server
from .config import APP_TITLE as PRODUCT_TITLE
from .runtime import ServiceRuntime
from .gui_profiles import load_profile, save_profile
from .proxy import ProxyDetection, detect_proxy, display_proxy

APP_TITLE = f"{PRODUCT_TITLE} v{VERSION} · 让网页对话改造本地项目"
DESKTOP_NOTICE = "桌面自动化：暂不使用"

# 界面显示友好文案，取值时映射回模式名。两种常用模式：直连 和 需授权。
AUTH_MODE_LABELS = {
    "直连（不用授权，推荐）": "path",
    "授权（多一道口令）": "oauth",
    "本地客户端（bearer）": "bearer",
    "完全开放（任何人可访问）": "none",
}

TUNNEL_LABELS = {
    "Cloudflare（默认）": "cloudflared",
    "cpolar（国内）": "cpolar",
    "frp（自建服务器）": "frp",
    "不穿透（仅本机）": "none",
}


class App:
    def __init__(self, root: tk.Tk, args: argparse.Namespace | None = None) -> None:
        self.root = root
        self.server = None
        self.runtime = None
        self.args, self.recent_workspaces = load_profile(args or build_arg_parser().parse_args([]))
        self._refresh_id = None
        self._refreshing = False
        self._refresh_again = False
        self._switching = False
        self._view_generation = 0
        self._task_offset = 0
        self._ui_queue = queue.SimpleQueue()
        self._destroyed = False
        self._workers = []
        self._dispatch_id = None
        self.tunnel = None
        self.public_url = ""

        root.title(APP_TITLE)
        root.geometry("900x720")
        root.minsize(820, 640)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        self._build()
        root.bind("<Destroy>", self._on_destroy, add="+")
        self._dispatch_id = root.after(25, self._drain_ui)

    def _post(self, callback, *args):
        # 后台线程只入队，不直接调用 Tcl；销毁窗口后不会残留 Tcl 回调。
        if not self._destroyed:
            self._ui_queue.put((callback, args))

    def _drain_ui(self):
        self._dispatch_id = None
        try:
            for _ in range(100):
                try:
                    callback, args = self._ui_queue.get_nowait()
                except queue.Empty:
                    break
                if self._destroyed:
                    break
                callback(*args)
        finally:
            if not self._destroyed:
                self._dispatch_id = self.root.after(25, self._drain_ui)

    def _on_destroy(self, event):
        if event.widget is self.root:
            self._destroyed = True
            if self._dispatch_id is not None:
                self.root.after_cancel(self._dispatch_id)
                self._dispatch_id = None
            # 工作线程只操作数据与回调队列；窗口释放前等其结束，避免 Tk
            # 引用最终在工作线程中释放，触发 Tcl_AsyncDelete。
            for worker in self._workers:
                worker.join()
            self._workers.clear()
            while not self._ui_queue.empty():
                self._ui_queue.get_nowait()

    def _launch_worker(self, work):
        self._workers = [worker for worker in self._workers if worker.is_alive()]
        worker = threading.Thread(target=work, daemon=True)
        self._workers.append(worker)
        worker.start()

    # ---------------- UI ----------------

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}

        top = ttk.LabelFrame(self.root, text="基本设置")
        top.pack(fill="x", **pad)

        row1 = ttk.Frame(top)
        row1.pack(fill="x", **pad)
        ttk.Label(row1, text="项目目录：").pack(side="left")
        self.workspace_var = tk.StringVar(value=self.args.workspace or str(Path.cwd()))
        self.workspace_input = ttk.Combobox(row1, textvariable=self.workspace_var, values=self.recent_workspaces)
        self.workspace_input.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.workspace_input.bind("<<ComboboxSelected>>", self._load_workspace_profile)
        self.directory_btn = ttk.Button(row1, text="选择…", command=self.pick_workspace)
        self.directory_btn.pack(side="left")
        self.switch_btn = ttk.Button(row1, text="切换项目", command=self.switch_workspace, state="disabled")
        self.switch_btn.pack(side="left", padx=(6, 0))
        self.workspace_input.bind("<Return>", lambda event: self.switch_workspace())

        row2 = ttk.Frame(top)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="本地端口：").pack(side="left")
        self.port_var = tk.StringVar(value=str(self.args.port))
        ttk.Entry(row2, textvariable=self.port_var, width=8, justify="center").pack(side="left", padx=(0, 16))

        ttk.Label(row2, text="穿透方式：").pack(side="left")
        self.tunnel_var = tk.StringVar(value=next(k for k, v in TUNNEL_LABELS.items() if v == self.args.tunnel))
        ttk.Combobox(
            row2,
            textvariable=self.tunnel_var,
            values=tuple(TUNNEL_LABELS),
            state="readonly",
            width=18,
        ).pack(side="left", padx=(0, 16))

        ttk.Label(row2, text="连接方式：").pack(side="left")
        self.auth_var = tk.StringVar(value=next(k for k, v in AUTH_MODE_LABELS.items() if v == self.args.auth_mode))
        ttk.Combobox(
            row2,
            textvariable=self.auth_var,
            values=tuple(AUTH_MODE_LABELS),
            state="readonly",
            width=20,
        ).pack(side="left")

        row3 = ttk.Frame(top)
        row3.pack(fill="x", **pad)
        ttk.Label(row3, text="代理（可选，能提速）：").pack(side="left")
        self.proxy_var = tk.StringVar(value=self.args.proxy or "")
        ttk.Entry(row3, textvariable=self.proxy_var, width=28).pack(side="left", padx=(0, 8))
        ttk.Button(row3, text="自动探测", command=self.auto_detect_proxy).pack(side="left")
        ttk.Label(row3, text="留空自动探测", foreground="#666").pack(side="left", padx=(8, 0))
        self.advanced_btn = ttk.Button(row3, text="高级连接设置", command=self.connection_settings)
        self.advanced_btn.pack(side="right")

        btn_row = ttk.Frame(self.root)
        btn_row.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btn_row, text="启动服务 + 穿透", command=self.start)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(btn_row, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        self.status_var = tk.StringVar(value="状态：未启动")
        ttk.Label(btn_row, textvariable=self.status_var, foreground="#666").pack(side="left", padx=(16, 0))
        self.desktop_var = tk.StringVar(value=DESKTOP_NOTICE)
        ttk.Label(self.root, textvariable=self.desktop_var, foreground="#666").pack(anchor="w", padx=14)

        url_frame = ttk.LabelFrame(self.root, text="公网 MCP 地址（填到 ChatGPT 连接器里）")
        url_frame.pack(fill="x", **pad)
        self.url_var = tk.StringVar(value="—— 未启动 ——")
        tk.Label(
            url_frame,
            textvariable=self.url_var,
            font=("Microsoft YaHei UI", 11, "bold"),
            fg="#0a6ccd",
            wraplength=740,
            justify="left",
        ).pack(padx=10, pady=8, anchor="w")
        url_btns = ttk.Frame(url_frame)
        url_btns.pack(padx=10, pady=(0, 8), anchor="w")
        ttk.Button(url_btns, text="复制地址", command=self.copy_url).pack(side="left", padx=(0, 8))
        ttk.Button(url_btns, text="复制接入步骤", command=self.copy_steps).pack(side="left")

        pwd_frame = ttk.Frame(self.root)
        pwd_frame.pack(fill="x", **pad)
        ttk.Label(pwd_frame, text="授权口令：").pack(side="left")
        self.pwd_var = tk.StringVar(value="——")
        tk.Label(pwd_frame, textvariable=self.pwd_var, font=("Consolas", 11, "bold"), fg="#c0392b").pack(side="left", padx=(0, 10))
        ttk.Button(pwd_frame, text="复制", command=self.copy_password).pack(side="left")

        log_frame = ttk.LabelFrame(self.root, text="运行状态")
        log_frame.pack(fill="both", expand=True, **pad)
        self.tabs = ttk.Notebook(log_frame)
        self.tabs.pack(fill="both", expand=True, padx=6, pady=6)
        log_tab, jobs_tab, changes_tab, tasks_tab = (ttk.Frame(self.tabs) for _ in range(4))
        self.tabs.add(log_tab, text="运行日志")
        self.tabs.add(tasks_tab, text="任务中心")
        self.tabs.add(jobs_tab, text="后台进程")
        self.tabs.add(changes_tab, text="文件改动")
        self.log_text = tk.Text(
            log_tab, height=12, state="disabled",
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
        )
        scroll = ttk.Scrollbar(log_tab, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll.pack(side="right", fill="y", pady=8)

        job_buttons = ttk.Frame(jobs_tab)
        job_buttons.pack(fill="x", padx=6, pady=5)
        ttk.Button(job_buttons, text="刷新", command=self.refresh_panels).pack(side="left")
        ttk.Button(job_buttons, text="查看日志", command=self.show_process).pack(side="left", padx=6)
        ttk.Button(job_buttons, text="停止选中进程", command=self.stop_selected_process).pack(side="left")
        self.process_tree = ttk.Treeview(jobs_tab, columns=("state", "command"), show="tree headings", height=6)
        self.process_tree.heading("#0", text="进程 ID")
        self.process_tree.column("#0", width=100, stretch=False)
        self.process_tree.heading("state", text="状态")
        self.process_tree.column("state", width=100, stretch=False)
        self.process_tree.heading("command", text="命令")
        self.process_tree.pack(fill="both", expand=True, padx=6, pady=6)
        change_buttons = ttk.Frame(changes_tab)
        change_buttons.pack(fill="x", padx=6, pady=5)
        ttk.Button(change_buttons, text="刷新", command=self.refresh_panels).pack(side="left")
        ttk.Button(change_buttons, text="查看 diff", command=self.show_change).pack(side="left", padx=6)
        ttk.Button(change_buttons, text="撤销选中改动", command=self.undo_selected_change).pack(side="left")
        self.change_tree = ttk.Treeview(changes_tab, columns=("state", "files"), show="tree headings", height=6)
        self.change_tree.heading("#0", text="改动 ID")
        self.change_tree.column("#0", width=100, stretch=False)
        self.change_tree.heading("state", text="状态")
        self.change_tree.column("state", width=110, stretch=False)
        self.change_tree.heading("files", text="文件")
        self.change_tree.pack(fill="both", expand=True, padx=6, pady=6)

        task_buttons = ttk.Frame(tasks_tab)
        task_buttons.pack(fill="x", padx=6, pady=5)
        for label, command in (("新建", self.create_task), ("刷新", self.refresh_panels),
                               ("查看恢复包", self.show_task), ("恢复", lambda: self.update_task("running")),
                               ("暂停", lambda: self.update_task("paused")), ("取消", lambda: self.update_task("cancelled")),
                               ("上一页", lambda: self.task_page(-1)), ("下一页", lambda: self.task_page(1))):
            ttk.Button(task_buttons, text=label, command=command, width=10).pack(side="left", padx=2)
        copy_buttons = ttk.Frame(tasks_tab)
        copy_buttons.pack(fill="x", padx=6, pady=2)
        for label, command in (("副本差异", lambda: self.isolated_task_action("task_changes", show=True)),
                               ("合入原项目", lambda: self.isolated_task_action("task_apply")),
                               ("预览撤回", lambda: self.isolated_task_action("task_revert", show=True)),
                               ("撤回合入", lambda: self.isolated_task_action("task_revert"))):
            ttk.Button(copy_buttons, text=label, command=command).pack(side="left", padx=2)
        maintenance = ttk.Frame(tasks_tab)
        maintenance.pack(fill="x", padx=6, pady=2)
        self.show_archived = tk.BooleanVar(value=False)
        def toggle_archived():
            self._task_offset = 0
            self.refresh_panels()
        ttk.Checkbutton(maintenance, text="显示归档任务", variable=self.show_archived, command=toggle_archived).pack(side="left")
        ttk.Button(maintenance, text="归档 / 恢复", command=self.archive_task).pack(side="left", padx=2)
        ttk.Button(maintenance, text="导出历史", command=lambda: self.isolated_task_action("task_export", show=True)).pack(side="left", padx=2)
        ttk.Button(maintenance, text="容量 / 索引", command=self.project_health).pack(side="left", padx=2)
        ttk.Button(maintenance, text="清理归档副本…", command=self.cleanup_tasks).pack(side="left", padx=2)
        ttk.Label(tasks_tab, text="暂停会阻止该任务的新修改，已运行命令继续执行。取消会停止本服务所属进程，保留已有修改。",
                  foreground="#666").pack(anchor="w", padx=8)
        self.task_tree = ttk.Treeview(tasks_tab, columns=("state", "title"), show="tree headings", height=6)
        self.task_tree.heading("#0", text="任务 ID")
        self.task_tree.column("#0", width=100, stretch=False)
        self.task_tree.heading("state", text="状态")
        self.task_tree.column("state", width=110, stretch=False)
        self.task_tree.heading("title", text="目标")
        self.task_tree.pack(fill="both", expand=True, padx=6, pady=6)

        self.log("选择项目目录，点「启动服务 + 穿透」，把得到的地址填进 ChatGPT 的自定义连接器。")
        self.log("提示：cloudflared 的免费隧道域名每次启动都会变，重启后需要更新一次地址。")
        self.log("      想提速就在「代理」里填上你的本地代理（如 http://127.0.0.1:7897）。")

    def log(self, message: str) -> None:
        def append() -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            if int(self.log_text.index("end-1c").split(".")[0]) > 5000:
                self.log_text.delete("1.0", "1000.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        if threading.current_thread() is threading.main_thread():
            append()
        else:
            self._post(append)

    def set_status(self, text: str) -> None:
        # 所有调用均在 Tk 主线程；就绪 URL 与状态必须在同一回调内更新。
        self.status_var.set("状态：" + text)

    # ---------------- 动作 ----------------

    def pick_workspace(self) -> None:
        chosen = filedialog.askdirectory(title="选择要暴露给对话的项目目录")
        if chosen:
            self.workspace_var.set(chosen)
            self._load_workspace_profile()

    def _load_workspace_profile(self, event=None):
        if self.server is not None:
            self.switch_workspace()
            return
        args = argparse.Namespace(**vars(self.args))
        args.workspace = self.workspace_var.get()
        self.args, recent = load_profile(args)
        self.workspace_input.configure(values=recent)
        self.port_var.set(str(self.args.port))
        self.auth_var.set(next(k for k, v in AUTH_MODE_LABELS.items() if v == self.args.auth_mode))
        self.tunnel_var.set(next(k for k, v in TUNNEL_LABELS.items() if v == self.args.tunnel))
        self.proxy_var.set(self.args.proxy or "")

    def switch_workspace(self):
        runtime = self.runtime
        if runtime is None or runtime.server is None or self._switching:
            return
        target = Path(self.workspace_var.get().strip())
        if not target.is_dir():
            messagebox.showerror("切换失败", "项目目录无效")
            self.workspace_var.set(str(runtime.server.workspace.root))
            return
        args = argparse.Namespace(**vars(self.args))
        args.workspace = str(target.resolve())
        profile, _ = load_profile(args)
        # 项目只读取语言服务配置，不改变当前连接、代理、认证或连接级权限。
        from dataclasses import replace
        settings = replace(runtime.settings, workspace=target.resolve(), projects=[],
                           language_servers=dict(profile.language_server))
        self._switching = True
        self.switch_btn.configure(state="disabled")
        self.directory_btn.configure(state="disabled")
        self.log(f"正在打开项目：{target}；现有连接保持运行")
        def work():
            try:
                project, error = runtime.switch_workspace(target, settings=settings), None
            except Exception as exc:
                project, error = None, exc
            def finish():
                if self.runtime is not runtime or self.server is None:
                    return
                self._switching = False
                self.switch_btn.configure(state="normal")
                self.directory_btn.configure(state="normal")
                if error is not None:
                    self.workspace_var.set(str(runtime.server.workspace.root))
                    messagebox.showerror("切换失败", str(error))
                    return
                self._view_generation += 1
                self._refreshing = False
                self._refresh_again = False
                self.workspace_var.set(str(project.workspace.root))
                self.args.workspace = str(project.workspace.root)
                self.args.language_server = list(project.settings.language_servers.items())
                self.recent_workspaces = list(dict.fromkeys([*self.recent_workspaces, str(project.workspace.root)]))[-20:]
                self.workspace_input.configure(values=self.recent_workspaces)
                self._task_offset = 0
                for tree in (self.change_tree, self.process_tree, self.task_tree):
                    tree.delete(*tree.get_children())
                try:
                    save_profile(project.settings)
                except OSError as exc:
                    self.log(f"项目配置未能保存：{exc}")
                self.log("项目已切换；MCP 地址、认证和隧道不变。旧任务仍属于原项目。")
                self.refresh_panels()
            try:
                self._post(finish)
            except (RuntimeError, tk.TclError):
                pass
        self._launch_worker(work)

    def auto_detect_proxy(self) -> None:
        self.log("正在探测本机代理…")
        def work() -> None:
            found = detect_proxy()
            self._post(self._proxy_result, found)
        self._launch_worker(work)

    def _proxy_result(self, found: ProxyDetection) -> None:
        if found.proxy:
            self.proxy_var.set(found.proxy)
            self.log(f"探测到代理：{display_proxy(found.proxy)}（来源：{found.source}）。{found.message}")
        else:
            self.log(found.message)

    def _settings(self) -> Settings | None:
        workspace = Path(self.workspace_var.get().strip())
        if not workspace.is_dir():
            messagebox.showerror("错误", "项目目录无效，请重新选择")
            return None
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("错误", "端口必须是数字")
            return None
        if not 1 <= port <= 65535:
            messagebox.showerror("错误", "端口必须介于 1 和 65535 之间")
            return None
        args = argparse.Namespace(**vars(self.args))
        args.workspace = str(workspace.resolve())
        args.port = port
        args.auth_mode = AUTH_MODE_LABELS.get(self.auth_var.get(), "path")
        args.tunnel = TUNNEL_LABELS.get(self.tunnel_var.get(), "cloudflared")
        args.proxy = self.proxy_var.get().strip() or None
        return settings_from_args(args)

    def start(self) -> None:
        if self.server is not None:
            messagebox.showinfo("提示", "服务已在运行，请先停止")
            return
        settings = self._settings()
        if settings is None:
            return

        self.settings = settings
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.set_status("正在启动…")
        self.url_var.set("—— 正在打通隧道，请稍候 ——")
        self.log("-" * 56)
        self.log(f"工作区：{settings.workspace}")
        self.log(f"本地端口：{settings.port}  穿透：{settings.tunnel}  认证：{settings.auth_mode}")
        if settings.read_only:
            self.log("只读模式：网页只能读取项目；命令执行和语言服务进程已禁用")
        self.log("文件访问策略：" + ("允许敏感文件" if settings.allow_sensitive_files else "默认屏蔽 .env / 私钥") + "；不属于命令沙箱")

        runtime = ServiceRuntime(settings, log=self.log,
                                 on_ready=lambda url: self._post(self._ready, url, runtime))
        self.runtime = runtime
        try:
            runtime.start()
            self.server = runtime.server
            self.advanced_btn.configure(state="disabled")
            self.workspace_input.configure(state="normal")
            self.directory_btn.configure(state="normal")
            self.switch_btn.configure(state="normal")
            try:
                save_profile(settings)
            except OSError as exc:
                self.log(f"项目配置未能保存：{exc}")
            self.refresh_panels()
        except (Exception, SystemExit) as exc:
            runtime.stop()
            self.runtime = None
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.set_status("启动失败")
            messagebox.showerror("启动失败", str(exc))
            return

        if settings.auth_mode == "oauth":
            self.pwd_var.set(settings.oauth_password)
            self.log(f"授权口令：{settings.oauth_password}")
        elif settings.auth_mode == "path":
            self.pwd_var.set("（无需口令）")
            self.log("path 模式：凭据在地址末尾，客户端选「无认证」直接连，不会弹授权页")
        elif settings.auth_mode == "bearer":
            self.pwd_var.set("（bearer 模式）")
            self.log(f"Bearer 令牌：{settings.bearer_token}")
        elif settings.auth_mode == "none":
            self.pwd_var.set("（无认证）")
            self.log("警告：无认证模式，任何拿到地址的人都能读写工作区")

    def _ready(self, url: str, runtime: ServiceRuntime | None = None) -> None:
        if self.runtime is None or self.server is None or (runtime is not None and runtime is not self.runtime):
            return
        settings = self.settings
        origin = url.rstrip("/") if url else f"http://127.0.0.1:{settings.port}"
        self.public_url = origin
        if url:
            settings.discovered_public_url = origin
        mcp_url = settings.mcp_url()
        if url:
            self.url_var.set(mcp_url)
            self.set_status("运行中 ✅")
            self.log(f"✅ 隧道已打通：{mcp_url}")
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(mcp_url)
                self.log("（地址已自动复制到剪贴板）")
            except tk.TclError:
                pass
        else:
            self.url_var.set(mcp_url + "  （仅本地，未打通公网）")
            self.set_status("仅本地")
            self.log("未取得公网地址，只能在本机访问")

    def stop(self) -> None:
        if self._refresh_id is not None:
            self.root.after_cancel(self._refresh_id)
            self._refresh_id = None
        self.advanced_btn.configure(state="normal")
        self.workspace_input.configure(state="normal")
        self.directory_btn.configure(state="normal")
        self._refreshing = False
        self._switching = False
        self._view_generation += 1
        self.switch_btn.configure(state="disabled")
        if self.runtime is not None:
            self.runtime.stop()
            self.runtime = None
        self.server = None
        self.tunnel = None
        self.public_url = ""
        self.url_var.set("—— 已停止 ——")
        self.desktop_var.set(DESKTOP_NOTICE)
        self.set_status("已停止")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.log("已停止。")

    def connection_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("高级连接设置")
        dialog.transient(self.root)
        fields = {}
        labels = {"host": "本地监听地址", "public_url": "固定公网 origin", "tunnel_binary": "隧道程序路径",
                  "frp_server_addr": "frp 服务器", "frp_server_port": "frp 服务器端口",
                  "frp_token": "frp 认证 token", "cpolar_authtoken": "cpolar token",
                  "execution_mode": "命令执行模式（host / native）",
                  "sandbox_memory_mb": "原生沙箱内存 MiB（128..8192）", "storage_quota_mb": "项目副本容量预算 MiB"}
        for row, (name, label) in enumerate(labels.items()):
            ttk.Label(dialog, text=label).grid(row=row, column=0, padx=12, pady=5, sticky="w")
            value = tk.StringVar(value=str(getattr(self.args, name)))
            ttk.Entry(dialog, textvariable=value, width=42, show="*" if name.endswith("token") else "").grid(row=row, column=1, padx=12, pady=5)
            fields[name] = value
        booleans = {}
        for index, (name, label) in enumerate((("read_only", "只读模式（同时禁用命令和语言服务）"),
                ("allow_sensitive_files", "允许访问 .env / 私钥文件"), ("no_exec", "禁用命令执行"),
                ("no_proxy", "禁用自动代理"), ("no_gzip", "禁用压缩")), start=len(labels)):
            value = tk.BooleanVar(value=getattr(self.args, name))
            ttk.Checkbutton(dialog, text=label, variable=value).grid(row=index, column=1, sticky="w", padx=12, pady=3)
            booleans[name] = value
        server_row = len(labels) + len(booleans)
        ttk.Label(dialog, text="语言服务命令（JSON 对象，可选）").grid(row=server_row, column=0, columnspan=2, padx=12, pady=(8, 0), sticky="w")
        language_commands = tk.StringVar(value=json.dumps(dict(self.args.language_server), ensure_ascii=False))
        ttk.Entry(dialog, textvariable=language_commands, width=62).grid(row=server_row+1, column=0, columnspan=2, padx=12, pady=5)
        def detect_languages():
            from .language_setup import detect
            found = detect(Path(self.workspace_var.get()).resolve())
            configured = json.loads(language_commands.get() or "{}")
            if not isinstance(configured, dict):
                configured = {}
            for name, command in found["detected_language_servers"].items():
                configured.setdefault(name, command)
            language_commands.set(json.dumps(configured, ensure_ascii=False))
            self._show_text("本机语言工具检测", json.dumps(found, ensure_ascii=False, indent=2))
        ttk.Button(dialog, text="检测并填入 Java / Go 语言服务", command=detect_languages).grid(row=server_row+2, columnspan=2)
        def save():
            try:
                values = {key: value.get().strip() for key, value in fields.items()}
                values["frp_server_port"] = int(values["frp_server_port"])
                if values["execution_mode"] not in ("host", "native"):
                    raise ValueError("执行模式必须是 host 或 native")
                for key, low, high in (("sandbox_memory_mb", 128, 8192), ("storage_quota_mb", 128, 65536)):
                    values[key] = int(values[key])
                    if not low <= values[key] <= high:
                        raise ValueError(f"{key} 必须介于 {low} 和 {high}")
                if not 1 <= values["frp_server_port"] <= 65535:
                    raise ValueError("frp 端口必须介于 1 和 65535 之间")
                if values["public_url"]:
                    from urllib.parse import urlparse
                    parsed = urlparse(values["public_url"])
                    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
                        raise ValueError("固定地址应是 http(s) origin，例如 https://code.example.com")
                configured = json.loads(language_commands.get() or "{}")
                if not isinstance(configured, dict):
                    raise ValueError("语言服务配置需要 JSON 对象，例如 {\"python\": [\"pylsp\"]}")
                servers = [parse_language_server(language + "=" + json.dumps(command)) for language, command in configured.items()]
                self.args.language_server = servers
                for key, value in values.items():
                    setattr(self.args, key, value)
                for key, value in booleans.items():
                    setattr(self.args, key, value.get())
                dialog.destroy()
            except (ValueError, argparse.ArgumentTypeError) as exc:
                messagebox.showerror("设置无效", str(exc), parent=dialog)
        ttk.Label(dialog, text="native 使用 Windows 内置 AppContainer，无需安装额外沙箱软件。\n允许公网联网，仅授权任务副本写入；保留进程与资源限制，不可用时拒绝回退。").grid(row=server_row+3, columnspan=2, padx=12, pady=8)
        ttk.Button(dialog, text="保存", command=save).grid(row=server_row+4, column=1, sticky="e", padx=12, pady=12)

    def pause_desktop(self):
        if self.server is not None:
            self.server.projects.desktop.pause()
        self.desktop_var.set(DESKTOP_NOTICE)

    def desktop_settings(self):
        if self.server is None:
            messagebox.showinfo("桌面设置", "请先启动本地服务，再绑定窗口", parent=self.root)
            return
        manager = self.server.projects.desktop
        project = self.server.workspace.root
        dialog = tk.Toplevel(self.root)
        dialog.title("桌面窗口设置（本次运行有效）")
        dialog.transient(self.root)
        dialog.grab_set()
        default = app_data_dir() / "desktop-runtime-v0.10" / "Scripts" / "python.exe"
        python = tk.StringVar(value=manager.python or (str(default) if default.is_file() else ""))
        selected, allow_input = tk.StringVar(), tk.BooleanVar(value=False)
        windows = {}
        ttk.Label(dialog, text=f"绑定项目：{project}", wraplength=640).pack(anchor="w", padx=12, pady=8)
        ttk.Label(dialog, text="独立 Python 3.14（需安装固定版 Windows-MCP 后端）").pack(anchor="w", padx=12)
        ttk.Entry(dialog, textvariable=python, width=85).pack(fill="x", padx=12, pady=6)
        choices = ttk.Combobox(dialog, textvariable=selected, state="readonly", width=82)
        choices.pack(fill="x", padx=12, pady=6)
        ttk.Checkbutton(dialog, text="允许点击和向普通编辑框设置文本（默认仅观察）", variable=allow_input).pack(anchor="w", padx=12)
        ttk.Label(dialog, text="只绑定所选窗口；需要其位于前台。窗口失焦、本机输入或 Escape 会阻止动作。\n桌面输入不属于文件副本或整批撤回范围。主窗口可随时暂停。", foreground="#666").pack(anchor="w", padx=12, pady=8)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=12, pady=10)
        def load():
            value = python.get().strip()
            def done(items, error):
                if not dialog.winfo_exists():
                    return
                if error:
                    messagebox.showerror("读取窗口失败", str(error), parent=dialog)
                    return
                windows.clear()
                windows.update({f"{item['handle']} · {item['title']}": item["handle"] for item in items})
                choices.configure(values=list(windows))
                if windows:
                    selected.set(next(iter(windows)))
            self._background(lambda ws: manager.local_windows(value), done)
        def bind():
            handle = windows.get(selected.get())
            if handle is None:
                return
            value, input_enabled = python.get().strip(), allow_input.get()
            def done(state, error):
                if not dialog.winfo_exists():
                    return
                if error:
                    messagebox.showerror("绑定失败", str(error), parent=dialog)
                else:
                    self.desktop_var.set(f"桌面：{'允许输入' if state['input_enabled'] else '仅观察'} · {state['window']['title'][:50]}")
                    self.log(f"桌面窗口已绑定到项目 {project.name}；本次运行有效")
                    dialog.destroy()
            self._background(lambda ws: manager.bind_local(value, handle, project, input_enabled), done)
        ttk.Button(buttons, text="刷新窗口列表", command=load).pack(side="left")
        ttk.Button(buttons, text="绑定并激活窗口", command=bind).pack(side="right")

    def _background(self, work, done):
        runtime = self.runtime
        if runtime is None or runtime.server is None or self._switching:
            return
        workspace = runtime.server.workspace
        generation = self._view_generation
        def worker():
            try:
                result, error = work(workspace), None
            except Exception as exc:
                result, error = None, exc
            def finish():
                if (self.runtime is runtime and self.server is not None and generation == self._view_generation
                        and self.server.workspace is workspace):
                    done(result, error)
            try:
                self._post(finish)
            except (RuntimeError, tk.TclError):
                pass
        self._launch_worker(worker)

    def refresh_panels(self):
        if self._refresh_id is not None:
            self.root.after_cancel(self._refresh_id)
            self._refresh_id = None
        if self.runtime is None:
            return
        if self._switching:
            self._refresh_id = self.root.after(1500, self.refresh_panels)
            return
        if not self._refreshing:
            self._refreshing = True
            self._refresh_again = False
            archived = self.show_archived.get()
            task_offset = self._task_offset
            def load(workspace):
                jobs = workspace._processes.summaries() if workspace._processes is not None else []
                if workspace._isolated is not None:
                    with workspace._process_lock:
                        copies = list(workspace._isolated.workspaces.values())
                    for copy in copies:
                        if copy._processes is not None:
                            jobs.extend(copy._processes.summaries())
                return jobs, workspace.editor.history(limit=20), workspace.tasks.listing(offset=task_offset, limit=20, archived=archived)["tasks"]
            def apply(result, error):
                self._refreshing = False
                if error:
                    self.log(f"状态刷新失败：{error}")
                    return
                jobs, changes, tasks = result
                self.desktop_var.set(DESKTOP_NOTICE)
                states = {"running": "进行中", "paused": "已暂停", "blocked": "已阻塞", "waiting_user": "等待用户",
                          "failed": "失败", "interrupted": "已中断", "cancelled": "已取消", "completed": "已完成"}
                def task_state(item):
                    if item["recovery_required"]:
                        return "待恢复"
                    if item["status"] == "completed":
                        return {"working": "待合入", "applied": "已合入", "reverted": "已撤回"}.get(item["isolation"]["state"], "已完成")
                    return states.get(item["status"], item["status"])
                for tree, items, values in ((self.process_tree, jobs, lambda j: (j["state"], j["command"])),
                                            (self.change_tree, changes, lambda j: (j["state"], ", ".join(j["paths"]))),
                                            (self.task_tree, tasks, lambda j: (task_state(j), j["title"]))):
                    selection = tree.selection()
                    tree.delete(*tree.get_children())
                    for item in items:
                        identity = item.get("process_id") or item.get("change_id") or item["task_id"]
                        tree.insert("", "end", iid=identity, text=identity[:8], values=values(item))
                    for identity in selection:
                        if tree.exists(identity):
                            tree.selection_add(identity)
                if self._refresh_again:
                    self.refresh_panels()
            self._background(load, apply)
        else:
            # A mutation/manual refresh during an in-flight read must not be lost.
            self._refresh_again = True
        if self._refresh_id is not None:
            self.root.after_cancel(self._refresh_id)
        self._refresh_id = self.root.after(1500, self.refresh_panels)

    def _show_text(self, title, text):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry("900x600")
        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        ttk.Button(window, text="复制全部", command=copy).pack(anchor="w", padx=8, pady=5)
        view = tk.Text(window, font=("Consolas", 10), wrap="none")
        scroll = ttk.Scrollbar(window, command=view.yview)
        view.configure(yscrollcommand=scroll.set)
        view.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        view.insert("1.0", text)
        view.configure(state="disabled")

    def task_page(self, direction):
        if not self._refreshing:
            if direction < 0 or len(self.task_tree.get_children()) == 20:
                self._task_offset = max(0, self._task_offset + direction * 20)
                self.refresh_panels()

    def _task_action(self, name, arguments, *, show=False):
        if self.server is None:
            return
        from .tools import ToolContext, call_tool
        registry = self.server.projects
        def work(workspace):
            with registry.lock:
                project = next(entry for entry in registry.entries.values() if entry.workspace is workspace)
                ctx = ToolContext(workspace, project.settings, workspace_id=project.workspace_id,
                                  workspace_token=project.workspace_token, registry=registry)
            request = dict(arguments)
            if name in ("task_update", "task_cancel", "task_apply", "task_revert", "task_archive"):
                request["expected_revision"] = workspace.tasks.get(request["task_id"])["revision"]
            if name == "task_apply" and not request.get("dry_run", True):
                request["diff_revision"] = workspace.tasks.isolated_changes(request["task_id"])["diff_revision"]
            return call_tool(name, ctx, request).structured
        def done(value, error):
            if error:
                messagebox.showerror("任务操作失败", str(error), parent=self.root)
            elif show:
                self._show_text("任务预览 / 恢复包（可复制）", json.dumps(value, ensure_ascii=False, indent=2))
            else:
                task = value.get("task", value)
                self.log(f"{name} 已完成：{task.get('task_id', '')[:8]} {task.get('isolation', {}).get('state') or task.get('status', '')}")
                self.refresh_panels()
        self._background(work, done)

    def create_task(self):
        if self.server is None:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("新建任务")
        dialog.transient(self.root)
        dialog.grab_set()
        isolated = tk.BooleanVar(value=True)
        ttk.Checkbutton(dialog, text="在独立文件副本中工作，完成后再合入原项目（不是系统沙箱）", variable=isolated).pack(anchor="w", padx=12, pady=8)
        fields = {}
        for title, key, height in (("任务目标", "goal", 5), ("验收条件（每行一条，可留空）", "acceptance_criteria", 4),
                                   ("必需检查名称（每行一个，如 tests；须已配置，可留空）", "required_checks", 2)):
            ttk.Label(dialog, text=title).pack(anchor="w", padx=12, pady=(8, 2))
            fields[key] = tk.Text(dialog, height=height, width=72)
            fields[key].pack(padx=12)
        def submit():
            goal = fields["goal"].get("1.0", "end").strip()
            if not goal:
                return
            args = {"goal": goal, "operation_id": uuid.uuid4().hex, "isolation": "copy" if isolated.get() else "direct"}
            for key in ("acceptance_criteria", "required_checks"):
                args[key] = [line.strip() for line in fields[key].get("1.0", "end").splitlines() if line.strip()]
            dialog.destroy()
            self._task_offset = 0
            self._task_action("task_create", args)
        ttk.Button(dialog, text="创建", command=submit).pack(anchor="e", padx=12, pady=12)

    def archive_task(self):
        selected = self.task_tree.selection()
        if selected:
            self._task_action("task_archive", {"task_id": selected[0], "archived": not self.show_archived.get()})

    def project_health(self):
        self._task_action("storage_status", {}, show=True)
        self._task_action("index_status", {}, show=True)

    def cleanup_tasks(self):
        if self.server is None:
            return
        from .tools import ToolContext, call_tool
        project = self.server.projects.active
        ctx = ToolContext(project.workspace, project.settings)
        def preview(_workspace):
            return call_tool("task_cleanup", ctx, {"older_than_days": 0}).structured
        def ready(value, error):
            if error:
                messagebox.showerror("清理预览失败", str(error), parent=self.root)
                return
            dialog = tk.Toplevel(self.root)
            dialog.title("清理归档副本：确认预览")
            dialog.transient(self.root)
            dialog.grab_set()
            view = tk.Text(dialog, width=90, height=23, wrap="word")
            view.pack(padx=10, pady=10)
            view.insert("1.0", json.dumps(value, ensure_ascii=False, indent=2))
            view.configure(state="disabled")
            def execute():
                dialog.destroy()
                request = {"older_than_days": 0, "dry_run": False, "plan_revision": value["plan_revision"]}
                def done(result, failure):
                    if failure:
                        messagebox.showerror("清理未完成", str(failure), parent=self.root)
                    else:
                        self._show_text("清理结果", json.dumps(result, ensure_ascii=False, indent=2))
                        self.refresh_panels()
                self._background(lambda _ws: call_tool("task_cleanup", ctx, request).structured, done)
            ttk.Button(dialog, text="永久删除上述任务副本（保留历史）", command=execute).pack(pady=8)
        self._background(preview, ready)

    def show_task(self):
        selected = self.task_tree.selection()
        if selected:
            self._task_action("task_context", {"task_id": selected[0]}, show=True)

    def isolated_task_action(self, name, *, show=False):
        selected = self.task_tree.selection()
        if selected:
            args = {"task_id": selected[0]}
            if name in ("task_apply", "task_revert"):
                args["dry_run"] = show
            self._task_action(name, args, show=show)

    @staticmethod
    def _process_manager(workspace, identity):
        managers = [workspace._processes]
        if workspace._isolated is not None:
            with workspace._process_lock:
                managers.extend(copy._processes for copy in workspace._isolated.workspaces.values())
        for manager in managers:
            if manager is not None:
                with manager.lock:
                    if identity in manager.jobs:
                        return manager
        raise ValueError("当前项目找不到该进程")

    def update_task(self, status):
        selected = self.task_tree.selection()
        if selected:
            args = {"task_id": selected[0]}
            if status == "cancelled":
                self._task_action("task_cancel", {**args, "reason": "用户从任务中心取消"})
            else:
                self._task_action("task_update", {**args, "status": status})

    def show_change(self):
        selected = self.change_tree.selection()
        if selected:
            self._background(lambda ws: ws.editor.details(selected[0]),
                             lambda value, error: messagebox.showerror("读取失败", str(error)) if error else self._show_text("文件 diff", value["diff"] or "内容未变化"))

    def undo_selected_change(self):
        selected = self.change_tree.selection()
        if selected:
            def finish(value, error):
                if error:
                    messagebox.showerror("撤销未完成", str(error))
                else:
                    self.log(f"已撤销改动，恢复操作 ID：{value.get('change_id')}")
                    self.refresh_panels()
            self._background(lambda ws: ws.editor.undo(selected[0]), finish)

    def show_process(self):
        selected = self.process_tree.selection()
        if selected:
            self._background(lambda ws: self._process_manager(ws, selected[0]).read(selected[0], max_bytes=64_000),
                             lambda value, error: messagebox.showerror("读取失败", str(error)) if error else self._show_text("任务日志", value["stdout"] + "\n--- stderr ---\n" + value["stderr"]))

    def stop_selected_process(self):
        selected = self.process_tree.selection()
        if selected:
            def finish(value, error):
                if error:
                    messagebox.showerror("停止失败", str(error))
                else:
                    self.log(f"任务 {selected[0][:8]}：{value['state']}")
                    self.refresh_panels()
            self._background(lambda ws: self._process_manager(ws, selected[0]).stop(selected[0]), finish)

    def copy_url(self) -> None:
        if not self.public_url:
            messagebox.showinfo("提示", "还没启动或没有公网地址")
            return
        value = self.settings.mcp_url()
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        messagebox.showinfo("已复制", value)

    def copy_password(self) -> None:
        value = self.pwd_var.get()
        if value in ("——", "（bearer 模式）"):
            messagebox.showinfo("提示", "当前没有可用的授权口令")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        messagebox.showinfo("已复制", value)

    def copy_steps(self) -> None:
        if not self.public_url:
            messagebox.showinfo("提示", "还没启动")
            return
        url = self.settings.mcp_url()
        if self.settings.auth_mode == "path":
            steps = (
                "ChatGPT 接入步骤（无需授权）：\n"
                "1. 设置 → 开启开发者模式（Developer Mode）\n"
                "2. 新建自定义连接器 / MCP 应用\n"
                f"3. 服务器 URL：{url}\n"
                "4. 认证方式选「无认证」(No Auth)，直接保存\n"
                "5. 新建对话，工具菜单里启用，然后提需求\n"
                "（地址末尾那串随机字符就是凭据，别发到公开场合）"
            )
        elif self.settings.auth_mode == "oauth":
            steps = (
                "ChatGPT 接入步骤：\n"
                "1. 设置 → 开启开发者模式（Developer Mode）\n"
                "2. 新建自定义连接器 / MCP 应用\n"
                f"3. 服务器 URL：{url}\n"
                "4. 认证方式选 OAuth，授权页输入口令：\n"
                f"   {self.settings.oauth_password}\n"
                "5. 新建对话，在工具菜单里启用，然后提需求"
            )
        else:
            steps = f"MCP 地址：{url}\nBearer 令牌：{self.settings.bearer_token}"
        self.root.clipboard_clear()
        self.root.clipboard_append(steps)
        messagebox.showinfo("已复制接入步骤", steps)

    def on_close(self) -> None:
        if self.server is not None:
            if not messagebox.askyesno("确认退出", "服务正在运行，退出后公网地址立即失效。\n确定退出吗？"):
                return
            self.stop()
        self.root.destroy()


def run_gui(args: argparse.Namespace) -> int:
    root = tk.Tk()
    app = App(root, args)
    root.mainloop()
    del app, root
    gc.collect()
    return 0
