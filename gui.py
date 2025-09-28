import sys
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QLineEdit, QTextEdit, QPushButton, QCheckBox, QFrame, QMessageBox, QHBoxLayout
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPalette, QColor
import subprocess
import httpx
import base64
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from base64 import b64encode
import codecs
from pathlib import Path
import os
import shlex

WVD_PATH = "./device.wvd"
WIDEVINE_SYSTEM_ID = 'EDEF8BA9-79D6-4ACE-A3C8-27DCD51D21ED'

class DownloadThread(QThread):
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, command):
        super().__init__()
        self.command = command

    def run(self):
        try:
            subprocess.run(self.command, check=True)
            self.finished.emit()
        except subprocess.CalledProcessError as e:
            self.error.emit(str(e))

class AllHell3App(QWidget):
    def __init__(self):
        super().__init__()
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("allhell3 GUI")
        layout = QVBoxLayout()

        self.mpd_url_label = QLabel("MPD URL")
        layout.addWidget(self.mpd_url_label)
        self.mpd_url_entry = QLineEdit()
        layout.addWidget(self.mpd_url_entry)

        self.curl_label = QLabel("cURL of License Request")
        layout.addWidget(self.curl_label)
        self.curl_text = QTextEdit()
        layout.addWidget(self.curl_text)

        highlighted_frame = QFrame()
        highlighted_layout = QVBoxLayout()
        highlighted_frame.setLayout(highlighted_layout)
        highlighted_frame.setStyleSheet("border: 1px solid red;")

        self.video_name_label = QLabel("Video Name")
        highlighted_layout.addWidget(self.video_name_label)
        self.video_name_entry = QLineEdit()
        highlighted_layout.addWidget(self.video_name_entry)

        self.fetch_keys_button = QPushButton("Get Keys")
        self.fetch_keys_button.clicked.connect(self.fetch_keys)
        highlighted_layout.addWidget(self.fetch_keys_button)

        self.download_button = QPushButton("Download Nm~RE")
        self.download_button.clicked.connect(self.download_video)
        highlighted_layout.addWidget(self.download_button)

        self.download_button2 = QPushButton("Download DASH")
        self.download_button2.clicked.connect(self.download_dash)
        highlighted_layout.addWidget(self.download_button2)

        layout.addWidget(highlighted_frame)

        self.keys_output = QTextEdit()
        layout.addWidget(self.keys_output)

        self.n_m3u8dl_re_label = QLabel("N_m3u8DL-RE command")
        layout.addWidget(self.n_m3u8dl_re_label)
        self.n_m3u8dl_re_output = QTextEdit()
        layout.addWidget(self.n_m3u8dl_re_output)

        self.dash_mpd_cli_label = QLabel("Dash-MPD-CLI command")
        layout.addWidget(self.dash_mpd_cli_label)
        self.dash_mpd_cli_output = QTextEdit()
        layout.addWidget(self.dash_mpd_cli_output)

        checkbox_layout = QHBoxLayout()
        self.dark_mode_checkbox = QCheckBox("Dark Mode")
        self.dark_mode_checkbox.setChecked(True)
        self.dark_mode_checkbox.stateChanged.connect(self.toggle_dark_mode)
        checkbox_layout.addWidget(self.dark_mode_checkbox)

        self.reset_button = QPushButton("Reset")
        self.reset_button.clicked.connect(self.reset_fields)
        checkbox_layout.addWidget(self.reset_button)

        layout.addLayout(checkbox_layout)

        self.setLayout(layout)
        self.toggle_dark_mode()

    def reset_fields(self):
        self.mpd_url_entry.clear()
        self.curl_text.clear()
        self.video_name_entry.clear()
        self.keys_output.clear()
        self.n_m3u8dl_re_output.clear()
        self.dash_mpd_cli_output.clear()


    def toggle_dark_mode(self):
        if self.dark_mode_checkbox.isChecked():
            palette = QPalette()
            palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
            palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.Base, QColor(35, 35, 35))
            palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
            palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.black)
            palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
            palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
            palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
            palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
            palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)
            self.setPalette(palette)

            self.mpd_url_label.setStyleSheet("color: white;")
            self.curl_label.setStyleSheet("color: white;")
            self.video_name_label.setStyleSheet("color: white;")
            self.n_m3u8dl_re_label.setStyleSheet("color: white;")
            self.dash_mpd_cli_label.setStyleSheet("color: white;")
            self.dark_mode_checkbox.setStyleSheet("color: white;")
            self.fetch_keys_button.setStyleSheet("""color: white;
                                                background-color: #4e4e4e;
                                                border: 1px solid #6e6e6e;
                                                padding: 5px;""")
            self.download_button.setStyleSheet("""color: white;
                                                background-color: #4e4e4e;
                                                border: 1px solid #6e6e6e;
                                                padding: 5px;""")
            
            self.download_button2.setStyleSheet("""color: white;
                                                background-color: #4e4e4e;
                                                border: 1px solid #6e6e6e;
                                                padding: 5px;""")
            
            self.curl_text.setStyleSheet("""color: white; border: 1px solid red;
                                            background-color: #232323;""")
            self.video_name_label.setStyleSheet("""
                                                color: white; border: None;""")
            self.mpd_url_entry.setStyleSheet("""color: white; border: 1px solid red;
                                            background-color: #232323;""")
                                                
        else:
            self.setPalette(QApplication.palette())

            self.mpd_url_label.setStyleSheet("color: black;")
            self.curl_label.setStyleSheet("color: black;")
            self.video_name_label.setStyleSheet("color: black;")
            self.n_m3u8dl_re_label.setStyleSheet("color: black;")
            self.dash_mpd_cli_label.setStyleSheet("color: black;")
            self.dark_mode_checkbox.setStyleSheet("color: black;")
            self.fetch_keys_button.setStyleSheet("""color: black;
                                                background-color: #aeaeae;
                                                border: 1px solid #6e6e6e;
                                                padding: 5px;""")
            self.download_button.setStyleSheet("""color: black;
                                                background-color: #aeaeae;
                                                border: 1px solid #6e6e6e;
                                                padding: 5px;""")
            self.download_button2.setStyleSheet("""color: black;
                                                background-color: #aeaeae;
                                                border: 1px solid #6e6e6e;
                                                padding: 5px;""")
            
            self.curl_text.setStyleSheet("""color: black; border: 1px solid red;
                                            background-color: white;""")
            self.video_name_label.setStyleSheet("""
                                                color: black; border: None;""")
            self.mpd_url_entry.setStyleSheet("""color: black; border: 1px solid red;
                                            background-color: white;""")

    def fetch_mpd_content(self, mpd_url):
        response = httpx.get(mpd_url)
        response.raise_for_status()
        return response.text

    def find_default_kid_with_regex(self, mpd_content):
        match = re.search(r'cenc:default_KID="([A-F0-9-]+)"', mpd_content)
        if match:
            return match.group(1)
        return None

    def extract_or_generate_pssh(self, mpd_content):
        try:
            tree = ET.ElementTree(ET.fromstring(mpd_content))
            root = tree.getroot()

            namespaces = {
                'cenc': 'urn:mpeg:cenc:2013',
                '': 'urn:mpeg:dash:schema:mpd:2011'
            }

            default_kid = None
            for elem in root.findall('.//ContentProtection', namespaces):
                scheme_id_uri = elem.attrib.get('schemeIdUri', '').upper()
                if scheme_id_uri == 'URN:MPEG:DASH:MP4PROTECTION:2011':
                    default_kid = elem.attrib.get('cenc:default_KID')
                    if default_kid:
                        break

            if not default_kid:
                default_kid = self.find_default_kid_with_regex(mpd_content)

            pssh = None
            for elem in root.findall('.//ContentProtection', namespaces):
                scheme_id_uri = elem.attrib.get('schemeIdUri', '').upper()
                if scheme_id_uri == f'URN:UUID:{WIDEVINE_SYSTEM_ID}':
                    pssh_elem = elem.find('cenc:pssh', namespaces)
                    if pssh_elem is not None:
                        pssh = pssh_elem.text
                        break

            if pssh is not None:
                return pssh
            elif default_kid is not None:
                default_kid = default_kid.replace('-', '')
                s = f'000000387073736800000000edef8ba979d64acea3c827dcd51d21ed000000181210{default_kid}48e3dc959b06'
                return b64encode(bytes.fromhex(s)).decode()

            else:
                # No pssh or default_KID found
                try:
                    pssh = self.get_pssh_from_mpd(self.mpd_url_entry.text())  # init.m4f method
                    if pssh:
                        return pssh
                    else:
                        return None
                except:
                    return None

        except ET.ParseError as e:
            try:
                pssh = self.get_pssh_from_mpd(self.mpd_url_entry.text())  # init.m4f method
                return pssh
            except:
                pass
            print(f"Error parsing MPD content: {e}")
            return None

        
    def get_pssh_from_mpd(self, mpd: str):
        print("Extracting PSSH from MPD...")

        yt_dl = 'yt-dlp'
        init = 'init.m4f'

        files_to_delete = [init]

        for file_name in files_to_delete:
            if os.path.exists(file_name):
                os.remove(file_name)
                print(f"{file_name} file successfully deleted.")

        try:
            subprocess.run([yt_dl, '-q', '--no-warning', '--test', '--allow-u', '-f', 'bestvideo[ext=mp4]/bestaudio[ext=m4a]/best', '-o', init, mpd])
        except FileNotFoundError:
            print("yt-dlp not found. Trying installing it...")
            exit(0)

        pssh_list = self.extract_pssh_from_file('init.m4f')
        pssh = None
        for target_pssh in pssh_list:
            if 20 < len(target_pssh) < 220:
                pssh = target_pssh

        print(f'\n{pssh}\n')

        for file_name in files_to_delete:
            if os.path.exists(file_name):
                os.remove(file_name)
                print(f"{file_name} file successfully deleted.")
        return pssh
        
    

    def extract_pssh_from_file(self,file_path: str) -> list:
        print('Extracting PSSHs from init file:', file_path)
        return self.to_pssh(Path(file_path).read_bytes())
    
    def find_wv_pssh_offsets(self, raw: bytes) -> list:
        offsets = []
        offset = 0
        while True:
            offset = raw.find(b'pssh', offset)
            if offset == -1:
                break
            size = int.from_bytes(raw[offset-4:offset], byteorder='big')
            pssh_offset = offset - 4
            offsets.append(raw[pssh_offset:pssh_offset+size])
            offset += size
        return offsets
    
    def to_pssh(self, content: bytes) -> list:
        wv_offsets = self.find_wv_pssh_offsets(content)
        return [base64.b64encode(wv_offset).decode() for wv_offset in wv_offsets]
    
    

    def parse_curl(self, curl_command):
        url_match = re.search(r"curl\s+'(.*?)'", curl_command)
        url = url_match.group(1) if url_match else ""

        method_match = re.search(r"-X\s+(\w+)", curl_command)
        method = method_match.group(1) if method_match else "UNDEFINED"

        headers = {}
        headers_matches = re.findall(r"-H\s+'([^:]+):\s*(.*?)'", curl_command)
        for header in headers_matches:
            headers[header[0]] = header[1]

        data_match = re.search(r"--data(?:-raw)?\s+(?:(\$?')|(\$?{?))(.*?)'", curl_command, re.DOTALL)
        if data_match:
            raw_prefix = data_match.group(1)
            data = data_match.group(3)
            if raw_prefix and raw_prefix.startswith('$'):
                data = None
            else:
                data = data.replace('\\\\', '\\').replace('\\x', '\\\\x')
                try:
                    data = codecs.decode(data, 'unicode_escape')
                except Exception as e:
                    data = ""
        else:
            data = ""
        return url, method, headers, data

    def get_key(self, pssh, license_url, headers, data):
        device = Device.load(WVD_PATH)
        cdm = Cdm.from_device(device)
        session_id = cdm.open()

        challenge = cdm.get_license_challenge(session_id, PSSH(pssh))

        if data:
            if match := re.search(r'"(CAQ=.*?)"', data):
                challenge = data.replace(match.group(1), base64.b64encode(challenge).decode())
            elif match := re.search(r'"(CAES.*?)"', data):
                challenge = data.replace(match.group(1), base64.b64encode(challenge).decode())
            elif match := re.search(r'=(CAES.*?)(&.*)?$', data):
                b64challenge = base64.b64encode(challenge).decode()
                quoted = urllib.parse.quote_plus(b64challenge)
                challenge = data.replace(match.group(1), quoted)

        payload = challenge if data is None else challenge

        license_response = httpx.post(url=license_url, data=payload, headers=headers)
        try:
            license_response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise e

        license_content = license_response.content
        try:
            match = re.search(r'"(CAIS.*?)"', license_response.content.decode('utf-8'))
            if match:
                license_content = base64.b64decode(match.group(1))
        except:
            pass

        if isinstance(license_content, str):
            license_content = base64.b64decode(license_content)

        cdm.parse_license(session_id, license_content)

        keys = []
        for key in cdm.get_keys(session_id):
            if key.type == 'CONTENT':
                keys.append(f"--key {key.kid.hex}:{key.key.hex()}")

        cdm.close(session_id)
        return keys

    def fetch_keys(self):
        mpd_url = self.mpd_url_entry.text()
        try:
            mpd_content = self.fetch_mpd_content(mpd_url)
            pssh = self.extract_or_generate_pssh(mpd_content)
            if not pssh:
                QMessageBox.critical(self, "Error", "Failed to extract or generate PSSH\nAre you sure the video is Widevine encrypted?.")
                return

            curl_command = self.curl_text.toPlainText().strip()
            license_url, method, headers, data = self.parse_curl(curl_command)
            keys = self.get_key(pssh, license_url, headers, data)
            self.keys_output.clear()
            self.keys_output.append("\n".join( keys))
            key_str = " ".join(keys)
            
            self.n_m3u8dl_re_command = f"N_m3u8DL-RE '{mpd_url}' {key_str} --save-name {self.video_name_entry.text()} -mt -M:format=mkv:muxer=mkvmerge"
            self.n_m3u8dl_re_output.setText(self.n_m3u8dl_re_command)

            self.dash_mpd_cli_command = f"dash-mpd-cli --quality best --muxer-preference mkv:mkvmerge {key_str} \"{mpd_url}\" --write-subs --output '{self.video_name_entry.text()}.mkv'"

            self.dash_mpd_cli_output.setText(str(self.dash_mpd_cli_command))

        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def download_video(self):
        try:
            self.thread = DownloadThread(shlex.split(self.n_m3u8dl_re_command))
            self.thread.finished.connect(self.on_download_finished)
            self.thread.error.connect(self.on_download_error)
            self.thread.start()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def download_dash(self):
        try:
            self.thread = DownloadThread(shlex.split(self.dash_mpd_cli_command))
            self.thread.finished.connect(self.on_download_finished)
            self.thread.error.connect(self.on_download_error)
            self.thread.start()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def on_download_finished(self):
        QMessageBox.information(self, "Success", "Download finished successfully.")

    def on_download_error(self, error_message):
        QMessageBox.critical(self, "Error", f"Download failed: {error_message}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = AllHell3App()
    window.show()
    sys.exit(app.exec())
