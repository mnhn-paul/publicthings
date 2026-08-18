import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import requests
import pandas as pd
import mimetypes
import logging
import math
import threading
from uuid import uuid4
from pathlib import Path
from typing import Dict, Union
from io import BytesIO

# =====================================================
# Configuration
# =====================================================

API_ENDPOINT = "https://data.public.lu/api/1/"

SUPPORTED_TYPES = {
    "main": "Fichier principal",
    "documentation": "Documentation",
    "update": "Mise à jour",
    "api": "API",
    "code": "Dépôt de code",
    "other": "Autre"
}

SUPPORTED_FORMATS = {
    "CSV": "text/csv",
    "JSON": "application/json",
    "GEOJSON": "application/geo+json",
    "JPG": "image/jpeg",
    "PNG": "image/png",
    "HTML": "text/html",
    "ZIP": "application/zip",
    "OTHER": "other",
    "URL": "url"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

# =====================================================
# Helpers
# =====================================================

def clean(value):
    if pd.isna(value):
        return ""
    return str(value).strip()

# =====================================================
# UData Uploader Class
# =====================================================

class UdataUploader:
    def __init__(self, api_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "accept": "application/json",
            "X-API-KEY": api_token
        })
        self.report_data = []

    def convert_type(self, value: Union[str, int]) -> str:
        value = clean(value).lower()

        if value in SUPPORTED_TYPES:
            return value

        numeric_mapping = {
            "0": "main",
            "1": "documentation",
            "2": "update",
            "3": "api",
            "4": "code",
            "5": "other"
        }

        if value in numeric_mapping:
            return numeric_mapping[value]

        french_mapping = {
            "fichier principal": "main",
            "documentation": "documentation",
            "mise à jour": "update",
            "mise a jour": "update",
            "api": "api",
            "dépôt de code": "code",
            "depot de code": "code",
            "autre": "other"
        }

        if value in french_mapping:
            return french_mapping[value]

        raise ValueError(f"Invalid type '{value}'")

    def validate_row(self, row: Dict) -> bool:
        required = ["dataset_id", "operation_type"]

        for field in required:
            if field not in row:
                return False

        operation = clean(row["operation_type"])

        if operation not in ["file_upload", "add_link"]:
            return False

        if operation == "file_upload":
            required_file = ["file_path", "type", "format"]

            for field in required_file:
                if not clean(row.get(field)):
                    return False

            if clean(row["format"]).upper() not in SUPPORTED_FORMATS:
                return False

        if operation == "add_link":
            if not clean(row.get("url")):
                return False

        return True

    def upload_file(self, row: Dict) -> Dict:
        dataset_id = clean(row["dataset_id"])
        file_path = Path(clean(row["file_path"]))

        if not file_path.exists():
            return {
                "status": "error",
                "error": f"File not found: {file_path}"
            }

        mime_type = SUPPORTED_FORMATS.get(clean(row["format"]).upper(), "application/octet-stream")

        upload_url = f"{API_ENDPOINT}datasets/{dataset_id}/upload/"
        chunk_size = 10 * 1024 * 1024  # 10MB
        file_size = os.path.getsize(file_path)
        parts = math.ceil(file_size / chunk_size)
        uuid = str(uuid4())

        logging.info("Uploading file %s in %d parts", file_path.name, parts)

        try:
            with file_path.open("rb") as input_file:
                chunk_index = 0
                chunk_offset = 0

                while True:
                    chunk_data = input_file.read(chunk_size)
                    if not chunk_data:
                        break

                    logging.info("Uploading chunk %d of %d", chunk_index + 1, parts)
                    data = {
                        "partindex": str(chunk_index),
                        "partbyteoffset": str(chunk_offset),
                        "chunksize": str(len(chunk_data)),
                        "totalparts": str(parts),
                        "size": str(file_size),
                        "filename": file_path.name,
                        "uuid": uuid,
                    }

                    files = {
                        "file": ("blob", BytesIO(chunk_data)),
                    }

                    response = self.session.post(
                        upload_url,
                        data=data,
                        files=files,
                        timeout=(30, 1800)
                    )

                    if not response.ok:
                        return {
                            "status": "error",
                            "code": response.status_code,
                            "error": response.text
                        }

                    chunk_index += 1
                    chunk_offset += len(chunk_data)

            finalize_response = self.session.post(
                upload_url,
                data={
                    "uuid": uuid,
                    "filename": file_path.name,
                    "size": str(file_size),
                    "totalparts": str(parts),
                },
                timeout=60
            )

            if not finalize_response.ok:
                return {
                    "status": "error",
                    "code": finalize_response.status_code,
                    "error": finalize_response.text
                }

            resource = finalize_response.json()
            resource_id = resource.get("id")

            if resource_id:
                update_url = f"{API_ENDPOINT}datasets/{dataset_id}/resources/{resource_id}/"
                metadata = {
                    "title": clean(row.get("title")),
                    "description": clean(row.get("description")),
                    "type": self.convert_type(row["type"]),
                    "format": clean(row["format"]).upper()
                }

                update = self.session.put(update_url, json=metadata, timeout=60)
                if not update.ok:
                    return {
                        "status": "error",
                        "resource_id": resource_id,
                        "error": f"Metadata update failed: {update.text}"
                    }

            return {
                "status": "success",
                "resource_id": resource_id
            }

        except Exception as e:
            logging.exception("File upload failed")
            return {
                "status": "error",
                "error": str(e)
            }

    def add_link(self, row: Dict) -> Dict:
        dataset_id = clean(row["dataset_id"])
        url_value = clean(row["url"])

        if not url_value.startswith(("http://", "https://")):
            return {
                "status": "error",
                "error": f"Invalid URL: {url_value}"
            }

        resource_url = f"{API_ENDPOINT}datasets/{dataset_id}/resources/"
        payload = {
            "title": clean(row.get("title")),
            "description": clean(row.get("description")),
            "url": url_value,
            "type": "other",
            "filetype": "remote"
        }

        try:
            logging.info("Creating URL resource '%s'", payload["title"])
            response = self.session.post(resource_url, json=payload, timeout=60)

            if not response.ok:
                return {
                    "status": "error",
                    "code": response.status_code,
                    "error": response.text
                }

            resource = response.json()
            return {
                "status": "success",
                "resource_id": resource.get("id")
            }

        except Exception as e:
            logging.exception("URL resource creation failed")
            return {
                "status": "error",
                "error": str(e)
            }

    def process_csv(self, csv_path: str):
        df = pd.read_csv(csv_path, encoding="utf-8")
        logging.info("Processing %d rows", len(df))

        for _, record in df.iterrows():
            row = record.to_dict()

            if not self.validate_row(row):
                self.report_data.append({
                    "dataset_id": clean(row.get("dataset_id")),
                    "operation": clean(row.get("operation_type")),
                    "status": "error",
                    "error": "Validation failed"
                })
                continue

            operation = clean(row["operation_type"])

            if operation == "file_upload":
                result = self.upload_file(row)
            else:
                result = self.add_link(row)

            self.report_data.append({
                "dataset_id": clean(row["dataset_id"]),
                "operation": operation,
                "status": result.get("status"),
                "resource_id": result.get("resource_id", ""),
                "error": result.get("error", "")
            })

    def generate_report(self, output_path: str):
        pd.DataFrame(self.report_data).to_csv(output_path, index=False)

# =====================================================
# GUI Class
# =====================================================

class UDataUploaderGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("UData Uploader")

        # Variables
        self.api_key = tk.StringVar()
        self.csv_file = tk.StringVar()
        self.data_folder = tk.StringVar()

        # GUI Layout
        self.create_widgets()

    def create_widgets(self):
        # API Key
        ttk.Label(self.root, text="API Key:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        ttk.Entry(self.root, textvariable=self.api_key, width=50).grid(row=0, column=1, padx=5, pady=5)

        # CSV File
        ttk.Label(self.root, text="CSV File:").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        ttk.Entry(self.root, textvariable=self.csv_file, width=50).grid(row=1, column=1, padx=5, pady=5)
        ttk.Button(self.root, text="...", command=lambda: self.browse_file(self.csv_file)).grid(row=1, column=2, padx=5, pady=5)

        # Data Folder
        ttk.Label(self.root, text="Data Folder:").grid(row=2, column=0, padx=5, pady=5, sticky="w")
        ttk.Entry(self.root, textvariable=self.data_folder, width=50).grid(row=2, column=1, padx=5, pady=5)
        ttk.Button(self.root, text="...", command=lambda: self.browse_folder(self.data_folder)).grid(row=2, column=2, padx=5, pady=5)

        # Validate Button
        ttk.Button(self.root, text="Validate Inputs", command=self.validate_inputs).grid(row=3, column=1, padx=5, pady=10)

        # Overview Text
        self.overview_text = scrolledtext.ScrolledText(self.root, width=80, height=10)
        self.overview_text.grid(row=4, column=0, columnspan=3, padx=5, pady=5)

        # Progress Bar
        self.progress = ttk.Progressbar(self.root, orient="horizontal", length=400, mode="determinate")
        self.progress.grid(row=5, column=0, columnspan=3, padx=5, pady=5)

        # Log Output
        self.log_output = scrolledtext.ScrolledText(self.root, width=80, height=10)
        self.log_output.grid(row=6, column=0, columnspan=3, padx=5, pady=5)

        # Upload Button
        ttk.Button(self.root, text="Upload", command=self.start_upload).grid(row=7, column=1, padx=5, pady=10)

    def browse_file(self, variable):
        filename = filedialog.askopenfilename(title="Select CSV File", filetypes=[("CSV files", "*.csv")])
        if filename:
            variable.set(filename)

    def browse_folder(self, variable):
        foldername = filedialog.askdirectory(title="Select Data Folder")
        if foldername:
            variable.set(foldername)

    def validate_inputs(self):
        api_key = self.api_key.get()
        csv_file = self.csv_file.get()
        data_folder = self.data_folder.get()

        if not api_key or not csv_file or not data_folder:
            messagebox.showerror("Error", "All fields are required!")
            return False

        if not os.path.exists(csv_file):
            messagebox.showerror("Error", "CSV file does not exist!")
            return False

        if not os.path.exists(data_folder):
            messagebox.showerror("Error", "Data folder does not exist!")
            return False

        try:
            df = pd.read_csv(csv_file, keep_default_na=False)  # Treat empty cells as empty strings
            overview = []
            for _, row in df.iterrows():
                operation = clean(row["operation_type"])
                dataset_id = clean(row["dataset_id"])
                file_path = row.get("file_path", "")
                url = row.get("url", "")

                if operation == "file_upload":
                    if not file_path:
                        messagebox.showerror("Error", "File path is required for file_upload operation!")
                        return False
                    full_path = os.path.join(data_folder, os.path.basename(file_path))
                    if not os.path.exists(full_path):
                        messagebox.showerror("Error", f"File {os.path.basename(file_path)} not found in data folder!")
                        return False
                    overview.append(f"Dataset: {dataset_id} | File: {os.path.basename(file_path)} | Title: {row['title']}")

                elif operation == "add_link":
                    if not url or not url.startswith(("http://", "https://")):
                        messagebox.showerror("Error", f"Invalid URL: {url}")
                        return False
                    overview.append(f"Dataset: {dataset_id} | URL: {url} | Title: {row['title']}")

            self.overview_text.delete(1.0, tk.END)
            self.overview_text.insert(tk.END, "Upload Overview:\n\n")
            for item in overview:
                self.overview_text.insert(tk.END, f"{item}\n")

            messagebox.showinfo("Success", "Inputs are valid! Review the upload overview.")
            return True
        except Exception as e:
            messagebox.showerror("Error", f"Failed to parse CSV file: {e}")
            return False


    def start_upload(self):
        if not self.validate_inputs():
            return

        api_key = self.api_key.get()
        csv_file = self.csv_file.get()
        data_folder = self.data_folder.get()

        self.progress["value"] = 0
        self.log_output.delete(1.0, tk.END)

        threading.Thread(target=self.run_upload, args=(api_key, csv_file, data_folder), daemon=True).start()

    def run_upload(self, api_key, csv_file, data_folder):
        uploader = UdataUploader(api_key)
        uploader.process_csv(csv_file)
        uploader.generate_report(os.path.join(data_folder, "output_report.csv"))

        self.progress["value"] = 100
        self.log_output.insert(tk.END, "Upload completed successfully!\n")
        messagebox.showinfo("Success", "Upload completed successfully!")

# =====================================================
# Main
# =====================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = UDataUploaderGUI(root)
    root.mainloop()
