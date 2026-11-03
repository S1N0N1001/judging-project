"""Local Windows launcher and frozen server entry point."""
import argparse
import json
import http.client
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from judging_store import Store, data_root
from judging_network import local_addresses


def request(url, secret=None):
    # Only local HTTP is required here. Avoid HTTPS handler/certificate discovery
    # and system proxy configuration in a frozen process.
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("Контрольный адрес должен быть локальным HTTP")
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    try:
        connection.request("GET", parsed.path or "/", headers={"X-Judging-Control": secret} if secret else {})
        response = connection.getresponse()
        if response.status != 200:
            raise OSError(f"HTTP {response.status}")
        return response.read().decode()
    finally:
        connection.close()


def unused_port(preferred=0, address="127.0.0.1"):
    with socket.socket() as sock:
        sock.bind((address, preferred))
        return sock.getsockname()[1]


def serve(args):
    import asyncio
    from streamlit.web import bootstrap

    server_ref = {}
    original_handler = bootstrap._set_up_signal_handler

    def capture_server(server):
        server_ref.update(server=server, loop=asyncio.get_running_loop())
        original_handler(server)

    bootstrap._set_up_signal_handler = capture_server

    class Control(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # never log URL tokens

        def do_GET(self):
            if self.headers.get("X-Judging-Control") != os.environ["JUDGING_CONTROL_SECRET"]:
                self.send_error(403)
                return
            if self.path == "/identity":
                payload = os.environ["JUDGING_INSTANCE_ID"]
            elif self.path == "/stop":
                if "server" in server_ref:
                    server_ref["loop"].call_soon_threadsafe(server_ref["server"].stop)
                payload = "stopping"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload.encode())

    control = ThreadingHTTPServer(("127.0.0.1", args.control_port), Control)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    # Run from the data directory, never load an unsafe config in caller's cwd.
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    flags = {"server.headless": True, "server.port": args.port, "server.address": args.address,
             "global.developmentMode": False,
             "server.fileWatcherType": "none", "server.enableCORS": True, "server.enableXsrfProtection": True,
             "browser.gatherUsageStats": False, "browser.serverAddress": "localhost"}
    bootstrap.load_config_options(flag_options=flags)
    try:
        bootstrap.run(str(Path(__file__).resolve().parent / "main.py"), False, [], flags)
    finally:
        control.shutdown()


def acquire_lock(root):
    import msvcrt
    handle = open(root / "instance.lock", "a+b")
    # Reading the byte before locking fails on Windows if another instance owns
    # its byte-range lock. Inspect file size without reading the locked region.
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle  # OS releases it even when the launcher crashes


def start(args):
    # Bundled development runtime needs an ASCII Tcl path on Windows.
    # Frozen builds use PyInstaller's tkinter runtime hook instead.
    if not getattr(sys, "frozen", False):
        tcl = Path(sys.prefix) / "tcl"
        if (tcl / "tcl8.6").exists():
            os.environ.setdefault("TCL_LIBRARY", str(tcl / "tcl8.6"))
            os.environ.setdefault("TK_LIBRARY", str(tcl / "tk8.6"))
    import tkinter as tk
    from tkinter import messagebox

    if args.smoke_test:
        probe = tk.Tk()
        probe.withdraw()
        probe.update_idletasks()
        probe.destroy()

    root_dir = data_root()
    Store(root_dir)
    state_path = root_dir / "instance.json"
    lock = acquire_lock(root_dir)
    if lock is None:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if request(state["control"] + "/identity", state["secret"]) != state["id"]:
                raise ValueError("Неверный владелец порта")
            if not args.smoke_test:
                webbrowser.open(state["url"])
            print("EXISTING_INSTANCE_OK")
            return
        except Exception:
            raise RuntimeError("Приложение уже запускается. Подождите и используйте первое окно.")
    # Recover a live child left by an interrupted launcher, before spawning another.
    if state_path.exists():
        try:
            old = json.loads(state_path.read_text(encoding="utf-8"))
            if request(old["control"] + "/identity", old["secret"]) == old["id"]:
                request(old["control"] + "/stop", old["secret"])
                for _ in range(50):
                    try:
                        request(old["control"] + "/identity", old["secret"])
                    except Exception:
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError("Предыдущий сервер ещё завершается")
        except (OSError, ValueError, KeyError):
            pass
    ui = None if args.smoke_test else tk.Tk()
    if ui:
        ui.title("Судейство — запуск сервера")
        ui.geometry("620x360")
    try:
        port = unused_port(args.port, args.address)
    except OSError:
        if ui and not messagebox.askyesno("Порт занят", f"Порт {args.port} занят. Выбрать свободный порт?"):
            ui.destroy()
            lock.close()
            return
        port = unused_port(0, args.address)
    control_port = unused_port()
    secret, instance, setup = secrets.token_urlsafe(32), secrets.token_hex(16), secrets.token_urlsafe(32)
    url = f"http://localhost:{port}"
    setup_url = url + ("/?setup=" + setup if not Store(root_dir).initialized() else "")
    state = {"id": instance, "secret": secret, "url": url, "control": f"http://127.0.0.1:{control_port}"}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    env = dict(os.environ, JUDGING_DATA_DIR=str(root_dir), JUDGING_CONTROL_SECRET=secret, JUDGING_INSTANCE_ID=instance, JUDGING_SETUP_TOKEN=setup, JUDGING_PORT=str(port), JUDGING_BIND_ADDRESS=args.address)
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(Path(__file__).resolve()))
    command += ["--serve", "--port", str(port), "--address", args.address, "--control-port", str(control_port)]
    log_path = root_dir / "logs" / "server.log"
    log = open(log_path, "a", encoding="utf-8")
    child = subprocess.Popen(command, env=env, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    started, ready, stopping = time.monotonic(), False, False

    def stop():
        nonlocal stopping
        if child.poll() is None:
            request(state["control"] + "/stop", secret)
        stopping = True

    def cleanup():
        state_path.unlink(missing_ok=True)
        log.close()
        lock.close()
        if ui:
            ui.destroy()

    if args.smoke_test:
        last_error = ""
        try:
            while time.monotonic() - started < 45:
                if child.poll() is not None:
                    raise RuntimeError(f"Сервер завершился: {log_path}")
                try:
                    if request(url + "/_stcore/health") == "ok":
                        ready = True
                        break
                except Exception as exc:
                    last_error = repr(exc)
                time.sleep(.2)
            if not ready:
                raise RuntimeError("Сервер не готов за 45 секунд: " + last_error)
            print(f"SERVER_READY seconds={time.monotonic()-started:.2f} port={port}")
            stop()
            child.wait(timeout=20)
            print("SERVER_STOPPED")
        finally:
            if child.poll() is None:
                try:
                    stop()
                    child.wait(timeout=10)
                except Exception:
                    child.terminate()  # own test child only; no browser sessions
                    child.wait(timeout=10)
            if child.poll() is not None:
                cleanup()
        return

    status = tk.StringVar(value="Запуск…")
    tk.Label(ui, textvariable=status, wraplength=580).pack(pady=12)
    tk.Label(ui, text=f"Локальный интерфейс: {url}\nСервер слушает: {args.address}:{port}\nДанные: {root_dir}", wraplength=580).pack(pady=8)
    lan = ', '.join(f"{a['ip']}:{port}" for a in local_addresses()) if args.address == '0.0.0.0' else 'доступен только этот компьютер'
    tk.Label(ui, text=f"Адреса для телефонов: {lan}\nВыберите адаптер Wi-Fi/LAN в панели секретаря.", wraplength=580).pack()
    open_button = tk.Button(ui, text="Открыть интерфейс", state="disabled", command=lambda: webbrowser.open(setup_url))
    open_button.pack(pady=8)
    tk.Button(ui, text="Открыть журнал", command=lambda: os.startfile(log_path)).pack()
    tk.Button(ui, text="Остановить сервер", command=lambda: safe_stop()).pack(pady=8)

    def safe_stop():
        try:
            stop()
            status.set("Остановка: ждём завершения активных операций…")
        except Exception as exc:
            messagebox.showerror("Остановка", str(exc))

    def on_close():
        if messagebox.askyesno("Закрытие", "Остановить сервер? Нет — свернуть окно."):
            safe_stop()
        else:
            ui.iconify()

    def poll():
        nonlocal ready
        if child.poll() is not None:
            if not stopping:
                messagebox.showerror("Сервер остановлен", f"Подробности: {log_path}")
            cleanup()
            return
        if not ready and not stopping:
            try:
                if request(url + "/_stcore/health") == "ok":
                    ready = True
                    status.set("Сервер работает")
                    open_button.configure(state="normal")
                    webbrowser.open(setup_url)
            except Exception:
                if time.monotonic() - started > 45:
                    status.set(f"Запуск длится дольше обычного. Проверьте журнал: {log_path}")
        ui.after(500, poll)

    ui.protocol("WM_DELETE_WINDOW", on_close)
    ui.after(200, poll)
    ui.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-test", action="store_true", help="Запустить, проверить HTTP и остановить сервер без браузера")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--control-port", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--address", choices=["0.0.0.0", "127.0.0.1"], default="0.0.0.0")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--reset-password", action="store_true", help="Локальное восстановление пароля при остановленном сервере")
    parser.add_argument("--restore", action="store_true", help="Восстановить резервную копию при остановленном сервере")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["JUDGING_DATA_DIR"] = str(args.data_dir.resolve())
    if args.reset_password or args.restore:
        # Local executable operation, never exposed as a web action.
        if not getattr(sys, "frozen", False) and (Path(sys.prefix) / "tcl").exists():
            os.environ.setdefault("TCL_LIBRARY", str(Path(sys.prefix) / "tcl/tcl8.6"))
            os.environ.setdefault("TK_LIBRARY", str(Path(sys.prefix) / "tcl/tk8.6"))
        import tkinter as tk
        from tkinter import filedialog, messagebox, simpledialog
        from maintenance import restore
        root = data_root()
        store = Store(root)
        lock = acquire_lock(root)
        if lock is None:
            raise RuntimeError("Сначала остановите сервер и закройте лаунчер")
        ui = tk.Tk()
        ui.withdraw()
        try:
            state_file = root / "instance.json"
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                    if request(state["control"] + "/identity", state["secret"]) == state["id"]:
                        raise RuntimeError("Предыдущий сервер работает. Сначала завершите его")
                except (OSError, ValueError, KeyError):
                    pass
            if args.reset_password:
                if not store.initialized():
                    raise ValueError("Сначала пройдите первоначальную настройку через лаунчер")
                password = simpledialog.askstring("Пароль", "Новый пароль (минимум 12 символов)", show="*", parent=ui)
                if password:
                    repeat = simpledialog.askstring("Пароль", "Повторите пароль", show="*", parent=ui)
                    if password != repeat:
                        raise ValueError("Пароли не совпадают")
                    store.reset_password_local(password)
                    messagebox.showinfo("Готово", "Пароль изменён. Оценки сохранены.")
            else:
                path = filedialog.askopenfilename(title="Выберите резервную копию SQLite", filetypes=[("SQLite", "*.sqlite3")], parent=ui)
                if path and messagebox.askyesno("Восстановление", "Заменить текущие данные резервной копией? Текущая база будет сохранена отдельно."):
                    backup = restore(root, path)
                    messagebox.showinfo("Готово", f"База восстановлена. Предыдущая версия: {backup}")
        finally:
            lock.close()
            ui.destroy()
    elif args.serve:
        serve(args)
    else:
        start(args)


if __name__ == "__main__":
    if sys.stdout is None or sys.stderr is None:
        root = data_root()
        (root / "logs").mkdir(parents=True, exist_ok=True)
        stream = open(root / "logs" / "launcher.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    try:
        main()
    except Exception as exc:
        if "--serve" in sys.argv or "--smoke-test" in sys.argv:
            import traceback
            traceback.print_exc()
            sys.exit(1)
        import tkinter.messagebox
        tkinter.messagebox.showerror("Судейство", str(exc))
