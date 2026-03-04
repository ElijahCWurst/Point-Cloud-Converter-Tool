import sys
import json
import traceback

# --- SAFE IMPORT BLOCK ---
try:
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.fill import fillnodata
    import laspy
    from scipy.ndimage import median_filter, gaussian_filter
    # NEW IMPORT FOR TIN
    from scipy.interpolate import LinearNDInterpolator
except ImportError as e:
    print(json.dumps({"status": "error", "message": f"Missing Library: {str(e)}"}))
    sys.exit(0)

# --- CONFIGURATION ---
DEFAULT_NODATA = -9999
CHUNK_SIZE = 1_000_000
UNCLASSIFIED_OFFSET = 0.0 # Changed to 0.0 (TIN usually finds true ground, offset not needed)

# --- ACCURACY SETTINGS ---
# 1. Ground Search (Meters): Grid size for initial seed selection.
MORPH_SEARCH_METERS = 20.0  

# 2. Minimum Height (Meters): Ignores bushes/cars below this height.
MIN_CANOPY_HEIGHT = 2.0     

# 3. TIN Tolerance (Meters): Points within this distance of the TIN are classified as ground
TIN_TOLERANCE = 0.5
TIN_MAX_ITERATIONS = 5

# 4. Percentile (90%):
PERCENTILE_RANK = 0.90 

def get_grid_dimensions(header, resolution):
    min_x, min_y, _ = header.mins
    max_x, max_y, _ = header.maxs
    width = int(np.ceil((max_x - min_x) / resolution))
    height = int(np.ceil((max_y - min_y) / resolution))
    transform = from_origin(min_x, max_y, resolution, resolution)
    return width, height, transform, (min_x, max_y)
def perform_tin_densification(dtm_raw_grid, resolution, search_meters):
    """
    Performs Progressive TIN Densification with Noise Filtering.
    """
    rows, cols = dtm_raw_grid.shape
    
    # 1. Extract all valid data points
    valid_y, valid_x = np.nonzero(dtm_raw_grid != np.inf)
    if len(valid_y) < 3:
        return dtm_raw_grid 
        
    valid_z = dtm_raw_grid[valid_y, valid_x]
    
    # 2. Select Initial Seeds (Block "Robust" Minimums)
    block_size = int(search_meters / resolution)
    if block_size < 1: block_size = 1
    
    coarse_y = valid_y // block_size
    coarse_x = valid_x // block_size
    block_ids = coarse_y * cols + coarse_x
    
    # Sort data by Block ID, then by Z height
    sorter = np.lexsort((valid_z, block_ids))
    
    # Get the start index of each unique block (this is the 1st lowest point)
    sorted_block_ids = block_ids[sorter]
    _, start_indices = np.unique(sorted_block_ids, return_index=True)
    
    # --- NEW: NOISE FILTERING ---
    # Instead of taking the absolute min (index + 0), we take the 3rd lowest (index + 2)
    # This skips isolated "pit" noise points.
    
    # Calculate how many points are in each block to avoid index errors
    # (e.g. if a block only has 1 point, we can't take the 3rd one)
    # We can calculate counts by differencing the start indices
    # Append total length to handle the last block
    end_indices = np.append(start_indices[1:], len(block_ids))
    counts = end_indices - start_indices
    
    # Define our "Robust Offset" (Skip the bottom 2 points to avoid noise)
    # If a block has fewer points, we clamp to the last available point.
    points_to_skip = 2 
    safe_offsets = np.clip(points_to_skip, 0, counts - 1)
    
    # Apply offsets
    seed_indices_sorted = start_indices + safe_offsets
    seed_indices = sorter[seed_indices_sorted]
    # -----------------------------
    
    is_ground_mask = np.zeros(len(valid_z), dtype=bool)
    is_ground_mask[seed_indices] = True
    
    # 3. Iterative Densification (Standard)
    for i in range(TIN_MAX_ITERATIONS):
        ground_x = valid_x[is_ground_mask]
        ground_y = valid_y[is_ground_mask]
        ground_z = valid_z[is_ground_mask]
        
        try:
            # optimize: only use qhull options if needed, standard call is usually fine
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
        
        # Check tolerance (0.5m)
        new_ground = (np.abs(residuals) < TIN_TOLERANCE)
        
        if np.sum(new_ground) == 0:
            break
            
        full_indices = np.arange(len(valid_z))
        accepted_indices = full_indices[candidate_mask][new_ground]
        is_ground_mask[accepted_indices] = True

    # 4. Construct Final Grid
    final_dtm = np.full(dtm_raw_grid.shape, np.inf)
    final_dtm[valid_y[is_ground_mask], valid_x[is_ground_mask]] = valid_z[is_ground_mask]
    
    return final_dtm
# def perform_tin_densification(dtm_raw_grid, resolution, search_meters):
#     """
#     Performs Progressive TIN Densification on a raster grid of minimum Z values.
#     """
#     rows, cols = dtm_raw_grid.shape
    
#     # 1. Extract all valid data points (y, x, z)
#     # We work in pixel coordinates for speed (Euclidean distance still holds relative)
#     valid_y, valid_x = np.nonzero(dtm_raw_grid != np.inf)
#     if len(valid_y) < 3:
#         return dtm_raw_grid # Not enough points to triangulate
        
#     valid_z = dtm_raw_grid[valid_y, valid_x]
    
#     # 2. Select Initial Seeds (Block Minimums)
#     # Calculate block size in pixels
#     block_size = int(search_meters / resolution)
#     if block_size < 1: block_size = 1
    
#     # Map every pixel to a coarse block ID
#     coarse_y = valid_y // block_size
#     coarse_x = valid_x // block_size
    
#     # Create a unique ID for each block (y * max_width + x)
#     # We use a large multiplier to ensure unique IDs
#     block_ids = coarse_y * cols + coarse_x
    
#     # Sort data by Block ID, then by Z height
#     # This puts the lowest Z for each block first in the sorted list
#     sorter = np.lexsort((valid_z, block_ids))
    
#     # Find indices where the Block ID changes (the unique blocks)
#     # Since we sorted by Z, the first occurrence of a block ID is the minimum Z
#     _, unique_indices = np.unique(block_ids[sorter], return_index=True)
    
#     # These are indices into the SORTED array
#     seed_indices_sorted = unique_indices
#     # Map back to original indices
#     seed_indices = sorter[seed_indices_sorted]
    
#     # Create a mask of which points are currently "Ground"
#     is_ground_mask = np.zeros(len(valid_z), dtype=bool)
#     is_ground_mask[seed_indices] = True
    
#     # 3. Iterative Densification
#     for i in range(TIN_MAX_ITERATIONS):
#         # Get current ground points (seeds)
#         ground_x = valid_x[is_ground_mask]
#         ground_y = valid_y[is_ground_mask]
#         ground_z = valid_z[is_ground_mask]
        
#         # Build TIN (Linear Interpolation Surface)
#         # using pixel coordinates as X/Y
#         try:
#             tin_surf = LinearNDInterpolator(list(zip(ground_x, ground_y)), ground_z)
#         except Exception:
#             # Degenerate mesh or errors
#             break

#         # Identify candidates: points NOT yet ground
#         # (Optimization: We only predict Z for non-ground points to save time)
#         candidate_mask = ~is_ground_mask
#         cand_x = valid_x[candidate_mask]
#         cand_y = valid_y[candidate_mask]
#         cand_z = valid_z[candidate_mask]
        
#         if len(cand_x) == 0: break
            
#         # Predict Z at candidate locations
#         predicted_z = tin_surf(list(zip(cand_x, cand_y)))
        
#         # Calculate residual (Actual - Predicted)
#         # NaN indicates the point is outside the Convex Hull of the seeds (ignore them)
#         residuals = cand_z - predicted_z
        
#         # Logic: If the point is physically close to the TIN surface, it's ground.
#         # usually: -Threshold < residual < Threshold
#         new_ground = (np.abs(residuals) < TIN_TOLERANCE)
        
#         # Count how many we are adding
#         added_count = np.sum(new_ground)
#         if added_count == 0:
#             break
            
#         # Update the master mask
#         # We need to map the "true" indices of the candidates back to the full array
#         # We can do this by iteratively updating positions
#         # Easier way: Just recreate mask? No, need to be specific.
        
#         # Get indices of ALL points, filter by candidate mask, then filter by new_ground
#         full_indices = np.arange(len(valid_z))
#         candidate_indices = full_indices[candidate_mask]
#         accepted_indices = candidate_indices[new_ground]
        
#         is_ground_mask[accepted_indices] = True

#     # 4. Construct Final Ground Grid
#     # We return a sparse grid with only the classified ground points
#     # The main function will use fillnodata to interpolate the gaps
#     final_dtm = np.full(dtm_raw_grid.shape, np.inf)
#     final_dtm[valid_y[is_ground_mask], valid_x[is_ground_mask]] = valid_z[is_ground_mask]
    
#     return final_dtm

def calculate_height_robust(las_file, width, height, resolution, bounds):
    min_x, max_y = bounds
    
    dsm_z_chunks = []
    dsm_idx_chunks = []
    dtm_class2 = np.full((height, width), np.inf)
    dtm_raw_min = np.full((height, width), np.inf)
    has_class_2 = False

    # --- STREAMING PASS ---
    for points in las_file.chunk_iterator(CHUNK_SIZE):
        x = points.x
        y = points.y
        z = points.z
        classification = points.classification

        col_indices = np.clip(((x - min_x) / resolution).astype(int), 0, width - 1)
        row_indices = np.clip(((max_y - y) / resolution).astype(int), 0, height - 1)
        flat_indices = row_indices * width + col_indices
        
        # DSM (Canopy Candidates)
        dsm_z_chunks.append(z)
        dsm_idx_chunks.append(flat_indices)

        # DTM (Ground Candidates)
        # Accumulate raw minimums for the TIN/Unclassified logic
        np.minimum.at(dtm_raw_min.ravel(), flat_indices, z)
        
        ground_mask = (classification == 2)
        if np.any(ground_mask):
            has_class_2 = True
            np.minimum.at(dtm_class2.ravel(), flat_indices[ground_mask], z[ground_mask])

    # --- DTM GENERATION ---
    dtm_final = None

    if has_class_2:
        # Scenario A: Classified (Trust the data)
        dtm_grid = dtm_class2
        mask_valid = (dtm_grid != np.inf)
        dtm_grid[~mask_valid] = DEFAULT_NODATA
        dtm_final = fillnodata(dtm_grid, mask=mask_valid, max_search_distance=100.0)

    else:
        # Scenario B: Unclassified (TIN Densification)
        valid_mask = (dtm_raw_min != np.inf)
        if not np.any(valid_mask):
            return None 
            
        # 1. Run TIN Densification
        # This returns a grid with ONLY ground points, others are inf
        tin_ground_grid = perform_tin_densification(dtm_raw_min, resolution, MORPH_SEARCH_METERS)
        
        # 2. Prepare for Interpolation
        mask_tin_valid = (tin_ground_grid != np.inf)
        
        # If TIN failed to find points (rare), fall back to raw minimums
        if not np.any(mask_tin_valid):
            tin_ground_grid = dtm_raw_min
            mask_tin_valid = valid_mask
        
        tin_ground_grid[~mask_tin_valid] = DEFAULT_NODATA
        
        # 3. Fill Gaps (Raster Interpolation of the TIN points)
        # We use rasterio's fillnodata (IDW) to smooth between the TIN points
        dtm_final = fillnodata(tin_ground_grid, mask=mask_tin_valid, max_search_distance=200.0)
        
        # 4. Optional Light Smooth to remove triangulation artifacts
        dtm_final = gaussian_filter(dtm_final, sigma=1)

    # --- DSM GENERATION (P90) ---
    if not dsm_z_chunks: return None
    big_z = np.concatenate(dsm_z_chunks)
    big_idx = np.concatenate(dsm_idx_chunks)
    
    sorter = np.lexsort((big_z, big_idx))
    unique_indices, start_locs = np.unique(big_idx[sorter], return_index=True)
    end_locs = np.append(start_locs[1:], len(big_idx))
    
    counts = end_locs - start_locs
    p_offsets = start_locs + (counts * PERCENTILE_RANK).astype(int)
    
    dsm_grid = np.full((height, width), DEFAULT_NODATA, dtype=np.float32)
    dsm_grid.ravel()[unique_indices] = big_z[sorter][p_offsets]

    # --- FINAL CALCULATION ---
    output_data = np.full((height, width), DEFAULT_NODATA, dtype=np.float32)
    valid_mask = (dsm_grid != DEFAULT_NODATA) & (dtm_final != DEFAULT_NODATA)
    
    raw_height = dsm_grid[valid_mask] - dtm_final[valid_mask]
    
    if not has_class_2:
        raw_height -= UNCLASSIFIED_OFFSET

    # 1. Clamp Negatives
    raw_height[raw_height < 0] = 0
    
    # 2. Understory Filter
    raw_height[raw_height < MIN_CANOPY_HEIGHT] = 0
    
    output_data[valid_mask] = raw_height
    return output_data

def calculate_cover(las_file, width, height, resolution, bounds):
    min_x, max_y = bounds
    total_counts = np.zeros((height, width), dtype=np.int32)
    ground_counts = np.zeros((height, width), dtype=np.int32)
    has_class_2 = False

    for points in las_file.chunk_iterator(CHUNK_SIZE):
        x = points.x
        y = points.y
        classification = points.classification
        
        col = np.clip(((x - min_x) / resolution).astype(int), 0, width - 1)
        row = np.clip(((max_y - y) / resolution).astype(int), 0, height - 1)
        flat = row * width + col

        np.add.at(total_counts.ravel(), flat, 1)
        ground_mask = (classification == 2)
        if np.any(ground_mask):
            has_class_2 = True
            np.add.at(ground_counts.ravel(), flat[ground_mask], 1)

    output_data = np.full((height, width), DEFAULT_NODATA, dtype=np.float32)
    valid_mask = (total_counts > 0)
    
    if has_class_2:
        output_data[valid_mask] = (total_counts[valid_mask] - ground_counts[valid_mask]) / total_counts[valid_mask]
    else:
        output_data[valid_mask] = 0.0
    return output_data

def save_raster(output_path, data, width, height, transform, crs):
    with rasterio.open(
        output_path, 'w', driver='GTiff', height=height, width=width, count=1,
        dtype=data.dtype, crs=crs, transform=transform, nodata=DEFAULT_NODATA
    ) as dst:
        dst.write(data, 1)

def main():
    if len(sys.argv) < 5:
        print(json.dumps({"status": "error", "message": "Missing arguments"}))
        return
    try:
        input_path = sys.argv[1]
        output_path = sys.argv[2]
        mode = sys.argv[3]
        resolution = float(sys.argv[4])

        with laspy.open(input_path) as las_file:
            width, height, transform, bounds = get_grid_dimensions(las_file.header, resolution)
            if mode == 'height':
                result = calculate_height_robust(las_file, width, height, resolution, bounds)
            elif mode == 'cover':
                result = calculate_cover(las_file, width, height, resolution, bounds)
            else:
                raise ValueError(f"Unknown mode: {mode}")

            if result is not None:
                try: crs = las_file.header.parse_crs()
                except: crs = None
                save_raster(output_path, result, width, height, transform, crs)
                print(json.dumps({"status": "success", "file": output_path}))
            else:
                print(json.dumps({"status": "error", "message": "Empty result"}))
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Script Crash: {str(e)}", "traceback": traceback.format_exc()}))

if __name__ == "__main__":
    main()