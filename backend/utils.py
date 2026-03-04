import json
import sys
import os
import time
import numpy as np
import rasterio
from rasterio.transform import from_origin

# --- SHARED CONFIGURATION ---
DEFAULT_NODATA = -9999
CHUNK_SIZE = 1_000_000

def send_progress(percentage, message):
    """
    Prints a JSON line specifically for the Electron app to read as a progress update.
    flush=True is critical to ensure it sends immediately.
    """
    data = {
        "progress": int(percentage),
        "text": message
    }
    print(json.dumps(data), flush=True)

def get_grid_dimensions(header, resolution):
    min_x, min_y, _ = header.mins
    max_x, max_y, _ = header.maxs
    width = int(np.ceil((max_x - min_x) / resolution))
    height = int(np.ceil((max_y - min_y) / resolution))
    transform = from_origin(min_x, max_y, resolution, resolution)
    return width, height, transform, (min_x, max_y)

def save_raster(output_path, data, width, height, transform, crs):
    # Ensure data is float32 for consistency
    if data.dtype != np.float32:
        data = data.astype(np.float32)
        
    with rasterio.open(
        output_path, 'w', driver='GTiff', height=height, width=width, count=1,
        dtype=data.dtype, crs=crs, transform=transform, nodata=DEFAULT_NODATA
    ) as dst:
        dst.write(data, 1)

class FileLock:
    """
    A simple cross-process lock using a .lock file.
    Used to prevent the background pre-cacher and the foreground generator
    from writing the file at the same time.
    """
    def __init__(self, filepath, timeout=300):
        self.lock_file = filepath + ".lock"
        self.timeout = timeout

    def acquire(self):
        """Wait for lock to be free, then acquire it."""
        start_time = time.time()
        while os.path.exists(self.lock_file):
            if time.time() - start_time > self.timeout:
                raise TimeoutError(f"Timed out waiting for lock: {self.lock_file}")
            time.sleep(1) # Check every second
        
        # Create lock file
        with open(self.lock_file, 'w') as f:
            f.write("LOCKED")

    def release(self):
        """Remove lock file."""
        if os.path.exists(self.lock_file):
            os.remove(self.lock_file)

    def is_locked(self):
        return os.path.exists(self.lock_file)

    def wait(self):
        """Just wait for the lock to clear, don't acquire it."""
        start_time = time.time()
        while os.path.exists(self.lock_file):
            if time.time() - start_time > self.timeout:
                break
            time.sleep(1)