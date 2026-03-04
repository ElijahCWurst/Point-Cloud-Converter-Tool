import sys
import json
import traceback
import os
import numpy as np
import laspy
import rasterio
import warnings
from rasterio.fill import fillnodata
from scipy.ndimage import gaussian_filter
from scipy.interpolate import LinearNDInterpolator
from rasterio.enums import Resampling
from rasterio.warp import reproject

import utils 

# --- SETTINGS ---
MORPH_SEARCH_METERS = 20.0  
MIN_CANOPY_HEIGHT = 0.0     
TIN_TOLERANCE = 0.5
TIN_MAX_ITERATIONS = 5
PERCENTILE_RANK = 0.99  # 99th percentile 
UNCLASSIFIED_OFFSET = 0.0
HIGH_RES_STEP = 0.5 # The resolution of the cached master file

# --- 1. CORE ALGORITHMS (TIN / DSM) ---

def perform_tin_densification(dtm_raw_grid, resolution, search_meters):
    rows, cols = dtm_raw_grid.shape
    valid_y, valid_x = np.nonzero(dtm_raw_grid != np.inf)
    
    if len(valid_y) < 3: return dtm_raw_grid 
        
    valid_z = dtm_raw_grid[valid_y, valid_x]
    
    block_size = int(search_meters / resolution)
    if block_size < 1: block_size = 1
    
    coarse_y = valid_y // block_size
    coarse_x = valid_x // block_size
    block_ids = coarse_y * cols + coarse_x
    
    sorter = np.lexsort((valid_z, block_ids))
    sorted_block_ids = block_ids[sorter]
    _, start_indices = np.unique(sorted_block_ids, return_index=True)
    
    end_indices = np.append(start_indices[1:], len(block_ids))
    counts = end_indices - start_indices
    points_to_skip = 2 
    safe_offsets = np.clip(points_to_skip, 0, counts - 1)
    
    seed_indices_sorted = start_indices + safe_offsets
    seed_indices = sorter[seed_indices_sorted]
    
    is_ground_mask = np.zeros(len(valid_z), dtype=bool)
    is_ground_mask[seed_indices] = True
    
    for i in range(TIN_MAX_ITERATIONS):
        ground_x = valid_x[is_ground_mask]
        ground_y = valid_y[is_ground_mask]
        ground_z = valid_z[is_ground_mask]
        
        try:
            tin_surf = LinearNDInterpolator(list(zip(ground_x, ground_y)), ground_z)
        except Exception:
            break

        candidate_mask = ~is_ground_mask
        cand_x = valid_x[candidate_mask]
        cand_y = valid_y[candidate_mask]
        cand_z = valid_z[candidate_mask]
        
        if len(cand_x) == 0: break
            
        predicted_z = tin_surf(list(zip(cand_x, cand_y)))
        residuals = cand_z - predicted_z
        new_ground = (np.abs(residuals) < TIN_TOLERANCE)
        
        if np.sum(new_ground) == 0: break
            
        full_indices = np.arange(len(valid_z))
        accepted_indices = full_indices[candidate_mask][new_ground]
        is_ground_mask[accepted_indices] = True

    final_dtm = np.full(dtm_raw_grid.shape, np.inf)
    final_dtm[valid_y[is_ground_mask], valid_x[is_ground_mask]] = valid_z[is_ground_mask]
    return final_dtm

def generate_dtm_grid(las_file, width, height, resolution, bounds, progress_callback=None):
    min_x, max_y = bounds
    dtm_class2 = np.full((height, width), np.inf)
    dtm_raw_min = np.full((height, width), np.inf)
    has_class_2 = False

    total_points = las_file.header.point_count
    points_processed = 0

    if progress_callback: progress_callback(1, "Scanning Ground Points...")

    for points in las_file.chunk_iterator(utils.CHUNK_SIZE):
        x = points.x
        y = points.y
        z = points.z
        classification = points.classification

        col_indices = np.clip(((x - min_x) / resolution).astype(int), 0, width - 1)
        row_indices = np.clip(((max_y - y) / resolution).astype(int), 0, height - 1)
        flat_indices = row_indices * width + col_indices

        np.minimum.at(dtm_raw_min.ravel(), flat_indices, z)
        
        ground_mask = (classification == 2)
        if np.any(ground_mask):
            has_class_2 = True
            np.minimum.at(dtm_class2.ravel(), flat_indices[ground_mask], z[ground_mask])
        
        points_processed += len(x)
        if progress_callback:
            pct = (points_processed / total_points) * 45
            progress_callback(pct, "Scanning Ground Points...")

    if progress_callback: progress_callback(46, "Interpolating Terrain Model...")

    if has_class_2:
        dtm_grid = dtm_class2
        mask_valid = (dtm_grid != np.inf)
        dtm_grid[~mask_valid] = utils.DEFAULT_NODATA
        dtm_final = fillnodata(dtm_grid, mask=mask_valid, max_search_distance=100.0)
    else:
        valid_mask = (dtm_raw_min != np.inf)
        if not np.any(valid_mask): return None, False

        tin_ground_grid = perform_tin_densification(dtm_raw_min, resolution, MORPH_SEARCH_METERS)
        
        mask_tin_valid = (tin_ground_grid != np.inf)
        if not np.any(mask_tin_valid):
            tin_ground_grid = dtm_raw_min
            mask_tin_valid = valid_mask
        
        tin_ground_grid[~mask_tin_valid] = utils.DEFAULT_NODATA
        dtm_final = fillnodata(tin_ground_grid, mask=mask_tin_valid, max_search_distance=200.0)
        dtm_final = gaussian_filter(dtm_final, sigma=1)

    return dtm_final, has_class_2

def calculate_height_robust(las_file, width, height, resolution, bounds, progress_callback=None):
    min_x, max_y = bounds
    dtm_final, has_class_2 = generate_dtm_grid(las_file, width, height, resolution, bounds, progress_callback)
    if dtm_final is None: return None

    las_file.seek(0)
    
    dsm_z_chunks = []
    dsm_idx_chunks = []
    
    total_points = las_file.header.point_count
    points_processed = 0

    if progress_callback: progress_callback(50, "Scanning Canopy...")

    for points in las_file.chunk_iterator(utils.CHUNK_SIZE):
        x = points.x
        y = points.y
        z = points.z
        
        col_indices = np.clip(((x - min_x) / resolution).astype(int), 0, width - 1)
        row_indices = np.clip(((max_y - y) / resolution).astype(int), 0, height - 1)
        flat_indices = row_indices * width + col_indices
        
        dsm_z_chunks.append(z)
        dsm_idx_chunks.append(flat_indices)

        points_processed += len(x)
        if progress_callback:
            pct = 50 + ((points_processed / total_points) * 45)
            progress_callback(pct, "Scanning Canopy...")

    if not dsm_z_chunks: return None
    
    if progress_callback: progress_callback(96, "Calculating Height Model...")

    big_z = np.concatenate(dsm_z_chunks)
    big_idx = np.concatenate(dsm_idx_chunks)
    
    sorter = np.lexsort((big_z, big_idx))
    unique_indices, start_locs = np.unique(big_idx[sorter], return_index=True)
    end_locs = np.append(start_locs[1:], len(big_idx))
    
    counts = end_locs - start_locs
    p_offsets = start_locs + (counts * PERCENTILE_RANK).astype(int)
    
    dsm_grid = np.full((height, width), utils.DEFAULT_NODATA, dtype=np.float32)
    dsm_grid.ravel()[unique_indices] = big_z[sorter][p_offsets]

    output_data = np.full((height, width), utils.DEFAULT_NODATA, dtype=np.float32)
    valid_mask = (dsm_grid != utils.DEFAULT_NODATA) & (dtm_final != utils.DEFAULT_NODATA)
    
    raw_height = dsm_grid[valid_mask] - dtm_final[valid_mask]
    
    if not has_class_2:
        raw_height -= UNCLASSIFIED_OFFSET

    raw_height[raw_height < 0] = 0
    raw_height[raw_height < MIN_CANOPY_HEIGHT] = 0
    output_data[valid_mask] = raw_height
    
    return output_data

# --- 2. CACHING LOGIC (The new "Brains") ---

def get_cached_chm_path(input_path):
    dir_name = os.path.dirname(input_path)
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    # Dynamically puts the resolution in the name (e.g. "..._internal_chm_0.5m.tif")
    return os.path.join(dir_name, f"{base_name}_internal_chm_{HIGH_RES_STEP}m.tif")

def get_or_create_1m_chm(input_path, progress_callback=None):
    """
    Ensures the 1m CHM exists. 
    If being generated by another process (locked), waits.
    If not exists, generates it.
    Returns: (data, width, height, transform, crs)
    """
    cache_path = get_cached_chm_path(input_path)
    lock = utils.FileLock(cache_path)

    # 1. Check if Locked (Another process is building it)
    if lock.is_locked():
        if progress_callback: progress_callback(5, "Waiting for background processing...")
        lock.wait() # Block until lock is gone

    # 2. Check if exists
    if os.path.exists(cache_path):
        if progress_callback: progress_callback(10, "Loading cached CHM...")
        with rasterio.open(cache_path) as src:
            data = src.read(1)
            # Normalize nodata
            nodata = src.nodata
            if nodata is not None:
                data[data == nodata] = utils.DEFAULT_NODATA
            # Also handle NaNs
            data[np.isnan(data)] = utils.DEFAULT_NODATA
            
            return data, src.width, src.height, src.transform, src.crs

    # 3. Generate New (If we got here, file doesn't exist and wasn't locked)
    try:
        lock.acquire() # Grab the lock so no one else generates
        
        if progress_callback: progress_callback(5, f"Generating {HIGH_RES_STEP}m Model...")
        
        with laspy.open(input_path) as las_file:
            width, height, transform, bounds = utils.get_grid_dimensions(las_file.header, HIGH_RES_STEP)
            
            # Pass our callback, but scale it so it fits in 0-95%
            def sub_prog(p, m):
                if progress_callback: progress_callback(p, f"Internal {HIGH_RES_STEP}m: {m}")

            chm_data = calculate_height_robust(
                las_file, width, height, HIGH_RES_STEP, bounds, progress_callback=sub_prog
            )

            if chm_data is None: return None, None, None, None, None

            # Save
            try: crs = las_file.header.parse_crs()
            except: crs = None
            
            utils.save_raster(cache_path, chm_data, width, height, transform, crs)
            
            return chm_data, width, height, transform, crs

    finally:
        lock.release()


def resample_custom_percentile(source_data, src_nodata, scale_x, scale_y, rank_0_to_100):
    """
    Downsamples grid by calculating percentile in chunks to save RAM.
    Optimized for weak machines.
    """
    h, w = source_data.shape
    
    # 1. Determine Output Size
    new_h = int(h // scale_y)
    new_w = int(w // scale_x)
    
    # Prepare output array (float32 to handle NaNs during math)
    result_grid = np.full((new_h, new_w), src_nodata, dtype=np.float32)

    # 2. Settings for Chunking
    # We process 'row_chunk_size' output rows at a time.
    # 100 rows is very safe for RAM (even on 4GB machines)
    chunk_rows_out = 100  
    
    # 3. Iterate over the grid in strips
    for i in range(0, new_h, chunk_rows_out):
        # Determine the range of rows in the OUTPUT
        r_out_start = i
        r_out_end = min(i + chunk_rows_out, new_h)
        rows_in_this_chunk = r_out_end - r_out_start
        
        # Determine the corresponding range of rows in the INPUT
        r_src_start = int(r_out_start * scale_y)
        r_src_end = int(r_src_start + (rows_in_this_chunk * scale_y))
        
        # Determine width to trim (must be multiple of scale)
        eff_w = int(new_w * scale_x)
        
        # Extract just this strip from source
        src_strip = source_data[r_src_start:r_src_end, :eff_w]
        
        # Convert to float/NaN for math (Only consumes RAM for this small strip)
        working_strip = src_strip.astype(np.float32)
        working_strip[working_strip == src_nodata] = np.nan
        
        # Reshape: (rows, scale_y, cols, scale_x)
        view = working_strip.reshape(
            rows_in_this_chunk, 
            int(scale_y), 
            new_w, 
            int(scale_x)
        )
        
        # Calculate Percentile (e.g. 95 or 98)
        # Suppress "All-NaN" warnings for empty space
        # ... inside resample_custom_percentile ...

        # Calculate Percentile (e.g. 95 or 98)
        # Suppress "All-NaN" warnings for empty space
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', r'All-NaN (slice|axis) encountered')
            # axis=(1, 3) collapses the blocks into single pixels
            chunk_result = np.nanpercentile(view, rank_0_to_100, axis=(1, 3))
        # with np.warnings.catch_warnings():
        #     np.warnings.filterwarnings('ignore', r'All-NaN (slice|axis) encountered')
        #     # axis=(1, 3) collapses the blocks into single pixels
        #     chunk_result = np.nanpercentile(view, rank_0_to_100, axis=(1, 3))
        
        # Write this chunk to the final result
        result_grid[r_out_start:r_out_end, :] = chunk_result
        
        # Explicitly delete temporary vars to free RAM
        del working_strip, view, chunk_result

    # Fill NaNs back with NoData
    result_grid[np.isnan(result_grid)] = src_nodata
    
    return result_grid
# --- 3. MAIN (Modified for Resampling) ---

# ... [Keep all imports and previous functions exactly the same] ...

# --- 3. MAIN (Modified for Resampling Selection) ---

# Inside generate_chm.py

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"status": "error", "message": "Missing arguments"}))
        return

    # A. PRE-CACHE MODE
    if len(sys.argv) == 3 and sys.argv[2] == "--precache":
        # ... (Keep existing precache logic) ...
        return

    try:
        input_path = sys.argv[1]
        output_path = sys.argv[2]
        target_res = float(sys.argv[3])
        
        # Default to 95 if something goes wrong, but we expect an integer from JS
        custom_percentile_val = 95.0 

        if len(sys.argv) > 4:
            try:
                # We simply float() it. JS ensures it's an integer 1-100.
                custom_percentile_val = float(sys.argv[4])
            except ValueError:
                print(json.dumps({"status": "error", "message": "Invalid percentile value provided."}))
                return

        # 1. Get 1m Data
        chm_1m, width_1m, height_1m, transform_1m, crs = get_or_create_1m_chm(
            input_path, 
            progress_callback=utils.send_progress
        )

        if chm_1m is None:
            print(json.dumps({"status": "error", "message": "Could not generate CHM"}))
            return

        # 2. Check Scaling
        scale_x = target_res / HIGH_RES_STEP
        scale_y = target_res / HIGH_RES_STEP
        
        # CRITICAL CHECK: The custom percentile code crashes on non-integer scaling
        if not scale_x.is_integer() or not scale_y.is_integer():
             print(json.dumps({
                "status": "error", 
                "message": f"Percentile resampling requires integer scaling. {target_res}m is not a multiple of {HIGH_RES_STEP}m."
            }))
             return

        tgt_width = int(width_1m / scale_x)
        tgt_height = int(height_1m / scale_y)
        tgt_transform = transform_1m * transform_1m.scale(scale_x, scale_y)

        # 3. Perform Resampling (Only Custom Percentile now)
        utils.send_progress(95, f"Resampling (P{int(custom_percentile_val)}) to {target_res}m...")
        
        output_data = resample_custom_percentile(
            chm_1m, 
            utils.DEFAULT_NODATA, 
            scale_x, 
            scale_y, 
            custom_percentile_val
        )

        # 4. Save
        utils.save_raster(output_path, output_data, tgt_width, tgt_height, tgt_transform, crs)
        utils.send_progress(100, "Done")
        print(json.dumps({"status": "success", "file": output_path}))

    except Exception as e:
        print(json.dumps({"status": "error", "message": f"CHM Crash: {str(e)}", "traceback": traceback.format_exc()}))

if __name__ == "__main__":
    main()