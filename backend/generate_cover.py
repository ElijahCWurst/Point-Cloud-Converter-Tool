import sys
import json
import traceback
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

import utils 
import generate_chm # Imports the shared 1m generator

def calculate_stratified_cover(input_path, output_path, target_res, slider_values):
    
    # 1. Get High-Res Data (Cached)
    chm_high_res, hr_width, hr_height, hr_transform, hr_crs = generate_chm.get_or_create_1m_chm(
        input_path, 
        progress_callback=lambda p, m: utils.send_progress(p*0.1, f"Preparing: {m}")
    )

    if chm_high_res is None:
        return False, "Could not generate underlying CHM"
    
    # Replace nodata with 0 for calculation math
    chm_high_res[chm_high_res == utils.DEFAULT_NODATA] = 0

    # 2. Define Bins
    valid_sliders = []
    if slider_values:
        for v in slider_values:
            try:
                f = float(v)
                if np.isfinite(f): valid_sliders.append(f)
            except: continue
    
    # --- MODIFICATION START ---
    # If no sliders are provided, we default to a single "Canopy Cover" band.
    # We set the lower bound to 2.0m to exclude ground/shrub cover.
    if not valid_sliders:
        bins = [(2.0, np.inf)]
    else:
        valid_sliders.sort()
        boundaries = [0] + valid_sliders + [np.inf]
        bins = list(zip(boundaries[:-1], boundaries[1:]))
    # --- MODIFICATION END ---

    # 3. Prepare Target Output Grid
    scale_x = target_res / generate_chm.HIGH_RES_STEP 
    scale_y = target_res / generate_chm.HIGH_RES_STEP
    tgt_width = int(hr_width / scale_x)
    tgt_height = int(hr_height / scale_y)
    tgt_transform = hr_transform * hr_transform.scale(scale_x, scale_y)

    profile = {
        'driver': 'GTiff',
        'height': tgt_height,
        'width': tgt_width,
        'count': len(bins),
        'dtype': rasterio.float32,
        'crs': hr_crs,
        'transform': tgt_transform,
        'nodata': -9999
    }

    # 4. Aggregation Loop
    print(f"Aggregating {len(bins)} bands to {target_res}m resolution...")
    utils.send_progress(15, "Starting Aggregation...")
    
    total_bands = len(bins)

    with rasterio.open(output_path, 'w', **profile) as dst:
        for i, (low, high) in enumerate(bins):
            
            # Update Progress (15% -> 100%)
            current_pct = 15 + ((i / total_bands) * 85)
            utils.send_progress(current_pct, f"Processing Band {i+1}/{total_bands}...")

            # If the lower bound is 0, we must use strictly greater (>) 
            # to exclude the ground (0.0) from the cover count.
            if low == 0:
                mask = ((chm_high_res > low) & (chm_high_res < high)).astype(np.float32)
            else:
                # For 2m or other thresholds, we typically include the threshold (>=)
                mask = ((chm_high_res >= low) & (chm_high_res < high)).astype(np.float32)
            
            # Aggregate
            aggregated_cover = np.zeros((tgt_height, tgt_width), dtype=np.float32)
            reproject(
                source=mask,
                destination=aggregated_cover,
                src_transform=hr_transform,
                src_crs=hr_crs,
                dst_transform=tgt_transform,
                dst_crs=hr_crs,
                resampling=Resampling.average
            )

            aggregated_cover *= 100
            
            band_idx = i + 1
            dst.write(aggregated_cover, band_idx)
            
            high_label = "MAX" if high == np.inf else f"{high}m"
            desc = f"Cover {low}m - {high_label}"
            dst.set_band_description(band_idx, desc)

    utils.send_progress(100, "Done!")
    return True, f"Success. {len(bins)} Bands."

def main():
    if len(sys.argv) < 5:
        print(json.dumps({"status": "error", "message": "Missing arguments"}))
        return

    try:
        input_path = sys.argv[1]
        output_path = sys.argv[2]
        resolution = float(sys.argv[3])
        thresholds_json = sys.argv[4]
        
        try:
            thresholds = json.loads(thresholds_json)
            if not isinstance(thresholds, list): thresholds = []
        except: thresholds = []

        success, msg = calculate_stratified_cover(
            input_path, output_path, resolution, thresholds
        )

        if success:
            print(json.dumps({"status": "success", "file": output_path}))
        else:
            print(json.dumps({"status": "error", "message": msg}))

    except Exception as e:
        print(json.dumps({
            "status": "error", 
            "message": f"Cover Crash: {str(e)}", 
            "traceback": traceback.format_exc()
        }))

if __name__ == "__main__":
    main()