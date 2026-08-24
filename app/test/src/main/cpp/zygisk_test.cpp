#include <sys/types.h>

#include "api.hpp"

extern "C" [[gnu::visibility("default"), gnu::used]]
#if KITSUNE_ZYGISK_TEST_DLCLOSE
const char kitsune_zygisk_test_contract[] =
    "kitsune-zygisk-test-v1:minimal-dlclose";

class TestModule final : public zygisk::ModuleBase {
public:
    void onLoad(zygisk::Api *api, JNIEnv *) override {
        api->setOption(zygisk::DLCLOSE_MODULE_LIBRARY);
    }
};
#else
const char kitsune_zygisk_test_contract[] =
    "kitsune-zygisk-test-v1:minimal-resident";

class TestModule final : public zygisk::ModuleBase {};
#endif

REGISTER_ZYGISK_MODULE(TestModule)
