import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import requests
import pandas as pd
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


# Required columns in the CSV/Excel input file
REQUIRED_COLUMNS = [
    "dataset_id",
    "operation_type"
]


# Optional columns that may be present
OPTIONAL_COLUMNS = [
    "file_path",
    "url",
    "type",
    "format",
    "title",
    "description"
]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)


# =====================================================
# Helpers
# =====================================================

def clean(value):
    """
    Convert pandas/Excel values into clean strings.
    Empty cells become empty strings.
    """

    if pd.isna(value):
        return ""

    return str(value).strip()


def read_input_file(file_path: str) -> pd.DataFrame:
    """
    Read CSV or Excel input file.

    Supported:
        .csv
        .xlsx
        .xls
    """

    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"Input file not found: {file_path}"
        )

    extension = file_path.suffix.lower()

    # ---------------------------------------------
    # CSV
    # ---------------------------------------------

    if extension == ".csv":

        df = pd.read_csv(
            file_path,
            encoding="utf-8",
            keep_default_na=False
        )

    # ---------------------------------------------
    # XLSX
    # ---------------------------------------------

    elif extension == ".xlsx":

        df = pd.read_excel(
            file_path,
            engine="openpyxl",
            keep_default_na=False
        )

    # ---------------------------------------------
    # XLS
    # ---------------------------------------------

    elif extension == ".xls":

        df = pd.read_excel(
            file_path,
            engine="xlrd",
            keep_default_na=False
        )

    else:

        raise ValueError(
            "Unsupported input file type.\n\n"
            "Please select a CSV, XLSX, or XLS file."
        )

    # Remove accidental spaces from column names
    df.columns = [
        str(column).strip()
        for column in df.columns
    ]

    return df


def validate_columns(df: pd.DataFrame):
    """
    Verify that the input file contains
    the required columns.
    """

    missing_columns = [
        column
        for column in REQUIRED_COLUMNS
        if column not in df.columns
    ]

    if missing_columns:

        expected = (
            REQUIRED_COLUMNS +
            OPTIONAL_COLUMNS
        )

        raise ValueError(
            "The following required columns "
            "are missing:\n\n"
            +
            "\n".join(
                f"- {column}"
                for column in missing_columns
            )
            +
            "\n\nExpected columns are:\n\n"
            +
            "\n".join(
                f"- {column}"
                for column in expected
            )
        )


def resolve_file_path(
    file_name: str,
    data_folder: str
) -> str:
    """
    Combine the filename from the Excel/CSV file
    with the Data Folder selected in the GUI.

    IMPORTANT:
    Only the filename is used.

    Example:

        Excel:
            data.csv

        Data Folder:
            C:\\MyData

        Result:
            C:\\MyData\\data.csv
    """

    file_name = clean(file_name)

    if not file_name:
        return ""

    data_folder = os.path.abspath(
        data_folder
    )

    # Only use the filename itself.
    # This means even if the Excel contains:
    #
    # C:\SomeOtherFolder\data.csv
    #
    # or:
    #
    # ../../data.csv
    #
    # only "data.csv" will be used.

    safe_filename = os.path.basename(
        file_name
    )

    return os.path.join(
        data_folder,
        safe_filename
    )


# =====================================================
# UData Uploader Class
# =====================================================

class UdataUploader:

    def __init__(
        self,
        api_token: str
    ):

        self.session = requests.Session()

        self.session.headers.update({
            "accept": "application/json",
            "X-API-KEY": api_token
        })

        self.report_data = []


    # =================================================
    # Convert resource type
    # =================================================

    def convert_type(
        self,
        value: Union[str, int]
    ) -> str:

        value = clean(value).lower()

        # Already a supported type
        if value in SUPPORTED_TYPES:
            return value

        # Numeric mapping
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

        # French mapping
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

        raise ValueError(
            f"Invalid type '{value}'"
        )


    # =================================================
    # Validate row
    # =================================================

    def validate_row(
        self,
        row: Dict
    ) -> bool:

        required = [
            "dataset_id",
            "operation_type"
        ]

        # ---------------------------------------------
        # Required fields
        # ---------------------------------------------

        for field in required:

            if field not in row:
                return False

        operation = clean(
            row["operation_type"]
        )

        # ---------------------------------------------
        # Operation type
        # ---------------------------------------------

        if operation not in [
            "file_upload",
            "add_link"
        ]:

            return False

        # ---------------------------------------------
        # File upload
        # ---------------------------------------------

        if operation == "file_upload":

            required_file = [
                "file_path",
                "type",
                "format"
            ]

            for field in required_file:

                if not clean(
                    row.get(field)
                ):

                    return False

            if clean(
                row["format"]
            ).upper() not in SUPPORTED_FORMATS:

                return False

        # ---------------------------------------------
        # Add link
        # ---------------------------------------------

        if operation == "add_link":

            if not clean(
                row.get("url")
            ):

                return False

        return True


    # =================================================
    # Upload file
    # =================================================

    def upload_file(
        self,
        row: Dict
    ) -> Dict:

        dataset_id = clean(
            row["dataset_id"]
        )

        file_path = Path(
            clean(row["file_path"])
        )

        # ---------------------------------------------
        # Check file exists
        # ---------------------------------------------

        if not file_path.exists():

            return {
                "status": "error",
                "error": (
                    f"File not found: "
                    f"{file_path}"
                )
            }

        # ---------------------------------------------
        # Determine MIME type
        # ---------------------------------------------

        mime_type = SUPPORTED_FORMATS.get(
            clean(
                row["format"]
            ).upper(),
            "application/octet-stream"
        )

        # ---------------------------------------------
        # Upload URL
        # ---------------------------------------------

        upload_url = (
            f"{API_ENDPOINT}"
            f"datasets/{dataset_id}/upload/"
        )

        # ---------------------------------------------
        # Chunk settings
        # ---------------------------------------------

        chunk_size = 10 * 1024 * 1024  # 10 MB

        file_size = os.path.getsize(
            file_path
        )

        parts = math.ceil(
            file_size / chunk_size
        )

        upload_uuid = str(
            uuid4()
        )

        logging.info(
            "Uploading file %s in %d parts",
            file_path.name,
            parts
        )

        try:

            # -----------------------------------------
            # Upload chunks
            # -----------------------------------------

            with file_path.open(
                "rb"
            ) as input_file:

                chunk_index = 0
                chunk_offset = 0

                while True:

                    chunk_data = (
                        input_file.read(
                            chunk_size
                        )
                    )

                    if not chunk_data:
                        break

                    logging.info(
                        "Uploading chunk %d of %d",
                        chunk_index + 1,
                        parts
                    )

                    data = {
                        "partindex": str(
                            chunk_index
                        ),
                        "partbyteoffset": str(
                            chunk_offset
                        ),
                        "chunksize": str(
                            len(chunk_data)
                        ),
                        "totalparts": str(
                            parts
                        ),
                        "size": str(
                            file_size
                        ),
                        "filename": (
                            file_path.name
                        ),
                        "uuid": upload_uuid,
                    }

                    files = {
                        "file": (
                            "blob",
                            BytesIO(chunk_data),
                            mime_type
                        )
                    }

                    response = (
                        self.session.post(
                            upload_url,
                            data=data,
                            files=files,
                            timeout=(30, 1800)
                        )
                    )

                    if not response.ok:

                        return {
                            "status": "error",
                            "code": (
                                response.status_code
                            ),
                            "error": (
                                response.text
                            )
                        }

                    chunk_index += 1

                    chunk_offset += len(
                        chunk_data
                    )

            # -----------------------------------------
            # Finalize upload
            # -----------------------------------------

            finalize_response = (
                self.session.post(
                    upload_url,
                    data={
                        "uuid": upload_uuid,
                        "filename": (
                            file_path.name
                        ),
                        "size": str(
                            file_size
                        ),
                        "totalparts": str(
                            parts
                        ),
                    },
                    timeout=60
                )
            )

            if not finalize_response.ok:

                return {
                    "status": "error",
                    "code": (
                        finalize_response.status_code
                    ),
                    "error": (
                        finalize_response.text
                    )
                }

            resource = (
                finalize_response.json()
            )

            resource_id = resource.get(
                "id"
            )

            # -----------------------------------------
            # Update resource metadata
            # -----------------------------------------

            if resource_id:

                update_url = (
                    f"{API_ENDPOINT}"
                    f"datasets/{dataset_id}"
                    f"/resources/{resource_id}/"
                )

                metadata = {
                    "title": clean(
                        row.get("title")
                    ),
                    "description": clean(
                        row.get(
                            "description"
                        )
                    ),
                    "type": (
                        self.convert_type(
                            row["type"]
                        )
                    ),
                    "format": clean(
                        row["format"]
                    ).upper()
                }

                update = (
                    self.session.put(
                        update_url,
                        json=metadata,
                        timeout=60
                    )
                )

                if not update.ok:

                    return {
                        "status": "error",
                        "resource_id": (
                            resource_id
                        ),
                        "error": (
                            "Metadata update "
                            "failed: "
                            f"{update.text}"
                        )
                    }

            return {
                "status": "success",
                "resource_id": resource_id
            }

        except Exception as e:

            logging.exception(
                "File upload failed"
            )

            return {
                "status": "error",
                "error": str(e)
            }


    # =================================================
    # Add URL link
    # =================================================

    def add_link(
        self,
        row: Dict
    ) -> Dict:

        dataset_id = clean(
            row["dataset_id"]
        )

        url_value = clean(
            row["url"]
        )

        # ---------------------------------------------
        # Validate URL
        # ---------------------------------------------

        if not url_value.startswith(
            ("http://", "https://")
        ):

            return {
                "status": "error",
                "error": (
                    f"Invalid URL: "
                    f"{url_value}"
                )
            }

        # ---------------------------------------------
        # Resource endpoint
        # ---------------------------------------------

        resource_url = (
            f"{API_ENDPOINT}"
            f"datasets/{dataset_id}"
            f"/resources/"
        )

        payload = {
            "title": clean(
                row.get("title")
            ),
            "description": clean(
                row.get("description")
            ),
            "url": url_value,
            "type": "other",
            "filetype": "remote"
        }

        try:

            logging.info(
                "Creating URL resource '%s'",
                payload["title"]
            )

            response = (
                self.session.post(
                    resource_url,
                    json=payload,
                    timeout=60
                )
            )

            if not response.ok:

                return {
                    "status": "error",
                    "code": (
                        response.status_code
                    ),
                    "error": (
                        response.text
                    )
                }

            resource = (
                response.json()
            )

            return {
                "status": "success",
                "resource_id": (
                    resource.get("id")
                )
            }

        except Exception as e:

            logging.exception(
                "URL resource creation failed"
            )

            return {
                "status": "error",
                "error": str(e)
            }


    # =================================================
    # Process CSV / Excel input
    # =================================================

    def process_input_file(
        self,
        input_path: str,
        data_folder: str
    ):

        logging.info(
            "Reading input file: %s",
            input_path
        )

        # ---------------------------------------------
        # Read CSV / Excel
        # ---------------------------------------------

        df = read_input_file(
            input_path
        )

        # ---------------------------------------------
        # Validate columns
        # ---------------------------------------------

        validate_columns(
            df
        )

        logging.info(
            "Processing %d rows",
            len(df)
        )

        # ---------------------------------------------
        # Process rows
        # ---------------------------------------------

        for _, record in df.iterrows():

            row = record.to_dict()

            operation = clean(
                row.get(
                    "operation_type"
                )
            )

            # -----------------------------------------
            # Resolve filename against Data Folder
            # -----------------------------------------

            if operation == "file_upload":

                filename = clean(
                    row.get(
                        "file_path"
                    )
                )

                row["file_path"] = (
                    resolve_file_path(
                        filename,
                        data_folder
                    )
                )

            # -----------------------------------------
            # Validate row
            # -----------------------------------------

            if not self.validate_row(
                row
            ):

                self.report_data.append({
                    "dataset_id": clean(
                        row.get(
                            "dataset_id"
                        )
                    ),
                    "operation": operation,
                    "status": "error",
                    "resource_id": "",
                    "error": (
                        "Validation failed"
                    )
                })

                continue

            # -----------------------------------------
            # Perform operation
            # -----------------------------------------

            if operation == "file_upload":

                result = (
                    self.upload_file(
                        row
                    )
                )

            else:

                result = (
                    self.add_link(
                        row
                    )
                )

            # -----------------------------------------
            # Save result
            # -----------------------------------------

            self.report_data.append({
                "dataset_id": clean(
                    row["dataset_id"]
                ),
                "operation": operation,
                "status": result.get(
                    "status"
                ),
                "resource_id": result.get(
                    "resource_id",
                    ""
                ),
                "error": result.get(
                    "error",
                    ""
                )
            })


    # =================================================
    # Generate report
    # =================================================

    def generate_report(
        self,
        output_path: str
    ):

        pd.DataFrame(
            self.report_data
        ).to_csv(
            output_path,
            index=False
        )


# =====================================================
# GUI Class
# =====================================================

class UDataUploaderGUI:

    def __init__(
        self,
        root
    ):

        self.root = root

        self.root.title(
            "UData Uploader"
        )

        # ---------------------------------------------
        # Variables
        # ---------------------------------------------

        self.api_key = (
            tk.StringVar()
        )

        self.input_file = (
            tk.StringVar()
        )

        self.data_folder = (
            tk.StringVar()
        )

        # ---------------------------------------------
        # Create GUI
        # ---------------------------------------------

        self.create_widgets()


    # =================================================
    # Create GUI widgets
    # =================================================

    def create_widgets(self):

        # ---------------------------------------------
        # API Key
        # ---------------------------------------------

        ttk.Label(
            self.root,
            text="API Key:"
        ).grid(
            row=0,
            column=0,
            padx=5,
            pady=5,
            sticky="w"
        )

        ttk.Entry(
            self.root,
            textvariable=self.api_key,
            width=50,
            show="*"
        ).grid(
            row=0,
            column=1,
            padx=5,
            pady=5
        )


        # ---------------------------------------------
        # Input File
        # ---------------------------------------------

        ttk.Label(
            self.root,
            text="Input File:"
        ).grid(
            row=1,
            column=0,
            padx=5,
            pady=5,
            sticky="w"
        )

        ttk.Entry(
            self.root,
            textvariable=self.input_file,
            width=50
        ).grid(
            row=1,
            column=1,
            padx=5,
            pady=5
        )

        ttk.Button(
            self.root,
            text="...",
            command=lambda: self.browse_file(
                self.input_file
            )
        ).grid(
            row=1,
            column=2,
            padx=5,
            pady=5
        )


        # ---------------------------------------------
        # Data Folder
        # ---------------------------------------------

        ttk.Label(
            self.root,
            text="Data Folder:"
        ).grid(
            row=2,
            column=0,
            padx=5,
            pady=5,
            sticky="w"
        )

        ttk.Entry(
            self.root,
            textvariable=self.data_folder,
            width=50
        ).grid(
            row=2,
            column=1,
            padx=5,
            pady=5
        )

        ttk.Button(
            self.root,
            text="...",
            command=lambda: self.browse_folder(
                self.data_folder
            )
        ).grid(
            row=2,
            column=2,
            padx=5,
            pady=5
        )


        # ---------------------------------------------
        # Validate button
        # ---------------------------------------------

        ttk.Button(
            self.root,
            text="Validate Inputs",
            command=self.validate_inputs
        ).grid(
            row=3,
            column=1,
            padx=5,
            pady=10
        )


        # ---------------------------------------------
        # Overview
        # ---------------------------------------------

        self.overview_text = (
            scrolledtext.ScrolledText(
                self.root,
                width=80,
                height=12
            )
        )

        self.overview_text.grid(
            row=4,
            column=0,
            columnspan=3,
            padx=5,
            pady=5
        )


        # ---------------------------------------------
        # Progress bar
        # ---------------------------------------------

        self.progress = ttk.Progressbar(
            self.root,
            orient="horizontal",
            length=400,
            mode="determinate"
        )

        self.progress.grid(
            row=5,
            column=0,
            columnspan=3,
            padx=5,
            pady=5
        )


        # ---------------------------------------------
        # Log output
        # ---------------------------------------------

        self.log_output = (
            scrolledtext.ScrolledText(
                self.root,
                width=80,
                height=12
            )
        )

        self.log_output.grid(
            row=6,
            column=0,
            columnspan=3,
            padx=5,
            pady=5
        )


        # ---------------------------------------------
        # Upload button
        # ---------------------------------------------

        ttk.Button(
            self.root,
            text="Upload",
            command=self.start_upload
        ).grid(
            row=7,
            column=1,
            padx=5,
            pady=10
        )


    # =================================================
    # Browse input file
    # =================================================

    def browse_file(
        self,
        variable
    ):

        filename = (
            filedialog.askopenfilename(
                title="Select Input File",
                filetypes=[
                    (
                        "CSV and Excel files",
                        "*.csv *.xlsx *.xls"
                    ),
                    (
                        "Excel files",
                        "*.xlsx *.xls"
                    ),
                    (
                        "CSV files",
                        "*.csv"
                    ),
                    (
                        "All files",
                        "*.*"
                    )
                ]
            )
        )

        if filename:

            variable.set(
                filename
            )


    # =================================================
    # Browse data folder
    # =================================================

    def browse_folder(
        self,
        variable
    ):

        foldername = (
            filedialog.askdirectory(
                title="Select Data Folder"
            )
        )

        if foldername:

            variable.set(
                foldername
            )


    # =================================================
    # Validate inputs
    # =================================================

    def validate_inputs(
        self
    ):

        api_key = (
            self.api_key.get().strip()
        )

        input_file = (
            self.input_file.get().strip()
        )

        data_folder = (
            self.data_folder.get().strip()
        )

        # ---------------------------------------------
        # Check fields
        # ---------------------------------------------

        if (
            not api_key
            or not input_file
            or not data_folder
        ):

            messagebox.showerror(
                "Error",
                "All fields are required!"
            )

            return False


        # ---------------------------------------------
        # Check input file
        # ---------------------------------------------

        if not os.path.exists(
            input_file
        ):

            messagebox.showerror(
                "Error",
                "Input file does not exist!"
            )

            return False


        # ---------------------------------------------
        # Check data folder
        # ---------------------------------------------

        if not os.path.isdir(
            data_folder
        ):

            messagebox.showerror(
                "Error",
                "Data folder does not exist!"
            )

            return False


        try:

            # -----------------------------------------
            # Read CSV / Excel
            # -----------------------------------------

            df = read_input_file(
                input_file
            )

            # -----------------------------------------
            # Validate columns
            # -----------------------------------------

            validate_columns(
                df
            )

            overview = []

            # -----------------------------------------
            # Validate each row
            # -----------------------------------------

            for index, row in df.iterrows():

                operation = clean(
                    row.get(
                        "operation_type"
                    )
                )

                dataset_id = clean(
                    row.get(
                        "dataset_id"
                    )
                )

                file_name = clean(
                    row.get(
                        "file_path"
                    )
                )

                url = clean(
                    row.get(
                        "url"
                    )
                )

                title = clean(
                    row.get(
                        "title"
                    )
                )


                # -------------------------------------
                # file_upload
                # -------------------------------------

                if operation == "file_upload":

                    if not file_name:

                        messagebox.showerror(
                            "Error",
                            (
                                f"Row {index + 2}: "
                                "file_path is required "
                                "for file_upload."
                            )
                        )

                        return False


                    # Resolve filename against
                    # selected Data Folder
                    full_path = (
                        resolve_file_path(
                            file_name,
                            data_folder
                        )
                    )


                    # Check actual file
                    if not os.path.exists(
                        full_path
                    ):

                        messagebox.showerror(
                            "Error",
                            (
                                f"Row {index + 2}:\n\n"
                                f"File not found:\n"
                                f"{full_path}\n\n"
                                f"Filename from Excel:\n"
                                f"{file_name}\n\n"
                                f"Data Folder:\n"
                                f"{data_folder}"
                            )
                        )

                        return False


                    overview.append(
                        (
                            f"Dataset: {dataset_id} | "
                            f"File: "
                            f"{os.path.basename(full_path)} | "
                            f"Path: {full_path} | "
                            f"Title: {title}"
                        )
                    )


                # -------------------------------------
                # add_link
                # -------------------------------------

                elif operation == "add_link":

                    if not url:

                        messagebox.showerror(
                            "Error",
                            (
                                f"Row {index + 2}: "
                                "URL is required "
                                "for add_link."
                            )
                        )

                        return False


                    if not url.startswith(
                        (
                            "http://",
                            "https://"
                        )
                    ):

                        messagebox.showerror(
                            "Error",
                            (
                                f"Row {index + 2}: "
                                f"Invalid URL:\n{url}"
                            )
                        )

                        return False


                    overview.append(
                        (
                            f"Dataset: {dataset_id} | "
                            f"URL: {url} | "
                            f"Title: {title}"
                        )
                    )


                # -------------------------------------
                # Invalid operation
                # -------------------------------------

                else:

                    messagebox.showerror(
                        "Error",
                        (
                            f"Row {index + 2}: "
                            f"Invalid operation type:\n\n"
                            f"'{operation}'\n\n"
                            "Allowed values are:\n"
                            "- file_upload\n"
                            "- add_link"
                        )
                    )

                    return False


            # -----------------------------------------
            # Display overview
            # -----------------------------------------

            self.overview_text.delete(
                1.0,
                tk.END
            )

            self.overview_text.insert(
                tk.END,
                "Upload Overview:\n\n"
            )

            for item in overview:

                self.overview_text.insert(
                    tk.END,
                    f"{item}\n"
                )


            messagebox.showinfo(
                "Success",
                (
                    "Inputs are valid!\n\n"
                    "Review the upload overview "
                    "before clicking Upload."
                )
            )

            return True


        except Exception as e:

            logging.exception(
                "Input validation failed"
            )

            messagebox.showerror(
                "Error",
                (
                    "Failed to parse input file:\n\n"
                    f"{e}"
                )
            )

            return False


    # =================================================
    # Start upload
    # =================================================

    def start_upload(
        self
    ):

        if not self.validate_inputs():
            return


        api_key = (
            self.api_key.get().strip()
        )

        input_file = (
            self.input_file.get().strip()
        )

        data_folder = (
            self.data_folder.get().strip()
        )


        # Reset UI
        self.progress["value"] = 0

        self.log_output.delete(
            1.0,
            tk.END
        )


        self.log_output.insert(
            tk.END,
            "Starting upload...\n"
        )


        # Run upload in background thread
        threading.Thread(
            target=self.run_upload,
            args=(
                api_key,
                input_file,
                data_folder
            ),
            daemon=True
        ).start()


    # =================================================
    # Run upload
    # =================================================

    def run_upload(
        self,
        api_key,
        input_file,
        data_folder
    ):

        try:

            # -----------------------------------------
            # Create uploader
            # -----------------------------------------

            uploader = UdataUploader(
                api_key
            )


            # -----------------------------------------
            # Process CSV / Excel
            #
            # IMPORTANT:
            # data_folder is passed here so that
            # filenames from Excel are resolved
            # against the selected folder.
            # -----------------------------------------

            uploader.process_input_file(
                input_file,
                data_folder
            )


            # -----------------------------------------
            # Generate report
            # -----------------------------------------

            report_path = os.path.join(
                data_folder,
                "output_report.csv"
            )

            uploader.generate_report(
                report_path
            )


            # -----------------------------------------
            # Tell GUI upload succeeded
            # -----------------------------------------

            self.root.after(
                0,
                self.upload_finished,
                report_path
            )


        except Exception as e:

            logging.exception(
                "Upload process failed"
            )

            self.root.after(
                0,
                self.upload_failed,
                str(e)
            )


    # =================================================
    # Upload completed
    # =================================================

    def upload_finished(
        self,
        report_path
    ):

        self.progress["value"] = 100

        self.log_output.insert(
            tk.END,
            "\nUpload completed successfully!\n"
        )

        self.log_output.insert(
            tk.END,
            f"\nReport created:\n"
            f"{report_path}\n"
        )

        messagebox.showinfo(
            "Success",
            (
                "Upload completed successfully!\n\n"
                f"Report:\n{report_path}"
            )
        )


    # =================================================
    # Upload failed
    # =================================================

    def upload_failed(
        self,
        error
    ):

        self.log_output.insert(
            tk.END,
            "\nUpload failed:\n"
        )

        self.log_output.insert(
            tk.END,
            f"{error}\n"
        )

        messagebox.showerror(
            "Upload Error",
            (
                "Upload failed:\n\n"
                f"{error}"
            )
        )


# =====================================================
# Main
# =====================================================

if __name__ == "__main__":

    root = tk.Tk()

    app = UDataUploaderGUI(
        root
    )

    root.mainloop()
