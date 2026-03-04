import laspy
import numpy as np
import sys
import os

def declassify_ground(input_file, output_file):
    if not os.path.exists(input_file):
        print(f"Error: File '{input_file}' not found.")
        return

    try:
        print(f"Reading {input_file}...")
        las = laspy.read(input_file)
        
        # ASPRS Standard: Class 2 = Ground, Class 1 = Unclassified
        # Create a mask for all points currently classified as Ground (2)
        ground_mask = (las.classification == 2)
        ground_count = np.count_nonzero(ground_mask)
        
        if ground_count == 0:
            print("No ground points (Class 2) found in this file.")
            # Optional: Uncomment below if you want to save anyway
            # las.write(output_file)
            return

        print(f"Found {ground_count} ground points. Declassifying to 'Unclassified' (Class 1)...")
        
        # Update the classification array
        # We change them to 1 (Unclassified). 
        # Use 0 if you prefer "Created, never classified".
        las.classification[ground_mask] = 1
        
        print(f"Writing output to {output_file}...")
        las.write(output_file)
        print("Done!")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    # Usage: python declassify.py input.laz output.laz
    if len(sys.argv) < 3:
        print("Usage: python declassify.py <input_file> <output_file>")
    else:
        declassify_ground(sys.argv[1], sys.argv[2])