import csv
import requests
import json
import time

# ============================================================
# Configuration
# ============================================================

API_ENDPOINT = "https://data.public.lu/api/1/"

# Put your API key here
API_KEY = "YOU_API_KEY_HERE"  # Replace with your actual API key

# Windows path to your CSV file
# csv format:
# ID|keyword
# 6a7498f01798b758f5884ce9|DWC-OCCURRENCE,OCCURRENCE
CSV_FILE = r"YOU_FILE_PATH_HERE"


# ============================================================
# Headers
# ============================================================

headers = {
    "X-API-KEY": API_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
}


# ============================================================
# Check API authentication
# ============================================================

print("Checking API authentication...")

try:
    response = requests.get(
        f"{API_ENDPOINT}me/",
        headers=headers,
        timeout=30,
    )

    print(f"Authentication status: {response.status_code}")

    if response.status_code != 200:
        print("Authentication failed.")
        print("Response:")
        print(response.text)
        raise SystemExit(1)

    print("Authentication successful.\n")

except requests.RequestException as e:
    print(f"Could not connect to the API: {e}")
    raise SystemExit(1)


# ============================================================
# Read CSV file
# ============================================================

print(f"Reading CSV file:\n{CSV_FILE}\n")

try:
    with open(
        CSV_FILE,
        mode="r",
        encoding="utf-8-sig",
        newline=""
    ) as csv_file:

        reader = csv.DictReader(
            csv_file,
            delimiter="|"
        )

        # Check required columns
        if not reader.fieldnames:
            print("ERROR: CSV file is empty.")
            raise SystemExit(1)

        required_columns = {"ID", "keyword"}

        if not required_columns.issubset(set(reader.fieldnames)):
            print("ERROR: CSV must contain these columns:")
            print("ID|keyword")
            print(f"Found columns: {reader.fieldnames}")
            raise SystemExit(1)

        rows = list(reader)

except FileNotFoundError:
    print("ERROR: CSV file not found.")
    print(f"Check this path:\n{CSV_FILE}")
    raise SystemExit(1)

except OSError as e:
    print(f"ERROR reading CSV file: {e}")
    raise SystemExit(1)


print(f"Found {len(rows)} dataset(s) in CSV.\n")


# ============================================================
# Process each dataset
# ============================================================

successful = 0
failed = 0
unchanged = 0

for row_number, row in enumerate(rows, start=2):

    dataset_id = row["ID"].strip()
    keyword_string = row["keyword"].strip()

    # --------------------------------------------------------
    # Validate row
    # --------------------------------------------------------

    if not dataset_id:
        print(f"Row {row_number}: ERROR - missing dataset ID.")
        failed += 1
        continue

    if not keyword_string:
        print(
            f"Row {row_number}: ERROR - "
            f"no keywords specified for {dataset_id}."
        )
        failed += 1
        continue


    # --------------------------------------------------------
    # Convert CSV keywords into a list
    # --------------------------------------------------------

    new_keywords = [
        keyword.strip()
        for keyword in keyword_string.split(",")
        if keyword.strip()
    ]

    print("=" * 70)
    print(f"Dataset: {dataset_id}")
    print(f"New keywords from CSV: {new_keywords}")
    print("=" * 70)


    dataset_url = f"{API_ENDPOINT}datasets/{dataset_id}/"


    # --------------------------------------------------------
    # Retrieve existing dataset
    # --------------------------------------------------------

    try:
        response = requests.get(
            dataset_url,
            headers=headers,
            timeout=30,
        )

    except requests.RequestException as e:
        print(f"ERROR retrieving dataset: {e}")
        failed += 1
        continue


    if response.status_code != 200:
        print(
            f"ERROR retrieving dataset. "
            f"Status code: {response.status_code}"
        )
        print(f"Response: {response.text}")
        failed += 1
        continue


    # --------------------------------------------------------
    # Parse dataset JSON
    # --------------------------------------------------------

    try:
        dataset = response.json()

    except ValueError:
        print("ERROR: API returned invalid JSON.")
        print(response.text)
        failed += 1
        continue


    # --------------------------------------------------------
    # Get existing tags
    # --------------------------------------------------------

    existing_tags = dataset.get("tags", [])

    print("\nExisting tags:")
    print(
        json.dumps(
            existing_tags,
            indent=2,
            ensure_ascii=False
        )
    )


    # --------------------------------------------------------
    # Make sure existing tags are strings
    # --------------------------------------------------------

    cleaned_existing_tags = []

    for tag in existing_tags:

        # If uData returns tags as strings
        if isinstance(tag, str):
            cleaned_existing_tags.append(tag)

        # If uData returns tag objects
        elif isinstance(tag, dict):

            # Try common tag representations
            tag_name = (
                tag.get("name")
                or tag.get("label")
                or tag.get("id")
            )

            if tag_name:
                cleaned_existing_tags.append(str(tag_name))


    # --------------------------------------------------------
    # Compare existing and new tags
    # --------------------------------------------------------

    # Used for case-insensitive duplicate detection
    existing_lower = {
        tag.strip().casefold()
        for tag in cleaned_existing_tags
    }

    tags_to_add = []

    for keyword in new_keywords:

        keyword_clean = keyword.strip()

        if not keyword_clean:
            continue

        if keyword_clean.casefold() not in existing_lower:

            tags_to_add.append(keyword_clean)

            # Add to set immediately so duplicates
            # within the CSV are also avoided
            existing_lower.add(keyword_clean.casefold())


    # --------------------------------------------------------
    # Show what will happen
    # --------------------------------------------------------

    print("\nTags to add:")

    if tags_to_add:
        print(
            json.dumps(
                tags_to_add,
                indent=2,
                ensure_ascii=False
            )
        )
    else:
        print("No new tags. Dataset already contains all keywords.")


    # --------------------------------------------------------
    # Nothing to update?
    # --------------------------------------------------------

    if not tags_to_add:

        print(
            f"\nNo update required for dataset {dataset_id}."
        )

        unchanged += 1

        # Move to next dataset
        continue


    # --------------------------------------------------------
    # Create combined tag list
    # --------------------------------------------------------

    combined_tags = (
        cleaned_existing_tags +
        tags_to_add
    )


    print("\nFinal tag list:")
    print(
        json.dumps(
            combined_tags,
            indent=2,
            ensure_ascii=False
        )
    )


    # --------------------------------------------------------
    # Prepare update payload
    # --------------------------------------------------------

    payload = {
        "tags": combined_tags
    }


    # --------------------------------------------------------
    # Update dataset
    # --------------------------------------------------------

    print("\nUpdating dataset...")

    try:

        update_response = requests.put(
            dataset_url,
            headers=headers,
            json=payload,
            timeout=30,
        )

    except requests.RequestException as e:

        print(f"ERROR updating dataset: {e}")
        failed += 1
        continue


    # --------------------------------------------------------
    # Process update response
    # --------------------------------------------------------

    if update_response.status_code in (200, 201):

        print(
            f"SUCCESS: Dataset {dataset_id} "
            f"updated successfully."
        )

        successful += 1

    else:

        print(
            f"FAILED: Dataset {dataset_id}"
        )

        print(
            f"Status code: "
            f"{update_response.status_code}"
        )

        print(
            f"Response: "
            f"{update_response.text}"
        )

        failed += 1


    # --------------------------------------------------------
    # Small delay between datasets
    # --------------------------------------------------------

    time.sleep(0.5)


# ============================================================
# Summary
# ============================================================

print("\n")
print("=" * 70)
print("UPDATE SUMMARY")
print("=" * 70)

print(f"Datasets in CSV:    {len(rows)}")
print(f"Successfully updated: {successful}")
print(f"Already complete:     {unchanged}")
print(f"Failed:               {failed}")

print("=" * 70)
