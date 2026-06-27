from pathlib import Path

def load_metadata(path):
    metadata = {}
    with open(path) as f:
        for line in f:
            if "=" in line:
                key, value = line.split("=", 1)
                metadata[key.strip()] = value.strip()
    return metadata
