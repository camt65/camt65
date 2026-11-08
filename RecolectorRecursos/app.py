import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from motor import process_manifest


class RecolectorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Recolector de recursos")
        self.root.geometry("860x620")
        self.root.minsize(720, 520)

        self.json_var = tk.StringVar()
        self.sources_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.pexels_var = tk.StringVar()

        self._build_ui()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=14)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(7, weight=1)

        ttk.Label(
            main,
            text="Recolector y organizador de recursos",
            font=("Segoe UI", 16, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Label(
            main,
            text=(
                "Los vídeos de episodios se recortan desde archivos locales. "
                "Las imágenes descargadas se limitan a licencias reconocidas; "
                "las peticiones sin fuente generan enlaces para revisión."
            ),
            wraplength=790,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 14))

        self._path_row(
            main,
            row=2,
            label="JSON de peticiones:",
            variable=self.json_var,
            button_text="Elegir JSON",
            command=self._choose_json,
        )

        self._path_row(
            main,
            row=3,
            label="Carpeta de fuentes locales:",
            variable=self.sources_var,
            button_text="Elegir carpeta",
            command=self._choose_sources,
        )

        self._path_row(
            main,
            row=4,
            label="Carpeta de salida:",
            variable=self.output_var,
            button_text="Elegir carpeta",
            command=self._choose_output,
        )

        ttk.Label(
            main,
            text="Clave API de Pexels (opcional):",
        ).grid(row=5, column=0, sticky="w", pady=4)

        pexels_entry = ttk.Entry(
            main,
            textvariable=self.pexels_var,
            show="*",
        )
        pexels_entry.grid(row=5, column=1, sticky="ew", padx=8, pady=4)

        ttk.Label(
            main,
            text="No se guarda en un archivo.",
            foreground="#666666",
        ).grid(row=5, column=2, sticky="w", pady=4)

        button_row = ttk.Frame(main)
        button_row.grid(
            row=6,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(10, 8),
        )

        self.run_button = ttk.Button(
            button_row,
            text="Procesar peticiones",
            command=self._run,
        )
        self.run_button.pack(side="left")

        ttk.Button(
            button_row,
            text="Abrir carpeta de salida",
            command=self._open_output,
        ).pack(side="left", padx=8)

        ttk.Label(
            main,
            text="Registro del proceso:",
        ).grid(row=7, column=0, columnspan=3, sticky="nw")

        self.log_box = ScrolledText(
            main,
            height=16,
            wrap="word",
            font=("Consolas", 9),
        )
        self.log_box.grid(
            row=8,
            column=0,
            columnspan=3,
            sticky="nsew",
            pady=(5, 0),
        )

        main.rowconfigure(8, weight=1)

    def _path_row(
        self,
        parent,
        row,
        label,
        variable,
        button_text,
        command,
    ):
        ttk.Label(parent, text=label).grid(
            row=row,
            column=0,
            sticky="w",
            pady=4,
        )

        ttk.Entry(
            parent,
            textvariable=variable,
        ).grid(
            row=row,
            column=1,
            sticky="ew",
            padx=8,
            pady=4,
        )

        ttk.Button(
            parent,
            text=button_text,
            command=command,
        ).grid(
            row=row,
            column=2,
            sticky="ew",
            pady=4,
        )

    def _choose_json(self):
        selected = filedialog.askopenfilename(
            title="Selecciona el JSON de peticiones",
            filetypes=[("Archivos JSON", "*.json"), ("Todos los archivos", "*.*")],
        )

        if not selected:
            return

        self.json_var.set(selected)
        parent = Path(selected).parent

        if not self.sources_var.get():
            self.sources_var.set(str(parent / "fuentes_locales"))

        if not self.output_var.get():
            self.output_var.set(str(parent / "salida"))

    def _choose_sources(self):
        selected = filedialog.askdirectory(
            title="Selecciona la carpeta de vídeos y subtítulos",
        )
        if selected:
            self.sources_var.set(selected)

    def _choose_output(self):
        selected = filedialog.askdirectory(
            title="Selecciona la carpeta donde guardar el resultado",
        )
        if selected:
            self.output_var.set(selected)

    def _append_log(self, message):
        self.log_box.insert("end", str(message) + "\n")
        self.log_box.see("end")

    def _run(self):
        json_path = self.json_var.get().strip()

        if not json_path or not Path(json_path).is_file():
            messagebox.showerror(
                "Falta el JSON",
                "Selecciona un archivo JSON de peticiones válido.",
            )
            return

        output_path = self.output_var.get().strip()
        if not output_path:
            output_path = str(Path(json_path).parent / "salida")
            self.output_var.set(output_path)

        source_path = self.sources_var.get().strip() or None
        pexels_key = self.pexels_var.get().strip()

        self.log_box.delete("1.0", "end")
        self.run_button.config(state="disabled")

        def worker():
            try:
                project_dir = process_manifest(
                    json_path=json_path,
                    source_dir=source_path,
                    output_dir=output_path,
                    pexels_key=pexels_key,
                    log_callback=lambda text: self.root.after(
                        0, self._append_log, text
                    ),
                )
                self.root.after(
                    0,
                    self._finished,
                    True,
                    str(project_dir),
                )
            except Exception as error:
                self.root.after(
                    0,
                    self._finished,
                    False,
                    str(error),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _finished(self, success, message):
        self.run_button.config(state="normal")

        if success:
            self._append_log("")
            self._append_log(f"Carpeta creada: {message}")
            messagebox.showinfo(
                "Proceso terminado",
                f"Recursos y manifiestos guardados en:\n{message}",
            )
        else:
            self._append_log(f"ERROR: {message}")
            messagebox.showerror("Error", message)

    def _open_output(self):
        path = self.output_var.get().strip()
        if not path:
            messagebox.showinfo(
                "Carpeta de salida",
                "Selecciona primero una carpeta de salida.",
            )
            return

        folder = Path(path)
        if not folder.exists():
            messagebox.showinfo(
                "Carpeta de salida",
                "La carpeta todavía no existe. Se creará al procesar el JSON.",
            )
            return

        try:
            os.startfile(str(folder))
        except Exception as error:
            messagebox.showerror(
                "No se pudo abrir la carpeta",
                str(error),
            )


if __name__ == "__main__":
    root = tk.Tk()
    app = RecolectorApp(root)
    root.mainloop()
