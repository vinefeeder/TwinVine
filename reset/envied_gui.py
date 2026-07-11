# -*- coding: utf-8 -*-
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QPushButton, QFrame, QLabel
from PyQt6.QtCore import QProcess
from PyQt6.QtCore import Qt
import sys
import os
import subprocess
import shutil


CFG = os.path.abspath("./packages/envied/src/envied/envied.yaml")
class EnviedPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Envied Control   ")
        # Frame styling: 1px white border (no pink)
        self.setObjectName("enviedFrame")
        self.setStyleSheet("#enviedFrame { background-color: #2D2D2D; border: 1px solid pink; }")
        # ("color: #f5c2e7; border: none; background-color:#1E1E2E;padding: 5px;") 

        # Layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)


        # Helper to make consistently styled buttons
        def make_btn(text):
            b = QPushButton(text)
            b.setStyleSheet(
                            "background-color: #1E1E2E;  \
                            color: #f5c2e7; \
                            border-width: 2px;\
                            border-color: pink;\
                            font:  14px;\
                            min-width: 10em;\
                            padding: 5px;"
                            )
            b.setAutoFillBackground(True)
            b.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            return b

        # Buttons
        btn_run   = make_btn("run envied")
        btn_check = make_btn("env check")
        btn_info  = make_btn("env info")
        btn_cfg   = make_btn("config")

        layout.addWidget(btn_run)
        layout.addWidget(btn_check)
        layout.addWidget(btn_info)
        layout.addWidget(btn_cfg)

        # --- Wire up clicks ---
        # Option A: non-blocking (recommended): use QProcess so the GUI stays responsive
        btn_run.clicked.connect(lambda: open_prefilled_terminal())
        btn_check.clicked.connect(lambda: self.run_cmd(["uv", "run", "envied", "env", "check"]))
        btn_info.clicked.connect(lambda: self.run_cmd(["uv", "run", "envied", "env", "info"]))
        '''btn_cfg.clicked.connect(
        lambda: os.system('nano ./packages/envied/src/envied/envied.yaml') if os.name == 'posix'
        else os.system('notepad.exe ./packages/envied/src/envied/envied.yaml')
        )'''
        btn_cfg.clicked.connect(open_config_detached)

    # ---- Helpers ----
    def run_cmd(self, argv):
        """
        Non-blocking run using QProcess.
        """
        proc = QProcess(self)
        # Optional: forward output to your terminal
        proc.setProgram(argv[0])
        proc.setArguments(argv[1:])
        # Show output in your launching terminal (detach these if not needed)
        proc.readyReadStandardOutput.connect(lambda p=proc: print(p.readAllStandardOutput().data().decode(), end=""))
        proc.readyReadStandardError.connect(lambda p=proc: print(p.readAllStandardError().data().decode(), end=""))
        proc.start()

    def run_script(self, what):
        """
        Placeholder hook for your 'config' action.
        e.g., open a config dialog or run a script.
        """
        print(f"[EnviedPanel] run_script({what}) not implemented yet.")

def get_terminal():
    terminals = ["gnome-terminal", "xterm", "konsole", "lxterminal", "xfce4-terminal"]
    for term in terminals:
        if shutil.which(term):
            return term
        elif os.__name__ =="nt":
            TERMINALS = ["WindowsTerminal.exe", "OpenConsole.exe","powershell.exe", "Terminal.exe", "cmd.exe"]
            for TERMINAL in TERMINALS:
                if shutil.which(TERMINAL):
                    return TERMINAL
    raise EnvironmentError("No suitable terminal emulator found.")

def open_prefilled_terminal():
    prefill = 'uv run envied dl --select-titles '
    term = get_terminal()

    if term == "gnome-terminal":
        # -l starts login shell; -c runs the command, then we keep the shell open with 'exec bash'
        cmd = [
            "gnome-terminal",
            "--",
            "bash", "-lc",
            # read: -e enables readline, -i sets initial text
            f'read -e -i "{prefill}" cmd; eval "$cmd"; exec bash'
        ]
    elif term == "konsole":
        cmd = [
            "konsole", "-e",
            "bash", "-lc",
            f'read -e -i "{prefill}" cmd; eval "$cmd"; exec bash'
        ]
    elif term =="lxterminal":  # xterm fallback
        cmd = [
            "xterm", "-e",
            "bash", "-lc",
            f'read -e -i "{prefill}" cmd; eval "$cmd"; exec bash'
        ]
    
    elif os.name == "nt":
    # --- Windows: open PowerShell (or Windows Terminal -> PowerShell)
    # and pre-fill the command line using PSReadLine.
    # This requires PSReadLine (present by default on modern Windows).
        ps_prefill = prefill.replace('"', r'`"')  # escape quotes for PowerShell

        if shutil.which("wt.exe"):
            # Windows Terminal present: open a new tab running PowerShell
            cmd = [
                "wt.exe",
                "powershell",
                "-NoExit",
                "-Command",
                f"[Microsoft.PowerShell.PSConsoleReadLine]::Insert(\"{ps_prefill}\")"
            ]
        elif shutil.which("powershell.exe"):
            # Fallback: plain PowerShell
            cmd = [
                "powershell.exe",
                "-NoExit",
                "-Command",
                f"[Microsoft.PowerShell.PSConsoleReadLine]::Insert(\"{ps_prefill}\")"
            ]
        else:
            # Last resort: cmd.exe can't truly prefill. Keep the window open and hint user.
            cmd = [
                "cmd.exe", "/K",
                f'echo Please use PowerShell for prefill. Intended command: {prefill}'
            ]



    subprocess.Popen(cmd)


def open_config_detached():
    if os.name == "nt":
        QProcess.startDetached("notepad.exe", [CFG])
        return

    # POSIX
    # Easiest: open in the default GUI editor
    if shutil.which("xdg-open"):
        QProcess.startDetached("xdg-open", [CFG])
        return

    # If you insist on nano, run it *inside* a terminal emulator
    # (xterm is very common and works out of the box)
    if shutil.which("xterm"):
        QProcess.startDetached("xterm", ["-e", "nano", CFG])
        return

    # Last resort (may fail without a TTY)
    QProcess.startDetached("nano", [CFG])

if __name__ == "__main__":
    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    envied_panel = EnviedPanel()
    window = envied_panel
    window.show()
    sys.exit(app.exec())




    subprocess.Popen(cmd)
