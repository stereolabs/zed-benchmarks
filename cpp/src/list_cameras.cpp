/**
 * Minimal ZED camera lister: used as a fallback by run_multi_process.sh
 * when the Python pyzed module is not installed.
 *
 * Output: one serial number per line (same format as list_zed_cameras.py).
 *
 * Flags:
 *   --stereo-only   List only stereo (Camera) devices.
 *   --mono-only     List only mono (CameraOne) devices.
 *   (default)       List all devices, stereo first.
 */

#include <sl/Camera.hpp>
#include <sl/CameraOne.hpp>
#include <iostream>
#include <string>
#include <set>
#include <vector>
#include <cstring>

int main(int argc, char* argv[]) {
    bool stereo_only = false;
    bool mono_only = false;

    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--stereo-only") == 0)
            stereo_only = true;
        else if (std::strcmp(argv[i], "--mono-only") == 0)
            mono_only = true;
    }

    std::set<unsigned int> seen;
    std::vector<unsigned int> serials;

    if (!mono_only) {
        auto stereo_devs = sl::Camera::getDeviceList();
        for (auto& d : stereo_devs) {
            unsigned int sn = d.serial_number;
            if (seen.insert(sn).second)
                serials.push_back(sn);
        }
    }

    if (!stereo_only) {
        auto mono_devs = sl::CameraOne::getDeviceList();
        for (auto& d : mono_devs) {
            unsigned int sn = d.serial_number;
            if (seen.insert(sn).second)
                serials.push_back(sn);
        }
    }

    if (serials.empty()) {
        std::string kind;
        if (stereo_only)
            kind = "stereo ";
        else if (mono_only)
            kind = "mono ";
        std::cerr << "ERROR: No " << kind << "ZED cameras detected" << std::endl;
        return 1;
    }

    for (auto sn : serials)
        std::cout << sn << "\n";

    return 0;
}
