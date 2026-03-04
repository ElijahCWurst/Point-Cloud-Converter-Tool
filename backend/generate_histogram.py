import sys
import argparse
import json
import os
import traceback
import laspy
import numpy as np
import matplotlib.pyplot as plt
import utils
import generate_chm # Import the shared logic

# Settings for histogram generation
HIST_RESOLUTION = 2.0 # Coarser res is fine for DTM generation for stats
GROUND_THRESHOLD = 0.5 # Points below 0.5m are considered "Ground" and hidden

def generate_normalized_histogram(input_path, output_path):
    try:
        plt.switch_backend('Agg')

        with laspy.open(input_path) as las_file:
            # 1. Setup Grid
            width, height, transform, bounds = utils.get_grid_dimensions(las_file.header, HIST_RESOLUTION)
            min_x, max_y = bounds

            # 2. Generate DTM (Ground Model) using the robust logic
            # This handles both Classified and Unclassified inputs automatically
            dtm_grid, has_class_2 = generate_chm.generate_dtm_grid(las_file, width, height, HIST_RESOLUTION, bounds)
            
            if dtm_grid is None:
                raise Exception("Could not generate Ground Model (DTM)")

            # 3. Read All Points to normalize them
            # We iterate chunks to avoid memory overflow on huge files
            las_file.seek(0)
            
            height_samples = []
            
            for points in las_file.chunk_iterator(utils.CHUNK_SIZE):
                x = points.x
                y = points.y
                z = points.z

                # Map points to DTM pixels
                col_indices = np.clip(((x - min_x) / HIST_RESOLUTION).astype(int), 0, width - 1)
                row_indices = np.clip(((max_y - y) / HIST_RESOLUTION).astype(int), 0, height - 1)
                
                # Look up ground elevation for each point
                ground_z = dtm_grid[row_indices, col_indices]
                
                # Calculate Height Above Ground
                normalized_z = z - ground_z
                
                # Filter out Nodatas and Ground points
                # (Ignore points where DTM was infinite or height is near 0)
                valid_mask = (ground_z != utils.DEFAULT_NODATA) & (normalized_z > GROUND_THRESHOLD)
                
                # Append to our sample list
                # If valid_mask is empty, skip
                if np.any(valid_mask):
                    height_samples.append(normalized_z[valid_mask])

            if not height_samples:
                raise Exception("No non-ground points found.")

            # Flatten list of arrays into one big array
            all_heights = np.concatenate(height_samples)

            # 4. Plot Histogram
            plt.figure(figsize=(10, 6))
            
            # Use 99th percentile to cut off extreme noise outliers for the chart range
            max_h = np.percentile(all_heights, 99.9)
            
            counts, bins, patches = plt.hist(
                all_heights, 
                bins=75, 
                range=(0, max_h), 
                color='#cc163e', 
                edgecolor='black', 
                alpha=0.7
            )
            
            plt.xticks(np.arange(0, max_h + 2, 2))
            plt.title(f"Canopy Height Distribution: {os.path.basename(input_path)}\n(Points > {GROUND_THRESHOLD}m)")
            plt.xlabel("Height Above Ground (meters)")
            plt.ylabel("Point Count")
            plt.grid(axis='y', alpha=0.5)
            
            # Add mean/median text
            mean_h = np.mean(all_heights)
            median_h = np.median(all_heights)
            plt.axvline(mean_h, color='blue', linestyle='dashed', linewidth=1, label=f'Mean: {mean_h:.1f}m')
            plt.legend()

            plt.savefig(output_path)
            plt.close()

            return {"status": "success", "file": output_path}

    except Exception as e:
        return {"status": "error", "message": str(e), "traceback": traceback.format_exc()}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input_path", help="Path to input LAS/LAZ file")
    parser.add_argument("output_path", help="Path to save the PNG histogram")
    args = parser.parse_args()

    result = generate_normalized_histogram(args.input_path, args.output_path)
    print(json.dumps(result))