import os
import sys
import json
import re
import traceback
from dataclasses import dataclass
from typing import List, Optional, Dict

# GUI framework import (PyQt5 preferred, fallback to PySide2)
try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    QtWidgetsModule = QtWidgets
except Exception:
    from PySide2 import QtCore, QtGui, QtWidgets
    QtWidgetsModule = QtWidgets

import ctypes
import winreg

APP_NAME = "SketchJVM"
CONFIG_DIR = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~"), APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

COMMON_SCAN_PATHS = [
    r"C:\Program Files\Java",
    r"C:\Program Files (x86)\Java",
    os.path.join(os.getenv("USERPROFILE", ""), r"AppData\Local\Programs\Java"),
]

WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002
HWND_BROADCAST = 0xffff


def broadcast_env_change():
    """Notify Windows that environment variables changed (so new processes pick them up)."""
    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            5000,
            None,
        )
    except Exception:
        pass


def ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def load_config() -> dict:
    ensure_config_dir()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg: dict):
    ensure_config_dir()
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


@dataclass
class JDKEntry:
    version: str
    path: str
    is_default: bool = False


def parse_release_file(release_path: str) -> Optional[str]:
    """Parse the 'release' file to extract JAVA_VERSION"""
    try:
        with open(release_path, "r", encoding="utf-8") as f:
            text = f.read()
        # typical line: JAVA_VERSION="1.8.0_241" or JAVA_VERSION="17.0.1"
        m = re.search(r'JAVA_VERSION\s*=\s*"([^"]+)"', text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def validate_jdk_dir(path: str) -> Optional[str]:
    """
    Returns version string if valid JDK; otherwise None.
    Checks for bin\\javac.exe and release file with JAVA_VERSION.
    """
    try:
        javac = os.path.join(path, "bin", "javac.exe")
        release = os.path.join(path, "release")
        if os.path.isfile(javac) and os.path.isfile(release):
            v = parse_release_file(release)
            if v:
                return v
    except Exception:
        pass
    return None


def find_jdks_in_path(root: str, max_depth=3) -> List[JDKEntry]:
    """
    Recursively search root for directories containing 'jdk' in their name.
    Limit depth for performance unless root is small.
    """
    found = []
    root = os.path.abspath(root)
    if not os.path.exists(root):
        return []
    # walk but limit depth by path components
    root_parts = root.rstrip(os.sep).split(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(os.path.abspath(dirpath).split(os.sep)) - len(root_parts)
        # optional: skip extremely deep recursion
        if depth > 8:
            # prune deeper dirs for performance
            dirnames[:] = []
            continue
        basename = os.path.basename(dirpath).lower()
        if "jdk" in basename:
            v = validate_jdk_dir(dirpath)
            if v:
                found.append(JDKEntry(version=v, path=dirpath))
        # if we are past max_depth, prune
        if depth >= max_depth:
            dirnames[:] = []
    return found


def scan_common_locations(custom_paths: List[str]) -> List[JDKEntry]:
    """
    Scan common directories and any provided custom paths.
    Returns unique JDKEntry list (unique by path).
    """
    seen = {}
    candidates = []

    # include commons and custom
    for base in COMMON_SCAN_PATHS + custom_paths:
        if not base:
            continue
        try:
            if os.path.isfile(base):
                continue
            # If base exists and contains multiple children, check known child directories first
            if os.path.isdir(base):
                # First: check direct children for JDK folders (e.g., jdk-17.0.2)
                try:
                    for child in os.listdir(base):
                        full = os.path.join(base, child)
                        if os.path.isdir(full):
                            v = validate_jdk_dir(full)
                            if v and full not in seen:
                                seen[full] = JDKEntry(version=v, path=full)
                    # Now do a recursive search for 'jdk' directories
                    found = find_jdks_in_path(base)
                    for e in found:
                        if e.path not in seen:
                            seen[e.path] = e
                except PermissionError:
                    continue
            else:
                # maybe it's a path to a JDK itself
                v = validate_jdk_dir(base)
                if v and base not in seen:
                    seen[base] = JDKEntry(version=v, path=base)
        except Exception:
            continue

    return sorted(seen.values(), key=lambda e: e.version, reverse=True)


def get_user_env_variable(name: str) -> Optional[str]:
    """Read a user environment variable from HKCU\\Environment"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ) as key:
            try:
                val, _ = winreg.QueryValueEx(key, name)
                return val
            except FileNotFoundError:
                return None
    except PermissionError:
        return None
    except Exception:
        return None


def set_user_env_variable(name: str, value: str):
    """
    Set a user environment variable in HKCU\\Environment.
    This updates the registry but you should call broadcast_env_change() after.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_EXPAND_SZ, value)
    except PermissionError as e:
        raise e


def get_user_path() -> Optional[str]:
    return get_user_env_variable("Path") or get_user_env_variable("PATH")


def set_user_path(new_path: str):
    return set_user_env_variable("Path", new_path)


def is_path_bin_from_java(path_entry: str, java_home_candidate: str) -> bool:
    """Check whether a PATH entry points to a bin folder under a given JAVA_HOME"""
    try:
        normalized = os.path.normcase(os.path.normpath(path_entry.strip().strip('"')))
        candidate = os.path.normcase(os.path.normpath(os.path.join(java_home_candidate, "bin")))
        return normalized == candidate
    except Exception:
        return False


def update_user_java_environment(jdk_path: str):
    """
    Updates user-level JAVA_HOME and modifies user-level PATH to ensure java bin is first.
    Strategy:
      - Set HKCU\\Environment\\JAVA_HOME = jdk_path
      - Read user PATH, remove any existing entries that equal previous JAVA_HOME\bin, and
        prepend new JDK's bin path
    """
    try:
        prev_java_home = get_user_env_variable("JAVA_HOME")
        prev_path = get_user_path() or os.environ.get("PATH", "")

        new_java_home = jdk_path
        new_bin = os.path.join(new_java_home, "bin")

        # Tokenize PATH (semicolon-separated)
        parts = [p for p in prev_path.split(";") if p.strip() != ""]
        # Remove entries that point exactly to previous JAVA_HOME\bin or that are the same as new_bin
        filtered = []
        for p in parts:
            if prev_java_home and is_path_bin_from_java(p, prev_java_home):
                continue
            if is_path_bin_from_java(p, new_java_home):
                continue
            filtered.append(p)
        # Prepend new_bin if not present
        new_parts = [new_bin] + filtered

        # Rebuild PATH
        new_path_value = ";".join(new_parts)

        # Write registry
        set_user_env_variable("JAVA_HOME", new_java_home)
        set_user_path(new_path_value)
        broadcast_env_change()
    except PermissionError as pe:
        raise pe
    except Exception as e:
        raise e


def get_effective_java_home() -> Optional[str]:
    """Return effective JAVA_HOME for user if set, else system environment via os.environ."""
    user_val = get_user_env_variable("JAVA_HOME")
    if user_val:
        return os.path.expandvars(user_val)
    # fallback to process env
    return os.environ.get("JAVA_HOME")


class SketchStyles:
    LIGHT_BG = "#fdf6e3"  # warm pastel
    LIGHT_CARD = "#ffffff"
    LIGHT_ACCENT = "#f7d6e0"
    DARK_BG = "#1f2226"
    DARK_CARD = "#2b2f33"
    FONT_FAMILY = "Comic Sans MS, 'Comic Neue', 'Patrick Hand', Arial, sans-serif"


class JDKCard(QtWidgets.QFrame):
    def __init__(self, jdk: JDKEntry, parent=None):
        super().__init__(parent)
        self.jdk = jdk
        self.setup_ui()
        self.set_graphics_effects()

    def setup_ui(self):
        self.setObjectName("jdkCard")
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setFrameShadow(QtWidgets.QFrame.Raised)
        self.setStyleSheet("""
        QFrame#jdkCard {
            border-radius: 12px;
            margin: 8px;
            padding: 12px;
            background: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0, stop:0 rgba(255,255,255,0.98), stop:1 rgba(250,250,250,0.98));
            border: 2px solid rgba(0,0,0,0.06);
        }
        QLabel.versionLabel {
            font-size: 14pt;
            font-weight: 700;
        }
        QLabel.pathLabel {
            font-size: 9pt;
            color: #666666;
        }
        QPushButton.setDefaultBtn {
            background: #ffd9e8;
            border-radius: 10px;
            border: 1px dashed rgba(0,0,0,0.12);
            padding: 6px 10px;
        }
        QPushButton.setDefaultBtn:hover {
            transform: translateY(-2px);
        }
        """)
        layout = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        self.ver_label = QtWidgets.QLabel(f"Java Ver. {self.jdk.version}")
        self.ver_label.setObjectName("versionLabel")
        self.ver_label.setProperty("class", "versionLabel")
        self.path_label = QtWidgets.QLabel(self.jdk.path)
        self.path_label.setObjectName("pathLabel")
        self.path_label.setProperty("class", "pathLabel")
        self.path_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        left_v = QtWidgets.QVBoxLayout()
        left_v.addWidget(self.ver_label)
        left_v.addWidget(self.path_label)
        top.addLayout(left_v)

        # right side: set button if not default, else highlight
        self.btn_set = QtWidgets.QPushButton("Set to default")
        self.btn_set.setProperty("class", "setDefaultBtn")
        self.btn_set.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_set.setFixedWidth(120)
        top.addStretch()
        top.addWidget(self.btn_set)

        layout.addLayout(top)

        if self.jdk.is_default:
            # highlight the card
            self.setStyleSheet(self.styleSheet() + """
            QFrame#jdkCard {
                border: 2px solid rgba(50,160,100,0.35);
                background: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0, stop:0 rgba(240,255,245,0.98), stop:1 rgba(245,255,250,0.98));
            }
            """)
            self.btn_set.hide()


    def set_graphics_effects(self):
        # subtle drop shadow
        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(12)
        shadow.setOffset(3, 3)
        shadow.setColor(QtGui.QColor(0, 0, 0, 40))
        self.setGraphicsEffect(shadow)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sketchboard Java Version Manager")
        self.resize(920, 640)
        self.config = load_config()
        self.custom_paths = self.config.get("last_scanned_paths", [])
        self.selected_jdk = self.config.get("selected_jdk", None)
        self.theme = self.config.get("theme", "light")
        self._current_jdks: List[JDKEntry] = []
        self.setup_ui()
        # initial scan
        self.refresh_scan()

    def setup_ui(self):
        # central widget
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        # Top bar: input + scan button + refresh icon + theme toggle
        top_bar = QtWidgets.QHBoxLayout()
        self.path_input = QtWidgets.QLineEdit()
        self.path_input.setPlaceholderText("Enter custom path to scan (or drop a folder here)...")
        # if last custom path exists show
        if self.custom_paths:
            self.path_input.setText(self.custom_paths[-1])

        self.scan_btn = QtWidgets.QPushButton("Scan")
        self.scan_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.scan_btn.setFixedWidth(100)
        self.scan_btn.clicked.connect(self.on_scan_clicked)

        self.refresh_btn = QtWidgets.QToolButton()
        self.refresh_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.refresh_btn.setToolTip("Refresh list")
        self.refresh_btn.setText("↻")
        self.refresh_btn.clicked.connect(self.refresh_scan)

        self.theme_toggle = QtWidgets.QPushButton("Toggle Theme")
        self.theme_toggle.clicked.connect(self.toggle_theme)
        self.theme_toggle.setFixedWidth(110)
        top_bar.addWidget(self.path_input)
        top_bar.addWidget(self.scan_btn)
        top_bar.addWidget(self.refresh_btn)
        top_bar.addWidget(self.theme_toggle)
        main_layout.addLayout(top_bar)

        # main area: scrollable list
        self.scroll_area = QtWidgets.QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.list_container = QtWidgets.QWidget()
        self.list_layout = QtWidgets.QVBoxLayout(self.list_container)
        self.list_layout.setAlignment(QtCore.Qt.AlignTop)
        self.scroll_area.setWidget(self.list_container)
        main_layout.addWidget(self.scroll_area)

        # footer: status label
        footer = QtWidgets.QHBoxLayout()
        self.status_label = QtWidgets.QLabel("Ready")
        footer.addWidget(self.status_label)
        footer.addStretch()
        main_layout.addLayout(footer)

        # Drag & drop support for folder dropping
        self.setAcceptDrops(True)

        self.apply_sketch_theme()

    def apply_sketch_theme(self):
        """Apply a playful, sketchboard-like QSS style based on theme."""
        if self.theme == "light":
            bg = SketchStyles.LIGHT_BG
            card = SketchStyles.LIGHT_CARD
            accent = SketchStyles.LIGHT_ACCENT
            text_color = "#222222"
        else:
            bg = "#111218"
            card = SketchStyles.DARK_CARD
            accent = "#3a3f47"
            text_color = "#e6e6e6"

        qss = f"""
        QWidget {{
            background: {bg};
            font-family: {SketchStyles.FONT_FAMILY};
            color: {text_color};
        }}
        QLineEdit {{
            border: 2px dashed rgba(0,0,0,0.08);
            border-radius: 10px;
            padding: 8px;
            background: rgba(255,255,255,0.9);
            min-height: 36px;
        }}
        QPushButton {{
            border-radius: 10px;
            padding: 8px;
            min-height: 34px;
            background: {accent};
            border: 2px solid rgba(0,0,0,0.06);
        }}
        QPushButton:hover {{
            transform: translateY(-2px);
        }}
        QToolButton {{
            border-radius: 6px;
            padding: 6px;
            min-width: 28px;
            min-height: 28px;
        }}
        QScrollArea {{
            border: none;
        }}
        """
        self.setStyleSheet(qss)

    def toggle_theme(self):
        self.theme = "dark" if self.theme == "light" else "light"
        self.config["theme"] = self.theme
        save_config(self.config)
        self.apply_sketch_theme()

    def dragEnterEvent(self, e: QtGui.QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QtGui.QDropEvent):
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if os.path.isdir(path):
                self.path_input.setText(path)
                self.on_scan_clicked()

    def clear_list(self):
        # remove widgets from layout
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def refresh_scan(self):
        self.status_label.setText("Scanning for JDKs...")
        QtWidgets.QApplication.processEvents()
        custom = []
        input_path = self.path_input.text().strip()
        if input_path:
            custom.append(input_path)
            # record it in config
            self.custom_paths.append(input_path)
            # keep only last 8
            self.custom_paths = list(dict.fromkeys(self.custom_paths))[-8:]
            self.config["last_scanned_paths"] = self.custom_paths
            save_config(self.config)

        jdks = scan_common_locations(custom)
        # determine default
        effective = get_effective_java_home()
        for e in jdks:
            if effective:
                # compare normalized paths
                try:
                    if os.path.normcase(os.path.normpath(os.path.expandvars(e.path))) == os.path.normcase(os.path.normpath(os.path.expandvars(effective))):
                        e.is_default = True
                    else:
                        e.is_default = False
                except Exception:
                    e.is_default = False
            else:
                e.is_default = False

        self._current_jdks = jdks
        self.populate_list()
        if jdks:
            self.status_label.setText(f"Found {len(jdks)} JDK(s). Current default: {effective or 'None'}")
        else:
            self.status_label.setText("No valid JDKs found. Try scanning a custom path.")
        QtWidgets.QApplication.processEvents()

    def on_scan_clicked(self):
        # scan provided path only if present, otherwise full scan
        self.refresh_scan()

    def populate_list(self):
        self.clear_list()
        if not self._current_jdks:
            lbl = QtWidgets.QLabel("No JDKs found. Try scanning a folder or add a custom install path.")
            lbl.setWordWrap(True)
            self.list_layout.addWidget(lbl)
            return
        for jdk in self._current_jdks:
            card = JDKCard(jdk)
            card.btn_set.clicked.connect(lambda _, path=jdk.path: self.on_set_default(path))
            # allow clicking the whole card to set default too (if not default)
            if not jdk.is_default:
                card.mousePressEvent = lambda ev, path=jdk.path: self.on_set_default(path)
            self.list_layout.addWidget(card)
        self.list_layout.addStretch()

    def on_set_default(self, path: str):
        # confirm
        reply = QtWidgets.QMessageBox.question(
            self,
            "Set Default JDK",
            f"Set the selected JDK as user-level default?\n\n{path}",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        try:
            update_user_java_environment(path)
            self.status_label.setText(f"JAVA_HOME set to {path}")
            # save selected jdk in config
            self.config["selected_jdk"] = path
            save_config(self.config)
            # refresh to update UI badges
            self.refresh_scan()
            QtWidgets.QMessageBox.information(self, "Success", "JAVA_HOME and PATH updated in user environment.\nYou may need to restart applications to pick up the change.")
        except PermissionError:
            QtWidgets.QMessageBox.critical(self, "Permission error", "Unable to write user environment variables (permission denied). Run the app with appropriate permissions.")
        except Exception as e:
            tb = traceback.format_exc()
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to update environment: {e}\n\n{tb}")
            self.status_label.setText("Error updating environment.")

    # optional: menu actions for extras
    def closeEvent(self, event: QtGui.QCloseEvent):
        # save config (already saved on changes, but ensure)
        self.config["last_scanned_paths"] = self.custom_paths
        save_config(self.config)
        event.accept()


def main():
    app = QtWidgetsModule.QApplication(sys.argv)
    app.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
