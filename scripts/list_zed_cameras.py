#!/usr/bin/env python3
"""List connected ZED cameras and output their serial numbers.

Used by run_multi_process.sh to discover cameras and assign one per process.
Output format: one serial number per line.

Flags:
    --stereo-only   List only stereo (Camera) devices.
    --mono-only     List only mono (CameraOne) devices.
    (default)       List all devices, stereo first.
"""
import argparse
import sys

try:
    import pyzed.sl as sl
except ImportError:
    print("ERROR: pyzed not found", file=sys.stderr)
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="List connected ZED cameras")
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--stereo-only', action='store_true',
                       help='List only stereo (ZED X / ZED 2i) cameras')
    group.add_argument('--mono-only', action='store_true',
                       help='List only mono (ZED X One) cameras')
    args = parser.parse_args()

    # Discover stereo cameras
    stereo_devices = sl.Camera.get_device_list()
    # Discover mono cameras
    try:
        mono_devices = sl.CameraOne.get_device_list()
    except AttributeError:
        mono_devices = []

    # Build list based on filter
    seen_serials = set()
    all_serials = []

    if not args.mono_only:
        for dev in stereo_devices:
            sn = dev.serial_number
            if sn not in seen_serials:
                seen_serials.add(sn)
                all_serials.append(sn)

    if not args.stereo_only:
        for dev in mono_devices:
            sn = dev.serial_number
            if sn not in seen_serials:
                seen_serials.add(sn)
                all_serials.append(sn)

    if not all_serials:
        kind = "stereo" if args.stereo_only else "mono" if args.mono_only else ""
        print(f"ERROR: No {kind + ' ' if kind else ''}ZED cameras detected",
              file=sys.stderr)
        sys.exit(1)

    for sn in all_serials:
        print(sn)

if __name__ == "__main__":
    main()
