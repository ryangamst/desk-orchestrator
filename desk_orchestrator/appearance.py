"""Task appearance choices and backwards-compatible defaults."""
import hashlib

COLORS = {"lavender": "Lavender", "blue": "Blue", "teal": "Teal",
          "amber": "Amber", "rose": "Rose"}
ICONS = {"layers": "Layers", "monitor": "Monitor", "briefcase": "Briefcase",
         "gamepad": "Gamepad", "disc": "Disc", "record": "Record",
         "audio": "Speaker", "numpad": "Numpad", "switch": "Switch",
         "activity": "Activity", "settings": "Sliders", "overview": "Grid"}


def task_appearance(name, task=None):
    """Resolve saved choices, retaining the original identity for older tasks."""
    presets = {"personal": ("lavender", "monitor"), "work": ("blue", "briefcase"),
               "steam_deck": ("teal", "gamepad"), "cds": ("amber", "disc"),
               "lps": ("rose", "record")}
    palette = tuple(COLORS)
    color, icon = presets.get(name, (palette[int(hashlib.sha256((name or "").encode()).hexdigest()[:8], 16) % len(palette)], "layers"))
    task = task or {}
    return {"color": task.get("color", color), "icon": task.get("icon", icon)}
