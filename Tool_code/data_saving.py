import numpy as np
import scipy.io as sio

def save_experiment_data(path, all_data, pos_mapping, w, h, records_per_point):

    print(f"💾 Saving to {path}...")
    try:
        flattened = [buf for point_bufs in all_data for buf in point_bufs]
        raw_matrix = np.vstack(flattened)

        final_data = (raw_matrix / records_per_point).astype(np.uint16)
        
        pos_numeric = np.array([[float(v) for v in s.split(',')] for s in pos_mapping])
        
        sio.savemat(path, {
            "raw_data": final_data,
            "pos_map": pos_numeric,
            "params": {"W": w, "H": h}
        }, do_compression=True)
        print("✅ Success!")
    except Exception as e:
        print(f"❌ Save Failed: {e}")