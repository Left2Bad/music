from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from tkinter import BOTH, END, LEFT, RIGHT, StringVar, Tk, Toplevel, filedialog, messagebox
from tkinter import ttk

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    def load_dotenv(*args: Any, **kwargs: Any) -> bool:
        return False


try:
    import google.generativeai as genai
except ImportError:  # pragma: no cover - optional dependency
    genai = None


AUDIO_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".alac",
    ".flac",
    ".m4a",
    ".m4b",
    ".m4p",
    ".mid",
    ".midi",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
    ".webm",
}

OUTPUT_FORMATS = [
    "mp3",
    "wav",
    "flac",
    "ogg",
    "opus",
    "m4a",
    "aac",
    "wma",
    "aiff",
]

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')


@dataclass
class RenamePreviewRow:
    source: Path
    proposed_name: str
    selected: bool = True


def human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def sanitize_filename_part(value: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", value).strip().rstrip(".")
    return cleaned or "untitled"


def normalize_target_filename(raw_name: str, default_suffix: str) -> str:
    cleaned_name = sanitize_filename_part(Path(raw_name).name)
    stem = sanitize_filename_part(Path(cleaned_name).stem)
    suffix = Path(cleaned_name).suffix or default_suffix
    if suffix and not suffix.startswith("."):
        suffix = f".{suffix}"
    return f"{stem}{suffix}"


def unique_destination_path(folder: Path, desired_filename: str) -> Path:
    normalized_name = normalize_target_filename(desired_filename, Path(desired_filename).suffix or "")
    candidate = folder / normalized_name
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while candidate.exists():
        candidate = folder / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def is_audio_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS


def path_is_inside(path: Path, parents: list[Path]) -> bool:
    resolved = path.resolve()
    for parent in parents:
        try:
            parent_resolved = parent.resolve()
        except FileNotFoundError:
            continue
        try:
            if resolved == parent_resolved or resolved.is_relative_to(parent_resolved):
                return True
        except AttributeError:
            if str(resolved).startswith(str(parent_resolved)):
                return True
    return False


def scan_audio_files(folder: Path, exclude_dirs: list[Path] | None = None) -> list[Path]:
    if not folder.exists() or not folder.is_dir():
        return []

    exclude_dirs = exclude_dirs or []
    files: list[Path] = []
    for path in folder.rglob("*"):
        if is_audio_file(path) and not path_is_inside(path, exclude_dirs):
            files.append(path)
    return sorted(files, key=lambda item: str(item).lower())


def summarize_by_format(files: list[Path]) -> list[tuple[str, int, int]]:
    summary: dict[str, list[int]] = {}
    for path in files:
        fmt = path.suffix.lower().lstrip(".") or "unknown"
        bucket = summary.setdefault(fmt, [0, 0])
        bucket[0] += 1
        bucket[1] += path.stat().st_size
    return sorted(((fmt, count, size) for fmt, (count, size) in summary.items()), key=lambda item: item[0])


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def load_app_environment(env_file: Path) -> None:
    load_dotenv(dotenv_path=env_file)


def split_response_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"\s*```$", "", stripped).strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(stripped[start : end + 1])

    start = stripped.find("[")
    end = stripped.rfind("]")
    if start != -1 and end != -1 and end > start:
        return json.loads(stripped[start : end + 1])

    raise ValueError("Gemini returned invalid JSON")


def convert_audio_file(ffmpeg_path: str, source: Path, destination: Path) -> None:
    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        str(destination),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"ffmpeg failed for {source.name}")


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


class MusicToolsApp:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("Music Tools")
        self.root.geometry("1020x720")
        self.root.minsize(940, 640)

        self.base_folder = Path(__file__).resolve().parent
        load_app_environment(self.base_folder / ".env")
        self.music_root = self.base_folder
        self.new_folder = self.music_root / "New"
        ensure_directory(self.new_folder)

        self.root_path_var = StringVar(value=str(self.music_root))
        self.library_count_var = StringVar(value="0")
        self.library_size_var = StringVar(value="0 B")
        self.new_count_var = StringVar(value="0")
        self.new_size_var = StringVar(value="0 B")
        self.ffmpeg_status_var = StringVar(value=self.ffmpeg_status_text())
        self.gemini_status_var = StringVar(value=self.gemini_status_text())
        self.status_var = StringVar(value="Папка New создана и готова к работе")

        self.converter_window: Toplevel | None = None
        self.converter_tree: ttk.Treeview | None = None
        self.output_format_var = StringVar(value="mp3")
        self.last_output_dir: Path | None = None
        self.progress_bar: ttk.Progressbar | None = None

        self.preview_window: Toplevel | None = None
        self.preview_tree: ttk.Treeview | None = None
        self.preview_count_var = StringVar(value="0 / 0")
        self.preview_status_var = StringVar(value="")
        self.preview_rows: list[RenamePreviewRow] = []
        self.preview_item_ids: list[str] = []

        self.worker_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.active_worker: threading.Thread | None = None
        self.current_task: str | None = None

        self.build_style()
        self.build_main_ui()
        self.refresh_all()

    def build_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.root.configure(bg="#10151f")
        style.configure("TFrame", background="#10151f")
        style.configure("Card.TFrame", background="#151b28", relief="flat")
        style.configure("TLabel", background="#10151f", foreground="#e5e7eb", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 20), foreground="#f8fafc")
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10), foreground="#cbd5e1")
        style.configure("StatValue.TLabel", font=("Segoe UI Semibold", 24), foreground="#ffffff", background="#151b28")
        style.configure("StatCaption.TLabel", font=("Segoe UI", 10), foreground="#94a3b8", background="#151b28")
        style.configure("TButton", font=("Segoe UI Semibold", 10), padding=8)
        style.map("TButton", foreground=[("active", "#ffffff")])
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=28)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))
        style.configure("TCombobox", padding=6)
        style.configure("Horizontal.TProgressbar", thickness=16)

    def set_music_root(self, folder: Path) -> None:
        self.music_root = folder.resolve()
        self.new_folder = self.music_root / "New"
        ensure_directory(self.new_folder)
        self.root_path_var.set(str(self.music_root))

    def build_main_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=BOTH, expand=True)

        header = ttk.Frame(outer, style="Card.TFrame", padding=18)
        header.pack(fill=BOTH)
        ttk.Label(header, text="Music Tools", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Сканирует библиотеку, создает New автоматически, переименовывает новые песни через Gemini и конвертирует по форматам.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(6, 0))

        root_row = ttk.Frame(outer, padding=(0, 16, 0, 8))
        root_row.pack(fill=BOTH)
        ttk.Label(root_row, text="Основная папка:").pack(side=LEFT)
        self.root_entry = ttk.Entry(root_row, textvariable=self.root_path_var)
        self.root_entry.pack(side=LEFT, fill=BOTH, expand=True, padx=10)
        ttk.Button(root_row, text="Выбрать", command=self.choose_music_root).pack(side=LEFT, padx=(0, 8))
        ttk.Button(root_row, text="Открыть", command=self.open_music_root).pack(side=LEFT, padx=(0, 8))
        ttk.Button(root_row, text="Open New", command=self.open_new_folder).pack(side=LEFT)

        stats = ttk.Frame(outer)
        stats.pack(fill=BOTH, pady=(4, 12))
        for index, (caption, value_var) in enumerate(
            [
                ("Песен в библиотеке", self.library_count_var),
                ("Песен в New", self.new_count_var),
                ("Вес библиотеки", self.library_size_var),
                ("Состояние", self.status_var),
            ]
        ):
            card = ttk.Frame(stats, style="Card.TFrame", padding=18)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 10, 0))
            stats.columnconfigure(index, weight=1)
            ttk.Label(card, textvariable=value_var, style="StatValue.TLabel").pack(anchor="w")
            ttk.Label(card, text=caption, style="StatCaption.TLabel").pack(anchor="w", pady=(4, 0))

        actions = ttk.Frame(outer, padding=(0, 2, 0, 12))
        actions.pack(fill=BOTH)
        ttk.Button(actions, text="Обновить", command=self.refresh_all).pack(side=LEFT)
        ttk.Button(actions, text="Конвертер", command=self.open_converter_window).pack(side=LEFT, padx=10)
        ttk.Button(actions, text="Разобрать New через Gemini", command=self.start_gemini_analysis).pack(side=LEFT, padx=10)

        convert_row = ttk.Frame(outer, padding=(0, 0, 0, 12))
        convert_row.pack(fill=BOTH)
        ttk.Label(convert_row, text="Всё в формат:").pack(side=LEFT)
        ttk.Combobox(convert_row, textvariable=self.output_format_var, values=OUTPUT_FORMATS, state="readonly", width=10).pack(side=LEFT, padx=10)
        ttk.Button(convert_row, text="Конвертировать", command=self.start_conversion).pack(side=LEFT)
        ttk.Button(convert_row, text="Открыть результат", command=self.open_last_output_folder).pack(side=LEFT, padx=10)

        info_box = ttk.Frame(outer, style="Card.TFrame", padding=18)
        info_box.pack(fill=BOTH, expand=True)
        ttk.Label(info_box, text="Что делает приложение", style="Title.TLabel").pack(anchor="w", pady=(0, 10))
        self.info_label = ttk.Label(
            info_box,
            text=self.build_info_text(),
            style="Subtitle.TLabel",
            justify="left",
            wraplength=900,
        )
        self.info_label.pack(anchor="w")

        footer = ttk.Frame(outer)
        footer.pack(fill=BOTH, pady=(12, 0))
        ttk.Label(footer, textvariable=self.ffmpeg_status_var, style="Subtitle.TLabel").pack(anchor="w")
        ttk.Label(footer, textvariable=self.gemini_status_var, style="Subtitle.TLabel").pack(anchor="w", pady=(4, 0))

    def build_info_text(self) -> str:
        return (
            "- Папка New создается автоматически при запуске.\n"
            "- Новые песни вручную кладутся в New, после чего Gemini предлагает названия в стиле библиотеки.\n"
            "- В окне подтверждения можно снять выделение, отредактировать результат и перенести только выбранные файлы.\n"
            "- При конвертации в mp3 сначала все не-MP3 файлы раскладываются по папкам своих форматов, а потом копируются в основную папку как mp3.\n"
            "- Для других форматов создается папка с именем формата, куда попадают конвертированные копии."
        )

    def music_root_is_valid(self) -> bool:
        return self.music_root.exists() and self.music_root.is_dir()

    def library_files(self) -> list[Path]:
        return scan_audio_files(self.music_root, exclude_dirs=[self.new_folder])

    def new_files(self) -> list[Path]:
        return scan_audio_files(self.new_folder)

    def refresh_all(self) -> None:
        self.set_music_root(Path(self.root_path_var.get()).expanduser())
        if not self.music_root_is_valid():
            self.library_count_var.set("0")
            self.library_size_var.set("0 B")
            self.new_count_var.set("0")
            self.new_size_var.set("0 B")
            self.status_var.set("Папка не найдена")
            self.ffmpeg_status_var.set(self.ffmpeg_status_text())
            self.gemini_status_var.set(self.gemini_status_text())
            self.info_label.configure(text=self.build_info_text())
            if self.converter_window and self.converter_window.winfo_exists():
                self.refresh_converter_table()
            return

        ensure_directory(self.new_folder)
        library_files = self.library_files()
        new_files = self.new_files()

        self.library_count_var.set(str(len(library_files)))
        self.library_size_var.set(human_size(sum(path.stat().st_size for path in library_files)))
        self.new_count_var.set(str(len(new_files)))
        self.new_size_var.set(human_size(sum(path.stat().st_size for path in new_files)))
        self.status_var.set(f"Готово: {self.music_root}")
        self.ffmpeg_status_var.set(self.ffmpeg_status_text())
        self.gemini_status_var.set(self.gemini_status_text())
        self.info_label.configure(text=self.build_info_text())

        if self.converter_window and self.converter_window.winfo_exists():
            self.refresh_converter_table()

    def ffmpeg_status_text(self) -> str:
        return f"ffmpeg: {'найден' if find_ffmpeg() else 'не найден'}"

    def gemini_status_text(self) -> str:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip() or "gemini-2.0-flash"
        if genai is None:
            return "Gemini: пакет google-generativeai не установлен"
        if not api_key:
            return f"Gemini: ключ не задан, модель по умолчанию {model_name}"
        return f"Gemini: готов, модель {model_name}"

    def choose_music_root(self) -> None:
        selected = filedialog.askdirectory(initialdir=str(self.music_root))
        if selected:
            self.set_music_root(Path(selected))
            self.refresh_all()

    def open_music_root(self) -> None:
        if not self.music_root.exists():
            messagebox.showerror("Папка не найдена", "Основная папка не существует.")
            return
        os.startfile(str(self.music_root))

    def open_new_folder(self) -> None:
        ensure_directory(self.new_folder)
        os.startfile(str(self.new_folder))

    def open_last_output_folder(self) -> None:
        if self.last_output_dir and self.last_output_dir.exists():
            os.startfile(str(self.last_output_dir))
            return
        messagebox.showinfo("Результат", "Пока нет папки результата. Сначала запусти конвертацию.")

    def open_converter_window(self) -> None:
        if self.converter_window and self.converter_window.winfo_exists():
            self.converter_window.lift()
            self.converter_window.focus_force()
            return

        window = Toplevel(self.root)
        window.title("Конвертер")
        window.geometry("1020x700")
        window.minsize(940, 620)
        window.configure(bg="#10151f")
        window.protocol("WM_DELETE_WINDOW", self.close_converter_window)
        self.converter_window = window

        container = ttk.Frame(window, padding=18)
        container.pack(fill=BOTH, expand=True)

        top = ttk.Frame(container, style="Card.TFrame", padding=18)
        top.pack(fill=BOTH)
        ttk.Label(top, text="Конвертер форматов", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            top,
            text="Таблица показывает количество и общий вес файлов по каждому формату в библиотеке.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(6, 0))

        controls = ttk.Frame(container, padding=(0, 14, 0, 10))
        controls.pack(fill=BOTH)
        ttk.Label(controls, text="Всё в формат:").pack(side=LEFT)
        ttk.Combobox(controls, textvariable=self.output_format_var, values=OUTPUT_FORMATS, state="readonly", width=10).pack(side=LEFT, padx=10)
        ttk.Button(controls, text="Запустить", command=self.start_conversion).pack(side=LEFT)
        ttk.Button(controls, text="Обновить таблицу", command=self.refresh_converter_table).pack(side=LEFT, padx=10)
        ttk.Button(controls, text="Открыть результат", command=self.open_last_output_folder).pack(side=LEFT)

        table_card = ttk.Frame(container, style="Card.TFrame", padding=14)
        table_card.pack(fill=BOTH, expand=True)

        columns = ("format", "count", "size")
        tree = ttk.Treeview(table_card, columns=columns, show="headings", height=13)
        tree.heading("format", text="Формат")
        tree.heading("count", text="Количество")
        tree.heading("size", text="Вес")
        tree.column("format", width=180, anchor="w")
        tree.column("count", width=120, anchor="center")
        tree.column("size", width=160, anchor="e")
        tree.pack(side=LEFT, fill=BOTH, expand=True)

        scrollbar = ttk.Scrollbar(table_card, orient="vertical", command=tree.yview)
        scrollbar.pack(side=RIGHT, fill="y")
        tree.configure(yscrollcommand=scrollbar.set)
        self.converter_tree = tree

        bottom = ttk.Frame(container, padding=(0, 12, 0, 0))
        bottom.pack(fill=BOTH)
        ttk.Label(bottom, textvariable=self.status_var, style="Subtitle.TLabel").pack(anchor="w")
        self.progress_bar = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress_bar.pack(fill=BOTH, pady=(8, 4))
        ttk.Label(bottom, text="Конвертация и организация файлов идут без удаления исходников.", style="Subtitle.TLabel").pack(anchor="w")

        self.refresh_converter_table()

    def close_converter_window(self) -> None:
        if self.converter_window and self.converter_window.winfo_exists():
            self.converter_window.destroy()
        self.converter_window = None
        self.converter_tree = None

    def refresh_converter_table(self) -> None:
        if not self.converter_tree:
            return

        for item in self.converter_tree.get_children():
            self.converter_tree.delete(item)

        if not self.music_root_is_valid():
            self.status_var.set("Папка не найдена")
            return

        library_files = self.library_files()
        for fmt, count, size in summarize_by_format(library_files):
            self.converter_tree.insert("", END, values=(fmt, count, human_size(size)))

        self.status_var.set(f"Найдено файлов: {len(library_files)}")

    def start_conversion(self) -> None:
        if self.active_worker and self.active_worker.is_alive():
            messagebox.showinfo("Конвертация", "Сейчас уже идет обработка. Подожди завершения.")
            return

        if not self.music_root_is_valid():
            messagebox.showerror("Папка не найдена", "Сначала выбери существующую основную папку.")
            return

        target_format = self.output_format_var.get().strip().lower().lstrip(".")
        if not target_format:
            messagebox.showerror("Формат не выбран", "Выбери формат конвертации.")
            return

        ffmpeg_path = find_ffmpeg()
        if not ffmpeg_path:
            messagebox.showerror(
                "ffmpeg не найден",
                "Для конвертации нужен ffmpeg. Установи его и добавь в PATH, затем нажми 'Обновить'.",
            )
            return

        self.current_task = "convert"
        self.status_var.set(f"Старт конвертации в {target_format.upper()}...")
        self.active_worker = threading.Thread(
            target=self._conversion_worker,
            args=(ffmpeg_path, target_format),
            daemon=True,
        )
        self.active_worker.start()
        self.root.after(100, self.poll_worker_queue)

    def _conversion_worker(self, ffmpeg_path: str, target_format: str) -> None:
        try:
            if target_format == "mp3":
                result = self.convert_non_mp3_to_root_mp3(ffmpeg_path)
            else:
                result = self.convert_all_to_target(ffmpeg_path, target_format)
            self.worker_queue.put(("done", result))
        except Exception as exc:  # noqa: BLE001
            self.worker_queue.put(("error", f"Конвертация: {exc}"))

    def convert_all_to_target(self, ffmpeg_path: str, target_format: str) -> dict[str, Any]:
        library_files = self.library_files()
        output_dir = self.music_root / target_format
        ensure_directory(output_dir)
        self.last_output_dir = output_dir

        converted = 0
        errors = 0
        total = len(library_files)

        for index, source in enumerate(library_files, start=1):
            destination = unique_destination_path(output_dir, f"{source.stem}.{target_format}")
            try:
                if source.suffix.lower() == f".{target_format}" and source.parent.resolve() == output_dir.resolve():
                    if source.resolve() != destination.resolve():
                        shutil.copy2(source, destination)
                else:
                    convert_audio_file(ffmpeg_path, source, destination)
                converted += 1
                self.worker_queue.put(("progress", (index, total, f"OK: {source.name} -> {destination.name}")))
            except Exception as exc:  # noqa: BLE001
                errors += 1
                self.worker_queue.put(("progress", (index, total, f"Ошибка: {source.name} ({exc})")))

        return {"converted": converted, "errors": errors, "output_dir": output_dir, "target_format": target_format}

    def organize_non_mp3_files(self, library_files: list[Path]) -> list[tuple[str, str]]:
        moved: list[tuple[str, str]] = []
        for source in library_files:
            if source.suffix.lower() == ".mp3":
                continue
            format_dir = self.music_root / source.suffix.lower().lstrip(".")
            ensure_directory(format_dir)
            destination = unique_destination_path(format_dir, source.name)
            if source.resolve() == destination.resolve():
                continue
            if source.parent.resolve() == format_dir.resolve() and source.name == destination.name:
                continue
            shutil.move(str(source), str(destination))
            moved.append((source.name, str(destination)))
        return moved

    def convert_non_mp3_to_root_mp3(self, ffmpeg_path: str) -> dict[str, Any]:
        library_files = self.library_files()
        organized = self.organize_non_mp3_files(library_files)
        convert_sources = [path for path in self.library_files() if path.suffix.lower() != ".mp3"]
        total = len(convert_sources)
        converted = 0
        errors = 0

        for index, source in enumerate(convert_sources, start=1):
            destination = unique_destination_path(self.music_root, f"{source.stem}.mp3")
            try:
                convert_audio_file(ffmpeg_path, source, destination)
                converted += 1
                self.worker_queue.put(("progress", (index, total, f"OK: {source.name} -> {destination.name}")))
            except Exception as exc:  # noqa: BLE001
                errors += 1
                self.worker_queue.put(("progress", (index, total, f"Ошибка: {source.name} ({exc})")))

        self.last_output_dir = self.music_root
        return {
            "converted": converted,
            "organized": len(organized),
            "errors": errors,
            "output_dir": self.music_root,
            "target_format": "mp3",
        }

    def collect_reference_examples(self, limit: int = 100) -> list[str]:
        examples = [path.stem for path in self.library_files()][::2]
        return examples[:limit]

    def collect_new_files(self) -> list[Path]:
        ensure_directory(self.new_folder)
        return self.new_files()

    def start_gemini_analysis(self) -> None:
        if self.active_worker and self.active_worker.is_alive():
            messagebox.showinfo("Обработка", "Сейчас уже идет другая операция. Подожди завершения.")
            return

        if genai is None:
            messagebox.showerror(
                "Gemini недоступен",
                "Не установлен пакет google-generativeai. Поставь зависимости из requirements.txt.",
            )
            return

        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            messagebox.showerror("Gemini недоступен", "В .env не задан GEMINI_API_KEY.")
            return

        new_files = self.collect_new_files()
        if not new_files:
            messagebox.showinfo("New пустой", "В папке New нет файлов для переименования.")
            return

        examples = self.collect_reference_examples()
        model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip() or "gemini-2.0-flash"

        self.current_task = "gemini"
        self.status_var.set("Отправляю файлы в Gemini...")
        self.active_worker = threading.Thread(
            target=self._gemini_worker,
            args=(api_key, model_name, examples, new_files),
            daemon=True,
        )
        self.active_worker.start()
        self.root.after(120, self.poll_worker_queue)

    def _gemini_worker(self, api_key: str, model_name: str, examples: list[str], new_files: list[Path]) -> None:
        try:
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(model_name)
            payload = {
                "examples": examples,
                "new_files": [
                    {
                        "file_name": path.name,
                        "stem": path.stem,
                        "suffix": path.suffix,
                    }
                    for path in new_files
                ],
            }
            prompt = (
                "Ты переименовываешь аудиофайлы в музыкальной библиотеке.\n"
                "Проанализируй стиль примеров и предложи новые названия для файлов из New.\n"
                "Верни ТОЛЬКО JSON без пояснений и без markdown.\n"
                "Формат ответа: {\"renames\":[{\"file_name\":\"old.ext\",\"new_name\":\"Artist - Title\"}]}\n"
                "Правила:\n"
                "- new_name не должен содержать путь.\n"
                "- Расширение не указывай, оно сохранится из исходного файла.\n"
                "- Сохраняй стиль именования из примеров.\n"
                "- Если стиль примеров допускает сокращения, используй их.\n"
                "- Верни ответ для каждого файла из списка new_files.\n"
                "\nПримеры имён из библиотеки (каждое второе, максимум 100):\n"
                f"{json.dumps(examples, ensure_ascii=False, indent=2)}\n"
                "\nНовые файлы в папке New:\n"
                f"{json.dumps(payload['new_files'], ensure_ascii=False, indent=2)}\n"
            )
            response = model.generate_content(prompt)
            text = getattr(response, "text", "") or ""
            parsed = split_response_json(text)
            renames = self.normalize_gemini_output(parsed, new_files)
            self.worker_queue.put(("gemini_done", renames))
        except Exception as exc:  # noqa: BLE001
            self.worker_queue.put(("error", f"Gemini: {exc}"))

    def normalize_gemini_output(self, parsed: Any, new_files: list[Path]) -> list[RenamePreviewRow]:
        candidates: dict[str, str] = {}
        if isinstance(parsed, dict):
            items = parsed.get("renames", [])
        elif isinstance(parsed, list):
            items = parsed
        else:
            raise ValueError("Gemini returned unsupported structure")

        for item in items:
            if not isinstance(item, dict):
                continue
            source_name = str(item.get("file_name") or item.get("file") or item.get("source") or "").strip()
            new_name = str(item.get("new_name") or item.get("new") or item.get("name") or "").strip()
            if source_name:
                candidates[source_name] = new_name

        rows: list[RenamePreviewRow] = []
        for path in new_files:
            proposed = candidates.get(path.name, path.stem)
            proposed = normalize_target_filename(proposed, path.suffix)
            rows.append(RenamePreviewRow(source=path, proposed_name=proposed, selected=True))
        return rows

    def handle_polling_done(self) -> None:
        self.active_worker = None
        self.current_task = None
        self.progress_bar_reset()

    def poll_worker_queue(self) -> None:
        while True:
            try:
                kind, payload = self.worker_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "progress":
                current, total, message = payload
                total = max(int(total), 1)
                current = int(current)
                if self.progress_bar is not None:
                    self.progress_bar["value"] = int((current / total) * 100)
                self.status_var.set(str(message))
            elif kind == "gemini_done":
                self.handle_polling_done()
                self.handle_gemini_result(payload)
            elif kind == "done":
                self.handle_polling_done()
                self.handle_conversion_result(payload)
            elif kind == "error":
                self.handle_polling_done()
                messagebox.showerror("Ошибка", str(payload))

        if self.active_worker and self.active_worker.is_alive():
            self.root.after(120, self.poll_worker_queue)

    def handle_conversion_result(self, payload: dict[str, Any]) -> None:
        self.refresh_all()
        target_format = payload.get("target_format", "")
        if target_format == "mp3":
            message = (
                f"Готово: mp3 созданы {payload.get('converted', 0)} раз, разложено по папкам {payload.get('organized', 0)} файлов, ошибок {payload.get('errors', 0)}."
            )
        else:
            message = (
                f"Готово: {payload.get('converted', 0)} файлов, ошибок {payload.get('errors', 0)}. Папка: {payload.get('output_dir')}"
            )
        self.status_var.set(message)
        messagebox.showinfo("Конвертация завершена", message)

    def handle_gemini_result(self, rows: list[RenamePreviewRow]) -> None:
        self.preview_rows = rows
        self.open_preview_window()
        self.status_var.set(f"Gemini предложил переименование для {len(rows)} файлов")

    def open_preview_window(self) -> None:
        if self.preview_window and self.preview_window.winfo_exists():
            self.preview_window.lift()
            self.preview_window.focus_force()
            self.render_preview_rows()
            return

        window = Toplevel(self.root)
        window.title("Переименование New")
        window.geometry("1120x720")
        window.minsize(980, 620)
        window.configure(bg="#10151f")
        window.protocol("WM_DELETE_WINDOW", self.close_preview_window)
        self.preview_window = window

        container = ttk.Frame(window, padding=18)
        container.pack(fill=BOTH, expand=True)

        top = ttk.Frame(container, style="Card.TFrame", padding=18)
        top.pack(fill=BOTH)
        ttk.Label(top, text="Предпросмотр переименования", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            top,
            text="По двойному щелчку можно править итоговое имя. Кнопки ниже управляют выбором файлов перед переносом в основную папку.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(6, 0))

        controls = ttk.Frame(container, padding=(0, 14, 0, 10))
        controls.pack(fill=BOTH)
        ttk.Button(controls, text="Выбрать все", command=self.select_all_preview_rows).pack(side=LEFT)
        ttk.Button(controls, text="Снять все", command=self.deselect_all_preview_rows).pack(side=LEFT, padx=10)
        ttk.Button(controls, text="Подтвердить выбранное", command=self.confirm_preview_rows).pack(side=LEFT, padx=10)
        ttk.Button(controls, text="Обновить результат", command=self.render_preview_rows).pack(side=LEFT, padx=10)
        ttk.Label(controls, textvariable=self.preview_count_var, style="Subtitle.TLabel").pack(side=RIGHT)

        table_card = ttk.Frame(container, style="Card.TFrame", padding=14)
        table_card.pack(fill=BOTH, expand=True)

        tree = ttk.Treeview(table_card, columns=("selected", "current", "proposed"), show="headings", height=16)
        tree.heading("selected", text="Выбор")
        tree.heading("current", text="Было")
        tree.heading("proposed", text="Станет")
        tree.column("selected", width=90, anchor="center")
        tree.column("current", width=410, anchor="w")
        tree.column("proposed", width=520, anchor="w")
        tree.pack(side=LEFT, fill=BOTH, expand=True)

        scrollbar = ttk.Scrollbar(table_card, orient="vertical", command=tree.yview)
        scrollbar.pack(side=RIGHT, fill="y")
        tree.configure(yscrollcommand=scrollbar.set)
        tree.bind("<Double-1>", self.on_preview_double_click)
        tree.bind("<Button-1>", self.on_preview_single_click)
        self.preview_tree = tree

        bottom = ttk.Frame(container, padding=(0, 12, 0, 0))
        bottom.pack(fill=BOTH)
        ttk.Label(bottom, textvariable=self.preview_status_var, style="Subtitle.TLabel").pack(anchor="w")

        self.render_preview_rows()

    def close_preview_window(self) -> None:
        if self.preview_window and self.preview_window.winfo_exists():
            self.preview_window.destroy()
        self.preview_window = None
        self.preview_tree = None

    def render_preview_rows(self) -> None:
        if not self.preview_tree:
            return

        for item in self.preview_tree.get_children():
            self.preview_tree.delete(item)

        self.preview_item_ids = []
        selected_count = 0
        for row in self.preview_rows:
            symbol = "[x]" if row.selected else "[ ]"
            if row.selected:
                selected_count += 1
            item_id = self.preview_tree.insert("", END, values=(symbol, row.source.name, row.proposed_name))
            self.preview_item_ids.append(item_id)
            self.preview_tree.item(item_id, tags=("selected",) if row.selected else ("unselected",))

        self.preview_tree.tag_configure("selected", foreground="#e5e7eb")
        self.preview_tree.tag_configure("unselected", foreground="#94a3b8")
        total = len(self.preview_rows)
        self.preview_count_var.set(f"Выбрано {selected_count} из {total}")
        self.preview_status_var.set("Двойной щелчок по строке редактирует итоговое имя")

    def select_all_preview_rows(self) -> None:
        for row in self.preview_rows:
            row.selected = True
        self.render_preview_rows()

    def deselect_all_preview_rows(self) -> None:
        for row in self.preview_rows:
            row.selected = False
        self.render_preview_rows()

    def item_index_from_id(self, item_id: str) -> int:
        return self.preview_item_ids.index(item_id)

    def on_preview_single_click(self, event: Any) -> None:
        if not self.preview_tree:
            return
        region = self.preview_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        item_id = self.preview_tree.identify_row(event.y)
        column = self.preview_tree.identify_column(event.x)
        if not item_id:
            return

        index = self.item_index_from_id(item_id)
        if column == "#1":
            self.preview_rows[index].selected = not self.preview_rows[index].selected
            self.render_preview_rows()

    def on_preview_double_click(self, event: Any) -> None:
        if not self.preview_tree:
            return
        item_id = self.preview_tree.identify_row(event.y)
        if not item_id:
            return
        index = self.item_index_from_id(item_id)
        self.edit_preview_row(index)

    def unique_root_target(self, desired_filename: str) -> Path:
        return unique_destination_path(self.music_root, desired_filename)

    def confirm_preview_rows(self) -> None:
        selected_rows = [row for row in self.preview_rows if row.selected]
        if not selected_rows:
            messagebox.showinfo("Перенос", "Не выбрано ни одного файла.")
            return

        ensure_directory(self.music_root)
        ensure_directory(self.new_folder)
        moved = 0
        errors: list[str] = []

        for row in selected_rows:
            if not row.source.exists():
                errors.append(f"Файл не найден: {row.source.name}")
                continue

            target_filename = normalize_target_filename(row.proposed_name, row.source.suffix)
            destination = self.unique_root_target(target_filename)
            try:
                shutil.move(str(row.source), str(destination))
                moved += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{row.source.name}: {exc}")

        self.preview_rows = [row for row in self.preview_rows if not row.selected]
        self.render_preview_rows()
        self.refresh_all()

        summary = f"Перенесено {moved} файлов в основную папку."
        if errors:
            summary += f" Ошибок: {len(errors)}."
        self.status_var.set(summary)
        if self.preview_rows:
            self.preview_status_var.set(summary)
        else:
            self.close_preview_window()

        if errors:
            messagebox.showwarning("Перенос завершен с ошибками", summary + "\n\n" + "\n".join(errors[:12]))
        else:
            messagebox.showinfo("Перенос завершен", summary)

    def edit_preview_row(self, index: int) -> None:
        row = self.preview_rows[index]
        editor = Toplevel(self.root)
        editor.title("Редактирование имени")
        editor.geometry("620x200")
        editor.resizable(False, False)
        editor.configure(bg="#10151f")
        editor.grab_set()

        container = ttk.Frame(editor, padding=18)
        container.pack(fill=BOTH, expand=True)
        ttk.Label(container, text=f"Было: {row.source.name}", style="Subtitle.TLabel").pack(anchor="w")
        ttk.Label(container, text="Новое имя:", style="Subtitle.TLabel").pack(anchor="w", pady=(12, 4))
        value_var = StringVar(value=row.proposed_name)
        entry = ttk.Entry(container, textvariable=value_var)
        entry.pack(fill=BOTH)
        entry.focus_set()

        button_row = ttk.Frame(container)
        button_row.pack(fill=BOTH, pady=(14, 0))

        def save() -> None:
            row.proposed_name = normalize_target_filename(value_var.get(), row.source.suffix)
            editor.destroy()
            self.render_preview_rows()

        ttk.Button(button_row, text="Сохранить", command=save).pack(side=LEFT)
        ttk.Button(button_row, text="Отмена", command=editor.destroy).pack(side=LEFT, padx=10)
        editor.bind("<Return>", lambda _event: save())

    def organize_non_mp3_files(self, library_files: list[Path]) -> list[tuple[str, str]]:
        moved: list[tuple[str, str]] = []
        for source in library_files:
            if source.suffix.lower() == ".mp3":
                continue
            format_dir = self.music_root / source.suffix.lower().lstrip(".")
            ensure_directory(format_dir)
            destination = unique_destination_path(format_dir, source.name)
            if source.resolve() == destination.resolve():
                continue
            if source.parent.resolve() == format_dir.resolve() and source.name == destination.name:
                continue
            shutil.move(str(source), str(destination))
            moved.append((source.name, str(destination)))
        return moved

    def convert_non_mp3_to_root_mp3(self, ffmpeg_path: str) -> dict[str, Any]:
        library_files = self.library_files()
        organized = self.organize_non_mp3_files(library_files)
        convert_sources = [path for path in self.library_files() if path.suffix.lower() != ".mp3"]
        total = len(convert_sources)
        converted = 0
        errors = 0

        for index, source in enumerate(convert_sources, start=1):
            destination = unique_destination_path(self.music_root, f"{source.stem}.mp3")
            try:
                convert_audio_file(ffmpeg_path, source, destination)
                converted += 1
                self.worker_queue.put(("progress", (index, total, f"OK: {source.name} -> {destination.name}")))
            except Exception as exc:  # noqa: BLE001
                errors += 1
                self.worker_queue.put(("progress", (index, total, f"Ошибка: {source.name} ({exc})")))

        self.last_output_dir = self.music_root
        return {
            "converted": converted,
            "organized": len(organized),
            "errors": errors,
            "output_dir": self.music_root,
            "target_format": "mp3",
        }

    def collect_reference_examples(self, limit: int = 100) -> list[str]:
        examples = [path.stem for path in self.library_files()][::2]
        return examples[:limit]

    def collect_new_files(self) -> list[Path]:
        ensure_directory(self.new_folder)
        return self.new_files()

    def start_gemini_analysis(self) -> None:
        if self.active_worker and self.active_worker.is_alive():
            messagebox.showinfo("Обработка", "Сейчас уже идет другая операция. Подожди завершения.")
            return

        if genai is None:
            messagebox.showerror(
                "Gemini недоступен",
                "Не установлен пакет google-generativeai. Поставь зависимости из requirements.txt.",
            )
            return

        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            messagebox.showerror("Gemini недоступен", "В .env не задан GEMINI_API_KEY.")
            return

        new_files = self.collect_new_files()
        if not new_files:
            messagebox.showinfo("New пустой", "В папке New нет файлов для переименования.")
            return

        examples = self.collect_reference_examples()
        model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip() or "gemini-2.0-flash"

        self.current_task = "gemini"
        self.status_var.set("Отправляю файлы в Gemini...")
        self.active_worker = threading.Thread(
            target=self._gemini_worker,
            args=(api_key, model_name, examples, new_files),
            daemon=True,
        )
        self.active_worker.start()
        self.root.after(120, self.poll_worker_queue)

    def _gemini_worker(self, api_key: str, model_name: str, examples: list[str], new_files: list[Path]) -> None:
        try:
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(model_name)
            payload = {
                "examples": examples,
                "new_files": [
                    {
                        "file_name": path.name,
                        "stem": path.stem,
                        "suffix": path.suffix,
                    }
                    for path in new_files
                ],
            }
            prompt = (
                "Ты переименовываешь аудиофайлы в музыкальной библиотеке.\n"
                "Проанализируй стиль примеров и предложи новые названия для файлов из New.\n"
                "Верни ТОЛЬКО JSON без пояснений и без markdown.\n"
                "Формат ответа: {\"renames\":[{\"file_name\":\"old.ext\",\"new_name\":\"Artist - Title\"}]}\n"
                "Правила:\n"
                "- new_name не должен содержать путь.\n"
                "- Расширение не указывай, оно сохранится из исходного файла.\n"
                "- Сохраняй стиль именования из примеров.\n"
                "- Если стиль примеров допускает сокращения, используй их.\n"
                "- Верни ответ для каждого файла из списка new_files.\n"
                "\nПримеры имён из библиотеки (каждое второе, максимум 100):\n"
                f"{json.dumps(examples, ensure_ascii=False, indent=2)}\n"
                "\nНовые файлы в папке New:\n"
                f"{json.dumps(payload['new_files'], ensure_ascii=False, indent=2)}\n"
            )
            response = model.generate_content(prompt)
            text = getattr(response, "text", "") or ""
            parsed = split_response_json(text)
            renames = self.normalize_gemini_output(parsed, new_files)
            self.worker_queue.put(("gemini_done", renames))
        except Exception as exc:  # noqa: BLE001
            self.worker_queue.put(("error", f"Gemini: {exc}"))

    def normalize_gemini_output(self, parsed: Any, new_files: list[Path]) -> list[RenamePreviewRow]:
        candidates: dict[str, str] = {}
        if isinstance(parsed, dict):
            items = parsed.get("renames", [])
        elif isinstance(parsed, list):
            items = parsed
        else:
            raise ValueError("Gemini returned unsupported structure")

        for item in items:
            if not isinstance(item, dict):
                continue
            source_name = str(item.get("file_name") or item.get("file") or item.get("source") or "").strip()
            new_name = str(item.get("new_name") or item.get("new") or item.get("name") or "").strip()
            if source_name:
                candidates[source_name] = new_name

        rows: list[RenamePreviewRow] = []
        for path in new_files:
            proposed = candidates.get(path.name, path.stem)
            proposed = normalize_target_filename(proposed, path.suffix)
            rows.append(RenamePreviewRow(source=path, proposed_name=proposed, selected=True))
        return rows

    def handle_polling_done(self) -> None:
        self.active_worker = None
        self.current_task = None
        self.progress_bar_reset()

    def poll_worker_queue(self) -> None:
        while True:
            try:
                kind, payload = self.worker_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "progress":
                current, total, message = payload
                total = max(int(total), 1)
                current = int(current)
                if self.progress_bar is not None:
                    self.progress_bar["value"] = int((current / total) * 100)
                self.status_var.set(str(message))
            elif kind == "gemini_done":
                self.handle_polling_done()
                self.handle_gemini_result(payload)
            elif kind == "done":
                self.handle_polling_done()
                self.handle_conversion_result(payload)
            elif kind == "error":
                self.handle_polling_done()
                messagebox.showerror("Ошибка", str(payload))

        if self.active_worker and self.active_worker.is_alive():
            self.root.after(120, self.poll_worker_queue)

    def handle_conversion_result(self, payload: dict[str, Any]) -> None:
        self.refresh_all()
        target_format = payload.get("target_format", "")
        if target_format == "mp3":
            message = (
                f"Готово: mp3 созданы {payload.get('converted', 0)} раз, разложено по папкам {payload.get('organized', 0)} файлов, ошибок {payload.get('errors', 0)}."
            )
        else:
            message = (
                f"Готово: {payload.get('converted', 0)} файлов, ошибок {payload.get('errors', 0)}. Папка: {payload.get('output_dir')}"
            )
        self.status_var.set(message)
        messagebox.showinfo("Конвертация завершена", message)

    def handle_gemini_result(self, rows: list[RenamePreviewRow]) -> None:
        self.preview_rows = rows
        self.open_preview_window()
        self.status_var.set(f"Gemini предложил переименование для {len(rows)} файлов")

    def unique_root_target(self, desired_filename: str) -> Path:
        return unique_destination_path(self.music_root, desired_filename)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    app = MusicToolsApp()
    app.run()


if __name__ == "__main__":
    main()