from rich.console import Console
from rich.panel import Panel


# thanks rlaphoenix for catppuccin_mocha color scheme


# Define the Catppuccin Mocha color scheme
catppuccin_mocha = {
    "bg": "rgb(30,30,46)",
    "text": "rgb(205,214,244)",
    "text2": "rgb(162,169,193)",
    "black": "rgb(69,71,90)",
    "bright_black": "rgb(88,91,112)",
    "red": "rgb(243,139,168)",
    "green": "rgb(166,227,161)",
    "yellow": "rgb(249,226,175)",
    "blue": "rgb(137,180,250)",
    "pink": "rgb(245,194,231)",
    "cyan": "rgb(148,226,213)",
    "gray": "rgb(166,173,200)",
    "bright_gray": "rgb(186,194,222)",
    "dark_gray": "rgb(54,54,84)"
}

def pretty_print():
    console = Console()

    # Combined text block with the color scheme
    text_block = (
        f"[{catppuccin_mocha['pink']}] _   ___          ____           __" "\n"
        f"| | / (_)__  ___ / __/__ ___ ___/ /__ ____" "\n"
        f"| |/ / / _ \\/ -_) _// -_) -_) _  / -_) __/" "\n"
        f"|___/_/_//_/\\__/__/  \\__/\\__/\\_,_/\\__/_/     [/]" + "\n\n"
        f"[{catppuccin_mocha['text2']}]© 2024-25  A_n_g_e_l_a[/]\n\n"
        f"[{catppuccin_mocha['pink']}]driving ...[/]\n\n"
        f"[{catppuccin_mocha['text2']}]░█▀▀░█▀█░█░█░▀█▀░█▀▀░█▀▄" + "\n"
        f"[{catppuccin_mocha['text2']}]░█▀▀░█░█░▀▄▀░░█░░█▀▀░█░█" + "\n"
        f"[{catppuccin_mocha['text2']}]░▀▀▀░▀░▀░░▀░░▀▀▀░▀▀▀░▀▀░" + "\n\n"
        f"[{catppuccin_mocha['gray']}]© 2019-2025 rlaphoenix [/]" + "\n"
 

        #f"[{catppuccin_mocha['blue']}]https://github.com/vinefeeder[/]"
    )

    # 
    instructions = (
        f"[{catppuccin_mocha['pink']}]uv run vinefeeder --help for options[/]\n"
        f"[{catppuccin_mocha['pink']}]uv run envied -? for options.[/]")

    # Display the panel
    panel = Panel(
        text_block + "\n\n" + instructions,
        title=f"[{catppuccin_mocha['pink']}]TwinVine[/]",
        border_style=catppuccin_mocha["text2"],
        padding=(0, 17, 1, 16),
        style=f"on {catppuccin_mocha['bg']}",
        expand=False
    )

    console.print(panel)
    
def create_clean_panel(content, title=""):
    """Create and return a clean panel with specified content and optional title."""
    panel = Panel(
        content,
        title=title,
        border_style=catppuccin_mocha["text2"],
        padding=(1, 1, 1, 1),
        style=f"on {catppuccin_mocha['bg']}",
        expand=False
    )
    return panel

