from PyQt6.QtWidgets import (QApplication,QWidget,QVBoxLayout,QLabel,QLineEdit,QPushButton,QCheckBox,QFrame,)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QHBoxLayout, QSlider
import sys
import os
import re
from importlib import resources
import importlib
from pathlib import Path
import yaml
from beaupy import select
import threading
from .pretty import pretty_print
from rich.console import Console
from .parsing_utils import prettify
import click
import subprocess
#from .batchloader import batchload
import pkgutil
from .pretty import catppuccin_mocha
from .config_loader import load_config_with_fallback, get_bool, project_config_path, save_project_config

PAGE_SIZE = 8  # size of beaupy pagination


console = Console()



"""
Example usage:
    uv run vinefeeder --help       # Show help text
    uv run vinefeeder              # Launch VineFeeder GUI
    To run envied:  uv run envied
    
    In the GUI:-
    	enter search text and select a service
    or
    	enter a video URL for direct download
    	and select a service
    or
    	leave the seach box blank and select a service
    	a further menu will appear in the terminal
    After a download has finished and 'Ready!' appears
    another service may be started.
    In the terminal:-
    	enter the number(s) of the service to download
        see
    
    	 
"""
# --- config helpers ----------------------------------------------------------


_PKG_NAME = "vinefeeder"
_CFG_NAME = "config.yaml"





'''def load_config_with_fallback() -> tuple[dict, Path | None]:
    """
    Returns (config_dict, user_path_if_used_or_None).
    Prefers user config; falls back to package-bundled default.
    """
    user_path = _user_config_path()
    if user_path.exists():
        data = yaml.safe_load(user_path.read_text(encoding="utf-8")) or {}
        return data, user_path

    # packaged default
    with resources.files(_PKG_NAME).joinpath(_CFG_NAME).open("rb") as f:
        data = yaml.safe_load(f) or {}
    return data, None'''



def derive_loader_class_name(service_modname: str) -> str:
    """
    Derive a class name from a service module name.
    Examples:
      'ALL4' -> 'All4Loader'
      'BBC'  -> 'BbcLoader'
      'ITVX' -> 'ItvxLoader'
      'TVNZ' -> 'TvnzLoader'
      'MY5'  -> 'My5Loader'
      'STV'  -> 'StvLoader'
      'TPTV' -> 'TptvLoader'
      'U'    -> 'ULoader'
    """
    # Keep only alphanumerics, split on boundaries, then TitleCase & join
    cleaned = re.sub(r'[^A-Za-z0-9]+', ' ', service_modname).strip()
    # Title-case the whole thing (digits stay as-is)
    titled = ''.join(part.capitalize() for part in cleaned.split())
    if not titled:  # safety
        titled = service_modname.capitalize()
    return f"{titled}Loader"


class VineFeeder(QWidget):
    def __init__(self):
        """
        Initialize the VineFeeder object.

        This method sets up the VineFeeder object by calling necessary functions to
        initialize the UI, store available services dynamically, load services,
        and create buttons dynamically.
        """
        super().__init__()
        self.init_ui()
        self.available_services = {}  # Store available services dynamically
        self.available_service_media_dict = {}
        self.available_services_hlg_status = {}
        self.available_services_options = {}
        self.load_services()  # Discover and load services
        self.create_service_buttons()  # Create buttons dynamically

    def init_ui(self):
        """
        Initialize the UI components and layout.

        This method creates the necessary UI components and sets up the layout.
        
        """
        self.setWindowTitle("VineFeeder")
        layout = QVBoxLayout()

        self.search_url_label = QLabel("URL or Search")
        layout.addWidget(self.search_url_label)
        self.search_url_entry = QLineEdit()
        self.search_url_entry.setStyleSheet("""
            QLineEdit {
                border: 2px solid pink;
            }
            QLineEdit:focus {
                border: 2px solid hotpink;
                outline: none;
            }
        """)

        layout.addWidget(self.search_url_entry)

        highlighted_frame = QFrame()
        self.highlighted_layout = QVBoxLayout()
        highlighted_frame.setLayout(self.highlighted_layout)
        highlighted_frame.setStyleSheet(
            "border: 1px solid pink;"
        )  
        layout.addWidget(highlighted_frame)
        #
        sechighlighted_frame = QFrame()
        self.sechighlighted_layout = QVBoxLayout()
        sechighlighted_frame.setLayout(self.sechighlighted_layout)
        sechighlighted_frame.setStyleSheet(
            "border: 1px solid pink;"
          
        )  
        layout.addWidget(sechighlighted_frame)

        # Batch Mode Layout
        batch_mode_layout = QHBoxLayout()
        self.batch_slider = QSlider(Qt.Orientation.Horizontal)
        self.batch_slider.setMinimum(0)
        self.batch_slider.setMaximum(1)
        self.batch_slider.setTickPosition(QSlider.TickPosition.NoTicks)
        self.batch_slider.setSingleStep(1)
        self.batch_slider.setFixedWidth(80)
        self.batch_slider.valueChanged.connect(self.toggle_batch_mode)
        self.batch_slider.setStyleSheet("""QSlider::groove:horizontal {
            border: 1px solid #999999;
            height: 5px; 
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 hotpink, stop:1 #c4c4c4);
            margin: 2px 0;
            }
            QSlider::handle:horizontal {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #b4b4b4, stop:1 #8f8f8f);
                border: 1px solid #5c5c5c;
                width: 18px;
                height: 18px;
                margin: -12px 0; 
                border-radius: 3px;
            }
            """)
        
        # Batch Mode Label
        self.batch_label = QLabel("Batch Mode")
        self.batch_label.setStyleSheet("color: white; padding-left: 5px; border: none;")

        batch_mode_layout.addWidget(self.batch_label)
        batch_mode_layout.addWidget(self.batch_slider)

        batch_mode_frame = QFrame()
        batch_mode_frame.setLayout(batch_mode_layout)
        batch_mode_frame.setStyleSheet("border: none;")
        self.sechighlighted_layout.addWidget(batch_mode_frame)

        # Batch File Existence Indicator
        self.batch_file_status = QLabel("")
        self.sechighlighted_layout.addWidget(self.batch_file_status)


        # Run Batch Button
        self.run_batch_button = QPushButton("Run Batch")
        self.run_batch_button.clicked.connect(self._run_batch_button_handler)
        self.run_batch_button.setEnabled(False)  # Initially disabled
        self.style_batch_button(self.run_batch_button)
        self.sechighlighted_layout.addWidget(self.run_batch_button)

        # Load batch mode from config
        self.load_batch_mode()

        self.dark_mode_checkbox = QCheckBox("Dark Mode")
        self.dark_mode_checkbox.setChecked(True)  # Set dark mode by default
        self.dark_mode_checkbox.stateChanged.connect(self.toggle_dark_mode)
        layout.addWidget(self.dark_mode_checkbox, alignment=Qt.AlignmentFlag.AlignLeft)

        self.setLayout(layout)
        # Poll for batch.txt changes every 2 seconds
        self.batch_poll_timer = QTimer(self)
        self.batch_poll_timer.timeout.connect(self.update_batch_file_indicator)
        self.batch_poll_timer.start(2000)  # 2000 ms = 2 seconds

        # Use a timer to delay the dark mode application slightly
        QTimer.singleShot(100, self.toggle_dark_mode)  # 100ms delay to ensure rendering

    def load_batch_mode(self):
        try:
            cfg, _ = load_config_with_fallback()
            batch_on = bool(cfg.get("BATCH_DOWNLOAD", False))
            self.batch_slider.setValue(1 if batch_on else 0)
            self.run_batch_button.setEnabled(batch_on)
        except Exception as e:
            console.print(f"[{catppuccin_mocha['text2']}][warning] Could not load batch mode config: {e}[/]")
            self.batch_slider.setValue(0)
            self.run_batch_button.setEnabled(False)

        self.update_batch_file_indicator()

    def _run_batch_button_handler(self):
        from .batchloader import batchload
        batchload()

    def toggle_batch_mode(self, value=None):
        state = (value == 1) if value is not None else (self.batch_slider.value() == 1)
        if state:
            self.batch_label.setStyleSheet("color: lightgreen; padding-left: 5px; border: none")
        else:
            self.toggle_dark_mode()

        self.run_batch_button.setEnabled(state)
        self.update_batch_file_indicator()

        # Persist to config
        try:
            cfg, _ = load_config_with_fallback()
            cfg["BATCH_DOWNLOAD"] = state
            save_project_config(cfg)
        except Exception as e:
            console.print(f"[{catppuccin_mocha['text2']}][warning] Could not update config.yaml: {e}[/]")


    def style_batch_button(self, button):
        button.setStyleSheet("""
            color: white;
            background-color:#1E1E2E;
            border: none;
            padding: 5px;
        """)

    def update_batch_file_indicator(self):
        exists = os.path.exists("./batch.txt")
        if hasattr(self, "_batch_file_last_state") and self._batch_file_last_state == exists:
            return  # no change
        self._batch_file_last_state = exists

        if exists:
            self.batch_file_status.setText("✅ batch file exists")
            self.batch_file_status.setStyleSheet("color: lightgreen; padding-left: 25px; border: none;")
        else:
            self.batch_file_status.setText("❌ batch file missing")
            self.batch_file_status.setStyleSheet("color: hotpink; padding-left: 25px; border: none;")

       

    def toggle_dark_mode(self):
        """
        Toggle the application's dark mode on or off.

        This method is connected to the dark mode checkbox's stateChanged signal.
        When the checkbox is checked, the application is set to dark mode. When unchecked,
        the application is set to light mode.

        In dark mode, the window and text colors are changed to dark colors,
        and the search label and dark mode checkbox text are changed to white.
        Buttons are also set to have a white text color and a dark grey background color.

        In light mode, the window and text colors are set back to their default values,
        and the search label and dark mode checkbox text are changed back to black.
        Buttons are also reset to their default appearance.

        NOTE: This method uses a QTimer to delay the application of the dark mode style slightly.
        This is to ensure that the rendering of the UI components is complete
        before applying the style changes.
        """
        if self.dark_mode_checkbox.isChecked():
            palette = QPalette()
            palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
            palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
            palette.setColor(
                QPalette.ColorRole.Base, QColor(30, 30, 54)
            )  # QColor(35, 35, 35))
            palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
            self.setPalette(palette)
            self.search_url_label.setStyleSheet("color: white;")
            self.dark_mode_checkbox.setStyleSheet("color: white;")

            # Set button text to white in dark mode, remove red border
            for i in range(self.highlighted_layout.count()):
                button = self.highlighted_layout.itemAt(i).widget()
                if isinstance(button, QPushButton):
                    button.setStyleSheet("""
                        color: white;
                        background-color:#1E1E2E;
                        border: none;
                        padding: 5px;
                    """)
                    button.repaint()  # Force update of the button's appearance

        else:
            self.setPalette(QApplication.palette())
            self.search_url_label.setStyleSheet("color: black;")
            self.dark_mode_checkbox.setStyleSheet("color: black;")
            self.batch_label.setStyleSheet("color: black;")

            for i in range(self.highlighted_layout.count()):
                button = self.highlighted_layout.itemAt(i).widget()
                if isinstance(button, QPushButton):
                    button.setStyleSheet("""
                        color: black;
                        background-color: #aeaeae;
                        border: none;
                        padding: 5px;
                    """)
                    button.repaint()  # Force update of the button's appearance
        # Update batch_label based on dark mode and batch mode state
        if self.batch_slider.value() == 1:
            self.batch_label.setStyleSheet("color: lightgreen; padding-left: 5px; border: none;")
        else:
            if self.dark_mode_checkbox.isChecked():
                self.batch_label.setStyleSheet("color: white; padding-left: 5px; border: none;")
            else:
                self.batch_label.setStyleSheet("color: black; padding-left: 5px; border: none;")
            

    def load_services(self):
        """
        Discover subpackages under vinefeeder.services, import them (executes __init__.py),
        read config.yaml, and store both module + loader class for later use.
        """
        import vinefeeder.services as services_pkg

        self.available_services.clear()
        self.available_services_hlg_status.clear()
        self.available_services_options.clear()
        self.available_service_media_dict.clear()

        for _finder, modname, ispkg in pkgutil.iter_modules(services_pkg.__path__):
            if not ispkg:
                continue

            fqname = f"{services_pkg.__name__}.{modname}"

            # Import executes the service's __init__.py
            try:
                module = importlib.import_module(fqname)
            except Exception as e:
                console.print(f"[{catppuccin_mocha['text2']}][warning] Could not import service {fqname}: {e}[/]")
                continue

            # Try reading config.yaml alongside the service package
            cfg = {}
            try:
                cfg_res = resources.files(fqname).joinpath("config.yaml")
                if cfg_res.exists():
                    cfg = yaml.safe_load(cfg_res.read_text(encoding="utf-8")) or {}
            except Exception as e:
                console.print(f"[{catppuccin_mocha['text2']}][warning] Could not read config for {fqname}: {e}[/]")

            service_name = cfg.get("service_name", modname)
            service_media_dict = cfg.get("media_dict", {})
            service_hlg_status = cfg.get("hlg_status", False)
            service_options = cfg.get("options", {})

            # Either explicit loader class from config, or derived
            loader_class = cfg.get("loader_class") or derive_loader_class_name(modname)

            self.available_services_hlg_status[service_name] = service_hlg_status
            self.available_services_options[service_name] = service_options
            self.available_service_media_dict[service_name] = service_media_dict

            # Store metadata for later use (module name + class name)
            self.available_services[service_name] = {
                "module": fqname,
                "loader_class": loader_class,
        }


    
    def load_service(self, service_name: str):
        self.update_batch_file_indicator()

        meta = self.available_services.get(service_name)
        if not meta:
            console.print(f"[{catppuccin_mocha['text2']}]Service {service_name} not found![/]")
            console.print(f"[{catppuccin_mocha['text2']}]Try again[/]")
            sys.exit(0)

        fqname = meta["module"]
        cls_name = meta["loader_class"]

        try:
            module = importlib.import_module(fqname)
            if not hasattr(module, cls_name):
                raise AttributeError(f"{fqname} has no class {cls_name}")
            loader_class = getattr(module, cls_name)
            loader_instance = loader_class()

            hlg_status = self.available_services_hlg_status[service_name]
            options = self.available_services_options[service_name]

            text = self.search_url_entry.text().strip()
            text_to_pass = text if text else None

            if hasattr(loader_instance, "receive"):
                if text_to_pass:
                    if "http" in text_to_pass:
                        loader_instance.receive(1, text_to_pass, None, hlg_status, options)
                        self.clear_search_box()
                        loader_instance.clean_terminal()
                        sys.exit(0)
                    else:
                        loader_instance.receive(3, text_to_pass, None, hlg_status, options)
                        self.clear_search_box()
                        loader_instance.clean_terminal()
                        sys.exit(0)
                else:
                    inx, text_to_pass, found = self.do_action_select(service_name)
                    loader_instance.receive(inx, text_to_pass, found, hlg_status, options)
                    loader_instance.clean_terminal()
                    sys.exit(0)
            else:
                console.print(f"[{catppuccin_mocha['pink']}]Service class {cls_name} has no 'receive' method[/]")

        except Exception as e:
            console.print(f"[{catppuccin_mocha['pink']}]Error loading service: {service_name}  {e}[/]")
            console.print(f"[{catppuccin_mocha['pink']}]Try again[/]")
            sys.exit(0)



    def create_service_buttons(self):
        """Create buttons for each dynamically loaded service in alphabetical order."""
        # Sort the services alphabetically by their names
        for service_name in sorted(self.available_services.keys()):
            button = QPushButton(service_name)
            button.clicked.connect(
                self.run_load_service_thread(service_name)
            )  # Bind to threaded service loading
            self.highlighted_layout.addWidget(button)
            #

    def do_action_select(self, service_name):
        """
        Top level choice for action required. Called if search_box is empty.
        Uses beaupy to display a list of 4 options:
            - Search by keyword
            - Greedy Search by URL
            - Browse by Category
            - Download by URL
        Uses the selected option to call the appropriate function:
            - 0 for greedy search with url
            - 1 for direct url download
            - 2 for browse
            - 3 for search with keyword
        Returns a tuple of the function selector and the url or None if no valid data is entered.
        """

        fn = [
            "Greedy Search by URL",
            "Download by URL",
            "Browse by Category",
            "Search by keyword(s)",
        ]
        # check for batch.txt
        self.update_batch_file_indicator()

        action = select(
            fn, preprocessor=lambda val: prettify(val), cursor="🢧", cursor_style="pink1"
        )

        if "Greedy" in action:
            url = input("URL for greedy search ")
            return 0, url, None

        elif "Download" in action:
            url = input("URL for direct download ")
            return 1, url, None

        elif "Browse" in action:
            media_dict = self.available_service_media_dict[service_name]
            beaupylist = []
            for item in media_dict:
                beaupylist.append(item)
            found = select(
                beaupylist,
                preprocessor=lambda val: prettify(val),
                cursor="🢧",
                cursor_style="pink1",
                page_size=PAGE_SIZE,
                pagination=True,
            )
            url = media_dict[found]
            return 2, url, found  # found is category

        elif "Search" in action:
            keyword = input("Keyword(s) for search ")
            return 3, keyword, None

        else:
            console.print(f"[{catppuccin_mocha['text2']}]No valid data entered![/]")
            sys.exit(0)

    def run_load_service_thread(self, service_name):
        """Start a new thread to load the service."""
        return lambda: threading.Thread(
            target=self.load_service, args=(service_name,)
        ).start()

    

    def clear_search_box(self):
        self.search_url_entry.clear()


@click.command()
@click.option(
    "--service-folder",
    type=str,
    default="services",
    help="Specify a service folder for adding **Devine** download options.",
)
@click.option(
    "--list-services",
    is_flag=True,
    help="List available services in the specified service folder.",
)
@click.option(
    "--select-series",
    is_flag=True,
    help="How to select which series you need from those available",
)
def cli(service_folder, list_services, select_series):
    """
    uv run vinefeeder --help to show help\n
    uv run vinefeeder --list-services  to list available services\n
    uv run vinefeeder --service-folder <folder_name> to edit config.yaml
    uv run vinefeeder --select-series  list, range or 'all'\n\n
    In the GUI:-
    The text box will take keyword(s) or a URL for download from a button selected service.
    Or leave the text box blank for further options when the service button is clicked.\n
    Batch Mode: slide the slider to the right to engage Batch Mode. 
    All devine commands will be saved to a batch.txt file in the VineFeeder folder.
    When a green 'batch file exists' notice is present the 'Run Batch' button will process
    the batch.txt file.
    At the end of download the option to delete the batch.txt file will appear.
    It may be deleted manually at any time. 

    """
    # Ensure service-folder paths are handled correctly
    if os.path.isabs(service_folder):
        base_path = os.path.abspath(service_folder)
    else:
        base_path = (
            os.path.abspath(os.path.join("packages/vinefeeder/src/vinefeeder/services", service_folder))
            if service_folder != "services"
            else os.path.abspath("services")
        )

    # Handle --list-services option
    if list_services:
        if not os.path.exists(base_path):
            console.print(f"[{catppuccin_mocha['pink']}]Error: The service folder '{base_path}' does not exist![/]")
            return

        console.print(f"[{catppuccin_mocha['pink']}]Available services in '{base_path}':")
        for service in os.listdir(base_path):
            service_dir = os.path.join(base_path, service)
            config_path = os.path.join(service_dir, "config.yaml")
            if os.path.isdir(service_dir) and os.path.exists(config_path):
                console.print(f"[{catppuccin_mocha['text2']}] - {service}[/]")
        return

    # Handle --select-series option
    if select_series:
        console.print(f"[{catppuccin_mocha['text2']}]Series Selection:[/]")
        console.print(
            f"[{catppuccin_mocha['text2']}]Check the available series.\nUse, for example,\n1,3,7 or a range 3..8,\nor 'all' or 0 to show all series.[/]"
        )
        return

    # Default behavior: Open config.yaml
    config_path = os.path.join(base_path, "config.yaml")

    # Check if the services folder exists
    if not os.path.exists(base_path):
        console.print(f"[{catppuccin_mocha['pink']}]Error: The service folder '{base_path}' does not exist![/]")
        return

    # Check if config.yaml exists
    if not os.path.exists(config_path):
        console.print(
            f"[{catppuccin_mocha['text2']}]Error: The file '{config_path}' does not exist! Please create it or specify a valid service folder.[/]"
        )
        return

    # Open the file in the system's default text editor
    try:
        if os.name == "nt":  # For Windows
            os.startfile(config_path)
        elif os.name == "posix":  # For Linux/Mac
            subprocess.run(["xdg-open", config_path], check=True)
        else:
            console.print(f"[{catppuccin_mocha['text2']}]Unsupported operating system.[/]")
    except Exception as e:
        console.print(f"[{catppuccin_mocha['pink']}]Failed to open the file: {e}[/]")


def main():
    """
    Entry point for the script. Decides between GUI launch and CLI behavior.
    """
    if len(sys.argv) == 1:
        # say hello nicely
        pretty_print()
        app = QApplication(sys.argv)
        window = VineFeeder()
        window.show()
        sys.exit(app.exec())
    else:
        # CLI arguments passed, handle them with click
        cli()


if __name__ == "__main__":
    main()
