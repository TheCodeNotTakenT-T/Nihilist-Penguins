from pathlib import Path
import numpy as np
import rasterio

def dn_to_temperature(band_path: str | Path, metadata: dict, band: int = 10, unit: str = "celsius"):
    with rasterio.open(band_path) as src:
        dn = src.read(1)

    ML = float(metadata[f"RADIANCE_MULT_BAND_{band}"])
    AL = float(metadata[f"RADIANCE_ADD_BAND_{band}"])
    K1 = float(metadata[f"K1_CONSTANT_BAND_{band}"])
    K2 = float(metadata[f"K2_CONSTANT_BAND_{band}"])

    radiance = ML * dn + AL
    temperature = K2 / np.log((K1 / radiance) + 1)
    if unit == "celsius":
        temperature -= 273.15

    return {
        "dn": dn,
        "radiance": radiance,
        "temperature": temperature
    }

def validate_temperature(temp):
    print(f"Minimum: {temp.min():.2f} °C")
    print(f"Maximum: {temp.max():.2f} °C")
    print(f"Mean: {temp.mean():.2f} °C")

    if temp.min() < -100:
        print("Warning: Minimum temperature is below -100 °C, which is unusual.")

    if temp.max() > 100:
        print("Warning: Maximum temperature is above 100 °C, which is unusual.")
