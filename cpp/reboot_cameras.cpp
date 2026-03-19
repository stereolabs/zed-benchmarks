// Reboot cameras to reset nvargus-daemon (Jetson only).
// Mirrors restartArgusAndWait() from the MultiCameras test suite.

#include <sl/Camera.hpp>
#include <iostream>

int main() {
    auto err = sl::Camera::reboot(sl::INPUT_TYPE::GMSL);
    if (err != sl::ERROR_CODE::SUCCESS) {
        std::cerr << "Error: Failed to restart nvargus-daemon: " << sl::toString(err) << std::endl;
        return 1;
    }
    std::cout << "Reboot OK — nvargus-daemon restarted" << std::endl;
    return 0;
}
