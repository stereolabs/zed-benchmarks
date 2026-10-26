// Reboot cameras before a test run

#include <sl/Camera.hpp>
#include <iostream>

static bool reboot_ok(sl::ERROR_CODE err) {
    return err == sl::ERROR_CODE::SUCCESS || err == sl::ERROR_CODE::CAMERA_NOT_DETECTED;
}

int main() {
    int rc = 0;

    // USB cameras: full-reset every connected ZED 2 / 2i MCU.
    std::cout << "Rebooting USB cameras (MCU reset)..." << std::endl;
    auto usb_err = sl::Camera::reboot(sl::INPUT_TYPE::USB);
    if (!reboot_ok(usb_err)) {
        std::cerr << "Error: USB camera reboot failed: " << sl::toString(usb_err) << std::endl;
        rc = 1;
    }

    // GMSL cameras: restart nvargus-daemon (no-op off Jetson).
    std::cout << "Rebooting GMSL cameras (nvargus-daemon restart)..." << std::endl;
    auto gmsl_err = sl::Camera::reboot(sl::INPUT_TYPE::GMSL);
    if (!reboot_ok(gmsl_err)) {
        std::cerr << "Error: Failed to restart nvargus-daemon: " << sl::toString(gmsl_err) << std::endl;
        rc = 1;
    }

    if (rc == 0)
        std::cout << "Reboot OK" << std::endl;
    return rc;
}
