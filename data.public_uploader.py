import os
import requests
import pandas as pd
import mimetypes
import logging
import random
import string
import math
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
    "ZIP": "zip"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

# =====================================================
# Helpers
# =====================================================

def clean(value):
    """
    Convert pandas NaN values into empty strings.
    """
    if pd.isna(value):
        return ""
    return str(value).strip()

# =====================================================
# UData uploader
# =====================================================

class UdataUploader:
    def __init__(self, api_token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "application/json",
                "X-API-KEY": api_token
            }
        )
        self.report_data = []

    # -------------------------------------------------
    # Convert type to uData accepted value
    # -------------------------------------------------

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

    # -------------------------------------------------
    # Validate CSV row
    # -------------------------------------------------

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

    # -------------------------------------------------
    # Upload file resource
    # -------------------------------------------------

    def upload_file(self, row: Dict) -> Dict:
        dataset_id = clean(row["dataset_id"])
        file_path = Path(clean(row["file_path"]))

        if not file_path.exists():
            return {
                "status": "error",
                "error": f"File not found: {file_path}"
            }

        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = "application/octet-stream"

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
                        break  # End of file

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

            # Finalize the upload
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

    # -------------------------------------------------
    # Create remote URL resource
    # -------------------------------------------------

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

    # -------------------------------------------------
    # Process CSV
    # -------------------------------------------------

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

    # -------------------------------------------------
    # Generate report
    # -------------------------------------------------

    def generate_report(self, output_path: str):
        pd.DataFrame(self.report_data).to_csv(output_path, index=False)

# =====================================================
# Main
# =====================================================

if __name__ == "__main__":
    API_TOKEN = ""

    uploader = UdataUploader(api_token=API_TOKEN)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_csv = os.path.join(script_dir, "input.csv")
    output_report = os.path.join(script_dir, "output_report.csv")

    uploader.process_csv(input_csv)
    uploader.generate_report(output_report)

    logging.info("Completed. Report: %s", output_report)
