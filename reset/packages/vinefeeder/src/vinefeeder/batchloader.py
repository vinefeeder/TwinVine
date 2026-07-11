import subprocess
import os
import threading
from rich.console import Console
from .pretty import catppuccin_mocha
import time
from .config_loader import load_config_with_fallback, get_bool

console = Console()

def _batchloader():

    cfg, _ = load_config_with_fallback()
    TERMINAL_RESET = bool(cfg.get("TERMINAL_RESET", False))
    try:
        with open('./batch.txt', 'r') as file:
            for line in file:
                line = line.strip()
                if line:
                    print(line)
                    subprocess.run(line, shell=True)
     
        print("Batch processing complete.")
        yesno = input("delete batch file?  (y/n) ")
        if yesno.lower() == 'y':
            os.remove("./batch.txt")
            console.print(f"batch.txt deleted.\n\n[{catppuccin_mocha['pink']}]Ready.")
        if TERMINAL_RESET:
            console.print(f"[{catppuccin_mocha['text2']}]Preparing to reset Terminal[/]")
            time.sleep(5)
            if os.name == 'nt':  # Windows
                os.system('cls')
            else:  # Unix/Linux/macOS
                try:
                    subprocess.run(['reset'], check=True)
                    console.print(f"[{catppuccin_mocha['pink']}]Ready![/]")
                except Exception:
                    os.system('clear')  # fallback if 'reset' is not available
                        
            



    except FileNotFoundError:
        print("batch.txt not found.\nFirst set batch mode to True in the GUI, then choose some videos.")
    except Exception as e:
        print(f"An error occurred: {e}")


def batchload():
    thread = threading.Thread(target=_batchloader, daemon=True)
    thread.start()



if __name__ == "__main__":
    batchload() 